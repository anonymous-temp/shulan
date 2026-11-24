"""Exact public Profile admission backed by Questionnaire's independent immutable owner proof."""

from copy import deepcopy
from datetime import datetime

from adapters.questionnaire_source_material import snapshot_from_raw
from adapters.questionnaire_source_material import validate_descriptor
from modules.health_profile.contracts import ProfileFreshSourceAuthorizationV1
from modules.health_profile.contracts import ProfileOwnerAdmissionFenceV1
from modules.health_profile.contracts import ProfileSourceEventV1
from modules.health_profile.contracts import ProfileSourceReceiptV1
from modules.health_profile.contracts import ProfileSourceSnapshotRefV1
from modules.health_profile.contracts import profile_content_digest
from modules.questionnaire.profile_publication import source_task_id
from modules.questionnaire.profile_source import QuestionnaireSourceConflict
from modules.questionnaire.profile_source import saved_binding
from modules.questionnaire.profile_source import source_digest

ADMISSION_REVISION = 'questionnaire-source-admission.v1'


class QuestionnaireAdmissionUnavailable(RuntimeError):
    pass


def _seal(row):
    row['recordDigest'] = source_digest(row)
    return row


def _immutable(row):
    if row.get('implementationRevision') != ADMISSION_REVISION or row.get('recordDigest') != source_digest(
        {key: value for key, value in row.items() if key != 'recordDigest'}
    ):
        raise QuestionnaireSourceConflict('the fixed Questionnaire admission record changed or is unsupported')
    return row


class QuestionnaireOwnerFenceReader:
    """Historical proof reads are pure; held checks re-read only the fixed saved submission.

    New-mode submissions are born with their mode in the same business replace. The
    compatible legacy sender refuses that mode before enqueuing, independently of
    task/cursor existence. Factor assessment updates do not change this immutable submission binding.
    """

    def __init__(self, *, store, owner):
        self.store, self.owner = store, owner

    async def _read(self, scope, ref):
        if ref.provider != 'questionnaire' or ref.contract_version != 'profile-owner-admission-fence.v1':
            raise QuestionnaireSourceConflict('unsupported Questionnaire owner fence selection')
        row = await self.store.get(ref.snapshot_id)
        if row is None or row.get('recordKind') != 'owner_fence':
            raise LookupError('exact Questionnaire admission fence is unavailable')
        _immutable(row)
        proof = ProfileOwnerAdmissionFenceV1.model_validate(row['proof'])
        material, event = validate_descriptor(row['material'])
        if (
            proof.scope != scope
            or proof.source_type != 'questionnaire'
            or proof.source_id != event.source_id
            or event.scope != scope
            or proof.prepared_manifest_ref != material['binding']['manifestDigest']
            or proof.fence_id != ref.snapshot_id
            or row['proofDigest'] != profile_content_digest(row['proof'])
        ):
            raise QuestionnaireSourceConflict('Questionnaire owner proof has a conflicting immutable binding')
        return row

    async def read_admission_fence(self, *, scope, snapshot_ref):
        return deepcopy((await self._read(scope, snapshot_ref))['proof'])

    async def is_admission_fence_held(self, *, scope, snapshot_ref, content_digest):
        row = await self._read(scope, snapshot_ref)
        if row['proofDigest'] != content_digest:
            return False
        current = await self.owner.read_source(user_id=scope.user_id, questionnaire_id=row['proof']['sourceId'])
        return current == row['binding']


class QuestionnaireSourceAdmissionAdapter:
    """No caller-supplied quiescence. Freeze authorization before the first remote reserve."""

    def __init__(self, *, store, owner, inbox, presence, fresh_admission):
        self.store, self.owner = store, owner
        self.inbox, self.presence, self.fresh = inbox, presence, fresh_admission

    async def prepare_self_scope(self, user_id):
        return (await self.inbox.prepare_self_scope(user_id)).model_dump(mode='json', by_alias=True)

    @staticmethod
    def identity(event):
        return 'questionnaire_admission_' + source_digest([event.scope.user_id, event.source_id])

    async def reserve(self, material):
        material, event = validate_descriptor(material)
        identity = self.identity(event)
        stored = await self.store.get(identity)
        if stored is None:
            session = await self.owner.read_submission(user_id=event.scope.user_id, questionnaire_id=event.source_id)
            binding = saved_binding(session)
            task = await self.store.get(source_task_id(event.scope.user_id, event.source_id))
            if (
                task is None
                or task.get('binding') != binding
                or task.get('scope') != event.scope.model_dump(by_alias=True)
                or task.get('material') != material
            ):
                raise QuestionnaireSourceConflict(
                    'Questionnaire admission requires its own exact durable prepared task'
                )
            if any(
                task.get(key) is not None
                for key in ('reservation', 'authorization', 'receipt', 'attachmentCommand', 'attachmentReceipt')
            ) or task.get('dispatchAttempts', 0):
                raise QuestionnaireSourceConflict(
                    'Questionnaire already has formal delivery without this owner admission'
                )
            _, raw = await self.owner.read_raw(user_id=event.scope.user_id, questionnaire_id=event.source_id)
            if (
                profile_content_digest(snapshot_from_raw(raw, scope=event.scope)) != event.content_digest
                or profile_content_digest(raw) != material['binding']['draftDigest']
            ):
                raise QuestionnaireSourceConflict('fresh admission requires the exact saved Questionnaire submission')
            fence_id = 'questionnaire_fence_' + source_digest(
                [event.scope.model_dump(by_alias=True), event.source_id, material['binding']['manifestDigest']]
            )
            proof = ProfileOwnerAdmissionFenceV1(
                scope=event.scope,
                source_type='questionnaire',
                source_id=event.source_id,
                fence_id=fence_id,
                fence_generation=1,
                prepared_manifest_ref=material['binding']['manifestDigest'],
                formal_history_absent=True,
                legacy_intents_absent=True,
                active_legacy_writers=0,
                active_legacy_workers=0,
                legacy_admission_blocked=True,
                issued_at=datetime.fromisoformat(material['binding']['createdAt']),
            ).model_dump(mode='json', by_alias=True)
            fence = _seal(
                {
                    'implementationRevision': ADMISSION_REVISION,
                    '_id': fence_id,
                    'recordKind': 'owner_fence',
                    'proof': proof,
                    'proofDigest': profile_content_digest(proof),
                    'binding': binding,
                    'material': material,
                }
            )
            persisted = await self.store.insert(fence)
            if persisted != fence:
                raise QuestionnaireSourceConflict('Questionnaire admission fence was already frozen for another source')
            # Inspect all historical Profile jobs/facts/publications across subjects,
            # not merely this delivery queue or unsettled legacy jobs.
            presence = await self.presence.read_legacy_presence(
                event.scope, source_type='questionnaire', source_id=event.source_id
            )
            if (
                presence.scope != event.scope
                or presence.source_type != 'questionnaire'
                or presence.source_id != event.source_id
                or presence.status != 'absent'
                or {part.kind for part in presence.sections} != {'jobs', 'fact_contributions', 'publications'}
                or any(part.observed_count or part.truncated for part in presence.sections)
            ):
                if presence.status in {'unavailable', 'missing'}:
                    raise QuestionnaireAdmissionUnavailable(
                        'Questionnaire source absence proof is temporarily unavailable'
                    )
                raise QuestionnaireSourceConflict('fresh Questionnaire history absence is not proven')
            auth = ProfileFreshSourceAuthorizationV1(
                scope=event.scope,
                source_type='questionnaire',
                source_id=event.source_id,
                authorization_id=identity,
                owner_fence_ref=ProfileSourceSnapshotRefV1(
                    provider='questionnaire', contract_version='profile-owner-admission-fence.v1', snapshot_id=fence_id
                ),
                owner_fence_digest=fence['proofDigest'],
                presence_proof_ref=presence.proof_ref,
                presence_proof_digest=presence.content_digest,
                material_contract_version='profile.questionnaire-source.v2',
                material_snapshot_ref=event.snapshot_ref,
                material_content_digest=event.content_digest,
                authorized_at=datetime.fromisoformat(material['binding']['createdAt']),
            )
            stored = await self.store.insert(
                _seal(
                    {
                        'implementationRevision': ADMISSION_REVISION,
                        '_id': identity,
                        'recordKind': 'admission',
                        'material': material,
                        'authorization': auth.model_dump(mode='json', by_alias=True),
                        'presenceProof': presence.model_dump(mode='json', by_alias=True),
                    }
                )
            )
        if stored.get('recordKind') != 'admission' or stored.get('material') != material:
            raise QuestionnaireSourceConflict('Questionnaire admission identity has a different material')
        _immutable(stored)
        auth = ProfileFreshSourceAuthorizationV1.model_validate(stored['authorization'])
        if (
            auth.scope != event.scope
            or auth.source_type != 'questionnaire'
            or auth.source_id != event.source_id
            or auth.material_snapshot_ref != event.snapshot_ref
            or auth.material_content_digest != event.content_digest
            or auth.material_contract_version != 'profile.questionnaire-source.v2'
            or auth.authorization_id != identity
        ):
            raise QuestionnaireSourceConflict('Questionnaire authorization no longer binds its frozen material')
        state = await self.fresh.reserve_fresh_source(auth, scope=event.scope)
        return state.model_dump(mode='json', by_alias=True)

    async def authorize(self, reservation):
        auth = ProfileFreshSourceAuthorizationV1.model_validate(reservation['authorization'])
        identity = 'questionnaire_admission_' + source_digest([auth.scope.user_id, auth.source_id])
        stored = await self.store.get(identity)
        if stored is None or stored.get('authorization') != auth.model_dump(mode='json', by_alias=True):
            raise QuestionnaireSourceConflict('Questionnaire authorization is not the frozen local intent')
        _immutable(stored)
        state = await self.fresh.authorize_fresh_source(auth, scope=auth.scope)
        return state.model_dump(mode='json', by_alias=True)


class QuestionnaireProfileSourceReceiver:
    def __init__(self, inbox):
        self.inbox = inbox

    async def receive(self, event):
        value = ProfileSourceEventV1.model_validate(deepcopy(event))
        receipt = await self.inbox.receive(value, scope=value.scope)
        return ProfileSourceReceiptV1.model_validate(receipt).model_dump(mode='json', by_alias=True)

    async def get_receipt(self, event):
        value = ProfileSourceEventV1.model_validate(deepcopy(event))
        receipt = await self.inbox.get_receipt(value.event_id, scope=value.scope)
        if receipt is None:
            raise LookupError('previously accepted Questionnaire source receipt is unavailable')
        return ProfileSourceReceiptV1.model_validate(receipt).model_dump(mode='json', by_alias=True)
