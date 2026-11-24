"""GET stays pure while background recovery uses durable budgets and frozen results."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.persistence.record_creation import RedisRecordReplyRecoveryStore
from modules.record.application.recovery import ReplyRecoveryCoordinator
from modules.record.application.recovery import ReplyRecoveryPolicy
from modules.record.application.recovery import eligible_for_scan

from .test_dynamic_record import cached
from .test_missing_reply_recovery import assert_original_data_retained
from .test_missing_reply_recovery import missing_reply


class RecoveryStore:
    encode_snapshot = staticmethod(RedisRecordReplyRecoveryStore.encode_snapshot)
    decode_snapshot = staticmethod(RedisRecordReplyRecoveryStore.decode_snapshot)

    def __init__(self, module):
        self.module = module
        self.fail_final_once = False
        self.writes = 0

    def current(self):
        raw = self.module.redis_client.value
        return json.loads(raw) if raw else None

    async def pending_replies(self, *, now, limit):
        value = self.current()
        return [('u', 's')] if eligible_for_scan(value, now) else []

    async def compare_session(self, user_id, session_id, expected, session, *, ttl_seconds):
        if self.current() != expected:
            return False
        if self.fail_final_once and expected.get('qaRecovery', {}).get('resultJson') and not session.get('qaRecovery'):
            self.fail_final_once = False
            return False
        self.module.redis_client.value = json.dumps(session, ensure_ascii=False)
        self.writes += 1
        return True

    async def compare_delete(self, user_id, session_id, expected):
        if self.current() != expected:
            return False
        self.module.redis_client.value = None
        return True


class CompletionStore:
    def __init__(self, module):
        self.module = module
        self.calls = []

    async def publish_once(self, user_id, session_id, session):
        self.calls.append(deepcopy(session))
        previous = self.module.collection_sessions.document
        if previous is None:
            self.module.collection_sessions.document = deepcopy(session)
        elif previous.get('recordCompletionId') != session['recordCompletionId']:
            raise ValueError('conflicting completion')
        return True


async def environment(shape='v2-missing'):
    manager, module, gateway, reports, memory, before = await missing_reply(shape)
    store = RecoveryStore(module)
    clock = SimpleNamespace(now=100.0)
    manager._reply_recovery_store = store
    manager._completion_store = CompletionStore(module)
    manager._reply_recovery = ReplyRecoveryCoordinator(manager, store, clock=lambda: clock.now)
    return SimpleNamespace(
        manager=manager,
        module=module,
        gateway=gateway,
        reports=reports,
        memory=memory,
        before=before,
        store=store,
        clock=clock,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', ['v2-missing', 'v2-stale', 'legacy-active-missing', 'legacy-awaiting'])
async def test_queries_do_not_acquire_lease_generate_or_save_missing_reply(shape):
    env = await environment(shape)
    calls = len(env.gateway.requests)
    leases = len(env.manager._agent_runtime.lease_manager.acquire_calls)
    before = deepcopy(cached(env.module))
    for method in ('getSession', 'getHistory'):
        with pytest.raises(env.module.QASessionConflictError) as failure:
            await getattr(env.manager, method)('u', 's')
        assert failure.value.code == 'RECORD_REPLY_PENDING'
    assert len(env.gateway.requests) == calls
    assert len(env.manager._agent_runtime.lease_manager.acquire_calls) == leases
    assert cached(env.module) == before and env.store.writes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', ['v2-missing', 'legacy-awaiting'])
async def test_background_scan_recovers_without_any_get_and_queries_only_render(shape):
    env = await environment(shape)
    before_calls = len(env.gateway.requests)
    reports, memory = len(env.reports.calls), len(env.memory.calls)
    assert await env.manager.recoverPendingReplies() == 1
    assert_original_data_retained(cached(env.module), env.before)
    assert len(env.gateway.requests) - before_calls == (0 if shape == 'legacy-awaiting' else 2)
    assert len(env.reports.calls) == reports and len(env.memory.calls) == memory
    frozen_calls = len(env.gateway.requests)
    session = await env.manager.getSession('u', 's')
    history = await env.manager.getHistory('u', 's')
    assert session['questionId'] == cached(env.module)['currentQuestion']['questionId']
    assert history['status'] == 'question' and len(history['history']) == len(env.before['qaHistory'])
    assert len(env.gateway.requests) == frozen_calls
    assert await env.manager.recoverPendingReplies() == 0


@pytest.mark.asyncio
async def test_frozen_result_survives_commit_failure_and_restarts_without_model():
    env = await environment()
    env.store.fail_final_once = True
    calls = len(env.gateway.requests)
    await env.manager.recoverPendingReplies()
    receipt = cached(env.module)['qaRecovery']
    assert receipt['status'] == 'result_ready'
    fixed = env.store.decode_snapshot(receipt['resultJson'])
    assert len(env.gateway.requests) == calls + 2
    env.clock.now += 16
    env.manager._reply_recovery = ReplyRecoveryCoordinator(env.manager, env.store, clock=lambda: env.clock.now)
    assert await env.manager.recoverPendingReplies() == 1
    assert (await env.manager.getSession('u', 's'))['questionId'] == fixed['currentQuestion']['questionId']
    assert len(env.gateway.requests) == calls + 2


@pytest.mark.asyncio
async def test_retry_attempts_and_deadline_survive_new_worker_and_exhaustion_is_visible():
    env = await environment()
    env.manager._reply_recovery.policy = ReplyRecoveryPolicy(max_attempts=2, deadline_seconds=60, retry_seconds=5)
    env.manager._recoverReplyUnderLease = AsyncMock(side_effect=RuntimeError('synthetic failure'))
    await env.manager.recoverPendingReplies()
    first = cached(env.module)['qaRecovery']
    assert first['attempts'] == 1 and first['deadlineAt'] == 160
    env.clock.now += 6
    env.manager._reply_recovery = ReplyRecoveryCoordinator(env.manager, env.store, clock=lambda: env.clock.now)
    await env.manager.recoverPendingReplies()
    last = cached(env.module)['qaRecovery']
    assert last['attempts'] == 2 and last['maxAttempts'] == 2 and last['deadlineAt'] == 160
    assert last['status'] == 'exhausted'
    env.clock.now += 100
    assert await env.manager.recoverPendingReplies() == 0
    with pytest.raises(env.module.QASessionConflictError) as failure:
        await env.manager.getSession('u', 's')
    assert failure.value.code == 'RECORD_REPLY_RECOVERY_FAILED'
    assert env.manager._recoverReplyUnderLease.await_count == 2


@pytest.mark.asyncio
async def test_expired_original_submission_budget_only_publishes_fallback_without_model():
    from .test_submission_receipt import ANSWER
    from .test_submission_receipt import assert_published_once
    from .test_submission_receipt import interrupted_submission

    manager, module, gateway, current, before = await interrupted_submission()
    store = RecoveryStore(module)
    manager._reply_recovery_store = store
    manager._completion_store = CompletionStore(module)
    manager._reply_recovery = ReplyRecoveryCoordinator(manager, store)
    session = cached(module)
    session['qaFlow']['pendingSubmission']['deadlineAt'] = 0.0
    module.redis_client.value = json.dumps(session)
    calls = len(gateway.requests)
    assert await manager.recoverPendingReplies() == 1
    saved = assert_published_once(module, before, current)
    assert saved['qaHistory'][-1]['answer'] == ANSWER
    assert len(gateway.requests) == calls
    assert (await manager.getSession('u', 's'))['status'] == 'question'


@pytest.mark.asyncio
async def test_stale_expected_session_cannot_replace_another_writer_result():
    env = await environment()
    async with env.manager._sessionLease('u', 's'):
        original = await env.manager._load_session('u', 's')
        changed = {**deepcopy(original), 'concurrent': 'new owner'}
        env.module.redis_client.value = json.dumps(changed)
        with pytest.raises(env.module.QASessionConflictError):
            await env.manager._compareRecordSession(original)
    assert cached(env.module)['concurrent'] == 'new owner'


@pytest.mark.asyncio
async def test_pending_submission_recovers_in_background_with_original_options_and_identity():
    from .test_submission_receipt import assert_published_once
    from .test_submission_receipt import interrupted_submission

    manager, module, gateway, current, before = await interrupted_submission()
    store = RecoveryStore(module)
    manager._reply_recovery_store = store
    manager._completion_store = CompletionStore(module)
    manager._reply_recovery = ReplyRecoveryCoordinator(manager, store)
    calls = len(gateway.requests)
    assert await manager.recoverPendingReplies() == 1
    assert_published_once(module, before, current)
    requests = gateway.requests[calls:]
    assert len(requests) == 2
    analysis = next(request for request in requests if request.profile.name == 'record.plan')
    assert analysis.reasoning_effort == 'high' and not analysis.thinking


@pytest.mark.asyncio
async def test_foreground_generated_reply_is_frozen_before_failed_save_then_replayed_without_model():
    from .test_submission_receipt import ANSWER
    from .test_submission_receipt import assert_published_once
    from .test_submission_receipt import interrupted_submission

    manager, module, gateway, current, before = await interrupted_submission()
    store = RecoveryStore(module)
    manager._reply_recovery_store = store
    manager._completion_store = CompletionStore(module)
    manager._reply_recovery = ReplyRecoveryCoordinator(manager, store)
    store.fail_final_once = True
    with pytest.raises(module.QASessionConflictError):
        await manager.submitAnswer('u', 's', current['questionId'], ANSWER)
    frozen = store.decode_snapshot(cached(module)['qaRecovery']['resultJson'])
    calls = len(gateway.requests)
    replay = await manager.submitAnswer('u', 's', current['questionId'], ANSWER)
    assert replay['questionId'] == frozen['currentQuestion']['questionId']
    assert len(gateway.requests) == calls
    assert_published_once(module, before, current)


@pytest.mark.asyncio
async def test_completed_result_lost_ack_keeps_profile_marker_and_cannot_overwrite_chat_owner():
    from datetime import datetime

    from modules.record.application import flow

    env = await environment()
    original = cached(env.module)
    final = deepcopy(original)
    final.update(status='completed', completedAt='2026-11-10T10:00:00', currentQuestion=None)
    final['qaFlow'].update(phase='finished', finalMessage=flow.COMPLETION_MESSAGE)
    marker = {'status': 'pending', 'updatedAt': datetime(2026, 11, 10, 10)}
    final['qaProfileIngestion'] = marker
    store = env.manager._completion_store
    publish = store.publish_once
    lost = True

    async def lost_ack(*args):
        nonlocal lost
        await publish(*args)
        if lost:
            lost = False
            raise RuntimeError('lost Mongo acknowledgement')
        return True

    store.publish_once = lost_ack
    async with env.manager._sessionLease('u', 's') as guard:
        await env.manager._load_session('u', 's')
        with pytest.raises(RuntimeError, match='lost Mongo'):
            await env.manager._save_session('u', 's', final, completed=True, lease_guard=guard)
    frozen_intent = deepcopy(cached(env.module)['qaCommit'])
    env.clock.now = frozen_intent['nextAttemptAt'] + 1
    published = env.module.collection_sessions.document
    published.update(type='chat', qaCompletedAt=published['completedAt'], messages=[{'content': 'later Chat turn'}])
    after_chat = deepcopy(published)
    calls = len(env.gateway.requests)
    env.manager._ensureRecordProfileQueued = AsyncMock()
    assert await env.manager.recoverPendingReplies() == 1
    assert env.module.collection_sessions.document == after_chat
    assert env.module.collection_sessions.document['qaProfileIngestion'] == marker
    assert env.module.redis_client.value is None
    assert len(env.gateway.requests) == calls
    assert store.calls[0] == store.calls[1]
    assert store.calls[1]['recordCompletionId'] == frozen_intent['commitId']
    env.manager._ensureRecordProfileQueued.assert_awaited_once_with('u', 's')


@pytest.mark.asyncio
async def test_unsupported_persisted_agent_selection_fails_without_model_or_latest_fallback():
    env = await environment()
    env.manager._reply_recovery.policy = ReplyRecoveryPolicy(retry_seconds=1)
    env.manager._recoverReplyUnderLease = AsyncMock(side_effect=RuntimeError('interrupted before generation'))
    await env.manager.recoverPendingReplies()
    pending = cached(env.module)
    pending['qaRecovery']['agentSelection']['implementationRevision'] = 'unavailable-old-worker'
    env.module.redis_client.value = json.dumps(pending)
    env.clock.now += 2
    calls = len(env.gateway.requests)
    assert await env.manager.recoverPendingReplies() == 1
    assert cached(env.module)['qaRecovery']['status'] == 'failed'
    assert len(env.gateway.requests) == calls
    assert env.manager._recoverReplyUnderLease.await_count == 1
    with pytest.raises(env.module.QASessionConflictError) as failure:
        await env.manager.getHistory('u', 's')
    assert failure.value.code == 'RECORD_REPLY_RECOVERY_FAILED'


@pytest.mark.asyncio
async def test_closing_public_stream_closes_private_model_and_releases_session_lease():
    from agents.llm import LLMChunk

    from .test_dynamic_record import setup_manager

    manager, module, gateway, runtime = setup_manager()
    current = await manager.createSession('u', 's')
    closed = []

    async def model_stream(request):
        try:
            yield LLMChunk(reasoning_content='synthetic progress')
            raise AssertionError('closing the response must not request another model chunk')
        finally:
            closed.append(True)

    gateway.stream = model_stream
    releases = len(runtime.lease_manager.release_calls)
    events = manager.submitAnswerStream('u', 's', current['questionId'], 'synthetic answer', thinking=True)
    assert (await anext(events))['data']['type'] == 'start'
    assert (await anext(events))['data']['type'] == 'reasoning'
    await events.aclose()
    assert closed == [True]
    assert len(runtime.lease_manager.release_calls) == releases + 1
    assert cached(module)['qaFlow']['pendingSubmission']['answer'] == 'synthetic answer'
    assert not cached(module)['qaHistory']


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_child_selections_ignore_a_newer_registered_legacy_alias(streaming):
    from agents.runtime.registry import GraphRegistry

    from .test_dynamic_record import setup_manager
    from .test_dynamic_record import submit

    manager, _, gateway, runtime = setup_manager()
    registered = [runtime.registry.get(name) for name in runtime.registry.capabilities()]
    replacement = GraphRegistry()
    future = SimpleNamespace(ainvoke=AsyncMock(side_effect=AssertionError('future child must not run')))
    for old in registered:
        replacement.register(old.capability, old.graph, version=old.version, legacy=False)
        replacement.register(old.capability, future, version='future-v2', legacy=True)
    runtime.registry.publish(replacement)
    current = await manager.createSession('u', 's')
    result = await submit(manager, current, 'synthetic saved answer', streaming=streaming)
    assert result['status'] == 'question'
    assert gateway.requests and {request.profile.name for request in gateway.requests} == {
        'record.plan',
        'record.question',
    }
    future.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_fixed_child_version_fails_before_any_model_and_legacy_receipt_cannot_follow_current():
    from agents.runtime.registry import GraphRegistry
    from modules.record.agent import LEGACY_RECORD_AGENT_MANIFEST
    from modules.record.agent import RECORD_AGENT_MANIFEST

    from .test_dynamic_record import setup_manager

    manager, module, gateway, runtime = setup_manager()
    current = await manager.createSession('u', 's')
    before = len(gateway.requests)
    runtime.registry.publish(GraphRegistry())
    with pytest.raises(module.QASessionConflictError) as failure:
        await manager.submitAnswer('u', 's', current['questionId'], 'synthetic answer')
    assert failure.value.code == 'RECORD_AGENT_VERSION_UNAVAILABLE'
    assert len(gateway.requests) == before
    # The historical mapping stays independent when an implementation manifest changes.
    fixed = deepcopy(LEGACY_RECORD_AGENT_MANIFEST)
    original = deepcopy(RECORD_AGENT_MANIFEST)
    try:
        RECORD_AGENT_MANIFEST['implementationRevision'] = 'future-main'
        with pytest.raises(module.QASessionConflictError) as failure:
            manager._validateSubmissionReceipt({'currentQuestion': {}}, {})
        assert failure.value.code == 'RECORD_AGENT_VERSION_UNAVAILABLE'
        assert LEGACY_RECORD_AGENT_MANIFEST == fixed
    finally:
        RECORD_AGENT_MANIFEST.clear()
        RECORD_AGENT_MANIFEST.update(original)


@pytest.mark.asyncio
async def test_frozen_reply_storage_budget_survives_restart_and_exposes_terminal_failure():
    env = await environment()
    env.store.fail_final_once = True
    await env.manager.recoverPendingReplies()
    frozen = deepcopy(cached(env.module)['qaRecovery'])
    compare = env.store.compare_session

    async def fail_commit(user_id, session_id, expected, session, *, ttl_seconds):
        if expected.get('qaRecovery', {}).get('resultJson') and not session.get('qaRecovery'):
            return False
        return await compare(user_id, session_id, expected, session, ttl_seconds=ttl_seconds)

    env.store.compare_session = fail_commit
    calls = len(env.gateway.requests)
    for _ in range(10):
        env.clock.now += 16
        env.manager._reply_recovery = ReplyRecoveryCoordinator(env.manager, env.store, clock=lambda: env.clock.now)
        await env.manager.recoverPendingReplies()
    terminal = cached(env.module)['qaRecovery']
    assert terminal['status'] == 'failed'
    assert terminal['errorCode'] == 'RECORD_REPLY_COMMIT_EXHAUSTED'
    assert terminal['resultJson'] == frozen['resultJson']
    assert terminal['commitDeadlineAt'] == frozen['commitDeadlineAt']
    assert terminal['commitAttempts'] <= 8
    assert len(env.gateway.requests) == calls
    with pytest.raises(env.module.QASessionConflictError) as failure:
        await env.manager.getSession('u', 's')
    assert failure.value.code == 'RECORD_REPLY_RECOVERY_FAILED'


@pytest.mark.asyncio
async def test_failed_module_install_blocks_new_ai_before_profile_or_documents_but_preserves_queries():
    from agents.runtime.registry import GraphRegistry
    from modules.record.agent import RECORD_AGENT_SELECTION
    from modules.record.agent import install_record_agent

    from .test_dynamic_record import setup_manager

    manager, module, gateway, runtime = setup_manager()
    current = await manager.createSession('u', 's')
    runtime.registry.publish(GraphRegistry())
    manager._loadHealthProfileContext = AsyncMock(side_effect=AssertionError('no Profile AI after install failure'))
    manager._processHealthReports = AsyncMock(side_effect=AssertionError('no Document work after install failure'))
    calls = len(gateway.requests)
    assert install_record_agent(runtime, manager) is False
    assert not runtime.module_statuses['record.main']['ready']
    assert RECORD_AGENT_SELECTION not in runtime.ports.selections()
    assert (await manager.getSession('u', 's'))['questionId'] == current['questionId']
    assert (await manager.createSession('u', 's'))['questionId'] == current['questionId']
    with pytest.raises(module.QASessionConflictError) as failure:
        await manager.submitAnswer('u', 's', current['questionId'], 'new answer')
    assert failure.value.code == 'RECORD_AGENT_VERSION_UNAVAILABLE'
    module.redis_client.value = None
    with pytest.raises(module.QASessionConflictError):
        await manager.createSession('u', 'new')
    manager._loadHealthProfileContext.assert_not_awaited()
    manager._processHealthReports.assert_not_awaited()
    assert len(gateway.requests) == calls
    assert not runtime._closed


@pytest.mark.asyncio
async def test_install_registers_main_and_failed_readiness_does_not_block_committed_answer_replay():
    from modules.record.agent import RECORD_AGENT_SELECTION
    from modules.record.agent import install_record_agent

    from .test_refresh_reanswer import ready_session

    manager, _, gateway, _, _, published, _, snapshot = await ready_session(streaming=False)
    assert install_record_agent(manager._agent_runtime, manager)
    assert RECORD_AGENT_SELECTION in manager._agent_runtime.ports.selections()
    manager.set_agent_readiness(False)
    last = snapshot['qaHistory'][-1]
    calls = len(gateway.requests)
    replay = await manager.submitAnswer('u', 's', last['question']['questionId'], last['answer'])
    assert replay == published
    assert len(gateway.requests) == calls


@pytest.mark.asyncio
async def test_legacy_missing_reply_new_receipt_keeps_fixed_historical_manifest():
    from modules.record.agent import LEGACY_RECORD_AGENT_MANIFEST
    from modules.record.agent import RECORD_AGENT_MANIFEST

    env = await environment('legacy-awaiting')
    current = deepcopy(RECORD_AGENT_MANIFEST)
    try:
        RECORD_AGENT_MANIFEST['implementationRevision'] = 'future-main'
        calls = len(env.gateway.requests)
        assert await env.manager.recoverPendingReplies() == 1
        receipt = cached(env.module)['qaRecovery']
        assert receipt['agentSelection'] == LEGACY_RECORD_AGENT_MANIFEST
        assert receipt['status'] == 'failed'
        assert len(env.gateway.requests) == calls
    finally:
        RECORD_AGENT_MANIFEST.clear()
        RECORD_AGENT_MANIFEST.update(current)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['create', 'recover'])
async def test_registered_execution_port_rejects_unowned_creation_and_recovery_before_io(operation):
    from agents import AgentRequest
    from agents import AgentRunContext
    from agents.errors import AgentConfigurationError
    from modules.record.agent import RECORD_AGENT_SELECTION
    from modules.record.agent import install_record_agent

    from .test_dynamic_record import setup_manager

    manager, module, gateway, runtime = setup_manager()
    assert install_record_agent(runtime, manager)
    manager._load_session = AsyncMock(side_effect=AssertionError('no unowned session read'))
    manager._loadHealthProfileContext = AsyncMock(side_effect=AssertionError('no unowned Profile read'))
    manager._processHealthReports = AsyncMock(side_effect=AssertionError('no unowned Document processing'))
    request = AgentRequest({'operation': operation, 'user_id': 'u', 'session_id': 's'})
    context = AgentRunContext.create(user_id='u', session_id='s')
    with pytest.raises(AgentConfigurationError, match='owned service execution lease'):
        await runtime.ports.get(RECORD_AGENT_SELECTION).invoke(request, context)
    manager._load_session.assert_not_awaited()
    manager._loadHealthProfileContext.assert_not_awaited()
    manager._processHealthReports.assert_not_awaited()
    assert not gateway.requests and not runtime.lease_manager.acquire_calls
    assert module.redis_client.value is None and module.collection_sessions.document is None
