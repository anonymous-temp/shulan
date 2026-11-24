"""Lifestyle relevance wins first; equally useful questions broaden the interview."""

import json
from copy import deepcopy

import pytest

from modules.record.application import fallback
from modules.record.application import flow
from modules.record.application import intake
from modules.record.application import interview
from modules.record.application.output_repair import AnalysisRepair

from .test_dynamic_record import assessment
from .test_dynamic_record import cached
from .test_dynamic_record import setup_manager
from .test_dynamic_record import submit
from .test_intake_coverage import fact
from .test_intake_quality import lifestyle_gap
from .test_intake_quality import snapshot
from .test_intake_quality import step


def gap(dimension, *, relevance='general', essential=False):
    return {**lifestyle_gap(dimension, essential=essential), 'relevance': relevance}


def first_lifestyle(dimension='diet_structure'):
    state, _ = step(snapshot())
    for answer in ('希望了解日常生活如何安排', '没有已知疾病', '不清楚血亲的疾病史'):
        state, decision = step(state, answer, gaps=[gap(dimension)])
    assert state['qaFlow']['intakeQuestionCount'] == 3
    assert decision['target']['dimension'] == dimension
    return state


@pytest.mark.parametrize('relevance', ['general', 'focused'])
@pytest.mark.parametrize(
    ('first', 'same_category'),
    [
        ('diet_structure', 'meal_pattern'),
        ('diet_structure', 'hydration'),
        ('meal_pattern', 'diet_structure'),
        ('activity', 'sedentary'),
        ('smoking', 'alcohol'),
    ],
)
def test_equally_relevant_candidates_switch_categories_instead_of_continuing_the_same_branch(
    first, same_category, relevance
):
    before = first_lifestyle(first)
    after, decision = step(
        before,
        '已说明当前习惯',
        gaps=[gap(same_category, relevance=relevance), gap('sleep', relevance=relevance)],
    )
    assert decision['target']['dimension'] == 'sleep'
    assert decision['target']['relevance'] == relevance
    assert after['qaFlow']['intakeQuestionCount'] == 4
    assert after['qaFlow']['qaRoundCount'] == 0


def test_focused_same_category_can_outweigh_a_general_new_category():
    before = first_lifestyle()
    _, decision = step(
        before,
        '已经说明饮食搭配',
        gaps=[gap('activity'), gap('meal_pattern', relevance='focused')],
    )
    assert decision['target']['dimension'] == 'meal_pattern'
    assert decision['target']['relevance'] == 'focused'


def test_original_model_order_breaks_ties_between_unasked_categories():
    before = first_lifestyle()
    _, decision = step(
        before,
        '已经说明饮食搭配',
        gaps=[gap('hydration'), gap('stress_coping'), gap('sleep'), gap('activity')],
    )
    assert decision['target']['dimension'] == 'stress_coping'


def test_essential_does_not_replace_relevance_and_only_grants_a_sixth_slot():
    before = first_lifestyle()
    fifth, decision = step(
        before,
        '已经说明饮食搭配',
        gaps=[gap('hydration', essential=True), gap('sleep', relevance='focused')],
    )
    assert decision['target']['dimension'] == 'sleep'
    sixth, decision = step(fifth, '作息规律', gaps=[gap('hydration', essential=True)])
    assert decision['target']['dimension'] == 'hydration'
    assert sixth['qaFlow']['intakeQuestionCount'] == 5
    complete, decision = step(sixth, '平时喝白水', gaps=[gap('activity', essential=True)])
    assert decision['action'] == 'invite_supplement'
    assert complete['qaFlow']['intakeQuestionCount'] == 6
    assert complete['qaFlow']['qaRoundCount'] == 0


@pytest.mark.parametrize('dimension', ['sleep', 'stress_coping'])
@pytest.mark.parametrize('status', ['known', 'unknown', 'declined'])
def test_known_unknown_and_declined_details_are_not_reasked_to_obtain_a_new_category(dimension, status):
    before = first_lifestyle()
    answer = {'known': '已经说明这一方面', 'unknown': '这一方面记不清', 'declined': '这一方面不想说明'}[status]
    _, decision = step(
        before,
        answer,
        facts=[fact(dimension, answer, scope='general', status=status)],
        gaps=[gap(dimension, relevance='focused'), gap('activity')],
    )
    assert decision['target']['dimension'] == 'activity'


def test_old_candidate_without_relevance_stays_general_and_focused_requires_an_explanation():
    old = flow.InformationGap.model_validate(lifestyle_gap('activity'))
    assert old.relevance == 'general'
    localized = flow.InformationGap.model_validate({**lifestyle_gap('sleep'), '相关性': 'focused'})
    assert localized.relevance == 'focused'
    with pytest.raises(ValueError):
        flow.InformationGap.model_validate({**gap('sleep', relevance='focused'), 'reason': ''})
    with pytest.raises(ValueError):
        flow.InformationGap.model_validate({**lifestyle_gap('activity'), 'relevance': 'highest'})


def test_metadata_drives_coverage_without_counting_an_unpublished_working_choice():
    published = first_lifestyle('meal_pattern')
    original = deepcopy(published)
    working = deepcopy(published)
    working['qaFlow']['intake']['asked'].append('general:activity')
    assert interview.asked_lifestyle_dimensions(working) == {'meal_pattern'}
    assert interview.asked_lifestyle_categories(working) == {'nutrition'}
    result = fallback.build_fallback(published, answer='进餐时间不固定', validated_draft=working)
    assert result['currentQuestion']['intakeDimension'] == 'activity'
    assert published == original
    assert result['qaFlow']['intakeQuestionCount'] == 4


def test_legacy_questions_without_dimension_metadata_use_persisted_coverage_conservatively():
    before = first_lifestyle('meal_pattern')
    for item in before['qaHistory']:
        item['question'].pop('intakeDimension', None)
    before['currentQuestion'].pop('intakeDimension', None)
    original = deepcopy(before)
    assert interview.asked_lifestyle_dimensions(before) == {'meal_pattern'}
    assert interview.asked_lifestyle_categories(before) == {'nutrition'}
    after = fallback.build_fallback(before, answer='已说明进餐习惯')
    assert after['currentQuestion']['intakeDimension'] == 'activity'
    assert before == original


def test_broad_legacy_habits_are_not_guessed_into_a_specific_lifestyle_category():
    before = first_lifestyle()
    before['qaHistory'] = []
    before['currentQuestion'] = {'questionId': 'old-habits', 'phase': 'intake'}
    before['qaFlow']['intake']['asked'] = ['general:habits']
    assert interview.asked_lifestyle_categories(before) == set()


def test_normal_and_whole_fallback_defaults_skip_the_previously_asked_category():
    before = first_lifestyle('meal_pattern')
    normal, decision = step(before, '进餐时间不固定', gaps=[])
    degraded = fallback.build_fallback(before, answer='进餐时间不固定', failure_kind='timeout')
    assert decision['target']['dimension'] == 'activity'
    assert normal['currentQuestion']['intakeDimension'] == degraded['currentQuestion']['intakeDimension'] == 'activity'
    assert normal['qaFlow']['intakeQuestionCount'] == degraded['qaFlow']['intakeQuestionCount'] == 4
    assert degraded['qaFlow']['intake']['facts'] == before['qaFlow']['intake']['facts']
    assert '报告异常' not in degraded['currentQuestion']['assistantMessage']


@pytest.mark.parametrize('with_partial_question', [False, True])
def test_validated_partial_presentation_keeps_the_same_cross_category_choice(with_partial_question):
    before = first_lifestyle()
    parsed = flow.PatientAssessment(**assessment(gaps=[gap('meal_pattern'), gap('stress_coping')]))
    draft, decision = flow.advance_flow(before, parsed, answer='饮食搭配已经说明')
    assert decision['target']['dimension'] == 'stress_coping'
    partial = (
        {
            'question': flow.GeneratedQuestion(
                question='工作忙碌时，您通常用什么方式放松？',
                questionType='radio',
                options=['散步或运动', '休息或做喜欢的事', '听音乐'],
            )
        }
        if with_partial_question
        else None
    )
    result = fallback.build_fallback(
        before,
        answer='饮食搭配已经说明',
        validated_draft=draft,
        decision=decision,
        partial_presentation=partial,
    )
    assert result['currentQuestion']['intakeDimension'] == 'stress_coping'
    assert '焦虑' not in result['currentQuestion']['question']
    assert '抑郁' not in result['currentQuestion']['question']
    assert result['qaFlow']['intakeQuestionCount'] == 4


@pytest.mark.parametrize('include_empty_intake', [False, True])
def test_partial_analysis_uses_the_same_ranked_candidates_instead_of_discarding_their_relevance(
    include_empty_intake,
):
    before = first_lifestyle()
    collector = AnalysisRepair(before, '已经说明饮食搭配')
    assert (
        collector.ingest(
            json.dumps(
                {
                    **({'intake': {'complaints': [], 'facts': []}} if include_empty_intake else {}),
                    'gaps': [gap('activity'), gap('meal_pattern', relevance='focused')],
                },
                ensure_ascii=False,
            )
        )
        is None
    )
    assert len(collector.gaps) == 2
    result = fallback.build_fallback(
        before, answer='已经说明饮食搭配', partial_analysis=collector, analysis_partial=True
    )
    assert result['currentQuestion']['intakeDimension'] == 'meal_pattern'
    assert result['qaFlow']['intakeQuestionCount'] == 4


def test_gaps_only_partial_analysis_keeps_previously_understood_facts_complaints_and_priority():
    before = first_lifestyle()
    understanding = intake.IntakeUnderstanding(
        complaints=[
            {
                'text': '用户实际关心的健康情况',
                'evidence': [{'source': '历史回答', 'quote': '用户实际关心的健康情况'}],
            }
        ],
        facts=[fact('goal', '希望了解饮食安排', scope='general', source='历史回答')],
        priorityClarificationNeeded=True,
    )
    intake.merge_understanding(before['qaFlow'], understanding)
    original = deepcopy(before)
    collector = AnalysisRepair(before, '已经说明饮食搭配')
    collector.ingest(
        json.dumps({'gaps': [gap('activity'), gap('meal_pattern', relevance='focused')]}, ensure_ascii=False)
    )
    assert collector.partial_intake is None
    assert len(collector.gaps) == 2
    result = fallback.build_fallback(
        before, answer='已经说明饮食搭配', partial_analysis=collector, analysis_partial=True
    )
    assert result['currentQuestion']['intakeDimension'] == 'meal_pattern'
    for key in ('facts', 'complaints', 'priorityClarificationNeeded', 'focusIds'):
        assert result['qaFlow']['intake'][key] == original['qaFlow']['intake'][key]
    assert before == original


@pytest.mark.asyncio
async def test_status_and_history_keep_an_already_published_lifestyle_question_and_counters_unchanged():
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    for answer in ('希望了解饮食安排', '没有已知疾病', '不清楚家族病史'):
        gateway.next_assessment = assessment(gaps=[gap('diet_structure')])
        current = await submit(manager, current, answer)
    before = deepcopy(cached(module))
    calls = len(gateway.requests)
    assert current['intakeQuestionCount'] == 3
    assert await manager.getSession('u', 's') == current
    history = await manager.getHistory('u', 's')
    assert [item['questionId'] for item in history['history']] == [
        item['question']['questionId'] for item in before['qaHistory']
    ]
    assert history['intakeQuestionCount'] == 3
    assert history['qaRoundCount'] == 0
    assert cached(module) == before
    assert len(gateway.requests) == calls
