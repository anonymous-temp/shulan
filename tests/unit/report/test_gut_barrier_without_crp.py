"""New gut-barrier interpretations omit retired CRP data without changing evaluate."""

from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest

from modules.report import reports
from modules.report.gut_barrier_context import (
    build_gut_barrier_contexts, strip_crp_text, without_crp_content,
)
from modules.report.gut_barrier_defaults import default_gut_barrier_manual, default_section
from modules.report.gut_barrier_normalize import normalize_section
from tests.unit.report.test_risk_warning import gut_constitution_report


@pytest.mark.parametrize("crp_status", ["正常", "hs-CRP指标异常", ">10 mg/L", "范围型结果", "缺失"])
def test_crp_cannot_change_new_manual_facts_or_create_retest(crp_status):
    source = gut_constitution_report(crp_evaluation=crp_status)
    before = deepcopy(source)
    context = build_gut_barrier_contexts(source)
    adapted = reports._gut_barrier_followup_input(source)
    assert source == before
    assert len(context["part1"]["indicator_table"]["rows"]) == 3
    assert "inflammation" not in context["part1"]
    assert context["quick_card"]["inflammation_band"] == ""
    assert "CRP" not in json.dumps(adapted, ensure_ascii=False)
    assert "反应蛋白" not in json.dumps(context, ensure_ascii=False)
    assert all(dimension["code"] != "inflammation" for dimension in adapted["healthAssessment"]["dimensions"])
    assert "炎症指标" not in adapted["instruction_summary"]["constitution"]
    no_crp = deepcopy(source)
    no_crp["report"]["sections"][0]["blocks"][1]["rows"].pop()
    assert adapted == reports._gut_barrier_followup_input(no_crp)


@pytest.mark.parametrize("text,expected", [
    ("DAO指标异常，伴hs-CRP升高。肠道屏障评分为8分。", "DAO指标异常。肠道屏障评分为8分。"),
    ("DAO、hs-CRP指标异常。", "DAO指标异常。"),
    ("DAO指标异常，hsCRP指标异常，LPS指标异常。", "DAO指标异常，LPS指标异常。"),
    ("hs-CRP指标异常。", ""),
    ("hs-CRP＞3，需补充定量结果，无法精确分档。", ""),
    ("hs-CRP升高，已明确急性感染，需处理当前感染。", "已明确急性感染，需处理当前感染。"),
    ("DAO、hs-CRP 12 mg/L。", ""),
    ("促进全身性慢性低度炎症和氧化应激。", "促进全身性慢性低度炎症和氧化应激。"),
    ("已明确急性感染，需处理当前感染。", "已明确急性感染，需处理当前感染。"),
])
def test_retired_clauses_do_not_remove_adjacent_barrier_or_actual_clinical_facts(text, expected):
    assert strip_crp_text(text) == expected


def test_rows_keep_column_alignment_and_reference_titles_are_not_rewritten():
    data = {
        "rows": [
            ["屏障", "DAO", "11", "", "指标异常", ""],
            ["炎症", "超敏C反应蛋白", "4", "0.5", "异常", "炎症"],
        ],
        "references": [
            {"id": "keep", "title": "肠道屏障与炎症机制"},
            {"id": "omit", "title": "C-reactive protein and outcomes", "doi": "original"},
        ],
    }
    cleaned = without_crp_content(data)
    assert cleaned["rows"] == [["屏障", "DAO", "11", "", "指标异常", ""]]
    assert cleaned["references"] == [{"id": "keep", "title": "肠道屏障与炎症机制"}]
    assert len(data["rows"]) == 2


@pytest.mark.parametrize("row", [
    ["IBFC", 0],
    ["IBFC", 0.0],
    ["异常", False],
    ["检测结果", None],
    ["检测结果", ""],
    ["风险因子", 0, False, None, ""],
    ["辅助资料", [], {}],
])
def test_non_crp_rows_preserve_zero_false_and_existing_empty_values(row):
    source = {"rows": [deepcopy(row)]}
    assert without_crp_content(source) == source


def test_only_crp_cleared_rows_are_removed_without_discarding_retained_zero_false():
    source = {"rows": [
        ["说明", None, "hs-CRP指标异常"],
        ["IBFC", 0, "hs-CRP指标异常"],
        ["异常", False, "hs-CRP指标异常"],
    ]}
    assert without_crp_content(source) == {"rows": [
        ["IBFC", 0, ""],
        ["异常", False, ""],
    ]}
    assert source["rows"][0][2] == "hs-CRP指标异常"


def test_model_cannot_reintroduce_crp_and_compatibility_object_remains_empty():
    context = build_gut_barrier_contexts(gut_constitution_report())
    model = default_section(1, context)
    model["inflammation_talk"] = {"kind": "script", "text": "CRP升高提示炎症", "must_say": "需CRP复测"}
    model["script"]["opening"] = "DAO指标异常，伴hs-CRP升高。"
    model["indicator_talk"]["items"].append({"name": "hs-CRP", "result": "9"})
    normalized = normalize_section(1, model, context)
    assert normalized["inflammation_talk"] == {"kind": "script", "text": "", "must_say": ""}
    assert "CRP" not in json.dumps(normalized, ensure_ascii=False)
    assert "DAO指标异常" in normalized["script"]["opening"]
    assert len(normalized["indicator_talk"]["items"]) == 3


def test_fallback_manual_has_three_marker_content_without_crp_placeholder():
    manual = default_gut_barrier_manual(evaluate=gut_constitution_report(), user_id="u", session_id="s")
    first = manual["sections"][0]
    assert first["inflammation_talk"] == {"kind": "script", "text": "", "must_say": ""}
    assert len(first["indicator_talk"]["items"]) == 3
    assert "CRP" not in json.dumps(manual, ensure_ascii=False)
    assert "四项" not in json.dumps(first, ensure_ascii=False)
    assert "肠道屏障报告解读" in manual["manual_meta"]["title"]


@pytest.mark.asyncio
async def test_evaluate_still_preserves_its_crp_information(monkeypatch):
    monkeypatch.setattr(reports, "safe_complete_async", AsyncMock(return_value=json.dumps({
        "body_mass": "hs-CRP指标异常。", "risk_warning": "hs-CRP指标异常。",
    }, ensure_ascii=False)))
    monkeypatch.setattr(reports, "_build_scope_note", lambda data: "")
    evaluate = await reports._info({"type": "evaluate"})
    gut = await reports._info(reports._gut_barrier_followup_input(gut_constitution_report()))
    assert "hs-CRP" in evaluate["body_mass"]
    assert "hs-CRP" not in json.dumps(gut, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_type,expected", [
    ("gutBarrier", "DAO指标异常。"),
    ("evaluate", "DAO指标异常，hs-CRP指标异常。"),
])
async def test_crp_filter_runs_before_new_artifact_archive_only_for_gut(monkeypatch, source_type, expected):
    from modules.report.fallback_policy import ReportFallbackPolicy
    from modules.report.followup_defaults import default_followup_report

    body = default_followup_report("opinion", evaluate={}, user_id="u", session_id="s")
    body["diagnosis_summary"]["conclusion"] = "DAO指标异常，hs-CRP指标异常。"
    original = deepcopy(body)
    archive = AsyncMock(side_effect=lambda payload, *args, **kwargs: payload)
    monkeypatch.setattr(reports, "_archive_finished_followup", archive)
    result, status = await reports._generate_followup_module(
        "opinion", AsyncMock(return_value=body), data={"type": source_type},
        user_id="u", session_id="s", policy=ReportFallbackPolicy(llm_attempts=1),
    )
    assert status == "success"
    assert archive.await_args.args[0]["diagnosis_summary"]["conclusion"] == expected
    assert result["diagnosis_summary"]["conclusion"] == expected
    assert body == original
