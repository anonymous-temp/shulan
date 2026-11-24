"""Opening choices, repair and compatibility through the production Record flow."""

from copy import deepcopy

import pytest

from modules.record.application import flow

from .test_creation_recovery import creation_manager
from .test_creation_recovery import receipt
from .test_dynamic_record import cached
from .test_dynamic_record import opening_question
from .test_dynamic_record import save_cache
from .test_dynamic_record import setup_manager
from .test_dynamic_record import submit
from .test_patient_language_and_choices import latest_analysis
from .test_patient_language_and_choices import patient_assessment


@pytest.mark.parametrize('question_type', ['radio', 'checkbox'])
@pytest.mark.parametrize('option_count', [2, 3, 4, 5, 6, 12])
def test_opening_accepts_choice_boundaries_without_changing_counters(question_type, option_count):
    question = opening_question(question_type)
    question['options'] = (question['options'] + ['说不清具体感受'])[:option_count]
    draft = {'qaFlow': flow.new_flow(), 'currentQuestion': None}
    before = deepcopy(draft['qaFlow'])
    presentation = flow.TurnPresentation(
        assistantMessage={'acknowledgement': '先了解您的整体感受。', 'rationale': '了解相关背景。', 'evidence': []},
        responses=[],
        question=question,
    )
    flow.apply_presentation(draft, {'action': 'open_intake', 'responses': []}, presentation, '')
    assert draft['currentQuestion']['questionType'] == question_type
    assert draft['currentQuestion']['options'] == question['options']
    assert draft['qaFlow'] == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('question_type', 'options'),
    [
        ('text', {'content': ''}),
        ('radio', ['肠胃']),
        ('checkbox', [f'具体关注{index}' for index in range(13)]),
        ('radio', []),
        ('checkbox', ['肠胃', '排便', '其他', '']),
        ('radio', ['肠胃', '排便', '其他', ' ']),
        ('checkbox', ['肠胃', '排便', '其他', '肠胃']),
        ('radio', ['肠胃', '排便', '其他', ' 肠胃 ']),
    ],
)
async def test_invalid_opening_is_repaired_once_before_saving(question_type, options):
    manager, module, gateway, _ = setup_manager()
    attempts = []

    def generate(payload):
        assert payload['responseTask']['action'] == 'open_intake'
        assert module.redis_client.value is None
        attempts.append(payload)
        if len(attempts) == 1:
            return {'question': '最近有什么感受？', 'questionType': question_type, 'options': options}
        return opening_question()

    gateway.next_question = generate
    first = await manager.createSession('u', 's')
    assert len(attempts) == 2
    requests = [request for request in gateway.requests if request.profile.name == 'record.question']
    assert len(requests) == 2
    assert '至少需要两个' in requests[-1].content and '最多 12 个' in requests[-1].content
    assert '不必凑满' in requests[-1].content and '不能使用 text' in requests[-1].content
    assert all(request.effective_thinking is False for request in requests)
    assert first['options'] == opening_question()['options']
    assert first['interactionCount'] == 0
    assert cached(module)['qaHistory'] == []
    assert cached(module)['currentQuestion']['questionId'] == first['questionId']


@pytest.mark.asyncio
@pytest.mark.parametrize('question_type', ['radio', 'checkbox'])
@pytest.mark.parametrize('option_count', [2, 3])
async def test_fewer_relevant_options_are_saved_without_an_extra_model_call(question_type, option_count):
    manager, module, gateway, _ = setup_manager()
    gateway.next_question = opening_question(question_type)
    gateway.next_question['options'] = gateway.next_question['options'][:option_count]
    first = await manager.createSession('u', 's')
    questions = [request for request in gateway.requests if request.profile.name == 'record.question']
    assert len(questions) == 1
    assert first['options'] == opening_question()['options']
    assert first['interactionCount'] == 0
    assert cached(module)['qaHistory'] == []


@pytest.mark.asyncio
async def test_repeated_invalid_opening_publishes_queryable_conservative_choice():
    creation = creation_manager()
    creation.gateway.next_question = {'question': '请描述', 'questionType': 'text', 'options': {'content': ''}}
    current = await creation.manager.createSession('user-1', 'session-1')
    assert current['status'] == 'question'
    assert current['questionType'] in ('radio', 'checkbox')
    assert 2 <= len(current['options']) <= flow.OPENING_MAX_OPTIONS
    assert 'errorCode' not in current
    snapshot = await receipt(creation)
    assert snapshot['currentQuestion']['questionId'] == current['questionId']
    assert snapshot['qaFlow']['generationQuality']['mode'] == 'partial'
    assert not snapshot.get('qaHistory')
    assert creation.module.collection_sessions.document is None
    requests = [request for request in creation.gateway.requests if request.profile.name == 'record.question']
    assert len(requests) == 2
    restored = await creation.manager.getSession('user-1', 'session-1')
    assert restored['status'] == 'question' and restored['questionId'] == current['questionId']


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('question_type', ['radio', 'checkbox'])
@pytest.mark.parametrize('free_text', [False, True])
async def test_opening_answers_restore_and_reach_the_next_analysis(streaming, question_type, free_text):
    manager, module, gateway, _ = setup_manager()
    gateway.next_question = opening_question(question_type)
    first = await manager.createSession('u', 's')
    restored = await manager.getSession('u', 's')
    assert restored['questionId'] == first['questionId']
    assert restored['options'] == first['options']
    assert restored['assistantMessage'] == first['assistantMessage']
    answer = first['options'][: 2 if question_type == 'checkbox' else 1]
    other = '补充：主要是餐后腹胀，排便也有变化。' if free_text else None
    expected = other if free_text else answer
    gateway.next_assessment = patient_assessment()
    gateway.next_question = {
        'question': '您提到肠胃方面的感受，最近主要有哪些表现？可多选。',
        'questionType': 'checkbox',
        'options': ['腹胀', '嗳气', '腹痛'],
    }
    current = await submit(manager, first, answer, other=other, streaming=streaming, thinking=False)
    assert current['interactionCount'] == 1
    assert current['questionId'] != first['questionId']
    assert current['assistantMessage'] and current['reasoning'] == ''
    assert latest_analysis(gateway)['本次回答'] == expected
    assert cached(module)['qaHistory'][0]['answer'] == expected
    history = await manager.getHistory('u', 's')
    assert history['history'][0]['questionType'] == 'checkbox'
    assert history['history'][0]['answer'] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_existing_text_opening_is_replayed_and_can_be_answered_without_regeneration(streaming):
    manager, module, gateway, _ = setup_manager()
    await manager.createSession('u', 's')
    session = cached(module)
    session['currentQuestion'].update(question='最近有什么身体感受？', questionType='text', options={'content': ''})
    save_cache(module, session)
    request_count = len(gateway.requests)
    replay = await manager.createSession('u', 's')
    assert replay['questionType'] == 'text'
    assert replay['questionId'] == session['currentQuestion']['questionId']
    assert len(gateway.requests) == request_count
    assert (await manager.getSession('u', 's'))['options'] == {'content': ''}
    answer = {'content': '有些肠胃不适'}
    current = await submit(manager, replay, answer, streaming=streaming)
    assert current['interactionCount'] == 1
    assert cached(module)['qaFlow']['version'] == session['qaFlow']['version'] == 2
    history = await manager.getHistory('u', 's')
    assert history['history'][0]['questionType'] == 'text'
    assert history['history'][0]['answer'] == answer


@pytest.mark.parametrize('action', ['invite_supplement', 'clarify_supplement'])
@pytest.mark.parametrize('question_type', ['radio', 'checkbox'])
def test_supplement_questions_are_owned_by_the_service(action, question_type):
    draft = {'qaFlow': flow.new_flow(), 'currentQuestion': None}
    draft['qaFlow']['phase'] = 'supplement'
    presentation = flow.TurnPresentation(
        assistantMessage={
            'acknowledgement': '日常生活值得从不同方面了解。',
            'rationale': '了解相关背景。',
            'evidence': [],
        },
        responses=[],
        question=opening_question(question_type),
    )
    flow.apply_presentation(draft, {'action': action, 'responses': [], 'target': None}, presentation, '')
    assert draft['currentQuestion']['questionType'] == 'radio'
    assert draft['currentQuestion']['options'] == ['没有了']
    assert '文字或语音' in draft['currentQuestion']['question']
