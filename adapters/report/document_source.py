"""Report-owned adapter to the application's shared document processor."""

from __future__ import annotations

import asyncio
import hashlib
import re
from copy import deepcopy
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from time import monotonic

import httpx

from modules.analysis.application.documents import DocumentInterpretationError
from modules.analysis.application.documents import DocumentParsingError
from modules.analysis.application.output_profiles import DocumentSnapshot
from modules.analysis.application.service import HealthReportUpstreamError
from modules.analysis.contracts import OutputSelection
from modules.report import parsing
from modules.report.artifacts import note_report_documents
from modules.report.document_source import EXTRACTOR_VERSION
from modules.report.document_source import evaluation_amr_class_rows
from modules.report.document_source import evaluation_disease_evidence_metrics
from modules.report.document_source import evaluation_panel_summary_metrics
from modules.report.document_source import extract_intrinsic_report_source
from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY
from modules.report.fallback_policy import ReportFallbackPolicy
from modules.report.ports import DocumentServiceError
from modules.report.ports import ReportDocumentSource
from modules.report.schemas import ParsedMicrobiomeReport
from utils.log import getLogger
from utils.report_content import report_public_content

from .document_profiles import configure_report_source_profiles
from .verified_source import exact_source_projection
from .verified_source import trusted_report_source

logger = getLogger(__name__)

REPORT_DOCUMENT_SELECTION = OutputSelection('report_source', '1', EXTRACTOR_VERSION)


def _parsed(data):
    if not isinstance(data, dict):
        raise DocumentServiceError('shared service returned no report_source')
    value = {key: item for key, item in data.items() if key not in {'sourceDocuments', 'processing'}}
    value['sourceType'] = 'pdf' if value.get('sourceType') == 'pdf' else 'image'
    if not any(value.get(key) for key in ('metrics', 'taxa', 'diseaseResults')):
        value['sourceType'] = 'pending'
    return ParsedMicrobiomeReport.model_validate(value)


def _processing(*outcomes):
    """An extension cannot upgrade the quality of any contributing base snapshot."""
    summaries = []
    ranks = {'complete': 0, 'partial': 1, 'unavailable': 2}
    for outcome in outcomes:
        for value in (outcome.report, outcome.structured_data):
            if not isinstance(value, dict) or 'processing' not in value:
                continue  # Legacy snapshots have no processing field.
            processing = value['processing']
            if not isinstance(processing, dict) or processing.get('status') not in ranks:
                processing = {'status': 'unavailable', 'retryable': False}
            summaries.append(processing)
    if not summaries:
        return {}
    summary = dict(max(summaries, key=lambda item: ranks[item['status']]))
    summary['retryable'] = any(item.get('retryable', False) for item in summaries)
    if len(outcomes) > 1:
        # Per-input issue counts may overlap; do not invent a combined count.
        summary.pop('issueCount', None)
    return report_public_content({'processing': summary})['processing']


def _parsed_output(outcome, *, location, inputs=(), table_documents=()):
    processing = _processing(outcome, *inputs)
    if processing.get('status') == 'unavailable':
        # Only the verified archive fallback may expose source-only recovery.
        raise DocumentServiceError('shared report output is unavailable')
    data = outcome.structured_data
    if processing.get('status') == 'partial':
        if not isinstance(data, dict):
            raise DocumentServiceError('shared service returned no report_source')
        data = trusted_report_source(data, inputs or (outcome,), table_documents=table_documents)
    data = exact_source_projection(data, table_documents)
    # The shared profile may omit report-only extension fields. Never trust a
    # candidate array from that profile: recover the evaluate headings locally
    # from the permission-checked text/original of this exact selected file.
    if isinstance(data, dict):
        data = {
            key: value
            for key, value in data.items()
            if key
            not in {
                'evaluationPanelSummaries',
                'evaluation_panel_summaries',
                'evaluationAmrClasses',
                'evaluation_amr_classes',
            }
        }
    parsed = _parsed(data)
    filename = getattr(outcome, 'file_name', None)
    checksum = getattr(outcome, 'content_sha256', None)
    if filename and checksum and filename == data.get('sourceName') and checksum == data.get('sourceChecksum'):
        documents = [
            parsing.ExtractedDocument(
                filename,
                getattr(outcome, 'resolved_file_type', None) or 'pdf',
                getattr(outcome, 'text', None) or '',
                metadata={'content_sha256': checksum},
            )
        ]
        documents.extend(
            document
            for document in table_documents
            if document.filename == filename and document.metadata.get('content_sha256') == checksum
        )
        summaries = {}
        for document in documents:
            for item in evaluation_panel_summary_metrics(document):
                summaries.setdefault(
                    (item.code, item.name, item.value, item.unit, item.reference_range, item.status), item
                )
        parsed.evaluation_panel_summaries = list(summaries.values())
        # Evaluate may restore categorical overview cells omitted by a shared
        # partial interpretation. Only the already verified current original
        # and its real table pages enter the existing excluded metadata lane.
        from modules.report.evaluation_source_recovery import evaluation_health_summary_metrics

        for document in documents:
            parsed.evaluation_panel_summaries.extend(evaluation_health_summary_metrics(document))
            parsed.evaluation_panel_summaries.extend(evaluation_disease_evidence_metrics(document))
        # Class metadata requires explicit original table columns. Unlike
        # heading ratios, linear text cannot establish merged-cell ownership.
        amr_classes = []
        for document in table_documents:
            if document.filename == filename and document.metadata.get('content_sha256') == checksum:
                for row in evaluation_amr_class_rows(document):
                    if row not in amr_classes:
                        amr_classes.append(row)
        parsed.evaluation_amr_classes = amr_classes
    if processing.get('status') == 'partial':
        marker = f'{location}: SHARED_INTERPRETATION_PARTIAL'
        if marker not in parsed.source_errors:
            parsed.source_errors.append(marker)
    return parsed


def _legacy_fallback_allowed(document):
    version = re.fullmatch(r'analysis-health-report-v(\d+)', document.get('interpretationVersion') or '')
    return bool(version and int(version[1]) < 6)


def _reference(outcome, index):
    processing = _processing(outcome)
    return {
        'fileIndex': index,
        'documentId': outcome.document_id,
        'traceId': outcome.trace_id,
        'reportId': outcome.report.get('reportId'),
        'canonicalReportId': outcome.canonical_report_id,
        'ingestionStatus': outcome.ingestion_status,
        'jobId': outcome.ingestion_job_id,
        'reused': outcome.reused,
        'extractionId': outcome.extraction_id,
        'sourceRevision': outcome.source_revision,
        'outputTraceId': outcome.output_trace_id,
        'processingStatus': 'completed' if processing.get('status', 'complete') == 'complete' else processing['status'],
        'processing': processing,
        **({'formalSource': deepcopy(outcome.formal_source)} if hasattr(outcome, 'formal_source') else {}),
    }


def _parsing_failure(error):
    seen, chain = set(), []
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        chain.append(error)
        error = error.__cause__
    if any(
        getattr(item, 'content_failure', True) is False
        or isinstance(item, PermissionError)
        or getattr(item, 'status_code', None) in (401, 403)
        for item in chain
    ):
        return None
    return next((item for item in chain if isinstance(item, DocumentParsingError)), None)


def _retryable_download_failure(error):
    failure = _parsing_failure(error)
    # The shared downloader wraps transport failures with this specific error.
    # Other parsing errors must not restart OCR or interpretation.
    if failure is None or str(failure) != '文件下载失败或超时' or not failure.automatic_retryable:
        return False
    cause = failure.__cause__
    if isinstance(cause, httpx.HTTPStatusError):
        return cause.response.status_code == 429 or 500 <= cause.response.status_code < 600
    return isinstance(
        cause,
        (TimeoutError, ConnectionError, httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError),
    )


class ReportDocumentSourceFactory:
    def __init__(self, processing_service_factory, archive_locator=None):
        self.processing_service_factory = processing_service_factory
        self.archive_locator = archive_locator

    def document_input_manifest(self):
        return {
            'consumerRevision': 'report-legacy-documents.v1',
            'outputSelection': asdict(REPORT_DOCUMENT_SELECTION),
        }

    def create(self, *, user_id, session_id, final_report_id):
        try:
            return self._create(user_id=user_id, session_id=session_id, final_report_id=final_report_id)
        except Exception as error:
            # A shared-service configuration failure is a per-file source
            # failure, not a reason to discard structured JSON/testData.
            return UnavailableReportDocuments(type(error).__name__)

    def _create(self, *, user_id, session_id, final_report_id):
        processor = self.processing_service_factory()
        configure_report_source_profiles(processor)
        return SharedReportDocuments(processor, user_id, session_id, final_report_id, self.archive_locator)


class ReportArchiveLocator:
    """Read metadata for this exact failed call; payload access stays in the shared service."""

    def __init__(self, traces, documents):
        self.traces, self.documents = traces, documents

    async def locate(self, *, user_id, session_id, final_report_id, source, index, started):
        if not final_report_id:
            return None
        query = {
            'userId': user_id,
            'source.entrypoint': 'report',
            'source.sourceId': final_report_id,
            'source.sessionId': session_id,
            'source.fileIndex': index,
            'createdAt': {'$gte': started},
            'status': 'failed',
            'stage': 'interpreting',
            'requestedDocumentId': source.document_id,
            'fileUrl': source.url,
        }
        matches = await self.traces.find(query).limit(2).to_list(length=2)
        if len(matches) != 1:
            return None
        trace = matches[0]
        # Persisted metadata uses names; both the legacy base error and the
        # typed interpretation error identify this same recoverable boundary.
        parsing_errors = {DocumentParsingError.__name__, DocumentInterpretationError.__name__}
        if trace.get('errorType') not in parsing_errors or not trace.get('documentId') or not trace.get('subjectId'):
            return None
        document = await self.documents.find_one(
            {
                '_id': trace['documentId'],
                'userId': user_id,
                'subjectId': trace['subjectId'],
            }
        )
        if not document:
            return None
        failed = document.get('status') == 'failed' and document.get('errorType') in parsing_errors
        unavailable = document.get('status') == 'completed' and document.get('processingStatus') == 'unavailable'
        if not (failed or unavailable) or not all(
            name in document.get('payloadRefs', {}) for name in ('original_file', 'docmind_result')
        ):
            return None
        return trace, document


@dataclass(frozen=True)
class UnavailableReportDocuments:
    error_type: str

    async def read(self, source, *, index):
        raise DocumentServiceError(f'shared document configuration unavailable: {self.error_type}')

    async def combine(self, indexes):
        raise DocumentServiceError('shared document configuration unavailable')


@dataclass(frozen=True)
class SharedReportDocuments:
    processor: object
    user_id: str
    session_id: str
    final_report_id: str | None
    archive_locator: object = None
    outcomes: dict = field(default_factory=dict, compare=False)
    recovered: dict = field(default_factory=dict, compare=False)
    policy: ReportFallbackPolicy = DEFAULT_REPORT_FALLBACK_POLICY

    def _identity(self):
        return dict(
            user_id=self.user_id,
            session_id=self.session_id,
            source_type='report',
            source_id=self.final_report_id,
            output_profile='report_source',
            output_selection=REPORT_DOCUMENT_SELECTION,
        )

    async def read(self, source, *, index):
        started = datetime.now(timezone.utc)
        location = (
            {'document_id': source.document_id}
            if source.document_id
            else {'file_id': source.url, 'file_type': source.file_type}
        )
        try:
            attempts = 1 if source.document_id else self.policy.attempts_for(self.policy.source_fetch_attempts)
            for attempt in range(attempts):
                started = datetime.now(timezone.utc)
                attempt_started = monotonic()
                try:
                    outcome = await self.processor.process_document(
                        **location,
                        **self._identity(),
                        file_index=index,
                    )
                except Exception as error:
                    if not _retryable_download_failure(error):
                        raise
                    logger.warning(
                        'Report source download failed file_index=%s attempt=%s/%s elapsed_ms=%s retry=%s',
                        index,
                        attempt + 1,
                        attempts,
                        round((monotonic() - attempt_started) * 1000),
                        attempt + 1 < attempts,
                    )
                    if attempt + 1 == attempts:
                        raise
                    await asyncio.sleep(self.policy.backoff_after(attempt))
                else:
                    break
        except Exception as error:
            if (
                isinstance(error, HealthReportUpstreamError)
                and _parsing_failure(error) is not None
                and self.archive_locator is not None
            ):
                try:
                    recovered = await self._recover(source, index, started)
                except Exception as recovery_error:
                    note_report_documents(
                        [
                            {
                                'fileIndex': index,
                                'recoveryErrorType': type(recovery_error).__name__,
                            }
                        ]
                    )
                else:
                    if recovered is not None:
                        return recovered
            note_report_documents(
                [
                    {
                        'fileIndex': index,
                        'documentId': source.document_id,
                        'processingStatus': 'failed',
                        'ingestionStatus': 'unknown',
                        'errorType': type(error).__name__,
                    }
                ]
            )
            raise
        self.outcomes[index] = outcome
        documents = await self._original_tables(outcome)
        parsed = _parsed_output(outcome, location=f'fileList[{index}]', table_documents=documents)
        from modules.report.evaluation_source_recovery import evaluation_clinical_source_rows

        clinical_rows = tuple(
            row
            for document in documents
            for row in evaluation_clinical_source_rows(
                document,
                filename=outcome.file_name,
                checksum=outcome.content_sha256,
                report_type=(outcome.report or {}).get('reportType'),
            )
        )
        note_report_documents([_reference(outcome, index)])
        return ReportDocumentSource(
            parsed,
            outcome.text or '',
            outcome.file_name,
            'pdf' if outcome.resolved_file_type == 'pdf' else 'image',
            (_reference(outcome, index),),
            clinical_rows,
            tuple(
                document
                for document in documents
                if document.filename == outcome.file_name
                and document.metadata.get('content_sha256') == outcome.content_sha256
            ),
        )

    async def _original_tables(self, outcome):
        needs_amr_classes = bool(
            re.search(
                r'抗生素耐药(?:风险|性)?评估|抗菌药物耐药风险评估|(?:antibiotic|antimicrobial)\s+resistance\s+risk\s+assessment',
                outcome.text or '',
                re.I,
            )
        )
        needs_score_cells = outcome.resolved_file_type == 'pdf' and bool(
            re.search(r'GMWI|菌群|微生态|营养|代谢|风险指数|风险值|风险等级', outcome.text or '', re.I)
        )
        if _processing(outcome).get('status') != 'partial' and not needs_amr_classes and not needs_score_cells:
            return ()
        # Images use the OCR tables already returned for this exact document.
        # PDF geometry is read through the shared ownership-checked archive;
        # no second download, OCR submission or AI interpretation is performed.
        document = parsing.ExtractedDocument(
            outcome.file_name or '',
            outcome.resolved_file_type or 'image',
            '',
            tables=parsing._markdown_tables(outcome.text or '', page_number=None),
        )
        if outcome.resolved_file_type == 'pdf' and outcome.document_id and outcome.content_sha256:
            try:
                raw = await self.processor.read_document_payload(
                    outcome.document_id,
                    user_id=self.user_id,
                    subject_id=outcome.report.get('subjectId'),
                    name='original_file',
                )
                content = raw.get('content') if isinstance(raw, dict) else None
                if not isinstance(content, bytes) or hashlib.sha256(content).hexdigest() != outcome.content_sha256:
                    raise DocumentServiceError('source table checksum mismatch')
                native = await asyncio.to_thread(parsing._extract_pdf, outcome.file_name, content, ocr=False)
                if native.tables or (native.text or '').strip():
                    return (native,)
            except Exception as error:
                # A scan may have no recoverable native geometry. The exact
                # OCR text already returned for this permission-checked
                # document is still usable when it contains semantic tables.
                # Integrity/access failures do not expose any other payload.
                logger.warning('Report original table verification unavailable: %s', type(error).__name__)
                return (document,) if document.tables else ()
        return (document,)

    async def _recover(self, source, index, started):
        located = await self.archive_locator.locate(
            user_id=self.user_id,
            session_id=self.session_id,
            final_report_id=self.final_report_id,
            source=source,
            index=index,
            started=started,
        )
        if located is None:
            return None
        trace, document = located
        if not _legacy_fallback_allowed(document):
            # New interpretation already quarantined unverified observations;
            # reparsing its whole raw text would undo that decision.
            return None
        identity = {'user_id': self.user_id, 'subject_id': document['subjectId']}
        raw, ocr = await asyncio.gather(
            *(
                self.processor.read_document_payload(document['_id'], **identity, name=name)
                for name in ('original_file', 'docmind_result')
            )
        )
        content = raw.get('content') if isinstance(raw, dict) else None
        text = ocr.get('text') if isinstance(ocr, dict) else None
        if (
            not isinstance(content, bytes)
            or len(content) != document['byteSize']
            or hashlib.sha256(content).hexdigest() != document['contentSha256']
            or not isinstance(ocr, dict)
            or ocr.get('success') is not True
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise DocumentServiceError('archive is not a complete verified source')
        snapshot = DocumentSnapshot(
            document['_id'], {}, text, document['fileName'], document['fileType'], document['contentSha256'], content
        )
        parsed = _parsed(await asyncio.to_thread(extract_intrinsic_report_source, (snapshot,)))
        if not (parsed.metrics or parsed.taxa or parsed.disease_results or parsed.supplementary_hospital_data):
            return None
        parsed.source_errors = [f'fileList[{index}]: SHARED_INTERPRETATION_FALLBACK']
        reference = {
            'fileIndex': index,
            'documentId': document['_id'],
            'traceId': trace['traceId'],
            'canonicalReportId': document['canonicalReportId'],
            'reportId': None,
            'processingStatus': 'fallback_source_only',
            'sharedProcessingStatus': 'failed',
            'ingestionStatus': 'not_submitted',
            'jobId': None,
            'errorType': trace['errorType'],
            'fallbackReason': 'SHARED_INTERPRETATION_FALLBACK',
            'sourceExtractorVersion': EXTRACTOR_VERSION,
        }
        originals = ()
        if document['fileType'] == 'pdf':
            try:
                original = await asyncio.to_thread(parsing._extract_pdf, document['fileName'], content, ocr=False)
                originals = (original,)
            except Exception:
                # Preserve the already verified source-only fallback even
                # when this file has no readable native PDF geometry.
                pass
        result = ReportDocumentSource(
            parsed,
            text,
            document['fileName'],
            'pdf' if document['fileType'] == 'pdf' else 'image',
            (reference,),
            evaluation_original_documents=originals,
        )
        self.recovered[index] = (snapshot, result)
        note_report_documents([reference])
        return result

    async def combine(self, indexes):
        if any(index in self.recovered for index in indexes):
            if any(
                index not in self.recovered
                and _processing(self.outcomes[index]).get('status', 'complete') != 'complete'
                for index in indexes
            ):
                raise DocumentServiceError('cannot combine raw fallback with a quarantined source')
            snapshots, references = [], []
            for index in indexes:
                if index in self.recovered:
                    snapshot, result = self.recovered[index]
                    snapshots.append(snapshot)
                    references.extend(result.references)
                else:
                    outcome = self.outcomes[index]
                    raw = await self.processor.read_document_payload(
                        outcome.document_id,
                        user_id=self.user_id,
                        subject_id=outcome.report['subjectId'],
                        name='original_file',
                    )
                    if hashlib.sha256(raw['content']).hexdigest() != outcome.content_sha256:
                        raise DocumentServiceError('combined source checksum mismatch')
                    snapshots.append(
                        DocumentSnapshot(
                            outcome.document_id,
                            {},
                            outcome.text,
                            outcome.file_name,
                            outcome.resolved_file_type,
                            outcome.content_sha256,
                            raw['content'],
                        )
                    )
                    references.append(_reference(outcome, index))
            parsed = _parsed(await asyncio.to_thread(extract_intrinsic_report_source, tuple(snapshots)))
            parsed.source_errors = ['documentGroup: SHARED_INTERPRETATION_FALLBACK']
            references = [dict(item, groupProcessingStatus='fallback_source_only') for item in references]
            note_report_documents(references)
            return ReportDocumentSource(parsed, '', parsed.source_name or '', 'image', tuple(references))
        outcome = await self.processor.process_documents(
            [{'documentId': self.outcomes[index].document_id} for index in indexes],
            **self._identity(),
            resume_incomplete=False,
        )
        inputs = tuple(self.outcomes[index] for index in indexes)
        processing = _processing(outcome, *inputs)
        parsed = _parsed_output(outcome, location='documentGroup', inputs=inputs)
        references = []
        for index in indexes:
            reference = _reference(self.outcomes[index], index)
            reference['groupExtractionId'] = outcome.extraction_id
            reference['groupOutputTraceId'] = outcome.output_trace_id
            reference['groupCanonicalReportId'] = outcome.canonical_report_id
            reference['groupSourceRevision'] = outcome.source_revision
            reference['groupIngestionStatus'] = outcome.ingestion_status
            reference['groupJobId'] = outcome.ingestion_job_id
            reference['groupProcessingStatus'] = (
                'completed' if processing.get('status', 'complete') == 'complete' else processing['status']
            )
            reference['groupProcessing'] = processing
            references.append(reference)
        note_report_documents(references)
        return ReportDocumentSource(
            parsed,
            '',
            outcome.file_name,
            'image',
            tuple(references),
        )
