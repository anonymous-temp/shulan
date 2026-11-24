"""Service transactions enforce the intake bound without rewriting old snapshots."""

import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from modules.record.application import flow
from modules.record.application.intake import DIMENSION_LABELS
from modules.record.application.presentation import CLARIFY_SUPPLEMENT_QUESTION
from modules.record.application.presentation import SUPPLEMENT_QUESTION
from modules.record.application.presentation import with_answer_hint
from tests.unit._support.record_policy import short_interview_policy

from .test_dynamic_record import assessment
from .test_dynamic_record import cached
from .test_dynamic_record import concern
from .test_dynamic_record import save_cache
from .test_dynamic_record import setup_manager
from .test_dynamic_record import submit

# Only the 17-intake + 1-supplement phase shape and deidentified themes come
# from the reviewed session. These answers and model assessments are synthetic;
# no original model JSON, patient identifiers or medical report are reproduced.
REVIEWED_TRAJECTORY = (
    ('intake', '我同时关注排便、饮食和餐后不适。'),
    ('intake', '排便颜色和次数都有些变化。'),
    ('intake', '没有相关肠道病史。'),
    ('intake', '次数有变化，但具体说不清。'),
    ('intake', '减重期间调整了饮食，增加了奶类。'),
    ('intake', '餐后不适主要是恶心。'),
    ('intake', '也有呕吐和腹部不适。'),
    ('intake', '已经存在较长时间，记不清何时开始。'),
    ('intake', '没有胃部相关病史。'),
    ('intake', '排便变化从什么时候开始也记不清。'),
    ('intake', '近期没有用药或补充剂。'),
    ('intake', '发生频率记不清。'),
    ('intake', '没有其他排便伴随症状。'),
    ('intake', '这几项对我来说同样重要。'),
    ('intake', '与进食的关系还不清楚。'),
    ('intake', '对日常生活影响不大。'),
    ('intake', '没有相关家族史。'),
    ('supplement', '没有了'),
)


def essential_assessment(**kwargs):
    return assessment(
        sufficient=False,
        gaps=[
            {
                'topic': '日常睡眠',
                'aspect': 'habits',
                'dimension': 'sleep',
                'scope': 'general',
                'essential': True,
                'reason': '了解可能影响当前不适的用药背景',
                'informationNeeded': '近期是否使用相关药物',
            }
        ],
        **kwargs,
    )


async def seeded_intake(*, answered=5, substantive=None, legacy=False):
    manager, module, gateway, runtime = setup_manager()
    current = await manager.createSession('u', 's')
    snapshot = cached(module)
    snapshot['qaFlow']['interviewPolicy'] = short_interview_policy()
    count = answered if substantive is None else substantive
    snapshot['qaFlow'].update(intakeCount=count, interactionCount=count, intakeQuestionCount=answered)
    snapshot['qaHistory'] = [
        {
            'question': {**deepcopy(snapshot['currentQuestion']), 'questionId': f'previous-{index}'},
            'answer': '合成历史回答，相关背景仍需了解。',
            'answeredAt': '2026-01-01T00:00:00',
            'interactionCount': min(index + 1, count),
        }
        for index in range(answered)
    ]
    if legacy:
        snapshot['qaFlow'].pop('interviewPolicy')
        snapshot['qaFlow'].pop('intake')
        snapshot['qaFlow'].pop('intakeQuestionCount')
        snapshot['qaFlow'].pop('qaRoundCount')
        snapshot['qaFlow'].pop('qaVagueCount')
    save_cache(module, snapshot)
    gateway.next_assessment = essential_assessment()
    return manager, module, gateway, runtime, current


def assert_supplement(result, *, clarify=False, qa=False):
    assert result['status'] == 'question'
    assert result['phase'] == ('qa' if qa else 'supplement')
    assert result['question'] == with_answer_hint(CLARIFY_SUPPLEMENT_QUESTION if clarify else SUPPLEMENT_QUESTION)
    assert result['questionType'] == 'radio'
    assert result['options'] == [flow.NO_MORE_OPTION]


def last_task(gateway):
    request = next(item for item in reversed(gateway.requests) if item.profile.name == 'record.question')
    payload, _ = json.JSONDecoder().raw_decode(request.content)
    return payload['responseTask']


def medication_understanding():
    return {
        'complaints': [],
        'preferredIndex': None,
        'priorityClarificationNeeded': False,
        'facts': [
            {
                'dimension': 'medication',
                'scope': 'general',
                'status': 'known',
                'summary': '近期没有用药',
                'evidence': [{'source': '本次回答', 'quote': '近期没有用药'}],
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_sixth_successful_answer_invites_supplement_even_with_an_essential_gap(streaming):
    manager, module, gateway, _, current = await seeded_intake()
    result = await submit(manager, current, '近期用药情况暂时记不清。', streaming=streaming)
    assert_supplement(result)
    assert result['interactionCount'] == 6
    assert len(cached(module)['qaHistory']) == 6
    assert last_task(gateway)['action'] == 'invite_supplement'


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize(('answered', 'substantive'), [(5, 3), (0, 5)])
async def test_unclear_answers_count_toward_the_intake_bound_without_inflating_substantive_count(
    answered, substantive, streaming
):
    manager, module, gateway, _, current = await seeded_intake(answered=answered, substantive=substantive)
    gateway.next_assessment = essential_assessment(intent='unclear', substantive=False)
    result = await submit(manager, current, '嗯，还是说不清。', streaming=streaming)
    assert_supplement(result)
    assert result['interactionCount'] == substantive
    assert len(cached(module)['qaHistory']) == answered + 1


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('stage', ['analysis', 'response'])
async def test_failed_generation_at_limit_continues_with_saved_answer_and_counts_once(stage, streaming):
    manager, module, gateway, _, current = await seeded_intake()
    setattr(gateway, 'invalid_' + stage, 2)
    result = await submit(manager, current, '暂时想不起具体用药。', streaming=streaming)
    assert module.collection_sessions.document is None
    assert result['status'] == 'question' and result['phase'] == 'supplement'
    assert result['interactionCount'] == 6
    assert len(cached(module)['qaHistory']) == 6
    assert cached(module)['qaHistory'][-1]['answer'] == '暂时想不起具体用药。'
    persisted = deepcopy(cached(module))
    calls = len(gateway.requests)
    repeated = await submit(manager, current, '暂时想不起具体用药。', streaming=streaming)
    assert repeated['questionId'] == result['questionId']
    assert repeated['interactionCount'] == 6
    assert cached(module) == persisted
    assert len(gateway.requests) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize('answered', [10, 17])
async def test_legacy_active_intake_changes_only_on_the_next_successful_answer(answered):
    manager, module, gateway, _, current = await seeded_intake(answered=answered, legacy=True)
    before = deepcopy(cached(module))
    manager._session_cache.save = AsyncMock(wraps=manager._session_cache.save)
    manager._session_repository.save = AsyncMock(wraps=manager._session_repository.save)
    calls = len(gateway.requests)
    status = await manager.getSession('u', 's')
    history = await manager.getHistory('u', 's')
    assert status['phase'] == history['phase'] == 'intake'
    assert len(history['history']) == answered
    assert cached(module) == before
    manager._session_cache.save.assert_not_awaited()
    manager._session_repository.save.assert_not_awaited()
    assert len(gateway.requests) == calls

    result = await submit(manager, current, '相关用药还需要核对。')
    assert_supplement(result)
    assert result['interactionCount'] == answered + 1
    assert len(cached(module)['qaHistory']) == answered + 1


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_no_more_after_sixth_answer_finishes_without_another_model_call(streaming):
    manager, module, gateway, _, current = await seeded_intake()
    current = await submit(manager, current, '近期没有服药。', streaming=streaming)
    assert_supplement(current)
    calls = len(gateway.requests)
    result = await submit(manager, current, flow.NO_MORE_OPTION, streaming=streaming)
    assert result['status'] == 'finished'
    assert result['interactionCount'] == 6
    assert result['endReason'] == 'no_more_questions'
    assert len(gateway.requests) == calls
    assert len(module.collection_sessions.document['qaHistory']) == 7


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_twentieth_interaction_finishes_instead_of_inviting_supplement(streaming):
    manager, module, gateway, _, current = await seeded_intake(answered=19)
    gateway.next_assessment = essential_assessment(concerns=[concern('是否需要调整饮食', ready=False)])
    result = await submit(manager, current, '想了解是否需要调整饮食，但用药情况还不清楚。', streaming=streaming)
    assert result['status'] == 'finished'
    assert result['interactionCount'] == 20
    assert result['endReason'] == 'interaction_limit'
    assert 'questionId' not in result
    assert last_task(gateway)['action'] == 'finish'
    assert module.collection_sessions.document['qaFlow']['concerns'][0]['status'] == 'limited'


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_save_failure_at_limit_does_not_commit_or_count_the_attempt(streaming):
    manager, module, _, _, current = await seeded_intake()
    before = deepcopy(cached(module))
    save = manager._session_cache.save
    manager._session_cache.save = AsyncMock(return_value=False)
    if streaming:
        events = [
            event
            async for event in manager.submitAnswerStream(
                'u', 's', current['questionId'], '用药情况还需核对。', thinking=False
            )
        ]
        assert events[-1]['code'] == 500
        assert events[-1]['data']['type'] == 'error'
        assert not any(event['data']['type'] in ('question', 'finish') for event in events)
    else:
        with pytest.raises(RuntimeError, match='保存回答失败'):
            await submit(manager, current, '用药情况还需核对。')
    assert cached(module) == before
    manager._session_cache.save = save
    result = await submit(manager, current, '用药情况还需核对。', streaming=streaming)
    assert_supplement(result)
    assert result['interactionCount'] == 6
    assert len(cached(module)['qaHistory']) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_limit_invites_supplement_before_pending_concern_then_no_more_gives_a_limited_final_response(streaming):
    manager, module, gateway, _, current = await seeded_intake()
    gateway.next_assessment = essential_assessment(concerns=[concern('恶心和饮食是否有关', ready=False)])
    current = await submit(manager, current, '想了解恶心和饮食是否有关，但时间记不清。', streaming=streaming)
    assert_supplement(current)
    pending = cached(module)['qaFlow']['concerns'][0]
    assert pending['status'] == 'limited'
    assert pending['response'] in current['assistantMessage']
    assert current['qaRoundCount'] == 0
    calls = len(gateway.requests)
    gateway.next_assessment = essential_assessment(
        intent='no_more',
        substantive=False,
        concerns=[concern(pending['text'], ready=False, identity=pending['id'])],
    )
    result = await submit(manager, current, flow.NO_MORE_OPTION, streaming=streaming)
    assert result['status'] == 'finished'
    assert result['interactionCount'] == 6
    assert result['endReason'] == 'no_more_questions'
    assert pending['response']
    assert module.collection_sessions.document['qaFlow']['concerns'][0]['status'] == 'limited'
    assert len(gateway.requests) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_supplement_accepts_new_facts_and_concern_qa_without_reopening_intake(streaming):
    manager, module, gateway, _, current = await seeded_intake()
    current = await submit(manager, current, '用药情况仍需核对。', streaming=streaming)
    assert_supplement(current)
    answer = '补充一下，近期没有用药；我想了解恶心和饮食是否有关。'
    gateway.next_assessment = essential_assessment(
        intent='additional',
        concerns=[concern('恶心和饮食是否有关', ready=False)],
        intake=medication_understanding(),
    )
    current = await submit(manager, current, answer, streaming=streaming)
    assert current['phase'] == 'qa'
    assert current['interactionCount'] == 7
    assert current['qaRoundCount'] == 1
    snapshot = cached(module)
    assert snapshot['qaFlow']['intakeCount'] == 6
    assert snapshot['qaFlow']['intake']['facts']['general:medication']['summary'] == '近期没有用药'
    answered = snapshot['qaFlow']['concerns'][0]
    gateway.next_assessment = essential_assessment(concerns=[])
    current = await submit(manager, current, '目前还是想不起发生时间。', streaming=streaming)
    assert_supplement(current, clarify=True, qa=True)
    assert current['interactionCount'] == 8
    assert current['qaRoundCount'] == 1
    assert cached(module)['qaFlow']['intakeCount'] == 6
    assert cached(module)['qaFlow']['concerns'][0]['status'] == 'limited'
    answer = '恶心和饮食是否有关，能再解释一下吗？'
    repeated = concern(answered['text'], ready=False, identity=answered['id'])
    repeated['evidence'] = answer
    gateway.next_assessment = essential_assessment(intent='additional', concerns=[repeated])
    current = await submit(manager, current, answer, streaming=streaming)
    assert_supplement(current, qa=True)
    assert current['interactionCount'] == 9
    assert current['qaRoundCount'] == 2
    assert cached(module)['qaFlow']['intakeCount'] == 6
    assert cached(module)['qaFlow']['concerns'][0]['handledCount'] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_supplement_with_only_health_facts_saves_them_and_repeats_the_fixed_invitation(streaming):
    manager, module, gateway, _, current = await seeded_intake()
    current = await submit(manager, current, '用药情况仍需核对。', streaming=streaming)
    assert_supplement(current)
    intake_count = cached(module)['qaFlow']['intakeCount']
    gateway.next_assessment = essential_assessment(intent='answer', concerns=[], intake=medication_understanding())
    answer = '补充一下，近期没有用药。'
    result = await submit(manager, current, answer, streaming=streaming)
    assert_supplement(result, clarify=True)
    assert result['interactionCount'] == 7
    snapshot = cached(module)
    assert snapshot['qaFlow']['intakeCount'] == intake_count == 6
    assert snapshot['qaFlow']['concerns'] == []
    assert snapshot['qaFlow']['intake']['facts']['general:medication']['summary'] == '近期没有用药'
    assert snapshot['qaHistory'][-1]['answer'] == answer
    assert len(snapshot['qaHistory']) == 7
    task = last_task(gateway)
    assert task['action'] == 'clarify_supplement'
    assert snapshot['qaFlow']['qaRoundCount'] == 0
    assert snapshot['qaFlow']['qaVagueCount'] == 1
    assert task['questionMode'] == 'fixed'
    assert task['target'] is None
    assert task['responses'] == []


@pytest.mark.asyncio
async def test_completed_legacy_seventeen_intake_history_is_read_without_rewriting():
    manager, module, gateway, _, _ = await seeded_intake(answered=17, legacy=True)
    saved = cached(module)
    saved.update(status='completed', completedAt='2026-01-01T01:00:00', currentQuestion=None)
    saved['qaFlow'].update(phase='finished', endReason='no_more_questions', finalMessage=flow.COMPLETION_MESSAGE)
    last_question = deepcopy(saved['qaHistory'][-1]['question'])
    last_question.update(questionId='old-supplement', phase='supplement', question=SUPPLEMENT_QUESTION)
    saved['qaHistory'].append(
        {
            'question': last_question,
            'answer': flow.NO_MORE_OPTION,
            'answeredAt': '2026-01-01T01:00:00',
            'interactionCount': 17,
        }
    )
    module.collection_sessions.document = deepcopy(saved)
    module.redis_client.value = None
    manager._session_cache.save = AsyncMock(wraps=manager._session_cache.save)
    manager._session_cache.delete = AsyncMock(wraps=manager._session_cache.delete)
    manager._session_repository.save = AsyncMock(wraps=manager._session_repository.save)
    calls = len(gateway.requests)
    status = await manager.getSession('u', 's')
    history = await manager.getHistory('u', 's')
    assert status['status'] == history['status'] == 'finished'
    assert status['interactionCount'] == 17
    assert [item['phase'] for item in history['history']] == [phase for phase, _ in REVIEWED_TRAJECTORY]
    assert module.collection_sessions.document == saved
    assert module.redis_client.value is None
    manager._session_cache.save.assert_not_awaited()
    manager._session_cache.delete.assert_not_awaited()
    manager._session_repository.save.assert_not_awaited()
    assert len(gateway.requests) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_reviewed_long_intake_shape_now_stops_after_six_answers(streaming):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    legacy = cached(module)
    legacy['qaFlow']['interviewPolicy'] = short_interview_policy()
    save_cache(module, legacy)
    # Deterministically leave a fresh essential purpose available beyond six;
    # this tests the policy cap, not the unarchived model's clinical assessment.
    dimensions = [key for key in DIMENSION_LABELS if key not in ('chief_complaint', 'priority')]
    gateway.next_assessment = assessment(
        sufficient=False,
        gaps=[
            {
                'topic': '餐后不适',
                'aspect': 'symptoms',
                'dimension': dimension,
                'scope': 'primary',
                'essential': True,
                'reason': '了解相关背景',
                'informationNeeded': DIMENSION_LABELS[dimension],
            }
            for dimension in dimensions
        ],
    )
    for count, (_, answer) in enumerate(REVIEWED_TRAJECTORY[:6], start=1):
        current = await submit(manager, current, answer, streaming=streaming)
        assert current['interactionCount'] == count
        assert current['phase'] == ('intake' if count < 6 else 'supplement')
    assert_supplement(current)
    assert [item['answer'] for item in cached(module)['qaHistory']] == [answer for _, answer in REVIEWED_TRAJECTORY[:6]]
    assert cached(module)['qaFlow']['concerns'] == []
    assert last_task(gateway)['action'] == 'invite_supplement'
    calls = len(gateway.requests)
    final = await submit(manager, current, REVIEWED_TRAJECTORY[-1][1], streaming=streaming)
    assert final['status'] == 'finished'
    assert final['interactionCount'] == 6
    assert len(gateway.requests) == calls
