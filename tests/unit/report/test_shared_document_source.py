"""Report adapter integration with the real shared coordinators, isolated I/O."""

import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pymupdf
import pytest
from pydantic import ValidationError

from adapters.report.document_source import ReportArchiveLocator
from adapters.report.document_source import ReportDocumentSourceFactory
from adapters.report.document_source import SharedReportDocuments
from modules.analysis.application.documents import DocumentParsingError
from modules.analysis.application.documents import ProcessedHealthReport
from modules.analysis.application.output_profiles import DocumentOutputProfiles
from modules.analysis.application.output_profiles import DocumentSnapshot
from modules.analysis.application.service import HealthReportUpstreamError
from modules.report import parsing
from modules.report.document_source import extract_intrinsic_report_source
from modules.report.parsing import parse_request_source
from modules.report.pipeline import ReportPipeline
from modules.report.pipeline import merge_request_session_fallbacks
from modules.report.ports import DocumentServiceError
from modules.report.schemas import RemoteFileInput
from modules.report.schemas import ReportRequest
from tests.unit.services.analysis.test_document_provenance import report_text
from tests.unit.services.analysis.test_document_workflow import extended_flow
from tests.unit.services.analysis.test_document_workflow import install_graph
from tests.unit.services.analysis.test_document_workflow import lab


class ScopedTraceCollection:
    def __init__(self, documents):
        self.documents = documents

    def find(self, query):
        from copy import deepcopy

        from tests.unit.services.analysis.fakes import MemoryMongoCollection

        rows = []
        for row in self.documents.values():
            if all(
                MemoryMongoCollection.value(row, key) >= value['$gte']
                if isinstance(value, dict)
                else MemoryMongoCollection.value(row, key) == value
                for key, value in query.items()
            ):
                rows.append(deepcopy(row))

        class Cursor:
            def limit(self, count):
                self.count = count
                return self

            async def to_list(self, length):
                return rows[: min(length, self.count)]

        return Cursor()


def recovery_flow(*, version='analysis-health-report-v5'):
    flow = report_flow()
    # Explicit legacy metadata exercises the pre-v6 archive fallback contract.
    flow.interpreter.VERSION = version
    flow.client.result = {'success': True, 'text': '肠道总体评分\n您的肠道总评分为：\n58'}
    install_graph(flow, lambda text: '{"observations": [')
    flow.locator = ReportArchiveLocator(ScopedTraceCollection(flow.traces.documents), flow.index_collection)
    flow.factory = ReportDocumentSourceFactory(lambda: flow.service, flow.locator)
    return flow


@pytest.mark.asyncio
async def test_recovered_image_pages_combine_without_calling_shared_model_again():
    from modules.report.ports import ReportDocumentSource

    flow = recovery_flow()
    gateway = reader(flow)
    pages = ['肠道总体评分\n您的肠道总评分为：\n58', '2.肠道免疫力评估\n肠道免疫力 46']
    for i, text in enumerate(pages):
        snapshot = DocumentSnapshot(str(i), {}, text, f'{i}.png', 'png', str(i), b'')
        parsed = extract_intrinsic_report_source((snapshot,))
        from modules.report.schemas import ParsedMicrobiomeReport

        result = ReportDocumentSource(
            ParsedMicrobiomeReport.model_validate(parsed),
            text,
            f'{i}.png',
            'image',
            ({'fileIndex': i, 'processingStatus': 'fallback_source_only'},),
        )
        gateway.recovered[i] = snapshot, result
    flow.service.process_documents = AsyncMock(side_effect=AssertionError('no shared retry'))
    result = await gateway.combine([0, 1])
    assert {item.code for item in result.parsed.metrics} >= {'gut_overall_score', 'gut_immunity'}
    assert result.parsed.source_errors == ['documentGroup: SHARED_INTERPRETATION_FALLBACK']
    assert all(item['groupProcessingStatus'] == 'fallback_source_only' for item in result.references)


@pytest.mark.asyncio
async def test_failed_shared_model_recovers_verified_archive_without_new_ocr_or_success_flag():
    flow = recovery_flow()
    payload = ReportRequest(
        user_id='user-1',
        session_id='s-1',
        file_list=[RemoteFileInput(url='https://example.test/a.pdf', file_type='pdf')],
    )
    source = SimpleNamespace(fetch_file=AsyncMock(side_effect=AssertionError('no direct download')))
    result = await parse_request_source(payload, source_client=source, document_source=reader(flow))
    assert result.metrics[0].value == '58'
    assert result.source_errors == ['fileList[0]: SHARED_INTERPRETATION_FALLBACK']
    assert result.source_documents[0]['processingStatus'] == 'fallback_source_only'
    assert result.source_documents[0]['ingestionStatus'] == 'not_submitted'
    assert len(flow.client.submit_calls) == 1
    assert len(flow.graph.calls) == 1  # An unparseable reply is settled without a second call.
    assert all(
        row['status'] == 'completed' and row['processingStatus'] == 'unavailable'
        for row in flow.index_collection.documents.values()
    )
    assert result.source_documents[0]['errorType'] == 'DocumentInterpretationError'
    assert not flow.collection.documents  # No partially interpreted health_report is published.


@pytest.mark.asyncio
async def test_failed_document_reference_recovers_without_url_download_or_partial_json_read():
    flow = recovery_flow()
    first = await reader(flow).read(RemoteFileInput(url='https://example.test/a.pdf', file_type='pdf'), index=0)
    flow.downloader.download.side_effect = AssertionError('reference must not download')
    original = flow.service.read_document_payload
    calls = []

    async def read_archive(document_id, **kwargs):
        calls.append(kwargs['name'])
        return await original(document_id, **kwargs)

    flow.service.read_document_payload = read_archive
    result = await reader(flow, batch='new-batch').read(
        RemoteFileInput(document_id=first.references[0]['documentId']),
        index=0,
    )
    assert result.parsed.metrics[0].value == '58'
    assert set(calls) == {'original_file', 'docmind_result'}
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_failed_ocr_cannot_enter_structured_model_fallback():
    flow = recovery_flow()
    flow.client.result = {'success': False, 'text': '肠道总体评分\n您的肠道总评分为：\n58'}
    payload = ReportRequest(
        user_id='user-1',
        session_id='s-1',
        file_list=[RemoteFileInput(url='https://example.test/a.pdf', file_type='pdf')],
    )
    result = await parse_request_source(payload, source_client=SimpleNamespace(), document_source=reader(flow))
    assert result.source_type == 'pending'
    assert not result.metrics
    assert result.source_documents[0]['processingStatus'] == 'failed'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['configuration', 'authentication', 'database'])
async def test_service_or_persistence_failures_do_not_trigger_archive_fallback(kind):
    flow = recovery_flow()
    cause = DocumentParsingError('service unavailable')
    if kind == 'configuration':
        cause.content_failure = False
    elif kind == 'authentication':
        cause.__cause__ = RuntimeError('provider authentication failed')
        cause.__cause__.status_code = 401
    else:
        cause = RuntimeError('database save failed')
    error = HealthReportUpstreamError('processing failed')
    error.__cause__ = cause
    flow.service.process_document = AsyncMock(side_effect=error)
    flow.locator.locate = AsyncMock(side_effect=AssertionError('must not read archive'))
    with pytest.raises(HealthReportUpstreamError):
        await reader(flow).read(RemoteFileInput(url='https://example.test/a.pdf', file_type='pdf'), index=0)
    flow.locator.locate.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_shared_result_exposes_quality_without_claiming_complete():
    flow = report_flow()
    # A malformed model row leaves the schema-valid measurement available.
    flow.client.result = {'success': True, 'text': '尿酸 468 umol/L'}
    partial = lab()
    partial['observations'].append({'itemName': '未成形字段', 'resultValue': []})
    install_graph(flow, lambda text: partial)
    result = await reader(flow).read(RemoteFileInput(url='https://example.test/a.pdf', file_type='pdf'), index=0)
    assert result.parsed.supplementary_hospital_data
    assert result.references[0]['processingStatus'] == 'partial'
    assert result.references[0]['processing']['status'] == 'partial'
    assert result.parsed.source_errors == ['fileList[0]: SHARED_INTERPRETATION_PARTIAL']
    assert result.references[0]['ingestionStatus'] == 'not_submitted'
    assert result.references[0]['jobId'] is None
    assert result.references[0]['sourceRevision'] is None
    assert len(flow.jobs.jobs) == 1  # Only the verified base observations enter the profile queue.


def quality_outcome(index, status):
    return ProcessedHealthReport(
        report={
            'reportId': f'r-{index}',
            'userId': 'u',
            'subjectId': 'self',
            'processing': {'status': status},
            'observations': [{'itemName': '肠道总体评分', 'resultValue': '58'}],
        },
        text='肠道总体评分\n您的肠道总评分为：\n58' if index == 0 else '2.肠道免疫力评估\n肠道免疫力 46',
        document_id=f'd-{index}',
        trace_id=f't-{index}',
        ingestion_status='not_submitted',
        ingestion_job_id=None,
        reused=False,
        file_name=f'{index}.png',
        resolved_file_type='png',
        canonical_report_id=f'r-{index}',
        source_revision=None,
        output_profile='report_source',
        structured_data={
            'sourceName': f'{index}.png',
            'subject': {},
            'metrics': [{'code': 'gut_overall_score', 'name': '肠道总体评分', 'value': '58'}],
            # An extension declaring itself complete cannot erase a partial input.
            'processing': {'status': 'complete'},
        },
    )


@pytest.mark.asyncio
async def test_partial_pdf_table_access_is_scoped_and_checksum_checked(monkeypatch):
    content = b'archived-original'
    outcome = replace(
        quality_outcome(0, 'partial'),
        resolved_file_type='pdf',
        file_name='source.pdf',
        content_sha256=hashlib.sha256(content).hexdigest(),
    )
    processor = SimpleNamespace(read_document_payload=AsyncMock(return_value={'content': content}))
    gateway = SharedReportDocuments(processor, 'u', 's', 'r')
    document = parsing.ExtractedDocument('source.pdf', 'pdf', '', tables=[parsing.ExtractedTable(1, [['row']])])
    calls = []

    def extract(name, raw, *, ocr):
        calls.append((name, raw, ocr))
        return document

    monkeypatch.setattr(parsing, '_extract_pdf', extract)
    assert await gateway._original_tables(outcome) == (document,)
    processor.read_document_payload.assert_awaited_once_with(
        'd-0',
        user_id='u',
        subject_id='self',
        name='original_file',
    )
    assert calls == [('source.pdf', content, False)]
    processor.read_document_payload.return_value = {'content': b'corrupted'}
    assert await gateway._original_tables(outcome) == ()
    processor.read_document_payload.side_effect = PermissionError('not owned')
    assert await gateway._original_tables(outcome) == ()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_partial_image_reuses_ocr_tables_and_unavailable_never_recovers():
    processor = SimpleNamespace(read_document_payload=AsyncMock(side_effect=AssertionError('no archive request')))
    gateway = SharedReportDocuments(processor, 'u', 's', 'r')
    outcome = replace(quality_outcome(0, 'partial'), text='| 项目 | 结果 |\n|---|---|\n| 指标 | 1 |')
    documents = await gateway._original_tables(outcome)
    assert len(documents) == 1 and documents[0].tables
    assert await gateway._original_tables(quality_outcome(0, 'unavailable')) == ()
    processor.read_document_payload.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_partial_source_reaches_pipeline_partial_success():
    outcome = quality_outcome(0, 'partial')
    processor = SimpleNamespace(process_document=AsyncMock(return_value=outcome))
    gateway = SharedReportDocuments(processor, 'u', 's', 'r')
    pipeline = quality_pipeline(gateway)
    result = await pipeline.generate(
        ReportRequest(user_id='u', session_id='s', file_list=[RemoteFileInput(document_id='d-0')])
    )
    assert result.status == 'partial_success'
    assert 'DOCUMENT_SOURCE_INCOMPLETE' in result.partial_reasons


@pytest.mark.asyncio
async def test_complete_first_image_cannot_hide_partial_second_image_in_combined_output():
    outcomes = [quality_outcome(0, 'complete'), quality_outcome(1, 'partial')]
    processor = SimpleNamespace(
        process_document=AsyncMock(side_effect=lambda **kwargs: outcomes[kwargs['file_index']]),
        process_documents=AsyncMock(return_value=outcomes[0]),
    )
    gateway = SharedReportDocuments(processor, 'u', 's', 'r')
    request = ReportRequest(
        user_id='u', session_id='s', file_list=[RemoteFileInput(document_id=f'd-{index}') for index in range(2)]
    )
    parsed = await parse_request_source(request, source_client=SimpleNamespace(), document_source=gateway)
    assert parsed.source_errors == ['documentGroup: SHARED_INTERPRETATION_PARTIAL']
    assert [item['processingStatus'] for item in parsed.source_documents] == ['completed', 'partial']
    assert all(item['groupProcessingStatus'] == 'partial' for item in parsed.source_documents)
    assert processor.process_documents.await_args.kwargs['resume_incomplete'] is False
    result = await quality_pipeline(gateway).generate(request)
    assert result.status == 'partial_success'
    assert 'DOCUMENT_SOURCE_INCOMPLETE' in result.partial_reasons


@pytest.mark.asyncio
async def test_shared_read_then_combine_does_not_resume_partial_input_again():
    flow = report_flow()
    flow.downloader.download.side_effect = [b'\x89PNG\r\n\x1a\nfirst', b'\x89PNG\r\n\x1a\nsecond']
    partial = lab()
    partial['observations'].append({'itemName': '未成形字段', 'resultValue': []})
    install_graph(flow, lambda text: partial)
    gateway = reader(flow)
    flow.client.result = {'success': True, 'text': '肠道总体评分\n您的肠道总评分为：\n58\n' + report_text(lab())}
    await gateway.read(RemoteFileInput(url='https://example.test/a.png', file_type='image'), index=0)
    flow.client.result = {'success': True, 'text': '2.肠道免疫力评估\n肠道免疫力 46\n尿酸 468 umol/L'}
    await gateway.read(RemoteFileInput(url='https://example.test/b.png', file_type='image'), index=1)
    model_calls = len(flow.graph.calls)
    assert {row['interpretationRound'] for row in flow.index_collection.documents.values()} == {1}
    result = await gateway.combine([0, 1])
    assert result.parsed.source_errors == ['documentGroup: SHARED_INTERPRETATION_PARTIAL']
    assert len(flow.graph.calls) == model_calls
    assert len(flow.client.submit_calls) == 2
    assert {row['interpretationRound'] for row in flow.index_collection.documents.values()} == {1}


@pytest.mark.asyncio
@pytest.mark.parametrize('processing', [{'status': 'unavailable'}, {'status': 'unexpected'}, None])
async def test_unknown_or_unavailable_extension_quality_cannot_publish_unverified_source(processing):
    outcome = quality_outcome(0, 'complete')
    outcome.structured_data['processing'] = processing
    gateway = SharedReportDocuments(SimpleNamespace(process_document=AsyncMock(return_value=outcome)), 'u', 's', 'r')
    with pytest.raises(DocumentServiceError, match='unavailable'):
        await gateway.read(RemoteFileInput(document_id='d-0'), index=0)


def quality_pipeline(gateway):
    return ReportPipeline(
        source_client=SimpleNamespace(),
        evidence_client=SimpleNamespace(search=AsyncMock(return_value=[])),
        callback_client=SimpleNamespace(),
        session_loader=AsyncMock(return_value={}),
        narrative_client=None,
        document_source_factory=SimpleNamespace(create=lambda **kwargs: gateway),
        generation_timeout_seconds=2,
    )


@pytest.mark.asyncio
async def test_v6_unavailable_cannot_reintroduce_quarantined_values_through_raw_fallback(monkeypatch):
    extractor = AsyncMock(side_effect=AssertionError('v6 raw fallback is forbidden'))
    monkeypatch.setattr('adapters.report.document_source.extract_intrinsic_report_source', extractor)
    flow = recovery_flow(version='analysis-health-report-v6')
    request = ReportRequest(
        user_id='user-1',
        session_id='s-1',
        file_list=[RemoteFileInput(url='https://example.test/a.pdf', file_type='pdf')],
    )
    parsed = await parse_request_source(request, source_client=SimpleNamespace(), document_source=reader(flow))
    assert parsed.source_type == 'pending'
    assert not parsed.metrics and not parsed.supplementary_hospital_data
    assert parsed.source_documents[0]['processingStatus'] == 'failed'
    extractor.assert_not_called()


@pytest.mark.asyncio
async def test_mixed_legacy_fallback_and_partial_input_never_reparse_partial_raw_text():
    from modules.report.ports import ReportDocumentSource
    from modules.report.schemas import ParsedMicrobiomeReport

    processor = SimpleNamespace(read_document_payload=AsyncMock(side_effect=AssertionError('no raw merge')))
    gateway = SharedReportDocuments(processor, 'u', 's', 'r')
    snapshot = DocumentSnapshot('old', {}, 'legacy source', 'old.png', 'png', 'hash')
    parsed = ParsedMicrobiomeReport(source_type='pending', source_name='old.png', subject={})
    gateway.recovered[0] = snapshot, ReportDocumentSource(parsed, '', 'old.png', 'image', ())
    gateway.outcomes[1] = quality_outcome(1, 'partial')
    with pytest.raises(DocumentServiceError, match='quarantined'):
        await gateway.combine([0, 1])
    processor.read_document_payload.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmatched_probe_and_raw_summary_never_reach_parsed_or_narrative_context(monkeypatch):
    from modules.report.narrative import NarrativeError

    outcome = quality_outcome(0, 'partial')
    outcome.structured_data['metrics'].append({'code': 'probe', 'name': '隔离探针', 'value': '999'})
    outcome.structured_data['sourceSummary'] = '隔离探针999构成原文摘要'
    outcome.structured_data['enterotype'] = '未经确认肠型'
    gateway = SharedReportDocuments(SimpleNamespace(process_document=AsyncMock(return_value=outcome)), 'u', 's', 'r')
    source = await gateway.read(RemoteFileInput(document_id='d-0'), index=0)
    assert [item.value for item in source.parsed.metrics] == ['58']
    assert source.parsed.source_summary is None and source.parsed.enterotype is None
    captured = {}

    async def capture(_client, **context):
        captured.update(context)
        raise NarrativeError('offline output')

    monkeypatch.setattr('modules.report.pipeline.generate_narrative', capture)
    monkeypatch.setattr('modules.report.pipeline.supplement_common_examinations', AsyncMock(return_value=[]))
    pipeline = quality_pipeline(gateway)
    pipeline.narrative_client = object()
    result = await pipeline.generate(
        ReportRequest(user_id='u', session_id='s', file_list=[RemoteFileInput(document_id='d-0')])
    )
    assert captured
    assert '999' not in str(captured) and '隔离探针' not in str(captured)
    # The timestamp may legitimately contain the probe digits in its microseconds.
    body = result.model_dump_json(exclude={'generated_at'})
    assert '999' not in body and '隔离探针' not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'change', ['user_id', 'session_id', 'final_report_id', 'index', 'url', 'document_id', 'old', 'ambiguous']
)
async def test_archive_locator_rejects_unrelated_stale_or_ambiguous_attempts(change):
    from copy import deepcopy
    from datetime import datetime
    from datetime import timedelta
    from datetime import timezone

    flow = recovery_flow()
    source = RemoteFileInput(url='https://example.test/a.pdf', file_type='pdf')
    started = datetime.now(timezone.utc)
    await reader(flow).read(source, index=0)
    args = dict(user_id='user-1', session_id='s-1', final_report_id='rpt-test', source=source, index=0, started=started)
    if change in ('user_id', 'session_id', 'final_report_id'):
        args[change] = 'different'
    elif change == 'index':
        args['index'] = 1
    elif change == 'url':
        args['source'] = RemoteFileInput(url='https://example.test/other.pdf', file_type='pdf')
    elif change == 'document_id':
        args['source'] = RemoteFileInput(document_id='other')
    elif change == 'old':
        args['started'] = datetime.now(timezone.utc) + timedelta(seconds=1)
    else:
        trace = deepcopy(next(iter(flow.traces.documents.values())))
        trace['_id'] = 'duplicate'
        flow.traces.documents['duplicate'] = trace
    assert await flow.locator.locate(**args) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['bytes', 'ocr', 'empty_text', 'wrong_subject'])
async def test_recovery_refuses_invalid_or_inaccessible_archive(damage):
    flow = recovery_flow()
    original = flow.service.read_document_payload

    async def corrupt(document_id, **kwargs):
        if damage == 'wrong_subject':
            kwargs['subject_id'] = 'other'
        value = await original(document_id, **kwargs)
        if kwargs['name'] == 'original_file' and damage == 'bytes':
            value['content'] = b'changed'
        if kwargs['name'] == 'docmind_result':
            if damage == 'ocr':
                value['success'] = False
            if damage == 'empty_text':
                value['text'] = ''
        return value

    flow.service.read_document_payload = corrupt
    payload = ReportRequest(
        user_id='user-1',
        session_id='s-1',
        file_list=[RemoteFileInput(url='https://example.test/a.pdf', file_type='pdf')],
    )
    result = await parse_request_source(payload, source_client=SimpleNamespace(), document_source=reader(flow))
    assert result.source_type == 'pending'
    assert not result.metrics
    assert result.source_documents[0]['processingStatus'] == 'failed'


def report_flow():
    flow = extended_flow()
    registry = DocumentOutputProfiles()
    flow.service.output_profiles = registry
    flow.service._document_outputs.profiles = registry
    flow.factory = ReportDocumentSourceFactory(lambda: flow.service)
    return flow


def reader(flow, user='user-1', session='s-1', batch='rpt-test'):
    return flow.factory.create(user_id=user, session_id=session, final_report_id=batch)


def download_failure(cause):
    parsing = DocumentParsingError('文件下载失败或超时')
    parsing.__cause__ = cause
    upstream = HealthReportUpstreamError(str(parsing))
    upstream.__cause__ = parsing
    return upstream


@pytest.mark.asyncio
@pytest.mark.parametrize('file_type', ['pdf', 'image'])
async def test_transient_download_retry_preserves_output(file_type, monkeypatch):
    gateway = reader(report_flow())
    outcome = quality_outcome(0, 'complete')
    processor = AsyncMock(side_effect=[download_failure(TimeoutError()), outcome])
    gateway.processor.process_document = processor
    sleep = AsyncMock()
    monkeypatch.setattr('adapters.report.document_source.asyncio.sleep', sleep)
    source = RemoteFileInput(url='https://example.test/file', file_type=file_type)
    result = await gateway.read(source, index=0)
    assert processor.await_count == 2
    assert processor.await_args_list[0] == processor.await_args_list[1]
    sleep.assert_awaited_once_with(1.0)
    processor.side_effect = None
    processor.return_value = outcome
    assert result == await gateway.read(source, index=0)


@pytest.mark.asyncio
@pytest.mark.parametrize('status, attempts', [(429, 3), (500, 3), (503, 3), (401, 1), (403, 1), (404, 1)])
async def test_download_http_retry_is_bounded(status, attempts, monkeypatch):
    gateway = reader(report_flow())
    response = httpx.Response(status, request=httpx.Request('GET', 'https://example.test/file.pdf'))
    error = download_failure(httpx.HTTPStatusError('download status', request=response.request, response=response))
    gateway.processor.process_document = AsyncMock(side_effect=error)
    sleep = AsyncMock()
    monkeypatch.setattr('adapters.report.document_source.asyncio.sleep', sleep)
    with pytest.raises(HealthReportUpstreamError):
        await gateway.read(RemoteFileInput(url='https://example.test/file.pdf', file_type='pdf'), index=0)
    assert gateway.processor.process_document.await_count == attempts
    assert [call.args[0] for call in sleep.await_args_list] == ([1.0, 2.0] if attempts == 3 else [])


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [DocumentParsingError('invalid PDF'), PermissionError(), asyncio.CancelledError()])
async def test_non_download_failure_and_cancellation_are_not_retried(error):
    gateway = reader(report_flow())
    gateway.processor.process_document = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        await gateway.read(RemoteFileInput(url='https://example.test/file.pdf', file_type='pdf'), index=0)
    assert gateway.processor.process_document.await_count == 1


@pytest.mark.asyncio
async def test_document_reference_never_retries_download():
    gateway = reader(report_flow())
    gateway.processor.process_document = AsyncMock(side_effect=download_failure(TimeoutError()))
    with pytest.raises(HealthReportUpstreamError):
        await gateway.read(RemoteFileInput(document_id='document-1'), index=0)
    assert gateway.processor.process_document.await_count == 1


@pytest.mark.asyncio
async def test_retry_with_shared_coordinator_runs_ocr_only_once():
    flow = report_flow()
    flow.client.result = {'success': True, 'text': report_text(lab())}
    install_graph(flow, lambda text: lab())
    content = flow.downloader.download.return_value
    failure = DocumentParsingError('文件下载失败或超时')
    failure.__cause__ = TimeoutError()
    flow.downloader.download.side_effect = [failure, content]
    gateway = reader(flow)
    gateway = replace(gateway, policy=replace(gateway.policy, retry_backoff_seconds=()))
    result = await gateway.read(RemoteFileInput(url='https://example.test/one.pdf', file_type='pdf'), index=0)
    assert result.parsed.supplementary_hospital_data
    assert flow.downloader.download.await_count == 2
    assert len(flow.client.submit_calls) == 1


@pytest.mark.parametrize(
    'source',
    [
        {},
        {'documentId': ' '},
        {'documentId': 'd', 'url': 'https://example.test/a.pdf'},
        {'documentId': 'd', 'fileType': 'pdf'},
        {'url': 'https://example.test/a.pdf'},
    ],
)
def test_file_source_is_exactly_one_reference_or_typed_url(source):
    with pytest.raises(ValidationError):
        RemoteFileInput.model_validate(source)


@pytest.mark.asyncio
async def test_shared_report_url_then_reference_reuses_ocr_model_and_extension():
    flow = report_flow()
    flow.client.result = {'success': True, 'text': report_text(lab())}
    install_graph(flow, lambda text: lab())
    first_reader = reader(flow)
    first = await first_reader.read(RemoteFileInput(url='https://example.test/one.pdf', file_type='pdf'), index=0)
    assert first.parsed.supplementary_hospital_data
    first_calls = len(flow.graph.calls)
    first_submissions = len(flow.client.submit_calls)
    flow.downloader.download.side_effect = AssertionError('reference must not download')
    second = await reader(flow, session='s-2', batch='rpt-second').read(
        RemoteFileInput(document_id=first.references[0]['documentId']),
        index=2,
    )
    assert second.parsed == first.parsed
    assert second.references[0]['reused']
    assert second.references[0]['traceId'] != first.references[0]['traceId']
    assert len(flow.graph.calls) == first_calls
    assert len(flow.client.submit_calls) == first_submissions == 1
    traces = list(flow.traces.documents.values())
    assert any(t.get('source', {}).get('sourceId') == 'rpt-second' for t in traces)
    assert second.references[0]['ingestionStatus'] == 'queued'


@pytest.mark.asyncio
async def test_report_parse_uses_service_for_every_file_before_snapshot_selection():
    flow = report_flow()
    flow.client.result = {'success': True, 'text': report_text(lab())}
    install_graph(flow, lambda text: lab())
    payload = ReportRequest(
        user_id='user-1',
        session_id='s',
        name='请求姓名',
        file_list=[
            RemoteFileInput(url='https://example.test/a.pdf', file_type='pdf'),
            RemoteFileInput(url='https://example.test/b.pdf', file_type='pdf'),
        ],
    )
    sources = SimpleNamespace(fetch_file=AsyncMock(side_effect=AssertionError('no independent download')))
    parsed = await parse_request_source(payload, source_client=sources, document_source=reader(flow))
    assert parsed.source_type == 'pending'  # clinical report is not microbiome evidence
    assert parsed.subject.name == '请求姓名'
    assert len(parsed.source_documents) == 2
    assert parsed.source_documents[0]['documentId'] == parsed.source_documents[1]['documentId']
    assert parsed.supplementary_hospital_data[0].item_name == '尿酸'
    assert not parsed.source_errors
    assert 'sourceDocuments' not in parsed.model_dump(by_alias=True)


@pytest.mark.asyncio
async def test_inaccessible_reference_is_isolated_and_never_downloaded():
    flow = report_flow()
    sources = SimpleNamespace(fetch_file=AsyncMock(side_effect=AssertionError('no URL fallback')))
    payload = ReportRequest(user_id='user-1', session_id='s', file_list=[RemoteFileInput(document_id='missing')])
    parsed = await parse_request_source(payload, source_client=sources, document_source=reader(flow))
    assert parsed.source_errors
    assert parsed.source_documents[0]['processingStatus'] == 'failed'
    assert flow.downloader.download.await_count == 0


@pytest.mark.asyncio
async def test_other_users_cannot_read_archived_report():
    flow = report_flow()
    first = await reader(flow).read(RemoteFileInput(url='https://example.test/a.pdf', file_type='pdf'), index=0)
    with pytest.raises(Exception):
        await reader(flow, user='other-user').read(
            RemoteFileInput(document_id=first.references[0]['documentId']), index=0
        )
    assert flow.downloader.download.await_count == 1


def test_session_uses_document_references_but_explicit_files_win():
    request = ReportRequest(user_id='u', session_id='s')
    session = {
        'documents': [{'fileIndex': 0, 'documentId': 'archived'}],
        'healthData': {'fileList': [{'url': 'https://example.test/expired.pdf', 'fileType': 'pdf'}]},
    }
    merged = merge_request_session_fallbacks(request, session)
    assert merged.file_list[0].document_id == 'archived'
    assert merged.file_list[0].url is None
    explicit = request.model_copy(
        update={'file_list': [RemoteFileInput(url='https://example.test/new.pdf', file_type='pdf')]}
    )
    assert merge_request_session_fallbacks(explicit, session).file_list == explicit.file_list


def test_source_extractor_keeps_clinical_dates_and_ignores_ai_summary():
    snapshot = DocumentSnapshot(
        'd',
        {
            'reportType': 'checkup',
            'aiSummary': '推断患糖尿病',
            'sections': [
                lab('400', '2025-01-01'),
                lab('400', '2026-01-01'),
                {
                    'reportType': 'imaging',
                    'title': '超声',
                    'reportTime': None,
                    'expansion': {'findings': '胆囊内强回声', 'diagnosis': '胆囊结石'},
                },
            ],
        },
        '体检资料',
        'clinical.pdf',
        'pdf',
        'digest',
    )
    value = extract_intrinsic_report_source((snapshot,))
    rows = value['supplementaryHospitalData']
    assert {row['reportTime'] for row in rows if row['itemName'] == '尿酸'} == {'2025-01-01', '2026-01-01'}
    assert any(row.get('examResult') == '胆囊结石' for row in rows)
    assert '推断患糖尿病' not in str(value)
    assert value['diseaseResults'] == []


@pytest.mark.asyncio
async def test_bound_readers_keep_execution_identity_separate():
    flow = report_flow()
    source = RemoteFileInput(url='https://example.test/a.pdf', file_type='pdf')
    one, two = reader(flow, session='one', batch='batch-one'), reader(flow, session='two', batch='batch-two')
    await asyncio.gather(one.read(source, index=0), two.read(source, index=1))
    identities = {
        (
            t.get('source', {}).get('sessionId'),
            t.get('source', {}).get('sourceId'),
            t.get('source', {}).get('fileIndex'),
        )
        for t in flow.traces.documents.values()
    }
    assert ('one', 'batch-one', 0) in identities
    assert ('two', 'batch-two', 1) in identities


@pytest.mark.asyncio
async def test_shared_report_keeps_middle_of_long_document():
    flow = report_flow()
    text = '开头\n' + 'a' * 14000 + '\n' + report_text(lab()) + '\n' + 'b' * 14000
    flow.client.result = {'success': True, 'text': text}
    install_graph(
        flow,
        lambda chunk: (
            lab()
            if '本次检测结果：尿酸' in chunk
            else {'reportType': 'unknown', 'status': 'pending', 'observations': []}
        ),
    )
    result = await reader(flow).read(RemoteFileInput(url='https://example.test/long.pdf', file_type='pdf'), index=0)
    assert result.text == text
    assert any(row.item_name == '尿酸' for row in result.parsed.supplementary_hospital_data)
    assert len(flow.graph.calls) == 1
    assert flow.graph.calls[0]['input']['report_text'] == text
    assert len(flow.client.submit_calls) == 1


def test_image_group_extracts_continuation_without_personal_context():
    pages = [
        '肠道总体评分\n您的肠道总评分为：\n58\n中度失衡\n菌群多样性 4.96',
        '2.肠道免疫力评估\n肠道抗炎能力 50\n肠道免疫力 46',
        '4.短链脂肪酸合成能力\n乙酸 59\n丙酸 45\n丁酸 47',
    ]
    snapshots = tuple(
        DocumentSnapshot(str(i), {'reportType': 'microbiome'}, text, f'page-{i}.png', 'png', f'digest-{i}')
        for i, text in enumerate(pages)
    )
    output = extract_intrinsic_report_source(snapshots)
    values = {row['code']: row['value'] for row in output['metrics']}
    assert values['gut_overall_score'] == '58'
    assert values['gut_immunity'] == '46'
    assert values['acetate'] == '59'
    assert output['sourceChecksum'] is None
    with pytest.raises(ValueError):
        extract_intrinsic_report_source((snapshots[0], replace(snapshots[1], file_type='pdf')))


def test_shared_source_never_runs_local_ocr(monkeypatch):
    from modules.report import parsing

    calls = []

    def local(filename, content, *, ocr):
        calls.append(ocr)
        raise parsing.SourceParseError('scan-only')

    monkeypatch.setattr(parsing, '_extract_pdf', local)
    snapshot = DocumentSnapshot(
        'd', {'reportType': 'microbiome'}, '肠道总体评分\n您的肠道总评分为：\n58', 'scan.pdf', 'pdf', 'digest', b'pdf'
    )
    output = extract_intrinsic_report_source((snapshot,))
    assert calls == [False]
    assert any(row['value'] == '58' for row in output['metrics'])


def test_native_and_hybrid_pdf_keep_existing_metrics_and_add_shared_text():
    import hashlib

    from modules.report.parsing import parse_downloaded_document
    from modules.report.ports import DownloadedFile

    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((50, 50), 'F/B 1.25\nB/E 0.82')
        content = pdf.tobytes()
    request = ReportRequest(user_id='u', session_id='s')
    original = parse_downloaded_document(request, DownloadedFile('hybrid.pdf', 'application/pdf', content, 'pdf'))
    snapshot = DocumentSnapshot(
        'd',
        {'reportType': 'microbiome'},
        'F/B 1.25\nB/E 0.82\n肠道免疫力 46',
        'hybrid.pdf',
        'pdf',
        hashlib.sha256(content).hexdigest(),
        content,
    )
    converted = extract_intrinsic_report_source((snapshot,))
    rows = {row['code']: row for row in converted['metrics']}
    assert {item.code for item in original.metrics} == {'fb_ratio', 'be_ratio'}
    for item in original.metrics:
        assert rows[item.code] == item.model_dump(by_alias=True)
    assert rows['gut_immunity']['value'] == '46'


@pytest.mark.asyncio
async def test_document_references_are_persisted_and_loaded_separately_from_body():
    from adapters.report.artifact_store import MongoReportArtifactStore
    from modules.report.ports import ReportArtifactRecord

    batches = SimpleNamespace(create_index=AsyncMock(), update_one=AsyncMock())
    artifacts = SimpleNamespace(create_index=AsyncMock(), insert_one=AsyncMock(), find_one=AsyncMock())
    store = MongoReportArtifactStore(batches=batches, artifacts=artifacts)
    record = ReportArtifactRecord(
        'rpt-1',
        'u',
        's',
        'evaluate',
        'success',
        'ai_unverified',
        'hash',
        {'report_type': 'evaluate'},
        documents=({'fileIndex': 0, 'documentId': 'd'},),
    )
    assert await store.save_artifact(record) == 'saved'
    saved = artifacts.insert_one.call_args.args[0]
    assert saved['body'] == {'report_type': 'evaluate'}
    assert saved['documents'] == [{'fileIndex': 0, 'documentId': 'd'}]
    assert batches.update_one.call_args.args[1]['$set']['documents'] == saved['documents']
    artifacts.find_one.return_value = saved
    loaded = await store.get_artifact(user_id='u', final_report_id='rpt-1', report_type='evaluate')
    assert loaded.documents == record.documents
    assert artifacts.find_one.call_args.args[0]['userId'] == 'u'


@pytest.mark.asyncio
async def test_shared_configuration_failure_preserves_structured_request_sources():
    factory = ReportDocumentSourceFactory(lambda: (_ for _ in ()).throw(RuntimeError('configuration')))
    payload = ReportRequest(
        user_id='u',
        session_id='s',
        file_list=[RemoteFileInput(document_id='d')],
        microbiome_data={'overallMetrics': [{'code': 'diversity', 'name': '菌群多样性', 'value': '2.1'}]},
    )
    parsed = await parse_request_source(
        payload, source_client=object(), document_source=reader(SimpleNamespace(factory=factory))
    )
    assert parsed.metrics[0].value == '2.1'
    assert parsed.source_documents[0]['processingStatus'] == 'failed'
    assert parsed.source_errors


def test_report_document_metadata_stays_outside_body():
    from modules.report.artifacts import _ACTIVE_BATCH
    from modules.report.artifacts import bind_report_batch
    from modules.report.artifacts import build_artifact_record
    from modules.report.artifacts import note_report_documents
    from modules.report.artifacts import reset_report_batch

    token = bind_report_batch(store=None, final_report_id='rpt-1', user_id='u', session_id='s')
    try:
        note_report_documents([{'fileIndex': 0, 'documentId': 'd', 'ingestionStatus': 'queued'}])
        body = {'report_type': 'evaluate'}
        record = build_artifact_record(
            _ACTIVE_BATCH.get(), report_type='evaluate', body=body, generation_status='success'
        )
        assert record.documents[0]['documentId'] == 'd'
        assert record.body == body
        assert 'documents' not in record.body
    finally:
        reset_report_batch(token)
