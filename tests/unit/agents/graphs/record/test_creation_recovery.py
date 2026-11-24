"""Creation acceptance, replay and crash recovery with the production service."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.persistence.record_creation import RedisRecordCreationStore
from adapters.record.documents import LegacyRecordDocumentAdapter
from modules.record.application.creation import CREATION_TTL
from modules.record.application.creation import CreationAttempt
from modules.record.application.creation import CreationExpiredError
from modules.record.application.creation import QACreationCoordinator
from modules.record.application.service import QASessionConflictError
from scripts.recover_qa_creation import recover_creation
from tests.unit.services.analysis.test_document_reuse import reuse_flow

from .test_dynamic_record import assessment
from .test_dynamic_record import setup_manager


class ReceiptRedis:
    """Atomic Redis model, including expiry, used by the production CAS adapter."""

    def __init__(self):
        self.now = time.time()
        self.values = {}
        self.ttls = {}
        self.fail_save = False

    async def get(self, key):
        if self.ttls.get(key, 0) <= self.now:
            self.values.pop(key, None)
        return self.values.get(key)

    async def setex(self, key, ttl, raw):
        self.values[key] = raw
        self.ttls[key] = self.now + ttl
        return True

    async def delete(self, key):
        self.values.pop(key, None)
        return 1

    async def eval(self, script, key_count, key, revision, raw, ttl):
        assert key_count == 1, 'Redis Cluster requires single-key CAS'
        current = await self.get(key)
        if not revision:
            if current is not None:
                return 0
        else:
            previous = json.loads(current) if current else {}
            creation = previous.get('qaCreation', {})
            if creation.get('revision') != revision or creation.get('expiresAt', 0) <= self.now:
                return 0
        if self.fail_save:
            raise RuntimeError('Redis unavailable')
        self.values[key] = raw
        self.ttls[key] = self.now + ttl
        return 1

    async def scan_iter(self, **kwargs):
        for key in list(self.values):
            yield key


def creation_manager(**kwargs):
    manager, module, gateway, runtime = setup_manager(**kwargs)
    redis = ReceiptRedis()
    store = RedisRecordCreationStore(redis)
    manager._session_cache._client = redis
    manager._creation = QACreationCoordinator(manager, store, clock=lambda: redis.now)
    manager._creation.response_wait = 0.5
    # Emulate actual exclusion rather than the always-success lease test double.
    leases = {}

    @asynccontextmanager
    async def lease(user_id, session_id):
        key = (user_id, session_id)
        if key in leases:
            raise QASessionConflictError('busy')
        token = object()
        leases[key] = token

        async def guard():
            if leases.get(key) is not token:
                raise QASessionConflictError('lease lost')

        try:
            yield guard
        finally:
            if leases.get(key) is token:
                leases.pop(key)

    manager._sessionLease = lease
    return SimpleNamespace(manager=manager, module=module, gateway=gateway, redis=redis, store=store, leases=leases)


async def drain(flow):
    tasks = list(flow.manager._creation.tasks.values())
    if tasks:
        await asyncio.gather(*tasks)


async def receipt(flow):
    return await flow.manager._session_cache.get('user-1', 'session-1')


def fail_initial_runtime_once(flow):
    """Keep creation recovery coverage for failures outside model validation."""
    generate = flow.manager._generateTurn
    failed = False

    async def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError('initial generation runtime unavailable')
        async for event in generate(*args, **kwargs):
            yield event

    flow.manager._generateTurn = fail_once


@pytest.mark.asyncio
@pytest.mark.parametrize('generation_fails', [False, True])
async def test_two_minute_http_wait_returns_when_the_current_attempt_finishes(monkeypatch, generation_fails):
    flow = creation_manager()
    # Use the production default instead of this module's short test budget.
    flow.manager._creation = QACreationCoordinator(flow.manager, flow.store, clock=lambda: flow.redis.now)
    if generation_fails:
        fail_initial_runtime_once(flow)
    observed_timeouts = []
    wait_for = asyncio.wait_for

    async def bounded_wait(awaitable, timeout):
        observed_timeouts.append(timeout)
        # A ready question or a failed attempt must return promptly, without
        # waiting for the full HTTP budget or later recovery attempts.
        return await wait_for(awaitable, timeout=1)

    monkeypatch.setattr(asyncio, 'wait_for', bounded_wait)
    result = await flow.manager.createSession('user-1', 'session-1')
    assert observed_timeouts == [120]
    assert result['status'] == ('failed' if generation_fails else 'question')
    if generation_fails:
        assert result['errorCode'] == 'CREATION_INTERNAL_ERROR' and result['retryable']
        assert (await receipt(flow))['qaCreation']['attempts'] == 1


@pytest.mark.asyncio
async def test_acceptance_is_queryable_while_inflight_and_same_input_retry_does_not_contend():
    flow = creation_manager()
    entered, release = asyncio.Event(), asyncio.Event()

    async def profile(user_id):
        entered.set()
        await release.wait()
        return None

    flow.manager._loadHealthProfileContext = AsyncMock(side_effect=profile)
    flow.manager._creation.response_wait = 0.01
    first = await flow.manager.createSession('user-1', 'session-1')
    await entered.wait()
    assert first['status'] == 'processing'
    assert (await flow.manager.getSession('user-1', 'session-1'))['stage'] == 'accepted'
    assert (await flow.manager.getHistory('user-1', 'session-1'))['history'] == []
    assert await flow.manager.hasPendingSession('user-1', 'session-1')
    assert (await flow.manager.createSession('user-1', 'session-1', file_list=[]))['status'] == 'processing'
    with pytest.raises(QASessionConflictError) as conflict:
        await flow.manager.createSession('user-1', 'session-1', name='changed')
    assert conflict.value.code == 'CREATION_REQUEST_CONFLICT'
    release.set()
    await drain(flow)
    question = await flow.manager.getSession('user-1', 'session-1')
    assert question['status'] == 'question' and question['interactionCount'] == 0
    assert (await flow.manager.createSession('user-1', 'session-1'))['questionId'] == question['questionId']
    assert flow.manager._loadHealthProfileContext.await_count == 1
    assert flow.module.collection_sessions.document is None


@pytest.mark.asyncio
async def test_http_waiter_cancellation_keeps_supervised_work_alive():
    flow = creation_manager()
    entered, release = asyncio.Event(), asyncio.Event()

    async def profile(user_id):
        entered.set()
        await release.wait()

    flow.manager._loadHealthProfileContext = profile
    waiter = asyncio.create_task(flow.manager.createSession('user-1', 'session-1'))
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert (await flow.manager.getSession('user-1', 'session-1'))['status'] == 'processing'
    release.set()
    await drain(flow)
    assert (await flow.manager.getSession('user-1', 'session-1'))['status'] == 'question'


@pytest.mark.asyncio
async def test_restart_resumes_checkpoint_and_never_rereads_completed_files_or_profile():
    flow = creation_manager()
    fail_initial_runtime_once(flow)
    reports = SimpleNamespace(process=AsyncMock(return_value={'reportId': 'r', 'reportType': 'lab'}))
    flow.manager._record_document_port = LegacyRecordDocumentAdapter(processor=reports)
    flow.manager._loadHealthProfileContext = AsyncMock(return_value='frozen profile')
    first = await flow.manager.createSession('user-1', 'session-1', file_list=[{'url': 'https://example.test/a.pdf'}])
    assert first['status'] == 'failed' and first['retryable']
    before = await receipt(flow)
    assert before['qaCreation']['checkpoint']['files']['0']['report']['reportId'] == 'r'
    assert before['qaCreation']['attempts'] == 1
    flow.manager._creation = QACreationCoordinator(
        flow.manager, RedisRecordCreationStore(flow.redis), clock=lambda: flow.redis.now
    )
    flow.redis.now += 16
    await flow.manager.recoverPendingCreations()
    await drain(flow)
    assert (await flow.manager.getSession('user-1', 'session-1'))['status'] == 'question'
    assert reports.process.await_count == flow.manager._loadHealthProfileContext.await_count == 1
    assert flow.module.collection_sessions.document is None


@pytest.mark.asyncio
async def test_process_crash_and_last_attempt_interruption_are_recovered_with_a_bound():
    flow = creation_manager()
    flow.gateway.delay = 10
    flow.manager._creation.response_wait = 0.01
    await flow.manager.createSession('user-1', 'session-1')
    task = next(iter(flow.manager._creation.tasks.values()))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    stored = await receipt(flow)
    stored['qaCreation']['attempts'] = 3
    stored['qaCreation']['nextAttemptAt'] = flow.redis.now
    key = flow.manager._get_redis_session_key('user-1', 'session-1')
    flow.redis.values[key] = json.dumps(stored)
    flow.manager._creation = QACreationCoordinator(flow.manager, flow.store, clock=lambda: flow.redis.now)
    await flow.manager.recoverPendingCreations()
    await drain(flow)
    result = await flow.manager.getSession('user-1', 'session-1')
    assert result['status'] == 'failed' and result['errorCode'] == 'CREATION_RETRY_EXHAUSTED'
    assert result['retryable'] is False
    assert await flow.manager.recoverPendingCreations() == 0


@pytest.mark.asyncio
async def test_expiry_cannot_be_extended_by_checkpoint_or_stale_publication():
    flow = creation_manager()
    fail_initial_runtime_once(flow)
    await flow.manager.createSession('user-1', 'session-1')
    before = await receipt(flow)
    attempt = CreationAttempt(flow.manager._creation, before, AsyncMock())
    flow.redis.now += 100
    await attempt.checkpoint('processing_reports', healthProfileContext='saved')
    assert (await receipt(flow))['qaCreation']['expiresAt'] == before['qaCreation']['expiresAt']
    assert max(flow.redis.ttls.values()) <= before['qaCreation']['expiresAt'] + 1
    flow.redis.now += CREATION_TTL
    assert await flow.manager.getSession('user-1', 'session-1') is None
    with pytest.raises(CreationExpiredError):
        await attempt.publish({'status': 'active'})
    assert not flow.redis.values
    assert flow.module.collection_sessions.document is None


@pytest.mark.asyncio
async def test_stale_revision_cannot_replace_new_creation():
    flow = creation_manager()
    fail_initial_runtime_once(flow)
    await flow.manager.createSession('user-1', 'session-1')
    old = await receipt(flow)
    attempt = CreationAttempt(flow.manager._creation, deepcopy(old), AsyncMock())
    await recover_creation(
        flow.manager._session_cache,
        flow.store,
        user_id='user-1',
        session_id='session-1',
        apply=True,
        reason='upstream repaired',
        now=flow.redis.now,
    )
    with pytest.raises(CreationExpiredError):
        await attempt.checkpoint('generating_question')
    assert (await receipt(flow))['qaCreation']['attempts'] == 0
    assert (await receipt(flow))['qaCreation']['expiresAt'] == old['qaCreation']['expiresAt']


@pytest.mark.asyncio
async def test_save_failure_does_not_start_any_expensive_processing():
    flow = creation_manager()
    flow.redis.fail_save = True
    with pytest.raises(RuntimeError, match='Redis'):
        await flow.manager.createSession('user-1', 'session-1')
    assert not flow.gateway.requests
    assert not flow.manager._creation.tasks
    assert flow.module.collection_sessions.document is None


@pytest.mark.asyncio
async def test_attempt_timeout_is_queryable_and_stops_after_three_attempts():
    flow = creation_manager()
    flow.gateway.delay = 1
    flow.manager._creation.attempt_timeout = 0.01
    for number in range(3):
        result = await flow.manager.createSession('user-1', 'session-1')
        assert result['status'] == 'failed'
        assert result['retryable'] is (number < 2)
        flow.redis.now += 16
    assert result['errorCode'] == 'CREATION_RETRY_EXHAUSTED'
    assert (await receipt(flow))['qaCreation']['attempts'] == 3


@pytest.mark.asyncio
async def test_report_uncertainty_recovers_same_bytes_without_downloading_again():
    documents = reuse_flow()
    flow = creation_manager(health_report_processing_service=documents.service)
    original_submit = documents.client.submit_local_file
    calls = []

    def submit(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError('submission response lost')
        return original_submit(*args, **kwargs)

    documents.client.submit_local_file = submit
    documents.coordinator.now = lambda: datetime.fromtimestamp(flow.redis.now, timezone.utc)
    request = {'file_list': [{'url': 'https://example.test/report.pdf'}]}
    first = await flow.manager.createSession('user-1', 'session-1', **request)
    assert first['errorCode'] == 'DOCUMENT_SUBMISSION_UNCONFIRMED'
    assert first['retryable'] and first['retryAfterSeconds'] >= 299
    stored = await receipt(flow)
    assert stored['qaCreation']['checkpoint']['references']['0'].startswith('document_')
    documents.downloader.download.side_effect = AssertionError('must reuse archived bytes')
    flow.redis.now += 301
    await flow.manager.recoverPendingCreations()
    await drain(flow)
    result = await flow.manager.getSession('user-1', 'session-1')
    assert result['status'] == 'question'
    assert len(calls) == 2 and len(documents.jobs.jobs) == 1
    assert documents.downloader.download.await_count == 1
    assert flow.module.collection_sessions.document is None


@pytest.mark.asyncio
async def test_exhausted_model_output_settles_attachment_and_keeps_published_qa_snapshot():
    documents = reuse_flow()
    original = documents.graph.content
    documents.graph.content = '{"observations":['
    flow = creation_manager(health_report_processing_service=documents.service)
    request = {'file_list': [{'url': 'https://example.test/report.pdf'}]}
    first = await flow.manager.createSession('user-1', 'session-1', **request)
    assert first['status'] == 'question'
    assert first['documentProcessing'][0]['status'] == 'unavailable'
    assert first['documents'] == []
    count = len(documents.graph.calls)
    flow.redis.now += 16
    await flow.manager.recoverPendingCreations()
    await drain(flow)
    assert len(documents.graph.calls) == count
    assert (await flow.manager.getSession('user-1', 'session-1'))['documentProcessing'] == first['documentProcessing']
    documents.graph.content = original
    second = await flow.manager.createSession('user-1', 'session-2', **request)
    assert second['status'] == 'question'
    assert len(documents.client.submit_calls) == 1
    assert (await flow.manager.getSession('user-1', 'session-1'))['documentProcessing'] == first['documentProcessing']


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_recoverable_creation_continues_to_normal_automatic_completion(streaming):
    flow = creation_manager()
    current = await flow.manager.createSession('user-1', 'session-1')

    async def answer(text):
        if not streaming:
            return await flow.manager.submitAnswer(
                'user-1', 'session-1', current['questionId'], text, None, thinking=False
            )
        events = [
            event
            async for event in flow.manager.submitAnswerStream(
                'user-1',
                'session-1',
                current['questionId'],
                text,
                None,
                thinking=False,
            )
        ]
        return next(event['data'] for event in events if event['data'].get('status') in ('question', 'finished'))

    for _ in range(15):
        current = await answer('餐后偶尔腹胀')
    assert current['phase'] == 'supplement'
    flow.gateway.next_assessment = assessment(intent='no_more', substantive=False)
    current = await answer('没有了')
    assert current['status'] == 'finished' and current['interactionCount'] == 15
    assert not flow.redis.values
    assert flow.module.collection_sessions.document['status'] == 'completed'
    assert 'qaCreation' not in flow.module.collection_sessions.document
    restored = await flow.manager.createSession('user-1', 'session-1')
    assert restored['assistantMessage'] == current['assistantMessage']


@pytest.mark.asyncio
async def test_malformed_shared_cache_entries_cannot_block_other_users_recovery():
    flow = creation_manager()
    for number, raw in enumerate(('broken JSON', '[]', '{"type":"qa","qaCreation":{"expiresAt":null}}')):
        key = f'shulan:session:legacy-{number}:s'
        flow.redis.values[key] = raw
        flow.redis.ttls[key] = flow.redis.now + CREATION_TTL
    fail_initial_runtime_once(flow)
    assert (await flow.manager.createSession('user-1', 'session-1'))['status'] == 'failed'
    flow.redis.now += 16
    await flow.manager.recoverPendingCreations()
    await drain(flow)
    assert (await flow.manager.getSession('user-1', 'session-1'))['status'] == 'question'


@pytest.mark.asyncio
@pytest.mark.parametrize('user_id,session_id', [(123, 's'), ('u', {'id': 's'})])
async def test_invalid_identity_is_rejected_before_acceptance(user_id, session_id):
    flow = creation_manager()
    with pytest.raises(ValueError, match='非空字符串'):
        await flow.manager.createSession(user_id, session_id)
    assert not flow.redis.values
    assert not flow.manager._creation.tasks


@pytest.mark.asyncio
async def test_first_question_is_checkpointed_before_failed_publish_and_retried_without_generation():
    flow = creation_manager()
    compare = flow.store.compare_save
    failed = False

    async def fail_publish_once(user_id, session_id, revision, value, *, ttl_seconds):
        nonlocal failed
        if 'qaCreation' not in value and not failed:
            failed = True
            raise RuntimeError('synthetic publish interruption')
        return await compare(user_id, session_id, revision, value, ttl_seconds=ttl_seconds)

    flow.store.compare_save = fail_publish_once
    result = await flow.manager.createSession('user-1', 'session-1')
    assert result['status'] == 'failed'
    pending = await receipt(flow)
    frozen = deepcopy(pending['qaCreation']['checkpoint']['firstQuestionSession'])
    calls = len(flow.gateway.requests)
    flow.redis.now += 16
    result = await flow.manager.createSession('user-1', 'session-1')
    await drain(flow)
    saved = await receipt(flow)
    assert result['status'] == 'question'
    assert saved['currentQuestion'] == frozen['currentQuestion']
    assert saved['qaFlow'] == frozen['qaFlow']
    assert len(flow.gateway.requests) == calls
