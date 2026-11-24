"""Manual retries preserve correction audits and source-backed nested fields."""

from collections import Counter
from copy import deepcopy

import pytest

from modules.report import reports
from modules.report.fallback_policy import ReportFallbackPolicy
from modules.report.terminal import failure_payload


POLICY = ReportFallbackPolicy(llm_attempts=2, retry_backoff_seconds=())


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,prefix,parts", [
    ("explain", "explain", (1, 2, 3, 4)),
    ("explain_gutBarrier", "gut_barrier", (1, 2)),
])
async def test_envelope_retry_replays_cached_manual_correction_audit(monkeypatch, kind, prefix, parts):
    calls, attempts, attempt_audits = Counter(), 0, []
    manual = reports.gut_barrier_report if kind == "explain_gutBarrier" else reports.explain_report

    async def model(prompt, *, task_type):
        calls[task_type] += 1
        if task_type == f"{prefix}_opening":
            return '{"opening_script":{"self_intro":"您好，我们一起核对已有报告资料。"}}'
        # This number has no source: each section must remove it and retain
        # the audit even when a later envelope retry reuses the corrected text.
        return '{"script":{"opening":"新增数字999"}}'

    async def malformed_first_envelope():
        nonlocal attempts
        attempts += 1
        body = await manual({}, "u", "s")
        attempt_audits.append(deepcopy(body["closing"]["self_check"]))
        if attempts == 1:
            body["type"] = "invalid-first-envelope"
        return body

    monkeypatch.setattr(reports, "safe_complete_async", model)
    body, status = await reports._generate_followup_module(
        kind, malformed_first_envelope, data={}, user_id="u", session_id="s", policy=POLICY,
    )

    assert attempts == 2 and status == "retried_success"
    assert calls == {**{f"{prefix}_part{part}": 1 for part in parts}, f"{prefix}_opening": 1}
    assert all("999" not in section["script"]["opening"] for section in body["sections"])
    correction = next(item for item in body["closing"]["self_check"] if item["item"] == "归一化与合规修正")
    assert "999" in correction["result"] and "已修正" in correction["result"]
    assert "通过（无改动）" not in correction["result"]
    assert attempt_audits[0] == attempt_audits[1] == body["closing"]["self_check"]


_BACKGROUND_COLUMNS = ["检查日期", "项目", "时间属性", "原始检查摘要"]
_BACKGROUND_CELLS = [
    ["2026-09-24", "hs-CRP", "本次", ">3.0 mg/L；需结合原始结果核对"],
    ["2026-08-20", "腹部检查", "既往", "来源记载的检查所见"],
]


def gut_source(with_rows):
    blocks = [{
        "kind": "table", "title": "2.1 消化系统疾病风险评估",
        "columns": ["疾病", "风险指数", "风险等级", "机制关联"],
        "rows": [
            ["来源关注项目", "23", "需关注", "来源所述关联方向"],
            ["待评估项目", None, "未评估", None],
        ],
    }]
    if with_rows:
        blocks.append({
            "kind": "table", "title": "个体临床背景",
            "columns": list(_BACKGROUND_COLUMNS), "rows": deepcopy(_BACKGROUND_CELLS),
        })
    return {"type": "gutBarrier", "report": {
        "sections": [{"id": "disease_risks", "visible": True, "blocks": blocks}],
    }}


def assert_source_fields(body, with_rows):
    assert isinstance(body["sections"], list)
    assert [section["part_no"] for section in body["sections"]] == [1, 2]
    section = body["sections"][1]
    assert set(section["risk_overview"]) == {"kind", "text", "must_say", "stats"}
    assert section["risk_overview"]["stats"] == {"total": 2, "assessed": 1, "unassessed": 1}
    assert set(section["background_talk"]) == {"kind", "talk", "rows"}
    expected = [dict(zip(_BACKGROUND_COLUMNS, row)) for row in _BACKGROUND_CELLS if row[1] != "hs-CRP"] if with_rows else []
    assert section["background_talk"]["rows"] == expected
    return section["risk_overview"]["stats"], section["background_talk"]["rows"]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_rows", [False, True], ids=["no_background", "source_background"])
async def test_gut_manual_keeps_stats_and_background_after_component_failure(monkeypatch, with_rows):
    source = gut_source(with_rows)
    unavailable = None
    calls = Counter()

    async def model(prompt, *, task_type):
        calls[task_type] += 1
        if unavailable == "all" or unavailable == task_type:
            raise TimeoutError("risk chapter unavailable")
        if task_type == "gut_barrier_opening":
            return '{"opening_script":{"self_intro":"我们一起核对本次报告原文。"}}'
        return '{"script":{"opening":"本章依据来源记录逐项解读。"}}'

    async def generate():
        return await reports._generate_followup_module(
            "explain_gutBarrier", lambda: reports.gut_barrier_report(source, "u", "s"),
            data=source, user_id="u", session_id="s", policy=POLICY,
        )

    monkeypatch.setattr(reports, "safe_complete_async", model)
    normal, _ = await generate()
    normal_fields = assert_source_fields(normal, with_rows)
    calls.clear()
    unavailable = "gut_barrier_part2"
    partial, _ = await generate()
    assert partial["status"] == "partial_success"
    assert calls == {"gut_barrier_part1": 1, "gut_barrier_part2": 2, "gut_barrier_opening": 1}
    assert assert_source_fields(partial, with_rows) == normal_fields
    calls.clear()
    unavailable = "all"
    failed, status = await generate()
    assert failed["status"] == "failed" and status == "fallback_empty"
    assert calls == {"gut_barrier_part1": 2, "gut_barrier_part2": 2, "gut_barrier_opening": 2}
    assert assert_source_fields(failed, with_rows) == normal_fields


@pytest.mark.asyncio
@pytest.mark.parametrize("with_rows", [False, True], ids=["no_background", "source_background"])
@pytest.mark.parametrize("error_code", ["REPORT_GENERATION_INTERRUPTED", "REPORT_DEPENDENCY_FAILED"])
async def test_recovered_gut_manual_keeps_the_same_source_fields(monkeypatch, with_rows, error_code):
    source = gut_source(with_rows)

    async def model(prompt, *, task_type):
        if task_type == "gut_barrier_opening":
            return '{"opening_script":{"self_intro":"一起核对报告内容。"}}'
        return '{"script":{"opening":"按原始记录核对本章内容。"}}'

    monkeypatch.setattr(reports, "safe_complete_async", model)
    normal = await reports.gut_barrier_report(source, "u", "s")

    async def no_model_during_recovery(*args, **kwargs):
        pytest.fail("recovery must not invoke a model")

    monkeypatch.setattr(reports, "safe_complete_async", no_model_during_recovery)
    recovered = failure_payload(
        "explain_gutBarrier", {"userId": "u", "sessionId": "s", "finalReportId": "f"},
        error_code=error_code, evaluate=source,
    )
    assert recovered["status"] == "failed"
    assert recovered["error"]["error_code"] == error_code
    assert assert_source_fields(recovered, with_rows) == assert_source_fields(normal, with_rows)
    # Source rows, including comparison-valued results and dates, are copied;
    # mutating a terminal payload must not alter the supplied report.
    if with_rows:
        recovered["sections"][1]["background_talk"]["rows"][0]["原始检查摘要"] = "changed"
        assert source["report"]["sections"][0]["blocks"][1]["rows"] == _BACKGROUND_CELLS
