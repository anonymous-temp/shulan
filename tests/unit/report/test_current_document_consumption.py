"""The explicit Report consumer keeps current Document inputs and commands fixed."""

from dataclasses import asdict
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pymupdf
import pytest
from pymongo.errors import AutoReconnect

from adapters.report.artifact_store import MongoReportArtifactStore
from adapters.report.current_document_checkpoints import MAX_SUBMISSION_ATTEMPTS
from adapters.report.current_document_checkpoints import MongoReportDocumentCheckpoints
from adapters.report.current_documents import CurrentReportDocumentFactory
from adapters.report.current_documents import ReportDocumentSubject
from adapters.report.durable_store import MongoReportWorkflowStore
from modules.analysis.application.documents import DocumentBusyError
from modules.analysis.publication import PublicationConflictError
from modules.analysis.publication import PublicationReceipt
from modules.report import pipeline as report_pipeline
from modules.report.artifacts import bind_report_batch
from modules.report.artifacts import note_report_documents
from modules.report.artifacts import reset_report_batch
from modules.report.artifacts import save_report_artifact
from modules.report.document_dispatch import ReportDocumentFactoryDispatcher
from modules.report.durable import DurableReportService
from modules.report.durable_ports import ACTIVE_GENERATION
from modules.report.parsing import parse_request_source
from modules.report.ports import DocumentServiceError
from modules.report.schemas import MicrobiomeReportDataInput
from modules.report.schemas import RemoteFileInput
from modules.report.schemas import ReportRequest
from modules.report.schemas import SubjectInfo
from tests.unit._support.current_document import model_report
from tests.unit._support.current_document import report
from tests.unit._support.current_document_profile import settle
from tests.unit._support.current_report_projection import composition
from tests.unit._support.current_report_projection import page_bytes
from tests.unit._support.native_report_pdf import native_pdf
from tests.unit.report.durable_fakes import Clock
from tests.unit.report.durable_fakes import Collection


def receipt(source_id, *, state='received'):
    return {
        'status': 'published',
        'sourceId': source_id,
        'sourceSnapshotRef': 'fixed-source-snapshot',
        'sourceRevision': 1,
        'publicationRevision': 1,
        'publication': asdict(PublicationReceipt(source_id, 1, 'event-digest', state, 'durable-receipt')),
    }


@pytest.fixture(autouse=True)
def active_generation():
    token = ACTIVE_GENERATION.set(('batch', 'test-lease'))
    try:
        yield
    finally:
        ACTIVE_GENERATION.reset(token)


def gateway(flow, *, checkpoints=None, consumer=None, select_scope=None):
    select_scope = select_scope or AsyncMock(return_value=ReportDocumentSubject(flow.scope, flow.epoch, SubjectInfo()))
    consumer = consumer or SimpleNamespace(consume_prepared=AsyncMock())
    if checkpoints is None:
        clock, batches = Clock(), Collection()
        batches.documents['batch'] = {
            '_id': 'batch',
            'userId': flow.scope.user_id,
            'sessionId': 'session',
            'durableVersion': 1,
            'generationState': 'running',
            'leaseToken': 'test-lease',
            'leaseExpiresAt': clock.now + timedelta(seconds=120),
        }
        checkpoints = MongoReportDocumentCheckpoints(batches, clock=clock)
    factory = CurrentReportDocumentFactory(
        prepared=flow.prepared,
        report_projections=flow.readers.report_projections,
        consumer=consumer,
        select_scope=select_scope,
        checkpoints=checkpoints,
        material_reader=flow.readers.report_projections.materials,
    )
    return factory, consumer, select_scope


def accept_sources(consumer, references):
    source_by_document = {row['documentId']: row['canonicalReportId'] for row in references}

    async def deliver(*, prepared, **kwargs):
        return receipt(source_by_document[prepared.preparation_receipt.document_id])

    consumer.consume_prepared.side_effect = deliver


def attachments(kind='pdf'):
    return RemoteFileInput(url=f'https://example.test/report.{kind}', file_type='image' if kind == 'png' else kind)


@pytest.mark.asyncio
async def test_current_consumption_reuses_fixed_document_projection_and_contribution_after_lost_ack(monkeypatch):
    flow = await composition(monkeypatch, [model_report()])
    factory, consumer, select_scope = gateway(flow)
    document = factory.create(user_id=flow.scope.user_id, session_id='session', final_report_id='batch')
    data = await document.read(attachments(), index=0)
    projection_ref = data.references[0]['reportProjectionSnapshotRef']
    assert data.references[0]['ingestionStatus'] == 'not_submitted'
    consumer.consume_prepared.assert_not_called()
    flow.upstream.downloader.download.side_effect = AssertionError('replay cannot download the original again')

    consumer.consume_prepared.side_effect = [
        DocumentBusyError('the primary publication is still applying'),
        receipt(data.references[0]['canonicalReportId']),
    ]
    pending = await document.settle_attachments(data.references)
    assert pending[0]['ingestionStatus'] == 'unknown'
    assert pending[0]['ingestionErrorType'] == 'DocumentSubmissionPending'
    assert await factory.recover_document_submissions() == 0
    factory.checkpoints.clock.advance(30)
    assert await factory.recover_document_submissions() == 1
    settled = await document.settle_attachments(data.references)
    assert settled[0]['ingestionStatus'] == 'queued'
    assert settled[0]['jobId'] == 'durable-receipt'
    assert settled[0]['sourceRevision'] == 1
    assert settled[0]['reportId'] == settled[0]['canonicalReportId']
    assert settled[0]['processing'] == {'status': 'complete', 'retryable': False, 'warnings': []}
    assert settled[0]['submittedProjectionSnapshotRef'] == projection_ref
    assert 'ingestionErrorType' not in settled[0]
    first, second = consumer.consume_prepared.await_args_list
    assert first == second
    assert first.kwargs['scope'] == flow.scope
    assert first.kwargs['lifecycle_epoch'] == flow.epoch
    assert first.kwargs['report_projection_ref'] == projection_ref
    restarted_factory, _, restarted_scope = gateway(flow, checkpoints=factory.checkpoints)
    restarted = restarted_factory.create(user_id=flow.scope.user_id, session_id='session', final_report_id='batch')
    replay = await restarted.read(attachments(), index=0)
    restarted_scope.assert_not_awaited()
    assert replay == data
    select_scope.assert_awaited_once_with(flow.scope.user_id)

    with pytest.raises(DocumentServiceError, match='attachment changed'):
        await restarted.read(attachments('png'), index=0)


@pytest.mark.asyncio
@pytest.mark.parametrize('multiple', [True, False])
async def test_attachment_with_conflicting_ownership_is_queued_but_excluded_from_generation(monkeypatch, multiple):
    front = model_report()
    if multiple:
        child = report(name='测试乙', gender='男', age=None, subject='测试乙')
        child['observations'] = [{'itemName': 'AST', 'resultValue': '13', 'unit': 'U/L'}]
        front['sections'] = [child]
    flow = await composition(monkeypatch, [front, front])
    expected = SubjectInfo() if multiple else SubjectInfo(name='画像已确认的用户', sex='male')
    select_scope = AsyncMock(return_value=ReportDocumentSubject(flow.scope, flow.epoch, expected))
    if multiple:
        flow.upstream.client.result['text'] += '\n受检者：测试乙\n姓名：测试乙\n性别：男\nAST 13 U/L'
    factory, consumer, _ = gateway(flow, select_scope=select_scope)

    async def queue(*, prepared, **kwargs):
        archived = flow.upstream.index_collection.documents[prepared.preparation_receipt.document_id]
        return receipt(archived['canonicalReportId'])

    consumer.consume_prepared.side_effect = queue
    document = factory.create(user_id=flow.scope.user_id, session_id='session', final_report_id='batch')
    document.enable_native_evaluation()
    result = await document.read(attachments(), index=0)

    assert not result.parsed.metrics and not result.parsed.supplementary_hospital_data
    assert not result.evaluation_original_documents and not result.evaluation_clinical_rows and not result.text
    assert result.references[0]['evaluationDisposition'] == 'different_subject_excluded'
    consumer.consume_prepared.assert_awaited_once()
    assert consumer.consume_prepared.await_args.kwargs['report_projection_ref'] is None
    await document.settle_attachments(result.references)
    consumer.consume_prepared.assert_awaited_once()


@pytest.mark.asyncio
async def test_pipeline_consumes_document_projection_from_admitted_current_factory(monkeypatch):
    flow = await composition(monkeypatch, [model_report()])
    factory, consumer, _ = gateway(flow)
    default = SimpleNamespace(document_input_manifest=lambda: {'consumerRevision': 'legacy-synthetic'})
    dispatcher = ReportDocumentFactoryDispatcher(default=default, supported=(factory,))
    assert dispatcher.document_input_manifest() == {'consumerRevision': 'legacy-synthetic'}
    with dispatcher.bind_document_manifest(default.document_input_manifest()):
        assert not dispatcher.supports_dedicated_evaluation_pdf
    with dispatcher.bind_document_manifest(factory.document_input_manifest()):
        assert dispatcher.supports_dedicated_evaluation_pdf
    real_parser = parse_request_source

    async def parse(*args, **kwargs):
        parsed = await real_parser(*args, **kwargs)
        consumer.consume_prepared.return_value = receipt(parsed.source_documents[0]['canonicalReportId'])
        return parsed

    class Settled(RuntimeError):
        pass

    def note(rows):
        assert rows[0]['ingestionStatus'] == 'queued'
        assert rows[0]['extractionId']
        assert 'reportProjectionSnapshotRef' not in rows[0]
        assert 'submittedProjectionSnapshotRef' not in rows[0]
        raise Settled

    monkeypatch.setattr(report_pipeline, 'parse_request_source', parse)
    monkeypatch.setattr(report_pipeline, 'note_report_documents', note)
    pipeline = report_pipeline.ReportPipeline(
        source_client=object(),
        evidence_client=object(),
        callback_client=object(),
        session_loader=AsyncMock(return_value={}),
        narrative_client=None,
        document_source_factory=dispatcher,
    )
    token = bind_report_batch(store=None, final_report_id='batch', user_id=flow.scope.user_id, session_id='session')
    try:
        with dispatcher.bind_document_manifest(factory.document_input_manifest()), pytest.raises(Settled):
            await pipeline.generate(
                ReportRequest(user_id=flow.scope.user_id, session_id='session', file_list=[attachments()])
            )
    finally:
        reset_report_batch(token)
    consumer.consume_prepared.assert_awaited_once()


@pytest.mark.asyncio
async def test_pure_clinical_group_preserves_accepted_reference_through_report_parser(monkeypatch):
    flow = await composition(monkeypatch, [model_report(), model_report()], png=True)
    factory, consumer, _ = gateway(flow)
    document = factory.create(user_id=flow.scope.user_id, session_id='session', final_report_id='batch')
    second = RemoteFileInput(url='https://example.test/second.png', file_type='image')
    await document.read(attachments('png'), index=0)
    flow.upstream.downloader.download.return_value = page_bytes('Synthetic continuation', png=True)
    flow.upstream.client.result = {'success': True, 'text': '检测结果\nALT 12 U/L\nAST 13 U/L'}
    await document.read(second, index=1)
    original_project = document._project_source

    async def clinical_group(indexes):
        result = await original_project(indexes)
        if len(indexes) == 1:
            return result
        assert result.parsed.supplementary_hospital_data
        return replace(result, parsed=result.parsed.model_copy(update={'source_type': 'pending', 'metrics': []}))

    monkeypatch.setattr(document, '_project_source', clinical_group)
    parsed = await parse_request_source(
        ReportRequest(user_id=flow.scope.user_id, session_id='session', file_list=[attachments('png'), second]),
        source_client=object(),
        document_source=document,
    )
    assert parsed.supplementary_hospital_data
    accept_sources(consumer, parsed.source_documents)
    first, second = await document.settle_attachments(parsed.source_documents)
    assert first['reportProjectionSnapshotRef'] == second['reportProjectionSnapshotRef']
    assert [call.kwargs['report_projection_ref'] for call in consumer.consume_prepared.await_args_list] == [
        None,
        first['reportProjectionSnapshotRef'],
    ]


@pytest.mark.asyncio
async def test_continuous_group_contributes_once_using_its_fixed_projection(monkeypatch):
    flow = await composition(monkeypatch, [model_report(), model_report()], png=True)
    factory, consumer, _ = gateway(flow)
    document = factory.create(user_id=flow.scope.user_id, session_id='session', final_report_id='batch')
    front = await document.read(attachments('png'), index=0)
    flow.upstream.downloader.download.return_value = page_bytes('Synthetic continuation', png=True)
    flow.upstream.client.result = {'success': True, 'text': '检测结果\nALT 12 U/L\nAST 13 U/L'}
    await document.read(RemoteFileInput(url='https://example.test/second.png', file_type='image'), index=1)
    group = await document.combine([0, 1])
    fixed_ref = group.references[0]['reportProjectionSnapshotRef']
    assert fixed_ref != front.references[0]['reportProjectionSnapshotRef']
    assert group.references[1]['reportProjectionSnapshotRef'] == fixed_ref
    consumer.consume_prepared.assert_not_called()
    accept_sources(consumer, group.references)

    result = await document.settle_attachments(group.references)
    assert [row['submittedProjectionSnapshotRef'] for row in result] == [fixed_ref, fixed_ref]
    assert [row['groupIngestionStatus'] for row in result] == ['queued', 'queued']
    assert [row['ingestionStatus'] for row in result] == ['not_submitted', 'not_submitted']
    assert [row['groupProcessingStatus'] for row in result] == ['completed', 'completed']
    assert result[0]['extractionId'] == front.references[0]['extractionId']
    assert {row['groupExtractionId'] for row in result} == {fixed_ref}
    assert len({row['groupOutputTraceId'] for row in result}) == 1
    assert {row['groupCanonicalReportId'] for row in result} == {front.references[0]['canonicalReportId']}
    assert {row['groupSourceRevision'] for row in result} == {1}
    assert {row['groupJobId'] for row in result} == {'durable-receipt'}
    second_page, command = [item.kwargs for item in consumer.consume_prepared.await_args_list]
    assert second_page['report_projection_ref'] is None
    assert second_page['prepared'].preparation_receipt.document_id == group.references[1]['documentId']
    assert command['report_projection_ref'] == fixed_ref
    assert command['prepared'].preparation_receipt.document_id == front.references[0]['documentId']
    assert 'replace_report_contribution_id' not in command

    consumer.consume_prepared.reset_mock()
    wrong_batch_identity = SubjectInfo(name='different synthetic patient')
    skipped = await document.settle_attachments(group.references, expected_subject=wrong_batch_identity)
    assert {row['ingestionDisposition'] for row in skipped} == {'different_subject_excluded'}
    assert {row['ingestionStatus'] for row in skipped} == {'not_submitted'}
    consumer.consume_prepared.assert_awaited_once()
    independent = consumer.consume_prepared.await_args.kwargs
    assert independent['report_projection_ref'] is None
    assert independent['prepared'].preparation_receipt.document_id == front.references[0]['documentId']


@pytest.mark.asyncio
async def test_consumer_excludes_another_subject_from_report_but_delivers_analysis_to_profile(monkeypatch):
    flow = await composition(monkeypatch, [model_report()])
    factory, consumer, _ = gateway(flow)
    document = factory.create(user_id=flow.scope.user_id, session_id='session', final_report_id='batch')
    report = await document.read(attachments(), index=0)
    accept_sources(consumer, report.references)
    rejected = [dict(report.references[0], evaluationDisposition='different_subject_excluded')]
    assert await document.settle_attachments(rejected) == rejected
    consumer.consume_prepared.assert_awaited_once()
    assert consumer.consume_prepared.await_args.kwargs['report_projection_ref'] is None
    consumer.consume_prepared.reset_mock()
    denied = await document.settle_attachments(report.references, expected_subject=SubjectInfo(sex='male'))
    assert denied[0]['ingestionDisposition'] == 'different_subject_excluded'
    consumer.consume_prepared.assert_not_awaited()
    consumer.consume_prepared.side_effect = PublicationConflictError('the group requires an explicit replacement')
    refused = await document.settle_attachments(report.references)
    assert refused[0]['ingestionStatus'] == 'not_submitted'
    assert refused[0]['ingestionErrorType'] == 'PublicationConflictError'
    assert await document.settle_attachments(report.references) == refused
    consumer.consume_prepared.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'response, expected_state, has_receipt',
    [
        ('unchanged_with_receipt', 'queued', True),
        ('unchanged_without_receipt', 'not_submitted', False),
        ('different_source', 'unknown', False),
    ],
)
async def test_current_document_receipt_is_bound_to_the_fixed_source(
    monkeypatch, response, expected_state, has_receipt
):
    flow = await composition(monkeypatch, [model_report()])
    factory, consumer, _ = gateway(flow)
    document = factory.create(user_id=flow.scope.user_id, session_id='session', final_report_id='batch')
    report = await document.read(attachments(), index=0)
    source_id = report.references[0]['canonicalReportId']
    result = receipt('other-source' if response == 'different_source' else source_id)
    if response != 'different_source':
        result['status'] = 'unchanged'
    if response == 'unchanged_without_receipt':
        result['publication'] = None
    consumer.consume_prepared.return_value = result
    (applied,) = await document.settle_attachments(report.references)
    assert applied['ingestionStatus'] == expected_state
    assert (applied['jobId'] is not None) is has_receipt
    assert applied['sourceRevision'] == (1 if has_receipt else None)
    if response == 'different_source':
        assert applied['ingestionErrorType'] == 'DocumentReceiptInvalid'


@pytest.mark.asyncio
async def test_durable_report_recovery_replays_only_the_fixed_document_submission(monkeypatch):
    flow = await composition(monkeypatch, [model_report()])
    clock, batches, artifacts, deliveries = Clock(), Collection(), Collection(), Collection()
    workflow = MongoReportWorkflowStore(batches=batches, artifacts=artifacts, deliveries=deliveries, clock=clock)
    checkpoints = MongoReportDocumentCheckpoints(batches, clock=clock)
    results = []

    async def lost_ack(**command):
        results.append(await flow.runtime.consumption.consume_prepared(**command))
        raise OSError('worker lost an acknowledged Document response')

    consumer = SimpleNamespace(consume_prepared=AsyncMock(side_effect=lost_ack))
    factory, _, _ = gateway(flow, checkpoints=checkpoints, consumer=consumer)
    request = ReportRequest(user_id=flow.scope.user_id, session_id='session', file_list=[attachments()])
    batch = await workflow.admit(
        request.model_dump(mode='json', by_alias=True),
        'report-document',
        'https://callbacks.test/report',
    )
    job = await workflow.claim_generation('original-report-worker')
    assert batch['_id'] == job['_id']
    token = ACTIVE_GENERATION.set((job['_id'], job['leaseToken']))
    document = factory.create(user_id=flow.scope.user_id, session_id='session', final_report_id=job['_id'])
    try:
        report = await document.read(attachments(), index=0)
        (pending,) = await document.settle_attachments(report.references)
    finally:
        ACTIVE_GENERATION.reset(token)
    assert pending['ingestionStatus'] == 'unknown'
    assert results[0]['status'] == 'published'
    # Lost generation is sealed by the existing Report recovery. Its source
    # settlement can finish after the generation lease has expired.
    clock.advance(121)
    flow.upstream.downloader.download.side_effect = AssertionError('fixed commands do not download again')
    rerun = AsyncMock(side_effect=AssertionError('Document recovery cannot regenerate Report'))
    retry = SimpleNamespace(
        consume_prepared=AsyncMock(wraps=flow.runtime.consumption.consume_prepared),
    )
    restarted, _, restarted_scope = gateway(
        flow,
        checkpoints=MongoReportDocumentCheckpoints(batches, clock=clock),
        consumer=retry,
    )
    recreated = DurableReportService(
        store=workflow,
        artifact_store=MongoReportArtifactStore(batches=batches, artifacts=artifacts, workflow=workflow),
        pipeline=SimpleNamespace(document_source_factory=restarted, generate=rerun),
        callback_url='https://callbacks.test/report',
    )
    assert await recreated.recover_pending() == 2
    recovered = batches.documents[job['_id']]
    assert recovered['generationState'] == 'completed'
    (saved,) = recovered['documentSubmissions'].values()
    assert saved['attempts'] == 2
    assert saved['outcome']['publicationRevision'] == results[0]['publicationRevision']
    assert saved['outcome']['jobId'] == results[0]['publication']['receipt_ref']
    assert consumer.consume_prepared.await_args == retry.consume_prepared.await_args
    await settle(flow)
    rerun.assert_not_called()
    restarted_scope.assert_not_awaited()
    callback_ids = set(deliveries.documents)
    assert await recreated.recover_pending() == 0
    assert set(deliveries.documents) == callback_ids


@pytest.mark.asyncio
@pytest.mark.parametrize('dedicated', [False, True])
async def test_evaluate_reads_the_same_fixed_pdf_without_a_second_download(monkeypatch, dedicated):
    flow = await composition(monkeypatch, [model_report()])
    flow.upstream.downloader.download.return_value = native_pdf()
    factory, consumer, _ = gateway(flow)
    document = factory.create(user_id=flow.scope.user_id, session_id='session', final_report_id='batch')
    document.enable_native_evaluation()
    old_source = SimpleNamespace(fetch_file=AsyncMock(side_effect=AssertionError('unexpected legacy download')))
    downloads_before = flow.upstream.downloader.download.await_count
    from modules.report.parsing import _extract_pdf

    native = _extract_pdf
    extraction_calls = []

    def count_geometry(filename, content, *, ocr):
        extraction_calls.append((content, ocr))
        return native(filename, content, ocr=ocr)

    monkeypatch.setattr('modules.report.parsing._extract_pdf', count_geometry)
    original = attachments()
    request = ReportRequest(
        user_id=flow.scope.user_id,
        session_id='session',
        file_list=[original],
        microbiome_report_data=MicrobiomeReportDataInput(original_report_url=original.url) if dedicated else None,
    )
    parsed = await parse_request_source(
        request,
        source_client=old_source,
        document_source=document,
        enforce_subject_identity=True,
    )
    assert parsed.source_type == 'pdf'
    assert {metric.code: metric.value for metric in parsed.metrics} == {'diversity': '7.36', 'gmhi': '-0.02'}
    (reference,) = parsed.source_documents
    assert reference['reportProjectionSnapshotRef']
    assert extraction_calls == [(flow.upstream.downloader.download.return_value, False)] * 2
    assert len(flow.gateway.complete_requests) == len(flow.upstream.client.submit_calls) == 1
    assert flow.upstream.downloader.download.await_count == downloads_before + 1
    old_source.fetch_file.assert_not_awaited()
    consumer.consume_prepared.assert_not_awaited()


@pytest.mark.asyncio
async def test_evaluate_uses_usable_json_before_touching_the_dedicated_pdf(monkeypatch):
    from modules.report.ports import DownloadedTestData

    flow = await composition(monkeypatch, [model_report()])
    factory, consumer, select_scope = gateway(flow)
    document = factory.create(user_id=flow.scope.user_id, session_id='session', final_report_id='batch')
    payload = {'overallMetrics': [{'name': 'diversity', 'value': 7.36}], 'taxa': [], 'diseaseRisks': []}
    source = SimpleNamespace(fetch_evaluation_json=AsyncMock(return_value=DownloadedTestData(payload, 'json-hash')))
    original = attachments()
    request = ReportRequest(
        user_id=flow.scope.user_id,
        session_id='session',
        file_list=[original],
        microbiome_report_data=MicrobiomeReportDataInput(
            original_report_url=original.url,
            data_info_url='https://example.test/source.json',
        ),
    )
    parsed = await parse_request_source(
        request, source_client=source, document_source=document, enforce_subject_identity=True
    )
    assert parsed.source_checksum == 'json-hash'
    assert parsed.source_documents == []
    select_scope.assert_not_awaited()
    consumer.consume_prepared.assert_not_awaited()


@pytest.mark.asyncio
async def test_document_recovery_remains_bounded_when_every_worker_stops_after_claim(monkeypatch):
    flow = await composition(monkeypatch, [model_report()])
    factory, consumer, _ = gateway(flow)
    checkpoints = factory.checkpoints
    document = factory.create(user_id=flow.scope.user_id, session_id='session', final_report_id='batch')
    report = await document.read(attachments(), index=0)
    submit = factory._submit_claim

    async def stop_worker(claim):
        await checkpoints.start_submission(claim)
        raise RuntimeError('process interrupted before recording Document outcome')

    monkeypatch.setattr(factory, '_submit_claim', stop_worker)
    with pytest.raises(RuntimeError, match='process interrupted'):
        await document.settle_attachments(report.references)
    for _ in range(MAX_SUBMISSION_ATTEMPTS - 1):
        checkpoints.clock.advance(121)
        with pytest.raises(RuntimeError, match='process interrupted'):
            await factory.recover_document_submissions()
    monkeypatch.setattr(factory, '_submit_claim', submit)
    checkpoints.clock.advance(121)
    assert await factory.recover_document_submissions() == 1
    assert await factory.recover_document_submissions() == 0
    row = await checkpoints.batches.find_one({'_id': 'batch'})
    (saved,) = row['documentSubmissions'].values()
    assert saved['attempts'] == MAX_SUBMISSION_ATTEMPTS
    assert saved['outcome'] == {'ingestionStatus': 'unknown', 'ingestionErrorType': 'RetryExhausted'}
    consumer.consume_prepared.assert_not_awaited()


def _native_clinical_pdf():
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((52, 46), '肝功能检验报告', fontname='china-s', fontsize=12)
        xs, ys = (45, 151, 244, 339, 456, 552), (96, 128, 160)
        for x in xs:
            page.draw_line((x, ys[0]), (x, ys[-1]))
        for y in ys:
            page.draw_line((xs[0], y), (xs[-1], y))
        for index, row in enumerate(
            (
                ('检测项目', '检测结果', '单位', '参考范围', '状态'),
                ('谷丙转氨酶', '54', 'U/L', '7-40', '偏高'),
            )
        ):
            for x, text in zip(xs, row):
                page.insert_text((x + 5, ys[index] + 18), text, fontname='china-s', fontsize=11)
        return pdf.tobytes()


@pytest.mark.asyncio
@pytest.mark.parametrize('report_type', ['checkup', 'imaging', 'lab'])
async def test_evaluate_preserves_objective_original_clinical_cells_on_partial_round(monkeypatch, report_type):
    from modules.report.evaluation_source_recovery import evaluation_clinical_source_rows
    from modules.report.parsing import _extract_pdf

    model = model_report(incomplete=True)
    model['reportType'] = report_type
    flow = await composition(monkeypatch, [model, model])
    original = _native_clinical_pdf()
    flow.upstream.downloader.download.return_value = original
    factory, consumer, _ = gateway(flow)
    document = factory.create(user_id=flow.scope.user_id, session_id='session', final_report_id='batch')
    source = attachments()
    document.enable_native_evaluation()
    first = await document.read(source, index=0)
    assert first.references[0]['processingStatus'] == 'partial'
    legacy_pdf = _extract_pdf(first.filename, original, ocr=False)
    legacy_rows = evaluation_clinical_source_rows(
        legacy_pdf,
        filename=first.filename,
        checksum=first.references[0]['contentSha256'],
        report_type=report_type,
    )
    assert len(legacy_rows) == 1
    assert first.evaluation_clinical_rows == legacy_rows
    assert legacy_rows[0].test_result_value == '54'
    assert legacy_rows[0].reference_range == '7-40'
    assert legacy_rows[0].model_extra['nativeStatusCell'] == '偏高'
    assert legacy_rows[0].model_extra['sourcePage'] == 1
    request = ReportRequest(user_id=flow.scope.user_id, session_id='session', file_list=[source])
    parsed = await parse_request_source(
        request,
        source_client=object(),
        document_source=document,
        enforce_subject_identity=True,
    )
    assert any(
        row.test_result_value == '54' and row.model_extra.get('nativeStatusCell') == '偏高'
        for row in parsed.supplementary_hospital_data
    )
    assert flow.upstream.downloader.download.await_count == 1
    consumer.consume_prepared.assert_not_awaited()


def _native_template_pdf():
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        for y, text in (
            (40, '肠道微生态总评分'),
            (65, '高通量宏基因组测序'),
            (90, '肠道菌群健康基因检测'),
        ):
            page.insert_text((40, y), text, fontname='china-s', fontsize=12)
        xs, ys = (45, 255, 360, 540), (120, 150, 180)
        for x in xs:
            page.draw_line((x, ys[0]), (x, ys[-1]))
        for y in ys:
            page.draw_line((xs[0], y), (xs[-1], y))
        for index, row in enumerate((('项目', '检测结果', '参考范围'), ('肠道菌群多样性', '7.36', '5-8'))):
            for x, text in zip(xs, row):
                page.insert_text((x + 5, ys[index] + 18), text, fontname='china-s', fontsize=11)
        return pdf.tobytes()


@pytest.mark.asyncio
@pytest.mark.parametrize('preparation_fails', [False, True])
async def test_dedicated_native_fallback_keeps_only_the_owned_fixed_pdf(monkeypatch, preparation_fails):
    if not preparation_fails:
        monkeypatch.setattr(
            'modules.analysis.current_understanding._admission_quality',
            lambda interpretation: {'status': 'unavailable'},
        )
    flow = await composition(monkeypatch, [{}, {}] if preparation_fails else [model_report()])
    flow.upstream.downloader.download.return_value = _native_template_pdf()
    factory, consumer, _ = gateway(flow)
    document = factory.create(user_id=flow.scope.user_id, session_id='session', final_report_id='batch')
    old_source = SimpleNamespace(fetch_file=AsyncMock(side_effect=AssertionError('unexpected legacy download')))
    original = attachments()
    request = ReportRequest(
        user_id=flow.scope.user_id,
        session_id='session',
        file_list=[original],
        microbiome_report_data=MicrobiomeReportDataInput(original_report_url=original.url),
    )
    parsed = await parse_request_source(
        request,
        source_client=old_source,
        document_source=document,
        enforce_subject_identity=True,
    )
    assert parsed.source_type == 'pdf', parsed.source_errors
    assert any(metric.code == 'diversity' and metric.value == '7.36' for metric in parsed.metrics)
    (reference,) = parsed.source_documents
    assert reference['processingStatus'] == 'fallback_source_only'
    assert reference['sharedProcessingStatus'] == 'failed'
    assert reference['ingestionStatus'] == 'not_submitted'
    assert reference['errorType'] == (
        'DocumentInterpretationError' if preparation_fails else 'DocumentCompleteSourceUnavailable'
    )
    assert await document.settle_attachments(parsed.source_documents) == parsed.source_documents
    assert flow.upstream.downloader.download.await_count == 1
    if preparation_fails:
        consumer.consume_prepared.assert_not_awaited()
    else:
        consumer.consume_prepared.assert_awaited_once()
        assert consumer.consume_prepared.await_args.kwargs['report_projection_ref'] is None
    old_source.fetch_file.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('revision', ['report-understanding-current.v2', 'report-understanding-current.v3'])
async def test_current_report_manifest_registers_the_supplied_fixed_projection_revision(monkeypatch, revision):
    flow = await composition(monkeypatch, [model_report()])
    flow.readers.report_projections.interpretation_projection_revision = revision
    factory, _, _ = gateway(flow)
    assert factory.document_input_manifest()['interpretationProjectionRevision'] == revision


@pytest.mark.asyncio
@pytest.mark.parametrize('written', [True, False])
async def test_report_uses_native_results_when_document_queue_acknowledgement_is_lost(monkeypatch, written):
    flow = await composition(monkeypatch, [model_report()])
    flow.upstream.downloader.download.return_value = native_pdf()
    consumer = SimpleNamespace(consume_prepared=AsyncMock(wraps=flow.runtime.consumption.consume_prepared))
    factory, _, _ = gateway(flow, consumer=consumer)
    checkpoints = factory.checkpoints
    enqueue = checkpoints.queue_once

    async def interrupted_write(*args):
        if written:
            await enqueue(*args)
        raise AutoReconnect('test: queue acknowledgement unavailable')

    monkeypatch.setattr(checkpoints, 'queue_once', interrupted_write)
    original = attachments()
    request = ReportRequest(
        user_id=flow.scope.user_id,
        session_id='session',
        file_list=[original],
        microbiome_report_data=MicrobiomeReportDataInput(original_report_url=original.url),
    )
    pipeline = report_pipeline.ReportPipeline(
        source_client=object(),
        evidence_client=object(),
        callback_client=object(),
        session_loader=AsyncMock(return_value={}),
        narrative_client=None,
        clinical_report_enabled=True,
        document_source_factory=factory,
    )
    captured = []
    real_note = report_pipeline.note_report_documents

    def note(documents):
        real_note(documents)
        captured.append(documents)

    class ReportFactsBuilt(Exception):
        pass

    def verify_native_facts(parsed):
        assert {metric.code: metric.value for metric in parsed.metrics} == {'diversity': '7.36', 'gmhi': '-0.02'}
        raise ReportFactsBuilt

    monkeypatch.setattr(report_pipeline, 'note_report_documents', note)
    monkeypatch.setattr(report_pipeline, 'build_abnormal_microbes', verify_native_facts)
    token = bind_report_batch(store=None, final_report_id='batch', user_id=flow.scope.user_id, session_id='session')
    try:
        with pytest.raises(ReportFactsBuilt):
            await pipeline.generate(request)
    finally:
        reset_report_batch(token)
    ((reference,),) = captured
    assert reference['ingestionStatus'] == 'unknown'
    assert reference['jobId'] is None and reference['sourceRevision'] is None
    assert 'ingestionErrorType' not in reference
    consumer.consume_prepared.assert_not_awaited()
    restored, _, restored_scope = gateway(
        flow,
        checkpoints=MongoReportDocumentCheckpoints(checkpoints.batches, clock=checkpoints.clock),
        consumer=consumer,
    )
    assert await restored.recover_document_submissions() == int(written)
    assert await restored.recover_document_submissions() == 0
    assert consumer.consume_prepared.await_count == int(written)
    if written:
        assert consumer.consume_prepared.await_args.kwargs['report_projection_ref'] == reference['extractionId']
        await settle(flow)
    restored_scope.assert_not_awaited()
    assert flow.upstream.downloader.download.await_count == 1


@pytest.mark.asyncio
async def test_document_recovery_claims_only_its_admitted_manifest_in_shared_batches(monkeypatch):
    flow = await composition(monkeypatch, [model_report()])
    clock, batches = Clock(), Collection()
    workflow = MongoReportWorkflowStore(batches=batches, artifacts=Collection(), deliveries=Collection(), clock=clock)
    first, first_consumer, _ = gateway(flow, checkpoints=MongoReportDocumentCheckpoints(batches, clock=clock))
    projection = flow.readers.report_projections
    alternate = CurrentReportDocumentFactory(
        prepared=flow.prepared,
        report_projections=SimpleNamespace(interpretation_projection_revision='report-understanding-current.v3'),
        consumer=SimpleNamespace(consume_prepared=AsyncMock(side_effect=AssertionError('different revision'))),
        select_scope=AsyncMock(),
        checkpoints=MongoReportDocumentCheckpoints(batches, clock=clock),
        material_reader=projection.materials,
    )
    request = ReportRequest(user_id=flow.scope.user_id, session_id='session', file_list=[attachments()])
    expected = first.document_input_manifest()
    admitted = await workflow.admit(
        request.model_dump(mode='json', by_alias=True),
        'revision-v2',
        'https://callbacks.test/report',
        execution_manifest={'documentConsumer': expected},
    )
    job = await workflow.claim_generation('original-worker')
    token = ACTIVE_GENERATION.set((job['_id'], job['leaseToken']))
    try:
        mismatched = alternate.create(user_id=flow.scope.user_id, session_id='session', final_report_id=admitted['_id'])
        with pytest.raises(DocumentServiceError, match='implementation differs'):
            await mismatched.read(attachments(), index=0)
        document = first.create(user_id=flow.scope.user_id, session_id='session', final_report_id=admitted['_id'])
        report = await document.read(attachments(), index=0)
        first_consumer.consume_prepared.side_effect = [
            DocumentBusyError('fixed command awaiting processing'),
            receipt(report.references[0]['canonicalReportId']),
        ]
        await document.settle_attachments(report.references)
    finally:
        ACTIVE_GENERATION.reset(token)
    clock.advance(30)
    assert await alternate.recover_document_submissions() == 0
    alternate.consumer.consume_prepared.assert_not_awaited()
    assert await first.recover_document_submissions() == 1
    assert first_consumer.consume_prepared.await_count == 2
    assert first_consumer.consume_prepared.await_args_list[0] == first_consumer.consume_prepared.await_args_list[1]


@pytest.mark.asyncio
async def test_partial_attachment_group_keeps_public_quality_and_hidden_basic_receipt_on_report_read(monkeypatch):
    primary_model = model_report(incomplete=True)
    flow = await composition(monkeypatch, [primary_model, primary_model, model_report()], png=True)
    clock, batches, artifacts, deliveries = Clock(), Collection(), Collection(), Collection()
    workflow = MongoReportWorkflowStore(batches=batches, artifacts=artifacts, deliveries=deliveries, clock=clock)
    store = MongoReportArtifactStore(batches=batches, artifacts=artifacts, workflow=workflow)
    factory, _, _ = gateway(
        flow,
        checkpoints=MongoReportDocumentCheckpoints(batches, clock=clock),
        consumer=flow.runtime.consumption,
    )
    secondary = RemoteFileInput(url='https://example.test/second.png', file_type='image')
    request = ReportRequest(user_id=flow.scope.user_id, session_id='session', file_list=[attachments('png'), secondary])
    batch = await workflow.admit(
        request.model_dump(mode='json', by_alias=True),
        'report-partial-public',
        'https://callbacks.test/report',
        execution_manifest={'documentConsumer': factory.document_input_manifest()},
    )
    job = await workflow.claim_generation('worker')
    assert job['_id'] == batch['_id']
    lease = ACTIVE_GENERATION.set((job['_id'], job['leaseToken']))
    context = bind_report_batch(
        store=store, final_report_id=job['_id'], user_id=flow.scope.user_id, session_id='session'
    )
    document = factory.create(user_id=flow.scope.user_id, session_id='session', final_report_id=job['_id'])
    try:
        partial = await document.read(attachments('png'), index=0)
        flow.upstream.downloader.download.return_value = page_bytes('Synthetic continuation', png=True)
        flow.upstream.client.result = {'success': True, 'text': '检测结果\nALT 12 U/L\nAST 13 U/L'}
        completed = await document.read(secondary, index=1)
        group = await document.combine([0, 1])
        references = await document.settle_attachments(group.references)
        note_report_documents(document.public_attachment_references(references))
        assert (
            await save_report_artifact('evaluate', {'source': 'synthetic'}, generation_status='partial_success')
            == 'saved'
        )
    finally:
        reset_report_batch(context)
        ACTIVE_GENERATION.reset(lease)
    result = await store.get_artifact(user_id=flow.scope.user_id, final_report_id=job['_id'], report_type='evaluate')
    listing = await store.list_batches(user_id=flow.scope.user_id, session_id='session')
    assert list(result.documents) == listing['items'][0]['documents']
    front, next_page = result.documents
    assert front['processingStatus'] == 'partial'
    assert front['processing']['status'] == 'partial'
    assert isinstance(front['processing']['retryable'], bool)
    assert front['processing']['warnings']
    assert front['processing']['issueCount'] >= 1
    assert front['extractionId'] == partial.references[0]['extractionId']
    assert next_page['processingStatus'] == 'completed'
    assert next_page['processing'] == {'status': 'complete', 'retryable': False, 'warnings': []}
    assert next_page['extractionId'] == completed.references[0]['extractionId']
    for reference in result.documents:
        assert reference['groupProcessingStatus'] == 'partial'
        assert reference['groupProcessing']['status'] == 'partial'
        assert reference['groupProcessing']['warnings']
        assert 'issueCount' not in reference['groupProcessing']
        assert reference['groupIngestionStatus'] == reference['ingestionStatus'] == 'not_submitted'
        assert reference['jobId'] is None and reference['sourceRevision'] is None
        assert reference['groupJobId'] is None and reference['groupSourceRevision'] is None
        assert not reference.keys() & {
            'contentSha256',
            'reportProjectionSnapshotRef',
            'currentSourceSnapshotRef',
            'publicationRevision',
            'submittedProjectionSnapshotRef',
            'ingestionErrorType',
        }
    submissions = batches.documents[job['_id']]['documentSubmissions'].values()
    individual = [row for row in submissions if row['command']['reportProjectionRef'] is None]
    assert len(submissions) == 2 and len(individual) == 1
    assert all(row['outcome']['ingestionStatus'] == 'received' and row['outcome']['jobId'] for row in individual)
    await settle(flow)
    profile = await flow.profile.basic_info.read(flow.scope.user_id, subject_id=flow.scope.subject_id)
    assert profile.fields['age'].value == 42


@pytest.mark.asyncio
@pytest.mark.parametrize('sexes', [('女', '男'), ('男', '女'), ('未知', '女'), ('女', '未知')])
async def test_report_group_ignores_unknown_sex_when_using_original_patient_evidence(monkeypatch, sexes):
    import json

    from agents.llm import LLMResult
    from tests.unit._support.current_document import REPORT_TEXT
    from tests.unit._support.current_document import report

    models = []
    for sex in sexes:
        value = model_report()
        if sex == '未知':
            value['basicInfo']['gender'] = None
            value['basicInfoMeta']['gender'] = report(gender=None)['basicInfoMeta']['gender']
        else:
            value['basicInfo']['gender'] = sex
            value['basicInfoMeta']['gender']['evidence'] = {'quote': f'性别：{sex}'}
        models.append(value)
    flow = await composition(monkeypatch, [], png=True)

    def use_model(index):
        flow.gateway.complete_results[:] = [LLMResult(content=json.dumps(models[index]))] * 3

    factory, _, _ = gateway(flow, consumer=flow.runtime.consumption)
    document = factory.create(user_id=flow.scope.user_id, session_id='session', final_report_id='batch')
    text = '\n'.join(line for line in REPORT_TEXT.splitlines() if not line.startswith('性别'))
    flow.upstream.client.result = {'success': True, 'text': f'肠道菌群检测报告\n性别：{sexes[0]}\n{text}'}
    use_model(0)
    await document.read(attachments('png'), index=0)
    flow.upstream.downloader.download.return_value = page_bytes('Synthetic continuation', png=True)
    flow.upstream.client.result = {'success': True, 'text': f'检测结果\n性别：{sexes[1]}\n{text}\nAST 13 U/L'}
    use_model(1)
    await document.read(RemoteFileInput(url='https://example.test/second.png', file_type='image'), index=1)
    group = await document.combine([0, 1])
    assert group.parsed.supplementary_hospital_data
    assert [document._original_subjects[index].sex for index in range(2)] == list(sexes)
    refs = await document.settle_attachments(group.references, expected_subject=SubjectInfo(sex='未知'))
    batch = factory.checkpoints.batches.documents['batch']
    if '未知' in sexes:
        assert {row['groupIngestionStatus'] for row in refs} == {'queued'}
        await settle(flow)
    else:
        assert {row['groupIngestionStatus'] for row in refs} == {'not_submitted'}
        assert {row['ingestionDisposition'] for row in refs} == {'different_subject_excluded'}
        assert len(batch['documentSubmissions']) == 2
        assert all(row['command']['reportProjectionRef'] is None for row in batch['documentSubmissions'].values())
        for row in refs:
            ledger = await flow.ledger.read_source(flow.scope, row['canonicalReportId'])
            assert ledger['publicationRevision'] == 1
            event = ledger['currentIntent']['event']
            material = await flow.readers.source.read_source_observations(
                scope=flow.scope, snapshot_id=event['snapshotRef']['snapshotId']
            )
            assert all(
                not observation['observationRef'].startswith('/reportSource/')
                for contribution in material['payload']['clinicalContributions']
                for observation in contribution['observations']
            )


@pytest.mark.asyncio
@pytest.mark.parametrize('already_archived', [False, True])
async def test_native_pdf_specialized_template_survives_shared_ocr_polling_timeout(monkeypatch, already_archived):
    flow = await composition(monkeypatch, [model_report()])
    flow.upstream.downloader.download.return_value = _native_template_pdf()

    def unavailable(*args, **kwargs):
        raise TimeoutError('synthetic OCR service polling timeout')

    monkeypatch.setattr(flow.upstream.client, 'get_result', unavailable)
    from modules.analysis.application.documents import DocumentParsingError

    original = attachments()
    if already_archived:
        with pytest.raises(DocumentParsingError, match='查询失败'):
            await flow.prepared.prepare_bundle(
                scope=flow.scope,
                lifecycle_epoch=flow.epoch,
                file_id=original.url,
                file_type='pdf',
                source_type='analysis',
            )
        (archived_trace,) = flow.traces.documents.values()
        assert archived_trace['payloads']['original_file']
    factory, consumer, _ = gateway(flow)
    document = factory.create(user_id=flow.scope.user_id, session_id='session', final_report_id='batch')
    request = ReportRequest(
        user_id=flow.scope.user_id,
        session_id='session',
        file_list=[original],
        microbiome_report_data=MicrobiomeReportDataInput(original_report_url=original.url),
    )
    parsed = await parse_request_source(
        request,
        source_client=object(),
        document_source=document,
        enforce_subject_identity=True,
    )
    assert parsed.source_type == 'pdf', parsed.source_errors
    assert any(metric.code == 'diversity' and metric.value == '7.36' for metric in parsed.metrics)
    (reference,) = parsed.source_documents
    assert reference['processingStatus'] == 'fallback_source_only'
    assert reference['ingestionStatus'] == 'not_submitted'
    assert reference['sharedProcessingStatus'] == 'failed'
    assert await document.settle_attachments(parsed.source_documents) == parsed.source_documents
    assert flow.upstream.downloader.download.await_count == (2 if already_archived else 1)
    original_proof = flow.traces.documents[reference['traceId']]['verifiedOriginal']
    if already_archived:
        assert original_proof['artifactRef'] == archived_trace['traceId']
        assert original_proof['artifactRef'] != reference['traceId']
    else:
        assert original_proof['artifactRef'] == reference['traceId']
    assert len(flow.upstream.client.submit_calls) == 1
    assert not flow.gateway.complete_requests
    consumer.consume_prepared.assert_not_awaited()
