"""Real HEAD budgets and conservative applicability use only synthetic records."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.record.application import fallback
from modules.record.application import flow
from modules.record.application import intake
from modules.record.application import interview
from tests.unit._support.record_policy import short_interview_policy

from .test_dynamic_record import cached
from .test_dynamic_record import save_cache
from .test_dynamic_record import setup_manager
from .test_lifestyle_expansion import gap
from .test_lifestyle_expansion import observe
from .test_lifestyle_expansion import ranked
from .test_lifestyle_expansion import session as lifestyle_session


def head_snapshot(*, answered=6):
    """HEAD's new_flow had neither a policy marker nor the newer counters."""
    return {
        'qaFlow': {
            'version': flow.FLOW_VERSION,
            'phase': 'intake',
            'interactionCount': answered,
            'intakeCount': answered,
            'supplementClarifications': 0,
            'concerns': [],
            'focus': None,
            'intake': intake.new_intake(),
            'finalMessage': '',
            'finalReasoning': '',
            'endReason': None,
            'lastSubmission': None,
        },
        'qaHistory': [
            {
                'question': {
                    'questionId': f'old-{index}',
                    'question': '合成的历史问题',
                    'questionType': 'radio',
                    'options': ['大多规律', '经常变化'],
                    'phase': 'intake',
                },
                'answer': '合成历史回答',
                'answeredAt': '2026-01-01T00:00:00',
            }
            for index in range(answered)
        ],
        'currentQuestion': {
            'questionId': 'old-current',
            'question': '合成的已发布问题',
            'questionType': 'radio',
            'options': ['大多规律', '经常变化'],
            'phase': 'intake',
        },
    }


def remaining_essential():
    return flow.PatientAssessment(
        intent='answer',
        substantive=True,
        intakeSufficient=False,
        summary='还有与用户目标相关的生活习惯值得了解。',
        concerns=[],
        intake={'complaints': [], 'facts': []},
        gaps=[gap('sleep', essential=True)],
    )


def test_unmarked_budget_preserves_the_real_head_ten_question_cap_without_mutating_input():
    original = head_snapshot()
    before = deepcopy(original)
    assert interview.budget(original['qaFlow']) == {
        'version': 1,
        'minIntake': 1,
        'targetIntake': 5,
        'maxIntake': 10,
        'maxInteractions': 20,
    }
    assert original == before
    draft, decision = flow.advance_flow(original, remaining_essential(), answer='大多规律')
    assert decision['action'] == 'ask_intake'
    assert decision['target']['dimension'] == 'sleep'
    assert draft['qaFlow']['intakeQuestionCount'] == 7
    assert draft['qaFlow']['interviewPolicy']['maxIntake'] == 10
    assert original == before


@pytest.mark.parametrize(
    ('policy', 'answered'), [(None, 9), (short_interview_policy(), 5), (interview.new_policy(), 14)]
)
def test_each_explicit_or_historical_cap_ends_at_its_own_boundary(policy, answered):
    snapshot = head_snapshot(answered=answered)
    if policy is not None:
        snapshot['qaFlow']['interviewPolicy'] = deepcopy(policy)
    draft, decision = flow.advance_flow(snapshot, remaining_essential(), answer='大多规律')
    assert decision['action'] == 'invite_supplement'
    assert draft['qaFlow']['intakeQuestionCount'] == answered + 1
    assert interview.budget(draft['qaFlow'])['maxIntake'] == answered + 1


@pytest.mark.parametrize('with_newer_counters', [False, True])
def test_budget_freezes_before_counter_derivation_and_is_not_guessed_from_newer_fields(with_newer_counters):
    snapshot = head_snapshot()
    if with_newer_counters:
        snapshot['qaFlow'].update(intakeQuestionCount=6, qaRoundCount=0, qaVagueCount=0)
    interview.ensure_counters(snapshot)
    first = deepcopy(snapshot['qaFlow']['interviewPolicy'])
    assert first['maxIntake'] == 10
    interview.ensure_counters(snapshot)
    assert snapshot['qaFlow']['interviewPolicy'] == first
    snapshot['qaFlow']['interviewPolicy'] = short_interview_policy()
    interview.ensure_counters(snapshot)
    assert snapshot['qaFlow']['interviewPolicy'] == short_interview_policy()


@pytest.mark.parametrize(
    ('policy', 'answered', 'phase'), [(None, 6, 'intake'), (short_interview_policy(), 5, 'supplement')]
)
def test_partial_fallback_uses_the_same_snapshot_budget_for_an_essential_candidate(policy, answered, phase):
    snapshot = head_snapshot(answered=answered)
    if policy is not None:
        snapshot['qaFlow']['interviewPolicy'] = policy
    result = fallback.build_fallback(
        snapshot,
        answer='大多规律',
        partial_analysis=SimpleNamespace(
            partial_intake=intake.IntakeUnderstanding(complaints=[], facts=[]), gaps=[gap('sleep', essential=True)]
        ),
        failure_kind='upstream',
    )
    assert result['qaFlow']['phase'] == phase
    assert result['qaFlow']['intakeQuestionCount'] == answered + 1
    assert result['qaFlow']['interviewPolicy']['maxIntake'] == (10 if policy is None else 6)


def test_three_intake_progress_formulas_preserve_head_substantive_count_and_short_and_new_shapes():
    snapshot = head_snapshot()['qaFlow']
    snapshot.update(intakeQuestionCount=6, interactionCount=4)
    assert interview.progress(snapshot) == '20%'
    snapshot.pop('interactionCount')
    assert interview.progress(snapshot) == '30%'
    snapshot['interviewPolicy'] = short_interview_policy()
    assert interview.progress(snapshot) == '50%'
    snapshot['interviewPolicy'] = interview.new_policy()
    assert interview.progress(snapshot) == '20%'


@pytest.mark.asyncio
async def test_normal_status_and_history_queries_do_not_persist_a_derived_head_policy_or_change_the_question():
    manager, module, gateway, _ = setup_manager()
    await manager.createSession('u', 's')
    stored = cached(module)
    stored.update(head_snapshot())
    stored['qaFlow']['interactionCount'] = 4
    save_cache(module, stored)
    before = deepcopy(cached(module))
    calls = len(gateway.requests)
    manager._session_cache.save = AsyncMock(wraps=manager._session_cache.save)
    manager._session_repository.save = AsyncMock(wraps=manager._session_repository.save)
    current = await manager.getSession('u', 's')
    await manager.getHistory('u', 's')
    assert current['questionId'] == before['currentQuestion']['questionId']
    assert current['options'] == before['currentQuestion']['options']
    assert current['progress'] == '20%'
    assert cached(module) == before
    assert len(gateway.requests) == calls
    manager._session_cache.save.assert_not_awaited()
    manager._session_repository.save.assert_not_awaited()


@pytest.mark.parametrize(
    ('dimension', 'summary', 'followup'),
    [
        ('alcohol', '工作日不喝酒，周末喝一点', 'alcohol_amount'),
        ('alcohol', '开车时不喝酒', 'alcohol_amount'),
        ('alcohol', '现在不喝白酒，但会喝啤酒', 'alcohol_amount'),
        ('activity', '工作日不运动，周末会运动两次', 'exercise_duration'),
        ('activity', '天气不好时不运动', 'exercise_frequency'),
        ('activity', '没有运动计划', 'exercise_duration'),
    ],
)
def test_qualified_non_use_does_not_discard_a_focused_candidate(dimension, summary, followup):
    state = lifestyle_session()
    observe(state, (dimension, 'known', summary))
    assert ranked(state, gap(followup, focused=True))[0]['dimension'] == followup


@pytest.mark.parametrize('dimension', ['activity', 'exercise_frequency', 'exercise_duration'])
def test_explicit_zero_exercise_from_any_controlled_source_skips_unknown_frequency_and_duration(dimension):
    state = lifestyle_session()
    observe(state, (dimension, 'known', '目前没有安排运动'))
    assert ranked(state, gap('exercise_frequency'), gap('exercise_duration')) == []
    assert ranked(state, gap('sedentary'))[0]['dimension'] == 'sedentary'


@pytest.mark.parametrize('summary', ['我从来不喝酒', '现在不喝酒了', '用户目前没有任何饮酒习惯'])
def test_unqualified_current_alcohol_denial_still_excludes_the_amount_question(summary):
    state = lifestyle_session()
    observe(state, ('alcohol', 'known', summary))
    assert ranked(state, gap('alcohol_amount')) == []


@pytest.mark.parametrize('status', ['known', 'unknown', 'declined', 'conflicting'])
def test_other_activity_information_prevents_treating_a_negative_fact_as_unqualified_zero(status):
    state = lifestyle_session()
    observe(state, ('activity', 'known', '不运动'), ('exercise_frequency', status, '周末会运动两次'))
    assert ranked(state, gap('exercise_duration'))[0]['dimension'] == 'exercise_duration'


@pytest.mark.parametrize('status', ['known', 'unknown', 'declined', 'conflicting'])
def test_multiple_alcohol_facts_do_not_hide_a_conflict_behind_a_single_negative(status):
    facts = [
        {'dimension': 'alcohol', 'status': 'known', 'summary': '不饮酒'},
        {'dimension': 'alcohol', 'status': status, 'summary': '周末喝一点'},
    ]
    assert interview._not_applicable('alcohol_amount', facts) is False
