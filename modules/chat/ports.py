"""Outbound ports owned by the Chat business module."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from typing import Protocol
from typing import runtime_checkable


@runtime_checkable
class ChatReportContextProvider(Protocol):
    """按需读取最终报告，作为本轮对话的独立补充分区。

    实现方经 Report 自有的只读查询门面访问，Chat 不直接操作报告集合。
    """

    async def build_context(
            self,
            *,
            user_id: str,
            session_id: str,
            message: str,
            final_report_id: str | None = None,
            report_type: str | None = None,
            session_reference: Mapping[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any] | None] | None:
        ...


@runtime_checkable
class ChatSessionRepository(Protocol):
    """Persist business chat and converted QA sessions."""

    async def get(self, user_id: str, session_id: str) -> dict | None:
        ...

    async def save(
            self,
            user_id: str,
            session_id: str,
            session: Mapping[str, Any],
            *,
            upsert: bool,
    ) -> bool:
        ...

    async def list_session_ids(self, user_id: str) -> list[str]:
        ...


@runtime_checkable
class ChatCustomMessageOutbox(Protocol):
    """Durable idempotent queue used for custom Chat messages."""

    async def enqueue(
            self,
            *,
            user_id: str,
            session_id: str,
            event_id: str,
            message: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        ...

    async def get(
            self,
            user_id: str,
            session_id: str,
            event_id: str,
    ) -> dict[str, Any] | None:
        ...

    async def list_pending_sessions(
            self,
            *,
            limit: int = 100,
    ) -> list[tuple[str, str]]:
        ...

    async def list_pending(
            self,
            user_id: str,
            session_id: str,
    ) -> list[dict[str, Any]]:
        ...

    async def record_attempt(self, event: Mapping[str, Any]) -> None:
        ...

    async def mark_delivered(
            self,
            event: Mapping[str, Any],
            message: Mapping[str, Any],
    ) -> None:
        ...

    async def mark_failed(
            self,
            event: Mapping[str, Any],
            error: str,
    ) -> None:
        ...


@runtime_checkable
class QASessionGate(Protocol):
    """Prevent Chat from taking over an active Record session."""

    async def assertNoPendingSession(
            self,
            user_id: str,
            session_id: str,
    ) -> None:
        ...

    async def acquireSessionLease(self, user_id: str, session_id: str):
        ...

    async def releaseSessionLease(self, lease_id: str, lease) -> None:
        ...


@runtime_checkable
class ChatProfileSource(Protocol):
    """Publish persisted user messages for health-profile ingestion."""

    def prepare_user_message(self, session: dict, message: dict) -> None:
        ...

    async def ensure_queued(self, *, user_id: str, message_id: str):
        ...


@runtime_checkable
class HealthProfileMemory(Protocol):
    """Provide bounded read-only health-profile context."""

    async def build_context(self, user_id: str) -> str | None:
        ...


__all__ = [
    'ChatProfileSource',
    'ChatCustomMessageOutbox',
    'ChatSessionRepository',
    'HealthProfileMemory',
    'QASessionGate',
]
