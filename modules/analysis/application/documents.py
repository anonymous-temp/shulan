"""Shared document parsing and durable provenance, independent of HTTP and Report."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

from modules.analysis.application.content_results import empty_content_result
from modules.analysis.contracts import DocumentInputSnapshot
from modules.analysis.contracts import OutputSelection
from modules.analysis.ports import DocumentParsingClient
from modules.analysis.ports import DocumentTraceStore
from modules.analysis.ports import LocalDocumentParsingClient
from modules.analysis.preparation import DocumentPreparationBinding


class DocumentParsingError(RuntimeError):
    """The SDK failed or returned no usable document text."""

    def __init__(self, message, *, retryable=True, automatic_retryable=None, reason_code=None):
        super().__init__(message)
        self.retryable = retryable
        self.automatic_retryable = retryable if automatic_retryable is None else automatic_retryable
        self.reason_code = reason_code


class DocumentInterpretationError(DocumentParsingError):
    """Output recovery was exhausted; retain checkpoints for an explicit retry."""

    def __init__(
        self,
        message,
        *,
        retryable=True,
        processing=None,
        document_id=None,
        trace_id=None,
        result=None,
        content_outcome=None,
    ):
        super().__init__(message, retryable=retryable, automatic_retryable=False)
        self.processing = deepcopy(processing)
        self.document_id = document_id
        self.trace_id = trace_id
        self.source_revision = None
        self.result = deepcopy(result)
        self.content_outcome = content_outcome
        self.result_context = {}


class DocumentSubmissionConflictError(RuntimeError):
    """An earlier unconfirmed submission prevents another submission."""

    def __init__(self, message, *, retry_after_seconds=None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def parsed_document_text(result, *, allow_empty=False):
    """Validate the OCR transport contract separately from readable report content."""
    if not isinstance(result, dict) or type(result.get('success')) is not bool:
        raise DocumentParsingError('文档解析服务响应格式异常', retryable=False, reason_code='ocr_response_invalid')
    if not result['success']:
        raise DocumentParsingError('体检报告解析失败', retryable=False, reason_code='ocr_service_failed')
    text = result.get('text')
    if not isinstance(text, str):
        raise DocumentParsingError('文档解析服务未返回有效正文', retryable=False, reason_code='ocr_response_invalid')
    if not text.strip() and not allow_empty:
        outcome = empty_content_result()
        raise DocumentInterpretationError(
            '文档解析服务未返回有效正文',
            retryable=False,
            processing=outcome['processing'],
            result=outcome,
            content_outcome='no_readable_text',
        )
    return text


class DocumentSubmissionRejectedError(DocumentParsingError):
    """Local preparation or an explicit upstream rejection proves no task was accepted."""


class DocumentNotFoundError(LookupError):
    """A reference is absent or inaccessible within the requested owner scope."""


class DocumentBusyError(RuntimeError):
    """A waiter timed out or an execution lost its lease."""


@dataclass(frozen=True, slots=True)
class PreparedDocumentRound:
    """Scope-bound current extraction artifacts read from one immutable archived round."""

    user_id: str
    subject_id: str
    document_id: str
    preparation_binding: DocumentPreparationBinding
    parsed_document: dict[str, Any]
    interpretation: dict[str, Any]
    output_selection: OutputSelection


@dataclass(frozen=True, slots=True)
class ProcessedHealthReport:
    """Internal result. Only ``report`` belongs in the existing public response."""

    report: dict[str, Any]
    text: str | None
    document_id: str | None
    trace_id: str
    ingestion_status: str | None
    ingestion_job_id: str | None
    reused: bool
    archive_trace_id: str | None = None
    file_name: str | None = None
    resolved_file_type: str | None = None
    content_sha256: str | None = None
    canonical_report_id: str | None = None
    structured_data: dict[str, Any] | None = None
    output_profile: str = 'health_report'
    extraction_id: str | None = None
    source_revision: int | None = 1
    documents: tuple[dict[str, Any], ...] = ()
    output_trace_id: str | None = None
    output_reused: bool = False
    input_snapshot: DocumentInputSnapshot | None = None
    output_selection: OutputSelection | None = None
    preparation_binding: DocumentPreparationBinding | None = None
    response_is_projected: bool = False


class DocumentProcessingTrace:
    """One attempt; failures and replays never overwrite an earlier attempt."""

    def __init__(self, store: DocumentTraceStore, *, user_id: str) -> None:
        self.store = store
        self.user_id = user_id
        self.trace_id = f'document_{uuid4().hex}'
        self.stage = 'validation'

    async def start(self, **fields: Any) -> None:
        self._started_at = asyncio.get_running_loop().time()
        now = datetime.now(timezone.utc)
        await self.store.create(
            {
                '_id': self.trace_id,
                'traceId': self.trace_id,
                'userId': self.user_id,
                'schemaVersion': 1,
                'parserVersion': 'docmind-health-document-v1',
                'status': 'running',
                'stage': self.stage,
                'createdAt': now,
                'updatedAt': now,
                'events': [{'stage': self.stage, 'status': 'running', 'at': now}],
                **fields,
            }
        )

    async def update(self, *, stage: str | None = None, **fields: Any) -> None:
        if stage is not None:
            self.stage = stage
        if fields.get('status') in {'completed', 'reused', 'failed', 'cancelled'} and hasattr(self, '_started_at'):
            fields['processingElapsedMs'] = round((asyncio.get_running_loop().time() - self._started_at) * 1000)
        await self.store.update(
            self.trace_id,
            self.user_id,
            {'stage': self.stage, 'updatedAt': datetime.now(timezone.utc), **fields},
        )

    async def write_payload(self, name: str, payload: Any) -> None:
        await self.store.write_payload(self.trace_id, self.user_id, name, payload)


class _PreparedSubmission:
    def __init__(self, call, directory=None):
        self.call, self.directory = call, directory

    def close(self):
        if self.directory is not None:
            self.directory.cleanup()

    def submit(self):
        # The worker owns the file lifetime, even if its async waiter is cancelled.
        try:
            return self.call()
        finally:
            self.close()


def prepare_document(client, *, file_url, filename, file_type, content=None):
    if content is None:
        return _PreparedSubmission(
            partial(client.submit_online_file, file_url, file_name=filename, file_type=file_type)
        )
    if not isinstance(client, LocalDocumentParsingClient):
        raise DocumentSubmissionRejectedError('文档解析客户端不支持已下载文件', retryable=False)
    directory = tempfile.TemporaryDirectory(prefix='shulan-document-')
    try:
        # The filename is never used as a caller-controlled filesystem path.
        path = Path(directory.name) / f'document.{file_type}'
        path.write_bytes(content)
        return _PreparedSubmission(
            partial(client.submit_local_file, str(path), file_name=filename, file_type=file_type), directory
        )
    except BaseException:
        directory.cleanup()
        raise


def submit_document(client: DocumentParsingClient, **kwargs) -> Any:
    """Compatibility path; local preparation and cleanup use the same implementation."""
    return prepare_document(client, **kwargs).submit()


async def parse_document(
    client: DocumentParsingClient,
    *,
    trace: DocumentProcessingTrace,
    file_url: str,
    filename: str,
    file_type: str,
    content: bytes | None = None,
) -> str:
    if content is not None:
        await trace.write_payload('original_file', {'content': content})
    task_id = getattr(trace, 'resume_task_id', None)
    if task_id is None:
        # Prepare the exact bytes before recording the external submission boundary.
        prepared = prepare_document(client, file_url=file_url, filename=filename, file_type=file_type, content=content)
        try:
            await trace.update(stage='submitting', parser='aion.aliyun.docmind')
        except BaseException:
            prepared.close()
            raise
        try:
            task_id = await asyncio.to_thread(prepared.submit)
        except DocumentSubmissionRejectedError:
            raise
        except Exception as error:
            await trace.update(upstreamErrorType=type(error).__name__)
            raise DocumentParsingError('文档解析任务提交失败') from error
    if not isinstance(task_id, str) or not task_id.strip():
        raise DocumentParsingError('文档解析服务未返回任务 ID')
    await trace.update(stage='polling', taskId=task_id)
    try:
        result = await asyncio.to_thread(client.get_result, task_id, poll_interval=1)
    except Exception as error:
        await trace.update(upstreamErrorType=type(error).__name__)
        raise DocumentParsingError('文档解析任务查询失败', reason_code='ocr_query_failed') from error
    # Preserve the entire SDK response, including data/layouts and failed results.
    await trace.write_payload('docmind_result', result)
    trace.parsed_document = result
    text = parsed_document_text(result)
    await trace.update(
        stage='parsed', textLength=len(text), textSha256=hashlib.sha256(text.encode('utf-8')).hexdigest()
    )
    return text
