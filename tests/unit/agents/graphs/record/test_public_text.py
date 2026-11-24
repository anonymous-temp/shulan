"""Operational diagnostics never escape through Record copy or legacy receipts."""

import hashlib
from copy import deepcopy

import pytest

from modules.record.application import flow
from modules.record.application.dialogue import CONTEXT_KEY
from modules.record.application.presentation import COMPLETION_MESSAGE
from modules.record.application.public_text import contains_internal_notice
from modules.record.application.public_text import public_assistant_message
from modules.record.application.public_text import public_history
from modules.record.application.public_text import public_response
from modules.record.application.public_text import strip_internal_notices
from modules.record.application.public_text import validate_internal_notice
from modules.record.application.validation import RecordValidationError

from .test_dynamic_record import cached
from .test_dynamic_record import save_cache
from .test_dynamic_record import submit
from .test_record_experience_v2 import experience_manager

NOTICES = (
    '部分已提交内容尚未完成可靠分析，已按原文保留。',
    '部分内容尚未完成可靠分析，您提交的原文会保留。',
    '这项问题暂时未能获得可靠回应，本次保留您的问题，不作推断。',
    '仍有部分问题暂时未能获得可靠回应，已保留为待回应的问题。',
    '尚未完成可靠分析的内容会按原文保留。',
    '您的回答已记录。部分内容暂时未能完成可靠分析或回应，已保留您提交的内容。',
    '暂时未能生成可靠的分析，我们可以先记录您希望说明的情况。',
    '本轮分析失败，已保存原始回答。',
    '本轮模型生成超时，问题已标记为待回答。',
    '部分内容尚未完成\n可靠分析，已按原文保留。',
    '未能可靠地分析您的回答。',
    '暂时无法处理您的回答。',
    '由于系统暂时无法响应，请稍后重试。',
    '系统出现错误。',
    '分析过程中出现错误。',
)
VALID = '先聊聊您平时的饮食。'


@pytest.mark.parametrize('notice', NOTICES)
@pytest.mark.parametrize('position', ['start', 'middle', 'end'])
def test_notices_are_rejected_and_removed_without_erasing_valid_copy(notice, position):
    text = {
        'start': notice + VALID,
        'middle': VALID + notice + '也聊聊日常活动。',
        'end': VALID + notice,
    }[position]
    with pytest.raises(RecordValidationError, match='对话正文不得'):
        validate_internal_notice(text)
    result = strip_internal_notices(text)
    assert result == VALID + ('也聊聊日常活动。' if position == 'middle' else '')
    assert not contains_internal_notice(result)
    assert strip_internal_notices(result) == result


@pytest.mark.parametrize(
    'text',
    [
        '家族背景并不意味着一定患病。血糖波动可能让人感到疲劳。',
        '请保留检查报告，下次复诊时带给医生。',
        '运动安排可以保留休息日。',
        '手术失败风险与手术类型相关。',
        '一线系统治疗失败后，医生可能调整治疗方案。',
        '如果牙齿修复失败，需要由口腔科评估。',
        '修复失败后，需要由口腔科评估。',
        '伤口处理失败可能需要重新评估。',
        '神经系统出现异常需要结合检查评估。',
        '饮食习惯不能直接确定病因。',
    ],
)
def test_ordinary_medical_copy_is_unchanged(text):
    validate_internal_notice(text)
    assert strip_internal_notices(text) == text


def test_clause_filter_keeps_same_sentence_valid_explanation_and_handles_decoration():
    text = '部分内容尚未完成**可靠分析**，规律进餐可减少长时间空腹。'
    assert strip_internal_notices(text) == '规律进餐可减少长时间空腹。'
    text = '规律进餐可减少长时间空腹，原文已保留。'
    assert strip_internal_notices(text) == '规律进餐可减少长时间空腹。'


def test_empty_copy_uses_theme_or_completion_without_changing_non_prose_fields():
    assert (
        public_assistant_message(NOTICES[0], question={'intakeDimension': 'family_history'})
        == '接着聊聊家里人的健康情况。'
    )
    assert public_assistant_message(NOTICES[0], completed=True) == COMPLETION_MESSAGE
    raw = {
        'status': 'finished',
        'assistantMessage': NOTICES[0],
        'reasoning': NOTICES[1],
        'documentProcessing': [{'message': NOTICES[2]}],
        'endReason': 'qa_round_limit',
    }
    original = deepcopy(raw)
    result = public_response(raw)
    assert raw == original
    assert result['assistantMessage'] == COMPLETION_MESSAGE
    assert result['reasoning'] == raw['reasoning']
    assert result['documentProcessing'] == raw['documentProcessing']
    assert result['endReason'] == raw['endReason']
    for status in ['failed', 'creating']:
        failure = {**raw, 'status': status}
        assert public_response(failure) == failure


def test_history_projection_leaves_user_quotes_evidence_and_original_snapshots_unchanged():
    raw = [
        {
            'question': {
                'questionId': 'q1',
                'question': '平时如何活动？' + NOTICES[0],
                'assistantMessage': VALID + NOTICES[1],
                'options': ['步行'],
                'reasoning': NOTICES[0],
                'dialogueFocusRef': 'internal',
            },
            'answer': {'content': NOTICES[0]},
            'evidence': NOTICES[1],
            'answeredAt': 'same',
        }
    ]
    original = deepcopy(raw)
    result = public_history(raw)
    assert result[0]['answer'] == original[0]['answer']
    assert result[0]['evidence'] == original[0]['evidence']
    assert result[0]['question']['assistantMessage'] == VALID
    assert result[0]['question']['question'] == '平时如何活动？'
    assert result[0]['question']['reasoning'] == NOTICES[0]
    assert result[0]['question']['options'] == ['步行']
    assert 'dialogueFocusRef' not in result[0]['question']
    assert raw == original


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_old_state_history_and_receipt_replay_hide_notices_without_writes_or_calls(streaming):
    manager, module, gateway, _ = experience_manager()
    first = await manager.createSession('u', 's')
    raw_answer = NOTICES[0]
    current = await submit(manager, first, raw_answer)
    snapshot = cached(module)
    for question in [snapshot['currentQuestion'], snapshot['qaHistory'][0]['question']]:
        question['assistantMessage'] = VALID + NOTICES[0]
        question['question'] += NOTICES[1]
    receipt = snapshot['qaFlow']['lastSubmission']['result']
    receipt['assistantMessage'] = VALID + NOTICES[0]
    receipt['question'] += NOTICES[1]
    save_cache(module, snapshot)
    before = deepcopy(cached(module))
    calls = len(gateway.requests)
    restored = await manager.getSession('u', 's')
    history = await manager.getHistory('u', 's')
    replayed = await submit(manager, first, raw_answer, streaming=streaming)
    for response in [restored, replayed, *history['history']]:
        assert not contains_internal_notice(response['assistantMessage'])
        assert not contains_internal_notice(response['question'])
    assert restored['questionId'] == current['questionId']
    assert replayed['questionId'] == current['questionId']
    assert replayed['options'] == current['options']
    assert history['history'][0]['answer'] == raw_answer
    assert (
        before['qaFlow']['lastSubmission']['answerHash']
        == hashlib.sha256(flow.answer_text(raw_answer).encode()).hexdigest()
    )
    assert cached(module) == before
    assert len(gateway.requests) == calls


@pytest.mark.asyncio
async def test_expression_references_clean_only_ai_copy_in_every_context_channel():
    manager, module, gateway, _ = experience_manager()
    first = await manager.createSession('u', 's')
    await submit(manager, first, NOTICES[0])
    previous = cached(module)
    for question in [previous['currentQuestion'], previous['qaHistory'][0]['question']]:
        question.update(assistantMessage=VALID + NOTICES[0], acknowledgementText=VALID + NOTICES[0])
    previous['qaFlow']['concerns'] = [
        {
            'id': 'old',
            'text': '家族背景有什么影响',
            'evidence': NOTICES[0],
            'status': 'limited',
            'response': VALID + NOTICES[1],
            'responseHistory': [NOTICES[0]],
        }
    ]
    original = deepcopy(previous)
    patient = manager._presentationContext(previous, previous, NOTICES[0], previous['qaFlow'].get('focus'))
    assert patient['本次回答'] == NOTICES[0]
    assert patient['历史问答'][0]['用户回答'] == NOTICES[0]
    assert patient['已有疑问'][0]['用户原话'] == NOTICES[0]
    assert patient['已有疑问'][0]['已有回应'] == VALID
    assert patient['已展示说明'] == [VALID]
    assert [item['答复'] for item in patient['已展示答复']] == [VALID]
    reference = patient[CONTEXT_KEY]
    assert reference
    instruction = flow._response_instruction(previous['qaFlow']['concerns'][0], final=True)
    assert instruction['previousResponses'] == [VALID]
    assert previous == original
