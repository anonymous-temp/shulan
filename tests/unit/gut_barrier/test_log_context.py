"""Existing gut-barrier logs retain persisted ownership across async boundaries."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.gut_barrier import service
from modules.report import durable
from modules.report.durable_ports import CallbackResponse
from tests.unit.gut_barrier.test_service import environment
from tests.unit.gut_barrier.test_service import request
from utils.log.context import LogContext
from utils.log.context import bind_log_context
from utils.log.context import log_context


@pytest.mark.asyncio
async def test_generation_heartbeat_followups_and_callbacks_bind_each_job(monkeypatch):
    seen = []
    response = CallbackResponse(204)

    async def send(url, body, *, idempotency_key):
        seen.append(("callback", body["userId"], log_context()))
        assert log_context().task_id == idempotency_key
        return response

    env = environment(
        callback_url="https://receiver.invalid/callback",
        transport=SimpleNamespace(post_once=AsyncMock(side_effect=send)),
    )
    env.service.heartbeat_seconds = 0.001
    generate = env.pipeline.generate
    renew = env.store.renew_generation
    complete = env.store.complete

    async def generate_with_context(value, **kwargs):
        assert await asyncio.to_thread(log_context) == log_context()
        seen.append(("generation", value.user_id, log_context()))
        await asyncio.sleep(0.01)
        return await generate(value, **kwargs)

    async def renew_with_context(job):
        seen.append(("heartbeat", job["userId"], log_context()))
        return await renew(job)

    async def complete_with_context(job, **kwargs):
        seen.append(("complete", job["userId"], log_context()))
        return await complete(job, **kwargs)

    async def followup(body, user_id, session_id):
        seen.append(("followup", user_id, log_context()))
        assert session_id == log_context().session_id

    monkeypatch.setattr(env.pipeline, "generate", generate_with_context)
    monkeypatch.setattr(env.store, "renew_generation", renew_with_context)
    monkeypatch.setattr(env.store, "complete", complete_with_context)
    env.service.followup_handler = followup
    jobs = [
        await env.service.submit(request(user).model_copy(update={"session_id": f"s-{user}"}))
        for user in ("a", "b")
    ]
    with bind_log_context(user_id="trigger", request_uuid="trigger-request") as caller:
        assert await env.service.drain_generation() == 2
        assert await env.service.drain_delivery() == 2
        assert log_context() == caller

    expected = {job["userId"]: job["_id"] for job in jobs}
    assert {stage for stage, _, _ in seen} == {"generation", "heartbeat", "complete", "followup", "callback"}
    for stage, user_id, context in seen:
        assert context.user_id == user_id
        assert context.session_id == f"s-{user_id}"
        assert context.request_uuid is None
        assert context.task_id == (
            expected[user_id] + ":gutBarrier:v2" if stage == "callback" else expected[user_id]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [204, 503, "connection", "acknowledgement"])
async def test_callback_confirmation_and_failures_keep_delivery_owner(monkeypatch, outcome):
    observed = []

    def capture(message, *args):
        observed.append((message % args, log_context()))

    for level in ("info", "warning", "error"):
        monkeypatch.setattr(durable.logger, level, capture)
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(
        outcome if isinstance(outcome, int) else 204,
    )))
    if outcome == "connection":
        transport.post_once.side_effect = ConnectionError("offline failure")
    env = environment(callback_url="https://receiver.invalid/callback", transport=transport)
    jobs = [
        await env.service.submit(request(user).model_copy(update={"session_id": f"s-{user}"}))
        for user in ("a", "b")
    ]
    await env.service.drain_generation()
    if outcome == "acknowledgement":
        monkeypatch.setattr(env.store, "acknowledge_delivery", AsyncMock(side_effect=ConnectionError()))

    with bind_log_context(user_id="trigger", request_uuid="request") as caller:
        assert await env.service.drain_delivery() == 2
        assert log_context() == caller

    assert all(message.endswith(" callback_scope=gut_barrier") for message, _ in observed)
    deliveries = [(message, context) for message, context in observed if "积压" not in message]
    assert len(deliveries) == 2
    expected = {
        job["_id"] + ":gutBarrier:v2": (job["userId"], job["sessionId"]) for job in jobs
    }
    for _, context in deliveries:
        assert (context.user_id, context.session_id) == expected[context.task_id]
        assert context.request_uuid is None
    assert [context for message, context in observed if "积压" in message] == [LogContext()]
    state = "delivered" if outcome == 204 else "retry_wait"
    assert all(row["deliveryStatus"] == state for row in env.collection.documents.values())


@pytest.mark.asyncio
async def test_recovery_and_followup_failure_keep_job_owner(monkeypatch):
    env = environment()
    jobs = [
        await env.service.submit(request(user).model_copy(update={"session_id": f"s-{user}"}))
        for user in ("a", "b")
    ]
    for _ in jobs:
        await env.store.claim_generation()
    env.clock.advance(121)
    observed = []
    monkeypatch.setattr(service.logger, "error", lambda *args: observed.append(log_context()))
    env.service.followup_handler = AsyncMock(side_effect=RuntimeError("offline followup failure"))

    with bind_log_context(user_id="trigger", request_uuid="request") as caller:
        assert await env.service.recover_pending() == 2
        assert log_context() == caller

    assert env.pipeline.generate.await_count == 0
    assert observed == [
        LogContext(user_id=job["userId"], session_id=job["sessionId"], task_id=job["_id"])
        for job in jobs
    ]
    assert all(row["body"]["error"]["code"] == "GENERATION_INTERRUPTED"
               for row in env.collection.documents.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", ["owner", None])
async def test_unstarted_release_failure_uses_job_identity_or_system(monkeypatch, user_id):
    env = environment()
    monkeypatch.setattr(env.store, "release_generation", AsyncMock(side_effect=ConnectionError()))
    observed = []
    monkeypatch.setattr(service.logger, "warning", lambda *args: observed.append(log_context()))
    job = {"_id": "known-unstarted"}
    if user_id is not None:
        job.update(userId=user_id, sessionId="session")

    with bind_log_context(user_id="trigger", request_uuid="request") as caller:
        assert not await env.service._release_unstarted(job)
        assert log_context() == caller

    assert observed == [LogContext(
        user_id=user_id, session_id="session" if user_id else None, task_id=job["_id"],
    )]


@pytest.mark.asyncio
async def test_claim_retry_and_coordinator_failure_are_system_owned(monkeypatch):
    env = environment()
    monkeypatch.setattr(env.store, "claim_generation", AsyncMock(side_effect=[
        ConnectionError("offline claim failure"), None,
    ]))
    observed = []
    monkeypatch.setattr(service.logger, "warning", lambda *args: observed.append(log_context()))
    with bind_log_context(user_id="trigger", request_uuid="request") as caller:
        with pytest.raises(ConnectionError, match="offline claim failure"):
            await env.service.drain_generation()
        assert log_context() == caller
    assert observed == [LogContext()]


@pytest.mark.asyncio
async def test_cancelled_generation_restores_caller_context():
    env = environment()
    job = await env.service.submit(request("owner"))
    claimed = await env.store.claim_generation()
    env.pipeline.generate.side_effect = asyncio.CancelledError()
    with bind_log_context(user_id="trigger", request_uuid="request") as caller:
        with pytest.raises(asyncio.CancelledError):
            await env.service._generate(claimed)
        assert log_context() == caller
    assert env.collection.documents[job["_id"]]["status"] == "generating"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_session", [True, False])
async def test_delivery_view_uses_available_session_without_inheriting_caller(has_session):
    observed = []

    async def send(*args, **kwargs):
        observed.append(log_context())
        return CallbackResponse(204)

    env = environment(
        callback_url="https://receiver.invalid/callback",
        transport=SimpleNamespace(post_once=AsyncMock(side_effect=send)),
    )
    job = await env.service.submit(request("owner"))
    await env.service.drain_generation()
    if not has_session:
        env.collection.documents[job["_id"]].pop("sessionId")
    delivery = await env.store.claim_delivery("offline-worker")
    with bind_log_context(user_id="trigger", session_id="wrong-session", request_uuid="request"):
        await env.worker._deliver(delivery)
    assert observed == [LogContext(
        user_id="owner", session_id="s" if has_session else None, task_id=delivery["_id"],
    )]
    assert env.collection.documents[job["_id"]]["deliveryStatus"] == "delivered"


@pytest.mark.asyncio
async def test_existing_report_interpretation_logs_and_child_tasks_keep_main_owner(monkeypatch):
    from modules.report import reports
    from modules.report.artifacts import bind_report_batch
    from modules.report.artifacts import reset_report_batch
    from modules.report.fallback_policy import ReportFallbackPolicy
    from modules.report.followup_defaults import FOLLOWUP_DEFAULTS
    from modules.report.followup_defaults import default_followup_report
    from modules.report.gut_barrier_defaults import default_gut_barrier_manual

    observed, generated, delivered, generated_followups = [], [], [], []

    def capture(message, *args):
        observed.append((message, log_context()))

    monkeypatch.setattr(reports.logger, "info", capture)
    monkeypatch.setattr(reports.logger, "warning", capture)

    async def interpretation(body, user_id, session_id):
        generated.append(log_context())
        assert await asyncio.to_thread(log_context) == log_context()
        manual = default_gut_barrier_manual(
            evaluate={}, user_id=user_id, session_id=session_id,
        )
        manual["preface"]["opening_script"]["self_intro"] = "测试解读开场内容"
        return manual

    async def deliver(body, *, timeout_seconds):
        delivered.append((body, log_context()))
        return 204

    async def followup_report(report_type, data, user_id, session_id):
        # opinion / instruction 子任务同样要继承主任务的日志归属
        generated_followups.append((report_type, log_context()))
        body = default_followup_report(report_type, evaluate={}, user_id=user_id, session_id=session_id)
        if report_type == "opinion":
            body["diagnosis_summary"]["conclusion"] = "测试诊断结论"
        else:
            body["info"]["body_mass"] = "测试体重说明"
        return body

    async def opinion_report(data, user_id, session_id):
        return await followup_report("opinion", data, user_id, session_id)

    async def instruction_report(data, info, recommend, user_id, session_id):
        return await followup_report("instruction", data, user_id, session_id)

    monkeypatch.setattr(reports, "gut_barrier_report", interpretation)
    monkeypatch.setattr(reports, "opinion_report", opinion_report)
    monkeypatch.setattr(reports, "instruction_report", instruction_report)
    monkeypatch.setattr(reports, "_info", AsyncMock(return_value=deepcopy(FOLLOWUP_DEFAULTS["info"])))
    monkeypatch.setattr(reports, "fetch_catalog", AsyncMock(return_value=("", [], [], {}, {}, {}, [])))
    monkeypatch.setattr(reports, "_recommend", AsyncMock(return_value={
        **deepcopy(FOLLOWUP_DEFAULTS["recommend"]), "recommended_combination_method": "生活方式干预",
    }))
    gateway = SimpleNamespace(deliver_json=AsyncMock(side_effect=deliver))

    async def followup(body, user_id, session_id):
        token = bind_report_batch(
            store=None, final_report_id=body["finalReportId"], user_id=user_id, session_id=session_id,
        )
        try:
            await reports._push_report(
                body, user_id, session_id, gateway=gateway,
                policy=ReportFallbackPolicy(retry_backoff_seconds=()),
            )
        finally:
            reset_report_batch(token)

    env = environment()
    env.service.followup_handler = followup
    jobs = [await env.service.submit(request(user)) for user in ("a", "b")]
    with bind_log_context(user_id="trigger", request_uuid="request") as caller:
        assert await env.service.drain_generation() == 2
        assert log_context() == caller

    expected = {
        job["userId"]: LogContext(user_id=job["userId"], session_id="s", task_id=job["_id"])
        for job in jobs
    }
    # 每个任务生成 explain_gutBarrier / opinion / instruction 三份
    assert len(generated) == 2 and len(delivered) == 6 and len(generated_followups) == 4
    assert set(generated) == set(expected.values())
    assert {context for _, context in generated_followups} == set(expected.values())
    for user_id, context in expected.items():
        types = sorted(body["type"] for body, ctx in delivered if body["userId"] == user_id)
        assert types == ["explain_gutBarrier", "instruction", "opinion"]
    for body, context in delivered:
        assert body["finalReportId"] == context.task_id
        assert context == expected[body["userId"]]
    for message in ("starting gut barrier interpretation: %s", "gut barrier workflow completed: %s"):
        contexts = [context for text, context in observed if text == message]
        assert len(contexts) == 2 and set(contexts) == set(expected.values())
    assert all(context == expected[context.user_id] for _, context in observed)
