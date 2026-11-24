"""Lifestyle extraction must validate the user's complete, source-local assertion."""

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

pytestmark = pytest.mark.asyncio


def source_input(answer, evidence, value, *, fact_type='lifestyle.diet_pattern', value_type=None):
    record = RecordFixture.record(
        qaHistory=[
            {
                'question': {'questionId': 'q_scope', 'question': '请描述您的相关生活习惯'},
                'answer': answer,
                'answeredAt': RecordFixture.now.isoformat(),
            }
        ]
    )
    item = {
        'answerIndex': 0,
        'factType': fact_type,
        'displayName': '生活行为',
        'valueType': value_type or ('object' if isinstance(value, dict) else 'text'),
        'value': value,
        'subjectKind': 'self',
        'subjectHint': '本人',
        'evidenceText': evidence,
        'confidence': 0.9,
    }
    return record, item


async def extract(record, *items):
    runtime, _ = _runtime({'candidates': list(items)})
    return await RecordAnswerCandidateExtractor(runtime.hub).extract(record, subject_id='subject-self')


@pytest.mark.parametrize(
    ('answer', 'evidence', 'value', 'fact_type'),
    [
        ('我从不喝含糖饮料', '喝含糖饮料', '喝含糖饮料', 'lifestyle.diet_pattern'),
        (
            '我从不喝含糖饮料',
            '喝含糖饮料',
            {'status': 'reported', 'description': '喝含糖饮料'},
            'lifestyle.diet_pattern',
        ),
        (
            '不是每天都喝酒',
            '每天都喝酒',
            {'status': 'reported', 'description': '每天都喝酒'},
            'lifestyle.alcohol_use',
        ),
        (
            '只在周末喝酒',
            '喝酒',
            {'status': 'reported', 'description': '喝酒'},
            'lifestyle.alcohol_use',
        ),
        (
            '如果不加班，我会散步',
            '我会散步',
            {'status': 'reported', 'description': '我会散步'},
            'lifestyle.physical_activity',
        ),
        (
            '工作日不喝酒，周末喝一点',
            '不喝酒',
            {'status': 'denied', 'description': '不喝酒'},
            'lifestyle.alcohol_use',
        ),
    ],
)
async def test_truncated_evidence_cannot_drop_denial_or_conditions(answer, evidence, value, fact_type):
    record, item = source_input(answer, evidence, value, fact_type=fact_type)
    original_session = deepcopy(record.session)

    candidates = await extract(record, item)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.value_type.value == 'text'
    assert candidate.value == answer
    assert candidate.evidence_text == answer
    assert candidate.assertion_level is AssertionLevel.USER_REPORTED
    assert candidate.source.pointer == '/qaHistory/0/answer'
    assert candidate.source.observed_at == RecordFixture.now
    assert record.session == original_session


@pytest.mark.parametrize(
    ('answer', 'evidence', 'description', 'number', 'unit'),
    [
        ('每天饮水1800毫升', '800毫升', '800毫升', 800, '毫升'),
        ('每天饮水1,800毫升', '800毫升', '800毫升', 800, '毫升'),
        ('每天饮水-800毫升', '800毫升', '800毫升', 800, '毫升'),
        ('每天饮水−800毫升', '800毫升', '800毫升', 800, '毫升'),
        ('每天睡眠8.5小时', '5小时', '5小时', 5, '小时'),
        ('每天饮水1800毫升', '每天饮水1800毫', '每天饮水1800毫', 1800, '毫'),
        ('每天摄入钠1mg', '每天摄入钠1m', '每天摄入钠1m', 1, 'm'),
    ],
)
async def test_source_expansion_precedes_complete_number_and_unit_validation(
    answer, evidence, description, number, unit
):
    value = {'status': 'reported', 'description': description, 'amount': {'number': number, 'unit': unit}}
    record, item = source_input(answer, evidence, value)

    candidates = await extract(record, item)

    assert len(candidates) == 1
    assert candidates[0].value_type.value == 'text'
    assert candidates[0].value == answer
    assert candidates[0].evidence_text == answer


async def test_quantity_from_another_behavior_cannot_support_a_structured_water_amount():
    answer = '我每天饮水1800毫升，散步30分钟'
    value = {
        'status': 'reported',
        'description': '每天饮水1800毫升',
        'amount': {'number': 30, 'unit': '分钟'},
    }
    record, item = source_input(answer, '每天饮水1800毫升', value)

    candidates = await extract(record, item)

    assert len(candidates) == 1
    assert candidates[0].value_type.value == 'text'
    assert candidates[0].value == '我每天饮水1800毫升'
    assert candidates[0].evidence_text == candidates[0].value


@pytest.mark.parametrize(
    ('answer', 'evidence', 'description', 'status', 'fact_type'),
    [
        (
            '我以前每天散步30分钟',
            '每天散步30分钟',
            '每天散步30分钟',
            'reported',
            'lifestyle.physical_activity',
        ),
        ('工作日，我不喝酒', '我不喝酒', '我不喝酒', 'denied', 'lifestyle.alcohol_use'),
        ('我不喝酒，也不吸烟', '不吸烟', '不吸烟', 'reported', 'lifestyle.smoking_status'),
    ],
)
async def test_original_time_and_continued_denial_cannot_be_lost(answer, evidence, description, status, fact_type):
    value = {'status': status, 'description': description}
    record, item = source_input(answer, evidence, value, fact_type=fact_type)

    candidates = await extract(record, item)

    assert len(candidates) == 1
    assert candidates[0].value_type.value == 'text'
    assert candidates[0].value == answer
    assert candidates[0].evidence_text == answer


@pytest.mark.parametrize(
    ('answer', 'number', 'unit'),
    [
        ('我每天饮水1800毫升，每次散步30分钟', 30, '分钟'),
        ('我每天饮水1800毫升和喝咖啡200毫升', 200, '毫升'),
    ],
)
async def test_attached_but_distinct_behavior_cannot_supply_the_water_quantity(answer, number, unit):
    description = '我每天饮水1800毫升'
    value = {'status': 'reported', 'description': description, 'amount': {'number': number, 'unit': unit}}
    record, item = source_input(answer, description, value)

    candidates = await extract(record, item)

    assert len(candidates) == 1
    assert candidates[0].value_type.value == 'text'
    assert candidates[0].value in answer
    assert description in candidates[0].value
    assert candidates[0].evidence_text == candidates[0].value


@pytest.mark.parametrize(
    'answer',
    ['我每天饮水1800毫升，每次散步30分钟', '我每天饮水1800毫升和喝咖啡200毫升'],
)
async def test_the_correct_water_quantity_remains_valid_beside_a_distinct_behavior(answer):
    description = '我每天饮水1800毫升'
    value = {'status': 'reported', 'description': description, 'amount': {'number': 1800, 'unit': '毫升'}}
    record, item = source_input(answer, description, value)

    candidates = await extract(record, item)

    assert len(candidates) == 1
    assert candidates[0].value_type.value == 'object'
    assert candidates[0].value == value
    assert candidates[0].evidence_text in answer
    assert description in candidates[0].evidence_text


async def test_explicit_historical_behavior_preserves_its_structured_time_scope():
    answer = '我以前每天散步30分钟'
    value = {'status': 'reported', 'description': answer, 'duration': {'number': 30, 'unit': '分钟'}}
    record, item = source_input(answer, answer, value, fact_type='lifestyle.physical_activity')

    candidates = await extract(record, item)

    assert len(candidates) == 1
    assert candidates[0].value_type.value == 'object'
    assert candidates[0].value == value
    assert candidates[0].evidence_text == answer


@pytest.mark.parametrize(('clause', 'status'), [('也不吸烟', 'denied'), ('也吸烟', 'reported')])
async def test_continued_smoking_statement_keeps_its_own_polarity(clause, status):
    answer = f'我不喝酒，{clause}'
    description = clause.removeprefix('也')
    value = {'status': status, 'description': description}
    record, item = source_input(answer, description, value, fact_type='lifestyle.smoking_status')

    candidates = await extract(record, item)

    assert len(candidates) == 1
    assert candidates[0].value_type.value == 'object'
    assert candidates[0].value == value
    assert candidates[0].evidence_text in answer
    assert clause in candidates[0].evidence_text


@pytest.mark.parametrize('status', ['reported', 'denied', 'unknown'])
@pytest.mark.parametrize(
    'answer',
    [
        '我喝含糖饮料。我从不喝含糖饮料',
        '我工作日不喝含糖饮料。我周末喝含糖饮料',
        '我以前喝含糖饮料。我现在不喝含糖饮料',
    ],
)
async def test_all_occurrences_are_checked_without_selecting_by_model_polarity(answer, status):
    record, item = source_input(answer, '喝含糖饮料', {'status': status, 'description': '喝含糖饮料'})

    assert await extract(record, item) == ()


@pytest.mark.parametrize(
    ('answer', 'expected'),
    [
        ('父亲喝含糖饮料。我从不喝含糖饮料', '我从不喝含糖饮料'),
        ('我从不喝含糖饮料。父亲喝含糖饮料', '我从不喝含糖饮料'),
    ],
)
async def test_explicit_source_subject_can_disambiguate_identical_behavior_mentions(answer, expected):
    record, item = source_input(answer, '喝含糖饮料', {'status': 'reported', 'description': '喝含糖饮料'})

    candidates = await extract(record, item)

    assert len(candidates) == 1
    assert candidates[0].value_type.value == 'text'
    assert candidates[0].value == expected
    assert candidates[0].evidence_text == expected
    assert candidates[0].subject_id == 'subject-self'


@pytest.mark.parametrize(
    ('answer', 'evidence', 'value', 'fact_type', 'value_type'),
    [
        ('父亲喝含糖饮料', '喝含糖饮料', '喝含糖饮料', 'lifestyle.diet_pattern', 'text'),
        (
            '父亲喝含糖饮料',
            '喝含糖饮料',
            {'status': 'reported', 'description': '喝含糖饮料'},
            'lifestyle.diet_pattern',
            'object',
        ),
        ('父亲目前吸烟', '目前吸烟', 'current', 'lifestyle.smoking_status', 'coded'),
        ('父亲每天喝水，约800毫升', '800毫升', '800毫升', 'lifestyle.diet_pattern', 'text'),
    ],
)
async def test_source_subject_is_checked_before_accepting_text_object_or_coded_values(
    answer, evidence, value, fact_type, value_type
):
    record, item = source_input(answer, evidence, value, fact_type=fact_type, value_type=value_type)

    assert await extract(record, item) == ()


@pytest.mark.parametrize(
    ('answer', 'evidence', 'fact_type'),
    [
        ('我会和朋友聊天放松', '聊天放松', 'lifestyle.stress_coping'),
        ('我每天和父亲一起散步30分钟', '散步30分钟', 'lifestyle.physical_activity'),
        ('我每天陪母亲散步', '散步', 'lifestyle.physical_activity'),
    ],
)
async def test_accompanying_people_do_not_remove_the_users_own_behavior(answer, evidence, fact_type):
    record, item = source_input(answer, evidence, answer, fact_type=fact_type)

    candidates = await extract(record, item)

    assert len(candidates) == 1
    assert candidates[0].subject_id == 'subject-self'
    assert candidates[0].value == answer
    assert candidates[0].evidence_text == answer


async def test_coordinated_distinct_owners_are_not_mistaken_for_accompanying_people():
    record, item = source_input(
        '我不喝酒和父亲喝酒',
        '喝酒',
        {'status': 'reported', 'description': '喝酒'},
        fact_type='lifestyle.alcohol_use',
    )

    candidates = await extract(record, item)

    # A uniquely recovered self clause is safe; an unresolved mixed scope is not.
    assert len(candidates) <= 1
    for candidate in candidates:
        assert candidate.value_type.value == 'text'
        assert candidate.value == '我不喝酒'
        assert candidate.evidence_text == candidate.value


async def test_legacy_smoking_code_cannot_choose_one_of_conflicting_source_mentions():
    record, item = source_input(
        '我目前吸烟。我以前吸烟，现在不吸烟',
        '吸烟',
        'current',
        fact_type='lifestyle.smoking_status',
        value_type='coded',
    )

    assert await extract(record, item) == ()


async def test_legacy_smoking_code_cannot_reverse_a_truncated_source_denial():
    record, item = source_input(
        '我从不吸烟',
        '吸烟',
        'current',
        fact_type='lifestyle.smoking_status',
        value_type='coded',
    )

    candidates = await extract(record, item)

    assert len(candidates) == 1
    assert candidates[0].value_type.value == 'text'
    assert candidates[0].value == '我从不吸烟'
    assert candidates[0].evidence_text == candidates[0].value


@pytest.mark.parametrize(
    ('answer', 'evidence', 'code'),
    [('我目前吸烟', '吸烟', 'current'), ('我从不吸烟', '吸烟', 'never'), ('我已戒烟', '戒烟', 'former')],
)
async def test_source_supported_legacy_smoking_codes_remain_coded(answer, evidence, code):
    record, item = source_input(answer, evidence, code, fact_type='lifestyle.smoking_status', value_type='coded')

    candidates = await extract(record, item)

    assert len(candidates) == 1
    assert candidates[0].value_type.value == 'coded'
    assert candidates[0].value == code
    assert candidates[0].evidence_text == answer


@pytest.mark.parametrize('answer', ['我从不喝含糖饮料。我从不喝含糖饮料', ['我从不喝含糖饮料', '我从不喝含糖饮料']])
async def test_identical_repeated_source_assertions_are_not_treated_as_a_conflict(answer):
    record, item = source_input(answer, '喝含糖饮料', {'status': 'reported', 'description': '喝含糖饮料'})

    candidates = await extract(record, item)

    assert len(candidates) == 1
    assert candidates[0].value == '我从不喝含糖饮料'
    assert candidates[0].evidence_text == candidates[0].value


@pytest.mark.parametrize(
    ('answer', 'evidence'),
    [
        ({'喝含糖饮料': False}, '喝含糖饮料'),
        ({'回答': {'喝含糖饮料': '没有'}}, '喝含糖饮料'),
        (['喝含糖', '饮料'], '喝含糖饮料'),
        (['我喝水', '每天散步'], '我喝水", "每天散步'),
    ],
)
async def test_only_original_string_values_can_supply_evidence(answer, evidence):
    record, item = source_input(answer, evidence, evidence)

    assert await extract(record, item) == ()


async def test_nested_string_value_is_expanded_without_including_keys_or_other_values():
    answer = {'备注': ['我从不喝含糖饮料'], '其他': {'父亲': '每天散步'}}
    record, item = source_input(answer, '喝含糖饮料', {'status': 'reported', 'description': '喝含糖饮料'})

    candidates = await extract(record, item)

    assert len(candidates) == 1
    assert candidates[0].value == '我从不喝含糖饮料'
    assert candidates[0].evidence_text == candidates[0].value
    assert record.session['qaHistory'][0]['answer'] == answer


@pytest.mark.parametrize(
    ('answer', 'evidence', 'fact_type'),
    [
        ({'回答': {'习惯': '我周末喝"零糖"饮料'}}, '我周末喝"零糖"饮料', 'lifestyle.diet_pattern'),
        ({'回答': ['我每天散步30分钟\n每周3次']}, '我每天散步30分钟\n每周3次', 'lifestyle.physical_activity'),
    ],
)
async def test_original_string_leaf_quotes_and_newlines_are_not_json_escaped_evidence(answer, evidence, fact_type):
    record, item = source_input(answer, evidence, evidence, fact_type=fact_type)

    candidates = await extract(record, item)

    assert len(candidates) == 1
    assert candidates[0].value_type.value == 'text'
    assert candidates[0].value == evidence
    assert candidates[0].evidence_text == evidence
    assert record.session['qaHistory'][0]['answer'] == answer


async def test_independent_valid_short_clause_retains_its_structured_value():
    value = {'status': 'reported', 'description': '我每天散步30分钟', 'duration': {'number': 30, 'unit': '分钟'}}
    record, item = source_input(
        '父亲喝含糖饮料。我每天散步30分钟。我不喝酒',
        '我每天散步30分钟',
        value,
        fact_type='lifestyle.physical_activity',
    )

    candidates = await extract(record, item)

    assert len(candidates) == 1
    assert candidates[0].value_type.value == 'object'
    assert candidates[0].value == value
    assert candidates[0].evidence_text == '我每天散步30分钟'


async def test_neighboring_quantity_continuation_is_preserved_with_the_behavior():
    answer = '我每周运动3次，每次30分钟'
    value = {
        'status': 'reported',
        'description': '我每周运动3次',
        'frequency': {'number': 3, 'unit': '次'},
        'duration': {'number': 30, 'unit': '分钟'},
    }
    record, item = source_input(answer, '我每周运动3次', value, fact_type='lifestyle.physical_activity')

    candidates = await extract(record, item)

    assert len(candidates) == 1
    assert candidates[0].value_type.value == 'object'
    assert candidates[0].value == value
    assert candidates[0].evidence_text == answer


async def test_expanded_source_keeps_original_source_hash_and_candidate_identity():
    answer = {'回答': '我从不喝含糖饮料', '其他': '我每天散步'}
    record, item = source_input(answer, '喝含糖饮料', {'status': 'reported', 'description': '喝含糖饮料'})
    full_item = deepcopy(item)
    full_item['evidenceText'] = '我从不喝含糖饮料'

    truncated = await extract(record, item)
    complete = await extract(record, full_item)

    assert len(truncated) == len(complete) == 1
    assert truncated[0].candidate_id == complete[0].candidate_id
    assert truncated[0].source == complete[0].source
    assert truncated[0].source.excerpt_hash is not None
    assert truncated[0].source.pointer == '/qaHistory/0/answer'


async def test_recovered_raw_behavior_remains_pending_idempotent_and_readable_in_chat():
    answer = '我从不喝含糖饮料'
    record, item = source_input(answer, '喝含糖饮料', {'status': 'reported', 'description': '喝含糖饮料'})
    original_session = deepcopy(record.session)
    runtime, _ = _runtime({'candidates': [item]})
    subject = ReportIngestionFixture.subject()
    repository = FakeProfileRepository(subject)
    sequence = 0

    def next_id(prefix):
        nonlocal sequence
        sequence += 1
        return f'{prefix}-source-scope-{sequence}'

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
    assert len(habits) == 1
    assert replay.fact_count == 0
    fact = habits[0]
    assert fact.value == answer
    assert fact.status is FactStatus.PENDING
    assert fact.assertion_level is AssertionLevel.USER_REPORTED
    assert fact.sources[0].pointer == '/qaHistory/0/answer'
    assert fact.sources[0].excerpt_hash is not None
    assert record.session == original_session
    memory = await RepositoryHealthProfileChatMemoryProvider(repository).build_context('user-1')
    assert answer in memory
    assert '"description":"喝含糖饮料"' not in memory
