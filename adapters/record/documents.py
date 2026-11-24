"""Adapt existing document processing to the first Record-owned consumption contract."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Mapping
from typing import Any

from modules.record.document_context import RecordDocumentContextV1
from modules.record.document_context import document_processing_summary
from modules.record.ports import HealthReportProcessor


def legacy_record_report_id(*, user_id: str, file_id: str) -> str:
    """Retain the legacy URL-based retry identity for processors without reuse."""
    encoded = json.dumps(
        {'userId': user_id, 'fileId': file_id},
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return f'qa_report_{hashlib.sha256(encoded).hexdigest()}'


class LegacyRecordDocumentAdapter:
    """Call the existing public processor once and isolate its result shape.

    This bridge does not request a new extractor, publish profile facts, or change
    source identity. The shared processor retains those existing responsibilities.
    """

    def __init__(
        self,
        *,
        processor: HealthReportProcessor | None = None,
        processor_factory: Callable[[], HealthReportProcessor] | None = None,
    ) -> None:
        if processor is not None and not isinstance(processor, HealthReportProcessor):
            raise TypeError('processor must implement HealthReportProcessor')
        self._processor = processor
        self._processor_factory = processor_factory

    def _get_processor(self) -> HealthReportProcessor:
        if self._processor is None:
            if self._processor_factory is None:
                raise RuntimeError('体检报告处理服务未配置')
            processor = self._processor_factory()
            if processor is None or not callable(getattr(processor, 'process', None)):
                raise TypeError('体检报告处理服务必须提供 process 方法')
            self._processor = processor
        return self._processor

    async def process_attachment(
        self,
        *,
        user_id: str,
        session_id: str,
        file_index: int,
        attachment: Mapping[str, Any],
        settled_document_ids: set[str],
        on_document_ready: Callable[[str], Awaitable[None]] | None = None,
    ) -> RecordDocumentContextV1:
        processor = self._get_processor()
        if getattr(processor, 'supports_document_reuse', False):
            recovery_options = {}
            if on_document_ready is not None:
                recovery_options['on_document_ready'] = on_document_ready
            result = await processor.process_document(
                file_id=attachment.get('url'),
                document_id=attachment.get('documentId'),
                file_type=attachment.get('fileType'),
                user_id=user_id,
                source_type='record',
                source_id=session_id,
                session_id=session_id,
                file_index=file_index,
                _settled_document_ids=settled_document_ids,
                **recovery_options,
            )
        else:
            if 'documentId' in attachment:
                raise ValueError('文件引用服务未配置')
            result = await processor.process(
                file_id=attachment['url'],
                file_type=attachment.get('fileType'),
                user_id=user_id,
                source_type='record',
                source_id=session_id,
                session_id=session_id,
                file_index=file_index,
                subject_id=None,
                report_id=legacy_record_report_id(user_id=user_id, file_id=attachment['url']),
            )
        if isinstance(result, dict):
            return RecordDocumentContextV1(
                report=result,
                document=None,
                processing=document_processing_summary(file_index, result.get('processing')),
            )
        return RecordDocumentContextV1(
            report={key: value for key, value in result.report.items() if key != 'documentId'},
            document={
                'fileIndex': file_index,
                'documentId': result.document_id,
                'reportId': result.report['reportId'],
                'reused': result.reused,
            },
            processing=document_processing_summary(
                file_index,
                result.report.get('processing'),
                document_id=result.document_id,
                source_revision=getattr(result, 'source_revision', None),
            ),
        )
