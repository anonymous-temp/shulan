"""Integration feedback must survive publication and frozen callback replay."""
from copy import deepcopy

import pytest

from modules.gut_barrier.display_versions import CANONICAL_DISPLAY_TEMPLATE
from modules.gut_barrier.integration_content import (
    ATTENTION_OVERVIEW_COLUMNS, ATTENTION_OVERVIEW_TITLE, DISEASE_INTRODUCTION,
)
from modules.gut_barrier.publication import normalize_current_dynamic_report, project_callback_report
from modules.report.gut_barrier_context import build_part2_context
from tests.unit.gut_barrier.test_dynamic_publication import clinical_report, section


def _internal(rows, *, attention=True):
    report = clinical_report()
    report["meta"]["displayTemplateVersion"] = CANONICAL_DISPLAY_TEMPLATE
    report["assessment"]["diseaseAssessment"] = {"mode": "data_driven_attention" if attention else "registered"}
    section(report, "disease_risks")["blocks"] = [
        *([{"kind": "callout", "title": "本次关注指数", "text": "原有标尺说明", "tone": "info"}]
          if attention else []),
        {"kind": "table", "title": "2.5 消化系统疾病风险评估",
         "columns": ["疾病", "风险指数", "风险等级", "机制关联"], "rows": rows},
        {"kind": "list", "title": "本章小结", "items": ["原有关注方向"]},
    ]
    return report


def _chart(report):
    return next(block for block in section(report, "disease_risks")["blocks"]
                if block["title"] == ATTENTION_OVERVIEW_TITLE)


def test_intro_chart_and_summary_are_published_without_losing_source_fields_or_directions():
    rows = [["关注甲", 0, "基础关注", "原有依据"],
            {"cells": ["关注乙", 8, "优先评估", "另一依据"], "current": False},
            ["既往胃炎随访", None, "随访关注", "既往病史依据"],
            ["独立模型", 7.5, "优先评估", "本项数值来自独立的已配置模型，不与本章自定义关注指数比较。"]]
    internal = _internal(rows)
    before = deepcopy(internal)
    report = project_callback_report(internal)
    blocks = section(report, "disease_risks")["blocks"]
    assert blocks[0] == {"kind": "paragraph", "title": None, "text": DISEASE_INTRODUCTION}
    assert _chart(report) == {"kind": "table", "title": ATTENTION_OVERVIEW_TITLE,
        "columns": ATTENTION_OVERVIEW_COLUMNS, "rows": [
            ["消化系统", "关注甲", 0, "基础关注"], ["消化系统", "关注乙", 8, "优先评估"],
            ["消化系统", "既往胃炎随访", None, "随访关注"]]}
    assert next(block for block in blocks if block["title"] == "2.1 消化系统疾病风险评估")["rows"] == rows
    summary, directions = blocks[-2:]
    assert set(summary) == {"kind", "title", "text"} and summary["kind"] == "paragraph"
    assert summary["title"] == directions["title"] == "2.2 本章小结"
    assert "消化系统的4项" in summary["text"] and "2项提供健康关注指数" in summary["text"]
    assert "1项为定性随访" in summary["text"] and "神经系统" not in summary["text"]
    assert directions == {"kind": "list", "title": "2.2 本章小结", "items": ["原有关注方向"]}
    assert report == project_callback_report(report) == normalize_current_dynamic_report(report)
    assert internal == before

    explanation = build_part2_context({"report": report})
    assert explanation["stats"]["total"] == len(rows)
    assert len(explanation["systems"]) == 1
    assert explanation["systems"][0]["rows"][0]["disease"] == "关注甲"
    assert not any(block.get("title") == ATTENTION_OVERVIEW_TITLE for block in explanation["extra_blocks"])


@pytest.mark.parametrize("value", [True, False, "8.5", -1, 11, float("nan"), float("inf"), None])
def test_chart_does_not_coerce_invalid_attention_to_zero_or_a_bar(value):
    report = project_callback_report(_internal([["关注甲", value, "优先评估", "原有依据"]]))
    assert _chart(report)["rows"] == []


def test_unassessed_and_registered_results_do_not_gain_a_shared_attention_scale():
    report = project_callback_report(_internal([["模型甲", 0, "低风险", "独立依据"]], attention=False))
    assert _chart(report)["rows"] == []
    source = next(block for block in section(report, "disease_risks")["blocks"]
                  if block["title"] == "2.1 消化系统疾病风险评估")
    assert source["rows"][0][1] == 0


def test_empty_reports_keep_empty_chart_and_summary_without_a_template_disease_inventory():
    internal = _internal([])
    section(internal, "disease_risks")["blocks"] = [
        {"kind": "list", "title": "本章小结", "items": []}]
    report = project_callback_report(internal)
    assert _chart(report)["rows"] == []
    summary, directions = section(report, "disease_risks")["blocks"][-2:]
    assert summary["title"] == directions["title"] == "2.1 本章小结"
    assert "尚未形成" in summary["text"] and "冠心病" not in summary["text"]
    assert directions["items"] == []


def test_frozen_callbacks_keep_their_actual_intro_chart_summary_and_missing_blocks():
    old = project_callback_report(clinical_report())
    before = deepcopy(old)
    assert project_callback_report(old) == before
    assert not any(block["title"] == ATTENTION_OVERVIEW_TITLE for block in section(old, "disease_risks")["blocks"])
    report = project_callback_report(_internal([["关注甲", 8, "优先评估", "原有依据"]]))
    blocks = section(report, "disease_risks")["blocks"]
    blocks[0]["text"] = "已发布导语"
    blocks[-2]["text"] = "已发布总结"
    _chart(report)["rows"] = [["消化系统", "已发布关注", 0, "基础关注"]]
    before = deepcopy(report)
    assert project_callback_report(report) == before
    assert report == before
