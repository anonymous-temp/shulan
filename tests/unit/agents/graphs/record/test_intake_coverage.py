"""Clinical intake trajectories: meaningful breadth, source fidelity and bounds."""

from copy import deepcopy

import pytest

from modules.record.application import flow
from modules.record.application import intake
from tests.unit._support.record_policy import short_interview_policy

from .test_dynamic_record import assessment
from .test_dynamic_record import concern


def session():
    legacy = flow.new_flow()
    legacy['interviewPolicy'] = short_interview_policy()
    return {'qaFlow': legacy, 'currentQuestion': {'questionId': 'q0'}, 'qaHistory': []}


def gap(dimension, *, topic='餐后不适', scope='primary', essential=False):
    aspect = dimension if dimension in flow.ASPECTS else 'symptoms'
    if dimension == 'medication':
        aspect = 'medical_history'
    return {
        'topic': topic,
        'aspect': aspect,
        'dimension': dimension,
        'scope': scope,
        'essential': essential,
        'reason': '结合餐后不适了解相关背景',
        'informationNeeded': intake.DIMENSION_LABELS[dimension],
    }


def fact(dimension, quote, *, status='known', scope='primary', source='本次回答'):
    return {
        'dimension': dimension,
        'scope': scope,
        'status': status,
        'summary': quote,
        'evidence': [{'source': source, 'quote': quote}],
    }


def model(snapshot, answer, gaps, *, facts=(), sufficient=False, concerns=()):
    state = snapshot['qaFlow'].get('intake') or intake.new_intake()
    understanding = {
        'complaints': [
            {
                'id': state['primaryId'],
                'text': '餐后不适',
                'primary': True,
                'evidence': [{'source': '本次回答', 'quote': answer}],
            }
        ],
        'facts': list(facts),
        'priorityClarificationNeeded': False,
    }
    return flow.PatientAssessment(
        **assessment(gaps=gaps, intake=understanding, sufficient=sufficient, concerns=concerns)
    )


def advance(snapshot, parsed, answer, *, sources=None):
    flow.validate_assessment(parsed, snapshot, answer, sources=sources)
    draft, decision = flow.advance_flow(snapshot, parsed, answer=answer)
    question = (
        None
        if decision['action'] in ('finish', 'invite_supplement', 'clarify_supplement')
        else {
            'question': decision['target']['informationNeeded'] + '？',
            'questionType': 'radio',
            'options': ['有', '没有', '记不清'],
        }
    )
    presentation = flow.TurnPresentation(
        assistantMessage={
            'acknowledgement': '饮食和活动等背景值得结合起来了解。',
            'rationale': '了解相关背景。',
            'evidence': [],
        },
        responses=[
            {
                'concernId': item['concernId'],
                'text': (
                    '饮食安排可以从食物搭配做起，尽量保持三餐时间稳定。',
                    '日常活动可以从步行开始，逐步形成适合自己的固定安排。',
                    '休息时可以留出放松时间，尽量保持稳定的睡眠作息。',
                )[index % 3],
            }
            for index, item in enumerate(decision['responses'])
        ],
        question=question,
    )
    flow.apply_presentation(draft, decision, presentation, '')
    draft['qaHistory'].append({'question': snapshot['currentQuestion'], 'answer': answer})
    return draft, decision


@pytest.mark.parametrize(
    ('history_count', 'legacy_count', 'substantive', 'action'),
    [
        (4, 4, True, 'ask_intake'),
        (5, 5, True, 'invite_supplement'),
        (5, 0, False, 'invite_supplement'),
        (0, 5, False, 'invite_supplement'),
        (0, 17, True, 'invite_supplement'),
    ],
)
def test_intake_limit_counts_saved_answers_with_legacy_fallback(history_count, legacy_count, substantive, action):
    snapshot = session()
    snapshot['qaHistory'] = [{'answer': '记不清'} for _ in range(history_count)]
    snapshot['qaFlow'].update(intakeCount=legacy_count, interactionCount=legacy_count)
    answer = '记不清'
    parsed = model(snapshot, answer, [gap('activity', scope='general', essential=True)])
    parsed.intent = 'answer' if substantive else 'unclear'
    parsed.substantive = substantive
    original = deepcopy(snapshot)
    draft, decision = advance(snapshot, parsed, answer)
    assert decision['action'] == action
    assert snapshot == original
    # Preserve the existing public/global counter's meaning. The hard intake
    # boundary also includes successful answers the model calls non-substantive.
    assert draft['qaFlow']['intakeCount'] == legacy_count + int(substantive)
    assert len(draft['qaHistory']) == history_count + 1


@pytest.mark.parametrize('history_count', [3, 4, 5])
def test_every_normal_intake_exit_invites_supplement_before_pending_clarification(history_count):
    snapshot = session()
    snapshot['qaHistory'] = [{'answer': '已有回答'} for _ in range(history_count)]
    answer = '想了解为什么饭后不舒服'
    parsed = model(snapshot, answer, [], sufficient=True, concerns=[concern(answer, ready=False)])
    draft, decision = advance(snapshot, parsed, answer)
    assert decision['action'] == 'invite_supplement'
    assert draft['qaFlow']['phase'] == 'supplement'
    assert draft['qaFlow']['concerns'][0]['status'] == 'limited'
    assert draft['qaFlow']['concerns'][0]['response']
    assert draft['qaFlow']['qaRoundCount'] == 0
    assert draft['currentQuestion']['options'] == ['没有了']


@pytest.mark.parametrize('intent', ['answer', 'unclear', 'no_more'])
@pytest.mark.parametrize('selection', ['没有了', ['没有了'], {'content': '没有了'}])
def test_no_more_option_finishes_pending_concerns_without_clarification_or_count(intent, selection):
    snapshot = session()
    answer = '想了解为什么饭后不舒服'
    snapshot, _ = advance(snapshot, model(snapshot, answer, [], concerns=[concern(answer, ready=False)]), answer)
    snapshot['qaFlow']['phase'] = 'supplement'
    identity = snapshot['qaFlow']['concerns'][0]['id']
    parsed = flow.PatientAssessment(
        **assessment(intent=intent, substantive=False, concerns=[concern(answer, ready=False, identity=identity)])
    )
    previous_count = snapshot['qaFlow']['interactionCount']
    assert flow.finish_without_more(snapshot, selection) is None
    draft, decision = advance(snapshot, parsed, selection)
    assert decision['action'] == 'finish'
    assert decision['endReason'] == 'no_more_questions'
    assert decision['responses'][0]['resolution'] == 'limited'
    assert draft['qaFlow']['concerns'][0]['status'] == 'limited'
    assert draft['qaFlow']['interactionCount'] == previous_count
    assert draft['currentQuestion'] is None


def test_relevant_history_can_follow_an_unavailable_symptom_description():
    snapshot = session()
    options = [gap(d) for d in ('character', 'frequency', 'associated_symptoms', 'onset', 'medical_history')]
    answer = '吃完东西偶尔会不舒服'
    snapshot, decision = advance(snapshot, model(snapshot, answer, options), answer)
    assert decision['target']['dimension'] == 'medical_history'
    identity = snapshot['qaFlow']['intake']['primaryId']
    answer = '说不清具体感受，只是不舒服，有一点点恶心'
    options = [gap('medical_history'), *options]
    snapshot, decision = advance(
        snapshot, model(snapshot, answer, options, facts=[fact('character', answer, status='unknown')]), answer
    )
    assert decision['target']['dimension'] == 'family_history'
    assert snapshot['qaFlow']['intake']['primaryId'] == identity
    answer = '以前有类似不适，曾经就诊'
    snapshot, decision = advance(
        snapshot, model(snapshot, answer, options, facts=[fact('medical_history', answer)], sufficient=True), answer
    )
    assert decision['target']['dimension'] in intake.LIFESTYLE_DIMENSIONS
    assert snapshot['qaFlow']['interactionCount'] == 3
    snapshot, decision = advance(snapshot, model(snapshot, '平时走路上下班', [], sufficient=True), '平时走路上下班')
    assert decision['action'] == 'invite_supplement'


def test_renaming_topics_does_not_repeat_a_lifestyle_purpose():
    snapshot = session()
    snapshot['qaFlow'].update(intakeQuestionCount=2, intakeCount=2, interactionCount=2)
    for dimension in ('diet_structure', 'activity'):
        answer = f'回答了{dimension}'
        snapshot, decision = advance(snapshot, model(snapshot, answer, [gap(dimension, scope='general')]), answer)
        assert decision['target']['dimension'] == dimension
    answer = '说不清具体是哪种情况'
    options = [gap('diet_structure', topic='吃饭习惯', scope='general'), gap('sleep', scope='general', essential=True)]
    _, decision = advance(snapshot, model(snapshot, answer, options), answer)
    assert decision['target']['dimension'] == 'sleep'


@pytest.mark.parametrize('status', ['known', 'unknown', 'declined'])
def test_answered_unavailable_and_declined_information_is_not_asked_again(status):
    snapshot = session()
    answer = '我记不清出现时间，不想再说这个'
    options = [gap('onset'), gap('medical_history')]
    snapshot, decision = advance(
        snapshot, model(snapshot, answer, options, facts=[fact('onset', answer, status=status)]), answer
    )
    assert decision['target']['dimension'] == 'medical_history'


def test_report_history_is_used_for_supplementation_and_cannot_become_a_chief_complaint():
    snapshot = session()
    answer = '饭后有点恶心'
    report = '既往病史：慢性胃炎'
    parsed = model(
        snapshot,
        answer,
        [gap('medical_history'), gap('family_history')],
        facts=[fact('medical_history', report, scope='general', source='本次报告')],
    )
    _, decision = advance(snapshot, parsed, answer, sources={'本次报告': report})
    assert decision['target']['dimension'] == 'medical_history'
    assert decision['target']['supplementation']
    assert decision['target']['knownFacts'][0]['summary'] == report
    parsed.intake.complaints[0].evidence[0].source = '本次报告'
    parsed.intake.complaints[0].evidence[0].quote = report
    with pytest.raises(ValueError, match='不能将报告发现当作主诉'):
        flow.validate_assessment(parsed, snapshot, answer, sources={'本次报告': report})


@pytest.mark.parametrize('source', ['本次回答', '历史回答', '本次报告', '长期画像', '附带资料'])
def test_intake_accepts_faithful_paraphrases_from_available_sources(source):
    snapshot = session()
    original = '以前就因胃部不舒服看过医生。\n当时诊断是慢性胃炎。'
    snapshot['qaHistory'] = [{'answer': {'content': original}}]
    paraphrase = '曾因胃部不适就医，被诊断为慢性胃炎'
    parsed = flow.PatientAssessment(
        **assessment(
            intake={
                'complaints': [],
                'priorityClarificationNeeded': False,
                'facts': [fact('medical_history', paraphrase, scope='general', source=source)],
            }
        )
    )
    assert paraphrase not in original
    flow.validate_assessment(
        parsed,
        snapshot,
        {'content': original},
        sources={'本次报告': original, '长期健康画像': original, '附带健康资料': original},
    )


def test_complaint_and_fact_accept_non_contiguous_summary_of_user_answer():
    snapshot = session()
    original = '我吃完饭会觉得胃里难受。最近工作比较忙。还有一点恶心。'
    parsed = model(
        snapshot,
        '餐后胃部不适，伴轻微恶心',
        [gap('medical_history')],
        facts=[fact('character', '吃完饭胃部难受，还有一点恶心')],
    )
    flow.validate_assessment(parsed, snapshot, original)


@pytest.mark.parametrize('source', ['本次回答', '历史回答', '本次报告', '长期画像', '附带资料'])
@pytest.mark.parametrize('empty', [None, ' \n\t ', {'content': ['', None, 3]}])
def test_intake_requires_actual_text_in_the_selected_source(source, empty):
    snapshot = session()
    original = '既往诊断为慢性胃炎'
    answer = empty if source == '本次回答' else original
    snapshot['qaHistory'] = [
        {'answer': empty if source == '历史回答' else original, 'question': {'assistantMessage': original}}
    ]
    sources = {'本次报告': original, '长期健康画像': original, '附带健康资料': original}
    source_key = {'本次报告': '本次报告', '长期画像': '长期健康画像', '附带资料': '附带健康资料'}.get(source)
    if source_key:
        sources[source_key] = empty
    parsed = flow.PatientAssessment(
        **assessment(
            intake={
                'complaints': [],
                'priorityClarificationNeeded': False,
                'facts': [fact('medical_history', original, scope='general', source=source)],
            }
        )
    )
    with pytest.raises(ValueError, match='指定来源必须有实际文本内容') as raised:
        flow.validate_assessment(parsed, snapshot, answer, sources=sources)
    assert raised.value.code == 'intake_source_unavailable'


def test_ai_explanations_do_not_make_historical_user_source_available():
    snapshot = session()
    snapshot['qaHistory'] = [{'answer': None, 'question': {'assistantMessage': '既往有胃炎'}}]
    parsed = model(
        snapshot, '不清楚', [gap('medical_history')], facts=[fact('medical_history', '既往有胃炎', source='历史回答')]
    )
    with pytest.raises(ValueError, match='指定来源必须有实际文本内容'):
        flow.validate_assessment(parsed, snapshot, '不清楚')


@pytest.mark.parametrize('source', ['本次报告', '长期画像', '附带资料'])
def test_non_user_sources_cannot_establish_a_complaint_even_when_available(source):
    snapshot = session()
    parsed = model(snapshot, '胃部不适', [gap('medical_history')])
    parsed.intake.complaints[0].evidence[0].source = source
    with pytest.raises(ValueError, match='不能将报告发现当作主诉'):
        flow.validate_assessment(
            parsed,
            snapshot,
            '胃部不适',
            sources={'本次报告': '胃部不适', '长期健康画像': '胃部不适', '附带健康资料': '胃部不适'},
        )


@pytest.mark.parametrize('dimension', ['chief_complaint', 'priority', 'goal'])
@pytest.mark.parametrize('source', ['本次报告', '长期画像', '附带资料'])
def test_patient_priorities_and_goals_still_require_user_sources(dimension, source):
    snapshot = session()
    parsed = model(
        snapshot,
        '胃部不适',
        [gap('medical_history')],
        facts=[fact(dimension, '想先了解胃部不适', source=source)],
    )
    with pytest.raises(ValueError, match='主诉、优先级与诉求只能依据用户陈述'):
        flow.validate_assessment(
            parsed,
            snapshot,
            '胃部不适',
            sources={'本次报告': '胃部不适', '长期健康画像': '胃部不适', '附带健康资料': '胃部不适'},
        )


def test_initial_pending_concerns_can_use_a_remaining_essential_gap_after_five_interactions():
    snapshot = session()
    snapshot['qaFlow'].update(intakeCount=4, interactionCount=4)
    answer = '餐后恶心是什么原因'
    parsed = model(
        snapshot, answer, [gap('sleep', scope='general', essential=True)], concerns=[concern(answer, ready=False)]
    )
    snapshot, decision = advance(snapshot, parsed, answer)
    assert decision['action'] == 'ask_intake'
    assert snapshot['qaFlow']['intakeCount'] == 5
    assert snapshot['qaFlow']['concerns'][0]['status'] == 'pending'
    assert decision['target']['dimension'] == 'sleep'
    identity = snapshot['qaFlow']['concerns'][0]['id']
    answer = '近两周开始'
    snapshot, decision = advance(
        snapshot,
        model(
            snapshot,
            answer,
            [gap('sleep', scope='general', essential=True), gap('habits')],
            facts=[fact('sleep', answer, scope='general')],
            concerns=[concern('餐后恶心是什么原因', identity=identity)],
        ),
        answer,
    )
    assert decision['action'] == 'invite_supplement'
    assert snapshot['qaFlow']['intakeCount'] == 6
    assert decision['responses'][0]['resolution'] == 'answered'


def test_optional_priority_is_not_repeated_when_the_user_has_no_preference():
    snapshot = session()
    answer = '饭后不舒服，同时也想了解体重变化'
    parsed = model(snapshot, answer, [gap('priority', scope='general'), gap('character')])
    parsed.intake.complaints[0].primary = False
    parsed.intake.priorityClarificationNeeded = True
    snapshot, decision = advance(snapshot, parsed, answer)
    assert decision['target']['dimension'] == 'medical_history'
    answer = '两个都想了解，没有哪个更重要'
    parsed = model(snapshot, answer, [gap('chief_complaint'), gap('priority'), gap('medical_history')])
    parsed.intake.priorityClarificationNeeded = True
    snapshot, decision = advance(snapshot, parsed, answer)
    assert decision['target']['dimension'] == 'family_history'
    assert snapshot['qaFlow']['intake']['priorityAsked'] is False
    assert snapshot['qaFlow']['intake']['preferredId'] is None


def test_symptom_conflicts_are_saved_without_opening_new_symptom_questions():
    snapshot = session()
    answer = '刚才说偶尔，但实际每天都有'
    parsed = model(snapshot, answer, [gap('frequency')], facts=[fact('frequency', answer, status='conflicting')])
    snapshot, decision = advance(snapshot, parsed, answer)
    assert decision['target']['dimension'] == 'medical_history'
    _, decision = advance(
        snapshot,
        model(
            snapshot,
            answer,
            [gap('frequency'), gap('medical_history')],
            facts=[fact('frequency', answer, status='conflicting')],
        ),
        answer,
    )
    assert decision['target']['dimension'] == 'family_history'
    assert snapshot['qaFlow']['intake']['conflictAsked'] == []
    assert any(item['status'] == 'conflicting' for item in snapshot['qaFlow']['intake']['facts'].values())


def test_legacy_snapshot_remains_untouched_until_a_valid_new_turn():
    snapshot = session()
    del snapshot['qaFlow']['intake']
    snapshot['qaFlow']['focus'] = {'topic': '腹胀', 'aspect': 'symptoms', 'depth': 2}
    before = deepcopy(snapshot)
    answer = '饭后不适'
    updated, decision = advance(snapshot, model(snapshot, answer, [gap('onset'), gap('medical_history')]), answer)
    assert snapshot == before
    assert decision['target']['dimension'] == 'medical_history'
    assert updated['qaFlow']['version'] == 2


def test_history_for_an_old_complaint_does_not_cover_a_new_main_complaint():
    snapshot = session()
    answer = '以前因餐后不适就诊'
    snapshot, _ = advance(
        snapshot, model(snapshot, answer, [gap('family_history')], facts=[fact('medical_history', answer)]), answer
    )
    previous = snapshot['qaFlow']['intake']['primaryId']
    answer = '这次更希望了解体重变化'
    parsed = model(snapshot, answer, [gap('medical_history', topic='体重变化')])
    parsed.intake.complaints[0].id = None
    parsed.intake.complaints[0].text = '体重变化'
    updated, decision = advance(snapshot, parsed, answer)
    assert updated['qaFlow']['intake']['primaryId'] != previous
    assert decision['target']['dimension'] == 'family_history'
    known = intake.patient_projection(updated['qaFlow'])['已了解的信息']
    assert len(known) == 1
    assert known[0]['困扰'] == [{'编号': previous, '困扰': '餐后不适'}]


def test_changing_background_scope_does_not_repeat_the_same_purpose():
    snapshot = session()
    snapshot['qaFlow'].update(intakeQuestionCount=2, intakeCount=2, interactionCount=2)
    answer = '饭后不适'
    snapshot, _ = advance(snapshot, model(snapshot, answer, [gap('activity')]), answer)
    _, decision = advance(
        snapshot, model(snapshot, answer, [gap('activity', scope='general'), gap('sleep', scope='general')]), answer
    )
    assert decision['target']['dimension'] == 'sleep'


def test_twentieth_turn_finishes_after_preserving_split_subfacts_and_all_three_concerns():
    snapshot = session()
    snapshot['qaFlow'].update(phase='qa', intakeCount=5, interactionCount=19)
    answer = '餐后不适但没有腹痛、发热或便血。想知道和生活习惯的关系、能否散步以及益生菌有没有帮助。'
    symptom_summaries = ['没有腹痛', '没有发热或便血']
    goal_summaries = ['想了解与生活习惯的关系', '想知道是否适合散步', '想了解益生菌是否有帮助']
    facts = [fact('associated_symptoms', summary) for summary in symptom_summaries]
    facts.extend(fact('goal', summary) for summary in goal_summaries)
    parsed = model(snapshot, answer, [], facts=facts, concerns=[concern(item) for item in goal_summaries])
    updated, decision = advance(snapshot, parsed, answer)
    assert decision['action'] == 'finish'
    assert len(decision['responses']) == 3
    assert updated['qaFlow']['interactionCount'] == 20
    identity = updated['qaFlow']['intake']['complaints'][0]['id']
    saved = updated['qaFlow']['intake']['facts']
    assert saved[f'{identity}:associated_symptoms']['summary'] == '\n'.join(symptom_summaries)
    assert saved[f'{identity}:goal']['summary'] == '\n'.join(goal_summaries)
    assert [item['quote'] for item in saved[f'{identity}:goal']['evidence']] == goal_summaries
