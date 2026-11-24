"""Record-owned attachment snapshots; provider result models stay outside this boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from typing import ClassVar


@dataclass(frozen=True)
class RecordDocumentContextV1:
    """Preserve the first Record projection without sharing mutable provider output.

    The checkpoint representation deliberately retains the existing three keys.
    Legacy creation receipts already contain this projection and remain readable.
    """

    report: dict[str, Any] | None
    document: dict[str, Any] | None
    processing: dict[str, Any]
    consumer_projection_revision: ClassVar[str] = 'record-document-context.v1'

    def __post_init__(self) -> None:
        object.__setattr__(self, 'report', deepcopy(self.report))
        object.__setattr__(self, 'document', deepcopy(self.document))
        object.__setattr__(self, 'processing', deepcopy(self.processing))

    def to_checkpoint(self) -> dict[str, Any]:
        return deepcopy({'report': self.report, 'document': self.document, 'processing': self.processing})


def document_processing_summary(
    index: int,
    processing: Any,
    *,
    status: str = 'complete',
    retryable: bool = False,
    document_id: str | None = None,
    source_revision: int | None = None,
) -> dict[str, Any]:
    """Keep Record's public attachment status separate from provider diagnostics."""
    processing = processing if isinstance(processing, dict) else {}
    status = processing.get('status', status)
    messages = {
        'complete': '资料已读取。',
        'partial': '资料已部分读取，仅使用已确认的内容；您可以在问答中补充未读取的信息。',
        'unavailable': '这份资料暂时未能读取，您可以在问答中补充其中的重要内容。',
    }
    if status not in messages:
        raise ValueError('无效的附件处理状态')
    result = {
        'fileIndex': index,
        'status': status,
        'message': messages[status],
        'retryable': bool(processing.get('retryable', retryable)),
    }
    if document_id:
        result['documentId'] = document_id
    if source_revision is not None:
        result['sourceRevision'] = source_revision
    return result
