"""Actual public Profile admission plus Record durable evidence, using offline stores."""

import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from types import MethodType
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.record.profile_source import PublicRecordSourceMaterialReader
from adapters.record.profile_source import PublicRecordSourceReceiver
from adapters.record.source_admission import create_public_record_source_admission
from adapters.record.source_admission import create_record_source_admission_owner
from health_profile.ingestion import HealthFactPolicy
from health_profile.models import FactStatus
from modules.health_profile.application import DurableProfileSourceExtractor
from modules.health_profile.application import MongoProfileSourcePresenceReader
from modules.health_profile.application import ProfileFreshSourceAdmission
from modules.health_profile.application import ProfileUserSourceResolver
from modules.health_profile.contracts import ProfileScope
from modules.health_profile.contracts import ProfileSourceSnapshotRefV1
from modules.record.profile_publication import RecordSourceConflict
from modules.record.profile_publication import source_intent_id
from modules.record.profile_source import RECORD_PROFILE_SOURCE_REVISION
from modules.record.source_admission import fixed_request
from tests.unit.agents.graphs.record.test_profile_publication import Collection
from tests.unit.agents.graphs.record.test_profile_publication import environment
from tests.unit.health_profile.test_public_contracts import setup_flow
from tests.unit.health_profile.test_record_ingestion import _runtime
from tests.unit.health_profile.test_source_admission import Collection as PresenceCollection


class Leases:
    def __init__(self):
        self.held = {}
        self.lost = False

    async def try_acquire(self, key, owner):
        if key in self.held:
            return None
        lease = SimpleNamespace(key=key, owner=owner)
        self.held[key] = lease
        return lease

    async def renew(self, lease):
        return not self.lost and self.held.get(lease.key) is lease

    async def release(self, lease):
        if self.held.get(lease.key) is lease:
            del self.held[lease.key]
            return True
        return False


def options(collection):
    collection.options = []

    def with_options(current, **kwargs):
        current.options.append(kwargs)
        return current

    collection.with_options = MethodType(with_options, collection)
    return collection


async def composition():
    env = environment()
    profile = await setup_flow(authorize=False)
    env.raw['userId'] = 'u'
    env.raw.pop('qaProfileIngestion')
    env.raw['qaHistory'] = [{'question': {'questionId': 'q0', 'question': '近况'}, 'answer': '我最近总是头痛'}]
    env.raw['extractedInfo'] = {'age': 42}
    env.raw['extractedInfoSources'] = {'age': 'user_input'}
    env.raw['healthData'] = {'hospitalData': [], 'weRunData': [], 'smartDeviceData': {}}
    env.service.prepare_completed_snapshot(env.raw)
    env.intent_id = source_intent_id('u', 'session')
    env.cache = SimpleNamespace(get=AsyncMock(return_value=None))
    env.leases = Leases()
    env.admission_records = options(Collection())
    options(env.sessions)
    env.owner = create_record_source_admission_owner(
        admission_collection=env.admission_records,
        completion_collection=env.sessions,
        source_store=env.store,
        session_cache=env.cache,
        lease_manager=env.leases,
        clock=lambda: profile.now[0],
    )
    env.service.completion_evidence = env.owner.evidence
    collections = {
        name: PresenceCollection() for name in ('subjects', 'reports', 'revisions', 'jobs', 'facts', 'publications')
    }
    for name in ('subjects', 'jobs', 'facts', 'publications'):
        collections[name].documents = profile.collections[name].documents
    env.presence_collections = collections
    env.presence = MongoProfileSourcePresenceReader(**collections, clock=lambda: profile.now[0])
    env.fresh = ProfileFreshSourceAdmission(
        inbox=profile.inbox, presence_reader=env.presence, owner_fence_readers={'record': env.owner}
    )
    env.service.admission = create_public_record_source_admission(
        owner=env.owner, inbox=profile.inbox, presence=env.presence, admission=env.fresh
    )
    env.service.receiver = PublicRecordSourceReceiver(profile.inbox)
    resolver = ProfileUserSourceResolver(PublicRecordSourceMaterialReader(env.service))
    profile.applier.resolvers[('record', 'profile.record-source.v2')] = resolver
    runtime, gateway = _runtime(
        {
            'candidates': [
                {
                    'answerIndex': 0,
                    'factType': 'symptom.reported',
                    'displayName': '头痛',
                    'valueType': 'text',
                    'value': '反复头痛',
                    'subjectKind': 'self',
                    'subjectHint': '本人',
                    'evidenceText': '我最近总是头痛',
                    'confidence': 0.96,
                }
            ]
        }
    )
    profile.applier.extractor = DurableProfileSourceExtractor(runtime.hub, policy_version=HealthFactPolicy.VERSION)
    env.profile, env.runtime, env.gateway = profile, runtime, gateway
    return env


async def complete(env):
    assert await env.completion.publish_once('u', 'session', env.raw)
    return next(iter(env.sessions.documents.values()))


async def row(env):
    return await env.store.get(env.intent_id)


async def retry(env):
    env.now[0] += 30
    env.profile.now[0] += timedelta(seconds=30)
    await env.service.recover_pending()
    return await row(env)


def request_from(row):
    return fixed_request(
        material=json.loads(row['materialJson']),
        snapshot_ref=row['snapshotRef'],
        completion_id=row['completionId'],
        draft_id=row['draftId'],
    )


@pytest.mark.asyncio
async def test_real_public_admission_starts_only_after_business_completion_and_applies_once(monkeypatch):
    env = await composition()
    scope = AsyncMock(wraps=env.profile.inbox.prepare_self_scope)
    monkeypatch.setattr(env.profile.inbox, 'prepare_self_scope', scope)
    stored = await complete(env)
    await env.service.ensure_queued(user_id='u', session_id='session')
    assert stored['status'] == 'completed' and scope.await_count == 0
    assert not env.admission_records.documents and not env.profile.collections['publications'].documents
    assert await env.service.recover_pending() == 1
    current = await row(env)
    assert current['status'] == 'waiting_receipt' and current['authorization']['status'] == 'ready'
    assert len(env.admission_records.documents) == 1
    assert not env.profile.collections['jobs'].documents
    assert await env.profile.worker.recover_pending() == 1
    assert (await retry(env))['status'] == 'applied'
    facts = await env.profile.repository.list_facts(
        'u', env.profile.scope.subject_id, statuses=(FactStatus.ACTIVE, FactStatus.PENDING)
    )
    assert len(facts) == 2 and len(env.gateway.complete_requests) == 1
    assert await env.service.recover_pending() == 0
    assert len(env.gateway.complete_requests) == 1
    assert env.admission_records.indexes == []


@pytest.mark.asyncio
async def test_missing_or_unknown_external_configuration_preserves_local_completion():
    env = await composition()
    env.service.admission = create_public_record_source_admission(
        owner=env.owner,
        inbox=env.profile.inbox,
        presence=env.presence,
        admission=env.fresh,
        implementation_revision='unknown.v9',
    )
    assert (await complete(env))['status'] == 'completed'
    await env.service.ensure_queued(user_id='u', session_id='session')
    await env.service.recover_pending()
    assert (await row(env))['status'] == 'retry_wait'
    assert not env.admission_records.documents and not env.profile.collections['publications'].documents
    env.service.admission = create_public_record_source_admission(
        owner=env.owner,
        inbox=env.profile.inbox,
        presence=env.presence,
        admission=env.fresh,
    )
    assert (await retry(env))['status'] == 'waiting_receipt'


@pytest.mark.parametrize('phase', ['reserve_fresh_source', 'authorize_fresh_source', 'receive'])
@pytest.mark.asyncio
async def test_real_lost_ack_replays_fixed_authorization_after_new_adapter(phase, monkeypatch):
    env = await composition()
    await complete(env)
    target = env.profile.inbox if phase == 'receive' else env.fresh
    original = getattr(target, phase)
    first = [True]

    async def lost(*args, **kwargs):
        result = await original(*args, **kwargs)
        if first[0]:
            first[0] = False
            raise RuntimeError('offline ACK loss')
        return result

    monkeypatch.setattr(target, phase, lost)
    await env.service.recover_pending()
    assert (await row(env))['status'] == 'retry_wait'
    authorization = deepcopy(next(iter(env.admission_records.documents.values()))['authorization'])
    env.service.admission = create_public_record_source_admission(
        owner=env.owner,
        inbox=env.profile.inbox,
        presence=env.presence,
        admission=env.fresh,
    )
    assert (await retry(env))['status'] == 'waiting_receipt'
    current = await row(env)
    assert current['authorization']['authorization'] == authorization
    assert len([x for x in env.profile.collections['publications'].documents if x['recordKind'] == 'publication']) == 1
    assert len(env.admission_records.documents) == 1


@pytest.mark.asyncio
async def test_reserve_replay_keeps_real_ready_receipt_and_does_not_rescan_old_absence(monkeypatch):
    env = await composition()
    await complete(env)
    await env.service.recover_pending()
    current = await row(env)
    reservation = await env.service.admission.reserve(
        material=json.loads(current['materialJson']),
        snapshot_ref=current['snapshotRef'],
        completion_id=current['completionId'],
        draft_id=current['draftId'],
    )
    assert reservation['status'] == 'ready'
    # Simulate the worker's reservation/authorization ACK checkpoints being lost;
    # actual Profile and owner records are already durable, including a publication.
    stored = env.intents.documents[env.intent_id]
    stored.pop('reservation')
    stored.pop('authorization')
    stored.pop('receipt')
    env.presence.read_legacy_presence = AsyncMock(side_effect=AssertionError('ready replay must not rescan'))
    assert (await retry(env))['status'] == 'waiting_receipt'
    assert (await row(env))['reservation']['status'] == 'ready'


@pytest.mark.parametrize('mutation', ['raw', 'commit', 'digest', 'mode', 'selection', 'source_revision', 'duplicate'])
@pytest.mark.asyncio
async def test_complete_business_binding_is_not_replaced_by_outbox_absence(mutation):
    env = await composition()
    saved = await complete(env)
    binding = saved['qaProfileIngestion']['recordSourceIntent']
    if mutation == 'raw':
        saved['qaHistory'][0]['answer'] = 'changed original'
    elif mutation == 'commit':
        binding['completionId'] = 'other'
    elif mutation == 'digest':
        binding['rawDigest'] = '0' * 64
    elif mutation == 'mode':
        saved['qaProfileIngestion']['deliveryMode'] = 'legacy'
    elif mutation == 'selection':
        saved['recordProfileConsumerRevision'] = 'unknown.v2'
    elif mutation == 'source_revision':
        saved['qaSourceRevision'] = 2
    else:
        env.sessions.documents['duplicate'] = deepcopy(saved)
    await env.service.recover_pending()
    assert not env.admission_records.documents
    assert not env.profile.collections['publications'].documents
    assert (await row(env))['status'] in {'failed', 'awaiting_completion'}


@pytest.mark.parametrize('job_status', ['queued', 'processing', 'completed', 'failed', 'dead_letter'])
@pytest.mark.asyncio
async def test_all_old_profile_job_history_blocks_fresh_without_changing_completed_record(job_status):
    env = await composition()
    saved = await complete(env)
    env.profile.collections['jobs'].documents.append(
        {
            '_id': 'old',
            'jobId': 'old',
            'userId': 'u',
            'subjectId': None,
            'sourceType': 'record',
            'sourceId': 'session',
            'sourceRevision': 1,
            'status': job_status,
        }
    )
    await env.service.recover_pending()
    assert saved['status'] == 'completed'
    assert (await row(env))['status'] == 'retry_wait'
    assert not env.admission_records.documents and not env.profile.collections['publications'].documents


@pytest.mark.asyncio
async def test_actual_chat_handoff_retains_complete_evidence_without_writing_chat():
    from modules.chat.application.service import ChatSessionManager

    env = await composition()
    saved = await complete(env)
    saved['createdAt'] = '2026-11-10T00:00:00'
    manager = object.__new__(ChatSessionManager)
    converted = manager._convertQASessionToChat(saved)
    # The conversion method is synchronous and retains the nested source binding.
    env.sessions.documents[saved['_id']] = converted
    env.cache.get.return_value = deepcopy(converted)
    before = deepcopy(converted)
    await env.service.recover_pending()
    assert (await row(env))['status'] == 'waiting_receipt'
    assert env.sessions.documents[saved['_id']] == before


@pytest.mark.parametrize('bad', [False, True])
@pytest.mark.asyncio
async def test_completed_qa_with_pending_commit_ack_requires_the_same_fixed_commit(bad):
    env = await composition()
    await complete(env)
    env.cache.get.return_value = {
        'userId': 'u',
        'sessionId': 'session',
        'recordProfileConsumerRevision': RECORD_PROFILE_SOURCE_REVISION,
        'qaCommit': {
            'version': 'record.completion.v1',
            'commitId': 'wrong' if bad else 'commit-1',
            'snapshotJson': json.dumps(env.raw, ensure_ascii=False),
        },
    }
    await env.service.recover_pending()
    assert (await row(env))['status'] == ('failed' if bad else 'waiting_receipt')
    assert bool(env.admission_records.documents) is not bad


@pytest.mark.asyncio
async def test_unknown_active_cache_and_lost_session_lease_never_issue_owner_proof():
    env = await composition()
    await complete(env)
    env.cache.get.return_value = {'userId': 'u', 'sessionId': 'session', 'type': 'qa', 'status': 'active'}
    await env.service.recover_pending()
    assert (await row(env))['status'] == 'failed' and not env.admission_records.documents
    second = await composition()
    await complete(second)
    second.leases.lost = True
    await second.service.recover_pending()
    assert (await row(second))['status'] == 'retry_wait' and not second.admission_records.documents


@pytest.mark.asyncio
async def test_owner_fixed_read_is_pure_scoped_and_current_held_check_detects_changed_evidence():
    env = await composition()
    await complete(env)
    await env.service.recover_pending()
    stored = next(iter(env.admission_records.documents.values()))
    ref = ProfileSourceSnapshotRefV1.model_validate(stored['authorization']['ownerFenceRef'])
    count = len(env.admission_records.documents)
    proof = await env.owner.read_admission_fence(scope=env.profile.scope, snapshot_ref=ref)
    assert len(env.admission_records.documents) == count
    with pytest.raises(PermissionError):
        await env.owner.read_admission_fence(scope=ProfileScope(user_id='other', subject_id='other'), snapshot_ref=ref)
    next(iter(env.sessions.documents.values()))['qaHistory'].clear()
    with pytest.raises(RecordSourceConflict):
        await env.owner.is_admission_fence_held(
            scope=env.profile.scope, snapshot_ref=ref, content_digest=stored['authorization']['ownerFenceDigest']
        )
    assert proof == stored['proof']


@pytest.mark.asyncio
async def test_concurrent_reserve_and_owner_insert_ack_loss_keep_one_fixed_authorization(monkeypatch):
    env = await composition()
    await complete(env)
    original = env.admission_records.insert_one
    first = [True]

    async def lost(value):
        result = await original(value)
        if first[0]:
            first[0] = False
            raise RuntimeError('owner store ACK lost')
        return result

    monkeypatch.setattr(env.admission_records, 'insert_one', lost)
    await env.service.recover_pending()
    fixed = deepcopy(next(iter(env.admission_records.documents.values())))
    current = await row(env)
    request = request_from(current)
    values = await asyncio.gather(
        *[
            env.service.admission.reserve(
                material=request['material'],
                snapshot_ref=request['snapshotRef'],
                completion_id=request['completionId'],
                draft_id=request['draftId'],
            )
            for _ in range(2)
        ],
        return_exceptions=True,
    )
    successful = [value for value in values if isinstance(value, dict)]
    assert successful and all(value['authorization'] == fixed['authorization'] for value in successful)
    assert (await retry(env))['status'] == 'waiting_receipt'
    assert next(iter(env.admission_records.documents.values())) == fixed


@pytest.mark.asyncio
async def test_changed_immutable_proof_and_changed_scope_do_not_fall_back_to_fresh():
    env = await composition()
    await complete(env)
    await env.service.recover_pending()
    current = await row(env)
    request = request_from(current)
    request['material']['scope']['subjectId'] = 'other'
    with pytest.raises(RecordSourceConflict):
        await env.owner.existing(request)
    stored = next(iter(env.admission_records.documents.values()))
    stored['authorization']['authorizationId'] = 'rewritten'
    with pytest.raises(RecordSourceConflict, match='record was changed'):
        await env.owner.read_admission_fence(
            scope=env.profile.scope,
            snapshot_ref=ProfileSourceSnapshotRefV1.model_validate(stored['authorization']['ownerFenceRef']),
        )


@pytest.mark.parametrize('field', ['event', 'reservation', 'authorization', 'receipt', 'dispatchAttempts'])
@pytest.mark.asyncio
async def test_existing_local_formal_delivery_without_owner_proof_is_never_called_fresh(field):
    env = await composition()
    await complete(env)
    env.intents.documents[env.intent_id][field] = (
        1 if field == 'dispatchAttempts' else {'unknown': 'old formal identity'}
    )
    await env.service.recover_pending()
    assert (await row(env))['status'] == 'failed'
    assert not env.admission_records.documents and not env.profile.collections['publications'].documents


@pytest.mark.asyncio
async def test_real_legacy_sender_is_blocked_before_profile_marker_exists_and_after_authorization():
    from health_profile.ingestion import HealthProfileJobQueue
    from health_profile.record_source import MongoRecordProfileStore
    from health_profile.record_source import RecordProfileSourceService
    from tests.unit.health_profile.test_record_profile_source import FakeSessionCollection
    from tests.unit.health_profile.test_record_profile_source import InMemoryJobRepository

    env = await composition()
    saved = await complete(env)
    jobs = InMemoryJobRepository()
    legacy = RecordProfileSourceService(
        MongoRecordProfileStore(FakeSessionCollection(saved)), HealthProfileJobQueue(jobs)
    )
    before = deepcopy(saved)
    result = await legacy.ensure_queued(user_id='u', session_id='session')
    assert not result.ingestion_queued and not jobs.jobs
    assert not env.profile.collections['publications'].documents
    legacy.prepare_completed_snapshot(saved)
    assert saved == before
    await env.service.recover_pending()
    result = await legacy.ensure_queued(user_id='u', session_id='session')
    assert not result.ingestion_queued and not jobs.jobs


@pytest.mark.parametrize('kind', ['family_fact', 'overflow', 'unavailable', 'after_read_race'])
@pytest.mark.asyncio
async def test_full_history_absence_rejects_relative_contribution_incomplete_scan_and_races(kind):
    env = await composition()
    await complete(env)
    if kind == 'family_fact':
        env.profile.collections['facts'].documents.append(
            {
                '_id': 'family-fact',
                'factId': 'family-fact',
                'userId': 'u',
                'subjectId': 'father',
                'status': 'deleted',
                'version': 3,
                'sources': [{'sourceType': 'record', 'sourceId': 'session', 'sourceRevision': 1}],
            }
        )
    elif kind == 'overflow':
        env.profile.collections['jobs'].documents.extend(
            {
                '_id': f'old-{i}',
                'userId': 'u',
                'sourceType': 'record',
                'sourceId': 'session',
                'status': 'completed',
            }
            for i in range(1001)
        )
    elif kind == 'unavailable':
        env.presence_collections['jobs'].fail = True
    else:

        def race():
            env.presence_collections['jobs'].after_read = None
            env.profile.collections['jobs'].documents.append(
                {
                    '_id': 'raced',
                    'userId': 'u',
                    'sourceType': 'record',
                    'sourceId': 'session',
                    'status': 'processing',
                }
            )

        env.presence_collections['jobs'].after_read = race
    await env.service.recover_pending()
    assert not env.profile.collections['publications'].documents
    assert (await row(env))['status'] in {'failed', 'retry_wait'}
    if kind != 'after_read_race':
        assert not env.admission_records.documents


@pytest.mark.asyncio
async def test_reserve_freezes_inputs_before_first_await(monkeypatch):
    env = await composition()
    await complete(env)
    await env.service.recover_pending()
    current = await row(env)
    material, ref = json.loads(current['materialJson']), deepcopy(current['snapshotRef'])
    entered, resume = asyncio.Event(), asyncio.Event()
    original = env.owner.store.get

    async def blocked(identity):
        entered.set()
        await resume.wait()
        return await original(identity)

    monkeypatch.setattr(env.owner.store, 'get', blocked)
    pending = asyncio.create_task(
        env.service.admission.reserve(
            material=material,
            snapshot_ref=ref,
            completion_id=current['completionId'],
            draft_id=current['draftId'],
        )
    )
    await entered.wait()
    material['qaHistory'].clear()
    ref['snapshotId'] = 'mutated'
    resume.set()
    state = await pending
    assert state['status'] == 'ready'
    assert state['authorization'] == current['authorization']['authorization']


@pytest.mark.asyncio
async def test_full_completed_evidence_rejects_corruption_before_any_profile_scope_command(monkeypatch):
    env = await composition()
    saved = await complete(env)
    before = deepcopy(env.profile.collections['publications'].documents)
    scope = AsyncMock(wraps=env.profile.inbox.prepare_self_scope)
    monkeypatch.setattr(env.profile.inbox, 'prepare_self_scope', scope)
    saved['qaHistory'].append({'answer': 'an uncommitted extra answer'})
    await env.service.recover_pending()
    assert (await row(env))['status'] == 'failed'
    assert scope.await_count == 0 and not env.admission_records.documents
    assert env.profile.collections['publications'].documents == before


@pytest.mark.asyncio
async def test_pure_completion_evidence_can_replay_under_existing_business_lease():
    from utils.identifiers import create_session_lease_id

    env = await composition()
    lease = await env.leases.try_acquire(create_session_lease_id('u', 'session'), 'business-request')
    try:
        await complete(env)
        assert await env.completion.publish_once('u', 'session', deepcopy(env.raw))
        assert env.leases.held[lease.key] is lease
        assert not env.admission_records.documents and not env.profile.collections['publications'].documents
    finally:
        await env.leases.release(lease)
