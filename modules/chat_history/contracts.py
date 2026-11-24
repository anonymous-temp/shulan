"""Public, provider-independent contracts for external group chat history."""

from __future__ import annotations

from typing import Any
from typing import Protocol


class ChatHistoryQueryError(RuntimeError):
    """Expose a safe failure category without chat text or endpoint details."""

    def __init__(self, category: str, *, http_status: int | None = None, business_code: int | None = None):
        super().__init__(f'chat history query {category}')
        self.category = category
        self.http_status = http_status
        self.business_code = business_code


class ChatHistoryReader(Protocol):
    async def query(self, group_id: str) -> dict[str, Any]:
        """Return one upstream response's chat data without interpreting messages."""
        ...


def require_group_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChatHistoryQueryError('invalid_arguments')
    return value
