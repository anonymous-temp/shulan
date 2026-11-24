"""The extractor resolves a supplied disease within original coordinated clauses."""

import json

import pytest

from agents.graphs.health_profile.focus import clinical_anchor
from health_profile import AssertionLevel
from health_profile import FactStatus
from health_profile import RepositoryHealthProfileChatMemoryProvider
from health_profile.record_observations import FAMILY_REPORTED
from health_profile.record_observations import HISTORY_STATUS
from tests.unit.health_profile.test_record_answer_observations import candidate
from tests.unit.health_profile.test_record_answer_observations import extract
from tests.unit.health_profile.test_record_answer_observations import source
from tests.unit.health_profile.test_record_meaning_end_to_end import ingest


@pytest.mark.asyncio
@pytest.mark.parametrize('join', ['和', '及', '以及', '、'])
@pytest.mark.parametrize('names', [('糖尿病', '高血压'), ('糖尿病', '高血压', '甲状腺功能减退症')])
@pytest.mark.parametrize('predicate,presence', [('患有', 'present'), ('没有', 'absent'), ('不清楚有没有', 'unknown')])
async def test_only_last_model_candidate_inherits_a_shared_predicate(join, names, predicate, presence):
    answer = '我' + predicate + join.join(names)
    name = names[-1]
    result = await extract(source(answer), candidate('condition.diagnosis', name, name))
    matching = [item for item in result if item.display_name == name or name in item.value.get('conditions', [])]
    assert len(matching) == 1
    item = matching[0]
    if presence == 'present':
        assert item.fact_type == 'condition.diagnosis'
        assert item.value == name
    else:
        assert item.fact_type == HISTORY_STATUS
        assert item.value['presence'] == presence
        assert item.value['conditions'] == [name]
    assert item.evidence_text == answer
    assert item.assertion_level is AssertionLevel.USER_REPORTED
    assert item.source.pointer == '/qaHistory/0/answer'
    assert not any(item.fact_type == 'condition.diagnosis' and item.value in names[:-1] for item in result)


@pytest.mark.asyncio
@pytest.mark.parametrize('dimension', ['medical_history', 'goal'])
async def test_each_extracted_disease_uses_the_same_shared_assertion(dimension):
    answer = '我患有糖尿病、高血压以及甲状腺功能减退症'
    names = ['糖尿病', '高血压', '甲状腺功能减退症']
    result = await extract(
        source(answer, dimension=dimension), *(candidate('condition.diagnosis', n, n) for n in names)
    )
    assert [(item.fact_type, item.value) for item in result] == [('condition.diagnosis', name) for name in names]


@pytest.mark.asyncio
async def test_predicate_characters_within_the_target_name_are_not_reinterpreted():
    answer = '我患有慢性无菌性前列腺炎和高血压'
    result = await extract(source(answer), candidate('condition.diagnosis', '慢性无菌性前列腺炎', '慢性无菌性前列腺炎'))
    assert len(result) == 1
    assert result[0].fact_type == 'condition.diagnosis'
    assert result[0].value == '慢性无菌性前列腺炎'


@pytest.mark.asyncio
@pytest.mark.parametrize('predicate', ['没有', '未诊断为', '从未被医生确诊'])
async def test_predicate_characters_in_a_previous_disease_cannot_erase_the_denial(predicate):
    answer = '我' + predicate + '有机磷中毒和高血压'
    result = await extract(source(answer), candidate('condition.diagnosis', '高血压', '高血压'))
    assert len(result) == 1
    assert result[0].fact_type == HISTORY_STATUS
    assert result[0].value['presence'] == 'absent'
    assert result[0].value['conditions'] == ['高血压']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'answer,name,presence',
    [
        ('我没有糖尿病但患有高血压和脂肪肝', '脂肪肝', 'present'),
        ('我患有糖尿病但没有高血压和脂肪肝', '脂肪肝', 'absent'),
        ('我没有糖尿病和患有高血压', '高血压', 'present'),
        ('我患有糖尿病以及没有高血压', '高血压', 'absent'),
        ('我没有糖尿病以及患有高血压', '糖尿病', 'absent'),
        ('我患有糖尿病和高血压，记不清确诊时间', '高血压', 'present'),
        ('我确诊糖尿病和高血压后没有治疗', '高血压', 'present'),
    ],
)
async def test_new_predicates_and_time_uncertainty_do_not_change_other_diseases(answer, name, presence):
    result = await extract(source(answer), candidate('condition.diagnosis', name, name))
    selected = [
        item for item in result if item.fact_type == 'condition.diagnosis' or name in item.value.get('conditions', [])
    ]
    assert len(selected) == 1
    assert selected[0].fact_type == ('condition.diagnosis' if presence == 'present' else HISTORY_STATUS)
    if presence != 'present':
        assert selected[0].value['presence'] == presence


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', ['父亲患有糖尿病和我患有高血压', '父亲没有糖尿病以及我患有高血压'])
async def test_explicit_subject_switch_cannot_transfer_fathers_disease_to_self(answer):
    result = await extract(
        source(answer),
        candidate('condition.diagnosis', '糖尿病', '糖尿病'),
        candidate('condition.diagnosis', '高血压', '高血压'),
    )
    assert [(item.fact_type, item.value) for item in result] == [('condition.diagnosis', '高血压')]
    assert result[0].evidence_text == '我患有高血压'


@pytest.mark.asyncio
@pytest.mark.parametrize('predicate,presence', [('患有', 'present'), ('没有', 'absent'), ('不清楚有没有', 'unknown')])
async def test_shared_family_assertion_stays_on_the_relative(predicate, presence):
    answer = '父亲' + predicate + '糖尿病和高血压'
    result = await extract(
        source(answer, dimension='family_history'),
        candidate('family_history.condition', '高血压', '高血压', subject='blood_relative', hint='父亲'),
    )
    selected = [
        item
        for item in result
        if item.fact_type == 'family_history.condition' or '高血压' in item.value.get('conditions', [])
    ]
    assert len(selected) == 1
    item = selected[0]
    if presence == 'present':
        assert item.fact_type == 'family_history.condition'
        assert item.subject_kind_hint.value == 'blood_relative'
    else:
        assert item.fact_type == FAMILY_REPORTED
        assert item.value['presence'] == presence
        assert item.value['relationship'] == '父亲'
    assert not any(item.fact_type == 'condition.diagnosis' for item in result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'answer',
    [
        '我想了解糖尿病和高血压',
        '我担心患有糖尿病和高血压',
        '如果患有糖尿病和高血压怎么办',
        '我接受糖尿病和高血压筛查',
        '我没有治疗糖尿病和高血压',
        '我未检查糖尿病和高血压',
        '我有糖尿病和高血压的筛查记录',
        '我有糖尿病和高血压家族史',
        '我有糖尿病和高血压的治疗安排',
    ],
)
async def test_coordinated_questions_tests_and_treatments_do_not_assert_disease(answer):
    result = await extract(source(answer), candidate('condition.diagnosis', '高血压', '高血压'))
    assert not any(item.fact_type == 'condition.diagnosis' for item in result)
    assert not any(item.fact_type == HISTORY_STATUS and item.value['presence'] == 'absent' for item in result)


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['糖尿病', '高血压'])
@pytest.mark.parametrize(
    'answer',
    ['我有糖尿病和高血压的可能', '我有糖尿病和高血压还不确定是否存在', '我有糖尿病和高血压不一定是真的'],
)
async def test_list_tail_uncertainty_cannot_disappear_when_only_one_entity_is_extracted(answer, name):
    result = await extract(source(answer), candidate('condition.diagnosis', name, name))
    assert not any(item.fact_type == 'condition.diagnosis' for item in result)
    assert all(item.evidence_text == answer for item in result)


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', ['我以前患有糖尿病和高血压', '我曾经患有糖尿病和高血压，现在已经康复'])
async def test_coordinated_historical_or_resolved_disease_keeps_history_observation(answer):
    result = await extract(source(answer), candidate('condition.diagnosis', '高血压', '高血压'))
    assert len(result) == 1
    assert result[0].fact_type == HISTORY_STATUS
    assert result[0].value['presence'] == 'present'
    assert result[0].value['conditions'] == ['高血压']
    assert result[0].evidence_text == answer


@pytest.mark.asyncio
async def test_repeated_entity_with_conflicting_shared_assertions_is_not_positive():
    answer = '我没有糖尿病和高血压，但我患有高血压'
    result = await extract(source(answer), candidate('condition.diagnosis', '高血压', '高血压'))
    assert len(result) == 1
    assert result[0].fact_type == HISTORY_STATUS
    assert result[0].value['presence'] == 'conflicting'


@pytest.mark.asyncio
@pytest.mark.parametrize('predicate,presence', [('患有', 'present'), ('没有', 'absent'), ('不清楚有没有', 'unknown')])
async def test_shared_assertion_survives_persistence_replay_and_chat(predicate, presence):
    answer = '我' + predicate + '糖尿病和高血压'
    repository, _, replay = await ingest(source(answer), [candidate('condition.diagnosis', '高血压', '高血压')])
    facts = [
        fact
        for fact in repository.facts
        if fact.value == '高血压' or isinstance(fact.value, dict) and '高血压' in fact.value.get('conditions', [])
    ]
    assert len(facts) == 1
    assert replay.fact_count == 0
    fact = facts[0]
    assert fact.status is FactStatus.PENDING
    assert fact.assertion_level is AssertionLevel.USER_REPORTED
    assert fact.subject_id == repository.subject.subject_id
    assert fact.sources[0].pointer == '/qaHistory/0/answer'
    context = await RepositoryHealthProfileChatMemoryProvider(repository).build_context('user-1')
    rows = [json.loads(line) for line in context.splitlines() if line.startswith('{')]
    row = next(row for row in rows if row['factType'] == fact.fact_type)
    assert clinical_anchor({**row, 'category': fact.category.value}) == (presence == 'present')
    if presence != 'present':
        assert row['value']['presence'] == presence
        assert row['value']['description'] == answer
