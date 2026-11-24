"""Outbound ports owned by the Analysis business module."""

from __future__ import annotations

from typing import Any
from typing import Protocol
from typing import runtime_checkable


class InputTokenCounter(Protocol):
    """Estimate how many model tokens a string uses."""

    def __call__(self, text: str, /) -> int: ...


@runtime_checkable
class DocumentParsingClient(Protocol):
    """Submit a remote document and read the parsed text result."""

    def submit_online_file(
        self,
        file_url: str,
        *,
        file_name: str | None = None,
        file_type: str | None = None,
    ) -> Any: ...

    def get_result(
        self,
        task_id: Any,
        *,
        poll_interval: float,
    ) -> dict[str, Any]: ...


@runtime_checkable
class LocalDocumentParsingClient(Protocol):
    """Submit the exact downloaded bytes through a temporary local file."""

    def submit_local_file(
        self,
        file_path: str,
        *,
        file_name: str | None = None,
        file_type: str | None = None,
    ) -> Any: ...


@runtime_checkable
class DocumentTraceStore(Protocol):
    """Durable document attempts and lossless payloads, scoped to a user."""

    async def create(self, document: dict[str, Any]) -> None: ...

    async def update(self, trace_id: str, user_id: str, fields: dict[str, Any]) -> None: ...

    async def write_payload(self, trace_id: str, user_id: str, name: str, payload: Any) -> None: ...

    async def read_payload(self, trace_id: str, user_id: str, subject_id: str, name: str) -> Any: ...


@runtime_checkable
class DocumentDownloader(Protocol):
    async def download(self, file_url: str) -> bytes: ...


@runtime_checkable
class DocumentIndexStore(Protocol):
    """Shared identity and fenced execution; all reads are owner scoped."""

    async def resolve(self, document: dict[str, Any]) -> dict[str, Any]: ...

    async def get(self, document_id: str, user_id: str, subject_id: str) -> dict[str, Any] | None: ...

    async def find_legacy_outputs(
        self, *, user_id: str, subject_id: str, identity: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Output indexes return up to two owner-scoped, exact legacy matches."""
        ...

    async def bind_report_id(self, document: dict[str, Any], report_id: str) -> None:
        """Reserve an immutable report ID for this file without publishing clinical data."""
        ...

    async def claim(self, document: dict[str, Any], token: str, now: Any, expires: Any) -> dict[str, Any] | None: ...

    async def claim_resubmission(
        self, document: dict[str, Any], token: str, now: Any, expires: Any
    ) -> dict[str, Any] | None: ...

    async def update_owned(self, document: dict[str, Any], token: str, now: Any, fields: dict[str, Any]) -> None: ...


class UnsupportedImageFormatError(ValueError):
    """The remote bytes are not one of the supported image formats."""


class ImageProbeError(RuntimeError):
    """The remote image could not be inspected within the resource limits."""


@runtime_checkable
class ImageFormatProbe(Protocol):
    """Resolve the concrete format of an extensionless image URL."""

    async def detect(self, file_url: str) -> str: ...


__all__ = [
    'DocumentDownloader',
    'DocumentIndexStore',
    'DocumentParsingClient',
    'DocumentTraceStore',
    'ImageFormatProbe',
    'ImageProbeError',
    'LocalDocumentParsingClient',
    'UnsupportedImageFormatError',
]
