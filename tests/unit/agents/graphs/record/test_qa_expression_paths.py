"""New QA copy rules share normal, repair, fallback and replay paths offline."""

import json
from copy import deepcopy

import pytest

from modules.record.application import fallback
from modules.record.application import flow
from modules.record.application.output_repair import PresentationRepair

from .test_dynamic_record import assessment
from .test_dynamic_record import cached
from .test_dynamic_record import concern
from .test_dynamic_record import save_cache
from .test_dynamic_record import submit
from .test_record_experience_v2 import complete_intake
from .test_record_experience_v2 import experience_manager

FAMILY_ANSWER = '家族聚集倾向并不等同于明确的遗传病。生活安排可以结合家族背景作适当调整。'
IMPACT_ANSWER = '日常调整可从规律进餐和容易坚持的活动开始。体检时关注相关项目。'
OLD_ANSWER = '日常生活中的不同安排可以放在一起观察。适度活动与规律休息可以从容易坚持的方式开始。'
BAD_DEFERRAL = '近期身体不适的具体原因仅凭现有信息无法确定。有糖尿病史时，可以关注规律进餐。'
BAD_RECAP = '家族中母亲和外祖父有糖尿病，您本人也确诊糖尿病，确实会关心遗传背景。'


def expression(gateway, transform):
    original = gateway.content

    def content(request):
        result = json.loads(original(request))
        if request.profile.name == 'record.question':
            task = json.JSONDecoder().raw_decode(request.content)[0]['responseTask']
            if task['responses']:
                result['assistantMessage'].update(acknowledgement='', rationale='')
                transform(result, task)
        return json.dumps(result, ensure_ascii=False)

    gateway.content = content


def old_question(module):
    snapshot = cached(module)
    snapshot['qaFlow']['concerns'].append(
        {
            'id': 'old',
            'text': '近期身体不适的原因',
            'evidence': '近期身体不适的原因',
            'status': 'limited',
            'handledCount': 2,
            'response': OLD_ANSWER,
            'responseStyleVersion': 13,
            'readyToAnswer': False,
            'informationNeeded': '具体不适',
        }
    )
    save_cache(module, snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_two_current_questions_start_with_answers_and_restore_and_replay_unchanged(streaming):
    manager, module, gateway, _ = experience_manager()
    current = await complete_intake(manager, module, gateway, streaming=streaming)

    def replies(result, task):
        for item, text in zip(result['responses'], [FAMILY_ANSWER, IMPACT_ANSWER], strict=True):
            item['text'] = text

    expression(gateway, replies)
    questions = ['我的家族是否存在遗传病', '这对我有什么影响']
    gateway.next_assessment = assessment(
        intent='additional', gaps=[], concerns=[concern(text, ready=False) for text in questions]
    )
    answer = '；'.join(questions)
    before = deepcopy(current)
    result = await submit(manager, current, answer, streaming=streaming)
    assert result['assistantMessage'] == FAMILY_ANSWER + '\n\n' + IMPACT_ANSWER
    assert result['qaRoundCount'] == 1
    assert cached(module)['currentQuestion']['acknowledgementText'] == ''
    assert [item['handledCount'] for item in cached(module)['qaFlow']['concerns']] == [1, 1]
    calls = len(gateway.requests)
    assert (await manager.getSession('u', 's'))['assistantMessage'] == result['assistantMessage']
    await manager.getHistory('u', 's')
    assert await submit(manager, before, answer, streaming=streaming) == result
    assert len(gateway.requests) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_pre_qa_answer_directly_explains_without_spending_a_qa_round(streaming):
    manager, module, gateway, _ = experience_manager()
    current = await manager.createSession('u', 's')
    expression(gateway, lambda result, task: result['responses'][0].update(text=OLD_ANSWER))
    identity = None
    for number in range(15):
        text = '近期身体不适的原因'
        gateway.next_assessment = assessment(gaps=[], concerns=[concern(text, ready=False, identity=identity)])
        answer = text if number == 0 else current['options'][0]
        current = await submit(manager, current, answer, streaming=streaming)
        identity = cached(module)['qaFlow']['concerns'][0]['id']
    assert current['assistantMessage'] == OLD_ANSWER
    assert current['qaRoundCount'] == 0
    assert cached(module)['qaFlow']['preQaAnswered'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_unrequested_answered_concern_is_removed_but_two_current_questions_survive(streaming):
    manager, module, gateway, _ = experience_manager()
    current = await complete_intake(manager, module, gateway, streaming=streaming)
    old_question(module)
    questions = ['我的家族是否存在遗传病', '这对我有什么影响']
    gateway.next_assessment = assessment(
        intent='additional',
        gaps=[],
        concerns=[concern('近期身体不适的原因', ready=False, identity='old'), *[concern(q) for q in questions]],
    )

    def replies(result, task):
        assert all(item['concernId'] != 'old' for item in task['responses'])
        for item, text in zip(result['responses'], [FAMILY_ANSWER, IMPACT_ANSWER], strict=True):
            item['text'] = text

    expression(gateway, replies)
    result = await submit(manager, current, '；'.join(questions), streaming=streaming)
    assert FAMILY_ANSWER in result['assistantMessage'] and IMPACT_ANSWER in result['assistantMessage']
    assert OLD_ANSWER not in result['assistantMessage']
    assert '近期身体不适的原因' not in result['assistantMessage']
    assert result['qaRoundCount'] == 1
    assert cached(module)['qaFlow']['concerns'][0]['handledCount'] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_active_reasks_rewrite_old_answer_and_do_not_reuse_it_after_handling_limit(streaming):
    manager, module, gateway, _ = experience_manager()
    current = await complete_intake(manager, module, gateway, streaming=streaming)
    old_question(module)
    expected = [OLD_ANSWER]
    attempts = [0]

    def replies(result, task):
        assert len(task['responses']) == 1
        instruction = task['responses'][0]
        assert instruction['previousResponse'] == expected[-1]
        assert instruction['previousResponses'] == expected
        assert 'reuseResponse' not in instruction
        attempts[0] += 1
        result['responses'][0]['text'] = (
            expected[0]
            if attempts[0] % 2
            else f'第{len(expected)}次补充可以先从一项容易坚持的日常安排开始观察，再逐步调整。'
        )

    expression(gateway, replies)
    for number in range(1, 3):
        answer = '刚才身体不适那个问题，再简单解释一下'
        repeated = concern('近期身体不适的原因', ready=False, identity='old')
        repeated['evidence'] = answer
        gateway.next_assessment = assessment(intent='additional', gaps=[], concerns=[repeated])
        calls = len(gateway.requests)
        current = await submit(manager, current, answer, streaming=streaming)
        stored = cached(module)['qaFlow']['concerns'][0]
        assert expected[-1] not in current['assistantMessage']
        assert stored['response'] in current['assistantMessage']
        assert stored['responseStyleVersion'] == flow.RESPONSE_STYLE_VERSION
        assert stored['handledCount'] == 2 + number
        assert current['qaRoundCount'] == number
        assert len(gateway.requests) - calls == 3
        expected.append(stored['response'])


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_failed_copy_repair_keeps_good_answer_without_recap_in_partial_fallback(streaming):
    manager, module, gateway, _ = experience_manager()
    current = await complete_intake(manager, module, gateway, streaming=streaming)

    def replies(result, task):
        result['assistantMessage']['acknowledgement'] = BAD_RECAP
        result['responses'][0]['text'] = FAMILY_ANSWER
        result['responses'][1]['text'] = BAD_DEFERRAL

    expression(gateway, replies)
    gateway.next_assessment = assessment(
        intent='additional', gaps=[], concerns=[concern('家族的遗传背景'), concern('身体不适的原因')]
    )
    result = await submit(manager, current, '家族的遗传背景；身体不适的原因', streaming=streaming)
    assert result['assistantMessage'].startswith(FAMILY_ANSWER)
    assert BAD_RECAP not in result['assistantMessage'] and BAD_DEFERRAL not in result['assistantMessage']
    assert result['assistantMessage'] == FAMILY_ANSWER
    assert result['qaRoundCount'] == 1
    stored = cached(module)['qaFlow']
    assert stored['concerns'][0]['status'] == 'answered'
    assert stored['concerns'][1]['status'] == 'pending'
    assert stored['generationQuality']['mode'] == 'partial'


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_cross_response_duplicate_does_not_bypass_partial_fallback(streaming):
    manager, module, gateway, _ = experience_manager()
    current = await complete_intake(manager, module, gateway, streaming=streaming)

    def replies(result, task):
        for response in result['responses']:
            response['text'] = FAMILY_ANSWER

    expression(gateway, replies)
    gateway.next_assessment = assessment(
        intent='additional', gaps=[], concerns=[concern('遗传病的一般含义'), concern('如何安排日常生活')]
    )
    result = await submit(manager, current, '遗传病的一般含义；如何安排日常生活', streaming=streaming)
    assert result['assistantMessage'].count(FAMILY_ANSWER) == 1
    assert result['assistantMessage'] == FAMILY_ANSWER
    assert result['qaRoundCount'] == 1


@pytest.mark.asyncio
async def test_answer_status_and_expression_reference_do_not_become_patient_fact_sources():
    manager, module, gateway, _ = experience_manager()
    current = await complete_intake(manager, module, gateway)
    old_question(module)
    gateway.next_assessment = assessment(intent='additional', gaps=[], concerns=[concern('新的生活问题')])
    expression(gateway, lambda result, task: result['responses'][0].update(text=IMPACT_ANSWER))
    await submit(manager, current, '新的生活问题')
    planner = next(request for request in reversed(gateway.requests) if request.profile.name == 'record.plan')
    context = json.JSONDecoder().raw_decode(planner.content[planner.content.index('{') :])[0]
    assert context['已有疑问'][0]['处理状态'] == '已作一般说明'
    assert '已展示答复' not in context
    question = next(request for request in reversed(gateway.requests) if request.profile.name == 'record.question')
    payload = json.JSONDecoder().raw_decode(question.content)[0]
    assert payload['patient']['已展示答复'][0]['答复'] == OLD_ANSWER
    for item in cached(module)['qaFlow']['intake']['facts']:
        assert OLD_ANSWER not in json.dumps(item, ensure_ascii=False)


def test_bad_bridge_does_not_prevent_collecting_and_replacing_a_duplicate_answer():
    patient = {
        '本次回答': '家族背景和日常安排',
        '历史问答': [],
        '本次报告': None,
        '长期健康画像': None,
        '附带健康资料': None,
    }
    collector = PresentationRepair(
        {'action': 'invite_supplement', 'responses': [{'concernId': 'a'}, {'concernId': 'b'}]}, patient
    )
    first = {
        'assistantMessage': {
            'acknowledgement': BAD_RECAP,
            'rationale': '',
            'evidence': [{'source': '本次回答', 'quote': patient['本次回答']}],
        },
        'responses': [{'concernId': identity, 'text': FAMILY_ANSWER} for identity in ('a', 'b')],
        'question': None,
    }
    assert collector.ingest(json.dumps(first, ensure_ascii=False)) is None
    assert list(collector.responses) == ['a']
    first['assistantMessage']['acknowledgement'] = ''
    first['responses'] = [{'concernId': 'b', 'text': IMPACT_ANSWER}]
    repaired = collector.ingest(json.dumps(first, ensure_ascii=False))
    assert repaired is not None
    assert [item.text for item in repaired.responses] == [FAMILY_ANSWER, IMPACT_ANSWER]


@pytest.mark.asyncio
async def test_failed_reask_keeps_published_history_as_a_reference_for_later_repair():
    manager, module, gateway, _ = experience_manager()
    await complete_intake(manager, module, gateway)
    old_question(module)
    snapshot = cached(module)
    answer = '刚才身体不适的问题请再解释一下'
    repeated = dict(concern('近期身体不适的原因', ready=False, identity='old'), evidence=answer)
    parsed = flow.PatientAssessment(**assessment(intent='additional', gaps=[], concerns=[repeated]))
    draft, decision = flow.advance_flow(snapshot, parsed, answer=answer)
    result = fallback.build_fallback(
        snapshot,
        answer=answer,
        validated_draft=draft,
        decision=decision,
        partial_presentation={'responses': [flow.ConcernResponse(concernId='old', text=BAD_DEFERRAL)]},
    )
    old = result['qaFlow']['concerns'][0]
    assert old['status'] == 'pending' and old['response'] == ''
    assert old['responseHistory'] == [OLD_ANSWER]
    instruction = flow._response_instruction(old, final=True)
    assert instruction['previousResponse'] == OLD_ANSWER
    assert instruction['previousResponses'] == [OLD_ANSWER]
    patient = manager._presentationContext(result, result, answer)
    assert patient['已展示答复'][0]['答复'] == OLD_ANSWER
    collector = PresentationRepair({'action': 'invite_supplement', 'responses': [instruction]}, patient)
    copy = {
        'assistantMessage': {
            'acknowledgement': '',
            'rationale': '',
            'evidence': [{'source': '本次回答', 'quote': answer}],
        },
        'responses': [{'concernId': 'old', 'text': OLD_ANSWER}],
        'question': None,
    }
    assert collector.ingest(json.dumps(copy, ensure_ascii=False)) is None
    assert collector.responses == {}
    copy['responses'][0]['text'] = IMPACT_ANSWER
    assert collector.ingest(json.dumps(copy, ensure_ascii=False)) is not None
