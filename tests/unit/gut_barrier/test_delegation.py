"""Restart, lost ACK and independent callback behavior for the peer handoff."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.gut_barrier.delegation import MongoGutBarrierDelegationStore
from adapters.gut_barrier.delegation import PublicReportDelegationAdapter
from modules.gut_barrier.delegation import DelegationPolicy
from modules.gut_barrier.delegation import GutBarrierDelegationWorker
from modules.report.durable_ports import CallbackResponse
from tests.unit.gut_barrier.test_service import environment
from tests.unit.gut_barrier.test_service import request
from tests.unit.report.test_delegation import setup as report_environment


def setup(*, policy=None, port=None):
    env = environment(
        callback_url='https://receiver.invalid',
        transport=SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(500))),
    )
    peer = report_environment()
    port = port or PublicReportDelegationAdapter(peer.service)
    store = MongoGutBarrierDelegationStore(env.store)
    env.service.delegation_policy = policy or DelegationPolicy()
    env.service.delegation_worker = GutBarrierDelegationWorker(store=store, port=port)
    env.peer, env.delegations, env.port = peer, store, port
    return env


@pytest.mark.asyncio
async def test_completion_persists_intent_before_worker_and_callbacks_are_independent():
    env = setup()
    job = await env.service.submit(request())
    await env.service.drain_generation()
    frozen = env.collection.documents[job['_id']]['body'].copy()
    assert env.peer.generator.calls == []
    assert env.collection.documents[job['_id']]['delegationStatus'] == 'pending'
    await env.service.drain_delivery()  # The main callback fails independently.
    replacement = GutBarrierDelegationWorker(store=env.delegations, port=env.port)
    assert await replacement.drain_pending() == 1
    assert env.collection.documents[job['_id']]['delegationStatus'] == 'accepted'
    assert env.collection.documents[job['_id']]['deliveryStatus'] == 'retry_wait'
    await env.peer.service.drain_generation()
    assert len(env.peer.generator.calls) == 1
    artifacts = list(env.peer.store.artifacts.documents.values())
    assert {item['reportType'] for item in artifacts} == {'explain_gutBarrier', 'opinion', 'instruction'}
    assert {item['body']['finalReportId'] for item in artifacts} == {job['_id']}
    assert env.collection.documents[job['_id']]['body'] == frozen
    await replacement.drain_pending()
    assert len(env.peer.store.batches.documents) == 1


@pytest.mark.asyncio
async def test_lost_admission_ack_is_reconciled_even_after_budget_exhaustion():
    env = setup(policy=DelegationPolicy(max_attempts=1, deadline_seconds=5))
    original = env.port.submit

    async def lose_ack(body):
        await original(body)
        raise ConnectionError('lost ACK')

    env.port.submit = lose_ack
    job = await env.service.submit(request())
    await env.service.drain_generation()
    await env.service.drain_delegation()
    env.clock.advance(6)
    await env.service.drain_delegation()
    row = env.collection.documents[job['_id']]
    assert row['delegationStatus'] == 'accepted' and row['delegationAttempts'] == 1
    assert len(env.peer.store.batches.documents) == 1
    assert env.peer.generator.calls == []


@pytest.mark.asyncio
async def test_budget_exhaustion_does_not_stop_primary_callback_retries():
    port = SimpleNamespace(receipt=AsyncMock(return_value=None), submit=AsyncMock(side_effect=ConnectionError))
    env = setup(policy=DelegationPolicy(max_attempts=1), port=port)
    job = await env.service.submit(request())
    await env.service.drain_generation()
    await env.service.drain_delegation()
    env.clock.advance(3)
    await env.service.drain_delegation()
    assert env.collection.documents[job['_id']]['delegationStatus'] == 'needs_reconciliation'
    port.submit.assert_awaited_once()
    for _ in range(8):
        await env.service.drain_delivery()
        env.clock.advance(86400)
    assert env.service.transport.post_once.await_count == 8
    assert env.collection.documents[job['_id']]['deliveryStatus'] == 'retry_wait'


@pytest.mark.asyncio
async def test_expired_worker_cannot_overwrite_recovered_delegation():
    env = setup()
    job = await env.service.submit(request())
    await env.service.drain_generation()
    old = await env.delegations.claim()
    env.clock.advance(121)
    await env.service.drain_delegation()
    assert not await env.delegations.finish(old, status='rejected')
    assert env.collection.documents[job['_id']]['delegationStatus'] == 'accepted'


@pytest.mark.asyncio
async def test_preexisting_completed_jobs_are_not_replayed_on_upgrade():
    env = environment()
    job = await env.service.submit(request())
    await env.service.drain_generation()
    env.service.delegation_worker = GutBarrierDelegationWorker(
        store=MongoGutBarrierDelegationStore(env.store), port=SimpleNamespace(submit=AsyncMock())
    )
    assert await env.service.drain_delegation() == 0
    assert 'delegationStatus' not in env.collection.documents[job['_id']]


@pytest.mark.asyncio
async def test_old_unfinished_main_uses_explicit_compatibility_without_model_replay():
    env = setup()
    env.service.delegation_policy = None
    job = await env.service.submit(request())
    await env.store.claim_generation()
    env.clock.advance(121)
    env.service.delegation_policy = DelegationPolicy()
    await env.service.recover_pending()
    assert env.pipeline.generate.await_count == 0
    row = env.collection.documents[job['_id']]
    assert row['body']['error']['code'] == 'GENERATION_INTERRUPTED'
    assert row['delegationCompatibility'] == 'legacy-unfinished-main.v1'
    await env.service.drain_delegation()
    assert row['body']['status'] == 'failed'
    assert len(env.peer.store.batches.documents) == 1


@pytest.mark.asyncio
async def test_mongo_naive_utc_deadline_can_submit_and_retry_without_changing_budget():
    from bson import BSON

    port = SimpleNamespace(receipt=AsyncMock(return_value=None), submit=AsyncMock(side_effect=ConnectionError))
    env = setup(policy=DelegationPolicy(max_attempts=1), port=port)
    job = await env.service.submit(request())
    await env.service.drain_generation()
    original = env.collection.find_one_and_update

    async def mongo_codec(*args, **kwargs):
        row = await original(*args, **kwargs)
        return None if row is None else BSON.encode(row).decode()

    env.collection.find_one_and_update = mongo_codec
    await env.service.drain_delegation()
    assert env.collection.documents[job['_id']]['delegationStatus'] == 'retry_wait'
    env.clock.advance(3)
    await env.service.drain_delegation()
    assert env.collection.documents[job['_id']]['delegationStatus'] == 'needs_reconciliation'
    port.submit.assert_awaited_once()
