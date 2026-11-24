"""Explicit Questionnaire source resources and commands; default bootstrap is separate."""

from copy import deepcopy
from dataclasses import dataclass

from adapters.questionnaire_source_admission import QuestionnaireOwnerFenceReader
from adapters.questionnaire_source_admission import QuestionnaireProfileSourceReceiver
from adapters.questionnaire_source_admission import QuestionnaireSourceAdmissionAdapter
from adapters.questionnaire_source_material import QuestionnaireSourceMaterialPreparer
from adapters.questionnaire_source_material import QuestionnaireSourceSnapshotReader
from adapters.questionnaire_source_store import MongoQuestionnaireSavedSourceReader
from adapters.questionnaire_source_store import MongoQuestionnaireSourceStore
from modules.analysis.snapshots import DocumentScope
from modules.questionnaire.profile_publication import QuestionnaireSourcePublicationService
from modules.questionnaire.profile_source import QuestionnaireSourcePolicy
from modules.questionnaire.profile_source import execution_selection


@dataclass(frozen=True)
class QuestionnaireSourceResources:
    store: object
    owner: object
    snapshot_reader: object
    fence_reader: object


def create_questionnaire_source_resources(*, collection, questionnaires):
    store = MongoQuestionnaireSourceStore(collection)
    owner = MongoQuestionnaireSavedSourceReader(questionnaires)
    return QuestionnaireSourceResources(
        store, owner, QuestionnaireSourceSnapshotReader(store), QuestionnaireOwnerFenceReader(store=store, owner=owner)
    )


class QuestionnaireProfileSourceRouter:
    def __init__(self, publication, *, policy=QuestionnaireSourcePolicy()):
        self.publication, self.policy = publication, policy

    def execution_selection(self):
        return execution_selection(self.policy)

    async def ensure_queued(self, *, user_id, questionnaire_id):
        return await self.publication.ensure_queued(user_id=user_id, questionnaire_id=questionnaire_id)

    async def recover_pending(self, *, limit=100):
        return await self.publication.recover_pending(limit=limit)


def create_questionnaire_profile_source(
    *,
    resources,
    inbox,
    presence,
    fresh_admission,
    submitted,
    attachments,
    policy=QuestionnaireSourcePolicy(),
    clock=None,
):
    options = {} if clock is None else {'clock': clock}
    publication = QuestionnaireSourcePublicationService(
        store=resources.store,
        owner=resources.owner,
        materials=QuestionnaireSourceMaterialPreparer(owner=resources.owner, store=resources.store),
        admission=QuestionnaireSourceAdmissionAdapter(
            store=resources.store,
            owner=resources.owner,
            inbox=inbox,
            presence=presence,
            fresh_admission=fresh_admission,
        ),
        receiver=QuestionnaireProfileSourceReceiver(inbox),
        submitted=submitted,
        attachments=attachments,
        **options,
    )
    return QuestionnaireProfileSourceRouter(publication, policy=policy)


class QuestionnaireDocumentAttachmentAdapter:
    """Translate the domain's dict scope to Document's existing public value object."""

    def __init__(self, publisher):
        self.publisher = publisher

    @staticmethod
    def _scope(value):
        value = deepcopy(value)
        if not isinstance(value, dict) or set(value) != {'userId', 'subjectId'}:
            raise ValueError('Questionnaire attachment scope must be explicit')
        return DocumentScope(user_id=value['userId'], subject_id=value['subjectId'])

    async def prepare(
        self, *, scope, request_id, submitted_reference, submitted_digest, questionnaire_event, selection
    ):
        scope = self._scope(scope)
        reference, event = deepcopy(submitted_reference), deepcopy(questionnaire_event)
        return deepcopy(
            await self.publisher.prepare(
                scope=scope,
                request_id=request_id,
                submitted_reference=reference,
                submitted_digest=submitted_digest,
                questionnaire_event=event,
                selection=selection,
            )
        )

    async def publish(self, *, scope, command_id):
        return deepcopy(await self.publisher.publish(scope=self._scope(scope), command_id=command_id))
