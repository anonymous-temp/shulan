"""综述整合事实、验证解释及精选提示的回归；不用真实模型或数据库。"""

from copy import deepcopy
from datetime import timedelta

import pytest

from health_profile.alerts import select_profile_alerts
from health_profile.models import FactCategory
from health_profile.models import FactStatus
from health_profile.models import FactValueType
from health_profile.projection import _build_alerts
from health_profile.projection import current_profile_facts
from health_profile.summary import EMPTY_FOLLOW_UP
from health_profile.summary import EMPTY_OVERALL
from health_profile.summary import build_profile_summary
from tests.unit.health_profile.test_alert_selection import NOW
from tests.unit.health_profile.test_alert_selection import association
from tests.unit.health_profile.test_alert_selection import evidence
from tests.unit.health_profile.test_alert_selection import measurement
from tests.unit.health_profile.test_specialty_projection import setup_case


def summary(*facts, insights=(), items=()):
    alerts = select_profile_alerts(
        facts=facts,
        alerts=_build_alerts(current_profile_facts(facts)),
        insights=insights,
        evidence_items=items,
    )
    return build_profile_summary(
        facts=facts,
        profile_version=858,
        generated_at=NOW,
        alerts=alerts,
        insights=insights,
        evidence_items=items,
    )


def content(result):
    return '\n'.join([result['overall'], *result['priorities'], result['followUp']])


def lab(identity, name, flag='high', **changes):
    return measurement(
        identity, name, category='laboratory', factType='laboratory.' + identity, interpretation=flag, **changes
    )


@pytest.mark.parametrize('facts', [(), (measurement(status='pending'),), (measurement(status='disputed'),)])
def test_absent_current_data_has_direct_empty_text(facts):
    result = summary(*facts)
    assert result['overall'] == EMPTY_OVERALL
    assert result['priorities'] == []
    assert result['followUp'] == EMPTY_FOLLOW_UP
    assert '画像' not in content(result)
    assert '正常' not in result['overall'] and '未见异常' not in result['overall']
    assert set(result) == {'overall', 'priorities', 'followUp', 'generatedAt', 'basedOnProfileVersion'}
    assert result['basedOnProfileVersion'] == 858
    assert isinstance(result['generatedAt'], str)


@pytest.mark.parametrize('flag,phrase', [('normal', '标记为正常'), (None, '异常判断依据'), ('unknown', '异常判断依据')])
def test_normal_and_unjudged_results_are_distinguished(flag, phrase):
    result = summary(measurement(interpretation=flag))
    assert phrase in result['overall']
    assert result['priorities'] == [] and result['followUp'] == EMPTY_FOLLOW_UP
    assert '画像' not in content(result)
    if flag != 'normal':
        assert '正常' not in result['overall']


def test_normal_subset_does_not_conclude_all_tests_normal():
    result = summary(lab('glucose', '血糖', 'normal'), measurement(interpretation=None))
    assert '另有检测结果未提供明确的异常判断依据' in result['overall']
    assert '均标记为正常' in result['overall']
    assert not result['priorities']


def test_numeric_review_is_in_summary_without_evidence_or_disease_inference():
    result = summary(measurement())
    text = content(result)
    assert all(value in text for value in ('Bacteroides coprophilus', '5.5123', '0-0.2394', '偏高'))
    assert len(result['priorities']) == 1 and '核对原报告' in result['priorities'][0]
    assert result['followUp'] == EMPTY_FOLLOW_UP
    assert all(value not in text for value in ('糖尿病', '感染', '画像', 'fact_', 'evidence_'))


def test_many_report_anomalies_are_counted_beyond_alert_limit():
    facts = [
        measurement(
            'fact_extra_' + str(index), '检测项' + str(index), number=10 + index, referenceRange={'low': 0, 'high': 1}
        )
        for index in range(8)
    ]
    before = deepcopy(facts)
    result = summary(*facts)
    assert len(result['priorities']) == 3
    assert '其他来源标记异常的具体检测共 5 项' in result['overall']
    assert '等 5 项' in result['overall']
    assert facts == before


def test_mild_and_background_flags_are_described_without_creating_actions():
    mild = measurement(number=0.3)
    score = measurement('score', '肠道健康指数', value={'number': 0.35, 'kind': 'assessment'})
    unknown = measurement('scalar', '未知分类项', value={'raw': '偏低', 'kind': 'scalar'})
    result = summary(mild, score, unknown)
    assert all(name in result['overall'] for name in (mild.display_name, score.display_name, unknown.display_name))
    assert '报告评估共 1 项' in result['overall'] and '其他报告结果共 1 项' in result['overall']
    assert result['priorities'] == []
    assert result['followUp'] == EMPTY_FOLLOW_UP


def test_exact_report_alias_latest_normal_overrides_old_abnormal():
    old = measurement('old', '类志贺邻单胞菌（Plesiomonas shigelloides）')
    new = measurement(
        'new', 'Plesiomonas shigelloides', interpretation='normal', effectivePeriod={'start': NOW + timedelta(days=1)}
    )
    result = summary(old, new)
    assert '偏高' not in content(result)
    assert result['priorities'] == []
    assert '标记为正常' in result['overall']


def test_matching_association_retains_explanation_but_never_model_action_or_follow_up():
    fact = measurement(name='Plesiomonas shigelloides', number=0.3)
    insight = {**association(fact), 'action': '服用某种药物', 'followUp': '三天后复查'}
    result = summary(fact, insights=[insight], items=evidence(fact))
    assert insight['explanation'] in result['overall']
    assert '0.3' in result['overall']
    assert result['priorities'] == [fact.display_name + '：核对原报告中的结果、参考范围和检测方法。']
    assert result['followUp'] == EMPTY_FOLLOW_UP
    assert '药物' not in content(result) and '三天' not in content(result)


@pytest.mark.parametrize(
    'quote',
    [
        'Bacteroides coprocola abundance was associated with diabetes in this observational study.',
        'Bacteroides coprophilus strains have characteristic SNP and gene distributions.',
    ],
)
def test_wrong_species_or_assay_never_supplies_summary_explanation(quote):
    fact = measurement()
    insight = association(fact, quote)
    result = summary(fact, insights=[insight], items=evidence(fact))
    assert insight['explanation'] not in result['overall']
    assert '5.5123' in result['overall']
    assert len(result['priorities']) == 1  # 数值复核仍有效，与论文匹配无关。


def test_multi_fact_association_requires_every_entity_to_match():
    first = measurement('first', 'Bacteroides coprophilus', number=0.3)
    second = measurement('second', 'Plesiomonas shigelloides', number=0.3)
    insight = association(first)
    insight['factIds'].append(second.fact_id)
    insight['explanation'] += ' Plesiomonas shigelloides相关说明。'
    items = [{'evidenceId': 'evidence-test', 'factIds': insight['factIds']}]
    result = summary(first, second, insights=[insight], items=items)
    assert '胃肠炎' not in result['overall']


@pytest.mark.parametrize('binding', ['missing_evidence', 'wrong_fact', 'obsolete_fact'])
def test_stale_or_unbound_insights_cannot_create_summary_advice(binding):
    fact = lab('glucose', '血糖')
    insight = {**association(fact), 'kind': 'risk', 'action': '特殊行动建议', 'followUp': '特殊随访建议'}
    items = evidence(fact)
    if binding == 'missing_evidence':
        items = []
    elif binding == 'wrong_fact':
        items[0]['factIds'] = ['other']
    else:
        insight['factIds'] = ['obsolete']
    result = summary(fact, insights=[insight], items=items)
    assert '特殊' not in content(result)
    assert '血糖' in result['overall'] and result['priorities']


def test_critical_facts_all_preserved_and_unselected_clinical_groups_remain():
    facts = [lab('potassium', '血钾', 'critical'), lab('sodium', '血钠', 'critical')]
    facts.extend(
        lab(code, name)
        for code, name in [
            ('glucose', '血糖'),
            ('alt', '谷丙转氨酶'),
            ('triglyceride', '甘油三酯'),
            ('creatinine', '肌酐'),
        ]
    )
    result = summary(*facts)
    assert result['overall'].startswith('需优先关注来源已标记的危急异常')
    assert all(fact.display_name in result['overall'] for fact in facts)
    assert sum('及时联系医生' in action for action in result['priorities']) == 2
    assert len(result['priorities']) > 3


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['否认腹痛', False])
async def test_negated_symptom_is_not_presented_as_positive_or_advised(value):
    case, _, _, add = await setup_case()
    add(
        'symptom',
        '腹痛',
        'symptom.reported',
        category=FactCategory.SYMPTOM,
        value_type=FactValueType.TEXT if isinstance(value, str) else FactValueType.BOOLEAN,
        interpretation=None,
        value=value,
    )
    profile = await case.service.get_profile('synthetic-user')
    assert ('否认腹痛' if isinstance(value, str) else '未报告腹痛') in profile['summary']['overall']
    assert not profile['summary']['priorities']
    assert '需要关注' not in profile['summary']['overall']


@pytest.mark.asyncio
async def test_summary_stable_across_scope_and_restart_for_association_and_empty_evidence():
    from health_profile.service import HealthProfileService
    from tests.unit.health_profile.test_evidence_results import SharedLeases
    from tests.unit.health_profile.test_evidence_results import coordinator
    from tests.unit.health_profile.test_profile_read_and_subjects import FakeCollection

    for mode in ('success', 'no_relevant'):
        case, _, llm, add = await setup_case(mode=mode)
        fact = measurement(name='类志贺邻单胞菌', number=0.3)
        initial = await case.service.ensure_self_profile('synthetic-user')
        fact = fact.model_copy(update={'user_id': 'synthetic-user', 'subject_id': initial.subject.subject_id})
        case.facts.documents.append(fact.to_document(document_id=fact.fact_id))
        collection, leases = FakeCollection(), SharedLeases()
        case.service._evidence_results = coordinator(collection, leases)
        before = deepcopy(case.facts.documents)
        first = await case.service.get_profile('synthetic-user', include_evidence=True)
        filtered = await case.service.get_profile(
            'synthetic-user', include_evidence=True, indicator_scope='abnormal', indicator_limit=1
        )
        restarted = HealthProfileService(
            case.service._repository,
            evidence_results=coordinator(collection, leases),
            clock=lambda: NOW + timedelta(days=7),
        )
        second = await restarted.get_profile('synthetic-user', include_evidence=True, indicator_scope='flagged')
        assert first['summary'] == filtered['summary'] == second['summary']
        assert len(llm.calls) == 2
        assert 'insights' not in first
        assert before == case.facts.documents
        if mode == 'success':
            assert '胃肠炎' in first['summary']['overall']
        else:
            assert '胃肠炎' not in first['summary']['overall']


@pytest.mark.asyncio
async def test_summary_without_evidence_has_timestamp_and_uses_data_review():
    case, knowledge, llm, add = await setup_case()
    initial = await case.service.ensure_self_profile('synthetic-user')
    fact = measurement().model_copy(update={'user_id': 'synthetic-user', 'subject_id': initial.subject.subject_id})
    case.facts.documents.append(fact.to_document(document_id=fact.fact_id))
    profile = await case.service.get_profile('synthetic-user')
    assert isinstance(profile['summary']['generatedAt'], str)
    assert profile['summary']['basedOnProfileVersion'] == profile['profileVersion']
    assert '5.5123' in profile['summary']['overall']
    assert len(profile['summary']['priorities']) == 1
    assert profile['evidence']['generatedAt'] is None
    assert not knowledge.queries and not llm.calls


@pytest.mark.asyncio
async def test_all_user_text_fields_strip_internal_ids_while_bindings_remain():
    case, _, _, add = await setup_case(mode='internal_ids')
    identity = 'fact_a97dc2b8fbd5421c996d9ce04467642f'
    add(identity, '血糖', 'laboratory.glucose')
    profile = await case.service.get_profile('synthetic-user', include_evidence=True)
    assert all(token not in content(profile['summary']) for token in ('fact_', 'evidence_', 'topicId'))
    assert profile['alerts'][0]['factIds'] == [identity]
    assert profile['evidence']['items'][0]['factIds'] == [identity]


@pytest.mark.asyncio
async def test_summary_remains_empty_when_no_active_facts_match_the_profile():
    case, _, _, add = await setup_case()
    add('pending', '血糖', 'laboratory.glucose', status=FactStatus.PENDING)
    default = await case.service.get_profile('synthetic-user', include_evidence=True)
    assert default['summary']['overall'] == EMPTY_OVERALL
    assert default['summary']['priorities'] == []
    profile = await case.service.get_profile('synthetic-user', include_evidence=True, fact_statuses=['active'])
    assert profile['factGroups'] == []
    assert profile['summary'] == default['summary']
    assert profile['overview']['pendingConfirmationCount'] == 1
    assert profile['evidence'] == default['evidence']


@pytest.mark.asyncio
async def test_unexplained_group_keeps_both_measurement_and_affirmative_symptom():
    case, _, _, add = await setup_case(mode='synthesis_failure')
    add('stool', '粪便隐血', 'laboratory.fecal_occult_blood')
    add(
        'symptom',
        '便秘',
        'symptom.digestive',
        category=FactCategory.SYMPTOM,
        value_type=FactValueType.TEXT,
        interpretation=None,
        value='便秘',
    )
    profile = await case.service.get_profile('synthetic-user', include_evidence=True)
    assert '粪便隐血' in profile['summary']['overall']
    assert '便秘' in profile['summary']['overall']
    assert profile['summary']['priorities']


def test_same_name_different_methods_keep_consistent_observation_count():
    facts = [
        measurement(
            'fact_score_' + str(index),
            '报告综合评分',
            value={'number': 1, 'kind': 'assessment', 'method': 'method-' + str(index)},
        )
        for index in range(5)
    ]
    result = summary(*facts)
    assert '报告评估共 5 项' in result['overall']
    assert '报告综合评分等 5 项' in result['overall']
    assert result['priorities'] == []
    assert '等 1 项' not in result['overall']
