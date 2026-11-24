"""An explicitly constructed read tool; importing it enables no business path."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from .contracts import ChatHistoryQueryError
from .contracts import ChatHistoryReader
from .contracts import require_group_id

if TYPE_CHECKING:
    from agents.tools import ToolCallContext
    from agents.tools import ToolDefinition

CHAT_HISTORY_TOOL_NAME = 'query_chat_history'
CHAT_HISTORY_TOOL_VERSION = 'chat-history-query-v1'


def build_chat_history_tool(reader: ChatHistoryReader) -> ToolDefinition:
    """Wrap an injected reader; the caller owns tool and group authorization."""
    from agents.tools import ToolDefinition

    if not callable(getattr(reader, 'query', None)):
        raise TypeError('reader must implement ChatHistoryReader.query')

    async def query(arguments: Mapping, context: ToolCallContext):
        if not isinstance(arguments, Mapping) or set(arguments) != {'groupId'}:
            raise ChatHistoryQueryError('invalid_arguments')
        return await reader.query(require_group_id(arguments['groupId']))

    return ToolDefinition(
        name=CHAT_HISTORY_TOOL_NAME,
        version=CHAT_HISTORY_TOOL_VERSION,
        description=(
            'Query one upstream response of group chat history for an authorized groupId. '
            'Return original chatLogs and chatLogsNum, including revocation flags and unknown message types; '
            'the result is not guaranteed to contain all history.'
        ),
        parameters={
            'type': 'object',
            'properties': {'groupId': {'type': 'string', 'description': 'Nonempty group identifier'}},
            'required': ['groupId'],
            'additionalProperties': False,
        },
        handler=query,
        effect='read',
    )
