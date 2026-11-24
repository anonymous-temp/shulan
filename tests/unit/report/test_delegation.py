"""Durable peer delegation never generates ordinary report placeholders or duplicate tasks."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from adapters.report.delegation_store import MongoReportFollowupStore
from modules.report.artifacts import CONFLICT
from modules.report.artifacts import SAVED
from modules.report.artifacts import content_hash
from modules.report.artifacts import save_report_artifact
from modules.report.delegation import GUT_BARRIER_FOLLOWUP_TYPES
from modules.report.delegation import FollowupConflictError
from modules.report.delegation import GutBarrierFollowupRequest
from modules.report.delegation import ReportFollowupService
from modules.report.durable import ReportCallbackWorker
from modules.report.durable_ports import CallbackResponse
from modules.report.ports import ReportArtifactRecord
from tests.unit.report.durable_fakes import Clock
from tests.unit.report.durable_fakes import Collection


def request(user='u', source='gut-main', **extra):
    body = {
        'type': 'gutBarrier',
        'userId': user,
        'sessionId': 's',
        'finalReportId': source,
        'report': {'section': 'fixed synthetic input'},
        **extra,
    }
    return GutBarrierFollowupRequest(user, 's', source, body)


class Generator:
    def __init__(self):
        self.calls = []
        self.fail_after = None

    async def generate(self, body, *, user_id, session_id, execution_id):
        self.calls.append((deepcopy(body), execution_id))
        payloads = []
        for index, kind in enumerate(GUT_BARRIER_FOLLOWUP_TYPES):
            if self.fail_after == index:
                raise RuntimeError('simulated interruption')
            payload = {
                'type': kind,
                'userId': user_id,
                'sessionId': session_id,
                'finalReportId': body['finalReportId'],
                'status': 'success',
                'report': {'text': kind},
            }
            assert await save_report_artifact(kind, payload, generation_status='success') == SAVED
            payloads.append(payload)
        return SimpleNamespace(payloads=tuple(payloads))


def setup():
    clock = Clock()
    store = MongoReportFollowupStore(
        batches=Collection(),
        artifacts=Collection(),
        deliveries=Collection(),
        clock=clock,
        lease_seconds=30,
        reuse_seconds=1,
    )
    generator = Generator()
    service = ReportFollowupService(store=store, generator=generator, callback_url='https://example.invalid/callback')
    return SimpleNamespace(clock=clock, store=store, generator=generator, service=service)


@pytest.mark.parametrize('change', [{'type': 'evaluate'}, {'userId': 'other'}, {'finalReportId': ''}])
def test_delegation_validates_frozen_source_identity(change):
    with pytest.raises(ValueError, match='identity'):
        request(**change)


def test_delegation_copies_input_and_rejects_unknown_contract():
    original = request()
    other = GutBarrierFollowupRequest('u', 's', 'gut-main', original.body)
    original.body['report']['section'] = 'changed'
    assert other.body['report']['section'] == 'fixed synthetic input'
    with pytest.raises(ValueError, match='contract'):
        replace(other, contract_version='latest')


@pytest.mark.asyncio
async def test_permanent_receipt_is_scoped_and_does_not_expire_or_regenerate():
    env = setup()
    first = await env.service.submit(request())
    assert (await env.service.submit(request())).task_id == first.task_id
    with pytest.raises(FollowupConflictError):
        await env.service.submit(request(report={'section': 'different'}))
    other = await env.service.submit(request(user='other'))
    assert other.task_id != first.task_id
    with pytest.raises(KeyError):
        await env.service.get_receipt(user_id='other', session_id='s', task_id=first.task_id)
    await env.service.drain_generation()
    env.clock.advance(100000)
    assert (await env.service.submit(request())).task_id == first.task_id
    assert await env.service.drain_generation() == 0
    assert len(env.generator.calls) == 2


@pytest.mark.asyncio
async def test_three_isolated_artifacts_keep_original_callback_identity():
    env = setup()
    receipt = await env.service.submit(request())
    assert receipt.task_id != 'gut-main'
    await env.service.drain_generation()
    artifacts = await env.store.artifacts_for(receipt.task_id)
    assert {a['reportType'] for a in artifacts} == set(GUT_BARRIER_FOLLOWUP_TYPES)
    assert all(a['body']['finalReportId'] == 'gut-main' for a in artifacts)
    assert all(a['finalReportId'] == receipt.task_id for a in artifacts)
    assert len(env.store.deliveries.documents) == 3
    assert (
        await env.service.get_receipt(user_id='u', session_id='s', task_id=receipt.task_id)
    ).generation_status == 'completed'


@pytest.mark.asyncio
async def test_partial_generation_seals_only_missing_followups_without_rerunning_model():
    env = setup()
    env.generator.fail_after = 1
    receipt = await env.service.submit(request())
    await env.service.drain_generation()
    artifacts = {a['reportType']: a['body'] for a in await env.store.artifacts_for(receipt.task_id)}
    assert artifacts['explain_gutBarrier']['status'] == 'success'
    assert artifacts['opinion']['status'] == artifacts['instruction']['status'] == 'failed'
    assert all(a['finalReportId'] == 'gut-main' for a in artifacts.values())
    env.clock.advance(100)
    await env.service.recover_pending()
    assert len(env.generator.calls) == 1
    assert set(artifacts) == set(GUT_BARRIER_FOLLOWUP_TYPES)


@pytest.mark.asyncio
async def test_frozen_artifacts_survive_projection_outage_and_recover_without_model():
    env = setup()
    receipt = await env.service.submit(request())
    env.store.artifacts.unavailable = True
    await env.service.drain_generation()
    assert len(await env.store.artifacts_for(receipt.task_id)) == 3
    assert not env.store.deliveries.documents
    env.store.artifacts.unavailable = False
    env.clock.advance(31)
    assert await env.service.recover_pending() == 1
    assert len(env.generator.calls) == 1
    assert len(env.store.deliveries.documents) == 3
    assert all(a['body']['status'] == 'success' for a in await env.store.artifacts_for(receipt.task_id))


@pytest.mark.asyncio
async def test_expired_owner_cannot_freeze_artifact_after_new_owner_claim():
    env = setup()
    receipt = await env.service.submit(request())
    old = await env.store.claim_generation('old')
    env.clock.advance(31)
    new = await env.store.claim_generation('new', expired=True)
    body = {'type': 'opinion', 'userId': 'u', 'sessionId': 's', 'finalReportId': 'gut-main'}
    record = ReportArtifactRecord(
        receipt.task_id, 'u', 's', 'opinion', 'success', 'ai_unverified', content_hash(body), body
    )
    assert await env.store.save_frozen_artifact(old, record) == CONFLICT
    assert await env.store.save_frozen_artifact(new, record) == SAVED


@pytest.mark.asyncio
async def test_unavailable_execution_version_never_runs_current_generator():
    env = setup()
    receipt = await env.service.submit(request())
    env.service.execution_version = 'different-implementation'
    await env.service.drain_generation()
    assert env.generator.calls == []
    artifacts = await env.store.artifacts_for(receipt.task_id)
    assert len(artifacts) == 3
    assert all(a['body']['error']['error_code'] == 'REPORT_EXECUTION_VERSION_UNAVAILABLE' for a in artifacts)


@pytest.mark.asyncio
async def test_non_2xx_callbacks_continue_after_generation_completes():
    env = setup()
    await env.service.submit(request())
    await env.service.drain_generation()

    class Transport:
        calls = []
        status = 401

        async def post_once(self, url, body, *, idempotency_key):
            self.calls.append((deepcopy(body), idempotency_key))
            return CallbackResponse(self.status)

    transport = Transport()
    worker = ReportCallbackWorker(
        store=env.store,
        transport=transport,
        clock=env.clock,
        retry_delays_seconds=(1,),
        jitter_ratio=0,
        callback_scope='report-followup',
    )
    await worker.drain_pending()
    assert all(d['status'] == 'retry_wait' for d in env.store.deliveries.documents.values())
    env.clock.advance(2)
    transport.status = 204
    await worker.drain_pending()
    assert all(d['status'] == 'delivered' for d in env.store.deliveries.documents.values())
    assert len(transport.calls) == 6
    assert {key for _, key in transport.calls[:3]} == {key for _, key in transport.calls[3:]}
    assert all(body['finalReportId'] == 'gut-main' for body, _ in transport.calls)
    assert len(env.generator.calls) == 1


@pytest.mark.asyncio
async def test_accepted_generation_budget_is_frozen_and_timeout_does_not_stop_delivery():
    env = setup()
    env.service.generation_timeout_seconds = 0.01
    receipt = await env.service.submit(request())
    env.service.generation_timeout_seconds = 100

    async def blocked(*args, **kwargs):
        await asyncio.sleep(30)

    env.generator.generate = blocked
    await asyncio.wait_for(env.service.drain_generation(), timeout=2)
    job = await env.store.get_owned(receipt.task_id, user_id='u', session_id='s')
    assert job['generationTimeoutSeconds'] == 0.01
    assert job['generationState'] == 'completed'
    assert len(env.store.deliveries.documents) == 3
    assert all(a['body']['status'] == 'failed' for a in await env.store.artifacts_for(receipt.task_id))


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('userId', 'other'), ('finalReportId', 'other'), ('type', 'evaluate')])
async def test_mutated_request_identity_is_rejected_at_admission(field, value):
    env = setup()
    payload = request()
    payload.body[field] = value
    with pytest.raises(ValueError, match='identity'):
        await env.service.submit(payload)
    assert env.store.batches.documents == {}


@pytest.mark.asyncio
async def test_input_and_digest_are_detached_before_admission_awaits():
    env = setup()
    payload = request()
    began, release = asyncio.Event(), asyncio.Event()
    ensure_indexes = env.store.ensure_indexes

    async def paused():
        began.set()
        await release.wait()
        await ensure_indexes()

    env.store.ensure_indexes = paused
    task = asyncio.create_task(env.service.submit(payload))
    await began.wait()
    payload.body['report']['section'] = 'changed during admission'
    payload.body['userId'] = 'other'
    release.set()
    receipt = await task
    stored = await env.store.get_owned(receipt.task_id, user_id='u', session_id='s')
    assert stored['request']['report']['section'] == 'fixed synthetic input'
    assert stored['request']['userId'] == 'u'
    assert stored['inputDigest'] == content_hash(stored['request'])
