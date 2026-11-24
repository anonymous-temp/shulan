"""Offline Questionnaire source stores with real BSON round trips and controlled providers."""

from copy import deepcopy
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bson import BSON

from adapters.persistence.questionnaires import MongoQuestionnaireStore
from adapters.questionnaire_profile_source import create_questionnaire_source_resources
from adapters.questionnaire_source_material import QuestionnaireSourceMaterialPreparer
from modules.health_profile.contracts import ProfileSourceReceiptV1
from modules.questionnaire.profile_publication import QuestionnaireSourcePublicationService
from modules.questionnaire.profile_publication import source_task_id
from modules.questionnaire.profile_source import QuestionnaireSourcePolicy
from modules.questionnaire.profile_source import execution_selection
from modules.questionnaire.profile_source import prepare_submission_source
from modules.questionnaire.profile_source import source_digest
from modules.questionnaire.schemas import normalize_answers
from modules.questionnaire.schemas import validate_questionnaire
from modules.questionnaire.service import QuestionnaireService
from modules.questionnaire.submitted_material import QuestionnaireSubmittedMaterialReader
from tests.unit.services.analysis.fakes import MemoryMongoCollection
from tests.unit.services.analysis.fakes import MemoryMongoCursor


class Cursor(MemoryMongoCursor):
    def limit(self, count):
        self.rows = self.rows[:count]
        return self

    def max_time_ms(self, value):
        assert value == 5000
        return self

    def collation(self, value):
        assert value.document == {'locale': 'simple'}
        return self


class Collection(MemoryMongoCollection):
    def __init__(self):
        super().__init__()
        self.options = []

    def with_options(self, **options):
        self.options.append(options)
        return self

    def matches(self, item, filters):
        for key, expected in filters.items():
            if isinstance(expected, dict):
                actual = self.value(item, key)
                if '$nin' in expected and actual in expected['$nin']:
                    return False
                if '$ne' in expected and actual == expected['$ne']:
                    return False
                if '$exists' in expected and self.has_field(item, key) != expected['$exists']:
                    return False
        return super().matches(item, filters)

    async def insert_one(self, value):
        return await super().insert_one(BSON.encode(value).decode())

    async def replace_one(self, query, value):
        for key, old in self.documents.items():
            if self.matches(old, query):
                self.documents[key] = BSON.encode(value).decode()
                return SimpleNamespace(acknowledged=True, modified_count=1)
        return SimpleNamespace(acknowledged=True, modified_count=0)

    def find(self, query, projection=None):
        return Cursor([row for row in self.documents.values() if self.matches(row, query)])


def submission(*, user_id='u', questionnaire_id='q', policy=QuestionnaireSourcePolicy(), label='我最近头痛'):
    survey = validate_questionnaire(
        {
            'title': '问卷',
            'pages': [
                {
                    'name': 'p',
                    'elements': [
                        {'name': 'z_note', 'title': '情况', 'type': 'text'},
                        {
                            'name': 'a_choice',
                            'title': '症状',
                            'type': 'radiogroup',
                            'choices': [{'value': 'x', 'text': label}],
                        },
                    ],
                }
            ],
        }
    )
    answers = {'z_note': '  原始文字  ', 'a_choice': 'x'}
    rows, canonical = normalize_answers(survey, answers)
    source = {
        'userId': user_id,
        'sessionId': 's',
        'questionnaireId': questionnaire_id,
        'status': 'ready',
        'questionnaire': survey,
        'reports': [],
        'artifacts': [],
        'profileSourceSelection': execution_selection(policy),
    }
    pending = {
        'acceptedAt': '2026-11-10T00:00:00+00:00',
        'rawAnswers': answers,
        'answers': rows,
        'canonicalAnswers': canonical,
        'answerHash': source_digest(canonical),
        'rawAnswerHash': source_digest(answers),
    }
    service = object.__new__(QuestionnaireService)
    service.clock, service.factor_tasks = lambda: 1794268800.0, None
    return service._submission_document(source, pending)


class Admission:
    def __init__(self, order):
        self.order, self.failure = order, None

    async def prepare_self_scope(self, user_id):
        self.order.append('scope')
        if self.failure:
            raise RuntimeError('Profile unavailable')
        return {'userId': user_id, 'subjectId': 'self'}

    async def reserve(self, material):
        self.order.append('reserve')
        event = material['event']
        return {
            'status': 'blocked',
            'authorization': {
                'admissionKind': 'fresh',
                'scope': event['scope'],
                'sourceType': 'questionnaire',
                'sourceId': event['sourceId'],
                'authorizationId': 'auth',
                'ownerFenceRef': {'provider': 'questionnaire', 'snapshotId': 'proof'},
                'materialContractVersion': 'profile.questionnaire-source.v2',
                'materialSnapshotRef': event['snapshotRef'],
                'materialContentDigest': event['contentDigest'],
            },
        }

    async def authorize(self, reservation):
        self.order.append('ready')
        return {**deepcopy(reservation), 'status': 'ready'}


class Attachments:
    def __init__(self, order):
        self.order, self.prepared, self.published = order, [], []
        self.failure, self.ack_lost = False, False
        self.command = None

    async def prepare(self, **kwargs):
        self.order.append('attachments_prepare')
        self.prepared.append(deepcopy(kwargs))
        event = kwargs['questionnaire_event']
        result = {
            'contractVersion': 'document.questionnaire-attachment-command.v1',
            'commandId': 'attachments_' + source_digest(kwargs),
            'commandDigest': source_digest(kwargs),
            'scope': kwargs['scope'],
            'questionnaireId': event['sourceId'],
            'questionnaireSourceRevision': 1,
            'status': 'prepared',
            'items': [],
            'excluded': [],
        }
        if self.command is not None:
            assert self.command == result
        self.command = deepcopy(result)
        return result

    async def publish(self, **kwargs):
        self.order.append('attachments_publish')
        self.published.append(deepcopy(kwargs))
        if self.failure:
            raise RuntimeError('Attachment delivery unavailable')
        receipt = {
            'contractVersion': 'document.questionnaire-attachment-receipt.v1',
            'commandId': self.command['commandId'],
            'commandDigest': self.command['commandDigest'],
            'status': 'accepted',
            'items': [],
            'excluded': [],
        }
        if self.ack_lost:
            self.ack_lost = False
            raise RuntimeError('Attachment ACK lost after durable acceptance')
        return receipt


class Receiver:
    def __init__(self, order):
        self.order, self.events, self.lookups = order, [], []
        self.ack_lost, self.status = False, 'received'

    def receipt(self, event):
        return ProfileSourceReceiptV1(
            event_id=event['eventId'],
            publication_id='pub',
            scope=event['scope'],
            source_type='questionnaire',
            source_id=event['sourceId'],
            publication_revision=1,
            status=self.status,
            received_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            application_state='applied' if self.status == 'applied' else 'not_applied',
            watermark={
                **{key: event[key] for key in ('scope', 'sourceType', 'sourceId')},
                'acceptedPublicationRevision': 1,
                'appliedPublicationRevision': int(self.status == 'applied'),
            },
        ).model_dump(mode='json', by_alias=True)

    async def receive(self, event):
        self.order.append('answers')
        self.events.append(deepcopy(event))
        if self.ack_lost:
            self.ack_lost = False
            raise RuntimeError('Answer ACK lost')
        return self.receipt(event)

    async def get_receipt(self, event):
        self.lookups.append(deepcopy(event))
        return self.receipt(event)


def environment(policy=QuestionnaireSourcePolicy()):
    source, owned = Collection(), Collection()
    resources = create_questionnaire_source_resources(collection=owned, questionnaires=source)
    order, now = [], [datetime(2026, 11, 10, tzinfo=UTC).timestamp()]
    admission, receiver, attachments = Admission(order), Receiver(order), Attachments(order)
    submitted = QuestionnaireSubmittedMaterialReader(store=MongoQuestionnaireStore(source), archive=AsyncMock())
    service = QuestionnaireSourcePublicationService(
        store=resources.store,
        owner=resources.owner,
        materials=QuestionnaireSourceMaterialPreparer(owner=resources.owner, store=resources.store),
        admission=admission,
        receiver=receiver,
        submitted=submitted,
        attachments=attachments,
        clock=lambda: now[0],
    )
    return SimpleNamespace(
        source=source,
        owned=owned,
        resources=resources,
        service=service,
        order=order,
        now=now,
        admission=admission,
        receiver=receiver,
        attachments=attachments,
        submitted=submitted,
        raw=submission(policy=policy),
    )


async def save(env):
    await env.source.insert_one({'_id': env.raw['questionnaireId'], **deepcopy(env.raw)})


async def task(env, questionnaire_id='q'):
    return await env.resources.store.get(source_task_id('u', questionnaire_id))


def add_attachment(env):
    interpretation = {'observations': [{'itemName': '血糖', 'resultValue': '6.4', 'unit': 'mmol/L'}]}
    env.raw['artifacts'] = [
        {'artifactId': 'a', 'questionnaireId': env.raw['questionnaireId'], 'kind': 'questionnaire_document'}
    ]
    env.raw['reports'] = [{'artifactId': 'a', 'status': 'partial', 'interpretation': interpretation}]
    env.raw['submissionResponse']['reports'] = deepcopy(env.raw['reports'])
    prepare_submission_source(env.raw)
    env.submitted.archive = SimpleNamespace(
        get=AsyncMock(return_value={'userId': 'u', 'questionnaireId': env.raw['questionnaireId'], 'retained': True}),
        read_payload=AsyncMock(return_value=deepcopy(interpretation)),
    )
    return {'ordinal': 0, 'artifactId': 'a', 'contentKey': 'a', 'interpretationDigest': source_digest(interpretation)}
