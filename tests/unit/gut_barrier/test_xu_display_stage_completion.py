"""The section 1.1 label update is not a new mandatory diagnostic model."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.risk_evaluator import RiskBand, RiskModelRegistry, RiskModelSpecification
from modules.gut_barrier.risk_model import build_disease_risks
from modules.gut_barrier.schemas import GutBarrierRequest
from tests.unit.gut_barrier.test_service import environment


def complete_case(*, missing=None, age=26):
    indicators = {
        "bacterialEndotoxin": {"value": "12.5", "unit": "U/L"},
        "diamineOxidase": {"value": "3.8", "unit": "U/L"},
        "dLacticAcid": {"value": "0.06", "unit": "mg/L"},
        "hsCRP": {"value": "0.2", "unit": "mg/L"},
    }
    context = {
        "recentInfection": False, "medications": [], "allergies": [],
        "immuneStatus": "正常", "liverAndKidneyFunction": "正常", "nutrientStatus": {},
    }
    if missing in indicators:
        del indicators[missing]
    elif missing in context:
        del context[missing]
    request = GutBarrierRequest.model_validate({
        "userId": "display-stage-test", "sessionId": "display-stage-session", "age": age,
        "gutHealthIndicator": indicators,
    })
    sources = SimpleNamespace(collect=AsyncMock(return_value={
        "observations": [], "sources": [], "issues": [], "context": context,
    }))
    return request, GutBarrierPipeline(sources=sources)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [None, "hsCRP", "diamineOxidase", "recentInfection"])
async def test_four_complete_markers_do_not_hide_missing_clinical_information(missing):
    request, pipeline = complete_case(missing=missing)
    report, audit = await pipeline.generate(
        request, report_id="stage-display-test", evaluation_date="2026-09-28",
    )
    assert audit["overallStage"]["status"] == "display_only"
    assert audit["overallStage"]["scope"] == "ibfc_display"
    assert audit["overallStage"]["code"] is None and audit["overallStage"]["label"] is None
    assert audit["partial"] is True
    assert audit["riskRuleAssessment"]["clinicalCompleteness"]["status"] == "incomplete"
    if missing is None:
        assert audit["missingData"] == audit["reviewItems"] == audit["issues"] == []
    # Display metadata remains internal. Existing callback keys and nullable
    # stage-card values are not replaced by an invented health diagnosis.
    public = project_callback_report(report)
    assert set(public) == {"schemaVersion", "meta", "sections"}
    card = next(
        item for section in public["sections"] for block in section["blocks"]
        if block["kind"] == "cards" for item in block["items"] if item["label"] == "总体阶段"
    )
    assert card == {"label": "总体阶段", "value": None, "detail": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [26, 101])
async def test_new_pipeline_persists_partial_callback_and_uses_new_identity(age):
    request, pipeline = complete_case(age=age)
    assert pipeline.version == "gut-barrier-pipeline-v51.20261010-fixed-catalog-publication"
    env = environment(pipeline=pipeline)
    accepted = await env.service.submit(request)
    assert await env.service.drain_generation() == 1
    saved = env.collection.documents[accepted["_id"]]
    assert saved["status"] == saved["body"]["status"] == "partial"
    assert saved["audit"]["overallStage"]["status"] == "display_only"
    frozen = deepcopy(saved["body"])
    replay = await env.service.submit(request)
    assert replay["_id"] == accepted["_id"]
    assert env.collection.documents[accepted["_id"]]["body"] == frozen
    assert await env.service.drain_generation() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("age,partial", [(26, False), (101, True)])
async def test_risk_rule_population_limit_stays_partial_without_changing_normal_scores(age, partial):
    request, pipeline = complete_case(age=age)
    report, audit = await pipeline.generate(request, report_id="risk-population", evaluation_date="2026-09-28")
    assert audit["missingData"] == audit["reviewItems"] == audit["issues"] == []
    assert audit["partial"] is True
    rules = audit["riskRuleAssessment"]
    assert rules["clinicalCompleteness"]["status"] == "incomplete"
    assert rules["status"] == ("not_evaluated" if partial else "evaluated")
    assert len(rules["items"]) == 23
    public = project_callback_report(report)
    chapter = next(section for section in public["sections"] if section["id"] == "disease_risks")
    tables = [block for block in chapter["blocks"] if block.get("columns") == ["疾病", "风险指数", "风险等级", "机制关联"]]
    rows = [row for table in tables for row in table["rows"]]
    assert len(rows) == 23
    if partial:
        assert all("adult_age_required" in item["reasons"] for item in rules["items"])
        assert all(row[1:3] == [None, None] for row in rows)
    else:
        assert rules["allocation"]["highCodes"] == []
        assert rules["allocation"]["mediumCodes"] == []
        assert [row[1:3] for row in rows if row[2] == "中"] == []
        assert sum(row[2] == "低" for row in rows) == 23
        assert sum(row[2] == "高" for row in rows) == 0
        assert all(1 <= row[1] <= 3.3 for row in rows if row[2] == "低")
        assert len({row[1] for row in rows}) == 23


@pytest.mark.asyncio
@pytest.mark.parametrize("complete_model,label", [
    (True, "low"), (False, "low"), (True, "TEST_ONLY_UNKNOWN_GRADE"),
])
async def test_audited_models_preserve_scores_without_completing_missing_clinical_information(complete_model, label):
    request, original = complete_case(age=101)
    codes = [row["code"] for row in build_disease_risks()[0]]
    covered = codes if complete_model else codes[:1]
    specification = RiskModelSpecification(
        model_id="TEST_ONLY_coverage", version="TEST_ONLY_v1",
        approval_reference="Technical fixture only; no clinical approval",
        validation_reference="Technical coverage fixture", endpoint="TEST_ONLY",
        time_horizon="TEST_ONLY", population="TEST_ONLY synthetic adults",
        disease_inputs=tuple((code, ("age",)) for code in covered),
        bands=(RiskBand(label, 0, 10, True, True),), precision=1,
        min_age=18, max_age=150, allowed_sexes=(), max_observation_age_days=30,
    )
    evaluator = SimpleNamespace(evaluate=AsyncMock(return_value={
        "modelId": specification.model_id, "modelVersion": specification.version,
        "results": [{"code": code, "riskIndex": 0.0, "riskLevel": label, "trace": {"fixture": True}}
                    for code in covered],
    }))
    registry = RiskModelRegistry()
    registry.register(specification, evaluator)
    pipeline = GutBarrierPipeline(sources=original.sources, risk_models=registry,
                                  risk_model_id=specification.model_id, risk_model_version=specification.version)
    report, audit = await pipeline.generate(request, report_id="model-coverage", evaluation_date="2026-09-28")
    assert audit["riskRuleAssessment"]["status"] == "not_evaluated"
    assert audit["riskModel"]["status"] == ("evaluated" if complete_model else "partial")
    assert audit["partial"] is True
    assert audit["riskRuleAssessment"]["clinicalCompleteness"]["status"] == "incomplete"
    evaluator.evaluate.assert_awaited_once()
    assessed_rows = {row["code"]: row for row in report["assessment"]["diseaseRisks"]}
    assert all(assessed_rows[code]["riskIndex"] == 0.0 for code in covered)


@pytest.mark.asyncio
async def test_questionnaire_cannot_fill_missing_objective_risk_domains():
    request, pipeline = complete_case()
    payload = request.model_dump(mode="json", by_alias=True)
    payload["healthSurvey"] = {
        "heightCm": "160", "weightKg": "55", "digestion": "无", "bowelMovement": "无",
        "fourHigh": "无", "pastDisease": "无", "familyDisease": "无",
        "currentMedication": "无", "specialMedication": "无", "allergy": "无",
    }
    payload["hospitalData"] = [{
        "dataType": "INSPECTION", "itemName": "低密度脂蛋白(LDL-c)",
        "checkTime": "2026-09-28", "testResultValue": "2.0", "unit": "mmol/L", "resultStatusName": "正常",
    }]
    request = GutBarrierRequest.model_validate(payload)
    _, audit = await pipeline.generate(request, report_id="complete-clinical-data", evaluation_date="2026-09-28")
    completeness = audit["riskRuleAssessment"]["clinicalCompleteness"]
    assert completeness["status"] == "incomplete"
    assert set(completeness["missingDomains"]) == {"diagnosis"}
    assert audit["partial"] is True
    items = {row["code"]: row for row in audit["riskRuleAssessment"]["items"]}
    assert audit["riskRuleAssessment"]["allocation"]["highCodes"] == []
    assert audit["riskRuleAssessment"]["allocation"]["mediumCodes"] == []
    # Cardiovascular raw scores no longer include h_pos. All 23 items stay low;
    # within-band ordering still includes the three barrier marker positions.
    assert items["atherosclerosis"]["rawScore"] == 1.0
    assert (items["atherosclerosis"]["riskIndex"], items["atherosclerosis"]["riskLevel"]) == (2.2, "低")
    assert (items["coronary_heart_disease"]["riskIndex"], items["coronary_heart_disease"]["riskLevel"]) == (2.1, "低")
    assert {code: row["riskIndex"] for code, row in items.items() if row["riskLevel"] == "高"} == {}
    assert sum(row["riskLevel"] == "低" for row in items.values()) == 23
    assert all(1 <= row["riskIndex"] <= 3.3 for row in items.values() if row["riskLevel"] == "低")
    assert len({row["riskIndex"] for row in audit["riskRuleAssessment"]["items"]}) == 23


@pytest.mark.asyncio
async def test_missing_body_measurements_do_not_alone_make_a_complete_report_partial():
    request, pipeline = complete_case()
    payload = request.model_dump(mode="json", by_alias=True)
    payload["hospitalData"] = [{
        "dataType": "INSPECTION", "itemName": "低密度脂蛋白(LDL-c)",
        "checkTime": "2026-09-28", "testResultValue": "2.0", "unit": "mmol/L", "resultStatusName": "正常",
    }, {
        "dataType": "EXAM", "itemName": "电子胃镜", "checkTime": "2026-09-28",
        "examResult": "慢性非萎缩性胃炎",
    }]
    report, audit = await pipeline.generate(
        GutBarrierRequest.model_validate(payload), report_id="complete-without-bmi", evaluation_date="2026-09-28")
    assert "bmi" not in report["assessment"]["context"]
    assert report["assessment"]["context"].get("healthSurvey") is None
    assert audit["riskRuleAssessment"]["clinicalCompleteness"]["status"] == "complete"
    assert audit["riskRuleAssessment"]["clinicalCompleteness"]["missingDomains"] == []
    assert audit["missingData"] == audit["reviewItems"] == audit["issues"] == []
    assert audit["partial"] is False
