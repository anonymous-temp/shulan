"""Keep upload time separate from examination time in Profile reads."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from health_profile.alerts import current_alert_facts
from health_profile.models import HealthFact
from health_profile.models import HealthProfileView
from health_profile.models import HealthSubject
from health_profile.projection import build_profile_projection
from health_profile.projection import evidence_fact_payloads

NOW = datetime(2026, 11, 12, tzinfo=UTC)


def reading(name, number, *, examined=None, recorded=None, report=None, **qualifiers):
    source = {'sourceType': 'health_report', 'sourceId': report or name, 'sourceRevision': 1, 'observedAt': examined}
    if recorded is not None:
        source.update(confirmationBasis='analysis_public', recordedAt=recorded)
    return HealthFact.model_validate(
        {
            'factId': name,
            'userId': 'synthetic',
            'subjectId': 'self',
            'category': 'laboratory',
            'factType': 'laboratory.glucose',
            'displayName': 'Synthetic glucose',
            'valueType': 'quantity',
            'value': {'number': number, 'unit': 'mmol/L', **qualifiers},
            'assertionLevel': 'observed',
            'verificationStatus': 'confirmed' if recorded is not None else 'verified_source',
            'interpretation': 'high',
            'confidence': 1,
            'effectivePeriod': {'start': examined},
            'status': 'active',
            'sources': [source],
            'createdAt': NOW,
            'updatedAt': NOW,
        }
    )


def project_profile(*facts, limit=12):
    return build_profile_projection(
        user_id='synthetic',
        subject=HealthSubject(user_id='synthetic', subject_id='self', kind='self', created_at=NOW, updated_at=NOW),
        view=HealthProfileView(user_id='synthetic', subject_id='self', updated_at=NOW),
        facts=facts,
        indicator_limit=limit,
        generated_at=NOW,
    )


def project(*facts, limit=12):
    return project_profile(*facts, limit=limit)['indicatorGroups'][0]['indicators'][0]


def test_recorded_trend_uses_only_fixed_publication_times_and_distinct_reports():
    later = NOW + timedelta(days=1)
    corrected_first = reading('older', 4, recorded=NOW).model_copy(update={'updated_at': later + timedelta(days=1)})
    second = reading('newer', 6, recorded=later)
    item = project(second, corrected_first)

    assert item['current']['factId'] == 'newer'
    assert item['trend'] == 'unknown'
    assert item['recordedTrend'] == {'direction': 'up', 'basis': 'recordedAt'}
    observations = {entry['factId']: entry for entry in item['observations']}
    assert observations['older']['recordedAt'] == '2026-11-12T00:00:00Z'
    assert observations['newer']['recordedAt'] == '2026-11-13T00:00:00Z'
    assert all(entry['observedAt'] is None for entry in observations.values())
    assert observations['older']['sources'][0]['confirmationBasis'] == 'analysis_public'


def test_recorded_and_examined_trends_stay_independent():
    early = NOW - timedelta(days=2)
    later = NOW + timedelta(days=1)
    undated = reading('undated', 4, recorded=NOW)
    newer_upload = reading('another', 6, recorded=later)
    dated = reading('dated', 9, examined=early)
    latest_exam = reading('recent', 7, examined=NOW)
    display = project(undated, newer_upload, dated, latest_exam, limit=2)
    assert display['current']['factId'] == 'recent'
    assert 'recordedAt' not in display['current']
    assert display['trend'] == 'down'
    assert display['recordedTrend'] == {'direction': 'up', 'basis': 'recordedAt'}
    assert project(dated, undated)['trend'] == 'unknown'
    assert 'recordedTrend' not in project(dated, undated)
    assert 'recordedTrend' not in project(undated, newer_upload, limit=1)


@pytest.mark.parametrize(
    'changes',
    [
        {'report': 'same'},
        {'recorded': NOW},
        {'unit': 'mg/dL'},
        {'method': 'another method'},
        {'specimen': 'urine'},
        {'comparator': '>'},
    ],
)
def test_recorded_trend_requires_comparable_readings(changes):
    older = reading('original', 4, recorded=NOW, report='same')
    newer = reading('another', 6, **({'recorded': NOW + timedelta(days=1)} | changes))
    assert 'recordedTrend' not in project(older, newer)


def test_same_report_with_multiple_observations_is_not_a_second_time_point():
    older = reading('first', 4, recorded=NOW, report='first-report')
    another = reading('another', 5, recorded=NOW, report='first-report')
    assert 'recordedTrend' not in project(older, another)
    newer = reading('newer', 6, recorded=NOW + timedelta(days=1))
    assert 'recordedTrend' not in project(older, another, newer)


def test_republication_of_an_earlier_undated_report_does_not_change_current_assessment():
    later = NOW + timedelta(days=1)
    older = reading('older', 12, recorded=NOW).model_copy(update={'updated_at': later + timedelta(days=1)})
    newer = reading('newer', 4, recorded=later).model_copy(update={'interpretation': 'normal'})
    assert project_profile(older)['alerts'][0]['factIds'] == ['older']

    profile = project_profile(older, newer)
    indicator = profile['indicatorGroups'][0]['indicators'][0]
    assert indicator['current']['factId'] == 'newer'
    assert indicator['trend'] == 'unknown'
    assert indicator['recordedTrend'] == {'direction': 'down', 'basis': 'recordedAt'}
    assert profile['factGroups'][0]['items'][0]['factId'] == 'newer'
    assert profile['alerts'] == []
    assert profile['summary']['priorities'] == []
    assert '正常' in profile['summary']['overall']
    assert list(current_alert_facts((older, newer)).values()) == [newer]
    (candidate,) = evidence_fact_payloads((older, newer))
    assert candidate['factId'] == 'newer' and candidate['observedAt'] is None


def test_unknown_date_fact_does_not_supersede_a_fact_with_an_examination_date():
    undated = reading('undated', 12, recorded=NOW)
    dated = reading('dated', 4, examined=NOW - timedelta(days=1))
    assert list(current_alert_facts((undated,)).values()) == [undated]
    assert list(current_alert_facts((undated, dated)).values()) == [dated]
    assert evidence_fact_payloads((undated,))[0]['observedAt'] is None
