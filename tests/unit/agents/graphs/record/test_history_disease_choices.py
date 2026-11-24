"""History slots collect disease names without changing interview or source semantics."""

from copy import deepcopy

import pytest

from health_profile import RecordAnswerCandidateExtractor
from modules.record.application import fallback
from modules.record.application import flow
from modules.record.application import interview
from tests.unit._support.record_policy import short_interview_policy
from tests.unit.health_profile.test_record_ingestion import RecordFixture
from tests.unit.health_profile.test_record_ingestion import _runtime

from .test_conservative_fallback import session
from .test_dynamic_record import assessment
from .test_dynamic_record import cached
from .test_dynamic_record import save_cache
from .test_dynamic_record import setup_manager
from .test_dynamic_record import submit


def binary_question(text='您有哪些医生明确诊断的疾病？'):
    return flow.GeneratedQuestion(question=text, questionType='radio', options=['有', '没有', '不清楚'])


def assert_disease_choices(question, dimension):
    assert question['questionType'] == 'checkbox'
    assert {'没有', '不清楚', '糖尿病', '高血压'} <= set(question['options'])
    assert not any('其他' in option or '其它' in option for option in question['options'])
    assert '有' not in question['options']
    assert not any('不想' in option or '跳过' in option for option in question['options'])
    if dimension == 'family_history':
        assert {'地中海贫血', '血友病'} <= set(question['options'])


@pytest.mark.parametrize(('dimension', 'index'), [('medical_history', 1), ('family_history', 2)])
def test_normal_history_publication_corrects_binary_model_options_but_preserves_natural_question(dimension, index):
    draft = session(count=index)
    target = interview.fixed_target(draft['qaFlow'], index)
    assert_disease_choices(target['choiceConstraints'], dimension)
    assert not target['supplementation']
    text = '关于您已经确诊的疾病，哪些情况需要记录？' if index == 1 else '血亲中有哪些您知道的疾病情况？'
    presentation = flow.TurnPresentation(
        assistantMessage={'acknowledgement': '个人诊断记录值得与生活背景结合了解。', 'rationale': '', 'evidence': []},
        responses=[],
        question=binary_question(text),
    )
    flow.apply_presentation(draft, {'action': 'ask_intake', 'target': target, 'responses': []}, presentation, '')
    current = draft['currentQuestion']
    assert_disease_choices(current, dimension)
    assert current['question'].startswith(text)
    assert current['intakeDimension'] == dimension


@pytest.mark.parametrize(('dimension', 'index'), [('medical_history', 1), ('family_history', 2)])
@pytest.mark.parametrize('refusal', ['暂不想回答', '跳过'])
def test_disease_choices_do_not_hide_forbidden_model_shortcuts(dimension, index, refusal):
    draft = session(count=index)
    target = interview.fixed_target(draft['qaFlow'], index)
    assert target['dimension'] == dimension
    original = deepcopy(draft)
    presentation = flow.TurnPresentation(
        assistantMessage={'acknowledgement': '家族背景能补充个人诊断记录之外的信息。', 'rationale': '', 'evidence': []},
        responses=[],
        question=flow.GeneratedQuestion(
            question='哪些疾病情况需要记录？', questionType='radio', options=[refusal, '没有']
        ),
    )
    with pytest.raises(ValueError, match='拒绝回答或跳过'):
        flow.apply_presentation(draft, {'action': 'ask_intake', 'target': target, 'responses': []}, presentation, '')
    assert draft == original


@pytest.mark.parametrize(('dimension', 'count'), [('medical_history', 0), ('family_history', 1)])
def test_whole_fallback_publishes_the_same_concrete_disease_choices(dimension, count):
    before = session(count=count)
    original = deepcopy(before)
    result = fallback.build_fallback(before, answer='已提交的背景信息', failure_kind='timeout')
    assert before == original
    assert_disease_choices(result['currentQuestion'], dimension)
    assert result['currentQuestion']['intakeDimension'] == dimension
    assert result['qaFlow']['intakeQuestionCount'] == count + 1


@pytest.mark.parametrize(('dimension', 'index'), [('medical_history', 1), ('family_history', 2)])
def test_partial_fallback_cannot_reintroduce_binary_history_choices(dimension, index):
    before = session(count=index - 1)
    parsed = flow.PatientAssessment(**assessment())
    draft, decision = flow.advance_flow(before, parsed, answer='已提供背景')
    assert decision['target']['dimension'] == dimension
    result = fallback.build_fallback(
        before,
        answer='已提供背景',
        validated_draft=draft,
        decision=decision,
        partial_presentation={'question': binary_question()},
        failure_kind='validation',
    )
    assert_disease_choices(result['currentQuestion'], dimension)
    assert result['qaFlow']['intakeQuestionCount'] == index


@pytest.mark.parametrize(('dimension', 'index'), [('medical_history', 1), ('family_history', 2)])
def test_previously_known_history_keeps_the_supplement_question_instead_of_reasking_diseases(dimension, index):
    draft = session(count=index)
    draft['qaFlow']['intake']['facts'][f'general:{dimension}'] = {
        'dimension': dimension,
        'scope': 'general',
        'complaintIds': [],
        'status': 'known',
        'summary': '已说明糖尿病病史',
        'evidence': [],
    }
    target = interview.fixed_target(draft['qaFlow'], index)
    assert target['supplementation']
    assert target['choiceConstraints'] == {'questionType': 'radio', 'options': ['没有需要补充的内容', '目前记不清']}
    question = flow.GeneratedQuestion(
        question='您已说明相关情况，这方面还有哪些补充或更正？',
        questionType='radio',
        options=['没有需要补充的内容', '目前记不清'],
    )
    presentation = flow.TurnPresentation(
        assistantMessage={'acknowledgement': '家族背景能补充个人诊断记录之外的信息。', 'rationale': '', 'evidence': []},
        responses=[],
        question=question,
    )
    flow.apply_presentation(draft, {'action': 'ask_intake', 'target': target, 'responses': []}, presentation, '')
    assert draft['currentQuestion']['questionType'] == 'radio'
    assert draft['currentQuestion']['options'] == question.options


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('length', [4, 5, 6])
async def test_complete_and_streaming_keep_multi_disease_answers_and_four_to_six_topic_slots(streaming, length):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    legacy = cached(module)
    legacy['qaFlow']['interviewPolicy'] = short_interview_policy()
    save_cache(module, legacy)
    answers = ['想了解如何调整饮食', ['糖尿病', '高血压'], ['糖尿病', '血友病'], '经常外食', '经常久坐', '作息不规律']
    for count in range(1, length + 1):
        if count in (2, 3):
            assert_disease_choices(current, 'medical_history' if count == 2 else 'family_history')
        gateway.next_assessment = assessment(
            sufficient=count >= length, gaps=[] if count == length and length == 4 else None
        )
        if length == 6:
            for gap in gateway.next_assessment['gaps']:
                if gap.get('dimension') in {'diet_structure', 'activity', 'sleep'}:
                    gap['essential'] = True
        current = await submit(manager, current, answers[count - 1], streaming=streaming)
        assert current['phase'] == ('supplement' if count == length else 'intake')
    stored = cached(module)
    assert stored['qaHistory'][1]['answer'] == ['糖尿病', '高血压']
    assert stored['qaHistory'][2]['answer'] == ['糖尿病', '血友病']
    assert stored['qaFlow']['intakeQuestionCount'] == length
    assert stored['qaFlow']['qaRoundCount'] == 0
    status = await manager.getSession('u', 's')
    history = await manager.getHistory('u', 's')
    assert status['intakeQuestionCount'] == history['intakeQuestionCount'] == length
    assert history['history'][1]['answer'] == ['糖尿病', '高血压']
    assert history['history'][2]['answer'] == ['糖尿病', '血友病']


@pytest.mark.asyncio
@pytest.mark.parametrize(('subject_kind', 'subject_hint'), [('self', '本人'), ('blood_relative', '父亲')])
async def test_disease_only_family_selection_cannot_be_assigned_to_self_or_an_invented_relative(
    subject_kind, subject_hint
):
    runtime, _ = _runtime(
        {
            'candidates': [
                {
                    'answerIndex': 0,
                    'factType': 'family_history.condition',
                    'displayName': '糖尿病家族史',
                    'valueType': 'text',
                    'value': '糖尿病',
                    'subjectKind': subject_kind,
                    'subjectHint': subject_hint,
                    'evidenceText': '糖尿病',
                    'confidence': 0.9,
                }
            ]
        }
    )
    record = RecordFixture.record(
        qaHistory=[
            {
                'question': {
                    'questionId': 'q_family',
                    'questionType': 'checkbox',
                    'question': '父母、兄弟姐妹等血亲中，您知道有哪些疾病情况？',
                },
                'answer': ['糖尿病'],
                'answeredAt': RecordFixture.now.isoformat(),
            }
        ]
    )
    candidates = await RecordAnswerCandidateExtractor(runtime.hub).extract(record, subject_id='subject-self')
    if subject_kind == 'self':
        assert len(candidates) == 1
        assert candidates[0].fact_type == 'family_history.reported'
        assert candidates[0].value['presence'] == 'present'
        assert candidates[0].value['conditions'] == ['糖尿病']
        assert candidates[0].value['relationship'] is None
        assert candidates[0].subject_id == 'subject-self'
    else:
        assert candidates == ()
