"""Deprecated import compatibility for the Chat Outbox adapter."""

from adapters.messaging.chat_outbox import (
    ChatCustomMessageOutbox,
    ChatCustomMessageOutboxConflictError,
)

__all__ = [
    'ChatCustomMessageOutbox',
    'ChatCustomMessageOutboxConflictError',
]
