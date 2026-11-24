"""Buttons submit answers; only the existing text and voice paths accept extra input."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from modules.record.application import fallback
from modules.record.application import flow
from modules.record.application import interview
from modules.record.application import presentation
from modules.record.application.intake import IntakeUnderstanding
from modules.record.application.output_repair import PresentationRepair
from modules.record.application.validation import RecordValidationError

from .test_conservative_fallback import concern
from .test_conservative_fallback import decision
from .test_conservative_fallback import session
from .test_dynamic_record import cached
from .test_dynamic_record import save_cache
from .test_dynamic_record import setup_manager
from .test_dynamic_record import submit
from .test_dynamic_record import supplement
from .test_missing_reply_recovery import recover_then_query

BAD_ENTRY = '如果有这里没列出的，可以在“其他疾病”里补充。'
DISEASE_OPTIONS = ['高血压', '糖尿病']
LEGACY_INVITATIONS = [
    '您还有其他想了解的问题吗？可以直接通过文字或语音告诉我，也可以选择“没有了”。',
    '请直接说说您想了解的具体问题；如果没有其他问题，可以选择“没有了”。',
]
LEGACY_INVITATIONS += [text + ' ' + presentation.ANSWER_HINT for text in LEGACY_INVITATIONS]


@pytest.mark.parametrize(
    'text',
    [
        BAD_ENTRY,
        '其它病史可以在「其它疾病」中填写。',
        '请在“其他情况”选项中输入您的回答。',
        '点击“其他”按钮后会弹出输入框，请补充。',
        '选择“高血压”后填写具体情况。',
        '勾选“糖尿病”后补充具体情况。',
        '可以在 其它疾病 里 补充具体内容。',
        '请在按钮中填写疾病名称。',
        '在“高血压”选项里补充。',
        '在“高血压”里补充具体情况。',
        '不要忘记在按钮中填写疾病名称。',
        '请在选项中补充您的疾病名称。',
    ],
)
def test_invalid_button_input_claims_are_rejected_even_when_choices_are_explicit(text):
    with pytest.raises(RecordValidationError):
        presentation.validate_answer_entry_instructions(text, options=DISEASE_OPTIONS)


@pytest.mark.parametrize(
    'text',
    [
        '可以在下方文本框输入具体疾病名称。',
        '在“文本框”里输入您的回答。',
        '不要在按钮中填写；请使用文字或语音回答。',
        '点击“高血压”按钮后不会弹出输入框。',
        '点击“高血压”按钮后无法直接展开输入框。',
        '也可以通过文字或语音补充描述。',
        '“没有”和“不清楚”请单独选择。',
        '请勾选医生明确诊断过的疾病，可以多选。',
        '可以选择“高血压”或“糖尿病”。',
        '在饮食中补充蛋白质应结合实际情况。',
        '在“饮食”中补充蛋白质应结合实际情况。',
        '可以从其他食物中补充蛋白质。',
        '也可通过其他食物补充营养。',
        '其他药物是否适合需要结合实际情况。',
        '其他报告中的结果还需要结合检查时间理解。',
        '关于用药，请通过文字说明名称和剂量。',
        '选择“高血压”后，可以通过文字或语音补充描述。',
        '已填写糖尿病，并选择高血压。',
    ],
)
def test_real_input_paths_and_medical_language_do_not_trigger_the_button_guard(text):
    presentation.validate_answer_entry_instructions(text, options=DISEASE_OPTIONS)


def patient():
    return {
        '本次回答': '平时会散步',
        '历史问答': [],
        '本次报告': None,
        '长期健康画像': None,
        '附带健康资料': None,
        '基本信息': None,
    }


def render_decision():
    return decision(
        action='ask_intake',
        target={'dimension': 'activity'},
        responses=[{'concernId': 'a', 'resolution': 'answered'}],
    )


def safe_payload():
    return {
        'assistantMessage': {
            'acknowledgement': '日常活动值得结合工作和休息安排一起了解。',
            'rationale': '再了解平时活动的安排。',
            'evidence': [{'source': '本次回答', 'quote': '平时会散步'}],
        },
        'responses': [{'concernId': 'a', 'text': '目前只能根据已提供的信息作一般说明。'}],
        'question': {
            'question': '您平时活动安排更接近哪一种？',
            'questionType': 'radio',
            'options': ['日常走动较多', '有规律运动'],
        },
    }


def damaged_payload(component):
    payload = safe_payload()
    if component == 'question':
        payload['question']['question'] = BAD_ENTRY
    elif component == 'response':
        payload['responses'][0]['text'] = BAD_ENTRY
    else:
        payload['assistantMessage'][component] = BAD_ENTRY
    return payload


@pytest.mark.parametrize('component', ['question', 'acknowledgement', 'rationale', 'response'])
def test_partial_repair_rejects_only_the_unsafe_component_and_can_accept_its_safe_replacement(component):
    collector = PresentationRepair(render_decision(), patient())
    assert collector.ingest(json.dumps(damaged_payload(component), ensure_ascii=False)) is None
    retained = collector.partial_presentation
    if component == 'question':
        assert retained['question'] is None
        assert retained['assistantMessage'] is not None
        assert len(retained['responses']) == 1
    elif component == 'response':
        assert retained['responses'] == []
        assert retained['question'] is not None
        assert retained['assistantMessage'] is not None
    else:
        assert retained['assistantMessage'] is None
        assert retained['question'] is not None
        assert len(retained['responses']) == 1
    repaired = collector.ingest(json.dumps(safe_payload(), ensure_ascii=False))
    assert repaired is not None
    assert BAD_ENTRY not in repaired.model_dump_json()


@pytest.mark.parametrize('component', ['acknowledgement', 'rationale', 'response'])
def test_question_arriving_later_rechecks_retained_input_instructions_using_its_actual_choices(component):
    collector = PresentationRepair(render_decision(), patient())
    instruction = '在“有规律运动”里填写频率。'
    partial = safe_payload()
    question = partial.pop('question')
    if component == 'response':
        partial['responses'][0]['text'] = instruction
    else:
        partial['assistantMessage'][component] = instruction
    assert collector.ingest(json.dumps(partial, ensure_ascii=False)) is None
    assert collector.ingest(json.dumps({'question': question}, ensure_ascii=False)) is None
    retained = collector.partial_presentation
    assert retained['question'] is not None
    if component == 'response':
        assert retained['responses'] == []
        assert retained['assistantMessage'] is not None
    else:
        assert retained['assistantMessage'] is None
        assert len(retained['responses']) == 1
    repaired = collector.ingest(json.dumps(safe_payload(), ensure_ascii=False))
    assert repaired is not None
    assert instruction not in repaired.model_dump_json()


@pytest.mark.parametrize('component', ['acknowledgement', 'rationale', 'response'])
def test_late_question_does_not_discard_a_safe_replacement_submitted_in_the_same_repair(component):
    collector = PresentationRepair(render_decision(), patient())
    partial = safe_payload()
    partial.pop('question')
    if component == 'response':
        partial['responses'][0]['text'] = '在“有规律运动”里填写频率。'
    else:
        partial['assistantMessage'][component] = '在“有规律运动”里填写频率。'
    assert collector.ingest(json.dumps(partial, ensure_ascii=False)) is None
    repaired = collector.ingest(json.dumps(safe_payload(), ensure_ascii=False))
    assert repaired is not None
    assert '里填写频率' not in repaired.model_dump_json()


@pytest.mark.parametrize('component', ['question', 'acknowledgement', 'rationale', 'response'])
@pytest.mark.parametrize('final_gate', [False, True])
def test_disease_choice_context_is_not_limited_to_the_incomplete_model_supplied_options(component, final_gate):
    instructions = render_decision()
    instructions['target'] = {'dimension': 'medical_history'}
    payload = safe_payload()
    payload['question']['options'] = ['没有', '不清楚']
    instruction = '在“高血压”里填写病情。'
    if component == 'question':
        payload['question']['question'] = instruction
    elif component == 'response':
        payload['responses'][0]['text'] = instruction
    else:
        payload['assistantMessage'][component] = instruction
    if final_gate:
        before = session(concerns=[concern('a')])
        with pytest.raises(RecordValidationError):
            flow.apply_presentation(before, instructions, flow.TurnPresentation(**payload), '')
    else:
        collector = PresentationRepair(instructions, patient())
        assert collector.ingest(json.dumps(payload, ensure_ascii=False)) is None
        retained = collector.partial_presentation
        if component == 'question':
            assert retained['question'] is None
        elif component == 'response':
            assert retained['responses'] == []
            assert retained['question'] is not None
        else:
            assert retained['assistantMessage'] is None
            assert retained['question'] is not None


@pytest.mark.parametrize('component', ['question', 'acknowledgement', 'rationale', 'response'])
def test_fallback_uses_the_final_disease_choices_to_drop_only_unsafe_partial_components(component):
    before = session(count=1, concerns=[concern('a')])
    original = deepcopy(before)
    instructions = render_decision()
    instructions['target'] = interview.fixed_target(before['qaFlow'], 1)
    payload = safe_payload()
    payload['question']['options'] = ['没有', '不清楚']
    instruction = '在“高血压”里填写病情。'
    if component == 'question':
        payload['question']['question'] = instruction
    elif component == 'response':
        payload['responses'][0]['text'] = instruction
    else:
        payload['assistantMessage'][component] = instruction
    partial = flow.TurnPresentation(**payload)
    result = fallback.build_fallback(
        before,
        answer='平时会散步',
        validated_draft=deepcopy(before),
        decision=instructions,
        partial_presentation={
            'question': partial.question,
            'assistantMessage': partial.assistantMessage,
            'responses': partial.responses,
        },
    )
    assert instruction not in result['currentQuestion']['question']
    assert instruction not in result['currentQuestion']['assistantMessage']
    assert '高血压' in result['currentQuestion']['options']
    saved = result['qaFlow']['concerns'][0]
    if component == 'response':
        assert saved['status'] == 'pending'
    else:
        assert saved['response'] == safe_payload()['responses'][0]['text']
    assert before == original


@pytest.mark.parametrize('component', ['acknowledgement', 'rationale', 'response'])
@pytest.mark.parametrize('question_state', ['absent', 'invalid'])
def test_fallback_rechecks_components_against_its_replacement_lifestyle_question(component, question_state):
    before = session(count=3, concerns=[concern('a')])
    original = deepcopy(before)
    instructions = render_decision()
    instructions['target'] = {
        'topic': '饮水情况',
        'aspect': 'habits',
        'dimension': 'hydration',
        'scope': 'general',
        'complaintIds': [],
        'complaints': [],
    }
    payload = safe_payload()
    instruction = '在“白水”里填写频次。'
    if component == 'response':
        payload['responses'][0]['text'] = instruction
    else:
        payload['assistantMessage'][component] = instruction
    if question_state == 'absent':
        payload['question'] = None
    else:
        payload['question']['options'] = ['其他饮品', '白水']
    partial = flow.TurnPresentation(**payload)
    result = fallback.build_fallback(
        before,
        answer='平时会散步',
        validated_draft=deepcopy(before),
        decision=instructions,
        partial_presentation={
            'question': partial.question,
            'assistantMessage': partial.assistantMessage,
            'responses': partial.responses,
        },
    )
    assert '白水' in result['currentQuestion']['options']
    assert instruction not in result['currentQuestion']['assistantMessage']
    saved = result['qaFlow']['concerns'][0]
    if component == 'response':
        assert saved['status'] == 'pending'
        assert instruction not in saved['response']
    else:
        assert (
            safe_payload()['assistantMessage']['acknowledgement'] not in result['currentQuestion']['assistantMessage']
        )
        assert saved['response'] == safe_payload()['responses'][0]['text']
    assert before == original


@pytest.mark.parametrize('component', ['acknowledgement', 'rationale', 'response'])
def test_partial_analysis_without_decision_uses_the_final_default_question_for_component_checks(component):
    before = session(count=2, concerns=[concern('a')])
    original = deepcopy(before)
    payload = safe_payload()
    instruction = '在“蔬菜”里填写比例。'
    if component == 'response':
        payload['responses'][0]['text'] = instruction
    else:
        payload['assistantMessage'][component] = instruction
    payload['question'] = None
    partial = flow.TurnPresentation(**payload)
    result = fallback.build_fallback(
        before,
        answer='平时会散步',
        partial_analysis=SimpleNamespace(
            partial_intake=IntakeUnderstanding(complaints=[], facts=[]), concerns=[], gaps=[]
        ),
        partial_presentation={
            'question': partial.question,
            'assistantMessage': partial.assistantMessage,
            'responses': partial.responses,
        },
    )
    assert result['currentQuestion']['intakeDimension'] == 'diet_structure'
    assert '蔬菜' in result['currentQuestion']['options']
    assert instruction not in result['currentQuestion']['assistantMessage']
    if component != 'response':
        assert (
            safe_payload()['assistantMessage']['acknowledgement'] not in result['currentQuestion']['assistantMessage']
        )
    assert result['qaFlow']['concerns'][0]['status'] == 'pending'
    assert before == original


def test_model_supplied_text_and_voice_hint_is_replaced_by_the_single_service_hint():
    result = presentation.with_answer_hint('您平时活动如何安排？请用文字或语音回答。')
    assert result == '您平时活动如何安排？ ' + presentation.ANSWER_HINT
    assert result.count(presentation.ANSWER_HINT) == 1
    assert '请用文字或语音回答' not in result


@pytest.mark.parametrize('text', LEGACY_INVITATIONS)
def test_legacy_fixed_invitations_remain_recognizable_without_rewriting_cached_copy(text):
    question = {'question': text, 'questionType': 'radio', 'options': ['没有了']}
    original = deepcopy(question)
    assert presentation.is_supplement_question(question)
    assert question == original


@pytest.mark.parametrize('clarify', [False, True])
def test_new_fixed_invitation_has_only_the_canonical_text_and_voice_hint(clarify):
    question = presentation.supplement_question(clarify=clarify)
    published = presentation.with_answer_hint(question['question'])
    assert published == question['question']
    assert published.count('文字或语音') == 1
    assert presentation.with_answer_hint(published) == published
    assert presentation.is_supplement_question({**question, 'question': published})


@pytest.mark.asyncio
@pytest.mark.parametrize('text', LEGACY_INVITATIONS)
@pytest.mark.parametrize('streaming', [False, True])
async def test_legacy_invitation_no_more_finishes_and_replays_without_model_calls(text, streaming):
    manager, module, gateway, _ = setup_manager()
    current = await supplement(manager, gateway, length=4)
    before = cached(module)
    before['currentQuestion']['question'] = text
    before['qaFlow']['lastSubmission']['result']['question'] = text
    save_cache(module, before)
    current = await manager.getSession('u', 's')
    assert current['question'] == text
    calls = len(gateway.requests)
    final = await submit(manager, current, '没有了', streaming=streaming)
    assert final['status'] == 'finished'
    assert final['endReason'] == 'no_more_questions'
    assert final['interactionCount'] == current['interactionCount']
    repeated = await submit(manager, current, '没有了', streaming=streaming)
    assert repeated == final
    assert len(gateway.requests) == calls
    history = await manager.getHistory('u', 's')
    assert len(history['history']) == len(before['qaHistory']) + 1


@pytest.mark.asyncio
async def test_legacy_invitation_with_multiple_pending_concerns_restores_without_regeneration():
    manager, module, gateway, _ = setup_manager()
    await supplement(manager, gateway, length=4)
    before = cached(module)
    before['qaFlow']['phase'] = 'qa'
    before['qaFlow']['concerns'] = [concern('a'), concern('b')]
    published = before['qaFlow']['lastSubmission']['result']
    published.update(question=LEGACY_INVITATIONS[0], phase='qa')
    before['currentQuestion'] = None
    save_cache(module, before)
    calls = len(gateway.requests)
    restored = await recover_then_query(manager, 'getSession')
    assert restored == published
    assert await manager.getSession('u', 's') == restored
    after = cached(module)
    assert after['currentQuestion']['question'] == LEGACY_INVITATIONS[0]
    assert after['qaHistory'] == before['qaHistory']
    assert after['answers'] == before['answers']
    assert after['qaFlow']['concerns'] == before['qaFlow']['concerns']
    assert after['qaFlow']['qaRoundCount'] == before['qaFlow']['qaRoundCount']
    assert len(gateway.requests) == calls


@pytest.mark.parametrize('component', ['question', 'acknowledgement', 'rationale', 'response'])
def test_final_publication_gate_cannot_republish_unsafe_components(component):
    before = session(concerns=[concern('a')])
    original = deepcopy(before)
    with pytest.raises(RecordValidationError):
        flow.apply_presentation(before, render_decision(), flow.TurnPresentation(**damaged_payload(component)), '')
    assert before == original


def test_unsafe_historical_response_is_not_locked_into_a_reuse_instruction():
    old = concern('a', status='answered', response=BAD_ENTRY)
    old['handledCount'] = flow.MAX_CONCERN_HANDLINGS
    original = deepcopy(old)
    instruction = flow._response_instruction(old, final=True)
    assert 'reuseResponse' not in instruction
    assert old == original


@pytest.mark.parametrize('closing', [False, True])
def test_fallback_never_republishes_unsafe_reused_response_even_if_another_component_survives(closing):
    before = session(
        count=20 if closing else 4, phase='qa', concerns=[concern('a', status='answered', response=BAD_ENTRY)]
    )
    original = deepcopy(before)
    result = fallback.build_fallback(
        before,
        answer='还想了解怎样安排活动',
        validated_draft=deepcopy(before),
        decision=decision(
            action='finish' if closing else 'invite_supplement',
            responses=[{'concernId': 'a', 'resolution': 'answered', 'reuseResponse': BAD_ENTRY}],
        ),
        partial_presentation={'responses': [flow.ConcernResponse(concernId='a', text='另一份可靠的简短说明。')]},
    )
    saved = result['qaFlow']['concerns'][0]
    text = result['qaFlow']['finalMessage'] if closing else result['currentQuestion']['assistantMessage']
    assert BAD_ENTRY not in text
    assert BAD_ENTRY not in saved['response']
    assert saved['status'] == 'pending'
    assert before == original


@pytest.mark.parametrize('previous_response', [BAD_ENTRY, '在“白水”里填写频次。'])
def test_finishing_unresolved_concern_does_not_append_an_unsafe_old_partial_answer(previous_response):
    before = session(phase='qa', concerns=[concern('a', response=previous_response)])
    before['currentQuestion'].update(question='您平时常喝哪些饮品？', questionType='radio', options=['白水', '茶'])
    original = deepcopy(before)
    final = fallback.build_fallback(before, answer=['没有了'])
    assert final['currentQuestion'] is None
    assert final['qaFlow']['concerns'][0]['status'] == 'pending'
    assert final['qaFlow']['concerns'][0]['response'] == previous_response
    assert previous_response not in final['qaFlow']['finalMessage']
    assert before == original


@pytest.mark.parametrize('degraded', [False, True])
@pytest.mark.parametrize(
    'user_question',
    ['为什么让我在“其他疾病”里补充？按钮里又不能输入。', '为什么让我在“白水”里填写频次？按钮里又不能输入。'],
)
def test_user_question_quoting_the_bad_instruction_remains_verbatim_in_its_own_source_field(degraded, user_question):
    item = concern('a')
    item['text'] = user_question
    item['evidence'] = user_question
    before = session(phase='qa', concerns=[item])
    before['currentQuestion'].update(question='您平时常喝哪些饮品？', questionType='radio', options=['白水', '茶'])
    original = deepcopy(before)
    reply = '请直接通过文字或语音说明；快捷按钮仅用于选择答案。'
    instructions = decision(action='finish', responses=[{'concernId': 'a', 'resolution': 'answered'}])
    if degraded:
        result = fallback.build_fallback(
            before,
            answer=['没有了'],
            validated_draft=deepcopy(before),
            decision=instructions,
            partial_presentation={'responses': [flow.ConcernResponse(concernId='a', text=reply)]},
        )
    else:
        result = deepcopy(before)
        flow.apply_presentation(
            result,
            instructions,
            flow.TurnPresentation(
                assistantMessage={'acknowledgement': '已说明回答方式。', 'rationale': '', 'evidence': []},
                responses=[{'concernId': 'a', 'text': reply}],
                question=None,
            ),
            '',
        )
    assert result['qaFlow']['concerns'][0]['text'] == user_question
    assert result['qaFlow']['concerns'][0]['evidence'] == user_question
    assert user_question not in result['qaFlow']['finalMessage']
    assert reply in result['qaFlow']['finalMessage']
    assert before == original
