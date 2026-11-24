"""An unavailable component never erases a completed sibling result."""
import asyncio
from collections import Counter
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest

from modules.report import reports
from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY, ReportFallbackPolicy
from modules.report.followup_defaults import FOLLOWUP_DEFAULTS, PFMT_USAGE, notes_base


POLICY = ReportFallbackPolicy(llm_attempts=2, retry_backoff_seconds=())


async def generate(kind, factory, *, policy=POLICY):
    return await reports._generate_followup_module(kind, factory, data={}, user_id="u", session_id="s", policy=policy)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "timeout"])
async def test_summary_survives_basis_failure_and_only_failed_component_retries(monkeypatch, failure):
    calls = Counter()

    async def model(prompt, *, task_type):
        calls[task_type] += 1
        if task_type == "opinion_summary":
            return json.dumps({"conclusion": "已验证的来源结论"})
        if task_type == "opinion_basis":
            if failure == "timeout":
                await asyncio.wait_for(asyncio.Event().wait(), timeout=0.001)
            raise RuntimeError("component unavailable")
        return "{}"

    monkeypatch.setattr(reports, "safe_complete_async", model)
    body, status = await generate("opinion", lambda: reports.opinion_report({}, "u", "s"))
    assert body["diagnosis_summary"]["conclusion"] == "已验证的来源结论"
    assert body["diagnostic_basis"] == FOLLOWUP_DEFAULTS["opinion_basis"]
    assert body["status"] == "partial_success" and status == "success"
    assert calls == {"opinion_summary": 1, "opinion_basis": 2, "opinion_advice": 1}


@pytest.mark.asyncio
async def test_instruction_preserves_valid_lifestyle_when_microbiota_and_supplements_fail(monkeypatch):
    calls = Counter()

    async def model(prompt, *, task_type):
        calls[task_type] += 1
        if task_type == "lifestyle":
            return '{"diet":{"summary":"已核实的个体饮食说明","points":["保留此个体化建议"]}}'
        if task_type in ("gut_microbiota", "nutrient_supplements"):
            raise RuntimeError("unavailable")
        if task_type == "recommended_tests":
            return '{"items":[{"name":"待补充资料核对","reason":"明确本次评估依据","timeframe":"2周内"}]}'
        return "{}"

    monkeypatch.setattr(reports, "safe_complete_async", model)
    recommend = {**deepcopy(FOLLOWUP_DEFAULTS["recommend"]), "recommended_combination_method": "生活方式干预"}
    body, _ = await generate("instruction", lambda: reports.instruction_report({}, deepcopy(FOLLOWUP_DEFAULTS["info"]), recommend, "u", "s"))
    assert body["status"] == "partial_success"
    intervention = body["non_drug_intervention"]
    diet = next(item for item in intervention["lifestyle_intervention"]["items"] if item["key"] == "diet")
    assert diet["summary"] == "已核实的个体饮食说明"
    assert diet["points"] == ["保留此个体化建议"]
    assert intervention["gut_microbiota_regulation"]["probiotics"]["products"] == []
    assert isinstance(intervention["supplements"]["items"], list)
    assert calls == {"lifestyle": 1, "gut_microbiota": 2, "nutrient_supplements": 2, "notes": 1, "recommended_tests": 1}
    assert "doctor_advice" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,prefix,part_numbers", [
    ("explain", "explain", [1, 2, 3, 4]),
    ("explain_gutBarrier", "gut_barrier", [1, 2]),
])
@pytest.mark.parametrize("failure", ["exception", "timeout"])
async def test_manual_preserves_completed_opening_and_sections_when_one_section_fails(monkeypatch, kind, prefix, part_numbers, failure):
    calls = Counter()

    async def model(prompt, *, task_type):
        calls[task_type] += 1
        if task_type == f"{prefix}_part2":
            if failure == "timeout":
                await asyncio.wait_for(asyncio.Event().wait(), timeout=0.001)
            raise RuntimeError("section unavailable")
        if task_type == f"{prefix}_opening":
            return '{"opening_script":{"self_intro":"您好，让我们一起核对本次报告。"}}'
        return '{"script":{"opening":"本段保留已核实的说明。"}}'

    monkeypatch.setattr(reports, "safe_complete_async", model)
    factory = reports.gut_barrier_report if kind == "explain_gutBarrier" else reports.explain_report
    body, status = await generate(kind, lambda: factory({}, "u", "s"))
    assert status == "success" and body["status"] == "partial_success"
    assert body["preface"]["opening_script"]["self_intro"] == "您好，让我们一起核对本次报告。"
    assert [section["part_no"] for section in body["sections"]] == part_numbers
    for section in body["sections"]:
        if section["part_no"] != 2:
            assert section["script"]["opening"] == "本段保留已核实的说明。"
    assert calls == {**{f"{prefix}_part{part}": 2 if part == 2 else 1 for part in part_numbers}, f"{prefix}_opening": 1}
    assert "diagnostic_summary" not in body
    assert "diagnostic_basis" not in body


@pytest.mark.asyncio
async def test_instruction_uses_fixed_pfmt_usage_and_safety_copy_after_polishing_failure(monkeypatch):
    calls = Counter()

    async def model(prompt, *, task_type):
        calls[task_type] += 1
        raise TimeoutError("polishing unavailable")

    monkeypatch.setattr(reports, "safe_complete_async", model)
    method = "健康人源肠道菌群移植胶囊(PFMT) + 生活方式"
    recommend = {**deepcopy(FOLLOWUP_DEFAULTS["recommend"]), "recommended_combination_method": method,
                 "PFMT": "已核对的目录产品", "pfmt_code": "catalog-pfmt",
                 "pfmt_usageInstructions": "已经核对的目录用法原文"}
    body, _ = await generate("instruction", lambda: reports.instruction_report({}, deepcopy(FOLLOWUP_DEFAULTS["info"]), recommend, "u", "s"))
    assert body["status"] == "partial_success"
    # PFMT 用法用量是产品固定方案，不取目录原文、不调模型
    assert body["usage"] == {"pfmt_usage": list(PFMT_USAGE), "mdf_usage": []}
    assert body["notes"] == notes_base(has_pfmt=True)
    assert body["exception_handling"] == FOLLOWUP_DEFAULTS["exception_handling"]
    products = body["non_drug_intervention"]["gut_microbiota_regulation"]["probiotics"]["products"]
    assert len(products) == 1 and products[0]["code"] == "catalog-pfmt"
    assert products[0]["usage"] == list(PFMT_USAGE)
    assert products[0]["notes"] == notes_base(has_pfmt=True)["pfmt_notes"]
    assert products[0]["storage"] == FOLLOWUP_DEFAULTS["exception_handling"]
    assert calls == {"exception_handling": 2, "notes": 2, "lifestyle": 2,
                     "gut_microbiota": 2, "nutrient_supplements": 2, "recommended_tests": 2}


@pytest.mark.asyncio
async def test_no_product_still_skips_usage_and_exception_model_calls(monkeypatch):
    calls = Counter()

    async def model(prompt, *, task_type):
        calls[task_type] += 1
        if task_type == "lifestyle":
            return '{"diet":{"summary":"保留已确认的饮食说明","points":[]}}'
        if task_type == "gut_microbiota":
            return '{"prebiotics":{"summary":"依据耐受情况核对膳食底物","points":[]}}'
        if task_type == "nutrient_supplements":
            return '{"items":[{"name":"膳食营养支持","reason":"核对已记录的膳食摄入"}]}'
        if task_type == "recommended_tests":
            return '{"items":[{"name":"资料核对","reason":"补充缺失信息","timeframe":"2周内"}]}'
        return "{}"

    monkeypatch.setattr(reports, "safe_complete_async", model)
    recommend = {**deepcopy(FOLLOWUP_DEFAULTS["recommend"]), "recommended_combination_method": "生活方式干预"}
    body, _ = await generate("instruction", lambda: reports.instruction_report({}, deepcopy(FOLLOWUP_DEFAULTS["info"]), recommend, "u", "s"))
    assert body["usage"] == {"pfmt_usage": [], "mdf_usage": []}
    assert body["exception_handling"] == []
    assert calls == {"notes": 1, "lifestyle": 1, "gut_microbiota": 1, "nutrient_supplements": 1, "recommended_tests": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["opinion", "explain", "explain_gutBarrier"])
@pytest.mark.parametrize("response", [
    "{}", "null", "[]", '{"irrelevant":true}', '{"part_no":999}', '{"script":{"opening":123}}',
])
async def test_all_empty_or_unusable_component_responses_remain_failed_with_declared_fields(monkeypatch, kind, response):
    monkeypatch.setattr(reports, "safe_complete_async", AsyncMock(return_value=response))
    factory = reports.gut_barrier_report if kind == "explain_gutBarrier" else getattr(reports, f"{kind}_report")
    body, status = await generate(kind, lambda: factory({}, "u", "s"))
    assert status == "fallback_empty" and body["status"] == "failed"
    if kind == "opinion":
        assert set(body["diagnostic_basis"]) == {"abnormal_findings", "evidence_refs"}
        assert body["diagnostic_basis"]["abnormal_findings"] == []
        assert set(body["diagnostic_advice"]["Plan"]) == {"未来1-2周", "未来1个月", "未来2-3个月", "未来6-12个月"}
    else:
        assert set(body["preface"]) == {"quick_card", "reader_notice", "opening_script"}
        assert [section["part_no"] for section in body["sections"]] == ([1, 2] if kind == "explain_gutBarrier" else [1, 2, 3, 4])
        assert set(body["closing"]) == {"wrap_up_script", "forbidden_words", "self_check"}
        assert "diagnostic_summary" not in body


@pytest.mark.asyncio
async def test_all_model_tasks_unavailable_yield_three_active_failed_reports_not_success(monkeypatch):
    monkeypatch.setattr(reports, "safe_complete_async", AsyncMock(side_effect=TimeoutError("unavailable")))
    monkeypatch.setattr(reports, "fetch_catalog", AsyncMock(return_value=("", [], [], {}, {}, {}, [])))
    monkeypatch.setattr(reports, "replay_followup_reports", AsyncMock(return_value={}))
    result = await reports._push_report({}, "u", "s", policy=ReportFallbackPolicy(llm_attempts=1, retry_backoff_seconds=()))
    assert len(result.payloads) == 3
    assert {body["type"] for body in result.payloads} == {"instruction", "opinion", "explain"}
    assert all(body["status"] == "failed" for body in result.payloads)
    assert all(body["error"]["error_code"] == "REPORT_GENERATION_FAILED" for body in result.payloads)


@pytest.mark.asyncio
async def test_successful_component_is_cached_across_envelope_retry_and_gaps_replayed(monkeypatch):
    calls = Counter()
    attempts = 0

    async def model(prompt, *, task_type):
        calls[task_type] += 1
        if task_type == "opinion_summary":
            return '{"conclusion":"不可丢失或重新生成的结论"}'
        if task_type == "opinion_basis":
            raise RuntimeError("basis unavailable")
        return "{}"

    async def malformed_first_envelope():
        nonlocal attempts
        attempts += 1
        body = await reports.opinion_report({}, "u", "s")
        if attempts == 1:
            body["type"] = "wrong-envelope"
        return body

    monkeypatch.setattr(reports, "safe_complete_async", model)
    body, status = await generate("opinion", malformed_first_envelope)
    assert attempts == 2 and status == "retried_success"
    assert body["status"] == "partial_success"
    assert body["diagnosis_summary"]["conclusion"] == "不可丢失或重新生成的结论"
    assert calls == {"opinion_summary": 1, "opinion_basis": 2, "opinion_advice": 1}


@pytest.mark.asyncio
async def test_component_retry_success_does_not_retain_failed_attempt_gap():
    gaps, calls = set(), 0
    parent_token = reports._ACTIVE_FOLLOWUP_GAPS.set(gaps)
    policy_token = reports._ACTIVE_FOLLOWUP_COMPONENT_POLICY.set(POLICY)

    async def factory():
        nonlocal calls
        calls += 1
        if calls == 1:
            reports._ACTIVE_FOLLOWUP_GAPS.get().add("failed_attempt_only")
            raise RuntimeError("try once more")
        return {"known": "confirmed", "flag": False, "value": 0}

    try:
        result = await reports._followup_component("example", factory, {"known": None, "flag": True, "value": 1})
    finally:
        reports._ACTIVE_FOLLOWUP_COMPONENT_POLICY.reset(policy_token)
        reports._ACTIVE_FOLLOWUP_GAPS.reset(parent_token)
    assert result == {"known": "confirmed", "flag": False, "value": 0}
    assert calls == 2 and gaps == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["owner", "component"])
async def test_cancellation_propagates_and_joins_component_tasks_without_archiving(monkeypatch, source):
    summary_started, basis_started = asyncio.Event(), asyncio.Event()
    summary_stopped, basis_stopped = asyncio.Event(), asyncio.Event()
    archive = AsyncMock()
    monkeypatch.setattr(reports, "_archive_finished_followup", archive)

    async def model(prompt, *, task_type):
        if task_type == "opinion_summary":
            summary_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                summary_stopped.set()
        elif task_type == "opinion_basis":
            basis_started.set()
            try:
                if source == "component":
                    await summary_started.wait()
                    raise asyncio.CancelledError()
                await asyncio.Event().wait()
            finally:
                basis_stopped.set()
        else:
            pytest.fail("advice cannot start after cancellation")

    monkeypatch.setattr(reports, "safe_complete_async", model)
    task = asyncio.create_task(generate("opinion", lambda: reports.opinion_report({}, "u", "s")))
    await asyncio.wait_for(asyncio.gather(summary_started.wait(), basis_started.wait()), timeout=2)
    if source == "owner":
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert summary_stopped.is_set() and basis_stopped.is_set()
    archive.assert_not_awaited()
    assert reports._ACTIVE_FOLLOWUP_GAPS.get() is None
    assert reports._ACTIVE_FOLLOWUP_COMPONENT_CACHE.get() is None
    assert reports._ACTIVE_FOLLOWUP_COMPONENT_POLICY.get() == DEFAULT_REPORT_FALLBACK_POLICY


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["owner", "component"])
@pytest.mark.parametrize("kind,component_names,cancelling_component", [
    (
        "instruction",
        ("notes", "lifestyle", "gut_microbiota", "nutrient_supplements", "recommended_tests"),
        "gut_microbiota",
    ),
    (
        "explain",
        ("explain_part1", "explain_part2", "explain_part3", "explain_part4", "explain_opening"),
        "explain_part2",
    ),
    (
        "explain_gutBarrier",
        ("gut_barrier_part1", "gut_barrier_part2", "gut_barrier_opening"),
        "gut_barrier_part2",
    ),
])
async def test_active_report_cancellation_joins_all_siblings_and_resets_owner_context(
    monkeypatch, source, kind, component_names, cancelling_component,
):
    started = {name: asyncio.Event() for name in component_names}
    stopped = {name: asyncio.Event() for name in component_names}
    never_finishes = asyncio.Event()
    calls = Counter()
    owner_context_after_cancellation = []
    archive = AsyncMock()
    monkeypatch.setattr(reports, "_archive_finished_followup", archive)

    async def model(prompt, *, task_type):
        assert task_type in started, f"unexpected component: {task_type}"
        calls[task_type] += 1
        started[task_type].set()
        try:
            if source == "component" and task_type == cancelling_component:
                # In instruction this is a nested child: cancellation must
                # join both its inner siblings and the outer report tasks.
                await asyncio.gather(*(event.wait() for event in started.values()))
                raise asyncio.CancelledError()
            await never_finishes.wait()
        finally:
            stopped[task_type].set()

    monkeypatch.setattr(reports, "safe_complete_async", model)
    if kind == "instruction":
        factory = lambda: reports.instruction_report(
            {}, deepcopy(FOLLOWUP_DEFAULTS["info"]),
            deepcopy(FOLLOWUP_DEFAULTS["recommend"]), "u", "s",
        )
    else:
        report_factory = reports.gut_barrier_report if kind == "explain_gutBarrier" else reports.explain_report
        factory = lambda: report_factory({}, "u", "s")

    async def owner():
        try:
            return await generate(kind, factory)
        finally:
            # Inspect the cancelled owner's context, not only the unrelated
            # parent test task's context (which is isolated by asyncio).
            owner_context_after_cancellation.append((
                reports._ACTIVE_FOLLOWUP_GAPS.get(),
                reports._ACTIVE_FOLLOWUP_COMPONENT_CACHE.get(),
                reports._ACTIVE_FOLLOWUP_COMPONENT_POLICY.get(),
            ))

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), timeout=2)
        if source == "owner":
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert all(event.is_set() for event in stopped.values())
    assert calls == {name: 1 for name in component_names}
    archive.assert_not_awaited()
    assert owner_context_after_cancellation == [(None, None, DEFAULT_REPORT_FALLBACK_POLICY)]
    assert reports._ACTIVE_FOLLOWUP_GAPS.get() is None
    assert reports._ACTIVE_FOLLOWUP_COMPONENT_CACHE.get() is None
    assert reports._ACTIVE_FOLLOWUP_COMPONENT_POLICY.get() == DEFAULT_REPORT_FALLBACK_POLICY


@pytest.mark.asyncio
async def test_parallel_reports_do_not_share_cached_components_or_partial_status(monkeypatch):
    calls = Counter()

    def complete_fields(value):
        if isinstance(value, dict):
            return {key: complete_fields(item) for key, item in value.items()}
        if isinstance(value, list):
            return [complete_fields(item) for item in value]
        return "已核实的说明" if value is None else value

    async def model(prompt, *, task_type):
        source = "first" if "source-first" in prompt else "second"
        calls[source, task_type] += 1
        response = complete_fields(FOLLOWUP_DEFAULTS[task_type])
        if task_type == "opinion_summary":
            response["conclusion"] = source
        if task_type == "opinion_basis":
            response["abnormal_findings"] = [{"problem": f"{source}已核实的问题", "evidence_refs": []}]
        if source == "second" and task_type == "opinion_basis":
            raise RuntimeError("only the second report is incomplete")
        return json.dumps(response, ensure_ascii=False)

    monkeypatch.setattr(reports, "safe_complete_async", model)
    first, second = await asyncio.gather(
        generate("opinion", lambda: reports.opinion_report({"source": "source-first"}, "u", "s")),
        generate("opinion", lambda: reports.opinion_report({"source": "source-second"}, "u", "s")),
    )
    assert first[0]["diagnosis_summary"]["conclusion"] == "first"
    assert second[0]["diagnosis_summary"]["conclusion"] == "second"
    assert first[0]["status"] == "success"
    assert second[0]["status"] == "partial_success"
    assert calls["first", "opinion_summary"] == calls["second", "opinion_summary"] == 1
    assert calls["first", "opinion_basis"] == 1 and calls["second", "opinion_basis"] == 2
    assert reports._ACTIVE_FOLLOWUP_COMPONENT_CACHE.get() is None
