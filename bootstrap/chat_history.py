"""Opt-in composition for a chat-history tool that no application path uses yet."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from adapters.chat_history import OpenIMChatHistoryReader
from modules.chat_history import build_chat_history_tool

if TYPE_CHECKING:
    import httpx

    from agents.tools import ToolDefinition


def create_application_chat_history_tool(
    *,
    query_url: str | None = None,
    timeout_seconds: float = 20.0,
    client_factory: Callable[..., httpx.AsyncClient] | None = None,
) -> ToolDefinition:
    """Construct explicitly; callers later supply this tool and its allowed identity."""
    if query_url is None:
        from config import env

        query_url = env.CHAT_HISTORY_QUERY_URL
    reader = OpenIMChatHistoryReader(query_url, timeout_seconds=timeout_seconds, client_factory=client_factory)
    return build_chat_history_tool(reader)
