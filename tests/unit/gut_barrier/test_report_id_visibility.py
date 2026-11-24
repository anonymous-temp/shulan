"""Report identifiers remain data keys, not patient-facing copy."""

from copy import deepcopy

import pytest

from modules.gut_barrier.publication import project_callback_report, project_public_report
from tests.unit.gut_barrier.report_fixtures import document


@pytest.mark.parametrize("projector", [lambda report: report, project_public_report, project_callback_report])
def test_new_report_omits_visible_identifier_without_removing_metadata(projector):
    report = projector(document())
    assert report["meta"]["reportId"] == "ACTUAL-REPORT-01"
    assert not any(item.get("label") == "报告编号"
                   for section in report["sections"] for block in section["blocks"]
                   if block["kind"] == "facts" for item in block["items"])


@pytest.mark.parametrize("schema_version", ["gut-barrier-public.v1", "gut-barrier-public.v2"])
@pytest.mark.parametrize("identifier", ["HISTORICAL-REPORT-ID", None, 0, False])
def test_old_callback_identifier_is_preserved_by_reprojection(schema_version, identifier):
    projector = project_callback_report if schema_version.endswith("v2") else project_public_report
    report = projector(document())
    report["meta"]["reportId"] = identifier
    overview = next(section for section in report["sections"] if section["id"] == "overview")
    facts = next(block for block in overview["blocks"] if block["kind"] == "facts")
    facts["items"].append({"label": "报告编号", "value": identifier})
    before = deepcopy(report)
    assert projector(report) == before
    assert report == before
    assert facts["items"][-1] == {"label": "报告编号", "value": identifier}
