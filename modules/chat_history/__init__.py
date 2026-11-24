"""Shared external chat query contracts and an opt-in Agent tool factory."""

from .contracts import ChatHistoryQueryError
from .contracts import ChatHistoryReader
from .tool import CHAT_HISTORY_TOOL_NAME
from .tool import CHAT_HISTORY_TOOL_VERSION
from .tool import build_chat_history_tool

__all__ = [
    'CHAT_HISTORY_TOOL_NAME',
    'CHAT_HISTORY_TOOL_VERSION',
    'ChatHistoryQueryError',
    'ChatHistoryReader',
    'build_chat_history_tool',
]
