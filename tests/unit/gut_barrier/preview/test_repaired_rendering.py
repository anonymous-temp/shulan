"""Independent presentation acceptance checks; never derive expected clinical scores."""
from copy import deepcopy
import json
import re

import pytest

from adapters.gut_barrier.rendering import render_report_html
from tests.unit.gut_barrier.preview.support import (
    Parsed, figure_source, reordered_chapter_fixture, visible_source,
)


def _report():
    report = reordered_chapter_fixture()
    overview = report["sections"][0]["blocks"]
    overview.extend([
        {"kind": "facts", "title": "受检者信息", "items": [
            {"label": "姓名", "value": "模拟受检者甲"}, {"label": "性别", "value": "女"},
            {"label": "年龄", "value": "50 岁"}, {"label": "检测日期", "value": None},
            {"label": "报告日期", "value": "2026-09-24"}]},
        {"kind": "table", "title": "本次检测方法与参考信息", "columns": ["检测指标", "本次检测方法", "实验室原始参考信息"],
         "rows": [["D-乳酸", None, []], ["DAO", "", None], ["LPS", None, None], ["hs-CRP", None, None]]},
        {"kind": "table", "title": "本次样本与质控记录", "columns": ["校验项目", "记录"],
         "rows": [["样本类型", None], ["样本质量", None], ["检测人", None]]},
        {"kind": "bands", "title": "屏障评分分级", "items": [
            {"range": span, "label": label, "selected": False}
            for span, label in [("1–2分", "I 级（亚健康）"), ("＞2–4.5分", "II 级（轻度）"),
                                ("＞4.5–6分", "III 级（中度）"), ("＞6–8分", "IV 级（重度）"), ("＞8分", "V 级（极重度）")]]},
        {"kind": "bands", "title": "hs-CRP 项目功能分层", "items": [
            {"range": span, "label": label, "selected": False}
            for span, label in [("≤0.5 mg/L", "理想范围"), ("0.5–1 mg/L", "轻度升高"),
                                ("1–3 mg/L", "中度升高"), ("＞3 mg/L", "显著升高")]]},
    ])
    appendix = report["sections"][-1]["blocks"]
    appendix[-1]["items"] = [{"citation": f"原文献{index}", "url": None} for index in range(1, 15)]
    appendix.append({"kind": "references", "title": "营养与微生态干预补充依据", "items": [
        {"citation": "补充文献一", "url": "https://example.org/one"}, {"title": "历史标题兼容", "link": "https://example.org/two"}]})
    return report


def _block(report, title):
    return next(block for section in report["sections"] for block in section["blocks"] if block.get("title") == title)


def test_empty_auxiliary_tables_are_compacted_without_changing_complete_json():
    report = _report()
    before = deepcopy(report)
    rendered = render_report_html(report)
    body = visible_source(rendered)
    assert body.count('class="empty auxiliary-empty"') == 2
    assert "本次资料未提供检测方法及实验室原始参考信息" in body
    assert "本次资料未提供样本及质控记录" in body
    assert "检测人</td>" not in body
    assert len(_block(report, "1.2 检测指标分项阐述")["rows"]) == 4
    assert figure_source(body, 3).count('class="risk-unassessed"') == 21
    embedded = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', rendered, re.S).group(1)
    assert json.loads(embedded) == before == report


@pytest.mark.parametrize("value", [0, False, "0", [0], [False], "合格"])
def test_real_zero_and_false_keep_auxiliary_table_expanded(value):
    report = _report()
    _block(report, "本次样本与质控记录")["rows"][1][1] = value
    body = visible_source(render_report_html(report))
    assert body.count('class="empty auxiliary-empty"') == 1
    assert 'data-label="校验项目">检测人</td>' in body
    assert '<span class="empty">未提供</span>' in body
    assert f'data-label="记录">{value[0] if isinstance(value, list) else value}</td>' in body


def test_empty_core_tables_are_not_suppressed_as_auxiliary_tables():
    report = _report()
    markers = _block(report, "1.2 检测指标分项阐述")
    for row in markers["rows"]:
        row[1] = row[4] = None
    body = visible_source(render_report_html(report))
    assert 'data-label="检测指标">D-乳酸</td>' in body
    assert 'data-label="疾病">疾病1-1</td>' in body


def test_two_bibliographies_have_unique_anchors_and_resolve_numeric_ranges():
    report = _report()
    report["sections"][0]["blocks"].append({"kind": "paragraph", "text": "原论述[1,4–7]；补充[补1,2]；未知[99]；部分[2,99]；逆向[7–4]。"})
    body = visible_source(render_report_html(report))
    ids = [attributes["id"] for _, attributes in Parsed(body).tags if "id" in attributes]
    assert len(ids) == len(set(ids))
    for number in (1, 2, 4, 5, 6, 7):
        assert f'href="#reference-primary-{number}"' in body
    for number in (1, 2):
        assert f'href="#reference-supplement-1-{number}"' in body
    assert 'href="#reference-primary-99"' not in body
    assert "未知[99]" in body and "逆向[7–4]" in body
    assert "历史标题兼容" in body and 'href="https://example.org/two"' in body
    assert body.count('class="reference-index">[1]</span>') == 1
    assert body.count('class="reference-index">[补1]</span>') == 1


def test_reference_text_is_escaped_and_unresolved_numbers_remain_plain():
    report = _report()
    report["sections"][0]["blocks"].append({"kind": "paragraph", "text": '<img src=x onerror="attack()">[1] [1–1000000]'})
    body = visible_source(render_report_html(report))
    assert "&lt;img" in body and "[1–1000000]" in body
    assert not any(tag == "img" or "onerror" in attributes for tag, attributes in Parsed(body).tags)


def test_same_title_reference_blocks_and_legacy_name_alias_have_unique_anchors():
    report = _report()
    appendix = report["sections"][-1]["blocks"]
    appendix[-1]["title"] = "5.5 参考文献"
    appendix[-1]["items"] = [{"name": "旧格式来源名称", "citation": None, "url": None},
                              {"citation": False}, {"citation": 0}]
    body = visible_source(render_report_html(report))
    ids = [attributes["id"] for _, attributes in Parsed(body).tags if "id" in attributes]
    assert len(ids) == len(set(ids))
    assert 'id="reference-primary-1"' in body and 'id="reference-supplement-1-1"' in body
    assert "旧格式来源名称" in body and "[补2]</span>False" in body and "[补3]</span>0" in body


def test_citation_digit_limit_prevents_expansion_or_integer_conversion_failure():
    report = _report()
    report["sections"][0]["blocks"].append({"kind": "paragraph", "text": "[" + "9" * 6000 + "]"})
    assert "9" * 6000 in visible_source(render_report_html(report))


def test_cover_reuses_known_patient_facts_not_template_identity():
    report = _report()
    body = visible_source(render_report_html(report))
    cover = body.split('<div class="report-section cover">', 1)[1].split('<nav class="toc"', 1)[0]
    assert all(value in cover for value in ("模拟受检者甲", "女", "50 岁", "2026-09-24"))
    assert "检测日期" not in cover and "张学斌" not in body


def test_indicator_color_bands_use_existing_boundaries_without_reclassifying(monkeypatch):
    from modules.gut_barrier import rules
    monkeypatch.setattr(rules, "score_marker", lambda *args: pytest.fail("renderer must not score"))
    monkeypatch.setattr(rules, "grade_score", lambda *args: pytest.fail("renderer must not grade"))
    report = _report()
    markers = _block(report, "1.2 检测指标分项阐述")
    markers["rows"][0][1:5] = ["14 mg/L", "＜14 mg/L", "边界待复核", None]
    plot = figure_source(visible_source(render_report_html(report)), 1)
    assert plot.count('class="scale-segment"') == 19
    for threshold in ("14–17", "17–22", "22–30", "8.5–11", "11–16", "16–20", "18–22", "22–28", "28–35"):
        assert threshold in plot
    assert "边界待复核" in plot and "中度升高" in plot
    assert 'class="scale-legend-item selected"' not in plot
    crp = plot.split("<b>hs-CRP</b>", 1)[1]
    assert 'class="range-bound"' in crp and 'class="result-point"' not in crp


def test_ibfc_scale_consumes_public_ranges_and_original_grade_without_assigning_boundary():
    report = _report()
    cards = _block(report, "肠道屏障功能评分（IBFC）")
    cards["items"][0]["value"] = 4.5
    cards["items"][1]["value"] = "分级待复核"
    plot = figure_source(visible_source(render_report_html(report)), 2)
    assert plot.count('class="scale-segment"') == 5
    assert 'class="result-point" style="left:30.0000%"' in plot
    assert "分级待复核" in plot and "4.5 / 15" in plot
    assert all(f">{boundary}</span>" in plot for boundary in ("0", "1", "2", "4.5", "6", "8", "15"))
    assert 'class="scale-legend-item selected"' not in plot


def test_eight_figures_include_required_mechanisms_without_fabricated_age_quantities():
    body = visible_source(render_report_html(_report()))
    assert re.findall(r'<figure class="report-figure" id="figure-(\d+)"', body) == [str(i) for i in range(1, 9)]
    for content in ("去除", "营养补充", "PFMT", "MDF", "生活方式", "原料 · 能量 · 信号 · 抗炎"):
        assert content in figure_source(body, 4)
    for content in ("黏液", "紧密连接", "上皮", "组织侧", "免疫细胞"):
        assert content in figure_source(body, 5)
    for content in ("LPS", "TLR4", "NF-κB", "IL-6", "CRP", "放大环路"):
        assert content in figure_source(body, 6)
    assert figure_source(body, 7).count('class="axis-route"') == 7
    age = figure_source(body, 8)
    assert "定性研究概念示意" in age and "个体差异" in age
    assert "<svg" not in age and "<polyline" not in age and not re.search(r"\d+倍", age)


def test_static_css_contains_narrow_screen_and_print_resilience():
    body = render_report_html(_report())
    assert "@media(max-width:540px)" in body and "@media print" in body
    assert ".principle-grid{grid-template-columns:repeat(2,minmax(0,1fr))}" in body
    assert ".age-concept{grid-template-columns:1fr;gap:6px}" in body
    assert "thead{display:table-header-group}" in body


def test_risk_plot_retains_callback_numeric_precision_without_reclassification():
    report = _report()
    risk = _block(report, "2.1 消化系统疾病风险评估")
    risk["rows"][0][1:3] = [4.123456789, "模型原有分级"]
    plot = figure_source(visible_source(render_report_html(report)), 3)
    assert '<b class="risk-number">4.123456789 / 10</b>' in plot
    assert "模型原有分级" in plot
