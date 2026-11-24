"""Shortcut answers stay concrete; free answers use the existing text/voice input."""

from copy import deepcopy

import pytest

from modules.record.application import fallback
from modules.record.application import flow
from modules.record.application import intake
from modules.record.application.presentation import ANSWER_HINT

from .test_dynamic_record import cached
from .test_dynamic_record import opening_question
from .test_dynamic_record import save_cache
from .test_dynamic_record import setup_manager
from .test_dynamic_record import submit


def assert_explicit_options(options):
    assert len(options) >= 2
    assert not any('其他' in option or '其它' in option for option in options)
    assert not {'有补充', '需要更正', '点击补充'} & set(options)


@pytest.mark.parametrize(
    'placeholder',
    ['其他', '其它', '其他健康问题', '其它疾病', '其他情况（请补充）', '阅读、音乐或其他兴趣活动'],
)
def test_placeholder_answers_are_rejected_only_when_a_new_question_is_published(placeholder):
    question = flow.GeneratedQuestion(
        question='您这次最希望了解什么？', questionType='radio', options=['报告解读', placeholder]
    )
    with pytest.raises(ValueError):
        flow.validate_new_question(question, action='open_intake')


@pytest.mark.asyncio
@pytest.mark.parametrize('placeholder', ['其他健康问题', '其它健康问题'])
async def test_opening_placeholder_is_repaired_without_losing_the_free_answer_hint(placeholder):
    manager, module, gateway, _ = setup_manager()
    attempts = 0

    def question(payload):
        nonlocal attempts
        attempts += 1
        result = opening_question()
        if attempts == 1:
            result['options'] = ['报告解读', placeholder]
        return result

    gateway.next_question = question
    current = await manager.createSession('u', 's')
    assert attempts == 2
    assert_explicit_options(current['options'])
    assert current['question'].count(ANSWER_HINT) == 1
    assert current['intakeQuestionCount'] == current['qaRoundCount'] == 0
    assert cached(module)['qaHistory'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_repeated_history_button_input_instruction_publishes_a_safe_fallback_and_keeps_the_answer(streaming):
    manager, module, gateway, _ = setup_manager()
    first = await manager.createSession('u', 's')
    before = deepcopy(cached(module))
    attempts = 0

    def question(payload):
        nonlocal attempts
        attempts += 1
        return {
            'question': '您有哪些确诊疾病？如果没列出，可以在“其他疾病”里补充。',
            'questionType': 'checkbox',
            'options': ['没有', '不清楚', '高血压', '糖尿病'],
        }

    gateway.next_question = question
    answer = '我想了解如何安排日常饮食'
    current = await submit(manager, first, answer, streaming=streaming)
    assert attempts == 2
    assert_explicit_options(current['options'])
    assert current['questionType'] == 'checkbox'
    assert {'没有', '不清楚', '高血压', '糖尿病'} <= set(current['options'])
    assert '其他疾病' not in current['question']
    assert '选项中' not in current['question']
    assert current['question'].endswith(ANSWER_HINT)
    stored = cached(module)
    assert len(stored['qaHistory']) == 1
    assert stored['qaHistory'][0]['question'] == before['currentQuestion']
    assert stored['qaHistory'][0]['answer'] == answer
    assert (current['intakeQuestionCount'], current['qaRoundCount']) == (1, 0)
    assert stored['qaFlow']['generationQuality']['mode'] in {'partial', 'fallback'}
    calls = len(gateway.requests)
    assert await submit(manager, first, answer, streaming=streaming) == current
    assert cached(module) == stored
    assert len(gateway.requests) == calls


@pytest.mark.asyncio
async def test_known_history_offers_clear_states_and_accepts_a_concrete_free_text_correction():
    manager, module, gateway, _ = setup_manager()
    first = await manager.createSession('u', 's')
    previous = cached(module)
    previous['qaFlow']['intake']['facts']['general:medical_history'] = {
        'dimension': 'medical_history',
        'scope': 'general',
        'complaintIds': [],
        'status': 'known',
        'summary': '既往医生确诊糖尿病',
        'evidence': [],
    }
    save_cache(module, previous)
    current = await submit(manager, first, '希望了解饮食安排')
    assert current['questionType'] == 'radio'
    assert current['options'] == ['没有需要补充的内容', '目前记不清']
    correction = '更正：没有糖尿病，过去确诊的是高血压'
    next_question = await submit(manager, current, None, other=correction)
    assert next_question['intakeQuestionCount'] == 2
    assert cached(module)['qaHistory'][-1]['answer'] == correction


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('answer', ['其他', ['其它疾病']])
async def test_legacy_cached_placeholders_keep_their_identity_and_raw_user_answers(answer, streaming):
    manager, module, gateway, _ = setup_manager()
    await manager.createSession('u', 's')
    previous = cached(module)
    previous['currentQuestion'].update(
        question='如果有未列出的疾病，可以在“其他疾病”里补充。',
        questionType='checkbox',
        options=['没有', '其他疾病'],
    )
    save_cache(module, previous)
    calls = len(gateway.requests)
    restored = await manager.getSession('u', 's')
    recreated = await manager.createSession('u', 's')
    assert recreated == restored
    assert restored['questionId'] == previous['currentQuestion']['questionId']
    assert restored['question'] == previous['currentQuestion']['question']
    assert restored['options'] == previous['currentQuestion']['options']
    assert cached(module) == previous
    assert len(gateway.requests) == calls
    current = await submit(manager, restored, answer, streaming=streaming)
    stored = cached(module)
    assert stored['qaHistory'][-1]['question'] == previous['currentQuestion']
    assert stored['qaHistory'][-1]['answer'] == answer
    assert_explicit_options(current['options'])
    calls = len(gateway.requests)
    assert await submit(manager, restored, answer, streaming=streaming) == current
    assert len(gateway.requests) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_unlisted_answers_use_other_input_and_keep_existing_precedence(streaming):
    manager, module, _, _ = setup_manager()
    first = await manager.createSession('u', 's')
    free_text = '其他想了解的内容：想了解工作日如何安排休息'
    current = await submit(manager, first, first['options'][:1], other=free_text, streaming=streaming)
    assert cached(module)['qaHistory'][-1]['answer'] == free_text
    assert current['intakeQuestionCount'] == 1
    history = await manager.getHistory('u', 's')
    assert history['history'][-1]['answer'] == free_text


@pytest.mark.parametrize(
    'dimension', ['goal', 'medical_history', 'family_history', *sorted(intake.LIFESTYLE_DIMENSIONS)]
)
def test_every_fallback_topic_has_explicit_answers(dimension):
    target = {'dimension': dimension, 'topic': intake.DIMENSION_LABELS[dimension]}
    question = fallback._gap_question(target)
    assert_explicit_options(question['options'])
    flow.validate_new_question(flow.GeneratedQuestion(**question), action='fallback')
    target['supplementation'] = True
    supplement = fallback._gap_question(target)
    assert supplement['options'] == ['没有需要补充的内容', '目前记不清']
    flow.validate_new_question(flow.GeneratedQuestion(**supplement), action='fallback')
