"""Valid pending evidence survives cross-part and optional demographic merging."""

from copy import deepcopy

import pytest

from modules.analysis.application.basic_info import empty_basic_info
from modules.analysis.application.basic_info import merge_basic_check
from modules.analysis.application.basic_info import merge_basic_info
from modules.analysis.application.basic_info import validate_basic_info
from tests.unit._support.current_document import report
from tests.unit.services.analysis.test_analysis_json import MemoryTrace
from tests.unit.services.analysis.test_basic_info_current import normalized
from tests.unit.services.analysis.test_basic_info_current import setup

TEXT = (
    '受检者：P1\n姓名：张三\n姓名：李四\n姓名：王五\n性别：女\n性别：男\n年龄：42岁\n年龄：43岁\n年龄：44岁\n指标：3\n'
)


def selected(field='name', value='张三', *, reliability='medium', subject='P1', quote=None):
    values, metadata = empty_basic_info(status='not_found', reason='原文未发现该字段。')
    values[field] = value
    labels = {'name': '姓名', 'gender': '性别', 'age': '年龄'}
    metadata[field].update(
        status='extracted',
        sourceReliability=reliability,
        reason='原文明确属于同一受检者。',
        evidence={'quote': quote or f'{labels[field]}：{value}' + ('岁' if field == 'age' else '')},
        subjectStatus='single',
        subjectRef=subject,
        subjectEvidence={'quote': f'受检者：{subject}'},
    )
    return {
        'reportType': 'lab',
        'status': 'pending',
        'observations': [{'itemName': '指标', 'resultValue': '3'}],
        'basicInfo': values,
        'basicInfoMeta': metadata,
    }


def pending(field='name', values=('李四', '王五'), *, status='conflicting', subject='P1', reliability='low'):
    raw = selected(field, values[0], subject=subject)
    raw['basicInfo'][field] = None
    raw['basicInfoMeta'][field].update(
        status=status,
        sourceReliability=None,
        evidence=None,
        candidates=[
            {
                'value': value,
                'evidence': {'quote': str(value)},
                'sourceReliability': reliability,
                'reason': '同一受检者的待核实原文。',
                'subjectRef': subject,
            }
            for value in values
        ],
    )
    return raw


def merge(method, first, second):
    return merge_basic_info([first, second], {}) if method == 'parts' else merge_basic_check(first, second)


@pytest.mark.parametrize('method', ['parts', 'check'])
@pytest.mark.parametrize(
    'field,value,alternatives',
    [('name', '张三', ('李四', '王五')), ('gender', '女', ('男',)), ('age', 42, (43, 44))],
)
def test_valid_pending_values_conflict_with_a_selected_value_in_either_order(method, field, value, alternatives):
    first = normalized(selected(field, value), TEXT)
    second = normalized(pending(field, alternatives), TEXT)
    before = deepcopy((first, second))
    for left, right in ((first, second), (second, first)):
        result = merge(method, left, right)
        assert result['basicInfo'][field] is None
        metadata = result['basicInfoMeta'][field]
        assert metadata['status'] == 'conflicting'
        assert metadata['subjectStatus'] == 'single'
        assert metadata['subjectRef'] == 'P1'
        assert {row['value'] for row in metadata['candidates']} == {value, *alternatives}
        for row in metadata['candidates']:
            proof = row['evidence']
            assert TEXT[proof['start'] : proof['end']] == proof['quote']
            assert row['sourceReliability'] == ('medium' if row['value'] == value else 'low')
            assert row['subjectRef'] == 'P1'
    assert (first, second) == before


@pytest.mark.parametrize('method', ['parts', 'check'])
def test_pending_same_value_does_not_upgrade_the_selected_ai_grade(method):
    first = normalized(selected(reliability='low'), TEXT)
    second = normalized(pending(values=('张三',), status='unresolved', reliability='medium'), TEXT)
    result = merge(method, first, second)
    assert result['basicInfo']['name'] == '张三'
    assert result['basicInfoMeta']['name']['status'] == 'extracted'
    assert result['basicInfoMeta']['name']['sourceReliability'] == 'low'


@pytest.mark.parametrize('method', ['parts', 'check'])
def test_candidate_only_evidence_is_never_promoted_to_a_selected_value(method):
    first = normalized(pending(values=('李四',), status='unresolved'), TEXT)
    second = normalized(pending(values=('李四',), status='unresolved', reliability='medium'), TEXT)
    result = merge(method, first, second)
    assert result['basicInfo']['name'] is None
    assert result['basicInfoMeta']['name']['status'] == 'unresolved'
    assert result['basicInfoMeta']['name']['sourceReliability'] is None


@pytest.mark.parametrize('method', ['parts', 'check'])
def test_pending_only_parts_retain_every_distinct_supported_candidate(method):
    first = normalized(pending(values=('李四',), status='unresolved'), TEXT)
    second = normalized(pending(values=('王五',), status='unresolved'), TEXT)
    result = merge(method, first, second)
    assert result['basicInfo']['name'] is None
    assert result['basicInfoMeta']['name']['status'] == 'conflicting'
    assert {row['value'] for row in result['basicInfoMeta']['name']['candidates']} == {'李四', '王五'}


@pytest.mark.parametrize('method', ['parts', 'check'])
@pytest.mark.parametrize('invalid', ['missing_proof', 'wrong_quote', 'bad_grade', 'invalid_value'])
def test_rejected_pending_rows_cannot_create_a_false_conflict(method, invalid):
    first = normalized(selected(), TEXT)
    raw = pending()
    candidate = raw['basicInfoMeta']['name']['candidates'][0]
    candidate.update(
        {
            'missing_proof': {'evidence': None},
            'wrong_quote': {'evidence': {'quote': '姓名：张三'}},
            'bad_grade': {'sourceReliability': 'very_high'},
            'invalid_value': {'value': {}},
        }[invalid]
    )
    raw['basicInfoMeta']['name']['candidates'] = [candidate]
    second = normalized(raw, TEXT)
    assert second['basicInfoMeta']['name']['candidates'] == []
    result = merge(method, first, second)
    assert result['basicInfo']['name'] == '张三'
    assert result['basicInfoMeta']['name']['status'] == 'extracted'


@pytest.mark.parametrize('method', ['parts', 'check'])
def test_candidate_only_other_patient_still_blocks_cross_patient_splicing(method):
    text = TEXT + '受检者：P2\n'
    first = normalized(report(name='张三', subject='P1'), text)
    second = normalized(pending(subject='P2'), text)
    result = merge(method, first, second)
    assert all(value is None for value in result['basicInfo'].values())
    assert all(meta['status'] == 'conflicting' for meta in result['basicInfoMeta'].values())
    assert all(meta['subjectStatus'] == 'ambiguous' for meta in result['basicInfoMeta'].values())


@pytest.mark.parametrize('method', ['parts', 'check'])
def test_equivalent_numeric_ages_keep_the_stronger_extracted_ai_grade(method):
    text = TEXT + '复核年龄：42.0岁\n'
    first = normalized(selected('age', 42, reliability='low'), text)
    second = normalized(selected('age', 42.0, quote='复核年龄：42.0岁'), text)
    result = merge(method, first, second)
    assert result['basicInfo']['age'] == 42
    assert result['basicInfoMeta']['age']['status'] == 'extracted'
    assert result['basicInfoMeta']['age']['sourceReliability'] == 'medium'


def test_normalization_preserves_the_supported_selected_value_when_it_becomes_conflicting():
    raw = selected('age', 42)
    raw['basicInfoMeta']['age']['candidates'] = pending('age', (43,))['basicInfoMeta']['age']['candidates']
    values, metadata, issues = validate_basic_info(raw, text=TEXT)
    assert not issues and values['age'] is None
    assert {row['value'] for row in metadata['age']['candidates']} == {42, 43}
    first = {**raw, 'basicInfo': values, 'basicInfoMeta': metadata}
    later = normalized(selected('age', 43), TEXT)
    result = merge_basic_info([first, later], {})
    assert result['basicInfo']['age'] is None
    assert result['basicInfoMeta']['age']['status'] == 'conflicting'
    assert {row['value'] for row in result['basicInfoMeta']['age']['candidates']} == {42, 43}


@pytest.mark.asyncio
async def test_optional_check_keeps_conflicting_candidates_and_the_first_clinical_result_on_resume():
    def respond(prompt):
        if '第二阶段' not in prompt:
            return report(name='张三', age=None, subject='P1')
        raw = report(name=None, subject='P1')
        raw['basicInfoMeta']['name'] = pending()['basicInfoMeta']['name']
        raw['observations'] = [{'itemName': '不得替换', 'resultValue': '999'}]
        return raw

    current, _, graph = setup(respond)
    trace = MemoryTrace()
    first = await current.analyze(TEXT, trace=trace)
    assert first['basicInfo']['name'] is None
    assert first['basicInfoMeta']['name']['status'] == 'conflicting'
    assert {row['value'] for row in first['basicInfoMeta']['name']['candidates']} == {'张三', '李四', '王五'}
    assert first['basicInfo']['age'] == 42
    assert first['observations'][0]['itemName'] == '指标'
    assert first['processing']['status'] == 'complete'
    assert trace.payloads['analysis_part_00000']['firstPhaseCalls'] == 1
    assert trace.payloads['analysis_part_00000_0']['done']
    second = await current.analyze(TEXT, trace=trace)
    assert second['basicInfo'] == first['basicInfo'] and second['basicInfoMeta'] == first['basicInfoMeta']
    assert second['observations'] == first['observations']
    assert len(graph.calls) == 2


@pytest.mark.asyncio
async def test_real_partitioning_retains_pending_conflicts_from_the_other_first_pass():
    first_text = '受检者：P1\n姓名：张三\n性别：女\n年龄：42岁\n指标甲：3\n'
    second_text = '受检者：P1\n姓名：李四\n姓名：王五\n性别：女\n年龄：42岁\n指标乙：4\n'
    width = max(len(first_text), len(second_text)) + 2
    text = first_text.ljust(width - 2) + '\n\n' + second_text.ljust(width - 2) + '\n\n'

    def respond(prompt):
        if '指标甲' in prompt:
            raw = report(name='张三', subject='P1')
            raw['observations'] = [{'itemName': '指标甲', 'resultValue': '3'}]
        else:
            raw = report(name=None, subject='P1')
            raw['basicInfoMeta']['name'] = pending()['basicInfoMeta']['name']
            raw['observations'] = [{'itemName': '指标乙', 'resultValue': '4'}]
        return raw

    current, _, graph = setup(respond)
    current.MAX_ANALYZE_INPUT_TOKENS = width
    trace = MemoryTrace()
    first = await current.analyze(text, trace=trace)
    assert first['basicInfo']['name'] is None
    assert first['basicInfoMeta']['name']['status'] == 'conflicting'
    assert {row['value'] for row in first['basicInfoMeta']['name']['candidates']} == {'张三', '李四', '王五'}
    assert {(row['itemName'], row['resultValue']) for row in first['observations']} == {
        ('指标甲', '3'),
        ('指标乙', '4'),
    }
    for candidate in first['basicInfoMeta']['name']['candidates']:
        proof = candidate['evidence']
        assert text[proof['start'] : proof['end']] == proof['quote']
    assert first['processing']['status'] == 'complete'
    assert len(graph.calls) == 3
    assert trace.payloads['analysis_part_00000']['firstPhaseCalls'] == 1
    assert trace.payloads['analysis_part_00001']['firstPhaseCalls'] == 1
    assert not any('_repair' in key for key in trace.payloads)
    second = await current.analyze(text, trace=trace)
    assert second['basicInfoMeta'] == first['basicInfoMeta']
    assert second['observations'] == first['observations']
    assert len(graph.calls) == 3


def test_same_source_check_keeps_single_patient_fields_without_inventing_a_cross_part_anchor():
    first = report(name='张三', age=None, subject='P1')
    second = report(name=None, subject='P1')
    for raw in (first, second):
        for meta in raw['basicInfoMeta'].values():
            meta['subjectRef'] = None
            meta['subjectEvidence'] = None
    first, second = normalized(first, TEXT), normalized(second, TEXT)
    checked = merge_basic_check(first, second)
    assert checked['basicInfo'] == {'name': '张三', 'gender': '女', 'age': 42}
    assert all(value is None for value in merge_basic_info([first, second], {})['basicInfo'].values())


@pytest.mark.parametrize('invalid', ['wrong_quote', 'bad_grade'])
def test_rejected_typed_value_is_not_reintroduced_as_a_candidate(invalid):
    raw = selected('age', 42)
    raw['basicInfoMeta']['age']['candidates'] = pending('age', (43,))['basicInfoMeta']['age']['candidates']
    raw['basicInfoMeta']['age'].update(
        {'evidence': {'quote': '年龄：44岁'}} if invalid == 'wrong_quote' else {'sourceReliability': 'very_high'}
    )
    values, metadata, _ = validate_basic_info(raw, text=TEXT)
    assert values['age'] is None
    assert metadata['age']['status'] == 'unresolved'
    assert {row['value'] for row in metadata['age']['candidates']} == {43}
    result = merge_basic_info(
        [{**raw, 'basicInfo': values, 'basicInfoMeta': metadata}, normalized(selected('age', 43), TEXT)], {}
    )
    assert result['basicInfo']['age'] == 43


@pytest.mark.parametrize('invalid', [False, True])
def test_boolean_pending_value_is_not_equal_to_a_valid_zero_or_one_year_age(invalid):
    age = int(invalid)
    text = TEXT + f'年龄：{age}岁\n'
    first = normalized(selected('age', age), text)
    second = normalized(pending('age', (invalid,)), text)
    assert second['basicInfoMeta']['age']['candidates'] == []
    assert merge_basic_info([first, second], {})['basicInfo']['age'] == age


@pytest.mark.parametrize('method', ['parts', 'check'])
def test_explicit_ambiguous_patient_signal_blocks_selection_even_without_field_candidates(method):
    first = normalized(report(name='张三', subject='P1'), TEXT)
    raw = report(name=None, gender=None, age=None, subject='P1')
    for meta in raw['basicInfoMeta'].values():
        meta.update(subjectStatus='ambiguous', reason='该片段存在多个受检者，无法确定归属。')
    second = normalized(raw, TEXT)
    assert not any(second['basicInfo'].values())
    assert all(not meta['candidates'] for meta in second['basicInfoMeta'].values())
    for left, right in ((first, second), (second, first)):
        result = merge(method, left, right)
        assert all(value is None for value in result['basicInfo'].values())
        assert all(meta['status'] == 'conflicting' for meta in result['basicInfoMeta'].values())
        assert all(meta['subjectStatus'] == 'ambiguous' for meta in result['basicInfoMeta'].values())
        assert {row['value'] for row in result['basicInfoMeta']['name']['candidates']} == {'张三'}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['optional', 'parts'])
async def test_real_paths_keep_empty_ambiguous_patient_judgments_and_clinical_results(mode):
    def ambiguous():
        raw = report(name=None, gender=None, age=None, subject='P1')
        raw.update(reportType='unknown', observations=[])
        for meta in raw['basicInfoMeta'].values():
            meta.update(subjectStatus='ambiguous', reason='该片段存在多个受检者，无法确定归属。')
        return raw

    def respond(prompt):
        if '第二阶段' in prompt:
            return ambiguous()
        if mode == 'parts' and '指标乙' in prompt:
            raw = ambiguous()
            raw.update(reportType='lab', observations=[{'itemName': '指标乙', 'resultValue': '4'}])
            return raw
        return report(name='张三', age=None if mode == 'optional' else 42, subject='P1')

    current, _, graph = setup(respond)
    text = TEXT
    if mode == 'parts':
        other = '受检者：P1\n受检者：P2\n指标乙：4\n'
        width = max(len(TEXT), len(other)) + 2
        text = TEXT.ljust(width - 2) + '\n\n' + other.ljust(width - 2) + '\n\n'
        current.MAX_ANALYZE_INPUT_TOKENS = width
    trace = MemoryTrace()
    first = await current.analyze(text, trace=trace)
    assert all(value is None for value in first['basicInfo'].values())
    assert all(meta['subjectStatus'] == 'ambiguous' for meta in first['basicInfoMeta'].values())
    assert first['processing']['status'] == 'complete'
    expected = {('指标', '3')} | ({('指标乙', '4')} if mode == 'parts' else set())
    assert {(row['itemName'], row['resultValue']) for row in first['observations']} == expected
    assert len(graph.calls) == (3 if mode == 'parts' else 2)
    second = await current.analyze(text, trace=trace)
    assert second['basicInfoMeta'] == first['basicInfoMeta']
    assert second['observations'] == first['observations']
    assert len(graph.calls) == (3 if mode == 'parts' else 2)
