"""Regression cases for patient-facing options and generated text boundaries."""

import asyncio
import json

import pytest

from agents.llm import LLMChunk
from agents.llm import LLMResult
from modules.record.application import flow
from modules.record.application.flow import GeneratedQuestion
from modules.record.application.presentation import ANSWER_HINT
from modules.record.application.presentation import SUPPLEMENT_QUESTION
from modules.record.application.presentation import question_task
from modules.record.application.presentation import with_answer_hint

from .test_dynamic_record import assessment
from .test_dynamic_record import cached
from .test_dynamic_record import concern
from .test_dynamic_record import save_cache
from .test_dynamic_record import setup_manager
from .test_dynamic_record import submit
from .test_dynamic_record import supplement
from .test_message_contract import assert_pending_answer_only

RAW_REASONING_PARTS = ('  患者\ufffd原始分析\n', '额ก几个月了\u200b\n')
RAW_REASONING = ''.join(RAW_REASONING_PARTS)
RETRY_REASONING = '重试或展示阶段的推理不应追加'


def install_raw_reasoning(gateway, *, broken_stage=None):
    attempts = {'record.plan': 0, 'record.question': 0}
    original = gateway.content

    def content(request):
        value = original(request)
        attempts[request.profile.name] += 1
        if request.profile.name == broken_stage and attempts[broken_stage] == 1:
            return '{"unfinished":'
        return value

    async def complete(request):
        value = content(request)
        reasoning = (
            RAW_REASONING
            if request.profile.name == 'record.plan' and attempts['record.plan'] == 1
            else (RETRY_REASONING)
        )
        return LLMResult(content=value, reasoning_content=reasoning)

    async def stream(request):
        first_plan = request.profile.name == 'record.plan' and attempts['record.plan'] == 0
        for part in RAW_REASONING_PARTS if first_plan else (RETRY_REASONING,):
            yield LLMChunk(reasoning_content=part)
        yield LLMChunk(content=content(request))

    gateway.complete, gateway.stream = complete, stream
    return attempts


@pytest.mark.parametrize('action', ['invite_supplement', 'clarify_supplement', 'finish'])
def test_fixed_and_finished_replies_omit_a_stale_next_question_rationale(action):
    snapshot = {'qaFlow': flow.new_flow(), 'currentQuestion': {'questionId': 'q'}}
    presentation = flow.TurnPresentation(
        assistantMessage={
            'acknowledgement': '生活中的不同安排值得结合起来了解。',
            'rationale': '接下来会询问伴随感受。',
            'evidence': [],
        },
        responses=[],
        question=None,
    )
    decision = {'action': action, 'responses': [], **question_task(action)}
    flow.apply_presentation(snapshot, decision, presentation, '')
    if action == 'finish':
        assert snapshot['currentQuestion'] is None
        message = snapshot['qaFlow']['finalMessage']
    else:
        assert snapshot['currentQuestion']['options'] == ['没有了']
        message = snapshot['currentQuestion']['assistantMessage']
    assert message == (flow.COMPLETION_MESSAGE if action == 'finish' else '生活中的不同安排值得结合起来了解。')


def test_option_instructions_are_removed_before_duplicate_validation():
    question = GeneratedQuestion(
        question='目前有哪些感受？', questionType='checkbox', options=['没有', ' 其他情况（请补充） ']
    )
    assert question.options == ['没有', '其他情况']
    with pytest.raises(ValueError, match='重复'):
        GeneratedQuestion(
            question='目前有哪些感受？', questionType='checkbox', options=['其他情况', '其他情况（请补充）']
        )
    with pytest.raises(ValueError, match='不能为空'):
        GeneratedQuestion(question='目前有哪些感受？', questionType='radio', options=['没有', '（请补充）'])


def test_free_answer_hint_is_added_once_without_changing_medical_options():
    question = GeneratedQuestion(
        question='既往有哪些诊断？', questionType='checkbox', options=['胃炎（慢性）', '其他(请说明)']
    )
    assert question.options == ['胃炎（慢性）', '其他']
    text = with_answer_hint(question.question)
    assert text.endswith(ANSWER_HINT)
    assert with_answer_hint(text) == text


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['analysis', 'presentation'])
async def test_bad_model_text_retries_once_then_saves_only_a_safe_fallback(stage):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    before = cached(module)
    original = gateway.content

    def damaged(request):
        value = original(request)
        if request.profile.name == ('record.plan' if stage == 'analysis' else 'record.question'):
            payload = json.loads(value)
            payload['summary' if stage == 'analysis' else 'assistantMessage'] = '饭后\ufffd不适'
            return json.dumps(payload, ensure_ascii=True)
        return value

    gateway.content = damaged
    count = len(gateway.requests)
    events = [
        event async for event in manager.submitAnswerStream('u', 's', current['questionId'], '偶尔恶心', thinking=True)
    ]
    assert events[-1]['data']['type'] == 'finish'
    assert [event['data']['content'] for event in events if event['data']['type'] == 'reasoning'] == [
        '需要了解用户症状与饮食的关联。'
    ]
    question = next(event['data'] for event in events if event['data']['type'] == 'question')
    assert '\ufffd' not in question['assistantMessage']
    assert len(gateway.requests) - count == (2 if stage == 'analysis' else 3)
    plan_calls = [request for request in gateway.requests[count:] if request.profile.name == 'record.plan']
    assert [request.effective_thinking for request in plan_calls] == ([True, False] if stage == 'analysis' else [True])
    updated = cached(module)
    assert updated['qaHistory'][:-1] == before['qaHistory']
    assert updated['qaHistory'][-1]['answer'] == '偶尔恶心'
    assert updated['qaFlow']['interactionCount'] == before['qaFlow']['interactionCount'] + 1
    assert updated['qaFlow']['generationQuality']['mode'] in ('partial', 'fallback')
    assert 'pendingSubmission' not in updated['qaFlow']


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_raw_reasoning_is_preserved_in_normal_and_sse_results_and_cached_snapshot(streaming):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    install_raw_reasoning(gateway)
    result = await submit(manager, current, '餐后不适', streaming=streaming, thinking=True)
    assert result['reasoning'] == RAW_REASONING
    assert cached(module)['currentQuestion']['reasoning'] == RAW_REASONING
    assert (await manager.getSession('u', 's'))['reasoning'] == RAW_REASONING
    assert result['interactionCount'] == 1
    assert RETRY_REASONING not in result['reasoning']
    await submit(manager, result, '另外补充，我暂时没有用药', streaming=streaming, thinking=False)
    assert cached(module)['qaHistory'][-1]['question']['reasoning'] == RAW_REASONING


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_twentieth_turn_saves_raw_final_reasoning_and_restores_it_from_completed_snapshot(streaming):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    snapshot = cached(module)
    snapshot['qaFlow']['interactionCount'] = 19
    snapshot['qaFlow'].pop('interviewPolicy', None)
    save_cache(module, snapshot)
    install_raw_reasoning(gateway)
    result = await submit(manager, current, '餐后有些不适', streaming=streaming, thinking=True)
    assert result['status'] == 'finished' and result['interactionCount'] == 20
    assert result['reasoning'] == RAW_REASONING
    saved = module.collection_sessions.document
    assert saved['qaFlow']['finalReasoning'] == RAW_REASONING
    assert saved['currentQuestion'] is None
    assert saved['qaHistory'][-1]['question']['reasoning'] == snapshot['currentQuestion']['reasoning']
    restored = await manager.getSession('u', 's')
    assert restored['status'] == 'finished' and restored['reasoning'] == RAW_REASONING


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('stage', ['record.plan', 'record.question'])
async def test_json_repair_preserves_first_raw_reasoning_and_never_appends_retry_reasoning(streaming, stage):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    before = cached(module)
    attempts = install_raw_reasoning(gateway, broken_stage=stage)
    start = len(gateway.requests)
    if streaming:
        events = [
            event['data']
            async for event in manager.submitAnswerStream(
                'u',
                's',
                current['questionId'],
                '餐后不适',
                thinking=True,
            )
        ]
        assert [event['content'] for event in events if event['type'] == 'reasoning'] == list(RAW_REASONING_PARTS)
        result = next(event for event in events if event['type'] == 'question')
        assert events[-1]['type'] == 'finish'
    else:
        result = await manager.submitAnswer('u', 's', current['questionId'], '餐后不适', thinking=True)
    assert result['reasoning'] == RAW_REASONING
    assert attempts == {
        'record.plan': 2 if stage == 'record.plan' else 1,
        'record.question': 2 if stage == 'record.question' else 1,
    }
    requests = gateway.requests[start:]
    assert [request.effective_thinking for request in requests if request.profile.name == 'record.plan'] == (
        [True, False] if stage == 'record.plan' else [True]
    )
    assert all(not request.effective_thinking for request in requests if request.profile.name == 'record.question')
    saved = cached(module)
    assert saved['qaFlow']['interactionCount'] == before['qaFlow']['interactionCount'] + 1
    assert len(saved['qaHistory']) == len(before['qaHistory']) + 1
    assert saved['currentQuestion']['reasoning'] == RAW_REASONING


@pytest.mark.asyncio
async def test_reasoning_chunks_arrive_before_model_json_finishes_without_publishing_a_draft():
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    before = cached(module)
    release_json = asyncio.Event()

    async def stream(request):
        yield LLMChunk(reasoning_content=RAW_REASONING_PARTS[0])
        await release_json.wait()
        yield LLMChunk(reasoning_content=RAW_REASONING_PARTS[1])
        yield LLMChunk(content=gateway.content(request))

    gateway.stream = stream
    events = manager.submitAnswerStream('u', 's', current['questionId'], '餐后不适', thinking=True)
    assert (await anext(events))['data']['type'] == 'start'
    pending = asyncio.create_task(anext(events))
    try:
        first = await asyncio.wait_for(asyncio.shield(pending), timeout=1)
        assert first['data'] == {
            'userId': 'u',
            'sessionId': 's',
            'type': 'reasoning',
            'content': RAW_REASONING_PARTS[0],
        }
        assert not release_json.is_set()
        assert_pending_answer_only(module, before, '餐后不适')
        release_json.set()
        remaining = [event['data'] async for event in events]
        assert [event['content'] for event in remaining if event['type'] == 'reasoning'] == [RAW_REASONING_PARTS[1]]
        result = next(event for event in remaining if event['type'] == 'question')
        assert result['reasoning'] == RAW_REASONING
        assert cached(module)['currentQuestion']['reasoning'] == RAW_REASONING
    finally:
        release_json.set()
        await asyncio.wait_for(pending, timeout=2)
        await events.aclose()


@pytest.mark.asyncio
async def test_garbled_options_repair_then_publish_only_the_valid_complete_result():
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    gateway.next_assessment = assessment(sufficient=False)
    calls = 0

    def question(payload):
        nonlocal calls
        calls += 1
        return {
            'question': '大约多久？',
            'questionType': 'radio',
            'options': ['额ก几个月了' if calls == 1 else '几个月', '记不清'],
        }

    gateway.next_question = question
    result = await submit(manager, current, '餐后不适')
    assert calls == 2
    assert result['questionType'] == 'checkbox'
    assert {'没有', '不清楚', '糖尿病', '高血压'} <= set(result['options'])
    assert not any('其他' in option or '其它' in option for option in result['options'])
    assert '几个月' not in result['options'] and '记不清' not in result['options']
    stored = cached(module)
    assert stored['currentQuestion']['questionId'] == result['questionId']
    assert stored['currentQuestion']['options'] == result['options']
    assert stored['qaHistory'][-1]['answer'] == '餐后不适'


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('answer', ['没有了', ['没有了'], {'content': '没有了'}])
async def test_fixed_supplement_shortcut_finishes_without_model_and_is_idempotent(answer, streaming):
    manager, module, gateway, _ = setup_manager()
    current = await supplement(manager, gateway)
    assert current['question'] == SUPPLEMENT_QUESTION
    assert current['questionType'] == 'radio' and current['options'] == ['没有了']
    gateway.invalid_analysis = gateway.invalid_response = 99
    count = len(gateway.requests)
    final = await submit(manager, current, answer, streaming=streaming, thinking=True)
    assert final['status'] == 'finished' and final['reasoning'] == ''
    assert final['interactionCount'] == 5 and len(gateway.requests) == count
    assert final['intakeQuestionCount'] == 5 and final['qaRoundCount'] == 0
    assert await submit(manager, current, answer, streaming=streaming) == final
    assert module.collection_sessions.document['qaHistory'][-1]['answer'] == answer


@pytest.mark.asyncio
async def test_supplement_shortcut_does_not_discard_custom_answer():
    manager, module, gateway, _ = setup_manager()
    current = await supplement(manager, gateway)
    gateway.next_assessment = assessment(intent='additional', concerns=[concern('腹胀')])
    result = await submit(manager, current, ['没有了'], other='但还想了解腹胀')
    assert result['status'] == 'question'
    assert cached(module)['qaHistory'][-1]['answer'] == '但还想了解腹胀'


def test_single_option_exception_is_scoped_to_supplement_and_pending_concerns_are_not_dropped():
    session = {'qaFlow': flow.new_flow(), 'currentQuestion': {'questionId': 'q'}}
    single = flow.TurnPresentation(
        assistantMessage={'acknowledgement': '先了解主要困扰。', 'rationale': '了解相关背景。', 'evidence': []},
        responses=[],
        question={'question': '还有什么情况？', 'questionType': 'radio', 'options': ['没有了']},
    )
    with pytest.raises(ValueError, match='至少两个'):
        flow.apply_presentation(session, {'action': 'open_intake', 'responses': []}, single, '')
    assert flow.finish_without_more(session, '没有了') is None
    session['qaFlow']['phase'] = 'supplement'
    session['qaFlow']['concerns'] = [{'status': 'pending'}]
    assert flow.finish_without_more(session, '没有了') is None
    session['qaFlow']['concerns'] = []
    assert flow.finish_without_more(session, '没有了，但还有腹胀') is None
