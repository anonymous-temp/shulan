"""Partial repair preserves valid items without inventing their ownership."""

import json
from copy import deepcopy

import pytest

from modules.record.application import flow
from modules.record.application.output_repair import AnalysisRepair
from modules.record.application.output_repair import PresentationRepair


def encoded(value):
    return json.dumps(value, ensure_ascii=False)


def snapshot():
    return {'qaFlow': flow.new_flow(), 'qaHistory': [], 'currentQuestion': {'questionId': 'q'}}


def observation(text):
    return {'text': text, 'evidence': [{'source': '本次回答', 'quote': text}]}


def fact(index, summary='近一周出现', *, status='known'):
    return {
        'dimension': 'onset',
        'scope': 'complaint',
        'complaintIndexes': [index],
        'status': status,
        'summary': summary,
        'evidence': [{'source': '本次回答', 'quote': summary}],
    }


def gap(index):
    return {
        'topic': '出现频率',
        'dimension': 'frequency',
        'scope': 'complaint',
        'complaintIndexes': [index],
        'informationNeeded': '每周出现几次',
        'reason': '了解已经提到的不适出现频率',
    }


def assessment(*, complaints=None, facts=None, gaps=None):
    return {
        'intent': 'answer',
        'substantive': True,
        'intakeSufficient': True,
        'summary': '用户描述了近期不适。',
        'concerns': [],
        'gaps': gaps or [],
        'intake': {'complaints': complaints or [], 'facts': facts or []},
    }


def patient():
    return {
        '本次回答': '腹部不适',
        '历史问答': [],
        '本次报告': [],
        '长期健康画像': {},
        '附带健康资料': {},
        '基本信息': {},
    }


def message():
    return {
        'acknowledgement': '日常生活背景有助于理解当前关注。',
        'rationale': '继续了解相关情况。',
        'evidence': [{'source': '本次回答', 'quote': '腹部不适'}],
    }


def presentation(responses):
    return {
        'assistantMessage': message(),
        'responses': responses,
        'question': {
            'question': '不适出现多久？',
            'questionType': 'radio',
            'options': ['不到一周', '一周以上', '记不清'],
        },
    }


def presenter(*identities, action='ask_intake'):
    return PresentationRepair(
        {'action': action, 'responses': [{'concernId': identity} for identity in identities]}, patient()
    )


def test_bad_analysis_item_keeps_good_fact_without_marking_intake_sufficient():
    raw = assessment(complaints=[observation('腹部不适')], facts=[fact(0), {**fact(0), 'dimension': 'invented'}])
    collector = AnalysisRepair(snapshot(), '近一周腹部不适')
    parsed = collector.ingest(encoded(raw))
    assert parsed is not None
    assert len(parsed.intake.facts) == 1
    assert parsed.intakeSufficient is False
    assert collector.diagnostics['invalidCounts'] == {'facts': 1}


def test_repair_only_broken_fields_keeps_previously_valid_controls_and_facts():
    raw = assessment(complaints=[observation('腹部不适')], facts=[fact(0)])
    raw['intent'] = 'invalid'
    collector = AnalysisRepair(snapshot(), '近一周腹部不适')
    assert collector.ingest(encoded(raw)) is None
    assert len(collector.partial_intake.facts) == 1
    repaired = collector.ingest(encoded({'intent': '回答问题'}))
    assert repaired is not None
    assert repaired.intent == 'answer'
    assert len(repaired.intake.facts) == 1
    assert repaired.summary == raw['summary']
    assert repaired.intakeSufficient is False


def test_invalid_first_complaint_never_shifts_fact_to_the_second_complaint():
    raw = assessment(
        complaints=[{'text': '无依据的困扰'}, observation('头痛')],
        facts=[fact(0, '错误归属'), fact(1, '昨天开始')],
        gaps=[gap(0), gap(1)],
    )
    collector = AnalysisRepair(snapshot(), '昨天开始头痛')
    result = collector.ingest(encoded(raw))
    assert result is not None
    assert [item.text for item in result.intake.complaints] == ['头痛']
    assert [(item.summary, item.complaintIndexes) for item in result.intake.facts] == [('昨天开始', [0])]
    assert [item.complaintIndexes for item in result.gaps] == [[0]]


def test_reordered_repair_list_maps_facts_by_explicit_complaint_identity():
    collector = AnalysisRepair(snapshot(), '腹部不适和头痛')
    original = assessment(complaints=[observation('腹部不适'), observation('头痛')], facts=[fact(0)])
    collector.ingest(encoded(original))
    repaired = assessment(complaints=[observation('头痛'), observation('腹部不适')], facts=[fact(0, '昨天开始')])
    result = collector.ingest(encoded(repaired))
    assert [item.text for item in result.intake.complaints] == ['腹部不适', '头痛']
    assert [item.complaintIndexes for item in result.intake.facts] == [[0], [1]]


def test_same_output_conflicting_fact_statuses_are_both_excluded():
    raw = assessment(complaints=[observation('腹部不适')], facts=[fact(0), fact(0, '记不清', status='unknown')])
    collector = AnalysisRepair(snapshot(), '腹部不适')
    result = collector.ingest(encoded(raw))
    assert result is not None
    assert result.intake.facts == []
    assert result.intakeSufficient is False


def test_facts_with_unavailable_sources_are_not_preserved():
    raw = assessment(complaints=[observation('腹部不适')], facts=[fact(0)])
    raw['intake']['facts'][0]['evidence'] = [{'source': '本次报告', 'quote': '虚构的报告内容'}]
    collector = AnalysisRepair(snapshot(), '腹部不适')
    result = collector.ingest(encoded(raw))
    assert result is not None and not result.intake.facts
    assert '虚构' not in encoded(collector.diagnostics)


def test_all_proposed_gaps_invalid_cannot_silently_end_intake():
    raw = assessment(complaints=[observation('腹部不适')], gaps=[{**gap(0), 'dimension': 'unknown'}])
    collector = AnalysisRepair(snapshot(), '腹部不适')
    assert collector.ingest(encoded(raw)) is None
    assert len(collector.partial_intake.complaints) == 1
    repaired = collector.ingest(
        encoded({'gaps': [gap(0)], 'intake': {'complaints': [observation('腹部不适')], 'facts': []}})
    )
    assert repaired is not None and len(repaired.gaps) == 1


def test_unknown_concern_is_omitted_without_satisfying_pending_concern_coverage():
    session = snapshot()
    session['qaFlow']['concerns'] = [{'id': 'concern_known', 'status': 'pending'}]
    raw = assessment()
    raw['concerns'] = [
        {
            'id': 'patient-secret-invented-id',
            'text': '用户问题',
            'evidence': '用户原话',
            'readyToAnswer': True,
            'informationNeeded': '',
        }
    ]
    collector = AnalysisRepair(session, '腹部不适')
    assert collector.ingest(encoded(raw)) is None
    assert collector.concerns == []
    assert collector.diagnostics['missingConcernIds'] == ['concern_known']
    assert 'patient-secret' not in encoded(collector.diagnostics)


def test_analysis_rejects_ambiguous_aliases_and_never_fills_required_control():
    raw = assessment()
    raw['意图'] = '结束交流'
    collector = AnalysisRepair(snapshot(), '腹部不适')
    assert collector.ingest(encoded(raw)) is None
    assert 'intent' in collector.diagnostics['missingFields']


def test_presentation_exact_duplicate_is_deduplicated_and_unknown_id_is_omitted():
    collector = presenter('concern_a')
    response = {'concernId': 'concern_a', 'text': '针对本次疑问的说明。'}
    result = collector.ingest(
        encoded(presentation([response, response, {'concernId': 'private-unknown', 'text': '错误'}]))
    )
    assert result is not None
    assert [item.concernId for item in result.responses] == ['concern_a']
    assert 'private-unknown' not in encoded(collector.diagnostics)


def test_presentation_conflict_is_repaired_by_id_without_remapping_unknown_response():
    collector = presenter('concern_a', 'concern_b')
    raw = presentation(
        [
            {'concernId': 'concern_a', 'text': '已通过校验的回应。'},
            {'concernId': 'concern_b', 'text': '冲突一'},
            {'concernId': 'concern_b', 'text': '冲突二'},
            {'concernId': 'unknown', 'text': '不能按顺序分配给缺失疑问'},
        ]
    )
    assert collector.ingest(encoded(raw)) is None
    assert list(collector.responses) == ['concern_a']
    assert collector.diagnostics['conflictingConcernIds'] == ['concern_b']
    result = collector.ingest(encoded({'responses': [{'concernId': 'concern_b', 'text': '修复后的回应。'}]}))
    assert result is not None
    assert [item.text for item in result.responses] == ['已通过校验的回应。', '修复后的回应。']
    assert collector.diagnostics['conflictingConcernIds'] == []


def test_presentation_bad_acknowledgement_keeps_responses_and_question():
    collector = presenter('concern_a')
    raw = presentation([{'concernId': 'concern_a', 'text': '有效的回应。'}])
    raw['assistantMessage']['evidence'] = [{'source': '本次报告', 'quote': '本次不存在的报告'}]
    assert collector.ingest(encoded(raw)) is None
    partial = collector.partial_presentation
    assert partial['assistantMessage'] is None
    assert partial['question'] is not None
    assert len(partial['responses']) == 1
    assert collector.ingest(encoded({'assistantMessage': message()})) is not None


def test_text_quality_failure_is_local_to_the_affected_response():
    collector = presenter('concern_a', 'concern_b')
    raw = presentation(
        [
            {'concernId': 'concern_a', 'text': '有效的回应。'},
            {'concernId': 'concern_b', 'text': '不完整\ufffd'},
        ]
    )
    assert collector.ingest(encoded(raw)) is None
    assert list(collector.responses) == ['concern_a']


@pytest.mark.parametrize('action', ['finish', 'invite_supplement'])
def test_application_owned_question_shape_ignores_bad_model_question(action):
    collector = presenter(action=action)
    raw = presentation([])
    raw['question'] = {'broken': True}
    result = collector.ingest(encoded(raw))
    assert result is not None
    assert (result.question is None) is (action == 'finish')


def test_collectors_do_not_mutate_session_or_patient_inputs():
    session, context = snapshot(), patient()
    original_session, original_patient = deepcopy(session), deepcopy(context)
    AnalysisRepair(session, '腹部不适').ingest(encoded(assessment(complaints=[observation('腹部不适')])))
    PresentationRepair({'action': 'ask_intake', 'responses': []}, context).ingest(encoded(presentation([])))
    assert session == original_session
    assert context == original_patient


@pytest.mark.parametrize('content', ['{}', '{bad json', '{"intake":{}}'])
def test_missing_analysis_does_not_claim_empty_intake_is_reliable(content):
    session = snapshot()
    session['qaFlow']['intake']['priorityClarificationNeeded'] = True
    original = deepcopy(session)
    collector = AnalysisRepair(session, '腹部不适')
    assert collector.ingest(content) is None
    assert collector.partial_intake is None
    assert session == original


def test_partial_intake_keeps_prior_priority_when_output_omits_that_control():
    session = snapshot()
    session['qaFlow']['intake']['priorityClarificationNeeded'] = True
    collector = AnalysisRepair(session, '腹部不适')
    collector.ingest(encoded({'intake': {'complaints': [observation('腹部不适')], 'facts': []}}))
    assert collector.partial_intake.priorityClarificationNeeded is True


def test_garbled_legacy_reused_response_is_not_a_valid_partial_response():
    collector = PresentationRepair(
        {'action': 'invite_supplement', 'responses': [{'concernId': 'known', 'reuseResponse': '旧回复\ufffd'}]},
        patient(),
    )
    assert collector.responses == {}
    assert collector.diagnostics['invalidCounts'] == {'reusedResponses': 1}


def test_last_complete_distinguishes_full_repair_from_still_partial_analysis():
    collector = AnalysisRepair(snapshot(), '腹部不适')
    original = assessment(complaints=[observation('腹部不适')], facts=[{**fact(0), 'dimension': 'invalid'}])
    assert collector.ingest(encoded(original)) is not None
    assert collector.last_complete is False
    assert collector.is_partial is True
    complete = assessment(complaints=[observation('腹部不适')], facts=[fact(0)])
    result = collector.ingest(encoded(complete))
    assert result is not None and len(result.intake.facts) == 1
    assert collector.last_complete is True
    assert collector.is_partial is True
    assert result.intakeSufficient is False
    collector.ingest('{')
    assert collector.last_complete is False


def test_complete_repair_corrects_stop_intent_from_an_otherwise_invalid_reply():
    collector = AnalysisRepair(snapshot(), '腹部不适')
    broken = assessment()
    broken.update(intent='stop', summary='错误\ufffd')
    assert collector.ingest(encoded(broken)) is None
    corrected = collector.ingest(encoded(assessment()))
    assert corrected is not None
    assert collector.last_complete is True
    assert corrected.intent == 'answer'


def test_complete_repair_corrects_concern_readiness_without_losing_other_concerns():
    session = snapshot()
    session['qaFlow']['concerns'] = [
        {'id': 'concern_a', 'status': 'pending'},
        {'id': 'concern_b', 'status': 'answered'},
    ]
    collector = AnalysisRepair(session, '腹部不适')
    original = assessment()
    original['summary'] = '错误\ufffd'
    original['concerns'] = [
        {'id': identity, 'text': '用户疑问', 'evidence': '腹部不适', 'readyToAnswer': True, 'informationNeeded': ''}
        for identity in ('concern_a', 'concern_b')
    ]
    collector.ingest(encoded(original))
    repaired = assessment()
    repaired['concerns'] = [
        {
            'id': 'concern_a',
            'text': '用户疑问',
            'evidence': '腹部不适',
            'readyToAnswer': False,
            'informationNeeded': '出现时间',
        }
    ]
    result = collector.ingest(encoded(repaired))
    assert result is not None and collector.last_complete
    assert [item.id for item in result.concerns] == ['concern_a', 'concern_b']
    assert result.concerns[0].readyToAnswer is False
    assert result.concerns[0].informationNeeded == '出现时间'


def test_complete_repair_replaces_fact_slot_group_and_retains_unrelated_multi_target_fact():
    collector = AnalysisRepair(snapshot(), '腹部不适和头痛')
    original = assessment(
        complaints=[observation('腹部不适'), observation('头痛')],
        facts=[{**fact(0), 'complaintIndexes': [0, 1]}],
    )
    original['summary'] = '错误\ufffd'
    collector.ingest(encoded(original))
    repaired = assessment(
        complaints=[observation('头痛'), observation('腹部不适')],
        facts=[fact(1, '不清楚腹部不适开始时间', status='unknown'), fact(1, '不记得具体日期', status='unknown')],
    )
    result = collector.ingest(encoded(repaired))
    assert result is not None and collector.last_complete
    assert [(item.complaintIndexes, item.status) for item in result.intake.facts] == [
        ([1], 'known'),
        ([0], 'unknown'),
        ([0], 'unknown'),
    ]
    assert len({item.summary for item in result.intake.facts}) == 3


def test_partial_repair_conflicting_fact_status_drops_only_conflicting_slots():
    collector = AnalysisRepair(snapshot(), '腹部不适和头痛')
    original = assessment(
        complaints=[observation('腹部不适'), observation('头痛')],
        facts=[{**fact(0), 'complaintIndexes': [0, 1]}],
    )
    original['summary'] = '错误\ufffd'
    collector.ingest(encoded(original))
    repaired = assessment(
        complaints=[observation('腹部不适'), observation('头痛')],
        facts=[fact(0, '记不清', status='unknown')],
    )
    repaired['intent'] = 'invalid'
    result = collector.ingest(encoded(repaired))
    assert result is not None and not collector.last_complete
    assert [(item.complaintIndexes, item.status) for item in result.intake.facts] == [([1], 'known')]
    assert result.intakeSufficient is False
    # Catch-and-log paths may collect the same output again. A duplicate ingest
    # must not turn an excluded conflicting slot into accepted information.
    repeated = collector.ingest(encoded(repaired))
    assert repeated.model_dump() == result.model_dump()
    assert [(item.complaintIndexes, item.status) for item in collector.partial_intake.facts] == [([1], 'known')]


def test_partial_repair_cannot_keep_ready_when_a_valid_item_requests_clarification():
    session = snapshot()
    session['qaFlow']['concerns'] = [{'id': 'concern_a', 'status': 'pending'}]
    collector = AnalysisRepair(session, '腹部不适')
    original = assessment()
    original['summary'] = '错误\ufffd'
    original['concerns'] = [
        {
            'id': 'concern_a',
            'text': '用户疑问',
            'evidence': '腹部不适',
            'readyToAnswer': True,
            'informationNeeded': '',
        }
    ]
    collector.ingest(encoded(original))
    repaired = assessment()
    repaired['intent'] = 'invalid'
    repaired['concerns'] = [
        {
            'id': 'concern_a',
            'text': '用户疑问',
            'evidence': '腹部不适',
            'readyToAnswer': False,
            'informationNeeded': '出现时间',
        }
    ]
    result = collector.ingest(encoded(repaired))
    assert result is not None and not collector.last_complete
    assert result.concerns[0].readyToAnswer is False


def test_primary_fact_uses_current_output_flags_not_flags_retained_from_previous_output():
    collector = AnalysisRepair(snapshot(), '腹部不适和头痛')
    original = assessment(complaints=[{**observation('腹部不适'), 'primary': True}, observation('头痛')])
    original['summary'] = '错误\ufffd'
    collector.ingest(encoded(original))
    repaired = assessment(
        complaints=[observation('腹部不适'), {**observation('头痛'), 'primary': True}],
        facts=[{**fact(0), 'scope': 'primary', 'complaintIndexes': []}],
    )
    repaired['intent'] = 'invalid'
    result = collector.ingest(encoded(repaired))
    assert result is not None and not collector.last_complete
    assert result.intake.facts[0].complaintIndexes == [1]
