import json

import pytest

from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.publication import project_public_report
from modules.gut_barrier.schemas import GutBarrierRequest


def payload(exam, *, day="2026-09-24", normal=False):
    return GutBarrierRequest.model_validate({
        "userId": "hospital-safety-test", "sessionId": "hospital-safety-session", "age": 50,
        "gutHealthIndicator": {
            "dLacticAcid": {"value": "5" if normal else "26", "unit": "mg/L"},
            "diamineOxidase": {"value": "5" if normal else "17", "unit": "U/L"},
            "bacterialEndotoxin": {"value": "5" if normal else "23", "unit": "U/L"},
            "hsCRP": {"value": "2.7", "unit": "mg/L"},
        },
        "hospitalData": [{"dataType": "EXAM", "itemName": "院内检查", "examResult": exam,
                          "checkTime": day, "reportTime": day}],
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("normal", [True, False])
async def test_hospital_obstruction_restricts_nutrition_and_lifestyle_even_when_markers_normal(normal):
    report, audit = await GutBarrierPipeline().generate(payload("肠梗阻", normal=normal), report_id="gbr_safety", evaluation_date="2026-09-24")
    assert "fiber_hold" in {s["code"] for s in report["assessment"]["context"]["hospitalSafetySignals"]}
    assert report["interventions"]["nutrition"][0]["status"] == "safety_hold"
    diet = next(row for row in report["interventions"]["lifestyle"] if row["name"] == "肠道友好的日常饮食结构")
    assert diet["status"] == "safety_hold" and diet["dose"] is None
    assert "25–35" not in json.dumps(project_public_report(report), ensure_ascii=False)
    assert audit["partial"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("exam", ["未见肠梗阻。", "所见结直肠粘膜大致正常。术后注意事项：如出现腹痛、肠梗阻请就诊。"])
async def test_hospital_negative_and_instructions_are_not_positive_safety_findings(exam):
    report, _ = await GutBarrierPipeline().generate(payload(exam), report_id="gbr_negative", evaluation_date="2026-09-24")
    assert report["assessment"]["context"]["hospitalSafetySignals"] == []
    assert report["interventions"]["nutrition"][0]["status"] == "food_first"


@pytest.mark.asyncio
@pytest.mark.parametrize("day,review", [("2026-09-24", True), ("2025-09-24", False), ("2027-09-24", False)])
async def test_only_current_acute_hospital_context_blocks_risk_assessment(day, review):
    report, _ = await GutBarrierPipeline().generate(payload("急性感染", day=day), report_id="gbr_acute", evaluation_date="2026-09-24")
    assessed = report["assessment"]
    assert "inflammation" not in assessed
    assert (assessed["context"].get("acuteInfection") is True) is review
    assert (assessed["riskRuleAssessment"]["status"] == "not_evaluated") is review
    if review:
        followup = next(row for row in report["interventions"]["followup"] if row["status"] == "priority_review")
        assert "CRP" not in followup["reason"] and ">10" not in followup["reason"]


@pytest.mark.asyncio
async def test_public_appendix_carries_actual_intervention_guidance_sources():
    report, _ = await GutBarrierPipeline().generate(payload("未见异常"), report_id="gbr_sources", evaluation_date="2026-09-24")
    public = project_public_report(report)
    refs = next(b for b in public["sections"][-1]["blocks"] if b.get("title") == "营养与微生态干预补充依据")
    citations = json.dumps(refs, ensure_ascii=False)
    for source in ("World Health Organization", "NIH Office of Dietary Supplements", "American Gastroenterological Association", "US Food and Drug Administration"):
        assert source in citations
    assert all(set(item) == {"citation", "url", "year", "publisher", "doi", "location"} for item in refs["items"])
