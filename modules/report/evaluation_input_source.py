"""Choose one dedicated evaluate microbiome source before assessing it.

JSON and PDF need not describe the same person/report. They are alternatives,
never sources with which to fill each other's missing observations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from .schemas import RemoteFileInput
from .schemas import SourceSnapshotAudit


def recover_original_pdf(parsed, documents):
    """Apply source-owned recovery on an evaluate copy, across PDF inputs."""
    from .evaluation_pdf_amr import attach_original_pdf_amr
    from .evaluation_pdf_scores import attach_original_pdf_scores
    from .evaluation_template_pdf import parse_template_pdf_document

    for document in documents:
        if (
            getattr(document, 'source_type', None) != 'pdf'
            or document.filename != parsed.source_name
            or not parsed.source_checksum
            or document.metadata.get('content_sha256') != parsed.source_checksum
        ):
            continue
        native = parse_template_pdf_document(document)
        if native is not None:
            # A recognised original owns its complete C result projection.
            # Do not retain uncovered AI candidates (for example a vitamin
            # never measured by this method) as additional native results.
            native.source_errors = list(dict.fromkeys([*parsed.source_errors, *native.source_errors]))
            native.source_documents = list(parsed.source_documents)
            native._evaluation_source_audit.append(
                dict(
                    kind='verified_original_pdf_projection',
                    sourceFile=document.filename,
                    sourceChecksum=parsed.source_checksum,
                    previousMetricCount=len(parsed.metrics),
                )
            )
            parsed = native
        parsed = attach_original_pdf_amr(attach_original_pdf_scores(parsed, document), document)
    return parsed


def _json_report(request, downloaded, template_number):
    from .document_source import EvaluationDiseaseEvidenceMetric
    from .evaluation_template_input import parse_template_json
    from .parsing import parse_canonical_data
    from .parsing import parse_test_data

    payload = downloaded.detail
    if isinstance(payload.get('microbiomeData'), dict):
        report = parse_canonical_data(request, payload['microbiomeData'], evaluation_mode=True)
    elif any(isinstance(payload.get(key), list) for key in ('overallMetrics', 'taxa', 'diseaseRisks')):
        try:
            report = parse_canonical_data(request, payload, evaluation_mode=True)
        except ValueError:
            # Empty compatibility arrays do not invalidate supported, measured
            # template modules in this very same JSON. Do not mix sources.
            return parse_template_json(
                request,
                payload,
                template_number=template_number,
                checksum=downloaded.detail_checksum,
                source_name='microbiomeReportData.json',
            )
    elif isinstance(payload.get('microbeData'), dict) and isinstance(payload.get('diseaseRisk'), dict):
        report = parse_test_data(request, downloaded, evaluation_mode=True)
    else:
        return parse_template_json(
            request,
            payload,
            template_number=template_number,
            checksum=downloaded.detail_checksum,
            source_name='microbiomeReportData.json',
        )
    original_name, original_checksum = report.source_name, report.source_checksum
    report.source_checksum = downloaded.detail_checksum
    report.source_name = 'microbiomeReportData.json'
    for card in report.evaluation_panel_summaries:
        if (
            isinstance(card, EvaluationDiseaseEvidenceMetric)
            and card.source_page == 0
            and (card.source_file, card.source_checksum) == (original_name, original_checksum)
        ):
            card.source_file = report.source_name
            card.source_checksum = report.source_checksum
    return report


async def _pdf_report(request, *, source_client, document_source, document_client):
    from .evaluation_template_pdf import parse_template_pdf_document
    from .parsing import _extract_pdf
    from .parsing import parse_downloaded_document
    from .parsing import parse_extracted_text

    spec = request.microbiome_report_data
    item = RemoteFileInput(url=spec.original_report_url, file_type='pdf')
    fixed_read = getattr(document_source, 'read_dedicated_evaluation_pdf', None)
    if callable(fixed_read):
        outcome = await fixed_read(item)
        parsed = outcome.parsed.model_copy(deep=True)
        parsed.source_documents = list(outcome.references)
        return recover_original_pdf(parsed, outcome.evaluation_original_documents)
    downloaded = None
    document = None
    # The verified, source-specific native reader avoids the generic directory
    # and adjacent-chart heuristics. Scanned/other layouts use the same existing
    # document service as ordinary PDF evaluate inputs.
    try:
        downloaded = await source_client.fetch_file(item)
        document = await asyncio.to_thread(_extract_pdf, downloaded.filename, downloaded.content, ocr=False)
        parsed = parse_template_pdf_document(document, request=request)
        if parsed is not None:
            return recover_original_pdf(parsed, (document,))
    except Exception:
        if document_source is None and downloaded is None:
            raise
    if document_source is not None:
        outcome = await document_source.read(item, index=len(request.file_list))
        parsed = outcome.parsed.model_copy(deep=True)
        parsed.source_documents = list(outcome.references)
        return recover_original_pdf(parsed, (document,) if document is not None else ())
    if downloaded is None:
        downloaded = await source_client.fetch_file(item)
    try:
        parsed = await asyncio.to_thread(parse_downloaded_document, request, downloaded)
        return recover_original_pdf(parsed, (document,) if document is not None else ())
    except Exception:
        if document_client is None:
            raise
        text = await document_client.extract_text(item, filename=downloaded.filename, content=downloaded.content)
        parsed = parse_extracted_text(
            request,
            filename=downloaded.filename,
            source_type='pdf',
            text=text,
            checksum=hashlib.sha256(downloaded.content).hexdigest(),
        )
        return recover_original_pdf(parsed, (document,) if document is not None else ())


async def parse_evaluation_report_source(request, *, source_client, document_client, document_source, legacy_parser):
    from .parsing import _merge_subjects
    from .parsing import _subject

    spec = request.microbiome_report_data
    errors = []
    selected = None
    from .evaluation_request_input import inline_microbiome_data
    from .ports import DownloadedTestData

    inline = inline_microbiome_data(request)
    if inline not in (None, {}, ''):
        try:
            if not isinstance(inline, dict):
                raise ValueError('inline dataInfo must be an object')
            raw = json.dumps(inline, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('inline dataInfo exceeds the JSON source budget')
            selected = _json_report(
                request, DownloadedTestData(inline, hashlib.sha256(raw).hexdigest()), spec.template_number
            )
        except Exception as exc:
            errors.append(f'microbiomeReportData.dataInfo: {type(exc).__name__}')
    if selected is None and spec.data_info_url:
        try:
            downloaded = await source_client.fetch_evaluation_json(spec.data_info_url)
            selected = _json_report(request, downloaded, spec.template_number)
        except Exception as exc:
            errors.append(f'microbiomeReportData.dataInfoUrl: {type(exc).__name__}')
    if selected is None and spec.original_report_url:
        try:
            selected = await _pdf_report(
                request, source_client=source_client, document_source=document_source, document_client=document_client
            )
            if selected.source_type == 'pending':
                errors.extend(selected.source_errors)
                selected = None
        except Exception as exc:
            errors.append(f'microbiomeReportData.originalReportUrl: {type(exc).__name__}')

    # A caller may repeat the dedicated PDF among uploaded attachments. A JSON
    # success must not download it or pull its metadata/values into the result.
    files = [
        item for item in request.file_list if not (spec.original_report_url and item.url == spec.original_report_url)
    ]
    clinical_request = request.model_copy(
        update={'microbiome_report_data': None, 'microbiome_data': None, 'test_data': [], 'file_list': files}
    )
    clinical = await legacy_parser(
        clinical_request,
        source_client=source_client,
        document_client=document_client,
        document_source=document_source,
        enforce_subject_identity=True,
    )
    if selected is None:
        # With no usable dedicated source, retain the original uploaded-PDF
        # selection rather than discarding a valid legacy result.
        clinical.source_errors = list(dict.fromkeys([*errors, *clinical.source_errors]))
        return clinical
    selected.subject = _merge_subjects(_subject(request), selected.subject)
    selected.source_errors = list(dict.fromkeys([*errors, *selected.source_errors, *clinical.source_errors]))
    selected.supplementary_hospital_data.extend(clinical.supplementary_hospital_data)
    selected._evaluation_clinical_history.extend(clinical._evaluation_clinical_history)
    selected._evaluation_source_audit.extend(clinical._evaluation_source_audit)
    selected.source_documents.extend(clinical.source_documents)
    # In particular, never copy clinical.sample, metrics, taxa, source terms or
    # history into the independently selected C source.
    if selected.source_type != 'pending':
        selected.source_history = [
            SourceSnapshotAudit(
                source_type=selected.source_type,
                source_name=selected.source_name,
                source_checksum=selected.source_checksum,
                sample=selected.sample,
                metrics=selected.metrics,
                taxon_count=len(selected.taxa),
                disease_count=len(selected.disease_results),
                selected=True,
            )
        ]
    return selected
