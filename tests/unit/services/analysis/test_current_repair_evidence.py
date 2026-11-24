"""Current repair keeps clinical candidates and demographic proofs independently."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from modules.analysis.application.current_interpreter import CurrentHealthReportInterpreter
from tests.unit._support.current_document import report
from tests.unit.services.analysis.test_analysis_json import MemoryTrace
from tests.unit.services.analysis.test_basic_info_current import TEXT
from tests.unit.services.analysis.test_basic_info_current import setup

CONFLICT_TEXT = '受检者：P1\n姓名：张三\n姓名：李四\n姓名：王五\n性别：女\n年龄：42岁\n年龄：43岁\n'


def is_repair(text):
    return '修复' in text or '重新输出为完整' in text


def clinical_reply(*, root_count=0, child_counts=(), label='首答', invalid='title'):
    raw = report()
    raw.update(reportType='checkup', observations=[], title=None)
    raw['observations'] = [
        {'itemName': f'{label}根指标{index}', 'resultValue': str(index + 1)} for index in range(root_count)
    ]
    if child_counts:
        raw['sections'] = []
        for child_index, count in enumerate(child_counts):
            child = report()
            child['observations'] = [
                {'itemName': f'{label}子{child_index}指标{index}', 'resultValue': str(index + 1)}
                for index in range(count)
            ]
            raw['sections'].append(child)
    if invalid:
        raw[invalid] = 123
    return raw


@pytest.mark.asyncio
@pytest.mark.parametrize('root_count,child_counts', [(0, (2,)), (1, (2,)), (2, ())])
async def test_invalid_repair_cannot_discard_a_larger_validated_clinical_subtree(root_count, child_counts):
    first = clinical_reply(root_count=root_count, child_counts=child_counts)
    repaired = clinical_reply(label='修正')
    current, _, graph = setup(lambda text: deepcopy(repaired if is_repair(text) else first))
    result = await current.analyze(TEXT, trace=MemoryTrace())
    assert len(result['observations']) == root_count + sum(child_counts)
    assert all(item['itemName'].startswith('首答') for item in result['observations'])
    assert any(issue.get('path') == '$.title' for issue in result['processing']['issues'])
    assert len(graph.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('valid_repair', [False, True])
async def test_repair_selection_preserves_existing_valid_first_and_recursive_count_rules(valid_repair):
    first = clinical_reply(child_counts=(2,))
    repaired = clinical_reply(
        root_count=1, child_counts=() if valid_repair else (2,), label='修正', invalid=None if valid_repair else 'title'
    )
    current, _, graph = setup(lambda text: deepcopy(repaired if is_repair(text) else first))
    result = await current.analyze(TEXT, trace=MemoryTrace())
    assert len(result['observations']) == (1 if valid_repair else 3)
    assert all(item['itemName'].startswith('修正') for item in result['observations'])
    assert bool(result['processing']['issues']) is not valid_repair
    assert len(graph.calls) == 2


@pytest.mark.asyncio
async def test_equal_recursive_counts_still_adopt_the_repair_candidate():
    first = clinical_reply(child_counts=(2,))
    repaired = clinical_reply(root_count=2, label='修正')
    current, _, _ = setup(lambda text: deepcopy(repaired if is_repair(text) else first))
    result = await current.analyze(TEXT)
    assert len(result['observations']) == 2
    assert all(item['itemName'].startswith('修正') for item in result['observations'])


@pytest.mark.asyncio
async def test_kept_clinical_tree_keeps_its_own_unresolved_format_diagnostics():
    first = clinical_reply(child_counts=(2,))
    repaired = clinical_reply(label='修正', invalid='aiSummary')
    current, _, _ = setup(lambda text: deepcopy(repaired if is_repair(text) else first))
    result = await current.analyze(TEXT)
    paths = {issue.get('path') for issue in result['processing']['issues']}
    assert len(result['observations']) == 2
    assert '$.title' in paths
    assert '$.aiSummary' not in paths


@pytest.mark.asyncio
async def test_nonjson_repair_does_not_remove_the_kept_clinical_issue():
    first = clinical_reply(root_count=2)
    current, _, graph = setup(lambda text: 'not json' if is_repair(text) else deepcopy(first))
    result = await current.analyze(TEXT)
    assert len(result['observations']) == 2
    assert any(issue.get('path') == '$.title' for issue in result['processing']['issues'])
    assert len(graph.calls) == 2


def conflicting_reply(text, *, field, nested=False, invalid_evidence=False, pending_same=False):
    first = report(name='张三', subject='P1', reliability='low')
    first.update(
        title=123, observations=[{'itemName': '指标甲', 'resultValue': '3'}, {'itemName': '指标乙', 'resultValue': '4'}]
    )
    if not is_repair(text):
        return first
    changed = report(
        name='李四' if field == 'name' else '张三', age=43 if field == 'age' else 42, subject='P1', reliability='medium'
    )
    changed['observations'] = []
    if invalid_evidence:
        changed['basicInfoMeta'][field]['evidence'] = {'quote': '没有这段字段证据'}
    if pending_same:
        changed['basicInfo'][field] = None
        changed['basicInfoMeta'][field].update(
            status='unresolved',
            sourceReliability=None,
            candidates=[
                {
                    'value': first['basicInfo'][field],
                    'evidence': deepcopy(first['basicInfoMeta'][field]['evidence']),
                    'sourceReliability': 'medium',
                    'reason': '仍需核实',
                    'subjectRef': 'P1',
                }
            ],
        )
    if nested:
        raw = report(name=None, gender=None, age=None)
        raw.update(title=123, observations=[], reportType='checkup', sections=[changed])
        return raw
    changed['title'] = 123
    return changed


@pytest.mark.asyncio
@pytest.mark.parametrize('field,values', [('name', {'张三', '李四'}), ('age', {42, 43})])
@pytest.mark.parametrize('nested', [False, True])
async def test_discarded_clinical_reply_still_contributes_all_its_valid_basic_evidence(field, values, nested):
    current, _, graph = setup(lambda text: conflicting_reply(text, field=field, nested=nested))
    trace = MemoryTrace()
    result = await current.analyze(CONFLICT_TEXT, trace=trace)
    assert len(result['observations']) == 2
    assert result['basicInfo'][field] is None
    meta = result['basicInfoMeta'][field]
    assert meta['status'] == 'conflicting'
    assert {item['value'] for item in meta['candidates']} == values
    assert {item['sourceReliability'] for item in meta['candidates']} == {'low', 'medium'}
    assert all(item['subjectRef'] == 'P1' for item in meta['candidates'])
    assert all(item['evidence']['quote'] in CONFLICT_TEXT for item in meta['candidates'])
    first = trace.payloads['analysis_part_00000']
    assert first['rawData']['basicInfo'][field] is None
    assert first['rawData']['basicInfoMeta'][field]['status'] == 'conflicting'
    assert first['result']['basicInfoMeta'][field]['status'] == 'conflicting'
    assert first['firstPhaseCalls'] == 2
    assert len(graph.calls) == 3
    resumed = CurrentHealthReportInterpreter(
        current._agent_runtime, count_input_tokens=len, docmind_client=current.docmind_client
    )
    again = await resumed.analyze(CONFLICT_TEXT, trace=trace)
    assert again['basicInfoMeta'] == result['basicInfoMeta']
    assert again['observations'] == result['observations']
    assert len(graph.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['name', 'age'])
@pytest.mark.parametrize('pending_same', [False, True])
async def test_discarded_invalid_or_same_pending_evidence_cannot_create_conflict_or_raise_ai_grade(field, pending_same):
    current, _, graph = setup(
        lambda text: conflicting_reply(text, field=field, invalid_evidence=not pending_same, pending_same=pending_same)
    )
    result = await current.analyze(CONFLICT_TEXT, trace=MemoryTrace())
    assert len(result['observations']) == 2
    assert result['basicInfo'][field] == ('张三' if field == 'name' else 42)
    assert result['basicInfoMeta'][field]['status'] == 'extracted'
    assert result['basicInfoMeta'][field]['sourceReliability'] == 'low'
    assert len(graph.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('nested', [False, True])
async def test_discarded_reply_with_only_ambiguous_patient_metadata_still_blocks_basic_selection(nested):
    def respond(text):
        if not is_repair(text):
            return conflicting_reply(text, field='name')
        ambiguous = report(name=None, gender=None, age=None)
        ambiguous['observations'] = []
        for meta in ambiguous['basicInfoMeta'].values():
            meta.update(status='unresolved', subjectStatus='ambiguous', reason='原文有两位受检者')
        if nested:
            raw = report(name=None, gender=None, age=None)
            raw.update(reportType='checkup', title=123, observations=[], sections=[ambiguous])
            return raw
        ambiguous['title'] = 123
        return ambiguous

    current, _, graph = setup(respond)
    result = await current.analyze(CONFLICT_TEXT, trace=MemoryTrace())
    assert len(result['observations']) == 2
    assert all(value is None for value in result['basicInfo'].values())
    assert all(meta['subjectStatus'] == 'ambiguous' for meta in result['basicInfoMeta'].values())
    assert len(graph.calls) == 3


@pytest.mark.asyncio
async def test_interrupted_merged_checkpoint_restores_proofs_without_repeating_used_first_stage_calls():
    class InterruptedTrace(MemoryTrace):
        interrupted = False

        async def write_payload(self, name, value):
            await super().write_payload(name, value)
            if (
                not self.interrupted
                and name == 'analysis_part_00000'
                and value.get('firstPhaseCalls') == 2
                and not value.get('firstPhaseDone')
                and value.get('rawData', {}).get('basicInfoMeta', {}).get('name', {}).get('status') == 'conflicting'
            ):
                self.interrupted = True
                raise OSError('synthetic acknowledgement loss after durable merged checkpoint')

    current, _, graph = setup(lambda text: conflicting_reply(text, field='name', nested=True))
    trace = InterruptedTrace()
    with pytest.raises(OSError, match='acknowledgement loss'):
        await current.analyze(CONFLICT_TEXT, trace=trace)
    assert trace.interrupted
    assert len(graph.calls) == 2
    result = await current.analyze(CONFLICT_TEXT, trace=trace)
    assert result['basicInfo']['name'] is None
    assert {row['value'] for row in result['basicInfoMeta']['name']['candidates']} == {'张三', '李四'}
    assert len(result['observations']) == 2
    assert trace.payloads['analysis_part_00000']['firstPhaseCalls'] == 2
    assert len(graph.calls) == 3
    await current.analyze(CONFLICT_TEXT, trace=trace)
    assert len(graph.calls) == 3


def test_basic_projection_retains_ambiguous_nested_metadata_without_clinical_fields_or_aliases():
    from modules.analysis.application.basic_info import basic_info_projections

    child = report(name=None, gender=None, age=None)
    for meta in child['basicInfoMeta'].values():
        meta.update(subjectStatus='ambiguous', reason='嵌套节点含两位受检者')
    root = report()
    root['sections'] = [{'sections': [child]}]
    projected = basic_info_projections([root])
    assert len(projected) == 2
    assert all(set(node) == {'basicInfo', 'basicInfoMeta'} for node in projected)
    assert projected[1]['basicInfo'] == dict.fromkeys(('name', 'gender', 'age'))
    assert all(meta['subjectStatus'] == 'ambiguous' for meta in projected[1]['basicInfoMeta'].values())
    projected[1]['basicInfoMeta']['name']['reason'] = '修改投影'
    assert child['basicInfoMeta']['name']['reason'] == '嵌套节点含两位受检者'
    assert basic_info_projections([{'basicInfoMeta': {}}]) == [{'basicInfo': {}, 'basicInfoMeta': {}}]


@pytest.mark.asyncio
async def test_current_phase_archive_names_stay_within_the_existing_controlled_trace_contract():
    def respond(text):
        if '第二阶段' in text:
            return report()
        return report(age=None) if is_repair(text) else 'not json'

    current, _, graph = setup(respond)
    trace = MemoryTrace()
    await current.analyze(TEXT, trace=trace)
    for phase, suffix in [('first', '_00000'), ('repair', '_00000_repair'), ('basic_check', '_00000_0')]:
        archived = trace.payloads['analysis_input' + suffix]
        assert archived['phase'] == phase
        assert archived['source']['version'] == current.VERSION
        assert 'analysis_output' + suffix in trace.payloads
    assert trace.payloads['analysis_part_00000']['firstPhaseCalls'] == 2
    assert trace.payloads['analysis_part_00000_0']['done']
    assert len(graph.calls) == 3


@pytest.mark.asyncio
async def test_unknown_current_phase_is_rejected_before_model_or_trace_writes():
    current, _, graph = setup(lambda _: report())
    trace = MemoryTrace()
    with pytest.raises(ValueError, match='phase'):
        await current._call_part(
            SimpleNamespace(text=TEXT),
            '00000',
            {'start': 0},
            phase='unknown',
            user_id='test-user',
            session_id='test-session',
            subject_id='test-subject',
            trace=trace,
            report_type_hint=None,
            metrics=None,
        )
    assert not graph.calls
    assert not trace.payloads


def assert_no_private_basic_marker(value):
    if isinstance(value, dict):
        assert '_basicInfoMerged' not in value
        for child in value.values():
            assert_no_private_basic_marker(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_private_basic_marker(child)


def unanchored_child_reply(*, independent_children=False):
    root = report(name=None, gender=None, age=None)
    root.update(reportType='checkup', observations=[])
    child = report(gender=None, age=None)
    child['basicInfoMeta']['name'].update(subjectRef=None, subjectEvidence=None)
    for field in ('gender', 'age'):
        root['basicInfoMeta'][field]['reason'] = '首页未出现此字段。'
        child['basicInfoMeta'][field]['reason'] = '检查页未出现此字段。'
    root['sections'] = [child]
    if independent_children:
        second = deepcopy(child)
        second['basicInfoMeta']['name']['reason'] = '另一独立报告页，不能确定与前页为同一人。'
        root['sections'].append(second)
    return root


@pytest.mark.asyncio
async def test_one_unanchored_child_is_not_counted_again_as_a_new_derived_root_source():
    raw = unanchored_child_reply()
    current, _, graph = setup(lambda _: deepcopy(raw))
    trace = MemoryTrace()
    result = await current.analyze(TEXT, trace=trace)
    assert result['basicInfo']['name'] == '测试甲'
    assert result['basicInfoMeta']['name']['status'] == 'extracted'
    assert result['basicInfoMeta']['name']['subjectStatus'] == 'single'
    assert result['basicInfoMeta']['name']['subjectRef'] is None
    assert len(result['observations']) == 1
    assert result['sections'][0]['basicInfo']['name'] == '测试甲'
    assert trace.payloads['analysis_part_00000']['result']['basicInfo']['name'] == '测试甲'
    assert trace.payloads['analysis_part_00000']['rawData']['_basicInfoMerged'] is True
    assert trace.payloads['analysis_part_00000']['result']['_basicInfoMerged'] is True
    assert_no_private_basic_marker(result)
    before = len(graph.calls)
    resumed = await current.analyze(TEXT, trace=trace)
    assert resumed['basicInfoMeta'] == result['basicInfoMeta']
    assert len(graph.calls) == before


@pytest.mark.asyncio
async def test_same_supported_name_in_report_sections_does_not_require_repeated_patient_refs():
    raw = unanchored_child_reply(independent_children=True)
    current, _, _ = setup(lambda _: deepcopy(raw))
    result = await current.analyze(TEXT, trace=MemoryTrace())
    assert result['basicInfo']['name'] == '测试甲'
    assert result['basicInfoMeta']['name']['subjectStatus'] == 'single'
    assert result['basicInfoMeta']['name']['status'] == 'extracted'
    assert result['observations']


@pytest.mark.asyncio
async def test_ai_supplied_private_marker_cannot_bypass_real_independent_subject_evidence():
    raw = unanchored_child_reply(independent_children=True)
    raw['_basicInfoMerged'] = True
    for child in raw['sections']:
        child['_basicInfoMerged'] = True
    current, _, _ = setup(lambda _: deepcopy(raw))
    validated = current._validate_reply(raw, text=TEXT, attempt={})
    assert_no_private_basic_marker(validated.data)
    result = await current.analyze(TEXT, trace=MemoryTrace())
    assert result['basicInfo']['name'] == '测试甲'
    assert result['basicInfoMeta']['name']['subjectStatus'] == 'single'
    assert_no_private_basic_marker(result)


@pytest.mark.asyncio
async def test_pure_post_analysis_merges_do_not_create_a_new_private_execution_marker():
    from modules.analysis.application.analysis_outcomes import combine_verified
    from modules.analysis.application.extraction import merge_document_results

    current, _, _ = setup(lambda _: unanchored_child_reply())
    first = await current.analyze(TEXT)
    next_raw = report(name=None, gender=None, age=None)
    next_raw['observations'] = [{'itemName': '补充指标', 'resultValue': '8'}]
    current, _, _ = setup(lambda _: deepcopy(next_raw))
    second = await current.analyze(TEXT)
    assert_no_private_basic_marker(first)
    assert_no_private_basic_marker(second)
    assert_no_private_basic_marker(merge_document_results([deepcopy(first), deepcopy(second)]))
    assert_no_private_basic_marker(combine_verified(deepcopy(first), deepcopy(second)))
