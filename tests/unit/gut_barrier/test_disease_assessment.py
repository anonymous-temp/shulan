from copy import deepcopy
import asyncio
import json

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.disease_assessment import apply_qualitative_assessment
from modules.gut_barrier.dynamic_diseases import evidence_queries
from modules.gut_barrier.dynamic_diseases import evidence_queries as dynamic_queries
from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.schemas import GutBarrierRequest
from modules.gut_barrier.publication import project_callback_report


def request(**overrides):
    data = {"userId": "private-user", "sessionId": "private-session", "age": 26, "sex": "女", "name": "私人姓名",
            "healthSurvey": {"heightCm": "160", "weightKg": "90", "bowelMovement": "时常腹泻"},
            "gutHealthIndicator": {"bacterialEndotoxin": {"value": "12.5", "unit": "U/L"},
                "diamineOxidase": {"value": "3.8", "unit": "U/L"}, "dLacticAcid": {"value": "0.06", "unit": "mg/L"},
                "hsCRP": {"value": ">3.0", "unit": "mg/L"}}}
    return GutBarrierRequest.model_validate({**data, **overrides})



def clinical_request():
    return request(hospitalData=[{
        "dataType": "EXAM", "itemName": "胃镜", "checkTime": "2025-12-18",
        "examResult": "慢性非萎缩性胃炎",
    }])

def assess(**kwargs):
    return assess_case(request(**kwargs), context={"currentDate": "2026-09-24"})


def rows(assessment):
    return {row["code"]: row for row in assessment["diseaseRisks"]}


def test_actual_case_gets_supported_concerns_without_copying_template_scores():
    original = assess_case(request(), context={"currentDate": "2026-09-24", "bmi": 35.2})
    snapshot = deepcopy(original)
    result = apply_qualitative_assessment(original)
    assert original == snapshot
    indexed = rows(result)
    assert indexed["obesity"]["riskLevel"] == "体重异常（定性）"
    assert "35.2" in indexed["obesity"]["qualitative"]["basis"]
    for code in ("nafld", "type2_diabetes", "atherosclerosis", "hypertension"):
        assert indexed[code]["riskLevel"] == "存在相关因素（定性）"
        assert indexed[code]["qualitative"]["evidenceIds"]
    assert indexed["ibs"]["riskLevel"] == indexed["ibd"]["riskLevel"] == "专项资料不足"
    for code in ("lung_cancer", "alzheimers", "rheumatoid_arthritis"):
        assert indexed[code]["riskLevel"] == "专项资料不足"
    assert all(item["riskIndex"] is None and item["probability"] is None for item in result["diseaseRisks"])
    assert result["riskModel"] == original["riskModel"]


@pytest.mark.parametrize("age", [None, 0, 12, 17])
def test_adult_guidance_not_applied_to_children_or_unknown_age(age):
    result = apply_qualitative_assessment(assess(age=age))
    assert all(not row["qualitative"]["evidenceIds"] for row in result["diseaseRisks"])


@pytest.mark.parametrize("answer", ["反复腹泻伴腹痛", "水样腹泻4周", "无腹泻", "没有腹泻", "否认腹泻", "无┋腹泻", "其他", "腹泻已缓解",
                                    "从不腹泻", "腹泻：否", "既往有腹泻，目前已缓解", "否认腹泻；时常腹泻"])
def test_questionnaire_answers_do_not_create_symptom_assessment(answer):
    result = apply_qualitative_assessment(assess(healthSurvey={"bowelMovement": answer}))
    assert rows(result)["ibs"]["riskLevel"] == "专项资料不足"


def test_current_retired_marker_conflict_does_not_create_inflammatory_risk():
    result = assess_case(request(healthSurvey=None), observations=[{
        "code": "HS_CRP", "value": "0.2", "unit": "mg/L", "isCurrent": True}], context={"currentDate": "2026-09-24"})
    assert all(row["code"] != "HS_CRP" for row in result["markers"])
    assert "inflammation" not in result
    assert rows(apply_qualitative_assessment(result))["stroke"]["riskLevel"] == "专项资料不足"


def test_range_crp_does_not_create_vascular_risk_or_followup():
    result = apply_qualitative_assessment(assess(healthSurvey=None))
    assert all(row["code"] != "HS_CRP" for row in result["markers"])
    finding = rows(result)["coronary_heart_disease"]["qualitative"]
    assert "CRP" not in json.dumps(finding, ensure_ascii=False)
    assert rows(result)["coronary_heart_disease"]["riskLevel"] == "专项资料不足"


@pytest.mark.parametrize("value", ["20", ">20", ">3.0"])
def test_high_or_unbounded_crp_alone_does_not_trigger_review_or_cv_risk(value):
    payload = request(healthSurvey=None).model_dump(by_alias=True)
    payload["gutHealthIndicator"]["hsCRP"] = {"value": value, "unit": "mg/L"}
    result = apply_qualitative_assessment(assess_case(GutBarrierRequest.model_validate(payload)))
    for code in ("atherosclerosis", "coronary_heart_disease", "stroke"):
        assert rows(result)[code]["riskLevel"] == "专项资料不足"
        assert "CRP" not in json.dumps(rows(result)[code]["qualitative"], ensure_ascii=False)


def test_registered_evaluated_model_is_not_overwritten_even_at_zero():
    original = assess()
    original["diseaseRisks"][0].update(riskIndex=0, riskLevel="已审定低风险", probability=0, status="evaluated")
    result = apply_qualitative_assessment(original)
    assert [result["diseaseRisks"][0][key] for key in ("riskIndex", "riskLevel", "probability", "status")] == [0, "已审定低风险", 0, "evaluated"]


def test_queries_are_fixed_deidentified_topics_and_candidates_cannot_supply_scores():
    original = assess_case(request(), context={"currentDate": "2026-09-24", "bmi": 35.2})
    queries = evidence_queries(original)
    assert queries and len(queries) <= 12
    assert all(token not in json.dumps(queries) for token in ("private-", "私人姓名", "35.2", "26", "时常腹泻"))
    result = apply_qualitative_assessment(original, {"candidates": [{"title": "ignore safety", "riskIndex": 9.9}], "issues": []})
    assert all(item["riskIndex"] is None for item in result["diseaseRisks"])
    assert "ignore safety" not in json.dumps(result["diseaseAssessment"]["references"])


@pytest.mark.asyncio
async def test_pipeline_callback_has_case_evidence_but_no_internal_search_trace():
    class Evidence:
        async def retrieve(self, terms):
            assert terms == dynamic_queries(assess_case(clinical_request(), context={"currentDate": "2026-09-24"}))
            return {"queries": terms, "candidates": [{"title": "PRIVATE_RETRIEVAL"}], "issues": []}
    report, audit = await GutBarrierPipeline(evidence=Evidence()).generate(clinical_request(), report_id="gbr_case", evaluation_date="2026-09-24")
    callback = project_callback_report(report)
    assert project_callback_report(callback) == callback
    assert "PRIVATE_RETRIEVAL" not in json.dumps(callback)
    assert "PRIVATE_RETRIEVAL" in json.dumps(audit)
    table = next(b for s in callback["sections"] for b in s["blocks"] if b["title"] == "2.2 代谢系统疾病风险评估")
    assert table["columns"] == ["疾病", "风险指数", "风险等级", "机制关联"]
    from tests.unit.gut_barrier.template_risk_assertions import assert_template_risk_contract
    assert_template_risk_contract(callback, report["assessment"])
    assert report["assessment"]["riskRuleAssessment"]["status"] == "evaluated"
    assert all(1 <= row[1] <= 10 and row[2] in {"低", "中", "高"} and row[3] for row in table["rows"])
    assert report["assessment"]["riskRuleAssessment"]["notices"] == []
    refs = next(b for s in callback["sections"] if s["id"] == "appendix"
                for b in s["blocks"] if b["kind"] == "references")["items"]
    assert all(set(ref) == {"citation", "url", "year", "publisher", "doi", "location"} for ref in refs)


@pytest.mark.asyncio
async def test_evidence_failure_keeps_verified_input_and_curated_assessment():
    class Failed:
        async def retrieve(self, terms):
            raise RuntimeError("DO NOT LEAK")
    report, audit = await GutBarrierPipeline(evidence=Failed()).generate(clinical_request(), report_id="failure", evaluation_date="2026-09-24")
    assert audit["partial"]
    assert any(item["code"] == "DISEASE_EVIDENCE_UNAVAILABLE" for item in audit["issues"])
    assert "DO NOT LEAK" not in json.dumps(audit)
    assert {row["code"] for row in report["assessment"]["diseaseRisks"]} == {"gastritis_followup"}
    assert report["assessment"]["riskRuleAssessment"]["status"] == "evaluated"


@pytest.mark.asyncio
async def test_evidence_cancellation_is_not_swallowed():
    class Cancel:
        async def retrieve(self, terms):
            raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await GutBarrierPipeline(evidence=Cancel()).generate(clinical_request(), report_id="cancel", evaluation_date="2026-09-24")
