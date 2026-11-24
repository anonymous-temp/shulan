"""Dynamic Record policy and full AgentHub/service transactions, without network."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.llm import LLMChunk
from agents.llm import LLMResult
from health_profile.record_ingestion import _sanitized_answers
from modules.chat.application.service import ChatSessionManager
from modules.record.application import flow
from modules.record.application import interview
from modules.record.application.intake import DIMENSION_LABELS
from tests.unit._support.record_policy import short_interview_policy

from .test_record_agent import FakeHealthProfileMemoryProvider
from .test_record_agent import FakeHealthReportProcessingService
from .test_record_agent import FakeRecordProfileSource
from .test_record_agent import _build_runtime
from .test_record_agent import _load_record_service


def assessment(*, intent='answer', substantive=True, sufficient=True, concerns=(), gaps=None, intake=None):
    return {
        'intent': intent,
        'substantive': substantive,
        'intakeSufficient': sufficient,
        'summary': '患者描述餐后腹胀，需要结合出现时间及饮食背景理解。',
        'intake': intake or {'complaints': [], 'priorityClarificationNeeded': False, 'facts': []},
        'concerns': list(concerns),
        'gaps': gaps
        if gaps is not None
        else [
            {
                'topic': '主诉',
                'aspect': 'symptoms',
                'dimension': 'chief_complaint',
                'scope': 'general',
                'reason': '了解主要困扰',
                'informationNeeded': '主要困扰',
            },
            {
                'topic': '腹胀',
                'aspect': 'symptoms',
                'dimension': 'character',
                'reason': '理解具体不适',
                'informationNeeded': '不适的具体表现',
            },
            {
                'topic': '腹胀',
                'aspect': 'habits',
                'dimension': 'habits',
                'reason': '了解相关饮食背景',
                'informationNeeded': '与饮食的关联',
            },
            {
                'topic': '饮食结构',
                'aspect': 'habits',
                'dimension': 'diet_structure',
                'scope': 'general',
                'reason': '了解与本次目标相关的生活背景',
                'informationNeeded': '日常饮食结构',
            },
            {
                'topic': '日常活动',
                'aspect': 'habits',
                'dimension': 'activity',
                'scope': 'general',
                'reason': '了解与本次目标相关的生活背景',
                'informationNeeded': '平时的活动情况',
            },
            {
                'topic': '睡眠作息',
                'aspect': 'habits',
                'dimension': 'sleep',
                'scope': 'general',
                'reason': '了解与本次目标相关的生活背景',
                'informationNeeded': '平时的睡眠作息',
            },
            {
                'topic': '腹胀',
                'aspect': 'medical_history',
                'dimension': 'medical_history',
                'reason': '了解是否有相关病史',
                'informationNeeded': '相关病史',
            },
            {
                'topic': '腹胀',
                'aspect': 'family_history',
                'dimension': 'family_history',
                'reason': '了解相关家族情况',
                'informationNeeded': '相关家族史',
            },
        ],
    }


def concern(text, *, ready=True, identity=None):
    return {
        'id': identity,
        'text': text,
        'evidence': text,
        'readyToAnswer': ready,
        'informationNeeded': '' if ready else '症状出现的时间',
    }


def opening_question(question_type='checkbox'):
    return {
        'question': '这次您最希望进一步了解哪方面？可多选。'
        if question_type == 'checkbox'
        else '目前最希望先了解哪一方面的身体情况？',
        'questionType': question_type,
        'options': list(interview.DEFAULT_GOAL_OPTIONS) + list(interview.GOAL_STATUS_OPTIONS),
    }


class PatientGateway:
    def __init__(self):
        self.requests = []
        self.next_assessment = None
        self.next_question = None
        self.invalid_analysis = 0
        self.invalid_response = 0
        self.delay = 0

    def content(self, request):
        self.requests.append(request)
        payload, _ = json.JSONDecoder().raw_decode(request.content[request.content.index('{') :])
        if request.profile.name == 'record.plan':
            if self.invalid_analysis:
                self.invalid_analysis -= 1
                return '{}'
            result = self.next_assessment
            if callable(result):
                result = result(payload)
            result = deepcopy(result or assessment(substantive=payload['本次回答'] is not None))
            if payload['本次回答'] is not None:
                fragment = next(flow._answer_fragments(payload['本次回答']))
                primary = next((c for c in payload['已识别的困扰'] if c['主要困扰']), None)
                if not result['intake']['complaints']:
                    result['intake']['complaints'] = [
                        {
                            'id': primary['编号'] if primary else None,
                            'text': '餐后不适',
                            'primary': True,
                            'evidence': [{'source': '本次回答', 'quote': fragment}],
                        }
                    ]
                if payload['已问过的内容'] and not result['intake']['facts']:
                    label = payload['已问过的内容'][-1]
                    dimension = next(key for key, value in DIMENSION_LABELS.items() if value == label)
                    result['intake']['facts'] = [
                        {
                            'dimension': dimension,
                            'scope': 'primary',
                            'status': 'known',
                            'summary': fragment,
                            'evidence': [{'source': '本次回答', 'quote': fragment}],
                        }
                    ]
            return json.dumps(result, ensure_ascii=False)
        if self.invalid_response:
            self.invalid_response -= 1
            return '{}'
        task = payload['responseTask']
        patient = payload['patient']
        evidence = []
        rationale = '结合这些情况了解相关背景，有助于理解您当前的困扰。'
        message = '日常习惯能够提供报告之外的生活背景。'
        if patient['本次回答'] is not None:
            fragment = next(flow._answer_fragments(patient['本次回答']))
            message = '生活中的不同安排值得结合起来了解。'
            evidence = [{'source': '本次回答', 'quote': fragment}]
        if task['action'] == 'open_intake':
            if patient['本次报告']:
                message = '根据本次报告：' + patient['本次报告']
                evidence = [{'source': '本次报告', 'quote': patient['本次报告'][:100]}]
            elif patient['长期健康画像']:
                message = '结合您的既有健康资料，先了解当前困扰。'
            else:
                message = '先了解您目前最想关注的健康问题。'
        if task['action'] == 'finish':
            message = '本次建档已结束。'
            rationale = ''
            evidence = []
        question = self.next_question(payload) if callable(self.next_question) else self.next_question
        if question is None and task['action'] == 'open_intake':
            question = opening_question()
        if question is None and task.get('target') and task['action'] in ('ask_intake', 'invite_background'):
            target = task['target']
            dimension = target.get('dimension')
            topic = {
                'goal': ('这次您最想了解或改善哪些情况？', ['了解检查结果', '改善日常健康']),
                'medical_history': ('您过去诊断过哪些疾病，目前是否仍在治疗？', ['曾诊断过疾病', '没有已知的疾病']),
                'family_history': ('直系亲属中有哪些您了解的疾病情况？', ['家人有相关疾病', '没有已知的家族病史']),
                'diet_structure': ('平时的饮食结构更接近哪一种？', ['蔬菜和蛋白质较均衡', '外卖或精制主食较多']),
                'activity': ('平时一周的活动情况如何？', ['多数天会活动', '久坐较多']),
                'sleep': ('最近的睡眠作息更接近哪一种？', ['作息相对规律', '经常晚睡或睡眠不足']),
            }.get(
                dimension,
                (f'平时的{DIMENSION_LABELS.get(dimension, "生活安排")}更接近哪种情况？', ['大多规律', '经常不固定']),
            )
            if topic:
                text, options = topic
                if target.get('supplementation'):
                    text = '您已经提到相关情况，这方面还有哪些补充或近期变化？'
                    options = ['没有需要补充的内容', '目前记不清']
                question = {'question': text, 'questionType': 'radio', 'options': options}
                if target.get('choiceConstraints'):
                    question.update(target['choiceConstraints'])
        return json.dumps(
            {
                'assistantMessage': {'acknowledgement': message, 'rationale': rationale, 'evidence': evidence},
                'responses': [
                    {
                        'concernId': item['concernId'],
                        'text': '资料范围内的回应：'
                        + item['resolution']
                        + f'；模拟答复{len(self.requests)}：{item["concernId"]}。',
                    }
                    for item in task['responses']
                ],
                'question': None
                if task['action'] == 'finish'
                else question
                or {
                    'question': '还有哪些疑问？' if task['action'] == 'invite_supplement' else '请描述具体情况。',
                    'questionType': 'radio',
                    'options': ['近期有变化', '一直如此', '暂时不清楚'],
                },
            },
            ensure_ascii=False,
        )

    async def complete(self, request):
        await asyncio.sleep(self.delay)
        return LLMResult(content=self.content(request), reasoning_content='需要了解用户症状与饮食的关联。')

    async def stream(self, request):
        await asyncio.sleep(self.delay)
        yield LLMChunk(reasoning_content='需要了解用户症状与饮食的关联。')
        yield LLMChunk(content=self.content(request))


def setup_manager(**kwargs):
    gateway = PatientGateway()
    runtime = _build_runtime(gateway)
    module = _load_record_service()
    manager = module.QASessionManager(runtime, **kwargs)
    return manager, module, gateway, runtime


def cached(module):
    return json.loads(module.redis_client.value)


def save_cache(module, session):
    module.redis_client.value = json.dumps(session, ensure_ascii=False)


async def submit(manager, current, answer, *, streaming=False, thinking=False, other=None):
    if not streaming:
        return await manager.submitAnswer('u', 's', current['questionId'], answer, other, thinking=thinking)
    events = [
        event
        async for event in manager.submitAnswerStream(
            'u',
            's',
            current['questionId'],
            answer,
            other,
            thinking=thinking,
        )
    ]
    assert events[0]['data']['type'] == 'start'
    assert events[-1]['data']['type'] == 'finish', events
    if not thinking:
        assert not any(event['data']['type'] == 'reasoning' for event in events)
    return next(event['data'] for event in events if event['data'].get('status') in ('question', 'finished'))


async def supplement(manager, gateway, *, streaming=False, length=5, legacy=True):
    current = await manager.createSession('u', 's')
    if legacy:
        # These transaction fixtures deliberately exercise already-created
        # sessions from the former 4-6 question policy. Fresh-policy coverage
        # lives in test_record_experience_v2.py.
        previous = await manager._load_session('u', 's')
        previous['qaFlow']['interviewPolicy'] = short_interview_policy()
        await manager._save_session_to_cache('u', 's', previous)
    for count in range(1, length + 1):
        gateway.next_assessment = assessment(
            sufficient=count >= length, gaps=[] if length == 4 and count == length else None
        )
        if length == 6:
            for gap in gateway.next_assessment['gaps']:
                if gap.get('dimension') in {'diet_structure', 'activity', 'sleep'}:
                    gap['essential'] = True
        current = await submit(manager, current, '餐后有腹胀', streaming=streaming)
        assert current['interactionCount'] == count
        assert current['phase'] == ('supplement' if count == length else 'intake')
        assert current['assistantMessage']
    return current


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('length', [4, 5, 6])
async def test_dynamic_intake_and_non_substantive_finish(length, streaming):
    source = FakeRecordProfileSource()
    manager, module, gateway, _ = setup_manager(record_profile_source=source)
    current = await supplement(manager, gateway, length=length, streaming=streaming)
    gateway.next_assessment = assessment(intent='no_more', substantive=False)
    final = await submit(manager, current, '没有了', streaming=streaming)
    assert final['status'] == 'finished'
    assert final['interactionCount'] == length
    assert final['progress'] == '100%'
    assert final['endReason'] == 'no_more_questions'
    assert module.redis_client.value is None
    assert source.queued == [('u', 's')]
    restored = await manager.getSession('u', 's')
    assert restored['assistantMessage'] == final['assistantMessage']
    history = await manager.getHistory('u', 's')
    assert len(history['history']) == length + 1
    assert history['assistantMessage'] == final['assistantMessage']
    assert all(item['assistantMessage'] for item in history['history'])
    retried = await submit(manager, current, '没有了', streaming=streaming)
    assert retried['assistantMessage'] == final['assistantMessage']
    assert source.queued == [('u', 's')]


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_multi_concerns_one_interaction_and_two_handlings(streaming):
    manager, module, gateway, _ = setup_manager()
    current = await supplement(manager, gateway, streaming=streaming)
    gateway.next_assessment = assessment(
        intent='additional',
        concerns=[
            concern('饮食'),
            concern('排便', ready=False),
            concern('运动'),
        ],
    )
    current = await submit(manager, current, '饮食、排便和运动', streaming=streaming)
    assert current['interactionCount'] == 6
    assert current['phase'] == 'qa'
    items = cached(module)['qaFlow']['concerns']
    assert [item['handledCount'] for item in items] == [1, 1, 1]
    assert all(item['response'] in current['assistantMessage'] for item in items)
    pending = items[1]
    repeated = concern('排便', ready=False, identity=pending['id'])
    answer = '排便还不确定，想再了解一下，另外想了解家族史'
    repeated['evidence'] = answer
    gateway.next_assessment = assessment(
        concerns=[
            repeated,
            concern('家族史'),
        ]
    )
    current = await submit(manager, current, answer, streaming=streaming)
    assert current['interactionCount'] == 7
    assert current['phase'] == 'qa'
    item = cached(module)['qaFlow']['concerns'][1]
    assert item['status'] == 'limited'
    assert item['handledCount'] == 2
    assert cached(module)['qaFlow']['concerns'][-1]['response'] in current['assistantMessage']


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_multiple_direct_answers_count_once_and_mixed_no_more(streaming):
    manager, module, gateway, _ = setup_manager()
    current = await supplement(manager, gateway)
    texts = ['饮食', '运动', '排便']
    gateway.next_assessment = assessment(intent='additional', concerns=[concern(text) for text in texts])
    current = await submit(manager, current, '没有其他了，但饮食运动排便还想了解', streaming=streaming)
    assert current['interactionCount'] == 6
    assert current['phase'] == 'qa'
    assert all(item['response'] in current['assistantMessage'] for item in cached(module)['qaFlow']['concerns'])
    known = cached(module)['qaFlow']['concerns'][0]
    gateway.next_assessment = assessment(intent='additional', concerns=[concern('饮食', identity=known['id'])])
    current = await submit(manager, current, '饮食仍不理解', streaming=streaming)
    assert current['interactionCount'] == 7
    assert len(cached(module)['qaFlow']['concerns']) == 3
    saved_answer = cached(module)['qaFlow']['concerns'][0]['response']
    current = await submit(manager, current, '饮食再说一下', streaming=streaming)
    repeated = cached(module)['qaFlow']['concerns'][0]
    assert repeated['handledCount'] == 3
    assert repeated['response'] != saved_answer
    assert saved_answer not in current['assistantMessage']
    assert repeated['response'] in current['assistantMessage']


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_nineteen_twenty_boundary_covers_all_pending(streaming):
    manager, module, gateway, _ = setup_manager()
    current = await supplement(manager, gateway)
    session = cached(module)
    session['qaFlow']['interactionCount'] = 18
    save_cache(module, session)
    gateway.next_assessment = assessment(
        intent='additional', concerns=[concern(f'问题{i}', ready=False) for i in range(7)]
    )
    current = await submit(manager, current, '问题0 问题1 问题2 问题3 问题4 问题5 问题6', streaming=streaming)
    assert current['interactionCount'] == 19
    assert current['phase'] == 'qa'
    items = cached(module)['qaFlow']['concerns']
    answer = '问题0到问题6请再简单说明一次'
    repeated = [dict(concern(c['text'], ready=False, identity=c['id']), evidence=answer) for c in items]
    gateway.next_assessment = assessment(concerns=repeated)
    final = await submit(manager, current, answer, streaming=streaming)
    assert final['interactionCount'] == 20
    assert final['status'] == 'finished'
    assert final['endReason'] == 'interaction_limit'
    assert 'questionId' not in final
    assert all(
        item['response'] in final['assistantMessage']
        for item in module.collection_sessions.document['qaFlow']['concerns']
    )
    assert all(c['status'] == 'limited' for c in module.collection_sessions.document['qaFlow']['concerns'])


@pytest.mark.asyncio
async def test_nineteen_with_no_pending_allows_the_last_supplement():
    manager, module, gateway, _ = setup_manager()
    current = await supplement(manager, gateway)
    session = cached(module)
    session['qaFlow']['interactionCount'] = 18
    save_cache(module, session)
    gateway.next_assessment = assessment(intent='additional', concerns=[concern('饮食')])
    current = await submit(manager, current, '饮食')
    assert current['interactionCount'] == 19 and current['status'] == 'question'
    assert current['phase'] == 'qa'


@pytest.mark.asyncio
async def test_vague_supplement_gets_one_clarification_then_finishes():
    manager, _, gateway, _ = setup_manager()
    current = await supplement(manager, gateway)
    gateway.next_assessment = assessment(intent='unclear', substantive=False)
    current = await submit(manager, current, '有')
    assert current['phase'] == 'supplement' and current['interactionCount'] == 5
    current = await submit(manager, current, '嗯')
    assert current['status'] == 'finished' and current['interactionCount'] == 5


@pytest.mark.asyncio
async def test_early_stop_answers_included_concerns():
    manager, _, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    gateway.next_assessment = assessment(intent='stop', concerns=[concern('腹胀', ready=False)])
    final = await submit(manager, current, '请结束，但腹胀如何处理')
    assert final['status'] == 'finished' and final['endReason'] == 'user_stop'
    assert '资料范围内的回应：limited' in final['assistantMessage']
    assert final['assistantMessage'].endswith(flow.COMPLETION_MESSAGE)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['analysis', 'response', 'redis'])
async def test_invalid_output_is_saved_conservatively_but_storage_failure_is_not_success(failure):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    before = cached(module)
    if failure == 'redis':
        manager._session_cache.save = AsyncMock(return_value=False)
        with pytest.raises(RuntimeError):
            await submit(manager, current, '餐后腹胀')
        assert cached(module) == before
    else:
        setattr(gateway, 'invalid_' + failure, 2)
        result = await submit(manager, current, '餐后腹胀')
        saved = cached(module)
        assert result['status'] == 'question' and result['interactionCount'] == 1
        assert saved['qaHistory'][0]['answer'] == '餐后腹胀'
        assert saved['qaFlow']['generationQuality']['mode'] in ('fallback', 'partial')
        calls = len(gateway.requests)
        assert await submit(manager, current, '餐后腹胀') == result
        assert len(gateway.requests) == calls
    assert module.collection_sessions.document is None


@pytest.mark.asyncio
async def test_one_repair_and_other_precedence_and_duplicate_conflict():
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    gateway.invalid_response = 1
    next_question = await submit(manager, current, '被忽略', other='实际用户回答')
    assert cached(module)['qaHistory'][0]['answer'] == '实际用户回答'
    request_count = len(gateway.requests)
    assert await submit(manager, current, '被忽略', other='实际用户回答') == next_question
    assert len(gateway.requests) == request_count
    with pytest.raises(module.QASessionConflictError):
        await submit(manager, current, '另一个回答')
    await submit(manager, next_question, '继续说明')
    with pytest.raises(module.QASessionConflictError):
        await submit(manager, current, '实际用户回答')


@pytest.mark.asyncio
async def test_completion_storage_failure_retry_and_cache_cleanup_failure():
    source = FakeRecordProfileSource()
    manager, module, gateway, _ = setup_manager(record_profile_source=source)
    current = await supplement(manager, gateway)
    gateway.next_assessment = assessment(intent='no_more', substantive=False)
    original_save = manager._session_repository.save
    manager._session_repository.save = AsyncMock(return_value=False)
    with pytest.raises(RuntimeError):
        await submit(manager, current, '没有了')
    assert not source.queued
    assert cached(module)['qaFlow']['phase'] == 'supplement'
    manager._session_repository.save = original_save
    manager._session_cache.delete = AsyncMock(return_value=False)
    final = await submit(manager, current, '没有了')
    assert final['status'] == 'finished'
    assert await manager.getSession('u', 's') == final
    assert not await manager.hasPendingSession('u', 's')


@pytest.mark.asyncio
async def test_profile_failure_preserves_completion_and_chat_replay_user_only_facts():
    source = FakeRecordProfileSource()
    source.ensure_queued = AsyncMock(side_effect=RuntimeError('queue unavailable'))
    manager, module, gateway, _ = setup_manager(record_profile_source=source)
    current = await supplement(manager, gateway)
    gateway.next_assessment = assessment(intent='no_more', substantive=False)
    final = await submit(manager, current, '没有了')
    session = module.collection_sessions.document
    session['qaFlow']['finalMessage'] += ' AI_ONLY_MARKER'
    session['qaHistory'][0]['question']['assistantMessage'] += ' AI_ONLY_MARKER'
    assert 'AI_ONLY_MARKER' not in json.dumps(_sanitized_answers(session), ensure_ascii=False)
    chat_manager = object.__new__(ChatSessionManager)
    converted = chat_manager._convertQASessionToChat(session)
    assert converted['qaFlow'] == session['qaFlow']
    assert 'AI_ONLY_MARKER' in ChatSessionManager._buildQAHistorySummary(converted)
    module.collection_sessions.document = converted
    restored = await manager.getSession('u', 's')
    assert restored['assistantMessage'].startswith(final['assistantMessage'])


@pytest.mark.asyncio
async def test_reasoning_inputs_exclude_business_state_and_composition_is_silent():
    manager, _, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    current = await submit(manager, current, '腹胀', streaming=True, thinking=True)
    assert current['reasoning'] == '需要了解用户症状与饮食的关联。'
    for request in gateway.requests:
        if request.profile.name == 'record.plan':
            assert all(
                key not in request.content
                for key in (
                    'interactionCount',
                    'intakeCount',
                    'handledCount',
                    'responseTask',
                    'plannedTotal',
                    'phase',
                )
            )
        else:
            assert request.effective_thinking is False
    current = await submit(manager, current, '餐后明显', thinking=False)
    assert current['reasoning'] == '' and current['assistantMessage']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['none', 'profile', 'report'])
@pytest.mark.parametrize('question_type', ['radio', 'checkbox'])
async def test_opening_evidence_projection(kind, question_type):
    kwargs = {}
    if kind == 'profile':
        kwargs['health_profile_memory_provider'] = FakeHealthProfileMemoryProvider('既往用户确认：腹胀')
    if kind == 'report':
        kwargs['health_report_processing_service'] = FakeHealthReportProcessingService()
    manager, _, gateway, _ = setup_manager(**kwargs)
    gateway.next_question = opening_question(question_type)
    first = await manager.createSession(
        'u', 's', file_list=[{'url': 'https://example.com/a.pdf'}] if kind == 'report' else None
    )
    assert first['questionType'] == 'checkbox'
    assert first['options'] == gateway.next_question['options']
    assert first['question'].startswith(gateway.next_question['question'])
    assert '文字或语音' in first['question']
    assert first['interactionCount'] == 0
    assert ('本次报告' in first['assistantMessage']) == (kind == 'report')
    assert ('既有健康资料' in first['assistantMessage']) == (kind == 'profile')
    assert all('plannedTotal' not in request.content for request in gateway.requests)


@pytest.mark.asyncio
async def test_old_active_version_and_retired_control_endpoints():
    manager, module, _, _ = setup_manager()
    current = await manager.createSession('u', 's')
    with pytest.raises(module.QASessionConflictError):
        await manager.completeSession('u', 's', 1)
    with pytest.raises(module.QASessionConflictError):
        await manager.continueSession('u', 's', 1)
    old = cached(module)
    del old['qaFlow']
    save_cache(module, old)
    for operation in (
        manager.createSession('u', 's'),
        manager.getSession('u', 's'),
        manager.getHistory('u', 's'),
        submit(manager, current, '回答'),
    ):
        with pytest.raises(module.QASessionConflictError):
            await operation


@pytest.mark.asyncio
async def test_concurrent_requests_and_lease_loss_cannot_write():
    manager, module, gateway, runtime = setup_manager()
    current = await manager.createSession('u', 's')
    before = cached(module)
    runtime.lease_manager.try_acquire = AsyncMock(return_value=None)
    with pytest.raises(module.QASessionConflictError):
        await submit(manager, current, '回答')
    runtime.lease_manager.try_acquire = AsyncMock(return_value=SimpleNamespace(ttl_seconds=0.03))
    runtime.lease_manager.renew = AsyncMock(return_value=False)
    gateway.delay = 0.04
    with pytest.raises(module.QASessionConflictError):
        await submit(manager, current, '回答')
    assert runtime.lease_manager.renew.await_count >= 1
    assert cached(module) == before


def test_missing_model_gaps_do_not_skip_the_fixed_history_slots():
    session = {'qaFlow': flow.new_flow(), 'currentQuestion': {'questionId': 'q'}}
    _, decision = flow.advance_flow(session, flow.PatientAssessment(**assessment(sufficient=False)), answer='回答')
    assert decision['target']['dimension'] == 'medical_history'
    draft, decision = flow.advance_flow(
        session, flow.PatientAssessment(**assessment(sufficient=False, gaps=[])), answer='回答'
    )
    assert decision['action'] == 'ask_intake'
    assert decision['target']['dimension'] == 'medical_history'
    assert draft['qaFlow']['phase'] == 'intake'


def test_coverage_validation_rejects_dropped_concerns_and_invented_ids():
    session = {'qaFlow': flow.new_flow(), 'currentQuestion': {'questionId': 'q'}}
    model = flow.PatientAssessment(**assessment(concerns=[concern('甲'), concern('乙')]))
    flow.validate_assessment(model, session, '甲乙')
    draft, decision = flow.advance_flow(session, model, answer='甲乙')
    before = deepcopy(draft)
    with pytest.raises(ValueError):
        flow.apply_presentation(
            draft,
            decision,
            flow.TurnPresentation(
                assistantMessage={'acknowledgement': '已了解', 'rationale': '', 'evidence': []},
                responses=[],
                question=flow.GeneratedQuestion(question='请补充', questionType='text', options={'content': ''}),
            ),
            '',
        )
    assert draft == before
    with pytest.raises(ValueError, match='编造 ID'):
        flow.validate_assessment(
            flow.PatientAssessment(**assessment(concerns=[concern('甲', identity='invented')])), session, '甲乙'
        )
    with pytest.raises(ValueError, match='不能遗漏'):
        flow.validate_assessment(flow.PatientAssessment(**assessment(concerns=[])), draft, '甲乙')


def test_structured_answer_preserves_literal_source_evidence():
    session = {'qaFlow': flow.new_flow()}
    text = '腹胀\n如何改善"饮食"'
    model = flow.PatientAssessment(**assessment(concerns=[concern(text)]))
    flow.validate_assessment(model, session, {'content': text})


@pytest.mark.parametrize(
    'answer',
    [
        '最近吃饭以后腹胀。工作比较忙，想知道饮食方面可以怎么调整？',
        {'content': ['最近吃饭以后腹胀。', {'question': '饮食方面可以怎么调整？'}]},
    ],
)
def test_new_concern_accepts_natural_paraphrase_and_non_contiguous_summary(answer):
    session = {'qaFlow': flow.new_flow()}
    evidence = '餐后腹胀，希望了解如何调整饮食'
    model = flow.PatientAssessment(**assessment(concerns=[concern(evidence)]))
    flow.validate_assessment(model, session, answer)


@pytest.mark.parametrize('answer', [None, '', ' \n\t ', [], {}, {'content': ['', None, 3]}])
def test_new_concern_requires_actual_current_answer_even_with_history_and_report(answer):
    session = {'qaFlow': flow.new_flow(), 'qaHistory': [{'answer': '餐后腹胀，希望调整饮食'}]}
    model = flow.PatientAssessment(**assessment(concerns=[concern('餐后腹胀如何调整饮食')]))
    with pytest.raises(ValueError, match='新增疑问必须有本次用户实际回答') as raised:
        flow.validate_assessment(model, session, answer, sources={'本次报告': '餐后腹胀'})
    assert raised.value.code == 'concern_source_unavailable'


@pytest.mark.asyncio
async def test_overlapping_submissions_have_one_winner_and_release_before_return():
    manager, module, gateway, runtime = setup_manager()
    current = await manager.createSession('u', 's')
    owner = None

    async def acquire(key, owner_id):
        nonlocal owner
        if owner is not None:
            return None
        owner = owner_id
        return SimpleNamespace(owner_id=owner_id, ttl_seconds=30)

    async def release(lease):
        nonlocal owner
        assert owner == lease.owner_id
        owner = None
        return True

    async def renew(lease):
        return owner == lease.owner_id

    runtime.lease_manager.try_acquire = acquire
    runtime.lease_manager.release = release
    runtime.lease_manager.renew = renew
    gateway.delay = 0.02
    results = await asyncio.gather(
        submit(manager, current, '同一回答'),
        submit(manager, current, '另一回答'),
        return_exceptions=True,
    )
    assert sum(isinstance(result, module.QASessionConflictError) for result in results) == 1
    assert cached(module)['qaFlow']['interactionCount'] == 1
    assert len(cached(module)['qaHistory']) == 1
    assert owner is None
