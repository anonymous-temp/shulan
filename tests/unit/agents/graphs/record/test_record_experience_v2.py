"""New-session intake budget and direct educational QA, using synthetic data."""

import json
from copy import deepcopy

import pytest

from modules.record.application import flow
from modules.record.application import intake
from modules.record.application import interview
from modules.record.application import lifestyle
from modules.record.application import presentation
from modules.record.application.validation import RecordValidationError

from .test_dynamic_record import assessment
from .test_dynamic_record import cached
from .test_dynamic_record import concern
from .test_dynamic_record import save_cache
from .test_dynamic_record import setup_manager
from .test_dynamic_record import submit
from .test_missing_reply_recovery import recover_then_query

INVITATION = '您还有其他想了解的问题吗？可以通过文字或语音补充描述'
DIRECT_REPLY = '家族背景可以提供健康管理的参考。平时关注饮食和活动安排，体检时留意相关指标。'


def fact(dimension, answer):
    return {
        'dimension': dimension,
        'scope': 'general',
        'status': 'known',
        'summary': answer,
        'evidence': [{'source': '本次回答', 'quote': answer}],
    }


def experience_manager():
    """Avoid inventing symptoms in the legacy transaction model double."""
    manager, module, gateway, runtime = setup_manager()
    original = gateway.content

    def content(request):
        payload, _ = json.JSONDecoder().raw_decode(request.content[request.content.index('{') :])
        result = json.loads(original(request))
        if request.profile.name == 'record.plan':
            result['intake']['complaints'] = []
            if not payload['当前关注']:
                result['intake']['facts'] = []
            else:
                for item in result['intake']['facts']:
                    item.update(scope='general', complaintIndexes=[])
        else:
            for index, item in enumerate(result['responses'], 1):
                item['text'] = DIRECT_REPLY if index == 1 else '日常调整可从容易坚持的小安排开始。' + str(index)
        return json.dumps(result, ensure_ascii=False)

    gateway.content = content
    return manager, module, gateway, runtime


async def complete_intake(manager, module, gateway, *, streaming=False):
    current = await manager.createSession('u', 's')
    seen = []
    for number in range(1, 16):
        before = cached(module)
        dimension = before['currentQuestion']['intakeDimension']
        seen.append(dimension)
        answer = before['currentQuestion']['options'][0]
        gateway.next_assessment = assessment(sufficient=True, gaps=[])
        current = await submit(manager, current, answer, streaming=streaming)
        assert current['intakeQuestionCount'] == number
        assert current['assistantMessage']
        if number < 15:
            assert current['phase'] == 'intake'
        else:
            assert current['phase'] == 'supplement'
    assert seen[:3] == ['goal', 'medical_history', 'family_history']
    assert len(set(seen[3:])) == 12
    assert set(seen[3:]) <= intake.LIFESTYLE_DIMENSIONS
    assert current['question'] == INVITATION
    assert current['options'] == ['没有了']
    return current


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_fresh_session_uses_fifteen_distinct_topics_and_restores_without_new_calls(streaming):
    manager, module, gateway, _ = experience_manager()
    current = await complete_intake(manager, module, gateway, streaming=streaming)
    stored = deepcopy(cached(module))
    assert stored['qaFlow']['interviewPolicy']['version'] == 2
    calls = len(gateway.requests)
    restored = await manager.getSession('u', 's')
    history = await manager.getHistory('u', 's')
    assert restored['questionId'] == current['questionId']
    assert restored['question'] == INVITATION
    assert len(history['history']) == 15
    assert len(gateway.requests) == calls
    assert cached(module) == stored
    assert 'interviewPolicy' not in restored


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_fifteen_intake_answers_allow_all_five_qa_replies_with_interleaved_lifestyle_updates(streaming):
    manager, module, gateway, _ = experience_manager()
    current = await complete_intake(manager, module, gateway, streaming=streaming)
    for number in range(1, 6):
        gateway.next_assessment = assessment(substantive=True, gaps=[], concerns=[])
        current = await submit(manager, current, '平时还有一项生活安排想补充', streaming=streaming)
        assert current['qaRoundCount'] == number - 1
        assert current['status'] == 'question'
        text = f'请解释第{number}项家族背景的一般影响'
        gateway.next_assessment = assessment(intent='additional', gaps=[], concerns=[concern(text, ready=False)])
        answered = deepcopy(current)
        current = await submit(manager, current, text, streaming=streaming)
        assert current['qaRoundCount'] == number
        assert DIRECT_REPLY in current['assistantMessage']
        assert '关于“' not in current['assistantMessage']
        assert text not in current['assistantMessage']
        if number < 5:
            assert current['question'] == INVITATION
            assert current['status'] == 'question'
        else:
            assert current['status'] == 'finished'
            assert current['endReason'] == 'qa_round_limit'
            assert current['interactionCount'] == 25
            calls = len(gateway.requests)
            replay = await submit(manager, answered, text, streaming=streaming)
            assert replay == current
            assert len(gateway.requests) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_early_answer_can_cover_all_lifestyle_details_without_repeated_supplement_questions(streaming):
    manager, module, gateway, _ = experience_manager()
    current = await manager.createSession('u', 's')
    answers = {
        dimension: lifestyle.question_fields(dimension)['options'][0] for dimension in intake.LIFESTYLE_DIMENSIONS
    }
    answer = '；'.join(f'{intake.DIMENSION_LABELS[dimension]}：{value}' for dimension, value in answers.items())
    gateway.next_assessment = assessment(
        gaps=[], intake={'complaints': [], 'facts': [fact(dimension, answer) for dimension in answers]}
    )
    current = await submit(manager, current, answer, streaming=streaming)
    assert cached(module)['currentQuestion']['intakeDimension'] == 'medical_history'
    for expected in ('family_history', None):
        gateway.next_assessment = assessment(gaps=[])
        current = await submit(manager, current, '没有需要补充的内容', streaming=streaming)
        assert cached(module)['currentQuestion']['intakeDimension'] == expected
    assert current['phase'] == 'supplement'
    assert current['intakeQuestionCount'] == 3
    assert current['question'] == INVITATION
    assert len(cached(module)['qaHistory']) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_incomplete_personal_details_do_not_create_a_qa_clarification_question(streaming):
    manager, module, gateway, _ = experience_manager()
    current = await complete_intake(manager, module, gateway, streaming=streaming)
    gateway.next_assessment = assessment(
        intent='additional', gaps=[], concerns=[concern('家族有糖尿病对我有什么影响', ready=False)]
    )
    current = await submit(manager, current, '家族有糖尿病对我有什么影响', streaming=streaming)
    assert current['question'] == INVITATION
    assert current['options'] == ['没有了']
    assert current['qaRoundCount'] == 1
    assert DIRECT_REPLY in current['assistantMessage']
    stored = cached(module)
    assert stored['currentQuestion']['concernId'] is None
    assert stored['qaFlow']['concerns'][0]['status'] == 'limited'
    assert stored['qaFlow']['concerns'][0]['handledCount'] == 1


@pytest.mark.asyncio
async def test_many_questions_receive_independent_answers_with_no_user_question_prefixes():
    manager, module, gateway, _ = experience_manager()
    current = await complete_intake(manager, module, gateway)
    questions = ['家族背景的一般影响', '运动如何开始', '饮食如何安排']
    gateway.next_assessment = assessment(
        intent='additional', gaps=[], concerns=[concern(text, ready=False) for text in questions]
    )
    result = await submit(manager, current, '；'.join(questions))
    items = cached(module)['qaFlow']['concerns']
    assert len(items) == 3
    assert all(item['response'] in result['assistantMessage'] for item in items)
    assert all(item['handledCount'] == 1 for item in items)
    assert '关于“' not in result['assistantMessage']
    assert all(text not in result['assistantMessage'] for text in questions)
    assert result['qaRoundCount'] == 1


@pytest.mark.parametrize('clarify', [False, True])
def test_invitation_hint_is_not_appended_twice(clarify):
    question = presentation.supplement_question(clarify=clarify)['question']
    if not clarify:
        assert question == INVITATION
    assert presentation.with_answer_hint(question) == question
    assert question.count('文字或语音') == 1


@pytest.mark.parametrize(
    'text',
    [
        '您这次选择了身体不适或症状。',
        '您提到没有确诊疾病，已经记录。',
        '您想要了解如何改善饮食。',
        '您平时运动多少次？',
    ],
)
def test_new_acknowledgements_do_not_echo_user_actions_or_hide_questions(text):
    message = flow.PatientMessage(acknowledgement=text, rationale='日常生活的不同安排值得结合起来了解。', evidence=[])
    with pytest.raises(RecordValidationError):
        presentation.validate_acknowledgement_style(message, action='ask_intake')


@pytest.mark.parametrize(
    'text',
    [
        '接着聊聊家里人的健康情况。',
        '再聊聊您平时的活动安排。',
    ],
)
def test_neutral_human_acknowledgements_remain_valid_without_report_or_disease(text):
    message = flow.PatientMessage(acknowledgement=text, evidence=[])
    presentation.validate_acknowledgement_style(message, action='ask_intake')


@pytest.mark.parametrize(
    'text',
    ['请补充是哪位血亲、发病年龄，再判断风险。', '您最近出现这些症状多久了？'],
)
def test_qa_does_not_require_the_user_to_complete_personal_information(text):
    with pytest.raises(RecordValidationError):
        presentation.validate_response_style(text)


@pytest.mark.parametrize(
    'text',
    [DIRECT_REPLY, '这类信息可以提供一般参考，不能据此直接确诊。平时可以关注生活安排及定期检查。'],
)
def test_qa_can_explain_general_information_and_appropriate_limits(text):
    presentation.validate_response_style(text)


@pytest.mark.asyncio
async def test_presentation_context_has_published_copy_for_deduplication_but_not_patient_evidence():
    manager, module, gateway, _ = experience_manager()
    current = await manager.createSession('u', 's')
    previous_message = current['assistantMessage']
    gateway.next_assessment = assessment(gaps=[])
    await submit(manager, current, '想了解日常生活')
    request = next(request for request in reversed(gateway.requests) if request.profile.name == 'record.question')
    payload, _ = json.JSONDecoder().raw_decode(request.content)
    assert previous_message in json.dumps(payload['patient']['已展示说明'], ensure_ascii=False)
    plan_request = next(request for request in reversed(gateway.requests) if request.profile.name == 'record.plan')
    plan_payload, _ = json.JSONDecoder().raw_decode(plan_request.content[plan_request.content.index('{') :])
    assert '已展示说明' not in plan_payload
    assert cached(module)['qaHistory'][0]['answer'] == '想了解日常生活'


def test_old_response_without_current_style_version_is_not_reused_in_new_turn():
    prior = {
        'id': 'prior',
        'text': '家族背景的影响',
        'readyToAnswer': False,
        'informationNeeded': '需要亲属和年龄',
        'handledCount': 2,
        'status': 'limited',
        'response': '信息不完整，目前无法回答。',
    }
    instruction = flow._response_instruction(prior, final=True)
    assert 'reuseResponse' not in instruction
    prior.update(response=DIRECT_REPLY, responseStyleVersion=flow.RESPONSE_STYLE_VERSION)
    instruction = flow._response_instruction(prior, final=True)
    assert 'reuseResponse' not in instruction
    assert instruction['previousResponse'] == DIRECT_REPLY


def test_old_clarification_question_is_restored_but_its_answer_no_longer_reopens_a_clarification_chain():
    state = {'qaFlow': flow.new_flow(), 'qaHistory': [], 'currentQuestion': {'questionId': 'old', 'concernId': 'c'}}
    state['qaFlow'].update(phase='qa', intakeQuestionCount=15, intakeCount=15, interactionCount=15)
    state['qaFlow']['concerns'] = [
        {
            'id': 'c',
            'text': '家族背景的影响',
            'evidence': '家族背景的影响',
            'status': 'pending',
            'handledCount': 1,
            'response': '此前的初步答复。',
            'readyToAnswer': False,
            'informationNeeded': '具体亲属',
        }
    ]
    parsed = flow.PatientAssessment(
        **assessment(concerns=[concern('家族背景的影响', ready=False, identity='c')], gaps=[])
    )
    _, decision = flow.advance_flow(state, parsed, answer='具体亲属目前记不清')
    assert decision['action'] == 'invite_supplement'
    assert decision['responses'][0]['resolution'] == 'limited'
    assert decision.get('target') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_failed_acknowledgement_repair_keeps_valid_question_and_uses_neutral_bridge(streaming):
    manager, module, gateway, _ = experience_manager()
    current = await manager.createSession('u', 's')
    original = gateway.content
    bad = '您这次选择了身体不适或症状。'

    def damaged(request):
        result = json.loads(original(request))
        if request.profile.name == 'record.question':
            result['assistantMessage']['acknowledgement'] = bad
        return json.dumps(result, ensure_ascii=False)

    gateway.content = damaged
    gateway.next_assessment = assessment(gaps=[])
    before = len(gateway.requests)
    result = await submit(manager, current, '想了解生活方式', streaming=streaming)
    assert result['status'] == 'question'
    assert bad not in result['assistantMessage']
    assert result['assistantMessage']
    assert cached(module)['currentQuestion']['intakeDimension'] == 'medical_history'
    assert cached(module)['qaFlow']['generationQuality']['mode'] == 'partial'
    assert len(gateway.requests) - before == 3
    assert cached(module)['qaHistory'][0]['answer'] == '想了解生活方式'


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_partial_qa_fallback_preserves_valid_answer_but_never_asks_for_missing_personal_details(streaming):
    manager, module, gateway, _ = experience_manager()
    current = await complete_intake(manager, module, gateway, streaming=streaming)
    original = gateway.content
    bad = '请补充是哪位血亲、发病年龄，再判断风险。'

    def damaged(request):
        result = json.loads(original(request))
        if request.profile.name == 'record.question':
            result['responses'][-1]['text'] = bad
        return json.dumps(result, ensure_ascii=False)

    gateway.content = damaged
    gateway.next_assessment = assessment(
        intent='additional', gaps=[], concerns=[concern('日常生活怎么调整'), concern('家族背景的影响', ready=False)]
    )
    calls = len(gateway.requests)
    result = await submit(manager, current, '日常生活怎么调整；家族背景的影响', streaming=streaming)
    stored = cached(module)
    assert result['question'] == INVITATION
    assert DIRECT_REPLY in result['assistantMessage']
    assert bad not in result['assistantMessage']
    assert result['assistantMessage'] == DIRECT_REPLY
    assert stored['qaFlow']['concerns'][0]['status'] == 'answered'
    assert stored['qaFlow']['concerns'][1]['status'] == 'pending'
    assert stored['qaFlow']['qaRoundCount'] == 1
    assert len(gateway.requests) - calls == 3
    assert '关于“' not in result['assistantMessage']


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['getSession', 'getHistory'])
async def test_restored_new_intake_question_keeps_private_dimension_and_published_coverage(method):
    manager, module, gateway, _ = experience_manager()
    current = await manager.createSession('u', 's')
    for _ in range(4):
        gateway.next_assessment = assessment(gaps=[])
        current = await submit(manager, current, current['options'][0])
    before = cached(module)
    original_question = deepcopy(before['currentQuestion'])
    selection = interview.selection_context(before)
    before['currentQuestion'] = None
    save_cache(module, before)
    calls = len(gateway.requests)
    await recover_then_query(manager, method)
    restored = cached(module)
    assert len(gateway.requests) == calls
    assert restored['currentQuestion']['questionId'] == original_question['questionId']
    assert restored['currentQuestion']['question'] == original_question['question']
    assert restored['currentQuestion']['options'] == original_question['options']
    assert restored['currentQuestion']['intakeDimension'] == original_question['intakeDimension']
    assert restored['currentQuestion']['intakeQuestionNumber'] == original_question['intakeQuestionNumber'] == 5
    assert interview.selection_context(restored) == selection
    assert restored['qaHistory'] == before['qaHistory']
    assert restored['answers'] == before['answers']
