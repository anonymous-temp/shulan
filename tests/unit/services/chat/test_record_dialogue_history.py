"""Record dialogue metadata stays internal across the QA-to-Chat boundary."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.unit.services.chat.test_chat_service_agent import _load_chat_service


@pytest.mark.asyncio
@pytest.mark.parametrize('include_ref', [False, True])
async def test_chat_history_preserves_published_record_but_hides_dialogue_reference(include_ref):
    module = _load_chat_service(None)
    manager = object.__new__(module.ChatSessionManager)
    question = {
        'questionId': 'q-1',
        'question': '您平时通常在几点准备睡觉？',
        'questionType': 'radio',
        'options': ['22点前', '22至24点'],
        'assistantMessage': '接着聊聊您晚上的作息。',
        'acknowledgementText': '接着聊聊您晚上的作息。',
        'intakeDimension': 'sleep',
    }
    if include_ref:
        question['dialogueFocusRef'] = 'dialogue_test_reference'
    original = {
        'userId': 'user-1',
        'sessionId': 'session-1',
        'createdAt': '2026-10-23T12:00:00',
        'type': 'qa',
        'status': 'completed',
        'qaHistory': [
            {
                'question': question,
                # Only metadata is removed: an identically named user key is
                # still their original answer, and must survive unchanged.
                'answer': {'content': '22点前', 'dialogueFocusRef': 'user-supplied-text'},
                'answeredAt': '2026-10-23T12:01:00',
            }
        ],
        'qaFlow': {'finalMessage': '本次建档已结束。'},
    }
    original_snapshot = deepcopy(original)
    stored = manager._convertQASessionToChat(original)
    stored_snapshot = deepcopy(stored)

    async def get(user_id, session_id):
        assert (user_id, session_id) == ('user-1', 'session-1')
        return stored

    manager._session_repository = SimpleNamespace(get=get)
    result = await manager.getHistory('user-1', 'session-1')
    expected = deepcopy(original['qaHistory'])
    expected[0]['question'].pop('dialogueFocusRef', None)
    assert result['qaHistory'] == expected
    assert result['qaAssistantMessage'] == '本次建档已结束。'
    assert stored == stored_snapshot
    assert original == original_snapshot
    assert result['qaHistory'] is not stored['qaHistory']
    assert 'dialogue_test_reference' not in manager._buildQASummary(stored)
