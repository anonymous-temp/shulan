"""完整与 SSE 健康引导都能逐轮读取建档资料，并隔离展示卡片。"""

import pytest

from adapters.persistence import MongoChatSessionRepository
from agents.persistence import LLMContextSnapshot
from tests.unit.services.chat.test_chat_service_agent import FakeCustomMessageOutbox
from tests.unit.services.chat.test_chat_service_agent import FakeHealthProfileMemoryProvider
from tests.unit.services.chat.test_chat_service_agent import FakeSessionCollection
from tests.unit.services.chat.test_chat_service_agent import _build_runtime
from tests.unit.services.chat.test_chat_service_agent import _load_chat_service


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('profile_failure', [False, True])
async def test_greeting_uses_session_evidence_even_after_context_rebuild_window(stream, profile_failure):
    collection = FakeSessionCollection(
        {
            'userId': 'test-user',
            'sessionId': 'test-session',
            'type': 'chat',
            'agentThreadId': 'test-thread',
            'agentContextInitialized': True,
            'qaCompletedAt': '2026-08-18T10:00:00',
            'parsedHealthReports': [
                {
                    'title': '测试体检报告',
                    'reportTime': '2026-08-18',
                    'status': 'abnormal',
                    'aiSummary': '睡眠评估需随访',
                    'fileId': 'https://example.test/private.pdf',
                }
            ],
            'qaHistory': [
                {
                    'question': {'question': '近期睡眠如何？'},
                    'answer': '经常熬夜',
                }
            ],
            'messages': [{'role': 'assistant', 'content': '历史对话'} for _ in range(25)]
            + [
                {
                    'role': 'custom',
                    'content': '私有医生卡片内容',
                    'payload': {'reportId': 'private-final-report'},
                    'aiContextMode': 'none',
                }
            ],
        }
    )
    module = _load_chat_service(collection)
    runtime, graph, _, _ = _build_runtime(
        context_snapshots=[
            LLMContextSnapshot(key='chat.reply:test-thread', revision=1, payload={}),
        ]
    )
    memory = FakeHealthProfileMemoryProvider(
        '已确认的长期画像',
        error=RuntimeError('test unavailable') if profile_failure else None,
    )
    manager = module.ChatSessionManager(
        runtime,
        session_repository=MongoChatSessionRepository(collection),
        custom_message_outbox=FakeCustomMessageOutbox(),
        health_profile_memory_provider=memory,
    )
    for index in range(2):
        if index:
            collection.document['parsedHealthReports'][0]['aiSummary'] = '更新后的随访线索'
        if stream:
            result = [event async for event in manager.sendMessageStream('test-user', 'test-session', '你好')]
            assert result[-1]['data']['type'] == 'finish'
            call = graph.stream_calls[-1]
        else:
            result = await manager.sendMessage('test-user', 'test-session', '你好')
            call = graph.calls[-1]
        context = call['context'].session_health_context
        assert '测试体检报告' in context
        assert '2026-08-18' in context
        assert '经常熬夜' in context
        if index:
            assert '更新后的随访线索' in context
        assert call['input']['user_message'] == '你好'
        assert 'session_health_context' not in call['input']
        assert 'private.pdf' not in context
        assert '私有医生卡片' not in context
        assert 'private-final-report' not in context
        assert context not in str(result)
        assert context not in str(collection.document['messages'])
        assert call['context'].health_profile_context == (None if profile_failure else '已确认的长期画像')
    assert memory.calls == ['test-user', 'test-user']


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_new_user_has_no_fabricated_health_context(stream):
    collection = FakeSessionCollection()
    module = _load_chat_service(collection)
    runtime, graph, _, _ = _build_runtime()
    manager = module.ChatSessionManager(
        runtime,
        session_repository=MongoChatSessionRepository(collection),
        custom_message_outbox=FakeCustomMessageOutbox(),
    )
    if stream:
        _ = [event async for event in manager.sendMessageStream('test-new', 'test-session', '你好')]
        call = graph.stream_calls[0]
    else:
        await manager.sendMessage('test-new', 'test-session', '你好')
        call = graph.calls[0]
    assert call['context'].session_health_context is None
    assert call['context'].health_profile_context is None
    assert call['input']['user_message'] == '你好'
