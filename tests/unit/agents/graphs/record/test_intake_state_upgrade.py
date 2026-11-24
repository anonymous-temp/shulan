"""Multi-complaint coverage and lazy upgrades do not change stored snapshots."""

from copy import deepcopy

import pytest

from modules.record.application import flow
from modules.record.application import intake


def gap(**changes):
    return flow.InformationGap(
        **{
            'topic': '腹胀与食欲变化',
            'aspect': 'symptoms',
            'dimension': 'onset',
            'scope': 'complaint',
            'reason': '明确各项变化的出现时间',
            'informationNeeded': '这些变化分别从什么时候开始',
            **changes,
        }
    )


def observation(text, identity=None, *, primary=False):
    return {
        'id': identity,
        'text': text,
        'primary': primary,
        'evidence': [{'source': '本次回答', 'quote': text}],
    }


def fact(indexes, *, dimension='onset', status='known', summary='近两周开始', scope='complaint'):
    return {
        'dimension': dimension,
        'scope': scope,
        'complaintIndexes': indexes,
        'status': status,
        'summary': summary,
        'evidence': [{'source': '本次回答', 'quote': summary}],
    }


def understanding(*, complaints=None, facts=(), preferred=None, priority=False):
    return intake.IntakeUnderstanding(
        complaints=complaints or [observation('腹胀'), observation('食欲变化')],
        facts=list(facts),
        preferredIndex=preferred,
        priorityClarificationNeeded=priority,
    )


def flow_state():
    return {'intake': intake.new_intake(), 'intakeCount': 1, 'focus': None}


def validate(parsed, state):
    intake.validate_understanding(parsed, {'qaFlow': state, 'qaHistory': []}, '腹胀与食欲变化', {})


def test_old_v2_intake_is_upgraded_as_a_copy_without_inventing_preference():
    old = {
        'complaints': [{'id': 'old', 'text': '腹胀', 'evidence': []}],
        'primaryId': 'old',
        'priorityClarificationNeeded': True,
        'facts': {'old:onset': {'dimension': 'onset', 'scope': 'primary', 'status': 'unknown'}},
        'asked': ['general:priority', 'old:onset'],
        'conflictAsked': [],
        'detailStreak': 1,
    }
    state = {'version': 2, 'intake': old, 'intakeCount': 2}
    before = deepcopy(state)
    upgraded = intake.normalize_state(state)
    assert state == before
    assert upgraded['schemaVersion'] == 3
    assert upgraded['focusIds'] == ['old']
    assert upgraded['preferredId'] is None
    assert upgraded['priorityAsked'] is True
    assert upgraded['facts']['old:onset']['status'] == 'unknown'
    upgraded['complaints'][0]['text'] = '修改草稿'
    assert state == before


def test_old_snapshot_with_user_volunteered_priority_does_not_reopen_priority_question():
    state = {
        'intake': {
            'complaints': [{'id': 'old', 'text': '腹胀', 'evidence': []}],
            'primaryId': 'old',
            'facts': {'old:priority': {'dimension': 'priority', 'scope': 'primary', 'status': 'unknown'}},
            'asked': [],
        },
    }
    upgraded = intake.normalize_state(state)
    assert upgraded['priorityAsked'] is True
    assert state['intake']['asked'] == []


def test_first_turn_maps_new_complaints_and_their_distinct_facts_to_stable_ids():
    state = flow_state()
    parsed = understanding(facts=[fact([0], summary='腹胀两周'), fact([1], summary='食欲变化三天')])
    validate(parsed, state)
    result = intake.merge_understanding(state, parsed)
    first, second = [item['id'] for item in result['complaints']]
    assert result['facts'][f'{first}:onset']['summary'] == '腹胀两周'
    assert result['facts'][f'{second}:onset']['summary'] == '食欲变化三天'
    assert result['preferredId'] is None
    assert result['focusIds'] == [first, second]
    gaps = intake.resolve_gaps(state, parsed, [gap(complaintIndexes=[1])])
    assert gaps[0].complaintIds == [second]


@pytest.mark.parametrize('indexes', [[-1], [2], [0, 0], []])
def test_complaint_fact_rejects_invalid_or_missing_targets(indexes):
    state = flow_state()
    parsed = understanding(facts=[fact(indexes)])
    with pytest.raises(ValueError):
        validate(parsed, state)


def test_general_fact_cannot_claim_a_complaint_target():
    parsed = understanding(facts=[fact([0], dimension='habits', scope='general')])
    with pytest.raises(ValueError):
        validate(parsed, flow_state())


def test_shared_and_specific_subfacts_are_merged_only_into_their_respective_targets():
    state = flow_state()
    shared = fact([0, 1], dimension='associated_symptoms', summary='没有发热')
    specific = fact([1], dimension='associated_symptoms', summary='食欲变化不伴随恶心')
    parsed = understanding(facts=[shared, specific, shared])
    validate(parsed, state)
    result = intake.merge_understanding(state, parsed)
    first, second = [item['id'] for item in result['complaints']]
    assert result['facts'][f'{first}:associated_symptoms']['summary'] == '没有发热'
    aggregate = result['facts'][f'{second}:associated_symptoms']
    assert aggregate['summary'] == '没有发热\n食欲变化不伴随恶心'
    assert aggregate['evidence'] == shared['evidence'] + specific['evidence']


@pytest.mark.parametrize(
    'dimension,summaries',
    [
        ('associated_symptoms', ['无腹痛', '无发热或便血']),
        ('goal', ['想了解和生活习惯的关系', '想知道是否适合散步', '想了解益生菌是否有帮助']),
    ],
)
def test_same_slot_subfacts_preserve_every_summary_and_unique_evidence(dimension, summaries):
    state = flow_state()
    facts = [fact([0], dimension=dimension, summary=summary) for summary in summaries]
    duplicate = deepcopy(facts[0])
    additional_evidence = {'source': '本次回答', 'quote': '补充说明：' + summaries[0]}
    duplicate['evidence'].append(additional_evidence)
    parsed = understanding(facts=[*facts, duplicate])
    validate(parsed, state)
    result = intake.merge_understanding(state, parsed)
    identity = result['complaints'][0]['id']
    aggregate = result['facts'][f'{identity}:{dimension}']
    assert aggregate['summary'] == '\n'.join(summaries)
    assert aggregate['evidence'] == [*([item['evidence'][0] for item in facts]), additional_evidence]
    assert aggregate['status'] == 'known'


@pytest.mark.parametrize('statuses', [('known', 'unknown'), ('known', 'conflicting'), ('unknown', 'declined')])
@pytest.mark.parametrize('first_indexes', [[0], [0, 1]])
def test_overlapping_subfacts_with_different_statuses_require_model_resolution(statuses, first_indexes):
    state = flow_state()
    parsed = understanding(facts=[fact(first_indexes, status=statuses[0]), fact([0], status=statuses[1])])
    with pytest.raises(ValueError, match='状态') as raised:
        validate(parsed, state)
    assert raised.value.code == 'duplicate_intake_fact'
    with pytest.raises(ValueError, match='状态'):
        intake.merge_understanding(state, parsed)
    assert state['intake']['facts'] == {}


def test_current_turn_aggregation_replaces_the_old_slot_without_appending_stale_information():
    state = flow_state()
    initial = intake.merge_understanding(state, understanding(facts=[fact([0], summary='之前说两周')]))
    identity = initial['complaints'][0]['id']
    parsed = understanding(facts=[fact([0], summary='更正为一个月'), fact([0], summary='考试前开始')])
    validate(parsed, state)
    result = intake.merge_understanding(state, parsed)
    aggregate = result['facts'][f'{identity}:onset']
    assert aggregate['summary'] == '更正为一个月\n考试前开始'
    assert [item['quote'] for item in aggregate['evidence']] == ['更正为一个月', '考试前开始']


@pytest.mark.parametrize('marked', [False, True])
def test_legacy_primary_fact_cannot_fan_out_to_multiple_current_complaints(marked):
    state = flow_state()
    initial = intake.merge_understanding(state, understanding())
    # The compatibility primaryId is only a projection of the first focus.
    assert initial['primaryId'] == initial['complaints'][0]['id']
    parsed = understanding(
        complaints=[observation(item['text'], item['id'], primary=marked) for item in initial['complaints']],
        facts=[fact([], scope='primary', summary='只有腹胀从两周前开始')],
    )
    before = deepcopy(state)
    with pytest.raises(ValueError, match='complaintIndexes') as raised:
        validate(parsed, state)
    assert raised.value.code == 'ambiguous_complaint_reference'
    assert state == before


def test_explicit_target_keeps_one_complaints_fact_separate_in_a_multi_complaint_turn():
    state = flow_state()
    parsed = understanding(facts=[fact([0], scope='primary', summary='腹胀从两周前开始')])
    validate(parsed, state)
    result = intake.merge_understanding(state, parsed)
    first, second = [item['id'] for item in result['complaints']]
    assert result['facts'][f'{first}:onset']['summary'] == '腹胀从两周前开始'
    assert f'{second}:onset' not in result['facts']


def test_legacy_primary_with_one_explicit_mark_still_targets_only_that_complaint():
    state = flow_state()
    parsed = understanding(
        complaints=[observation('腹胀'), observation('食欲变化', primary=True)],
        facts=[fact([], scope='primary', summary='食欲变化从三天前开始')],
    )
    validate(parsed, state)
    result = intake.merge_understanding(state, parsed)
    first, second = [item['id'] for item in result['complaints']]
    assert f'{first}:onset' not in result['facts']
    assert result['facts'][f'{second}:onset']['summary'] == '食欲变化从三天前开始'


def test_legacy_primary_gap_requires_indexes_when_current_targets_are_ambiguous():
    state = flow_state()
    parsed = understanding()
    intake.merge_understanding(state, parsed)
    with pytest.raises(ValueError, match='complaintIndexes'):
        intake.resolve_gaps(state, parsed, [gap(scope='primary')])


def test_legacy_primary_can_use_an_existing_single_focus_when_current_list_is_empty():
    state = flow_state()
    initial = intake.merge_understanding(state, understanding(complaints=[observation('腹胀')]))
    parsed = intake.IntakeUnderstanding(complaints=[], facts=[fact([], scope='primary')])
    validate(parsed, state)
    result = intake.merge_understanding(state, parsed)
    identity = initial['complaints'][0]['id']
    assert list(result['facts']) == [f'{identity}:onset']


def test_existing_complaint_id_survives_a_synonym_without_reasking_unknown():
    state = flow_state()
    original = understanding(facts=[fact([0], status='unknown', summary='记不清开始时间')])
    result = intake.merge_understanding(state, original)
    first, second = [item['id'] for item in result['complaints']]
    renamed = understanding(complaints=[observation('肚子胀', first), observation('食欲变化', second)])
    validate(renamed, state)
    intake.merge_understanding(state, renamed)
    gaps = intake.resolve_gaps(state, renamed, [gap(complaintIndexes=[0, 1])])
    eligible = intake.candidates(state, gaps)
    assert len(state['intake']['complaints']) == 2
    assert eligible[0].complaintIds == [second]
    assert gaps[0].complaintIds == [first, second]


def test_shared_gap_marks_each_target_asked_and_remains_deduplicated_when_split():
    state = flow_state()
    parsed = understanding()
    result = intake.merge_understanding(state, parsed)
    ids = [item['id'] for item in result['complaints']]
    gaps = intake.resolve_gaps(state, parsed, [gap(complaintIndexes=[0, 1])])
    intake.select_gap(state, intake.candidates(state, gaps))
    assert result['asked'] == [f'{identity}:onset' for identity in ids]
    assert result['focusIds'] == ids
    assert intake.candidates(state, intake.resolve_gaps(state, parsed, [gap(complaintIndexes=[1])])) == []


def test_common_background_covers_both_targets_but_specific_history_does_not():
    state = flow_state()
    parsed = understanding(facts=[fact([0], dimension='medical_history', summary='腹胀曾就诊')])
    result = intake.merge_understanding(state, parsed)
    ids = [item['id'] for item in result['complaints']]
    gaps = intake.resolve_gaps(state, parsed, [gap(dimension='medical_history', complaintIndexes=[0, 1])])
    assert intake.candidates(state, gaps)[0].complaintIds == [ids[1]]
    common = understanding(facts=[fact([], dimension='medical_history', scope='general')])
    intake.merge_understanding(state, common)
    assert intake.candidates(state, gaps) == []


def test_general_background_gap_still_covers_unanswered_target_after_one_specific_answer():
    state = flow_state()
    parsed = understanding(facts=[fact([0], dimension='medical_history', summary='腹胀曾就诊')])
    result = intake.merge_understanding(state, parsed)
    gaps = intake.resolve_gaps(state, parsed, [gap(scope='general', dimension='medical_history')])
    eligible = intake.candidates(state, gaps)
    assert eligible[0].complaintIds == [result['complaints'][1]['id']]
    target = intake.select_gap(state, eligible)
    assert target['complaints'] == [{'id': result['complaints'][1]['id'], 'text': '食欲变化'}]
    assert intake.candidates(state, gaps) == []


def test_priority_is_optional_and_cannot_block_other_unanswered_dimensions():
    state = flow_state()
    parsed = understanding(priority=True)
    intake.merge_understanding(state, parsed)
    gaps = intake.resolve_gaps(
        state, parsed, [gap(scope='general', dimension='priority'), gap(complaintIndexes=[0, 1])]
    )
    eligible = intake.candidates(state, gaps)
    assert [item.dimension for item in eligible] == ['priority', 'onset']
    intake.select_gap(state, eligible)
    assert state['intake']['priorityAsked'] is True
    assert [item.dimension for item in intake.candidates(state, gaps)] == ['onset']


def test_only_explicit_preferred_index_sets_preference_not_legacy_primary():
    state = flow_state()
    parsed = understanding(complaints=[observation('腹胀', primary=True), observation('食欲变化', primary=True)])
    validate(parsed, state)
    result = intake.merge_understanding(state, parsed)
    assert result['preferredId'] is None
    preferred = understanding(preferred=1)
    intake.merge_understanding(state, preferred)
    assert state['intake']['preferredId'] == state['intake']['complaints'][1]['id']
    intake.merge_understanding(state, understanding())
    assert state['intake']['preferredId'] == state['intake']['complaints'][1]['id']


@pytest.mark.parametrize('status', ['known', 'unknown', 'declined'])
def test_user_no_longer_prioritizing_one_complaint_clears_the_previous_preference(status):
    state = flow_state()
    intake.merge_understanding(state, understanding(preferred=0))
    result = intake.merge_understanding(
        state,
        understanding(
            facts=[
                fact(
                    [], dimension='priority', scope='general', status=status, summary='现在两个都想了解，没有哪个更重要'
                )
            ]
        ),
    )
    assert result['preferredId'] is None
    assert result['priorityAsked'] is True


@pytest.mark.parametrize('status', ['known', 'unknown', 'declined'])
@pytest.mark.parametrize('scope,indexes', [('general', []), ('complaint', [0])])
def test_user_supplied_priority_is_settled_without_reasking_after_a_scope_change(status, scope, indexes):
    state = flow_state()
    parsed = understanding(
        facts=[fact(indexes, dimension='priority', status=status, summary='两个都想了解，请你来决定顺序', scope=scope)],
        priority=True,
    )
    validate(parsed, state)
    result = intake.merge_understanding(state, parsed)
    assert result['priorityAsked'] is True
    followup = understanding(complaints=[observation('腰背不适')], priority=True)
    intake.merge_understanding(state, followup)
    gaps = intake.resolve_gaps(state, followup, [gap(scope='general', dimension='priority'), gap(complaintIndexes=[0])])
    assert [item.dimension for item in intake.candidates(state, gaps)] == ['onset']


def test_detail_streak_does_not_force_a_topic_change():
    state = flow_state()
    parsed = understanding()
    result = intake.merge_understanding(state, parsed)
    result['detailStreak'] = 2
    only = intake.resolve_gaps(state, parsed, [gap(complaintIndexes=[0])])
    assert len(intake.candidates(state, only)) == 1
    alternatives = intake.resolve_gaps(
        state, parsed, [gap(complaintIndexes=[0]), gap(dimension='goal', complaintIndexes=[1])]
    )
    assert [item.dimension for item in intake.candidates(state, alternatives)] == ['onset', 'goal']


def test_gap_uses_the_same_identity_mapping_when_an_existing_label_is_renamed():
    state = flow_state()
    first_turn = understanding()
    result = intake.merge_understanding(state, first_turn)
    first, second = [item['id'] for item in result['complaints']]
    parsed = understanding(
        complaints=[observation('食欲变化', first), observation('食欲变化')],
        facts=[fact([0], summary='两周'), fact([1], summary='三天')],
    )
    validate(parsed, state)
    result = intake.merge_understanding(state, parsed)
    resolved = intake.resolve_gaps(state, parsed, [gap(complaintIndexes=[1])])
    assert resolved[0].complaintIds == [second]
    assert result['facts'][f'{first}:onset']['summary'] == '两周'
    assert result['facts'][f'{second}:onset']['summary'] == '三天'


def test_missing_id_is_rejected_after_two_existing_complaints_acquire_the_same_label():
    state = flow_state()
    initial = intake.merge_understanding(state, understanding())
    first, second = [item['id'] for item in initial['complaints']]
    rename = understanding(complaints=[observation('食欲变化', first), observation('食欲变化', second)])
    validate(rename, state)
    intake.merge_understanding(state, rename)
    ambiguous = understanding(complaints=[observation('食欲变化')], facts=[fact([0], summary='三天')])
    before = deepcopy(state)
    with pytest.raises(ValueError, match='编号') as raised:
        validate(ambiguous, state)
    assert raised.value.code == 'ambiguous_complaint_identity'
    with pytest.raises(ValueError, match='编号'):
        intake.merge_understanding(state, ambiguous)
    assert state == before
    explicit = understanding(complaints=[observation('食欲变化', second)], facts=[fact([0], summary='三天')])
    validate(explicit, state)
    result = intake.merge_understanding(state, explicit)
    assert f'{first}:onset' not in result['facts']
    assert result['facts'][f'{second}:onset']['summary'] == '三天'


def test_model_cannot_forge_persisted_complaint_ids_in_a_gap():
    with pytest.raises(ValueError, match='Extra inputs'):
        gap(complaintIndexes=[0], complaintIds=['forged'])


@pytest.mark.parametrize('preferred', [-1, 2])
def test_preference_index_must_reference_a_current_complaint(preferred):
    with pytest.raises(ValueError):
        validate(understanding(preferred=preferred), flow_state())


def test_patient_projection_retains_other_complaints_coverage_with_attribution():
    state = flow_state()
    parsed = understanding(facts=[fact([0], summary='腹胀两周'), fact([1], summary='食欲变化三天')])
    result = intake.merge_understanding(state, parsed)
    result['focusIds'] = [result['complaints'][1]['id']]
    projection = intake.patient_projection(state)
    assert len(projection['已了解的信息']) == 2
    assert {item['困扰'][0]['困扰'] for item in projection['已了解的信息']} == {'腹胀', '食欲变化'}
    assert all(item['困扰'][0]['编号'] for item in projection['已了解的信息'])
