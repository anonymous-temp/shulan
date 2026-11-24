"""Patient copy guards apply to new text, without rewriting source or history."""

from types import SimpleNamespace

import pytest

from modules.record.application.presentation import validate_acknowledgement_style
from modules.record.application.presentation import validate_copy_components
from modules.record.application.presentation import validate_response_style
from modules.record.application.validation import RecordValidationError

LONG_EXPLANATION = '家族中的共同生活方式与遗传背景可以共同影响健康状况，但不等于某个人一定发病'
SECOND_EXPLANATION = '日常活动可以从通勤和工作间隙逐步安排，结合个人能坚持的节奏形成习惯'


def message(acknowledgement='', rationale=''):
    return SimpleNamespace(acknowledgement=acknowledgement, rationale=rationale)


@pytest.mark.parametrize(
    'limit',
    [
        '单凭现有信息无法准确判断。',
        '近期身体不适的具体原因仅凭现有信息无法确定。',
        '现有资料还不能判断具体原因。',
        '无法根据目前信息确定个人风险。',
        '信息还不完整。',
        '没有提供具体症状，因此无法确定原因。',
    ],
)
@pytest.mark.parametrize('position', ['before', 'middle', 'after'])
def test_information_deferral_is_rejected_even_inside_an_otherwise_substantive_answer(limit, position):
    texts = [LONG_EXPLANATION + '。', SECOND_EXPLANATION + '。']
    texts.insert({'before': 0, 'middle': 1, 'after': 2}[position], limit)
    with pytest.raises(RecordValidationError) as caught:
        validate_response_style(''.join(texts))
    assert caught.value.code == 'qa_information_deferral_forbidden'


@pytest.mark.parametrize('field', ['acknowledgement', 'rationale'])
def test_information_deferral_is_also_rejected_in_new_bridge_components(field):
    values = {'acknowledgement': '日常生活的不同安排值得结合起来了解。', 'rationale': ''}
    values[field] = '近期身体不适的具体原因仅凭现有信息无法确定。'
    with pytest.raises(RecordValidationError) as caught:
        validate_acknowledgement_style(message(**values), action='ask_intake')
    assert caught.value.code == 'qa_information_deferral_forbidden'


@pytest.mark.parametrize(
    'text',
    [
        '这些因素可能相关，并不代表已确定病因。',
        '这类信息可以提供一般参考，不能据此直接确诊。平时可以关注生活安排及定期检查。',
        '家族聚集不等于子女一定发病，也不意味着已经确诊遗传病。',
        '有糖尿病史时，血糖波动可以作为需要关注的因素之一。',
        '父母患有糖尿病，子女并不一定发病。',
        '具体用药需要由医生结合检查评估，不自行调整剂量。',
    ],
)
def test_uncertainty_conditionals_and_medical_boundaries_remain_allowed(text):
    validate_response_style(text)


@pytest.mark.parametrize(
    'text',
    [
        '家族中母亲和外祖父有糖尿病，您本人也确诊糖尿病，确实会关心遗传背景。',
        '从您提供的信息看，母亲和外祖父有糖尿病，您本人也确诊糖尿病，提示家族中可能存在糖尿病聚集倾向。',
        '您本人也确诊糖尿病，平时注意生活安排。',
        '根据您的描述，您母亲患有糖尿病，确实会担心遗传风险。',
    ],
)
def test_explicit_history_preambles_are_rejected_in_both_bridge_and_answer(text):
    for validate in (
        lambda: validate_acknowledgement_style(message(text), action='invite_supplement', has_responses=True),
        lambda: validate_response_style(text),
    ):
        with pytest.raises(RecordValidationError) as caught:
            validate()
        assert caught.value.code == 'background_recap_forbidden'


def test_answering_turn_may_have_empty_bridge_but_an_intake_turn_must_keep_its_bridge():
    validate_acknowledgement_style(message(), action='invite_supplement', has_responses=True)
    validate_acknowledgement_style(message(), action='finish')
    with pytest.raises(RecordValidationError) as caught:
        validate_acknowledgement_style(message(), action='ask_intake')
    assert caught.value.code == 'acknowledgement_required'


def test_same_disease_words_and_short_conditions_are_not_duplicate_answers():
    answers = ['糖尿病可以关注血糖变化。', '糖尿病的日常饮食可以结合个人习惯安排。']
    validate_copy_components(message(), answers, action='invite_supplement')
    validate_response_style('糖尿病可以关注血糖变化。糖尿病的日常饮食可以结合个人习惯安排。')


@pytest.mark.parametrize('separator', ['。', '；', '\n'])
def test_repeated_long_sentence_inside_answer_is_rejected(separator):
    with pytest.raises(RecordValidationError) as caught:
        validate_response_style(LONG_EXPLANATION + separator + LONG_EXPLANATION + '。')
    assert caught.value.code == 'duplicate_response_copy'


@pytest.mark.parametrize('as_objects', [False, True])
def test_duplicate_answer_components_report_the_later_component_for_partial_repair(as_objects):
    answers = [LONG_EXPLANATION + '。', LONG_EXPLANATION + '。' + SECOND_EXPLANATION + '。']
    if as_objects:
        answers = [SimpleNamespace(text=text) for text in answers]
    with pytest.raises(RecordValidationError) as caught:
        validate_copy_components(message(), answers, action='invite_supplement')
    assert caught.value.code == 'duplicate_response_copy'
    assert caught.value.diagnostic_fields == {'component': 'responses', 'responseIndex': 1}


def test_self_repeated_answer_component_reports_its_own_index():
    answers = [SECOND_EXPLANATION + '。', (LONG_EXPLANATION + '。') * 2]
    with pytest.raises(RecordValidationError) as caught:
        validate_copy_components(message(), answers, action='invite_supplement')
    assert caught.value.diagnostic_fields == {'component': 'responses', 'responseIndex': 1}


def test_visible_bridge_repeating_body_is_identified_without_rejecting_valid_body():
    with pytest.raises(RecordValidationError) as caught:
        validate_copy_components(message(LONG_EXPLANATION + '。'), [LONG_EXPLANATION + '。'], action='ask_intake')
    assert caught.value.code == 'acknowledgement_repeats_answer'
    assert caught.value.diagnostic_fields == {'component': 'assistantMessage'}
    validate_response_style(LONG_EXPLANATION + '。')


@pytest.mark.parametrize('action', ['invite_supplement', 'finish'])
def test_hidden_formal_qa_bridge_does_not_cause_duplicate_copy_repair(action):
    validate_copy_components(message(LONG_EXPLANATION + '。'), [LONG_EXPLANATION + '。'], action=action)


@pytest.mark.parametrize('reply', [LONG_EXPLANATION + '。', LONG_EXPLANATION + '。' + SECOND_EXPLANATION + '。'])
def test_reasked_question_cannot_reuse_prior_answer_or_its_long_sentence(reply):
    with pytest.raises(RecordValidationError) as caught:
        validate_response_style(reply, previous_response=LONG_EXPLANATION + '。')
    assert caught.value.code == 'repeated_previous_response'


def test_reasked_question_can_give_short_new_explanation_with_same_medical_terms():
    validate_response_style(
        '家族健康背景可以提醒您关注生活习惯和日常检查，重点是长期管理。',
        previous_response=LONG_EXPLANATION + '。',
    )
