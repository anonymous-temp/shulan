"""Patient-facing model vocabulary and clickable answers through the Record flow."""

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from modules.record.application import flow

from .test_dynamic_record import cached
from .test_dynamic_record import setup_manager
from .test_dynamic_record import submit
from .test_record_agent import PROJECT_ROOT


def patient_assessment():
    return {
        '意图': '回答问题',
        '包含实质信息': True,
        '建档信息充分': False,
        'intake': {'complaints': [], 'priorityClarificationNeeded': False, 'facts': []},
        '患者情况理解': '患者描述餐后腹胀，尚需结合伴随表现及饮食背景理解。',
        '疑问': [],
        '信息缺口': [
            {
                '主题': '腹胀',
                '角度': '症状表现',
                '内容': '伴随表现',
                '提问关联': '理解相关表现',
                '待补充信息': '伴随表现',
            },
            {
                '主题': '腹胀',
                '角度': '生活习惯',
                '内容': '生活习惯',
                '提问关联': '理解饮食背景',
                '待补充信息': '与饮食的关联',
            },
        ],
    }


def latest_analysis(gateway):
    request = next(request for request in reversed(gateway.requests) if request.profile.name == 'record.plan')
    return json.JSONDecoder().raw_decode(request.content[request.content.index('{') :])[0]


@pytest.mark.parametrize(
    ('label', 'expected'),
    [
        ('回答问题', 'answer'),
        ('补充疑问', 'additional'),
        ('没有更多问题', 'no_more'),
        ('结束交流', 'stop'),
        ('表达不明确', 'unclear'),
    ],
)
def test_patient_intent_labels_map_to_existing_internal_values(label, expected):
    payload = patient_assessment()
    payload['意图'] = label
    result = flow.parse_model_object(json.dumps(payload, ensure_ascii=False), flow.PatientAssessment)
    assert result.intent == expected
    assert result.model_dump()['intent'] == expected
    assert '意图' not in result.model_dump()


@pytest.mark.parametrize(
    ('label', 'expected'),
    [
        ('症状表现', 'symptoms'),
        ('生活习惯', 'habits'),
        ('既往病史', 'medical_history'),
        ('家族病史', 'family_history'),
        ('其他症状', 'other_symptoms'),
    ],
)
def test_legacy_information_angles_map_to_existing_focus_policy_without_dimension(label, expected):
    payload = patient_assessment()
    payload['信息缺口'][0].pop('内容')
    payload['信息缺口'][0]['角度'] = label
    result = flow.PatientAssessment.model_validate(payload)
    assert result.gaps[0].aspect == expected
    assert result.gaps[0].dimension is None
    assert result.gaps[0].informationNeeded == '伴随表现'


@pytest.mark.parametrize(
    'invalid',
    [{'意图': '随意结束'}, {'包含实质信息': '是'}, {'阶段': '完成'}, {'intent': 'stop'}],
)
def test_localized_contract_keeps_strict_types_and_rejects_unknown_or_conflicting_fields(invalid):
    with pytest.raises(ValidationError):
        flow.PatientAssessment.model_validate(patient_assessment() | invalid)


def test_localized_concerns_accept_paraphrases_and_validate_existing_ids():
    statement = '饭后腹胀\n是否与 IBS 有关？'
    paraphrase = '餐后腹胀，想了解是否与 IBS 相关'
    payload = patient_assessment()
    payload['疑问'] = [
        {
            '编号': None,
            '问题': '是否与 IBS 有关？',
            '用户原话': paraphrase,
            '可直接回应': False,
            '待补充信息': '持续时间',
        },
    ]
    payload['意图'] = '补充疑问'
    result = flow.PatientAssessment.model_validate(payload)
    session = {'qaFlow': flow.new_flow(), 'currentQuestion': {'questionId': 'q'}}
    session['qaFlow']['phase'] = 'supplement'
    flow.validate_assessment(result, session, {'content': statement})
    draft, decision = flow.advance_flow(session, result, answer={'content': statement})
    accepted = draft['qaFlow']['concerns'][0]
    assert accepted['text'] == '是否与 IBS 有关？'
    assert accepted['evidence'] == paraphrase
    assert decision['responses'][0]['concernId'] == accepted['id']
    payload['疑问'][0]['编号'] = accepted['id']
    flow.validate_assessment(flow.PatientAssessment.model_validate(payload), draft, ['仍然腹胀'])
    with pytest.raises(ValueError, match='尚未回应'):
        flow.validate_assessment(flow.PatientAssessment.model_validate(patient_assessment()), draft, ['仍然腹胀'])
    payload['疑问'][0]['编号'] = 'invented'
    with pytest.raises(ValueError, match='编造'):
        flow.validate_assessment(flow.PatientAssessment.model_validate(payload), draft, statement)
    payload['疑问'][0]['编号'] = None
    with pytest.raises(ValueError, match='新增疑问必须有本次用户实际回答'):
        flow.validate_assessment(flow.PatientAssessment.model_validate(payload), session, None)


def test_plan_prompt_example_is_accepted_by_patient_contract():
    prompt = (PROJECT_ROOT / 'prompts/record/plan.txt').read_text(encoding='utf-8')
    example, _ = json.JSONDecoder().raw_decode(prompt[prompt.index('{') :])
    result = flow.PatientAssessment.model_validate(example)
    # A health-management goal does not require inventing a symptom.
    assert len(result.concerns) == 1
    assert result.concerns[0].text == '饮食如何调整'
    assert result.concerns[0].evidence == '我想知道饮食怎么调整'
    assert result.intake.complaints == []
    assert result.intake.facts
    assert result.gaps[0].dimension == 'meal_pattern' and result.gaps[0].aspect == 'habits'


@pytest.mark.asyncio
async def test_patient_projection_uses_readable_labels_without_rewriting_source_data():
    manager, module, _, _ = setup_manager()
    await manager.createSession('u', 's')
    session = cached(module)
    statement = {'content': 'HbA1c 6.1%，\n这和腹胀有关吗？'}
    session['qaFlow']['focus'] = {'topic': '腹胀', 'aspect': 'medical_history', 'depth': 2}
    session['qaFlow']['concerns'] = [
        {
            'id': 'c1',
            'text': '血糖与腹胀的关联',
            'evidence': statement['content'],
            'status': 'pending',
            'handledCount': 1,
            'response': '已有说明',
        },
    ]
    session['qaHistory'] = [{'question': {'question': '最近有何不适？'}, 'answer': statement}]
    session['healthData'] = {'smartDeviceData': {'restingHeartRate': 72}}
    before = deepcopy(session)
    context = manager._patientContext(session, statement)
    assert context['本次报告'] is None
    assert context['报告总体情况'] is None
    assert context['当前关注'] == {'主题': '腹胀', '角度': '既往病史'}
    assert context['已有疑问'] == [
        {
            '编号': 'c1',
            '问题': '血糖与腹胀的关联',
            '用户原话': statement['content'],
            '处理状态': '尚未回应',
            '已有回应': '已有说明',
        }
    ]
    assert context['尚未回应的疑问编号'] == ['c1']
    assert context['本次回答'] == statement
    assert context['历史问答'] == [{'问题': '最近有何不适？', '用户回答': statement}]
    assert '"restingHeartRate":72' in context['附带健康资料']
    assert session == before
    serialized = json.dumps(context, ensure_ascii=False)
    assert all(
        key not in serialized
        for key in ('interactionCount', 'handledCount', 'phase', 'depth', 'latestAnswer', 'currentFocus')
    )


@pytest.mark.parametrize('options', [['甲', '甲'], ['甲', ' 甲 '], ['甲', ' '], ['', '乙']])
@pytest.mark.parametrize('question_type', ['radio', 'checkbox'])
def test_empty_and_duplicate_shortcut_options_are_rejected(options, question_type):
    with pytest.raises(ValidationError, match='选项不能为空或重复'):
        flow.GeneratedQuestion(question='请选择', questionType=question_type, options=options)


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize(
    ('question_type', 'options', 'answer'),
    [
        ('radio', ['有医生诊断的疾病', '没有确诊疾病', '不清楚'], ['糖尿病']),
        ('checkbox', ['高血压', '糖尿病', '不清楚'], ['高血压', '糖尿病']),
    ],
)
async def test_choice_answers_with_localized_analysis_survive_submit_restore_and_other_precedence(
    streaming, question_type, options, answer
):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    assert current['questionType'] == 'checkbox'
    gateway.next_assessment = patient_assessment()
    gateway.next_question = {'question': '您过去诊断过哪些疾病？', 'questionType': question_type, 'options': options}
    current = await submit(manager, current, '餐后腹胀', streaming=streaming, thinking=True)
    assert current['questionType'] == 'checkbox'
    assert {'没有', '不清楚', '糖尿病', '高血压'} <= set(current['options'])
    assert not any('其他' in option or '其它' in option for option in current['options'])
    assert '有医生诊断的疾病' not in current['options']
    published_options = current['options']
    assert current['interactionCount'] == 1
    assert current['assistantMessage']
    restored = await manager.getSession('u', 's')
    assert restored['questionId'] == current['questionId']
    assert restored['questionType'] == 'checkbox'
    assert restored['options'] == published_options
    current = await submit(manager, current, answer, streaming=streaming)
    assert current['interactionCount'] == 2
    assert cached(module)['qaHistory'][-1]['answer'] == answer
    assert latest_analysis(gateway)['本次回答'] == answer
    history = await manager.getHistory('u', 's')
    assert history['history'][-1]['answer'] == answer
    assert history['history'][-1]['questionType'] == 'checkbox'
    current = await submit(manager, current, answer, other='补充：喝奶后更明显', streaming=streaming)
    assert current['interactionCount'] == 3
    assert cached(module)['qaHistory'][-1]['answer'] == '补充：喝奶后更明显'
    assert latest_analysis(gateway)['本次回答'] == '补充：喝奶后更明显'
    assert latest_analysis(gateway)['当前关注'] == {'主题': '血亲家族病史', '角度': '家族病史'}


@pytest.mark.asyncio
async def test_invalid_choices_use_one_repair_then_a_valid_deterministic_question():
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    gateway.next_assessment = patient_assessment()
    gateway.next_question = {'question': '何时腹胀？', 'questionType': 'radio', 'options': ['饭后', '饭后']}
    requests_before = len(gateway.requests)
    result = await submit(manager, current, '腹胀')
    assert len(gateway.requests) == requests_before + 3
    assert result['interactionCount'] == 1
    assert result['questionType'] in ('radio', 'checkbox') and len(result['options']) >= 2
    assert '文字或语音' in result['question']
    assert cached(module)['qaHistory'][-1]['answer'] == '腹胀'
    assert await submit(manager, current, '腹胀') == result
    assert len(gateway.requests) == requests_before + 3
