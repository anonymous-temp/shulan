"""Display validated model JSON without requiring source-reference metadata."""

from modules.analysis.application.analysis_display import display_result
from modules.analysis.application.analysis_display import has_displayable
from modules.analysis.application.analysis_outcomes import empty_analysis
from modules.analysis.application.analysis_outcomes import settle
from modules.analysis.application.analysis_validation import validate_analysis


def test_model_metadata_can_be_displayed_without_becoming_a_clinical_fact():
    data = validate_analysis(
        {'reportType': 'lab', 'title': '检验报告', 'reportTime': '2026-10-30', 'observations': []}
    ).data
    assert has_displayable(data)
    public = display_result(settle(data))
    assert public['processing']['status'] == 'partial'
    assert public['title'] == '检验报告'
    assert public['observations'] == []


def test_model_pending_items_are_preserved_without_physical_quotes_or_locators():
    data = validate_analysis(
        {
            'reportType': 'lab',
            'observations': [],
            'unresolvedItems': [
                {'itemName': '血红蛋白', 'resultValue': None, 'unit': 'g/L', 'reason': '原文结果栏为空'}
            ],
        }
    ).data
    assert has_displayable(data)
    public = display_result(settle(data))
    assert public['processing']['status'] == 'partial'
    assert public['unresolvedItems'][0]['itemName'] == '血红蛋白'
    assert public['unresolvedItems'][0]['resultValue'] is None
    assert 'evidence' not in public['unresolvedItems'][0]


def test_empty_json_has_no_displayable_clinical_information():
    data = empty_analysis()
    assert not has_displayable(data)
    assert display_result(settle(data))['processing']['status'] == 'unavailable'


def test_schema_valid_observations_are_displayed_without_source_ids():
    data = validate_analysis({'reportType': 'lab', 'observations': [{'itemName': '甲', 'resultValue': '0'}]}).data
    public = display_result(settle(data))
    assert public['processing']['status'] == 'complete'
    assert public['observations'][0]['resultValue'] == '0'
