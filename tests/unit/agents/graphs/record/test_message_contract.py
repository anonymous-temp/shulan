"""Source-linked acknowledgement stays visible independently of model reasoning."""

import asyncio
import json
from copy import deepcopy

import pytest

from agents.llm import LLMChunk
from modules.record.application import flow
from modules.record.application.presentation import validate_message
from tests.unit._support.record_policy import short_interview_policy

from .test_dynamic_record import assessment
from .test_dynamic_record import cached
from .test_dynamic_record import concern
from .test_dynamic_record import save_cache
from .test_dynamic_record import setup_manager
from .test_dynamic_record import submit
from .test_dynamic_record import supplement


def assert_pending_answer_only(module, before, answer):
    """Receipt durability must not publish a successor or advance history."""
    saved = deepcopy(cached(module))
    receipt = saved['qaFlow'].pop('pendingSubmission')
    assert receipt['answer'] == answer
    assert receipt['question']['questionId'] == before['currentQuestion']['questionId']
    assert saved == before


@pytest.mark.asyncio
async def test_opening_can_acknowledge_provided_age_without_inventing_a_profile():
    manager, module, gateway, _ = setup_manager()
    original = gateway.content

    def response(request):
        value = json.loads(original(request))
        if request.profile.name == 'record.question':
            value['assistantMessage'].update(
                acknowledgement='目前了解到您38岁，我们可以从最近的消化感受开始了解。',
                evidence=[{'来源': '基本信息', '原文': '年龄38岁'}],
            )
        return json.dumps(value, ensure_ascii=False)

    gateway.content = response
    result = await manager.createSession('u', 's', basic_info={'age': 38})
    assert result['status'] == 'question' and len(gateway.requests) == 2
    assert '38岁' in result['assistantMessage']
    assert not cached(module)['qaFlow']['intake']['complaints']


def test_basic_information_cannot_be_invented_or_used_as_a_complaint_source():
    message = flow.PatientMessage(
        acknowledgement='您38岁。',
        rationale='了解最近的感受有助于梳理情况。',
        evidence=[{'source': '基本信息', 'quote': '38岁'}],
    )
    patient = {name: None for name in ('本次回答', '本次报告', '长期健康画像', '附带健康资料', '基本信息')}
    patient['历史问答'] = []
    with pytest.raises(ValueError):
        validate_message(message, patient, action='open_intake')
    with pytest.raises(ValueError):
        flow.IntakeEvidence(source='基本信息', quote='38岁')


@pytest.mark.parametrize('basic_info', [None, {}, {'age': None, 'gender': ''}])
def test_missing_basic_information_has_no_evidence_placeholder(basic_info):
    manager, _, _, _ = setup_manager()
    assert manager._formatBasicInfo(basic_info) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_supplement_uses_updated_patient_state_and_never_promises_another_question(streaming):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    legacy = cached(module)
    legacy['qaFlow']['interviewPolicy'] = short_interview_policy()
    save_cache(module, legacy)
    gateway.next_assessment = assessment(sufficient=True, gaps=[])
    for answer in ('最近想了解健康情况', '没有已知疾病', '没有已知家族史'):
        current = await submit(manager, current, answer, streaming=streaming)
    original = gateway.content
    seen = []

    def response(request):
        text = original(request)
        if request.profile.name == 'record.question':
            payload, _ = json.JSONDecoder().raw_decode(request.content[request.content.index('{') :])
            seen.append(payload)
            value = json.loads(text)
            value['assistantMessage']['rationale'] = '接下来想了解您是否有伴随的不适。'
            return json.dumps(value, ensure_ascii=False)
        return text

    gateway.content = response
    result = await submit(manager, current, '最近一两周内', streaming=streaming)
    assert result['phase'] == 'supplement'
    assert '伴随的不适' not in result['assistantMessage']
    assert result['options'] == ['没有了']
    task, patient = seen[0]['responseTask'], seen[0]['patient']
    assert task['questionMode'] == 'fixed'
    assert task['fixedQuestion']['options'] == ['没有了']
    assert patient['上一题']['问题'] == current['question']
    assert patient['本次回答'] == '最近一两周内'
    assert '当前问题' not in patient
    assert patient['已识别的困扰'][0]['编号'] == cached(module)['qaFlow']['intake']['complaints'][0]['id']
    assert patient['当前关注'] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_twentieth_turn_answers_old_and_new_concerns_before_closing_once(streaming):
    manager, module, gateway, _ = setup_manager()
    current = await supplement(manager, gateway)
    gateway.next_assessment = assessment(intent='additional', concerns=[concern('排便', ready=False)])
    current = await submit(manager, current, '想了解排便变化', streaming=streaming)
    snapshot = cached(module)
    snapshot['qaFlow']['interactionCount'] = 19
    old = snapshot['qaFlow']['concerns'][0]
    save_cache(module, snapshot)
    gateway.next_assessment = assessment(
        concerns=[
            concern(old['text'], ready=False, identity=old['id']),
            concern('饮食'),
            concern('运动', ready=False),
        ]
    )
    answer = '排便变化需要注意的请再说一下，另外饮食和运动需要注意什么？'
    final = await submit(manager, current, answer, streaming=streaming)
    assert final['status'] == 'finished' and final['interactionCount'] == 20
    assert final['endReason'] == 'interaction_limit'
    saved = module.collection_sessions.document['qaFlow']['concerns']
    assert all(item['response'] in final['assistantMessage'] for item in saved)
    assert '关于“' not in final['assistantMessage']
    assert final['assistantMessage'].endswith(flow.COMPLETION_MESSAGE)
    assert 'questionId' not in final
    calls = len(gateway.requests)
    retry = await submit(manager, current, answer, streaming=streaming)
    assert retry['assistantMessage'] == final['assistantMessage']
    assert retry['interactionCount'] == 20 and len(gateway.requests) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_legacy_intake_upgrade_is_saved_with_a_durable_fallback_reply(streaming):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    gateway.next_assessment = assessment(sufficient=False)
    current = await submit(manager, current, '餐后不适')
    old = cached(module)
    intake = old['qaFlow']['intake']
    for key in ('schemaVersion', 'focusIds', 'preferredId', 'priorityAsked'):
        intake.pop(key)
    intake['asked'].append('general:priority')
    save_cache(module, old)
    original = gateway.content

    def broken(request):
        return '{' if request.profile.name == 'record.question' else original(request)

    gateway.content = broken
    result = await submit(manager, current, '记不清', streaming=streaming)
    updated = cached(module)
    assert result['status'] == 'question'
    assert updated['qaFlow']['generationQuality']['mode'] == 'partial'
    assert updated['qaFlow']['version'] == 2
    assert updated['qaFlow']['intake']['schemaVersion'] == 3
    assert updated['qaFlow']['intake']['preferredId'] is None
    assert updated['qaFlow']['intake']['priorityAsked'] is True
    assert updated['qaHistory'][:-1] == old['qaHistory']
    assert updated['qaHistory'][-1]['question']['questionId'] == current['questionId']
    gateway.content = original
    calls = len(gateway.requests)
    assert (await submit(manager, current, '记不清', streaming=streaming))['questionId'] == result['questionId']
    assert len(gateway.requests) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['generic', 'method_explanation', 'unavailable_source'])
async def test_unconnected_explanation_is_repaired_once_then_replaced_by_safe_fallback(defect):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    gateway.next_assessment = assessment(sufficient=False)
    before = cached(module)
    original = gateway.content

    def response(request):
        text = original(request)
        if request.profile.name != 'record.question':
            return text
        value = json.loads(text)
        message = value['assistantMessage']
        if defect == 'generic':
            message.update(acknowledgement='已记录。', evidence=[])
        elif defect == 'method_explanation':
            message['acknowledgement'] = '这些信息有助于判断身体不适与哪些情况有关。'
        else:
            message.update(
                acknowledgement='既有画像记录了相关不适。', evidence=[{'source': '长期画像', 'quote': '偶尔恶心'}]
            )
        return json.dumps(value, ensure_ascii=False)

    gateway.content = response
    count = len(gateway.requests)
    result = await submit(manager, current, '偶尔有点恶心')
    assert len(gateway.requests) == count + 3
    assert result['status'] == 'question'
    assert '既有画像记录了相关不适' not in result['assistantMessage']
    updated = cached(module)
    assert updated['qaFlow']['generationQuality']['mode'] == 'partial'
    assert updated['qaHistory'][:-1] == before['qaHistory']
    assert updated['qaHistory'][-1]['answer'] == '偶尔有点恶心'
    assert 'pendingSubmission' not in updated['qaFlow']


@pytest.mark.asyncio
async def test_reasoning_deltas_match_saved_text_and_message_is_a_separate_public_string():
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    stream_calls = 0

    async def stream(request):
        nonlocal stream_calls
        stream_calls += 1
        yield LLMChunk(reasoning_content='需要结合用户的实际感受，')
        yield LLMChunk(reasoning_content='了解相关背景。')
        yield LLMChunk(content=gateway.content(request))

    gateway.stream = stream
    events = [
        event['data']
        async for event in manager.submitAnswerStream('u', 's', current['questionId'], '偶尔有点恶心', thinking=True)
    ]
    reasoning_events = [event for event in events if event['type'] == 'reasoning']
    assert [event['content'] for event in reasoning_events] == ['需要结合用户的实际感受，', '了解相关背景。']
    question = next(event for event in events if event['type'] == 'question')
    assert (
        ''.join(event['content'] for event in reasoning_events)
        == question['reasoning']
        == '需要结合用户的实际感受，了解相关背景。'
    )
    assert isinstance(question['assistantMessage'], str)
    assert '偶尔有点恶心' not in question['assistantMessage']
    assert cached(module)['qaHistory'][-1]['answer'] == '偶尔有点恶心'
    assert not any(key in question for key in ('acknowledgement', 'rationale', 'evidence', 'intake'))
    restored = await manager.getSession('u', 's')
    assert restored['assistantMessage'] == question['assistantMessage']
    assert restored['reasoning'] == question['reasoning']
    replay = [
        event['data']
        async for event in manager.submitAnswerStream('u', 's', current['questionId'], '偶尔有点恶心', thinking=True)
    ]
    assert [event['type'] for event in replay] == ['start', 'question', 'finish']
    assert replay[1] == question
    assert stream_calls == 1
    next_question = await submit(manager, question, '以前有过类似不适', thinking=False)
    assert next_question['reasoning'] == '' and next_question['assistantMessage']
    history = await manager.getHistory('u', 's')
    assert history['history'][-1]['assistantMessage'] == question['assistantMessage']
    assert cached(module)['qaHistory'][-1]['question']['reasoning'] == question['reasoning']
    assert cached(module)['qaHistory'][-1]['answer'] == '以前有过类似不适'


@pytest.mark.asyncio
async def test_reasoning_arrives_before_analysis_finishes_and_question_waits_for_save(monkeypatch):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    before = cached(module)
    analysis_release = asyncio.Event()
    analysis_finished = asyncio.Event()
    presentation_started = asyncio.Event()
    presentation_release = asyncio.Event()
    save_started = asyncio.Event()
    save_release = asyncio.Event()
    original_complete = gateway.complete
    original_save = manager._save_session

    async def stream(request):
        yield LLMChunk(reasoning_content='先了解实际感受。')
        await analysis_release.wait()
        yield LLMChunk(reasoning_content='再结合饮食背景。')
        content = gateway.content(request)
        yield LLMChunk(content=content[:10])
        yield LLMChunk(content=content[10:])
        analysis_finished.set()

    async def complete(request):
        presentation_started.set()
        await presentation_release.wait()
        return await original_complete(request)

    async def save(*args, **kwargs):
        if args[2]['qaFlow'].get('pendingSubmission'):
            return await original_save(*args, **kwargs)
        save_started.set()
        await save_release.wait()
        return await original_save(*args, **kwargs)

    gateway.stream = stream
    gateway.complete = complete
    monkeypatch.setattr(manager, '_save_session', save)
    events = manager.submitAnswerStream('u', 's', current['questionId'], '偶尔恶心', thinking=True)
    pending = None
    try:
        assert (await anext(events))['data']['type'] == 'start'
        first = await asyncio.wait_for(anext(events), timeout=2)
        assert first['data']['type'] == 'reasoning'
        assert first['data']['content'] == '先了解实际感受。'
        assert not analysis_finished.is_set()
        assert not presentation_started.is_set()
        assert_pending_answer_only(module, before, '偶尔恶心')

        analysis_release.set()
        second = await asyncio.wait_for(anext(events), timeout=2)
        assert second['data']['type'] == 'reasoning'
        assert second['data']['content'] == '再结合饮食背景。'
        pending = asyncio.create_task(anext(events))
        await asyncio.wait_for(presentation_started.wait(), timeout=2)
        assert analysis_finished.is_set()
        assert not pending.done()
        assert not save_started.is_set()

        presentation_release.set()
        await asyncio.wait_for(save_started.wait(), timeout=2)
        assert not pending.done()
        assert_pending_answer_only(module, before, '偶尔恶心')
        save_release.set()
        question = (await asyncio.wait_for(pending, timeout=2))['data']
        assert question['type'] == 'question'
        assert question['reasoning'] == first['data']['content'] + second['data']['content']
        assert cached(module)['currentQuestion']['questionId'] == question['questionId']
        assert [event['data']['type'] async for event in events] == ['finish']
    finally:
        analysis_release.set()
        presentation_release.set()
        save_release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await events.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['analysis_model', 'presentation_model', 'save_false', 'save_exception'])
async def test_stream_failure_after_reasoning_does_not_publish_or_advance(failure, monkeypatch):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    before = cached(module)
    original_complete = gateway.complete
    original_save = manager._save_session

    async def stream(request):
        yield LLMChunk(reasoning_content='原始思考过程。')
        if failure == 'analysis_model':
            raise RuntimeError('synthetic model interruption')
        yield LLMChunk(content=gateway.content(request))

    async def complete(request):
        if failure == 'presentation_model':
            raise RuntimeError('synthetic presentation interruption')
        return await original_complete(request)

    async def save(*args, **kwargs):
        if args[2]['qaFlow'].get('pendingSubmission'):
            return await original_save(*args, **kwargs)
        if failure == 'save_exception':
            raise RuntimeError('synthetic storage failure')
        return False

    gateway.stream, gateway.complete = stream, complete
    if failure.startswith('save_'):
        monkeypatch.setattr(manager, '_save_session', save)
    events = [
        event async for event in manager.submitAnswerStream('u', 's', current['questionId'], '偶尔恶心', thinking=True)
    ]
    assert [event['data']['type'] for event in events] == ['start', 'reasoning', 'error']
    assert events[1]['data']['content'] == '原始思考过程。'
    assert events[-1]['code'] == 500
    assert_pending_answer_only(module, before, '偶尔恶心')


@pytest.mark.asyncio
async def test_saved_raw_reasoning_does_not_enter_later_patient_context():
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    raw = '仅属于思考过程的标识 RAW_REASONING_9f73：未经确认。'

    async def stream(request):
        yield LLMChunk(reasoning_content=raw)
        yield LLMChunk(content=gateway.content(request))

    gateway.stream = stream
    current = await submit(manager, current, '偶尔恶心', streaming=True, thinking=True)
    assert current['reasoning'] == raw
    count = len(gateway.requests)
    await submit(manager, current, '以前有过类似不适', thinking=False)
    assert cached(module)['qaHistory'][-1]['question']['reasoning'] == raw
    following_requests = gateway.requests[count:]
    assert {request.profile.name for request in following_requests} == {'record.plan', 'record.question'}
    assert all(raw not in request.content for request in following_requests)


def test_opening_wording_does_not_require_report_claim_or_limitation_regex():
    patient = {'本次回答': None, '历史问答': [], '本次报告': None, '长期健康画像': '', '附带健康资料': ''}
    message = flow.PatientMessage(acknowledgement='先了解您的情况。', rationale='从目前关注的感受开始。', evidence=[])
    validate_message(message, patient, action='open_intake')
    patient['本次报告'] = '皮肤检查记录'
    for wording in (
        '先了解您的情况。',
        '这份皮肤检查与本次关注的消化感受关联不大，我们先聊聊您的实际体验。',
        '报告显示的是皮肤检查相关内容，我们结合您的实际感受继续了解。',
    ):
        message.acknowledgement = wording
        validate_message(message, patient, action='open_intake')
    patient['本次报告'] = '空腹血糖 6.1 mmol/L'
    message = flow.PatientMessage(
        acknowledgement='报告记录空腹血糖 6.1 mmol/L，需要结合您的实际情况了解。',
        rationale='先了解您当前最在意的感受。',
        evidence=[{'source': '本次报告', 'quote': '空腹检测的血糖结果为 6.1 mmol/L。'}],
    )
    validate_message(message, patient, action='open_intake')


@pytest.mark.asyncio
async def test_garbled_legacy_response_cannot_escape_the_safe_fallback():
    manager, module, gateway, _ = setup_manager()
    current = await supplement(manager, gateway)
    snapshot = cached(module)
    snapshot['qaFlow']['concerns'] = [
        {
            'id': 'old',
            'text': '饮食',
            'evidence': '饮食',
            'handledCount': 2,
            'status': 'answered',
            'response': '旧回复\ufffd',
            'readyToAnswer': True,
            'informationNeeded': '',
        }
    ]
    save_cache(module, snapshot)
    gateway.next_assessment = assessment(intent='additional', concerns=[concern('饮食', identity='old')])
    result = await submit(manager, current, '再说说饮食')
    assert result['status'] == 'question'
    assert '\ufffd' not in result['assistantMessage']
    updated = cached(module)
    assert updated['qaHistory'][:-1] == snapshot['qaHistory']
    assert updated['qaHistory'][-1]['answer'] == '再说说饮食'
    assert updated['qaFlow']['generationQuality']['mode'] == 'normal'
