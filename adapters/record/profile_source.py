"""Record completion bridge and public Profile receiver, without automatic cutover."""

from copy import deepcopy

from modules.health_profile.contracts import ProfileScope
from modules.health_profile.contracts import ProfileSourceEventV1
from modules.health_profile.contracts import ProfileSourceSnapshotRefV1
from modules.record.profile_publication import RecordSourceConflict
from modules.record.profile_publication import RecordSourceMaterialReader
from modules.record.profile_source import LEGACY_RECORD_PROFILE_REVISION
from modules.record.profile_source import RECORD_PROFILE_SOURCE_REVISION


class RecordSourceCompletionStore:
    """Persist only local evidence before business insert; Profile runs independently."""

    supports_record_profile_sources = True

    def __init__(self, completion_store, publications):
        self.completions, self.publications = completion_store, publications

    async def publish_once(self, user_id, session_id, session):
        value = deepcopy(session)
        revision = value.get('recordProfileConsumerRevision', LEGACY_RECORD_PROFILE_REVISION)
        if revision == LEGACY_RECORD_PROFILE_REVISION:
            marker = value.get('qaProfileIngestion', {})
            if marker.get('deliveryMode') not in (None, 'legacy') or marker.get('recordSourceIntent'):
                raise RecordSourceConflict('new source evidence cannot fall back to legacy completion')
            return await self.completions.publish_once(user_id, session_id, value)
        if revision != RECORD_PROFILE_SOURCE_REVISION:
            raise RecordSourceConflict('the accepted Record source consumer is unavailable')
        if value.get('userId') != user_id or value.get('sessionId') != session_id:
            raise RecordSourceConflict('Record completion owner does not match its source')
        row = await self.publications.stage_completion(value)
        if await self.publications.completion_evidence.is_committed(
            user_id=user_id, session_id=session_id, completion_id=row['completionId'], draft_id=row['draftId']
        ):
            return True
        value['qaProfileIngestion']['recordSourceIntent']['completionId'] = row['completionId']
        # No post-commit network operation can turn completed QA into a failure.
        return await self.completions.publish_once(user_id, session_id, value)


class RecordProfileSourceRouter:
    """Pin new acceptance explicitly; missing historical selections always stay legacy."""

    def __init__(self, *, legacy, publications, default_revision=LEGACY_RECORD_PROFILE_REVISION):
        if default_revision not in {LEGACY_RECORD_PROFILE_REVISION, RECORD_PROFILE_SOURCE_REVISION}:
            raise ValueError('unsupported Record source consumer default')
        self.legacy, self.publications = legacy, publications
        self.consumer_revision = default_revision

    def supports_revision(self, revision):
        return revision in {LEGACY_RECORD_PROFILE_REVISION, RECORD_PROFILE_SOURCE_REVISION}

    def prepare_completed_snapshot(self, session):
        revision = session.get('recordProfileConsumerRevision', LEGACY_RECORD_PROFILE_REVISION)
        if revision == RECORD_PROFILE_SOURCE_REVISION:
            self.publications.prepare_completed_snapshot(session)
        elif revision == LEGACY_RECORD_PROFILE_REVISION:
            if session.get('qaProfileIngestion', {}).get('deliveryMode') not in (None, 'legacy'):
                raise RecordSourceConflict('new source delivery cannot be reinterpreted as legacy')
            self.legacy.prepare_completed_snapshot(session)
        else:
            raise RecordSourceConflict('the accepted Record source consumer is unavailable')

    async def ensure_queued(self, *, user_id, session_id):
        from modules.record.profile_publication import source_intent_id

        # New staged intents are authoritative even when completion ACK was lost.
        row = await self.publications.store.get(source_intent_id(user_id, session_id))
        if row is not None:
            return await self.publications.ensure_queued(user_id=user_id, session_id=session_id)
        return await self.legacy.ensure_queued(user_id=user_id, session_id=session_id)


class PublicRecordSourceReceiver:
    def __init__(self, inbox):
        self.inbox = inbox

    async def receive(self, event):
        value = ProfileSourceEventV1.model_validate(deepcopy(event))
        receipt = await self.inbox.receive(value, scope=value.scope)
        return receipt.model_dump(mode='json', by_alias=True)

    async def get_receipt(self, event):
        value = ProfileSourceEventV1.model_validate(deepcopy(event))
        receipt = await self.inbox.get_receipt(value.event_id, scope=ProfileScope.model_validate(value.scope))
        if receipt is None:
            raise RecordSourceConflict('acknowledged Profile publication is no longer available')
        return receipt.model_dump(mode='json', by_alias=True)


class PublicRecordSourceMaterialReader:
    """Adapt only public immutable identifiers to the Record-owned pure reader."""

    def __init__(self, publications):
        self.publications = publications

    async def read_source_snapshot(self, snapshot_ref, *, scope):
        ref = ProfileSourceSnapshotRefV1.model_validate(snapshot_ref)
        owned = ProfileScope.model_validate(scope)
        return await self.publications.read_source_snapshot(
            ref.model_dump(mode='json', by_alias=True), scope=owned.model_dump(mode='json', by_alias=True)
        )


def create_record_source_material_reader(*, store):
    """Build Profile's immutable reader before admission/receiver/bundle assembly.

    Reuse the exact durable Record source store that the later worker will use.
    Construction performs no I/O and requires only its find_snapshot method.
    """
    if store is None:
        raise ValueError('an explicit Record source store is required')
    return PublicRecordSourceMaterialReader(RecordSourceMaterialReader(store))


def create_record_profile_source_bundle(
    *,
    legacy,
    completion_store,
    store,
    admission,
    receiver,
    completion_evidence,
    default_revision=LEGACY_RECORD_PROFILE_REVISION,
    policy=None,
    clock=None,
):
    """Pure explicit assembly; callers must provide independently verified fresh admission.

    The returned worker is not started here. Installing it or selecting the new
    default is a separate composition change after provider and owner-fence gates.
    """
    from types import SimpleNamespace

    from modules.record.profile_publication import RecordSourcePublicationService

    options = {}
    if policy is not None:
        options['policy'] = policy
    if clock is not None:
        options['clock'] = clock
    if any(value is None for value in (legacy, completion_store, store, admission, receiver, completion_evidence)):
        raise ValueError('Record source assembly requires every explicit delivery dependency')
    publications = RecordSourcePublicationService(
        store=store, admission=admission, receiver=receiver, completion_evidence=completion_evidence, **options
    )
    return SimpleNamespace(
        source=RecordProfileSourceRouter(legacy=legacy, publications=publications, default_revision=default_revision),
        completion_store=RecordSourceCompletionStore(completion_store, publications),
        material_reader=create_record_source_material_reader(store=store),
        worker=publications,
    )
