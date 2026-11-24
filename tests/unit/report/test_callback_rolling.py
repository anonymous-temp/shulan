"""Callback slots roll independently and include pending lease claims."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from adapters.report.durable_store import MongoReportWorkflowStore
from modules.report.durable import ReportCallbackWorker
from modules.report.durable_ports import CallbackResponse
from tests.unit.report.durable_fakes import Clock, Collection


class Queue:
    def __init__(self):
        self.clock = Clock()
        self.artifacts, self.deliveries = Collection(), Collection()
        self.store = MongoReportWorkflowStore(
            batches=Collection(), artifacts=self.artifacts, deliveries=self.deliveries,
            clock=self.clock,
        )

    async def add(self, count):
        for _ in range(count):
            number = len(self.artifacts.documents)
            report_id = f'rolling-{number:03d}'
            key = f'{report_id}:evaluate'
            body = {'finalReportId': report_id, 'type': 'evaluate', 'status': 'success',
                    'content': [None, 0, False, [], {'original': number}]}
            await self.artifacts.insert_one({
                '_id': key, 'userId': 'u', 'contentHash': f'frozen-{number}', 'body': body,
            })
            await self.deliveries.insert_one({
                '_id': key, 'userId': 'u', 'finalReportId': report_id, 'reportType': 'evaluate',
                'contentHash': f'frozen-{number}', 'targetUrl': 'https://receiver.invalid/callback',
                'status': 'pending', 'attempts': 0, 'createdAt': self.clock(),
                'nextRetryAt': self.clock(),
            })


class BlockingTransport:
    def __init__(self):
        self.calls = []
        self.gates = {}
        self.started = asyncio.Queue()
        self.active = self.peak = 0
        self.open = False

    async def post_once(self, url, body, *, idempotency_key):
        self.calls.append((url, deepcopy(body), idempotency_key))
        self.gates.setdefault(idempotency_key, asyncio.Event())
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.put_nowait(idempotency_key)
        try:
            if not self.open:
                await self.gates[idempotency_key].wait()
            return CallbackResponse(204)
        finally:
            self.active -= 1

    async def wait_started(self, count):
        while len(self.calls) < count:
            await asyncio.wait_for(self.started.get(), 1)

    def release(self, index):
        self.gates[self.calls[index][2]].set()

    def release_all(self):
        self.open = True
        for gate in self.gates.values():
            gate.set()


def worker(queue, transport, **kwargs):
    return ReportCallbackWorker(store=queue.store, transport=transport,
                                clock=queue.clock, jitter=lambda: 1, **kwargs)


async def cancel_drains(*tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_fifth_callback_enters_when_one_of_four_slow_callbacks_finishes():
    queue, transport = Queue(), BlockingTransport()
    await queue.add(6)
    runner = worker(queue, transport)
    assert runner.concurrency == 4
    drain = asyncio.create_task(runner.drain_pending())
    try:
        await transport.wait_started(4)
        assert transport.active == 4
        transport.release(0)
        await transport.wait_started(5)
        assert transport.active == 4 and transport.peak == 4
        assert not any(transport.gates[call[2]].is_set() for call in transport.calls[1:4])
        assert not drain.done()
        runner.stop_accepting()
        transport.release_all()
        assert await asyncio.wait_for(drain, 1) == 5
        assert sum(row['status'] == 'delivered' for row in queue.deliveries.documents.values()) == 5
        assert sum(row['status'] == 'pending' for row in queue.deliveries.documents.values()) == 1
        assert not runner._delivery_tasks
    finally:
        await cancel_drains(drain)


@pytest.mark.asyncio
async def test_new_arrivals_can_fill_free_slots_while_an_earlier_drain_waits():
    queue, transport = Queue(), BlockingTransport()
    await queue.add(1)
    runner = worker(queue, transport)
    original = asyncio.create_task(runner.drain_pending())
    arrivals = []
    try:
        await transport.wait_started(1)
        # The queue was exhausted, but the existing request remains slow.
        assert await runner.drain_pending() == 0
        await queue.add(3)
        arrivals = [asyncio.create_task(runner.drain_pending()) for _ in range(6)]
        await transport.wait_started(4)
        assert not original.done() and transport.active == 4
        assert transport.peak == 4 and len(runner._delivery_tasks) == 4
        transport.release_all()
        assert await asyncio.wait_for(original, 1) == 1
        assert sum(await asyncio.wait_for(asyncio.gather(*arrivals), 1)) == 3
        assert len({call[2] for call in transport.calls}) == 4
    finally:
        await cancel_drains(original, *arrivals)


@pytest.mark.asyncio
async def test_claim_reservations_and_duplicate_drains_share_one_concurrency_limit():
    queue, transport = Queue(), BlockingTransport()
    await queue.add(12)
    runner = worker(queue, transport, batch_size=7)
    original_claim = queue.store.claim_delivery
    gate, four_claims = asyncio.Event(), asyncio.Event()
    claims = active = peak = 0

    async def blocked_claim(owner):
        nonlocal claims, active, peak
        claims += 1
        active += 1
        peak = max(peak, active)
        if claims == 4:
            four_claims.set()
        try:
            await gate.wait()
            return await original_claim(owner)
        finally:
            active -= 1

    queue.store.claim_delivery = blocked_claim
    drains = [asyncio.create_task(runner.drain_pending()) for _ in range(12)]
    try:
        await asyncio.wait_for(four_claims.wait(), 1)
        assert active == peak == claims == 4
        assert len(runner._delivery_tasks) == 4 and not transport.calls
        assert await runner.drain_pending() == 0
        assert claims == 4
        gate.set()
        await transport.wait_started(4)
        transport.release_all()
        assert sum(await asyncio.wait_for(asyncio.gather(*drains), 1)) == 7
        assert claims == 7 and transport.peak == 4 and not runner._delivery_tasks
        assert sum(row['status'] == 'pending' for row in queue.deliveries.documents.values()) == 5
        assert await runner.drain_pending() == 5
        assert len({call[2] for call in transport.calls}) == 12
    finally:
        await cancel_drains(*drains)


@pytest.mark.asyncio
async def test_transient_claim_failure_does_not_strand_or_cancel_claimed_siblings():
    queue, transport = Queue(), BlockingTransport()
    await queue.add(5)
    runner = worker(queue, transport)
    original_claim = queue.store.claim_delivery
    calls = 0

    async def flaky_claim(owner):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ConnectionError('private database detail')
        return await original_claim(owner)

    queue.store.claim_delivery = flaky_claim
    first = asyncio.create_task(runner.drain_pending())
    second = None
    try:
        await transport.wait_started(3)
        assert not first.done() and transport.active == 3
        second = asyncio.create_task(runner.drain_pending())
        await transport.wait_started(4)
        transport.release_all()
        assert await asyncio.wait_for(first, 1) == 3
        assert await asyncio.wait_for(second, 1) == 2
        assert transport.peak <= 4
        assert all(row['status'] == 'delivered' for row in queue.deliveries.documents.values())
    finally:
        await cancel_drains(first, *([second] if second else []))


@pytest.mark.asyncio
async def test_one_http_delivery_and_three_slow_claims_exhaust_the_same_four_slots():
    queue, transport = Queue(), BlockingTransport()
    await queue.add(1)
    runner = worker(queue, transport)
    original = asyncio.create_task(runner.drain_pending())
    arrivals = []
    original_claim = queue.store.claim_delivery
    gate, three_claims = asyncio.Event(), asyncio.Event()
    reservations = calls = 0

    async def blocked_claim(owner):
        nonlocal reservations, calls
        calls += 1
        reservations += 1
        if reservations == 3:
            three_claims.set()
        try:
            await gate.wait()
            return await original_claim(owner)
        finally:
            reservations -= 1

    try:
        await transport.wait_started(1)
        assert await runner.drain_pending() == 0
        await queue.add(7)
        queue.store.claim_delivery = blocked_claim
        arrivals = [asyncio.create_task(runner.drain_pending()) for _ in range(8)]
        await asyncio.wait_for(three_claims.wait(), 1)
        assert transport.active == 1 and reservations == calls == 3
        assert len(runner._delivery_tasks) == transport.active + reservations == 4
        assert await runner.drain_pending() == 0
        assert calls == 3
        gate.set()
        await transport.wait_started(4)
        runner.stop_accepting()
        transport.release_all()
        assert await asyncio.wait_for(original, 1) == 1
        assert sum(await asyncio.wait_for(asyncio.gather(*arrivals), 1)) == 3
        assert transport.peak == 4 and not runner._delivery_tasks
        assert sum(row['status'] == 'pending' for row in queue.deliveries.documents.values()) == 4
    finally:
        await cancel_drains(original, *arrivals)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [RuntimeError, asyncio.CancelledError])
async def test_one_item_error_or_cancellation_does_not_cancel_siblings_or_block_refill(failure):
    queue, transport = Queue(), BlockingTransport()
    await queue.add(5)
    runner = worker(queue, transport)
    original_deliver = runner._deliver

    async def broken_item(job):
        if job['_id'] == 'rolling-000:evaluate':
            raise failure()
        await original_deliver(job)

    runner._deliver = broken_item
    drain = asyncio.create_task(runner.drain_pending())
    try:
        await transport.wait_started(4)
        assert transport.active == 4 and not drain.done()
        transport.release_all()
        assert await asyncio.wait_for(drain, 1) == 5
        assert queue.deliveries.documents['rolling-000:evaluate']['status'] == 'sending'
        assert sum(row['status'] == 'delivered' for row in queue.deliveries.documents.values()) == 4
        runner._deliver = original_deliver
        queue.clock.advance(121)
        assert await runner.drain_pending() == 1
        assert all(row['status'] == 'delivered' for row in queue.deliveries.documents.values())
    finally:
        await cancel_drains(drain)


@pytest.mark.asyncio
async def test_ack_and_retry_save_failure_releases_slot_and_recovers_same_frozen_body():
    queue, transport = Queue(), BlockingTransport()
    await queue.add(5)
    runner = worker(queue, transport)
    original_ack, original_retry = queue.store.acknowledge_delivery, queue.store.retry_delivery

    async def fail_ack(job):
        if job['_id'] == 'rolling-000:evaluate':
            raise ConnectionError('ack unavailable')
        return await original_ack(job)

    queue.store.acknowledge_delivery = fail_ack
    queue.store.retry_delivery = AsyncMock(side_effect=ConnectionError('retry write unavailable'))
    drain = asyncio.create_task(runner.drain_pending())
    try:
        await transport.wait_started(4)
        transport.release(0)
        await transport.wait_started(5)
        assert transport.active == 4
        transport.release_all()
        assert await asyncio.wait_for(drain, 1) == 5
        queue.store.acknowledge_delivery, queue.store.retry_delivery = original_ack, original_retry
        queue.clock.advance(121)
        assert await runner.drain_pending() == 1
        repeats = [call for call in transport.calls if call[2] == 'rolling-000:evaluate']
        assert len(repeats) == 2 and repeats[0] == repeats[1]
        assert all(row['status'] == 'delivered' for row in queue.deliveries.documents.values())
    finally:
        await cancel_drains(drain)


@pytest.mark.asyncio
async def test_cancelling_one_drain_cleans_only_its_tasks_and_leases_are_recoverable():
    queue, transport = Queue(), BlockingTransport()
    await queue.add(4)
    runner = worker(queue, transport, batch_size=2)
    first = asyncio.create_task(runner.drain_pending())
    second = None
    try:
        await transport.wait_started(2)
        second = asyncio.create_task(runner.drain_pending())
        await transport.wait_started(4)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not second.done() and transport.active == 2
        assert len(runner._delivery_tasks) == 2
        transport.release_all()
        assert await asyncio.wait_for(second, 1) == 2
        assert not runner._delivery_tasks
        assert sum(row['status'] == 'sending' for row in queue.deliveries.documents.values()) == 2
        queue.clock.advance(121)
        assert await runner.drain_pending() == 2
        for key in ('rolling-000:evaluate', 'rolling-001:evaluate'):
            repeats = [call for call in transport.calls if call[2] == key]
            assert len(repeats) == 2 and repeats[0] == repeats[1]
    finally:
        await cancel_drains(first, *([second] if second else []))


@pytest.mark.asyncio
async def test_cancellation_during_claim_keeps_ambiguous_database_lease_recoverable():
    queue, transport = Queue(), BlockingTransport()
    await queue.add(1)
    runner = worker(queue, transport, concurrency=1)
    original_claim = queue.store.claim_delivery
    claimed, gate = asyncio.Event(), asyncio.Event()

    async def slow_claim(owner):
        job = await original_claim(owner)
        claimed.set()
        await gate.wait()
        return job

    queue.store.claim_delivery = slow_claim
    drain = asyncio.create_task(runner.drain_pending())
    try:
        await asyncio.wait_for(claimed.wait(), 1)
        drain.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain
        assert not runner._delivery_tasks and not transport.calls
        assert queue.deliveries.documents['rolling-000:evaluate']['status'] == 'sending'
        queue.store.claim_delivery = original_claim
        queue.clock.advance(121)
        transport.release_all()
        assert await runner.drain_pending() == 1
        assert queue.deliveries.documents['rolling-000:evaluate']['status'] == 'delivered'
    finally:
        await cancel_drains(drain)


@pytest.mark.asyncio
async def test_stop_during_claim_finishes_already_claimed_work_without_more_claims():
    queue, transport = Queue(), BlockingTransport()
    await queue.add(2)
    runner = worker(queue, transport, concurrency=1)
    original_claim = queue.store.claim_delivery
    claimed, gate = asyncio.Event(), asyncio.Event()

    async def slow_claim(owner):
        job = await original_claim(owner)
        claimed.set()
        await gate.wait()
        return job

    queue.store.claim_delivery = slow_claim
    drain = asyncio.create_task(runner.drain_pending())
    try:
        await asyncio.wait_for(claimed.wait(), 1)
        runner.stop_accepting()
        gate.set()
        transport.release_all()
        assert await asyncio.wait_for(drain, 1) == 1
        assert await runner.drain_pending() == 0
        assert queue.deliveries.documents['rolling-000:evaluate']['status'] == 'delivered'
        assert queue.deliveries.documents['rolling-001:evaluate']['status'] == 'pending'
    finally:
        await cancel_drains(drain)


@pytest.mark.asyncio
async def test_backlog_failure_does_not_fail_delivery_or_duplicate_queries():
    queue, transport = Queue(), BlockingTransport()
    await queue.add(1)
    transport.release_all()
    queue.store.backlog = AsyncMock(side_effect=ConnectionError('private backlog detail'))
    runner = worker(queue, transport)
    counts = await asyncio.gather(*(runner.drain_pending() for _ in range(10)))
    assert sum(counts) == 1
    assert queue.store.backlog.await_count == 1
    assert queue.deliveries.documents['rolling-000:evaluate']['status'] == 'delivered'
    assert not runner._delivery_tasks
