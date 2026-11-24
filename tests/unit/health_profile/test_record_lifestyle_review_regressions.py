"""Review counterexamples keep user meaning through extraction and consumption."""

from copy import deepcopy

import pytest

from health_profile import AssertionLevel
from health_profile import FactStatus
from health_profile import RecordAnswerCandidateExtractor
from health_profile import RecordProfileProcessor
from health_profile import RepositoryHealthProfileChatMemoryProvider
from tests.unit.health_profile.test_record_ingestion import FakeRecordProfileService
from tests.unit.health_profile.test_record_ingestion import FakeRecordStore
from tests.unit.health_profile.test_record_ingestion import RecordFixture
from tests.unit.health_profile.test_record_ingestion import _runtime
from tests.unit.health_profile.test_report_ingestion import FakeProfileRepository
from tests.unit.health_profile.test_report_ingestion import ReportIngestionFixture


def observation(answer, fact_type, value):
    record = RecordFixture.record(
        qaHistory=[
            {
                'question': {'questionId': 'q_behavior', 'question': '请描述相关的生活安排'},
                'answer': answer,
                'answeredAt': RecordFixture.now.isoformat(),
            }
        ]
    )
    item = {
        'answerIndex': 0,
        'factType': fact_type,
        'displayName': '生活行为',
        'valueType': 'object' if isinstance(value, dict) else 'text',
        'value': value,
        'subjectKind': 'self',
        'subjectHint': '本人',
        'evidenceText': answer,
        'confidence': 0.9,
    }
    return record, item


async def extract(record, *items):
    runtime, _ = _runtime({'candidates': list(items)})
    return await RecordAnswerCandidateExtractor(runtime.hub).extract(record, subject_id='subject-self')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'fact_type,answer,value',
    [
        ('diet_pattern', '我从不喝含糖饮料', {'status': 'reported', 'description': '喝含糖饮料'}),
        (
            'physical_activity',
            '我每天散步半小时',
            {'status': 'denied', 'negation': True, 'description': '我每天散步半小时'},
        ),
        ('physical_activity', '我每天散步半小时', {'STATUS': 'denied', 'description': '我每天散步半小时'}),
        (
            'physical_activity',
            '我每天散步半小时',
            {'status': 'reported', 'NEGATION': True, 'description': '我每天散步半小时'},
        ),
        ('sleep', '我每晚睡8小时', {'status': 'unknown', 'unknown': True, 'description': '我每晚睡8小时'}),
        ('alcohol_use', '我不是每天都喝酒', {'status': 'reported', 'description': '每天都喝酒'}),
        ('alcohol_use', '工作日不喝酒，周末喝一点', {'status': 'denied', 'description': '不喝酒'}),
        (
            'hydration',
            '我每天饮水1800毫升',
            {'status': 'reported', 'description': '我每天饮水1800毫升', 'amount': {'number': 800, 'unit': '毫升'}},
        ),
        (
            'hydration',
            '每天饮水1,800毫升',
            {'status': 'reported', 'description': '每天饮水1,800毫升', 'amount': {'number': 800, 'unit': '毫升'}},
        ),
        (
            'hydration',
            '每天饮水-800毫升',
            {'status': 'reported', 'description': '每天饮水-800毫升', 'amount': {'number': 800, 'unit': '毫升'}},
        ),
        (
            'diet_pattern',
            '每天摄入1mg',
            {'status': 'reported', 'description': '每天摄入1mg', 'amount': {'number': 1, 'unit': 'm'}},
        ),
        (
            'hydration',
            '每天饮水800毫升',
            {'status': 'reported', 'description': '每天饮水800毫升', 'amount': {'number': 800, 'unit': '毫'}},
        ),
        ('diet_pattern', '每天摄入1mg', {'status': 'reported', 'description': '每天摄入1mg', 'amount': '1m'}),
        ('hydration', '每天饮水800毫升', {'status': 'reported', 'description': '每天饮水800毫升', 'amount': '800毫'}),
        (
            'hydration',
            '喝水200毫升后散步30分钟',
            {'status': 'reported', 'description': '喝水200毫升后散步30分钟', 'amount': {'number': 30, 'unit': '毫升'}},
        ),
        (
            'physical_activity',
            '每周运动3次，每次20分钟',
            {'status': 'reported', 'description': '每周运动3次，每次20分钟', 'frequency': {'number': 20, 'unit': '次'}},
        ),
        (
            'physical_activity',
            '每周运动3次，每次30分钟',
            {'status': 'reported', 'description': '每周运动3次，每次30分钟', 'frequency': 30, 'duration': 3},
        ),
    ],
)
async def test_unproven_structure_preserves_the_full_original_answer(fact_type, answer, value):
    record, item = observation(answer, f'lifestyle.{fact_type}', value)
    original = deepcopy(record.session)
    actual = await extract(record, item)
    assert len(actual) == 1
    assert actual[0].value_type.value == 'text'
    assert actual[0].value == answer
    assert actual[0].assertion_level is AssertionLevel.USER_REPORTED
    assert actual[0].source.pointer == '/qaHistory/0/answer'
    assert record.session == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'fact_type,answer,value',
    [
        ('diet_pattern', '我从不喝含糖饮料', {'status': 'denied', 'negation': True, 'description': '我从不喝含糖饮料'}),
        ('alcohol_use', '饮酒频次记不清', {'status': 'unknown', 'unknown': True, 'description': '饮酒频次记不清'}),
        ('alcohol_use', '我不吸烟，周末喝啤酒', {'status': 'reported', 'description': '周末喝啤酒'}),
        (
            'hydration',
            '每天饮水800毫升',
            {'status': 'reported', 'description': '每天饮水800毫升', 'amount': {'number': 800.0, 'unit': '毫升'}},
        ),
        (
            'hydration',
            '每天饮水0.8升',
            {'status': 'reported', 'description': '每天饮水0.8升', 'amount': {'number': 0.8, 'unit': '升'}},
        ),
    ],
)
async def test_grounded_local_polarity_and_complete_quantities_keep_structure(fact_type, answer, value):
    record, item = observation(answer, f'lifestyle.{fact_type}', value)
    actual = await extract(record, item)
    assert len(actual) == 1
    assert actual[0].value_type.value == 'object'
    assert actual[0].value == value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'answer',
    ['最近工作压力很大，而且一直很焦虑', '最近工作压力挺大的', '我很焦虑，也很烦躁', '最近血压偏高'],
)
async def test_a_state_without_behavior_is_not_recorded_as_coping(answer):
    record, item = observation(answer, 'lifestyle.stress_coping', answer)
    assert await extract(record, item) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'answer',
    [
        '焦虑时我会出去散步',
        '忙完后会整理花草放松',
        '周末会去做陶艺',
        '晚上通过拼乐高放松',
        '下班后去钓鱼',
        '泡热水澡',
        '小憩片刻',
        '瑜伽',
    ],
)
async def test_free_form_coping_actions_are_not_limited_to_a_hobby_list(answer):
    record, item = observation(answer, 'lifestyle.stress_coping', answer)
    actual = await extract(record, item)
    assert len(actual) == 1
    assert actual[0].value == answer


@pytest.mark.asyncio
async def test_corrected_raw_behavior_is_pending_source_backed_and_idempotent_in_chat_memory():
    answer = '我从不喝含糖饮料'
    record, first = observation(answer, 'lifestyle.diet_pattern', {'status': 'reported', 'description': '喝含糖饮料'})
    second = deepcopy(first)
    second['value']['negation'] = False
    runtime, _ = _runtime({'candidates': [first, second]})
    subject = ReportIngestionFixture.subject()
    repository = FakeProfileRepository(subject)
    sequence = 0

    def next_id(prefix):
        nonlocal sequence
        sequence += 1
        return f'{prefix}-review-{sequence}'

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
    habits = [fact for fact in repository.facts if fact.fact_type == 'lifestyle.diet_pattern']
    assert len(habits) == 1 and replay.fact_count == 0
    fact = habits[0]
    assert fact.value == answer
    assert fact.status is FactStatus.PENDING
    assert fact.assertion_level is AssertionLevel.USER_REPORTED
    assert fact.sources[0].pointer == '/qaHistory/0/answer'
    assert fact.sources[0].excerpt_hash is not None
    memory = await RepositoryHealthProfileChatMemoryProvider(repository).build_context('user-1')
    assert answer in memory
    assert '"description":"喝含糖饮料"' not in memory
