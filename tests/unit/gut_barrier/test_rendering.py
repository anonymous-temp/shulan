"""Report assembly and clinical-state contracts without a preview dependency."""

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.gut_barrier.presentation import build_report
from modules.gut_barrier.publication import project_public_report
from tests.unit.gut_barrier.report_fixtures import (
    assessment, interventions, document, five_chapter_fixture,
    reordered_chapter_fixture, CHAPTER_IDS, RISK_COLUMNS,
)


def test_document_preserves_inputs_without_aliasing_or_simulation():
    source, plans = assessment(), interventions()
    before = deepcopy((source, plans))
    report = build_report(source, plans, report_id="REPORT-X", source_summary=[{"name": "检验单"}])
    assert (source, plans) == before
    assert report["assessment"] == source
    assert report["interventions"] == plans
    assert report["meta"]["mode"] == "clinical"
    report["assessment"]["subject"]["name"] = "changed copy"
    assert source["subject"]["name"] == "真实输入姓名"


def test_invalid_top_level_input_is_rejected():
    with pytest.raises(TypeError):
        build_report([], {}, report_id="X")
    with pytest.raises(ValueError):
        build_report({}, {}, report_id="")


def test_score_grade_and_demographics_come_from_assessment_not_old_sample():
    report = document()
    card = next(b for b in report["sections"][0]["blocks"] if b.get("title") == "肠道屏障功能评分（IBFC）")
    grade = next(item["value"] for item in card["items"] if item["label"] == "损伤等级")
    assert "IV 级" in grade and "III 级" not in grade
    tables = [b for s in report["sections"] if s["id"] == "disease_risks" for b in s["blocks"]
              if b.get("columns") == list(RISK_COLUMNS)]
    assert sum(len(b["rows"]) for b in tables) == 22
    assert all(row[1] is None for b in tables for row in b["rows"])


@pytest.mark.parametrize("band", ["low", "intermediate", "high"])
def test_hs_crp_selected_band_is_data_driven(band):
    report = document(band=band)
    blocks = next(section for section in report["sections"] if section["id"] == "overview")["blocks"]
    selected = [item for block in blocks if block.get("title") == "hs-CRP 项目功能分层" for item in block["items"] if item["selected"]]
    assert selected == []


def test_review_or_missing_inflammation_does_not_highlight_a_stable_risk_band():
    report = document(band="requires_clinical_review", hs_status="needs_review")
    assert not any(b.get("title") == "hs-CRP 项目功能分层" for b in report["sections"][0]["blocks"])


def test_normal_barrier_has_no_invasive_treatment_sections():
    report = document(pattern="---", band="low")
    ids = {section["id"] for section in report["sections"]}
    assert "barrier-care" not in ids and "pfmt" not in ids
    assert len(report["interventions"]["microbiome"]["sevenPatternPlans"]) == 7


def test_fixed_repair_chapter_does_not_publish_unapproved_pattern_matrix():
    report = document()
    assert len(report["interventions"]["microbiome"]["sevenPatternPlans"]) == 7
    section = next(s for s in report["sections"] if s["id"] == "repair_plan")
    assert any(b.get("title") == "3.5 第四原则 · 修复：MDF 靶向功能菌群调节" for b in section["blocks"])
    assert all(b.get("title") != "本次分型的功能调节目标" for b in section["blocks"])


def test_runtime_intervention_records_remain_internal_not_in_fixed_repair_copy():
    report = document()
    assert report["interventions"] == interventions()


def test_historical_clinical_findings_remain_visible_and_not_current_diagnoses():
    source = assessment()
    source["subject"]["clinicalFindings"] = [
        {"itemName": "胃镜", "date": "2023-01-02", "temporalStatus": "historical", "examResult": "原始胃镜描述"},
        {"itemName": "低密度脂蛋白", "date": "2024-01-02", "temporalStatus": "historical", "value": 4.5, "unit": "mmol/L", "resultStatusName": "偏高", "referenceRange": "原单区间"},
    ]
    report = build_report(source, interventions(), report_id="HISTORY")
    assert report["assessment"] == source


def test_real_assessment_contract_renders_without_fixture_specific_assumptions():
    from modules.gut_barrier.assessment import assess_case
    from modules.gut_barrier.schemas import GutBarrierRequest

    request = GutBarrierRequest.model_validate({
        "userId": "UNIT-USER", "sessionId": "UNIT-SESSION", "age": 52,
        "gutHealthIndicator": {
            "dLacticAcid": {"value": 19, "unit": "mg/L"},
            "diamineOxidase": {"value": 13, "unit": "U/L"},
            "bacterialEndotoxin": {"value": 24, "unit": "U/L"},
        },
    })
    assessed = assess_case(request, observations=[{"code": "HS_CRP", "value": 4.2, "unit": "mg/L", "isCurrent": True}])
    report = build_report(assessed, interventions(), report_id="REAL-CONTRACT")
    assert report["assessment"]["barrier"]["grade"] == "IV"


@pytest.mark.parametrize("age", [5, 17, None])
def test_adult_risk_and_regimens_are_not_applied_to_minors_or_unknown_age(age):
    source, plans = assessment(), interventions()
    source["subject"]["age"] = age
    source["risks"] = [{"code": "cardiovascular", "name": "成人心血管风险判断", "evaluation": "成人较高风险"}]
    plans["nutrition"][0]["dose"] = {"amount": "ADULT-DOSE-15g"}
    plans["lifestyle"][0]["actions"] = ["ADULT-ACTIVITY-150MIN"]
    report = build_report(source, plans, report_id="AGE-SCOPE")
    assert not any(b.get("title") == "hs-CRP 项目功能分层" for b in report["sections"][0]["blocks"])
    assert "barrier-care" not in {section["id"] for section in report["sections"]}
    assert report["assessment"] == source and report["interventions"] == plans


@pytest.mark.parametrize("score, pattern", [(2, "+--"), (4.5, "++-")])
def test_known_score_and_pattern_remain_visible_when_only_grade_needs_review(score, pattern):
    source = assessment()
    source["barrier"] = {"score": score, "grade": None, "pattern": pattern, "type": "已确定组合", "status": "needs_review"}
    report = build_report(source, interventions(), report_id="GRADE-BOUNDARY")
    assert str(score) in report["summary"]["text"]
    assert "已确定组合" in report["summary"]["text"]
    assert "分级待复核" in report["summary"]["text"]
    assert "缺失结果" not in report["summary"]["text"]


def test_original_microbiome_source_is_preserved_without_importing_diagnosis_or_prescription():
    source = assessment()
    original = {
        "sourceId": "SRC-DETAIL", "format": "json", "status": "source_only",
        "origin": "testData", "identityVerified": False, "sampleDate": None,
        "enterotype": {"raw": "原始 B 型", "value": "B", "status": "source_only", "eligibleForMatching": False},
        "metrics": [{"code": "Diversity_Index", "name": "菌群多样性", "value": 7.19}, {"name": "ASD风险", "value": 99}],
        "sourceModelIndices": [{"name": "原报告糖代谢评价", "value": 22.35, "interpretation": "source_model_index_not_probability"}, {"name": "ASD风险", "value": 99}],
        "modelMetadata": {"age": 88, "conclusion": "SOURCE-CONCLUSION-NOT-NEW-DIAGNOSIS"},
        "rawData": {"source": "unchanged"}, "summaryText": "SOURCE-PRESCRIPTION-NOT-NEW-PLAN",
        "warnings": ["conflicting_source_metric:Diversity_Index"],
    }
    source["context"] = {"microbiomeSources": [original]}
    before = deepcopy(source)
    report = build_report(source, interventions(), report_id="SOURCE-PRESERVED")
    assert source == before
    assert report["microbiomeSources"][0]["sourceModelIndices"] == original["sourceModelIndices"]
    assert report["microbiomeSources"][0]["sourceId"] == original["sourceId"]
    assert "microbiomeSources" not in report["assessment"]["context"]
    assert not {"rawData", "summaryText", "modelMetadata"} & report["microbiomeSources"][0].keys()
    assert report["microbiomeSources"][0]["warnings"] == ["conflicting_source_metric:Diversity_Index"]


def test_patient_body_uses_fixed_principles_without_repeated_implementation_rules():
    plans = interventions()
    plans["followup"].append({"timepoint": "临床评估后", "actions": ["复查 hs-CRP，结合近期感染情况解释"], "monitoring": ["炎症变化"]})
    report = build_report(assessment(), plans, report_id="FORMAL-COPY")
    assert report["interventions"]["followup"] == plans["followup"]


@pytest.mark.asyncio
async def test_source_parser_to_assessment_to_report_keeps_auxiliary_data_separate():
    from adapters.gut_barrier.sources import GutBarrierSources
    from modules.gut_barrier.assessment import assess_case
    from modules.gut_barrier.schemas import GutBarrierRequest

    payload = {
        "diseaseRisk": {"便秘": "55.56", "ASD": "74.17", "age": "1"},
        "microbeData": {"comprehensive": [{"latinName": "Enterotype", "value": "Bacteroides"}, {"latinName": "Diversity_Index", "value": "3.4818"}]},
    }
    original_summary = "多样性指数3.6909。建议移植菌群。"
    downloader = SimpleNamespace(download=AsyncMock(side_effect=[json.dumps(payload).encode(), original_summary.encode()]))
    request = GutBarrierRequest.model_validate({
        "userId": "UNIT-SOURCE", "sessionId": "UNIT-SOURCE", "name": "example-name",
        "testData": [{"resultDetailUrl": "https://example.org/detail", "resultSummaryUrl": "https://example.org/summary"}],
        "gutHealthIndicator": {"dLacticAcid": {"value": 19, "unit": "mg/L"}, "diamineOxidase": {"value": 13, "unit": "U/L"}, "bacterialEndotoxin": {"value": 24, "unit": "U/L"}},
    })
    collected = await GutBarrierSources(downloader=downloader).collect(request)
    assessed = assess_case(request, observations=collected["observations"], context=collected["context"])
    report = build_report(assessed, {}, report_id="AUXILIARY-INTEGRATION")
    assert report["subject"]["age"] is None
    assert len(report["microbiomeSources"]) == 2
    assert report["microbiomeSources"][0]["sourceModelIndices"][0]["value"] == "55.56"
    assert "rawData" not in report["microbiomeSources"][0]
    assert any("conflicting_source_metric:Diversity_Index" in s["warnings"] for s in report["microbiomeSources"])


@pytest.mark.parametrize("pattern", ["---", "+--", "-+-", "--+", "++-", "+-+", "-++", "+++"])
def test_eight_real_assessment_intervention_paths_render_the_matching_case(pattern):
    from modules.gut_barrier.assessment import assess_case
    from modules.gut_barrier.interventions import build_interventions
    from modules.gut_barrier.schemas import GutBarrierRequest

    values = [high if sign == "+" else low for sign, high, low in zip(pattern, [19, 13, 24], [9, 5, 10])]
    request = GutBarrierRequest.model_validate({
        "userId": "UNIT-ALL-PATTERNS", "sessionId": "UNIT-ALL-PATTERNS", "name": "测试输入", "age": 46,
        "gutHealthIndicator": {
            "dLacticAcid": {"value": values[0], "unit": "mg/L"},
            "diamineOxidase": {"value": values[1], "unit": "U/L"},
            "bacterialEndotoxin": {"value": values[2], "unit": "U/L"},
        },
    })
    assessed = assess_case(request, observations=[{"code": "HS_CRP", "value": 4.2, "unit": "mg/L", "isCurrent": True}])
    plans = build_interventions(assessed)
    report = build_report(assessed, plans, report_id="ALL-PATTERNS")
    assert report["assessment"]["barrier"]["pattern"] == pattern
    assert report["assessment"] == assessed and report["interventions"] == plans
    assert len(plans["microbiome"]["sevenPatternPlans"]) == 7
    assert tuple(section["id"] for section in report["sections"]) == CHAPTER_IDS
    if assessed["barrier"]["grade"] is None:
        assert '缺失结果' not in report['summary']['text']


@pytest.mark.parametrize("age", [8, 17, None])
def test_real_intervention_builder_never_exposes_adult_regimens_to_children_or_unknown_age(age):
    from modules.gut_barrier.interventions import build_interventions

    assessed = assessment()
    assessed["subject"]["age"] = age
    plans = build_interventions(assessed)
    report = build_report(assessed, plans, report_id="REAL-AGE-SAFETY")
    assert not any(b.get("title") == "hs-CRP 项目功能分层" for b in report["sections"][0]["blocks"])
    assert "barrier-care" not in {section["id"] for section in report["sections"]}
    assert report["interventions"] == plans


def test_large_auxiliary_source_is_summarized_once_without_losing_clinical_context():
    source = assessment()
    microbiome_source = {
        "sourceId": "BIG-SOURCE", "format": "json", "status": "source_only", "sha256": "a" * 64,
        "identityVerified": False, "sampleDate": None,
        "rawData": {"species": "RAW-SPECIES-PAYLOAD-" * 70000},
        "summaryText": "SOURCE-REGIMEN-" * 1000,
        "modelMetadata": {"age": "1", "conclusion": "SOURCE-MODEL-CONCLUSION"},
        "metrics": [{"code": "Diversity_Index", "name": "多样性指数", "value": 3.4818, "path": "$.microbeData.comprehensive[1]"}],
        "sourceModelIndices": [{"name": "便秘", "value": 55.56, "raw": "55.56", "path": "$.diseaseRisk.便秘", "interpretation": "source_model_index_not_probability"}],
    }
    source["subject"]["context"] = {"microbiomeSources": [microbiome_source], "symptoms": ["输入腹胀"], "medications": ["输入用药"]}
    source["context"] = {"microbiomeSources": [microbiome_source], "clinicalFindings": [{"itemName": "原胃镜", "temporalStatus": "historical", "date": "2023-01-02", "examResult": "原胃镜描述"}]}
    before = deepcopy(source)
    report = build_report(source, interventions(), report_id="LARGE-SOURCE", source_summary=[{"sourceId": "BIG-SOURCE", "sha256": "a" * 64}])
    encoded = json.dumps(report, ensure_ascii=False)
    assert source == before
    assert len(encoded.encode()) < 400000
    assert encoded.count('"microbiomeSources"') == 1
    assert all(f'"{key}"' not in encoded for key in ("rawData", "summaryText", "modelMetadata"))
    assert report["assessment"]["markers"] == source["markers"]
    assert report["assessment"]["barrier"] == source["barrier"]
    assert report["subject"]["context"]["medications"] == ["输入用药"]
    assert report["assessment"]["context"]["clinicalFindings"] == source["context"]["clinicalFindings"]
    assert report["microbiomeSources"][0]["sha256"] == "a" * 64
    assert report["microbiomeSources"][0]["sourceModelIndices"][0]["path"] == "$.diseaseRisk.便秘"


def test_new_health_indicator_range_and_real_lipid_history_are_patient_readable():
    from modules.gut_barrier.assessment import assess_case
    from modules.gut_barrier.schemas import GutBarrierRequest

    rows = [
        {"itemName": "低密度脂蛋白(LDL-c)", "date": "2025-12-15", "value": "3.70", "resultStatusName": "偏高"},
        {"itemName": "总胆固醇(TC)", "date": "2025-12-15", "value": "6.48", "resultStatusName": "偏高"},
        {"itemName": "总胆固醇（TC）", "date": "2025-10-29", "value": "6.09", "resultStatusName": "偏高"},
        {"itemName": "低密度脂蛋白（LDL-c）", "date": "2025-10-29", "value": "3.44", "resultStatusName": "偏高"},
    ]
    request = GutBarrierRequest.model_validate({
        "userId": "RANGE-USER", "sessionId": "RANGE-SESSION", "age": 28,
        "hospitalData": rows,
        "gutHealthIndicator": {
            "dLacticAcid": {"value": "0.06", "unit": "mg/L"},
            "diamineOxidase": {"value": "3.8", "unit": "U/L"},
            "bacterialEndotoxin": {"value": "12.5", "unit": "U/L"},
            "hsCRP": {"value": ">3.0", "unit": "mg/L"},
        },
    })
    assessed = assess_case(request, context={"currentDate": "2026-09-23"})
    report = build_report(assessed, {}, report_id="RANGE-REPORT")

    assert report["assessment"]["barrier"]["pattern"] == "---"
    assert "inflammation" not in report["assessment"]
    cardiovascular = next(row for row in report["assessment"]["risks"] if row["code"] == "cardiovascular")
    assert len(cardiovascular["basis"]) == 4
    assert sum("原报告标记升高" in item for item in cardiovascular["basis"]) == 4
    assert not any("mmol" in item for item in cardiovascular["basis"])

    assert '>3.0 mg/L' not in report['summary']['text']
    bands = {block["title"]: block["items"] for block in report["sections"][0]["blocks"] if block.get("kind") == "bands"}
    assert "hs-CRP 项目功能分层" not in bands
    assert not any(item["selected"] for item in bands["屏障评分分级"])
    marker_table = next(b for b in report["sections"][0]["blocks"] if b.get("title") == "三项检测指标")
    assert len(marker_table["rows"]) == 3


def test_converted_range_uses_normalized_value_in_visible_report_and_keeps_raw_source():
    from modules.gut_barrier.assessment import assess_case
    from modules.gut_barrier.schemas import GutBarrierRequest

    request = GutBarrierRequest.model_validate({
        "userId": "CONVERTED", "sessionId": "CONVERTED", "age": 28,
        "gutHealthIndicator": {"hsCRP": {"value": ">0.3", "unit": "mg/dL"}},
    })
    assessed = assess_case(request, context={"currentDate": "2026-09-23"})
    report = build_report(assessed, {}, report_id="CONVERTED-RANGE")

    assert all(item["code"] != "HS_CRP" for item in report["assessment"]["markers"])
    assert assessed["barrier"]["score"] is None
    assert '>3 mg/L' not in report['summary']['text']


def test_reordered_json_has_no_preview_only_figures_or_anchors():
    report = reordered_chapter_fixture()
    public = project_public_report(report)
    assert all(block["kind"] != "figure" and "anchor" not in block for section in public["sections"] for block in section["blocks"])


def test_legacy_json_does_not_add_preview_only_figure_kind():
    report = five_chapter_fixture()
    public = project_public_report(report)
    assert all(block["kind"] != "figure" for section in public["sections"] for block in section["blocks"])


@pytest.mark.parametrize("index", [0, 2.5, 10])
def test_numeric_risk_index_uses_same_frozen_values_as_the_table(index):
    report = five_chapter_fixture(risk_index=index)
    assert report["sections"][2]["blocks"][0]["rows"][0][1] == index

