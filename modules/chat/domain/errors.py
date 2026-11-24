"""Stable Chat domain errors shared by ports and adapters."""


class ChatOutboxConflictError(ValueError):
    """An idempotency key already belongs to different message content."""


__all__ = ['ChatOutboxConflictError']
