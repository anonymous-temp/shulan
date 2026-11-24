"""Regression gates for neutral defaults and per-result persistence."""
import json
from unittest.mock import AsyncMock

import pytest

from modules.report import reports
from modules.report.fallback_policy import ReportFallbackPolicy
from modules.report.followup_defaults import FOLLOWUP_DEFAULTS, default_followup_report


@pytest.mark.parametrize("kind", ["intervene", "instruction", "opinion", "explain", "explain_gutBarrier"])
def test_fallback_never_invents_normality_doses_or_evidence(kind):
    body = default_followup_report(kind, evaluate={}, user_id="test", session_id="test")
    text = json.dumps(body, ensure_ascii=False)
    for forbidden in ("一度失衡", "整体平稳", "低风险", "未提示", "可代偿", "1600", "30-35", "150 分钟"):
        assert forbidden not in text
    assert body["references" if kind == "explain_gutBarrier" else "evidence"] == []
    assert set(body["subject"]) == {"name", "gender", "age", "report_date"}
    if kind == "intervene":
        assert set(body["diet"]["target"]) == {"energy_kcal", "protein_g", "fat_g", "carbs_g", "fiber_g", "sodium_mg", "iodine_ug"}
        assert all(value is None for value in body["diet"]["target"].values())
        assert body["supplements"]["supplement_list"] == []
    if kind == "instruction":
        assert body["non_drug_intervention"]["supplements"]["items"] == []
        assert body["non_drug_intervention"]["gut_microbiota_regulation"]["probiotics"]["products"] == []
    if kind == "opinion":
        assert body["diagnostic_basis"]["abnormal_findings"] == []
    if kind in {"explain", "explain_gutBarrier"}:
        assert isinstance(body["preface"], dict)
        assert isinstance(body["sections"], list)
        assert isinstance(body["closing"], dict)
        assert "diagnostic_basis" not in body


def test_explicit_empty_supplements_never_turn_into_default_doses():
    from modules.report.followup_defaults import supplements_base
    assert supplements_base([])["items"] == []
    assert supplements_base()["items"] == []
    verified = [{"name": "已核验项目", "daily_dose": "来自有效评估的用量"}]
    body = supplements_base(verified)
    assert body["items"] == verified
    body["items"][0]["name"] = "changed"
    assert verified[0]["name"] == "已核验项目"


def test_coalesce_preserves_populated_fields_zero_false_and_nested_schema():
    parsed = {"energy_kcal": "已核实的个体化目标", "protein_g": 0, "flag": False}
    result = reports._coalesce(parsed, {"energy_kcal": None, "protein_g": 1, "flag": True, "nested": {"missing": None}})
    assert result == {**parsed, "nested": {"missing": None}}
    assert "nested" not in parsed
    assert reports._coalesce([], ["must not repopulate an explicitly empty list"]) == []


def test_nullable_string_fields_do_not_accept_numeric_or_boolean_model_values():
    result = reports._coalesce({"conclusion": 17, "risk": False}, FOLLOWUP_DEFAULTS["opinion_summary"])
    assert isinstance(result["conclusion"], str) and "暂缺" in result["conclusion"]
    assert result["risk"] is None
    assert reports._coalesce(0, 1) == 0
    assert reports._coalesce(False, True) is False
    assert reports._coalesce(False, 1) == 1  # bool is not an integer value here.


@pytest.mark.asyncio
async def test_model_empty_output_is_failed_not_a_successful_normal_report(monkeypatch):
    monkeypatch.setattr(reports, "safe_complete_async", AsyncMock(return_value="{}"))
    result, status = await reports._generate_followup_module(
        "opinion", lambda: reports.opinion_report({}, "u", "s"), data={}, user_id="u", session_id="s",
        policy=ReportFallbackPolicy(retry_backoff_seconds=()),
    )
    assert status == "fallback_empty" and result["status"] == "failed"
    assert result["diagnosis_summary"]["risk"] is None
    assert result["diagnostic_basis"]["abnormal_findings"] == []


@pytest.mark.asyncio
async def test_empty_recommend_does_not_attach_catalog_products(monkeypatch):
    monkeypatch.setattr(reports, "safe_complete_async", AsyncMock(return_value="{}"))
    with pytest.raises(ValueError, match="no usable selection"):
        await reports._recommend({}, "PRODUCT", ["strain"], [], [], {}, {}, {}, [])


@pytest.mark.asyncio
async def test_partial_model_output_keeps_verified_fields_and_marks_partial(monkeypatch):
    async def model(prompt, *, task_type):
        return '{"conclusion":"有来源支持的已核验结论"}' if task_type == "opinion_summary" else "{}"
    monkeypatch.setattr(reports, "safe_complete_async", model)
    body, status = await reports._generate_followup_module(
        "opinion", lambda: reports.opinion_report({}, "u", "s"), data={}, user_id="u", session_id="s",
        policy=ReportFallbackPolicy(retry_backoff_seconds=()),
    )
    assert status == "success" and body["status"] == "partial_success"
    assert body["diagnosis_summary"]["conclusion"] == "有来源支持的已核验结论"
    assert body["diagnosis_summary"]["risk"] is None


def test_defaults_are_not_shared_with_emitted_payloads():
    body = default_followup_report("intervene", evaluate={}, user_id="u", session_id="s")
    body["diet"]["target"]["energy_kcal"] = "changed"
    assert FOLLOWUP_DEFAULTS["diet"]["target"]["energy_kcal"] is None


@pytest.mark.parametrize("kind", ["intervene", "instruction", "opinion", "explain", "explain_gutBarrier"])
@pytest.mark.parametrize("error_code", ["REPORT_GENERATION_INTERRUPTED", "REPORT_DEPENDENCY_FAILED", "REPORT_GENERATION_FAILED"])
def test_interruption_and_dependency_failure_keep_identical_declared_fields(kind, error_code):
    from modules.report.terminal import failure_payload
    neutral = default_followup_report(kind, evaluate={}, user_id="u", session_id="s")
    failed = failure_payload(kind, {"userId": "u", "sessionId": "s", "finalReportId": "f"},
                             error_code=error_code)
    for field, value in neutral.items():
        assert failed[field] == value
    assert failed["status"] == "failed"


@pytest.mark.asyncio
async def test_catalog_and_assessment_reach_the_correct_recommend_parameters(monkeypatch):
    info = {"intestine_type": "已核验肠型"}
    candidates = [{"productCode": "TEST_ONLY"}]
    monkeypatch.setattr(reports, "_info", AsyncMock(return_value=info))
    monkeypatch.setattr(reports, "fetch_catalog", AsyncMock(return_value=("", [], [], {}, {}, {}, candidates)))
    seen = []

    async def recommend(data, mdf_code, fixed, random_strains, capsule, mdf_details, pfmt_details, info, pfmt_candidates):
        seen.append((info, pfmt_candidates))
        return {}  # Do not generate products in this argument-routing test.

    monkeypatch.setattr(reports, "_recommend", recommend)
    monkeypatch.setattr(reports, "opinion_report", AsyncMock(side_effect=RuntimeError("offline")))
    monkeypatch.setattr(reports, "explain_report", AsyncMock(side_effect=RuntimeError("offline")))
    monkeypatch.setattr(reports, "replay_followup_reports", AsyncMock(return_value={}))
    await reports._push_report({}, "u", "s", policy=ReportFallbackPolicy(retry_backoff_seconds=()))
    assert seen and all(pair == (info, candidates) for pair in seen)
