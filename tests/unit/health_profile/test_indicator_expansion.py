"""报告与指标扩展的合成数据回归；不连接真实服务。"""

from datetime import datetime
from datetime import timezone

import pytest

from health_profile.ingestion import FactTypeRegistry
from health_profile.ingestion import HealthFactPolicy
from health_profile.ingestion import PolicyDisposition
from health_profile.models import AssertionLevel
from health_profile.models import FactCategory
from health_profile.models import FactSource
from health_profile.models import FactStatus
from health_profile.models import HealthFact
from health_profile.models import HealthProfileView
from health_profile.models import ProfileIngestionSourceType
from health_profile.models import SubjectKind
from health_profile.projection import build_profile_projection
from health_profile.report_ingestion import HealthReportCandidateMapper
from modules.analysis.application.analysis_validation import validate_analysis
from modules.analysis.application.service import HealthReportInterpreter
from tests.unit.health_profile.test_report_ingestion import ReportIngestionFixture


def report(items, **changes):
    return ReportIngestionFixture.record(
        result={
            'reportType': 'lab',
            'reportTime': '2026-09-01',
            'expansion': items,
            **changes,
        }
    )


def projection(candidates):
    return build_projection(candidates)


def build_projection(candidates, **overrides):
    facts = []
    policy = HealthFactPolicy()
    now = datetime(2026, 9, 7, tzinfo=timezone.utc)
    for index, candidate in enumerate(candidates):
        decision = policy.evaluate(candidate, subject_kind=SubjectKind.SELF)
        facts.append(
            HealthFact(
                fact_id=f'test-fact-{index}',
                user_id='user-1',
                subject_id='subject-self',
                fact_type=candidate.fact_type,
                category=decision.definition.category,
                display_name=candidate.display_name,
                value_type=candidate.value_type,
                value=candidate.value,
                interpretation=candidate.interpretation,
                reference_range=candidate.reference_range,
                assertion_level=candidate.assertion_level,
                verification_status=decision.verification_status,
                confidence=candidate.confidence,
                effective_period=candidate.effective_period,
                status=decision.fact_status,
                sources=(candidate.source,),
                created_at=now,
                updated_at=now,
            )
        )
    options = dict(
        user_id='user-1',
        subject=ReportIngestionFixture.subject(),
        view=HealthProfileView(user_id='user-1', subject_id='subject-self', updated_at=now),
        facts=tuple(facts),
        indicator_limit=12,
        generated_at=now,
    )
    options.update(overrides)
    return build_profile_projection(**options)


def test_microbiome_measurements_and_assessments_are_kept_separate():
    result = HealthReportCandidateMapper().map(
        report(
            [
                {'itemName': '菌群多样性', 'resultValue': '3.2'},
                {'itemName': '有益菌', 'resultValue': '5%', 'unit': '%'},
                {'itemName': '有害菌', 'resultValue': '10%', 'unit': '%'},
                {'itemName': '肠道年龄', 'resultValue': '38', 'unit': '岁', 'flag': '偏高'},
                {'itemName': '肠道微生物健康指数', 'resultValue': '-0.2', 'flag': '偏低'},
            ],
            reportType='microbiome',
        )
    )
    assert len(result.candidates) == 5
    profile = projection(result.candidates)
    groups = {group['code']: group for group in profile['indicatorGroups']}
    assert len(groups['microbiome']['indicators']) == 3
    assert len(groups['assessment']['indicators']) == 2
    assert all(item['trend'] == 'unknown' for group in groups.values() for item in group['indicators'])
    assert profile['alerts'] == []
    assert any(group['code'] == 'assessment' for group in profile['factGroups'])


def test_unregistered_and_qualitative_results_remain_visible_without_fake_trends():
    result = HealthReportCandidateMapper().map(
        report(
            [
                {'itemName': '测试专有检测甲', 'resultValue': '阴性', 'unit': None},
                {'itemName': '测试专有检测乙', 'resultValue': '1.2e3', 'unit': 'copies/mL'},
            ]
        )
    )
    assert result.skipped_item_count == 0
    assert len({candidate.fact_type for candidate in result.candidates}) == 2
    profile = projection(result.candidates)
    group = profile['indicatorGroups'][0]
    assert group['code'] == 'report_observation'
    assert {item['current']['value'] for item in group['indicators']} == {'阴性', 1200}
    assert all(item['trend'] == 'unknown' for item in group['indicators'])
    assert all(item['current']['sources'][0]['pointer'].startswith('/expansion/') for item in group['indicators'])


def test_full_blood_count_preserves_all_24_indicators():
    names = (
        '白细胞',
        '中性细胞比率',
        '淋巴细胞比率',
        '单核细胞比率',
        '嗜酸性粒细胞比率',
        '嗜碱性粒细胞比率',
        '中性细胞数',
        '淋巴细胞数',
        '单核细胞',
        '嗜酸性粒细胞',
        '嗜碱性粒细胞',
        '红细胞',
        '血红蛋白',
        '红细胞压积',
        '红细胞平均体积',
        '平均血红蛋白量',
        '平均血红蛋白浓度',
        '红细胞分布宽度',
        'RDW-CV',
        '血小板',
        '血小板分布宽度',
        '平均血小板体积',
        '血小板压积',
        '大型血小板比率',
    )
    result = HealthReportCandidateMapper().map(report([{'itemName': name, 'resultValue': '1.5'} for name in names]))
    assert len(result.candidates) == 24
    assert all(candidate.fact_type.startswith('laboratory.') for candidate in result.candidates)
    assert result.skipped_item_count == 0


def test_report_date_prefix_is_parsed_but_missing_time_is_not_fabricated():
    item = {'itemName': '血红蛋白', 'resultValue': '130', 'unit': 'g/L'}
    mapped = HealthReportCandidateMapper().map(report([item], reportTime='报告日期：2026/09/01 15:56'))
    assert len(mapped.candidates) == 1
    assert mapped.candidates[0].effective_period.start.hour == 15
    missing = HealthReportCandidateMapper().map(report([item], reportTime=None))
    assert not missing.candidates
    assert 'report_time_unavailable' in missing.reason_codes


def test_cross_type_observations_survive_unknown_and_checkup_normalization():
    interpreter = HealthReportInterpreter.__new__(HealthReportInterpreter)
    for kind in ('unknown', 'checkup', 'microbiome'):
        normalized = interpreter._normalize(
            {
                'reportType': kind,
                'reportTime': '2026-09-01',
                'observations': [{'itemName': '血糖', 'resultValue': '5.2', 'unit': 'mmol/L'}],
            }
        )
        mapped = HealthReportCandidateMapper().map(ReportIngestionFixture.record(result=normalized))
        assert len(mapped.candidates) == 1
        assert mapped.candidates[0].source.pointer == ('/expansion/0' if kind == 'microbiome' else '/observations/0')


def test_record_laboratory_values_are_allowed_but_still_unconfirmed():
    registry = FactTypeRegistry.default()
    definition = registry.require('laboratory.glucose')
    assert ProfileIngestionSourceType.RECORD in definition.permitted_source_types
    candidate = (
        HealthReportCandidateMapper()
        .map(
            report(
                [
                    {'itemName': '血糖', 'resultValue': '5.2', 'unit': 'mmol/L'},
                ]
            )
        )
        .candidates[0]
    )
    candidate = candidate.model_copy(
        update={
            'assertion_level': AssertionLevel.USER_REPORTED,
            'source': candidate.source.model_copy(update={'source_type': 'record'}),
        }
    )
    decision = HealthFactPolicy().evaluate(candidate, subject_kind=SubjectKind.SELF)
    assert decision.disposition is PolicyDisposition.PENDING
    assert decision.fact_status is FactStatus.PENDING
    assert decision.definition.category is FactCategory.LABORATORY


@pytest.mark.parametrize(
    ('older', 'newer', 'expected'),
    [
        ({'resultValue': '4', 'unit': 'mmol/L'}, {'resultValue': '5', 'unit': 'mmol/L'}, 'up'),
        ({'resultValue': '4', 'unit': 'mmol/L'}, {'resultValue': '90', 'unit': 'mg/dL'}, 'unknown'),
        ({'resultValue': '<4', 'unit': 'mmol/L'}, {'resultValue': '5', 'unit': 'mmol/L'}, 'unknown'),
        ({'resultValue': '阴性'}, {'resultValue': '阳性'}, 'unknown'),
        (
            {'resultValue': '4', 'unit': 'mmol/L', 'method': '方法甲'},
            {'resultValue': '5', 'unit': 'mmol/L', 'method': '方法乙'},
            'unknown',
        ),
    ],
)
def test_only_comparable_measurements_have_a_numeric_trend(older, newer, expected):
    mapper = HealthReportCandidateMapper()
    first = mapper.map(report([{'itemName': '血糖', **older}], reportTime='2026-08-01'))
    second = mapper.map(report([{'itemName': '血糖', **newer}]))
    data = projection(first.candidates + second.candidates)
    assert data['indicatorGroups'][0]['indicators'][0]['trend'] == expected


def test_abnormal_scope_filters_indicators_by_latest_observation():
    mapper = HealthReportCandidateMapper()
    normal = mapper.map(
        report(
            [{'itemName': '血糖', 'resultValue': '4.8', 'unit': 'mmol/L', 'flag': '正常'}],
            reportTime='2026-08-01',
        )
    )
    high = mapper.map(
        report(
            [{'itemName': '血糖', 'resultValue': '9.2', 'unit': 'mmol/L', 'flag': '偏高'}],
            reportTime='2026-09-01',
        )
    )
    microbiome = HealthReportCandidateMapper().map(
        report([{'itemName': '菌群多样性', 'resultValue': '3.2'}], reportType='microbiome')
    )
    candidates = normal.candidates + high.candidates + microbiome.candidates

    full = build_projection(candidates)
    assert {group['code'] for group in full['indicatorGroups']} == {'laboratory', 'microbiome'}

    abnormal = build_projection(candidates, indicator_scope='abnormal')
    assert [group['code'] for group in abnormal['indicatorGroups']] == ['laboratory']
    indicator = abnormal['indicatorGroups'][0]['indicators'][0]
    assert indicator['current']['value'] == 9.2
    assert indicator['current']['interpretation'] == 'high'
    # 整组观测随异常指标保留，不逐条过滤。
    assert [item['value'] for item in indicator['observations']] == [4.8, 9.2]

    # 最新观测正常时，同一指标即使历史有异常也不保留。
    recovered = build_projection(normal.candidates, indicator_scope='abnormal')
    assert recovered['indicatorGroups'] == []


def test_flagged_scope_keeps_source_flagged_background_indicators():
    flagged_microbiome = HealthReportCandidateMapper().map(
        report(
            [
                {'itemName': '测试菌属甲', 'resultValue': '0.4', 'unit': '%', 'flag': '偏高'},
                {'itemName': '测试菌属乙', 'resultValue': '0.02', 'unit': '%', 'flag': '正常'},
            ],
            reportType='microbiome',
        )
    )
    standard_microbiome = HealthReportCandidateMapper().map(
        report(
            [{'itemName': '肠道年龄', 'resultValue': '52', 'unit': '岁', 'flag': '偏高'}],
            reportType='microbiome',
        )
    )
    candidates = flagged_microbiome.candidates + standard_microbiome.candidates

    # abnormal 是临床口径：未标准化项目不参与；肠道年龄属背景评估也不参与。
    clinical = build_projection(candidates, indicator_scope='abnormal')
    assert clinical['indicatorGroups'] == []

    # flagged 是来源标记口径：带偏高标记的菌属与评估都保留。
    flagged = build_projection(candidates, indicator_scope='flagged')
    groups = {group['code']: group for group in flagged['indicatorGroups']}
    assert set(groups) == {'report_observation', 'assessment'}
    assert groups['report_observation']['indicators'][0]['name'] == '测试菌属甲'
    assert groups['report_observation']['indicators'][0]['current']['interpretation'] == 'high'
    assessment = groups['assessment']['indicators'][0]
    assert assessment['factType'] == 'assessment.microbiome_age'
    assert assessment['current']['interpretation'] == 'high'
    # normal 与无标记的项目被过滤。
    assert all(
        item['current']['interpretation'] != 'normal' for group in groups.values() for item in group['indicators']
    )


def test_pending_scope_returns_latest_pending_and_disputed_indicators():
    mapper = HealthReportCandidateMapper()
    active = mapper.map(report([{'itemName': '血糖', 'resultValue': '9.2', 'unit': 'mmol/L', 'flag': '偏高'}]))
    pending_chat = active.candidates[0].model_copy(
        update={
            'assertion_level': AssertionLevel.USER_REPORTED,
            'source': FactSource(
                source_type='chat',
                source_id='message-pending-1',
                source_revision=1,
            ),
        }
    )

    later_pending = pending_chat.model_copy(
        update={'effective_period': pending_chat.effective_period.model_copy(update={'start': datetime(2026, 9, 3)})},
    )
    data = build_projection((pending_chat, later_pending), indicator_scope='pending')
    group = data['indicatorGroups'][0]
    assert group['code'] == 'laboratory'
    entry = group['indicators'][0]
    assert entry['current']['status'] == 'pending'
    assert entry['current']['factId'] == 'test-fact-1'
    assert entry['trend'] == 'unknown'
    assert len(entry['observations']) == 1

    # all 与 abnormal 模式下 pending 不出现（现状回归）。
    assert build_projection((pending_chat,), indicator_scope='all')['indicatorGroups'] == []
    assert build_projection((pending_chat,), indicator_scope='abnormal')['indicatorGroups'] == []

    # 复合：flagged 只看 active 标记，pending 单独成条；联合不重复。
    combined = build_projection(
        (pending_chat,) + active.candidates,
        indicator_scope='flagged,pending',
    )
    flat = [item for group in combined['indicatorGroups'] for item in group['indicators']]
    assert [item['current']['status'] for item in flat] == ['active', 'pending']


def test_abnormal_and_flagged_union_deduplicates_by_indicator():
    mapper = HealthReportCandidateMapper()
    high_lab = mapper.map(
        report(
            [{'itemName': '血糖', 'resultValue': '9.2', 'unit': 'mmol/L', 'flag': '偏高'}],
            reportTime='2026-09-01',
        )
    )
    data = build_projection(high_lab.candidates, indicator_scope='abnormal,flagged')
    flat = [item for group in data['indicatorGroups'] for item in group['indicators']]
    assert len(flat) == 1
    assert flat[0]['current']['interpretation'] == 'high'


@pytest.mark.parametrize(
    ('value', 'reference', 'status', 'basis', 'reason'),
    [
        ('0', '0-5', 'normal', 'reference_range', None),
        ('5', '0-5', 'normal', 'reference_range', None),
        ('5.1', '0-5', 'high', 'reference_range', None),
        ('-1', '-0.8--0.2', 'low', 'reference_range', None),
        ('1e1', '3.5-9.5 10^9/L', 'high', 'reference_range', None),
        ('<5', '5-10', 'low', 'reference_range', None),
        ('<=5', '5-10', 'unknown', None, 'inconclusive_comparator'),
        ('5', '<5', 'high', 'reference_range', None),
        ('5', '<=5', 'normal', 'reference_range', None),
        ('<5', '<=5', 'normal', 'reference_range', None),
        ('>=5', '>5', 'unknown', None, 'inconclusive_comparator'),
        ('6.2', '3.5-9.5 mg/L', 'unknown', None, 'unit_mismatch'),
        ('6.2', '儿童 3.5-9.5，成人 4-10', 'unknown', None, 'unsupported_reference_range'),
        ('6.2', '9.5-3.5', 'unknown', None, 'unsupported_reference_range'),
        ('6.2', None, 'unknown', None, 'missing_reference_range'),
        ('阴性', None, 'negative', 'source_result', None),
        ('未检出', None, 'not_detected', 'source_result', None),
        ('检出', None, 'detected', 'source_result', None),
        ('少量', None, 'unknown', None, 'non_numeric_value'),
    ],
)
def test_indicator_result_judgment_uses_the_same_observation(value, reference, status, basis, reason):
    candidates = (
        HealthReportCandidateMapper()
        .map(
            report(
                [
                    {
                        'itemName': '白细胞计数',
                        'resultValue': value,
                        'unit': '10^9/L',
                        'referenceRange': reference,
                    }
                ],
            )
        )
        .candidates
    )
    data = build_projection(candidates)
    indicator = data['indicatorGroups'][0]['indicators'][0]
    assert indicator['current']['resultJudgment'] == {'status': status, 'basis': basis, 'reason': reason}
    assert indicator['current']['interpretation'] == 'unknown'
    assert indicator['current']['status'] == 'active'
    assert 'resultJudgment' not in indicator['observations'][0]
    assert 'resultJudgment' not in data['factGroups'][0]['items'][0]


@pytest.mark.parametrize(
    ('raw', 'unit', 'reference', 'reason'),
    [
        ('11.0', '×10⁹/L', '4.0–10.0 ×10⁹/L', None),
        ('11.0', '10^9/L', '4.0–10.0 ×10⁹/L', None),
        ('11.0 ×10⁹/L', '10^9/L', '≤10 ×10^9/L', None),
        ('11.0', '× 10⁹ / L', '4.0–10.0 10^9/L', None),
        ('11.0', '×10⁹/L', '4.0–10.0 ×10⁶/L', 'unit_mismatch'),
        ('11.0', '×10⁹/L', '4.0–10.0 ×10⁹/mL', 'unit_mismatch'),
        ('11.0', '10⁹/mL', '4.0–10.0 ×10⁹/ML', 'unit_mismatch'),
    ],
)
def test_reference_judgment_compares_equivalent_count_unit_spellings(raw, unit, reference, reason):
    (candidate,) = (
        HealthReportCandidateMapper()
        .map(report([{'itemName': '白细胞计数', 'resultValue': '11.0', 'unit': unit, 'referenceRange': reference}]))
        .candidates
    )
    candidate = candidate.model_copy(update={'value': {**candidate.value, 'raw': raw}})
    data = build_projection((candidate,))
    current = data['indicatorGroups'][0]['indicators'][0]['current']
    status, basis = ('unknown', None) if reason else ('high', 'reference_range')
    assert current['resultJudgment'] == {'status': status, 'basis': basis, 'reason': reason}
    assert (current['rawValue'], current['unit'], current['referenceRange']) == (raw, unit, reference)
    assert current['interpretation'] == 'unknown'
    assert data['alerts'] == []
    unjudged = build_projection((candidate,), indicator_scope='unjudged')
    assert bool(unjudged['indicatorGroups']) == (reason is not None)


def test_range_judgment_changes_unjudged_scope_without_creating_clinical_abnormality():
    candidates = (
        HealthReportCandidateMapper()
        .map(
            report([{'itemName': '血糖', 'resultValue': '9.2', 'unit': 'mmol/L', 'referenceRange': '3.9-6.1'}]),
        )
        .candidates
    )
    data = build_projection(candidates)
    current = data['indicatorGroups'][0]['indicators'][0]['current']
    assert current['resultJudgment']['status'] == 'high'
    assert current['interpretation'] == 'unknown'
    assert data['alerts'] == []
    assert data['summary'] == build_projection((candidates[0].model_copy(update={'reference_range': None}),))['summary']
    for scope in ('unjudged', 'abnormal', 'flagged'):
        assert build_projection(candidates, indicator_scope=scope)['indicatorGroups'] == []


@pytest.mark.parametrize(('public_analysis', 'basis'), [(True, 'report_flag'), (False, 'source_flag')])
def test_source_flag_takes_precedence_and_shows_conflicting_range_comparison(public_analysis, basis):
    candidates = (
        HealthReportCandidateMapper()
        .map(
            report(
                [{'itemName': '血糖', 'resultValue': '4', 'unit': 'mmol/L', 'referenceRange': '3.9-6.1', 'flag': 'H'}]
            ),
        )
        .candidates
    )
    if public_analysis:
        candidate = candidates[0]
        source = candidate.source.model_copy(update={'confirmation_basis': 'analysis_public'})
        candidates = (candidate.model_copy(update={'source': source}),)
    data = build_projection(candidates, indicator_scope='flagged')
    current = data['indicatorGroups'][0]['indicators'][0]['current']
    assert current['resultJudgment'] == {
        'status': 'high',
        'basis': basis,
        'reason': None,
        'referenceStatus': 'normal',
    }
    assert current['interpretation'] == 'high'
    assert data['alerts']


def test_invalid_legacy_comparator_is_explained_instead_of_failing_profile_read():
    (candidate,) = (
        HealthReportCandidateMapper()
        .map(report([{'itemName': '血糖', 'resultValue': '4', 'unit': 'mmol/L', 'referenceRange': '3.9-6.1'}]))
        .candidates
    )
    candidate = candidate.model_copy(update={'value': {**candidate.value, 'comparator': []}})
    data = build_projection((candidate,), indicator_scope='unjudged')
    current = data['indicatorGroups'][0]['indicators'][0]['current']
    assert current['resultJudgment'] == {'status': 'unknown', 'basis': None, 'reason': 'inconclusive_comparator'}


def test_projected_original_unit_keeps_value_comparable_with_the_original_range():
    (candidate,) = (
        HealthReportCandidateMapper()
        .map(report([{'itemName': '血糖', 'resultValue': '4', 'unit': 'mmol/L', 'referenceRange': '3.9-6.1'}]))
        .candidates
    )
    candidate = candidate.model_copy(
        update={'value': {**candidate.value, 'normalizedNumber': 72.07, 'normalizedUnit': 'mg/dL'}},
    )
    current = build_projection((candidate,))['indicatorGroups'][0]['indicators'][0]['current']
    assert (current['value'], current['unit']) == (72.07, 'mg/dL')
    assert (current['rawValue'], current['rawUnit']) == ('4', 'mmol/L')
    assert current['resultJudgment'] == {'status': 'normal', 'basis': 'reference_range', 'reason': None}


def test_unjudged_scope_keeps_active_entries_without_source_flag():
    mapped = HealthReportCandidateMapper().map(
        report(
            [
                {'itemName': '测试菌属甲', 'resultValue': '0.4', 'unit': '%', 'flag': '偏高'},
                {'itemName': '测试菌属乙', 'resultValue': '0.02', 'unit': '%', 'flag': '正常'},
                {'itemName': '测试菌属丙', 'resultValue': '0.1', 'unit': '%'},
                {'itemName': '血糖', 'resultValue': '5.2', 'unit': 'mmol/L', 'flag': '正常'},
            ],
            reportType='microbiome',
        )
    )
    candidates = mapped.candidates

    unjudged = build_projection(candidates, indicator_scope='unjudged')
    flat = [item for group in unjudged['indicatorGroups'] for item in group['indicators']]
    assert [item['current']['factId'] for item in flat] == ['test-fact-2']
    assert flat[0]['current']['interpretation'] == 'unknown'
    assert flat[0]['current']['status'] == 'active'

    # flagged 与 unjudged 覆盖互补：联合 = 全部非 normal 指标。
    combined = build_projection(candidates, indicator_scope='flagged,unjudged')
    combined_flat = [item for group in combined['indicatorGroups'] for item in group['indicators']]
    assert {item['current']['factId'] for item in combined_flat} == {'test-fact-0', 'test-fact-2'}

    # normal 与带标记项不出现在 unjudged。
    assert 'test-fact-1' not in {item['current']['factId'] for item in flat}
    assert 'test-fact-3' not in {item['current']['factId'] for item in flat}


def test_explicit_detection_is_flagged_not_unjudged():
    mapped = HealthReportCandidateMapper().map(
        report(
            [{'itemName': '类志贺邻单胞菌', 'resultValue': '检出'}],
            reportType='microbiome',
        )
    )
    flagged = build_projection(mapped.candidates, indicator_scope='flagged')
    unjudged = build_projection(mapped.candidates, indicator_scope='unjudged')
    assert flagged['indicatorGroups'][0]['indicators'][0]['name'] == '类志贺邻单胞菌'
    assert unjudged['indicatorGroups'] == []


def test_fact_statuses_filter_only_fact_groups():
    mapper = HealthReportCandidateMapper()
    mapped = mapper.map(
        report(
            [
                {'itemName': '血糖', 'resultValue': '5.2', 'unit': 'mmol/L'},
                {'itemName': '尿酸', 'resultValue': '520', 'unit': 'umol/L'},
            ]
        )
    )
    # 报告来源候选经策略判定为 active；用显式 pending 事实验证状态过滤。
    active_candidates = mapped.candidates
    pending_candidate = active_candidates[0].model_copy(
        update={
            'assertion_level': AssertionLevel.USER_REPORTED,
            'source': active_candidates[0].source.model_copy(update={'source_type': 'record'}),
        }
    )

    candidates = active_candidates + (pending_candidate,)
    default = build_projection(candidates)
    active_only = build_projection(candidates, fact_statuses=frozenset({FactStatus.ACTIVE}))
    pending_only = build_projection(candidates, fact_statuses=frozenset({FactStatus.PENDING}))

    assert [item['status'] for item in default['factGroups'][0]['items']] == ['active', 'active', 'pending']
    assert [item['status'] for item in active_only['factGroups'][0]['items']] == ['active', 'active']
    assert [item['status'] for item in pending_only['factGroups'][0]['items']] == ['pending']
    assert default['overview']['pendingConfirmationCount'] == 1
    for projection in (active_only, pending_only):
        assert projection['overview'] == default['overview']
        assert projection['summary'] == default['summary']
        assert projection['indicatorGroups'] == default['indicatorGroups']
        assert projection['alerts'] == default['alerts']
        assert projection['profileVersion'] == default['profileVersion']


def test_imaging_findings_are_pending_facts_and_never_numeric_indicators():
    mapped = HealthReportCandidateMapper().map(
        report(
            {'findings': '示例影像所见', 'diagnosis': '考虑示例病变，需进一步检查'},
            reportType='imaging',
        )
    )
    data = projection(mapped.candidates)
    assert len(mapped.candidates) == 2
    assert data['indicatorGroups'] == []
    assert data['alerts'] == []
    assert {item['status'] for item in data['factGroups'][0]['items']} == {'pending'}


def test_mirrored_observations_preserve_legacy_pointer_without_double_ingestion():
    item = {'itemName': '血糖', 'resultValue': '5', 'unit': 'mmol/L'}
    result = HealthReportCandidateMapper().map(report([item], observations=[item]))
    assert len(result.candidates) == 1
    assert result.candidates[0].source.pointer == '/expansion/0'


def test_assessment_label_does_not_become_a_clinical_lab_alert():
    mapped = HealthReportCandidateMapper().map(
        report(
            [
                {'itemName': '血糖', 'resultValue': '80', 'kind': 'assessment', 'flag': '偏高'},
            ]
        )
    )
    data = projection(mapped.candidates)
    assert data['indicatorGroups'][0]['code'] == 'report_observation'
    assert data['alerts'] == []


def test_each_measurement_uses_its_source_date_without_filling_a_missing_one():
    mapped = HealthReportCandidateMapper().map(
        report(
            [
                {'itemName': '血糖', 'resultValue': '5', 'observedAt': '2026-08-01'},
                {'itemName': '白蛋白', 'resultValue': '40'},
            ],
            reportTime=None,
        )
    )
    assert len(mapped.candidates) == 1
    assert mapped.source_item_count == 2
    assert mapped.skipped_item_count == 1
    assert mapped.reason_codes == ('report_time_unavailable',)


def test_body_measurements_keep_report_units_without_corrupting_overview():
    mapped = HealthReportCandidateMapper().map(
        report(
            [
                {'itemName': '身高', 'resultValue': '1.7', 'unit': 'm'},
                {'itemName': '体重', 'resultValue': '60000', 'unit': 'g'},
            ]
        )
    )
    data = projection(mapped.candidates)
    assert data['overview']['heightCm'] == 170
    assert data['overview']['weightKg'] == 60
    assert data['overview']['bmi'] == 20.8
    assert {item['current']['unit'] for item in data['indicatorGroups'][0]['indicators']} == {'m', 'g'}


def test_ambiguous_body_measurement_is_preserved_as_unmapped_result():
    mapped = HealthReportCandidateMapper().map(
        report(
            [
                {'itemName': '身高', 'resultValue': '正常'},
                {'itemName': '体重', 'resultValue': '120'},
            ]
        )
    )
    data = projection(mapped.candidates)
    assert data['indicatorGroups'][0]['code'] == 'report_observation'
    assert data['overview']['weightKg'] is None
    assert data['overview']['heightCm'] is None


def test_numeric_parser_does_not_turn_multiple_values_into_one_number():
    mapped = HealthReportCandidateMapper().map(
        report(
            [
                {'itemName': '测试项目甲', 'resultValue': '1,2'},
                {'itemName': '测试项目乙', 'resultValue': '1,200.5'},
            ]
        )
    )
    assert mapped.candidates[0].value == '1,2'
    assert mapped.candidates[1].value['number'] == 1200.5


def test_repeated_items_inside_observations_keep_their_source_positions():
    item = {'itemName': '测试项目', 'resultValue': '阴性'}
    mapped = HealthReportCandidateMapper().map(
        report(
            [{'itemName': '白蛋白', 'resultValue': '40'}],
            observations=[item, item],
        )
    )
    assert [candidate.source.pointer for candidate in mapped.candidates] == [
        '/expansion/0',
        '/observations/0',
        '/observations/1',
    ]


def test_new_report_mirrors_merge_optional_metadata_before_persistence():
    interpreter = HealthReportInterpreter.__new__(HealthReportInterpreter)
    item = {'itemName': '血糖', 'resultValue': '80', 'unit': None}
    normalized = interpreter._normalize(
        {
            'reportType': 'lab',
            'reportTime': '2026-09-01',
            'expansion': [item],
            'observations': [{**item, 'kind': 'assessment', 'method': '测试算法'}],
        }
    )
    assert normalized['expansion'] == normalized['observations']
    assert normalized['expansion'][0]['kind'] == 'assessment'
    mapped = HealthReportCandidateMapper().map(ReportIngestionFixture.record(result=normalized))
    assert len(mapped.candidates) == 1
    assert mapped.candidates[0].fact_type.startswith('report_observation.')


@pytest.mark.parametrize('validate_first', [False, True])
def test_new_report_mirrors_do_not_merge_conflicting_methods(validate_first):
    interpreter = HealthReportInterpreter.__new__(HealthReportInterpreter)
    item = {'itemName': '血糖', 'resultValue': '5', 'unit': 'mmol/L'}
    data = {
        'reportType': 'lab',
        'reportTime': '2026-09-01',
        'expansion': [{**item, 'method': '方法甲'}],
        'observations': [{**item, 'method': '方法乙'}],
    }
    if validate_first:
        validated = validate_analysis(data)
        assert validated.valid
        data = validated.data
    normalized = interpreter._normalize(data)
    mapped = HealthReportCandidateMapper().map(ReportIngestionFixture.record(result=normalized))
    assert len(mapped.candidates) == 2
    assert {candidate.value['method'] for candidate in mapped.candidates} == {'方法甲', '方法乙'}
