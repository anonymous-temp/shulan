"""Record AI-copy boundary; never apply this projection to patient evidence.

Operational failures belong to internal state and error responses. Legacy
snapshots remain immutable: every public projection below returns a copy.
"""

import re
from copy import deepcopy

from modules.record.application.validation import RecordValidationError

_CLAUSE_END = r'[^，,。！？!?；;\n]'
_SYSTEM_SUBJECT = r'(?:^|[，,。！？!?；;\n])(?:由于|因为|目前|当前|本轮|本次)?(?:系统|模型|AI)'
_NOTICE = re.compile(
    rf'(?:尚未|还未|暂未|未能|未完成|未成功|无法|不能|未获|没有得到){_CLAUSE_END}{{0,18}}'
    r'(?:(?:可靠|有效)(?:的|地)?(?:分析|回应|答复|回答)|完成分析|完成解析|完成处理)'
    rf'|(?:未能|无法|暂未)(?:完成|进行|完整)?(?:分析|解析|处理){_CLAUSE_END}{{0,6}}(?:您的回答|提交内容|用户输入)'
    rf'|{_SYSTEM_SUBJECT}(?:调用|生成|分析|解析|校验|响应|处理|修复|运行|服务)'
    r'(?:暂时|目前|出现)?(?:失败|超时|尚未完成|未完成)'
    rf'|{_SYSTEM_SUBJECT}(?:暂时|目前|仍|还)?(?:无法|未能)(?:响应|返回|处理|分析|生成)'
    rf'|{_SYSTEM_SUBJECT}(?:出现|发生)(?:错误|故障|异常)'
    r'|(?:分析|解析|生成|校验)(?:过程(?:中)?|期间)(?:出现|发生)(?:错误|异常|故障)'
    r'|(?:本轮|本次|这次|内部|已提交内容|您的回答|回答|问题|资料|报告|附件|文档)(?:的)?'
    r'(?:分析|解析|生成|校验|修复|处理)(?:暂时|目前|出现|发生|仍|尚)?(?:失败|超时|未完成)'
    r'|(?:^|[，,。！？!?；;\n])(?:分析|解析|生成|校验)(?:暂时|目前|出现|发生|仍|尚)?(?:失败|超时|未完成)'
    rf'|(?:原文|原始回答|原始内容){_CLAUSE_END}{{0,12}}(?:保留|保存)'
    rf'|(?:保留|保存){_CLAUSE_END}{{0,15}}(?:原文|原始回答|原始内容|待回应|待回答|待处理)'
    rf'|(?:保留|保存){_CLAUSE_END}{{0,8}}(?:您提交|已提交|已经提交)(?:的)?(?:内容|回答)'
    rf'|(?:已提交|您提交|已经提交){_CLAUSE_END}{{0,10}}(?:保留|保存)'
    rf'|(?:保留|列为|标记|记录|转为){_CLAUSE_END}{{0,10}}待(?:回应|回答|处理)'
    rf'|(?:稍后|后续|等待){_CLAUSE_END}{{0,12}}(?:重新分析|补偿处理|重试)'
)
_NOTICE_COMPANION = re.compile(
    r'^(?:本次|我们会|会|已|将|暂时)?(?:保留您的问题|保留您的回答|不作推断|不做推断|无需重复发送|您无需重复发送'
    r'|您的回答已记录|我们可以先记录您希望说明的情况)[。\s]*$'
)
_DECORATION = re.compile(r'[\s*_`\u200b\ufeff]+')


def contains_internal_notice(text: str) -> bool:
    """Recognize operational notices, not ordinary medical uncertainty."""
    return bool(isinstance(text, str) and _NOTICE.search(_DECORATION.sub('', text)))


def validate_internal_notice(text: str) -> None:
    if contains_internal_notice(text):
        raise RecordValidationError(
            'internal_processing_notice_forbidden',
            '对话正文不得描述分析、修复或生成失败、可靠回应缺失、原文保留或待处理状态；直接保留有效解释或自然引入当前话题',
        )


def strip_internal_notices(text: str) -> str:
    """Remove diagnostic clauses while retaining adjacent usable prose."""
    if not contains_internal_notice(text):
        return text
    # Match across formatting/newlines, but only join line breaks *inside* a
    # detected notice. Other paragraphs and useful adjacent prose stay intact.
    offsets = [index for index, char in enumerate(text) if not _DECORATION.fullmatch(char)]
    normalized = ''.join(text[index] for index in offsets)
    joined_breaks = {
        index
        for match in _NOTICE.finditer(normalized)
        for index in range(offsets[match.start()], offsets[match.end() - 1] + 1)
        if text[index] in '\r\n'
    }
    if joined_breaks:
        text = ''.join(char for index, char in enumerate(text) if index not in joined_breaks)
    output = []
    for match in re.finditer(r'([^。！？!?；;\n]+)([。！？!?；;]*)|(\n+)', text):
        body, stop, newline = match.groups()
        if newline:
            output.append(newline)
            continue
        if _NOTICE_COMPANION.fullmatch(_DECORATION.sub('', body)):
            continue
        if not contains_internal_notice(body):
            output.append(body + stop)
            continue
        clauses = re.findall(r'([^，,]+)([，,]?)', body)
        retained = [
            (clause, separator)
            for clause, separator in clauses
            if not contains_internal_notice(clause) and not _NOTICE_COMPANION.fullmatch(_DECORATION.sub('', clause))
        ]
        if retained:
            output.append(''.join(clause + sep for clause, sep in retained[:-1]) + retained[-1][0] + stop)
    return re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+', '\n\n', ''.join(output)).strip()


def public_assistant_message(text: str, *, flow=None, question=None, completed=False) -> str:
    cleaned = strip_internal_notices(text)
    if cleaned or not text:
        return cleaned
    # Lazy imports keep the validation primitive usable from presentation.py.
    from modules.record.application.presentation import COMPLETION_MESSAGE
    from modules.record.application.presentation import default_acknowledgement

    if completed:
        return COMPLETION_MESSAGE
    context = dict(flow or {})
    question = question or {}
    if question.get('phase'):
        context['phase'] = question['phase']
    if question.get('intakeDimension'):
        context['focus'] = {'dimension': question['intakeDimension']}
    return default_acknowledgement(context)


def public_question(question: dict, *, flow=None) -> dict:
    result = deepcopy(question)
    if isinstance(result.get('question'), str):
        result['question'] = public_question_text(result['question'], question=question)
    for key in ('acknowledgementText', 'rationale'):
        if isinstance(result.get(key), str):
            result[key] = strip_internal_notices(result[key])
    if 'assistantMessage' in result:
        result['assistantMessage'] = public_assistant_message(result['assistantMessage'], flow=flow, question=question)
    result.pop('dialogueFocusRef', None)
    return result


def public_question_text(text: str, *, question=None) -> str:
    cleaned = strip_internal_notices(text)
    if cleaned or not text:
        return cleaned
    question = question or {}
    if question.get('options') == ['没有了']:
        from modules.record.application.presentation import SUPPLEMENT_QUESTION

        return SUPPLEMENT_QUESTION
    if question.get('options') == ['先结束本次建档', '继续补充']:
        return '您希望先结束本次建档，还是继续补充？'
    return '以下哪些情况符合您的实际情况？' if question.get('options') else '请说说您的实际情况。'


def public_history(history: list) -> list:
    result = deepcopy(history)
    if not isinstance(result, list):
        return result
    for item in result:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get('question'), dict):
            item['question'] = public_question(item['question'])
        elif isinstance(item.get('question'), str):
            item['question'] = public_question_text(item['question'], question=item)
        if isinstance(item.get('assistantMessage'), str):
            item['assistantMessage'] = public_assistant_message(item['assistantMessage'], question=item)
    return result


def public_response(response: dict, *, flow=None, question=None) -> dict:
    """Project successful Record envelopes, including saved submission receipts."""
    result = deepcopy(response)
    if result.get('status') not in ('question', 'finished'):
        return result
    if isinstance(result.get('question'), str):
        result['question'] = public_question_text(result['question'], question=question or result)
    if 'assistantMessage' in result:
        result['assistantMessage'] = public_assistant_message(
            result['assistantMessage'],
            flow=flow or result,
            question=question or result,
            completed=result['status'] == 'finished',
        )
    return result
