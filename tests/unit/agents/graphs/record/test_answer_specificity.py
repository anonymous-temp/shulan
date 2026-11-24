"""Unspecified user reports remain useful without adding clarification questions."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from modules.record.application import flow
from modules.record.application import intake
from modules.record.application.validation import RecordValidationError


def observation(dimension, answer, *, presence='present', specificity='unspecified', entities=(), status='unknown'):
    return {
        'dimension': dimension,
        'scope': 'general',
        'status': status,
        'summary': answer,
        'presence': presence,
        'specificity': specificity,
        'entities': list(entities),
        'evidence': [{'source': '本次回答', 'quote': answer}],
    }


@pytest.mark.parametrize(
    'count,dimension,answer,next_dimension',
    [
        (0, 'goal', '身体不舒服', 'medical_history'),
        (1, 'medical_history', '有病史但忘了病名', 'family_history'),
        (2, 'family_history', '家里有疾病史但不知道名称', None),
    ],
)
def test_unspecified_positive_keeps_presence_and_moves_to_the_next_planned_topic(
    count, dimension, answer, next_dimension
):
    snapshot = {'qaFlow': flow.new_flow(), 'currentQuestion': {'questionId': 'q'}, 'qaHistory': []}
    snapshot['qaFlow'].update(intakeQuestionCount=count, intakeCount=count, interactionCount=count)
    original = deepcopy(snapshot)
    parsed = flow.PatientAssessment(
        intent='answer',
        substantive=True,
        intakeSufficient=False,
        summary=answer,
        concerns=[],
        intake={'facts': [observation(dimension, answer)], 'complaints': []},
        gaps=[],
    )
    flow.validate_assessment(parsed, snapshot, answer, sources={})
    draft, decision = flow.advance_flow(snapshot, parsed, answer=answer)
    recorded = draft['qaFlow']['intake']['facts'][f'general:{dimension}']
    assert recorded['observations'][0]['presence'] == 'present'
    assert recorded['observations'][0]['specificity'] == 'unspecified'
    assert recorded['observations'][0]['entities'] == []
    assert recorded['summary'] == answer
    assert decision['action'] == 'ask_intake'
    if next_dimension:
        assert decision['target']['dimension'] == next_dimension
    else:
        assert decision['target']['dimension'] in intake.LIFESTYLE_DIMENSIONS
    assert draft['qaFlow']['intakeQuestionCount'] == count + 1
    assert snapshot == original


@pytest.mark.parametrize(
    'answer,presence,entities',
    [('没有疾病史', 'absent', []), ('不清楚有没有病史', 'unknown', []), ('没有糖尿病', 'absent', ['糖尿病'])],
)
def test_explicit_absence_unknown_and_scoped_denial_are_distinct(answer, presence, entities):
    fact = intake.IntakeFact.model_validate(
        observation(
            'medical_history',
            answer,
            presence=presence,
            specificity='specific' if entities else 'unspecified',
            entities=entities,
        )
    )
    state = flow.new_flow()
    intake.merge_understanding(state, intake.IntakeUnderstanding(complaints=[], facts=[fact]))
    result = state['intake']['facts']['general:medical_history']['observations'][0]
    assert result['presence'] == presence
    assert result['entities'] == entities


@pytest.mark.parametrize('entity', ['某种慢性病', '身体不舒服', '不清楚'])
def test_vague_text_cannot_be_published_as_a_specific_entity(entity):
    with pytest.raises(ValidationError, match='泛化描述'):
        intake.IntakeFact.model_validate(
            observation('medical_history', entity, specificity='specific', entities=[entity])
        )


def test_fabricated_entity_quote_cannot_replace_the_actual_user_answer():
    understanding = intake.IntakeUnderstanding(
        complaints=[], facts=[observation('medical_history', '糖尿病', specificity='specific', entities=['糖尿病'])]
    )
    with pytest.raises(RecordValidationError, match='对应来源'):
        intake.validate_understanding(understanding, {'qaFlow': flow.new_flow()}, '没有病史', {})


def test_legacy_fact_without_meaning_metadata_remains_readable():
    raw = observation('medical_history', '过去没有确诊疾病')
    for key in ('presence', 'specificity', 'entities'):
        raw.pop(key)
    fact = intake.IntakeFact.model_validate(raw)
    state = flow.new_flow()
    intake.merge_understanding(state, intake.IntakeUnderstanding(complaints=[], facts=[fact]))
    assert 'observations' not in state['intake']['facts']['general:medical_history']
