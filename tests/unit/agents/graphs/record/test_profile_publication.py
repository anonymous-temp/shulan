"""Record durable publication uses exact business evidence, bounded leases and real DTOs."""

from copy import deepcopy
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bson import BSON

from adapters.record.completion import MongoRecordCompletionStore
from adapters.record.profile_source import PublicRecordSourceReceiver
from adapters.record.profile_source import RecordProfileSourceRouter
from adapters.record.profile_source import RecordSourceCompletionStore
from adapters.record.profile_source_store import MongoRecordCompletionEvidence
from adapters.record.profile_source_store import MongoRecordSourceStore
from modules.health_profile.contracts import ProfileRecordSourceSnapshotV2
from modules.health_profile.contracts import ProfileSourceEventV1
from modules.health_profile.contracts import ProfileSourceReceiptV1
from modules.record.profile_publication import RecordSourceConflict
from modules.record.profile_publication import RecordSourceLeaseLost
from modules.record.profile_publication import RecordSourcePublicationService
from modules.record.profile_publication import source_intent_id
from modules.record.profile_source import RECORD_PROFILE_SOURCE_REVISION
from modules.record.profile_source import RecordPublicationPolicy
from modules.record.profile_source import content_digest
from tests.unit.agents.graphs.record.test_profile_source_draft import session
from tests.unit.services.analysis.fakes import MemoryMongoCollection
from tests.unit.services.analysis.fakes import MemoryMongoCursor


class Cursor(MemoryMongoCursor):
    def limit(self, count):
        self.rows = self.rows[:count]
        return self


class Collection(MemoryMongoCollection):
    def matches(self, item, filters):
        remaining = {}
        for key, value in filters.items():
            if isinstance(value, dict) and '$nin' in value:
                if self.value(item, key) in value['$nin']:
                    return False
            else:
                remaining[key] = value
        return super().matches(item, remaining)

    async def insert_one(self, value):
        value = deepcopy(value)
        value.setdefault('_id', str(len(self.documents)))
        return await super().insert_one(BSON.encode(value).decode())

    async def replace_one(self, filters, replacement):
        for key, row in self.documents.items():
            if self.matches(row, filters):
                self.documents[key] = BSON.encode(replacement).decode()
                return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    def find(self, filters, projection=None):
        return Cursor([row for row in self.documents.values() if self.matches(row, filters)])


class Admission:
    def __init__(self):
        self.reservations = {}
        self.calls = []
        self.fail_reserve_ack = False
        self.unavailable = False

    async def prepare_self_scope(self, user_id, *, display_name):
        self.calls.append(('scope', display_name))
        return {'userId': user_id, 'subjectId': 'self'}

    async def reserve(self, *, material, snapshot_ref, completion_id, draft_id):
        self.calls.append(('reserve', completion_id, draft_id))
        if self.unavailable:
            raise RuntimeError('fresh verification unavailable')
        key = snapshot_ref['snapshotId']
        state = self.reservations.setdefault(
            key,
            {
                'status': 'blocked',
                'authorization': {
                    'admissionKind': 'fresh',
                    'scope': material['scope'],
                    'sourceType': 'record',
                    'sourceId': material['sourceId'],
                    'authorizationId': 'verified-fresh:' + key,
                    'materialContractVersion': 'profile.record-source.v2',
                    'materialSnapshotRef': deepcopy(snapshot_ref),
                    'materialContentDigest': content_digest(material),
                    'ownerFenceRef': {
                        'provider': 'record',
                        'contractVersion': 'test-owner-proof.v1',
                        'snapshotId': draft_id,
                    },
                },
            },
        )
        if self.fail_reserve_ack:
            self.fail_reserve_ack = False
            raise RuntimeError('lost blocked ACK')
        return deepcopy(state)

    async def authorize(self, reservation):
        self.calls.append(('authorize', reservation['authorization']['authorizationId']))
        return {**deepcopy(reservation), 'status': 'ready'}


class Receiver:
    def __init__(self):
        self.events, self.receive_calls, self.lookups = {}, [], []
        self.fail_ack = False
        self.status = 'received'

    def receipt(self, event):
        status = self.status
        return ProfileSourceReceiptV1.model_validate(
            {
                'contractVersion': 'profile-source-receipt.v1',
                'eventId': event['eventId'],
                'publicationId': 'public:' + event['eventId'],
                'scope': event['scope'],
                'sourceType': 'record',
                'sourceId': event['sourceId'],
                'publicationRevision': 1,
                'status': status,
                'receivedAt': datetime(2026, 11, 10, tzinfo=UTC),
                'updatedAt': datetime(2026, 11, 10, tzinfo=UTC),
                'applicationState': 'applied'
                if status == 'applied'
                else 'partial'
                if status == 'partial'
                else 'not_applied',
                'watermark': {
                    'scope': event['scope'],
                    'sourceType': 'record',
                    'sourceId': event['sourceId'],
                    'acceptedPublicationRevision': 1,
                    'appliedPublicationRevision': int(status == 'applied'),
                },
            }
        ).model_dump(mode='json', by_alias=True)

    async def receive(self, event):
        ProfileSourceEventV1.model_validate(event)
        self.receive_calls.append(deepcopy(event))
        old = self.events.setdefault(event['eventId'], deepcopy(event))
        assert old == event
        if self.fail_ack:
            self.fail_ack = False
            raise RuntimeError('lost accepted ACK')
        return self.receipt(event)

    async def get_receipt(self, event):
        self.lookups.append(deepcopy(event))
        return self.receipt(event)


def environment(policy=RecordPublicationPolicy()):
    intents, sessions = Collection(), Collection()
    now = [1000.0]
    store = MongoRecordSourceStore(intents)
    admission, receiver = Admission(), Receiver()
    service = RecordSourcePublicationService(
        store=store,
        admission=admission,
        receiver=receiver,
        completion_evidence=MongoRecordCompletionEvidence(sessions),
        policy=policy,
        clock=lambda: now[0],
    )
    completion = RecordSourceCompletionStore(MongoRecordCompletionStore(sessions), service)
    raw = session()
    raw['recordProfileConsumerRevision'] = RECORD_PROFILE_SOURCE_REVISION
    service.prepare_completed_snapshot(raw)
    raw['recordCompletionId'] = 'commit-1'
    return SimpleNamespace(
        intents=intents,
        sessions=sessions,
        store=store,
        admission=admission,
        receiver=receiver,
        service=service,
        completion=completion,
        raw=raw,
        now=now,
        intent_id=source_intent_id('user', 'session'),
    )


async def persisted(env):
    return await env.store.get(env.intent_id)


@pytest.mark.asyncio
async def test_completion_freezes_local_raw_before_business_commit_without_any_profile_calls():
    env = environment()
    assert await env.completion.publish_once('user', 'session', env.raw)
    row = await persisted(env)
    assert row['status'] == 'staged' and not row.get('reservation')
    assert not row.get('event') and not env.receiver.events and not env.admission.calls
    assert len(env.sessions.documents) == 1 and env.intents.indexes == []
    env.raw['qaHistory'].clear()
    await env.service.ensure_queued(user_id='user', session_id='session')
    assert not env.admission.calls and not env.receiver.events
    await env.service.recover_pending()
    row = await persisted(env)
    material = await env.service.read_source_snapshot(row['snapshotRef'], scope=row['scope'])
    assert len(material['qaHistory']) == 1
    assert ProfileRecordSourceSnapshotV2.model_validate(material).qa_history
    assert env.admission.calls[0] == ('scope', session()['extractedInfo']['name'])


@pytest.mark.asyncio
async def test_background_scan_publishes_without_any_get_and_received_is_not_applied():
    env = environment()
    await env.completion.publish_once('user', 'session', env.raw)
    assert await env.service.recover_pending() == 1
    row = await persisted(env)
    assert row['completionConfirmed'] and row['status'] == 'waiting_receipt'
    assert len(env.receiver.events) == len(env.receiver.receive_calls) == 1
    env.now[0] += 30
    env.receiver.status = 'applied'
    await env.service.recover_pending()
    assert (await persisted(env))['status'] == 'applied'
    assert len(env.receiver.receive_calls) == len(env.receiver.lookups) == 1
    assert await env.service.recover_pending() == 0


@pytest.mark.asyncio
async def test_failed_business_commit_never_publishes_and_observable_deadline_expires():
    env = environment(RecordPublicationPolicy(deadline_seconds=50))
    await env.service.stage_completion(env.raw)
    await env.service.recover_pending()
    assert (await persisted(env))['status'] == 'awaiting_completion'
    assert not env.receiver.events
    env.now[0] += 60
    await env.service.recover_pending()
    assert (await persisted(env))['errorCode'] == 'record_source_deadline_exceeded'
    assert not env.receiver.events


@pytest.mark.asyncio
async def test_lost_reservation_ack_keeps_completed_business_and_replays_same_frozen_material():
    env = environment()
    env.admission.fail_reserve_ack = True
    assert await env.completion.publish_once('user', 'session', env.raw)
    before = deepcopy(env.sessions.documents)
    assert not env.admission.calls
    await env.service.recover_pending()
    assert env.sessions.documents == before and not env.receiver.events
    first = await persisted(env)
    env.now[0] += 30
    await env.service.recover_pending()
    assert (await persisted(env))['snapshotRef'] == first['snapshotRef']
    assert len(env.admission.reservations) == 1
    assert sum(call[0] == 'scope' for call in env.admission.calls) == 1


@pytest.mark.asyncio
async def test_lost_receive_ack_reuses_fingerprint_without_rebuilding_material_or_polling_unproven_ack():
    env = environment()
    await env.completion.publish_once('user', 'session', env.raw)
    env.receiver.fail_ack = True
    await env.service.recover_pending()
    row = await persisted(env)
    assert row['status'] == 'retry_wait' and not row.get('receipt')
    env.now[0] += 30
    await env.service.recover_pending()
    assert len(env.receiver.receive_calls) == 2 and not env.receiver.lookups
    assert env.receiver.receive_calls[0] == env.receiver.receive_calls[1]
    assert len(env.receiver.events) == 1
    assert (await persisted(env))['snapshotRef'] == row['snapshotRef']


@pytest.mark.asyncio
async def test_restart_keeps_persisted_policy_and_total_attempts_until_explicit_redrive():
    env = environment(RecordPublicationPolicy(max_dispatches=1))
    await env.completion.publish_once('user', 'session', env.raw)
    env.receiver.fail_ack = True
    await env.service.recover_pending()
    env.service.policy = RecordPublicationPolicy(max_dispatches=9)
    env.now[0] += 30
    await env.service.recover_pending()
    row = await persisted(env)
    assert row['status'] == 'failed' and row['dispatchAttempts'] == 1
    await env.service.redrive_transport(
        env.intent_id, reason='operator verified receiver recovery', policy=RecordPublicationPolicy(max_dispatches=2)
    )
    await env.service.recover_pending()
    row = await persisted(env)
    assert row['dispatchAttempts'] == 2 and row['redrives'][0]['previousPolicy']['max_dispatches'] == 1
    assert row['status'] == 'waiting_receipt'


@pytest.mark.asyncio
async def test_expired_owner_cannot_publish_or_release_a_new_generation():
    env = environment()
    await env.service.stage_completion(env.raw)
    first = await env.store.claim(env.intent_id, now=env.now[0], owner='old')
    env.now[0] += 61
    second = await env.store.claim(env.intent_id, now=env.now[0], owner='new')
    assert second['leaseGeneration'] == first['leaseGeneration'] + 1
    with pytest.raises(RecordSourceLeaseLost):
        await env.service._save(first, {'status': 'applied'})
    await env.service._release(first)
    assert (await persisted(env))['leaseOwner'] == 'new'


@pytest.mark.asyncio
async def test_chat_takeover_keeps_completion_evidence_without_source_overwriting_chat():
    env = environment()
    await env.completion.publish_once('user', 'session', env.raw)
    saved = next(iter(env.sessions.documents.values()))
    env.sessions.documents[saved['_id']] = {
        '_id': saved['_id'],
        'userId': 'user',
        'sessionId': 'session',
        'type': 'chat',
        'status': 'active',
        'qaCompletedAt': saved['completedAt'],
        'qaProfileIngestion': deepcopy(saved['qaProfileIngestion']),
        'messages': [{'new': 'chat content'}],
    }
    before = deepcopy(env.sessions.documents)
    await env.service.recover_pending()
    assert len(env.receiver.events) == 1 and env.sessions.documents == before
    material = await env.service.read_source_snapshot(
        (await persisted(env))['snapshotRef'], scope={'userId': 'user', 'subjectId': 'self'}
    )
    assert material['qaHistory'] == session()['qaHistory']


@pytest.mark.asyncio
async def test_preexisting_completion_without_exact_marker_is_not_misreported_as_source_commit():
    env = environment()
    await env.service.stage_completion(env.raw)
    await env.sessions.insert_one(session())
    await env.service.recover_pending()
    assert (await persisted(env))['status'] == 'awaiting_completion'
    assert not env.receiver.events


@pytest.mark.asyncio
async def test_different_completion_cannot_replace_raw_source_identity():
    env = environment()
    await env.service.stage_completion(env.raw)
    changed = deepcopy(env.raw)
    changed['recordCompletionId'] = 'different'
    with pytest.raises(RecordSourceConflict):
        await env.service.stage_completion(changed)
    assert (await persisted(env))['completionId'] == 'commit-1'


@pytest.mark.asyncio
async def test_snapshot_read_is_detached_and_rejects_wrong_scope_and_tampered_bytes():
    env = environment()
    await env.completion.publish_once('user', 'session', env.raw)
    await env.service.recover_pending()
    row = await persisted(env)
    with pytest.raises(LookupError):
        await env.service.read_source_snapshot(row['snapshotRef'], scope={'userId': 'other', 'subjectId': 'self'})
    first = await env.service.read_source_snapshot(row['snapshotRef'], scope=row['scope'])
    first['qaHistory'].clear()
    assert (await env.service.read_source_snapshot(row['snapshotRef'], scope=row['scope']))['qaHistory']
    env.intents.documents[env.intent_id]['materialJson'] = '{}'
    with pytest.raises(RecordSourceConflict):
        await env.service.read_source_snapshot(row['snapshotRef'], scope=row['scope'])


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['partial', 'failed'])
async def test_application_failure_is_observable_but_does_not_rollback_completion(status):
    env = environment()
    await env.completion.publish_once('user', 'session', env.raw)
    original = deepcopy(env.sessions.documents)
    env.receiver.status = status
    await env.service.recover_pending()
    assert (await persisted(env))['status'] == status
    assert env.sessions.documents == original
    assert await env.service.recover_pending() == 0


@pytest.mark.asyncio
async def test_applied_receipt_cannot_lie_about_watermark():
    env = environment()
    await env.completion.publish_once('user', 'session', env.raw)
    original = env.receiver.receipt

    def invalid(event):
        value = original(event)
        value.update(status='applied', applicationState='applied')
        return value

    env.receiver.receipt = invalid
    await env.service.recover_pending()
    assert (await persisted(env))['status'] == 'failed'
    assert (await persisted(env))['errorCode'] == 'record_source_RecordSourceConflict'


@pytest.mark.asyncio
async def test_public_receiver_uses_only_public_commands_and_exact_scope():
    env = environment()
    await env.completion.publish_once('user', 'session', env.raw)
    await env.service.recover_pending()
    event = (await persisted(env))['event']
    receipt = ProfileSourceReceiptV1.model_validate(env.receiver.receipt(event))
    inbox = SimpleNamespace(receive=AsyncMock(return_value=receipt), get_receipt=AsyncMock(return_value=receipt))
    public = PublicRecordSourceReceiver(inbox)
    assert (await public.receive(event))['eventId'] == event['eventId']
    assert (await public.get_receipt(event))['eventId'] == event['eventId']
    assert inbox.receive.await_args.kwargs['scope'].user_id == 'user'
    assert inbox.get_receipt.await_args.args == (event['eventId'],)


@pytest.mark.asyncio
async def test_legacy_router_and_completion_stay_explicit_and_do_not_create_new_intents():
    env = environment()
    legacy = SimpleNamespace(
        prepare_completed_snapshot=lambda value: value.update(legacy=True), ensure_queued=AsyncMock()
    )
    router = RecordProfileSourceRouter(
        legacy=legacy, publications=env.service, default_revision=RECORD_PROFILE_SOURCE_REVISION
    )
    old = session()
    router.prepare_completed_snapshot(old)
    assert old['legacy'] and 'qaProfileIngestion' not in old
    await router.ensure_queued(user_id='user', session_id='session')
    legacy.ensure_queued.assert_awaited_once()
    assert not env.intents.documents
    old['recordProfileConsumerRevision'] = 'unknown-v99'
    with pytest.raises(RecordSourceConflict):
        router.prepare_completed_snapshot(old)


@pytest.mark.asyncio
async def test_public_profile_resolver_reads_the_exact_record_material_without_writes():
    from adapters.record.profile_source import PublicRecordSourceMaterialReader
    from modules.health_profile.application import ProfileUserSourceResolver

    env = environment()
    await env.completion.publish_once('user', 'session', env.raw)
    await env.service.recover_pending()
    row = await persisted(env)
    before = deepcopy(env.intents.documents)
    resolver = ProfileUserSourceResolver(PublicRecordSourceMaterialReader(env.service))
    resolved = await resolver.resolve(ProfileSourceEventV1.model_validate(row['event']))
    assert resolved.source_id == 'session'
    assert resolved.snapshot['qaHistory'] == session()['qaHistory']
    assert env.intents.documents == before


@pytest.mark.asyncio
async def test_creation_pins_source_revision_and_missing_old_acceptance_stays_legacy():
    from modules.record.profile_source import LEGACY_RECORD_PROFILE_REVISION
    from tests.unit.agents.graphs.record.test_creation_recovery import creation_manager

    env = environment()
    flow = creation_manager()
    legacy = SimpleNamespace(prepare_completed_snapshot=lambda value: None, ensure_queued=AsyncMock())
    flow.manager._record_profile_source = RecordProfileSourceRouter(
        legacy=legacy, publications=env.service, default_revision=RECORD_PROFILE_SOURCE_REVISION
    )
    flow.manager._completion_store = env.completion
    await flow.manager.createSession('user-1', 'session-1')
    active = await flow.manager._load_session('user-1', 'session-1')
    assert active['recordProfileConsumerRevision'] == RECORD_PROFILE_SOURCE_REVISION
    old = SimpleNamespace(session={'qaCreation': {}})
    assert flow.manager._selectRecordProfileConsumerRevision(old) == LEGACY_RECORD_PROFILE_REVISION
    assert not env.intents.documents and not env.admission.calls


@pytest.mark.asyncio
async def test_accepted_new_source_cannot_fall_back_to_old_provider_or_call_models_after_rollback():
    from tests.unit.agents.graphs.record.test_dynamic_record import setup_manager

    manager, module, gateway, _ = setup_manager()
    raw = session()
    raw['recordProfileConsumerRevision'] = RECORD_PROFILE_SOURCE_REVISION
    with pytest.raises(module.QASessionConflictError, match='画像来源版本'):
        async for _ in manager._generateTurn(raw, user_id='user'):
            pass
    assert not gateway.requests
    with pytest.raises(module.QASessionConflictError):
        manager._prepareRecordProfileSourceSnapshot(raw)
    assert 'qaProfileIngestion' not in raw


@pytest.mark.asyncio
async def test_mutating_original_during_first_store_await_cannot_change_frozen_draft():
    import asyncio

    env = environment()
    original = deepcopy(env.raw)
    entered, proceed = asyncio.Event(), asyncio.Event()
    insert = env.store.insert

    async def blocked(row):
        entered.set()
        await proceed.wait()
        return await insert(row)

    env.store.insert = blocked
    task = asyncio.create_task(env.service.stage_completion(env.raw))
    await entered.wait()
    env.raw['qaHistory'].clear()
    env.raw['recordCompletionId'] = 'mutated'
    proceed.set()
    row = await task
    from modules.record.profile_source import RecordSourceDraftV1

    assert row['draftId'] == RecordSourceDraftV1.capture(original).draft_id
    assert row['completionId'] == 'commit-1'


@pytest.mark.asyncio
async def test_lost_business_ack_after_chat_takeover_returns_existing_commit_without_overwrite():
    env = environment()
    original = env.completion.completions.publish_once

    async def lost(user_id, session_id, snapshot):
        await original(user_id, session_id, snapshot)
        raise RuntimeError('lost completion ACK')

    env.completion.completions.publish_once = lost
    with pytest.raises(RuntimeError, match='completion ACK'):
        await env.completion.publish_once('user', 'session', env.raw)
    saved = next(iter(env.sessions.documents.values()))
    env.sessions.documents[saved['_id']] = {
        '_id': saved['_id'],
        'userId': 'user',
        'sessionId': 'session',
        'type': 'chat',
        'status': 'active',
        'qaCompletedAt': saved['completedAt'],
        'qaProfileIngestion': deepcopy(saved['qaProfileIngestion']),
        'messages': [{'content': 'keep me'}],
    }
    before = deepcopy(env.sessions.documents)
    # The old commit path would conflict with the later Chat owner; exact proof
    # permits an idempotent acknowledgement without invoking that writer again.
    assert await env.completion.publish_once('user', 'session', env.raw)
    assert env.sessions.documents == before
    await env.service.recover_pending()
    assert len(env.receiver.events) == 1


@pytest.mark.asyncio
async def test_cancelled_dispatch_keeps_attempt_reservation_and_cannot_reset_budget_on_restart():
    import asyncio

    env = environment(RecordPublicationPolicy(max_dispatches=1))
    await env.completion.publish_once('user', 'session', env.raw)
    entered = asyncio.Event()

    async def pending(event):
        entered.set()
        await asyncio.Event().wait()

    env.receiver.receive = pending
    task = asyncio.create_task(env.service.recover_one(env.intent_id))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await persisted(env))['dispatchAttempts'] == 1
    await env.service.recover_pending()
    assert (await persisted(env))['errorCode'] == 'record_source_dispatch_exhausted'


@pytest.mark.asyncio
async def test_source_factory_is_pure_and_defaults_to_legacy_until_separately_enabled():
    from adapters.record.profile_source import create_record_profile_source_bundle
    from modules.record.profile_source import LEGACY_RECORD_PROFILE_REVISION

    env = environment()
    legacy = SimpleNamespace(prepare_completed_snapshot=lambda value: None, ensure_queued=AsyncMock())
    bundle = create_record_profile_source_bundle(
        legacy=legacy,
        completion_store=env.completion.completions,
        store=env.store,
        admission=env.admission,
        receiver=env.receiver,
        completion_evidence=env.service.completion_evidence,
    )
    assert bundle.source.consumer_revision == LEGACY_RECORD_PROFILE_REVISION
    assert not env.intents.documents and not env.sessions.documents and not env.admission.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'raw_name,display_name', [(123, None), ({'raw': 'name'}, None), ('  ', None), (' x ', 'x'), ('x' * 140, 'x' * 128)]
)
async def test_scope_name_hint_preserves_legacy_rules_without_rewriting_raw_evidence(raw_name, display_name):
    env = environment()
    raw = session()
    raw['recordProfileConsumerRevision'] = RECORD_PROFILE_SOURCE_REVISION
    raw['extractedInfo']['name'] = raw_name
    env.service.prepare_completed_snapshot(raw)
    raw['recordCompletionId'] = 'commit-1'
    await env.completion.publish_once('user', 'session', raw)
    assert not env.admission.calls
    await env.service.recover_pending()
    assert env.admission.calls[0] == ('scope', display_name)
    row = await persisted(env)
    material = await env.service.read_source_snapshot(row['snapshotRef'], scope=row['scope'])
    assert next(item['value'] for item in material['userInputs'] if item['field'] == 'name') == raw_name


@pytest.mark.asyncio
async def test_expired_source_never_blocks_business_completion_and_worker_stops_without_profile_calls():
    env = environment(RecordPublicationPolicy(deadline_seconds=10))
    await env.service.stage_completion(env.raw)
    env.now[0] += 11
    assert await env.completion.publish_once('user', 'session', env.raw)
    await env.service.ensure_queued(user_id='user', session_id='session')
    assert len(env.sessions.documents) == 1 and not env.admission.calls
    await env.service.recover_pending()
    row = await persisted(env)
    assert row['status'] == 'failed' and row['errorCode'] == 'record_source_deadline_exceeded'
    assert not env.receiver.events


@pytest.mark.asyncio
async def test_profile_unavailable_never_blocks_completion_and_background_recovery_keeps_failure_budget():
    env = environment(RecordPublicationPolicy(max_transport_failures=2))
    env.admission.unavailable = True
    assert await env.completion.publish_once('user', 'session', env.raw)
    await env.service.ensure_queued(user_id='user', session_id='session')
    before = deepcopy(env.sessions.documents)
    assert not env.admission.calls
    for count in (1, 2):
        await env.service.recover_pending()
        row = await persisted(env)
        assert row['transportFailures'] == count
        assert env.sessions.documents == before
        env.now[0] += 30
    assert row['status'] == 'failed'
    calls = deepcopy(env.admission.calls)
    env.service.policy = RecordPublicationPolicy(max_transport_failures=9)
    assert await env.completion.publish_once('user', 'session', env.raw)
    await env.service.ensure_queued(user_id='user', session_id='session')
    assert await env.service.recover_pending() == 0
    assert env.admission.calls == calls
    await env.service.redrive_transport(
        env.intent_id, reason='admission service restored', policy=RecordPublicationPolicy()
    )
    env.admission.unavailable = False
    await env.service.recover_pending()
    assert (await persisted(env))['transportFailures'] == 2
    assert (await persisted(env))['status'] == 'waiting_receipt'
    assert env.sessions.documents == before


@pytest.mark.asyncio
async def test_scope_returning_after_deadline_cannot_start_a_new_reservation_call():
    env = environment(RecordPublicationPolicy(deadline_seconds=10))
    original = env.admission.prepare_self_scope

    async def slow_scope(user_id, *, display_name):
        scope = await original(user_id, display_name=display_name)
        env.now[0] += 11
        return scope

    env.admission.prepare_self_scope = slow_scope
    assert await env.completion.publish_once('user', 'session', env.raw)
    assert not env.admission.calls
    await env.service.recover_pending()
    assert [call[0] for call in env.admission.calls] == ['scope']
    assert len(env.sessions.documents) == 1 and not env.receiver.events
    row = await persisted(env)
    assert row['status'] == 'failed' and row['errorCode'] == 'record_source_deadline_exceeded'


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_http_and_sse_complete_before_any_profile_call_then_independent_worker_recovers(streaming):
    from modules.record.application.recovery import ReplyRecoveryCoordinator
    from tests.unit.agents.graphs.record.test_background_reply_recovery import RecoveryStore
    from tests.unit.agents.graphs.record.test_dynamic_record import assessment
    from tests.unit.agents.graphs.record.test_dynamic_record import concern
    from tests.unit.agents.graphs.record.test_dynamic_record import setup_manager
    from tests.unit.agents.graphs.record.test_dynamic_record import submit

    env = environment()
    manager, module, gateway, _ = setup_manager()
    manager._record_profile_source = RecordProfileSourceRouter(
        legacy=SimpleNamespace(prepare_completed_snapshot=lambda value: None, ensure_queued=AsyncMock()),
        publications=env.service,
        default_revision=RECORD_PROFILE_SOURCE_REVISION,
    )
    manager._completion_store = env.completion
    manager._reply_recovery_store = RecoveryStore(module)
    manager._reply_recovery = ReplyRecoveryCoordinator(manager, manager._reply_recovery_store)
    original_scope = env.admission.prepare_self_scope
    unavailable_scope = AsyncMock(side_effect=RuntimeError('Profile wholly unavailable'))
    env.admission.prepare_self_scope = unavailable_scope
    env.admission.unavailable = True
    current = await manager.createSession('u', 's')
    gateway.next_assessment = assessment(intent='stop', concerns=[concern('腹胀', ready=False)])
    final = await submit(manager, current, '请结束，但腹胀如何处理', streaming=streaming)
    assert final['status'] == 'finished'
    assert len(env.sessions.documents) == 1
    completed = next(iter(env.sessions.documents.values()))
    assert completed['status'] == 'completed'
    unavailable_scope.assert_not_awaited()
    assert not env.admission.calls and not env.receiver.events and not env.receiver.lookups
    calls = len(gateway.requests)
    intent_id = source_intent_id('u', 's')
    assert (await env.store.get(intent_id))['status'] == 'staged'
    await env.service.recover_pending()
    unavailable_scope.assert_awaited_once()
    assert (await env.store.get(intent_id))['status'] == 'retry_wait'
    env.admission.prepare_self_scope = original_scope
    env.admission.unavailable = False
    env.now[0] += 30
    await env.service.recover_pending()
    assert (await env.store.get(intent_id))['status'] == 'waiting_receipt'
    assert len(env.receiver.events) == 1 and len(gateway.requests) == calls
    assert next(iter(env.sessions.documents.values())) == completed
