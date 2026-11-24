"""从内存事实仓库经真实 Service/Graph 验证旧 API 结构下的专科解释。"""

from copy import deepcopy

import pytest

from agents.graphs.health_profile.focus import fact_rank
from health_profile.models import FactCategory
from health_profile.models import FactStatus
from health_profile.models import FactValueType
from health_profile.projection import evidence_fact_payloads
from health_profile.summary import build_profile_summary
from tests.unit.agents.graphs.health_profile.test_profile_interpretation import ScenarioKnowledge
from tests.unit.agents.graphs.health_profile.test_profile_interpretation import ScenarioLLM
from tests.unit.agents.graphs.health_profile.test_profile_interpretation import runtime_hub
from tests.unit.health_profile import test_profile_read_and_subjects as fixtures


async def setup_case(*, mode='success'):
    case = fixtures.ProfileReadAndSubjectsTest()
    case.setUp()
    knowledge = ScenarioKnowledge()
    llm = ScenarioLLM(mode=mode)
    case.service._agent_hub = runtime_hub(knowledge, llm)
    initial = await case.service.ensure_self_profile('synthetic-user')

    def add(identity, name, fact_type, **kwargs):
        defaults = {'category': FactCategory.LABORATORY, 'interpretation': 'high', 'status': FactStatus.ACTIVE}
        defaults.update(kwargs)
        return case._add_fact(
            user_id='synthetic-user',
            subject_id=initial.subject.subject_id,
            fact_id=identity,
            display_name=name,
            fact_type=fact_type,
            **defaults,
        )

    return case, knowledge, llm, add


@pytest.mark.asyncio
async def test_latest_normal_result_replaces_old_abnormal_even_with_legacy_type():
    case, knowledge, _, add = await setup_case()
    old = add('old', '血糖', 'lab.glucose', day=1, interpretation='critical')
    new = add('new', '血糖', 'laboratory.glucose', day=2, interpretation='normal')
    profile = await case.service.get_profile('synthetic-user', include_evidence=True)
    assert not profile['alerts']
    payloads = evidence_fact_payloads((old, new))
    assert [item['factId'] for item in payloads] == ['new']
    assert all('异常' not in query.query for query in knowledge.queries)
    assert '危急异常' not in profile['summary']['overall']


@pytest.mark.asyncio
async def test_generic_condition_type_preserves_different_entities_and_both_specialties():
    case, _, _, add = await setup_case()
    options = {'category': FactCategory.CONDITION, 'interpretation': None, 'value_type': FactValueType.TEXT}
    dental = add('dental', '牙齿发育异常', 'condition.diagnosis', value='牙齿发育异常', **options)
    gut = add('gut', '慢性便秘', 'condition.diagnosis', value='慢性便秘', **options)
    glucose = add('glucose', '血糖', 'laboratory.glucose')
    add('critical', '血钾', 'laboratory.potassium', interpretation='critical')
    before = deepcopy(case.facts.documents)
    profile = await case.service.get_profile('synthetic-user', include_evidence=True)
    payloads = evidence_fact_payloads((dental, gut, glucose))
    assert len(payloads) == 3
    assert profile['alerts'][0]['factIds'] == ['critical']
    assert profile['alerts'][0]['severity'] == 'critical'
    overall = profile['summary']['overall']
    assert overall.index('慢性便秘') < overall.index('牙齿发育异常')
    assert overall.index('血糖') < overall.index('牙齿发育异常')
    assert case.facts.documents == before
    assert set(profile['summary']) == {'overall', 'priorities', 'followUp', 'generatedAt', 'basedOnProfileVersion'}
    assert all(set(item) == {'alertId', 'severity', 'title', 'factIds', 'status'} for item in profile['alerts'])
    assert profile['summary']['basedOnProfileVersion'] == profile['profileVersion']


@pytest.mark.asyncio
async def test_synthesis_failure_selects_three_alerts_and_preserves_summary():
    case, _, _, add = await setup_case(mode='synthesis_failure')
    for identity, name, code in [
        ('glucose', '血糖', 'glucose'),
        ('lipids', '甘油三酯', 'triglyceride'),
        ('gut', '粪便隐血', 'fecal_occult_blood'),
        ('other', '肌酐', 'creatinine'),
    ]:
        add(identity, name, 'laboratory.' + code)
    profile = await case.service.get_profile('synthetic-user', include_evidence=True)
    # 综合解释失败仍按确定性规则精选提醒；完整临床异常说明保留在摘要。
    assert profile['evidence']['status'] == 'empty'
    assert profile['evidence']['items'] == []
    assert len(profile['alerts']) == 3
    assert {item['factIds'][0] for item in profile['alerts']} <= {'glucose', 'lipids', 'gut', 'other'}
    assert len(profile['summary']['priorities']) == 4


@pytest.mark.asyncio
async def test_alert_limit_does_not_remove_uncovered_abnormal_indicators():
    case, _, _, add = await setup_case()
    for index, code in enumerate(
        ('glucose', 'hba1c', 'triglyceride', 'alt', 'uric_acid', 'tsh', 'creatinine', 'hemoglobin')
    ):
        add(f'fact-{index}', code, 'laboratory.' + code)
    profile = await case.service.get_profile('synthetic-user', include_evidence=True)
    covered = {key for alert in profile['alerts'] for key in alert['factIds']}
    assert len(profile['alerts']) <= 3
    assert covered <= {f'fact-{index}' for index in range(8)}
    indicators = [item for group in profile['indicatorGroups'] for item in group['indicators']]
    assert {item['current']['factId'] for item in indicators} == {f'fact-{index}' for index in range(8)}


@pytest.mark.asyncio
async def test_evidence_items_are_always_explanation_backed():
    case, _, _, add = await setup_case()
    add('glucose', '血糖', 'laboratory.glucose')
    add('lipids', '甘油三酯', 'laboratory.triglyceride')
    profile = await case.service.get_profile('synthetic-user', include_evidence=True)
    evidence = profile['evidence']
    assert evidence['status'] == 'ready'
    items = evidence['items']
    assert items
    # conclusion 一律为解释态：没有"检索资料摘要"默认态。
    assert all('检索资料摘要' not in item['conclusion'] for item in items)
    assert all(item['conclusion'].strip() for item in items)
    # items 与已验证解释实际引用的资料集合一致。
    cited = {
        support['evidenceId']
        for insight in getattr(case, '_last_insights', []) or []
        for support in insight.get('supports', [])
    }
    if cited:
        assert {item['evidenceId'] for item in items} == cited
    # factIds 均为真实事实且按 fact_rank 升序。
    for item in items:
        assert item['factIds']

        def rank_of(key):
            return fact_rank(
                {'factId': key, 'displayName': key, 'factType': 'laboratory.x', 'category': 'laboratory'},
            )

        ranks = [rank_of(key) for key in item['factIds']]
        assert ranks == sorted(ranks)


@pytest.mark.asyncio
async def test_evidence_switch_skips_external_calls_and_pending_remains_excluded():
    case, knowledge, llm, add = await setup_case()
    add('pending', '血糖', 'laboratory.glucose', status=FactStatus.PENDING)
    add('disputed', '粪便隐血', 'laboratory.fecal_occult_blood', status=FactStatus.DISPUTED)
    profile = await case.service.get_profile('synthetic-user', include_evidence=True)
    assert profile['alerts'] == []
    assert profile['evidence']['status'] == 'empty'
    assert knowledge.queries == [] and llm.calls == []
    add('active', '血糖', 'laboratory.glucose')
    profile = await case.service.get_profile('synthetic-user', include_evidence=False, fact_statuses=['active'])
    assert profile['summary']['basedOnProfileVersion'] == profile['profileVersion']
    assert profile['evidence']['status'] == 'not_requested'
    assert knowledge.queries == [] and llm.calls == []
    profile = await case.service.get_profile('synthetic-user', include_evidence=True, fact_statuses=['active'])
    assert profile['summary']['basedOnProfileVersion'] == profile['profileVersion']
    assert profile['evidence']['status'] == 'ready'
    assert all('pending' not in item['factIds'] for item in profile['evidence']['items'])


@pytest.mark.asyncio
async def test_microbiome_flagged_joins_evidence_but_unmarked_stays_out():
    case, knowledge, llm, add = await setup_case()
    # 具体菌种的来源异常标记可触发循证；匹配资料可产生中性的报告复核提示。
    add(
        'micro',
        '类志贺邻单胞菌',
        'report_observation.aaaaaaaaaaaaaaaaaaaaaaaa',
        category=FactCategory.REPORT_OBSERVATION,
        interpretation='high',
        value=0.0002,
    )
    profile = await case.service.get_profile('synthetic-user', include_evidence=True, indicator_scope='abnormal')
    assert profile['indicatorGroups'] == []  # 展示筛选不影响循证入选。
    assert knowledge.queries, '带来源标记的菌群事实应参与循证检索'
    assert len(profile['alerts']) == 1
    assert profile['alerts'][0]['severity'] == 'important'
    assert '建议核对原报告' in profile['alerts'][0]['title']
    assert profile['evidence']['status'] == 'ready'
    assert profile['evidence']['items']
    assert all('类志贺邻单胞菌' in item['conclusion'] for item in profile['evidence']['items'])
    assert all('可作为健康背景' not in item['conclusion'] for item in profile['evidence']['items'])
    # 背景评估类事实仍不参与循证：不触发检索，不产生空壳条目。
    case2, knowledge2, llm2, add2 = await setup_case()
    add2(
        'index',
        '肠道微生物健康指数',
        'assessment.microbiome_health_index',
        category=FactCategory.ASSESSMENT,
        interpretation='high',
        value=0.35,
    )
    quiet = await case2.service.get_profile('synthetic-user', include_evidence=True)
    assert knowledge2.queries == [] and llm2.calls == []
    assert quiet['evidence']['status'] == 'empty'
    assert quiet['evidence']['items'] == []
    assert '不能单独据此判断疾病风险' not in quiet['summary']['overall']
    # 定性阳性检验仍可产生事实提醒（现状回归）。
    add(
        'stool',
        '粪便隐血',
        'laboratory.fecal_occult_blood',
        interpretation=None,
        value_type=FactValueType.TEXT,
        value='阳性',
        unit=None,
    )
    profile = await case.service.get_profile('synthetic-user')
    assert [item['factIds'] for item in profile['alerts']] == [['stool']]


@pytest.mark.asyncio
@pytest.mark.parametrize('result', ['检出', '阳性'])
async def test_explicit_detection_with_matching_evidence_gets_review_alert(result):
    case, knowledge, llm, add = await setup_case()
    add(
        'detected',
        '类志贺邻单胞菌',
        'report_observation.bbbbbbbbbbbbbbbbbbbbbbbb',
        category=FactCategory.REPORT_OBSERVATION,
        interpretation=None,
        value_type=FactValueType.TEXT,
        value=result,
        unit=None,
    )
    profile = await case.service.get_profile('synthetic-user', include_evidence=True)
    assert knowledge.queries
    assert profile['evidence']['status'] == 'ready'
    assert profile['evidence']['items'][0]['factIds'] == ['detected']
    assert profile['alerts'][0]['factIds'] == ['detected']
    assert profile['alerts'][0]['severity'] == 'important'
    assert '建议核对原报告' in profile['alerts'][0]['title']

    quiet_case, quiet_knowledge, quiet_llm, quiet_add = await setup_case()
    quiet_add(
        'abundance',
        '菌属乙',
        'report_observation.cccccccccccccccccccccccc',
        category=FactCategory.REPORT_OBSERVATION,
        interpretation=None,
        value=0.0002,
    )
    quiet = await quiet_case.service.get_profile('synthetic-user', include_evidence=True)
    assert quiet['evidence']['status'] == 'empty'
    assert quiet['evidence']['items'] == []
    assert quiet_knowledge.queries == [] and quiet_llm.calls == []


@pytest.mark.asyncio
async def test_background_assessment_is_excluded_from_evidence_input_but_clinical_facts_flow():
    case, knowledge, llm, add = await setup_case()
    add(
        'microbiome-index',
        '肠道微生物健康指数',
        'assessment.microbiome_health_index',
        category=FactCategory.ASSESSMENT,
        interpretation=None,
        value=0.35,
    )
    add('glucose', '血糖', 'laboratory.glucose')
    profile = await case.service.get_profile('synthetic-user', include_evidence=True)
    assert knowledge.queries, '临床事实应正常触发循证检索'
    assert all('肠道微生物健康指数' not in query.query for query in knowledge.queries)
    assert profile['evidence']['status'] == 'ready'
    items = profile['evidence']['items']
    assert items
    # 条目均为 risk 解释态，不出现 background 模板文案。
    assert all('可作为健康背景' not in item['conclusion'] for item in items)
    assert all('肠道微生物健康指数' not in item['conclusion'] for item in items)


@pytest.mark.asyncio
async def test_cached_response_preserves_generation_timestamp_without_mutating_facts():
    case, _, llm, add = await setup_case()
    add('glucose', '血糖', 'laboratory.glucose')
    before = deepcopy(case.facts.documents)
    first = await case.service.get_profile('synthetic-user', include_evidence=True)
    second = await case.service.get_profile('synthetic-user', include_evidence=True)
    assert len(llm.calls) == 2
    assert first['evidence']['generatedAt'] == second['evidence']['generatedAt']
    assert first['summary']['generatedAt'] == second['summary']['generatedAt']
    assert first['profileVersion'] == second['profileVersion']
    assert case.facts.documents == before


@pytest.mark.asyncio
async def test_model_omitting_specialties_cannot_put_dental_before_uncovered_specialty_facts():
    case, _, _, add = await setup_case()
    dental = add(
        'dental',
        '牙齿发育异常',
        'condition.diagnosis',
        category=FactCategory.CONDITION,
        value_type=FactValueType.TEXT,
        value='牙齿发育异常',
        interpretation=None,
    )
    gut = add(
        'gut',
        '便秘',
        'symptom.digestive',
        category=FactCategory.SYMPTOM,
        value_type=FactValueType.TEXT,
        value='便秘',
        interpretation=None,
    )
    glucose = add('glucose', '血糖', 'laboratory.glucose')
    profile = await case.service.get_profile('synthetic-user')
    facts = evidence_fact_payloads((dental, gut, glucose))
    dental_fact = next(fact for fact in facts if fact['factId'] == 'dental')
    profile['summary'] = build_profile_summary(
        facts=(dental, gut, glucose),
        profile_version=profile['profileVersion'],
        insights=[
            {
                'kind': 'risk',
                'topicId': 'dental',
                'factIds': ['dental'],
                'title': '牙齿发育异常',
                'explanation': '牙齿发育异常应进一步评估。',
                'action': '进行相关评估',
                'followUp': '',
                'rank': fact_rank(dental_fact),
                'supports': [{'evidenceId': 'evidence_dental', 'quote': '牙齿发育异常应进一步评估。'}],
            }
        ],
        evidence_items=[{'evidenceId': 'evidence_dental', 'factIds': ['dental']}],
        generated_at='2026-09-09T00:00:00Z',
    )
    text = profile['summary']['overall']
    assert '牙齿发育异常应进一步评估。' in text
    assert text.index('便秘') < text.index('牙齿发育异常')
    assert text.index('血糖') < text.index('牙齿发育异常')


@pytest.mark.asyncio
async def test_internal_ids_do_not_reach_evidence_conclusion_or_summary():
    case, _, llm, add = await setup_case(mode='internal_ids')
    identity = 'fact_a97dc2b8fbd5421c996d9ce04467642f'
    add(identity, '血糖', 'laboratory.glucose')
    first = await case.service.get_profile('synthetic-user', include_evidence=True)
    second = await case.service.get_profile('synthetic-user', include_evidence=True)
    expected = '当前血糖存在需关注的记录，所引资料说明应结合症状、检验结果和原始报告评估。'
    for profile in (first, second):
        item = profile['evidence']['items'][0]
        assert item['conclusion'] == expected
        assert item['factIds'] == [identity]
        assert item['evidenceId'].startswith('evidence_')
        assert expected in profile['summary']['overall']
        assert 'fact_' not in profile['summary']['overall']
        assert 'evidence_' not in profile['summary']['overall']
        assert profile['alerts'][0]['factIds'] == [identity]
    assert len(llm.calls) == 2
