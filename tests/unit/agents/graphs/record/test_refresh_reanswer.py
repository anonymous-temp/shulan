"""Fallback replies publish once; unwritten answer receipts remain retryable."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.errors import AionGatewayError

from .test_dynamic_record import assessment
from .test_dynamic_record import cached
from .test_dynamic_record import setup_manager
from .test_dynamic_record import submit
from .test_record_agent import FakeHealthProfileMemoryProvider
from .test_record_agent import FakeHealthReportProcessingService

FAILED_ANSWER = '偶尔有不适，还没能确定诱因。'
CHANGED_ANSWER = '这周没有再出现不适。'
CONTEXT_FIELDS = (
    'parsedHealthReports',
    'healthProfileContext',
    'healthData',
    'documents',
    'documentProcessing',
    'extractedInfo',
    'extractedInfoSources',
)


async def ready_session(*, streaming):
    reports = FakeHealthReportProcessingService()
    process = reports.process

    async def process_with_reference(**kwargs):
        return SimpleNamespace(
            report=await process(**kwargs), document_id='synthetic-document', reused=False, source_revision=1
        )

    reports.process = process_with_reference
    memory = FakeHealthProfileMemoryProvider('既往用户确认：每周有三次步行锻炼。')
    manager, module, gateway, _ = setup_manager(
        health_report_processing_service=reports,
        health_profile_memory_provider=memory,
    )
    current = await manager.createSession(
        'u',
        's',
        file_list=[{'url': 'https://unit-test.invalid/synthetic-report.pdf'}],
        we_run_data=[{'date': '2026-01-01', 'steps': 6000}],
        basic_info={'age': 38},
    )
    gateway.next_assessment = assessment(sufficient=False)
    for answer in ('主要关注餐后的腹胀。', '饭后会觉得腹部有点胀。'):
        current = await submit(manager, current, answer, streaming=streaming)
    before = deepcopy(cached(module))
    assert len(before['qaHistory']) == 2
    assert before['qaFlow']['intake']['facts']
    assert before['qaFlow']['focus']
    assert before['parsedHealthReports']
    assert before['healthProfileContext']
    assert before['documents']
    assert current['phase'] == 'intake'
    current = await manager.getSession('u', 's')
    history = await manager.getHistory('u', 's')
    manager._session_cache.save = AsyncMock(wraps=manager._session_cache.save)
    manager._session_cache.delete = AsyncMock(wraps=manager._session_cache.delete)
    manager._session_repository.save = AsyncMock(wraps=manager._session_repository.save)
    return manager, module, gateway, reports, memory, current, history, before


async def assert_failed_submission(manager, current, *, streaming, error_type, error_code):
    if not streaming:
        with pytest.raises(error_type):
            await submit(manager, current, FAILED_ANSWER, thinking=True)
        return
    events = [
        event
        async for event in manager.submitAnswerStream('u', 's', current['questionId'], FAILED_ANSWER, thinking=True)
    ]
    assert events[0]['data']['type'] == 'start'
    assert events[-1]['data']['type'] == 'error'
    assert events[-1]['code'] == error_code
    assert not any(event['data']['type'] in ('question', 'finish') for event in events)


async def assert_refresh_is_read_only(manager, module, gateway, reports, memory, current, history, before):
    assert cached(module) == before
    assert module.collection_sessions.document is None
    calls = len(gateway.requests)
    report_calls, memory_calls = len(reports.calls), len(memory.calls)
    writers = (manager._session_cache.save, manager._session_cache.delete, manager._session_repository.save)
    write_counts = [writer.await_count for writer in writers]
    restored = await manager.getSession('u', 's')
    restored_history = await manager.getHistory('u', 's')
    assert restored == current
    assert restored_history == history
    assert restored['questionId'] == before['currentQuestion']['questionId']
    assert restored['interactionCount'] == before['qaFlow']['interactionCount'] == 2
    assert [item['answer'] for item in restored_history['history']] == [item['answer'] for item in before['qaHistory']]
    assert cached(module) == before
    assert [writer.await_count for writer in writers] == write_counts
    assert len(gateway.requests) == calls
    assert len(reports.calls) == report_calls
    assert len(memory.calls) == memory_calls
    return restored


async def assert_reanswer_commits_once(manager, module, gateway, current, before, *, answer, streaming):
    calls = len(gateway.requests)
    result = await submit(manager, current, answer, streaming=streaming)
    assert result['status'] == 'question'
    assert result['questionId'] != current['questionId']
    assert result['interactionCount'] == 3
    assert len(gateway.requests) - calls == 2
    saved = deepcopy(cached(module))
    assert saved['qaHistory'][:-1] == before['qaHistory']
    assert saved['qaHistory'][-1]['question'] == before['currentQuestion']
    assert saved['qaHistory'][-1]['answer'] == answer
    assert len(saved['qaHistory']) == 3
    assert saved['qaFlow']['intakeCount'] == before['qaFlow']['intakeCount'] + 1
    assert saved['qaFlow']['lastSubmission']['questionId'] == current['questionId']
    assert {key: saved[key] for key in CONTEXT_FIELDS} == {key: before[key] for key in CONTEXT_FIELDS}
    calls = len(gateway.requests)
    write_count = manager._session_cache.save.await_count
    replayed = await submit(manager, current, answer, streaming=streaming)
    assert replayed == result
    assert len(gateway.requests) == calls
    assert manager._session_cache.save.await_count == write_count
    assert cached(module) == saved
    refreshed = await manager.getSession('u', 's')
    assert refreshed['questionId'] == result['questionId']
    history = await manager.getHistory('u', 's')
    assert len(history['history']) == 3
    assert history['history'][-1]['answer'] == answer


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('stage', ['record.plan', 'record.question'], ids=['analysis', 'presentation'])
@pytest.mark.parametrize('failure', ['gateway', 'invalid_json'])
@pytest.mark.parametrize('answer', [FAILED_ANSWER, CHANGED_ANSWER], ids=['same-answer', 'changed-answer'])
async def test_refresh_after_model_failure_preserves_answer_and_replays_fallback(streaming, stage, failure, answer):
    manager, module, gateway, reports, memory, current, history, before = await ready_session(streaming=streaming)
    original = gateway.content

    def damaged(request):
        content = original(request)
        if request.profile.name != stage:
            return content
        if failure == 'invalid_json':
            return '{"unfinished":'
        try:
            raise TimeoutError('synthetic upstream failure')
        except TimeoutError as error:
            raise AionGatewayError('synthetic gateway failure') from error

    gateway.content = damaged
    calls = len(gateway.requests)
    result = await submit(manager, current, FAILED_ANSWER, streaming=streaming, thinking=True)
    attempts = 1 if failure == 'gateway' else 2
    assert len(gateway.requests) - calls == attempts + (stage == 'record.question')
    assert result['status'] == 'question'
    assert result['questionId'] != current['questionId']
    assert result['interactionCount'] == 3
    saved = deepcopy(cached(module))
    assert saved['qaHistory'][:-1] == before['qaHistory']
    assert saved['qaHistory'][-1]['answer'] == FAILED_ANSWER
    assert len(saved['qaHistory']) == 3
    assert {key: saved[key] for key in CONTEXT_FIELDS} == {key: before[key] for key in CONTEXT_FIELDS}
    assert saved['qaFlow']['generationQuality']['mode'] in ('fallback', 'partial')
    assert not saved['qaFlow'].get('pendingSubmission')
    assert manager._session_cache.save.await_count == 2
    manager._session_repository.save.assert_not_awaited()
    calls = len(gateway.requests)
    writes = manager._session_cache.save.await_count
    assert await manager.getSession('u', 's') == {key: value for key, value in result.items() if key != 'type'}
    refreshed_history = await manager.getHistory('u', 's')
    assert [item['answer'] for item in refreshed_history['history']] == [item['answer'] for item in saved['qaHistory']]
    if answer == FAILED_ANSWER:
        replayed = await submit(manager, current, answer, streaming=streaming)
        assert replayed == result
    elif streaming:
        events = [event async for event in manager.submitAnswerStream('u', 's', current['questionId'], answer)]
        assert events[-1]['data']['type'] == 'error'
        assert events[-1]['code'] == 409
    else:
        with pytest.raises(module.QASessionConflictError):
            await submit(manager, current, answer)
    assert len(gateway.requests) == calls
    assert manager._session_cache.save.await_count == writes
    assert cached(module) == saved
    gateway.content = original
    continued = await submit(manager, result, CHANGED_ANSWER, streaming=streaming)
    assert continued['interactionCount'] == 4
    assert cached(module)['qaHistory'][:-1] == saved['qaHistory']
    assert cached(module)['qaHistory'][-1]['answer'] == CHANGED_ANSWER


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('failure', ['false', 'exception'])
@pytest.mark.parametrize('answer', [FAILED_ANSWER, CHANGED_ANSWER], ids=['same-answer', 'changed-answer'])
async def test_refresh_after_unwritten_save_failure_allows_reanswer(streaming, failure, answer):
    manager, module, gateway, reports, memory, current, history, before = await ready_session(streaming=streaming)
    save = manager._session_cache.save
    manager._session_cache.save = AsyncMock(
        return_value=False,
        side_effect=ConnectionError('synthetic cache failure') if failure == 'exception' else None,
    )
    await assert_failed_submission(manager, current, streaming=streaming, error_type=RuntimeError, error_code=500)
    manager._session_cache.save.assert_awaited_once()
    manager._session_repository.save.assert_not_awaited()
    current = await assert_refresh_is_read_only(manager, module, gateway, reports, memory, current, history, before)
    manager._session_cache.save = save
    await assert_reanswer_commits_once(manager, module, gateway, current, before, answer=answer, streaming=streaming)
