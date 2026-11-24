"""Technical fixtures only: these evaluators have NO clinical validity.

The two-point golden outputs (0 and 10) are deliberately literal test data,
not a made-up clinical formula or the supplied report's example scores.
"""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.publication import project_public_report
from modules.gut_barrier.risk_evaluator import (
    RiskBand, RiskModelRegistry, RiskModelSpecification, evaluate_registered_risks,
)
from modules.gut_barrier.risk_model import build_disease_risks, mechanism_evidence
from modules.gut_barrier.schemas import GutBarrierRequest


MODEL_ID = "TEST_ONLY_NO_CLINICAL_VALIDITY"
MODEL_VERSION = "test-fixture-1"
EVALUATION_DATE = "2026-09-24"
ALL_CODES = tuple(row["code"] for row in build_disease_risks()[0])


def test_mechanisms_restore_specific_paths_with_primary_sources_without_patient_diagnoses():
    rows = {row["code"]: row for row in build_disease_risks()[0]}
    # Independent knowledge-point acceptance, not snapshots imported from the
    # same prose catalog being tested.
    expected = {
        "nafld": "门静脉", "ibs": "肥大细胞", "ibd": "黏液层", "cirrhosis": "TGF-β",
        "intestinal_barrier_followup": "屏障",
        "type2_diabetes": "TNF-α", "obesity": "脂肪组织免疫细胞", "hyperlipidemia": "富含甘油三酯脂蛋白",
        "hyperuricemia": "ABCG2", "atherosclerosis": "TLR4/MyD88", "coronary_heart_disease": "脂质负荷",
        "hypertension": "血管紧张素Ⅱ", "stroke": "缺血性卒中", "alzheimers": "淀粉样",
        "parkinsons": "α-突触核蛋白", "depression": "FABP2", "anxiety": "无菌小鼠",
        "rheumatoid_arthritis": "ZO-1", "systemic_lupus": "遗传易感", "chronic_kidney_disease": "紧密连接蛋白",
        "colorectal_cancer": "IL-23/IL-17", "liver_cancer": "TLR4", "lung_cancer": "探索性试验",
    }
    assert set(rows) == set(expected)
    for code, phrase in expected.items():
        assert phrase in rows[code]["mechanism"]
        assert "本次已确诊" not in rows[code]["mechanism"]
        assert rows[code]["riskIndex"] is None
    ledger = mechanism_evidence()
    assert {item["code"] for item in ledger} == set(rows)
    for item in ledger:
        assert item["status"] == "mechanism_only_not_prediction_model"
        for study in item["studies"]:
            assert study["url"].startswith("https://") and study["title"]
            assert study["population"] and study["scope"]
            assert set(study) == {"id", "url", "population", "scope", "title", "year", "publisher", "doi"}
    ledger[0]["studies"][0]["title"] = "mutated"
    assert mechanism_evidence()[0]["studies"][0]["title"] != "mutated"


def request(**extra):
    return GutBarrierRequest.model_validate({
        "userId": "test", "sessionId": "test", "age": 50, "sex": "男",
        "gutHealthIndicator": {
            "dLacticAcid": {"value": 26, "unit": "mg/L"},
            "diamineOxidase": {"value": 17, "unit": "U/L"},
            "bacterialEndotoxin": {"value": 23, "unit": "U/L"},
            "hsCRP": {"value": 2.7, "unit": "mg/L"},
        }, **extra,
    })


def assessment(**extra):
    return assess_case(request(**extra), context={"currentDate": EVALUATION_DATE})


def specification(codes=ALL_CODES, **overrides):
    spec = RiskModelSpecification(
        model_id=MODEL_ID, version=MODEL_VERSION,
        approval_reference="TEST ONLY technical fixture; not a medical approval",
        validation_reference="TEST ONLY independently specified zero/ten expectations",
        endpoint="TEST endpoint", time_horizon="TEST horizon", population="TEST adults",
        disease_inputs=tuple((code, ("D_LACTATE", "DAO", "LPS", "IBFC")) for code in codes),
        bands=(RiskBand("TEST_LOW", 0, 5, True, True), RiskBand("TEST_HIGH", 5, 10, False, True)),
        precision=1, min_age=18, max_age=90, allowed_sexes=("男", "女"),
        max_observation_age_days=30, allow_undated_observations=True,
    )
    return replace(spec, **overrides)


def evaluator(results=None, **overrides):
    body = {"modelId": MODEL_ID, "modelVersion": MODEL_VERSION,
            "results": results if results is not None else [
                {"code": code, "riskIndex": 0 if code == "nafld" else 10,
                 "riskLevel": "TEST_LOW" if code == "nafld" else "TEST_HIGH",
                 "trace": {"fixture": "literal fixture; never use clinically"}}
                for code in ALL_CODES], **overrides}
    return type("TestOnlyEvaluator", (), {"evaluate": AsyncMock(return_value=body)})()


async def evaluate(model=None, spec=None, assessed=None):
    model = model or evaluator()
    registry = RiskModelRegistry()
    registry.register(spec or specification(), model)
    rows, audit = await evaluate_registered_risks(assessed or assessment(), registry=registry,
        model_id=MODEL_ID, version=MODEL_VERSION, evaluation_date=EVALUATION_DATE)
    return rows, audit, model


def test_registry_requires_reviewed_metadata_and_immutable_version():
    registry = RiskModelRegistry()
    with pytest.raises(ValueError, match="approval_reference"):
        registry.register(specification(approval_reference=""), evaluator())
    with pytest.raises(ValueError, match="validation_reference"):
        registry.register(specification(validation_reference=""), evaluator())
    registry.register(specification(), evaluator())
    with pytest.raises(ValueError, match="cannot be replaced"):
        registry.register(specification(), evaluator())
    with pytest.raises(TypeError, match="async evaluate"):
        RiskModelRegistry().register(specification(), type("Blocking", (), {"evaluate": lambda self, inputs: {}})())


@pytest.mark.parametrize("overrides", [
    {"bands": (RiskBand("L", 0, 5, True, False), RiskBand("H", 5, 10, False, True))},
    {"bands": (RiskBand("L", 0, 5, True, True), RiskBand("H", 5, 10, True, True))},
    {"bands": (RiskBand("L", 0, 9, True, True),)},
    {"bands": (RiskBand("L", False, 10, True, True),)},
    {"precision": True}, {"min_age": False}, {"allow_undated_observations": "approved"},
    {"disease_inputs": (("nafld", ("riskIndex",)),)},
    {"disease_inputs": (("no_such_disease", ("DAO",)),)},
])
def test_registry_rejects_incomplete_or_inconsistent_metadata(overrides):
    with pytest.raises(ValueError):
        RiskModelRegistry().register(specification(**overrides), evaluator())


@pytest.mark.asyncio
async def test_unconfigured_and_unregistered_versions_fail_closed():
    baseline = build_disease_risks()
    result = await evaluate_registered_risks(assessment(), registry=None, model_id=None, version=None,
                                             evaluation_date=EVALUATION_DATE)
    assert result == baseline
    registry = RiskModelRegistry()
    model = evaluator()
    registry.register(specification(), model)
    rows, audit = await evaluate_registered_risks(assessment(), registry=registry,
        model_id=MODEL_ID, version="unregistered", evaluation_date=EVALUATION_DATE)
    assert audit["status"] == "not_configured"
    assert all(row["riskIndex"] is None for row in rows)
    model.evaluate.assert_not_called()


@pytest.mark.asyncio
async def test_literal_test_only_golden_outputs_and_full_traceability():
    before = assessment()
    original = deepcopy(before)
    rows, audit, model = await evaluate(assessed=before)
    assert before == original
    by_code = {row["code"]: row for row in rows}
    assert by_code["nafld"]["riskIndex"] == 0.0 and by_code["nafld"]["riskLevel"] == "TEST_LOW"
    assert by_code["ibs"]["riskIndex"] == 10.0 and by_code["ibs"]["riskLevel"] == "TEST_HIGH"
    assert len(rows) == 23 and all(row["probability"] is None for row in rows)
    assert audit["status"] == "evaluated"
    assert audit["modelId"] == MODEL_ID and audit["modelVersion"] == MODEL_VERSION
    assert audit["inputSources"]["D_LACTATE"]["observations"][0] == {
        "date": None, "source": {"kind": "request", "path": "gutHealthIndicator.dLacticAcid"}}
    assert audit["inputValues"]["IBFC"] == 9.5
    assert audit["items"][0]["trace"]["fixture"].startswith("literal fixture")
    model.evaluate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("subject,reason,status", [
    ({"age": None}, "age_required_for_model_applicability", "missing_inputs"),
    ({"age": 5}, "age_outside_model_population", "not_applicable"),
    ({"age": 95}, "age_outside_model_population", "not_applicable"),
    ({"sex": "未知"}, "sex_outside_model_population", "not_applicable"),
])
async def test_applicability_is_checked_before_model_invocation(subject, reason, status):
    rows, audit, model = await evaluate(assessed=assessment(**subject))
    assert audit["status"] == status
    assert reason in audit["reasons"]
    assert all(row["riskIndex"] is row["riskLevel"] is None for row in rows)
    model.evaluate.assert_not_called()


@pytest.mark.asyncio
async def test_acute_context_is_not_promoted_to_valid_chronic_model_input():
    assessed = assessment()
    assessed["context"]["acuteInfection"] = True
    rows, audit, model = await evaluate(assessed=assessed)
    assert audit["status"] == "not_applicable"
    assert "acute_inflammatory_context_excluded" in audit["reasons"]
    model.evaluate.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, 0, 1, 3, 25, ">3.0", ">20"])
async def test_crp_values_do_not_change_registered_model_input_or_applicability(value):
    incoming = request().model_dump(by_alias=True)
    incoming["gutHealthIndicator"]["hsCRP"] = {"value": value, "unit": "mg/L"}
    assessed = assess_case(GutBarrierRequest.model_validate(incoming), context={"currentDate": EVALUATION_DATE})
    rows, audit, model = await evaluate(assessed=assessed)
    assert audit["status"] == "evaluated"
    model.evaluate.assert_awaited_once()
    assert "HS_CRP" not in model.evaluate.call_args.args[0]["values"]
    assert all(row["status"] == "evaluated" and row["riskIndex"] in {0, 10} for row in rows)


def test_registered_model_cannot_require_retired_crp_input():
    registry = RiskModelRegistry()
    spec = specification(codes=("nafld",), disease_inputs=(("nafld", ("HS_CRP",)),))
    with pytest.raises(ValueError, match="input not implemented"):
        registry.register(spec, evaluator())


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [
    ({"value": None, "status": "missing"}, "missing_or_unresolved_measurement"),
    ({"value": None, "operator": ">", "status": "needs_review"}, "missing_or_unresolved_measurement"),
    ({"value": True}, "missing_or_unresolved_measurement"),
    ({"unit": "mmol/L"}, "unexpected_measurement_unit"),
    ({"source": []}, "missing_current_source"),
])
async def test_missing_unresolved_or_invalid_marker_is_not_zero(change, reason):
    assessed = assessment()
    assessed["markers"][2].update(change)
    rows, audit, model = await evaluate(assessed=assessed)
    assert audit["status"] == "missing_inputs"
    assert f"LPS:{reason}" in audit["reasons"]
    assert all(row["riskIndex"] is None for row in rows)
    model.evaluate.assert_not_called()


@pytest.mark.asyncio
async def test_per_disease_input_requirements_preserve_partial_results():
    assessed = assessment()
    assessed["markers"][2].update(status="missing", value=None)
    spec = specification(codes=("nafld", "ibs"), disease_inputs=(("nafld", ("DAO",)), ("ibs", ("LPS",))))
    rows, audit, model = await evaluate(assessed=assessed, spec=spec, model=evaluator(results=[
        {"code": "nafld", "riskIndex": 0, "riskLevel": "TEST_LOW"}]))
    by_code = {row["code"]: row for row in rows}
    assert by_code["nafld"]["riskIndex"] == 0 and by_code["ibs"]["riskIndex"] is None
    assert by_code["ibs"]["status"] == "missing_inputs" and by_code["ibd"]["status"] == "not_supported"
    assert audit["status"] == "partial"
    assert model.evaluate.await_args.kwargs["disease_codes"] == ("nafld",)


@pytest.mark.asyncio
async def test_observation_date_requirement_and_freshness_are_explicit():
    rows, audit, model = await evaluate(spec=specification(allow_undated_observations=False))
    assert "DAO:observation_date_required" in audit["reasons"]
    model.evaluate.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("sample_date", ["2020-01-01", "2020/01/01", "2020年1月1日", "2026-09-25", "2026/9/25", "2026年9月25日"])
async def test_real_assessment_old_or_future_known_dates_cannot_become_allowed_undated(sample_date):
    assessed = assess_case(request(), context={"currentDate": EVALUATION_DATE, "sampleDate": sample_date})
    assert all(marker["status"] == "evaluated" for marker in assessed["markers"])
    rows, audit, model = await evaluate(assessed=assessed)
    assert audit["status"] == "missing_inputs"
    assert "DAO:observation_outside_model_time_window" in audit["reasons"]
    assert all(row["riskIndex"] is None for row in rows)
    model.evaluate.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("sample_date", ["2026-09-24", "2026/09/24", "2026年9月24日", "2026-09-24T08:00:00"])
async def test_supported_date_forms_are_normalized_in_input_audit(sample_date):
    assessed = assess_case(request(), context={"currentDate": EVALUATION_DATE, "sampleDate": sample_date})
    rows, audit, model = await evaluate(assessed=assessed, spec=specification(allow_undated_observations=False))
    assert audit["status"] == "evaluated"
    assert audit["inputSources"]["DAO"]["observations"][0]["date"] == EVALUATION_DATE
    model.evaluate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["not a date", "2026/99/01", "2026年2月30日", True, 0])
async def test_nonempty_invalid_dates_never_use_allow_undated_policy(invalid):
    assessed = assessment()
    assessed["markers"][0]["source"][0]["date"] = invalid
    rows, audit, model = await evaluate(assessed=assessed)
    assert "D_LACTATE:invalid_observation_date" in audit["reasons"]
    model.evaluate.assert_not_called()
    assessed = assessment()
    assessed["context"]["sampleDate"] = invalid
    rows, audit, model = await evaluate(assessed=assessed)
    assert "DAO:invalid_evaluation_or_sample_date" in audit["reasons"]
    model.evaluate.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [True, False, -0.1, 10.1, float("nan"), float("inf"), "1.0", None])
async def test_illegal_one_item_score_does_not_poison_other_items(invalid):
    results = [{"code": "nafld", "riskIndex": invalid, "riskLevel": "TEST_LOW"},
               {"code": "ibs", "riskIndex": 10, "riskLevel": "TEST_HIGH"}]
    rows, audit, _ = await evaluate(model=evaluator(results=results))
    by_code = {row["code"]: row for row in rows}
    assert by_code["nafld"]["riskIndex"] is None and by_code["nafld"]["status"] == "failed"
    assert by_code["ibs"]["riskIndex"] == 10 and by_code["ibs"]["status"] == "evaluated"
    assert by_code["ibd"]["riskIndex"] is None and by_code["ibd"]["reasons"] == ["model_result_missing"]
    assert audit["status"] == "partial"


@pytest.mark.asyncio
@pytest.mark.parametrize("item,reason", [
    ({"riskIndex": 1.234, "riskLevel": "TEST_LOW"}, "risk_index_precision_mismatch"),
    ({"riskIndex": 2, "riskLevel": "TEST_HIGH"}, "risk_level_mismatch"),
    ({"riskIndex": 0, "riskLevel": "TEST_LOW", "probability": 0.4}, "probability_not_supported_by_relative_index_model"),
    ({"riskIndex": 0, "riskLevel": "TEST_LOW", "trace": float("nan")}, "invalid_model_trace"),
    ({"riskIndex": None, "status": "failed"}, "model_item_not_evaluated"),
])
async def test_invalid_metadata_level_precision_and_trace_fail_only_that_disease(item, reason):
    rows, audit, _ = await evaluate(model=evaluator(results=[{"code": "nafld", **item}]))
    row = next(row for row in rows if row["code"] == "nafld")
    assert row["riskIndex"] is row["riskLevel"] is None
    assert row["reasons"] == [reason]


@pytest.mark.asyncio
async def test_response_version_duplicate_and_exception_fail_closed():
    rows, audit, _ = await evaluate(model=evaluator(modelVersion="other"))
    assert all(row["riskIndex"] is None for row in rows)
    assert "model_response_version_mismatch" in audit["reasons"]
    duplicate = {"code": "nafld", "riskIndex": 0, "riskLevel": "TEST_LOW"}
    rows, audit, _ = await evaluate(model=evaluator(results=[duplicate, duplicate]))
    assert next(row for row in rows if row["code"] == "nafld")["reasons"] == ["duplicate_model_result"]
    failed = evaluator()
    failed.evaluate.side_effect = RuntimeError("not exposed to patient")
    rows, audit, _ = await evaluate(model=failed)
    assert audit["status"] == "failed" and all(row["riskIndex"] is None for row in rows)
    assert "evaluator_error:RuntimeError" in audit["reasons"]


@pytest.mark.asyncio
async def test_request_extras_ngs_and_approval_cannot_select_model_or_inject_scores():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        request(riskIndex=9, clinicianApproved=True,
                riskModel={"modelId": MODEL_ID, "modelVersion": MODEL_VERSION},
                diseaseRisks=[{"code": "nafld", "riskIndex": 8.5}])
    incoming = request(testData=[{"sourceModelIndices": [{"value": 95, "name": "冠心病"}]}])
    report, audit = await GutBarrierPipeline().generate(incoming, report_id="test", evaluation_date=EVALUATION_DATE)
    assert audit["partial"] is True and audit["riskModel"]["status"] == "not_configured"
    section = next(s for s in project_public_report(report)["sections"] if s["id"] == "disease_risks")
    tables = [b for b in section["blocks"] if b["kind"] == "table" and b["columns"][0] == "疾病"]
    assert sum(len(table["rows"]) for table in tables) == 1
    assert {row["code"] for row in report["assessment"]["diseaseRisks"]} == {
        "intestinal_barrier_followup"}
    assert all(row[1] not in {9, 8.5, 95} for table in tables for row in table["rows"])
    model, registry = evaluator(), RiskModelRegistry()
    registry.register(specification(), model)
    report, audit = await GutBarrierPipeline(risk_models=registry, risk_model_id=MODEL_ID, risk_model_version=MODEL_VERSION).generate(
        incoming, report_id="test", evaluation_date=EVALUATION_DATE)
    inputs = model.evaluate.await_args.args[0]
    assert set(inputs) == {"evaluationDate", "values", "sources"}
    assert set(inputs["values"]) == {"age", "sex", "D_LACTATE", "DAO", "LPS", "IBFC"}
    assert inputs["values"]["LPS"] == 23
    assert audit["riskModel"]["status"] == "evaluated"
    # Other required clinical information remains missing: configuring a risk
    # evaluator is not permission to mark the whole medical report complete.
    assert audit["partial"] is True
    public = project_public_report(report)
    section = next(s for s in public["sections"] if s["id"] == "disease_risks")
    tables = [b for b in section["blocks"] if b["kind"] == "table" and b["columns"][0] == "疾病"]
    model_row = next(row for table in tables for row in table["rows"] if "脂肪性肝病" in row[0])
    assert model_row[1:3] == [0.0, "TEST_LOW"]
    assert "riskModel" not in public


def test_current_quota_results_must_be_complete_even_when_a_model_covers_missing_rows():
    from modules.gut_barrier.pipeline import _incomplete_risk_results
    from modules.gut_barrier.risk_index_rules import VERSION
    rows = [{"code": code, "status": "evaluated", "riskIndex": 5.0, "riskLevel": "中", "reasons": []}
            for code in ALL_CODES]
    assessed = {
        "riskRuleAssessment": {"version": VERSION, "items": deepcopy(rows)},
        "riskModel": {"status": "evaluated", "modelId": MODEL_ID, "modelVersion": MODEL_VERSION,
                      "items": deepcopy(rows)},
        "diseaseRisks": rows,
    }
    assert _incomplete_risk_results(assessed) is False
    assessed["riskRuleAssessment"]["items"][0]["status"] = "not_evaluated"
    assert _incomplete_risk_results(assessed) is True
    assessed["riskRuleAssessment"]["items"] = deepcopy(rows[1:])
    assert _incomplete_risk_results(assessed) is True
