"""Manual fallback text must distinguish unavailable facts from normal findings."""

import json

import pytest

from modules.report.explain_defaults import default_explain_manual
from modules.report.gut_barrier_defaults import default_gut_barrier_manual


def gut_source(*blocks):
    return {
        "type": "gutBarrier",
        "report": {"sections": [{"id": "overview", "visible": True, "blocks": list(blocks)}]},
    }


def manual(factory, source):
    return factory(evaluate=source, user_id="test-user", session_id="test-session")


@pytest.mark.parametrize("source", [{}, {"status": "failed", "error": {"code": "GENERATION_FAILED"}}])
def test_empty_evaluate_manual_does_not_invent_normality_or_evidence(source):
    report = manual(default_explain_manual, source)
    text = json.dumps(report, ensure_ascii=False)
    assert "没有需要立即处理的事项" not in text
    assert "没有需要逐条展开的临床问题" not in text
    assert "有的，报告第四部分列了 0 篇依据" not in text
    assert "最近两周" not in text
    assert "约两周后回访" not in text
    assert report["evidence"] == []
    assert "未包含可核对的综合分层" in report["sections"][0]["key_points"][0]["text"]
    assert "需要补充来源后确认" in report["sections"][3]["qa_table"][0]["answer"]
    assert report["sections"][2]["plan_talk"] == []
    assert report["sections"][2]["clinical_followup_talk"]["items"] == []
    assert "未包含具体检查安排" in report["sections"][2]["clinical_followup_talk"]["intro"]
    assert set(report) >= {"preface", "sections", "closing"}
    assert len(report["sections"]) == 4


def test_evaluate_manual_preserves_verified_conclusion_zero_score_and_evidence():
    report = manual(default_explain_manual, {
        "diagnosis_summary": {"conclusion": "已记录的检查结论。"},
        "healthAssessment": {"overallScore": 0, "levelLabel": "待复核"},
        "evidence": [{"number": 1, "title": "已提供的参考资料", "url": "https://example.test/evidence"}],
    })
    assert report["sections"][0]["script"]["body"] == ["已记录的检查结论。"]
    assert "综合评分 0 分" in report["sections"][0]["key_points"][0]["text"]
    assert len(report["evidence"]) == 1
    assert "按具体建议的引用编号逐条核对" in report["sections"][3]["qa_table"][0]["answer"]


def test_empty_gut_manual_does_not_invent_grade_or_current_normality():
    report = manual(default_gut_barrier_manual, {})
    part1, part2 = report["sections"]
    assert part1["ibfc_talk"]["text"] == "本次资料未包含可核对的肠道屏障评分与分级结果。"
    assert "这次这几项没有超出参考范围" not in json.dumps(report, ensure_ascii=False)
    assert "未包含可供解读的疾病风险条目" in part2["risk_overview"]["text"]
    assert report["references"] == []
    assert len(report["sections"]) == 2


@pytest.mark.parametrize("score", [0, 7.5])
def test_gut_manual_preserves_score_without_inventing_unprovided_grade(score):
    report = manual(default_gut_barrier_manual, gut_source({
        "kind": "cards", "title": "IBFC评分",
        "items": [{"label": "IBFC评分", "value": score}],
    }))
    part1 = report["sections"][0]
    assert part1["ibfc_talk"]["text"] == f"肠道屏障功能评分 {score} 分。"
    assert "未见评分异常" not in json.dumps(part1["key_points"], ensure_ascii=False)


@pytest.mark.parametrize("evaluation, expected_normal", [(None, False), ("正常", True), ("非正常", False)])
def test_gut_manual_only_calls_indicator_normal_with_explicit_source_evaluation(evaluation, expected_normal):
    report = manual(default_gut_barrier_manual, gut_source({
        "kind": "table", "title": "检测指标",
        "columns": ["检测指标", "检测结果", "参考范围", "结果评价"],
        "rows": [["DAO", "3.8 U/L", "0–5 U/L", evaluation]],
    }))
    points = json.dumps(report["sections"][0]["key_points"], ensure_ascii=False)
    assert ("未超出参考范围" in points) is expected_normal
    assert report["sections"][0]["indicator_talk"]["items"][0]["result"] == "3.8 U/L"


def test_gut_manual_keeps_supplied_ibfc_conclusion_instead_of_fallback_text():
    report = manual(default_gut_barrier_manual, gut_source({
        "kind": "paragraph", "title": "IBFC评分说明", "text": "本次报告明确记录的评分结论。",
    }))
    assert report["sections"][0]["ibfc_talk"]["text"] == "本次报告明确记录的评分结论。"
