"""Completed qualitative results are explicit without inventing numeric scores."""
from copy import deepcopy
import html as html_module
import json
import re

import pytest

from adapters.gut_barrier.frontend_template import expand_dynamic_report
from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import (
    ORDERED_REFERENCE_DISPLAY_TEMPLATE, PRODUCT_FEEDBACK_DISPLAY_TEMPLATE,
)
from modules.gut_barrier.integration_content import (
    ATTENTION_OVERVIEW_TITLE, QUALITATIVE_FOLLOWUP_LEVEL,
)
from modules.gut_barrier.publication import _product_disease_blocks, project_callback_report
from tests.unit.gut_barrier.test_dynamic_publication import (
    REFERENCE_KEYS, RISK_COLUMNS, SECTION_KEYS, clinical_report, section,
)


QUALITATIVE_NAMES = {"消化道占位性病变复核", "泌尿系统结石及尿检复核"}


def _cells(row):
    return row["cells"] if isinstance(row, dict) else row


def _risk_rows(report):
    return {cells[0]: cells for block in section(report, "disease_risks")["blocks"]
            if block.get("columns") == RISK_COLUMNS
            for row in block["rows"] if (cells := _cells(row))}


def _overview(report):
    return next(block for block in section(report, "disease_risks")["blocks"]
                if block["title"] == ATTENTION_OVERVIEW_TITLE)


def _visible(html):
    return re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", html, flags=re.S)


def _text(html):
    return html_module.unescape(re.sub(r"<[^>]+>", "", html)).strip()


def _html_risk_rows(html):
    rows = {}
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", _visible(html), re.S):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S)
        if len(cells) == 4 and 'data-label="风险指数"' in row:
            rows[_text(cells[0])] = [_text(cell) for cell in cells]
    return rows


def _plot_rows(html):
    return {_text(row.split("</span>", 1)[0]): row
            for row in re.findall(r'<div class="risk-plot-row">(.*?)</div>', _visible(html), re.S)}


def _source(*, model=False):
    source = clinical_report()
    source["meta"]["displayTemplateVersion"] = ORDERED_REFERENCE_DISPLAY_TEMPLATE
    source["assessment"]["diseaseAssessment"] = {"mode": "data_driven_attention", "references": []}
    source["assessment"]["diseaseRisks"] = []
    blocks = [{"kind": "callout", "title": "本次关注指数", "text": "本次健康关注排序。", "tone": "default"}]
    systems = [
        ("消化系统", [("肠道炎症与功能紊乱", 3), ("消化道占位性病变复核", None)]),
        ("代谢系统", [("糖脂代谢异常", 4)]),
        ("心脑血管系统", [("动脉粥样硬化性心血管病", 3)]),
        ("泌尿系统", [("泌尿系统结石及尿检复核", None)]),
    ]
    for index, (system, directions) in enumerate(systems, 1):
        rows = []
        for name, score in directions:
            level = "随访关注" if score is None else "加强关注" if score == 4 else "基础关注"
            mechanism = "既往检查发现需结合后续复查核实。" if score is None else "本次相关指标与该关注方向有关。"
            rows.append([name, score, level, mechanism])
            source["assessment"]["diseaseRisks"].append({
                "name": name, "systemName": system, "riskIndex": score, "riskLevel": level,
                "status": "qualitative_followup" if score is None else "attention_index",
                "mechanism": mechanism, "probability": None,
            })
        blocks.append({"kind": "table", "title": f"2.{index} {system}疾病风险评估",
                       "columns": RISK_COLUMNS[:], "rows": rows})
    if model:
        blocks[3]["rows"].append({"cells": ["独立模型零值", 0, "基础关注", "独立模型相关机制。"], "current": False})
        source["assessment"]["diseaseRisks"].append({
            "name": "独立模型零值", "systemName": "心脑血管系统", "status": "evaluated",
            "riskIndex": 0, "riskLevel": "基础关注", "probability": None,
        })
    section(source, "disease_risks")["blocks"] = blocks
    section(source, "overview")["blocks"].append({"kind": "facts", "title": "值保真检查", "items": [
        {"label": "零值", "value": 0}, {"label": "明确否定", "value": False}, {"label": "未提供", "value": None}]})
    return source


@pytest.mark.parametrize("status,internal_score,internal_level,display_score,display_level,expected", [
    ("qualitative_followup", None, "随访关注", None, "随访关注", QUALITATIVE_FOLLOWUP_LEVEL),
    ("unassessed", None, "随访关注", None, "随访关注", "随访关注"),
    ("attention_index", None, "随访关注", None, "随访关注", "随访关注"),
    ("evaluated", None, "随访关注", None, "随访关注", "随访关注"),
    (None, None, "随访关注", None, "随访关注", "随访关注"),
    ("qualitative_followup", 0, "随访关注", None, "随访关注", "随访关注"),
    ("qualitative_followup", False, "随访关注", None, "随访关注", "随访关注"),
    ("qualitative_followup", None, "随访关注", 0, "随访关注", "随访关注"),
    ("qualitative_followup", None, "随访关注", False, "随访关注", "随访关注"),
    ("qualitative_followup", None, "未评估", None, "随访关注", "随访关注"),
    ("qualitative_followup", None, "随访关注", None, "未评估", "未评估"),
])
def test_new_producer_requires_confirmed_qualitative_status_and_matching_null_row(
        status, internal_score, internal_level, display_score, display_level, expected):
    report = {"sections": [{"id": "disease_risks", "blocks": [{"kind": "table", "columns": RISK_COLUMNS[:],
        "rows": [{"cells": ["测试方向", display_score, display_level, "原始机制。"], "current": False}]}]}]}
    assessment = {"diseaseRisks": [{"name": "测试方向", "status": status,
        "riskIndex": internal_score, "riskLevel": internal_level}], "context": {}}
    before = deepcopy(assessment)
    _product_disease_blocks(report, assessment, explicit_qualitative=True)
    row = report["sections"][0]["blocks"][0]["rows"][0]
    assert row["cells"] == ["测试方向", display_score, expected, "原始机制。"]
    assert type(row["cells"][1]) is type(display_score)
    assert row["current"] is False
    assert assessment == before


def test_new_callback_expresses_qualitative_status_in_existing_cell_without_changing_schema_or_assessment(historical_ordered_release):
    source = _source(model=True)
    before = deepcopy(source)
    callback = project_callback_report(source, archive_directory=historical_ordered_release)
    rows = _risk_rows(callback)
    assert {name for name, row in rows.items() if row[2] == QUALITATIVE_FOLLOWUP_LEVEL} == QUALITATIVE_NAMES
    assert all(rows[name][1] is None for name in QUALITATIVE_NAMES)
    assert all(row["riskLevel"] == "随访关注" for row in source["assessment"]["diseaseRisks"]
               if row["status"] == "qualitative_followup")
    assert source == before
    assert set(callback) == {"schemaVersion", "meta", "sections"}
    assert set(callback["meta"]) == {"title", "reportId"}
    assert all(set(item) == SECTION_KEYS for item in callback["sections"])
    assert all(len(row) == 4 for row in rows.values())
    assert all(set(item) == REFERENCE_KEYS for block in section(callback, "appendix")["blocks"]
               if block["kind"] == "references" for item in block["items"])
    probes = next(block["items"] for block in section(callback, "overview")["blocks"] if block["title"] == "值保真检查")
    assert [item["value"] for item in probes] == [0, False, None]
    assert [type(item["value"]) for item in probes] == [int, bool, type(None)]
    assert rows["独立模型零值"][1] == 0 and type(rows["独立模型零值"][1]) is int
    model_row = next(row for block in section(callback, "disease_risks")["blocks"]
                     if block.get("columns") == RISK_COLUMNS for row in block["rows"]
                     if _cells(row)[0] == "独立模型零值")
    assert model_row["current"] is False
    assert any(block["title"] == "独立模型说明" and "不与关注指数比较" in block["text"]
               for block in section(callback, "disease_risks")["blocks"])
    assert project_callback_report(callback) == callback


def test_five_chart_directions_survive_but_only_three_have_numeric_bars_and_model_zero_is_separate(historical_ordered_release):
    callback = project_callback_report(_source(model=True), archive_directory=historical_ordered_release)
    before = deepcopy(callback)
    chart = _overview(callback)["rows"]
    assert len(chart) == 5
    assert [row[2] for row in chart] == [3, None, 4, 3, None]
    assert {row[1] for row in chart if row[3] == QUALITATIVE_FOLLOWUP_LEVEL} == QUALITATIVE_NAMES
    html = render_report_html(callback, template_version=ORDERED_REFERENCE_DISPLAY_TEMPLATE)
    rows, plotted = _html_risk_rows(html), _plot_rows(html)
    assert len(plotted) == 5 and "独立模型零值" not in plotted
    for name in QUALITATIVE_NAMES:
        assert rows[name][1:3] == ["定性随访", QUALITATIVE_FOLLOWUP_LEVEL]
        assert "定性随访" in plotted[name] and 'class="risk-qualitative"' in plotted[name]
        assert "risk-fill" not in plotted[name] and "risk-number" not in plotted[name]
        assert "未评估" not in plotted[name]
    numeric = {"肠道炎症与功能紊乱": 3, "糖脂代谢异常": 4, "动脉粥样硬化性心血管病": 3}
    for name, score in numeric.items():
        assert rows[name][1] == str(score)
        assert 'class="risk-fill"' in plotted[name] and f">{score} / 10</b>" in plotted[name]
    assert rows["独立模型零值"][1] == "0"
    assert json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.S)[1]) == before
    assert callback == before


@pytest.mark.parametrize("status", ["unassessed", "attention_index", None])
def test_ordinary_missing_result_still_renders_as_unassessed(status, historical_ordered_release):
    source = _source()
    for row in source["assessment"]["diseaseRisks"]:
        if row["name"] in QUALITATIVE_NAMES:
            row["status"] = status
    callback = project_callback_report(source, archive_directory=historical_ordered_release)
    assert all(_risk_rows(callback)[name][2] == "随访关注" for name in QUALITATIVE_NAMES)
    rendered = _html_risk_rows(render_report_html(callback, template_version=ORDERED_REFERENCE_DISPLAY_TEMPLATE))
    assert all(rendered[name][1] == "未评估" for name in QUALITATIVE_NAMES)


def test_prior_product_publication_does_not_opt_into_the_new_qualitative_label():
    source = _source()
    source["meta"]["displayTemplateVersion"] = PRODUCT_FEEDBACK_DISPLAY_TEMPLATE
    assert all(_risk_rows(project_callback_report(source))[name][2] == "随访关注" for name in QUALITATIVE_NAMES)
    default_report = {"sections": deepcopy(source["sections"])}
    _product_disease_blocks(default_report, source["assessment"])
    assert all(_risk_rows(default_report)[name][2] == "随访关注" for name in QUALITATIVE_NAMES)


def test_frozen_old_v10_callback_is_not_upgraded_and_legacy_table_and_chart_rendering_stay_distinct(historical_ordered_release):
    frozen = project_callback_report(_source(), archive_directory=historical_ordered_release)
    # Freeze the previous v10 producer's existing cells; references are still
    # the valid same-template frozen plan, not synthesized by the HTML adapter.
    for row in _risk_rows(frozen).values():
        if row[0] in QUALITATIVE_NAMES:
            row[2] = "随访关注"
    for row in _overview(frozen)["rows"]:
        if row[1] in QUALITATIVE_NAMES:
            row[3] = "随访关注"
    before = deepcopy(frozen)
    assert project_callback_report(frozen) == before
    expanded = expand_dynamic_report(frozen, template_version=ORDERED_REFERENCE_DISPLAY_TEMPLATE)
    assert _risk_rows(expanded) == _risk_rows(before)
    assert _overview(expanded) == _overview(before)
    html = render_report_html(frozen, template_version=ORDERED_REFERENCE_DISPLAY_TEMPLATE)
    rendered, plotted = _html_risk_rows(html), _plot_rows(html)
    for name in QUALITATIVE_NAMES:
        assert rendered[name][1:3] == ["未评估", "随访关注"]
        assert "定性随访" in plotted[name] and "risk-fill" not in plotted[name]
    assert QUALITATIVE_FOLLOWUP_LEVEL not in _visible(html)
    assert json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.S)[1]) == before
    assert render_report_html(project_callback_report(frozen), template_version=ORDERED_REFERENCE_DISPLAY_TEMPLATE) == html
    assert frozen == before
