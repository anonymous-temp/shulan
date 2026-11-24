import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.gut_barrier.store import MongoGutBarrierStore
from modules.gut_barrier.schemas import GutBarrierRequest
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.service import GutBarrierService, evaluation_day
from modules.report.durable import ReportCallbackWorker
from modules.report.durable_ports import CallbackResponse
from tests.unit.report.durable_fakes import Clock, Collection


def environment(*, pipeline=None, callback_url="", transport=None, callback_batch_size=100):
    clock, collection = Clock(), Collection()
    store = MongoGutBarrierStore(collection, clock=clock)
    pipeline = pipeline or SimpleNamespace(version="test-v1", generate=AsyncMock(
        return_value=({"meta": {"title": "测试报告"}}, {"partial": False})))
    worker = ReportCallbackWorker(store=store, transport=transport, clock=clock, jitter=lambda: 1,
                                  batch_size=callback_batch_size, callback_scope="gut_barrier") if transport else None
    service = GutBarrierService(store=store, pipeline=pipeline,
                                callback_url=callback_url, transport=transport, callback_worker=worker)
    return SimpleNamespace(clock=clock, collection=collection, store=store, pipeline=pipeline,
                           service=service, worker=worker)


def request(user="u", value="19"):
    return GutBarrierRequest.model_validate({"userId": user, "sessionId": "s", "gutHealthIndicator": {
        "dLacticAcid": {"value": value, "unit": "mg/L"}}})


def test_evaluation_day_treats_mongo_naive_dates_as_utc():
    from datetime import datetime, UTC
    assert evaluation_day(datetime(2026, 9, 23, 18)) == "2026-09-24"
    assert evaluation_day(datetime(2026, 9, 23, 18, tzinfo=UTC)) == "2026-09-24"


@pytest.mark.asyncio
async def test_new_worker_does_not_generate_old_queued_job_with_new_rules():
    env = environment()
    old = await env.service.submit(request())
    env.pipeline.version = "next-generation-version"
    new = await env.service.submit(request())
    assert await env.service.drain_generation() == 1
    assert env.collection.documents[new["_id"]]["status"] == "success"
    assert env.collection.documents[old["_id"]]["status"] == "queued"
    assert env.pipeline.generate.await_count == 1
    env.pipeline.version = "test-v1"
    assert await env.service.drain_generation() == 1
    assert env.collection.documents[old["_id"]]["status"] == "success"


@pytest.mark.asyncio
async def test_running_request_is_not_duplicated_when_calendar_day_changes():
    env = environment()
    first = await env.service.submit(request())
    env.clock.advance(86400)
    second = await env.service.submit(request())
    assert first["_id"] == second["_id"] and len(env.collection.documents) == 1


@pytest.mark.asyncio
async def test_admission_is_durable_and_same_request_replays_frozen_result():
    env = environment()
    first, same = await asyncio.gather(env.service.submit(request()), env.service.submit(request()))
    assert first["_id"] == same["_id"] and len(env.collection.documents) == 1
    assert env.pipeline.generate.await_count == 0
    await env.service.drain_generation()
    await env.service.drain_generation()
    assert env.pipeline.generate.await_count == 1
    replay = await env.service.submit(request())
    assert replay["_id"] == first["_id"] and replay["status"] == "success"
    stored = env.collection.documents[first["_id"]]
    assert "html" not in stored
    assert stored["body"]["type"] == "gutBarrier"
    assert stored["body"]["schemaVersion"] == "2.0"
    internal = stored["audit"]["reportDocument"]
    assert stored["body"]["report"] == project_callback_report(internal)
    assert all(field not in stored["body"] for field in ("request", "audit", "callbackUrl"))


@pytest.mark.asyncio
async def test_owner_isolation_payload_changes_and_generation_version_are_isolated():
    env = environment()
    a = await env.service.submit(request())
    b = await env.service.submit(request(value="20"))
    c = await env.service.submit(request(user="someone_else"))
    env.pipeline.version = "test-v2"
    d = await env.service.submit(request())
    assert len({item["_id"] for item in (a, b, c, d)}) == 4
    assert env.collection.documents[a["_id"]]["userId"] == "u"
    assert env.collection.documents[c["_id"]]["userId"] == "someone_else"
    assert len(env.collection.documents) == 4


@pytest.mark.asyncio
async def test_lease_recovery_and_fencing_prevent_stale_completion():
    env = environment()
    await env.service.submit(request())
    old = await env.store.claim_generation()
    env.clock.advance(121)
    assert await env.store.claim_generation() is None
    new = await env.store.claim_generation(expired=True)
    assert old["leaseToken"] != new["leaseToken"]
    assert not await env.store.complete(old, body={"status": "success"}, audit={})
    assert await env.store.complete(new, body={"status": "success"}, audit={})
    assert await env.store.claim_generation() is None


@pytest.mark.asyncio
async def test_lazy_callback_worker_uses_gut_barrier_scope_and_is_reused(monkeypatch):
    from modules.report import durable

    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(204)))
    env = environment(transport=transport)
    env.service.callback_worker = None
    messages = []

    def capture(message, *args):
        messages.append(message % args)

    monkeypatch.setattr(durable.logger, 'info', capture)
    assert await env.service.drain_delivery() == 0
    callback_worker = env.service.callback_worker
    assert callback_worker.callback_scope == 'gut_barrier'
    assert await env.service.drain_delivery() == 0
    assert env.service.callback_worker is callback_worker
    assert messages == [
        '报告回调积压: pending=0 oldest_pending_at=None oldest_wait_seconds=0 callback_scope=gut_barrier'
    ]
    transport.post_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_generation_is_persisted_and_callback_retries_without_regeneration():
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(503)))
    env = environment(callback_url="http://receiver.test/callback", transport=transport)
    env.pipeline.generate.side_effect = RuntimeError("sensitive content must not be public")
    job = await env.service.submit(request())
    await env.service.drain_generation()
    await env.service.drain_delivery()
    assert transport.post_once.await_count == 1
    saved = env.collection.documents[job["_id"]]
    assert saved["status"] == "failed" and saved["deliveryStatus"] == "retry_wait"
    assert "sensitive" not in str(saved)
    env.clock.advance(31)
    transport.post_once.return_value = CallbackResponse(204)
    await env.service.drain_delivery()
    await env.service.drain_delivery()
    assert transport.post_once.await_count == 2 and env.pipeline.generate.await_count == 1
    assert env.collection.documents[job["_id"]]["deliveryStatus"] == "delivered"


@pytest.mark.asyncio
async def test_no_configured_callback_never_delivers():
    transport = SimpleNamespace(post_once=AsyncMock())
    env = environment(transport=transport)
    await env.service.submit(request())
    await env.service.drain_generation()
    assert await env.service.drain_delivery() == 0
    transport.post_once.assert_not_called()


@pytest.mark.asyncio
async def test_database_outage_does_not_acknowledge_admission():
    env = environment()
    env.collection.unavailable = True
    with pytest.raises(ConnectionError):
        await env.service.submit(request())
    assert env.pipeline.generate.await_count == 0


@pytest.mark.asyncio
async def test_cancellation_leaves_lease_then_recovers_without_model_reentry():
    entered = asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    env = environment()
    env.pipeline.generate.side_effect = blocked
    job = await env.service.submit(request())
    running = asyncio.create_task(env.service.drain_generation())
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert env.collection.documents[job["_id"]]["status"] == "generating"
    assert await env.service.drain_generation() == 0
    env.clock.advance(121)
    assert await env.service.recover_pending() == 1
    assert env.collection.documents[job["_id"]]["body"]["error"]["code"] == "GENERATION_INTERRUPTED"
    assert env.pipeline.generate.await_count == 1


@pytest.mark.asyncio
async def test_timeout_has_explicit_terminal_error():
    env = environment()
    env.service.timeout_seconds = 0.005
    env.pipeline.generate.side_effect = lambda *args, **kwargs: None
    async def blocked(*args, **kwargs):
        await asyncio.sleep(1)
    env.pipeline.generate.side_effect = blocked
    job = await env.service.submit(request())
    await env.service.drain_generation()
    result = env.collection.documents[job["_id"]]
    assert result["body"]["error"]["code"] == "GENERATION_TIMEOUT"


@pytest.mark.asyncio
async def test_save_failure_refills_slot_while_sibling_still_generates():
    env = environment()
    env.service.concurrency = 2
    blocker_entered, release_blocker = asyncio.Event(), asyncio.Event()
    refill_entered, save_failed = asyncio.Event(), asyncio.Event()

    async def generate(value, **kwargs):
        if value.user_id == "save-fails":
            await blocker_entered.wait()
        elif value.user_id == "blocker":
            blocker_entered.set()
            await release_blocker.wait()
        else:
            refill_entered.set()
        return {"meta": {}}, {"partial": False}

    env.pipeline.generate.side_effect = generate
    original = env.store.complete

    async def complete(job, **kwargs):
        if job["userId"] == "save-fails":
            save_failed.set()
            raise ConnectionError("simulated save failure")
        return await original(job, **kwargs)

    env.store.complete = complete
    failed = await env.service.submit(request(user="save-fails"))
    blocker = await env.service.submit(request(user="blocker"))
    refill = await env.service.submit(request(user="refill"))
    running = asyncio.create_task(env.service.drain_generation())
    try:
        await blocker_entered.wait()
        await save_failed.wait()
        await asyncio.wait_for(refill_entered.wait(), 2)
        assert not release_blocker.is_set() and not running.done()
        assert env.collection.documents[failed["_id"]]["status"] == "generating"
        assert env.collection.documents[blocker["_id"]]["status"] == "generating"
        assert env.pipeline.generate.await_count == 3
    finally:
        release_blocker.set()
        with pytest.raises(ConnectionError):
            await running
    assert env.collection.documents[blocker["_id"]]["status"] == "success"
    assert env.collection.documents[refill["_id"]]["status"] == "success"


@pytest.mark.asyncio
async def test_completion_reuses_for_900_seconds_then_preserves_old_result_and_creates_new_id():
    env = environment()
    first = await env.service.submit(request())
    await env.service.drain_generation()
    frozen = env.collection.documents[first["_id"]]["body"]
    env.clock.advance(899)
    assert (await env.service.submit(request()))["_id"] == first["_id"]
    env.clock.advance(2)
    new = await env.service.submit(request())
    assert new["_id"] != first["_id"] and len(env.collection.documents) == 2
    assert env.collection.documents[first["_id"]]["body"] == frozen
    assert env.pipeline.generate.await_count == 1


@pytest.mark.asyncio
async def test_retry_after_and_configured_retry_schedule_are_honored_without_model_reentry():
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(429, "180")))
    env = environment(callback_url="http://receiver.invalid/callback", transport=transport)
    job = await env.service.submit(request())
    await env.service.drain_generation()
    await env.service.drain_delivery()
    row = env.collection.documents[job["_id"]]
    assert (row["nextDeliveryAt"] - env.clock()).total_seconds() == 180
    env.clock.advance(179)
    assert await env.service.drain_delivery() == 0
    env.clock.advance(1)
    transport.post_once.return_value = CallbackResponse(503)
    await env.service.drain_delivery()
    row = env.collection.documents[job["_id"]]
    assert (row["nextDeliveryAt"] - env.clock()).total_seconds() == 120
    assert env.pipeline.generate.await_count == 1


@pytest.mark.asyncio
async def test_durable_callback_has_no_three_attempt_limit_and_keeps_identical_body_and_key():
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(503)))
    env = environment(callback_url="http://receiver.invalid/callback", transport=transport)
    job = await env.service.submit(request())
    await env.service.drain_generation()
    delays = []
    for _ in range(6):
        assert await env.service.drain_delivery() == 1
        row = env.collection.documents[job["_id"]]
        delays.append((row["nextDeliveryAt"] - env.clock()).total_seconds())
        env.clock.advance(delays[-1])
    assert delays == [30, 120, 600, 3600, 3600, 3600]
    calls = transport.post_once.await_args_list
    assert all(call == calls[0] for call in calls)
    assert calls[0].kwargs["idempotency_key"] == job["_id"] + ":gutBarrier:v2"
    assert env.pipeline.generate.await_count == 1


@pytest.mark.asyncio
async def test_callback_batch_bound_and_concurrency_use_shared_worker_semantics():
    active = maximum = 0
    async def deliver(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return CallbackResponse(204)
    transport = SimpleNamespace(post_once=AsyncMock(side_effect=deliver))
    env = environment(callback_url="http://receiver.invalid/callback", transport=transport, callback_batch_size=5)
    for value in range(8):
        await env.service.submit(request(value=str(value)))
    while await env.service.drain_generation():
        pass
    assert await env.service.drain_delivery() == 5
    assert maximum == 4 and transport.post_once.await_count == 5
    assert await env.service.drain_delivery() == 3
    assert all(row["deliveryStatus"] == "delivered" for row in env.collection.documents.values())


@pytest.mark.asyncio
async def test_expired_generation_becomes_callback_failure_without_rerunning_pipeline():
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(204)))
    env = environment(callback_url="http://receiver.invalid/callback", transport=transport)
    job = await env.service.submit(request())
    claimed = await env.store.claim_generation()
    env.clock.advance(121)
    assert await env.service.recover_pending() == 1
    assert env.pipeline.generate.await_count == 0
    saved = env.collection.documents[job["_id"]]
    assert saved["status"] == "failed" and saved["body"]["error"]["code"] == "GENERATION_INTERRUPTED"
    assert not await env.store.complete(claimed, body={"status": "success"}, audit={})
    await env.service.drain_delivery()
    assert transport.post_once.await_args.args[1] == saved["body"]


@pytest.mark.asyncio
async def test_production_generation_persists_json_without_html():
    env = environment()
    job = await env.service.submit(request())
    await env.service.drain_generation()
    saved = env.collection.documents[job["_id"]]
    assert saved["status"] == "success" and "html" not in saved


@pytest.mark.asyncio
@pytest.mark.parametrize("generation_fails", [False, True])
@pytest.mark.parametrize("first_delivery_status", [204, 503])
async def test_terminal_resubmission_requeues_frozen_json_without_regeneration(
        generation_fails, first_delivery_status):
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(first_delivery_status)))
    env = environment(callback_url="http://receiver.invalid/callback", transport=transport)
    if generation_fails:
        env.pipeline.generate.side_effect = RuntimeError("generation failed")
    job = await env.service.submit(request())
    await env.service.drain_generation()
    assert await env.service.drain_delivery() == 1
    original_body = env.collection.documents[job["_id"]]["body"]
    expected_status = "failed" if generation_fails else "success"
    assert original_body["status"] == expected_status

    replay = await env.service.submit(request())
    assert replay["_id"] == job["_id"] and replay["status"] == expected_status
    assert await env.service.drain_generation() == 0
    transport.post_once.return_value = CallbackResponse(204)
    assert await env.service.drain_delivery() == 1
    calls = transport.post_once.await_args_list
    assert len(calls) == 2 and calls[0] == calls[1]
    saved = env.collection.documents[job["_id"]]
    assert saved["body"] == original_body and saved["deliveryStatus"] == "delivered"
    assert saved["generationAttempts"] == 1 and env.pipeline.generate.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("inflight_status", [204, 503])
async def test_resubmission_during_sending_survives_ack_or_retry_race(inflight_status):
    entered, release = asyncio.Event(), asyncio.Event()
    attempts = 0

    async def deliver(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            await release.wait()
            return CallbackResponse(inflight_status)
        return CallbackResponse(204)

    transport = SimpleNamespace(post_once=AsyncMock(side_effect=deliver))
    env = environment(callback_url="http://receiver.invalid/callback", transport=transport)
    job = await env.service.submit(request())
    await env.service.drain_generation()
    sending = asyncio.create_task(env.service.drain_delivery())
    await entered.wait()
    assert env.collection.documents[job["_id"]]["deliveryStatus"] == "sending"
    duplicate = await env.service.submit(request())
    assert duplicate["_id"] == job["_id"]
    assert await env.service.drain_generation() == 0
    release.set()
    await sending
    await env.service.drain_delivery()
    assert attempts == 2
    calls = transport.post_once.await_args_list
    assert calls[0] == calls[1]
    saved = env.collection.documents[job["_id"]]
    assert saved["deliveryStatus"] == "delivered" and saved["generationAttempts"] == 1
    assert env.pipeline.generate.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_version", ["1.0", "2.0"])
@pytest.mark.parametrize("missing_hash", [True, False])
async def test_historical_pending_document_retries_frozen_old_type_body_and_idempotency_key(schema_version, missing_hash):
    from adapters.gut_barrier.store import _content_hash

    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(503)))
    env = environment(callback_url="http://receiver.invalid/callback", transport=transport)
    job = await env.service.submit(request())
    await env.service.drain_generation()
    row = env.collection.documents[job["_id"]]
    # Historical snake_case records keep their original body and original key;
    # accepting new camelCase requests must not silently rewrite frozen records.
    row["body"]["type"] = "gut_barrier"
    row["body"]["schemaVersion"] = schema_version
    if schema_version == "1.0":
        row["body"]["report"] = deepcopy(row["audit"]["reportDocument"])
    else:
        from modules.gut_barrier.publication import project_public_report
        row["body"]["report"] = project_public_report(row["audit"]["reportDocument"])
    body = deepcopy(row["body"])
    if missing_hash:
        row.pop("contentHash")
    else:
        row["contentHash"] = _content_hash(body)
    row.pop("deliveryRequestRevision", None)
    assert await env.service.drain_delivery() == 1
    assert env.collection.documents[job["_id"]]["deliveryStatus"] == "retry_wait"
    env.clock.advance(30)
    transport.post_once.return_value = CallbackResponse(204)
    assert await env.service.drain_delivery() == 1
    saved = env.collection.documents[job["_id"]]
    assert saved["deliveryStatus"] == "delivered" and saved["contentHash"]
    assert saved["body"] == body and transport.post_once.await_args.args[1] == body
    version = "v1" if schema_version == "1.0" else "v2"
    assert transport.post_once.await_args.kwargs["idempotency_key"] == job["_id"] + ":gut_barrier:" + version
    assert saved["body"]["type"] == "gut_barrier" and saved["body"]["schemaVersion"] == schema_version
    assert transport.post_once.await_args_list[0] == transport.post_once.await_args_list[1]
    assert env.pipeline.generate.await_count == 1


@pytest.mark.asyncio
async def test_frozen_camelcase_full_v1_report_is_not_migrated_when_dynamic_v2_worker_retries():
    from adapters.gut_barrier.store import _content_hash
    from modules.gut_barrier.publication import project_public_report
    from tests.unit.gut_barrier.test_publication import clinical_report

    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(204)))
    env = environment(callback_url="http://receiver.invalid/callback", transport=transport)
    job = await env.service.submit(request())
    await env.service.drain_generation()
    row = env.collection.documents[job["_id"]]
    row["body"]["report"] = project_public_report(clinical_report())
    row["contentHash"] = _content_hash(row["body"])
    frozen = deepcopy(row["body"])
    assert await env.service.drain_delivery() == 1
    received = transport.post_once.await_args.args[1]
    assert received == frozen and received["report"]["schemaVersion"] == "gut-barrier-public.v1"
    assert len(received["report"]["sections"]) == 5
    assert transport.post_once.await_args.kwargs["idempotency_key"] == job["_id"] + ":gutBarrier:v2"
    assert env.pipeline.generate.await_count == 1


@pytest.mark.asyncio
async def test_current_camelcase_pending_body_can_backfill_hash_without_changing_new_key():
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(204)))
    env = environment(callback_url="http://receiver.invalid/callback", transport=transport)
    job = await env.service.submit(request())
    await env.service.drain_generation()
    row = env.collection.documents[job["_id"]]
    body = deepcopy(row["body"])
    assert body["type"] == "gutBarrier" and body["schemaVersion"] == "2.0"
    row.pop("contentHash")
    assert await env.service.drain_delivery() == 1
    saved = env.collection.documents[job["_id"]]
    assert saved["body"] == body and saved["contentHash"] and saved["deliveryStatus"] == "delivered"
    assert transport.post_once.await_args.args[1] == body
    assert transport.post_once.await_args.kwargs["idempotency_key"] == job["_id"] + ":gutBarrier:v2"
    assert env.pipeline.generate.await_count == 1


@pytest.mark.asyncio
async def test_service_archives_original_document_but_only_delivers_public_projection():
    from tests.unit.gut_barrier.test_publication import clinical_report

    original = clinical_report()
    before = deepcopy(original)
    internal_audit = {"partial": False, "issues": [{"code": "INTERNAL-AUDIT-MARKER"}]}
    pipeline = SimpleNamespace(version="public-test", generate=AsyncMock(return_value=(original, internal_audit)))
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(204)))
    env = environment(pipeline=pipeline, transport=transport, callback_url="http://receiver.invalid/callback")
    job = await env.service.submit(request())
    await env.service.drain_generation()
    await env.service.drain_delivery()
    stored = env.collection.documents[job["_id"]]
    body = transport.post_once.await_args.args[1]
    assert body["schemaVersion"] == "2.0" and body["type"] == "gutBarrier"
    assert body["report"] == project_callback_report(original)
    assert stored["audit"]["reportDocument"] == before
    assert stored["audit"]["issues"] == internal_audit["issues"]
    assert "reportDocument" not in internal_audit and original == before
    assert set(body["report"]) == {"schemaVersion", "meta", "sections"}
    # The client owns static copy/references; the callback retains only dynamic results.
    assert [s["id"] for s in body["report"]["sections"]] == ["overview", "disease_risks", "appendix"]
    assert "四重城墙" not in str(body["report"])
    assert "INTERNAL-AUDIT-MARKER" not in str(body)
    assert transport.post_once.await_args.kwargs["idempotency_key"] == job["_id"] + ":gutBarrier:v2"


@pytest.mark.asyncio
async def test_failed_v2_callback_keeps_failure_shape_without_report_or_internal_exception():
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(204)))
    env = environment(transport=transport, callback_url="http://receiver.invalid/callback")
    env.pipeline.generate.side_effect = ValueError("PRIVATE SOURCE CONTENT")
    job = await env.service.submit(request())
    await env.service.drain_generation()
    await env.service.drain_delivery()
    body = transport.post_once.await_args.args[1]
    assert body["schemaVersion"] == "2.0" and body["status"] == "failed"
    assert body["type"] == "gutBarrier"
    assert body["report"] is None and body["error"]["code"] == "GENERATION_FAILED"
    assert "PRIVATE SOURCE CONTENT" not in str(body)
    assert "audit" not in body and "reportDocument" not in body
    assert transport.post_once.await_args.kwargs["idempotency_key"] == job["_id"] + ":gutBarrier:v2"


@pytest.mark.asyncio
async def test_callback_never_repairs_a_present_but_mismatching_content_hash():
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(204)))
    env = environment(callback_url="http://receiver.invalid/callback", transport=transport)
    job = await env.service.submit(request())
    await env.service.drain_generation()
    env.collection.documents[job["_id"]]["contentHash"] = "0" * 64
    await env.service.drain_delivery()
    assert env.collection.documents[job["_id"]]["deliveryStatus"] == "retry_wait"
    assert env.collection.documents[job["_id"]]["contentHash"] == "0" * 64
    transport.post_once.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("userId", "another-user"), ("sessionId", "another-session"),
    ("finalReportId", "another-report"), ("type", "evaluate"), ("status", "processing"),
])
async def test_legacy_hash_backfill_does_not_authorize_wrong_identity_or_nonterminal_body(field, value):
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(204)))
    env = environment(callback_url="http://receiver.invalid/callback", transport=transport)
    job = await env.service.submit(request())
    await env.service.drain_generation()
    row = env.collection.documents[job["_id"]]
    row.pop("contentHash")
    row["body"][field] = value
    await env.service.drain_delivery()
    saved = env.collection.documents[job["_id"]]
    assert saved["deliveryStatus"] == "retry_wait" and "contentHash" not in saved
    transport.post_once.assert_not_called()


@pytest.mark.asyncio
async def test_stale_callback_lease_cannot_ack_or_reschedule_a_newer_replay_delivery():
    env = environment(callback_url="http://receiver.invalid/callback")
    job = await env.service.submit(request())
    await env.service.drain_generation()
    stale = await env.store.claim_delivery("old-worker")
    assert (await env.service.submit(request()))["_id"] == job["_id"]
    env.clock.advance(121)
    current = await env.store.claim_delivery("new-worker")
    assert stale["deliveryToken"] != current["deliveryToken"]
    assert not await env.store.acknowledge_delivery(stale)
    assert not await env.store.retry_delivery(stale, next_retry_at=env.clock(), error="stale")
    with pytest.raises(ValueError, match="unavailable"):
        await env.store.delivery_body(stale)
    assert (await env.store.delivery_body(current))["finalReportId"] == job["_id"]
    assert await env.store.acknowledge_delivery(current)
    assert env.collection.documents[job["_id"]]["deliveryStatus"] == "delivered"
    assert env.pipeline.generate.await_count == 1


@pytest.mark.asyncio
async def test_crash_between_replay_admission_and_pending_transition_does_not_lose_delivery():
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(204)))
    env = environment(callback_url="http://receiver.invalid/callback", transport=transport)
    job = await env.service.submit(request())
    await env.service.drain_generation()
    await env.service.drain_delivery()
    original_update = env.collection.update_one

    async def fail_pending_transition(query, update, **kwargs):
        if query.get("_id") == job["_id"] and update.get("$set", {}).get("deliveryStatus") == "pending":
            raise ConnectionError("process interrupted after durable replay admission")
        return await original_update(query, update, **kwargs)

    env.collection.update_one = fail_pending_transition
    with pytest.raises(ConnectionError):
        await env.service.submit(request())
    row = env.collection.documents[job["_id"]]
    assert row["deliveryStatus"] == "delivered" and row["deliveryReplayPending"] is True
    env.collection.update_one = original_update
    assert await env.service.drain_delivery() == 1
    calls = transport.post_once.await_args_list
    assert len(calls) == 2 and calls[0] == calls[1]
    row = env.collection.documents[job["_id"]]
    assert row["deliveryStatus"] == "delivered" and not row.get("deliveryReplayPending")
    assert env.pipeline.generate.await_count == 1
