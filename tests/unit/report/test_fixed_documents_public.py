"""The Report consumer composed with real public Document providers and isolated I/O."""

import hashlib
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from adapters.report.document_source import SharedReportDocuments
from adapters.report.fixed_documents import REPORT_FIXED_SELECTION
from adapters.report.fixed_documents import create_fixed_report_document_source_factory
from modules.analysis.application.service import HealthReportUpstreamError
from modules.analysis.contracts import OutputSelection
from modules.report import parsing
from modules.report.document_projection import ReportProjectionService
from modules.report.document_source import extract_intrinsic_report_source
from modules.report.schemas import RemoteFileInput
from tests.unit.report.test_document_projection import Cache
from tests.unit.services.analysis.test_document_report_publication import environment as provider_environment
from tests.unit.services.analysis.test_document_report_publication import prohibit_reprocessing
from tests.unit.services.analysis.test_document_snapshots import SCOPE
from tests.unit.services.analysis.test_document_workflow import install_graph
from tests.unit.services.analysis.test_document_workflow import lab


def environment():
    env = provider_environment(extractor=extract_intrinsic_report_source)
    env.cache = Cache()
    env.projections = ReportProjectionService(env.cache)
    env.factory = create_fixed_report_document_source_factory(
        compatibility=env.api,
        materials=env.materials,
        publications=env.publisher,
        projections=env.projections,
    )
    env.reader = env.factory.create(user_id=SCOPE.user_id, session_id='session', final_report_id='final')
    env.old = SharedReportDocuments(env.flow.service, SCOPE.user_id, 'session', 'final')
    return env


def content(source):
    return source.parsed.model_dump(mode='json', exclude={'generated_at'})


@pytest.mark.asyncio
async def test_real_v16_business_content_and_formal_references_match_legacy_without_extra_model():
    env = environment()
    source = RemoteFileInput(url='https://example.test/a.pdf', file_type='pdf')
    new = await env.reader.read(source, index=0)
    count = len(env.flow.jobs.jobs)
    old = await env.old.read(source, index=0)
    assert content(new) == content(old)
    assert new.parsed.supplementary_hospital_data
    for key in ('documentId', 'canonicalReportId', 'sourceRevision', 'extractionId', 'jobId'):
        assert new.references[0][key] == old.references[0][key]
    trace = env.traces.documents[new.references[0]['outputTraceId']]
    assert trace['extractionId'] == new.references[0]['extractionId']
    assert len(env.flow.client.submit_calls) == len(env.flow.graph.calls) == 1
    assert len(env.flow.jobs.jobs) == count == 2
    assert len(env.cache.rows) == 1
    prepare_trace = env.traces.documents[new.references[0]['traceId']]
    assert prepare_trace['source']['sourceId'] == 'final'
    assert prepare_trace['source']['sessionId'] == 'session'
    assert prepare_trace['source']['fileIndex'] == 0


@pytest.mark.asyncio
async def test_real_group_keeps_attachment_order_and_first_document_dedup_without_shared_business_cache():
    env = environment()
    env.flow.downloader.download.return_value = b'\x89PNG\r\n\x1a\nfirst'
    env.flow.client.result = {'success': True, 'text': '肠道总体评分\n您的肠道总评分为：\n58\n尿酸 468 umol/L'}
    install_graph(env.flow, lambda text: lab())
    first = await env.reader.read(RemoteFileInput(url='https://example.test/a.png', file_type='image'), index=0)
    env.flow.downloader.download.return_value = b'\x89PNG\r\n\x1a\nsecond'
    env.flow.client.result = {'success': True, 'text': '2.肠道免疫力评估\n肠道免疫力 46\n尿酸 468 umol/L'}
    second = await env.reader.read(RemoteFileInput(url='https://example.test/b.png', file_type='image'), index=1)
    await env.reader.read(RemoteFileInput(document_id=first.references[0]['documentId']), index=2)
    result = await env.reader.combine([0, 1, 2])
    calls = len(env.flow.graph.calls)
    jobs = len(env.flow.jobs.jobs)
    for index, item in enumerate((first, second, first)):
        await env.old.read(RemoteFileInput(document_id=item.references[0]['documentId']), index=index)
    expected = await env.old.combine([0, 1, 2])
    assert content(result) == content(expected)
    assert [item['fileIndex'] for item in result.references] == [0, 1, 2]
    assert result.references[0]['documentId'] == result.references[2]['documentId']
    assert result.parsed.source_name == 'a.png / b.png'
    trace = env.traces.documents[result.references[0]['groupOutputTraceId']]
    assert trace['extractionId'] == result.references[0]['groupExtractionId']
    assert len(env.flow.graph.calls) == calls == 2
    assert len(env.flow.jobs.jobs) == jobs
    assert len(env.cache.rows) == 3


@pytest.mark.asyncio
async def test_real_publication_ack_loss_replays_same_preparation_and_real_trace_without_new_jobs():
    env = environment()
    source = RemoteFileInput(url='https://example.test/a.pdf', file_type='pdf')
    publish = env.publisher.publish_report_source
    saved = []

    async def lose_ack(**kwargs):
        saved.append(await publish(**kwargs))
        raise RuntimeError('lost final publication ACK')

    env.publisher.publish_report_source = lose_ack
    with pytest.raises(RuntimeError, match='lost final'):
        await env.reader.read(source, index=0)
    fixed = env.reader.processor.prepared[0]
    jobs = len(env.flow.jobs.jobs)
    prohibit_reprocessing(env)
    env.api.prepare_bundle = AsyncMock(side_effect=AssertionError('must reuse accepted refs and receipt'))
    env.publisher.publish_report_source = publish
    result = await env.reader.read(source, index=0)
    assert env.reader.processor.prepared[0] == fixed
    assert result.references[0]['traceId'] == fixed.publication.preparation_receipt.trace_id
    assert result.references[0]['sourceRevision'] == saved[0].legacy_source_revision
    assert len(env.flow.jobs.jobs) == jobs == 2
    assert len(env.flow.client.submit_calls) == len(env.flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_report_private_next_revision_never_changes_formal_legacy_publication_or_record_bundle():
    env = environment()
    source = RemoteFileInput(url='https://example.test/a.pdf', file_type='pdf')
    first = await env.reader.read(source, index=0)
    prepared = env.reader.processor.prepared[0]
    frozen = deepcopy(prepared.bundle.understanding.payload)
    next_selection = OutputSelection('report_source', '2', 'report-source-next')
    newer = AsyncMock(return_value={'subject': {'name': 'Report private vNext'}})
    projections = ReportProjectionService(env.cache, implementations={next_selection: newer})
    factory = create_fixed_report_document_source_factory(
        compatibility=env.api,
        materials=env.materials,
        publications=env.publisher,
        projections=projections,
        selection=next_selection,
    )
    other = factory.create(user_id=SCOPE.user_id, session_id='session', final_report_id='final')
    changed = await other.read(RemoteFileInput(document_id=first.references[0]['documentId']), index=0)
    assert changed.parsed.subject.name == 'Report private vNext'
    assert changed.references[0]['sourceRevision'] == first.references[0]['sourceRevision']
    assert changed.references[0]['extractionId'] == first.references[0]['extractionId']
    restored = await env.api.read_bundle(
        scope=SCOPE,
        understanding_snapshot_id=prepared.bundle.understanding.snapshot_id,
        selection=prepared.publication.selection,
    )
    assert restored.understanding.payload == frozen
    assert len(env.flow.graph.calls) == 1 and len(env.flow.jobs.jobs) == 2
    newer.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_partial_keeps_approved_rows_and_only_base_publication(monkeypatch):
    env = environment()
    originals = []

    def native(filename, raw, *, ocr):
        originals.append((filename, raw, ocr))
        return parsing.ExtractedDocument(
            filename,
            'pdf',
            '',
            tables=[parsing.ExtractedTable(1, [['项目', '结果'], ['尿酸', '468 umol/L']])],
            metadata={'content_sha256': hashlib.sha256(raw).hexdigest()},
        )

    monkeypatch.setattr(parsing, '_extract_pdf', native)
    env.flow.client.result = {'success': True, 'text': '尿酸 468 umol/L'}
    partial = lab()
    partial['observations'].append({'itemName': '未成形字段', 'resultValue': []})
    install_graph(env.flow, lambda text: partial)
    result = await env.reader.read(RemoteFileInput(url='https://example.test/a.pdf', file_type='pdf'), index=0)
    assert result.parsed.supplementary_hospital_data
    assert result.references[0]['processingStatus'] == 'partial'
    assert result.parsed.source_errors == ['fileList[0]: SHARED_INTERPRETATION_PARTIAL']
    assert result.references[0]['ingestionStatus'] == 'not_submitted'
    assert result.references[0]['jobId'] is None and result.references[0]['sourceRevision'] is None
    assert len(env.flow.jobs.jobs) == 1
    before = len(env.flow.graph.calls)
    combined = await env.reader.combine([0])
    assert combined.references[0]['groupProcessingStatus'] == 'partial'
    assert len(env.flow.graph.calls) == before
    assert originals and all(
        raw == env.flow.downloader.download.return_value and ocr is False for _, raw, ocr in originals
    )
    assert result.evaluation_original_documents


@pytest.mark.asyncio
@pytest.mark.parametrize('version,allowed', [('analysis-health-report-v5', True), ('analysis-health-report-v6', False)])
async def test_real_failed_material_is_bound_to_call_and_preserves_legacy_version_fallback(version, allowed):
    env = environment()
    env.flow.interpreter.VERSION = version
    env.flow.downloader.download.return_value = b'\x89PNG\r\n\x1a\nimage'
    env.flow.client.result = {'success': True, 'text': '肠道总体评分\n您的肠道总评分为：\n58'}
    install_graph(env.flow, lambda text: '{"observations": [')
    source = RemoteFileInput(url='https://example.test/a.png', file_type='image')
    if not allowed:
        with pytest.raises(HealthReportUpstreamError):
            await env.reader.read(source, index=0)
        assert not env.reader.recovered
    else:
        result = await env.reader.read(source, index=0)
        assert result.parsed.metrics[0].value == '58'
        assert result.references[0]['processingStatus'] == 'fallback_source_only'
        assert result.references[0]['ingestionStatus'] == 'not_submitted'
        assert result.references[0]['traceId'] in env.traces.documents
        assert result.references[0]['sourceExtractorVersion'] == REPORT_FIXED_SELECTION.implementation_revision
    assert not env.flow.jobs.jobs and not env.commands.documents
