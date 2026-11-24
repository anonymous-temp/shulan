"""The current report reads its three markers only from gutHealthIndicator.

All identities and routes are synthetic. The October input's duplicate hospital
rows deliberately have normal labels/reference ranges and no measurement unit.
"""
from copy import deepcopy
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import tornado.testing

from adapters.gut_barrier.render_context import render_context_from_headers
from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.risk_model import build_disease_risks
from modules.gut_barrier.schemas import GutBarrierRequest
from tests.unit.gut_barrier.final_input_fixture import final_input
from tests.unit.gut_barrier import test_api as _api_fixture


TODAY = "2026-10-07"
PANEL = (
    ("dLacticAcid", "D_LACTATE", "D-乳酸", "9.68", "mg/L"),
    ("diamineOxidase", "DAO", "二胺氧化酶", "8.92", "U/L"),
    ("bacterialEndotoxin", "LPS", "细菌内毒素", "19.25", "U/L"),
)


def _payload():
    payload = final_input()
    payload.update(userId="marker-authority-user", sessionId="marker-authority-session",
                   name="合成专项指标受检者", age="42", sex="男", fileList=[], testData=[])
    payload["healthSurvey"].update(name=payload["name"], age="42", gender="男",
                                  submitTime="2026/10/7 09:00:00")
    payload["gutHealthIndicator"] = {
        field: {"value": value, "unit": unit} for field, _, _, value, unit in PANEL
    }
    payload["gutHealthIndicator"]["complete"] = True
    payload["hospitalData"] = _hospital_markers() + [_gastroscopy()]
    return payload


def _hospital_markers(*, variant="missing_unit"):
    references = ("0-20", "0-10", "0-20", "0-5")
    rows = []
    for (_, _, name, value, unit), reference in zip(PANEL, references):
        row = {"checkTime": TODAY + " 08:00:00", "reportTime": TODAY + " 08:30:00",
               "dataType": "INSPECTION", "itemName": name,
               "testResultValue": value, "referenceRange": reference,
               "resultStatus": 0, "resultStatusName": "正常"}
        if variant != "missing_unit":
            row["unit"] = "unrelated-unit" if variant == "different_unit" else unit
        if variant == "different_value":
            row["testResultValue"] = "99"
        rows.append(row)
    return rows


def _gastroscopy():
    return {"checkTime": "2025-12-18 08:00:00", "reportTime": "2025-12-18 09:00:00",
            "dataType": "EXAM", "itemName": "无痛胃镜检查(胃)",
            "examResult": "慢性非萎缩性胃炎"}


def _observations(*, spoof_source=False):
    rows = []
    for field, code, _, _, unit in PANEL:
        source = ({"kind": "request", "path": "gutHealthIndicator." + field}
                  if spoof_source else {"kind": "fileList", "index": 0, "page": 1})
        rows.append({"code": code, "value": "88", "unit": unit, "date": TODAY,
                     "isCurrent": True, "source": source})
    return rows


def _assess(payload, *, observations=None, context=None):
    return assess_case(GutBarrierRequest.model_validate(payload), observations=observations,
                       context={"currentDate": TODAY, "sampleDate": TODAY, **(context or {})},
                       indicator_source="gutHealthIndicator")


def _assert_panel(assessment):
    markers = {marker["code"]: marker for marker in assessment["markers"]}
    assert set(markers) == {code for _, code, _, _, _ in PANEL}
    for field, code, _, value, unit in PANEL:
        marker = markers[code]
        assert marker["status"] == "evaluated"
        assert set(marker["reasons"]) <= {"subhealth_pattern_definition_pending"}
        assert marker["value"] == float(value) and marker["raw"] == value
        assert marker["unit"] == unit
        assert len(marker["source"]) == 1
        assert marker["source"][0]["source"] == {
            "kind": "request", "path": "gutHealthIndicator." + field}
        assert marker["laboratoryReferenceRanges"] == []
    assert assessment["barrier"]["score"] == 2.5
    assert assessment["barrier"]["grade"] == "II"
    assert "inflammation" not in assessment


@pytest.mark.parametrize("variant", ["missing_unit", "different_value", "different_unit"])
def test_same_day_hospital_duplicates_cannot_override_the_canonical_panel(variant):
    payload = _payload()
    payload["hospitalData"] = _hospital_markers(variant=variant)
    before = deepcopy(payload)
    assessment = _assess(payload)
    _assert_panel(assessment)
    assert payload == before
    records = assessment["provenance"]["observations"]
    assert len(records) == 6
    assert sum(record["source"]["kind"] == "hospitalData" for record in records) == 3


@pytest.mark.parametrize("spoof_source", [False, True])
def test_upload_observations_even_with_spoofed_canonical_paths_do_not_supply_markers(spoof_source):
    payload, observations = _payload(), _observations(spoof_source=spoof_source)
    before, observations_before = deepcopy(payload), deepcopy(observations)
    assessment = _assess(payload, observations=observations)
    _assert_panel(assessment)
    assert payload == before and observations == observations_before
    assert len(assessment["provenance"]["observations"]) == 9
    assert sum(record["value"] == "88" for record in assessment["provenance"]["observations"]) == 3


@pytest.mark.parametrize("field,code", [(row[0], row[1]) for row in PANEL])
def test_a_missing_canonical_marker_stays_missing_with_complete_external_measurements(field, code):
    payload = _payload()
    del payload["gutHealthIndicator"][field]
    payload["hospitalData"] = _hospital_markers(variant="valid")
    assessment = _assess(payload, observations=_observations(spoof_source=True))
    marker = next(row for row in assessment["markers"] if row["code"] == code)
    assert marker["status"] == "missing" and marker["value"] is None
    assert marker["source"] == [] and marker["score"] is None
    assert code in assessment["missingData"]
    assert all(row["status"] == "evaluated" for row in assessment["markers"] if row["code"] != code)
    assert assessment["barrier"]["score"] is None


@pytest.mark.parametrize("empty", ["omitted", "null", "object"])
def test_an_absent_canonical_panel_is_not_backfilled_from_hospital_or_uploads(empty):
    payload = _payload()
    if empty == "omitted":
        del payload["gutHealthIndicator"]
    else:
        payload["gutHealthIndicator"] = None if empty == "null" else {}
    payload["hospitalData"] = _hospital_markers(variant="valid")
    assessment = _assess(payload, observations=_observations())
    assert all(row["status"] == "missing" and row["value"] is None and row["source"] == []
               for row in assessment["markers"])
    assert assessment["barrier"]["score"] is None and "inflammation" not in assessment
    assert len(assessment["provenance"]["observations"]) == 6


def test_routine_crp_cannot_fill_missing_hscrp_or_change_canonical_hscrp():
    payload = _payload()
    payload["hospitalData"] = [{"dataType": "INSPECTION", "itemName": "C反应蛋白(CRP)",
                               "testResultValue": "99", "unit": "mg/L", "checkTime": TODAY}]
    _assert_panel(_assess(payload))
    assert not any("CRP" in str(row) for row in _assess(payload)["context"].get("clinicalFindings", []))


@pytest.mark.asyncio
async def test_pipeline_keeps_hospital_risk_factors_and_questionnaire_body_display():
    payload = _payload()
    payload["hospitalData"].append({"dataType": "INSPECTION", "itemName": "低密度脂蛋白(LDL-c)",
                                    "checkTime": TODAY, "testResultValue": "4.1", "unit": "mmol/L",
                                    "resultStatusName": "偏高"})
    before = deepcopy(payload)
    request = GutBarrierRequest.model_validate(payload)
    request_before = request.model_dump(mode="json", by_alias=True)
    report, _ = await GutBarrierPipeline().generate(request, report_id="auxiliary-preserved",
                                                   evaluation_date=TODAY)
    assessment = report["assessment"]
    _assert_panel(assessment)
    assert payload == before and request.model_dump(mode="json", by_alias=True) == request_before
    assert request_before["hospitalData"] == payload["hospitalData"]
    assert request_before["healthSurvey"] == payload["healthSurvey"]
    context = assessment["context"]
    assert "bmi" not in context
    assert "35.2 kg/m²" in dict(context["healthSurveyRows"])["BMI"]
    assert set(context["symptoms"]) == {"消化不良", "时常腹泻"}
    assert len(context["clinicalFindings"]) == 5
    assert any(row["examResult"] == "慢性非萎缩性胃炎" for row in context["clinicalFindings"])
    lipid = next(row for row in context["clinicalFindings"] if row["code"] == "LDL_C")
    assert lipid["value"] == "4.1" and lipid["unit"] == "mmol/L" and lipid["temporalStatus"] == "current"
    assert {"gastritis_followup", "hyperlipidemia"} <= {
        row["code"] for row in assessment["diseaseRisks"]}
    risks = {row["code"]: row for row in assessment["riskRuleAssessment"]["items"]}
    lipid_factor = next(factor for factor in risks["hyperlipidemia"]["excludedFactors"] if factor["domain"] == "lipids")
    assert lipid_factor["sourceType"] == "objective_current" and lipid_factor["contribution"] == 3
    assert not any(factor["domain"] == "lipids" for factor in risks["hyperlipidemia"]["factors"])
    assert "diarrhea_evaluation" not in {row["code"] for row in assessment["diseaseRisks"]}
    assert not any(factor["domain"] == "body" for factor in
                   risks["obesity"]["factors"] + risks["obesity"]["excludedFactors"])


@pytest.mark.asyncio
async def test_pipeline_source_context_cannot_select_external_markers():
    payload = _payload()
    observations = _observations(spoof_source=True)
    sources = SimpleNamespace(collect=AsyncMock(return_value={
        "observations": observations, "sources": [], "issues": [],
        "context": {"sampleDate": TODAY, "indicator_source": "all_sources",
                    "indicatorSource": "all_sources"},
    }))
    report, _ = await GutBarrierPipeline(sources=sources).generate(
        GutBarrierRequest.model_validate(payload), report_id="fixed-marker-source", evaluation_date=TODAY)
    _assert_panel(report["assessment"])
    assert sum(record["value"] == "88" for record in report["assessment"]["provenance"]["observations"]) == 3


class TestGutBarrierAPI(tornado.testing.AsyncHTTPTestCase):
    # Reuse only the established HTTP harness; unrelated API cases are not
    # inherited or rerun by this focused regression module.
    get_app = _api_fixture.TestGutBarrierAPI.get_app
    post = _api_fixture.TestGutBarrierAPI.post

    def tearDown(self):
        self.io_loop.run_sync(self.transport.aclose)
        super().tearDown()

    @tornado.testing.gen_test
    async def test_october_duplicate_hospital_rows_reach_consistent_http_callback_and_html(self):
        self.clock.advance(23 * 24 * 60 * 60)
        self.service.callback_url = self.get_url("/test-callback")
        self._app.settings["receiver_ready"] = True
        payload = _payload()
        before = deepcopy(payload)
        response = await self.post("/api/v1/gut-barrier/report", payload)
        assert response.code == 200
        receipt = json.loads(response.body)["data"]
        assert receipt["status"] == "processing"
        await self.supervisor.wait_idle()
        assert payload == before and len(self._app.settings["received"]) == 1
        _, callback = self._app.settings["received"][0]
        saved = self.collection.documents[receipt["finalReportId"]]
        assert saved["deliveryStatus"] == "delivered"
        assert callback["status"] in {"success", "partial"} and callback["error"] is None
        assert saved["request"]["hospitalData"] == before["hospitalData"]
        assert saved["request"]["healthSurvey"] == before["healthSurvey"]
        assessment = saved["audit"]["reportDocument"]["assessment"]
        _assert_panel(assessment)
        assert "bmi" not in assessment["context"]
        assert len(assessment["context"]["clinicalFindings"]) == 4

        catalog, _ = build_disease_risks()
        catalog_codes = {row["code"] for row in catalog}
        assert len(catalog_codes) == 23
        risks = {row["code"]: row for row in assessment["riskRuleAssessment"]["items"]}
        assert catalog_codes <= set(risks)
        for code in catalog_codes:
            assert risks[code]["status"] == "evaluated"
            assert type(risks[code]["riskIndex"]) is float and 1 <= risks[code]["riskIndex"] <= 10
            assert risks[code]["riskLevel"] in {"低", "中", "高"}

        public = callback["report"]
        blocks = [block for section in public["sections"] for block in section["blocks"]]
        markers = next(block for block in blocks if block["title"] == "1.2 检测项目与结果总览")
        assert [row[2] for row in markers["rows"]] == ["8.92 U/L", "19.25 U/L", "9.68 mg/L"]
        score_cards = next(block for block in blocks if block["title"] == "肠道屏障功能评分（IBFC）")
        assert next(item for item in score_cards["items"] if item["label"] == "IBFC评分")["value"] == 2.5
        risk_rows = {row[0]: row for block in blocks
                     if block.get("columns") == ["疾病", "风险指数", "风险等级", "机制关联"]
                     for row in block["rows"]}
        for catalog_row in catalog:
            display_name = "代谢相关脂肪性肝病" if catalog_row["code"] == "nafld" else catalog_row["name"]
            row = risk_rows[display_name]
            assert row[1:3] == [risks[catalog_row["code"]]["riskIndex"], risks[catalog_row["code"]]["riskLevel"]]

        binding = render_context_from_headers(callback, self._app.settings["received_headers"][0])
        html = render_report_html(public, template_version=binding["templateVersion"])
        embedded = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S)
        assert embedded and json.loads(embedded.group(1)) == public
        assert "2.5" in html and "8.92 U/L" in html and "19.25 U/L" in html and "9.68 mg/L" in html
        assert "html" not in callback and "html" not in saved
