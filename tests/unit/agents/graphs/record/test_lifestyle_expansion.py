"""The expanded interview covers independent purposes without repeating answers."""

from copy import deepcopy

import pytest

from health_profile.ingestion import LIFESTYLE_FACT_LABELS
from modules.record.application import intake
from modules.record.application import interview
from modules.record.application import lifestyle
from modules.record.application.flow import GeneratedQuestion
from modules.record.application.flow import InformationGap
from modules.record.application.flow import validate_new_question
from modules.record.application.presentation import validate_acknowledgement_style
from modules.record.application.presentation import validate_answer_entry_instructions
from tests.unit._support.record_policy import short_interview_policy


def session(*, expanded=True, count=3, dimensions=()):
    state = {
        'qaFlow': {
            'phase': 'intake',
            'intakeQuestionCount': count,
            'intakeCount': count,
            'interactionCount': count,
            'intake': intake.new_intake(),
        },
        'qaHistory': [
            {'question': {'questionId': f'published-{index}', 'phase': 'intake', 'intakeDimension': dimension}}
            for index, dimension in enumerate(dimensions)
        ],
        'currentQuestion': None,
    }
    if expanded:
        state['qaFlow']['interviewPolicy'] = interview.new_policy()
    else:
        state['qaFlow']['interviewPolicy'] = short_interview_policy()
    return state


def observe(state, *items):
    understanding = intake.IntakeUnderstanding(
        complaints=[],
        facts=[
            intake.IntakeFact(
                dimension=dimension,
                scope='general',
                status=status,
                summary=text,
                evidence=[intake.IntakeEvidence(source='本次回答', quote=text)],
            )
            for dimension, status, text in items
        ],
    )
    intake.merge_understanding(state['qaFlow'], understanding)
    return understanding


def gap(dimension, *, focused=False, essential=False):
    return InformationGap(
        topic=lifestyle.LABELS[dimension],
        aspect='habits',
        dimension=dimension,
        scope='general',
        informationNeeded='了解该项具体生活行为',
        reason='根据用户已提供的生活目标了解实际习惯',
        relevance='focused' if focused else 'general',
        essential=essential,
    )


def ranked(state, *items):
    understanding = observe(state)
    return interview.lifestyle_targets(
        state['qaFlow'], understanding, list(items), **interview.selection_context(state)
    )


def test_catalog_has_24_independent_valid_purposes_in_five_categories_and_existing_profile_types():
    assert len(lifestyle.TOPICS) == 24
    assert set(lifestyle.TOPICS) == intake.LIFESTYLE_DIMENSIONS == set(lifestyle.DEFAULT_ORDER)
    assert len(lifestyle.CATEGORIES) == 5
    assert set(lifestyle.PROFILE_TYPE_BY_DIMENSION.values()) <= set(LIFESTYLE_FACT_LABELS)
    for dimension in lifestyle.TOPICS:
        question = GeneratedQuestion.model_validate(lifestyle.question_fields(dimension))
        validate_new_question(question, action='ask_intake')
        observation = intake.IntakeFact(
            dimension=dimension,
            scope='general',
            status='known',
            summary='用户明确提供的实际生活习惯',
            evidence=[{'source': '本次回答', 'quote': '用户明确提供的实际生活习惯'}],
        )
        assert observation.dimension == dimension
    fields = lifestyle.question_fields('bedtime_habits')
    fields['options'].append('not persisted')
    assert 'not persisted' not in lifestyle.question_fields('bedtime_habits')['options']


def test_every_lifestyle_fallback_has_a_distinct_source_neutral_bridge_that_passes_the_new_style_guards():
    assert set(lifestyle.BRIDGES) == intake.LIFESTYLE_DIMENSIONS
    assert len(set(lifestyle.BRIDGES.values())) == 24
    for dimension in lifestyle.TOPICS:
        text = lifestyle.bridge_text(dimension)
        assert 0 < len(text) <= 45
        message = type('Bridge', (), {'acknowledgement': text, 'rationale': ''})()
        validate_acknowledgement_style(message, action='ask_intake')
        validate_answer_entry_instructions(text, options=lifestyle.question_fields(dimension)['options'])


def test_new_policy_and_legacy_policy_are_snapshots_and_counters_do_not_upgrade_old_sessions():
    old = session(expanded=False)
    before = deepcopy(old)
    assert interview.budget(old['qaFlow']) == {
        'version': 1,
        'minIntake': 4,
        'targetIntake': 5,
        'maxIntake': 6,
        'maxInteractions': 20,
    }
    assert old == before
    interview.ensure_counters(old)
    assert old['qaFlow']['interviewPolicy'] == short_interview_policy()
    current = session()
    policy = interview.budget(current['qaFlow'])
    assert policy == {'version': 2, 'minIntake': 10, 'targetIntake': 15, 'maxIntake': 15, 'maxInteractions': 25}
    policy['maxIntake'] = 99
    assert current['qaFlow']['interviewPolicy']['maxIntake'] == 15
    assert interview.new_policy()['maxIntake'] == 15


@pytest.mark.parametrize(('count', 'expected'), [(0, '0%'), (3, '10%'), (9, '30%'), (12, '40%'), (15, '50%')])
def test_new_progress_follows_fifteen_question_budget(count, expected):
    assert interview.progress(session(count=count)['qaFlow']) == expected


def test_old_progress_remains_unchanged():
    assert interview.progress(session(expanded=False, count=5)['qaFlow']) == '50%'


def test_diversity_continues_after_every_category_has_already_been_asked():
    state = session(dimensions=['diet_structure', 'sleep', 'activity', 'smoking', 'stress_coping'])
    candidates = ranked(state, gap('rest_schedule'), gap('meal_pattern'), gap('sleep_duration'))
    assert [item['dimension'] for item in candidates] == ['meal_pattern', 'sleep_duration', 'rest_schedule']


def test_focused_same_category_outweighs_general_different_category():
    state = session(dimensions=['diet_structure'])
    candidates = ranked(state, gap('sleep_duration'), gap('staple_portion', focused=True))
    assert candidates[0]['dimension'] == 'staple_portion'


def test_category_counts_break_ties_before_model_order():
    state = session(dimensions=['diet_structure', 'meal_pattern', 'sleep', 'activity'])
    candidates = ranked(state, gap('hydration'), gap('sleep_duration'))
    assert candidates[0]['dimension'] == 'sleep_duration'


def test_last_category_avoidance_precedes_count_balance():
    state = session(dimensions=['diet_structure', 'meal_pattern', 'hydration', 'sleep'])
    candidates = ranked(state, gap('sleep_duration'), gap('staple_portion'))
    assert candidates[0]['dimension'] == 'staple_portion'


def test_model_order_is_stable_when_relevance_category_count_and_last_category_match():
    state = session(dimensions=['activity'])
    candidates = ranked(state, gap('staple_portion'), gap('meal_pattern'))
    assert [item['dimension'] for item in candidates] == ['staple_portion', 'meal_pattern']


def test_published_history_drives_counts_and_last_category_without_counting_provisional_markers():
    state = session(dimensions=['diet_structure', 'sleep'])
    state['qaFlow']['intake']['asked'] = ['general:activity', 'general:smoking']
    state['currentQuestion'] = {
        'questionId': 'currently-published',
        'phase': 'intake',
        'intakeDimension': 'exercise_frequency',
    }
    context = interview.selection_context(state)
    assert context['asked_dimensions'] == {'diet_structure', 'sleep', 'exercise_frequency'}
    assert context['category_counts'] == {'nutrition': 1, 'sleep': 1, 'activity': 1}
    assert context['last_category'] == 'activity'


def test_repeated_same_question_id_in_current_and_history_is_counted_once():
    state = session(dimensions=['diet_structure'])
    state['currentQuestion'] = deepcopy(state['qaHistory'][0]['question'])
    assert interview.selection_context(state)['category_counts'] == {'nutrition': 1}


def test_legacy_missing_metadata_uses_persisted_purposes():
    state = session()
    state['currentQuestion'] = {'questionId': 'legacy', 'phase': 'intake'}
    state['qaFlow']['intake']['asked'] = ['general:meal_pattern', 'general:exercise_frequency']
    context = interview.selection_context(state)
    assert context['asked_dimensions'] == {'meal_pattern', 'exercise_frequency'}
    assert context['category_counts'] == {'nutrition': 1, 'activity': 1}
    assert context['last_category'] == 'activity'


@pytest.mark.parametrize('status', ['known', 'unknown', 'declined', 'conflicting'])
def test_known_or_unavailable_detail_does_not_block_a_different_detail_in_the_same_dimension_family(status):
    state = session()
    observe(state, ('exercise_frequency', status, '已提供该项回答'))
    candidates = ranked(state, gap('exercise_frequency'), gap('exercise_duration'))
    assert [item['dimension'] for item in candidates] == ['exercise_duration']


def test_one_answer_can_cover_frequency_and_duration_without_reasking_either():
    state = session()
    observe(state, ('exercise_frequency', 'known', '每周运动3天'), ('exercise_duration', 'known', '每次30分钟'))
    candidates = ranked(state, gap('exercise_frequency'), gap('exercise_duration'), gap('activity_breaks'))
    assert [item['dimension'] for item in candidates] == ['activity_breaks']


def test_broad_habits_fact_does_not_close_all_expanded_purposes_or_rewrite_the_old_fact():
    state = session()
    observe(state, ('habits', 'known', '用户说生活较规律，无法可靠拆成全部细项'))
    before = deepcopy(state['qaFlow']['intake']['facts'])
    assert ranked(state, gap('water_intake'))[0]['dimension'] == 'water_intake'
    assert interview.default_lifestyle_target(state['qaFlow'])['dimension'] == 'diet_structure'
    assert state['qaFlow']['intake']['facts'] == before


@pytest.mark.parametrize('answer', ['不饮酒', '从不喝酒', '已经戒酒', '滴酒不沾'])
def test_explicit_non_drinking_excludes_amount_but_does_not_close_tobacco(answer):
    state = session()
    observe(state, ('alcohol', 'known', answer))
    assert [item['dimension'] for item in ranked(state, gap('alcohol_amount'), gap('passive_smoking'))] == [
        'passive_smoking'
    ]
    state['qaFlow']['intake']['asked'] = [f'general:{key}' for key in lifestyle.TOPICS if key != 'alcohol_amount']
    assert interview.default_lifestyle_target(state['qaFlow']) is None


def test_drinking_specific_beverages_or_occasionally_is_not_misclassified_as_non_drinking():
    state = session()
    observe(state, ('alcohol', 'known', '不喝白酒，但偶尔喝啤酒'))
    assert ranked(state, gap('alcohol_amount'))[0]['dimension'] == 'alcohol_amount'


def test_no_exercise_excludes_duration_but_not_general_daily_activity():
    state = session()
    observe(state, ('exercise_frequency', 'known', '目前没有安排运动'))
    assert [item['dimension'] for item in ranked(state, gap('exercise_duration'), gap('activity'))] == ['activity']


def test_non_smoking_does_not_suppress_secondhand_smoke_question():
    state = session()
    observe(state, ('smoking', 'known', '从不吸烟'))
    assert ranked(state, gap('passive_smoking'))[0]['dimension'] == 'passive_smoking'


def test_expanded_candidate_binds_its_information_purpose_without_rewriting_evidence_or_model_input():
    state = session()
    proposed = gap('activity', focused=True)
    proposed.informationNeeded = '每次运动持续多少分钟'
    result = ranked(state, proposed)[0]
    assert result['informationNeeded'] == lifestyle.question_fields('activity')['question']
    assert result['reason'] == proposed.reason
    assert result['relevance'] == 'focused'
    assert proposed.informationNeeded == '每次运动持续多少分钟'


def test_expanded_default_uses_the_specific_sleep_purpose_instead_of_the_legacy_broad_label():
    state = session()
    state['qaFlow']['intake']['asked'] = [f'general:{item}' for item in lifestyle.TOPICS if item != 'sleep']
    result = interview.default_lifestyle_target(state['qaFlow'])
    assert result['dimension'] == 'sleep'
    assert result['informationNeeded'] == lifestyle.question_fields('sleep')['question']
    assert '时间' in result['informationNeeded']
    assert '时长' not in result['informationNeeded']


def test_legacy_candidates_preserve_the_existing_information_needed_contract():
    state = session(expanded=False)
    proposed = gap('activity')
    proposed.informationNeeded = '原有会话中需要了解的活动情况'
    assert ranked(state, proposed)[0]['informationNeeded'] == proposed.informationNeeded


def test_explicit_no_exercise_covers_zero_frequency_and_duration_but_not_sitting_or_breaks():
    state = session()
    observe(state, ('activity', 'known', '我不运动'))
    result = ranked(
        state, gap('exercise_frequency'), gap('exercise_duration'), gap('sedentary'), gap('activity_breaks')
    )
    assert {item['dimension'] for item in result} == {'sedentary', 'activity_breaks'}


@pytest.mark.parametrize(
    ('status', 'summary'), [('unknown', '活动频次记不清'), ('known', '日常活动较少'), ('known', '不常运动但偶尔会运动')]
)
def test_uncertain_or_low_activity_does_not_establish_zero_exercise(status, summary):
    state = session()
    observe(state, ('activity', status, summary))
    result = ranked(state, gap('exercise_frequency'), gap('exercise_duration'))
    assert {item['dimension'] for item in result} == {'exercise_frequency', 'exercise_duration'}


def test_zero_exercise_frequency_does_not_close_daily_activity_or_secondhand_smoke():
    state = session()
    observe(state, ('exercise_frequency', 'known', '目前没有安排运动'))
    result = ranked(state, gap('exercise_duration'), gap('activity'), gap('activity_breaks'), gap('passive_smoking'))
    assert {item['dimension'] for item in result} == {'activity', 'activity_breaks', 'passive_smoking'}


@pytest.mark.parametrize('summary', ['基本没有跑步，平时骑车通勤', '没有运动计划'])
def test_lack_of_one_activity_or_an_exercise_plan_does_not_establish_zero_exercise(summary):
    state = session()
    observe(state, ('activity', 'known', summary))
    result = ranked(state, gap('exercise_frequency'), gap('exercise_duration'))
    assert {item['dimension'] for item in result} == {'exercise_frequency', 'exercise_duration'}


@pytest.mark.parametrize('summary', ['现在不喝了', '目前不喝了'])
def test_explicit_current_non_drinking_excludes_amount(summary):
    state = session()
    observe(state, ('alcohol', 'known', summary))
    assert ranked(state, gap('alcohol_amount')) == []


@pytest.mark.parametrize('summary', ['很少喝酒', '不喝白酒但喝啤酒'])
def test_low_frequency_or_avoiding_one_beverage_does_not_establish_non_drinking(summary):
    state = session()
    observe(state, ('alcohol', 'known', summary))
    assert ranked(state, gap('alcohol_amount'))[0]['dimension'] == 'alcohol_amount'


@pytest.mark.parametrize('count', [3, 9, 10, 12, 14])
def test_expanded_budget_checks_unused_catalog_even_if_model_says_sufficient(count):
    state = session(count=count)
    assert interview.select_lifestyle_target(state['qaFlow'], sufficient=True)['dimension'] == 'diet_structure'


def test_maximum_and_real_exhaustion_end_without_repeated_habits_supplement():
    assert interview.select_lifestyle_target(session(count=15)['qaFlow'], candidates=[{'dimension': 'sleep'}]) is None
    state = session(dimensions=lifestyle.DEFAULT_ORDER)
    context = interview.selection_context(state)
    assert interview.select_lifestyle_target(state['qaFlow'], **context) is None
    assert interview.default_lifestyle_target(state['qaFlow'], required=True, **context) is None


def test_legacy_budget_still_requires_essential_for_its_sixth_question():
    state = session(expanded=False, count=5)
    assert interview.select_lifestyle_target(state['qaFlow'], candidates=[{'dimension': 'activity'}]) is None
    essential = {'dimension': 'activity', 'essential': True}
    assert interview.select_lifestyle_target(state['qaFlow'], candidates=[essential]) == essential
    assert interview.select_lifestyle_target(state['qaFlow'], candidates=[essential], sufficient=True) is None


def test_legacy_minimum_keeps_its_single_required_habits_supplement_without_expanding_the_old_budget():
    state = session(expanded=False, count=3)
    observe(state, ('habits', 'known', '已提供生活习惯的概览'))
    result = interview.select_lifestyle_target(state['qaFlow'], sufficient=True)
    assert result['dimension'] == 'habits'
    assert result['supplementation'] is True
    assert result['knownFacts'][0]['summary'] == '已提供生活习惯的概览'
    state['qaFlow']['intakeQuestionCount'] = 4
    assert interview.select_lifestyle_target(state['qaFlow'], sufficient=True) is None
    assert interview.budget(state['qaFlow'])['maxIntake'] == 6
