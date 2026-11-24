"""Complete model replies, split recovery, and actionable diagnostics without network calls."""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from modules.analysis.application import service as analysis_service
from modules.analysis.application.service import HealthReportInterpreter
from tests.unit.services.analysis.test_document_workflow import lab


@pytest.fixture
def analysis_logs(monkeypatch, caplog):
    logger = logging.getLogger(__name__)
    monkeypatch.setattr(analysis_service, 'logger', logger)
    caplog.set_level(logging.DEBUG, logger=logger.name)
    return caplog


@pytest.mark.parametrize(
    'wrapper',
    ['%s', ' \n%s\n ', '```json\n%s\n```', '```\n%s\n```', '\n```JSON \r\n%s\r\n  ```\n'],
)
def test_complete_object_and_complete_json_fences_preserve_values(wrapper):
    raw = {
        **lab('3', name='叶黄素'),
        'title': '文字中的 {}、[]、``` 和 "引号"',
        'extra': {'null': None, 'zero': 0, 'false': False, 'empty': []},
    }
    reply = wrapper % json.dumps(raw, ensure_ascii=False)
    diagnostics = {}
    assert HealthReportInterpreter._parse_reply(reply, diagnostics=diagnostics) == raw
    assert diagnostics == {'status': 'parsed', 'replyChars': len(reply)}


@pytest.mark.parametrize(
    'reply',
    [
        '',
        '   ',
        None,
        {'reportType': 'lab'},
        '不是 JSON',
        '[{"reportType": "lab"}]',
        'null',
        'false',
        '1',
        '"{}"',
        '{"observations": [{"itemName": "叶黄素", "resultValue": "3"}',
        '{"observations": [{"observedAt": "2026-',
        '{"reportType": "lab"}\n{"unfinished": [ ',
        '{"reportType": "lab"}\n{"reportType": "imaging"}',
        '以下是结果：\n{"reportType": "lab"}',
        '{"reportType": "lab"}\n解释说明',
        '```json\n{"reportType": "lab"}',
        '{"reportType": "lab"}\n```',
        '```json\n{"reportType": "lab"}\n```\n未完成的后续内容',
        '```json\n{}\n```\n```json\n{}\n```',
        '```python\n{}\n```',
    ],
)
def test_incomplete_or_extra_content_is_never_clipped_into_a_success(reply):
    diagnostics = {}
    assert HealthReportInterpreter._parse_reply(reply, diagnostics=diagnostics) is None
    assert diagnostics['status'] == 'failed'
    assert diagnostics['errorType'] in {'JSONDecodeError', 'AgentProtocolError'}
    assert diagnostics['message']


@pytest.mark.parametrize('wrapper', [' \n%s  ', '\n  ```json\r\n%s\r\n```\n'])
def test_decoder_positions_refer_to_the_original_reply_including_fences(wrapper):
    reply = wrapper % '{\n    "title": "叶黄素",\n    "observations": ?\n}'
    diagnostics = {}
    assert HealthReportInterpreter._parse_reply(reply, diagnostics=diagnostics) is None
    position = reply.index('?')
    assert diagnostics['position'] == position
    assert diagnostics['line'] == reply.count('\n', 0, position) + 1
    assert diagnostics['column'] == position - reply.rfind('\n', 0, position)
    assert diagnostics['replyChars'] == len(reply)


class MemoryTrace:
    trace_id = 'synthetic-trace'

    def __init__(self):
        self.payloads = {}

    async def write_payload(self, name, value):
        from copy import deepcopy

        self.payloads[name] = deepcopy(value)

    async def read_payload(self, name):
        from copy import deepcopy

        return deepcopy(self.payloads.get(name))

    async def update(self, **fields):
        pass


def interpreter_with(respond):
    from tests.unit.services.analysis.test_analysis_service_agent import FakeDocMindClient
    from tests.unit.services.analysis.test_analysis_service_agent import _build_runtime

    runtime, graph = _build_runtime('{}')

    async def invoke(input, config=None, *, context=None, **kwargs):
        graph.calls.append({'input': input, 'context': context})
        result = respond(input['report_text'])
        if asyncio.iscoroutine(result):
            result = await result
        return {
            'content': json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else result,
            'status': 'completed',
            'transport_retries': 0,
        }

    graph.ainvoke = invoke
    return HealthReportInterpreter(runtime, count_input_tokens=len, docmind_client=FakeDocMindClient()), graph


def extract_narrative(text):
    import re

    return {
        'reportType': 'lab',
        'status': 'pending',
        'observations': [
            {'itemName': name, 'resultValue': value}
            for name, value in re.findall(r'本次检测结果：(项目[甲乙丙])=(\d+)。', text)
        ],
    }


@pytest.mark.asyncio
async def test_valid_json_fields_are_accepted_without_source_pattern_matching():
    interpreter, graph = interpreter_with(lambda _: lab(name='项目甲'))
    result = await interpreter.analyze('Patient result: 项目甲 measured 3 umol/L.')
    assert result['processing']['status'] == 'complete'
    assert result['observations'][0]['itemName'] == '项目甲'
    assert len(graph.calls) == 1


@pytest.mark.asyncio
async def test_malformed_model_output_does_not_trigger_local_table_extraction():
    interpreter, graph = interpreter_with(lambda _: '不是JSON')
    result = await interpreter.analyze('|项目|结果|结果评价|\n|---|---|---|\n|项目甲|0|正常|\n')
    assert result['observations'] == []
    assert result['processing']['status'] == 'unavailable'
    assert result['status'] == 'pending'
    assert len(graph.calls) == 1


@pytest.mark.asyncio
async def test_json_format_retry_is_bounded_without_splitting_source_regions():
    content = '本次检测结果：项目甲=1。' + '说明文字。' * 110 + '\n'
    content += '本次检测结果：项目乙=2。' + '说明文字。' * 110

    def respond(text):
        return {'reportType': 'lab', 'observations': 'invalid'} if len(graph.calls) == 1 else extract_narrative(text)

    interpreter, graph = interpreter_with(respond)
    trace = MemoryTrace()
    result = await interpreter.analyze(content, trace=trace)
    assert {x['itemName'] for x in result['observations']} == {'项目甲', '项目乙'}
    assert result['processing']['status'] == 'complete'
    assert len(graph.calls) == 2
    assert trace.payloads['analysis_metrics']['outputRecoveryCalls'] == 1


@pytest.mark.asyncio
async def test_failed_slice_does_not_cancel_a_slower_successful_sibling():
    events = []

    async def respond(text):
        if '项目乙' in text:
            await asyncio.sleep(0.02)
            events.append('completed')
            return extract_narrative(text)
        return 'invalid'

    interpreter, graph = interpreter_with(respond)
    interpreter._count_input_tokens = len
    interpreter.MAX_ANALYZE_INPUT_TOKENS = 500
    content = '本次检测结果：项目甲=1。' + '说明。' * 95 + '\n'
    content += '本次检测结果：项目乙=2。' + '说明。' * 95
    result = await interpreter.analyze(content)
    assert events == ['completed']
    assert result['processing']['status'] == 'partial'
    assert {x['itemName'] for x in result['observations']} == {'项目乙'}
    assert len(graph.calls) == 2


@pytest.mark.asyncio
async def test_resume_reuses_successful_slices_and_retries_only_missing_source():
    recovering = False

    def respond(text):
        if not recovering and '项目乙' in text:
            return 'invalid'
        return extract_narrative(text)

    interpreter, graph = interpreter_with(respond)
    interpreter._count_input_tokens = len
    interpreter.MAX_ANALYZE_INPUT_TOKENS = 500
    content = '本次检测结果：项目甲=1。' + '说明。' * 95 + '\n'
    content += '本次检测结果：项目乙=2。' + '说明。' * 95
    trace = MemoryTrace()
    first = await interpreter.analyze(content, trace=trace)
    assert first['processing']['status'] == 'partial'
    count = len(graph.calls)
    recovering = True
    second = await interpreter.analyze(content, trace=trace)
    assert second['processing']['status'] == 'complete'
    assert len(graph.calls) == count + 1
    assert '项目甲' not in graph.calls[-1]['input']['report_text']
    assert {x['itemName'] for x in second['observations']} == {'项目甲', '项目乙'}


@pytest.mark.asyncio
async def test_document_deadline_returns_completed_facts_and_can_resume():
    recovering = False
    cancelled = asyncio.Event()

    async def respond(text):
        if '项目乙' in text and not recovering:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return extract_narrative(text)

    interpreter, graph = interpreter_with(respond)
    interpreter._count_input_tokens = len
    interpreter.MAX_ANALYZE_INPUT_TOKENS = 500
    interpreter.ANALYSIS_TIMEOUT_SECONDS = 0.04
    content = '本次检测结果：项目甲=1。' + '说明。' * 95 + '\n'
    content += '本次检测结果：项目乙=2。' + '说明。' * 95
    trace = MemoryTrace()
    first = await interpreter.analyze(content, trace=trace)
    assert cancelled.is_set()
    assert first['processing']['status'] == 'partial'
    assert {x['itemName'] for x in first['observations']} == {'项目甲'}
    recovering = True
    interpreter.ANALYSIS_TIMEOUT_SECONDS = 1
    second = await interpreter.analyze(content, trace=trace)
    assert second['processing']['status'] == 'complete'
    assert len(graph.calls) == 3


@pytest.mark.asyncio
async def test_model_call_timeout_does_not_split_or_repeat_transport_failure():
    async def respond(_):
        await asyncio.Event().wait()

    interpreter, graph = interpreter_with(respond)
    interpreter.CALL_TIMEOUT_SECONDS = 0.01
    trace = MemoryTrace()
    result = await interpreter.analyze('本次检测结果：项目甲=1。', trace=trace)
    assert result['processing']['status'] == 'unavailable'
    assert len(graph.calls) == 1
    assert trace.payloads['analysis_output']['errorType'] == 'TimeoutError'
    assert trace.payloads['analysis_part_00000']['result']['processing']['issues']
    assert trace.payloads['analysis_metrics']['usageMetricsKnown'] is False


@pytest.mark.asyncio
async def test_json_diagnostics_preserve_original_offsets_and_model_identity(analysis_logs):
    reply = ' \n```json\n{\n    "observations": ?\n}\n```\n'
    interpreter, graph = interpreter_with(lambda _: reply)
    trace = MemoryTrace()
    result = await interpreter.analyze('无可用结果', trace=trace, session_id='test-session')
    assert result['processing']['status'] == 'unavailable'
    output = trace.payloads['analysis_output']
    original_input = trace.payloads['analysis_input']
    assert output['jsonParse']['position'] == reply.index('?')
    assert output['jsonParse']['line'] == 4
    assert output['jsonParse']['column'] == 21
    for key in ('sessionId', 'requestId', 'runId', 'chunkId', 'source'):
        assert output[key] == original_input[key]
    assert len(graph.calls) == 1
    assert all(not record.exc_info for record in analysis_logs.records)


@pytest.mark.asyncio
async def test_old_interpreter_checkpoint_is_not_reused():
    interpreter, graph = interpreter_with(extract_narrative)
    trace = MemoryTrace()
    content = '本次检测结果：项目甲=1。'
    first = await interpreter.analyze(content, trace=trace)
    assert first['processing']['status'] == 'complete'
    trace.payloads['analysis_part_00000']['identity']['version'] = 'analysis-health-report-v5'
    second = await interpreter.analyze(content, trace=trace)
    assert second['processing']['status'] == 'complete'
    assert len(graph.calls) == 2


@pytest.mark.asyncio
async def test_deadline_does_not_reuse_a_checkpoint_from_another_type_hint():
    wait = False

    async def respond(text):
        if wait:
            await asyncio.Event().wait()
        return extract_narrative(text)

    interpreter, _ = interpreter_with(respond)
    trace = MemoryTrace()
    content = '本次检测结果：项目甲=1。'
    first = await interpreter.analyze(content, trace=trace, report_type_hint='lab')
    assert first['processing']['status'] == 'complete'
    wait = True
    interpreter.ANALYSIS_TIMEOUT_SECONDS = 0.03
    second = await interpreter.analyze(content, trace=trace, report_type_hint='microbiome')
    assert second['processing']['status'] == 'unavailable'
    assert second['observations'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('initial_elapsed_ms', 'recovery_elapsed_ms'),
    [(0, 15), (10, 15), (15, 0)],
    ids=['initial-clock-unchanged', 'both-calls-timed', 'recovery-clock-unchanged'],
)
async def test_metrics_include_tokens_and_actual_recovery_input(monkeypatch, initial_elapsed_ms, recovery_elapsed_ms):
    measured_ms = 0
    measured_loop = SimpleNamespace(time=lambda: measured_ms / 1000)

    class MeasuredAsyncio:
        def get_running_loop(self):
            return measured_loop

        def __getattr__(self, name):
            return getattr(asyncio, name)

    # Control only the service's measurements; scheduling and timeouts use the real loop.
    monkeypatch.setattr(analysis_service, 'asyncio', MeasuredAsyncio())
    call_durations = iter((initial_elapsed_ms, recovery_elapsed_ms))
    interpreter, graph = interpreter_with(lambda _: {'reportType': 'lab', 'observations': 'invalid'})
    invoke = graph.ainvoke

    async def with_usage(*args, **kwargs):
        nonlocal measured_ms

        output = await invoke(*args, **kwargs)
        measured_ms += next(call_durations)
        output['usage'] = {'promptTokens': 100, 'completionTokens': 10, 'totalTokens': 110}
        return output

    graph.ainvoke = with_usage
    trace = MemoryTrace()
    await interpreter.analyze('本次检测结果：项目甲=1。', trace=trace)
    metrics = trace.payloads['analysis_metrics']
    assert metrics['promptTokens'] == 200
    assert metrics['completionTokens'] == 20
    assert metrics['totalTokens'] == 220
    assert metrics['usageMetricsKnown'] is True
    assert len(graph.calls) == 2
    assert metrics['outputRecoveryCalls'] == 1
    assert metrics['recoveryElapsedMs'] == recovery_elapsed_ms
    assert metrics['modelElapsedMs'] == initial_elapsed_ms + recovery_elapsed_ms
    assert metrics['queueWaitMs'] >= metrics['recoveryQueueWaitMs']
    assert metrics['inputChars'] == sum(len(call['input']['report_text']) for call in graph.calls)


@pytest.mark.asyncio
async def test_required_archive_failure_is_never_downgraded_to_partial():
    interpreter, _ = interpreter_with(extract_narrative)
    trace = MemoryTrace()
    original = trace.write_payload

    async def write(name, value):
        if name == 'analysis_output':
            raise RuntimeError('required archive failed')
        await original(name, value)

    trace.write_payload = write
    with pytest.raises(RuntimeError, match='required archive failed'):
        await interpreter.analyze('本次检测结果：项目甲=1。', trace=trace)


@pytest.mark.asyncio
async def test_no_more_than_four_slices_are_active_and_input_budget_includes_repair():
    active = maximum = 0

    async def respond(text):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.001)
            return 'invalid'
        finally:
            active -= 1

    interpreter, graph = interpreter_with(respond)
    interpreter._count_input_tokens = len
    interpreter.MAX_ANALYZE_INPUT_TOKENS = 500
    await interpreter.analyze(('说明。' * 100 + '\n') * 10)
    assert maximum <= 4
    assert all(len(call['input']['report_text']) <= 500 for call in graph.calls)
