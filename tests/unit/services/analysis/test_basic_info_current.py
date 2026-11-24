"""Current report demographics, finite phases and fixed current checkpoints."""

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from health_profile.document_source_contracts_v2 import DocumentBasicInfo
from modules.analysis.application.analysis_display import display_result
from modules.analysis.application.basic_info import merge_basic_info
from modules.analysis.application.basic_info import normalize_basic_info_times
from modules.analysis.application.basic_info import validate_basic_info
from modules.analysis.application.current_interpreter import CurrentHealthReportInterpreter
from tests.unit._support.current_document import report
from tests.unit.services.analysis.test_analysis_json import MemoryTrace
from tests.unit.services.analysis.test_analysis_json import interpreter_with

TEXT = '受检者：测试甲\n姓名：测试甲\n性别：女\n年龄：42岁\n检测指标：3\n'


def setup(respond):
    legacy, graph = interpreter_with(respond)
    legacy._agent_runtime.registry.register(
        CurrentHealthReportInterpreter.CAPABILITY,
        graph,
        version=CurrentHealthReportInterpreter.VERSION,
    )
    current = CurrentHealthReportInterpreter(
        legacy._agent_runtime,
        count_input_tokens=len,
        docmind_client=legacy.docmind_client,
    )
    return current, legacy, graph


@pytest.mark.parametrize('reliability', ['low', 'medium'])
def test_ai_single_value_judgment_is_preserved_without_confidence_or_heading_rules(reliability):
    raw = report(reliability=reliability)
    raw['confidence'] = 0.9999
    values, meta, issues = validate_basic_info(raw, text=TEXT)
    assert not issues
    assert values == {'name': '测试甲', 'gender': '女', 'age': 42}
    assert all(item['sourceReliability'] == reliability for item in meta.values())
    for item in meta.values():
        proof = item['evidence']
        assert TEXT[proof['start'] : proof['end']] == proof['quote']
        assert item['candidates'] == []


@pytest.mark.parametrize(
    'observed_at,expected',
    [
        ('2026-09-01', '2026-09-01'),
        ('2026-09-01T09:15:06.123+08:00', '2026-09-01T09:15:06.123+08:00'),
        ('报告时间：2026年9月1日 9：15+0800', '2026-09-01T09:15+08:00'),
        ('2026/9/1T09:15Z', '2026-09-01T09:15+00:00'),
        ('20260228', '2026-02-28'),
        ('20240229', '2024-02-29'),
        ('20260901T091506+0800', '2026-09-01T09:15:06+08:00'),
        ('20260901 09:15:06Z', '2026-09-01T09:15:06+00:00'),
        ('20260901T091500+0860', None),
        ('2026-09-01T09:15:00+08:60', None),
        ('检查时间：2026/9/1 9：15+08：60', None),
        ('2026-09-01T09:15:00-24:00', None),
        ('2026-09-01T09:15+08:00:60', None),
        ('20260901T0915-080060', None),
        ('20260901T0915+0800', '2026-09-01T09:15+08:00'),
        ('2026-09-01T09:15+08:00', '2026-09-01T09:15+08:00'),
        ('20260901T091506.1234567+0800', '2026-09-01T09:15:06.1234567+08:00'),
        ('2026-09-01T09:15:06.1234567+08:00', '2026-09-01T09:15:06.1234567+08:00'),
        ('20260901T091506.000+0800', '2026-09-01T09:15:06.000+08:00'),
        ('20260901T09+08', '2026-09-01T09+08'),
        ('20260901T091506,1234567+080030.12', '2026-09-01T09:15:06.1234567+08:00:30.12'),
        ('20260229', None),
        ('20260901T25:00:00', None),
        ('2026-09-01X09:15:06', None),
        ('20260901X091506', None),
        ('2026-W36-2', None),
        ('01/02/2026', None),
        ('2026-09-01至2026-09-02', None),
        (None, None),
    ],
)
def test_demographic_observation_time_preserves_only_explicit_calendar_time(observed_at, expected):
    raw = report(reliability='low')
    raw['basicInfoMeta']['age']['observedAt'] = observed_at
    values, metadata, issues = validate_basic_info(raw, text=TEXT)
    assert not issues
    assert values['age'] == 42
    age = metadata['age']
    assert age['status'] == 'extracted'
    assert age['sourceReliability'] == 'low'
    assert age['observedAt'] == expected
    proof = age['evidence']
    assert TEXT[proof['start'] : proof['end']] == proof['quote'] == '年龄：42岁'

    subject_proof = age['subjectEvidence']
    assert TEXT[subject_proof['start'] : subject_proof['end']] == subject_proof['quote']
    assert age['subjectRef'] == raw['basicInfoMeta']['age']['subjectRef']
    assert age['reason'] == raw['basicInfoMeta']['age']['reason']
    contribution = DocumentBasicInfo(
        field='age',
        value=values['age'],
        source_reliability=age['sourceReliability'],
        observed_at=age['observedAt'],
        reason=age['reason'],
        evidence=proof,
        subject_status=age['subjectStatus'],
        subject_ref=age['subjectRef'],
        subject_evidence=subject_proof,
        contribution_id='report-age',
        understanding_snapshot_ref='document_snapshot_' + '0' * 64,
        facts_snapshot_ref='document_snapshot_' + '1' * 64,
        interpretation_round=1,
        interpretation_revision='analysis-health-report-v13',
    )
    assert contribution.observed_at == expected


def test_inflight_time_normalization_is_recursive_idempotent_and_demographic_only():
    result = report()
    result['basicInfoMeta']['age']['observedAt'] = '20260901T0915+0800'
    result['observations'][0]['observedAt'] = '20260901T0915+0800'
    result['sections'] = [
        {'sections': [{'basicInfoMeta': {'gender': {'observedAt': '20260901T091500+0860', 'reason': 'sample'}}}]},
        {'basicInfoMeta': {'name': {'status': 'not_found'}}},
    ]
    expected = deepcopy(result)
    expected['basicInfoMeta']['age']['observedAt'] = '2026-09-01T09:15+08:00'
    expected['sections'][0]['sections'][0]['basicInfoMeta']['gender']['observedAt'] = None

    assert normalize_basic_info_times(result) is True
    assert result == expected
    assert normalize_basic_info_times(result) is False
    assert result == expected


@pytest.mark.parametrize(
    'field,value', [('age', True), ('age', -1), ('age', 151), ('age', '42'), ('gender', 'unknown'), ('name', '')]
)
def test_invalid_field_never_becomes_a_confirmed_candidate(field, value):
    raw = report()
    raw['basicInfo'][field] = value
    values, meta, issues = validate_basic_info(raw, text=TEXT)
    assert values[field] is None and meta[field]['status'] == 'unresolved'
    assert issues


def test_report_cannot_claim_the_api_highest_grade():
    raw = report(reliability='very_high')
    values, meta, issues = validate_basic_info(raw, text=TEXT)
    assert all(value is None for value in values.values())
    assert issues and all(item['sourceReliability'] is None for item in meta.values())


def test_unknown_subject_and_multiple_candidates_are_archived_not_selected():
    raw = report()
    raw['basicInfoMeta']['name'].update(
        subjectStatus='ambiguous',
        candidates=[
            {'value': '测试甲', 'evidence': {'quote': '姓名：测试甲'}, 'sourceReliability': 'low'},
            {'value': '测试乙', 'evidence': {'quote': '姓名：测试乙'}, 'sourceReliability': 'low'},
        ],
    )
    values, meta, _ = validate_basic_info(raw, text=TEXT + '姓名：测试乙')
    assert values['name'] is None and meta['name']['status'] == 'conflicting'
    assert len(meta['name']['candidates']) == 2


def test_wrong_quote_or_offsets_cannot_support_an_extracted_value():
    raw = report()
    raw['basicInfoMeta']['age']['evidence'] = {'quote': '年龄：42岁', 'start': 0, 'end': 6}
    values, meta, issues = validate_basic_info(raw, text=TEXT)
    assert values['age'] is None and issues
    assert meta['age']['evidence'] is None
    raw['basicInfoMeta']['name']['evidence'] = {'quote': '姓名：测试乙'}
    assert validate_basic_info(raw, text=TEXT)[0]['name'] is None


def test_no_source_is_not_found_not_a_low_default():
    raw = report(name=None, gender=None, age=None)
    values, meta, issues = validate_basic_info(raw, text=TEXT)
    assert not issues and all(value is None for value in values.values())
    assert all(item['status'] == 'not_found' and item['sourceReliability'] is None for item in meta.values())


def normalized(raw, text):
    values, metadata, _ = validate_basic_info(raw, text=text)
    return {**raw, 'basicInfo': values, 'basicInfoMeta': metadata}


def test_same_subject_can_merge_fields_but_unproven_subjects_cannot_be_spliced():
    first = normalized(report(gender=None, age=None), TEXT)
    second = normalized(report(name=None), TEXT)
    target = merge_basic_info([first, second], {})
    assert target['basicInfo'] == {'name': '测试甲', 'gender': '女', 'age': 42}
    second['basicInfoMeta']['gender']['subjectRef'] = None
    target = merge_basic_info([first, second], {})
    assert all(value is None for value in target['basicInfo'].values())


def test_different_patient_refs_leave_every_top_level_basic_value_unselected():
    first = normalized(report(), TEXT)
    other_text = TEXT.replace('测试甲', '测试乙')
    other = normalized(report(name='测试乙', subject='测试乙'), other_text)
    target = merge_basic_info([first, other], {})
    assert all(value is None for value in target['basicInfo'].values())
    assert all(item['status'] == 'conflicting' for item in target['basicInfoMeta'].values())


@pytest.mark.asyncio
async def test_complete_first_pass_has_no_optional_call_and_public_projection_retains_proofs():
    current, legacy, graph = setup(lambda _: report())
    trace = MemoryTrace()
    result = await current.analyze(TEXT, trace=trace)
    assert len(graph.calls) == 1
    assert result['processing']['version'] == current.VERSION
    assert display_result(result)['basicInfo']['age'] == 42
    assert trace.payloads['analysis_part_00000']['firstPhaseDone']
    assert 'analysis_part_00000_0' not in trace.payloads
    old = await legacy.analyze(TEXT)
    assert 'basicInfo' not in old
    assert old['processing']['version'] == 'analysis-health-report-v12'


@pytest.mark.asyncio
async def test_basic_info_only_is_displayable_partial_not_clinical_normality():
    raw = report()
    raw['observations'] = []
    current, _, graph = setup(lambda _: raw)
    result = await current.analyze(TEXT)
    assert len(graph.calls) == 1
    assert result['processing']['status'] == 'partial'
    assert result['status'] == 'pending'
    assert display_result(result)['basicInfo']['name'] == '测试甲'


@pytest.mark.asyncio
async def test_missing_field_checks_once_and_keeps_first_stage_clinical_items():
    def respond(text):
        raw = report(age=None) if '第二阶段' not in text else report()
        if '第二阶段' in text:
            raw['observations'] = [{'itemName': '不得替换', 'resultValue': '999'}]
        return raw

    current, _, graph = setup(respond)
    trace = MemoryTrace()
    result = await current.analyze(TEXT, trace=trace)
    assert len(graph.calls) == 2
    assert result['basicInfo']['age'] == 42
    assert result['observations'][0]['itemName'] == '指标'
    again = await current.analyze(TEXT, trace=trace)
    assert len(graph.calls) == 2
    assert again['basicInfo'] == result['basicInfo']


@pytest.mark.asyncio
async def test_invalid_format_and_optional_check_share_three_call_budget_across_restart():
    calls = 0

    def respond(text):
        nonlocal calls
        calls += 1
        return 'bad json' if calls == 1 else report(age=None)

    current, _, graph = setup(respond)
    trace = MemoryTrace()
    await current.analyze(TEXT, trace=trace)
    assert len(graph.calls) == 3
    await current.analyze(TEXT, trace=trace)
    assert len(graph.calls) == 3
    assert trace.payloads['analysis_part_00000']['firstPhaseCalls'] == 2
    assert trace.payloads['analysis_part_00000_0']['started']


@pytest.mark.asyncio
async def test_uncertain_basic_check_consumes_slot_and_cannot_repeat_after_restart():
    current, _, graph = setup(lambda _: report(age=None))
    trace = MemoryTrace()
    await current.analyze(TEXT, trace=trace)
    trace.payloads['analysis_part_00000_0'].pop('done')
    trace.payloads['analysis_part_00000_0'].pop('result')
    before = len(graph.calls)
    result = await current.analyze(TEXT, trace=trace)
    assert len(graph.calls) == before
    assert result['basicInfo']['age'] is None


@pytest.mark.asyncio
async def test_all_slices_first_pass_finish_before_slow_optional_checks_start():
    events = []

    async def respond(text):
        if '第二阶段' in text:
            events.append('check')
            await asyncio.sleep(0.025)
        else:
            events.append('first')
        return report(name=None, gender=None, age=None)

    current, _, graph = setup(respond)
    current.MAX_ANALYZE_INPUT_TOKENS = 80
    text = ('单段报告。' * 14 + '\n') * 8
    result = await current.analyze(text, trace=MemoryTrace())
    phases = events[: events.index('check')]
    assert len(phases) >= 5 and set(phases) == {'first'}
    assert events.count('first') == len(phases)
    assert result['observations']
    assert len(graph.calls) <= len(phases) * 3


@pytest.mark.asyncio
async def test_check_cannot_splice_a_new_patients_field_into_the_first_patient():
    def respond(text):
        return report(age=None) if '第二阶段' not in text else report(name='测试乙', subject='测试乙')

    current, _, _ = setup(respond)
    result = await current.analyze(TEXT + TEXT.replace('测试甲', '测试乙'))
    assert all(value is None for value in result['basicInfo'].values())
    assert result['observations'][0]['itemName'] == '指标'


def test_recovery_merge_does_not_drop_basic_only_material():
    from modules.analysis.application.analysis_outcomes import combine_verified

    first = normalized(report(gender=None, age=None), TEXT)
    first['observations'] = []
    second = normalized(report(name=None), TEXT)
    second['observations'] = []
    result = combine_verified(first, second)
    assert result['basicInfo'] == {'name': '测试甲', 'gender': '女', 'age': 42}


@pytest.mark.parametrize('value', [False, [], {}])
def test_invalid_nonnull_value_cannot_be_disguised_as_not_found(value):
    raw = report(name=None)
    raw['basicInfo']['name'] = value
    values, meta, issues = validate_basic_info(raw, text=TEXT)
    assert values['name'] is None and meta['name']['status'] == 'unresolved' and issues


def test_alternative_candidate_cannot_be_hidden_beside_the_selected_value():
    raw = report()
    raw['basicInfoMeta']['age']['candidates'] = [
        {'value': 43, 'evidence': {'quote': '年龄：43岁'}, 'sourceReliability': 'low'},
    ]
    values, meta, _ = validate_basic_info(raw, text=TEXT + '年龄：43岁')
    assert values['age'] is None and meta['age']['status'] == 'conflicting'


def test_name_uses_trim_nonempty_and_overall_capacity_not_a_new_length_cutoff():
    name = '合成受检者' * 100
    raw = report(name=name, subject=name)
    text = TEXT.replace('测试甲', name)
    values, _, issues = validate_basic_info(raw, text=text)
    assert values['name'] == name and not issues


@pytest.mark.parametrize(
    'candidate',
    [
        {'value': '42', 'sourceReliability': 'low'},
        {'value': 42, 'sourceReliability': 'very_high'},
        {'value': True, 'sourceReliability': 'medium'},
        {'value': float('nan'), 'sourceReliability': 'medium'},
    ],
)
def test_noncanonical_pending_candidates_do_not_escape_to_the_fixed_contract(candidate):
    raw = report(age=None)
    raw['basicInfoMeta']['age'].update(
        status='conflicting',
        candidates=[{**candidate, 'evidence': {'quote': '年龄：42岁'}, 'reason': '原候选'}],
    )
    values, metadata, _ = validate_basic_info(raw, text=TEXT)
    assert values['age'] is None
    assert metadata['age']['status'] == 'conflicting'
    assert metadata['age']['candidates'] == []
    assert len(metadata['age']['reason']) <= 2000


def test_a_limitation_appended_to_a_maximum_reason_stays_within_the_shared_contract():
    raw = report(age=None)
    raw['basicInfoMeta']['age']['reason'] = '原' * 2000
    current, _, _ = setup(lambda _: raw)
    result = current._limited_check(raw, '核查额度已用尽。')
    assert len(result['basicInfoMeta']['age']['reason']) == 2000
    assert result['basicInfoMeta']['age']['reason'].endswith('核查额度已用尽。')


@pytest.mark.parametrize(
    ('quote', 'gender'),
    [('Sex: Male', '男'), ('Gender: Female', '女'), ('Sex: M', '男'), ('性别：F', '女')],
)
def test_explicit_gender_enum_supports_the_normalized_field(quote, gender):
    raw = report()
    raw['basicInfo']['gender'] = gender
    raw['basicInfoMeta']['gender']['evidence'] = {'quote': quote}
    values, metadata, _ = validate_basic_info(raw, text=TEXT + quote)
    assert values['gender'] == gender
    assert metadata['gender']['status'] == 'extracted'


@pytest.mark.parametrize('quote', ['Sex: M/F', 'Gender: female/male', 'Surname: Female', 'Sex: malevolent'])
def test_gender_mapping_requires_a_single_explicit_gender_enum(quote):
    raw = report()
    raw['basicInfo']['gender'] = '女'
    raw['basicInfoMeta']['gender']['evidence'] = {'quote': quote}
    values, _, _ = validate_basic_info(raw, text=TEXT + quote)
    assert values['gender'] is None


@pytest.mark.parametrize(
    ('quote', 'age'),
    [
        ('年龄：6个月', 0.5),
        ('Age: 6 months', 0.5),
        ('月龄：6', 0.5),
        ('Age (months): 6', 0.5),
        ('年龄：1岁6个月', 1.5),
        ('Age: 1 year and 6 months', 1.5),
        ('Age: 42 years', 42),
    ],
)
def test_explicit_age_units_support_normalized_years(quote, age):
    raw = report()
    raw['basicInfo']['age'] = age
    raw['basicInfoMeta']['age']['evidence'] = {'quote': quote}
    values, metadata, _ = validate_basic_info(raw, text=TEXT + quote)
    assert values['age'] == age
    assert metadata['age']['status'] == 'extracted'


@pytest.mark.parametrize(('quote', 'age'), [('年龄：6个月', 6), ('Age: 6 days', 6), ('年龄：1岁6个月', 1)])
def test_explicit_age_units_cannot_be_reinterpreted_as_an_unconverted_year_count(quote, age):
    raw = report()
    raw['basicInfo']['age'] = age
    raw['basicInfoMeta']['age']['evidence'] = {'quote': quote}
    values, _, _ = validate_basic_info(raw, text=TEXT + quote)
    assert values['age'] is None


@pytest.mark.asyncio
async def test_malformed_basic_info_uses_optional_check_without_clinical_repair_or_quality_loss():
    def respond(text):
        raw = report()
        if '第二阶段' not in text:
            raw['basicInfoMeta'] = 'invalid field metadata'
        return raw

    current, _, graph = setup(respond)
    trace = MemoryTrace()
    result = await current.analyze(TEXT, trace=trace)
    assert len(graph.calls) == 2
    assert trace.payloads['analysis_part_00000']['firstPhaseCalls'] == 1
    validation = trace.payloads['analysis_output_00000']['validation']
    assert validation['valid'] and not validation['issues']
    assert validation['diagnostics'][0]['scope'] == 'basic_info'
    assert result['processing']['status'] == 'complete'
    assert result['observations'][0]['itemName'] == '指标'
    assert result['basicInfo']['age'] == 42


@pytest.mark.asyncio
async def test_low_quality_optional_reply_cannot_change_successful_clinical_result():
    def respond(text):
        raw = report(age=None)
        if '第二阶段' in text:
            raw.update(reportType='invalid-type', observations='invalid-clinical-data', basicInfoMeta='invalid')
        return raw

    current, _, graph = setup(respond)
    trace = MemoryTrace()
    result = await current.analyze(TEXT, trace=trace)
    assert len(graph.calls) == 2
    assert trace.payloads['analysis_part_00000']['firstPhaseCalls'] == 1
    assert result['reportType'] == 'lab'
    assert result['observations'][0]['itemName'] == '指标'
    assert result['processing']['status'] == 'complete'
    assert result['processing']['issues'] == []
    assert result['basicInfo']['age'] is None
    assert result['basicInfoMeta']['age']['status'] != 'extracted'


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type', [TimeoutError, asyncio.CancelledError])
async def test_failed_or_cancelled_basic_check_preserves_first_clinical_checkpoint_and_bounded_resume(error_type):
    def respond(text):
        if '第二阶段' in text:
            raise error_type()
        return report(age=None)

    current, _, graph = setup(respond)
    trace = MemoryTrace()
    if error_type is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await current.analyze(TEXT, trace=trace)
        result = await current.analyze(TEXT, trace=trace)
    else:
        result = await current.analyze(TEXT, trace=trace)
    assert len(graph.calls) == 2
    first = trace.payloads['analysis_part_00000']
    assert first['firstPhaseDone'] and first['firstPhaseCalls'] == 1
    assert first['result']['observations'][0]['itemName'] == '指标'
    assert result['processing']['status'] == 'complete'
    assert result['processing']['issues'] == []
    assert result['observations'][0]['itemName'] == '指标'
    assert result['basicInfo']['age'] is None
    assert trace.payloads['analysis_part_00000_0']['started']


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_grade', [[], {}, ['medium'], {'value': 'low'}])
async def test_container_grade_does_not_crash_or_invalidate_successful_clinical_extraction(bad_grade):
    def respond(_):
        raw = report()
        raw['basicInfoMeta']['age']['sourceReliability'] = bad_grade
        return raw

    current, _, graph = setup(respond)
    trace = MemoryTrace()
    result = await current.analyze(TEXT, trace=trace)
    assert len(graph.calls) == 2
    assert trace.payloads['analysis_part_00000']['firstPhaseCalls'] == 1
    assert result['processing']['status'] == 'complete'
    assert result['basicInfo']['age'] is None
    assert result['observations'][0]['itemName'] == '指标'


@pytest.mark.parametrize('bad_grade', [[], {}, ['medium'], {'value': 'low'}])
def test_container_candidate_grade_stays_unresolved_without_escaping_the_contract(bad_grade):
    raw = report(age=None)
    raw['basicInfoMeta']['age'].update(
        status='conflicting',
        candidates=[{'value': 42, 'evidence': {'quote': '年龄：42岁'}, 'sourceReliability': bad_grade}],
    )
    values, metadata, _ = validate_basic_info(raw, text=TEXT)
    assert values['age'] is None
    assert metadata['age']['status'] == 'conflicting'
    assert metadata['age']['candidates'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [[], 'not_an_object', 42])
async def test_nonobject_reply_produces_basic_diagnostics_without_breaking_clinical_json_repair(raw):
    values, metadata, issues = validate_basic_info(raw, text=TEXT)
    assert all(value is None for value in values.values())
    assert all(item['status'] == 'unresolved' for item in metadata.values())
    assert issues[0]['code'] == 'invalid_json_schema'
    current, _, graph = setup(lambda text: report() if '修复' in text else raw)
    trace = MemoryTrace()
    result = await current.analyze(TEXT, trace=trace)
    assert len(graph.calls) == 2
    assert trace.payloads['analysis_part_00000']['firstPhaseCalls'] == 2
    assert result['processing']['status'] == 'complete'
    assert result['observations'][0]['itemName'] == '指标'


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_sections', [None, {}])
async def test_invalid_section_container_follows_the_existing_clinical_repair_path(invalid_sections):
    def respond(text):
        raw = report()
        if '修复' not in text:
            raw['sections'] = invalid_sections
        return raw

    current, _, graph = setup(respond)
    trace = MemoryTrace()
    result = await current.analyze(TEXT, trace=trace)
    assert len(graph.calls) == 2
    assert trace.payloads['analysis_part_00000']['firstPhaseCalls'] == 2
    assert result['processing']['status'] == 'complete'
    assert result['observations'][0]['itemName'] == '指标'


@pytest.mark.parametrize(
    ('field', 'value', 'quote'),
    [('name', '测试乙', '姓名：测试甲'), ('gender', '男', '性别：女'), ('age', 43, '年龄：42岁')],
)
def test_candidate_value_must_be_supported_by_its_own_quote(field, value, quote):
    raw = report()
    raw['basicInfoMeta'][field]['candidates'] = [
        {'value': value, 'evidence': {'quote': quote}, 'sourceReliability': 'low', 'reason': 'invalid candidate'}
    ]
    values, metadata, _ = validate_basic_info(raw, text=TEXT)
    assert values[field] is None
    assert metadata[field]['status'] == 'unresolved'
    assert metadata[field]['candidates'] == []
    assert '部分候选未通过规范校验' in metadata[field]['reason']


@pytest.mark.parametrize('masked', ['张*', 'J***', '姓名已遮盖'])
def test_masked_name_retains_original_candidate_without_confirming_a_real_name(masked):
    text = f'受检者姓名：{masked}'
    raw = report(name=None, gender=None, age=None)
    raw['basicInfoMeta']['name'].update(
        status='unresolved',
        reason='姓名已脱敏或被遮盖，无法确认完整姓名。',
        subjectStatus='single',
        evidence={'quote': text},
        candidates=[
            {'value': masked, 'evidence': {'quote': text}, 'sourceReliability': 'low', 'reason': '原文遮盖姓名'}
        ],
    )
    values, metadata, _ = validate_basic_info(raw, text=text)
    assert values['name'] is None and metadata['name']['status'] == 'unresolved'
    assert metadata['name']['candidates'][0]['value'] == masked


def test_current_prompt_explicitly_keeps_masked_names_out_of_confirmed_name():
    prompt = Path('prompts/analysis/health_report_current.txt').read_text(encoding='utf-8')
    assert '脱敏' in prompt and '遮盖' in prompt
    assert '不得作为已确认姓名' in prompt
