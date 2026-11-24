"""Attention-only display semantics preserve dynamic evidence and old reports."""

from copy import deepcopy

import pytest

from modules.gut_barrier.publication import project_callback_report, project_public_report
from tests.unit.gut_barrier.report_fixtures import document


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
        {"kind": "paragraph", "title": "", "text": "固定介绍（投影测试）"},
        {"kind": "callout", "title": "本次关注指数", "tone": "caution",
         "text": "本次采用未验证的健康关注排序；不是患病概率。"},
        {"kind": "table", "title": "2.1 消化系统疾病风险评估", "columns": RISK_COLUMNS, "rows": rows},
    ]
    return report


@pytest.mark.parametrize("current_row", [False, True])
def test_new_attention_callback_retains_fourth_column_and_is_idempotent(current_row):
    original = attention_report(current_row=current_row)
    before = deepcopy(original)
    dynamic = project_callback_report(original)
    assert table(dynamic) == table(project_public_report(original))
    assert project_callback_report(dynamic) == dynamic
    assert project_callback_report(project_public_report(original))["sections"][1]["blocks"] == dynamic["sections"][1]["blocks"]
    assert original == before
    assert set(dynamic) == {"schemaVersion", "meta", "sections"}
    assert all(set(item) == {"id", "title", "subtitle", "blocks", "visible"} for item in dynamic["sections"])
    assert set(table(dynamic)) == {"kind", "title", "columns", "rows"}


@pytest.mark.parametrize("value", [None, 0, False, "", "真实个体评估依据"])
def test_reprojecting_already_dynamic_report_preserves_all_fourth_column_values(value):
    dynamic = project_callback_report(document())
    table(dynamic)["rows"][0][3] = value
    before = deepcopy(dynamic)
    assert project_callback_report(dynamic) == before == dynamic


def test_legacy_internal_reports_still_publish_null_static_mechanisms():
    original = document()
    assert table(original)["rows"][0][3]
    dynamic = project_callback_report(original)
    assert all(row[3] is None for block in section(dynamic, "disease_risks")["blocks"]
               if block.get("columns") == RISK_COLUMNS for row in block["rows"])


@pytest.mark.asyncio
async def test_current_pipeline_keeps_dynamic_basis_and_range_rule_scores_separate():
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
    assert_template_risk_contract(dynamic, report["assessment"])
    # Questionnaire symptoms and body measurements do not add risk rows.
    assert len(rows) == 23 and report["assessment"]["diseaseRisks"] == []
    assert report["assessment"]["riskRuleAssessment"]["status"] == "evaluated"
    assert all(1 <= row[1] <= 10 and row[2] in {"低", "中", "高"} and row[3]
               for row in rows if row[1] is not None)
    assert all(row[1] is not None and row[2] is not None for row in rows)
    chart = next(block for block in section(dynamic, "disease_risks")["blocks"]
                 if block["title"] == "本次疾病风险评估总览")
    assert len(chart["rows"]) == 23
    assert [row[1:] for row in chart["rows"]] == [row[:3] for row in rows if row[1] is not None]
    assert "HS_CRP:range_lower_bound_used" not in report["assessment"]["riskRuleAssessment"]["notices"]
    assert all("本次依据：" not in row[3] and "随访重点：" not in row[3] for row in rows)
    assert all(row[3].count("。") == 1 for row in rows)
    assert report["assessment"]["context"]["healthSurvey"]["sourceType"] == "self_reported"
    assert project_callback_report(dynamic) == dynamic
    assert "inflammation" not in report["assessment"]


@pytest.mark.asyncio
async def test_empty_new_pipeline_does_not_inject_the_old_fixed_disease_list():
    from modules.gut_barrier.pipeline import GutBarrierPipeline
    from modules.gut_barrier.schemas import GutBarrierRequest

    report, _ = await GutBarrierPipeline().generate(GutBarrierRequest.model_validate({
        "userId": "u", "sessionId": "s", "age": 26,
    }), report_id="gbr_empty_attention", evaluation_date="2026-09-24")
    dynamic = project_callback_report(report)
    assert report["assessment"]["diseaseRisks"] == []
