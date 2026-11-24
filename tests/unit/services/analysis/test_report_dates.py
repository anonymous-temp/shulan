"""The JSON date contract does not infer source dates through document regular expressions."""

import pytest

from modules.analysis.application.analysis_validation import validate_analysis


@pytest.mark.parametrize('date', [None, '2026-10-30', '2026-10-30T10:15:00'])
def test_nullable_date_strings_are_json_fields(date):
    result = validate_analysis(
        {
            'reportType': 'lab',
            'reportTime': date,
            'observations': [{'itemName': '甲', 'resultValue': '0', 'observedAt': date}],
        }
    )
    assert result.valid and result.issues == []
    assert result.data['reportTime'] == date
    assert result.data['observations'][0]['observedAt'] == date


@pytest.mark.parametrize('date', [0, False, [], {}])
def test_invalid_date_types_fail_the_schema_instead_of_a_source_quote_check(date):
    result = validate_analysis({'reportType': 'lab', 'reportTime': date, 'observations': []})
    assert not result.valid
    assert result.issues[0]['code'] == 'invalid_json_schema'
