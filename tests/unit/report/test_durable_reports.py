import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.report.artifact_store import MongoReportArtifactStore
from adapters.report.delivery import ReportCallbackTransport
from adapters.report.durable_store import MongoReportWorkflowStore
from modules.report.artifacts import REPORT_TYPES
from modules.report.artifacts import EVALUATE_REPORT_TYPES
from modules.report.artifacts import bind_report_batch
from modules.report.artifacts import reset_report_batch
from modules.report.artifacts import save_report_artifact
from modules.report.durable import DurableReportService
from modules.report.durable import ReportCallbackWorker
from modules.report.durable import next_delivery_time
from modules.report.durable_ports import ACTIVE_GENERATION
from modules.report.durable_ports import CallbackResponse
from modules.report.schemas import ReportRequest
from modules.report.task_coordinator import ReportExecutionResult
from tests.unit.report.durable_fakes import Clock
from tests.unit.report.durable_fakes import Collection


class GeneratingPipeline:
    def __init__(self, artifact_store, *, fail_after=None):
        self.store = artifact_store
        self.calls = 0
        self.fail_after = fail_after

    async def execute_and_callback(self, request, *, final_report_id, defer_callbacks):
        assert defer_callbacks is True
        self.calls += 1
        token = bind_report_batch(store=self.store, final_report_id=final_report_id,
                                  user_id=request.user_id, session_id=request.session_id,
                                  defer_callbacks=True)
        payloads = []
        try:
            for index, report_type in enumerate(EVALUATE_REPORT_TYPES):
                if self.fail_after == index:
                    raise RuntimeError('simulated model failure')
                body = {'type': report_type, 'finalReportId': final_report_id,
                        'userId': request.user_id, 'sessionId': request.session_id,
                        'status': 'success', 'error': {'error_code': None, 'message': None},
                        'sampleData': [0, False, {}, []]}
                await save_report_artifact(report_type, body, generation_status='success')
                payloads.append(body)
            return ReportExecutionResult(payloads[0], tuple(payloads[1:]))
        finally:
            reset_report_batch(token)


def setup(*, fail_after=None):
    clock = Clock()
    batches, artifacts, deliveries = Collection(), Collection(), Collection()
    workflow = MongoReportWorkflowStore(batches=batches, artifacts=artifacts,
                                        deliveries=deliveries, clock=clock)
    archive = MongoReportArtifactStore(batches=batches, artifacts=artifacts, workflow=workflow)
    pipeline = GeneratingPipeline(archive, fail_after=fail_after)
    service = DurableReportService(store=workflow, artifact_store=archive, pipeline=pipeline,
                                   callback_url='http://receiver.test/callback')
    return SimpleNamespace(clock=clock, batches=batches, artifacts=artifacts, deliveries=deliveries,
                           workflow=workflow, archive=archive, pipeline=pipeline, service=service)


REQUEST = ReportRequest(user_id='u', session_id='s')


@pytest.mark.asyncio
async def test_report_twenty_job_limit_does_not_change_other_report_policy():
    from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY

    env = setup()
    assert env.service.concurrency == 20
    assert DEFAULT_REPORT_FALLBACK_POLICY.max_concurrent_evaluate_reports == 2
    for index in range(25):
        await env.service.submit(ReportRequest(user_id='u', session_id=f'concurrency-{index}'))
    active = peak = 0
    all_started, release = asyncio.Event(), asyncio.Event()
    execute = env.pipeline.execute_and_callback

    async def blocked(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 20:
            all_started.set()
        try:
            await release.wait()
            return await execute(*args, **kwargs)
        finally:
            active -= 1

    env.pipeline.execute_and_callback = blocked
    drain = asyncio.create_task(env.service.drain_generation())
    try:
        await asyncio.wait_for(all_started.wait(), timeout=5)
        assert peak == 20
        assert await env.service.drain_generation() == 0
    finally:
        release.set()
        assert await drain == 25
    assert await env.service.drain_generation() == 0
    assert env.pipeline.calls == 25
    assert peak == 20


@pytest.mark.asyncio
async def test_twenty_running_jobs_refill_one_free_slot_without_waiting_for_peers():
    env = setup()
    started = [asyncio.Event() for _ in range(22)]
    release = [asyncio.Event() for _ in range(22)]
    active = peak = 0
    execute = env.pipeline.execute_and_callback

    async def blocked(request, **kwargs):
        nonlocal active, peak
        index = int(request.session_id)
        active += 1
        peak = max(peak, active)
        started[index].set()
        try:
            await release[index].wait()
            return await execute(request, **kwargs)
        finally:
            active -= 1

    env.pipeline.execute_and_callback = blocked
    for index in range(22):
        await env.service.submit(ReportRequest(user_id='u', session_id=str(index)))
        env.clock.advance(0.01)
    drain = asyncio.create_task(env.service.drain_generation())
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started[:20])), 2)
        assert not started[20].is_set() and not started[21].is_set()
        release[0].set()
        await asyncio.wait_for(started[20].wait(), 2)
        assert active == 20 and not started[21].is_set()
        assert all(not event.is_set() for event in release[1:20])
        # The completed report is already archived and can be delivered while
        # nineteen unrelated reports remain blocked.
        assert len(env.artifacts.documents) == len(env.deliveries.documents) == 4
        delivery_worker, transport = worker(env)
        assert await delivery_worker.drain_pending() == 4
        assert transport.post_once.await_count == 4
        release[20].set()
        await asyncio.wait_for(started[21].wait(), 2)
        assert active == peak == 20
    finally:
        for event in release:
            event.set()
        await asyncio.wait_for(drain, 5)
    assert env.pipeline.calls == 22
    assert not env.service._generation_tasks


@pytest.mark.asyncio
async def test_later_requests_use_free_slots_and_concurrent_dispatches_share_the_limit():
    env = setup()
    first_started, twenty_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    active = peak = 0
    calls = []
    execute = env.pipeline.execute_and_callback

    async def blocked(request, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(request.session_id)
        first_started.set()
        if active == 20:
            twenty_started.set()
        try:
            await release.wait()
            return await execute(request, **kwargs)
        finally:
            active -= 1

    env.pipeline.execute_and_callback = blocked
    await env.service.submit(REQUEST)
    drains = [asyncio.create_task(env.service.drain_generation())]
    try:
        await asyncio.wait_for(first_started.wait(), 2)
        for index in range(25):
            await env.service.submit(ReportRequest(user_id='u', session_id=f'later-{index}'))
        drains.extend(asyncio.create_task(env.service.drain_generation()) for _ in range(5))
        await asyncio.wait_for(twenty_started.wait(), 2)
        assert active == peak == 20 and len(calls) == 20
        assert sum(row['generationState'] == 'queued' for row in env.batches.documents.values()) == 6
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*drains), 5)
    assert len(calls) == len(set(calls)) == env.pipeline.calls == 26
    assert peak == 20 and not env.service._generation_tasks


@pytest.mark.asyncio
async def test_stopping_admission_drains_active_jobs_without_claiming_the_next_one():
    env = setup()
    env.service.concurrency = 1
    started, release = asyncio.Event(), asyncio.Event()
    execute = env.pipeline.execute_and_callback

    async def blocked(*args, **kwargs):
        started.set()
        await release.wait()
        return await execute(*args, **kwargs)

    env.pipeline.execute_and_callback = blocked
    await env.service.submit(REQUEST)
    env.clock.advance(0.01)
    queued = await env.service.submit(ReportRequest(user_id='u', session_id='queued'))
    drain = asyncio.create_task(env.service.drain_generation())
    try:
        await asyncio.wait_for(started.wait(), 2)
        env.service.stop_accepting()
    finally:
        release.set()
        assert await asyncio.wait_for(drain, 2) == 1
    assert env.pipeline.calls == 1
    assert env.batches.documents[queued['_id']]['generationState'] == 'queued'
    assert not env.service._generation_tasks


@pytest.mark.asyncio
async def test_cancelling_one_dispatch_cleans_its_job_without_cancelling_other_dispatches():
    env = setup()
    env.service.concurrency = 2
    started = {name: asyncio.Event() for name in ('first', 'second', 'replacement')}
    stopped = {name: asyncio.Event() for name in started}
    release = asyncio.Event()
    execute = env.pipeline.execute_and_callback

    async def blocked(request, **kwargs):
        started[request.session_id].set()
        try:
            await release.wait()
            return await execute(request, **kwargs)
        finally:
            stopped[request.session_id].set()

    env.pipeline.execute_and_callback = blocked
    drains = []
    try:
        for name in ('first', 'second'):
            await env.service.submit(ReportRequest(user_id='u', session_id=name))
            drains.append(asyncio.create_task(env.service.drain_generation()))
            await asyncio.wait_for(started[name].wait(), 2)
        drains[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await drains[0]
        assert stopped['first'].is_set() and not stopped['second'].is_set()
        assert len(env.service._generation_tasks) == 1
        await env.service.submit(ReportRequest(user_id='u', session_id='replacement'))
        drains.append(asyncio.create_task(env.service.drain_generation()))
        await asyncio.wait_for(started['replacement'].wait(), 2)
        assert len(env.service._generation_tasks) == 2
        assert not stopped['second'].is_set()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*drains, return_exceptions=True), 5)
    assert env.pipeline.calls == 2
    assert not env.service._generation_tasks


@pytest.mark.asyncio
async def test_queue_read_failure_does_not_cancel_already_claimed_reports():
    env = setup()
    env.service.concurrency = 2
    started, release = asyncio.Event(), asyncio.Event()
    execute = env.pipeline.execute_and_callback
    claim = env.workflow.claim_generation
    claims = 0

    async def flaky_claim(*args, **kwargs):
        nonlocal claims
        claims += 1
        if claims == 2:
            raise RuntimeError('temporary queue read failure')
        return await claim(*args, **kwargs)

    async def blocked(*args, **kwargs):
        started.set()
        await release.wait()
        return await execute(*args, **kwargs)

    env.workflow.claim_generation = flaky_claim
    env.pipeline.execute_and_callback = blocked
    for name in ('first', 'next'):
        await env.service.submit(ReportRequest(user_id='u', session_id=name))
    drain = asyncio.create_task(env.service.drain_generation())
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert not drain.done()
        assert len(env.service._generation_tasks) == 1
        assert sum(row['generationState'] == 'queued' for row in env.batches.documents.values()) == 1
    finally:
        release.set()
        assert await asyncio.wait_for(drain, 5) == 2
    assert env.pipeline.calls == 2
    assert not env.service._generation_tasks


async def complete(env):
    batch = await env.service.submit(REQUEST)
    await env.service.drain_generation()
    return batch


async def save_claimed(env, job, report_type='evaluate', body=None):
    token = ACTIVE_GENERATION.set((job['_id'], job['leaseToken']))
    batch_token = bind_report_batch(store=env.archive, final_report_id=job['_id'],
                                    user_id='u', session_id='s', defer_callbacks=True)
    try:
        return await save_report_artifact(report_type, body or {
            'type': report_type, 'finalReportId': job['_id'], 'status': 'success',
            'userId': 'u', 'sessionId': 's', 'data': [0, False, {}, []],
        }, generation_status='success')
    finally:
        reset_report_batch(batch_token)
        ACTIVE_GENERATION.reset(token)


def worker(env, *, response=CallbackResponse(200), concurrency=4, batch_size=100):
    transport = SimpleNamespace(post_once=AsyncMock(return_value=response))
    return ReportCallbackWorker(store=env.workflow, transport=transport, clock=env.clock,
                                jitter=lambda: 1, concurrency=concurrency, batch_size=batch_size), transport


@pytest.mark.asyncio
async def test_durable_admission_coalesces_across_instances_and_preserves_history():
    env = setup()
    other = DurableReportService(store=env.workflow, artifact_store=env.archive, pipeline=env.pipeline,
                                  callback_url='http://other.test/callback')
    first, repeated = await asyncio.gather(env.service.submit(REQUEST), other.submit(REQUEST))
    assert first['_id'] == repeated['_id']
    assert len(env.batches.documents) == 1
    assert first['request']['userId'] == 'u'
    assert first['callbackUrl'] == 'http://receiver.test/callback'
    assert env.pipeline.calls == 0
    await env.service.drain_generation()
    delivery_worker, transport = worker(env)
    assert await delivery_worker.drain_pending() == 4
    assert transport.post_once.await_count == 4
    same = await other.submit(REQUEST)
    assert same['_id'] == first['_id']
    assert await env.service.drain_generation() == 0
    assert await delivery_worker.drain_pending() == 0
    assert env.pipeline.calls == 1
    env.clock.advance(901)
    new = await env.service.submit(REQUEST)
    assert new['_id'] != first['_id']
    assert len(env.artifacts.documents) == 4
    assert all('expireAfterSeconds' not in options for collection in
               [env.batches, env.artifacts, env.deliveries] for _, options in collection.indexes)


@pytest.mark.asyncio
async def test_database_failure_does_not_accept_a_job():
    env = setup()
    env.batches.unavailable = True
    with pytest.raises(ConnectionError):
        await env.service.submit(REQUEST)
    assert env.pipeline.calls == 0
    assert not env.artifacts.documents and not env.deliveries.documents


@pytest.mark.asyncio
async def test_queued_job_survives_service_replacement():
    env = setup()
    batch = await env.service.submit(REQUEST)
    restarted = DurableReportService(store=env.workflow, artifact_store=env.archive,
                                     pipeline=env.pipeline, callback_url='http://new-config.test/callback')
    assert await restarted.drain_generation() == 1
    assert env.batches.documents[batch['_id']]['generationStatus'] == 'success'
    assert len(env.artifacts.documents) == len(env.deliveries.documents) == 4
    assert all(row['targetUrl'] == 'http://receiver.test/callback' for row in env.deliveries.documents.values())
    assert all(row['status'] == 'pending' for row in env.deliveries.documents.values())


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_after', [None, 0, 1])
async def test_evaluate_reconciliation_only_enqueues_its_own_four_report_types(fail_after):
    env = setup(fail_after=fail_after)
    batch = await complete(env)
    expected = {'evaluate', 'instruction', 'opinion', 'explain'}
    assert {row['reportType'] for row in env.artifacts.documents.values()} == expected
    assert {row['reportType'] for row in env.deliveries.documents.values()} == expected
    assert not env.batches.documents[batch['_id']]['reconcileNeeded']
    assert env.batches.documents[batch['_id']]['generationState'] == 'completed'
    await env.service.recover_pending()
    assert f'{batch["_id"]}:explain_gutBarrier' not in env.artifacts.documents
    assert env.pipeline.calls == 1


@pytest.mark.asyncio
async def test_recovery_preserves_and_delivers_existing_historical_extra_report():
    env = setup()
    batch = await env.service.submit(REQUEST)
    job = await env.workflow.claim_generation('old-process')
    await save_claimed(env, job)
    await save_claimed(env, job, 'explain_gutBarrier')
    key = f'{batch["_id"]}:explain_gutBarrier'
    frozen_artifact = deepcopy(env.artifacts.documents[key])
    frozen_delivery = deepcopy(env.deliveries.documents[key])
    env.clock.advance(121)

    assert await env.service.recover_pending() == 1
    assert env.artifacts.documents[key] == frozen_artifact
    assert env.deliveries.documents[key] == frozen_delivery
    assert len(env.deliveries.documents) == 5
    assert not env.batches.documents[batch['_id']]['reconcileNeeded']
    assert env.pipeline.calls == 0
    delivery_worker, transport = worker(env)
    assert await delivery_worker.drain_pending() == 5
    assert any(call.args[1] == frozen_artifact['body'] and call.kwargs['idempotency_key'] == key
               for call in transport.post_once.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_after', [0, 1, 3])
async def test_generation_failure_preserves_completed_types_and_completes_all_results(fail_after):
    env = setup(fail_after=fail_after)
    batch = await complete(env)
    assert env.pipeline.calls == 1
    assert len(env.artifacts.documents) == len(env.deliveries.documents) == 4
    for index, report_type in enumerate(EVALUATE_REPORT_TYPES):
        document = env.artifacts.documents[f'{batch["_id"]}:{report_type}']
        assert document['body']['status'] == ('success' if index < fail_after else 'failed')
        assert document['body']['finalReportId'] == batch['_id']
        if index >= fail_after:
            error_field = 'errorCode' if report_type == 'evaluate' else 'error_code'
            expected_error = (
                'REPORT_DEPENDENCY_FAILED' if fail_after == 0 and report_type != 'evaluate'
                else 'REPORT_GENERATION_FAILED'
            )
            assert document['body']['error'][error_field] == expected_error
    expected = 'failed' if fail_after == 0 else 'partial_success'
    assert env.batches.documents[batch['_id']]['generationStatus'] == expected


@pytest.mark.asyncio
async def test_expired_generation_is_failed_without_model_rerun_and_late_writer_is_rejected():
    env = setup()
    batch = await env.service.submit(REQUEST)
    old_job = await env.workflow.claim_generation('dead-process')
    assert await save_claimed(env, old_job) == 'saved'
    original = env.artifacts.documents[f'{batch["_id"]}:evaluate']['body'].copy()
    env.clock.advance(121)
    assert await env.service.recover_pending() == 1
    assert env.pipeline.calls == 0
    assert env.artifacts.documents[f'{batch["_id"]}:evaluate']['body'] == original
    for report_type in EVALUATE_REPORT_TYPES[1:]:
        error = env.artifacts.documents[f'{batch["_id"]}:{report_type}']['body']['error']
        assert error['error_code'] == 'REPORT_GENERATION_INTERRUPTED'
    assert await save_claimed(env, old_job, 'instruction', {'changed': True}) == 'conflict'
    assert env.artifacts.documents[f'{batch["_id"]}:instruction']['body']['status'] == 'failed'
    assert len(env.deliveries.documents) == 4


@pytest.mark.asyncio
async def test_archive_to_outbox_crash_gap_is_repaired_from_original_body():
    env = setup()
    batch = await env.service.submit(REQUEST)
    job = await env.workflow.claim_generation('dead-process')
    real_enqueue = env.workflow.ensure_delivery
    env.workflow.ensure_delivery = AsyncMock(side_effect=ConnectionError('queue write unavailable'))
    assert await save_claimed(env, job) == 'saved'
    original = env.artifacts.documents[f'{batch["_id"]}:evaluate'].copy()
    assert not env.deliveries.documents
    env.workflow.ensure_delivery = real_enqueue
    env.clock.advance(121)
    await env.service.recover_pending()
    assert env.artifacts.documents[original['_id']] == original
    assert len(env.deliveries.documents) == 4
    assert env.batches.documents[batch['_id']]['reconcileNeeded'] is False


@pytest.mark.asyncio
async def test_generation_and_callback_leases_are_exclusive_and_fenced():
    env = setup()
    await env.service.submit(REQUEST)
    first, second = await asyncio.gather(env.workflow.claim_generation('one'), env.workflow.claim_generation('two'))
    assert sum(job is not None for job in [first, second]) == 1
    job = first or second
    await save_claimed(env, job)
    a, b = await asyncio.gather(env.workflow.claim_delivery('one'), env.workflow.claim_delivery('two'))
    assert sum(item is not None for item in [a, b]) == 1
    old = a or b
    env.clock.advance(121)
    new = await env.workflow.claim_delivery('restarted')
    assert new['_id'] == old['_id'] and new['leaseToken'] != old['leaseToken']
    assert not await env.workflow.acknowledge_delivery(old)
    assert await env.workflow.acknowledge_delivery(new)
    assert not await env.workflow.retry_delivery(old, next_retry_at=env.clock(), error={'code': 'OLD'})


@pytest.mark.asyncio
async def test_generation_heartbeat_loss_cancels_computation_for_recovery():
    env = setup()
    cancelled = asyncio.Event()

    async def slow(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    env.service.pipeline = SimpleNamespace(execute_and_callback=slow)
    env.service.heartbeat_seconds = 0.001
    env.workflow.renew_generation = AsyncMock(return_value=False)
    await env.service.submit(REQUEST)
    assert await env.service.drain_generation() == 1
    assert cancelled.is_set()
    env.clock.advance(121)
    await env.service.recover_pending()
    assert len(env.deliveries.documents) == 4


@pytest.mark.asyncio
async def test_lost_generation_lease_keeps_healthy_peer_supervised():
    env = setup()
    lost = asyncio.Event()
    generate = env.pipeline.execute_and_callback
    renew = env.workflow.renew_generation

    async def execute(request, **kwargs):
        if request.user_id == 'u':
            try:
                await asyncio.Event().wait()
            finally:
                lost.set()
        await lost.wait()
        return await generate(request, **kwargs)

    async def renew_one(job):
        return False if job['userId'] == 'u' else await renew(job)

    env.pipeline.execute_and_callback = execute
    env.workflow.renew_generation = renew_one
    env.service.heartbeat_seconds = 0.001
    interrupted = await env.service.submit(REQUEST)
    healthy = await env.service.submit(ReportRequest(user_id='healthy', session_id='s'))
    assert await asyncio.wait_for(env.service.drain_generation(), 2) == 2
    assert env.batches.documents[healthy['_id']]['generationStatus'] == 'success'
    assert env.pipeline.calls == 1
    assert env.batches.documents[interrupted['_id']]['generationState'] == 'running'
    env.clock.advance(121)
    await env.service.recover_pending()
    assert env.batches.documents[interrupted['_id']]['generationStatus'] == 'failed'
    assert len(env.deliveries.documents) == 8


@pytest.mark.asyncio
async def test_followup_owner_cancellation_cleans_children_while_waiting_for_dependencies(monkeypatch):
    from modules.report import reports

    names = ('opinion', 'explain', 'info', 'catalog')
    started = {name: asyncio.Event() for name in names}
    cancelled = set()

    async def blocked(name):
        started[name].set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.add(name)

    async def module(report_type, *args, **kwargs):
        return await blocked(report_type)

    monkeypatch.setattr(reports, '_generate_followup_module', module)
    monkeypatch.setattr(reports, '_info', lambda data: blocked('info'))
    monkeypatch.setattr(reports, 'fetch_catalog', lambda policy: blocked('catalog'))
    owner = asyncio.create_task(reports._push_report_with_active_gateway(
        {'diagnostic_basis': {'abnormal_findings': ['test observation']}}, 'u', 's',
    ))
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 2)
    finally:
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
    assert cancelled == set(names)


@pytest.mark.asyncio
async def test_backlog_excludes_delivered_reports_and_reports_oldest_wait():
    env = setup()
    await complete(env)
    env.clock.advance(7200)
    summary = await env.workflow.backlog()
    assert summary['pending'] == 4 and summary['oldestWaitSeconds'] == 7200
    delivery_worker, _ = worker(env)
    await delivery_worker.drain_pending()
    assert await env.workflow.backlog() == {
        'pending': 0, 'oldestPendingAt': None, 'oldestWaitSeconds': 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('report_type', REPORT_TYPES[1:])
async def test_followup_partial_result_is_immediately_archived_and_queued(report_type):
    from modules.report.followup_defaults import default_followup_report
    from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY
    from modules.report.reports import _generate_followup_module

    env = setup()
    batch = await env.service.submit(REQUEST)
    job = await env.workflow.claim_generation('generating')
    body = default_followup_report(report_type, evaluate={}, user_id='u', session_id='s')
    # A typed empty shape is no longer a fabricated successful result. Supply a
    # real test finding so this test checks immediate partial-result persistence.
    if report_type == 'instruction':
        finding_path = ('info', 'body_mass')
    elif report_type == 'opinion':
        finding_path = ('diagnosis_summary', 'conclusion')
    else:
        finding_path = ('preface', 'opening_script', 'text')
    finding = body
    for key in finding_path[:-1]:
        finding = finding[key]
    finding[finding_path[-1]] = '可用的测试内容'
    lease = ACTIVE_GENERATION.set((job['_id'], job['leaseToken']))
    token = bind_report_batch(store=env.archive, final_report_id=job['_id'], user_id='u', session_id='s',
                              defer_callbacks=True)
    try:
        result, _ = await _generate_followup_module(
            report_type, AsyncMock(return_value=body), data={'status': 'partial_success'},
            user_id='u', session_id='s', policy=DEFAULT_REPORT_FALLBACK_POLICY,
        )
    finally:
        reset_report_batch(token)
        ACTIVE_GENERATION.reset(lease)
    artifact = env.artifacts.documents[f'{batch["_id"]}:{report_type}']
    assert artifact['body'] == result
    saved_finding = artifact['body']
    for key in finding_path:
        saved_finding = saved_finding[key]
    assert saved_finding == '可用的测试内容'
    assert artifact['generationStatus'] == result['status'] == 'partial_success'
    assert result['error'] == {'error_code': None, 'message': None}
    assert env.deliveries.documents[artifact['_id']]['status'] == 'pending'
    assert len(env.artifacts.documents) == 1  # Other types need not finish first.


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 201, 204, 301, 307, 400, 401, 403, 404, 429, 500, 503])
async def test_single_http_attempt_and_all_unconfirmed_statuses_are_retained(status):
    env = setup()
    await complete(env)
    calls = []

    async def receive(request):
        calls.append(request)
        return httpx.Response(status, headers={'Location': 'https://do-not-follow.test/', 'Retry-After': '600'})

    transport = ReportCallbackTransport(transport=httpx.MockTransport(receive))
    delivery_worker = ReportCallbackWorker(store=env.workflow, transport=transport,
                                           clock=env.clock, jitter=lambda: 1, batch_size=1)
    try:
        await delivery_worker.drain_pending()
    finally:
        await transport.aclose()
    assert len(calls) == 1
    key = calls[0].headers['Idempotency-Key']
    assert json.loads(calls[0].content) == env.artifacts.documents[key]['body']
    state = env.deliveries.documents[key]
    assert state['attempts'] == 1
    if 200 <= status < 300:
        assert state['status'] == 'delivered'
        assert 'nextRetryAt' not in state
    else:
        assert state['status'] == 'retry_wait'
        assert state['nextRetryAt'] == env.clock() + timedelta(seconds=600)


@pytest.mark.asyncio
@pytest.mark.parametrize('scope', ['report', 'gut_barrier'])
async def test_production_worker_retains_old_test_target_without_sending_or_acknowledging(monkeypatch, scope):
    from adapters.gut_barrier.delivery import GutBarrierCallbackTransport

    env = setup()
    env.service.callback_url = 'https://test.slmc.top/api/basic-health-assessment/ai-full-back/callback'
    await complete(env)  # Persisted under the former deployment configuration.
    frozen_artifacts = deepcopy(env.artifacts.documents)
    frozen_targets = {key: row['targetUrl'] for key, row in env.deliveries.documents.items()}
    acknowledge = AsyncMock(wraps=env.workflow.acknowledge_delivery)
    monkeypatch.setattr(env.workflow, 'acknowledge_delivery', acknowledge)
    monkeypatch.setattr('config.env.ENVIRONMENT', 'production')
    calls = []

    def receive(request):
        calls.append(request)
        return httpx.Response(204)

    http = httpx.MockTransport(receive)
    transport = (
        GutBarrierCallbackTransport(store=env.workflow, transport=http)
        if scope == 'gut_barrier' else ReportCallbackTransport(transport=http)
    )
    delivery_worker = ReportCallbackWorker(
        store=env.workflow, transport=transport, clock=env.clock, jitter=lambda: 1,
        batch_size=1, callback_scope=scope,
    )
    try:
        assert await delivery_worker.drain_pending() == 1
    finally:
        await transport.aclose()
    assert calls == []
    acknowledge.assert_not_awaited()
    assert env.artifacts.documents == frozen_artifacts
    assert {key: row['targetUrl'] for key, row in env.deliveries.documents.items()} == frozen_targets
    attempted = [row for row in env.deliveries.documents.values() if row['attempts'] == 1]
    assert len(attempted) == 1
    assert attempted[0]['status'] == 'retry_wait'
    assert attempted[0]['lastError']['code'] == 'ValueError'


@pytest.mark.asyncio
async def test_connection_failure_and_timeout_remain_retryable():
    env = setup()
    await complete(env)
    delivery_worker, transport = worker(env, batch_size=1)
    transport.post_once.side_effect = httpx.ConnectError('receiver unavailable')
    await delivery_worker.drain_pending()
    assert any(row.get('lastError', {}).get('code') == 'ConnectError' for row in env.deliveries.documents.values())

    async def hung(*args, **kwargs):
        await asyncio.Event().wait()

    transport.post_once.side_effect = hung
    delivery_worker.timeout_seconds = 0.001
    await delivery_worker.drain_pending()
    assert any(row.get('lastError', {}).get('code') == 'TimeoutError' for row in env.deliveries.documents.values())


@pytest.mark.asyncio
async def test_acknowledgement_storage_failure_repeats_same_body_and_key():
    env = setup()
    await complete(env)
    delivery_worker, transport = worker(env, batch_size=1)
    acknowledge = env.workflow.acknowledge_delivery
    env.workflow.acknowledge_delivery = AsyncMock(side_effect=ConnectionError('lost acknowledgement'))
    await delivery_worker.drain_pending()
    first_call = transport.post_once.call_args
    env.workflow.acknowledge_delivery = acknowledge
    env.clock.advance(3600)
    # Drain all pending types including the previously accepted result.
    delivery_worker.batch_size = 100
    await delivery_worker.drain_pending()
    repeats = [call for call in transport.post_once.call_args_list if call.kwargs == first_call.kwargs]
    assert len(repeats) == 2 and repeats[0] == repeats[1]
    assert all(row['status'] == 'delivered' for row in env.deliveries.documents.values())
    assert env.pipeline.calls == 1


@pytest.mark.asyncio
async def test_queue_retries_for_months_without_max_attempts_or_regeneration():
    env = setup()
    await complete(env)
    delivery_worker, transport = worker(env, response=CallbackResponse(401))
    for _ in range(40):
        await delivery_worker.drain_pending()
        env.clock.advance(86400 * 30)
    assert all(row['attempts'] == 40 and row['status'] == 'retry_wait' for row in env.deliveries.documents.values())
    transport.post_once.return_value = CallbackResponse(204)
    await delivery_worker.drain_pending()
    assert all(row['status'] == 'delivered' for row in env.deliveries.documents.values())
    assert env.pipeline.calls == 1


def test_backoff_and_retry_after_parsing():
    now = Clock()()
    assert [int((next_delivery_time(n, now, jitter=lambda: 1) - now).total_seconds()) for n in
            [1, 2, 3, 4, 100000]] == [30, 120, 600, 3600, 3600]
    assert next_delivery_time(1, now, retry_after='Tue, 15 Sep 2026 00:00:00 GMT') == now + timedelta(days=1)
    assert next_delivery_time(1, now, retry_after='broken', jitter=lambda: 1) == now + timedelta(seconds=30)
    assert next_delivery_time(1, now, retry_after='9' * 10000, jitter=lambda: 1) == now + timedelta(seconds=30)


def test_custom_backoff_repeats_last_delay_and_retry_after_never_shortens_it():
    now = Clock()()
    settings = {'retry_delays_seconds': (8, 20), 'jitter_ratio': 0}
    assert [(next_delivery_time(n, now, **settings) - now).total_seconds() for n in
            [1, 2, 3, 100000]] == [8, 20, 20, 20]
    assert next_delivery_time(1, now, retry_after='60', **settings) == now + timedelta(seconds=60)
    assert next_delivery_time(1, now, retry_after='1', **settings) == now + timedelta(seconds=8)
    assert next_delivery_time(1, now, retry_delays_seconds=(100,), jitter_ratio=0.25,
                              jitter=lambda: 0) == now + timedelta(seconds=75)
    assert next_delivery_time(1, now, retry_delays_seconds=(100,), jitter_ratio=0.25,
                              jitter=lambda: 1) == now + timedelta(seconds=100)


@pytest.mark.asyncio
async def test_changed_policy_preserves_pending_due_time_and_applies_to_next_failure():
    env = setup()
    await complete(env)
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(503)))
    original = ReportCallbackWorker(store=env.workflow, transport=transport, clock=env.clock,
                                    retry_delays_seconds=(5, 10), jitter_ratio=0)
    await original.drain_pending()
    due = env.clock() + timedelta(seconds=5)
    assert all(row['nextRetryAt'] == due for row in env.deliveries.documents.values())
    restarted = ReportCallbackWorker(store=env.workflow, transport=transport, clock=env.clock,
                                     retry_delays_seconds=(90,), jitter_ratio=0)
    env.clock.advance(4)
    assert await restarted.drain_pending() == 0
    env.clock.advance(1)
    assert await restarted.drain_pending() == 4
    assert all(row['attempts'] == 2 and row['nextRetryAt'] == env.clock() + timedelta(seconds=90)
               for row in env.deliveries.documents.values())
    transport.post_once.return_value = CallbackResponse(200)
    env.clock.advance(90)
    assert await restarted.drain_pending() == 4
    assert await restarted.drain_pending() == 0
    assert env.pipeline.calls == 1


@pytest.mark.asyncio
async def test_backlog_summary_frequency_is_independent_from_scanning():
    env = setup()
    env.workflow.backlog = AsyncMock(wraps=env.workflow.backlog)
    delivery_worker = ReportCallbackWorker(store=env.workflow, transport=SimpleNamespace(), clock=env.clock,
                                           backlog_log_interval_seconds=60)
    await delivery_worker.drain_pending()
    env.clock.advance(59)
    await delivery_worker.drain_pending()
    assert env.workflow.backlog.await_count == 1
    env.clock.advance(1)
    await delivery_worker.drain_pending()
    assert env.workflow.backlog.await_count == 2


@pytest.mark.asyncio
async def test_backlog_logs_identify_independent_queues_without_sharing_throttle(monkeypatch):
    from modules.report import durable
    from utils.log.context import LogContext
    from utils.log.context import bind_log_context
    from utils.log.context import log_context

    clock = Clock()
    stores = [
        SimpleNamespace(
            claim_delivery=AsyncMock(return_value=None),
            backlog=AsyncMock(
                return_value={
                    'pending': pending,
                    'oldestPendingAt': '2026-10-07T00:00:00+00:00' if pending else None,
                    'oldestWaitSeconds': 12 if pending else 0,
                }
            ),
        )
        for pending in (0, 2)
    ]
    workers = [
        ReportCallbackWorker(store=stores[0], transport=SimpleNamespace(), clock=clock),
        ReportCallbackWorker(store=stores[1], transport=SimpleNamespace(), clock=clock, callback_scope='gut_barrier'),
    ]
    observed = []

    def capture(message, *args):
        observed.append((message % args, log_context()))

    monkeypatch.setattr(durable.logger, 'info', capture)
    with bind_log_context(user_id='trigger', request_uuid='request') as caller:
        await asyncio.gather(*(item.drain_pending() for item in workers))
        clock.advance(299)
        await asyncio.gather(*(item.drain_pending() for item in workers))
        assert log_context() == caller
        assert len(observed) == 2
        assert [store.backlog.await_count for store in stores] == [1, 1]
        clock.advance(1)
        await asyncio.gather(*(item.drain_pending() for item in workers))
        assert log_context() == caller

    expected = {
        '报告回调积压: pending=0 oldest_pending_at=None oldest_wait_seconds=0 callback_scope=report',
        '报告回调积压: pending=2 oldest_pending_at=2026-10-07T00:00:00+00:00 '
        'oldest_wait_seconds=12 callback_scope=gut_barrier',
    }
    assert len(observed) == 4
    assert {message for message, _ in observed} == expected
    assert all(context == LogContext() for _, context in observed)
    assert [store.backlog.await_count for store in stores] == [2, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize('query_fails', [False, True])
async def test_concurrent_backlog_scans_reserve_one_interval_before_query_io(monkeypatch, query_fails):
    from modules.report import durable

    entered, release = asyncio.Event(), asyncio.Event()

    async def query():
        entered.set()
        await release.wait()
        if query_fails:
            raise ConnectionError('offline failure')
        return {'pending': 0, 'oldestPendingAt': None, 'oldestWaitSeconds': 0}

    store = SimpleNamespace(claim_delivery=AsyncMock(return_value=None), backlog=AsyncMock(side_effect=query))
    delivery_worker = ReportCallbackWorker(
        store=store, transport=SimpleNamespace(), clock=Clock(), callback_scope='gut_barrier'
    )
    messages = []

    def capture(message, *args):
        messages.append(message % args)

    monkeypatch.setattr(durable.logger, 'info', capture)
    monkeypatch.setattr(durable.logger, 'warning', capture)
    scans = asyncio.gather(*(delivery_worker.drain_pending() for _ in range(20)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert store.backlog.await_count == 1
    finally:
        release.set()
        assert await asyncio.wait_for(scans, 2) == [0] * 20

    await delivery_worker.drain_pending()
    assert store.backlog.await_count == 1
    assert messages == [
        '报告回调积压查询失败: error_type=ConnectionError callback_scope=gut_barrier'
        if query_fails
        else '报告回调积压: pending=0 oldest_pending_at=None oldest_wait_seconds=0 callback_scope=gut_barrier'
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', [204, 503, 'connection', 'acknowledgement', 'state'])
async def test_callback_logs_keep_default_report_scope_and_persisted_identity(monkeypatch, outcome):
    from modules.report import durable
    from utils.log.context import LogContext
    from utils.log.context import bind_log_context
    from utils.log.context import log_context

    env = setup()
    await complete(env)
    status = outcome if isinstance(outcome, int) else 503 if outcome == 'state' else 204
    delivery_worker, transport = worker(env, response=CallbackResponse(status))
    if outcome == 'connection':
        transport.post_once.side_effect = ConnectionError('offline failure')
    elif outcome == 'acknowledgement':
        monkeypatch.setattr(env.workflow, 'acknowledge_delivery', AsyncMock(side_effect=ConnectionError()))
    elif outcome == 'state':
        monkeypatch.setattr(env.workflow, 'retry_delivery', AsyncMock(side_effect=ConnectionError()))
    observed = []

    def capture(message, *args):
        observed.append((message % args, log_context()))

    for level in ('info', 'warning', 'error'):
        monkeypatch.setattr(durable.logger, level, capture)
    with bind_log_context(user_id='trigger', request_uuid='request') as caller:
        assert await delivery_worker.drain_pending() == len(EVALUATE_REPORT_TYPES)
        assert log_context() == caller

    assert all(message.endswith(' callback_scope=report') for message, _ in observed)
    deliveries = [(message, context) for message, context in observed if '积压' not in message]
    assert len(deliveries) == len(EVALUATE_REPORT_TYPES)
    prefix = (
        '报告回调已确认:' if outcome == 204 else '报告回调状态待租约恢复:' if outcome == 'state' else '报告回调待重试:'
    )
    assert all(message.startswith(prefix) for message, _ in deliveries)
    for _, context in deliveries:
        job = env.deliveries.documents[context.task_id]
        assert (context.user_id, context.session_id) == (job['userId'], job['sessionId'])
        assert context.request_uuid is None
    assert [context for message, context in observed if '积压' in message] == [LogContext()]
    state = 'delivered' if outcome == 204 else 'sending' if outcome == 'state' else 'retry_wait'
    assert all(job['status'] == state for job in env.deliveries.documents.values())


@pytest.mark.asyncio
async def test_transport_uses_configured_timeout_without_following_redirects():
    received = []

    async def receive(request):
        received.append(request)
        return httpx.Response(302, headers={'Location': 'http://do-not-follow.test/'})

    transport = ReportCallbackTransport(transport=httpx.MockTransport(receive), timeout_seconds=45, concurrency=8)
    try:
        response = await transport.post_once('http://receiver.test/callback', {}, idempotency_key='rpt:type')
        assert response.status == 302
        assert len(received) == 1
        assert received[0].extensions['timeout'] == dict(connect=45, read=45, write=45, pool=45)
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_retarget_revokes_old_lease_preserves_success_and_records_intent():
    env = setup()
    batch = await complete(env)
    accepted = await env.workflow.claim_delivery('first')
    await env.workflow.acknowledge_delivery(accepted)
    in_flight = await env.workflow.claim_delivery('old-target')
    await env.workflow.redrive(user_id='u', final_report_id=batch['_id'],
                               target_url='http://replacement.test/callback',
                               operator='developer', reason='receiver moved')
    assert not await env.workflow.acknowledge_delivery(in_flight)
    assert env.deliveries.documents[accepted['_id']]['status'] == 'delivered'
    assert all(row['targetUrl'] == 'http://replacement.test/callback' for row in env.deliveries.documents.values()
               if row['status'] != 'delivered')
    assert env.batches.documents[batch['_id']]['deliveryOperations'][0]['reason'] == 'receiver moved'
    assert 'redrivePending' not in env.batches.documents[batch['_id']]


@pytest.mark.asyncio
async def test_historical_reports_only_redrive_explicitly_and_keep_original_json():
    env = setup()
    await env.batches.insert_one({'_id': 'old', 'finalReportId': 'old', 'userId': 'u', 'sessionId': 's'})
    await env.artifacts.insert_one({'_id': 'old:evaluate', 'finalReportId': 'old', 'reportType': 'evaluate',
                                    'userId': 'u', 'contentHash': 'original-hash', 'body': {'legacy': True}})
    await env.workflow.ensure_delivery('old', 'evaluate')
    await env.service.recover_pending()
    assert not env.deliveries.documents
    with pytest.raises(ValueError):
        await env.workflow.redrive(user_id='another-user', final_report_id='old',
                                   target_url='http://receiver.test/', operator='dev', reason='test')
    with pytest.raises(ValueError):
        await env.workflow.redrive(user_id='u', final_report_id='old', target_url=None, operator='dev', reason='test')
    await env.workflow.redrive(user_id='u', final_report_id='old', target_url='http://receiver.test/',
                               operator='dev', reason='recover legacy callback')
    delivery_worker, transport = worker(env)
    await delivery_worker.drain_pending()
    assert transport.post_once.call_args.args[1] == {'legacy': True}
    assert env.pipeline.calls == 0


@pytest.mark.asyncio
async def test_query_exposes_delivery_without_request_or_target():
    env = setup()
    batch = await complete(env)
    delivery_worker, _ = worker(env, response=CallbackResponse(503), batch_size=1)
    await delivery_worker.drain_pending()
    response = await env.archive.list_batches(user_id='u')
    assert response['items'][0]['generationStatus'] == 'success'
    assert 'request' not in response['items'][0] and 'callbackUrl' not in response['items'][0]
    summaries = response['items'][0]['artifacts']
    assert len(summaries) == 5
    assert any(row['delivery']['status'] == 'retry_wait' and row['delivery']['attempts'] == 1 for row in summaries)
    inaccessible = await env.archive.get_artifact(
        user_id='another', final_report_id=batch['_id'], report_type='evaluate',
    )
    assert inaccessible is None


@pytest.mark.asyncio
async def test_shutdown_does_not_claim_new_jobs_or_callbacks():
    env = setup()
    await complete(env)
    env.service.stop_accepting()
    assert await env.service.drain_generation() == 0
    assert await env.service.recover_pending() == 0
    delivery_worker, transport = worker(env)
    delivery_worker.stop_accepting()
    assert await delivery_worker.drain_pending() == 0
    transport.post_once.assert_not_called()


@pytest.mark.asyncio
async def test_interrupted_retarget_is_repaired_after_restart():
    env = setup()
    batch = await complete(env)
    env.deliveries.unavailable = True
    with pytest.raises(ConnectionError):
        await env.workflow.redrive(user_id='u', final_report_id=batch['_id'],
                                   target_url='http://fixed.test/callback', operator='dev', reason='fix config')
    assert env.batches.documents[batch['_id']]['redrivePending']['reason'] == 'fix config'
    env.deliveries.unavailable = False
    await env.service.recover_pending()
    assert all(row['targetUrl'] == 'http://fixed.test/callback' for row in env.deliveries.documents.values())
    assert 'redrivePending' not in env.batches.documents[batch['_id']]
    assert not env.batches.documents[batch['_id']]['reconcileNeeded']


@pytest.mark.asyncio
async def test_recovery_uses_real_pipeline_failed_schema_and_never_directly_calls_receiver():
    from modules.report.callback import _fixed_callback_body
    from modules.report.pipeline import ReportPipeline
    from modules.report.pipeline import _empty_partial_payload

    env = setup()
    callback = SimpleNamespace(build_body=_fixed_callback_body,
                               deliver=AsyncMock(side_effect=AssertionError('direct callback forbidden')))
    pipeline = ReportPipeline(source_client=object(), evidence_client=object(), callback_client=callback,
                               session_loader=AsyncMock(return_value={}), narrative_client=None,
                               artifact_store=env.archive)
    pipeline.generate = AsyncMock(return_value=_empty_partial_payload(REQUEST, reasons=['REPORT_GENERATION_TIMEOUT']))
    env.service.pipeline = pipeline
    batch = await complete(env)
    assert all(row['body']['status'] == 'failed' for row in env.artifacts.documents.values())
    assert set(env.artifacts.documents[f'{batch["_id"]}:evaluate']['body']) >= {'status', 'error', 'healthAssessment'}
    instruction = env.artifacts.documents[f'{batch["_id"]}:instruction']['body']
    assert set(instruction) >= {'status', 'error', 'non_drug_intervention', 'recommended_tests'}
    assert set(instruction['non_drug_intervention']) == {
        'lifestyle_intervention', 'gut_microbiota_regulation', 'supplements',
    }
    assert 'doctor_advice' not in instruction
    callback.deliver.assert_not_called()
    assert len(env.deliveries.documents) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('generation_status', ['success', 'partial_success'])
@pytest.mark.parametrize('source_fields', [
    {'diagnostic_basis': {'abnormal_findings': [{'abnormal_microbes': [{'name': 'source taxon'}]}]}},
    {'healthAssessment': {'abnormalIndicators': [{'name': 'source indicator', 'value': '1'}]}},
    # Normal results with source evidence are still facts, even without abnormalities.
    {'healthAssessment': {'dimensions': [{'score': 85, 'evidenceUsed': [{'name': 'normal result'}]}]}},
])
async def test_real_deferred_pipeline_preserves_facts_after_followup_gate_removal(source_fields, generation_status):
    from modules.report import reports
    from modules.report.pipeline import ReportPipeline

    # Reproduce the current production boundary without restoring the removed gate.
    assert not hasattr(reports, '_has_followup_source_facts')
    env = setup()
    body = {
        'type': 'evaluate', 'userId': 'u', 'sessionId': 's',
        'status': generation_status,
        'partial_reasons': ['DOCUMENT_SOURCE_INCOMPLETE'] if generation_status == 'partial_success' else [],
        'error': {'error_code': None, 'message': None}, **source_fields,
    }
    callback = SimpleNamespace(build_body=lambda value: dict(value), deliver=AsyncMock())
    followup = AsyncMock(return_value=SimpleNamespace(payloads=(), module_statuses={}))
    pipeline = ReportPipeline(
        source_client=object(), evidence_client=object(), callback_client=callback,
        session_loader=AsyncMock(return_value={}), narrative_client=None,
        artifact_store=env.archive, followup_report_handler=followup,
    )
    pipeline.generate = AsyncMock(return_value=body)
    env.service.pipeline = pipeline

    batch = await complete(env)
    artifact = env.artifacts.documents[f'{batch["_id"]}:evaluate']
    saved = artifact['body']
    assert saved['status'] == artifact['generationStatus'] == generation_status
    assert saved['partial_reasons'] == body['partial_reasons']
    assert saved['error'] == body['error']
    for key, value in source_fields.items():
        assert saved[key] == value
    pipeline.generate.assert_awaited_once()
    followup.assert_awaited_once()
    callback.deliver.assert_not_called()
    assert f'{batch["_id"]}:evaluate' in env.deliveries.documents
