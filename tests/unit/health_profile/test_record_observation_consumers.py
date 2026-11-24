"""Record answer states remain background when read by profile and Chat consumers."""

import json
from datetime import timedelta

import pytest

from agents.graphs.health_profile.focus import clinical_anchor
from agents.graphs.health_profile.focus import default_topics
from agents.graphs.health_profile.focus import evidence_anchor
from agents.graphs.health_profile.focus import is_abnormal
from agents.graphs.health_profile.focus import source_flag
from health_profile.alerts import current_alert_facts
from health_profile.ingestion import FactTypeRegistry
from health_profile.ingestion import HealthFactPolicy
from health_profile.ingestion import PolicyDisposition
from health_profile.memory import RepositoryHealthProfileChatMemoryProvider
from health_profile.models import AssertionLevel
from health_profile.models import EffectivePeriod
from health_profile.models import FactSource
from health_profile.models import FactValueType
from health_profile.models import HealthSubject
from health_profile.models import MemoryCandidate
from health_profile.models import ProfileIngestionSourceType
from health_profile.models import SubjectKind
from health_profile.models import SubjectRelationship
from health_profile.models import SubjectStatus
from health_profile.projection import _build_fact_groups
from health_profile.summary import build_profile_summary
from tests.unit.health_profile.test_alert_selection import NOW
from tests.unit.health_profile.test_alert_selection import measurement
from tests.unit.health_profile.test_report_ingestion import FakeProfileRepository


def observation(presence='present', specificity='unspecified', conditions=(), **changes):
    value = {
        'presence': presence,
        'specificity': specificity,
        'description': '有病史但记不清病名',
        'conditions': list(conditions),
        'relationship': None,
    }
    value.update(changes)
    return value


def fact(identity='history', fact_type='condition.history_status', value=None, **changes):
    active = changes.get('status', 'active') == 'active'
    options = {
        'factType': fact_type,
        'category': 'condition' if fact_type.startswith('condition.') else 'family_history',
        'valueType': 'object',
        'value': observation() if value is None else value,
        'interpretation': None,
        'assertionLevel': 'user_confirmed' if active else 'user_reported',
        'verificationStatus': 'confirmed' if active else 'unconfirmed',
        'sources': [{'sourceType': 'record', 'sourceId': 'record-observations', 'sourceRevision': 1}],
    }
    options.update(changes)
    return measurement(
        identity,
        '病史说明' if fact_type.startswith('condition.') else '家族背景',
        **options,
    )


def subject(kind=SubjectKind.SELF):
    return HealthSubject(
        subject_id='self' if kind is SubjectKind.SELF else 'father',
        user_id='synthetic',
        kind=kind,
        display_name='本人' if kind is SubjectKind.SELF else '父亲',
        relationship=None
        if kind is SubjectKind.SELF
        else SubjectRelationship(relation_type='father', is_blood_relative=True),
        status=SubjectStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )


def payload(item):
    return {
        'factId': item.fact_id,
        'factType': item.fact_type,
        'category': item.category.value,
        'displayName': item.display_name,
        'value': item.value,
        'interpretation': item.interpretation,
    }


@pytest.mark.parametrize('fact_type', ['condition.history_status', 'family_history.reported'])
def test_observation_registry_accepts_only_self_record_objects_and_keeps_user_reports_pending(fact_type):
    registry = FactTypeRegistry.default()
    definition = registry.require(fact_type)
    assert registry.VERSION == 'health-fact-registry-v7'
    assert definition.permitted_value_types == (FactValueType.OBJECT,)
    assert definition.permitted_subject_kinds == (SubjectKind.SELF,)
    assert definition.permitted_source_types == (
        ProfileIngestionSourceType.RECORD,
        ProfileIngestionSourceType.QUESTIONNAIRE,
    )
    candidate = MemoryCandidate(
        candidate_id='observation',
        fact_type=fact_type,
        display_name='病史答题状态',
        value_type=FactValueType.OBJECT,
        value=observation(),
        assertion_level=AssertionLevel.USER_REPORTED,
        confidence=0.9,
        effective_period=EffectivePeriod(start=NOW),
        source=FactSource(source_type='record', source_id='record', source_revision=1),
        subject_id='self',
    )
    policy = HealthFactPolicy(registry)
    assert policy.evaluate(candidate, subject_kind=SubjectKind.SELF).disposition is PolicyDisposition.PENDING
    assert policy.evaluate(candidate, subject_kind=SubjectKind.BLOOD_RELATIVE).reason_code == 'subject_kind_not_allowed'
    for source_type in ('chat', 'health_report'):
        invalid = candidate.model_copy(
            update={'source': FactSource(source_type=source_type, source_id='other', source_revision=1)}
        )
        assert policy.evaluate(invalid, subject_kind=SubjectKind.SELF).reason_code == 'source_type_not_allowed'
    invalid = candidate.model_copy(update={'value_type': FactValueType.TEXT, 'value': '不清楚'})
    assert policy.evaluate(invalid, subject_kind=SubjectKind.SELF).reason_code == 'value_type_not_allowed'


@pytest.mark.parametrize('fact_type', ['condition.history_status', 'family_history.reported'])
@pytest.mark.parametrize('presence', ['present', 'absent', 'unknown', 'conflicting'])
def test_answer_states_never_become_clinical_anchors_even_after_confirmation(fact_type, presence):
    item = payload(fact(fact_type=fact_type, value=observation(presence), interpretation='high'))
    assert not clinical_anchor(item)
    assert not evidence_anchor(item)
    assert not is_abnormal(item)
    assert default_topics([item]) == []


@pytest.mark.parametrize(
    'value',
    [
        {'presence': 'unknown'},
        {'presence': 'absent'},
        {'presence': 'conflicting'},
        {'specificity': 'unspecified'},
        {'status': 'denied'},
        {'status': 'unknown'},
        {'negation': True},
        {'unknown': True},
    ],
)
@pytest.mark.parametrize('interpretation', [None, 'high'])
def test_explicit_nonaffirmative_symptom_objects_are_not_clinical_anchors(value, interpretation):
    item = {
        'factId': 'symptom',
        'factType': 'symptom.reported',
        'category': 'symptom',
        'displayName': '头痛',
        'value': value,
        'interpretation': interpretation,
    }
    assert not clinical_anchor(item)
    assert not is_abnormal(item)


def test_explicit_positive_symptom_still_supports_clinical_explanation():
    item = {
        'factId': 'symptom',
        'factType': 'symptom.reported',
        'category': 'symptom',
        'displayName': '头痛',
        'value': {'presence': 'present', 'specificity': 'specific', 'description': '我头痛'},
    }
    assert clinical_anchor(item)


@pytest.mark.parametrize(
    'presence,conditions,description,expected',
    [
        ('absent', (), '没有', '自述无个人疾病史'),
        ('absent', ('糖尿病',), '我没有糖尿病', '我没有糖尿病'),
        ('unknown', (), '不清楚', '个人疾病史情况不清楚'),
        ('present', (), '有', '自述有个人疾病史，病名未明确'),
        ('conflicting', ('糖尿病',), '没有，但也曾确诊糖尿病', '表述存在冲突：没有，但也曾确诊糖尿病'),
    ],
)
@pytest.mark.parametrize('interpretation', [None, 'high'])
def test_profile_summary_renders_history_status_without_diagnosis_or_risk(
    presence, conditions, description, expected, interpretation
):
    result = build_profile_summary(
        facts=(
            fact(
                value=observation(presence, conditions=conditions, description=description),
                interpretation=interpretation,
            ),
        ),
        profile_version=3,
        generated_at=NOW,
    )
    assert '病史说明：' in result['overall']
    assert expected in result['overall']
    assert '疾病记录' not in result['overall']
    assert '需要关注的健康记录' not in result['overall']
    assert '异常' not in result['overall']
    assert result['priorities'] == []


def test_family_summary_and_groups_preserve_unknown_relative_without_becoming_self_disease():
    item = fact(
        'family',
        'family_history.reported',
        observation('present', 'specific', ('糖尿病',), description='糖尿病'),
    )
    result = build_profile_summary(facts=(item,), profile_version=1, generated_at=NOW)
    assert '家族史记录：家族中报告糖尿病，具体血亲未明确' in result['overall']
    assert result['priorities'] == []
    groups = _build_fact_groups((item,))
    assert groups[0]['code'] == 'family_history'
    assert groups[0]['items'][0]['value']['relationship'] is None
    personal = _build_fact_groups((fact(),))
    assert personal[0]['code'] == 'condition' and personal[0]['name'] == '病史说明'


@pytest.mark.asyncio
async def test_chat_reads_only_the_dedicated_family_aggregate_and_keeps_original_father_path():
    repository = FakeProfileRepository(subject())
    repository.additional_subjects.append(subject(SubjectKind.BLOOD_RELATIVE))
    repository.facts.extend(
        (
            fact(
                'family', 'family_history.reported', observation('present', 'specific', ('糖尿病',)), status='pending'
            ),
            fact('misplaced', 'family_history.condition'),
            fact('father-history', 'family_history.condition', subjectId='father', status='pending'),
            fact('personal-unknown', value=observation('unknown'), status='pending'),
        )
    )
    result = await RepositoryHealthProfileChatMemoryProvider(repository).build_context('synthetic')
    rows = [json.loads(row) for row in result.splitlines() if row.startswith('{')]
    indexed = {row['factId']: row for row in rows}
    assert set(indexed) == {'family', 'father-history', 'personal-unknown'}
    assert indexed['family']['subjectKind'] == 'self'
    assert indexed['family']['value']['conditions'] == ['糖尿病']
    assert indexed['family']['value']['relationship'] is None
    assert indexed['father-history']['subjectKind'] == 'blood_relative'
    assert indexed['father-history']['relationType'] == 'father'
    assert indexed['personal-unknown']['status'] == 'pending'
    assert indexed['personal-unknown']['value']['presence'] == 'unknown'
    assert '不代表本人患病' in result and '不能猜测具体亲属' in result


@pytest.mark.asyncio
async def test_self_family_aggregate_shares_the_existing_family_memory_budget():
    repository = FakeProfileRepository(subject())
    repository.facts.append(fact('family', 'family_history.reported'))
    assert (
        await RepositoryHealthProfileChatMemoryProvider(repository, max_family_facts=0).build_context('synthetic')
        is None
    )


def test_unknown_history_does_not_displace_an_existing_explicit_diagnosis():
    existing = fact('diagnosis', 'condition.diagnosis', '糖尿病', valueType='text', displayName='糖尿病')
    newer = fact('unknown', value=observation('unknown', description='不清楚'))
    result = build_profile_summary(facts=(existing, newer), profile_version=2, generated_at=NOW)
    assert '糖尿病' in result['overall']
    assert '个人疾病史情况不清楚' in result['overall']
    assert clinical_anchor(payload(existing))
    assert not clinical_anchor(payload(newer))


def test_vague_complaint_goal_is_visible_without_a_fabricated_symptom():
    item = fact(
        'goal',
        'goal.health',
        {'presence': 'present', 'specificity': 'unspecified', 'description': '身体不舒服', 'entities': []},
        category='goal',
        displayName='本次关注',
    )
    result = build_profile_summary(facts=(item,), profile_version=2, generated_at=NOW)
    assert '健康目标：身体不舒服，具体内容未明确' in result['overall']
    assert result['priorities'] == []


@pytest.mark.parametrize(
    'presence,expected',
    [('absent', '自述无头痛'), ('unknown', '头痛情况不清楚'), ('conflicting', '头痛表述存在冲突')],
)
def test_nonaffirmative_symptom_summary_preserves_its_actual_meaning(presence, expected):
    item = fact(
        'symptom',
        'symptom.reported',
        {
            'presence': presence,
            'specificity': 'specific',
            'description': '不清楚' if presence == 'unknown' else '没有' if presence == 'absent' else '有',
            'name': '头痛',
        },
        category='symptom',
        displayName='头痛',
    )
    result = build_profile_summary(facts=(item,), profile_version=2, generated_at=NOW)
    assert '症状记录：' + expected in result['overall']
    assert result['priorities'] == []


@pytest.mark.parametrize(
    'fact_type,presence,description',
    [
        ('condition.history_status', 'absent', '我没有糖尿病'),
        ('condition.history_status', 'absent', '我没有被诊断为糖尿病'),
        ('condition.history_status', 'unknown', '不清楚是否有糖尿病'),
        ('condition.history_status', 'present', '以前有过肺炎，已经好了'),
        ('family_history.reported', 'absent', '家人没有糖尿病'),
    ],
)
def test_partial_or_temporal_history_description_is_not_widened_to_a_whole_history(fact_type, presence, description):
    result = build_profile_summary(
        facts=(fact(fact_type=fact_type, value=observation(presence, description=description)),),
        profile_version=2,
        generated_at=NOW,
    )
    assert description in result['overall']
    assert '自述无个人疾病史' not in result['overall']
    assert '自述无血亲家族病史' not in result['overall']
    assert '自述无糖尿病' not in result['overall']
    assert result['priorities'] == []


@pytest.mark.parametrize('status', ['past', 'historical', 'resolved', 'recovered', 'PAST'])
@pytest.mark.parametrize('interpretation', [None, 'high'])
def test_source_backed_historical_symptom_is_not_a_current_risk_anchor(status, interpretation):
    answer = '去年头痛，今年好了'
    item = fact(
        'past-headache',
        'symptom.reported',
        {'name': '头痛', 'description': answer, 'status': status},
        category='symptom',
        displayName='头痛',
        interpretation=interpretation,
    )
    row = payload(item)
    assert not clinical_anchor(row)
    assert not evidence_anchor(row)
    assert not is_abnormal(row)
    assert source_flag(row) is None
    assert default_topics([row]) == []
    result = build_profile_summary(facts=(item,), profile_version=2, generated_at=NOW)
    assert '症状记录：' + answer in result['overall']
    assert '异常' not in result['overall']
    assert result['priorities'] == []


@pytest.mark.parametrize('fact_type', ['condition.history_status', 'family_history.reported'])
def test_summary_retains_different_conditions_from_the_same_answer_state_type(fact_type):
    items = (
        fact('diabetes-denied', fact_type, observation('absent', 'specific', ['糖尿病'], description='没有糖尿病')),
        fact('hypertension-denied', fact_type, observation('absent', 'specific', ['高血压'], description='没有高血压')),
    )
    assert {item.fact_id for item in current_alert_facts(items).values()} == {'diabetes-denied', 'hypertension-denied'}
    result = build_profile_summary(facts=items, profile_version=4, generated_at=NOW)
    assert '没有糖尿病' in result['overall'] and '没有高血压' in result['overall']
    assert result['priorities'] == []


def test_summary_retains_distinct_reported_relatives_with_the_same_condition():
    items = tuple(
        fact(
            identity,
            'family_history.reported',
            observation(
                'unknown', 'specific', ['糖尿病'], relationship=relative, description=f'{relative}有没有糖尿病不清楚'
            ),
        )
        for identity, relative in [('father-unknown', '父亲'), ('mother-unknown', '母亲')]
    )
    assert len(current_alert_facts(items)) == 2
    result = build_profile_summary(facts=items, profile_version=4, generated_at=NOW)
    assert all(value in result['overall'] for value in ('父亲有没有糖尿病不清楚', '母亲有没有糖尿病不清楚'))
    assert result['priorities'] == []


@pytest.mark.parametrize('fact_type', ['condition.history_status', 'family_history.reported'])
def test_unclear_entities_preserve_independent_original_observations(fact_type):
    items = (
        fact('unknown-name', fact_type, observation('present', description='有病史但忘了病名')),
        fact('unknown-presence', fact_type, observation('unknown', description='不清楚有没有病史')),
    )
    assert len(current_alert_facts(items)) == 2
    result = build_profile_summary(facts=items, profile_version=4, generated_at=NOW)
    assert '有病史但忘了病名' in result['overall']
    assert '不清楚有没有病史' in result['overall']
    assert result['priorities'] == []


def test_unnamed_relatives_do_not_imply_the_same_relative_for_a_known_condition():
    items = (
        fact(
            'one-relative',
            'family_history.reported',
            observation('present', 'specific', ['糖尿病'], description='家里有人糖尿病'),
        ),
        fact(
            'another-relative',
            'family_history.reported',
            observation('present', 'specific', ['糖尿病'], description='还有一位血亲糖尿病'),
        ),
    )
    assert len(current_alert_facts(items)) == 2


@pytest.mark.parametrize('fact_type', ['condition.history_status', 'family_history.reported'])
def test_identical_observation_scope_still_uses_the_latest_confirmation(fact_type):
    older = fact('older', fact_type, observation('absent', 'specific', ['糖尿病'], description='没有糖尿病'))
    newer = older.model_copy(update={'fact_id': 'newer', 'updated_at': NOW + timedelta(seconds=1)})
    assert [item.fact_id for item in current_alert_facts((older, newer)).values()] == ['newer']
