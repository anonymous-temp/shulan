"""The saved main JSON enters the gut-barrier follow-up branch (explain_gutBarrier / opinion / instruction)."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.gut_barrier.store import MongoGutBarrierStore
from modules.gut_barrier.schemas import GutBarrierRequest
from modules.gut_barrier.service import GutBarrierService
from modules.report.durable import ReportCallbackWorker
from modules.report.durable_ports import CallbackResponse
from tests.unit.report.durable_fakes import Clock, Collection


OUTCOMES = (
    ("success", "success", None),
    ("partial", "partial", None),
    ("failed", "failed", "GENERATION_FAILED"),
    ("timeout", "failed", "GENERATION_TIMEOUT"),
    ("recovery_failed", "failed", "GENERATION_INTERRUPTED"),
)


def make_environment(*, handler=None, outcome="success", transport=None):
    clock, collection = Clock(), Collection()
    store = MongoGutBarrierStore(collection, clock=clock)
    report = {"meta": {"title": "测试肠道屏障报告"}, "sections": []}
    audit = {"partial": outcome == "partial", "privateTestAudit": "not public"}
    pipeline = SimpleNamespace(
        version="followup-handoff-test", generate=AsyncMock(return_value=(report, audit)),
    )
    if outcome == "failed":
        pipeline.generate.side_effect = RuntimeError("private model error")
    elif outcome == "timeout":
        async def never_finishes(*args, **kwargs):
            await asyncio.Event().wait()
        pipeline.generate.side_effect = never_finishes
    worker = ReportCallbackWorker(
        store=store, transport=transport, clock=clock, jitter=lambda: 1,
    ) if transport is not None else None
    service = GutBarrierService(
        store=store, pipeline=pipeline, followup_handler=handler,
        callback_url="https://callback.invalid/main", transport=transport,
        callback_worker=worker, timeout_seconds=0.005 if outcome == "timeout" else 10,
    )
    return SimpleNamespace(
        clock=clock, collection=collection, store=store, pipeline=pipeline,
        service=service, worker=worker,
    )


def request():
    return GutBarrierRequest.model_validate({
        "userId": "test-followup-user", "sessionId": "test-followup-session",
        "name": "合成测试受检者", "age": "28", "sex": "男",
        "fileList": [], "testData": [], "hospitalData": [], "microbiomeData": None,
        "gutHealthIndicator": {
            "dLacticAcid": {"value": "17.42", "unit": "mg/L"},
            "diamineOxidase": {"value": "10.5", "unit": "U/L"},
            "bacterialEndotoxin": {"value": "15.9", "unit": "U/L"},
            "hsCRP": {"value": "2.66", "unit": "mg/L"},
        },
    })


async def finish_main(env, outcome):
    job = await env.service.submit(request())
    if outcome == "recovery_failed":
        claimed = await env.store.claim_generation()
        assert await env.store.mark_generation_started(claimed)
        env.clock.advance(env.store.lease_seconds + 1)
        assert await env.service.recover_pending() == 1
    else:
        assert await env.service.drain_generation() == 1
    return env.collection.documents[job["_id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,status,error_code", OUTCOMES)
async def test_every_terminal_main_hands_off_a_copy_of_saved_public_json(outcome, status, error_code):
    env = make_environment(outcome=outcome)
    completed_arguments, received = [], []
    original_complete = env.store.complete

    async def complete(job, **kwargs):
        completed_arguments.append(kwargs["body"])
        return await original_complete(job, **kwargs)

    async def handler(body, user_id, session_id):
        persisted = env.collection.documents[body["finalReportId"]]
        assert persisted["status"] == status
        assert body == persisted["body"] == completed_arguments[0]
        assert body is not completed_arguments[0]
        assert (user_id, session_id) == (body["userId"], body["sessionId"])
        assert body["type"] == "gutBarrier" and body["schemaVersion"] == "2.0"
        assert not {"audit", "request", "reportDocument", "callbackUrl"} & body.keys()
        received.append(deepcopy(body))
        # A downstream implementation may normalize in place; it must not
        # corrupt the frozen main callback, even via a nested reference.
        if body["report"] is not None:
            body["report"]["meta"]["title"] = "downstream mutation"
        else:
            body["error"]["message"] = "downstream mutation"
        body["status"] = "downstream mutation"

    env.store.complete = complete
    env.service.followup_handler = AsyncMock(side_effect=handler)
    saved = await finish_main(env, outcome)
    env.service.followup_handler.assert_awaited_once()
    assert completed_arguments == received == [saved["body"]]
    assert saved["body"]["status"] == status
    assert (saved["body"]["error"] or {}).get("code") == error_code
    assert len(env.collection.documents) == 1
    assert env.pipeline.generate.await_count == (0 if outcome == "recovery_failed" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,status,error_code", OUTCOMES)
async def test_failed_followup_does_not_replace_any_main_terminal_result(outcome, status, error_code):
    handler = AsyncMock(side_effect=RuntimeError("downstream failed"))
    env = make_environment(handler=handler, outcome=outcome)
    saved = await finish_main(env, outcome)
    handler.assert_awaited_once()
    assert saved["status"] == status
    assert saved["body"]["status"] == status
    assert (saved["body"]["error"] or {}).get("code") == error_code
    assert "downstream failed" not in str(saved)
    assert len(env.collection.documents) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("save_outcome", ("lease_rejected", "database_error"))
async def test_followup_is_not_called_without_successfully_frozen_main(save_outcome):
    handler = AsyncMock()
    env = make_environment(handler=handler)
    job = await env.service.submit(request())
    claimed = await env.store.claim_generation()
    env.store.complete = AsyncMock(return_value=False)
    if save_outcome == "database_error":
        env.store.complete.side_effect = ConnectionError("save unavailable")
        with pytest.raises(ConnectionError, match="save unavailable"):
            await env.service._generate(claimed)
    else:
        await env.service._generate(claimed)
    handler.assert_not_awaited()
    assert env.collection.documents[job["_id"]]["status"] == "generating"


@pytest.mark.asyncio
async def test_callback_failure_and_duplicate_admission_do_not_regenerate_followup():
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(503)))
    handler = AsyncMock()
    env = make_environment(handler=handler, transport=transport)
    saved = await finish_main(env, "success")
    frozen = deepcopy(saved["body"])
    # No main callback delivery is required before handing off interpretation.
    handler.assert_awaited_once_with(frozen, frozen["userId"], frozen["sessionId"])
    transport.post_once.assert_not_awaited()
    assert await env.service.drain_delivery() == 1
    assert env.collection.documents[saved["_id"]]["deliveryStatus"] == "retry_wait"
    env.clock.advance(31)
    transport.post_once.return_value = CallbackResponse(204)
    assert await env.service.drain_delivery() == 1
    duplicate = await env.service.submit(request())
    assert duplicate["_id"] == saved["_id"]
    assert await env.service.drain_generation() == 0
    await env.service.drain_delivery()
    handler.assert_awaited_once()
    env.pipeline.generate.assert_awaited_once()
    assert env.collection.documents[saved["_id"]]["body"] == frozen
    assert len(env.collection.documents) == 1


@pytest.mark.asyncio
async def test_terminal_generation_heartbeat_cannot_cancel_slow_followup():
    handler_started, finish_handler = asyncio.Event(), asyncio.Event()

    async def handler(body, user_id, session_id):
        handler_started.set()
        await finish_handler.wait()

    env = make_environment(handler=AsyncMock(side_effect=handler))
    # A terminal main job cannot renew its generation lease. A leftover
    # heartbeat would cancel its owner while the interpretation is running.
    env.service.heartbeat_seconds = 0.001
    env.store.renew_generation = AsyncMock(return_value=False)
    await env.service.submit(request())
    running = asyncio.create_task(env.service.drain_generation())
    try:
        await asyncio.wait_for(handler_started.wait(), 1)
        await asyncio.sleep(0.025)
        assert not running.done()
        env.store.renew_generation.assert_not_awaited()
    finally:
        finish_handler.set()
        assert await asyncio.wait_for(running, 1) == 1
    env.service.followup_handler.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,status,error_code", OUTCOMES)
async def test_existing_push_report_dispatches_gut_followups_for_each_outcome(
    monkeypatch, outcome, status, error_code,
):
    """type=gutBarrier 生成 explain_gutBarrier / opinion / instruction 三份，不生成 explain。"""
    from modules.report import reports
    from modules.report.artifacts import bind_report_batch, reset_report_batch
    from modules.report.fallback_policy import ReportFallbackPolicy
    from modules.report.followup_defaults import FOLLOWUP_DEFAULTS
    from modules.report.followup_defaults import default_followup_report
    from modules.report.gut_barrier_defaults import default_gut_barrier_manual

    # 评估报告的解读手册（explain）不属于 gutBarrier 的产物
    forbidden = AsyncMock(side_effect=AssertionError("explain_report must not run"))
    monkeypatch.setattr(reports, "explain_report", forbidden)

    # Use the real manual shape, including the ordered sections array. The
    # generator is stubbed here; schema validation and dispatch remain real.
    interpretation = default_gut_barrier_manual(
        evaluate={}, user_id="test-followup-user", session_id="test-followup-session",
    )
    interpretation["preface"]["opening_script"]["self_intro"] = "测试解读开场内容"
    generate = AsyncMock(return_value=interpretation)
    monkeypatch.setattr(reports, "gut_barrier_report", generate)

    opinion = default_followup_report(
        "opinion", evaluate={}, user_id="test-followup-user", session_id="test-followup-session",
    )
    opinion["diagnosis_summary"]["conclusion"] = "测试诊断结论"
    opinion_report = AsyncMock(return_value=opinion)
    monkeypatch.setattr(reports, "opinion_report", opinion_report)

    info = deepcopy(FOLLOWUP_DEFAULTS["info"])
    recommend = {**deepcopy(FOLLOWUP_DEFAULTS["recommend"]), "recommended_combination_method": "生活方式干预"}
    instruction = default_followup_report(
        "instruction", evaluate={}, user_id="test-followup-user", session_id="test-followup-session",
    )
    instruction["info"]["body_mass"] = "测试体重说明"
    monkeypatch.setattr(reports, "_info", AsyncMock(return_value=info))
    monkeypatch.setattr(reports, "fetch_catalog", AsyncMock(return_value=("", [], [], {}, {}, {}, [])))
    monkeypatch.setattr(reports, "_recommend", AsyncMock(return_value=recommend))
    instruction_report = AsyncMock(return_value=instruction)
    monkeypatch.setattr(reports, "instruction_report", instruction_report)

    gateway = SimpleNamespace(
        deliver_json=AsyncMock(return_value=204),
        fetch_product_catalog=AsyncMock(side_effect=AssertionError("catalog is stubbed above")),
    )
    model = SimpleNamespace(complete_text=AsyncMock(side_effect=AssertionError("model not used in offline test")))
    execution_results = []

    async def existing_handler(body, user_id, session_id):
        token = bind_report_batch(
            store=None, final_report_id=body["finalReportId"], user_id=user_id, session_id=session_id,
        )
        try:
            execution_results.append(await reports._push_report(
                body, user_id, session_id, gateway=gateway, model_gateway=model,
                policy=ReportFallbackPolicy(retry_backoff_seconds=()),
            ))
        finally:
            reset_report_batch(token)

    env = make_environment(handler=existing_handler, outcome=outcome)
    saved = await finish_main(env, outcome)
    assert saved["status"] == status
    assert (saved["body"]["error"] or {}).get("code") == error_code

    # explain_gutBarrier 读源报告原文
    generate.assert_awaited_once_with(saved["body"], saved["userId"], saved["sessionId"])
    # opinion / instruction 读转换后的评估报告字段结构，来源仍标记为 gutBarrier
    opinion_report.assert_awaited_once()
    instruction_report.assert_awaited_once()
    followup_input = opinion_report.await_args.args[0]
    assert followup_input["type"] == "gutBarrier"
    assert set(followup_input) >= {"subject", "sample", "healthAssessment", "diagnostic_basis", "evidence"}
    assert instruction_report.await_args.args[0] == followup_input
    forbidden.assert_not_awaited()
    gateway.fetch_product_catalog.assert_not_awaited()
    model.complete_text.assert_not_awaited()

    delivered = [call.args[0] for call in gateway.deliver_json.await_args_list]
    assert [body["type"] for body in delivered] == ["explain_gutBarrier", "opinion", "instruction"]
    assert all(body["finalReportId"] == saved["_id"] for body in delivered)
    assert len(execution_results) == 1
    assert execution_results[0].payloads == tuple(delivered)
    assert set(execution_results[0].module_statuses) == {
        "explain_gutBarrier", "opinion", "instruction", "dependencies",
    }
    assert len(env.collection.documents) == 1
