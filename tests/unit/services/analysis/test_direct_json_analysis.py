"""The report extractor sends raw text to the LLM and validates only its JSON contract."""

from copy import deepcopy

import pytest

from modules.analysis.application.analysis_validation import validate_analysis
from modules.analysis.application.input_preparation import prepare_input
from tests.unit.services.analysis.test_analysis_json import MemoryTrace
from tests.unit.services.analysis.test_analysis_json import interpreter_with


def report(value='0', **fields):
    return {
        'reportType': 'lab',
        'status': 'pending',
        'observations': [{'itemName': '指标甲', 'resultValue': value, 'unit': None, 'observedAt': None}],
        **fields,
    }


class ForbiddenSource:
    def __getattr__(self, name):
        raise AssertionError('source metadata must not be inspected')

    def __iter__(self):
        raise AssertionError('source metadata must not be inspected')

    def get(self, *args):
        raise AssertionError('source metadata must not be inspected')


@pytest.mark.asyncio
async def test_the_exact_docmind_text_is_the_first_model_input_without_source_annotations():
    text = '陌生医院\n任意列名 | 实际读数 | 计量\n指标甲 | 0 | 未注明\n'
    interpreter, graph = interpreter_with(lambda _: report())
    trace = MemoryTrace()
    trace.parsed_document = ForbiddenSource()
    result = await interpreter.analyze(text, docmind=ForbiddenSource(), trace=trace)
    assert graph.calls[0]['input']['report_text'] == text
    assert len(graph.calls) == 1
    assert result['processing']['status'] == 'complete'
    assert result['processing']['issues'] == []
    assert result['observations'][0]['resultValue'] == '0'
    assert trace.payloads['analysis_metrics']['processingMode'] == 'llm_json'
    assert trace.payloads['analysis_metrics']['validationMode'] == 'json_schema'
    assert trace.payloads['analysis_metrics']['outputRecoveryCalls'] == 0


@pytest.mark.asyncio
async def test_a_report_longer_than_the_old_eight_thousand_character_limit_is_sent_whole():
    text = '原文不需要来源协议或布局标记。\n' * 1400
    assert 8000 < len(text) < 32000
    interpreter, graph = interpreter_with(lambda _: report())
    result = await interpreter.analyze(text)
    assert len(graph.calls) == 1
    assert graph.calls[0]['input']['report_text'] == text
    assert result['processing']['status'] == 'complete'


@pytest.mark.parametrize('value', ['0', 0, '<0.001', '≥1.20E-3', '阴性', '未检出', '12.340%'])
@pytest.mark.asyncio
async def test_schema_valid_field_values_need_no_table_header_quote_or_source_id(value):
    interpreter, graph = interpreter_with(lambda _: report(value))
    result = await interpreter.analyze('任意文档原文：指标甲的实际读数，排版由模型理解。')
    assert len(graph.calls) == 1
    assert result['processing']['status'] == 'complete'
    assert result['observations'][0]['resultValue'] == value
    assert result['observations'][0]['unit'] is None
    assert not result['processing']['issues']


@pytest.mark.asyncio
async def test_input_slices_cover_every_original_character_without_layout_transformation():
    text = '原始正文与表格\t一行文字。\n' * 100
    prepared = prepare_input(text, docmind=ForbiddenSource(), token_limit=180, count_tokens=len)
    assert prepared.text == text
    assert ''.join(part.text for part in prepared.chunks) == text
    assert all(prepared.model_text(part) == part.text for part in prepared.chunks)
    assert all(not part.records and not part.units for part in prepared.chunks)
    interpreter, graph = interpreter_with(lambda _: report())
    interpreter._count_input_tokens = len
    interpreter.MAX_ANALYZE_INPUT_TOKENS = 180
    result = await interpreter.analyze(text, docmind=ForbiddenSource())
    assert len(graph.calls) == len(prepared.chunks)
    assert ''.join(call['input']['report_text'] for call in graph.calls) == text
    assert result['processing']['status'] == 'complete'


@pytest.mark.asyncio
async def test_one_schema_correction_succeeds_without_source_repairs_or_a_split_tree():
    responses = [report(), report('<0.01')]
    responses[0]['observations'] = 'invalid-array'
    interpreter, graph = interpreter_with(lambda _: responses.pop(0))
    text = '报告原文，模型直接输出字段。'
    result = await interpreter.analyze(text)
    assert len(graph.calls) == 2
    assert graph.calls[0]['input']['report_text'] == text
    assert graph.calls[1]['input']['report_text'].endswith(text)
    assert result['processing']['status'] == 'complete'
    assert result['observations'][0]['resultValue'] == '<0.01'


@pytest.mark.asyncio
async def test_invalid_json_schema_is_bounded_to_two_calls_and_never_published_as_complete():
    interpreter, graph = interpreter_with(lambda _: {'reportType': 'lab', 'observations': [{}]})
    result = await interpreter.analyze('原文。' * 250)
    assert len(graph.calls) == 2
    assert result['processing']['status'] == 'unavailable'
    assert result['processing']['failureKind'] == 'content'
    assert all(issue['code'] == 'invalid_json_schema' for issue in result['processing']['issues'])


@pytest.mark.asyncio
async def test_valid_empty_json_is_not_retried_to_invent_results():
    interpreter, graph = interpreter_with(lambda _: {'reportType': 'unknown', 'observations': []})
    result = await interpreter.analyze('原文没有实际检测结果。')
    assert len(graph.calls) == 1 and not result['observations']
    assert result['processing']['retryable'] is False


@pytest.mark.asyncio
async def test_model_status_is_preserved_instead_of_recomputed_by_keyword_rules():
    interpreter, _ = interpreter_with(lambda _: report(status='abnormal'))
    result = await interpreter.analyze('原文整体结论异常，具体项目没有高低标记。')
    assert result['status'] == 'abnormal'
    assert result['processing']['status'] == 'complete'


def test_validator_does_not_read_legacy_source_arguments_or_require_physical_evidence():
    result = validate_analysis(
        report(), records=ForbiddenSource(), units=ForbiddenSource(), direct_observations=ForbiddenSource()
    )
    assert result.valid and result.accepted_count == 1


@pytest.mark.parametrize('change', ['array', 'name', 'value', 'bool', 'kind', 'date_type', 'sections'])
def test_schema_rejects_invalid_field_types_and_enums(change):
    data = report()
    if change == 'array':
        data['observations'] = {}
    elif change == 'name':
        data['observations'][0]['itemName'] = []
    elif change == 'value':
        data['observations'][0]['resultValue'] = []
    elif change == 'bool':
        data['observations'][0]['resultValue'] = False
    elif change == 'kind':
        data['observations'][0]['kind'] = 'not-a-kind'
    elif change == 'date_type':
        data['reportTime'] = 20261030
    else:
        data['sections'] = 'not-an-array'
    checked = validate_analysis(data)
    assert not checked.valid
    assert all(issue['code'] == 'invalid_json_schema' and issue['path'].startswith('$') for issue in checked.issues)


def test_unknown_source_metadata_is_not_a_contract_field_and_cannot_break_valid_json():
    data = report()
    data['observations'][0]['evidence'] = {'sourceId': 'does-not-exist', 'quote': 'unrelated'}
    checked = validate_analysis(data)
    assert checked.valid
    assert 'evidence' not in checked.data['observations'][0]


def test_nested_report_dates_and_imaging_values_are_accepted_as_model_fields():
    data = {
        'reportType': 'checkup',
        'observations': [],
        'sections': [
            report(reportTime='2026-01-01'),
            {
                'reportType': 'imaging',
                'reportTime': '2026-02-02',
                'observations': [],
                'expansion': {'findings': '原文所见', 'diagnosis': '原文结论'},
            },
        ],
    }
    checked = validate_analysis(data)
    assert checked.valid
    assert [part['reportTime'] for part in checked.data['sections']] == ['2026-01-01', '2026-02-02']
    assert checked.data['sections'][1]['expansion']['diagnosis'] == '原文结论'


def test_input_limitations_are_model_fields_and_do_not_fail_json_validation():
    data = report(unresolvedItems=[{'itemName': '指标乙', 'resultValue': None, 'reason': '原文数值为空'}])
    checked = validate_analysis(data)
    assert checked.valid and checked.issues == []
    assert checked.data['unresolvedItems'][0]['missingFields'] == ['resultValue']


@pytest.mark.asyncio
async def test_successful_json_checkpoint_is_reused_and_older_versions_are_not():
    interpreter, graph = interpreter_with(lambda _: report())
    trace = MemoryTrace()
    await interpreter.analyze('报告原文', trace=trace)
    await interpreter.analyze('报告原文', trace=trace)
    assert len(graph.calls) == 1
    trace.payloads['analysis_part_00000']['identity']['version'] = 'analysis-health-report-v10'
    await interpreter.analyze('报告原文', trace=trace)
    assert len(graph.calls) == 2


def test_validator_does_not_mutate_the_raw_model_json_archive():
    data = report()
    raw = deepcopy(data)
    validate_analysis(data)
    assert data == raw


def test_nonfinite_numbers_are_not_usable_json_values():
    assert not validate_analysis(report(float('inf'))).valid
    assert not validate_analysis(report(float('nan'))).valid
    assert validate_analysis(report(10**1000)).valid
