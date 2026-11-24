"""Attention-only display semantics preserve dynamic evidence and old reports."""

from copy import deepcopy
from html import escape

import json

import re

import pytest

from adapters.gut_barrier.frontend_template import expand_dynamic_report, load_static_template

from adapters.gut_barrier.graphics import after_block, before_section

from adapters.gut_barrier.rendering import render_report_html

from modules.gut_barrier.publication import project_callback_report, project_public_report
from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE

from tests.unit.gut_barrier.preview.support import document, visible_source

RISK_COLUMNS = ["疾病", "风险指数", "风险等级", "机制关联"]

def section(report, identifier):
    return next(section for section in report["sections"] if section["id"] == identifier)

def table(report):
    return next(block for block in section(report, "disease_risks")["blocks"] if block["kind"] == "table")

def attention_report(*, current_row=False):
    report = document()
    report["assessment"]["diseaseAssessment"] = {
        "mode": "data_driven_attention", "references": [{
            "citation": "[评估1] 已核对评估依据", "url": "https://example.org/assessment",
            "year": 2026, "publisher": "测试来源", "doi": None, "location": None,
        }],
    }
    rows = [["非酒精性脂肪性肝病", 5, "加强关注", "按自述体重资料计算，复核体格测量与肝功能。[评估1]"],
            ["新增关注方向", 7, "优先评估", "已有资料支持，结合实际情况补充评估。[评估1]"],
            ["已注册模型项目", 0, "低风险", "仅按该已注册模型独立解释，不混合排序。"]]
    if current_row:
        rows[0] = {"cells": rows[0], "current": True}
    section(report, "disease_risks")["blocks"] = [
        deepcopy(load_static_template()["diseaseIntroduction"]),
        {"kind": "callout", "title": "本次关注指数", "tone": "caution",
         "text": "本次采用未验证的健康关注排序；不是患病概率。"},
        {"kind": "table", "title": "2.1 消化系统疾病风险评估", "columns": RISK_COLUMNS, "rows": rows},
    ]
    return report

@pytest.mark.parametrize("projector", [lambda report: report, project_public_report, project_callback_report])
def test_attention_preview_uses_unvalidated_attention_copy_and_keeps_data_unchanged(projector, historical_templates):
    report = projector(attention_report())
    before = deepcopy(report)
    asset = deepcopy(load_static_template())
    expanded = expand_dynamic_report(report)
    disease = section(expanded, "disease_risks")
    intro = next(block for block in disease["blocks"] if block["kind"] == "paragraph" and not block["title"])
    assert "尚未经过临床验证" in intro["text"]
    assert "不是患病概率" in intro["text"] and "不能用于诊断、治疗决策" in intro["text"]
    assert load_static_template()["diseaseIntroduction"] not in disease["blocks"]
    scales = [block for block in section(expanded, "appendix")["blocks"] if block.get("title") == "5.3 风险指数标尺说明"]
    assert len(scales) == 1
    assert "1–3分为基础关注，4–6分为加强关注，7–10分为优先评估" in scales[0]["text"]
    assert "IBFC" in scales[0]["text"] and "不是同一标尺" in scales[0]["text"]
    assert "已注册模型数值（包括0分）" in scales[0]["text"]
    assert table(expanded) == table(report)
    assert expand_dynamic_report(expanded) == expanded
    assert load_static_template() == asset and report == before
    html = render_report_html(report)
    embedded = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S)[1]
    assert json.loads(embedded) == report
    assert "本次健康关注指数" in visible_source(html)

def test_attention_mode_never_fills_unknown_basis_from_legacy_static_mechanism(historical_templates):
    report = project_callback_report(attention_report())
    table(report)["rows"][0][3] = None
    table(report)["rows"].append(["未预置病种", 2, "基础关注", "独立个体依据"])
    expanded = expand_dynamic_report(report)
    assert table(expanded)["rows"] == table(report)["rows"]
    assert len(table(expanded)["rows"]) == 4

def test_attention_plot_only_draws_attention_scores_not_registered_model_values(historical_templates):
    report = expand_dynamic_report(project_callback_report(attention_report()))
    html = before_section("disease_risks", report["sections"])
    assert "本次健康关注指数" in html and "尚未经过临床验证" in html
    assert "不是患病概率" in html and "不能用于诊断、治疗决策或临床紧急分诊" in html
    assert "与IBFC不是同一标尺" in html
    assert "健康关注指数 5 / 10" in html and "健康关注指数 7 / 10" in html
    assert html.count('class="risk-fill"') == 2
    assert "已注册模型项目" not in html and "低风险" not in html
    assert table(report)["rows"][2][1] == 0

def test_registered_model_only_new_report_does_not_draw_a_misleading_attention_graph(historical_templates):
    report = expand_dynamic_report(project_callback_report(attention_report()))
    table(report)["rows"] = [table(report)["rows"][2]]
    assert before_section("disease_risks", report["sections"]) == ""
    assert table(report)["rows"][0][1] == 0

def test_registered_model_with_coincident_attention_label_is_still_not_plotted(historical_templates):
    report = expand_dynamic_report(project_callback_report(attention_report()))
    model_row = table(report)["rows"][2]
    model_row[2] = "加强关注"
    model_row[3] = "本项数值来自独立的已配置模型，不与本章自定义关注指数比较。"
    html = before_section("disease_risks", report["sections"])
    assert html.count('class="risk-fill"') == 2
    assert "已注册模型项目" not in html
    assert model_row[1] == 0

def test_new_attention_citations_resolve_after_projection_and_template_expansion(historical_templates):
    report = project_callback_report(attention_report())
    visible = visible_source(render_report_html(report))
    targets = set(re.findall(r'href="#(reference-[^"]+)"', visible))
    anchors = set(re.findall(r'id="(reference-[^"]+)"', visible))
    assert targets <= anchors
    assert re.search(r'<a class="citation" href="#reference-[^"]+"[^>]*>\[评估1\]</a>', visible)

def test_old_dynamic_report_retains_pinned_introduction_scale_and_graph_semantics(historical_templates):
    report = project_callback_report(document())
    before = deepcopy(report)
    expanded = expand_dynamic_report(report)
    asset = load_static_template()
    assert asset["diseaseIntroduction"] in section(expanded, "disease_risks")["blocks"]
    old_scale = next(block for block in asset["appendixBlocks"] if block.get("title") == "5.3 风险指数标尺说明")
    assert old_scale in section(expanded, "appendix")["blocks"]
    plot = before_section("disease_risks", expanded["sections"])
    assert "健康关注指数" not in plot
    assert "系统疾病风险评估总览" in plot
    assert report == before

def test_callout_with_different_title_or_hidden_disease_section_does_not_change_old_semantics(historical_templates):
    report = project_callback_report(document())
    disease = section(report, "disease_risks")
    disease["blocks"].insert(0, {"kind": "callout", "title": "其他提示", "tone": None, "text": "本次关注指数"})
    expanded = expand_dynamic_report(report)
    assert load_static_template()["diseaseIntroduction"] in section(expanded, "disease_risks")["blocks"]
    disease["blocks"][0]["title"] = "本次关注指数"
    disease["visible"] = False
    expanded = expand_dynamic_report(report)
    old_scale = next(block for block in load_static_template()["appendixBlocks"] if block.get("title") == "5.3 风险指数标尺说明")
    assert old_scale in section(expanded, "appendix")["blocks"]

@pytest.mark.asyncio
async def test_current_pipeline_dynamic_basis_survives_without_numeric_attention_bars():
    from modules.gut_barrier.pipeline import GutBarrierPipeline
    from modules.gut_barrier.schemas import GutBarrierRequest
    from tests.unit.gut_barrier.final_input_fixture import final_input

    payload = final_input()
    payload["fileList"] = []
    # With no adapters configured this is a fully offline pipeline check.
    report, _ = await GutBarrierPipeline().generate(
        GutBarrierRequest.model_validate(payload), report_id="gbr_attention_preview", evaluation_date="2026-09-24")
    assert report["assessment"]["diseaseAssessment"]["mode"] == "data_driven_attention"
    dynamic = project_callback_report(report)
    rows = [row for block in section(dynamic, "disease_risks")["blocks"]
            if block.get("columns") == RISK_COLUMNS for row in block["rows"]]
    from tests.unit.gut_barrier.template_risk_assertions import assert_template_risk_contract
    _, chart, tables = assert_template_risk_contract(dynamic, report["assessment"])
    assert len(chart["rows"]) == 23
    # Questionnaire symptoms and body measurements do not add risk rows.
    assert len(rows) == 23 and report["assessment"]["diseaseRisks"] == []
    assert all(1 <= row[1] <= 10 and row[2] in {"低", "中", "高"} and row[3]
               for row in rows if row[1] is not None)
    assert all(row[1] is not None and row[2] is not None for row in rows)
    assert all("本次依据：" not in row[3] and "随访重点：" not in row[3] for row in rows)
    assert all(row[3].count("。") == 1 for row in rows)
    assert project_callback_report(dynamic) == dynamic
    html = render_report_html(dynamic, template_version=LATEST_DISPLAY_TEMPLATE)
    embedded = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S)[1]
    assert json.loads(embedded) == dynamic
    expanded = expand_dynamic_report(dynamic, template_version=LATEST_DISPLAY_TEMPLATE)
    assert before_section("disease_risks", expanded["sections"], template_version=LATEST_DISPLAY_TEMPLATE) == ""
    expanded_disease = section(expanded, "disease_risks")
    overview = next(block for block in expanded_disease["blocks"] if block.get("title") == chart["title"])
    assert overview == chart
    plot = after_block("disease_risks", overview, expanded["sections"], template_version=LATEST_DISPLAY_TEMPLATE)
    assert chart["title"] in plot and "暂无可量化的疾病风险结果。" not in plot
    assert 'class="risk-fill"' not in plot and plot.count('class="risk-number"') == len(chart["rows"])
    assert 'class="risk-plot-row"' not in plot
    body = visible_source(html)
    disease_html = re.search(r'<section class="report-section" id="disease_risks">(.*?)</section>', body, re.S)[1]
    figures = re.findall(r'<figure\b.*?</figure>', disease_html, re.S)
    assert len(figures) == 1
    # The renderer may renumber figures, but must consume the exact overview
    # graphic at its block position rather than inserting it before the intro.
    normalize_number = lambda source: re.sub(r'figure-\d+|图 \d+', "FIGURE_NUMBER", source)
    assert normalize_number(figures[0]) == normalize_number(plot)
    introduction = next(block["text"] for block in expanded_disease["blocks"]
                        if block["kind"] == "paragraph" and not block.get("title"))
    assert disease_html.index(escape(introduction)) < disease_html.index(figures[0])
    assert disease_html.index(figures[0]) < disease_html.index(escape(tables[0]["title"]))
    expanded_text = json.dumps(expanded["sections"], ensure_ascii=False)
    for content in (expanded_text, body):
        assert all(title not in content for title in ("风险评估结果说明", "风险指数说明", "状态下限说明"))
        assert "健康关注指数标尺说明" not in content and "关注等级" not in content
    assert "inflammation" not in report["assessment"]

@pytest.mark.asyncio
async def test_empty_new_pipeline_keeps_full_template_unassessed_without_numeric_bars():
    from modules.gut_barrier.pipeline import GutBarrierPipeline
    from modules.gut_barrier.schemas import GutBarrierRequest

    report, _ = await GutBarrierPipeline().generate(GutBarrierRequest.model_validate({
        "userId": "u", "sessionId": "s", "age": 26,
    }), report_id="gbr_empty_attention", evaluation_date="2026-09-24")
    dynamic = project_callback_report(report)
    expanded = expand_dynamic_report(dynamic, template_version=LATEST_DISPLAY_TEMPLATE)
    from tests.unit.gut_barrier.template_risk_assertions import assert_template_risk_contract
    rows, chart, tables = assert_template_risk_contract(dynamic, report["assessment"])
    assert len(rows) == 23 and len(tables) == 8
    assert chart["rows"] == [] and all(row[1:3] == [None, None] for row in rows.values())
    plot = before_section("disease_risks", expanded["sections"], template_version=LATEST_DISPLAY_TEMPLATE)
    assert 'class="risk-plot-row"' not in plot and 'class="risk-fill"' not in plot
    assert "健康关注指数" not in json.dumps(section(expanded, "disease_risks"), ensure_ascii=False)
    assert report["assessment"]["diseaseRisks"] == []
