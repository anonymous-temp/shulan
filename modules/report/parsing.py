"""Parse one authoritative microbiome source from the unchanged request."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pymupdf
from pydantic import Field

from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY
from modules.report.ports import (
    DocumentTextGateway,
    ReportDocumentGateway,
    DownloadedFile,
    DownloadedTestData,
    SourceGateway,
)
from modules.report.schemas import (
    HospitalDataInput,
    PanelCountAudit,
    ParsedDisease,
    ParsedMicrobiomeReport,
    ParsedTaxon,
    RemoteFileInput,
    ReportRequest,
    SampleInfo,
    SourceMetric,
    SourceSnapshotAudit,
    SubjectInfo,
    stable_id,
)
from utils.log import getLogger

logger = getLogger(__name__)


class SourceParseError(RuntimeError):
    pass


class _NativeSourceMetric(SourceMetric):
    """A positioned result cell's page, excluded from shared serialization."""

    source_pages: list[int] = Field(default_factory=list, exclude=True)
    source_page: int | None = Field(default=None, exclude=True)


MICROBIOME_RESULT_PENDING_SUMMARY = (
    "肠道菌群检测结果尚在生成中，当前暂无法展示异常菌群及相关解读。"
    "检测结果出具后，报告将自动更新。"
)


@dataclass
class ExtractedTable:
    page_number: int
    rows: list[list[str]]
    context: str = ""
    observed_at: str | None = None
    section_title: str | None = None
    native_clinical_columns: bool = False
    native_cell_bounds: dict[int, tuple[tuple[float, float, float, float] | None, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class NativeResultRow:
    locator: str
    page: int
    label: str
    value: str
    unit: str | None
    reference_range: str | None
    flag: str | None
    date: str | None
    result_bounds: tuple[float, float, float, float] | None = None
    name_bounds: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class _NativeProofPage:
    rows: list[NativeResultRow]
    date: str | None
    size: tuple[float, float] | None
    nonpatient_ranges: list[tuple[float, float]]
    nonpatient_ongoing: bool


@dataclass
class ExtractedDocument:
    filename: str
    source_type: str
    text: str
    tables: list[ExtractedTable] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    native_metrics: list[SourceMetric] = field(default_factory=list)
    native_result_rows: tuple[NativeResultRow, ...] = ()
    native_page_dates: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractedClinicalFact:
    """One source-bound fact from an arbitrary clinical document.

    This is deliberately a generic internal representation. It describes the
    shape of a fact, not a catalogue of hospitals, report templates or test
    names. Only facts traceable to source text enter the clinical pipeline.
    """

    category: str
    item_name: str
    value: str | None = None
    unit: str | None = None
    reference_range: str | None = None
    status: str | None = None
    conclusion: str | None = None
    check_time: str | None = None
    source_file: str | None = None
    source_page: int | None = None
    source_text: str | None = None
    confidence: float = 1.0


@dataclass(frozen=True)
class _ParsedSourceCandidate:
    report: ParsedMicrobiomeReport
    priority: int
    order: int


@dataclass(frozen=True)
class _ParsedSampleCandidate:
    sample: SampleInfo
    source_name: str
    source_checksum: str
    order: int
    subject: SubjectInfo = field(default_factory=SubjectInfo)


@dataclass(frozen=True)
class _ParsedClinicalCandidate:
    hospital_data: tuple[HospitalDataInput, ...]
    subject: SubjectInfo
    sample: SampleInfo
    source_name: str
    source_checksum: str
    order: int


def _sample_value_present(value: object) -> bool:
    return value is not None and value != ""


async def parse_request_source(
    request: ReportRequest,
    *,
    source_client: SourceGateway,
    document_client: DocumentTextGateway | None = None,
    document_source: ReportDocumentGateway | None = None,
    enforce_subject_identity: bool = False,
) -> ParsedMicrobiomeReport:
    from .evaluation_native_worker import evaluate_native_scope

    with evaluate_native_scope(enforce_subject_identity):
        return await _parse_request_source_impl(request, source_client=source_client,
            document_client=document_client, document_source=document_source,
            enforce_subject_identity=enforce_subject_identity)


async def attach_fmt_typing_context(
    request: ReportRequest,
    parsed: ParsedMicrobiomeReport,
    *,
    source_client: SourceGateway,
    document_client: DocumentTextGateway | None = None,
) -> ParsedMicrobiomeReport:
    """Read a supplied matching PDF as context, never as a patient test.

    Use the existing guarded downloader and native extraction/OCR boundary.
    The generic health-report processor also publishes health-profile facts;
    a proposed donor/strain plan must not enter that measurement pipeline.
    """
    from .evaluation_request_input import fmt_typing_report_source

    url, marker = fmt_typing_report_source(request)
    if not url:
        return parsed
    from .evaluation_native_worker import evaluate_native_scope
    from .evaluation_sources import subject_mismatch

    try:
        source = RemoteFileInput(url=url, file_type='pdf')
        downloaded = await source_client.fetch_file(source)
        if downloaded.declared_type != 'pdf' or not downloaded.content.startswith(b'%PDF-'):
            raise SourceParseError('FMT matching source must be a PDF')
        try:
            with evaluate_native_scope(True):
                document = await asyncio.to_thread(extract_document, downloaded)
        except (SourceParseError, ValueError):
            if document_client is None:
                raise
            text = await document_client.extract_text(source,
                filename=downloaded.filename, content=downloaded.content)
            document = ExtractedDocument(downloaded.filename, 'pdf', text)
        if not isinstance(document.text, str) or not document.text.strip():
            raise SourceParseError('FMT matching source has no extractable text')
        # Matching documents can mention both donor and recipient. Only an
        # explicitly labelled recipient establishes this document's subject.
        recipient = re.search(r'(?:受者|受体|患者|受检者|受检人|客户)(?:姓名|名称)?[ \t]*[:：][ \t]*([^\r\n:：,，;；]{1,80})',
            document.text)
        recipient_name = re.split(r'\s+(?:性别|年龄|编号)', recipient[1])[0].strip() if recipient else None
        subject = SubjectInfo(name=recipient_name)
        expected = SubjectInfo(name=request.name or parsed.subject.name,
            sex=request.sex or parsed.subject.sex)
        if subject_mismatch(expected, subject):
            raise SourceParseError('FMT matching recipient does not match the report subject')
        checksum = hashlib.sha256(downloaded.content).hexdigest()
        pages = _document_pages_with_numbers(document.text)
        remaining, records = 24000, []
        for page, text in pages:
            if not text.strip():
                continue
            if remaining <= 0:
                break
            selected = text[:remaining]
            records.append(dict(sourceUrl=source.url, sourceName=downloaded.filename,
                sourceChecksum=checksum, sourcePurpose='fmt_typing', sourcePage=page,
                reportDate=_explicit_report_date(document), text=selected,
                originalPageTextLength=len(text), subject=subject.model_dump(mode='json', by_alias=True)))
            remaining -= len(selected)
        if not records:
            raise SourceParseError('FMT matching source has no extractable context')
        truncated = sum(len(row['text']) for row in records) < sum(len(text) for _, text in pages if text.strip())
        if truncated:
            parsed.source_errors.append(marker + ': FMT_TYPING_CONTEXT_TRUNCATED')
        if document.metadata.get('ocr_failed_pages'):
            parsed.source_errors.append(marker + ': FMT_TYPING_OCR_INCOMPLETE')
        parsed.fmt_typing_context = records
        parsed.source_documents.append(dict(sourceUrl=source.url, sourceName=downloaded.filename,
            sourceChecksum=checksum, sourcePurpose='fmt_typing',
            sourcePages=[row['sourcePage'] for row in records],
            processingStatus='partial' if truncated or document.metadata.get('ocr_failed_pages') else 'completed',
            extractionMode='existing_native_text_only'))
    except Exception as error:  # One unavailable auxiliary source cannot discard A/B/C.
        parsed.source_errors.append(marker + ': FMT_TYPING_SOURCE_UNAVAILABLE:' + type(error).__name__)
    return parsed


async def _parse_request_source_impl(
    request: ReportRequest,
    *,
    source_client: SourceGateway,
    document_client: DocumentTextGateway | None = None,
    document_source: ReportDocumentGateway | None = None,
    enforce_subject_identity: bool = False,
) -> ParsedMicrobiomeReport:
    """Parse every independent report and return only the latest snapshot.

    Facts from different dates are never unioned.  Sources that can be shown to
    represent the same report (for example structured JSON plus its PDF) may
    complement missing fields while preserving the structured-source priority.
    Older snapshots remain internal provenance only.
    """
    if (enforce_subject_identity and request.microbiome_report_data is not None
            and (request.microbiome_report_data.data_info_url or request.microbiome_report_data.original_report_url
                or (request.microbiome_report_data.model_extra or {}).get('dataInfo'))):
        from .evaluation_input_source import parse_evaluation_report_source

        return await parse_evaluation_report_source(
            request, source_client=source_client, document_client=document_client,
            document_source=document_source, legacy_parser=parse_request_source,
        )

    if not request.file_list and not request.test_data and not request.microbiome_data:
        return ParsedMicrobiomeReport(
            source_type="pending",
            source_name="microbiome_result_pending",
            subject=SubjectInfo(
                name=request.name,
                age=request.age,
                sex=request.sex,
            ),
            source_summary=MICROBIOME_RESULT_PENDING_SUMMARY,
        )

    errors: list[str] = []
    source_request = request.model_copy(update={"name": None, "age": None, "sex": None}) if enforce_subject_identity else request
    candidates: list[_ParsedSourceCandidate] = []
    sample_candidates: list[_ParsedSampleCandidate] = []
    clinical_candidates: list[_ParsedClinicalCandidate] = []
    image_texts: dict[int, tuple[DownloadedFile, str]] = {}
    document_references: dict[int, dict[str, Any]] = {}
    document_source_issues: list[tuple[int | None, str | None, str]] = []
    source_policy = getattr(
        source_client,
        "policy",
        DEFAULT_REPORT_FALLBACK_POLICY,
    )
    source_slots = asyncio.Semaphore(
        max(1, int(source_policy.source_parse_concurrency))
    )
    if request.microbiome_data:
        try:
            candidates.append(
                _ParsedSourceCandidate(
                    parse_canonical_data(source_request, request.microbiome_data,
                        evaluation_mode=enforce_subject_identity),
                    priority=0,
                    order=0,
                )
            )
        except Exception as exc:  # noqa: BLE001 - try the next caller-supplied source.
            errors.append(f"microbiomeData: {type(exc).__name__}")

    async def parse_test_data_source(index: int):
        async with source_slots:
            item = request.test_data[index]
            try:
                downloaded = await source_client.fetch_test_data(item)
                return _ParsedSourceCandidate(
                    parse_test_data(source_request, downloaded,
                        evaluation_mode=enforce_subject_identity),
                    priority=1,
                    order=index,
                )
            except Exception as exc:  # noqa: BLE001 - try the next caller-supplied source.
                return f"testData[{index}]: {type(exc).__name__}"

    async def parse_file_source(index: int):
        async with source_slots:
            return await _parse_file_source(index)

    async def parse_with_document_service(
        index: int,
        item: Any,
        downloaded: DownloadedFile,
    ) -> _ParsedSourceCandidate | _ParsedClinicalCandidate:
        if document_client is None:
            raise SourceParseError("document service is unavailable")
        text = await document_client.extract_text(
            item,
            filename=downloaded.filename,
            content=downloaded.content,
        )
        if item.file_type == "image":
            image_texts[index] = (downloaded, text)
        checksum = hashlib.sha256(downloaded.content).hexdigest()
        try:
            parsed = parse_extracted_text(
                source_request,
                filename=downloaded.filename,
                source_type=item.file_type,
                text=text,
                checksum=checksum,
            )
        except (SourceParseError, ValueError):
            document = ExtractedDocument(
                filename=downloaded.filename,
                source_type=item.file_type,
                text=text,
            )
            clinical_data = _supplementary_hospital_data_from_text(
                text,
                source_name=downloaded.filename,
            )
            if not clinical_data:
                raise
            return _ParsedClinicalCandidate(
                hospital_data=tuple(clinical_data),
                subject=_document_subject(document),
                sample=_document_sample(document),
                source_name=downloaded.filename,
                source_checksum=checksum,
                order=index,
            )
        return _ParsedSourceCandidate(
            parsed,
            priority=2 if item.file_type == "pdf" else 3,
            order=index,
        )

    async def _parse_file_source(index: int):
        item = request.file_list[index]
        try:
            if document_source is not None:
                outcome = await document_source.read(item, index=index)
                if outcome.references:
                    document_references[index] = dict(outcome.references[0])
                parsed = outcome.parsed.model_copy(deep=True)
                if enforce_subject_identity:
                    from .evaluation_input_source import recover_original_pdf

                    parsed = recover_original_pdf(parsed,
                        getattr(outcome, 'evaluation_original_documents', ()))
                    # Only evaluate consumes verified original clinical cells.
                    # Shared accepted output and other reports stay unchanged.
                    parsed.supplementary_hospital_data.extend(
                        row.model_copy(deep=True) for row in getattr(outcome, "evaluation_clinical_rows", ())
                    )
                document_source_issues.extend((index, parsed.source_checksum, issue) for issue in parsed.source_errors)
                if not enforce_subject_identity:
                    parsed.subject = _merge_subjects(_subject(request), parsed.subject)
                if outcome.file_type == "image":
                    image_texts[index] = (
                        DownloadedFile(outcome.filename, "", b"", "image"),
                        outcome.text,
                    )
                if parsed.supplementary_hospital_data or parsed.source_type == "pending":
                    clinical_candidates.append(_ParsedClinicalCandidate(
                        tuple(parsed.supplementary_hospital_data), parsed.subject,
                        parsed.sample, parsed.source_name, parsed.source_checksum, index,
                    ))
                if parsed.source_type == "pending":
                    return _ParsedSampleCandidate(
                        parsed.sample, parsed.source_name, parsed.source_checksum, index,
                        subject=parsed.subject,
                    )
                return _ParsedSourceCandidate(parsed, 2 if outcome.file_type == "pdf" else 3, index)
            if item.document_id:
                raise SourceParseError("shared document reference service is unavailable")
            downloaded = await source_client.fetch_file(item)
            try:
                # Local parsing preserves provider tables, row relationships
                # and visual abnormal markers that a general document OCR
                # response can flatten or omit.  It must therefore be the
                # primary path even for hybrid PDFs; the local extractor
                # already performs page-level OCR where needed.  The external
                # document service is the fallback for genuinely scan-only or
                # otherwise unreadable uploads.
                if item.file_type == "image":
                    document = await asyncio.to_thread(extract_document, downloaded)
                    image_texts[index] = (downloaded, document.text)
                    parsed = parse_extracted_text(
                        source_request,
                        filename=downloaded.filename,
                        source_type=item.file_type,
                        text=document.text,
                        checksum=hashlib.sha256(downloaded.content).hexdigest(),
                    )
                else:
                    parsed = await asyncio.to_thread(
                        parse_downloaded_document,
                        source_request,
                        downloaded,
                    )
                    if enforce_subject_identity and item.file_type == "pdf":
                        from .evaluation_input_source import recover_original_pdf

                        try:
                            original = await asyncio.to_thread(_extract_pdf,
                                downloaded.filename, downloaded.content, ocr=False)
                        except Exception:
                            original = None
                        if original is not None:
                            parsed = recover_original_pdf(parsed, (original,))
            except Exception:
                # A supplementary PDF may contain clinical facts but no
                # microbiome rows.  In that case, keep the structured local
                # extraction before considering the external OCR service: a
                # flattened OCR response can otherwise associate a value with
                # the neighbouring test item.  The external service remains
                # the fallback for genuinely unreadable or scan-only files.
                document = None
                clinical_data = []
                try:
                    document = await asyncio.to_thread(extract_document, downloaded)
                    clinical_data = _supplementary_hospital_data_from_document(
                        document
                    )
                except Exception as document_exc:  # noqa: BLE001 - fallback below.
                    logger.debug(
                        "local supplementary extraction failed: %s",
                        downloaded.filename,
                        exc_info=document_exc,
                    )
                if clinical_data:
                    return _ParsedClinicalCandidate(
                        hospital_data=tuple(clinical_data),
                        subject=_document_subject(document),
                        sample=_document_sample(document),
                        source_name=downloaded.filename,
                        source_checksum=hashlib.sha256(downloaded.content).hexdigest(),
                        order=index,
                    )
                if document_client is not None:
                    try:
                        return await parse_with_document_service(index, item, downloaded)
                    except Exception as external_exc:
                        logger.warning(
                            "local and external document parsing failed: %s",
                            downloaded.filename,
                            exc_info=external_exc,
                        )
                sample = _document_sample(document) if document is not None else None
                if sample is not None and any(
                    _sample_value_present(getattr(sample, field))
                    for field in SampleInfo.model_fields
                ):
                    return _ParsedSampleCandidate(
                        sample=sample,
                        source_name=downloaded.filename,
                        source_checksum=hashlib.sha256(downloaded.content).hexdigest(),
                        order=index,
                        subject=_document_subject(document) if document is not None else SubjectInfo(),
                    )
                raise
            return _ParsedSourceCandidate(
                parsed,
                priority=2 if item.file_type == "pdf" else 3,
                order=index,
            )
        except Exception as exc:  # noqa: BLE001 - malformed sources are isolated.
            if document_source is not None:
                document_references[index] = {
                    "fileIndex": index, "documentId": item.document_id,
                    "processingStatus": "failed", "ingestionStatus": "unknown",
                    "errorType": type(exc).__name__,
                }
            return f"fileList[{index}]({item.file_type}): {type(exc).__name__}"

    async_results = await asyncio.gather(
        *(parse_test_data_source(index) for index in range(len(request.test_data))),
        *(parse_file_source(index) for index in range(len(request.file_list))),
    )
    for result in async_results:
        if isinstance(result, _ParsedSourceCandidate):
            candidates.append(result)
        elif isinstance(result, _ParsedSampleCandidate):
            sample_candidates.append(result)
        elif isinstance(result, _ParsedClinicalCandidate):
            clinical_candidates.append(result)
        else:
            errors.append(result)

    # Mobile callers commonly upload a long report as consecutive page
    # screenshots.  Middle/continuation pages may contain only part of a table
    # and cannot be interpreted as independent reports.  Reconstruct only a
    # structurally continuous run beginning with a clear report header.
    if document_source is None:
        reconstructed, consumed_image_indexes = _consecutive_image_candidates(source_request, image_texts)
    else:
        reconstructed, consumed_image_indexes = [], set()
        for group in _continuous_image_groups(image_texts):
            indexes = [index for index, _downloaded, _text in group]
            try:
                outcome = await document_source.combine(indexes)
                parsed_group = outcome.parsed.model_copy(deep=True)
                document_source_issues.extend((None, parsed_group.source_checksum, issue) for issue in parsed_group.source_errors)
                if parsed_group.supplementary_hospital_data:
                    clinical_candidates.append(_ParsedClinicalCandidate(
                        tuple(parsed_group.supplementary_hospital_data), parsed_group.subject,
                        parsed_group.sample, parsed_group.source_name, parsed_group.source_checksum, indexes[0],
                    ))
                if parsed_group.source_type == "pending":
                    continue
                if not enforce_subject_identity:
                    parsed_group.subject = _merge_subjects(_subject(request), parsed_group.subject)
                reconstructed.append(_ParsedSourceCandidate(parsed_group, 3, indexes[0] - len(request.file_list)))
                consumed_image_indexes.update(indexes)
                for reference in outcome.references:
                    document_references[reference["fileIndex"]] = dict(reference)
            except Exception as exc:
                errors.append(f"documentGroup{indexes}: {type(exc).__name__}")
    if consumed_image_indexes:
        candidates = [
            candidate
            for candidate in candidates
            if not (
                candidate.priority == 3 and candidate.order in consumed_image_indexes
            )
        ]
        errors = [
            error
            for error in errors
            if not any(
                error.startswith(f"fileList[{index}]")
                for index in consumed_image_indexes
            )
        ]
    candidates.extend(reconstructed)

    expected = _subject(request)
    excluded_sources = []
    rejected_checksums = set()
    candidate_key = _evaluation_candidate_recency_key if enforce_subject_identity else _candidate_recency_key
    if enforce_subject_identity:
        from modules.report.evaluation_sources import subject_mismatch

        # JSON personal fields define the subject; only an otherwise compatible
        # supplied document can fill a missing field. Age changes are not an
        # identity conflict, and unnamed continuation pages stay eligible.
        subjects = [item.report.subject for item in sorted(candidates, key=candidate_key, reverse=True)]
        subjects.extend(item.subject for item in sorted(
            [*clinical_candidates, *sample_candidates],
            key=lambda item: _evaluation_sample_recency_key(item.sample), reverse=True))
        matching = [item for item in subjects if not subject_mismatch(expected, item)]
        anchor = next((item for item in matching if item.name and item.name.strip()), None)
        if anchor is not None:
            expected = _merge_subjects(expected, anchor)

        def belongs(subject, name, checksum):
            if subject_mismatch(expected, subject):
                if not any(item.get("sourceChecksum") == checksum and item["sourceName"] == name
                    for item in excluded_sources):
                    excluded_sources.append({"sourceName": name, "sourceChecksum": checksum,
                        "reason": "different_subject"})
                if checksum:
                    rejected_checksums.add(checksum)
                return False
            return True

        candidates = [item for item in candidates if belongs(
            item.report.subject, item.report.source_name, item.report.source_checksum)]
        clinical_candidates = [item for item in clinical_candidates if belongs(
            item.subject, item.source_name, item.source_checksum)]
        sample_candidates = [item for item in sample_candidates if belongs(
            item.subject, item.source_name, item.source_checksum)
            and item.source_checksum not in rejected_checksums]
        candidates = [item for item in candidates if item.report.source_checksum not in rejected_checksums]
        clinical_candidates = [item for item in clinical_candidates if item.source_checksum not in rejected_checksums]
        for reference in document_references.values():
            if reference.get("contentSha256") in rejected_checksums or reference.get("sourceChecksum") in rejected_checksums:
                reference["evaluationDisposition"] = "different_subject_excluded"

    errors.extend(issue for index, checksum, issue in document_source_issues
        if checksum not in rejected_checksums and (index is None or index not in consumed_image_indexes))

    if not candidates:
        # The request itself is already valid.  Caller-supplied optional
        # sources are therefore isolated failures rather than a terminal
        # report error.  Preserve subject/clinical facts and let the fixed
        # callback expose an empty microbiome section with partial_success.
        pending = ParsedMicrobiomeReport(
            source_type="pending",
            source_name="microbiome_source_unavailable",
            subject=_merge_subjects(expected, _request_and_document_subject(request, clinical_candidates, sample_candidates)),
            sample=_latest_clinical_sample(
                [*clinical_candidates, *sample_candidates] if enforce_subject_identity else clinical_candidates,
                latest_report=enforce_subject_identity,
            ),
            source_summary=MICROBIOME_RESULT_PENDING_SUMMARY,
            source_errors=errors,
            source_documents=[document_references[i] for i in sorted(document_references)],
            supplementary_hospital_data=_merge_supplementary_hospital_data(
                clinical_candidates, preserve_evaluation_sources=enforce_subject_identity
            ),
        )
        pending._evaluation_source_audit = excluded_sources
        return pending

    raw_candidates = list(candidates)
    consolidated = _consolidate_same_report_sources(candidates)
    selected = max(consolidated, key=candidate_key).report.model_copy(
        deep=True
    )
    selected = _enrich_sample_from_pdf_candidates(
        selected,
        raw_candidates,
        sample_candidates,
    )
    selected.subject = _merge_subjects(
        expected,
        selected.subject,
        *(
            candidate.subject
            for candidate in sorted(clinical_candidates, key=lambda item: item.order)
        ),
        *(candidate.subject for candidate in sorted(sample_candidates, key=lambda item: item.order)),
    )
    selected.source_errors = errors
    selected._evaluation_source_audit = [*selected._evaluation_source_audit, *excluded_sources]
    selected.source_microbiome_terms = list(dict.fromkeys(
        name for candidate in raw_candidates
        for name in [
            *candidate.report.source_microbiome_terms,
            *(n for taxon in candidate.report.taxa for n in (taxon.chinese_name, taxon.latin_name)),
            *(metric.name for metric in candidate.report.metrics),
            *(disease.name for disease in candidate.report.disease_results),
        ] if name
    ))
    selected.source_documents = [document_references[i] for i in sorted(document_references)]
    selected.supplementary_hospital_data = _merge_supplementary_hospital_data(
        clinical_candidates, preserve_evaluation_sources=enforce_subject_identity
    )
    selected.source_history = [
        SourceSnapshotAudit(
            source_type=candidate.report.source_type,
            source_name=candidate.report.source_name,
            source_checksum=candidate.report.source_checksum,
            sample=candidate.report.sample,
            metrics=[item.model_copy(deep=True) for item in candidate.report.metrics],
            taxon_count=len(candidate.report.taxa),
            disease_count=len(candidate.report.disease_results),
            selected=(
                candidate.report.source_type == selected.source_type
                and candidate.report.source_name == selected.source_name
                and candidate.report.source_checksum == selected.source_checksum
            ),
            date_unresolved=_report_date_value(candidate.report) == 0,
        )
        for candidate in raw_candidates
    ] + [
        SourceSnapshotAudit(
            source_type="pdf",
            source_name=candidate.source_name,
            source_checksum=candidate.source_checksum,
            sample=candidate.sample,
            taxon_count=0,
            disease_count=0,
            selected=False,
            date_unresolved=_sample_recency_key(candidate.sample) == 0,
        )
        for candidate in sample_candidates
    ]
    return selected


def _merge_supplementary_hospital_data(
    candidates: list[_ParsedClinicalCandidate],
    *, preserve_evaluation_sources: bool = False,
) -> list[HospitalDataInput]:
    output: list[HospitalDataInput] = []
    seen: set[tuple] = set()
    for candidate in sorted(candidates, key=lambda item: item.order):
        for item in candidate.hospital_data:
            key = (
                item.data_type,
                item.item_name,
                str(item.test_result_value or ""),
                str(item.exam_result or ""),
                item.check_time, item.report_time, item.reference_range,
            )
            if preserve_evaluation_sources:
                from modules.report.evaluation_sources import _comparison_key
                extra = item.model_extra or {}
                key += (_comparison_key(item), extra.get("sourceFile"),
                    extra.get("sourceChecksum"), extra.get("sourcePage"))
            if key in seen:
                continue
            seen.add(key)
            output.append(item)
    return output


def _merge_subjects(*subjects: SubjectInfo) -> SubjectInfo:
    """Merge explicit subject facts without overwriting caller values."""

    return SubjectInfo(
        name=next((item.name for item in subjects if item.name and item.name.strip()), None),
        age=next((item.age for item in subjects if item.age is not None), None),
        sex=next((item.sex for item in subjects if item.sex and item.sex.strip()), None),
    )


def _request_and_document_subject(
    request: ReportRequest,
    candidates: list[_ParsedClinicalCandidate],
    sample_candidates: list[_ParsedSampleCandidate],
) -> SubjectInfo:
    return _merge_subjects(
        _subject(request),
        *(
            candidate.subject
            for candidate in sorted([*candidates, *sample_candidates], key=lambda item: item.order)
        ),
    )


def _latest_clinical_sample(
    candidates: list[_ParsedClinicalCandidate | _ParsedSampleCandidate],
    *,
    latest_report: bool = False,
) -> SampleInfo:
    samples = [
        candidate.sample
        for candidate in candidates
        if any(
            _sample_value_present(getattr(candidate.sample, field))
            for field in SampleInfo.model_fields
        )
    ]
    key = _evaluation_sample_recency_key if latest_report else _sample_recency_key
    return max(samples, key=key) if samples else SampleInfo()


def _supplementary_hospital_data_from_text(
    text: str,
    *,
    source_name: str | None = None,
) -> list[HospitalDataInput]:
    """Normalize explicit facts from a non-microbiome clinical document.

    Extraction is structure-led rather than hospital-, template- or test-name
    specific. It retains only source wording, explicit values/ranges/statuses
    and report conclusions; it never infers a diagnosis from an unknown item.
    The resulting ordinary ``HospitalDataInput`` rows flow through the existing
    scoring, narrative and evidence-applicability logic.
    """

    cleaned = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    report_date = _labeled_value(
        cleaned,
        ("体检日期", "检测日期", "报告日期"),
        r"\d{4}[-_/.年]\d{1,2}[-_/.月]\d{1,2}日?",
    )
    rows: list[HospitalDataInput] = []
    body_fat_status = (
        "肥胖"
        if re.search(r"体脂(?:率|百分比)\s*[:：]?\s*肥胖", cleaned)
        else "轻度肥胖"
        if re.search(r"体脂(?:率|百分比)[^\n]{0,24}轻度肥胖", cleaned)
        else None
    )
    bmi_status = (
        "严重超重"
        if re.search(r"(?:BMI|体重指数)[^\n]{0,50}严重超重", cleaned, flags=re.IGNORECASE)
        else "超重"
        if re.search(r"(?:BMI|体重指数)[^\n]{0,50}超重", cleaned, flags=re.IGNORECASE)
        else None
    )

    def add_measurement(
        item_name: str,
        value: str,
        unit: str,
        *,
        status: str | None = None,
    ) -> None:
        rows.append(
            HospitalDataInput(
                data_type="INSPECTION",
                item_name=item_name,
                test_result_value=f"{value} {unit}".strip(),
                result_status_name=status,
                report_time=report_date,
            )
        )

    labelled_patterns = (
        (
            "BMI",
            r"(?:BMI(?:指数)?|体重指数)\s*[:：]?\s*(\d{1,2}(?:\.\d+)?)",
            "kg/m²",
        ),
        ("身高", r"身高\s*[:：]?\s*(\d{2,3}(?:\.\d+)?)\s*(cm|厘米)", "cm"),
        (
            "体重",
            r"体(?:重|质量)\s*[:：]?\s*(\d{2,3}(?:\.\d+)?)\s*(kg|千克|公斤)",
            "kg",
        ),
        ("腰围", r"腰围\s*[:：]?\s*(\d{2,3}(?:\.\d+)?)\s*(cm|厘米)", "cm"),
        (
            "收缩压",
            r"血压\s*\(收缩压\)\s*[:：]?\s*(\d{2,3}(?:\.\d+)?)\s*mmHg",
            "mmHg",
        ),
        (
            "舒张压",
            r"血压\s*\(舒张压\)\s*[:：]?\s*(\d{2,3}(?:\.\d+)?)\s*mmHg",
            "mmHg",
        ),
        ("心率", r"心率\s*[:：]?\s*(\d{2,3}(?:\.\d+)?)\s*(?:次/分|bpm)", "次/分"),
        ("体脂率", r"体脂(?:率|百分比)\s*[:：]?\s*(\d{1,2}(?:\.\d+)?)\s*%", "%"),
        ("骨骼肌量", r"骨骼肌[^\n]{0,200}?(\d{2}\.\d+)", "kg"),
        ("肝脏硬度", r"肝脏硬度\s*\(kpa\)[\s\S]{0,80}?中位数\s*(\d+(?:\.\d+)?)", "kPa"),
        (
            "脂肪衰减",
            r"脂肪衰减\s*\(db/m\)[\s\S]{0,80}?中位数\s*(\d+(?:\.\d+)?)",
            "dB/m",
        ),
    )
    found: dict[str, str] = {}
    for item_name, pattern, unit in labelled_patterns:
        match = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if not match:
            continue
        value = match.group(1)
        found[item_name] = value
        add_measurement(
            item_name,
            value,
            unit,
            status=(
                body_fat_status
                if item_name == "体脂率"
                else bmi_status
                if item_name == "BMI"
                else None
            ),
        )

    # Some InBody exports provide fat mass but OCR loses the separately printed
    # percentage.  Derive the percentage only when both labelled masses are
    # explicit; this is arithmetic, not a clinical assumption.
    if "体脂率" not in found and "体重" in found:
        fat_mass = re.search(
            r"体脂肪[^\n]{0,50}?\(kg\)[^\n]{0,30}?(\d{1,3}(?:\.\d+)?)",
            cleaned,
            flags=re.IGNORECASE,
        )
        if fat_mass:
            weight = float(found["体重"])
            fat = float(fat_mass.group(1))
            body_fat_percent = fat / weight * 100 if weight else 0
            # OCR may attach an unrelated neighbouring value to the fat-mass
            # label. Reject physiologically implausible arithmetic results
            # instead of exposing them as an inferred measurement.
            if (
                20 <= weight <= 400
                and 1 <= fat < weight
                and 3 <= body_fat_percent <= 70
            ):
                add_measurement(
                    "体脂率（由体脂肪量和体重计算）",
                    f"{body_fat_percent:.1f}",
                    "%",
                    status=body_fat_status,
                )

    priority = {
        "BMI": 0,
        "体脂率": 1,
        "体脂率（由体脂肪量和体重计算）": 1,
        "肝脏硬度": 2,
        "脂肪衰减": 3,
        "体重": 4,
        "身高": 5,
        "骨骼肌量": 6,
    }
    for fact in _generic_clinical_facts_from_text(
        cleaned,
        source_name=source_name,
        report_date=report_date,
    ):
        rows.append(_clinical_fact_to_hospital_data(fact))

    rows = _dedupe_hospital_rows(rows)
    rows.sort(key=lambda item: priority.get(item.item_name, 99))

    return rows


_PAGE_MARKER_RE = re.compile(r"^===== PAGE (?P<page>\d+) =====$", re.MULTILINE)
_RESULT_VALUE_RE = re.compile(
    r"^(?:[<>≤≥]\s*)?-?\d+(?:\.\d+)?(?:\s*[Ee][+-]?\d+)?%?$"
)
_REFERENCE_VALUE_RE = re.compile(
    r"^(?:-|阴性|未检出|(?:[<>≤≥]\s*)?-?\d+(?:\.\d+)?"
    r"(?:\s*[-~～至–—]\s*(?:[<>≤≥]\s*)?-?\d+(?:\.\d+)?)?)$"
)
_CATEGORICAL_RESULT_RE = re.compile(
    r"^(?:阴性|阳性|弱阳性|未检出|检出|正常|异常|偏高|偏低|升高|降低)$"
)
_SUMMARY_START_RE = re.compile(
    r"(?:主要|重要|本次|健康体检)?(?:异常结果|异常项目|阳性结果|阳性发现|"
    r"体检结论|体检小结|总检结论|检查结论)"
)
_SUMMARY_STOP_RE = re.compile(
    r"(?:健康建议|医生建议|生活建议|温馨提示|注意事项|健康指导|声明|报告说明)"
)


def _dedupe_hospital_rows(rows: list[HospitalDataInput]) -> list[HospitalDataInput]:
    output: list[HospitalDataInput] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in rows:
        key = (
            item.data_type,
            re.sub(r"\s+", "", item.item_name).casefold(),
            re.sub(r"\s+", "", str(item.test_result_value or "")).casefold(),
            re.sub(r"\s+", "", str(item.exam_result or "")).casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _clinical_fact_to_hospital_data(
    fact: ExtractedClinicalFact,
) -> HospitalDataInput:
    payload: dict[str, Any] = {
        "data_type": "INSPECTION" if fact.category == "laboratory" else "EXAM",
        "item_name": fact.item_name[:512],
        "report_time": fact.check_time,
        "source_file": fact.source_file,
        "source_page": fact.source_page,
        "source_text": fact.source_text,
        "fact_category": fact.category,
        "confidence": round(max(0.0, min(1.0, fact.confidence)), 3),
    }
    if fact.category == "laboratory":
        value = fact.value
        if value is not None and fact.unit and fact.unit not in value:
            value = f"{value} {fact.unit}".strip()
        payload.update(
            {
                "test_result_value": value,
                "reference_range": fact.reference_range,
                "result_status_name": fact.status,
            }
        )
    else:
        payload["exam_result"] = fact.conclusion or fact.value or fact.source_text
    return HospitalDataInput.model_validate(payload)


def _document_pages_with_numbers(text: str) -> list[tuple[int | None, str]]:
    matches = list(_PAGE_MARKER_RE.finditer(text))
    if not matches:
        return [(None, text)]
    pages: list[tuple[int | None, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        pages.append((int(match.group("page")), text[match.end() : end]))
    return pages


def _clean_document_line(value: str) -> str:
    return " ".join(value.replace("\u3000", " ").split()).strip(" |\t")


def _looks_like_item_name(value: str) -> bool:
    compact = _clean_document_line(value)
    if not 1 < len(compact) <= 100 or _RESULT_VALUE_RE.fullmatch(compact):
        return False
    if _REFERENCE_VALUE_RE.fullmatch(compact) or _CATEGORICAL_RESULT_RE.fullmatch(
        compact
    ):
        return False
    if compact.startswith("=====") or re.fullmatch(r"\d+", compact):
        return False
    return bool(re.search(r"[A-Za-z\u3400-\u9fff]", compact))


def _looks_like_unit(value: str) -> bool:
    compact = _clean_document_line(value)
    if not 0 < len(compact) <= 24:
        return False
    if _RESULT_VALUE_RE.fullmatch(compact) or _REFERENCE_VALUE_RE.fullmatch(compact):
        return False
    return bool(
        re.fullmatch(
            r"[%‰A-Za-zµμΩ℃个次亿升毫克克摩尔平方米厘米"
            r"0-9^/().·−+-]+",
            compact,
            flags=re.IGNORECASE,
        )
    )


def _status_from_source(value: str) -> str | None:
    if "↑" in value or re.search(r"(?:偏高|升高|增高|高于)", value):
        return "偏高"
    if "↓" in value or re.search(r"(?:偏低|降低|低于)", value):
        return "偏低"
    if re.search(r"(?:弱阳性|阳性|异常|检出)", value) and not re.search(
        r"(?:阴性|未检出)", value
    ):
        return "异常"
    if re.search(r"(?:阴性|未检出|正常)", value):
        return "正常"
    return None


def _generic_clinical_facts_from_text(
    text: str,
    *,
    source_name: str | None,
    report_date: str | None,
) -> list[ExtractedClinicalFact]:
    """Extract source-bound facts without a hospital or test-name whitelist."""

    facts: list[ExtractedClinicalFact] = []
    for page_number, page_text in _document_pages_with_numbers(text):
        lines = [
            line
            for raw in page_text.splitlines()
            if (line := _clean_document_line(raw))
        ]
        markdown_tables = _markdown_tables(page_text, page_number=page_number)
        facts.extend(
            _generic_inline_laboratory_facts(
                lines,
                source_name=source_name,
                source_page=page_number,
                report_date=report_date,
            )
        )
        if not markdown_tables:
            facts.extend(
                _generic_sequential_laboratory_facts(
                    lines,
                    source_name=source_name,
                    source_page=page_number,
                    report_date=report_date,
                )
            )
        facts.extend(
            _generic_summary_facts(
                lines,
                source_name=source_name,
                source_page=page_number,
                report_date=report_date,
            )
        )
        markdown_document = ExtractedDocument(
            filename=source_name or "uploaded-clinical-document",
            source_type="document_text",
            text=page_text,
            tables=markdown_tables,
        )
        facts.extend(
            _generic_clinical_facts_from_tables(
                markdown_document,
                report_date=report_date,
            )
        )
    unique: dict[tuple[str, str, str, str], ExtractedClinicalFact] = {}
    for fact in facts:
        key = (
            fact.category,
            re.sub(r"\s+", "", fact.item_name).casefold(),
            re.sub(r"\s+", "", fact.value or "").casefold(),
            re.sub(r"\s+", "", fact.conclusion or "").casefold(),
        )
        previous = unique.get(key)
        if previous is None or fact.confidence > previous.confidence:
            unique[key] = fact
    return list(unique.values())


def _generic_inline_laboratory_facts(
    lines: list[str],
    *,
    source_name: str | None,
    source_page: int | None,
    report_date: str | None,
) -> list[ExtractedClinicalFact]:
    facts: list[ExtractedClinicalFact] = []
    pattern = re.compile(
        r"^(?P<name>[^:：]{2,100})[:：]\s*"
        r"(?P<value>[<>≤≥]?-?\d+(?:\.\d+)?(?:\s*[Ee][+-]?\d+)?)\s*"
        r"(?P<arrow>[↑↓▲▼])?\s*"
        r"(?P<unit>[%‰A-Za-zµμ0-9^/().·−+-]{0,24})?\s*"
        r"(?P<tail>.*)$",
        flags=re.IGNORECASE,
    )
    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        name = _clean_document_line(match.group("name")).strip(" *")
        if not _looks_like_item_name(name) or re.search(
            r"(?:姓名|年龄|日期|编号|电话|页码|身份)", name
        ):
            continue
        tail = _clean_document_line(match.group("tail") or "")
        if len(tail) > 120 or re.search(
            r"(?:血脂异常|检查结论|体检结论|健康建议)$",
            name,
        ):
            continue
        reference_match = re.search(
            r"(?:参考(?:范围|值)?\s*[:：]?)?"
            r"(?P<reference>(?:[<>≤≥]\s*)?-?\d+(?:\.\d+)?"
            r"\s*[-~～至–—]\s*(?:[<>≤≥]\s*)?-?\d+(?:\.\d+)?)",
            tail,
        )
        source_status = _status_from_source(line)
        if not source_status and not reference_match:
            continue
        facts.append(
            ExtractedClinicalFact(
                category="laboratory",
                item_name=name,
                value=match.group("value").replace(" ", ""),
                unit=(match.group("unit") or None),
                reference_range=(
                    reference_match.group("reference").replace(" ", "")
                    if reference_match
                    else None
                ),
                status=source_status,
                check_time=report_date,
                source_file=source_name,
                source_page=source_page,
                source_text=line,
                confidence=0.9,
            )
        )
    return facts


def _markdown_tables(
    text: str,
    *,
    page_number: int | None,
) -> list[ExtractedTable]:
    tables: list[ExtractedTable] = []
    current: list[list[str]] = []
    current_start: int | None = None

    def flush() -> None:
        nonlocal current_start
        # Keep a single-row continuation. Page headers frequently split a
        # valid source table immediately before its final row; downstream
        # semantic parsers still require a matching section/header context.
        if current:
            tables.append(
                ExtractedTable(
                    page_number=page_number or 1,
                    rows=[list(row) for row in current],
                    context=text[max(0, (current_start or 0) - 4000) : current_start],
                )
            )
        current.clear()
        current_start = None

    offset = 0
    for raw in text.splitlines(keepends=True):
        line = raw.strip()
        if line.startswith("|") and line.count("|") >= 2:
            if current_start is None:
                current_start = offset
            cells = [
                _clean_markdown_cell(cell)
                for cell in line.strip("|").split("|")
            ]
            if cells and all(
                not cell or re.fullmatch(r":?-{2,}:?", cell) for cell in cells
            ):
                offset += len(raw)
                continue
            current.append(cells)
        else:
            flush()
        offset += len(raw)
    flush()
    return tables


def _clean_markdown_cell(value: str) -> str:
    cleaned = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"</?(?:p|span|div)\b[^>]*>",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    return " ".join(cleaned.replace("&nbsp;", " ").split())


def _generic_sequential_laboratory_facts(
    lines: list[str],
    *,
    source_name: str | None,
    source_page: int | None,
    report_date: str | None,
) -> list[ExtractedClinicalFact]:
    facts: list[ExtractedClinicalFact] = []
    index = 0
    while index + 2 < len(lines):
        name = lines[index]
        if not _looks_like_item_name(name):
            index += 1
            continue
        value = lines[index + 1].replace(" ", "")
        if not (
            _RESULT_VALUE_RE.fullmatch(value)
            or _CATEGORICAL_RESULT_RE.fullmatch(value)
        ):
            index += 1
            continue
        following = lines[index + 2 : index + 5]
        reference_index = next(
            (
                offset
                for offset, candidate in enumerate(following)
                if _REFERENCE_VALUE_RE.fullmatch(candidate.replace(" ", ""))
            ),
            None,
        )
        if reference_index is None:
            # A categorical abnormal result is still explicit even when the
            # report omits a separate reference-range cell.
            if not _CATEGORICAL_RESULT_RE.fullmatch(value):
                index += 1
                continue
            reference = None
            unit = None
            consumed = 2
        else:
            reference = following[reference_index].replace(" ", "")
            other = [
                candidate
                for offset, candidate in enumerate(following[: reference_index + 2])
                if offset != reference_index
            ]
            unit = next(
                (
                    candidate
                    for candidate in other
                    if _looks_like_unit(candidate)
                ),
                None,
            )
            consumed = 2 + reference_index + 1
        source_lines = lines[index : min(len(lines), index + consumed + 1)]
        facts.append(
            ExtractedClinicalFact(
                category="laboratory",
                item_name=name,
                value=value,
                unit=unit,
                reference_range=reference,
                status=_status_from_source(" ".join(source_lines)),
                check_time=report_date,
                source_file=source_name,
                source_page=source_page,
                source_text=" | ".join(source_lines),
                confidence=0.9 if reference is not None else 0.82,
            )
        )
        index += max(2, consumed)
    return facts


def _generic_summary_facts(
    lines: list[str],
    *,
    source_name: str | None,
    source_page: int | None,
    report_date: str | None,
) -> list[ExtractedClinicalFact]:
    start = next((index for index, line in enumerate(lines) if _SUMMARY_START_RE.search(line)), None)
    if start is None:
        return []
    facts: list[ExtractedClinicalFact] = []
    current: list[str] = []

    def flush() -> None:
        if not current:
            return
        conclusion = _clean_document_line(" ".join(current))
        if not conclusion or len(conclusion) > 1000:
            current.clear()
            return
        item_name = re.split(r"[:：;；。]", conclusion, maxsplit=1)[0]
        item_name = re.sub(r"^\s*(?:\d{1,3}[、.)．]|[（(]\d{1,3}[）)])\s*", "", item_name)
        item_name = item_name.strip(" -*")[:120]
        if _looks_like_item_name(item_name):
            facts.append(
                ExtractedClinicalFact(
                    category="examination",
                    item_name=item_name,
                    conclusion=conclusion,
                    status=_status_from_source(conclusion),
                    check_time=report_date,
                    source_file=source_name,
                    source_page=source_page,
                    source_text=conclusion,
                    confidence=0.86,
                )
            )
        current.clear()

    for line in lines[start + 1 :]:
        if _SUMMARY_STOP_RE.search(line):
            break
        # Markdown table cells are handled structurally below. Treating their
        # separator rows (``| --- |``) as list bullets created large amounts
        # of header and layout noise in supplementary clinical facts.
        if "|" in line or re.fullmatch(r"[-—–_=]{2,}", line):
            continue
        if re.search(
            r"^(?:姓名|性别|年龄|体检日期|报告日期|联系电话|身份证|第\s*\d+\s*页)",
            line,
        ):
            continue
        numbered = re.match(r"^\s*(?:\d{1,3}[、.)．]|[（(]\d{1,3}[）)])\s*(.+)$", line)
        bullet = re.match(
            r"^\s*[•·●▪▶◆*-]\s*(?P<text>.*[A-Za-z\u3400-\u9fff].*)$",
            line,
        )
        if numbered or bullet:
            flush()
            current.append(
                numbered.group(1) if numbered else bullet.group("text")
            )
        elif current and len(line) <= 240:
            current.append(line)
    flush()
    return facts[:100]


def _generic_clinical_facts_from_tables(
    document: ExtractedDocument,
    *,
    report_date: str | None,
) -> list[ExtractedClinicalFact]:
    facts: list[ExtractedClinicalFact] = []
    for table in document.tables:
        if len(table.rows) < 2:
            continue
        header_index, role_groups = _clinical_table_role_groups(table.rows)
        if header_index is None:
            continue
        for roles in role_groups:
            if "conclusion" in roles and "value" not in roles:
                for row in table.rows[header_index + 1 :]:
                    conclusion = _clean(_cell(row, roles["conclusion"])) or ""
                    if not conclusion or _SUMMARY_STOP_RE.search(conclusion):
                        continue
                    name = re.split(r"[:：,，;；。]", conclusion, maxsplit=1)[0]
                    name = name.strip(" -*")[:120]
                    if not _looks_like_item_name(name) or any(
                        marker in name
                        for marker in (
                            "人体成分测试历史记录",
                            "肌肉脂肪分析",
                            "细胞外水分比率分析",
                            "根据理想体重",
                            "体脂肪 (kg)",
                        )
                    ):
                        continue
                    source_text = " | ".join(
                        _clean(cell) or "" for cell in row
                    ).strip(" |")
                    facts.append(
                        ExtractedClinicalFact(
                            category="examination",
                            item_name=name,
                            conclusion=conclusion,
                            status=_status_from_source(conclusion),
                            check_time=report_date,
                            source_file=document.filename,
                            source_page=table.page_number,
                            source_text=source_text,
                            confidence=0.94,
                        )
                    )
                continue
            if "name" not in roles or "value" not in roles:
                continue
            for row in table.rows[header_index + 1 :]:
                name = _clean(_cell(row, roles["name"])) or ""
                value = _clean(_cell(row, roles["value"])) or ""
                if not _looks_like_item_name(name) or not value:
                    continue
                unit = _clean(_cell(row, roles.get("unit", -1))) if "unit" in roles else None
                reference = (
                    _clean(_cell(row, roles.get("reference", -1)))
                    if "reference" in roles
                    else None
                )
                status_text = (
                    _clean(_cell(row, roles.get("status", -1)))
                    if "status" in roles
                    else None
                )
                source_status = _status_from_source(f"{value} {status_text or ''}")
                if source_status is None:
                    source_status = _status_from_value_and_reference(value, reference)
                source_text = " | ".join(_clean(cell) or "" for cell in row).strip(" |")
                facts.append(
                    ExtractedClinicalFact(
                        category="laboratory",
                        item_name=name,
                        value=value.replace("↑", "").replace("↓", "").strip(),
                        unit=unit,
                        reference_range=reference,
                        status=source_status,
                        check_time=report_date,
                        source_file=document.filename,
                        source_page=table.page_number,
                        source_text=source_text,
                        confidence=0.96,
                    )
                )
    return facts


def _status_from_value_and_reference(
    value: str | None,
    reference: str | None,
) -> str | None:
    """Infer only an explicit numeric high/low relationship from one row."""

    if not value or not reference:
        return None
    value_match = re.search(r"-?\d+(?:\.\d+)?", value)
    if value_match is None:
        return None
    numeric = float(value_match.group())
    compact = reference.replace(" ", "")
    bounds = re.search(
        r"(?P<lower>-?\d+(?:\.\d+)?)"
        r"\s*[-~～至–—]\s*"
        r"(?P<upper>-?\d+(?:\.\d+)?)",
        compact,
    )
    if bounds:
        lower = float(bounds.group("lower"))
        upper = float(bounds.group("upper"))
        if numeric < lower:
            return "偏低"
        if numeric > upper:
            return "偏高"
        return "正常"
    threshold = re.search(r"(?P<operator><=|>=|<|>|≤|≥)(?P<limit>-?\d+(?:\.\d+)?)", compact)
    if threshold is None:
        return None
    limit = float(threshold.group("limit"))
    operator = threshold.group("operator")
    if operator in {"<", "<=", "≤"} and numeric > limit:
        return "偏高"
    if operator in {">", ">=", "≥"} and numeric < limit:
        return "偏低"
    return "正常"


def _clinical_table_roles(
    rows: list[list[str]],
) -> tuple[int | None, dict[str, int]]:
    header_index, groups = _clinical_table_role_groups(rows)
    return header_index, groups[0] if groups else {}


def _clinical_table_role_groups(
    rows: list[list[str]],
) -> tuple[int | None, list[dict[str, int]]]:
    for row_index, row in enumerate(rows[:3]):
        groups: list[dict[str, int]] = []
        roles: dict[str, int] = {}
        for column, raw in enumerate(row):
            cell = re.sub(r"\s+", "", _clean(raw) or "").casefold()
            if not cell:
                continue
            if any(token in cell for token in ("项目", "名称", "检验项", "检查项")):
                if "name" in roles and (
                    "value" in roles or "conclusion" in roles
                ):
                    groups.append(roles)
                    roles = {}
                roles["name"] = column
            elif any(
                token in cell
                for token in ("检查结论", "体检结论", "异常结果", "结论")
            ):
                roles.setdefault("conclusion", column)
            elif any(token in cell for token in ("参考范围", "参考值", "正常范围")):
                roles.setdefault("reference", column)
            elif "单位" in cell:
                roles.setdefault("unit", column)
            elif any(token in cell for token in ("状态", "提示", "异常标志", "结果标志")):
                roles.setdefault("status", column)
            elif any(token in cell for token in ("结果", "测定值", "检测值", "数值")):
                roles.setdefault("value", column)
        if ("name" in roles and "value" in roles) or "conclusion" in roles:
            groups.append(roles)
        valid = [
            group
            for group in groups
            if ("name" in group and "value" in group) or "conclusion" in group
        ]
        if valid:
            return row_index, valid
    return None, []


def _supplementary_hospital_data_from_document(
    document: ExtractedDocument,
) -> list[HospitalDataInput]:
    """Extract clinical rows from a complete supplementary document.

    The text-only helper remains intentionally conservative for OCR output.
    A locally parsed PDF additionally exposes tables, which lets us retain the
    provider's main-examination conclusions without copying the surrounding
    health-education paragraphs into patient facts.
    """

    rows = _supplementary_hospital_data_from_text(
        document.text,
        source_name=document.filename,
    )
    report_date = _labeled_value(
        document.text,
        ("体检日期", "检测日期", "报告日期"),
        r"\d{4}[-_/.\u5e74]\d{1,2}[-_/.\u6708]\d{1,2}\u65e5?",
    )
    seen = {
        (
            item.data_type,
            item.item_name,
            str(item.test_result_value or ""),
            str(item.exam_result or ""),
        )
        for item in rows
    }

    def append(item: HospitalDataInput) -> None:
        key = (
            item.data_type,
            item.item_name,
            str(item.test_result_value or ""),
            str(item.exam_result or ""),
        )
        if key not in seen:
            seen.add(key)
            rows.append(item)

    for fact in _generic_clinical_facts_from_tables(document, report_date=report_date):
        append(_clinical_fact_to_hospital_data(fact))

    # Health-assessment dimensions require a small set of core laboratory
    # results even when they are normal.  Full check-up PDFs commonly print
    # these as four consecutive lines (name, value, reference, unit), while
    # their summary pages mention only abnormalities.  Parse only the named
    # core fields needed by the existing assessment rules; do not turn every
    # table cell in the document into a clinical result.
    core_labs = (
        (
            "糖化血红蛋白",
            r"糖化血红蛋白\s*\n\s*(?:A1C\s*\n\s*)?",
            "%",
        ),
        (
            "葡萄糖(Glu)",
            r"葡萄糖\s*\(Glu\)\s*\n\s*",
            "mmol/L",
        ),
        (
            "高密度脂蛋白(HDL-c)",
            r"高密度脂蛋白\s*\n\s*\(HDL-c\)\s*\n\s*",
            "mmol/L",
        ),
        (
            "肌酐(Cr)",
            r"肌酐\s*\(Cr\)\s*\n\s*",
            "μmol/L",
        ),
        (
            "尿酸(UA)",
            r"尿酸\s*\(UA\)\s*\n\s*",
            "μmol/L",
        ),
        (
            "肾小球滤过率(GFR-Cr)",
            r"肾小球滤过率\s*\n\s*\(GFR-Cr\)\s*\n\s*",
            "mL/min",
        ),
    )
    for item_name, label_pattern, default_unit in core_labs:
        match = re.search(
            label_pattern
            + r"(?P<value>-?\d+(?:\.\d+)?)\s*\n\s*"
            + r"(?P<reference>-|[<>≤≥]?-?\d+(?:\.\d+)?"
            + r"(?:\s*[-~–—]\s*[<>≤≥]?-?\d+(?:\.\d+)?)?)"
            + r"\s*\n\s*(?P<unit>[^\n]{1,24})",
            document.text,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        unit = " ".join(match.group("unit").split())
        if len(unit) > 16 or re.search(r"[一-鿿]{4,}", unit):
            unit = default_unit
        append(
            HospitalDataInput(
                data_type="INSPECTION",
                item_name=item_name,
                test_result_value=f"{match.group('value')} {unit}".strip(),
                reference_range=match.group("reference"),
                report_time=report_date,
            )
        )

    # Preserve explicit abnormal laboratory facts, including their source
    # direction and reference range.  This also resolves contradictory prose
    # in a summary card (for example a value marked with an upward arrow but
    # described as "lower") in favour of the original result row.
    abnormal_line = re.compile(
        r"^(?P<name>[^\n:：]{1,100})[:：]\s*"
        r"(?P<value>-?\d+(?:\.\d+)?)\s*"
        r"(?P<unit>[^\s↑↓]{0,24})?\s*"
        r"(?P<direction>[↑↓])\s*"
        r"(?P<reference>[<>≤≥]?-?\d+(?:\.\d+)?"
        r"(?:\s*[-~–—]\s*[<>≤≥]?-?\d+(?:\.\d+)?)?)\s*$",
        flags=re.MULTILINE,
    )
    for match in abnormal_line.finditer(document.text):
        name = " ".join(match.group("name").split()).strip(" *")
        if not name or re.search(r"(?:增高|降低)$", name):
            continue
        unit = (match.group("unit") or "").strip()
        value = f"{match.group('value')} {unit}".strip()
        append(
            HospitalDataInput(
                data_type="INSPECTION",
                item_name=name,
                test_result_value=value,
                reference_range=" ".join(match.group("reference").split()),
                result_status_name=(
                    "偏高" if match.group("direction") == "↑" else "偏低"
                ),
                report_time=report_date,
            )
        )

    for match in re.finditer(
        r"(?m)^(?P<name>[^\n:：]{0,100}隐血[^\n:：]{0,30})[:：]\s*"
        r"(?P<value>弱阳性|阳性|阴性)(?:\s+[^\n]*)?$",
        document.text,
    ):
        value = match.group("value")
        if value == "阴性":
            continue
        append(
            HospitalDataInput(
                data_type="INSPECTION",
                item_name=" ".join(match.group("name").split()),
                test_result_value=value,
                reference_range="阴性",
                result_status_name="异常",
                report_time=report_date,
            )
        )

    for table in document.tables:
        if not table.rows:
            continue
        heading = table.rows[0]
        marker = _clean(_cell(heading, 0)) or ""
        if not re.fullmatch(r"\d+[、.]?", marker):
            continue
        title = next(
            (
                _clean(cell)
                for cell in reversed(heading[1:])
                if _clean(cell)
            ),
            None,
        )
        if not title:
            continue
        title = title.lstrip("*").strip()
        # A labelled high/low summary is redundant with the original result
        # row above and may itself contain a wording error.  Keep the measured
        # fact, not the generated summary sentence.
        if re.match(r"^.{1,80}(?:增高|降低)\s*[:：]\s*-?\d", title):
            continue
        item_name = re.split(r"[:：]", title, maxsplit=1)[0].strip()[:512]
        append(
            HospitalDataInput(
                data_type="EXAM",
                item_name=item_name,
                exam_result=title[:2000],
                report_time=report_date,
            )
        )
    return rows


def _consecutive_image_candidates(
    request: ReportRequest,
    image_texts: dict[int, tuple[DownloadedFile, str]],
) -> tuple[list[_ParsedSourceCandidate], set[int]]:
    groups = _continuous_image_groups(image_texts)
    results: list[_ParsedSourceCandidate] = []
    consumed_indexes: set[int] = set()
    for group in groups:
        combined_text = "\n".join(
            f"\n===== IMAGE PAGE {position} =====\n{text}"
            for position, (_index, _downloaded, text) in enumerate(group, start=1)
        )
        combined_content = b"\n".join(item.content for _index, item, _text in group)
        first_index = group[0][0]
        first_name = group[0][1].filename
        last_name = group[-1][1].filename
        try:
            report = parse_extracted_text(
                request, filename=f"{first_name}..{last_name}", source_type="image",
                text=combined_text, checksum=hashlib.sha256(combined_content).hexdigest(),
            )
        except (SourceParseError, ValueError):
            continue
        results.append(_ParsedSourceCandidate(report, 3, first_index - len(request.file_list)))
        consumed_indexes.update(index for index, _item, _text in group)
    return results, consumed_indexes


def _continuous_image_groups(image_texts):
    ordered = sorted(image_texts.items())
    if len(ordered) < 2:
        return []
    groups: list[list[tuple[int, DownloadedFile, str]]] = []
    current: list[tuple[int, DownloadedFile, str]] = []
    previous_index: int | None = None
    for index, (downloaded, text) in ordered:
        begins_report = _looks_like_image_report_start(text)
        if current and (
            previous_index is None or index != previous_index + 1 or begins_report
        ):
            groups.append(current)
            current = []
        current.append((index, downloaded, text))
        previous_index = index
    if current:
        groups.append(current)

    return [group for group in groups if _looks_like_continuous_image_report(group)]


def _looks_like_image_report_start(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return any(
        marker in compact
        for marker in (
            "您的肠道总评分为",
            "肠道总体评分",
            "肠道菌群检测报告",
            "肠道微生态检测报告",
        )
    )


def _looks_like_continuous_image_report(
    group: list[tuple[int, DownloadedFile, str]],
) -> bool:
    if len(group) < 2 or not _looks_like_image_report_start(group[0][2]):
        return False
    continuation_markers = (
        "评估",
        "检测结果",
        "结果提示",
        "短链脂肪酸",
        "营养物质",
        "维生素",
        "天然色素",
        "疾病风险",
        "异常菌群",
    )
    for _index, _downloaded, text in group[1:]:
        if _looks_like_image_report_start(text):
            return False
        compact = re.sub(r"\s+", "", text)
        if not any(marker in compact for marker in continuation_markers):
            return False
    return True


def _enrich_sample_from_pdf_candidates(
    parsed: ParsedMicrobiomeReport,
    parsed_sources: list[_ParsedSourceCandidate],
    metadata_sources: list[_ParsedSampleCandidate],
) -> ParsedMicrobiomeReport:
    """Fill missing sample metadata from an attached microbiome-report PDF.

    The selected microbiome facts still come exclusively from the higher-priority
    structured source.  Only sample metadata is copied, and only when the PDF
    explicitly identifies itself as a gut-microbiome test.
    """

    if all(
        _sample_value_present(getattr(parsed.sample, field))
        for field in SampleInfo.model_fields
    ):
        return parsed
    candidates: list[SampleInfo] = []
    for source in parsed_sources:
        if source.report.source_type != "pdf":
            continue
        if (
            source.report.taxa or source.report.disease_results
        ) and not _reports_are_same_snapshot(parsed, source.report):
            # A fully parsed microbiome PDF with conflicting findings is a
            # different report, not a metadata cover sheet for the selected
            # structured result.
            continue
        candidate = source.report.sample
        test_item = (candidate.test_item or "").replace(" ", "")
        if "菌群" not in test_item and "微生物" not in test_item:
            continue
        candidates.append(candidate)
    for source in metadata_sources:
        candidate = source.sample
        test_item = (candidate.test_item or "").replace(" ", "")
        if "菌群" not in test_item and "微生物" not in test_item:
            continue
        candidates.append(candidate)
    if not candidates:
        return parsed
    compatible = [
        candidate
        for candidate in candidates
        if _sample_metadata_is_compatible(parsed.sample, candidate)
    ]
    if not compatible:
        return parsed
    candidate = max(compatible, key=_sample_recency_key)
    parsed.sample = SampleInfo(
        **{
            field: (
                getattr(parsed.sample, field)
                if _sample_value_present(getattr(parsed.sample, field))
                else getattr(candidate, field)
            )
            for field in SampleInfo.model_fields
        }
    )
    return parsed


def _consolidate_same_report_sources(
    candidates: list[_ParsedSourceCandidate],
) -> list[_ParsedSourceCandidate]:
    groups: list[list[_ParsedSourceCandidate]] = []
    for candidate in sorted(candidates, key=lambda item: (item.priority, item.order)):
        group = next(
            (
                existing
                for existing in groups
                if any(
                    _reports_are_same_snapshot(candidate.report, item.report)
                    for item in existing
                )
            ),
            None,
        )
        if group is None:
            groups.append([candidate])
        else:
            group.append(candidate)

    consolidated: list[_ParsedSourceCandidate] = []
    for group in groups:
        primary = min(group, key=lambda item: (item.priority, item.order))
        merged = primary.report.model_copy(deep=True)
        for candidate in sorted(group, key=lambda item: (item.priority, item.order)):
            if candidate is primary:
                continue
            _merge_same_snapshot(merged, candidate.report)
        consolidated.append(
            _ParsedSourceCandidate(
                report=merged,
                priority=primary.priority,
                order=primary.order,
            )
        )
    return consolidated


def _reports_are_same_snapshot(
    left: ParsedMicrobiomeReport,
    right: ParsedMicrobiomeReport,
) -> bool:
    left_sample = left.sample
    right_sample = right.sample
    if (
        left_sample.sample_id
        and right_sample.sample_id
        and left_sample.sample_id != right_sample.sample_id
    ):
        return False
    for field_name in ("report_date", "sample_date"):
        left_value = _normalized_identity_value(getattr(left_sample, field_name))
        right_value = _normalized_identity_value(getattr(right_sample, field_name))
        if left_value and right_value and left_value != right_value:
            return False
    if left.source_checksum and left.source_checksum == right.source_checksum:
        return True
    for field_name in ("sample_id", "report_date", "sample_date"):
        left_value = _normalized_identity_value(getattr(left_sample, field_name))
        right_value = _normalized_identity_value(getattr(right_sample, field_name))
        if left_value and right_value and left_value == right_value:
            return True
    return _matching_fact_count(left, right) >= 2


def _matching_fact_count(
    left: ParsedMicrobiomeReport,
    right: ParsedMicrobiomeReport,
) -> int:
    left_facts = {
        f"taxon|{item.latin_name.casefold()}|{item.value or ''}|{item.source_status or ''}"
        for item in left.taxa
    } | {
        f"disease|{item.name.casefold()}|{item.source_score or ''}"
        for item in left.disease_results
    }
    right_facts = {
        f"taxon|{item.latin_name.casefold()}|{item.value or ''}|{item.source_status or ''}"
        for item in right.taxa
    } | {
        f"disease|{item.name.casefold()}|{item.source_score or ''}"
        for item in right.disease_results
    }
    return len(left_facts & right_facts)


def _merge_same_snapshot(
    primary: ParsedMicrobiomeReport,
    supplement: ParsedMicrobiomeReport,
) -> None:
    # The new evaluate-only headings may complement the exact same original
    # file or an explicitly matching sample. A shared date or similar taxon
    # values alone are insufficient to move heading ratios between sources.
    same_evaluation_source = bool(
        primary.source_checksum and primary.source_checksum == supplement.source_checksum
    ) or bool(
        primary.sample.sample_id and primary.sample.sample_id == supplement.sample.sample_id
        and _sample_metadata_is_compatible(primary.sample, supplement.sample)
    )
    if same_evaluation_source:
        _append_unique(
            primary.evaluation_panel_summaries,
            supplement.evaluation_panel_summaries,
            key=lambda item: (item.code, item.name, item.value, item.unit, item.reference_range, item.status),
        )
        # Keep original file provenance and all conflicting class candidates.
        # Only evaluate consumes this array, after exact scalar/source matching.
        for row in supplement.evaluation_amr_classes:
            if row not in primary.evaluation_amr_classes:
                primary.evaluation_amr_classes.append(dict(row))
    primary.sample = SampleInfo(
        **{
            field: (
                getattr(primary.sample, field)
                if _sample_value_present(getattr(primary.sample, field))
                else getattr(supplement.sample, field)
            )
            for field in SampleInfo.model_fields
        }
    )
    _append_unique(
        primary.metrics, supplement.metrics, key=lambda item: item.code.casefold()
    )
    _append_unique(
        primary.taxa,
        supplement.taxa,
        key=lambda item: item.latin_name.casefold(),
    )
    diseases_by_name = {item.name.casefold(): item for item in primary.disease_results}
    for item in supplement.disease_results:
        existing = diseases_by_name.get(item.name.casefold())
        if existing is None:
            primary.disease_results.append(item)
            diseases_by_name[item.name.casefold()] = item
            continue
        if existing.source_score is None:
            existing.source_score = item.source_score
        if existing.source_level is None:
            existing.source_level = item.source_level
        if existing.source_description is None:
            existing.source_description = item.source_description
        if not existing.related_taxa and item.related_taxa:
            existing.related_taxa = item.related_taxa
    _append_unique(
        primary.panel_counts,
        supplement.panel_counts,
        key=lambda item: item.category,
    )
    primary.enterotype = primary.enterotype or supplement.enterotype
    primary.source_summary = primary.source_summary or supplement.source_summary


def _append_unique(target: list[Any], source: list[Any], *, key) -> None:
    existing = {key(item) for item in target}
    for item in source:
        marker = key(item)
        if marker in existing:
            continue
        target.append(item.model_copy(deep=True))
        existing.add(marker)


def _candidate_recency_key(
    candidate: _ParsedSourceCandidate,
) -> tuple[int, int, int, int]:
    # ``microbiomeData`` and ``testData`` are the request's dedicated current
    # detection-data channels.  Uploaded PDFs/images may contain older reports
    # and must not displace an undated structured result merely because the
    # attachment happens to expose a date.  Within the same channel class we
    # still select the latest dated snapshot, then apply the documented source
    # priority and caller order as tie-breakers.
    dedicated_current_source = int(candidate.priority <= 1)
    return (
        dedicated_current_source,
        _report_date_value(candidate.report),
        -candidate.priority,
        -candidate.order,
    )


def _evaluation_candidate_recency_key(candidate: _ParsedSourceCandidate) -> tuple[int, int, int]:
    """Evaluate uses the latest published report across accepted channels."""
    return _evaluation_sample_recency_key(candidate.report.sample), -candidate.priority, -candidate.order


def _evaluation_sample_recency_key(sample: SampleInfo) -> int:
    from modules.report.evaluation_sources import _time

    when = _time(sample.report_date) or _time(sample.sample_date)
    stamp = 0 if when is None else (
        ((when.toordinal() * 24 + when.hour) * 60 + when.minute) * 60 + when.second
    ) * 1_000_000 + when.microsecond
    return stamp


def _report_date_value(report: ParsedMicrobiomeReport) -> int:
    return _date_value(report.sample.report_date) or _date_value(
        report.sample.sample_date
    )


def _sample_recency_key(sample: SampleInfo) -> int:
    return _date_value(sample.report_date) or _date_value(sample.sample_date)


def _date_value(value: str | None) -> int:
    if not value:
        return 0
    numbers = [int(item) for item in re.findall(r"\d+", value)]
    if len(numbers) >= 3:
        year, month, day = numbers[:3]
    elif len(numbers) == 1 and len(str(numbers[0])) == 8:
        raw = str(numbers[0])
        year, month, day = int(raw[:4]), int(raw[4:6]), int(raw[6:])
    else:
        return 0
    try:
        return date(year, month, day).toordinal()
    except ValueError:
        return 0


def _normalized_identity_value(value: str | None) -> str:
    return re.sub(r"\W+", "", (value or "").casefold())


def _sample_metadata_is_compatible(current: SampleInfo, candidate: SampleInfo) -> bool:
    for field_name in ("sample_id", "report_date", "sample_date"):
        current_value = _normalized_identity_value(getattr(current, field_name))
        candidate_value = _normalized_identity_value(getattr(candidate, field_name))
        if current_value and candidate_value and current_value != candidate_value:
            return False
    return True


def parse_test_data(
    request: ReportRequest,
    downloaded: DownloadedTestData,
    *, evaluation_mode: bool = False,
) -> ParsedMicrobiomeReport:
    if isinstance(downloaded.detail.get("microbiomeData"), dict):
        report = parse_canonical_data(request, downloaded.detail["microbiomeData"],
            evaluation_mode=evaluation_mode)
        original_identity = (report.source_name, report.source_checksum)
        report.source_type = "test_data"
        report.source_name = "testData-detail.json"
        report.source_checksum = downloaded.detail_checksum
        if evaluation_mode:
            from .document_source import EvaluationDiseaseEvidenceMetric
            for card in report.evaluation_panel_summaries:
                if (isinstance(card, EvaluationDiseaseEvidenceMetric) and card.source_page == 0
                        and (card.source_file, card.source_checksum) == original_identity):
                    card.source_file = report.source_name
                    card.source_checksum = report.source_checksum
        if downloaded.summary:
            report.source_summary = downloaded.summary
        return report
    return parse_provider_data(request, downloaded)


def parse_provider_data(
    request: ReportRequest,
    downloaded: DownloadedTestData,
) -> ParsedMicrobiomeReport:
    detail = downloaded.detail
    microbe_data = detail.get("microbeData") or {}
    disease_data = detail.get("diseaseRisk") or {}
    if not isinstance(microbe_data, dict) or not isinstance(disease_data, dict):
        raise SourceParseError("provider testData has an invalid shape")

    metrics: list[SourceMetric] = []
    for row in _rows(microbe_data.get("comprehensive")):
        code = _clean(row.get("latinName"))
        if not code:
            continue
        metric_code = _PROVIDER_METRIC_CODES.get(code, code)
        metric_value = _clean(row.get("value"))
        if metric_code == "enterotype" and metric_value:
            metric_value = _normalize_enterotype(metric_value)
        metrics.append(
            SourceMetric(
                code=metric_code,
                name=_PROVIDER_METRIC_NAMES.get(code, code),
                value=metric_value,
            )
        )
    metrics.extend(_provider_function_metrics(microbe_data, disease_data))

    taxa: list[ParsedTaxon] = []
    panel_counts: list[PanelCountAudit] = []
    source_summary_counts = _provider_summary_counts(microbe_data)
    for source_key, category in _PROVIDER_PANELS.items():
        aggregate_status = _provider_aggregate_status(disease_data, source_key)
        panel_rows = _rows(microbe_data.get(source_key))
        detected_count = 0
        trace_count = 0
        for row in panel_rows:
            latin_name = _clean(row.get("latinName"))
            value = _clean(row.get("value"))
            if not latin_name:
                continue
            detection = detection_status(value, explicit_detection=_explicit_detection_flag(row))
            detected_count += detection == "detected"
            trace_count += detection == "trace"
            # Provider detail does not grade individual species.  Preserve its
            # aggregate status for context but never project it onto each taxon.
            taxa.append(
                ParsedTaxon(
                    taxon_id=stable_id("taxon", latin_name),
                    category=category,
                    latin_name=latin_name,
                    value=value,
                    source_status=None,
                    explicitly_abnormal=False,
                    detection_status=detection,
                )
            )
        source_summary_count = source_summary_counts.get(category)
        panel_counts.append(
            PanelCountAudit(
                category=category,
                panel_name=_PROVIDER_PANEL_NAMES[source_key],
                row_count=len(panel_rows),
                detected_count=detected_count,
                trace_count=trace_count,
                source_summary_count=source_summary_count,
                matches_source_summary=(
                    detected_count + trace_count == source_summary_count
                    if source_summary_count is not None
                    else None
                ),
            )
        )
        if aggregate_status:
            metrics.append(
                SourceMetric(
                    code=f"panel_{category}",
                    name=_PROVIDER_PANEL_NAMES[source_key],
                    value=_provider_panel_total(
                        disease_data.get(_PROVIDER_PANEL_NAMES[source_key])
                    ),
                    unit="%",
                    status=aggregate_status,
                )
            )

    diseases: list[ParsedDisease] = []
    for name, raw_score in disease_data.items():
        if name in _PROVIDER_NON_DISEASE_KEYS:
            continue
        score = _numeric_text(raw_score)
        if score is None or float(score) < 0:
            continue
        related = [
            _clean(row.get("latinName"))
            for row in _rows(microbe_data.get(name))
            if _clean(row.get("latinName"))
        ]
        diseases.append(
            ParsedDisease(
                disease_id=stable_id("disease", name),
                name=name,
                source_score=score,
                related_taxa=list(dict.fromkeys(related)),
            )
        )

    enterotype_metric = next(
        (item for item in metrics if item.code == "enterotype" and item.value), None
    )
    return ParsedMicrobiomeReport(
        source_type="test_data",
        source_name="testData-detail.json",
        source_checksum=downloaded.detail_checksum,
        subject=_subject(request),
        metrics=metrics,
        taxa=taxa,
        disease_results=diseases,
        panel_counts=panel_counts,
        enterotype=(
            _normalize_enterotype(enterotype_metric.value)
            if enterotype_metric and enterotype_metric.value
            else None
        ),
        source_summary=downloaded.summary,
    )


def parse_canonical_data(
    request: ReportRequest,
    payload: dict[str, Any],
    *, evaluation_mode: bool = False,
) -> ParsedMicrobiomeReport:
    sample_data = (
        payload.get("sample") if isinstance(payload.get("sample"), dict) else {}
    )
    metrics = [
        _canonical_metric(item) for item in _dict_rows(payload.get("overallMetrics"))
    ]
    metrics.extend(
        _canonical_metric(item)
        for item in _dict_rows(payload.get("shortChainFattyAcids"))
    )

    taxa: list[ParsedTaxon] = []
    for row in _dict_rows(payload.get("taxa")):
        latin_name = _clean(row.get("latinName") or row.get("name"))
        if not latin_name:
            continue
        category = str(row.get("category") or "other")
        if category not in _CANONICAL_CATEGORIES:
            category = "other"
        status = _clean(row.get("status"))
        explicit = _is_explicit_abnormal(status, row.get("isAbnormal"))
        taxa.append(
            ParsedTaxon(
                taxon_id=stable_id("taxon", latin_name),
                category=category,
                latin_name=latin_name,
                chinese_name=_clean(row.get("chineseName")),
                value=_clean(row.get("value")),
                unit=_clean(row.get("unit")) or "%",
                reference_range=_clean(row.get("referenceRange")),
                source_status=status,
                explicitly_abnormal=explicit,
                detection_status=detection_status(row.get("value"), status=status,
                    explicit_detection=_explicit_detection_flag(row)),
            )
        )

    diseases: list[ParsedDisease] = []
    for row in _dict_rows(payload.get("diseaseRisks")):
        name = _clean(row.get("disease") or row.get("name"))
        if not name:
            continue
        score, level = _canonical_risk_fields(row, aliases=evaluation_mode)
        diseases.append(
            ParsedDisease(
                disease_id=stable_id("disease", name),
                name=name,
                source_score=score,
                source_level=level,
                related_taxa=[
                    text
                    for item in row.get("relatedMicrobes") or []
                    if (text := _clean(item))
                ],
            )
        )

    enterotype = _clean(payload.get("enterotype"))
    if not enterotype:
        enterotype_metric = next(
            (item for item in metrics if item.code.casefold() == "enterotype"), None
        )
        enterotype = enterotype_metric.value if enterotype_metric else None
    if enterotype:
        enterotype = _normalize_enterotype(enterotype)
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
        if evaluation_mode else repr(sorted(payload.keys())).encode("utf-8"))
    checksum = hashlib.sha256(raw).hexdigest()
    evidence = _canonical_disease_evidence(payload, checksum=checksum,
        report_date=_clean(sample_data.get("reportDate"))) if evaluation_mode else []
    return ParsedMicrobiomeReport(
        source_type="microbiome_data",
        source_name="microbiomeData.json",
        source_checksum=checksum,
        subject=_subject(request),
        sample=SampleInfo(
            sample_id=_clean(sample_data.get("sampleId")),
            sample_type=_clean(sample_data.get("sampleType")),
            test_item=_clean(sample_data.get("testItem")),
            sample_date=_clean(sample_data.get("sampleDate")),
            report_date=_clean(sample_data.get("reportDate")),
            sequencing_platform=_clean(sample_data.get("sequencingPlatform")),
        ),
        metrics=metrics,
        taxa=taxa,
        disease_results=diseases,
        evaluation_panel_summaries=evidence,
        enterotype=enterotype,
        source_summary=_clean(payload.get("summary")),
    )


def _canonical_risk_fields(row, *, aliases=True):
    """Established names win; explicit aliases fill only missing results."""
    score = next((row[key] for key in (("riskScore", "score", "riskIndex") if aliases else ("riskScore", "score"))
        if row.get(key) is not None), None)
    return _clean(score), _clean(row.get("riskLevel") or row.get("level") or (row.get("riskGrade") if aliases else None))


def _canonical_disease_evidence(payload, *, checksum, report_date):
    """Retain a canonical disease's own measured rows in the internal lane.

    Association signs and abundance flags remain separate source facts. No
    organism is borrowed from a global panel and no risk direction is inferred.
    """
    from .document_source import EvaluationDiseaseEvidenceMetric

    result = []
    for disease_index, row in enumerate(payload.get("diseaseRisks") or []):
        if not isinstance(row, dict):
            continue
        name = _clean(row.get("disease") or row.get("name"))
        if not name or not isinstance(row.get("relatedMicrobeDetails"), list):
            continue
        score, level = _canonical_risk_fields(row)
        # The model value and grade are required for exact downstream binding;
        # an absent model must not borrow another disease's organisms.
        if score is None or level is None:
            continue
        taxa = []
        for index, actual in enumerate(row["relatedMicrobeDetails"]):
            if not isinstance(actual, dict):
                continue
            scientific = _clean(actual.get("taxonomy") or actual.get("scientificName") or actual.get("latinName"))
            label = _clean(actual.get("name")) or scientific
            if not label:
                continue
            taxa.append(dict(name=label, scientificName=scientific,
                value=_clean(actual.get("value")), rawValue=actual.get("rawValue"),
                unit=_clean(actual.get("unit")), referenceRange=_clean(actual.get("referenceRange")),
                sourceStatus=_clean(actual.get("status") if actual.get("status") is not None
                    else actual.get("sourceStatus")), correlation=_clean(actual.get("correlation")),
                sourceChineseName=actual.get("sourceChineseName"),
                sourcePath=f"diseaseRisks[{disease_index}].relatedMicrobeDetails[{index}]"))
        result.append(EvaluationDiseaseEvidenceMetric(
            code="disease_source_evidence:" + stable_id("card", f"{checksum}:{disease_index}"),
            name=name, disease_name=name, disease_score=score, disease_level=level,
            report_date=report_date, source_page=0, source_file="microbiomeData.json",
            source_checksum=checksum, taxa=taxa))
    return result


def parse_downloaded_document(
    request: ReportRequest,
    downloaded: DownloadedFile,
) -> ParsedMicrobiomeReport:
    extracted = extract_document(downloaded)
    metrics = _document_metrics(extracted)
    taxa = _document_taxa(extracted)
    diseases = _document_diseases(extracted)
    checksum = hashlib.sha256(downloaded.content).hexdigest()
    sample = _document_sample(extracted)
    return ParsedMicrobiomeReport(
        source_type="pdf" if downloaded.declared_type == "pdf" else "image",
        source_name=downloaded.filename,
        source_checksum=checksum,
        subject=_merge_subjects(_subject(request), _document_subject(extracted)),
        sample=sample,
        metrics=metrics,
        taxa=taxa,
        disease_results=diseases,
        enterotype=extracted.metadata.get("enterotype"),
    )


def parse_extracted_text(
    request: ReportRequest,
    *,
    filename: str,
    source_type: str,
    text: str,
    checksum: str | None = None,
) -> ParsedMicrobiomeReport:
    metadata: dict[str, str] = {}
    if match := _ENTEROTYPE_TEXT.search(text):
        metadata["enterotype"] = _normalize_enterotype(match.group(1))
    # Layout services commonly return pipe-delimited Markdown even when the
    # source is a scanned PDF. Normalize those tables into the same internal
    # representation used by native PDF table extraction so downstream
    # parsing is driven by column meaning rather than one provider's visual
    # layout or OCR reading order.
    document = ExtractedDocument(
        filename=filename,
        source_type=source_type,
        text=text,
        tables=_markdown_tables(text, page_number=None),
        metadata=metadata,
    )
    table_taxa = _document_taxa(document)
    linear_taxa = _document_taxa_from_linear_text(text)
    return ParsedMicrobiomeReport(
        source_type="pdf" if source_type == "pdf" else "image",
        source_name=filename,
        source_checksum=checksum,
        subject=_merge_subjects(_subject(request), _document_subject(document)),
        sample=_document_sample(document),
        metrics=_document_metrics(document),
        taxa=_merge_document_taxa([*table_taxa, *linear_taxa]),
        disease_results=_document_diseases(document),
        enterotype=metadata.get("enterotype"),
    )


def extract_document(downloaded: DownloadedFile) -> ExtractedDocument:
    if downloaded.declared_type == "pdf":
        return _extract_pdf(downloaded.filename, downloaded.content)
    return _extract_image(downloaded.filename, downloaded.content)


def _native_clinical_tables(
    pages: list[tuple[int, dict]], *, proof_pages: dict[int, _NativeProofPage] | None = None
) -> list[ExtractedTable]:
    """Read positioned cells under explicit clinical headers, with section dates.

    A footer date belongs to the active examination, including its continuation
    pages. A new numbered examination closes that scope; its absent date never
    inherits an earlier examination's footer or the document's general date.
    """
    tables: list[ExtractedTable] = []
    section_tables: list[ExtractedTable] = []
    columns: list[tuple[float, str]] = []
    section_title: str | None = None
    section_date: str | None = None
    active: ExtractedTable | None = None
    date_pattern = re.compile(r"检查时间\s*[:：]?\s*(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?)")
    identity = re.compile(r"^(?:姓名|性别|年龄|编号|电话|体检号|身份证|病历|受检者|检查者|报告人|出生日期)")
    for page_number, native in pages:
        proof = proof_pages.get(page_number) if proof_pages is not None else None
        page_size = proof.size if proof is not None else None
        spans = [span for block in native.get('blocks', []) for line in block.get('lines', [])
                 for span in line.get('spans', []) if str(span.get('text', '')).strip() and len(span.get('bbox', ())) == 4]
        lines: list[list[dict]] = []
        # Font ascenders differ between Chinese labels and Latin values.
        # Their PDF baselines, rather than glyph-box tops, identify a row.
        def baseline(span):
            return span.get('origin', (0, span['bbox'][1]))[1]
        for span in sorted(spans, key=lambda s: (round(baseline(s), 1), s['bbox'][0])):
            if not lines or abs(baseline(span) - baseline(lines[-1][0])) > 3:
                lines.append([span])
            else:
                lines[-1].append(span)
        active = None
        for line in lines:
            y0, y1 = line[0]['bbox'][1], line[0]['bbox'][3]
            if proof is not None and any(start <= (y0 + y1) / 2 < end for start, end in proof.nonpatient_ranges):
                section_title, section_date, active = None, None, None
                section_tables, columns = [], []
                continue
            line.sort(key=lambda s: s['bbox'][0])
            texts = [_clean_document_line(s['text']) for s in line]
            text = ' '.join(texts)
            if re.match(r'^\d{1,3}[.、．]\s*[^\d\s]', text):
                section_title = text
                section_date = None
                section_tables = []
                columns = []
                active = None
                continue
            date_match = date_pattern.search(text)
            if date_match:
                if section_tables:
                    for table in section_tables:
                        table.observed_at = date_match.group(1)
                    section_tables = []
                    section_date = None
                    section_title = None
                    columns = []
                    active = None
                elif section_title:
                    # A leading date is usable only inside an explicit new
                    # examination, never as a previous section's loose footer.
                    section_date = date_match.group(1)
                continue
            _, role_groups = _clinical_table_role_groups([texts])
            clinical_header = len(role_groups) == 1 and {'name', 'value', 'reference'} <= role_groups[0].keys()
            if clinical_header and any(re.fullmatch(r'(?:检验|检查)?项目(?:名称)?|项目名称', cell) for cell in texts):
                columns = [(span['bbox'][0], cell) for span, cell in zip(line, texts)]
                active = ExtractedTable(page_number, [texts], section_title or '', section_date, section_title, True)
                tables.append(active)
                section_tables.append(active)
                continue
            if not columns:
                continue
            edges = [(columns[i][0] + columns[i + 1][0]) / 2 for i in range(len(columns) - 1)]
            cells = [''] * len(columns)
            cell_boxes = [[] for _ in columns] if page_size is not None else None
            for span in line:
                column = sum(span['bbox'][0] >= edge for edge in edges)
                cells[column] += span['text']
                if cell_boxes is not None:
                    cell_boxes[column].append(span['bbox'])
            cells = [_clean_document_line(cell) for cell in cells]
            _, groups = _clinical_table_role_groups([[label for _, label in columns]])
            roles = groups[0]
            name, value = _cell(cells, roles['name']), _cell(cells, roles['value'])
            named_cell = _looks_like_item_name(name) or bool(re.fullmatch(r'[\u3400-\u9fff]', name))
            if not named_cell or not value or identity.search(name):
                continue
            if active is None:
                active = ExtractedTable(page_number, [[label for _, label in columns]], section_title or '', section_date, section_title, True)
                tables.append(active)
                section_tables.append(active)
            if cell_boxes is not None:
                active.native_cell_bounds[len(active.rows)] = tuple(
                    _native_normalized_bounds(boxes, page_size) for boxes in cell_boxes
                )
            active.rows.append(cells)
    return [table for table in tables if len(table.rows) > 1]


def original_clinical_table_rows(document: ExtractedDocument) -> list[HospitalDataInput]:
    """Source cells only: no inferred normal flag, report date or patient prose."""
    rows = []
    for table in document.tables:
        if not table.native_clinical_columns:
            continue
        header, groups = _clinical_table_role_groups(table.rows)
        if header is None or len(groups) != 1:
            continue
        roles = groups[0]
        for row_index, cells in enumerate(table.rows[header + 1 :], start=header + 1):
            name = _clean(_cell(cells, roles['name']))
            value = _clean(_cell(cells, roles['value']))
            if not name or not value:
                continue
            unit = _clean(_cell(cells, roles['unit'])) if 'unit' in roles else None
            status_cell = _clean(_cell(cells, roles['status'])) if 'status' in roles else None
            status = _status_from_source(status_cell) if status_cell else None
            bounds = table.native_cell_bounds.get(row_index)
            rows.append(
                HospitalDataInput.model_validate(
                    {
                        'dataType': 'INSPECTION',
                        'itemName': name,
                        'testResultValue': value,
                        'unit': unit,
                        'referenceRange': _clean(_cell(cells, roles['reference'])),
                        'resultStatusName': status,
                        'reportTime': table.observed_at,
                        'sourceFile': document.filename,
                        'sourcePage': table.page_number,
                        'sourceSection': table.section_title,
                        'sourceText': ' | '.join(cells),
                        'sourceReportType': 'checkup',
                        'nativeRow': list(cells),
                        'nativeStatusCell': status_cell,
                        **(
                            {'sourceResultBounds': bounds[roles['value']], 'sourceNameBounds': bounds[roles['name']]}
                            if bounds is not None
                            else {}
                        ),
                    }
                )
            )
    return rows


def original_amr_table_rows(document: ExtractedDocument) -> list[dict]:
    """Read resistance metadata only under an explicit native AMR table header.

    Merged class cells establish the meaning of short labels such as A/B;
    isolated letters or enzyme names elsewhere do not establish resistance.
    Flags remain the literal printed labels, without clinical interpretation.
    """
    rows = []
    header_roles = {
        'class': {'抗生素耐药风险评估', '抗生素耐药性评估', '抗菌药物耐药风险评估',
                  'antibioticresistanceriskassessment', 'antimicrobialresistanceriskassessment'},
        'name': {'检测项目', '项目名称', '指标名称', 'item', 'testitem'},
        'value': {'评估值', '检测结果', '数值', 'value', 'result'},
        'status': {'结果评价', '结果评估', '评价结果', 'status', 'assessment'},
        'unit': {'单位', 'unit'},
    }
    for table in document.tables:
        # A compact antibiotic-class panel has explicit bilingual class cells;
        # its neighbouring "abnormal items: not detected" is not a zero result.
        header = [_compact_metric_label(c) for c in table.rows[0]] if table.rows else []
        if (not table.native_clinical_columns and re.search(r'抗生素耐药|抗菌药物耐药|antibiotic.*resistance', table.context or '', re.I)
                and {'拉丁学名','中文名','样本含量','参考范围','结果'} <= set(header)):
            indexes = {name: header.index(name) for name in ('中文名','样本含量','参考范围','结果')}
            for cells in table.rows[1:]:
                name, value = (_cell(cells, indexes[k]) for k in ('中文名','样本含量'))
                if name and re.fullmatch(r'[<>≤≥]?\s*\d+(?:\.\d+)?', value):
                    rows.append({'itemName':name,'value':value,'unit':None,
                        'referenceRange':_cell(cells,indexes['参考范围']) or None,
                        'status':_cell(cells,indexes['结果']) or None,'antibioticClass':name,
                        'sourceCategory':'antimicrobialResistance'})
            continue
        category = None
        roles = {}
        for cells in table.rows:
            detected = {}
            for index, cell in enumerate(cells):
                label = re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(cell or ''))).casefold()
                for role, labels in header_roles.items():
                    if label in labels:
                        detected[role] = index
            if {'class', 'name', 'value', 'status'} <= detected.keys():
                roles, category = detected, None
                continue
            if not roles:
                continue
            category = _clean(_cell(cells, roles['class'])) or category
            name, value = _clean(_cell(cells, roles['name'])), _clean(_cell(cells, roles['value']))
            if not category or not name or not value or not _RESULT_VALUE_RE.fullmatch(value):
                continue
            rows.append({'itemName': name, 'value': value,
                'unit': _clean(_cell(cells, roles['unit'])) if 'unit' in roles else None,
                'status': _clean(_cell(cells, roles['status'])), 'antibioticClass': category,
                'sourceCategory': 'antimicrobialResistance'})
    return rows


def original_nutrient_synthesis_rows(document: ExtractedDocument) -> list[dict]:
    """Literal synthesis/metabolism scalars under their complete native header."""
    rows = []
    metabolism_headers = {'营养物质代谢', 'nutrientmetabolism'}
    header_roles = {
        'category': {'营养素合成能力评估', '营养素合成能力评价',
            'nutrientsynthesiscapacityassessment'} | metabolism_headers,
        'name': {'检测项目', '项目名称', '指标名称', 'item', 'testitem'},
        'value': {'评估值', '检测结果', '数值', 'value', 'result'},
        'status': {'结果评价', '结果评估', '评价结果', 'status', 'assessment'},
        'unit': {'单位', 'unit'},
    }
    scalar = re.compile(r'[<>≤≥]?\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?')
    for table in document.tables:
        category, roles, source_category = None, {}, None
        for cells in table.rows:
            detected = {}
            for index, cell in enumerate(cells):
                label = re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(cell or ''))).casefold()
                for role, labels in header_roles.items():
                    if label in labels:
                        detected[role] = index
            if {'category', 'name', 'value', 'status'} <= detected.keys():
                category, roles = None, detected
                category_header = re.sub(r'\s+', '', unicodedata.normalize(
                    'NFKC', str(_cell(cells, roles['category']) or ''))).casefold()
                source_category = 'nutrientMetabolism' if category_header in metabolism_headers else 'nutrientSynthesis'
                continue
            if not roles:
                continue
            category = _clean(_cell(cells, roles['category'])) or category
            name, value = _clean(_cell(cells, roles['name'])), _clean(_cell(cells, roles['value']))
            if not category or not name or value is None or not scalar.fullmatch(value):
                continue
            rows.append({'itemName': name, 'value': value,
                'unit': _clean(_cell(cells, roles['unit'])) if 'unit' in roles else None,
                'status': _clean(_cell(cells, roles['status'])),
                'sourceCategory': source_category, 'sourceSubcategory': category,
                'sourcePage': table.page_number})
    return rows


def original_sample_metadata(document: ExtractedDocument) -> dict:
    """Explicit sample/QC labels on one source page, including inline cells.

    A page containing only a patient/report header is not a sample metadata
    block. Collection must be labelled as collection, never receipt/submission
    or a filename date. Conflicting source labels remain unresolved.
    """
    labels = {
        '检测编号': 'sampleId', '样本编号': 'sampleId', '样本ID': 'sampleId', '样本ID号': 'sampleId',
        '样本类型': 'sampleType', '标本类型': 'sampleType', '送检项目': 'testItem', '检测项目': 'testItem',
        '采样日期': 'sampleDate', '采样时间': 'sampleDate', '采集日期': 'sampleDate', '采集时间': 'sampleDate',
        '报告日期': 'reportDate', '报告时间': 'reportDate', '检测平台': 'sequencingPlatform', '测序平台': 'sequencingPlatform',
        '检测方法': 'method', '测序方法': 'method', '检测技术': 'method', '有效序列数': 'effectiveSequences',
        '质控结果': 'qualityControl', '样本制备质控': 'preparationQc', '检测数据质控': 'dataQc',
    }
    collected = {}
    scoped = False
    table_scoped = False
    missing_values = {'未知', '未提供', '未填写', '-', '--', 'n/a', 'na', 'unknown'}

    def append(key, value):
        field = labels.get(key)
        value = _clean(value)
        if not field or not value or value.casefold() in missing_values:
            return
        if field in {'sampleDate', 'reportDate'} and not _date_value(value):
            return
        if field == 'sampleId' and not re.fullmatch(r'[A-Za-z0-9._-]{3,64}', value):
            return
        if field == 'effectiveSequences':
            if not re.fullmatch(r'\d+|\d{1,3}(?:,\d{3})+', value):
                return
            value = int(value.replace(',', ''))
        normalized = (str(_date_value(value)) if field in {'sampleDate', 'reportDate'} else
            re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value))))
        collected.setdefault(field, {}).setdefault(normalized, value)

    label_pattern = '|'.join(re.escape(key) for key in sorted(labels, key=len, reverse=True))
    inline = re.compile(rf'(?<![A-Za-z0-9\u3400-\u9fff])(?P<label>{label_pattern})\s*[:：]\s*'
        rf'(?P<value>.*?)(?=(?:{label_pattern})\s*[:：]|$)')

    def labelled_pairs(lines):
        pairs = []
        lines = [_clean_document_line(unicodedata.normalize('NFKC', line)) for line in lines]
        lines = [line for line in lines if line]
        for index, line in enumerate(lines):
            matches = list(inline.finditer(line))
            for match in matches:
                if match['value'].strip():
                    pairs.append((match['label'], match['value'].strip()))
            key = re.sub(r'\s+', '', line).strip('：:')
            if key in labels and index + 1 < len(lines):
                value = lines[index + 1]
                next_key = re.sub(r'\s+', '', value).strip('：:')
                if next_key not in labels and not inline.search(value) and next_key not in {
                        '检测结果', '评估值', '参考范围', '正常范围', '结果评价', '单位'}:
                    pairs.append((key, value))
        return pairs

    sample_anchors = {'检测编号', '样本编号', '样本ID', '样本ID号', '样本类型', '标本类型'}
    sample_scope_labels = sample_anchors | {
        key for key, field in labels.items() if field in {
            'sampleDate', 'sequencingPlatform', 'method', 'effectiveSequences',
            'qualityControl', 'preparationQc', 'dataQc'}}
    for table in document.tables:
        keys = [re.sub(r'\s+', '', _cell(row, 0)).strip('：:') for row in table.rows]
        pairs = labelled_pairs(cell for row in table.rows for cell in row)
        inline_keys = {key for key, _ in pairs}
        if not (set(keys) & {'样本信息', '质控信息', '受检者信息', '患者信息'} or
                len(set(keys) & labels.keys()) >= 2 and set(keys) & sample_anchors or
                len(inline_keys) >= 2 and inline_keys & sample_anchors):
            continue
        scoped = True
        table_scoped = True
        for row, key in zip(table.rows, keys):
            append(key, _cell(row, 1))
        for key, value in pairs:
            append(key, value)
    source_pages = _document_pages_with_numbers(document.text or '')
    if len(source_pages) == 1 and source_pages[0][0] is None:
        parts = re.split(r'(?im)^[ \t]*(?:(?:#{1,6}[ \t]*)?(?:PAGE[ \t]*\d+|第[ \t]*\d+[ \t]*页)'
            r'|={3,}[ \t]*(?:IMAGE[ \t]+)?PAGE[ \t]*\d+[ \t]*={3,})[ \t]*$', document.text or '')
        source_pages = [(None, part) for part in parts if part.strip()]
    for _page, page_text in source_pages:
        pairs = labelled_pairs(page_text.splitlines())
        keys = {key for key, _ in pairs}
        if not (re.search(r'(?m)^\s*(?:样本信息|质控信息)\s*$', page_text)
                or len(keys & sample_scope_labels) >= 2 and keys & sample_anchors):
            continue
        scoped = True
        for key, value in pairs:
            append(key, value)
    if not scoped:
        return {}
    result = {name: next(iter(values.values())) for name, values in collected.items() if len(values) == 1}
    # This submitted test literally names the sequencing technique. No
    # sample type, sequencing platform or read count is inferred from it.
    if not result.get('method') and result.get('testItem') == '宏基因组测序':
        result['method'] = result['testItem']
    quality = [label + '：' + str(result[key]) for label, key in
        [('样本制备质控', 'preparationQc'), ('检测数据质控', 'dataQc')] if key in result]
    if quality:
        if result.get('qualityControl'):
            quality.insert(0, str(result['qualityControl']))
        result['qualityControl'] = '；'.join(quality)
    result.pop('preparationQc', None)
    result.pop('dataQc', None)
    # Keep the existing text-only legacy reader's fallback contract; this
    # strict helper itself never labels a detection/submission date as sample
    # collection. Native metadata tables explicitly withhold missing dates.
    if table_scoped or 'sampleDate' in collected:
        result.setdefault('sampleDate', None)
    return result


def _native_source_metrics(pages: list[tuple[int, dict]]) -> list[SourceMetric]:
    """Read explicit source indices and score labels without using chart shapes.

    Ratio tables and labels ending in a printed score unit are separate source
    observations. No range, status or independent medical/nutrition score is
    calculated here. Conflicting native values remain unresolved.
    """
    candidates: list[SourceMetric] = []
    score_labels = {
        'microbiome_health_score': ('肠道菌群健康度评分', '肠道菌群健康评分'),
        'be_score': ('肠道菌群平衡状态评估', 'B/E评分', 'B/E比值评分'),
        'fb_score': ('肠道菌群热量代谢评估', 'F/B评分', 'F/B比值评分'),
        'anti_inflammatory_capacity': ('菌群抗炎能力评估', '肠道抗炎能力评估', '肠道菌群抗炎能力评估'),
        'gut_immunity': ('肠道免疫力评估',),
    }
    header_roles = {
        'name': {'检测项目', '检验项目', '项目名称', '指标名称', '短链脂肪酸'},
        'value': {'评估值', '检测值', '检测结果', '结果', '数值'},
        'status': {'结果评估', '结果评价', '肠道健康状态', '合成能力评估'},
        'reference': {'参考范围', '参考值', '阈值'},
        'unit': {'单位'},
    }
    def baseline(span):
        return span.get('origin', (0, span['bbox'][1]))[1]
    for page_number, native in pages:
        spans = [s for b in native.get('blocks', []) for line in b.get('lines', [])
            for s in line.get('spans', []) if str(s.get('text', '')).strip() and len(s.get('bbox', ())) == 4]
        lines: list[list[dict]] = []
        for span in sorted(spans, key=lambda s: (baseline(s), s['bbox'][0])):
            if not lines or abs(baseline(span) - baseline(lines[-1][0])) > 3:
                lines.append([span])
            else:
                lines[-1].append(span)
        for line in lines:
            line.sort(key=lambda s: s['bbox'][0])
        for label in spans:
            raw_label = label['text'].strip()
            name = re.sub(r'\s+分\s*$', '', raw_label).strip()
            code = next((code for code, aliases in score_labels.items()
                if _compact_metric_label(name) in {_compact_metric_label(alias) for alias in aliases}), None)
            if code is None:
                continue
            numbers = [s for s in spans if _RESULT_VALUE_RE.fullmatch(s['text'].strip())
                and abs(baseline(s) - baseline(label)) <= 4
                and label['bbox'][0] <= s['bbox'][0] <= label['bbox'][2] + 60]
            if len(numbers) != 1:
                continue
            number = numbers[0]
            printed_unit = name != raw_label or any(s['text'].strip() == '分'
                and abs(baseline(s) - baseline(number)) <= 4
                and s['bbox'][0] <= number['bbox'][2] + 35
                and number['bbox'][2] <= s['bbox'][2] <= number['bbox'][2] + 60 for s in spans)
            if printed_unit:
                candidates.append(_NativeSourceMetric(code=code, name=name,
                    value=number['text'].strip(), unit='分', source_pages=[page_number]))
        for line_index, header in enumerate(lines):
            roles = {}
            for index, span in enumerate(header):
                cell = _compact_metric_label(span['text'])
                for role, aliases in header_roles.items():
                    if cell in aliases:
                        roles.setdefault(role, index)
            if not {'name', 'value'} <= roles.keys() or not ({'status', 'reference'} & roles.keys()):
                continue
            synthesis_table = (any(_compact_metric_label(s['text']) == '短链脂肪酸' for s in header)
                and any(_compact_metric_label(s['text']) == '合成能力评估' for s in header))
            header_y = baseline(header[0])
            heading = ' '.join(s['text'] for s in spans if baseline(s) < header_y)
            stop_y = min((baseline(next_line[0]) for next_line in lines[line_index + 1:]
                if any(re.search(r'检测结果说明|参考文献', s['text']) for s in next_line)), default=float('inf'))
            edges = [(header[i]['bbox'][0] + header[i + 1]['bbox'][0]) / 2 for i in range(len(header) - 1)]
            left_edge = header[0]['bbox'][0] - (header[1]['bbox'][0] - header[0]['bbox'][0]) / 2
            right_edge = header[-1]['bbox'][2] + (header[-1]['bbox'][0] - header[-2]['bbox'][0]) / 2
            def column(span):
                return sum(span['bbox'][0] >= edge for edge in edges)
            anchors = sorted([s for s in spans if column(s) == roles['value']
                and left_edge <= s['bbox'][0] < right_edge and header_y + 4 < baseline(s) < stop_y
                and _RESULT_VALUE_RE.fullmatch(s['text'].strip())], key=baseline)
            for index, anchor in enumerate(anchors):
                y = baseline(anchor)
                low = max(header_y + 4, y - 30, (baseline(anchors[index - 1]) + y) / 2 if index else y - 30)
                high = min(y + 25, (y + baseline(anchors[index + 1])) / 2 if index + 1 < len(anchors) else y + 25)
                cells = [''] * len(header)
                for span in sorted(spans, key=lambda s: (baseline(s), s['bbox'][0])):
                    if left_edge <= span['bbox'][0] < right_edge and low < baseline(span) < high:
                        cells[column(span)] += span['text']
                cells = [_clean_document_line(cell) for cell in cells]
                name = _cell(cells, roles['name'])
                compact = _compact_metric_label(name)
                if compact in {'肠道微生物健康指数', '肠道菌群健康指数'} and re.search(r'GMWI\s*2', heading, re.I):
                    code, name = 'gmwi2', '肠道菌群健康指数（GMWI2）'
                elif compact in {'肠道菌群种类评估', '肠道微生物种类评估', '肠道微生物种类', '菌种数'}:
                    code = 'species_count'
                elif compact in {'肠道菌群多样性评估', '菌群多样性评估', '菌群多样性', '肠道微生物多样性'}:
                    code = 'diversity'
                elif compact in {'肠道菌群致病潜力', '肠道菌群致病潜力评估'}:
                    code = 'pathogenic_potential'
                elif compact in {'肠道膳食纤维需求', '肠道膳食纤维需求评估'}:
                    code = 'dietary_fiber_need'
                elif compact in {'肠道菌群生物膜合成能力', '肠道菌群生物膜合成能力评估'}:
                    code = 'biofilm_capacity'
                elif compact in {'肠道菌群氧化胁迫耐受', '肠道菌群氧化胁迫耐受评估'}:
                    code = 'oxidative_stress_tolerance'
                elif compact in {'肠道免疫力', '肠道免疫力评估'}:
                    code = 'gut_immunity'
                elif compact in {'肠道菌群抗炎能力', '肠道菌群抗炎能力评估', '菌群抗炎能力评估'}:
                    code = 'anti_inflammatory_capacity'
                elif synthesis_table and compact in {'甲酸', '乙酸', '丙酸', '丁酸', '戊酸'}:
                    code = {'甲酸': 'formate', '乙酸': 'acetate', '丙酸': 'propionate',
                            '丁酸': 'butyrate', '戊酸': 'valerate'}[compact]
                elif compact in {'be比值', '肠道菌群平衡状态', '肠道菌群平衡状态评估'} and re.search(r'B\s*/\s*E', heading + ' ' + name, re.I):
                    code = 'be_ratio'
                elif compact in {'fb比值', '肠道菌群热量代谢fb比值', '肠道菌群热量代谢'} and re.search(r'F\s*/\s*B', heading + ' ' + name, re.I):
                    code = 'fb_ratio'
                else:
                    continue
                unit = _clean(_cell(cells, roles['unit'])) if 'unit' in roles else None
                if code in {'be_ratio', 'fb_ratio'} and (unit == '分' or re.search(
                    r'(?:B\s*/\s*E|F\s*/\s*B).{0,12}(?:评分|得分)', heading, re.I)):
                    code = 'be_score' if code == 'be_ratio' else 'fb_score'
                    name = ('B/E' if code == 'be_score' else 'F/B') + '比值评分'
                status = _clean(_cell(cells, roles['status'])) if 'status' in roles else None
                if code in {'formate', 'acetate', 'propionate', 'butyrate', 'valerate'} and status and not re.fullmatch(
                        r'(?:合成能力)?(?:强|较强|正常|一般|较弱|弱)', status):
                    status = None  # Multiple chart legend levels are not a printed patient direction.
                candidates.append(_NativeSourceMetric(code=code, name=name, value=anchor['text'].strip(),
                    unit=unit, status=status,
                    reference_range=_clean(_cell(cells, roles['reference'])) if 'reference' in roles else None,
                    source_pages=[page_number]))
    result = []
    for code in dict.fromkeys(item.code for item in candidates):
        group = [item for item in candidates if item.code == code]
        fields = {key: {re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)): value
            for item in reversed(group) if (value := _clean(getattr(item, key))) is not None}
            for key in ('value', 'unit', 'status', 'reference_range')}
        if any(len(values) > 1 for values in fields.values()):
            logger.warning('Report native metric conflict code=%s candidates=%s', code, len(group))
            continue
        # The detailed row's printed grade/range owns the primary locator;
        # an overview score on another page cannot inherit its printed grade.
        primary = max(group, key=lambda item: (
            item.status is not None, item.reference_range is not None, item.unit is not None))
        result.append(_NativeSourceMetric(code=code, name=group[0].name,
            source_pages=list(dict.fromkeys(page for item in group for page in item.source_pages)),
            source_page=primary.source_pages[0],
            **{key: next(iter(values.values())) if values else None for key, values in fields.items()}))
    return result


_NATIVE_RESULT_ROLES = {
    '项目名称': 'name',
    '检查项目': 'name',
    '检验项目': 'name',
    '检查结果': 'value',
    '检验结果': 'value',
    '参考值': 'reference',
    '参考范围': 'reference',
    '单位': 'unit',
}
_NATIVE_RESULT_BOUNDARY = re.compile(
    r'^(?:\d{1,3}[.、)]|[一二三四五六七八九十]+[.、)])?'
    r'(?:建议|[\u3400-\u9fff]{1,8}建议|说明|小结|结论|检查医生|检查者|(?:体检|检查|报告)(?:日期|时间))'
)
_NATIVE_HEADING_PREFIX = r'^(?:\d{1,3}[.、)]|[一二三四五六七八九十]+[.、)])?'
_NATIVE_NONPATIENT_HEADING = re.compile(
    _NATIVE_HEADING_PREFIX
    + r'(?:参考文献|参考资料|参考人群|参考案例|参考范围说明|示例|样例|历史(?:结果|记录|检测结果)|'
    r'历次(?:结果|记录)|上次(?:结果|检查结果)|检测原理|方法学)(?:[:：]|$)'
)
_NATIVE_CURRENT_HEADING = re.compile(
    _NATIVE_HEADING_PREFIX + r'(?:本次|当前)?(?:体检报告|体检结果|检查结果|检测结果|检验结果)[:：]?$'
)
_NATIVE_EXAM_DATE = re.compile(r'^(?:体检日期|检查日期|检查时间)[:：]?\s*(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})(?!\d)')


def _native_word_lines(words):
    """Group visible word boxes by line while keeping their page coordinates."""
    lines = []
    for word in sorted(words, key=lambda word: ((word[1] + word[3]) / 2, word[0])):
        middle = (word[1] + word[3]) / 2
        if not lines or middle - lines[-1][0] > 3:
            lines.append((middle, []))
        lines[-1][1].append(word)
    return [(middle, sorted(line, key=lambda word: word[0])) for middle, line in lines]


def _native_word_text(words):
    return ''.join(unicodedata.normalize('NFKC', ' '.join(word[4] for word in words)).split())


def _native_page_exam_date(lines):
    """Use an unambiguous examination date printed in this page's header."""
    dates = set()
    for middle, line in lines:
        if middle > 110:
            break
        for index in range(len(line)):
            if match := _NATIVE_EXAM_DATE.match(_native_word_text(line[index : index + 4])):
                try:
                    dates.add(date(*(int(item) for item in match.groups())).isoformat())
                except ValueError:
                    return None
    return next(iter(dates)) if len(dates) == 1 else None


def _native_result_groups(line, width):
    headers = [
        (word[0], role) for word in line if (role := _NATIVE_RESULT_ROLES.get(_native_word_text((word,)))) is not None
    ]
    for index in range(len(headers) - 3):
        group = headers[index : index + 4]
        if [role for _, role in group] == ['name', 'value', 'reference', 'unit']:
            end = next((x for x, role in headers[index + 4 :] if role == 'name'), width)
            yield (*[x - 1 for x, _ in group], end - 1)


def _native_normalized_bounds(words, page_size):
    if not words or page_size is None:
        return None
    width, height = page_size
    left = min(word[0] for word in words) / width
    top = min(word[1] for word in words) / height
    right = max(word[2] for word in words) / width
    bottom = max(word[3] for word in words) / height
    if 0 <= left < right <= 1 and 0 <= top < bottom <= 1:
        return left, top, right, bottom
    return None


def _native_result_group_rows(lines, header_index, columns, page_number, observed_at, page_size):
    previous = lines[header_index][0]
    for middle, line in lines[header_index + 1 :]:
        text = _native_word_text(line)
        if (
            middle - previous > 24
            or _NATIVE_RESULT_BOUNDARY.match(text)
            or _NATIVE_NONPATIENT_HEADING.match(text)
            or _NATIVE_CURRENT_HEADING.match(text)
            or any(_NATIVE_RESULT_ROLES.get(_native_word_text((word,))) == 'name' for word in line)
        ):
            break
        previous = middle
        cells = [[word for word in line if left <= word[0] < right] for left, right in zip(columns, columns[1:])]
        label, value, reference, unit = map(_native_word_text, cells)
        if _NATIVE_RESULT_BOUNDARY.match(label):
            break
        if label in _NATIVE_RESULT_ROLES or not _looks_like_item_name(label):
            continue
        flag = None
        if value.endswith(('↑', '↓')) and value[-2:-1] not in ('↑', '↓'):
            flag = _status_from_source(value[-1])
            value = value[:-1]
        if _RESULT_VALUE_RE.fullmatch(value):
            if not _looks_like_unit(unit):
                continue
        elif _CATEGORICAL_RESULT_RE.fullmatch(value):
            if unit not in {'', '-', '/'} and not _looks_like_unit(unit):
                continue
        else:
            continue
        if reference and not _REFERENCE_VALUE_RE.fullmatch(reference):
            continue
        yield NativeResultRow(
            locator=f'native:result:{page_number}:{round(middle * 10)}:{round((columns[0] + 1) * 10)}',
            page=page_number,
            label=label,
            value=value,
            unit=unit if unit not in {'', '-', '/'} else None,
            reference_range=reference if reference not in {'', '-'} else None,
            flag=flag,
            date=observed_at,
            result_bounds=_native_normalized_bounds(cells[1], page_size),
            name_bounds=_native_normalized_bounds(cells[0], page_size),
        )


def _native_result_page(page, page_number, nonpatient):
    lines = _native_word_lines(page.get_text('words'))
    observed_at = _native_page_exam_date(lines)
    width, height = page.cropbox.width, page.cropbox.height
    page_size = (width, height) if width > 0 and height > 0 and not page.rotation else None
    rows = []
    blocked_from = 0.0 if nonpatient else None
    nonpatient_ranges = []
    for index, (middle, line) in enumerate(lines):
        text = _native_word_text(line)
        if blocked_from is None and _NATIVE_NONPATIENT_HEADING.match(text):
            blocked_from = max(0.0, middle - 3)
        elif blocked_from is not None and _NATIVE_CURRENT_HEADING.match(text):
            nonpatient_ranges.append((blocked_from, middle))
            blocked_from = None
        if blocked_from is not None:
            continue
        for columns in _native_result_groups(line, width):
            rows.extend(_native_result_group_rows(lines, index, columns, page_number, observed_at, page_size))
    if blocked_from is not None:
        nonpatient_ranges.append((blocked_from, height))
    return _NativeProofPage(rows, observed_at, page_size, nonpatient_ranges, blocked_from is not None)


def _extract_pdf(
    filename: str, content: bytes, *, ocr: bool = True, prove_result_rows: bool = False
) -> ExtractedDocument:
    from .evaluation_native_worker import extract_in_native_process
    from .evaluation_native_worker import native_process_enabled

    if native_process_enabled():
        return extract_in_native_process(
            filename, content, source_type='pdf', ocr=ocr, prove_result_rows=prove_result_rows
        )
    text_parts: list[str] = []
    tables: list[ExtractedTable] = []
    metadata: dict[str, str] = {'content_sha256': hashlib.sha256(content).hexdigest()}
    ocr_pages: list[int] = []
    ocr_failures: list[int] = []
    actual_text_characters = 0
    native_pages: list[tuple[int, dict]] = []
    native_result_rows: list[NativeResultRow] = []
    native_page_dates: dict[int, str] = {}
    proof_pages: dict[int, _NativeProofPage] = {}
    nonpatient = False
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise SourceParseError("PDF cannot be opened") from exc
    with document:
        for page_index, page in enumerate(document, start=1):
            if prove_result_rows:
                try:
                    proof = _native_result_page(page, page_index, nonpatient)
                except (RuntimeError, ValueError):  # Native proof is optional for scanned pages.
                    pass
                else:
                    proof_pages[page_index] = proof
                    nonpatient = proof.nonpatient_ongoing
                    native_result_rows.extend(proof.rows)
                    if proof.date is not None:
                        native_page_dates[page_index] = proof.date
            try:
                native = page.get_text('dict')
                if isinstance(native, dict):
                    native_pages.append((page_index, native))
            except Exception:  # Native geometry is optional for scans.
                pass
            native_text = page.get_text("text") or ""
            page_text = native_text
            if ocr and _pdf_page_needs_ocr(page, native_text):
                try:
                    text_page = page.get_textpage_ocr(
                        language="chi_sim+eng",
                        dpi=300,
                        full=True,
                    )
                    ocr_text = page.get_text("text", textpage=text_page) or ""
                    if _meaningful_text_length(ocr_text) > _meaningful_text_length(
                        native_text
                    ):
                        page_text = ocr_text
                        ocr_pages.append(page_index)
                except Exception:  # noqa: BLE001 - DocMind remains the fallback.
                    ocr_failures.append(page_index)
            actual_text_characters += _meaningful_text_length(page_text)
            text_parts.append(f"\n===== PAGE {page_index} =====\n{page_text}")
            if enterotype := _detect_enterotype(page, page_text):
                metadata["enterotype"] = enterotype
            try:
                found = page.find_tables().tables
            except Exception:  # noqa: BLE001 - table detection is optional per page.
                found = []
            blocks = [
                (float(row[1]), float(row[3]), _clean(row[4]) or "")
                for row in page.get_text("blocks")
                if len(row) >= 5
            ]
            for table in found:
                rows = _clean_table(table.extract())
                if not rows:
                    continue
                for index, table_row in enumerate(table.rows):
                    if index >= len(rows):
                        break
                    status = _visual_table_row_status(page, table_row.bbox)
                    if not status:
                        continue
                    if len(rows[index]) >= 4 and not rows[index][-1]:
                        rows[index][-1] = status
                    elif status not in rows[index]:
                        rows[index].append(status)
                top = float(table.bbox[1])
                context = "\n".join(text for _y0, y1, text in blocks if y1 <= top + 2)[
                    -4000:
                ]
                tables.append(ExtractedTable(page_index, rows, context))
    tables.extend(_native_clinical_tables(native_pages, proof_pages=proof_pages))
    text = "".join(text_parts)
    if actual_text_characters == 0:
        raise SourceParseError("PDF contains no extractable text")
    if ocr_pages:
        metadata["ocr_pages"] = ",".join(str(item) for item in ocr_pages)
    if ocr_failures:
        metadata["ocr_failed_pages"] = ",".join(
            str(item) for item in ocr_failures
        )
    return ExtractedDocument(
        filename,
        'pdf',
        text,
        tables,
        metadata,
        _native_source_metrics(native_pages),
        tuple(native_result_rows),
        native_page_dates,
    )


def _meaningful_text_length(text: str) -> int:
    """Count visible source content and ignore synthetic page separators."""

    return len(re.findall(r"[A-Za-z0-9\u3400-\u9fff]", text or ""))


def _pdf_page_needs_ocr(page: Any, native_text: str) -> bool:
    """Choose OCR per page so native and scanned pages can coexist."""

    visible = _meaningful_text_length(native_text)
    if visible < 24:
        return True
    if visible >= 180:
        return False
    try:
        page_area = max(1.0, float(page.rect.width) * float(page.rect.height))
        covered = 0.0
        for image in page.get_images(full=True):
            for rect in page.get_image_rects(int(image[0])):
                covered += max(0.0, float(rect.width) * float(rect.height))
        return covered / page_area >= 0.45
    except Exception:  # noqa: BLE001 - image geometry is only an OCR hint.
        return False


def _extract_image(filename: str, content: bytes) -> ExtractedDocument:
    from .evaluation_native_worker import native_process_enabled, extract_in_native_process
    if native_process_enabled():
        return extract_in_native_process(filename, content, source_type='image')
    try:
        document = pymupdf.open(stream=content)
        pdf_bytes = document.convert_to_pdf()
        document.close()
        pdf = pymupdf.open("pdf", pdf_bytes)
        page = pdf[0]
        text_page = page.get_textpage_ocr(language="chi_sim+eng", dpi=300, full=True)
        text = page.get_text("text", textpage=text_page)
        pdf.close()
    except Exception as exc:
        raise SourceParseError(
            "image OCR is unavailable or produced no content"
        ) from exc
    if not text.strip():
        raise SourceParseError("image OCR produced no text")
    metadata: dict[str, str] = {}
    if match := _ENTEROTYPE_TEXT.search(text):
        metadata["enterotype"] = _normalize_enterotype(match.group(1))
    return ExtractedDocument(filename, "image", text, metadata=metadata)


def _document_metrics(document: ExtractedDocument) -> list[SourceMetric]:
    metrics = _document_table_metrics(document)
    table_codes = {item.code for item in metrics}
    for code, name, patterns, unit in _DOCUMENT_METRICS:
        if code in table_codes:
            continue
        value = None
        status = None
        reference_range = None
        for pattern in patterns:
            if match := re.search(pattern, document.text, flags=re.IGNORECASE):
                value = _clean(match.group(1))
                status = _metric_status_after_value(document.text, match.end())
                reference_range = _metric_reference_after_value(
                    document.text,
                    match.end(),
                )
                break
        if value is not None:
            metrics.append(
                SourceMetric(
                    code=code,
                    name=name,
                    value=value,
                    unit=unit,
                    status=status,
                    reference_range=reference_range,
                )
            )
    if enterotype := document.metadata.get("enterotype"):
        metrics.append(
            SourceMetric(
                code="enterotype", name="肠型", value=enterotype, status=enterotype
            )
        )
    metrics.extend(_document_panel_metrics(document.text))
    metrics.extend(_document_function_metrics(document))
    metrics.extend(_document_scored_function_metrics(document.text))
    return _dedupe(metrics, key=lambda item: item.code)


def _document_table_metrics(document: ExtractedDocument) -> list[SourceMetric]:
    """Read provider assessment tables without depending on OCR text order."""

    metrics: list[SourceMetric] = []
    for table in document.tables:
        for header_index, header in enumerate(table.rows):
            normalized = [_compact_metric_label(value) for value in header]
            value_index = next(
                (
                    index
                    for index, value in enumerate(normalized)
                    if value in {"评估值", "检测值"}
                ),
                None,
            )
            reference_index = next(
                (
                    index
                    for index, value in enumerate(normalized)
                    if value in {"参考范围", "阈值"}
                ),
                None,
            )
            status_index = next(
                (
                    index
                    for index, value in enumerate(normalized)
                    if value in {"结果评价", "合成能力评估"}
                ),
                None,
            )
            if value_index is None or status_index is None:
                continue
            for row in table.rows[header_index + 1 :]:
                if not row:
                    continue
                label = _clean(row[0])
                definition = _table_metric_definition(label)
                if definition is None:
                    continue
                code, name, unit = definition
                value = _clean(row[value_index]) if value_index < len(row) else None
                status = _clean(row[status_index]) if status_index < len(row) else None
                reference_range = (
                    _clean(row[reference_index])
                    if reference_index is not None and reference_index < len(row)
                    else None
                )
                if not value and not status:
                    continue
                metrics.append(
                    SourceMetric(
                        code=code,
                        name=name,
                        value=value,
                        unit=unit,
                        status=status,
                        reference_range=reference_range,
                    )
                )
            break
    return _dedupe(metrics, key=lambda item: item.code)


def _table_metric_definition(label: str | None) -> tuple[str, str, str | None] | None:
    normalized = _compact_metric_label(label or "")
    for code, name, aliases, unit in _TABLE_METRIC_DEFINITIONS:
        if any(_compact_metric_label(alias) in normalized for alias in aliases):
            return code, name, unit
    return None


def _compact_metric_label(value: str | None) -> str:
    return re.sub(r"[\s_()（）/\\:：\-]+", "", str(value or "")).casefold()


def _metric_status_after_value(text: str, value_end: int) -> str | None:
    tail = text[value_end : value_end + 96]
    # Pipe-table rows are self-contained.  Do not borrow the status from the
    # next row when the current indicator is normal or has an empty cell.
    nearby = tail.splitlines()[0] if re.match(r"\s*[|｜]", tail) else tail
    statuses = (
        "重度失衡",
        "中度失衡",
        "轻度失衡",
        "不健康倾向",
        "健康倾向",
        "不健康",
        "亚健康",
        "重度超标",
        "中度超标",
        "轻度超标",
        "重度缺乏",
        "中度缺乏",
        "轻度缺乏",
        "亚健康",
        "偏高",
        "偏低",
        "稍弱",
        "较弱",
        "需求高",
        "合成能力正常",
        "合成能力弱",
        "合成能力强",
        "正常",
        "优秀",
        "健康",
        "失衡",
        "异常",
    )
    return next((status for status in statuses if status in nearby), None)


def _metric_reference_after_value(text: str, value_end: int) -> str | None:
    """Read the reference-range cell from a DocMind pipe-table row."""

    nearby = text[value_end : value_end + 400]
    match = re.match(
        r"\s*[|｜]\s*[^|｜\n]{0,80}[|｜]\s*([^|｜\n]{1,260})[|｜]",
        nearby,
    )
    if not match:
        return None
    value = _clean(match.group(1))
    return value if value and re.search(r"\d|[<>≤≥]", value) else None


def _document_function_metrics(document: ExtractedDocument) -> list[SourceMetric]:
    """Preserve gut-barrier and SCFA function rows from microbiome reports."""

    definitions = (
        ("gut_barrier", "肠道屏障功能", ("肠道屏障", "屏障功能")),
        ("anti_inflammatory", "肠道抗炎能力", ("肠道抗炎能力", "抗炎能力")),
        ("gut_immunity", "肠道免疫力", ("肠道免疫力", "免疫调节能力")),
        ("acetate", "乙酸合成能力", ("乙酸合成", "乙酸产生", "乙酸")),
        ("propionate", "丙酸合成能力", ("丙酸合成", "丙酸产生", "丙酸")),
        ("butyrate", "丁酸合成能力", ("丁酸合成", "丁酸产生", "丁酸")),
        ("scfa", "短链脂肪酸合成能力", ("短链脂肪酸合成", "短链脂肪酸产生")),
        ("calprotectin", "粪钙卫蛋白", ("粪钙卫蛋白", "calprotectin")),
        ("zonulin", "Zonulin", ("zonulin", "连蛋白")),
        ("dao", "二胺氧化酶", ("二胺氧化酶", "dao")),
        ("d_lactate", "D-乳酸", ("d-乳酸", "d乳酸", "d-lactate")),
        (
            "endotoxin",
            "内毒素相关指标",
            ("lps", "lbp", "内毒素", "endotoxin", "lipopolysaccharide"),
        ),
    )
    results: list[SourceMetric] = []
    status_tokens = tuple(
        dict.fromkeys(
            (
                *_TABLE_RESULT_STATUSES,
                "强",
                "较强",
                "一般",
                "较弱",
                "弱",
                "正常",
                "异常",
                "优秀",
            )
        )
    )
    for code, name, aliases in definitions:
        matched = False
        status_only: str | None = None
        for table in document.tables:
            for row in table.rows:
                text = " ".join(cell.strip() for cell in row if cell.strip())
                if not any(alias.casefold() in text.casefold() for alias in aliases):
                    continue
                status = next(
                    (
                        token
                        for token in reversed(status_tokens)
                        if any(
                            (
                                _clean(cell) == token
                                or (
                                    len(_clean(cell) or "") <= 20
                                    and re.match(
                                        rf"^{re.escape(token)}\s*[\uff08(]?[<>≤≥]",
                                        _clean(cell) or "",
                                    )
                                )
                            )
                            for cell in row[1:]
                        )
                    ),
                    None,
                )
                values = [
                    cleaned
                    for cell in row[1:]
                    if (cleaned := _clean(cell))
                    and cleaned != status
                    and re.fullmatch(
                        r"[-+]?\d+(?:\.\d+)?\s*%?(?:\s*分)?",
                        cleaned,
                    )
                ]
                value = values[0] if values else None
                status = _function_score_status(document.text, value, status)
                if status or value:
                    results.append(
                        SourceMetric(
                            code=code,
                            name=name,
                            value=value,
                            status=status,
                            reference_range=_function_score_reference_range(
                                document.text
                            ),
                        )
                    )
                    matched = True
                    break
            if matched:
                break
        if matched:
            continue
        for alias in aliases:
            pattern = rf"{re.escape(alias)}\s*[:：]?\s*(?:\n\s*)?([^\n；;]{{1,40}})"
            for match in re.finditer(pattern, document.text, flags=re.IGNORECASE):
                raw = match.group(1).strip()
                source_status = next(
                    (
                        token
                        for token in status_tokens
                        if re.fullmatch(
                            rf"(?:[-+]?\d+(?:\.\d+)?\s*%?\s*)?{re.escape(token)}",
                            raw,
                        )
                    ),
                    None,
                )
                value_match = re.search(r"[-+]?\d+(?:\.\d+)?\s*%?", raw)
                value = value_match.group() if value_match else None
                # A section number immediately following a contents heading is
                # not a function score.  Without a compact status token, only
                # accept an explicitly labelled ``name: value`` form.
                has_explicit_separator = bool(
                    re.match(
                        r"\s*[:：]",
                        document.text[
                            match.start() + len(alias) : match.start(1)
                        ],
                    )
                )
                has_compact_value = bool(
                    re.fullmatch(r"[-+]?\d+(?:\.\d+)?\s*%?", raw)
                )
                if value and (
                    source_status or has_explicit_separator or has_compact_value
                ):
                    status = _function_score_status(
                        document.text,
                        value,
                        source_status,
                    )
                    results.append(
                        SourceMetric(
                            code=code,
                            name=name,
                            value=value,
                            status=status,
                            reference_range=_function_score_reference_range(
                                document.text
                            ),
                        )
                    )
                    matched = True
                    break
                status_only = status_only or source_status
            if matched:
                break
        if not matched and status_only and code != "scfa":
            results.append(
                SourceMetric(code=code, name=name, value=None, status=status_only)
            )
    return results


def _function_score_status(
    document_text: str,
    value: str | None,
    status: str | None,
) -> str | None:
    if status or value is None:
        return status
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    if not match:
        return None
    number = float(match.group())
    if (
        re.search(r">\s*80", document_text)
        and re.search(r"60\s*[-–—至]\s*80", document_text)
        and re.search(r"<\s*60", document_text)
    ):
        return "弱" if number < 60 else "正常" if number <= 80 else "强"
    if (
        re.search(r"检测结果\s*<\s*30", document_text)
        and re.search(r"30\s*[-–—至]\s*60", document_text)
        and re.search(r"检测结果\s*>\s*60", document_text)
    ):
        return "弱" if number < 30 else "稍弱" if number <= 60 else "正常"
    return None


def _function_score_reference_range(document_text: str) -> str | None:
    """Return the normal boundary explicitly printed by the source report."""

    if (
        re.search(r"检测结果\s*<\s*30", document_text)
        and re.search(r"30\s*[-–—至]\s*60", document_text)
        and re.search(r"检测结果\s*>\s*60", document_text)
    ):
        return ">60"
    if (
        re.search(r">\s*80", document_text)
        and re.search(r"60\s*[-–—至]\s*80", document_text)
        and re.search(r"<\s*60", document_text)
    ):
        return "60–80"
    return None


def _document_panel_metrics(text: str) -> list[SourceMetric]:
    results: list[SourceMetric] = []
    definitions = (
        ("有益菌", "panel_beneficial", _beneficial_panel_status),
        ("有害菌", "panel_harmful", _harmful_panel_status),
    )
    for label, code, status_fn in definitions:
        matches = re.findall(
            rf"(?:种类\s*\n\s*总检出丰度\s*\n\s*结果评价\s*\n\s*)?{label}"
            rf"\s*(?:\n\s*)?(\d+(?:\.\d+)?)\s*%",
            text,
        )
        if not matches:
            continue
        value = matches[-1]
        results.append(
            SourceMetric(
                code=code,
                name=f"{label}总检出丰度",
                value=value,
                unit="%",
                status=status_fn(float(value)),
            )
        )
    return results


def _document_scored_function_metrics(text: str) -> list[SourceMetric]:
    """Parse provider score pages whose OCR loses table geometry.

    These pages use one stable boundary: below 30 is weak, 30 through 60 is
    slightly weak, and above 60 is normal.  Values and statuses remain source
    facts; the function does not infer taxa or disease associations.
    """

    definitions = (
        ("gut_overall_score", "肠道总评分", ("您的肠道总评分为",)),
        ("be_ratio", "肠道微生物定植抗力", ("肠道微生物定植抗力",)),
        ("metabolic_capacity", "肠道代谢能力评估", ("肠道代谢能力评估",)),
        ("synthesis_capacity", "肠道合成能力评估", ("肠道合成能力评估",)),
        ("fb_ratio", "厚壁菌门/拟杆菌门", ("厚壁菌门/拟杆菌门",)),
        (
            "carbohydrate_metabolism",
            "糖（碳水化合物）代谢能力",
            ("糖(碳水化合物)", "糖（碳水化合物）"),
        ),
        ("protein_metabolism", "蛋白质代谢能力", ("蛋白质",)),
        ("fat_metabolism", "脂肪代谢能力", ("脂肪",)),
        ("purine_metabolism", "嘌呤代谢能力", ("嘌呤",)),
        ("vitamin_a", "维生素A合成能力", ("维生素A",)),
        ("vitamin_b1", "维生素B1合成能力", ("维生素B1",)),
        ("vitamin_b2", "维生素B2合成能力", ("维生素B2",)),
        ("vitamin_b3", "维生素B3合成能力", ("维生素B3",)),
        ("vitamin_b5", "维生素B5合成能力", ("维生素B5",)),
        ("vitamin_b6", "维生素B6合成能力", ("维生素B6",)),
        ("vitamin_b7", "维生素B7合成能力", ("维生素B7",)),
        ("vitamin_b9", "维生素B9合成能力", ("维生素B9",)),
        ("vitamin_b12", "维生素B12合成能力", ("维生素B12",)),
        ("vitamin_c", "维生素C合成能力", ("维生素C",)),
        ("vitamin_d", "维生素D合成能力", ("维生素D",)),
        ("vitamin_e", "维生素E合成能力", ("维生素E",)),
        ("vitamin_k", "维生素K合成能力", ("维生素K",)),
        ("carotenoid", "类胡萝卜素合成能力", ("类胡萝卜素",)),
        ("flavonoid", "类黄酮合成能力", ("类黄酮",)),
        ("anthocyanin", "花青素合成能力", ("花青素",)),
    )
    rows: list[SourceMetric] = []
    source_score_reference = _function_score_reference_range(text)
    for code, name, aliases in definitions:
        value: str | None = None
        for alias in aliases:
            match = re.search(
                rf"{re.escape(alias)}\s*(?:\n\s*)?(?:[:：|]\s*)?(-?\d+(?:\.\d+)?%?)",
                text,
                flags=re.IGNORECASE,
            )
            if match:
                value = match.group(1)
                break
        if value is None:
            continue
        numeric_match = re.search(r"-?\d+(?:\.\d+)?", value)
        numeric = float(numeric_match.group()) if numeric_match else None
        status = None
        unit = "%" if value.endswith("%") else None
        if code == "gut_overall_score":
            nearby = text[match.end() : match.end() + 40] if match else ""
            status_match = re.search(r"(重度失衡|中度失衡|轻度失衡|正常)", nearby)
            status = status_match.group(1) if status_match else None
            unit = "分"
        elif (
            code
            in {
                "anti_inflammatory",
                "gut_immunity",
                "acetate",
                "propionate",
                "butyrate",
                "carbohydrate_metabolism",
                "protein_metabolism",
                "fat_metabolism",
                "purine_metabolism",
                "vitamin_a",
                "vitamin_b1",
                "vitamin_b2",
                "vitamin_b3",
                "vitamin_b5",
                "vitamin_b6",
                "vitamin_b7",
                "vitamin_b9",
                "vitamin_b12",
                "vitamin_c",
                "vitamin_d",
                "vitamin_e",
                "vitamin_k",
                "carotenoid",
                "flavonoid",
                "anthocyanin",
            }
            and numeric is not None
        ):
            status = "弱" if numeric < 30 else "稍弱" if numeric <= 60 else "正常"
            unit = "分"
        rows.append(
            SourceMetric(
                code=code,
                name=name,
                value=value,
                unit=unit,
                status=status,
                reference_range=(
                    source_score_reference
                    if status in {"弱", "稍弱", "正常", "强"}
                    else None
                ),
            )
        )
    return rows


def _beneficial_panel_status(value: float) -> str:
    if value < 5:
        return "重度缺乏"
    if value < 10:
        return "中度缺乏"
    if value < 20:
        return "轻度缺乏"
    if value <= 30:
        return "正常"
    return "优秀"


def _harmful_panel_status(value: float) -> str:
    if value > 10:
        return "重度超标"
    if value > 5:
        return "中度超标"
    if value > 3:
        return "轻度超标"
    if value >= 1:
        return "正常"
    return "优秀"


def _document_taxa(document: ExtractedDocument) -> list[ParsedTaxon]:
    results: list[ParsedTaxon] = []
    known_genera = {
        latin.split()[0]
        for table in document.tables
        for row in table.rows
        if row
        for _chinese, latin in [_split_taxon_name(row[0])]
        if latin and " " in latin
    }
    active_category: str | None = None
    active_taxon_layout = False
    pending_split_row: tuple[str, str, str | None, str] | None = None
    for table in document.tables:
        header_text = " ".join(table.rows[0]) if table.rows else ""
        if ("相关性" in header_text and "参考范围" in header_text) or (
            "拉丁学名" in header_text and "人群检出率" in header_text
        ):
            active_category = None
        # Only preceding page text may switch a continuing table's section.
        # Row descriptions frequently contain words such as "致病菌" and must
        # not reclassify beneficial species.
        context_category = _category_from_text(table.context)
        if context_category:
            active_category = context_category
            active_taxon_layout = True
        elif _taxon_section_ended(table.context):
            active_category = None
            active_taxon_layout = False
        is_combined_panel_header = "菌名" in header_text and (
            "检测结果" in header_text or "丰度" in header_text
        )
        header = table.rows[0] if table.rows else []
        indexes = _microbiome_panel_indexes(header)
        if is_combined_panel_header or indexes is not None:
            active_taxon_layout = True
        elif not active_taxon_layout:
            continue
        for row in table.rows:
            joined = " ".join(row)
            if category := _category_from_aggregate_row(row):
                active_category = category
                continue
            if "菌名" in joined and ("检测结果" in joined or "丰度" in joined):
                continue
            if active_category is None or len(row) < 2:
                continue

            # Metagenomic reports use a regular four/five-column panel table.
            # Parse it by header semantics instead of treating the first cell as
            # a combined Chinese/Latin name (the 16S layout below does that).
            if indexes is not None:
                if row is header or _is_microbiome_panel_header(row):
                    continue
                panel_row = _microbiome_panel_row(
                    row,
                    indexes,
                    known_genera=known_genera,
                )
                if panel_row is None:
                    continue
                chinese, latin, value, reference_range, status = panel_row
                results.append(
                    ParsedTaxon(
                        taxon_id=stable_id("taxon", latin),
                        category=active_category,
                        latin_name=latin,
                        chinese_name=chinese,
                        value=value,
                        reference_range=reference_range,
                        source_status=status,
                        explicitly_abnormal=(
                            _is_explicit_abnormal(status, False)
                        ),
                        detection_status=detection_status(value, status=status),
                    )
                )
                continue

            name_cell = row[0]
            value = _taxon_value(row)
            if not name_cell:
                continue
            chinese, latin = _split_taxon_name(
                name_cell,
                known_genera=known_genera,
            )
            status = _taxon_status(row)
            if latin and value is None and pending_split_row and not chinese:
                pending_name, pending_value, pending_status, pending_category = (
                    pending_split_row
                )
                chinese = pending_name
                value = pending_value
                status = status or pending_status
                active_category = pending_category
                pending_split_row = None
            if (
                not latin
                and value is not None
                and status is not None
                and re.search(r"[\u4e00-\u9fff]", name_cell)
            ):
                pending_split_row = (name_cell, value, status, active_category)
                continue
            if value is None:
                continue
            if not latin:
                continue
            results.append(
                ParsedTaxon(
                    taxon_id=stable_id("taxon", latin),
                    category=active_category,
                    latin_name=latin,
                    chinese_name=_normalize_source_chinese_name(chinese),
                    value=value,
                    source_status=status,
                    explicitly_abnormal=_is_explicit_abnormal(status, False),
                    detection_status=detection_status(value, status=status),
                )
            )
    return _merge_document_taxa(results)


def _document_taxa_from_linear_text(text: str) -> list[ParsedTaxon]:
    """Parse DocMind/OCR reading order when no geometric table is available."""
    section_markers: list[tuple[int, str]] = []
    marker_pattern = re.compile(
        r"(?:^|\n)\s*(?:\d+(?:\.\d+)*\s*)?"
        r"(潜在致病真菌|条件致病菌|有益菌|有害菌|致病菌)"
        r"(?=\s*(?:\n|是|又称))",
    )
    for marker in marker_pattern.finditer(text):
        category = _category_from_text(marker.group(1))
        if category:
            section_markers.append((marker.start(), category))

    status_pattern = "|".join(map(re.escape, _TABLE_RESULT_STATUSES))
    row_pattern = re.compile(
        r"(?P<chinese>[\u4e00-\u9fff][^\n]{0,40})\s*\n"
        r"(?P<latin>[#*]?[A-Z][A-Za-z.-]+(?:[ \t\n]+[a-z][A-Za-z.-]+)?)\s*\n"
        r"(?P<body>.{0,1800}?)"
        r"(?P<value><\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?)\s*%?\s*\n"
        rf"(?P<status>{status_pattern})(?:\s|$)",
        flags=re.DOTALL,
    )
    taxa: list[ParsedTaxon] = []
    for match in row_pattern.finditer(text):
        category = _category_at(section_markers, match.start())
        if category is None:
            continue
        latin = " ".join(match.group("latin").replace("#", "").replace("*", "").split())
        value = re.sub(r"\s+", "", match.group("value"))
        status = match.group("status")
        taxa.append(
            ParsedTaxon(
                taxon_id=stable_id("taxon", latin),
                category=category,
                latin_name=latin,
                chinese_name=_normalize_source_chinese_name(match.group("chinese")),
                value=value,
                source_status=status,
                explicitly_abnormal=_is_explicit_abnormal(status, False),
                detection_status=detection_status(value, status=status),
            )
        )
    return _dedupe(taxa, key=lambda item: item.taxon_id)


def _category_at(markers: list[tuple[int, str]], offset: int) -> str | None:
    category = None
    for position, candidate in markers:
        if position > offset:
            break
        category = candidate
    return category


def _document_diseases(document: ExtractedDocument) -> list[ParsedDisease]:
    results: list[ParsedDisease] = []

    # Summary tables are the most reliable source when available.  They also
    # preserve related-taxon labels without trying to interpret them.
    for table in document.tables:
        if not table.rows:
            continue
        header = table.rows[0]
        score_index = _header_index_any(
            header,
            "风险指数",
            "风险评分",
            "模型指数",
            "模型分值",
            "风险值",
        )
        level_index = _header_index_any(
            header,
            "风险等级",
            "风险评估",
            "风险级别",
            "检测结果",
        )
        # ``名称`` is accepted only after the row has been proven to be a
        # risk table, so arbitrary clinical tables cannot enter this parser.
        disease_index = (
            _header_index_any(
                header,
                "疾病名称",
                "疾病类型",
                "疾病或健康问题",
                "健康问题",
                "风险名称",
                "名称",
            )
            if score_index is not None and level_index is not None
            else None
        )
        related_index = _header_index_any(
            header,
            "正负相关菌",
            "相关菌群",
            "关联菌群",
            "相关菌",
        )
        data_rows = table.rows[1:]
        if (
            (disease_index is None or score_index is None or level_index is None)
            and _disease_risk_context_active(table.context)
            and len(header) >= 3
            and _looks_like_disease_or_health_problem(_cell(header, 0))
            and _source_risk_score(_cell(header, 1))
            and _source_risk_level(_cell(header, 2))
        ):
            # OCR can interrupt one Markdown table with a page header. The
            # continuation then starts directly with a data row. Reuse the
            # semantic three-column layout only while the surrounding context
            # remains inside the same disease-risk section.
            disease_index, score_index, level_index = 0, 1, 2
            related_index = 3 if len(header) > 3 else None
            data_rows = table.rows
        if (
            disease_index is not None
            and score_index is not None
            and level_index is not None
        ):
            for row in data_rows:
                name = _normalize_source_disease_name(_cell(row, disease_index))
                score = _source_risk_score(_cell(row, score_index))
                level = _source_risk_level(_cell(row, level_index))
                if not name or not score or not level:
                    continue
                results.append(
                    ParsedDisease(
                        disease_id=stable_id("disease", name),
                        name=name,
                        source_score=score,
                        source_level=level,
                        related_taxa=_split_source_related_taxa(
                            _cell(row, related_index)
                        ),
                    )
                )
            continue

        # Some provider PDFs ship an unresolved export template: the disease
        # name is present, while score and level cells contain literal image
        # placeholders such as ``<constipation_png>`` and ``<constipation>``.
        # Those rows are still authoritative source items.  Preserve their
        # names one-for-one, but never infer a score or risk level from the
        # placeholder, disease prose, abnormal taxa or external evidence.
        if len(header) >= 3:
            for row in table.rows:
                name = _normalize_source_disease_name(_cell(row, 0))
                score_placeholder = _cell(row, 1)
                level_placeholder = _cell(row, 2)
                if (
                    not name
                    or not _looks_like_disease_or_health_problem(name)
                    or not _is_source_image_placeholder(score_placeholder)
                    or not _is_source_image_placeholder(level_placeholder)
                ):
                    continue
                results.append(
                    ParsedDisease(
                        disease_id=stable_id("disease", name),
                        name=name,
                    )
                )

    # Full-page disease cards contain the complete source list, including
    # clear zero-valued model results omitted from the overview table.  The
    # repeated title is required so explanatory prose cannot be mistaken for
    # a patient result.
    for page_text in _document_pages(document.text):
        results.extend(_disease_similarity_results(page_text))
        match = re.search(
            r"^\s*(?P<name>[^\n]{2,60})\s*\n"
            r"\s*疾病风险评估\s*\n"
            r"\s*(?P=name)\s*\n"
            r"\s*风险为\s*[:：]?\s*\n?"
            r"\s*(?P<score>\d{1,3}(?:\.\d+)?)\s*\n"
            r"\s*(?P<level>低风险|中低风险|中风险|中高风险|高风险)",
            page_text,
        )
        if not match:
            continue
        name = _normalize_source_disease_name(match.group("name"))
        if not name:
            continue
        results.append(
            ParsedDisease(
                disease_id=stable_id("disease", name),
                name=name,
                source_score=match.group("score"),
                source_level=match.group("level"),
            )
        )

    for block in _disease_risk_text_blocks(document.text):
        for score, name, level in re.findall(
            r"(?m)^[ \t]*(\d+(?:\.\d+)?)[ \t]*[]\[()DIl<>-]*[ \t]*"
            r"([Ⅰ-ⅿ2㐀-鿿]{2,30}?)[ \t]*"
            r"(低风险|中低风险|中风险|中高风险|高风险)[ \t]*$",
            block,
        ):
            cleaned_name = _normalize_source_disease_name(name)
            if not cleaned_name or not _looks_like_disease_or_health_problem(
                cleaned_name
            ):
                continue
            results.append(
                ParsedDisease(
                    disease_id=stable_id("disease", cleaned_name),
                    name=cleaned_name,
                    source_score=score,
                    source_level=level,
                )
            )
    # Prefer the overview row when it carries related taxa, otherwise retain
    # the detailed-card row.  Merge without changing source names or scores.
    merged: dict[str, ParsedDisease] = {}
    for item in results:
        existing = merged.get(item.disease_id)
        if existing is None:
            merged[item.disease_id] = item
            continue
        if not existing.related_taxa and item.related_taxa:
            existing.related_taxa = item.related_taxa
        if existing.source_score is None:
            existing.source_score = item.source_score
        if existing.source_level is None:
            existing.source_level = item.source_level
    descriptions = _document_disease_descriptions(
        document.text,
        disease_names=(item.name for item in merged.values()),
    )
    for item in merged.values():
        item.source_description = item.source_description or descriptions.get(
            item.name.casefold()
        )
    return list(merged.values())


def _disease_similarity_results(page_text: str) -> list[ParsedDisease]:
    """Read generic source disease/symptom score triplets from one page.

    The parser is anchored to a source section heading and understands the
    semantic score/level layout. It does not contain a disease catalogue.
    """

    if not re.search(r"疾病(?:相似|风险)评估", page_text):
        return []
    lines = [
        _normalized_source_text(line)
        for line in page_text.splitlines()
        if _normalized_source_text(line)
    ]
    results: list[ParsedDisease] = []
    for index in range(len(lines) - 2):
        name, raw_score, raw_level = lines[index : index + 3]
        if name.strip("：: ") in {
            "指标范围",
            "疾病相似评估",
            "疾病风险评估",
            "风险为",
            "风险指数",
            "风险评分",
            "风险等级",
        }:
            continue
        # A pipe row already has a complete table meaning. Treating it as a
        # name and searching the next rows' numbers shifts every patient result.
        if (re.search(r'[|｜。；;，,:：]|低风险|中风险|高风险|如果|根据|检测|您|建议', name)
                or not re.fullmatch(r'-?\d+(?:\.\d+)?%?', raw_score)
                or not re.fullmatch(r'低风险|中低风险|中风险|中高风险|高风险|低相似|中相似|高相似|注意|需关注', raw_level)):
            continue
        score = _source_risk_score(raw_score)
        level = _source_risk_level(raw_level)
        normalized_name = _normalize_source_disease_name(name)
        if (
            not score
            or not level
            or not normalized_name
            or not _looks_like_disease_or_health_problem(normalized_name)
        ):
            continue
        results.append(
            ParsedDisease(
                disease_id=stable_id("disease", normalized_name),
                name=normalized_name,
                source_score=score,
                source_level=level,
            )
        )
    return results


def _document_disease_descriptions(
    text: str,
    *,
    disease_names: Iterable[str],
) -> dict[str, str]:
    """Extract provider-authored disease-card prose without inventing facts.

    Some PDF overview tables contain image placeholders instead of readable
    scores.  Their later disease cards still contain a general description.
    Preserve at most the first two sentences as source provenance so the row
    is not visually blank, while leaving scores, levels and independent
    evidence untouched.
    """

    descriptions: dict[str, str] = {}
    for name in dict.fromkeys(disease_names):
        heading = re.search(
            rf"\{{\s*{re.escape(name)}\s*\}}\s*",
            text,
        )
        if heading is None:
            continue
        tail = text[heading.end() :]
        next_heading = re.search(r"\n\s*\{[^{}\n]{2,60}\}\s*\n", tail)
        block = tail[: next_heading.start()] if next_heading else tail
        block = re.sub(
            r"(?im)^\s*(?:16S\s*)?INTESTINAL MICROBIAL GENE TEST REPORT\s*$|"
            r"^\s*16S\s*肠道微生物基因检测报告\s*$|"
            r"^\s*=+\s*PAGE\s+\d+\s*=+\s*$",
            " ",
            block,
        )
        cleaned = _clean(block)
        if not cleaned:
            continue
        # PDF line wrapping inserts spaces inside Chinese sentences.  Remove
        # only CJK-adjacent wrapping spaces; preserve ordinary English words.
        cleaned = re.sub(
            r"(?<=[\u3400-\u9fff，。；：、（）])\s+"
            r"(?=[\u3400-\u9fff，。；：、（）])",
            "",
            cleaned,
        )
        sentences = [
            sentence.strip()
            for sentence in re.findall(r"[^。！？!?]+[。！？!?]?", cleaned)
            if sentence.strip()
        ]
        description = "".join(sentences[:2]).strip()
        if description and _source_description_names_disease(description, name):
            descriptions[name.casefold()] = description[:600]
    return descriptions


def _source_description_names_disease(description: str, disease_name: str) -> bool:
    """Prevent an adjacent table heading from becoming another disease's prose."""

    aliases = {
        "II型糖尿病": ("糖尿病", "2型糖尿病", "Ⅱ型糖尿病"),
        "肾脏疾病": ("肾病", "肾炎", "肾脏"),
        "非酒精性脂肪肝": ("非酒精性脂肪性肝病", "NAFLD"),
        "帕金森": ("帕金森病",),
        "酒精肝": ("酒精性肝病",),
    }.get(disease_name, ())
    target_terms = (disease_name, *aliases)
    opening = description[:240].casefold()
    return any(term.casefold() in opening for term in target_terms)


def _explicit_report_date(document: ExtractedDocument) -> str | None:
    """Read one unambiguous, calendar-valid labelled report date, not sampling.

    Native PDFs can contain CJK compatibility glyphs; images use the same
    rule over shared OCR text/tables. Conflicting headers are not resolved by
    taking the first page, the latest date, or the current date.
    """
    surfaces = [document.text, *(" | ".join(row) for table in document.tables for row in table.rows)]
    text = unicodedata.normalize('NFKC', '\n'.join(surfaces))
    pattern = (
        r'(?<![\w])(?:报告(?:日期|时间|生成(?:日期|时间)?)|出具日期'
        r'|Report\s+date|Date\s+reported|Date\s+of\s+report)\s*[:：|]?\s*'
        r'(\d{4})\s*[-_/年]\s*(\d{1,2})\s*[-_/月]\s*(\d{1,2})(?!\d)日?'
    )
    dates = {}
    for match in re.finditer(pattern, text, re.I):
        try:
            identity = date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            return None
        # Validate/compare dates canonically, but keep the printed spelling.
        dates.setdefault(identity, text[match.start(1):match.end()].strip())
    return next(iter(dates.values())) if len(dates) == 1 else None


def _document_sample(document: ExtractedDocument) -> SampleInfo:
    text = document.text
    values: dict[str, str] = {}
    sample_keys = {
        "检测编号",
        "样本编号",
        "样本ID",
        "样本ID号",
        "样本类型",
        "检测项目",
        "送检项目",
        "采样日期",
        "检测日期",
        "送检日期",
        "报告日期",
        "检测平台",
        "测序平台",
    }
    for table in document.tables:
        flat = " ".join(" ".join(row) for row in table.rows[:8])
        if not any(
            marker in flat
            for marker in ("受检者信息", "患者信息", "样本信息", "质控信息")
        ):
            continue
        for row in table.rows:
            if len(row) < 2:
                continue
            key = re.sub(r"\s+", "", row[0]).strip("：:")
            value = _clean(row[1])
            if key in sample_keys and value:
                values[key] = value

    sample = SampleInfo(
        sample_id=(
            values.get("检测编号")
            or values.get("样本编号")
            or values.get("样本ID")
            or values.get("样本ID号")
            or _labeled_value(
                text,
                ("检测编号", "样本编号", "样本ID", "样本ID号"),
                r"[A-Za-z0-9._-]{3,64}",
            )
        ),
        sample_type=values.get("样本类型")
        or _labeled_value(text, ("样本类型",), r"[^\n\r|]{1,40}"),
        test_item=values.get("检测项目")
        or values.get("送检项目")
        or _labeled_value(text, ("检测项目", "送检项目"), r"[^\n\r|]{1,80}"),
        sample_date=values.get("采样日期")
        or values.get("检测日期")
        or values.get("送检日期")
        or _labeled_value(
            text,
            ("采样日期", "检测日期", "送检日期"),
            r"\d{4}[-_/\u5e74]\d{1,2}[-_/\u6708]\d{1,2}日?",
        ),
        report_date=_explicit_report_date(document)
        or _labeled_value(
            text,
            ("体检日期",),
            r"\d{4}[-_/\u5e74]\d{1,2}[-_/\u6708]\d{1,2}日?",
        ),
        sequencing_platform=values.get("检测平台")
        or values.get("测序平台")
        or _labeled_value(
            text,
            ("检测平台", "测序平台"),
            r"[A-Za-z0-9._/-]{2,80}",
        ),
    )
    native = original_sample_metadata(document)
    values = sample.model_dump(by_alias=True)
    values.update({key: value for key, value in native.items() if value is not None or key == 'sampleDate'})
    return SampleInfo.model_validate(values)


def _document_subject(document: ExtractedDocument) -> SubjectInfo:
    """Read only explicitly labelled subject facts from an uploaded report."""

    text = document.text
    labelled: dict[str, str] = {}
    labels = {
        "姓名": "name",
        "患者姓名": "name",
        "受检者姓名": "name",
        "性别": "sex",
        "年龄": "age",
    }
    for table in document.tables:
        for row in table.rows:
            for index, raw_cell in enumerate(row):
                label = re.sub(r"[\s：:]", "", _normalized_source_text(raw_cell))
                field = labels.get(label)
                if not field or field in labelled:
                    continue
                value = next(
                    (
                        _normalized_source_text(candidate)
                        for candidate in row[index + 1 :]
                        if _normalized_source_text(candidate)
                    ),
                    "",
                )
                if value:
                    labelled[field] = value

    name = labelled.get("name") or _labeled_value(
        text,
        ("姓名", "患者姓名", "受检者姓名"),
        r"[A-Za-z\u3400-\u9fff·]{2,40}",
    )
    name = _valid_subject_name(name)
    # OCR output often places sex and age on the same physical line, for
    # example ``性别：男 年龄：49岁``. Narrow value patterns prevent one
    # label from swallowing the next one.
    sex_text = _normalized_source_text(labelled.get("sex"))
    sex_match = re.search(r"(?:男|女|未知)", sex_text)
    sex = sex_match.group(0) if sex_match else _labeled_value(text, ("性别",), r"(?:男|女|未知)")
    age_source = _normalized_source_text(labelled.get("age"))
    age_match = re.search(r"\d{1,3}", age_source)
    age_text = age_match.group(0) if age_match else _labeled_value(text, ("年龄",), r"\d{1,3}(?=\s*岁?)")
    age = int(age_text) if age_text and 0 <= int(age_text) <= 150 else None
    return SubjectInfo(name=name, age=age, sex=sex)


def _normalized_source_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(unicodedata.normalize("NFKC", str(value)).split())


def _valid_subject_name(value: Any) -> str | None:
    name = _normalized_source_text(value).strip("：:|")
    if not name or not re.fullmatch(r"[A-Za-z\u3400-\u9fff·]{2,40}", name):
        return None
    if name in {
        "信息",
        "基本信息",
        "受检者信息",
        "患者信息",
        "检测人",
        "审核人",
        "报告人",
        "样本的肠道菌群特征",
    }:
        return None
    return name


_NO_DETECTION_FLAG = object()


def _explicit_detection_flag(row):
    for key in ('isDetected', 'detected', 'detectionFlag', 'detectionStatus', 'detection_status'):
        if key in row:
            return row[key]
    return _NO_DETECTION_FLAG


def detection_status(value: Any, *, status: str | None = None,
                     explicit_detection: Any = _NO_DETECTION_FLAG) -> str:
    # A rounded abundance of zero is not a negative detection assay. Only an
    # explicit named detection field or literal source wording supplies that
    # conclusion; the original measured value is retained independently.
    if explicit_detection is not _NO_DETECTION_FLAG:
        flag = str(explicit_detection).strip().casefold()
        if explicit_detection is False or type(explicit_detection) in (int, float) and explicit_detection == 0 or flag in {'0', 'false', 'not_detected', 'notdetected', 'not detected', 'negative', '未检出', '阴性'}:
            return 'not_detected'
        if explicit_detection is True or type(explicit_detection) in (int, float) and explicit_detection == 1 or flag in {'1', 'true', 'detected', 'positive', '检出', '阳性'}:
            return 'detected'
    status_text = _clean(status) or ""
    if any(marker in status_text for marker in ("未检出", "阴性")) or status_text.casefold() in {'not_detected', 'notdetected', 'not detected', 'negative', 'nd'}:
        return "not_detected"
    value_text = _clean(value) or ""
    if not value_text or value_text in {"-", "—", "--", "未报告"}:
        return "unknown"
    if value_text.upper() in {"ND", "N.D."} or value_text.casefold() in {'未检出', '阴性', 'negative', 'not detected', 'not_detected'}:
        return "not_detected"
    if value_text.startswith("<"):
        return "trace"
    if any(marker in status_text for marker in ("检出", "阳性")):
        return "detected"
    match = re.search(r"-?\d+(?:\.\d+)?", value_text)
    if match:
        return "detected" if float(match.group(0)) > 0 else "unknown"
    return "unknown"


def _detect_enterotype(page: pymupdf.Page, page_text: str) -> str | None:
    if match := _ENTEROTYPE_TEXT.search(page_text):
        return _normalize_enterotype(match.group(1))
    if "肠型分析" not in page_text or "检测项目" not in page_text:
        return None
    width, height = page.rect.width, page.rect.height
    x_start, x_end = width * 0.265, width * 0.89
    y_start, y_end = height * 0.202, height * 0.252
    segment = (x_end - x_start) / 3
    scores: list[int] = []
    for index in range(3):
        clip = pymupdf.Rect(
            x_start + segment * index,
            y_start,
            x_start + segment * (index + 1),
            y_end,
        )
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=clip, alpha=False)
        blue = 0
        for offset in range(0, len(pixmap.samples), pixmap.n):
            red, green, blue_value = pixmap.samples[offset : offset + 3]
            if blue_value - red > 25 and green - red > 15 and blue_value > 130:
                blue += 1
        scores.append(blue)
    winner = max(range(3), key=scores.__getitem__)
    alternatives = [score for index, score in enumerate(scores) if index != winner]
    if scores[winner] >= 300 and scores[winner] >= max(alternatives, default=0) * 3:
        return ("肠型I", "肠型II", "肠型III")[winner]
    return None


def _normalize_enterotype(value: str) -> str:
    normalized = value.strip().upper()
    return {
        "I": "肠型I",
        "Ⅰ": "肠型I",
        "1": "肠型I",
        "II": "肠型II",
        "Ⅱ": "肠型II",
        "2": "肠型II",
        "III": "肠型III",
        "Ⅲ": "肠型III",
        "3": "肠型III",
        "BACTEROIDES": "肠型I",
        "PREVOTELLA": "肠型II",
        "RUMINOCOCCUS": "肠型III",
    }.get(normalized, value)


def _provider_summary_counts(microbe_data: dict[str, Any]) -> dict[str, int]:
    keys = {
        "Probiotics_num": "beneficial",
        "Pathogenic_num": "pathogenic",
        "Conditioned_Pathogen_num": "opportunistic",
    }
    counts: dict[str, int] = {}
    for row in _rows(microbe_data.get("Summary_spp")):
        category = keys.get(_clean(row.get("latinName")) or "")
        value = _numeric_text(row.get("value"))
        if category and value is not None:
            counts[category] = int(float(value))
    return counts


def _canonical_metric(row: dict[str, Any]) -> SourceMetric:
    return SourceMetric(
        code=_clean(row.get("code")) or stable_id("metric", str(row.get("name"))),
        name=_clean(row.get("name")) or "未命名指标",
        value=_clean(row.get("value")),
        unit=_clean(row.get("unit")),
        reference_range=_clean(row.get("referenceRange")),
        status=_clean(row.get("status")),
    )


def _subject(request: ReportRequest) -> SubjectInfo:
    return SubjectInfo(name=request.name, age=request.age, sex=request.sex)


def _provider_aggregate_status(disease_data: dict[str, Any], key: str) -> str | None:
    label = _PROVIDER_PANEL_NAMES[key]
    text = _clean(disease_data.get(label)) or ""
    for status in ("重度缺乏", "中度缺乏", "轻度缺乏", "偏高", "超标", "偏低", "正常"):
        if status in text:
            return status
    return None


def _provider_panel_total(value: Any) -> str | None:
    text = _clean(value) or ""
    match = re.search(r"总(?:检出)?丰度\s*([<>]?\d+(?:\.\d+)?)", text)
    return match.group(1) if match else None


def _numeric_text(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    text = _clean(value)
    if not text or not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return None
    return text


def _category_from_text(value: str) -> str | None:
    compact = value.replace(" ", "")
    # Prefer section headings.  Introductory paragraphs may list several panel
    # types and must not make the last word in the paragraph win.
    heading_aliases = {
        "潜在致病真菌": "fungi_pathogenic",
        "条件致病菌": "opportunistic",
        "有害菌": "opportunistic",
        "致病菌": "pathogenic",
        "有益菌": "beneficial",
        "其他健康相关菌": "other",
        "其他菌": "other",
    }
    standalone_hits: list[tuple[int, str]] = []
    for match in re.finditer(
        r"(?:^|\n)\s*(?:\d+(?:\.\d+)*[.\u3001]?\s*)?"
        r"(潜在致病真菌|条件致病菌|有害菌|(?<!条件)(?<!潜在)致病菌|"
        r"有益菌|其他健康相关菌|其他菌)\s*(?=\n|$)",
        value,
    ):
        standalone_hits.append((match.start(), heading_aliases[match.group(1)]))
    if standalone_hits:
        return max(standalone_hits, key=lambda item: item[0])[1]

    heading_patterns = (
        (r"核心菌属(?:构成)?表?", "other"),
        (r"其它重要菌属(?:构成)?表?", "other"),
        (r"其他重要菌属(?:构成)?表?", "other"),
        (r"益生菌(?:种)?", "beneficial"),
        (r"有害菌属(?:构成)?表?", "opportunistic"),
        (r"病原菌属(?:构成)?表?", "pathogenic"),
        (r"部分和健康相关的其他菌种检测结果", "other"),
        (r"其他健康相关菌(?:检测|检出)?结果", "other"),
        (r"其他菌(?:检测|检出)?结果", "other"),
        (r"潜在致病真菌(?:检测|检出)?结果", "fungi_pathogenic"),
        (r"条件致病菌(?:检测|检出)?结果", "opportunistic"),
        (r"有害菌(?:检测|检出)?结果", "opportunistic"),
        (
            r"(?<!条件)(?<!潜在)致病菌(?:检测|检出)?结果",
            "pathogenic",
        ),
        (r"有益菌(?:检测|检出)?结果", "beneficial"),
    )
    heading_hits: list[tuple[int, str]] = []
    for pattern, category in heading_patterns:
        heading_hits.extend(
            (match.start(), category) for match in re.finditer(pattern, compact)
        )
    if heading_hits:
        return max(heading_hits, key=lambda item: item[0])[1]

    generic_hits = []
    generic_text = compact
    for marker, category in (
        ("潜在致病真菌", "fungi_pathogenic"),
        ("条件致病菌", "opportunistic"),
    ):
        if marker in generic_text:
            generic_hits.append(category)
            generic_text = generic_text.replace(marker, "")
    for marker, category in (
        ("有害菌", "opportunistic"),
        ("致病菌", "pathogenic"),
        ("有益菌", "beneficial"),
    ):
        if marker in generic_text:
            generic_hits.append(category)
    if len(set(generic_hits)) == 1:
        return generic_hits[0]
    return None


def _category_from_aggregate_row(row: list[str]) -> str | None:
    first = row[0].strip() if row else ""
    return {
        "有益菌": "beneficial",
        "有害菌": "opportunistic",
        "条件致病菌": "opportunistic",
        "致病菌": "pathogenic",
        "潜在致病真菌": "fungi_pathogenic",
        "其他健康相关菌": "other",
        "其他菌": "other",
    }.get(first)


def _taxon_section_ended(value: str) -> bool:
    compact = value.replace(" ", "")
    return any(
        marker in compact
        for marker in (
            "营养物质代谢能力",
            "短链脂肪酸合成",
            "维生素合成能力",
            "疾病风险预测",
            "疾病相关风险评估",
            "门级水平结果总览",
            "纲级水平结果总览",
            "目级水平结果总览",
            "科级水平结果总览",
            "属级水平结果总览",
            "疾病正、负相关菌检出结果",
            "疾病风险评估",
            "慢性疾病风险评估",
            "肠道菌群与疾病相关风险评估",
            "肠道微生物检测结果分析",
            "肠道微生物整体健康状况",
            "肠道微生物健康指数",
            "肠道微生物定植抗力",
            "厚壁菌门/拟杆菌门",
            "过敏风险评估",
            "肠道抗炎能力",
            "肠道免疫力",
            "问题相关性",
            "相关菌",
        )
    )


def _disease_risk_context_active(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    starts = [
        compact.rfind(marker)
        for marker in (
            "肠道菌群与疾病相关风险评估",
            "慢性疾病风险评估",
            "疾病相关风险评估",
            "疾病风险评估",
        )
    ]
    start = max(starts, default=-1)
    if start < 0:
        return False
    stop = max(
        (
            compact.rfind(marker)
            for marker in (
                "肠道微生物检测结果分析",
                "ANALYSISOFINTESTINALMICROBIOTA",
                "健康建议",
                "参考文献",
            )
        ),
        default=-1,
    )
    return start > stop


def _disease_risk_text_blocks(text: str) -> list[str]:
    starts = list(
        re.finditer(
            r"(?:肠道菌群与)?(?:慢性)?疾病(?:相关)?风险评估",
            text,
        )
    )
    blocks: list[str] = []
    for match in starts:
        tail = text[match.end() :]
        stop = re.search(
            r"肠道微生物检测结果分析|"
            r"ANALYSIS\s*OF\s*INTESTINAL\s*MICROBIOTA|"
            r"参考文献",
            tail,
            flags=re.IGNORECASE,
        )
        blocks.append(tail[: stop.start() if stop else min(len(tail), 12000)])
    return blocks


def _taxon_value(row: list[str]) -> str | None:
    for cell in reversed(row[1:]):
        normalized = cell.strip().replace("%", "")
        if normalized in {"-", "—", "--"}:
            return None
        if re.fullmatch(r"<?\d+(?:\.\d+)?", normalized):
            return normalized
    return None


def _taxon_status(row: list[str]) -> str | None:
    for cell in reversed(row[1:]):
        compact = cell.replace(" ", "")
        for status in _TABLE_RESULT_STATUSES:
            if compact == status:
                return status
    return None


def _visual_table_row_status(
    page: pymupdf.Page,
    bbox: tuple[float, float, float, float],
) -> str | None:
    y0, y1 = float(bbox[1]), float(bbox[3])
    clip = pymupdf.Rect(
        page.rect.width * 0.82,
        y0,
        page.rect.width * 0.93,
        y1,
    )
    compact = (
        (page.get_text("text", clip=clip) or "").replace(" ", "").replace("\n", "")
    )
    for status in _TABLE_RESULT_STATUSES:
        if status in compact:
            return status
    return None


def _split_taxon_name(
    value: str,
    *,
    known_genera: Iterable[str] = (),
) -> tuple[str | None, str | None]:
    normalized = " ".join(value.replace("#", "").replace("*", "").split())
    match = re.search(r"([A-Z][A-Za-z.-]+(?:\s+[a-z][A-Za-z.-]+)+)$", normalized)
    if not match:
        match = re.search(r"([A-Z][A-Za-z.-]{3,})$", normalized)
    if not match:
        return None, None
    latin = match.group(1)
    if " " not in latin:
        genus = next(
            (
                candidate
                for candidate in sorted(set(known_genera), key=len, reverse=True)
                if latin.startswith(candidate)
                and len(latin) - len(candidate) >= 3
                and latin[len(candidate) : len(candidate) + 1].islower()
            ),
            None,
        )
        if genus:
            latin = f"{genus} {latin[len(genus):]}"
    chinese = normalized[: match.start()].strip(" （(") or None
    return chinese, latin


def _is_explicit_abnormal(status: Any, flag: Any) -> bool:
    if flag is True:
        return True
    text = _clean(status) or ""
    return any(
        marker in text
        for marker in (
            "偏高",
            "升高",
            "丰度高",
            "超标",
            "偏低",
            "降低",
            "丰度低",
            "缺乏",
            "异常",
            "高风险",
        )
    )


def _microbiome_panel_indexes(header: list[str]) -> dict[str, int | None] | None:
    if not _is_microbiome_panel_header(header):
        return None
    return {
        "name": _header_index_any(header, "名称", "菌名", "微生物名称"),
        "latin": _header_index_any(header, "拉丁学名", "学名"),
        "chinese": _header_index_any(header, "中文名称", "中文名"),
        "value": _header_index_any(header, "检测结果", "相对丰度", "丰度", "检测值"),
        "range": _header_index_any(header, "参考范围", "正常范围", "参考区间"),
        "status": _header_index_any(header, "结果评价", "是否异常", "状态", "评估"),
    }


def _is_microbiome_panel_header(row: list[str]) -> bool:
    has_separate_names = (
        _header_index_any(row, "拉丁学名", "学名") is not None
        and _header_index_any(row, "中文名称", "中文名") is not None
    )
    has_combined_name = _header_index_any(row, "名称", "菌名", "微生物名称") is not None
    has_value = _header_index_any(row, "检测结果", "相对丰度", "丰度", "检测值") is not None
    return has_value and (has_separate_names or has_combined_name)


def _microbiome_panel_row(
    row: list[str],
    indexes: dict[str, int | None],
    *,
    known_genera: Iterable[str] = (),
) -> tuple[str | None, str, str, str | None, str | None] | None:
    """Parse a taxon row by column meaning, including flattened PDF rows."""

    latin = _cell(row, indexes.get("latin"))
    chinese = _normalize_source_chinese_name(_cell(row, indexes.get("chinese")))
    combined_name = _cell(row, indexes.get("name"))
    raw_value = _cell(row, indexes.get("value"))
    reference_range = _clean(_cell(row, indexes.get("range")))
    status = _clean(_cell(row, indexes.get("status")))

    if not latin and combined_name and raw_value:
        chinese, latin = _split_taxon_name(
            combined_name,
            known_genera=known_genera,
        )

    # Native PDF extraction sometimes flattens all cells into the first
    # column. Recover only the source-printed name, abundance and interval;
    # trailing population percentiles are deliberately ignored.
    if (not latin or not raw_value) and combined_name:
        flattened = _flattened_taxon_row(
            combined_name,
            known_genera=known_genera,
        )
        if flattened is not None:
            chinese, latin, raw_value, printed_range = flattened
            reference_range = reference_range or printed_range

    if not latin or not _looks_like_latin_taxon(latin) or not raw_value:
        return None
    value, arrow_status = _source_value_and_arrow_status(raw_value)
    if value is None:
        return None
    status = status or arrow_status or _status_from_reference(value, reference_range)
    return chinese, latin, value, reference_range, status


def _flattened_taxon_row(
    value: str,
    *,
    known_genera: Iterable[str] = (),
) -> tuple[str | None, str, str, str | None] | None:
    normalized = _normalized_source_text(value)
    match = re.match(
        r"^(?P<name>.+?)\s+"
        r"(?P<value>ND|N\.D\.|<?-?\d+(?:\.\d+)?)(?P<arrow>[↑↓▲▼\uf175\uf176]?)%?\s+"
        r"(?P<range><?-?\d+(?:\.\d+)?(?:\s*[-~—–至]\s*<?-?\d+(?:\.\d+)?)?)"
        r"(?:\s|$)",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    chinese, latin = _split_taxon_name(
        match.group("name"),
        known_genera=known_genera,
    )
    if not latin:
        return None
    raw_value = f"{match.group('value')}{match.group('arrow')}"
    return chinese, latin, raw_value, match.group("range")


def _status_from_reference(value: str, reference_range: str | None) -> str | None:
    if not reference_range:
        return None
    # Taxon abundance intervals are non-negative. A hyphen is the interval
    # separator, not the sign of the upper bound.
    numbers = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", reference_range)]
    if not numbers:
        return None
    lower = numbers[0]
    upper = numbers[-1] if len(numbers) > 1 else numbers[0]
    if value.upper().replace(".", "") == "ND":
        return "缺乏" if lower > 0 else None
    numeric = float(value.lstrip("<"))
    if numeric < lower:
        return "缺乏"
    if numeric > upper:
        return "超标"
    return None


def _header_index(row: list[str], expected: str) -> int | None:
    compact_expected = re.sub(r"\s+", "", expected)
    for index, cell in enumerate(row):
        compact_cell = re.sub(r"\s+", "", cell)
        if compact_expected in compact_cell:
            return index
    return None


def _header_index_any(row: list[str], *expected: str) -> int | None:
    for value in expected:
        index = _header_index(row, value)
        if index is not None:
            return index
    return None


def _source_risk_score(value: str) -> str | None:
    """Recover one explicit numeric score from a noisy OCR table cell.

    Brackets, isolated image markers and HTML line breaks are common layout
    artifacts. Multiple numeric values remain ambiguous and are rejected so
    the parser never invents or silently combines a source model score.
    """

    cleaned = re.sub(r"<br\s*/?>", " ", value or "", flags=re.IGNORECASE)
    numbers = re.findall(r"(?<!\d)-?\d+(?:\.\d+)?(?!\d)", cleaned)
    if len(numbers) == 1:
        return numbers[0]
    decimals = [number for number in numbers if "." in number]
    integers = [number for number in numbers if "." not in number]
    # Page numbers and OCR image labels are sometimes appended after a score
    # with a line break. Keep the one decimal score only when every other token
    # is an integer artifact; competing decimal values remain ambiguous.
    if len(decimals) == 1 and integers:
        return decimals[0]
    return None


def _source_risk_level(value: str) -> str | None:
    match = re.search(
        r"(低风险|中低风险|中风险|中高风险|高风险|低相似|中相似|高相似|注意|需关注)",
        value or "",
    )
    if not match:
        return None
    return {
        "低相似": "低风险",
        "中相似": "中风险",
        "高相似": "高风险",
        "注意": "中风险",
        "需关注": "中风险",
    }.get(match.group(1), match.group(1))


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index < 0 or index >= len(row):
        return ""
    return " ".join(row[index].split())


def _looks_like_latin_taxon(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"[A-Za-z][A-Za-z0-9._-]*(?:\s+[A-Za-z][A-Za-z0-9._-]*)*",
            " ".join(value.split()),
        )
    )


def _normalize_source_chinese_name(value: Any) -> str | None:
    normalized = _clean(value)
    if not normalized or normalized in {"-", "--", "—"}:
        return None
    return re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", normalized)


def _source_value_and_arrow_status(value: str) -> tuple[str | None, str | None]:
    compact = _normalized_source_text(value).replace(" ", "").replace("%", "")
    arrow_status = None
    if any(marker in compact for marker in ("↑", "▲", "\uf176")):
        arrow_status = "超标"
    elif any(marker in compact for marker in ("↓", "▼", "\uf175")):
        arrow_status = "缺乏"
    normalized = compact.translate(str.maketrans("", "", "↑↓▲▼\uf175\uf176"))
    if normalized.upper() in {"ND", "N.D."}:
        return "ND", arrow_status
    if not re.fullmatch(r"<?-?\d+(?:\.\d+)?", normalized):
        return None, arrow_status
    return normalized, arrow_status


def _document_pages(text: str) -> list[str]:
    pages = re.split(r"\n===== PAGE \d+ =====\n", text)
    return [page for page in pages if page.strip()]


def _normalize_source_disease_name(value: str) -> str | None:
    normalized = re.sub(r"\s+", "", value).strip("：:")
    if not normalized or len(normalized) > 60:
        return None
    aliases = {
        "Ⅱ型糖尿病": "II型糖尿病",
        "非酒精性脂肪性肝病": "非酒精性脂肪肝",
    }
    return aliases.get(normalized, normalized)


def _split_source_related_taxa(value: str) -> list[str]:
    normalized = re.sub(r"\s+", "", value)
    return [
        item.strip("、，,")
        for item in re.split(r"[、，,]", normalized)
        if item.strip("、，,")
    ]


def _is_plain_number(value: str) -> bool:
    return bool(re.fullmatch(r"-?\d+(?:\.\d+)?", value.strip()))


def _is_source_image_placeholder(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).strip("_")
    return bool(re.fullmatch(r"<[A-Za-z0-9_-]+(?:png)?>", normalized, re.IGNORECASE))


def _looks_like_disease_or_health_problem(value: str) -> bool:
    """Reject adjacent provider-template rows that are not health outcomes.

    Unresolved provider exports use the same placeholder syntax for disease
    scores and for later environmental/function panels.  Disease-like Chinese
    names have a clinical outcome marker; substance names such as ``重金属`` do
    not.  This semantic guard avoids a report-specific disease allow-list.
    """

    compact = re.sub(r"\s+", "", value)
    return any(
        marker in compact
        for marker in (
            "病",
            "症",
            "炎",
            "癌",
            "肝",
            "狼疮",
            "硬化",
            "综合征",
            "障碍",
            "风险",
            "便秘",
            "腹泻",
            "癫痫",
            "帕金森",
            "高血压",
            "高血脂",
            "高尿酸",
        )
    )


def _labeled_value(
    text: str,
    labels: Iterable[str],
    value_pattern: str,
) -> str | None:
    for label in labels:
        match = re.search(
            rf"{re.escape(label)}[ \t]*[:：]?[ \t]*(?P<value>{value_pattern})",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return _clean(match.group("value"))
    return None


def _clean_table(rows: list[list[Any]]) -> list[list[str]]:
    return [
        [" ".join(str(cell or "").replace("\n", " ").split()) for cell in row]
        for row in rows
        if any(str(cell or "").strip() for cell in row)
    ]


def _clean(value: Any) -> str | None:
    # Numeric zero is a measured/source-model result, not a missing value.
    text = " ".join(str(value if value is not None else "").split()).strip()
    return text or None


def _dict_rows(value: Any) -> list[dict[str, Any]]:
    return [item for item in value or [] if isinstance(item, dict)]


def _rows(value: Any) -> list[dict[str, Any]]:
    return _dict_rows(value)


def _dedupe(items: Iterable[Any], *, key: Any) -> list[Any]:
    result: list[Any] = []
    seen: set[Any] = set()
    for item in items:
        marker = key(item)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


def _merge_document_taxa(items: Iterable[ParsedTaxon]) -> list[ParsedTaxon]:
    """Deduplicate repeated tables without losing an explicit source flag."""
    merged: dict[str, ParsedTaxon] = {}
    for item in items:
        existing = merged.get(item.taxon_id)
        if existing is None:
            merged[item.taxon_id] = item
            continue
        if item.explicitly_abnormal and not existing.explicitly_abnormal:
            merged[item.taxon_id] = item
            continue
        if (
            item.explicitly_abnormal == existing.explicitly_abnormal
            and item.reference_range
            and not existing.reference_range
        ):
            merged[item.taxon_id] = item
    return list(merged.values())


_CANONICAL_CATEGORIES = {
    "beneficial",
    "pathogenic",
    "opportunistic",
    "fungi_pathogenic",
    "other",
}

_TABLE_RESULT_STATUSES = (
    "重度超标",
    "中度超标",
    "轻度超标",
    "重度缺乏",
    "中度缺乏",
    "轻度缺乏",
    "丰度高",
    "丰度低",
    "偏高",
    "偏低",
    "异常",
    "未检出",
    "检出",
    "正常",
    "优秀",
)

_PROVIDER_PANELS = {
    "Probiotics": "beneficial",
    "Pathogenic": "pathogenic",
    "Conditional": "opportunistic",
    "Fungi_Pathgen": "fungi_pathogenic",
}

_PROVIDER_PANEL_NAMES = {
    "Probiotics": "有益菌",
    "Pathogenic": "致病菌",
    "Conditional": "条件致病菌",
    "Fungi_Pathgen": "潜在致病真菌",
}

_PROVIDER_METRIC_CODES = {
    "GMWI2": "gmwi2",
    "Diversity_Index": "diversity",
    "Species_Num": "species_count",
    "Enterotype": "enterotype",
    "F/B": "fb_ratio",
    "B/E": "be_ratio",
    "PHI": "pathogenic_potential",
}

_PROVIDER_METRIC_NAMES = {
    "GMWI2": "肠道菌群健康指数",
    "Diversity_Index": "菌群多样性",
    "Species_Num": "肠道微生物种类",
    "Enterotype": "肠型",
    "F/B": "厚壁菌门/拟杆菌门",
    "B/E": "肠道微生物定植抗力",
    "PHI": "病原潜能指数",
}

_PROVIDER_NON_DISEASE_KEYS = {
    "age",
    "amount",
    "classRate",
    "humRate",
    "conclusion",
    "有益菌",
    "致病菌",
    "条件致病菌",
    "部分与健康相关的其他菌种",
    "有益真菌",
    "潜在致病真菌",
    "其他酵母菌",
    "病毒检测",
    "毒力基因",
    "肝癌",
    "肠道屏障功能",
    "肠道抗炎能力",
    "肠道免疫力",
    "免疫调节能力",
    "短链脂肪酸合成能力",
}


def _provider_function_metrics(
    microbe_data: dict[str, Any],
    disease_data: dict[str, Any],
) -> list[SourceMetric]:
    """Read provider function blocks without projecting them onto diseases."""

    definitions = (
        ("gut_barrier", "肠道屏障功能", ("肠道屏障", "barrier")),
        ("anti_inflammatory", "肠道抗炎能力", ("抗炎", "antiinflammatory")),
        ("gut_immunity", "肠道免疫力", ("肠道免疫", "免疫调节", "gutimmunity")),
        ("acetate", "乙酸合成能力", ("乙酸", "acetate")),
        ("propionate", "丙酸合成能力", ("丙酸", "propionate")),
        ("butyrate", "丁酸合成能力", ("丁酸", "butyrate")),
        ("scfa", "短链脂肪酸合成能力", ("短链脂肪酸", "scfa")),
        ("calprotectin", "粪钙卫蛋白", ("粪钙卫蛋白", "calprotectin")),
        ("zonulin", "Zonulin", ("zonulin", "连蛋白")),
        ("dao", "二胺氧化酶", ("二胺氧化酶", "dao")),
        ("d_lactate", "D-乳酸", ("d乳酸", "d-lactate")),
        (
            "endotoxin",
            "内毒素相关指标",
            ("lps", "lbp", "内毒素", "endotoxin"),
        ),
    )
    candidates: list[tuple[str, Any]] = list(disease_data.items())
    for section_name, raw_rows in microbe_data.items():
        if isinstance(raw_rows, list):
            for row in raw_rows:
                if isinstance(row, dict):
                    label = _clean(
                        row.get("chineseName")
                        or row.get("name")
                        or row.get("latinName")
                        or section_name
                    )
                    candidates.append((label or section_name, row))
        elif isinstance(raw_rows, dict):
            candidates.append((section_name, raw_rows))
    results: list[SourceMetric] = []
    seen = set()
    for label, raw in candidates:
        compact = re.sub(r"\W+", "", str(label)).casefold()
        definition = next(
            (
                (code, name)
                for code, name, aliases in definitions
                if any(
                    re.sub(r"\W+", "", alias).casefold() in compact for alias in aliases
                )
            ),
            None,
        )
        if definition is None or definition[0] in seen:
            continue
        if isinstance(raw, dict):
            value = _clean(raw.get("value") or raw.get("result") or raw.get("score"))
            status = _clean(
                raw.get("status") or raw.get("level") or raw.get("resultStatus")
            )
            reference_range = _clean(raw.get("referenceRange"))
        else:
            value = _clean(raw)
            status = value if value and not re.search(r"\d", value) else None
            reference_range = None
        if not value and not status:
            continue
        code, name = definition
        results.append(
            SourceMetric(
                code=code,
                name=name,
                value=value,
                status=status,
                reference_range=reference_range,
            )
        )
        seen.add(code)
    return results


_ENTEROTYPE_TEXT = re.compile(
    r"肠型\s*[:：]?\s*(?:肠型\s*)?(III|II|I|Ⅲ|Ⅱ|Ⅰ|3|2|1)(?!\s*/)",
    flags=re.IGNORECASE,
)

_TABLE_METRIC_DEFINITIONS = (
    ("gmhi", "肠道微生物健康指数", ("肠道微生物健康指数", "肠道菌群健康指数"), None),
    ("diversity", "菌群多样性", ("菌群多样性",), None),
    (
        "species_count",
        "肠道微生物种类",
        ("肠道菌群种类", "肠道微生物种类", "菌种数"),
        "种",
    ),
    ("be_ratio", "肠道微生物定植抗力", ("B/E比值", "肠道微生物定植抗力"), None),
    ("fb_ratio", "厚壁菌门/拟杆菌门", ("F/B比值", "肠道菌群热量代谢"), None),
    ("pathogenic_potential", "肠道菌群致病潜力", ("肠道菌群致病潜力",), None),
    ("dietary_fiber_need", "肠道膳食纤维需求", ("肠道膳食纤维需求",), None),
    ("biofilm_capacity", "肠道菌群生物膜合成能力", ("肠道菌群生物膜合成能力",), None),
    (
        "oxidative_stress_tolerance",
        "肠道菌群氧化胁迫耐受",
        ("肠道菌群氧化胁迫耐受",),
        None,
    ),
    ("formate", "甲酸合成能力", ("甲酸",), None),
    ("acetate", "乙酸合成能力", ("乙酸",), None),
    ("propionate", "丙酸合成能力", ("丙酸",), None),
    ("butyrate", "丁酸合成能力", ("丁酸",), None),
    ("valerate", "戊酸合成能力", ("戊酸",), None),
)

_DOCUMENT_METRICS = (
    (
        "gmhi",
        "肠道微生物健康指数",
        (
            r"肠道微生物健康指数(?:（GMWI2）)?(?:评估)?\s*(?:[|｜:：]\s*)?(-?\d+(?:\.\d+)?)",
        ),
        None,
    ),
    (
        "diversity",
        "菌群多样性",
        (
            r"(?:肠道)?菌群多样性(?:评估)?\s*(?:[|｜:：]\s*)?(\d+(?:\.\d+)?)",
            r"肠道微生物多样性\s*(?:[|｜:：]\s*)?(\d+(?:\.\d+)?)",
        ),
        None,
    ),
    (
        "species_count",
        "肠道微生物种类",
        (
            r"肠道(?:微生物|菌群)种类(?:评估)?\s*(?:[|｜:：]\s*)?(\d+)",
            r"菌种数\s*[:：|]?\s*(\d+)",
        ),
        "种",
    ),
    (
        "fb_ratio",
        "厚壁菌门/拟杆菌门",
        (r"F/B(?:比值)?\s*(?:[|｜:：]\s*)?(\d+(?:\.\d+)?)",),
        None,
    ),
    (
        "be_ratio",
        "肠道微生物定植抗力",
        (
            r"B/E(?:比值)?\s*(?:[|｜:：]\s*)?(\d+(?:\.\d+)?)",
            r"肠道微生物定植抗力(?:评估)?\s*(?:[|｜:：]\s*)?(\d+(?:\.\d+)?)",
        ),
        None,
    ),
    (
        "pathogenic_potential",
        "肠道菌群致病潜力",
        (r"肠道菌群致病潜力(?:评估)?\s*(?:[|｜:：]\s*)?(\d+(?:\.\d+)?)",),
        None,
    ),
    (
        "dietary_fiber_need",
        "肠道膳食纤维需求",
        (r"肠道膳食纤维需求(?:评估)?\s*(?:[|｜:：]\s*)?(\d+(?:\.\d+)?)",),
        None,
    ),
    (
        "biofilm_capacity",
        "肠道菌群生物膜合成能力",
        (r"肠道菌群生物膜合成能力(?:评估)?\s*(?:[|｜:：]\s*)?(\d+(?:\.\d+)?)",),
        None,
    ),
    (
        "oxidative_stress_tolerance",
        "肠道菌群氧化胁迫耐受",
        (r"肠道菌群氧化胁迫耐受(?:评估)?\s*(?:[|｜:：]\s*)?(\d+(?:\.\d+)?)",),
        None,
    ),
)
