"""All interview purposes publish compact, independent answers, not topic menus."""

from copy import deepcopy

import pytest

from modules.record.application import fallback
from modules.record.application import flow
from modules.record.application import intake
from modules.record.application import interview
from modules.record.application import lifestyle
from modules.record.application.presentation import validate_choice_labels

from .test_conservative_fallback import session
from .test_dynamic_record import cached
from .test_dynamic_record import save_cache
from .test_dynamic_record import setup_manager
from .test_dynamic_record import submit


def target_for(dimension):
    return {
        'topic': lifestyle.LABELS[dimension],
        'aspect': 'habits',
        'dimension': dimension,
        'scope': 'general',
        'complaintIds': [],
    }


@pytest.mark.parametrize('dimension', sorted(lifestyle.TOPICS))
def test_all_24_lifestyle_purposes_have_one_shared_compact_option_set(dimension):
    canonical = lifestyle.question_fields(dimension)
    validate_choice_labels(canonical['options'])
    assert all(len(label) <= 16 for label in canonical['options'])
    target = target_for(dimension)
    assert interview.history_choice_fields(target) == {key: canonical[key] for key in ('questionType', 'options')}
    assert fallback._gap_question(target) == canonical
    draft = session(count=3)
    draft['qaFlow']['intakeQuestionCount'] = 3
    target = interview.select_target(draft['qaFlow'], target)
    assert target['choiceConstraints']['options'] == canonical['options']
    presentation = flow.TurnPresentation(
        assistantMessage={
            'acknowledgement': '日常生活中的具体安排，也能帮助了解您的健康背景。',
            'rationale': '',
            'evidence': [],
        },
        responses=[],
        question={'question': canonical['question'], 'questionType': 'radio', 'options': ['大多规律', '时间不固定']},
    )
    decision = {'action': 'ask_intake', 'target': target, 'responses': []}
    flow.apply_presentation(draft, decision, presentation, '')
    assert draft['currentQuestion']['options'] == canonical['options']
    assert draft['currentQuestion']['questionType'] == canonical['questionType']
    assert draft['qaFlow']['intakeQuestionCount'] == 3


@pytest.mark.parametrize('dimension', sorted(lifestyle.TOPICS))
def test_partial_fallback_cannot_restore_a_generic_or_combined_answer(dimension):
    before = session(count=3)
    draft = deepcopy(before)
    draft['qaFlow']['intakeQuestionCount'] = 4
    target = interview.select_target(draft['qaFlow'], target_for(dimension))
    canonical = lifestyle.question_fields(dimension)
    result = fallback.build_fallback(
        before,
        answer='有些情况记不清',
        validated_draft=draft,
        decision={'action': 'ask_intake', 'target': target, 'responses': []},
        partial_presentation={
            'question': flow.GeneratedQuestion(
                question=canonical['question'], questionType='radio', options=['某种生活方式', '整体健康情况']
            )
        },
        failure_kind='validation',
    )
    assert result['currentQuestion']['options'] == canonical['options']
    assert result['currentQuestion']['questionType'] == canonical['questionType']
    assert result['qaFlow']['intakeQuestionCount'] == 4


@pytest.mark.parametrize(
    'label',
    [
        '某个身体不适或症状',
        '某种疾病',
        '某病',
        '某症状',
        '某疾病',
        '某方面的健康问题',
        '整体健康情况',
        '长期健康维护',
        '日常体力与精力',
        '睡眠质量',
        '消化系统（胃胀／便秘）',
        '运动(跑步/骑行)',
        '跑步或骑行',
        '力量训练或球类',
        '休息或独处',
        '头晕/头痛',
        '血糖升高或糖尿病',
        '喝茶或咖啡',
        '胰岛素抵抗或糖尿病',
        '1型糖尿病/2型糖尿病',
        '每周跑步1天/骑行2天',
        '陶艺、钓鱼',
        '一个非常长的选项而且还要继续解释各种不同的背景及相关情况',
    ],
)
def test_placeholders_topic_menus_explanatory_brackets_and_distinct_answers_are_not_buttons(label):
    with pytest.raises(ValueError):
        validate_choice_labels(['没有', label])


@pytest.mark.parametrize(
    'label',
    [
        '糖尿病',
        '脂肪肝',
        '跑步',
        '不清楚',
        '每周1～2天',
        '每月1次或更少',
        '每周一次或更少',
        '1毫克/升',
        '5mg/dL',
        '1/2杯',
        '1小时或更短',
    ],
)
def test_names_quantity_units_ranges_and_clear_states_remain_valid(label):
    validate_choice_labels(['没有', label])


def test_opening_suggestions_are_concrete_multiselects_without_becoming_patient_facts():
    state = flow.new_flow()
    before = deepcopy(state)
    target = interview.fixed_target(state, 0)
    choices = target['choiceConstraints']
    assert choices['questionType'] == 'checkbox'
    assert len(choices['options']) == flow.OPENING_MAX_OPTIONS == 12
    assert choices['options'][-2:] == ['暂无改善需求', '不清楚']
    assert set(choices['options'][:-2]) <= set(interview.GOAL_OPTIONS)
    assert state == before
    assert fallback._opening_question()['options'] == choices['options']


@pytest.mark.parametrize('source', ['本次报告', '长期画像', '附带资料'])
def test_only_existing_source_backed_facts_order_related_goal_suggestions(source):
    state = flow.new_flow()
    state['intake']['facts']['general:medical_history'] = {
        'dimension': 'medical_history',
        'status': 'known',
        'summary': '血糖检查结果需要结合日常生活了解',
        'evidence': [{'source': source, 'quote': '血糖'}],
    }
    before = deepcopy(state)
    assert interview.fixed_target(state, 0)['choiceConstraints']['options'][0] == '控制血糖'
    assert state == before
    state['intake']['facts']['general:medical_history']['status'] = 'unknown'
    assert interview.fixed_target(state, 0)['choiceConstraints']['options'] == interview.goal_choice_fields()['options']


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_existing_long_or_generic_cached_buttons_restore_unchanged_and_keep_the_raw_answer(streaming):
    manager, module, gateway, _ = setup_manager()
    await manager.createSession('u', 's')
    previous = cached(module)
    previous['currentQuestion'].update(
        question='原来的问题', questionType='radio', options=['某个身体不适或症状', '消化系统（胃胀／便秘）']
    )
    save_cache(module, previous)
    calls = len(gateway.requests)
    restored = await manager.getSession('u', 's')
    assert restored['questionId'] == previous['currentQuestion']['questionId']
    assert restored['options'] == previous['currentQuestion']['options']
    assert len(gateway.requests) == calls
    answer = '我身体不舒服，但具体表现说不清楚'
    following = await submit(manager, restored, None, other=answer, streaming=streaming)
    assert cached(module)['qaHistory'][-1]['answer'] == answer
    assert following['intakeQuestionCount'] == 1
    assert following['questionType'] == 'checkbox'
    assert {'高血压', '糖尿病', '没有', '不清楚'} <= set(following['options'])
    validate_choice_labels(following['options'])


def test_fixed_supplement_and_known_topic_statuses_remain_explicit_short_answers():
    for dimension in ('goal', 'medical_history', 'family_history', *intake.LIFESTYLE_DIMENSIONS):
        fields = interview.history_choice_fields({'dimension': dimension, 'supplementation': True})
        assert fields == {'questionType': 'radio', 'options': ['没有需要补充的内容', '目前记不清']}
        validate_choice_labels(fields['options'])
    assert fallback._supplement_question()['options'] == ['没有了']
