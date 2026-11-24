"""Report identifiers remain data keys, not patient-facing copy."""

from copy import deepcopy

import json

import re

import pytest

from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE, WORD_REVIEW_DISPLAY_TEMPLATE

from modules.gut_barrier.publication import project_callback_report, project_public_report

from tests.unit.gut_barrier.preview.support import document, visible_source

def embedded_report(html):
    return json.loads(re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S)[1])

@pytest.mark.parametrize("projector", [lambda report: report, project_public_report, project_callback_report])
def test_new_report_omits_visible_identifier_without_removing_metadata(projector, historical_templates):
    internal = document()
    internal["meta"]["displayTemplateVersion"] = LATEST_DISPLAY_TEMPLATE
    report = projector(internal)
    assert report["meta"]["reportId"] == "ACTUAL-REPORT-01"
    assert not any(item.get("label") == "报告编号"
                   for section in report["sections"] for block in section["blocks"]
                   if block["kind"] == "facts" for item in block["items"])
    envelope = {"type": "gutBarrier", "schemaVersion": "2.0", "finalReportId": "ACTUAL-REPORT-01",
                "userId": "u", "sessionId": "s", "status": "partial", "report": report, "error": None}
    before = deepcopy(envelope)
    version = WORD_REVIEW_DISPLAY_TEMPLATE if report.get("schemaVersion") == "gut-barrier-public.v1" else LATEST_DISPLAY_TEMPLATE
    html = render_report_html(envelope["report"], template_version=version)
    visible = visible_source(html)
    assert "报告编号" not in visible and "ACTUAL-REPORT-01" not in visible
    assert 'class="cover-id"' not in visible
    assert "真实输入姓名" in visible and "46 岁" in visible
    assert embedded_report(html) == report
    assert envelope == before
    assert envelope["finalReportId"] == embedded_report(html)["meta"]["reportId"]

@pytest.mark.parametrize("schema_version", ["gut-barrier-public.v1", "gut-barrier-public.v2"])
@pytest.mark.parametrize("identifier", ["HISTORICAL-REPORT-ID", None, 0, False])
def test_old_callback_fact_is_hidden_without_mutating_or_removing_historical_data(schema_version, identifier, historical_templates):
    projector = project_callback_report if schema_version.endswith("v2") else project_public_report
    report = projector(document())
    report["meta"]["reportId"] = identifier
    overview = next(section for section in report["sections"] if section["id"] == "overview")
    facts = next(block for block in overview["blocks"] if block["kind"] == "facts")
    facts["items"].append({"label": "报告编号", "value": identifier})
    before = deepcopy(report)
    html = render_report_html(report, template_version=WORD_REVIEW_DISPLAY_TEMPLATE)
    visible = visible_source(html)
    assert "报告编号" not in visible
    if isinstance(identifier, str):
        assert identifier not in visible
    assert report == before == embedded_report(html)
    assert facts["items"][-1] == {"label": "报告编号", "value": identifier}

def test_legacy_nonchapter_preview_also_hides_report_number_in_folio_and_facts():
    report = {"meta": {"title": "历史报告", "reportId": "LEGACY-ID"}, "sections": [
        {"id": "legacy", "title": "结果", "blocks": [{"kind": "facts", "items": [
            {"label": " 报告编号 ", "value": "LEGACY-ID"}, {"label": "姓名", "value": "历史受检者"},
        ]}]},
    ]}
    before = deepcopy(report)
    html = render_report_html(report)
    visible = visible_source(html)
    assert "报告编号" not in visible and "LEGACY-ID" not in visible
    assert "历史受检者" in visible
    assert embedded_report(html) == before == report
