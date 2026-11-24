"""Recover an already saved answer's missing reply without duplicating the turn."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.errors import AionGatewayError
from modules.record.application import flow
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
from .test_dynamic_record import supplement
from .test_refresh_reanswer import CONTEXT_FIELDS
from .test_refresh_reanswer import ready_session

SAVED_ANSWER = '最近没有服用相关药物，也没有接受过相关治疗。'
# This reproduces the reviewed legacy document's three answered-question shape,
# using entirely synthetic wording and no original user or session identifiers.
LEGACY_ANSWERS = (
    ('最近睡眠情况如何？', '一般每晚睡七个小时。'),
    ('平时有哪些运动？', '每周步行三次。'),
    ('近期饮食习惯如何？', '按时吃饭，偶尔喝牛奶。'),
)


async def missing_reply(shape='v2-missing'):
    manager, module, gateway, reports, memory, _, _, snapshot = await ready_session(streaming=False)
    snapshot['qaFlow'].pop('interviewPolicy', None)
    snapshot['qaHistory'].append(
        {
            'question': deepcopy(snapshot['currentQuestion']),
            'answer': SAVED_ANSWER,
            'answeredAt': '2026-01-01T10:00:00',
            'interactionCount': 3,
        }
    )
    snapshot['answers'][snapshot['currentQuestion']['questionId']] = SAVED_ANSWER
    snapshot['qaFlow'].update(interactionCount=3, intakeCount=3)
    if shape.endswith('missing') or shape == 'legacy-awaiting':
        snapshot['currentQuestion'] = None
    if shape.startswith('legacy'):
        snapshot.pop('qaFlow')
        snapshot.update(plannedTotal=3, round=1, roundStartIndex=0)
    if shape == 'legacy-awaiting':
        snapshot['status'] = 'awaiting_decision'
        snapshot['qaHistory'] = [
            {
                'question': {
                    'questionId': f'legacy-{index}',
                    'question': question,
                    'questionType': 'text',
                    'options': {'content': ''},
                    'phase': 'intake',
                },
                'answer': answer,
                'answeredAt': '2026-01-01T10:00:00',
            }
            for index, (question, answer) in enumerate(LEGACY_ANSWERS)
        ]
        snapshot['answers'] = {item['question']['questionId']: item['answer'] for item in snapshot['qaHistory']}
    save_cache(module, snapshot)
    return manager, module, gateway, reports, memory, deepcopy(snapshot)


def assert_original_data_retained(saved, before):
    assert saved['qaHistory'] == before['qaHistory']
    assert saved['answers'] == before['answers']
    assert {key: saved[key] for key in CONTEXT_FIELDS} == {key: before[key] for key in CONTEXT_FIELDS}
    assert saved['createdAt'] == before['createdAt']
    assert saved['qaFlow']['version'] == 2
    assert saved['qaFlow']['interactionCount'] == 3
    assert saved['qaFlow']['intakeCount'] == 3
    if 'qaFlow' in before:
        for key, value in before['qaFlow']['intake']['facts'].items():
            assert saved['qaFlow']['intake']['facts'][key] == value


async def recover_then_query(manager, method):
    # Exercise the explicit recovery command; public GETs only render snapshots.
    async with manager._sessionLease('u', 's') as guard:
        session = await manager._load_session('u', 's')
        await manager._recoverReplyUnderLease(session, 'u', 's', guard)
    return await getattr(manager, method)('u', 's')


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['getSession', 'getHistory'])
@pytest.mark.parametrize(
    'shape', ['v2-missing', 'v2-stale', 'legacy-active-missing', 'legacy-active-stale', 'legacy-awaiting']
)
async def test_recovery_repairs_saved_answer_missing_reply_without_reappending_or_recounting(shape, method):
    manager, module, gateway, reports, memory, before = await missing_reply(shape)
    calls = len(gateway.requests)
    report_calls, memory_calls = len(reports.calls), len(memory.calls)
    result = await recover_then_query(manager, method)
    assert result['status'] == 'question'
    saved = deepcopy(cached(module))
    assert_original_data_retained(saved, before)
    assert saved['currentQuestion']['questionId'] != before['qaHistory'][-1]['question']['questionId']
    if shape == 'legacy-awaiting':
        assert result['phase'] == 'supplement'
        assert saved['currentQuestion']['question'] == SUPPLEMENT_QUESTION
        assert saved['currentQuestion']['options'] == [flow.NO_MORE_OPTION]
    assert len(gateway.requests) - calls == (0 if shape == 'legacy-awaiting' else 2)
    assert len(reports.calls) == report_calls
    assert len(memory.calls) == memory_calls
    manager._session_cache.save.assert_awaited_once()
    manager._session_repository.save.assert_not_awaited()
    calls = len(gateway.requests)
    restored = await manager.getSession('u', 's')
    history = await manager.getHistory('u', 's')
    assert restored['questionId'] == saved['currentQuestion']['questionId']
    assert len(history['history']) == 3
    assert [item['answer'] for item in history['history']] == [item['answer'] for item in before['qaHistory']]
    assert cached(module) == saved
    assert len(gateway.requests) == calls
    manager._session_cache.save.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['getSession', 'getHistory'])
@pytest.mark.parametrize('streaming', [False, True])
async def test_recovered_question_accepts_one_new_answer_and_replays_it_idempotently(method, streaming):
    manager, module, gateway, _, _, before = await missing_reply()
    await recover_then_query(manager, method)
    current = await manager.getSession('u', 's')
    recovery = deepcopy(cached(module))
    answer = '现在可以补充，近期饮食规律。'
    result = await submit(manager, current, answer, streaming=streaming)
    assert result['interactionCount'] == 4
    saved = deepcopy(cached(module))
    assert saved['qaHistory'][:-1] == before['qaHistory']
    assert len(saved['qaHistory']) == 4
    assert saved['qaHistory'][-1]['question'] == recovery['currentQuestion']
    assert saved['qaHistory'][-1]['answer'] == answer
    assert all(saved['answers'][key] == value for key, value in before['answers'].items())
    calls = len(gateway.requests)
    saves = manager._session_cache.save.await_count
    replayed = await submit(manager, current, answer, streaming=streaming)
    assert replayed == result
    assert cached(module) == saved
    assert len(gateway.requests) == calls
    assert manager._session_cache.save.await_count == saves


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['getSession', 'getHistory'])
async def test_matching_last_submission_restores_its_published_question_without_model_calls(method):
    manager, module, gateway, _, _, published, _, snapshot = await ready_session(streaming=False)
    before = deepcopy(snapshot)
    snapshot['currentQuestion'] = None
    save_cache(module, snapshot)
    calls = len(gateway.requests)
    await recover_then_query(manager, method)
    restored = await manager.getSession('u', 's')
    assert restored == published
    saved = cached(module)
    assert saved['qaHistory'] == before['qaHistory']
    assert saved['answers'] == before['answers']
    assert saved['qaFlow'] == before['qaFlow']
    assert saved['currentQuestion']['questionId'] == before['currentQuestion']['questionId']
    assert len(gateway.requests) == calls
    manager._session_cache.save.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['getSession', 'getHistory'])
@pytest.mark.parametrize(
    'failure', ['analysis-gateway', 'presentation-gateway', 'analysis-json', 'presentation-json', 'save']
)
async def test_recovery_uses_fallback_for_model_failure_but_retries_unsaved_results(method, failure):
    manager, module, gateway, _, _, before = await missing_reply()
    content = gateway.content
    save = manager._session_cache.save

    def damaged(request):
        result = content(request)
        target = 'record.plan' if failure.startswith('analysis') else 'record.question'
        if request.profile.name != target:
            return result
        if failure.endswith('json'):
            return '{"unfinished":'
        raise AionGatewayError('synthetic missing-reply recovery failure')

    if failure == 'save':
        manager._session_cache.save = AsyncMock(return_value=False)
        error_type = RuntimeError
    else:
        gateway.content = damaged
        result = await recover_then_query(manager, method)
        assert result['status'] == 'question'
        saved = deepcopy(cached(module))
        assert_original_data_retained(saved, before)
        assert saved['currentQuestion']['questionId'] != before['qaHistory'][-1]['question']['questionId']
        assert saved['qaFlow']['generationQuality']['mode'] in ('fallback', 'partial')
        calls = len(gateway.requests)
        await manager.getSession('u', 's')
        await manager.getHistory('u', 's')
        assert cached(module) == saved
        assert len(gateway.requests) == calls
        manager._session_cache.save.assert_awaited_once()
        manager._session_repository.save.assert_not_awaited()
        return
    with pytest.raises(error_type):
        await recover_then_query(manager, method)
    assert cached(module) == before
    assert module.collection_sessions.document is None
    manager._session_repository.save.assert_not_awaited()
    gateway.content = content
    manager._session_cache.save = save
    other_method = 'getHistory' if method == 'getSession' else 'getSession'
    recovered = await recover_then_query(manager, other_method)
    assert recovered['status'] == 'question'
    assert_original_data_retained(cached(module), before)
    assert cached(module)['currentQuestion']['questionId'] != before['qaHistory'][-1]['question']['questionId']


@pytest.mark.asyncio
@pytest.mark.parametrize('complete_analysis', [False, True])
async def test_pending_concerns_survive_complete_or_partial_recovery(complete_analysis):
    manager, module, gateway, _, _, before = await missing_reply()
    before['qaFlow']['phase'] = 'qa'
    before['qaHistory'][-1]['question']['phase'] = 'qa'
    before['qaFlow']['concerns'] = [
        {
            'id': f'pending-{index}',
            'text': text,
            'evidence': text,
            'sourceQuestionId': before['qaHistory'][0]['question']['questionId'],
            'originPhase': 'supplement',
            'handledCount': 0,
            'status': 'pending',
            'response': '',
            'informationNeeded': '相关不适发生的时间',
            'readyToAnswer': False,
        }
        for index, text in enumerate(('是否可以调整饮食', '是否适合散步'))
    ]
    before['qaHistory'][-1]['question']['concernId'] = 'pending-0'
    save_cache(module, before)
    if complete_analysis:
        gateway.next_assessment = assessment(
            sufficient=False,
            concerns=[concern(item['text'], ready=False, identity=item['id']) for item in before['qaFlow']['concerns']],
        )
    calls = len(gateway.requests)
    result = await recover_then_query(manager, 'getSession')
    assert result['phase'] == 'qa'
    saved = cached(module)
    assert_original_data_retained(saved, before)
    assert [item['id'] for item in saved['qaFlow']['concerns']] == ['pending-0', 'pending-1']
    if complete_analysis:
        assert all(item['status'] == 'limited' for item in saved['qaFlow']['concerns'])
        assert saved['currentQuestion']['concernId'] is None
    else:
        assert all(item['status'] == 'pending' for item in saved['qaFlow']['concerns'])
        assert saved['qaFlow']['generationQuality']['mode'] in ('fallback', 'partial')
        assert all(not item['response'] for item in saved['qaFlow']['concerns'])
    request = next(item for item in gateway.requests[calls:] if item.profile.name == 'record.plan')
    patient, _ = json.JSONDecoder().raw_decode(request.content[request.content.index('{') :])
    assert patient['本次回答'] == SAVED_ANSWER
    assert len(patient['历史问答']) == 2
    assert {item['编号'] for item in patient['已有疑问']} == {'pending-0', 'pending-1'}


@pytest.mark.asyncio
@pytest.mark.parametrize(('cap', 'answered'), [(6, 5), (6, 6), (10, 6), (10, 9), (10, 10)])
async def test_recovering_a_recorded_answer_does_not_count_it_again_for_the_intake_cap(cap, answered):
    manager, module, gateway, _, _, before = await missing_reply()
    while len(before['qaHistory']) < answered:
        item = deepcopy(before['qaHistory'][-1])
        item['question']['questionId'] = f'saved-question-{len(before["qaHistory"])}'
        before['qaHistory'].append(item)
        before['answers'][item['question']['questionId']] = item['answer']
    before['qaFlow'].update(interactionCount=answered, intakeCount=answered, intakeQuestionCount=answered)
    if cap == 6:
        before['qaFlow']['interviewPolicy'] = short_interview_policy()
    save_cache(module, before)
    gateway.next_assessment = assessment(
        sufficient=False,
        gaps=[
            {
                'topic': '饮水情况',
                'dimension': 'hydration',
                'scope': 'general',
                'essential': True,
                'reason': '了解与当前不适相关的家族背景',
                'informationNeeded': '有无相关家族病史',
            }
        ],
    )
    restored = await recover_then_query(manager, 'getSession')
    assert restored['phase'] == ('intake' if answered < cap else 'supplement')
    assert restored['interactionCount'] == answered
    assert cached(module)['qaFlow']['intakeCount'] == answered
    assert restored['intakeQuestionCount'] == answered
    assert cached(module)['qaHistory'] == before['qaHistory']


@pytest.mark.asyncio
async def test_concurrent_recovery_commands_publish_only_one_reply():
    manager, module, gateway, _, _, before = await missing_reply()
    lease_manager = manager._agent_runtime.lease_manager
    owner = None

    async def acquire(key, owner_id):
        nonlocal owner
        if owner is not None:
            return None
        owner = owner_id
        return SimpleNamespace(owner_id=owner_id, ttl_seconds=30)

    async def release(lease):
        nonlocal owner
        assert lease.owner_id == owner
        owner = None
        return True

    async def renew(lease):
        return lease.owner_id == owner

    lease_manager.try_acquire = acquire
    lease_manager.release = release
    lease_manager.renew = renew
    gateway.delay = 0.02
    calls = len(gateway.requests)
    results = await asyncio.gather(
        recover_then_query(manager, 'getSession'), recover_then_query(manager, 'getHistory'), return_exceptions=True
    )
    successes = [item for item in results if isinstance(item, dict)]
    assert successes
    assert all(item['status'] == 'question' for item in successes)
    assert all(isinstance(item, (dict, module.QASessionConflictError)) for item in results)
    assert len(gateway.requests) - calls == 2
    manager._session_cache.save.assert_awaited_once()
    assert_original_data_retained(cached(module), before)
    assert owner is None


@pytest.mark.asyncio
async def test_recovery_rereads_after_acquiring_lease_and_uses_an_already_repaired_snapshot():
    manager, module, gateway, _, _, published, _, good = await ready_session(streaming=False)
    broken = deepcopy(good)
    broken['currentQuestion'] = None
    save_cache(module, broken)
    lease_manager = manager._agent_runtime.lease_manager
    acquire = lease_manager.try_acquire

    async def repair_before_lock_read(*args):
        lease = await acquire(*args)
        save_cache(module, good)
        return lease

    lease_manager.try_acquire = repair_before_lock_read
    calls = len(gateway.requests)
    assert await recover_then_query(manager, 'getSession') == published
    assert cached(module) == good
    assert len(gateway.requests) == calls
    manager._session_cache.save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['getSession', 'getHistory'])
@pytest.mark.parametrize(
    'case', ['unanswered', 'completed', 'creation', 'unsupported-flow', 'no-tail', 'blank-answer', 'chat']
)
async def test_queries_do_not_repair_snapshots_outside_the_missing_reply_contract(case, method):
    manager, module, gateway, _, _, _, _, snapshot = await ready_session(streaming=False)
    expected = None
    if case == 'completed':
        snapshot.update(status='completed', currentQuestion=None, completedAt='2026-01-01T10:00:00')
        snapshot['qaFlow'].update(phase='finished', finalMessage=flow.COMPLETION_MESSAGE)
    elif case == 'creation':
        snapshot.update(status='creating', currentQuestion=None)
        snapshot['qaCreation'] = {
            'stage': 'generating_question',
            'expiresAt': 9999999999,
            'nextAttemptAt': 9999999999,
        }
    elif case == 'unsupported-flow':
        snapshot['qaFlow']['version'] = 99
        snapshot['currentQuestion'] = None
        expected = module.QASessionConflictError
    elif case in ('no-tail', 'blank-answer', 'chat'):
        snapshot['currentQuestion'] = None
        expected = TypeError
        if case == 'no-tail':
            snapshot['qaHistory'] = []
        elif case == 'blank-answer':
            snapshot['qaHistory'][-1]['answer'] = ' \n\t '
        else:
            snapshot['type'] = 'chat'
    save_cache(module, snapshot)
    calls = len(gateway.requests)
    if expected:
        with pytest.raises(expected):
            await getattr(manager, method)('u', 's')
    else:
        result = await getattr(manager, method)('u', 's')
        assert result['status'] == {'completed': 'finished', 'creation': 'processing'}.get(case, 'question')
    assert cached(module) == snapshot
    assert len(gateway.requests) == calls
    manager._session_cache.save.assert_not_awaited()
    manager._session_repository.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_twentieth_saved_answer_recovers_all_pending_responses_and_finishes_without_an_extra_turn():
    manager, module, gateway, _, _, before = await missing_reply()
    while len(before['qaHistory']) < 20:
        item = deepcopy(before['qaHistory'][-1])
        item['question']['questionId'] = f'saved-final-{len(before["qaHistory"])}'
        item['question']['phase'] = 'qa'
        item['interactionCount'] = len(before['qaHistory']) + 1
        before['qaHistory'].append(item)
        before['answers'][item['question']['questionId']] = item['answer']
    before['qaFlow'].update(phase='qa', interactionCount=20, intakeCount=10)
    before['qaFlow']['concerns'] = [
        {
            'id': f'final-pending-{index}',
            'text': text,
            'evidence': text,
            'sourceQuestionId': before['qaHistory'][0]['question']['questionId'],
            'originPhase': 'supplement',
            'handledCount': 0,
            'status': 'pending',
            'response': '',
            'informationNeeded': '相关症状发生时间',
            'readyToAnswer': False,
        }
        for index, text in enumerate(('如何调整饮食', '能否继续散步', '是否需要补充益生菌'))
    ]
    save_cache(module, before)
    gateway.next_assessment = assessment(
        sufficient=False,
        concerns=[concern(item['text'], ready=False, identity=item['id']) for item in before['qaFlow']['concerns']],
    )
    calls = len(gateway.requests)
    final = await recover_then_query(manager, 'getSession')
    assert final['status'] == 'finished'
    assert final['interactionCount'] == 20
    assert final['endReason'] == 'interaction_limit'
    assert 'questionId' not in final
    saved = deepcopy(module.collection_sessions.document)
    assert module.redis_client.value is None
    assert saved['currentQuestion'] is None
    assert saved['qaHistory'] == before['qaHistory']
    assert saved['answers'] == before['answers']
    assert saved['qaFlow']['intakeCount'] == 10
    assert {key: saved[key] for key in CONTEXT_FIELDS if key != 'healthProfileContext'} == {
        key: before[key] for key in CONTEXT_FIELDS if key != 'healthProfileContext'
    }
    assert {item['id'] for item in saved['qaFlow']['concerns']} == {item['id'] for item in before['qaFlow']['concerns']}
    assert all(item['status'] == 'limited' and item['handledCount'] == 1 for item in saved['qaFlow']['concerns'])
    assert all(item['response'] in final['assistantMessage'] for item in saved['qaFlow']['concerns'])
    assert len(gateway.requests) - calls == 2
    manager._session_repository.save.assert_awaited_once()
    calls = len(gateway.requests)
    assert await manager.getSession('u', 's') == final
    history = await manager.getHistory('u', 's')
    assert history['status'] == 'finished'
    assert len(history['history']) == 20
    assert len(gateway.requests) == calls
    assert module.collection_sessions.document == saved
    manager._session_repository.save.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('pending_count', [1, 2], ids=['unique-target', 'ambiguous-target'])
async def test_published_qa_reply_is_reused_only_when_its_missing_target_is_unique(pending_count):
    manager, module, gateway, _ = setup_manager()
    current = await supplement(manager, gateway)
    texts = [f'希望进一步了解饮食相关问题{index + 1}' for index in range(pending_count)]
    answer = '；'.join(texts)
    gateway.next_assessment = assessment(intent='additional', concerns=[concern(text, ready=False) for text in texts])
    await submit(manager, current, answer)
    before = cached(module)
    published = deepcopy(before['qaFlow']['lastSubmission']['result'])
    before['currentQuestion'] = None
    save_cache(module, before)
    gateway.next_assessment = assessment(
        intent='additional',
        sufficient=False,
        concerns=[concern(item['text'], ready=False, identity=item['id']) for item in before['qaFlow']['concerns']],
    )
    manager._session_cache.save = AsyncMock(wraps=manager._session_cache.save)
    calls = len(gateway.requests)
    recovered = await recover_then_query(manager, 'getSession')
    saved = cached(module)
    assert recovered['phase'] == 'qa'
    assert recovered['interactionCount'] == 6
    assert recovered['intakeQuestionCount'] == 5 and recovered['qaRoundCount'] == 1
    assert saved['currentQuestion'].get('concernId') is None
    assert saved['qaFlow']['qaRoundCount'] == before['qaFlow']['qaRoundCount']
    assert saved['qaHistory'] == before['qaHistory']
    assert saved['answers'] == before['answers']
    assert [item['id'] for item in saved['qaFlow']['concerns']] == [item['id'] for item in before['qaFlow']['concerns']]
    assert recovered == published
    assert saved['qaFlow']['concerns'] == before['qaFlow']['concerns']
    assert len(gateway.requests) == calls
    manager._session_cache.save.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('pending_count', [0, 1, 2], ids=['none-pending', 'one-pending', 'two-pending'])
@pytest.mark.parametrize('clarify', [False, True], ids=['normal-invitation', 'clarify-invitation'])
async def test_published_fixed_qa_invitation_is_restored_without_turning_it_into_a_clarification(
    pending_count, clarify
):
    manager, module, gateway, _ = setup_manager()
    current = await supplement(manager, gateway)
    texts = ['饮食应该怎样安排', '运动要注意什么', '睡眠应该如何调整']
    answer = '；'.join(texts)
    gateway.next_assessment = assessment(intent='additional', concerns=[concern(texts[0])])
    published = await submit(manager, current, answer)
    last_answer = answer
    if clarify:
        gateway.next_assessment = assessment(intent='unclear', substantive=False, concerns=[], gaps=[])
        last_answer = '有'
        published = await submit(manager, published, last_answer)
    before = cached(module)
    assert published['phase'] == 'qa'
    assert published['questionType'] == 'radio' and published['options'] == [flow.NO_MORE_OPTION]
    assert published['question'] == with_answer_hint(CLARIFY_SUPPLEMENT_QUESTION if clarify else SUPPLEMENT_QUESTION)
    assert not any(item['status'] == 'pending' for item in before['qaFlow']['concerns'])
    for index in range(pending_count):
        before['qaFlow']['concerns'].append(
            {
                'id': f'earlier-pending-{index}',
                'text': texts[index + 1],
                'evidence': texts[index + 1],
                'sourceQuestionId': before['qaHistory'][5]['question']['questionId'],
                'originPhase': 'qa',
                'handledCount': 0,
                'status': 'pending',
                'response': '',
                'informationNeeded': '相关生活背景',
                'readyToAnswer': False,
            }
        )
    before['currentQuestion'] = None
    save_cache(module, before)
    manager._session_cache.save = AsyncMock(wraps=manager._session_cache.save)
    calls = len(gateway.requests)
    recovered = await recover_then_query(manager, 'getSession')
    saved = cached(module)
    assert recovered == published
    assert saved['currentQuestion']['questionId'] == published['questionId']
    assert saved['currentQuestion']['question'] == published['question']
    assert saved['currentQuestion'].get('concernId') is None
    assert recovered['intakeQuestionCount'] == 5 and recovered['qaRoundCount'] == 1
    assert saved['qaFlow']['qaVagueCount'] == int(clarify)
    assert saved['qaFlow']['concerns'] == before['qaFlow']['concerns']
    assert saved['qaHistory'] == before['qaHistory'] and saved['answers'] == before['answers']
    assert len(gateway.requests) == calls
    manager._session_cache.save.assert_awaited_once()
    assert await manager.getSession('u', 's') == published
    assert (
        await manager.submitAnswer('u', 's', before['qaHistory'][-1]['question']['questionId'], last_answer)
        == published
    )
    assert len(gateway.requests) == calls
    manager._session_cache.save.assert_awaited_once()
