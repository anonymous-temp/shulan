"""Exercise the shared file workflow through the production BSON trace adapter."""

import pytest

from adapters.analysis.document_trace import MongoDocumentTraceStore
from modules.analysis.application.service import HealthReportUpstreamError
from tests.unit.services.analysis.fakes import FakeDocumentCollection
from tests.unit.services.analysis.test_analysis_json import extract_narrative
from tests.unit.services.analysis.test_document_reuse import request
from tests.unit.services.analysis.test_document_reuse import reuse_flow
from tests.unit.services.analysis.test_document_workflow import install_graph


def mongo_trace_flow():
    flow = reuse_flow()
    flow.metadata, flow.chunks = FakeDocumentCollection(), FakeDocumentCollection()
    flow.traces = MongoDocumentTraceStore(flow.metadata, flow.chunks)
    flow.service._document_trace_store = flow.traces
    return flow


async def read_checkpoint(flow, name):
    document = next(iter(flow.index_collection.documents.values()))
    return await flow.traces.read_payload(
        document['payloadRefs'][name], document['userId'], document['subjectId'], name
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('entrypoint', ['analysis', 'record'])
@pytest.mark.parametrize('scenario', ['first_pass', 'repair', 'exhausted', 'schema_repair'])
async def test_model_processing_roundtrips_all_trace_types(entrypoint, scenario):
    flow = mongo_trace_flow()
    table = '|项目|结果|单位|\n|---|---|---|\n|项目A|0|%|\n|项目B|1|%|\n'
    narrative = '本次检测结果：项目甲=1。'
    if scenario == 'schema_repair':
        narrative += '说明文字。' * 110 + '\n本次检测结果：项目乙=2。' + '说明文字。' * 110

    def respond(value):
        correction = '请将下面报告重新输出' in value
        if scenario == 'schema_repair' and not correction:
            return {'reportType': 'lab', 'observations': 'invalid-array'}
        if scenario == 'exhausted' or scenario == 'repair' and not correction:
            return {'reportType': 'lab', 'observations': 'invalid'}
        if scenario == 'first_pass':
            return {
                'reportType': 'lab',
                'observations': [
                    {'itemName': '项目A', 'resultValue': '0', 'unit': '%'},
                    {'itemName': '项目B', 'resultValue': '1', 'unit': '%'},
                ],
            }
        return extract_narrative(value)

    flow.client.result = {'success': True, 'text': table if scenario == 'first_pass' else narrative}
    install_graph(flow, respond)
    if scenario == 'exhausted':
        with pytest.raises(HealthReportUpstreamError) as failure:
            await flow.service.process_document(**request(source_type=entrypoint))
        assert failure.value.__cause__.automatic_retryable is False
        assert not flow.collection.documents and not flow.jobs.jobs
    else:
        outcome = await flow.service.process_document(**request(source_type=entrypoint))
        assert len(outcome.report['observations']) == (1 if scenario == 'repair' else 2)
        assert next(iter(flow.index_collection.documents.values()))['status'] == 'completed'
        assert await read_checkpoint(flow, 'interpretation')
        assert await read_checkpoint(flow, 'publication')
    metrics = await read_checkpoint(flow, 'analysis_metrics')
    expected_calls = {'first_pass': 1, 'repair': 2, 'exhausted': 2, 'schema_repair': 2}[scenario]
    assert len(flow.graph.calls) == metrics['modelCalls'] == expected_calls
    assert metrics['firstPassSuccess'] is (scenario == 'first_pass')
    assert metrics['completed'] is (scenario != 'exhausted')
    if scenario in {'repair', 'exhausted', 'schema_repair'}:
        assert await read_checkpoint(flow, 'analysis_input_00000_repair')
        assert await read_checkpoint(flow, 'analysis_output_00000_repair')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'name', ['publication', 'analysis_metrics', 'analysis_input_00000_repair', 'analysis_output_00000_repair']
)
async def test_new_trace_payload_names_roundtrip_with_checksum_and_owner_scope(name):
    metadata, chunks = FakeDocumentCollection(), FakeDocumentCollection()
    store = MongoDocumentTraceStore(metadata, chunks)
    await store.create({'_id': 'trace', 'userId': 'user', 'subjectId': 'subject'})
    payload = {'type': name, 'result': {'value': '0'}, 'missing': None}
    await store.write_payload('trace', 'user', name, payload)
    assert await store.read_payload('trace', 'user', 'subject', name) == payload
    with pytest.raises(LookupError):
        await store.read_payload('trace', 'other-user', 'subject', name)
    with pytest.raises(LookupError):
        await store.read_payload('trace', 'user', 'other-subject', name)
    next(iter(chunks.documents.values()))['data'] = b'corrupted'
    with pytest.raises(RuntimeError, match='校验失败'):
        await store.read_payload('trace', 'user', 'subject', name)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'name',
    [
        'unexpected',
        'analysis_metrics.extra',
        '$analysis_metrics',
        'analysis_part_00000_repair',
        'analysis_input_00000_repair_again',
        'analysis_input_00000_repair.unsafe',
    ],
)
async def test_trace_names_remain_explicit_and_reject_unknown_paths_before_writes(name):
    metadata, chunks = FakeDocumentCollection(), FakeDocumentCollection()
    store = MongoDocumentTraceStore(metadata, chunks)
    with pytest.raises(ValueError, match='未知文档留痕内容类型'):
        await store.write_payload('trace', 'user', name, {})
    assert not metadata.documents and not chunks.documents


@pytest.mark.asyncio
@pytest.mark.parametrize('model_fails', [False, True])
async def test_metrics_write_failure_cannot_override_success_or_original_processing_error(model_fails):
    flow = mongo_trace_flow()
    write = flow.traces.write_payload

    async def fail_metrics(trace_id, user_id, name, payload):
        if name == 'analysis_metrics':
            raise ValueError('synthetic metrics failure')
        return await write(trace_id, user_id, name, payload)

    flow.client.result = {'success': True, 'text': '本次检测结果：项目甲=1。'}

    def respond(text):
        if model_fails:
            raise RuntimeError('synthetic original model failure')
        return extract_narrative(text)

    flow.traces.write_payload = fail_metrics
    install_graph(flow, respond)
    if model_fails:
        with pytest.raises(HealthReportUpstreamError) as failure:
            await flow.service.process_document(**request(source_type='record'))
        chain = []
        error = failure.value
        while error is not None:
            chain.append(str(error))
            error = error.__cause__
        assert (await read_checkpoint(flow, 'analysis_output'))['errorType'] == 'RuntimeError'
        assert 'synthetic metrics failure' not in chain
        assert not flow.collection.documents and not flow.jobs.jobs
    else:
        result = await flow.service.process_document(**request(source_type='analysis'))
        assert result.report['observations'][0]['itemName'] == '项目甲'
        assert await read_checkpoint(flow, 'interpretation')
        assert next(iter(flow.index_collection.documents.values()))['status'] == 'completed'


@pytest.mark.asyncio
async def test_required_model_output_archive_failure_still_prevents_publication():
    flow = mongo_trace_flow()
    write = flow.traces.write_payload

    async def fail_output(trace_id, user_id, name, payload):
        if name == 'analysis_output':
            raise RuntimeError('synthetic required archive failure')
        return await write(trace_id, user_id, name, payload)

    flow.traces.write_payload = fail_output
    with pytest.raises(RuntimeError, match='synthetic required archive failure'):
        await flow.service.process_document(**request(source_type='record'))
    assert not flow.collection.documents and not flow.jobs.jobs
    metrics = await read_checkpoint(flow, 'analysis_metrics')
    assert metrics['completed'] is False and metrics['firstPassSuccess'] is False


@pytest.mark.asyncio
async def test_resume_after_repair_archive_rejection_reuses_docmind_and_saved_recovery_plan():
    flow = mongo_trace_flow()
    flow.client.result = {'success': True, 'text': '本次检测结果：项目甲=1。'}

    def respond(value):
        if '请将下面报告重新输出' in value:
            return extract_narrative(value)
        return {'reportType': 'lab', 'observations': 'invalid'}

    write = flow.traces.write_payload

    async def previous_adapter(trace_id, user_id, name, payload):
        if name.endswith('_repair'):
            raise ValueError('未知文档留痕内容类型')
        return await write(trace_id, user_id, name, payload)

    flow.traces.write_payload = previous_adapter
    install_graph(flow, respond)
    with pytest.raises(ValueError, match='未知文档留痕内容类型'):
        await flow.service.process_document(**request(source_type='record'))
    assert len(flow.graph.calls) == 1
    assert (await read_checkpoint(flow, 'analysis_part_00000'))['result']['processing']['status'] == 'unavailable'
    document = next(iter(flow.index_collection.documents.values()))
    flow.traces.write_payload = write
    result = await flow.service.process_document(user_id='user-1', document_id=document['_id'], source_type='record')
    assert [item['resultValue'] for item in result.report['observations']] == ['1']
    assert len(flow.client.submit_calls) == 1
    assert len(flow.graph.calls) == 2
    metrics = await read_checkpoint(flow, 'analysis_metrics')
    assert metrics['modelCalls'] == metrics['outputRecoveryCalls'] == 1
    assert metrics['firstPassSuccess'] is False
