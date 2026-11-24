"""Current rendering consumes frozen risk results; older HTML keeps its layout."""
from copy import deepcopy
from hashlib import sha256
import html as html_module
import json
import re

import pytest

from adapters.gut_barrier.frontend_template import load_static_template, project_display_report
from adapters.gut_barrier.graphics import _model_risk_plot, after_block, before_section
from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import (
    ORDERED_REFERENCE_DISPLAY_TEMPLATE, LATEST_DISPLAY_TEMPLATE,
)
from modules.gut_barrier.reference_order import publish_ordered_references
from modules.gut_barrier.template_risk_review import RISK_OVERVIEW_COLUMNS, RISK_OVERVIEW_TITLE
from tests.unit.gut_barrier.preview.archive_support import historical_archive
from tests.unit.gut_barrier.preview.support import figure_source, visible_source


def _section(identifier, title, blocks):
    return {"id": identifier, "title": title, "subtitle": None, "visible": True, "blocks": blocks}


def _fixture(*, score=2, share="13.33%", version=LATEST_DISPLAY_TEMPLATE, archive=None):
    """Synthetic frozen wire data, including its same-release citation plan."""
    modern = version == LATEST_DISPLAY_TEMPLATE
    overview = {"kind": "table", "title": RISK_OVERVIEW_TITLE if modern else "本次健康关注指数总览",
                "columns": RISK_OVERVIEW_COLUMNS[:] if modern else ["系统", "疾病", "健康关注指数", "关注等级"],
                "rows": [["消化系统", "合成模型零值", 0, "模型原等级"]] if modern else
                        [["消化系统", "合成定性随访", None, "随访关注（定性）"]]}
    report = {"schemaVersion": "gut-barrier-public.v2", "meta": {"title": "合成展示回归", "reportId": "synthetic-v13"},
        "sections": [
            _section("overview", "第一章 检测综述", [
                {"kind": "paragraph", "title": "1.1 综合评估指数（IBFC · 肠屏障功能评分）", "text": "合成综合评估正文。"},
                {"kind": "cards", "title": "肠道屏障功能评分（IBFC）", "items": [
                    {"label": "IBFC评分", "value": score, "detail": None},
                    {"label": "损伤等级", "value": "I 级（亚健康）", "detail": None},
                    {"label": "总体阶段", "value": "疾病人群", "detail": None},
                    {"label": "评分占满分比例", "value": share, "detail": "IBFC 满分为 15 分"},
                ]},
            ]),
            _section("disease_risks", "第二章 相关疾病风险评估", [
                {"kind": "paragraph", "title": None, "text": "合成疾病评估正文。"}, overview,
                {"kind": "table", "title": "2.1 消化系统疾病风险评估",
                 "columns": ["疾病", "风险指数", "风险等级", "机制关联"], "rows": [
                     ["合成定性随访", None, "随访关注（定性）", "合成机制。"],
                     ["合成资料不足", None, "依据不足", "待核实机制。"],
                     {"cells": ["合成模型零值", 0, "模型原等级", "模型机制。"], "current": False},
                     ["合成布尔值", True, "保留原始值", "不转换为数字。"],
                 ]},
            ]),
            _section("appendix", "第四章 附录", []),
        ]}
    asset = load_static_template(version, archive_directory=archive)
    if modern:
        publish_ordered_references(
            report, deepcopy(report), asset, reference_limit=15,
            pinned_references=asset["referencePolicy"]["pinnedPrefix"],
            allow_fixed_budget_omission=True,
        )
    else:
        publish_ordered_references(report, deepcopy(report), asset)
    return report


def _html(report, version=LATEST_DISPLAY_TEMPLATE, archive=None):
    return render_report_html(report, template_version=version, template_archive_dir=archive)


def _embedded(html):
    return json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.S)[1])


@pytest.mark.parametrize("title", ["风险指数说明", "状态下限说明", "风险评估资料提示"])
def test_frozen_risk_explanation_is_hidden_without_mutating_v13_asset_or_callback(title):
    from modules.gut_barrier.publication import project_callback_report

    report = _fixture()
    notice = {"kind": "callout", "title": title, "tone": "default",
              "text": "冻结的风险计算说明正文。"}
    reminder = {"kind": "callout", "title": "便血就医提示", "tone": "caution",
                "text": "出现便血应及时就医。"}
    report["sections"][1]["blocks"].insert(1, notice)
    report["sections"][1]["blocks"].append(reminder)
    before = deepcopy(report)
    asset = load_static_template(LATEST_DISPLAY_TEMPLATE)
    view = project_display_report(report, template_version=LATEST_DISPLAY_TEMPLATE)
    view_text = json.dumps(view["sections"], ensure_ascii=False)
    html = _html(report)
    for visible in (view_text, visible_source(html)):
        assert all(value not in visible for value in (
            "风险指数说明", "状态下限说明", "风险评估结果说明", "风险评估资料提示", notice["text"],
        ))
        assert reminder["title"] in visible and reminder["text"] in visible
    assert load_static_template(LATEST_DISPLAY_TEMPLATE) == asset
    assert project_callback_report(report) == view["sourceReport"] == before == report
    assert _embedded(html) == before


def test_v13_without_risk_notice_does_not_reintroduce_static_appendix_explanation():
    report = _fixture()
    before = deepcopy(report)
    asset = load_static_template(LATEST_DISPLAY_TEMPLATE)
    expected = next(block for block in asset["appendixBlocks"] if block.get("title") == "风险评估结果说明")
    view = project_display_report(report, template_version=LATEST_DISPLAY_TEMPLATE)
    appendix = next(section for section in view["sections"] if section["id"] == "appendix")
    assert not any(block.get("title") == expected["title"] for block in appendix["blocks"])
    body = visible_source(_html(report))
    assert expected["title"] not in body and expected["text"] not in body
    assert load_static_template(LATEST_DISPLAY_TEMPLATE) == asset
    assert view["sourceReport"] == before == report


@pytest.mark.parametrize("score,share", [(0, "0.00%"), (2, "13.33%"), (15, "100.00%"), (None, None), (True, None)])
def test_current_figure_header_uses_existing_percentage_and_fixed_stage_concepts(score, share):
    report = _fixture(score=score, share=share)
    before = deepcopy(report)
    html = _html(report)
    figure = figure_source(visible_source(html), 1)
    header = figure.split('class="source-stage-band"', 1)[0]
    assert "肠道屏障损伤评估指数（IBFC）" in header and "评分占满分比例" in header
    assert (share or "未提供") in header
    if score is None or score is True:
        assert "未获得评分" in header
    else:
        assert f"<strong>{score}</strong>" in header
    assert re.findall(r'class="source-stage-label">([^<]+)</span>', figure) == ["健康", "亚健康", "疾病"]
    assert 'aria-label="健康、亚健康、疾病三阶段色带"' in figure
    assert not any(label in figure for label in ("健康人群", "亚健康人群", "疾病人群"))
    assert 'class="source-stage-label selected"' not in figure and 'aria-current="true"' not in figure
    assert "I 级" not in figure and "轻度损伤" not in figure
    assert "人群百分位" not in figure and "WHO" not in figure
    assert _embedded(html) == before == report


def test_current_does_not_recompute_percentage_and_duplicate_card_is_not_silently_selected():
    report = _fixture(score=2, share="77.77%")
    html = _html(report)
    figure = figure_source(visible_source(html), 1)
    assert "77.77%" in figure and "13.33%" not in figure
    cards = report["sections"][0]["blocks"][1]
    cards["items"].append({"label": "评分占满分比例", "value": "22.22%", "detail": "重复源数据"})
    figure = figure_source(visible_source(_html(report)), 1)
    assert "未提供" in figure and "77.77%" not in figure and "22.22%" not in figure
    view = project_display_report(report, template_version=LATEST_DISPLAY_TEMPLATE)
    items = next(block["items"] for block in view["sections"][0]["blocks"] if block["kind"] == "cards")
    assert [item["value"] for item in items if item["label"] == "评分占满分比例"] == ["77.77%", "22.22%"]


def test_current_preserves_title_body_graph_order_and_avoids_repeating_percentage_card():
    report = _fixture()
    body = visible_source(_html(report)).split('id="overview">', 1)[1]
    assert body.index("<h3>1.1") < body.index("合成综合评估正文。") < body.index('id="figure-1"')
    assert body.index('id="figure-1"') < body.index('class="cards ibfc-meta-cards"')
    assert body.count('id="figure-1"') == 1
    assert "损伤等级" in body and "I 级（亚健康）" in body and "总体阶段" in body
    assert body.count("评分占满分比例") == 1
    view = project_display_report(report, template_version=LATEST_DISPLAY_TEMPLATE)
    assert view["sourceReport"] == report
    cards = next(block for block in view["sections"][0]["blocks"] if block["kind"] == "cards")
    assert "评分占满分比例" not in [item["label"] for item in cards["items"]]


def test_new_current_null_grade_and_stage_slots_stay_in_callback_but_not_empty_display_cards():
    report = _fixture()
    cards = report["sections"][0]["blocks"][1]
    for item in cards["items"]:
        if item["label"] in {"损伤等级", "总体阶段"}:
            item["value"] = None
            item["detail"] = None
    cards["items"].extend([
        {"label": "受损类型", "value": "合成受损组合", "detail": None},
        {"label": "其他未来卡片", "value": False, "detail": "必须保留的动态信息"},
    ])
    before = deepcopy(report)
    html = _html(report)
    body = visible_source(html)
    overview = re.search(r'<section class="report-section" id="overview">(.*?)</section>', body, re.S)[1]
    assert "损伤等级" not in overview and "总体阶段" not in overview
    assert "合成受损组合" in overview and "必须保留的动态信息" in overview
    view = project_display_report(report, template_version=LATEST_DISPLAY_TEMPLATE)
    display_cards = next(block for block in view["sections"][0]["blocks"] if block["kind"] == "cards")
    assert [item["label"] for item in display_cards["items"]] == ["受损类型", "其他未来卡片"]
    assert _embedded(html) == view["sourceReport"] == before == report


def test_current_does_not_hide_null_grade_with_custom_clinical_detail_or_nonempty_frozen_stage():
    report = _fixture()
    items = report["sections"][0]["blocks"][1]["items"]
    items[1].update(value=None, detail="临床限制原文须保留。")
    before = deepcopy(report)
    body = visible_source(_html(report))
    assert "临床限制原文须保留。" in body
    assert "疾病人群" in body
    assert _embedded(_html(report)) == before == report


def test_current_consumed_score_cards_leave_no_empty_heading_when_user_removed_type_item():
    report = _fixture(score=3.5, share="23.33%")
    items = report["sections"][0]["blocks"][1]["items"]
    for item in items:
        if item["label"] in {"损伤等级", "总体阶段"}:
            item.update(value=None, detail=None)
    assert not any(item["label"] == "受损类型" for item in items)
    before = deepcopy(report)
    html = _html(report)
    body = visible_source(html)
    figure = figure_source(body, 1)
    assert "<strong>3.5</strong>" in figure and "23.33%" in figure
    assert 'class="cards ibfc-meta-cards"' not in body
    assert "<h3>肠道屏障功能评分（IBFC）</h3>" not in body
    overview = re.search(r'<section class="report-section" id="overview">(.*?)</section>', body, re.S)[1]
    assert not any(label in overview for label in ("受损类型", "损伤等级", "总体阶段"))
    view = project_display_report(report, template_version=LATEST_DISPLAY_TEMPLATE)
    assert not any(block["kind"] == "cards" for block in view["sections"][0]["blocks"])
    assert view["sourceReport"] == _embedded(html) == before == report
    assert next(item for item in items if item["label"] == "损伤等级") == {"label": "损伤等级", "value": None, "detail": None}


def test_frozen_current_retains_previous_type_scope_and_fallback_while_new_card_cleanup_stays_view_only():
    from modules.gut_barrier.publication import project_callback_report
    report = _fixture()
    blocks = report["sections"][0]["blocks"]
    blocks[1]["items"].append({"label": "受损类型", "value": "冻结的受损类型", "detail": None})
    blocks.extend([
        {"kind": "callout", "title": "本次评估资料范围", "text": "冻结的本次资料范围。", "tone": "caution"},
        {"kind": "paragraph", "title": None, "text": "当前三项组合尚未确定，结合已获得的单项结果进一步完善分型评价。"},
    ])
    before = deepcopy(report)
    callback = project_callback_report(report)
    html = _html(callback)
    body = visible_source(html)
    assert "冻结的受损类型" in body and "本次评估资料范围" in body
    assert "当前三项组合尚未确定，结合已获得的单项结果进一步完善分型评价。" in body
    assert _embedded(html) == callback == before == report


def test_frozen_v10_keeps_type_scope_and_fallback_and_does_not_adopt_empty_legacy_card_filter():
    from modules.gut_barrier.publication import project_callback_report
    archive = historical_archive()
    report = _fixture(version=ORDERED_REFERENCE_DISPLAY_TEMPLATE, archive=archive)
    blocks = report["sections"][0]["blocks"]
    blocks[1]["items"].append({"label": "受损类型", "value": "v10冻结分型", "detail": None})
    blocks[1]["items"][2]["value"] = None
    blocks.extend([
        {"kind": "callout", "title": "本次评估资料范围", "text": "v10原始范围说明。", "tone": "caution"},
        {"kind": "paragraph", "title": None, "text": "当前三项组合尚未确定，结合已获得的单项结果进一步完善分型评价。"},
    ])
    before = deepcopy(report)
    callback = project_callback_report(report)
    html = _html(callback, ORDERED_REFERENCE_DISPLAY_TEMPLATE, archive)
    body = visible_source(html)
    assert "v10冻结分型" in body and "本次评估资料范围" in body
    assert "当前三项组合尚未确定，结合已获得的单项结果进一步完善分型评价。" in body
    assert "总体阶段" in body and "未评估" in body
    assert _embedded(html) == callback == before == report


def test_current_print_uses_compact_complete_metadata_bands_and_separate_cover():
    report = _fixture()
    cards = report["sections"][0]["blocks"][1]
    cards["items"].append({"label": "受损类型", "value": "合成受损类型", "detail": "需保留的说明"})
    cards["items"][2]["value"] = None
    for count in (5, 4):
        report["sections"][0]["blocks"].append({"kind": "bands", "title": f"合成 {count} 档",
            "items": [{"range": f"范围{i}", "label": f"等级{i}", "selected": i == 0}
                      for i in range(count)]})
    before = deepcopy(report)
    html = _html(report)
    body = visible_source(html)
    metadata = re.search(r'<div class="cards ibfc-meta-cards">(.*?)</div></div>', body, re.S)[1]
    assert "I 级（亚健康）" in metadata and "总体阶段" not in metadata and "未评估" not in metadata
    assert "合成受损类型" in body and "需保留的说明" in body
    assert 'class="bands bands-count-5"' in body and 'class="bands bands-count-4"' in body
    assert ".bands{grid-template-columns:repeat(5,minmax(0,1fr))" in html
    assert ".bands.bands-count-4{grid-template-columns:repeat(4,minmax(0,1fr))" in html
    assert ".cover+#overview{break-before:page;page-break-before:always" in html
    assert ".report-section+.report-section{break-before:page;page-break-before:always" not in html
    assert ".toc .toc-subsections a{font-size:8pt;line-height:1.45" in html
    assert _embedded(html) == before == report


def test_current_detail_null_is_dash_while_zero_and_boolean_keep_their_types_and_text():
    report = _fixture()
    html = _html(report)
    rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", visible_source(html), re.S)
    by_name = {name: next(row for row in rows if name in row) for name in
               ("合成定性随访", "合成资料不足", "合成模型零值", "合成布尔值")}
    for name in ("合成定性随访", "合成资料不足"):
        assert '<td data-label="风险指数"><span class="empty">—</span></td>' in by_name[name]
        assert "未评估" not in by_name[name]
    assert '<td data-label="风险指数">0</td>' in by_name["合成模型零值"]
    assert '<td data-label="风险指数">True</td>' in by_name["合成布尔值"]
    assert _embedded(html) == report


def test_current_unrated_level_remains_null_and_displays_dash_not_an_extra_risk_grade():
    report = _fixture()
    row = ["合成真实缺项", None, None, "保留对应机制与核对事项。"]
    report["sections"][1]["blocks"][2]["rows"] = [row]
    html = _html(report)
    rendered = next(part for part in re.findall(r"<tr\b[^>]*>(.*?)</tr>", visible_source(html), re.S)
                    if "合成真实缺项" in part)
    assert '<td data-label="风险等级"><span class="empty">—</span></td>' in rendered
    assert not any(label in rendered for label in ("未评估", "资料不足", "定性复核", "存在相关因素"))
    assert _embedded(html) == report


def test_current_model_overview_never_uses_a_shared_scale_or_coerces_boolean_and_null():
    block = {"kind": "table", "title": RISK_OVERVIEW_TITLE, "columns": RISK_OVERVIEW_COLUMNS[:], "rows": [
        ["消化系统", "零分模型", 0, "原等级甲"], ["代谢系统", "大标尺模型", 95.5, "原等级乙"],
        ["消化系统", "定性不画", None, "定性随访"], ["消化系统", "布尔不画", True, "未知"],
        ["消化系统", "字符串不画", "7", "未知"], ["消化系统", "非有限不画", float("inf"), "未知"],
    ]}
    graphic = _model_risk_plot(block)
    assert graphic.count('class="risk-number"') == 2
    assert '>0</b>' in graphic and '>95.5</b>' in graphic
    assert "原等级甲" in graphic and "原等级乙" in graphic
    assert all(name not in graphic for name in ("定性不画", "布尔不画", "字符串不画", "非有限不画"))
    assert "risk-fill" not in graphic and " / 10" not in graphic and "%" not in graphic
    assert "低风险" not in graphic and "健康关注指数" not in graphic
    assert "数值与风险等级对应各系统明细" in graphic
    assert "各自模型标尺" not in graphic


def test_current_risk_numbers_do_not_wrap_at_decimal_point_in_print_and_mobile_layout():
    html = _html(_fixture())
    assert '.model-risk-plot-row{display:flex;' in html
    assert '.model-risk-plot-row .risk-number{font-size:15px;white-space:nowrap;flex:0 0 auto;min-width:3em;' in html
    assert '.model-risk-plot-row small{flex:0 0 auto;min-width:1.5em;white-space:nowrap;' in html


def test_current_empty_overview_does_not_borrow_numeric_detail_or_old_attention_rows():
    report = _fixture()
    disease = report["sections"][1]
    overview = disease["blocks"][1]
    overview["rows"] = []
    html = visible_source(_html(report))
    figure = figure_source(html, 2)
    assert RISK_OVERVIEW_TITLE in figure and "暂无可量化的疾病风险结果" in figure
    assert "risk-number" not in figure and "合成模型零值" not in figure
    assert "合成模型零值" in html  # The detail table still owns the valid zero.
    old_attention = {**overview, "title": "本次健康关注指数总览",
                     "columns": ["系统", "疾病", "健康关注指数", "关注等级"],
                     "rows": [["消化系统", "旧关注值", 8, "优先评估"]]}
    disease["blocks"][1] = old_attention
    assert before_section("disease_risks", report["sections"], template_version=LATEST_DISPLAY_TEMPLATE) == ""
    assert after_block("disease_risks", old_attention, report["sections"], template_version=LATEST_DISPLAY_TEMPLATE) == ""


def test_frozen_v10_uses_its_previous_figure_table_and_percentage_placement():
    archive = historical_archive()
    report = _fixture(version=ORDERED_REFERENCE_DISPLAY_TEMPLATE, archive=archive)
    before = deepcopy(report)
    html = _html(report, ORDERED_REFERENCE_DISPLAY_TEMPLATE, archive)
    figure = figure_source(visible_source(html), 1)
    assert "评分占满分比例" not in figure
    assert 'class="source-stage-label selected"' in figure
    assert 'class="risk-qualitative">定性随访</span>' in visible_source(html)
    assert "template-risk-ibfc-summary" not in html
    assert _embedded(html) == before == report
    assert sha256(html.encode()).hexdigest() == "549001c1732b3d980d1cea544e75394bbc40ca84a2483d9faea6821b79bcc8d7"


def test_frozen_current_introduction_and_grade_are_not_rewritten_by_new_display_names():
    from modules.gut_barrier.publication import project_callback_report
    report = _fixture()
    chapter = report["sections"][1]
    old_intro = "本章按系统列示相关疾病风险评估，并保留本次资料支持的补充复核方向；风险指数仅列示已完成独立模型评估的结果。"
    chapter["blocks"][0]["text"] = old_intro
    chapter["blocks"][1]["rows"][0][3] = "low"
    chapter["blocks"][2]["rows"][2]["cells"][2] = "low"
    before = deepcopy(report)
    frozen = project_callback_report(report)
    assert frozen == before == report
    html = _html(frozen)
    assert old_intro in visible_source(html)
    assert '<td data-label="风险等级">low</td>' in visible_source(html)
    assert _embedded(html) == before


def test_new_current_html_renders_three_label_only_bands_and_embeds_exact_callback():
    from modules.gut_barrier.publication import project_callback_report
    from tests.unit.gut_barrier.test_template_risk_review import _new_template_overview_report

    internal = _new_template_overview_report()
    internal_before = deepcopy(internal)
    callback = project_callback_report(internal)
    before = deepcopy(callback)
    html = _html(callback)
    body = visible_source(html)
    stages = re.search(r'<div class="bands bands-count-3">((?:<div class="band"><b>[^<]+</b></div>){3})</div>', body)
    assert stages is not None
    assert re.findall(r'<div class="band"><b>([^<]+)</b></div>', stages[1]) == ["健康", "亚健康", "疾病"]
    assert "<b></b>" not in stages[1] and "selected-label" not in stages[1]
    assert "人群" not in stages[1] and "分" not in stages[1]
    assert "辅助资料摘要" not in body and "合成辅助肠型" not in body
    assert '.bands.bands-count-3{grid-template-columns:repeat(3,minmax(0,1fr))}' in html
    assert _embedded(html) == before == callback
    assert internal == internal_before
    # The removed CRP classification must not survive template expansion.
    assert 'class="bands bands-count-4"' not in body
    assert not any(b.get("title") == "hs-CRP 项目功能分层" for b in callback["sections"][0]["blocks"])


def test_new_callback_and_html_omit_extra_basis_and_use_same_summary_without_changing_internal_audit():
    from modules.gut_barrier.publication import project_callback_report
    from tests.unit.gut_barrier.test_template_risk_review import (
        _assert_chapter_synthesis, _chapter, _new_template_overview_report, _rows,
    )

    internal = _new_template_overview_report()
    _chapter(internal)["blocks"].append({
        "kind": "paragraph", "title": "本次相关因素与复核依据", "text": "合成内部证据未丢弃。",
    })
    _chapter(internal)["blocks"].append({
        "kind": "callout", "title": "影像检查待核实事项", "tone": "caution", "text": "合成影像原文仍保留内部。",
    })
    before = deepcopy(internal)
    callback = project_callback_report(internal)
    summary = _assert_chapter_synthesis(callback)
    html = _html(callback)
    body = visible_source(html)
    assert "本次相关因素与复核依据" not in body and "合成内部证据未丢弃" not in body
    assert "影像检查待核实事项" not in body and "合成影像原文仍保留内部" not in body
    summary_anchor="disease_risks-"+summary["title"].split()[0].replace(".", "-")
    summary_body=re.search(r'<div class="block" id="'+re.escape(summary_anchor)+r'">(.*?)</div>', body, re.S)[1]
    paragraphs=summary["text"].split("\n\n")
    rendered_paragraphs=re.findall(r"<p>(.*?)</p>", summary_body, re.S)
    assert len(paragraphs)==len(rendered_paragraphs)==2
    assert '#disease_risks .block:last-child{break-inside:avoid;page-break-inside:avoid}' in html
    assert [html_module.unescape(paragraph) for paragraph in rendered_paragraphs]==paragraphs
    assert body.count(f'<h3>{summary["title"]}</h3>')==1
    assert len(_rows(callback)) >= 22
    assert _embedded(html) == callback == project_callback_report(callback)
    assert internal == before
    assert internal["assessment"] == before["assessment"]


@pytest.mark.parametrize("version", [ORDERED_REFERENCE_DISPLAY_TEMPLATE, LATEST_DISPLAY_TEMPLATE])
def test_frozen_callback_and_html_keep_previously_delivered_basis_and_review_summary(version):
    from modules.gut_barrier.publication import project_callback_report

    archive = historical_archive() if version == ORDERED_REFERENCE_DISPLAY_TEMPLATE else None
    report = _fixture(version=version, archive=archive)
    chapter = report["sections"][1]
    basis = {"kind": "paragraph", "title": "本次相关因素与复核依据", "text": "冻结的个体证据正文。"}
    imaging = {"kind": "callout", "title": "影像检查待核实事项", "tone": "caution", "text": "冻结影像原文不能删除。"}
    summary = {"kind": "paragraph", "title": "2.9 本章小结", "text": "本章按8个系统逐项复核，冻结摘要仍须保留。"}
    chapter["blocks"].extend([basis, imaging, summary])
    before = deepcopy(report)
    callback = project_callback_report(report)
    html = _html(callback, version, archive)
    body = visible_source(html)
    assert basis["title"] in body and basis["text"] in body
    assert imaging["title"] in body and imaging["text"] in body
    assert summary["text"] in body
    assert _embedded(html) == callback == before == report


def test_current_three_band_print_override_does_not_change_unrelated_four_and_five_bands():
    report = _fixture()
    report["sections"][0]["blocks"].extend([
        {"kind": "bands", "title": "屏障评分分级", "items": [
            {"range": None, "label": label, "selected": False} for label in ("健康", "亚健康", "疾病")
        ]},
        *[{"kind": "bands", "title": f"其他合成{count}档", "items": [
            {"range": f"原范围{i}", "label": f"原等级{i}", "selected": i == 0}
            for i in range(count)
        ]} for count in (4, 5)],
    ])
    before = deepcopy(report)
    html = _html(report)
    body = visible_source(html)
    assert all(f'class="bands bands-count-{count}"' in body for count in (3, 4, 5))
    assert '.bands{grid-template-columns:repeat(5,minmax(0,1fr))' in html
    assert '.bands.bands-count-3{grid-template-columns:repeat(3,minmax(0,1fr))}' in html
    assert '.bands.bands-count-4{grid-template-columns:repeat(4,minmax(0,1fr))}' in html
    assert '<b>原范围0</b>原等级0<span class="selected-label">本次结果所在区间</span>' in body
    assert _embedded(html) == before == report


def test_frozen_current_html_preserves_old_five_grade_bands_and_auxiliary_content():
    from modules.gut_barrier.publication import project_callback_report
    from tests.unit.gut_barrier.test_template_risk_review import _new_template_overview_report, _overview_blocks

    internal = _new_template_overview_report()
    frozen = project_callback_report(internal)
    frozen_blocks = _overview_blocks(frozen)
    old_blocks = _overview_blocks(internal)
    old_bands = next(block for block in old_blocks if block["title"] == "屏障评分分级")
    next(block for block in frozen_blocks if block["title"] == "屏障评分分级")["items"] = deepcopy(old_bands["items"])
    frozen_blocks.append(deepcopy(next(block for block in old_blocks if block["title"] == "辅助资料摘要")))
    before = deepcopy(frozen)
    callback = project_callback_report(frozen)
    html = _html(callback)
    body = visible_source(html)
    assert callback == before == frozen
    assert 'class="bands bands-count-5"' in body
    assert "辅助资料摘要" in body and "合成辅助肠型" in body
    assert _embedded(html) == before
