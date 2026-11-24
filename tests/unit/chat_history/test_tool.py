"""The public read tool remains a thin wrapper over its injected query port."""

import pytest

from agents.tools import ToolCallContext
from agents.tools import ToolRunContext
from modules.chat_history import ChatHistoryQueryError
from modules.chat_history import build_chat_history_tool


class Reader:
    def __init__(self):
        self.calls = []
        self.data = {'chatLogs': [], 'chatLogsNum': 0, 'extra': 'synthetic-upstream-value'}

    async def query(self, group_id):
        self.calls.append(group_id)
        return self.data


def context():
    return ToolCallContext(
        ToolRunContext('test-user', 'test-run', 'test-run', 'chat_history'),
        'test-call',
        'test-idempotency-key',
        'query_chat_history',
        'chat-history-query-v1',
    )


@pytest.mark.asyncio
async def test_public_tool_contract_and_unmodified_reader_result():
    reader = Reader()
    tool = build_chat_history_tool(reader)
    assert tool.name == 'query_chat_history'
    assert tool.version == 'chat-history-query-v1'
    assert tool.effect == 'read'
    assert tool.parameters['type'] == 'object'
    assert set(tool.parameters['properties']) == {'groupId'}
    assert tool.parameters['properties']['groupId']['type'] == 'string'
    assert tool.parameters['required'] == ['groupId']
    assert tool.parameters['additionalProperties'] is False
    assert await tool.handler({'groupId': 'test-group'}, context()) is reader.data
    assert reader.calls == ['test-group']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'arguments',
    [
        None,
        [],
        {},
        {'groupId': ''},
        {'groupId': ' \t'},
        {'groupId': True},
        {'groupId': 1},
        {'groupId': 'test-group', 'userId': 'test-user'},
        {'groupId': 'test-group', 'page': 1},
    ],
)
async def test_invalid_or_extra_arguments_are_rejected_before_reading(arguments):
    reader = Reader()
    tool = build_chat_history_tool(reader)
    with pytest.raises(ChatHistoryQueryError) as caught:
        await tool.handler(arguments, context())
    assert caught.value.category == 'invalid_arguments'
    assert reader.calls == []
