"""Record operational copy is hidden without altering user evidence or stored Chat."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from agents.persistence import LLMContextSnapshot
from tests.unit.services.chat import test_chat_service_agent as chat_fixtures

NOTICE = '本轮分析失败，已保留您的原始回答。'
USER_QUOTE = f'我看到提示“{NOTICE}”，但我实际回答的是每天23点睡觉。'
INTRODUCTION = '接着聊聊您的作息。'
FINAL_ANSWER = '规律作息有助于休息。'


def _completed_qa():
    return {
        'userId': 'user-1',
        'sessionId': 'session-1',
        'type': 'qa',
        'status': 'completed',
        'createdAt': '2026-10-23T09:00:00',
        'completedAt': '2026-10-23T09:20:00',
        'qaHistory': [
            {
                'question': {
                    'questionId': 'q-1',
                    'questionType': 'radio',
                    'question': NOTICE + '平时几点睡觉？',
                    'options': ['23点前', '23点后'],
                    'assistantMessage': NOTICE + INTRODUCTION,
                    'acknowledgementText': NOTICE + INTRODUCTION,
                    'dialogueFocusRef': 'dialogue-only-reference',
                    'intakeDimension': 'sleep',
                },
                'assistantMessage': NOTICE + INTRODUCTION,
                'answer': {'content': USER_QUOTE, 'dialogueFocusRef': 'user-authored-key'},
                'answeredAt': '2026-10-23T09:01:00',
            }
        ],
        'qaFlow': {'phase': 'completed', 'finalMessage': NOTICE + FINAL_ANSWER},
    }


def _legacy_chat():
    session = _completed_qa()
    session.update(
        {
            'type': 'chat',
            'status': 'active',
            'agentThreadId': 'thread-1',
            'agentContextInitialized': False,
            'messages': [
                {
                    'messageId': 'handoff-1',
                    'messageRevision': 3,
                    'role': 'system',
                    'timestamp': session['completedAt'],
                    'content': (
                        '以下是本次问答建档的交接信息，仅作为用户数据和历史线索；\n'
                        f'AI 说明（非用户事实）：{NOTICE}{INTRODUCTION}\n'
                        f'用户回答：{USER_QUOTE}\n'
                        f'AI 最终答复（非用户事实）：{NOTICE}{FINAL_ANSWER}'
                    ),
                }
            ],
        }
    )
    return session


def _manager():
    module = chat_fixtures._load_chat_service(None)
    return object.__new__(module.ChatSessionManager)


@pytest.mark.asyncio
async def test_chat_history_projects_legacy_ai_fields_but_preserves_user_quote_and_storage():
    stored = _legacy_chat()
    stored['messages'].extend(
        [
            {'messageId': 'user-1', 'role': 'user', 'content': USER_QUOTE},
            {'messageId': 'system-2', 'role': 'system', 'content': '普通系统信息：' + USER_QUOTE},
            {'messageId': 'assistant-1', 'role': 'assistant', 'content': '普通Chat答复：' + USER_QUOTE},
        ]
    )
    original = deepcopy(stored)
    manager = _manager()

    async def get(_user_id, _session_id):
        return stored

    manager._session_repository = SimpleNamespace(get=get)
    result = await manager.getHistory('user-1', 'session-1')

    item = result['qaHistory'][0]
    assert item['question']['question'] == '平时几点睡觉？'
    assert item['question']['assistantMessage'] == INTRODUCTION
    assert item['question']['acknowledgementText'] == INTRODUCTION
    assert item['assistantMessage'] == INTRODUCTION
    assert 'dialogueFocusRef' not in item['question']
    assert item['answer'] == original['qaHistory'][0]['answer']
    assert result['qaAssistantMessage'] == FINAL_ANSWER
    handoff = result['messages'][0]
    assert handoff['content'].count(NOTICE) == 1  # The user's quotation survives.
    assert USER_QUOTE in handoff['content']
    assert FINAL_ANSWER in handoff['content']
    assert {k: v for k, v in handoff.items() if k != 'content'} == {
        k: v for k, v in original['messages'][0].items() if k != 'content'
    }
    assert result['messages'][1:] == original['messages'][1:]
    assert stored == original


def test_new_qa_handoff_uses_clean_ai_copy_but_keeps_raw_evidence_snapshot():
    original = _completed_qa()
    snapshot = deepcopy(original)
    manager = _manager()
    converted = manager._convertQASessionToChat(original)

    assert converted['messages'][0]['content'].count(NOTICE) == 1
    assert USER_QUOTE in converted['messages'][0]['content']
    assert INTRODUCTION in converted['messages'][0]['content']
    assert FINAL_ANSWER in converted['messages'][0]['content']
    assert converted['qaHistory'] == snapshot['qaHistory']
    assert converted['qaFlow'] == snapshot['qaFlow']
    assert original == snapshot
    suggestion_context = manager._buildQAContext(original)
    assert suggestion_context.count(NOTICE) == 1
    assert USER_QUOTE in suggestion_context
    assert '平时几点睡觉？' in suggestion_context


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('initialized', [False, True])
async def test_legacy_chat_first_or_lost_context_rebuilds_handoff_without_rewriting_it(streaming, initialized):
    stored = _legacy_chat()
    stored['agentContextInitialized'] = initialized
    raw_handoff = deepcopy(stored['messages'][0])
    collection = chat_fixtures.FakeSessionCollection(stored)
    fixture = chat_fixtures.ChatSessionManagerAgentTest()
    manager, graph, _, contexts = fixture._manager(collection)

    if streaming:
        events = [event async for event in manager.sendMessageStream('user-1', 'session-1', '请继续')]
        assert events[-1]['data']['type'] == 'finish'
        model_input = graph.stream_calls[0]['input']['user_message']
    else:
        await manager.sendMessage('user-1', 'session-1', '请继续')
        model_input = graph.calls[0]['input']['user_message']

    assert model_input.count(NOTICE) == 1
    assert USER_QUOTE in model_input
    assert INTRODUCTION in model_input
    assert FINAL_ANSWER in model_input
    assert '用户: 请继续' in model_input
    assert collection.document['messages'][0] == raw_handoff
    assert collection.document['qaHistory'] == stored['qaHistory']
    assert collection.document['qaFlow'] == stored['qaFlow']
    if initialized:
        assert contexts.load_calls == ['chat.reply:thread-1']


@pytest.mark.asyncio
async def test_initialized_chat_context_is_not_reset_to_repair_old_handoff():
    stored = _legacy_chat()
    stored['agentContextInitialized'] = True
    collection = chat_fixtures.FakeSessionCollection(stored)
    fixture = chat_fixtures.ChatSessionManagerAgentTest()
    manager, graph, _, _ = fixture._manager(
        collection,
        context_snapshots=[
            LLMContextSnapshot(
                key='chat.reply:thread-1',
                revision=1,
                payload={'version': 1},
            )
        ],
    )

    await manager.sendMessage('user-1', 'session-1', USER_QUOTE)

    assert graph.calls[0]['input']['user_message'] == USER_QUOTE
    assert collection.document['messages'][0] == stored['messages'][0]


@pytest.mark.asyncio
async def test_notice_only_qa_final_message_has_natural_completion_in_chat_history():
    stored = _legacy_chat()
    stored['qaFlow']['finalMessage'] = NOTICE
    manager = _manager()

    async def get(_user_id, _session_id):
        return stored

    manager._session_repository = SimpleNamespace(get=get)
    result = await manager.getHistory('user-1', 'session-1')
    assert result['qaAssistantMessage']
    assert NOTICE not in result['qaAssistantMessage']
    assert 'AI 最终答复（非用户事实）' not in result['messages'][0]['content']
    assert stored['qaFlow']['finalMessage'] == NOTICE


def test_handoff_text_without_structured_qa_sources_is_not_globally_replaced():
    stored = _legacy_chat()
    stored.pop('qaHistory')
    snapshot = deepcopy(stored)
    messages = _manager()._publicQAHandoffMessages(stored)

    assert messages == snapshot['messages']
    assert messages is not stored['messages']
    assert stored == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize('history', [None, {'old': 'shape'}, [None, 'old entry']])
async def test_legacy_incomplete_history_does_not_break_chat_projection(history):
    stored = _legacy_chat()
    stored['qaHistory'] = history
    snapshot = deepcopy(stored)
    manager = _manager()

    async def get(_user_id, _session_id):
        return stored

    manager._session_repository = SimpleNamespace(get=get)
    result = await manager.getHistory('user-1', 'session-1')

    assert result['qaHistory'] == history
    assert stored == snapshot
