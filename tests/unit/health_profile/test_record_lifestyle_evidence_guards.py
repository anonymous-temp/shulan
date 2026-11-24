"""Source-local qualifiers, quantities and explicit coping survive profile extraction."""

import pytest

from health_profile import RecordAnswerCandidateExtractor
from tests.unit.health_profile.test_record_ingestion import RecordFixture
from tests.unit.health_profile.test_record_ingestion import _runtime


async def extract(answer, value, *, fact_type='lifestyle.diet_pattern'):
    record = RecordFixture.record(
        qaHistory=[
            {
                'question': {'questionId': 'q_guard', 'question': '请说说相应的日常生活习惯'},
                'answer': answer,
                'answeredAt': RecordFixture.now.isoformat(),
            }
        ]
    )
    candidate = {
        'answerIndex': 0,
        'factType': fact_type,
        'displayName': '模型输出',
        'valueType': 'object' if isinstance(value, dict) else 'text',
        'value': value,
        'subjectKind': 'self',
        'subjectHint': '本人',
        'evidenceText': answer,
        'confidence': 0.9,
    }
    runtime, _ = _runtime({'candidates': [candidate]})
    return await RecordAnswerCandidateExtractor(runtime.hub).extract(record, subject_id='subject-self')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'answer,value',
    [
        ('我从不喝含糖饮料', '喝含糖饮料'),
        ('我从不喝含糖饮料', {'status': 'reported', 'description': '喝含糖饮料'}),
        ('我从不喝含糖饮料', {'status': 'reported', 'description': '我从不喝含糖饮料'}),
        ('我每天散步半小时', {'status': 'denied', 'negation': True, 'description': '我每天散步半小时'}),
        ('我每天散步半小时', {'status': 'unknown', 'unknown': True, 'description': '我每天散步半小时'}),
        ('我从不吸烟', {'status': 'denied', 'negation': False, 'description': '我从不吸烟'}),
        ('饮酒频次记不清', {'status': 'reported', 'description': '饮酒频次记不清'}),
        ('不是每天都喝酒', {'status': 'denied', 'description': '不是每天都喝酒'}),
        ('不喝白酒但喝啤酒', {'status': 'denied', 'description': '不喝白酒但喝啤酒'}),
        ('忙的时候每晚散步20分钟', {'status': 'reported', 'description': '每晚散步20分钟'}),
        ('如果不加班，我会散步', {'status': 'reported', 'description': '我会散步'}),
        ('只在周末喝酒', {'status': 'reported', 'description': '喝酒'}),
        ('工作日不喝酒，周末喝一点', {'status': 'denied', 'description': '不喝酒'}),
        ('我每天散步半小时', {'STATUS': 'denied', 'description': '我每天散步半小时'}),
        ('我每天散步半小时', {'status': 'reported', 'NEGATION': True, 'description': '我每天散步半小时'}),
        ('我每天散步半小时', {'status': {'reported': True}, 'description': '我每天散步半小时'}),
        (
            '我每天散步半小时',
            {'status': 'reported', 'description': '我每天散步半小时', 'details': {'negation': True}},
        ),
    ],
)
async def test_unproved_polarity_or_dropped_qualifier_falls_back_to_full_original(answer, value):
    result = await extract(answer, value)
    assert len(result) == 1
    assert result[0].value_type.value == 'text'
    assert result[0].value == answer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'answer,value',
    [
        ('我从不吸烟', {'status': 'denied', 'negation': True, 'description': '我从不吸烟'}),
        ('饮酒频次记不清', {'status': 'unknown', 'unknown': True, 'description': '饮酒频次记不清'}),
        ('不是每天都喝酒', {'status': 'reported', 'description': '不是每天都喝酒'}),
        ('不喝白酒但喝啤酒', {'status': 'reported', 'description': '喝啤酒'}),
        ('不吸烟，周末喝一点酒', {'status': 'reported', 'description': '周末喝一点酒'}),
        ('饮酒频次记不清，但每周散步3次', {'status': 'reported', 'description': '每周散步3次'}),
        ('只在周末喝酒', {'status': 'reported', 'description': '只在周末喝酒'}),
        ('近两个月常不吃早餐', {'status': 'reported', 'description': '近两个月常不吃早餐'}),
    ],
)
async def test_qualifiers_are_local_and_legitimate_states_or_conditioned_behavior_remain_structured(answer, value):
    result = await extract(answer, value)
    assert len(result) == 1
    assert result[0].value_type.value == 'object'
    assert result[0].value == value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'answer,description,amount',
    [
        ('我每天饮水1800毫升', '我每天饮水1800毫升', {'number': 800, 'unit': '毫升'}),
        ('我每天运动130分钟', '我每天运动130分钟', {'number': 30, 'unit': '分钟'}),
        ('我每天睡眠8.5小时', '我每天睡眠8.5小时', {'number': 8, 'unit': '小时'}),
        ('我每天饮水800毫升，每次运动30分钟', '我每天饮水800毫升', {'number': 30, 'unit': '毫升'}),
        ('我每天饮水1800毫升', '我每天饮水1800毫升', '800'),
        ('我每天饮水1800毫升', '我每天饮水1800毫升', '800毫升'),
        ('我每天饮水1,800毫升', '我每天饮水1,800毫升', {'number': 800, 'unit': '毫升'}),
        ('我每天饮水1,800毫升', '我每天饮水1,800毫升', '800毫升'),
        ('我每天饮水-800毫升', '我每天饮水-800毫升', {'number': 800, 'unit': '毫升'}),
        ('我每天饮水−800毫升', '我每天饮水−800毫升', {'number': 800, 'unit': '毫升'}),
        ('我每天饮水1,80毫升', '我每天饮水1,80毫升', {'number': 80, 'unit': '毫升'}),
        ('我每天补充1mg', '我每天补充1mg', {'number': 1, 'unit': 'm'}),
        ('我每天饮水800毫升', '我每天饮水800毫升', {'number': 800, 'unit': '毫'}),
        ('我每周运动3次/周', '我每周运动3次/周', {'number': 3, 'unit': '次'}),
        ('我每天补充1mg', '我每天补充1mg', '1m'),
        ('我每天饮水800毫升', '我每天饮水800毫升', '800毫'),
    ],
)
async def test_numeric_substrings_or_cross_context_quantity_units_fall_back_to_original(answer, description, amount):
    result = await extract(
        answer,
        {'status': 'reported', 'description': description, 'amount': amount},
        fact_type='lifestyle.hydration',
    )
    assert len(result) == 1
    assert result[0].value_type.value == 'text'
    assert result[0].value == answer


@pytest.mark.asyncio
async def test_mixed_subject_evidence_cannot_be_retained_as_a_self_behavior():
    result = await extract(
        '我每天饮水1800毫升；同事每天饮水800毫升',
        {
            'status': 'reported',
            'description': '我每天饮水1800毫升',
            'amount': {'number': 800, 'unit': '毫升'},
        },
        fact_type='lifestyle.hydration',
    )
    assert result == ()


@pytest.mark.asyncio
async def test_bare_numbers_cannot_swap_frequency_and_duration():
    answer = '我每周运动3次，每次30分钟'
    result = await extract(
        answer,
        {'status': 'reported', 'description': answer, 'frequency': 30, 'duration': 3},
        fact_type='lifestyle.physical_activity',
    )
    assert len(result) == 1
    assert result[0].value_type.value == 'text'
    assert result[0].value == answer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'answer,value',
    [
        (
            '我每天饮水约800毫升',
            {'status': 'reported', 'description': '我每天饮水约800毫升', 'amount': {'number': 800, 'unit': '毫升'}},
        ),
        (
            '我每周运动3次，每次30分钟',
            {
                'status': 'reported',
                'description': '我每周运动3次',
                'frequency': {'number': 3, 'unit': '次'},
                'duration': {'number': 30, 'unit': '分钟'},
            },
        ),
        (
            '我每天饮水1,800毫升',
            {'status': 'reported', 'description': '我每天饮水1,800毫升', 'amount': {'number': 1800, 'unit': '毫升'}},
        ),
        (
            '我每天补充1mg',
            {'status': 'reported', 'description': '我每天补充1mg', 'amount': {'number': 1, 'unit': 'mg'}},
        ),
    ],
)
async def test_complete_quantity_pairs_and_continued_behavior_details_remain_structured(answer, value):
    result = await extract(answer, value, fact_type='lifestyle.physical_activity')
    assert len(result) == 1
    assert result[0].value == value
    assert result[0].value_type.value == 'object'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'answer',
    [
        '最近工作压力很大，而且一直很焦虑',
        '最近工作压力挺大的',
        '我很焦虑，也很烦躁',
        '血压偏高',
        '我会感到焦虑',
        '我需要放松',
        '我不知道怎么放松',
        '这段时间工作很忙',
        '最近事情很多',
        '不知道怎样放松',
    ],
)
async def test_state_or_intention_without_a_coping_behavior_does_not_create_a_coping_fact(answer):
    assert await extract(answer, answer, fact_type='lifestyle.stress_coping') == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'answer',
    [
        '焦虑时会散步',
        '我焦虑时会折纸',
        '我会捯饬花草',
        '下班后做陶艺',
        '搓麻将',
        '阅读',
        '我晚上会练习手碟',
        '焦虑时我会出去散步',
        '下班后去钓鱼',
        '散步或运动',
        '与亲友交流',
        '听音乐',
        '休息或独处',
        '每天有固定休息时间',
        '主要在休息日安排',
        '有空时零散休息',
        '很少专门留出时间',
        '没有固定节奏',
        '会定时暂停休息',
        '完成一段任务后休息',
        '通常忙完才休息',
        '泡热水澡',
        '小憩片刻',
        '瑜伽',
    ],
)
async def test_open_ended_actual_actions_and_rest_arrangements_are_kept(answer):
    result = await extract(answer, answer, fact_type='lifestyle.stress_coping')
    assert len(result) == 1
    assert result[0].value == answer
    assert result[0].source.pointer == '/qaHistory/0/answer'
