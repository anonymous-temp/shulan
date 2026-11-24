"""Fallback drafts preserve reliable work without inventing patient facts."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from modules.record.application import fallback
from modules.record.application import flow
from modules.record.application import interview
from modules.record.application.intake import IntakeUnderstanding
from modules.record.application.public_text import contains_internal_notice
from tests.unit._support.record_policy import short_interview_policy


def session(*, count=0, phase='intake', concerns=()):
    state = flow.new_flow()
    state['interviewPolicy'] = short_interview_policy()
    state.update(interactionCount=count, intakeCount=count, phase=phase, concerns=deepcopy(list(concerns)))
    return {'qaFlow': state, 'currentQuestion': {'questionId': 'q-original'}, 'qaHistory': [], 'answers': {}}


def concern(identity='concern-a', *, status='pending', response=''):
    return {
        'id': identity,
        'text': '用户希望了解的情况',
        'evidence': '用户实际问题',
        'status': status,
        'response': response,
        'handledCount': 0,
        'readyToAnswer': True,
        'informationNeeded': '',
    }


def decision(*, action='invite_supplement', responses=(), target=None, end_reason=None):
    return {
        'action': action,
        'responses': list(responses),
        'target': target,
        'endReason': end_reason,
        **flow.question_task(action),
    }


def quality(snapshot):
    return snapshot['qaFlow']['generationQuality']


def test_unusable_analysis_keeps_raw_answer_out_of_facts_and_preserves_snapshot():
    before = session(concerns=[concern()])
    original = deepcopy(before)
    draft = fallback.build_fallback(before, answer='未被可靠分析的原始回答', failure_kind='timeout')

    assert before == original
    assert draft['qaHistory'] == [] and draft['answers'] == {}
    expected_intake = deepcopy(original['qaFlow']['intake'])
    expected_intake['asked'].append('general:medical_history')
    assert draft['qaFlow']['intake'] == expected_intake
    assert draft['qaFlow']['concerns'] == original['qaFlow']['concerns']
    assert draft['qaFlow']['interactionCount'] == draft['qaFlow']['intakeCount'] == 1
    assert draft['qaFlow']['phase'] == 'intake'
    assert draft['qaFlow']['intakeQuestionCount'] == 1
    assert draft['currentQuestion']['intakeDimension'] == 'medical_history'
    assert quality(draft)['unprocessedQuestionIds'] == ['q-original']
    assert quality(draft)['mode'] == 'fallback'
    assert quality(draft)['consecutiveFallbacks'] == 1
    assert not contains_internal_notice(draft['currentQuestion']['assistantMessage'])
    assert quality(draft)['failureKind'] == 'timeout'
    assert '信息不足' not in draft['currentQuestion']['assistantMessage']
    assert draft['currentQuestion']['questionId'] != 'q-original'


@pytest.mark.parametrize('failure_kind', ['validation', 'timeout', 'upstream'])
@pytest.mark.parametrize('analysis_partial', [False, True])
def test_opening_fallback_never_claims_to_have_saved_an_answer_or_consumes_turn(failure_kind, analysis_partial):
    before = session()
    before['currentQuestion'] = None
    draft = fallback.build_fallback(before, failure_kind=failure_kind, analysis_partial=analysis_partial)
    current = draft['currentQuestion']
    assert current['questionType'] == 'checkbox'
    assert len(current['options']) <= flow.OPENING_MAX_OPTIONS
    assert {'便秘', '入睡困难', '减轻体重'} <= set(current['options'])
    assert not any('其他' in option or '其它' in option for option in current['options'])
    assert current['assistantMessage'] == fallback.OPENING_MESSAGE
    assert '文字或语音' in current['question']
    assert '已记录' not in current['assistantMessage'] and '已保存' not in current['assistantMessage']
    assert draft['qaFlow']['interactionCount'] == quality(draft)['consecutiveFallbacks'] == 0
    assert quality(draft)['unprocessedQuestionIds'] == []
    assert quality(draft)['mode'] == 'fallback'
    assert quality(draft)['failureKind'] == failure_kind


def test_partial_opening_preserves_valid_acknowledgement_without_appending_diagnostics():
    before = session()
    before['currentQuestion'] = None
    acknowledgement = '您好，我们从您这次最想解决的问题聊起。'
    rationale = '先了解您此次关注的内容，有助于补充报告之外的信息。'
    draft = fallback.build_fallback(
        before,
        decision=decision(action='open_intake'),
        analysis_partial=True,
        partial_presentation={
            'assistantMessage': flow.PatientMessage(
                acknowledgement=acknowledgement,
                rationale=rationale,
                evidence=[{'source': '本次报告', 'quote': '既往因消化不适就诊'}],
            ),
        },
    )
    assert draft['currentQuestion']['assistantMessage'] == acknowledgement
    assert rationale not in draft['currentQuestion']['assistantMessage']
    assert draft['qaFlow']['interactionCount'] == 0
    assert quality(draft)['mode'] == 'partial'


def two_fallbacks():
    first = fallback.build_fallback(session(concerns=[concern()]), answer='第一条内容')
    return fallback.build_fallback(first, answer='另一条内容')


def test_two_entire_fallbacks_offer_explicit_choice_with_pending_concern():
    draft = two_fallbacks()
    assert quality(draft)['consecutiveFallbacks'] == 2
    assert quality(draft)['awaitingChoice'] is True
    assert draft['currentQuestion']['options'] == [fallback.FINISH_OPTION, fallback.CONTINUE_OPTION]
    assert draft['qaFlow']['concerns'][0]['status'] == 'pending'
    assert draft['qaFlow']['concerns'][0]['handledCount'] == 0
    assert draft['qaFlow']['concerns'][0]['response'] == ''


@pytest.mark.parametrize(
    'answer', [fallback.FINISH_OPTION, [fallback.FINISH_OPTION], {'content': fallback.FINISH_OPTION}]
)
def test_explicit_finish_keeps_unprocessed_and_unanswered_pending(answer):
    before = two_fallbacks()
    final = fallback.handle_control_answer(before, answer)
    assert before['qaFlow']['concerns'][0]['status'] == 'pending'
    assert final['currentQuestion'] is None
    assert final['qaFlow']['phase'] == 'finished'
    assert final['qaFlow']['interactionCount'] == 2
    assert final['qaFlow']['concerns'][0]['status'] == 'pending'
    assert final['qaFlow']['concerns'][0]['handledCount'] == 0
    assert not contains_internal_notice(final['qaFlow']['finalMessage'])
    assert quality(final)['unprocessedQuestionIds'] == quality(before)['unprocessedQuestionIds']
    assert final['qaFlow']['finalMessage'].endswith(flow.COMPLETION_MESSAGE)
    assert quality(final)['awaitingChoice'] is False


@pytest.mark.parametrize(
    'answer',
    [
        fallback.FINISH_OPTION + '，不过还有问题',
        [fallback.FINISH_OPTION, '还有问题'],
        {'content': fallback.FINISH_OPTION, 'other': '还有问题'},
    ],
)
def test_mixed_or_custom_control_answer_is_not_an_implicit_finish(answer):
    assert fallback.handle_control_answer(two_fallbacks(), answer) is None


def test_finish_choice_has_no_effect_when_not_displayed():
    assert fallback.handle_control_answer(session(), fallback.FINISH_OPTION) is None


def test_continue_choice_preserves_streak_and_does_not_increment_count():
    before = two_fallbacks()
    draft = fallback.handle_control_answer(before, fallback.CONTINUE_OPTION)
    assert draft['qaFlow']['phase'] == 'intake'
    assert draft['qaFlow']['intakeQuestionCount'] == before['qaFlow']['intakeQuestionCount'] == 2
    assert draft['qaFlow']['interactionCount'] == 2
    assert quality(draft)['consecutiveFallbacks'] == 2
    assert quality(draft)['awaitingChoice'] is False
    assert draft['currentQuestion']['questionType'] == 'checkbox'
    assert draft['currentQuestion']['intakeDimension'] == 'family_history'
    assert draft['currentQuestion']['intakeQuestionNumber'] == 3
    assert len(draft['currentQuestion']['options']) >= 2
    assert draft['currentQuestion']['questionId'] != before['currentQuestion']['questionId']
    assert draft['qaFlow']['concerns'][0]['status'] == 'pending'


def test_hard_limit_finishes_partial_without_synthesized_clinical_answer():
    before = session(count=19, concerns=[concern(), concern('answered', status='answered', response='已有可靠回应')])
    final = fallback.build_fallback(before, answer='末轮回答')
    assert final['qaFlow']['interactionCount'] == 20
    assert final['qaFlow']['phase'] == 'finished'
    assert final['qaFlow']['endReason'] == 'interaction_limit'
    assert final['qaFlow']['concerns'][0]['status'] == 'pending'
    assert final['qaFlow']['concerns'][1]['response'] == '已有可靠回应'
    assert '末轮回答' not in str(final['qaFlow']['intake']['facts'])
    assert not contains_internal_notice(final['qaFlow']['finalMessage'])


def test_legacy_already_recorded_answer_does_not_duplicate_counters_or_history():
    before = session(count=7)
    before['qaHistory'].append({'question': deepcopy(before['currentQuestion']), 'answer': '已记录的内容'})
    result = fallback.build_fallback(before, answer='已记录的内容', answer_already_recorded=True)
    assert result['qaFlow']['interactionCount'] == result['qaFlow']['intakeCount'] == 7
    assert result['qaHistory'] == before['qaHistory']
    assert quality(result)['unprocessedQuestionIds'] == ['q-original']


def test_valid_responses_are_kept_while_missing_ones_stay_pending():
    before = session(concerns=[concern('a'), concern('b')])
    advanced = deepcopy(before)
    advanced['qaFlow']['interactionCount'] = 1
    instructions = [{'concernId': identity, 'resolution': 'answered'} for identity in ('a', 'b')]
    result = fallback.build_fallback(
        before,
        answer='回答',
        validated_draft=advanced,
        decision=decision(responses=instructions),
        partial_presentation={'responses': [flow.ConcernResponse(concernId='a', text='已经通过校验的回应')]},
    )
    first, second = result['qaFlow']['concerns']
    assert (first['status'], first['response'], first['handledCount']) == ('answered', '已经通过校验的回应', 1)
    assert (second['status'], second['response'], second['handledCount']) == ('pending', '', 0)
    assert '已经通过校验的回应' in result['currentQuestion']['assistantMessage']
    assert result['currentQuestion']['assistantMessage'] == '已经通过校验的回应'
    assert result['qaFlow']['interactionCount'] == 1
    assert quality(result)['mode'] == 'partial'
    assert quality(result)['consecutiveFallbacks'] == 0


def test_unknown_response_id_is_never_positionally_attached():
    before = session(concerns=[concern('expected')])
    result = fallback.build_fallback(
        before,
        answer='回答',
        validated_draft=deepcopy(before),
        decision=decision(responses=[{'concernId': 'expected', 'resolution': 'answered'}]),
        partial_presentation={'responses': {'unknown': flow.ConcernResponse(concernId='unknown', text='不应保留')}},
    )
    assert result['qaFlow']['concerns'][0]['status'] == 'pending'
    assert '不应保留' not in result['currentQuestion']['assistantMessage']


def test_partial_analysis_facts_and_concerns_merge_without_inventing_intent():
    before = session()
    collector = SimpleNamespace(
        partial_intake=IntakeUnderstanding(
            complaints=[],
            facts=[
                {
                    'dimension': 'habits',
                    'scope': 'general',
                    'status': 'known',
                    'summary': '用户称最近作息规律',
                    'evidence': [{'source': '本次回答', 'quote': '最近作息规律'}],
                }
            ],
        ),
        concerns=[
            flow.ConcernAssessment(
                text='想了解饮食', evidence='想了解饮食', readyToAnswer=False, informationNeeded='具体疑问'
            )
        ],
    )
    result = fallback.build_fallback(before, answer='最近作息规律，想了解饮食', partial_analysis=collector)
    assert result['qaFlow']['intake']['facts']['general:habits']['summary'] == '用户称最近作息规律'
    assert result['qaFlow']['concerns'][0]['status'] == 'pending'
    assert result['qaFlow']['interactionCount'] == 1
    assert quality(result)['mode'] == 'partial'
    assert quality(result)['consecutiveFallbacks'] == 0
    assert quality(result)['unprocessedQuestionIds'] == ['q-original']


def test_validated_gap_gets_specific_deterministic_question():
    before = session()
    advanced = deepcopy(before)
    advanced['qaFlow'].update(interactionCount=3, intakeCount=3, intakeQuestionCount=3)
    target = {
        'topic': '睡眠情况',
        'aspect': 'habits',
        'dimension': 'sleep',
        'scope': 'general',
        'complaintIds': [],
        'complaints': [],
    }
    result = fallback.build_fallback(
        before,
        answer='近期作息不规律',
        validated_draft=advanced,
        decision=decision(action='ask_intake', target=target),
    )
    assert '睡觉' in result['currentQuestion']['question']
    assert result['currentQuestion']['intakeDimension'] == 'sleep'
    assert result['qaFlow']['intakeQuestionCount'] == 3
    assert result['currentQuestion']['intakeQuestionNumber'] == 4
    assert result['qaFlow']['phase'] == 'intake'
    assert quality(result)['consecutiveFallbacks'] == 0


def test_complete_turn_resets_streak_but_does_not_erase_unprocessed_content():
    draft = two_fallbacks()
    ids = deepcopy(quality(draft)['unprocessedQuestionIds'])
    fallback.mark_generation_success(draft, repaired=True)
    assert quality(draft)['consecutiveFallbacks'] == 0
    assert quality(draft)['awaitingChoice'] is False
    assert quality(draft)['mode'] == 'repaired'
    assert quality(draft)['unprocessedQuestionIds'] == ids


def test_usable_partial_turn_interrupts_whole_fallback_streak():
    before = fallback.build_fallback(session(), answer='第一条内容')
    result = fallback.build_fallback(
        before,
        answer='其他内容',
        validated_draft=deepcopy(before),
        decision=decision(),
    )
    assert quality(result)['mode'] == 'partial'
    assert quality(result)['consecutiveFallbacks'] == 0
    next_result = fallback.build_fallback(result, answer='再补充一条')
    assert quality(next_result)['consecutiveFallbacks'] == 1
    assert quality(next_result)['awaitingChoice'] is False


def test_finishing_pending_concern_preserves_previous_valid_partial_response():
    before = two_fallbacks()
    before['qaFlow']['concerns'][0]['response'] = '此前已经说明的可靠内容'
    before['qaFlow']['concerns'][0]['responseStyleVersion'] = flow.RESPONSE_STYLE_VERSION
    final = fallback.handle_control_answer(before, fallback.FINISH_OPTION)
    saved = final['qaFlow']['concerns'][0]
    assert saved['status'] == 'pending'
    assert saved['response'] == '此前已经说明的可靠内容'
    assert '此前已经说明的可靠内容' in final['qaFlow']['finalMessage']


def test_corrupt_legacy_response_is_never_republished_by_fallback_reuse():
    before = session(concerns=[concern('a', status='answered', response='旧回复\ufffd')])
    result = fallback.build_fallback(
        before,
        answer='回答',
        validated_draft=deepcopy(before),
        decision=decision(responses=[{'concernId': 'a', 'resolution': 'answered', 'reuseResponse': '旧回复\ufffd'}]),
        partial_presentation={'responses': [flow.ConcernResponse(concernId='a', text='另一份通过校验的内容')]},
    )
    assert result['qaFlow']['concerns'][0]['status'] == 'pending'
    assert result['qaFlow']['concerns'][0]['response'] == ''
    assert '\ufffd' not in result['currentQuestion']['assistantMessage']


def test_corrupt_legacy_response_omitted_by_model_remains_pending_after_finish():
    before = session(count=20, concerns=[concern('a', status='answered', response='旧回复\ufffd')])
    result = fallback.build_fallback(
        before,
        answer='回答',
        validated_draft=deepcopy(before),
        decision=decision(
            action='finish', responses=[{'concernId': 'a', 'resolution': 'answered', 'reuseResponse': '旧回复\ufffd'}]
        ),
        partial_presentation={'responses': []},
    )
    assert result['qaFlow']['concerns'][0]['status'] == 'pending'
    assert result['qaFlow']['concerns'][0]['response'] == ''
    assert '\ufffd' not in result['qaFlow']['finalMessage']


def test_partial_analysis_with_valid_presentation_keeps_original_answer_unprocessed():
    before = session()
    advanced = deepcopy(before)
    advanced['qaFlow']['interactionCount'] = 1
    result = fallback.build_fallback(
        before,
        answer='部分尚未确认的内容',
        validated_draft=advanced,
        decision=decision(),
        analysis_partial=True,
        partial_presentation={
            'assistantMessage': flow.PatientMessage(
                acknowledgement='日常生活的不同安排值得结合起来了解。', rationale='', evidence=[]
            )
        },
    )
    assert result['qaFlow']['interactionCount'] == 1
    assert quality(result)['unprocessedQuestionIds'] == ['q-original']
    assert not contains_internal_notice(result['currentQuestion']['assistantMessage'])
    assert '日常生活的不同安排值得结合起来了解。' in result['currentQuestion']['assistantMessage']


def test_partial_analysis_cannot_finish_using_an_unconfirmed_model_stop_intent():
    before = session(phase='supplement')
    advanced = deepcopy(before)
    advanced['qaFlow']['phase'] = 'finished'
    result = fallback.build_fallback(
        before,
        answer='我还想知道其他情况',
        validated_draft=advanced,
        analysis_partial=True,
        decision=decision(action='finish', end_reason='user_stop'),
    )
    assert result['qaFlow']['phase'] == 'supplement'
    assert result['currentQuestion'] is not None
    assert quality(result)['unprocessedQuestionIds'] == ['q-original']


def test_empty_partial_intake_metadata_does_not_interrupt_whole_fallback_streak():
    first = fallback.build_fallback(session(), answer='第一条内容')
    result = fallback.build_fallback(
        first,
        answer='第二条内容',
        partial_analysis=SimpleNamespace(partial_intake=IntakeUnderstanding(complaints=[], facts=[]), concerns=[]),
    )
    assert quality(result)['consecutiveFallbacks'] == 2
    assert quality(result)['awaitingChoice'] is True


def test_no_more_after_fallback_finishes_without_exposing_internal_limitation():
    before = fallback.build_fallback(session(count=5, phase='supplement', concerns=[concern()]), answer='原始回答')
    final = fallback.handle_control_answer(before, '没有了')
    assert final['qaFlow']['phase'] == 'finished'
    assert final['qaFlow']['concerns'][0]['status'] == 'pending'
    assert final['qaFlow']['finalMessage'] == flow.COMPLETION_MESSAGE


def test_provisional_implicit_finish_does_not_discard_unanswered_concern():
    before = session(phase='supplement', concerns=[concern()])
    advanced = deepcopy(before)
    advanced['qaFlow'].update(phase='finished', endReason='no_more_questions')
    result = fallback.build_fallback(
        before,
        answer='我还没想好',
        validated_draft=advanced,
        decision=decision(action='finish', end_reason='no_more_questions'),
    )
    assert result['qaFlow']['phase'] == 'supplement'
    assert result['qaFlow']['endReason'] is None
    assert result['qaFlow']['concerns'][0]['status'] == 'pending'
    assert result['currentQuestion'] is not None


def test_legacy_symptom_target_cannot_restore_a_symptom_probe():
    before = session()
    advanced = deepcopy(before)
    advanced['qaFlow'].update(interactionCount=1, intakeCount=1, intakeQuestionCount=1)
    result = fallback.build_fallback(
        before,
        answer='有时餐后不适',
        validated_draft=advanced,
        decision=decision(action='ask_intake', target={'dimension': 'onset'}),
    )
    assert result['currentQuestion']['intakeDimension'] == 'medical_history'
    assert '什么时候开始' not in result['currentQuestion']['question']
    assert result['qaFlow']['intakeQuestionCount'] == 1


def test_no_more_shortcut_does_not_bypass_the_minimum_intake_after_one_failure():
    before = fallback.build_fallback(session(concerns=[concern()]), answer='原始回答')
    assert before['qaFlow']['phase'] == 'intake'
    assert before['qaFlow']['intakeQuestionCount'] == 1
    assert fallback.handle_control_answer(before, flow.NO_MORE_OPTION) is None


@pytest.mark.parametrize('analysis_partial', [False, True])
@pytest.mark.parametrize(
    'reason, counter, limit',
    [
        ('qa_round_limit', 'qaRoundCount', flow.MAX_QA_ROUNDS),
        ('vague_question_limit', 'qaVagueCount', flow.MAX_VAGUE_ANSWERS),
    ],
)
def test_presentation_failure_honors_qa_and_vague_limits_without_losing_unanswered_concerns(
    analysis_partial,
    reason,
    counter,
    limit,
):
    before = session(count=4, phase='qa', concerns=[concern()])
    before['qaFlow'].update(
        qaRoundCount=limit - 1 if counter == 'qaRoundCount' else 0,
        qaVagueCount=limit - 1 if counter == 'qaVagueCount' else 0,
    )
    advanced = deepcopy(before)
    advanced['qaFlow'].update(phase='finished', endReason=reason)
    advanced['qaFlow'][counter] = limit
    original = deepcopy(before)
    result = fallback.build_fallback(
        before,
        answer='本轮原始回答',
        validated_draft=advanced,
        decision=decision(
            action='finish',
            end_reason=reason,
            responses=[{'concernId': 'concern-a', 'resolution': 'limited'}],
        ),
        analysis_partial=analysis_partial,
        failure_kind='timeout',
    )
    assert before == original
    assert result['qaFlow']['phase'] == 'finished'
    assert result['currentQuestion'] is None
    assert result['qaFlow']['endReason'] == reason
    assert result['qaFlow'][counter] == limit
    assert result['qaFlow']['interactionCount'] == 4
    assert result['qaFlow']['concerns'][0]['status'] == 'pending'
    assert result['qaFlow']['concerns'][0]['handledCount'] == 0
    assert not contains_internal_notice(result['qaFlow']['finalMessage'])
    assert quality(result)['failureKind'] == 'timeout'
    assert '本轮原始回答' not in str(result['qaFlow']['intake']['facts'])
    if analysis_partial:
        assert quality(result)['unprocessedQuestionIds'] == ['q-original']


def test_unanalysed_qa_attempt_uses_last_round_without_reopening_intake():
    before = session(count=4, phase='qa', concerns=[concern()])
    before['qaFlow']['qaRoundCount'] = flow.MAX_QA_ROUNDS - 1
    result = fallback.build_fallback(before, answer='无法可靠分析的原始问题')
    assert result['qaFlow']['qaRoundCount'] == flow.MAX_QA_ROUNDS
    assert result['qaFlow']['intakeQuestionCount'] == 4
    assert result['qaFlow']['phase'] == 'finished'
    assert result['qaFlow']['endReason'] == 'qa_round_limit'
    assert quality(result)['unprocessedQuestionIds'] == ['q-original']
    assert result['qaFlow']['concerns'][0]['status'] == 'pending'
    assert result['qaFlow']['intake']['facts'] == {}


def test_late_intake_failure_transitions_to_qa_without_emitting_a_seventh_question():
    before = session(count=5, concerns=[concern()])
    before['qaFlow']['intakeQuestionCount'] = 5
    result = fallback.build_fallback(before, answer='第六题原始回答')
    assert result['qaFlow']['intakeQuestionCount'] == 6
    assert result['qaFlow']['phase'] == 'supplement'
    assert result['qaFlow']['preQaAnswered'] is True
    assert result['qaFlow']['qaRoundCount'] == 0
    assert result['currentQuestion']['options'] == [flow.NO_MORE_OPTION]
    assert result['qaFlow']['concerns'][0]['status'] == 'pending'
    assert quality(result)['unprocessedQuestionIds'] == ['q-original']


def test_known_fixed_topic_uses_source_quote_and_asks_only_for_supplement_or_correction():
    before = session(count=1)
    before['qaFlow']['intake']['facts']['general:medical_history'] = {
        'dimension': 'medical_history',
        'scope': 'general',
        'complaintIds': [],
        'status': 'known',
        'summary': '已有疾病史',
        'evidence': [{'source': '本次报告', 'quote': '既往医生确诊高血压'}],
    }
    target = interview.fixed_target(before['qaFlow'], 1)
    result = fallback.build_fallback(
        before,
        validated_draft=deepcopy(before),
        decision=decision(action='ask_intake', target=target),
    )
    question = result['currentQuestion']
    assert '既往医生确诊高血压' in question['question']
    assert '补充或更正' in question['question']
    assert '曾经被医生明确诊断过哪些疾病' not in question['question']
    assert question['intakeDimension'] == 'medical_history'
    assert all('不想' not in option and '跳过' not in option for option in question['options'])


def test_qa_continue_choice_keeps_qa_phase_and_does_not_reset_its_budget():
    before = session(count=4, phase='qa', concerns=[concern()])
    before['qaFlow']['qaRoundCount'] = 1
    first = fallback.build_fallback(before, answer='第一次未分析的QA问题')
    second = fallback.build_fallback(first, answer='第二次未分析的QA问题')
    assert quality(second)['resumePhase'] == 'qa'
    assert second['qaFlow']['qaRoundCount'] == 3
    continued = fallback.handle_control_answer(second, fallback.CONTINUE_OPTION)
    assert continued['qaFlow']['phase'] == 'qa'
    assert continued['currentQuestion']['phase'] == 'qa'
    assert continued['qaFlow']['qaRoundCount'] == 3
    assert continued['qaFlow']['interactionCount'] == second['qaFlow']['interactionCount']
    assert continued['qaFlow']['concerns'][0]['status'] == 'pending'
    assert quality(continued)['unprocessedQuestionIds'] == quality(second)['unprocessedQuestionIds']


def test_partial_qa_presentation_does_not_return_an_active_qa_to_supplement():
    before = session(count=4, phase='qa', concerns=[concern()])
    before['qaFlow']['qaRoundCount'] = 2
    result = fallback.build_fallback(
        before,
        answer='已经记录的QA原话',
        validated_draft=deepcopy(before),
        decision=decision(action='invite_supplement'),
        analysis_partial=True,
    )
    assert result['qaFlow']['phase'] == 'qa'
    assert result['currentQuestion']['phase'] == 'qa'
    assert result['qaFlow']['qaRoundCount'] == 2
    assert result['qaFlow']['concerns'][0]['status'] == 'pending'
    assert quality(result)['unprocessedQuestionIds'] == ['q-original']


def _budget_response(index):
    return ''.join(
        f'第{index}组资料中的第{item}项情况已按您提供的内容记录，仍需结合完整资料理解。' for item in range(1, 11)
    )


@pytest.mark.parametrize('closing', [False, True])
def test_partial_presentation_keeps_complete_responses_within_budget_and_preserves_overflow(closing):
    identities = ('budget-a', 'budget-b', 'budget-c')
    concerns = [concern(identity) for identity in identities]
    concerns[2]['text'] = '原始长问题仍须完整保留' * 30
    before = session(count=4, phase='qa', concerns=concerns)
    before['qaFlow']['qaRoundCount'] = 4
    before['qaHistory'] = [{'question': {'questionId': 'raw-question'}, 'answer': '此前保存的原始回答'}]
    original = deepcopy(before)
    advanced = deepcopy(before)
    if closing:
        advanced['qaFlow'].update(phase='finished', qaRoundCount=5, endReason='qa_round_limit')
    texts = [_budget_response(index) for index in range(3)]
    assert all(len(text) <= flow.MAX_RESPONSE_CHARS for text in texts)
    assert len(texts[0]) + len(texts[1]) <= flow.MAX_TURN_RESPONSE_CHARS < sum(map(len, texts))
    result = fallback.build_fallback(
        before,
        answer='本轮原始回答不变',
        validated_draft=advanced,
        decision=decision(
            action='finish' if closing else 'invite_supplement',
            end_reason='qa_round_limit' if closing else None,
            responses=[{'concernId': identity, 'resolution': 'answered'} for identity in identities],
        ),
        partial_presentation={
            'responses': [
                flow.ConcernResponse(concernId=identity, text=text) for identity, text in zip(identities, texts)
            ],
        },
    )
    assert before == original
    assert result['qaHistory'] == before['qaHistory']
    saved = result['qaFlow']['concerns']
    assert [item['handledCount'] for item in saved] == [1, 1, 0]
    assert saved[0]['response'] == texts[0] and saved[1]['response'] == texts[1]
    assert saved[2]['status'] == 'pending'
    assert saved[2]['text'] == concerns[2]['text']
    visible = result['qaFlow']['finalMessage'] if closing else result['currentQuestion']['assistantMessage']
    assert texts[0] in visible and texts[1] in visible
    assert texts[2] not in visible
    assert concerns[2]['text'] not in visible
    assert not contains_internal_notice(visible)


def test_partial_finish_caps_complete_old_partial_responses_and_keeps_unanswered_pending():
    before = two_fallbacks()
    before['qaFlow']['concerns'] = [
        concern(f'old-partial-{index}', response=_budget_response(index)) for index in range(5)
    ]
    for item in before['qaFlow']['concerns']:
        item['responseStyleVersion'] = flow.RESPONSE_STYLE_VERSION
    final = fallback.handle_control_answer(before, fallback.FINISH_OPTION)
    assert all(item['status'] == 'pending' and item['handledCount'] == 0 for item in final['qaFlow']['concerns'])
    assert all(_budget_response(index) in item['response'] for index, item in enumerate(final['qaFlow']['concerns']))
    shown = [
        _budget_response(index) for index in range(5) if _budget_response(index) in final['qaFlow']['finalMessage']
    ]
    assert sum(map(len, shown)) <= flow.MAX_TURN_RESPONSE_CHARS
    assert len(shown) < 5
    assert not contains_internal_notice(final['qaFlow']['finalMessage'])


def test_known_topic_does_not_cut_a_long_source_quote_before_its_exclusion():
    before = session(count=1)
    quote = '此前曾考虑高血压；' + '其他检查资料均需一并核对；' * 30 + '最后已排除高血压。'
    assert len(quote) > 300
    before['qaFlow']['intake']['facts']['general:medical_history'] = {
        'dimension': 'medical_history',
        'scope': 'general',
        'complaintIds': [],
        'status': 'known',
        'summary': '报告既往诊断过程',
        'evidence': [{'source': '本次报告', 'quote': quote}],
    }
    original = deepcopy(before)
    target = interview.fixed_target(before['qaFlow'], 1)
    result = fallback.build_fallback(
        before,
        validated_draft=deepcopy(before),
        decision=decision(action='ask_intake', target=target),
    )
    assert before == original
    question = result['currentQuestion']['question']
    assert quote[:300] not in question
    assert '此前曾考虑高血压' not in question
    assert '补充或更正' in question
    assert result['qaFlow']['intake']['facts']['general:medical_history']['evidence'][0]['quote'] == quote
