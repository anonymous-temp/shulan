"""Summaries follow the final extraction without an additional model task."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from modules.analysis.application.analysis_display import display_result
from modules.analysis.application.source_facts import SourceFacts
from tests.unit.services.analysis.test_analysis_json import MemoryTrace
from tests.unit.services.analysis.test_analysis_json import interpreter_with


def report(*, summary='尿酸项目标记偏高。', observations=None, **fields):
    return {
        'reportType': 'lab',
        'status': 'abnormal',
        'aiSummary': summary,
        'observations': observations
        if observations is not None
        else [{'itemName': '尿酸', 'resultValue': '468', 'flag': '偏高'}],
        **fields,
    }


@pytest.mark.asyncio
async def test_complete_single_part_keeps_short_model_summary_without_extra_call():
    raw = report()
    interpreter, graph = interpreter_with(lambda _: deepcopy(raw))

    result = await interpreter.analyze('尿酸 468，原报告标记偏高。')

    assert result['aiSummary'] == raw['aiSummary']
    assert result['processing']['status'] == 'complete'
    assert len(graph.calls) == 1


@pytest.mark.asyncio
async def test_partial_filtered_result_discards_stale_summary_and_marks_limited_basis():
    raw = report(
        summary='尿酸和不存在的项目都偏高，整份报告均已核实。',
        observations=[
            {'itemName': '尿酸', 'resultValue': 0, 'flag': '正常'},
            {'itemName': '不存在的项目', 'resultValue': []},
        ],
    )
    interpreter, graph = interpreter_with(lambda _: deepcopy(raw))

    result = await interpreter.analyze('尿酸 0；其他项目原文不清晰。')

    assert result['processing']['status'] == 'partial'
    assert '不存在的项目' not in result['aiSummary']
    assert '1 项' in result['aiSummary'] and '仅依据' in result['aiSummary']
    assert '整份报告均已核实' not in result['aiSummary']
    assert result['observations'][0]['resultValue'] == 0
    assert len(graph.calls) == 2  # Existing schema correction, no summary task.


@pytest.mark.asyncio
async def test_split_result_rebuilds_one_short_summary_from_retained_items():
    def respond(text):
        name = '项目甲' if 'a' in text else '项目乙'
        return report(
            summary='整个报告均正常。' * 20,
            observations=[{'itemName': name, 'resultValue': '阴性', 'flag': '正常'}],
        )

    interpreter, graph = interpreter_with(respond)
    interpreter.MAX_ANALYZE_INPUT_TOKENS = 1024

    result = await interpreter.analyze('a' * 700 + '\n' + 'b' * 700)

    assert len(graph.calls) == 2
    assert len(result['observations']) == 2
    assert result['aiSummary'] == '已提取 2 项报告结果。'
    assert all('整个报告' not in child['aiSummary'] for child in result['sections'])


@pytest.mark.asyncio
async def test_source_reconciliation_never_keeps_summary_for_replaced_value():
    interpreter, graph = interpreter_with(
        lambda _: report(
            reportType='microbiome',
            summary='菌群指数偏高。',
            observations=[{'itemName': '菌群指数', 'resultValue': '20', 'flag': '偏高'}],
        )
    )
    interpreter._read_source_facts = AsyncMock(
        return_value=SourceFacts(
            observations=({'itemName': '菌群指数', 'resultValue': '2', 'flag': '正常'},),
            replaced_names=frozenset({'菌群指数'}),
        )
    )

    result = await interpreter.analyze('菌群指数 2，正常。')

    assert result['observations'][0]['resultValue'] == '2'
    assert '偏高' not in result['aiSummary']
    assert result['aiSummary'] == '已提取 1 项报告结果。'
    assert len(graph.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'fields',
    [
        {},
        {'title': '检测报告', 'unresolvedItems': [{'itemName': '尿酸', 'resultValue': None}]},
    ],
)
async def test_no_usable_results_clear_unsupported_model_summary(fields):
    interpreter, graph = interpreter_with(lambda _: report(observations=[], summary='整份报告正常。', **fields))

    result = await interpreter.analyze('只有标题和待核实内容。')

    assert result['aiSummary'] is None
    assert result['processing']['status'] == 'unavailable'
    assert display_result(result)['processing']['status'] == ('partial' if fields else 'unavailable')
    assert len(graph.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('in_section', [False, True])
async def test_missing_result_keeps_normal_observation_without_claiming_complete_normal_report(in_section):
    section = report(
        status='normal',
        summary='整份报告正常。',
        observations=[{'itemName': '已知项目', 'resultValue': 0, 'flag': '正常'}],
        unresolvedItems=[{'itemName': '缺结果项目', 'resultValue': None}],
    )
    raw = report(reportType='checkup', status='normal', observations=[], sections=[section]) if in_section else section
    interpreter, graph = interpreter_with(lambda _: deepcopy(raw))
    trace = MemoryTrace()

    result = await interpreter.analyze('已知项目 0，缺结果项目未提供。', trace=trace)

    assert result['processing']['status'] == 'partial'
    assert result['status'] == 'pending'
    assert result['observations'][0]['resultValue'] == 0
    assert '仅依据' in result['aiSummary'] and '整份报告正常' not in result['aiSummary']
    assert trace.payloads['analysis_part_00000']['result']['processing']['status'] == 'partial'
    if in_section:
        assert result['sections'][0]['status'] == 'pending'
        assert result['sections'][0]['unresolvedItems'][0]['itemName'] == '缺结果项目'
    else:
        assert result['unresolvedItems'][0]['itemName'] == '缺结果项目'
    assert display_result(result)['processing']['warnings'] == ['部分内容尚未确认，已保留可确认的结果。']
    assert len(graph.calls) == 1


@pytest.mark.asyncio
async def test_missing_result_does_not_hide_an_explicitly_abnormal_retained_result():
    interpreter, graph = interpreter_with(
        lambda _: report(unresolvedItems=[{'itemName': '缺结果项目', 'resultValue': None}])
    )

    result = await interpreter.analyze('尿酸 468，偏高；其他项目未提供结果。')

    assert result['processing']['status'] == 'partial'
    assert result['status'] == 'abnormal'
    assert result['observations'][0]['flag'] == '偏高'
    assert '仅依据' in result['aiSummary']
    assert len(graph.calls) == 1


@pytest.mark.asyncio
async def test_explicit_recovery_does_not_reuse_old_complete_checkpoint_with_unresolved_results():
    raw = report(unresolvedItems=[{'itemName': '补全项目', 'resultValue': None}])
    interpreter, graph = interpreter_with(lambda _: deepcopy(raw))
    trace = MemoryTrace()
    text = '尿酸和补全项目的合成报告。'
    first = await interpreter.analyze(text, trace=trace)
    assert first['processing']['status'] == 'partial'
    # A checkpoint saved before the completeness fix may have this old marker.
    trace.payloads['analysis_part_00000']['result']['processing']['status'] = 'complete'
    raw['unresolvedItems'] = []
    raw['observations'].append({'itemName': '补全项目', 'resultValue': '阴性'})

    second = await interpreter.analyze(text, trace=trace)

    assert second['processing']['status'] == 'complete'
    assert len(second['observations']) == 2 and not second['unresolvedItems']
    assert len(graph.calls) == 2


@pytest.mark.asyncio
async def test_successful_correction_replaces_initial_summary_without_third_model_call():
    outputs = [
        report(summary='被剔除项目异常。', observations=[{'itemName': '被剔除项目', 'resultValue': []}]),
        report(summary='尿酸项目标记偏高。'),
    ]
    interpreter, graph = interpreter_with(lambda _: deepcopy(outputs.pop(0)))

    result = await interpreter.analyze('尿酸 468，偏高。')

    assert result['processing']['status'] == 'complete'
    assert '被剔除项目' not in result['aiSummary']
    assert '1 项' in result['aiSummary']
    assert len(graph.calls) == 2


@pytest.mark.asyncio
async def test_overlong_model_summary_falls_back_to_short_final_result_description():
    interpreter, graph = interpreter_with(lambda _: report(summary='尿酸偏高。' * 100))

    result = await interpreter.analyze('尿酸 468，偏高。')

    assert len(result['aiSummary']) <= 200
    assert '尿酸' in result['aiSummary']
    assert len(graph.calls) == 1
