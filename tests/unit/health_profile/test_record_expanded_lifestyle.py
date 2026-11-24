"""Several precise intake answers retain their own provenance in shared fact types."""

import pytest

from health_profile import AssertionLevel
from health_profile import FactStatus
from health_profile import RecordProfileProcessor
from modules.record.application.lifestyle import PROFILE_TYPE_BY_DIMENSION
from tests.unit.health_profile.test_record_ingestion import FakeRecordProfileService
from tests.unit.health_profile.test_record_ingestion import FakeRecordStore
from tests.unit.health_profile.test_record_ingestion import RecordFixture
from tests.unit.health_profile.test_record_ingestion import _runtime
from tests.unit.health_profile.test_report_ingestion import FakeProfileRepository
from tests.unit.health_profile.test_report_ingestion import ReportIngestionFixture


@pytest.mark.asyncio
async def test_expanded_details_sharing_profile_types_keep_all_answer_sources_and_exclude_ai_analysis():
    details = (
        ('sleep', '我通常晚上十一点睡觉'),
        ('sleep_duration', '我每天通常睡七小时'),
        ('bedtime_habits', '睡前我会看手机半小时'),
        ('exercise_frequency', '我每周运动三次'),
        ('exercise_duration', '每次运动三十分钟'),
        ('work_breaks', '我每工作一小时就休息五分钟'),
    )
    history, output = [], []
    analysis = '模型认为用户患有焦虑症，并因此睡眠不足'
    for index, (dimension, answer) in enumerate(details):
        history.append(
            {
                'question': {
                    'questionId': f'q_expanded_{index}',
                    'question': '请描述您的日常生活习惯。',
                    'intakeDimension': dimension,
                    'assistantMessage': analysis,
                },
                'answer': answer,
                'answeredAt': RecordFixture.now.isoformat(),
            }
        )
        output.append(
            {
                'answerIndex': index,
                'factType': PROFILE_TYPE_BY_DIMENSION[dimension],
                'displayName': '模型给出的名称',
                'valueType': 'text',
                'value': answer,
                'subjectKind': 'self',
                'subjectHint': '本人',
                'evidenceText': answer,
                'confidence': 0.99,
            }
        )
    output.append({**output[0], 'value': analysis, 'evidenceText': analysis})
    record = RecordFixture.record(qaHistory=history)
    runtime, _ = _runtime({'candidates': output})
    subject = ReportIngestionFixture.subject()
    repository = FakeProfileRepository(subject)
    sequence = 0

    def next_id(prefix):
        nonlocal sequence
        sequence += 1
        return f'{prefix}-expanded-{sequence}'

    processor = RecordProfileProcessor(
        store=FakeRecordStore(record),
        repository=repository,
        profile_service=FakeRecordProfileService(subject, repository=repository),
        agent_hub=runtime.hub,
        clock=lambda: RecordFixture.now,
        id_factory=next_id,
    )
    await processor.process(RecordFixture.job())
    replay = await processor.process(RecordFixture.job())
    facts = [fact for fact in repository.facts if fact.fact_type.startswith('lifestyle.')]
    assert replay.fact_count == 0
    assert len(facts) == len(details)
    assert {fact.value for fact in facts} == {answer for _, answer in details}
    assert sum(fact.fact_type == 'lifestyle.sleep' for fact in facts) == 3
    assert sum(fact.fact_type == 'lifestyle.physical_activity' for fact in facts) == 2
    for fact in facts:
        index = next(index for index, (_, answer) in enumerate(details) if answer == fact.value)
        assert fact.sources[0].pointer == f'/qaHistory/{index}/answer'
        assert fact.sources[0].source_id == record.session_id
        assert fact.assertion_level is AssertionLevel.USER_REPORTED
        assert fact.status is FactStatus.PENDING
        assert analysis not in str(fact.value)
