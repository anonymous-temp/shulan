"""Exact public Profile admission backed by Chat's independent immutable owner proof."""

from copy import deepcopy
from datetime import datetime

from adapters.chat.source_material import material_from_saved_session
from adapters.chat.source_material import validate_material
from adapters.chat.source_store import saved_binding
from modules.chat.profile_publication import ChatSourceConflict
from modules.chat.profile_publication import source_task_id
from modules.chat.profile_source import source_digest
from modules.health_profile.contracts import ProfileFreshSourceAuthorizationV1
from modules.health_profile.contracts import ProfileOwnerAdmissionFenceV1
from modules.health_profile.contracts import ProfileSourceEventV1
from modules.health_profile.contracts import ProfileSourceReceiptV1
from modules.health_profile.contracts import ProfileSourceSnapshotRefV1
from modules.health_profile.contracts import profile_content_digest

ADMISSION_REVISION = 'chat-source-admission.v1'


class ChatAdmissionUnavailable(RuntimeError):
    pass


def _seal(row):
    row['recordDigest'] = source_digest(row)
    return row


def _immutable(row):
    if row.get('implementationRevision') != ADMISSION_REVISION or row.get('recordDigest') != source_digest(
        {key: value for key, value in row.items() if key != 'recordDigest'}
    ):
        raise ChatSourceConflict('the fixed Chat admission record changed or is unsupported')
    return row


class ChatMaterialPreparer:
    def __init__(self, *, owner, materials):
        self.owner, self.materials = owner, materials

    async def prepare(self, binding, *, scope):
        draft = binding['draft']
        session = await self.owner.read_session(user_id=draft['userId'], message_id=draft['sourceId'])
        if saved_binding(session, draft['sourceId']) != binding:
            raise ChatSourceConflict('Chat message changed before immutable material preparation')
        value = material_from_saved_session(session, draft['sourceId'], scope=scope)
        return await self.materials.put_once(value)


class ChatOwnerFenceReader:
    """Historical proof reads are pure; held checks re-read only the fixed saved message.

    New-mode messages are born with their mode in the same business replace. The
    compatible legacy sender refuses that mode before enqueuing, independently of
    task/cursor existence. A session lease alone would not fence that sender.
    """

    def __init__(self, *, store, owner):
        self.store, self.owner = store, owner

    async def _read(self, scope, ref):
        if ref.provider != 'chat' or ref.contract_version != 'profile-owner-admission-fence.v1':
            raise ChatSourceConflict('unsupported Chat owner fence selection')
        row = await self.store.get(ref.snapshot_id)
        if row is None or row.get('recordKind') != 'owner_fence':
            raise LookupError('exact Chat admission fence is unavailable')
        _immutable(row)
        proof = ProfileOwnerAdmissionFenceV1.model_validate(row['proof'])
        material, event = validate_material(row['material'])
        if (
            proof.scope != scope
            or proof.source_type != 'chat'
            or proof.source_id != event.source_id
            or event.scope != scope
            or proof.prepared_manifest_ref != material['deliveryManifestDigest']
            or proof.fence_id != ref.snapshot_id
            or row['proofDigest'] != profile_content_digest(row['proof'])
        ):
            raise ChatSourceConflict('Chat owner proof has a conflicting immutable binding')
        return row

    async def read_admission_fence(self, *, scope, snapshot_ref):
        return deepcopy((await self._read(scope, snapshot_ref))['proof'])

    async def is_admission_fence_held(self, *, scope, snapshot_ref, content_digest):
        row = await self._read(scope, snapshot_ref)
        if row['proofDigest'] != content_digest:
            return False
        current = await self.owner.read_source(user_id=scope.user_id, message_id=row['proof']['sourceId'])
        return current == row['binding']


class ChatSourceAdmissionAdapter:
    """No caller-supplied quiescence. Freeze authorization before the first remote reserve."""

    def __init__(self, *, store, owner, inbox, presence, fresh_admission):
        self.store, self.owner = store, owner
        self.inbox, self.presence, self.fresh = inbox, presence, fresh_admission

    async def prepare_self_scope(self, user_id):
        return (await self.inbox.prepare_self_scope(user_id)).model_dump(mode='json', by_alias=True)

    @staticmethod
    def identity(event):
        return 'chat_admission_' + source_digest([event.scope.user_id, event.source_id])

    async def reserve(self, material):
        material, event = validate_material(material)
        identity = self.identity(event)
        stored = await self.store.get(identity)
        if stored is None:
            session = await self.owner.read_session(user_id=event.scope.user_id, message_id=event.source_id)
            binding = saved_binding(session, event.source_id)
            task = await self.store.get(source_task_id(event.scope.user_id, event.source_id))
            if (
                task is None
                or task.get('binding') != binding
                or task.get('scope') != event.scope.model_dump(by_alias=True)
                or task.get('material') != material
            ):
                raise ChatSourceConflict('Chat admission requires its own exact durable prepared task')
            if any(task.get(key) is not None for key in ('reservation', 'authorization', 'receipt')) or task.get(
                'dispatchAttempts', 0
            ):
                raise ChatSourceConflict('Chat already has formal delivery without this owner admission')
            if material_from_saved_session(session, event.source_id, scope=event.scope) != material:
                raise ChatSourceConflict('fresh admission requires the exact saved Chat message')
            fence_id = 'chat_fence_' + source_digest(
                [event.scope.model_dump(by_alias=True), event.source_id, material['deliveryManifestDigest']]
            )
            proof = ProfileOwnerAdmissionFenceV1(
                scope=event.scope,
                source_type='chat',
                source_id=event.source_id,
                fence_id=fence_id,
                fence_generation=1,
                prepared_manifest_ref=material['deliveryManifestDigest'],
                formal_history_absent=True,
                legacy_intents_absent=True,
                active_legacy_writers=0,
                active_legacy_workers=0,
                legacy_admission_blocked=True,
                issued_at=datetime.fromisoformat(material['createdAt']),
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
                raise ChatSourceConflict('Chat admission fence was already frozen for another source')
            # Inspect all historical Profile jobs/facts/publications across subjects,
            # not merely this delivery queue or unsettled legacy jobs.
            presence = await self.presence.read_legacy_presence(
                event.scope, source_type='chat', source_id=event.source_id
            )
            if (
                presence.scope != event.scope
                or presence.source_type != 'chat'
                or presence.source_id != event.source_id
                or presence.status != 'absent'
                or {part.kind for part in presence.sections} != {'jobs', 'fact_contributions', 'publications'}
                or any(part.observed_count or part.truncated for part in presence.sections)
            ):
                if presence.status in {'unavailable', 'missing'}:
                    raise ChatAdmissionUnavailable('Chat source absence proof is temporarily unavailable')
                raise ChatSourceConflict('fresh Chat history absence is not proven')
            auth = ProfileFreshSourceAuthorizationV1(
                scope=event.scope,
                source_type='chat',
                source_id=event.source_id,
                authorization_id=identity,
                owner_fence_ref=ProfileSourceSnapshotRefV1(
                    provider='chat', contract_version='profile-owner-admission-fence.v1', snapshot_id=fence_id
                ),
                owner_fence_digest=fence['proofDigest'],
                presence_proof_ref=presence.proof_ref,
                presence_proof_digest=presence.content_digest,
                material_contract_version='profile.chat-source.v2',
                material_snapshot_ref=event.snapshot_ref,
                material_content_digest=event.content_digest,
                authorized_at=datetime.fromisoformat(material['createdAt']),
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
            raise ChatSourceConflict('Chat admission identity has a different material')
        _immutable(stored)
        auth = ProfileFreshSourceAuthorizationV1.model_validate(stored['authorization'])
        if (
            auth.scope != event.scope
            or auth.source_type != 'chat'
            or auth.source_id != event.source_id
            or auth.material_snapshot_ref != event.snapshot_ref
            or auth.material_content_digest != event.content_digest
            or auth.material_contract_version != 'profile.chat-source.v2'
            or auth.authorization_id != identity
        ):
            raise ChatSourceConflict('Chat authorization no longer binds its frozen material')
        state = await self.fresh.reserve_fresh_source(auth, scope=event.scope)
        return state.model_dump(mode='json', by_alias=True)

    async def authorize(self, reservation):
        auth = ProfileFreshSourceAuthorizationV1.model_validate(reservation['authorization'])
        identity = 'chat_admission_' + source_digest([auth.scope.user_id, auth.source_id])
        stored = await self.store.get(identity)
        if stored is None or stored.get('authorization') != auth.model_dump(mode='json', by_alias=True):
            raise ChatSourceConflict('Chat authorization is not the frozen local intent')
        _immutable(stored)
        state = await self.fresh.authorize_fresh_source(auth, scope=auth.scope)
        return state.model_dump(mode='json', by_alias=True)


class ChatProfileSourceReceiver:
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
            raise LookupError('previously accepted Chat source receipt is unavailable')
        return ProfileSourceReceiptV1.model_validate(receipt).model_dump(mode='json', by_alias=True)
