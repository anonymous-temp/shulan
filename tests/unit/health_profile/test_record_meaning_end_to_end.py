"""Record meanings survive ingestion, replay and Chat without invented diagnoses."""

import json

import pytest

from agents.graphs.health_profile.focus import clinical_anchor
from agents.graphs.health_profile.focus import is_abnormal
from agents.graphs.health_profile.focus import source_flag
from health_profile import AssertionLevel
from health_profile import FactStatus
from health_profile import RecordProfileProcessor
from health_profile import RepositoryHealthProfileChatMemoryProvider
from health_profile.summary import build_profile_summary
from tests.unit.health_profile.test_record_ingestion import FakeRecordProfileService
from tests.unit.health_profile.test_record_ingestion import FakeRecordStore
from tests.unit.health_profile.test_record_ingestion import RecordFixture
from tests.unit.health_profile.test_record_ingestion import _runtime
from tests.unit.health_profile.test_report_ingestion import FakeProfileRepository
from tests.unit.health_profile.test_report_ingestion import ReportIngestionFixture


def candidate(index, fact_type, answer, value, label, *, subject='self', hint='本人'):
    return {
        'answerIndex': index,
        'factType': fact_type,
        'displayName': label,
        'valueType': 'object' if isinstance(value, dict) else 'text',
        'value': value,
        'subjectKind': subject,
        'subjectHint': hint,
        'evidenceText': answer,
        'confidence': 0.9,
    }


def meaning(answer, *, conditions=(), specificity='unspecified'):
    return {
        'presence': 'present',
        'specificity': specificity,
        'description': answer,
        'conditions': list(conditions),
        'relationship': None,
    }


async def ingest(record, items):
    runtime, _ = _runtime({'candidates': items})
    repository = FakeProfileRepository(ReportIngestionFixture.subject())
    profile_service = FakeRecordProfileService(repository.subject, repository=repository)
    sequence = 0

    def next_id(prefix):
        nonlocal sequence
        sequence += 1
        return f'{prefix}-meaning-{sequence}'

    processor = RecordProfileProcessor(
        store=FakeRecordStore(record),
        repository=repository,
        profile_service=profile_service,
        agent_hub=runtime.hub,
        clock=lambda: RecordFixture.now,
        id_factory=next_id,
    )
    await processor.process(RecordFixture.job())
    replay = await processor.process(RecordFixture.job())
    return repository, profile_service, replay


@pytest.mark.asyncio
async def test_vague_history_and_specific_family_background_are_saved_and_read_without_fake_relatives():
    observations = [
        ('您本次最希望了解什么健康问题？', '想了解糖尿病'),
        ('您曾经被医生明确诊断过哪些疾病？', '有病史但忘了病名'),
        ('您的血亲中有哪些疾病？', '糖尿病'),
        ('您平时如何运动？', '每周跑步3次，每次20分钟'),
    ]
    record = RecordFixture.record(
        qaHistory=[
            {
                'question': {'questionId': f'q-{index}', 'question': question},
                'answer': answer,
                'answeredAt': RecordFixture.now.isoformat(),
            }
            for index, (question, answer) in enumerate(observations)
        ]
    )
    items = [
        candidate(0, 'goal.health', observations[0][1], observations[0][1], '了解糖尿病'),
        candidate(1, 'condition.history_status', observations[1][1], meaning(observations[1][1]), '病史说明'),
        candidate(
            2,
            'family_history.reported',
            observations[2][1],
            meaning(observations[2][1], conditions=['糖尿病'], specificity='specific'),
            '家族背景',
        ),
        candidate(3, 'lifestyle.physical_activity', observations[3][1], observations[3][1], '跑步习惯'),
    ]
    repository, profile_service, replay = await ingest(record, items)
    assert replay.fact_count == 0
    assert profile_service.subject_calls == []
    assert not any(fact.fact_type == 'condition.diagnosis' for fact in repository.facts)
    facts = {fact.fact_type: fact for fact in repository.facts}
    for fact_type in (
        'condition.history_status',
        'family_history.reported',
        'goal.health',
        'lifestyle.physical_activity',
    ):
        fact = facts[fact_type]
        assert fact.status is FactStatus.PENDING
        assert fact.assertion_level is AssertionLevel.USER_REPORTED
        assert fact.subject_id == repository.subject.subject_id
        assert fact.sources[0].pointer.startswith('/qaHistory/')
        assert fact.sources[0].excerpt_hash
    assert facts['condition.history_status'].value['presence'] == 'present'
    assert facts['condition.history_status'].value['specificity'] == 'unspecified'
    assert facts['condition.history_status'].value['conditions'] == []
    assert facts['family_history.reported'].value['conditions'] == ['糖尿病']
    assert facts['family_history.reported'].value['relationship'] is None
    context = await RepositoryHealthProfileChatMemoryProvider(repository).build_context('user-1')
    rows = [json.loads(line) for line in context.splitlines() if line.startswith('{')]
    assert any(row['factType'] == 'family_history.reported' for row in rows)
    assert any(row['factType'] == 'condition.history_status' for row in rows)
    assert '不代表本人患病' in context


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', ['我没有糖尿病', '我没有高血压', '我不清楚是否有糖尿病'])
async def test_omitted_model_candidates_do_not_expand_a_scoped_answer_to_all_medical_history(answer):
    record = RecordFixture.record(
        qaHistory=[
            {
                'question': {'questionId': 'history', 'question': '您曾经被医生明确诊断过哪些疾病？'},
                'answer': answer,
                'answeredAt': RecordFixture.now.isoformat(),
            }
        ]
    )
    repository, profile_service, replay = await ingest(record, [])
    facts = [fact for fact in repository.facts if fact.fact_type == 'condition.history_status']
    assert len(facts) == 1 and replay.fact_count == 0
    assert facts[0].value['description'] == answer
    assert facts[0].status is FactStatus.PENDING
    assert profile_service.subject_calls == []
    assert not any(fact.fact_type == 'condition.diagnosis' for fact in repository.facts)
    confirmed = tuple(fact.model_copy(update={'status': FactStatus.ACTIVE}) for fact in repository.facts)
    summary = build_profile_summary(facts=confirmed, profile_version=1, generated_at=RecordFixture.now)
    assert '无个人疾病史' not in summary['overall']
    assert '糖尿病' in summary['overall'] or '高血压' in summary['overall']


def single_answer_record(answer, *, dimension='medical_history'):
    return RecordFixture.record(
        qaHistory=[
            {
                'question': {
                    'questionId': 'mixed-answer',
                    'question': '请说明个人及血亲的疾病情况。',
                    'intakeDimension': dimension,
                },
                'answer': answer,
                'answeredAt': RecordFixture.now.isoformat(),
            }
        ]
    )


@pytest.mark.asyncio
async def test_multiple_conditions_keep_independent_polarity_with_whole_answer_evidence():
    answer = '我没有糖尿病，但我有高血压'
    repository, _, replay = await ingest(
        single_answer_record(answer),
        [candidate(0, 'condition.diagnosis', answer, name, name) for name in ('糖尿病', '高血压')],
    )
    assert replay.fact_count == 0
    diagnosis = [fact for fact in repository.facts if fact.fact_type == 'condition.diagnosis']
    assert [fact.value for fact in diagnosis] == ['高血压']
    observation = next(fact for fact in repository.facts if fact.fact_type == 'condition.history_status')
    assert observation.value['presence'] == 'absent'
    assert observation.value['conditions'] == ['糖尿病']


@pytest.mark.asyncio
async def test_relative_positive_and_self_denial_keep_distinct_subjects_after_persistence():
    answer = '父亲有糖尿病，我没有糖尿病'
    repository, profile_service, replay = await ingest(
        single_answer_record(answer, dimension='family_history'),
        [
            candidate(0, 'family_history.condition', answer, '糖尿病', '糖尿病', subject='blood_relative', hint='父亲'),
            candidate(0, 'condition.diagnosis', answer, '糖尿病', '糖尿病'),
        ],
    )
    assert replay.fact_count == 0
    assert len(profile_service.subject_calls) == 1
    family = next(fact for fact in repository.facts if fact.fact_type == 'family_history.condition')
    personal = next(fact for fact in repository.facts if fact.fact_type == 'condition.history_status')
    assert family.subject_id != personal.subject_id == repository.subject.subject_id
    assert family.value == '糖尿病'
    assert personal.value['presence'] == 'absent'
    assert personal.value['conditions'] == ['糖尿病']


@pytest.mark.asyncio
async def test_resolved_symptom_keeps_time_context_and_cannot_become_a_current_risk_anchor():
    answer = '去年头痛，今年好了'
    repository, _, _ = await ingest(
        single_answer_record(answer, dimension='goal'),
        [candidate(0, 'symptom.reported', answer, {'name': '头痛', 'description': answer, 'status': 'past'}, '头痛')],
    )
    symptom = next(fact for fact in repository.facts if fact.fact_type == 'symptom.reported')
    assert isinstance(symptom.value, dict)
    assert symptom.value['description'] == answer
    row = {
        'factType': symptom.fact_type,
        'category': symptom.category.value,
        'displayName': symptom.display_name,
        'value': symptom.value,
        'interpretation': 'high',
    }
    assert not clinical_anchor(row)
    assert not is_abnormal(row)
    assert source_flag(row) is None
