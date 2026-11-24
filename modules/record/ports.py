"""Outbound ports owned by the Record business module."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Mapping
from typing import Any
from typing import Protocol
from typing import runtime_checkable

from modules.record.document_context import RecordDocumentContextV1
from modules.record.documents import RecordPreparedDocumentV1


@runtime_checkable
class RecordDocumentPort(Protocol):
    """Return Record-owned attachment input without exposing a provider result model."""

    async def process_attachment(
        self,
        *,
        user_id: str,
        session_id: str,
        file_index: int,
        attachment: Mapping[str, Any],
        settled_document_ids: set[str],
        on_document_ready: Callable[[str], Awaitable[None]] | None = None,
    ) -> RecordDocumentContextV1: ...


@runtime_checkable
class RecordPreparedDocumentPort(RecordDocumentPort, Protocol):
    """An explicit opt-in to durable prepare-before-publish consumption."""

    prepared_checkpoint_version: str

    async def process_attachment(
        self,
        *,
        user_id: str,
        session_id: str,
        file_index: int,
        attachment: Mapping[str, Any],
        settled_document_ids: set[str],
        on_document_ready: Callable[[str], Awaitable[None]] | None = None,
        prepared_checkpoint: Mapping[str, Any] | None = None,
        on_prepared: Callable[[RecordPreparedDocumentV1], Awaitable[None]] | None = None,
    ) -> RecordDocumentContextV1: ...


@runtime_checkable
class RecordDocumentConsumerRegistry(Protocol):
    """Select an already accepted document execution revision without I/O."""

    def for_consumer_revision(self, revision: str) -> RecordDocumentPort: ...


@runtime_checkable
class RecordSessionRepository(Protocol):
    """Persist completed Record session snapshots."""

    async def get(self, user_id: str, session_id: str) -> dict | None: ...

    async def save(
        self,
        user_id: str,
        session_id: str,
        session: Mapping[str, Any],
    ) -> bool: ...


@runtime_checkable
class RecordSessionCache(Protocol):
    """Persist active Record sessions with a bounded TTL."""

    async def get(self, user_id: str, session_id: str) -> dict | None: ...

    async def save(
        self,
        user_id: str,
        session_id: str,
        session: Mapping[str, Any],
        *,
        ttl_seconds: int,
    ) -> bool: ...

    async def delete(self, user_id: str, session_id: str) -> bool: ...


@runtime_checkable
class RecordCreationStore(Protocol):
    """Atomically replace temporary creation records and discover recoverable work."""

    async def compare_save(self, user_id, session_id, expected_revision, session, *, ttl_seconds) -> bool: ...

    async def pending(self, *, now, limit) -> list[tuple[str, str]]: ...


@runtime_checkable
class RecordReplyRecoveryStore(Protocol):
    """Discover recoverable replies and fence writes in the active session key."""

    def encode_snapshot(self, snapshot) -> str: ...
    def decode_snapshot(self, value: str) -> dict: ...
    async def pending_replies(self, *, now, limit) -> list[tuple[str, str]]: ...
    async def compare_session(self, user_id, session_id, expected, session, *, ttl_seconds) -> bool: ...
    async def compare_delete(self, user_id, session_id, expected) -> bool: ...


@runtime_checkable
class RecordCompletionStore(Protocol):
    """Publish one immutable QA completion without replacing a later Chat owner."""

    async def publish_once(self, user_id, session_id, session) -> bool: ...


@runtime_checkable
class PromptRenderer(Protocol):
    """Render a named application prompt with explicit variables."""

    def render(self, name: str, **variables: Any) -> str: ...


@runtime_checkable
class RecordProfileSource(Protocol):
    """Publish completed Record snapshots for profile ingestion."""

    def prepare_completed_snapshot(self, session: dict) -> None: ...

    async def ensure_queued(self, *, user_id: str, session_id: str): ...


@runtime_checkable
class HealthReportProcessor(Protocol):
    """Interpret and persist one health-report attachment."""

    async def process(self, **kwargs: Any) -> dict: ...


@runtime_checkable
class HealthProfileMemory(Protocol):
    """Provide bounded read-only health-profile context."""

    async def build_context(self, user_id: str) -> str | None: ...


__all__ = [
    'HealthProfileMemory',
    'HealthReportProcessor',
    'PromptRenderer',
    'RecordDocumentPort',
    'RecordDocumentConsumerRegistry',
    'RecordPreparedDocumentPort',
    'RecordProfileSource',
    'RecordCreationStore',
    'RecordReplyRecoveryStore',
    'RecordCompletionStore',
    'RecordSessionCache',
    'RecordSessionRepository',
]
