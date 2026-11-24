"""Durable answer acceptance and bounded generation, using synthetic data only."""

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from agents.errors import AgentConfigurationError
from agents.errors import LLMContextStorageError
from modules.record.application import fallback as fallback_policy

from .test_dynamic_record import cached
from .test_dynamic_record import save_cache
from .test_dynamic_record import setup_manager
from .test_missing_reply_recovery import recover_then_query

ANSWER = '近两周餐后偶尔腹胀。'


async def interrupted_submission(*, thinking=True, reasoning_effort='high'):
    """Cancel an in-flight request after its model call starts, simulating shutdown."""
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    before = deepcopy(cached(module))
    started = asyncio.Event()
    complete = gateway.complete

    async def blocked(request):
        started.set()
        await asyncio.Future()

    gateway.complete = blocked
    task = asyncio.create_task(
        manager.submitAnswer(
            'u', 's', current['questionId'], ANSWER, thinking=thinking, reasoning_effort=reasoning_effort
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gateway.complete = complete
    return manager, module, gateway, current, before


def assert_published_once(module, before, current):
    saved = cached(module)
    assert saved['qaHistory'][:-1] == before['qaHistory']
    assert len(saved['qaHistory']) == len(before['qaHistory']) + 1
    assert saved['qaHistory'][-1]['question']['questionId'] == current['questionId']
    assert saved['qaHistory'][-1]['answer'] == ANSWER
    assert saved['answers'][current['questionId']] == ANSWER
    assert saved['qaFlow']['interactionCount'] == before['qaFlow']['interactionCount'] + 1
    assert not saved['qaFlow'].get('pendingSubmission')
    return saved


@pytest.mark.asyncio
async def test_external_cancellation_keeps_receipt_without_counting_or_publishing_answer():
    manager, module, gateway, current, before = await interrupted_submission()
    saved = cached(module)
    receipt = saved['qaFlow']['pendingSubmission']
    assert receipt['question'] == before['currentQuestion']
    assert receipt['answer'] == ANSWER
    assert receipt['answerHash']
    assert receipt['thinking'] is True
    assert receipt['reasoningEffort'] == 'high'
    assert isinstance(receipt['deadlineAt'], (int, float))
    assert receipt['createdAt']
    assert saved['qaHistory'] == before['qaHistory']
    assert saved.get('answers', {}) == before.get('answers', {})
    assert saved['currentQuestion'] == before['currentQuestion']
    assert saved['qaFlow']['interactionCount'] == before['qaFlow']['interactionCount']
    assert manager._agent_runtime.lease_manager.release_calls


@pytest.mark.asyncio
async def test_retry_keeps_original_options_and_completed_replay_does_not_generate():
    manager, module, gateway, current, before = await interrupted_submission()
    calls = len(gateway.requests)
    result = await manager.submitAnswer('u', 's', current['questionId'], ANSWER, thinking=False, reasoning_effort='max')
    assert result['status'] == 'question'
    requests = gateway.requests[calls:]
    analysis = next(item for item in requests if item.profile.name == 'record.plan')
    assert analysis.thinking is True
    assert analysis.reasoning_effort == 'high'
    assert_published_once(module, before, current)
    calls = len(gateway.requests)
    replay = await manager.submitAnswer('u', 's', current['questionId'], ANSWER, thinking=False, reasoning_effort='low')
    assert replay == result
    assert len(gateway.requests) == calls
    assert_published_once(module, before, current)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['getSession', 'getHistory'])
async def test_explicit_recovery_then_queries_preserve_receipt_history(method):
    manager, module, gateway, current, before = await interrupted_submission()
    calls = len(gateway.requests)
    result = await recover_then_query(manager, method)
    assert result['status'] == 'question'
    analysis = next(item for item in gateway.requests[calls:] if item.profile.name == 'record.plan')
    assert analysis.thinking is False
    saved = assert_published_once(module, before, current)
    calls = len(gateway.requests)
    await manager.getSession('u', 's')
    await manager.getHistory('u', 's')
    assert cached(module) == saved
    assert len(gateway.requests) == calls


@pytest.mark.asyncio
async def test_pending_receipt_rejects_different_answer_before_model_or_state_changes():
    manager, module, gateway, current, _ = await interrupted_submission()
    pending = deepcopy(cached(module))
    calls = len(gateway.requests)
    with pytest.raises(module.QASessionConflictError):
        await manager.submitAnswer('u', 's', current['questionId'], '这周完全没有不适。')
    assert cached(module) == pending
    assert len(gateway.requests) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['submitAnswer', 'getSession', 'getHistory'])
async def test_expired_receipt_uses_fallback_without_resetting_generation_budget(method):
    manager, module, gateway, current, before = await interrupted_submission()
    pending = cached(module)
    pending['qaFlow']['pendingSubmission']['deadlineAt'] = 0.0
    save_cache(module, pending)
    calls = len(gateway.requests)
    if method == 'submitAnswer':
        result = await manager.submitAnswer('u', 's', current['questionId'], ANSWER)
    else:
        result = await recover_then_query(manager, method)
    assert result['status'] == 'question'
    assert len(gateway.requests) == calls
    saved = assert_published_once(module, before, current)
    assert saved['qaFlow']['intake']['facts'] == before['qaFlow']['intake']['facts']
    assert saved['currentQuestion']['questionId'] != current['questionId']


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_generation_timeout_returns_fallback_and_cancels_the_outstanding_call(monkeypatch, streaming):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    before = deepcopy(cached(module))
    cancelled = asyncio.Event()
    requests = []

    async def blocked(request):
        requests.append(request)
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async def blocked_stream(request):
        requests.append(request)
        try:
            await asyncio.Future()
            yield  # Make this an async generator without producing an event.
        finally:
            cancelled.set()

    gateway.complete = blocked
    gateway.stream = blocked_stream
    monkeypatch.setattr(module, 'QA_GENERATION_TIMEOUT_SECONDS', 0.25)
    if streaming:
        events = [
            event
            async for event in manager.submitAnswerStream(
                'u', 's', current['questionId'], ANSWER, thinking=True, reasoning_effort='max'
            )
        ]
        assert events[0]['data']['type'] == 'start'
        assert events[-1]['data']['type'] == 'finish'
        assert all(event['code'] == 200 for event in events)
        result = next(event['data'] for event in events if event['data']['type'] == 'question')
    else:
        result = await manager.submitAnswer(
            'u', 's', current['questionId'], ANSWER, thinking=True, reasoning_effort='max'
        )
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert result['status'] == 'question'
    assert requests[0].thinking is True
    assert requests[0].reasoning_effort == 'max'
    saved = assert_published_once(module, before, current)
    assert saved['qaFlow']['intake']['facts'] == before['qaFlow']['intake']['facts']


@pytest.mark.asyncio
async def test_provider_ignoring_cancellation_cannot_overwrite_published_fallback(monkeypatch):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    complete = gateway.complete
    cancelled = asyncio.Event()
    release_late_result = asyncio.Event()
    returned_late_result = asyncio.Event()

    async def cancellation_resistant(request):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release_late_result.wait()
        try:
            return await complete(request)
        finally:
            returned_late_result.set()

    gateway.complete = cancellation_resistant
    monkeypatch.setattr(module, 'QA_GENERATION_TIMEOUT_SECONDS', 0.25)
    try:
        result = await asyncio.wait_for(manager.submitAnswer('u', 's', current['questionId'], ANSWER), timeout=1)
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        assert result['status'] == 'question'
        assert not returned_late_result.is_set()
        published = deepcopy(cached(module))
    finally:
        release_late_result.set()
    await asyncio.wait_for(returned_late_result.wait(), timeout=1)
    await asyncio.sleep(0)
    assert cached(module) == published
    assert len(published['qaHistory']) == 1


@pytest.mark.asyncio
async def test_receipt_save_failure_prevents_model_generation_and_false_success():
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    before = deepcopy(cached(module))
    calls = len(gateway.requests)
    manager._session_cache.save = AsyncMock(return_value=False)
    with pytest.raises(RuntimeError):
        await manager.submitAnswer('u', 's', current['questionId'], ANSWER)
    assert cached(module) == before
    assert len(gateway.requests) == calls
    assert module.collection_sessions.document is None


@pytest.mark.asyncio
async def test_publication_save_failure_retains_receipt_and_recovery_commits_once():
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    before = deepcopy(cached(module))
    save = manager._session_cache.save
    save_count = 0

    async def fail_second_save(*args, **kwargs):
        nonlocal save_count
        save_count += 1
        if save_count == 2:
            return False
        return await save(*args, **kwargs)

    manager._session_cache.save = fail_second_save
    with pytest.raises(RuntimeError):
        await manager.submitAnswer('u', 's', current['questionId'], ANSWER)
    pending = cached(module)
    assert pending['qaFlow']['pendingSubmission']['answer'] == ANSWER
    assert pending['qaHistory'] == before['qaHistory']
    assert pending['currentQuestion'] == before['currentQuestion']
    manager._session_cache.save = save
    await recover_then_query(manager, 'getSession')
    assert_published_once(module, before, current)


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type', [AgentConfigurationError, LLMContextStorageError])
async def test_configuration_and_storage_errors_are_not_reported_as_successful_fallback(error_type):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    before = deepcopy(cached(module))

    async def fail(request):
        raise error_type('synthetic infrastructure failure')

    gateway.complete = fail
    with pytest.raises(error_type):
        await manager.submitAnswer('u', 's', current['questionId'], ANSWER)
    pending = cached(module)
    assert pending['qaFlow']['pendingSubmission']['answer'] == ANSWER
    assert pending['qaHistory'] == before['qaHistory']
    assert pending['currentQuestion'] == before['currentQuestion']


@pytest.mark.asyncio
async def test_expired_receipt_still_requires_session_lease_before_fallback_publication():
    manager, module, gateway, _, _ = await interrupted_submission()
    pending = cached(module)
    pending['qaFlow']['pendingSubmission']['deadlineAt'] = 0.0
    save_cache(module, pending)
    manager._agent_runtime.lease_manager.try_acquire = AsyncMock(return_value=None)
    calls = len(gateway.requests)
    with pytest.raises(module.QASessionConflictError):
        await manager.getSession('u', 's')
    assert cached(module) == pending
    assert len(gateway.requests) == calls


def pending_concern(identity='synthetic-concern'):
    return {
        'id': identity,
        'text': '希望了解饮食方面需要注意什么',
        'evidence': '饮食需要注意什么',
        'sourceQuestionId': 'synthetic-prior-question',
        'originPhase': 'supplement',
        'handledCount': 0,
        'status': 'pending',
        'response': '',
        'informationNeeded': '具体饮食背景',
        'readyToAnswer': False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('choice', [fallback_policy.FINISH_OPTION, fallback_policy.CONTINUE_OPTION])
async def test_two_whole_fallbacks_offer_explicit_choice_without_a_third_model_request(choice):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    snapshot = cached(module)
    snapshot['qaFlow']['concerns'] = [pending_concern()]
    save_cache(module, snapshot)
    gateway.invalid_analysis = 4
    for answer in (ANSWER, '现在还没有其他要补充的信息。'):
        current = await manager.submitAnswer('u', 's', current['questionId'], answer)
    assert current['options'] == [fallback_policy.FINISH_OPTION, fallback_policy.CONTINUE_OPTION]
    assert current['interactionCount'] == 2
    before_choice = deepcopy(cached(module))
    assert before_choice['qaFlow']['generationQuality']['consecutiveFallbacks'] == 2
    assert before_choice['qaFlow']['concerns'][0]['status'] == 'pending'
    calls = len(gateway.requests)
    result = await manager.submitAnswer('u', 's', current['questionId'], choice)
    assert len(gateway.requests) == calls
    assert result['interactionCount'] == 2
    completed = choice == fallback_policy.FINISH_OPTION
    assert result['status'] == ('finished' if completed else 'question')
    saved = module.collection_sessions.document if completed else cached(module)
    assert saved['qaHistory'][:-1] == before_choice['qaHistory']
    assert saved['qaHistory'][-1]['answer'] == choice
    assert saved['qaFlow']['concerns'][0]['status'] == 'pending'
    assert (
        saved['qaFlow']['generationQuality']['unprocessedQuestionIds']
        == (before_choice['qaFlow']['generationQuality']['unprocessedQuestionIds'])
    )
    if completed:
        assert result['assistantMessage'] == fallback_policy.COMPLETION_MESSAGE
    assert await manager.submitAnswer('u', 's', current['questionId'], choice) == result
    assert len(gateway.requests) == calls


@pytest.mark.asyncio
async def test_twentieth_answer_with_failed_analysis_finishes_without_resolving_pending_concerns():
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    snapshot = cached(module)
    snapshot['qaFlow'].update(phase='qa', interactionCount=19, intakeCount=10)
    snapshot['qaFlow'].pop('interviewPolicy', None)
    snapshot['qaFlow']['concerns'] = [pending_concern('pending-one'), pending_concern('pending-two')]
    snapshot['currentQuestion']['phase'] = 'qa'
    snapshot['qaHistory'] = [
        {
            'question': {**deepcopy(snapshot['currentQuestion']), 'questionId': f'old-question-{index}'},
            'answer': f'合成的既往回答 {index}',
            'answeredAt': '2026-01-01T10:00:00',
            'interactionCount': index + 1,
        }
        for index in range(19)
    ]
    snapshot['answers'] = {item['question']['questionId']: item['answer'] for item in snapshot['qaHistory']}
    save_cache(module, snapshot)
    gateway.invalid_analysis = 2
    result = await manager.submitAnswer('u', 's', current['questionId'], ANSWER)
    assert result['status'] == 'finished'
    assert result['interactionCount'] == 20
    assert result['endReason'] == 'interaction_limit'
    saved = module.collection_sessions.document
    assert len(saved['qaHistory']) == 20
    assert saved['qaHistory'][:-1] == snapshot['qaHistory']
    assert saved['qaHistory'][-1]['answer'] == ANSWER
    assert all(item['status'] == 'pending' and item['handledCount'] == 0 for item in saved['qaFlow']['concerns'])
    assert result['assistantMessage'] == fallback_policy.COMPLETION_MESSAGE


@pytest.mark.asyncio
@pytest.mark.parametrize('reasoning_effort', ['high', 'max'])
async def test_analysis_repair_and_expression_share_one_deadline_even_with_explicit_reasoning(reasoning_effort):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    gateway.invalid_analysis = 1
    budget = manager._awaitModel
    deadlines = []

    async def inspect_budget(awaitable, deadline):
        deadlines.append(deadline)
        return await budget(awaitable, deadline)

    manager._awaitModel = inspect_budget
    calls = len(gateway.requests)
    result = await manager.submitAnswer(
        'u', 's', current['questionId'], ANSWER, thinking=True, reasoning_effort=reasoning_effort
    )
    assert result['status'] == 'question'
    assert len(deadlines) == 3
    assert len(set(deadlines)) == 1
    requests = gateway.requests[calls:]
    assert [item.profile.name for item in requests] == ['record.plan', 'record.plan', 'record.question']
    assert requests[0].thinking is True and requests[0].reasoning_effort == reasoning_effort
    assert requests[1].thinking is False
    assert requests[2].thinking is False


@pytest.mark.asyncio
async def test_partial_analysis_does_not_silently_finish_or_lose_unprocessed_marker():
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    content = gateway.content

    def damaged(request):
        value = content(request)
        if request.profile.name != 'record.plan':
            return value
        raw = json.loads(value)
        raw['intent'] = 'stop'
        raw['intake']['facts'].append({'dimension': 'invalid-dimension'})
        return json.dumps(raw, ensure_ascii=False)

    gateway.content = damaged
    result = await manager.submitAnswer('u', 's', current['questionId'], ANSWER)
    assert result['status'] == 'question'
    saved = cached(module)
    quality = saved['qaFlow']['generationQuality']
    assert quality['mode'] == 'partial'
    assert current['questionId'] in quality['unprocessedQuestionIds']
    assert saved['qaHistory'][-1]['answer'] == ANSWER
    assert saved['qaFlow']['endReason'] is None
