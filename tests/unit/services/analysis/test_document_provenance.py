"""Full document-to-profile lineage without real SDK, model or database calls."""

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.document_trace import MongoDocumentTraceStore
from health_profile import HealthProfileService
from health_profile import HealthReportProfileProcessor
from health_profile import HealthReportSourceConflictError
from modules.analysis.application.documents import DocumentInterpretationError
from modules.analysis.application.service import HealthReportUpstreamError
from tests.unit._support.document_profile_composition.analysis.document_provenance import (
    LocalDocMindClient as LocalDocMindClient,
)
from tests.unit._support.document_profile_composition.analysis.document_provenance import (
    attach_source_evidence as attach_source_evidence,
)
from tests.unit._support.document_profile_composition.analysis.document_provenance import build_flow as build_flow
from tests.unit._support.document_profile_composition.analysis.document_provenance import report_text as report_text
from tests.unit.health_profile.test_report_ingestion import FakeProfileRepository
from tests.unit.health_profile.test_report_ingestion import ReportIngestionFixture
from tests.unit.services.analysis.fakes import FakeDocumentCollection


def arguments(**changes):
    return {
        'file_id': 'https://example.test/report.pdf?signature=test-only',
        'file_type': 'pdf',
        'user_id': 'user-1',
        'report_id': 'test-input-report',
        **changes,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('entrypoint', ['analysis', 'record', 'report'])
async def test_all_entrypoints_preserve_raw_data_and_feed_the_same_profile_processor(entrypoint):
    raw = {
        'success': True,
        'text': report_text(ReportIngestionFixture.record().result) + '\n附录说明。' * 200,
        'data': {'layouts': [{'text': '尿酸 468', 'pageNum': 1}], 'providerField': {'v': 1}},
    }
    flow = build_flow(raw_result=raw)
    outcome = await flow.service.process_document(
        **arguments(source_type=entrypoint, source_id='test-source-1', session_id='test-session-1', file_index=0)
    )
    trace = flow.traces.documents[outcome.trace_id]
    record = await flow.reports.get_report(outcome.report['reportId'])
    assert trace['fileUrl'] == arguments()['file_id']
    assert trace['source'] == {
        'entrypoint': entrypoint,
        'sourceId': 'test-source-1',
        'sessionId': 'test-session-1',
        'fileIndex': 0,
    }
    assert trace['taskId'] == 'task-1'
    assert trace['status'] == 'completed'
    assert record.document_id == outcome.document_id == outcome.trace_id
    assert outcome.text == raw['text']
    assert flow.traces.payloads[(outcome.document_id, 'docmind_result')] == raw
    model_input = next(
        payload
        for (trace_id, name), payload in flow.traces.payloads.items()
        if trace_id == outcome.document_id and name.startswith('analysis_input')
    )
    assert model_input['truncated'] is False
    assert model_input['text'] == flow.graph.calls[0]['input']['report_text']
    inputs = sorted(
        (payload['source']['start'], payload['source']['end'])
        for (trace_id, name), payload in flow.traces.payloads.items()
        if trace_id == outcome.document_id and name.startswith('analysis_input')
    )
    metrics = flow.traces.payloads[(outcome.document_id, 'analysis_metrics')]
    assert inputs[0][0] == 0 and inputs[-1][1] == metrics['preparedChars']
    assert all(left[1] == right[0] for left, right in zip(inputs, inputs[1:]))
    assert flow.graph.calls[0]['context'].subject_id == 'subject-self'
    assert 'documentId' not in record.public_result()
    assert 'fileUrl' not in record.public_result()
    assert len(flow.jobs.jobs) == 1
    job = next(iter(flow.jobs.jobs.values()))
    assert job.source_type.value == 'health_report'
    assert job.source_id == record.report_id
    assert job.job_id == outcome.ingestion_job_id
    repository = FakeProfileRepository(ReportIngestionFixture.subject())
    processor = HealthReportProfileProcessor(store=flow.reports, repository=repository)
    processed = await processor.process(job)
    assert processed.fact_count == 2
    assert {fact.sources[0].source_id for fact in repository.facts} == {record.report_id}
    assert {fact.sources[0].pointer for fact in repository.facts} == {'/expansion/0', '/expansion/1'}
    snapshot = await HealthProfileService(repository).get_profile('user-1', subject_id='subject-self')
    assert snapshot['indicatorGroups']


@pytest.mark.asyncio
async def test_retry_reuses_saved_text_and_records_a_separate_usage():
    flow = build_flow()
    first = await flow.service.process_document(**arguments())
    second = await flow.service.process_document(**arguments(source_type='report', source_id='test-final-batch'))
    assert second.reused is True
    assert second.document_id == first.document_id
    assert second.text == first.text
    assert second.trace_id != first.trace_id
    assert len(flow.client.submit_calls) == len(flow.graph.calls) == len(flow.jobs.jobs) == 1
    assert flow.traces.documents[second.trace_id]['reusedDocumentId'] == first.document_id


@pytest.mark.asyncio
async def test_local_bytes_are_submitted_and_archived_without_redownloading_the_url():
    flow = build_flow()
    content = b'%PDF-1.7\nsynthetic-document'
    outcome = await flow.service.process_document(**arguments(content=content, source_type='report'))
    assert flow.client.local_content == content
    assert not flow.client.local_path.exists()
    assert flow.traces.payloads[(outcome.document_id, 'original_file')] == {'content': content}
    assert flow.traces.documents[outcome.document_id]['contentSha256']
    with pytest.raises(HealthReportSourceConflictError):
        await flow.service.process_document(**arguments(content=content + b'changed', source_type='report'))
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_downloaded_extensionless_image_does_not_need_another_network_probe():
    flow = build_flow()
    await flow.service.process_document(
        **arguments(file_id='https://example.test/download', file_type='image', content=b'\x89PNG\r\n\x1a\nsynthetic')
    )
    assert flow.client.submit_calls[0][1:] == ('health-report.png', 'png')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'url, declared, content',
    [
        ('https://example.test/report.pdf', 'pdf', b'\x89PNG\r\n\x1a\nsynthetic'),
        ('https://example.test/report.png', 'pdf', b'\x89PNG\r\n\x1a\nsynthetic'),
        ('https://example.test/download', 'image', b'%PDF-1.7\nsynthetic'),
        ('https://example.test/report.png', 'image', b'not-an-image'),
    ],
)
async def test_downloaded_bytes_cannot_be_relabelled_as_another_file_type(url, declared, content):
    flow = build_flow()
    with pytest.raises(ValueError):
        await flow.service.process_document(**arguments(file_id=url, file_type=declared, content=content))
    assert not flow.client.submit_calls
    assert next(iter(flow.traces.documents.values()))['status'] == 'failed'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'result,content_outcome',
    [
        ({'success': False, 'error': 'test provider error', 'data': {'status': 'failed'}}, None),
        ({'success': True, 'text': '  ', 'data': {'layouts': []}}, 'no_readable_text'),
        ({'success': True, 'text': ['not a string']}, None),
    ],
)
async def test_failed_or_empty_sdk_results_survive_without_becoming_profile_sources(result, content_outcome):
    flow = build_flow(raw_result=result)
    with pytest.raises(HealthReportUpstreamError) as caught:
        await flow.service.process_document(**arguments())
    trace = next(iter(flow.traces.documents.values()))
    assert trace['status'] == 'failed'  # The shared clinical entry still has no usable report.
    if content_outcome:
        assert isinstance(caught.value.__cause__, DocumentInterpretationError)
        assert caught.value.__cause__.content_outcome == content_outcome
        interpretation = flow.traces.payloads[(trace['traceId'], 'interpretation')]
        assert interpretation['processing']['status'] == 'unavailable'
        assert interpretation['processing']['failureKind'] == 'content'
        assert interpretation['observations'] == []
    else:
        assert not isinstance(caught.value.__cause__, DocumentInterpretationError)
        assert (trace['traceId'], 'interpretation') not in flow.traces.payloads
    assert trace['taskId'] == 'task-1'
    assert flow.traces.payloads[(trace['traceId'], 'docmind_result')] == result
    assert flow.graph.calls == []
    assert flow.collection.documents == []
    assert flow.jobs.jobs == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['submit', 'poll'])
async def test_sdk_exceptions_are_upstream_errors_with_attempt_identity(stage):
    flow = build_flow()
    if stage == 'submit':
        flow.client.submit_error = RuntimeError('test upstream failure')
    else:

        def fail(*args, **kwargs):
            raise RuntimeError('test upstream failure')

        flow.client.get_result = fail
    with pytest.raises(HealthReportUpstreamError):
        await flow.service.process_document(**arguments())
    trace = next(iter(flow.traces.documents.values()))
    assert trace['stage'] == ('submitting' if stage == 'submit' else 'polling')
    assert trace['upstreamErrorType'] == 'RuntimeError'
    assert flow.jobs.jobs == {}


@pytest.mark.asyncio
async def test_invalid_model_answer_is_archived_without_saving_a_report():
    flow = build_flow()
    flow.graph.content = 'test invalid JSON output'
    with pytest.raises(HealthReportUpstreamError):
        await flow.service.process_document(**arguments())
    trace = next(iter(flow.traces.documents.values()))
    assert trace['stage'] == 'interpreting'
    assert flow.traces.payloads[(trace['traceId'], 'analysis_output')]['content'] == flow.graph.content
    assert flow.collection.documents == []


@pytest.mark.asyncio
async def test_archive_must_be_durable_before_sdk_submission_or_source_publication():
    flow = build_flow()
    flow.traces.create = AsyncMock(side_effect=RuntimeError('archive down'))
    with pytest.raises(RuntimeError, match='archive down'):
        await flow.service.process_document(**arguments())
    assert flow.client.submit_calls == []
    flow = build_flow()
    flow.traces.write_payload = AsyncMock(side_effect=RuntimeError('payload write failed'))
    with pytest.raises(RuntimeError, match='payload write failed'):
        await flow.service.process_document(**arguments())
    assert flow.graph.calls == []
    assert flow.jobs.jobs == {}


@pytest.mark.asyncio
async def test_failed_profile_queue_keeps_raw_and_structured_data_for_compensation():
    flow = build_flow()
    flow.jobs.fail_create = True
    first = await flow.service.process_document(**arguments())
    assert first.ingestion_status == 'pending'
    assert first.text
    flow.jobs.fail_create = False
    second = await flow.service.process_document(**arguments())
    assert second.ingestion_status == 'queued'
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_legacy_public_replay_works_but_raw_text_is_never_invented():
    flow = build_flow()
    first = await flow.service.process_document(**arguments())
    flow.collection.documents[0].pop('documentId')
    assert await flow.service.process(**arguments()) == first.report
    with pytest.raises(HealthReportUpstreamError, match='历史报告缺少原文归档'):
        await flow.service.process_document(**arguments())
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_concurrent_save_returns_the_winning_documents_text_and_result():
    flow = build_flow()
    first = await flow.service.process_document(**arguments())
    original_get = flow.source.get_existing
    flow.source.get_existing = AsyncMock(return_value=None)
    other = deepcopy(ReportIngestionFixture.record().result)
    other['expansion'][0]['resultValue'] = '420'
    flow.client.result = {'success': True, 'text': report_text(other)}
    flow.graph.content = json.dumps(other, ensure_ascii=False)
    second = await flow.service.process_document(**arguments())
    flow.source.get_existing = original_get
    assert second.document_id == first.document_id
    assert second.text == first.text
    assert second.report == first.report


@pytest.mark.asyncio
async def test_cancelled_attempt_is_distinguishable_from_upstream_failure():
    flow = build_flow()
    entered = asyncio.Event()

    async def pending(**kwargs):
        entered.set()
        await asyncio.Future()

    flow.interpreter.extract_document = pending
    task = asyncio.create_task(flow.service.process_document(**arguments()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert next(iter(flow.traces.documents.values()))['status'] == 'cancelled'
    assert not flow.jobs.jobs


@pytest.mark.asyncio
async def test_cross_user_and_subject_cannot_reuse_report_ids():
    flow = build_flow()
    await flow.service.process_document(**arguments())
    for changes in ({'user_id': 'test-other'}, {'subject_id': 'test-other-subject'}):
        with pytest.raises(HealthReportSourceConflictError):
            await flow.service.process_document(**arguments(**changes))
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_large_raw_response_roundtrips_in_chunks_and_enforces_ownership():
    metadata, chunks = FakeDocumentCollection(), FakeDocumentCollection()
    store = MongoDocumentTraceStore(metadata, chunks)
    await store.create({'_id': 'test-trace', 'userId': 'test-user', 'subjectId': 'test-subject'})
    payload = {'success': True, 'text': '检测\n' * 3_000_000, 'data': {'layouts': [{'page': 1}]}}
    await store.write_payload('test-trace', 'test-user', 'docmind_result', payload)
    reference = metadata.documents['test-trace']['payloads']['docmind_result']
    assert reference['bytes'] > 16 * 1024 * 1024
    assert len(chunks.documents) == reference['chunks']
    assert max(len(item['data']) for item in chunks.documents.values()) <= store.CHUNK_BYTES
    assert await store.read_payload('test-trace', 'test-user', 'test-subject', 'docmind_result') == payload
    for user, subject in [('test-other', 'test-subject'), ('test-user', 'test-other')]:
        with pytest.raises(LookupError):
            await store.read_payload('test-trace', user, subject, 'docmind_result')
    next(iter(chunks.documents.values()))['data'] = b'corrupt'
    with pytest.raises(RuntimeError, match='校验失败'):
        await store.read_payload('test-trace', 'test-user', 'test-subject', 'docmind_result')


@pytest.mark.asyncio
async def test_partial_payload_write_is_not_published_as_complete():
    metadata, chunks = FakeDocumentCollection(), FakeDocumentCollection()
    store = MongoDocumentTraceStore(metadata, chunks)
    await store.create({'_id': 'test-trace', 'userId': 'test-user', 'subjectId': 'test-subject'})
    chunks.fail_insert = True
    with pytest.raises(RuntimeError):
        await store.write_payload('test-trace', 'test-user', 'docmind_result', {'text': 'test'})
    assert 'payloads' not in metadata.documents['test-trace']
