"""Independent report assembly, clinical-state presentation and HTML safety."""

from copy import deepcopy

from html.parser import HTMLParser

import json

import re

from types import SimpleNamespace

from unittest.mock import AsyncMock

import pytest

from adapters.gut_barrier.rendering import render_report_html

from adapters.gut_barrier.graphics import CHAPTER_IDS, LEGACY_CHAPTER_IDS, MARKER_COLUMNS, RISK_COLUMNS, chapter_layout

from modules.gut_barrier.presentation import build_report

from modules.gut_barrier.publication import project_public_report
from tests.unit.gut_barrier.report_fixtures import (
    assessment, document, five_chapter_fixture, interventions, reordered_chapter_fixture,
)




def visible_source(html):
    return re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", html, flags=re.S)

class Parsed(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

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
    body = visible_source(render_report_html(build_report(source, plans, report_id="REPORT-X")))
    assert "真实输入姓名" in body
    assert "模拟受检者" not in body and "175 cm" not in body and "86 kg" not in body
    assert "internal_rule_check" not in body

def test_score_grade_and_demographics_come_from_assessment_not_old_sample():
    report = document()
    body = visible_source(render_report_html(report))
    assert "IV 级" in body and "7.5" in body
    card = next(b for b in report["sections"][0]["blocks"] if b.get("title") == "肠道屏障功能评分（IBFC）")
    grade = next(item["value"] for item in card["items"] if item["label"] == "损伤等级")
    assert "IV 级" in grade and "III 级" not in grade
    assert "46 岁" in body and "女" in body
    assert "84.78" not in body
    tables = [b for s in report["sections"] if s["id"] == "disease_risks" for b in s["blocks"]
              if b.get("columns") == list(RISK_COLUMNS)]
    assert sum(len(b["rows"]) for b in tables) == 22
    assert all(row[1] is None for b in tables for row in b["rows"])

@pytest.mark.parametrize("band", ["low", "intermediate", "high"])
def test_current_builder_ignores_legacy_crp_band(band):
    report = document(band=band)
    blocks = next(section for section in report["sections"] if section["id"] == "overview")["blocks"]
    assert not any(b.get("title") == "hs-CRP 项目功能分层" for b in blocks)
    assert not any("hs-CRP" in str(b) for b in blocks)
    tags = Parsed(render_report_html(report)).tags
    assert sum(attrs.get("class") == "band selected" for _,attrs in tags) == 1


def test_legacy_inflammation_review_state_does_not_reappear_in_current_overview():
    report = document(band="requires_clinical_review", hs_status="needs_review")
    assert not any(b.get("title") == "hs-CRP 项目功能分层" for b in report["sections"][0]["blocks"])


def test_normal_barrier_has_no_invasive_treatment_sections():
    report = document(pattern="---", band="low")
    ids = {section["id"] for section in report["sections"]}
    assert "barrier-care" not in ids and "pfmt" not in ids
    body = visible_source(render_report_html(report))
    assert "三项屏障相关指标未见异常" in body
    assert "移植备选" not in body and "来源方案范围" not in body
    assert "维持合理膳食、规律活动及作息" in body
    assert 'class="current"' not in body
    assert "七种异常组合" not in body and "STRAIN-1" not in body
    assert len(report["interventions"]["microbiome"]["sevenPatternPlans"]) == 7

def test_missing_and_boundary_results_do_not_invent_scores_or_profile():
    source = assessment()
    source["subject"] = {}
    source["barrier"] = {"score": None, "grade": None, "pattern": None, "status": "needs_review"}
    for marker in source["markers"][:3]:
        marker.update(value=None, score=None, abnormal=None, level=None, status="needs_review")
    report = build_report(source, {}, report_id="INCOMPLETE")
    body = visible_source(render_report_html(report))
    assert "暂不能评分及确定综合等级" in body
    assert "35 岁" not in body and "腹型肥胖" not in body
    assert "7.5" not in body

def test_fixed_repair_chapter_does_not_publish_unapproved_pattern_matrix():
    report = document()
    assert len(report["interventions"]["microbiome"]["sevenPatternPlans"]) == 7
    section = next(s for s in report["sections"] if s["id"] == "repair_plan")
    assert any(b.get("title") == "3.5 第四原则 · 修复：MDF 靶向功能菌群调节" for b in section["blocks"])
    assert all(b.get("title") != "本次分型的功能调节目标" for b in section["blocks"])
    body = visible_source(render_report_html(report))
    assert "STRAIN-7" not in body and "STRAIN-1" not in body
    assert "七种异常组合的干预策略对照" not in body

def test_runtime_intervention_records_remain_internal_not_in_fixed_repair_copy():
    report = document()
    body = visible_source(render_report_html(report))
    for internal in ["TEST-01", "目录用法A", "临床状态稳定后", "核对复查结果", "专科确认适应条件", "药师核对标签"]:
        assert internal not in body
    assert report["interventions"] == interventions()
    assert "精准肠道菌群移植（PFMT）" in body and "丙氨酰谷氨酰胺" in body
    assert "来源方案范围" not in body and "NCFM" not in body

def test_xss_in_all_sources_is_escaped_and_unsafe_links_are_rejected():
    source, plans = assessment(), interventions()
    attack = '</script><img src=x onerror="alert(1)"><script>alert(2)</script>'
    source["subject"]["name"] = attack
    source["risks"] = [{"name": attack, "axis": attack, "evaluation": attack, "basis": [attack], "recommendations": [attack]}]
    plans["references"] = [{"title": attack, "url": "javascript:alert(3)"}, {"title": "safe", "url": "https://example.org/?a=1&b=2"}]
    result = render_report_html(build_report(source, plans, report_id=attack))
    parsed = Parsed(result)
    assert not any(tag == "img" for tag, _ in parsed.tags)
    assert not any("onerror" in attrs for _, attrs in parsed.tags)
    assert not any(attrs.get("href", "").startswith("javascript:") for _, attrs in parsed.tags)
    assert sum(tag == "script" for tag, _ in parsed.tags) == 2
    assert "&lt;img" in result and "\\u003c/script\\u003e" in result

def test_serialized_document_is_the_same_single_source():
    report = document()
    result = render_report_html(report)
    data = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', result, re.S).group(1)
    assert json.loads(data) == report

def test_interpretation_reorders_whitelisted_facts_only_and_keeps_required():
    # A historical callback/preview still supports its dedicated legacy section.
    report = {"meta": {"title": "旧版解析"}, "sections": [{"id": "interpretation", "blocks": [
        {"kind": "paragraph", "text": "旧占位内容"}]}], "narrativeCandidates": [
        {"id": "summary", "text": "已核实的个体结论", "required": True},
        {"id": "marker-lps", "text": "LPS 是革兰阴性菌外膜成分"}],
        "interpretation": [{"id": "marker-lps", "text": "UNAPPROVED MEDICAL CLAIM"}, {"id": "unknown", "text": "UNAPPROVED SECOND CLAIM"}]}
    body = visible_source(render_report_html(report))
    interpretation = body.split('id="interpretation"', 1)[1].split("</section>", 1)[0]
    assert "UNAPPROVED" not in interpretation
    assert "LPS 是革兰阴性菌外膜成分" in interpretation
    assert "已核实的个体结论" in interpretation

def test_mobile_labels_exist_without_javascript_and_print_is_not_fixed_page_count():
    result = render_report_html(document())
    assert 'data-label="检测结果"' in result
    assert "break-after:page" not in result
    assert "grid-template-columns" in result and "@media print" in result
    assert "window.print()" in result

def test_historical_clinical_findings_remain_visible_and_not_current_diagnoses():
    source = assessment()
    source["subject"]["clinicalFindings"] = [
        {"itemName": "胃镜", "date": "2023-01-02", "temporalStatus": "historical", "examResult": "原始胃镜描述"},
        {"itemName": "低密度脂蛋白", "date": "2024-01-02", "temporalStatus": "historical", "value": 4.5, "unit": "mmol/L", "resultStatusName": "偏高", "referenceRange": "原单区间"},
    ]
    report = build_report(source, interventions(), report_id="HISTORY")
    body = visible_source(render_report_html(report))
    assert "2023-01-02" in body and "原始胃镜描述" in body
    assert "既往检查" in body and "4.5 mmol/L" in body
    assert "原报告参考范围：原单区间" in body
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
    body = visible_source(render_report_html(report))
    assert "52 岁" in body and "7.5" in body and "IV 级" in body
    assert "35 岁" not in body and "模拟案例" not in body

@pytest.mark.parametrize("age", [5, 17, None])
def test_adult_risk_and_regimens_are_not_applied_to_minors_or_unknown_age(age):
    source, plans = assessment(), interventions()
    source["subject"]["age"] = age
    source["risks"] = [{"code": "cardiovascular", "name": "成人心血管风险判断", "evaluation": "成人较高风险"}]
    plans["nutrition"][0]["dose"] = {"amount": "ADULT-DOSE-15g"}
    plans["lifestyle"][0]["actions"] = ["ADULT-ACTIVITY-150MIN"]
    report = build_report(source, plans, report_id="AGE-SCOPE")
    body = visible_source(render_report_html(report))
    assert not any(b.get("title") == "hs-CRP 项目功能分层" for b in report["sections"][0]["blocks"])
    assert "成人较高风险" not in body and "成人心血管风险判断" not in body
    assert "ADULT-DOSE-15g" not in body and "ADULT-ACTIVITY-150MIN" not in body
    assert "不套用成人营养剂量" in body
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
    assert "分级待复核" in visible_source(render_report_html(report))

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
    body = visible_source(render_report_html(report))
    assert source == before
    assert report["microbiomeSources"][0]["sourceModelIndices"] == original["sourceModelIndices"]
    assert report["microbiomeSources"][0]["sourceId"] == original["sourceId"]
    assert "microbiomeSources" not in report["assessment"]["context"]
    assert not {"rawData", "summaryText", "modelMetadata"} & report["microbiomeSources"][0].keys()
    assert "原始 B 型" in body and "7.19" not in body and "22.35" not in body
    assert report["microbiomeSources"][0]["warnings"] == ["conflicting_source_metric:Diversity_Index"]
    assert "SOURCE-PRESCRIPTION-NOT-NEW-PLAN" not in body and "SOURCE-CONCLUSION-NOT-NEW-DIAGNOSIS" not in body
    assert "ASD风险" not in body and "88 岁" not in body
    assert "22.35%" not in body

def test_patient_body_uses_fixed_principles_without_repeated_implementation_rules():
    plans = interventions()
    plans["followup"].append({"timepoint": "临床评估后", "actions": ["复查 hs-CRP，结合近期感染情况解释"], "monitoring": ["炎症变化"]})
    report = build_report(assessment(), plans, report_id="FORMAL-COPY")
    body = visible_source(render_report_html(report))
    for internal in ["组合仅描述指标状态", "结果评价依据所采用", "异常组合用于组织", "本次结果不能单独确认肠源性"]:
        assert internal not in body
    assert "复查 hs-CRP，结合近期感染情况解释" not in body
    assert "核对后确定" not in body and "药师核对标签" not in body
    assert "定期复评" in body and "适应证" in body
    assert report["interventions"]["followup"] == plans["followup"]

def test_internal_nutrition_modules_and_preparation_forms_are_translated():
    plans = interventions()
    plans["microbiome"]["sevenPatternPlans"][0]["nutritionModules"] = ["fiber_total", "zinc_if_needed", "glutamine_specialist_review"]
    plans["microbiome"]["sevenPatternPlans"][0]["researchCandidates"] = [{"name": "菌株A", "strain": "A-01", "form": "cell_free_supernatant"}]
    body = visible_source(render_report_html(build_report(assessment(), plans, report_id="PUBLIC-TERMS")))
    assert "fiber_total" not in body and "cell_free_supernatant" not in body
    assert "A-01" not in body  # Unmatched, unapproved research candidate stays internal.

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
    body = visible_source(render_report_html(report))
    assert report["subject"]["age"] is None
    assert len(report["microbiomeSources"]) == 2
    assert report["microbiomeSources"][0]["sourceModelIndices"][0]["value"] == "55.56"
    assert "rawData" not in report["microbiomeSources"][0]
    assert "Bacteroides" in body and "3.4818" not in body and "3.6909" not in body
    assert "55.56" not in body and "建议移植菌群" not in body
    assert any("conflicting_source_metric:Diversity_Index" in s["warnings"] for s in report["microbiomeSources"])
    assert "1 岁" not in body and "ASD" not in body

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
    body = visible_source(render_report_html(report))
    assert report["assessment"]["barrier"]["pattern"] == pattern
    assert report["assessment"] == assessed and report["interventions"] == plans
    assert "测试输入" in body and "46 岁" in body
    assert len(plans["microbiome"]["sevenPatternPlans"]) == 7
    assert "待核对适用条件" not in body
    assert "fiber_total" not in body and "cell_free_supernatant" not in body
    assert "3.6 第五原则 · 平衡：生活方式干预" in body
    assert tuple(section["id"] for section in report["sections"]) == CHAPTER_IDS
    if pattern == "---":
        assert "七种异常组合" not in body and 'class="current"' not in body
        assert "维持合理膳食、规律活动及作息" in body
    else:
        assert "1.4 肠道屏障受损类型与受损状况" in body
        assert "七种异常组合" not in body
        assert "通过食物保障能量" in body
        assert "40 mg/day" not in body and "4000 IU/day" not in body
        assert "营养状态" in body
    if assessed["barrier"]["grade"] is None:
        assert "分级待复核" in body and "缺失结果" not in report["summary"]["text"]
    if pattern == "+++":
        assert "5 g、每日3次" not in body  # Research regimen is not an approved prescription.

@pytest.mark.parametrize("age", [8, 17, None])
def test_real_intervention_builder_never_exposes_adult_regimens_to_children_or_unknown_age(age):
    from modules.gut_barrier.interventions import build_interventions

    assessed = assessment()
    assessed["subject"]["age"] = age
    plans = build_interventions(assessed)
    report = build_report(assessed, plans, report_id="REAL-AGE-SAFETY")
    body = visible_source(render_report_html(report))
    for adult_regimen in ["25–35", "15–25 g/day", "800–2000", "0.3–0.5 g/kg", "150–300", "至少7小时", "5 g、每日3次"]:
        assert adult_regimen not in body
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
    html = render_report_html(report)
    assert source == before
    assert len(html.encode()) < 400000 and len(encoded.encode()) < 400000
    assert encoded.count('"microbiomeSources"') == 1
    assert all(f'"{key}"' not in encoded for key in ("rawData", "summaryText", "modelMetadata"))
    assert "RAW-SPECIES-PAYLOAD" not in html and "SOURCE-REGIMEN" not in html
    assert report["assessment"]["markers"] == source["markers"]
    assert report["assessment"]["barrier"] == source["barrier"]
    assert report["subject"]["context"]["medications"] == ["输入用药"]
    assert report["assessment"]["context"]["clinicalFindings"] == source["context"]["clinicalFindings"]
    assert "输入腹胀" in html and "原胃镜描述" in html
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
    body = visible_source(render_report_html(report))

    assert report["assessment"]["barrier"]["pattern"] == "---"
    assert "inflammation" not in report["assessment"]
    cardiovascular = next(row for row in report["assessment"]["risks"] if row["code"] == "cardiovascular")
    assert len(cardiovascular["basis"]) == 4
    assert sum("原报告标记升高" in item for item in cardiovascular["basis"]) == 4
    assert not any("mmol" in item for item in cardiovascular["basis"])

    assert ">3.0 mg/L" not in report["summary"]["text"] and "&gt;3.0 mg/L" not in body
    assert all(value in body for value in ("3.70", "6.48", "6.09", "3.44"))
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
    body = visible_source(render_report_html(report))

    assert all(item["code"] != "HS_CRP" for item in report["assessment"]["markers"])
    assert assessed["barrier"]["score"] is None
    assert "&gt;3 mg/L" not in body and "&gt;0.3 mg/L" not in body


def figure_source(body, number):
    return body.split(f'id="figure-{number}"', 1)[1].split("</figure>", 1)[0]


@pytest.mark.parametrize("builder", [reordered_chapter_fixture, document])
def test_new_order_has_complete_numbered_toc_and_stable_subsection_anchors(builder):
    report = builder()
    before = deepcopy(report)
    body = visible_source(render_report_html(report))
    assert chapter_layout(report["sections"]) == "reordered"
    assert report == before
    assert re.findall(r'<section class="report-section" id="([^"]+)"', body) == list(CHAPTER_IDS)
    expected = [(section, chapter, size) for section, chapter, size in zip(CHAPTER_IDS, range(1, 6), [4 if builder is document else 5, 7, 6, 6, 5])]
    for section, chapter, size in expected:
        for subsection in range(1, size + 1):
            anchor = f"{section}-{chapter}-{subsection}"
            assert body.count(f'href="#{anchor}"') == 1
            assert body.count(f'id="{anchor}"') == 1
    parsed = Parsed(body)
    anchors = [attrs["id"] for _tag, attrs in parsed.tags if "id" in attrs]
    assert len(anchors) == len(set(anchors))
    assert body.count('class="toc-subsections"') == 5
    assert len(re.findall(r'href="#(?:overview|disease_risks|repair_plan|education|appendix)-\d-\d+"', body)) == (28 if builder is document else 29)

def test_duplicate_subsection_blocks_have_one_toc_entry_and_one_anchor():
    report = reordered_chapter_fixture()
    report["sections"][0]["blocks"].insert(2, {"kind": "paragraph", "title": "1.2 检测指标补充信息", "text": "补充说明"})
    body = visible_source(render_report_html(report))
    assert body.count('href="#overview-1-2"') == 1
    assert body.count('id="overview-1-2"') == 1
    assert "补充说明" in body
    toc = body.split('aria-label="报告目录"', 1)[1].split("</nav>", 1)[0]
    assert "1.2 检测指标分项阐述" in toc and "1.2 检测指标补充信息" not in toc

def test_new_layout_figures_follow_text_order_and_actual_seven_systems():
    report = reordered_chapter_fixture()
    public = project_public_report(report)
    rendered = render_report_html(public)
    body = visible_source(rendered)
    assert re.findall(r'<figure class="report-figure" id="figure-(\d+)"', body) == [str(i) for i in range(1, 9)]
    assert "七系统疾病风险评估总览" in figure_source(body, 3)
    assert "八系统" not in body and "泌尿" not in body
    assert figure_source(body, 3).count('class="risk-unassessed"') == 21
    assert "肠道修复五原则" in figure_source(body, 4)
    assert "非受检者影像" in figure_source(body, 5)
    assert "屏障与炎症信号的联系" in figure_source(body, 6)
    assert figure_source(body, 7).count('class="axis-node"') == 7
    assert '<div class="axis-node">肿瘤</div>' in figure_source(body, 7)
    assert "不是受检者年龄趋势图" in figure_source(body, 8)
    data = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', rendered, re.S).group(1)
    assert json.loads(data) == public
    assert all(block["kind"] != "figure" and "anchor" not in block for section in public["sections"] for block in section["blocks"])

def test_frozen_legacy_layout_retains_original_chapter_and_figure_numbers():
    report = five_chapter_fixture()
    before = deepcopy(report)
    body = visible_source(render_report_html(report))
    assert chapter_layout(report["sections"]) == "legacy"
    assert re.findall(r'<section class="report-section" id="([^"]+)"', body) == list(LEGACY_CHAPTER_IDS)
    assert "非受检者影像" in figure_source(body, 3)
    assert "八系统疾病风险评估总览" in figure_source(body, 7)
    assert "肠道修复五原则" in figure_source(body, 8)
    assert 'href="#education-2-1"' in body and 'href="#disease_risks-3-8"' in body
    assert report == before

def test_five_chapters_bind_eight_local_figures_and_full_navigation_without_new_public_kind():
    report = five_chapter_fixture()
    public = project_public_report(report)
    body = visible_source(render_report_html(public))
    assert body == visible_source(render_report_html(report))
    assert re.findall(r'<figure class="report-figure" id="figure-(\d+)"', body) == ["1", "2", "3", "4", "5", "6", "7", "8"]
    assert "报告目录" in body and '<div class="report-section cover">' in body
    for section_id in CHAPTER_IDS:
        assert body.count(f'href="#{section_id}"') == 2
        assert body.count(f'<section class="report-section" id="{section_id}"') == 1
    assert all(block["kind"] != "figure" for section in public["sections"] for block in section["blocks"])
    assert "src=\"http" not in body
    assert "非受检者影像" in figure_source(body, 3)
    assert "不是受检者年龄趋势图" in figure_source(body, 6)

@pytest.mark.parametrize("operator,escaped,arrow", [
    (">", "&gt;", "→"), (">=", "&gt;=", "→"),
    ("<", "&lt;", "←"), ("<=", "&lt;=", "←"), ("≥", "≥", "→"), ("≤", "≤", "←"),
])
def test_range_marker_draws_boundary_arrow_not_false_exact_result_point(operator, escaped, arrow):
    body = visible_source(render_report_html(five_chapter_fixture(crp=f"{operator}3.0 mg/L")))
    plot = figure_source(body, 1)
    crp = plot.split("<b>hs-CRP</b>", 1)[1]
    assert f"{escaped}3.0 mg/L" in crp and 'class="range-bound"' in crp
    assert f'>{arrow}</span>' in crp
    assert 'class="result-point"' not in crp
    assert "原有评价A" in plot and "范围待核实" in plot
    assert "style=\"left:50.0000%\"" in figure_source(body, 2)
    assert "原评估等级，未重新计算" in figure_source(body, 2)

def test_axis_uses_clean_system_names_including_tumor_title():
    report = five_chapter_fixture()
    report["sections"][2]["blocks"][-1]["title"] = "3.8 肿瘤风险评估"
    plot = figure_source(visible_source(render_report_html(report)), 5)
    assert '<div class="axis-node">肿瘤</div>' in plot
    assert "风险评估" not in plot

@pytest.mark.parametrize("score", [None, False, "7.5", -1, 16])
def test_unknown_or_invalid_ibfc_does_not_draw_zero_or_invent_a_grade(score):
    plot = figure_source(visible_source(render_report_html(five_chapter_fixture(score=score))), 2)
    assert 'class="result-point"' not in plot
    assert "未获得可定位的总分" in plot
    assert "原评估等级，未重新计算" in plot

def test_actual_zero_ibfc_remains_a_zero_point():
    plot = figure_source(visible_source(render_report_html(five_chapter_fixture(score=0))), 2)
    assert 'class="result-point" style="left:0.0000%"' in plot
    assert "0 / 15" in plot

@pytest.mark.parametrize("index", [None, False, "7.8", -1, 11])
def test_unknown_risk_index_is_not_a_zero_length_bar_or_a_percentage(index):
    plot = figure_source(visible_source(render_report_html(five_chapter_fixture(risk_index=index))), 7)
    assert plot.count('class="risk-unassessed"') == 22
    assert 'class="risk-track"' not in plot and 'class="risk-fill"' not in plot
    assert "未评估项目不表示零风险" in plot

@pytest.mark.parametrize("index", [0, 2.5, 10])
def test_numeric_risk_index_uses_same_frozen_values_as_the_table(index):
    report = five_chapter_fixture(risk_index=index)
    plot = figure_source(visible_source(render_report_html(report)), 7)
    assert plot.count(f'class="risk-fill" style="width:{index * 10:.4f}%"') == 22
    assert plot.count(f'<b class="risk-number">{index:g} / 10</b>') == 22
    assert "原评估等级" in plot
    assert report["sections"][2]["blocks"][0]["rows"][0][1] == index

def test_graphs_do_not_infer_quantities_from_prose_or_use_mismatched_units():
    report = five_chapter_fixture()
    table = report["sections"][0]["blocks"][0]
    table["rows"][0][1] = "结果大约在二十左右，可能升高"
    table["rows"][1][1] = "13 mg/L"  # reference remains U/L: no renderer conversion.
    table["rows"][2][1] = None
    plot = figure_source(visible_source(render_report_html(report)), 1)
    assert plot.count('class="plot-unavailable"') == 3
    assert 'class="result-point"' not in plot

def test_legacy_layout_and_unknown_bindings_are_not_reinterpreted_as_new_figures():
    report = five_chapter_fixture()
    report["sections"][0]["id"] = "old-overview"
    body = visible_source(render_report_html(report))
    assert '<figure class="report-figure"' not in body and 'class="report-section cover"' not in body
    assert "经审核正文6" in body
    report["sections"][0]["id"] = "overview"
    report["sections"][0]["blocks"][0]["title"] = "其他四指标表"
    body = visible_source(render_report_html(report))
    assert 'id="figure-1"' not in body

def test_five_chapter_graph_content_is_escaped_like_other_public_blocks():
    report = five_chapter_fixture()
    attack = '<img src=x onerror="alert(1)">'
    report["sections"][0]["blocks"][0]["rows"][0][0] = attack
    report["sections"][2]["blocks"][0]["rows"][0][0] = attack
    report["sections"][2]["blocks"][0]["title"] = attack
    body = visible_source(render_report_html(report))
    assert not any(tag == "img" or "onerror" in attrs for tag, attrs in Parsed(body).tags)
    assert "&lt;img" in figure_source(body, 1)
    assert "&lt;img" in figure_source(body, 5) and "&lt;img" in figure_source(body, 7)
