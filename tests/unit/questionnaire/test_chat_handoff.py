from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.persistence import MongoChatSessionRepository
from tests.unit.services.chat.test_chat_service_agent import FakeCustomMessageOutbox
from tests.unit.services.chat.test_chat_service_agent import FakeSessionCollection
from tests.unit.services.chat.test_chat_service_agent import _build_runtime
from tests.unit.services.chat.test_chat_service_agent import _load_chat_service


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_submitted_questionnaire_to_chat_before_profile_is_ready(stream):
    context = {'questionnaireId': 'q-1', 'answers': [{'question': '疾病史', 'answer': '糖尿病'}], 'healthData': {}}
    provider = SimpleNamespace(assertNoPendingSession=AsyncMock(), get_session_context=AsyncMock(return_value=context))
    collection = FakeSessionCollection(
        {
            'userId': 'test-user',
            'sessionId': 'test-session',
            'type': 'questionnaire',
            'status': 'completed',
            'questionnaireId': 'q-1',
        }
    )
    module = _load_chat_service(collection)
    runtime, graph, _, _ = _build_runtime()
    manager = module.ChatSessionManager(
        runtime,
        session_repository=MongoChatSessionRepository(collection),
        custom_message_outbox=FakeCustomMessageOutbox(),
        questionnaire_service=provider,
    )
    if stream:
        events = [event async for event in manager.sendMessageStream('test-user', 'test-session', '您好')]
        assert events[-1]['data']['type'] == 'finish'
        call = graph.stream_calls[-1]
    else:
        await manager.sendMessage('test-user', 'test-session', '您好')
        call = graph.calls[-1]
    assert collection.document['type'] == 'chat'
    assert collection.document['questionnaireId'] == 'q-1'
    assert '糖尿病' in call['context'].session_health_context
    assert 'qaHistory' not in collection.document
    assert context == deepcopy(context)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['queued', 'generating', 'ready', 'expired', 'failed'])
async def test_pending_questionnaire_blocks_all_chat_entry_points(status):
    provider = SimpleNamespace(
        assertNoPendingSession=AsyncMock(side_effect=ValueError('先提交问卷')),
        get_session_context=AsyncMock(return_value=None),
    )
    collection = FakeSessionCollection(
        {'userId': 'test-user', 'sessionId': 'test-session', 'type': 'questionnaire', 'status': status}
    )
    module = _load_chat_service(collection)
    runtime, graph, _, _ = _build_runtime()
    manager = module.ChatSessionManager(
        runtime,
        session_repository=MongoChatSessionRepository(collection),
        custom_message_outbox=FakeCustomMessageOutbox(),
        questionnaire_service=provider,
    )
    with pytest.raises(ValueError):
        await manager.sendMessage('test-user', 'test-session', '您好')
    events = [event async for event in manager.sendMessageStream('test-user', 'test-session', '您好')]
    assert any(
        event.get('event') == 'error' or event.get('type') == 'error' or event.get('data', {}).get('type') == 'error'
        for event in events
    )
    with pytest.raises(Exception):
        await manager._validateCustomMessageTarget('test-user', 'test-session')
    assert not graph.calls and not graph.stream_calls
    assert collection.replace_calls == []
