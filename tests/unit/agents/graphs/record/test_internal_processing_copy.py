"""Processing failures stay internal through validation, repair and fallback."""

import json
from copy import deepcopy

import pytest

from modules.record.application import fallback
from modules.record.application import flow
from modules.record.application.output_repair import PresentationRepair
from modules.record.application.presentation import validate_acknowledgement_style
from modules.record.application.public_text import contains_internal_notice
from modules.record.application.validation import RecordValidationError

from .test_answer_entry_instructions import patient
from .test_answer_entry_instructions import render_decision
from .test_answer_entry_instructions import safe_payload
from .test_conservative_fallback import concern
from .test_conservative_fallback import quality
from .test_conservative_fallback import session
from .test_conservative_fallback import two_fallbacks

NOTICE = '部分已提交内容尚未完成可靠分析，已按原文保留。'
GOOD_RESPONSE = '日常活动可以从短时间步行开始，逐步形成自己能坚持的安排。'


def payload_with_notice(component):
    payload = safe_payload()
    payload['responses'][0]['text'] = GOOD_RESPONSE
    if component == 'question':
        payload['question']['question'] += NOTICE
    elif component == 'response':
        payload['responses'][0]['text'] += NOTICE
    else:
        payload['assistantMessage'][component] = NOTICE
    return payload


@pytest.mark.parametrize('component', ['acknowledgement', 'rationale', 'question', 'response'])
def test_final_publication_rejects_processing_notice_in_each_component(component):
    before = session(count=3, concerns=[concern('a')])
    original = deepcopy(before)
    with pytest.raises(RecordValidationError) as caught:
        flow.apply_presentation(before, render_decision(), flow.TurnPresentation(**payload_with_notice(component)), '')
    assert caught.value.code == 'internal_processing_notice_forbidden'
    assert before == original


@pytest.mark.parametrize('field', ['acknowledgement', 'rationale'])
def test_unused_finishing_bridge_cannot_bypass_processing_notice_check(field):
    values = {'acknowledgement': '', 'rationale': '', 'evidence': []}
    values[field] = NOTICE
    with pytest.raises(RecordValidationError) as caught:
        validate_acknowledgement_style(flow.PatientMessage(**values), action='finish', has_responses=True)
    assert caught.value.code == 'internal_processing_notice_forbidden'


@pytest.mark.parametrize('component', ['acknowledgement', 'rationale', 'question', 'response'])
def test_partial_repair_keeps_valid_components_and_accepts_notice_free_replacement(component):
    collector = PresentationRepair(render_decision(), patient())
    assert collector.ingest(json.dumps(payload_with_notice(component), ensure_ascii=False)) is None
    retained = collector.partial_presentation
    if component == 'question':
        assert retained['question'] is None
        assert retained['assistantMessage'] is not None
        assert retained['responses'][0].text == GOOD_RESPONSE
    elif component == 'response':
        assert retained['responses'] == []
        assert retained['question'] is not None
        assert retained['assistantMessage'] is not None
    else:
        assert retained['assistantMessage'] is None
        assert retained['question'] is not None
        assert retained['responses'][0].text == GOOD_RESPONSE
    replacement = safe_payload()
    replacement['responses'][0]['text'] = GOOD_RESPONSE
    repaired = collector.ingest(json.dumps(replacement, ensure_ascii=False))
    assert repaired is not None
    assert not contains_internal_notice(repaired.model_dump_json())


@pytest.mark.parametrize('component', ['acknowledgement', 'rationale', 'question', 'response'])
def test_failed_repair_fallback_drops_notice_only_and_preserves_unanswered_status(component):
    before = session(count=3, concerns=[concern('a')])
    original = deepcopy(before)
    partial = flow.TurnPresentation(**payload_with_notice(component))
    instructions = render_decision()
    instructions['target'].update(topic='日常活动', aspect='habits', scope='general', complaintIds=[])
    result = fallback.build_fallback(
        before,
        answer='平时会散步',
        validated_draft=deepcopy(before),
        decision=instructions,
        partial_presentation=partial,
        presentation_patient=patient(),
    )
    question = result['currentQuestion']
    assert not contains_internal_notice(question['assistantMessage'])
    assert not contains_internal_notice(question['question'])
    saved = result['qaFlow']['concerns'][0]
    if component == 'response':
        assert (saved['status'], saved['response'], saved['handledCount']) == ('pending', '', 0)
    else:
        assert GOOD_RESPONSE in question['assistantMessage']
        assert (saved['status'], saved['response'], saved['handledCount']) == ('answered', GOOD_RESPONSE, 1)
    assert before == original


def test_finishing_keeps_legacy_partial_response_raw_but_publishes_only_usable_copy():
    before = two_fallbacks()
    stored_response = GOOD_RESPONSE + '\n' + NOTICE
    before['qaFlow']['concerns'][0].update(response=stored_response, responseStyleVersion=flow.RESPONSE_STYLE_VERSION)
    original = deepcopy(before)
    result = fallback.handle_control_answer(before, fallback.FINISH_OPTION)
    saved = result['qaFlow']['concerns'][0]
    assert saved['response'] == stored_response
    assert saved['status'] == 'pending' and saved['handledCount'] == 0
    assert result['qaFlow']['finalMessage'] == GOOD_RESPONSE + '\n\n' + flow.COMPLETION_MESSAGE
    assert before == original


def test_successful_end_does_not_reinsert_notices_for_prior_unprocessed_answers():
    draft = two_fallbacks()
    draft['qaFlow'].update(phase='finished', finalMessage=flow.COMPLETION_MESSAGE)
    pending_ids = deepcopy(quality(draft)['unprocessedQuestionIds'])
    fallback.mark_generation_success(draft, repaired=True)
    assert draft['qaFlow']['finalMessage'] == flow.COMPLETION_MESSAGE
    assert quality(draft)['unprocessedQuestionIds'] == pending_ids
    assert draft['qaFlow']['concerns'][0]['status'] == 'pending'


def test_consecutive_failures_and_continue_controls_use_only_natural_dialogue():
    failed = two_fallbacks()
    current = failed['currentQuestion']
    assert '您希望先结束本次建档，还是继续补充？' in current['question']
    assert not contains_internal_notice(current['question'])
    assert not contains_internal_notice(current['assistantMessage'])
    resumed = fallback.handle_control_answer(failed, fallback.CONTINUE_OPTION)
    assert resumed['currentQuestion']['assistantMessage'] == '接着聊聊家里人的健康情况。'
    assert quality(resumed)['unprocessedQuestionIds'] == quality(failed)['unprocessedQuestionIds']
