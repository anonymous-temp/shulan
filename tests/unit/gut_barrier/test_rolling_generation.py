import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.report.durable_ports import CallbackResponse
from tests.unit.gut_barrier.test_service import environment, request


async def settled(task):
    return await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_later_arrivals_fill_capacity_and_one_finished_slot_starts_twenty_first():
    entered = [asyncio.Event() for _ in range(21)]
    release = [asyncio.Event() for _ in range(21)]
    active = peak = 0

    async def generate(value, **kwargs):
        nonlocal active, peak
        index = int(value.user_id)
        active += 1
        peak = max(peak, active)
        entered[index].set()
        try:
            await release[index].wait()
            return {"meta": {}}, {"partial": False}
        finally:
            active -= 1

    env = environment(pipeline=SimpleNamespace(version="rolling", generate=AsyncMock(side_effect=generate)))
    await env.service.submit(request(user="0"))
    owner = asyncio.create_task(env.service.drain_generation())
    try:
        await settled(entered[0].wait())
        for index in range(1, 21):
            await env.service.submit(request(user=str(index)))
        await settled(asyncio.gather(*(event.wait() for event in entered[:20])))
        assert active == peak == 20 and not entered[20].is_set()
        assert await asyncio.gather(*(env.service.drain_generation() for _ in range(30))) == [0] * 30
        release[7].set()
        await settled(entered[20].wait())
        assert active == peak == 20
        assert sum(event.is_set() for event in release) == 1
        assert not owner.done()
    finally:
        for event in release:
            event.set()
        await settled(owner)
    assert owner.result() == 21
    assert peak == 20 and active == 0
    assert all(row["status"] == "success" and row["generationAttempts"] == 1
               for row in env.collection.documents.values())


@pytest.mark.asyncio
async def test_temporary_claim_failure_refills_while_started_sibling_remains_blocked():
    first_entered, second_entered = asyncio.Event(), asyncio.Event()
    release_first, failed = asyncio.Event(), asyncio.Event()

    async def generate(value, **kwargs):
        if value.user_id == "first":
            first_entered.set()
            await release_first.wait()
        else:
            second_entered.set()
        return {"meta": {}}, {"partial": False}

    env = environment(pipeline=SimpleNamespace(version="claim-failure", generate=AsyncMock(side_effect=generate)))
    first = await env.service.submit(request(user="first"))
    second = await env.service.submit(request(user="second"))
    original = env.store.claim_generation
    calls = 0

    async def claim(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            assert first_entered.is_set()
            failed.set()
            raise ConnectionError("temporary local DB failure")
        return await original(**kwargs)

    env.store.claim_generation = claim
    owner = asyncio.create_task(env.service.drain_generation())
    try:
        await settled(failed.wait())
        assert not owner.done()
        assert await env.service.drain_generation() == 0
        assert env.pipeline.generate.await_count == 1
        assert env.collection.documents[second["_id"]]["status"] == "queued"
        await asyncio.sleep(0.02)
        assert calls == 2  # The transient database failure cannot create a busy claim loop.
        await settled(second_entered.wait())
        assert not release_first.is_set() and not owner.done()
        assert env.pipeline.generate.await_count == 2
    finally:
        release_first.set()
        with pytest.raises(ConnectionError):
            await settled(owner)
    env.store.claim_generation = original
    assert env.collection.documents[first["_id"]]["status"] == "success"
    assert env.collection.documents[second["_id"]]["status"] == "success"


@pytest.mark.asyncio
async def test_uncertain_claim_is_left_for_fenced_recovery_without_model_reentry():
    generated = asyncio.Event()

    async def generate(value, **kwargs):
        assert value.user_id == "known"
        generated.set()
        return {"meta": {}}, {"partial": False}

    env = environment(pipeline=SimpleNamespace(version="uncertain-claim", generate=AsyncMock(side_effect=generate)))
    unknown = await env.service.submit(request(user="unknown"))
    known = await env.service.submit(request(user="known"))
    original = env.store.claim_generation
    calls = 0

    async def claim(**kwargs):
        nonlocal calls
        calls += 1
        job = await original(**kwargs)
        if calls == 1:
            assert job["_id"] == unknown["_id"]
            raise ConnectionError("claim acknowledgement was lost")
        return job

    env.store.claim_generation = claim
    with pytest.raises(ConnectionError):
        await env.service.drain_generation()
    assert generated.is_set() and env.pipeline.generate.await_count == 1
    assert env.collection.documents[unknown["_id"]]["status"] == "generating"
    assert env.collection.documents[known["_id"]]["status"] == "success"

    env.store.claim_generation = original
    env.clock.advance(121)
    assert await env.service.recover_pending() == 1
    assert env.pipeline.generate.await_count == 1
    assert env.collection.documents[unknown["_id"]]["body"]["error"]["code"] == "GENERATION_INTERRUPTED"


@pytest.mark.asyncio
async def test_slow_later_claim_does_not_delay_first_generation_heartbeat():
    entered, claiming, release_claim, renewed = [asyncio.Event() for _ in range(4)]
    env = environment()
    env.service.heartbeat_seconds = 0.01

    async def generate(*args, **kwargs):
        entered.set()
        await release_claim.wait()
        return {"meta": {}}, {"partial": False}

    env.pipeline.generate.side_effect = generate
    await env.service.submit(request())
    original_claim, original_renew = env.store.claim_generation, env.store.renew_generation
    calls = 0

    async def claim(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            claiming.set()
            await release_claim.wait()
        return await original_claim(**kwargs)

    async def renew(job):
        renewed.set()
        return await original_renew(job)

    env.store.claim_generation, env.store.renew_generation = claim, renew
    owner = asyncio.create_task(env.service.drain_generation())
    try:
        await settled(claiming.wait())
        assert entered.is_set()
        await settled(renewed.wait())
        assert not owner.done()
    finally:
        release_claim.set()
        await settled(owner)
    assert owner.result() == 1


@pytest.mark.asyncio
async def test_cancel_during_later_claim_cleans_running_child_and_recovers_without_model_retry():
    claiming, cancelled = asyncio.Event(), asyncio.Event()
    env = environment()

    async def blocked(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    env.pipeline.generate.side_effect = blocked
    first = await env.service.submit(request())
    claim = env.store.claim_generation
    calls = 0

    async def block_second(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            claiming.set()
            await asyncio.Event().wait()
        return await claim(**kwargs)

    env.store.claim_generation = block_second
    owner = asyncio.create_task(env.service.drain_generation())
    await settled(claiming.wait())
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await settled(owner)
    assert cancelled.is_set() and not env.service._generation_lock.locked()
    assert env.collection.documents[first["_id"]]["status"] == "generating"
    env.store.claim_generation = claim
    env.clock.advance(121)
    assert await env.service.recover_pending() == 1
    assert env.collection.documents[first["_id"]]["body"]["error"]["code"] == "GENERATION_INTERRUPTED"
    assert env.pipeline.generate.await_count == 1


@pytest.mark.asyncio
async def test_repeated_parent_cancellation_waits_for_all_child_cleanup():
    all_started, all_cleaning, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    started = cleaning = finished = 0
    env = environment()
    env.service.concurrency = 2

    async def generate(*args, **kwargs):
        nonlocal started, cleaning, finished
        started += 1
        if started == 2:
            all_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning += 1
            if cleaning == 2:
                all_cleaning.set()
            await release_cleanup.wait()
            finished += 1

    env.pipeline.generate.side_effect = generate
    for user in ("one", "two"):
        await env.service.submit(request(user=user))
    owner = asyncio.create_task(env.service.drain_generation())
    await settled(all_started.wait())
    owner.cancel()
    try:
        await settled(all_cleaning.wait())
        owner.cancel()
        await asyncio.sleep(0)
        assert not owner.done() and env.service._generation_lock.locked()
    finally:
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await settled(owner)
    assert finished == 2 and not env.service._generation_lock.locked()
    assert all(row["status"] == "generating" for row in env.collection.documents.values())
    env.clock.advance(121)
    assert await env.service.recover_pending() == 2
    assert env.pipeline.generate.await_count == 2


@pytest.mark.asyncio
async def test_start_marker_database_wait_is_within_overall_generation_timeout():
    env = environment()
    env.service.timeout_seconds = 0.01

    async def blocked(job):
        await asyncio.Event().wait()

    env.store.mark_generation_started = blocked
    job = await env.service.submit(request())
    assert await env.service.drain_generation() == 1
    assert env.collection.documents[job["_id"]]["body"]["error"]["code"] == "GENERATION_TIMEOUT"
    env.pipeline.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_admission_retains_worker_generation_recovery_and_delivery():
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(204)))
    env = environment(callback_url="https://receiver.invalid/callback", transport=transport)
    await env.service.submit(request(user="queued"))
    interrupted = await env.service.submit(request(user="interrupted"))
    queued = await env.store.claim_generation()
    assert queued is not None
    env.service.stop_admission()
    with pytest.raises(RuntimeError, match="draining"):
        await env.service.submit(request(user="new"))
    assert await env.service.drain_generation() == 1
    env.clock.advance(121)
    assert await env.service.recover_pending() == 1
    assert await env.service.drain_delivery() == 2
    assert len(env.collection.documents) == 2
    assert all(row["deliveryStatus"] == "delivered" for row in env.collection.documents.values())
    assert interrupted["_id"] in env.collection.documents


@pytest.mark.asyncio
async def test_full_stop_does_not_fill_freed_capacity_but_waits_for_active_work():
    entered, release = asyncio.Event(), asyncio.Event()
    env = environment()
    env.service.concurrency = 1

    async def generate(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"meta": {}}, {"partial": False}

    env.pipeline.generate.side_effect = generate
    first = await env.service.submit(request(user="first"))
    second = await env.service.submit(request(user="second"))
    owner = asyncio.create_task(env.service.drain_generation())
    await settled(entered.wait())
    env.service.stop_accepting()
    release.set()
    assert await settled(owner) == 1
    assert env.collection.documents[first["_id"]]["status"] == "success"
    assert env.collection.documents[second["_id"]]["status"] == "queued"
    assert await env.service.drain_generation() == 0
    assert await env.service.recover_pending() == 0
    assert await env.service.drain_delivery() == 0


@pytest.mark.asyncio
async def test_stop_while_claim_in_flight_releases_only_unstarted_claim():
    env = environment()
    job = await env.service.submit(request())
    claim = env.store.claim_generation

    async def stop_after_claim(**kwargs):
        claimed = await claim(**kwargs)
        env.service.stop_workers()
        return claimed

    env.store.claim_generation = stop_after_claim
    assert await env.service.drain_generation() == 0
    assert env.collection.documents[job["_id"]]["status"] == "queued"
    assert "leaseToken" not in env.collection.documents[job["_id"]]
    env.pipeline.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_unstarted_release_requires_explicit_knowledge_and_valid_unstarted_owner():
    env = environment()
    await env.service.submit(request())
    job = await env.store.claim_generation()
    assert not await env.store.release_generation(job)
    assert not await env.store.release_generation({**job, "leaseToken": "stale"}, known_not_started=True)
    assert await env.store.release_generation(job, known_not_started=True)
    assert not await env.store.release_generation(job, known_not_started=True)
    current = await env.store.claim_generation()
    assert await env.store.mark_generation_started(current)
    assert not await env.store.mark_generation_started(current)
    assert not await env.store.release_generation(current, known_not_started=True)
    env.clock.advance(121)
    assert not await env.store.release_generation(current, known_not_started=True)
    recovery = await env.store.claim_generation(expired=True)
    assert not await env.store.release_generation(recovery, known_not_started=True)


@pytest.mark.asyncio
async def test_stale_or_repeated_start_cannot_enter_clinical_pipeline():
    env = environment()
    await env.service.submit(request())
    job = await env.store.claim_generation()
    env.clock.advance(121)
    await env.service._generate(job)
    env.pipeline.generate.assert_not_awaited()
    assert await env.service.recover_pending() == 1
    assert env.collection.documents[job["_id"]]["body"]["error"]["code"] == "GENERATION_INTERRUPTED"


@pytest.mark.asyncio
async def test_release_database_error_is_not_assumed_to_requeue():
    env = environment()
    await env.service.submit(request())
    job = await env.store.claim_generation()
    original = deepcopy(env.collection.documents[job["_id"]])
    env.store.release_generation = AsyncMock(side_effect=ConnectionError("private DB URL"))
    assert await env.service._release_unstarted(job) is False
    assert env.collection.documents[job["_id"]] == original
    env.clock.advance(121)
    assert await env.service.recover_pending() == 1
    env.pipeline.generate.assert_not_awaited()
