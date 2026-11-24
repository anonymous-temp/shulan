"""Content-addressed processing, resumable checkpoints, and per-call provenance."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import suppress
from copy import deepcopy
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

from modules.analysis.analysis_formal_contracts import ANALYSIS_CURRENT_PUBLICATION
from modules.analysis.analysis_formal_contracts import ANALYSIS_FORMAL_REVISION
from modules.analysis.application.basic_info import normalize_basic_info_times
from modules.analysis.application.content_results import classify_outcome
from modules.analysis.application.documents import DocumentBusyError
from modules.analysis.application.documents import DocumentInterpretationError
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.application.documents import DocumentParsingError
from modules.analysis.application.documents import DocumentProcessingTrace
from modules.analysis.application.documents import DocumentSubmissionConflictError
from modules.analysis.application.documents import DocumentSubmissionRejectedError
from modules.analysis.application.documents import ProcessedHealthReport
from modules.analysis.application.documents import parsed_document_text
from modules.analysis.application.legacy_publication import _inherit_published_bindings as _inherit_published_bindings
from modules.analysis.contracts import DocumentInputSnapshot
from modules.analysis.material_binding import failure_material_binding
from modules.analysis.ports import DocumentDownloader
from modules.analysis.ports import DocumentIndexStore
from modules.analysis.preparation import DocumentPreparationBinding
from modules.analysis.preparation import payload_digest
from modules.analysis.snapshots import DocumentScope
from utils.exceptions import format_exception_summary
from utils.log import getLogger
from utils.report_content import report_source_content

logger = getLogger(__name__)


class DocumentReuseCoordinator:
    LEASE_SECONDS = 120
    HEARTBEAT_SECONDS = 30
    WAIT_SECONDS = 300
    POLL_SECONDS = 0.25
    UNKNOWN_COOLDOWN_SECONDS = 300

    def __init__(self, service, index, downloader, *, task_spawner=None):
        if not isinstance(index, DocumentIndexStore) or not isinstance(downloader, DocumentDownloader):
            raise TypeError('document reuse requires an index and a downloader')
        self.service, self.index, self.downloader = service, index, downloader
        self.task_spawner = task_spawner or asyncio.create_task
        self.tasks = set()

    @staticmethod
    def now():
        return datetime.now(timezone.utc)

    async def _subject(self, user_id, subject_id):
        if subject_id is not None:
            return subject_id
        bootstrap = await self.service._health_profile_service.ensure_self_profile(user_id)
        return bootstrap.subject.subject_id

    async def read_payload(self, document_id, user_id, subject_id, name):
        from .service import _optional_processing_text
        from .service import _required_processing_text

        document_id = _required_processing_text('documentId', document_id, missing_message='缺少 documentId')
        user_id = _required_processing_text('userId', user_id, missing_message='缺少 userId')
        subject_id = _optional_processing_text('subjectId', subject_id)
        subject_id = await self._subject(user_id, subject_id)
        document = await self.index.get(document_id, user_id, subject_id)
        if document is None:
            raise DocumentNotFoundError('文件引用不存在或不可访问')
        return await self._payload(document, name)

    async def _payload(self, document, name):
        trace_id = document.get('payloadRefs', {}).get(name)
        if trace_id is None:
            raise DocumentParsingError('文件归档缺少完整数据')
        return await self.service._document_trace_store.read_payload(
            trace_id,
            document['userId'],
            document['subjectId'],
            name,
        )

    @staticmethod
    def _quality(result):
        processing = result.get('processing') if isinstance(result, dict) else None
        if not isinstance(processing, dict):
            return 'complete', False
        status = processing.get('status', 'complete')
        return status, bool(processing.get('retryable', status != 'complete'))

    @classmethod
    def _target_round(cls, document):
        current = document.get('interpretationRound', 0)
        if document['status'] in {'running', 'failed', 'submission_unknown'}:
            return current
        if document['status'] != 'completed' or (
            document.get('processingStatus') in {'partial', 'unavailable'}
            and document.get('processingRetryable', False)
        ):
            return current + 1
        return current

    @staticmethod
    def _failed_error(document):
        error = DocumentParsingError(
            '共享文件处理失败，请稍后重试',
            retryable=document.get('retryable', True),
            automatic_retryable=document.get('automaticRetryable'),
            reason_code=document.get('reasonCode'),
        )
        error.content_failure = document.get('contentFailure')
        return error

    async def _save_publication(self, document, result):
        return await self.service._legacy_publication.publish_document(document, result)

    async def process(
        self,
        *,
        user_id,
        file_id=None,
        document_id=None,
        subject_id=None,
        report_id=None,
        file_type=None,
        source_type='analysis',
        source_id=None,
        session_id=None,
        file_index=None,
        content=None,
        file_name=None,
        require_text=False,
        on_document_ready=None,
        resume_incomplete=True,
        _current_interpreter=None,
        _settled_document_ids=None,
        publish=True,
        _analysis_publication=None,
        _archive_lifecycle_epoch=None,
    ):
        from .service import _optional_processing_text
        from .service import _required_processing_text

        interpreter = _current_interpreter or self.service._interpreter
        publication_revision = getattr(_analysis_publication, 'revision', None)
        if _analysis_publication is not None and (
            not publish
            or source_type != 'analysis'
            or publication_revision
            != (ANALYSIS_CURRENT_PUBLICATION if _current_interpreter is not None else ANALYSIS_FORMAL_REVISION)
        ):
            raise ValueError('the exact Analysis publication strategy is required')
        user_id = _required_processing_text('userId', user_id, missing_message='缺少必填参数: userId')
        file_id = _optional_processing_text('fileId', file_id)
        document_id = _optional_processing_text('documentId', document_id)
        file_name = _optional_processing_text('file_name', file_name)
        local = file_id is None and document_id is None and content is not None
        if not local and (file_id is None) == (document_id is None):
            raise ValueError('fileId 与 documentId 必须且只能提供一个')
        if local and file_name is None:
            raise ValueError('直接传入文件内容时必须提供 file_name')
        if document_id is not None and (content is not None or file_type is not None or file_name is not None):
            raise ValueError('documentId 引用不能同时提供文件内容或 fileType')
        if content is not None and (not isinstance(content, bytes) or not content):
            raise ValueError('content 必须为非空 bytes')
        if source_type not in {'analysis', 'record', 'report'}:
            raise ValueError('source_type 必须为 analysis、record 或 report')
        if file_index is not None and (type(file_index) is not int or file_index < 0):
            raise ValueError('file_index 必须为非负整数')
        subject_id = _optional_processing_text('subjectId', subject_id)
        if not publish and (subject_id is None or report_id is not None):
            raise ValueError('unpublished preparation requires subjectId and disallows legacy reportId')
        source_id = _optional_processing_text('source_id', source_id)
        session_id = _optional_processing_text('session_id', session_id)
        requested_report_id = None if report_id is None else self.service._report_source.resolve_report_id(report_id)
        subject_id = await self._subject(user_id, subject_id)
        publication_scope = {}
        if publication_revision == ANALYSIS_CURRENT_PUBLICATION:
            publication_scope['lifecycle_epoch'] = await _analysis_publication.subject_epoch(
                DocumentScope(user_id, subject_id)
            )
            _archive_lifecycle_epoch = publication_scope['lifecycle_epoch']
        if _archive_lifecycle_epoch is not None and (
            type(_archive_lifecycle_epoch) is not int or _archive_lifecycle_epoch < 1
        ):
            raise ValueError('a fixed archive requires the original subject lifecycle')
        source = self.service._report_source
        existing = (
            None
            if requested_report_id is None
            else await source.get_owned(
                requested_report_id,
                user_id,
                subject_id,
            )
        )
        # Explicit legacy report IDs still refer to their first saved snapshots.
        # They are never used as URL-only evidence for a new content identity.
        if existing is not None and existing.canonical_report_id is None and document_id is None:
            outcome = await self.service._process_legacy(
                file_id=file_id,
                user_id=user_id,
                subject_id=subject_id,
                report_id=report_id,
                file_type=file_type,
                source_type=source_type,
                source_id=source_id,
                session_id=session_id,
                file_index=file_index,
                content=content,
                require_text=require_text,
            )
            return outcome
        trace = DocumentProcessingTrace(self.service._document_trace_store, user_id=user_id)
        await trace.start(
            subjectId=subject_id,
            fileUrl=file_id,
            requestedDocumentId=document_id,
            reportId=requested_report_id,
            declaredFileType=file_type,
            source={'entrypoint': source_type, 'sourceId': source_id, 'sessionId': session_id, 'fileIndex': file_index},
        )
        owned_task = None
        document = None
        raw = None
        try:
            if document_id is not None:
                document = await self.index.get(document_id, user_id, subject_id)
                if document is None:
                    raise DocumentNotFoundError('文件引用不存在或不可访问')
            else:
                from urllib.parse import urlparse

                if file_id is not None and urlparse(file_id).scheme not in {'http', 'https'}:
                    raise ValueError('fileId 必须是 HTTP 或 HTTPS 地址')
                name_source = file_id or f'https://local.invalid/{file_name}'
                filename, inferred = interpreter._resolve_name(name_source)
                if file_name is not None:
                    from pathlib import PurePosixPath

                    filename = PurePosixPath(file_name.replace('\\', '/')).name
                await trace.update(stage='downloading')
                if content is None:
                    content = await self.downloader.download(file_id)
                if len(content) > 50 * 1024 * 1024:
                    raise DocumentParsingError('文件超过大小限制')
                resolved, _ = await self.service._resolve_file_type(name_source, file_type, inferred, content)
                filename = filename if inferred else f'health-report.{resolved}'
                digest = hashlib.sha256(content).hexdigest()
                fingerprint = source.source_fingerprint(
                    user_id=user_id, subject_id=subject_id, file_id=f'sha256:{digest}'
                )
                if existing is not None:
                    source._ensure_same_source(existing, fingerprint)
                await trace.update(
                    stage='identifying', contentSha256=digest, byteSize=len(content), sourceFingerprint=fingerprint
                )
                document = await self.index.resolve(
                    {
                        '_id': trace.trace_id,
                        'documentId': trace.trace_id,
                        'userId': user_id,
                        'subjectId': subject_id,
                        'contentSha256': digest,
                        'byteSize': len(content),
                        'fileType': resolved,
                        'fileName': filename,
                        'firstFileUrl': file_id,
                        'sourceFingerprint': fingerprint,
                        # Caller IDs are aliases, so competing files cannot poison each other's canonical source.
                        'canonicalReportId': source.resolve_report_id(None),
                        'parserVersion': 'docmind-health-document-v1',
                        'interpretationVersion': interpreter.VERSION,
                        'status': 'pending',
                        'payloadRefs': {},
                        'leaseExpiresAt': None,
                        'createdAt': self.now(),
                        'updatedAt': self.now(),
                    }
                )
            if existing is not None:
                source._ensure_same_source(existing, document['sourceFingerprint'])
            if _current_interpreter is not None and document.get('interpretationVersion') != interpreter.VERSION:
                raise DocumentParsingError('stored document does not belong to the current extraction')
            # Reserve identifiers without creating a clinical report for content-only outcomes.
            if requested_report_id and publication_revision == ANALYSIS_CURRENT_PUBLICATION:
                await _analysis_publication.check_result_document(
                    DocumentScope(user_id, subject_id), requested_report_id, document
                )
            await self.index.bind_report_id(document, document['canonicalReportId'])
            if requested_report_id and requested_report_id != document['canonicalReportId']:
                await self.index.bind_report_id(document, requested_report_id)
            await trace.update(documentId=document['_id'], canonicalReportId=document['canonicalReportId'])
            if on_document_ready is not None and 'original_file' in document['payloadRefs']:
                await on_document_ready(document['_id'])
            deadline = asyncio.get_running_loop().time() + self.WAIT_SECONDS
            may_resume = resume_incomplete and (
                _settled_document_ids is None or document['_id'] not in _settled_document_ids
            )
            target_round = (
                document.get('interpretationRound', 0)
                if document['status'] == 'completed' and not may_resume
                else self._target_round(document)
            )
            analysis_command = None
            if _analysis_publication is not None:
                analysis_command = await _analysis_publication.accept(
                    document=document,
                    trace=trace,
                    report_id=requested_report_id,
                    target_round=target_round,
                    interpretation_revision=interpreter.VERSION,
                    **publication_scope,
                )
            while document['status'] != 'completed' or document.get('interpretationRound', 0) < target_round:
                token, now = uuid4().hex, self.now()
                if document['status'] == 'submission_unknown':
                    delay = self._resubmission_delay(document)
                    if delay is None or delay > 0:
                        logger.warning(
                            '文档提交状态待核查: document_id=%s trace_id=%s status=%s '
                            'submission_started=%s task_id=%s previous_error_type=%s action=%s',
                            document['_id'],
                            trace.trace_id,
                            document['status'],
                            document.get('submissionStarted', False),
                            document.get('taskId') or '-',
                            document.get('errorType') or '-',
                            'manual_check' if delay is None else 'wait_recovery',
                        )
                        raise DocumentSubmissionConflictError(
                            '文档提交结果待核查，暂不重复提交',
                            retry_after_seconds=delay,
                        )
                    claimed = await self.index.claim_resubmission(
                        document,
                        token,
                        now,
                        now + timedelta(seconds=self.LEASE_SECONDS),
                    )
                else:
                    if document['status'] == 'failed' and document.get('retryable') is False:
                        raise self._failed_error(document)
                    claimed = await self.index.claim(document, token, now, now + timedelta(seconds=self.LEASE_SECONDS))
                if claimed is not None:
                    # The task is retained, supervised in production and shielded from HTTP waiter cancellation.
                    coroutine = self._execute(
                        claimed,
                        token,
                        trace,
                        content,
                        session_id,
                        on_document_ready,
                        publish=publish and _analysis_publication is None,
                        archive_lifecycle_epoch=_archive_lifecycle_epoch,
                        **({'_current_interpreter': interpreter} if _current_interpreter is not None else {}),
                    )
                    try:
                        task = self.task_spawner(coroutine)
                    except Exception:
                        coroutine.close()
                        await self.index.update_owned(
                            claimed,
                            token,
                            self.now(),
                            {'status': 'failed', 'leaseToken': None, 'leaseExpiresAt': None},
                        )
                        raise
                    self.tasks.add(task)
                    task.add_done_callback(self._task_done)
                    owned_task = task
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(task), max(0.01, deadline - asyncio.get_running_loop().time())
                        )
                    except TimeoutError as error:
                        raise DocumentBusyError('文件仍在处理中，请稍后重试') from error
                else:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise DocumentBusyError('文件仍在处理中，请稍后重试')
                    await asyncio.sleep(self.POLL_SECONDS)
                document = await self.index.get(document['_id'], user_id, subject_id)
                if document['status'] == 'failed':
                    raise self._failed_error(document)
            result = await self._payload(document, 'interpretation')
            raw = await self._payload(document, 'docmind_result')
            self._valid_text(raw, allow_empty=classify_outcome(result) == 'no_readable_text')
            if _settled_document_ids is not None:
                _settled_document_ids.add(document['_id'])
            quality, retryable = self._quality(result)
            if quality == 'unavailable':
                if (
                    _analysis_publication is not None
                    and getattr(_analysis_publication, 'stores_content_response', False)
                    and not require_text
                    and classify_outcome(result) is not None
                ):
                    return await _analysis_publication.finish(
                        command=analysis_command,
                        document=document,
                        result=result,
                        raw=raw,
                        trace=trace,
                        report_id=requested_report_id,
                        require_text=require_text,
                    )
                await trace.update(stage='interpreting')
                error = DocumentInterpretationError(
                    '文件暂无可用的解读结果',
                    processing=result.get('processing'),
                    retryable=retryable,
                    document_id=document['_id'],
                    trace_id=trace.trace_id,
                    result=result,
                    content_outcome=classify_outcome(result),
                )
                if error.content_outcome is None:
                    error.content_failure = False
                error.material_binding = failure_material_binding(document, raw)
                raise error
            if not publish:
                await trace.update(stage='prepared', status='completed', storedDocumentId=document['_id'])
                return ProcessedHealthReport(
                    report=report_source_content(result),
                    text=raw['text'] if require_text else None,
                    document_id=document['_id'],
                    trace_id=trace.trace_id,
                    ingestion_status='not_submitted',
                    ingestion_job_id=None,
                    reused=document['_id'] != trace.trace_id,
                    archive_trace_id=document['payloadRefs']['docmind_result'],
                    file_name=document['fileName'],
                    resolved_file_type=document['fileType'],
                    content_sha256=document['contentSha256'],
                    canonical_report_id=document['canonicalReportId'],
                    source_revision=None,
                    preparation_binding=DocumentPreparationBinding.capture(document, raw, result),
                )
            if _analysis_publication is not None:
                return await _analysis_publication.finish(
                    command=analysis_command,
                    document=document,
                    result=result,
                    raw=raw,
                    trace=trace,
                    report_id=requested_report_id,
                    require_text=require_text,
                )
            # Rechecking the canonical source also compensates pending ingestion after a queue outage.
            saved = await self._save_publication(document, result)
            if requested_report_id and requested_report_id != document['canonicalReportId']:
                await self.service._legacy_publication.save(
                    report_id=requested_report_id,
                    user_id=user_id,
                    subject_id=subject_id,
                    source_fingerprint=document['sourceFingerprint'],
                    result=report_source_content(result),
                    document_id=document['_id'],
                    canonical_report_id=document['canonicalReportId'],
                )
            reused = document['_id'] != trace.trace_id
            await trace.update(
                stage='reused' if reused else 'completed',
                status='reused' if reused else 'completed',
                reusedDocumentId=document['_id'] if reused else None,
                storedDocumentId=document['_id'],
                reportId=requested_report_id or saved.record.report_id,
                sourceRevision=saved.record.source_revision,
                processingStatus=quality,
                reuseReason='reference' if document_id else ('content' if reused else 'new'),
                profileIngestion={'status': saved.record.ingestion_status, 'jobId': saved.record.ingestion_job_id},
            )
            public = saved.record.public_result()
            public['reportId'] = requested_report_id or saved.record.report_id
            public['documentId'] = document['_id']
            return ProcessedHealthReport(
                report=public,
                text=raw['text'] if require_text else None,
                document_id=document['_id'],
                trace_id=trace.trace_id,
                ingestion_status=saved.record.ingestion_status,
                ingestion_job_id=saved.record.ingestion_job_id,
                reused=reused,
                archive_trace_id=document['payloadRefs']['docmind_result'],
                file_name=document['fileName'],
                resolved_file_type=document['fileType'],
                content_sha256=document['contentSha256'],
                canonical_report_id=document['canonicalReportId'],
                source_revision=saved.record.source_revision,
                input_snapshot=DocumentInputSnapshot(
                    parse_artifact_ref=document['payloadRefs'].get('docmind_result'),
                    interpretation_artifact_ref=document['payloadRefs'].get('interpretation'),
                    interpretation_revision=document.get('interpretationVersion'),
                    validation_revision=document.get('interpretationValidatedVersion'),
                    interpretation_round=document.get(
                        'interpretationResultRound', document.get('interpretationRound', 0)
                    ),
                ),
            )
        except (Exception, asyncio.CancelledError) as error:
            if isinstance(error, DocumentParsingError):
                error.document_id = document['_id'] if document is not None else None
                error.trace_id = trace.trace_id
            if isinstance(error, DocumentInterpretationError):
                error.result_context = {
                    'userId': user_id,
                    'subjectId': subject_id,
                    'reportId': requested_report_id or (document or {}).get('canonicalReportId'),
                    'documentId': (document or {}).get('_id'),
                }
            # An owner timeout/cancellation must not overwrite the still-running task's trace.
            if owned_task is None or owned_task.done():
                content_response = not require_text and getattr(error, 'content_outcome', None)
                with suppress(Exception):
                    await trace.update(
                        status=(
                            'cancelled'
                            if isinstance(error, asyncio.CancelledError)
                            else 'completed'
                            if content_response
                            else 'failed'
                        ),
                        errorType=None if content_response else type(error).__name__,
                        contentOutcome=getattr(error, 'content_outcome', None),
                        failureMaterialBinding=getattr(error, 'material_binding', None),
                    )
            else:
                with suppress(Exception):
                    await trace.update(
                        waiterStatus='cancelled' if isinstance(error, asyncio.CancelledError) else 'timed_out'
                    )
            raise

    def _task_done(self, task):
        self.tasks.discard(task)
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _valid_text(raw, *, allow_empty=False):
        parsed_document_text(raw, allow_empty=allow_empty)

    def _resubmission_delay(self, document):
        if document.get('resubmissionCount', 0) >= 1 + document.get('manualResubmissionGrants', 0):
            return None
        retry_at = document.get('submissionRetryAfter')
        if retry_at is None:
            updated = document.get('updatedAt', self.now())
            retry_at = updated + timedelta(seconds=self.UNKNOWN_COOLDOWN_SECONDS)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0, (retry_at - self.now()).total_seconds())

    async def _execute(
        self,
        document,
        token,
        trace,
        content,
        session_id,
        on_document_ready=None,
        *,
        publish=True,
        _current_interpreter=None,
        archive_lifecycle_epoch=None,
    ):
        checkpoint = _CheckpointTrace(self, document, token, trace)
        heartbeat = asyncio.create_task(self._heartbeat(checkpoint))
        interpreter = _current_interpreter or self.service._interpreter
        fixed_round = None
        try:
            if _current_interpreter is not None:
                number = document.get('interpretationRound', 0)
                binding = document.get('archiveLifecycleBinding') or {}
                if binding.get('round') == number:
                    archive_lifecycle_epoch = binding['epoch']
                else:
                    await checkpoint.save(archiveLifecycleBinding={'round': number, 'epoch': archive_lifecycle_epoch})
            if document.get('submissionStarted') and not document.get('taskId'):
                await checkpoint.save(
                    status='submission_unknown',
                    leaseToken=None,
                    leaseExpiresAt=None,
                    submissionRetryAfter=self.now() + timedelta(seconds=self.UNKNOWN_COOLDOWN_SECONDS),
                )
                # Return the persisted state to the waiter, which reports a submission conflict.
                return
            result_is_validated = (
                'interpretation' in document['payloadRefs']
                and document.get('interpretationVersion') == interpreter.VERSION
                and document.get('interpretationValidatedVersion') == interpreter.VERSION
                and document.get('interpretationResultRound', document.get('interpretationRound', 0))
                == document.get('interpretationRound', 0)
            )
            result = None
            if _current_interpreter is not None:
                fixed_round = await checkpoint.read_archived_round()
                if fixed_round is not None and (
                    not result_is_validated or 'docmind_result' not in document['payloadRefs']
                ):
                    raise RuntimeError('fixed document round lacks its validated artifacts')
                if result_is_validated:
                    result = await self._payload(document, 'interpretation')
            if 'docmind_result' in document['payloadRefs']:
                raw = await self._payload(document, 'docmind_result')
                self._valid_text(raw, allow_empty=classify_outcome(result) == 'no_readable_text')
                text = raw['text']
                checkpoint.parsed_document = raw
            else:
                if content is None:
                    content = (await self._payload(document, 'original_file'))['content']
                if (
                    hashlib.sha256(content).hexdigest() != document['contentSha256']
                    or len(content) != document['byteSize']
                ):
                    raise DocumentParsingError('原始文件归档校验失败')
                await checkpoint.write_payload('original_file', {'content': content})
                if not publish and document['fileType'] == 'pdf':
                    await trace.update(
                        verifiedOriginal={
                            'artifactRef': document['payloadRefs']['original_file'],
                            'contentSha256': document['contentSha256'],
                            'byteSize': document['byteSize'],
                            'fileName': document['fileName'],
                            'fileType': document['fileType'],
                        }
                    )
                if on_document_ready is not None:
                    await on_document_ready(document['_id'])
                text = await interpreter.extract_document(
                    trace=checkpoint,
                    file_url=document['firstFileUrl'],
                    filename=document['fileName'],
                    file_type=document['fileType'],
                    content=content,
                )
            if result_is_validated:
                if _current_interpreter is None:
                    result = await self._payload(document, 'interpretation')
            else:
                await checkpoint.update(stage='interpreting')
                await checkpoint.save(interpretationVersion=interpreter.VERSION, interpretationValidatedVersion=None)
                result = await interpreter.analyze(
                    text,
                    user_id=document['userId'],
                    subject_id=document['subjectId'],
                    session_id=session_id,
                    trace=checkpoint,
                )
                if result is None:
                    raise DocumentInterpretationError('体检报告解读失败')
            interpretation_changed = False
            if _current_interpreter is not None and fixed_round is None:
                normalized = deepcopy(result)
                interpretation_changed = normalize_basic_info_times(normalized)
                if interpretation_changed:
                    result = normalized
            if not result_is_validated or interpretation_changed:
                await checkpoint.write_payload('interpretation', result)
            if not result_is_validated:
                await checkpoint.save(
                    interpretationValidatedVersion=interpreter.VERSION,
                    interpretationResultRound=document.get('interpretationRound', 0),
                )
            await checkpoint.archive_round(result, lifecycle_epoch=archive_lifecycle_epoch)
            quality, retryable = self._quality(result)
            if quality == 'unavailable':
                # This round has settled. A new explicit request may recover its
                # unresolved checkpoints, but this waiter must not loop forever.
                await checkpoint.update(stage='interpreting')
                await checkpoint.save(
                    status='completed',
                    processingStatus=quality,
                    processingRetryable=retryable,
                    errorType=None,
                    leaseToken=None,
                    leaseExpiresAt=None,
                )
                return
            if not publish:
                await checkpoint.update(stage='prepared', status='completed')
                await checkpoint.save(
                    status='completed',
                    processingStatus=quality,
                    processingRetryable=retryable,
                    errorType=None,
                    leaseToken=None,
                    leaseExpiresAt=None,
                )
                return
            await checkpoint.update(stage='saving_report')
            saved = await self._save_publication(document, result)
            await checkpoint.write_payload(
                'publication',
                {'sourceRevision': saved.record.source_revision, 'result': saved.record.public_result()},
            )
            await checkpoint.update(stage='completed', status='completed')
            await checkpoint.save(
                status='completed',
                processingStatus=quality,
                processingRetryable=retryable,
                sourceRevision=saved.record.source_revision,
                errorType=None,
                leaseToken=None,
                leaseExpiresAt=None,
            )
        except (Exception, asyncio.CancelledError) as error:
            if (
                fixed_round is None
                and isinstance(error, DocumentInterpretationError)
                and error.content_outcome == 'no_readable_text'
            ):
                # A successful empty OCR task is settled once, with a durable non-clinical outcome.
                try:
                    await checkpoint.write_payload('interpretation', error.result)
                    await trace.update(stage='interpreting', status='completed', contentOutcome='no_readable_text')
                    await checkpoint.save(
                        interpretationVersion=interpreter.VERSION,
                        interpretationValidatedVersion=interpreter.VERSION,
                        interpretationResultRound=document.get('interpretationRound', 0),
                    )
                    await checkpoint.archive_round(error.result, lifecycle_epoch=archive_lifecycle_epoch)
                    await checkpoint.save(
                        status='completed',
                        processingStatus='unavailable',
                        processingRetryable=False,
                        interpretationVersion=interpreter.VERSION,
                        interpretationValidatedVersion=interpreter.VERSION,
                        interpretationResultRound=document.get('interpretationRound', 0),
                        errorType=None,
                        leaseToken=None,
                        leaseExpiresAt=None,
                    )
                    return
                except (Exception, asyncio.CancelledError) as archive_error:
                    # Failed required persistence follows the ordinary failure/lease cleanup path.
                    error = archive_error
            uncertain = bool(
                document.get('submissionStarted')
                and not document.get('taskId')
                and not isinstance(error, DocumentSubmissionRejectedError)
            )
            status = 'submission_unknown' if uncertain else 'failed'
            retryable = getattr(error, 'retryable', True)
            recovery = {
                'retryable': retryable,
                'reasonCode': getattr(error, 'reason_code', None),
                'automaticRetryable': getattr(error, 'automatic_retryable', retryable),
                'contentFailure': (
                    getattr(error, 'content_failure', None) if isinstance(error, DocumentParsingError) else False
                ),
            }
            if uncertain:
                recovery['submissionRetryAfter'] = self.now() + timedelta(seconds=self.UNKNOWN_COOLDOWN_SECONDS)
                error.submission_unknown = True
                error.retry_after_seconds = (
                    self.UNKNOWN_COOLDOWN_SECONDS
                    if document.get('resubmissionCount', 0) < 1 + document.get('manualResubmissionGrants', 0)
                    else None
                )
            elif isinstance(error, DocumentSubmissionRejectedError):
                recovery['submissionStarted'] = False
            logger.error(
                '文档处理失败: document_id=%s trace_id=%s stage=%s status=%s task_id=%s %s',
                document['_id'],
                trace.trace_id,
                trace.stage,
                status,
                document.get('taskId') or '-',
                format_exception_summary(error),
            )
            with suppress(Exception):
                await checkpoint.save(
                    status=status, errorType=type(error).__name__, leaseToken=None, leaseExpiresAt=None, **recovery
                )
            if isinstance(error, DocumentParsingError) and trace.stage == 'interpreting':
                with suppress(Exception):
                    error.material_binding = failure_material_binding(
                        document, getattr(checkpoint, 'parsed_document', None)
                    )
            with suppress(Exception):
                await trace.update(
                    status='failed',
                    errorType=type(error).__name__,
                    reasonCode=getattr(error, 'reason_code', None),
                    failureMaterialBinding=getattr(error, 'material_binding', None),
                )
            raise error
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat(self, checkpoint):
        while True:
            await asyncio.sleep(self.HEARTBEAT_SECONDS)
            try:
                await checkpoint.save(leaseExpiresAt=self.now() + timedelta(seconds=self.LEASE_SECONDS))
            except Exception:
                checkpoint.lost = True
                return


class _CheckpointTrace:
    def __init__(self, owner, document, token, trace):
        self.owner, self.document, self.token, self.trace = owner, document, token, trace
        self.trace_id = trace.trace_id
        self.resume_task_id = document.get('taskId')
        self.lost = False
        self._payload_lock = asyncio.Lock()

    async def save(self, **fields):
        if self.lost:
            raise DocumentBusyError('文件处理执行权已失效，请稍后重试')
        await self.owner.index.update_owned(self.document, self.token, self.owner.now(), fields)
        self.document.update(fields)

    async def update(self, **fields):
        durable = {}
        if fields.get('stage') == 'submitting':
            durable.update(
                submissionStarted=True, submissionStartedAt=self.owner.now(), submissionTraceId=self.trace_id
            )
        if 'taskId' in fields:
            durable['taskId'] = fields['taskId']
        await self.save(**durable)
        await self.trace.update(**fields)

    async def write_payload(self, name, payload):
        # Chunks execute concurrently. Publish each complete payload under a serialized manifest update.
        async with self._payload_lock:
            if name == 'original_file' and name in self.document['payloadRefs']:
                return
            await self.save()
            await self.trace.write_payload(name, payload)
            refs = {**self.document['payloadRefs'], name: self.trace_id}
            await self.save(payloadRefs=refs)

    async def read_payload(self, name):
        if name not in self.document['payloadRefs']:
            return None
        return await self.owner._payload(self.document, name)

    async def read_archived_round(self):
        return await self.owner.index.get_archived_round(
            self.document['_id'],
            self.document['userId'],
            self.document['subjectId'],
            self.document.get('interpretationRound', 0),
        )

    async def archive_round(self, interpretation, *, lifecycle_epoch=None):
        """Freeze this completed output before releasing the lease or HTTP cancellation."""
        parsed = await self.owner._payload(self.document, 'docmind_result')
        names = (
            '_id',
            'userId',
            'subjectId',
            'canonicalReportId',
            'contentSha256',
            'fileName',
            'fileType',
            'sourceFingerprint',
            'byteSize',
            'firstFileUrl',
            'taskId',
            'parserVersion',
            'sourceRevision',
            'interpretationVersion',
            'interpretationValidatedVersion',
            'interpretationRound',
            'interpretationResultRound',
        )
        archived = {key: deepcopy(self.document[key]) for key in names if key in self.document}
        archived['payloadRefs'] = {
            key: value
            for key, value in self.document['payloadRefs'].items()
            if key in {'original_file', 'docmind_result', 'interpretation'}
        }
        archived.update(
            status='completed',
            interpretationResultRound=self.document.get('interpretationRound', 0),
            parseDigest=payload_digest(parsed),
            interpretationDigest=payload_digest(interpretation),
        )
        if lifecycle_epoch is not None:
            archived['subjectLifecycleEpoch'] = lifecycle_epoch
        existing = await self.read_archived_round()
        if existing is not None:
            # Delivery can update live metadata after this write succeeded. Reuse
            # the original immutable metadata when its actual artifacts still match.
            identity = (
                '_id',
                'userId',
                'subjectId',
                'canonicalReportId',
                'contentSha256',
                'payloadRefs',
                'parseDigest',
                'interpretationDigest',
                'interpretationResultRound',
            )
            if any(existing.get(key) != archived.get(key) for key in identity):
                raise RuntimeError('fixed document round artifacts cannot be overwritten')
            return
        await self.owner.index.put_archived_round(self.document, self.token, self.owner.now(), archived)
