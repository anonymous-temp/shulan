"""Fresh-question admission and direct QA presentation without external services."""

from copy import deepcopy

import pytest

from modules.record.application import flow
from modules.record.application.presentation import COMPLETION_MESSAGE
from modules.record.application.validation import RecordValidationError

from .test_dynamic_record import assessment
from .test_dynamic_record import concern


def qa_snapshot(*, status='limited', handled=1):
    state = {'qaFlow': flow.new_flow(), 'qaHistory': [], 'currentQuestion': {'questionId': 'invitation'}}
    state['qaFlow'].update(phase='supplement', intakeQuestionCount=15, intakeCount=15, interactionCount=15)
    state['qaFlow']['concerns'] = [
        {
            'id': 'discomfort',
            'text': '近期身体不适的原因',
            'evidence': '近期身体不适的具体原因',
            'status': status,
            'handledCount': handled,
            'response': '身体变化可以结合饮食、活动与睡眠安排一起观察，日常保持规律的生活节奏。',
            'responseStyleVersion': flow.RESPONSE_STYLE_VERSION,
            'readyToAnswer': True,
            'informationNeeded': '',
        }
    ]
    return state


def parsed(*items):
    return flow.PatientAssessment(**assessment(intent='additional', concerns=items, gaps=[]))


def reask(state, *, identity='discomfort', quote='刚才身体不适那个问题再解释一下'):
    return {**concern(state['qaFlow']['concerns'][0]['text'], identity=identity), 'evidence': quote}


@pytest.mark.parametrize('status', ['answered', 'limited'])
@pytest.mark.parametrize('identity', ['discomfort', None])
def test_old_answer_cannot_be_reintroduced_using_only_its_historical_evidence(status, identity):
    state = qa_snapshot(status=status)
    before = deepcopy(state)
    result = parsed(reask(state, identity=identity, quote='近期身体不适的具体原因'))
    with pytest.raises(RecordValidationError, match='已答疑问'):
        flow.validate_assessment(result, state, '家族病史对我有什么影响')
    with pytest.raises(RecordValidationError, match='已答疑问'):
        flow.advance_flow(state, result, answer='家族病史对我有什么影响')
    assert state == before


@pytest.mark.parametrize('identity', ['discomfort', None])
@pytest.mark.parametrize(
    'answer', ['刚才身体不适那个问题，再解释一下。', {'content': '刚才身体不适那个问题，再解释一下。'}]
)
def test_explicit_current_requestion_is_admitted_without_requiring_the_old_wording(identity, answer):
    state = qa_snapshot()
    result = parsed(reask(state, identity=identity))
    flow.validate_assessment(result, state, answer)
    draft, decision = flow.advance_flow(state, result, answer=answer)
    assert draft['qaFlow']['qaRoundCount'] == 1
    assert [item['concernId'] for item in decision['responses']] == ['discomfort']
    instruction = decision['responses'][0]
    assert instruction['previousResponse'] == state['qaFlow']['concerns'][0]['response']
    assert 'reuseResponse' not in instruction


def test_new_family_questions_do_not_also_answer_the_previously_explained_discomfort():
    state = qa_snapshot()
    texts = ['我的家族是否存在遗传病史', '这对我有什么影响']
    result = parsed(*[concern(text) for text in texts])
    answer = '？'.join(texts) + '？'
    flow.validate_assessment(result, state, answer)
    draft, decision = flow.advance_flow(state, result, answer=answer)
    assert [item['text'] for item in decision['responses']] == texts
    assert draft['qaFlow']['qaRoundCount'] == 1
    assert draft['qaFlow']['concerns'][0] == state['qaFlow']['concerns'][0]


def test_real_pending_question_is_still_answered_without_requiring_a_fresh_requestion():
    state = qa_snapshot(status='pending')
    result = parsed(concern('近期身体不适的原因', identity='discomfort'), concern('家族病史对我有什么影响'))
    answer = '家族病史对我有什么影响'
    flow.validate_assessment(result, state, answer)
    draft, decision = flow.advance_flow(state, result, answer=answer)
    assert len(decision['responses']) == 2
    assert decision['responses'][0]['concernId'] == 'discomfort'
    assert draft['qaFlow']['qaRoundCount'] == 1


@pytest.mark.parametrize('version', [None, 13, 14])
def test_no_number_of_previous_handlings_enables_literal_response_reuse(version):
    state = qa_snapshot(handled=7)
    item = state['qaFlow']['concerns'][0]
    item['responseStyleVersion'] = version
    instruction = flow._response_instruction(item, final=True)
    assert instruction['previousResponse'] == item['response']
    assert 'reuseResponse' not in instruction


def test_old_minimal_snapshots_without_status_keep_pending_source_compatibility():
    state = {'qaFlow': {'concerns': [{'id': 'old'}]}}
    flow.validate_concern_source(flow.ConcernAssessment(**concern('旧疑问', identity='old')), state, '没有补充')


def direct_presentation(decision, *, acknowledgement=''):
    return flow.TurnPresentation(
        assistantMessage={'acknowledgement': acknowledgement, 'rationale': '', 'evidence': []},
        responses=[
            {
                'concernId': item['concernId'],
                'text': '日常调整可以从规律安排三餐做起，选择适合自己且容易坚持的活动。',
            }
            for item in decision['responses']
        ],
        question=None,
    )


@pytest.mark.parametrize('acknowledgement', ['', '生活习惯可以从不同方面结合起来了解。'])
def test_qa_responses_start_directly_without_publishing_a_separate_intro(acknowledgement):
    state = qa_snapshot()
    result = parsed(concern('日常饮食应该怎样安排'))
    draft, decision = flow.advance_flow(state, result, answer='日常饮食应该怎样安排')
    output = direct_presentation(decision, acknowledgement=acknowledgement)
    flow.apply_presentation(draft, decision, output, '')
    assert draft['currentQuestion']['assistantMessage'] == output.responses[0].text
    assert draft['currentQuestion']['acknowledgementText'] == ''
    assert draft['qaFlow']['concerns'][-1]['responseStyleVersion'] == flow.RESPONSE_STYLE_VERSION


def test_fifth_qa_reply_is_published_before_completion_without_intro():
    state = qa_snapshot()
    state['qaFlow']['qaRoundCount'] = 4
    result = parsed(concern('日常活动应该如何安排'))
    draft, decision = flow.advance_flow(state, result, answer='日常活动应该如何安排')
    output = direct_presentation(decision)
    flow.apply_presentation(draft, decision, output, '')
    assert draft['qaFlow']['qaRoundCount'] == 5
    assert draft['qaFlow']['finalMessage'] == output.responses[0].text + '\n\n' + COMPLETION_MESSAGE
    assert draft['currentQuestion'] is None


def test_a_reasked_question_cannot_publish_the_exact_previous_answer():
    state = qa_snapshot()
    result = parsed(reask(state))
    draft, decision = flow.advance_flow(state, result, answer='刚才身体不适那个问题再解释一下')
    output = direct_presentation(decision)
    output.responses[0].text = state['qaFlow']['concerns'][0]['response']
    before = deepcopy(draft)
    with pytest.raises(RecordValidationError):
        flow.apply_presentation(draft, decision, output, '')
    assert draft == before


def test_a_cached_legacy_decision_cannot_bypass_fresh_qa_wording_with_reuse_response():
    state = qa_snapshot()
    result = parsed(reask(state))
    draft, decision = flow.advance_flow(state, result, answer='刚才身体不适那个问题再解释一下')
    decision['responses'][0]['reuseResponse'] = state['qaFlow']['concerns'][0]['response']
    with pytest.raises(RecordValidationError, match='复用旧答原文'):
        flow.apply_presentation(draft, decision, direct_presentation(decision), '')


def test_a_new_intake_question_still_requires_a_nonempty_acknowledgement():
    state = {'qaFlow': flow.new_flow(), 'qaHistory': [], 'currentQuestion': None}
    result = flow.PatientAssessment(**assessment(substantive=False, gaps=[]))
    draft, decision = flow.advance_flow(state, result)
    output = flow.TurnPresentation(
        assistantMessage={'acknowledgement': '', 'rationale': '从日常生活中值得关心的方面开始。', 'evidence': []},
        responses=[],
        question={'question': '本次最关心什么方面？', 'questionType': 'radio', 'options': ['饮食安排', '活动安排']},
    )
    with pytest.raises(RecordValidationError):
        flow.apply_presentation(draft, decision, output, '')
