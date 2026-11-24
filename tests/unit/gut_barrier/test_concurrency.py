"""Exercise real coroutine overlap, using only the local durable store fake."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.gut_barrier.policy import GUT_BARRIER_GENERATION_CONCURRENCY
from modules.gut_barrier.service import GutBarrierService
from modules.report.durable_ports import CallbackResponse
from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY
from tests.unit.gut_barrier.test_service import environment, request


def test_generation_limit_is_independent_of_legacy_policy_and_callback_default():
    env = environment()
    assert env.service.concurrency == GUT_BARRIER_GENERATION_CONCURRENCY == 20
    assert DEFAULT_REPORT_FALLBACK_POLICY.max_concurrent_evaluate_reports == 2


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "20", None])
def test_invalid_injected_generation_limit_is_rejected(value):
    with pytest.raises(ValueError, match="positive integer"):
        GutBarrierService(store=None, pipeline=None, concurrency=value)


@pytest.mark.asyncio
async def test_twenty_tasks_overlap_twenty_first_queues_and_parallel_drains_do_not_exceed_cap():
    active = maximum = calls = 0
    capacity_reached, release = asyncio.Event(), asyncio.Event()

    async def generate(*args, **kwargs):
        nonlocal active, maximum, calls
        active += 1
        calls += 1
        maximum = max(maximum, active)
        if active == 20:
            capacity_reached.set()
        try:
            await release.wait()
            return {"meta": {}}, {"partial": False}
        finally:
            active -= 1

    env = environment(pipeline=SimpleNamespace(version="overlap-test", generate=AsyncMock(side_effect=generate)))
    jobs = [await env.service.submit(request(user=f"test-concurrency-{index}")) for index in range(21)]
    batch = asyncio.create_task(env.service.drain_generation())
    try:
        await asyncio.wait_for(capacity_reached.wait(), timeout=2)
        assert active == calls == maximum == 20
        statuses = [env.collection.documents[job["_id"]]["status"] for job in jobs]
        assert statuses.count("generating") == 20 and statuses.count("queued") == 1
        # Simulate overlapping worker ticks/HTTP wakeups on the same service.
        assert await asyncio.gather(*(env.service.drain_generation() for _ in range(30))) == [0] * 30
        assert active == calls == maximum == 20
    finally:
        release.set()
        await asyncio.wait_for(batch, timeout=2)
    assert batch.result() == 21
    assert await env.service.drain_generation() == 0
    assert calls == 21 and maximum == 20 and active == 0
    assert all(row["status"] == "success" and row["generationAttempts"] == 1
               for row in env.collection.documents.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["lease_rejected", "database_error"])
async def test_lost_generation_heartbeat_cancels_work_then_recovers_and_delivers_failure(failure_mode):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def generate(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(204)))
    env = environment(
        pipeline=SimpleNamespace(version="lease-test", generate=AsyncMock(side_effect=generate)),
        callback_url="http://receiver.invalid/callback", transport=transport,
    )
    env.service.heartbeat_seconds = 0.01
    env.store.renew_generation = AsyncMock(return_value=False)
    if failure_mode == "database_error":
        env.store.renew_generation.side_effect = ConnectionError("PRIVATE DATABASE MESSAGE")
    original_complete = env.store.complete
    env.store.complete = AsyncMock(wraps=original_complete)
    job = await env.service.submit(request())
    running = asyncio.create_task(env.service.drain_generation())
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert await asyncio.wait_for(running, timeout=2) == 1
    assert cancelled.is_set()
    env.store.complete.assert_not_awaited()
    assert env.collection.documents[job["_id"]]["status"] == "generating"
    assert await env.service.drain_delivery() == 0
    # The expired task is closed as interrupted; it must not rerun the model.
    env.clock.advance(121)
    assert await env.service.recover_pending() == 1
    assert await env.service.drain_delivery() == 1
    body = transport.post_once.await_args.args[1]
    assert body["status"] == "failed" and body["report"] is None
    assert body["error"]["code"] == "GENERATION_INTERRUPTED"
    assert "PRIVATE DATABASE MESSAGE" not in str(body)
    assert env.pipeline.generate.await_count == 1
    assert env.collection.documents[job["_id"]]["deliveryStatus"] == "delivered"
