"""Attention references are expression data, never a new patient-fact channel."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from modules.record.application.dialogue import CONTEXT_KEY
from modules.record.application.dialogue import MAX_REFERENCES
from modules.record.application.dialogue import build_dialogue_context
from modules.record.application.dialogue import validate_dialogue_focus
from modules.record.application.intake import IntakeUnderstanding
from modules.record.application.intake import merge_understanding
from modules.record.application.validation import RecordValidationError


def _patient():
    return {
        '本次回答': '最近入睡困难，也想减轻体重，优先改善入睡',
        '历史问答': [],
        '本次报告': '报告 1\n空腹血糖：6.8 mmol/L；标记：偏高\n低密度脂蛋白胆固醇：4.2 mmol/L',
    }


def _intake():
    return {
        'preferredId': 'sleep',
        'complaints': [
            {
                'id': 'sleep',
                'text': '入睡困难',
                'evidence': [{'source': '本次回答', 'quote': '最近入睡困难'}],
            },
            {
                'id': 'weight',
                'text': '减轻体重',
                'evidence': [{'source': '本次回答', 'quote': '也想减轻体重'}],
            },
        ],
        'facts': {},
    }


def _reports():
    return [
        {
            'reportId': 'r1',
            'status': 'abnormal',
            'aiSummary': '不可将AI摘要当成原始化验项目',
            'observations': [
                {'itemName': '空腹血糖', 'resultValue': '6.8', 'unit': 'mmol/L', 'flag': '偏高'},
                {'itemName': '低密度脂蛋白胆固醇', 'resultValue': '4.2', 'unit': 'mmol/L'},
            ],
        }
    ]


def _context(patient=None, *, dimension='sleep', previous=None, intake=None, reports=None):
    patient = deepcopy(patient if patient is not None else _patient())
    patient.update(
        build_dialogue_context(
            patient,
            previous or {},
            {'dimension': dimension},
            intake=_intake() if intake is None else intake,
            reports=_reports() if reports is None else reports,
        )
    )
    return patient


def _entries(patient):
    return patient[CONTEXT_KEY]['可用关注点']


def _message(entry=None, *, quote=None, source=None, acknowledgement='入睡这件事，先聊聊平时的作息。'):
    return SimpleNamespace(
        acknowledgement=acknowledgement,
        focusRef=entry['ref'] if entry else None,
        evidence=[SimpleNamespace(source=source or entry['source'], quote=quote or entry['quote'])] if entry else [],
    )


def test_catalog_separates_grounded_goals_and_each_report_item_and_respects_preference():
    patient = _context()
    entries = _entries(patient)
    assert [item['text'] for item in entries] == ['入睡困难', '减轻体重', '空腹血糖', '低密度脂蛋白胆固醇']
    assert [item['preferred'] for item in entries] == [True, False, False, False]
    assert entries[2]['path'] == 'r1/observations/0'
    assert len({item['ref'] for item in entries}) == 4
    for entry in entries:
        validate_dialogue_focus(_message(entry), patient, action='ask_intake')


def test_user_reference_stable_when_answer_becomes_history():
    first = _context()
    patient = _patient()
    patient['历史问答'] = [{'问题': '本次最关注什么？', '用户回答': patient['本次回答']}]
    patient['本次回答'] = '没有'
    second = _context(patient)
    assert _entries(first)[0]['ref'] == _entries(second)[0]['ref']
    assert _entries(second)[0]['source'] == '历史回答'


def test_complaint_reference_survives_updated_evidence_and_goal_fact_is_not_second_focus():
    first = _context()
    intake = _intake()
    intake['complaints'][0]['evidence'][0]['quote'] = _patient()['本次回答']
    intake['facts']['sleep:goal'] = {
        'dimension': 'goal',
        'status': 'known',
        'summary': '改善入睡',
        'complaintIds': ['sleep'],
        'evidence': [{'source': '本次回答', 'quote': '优先改善入睡'}],
    }
    second = _context(intake=intake)
    assert [item['ref'] for item in _entries(first)] == [item['ref'] for item in _entries(second)]


def test_normal_report_context_has_no_invented_abnormal_flag_and_refs_survive_metadata_update():
    reports = _reports()
    first = _context(reports=reports)
    reports[0]['status'] = 'normal'
    reports[0]['observations'][0]['flag'] = '正常'
    reports[0]['observations'][0]['referenceRange'] = '3.9～6.1'
    patient = _patient()
    patient['本次报告'] = patient['本次报告'].replace('标记：偏高', '参考范围：3.9～6.1；标记：正常')
    second = _context(patient, reports=reports)
    assert _entries(first)[2]['ref'] == _entries(second)[2]['ref']
    assert _entries(second)[2]['flag'] == '正常'
    assert _entries(second)[2]['reportStatus'] == 'normal'
    assert _entries(second)[2]['referenceRange'] == '3.9～6.1'
    assert _entries(second)[3]['flag'] == ''


def test_unavailable_section_and_observation_filtered_and_single_report_dict_supported():
    reports = _reports()
    reports[0]['observations'][1]['processing'] = {'status': 'unavailable'}
    reports[0]['sections'] = [{'status': 'failed', 'observations': reports[0]['observations']}]
    entries = _entries(_context(reports=reports[0]))
    assert [item['text'] for item in entries if item['kind'] == 'report'] == ['空腹血糖']


def test_long_evidence_is_reduced_to_a_complete_grounded_sentence():
    patient = _patient()
    patient['本次回答'] = '已有资料。' * 300 + '最近入睡困难。'
    intake = _intake()
    intake['complaints'][0]['evidence'][0]['quote'] = patient['本次回答']
    entries = _entries(_context(patient, intake=intake, reports=[]))
    assert len(entries) == 1
    assert entries[0]['quote'] == '最近入睡困难。'


def test_public_question_id_is_authoritative_for_replay_deduplication():
    reference = _entries(_context())[0]['ref']
    previous = {
        'qaHistory': [{'question': {'questionId': 'q1', 'id': 'old', 'dialogueFocusRef': reference}, 'answer': '是'}],
        'currentQuestion': {'questionId': 'q1', 'id': 'new', 'dialogueFocusRef': reference},
    }
    context = _context(previous=previous)[CONTEXT_KEY]
    assert context['已发布使用'] == [{'ref': reference, 'count': 1, 'lastQuestionId': 'q1'}]


def test_old_projection_can_recover_only_original_user_goals():
    patient = _patient()
    patient['已识别的困扰'] = [
        {'编号': 'sleep', '困扰': '入睡困难', '用户优先关注': True},
        {'编号': 'invented', '困扰': '夜间憋醒'},
    ]
    entries = _entries(_context(patient, intake={}, reports=[]))
    assert [item['text'] for item in entries] == ['入睡困难']


def test_unsupported_synthesized_complaint_label_uses_source_without_inventing_symptom():
    intake = _intake()
    intake['complaints'][0]['text'] = '严重焦虑与夜间憋醒'
    entries = _entries(_context(intake=intake, reports=[]))
    assert entries[0]['text'] == _patient()['本次回答']
    assert '焦虑' not in str(entries)


def test_short_goal_quote_keeps_original_negation_and_time_conditions():
    patient = _patient()
    patient['本次回答'] = '最近没有入睡困难，只在去年出差时睡不着。'
    intake = _intake()
    intake['complaints'][0]['evidence'][0]['quote'] = '入睡困难'
    entries = _entries(_context(patient, intake=intake, reports=[]))
    assert entries[0]['quote'] == patient['本次回答']


def test_report_metadata_not_exposed_on_source_line_is_not_invented():
    reports = _reports()
    reports[0]['observations'][0]['referenceRange'] = '3.9～6.1'
    entries = _entries(_context(reports=reports))
    assert entries[2]['referenceRange'] == ''
    assert entries[2]['flag'] == '偏高'


@pytest.mark.parametrize('status', ['unavailable', 'failed', 'pending', 'processing'])
def test_unavailable_reports_never_provide_references(status):
    reports = _reports()
    reports[0]['processing'] = {'status': status}
    assert all(item['kind'] == 'goal' for item in _entries(_context(reports=reports)))


def test_report_fields_omitted_from_bounded_patient_context_are_not_quotable():
    patient = _patient()
    patient['本次报告'] = '报告 1\n空腹血糖：6.8 mmol/L；标记：偏高'
    entries = _entries(_context(patient))
    assert [item['text'] for item in entries if item['kind'] == 'report'] == ['空腹血糖']


def test_catalog_budget_keeps_goals_and_late_explicit_report_abnormality_before_normal_rows():
    patient = _patient()
    observations = [
        {'itemName': f'正常项目{index}', 'resultValue': '10', 'flag': '正常'} for index in range(MAX_REFERENCES)
    ] + [{'itemName': '最后的异常项目', 'resultValue': '20', 'flag': '偏高'}]
    patient['本次报告'] = '\n'.join(
        f'{item["itemName"]}：{item["resultValue"]}；标记：{item["flag"]}' for item in observations
    )
    reports = [{'reportId': 'large', 'status': 'abnormal', 'observations': observations}]
    original = deepcopy((patient, reports))
    entries = _entries(_context(patient, reports=reports))
    assert len(entries) == MAX_REFERENCES
    assert [item['text'] for item in entries[:3]] == ['入睡困难', '减轻体重', '最后的异常项目']
    assert entries[2]['flag'] == '偏高'
    assert entries[3]['text'] == '正常项目0'
    assert (patient, reports) == original


def test_preferred_goal_is_preserved_when_goal_catalog_itself_exceeds_budget():
    patient = {'本次回答': '。'.join(f'关注事项{index}' for index in range(MAX_REFERENCES + 1)), '历史问答': []}
    intake = {
        'preferredId': f'goal{MAX_REFERENCES}',
        'complaints': [
            {
                'id': f'goal{index}',
                'text': f'关注事项{index}',
                'evidence': [{'source': '本次回答', 'quote': f'关注事项{index}'}],
            }
            for index in range(MAX_REFERENCES + 1)
        ],
    }
    entries = _entries(_context(patient, intake=intake, reports=[]))
    assert len(entries) == MAX_REFERENCES
    assert entries[0]['text'] == f'关注事项{MAX_REFERENCES}'
    assert entries[0]['preferred'] is True


def _goal_fact_context(statement, *, presence=None, specificity='unspecified', status='known', observations=True):
    evidence = [{'source': '本次回答', 'quote': statement}]
    fact = {'dimension': 'goal', 'status': status, 'summary': statement, 'evidence': evidence}
    if presence is not None:
        meaning = {'presence': presence, 'specificity': specificity, 'description': statement, 'evidence': evidence}
        if observations:
            fact['observations'] = [meaning]
        else:
            fact.update(meaning)
    return _context(
        {'本次回答': statement, '历史问答': []},
        intake={'complaints': [], 'facts': {'general:goal': fact}},
        reports=[],
    )


@pytest.mark.parametrize('observations', [True, False])
@pytest.mark.parametrize('presence', ['absent', 'unknown', 'conflicting'])
def test_explicit_nonpresent_goal_never_becomes_attention_even_when_coverage_known(presence, observations):
    patient = _goal_fact_context('目前没有不适，也没有想改善的具体问题。', presence=presence, observations=observations)
    assert _entries(patient) == []


@pytest.mark.parametrize('status', ['unknown', 'declined', 'conflicting'])
def test_unresolved_goal_coverage_is_not_an_attention_reference(status):
    assert _entries(_goal_fact_context('具体想了解什么暂不确定。', status=status)) == []


@pytest.mark.parametrize('statement', ['没有', '不清楚', '暂无改善需求'])
def test_explicit_shortcut_states_without_new_semantics_remain_non_goals(statement):
    assert _entries(_goal_fact_context(statement)) == []


@pytest.mark.parametrize('statement', ['身体不舒服', '不求看病，想减轻体重'])
def test_present_vague_complaint_and_healthy_positive_goal_are_retained(statement):
    entries = _entries(_goal_fact_context(statement, presence='present'))
    assert len(entries) == 1
    assert entries[0]['text'] == statement


def test_mixed_observations_keep_positive_goal_without_selecting_absent_summary():
    patient = {'本次回答': '没有不适。想减轻体重。', '历史问答': []}
    fact = {
        'dimension': 'goal',
        'status': 'known',
        'summary': '没有不适，但想减轻体重',
        'evidence': [{'source': '本次回答', 'quote': patient['本次回答']}],
        'observations': [
            {
                'presence': 'absent',
                'specificity': 'unspecified',
                'description': '没有不适',
                'evidence': [{'source': '本次回答', 'quote': '没有不适'}],
            },
            {
                'presence': 'present',
                'specificity': 'specific',
                'description': '减轻体重',
                'evidence': [{'source': '本次回答', 'quote': '想减轻体重'}],
            },
        ],
    }
    entries = _entries(_context(patient, intake={'facts': {'general:goal': fact}}, reports=[]))
    assert len(entries) == 1
    assert entries[0]['text'] == '减轻体重'
    assert entries[0]['quote'] == '想减轻体重。'


def test_distinct_healthy_goals_in_real_aggregate_keep_separate_stable_references():
    answer = '想减轻体重，也想增加肌肉。'
    understanding = IntakeUnderstanding.model_validate(
        {
            'complaints': [],
            'preferredIndex': None,
            'priorityClarificationNeeded': False,
            'facts': [
                {
                    'dimension': 'goal',
                    'scope': 'general',
                    'status': 'known',
                    'summary': name,
                    'evidence': [{'source': '本次回答', 'quote': quote}],
                    'presence': 'present',
                    'specificity': 'specific',
                    'entities': [name],
                }
                for name, quote in [('减轻体重', '想减轻体重'), ('增加肌肉', '也想增加肌肉')]
            ],
        }
    )
    flow = {}
    merge_understanding(flow, understanding)
    assert len(flow['intake']['facts']['general:goal']['observations']) == 2
    patient = {'本次回答': answer, '历史问答': []}
    first = _entries(_context(patient, intake=flow['intake'], reports=[]))
    first_ids = {item['text']: item['ref'] for item in first}
    assert set(first_ids) == {'减轻体重', '增加肌肉'}
    assert len(set(first_ids.values())) == 2
    observations = flow['intake']['facts']['general:goal']['observations']
    observations.reverse()
    for observation in observations:
        observation['evidence'][0]['quote'] = answer
    second = _entries(_context(patient, intake=flow['intake'], reports=[]))
    assert {item['text']: item['ref'] for item in second} == first_ids


def test_ai_summary_and_previous_ai_words_cannot_create_focus():
    patient = {'本次回答': None, '历史问答': [{'问题': '睡眠问题？', '用户回答': '没有'}], '本次报告': '解读摘要：焦虑'}
    previous = {'qaHistory': [{'question': {'id': 'q1', 'assistantMessage': '您有焦虑'}, 'answer': '没有'}]}
    intake = {'complaints': [{'text': '焦虑', 'evidence': [{'source': '历史回答', 'quote': '您有焦虑'}]}]}
    assert _entries(_context(patient, intake=intake, previous=previous, reports=[{'aiSummary': '焦虑'}])) == []


def test_medical_history_catalog_contains_goals_only_and_family_catalog_empty():
    assert all(item['kind'] == 'goal' for item in _entries(_context(dimension='medical_history')))
    assert _entries(_context(dimension='family_history')) == []


def test_family_history_cannot_narrow_to_report_even_if_reference_is_omitted():
    patient = _context(dimension='family_history')
    with pytest.raises(RecordValidationError, match='家族史'):
        validate_dialogue_focus(
            _message(acknowledgement='空腹血糖的情况，接着聊聊家里人。'), patient, action='ask_intake'
        )
    validate_dialogue_focus(_message(acknowledgement='接着聊聊家里人的健康情况。'), patient, action='ask_intake')


def test_usage_counts_only_published_ids_and_deduplicates_replay():
    reference = _entries(_context())[0]['ref']
    q1 = {'id': 'q1', 'question': '作息？', 'dialogueFocusRef': reference}
    q2 = {'id': 'q2', 'question': '运动？', 'dialogueFocusRef': reference}
    previous = {
        'qaHistory': [{'question': q1, 'answer': '规律'}, {'question': q1, 'answer': '规律'}],
        'currentQuestion': q2,
        'draft': {'id': 'q3', 'dialogueFocusRef': reference},
        'qaFlow': {'focus': {'dialogueFocusRef': reference}},
    }
    first = _context(previous=previous)[CONTEXT_KEY]
    assert first['已发布使用'] == [{'ref': reference, 'count': 2, 'lastQuestionId': 'q2'}]
    assert first['上一关注引用'] == reference
    assert _context(previous=previous)[CONTEXT_KEY] == first


def test_old_questions_do_not_invent_focus_counts_and_neutral_last_question_clears_previous():
    reference = _entries(_context())[0]['ref']
    previous = {
        'qaHistory': [
            {'question': {'id': 'q1', 'question': '入睡困难？', 'dialogueFocusRef': reference}, 'answer': '是'},
            {'question': {'id': 'q2', 'question': '入睡困难？'}, 'answer': '是'},
        ],
        'currentQuestion': {'id': 'q3', 'question': '饮水？'},
    }
    context = _context(previous=previous)[CONTEXT_KEY]
    assert context['已发布使用'][0]['count'] == 1
    assert context['上一关注引用'] is None


def test_recent_complete_turns_preserve_all_components_and_current_answer_without_mutation():
    patient = _patient()
    previous = {
        'qaHistory': [
            {'question': {'id': 'q1', 'assistantMessage': '先聊聊睡眠。', 'question': '何时入睡？'}, 'answer': '23点'},
        ],
        'currentQuestion': {'id': 'q2', 'assistantMessage': '再聊聊生活。', 'question': '关注什么？'},
    }
    originals = deepcopy((patient, previous))
    context = build_dialogue_context(patient, previous, {'dimension': 'sleep'})[CONTEXT_KEY]
    assert context['最近完整对话'][-1] == {
        '问题编号': 'q2',
        'AI发言': '再聊聊生活。',
        '问题': '关注什么？',
        '用户回答': patient['本次回答'],
    }
    assert (patient, previous) == originals
    patient['本次回答'] = None
    assert len(build_dialogue_context(patient, previous, None)[CONTEXT_KEY]['最近完整对话']) == 1


def test_unknown_reference_enters_existing_validation_path():
    patient = _context()
    message = _message()
    message.focusRef = 'dialogue_invented'
    with pytest.raises(RecordValidationError) as error:
        validate_dialogue_focus(message, patient, action='ask_intake')
    assert error.value.code == 'dialogue_focus_unknown'


def test_available_source_without_selected_focus_evidence_is_not_sufficient():
    patient = _context()
    report = _entries(patient)[2]
    with pytest.raises(RecordValidationError) as error:
        validate_dialogue_focus(_message(report, quote='低密度脂蛋白胆固醇：4.2 mmol/L'), patient, action='ask_intake')
    assert error.value.code == 'dialogue_focus_evidence_required'


@pytest.mark.parametrize('quote', ['空腹血糖：68 mmol/L', '空腹血糖：6 mmol/L', '血糖显著升高'])
def test_evidence_cannot_change_measurement_or_replace_quote_with_ungrounded_summary(quote):
    patient = _context()
    report = _entries(patient)[2]
    with pytest.raises(RecordValidationError):
        validate_dialogue_focus(_message(report, quote=quote), patient, action='ask_intake')


def test_catalog_quote_is_rechecked_against_source_not_trusted_as_patient_evidence():
    patient = _context()
    entry = _entries(patient)[2]
    patient['本次报告'] = '未能解析报告'
    with pytest.raises(RecordValidationError) as error:
        validate_dialogue_focus(_message(entry), patient, action='ask_intake')
    assert error.value.code == 'dialogue_focus_source_invalid'


@pytest.mark.parametrize('action', ['invite_supplement', 'finish', 'clarify_supplement'])
def test_non_question_turns_cannot_use_focus_metadata(action):
    patient = _context()
    with pytest.raises(RecordValidationError) as error:
        validate_dialogue_focus(_message(_entries(patient)[0]), patient, action=action)
    assert error.value.code == 'dialogue_focus_forbidden'


def test_absent_reference_keeps_legacy_expression_compatible():
    validate_dialogue_focus(SimpleNamespace(acknowledgement='接着聊聊生活。', evidence=[]), {}, action='ask_intake')


def test_raw_report_duplicate_expansion_does_not_duplicate_attention():
    reports = _reports()
    reports[0]['expansion'] = deepcopy(reports[0]['observations'])
    assert len([item for item in _entries(_context(reports=reports)) if item['kind'] == 'report']) == 2


def test_imaging_original_findings_can_be_referenced_without_promoting_summary():
    patient = {'本次回答': None, '历史问答': [], '本次报告': '影像所见：肝脏回声增强；日期：2026-10-22'}
    reports = [{'reportId': 'r1', 'status': 'abnormal', 'expansion': {'findings': '肝脏回声增强'}}]
    entries = _entries(_context(patient, intake={}, reports=reports))
    assert len(entries) == 1
    assert entries[0]['text'] == '肝脏回声增强'
    validate_dialogue_focus(_message(entries[0]), _context(patient, intake={}, reports=reports), action='ask_intake')
