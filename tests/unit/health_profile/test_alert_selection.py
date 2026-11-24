"""关键提示精选规则；数值门槛不是临床风险判定。"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from decimal import Decimal

import pytest

from health_profile.alerts import reference_deviation
from health_profile.alerts import select_profile_alerts
from health_profile.models import HealthFact
from tests.unit.health_profile.test_specialty_projection import setup_case

NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)


def measurement(identity='micro', name='Bacteroides coprophilus', number=5.5123, **changes):
    value = {'number': number, 'raw': str(number), 'kind': 'measurement'}
    data = {
        'factId': identity,
        'userId': 'synthetic',
        'subjectId': 'self',
        'factType': 'report_observation.' + identity,
        'category': 'report_observation',
        'displayName': name,
        'valueType': 'quantity',
        'value': value,
        'interpretation': 'high',
        'referenceRange': {'text': '0-0.2394', 'low': 0, 'high': 0.2394},
        'assertionLevel': 'observed',
        'verificationStatus': 'verified_source',
        'confidence': 1,
        'effectivePeriod': {'start': NOW},
        'status': 'active',
        'sources': [{'sourceType': 'health_report', 'sourceId': 'synthetic-report', 'sourceRevision': 1}],
        'createdAt': NOW,
        'updatedAt': NOW,
    }
    data.update(changes)
    return HealthFact.model_validate(data)


def select(*facts, alerts=(), insights=(), evidence_items=()):
    return select_profile_alerts(facts=tuple(facts), alerts=alerts, insights=insights, evidence_items=evidence_items)


def association(fact, quote=None):
    return {
        'topicId': 'topic',
        'kind': 'association',
        'title': fact.display_name,
        'explanation': fact.display_name + '与胃肠炎病例有关，不能据此判断个人患病。',
        'factIds': [fact.fact_id],
        'supports': [
            {
                'evidenceId': 'evidence-test',
                'quote': quote or fact.display_name + '曾在胃肠炎病例中被报告，应结合症状与检测方法判断。',
            }
        ],
    }


def evidence(fact):
    return [{'evidenceId': 'evidence-test', 'factIds': [fact.fact_id]}]


@pytest.mark.parametrize(
    ('number', 'flag', 'low', 'high', 'eligible'),
    [
        (5, 'high', 0, 1, True),
        (4.99999, 'high', 0, 1, False),
        (0.2, 'low', 1, 5, True),
        (0.20001, 'low', 1, 5, False),
        (0, 'low', 1, 5, True),
        (0.0002, 'high', 0, 0, False),
        (0, 'low', 0, 1, False),
        (0.5, 'high', 0, 1, False),
        (2, 'low', 1, 5, False),
        (5, 'normal', 0, 1, False),
        (5, None, 0, 1, False),
        (float('inf'), 'high', 0, 1, False),
        (float('nan'), 'high', 0, 1, False),
        (True, 'high', 0, 1, False),
        (-5, 'low', 1, 5, False),
        (5, 'high', 2, 1, False),
        (5, 'high', -1, 1, False),
    ],
)
def test_numeric_boundaries_and_direction(number, flag, low, high, eligible):
    fact = measurement(
        number=number, interpretation=flag, referenceRange={'text': f'{low}-{high}', 'low': low, 'high': high}
    )
    alerts = select(fact)
    assert bool(alerts) is eligible
    if alerts:
        assert alerts[0]['severity'] == 'important'
        assert set(alerts[0]) == {'alertId', 'severity', 'title', 'factIds', 'status'}


@pytest.mark.parametrize(
    ('value', 'reference', 'eligible'),
    [
        ({'number': 5, 'unit': '%'}, {'low': 0, 'high': 1, 'unit': '%'}, True),
        ({'number': 5, 'unit': '%'}, {'text': '0–1%', 'low': 0, 'high': 1}, True),
        ({'number': 5}, {'text': '≤1', 'high': 1}, True),
        ({'number': 5, 'unit': 'mg/L'}, {'low': 0, 'high': 1, 'unit': 'mmol/L'}, False),
        ({'number': 5, 'unit': '%'}, {'low': 0, 'high': 1}, False),
        ({'number': 5}, {'low': 0, 'high': 1, 'unit': '%'}, False),
        ({'number': 5, 'comparator': '>'}, {'low': 0, 'high': 1}, False),
        ({'number': 5, 'raw': '<5'}, {'low': 0, 'high': 1}, False),
        ({'number': 5, 'raw': '0.1'}, {'low': 0, 'high': 1}, False),
        ({'number': 5, 'raw': '5e0'}, {'low': 0, 'high': 1}, True),
        ({'number': 5, 'raw': '5%'}, {'low': 0, 'high': 1}, False),
        ({'number': 5, 'raw': '5%'}, {'low': 0, 'high': 1, 'unit': '%'}, True),
        ({'number': '1e9999999'}, {'low': 0, 'high': 1}, False),
        ({'number': '1e-9999999'}, {'low': 0, 'high': 1}, False),
        ({'number': 5}, {'text': '合成能力弱(0-1)，强(1-10)', 'low': 0, 'high': 1}, False),
        ({'number': 5}, {'text': '0-2', 'low': 0, 'high': 1}, False),
        ({'number': 5}, {'low': 0, 'high': float('inf')}, False),
        ({'number': 5}, {'low': 0, 'high': True}, False),
        ({'number': 5, 'unit': '%'}, {'text': '0-1mg/L', 'unit': '%'}, False),
        ({'number': 5}, {'text': '0-1 2-3', 'low': 0, 'high': 1}, False),
    ],
)
def test_reference_comparability(value, reference, eligible):
    fact = measurement(value={**value, 'kind': 'measurement'}, referenceRange=reference)
    assert bool(select(fact)) is eligible


@pytest.mark.parametrize(
    'name',
    [
        '肠道年龄',
        '肠道微生物健康指数',
        '菌群多样性',
        '菌种数',
        '菌群总丰度',
        '维生素B6合成能力',
        '糖尿病风险',
        '有害菌占比',
        'Gut diversity score',
    ],
)
def test_background_measurements_never_become_review_candidates(name):
    fact = measurement(name=name)
    assert not select(fact, insights=[association(fact)], evidence_items=evidence(fact))


def test_assessment_and_unknown_classification_are_excluded():
    assessed = measurement(value={'kind': 'assessment', 'number': 10})
    unknown = measurement(name='未分类项目', value={'number': 10})
    assert not select(assessed, unknown)
    missing_value = measurement(value={'kind': 'measurement'}, interpretation='positive')
    assert not select(missing_value, insights=[association(missing_value)], evidence_items=evidence(missing_value))


def test_review_title_keeps_result_and_reference_units_when_raw_is_missing():
    fact = measurement(
        value={'number': 5, 'raw': None, 'unit': '%', 'kind': 'measurement'},
        referenceRange={'text': '0-1', 'low': 0, 'high': 1, 'unit': '%'},
    )
    title = select(fact)[0]['title']
    assert '结果5%' in title and '参考范围0-1%' in title
    assert 'None' not in title


@pytest.mark.parametrize(
    ('quote', 'eligible'),
    [
        ('Bacteroides coprophilus abundance was associated with gastrointestinal symptoms in this study.', True),
        ('Bacteroides coprocola abundance was associated with gastrointestinal symptoms in this study.', False),
        ('Bacteroides coprophilusx abundance was associated with gastrointestinal symptoms in this study.', False),
        ('Bacteroides coprophilus had characteristic single nucleotide polymorphisms in these genes.', False),
        ('Bacteroides coprophilus strains were associated with characteristic SNPs in diabetic patients.', False),
        ('Bacteroides coprophilus was sequenced. Other bacteria were associated with diarrhea in patients.', False),
    ],
)
def test_evidence_matches_complete_test_entity_and_content(quote, eligible):
    fact = measurement(number=0.3)
    alerts = select(fact, insights=[association(fact, quote)], evidence_items=evidence(fact))
    assert bool(alerts) is eligible
    assert not any(alert['severity'] == 'critical' for alert in alerts)


def test_genus_cannot_borrow_species_evidence_and_bilingual_alias_is_explicit():
    genus = measurement(name='Bacteroides', number=0.3)
    quote = 'Bacteroides coprophilus abundance was associated with diarrhea in patients.'
    assert not select(genus, insights=[association(genus, quote)], evidence_items=evidence(genus))
    bilingual = measurement(name='类志贺邻单胞菌（Plesiomonas shigelloides）', number=0.3)
    quote = 'Plesiomonas shigelloides is associated with gastroenteritis and diarrhea in patients.'
    assert select(bilingual, insights=[association(bilingual, quote)], evidence_items=evidence(bilingual))


def test_mismatched_paper_does_not_block_independent_numeric_review():
    fact = measurement()
    insight = association(fact, 'Bacteroides coprocola abundance was studied in diabetic patients.')
    alerts = select(fact, insights=[insight], evidence_items=evidence(fact))
    assert reference_deviation(fact) == Decimal('5.5123') / Decimal('0.2394')
    assert alerts[0]['factIds'] == ['micro']
    assert all(
        text in alerts[0]['title']
        for text in ['Bacteroides coprophilus', '5.5123', '0-0.2394', '报告标记偏高', '建议核对']
    )
    assert '糖尿病' not in alerts[0]['title'] and 'micro' not in alerts[0]['title']


@pytest.mark.parametrize('result', ['阳性', '检出'])
def test_explicit_qualitative_detection_needs_matching_evidence(result):
    fact = measurement(name='类志贺邻单胞菌', valueType='text', value=result, interpretation=None, referenceRange=None)
    assert not select(fact)
    assert select(fact, insights=[association(fact)], evidence_items=evidence(fact))
    unflagged = measurement(number=0.3, interpretation=None)
    assert not select(unflagged, insights=[association(unflagged)], evidence_items=evidence(unflagged))


def test_support_must_belong_to_returned_validated_evidence():
    fact = measurement(number=0.3)
    insight = association(fact)
    assert not select(fact, insights=[insight], evidence_items=[])
    assert not select(fact, insights=[insight], evidence_items=[{'evidenceId': 'other', 'factIds': ['micro']}])
    insight['explanation'] += '该论文研究的是不同菌种，不能直接套用。'
    assert not select(fact, insights=[insight], evidence_items=evidence(fact))


def test_latest_normal_and_duplicate_observations_are_selected_before_flags():
    old = measurement('old')
    new = measurement('new', interpretation='normal', number=0.1, effectivePeriod={'start': NOW + timedelta(days=1)})
    assert not select(old, new)
    repeat = measurement('repeat', effectivePeriod={'start': NOW + timedelta(days=1)})
    assert [alert['factIds'] for alert in select(old, repeat)] == [['repeat']]
    bilingual = measurement(
        'bi', name='菌种甲（Bacteroides coprophilus）', effectivePeriod={'start': NOW + timedelta(days=2)}
    )
    assert [alert['factIds'] for alert in select(old, repeat, bilingual)] == [['bi']]
    distinct = measurement('distinct', name='Bacteroides coprocola')
    assert len(select(old, distinct)) == 2
    pending = old.model_copy(update={'status': 'pending'})
    assert not select(pending)
    legacy_naive = measurement(
        'legacy', effectivePeriod={'start': NOW.replace(tzinfo=None)}, updatedAt=NOW.replace(tzinfo=None)
    )
    assert not select(legacy_naive, new)


def clinical_alert(fact, *, risk=False):
    return {
        'alertId': ('alert_focus_' if risk else 'alert_') + fact.fact_id,
        'severity': 'critical' if fact.interpretation == 'critical' else 'important',
        'title': fact.display_name + '指标异常',
        'factIds': [fact.fact_id],
        'status': 'active',
    }


def test_all_critical_preserved_three_noncritical_and_evidence_tiers_stable():
    critical = [
        measurement(
            'critical' + str(i), factType='laboratory.k' + str(i), category='laboratory', interpretation='critical'
        )
        for i in range(4)
    ]
    clinical = measurement('clinical', factType='laboratory.glucose', category='laboratory', displayName='血糖')
    risk = measurement('risk', factType='laboratory.alt', category='laboratory', displayName='肝酶')
    matched = measurement('matched', name='Plesiomonas shigelloides', number=0.3)
    extreme = measurement('extreme', name='Bacteroides coprocola', number=500)
    insights = [{'kind': 'risk', 'factIds': ['risk']}, association(matched)]
    facts = [*critical, clinical, risk, matched, extreme]
    base = [*(clinical_alert(fact) for fact in critical), clinical_alert(clinical), clinical_alert(risk, risk=True)]
    first = select(*facts, alerts=base, insights=insights, evidence_items=evidence(matched))
    second = select(*reversed(facts), alerts=list(reversed(base)), insights=insights, evidence_items=evidence(matched))
    assert first == second
    assert len(first) == 7 and sum(alert['severity'] == 'critical' for alert in first) == 4
    assert [alert['factIds'] for alert in first[4:]] == [['risk'], ['clinical'], ['matched']]


def test_deviation_priority_and_no_padding():
    facts = [measurement(str(i), name='菌种' + chr(0x4E00 + i), number=i * 0.2394) for i in (5, 6, 7, 8)]
    assert [alert['factIds'] for alert in select(*facts)] == [['8'], ['7'], ['6']]
    assert len(select(facts[0])) == 1


def test_risk_reminder_for_only_critical_fact_is_not_duplicated_and_ids_are_hidden():
    critical = measurement('fact_critical', factType='laboratory.k', category='laboratory', interpretation='critical')
    alert = clinical_alert(critical)
    risk = clinical_alert(critical, risk=True)
    risk['severity'] = 'important'
    assert select(critical, alerts=[alert, risk], insights=[{'kind': 'risk', 'factIds': [critical.fact_id]}]) == [alert]
    fact = measurement('fact_test', factType='laboratory.glucose', category='laboratory', displayName='血糖')
    risk = clinical_alert(fact, risk=True)
    risk['title'] = '建议评估血糖（fact_test；evidence_test；focus_test）'
    result = select(
        fact, alerts=[risk], insights=[{'kind': 'risk', 'topicId': 'focus_test', 'factIds': [fact.fact_id]}]
    )
    assert result[0]['title'] == '建议评估血糖'
    assert result[0]['alertId'] == risk['alertId'] and result[0]['factIds'] == ['fact_test']


def test_different_methods_specimens_and_units_are_not_fuzzy_deduplicated():
    first = measurement('first')
    method = measurement('method', value={**first.value, 'method': 'PCR'})
    specimen = measurement('specimen', value={**first.value, 'specimen': '血液'})
    assert len(select(first, method, specimen)) == 3


def add_measurement(case, add, identity='micro', **changes):
    original = add(identity, 'Bacteroides coprophilus', 'report_observation.' + identity, category='report_observation')
    fact = original.model_copy(
        update={
            'value': {'number': 5.5123, 'raw': '5.5123', 'kind': 'measurement'},
            'reference_range': {'text': '0-0.2394', 'low': 0, 'high': 0.2394},
            **changes,
        }
    )
    case.facts.documents = [
        fact.to_document(document_id=identity) if item['factId'] == identity else item for item in case.facts.documents
    ]
    return fact


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['success', 'synthesis_failure', 'fake_quote', 'no_relevant', 'all_failure'])
async def test_service_numeric_review_survives_model_failures_and_display_filters(mode):
    case, _, llm, add = await setup_case(mode=mode)
    add_measurement(case, add)
    first = await case.service.get_profile('synthetic-user', include_evidence=True)
    second = await case.service.get_profile('synthetic-user', include_evidence=True, indicator_scope='abnormal')
    assert first['alerts'] == second['alerts'] and len(first['alerts']) == 1
    assert second['indicatorGroups'] == []
    assert first['alerts'][0]['severity'] == 'important'
    assert '建议核对' in first['alerts'][0]['title']
    assert 'insights' not in first and 'complete' not in first
    assert len(llm.calls) <= 4  # 每次最多规划、综合两次，失败不增加复核调用。


@pytest.mark.asyncio
async def test_service_matching_quote_and_invalid_quote_have_different_review_outcomes():
    for mode, expected in [('success', True), ('fake_quote', False), ('synthesis_failure', False)]:
        case, _, _, add = await setup_case(mode=mode)
        add_measurement(case, add, display_name='类志贺邻单胞菌', value={'number': 0.3, 'kind': 'measurement'})
        profile = await case.service.get_profile('synthetic-user', include_evidence=True)
        assert bool(profile['alerts']) is expected
        if expected:
            assert profile['evidence']['items'][0]['factIds'] == ['micro']
            assert profile['alerts'][0]['severity'] == 'important'
            assert '胃肠炎' in profile['summary']['overall']
            assert '不能据此判断本人感染' in profile['summary']['overall']
        else:
            assert '胃肠炎' not in profile['summary']['overall']


@pytest.mark.asyncio
async def test_service_restart_keeps_selected_alerts_and_structured_bindings():
    from health_profile.service import HealthProfileService
    from tests.unit.health_profile.test_evidence_results import SharedLeases
    from tests.unit.health_profile.test_evidence_results import coordinator
    from tests.unit.health_profile.test_profile_read_and_subjects import FakeCollection

    case, _, _, add = await setup_case()
    add_measurement(case, add)
    collection, leases = FakeCollection(), SharedLeases()
    case.service._evidence_results = coordinator(collection, leases)
    first = await case.service.get_profile('synthetic-user', include_evidence=True)
    restarted = HealthProfileService(
        case.service._repository, clock=case.service._clock, evidence_results=coordinator(collection, leases)
    )
    second = await restarted.get_profile('synthetic-user', include_evidence=True, indicator_scope=['flagged'])
    assert first['alerts'] == second['alerts'] and first['evidence'] == second['evidence']
    assert first['alerts'][0]['factIds'] == ['micro']
    assert all('micro' not in item['title'] for item in first['alerts'])
