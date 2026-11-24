from health_profile.report_ingestion import _report_observations
from modules.analysis.application.service import HealthReportInterpreter
from utils.report_content import report_public_content


def test_original_and_section_same_value_keep_their_own_dates():
    interpreter = HealthReportInterpreter.__new__(HealthReportInterpreter)
    item = {'itemName': '血糖', 'resultValue': 5, 'unit': 'mmol/L'}
    result = interpreter._normalize(
        {
            'reportType': 'lab',
            'reportTime': '2026-01-01',
            'observations': [item],
            'sections': [{'reportType': 'lab', 'reportTime': '2026-02-01', 'observations': [item]}],
        }
    )
    assert [row.get('observedAt', result['reportTime']) for row in result['observations']] == [
        '2026-01-01',
        '2026-02-01',
    ]


def test_historical_allowlist_only_limits_legacy_base_snapshots():
    row = {'itemName': '血糖', 'resultValue': '5.1'}
    report = {
        'reportType': 'lab',
        'processing': {'version': 'analysis-health-report-v12'},
        'observations': [row],
        'verifiedObservationPointers': [],
    }
    assert _report_observations(report) == []
    assert _report_observations(report, for_ingestion=False) == []
    assert _report_observations({**report, 'reportSource': {}}) == [('/observations/0', row)]
    current = {key: value for key, value in report.items() if key != 'verifiedObservationPointers'}
    assert _report_observations(current) == [('/observations/0', row)]
    assert 'verifiedObservationPointers' not in report_public_content(report)


def test_historical_materialized_ingestion_projection_takes_precedence():
    row = {'sourceObservationId': 'archived-row', 'itemName': '血糖', 'resultValue': '5.1'}
    report = {'verifiedObservationPointers': [], 'ingestionObservations': [row]}
    assert _report_observations(report) == [('/sourceObservations/archived-row', row)]
