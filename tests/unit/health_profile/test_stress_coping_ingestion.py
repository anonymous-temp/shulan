"""Coping facts retain explicit behavior, not mental-health inference."""

from copy import deepcopy

import pytest

from health_profile import AssertionLevel
from health_profile import RecordAnswerCandidateExtractor
from health_profile.ingestion import FactTypeRegistry
from tests.unit.health_profile.test_record_ingestion import RecordFixture
from tests.unit.health_profile.test_record_ingestion import _runtime


def coping_input(answer='忙的时候每晚散步20分钟放松', *, value=None, evidence=None, value_type='text'):
    evidence = answer if evidence is None else evidence
    record = RecordFixture.record(
        qaHistory=[
            {
                'question': {'questionId': 'q_stress', 'question': '忙碌或压力较大时，您通常怎样安排休息和放松？'},
                'answer': answer,
                'answeredAt': RecordFixture.now.isoformat(),
            }
        ]
    )
    candidate = {
        'answerIndex': 0,
        'factType': 'lifestyle.stress_coping',
        'displayName': '模型不能用推断疾病命名',
        'valueType': value_type,
        'value': evidence if value is None else value,
        'subjectKind': 'self',
        'subjectHint': '本人',
        'evidenceText': evidence,
        'confidence': 0.99,
    }
    return record, candidate


async def extract(record, output):
    runtime, _ = _runtime({'candidates': output})
    return await RecordAnswerCandidateExtractor(runtime.hub).extract(record, subject_id='subject-self')


def test_registry_permits_only_text_or_object_stress_coping_values():
    definition = FactTypeRegistry.default().require('lifestyle.stress_coping')
    assert {value.value for value in definition.permitted_value_types} == {'text', 'object'}


@pytest.mark.asyncio
@pytest.mark.parametrize('structured', [False, True])
async def test_explicit_stress_coping_keeps_source_details_and_stable_identity(structured):
    record, candidate = coping_input()
    if structured:
        candidate.update(
            valueType='object',
            value={
                'status': 'reported',
                'description': candidate['evidenceText'],
                'frequency': '每晚',
                'duration': '20分钟',
            },
        )
    original = deepcopy(record.session)
    first = await extract(record, [candidate])
    replay = await extract(record, [candidate])
    assert len(first) == len(replay) == 1
    actual = first[0]
    assert actual.candidate_id == replay[0].candidate_id
    assert actual.value == candidate['value']
    assert actual.display_name == '压力应对与放松'
    assert actual.assertion_level is AssertionLevel.USER_REPORTED
    assert actual.confidence == 0.8
    assert actual.source.pointer == '/qaHistory/0/answer'
    assert actual.source.source_id == record.session_id
    assert actual.source.excerpt_hash
    assert actual.evidence_text == candidate['evidenceText']
    assert record.session == original


@pytest.mark.asyncio
async def test_diagnostic_and_causal_object_fields_are_removed_from_explicit_behavior():
    record, candidate = coping_input(
        value_type='object',
        value={
            'status': 'reported',
            'description': '忙的时候每晚散步20分钟放松',
            'diagnosis': '焦虑症',
            'reasoning': {'mentalDiagnosis': '抑郁症'},
            'possibleCause': '报告异常是由压力引起',
        },
    )
    result = await extract(record, [candidate])
    assert len(result) == 1
    assert result[0].value == {'status': 'reported', 'description': '忙的时候每晚散步20分钟放松'}


@pytest.mark.asyncio
@pytest.mark.parametrize('diagnosis', ['焦虑症', '抑郁症', 'anxiety disorder', 'depression'])
async def test_model_diagnostic_value_falls_back_to_verified_behavior(diagnosis):
    record, candidate = coping_input(value=diagnosis)
    result = await extract(record, [candidate])
    assert len(result) == 1
    assert result[0].value == candidate['evidenceText']
    assert result[0].value_type.value == 'text'


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', ['最近工作压力很大', '我很焦虑', '最近情绪低落', 'I feel stressed'])
async def test_bare_feeling_is_not_converted_into_a_coping_behavior(answer):
    record, candidate = coping_input(answer)
    assert await extract(record, [candidate]) == ()
    assert record.session['qaHistory'][0]['answer'] == answer


@pytest.mark.asyncio
async def test_reported_diagnosis_is_not_written_as_coping_but_independent_behavior_survives():
    answer = '医生说我有抑郁症，我周末会看书放松'
    record, diagnosis = coping_input(answer)
    behavior = {**diagnosis, 'value': '周末会看书放松', 'evidenceText': '周末会看书放松'}
    result = await extract(record, [diagnosis, behavior])
    assert len(result) == 1
    assert result[0].value == '周末会看书放松'
    assert result[0].evidence_text == '我周末会看书放松'


@pytest.mark.asyncio
async def test_causal_claim_is_not_a_coping_fact_and_unselected_options_are_not_sources():
    record, causal = coping_input('我认为压力导致血糖升高')
    fabricated = {**causal, 'value': '每天冥想', 'evidenceText': '每天冥想'}
    assert await extract(record, [causal, fabricated]) == ()


@pytest.mark.asyncio
async def test_feeling_only_object_cannot_hide_behind_a_structured_description():
    record, candidate = coping_input(
        '最近工作压力很大',
        value_type='object',
        value={'status': 'reported', 'description': '最近工作压力很大', 'diagnosis': '焦虑症'},
    )
    assert await extract(record, [candidate]) == ()
