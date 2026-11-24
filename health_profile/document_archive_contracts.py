"""Profile-owned read port for archived document material.

Adapters translate a storage provider's public contracts into these values. The
port never captures snapshots, publishes sources, or starts document processing.
"""

from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Protocol


class ProfileDocumentArchiveChangedError(RuntimeError):
    """The fixed archive or source lifecycle no longer matches a cursor."""


class ProfileReportLifecycleConflict(ProfileDocumentArchiveChangedError):
    """The confirmed Profile subject or its fixed lifecycle is no longer current."""


class ProfileDocumentArchiveReadError(RuntimeError):
    """Material is temporarily unreadable, without exposing provider details."""


@dataclass(frozen=True, slots=True)
class ProfileDocumentArchiveDescriptor:
    user_id: str
    subject_id: str
    extraction_id: str | None
    document_id: str
    canonical_report_id: str
    lifecycle_token: str
    document_ids: tuple[str, ...] = ()
    title: str | None = None
    report_time: str | None = None
    processing: dict[str, Any] = field(default_factory=dict)
    source_status: str = 'not_published'
    source_revision: int | None = None
    publication_revision: int | None = None
    archive_status: str = 'ready'
    reason: str | None = None
    interpretation_round: int | None = None
    material_ref: str | None = None
    result_ref: str | None = None
    result_digest: str | None = None
    lifecycle_epoch: int | None = None
    current_source: bool = False
    archive_lifecycle_epoch: int | None = None
    archive_managed_bindings: tuple[tuple[str, int, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ProfileDocumentArchivePage:
    entries: tuple[ProfileDocumentArchiveDescriptor, ...]
    next_after_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProfileArchivedExtraction:
    descriptor: ProfileDocumentArchiveDescriptor
    result: dict[str, Any] | None
    source_observations: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ProfileDocumentSourceState:
    token: str
    legacy_visible: bool
    source_status: str


class ProfileDocumentArchivePort(Protocol):
    async def list_archived(
        self,
        *,
        user_id: str,
        subject_id: str,
        document_id: str | None = None,
        after_id: str | None = None,
        limit: int = 20,
    ) -> ProfileDocumentArchivePage: ...

    async def read_fixed(
        self,
        *,
        user_id: str,
        subject_id: str,
        extraction_id: str,
        expected_lifecycle_token: str | None = None,
        document_id: str | None = None,
    ) -> ProfileArchivedExtraction: ...

    async def read_source_state(
        self,
        *,
        user_id: str,
        subject_id: str,
        source_id: str,
    ) -> ProfileDocumentSourceState: ...
