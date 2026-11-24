"""Fixed Questionnaire material reads; consumers never normalize or publish while reading."""

from copy import deepcopy
from datetime import datetime

from modules.health_profile.contracts import ProfileQuestionnaireSourceSnapshotV2
from modules.health_profile.contracts import ProfileScope
from modules.health_profile.contracts import ProfileSourceEventV1
from modules.health_profile.contracts import ProfileSourceSnapshotRefV1
from modules.questionnaire.profile_source import CONSUMER_REVISION
from modules.questionnaire.profile_source import MATERIAL_CONTRACT
from modules.questionnaire.profile_source import QuestionnaireSourceConflict
from modules.questionnaire.profile_source import answer_evidence_v1
from modules.questionnaire.profile_source import source_digest


def snapshot_from_raw(raw, *, scope):
    scope = ProfileScope.model_validate(scope)
    proof = raw['submissionProof']
    if proof['userId'] != scope.user_id:
        raise PermissionError('Questionnaire source scope differs from its saved owner')
    snapshot = {
        'contractVersion': MATERIAL_CONTRACT,
        'scope': scope.model_dump(mode='json', by_alias=True),
        'sourceType': 'questionnaire',
        'sourceId': proof['questionnaireId'],
        'sourceRevision': 1,
        'sessionId': proof['sessionId'],
        'status': 'submitted',
        'submittedAt': proof['submittedAt'],
        'submissionProof': deepcopy(proof),
        'answers': answer_evidence_v1(raw),
    }
    canonical = ProfileQuestionnaireSourceSnapshotV2.model_validate(snapshot).model_dump(mode='json', by_alias=True)
    if canonical != snapshot:
        raise QuestionnaireSourceConflict('Profile V2 changed the fixed Questionnaire source evidence')
    return canonical


def validate_descriptor(value):
    value = deepcopy(value)
    event = ProfileSourceEventV1.model_validate(value['event'])
    binding = value['binding']
    if (
        value.get('consumerRevision') != CONSUMER_REVISION
        or event.source_type != 'questionnaire'
        or event.source_revision != 1
        or event.publication_revision != 1
        or event.prior_revision is not None
        or event.event_kind != 'publish'
        or event.legacy_source_revision is not None
        or event.scope.model_dump(mode='json', by_alias=True) != value.get('scope')
        or event.snapshot_ref.provider != 'questionnaire'
        or event.snapshot_ref.contract_version != MATERIAL_CONTRACT
        or event.snapshot_ref.snapshot_id != 'questionnaire_source_' + event.content_digest
        or event.content_digest != value['snapshotReference']['contentDigest']
        or binding.get('draftDigest') != value['rawReference']['contentDigest']
        or event.source_id != binding.get('sourceId')
        or event.occurred_at != datetime.fromisoformat(binding['createdAt'])
        or event.event_id
        != 'questionnaire_event_' + source_digest([value['scope'], event.source_id, 1, event.content_digest])
    ):
        raise QuestionnaireSourceConflict('Questionnaire material descriptor changed its fixed event binding')
    proof = value['submissionProof']
    if (
        proof.get('userId') != event.scope.user_id
        or proof.get('questionnaireId') != event.source_id
        or proof.get('sourceRevision') != event.source_revision
        or proof.get('submittedAt') != binding['createdAt']
    ):
        raise QuestionnaireSourceConflict('Questionnaire submission proof differs from its fixed event')
    return value, event


class QuestionnaireSourceMaterialPreparer:
    def __init__(self, *, owner, store):
        self.owner, self.store = owner, store

    async def prepare(self, binding, *, scope):
        binding, scope = deepcopy(binding), deepcopy(scope)
        actual, raw = await self.owner.read_raw(user_id=scope['userId'], questionnaire_id=binding['sourceId'])
        if actual != binding:
            raise QuestionnaireSourceConflict('Questionnaire submission changed before material preparation')
        raw_reference = await self.store.put_blob(raw)
        snapshot = snapshot_from_raw(raw, scope=scope)
        snapshot_reference = await self.store.put_blob(snapshot)
        digest = source_digest(snapshot)
        ref = ProfileSourceSnapshotRefV1(
            provider='questionnaire', contract_version=MATERIAL_CONTRACT, snapshot_id='questionnaire_source_' + digest
        )
        event = ProfileSourceEventV1(
            event_id='questionnaire_event_' + source_digest([scope, binding['sourceId'], 1, digest]),
            scope=scope,
            source_type='questionnaire',
            source_id=binding['sourceId'],
            source_revision=1,
            publication_revision=1,
            event_kind='publish',
            snapshot_ref=ref,
            content_digest=digest,
            occurred_at=datetime.fromisoformat(binding['createdAt']),
        )
        result = {
            'consumerRevision': CONSUMER_REVISION,
            'scope': scope,
            'binding': binding,
            'event': event.model_dump(mode='json', by_alias=True),
            'rawReference': raw_reference,
            'snapshotReference': snapshot_reference,
            'submissionProof': deepcopy(raw['submissionProof']),
        }
        validate_descriptor(result)
        await self.store.immutable({'_id': ref.snapshot_id, 'recordKind': 'source_material', 'material': result})
        return deepcopy(result)


class QuestionnaireSourceSnapshotReader:
    """Only a source store is required; construction cannot create a publication cycle."""

    def __init__(self, store):
        self.store = store

    async def read_source_snapshot(self, snapshot_ref, *, scope):
        snapshot_ref = ProfileSourceSnapshotRefV1.model_validate(snapshot_ref)
        scope = ProfileScope.model_validate(scope)
        if snapshot_ref.provider != 'questionnaire' or snapshot_ref.contract_version != MATERIAL_CONTRACT:
            raise QuestionnaireSourceConflict('Unsupported Questionnaire source snapshot selection')
        row = await self.store.get(snapshot_ref.snapshot_id)
        if row is None or row.get('recordKind') != 'source_material':
            raise FileNotFoundError('The exact Questionnaire source material is unavailable')
        material, event = validate_descriptor(row['material'])
        if event.scope != scope or event.snapshot_ref != snapshot_ref:
            raise PermissionError('Questionnaire source material has another scope or identity')
        raw = await self.store.read_blob(material['rawReference'])
        snapshot = await self.store.read_blob(material['snapshotReference'])
        if (
            source_digest(raw) != material['binding']['draftDigest']
            or raw['submissionProof'] != material['submissionProof']
            or snapshot_from_raw(raw, scope=scope) != snapshot
            or source_digest(snapshot) != event.content_digest
        ):
            raise QuestionnaireSourceConflict('Questionnaire immutable source material changed')
        return deepcopy(snapshot)
