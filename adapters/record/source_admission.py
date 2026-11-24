"""Actual public Profile admission with a Record-owned, permanent new-mode fence.

Only independently completed, first-publication Record sources qualify. These
adapters never migrate an existing legacy source or enable production defaults.
"""

from copy import deepcopy
from datetime import UTC
from datetime import datetime
from uuid import uuid4

from bson import json_util
from pymongo import ReadPreference

from modules.health_profile.contracts import ProfileFreshSourceAuthorizationV1
from modules.health_profile.contracts import ProfileFreshSourceStateV1
from modules.health_profile.contracts import ProfileOwnerAdmissionFenceV1
from modules.health_profile.contracts import ProfileRecordSourceSnapshotV2
from modules.health_profile.contracts import ProfileScope
from modules.health_profile.contracts import ProfileSourcePresenceV1
from modules.health_profile.contracts import ProfileSourceSnapshotRefV1
from modules.record.profile_publication import RecordSourceConflict
from modules.record.profile_publication import source_intent_id
from modules.record.profile_source import RECORD_PROFILE_SOURCE_REVISION
from modules.record.profile_source import RecordSourceDraftV1
from modules.record.profile_source import content_digest
from modules.record.source_admission import RECORD_SOURCE_ADMISSION_REVISION
from modules.record.source_admission import fixed_request
from modules.record.source_admission import request_identity
from modules.record.source_admission import verify_completion
from modules.record.source_admission import verify_intent
from utils.identifiers import create_session_lease_id


class RecordAdmissionUnavailable(RuntimeError):
    pass


class MongoRecordFreshCompletionReader:
    """Read complete raw evidence under the existing shared QA/Chat session lease."""

    def __init__(self, *, collection, source_store, session_cache, lease_manager):
        self.collection = collection.with_options(read_preference=ReadPreference.PRIMARY)
        self.sources, self.cache, self.leases = source_store, session_cache, lease_manager

    async def is_committed(self, *, user_id, session_id, completion_id, draft_id):
        # Also usable inside the business request's already-held session lease.
        # This method is pure and never acquires a second lease or contacts Profile.
        rows = await self.collection.find({'userId': user_id, 'sessionId': session_id}).limit(2).to_list(2)
        if not rows:
            return False
        if len(rows) != 1:
            raise RecordSourceConflict('Record completion identity is ambiguous')
        identity = source_intent_id(user_id, session_id)
        intent = await self.sources.get(identity)
        if not isinstance(intent, dict):
            raise RecordSourceConflict('the completed Record source intent is missing')
        draft = RecordSourceDraftV1(intent['draftJson'])
        if (
            intent.get('_id') != identity
            or intent.get('consumerRevision') != RECORD_PROFILE_SOURCE_REVISION
            or intent.get('userId') != user_id
            or intent.get('sessionId') != session_id
            or intent.get('completionId') != completion_id
            or intent.get('draftId') != draft_id
            or draft.user_id != user_id
            or draft.source_id != session_id
            or draft.draft_id != draft_id
        ):
            raise RecordSourceConflict('completed Record source identity differs from its immutable intent')
        verify_completion(rows[0], intent)
        return True

    async def verify(self, request, *, initial=False):
        request = deepcopy(request)
        material = request['material']
        user, session = material['scope']['userId'], material['sourceId']
        lease = await self.leases.try_acquire(create_session_lease_id(user, session), uuid4().hex)
        if lease is None:
            raise RecordAdmissionUnavailable('record_admission_session_busy')
        try:
            intent = await self.sources.get(request_identity(request))
            verify_intent(intent, request)
            if initial and (
                any(intent.get(key) is not None for key in ('event', 'reservation', 'authorization', 'receipt'))
                or intent.get('dispatchAttempts', 0) != 0
            ):
                raise RecordSourceConflict('Record already has formal delivery history without this owner fence')
            rows = await self.collection.find({'userId': user, 'sessionId': session}).limit(2).to_list(2)
            if len(rows) != 1:
                raise RecordSourceConflict('fresh admission requires one durable completed Record session')
            evidence = verify_completion(rows[0], intent)
            cached = await self.cache.get(user, session)
            if cached is not None:
                if cached.get('userId') != user or cached.get('sessionId') != session:
                    raise RecordSourceConflict('active Record cache belongs to another identity')
                commit = cached.get('qaCommit')
                if commit is not None:
                    if (
                        cached.get('recordProfileConsumerRevision') != RECORD_PROFILE_SOURCE_REVISION
                        or commit.get('version') != 'record.completion.v1'
                        or commit.get('commitId') != intent['completionId']
                    ):
                        raise RecordSourceConflict('an unknown or legacy Record commit remains active')
                    verify_completion(json_util.loads(commit['snapshotJson']), intent, pending_commit=True)
                else:
                    verify_completion(cached, intent)
            if await self.leases.renew(lease) is not True:
                raise RecordAdmissionUnavailable('record_admission_session_lease_lost')
            return evidence
        finally:
            await self.leases.release(lease)


class RecordSourceAdmissionOwner:
    """Persist one proof/auth per completed new-mode source; never silently unlock it."""

    def __init__(self, *, store, evidence, clock=None):
        self.store, self.evidence = store, evidence
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _validate(row, request=None):
        if row is None or row.get('implementationRevision') != RECORD_SOURCE_ADMISSION_REVISION:
            raise RecordSourceConflict('the fixed Record admission version is unavailable')
        if row.get('recordDigest') != content_digest(
            {key: value for key, value in row.items() if key != 'recordDigest'}
        ):
            raise RecordSourceConflict('immutable Record admission record was changed')
        fixed = fixed_request(
            **{
                'material': row['request']['material'],
                'snapshot_ref': row['request']['snapshotRef'],
                'completion_id': row['request']['completionId'],
                'draft_id': row['request']['draftId'],
            }
        )
        if (
            content_digest(fixed) != row['requestDigest']
            or row['_id'] != request_identity(fixed)
            or (request is not None and fixed != request)
        ):
            raise RecordSourceConflict('Record admission changed its fixed request')
        proof = ProfileOwnerAdmissionFenceV1.model_validate(row['proof'])
        auth = ProfileFreshSourceAuthorizationV1.model_validate(row['authorization'])
        expected_ref = {
            'provider': 'record',
            'contractVersion': 'profile-owner-admission-fence.v1',
            'snapshotId': row['_id'],
        }
        if (
            proof.scope.model_dump(by_alias=True) != fixed['material']['scope']
            or proof.source_type != 'record'
            or proof.source_id != fixed['material']['sourceId']
            or proof.fence_id != row['_id']
            or proof.fence_generation != 1
            or proof.prepared_manifest_ref != fixed['draftId']
            or auth.owner_fence_ref.model_dump(by_alias=True) != expected_ref
            or auth.owner_fence_digest != content_digest(row['proof'])
            or auth.scope != proof.scope
            or auth.source_type != 'record'
            or auth.source_id != proof.source_id
            or auth.material_snapshot_ref.model_dump(by_alias=True) != fixed['snapshotRef']
            or auth.material_content_digest != content_digest(fixed['material'])
            or auth.material_contract_version != 'profile.record-source.v2'
        ):
            raise RecordSourceConflict('Record owner proof does not bind the immutable admission')
        return deepcopy(row)

    async def existing(self, request):
        row = await self.store.get(request_identity(request))
        return self._validate(row, request) if row is not None else None

    async def prepare(self, request, presence):
        request = deepcopy(request)
        evidence = await self.evidence.verify(request, initial=True)
        now = self.clock()
        identity = request_identity(request)
        scope, source = request['material']['scope'], request['material']['sourceId']
        proof = ProfileOwnerAdmissionFenceV1(
            scope=scope,
            source_type='record',
            source_id=source,
            fence_id=identity,
            fence_generation=1,
            prepared_manifest_ref=request['draftId'],
            formal_history_absent=True,
            legacy_intents_absent=True,
            active_legacy_writers=0,
            active_legacy_workers=0,
            legacy_admission_blocked=True,
            issued_at=now,
        ).model_dump(mode='json', by_alias=True)
        authorization = ProfileFreshSourceAuthorizationV1(
            scope=scope,
            source_type='record',
            source_id=source,
            authorization_id='record_fresh_' + content_digest(request),
            owner_fence_ref={
                'provider': 'record',
                'contractVersion': 'profile-owner-admission-fence.v1',
                'snapshotId': identity,
            },
            owner_fence_digest=content_digest(proof),
            presence_proof_ref=presence.proof_ref,
            presence_proof_digest=presence.content_digest,
            material_contract_version='profile.record-source.v2',
            material_snapshot_ref=request['snapshotRef'],
            material_content_digest=content_digest(request['material']),
            authorized_at=now,
        ).model_dump(mode='json', by_alias=True)
        record = {
            '_id': identity,
            'implementationRevision': RECORD_SOURCE_ADMISSION_REVISION,
            'request': request,
            'requestDigest': content_digest(request),
            'evidence': evidence,
            'proof': proof,
            'authorization': authorization,
        }
        record['recordDigest'] = content_digest(record)
        stored = await self.store.put(record)
        return self._validate(stored, request)

    async def read_admission_fence(self, *, scope, snapshot_ref):
        owned, ref = ProfileScope.model_validate(scope), ProfileSourceSnapshotRefV1.model_validate(snapshot_ref)
        if ref.provider != 'record' or ref.contract_version != 'profile-owner-admission-fence.v1':
            raise RecordSourceConflict('unsupported Record owner proof reference')
        row = self._validate(await self.store.get(ref.snapshot_id))
        if row['proof']['scope'] != owned.model_dump(by_alias=True):
            raise PermissionError('Record admission proof belongs to another scope')
        return deepcopy(row['proof'])

    async def is_admission_fence_held(self, *, scope, snapshot_ref, content_digest):
        proof = await self.read_admission_fence(scope=scope, snapshot_ref=snapshot_ref)
        from modules.record.profile_source import content_digest as digest

        if digest(proof) != content_digest:
            return False
        ref = ProfileSourceSnapshotRefV1.model_validate(snapshot_ref)
        row = self._validate(await self.store.get(ref.snapshot_id))
        # The durable new-mode completion blocks all supported legacy admissions;
        # rechecking raw evidence also catches deleted/corrupt/unknown old owners.
        evidence = await self.evidence.verify(row['request'])
        return evidence == row['evidence']


class PublicRecordSourceAdmission:
    def __init__(self, *, owner, inbox, presence, admission):
        self.owner, self.inbox, self.presence, self.admission = owner, inbox, presence, admission

    async def prepare_self_scope(self, user_id, *, display_name):
        scope = await self.inbox.prepare_self_scope(user_id, display_name=display_name)
        return ProfileScope.model_validate(scope).model_dump(mode='json', by_alias=True)

    async def reserve(self, *, material, snapshot_ref, completion_id, draft_id):
        request = fixed_request(
            material=material, snapshot_ref=snapshot_ref, completion_id=completion_id, draft_id=draft_id
        )
        ProfileRecordSourceSnapshotV2.model_validate(request['material'])
        scope = ProfileScope.model_validate(request['material']['scope'])
        row = await self.owner.existing(request)
        if row is None:
            presence = ProfileSourcePresenceV1.model_validate(
                await self.presence.read_legacy_presence(
                    scope,
                    source_type='record',
                    source_id=request['material']['sourceId'],
                    limit=1000,
                )
            )
            if (
                presence.scope != scope
                or presence.source_type != 'record'
                or presence.source_id != request['material']['sourceId']
                or presence.status != 'absent'
                or {x.kind for x in presence.sections} != {'jobs', 'fact_contributions', 'publications'}
                or any(x.observed_count or x.truncated for x in presence.sections)
                or not presence.proof_ref
                or not presence.content_digest
            ):
                raise RecordAdmissionUnavailable('record_admission_legacy_absence_unproven')
            row = await self.owner.prepare(request, presence)
        authorization = ProfileFreshSourceAuthorizationV1.model_validate(row['authorization'])
        state = await self.admission.reserve_fresh_source(authorization, scope=scope)
        return self._state(state, row)

    async def authorize(self, reservation):
        frozen = ProfileFreshSourceStateV1.model_validate(deepcopy(reservation))
        request_id = 'record_source_' + content_digest(
            {
                'userId': frozen.authorization.scope.user_id,
                'sourceId': frozen.authorization.source_id,
            }
        )
        row = self.owner._validate(await self.owner.store.get(request_id))
        if frozen.authorization.model_dump(mode='json', by_alias=True) != row['authorization']:
            raise RecordSourceConflict('Record authorization was not issued by this durable owner')
        state = await self.admission.authorize_fresh_source(frozen.authorization, scope=frozen.authorization.scope)
        value = self._state(state, row)
        if value['status'] != 'ready':
            raise RecordAdmissionUnavailable('record_admission_not_ready')
        return value

    @staticmethod
    def _state(state, row):
        state = ProfileFreshSourceStateV1.model_validate(state)
        value = state.model_dump(mode='json', by_alias=True)
        if value['authorization'] != row['authorization']:
            raise RecordSourceConflict('Profile changed the fixed Record authorization')
        return value


class UnavailableRecordSourceAdmission:
    """Keep the completion bridge usable while only background delivery is unavailable."""

    async def prepare_self_scope(self, user_id, *, display_name):
        raise RecordAdmissionUnavailable('record_admission_configuration_unavailable')

    async def reserve(self, **kwargs):
        raise RecordAdmissionUnavailable('record_admission_configuration_unavailable')

    async def authorize(self, reservation):
        raise RecordAdmissionUnavailable('record_admission_configuration_unavailable')


def create_record_source_admission_owner(
    *, admission_collection, completion_collection, source_store, session_cache, lease_manager, clock=None
):
    """Pure construction using the same completed sessions, active cache and business lease."""
    from adapters.record.source_admission_store import MongoRecordAdmissionStore

    return RecordSourceAdmissionOwner(
        store=MongoRecordAdmissionStore(admission_collection),
        evidence=MongoRecordFreshCompletionReader(
            collection=completion_collection,
            source_store=source_store,
            session_cache=session_cache,
            lease_manager=lease_manager,
        ),
        clock=clock,
    )


def create_public_record_source_admission(
    *, owner, inbox, presence, admission, implementation_revision=RECORD_SOURCE_ADMISSION_REVISION
):
    """Unknown/missing external capability affects the background adapter, not business commit."""
    if implementation_revision != RECORD_SOURCE_ADMISSION_REVISION or any(
        value is None for value in (owner, inbox, presence, admission)
    ):
        return UnavailableRecordSourceAdmission()
    return PublicRecordSourceAdmission(owner=owner, inbox=inbox, presence=presence, admission=admission)
