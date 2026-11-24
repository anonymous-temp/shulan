"""Product-level Record bounds and stage ownership, using no external services."""

import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from modules.record.application import flow
from modules.record.application import intake
from modules.record.application.output_repair import AnalysisRepair
from modules.record.application.validation import RecordValidationError
from tests.unit._support.record_policy import short_interview_policy

from .test_dynamic_record import assessment
from .test_dynamic_record import cached
from .test_dynamic_record import concern
from .test_dynamic_record import save_cache
from .test_dynamic_record import setup_manager
from .test_dynamic_record import submit
from .test_dynamic_record import supplement

LIFESTYLE = {'diet_structure', 'meal_pattern', 'hydration', 'activity', 'sleep', 'weight_change', 'habits'}


def snapshot():
    legacy = flow.new_flow()
    legacy['interviewPolicy'] = short_interview_policy()
    return {'userId': 'u', 'sessionId': 's', 'qaFlow': legacy, 'qaHistory': [], 'currentQuestion': None}


def lifestyle_gap(dimension, *, essential=False):
    return {
        'topic': intake.DIMENSION_LABELS[dimension],
        'dimension': dimension,
        'scope': 'general',
        'reason': '了解与本次目标相关的生活情况',
        'informationNeeded': intake.DIMENSION_LABELS[dimension],
        'essential': essential,
    }


def step(
    previous,
    answer=None,
    *,
    sufficient=False,
    gaps=(),
    facts=(),
    concerns=(),
    intent='answer',
    substantive=True,
    sources=None,
    complaints=(),
):
    parsed = flow.PatientAssessment(
        **assessment(
            sufficient=sufficient,
            gaps=list(gaps),
            concerns=concerns,
            intent=intent,
            substantive=bool(answer is not None and substantive),
            intake={'complaints': list(complaints), 'priorityClarificationNeeded': False, 'facts': list(facts)},
        )
    )
    original = deepcopy(previous)
    flow.validate_assessment(parsed, previous, answer, sources=sources)
    draft, decision = flow.advance_flow(previous, parsed, answer=answer)
    assert previous == original
    question = None
    if decision['action'] not in {'finish', 'invite_supplement', 'clarify_supplement'}:
        question = {
            'question': '这方面您还有哪些补充情况？',
            'questionType': 'radio',
            'options': ['没有需要补充的内容', '目前记不清'],
        }
    presentation = flow.TurnPresentation(
        assistantMessage={'acknowledgement': '生活中的不同安排值得结合起来了解。', 'rationale': '', 'evidence': []},
        responses=[
            {
                'concernId': item['concernId'],
                'text': (
                    '饮食安排可以从食物搭配做起，尽量保持三餐时间稳定。',
                    '日常活动可以从步行开始，逐步形成适合自己的固定安排。',
                    '休息时可以留出放松时间，尽量保持稳定的睡眠作息。',
                )[index % 3],
            }
            for index, item in enumerate(decision['responses'])
        ],
        question=question,
    )
    flow.apply_presentation(draft, decision, presentation, '')
    if answer is not None:
        draft['qaHistory'].append({'question': deepcopy(previous['currentQuestion']), 'answer': answer})
    return draft, decision


def intake_to_qa(*, length=5, pending=()):
    state, _ = step(snapshot())
    for number in range(1, length + 1):
        state, decision = step(
            state,
            '我希望了解日常生活如何安排',
            sufficient=number >= length,
            gaps=[lifestyle_gap(dimension) for dimension in ('diet_structure', 'activity', 'sleep')],
            concerns=pending
            if number == 1
            else [
                concern(item['text'], identity=item['id'], ready=item.get('readyToAnswer', True))
                for item in state['qaFlow']['concerns']
                if item['status'] == 'pending'
            ],
        )
    return state, decision


def test_first_three_slots_keep_their_order_when_answer_already_covers_both_histories():
    state, first = step(snapshot())
    assert first['target']['dimension'] == 'goal'
    answer = '这次想改善睡眠，过去有高血压，父亲也有高血压'
    facts = [
        {
            'dimension': dimension,
            'scope': 'general',
            'status': 'known',
            'summary': answer,
            'evidence': [{'source': '本次回答', 'quote': answer}],
        }
        for dimension in ('medical_history', 'family_history')
    ]
    state, second = step(state, answer, facts=facts)
    assert second['target']['dimension'] == 'medical_history'
    assert second['target']['supplementation'] and second['target']['knownFacts']
    state, third = step(state, '没有其他补充')
    assert third['target']['dimension'] == 'family_history'
    assert third['target']['supplementation'] and third['target']['knownFacts']
    assert state['qaFlow']['intakeQuestionCount'] == 2


@pytest.mark.parametrize('source', ['本次报告', '长期画像'])
def test_existing_history_keeps_its_source_and_changes_the_fixed_history_slot_to_a_supplement(source):
    history = '既往由医生诊断高血压，目前规律随访'
    fact = {
        'dimension': 'medical_history',
        'scope': 'general',
        'status': 'known',
        'summary': history,
        'evidence': [{'source': source, 'quote': history}],
    }
    state, first = step(snapshot(), facts=[fact], sources={'长期健康画像' if source == '长期画像' else source: history})
    assert first['target']['dimension'] == 'goal'
    state, second = step(state, '这次想了解如何安排运动')
    assert second['target']['dimension'] == 'medical_history'
    assert second['target']['supplementation']
    assert second['target']['knownFacts'][0]['evidence'][0]['source'] == source


@pytest.mark.parametrize('length', [4, 5, 6])
def test_normal_intake_is_four_to_six_slots_and_later_slots_are_lifestyle_only(length):
    state, decision = step(snapshot())
    topics = [decision['target']['dimension']]
    for number in range(1, length + 1):
        state, decision = step(
            state,
            '目前没有其他补充',
            sufficient=number >= length,
            gaps=[]
            if length == 4 and number == length
            else [lifestyle_gap(dimension, essential=True) for dimension in ('diet_structure', 'activity', 'sleep')],
        )
        if state['qaFlow']['phase'] == 'intake':
            topics.append(decision['target']['dimension'])
        else:
            assert number == length
    assert topics[:3] == ['goal', 'medical_history', 'family_history']
    assert 1 <= len(topics[3:]) <= 3
    assert set(topics[3:]) <= LIFESTYLE
    assert len(topics[3:]) == len(set(topics[3:]))
    assert state['qaFlow']['phase'] == 'supplement'
    assert state['qaFlow']['intakeQuestionCount'] == length
    assert state['qaFlow']['qaRoundCount'] == 0


def test_symptom_candidates_cannot_reopen_symptom_investigation_after_the_fixed_slots():
    state, _ = step(snapshot())
    for number, answer in enumerate(('想了解餐后不适', '没有已知疾病', '没有已知家族病史')):
        known = state['qaFlow']['intake']['complaints']
        state, decision = step(
            state,
            answer,
            gaps=[
                {
                    'topic': '不适细节',
                    'dimension': 'onset',
                    'scope': 'complaint',
                    'complaintIndexes': [0],
                    'reason': '模型提出了不允许的追问',
                    'informationNeeded': '症状出现的时间',
                    'essential': True,
                },
                lifestyle_gap('activity'),
            ],
            complaints=[
                {
                    'id': known[0]['id'] if known else None,
                    'text': '餐后不适',
                    'evidence': [{'source': '本次回答' if number == 0 else '历史回答', 'quote': '想了解餐后不适'}],
                }
            ],
        )
    assert decision['target']['dimension'] in LIFESTYLE
    assert decision['target']['dimension'] != 'onset'


def test_pending_intake_questions_are_answered_at_qa_entry_without_consuming_a_qa_round():
    state, _ = step(snapshot())
    question_text = '饮食应该怎么调整'
    state, decision = step(state, question_text, concerns=[concern(question_text)])
    assert decision['responses'] == []
    for number in range(2, 6):
        pending = [
            concern(item['text'], identity=item['id'])
            for item in state['qaFlow']['concerns']
            if item['status'] == 'pending'
        ]
        state, decision = step(
            state,
            '继续了解我的生活情况',
            sufficient=number >= 5,
            gaps=[lifestyle_gap(d) for d in ('diet_structure', 'activity', 'sleep')],
            concerns=pending,
        )
    assert state['qaFlow']['phase'] == 'supplement'
    assert decision['responses'] and state['qaFlow']['preQaAnswered']
    assert state['qaFlow']['qaRoundCount'] == 0


def test_multiple_questions_and_clarification_each_consume_one_qa_round_and_fifth_round_finishes():
    state, _ = intake_to_qa()
    for number in range(1, 6):
        texts = [f'第{number}次想问饮食怎么安排', f'第{number}次也想问运动怎么安排']
        existing = [
            concern(item['text'], identity=item['id'])
            for item in state['qaFlow']['concerns']
            if item['status'] == 'pending'
        ]
        state, decision = step(
            state, '；'.join(texts), concerns=[*existing, *[concern(text, ready=number != 2) for text in texts]]
        )
        assert state['qaFlow']['qaRoundCount'] == number
        if number < 5:
            assert state['qaFlow']['phase'] in {'qa', 'supplement'}
        else:
            assert state['qaFlow']['phase'] == 'finished'
            assert decision['action'] == 'finish'
            assert state['currentQuestion'] is None
            assert all(item['status'] in {'answered', 'limited'} for item in state['qaFlow']['concerns'])


def test_two_vague_qa_answers_finish_and_an_actual_question_resets_the_streak():
    state, _ = intake_to_qa()
    state, _ = step(state, '嗯', intent='unclear', substantive=False)
    assert state['qaFlow']['qaVagueCount'] == 1 and state['qaFlow']['qaRoundCount'] == 0
    text = '日常运动应该怎么安排'
    state, _ = step(state, text, concerns=[concern(text)])
    assert state['qaFlow']['qaVagueCount'] == 0 and state['qaFlow']['qaRoundCount'] == 1
    state, _ = step(state, '先这样吧我还没想好', intent='unclear', substantive=False)
    assert state['qaFlow']['qaVagueCount'] == 1
    state, decision = step(state, '嗯嗯', intent='unclear', substantive=False)
    assert state['qaFlow']['phase'] == 'finished'
    assert decision['action'] == 'finish' and state['qaFlow']['qaRoundCount'] == 1


@pytest.mark.parametrize('option', ['暂不想回答', '不方便说', '跳过这一题', '暂时不想补充'])
def test_generated_choices_cannot_offer_refusal_or_skip_shortcuts(option):
    question = flow.GeneratedQuestion(
        question='您的生活作息通常如何？', questionType='radio', options=['作息规律', option]
    )
    with pytest.raises(RecordValidationError):
        flow.validate_new_question(question, action='ask_intake')


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_qa_retry_and_status_history_keep_new_counters_stable(streaming):
    manager, module, gateway, _ = setup_manager()
    current = await supplement(manager, gateway, streaming=streaming)
    assert current['intakeQuestionCount'] == 5 and current['qaRoundCount'] == 0
    text = '日常饮食应该如何安排'
    gateway.next_assessment = assessment(concerns=[concern(text)], gaps=[])
    question_id = current['questionId']
    result = await submit(manager, current, text, streaming=streaming)
    assert result['qaRoundCount'] == 1
    calls = len(gateway.requests)
    repeated = await manager.submitAnswer('u', 's', question_id, text)
    assert repeated == {key: value for key, value in result.items() if key != 'type'}
    status = await manager.getSession('u', 's')
    await manager.getHistory('u', 's')
    assert len(gateway.requests) == calls
    assert status['intakeQuestionCount'] == 5 and status['qaRoundCount'] == 1
    assert cached(module)['qaFlow']['intakeQuestionCount'] == 5
    assert cached(module)['qaFlow']['qaRoundCount'] == 1


async def legacy_qa_after_four_replies():
    manager, module, gateway, _ = setup_manager()
    current = await supplement(manager, gateway)
    for number in range(4):
        text = f'请解释第{number + 1}项日常饮食问题'
        gateway.next_assessment = assessment(intent='additional', concerns=[concern(text)])
        current = await submit(manager, current, text)
    stored = cached(module)
    stored['qaFlow']['phase'] = 'supplement'
    stored['currentQuestion']['phase'] = 'supplement'
    for entry in stored['qaHistory'][5:]:
        entry['question']['phase'] = 'supplement'
    for field in ('qaRoundCount', 'qaVagueCount', 'intakeQuestionCount', 'preQaAnswered'):
        stored['qaFlow'].pop(field)
    save_cache(module, stored)
    return manager, module, gateway, deepcopy(stored), current


@pytest.mark.asyncio
async def test_legacy_supplement_qa_history_keeps_four_used_replies_and_finishes_on_fifth():
    manager, module, gateway, before, current = await legacy_qa_after_four_replies()
    text = '再解释一下运动应该怎样安排'
    gateway.next_assessment = assessment(intent='additional', concerns=[concern(text)])
    result = await submit(manager, current, text)
    assert result['status'] == 'finished' and result['endReason'] == 'qa_round_limit'
    assert result['qaRoundCount'] == 5 and result['intakeQuestionCount'] == 5
    saved = module.collection_sessions.document
    assert saved['qaHistory'][:-1] == before['qaHistory']
    assert saved['qaHistory'][-1]['answer'] == text
    assert len(saved['qaHistory']) == 10


@pytest.mark.asyncio
async def test_status_infers_legacy_counters_without_writing_snapshot_or_refreshing_ttl():
    manager, module, gateway, before, _ = await legacy_qa_after_four_replies()
    calls = len(gateway.requests)
    manager._session_cache.save = AsyncMock(wraps=manager._session_cache.save)
    manager._session_repository.save = AsyncMock(wraps=manager._session_repository.save)
    module.redis_client.setex = AsyncMock(wraps=module.redis_client.setex)
    status = await manager.getSession('u', 's')
    assert status['qaRoundCount'] == 4 and status['intakeQuestionCount'] == 5
    assert cached(module) == before
    assert len(gateway.requests) == calls
    manager._session_cache.save.assert_not_awaited()
    manager._session_repository.save.assert_not_awaited()
    module.redis_client.setex.assert_not_awaited()


def test_additional_intent_without_an_actual_question_does_not_spend_a_qa_reply():
    state, _ = intake_to_qa()
    state, decision = step(state, '还有一些情况但没有具体问题', intent='additional', substantive=True)
    assert state['qaFlow']['qaRoundCount'] == 0
    assert state['qaFlow']['qaVagueCount'] == 1
    assert decision['action'] == 'clarify_supplement'


def test_repair_removes_historical_answered_questions_without_turning_a_vague_answer_into_a_new_qa_round():
    state, _ = intake_to_qa()
    text = '应该怎样安排运动'
    state, _ = step(state, text, intent='additional', concerns=[concern(text, ready=False)])
    assert state['qaFlow']['qaRoundCount'] == 1
    answered = state['qaFlow']['concerns'][0]
    repair = AnalysisRepair(state, '嗯', source_text='嗯')
    repaired = repair.ingest(
        json.dumps(
            assessment(
                intent='additional',
                substantive=False,
                gaps=[],
                concerns=[concern(answered['text'], ready=False, identity=answered['id'])],
            ),
            ensure_ascii=False,
        )
    )
    assert repaired is not None and repaired.concerns == []
    assert repair.diagnostics['invalidCounts']['concerns'] == 1
    state, decision = step(
        state,
        '嗯',
        intent=repaired.intent,
        substantive=repaired.substantive,
        concerns=[item.model_dump() for item in repaired.concerns],
    )
    assert state['qaFlow']['qaRoundCount'] == 1
    assert state['qaFlow']['qaVagueCount'] == 1
    assert decision['action'] == 'clarify_supplement'
    assert state['qaFlow']['concerns'][0]['status'] == 'limited'
