"""CRP is accepted solely for wire compatibility, never as a scoring input."""
from copy import deepcopy
import json

import pytest

from adapters.gut_barrier.sources import GutBarrierSources
from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.risk_index_rules import apply_risk_index_rules, confirmed_rule_results
from modules.gut_barrier.rules import is_retired_marker_name
from modules.gut_barrier.schemas import GutBarrierRequest
from tests.unit.gut_barrier.test_risk_index_rules import _payload


@pytest.mark.parametrize("markers", [(0, 0, 0), (15, 1, 1), (0, 11, 0), (17, 0, 35), (30, 20, 35)])
@pytest.mark.parametrize("retired", [None, 0, .5, 3, 10, 99, ">10", ">=3", "<0.5", "invalid", True,
                                      [], {}, {"unexpected": [1]}, {"value": -99, "unit": "unsupported"}])
def test_legacy_crp_variants_leave_entire_assessment_and_scores_identical(markers, retired):
    payload = _payload(values=markers)
    original = deepcopy(payload)
    baseline = apply_risk_index_rules(assess_case(GutBarrierRequest.model_validate(payload)))
    payload["gutHealthIndicator"]["hsCRP"] = retired
    req = GutBarrierRequest.model_validate(payload)
    actual = apply_risk_index_rules(assess_case(req))
    assert actual == baseline
    assert len(confirmed_rule_results(actual)) == 23
    assert "hsCRP" not in req.model_dump(mode="json", by_alias=True)["gutHealthIndicator"]
    assert "inflammation" not in actual
    assert set(x["code"] for x in actual["markers"]) == {"D_LACTATE", "DAO", "LPS"}
    assert actual["riskRuleAssessment"]["panelCoverage"] == {"available": 3, "required": 3}
    assert payload == {**original, "gutHealthIndicator": {**original["gutHealthIndicator"], "hsCRP": retired}}
    if markers == (0, 0, 0):
        assert {row["riskLevel"] for row in actual["riskRuleAssessment"]["items"]} == {"低"}


@pytest.mark.parametrize("name", ["HS_CRP", "hs-CRP", "CRP", "C反应蛋白(CRP)", "常规CRP", "超敏C反应蛋白水平",
                                 "超敏C反應蛋白", "CRP/白蛋白比值", "高敏C-反应蛋白", "high-sensitivity C-reactive protein"])
def test_retired_hospital_aliases_and_derived_tests_never_enter_clinical_findings(name):
    payload = _payload(values=(17, 0, 35))
    payload["hospitalData"] = [{"itemName": name, "dataType": "INSPECTION", "testResultValue": "99",
                                "unit": "mg/L", "checkTime": "2026-10-08", "resultStatusName": "偏高"}]
    assessed = assess_case(GutBarrierRequest.model_validate(payload))
    assert is_retired_marker_name(name)
    assert assessed["context"].get("clinicalFindings", []) == []
    assert not any(row["code"] == "HS_CRP" for row in assessed["provenance"]["observations"])


@pytest.mark.parametrize("name", ["肌酐", "血清脂蛋白", "ALB", "C肽", "hs-cTnT"])
def test_other_laboratory_names_are_not_retired(name):
    assert not is_retired_marker_name(name)


@pytest.mark.parametrize("data", [
    b'hs-CRP: invalid mg/L',
    b'CRP 99 mg/L',
    json.dumps({"rows": [{"itemName": "常规CRP", "value": {"bad": True}}]}).encode(),
])
def test_crp_only_attachments_are_recognized_as_ignored_not_parse_failures(data):
    parsed = GutBarrierSources._structured_or_text(data, summary={"sourceId": "retired-lab"})
    assert parsed["observations"] == parsed["issues"] == []
    assert parsed["summary"]["status"] == "parsed"


def test_ignoring_crp_attachment_rows_does_not_mask_real_barrier_errors():
    data = json.dumps([{"code": "HS_CRP", "value": True}, {"code": "DAO", "value": True}]).encode()
    parsed = GutBarrierSources._structured_or_text(data, summary={"sourceId": "mixed"})
    assert [issue["code"] for issue in parsed["issues"]] == ["AMBIGUOUS_MARKER_ROW"]
    assert parsed["issues"][0]["marker"] == "DAO"


@pytest.mark.asyncio
@pytest.mark.parametrize("retired", [None, ">10", {"bad": True}])
async def test_pipeline_status_public_output_and_partial_reasons_ignore_crp(retired):
    baseline_payload = _payload(values=(17, 0, 35))
    payload = deepcopy(baseline_payload)
    payload["gutHealthIndicator"]["hsCRP"] = retired
    pipeline = GutBarrierPipeline()
    before, before_audit = await pipeline.generate(GutBarrierRequest.model_validate(baseline_payload), report_id="no-crp", evaluation_date="2026-10-08")
    after, after_audit = await pipeline.generate(GutBarrierRequest.model_validate(payload), report_id="no-crp", evaluation_date="2026-10-08")
    assert before["assessment"] == after["assessment"]
    assert before_audit["partial"] == after_audit["partial"]
    assert before_audit["missingData"] == after_audit["missingData"]


@pytest.mark.parametrize("text", ["超敏C反應蛋白偏高", "high sensitivity C reactive protein elevated",
                                 "hs CRP 99mg/L", "常规CRP异常", "CRP/白蛋白比值高"])
def test_retired_questionnaire_synonyms_do_not_supply_risk_factors(text):
    baseline = apply_risk_index_rules(assess_case(GutBarrierRequest.model_validate(_payload())))
    assessed = assess_case(GutBarrierRequest.model_validate(_payload(survey={"digestion": text})))
    actual = apply_risk_index_rules(assessed)
    assert actual["riskRuleAssessment"]["items"] == baseline["riskRuleAssessment"]["items"]


@pytest.mark.parametrize("name", ["超敏C反應蛋白", "high sensitivity C reactive protein", "常规CRP",
                                 "超敏C反应蛋白水平", "CRP/白蛋白比值", "highSensitivityCRP", "highSensitivityCReactiveProtein", "Ｃ反应蛋白"])
def test_ignored_text_attachment_aliases_do_not_force_partial_status(name):
    parsed = GutBarrierSources._structured_or_text((name + ': 99 mg/L').encode(), summary={"sourceId": "retired-only"})
    assert parsed["observations"] == parsed["issues"] == []


@pytest.mark.parametrize("name", ["highSensitivityCRP", "highSensitivityCReactiveProtein", "Ｃ反应蛋白", "ＣＲＰ"])
def test_existing_aliases_share_retirement_across_risk_inputs_and_interpretation(name):
    from modules.report.gut_barrier_context import strip_crp_text
    assert is_retired_marker_name(name)
    assessed = assess_case(GutBarrierRequest.model_validate(_payload(survey={"digestion": name + "偏高"})))
    assert all(item["components"]["individualFactors"] == 0
               for item in apply_risk_index_rules(assessed)["riskRuleAssessment"]["items"])
    assert strip_crp_text(name + "偏高，DAO指标异常。") == "DAO指标异常。"
