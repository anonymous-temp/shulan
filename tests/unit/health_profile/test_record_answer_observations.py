"""Clear entities and unclear answer states survive Record profile extraction."""

from copy import deepcopy

import pytest

from health_profile import AssertionLevel
from health_profile import FactStatus
from health_profile import RecordAnswerCandidateExtractor
from health_profile import RecordProfileProcessor
from health_profile import RepositoryHealthProfileChatMemoryProvider
from health_profile.record_observations import FAMILY_REPORTED
from health_profile.record_observations import HISTORY_STATUS
from health_profile.summary import build_profile_summary
from tests.unit.health_profile.test_record_ingestion import FakeRecordProfileService
from tests.unit.health_profile.test_record_ingestion import FakeRecordStore
from tests.unit.health_profile.test_record_ingestion import RecordFixture
from tests.unit.health_profile.test_record_ingestion import _runtime
from tests.unit.health_profile.test_report_ingestion import FakeProfileRepository
from tests.unit.health_profile.test_report_ingestion import ReportIngestionFixture


def source(answer, *, dimension='medical_history', question=None):
    questions = {
        'goal': '您本次最希望了解或改善什么健康问题？',
        'medical_history': '您曾经被医生明确诊断过哪些疾病？',
        'family_history': '父母、兄弟姐妹等血亲中有哪些您知道的疾病？',
        'symptoms': '请描述您本人的实际身体情况。',
    }
    return RecordFixture.record(
        qaHistory=[
            {
                'question': {
                    'questionId': 'q_observation',
                    'questionType': 'checkbox',
                    'question': question or questions[dimension],
                    'intakeDimension': dimension,
                },
                'answer': answer,
                'answeredAt': RecordFixture.now.isoformat(),
            }
        ]
    )


def candidate(fact_type, value, evidence, *, name=None, subject='self', hint='本人'):
    return {
        'answerIndex': 0,
        'factType': fact_type,
        'displayName': name or (value if isinstance(value, str) else '模型摘要'),
        'valueType': 'object' if isinstance(value, dict) else 'text',
        'value': value,
        'subjectKind': subject,
        'subjectHint': hint,
        'evidenceText': evidence,
        'confidence': 0.9,
    }


async def extract(record, *items):
    runtime, _ = _runtime({'candidates': list(items)})
    return await RecordAnswerCandidateExtractor(runtime.hub).extract(record, subject_id='subject-self')


@pytest.mark.asyncio
@pytest.mark.parametrize('dimension', ['medical_history', 'family_history'])
async def test_full_answer_evidence_preserves_each_diseases_polarity(dimension):
    answer = '我没有糖尿病，但我有高血压'
    result = await extract(
        source(answer, dimension=dimension),
        candidate('condition.diagnosis', '糖尿病', answer),
        candidate('condition.diagnosis', '高血压', answer),
    )
    assert len(result) == 2
    denial = next(item for item in result if item.fact_type == HISTORY_STATUS)
    positive = next(item for item in result if item.fact_type == 'condition.diagnosis')
    assert denial.value['conditions'] == ['糖尿病']
    assert denial.value['presence'] == 'absent'
    assert denial.value['description'] == '我没有糖尿病'
    assert positive.value == '高血压'
    assert positive.evidence_text == '我有高血压'


@pytest.mark.asyncio
@pytest.mark.parametrize('subject,hint,prefix', [('self', '本人', '我'), ('blood_relative', '父亲', '父亲')])
async def test_subject_inheritance_does_not_inherit_another_diseases_denial(subject, hint, prefix):
    answer = f'{prefix}没有糖尿病，但患有高血压'
    fact_type = 'family_history.condition' if subject == 'blood_relative' else 'condition.diagnosis'
    result = await extract(source(answer), candidate(fact_type, '高血压', answer, subject=subject, hint=hint))
    assert len(result) == 1
    assert result[0].fact_type == fact_type
    assert result[0].value == '高血压'
    assert result[0].subject_hint == hint


@pytest.mark.asyncio
@pytest.mark.parametrize('dimension', ['medical_history', 'family_history'])
async def test_full_answer_evidence_resolves_repeated_names_by_real_subject(dimension):
    answer = '父亲有糖尿病，我没有糖尿病'
    result = await extract(
        source(answer, dimension=dimension),
        candidate('family_history.condition', '糖尿病', answer, subject='blood_relative', hint='父亲'),
        candidate('condition.diagnosis', '糖尿病', answer),
    )
    assert len(result) == 2
    father = next(item for item in result if item.fact_type == 'family_history.condition')
    self_denial = next(item for item in result if item.fact_type == HISTORY_STATUS)
    assert father.value == '糖尿病' and father.subject_hint == '父亲'
    assert father.evidence_text == '父亲有糖尿病'
    assert self_denial.value['presence'] == 'absent'
    assert self_denial.value['conditions'] == ['糖尿病']
    assert self_denial.value['description'] == '我没有糖尿病'


@pytest.mark.asyncio
async def test_conflicting_same_entity_same_subject_is_not_a_positive_diagnosis():
    answer = '我没有糖尿病，但我有糖尿病'
    result = await extract(source(answer), candidate('condition.diagnosis', '糖尿病', answer))
    assert len(result) == 1
    assert result[0].fact_type == HISTORY_STATUS
    assert result[0].value['presence'] == 'conflicting'
    assert result[0].value['description'] == answer


@pytest.mark.asyncio
async def test_resolved_symptom_keeps_source_time_and_state_in_an_object():
    answer = '去年头痛，今年好了'
    value = {'name': '头痛', 'description': answer, 'status': 'past'}
    result = await extract(
        source(answer, dimension='symptoms'), candidate('symptom.reported', value, answer, name='头痛')
    )
    assert len(result) == 1
    assert result[0].value_type.value == 'object'
    assert result[0].value['name'] == '头痛'
    assert result[0].value['description'] == answer
    assert result[0].value['status'] == 'resolved'
    assert result[0].evidence_text == answer


@pytest.mark.asyncio
@pytest.mark.parametrize('answer,name', [('以前有过肺炎，已经好了', '肺炎'), ('去年确诊糖尿病', '糖尿病')])
@pytest.mark.parametrize('fact_type', ['condition.diagnosis', HISTORY_STATUS])
async def test_past_disease_is_present_history_with_qualified_time_not_current_diagnosis(answer, name, fact_type):
    value = name
    if fact_type == HISTORY_STATUS:
        value = {
            'presence': 'present',
            'specificity': 'specific',
            'description': answer,
            'conditions': [name],
            'relationship': None,
        }
    result = await extract(source(answer), candidate(fact_type, value, answer, name=name))
    assert len(result) == 1
    assert result[0].fact_type == HISTORY_STATUS
    assert result[0].value['description'] == answer
    assert result[0].value['conditions'] == [name]
    assert result[0].value['presence'] == 'present'
    assert result[0].value['specificity'] == 'specific'


@pytest.mark.asyncio
@pytest.mark.parametrize('answer,name', [('父亲以前有过肺炎，已经好了', '肺炎'), ('父亲去年确诊糖尿病', '糖尿病')])
async def test_past_known_family_disease_preserves_relative_subject_path_and_time(answer, name):
    result = await extract(
        source(answer, dimension='family_history'),
        candidate('family_history.condition', name, answer, subject='blood_relative', hint='父亲'),
    )
    assert len(result) == 1
    assert result[0].fact_type == 'family_history.condition'
    assert result[0].subject_kind_hint.value == 'blood_relative'
    assert result[0].subject_hint == '父亲'
    assert result[0].value_type.value == 'text'
    assert result[0].value == answer


@pytest.mark.asyncio
async def test_past_known_father_history_persists_on_father_once_and_is_read_with_time():
    answer = '父亲去年确诊糖尿病'
    record = source(answer, dimension='family_history')
    runtime, _ = _runtime(
        {'candidates': [candidate('family_history.condition', '糖尿病', answer, subject='blood_relative', hint='父亲')]}
    )
    subject = ReportIngestionFixture.subject()
    repository = FakeProfileRepository(subject)
    profile_service = FakeRecordProfileService(subject, repository=repository)
    sequence = 0

    def next_id(prefix):
        nonlocal sequence
        sequence += 1
        return f'{prefix}-father-history-{sequence}'

    processor = RecordProfileProcessor(
        store=FakeRecordStore(record),
        repository=repository,
        profile_service=profile_service,
        agent_hub=runtime.hub,
        clock=lambda: RecordFixture.now,
        id_factory=next_id,
    )
    await processor.process(RecordFixture.job())
    replay = await processor.process(RecordFixture.job())
    facts = [fact for fact in repository.facts if fact.fact_type == 'family_history.condition']
    assert len(facts) == 1 and replay.fact_count == 0
    assert len(profile_service.subject_calls) == 1
    assert facts[0].subject_id != subject.subject_id
    assert facts[0].value == answer
    assert facts[0].status is FactStatus.PENDING
    assert facts[0].assertion_level is AssertionLevel.USER_REPORTED
    assert not any(fact.fact_type == FAMILY_REPORTED for fact in repository.facts)
    context = await RepositoryHealthProfileChatMemoryProvider(repository).build_context('user-1')
    assert answer in context
    confirmed = tuple(fact.model_copy(update={'status': FactStatus.ACTIVE}) for fact in facts)
    summary = build_profile_summary(facts=confirmed, profile_version=1, generated_at=RecordFixture.now)
    assert answer in summary['overall']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'answer,evidence,expected,conditions',
    [
        ('没有糖尿病', '糖尿病', 'absent', ['糖尿病']),
        ('我没有糖尿病', '我没有糖尿病', 'absent', ['糖尿病']),
        ('不清楚有没有糖尿病', '糖尿病', 'unknown', ['糖尿病']),
        ('有慢性病但记不清病名', '有慢性病但记不清病名', 'present', []),
        ('有病史但忘了病名', '有病史但忘了病名', 'present', []),
    ],
)
async def test_unsupported_positive_diagnosis_becomes_a_source_backed_history_observation(
    answer, evidence, expected, conditions
):
    value = '糖尿病' if conditions else '某种慢性病'
    record = source(answer)
    original = deepcopy(record.session)
    result = await extract(record, candidate('condition.diagnosis', value, evidence))
    assert len(result) == 1
    actual = result[0]
    assert actual.fact_type == HISTORY_STATUS
    assert actual.value['presence'] == expected
    assert actual.value['conditions'] == conditions
    assert actual.value['specificity'] == ('specific' if conditions else 'unspecified')
    assert actual.evidence_text in answer
    assert actual.subject_id == 'subject-self'
    assert actual.assertion_level is AssertionLevel.USER_REPORTED
    assert actual.source.pointer == '/qaHistory/0/answer'
    assert record.session == original


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', ['想了解糖尿病', '我担心有糖尿病', '如果患有糖尿病会怎样', '怀疑自己有糖尿病'])
async def test_interest_and_hypothetical_conditions_do_not_become_diagnoses(answer):
    result = await extract(source(answer, dimension='goal'), candidate('condition.diagnosis', '糖尿病', '糖尿病'))
    assert len(result) == 1
    assert result[0].fact_type == 'goal.health'
    assert result[0].value == answer


@pytest.mark.asyncio
async def test_bare_disease_selected_as_a_goal_is_not_a_diagnosis():
    result = await extract(source('糖尿病', dimension='goal'), candidate('condition.diagnosis', '糖尿病', '糖尿病'))
    assert len(result) == 1 and result[0].fact_type == 'goal.health'
    assert result[0].value == '糖尿病'


@pytest.mark.asyncio
async def test_observation_type_cannot_smuggle_an_interest_into_a_positive_diagnosis():
    answer = '想了解糖尿病'
    value = {
        'presence': 'present',
        'specificity': 'specific',
        'description': answer,
        'conditions': ['糖尿病'],
        'relationship': None,
    }
    result = await extract(source(answer, dimension='goal'), candidate(HISTORY_STATUS, value, answer))
    assert len(result) == 1 and result[0].fact_type == 'goal.health'


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', ['身体不舒服', '最近身体不适', '有某种症状但说不清楚'])
async def test_vague_complaint_keeps_presence_without_inventing_a_symptom(answer):
    result = await extract(source(answer, dimension='goal'), candidate('symptom.reported', '身体不舒服', answer))
    assert len(result) == 1
    assert result[0].fact_type == 'goal.health'
    assert result[0].value['specificity'] == 'unspecified'
    assert result[0].value['entities'] == []
    assert result[0].value['description'] == answer
    assert result[0].value['presence'] == 'present'


@pytest.mark.asyncio
@pytest.mark.parametrize('answer,presence', [('没有头痛', 'absent'), ('不清楚有没有头痛', 'unknown')])
async def test_specific_symptom_denial_and_uncertainty_keep_their_scope(answer, presence):
    result = await extract(source(answer, dimension='symptoms'), candidate('symptom.reported', '头痛', '头痛'))
    assert len(result) == 1
    assert result[0].fact_type == 'symptom.reported'
    assert result[0].value == {'presence': presence, 'specificity': 'specific', 'description': answer, 'name': '头痛'}


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['糖尿病', '脂肪肝', '银屑病', '强直性脊柱炎'])
async def test_free_form_named_disease_is_not_limited_to_shortcut_candidates(name):
    answer = f'医生确诊我患有{name}'
    result = await extract(source(answer), candidate('condition.diagnosis', name, answer))
    assert len(result) == 1
    assert result[0].fact_type == 'condition.diagnosis'
    assert result[0].value == name


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', ['糖尿病', '家里有人患糖尿病', '血亲中有糖尿病'])
async def test_family_disease_without_identified_relative_uses_an_aggregate_observation(answer):
    result = await extract(
        source(answer, dimension='family_history'),
        candidate('family_history.condition', '糖尿病', answer, subject='blood_relative', hint='血亲，具体亲属未说明'),
    )
    assert len(result) == 1
    assert result[0].fact_type == FAMILY_REPORTED
    assert result[0].subject_id == 'subject-self'
    assert result[0].subject_kind_hint.value == 'self'
    assert result[0].value['conditions'] == ['糖尿病']
    assert result[0].value['relationship'] is None


@pytest.mark.asyncio
async def test_known_father_remains_a_real_relative_and_is_not_duplicated_as_self():
    answer = '父亲患有糖尿病'
    result = await extract(
        source(answer, dimension='family_history'),
        candidate('family_history.condition', '糖尿病', answer, subject='blood_relative', hint='父亲'),
    )
    assert len(result) == 1
    assert result[0].fact_type == 'family_history.condition'
    assert result[0].subject_id is None
    assert result[0].subject_hint == '父亲'


@pytest.mark.asyncio
async def test_negative_relative_statement_cannot_become_positive_family_history():
    answer = '父亲没有糖尿病'
    result = await extract(
        source(answer, dimension='family_history'),
        candidate('family_history.condition', '糖尿病', answer, subject='blood_relative', hint='父亲'),
    )
    assert len(result) == 1
    assert result[0].fact_type == FAMILY_REPORTED
    assert result[0].value['presence'] == 'absent'
    assert result[0].value['relationship'] == '父亲'
    assert result[0].value['conditions'] == ['糖尿病']


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', ['家人中老婆有糖尿病', '朋友患糖尿病', '配偶患糖尿病'])
async def test_non_blood_relatives_are_not_promoted_to_blood_family_history(answer):
    item = candidate('family_history.condition', '糖尿病', answer, subject='blood_relative', hint='血亲')
    assert await extract(source(answer, dimension='family_history'), item) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize('answer,presence', [('我有糖尿病', 'present'), ('我没有糖尿病', 'absent')])
@pytest.mark.parametrize('fact_type', ['family_history.condition', FAMILY_REPORTED])
async def test_explicit_self_history_overrides_the_family_question_subject(answer, presence, fact_type):
    value = '糖尿病'
    subject, hint = 'blood_relative', '血亲'
    if fact_type == FAMILY_REPORTED:
        value = {
            'presence': 'present',
            'specificity': 'specific',
            'description': answer,
            'conditions': ['糖尿病'],
            'relationship': None,
        }
        subject, hint = 'self', '本人'
    result = await extract(
        source(answer, dimension='family_history'), candidate(fact_type, value, answer, subject=subject, hint=hint)
    )
    assert len(result) == 1
    assert result[0].subject_kind_hint.value == 'self'
    assert result[0].fact_type == ('condition.diagnosis' if presence == 'present' else HISTORY_STATUS)
    if presence == 'absent':
        assert result[0].value['presence'] == 'absent'


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', ['未发现糖尿病', '未见糖尿病', '不存在糖尿病', '糖尿病没有', '糖尿病未确诊'])
async def test_prefix_or_suffix_denial_cannot_become_a_positive_diagnosis(answer):
    result = await extract(source(answer), candidate('condition.diagnosis', '糖尿病', '糖尿病'))
    assert len(result) == 1 and result[0].fact_type == HISTORY_STATUS
    assert result[0].value['presence'] == 'absent'


@pytest.mark.asyncio
async def test_a_normal_screening_result_does_not_establish_a_diagnosis():
    answer = '我糖尿病筛查正常'
    result = await extract(source(answer), candidate('condition.diagnosis', '糖尿病', '糖尿病'))
    assert len(result) == 1 and result[0].fact_type == HISTORY_STATUS
    assert result[0].value['presence'] == 'unknown'
    assert result[0].value['description'] == answer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'answer',
    ['我确诊糖尿病后没有用药', '我有糖尿病目前没有治疗', '医生诊断是糖尿病', '糖尿病确诊5年'],
)
async def test_explicit_diagnosis_survives_independent_treatment_denial_and_common_wording(answer):
    result = await extract(source(answer), candidate('condition.diagnosis', '糖尿病', answer))
    assert len(result) == 1
    assert result[0].fact_type == 'condition.diagnosis'
    assert result[0].value == '糖尿病'
    assert result[0].evidence_text == answer


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', ['医生没有诊断是糖尿病', '医生未诊断为糖尿病', '医生从未确诊糖尿病'])
async def test_denied_diagnosis_wording_is_not_mistaken_for_the_new_positive_forms(answer):
    result = await extract(source(answer), candidate('condition.diagnosis', '糖尿病', answer))
    assert len(result) == 1
    assert result[0].fact_type == HISTORY_STATUS
    assert result[0].value['presence'] == 'absent'
    assert result[0].value['conditions'] == ['糖尿病']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'dimension,fact_type,answer,relationship',
    [
        ('medical_history', HISTORY_STATUS, '我没有糖尿病', None),
        ('family_history', FAMILY_REPORTED, '父亲没有糖尿病', '父亲'),
    ],
)
async def test_empty_model_conditions_do_not_erase_the_named_scope_of_a_history_denial(
    dimension, fact_type, answer, relationship
):
    value = {
        'presence': 'absent',
        'specificity': 'unspecified',
        'description': answer,
        'conditions': [],
        'relationship': None,
    }
    result = await extract(source(answer, dimension=dimension), candidate(fact_type, value, answer))
    assert len(result) == 1 and result[0].fact_type == fact_type
    assert result[0].value['presence'] == 'absent'
    assert result[0].value['specificity'] == 'specific'
    assert result[0].value['conditions'] == ['糖尿病']
    assert result[0].value['relationship'] == relationship


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', ['我不知道每天喝多少水', '我记不清一天喝几杯咖啡', '我没有运动习惯'])
async def test_lifestyle_statements_do_not_become_history_states_from_the_question_alone(answer):
    assert await extract(source(answer)) == ()


@pytest.mark.asyncio
async def test_a_specific_denial_omitted_by_the_model_keeps_its_named_scope():
    result = await extract(source('我没有糖尿病'))
    assert len(result) == 1
    assert result[0].value['presence'] == 'absent'
    assert result[0].value['conditions'] == ['糖尿病']


@pytest.mark.asyncio
async def test_a_family_unknown_clause_does_not_make_explicit_self_history_unknown():
    assert await extract(source('我有糖尿病，家里情况不清楚')) == ()
    result = await extract(source('我有糖尿病，家里情况不清楚', dimension='family_history'))
    assert len(result) == 1 and result[0].fact_type == FAMILY_REPORTED
    assert result[0].value['description'] == '家里情况不清楚'
    assert result[0].value['presence'] == 'unknown'


@pytest.mark.asyncio
async def test_lifestyle_dimension_prevents_topic_guessing_from_family_words_in_question():
    record = source('不清楚', dimension='symptoms', question='父母在家时您的就寝时间会变化吗？')
    record.session['qaHistory'][0]['question']['intakeDimension'] = 'sleep'
    assert await extract(record) == ()


@pytest.mark.asyncio
async def test_legacy_lifestyle_question_with_family_words_is_not_a_family_history_question():
    record = source('不清楚', dimension='symptoms', question='父母在家时您的就寝时间会变化吗？')
    record.session['qaHistory'][0]['question'].pop('intakeDimension')
    assert await extract(record) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize('dimension', ['medical_history', 'family_history'])
@pytest.mark.parametrize('answer', ['没有需要补充的内容', '目前记不清', '没有'])
async def test_known_topic_supplement_controls_are_not_new_history_denials_or_unknowns(dimension, answer):
    record = source(answer, dimension=dimension, question='关于已了解的病史，您还有需要补充或更正的内容吗？')
    assert await extract(record) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize('dimension', ['medical_history', 'family_history'])
@pytest.mark.parametrize('answer', [['没有', '糖尿病'], ['不清楚', '高血压'], ['没有', '不清楚']])
async def test_exclusive_state_mixed_with_another_choice_is_preserved_as_conflict(dimension, answer):
    items = [candidate('condition.diagnosis', '糖尿病', '糖尿病')] if '糖尿病' in answer else []
    result = await extract(source(answer, dimension=dimension), *items)
    assert len(result) == 1
    assert result[0].fact_type == (FAMILY_REPORTED if dimension == 'family_history' else HISTORY_STATUS)
    assert result[0].value['presence'] == 'conflicting'
    assert result[0].evidence_text.startswith('[')
    assert result[0].source.pointer == '/qaHistory/0/answer'


@pytest.mark.asyncio
@pytest.mark.parametrize('dimension', ['medical_history', 'family_history'])
@pytest.mark.parametrize(
    'answer,presence', [('没有', 'absent'), ('不清楚', 'unknown'), ('有病史但忘了病名', 'present')]
)
async def test_omitted_explicit_history_state_is_preserved_without_an_extra_model_call(dimension, answer, presence):
    runtime, gateway = _runtime({'candidates': []})
    result = await RecordAnswerCandidateExtractor(runtime.hub).extract(
        source(answer, dimension=dimension), subject_id='subject-self'
    )
    assert len(result) == 1
    assert result[0].fact_type == (FAMILY_REPORTED if dimension == 'family_history' else HISTORY_STATUS)
    assert result[0].value['presence'] == presence
    assert result[0].value['conditions'] == []
    assert result[0].value['description'] == answer
    assert len(gateway.complete_requests) == 1


@pytest.mark.asyncio
async def test_unmentioned_history_is_not_synthesized_from_a_lifestyle_answer():
    record = source('每周跑步三次', dimension='symptoms', question='您通常怎样运动？')
    assert await extract(record) == ()


@pytest.mark.asyncio
async def test_aggregate_family_fact_is_written_pending_without_creating_a_relative_and_replays_once():
    answer = '家里有人患糖尿病'
    record = source(answer, dimension='family_history')
    runtime, _ = _runtime(
        {'candidates': [candidate('family_history.condition', '糖尿病', answer, subject='blood_relative', hint='血亲')]}
    )
    subject = ReportIngestionFixture.subject()
    repository = FakeProfileRepository(subject)
    profile_service = FakeRecordProfileService(subject, repository=repository)
    sequence = 0

    def next_id(prefix):
        nonlocal sequence
        sequence += 1
        return f'{prefix}-observation-{sequence}'

    processor = RecordProfileProcessor(
        store=FakeRecordStore(record),
        repository=repository,
        profile_service=profile_service,
        agent_hub=runtime.hub,
        clock=lambda: RecordFixture.now,
        id_factory=next_id,
    )
    await processor.process(RecordFixture.job())
    replay = await processor.process(RecordFixture.job())
    facts = [fact for fact in repository.facts if fact.fact_type == FAMILY_REPORTED]
    assert len(facts) == 1
    assert replay.fact_count == 0
    assert facts[0].subject_id == subject.subject_id
    assert facts[0].status is FactStatus.PENDING
    assert facts[0].assertion_level is AssertionLevel.USER_REPORTED
    assert facts[0].sources[0].pointer == '/qaHistory/0/answer'
    assert profile_service.subject_calls == []
