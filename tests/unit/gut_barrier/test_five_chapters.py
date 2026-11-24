from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.content_catalog import CHAPTERS, CONTENT_VERSION, TEMPLATE_VERSION
from modules.gut_barrier.interventions import build_interventions
from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.presentation import build_report
from modules.gut_barrier.publication import project_public_report
from modules.gut_barrier.schemas import GutBarrierRequest


def sample(**updates):
    value = {"userId": "acceptance-user", "sessionId": "acceptance-session", "name": "新版验收样本", "age": "50", "sex": "男",
             "gutHealthIndicator": {"dLacticAcid": {"value": "26.0", "unit": "mg/L"},
                "diamineOxidase": {"value": "17.00", "unit": "U/L"},
                "bacterialEndotoxin": {"value": "23.00", "unit": "U/L"},
                "hsCRP": {"value": "2.7", "unit": "mg/L"}},
             "fileList": [], "testData": [], "hospitalData": []}
    value.update(updates)
    return GutBarrierRequest.model_validate(value)


def report_for(request):
    assessment = assess_case(request, context={"currentDate": "2026-09-24"})
    return build_report(assessment, build_interventions(assessment), report_id="gbr_five_chapter_test")


def block(report, section, title):
    return next(b for s in report["sections"] if s["id"] == section for b in s["blocks"] if b.get("title") == title)


@pytest.mark.parametrize("case", [sample(), sample(gutHealthIndicator={}), sample(age=None), sample(age=10)])
def test_five_chapters_and_required_sections_exist_for_every_input(case):
    report = report_for(case)
    public = project_public_report(report)
    assert [s["id"] for s in public["sections"]] == [identifier for identifier, _ in CHAPTERS]
    assert all(set(s) == {"id", "title", "subtitle", "blocks", "visible"} for s in public["sections"])
    assert all(s["visible"] for s in public["sections"])
    markers = block(public, "overview", "三项检测指标")
    assert len(markers["columns"]) == 5 and len(markers["rows"]) == 3
    assert all(len(row) == 5 for row in markers["rows"])
    assert len(block(public, "overview", "1.1 检测综合评估结论")["rows"]) == 3
    assert len(public["sections"][3]["blocks"]) == 6
    risk_tables = [b for b in public["sections"][1]["blocks"] if b["kind"] == "table" and b["columns"] == ["疾病", "风险指数", "风险等级", "机制关联"]]
    assert len(risk_tables) == 7 and sum(len(b["rows"]) for b in risk_tables) == 22
    assert all(row[1] is None and row[2] == "未评估" for b in risk_tables for row in b["rows"])
    assert len(block(public, "appendix", "5.5 参考文献")["items"]) == 14
    assert not any(b["title"] == "本次样本与质控记录" for s in public["sections"] for b in s["blocks"])


def test_latest_case_scores_precision_no_fake_disease_risk_or_completed_treatment():
    report = report_for(sample())
    assessed = report["assessment"]
    assert assessed["barrier"]["score"] == 9.5
    assert assessed["barrier"]["grade"] == "V" and assessed["barrier"]["pattern"] == "+++"
    assert "inflammation" not in assessed
    table = block(report, "overview", "三项检测指标")
    assert [r[1] for r in table["rows"]] == ["26.0 mg/L", "17.00 U/L", "23.00 U/L"]
    assert [r[-1] for r in table["rows"]] == [3, 3.5, 3]
    public_text = json.dumps(project_public_report(report), ensure_ascii=False)
    assert "9.5" in public_text and "混合型受损" in public_text
    for forbidden in ("张学斌", "GBF20260923-01", "已形成代谢性内毒素血症", "已完成治疗", "0.3–0.5 g/kg", "七种异常组合的干预策略对照"):
        assert forbidden not in public_text
    assert report["meta"]["contentVersion"] == CONTENT_VERSION
    assert report["meta"]["templateVersion"] == TEMPLATE_VERSION


def test_missing_fields_remain_null_and_not_normalized_to_zero():
    report = project_public_report(report_for(sample(gutHealthIndicator={})))
    assert all(row[1] is None and row[-1] is None for row in block(report, "overview", "三项检测指标")["rows"])
    card = block(report, "overview", "肠道屏障功能评分（IBFC）")["items"][0]
    assert card["value"] is None
    assert not any(item["selected"] for item in block(report, "overview", "屏障评分分级")["items"])


def test_removed_crp_range_does_not_change_barrier_readout():
    request = sample(gutHealthIndicator={"dLacticAcid": {"value": "26", "unit": "mg/L"},
        "diamineOxidase": {"value": "17", "unit": "U/L"}, "bacterialEndotoxin": {"value": "23", "unit": "U/L"},
        "hsCRP": {"value": ">1", "unit": "mg/dL"}})
    report = report_for(request)
    assert len(block(report, "overview", "三项检测指标")["rows"]) == 3
    assert "inflammation" not in report["assessment"]
    assert report["assessment"]["barrier"]["score"] == 9.5


@pytest.mark.asyncio
async def test_fixed_chapters_do_not_call_ai_and_risk_model_absence_is_partial():
    narrative = SimpleNamespace(arrange=AsyncMock(side_effect=AssertionError("fixed chapters must not be replaced")))
    pipeline = GutBarrierPipeline(narrative=narrative)
    report, audit = await pipeline.generate(sample(), report_id="gbr_pipeline", evaluation_date="2026-09-24")
    assert report["narrativeCandidates"] == [] and report["interpretation"] == []
    narrative.arrange.assert_not_called()
    assert audit["partial"] is True and audit["riskModel"]["status"] == "not_configured"
    assert audit["contentVersion"] == CONTENT_VERSION
    assert report["meta"]["narrativeStatus"] == "deterministic"


def test_public_json_projection_is_idempotent_and_does_not_mutate_frozen_document():
    report = project_public_report(report_for(sample()))
    before = deepcopy(report)
    assert project_public_report(report) == before
    assert report == before


@pytest.mark.parametrize("case", [sample(gutHealthIndicator={}), sample(age=None), sample(age=10),
                                  sample(gutHealthIndicator={
                                      "dLacticAcid": {"value": "5", "unit": "mg/L"},
                                      "diamineOxidase": {"value": "5", "unit": "U/L"},
                                      "bacterialEndotoxin": {"value": "10", "unit": "U/L"},
                                      "hsCRP": {"value": "0.4", "unit": "mg/L"},
                                  })])
def test_repair_education_and_appendix_guidance_are_fixed_across_patient_branches(case):
    baseline = project_public_report(report_for(sample()))
    actual = project_public_report(report_for(case))
    for section_id in ("repair_plan", "education"):
        expected = next(s for s in baseline["sections"] if s["id"] == section_id)
        assert next(s for s in actual["sections"] if s["id"] == section_id) == expected
    fixed_appendix = lambda report: [b for s in report["sections"] if s["id"] == "appendix"
                                    for b in s["blocks"] if b["kind"] != "references"]
    assert fixed_appendix(actual) == fixed_appendix(baseline)
