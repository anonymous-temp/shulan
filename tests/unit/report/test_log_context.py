"""Offline report log ownership across generation, recovery and delivery."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.report.artifacts import EVALUATE_REPORT_TYPES
from utils.log.context import LogContext
from utils.log.context import bind_log_context
from utils.log.context import log_context


@pytest.mark.asyncio
async def test_report_generation_and_callbacks_do_not_inherit_trigger_user():
    from modules.report.schemas import ReportRequest
    from tests.unit.report.test_durable_reports import setup
    from tests.unit.report.test_durable_reports import worker
    env = setup()
    seen = []
    original = env.pipeline.execute_and_callback
    async def generate(request, **kwargs):
        assert await asyncio.to_thread(log_context) == log_context()
        seen.append(log_context())
        return await original(request, **kwargs)
    env.pipeline.execute_and_callback = generate
    for user in ('a', 'b'):
        await env.service.submit(ReportRequest(user_id=user, session_id=f's-{user}'))
    with bind_log_context(user_id='trigger', request_uuid='trigger-request'):
        await env.service.drain_generation()
        delivery, transport = worker(env)
        response = transport.post_once.return_value
        async def send(*args, **kwargs):
            seen.append(log_context())
            return response
        transport.post_once.side_effect = send
        await delivery.drain_pending()
    assert {context.user_id for context in seen} == {'a', 'b'}
    assert len(seen) == 2 * (1 + len(EVALUATE_REPORT_TYPES))
    assert all(context.request_uuid is None and context.task_id for context in seen)


@pytest.mark.asyncio
async def test_generation_and_callback_failures_keep_job_owner(monkeypatch):
    from modules.report import durable
    from modules.report.schemas import ReportRequest
    from tests.unit.report.test_durable_reports import setup
    from tests.unit.report.test_durable_reports import worker
    env = setup(fail_after=0)
    observed = []
    monkeypatch.setattr(durable.logger, 'error', lambda *a: observed.append(log_context()))
    monkeypatch.setattr(durable.logger, 'warning', lambda *a: observed.append(log_context()))
    batches = [await env.service.submit(ReportRequest(user_id=u, session_id=f's-{u}')) for u in ('a', 'b')]
    with bind_log_context(user_id='trigger', request_uuid='request') as caller:
        await env.service.drain_generation()
        delivery, transport = worker(env)
        transport.post_once.side_effect = ConnectionError('offline failure')
        await delivery.drain_pending()
        assert log_context() == caller
    expected = {batch['_id']: (batch['userId'], batch['sessionId']) for batch in batches}
    expected.update({job['_id']: (job['userId'], job['sessionId']) for job in env.deliveries.documents.values()})
    assert len(observed) == len(expected)
    for context in observed:
        assert (context.user_id, context.session_id) == expected[context.task_id]
        assert context.request_uuid is None
    assert all(job['status'] == 'retry_wait' for job in env.deliveries.documents.values())


@pytest.mark.asyncio
async def test_recovery_and_reconciliation_bind_persisted_identity(monkeypatch):
    from modules.report.schemas import ReportRequest
    from tests.unit.report.test_durable_reports import setup
    env = setup()
    batch = await env.service.submit(ReportRequest(user_id='recovered', session_id='session'))
    await env.workflow.claim_generation('old-process')
    env.clock.advance(121)
    observed = []
    original = env.workflow.finalize_generation
    async def finalize(*args):
        observed.append(log_context())
        return await original(*args)
    monkeypatch.setattr(env.workflow, 'finalize_generation', finalize)
    with bind_log_context(user_id='trigger', request_uuid='request'):
        assert await env.service.recover_pending() == 1
    assert observed == [LogContext(user_id='recovered', session_id='session', task_id=batch['_id'])]
    assert env.pipeline.calls == 0
    observed.clear()
    jobs = [{'_id': u, 'userId': u, 'sessionId': f's-{u}', 'generationState': 'completed'} for u in ('a', 'b')]
    monkeypatch.setattr(env.workflow, 'reconciliation_jobs', AsyncMock(return_value=jobs))
    async def redrive(job):
        observed.append(log_context())
    monkeypatch.setattr(env.workflow, 'apply_redrive', redrive)
    with bind_log_context(user_id='trigger', request_uuid='request'):
        await env.service.recover_pending()
    assert observed == [LogContext(user_id=u, session_id=f's-{u}', task_id=u) for u in ('a', 'b')]


@pytest.mark.asyncio
async def test_report_profile_compensation_binds_each_source_owner(monkeypatch):
    from health_profile import report_source
    from tests.unit.health_profile.test_health_report_source import HealthReportSourceTest
    fixture = HealthReportSourceTest()
    fixture.setUp()
    fixture.job_repository.fail_create = True
    observed = []
    monkeypatch.setattr(report_source.logger, 'error', lambda *a: observed.append(log_context()))
    for user in ('a', 'b'):
        await fixture.service.save(report_id=f'report-{user}', user_id=user, subject_id='self',
                                   source_fingerprint=user, result=fixture.result)
    observed.clear()
    with bind_log_context(user_id='trigger', request_uuid='request') as caller:
        assert await fixture.service.recover_pending() == 0
        assert log_context() == caller
    assert observed == [LogContext(user_id=u, task_id='report-profile-delivery') for u in ('a', 'b')]


@pytest.mark.asyncio
async def test_callback_wake_failure_callback_is_system_owned(monkeypatch):
    from agents import AgentHub
    from agents import GraphRegistry
    from bootstrap.factory import build_container
    from runtime import supervisor
    from tests.unit.bootstrap.test_factory import _dependencies
    from tests.unit.bootstrap.test_factory import _RecordingManager
    from tests.unit.report.test_durable_reports import setup
    env = setup()
    callback = SimpleNamespace(drain_pending=AsyncMock(side_effect=RuntimeError('scan failed')))
    container = build_container(
        SimpleNamespace(hub=AgentHub(GraphRegistry())), **_dependencies(),
        report_pipeline=env.pipeline, report_artifact_store=env.archive, report_callback_worker=callback,
        qa_manager_type=_RecordingManager, chat_manager_type=_RecordingManager,
    )
    observed = []
    monkeypatch.setattr(supervisor.logger, 'error', lambda *a: observed.append(log_context()))
    try:
        with bind_log_context(user_id='trigger', request_uuid='request') as caller:
            env.workflow.on_delivery_ready()
            assert log_context() == caller
        await container.task_supervisor.wait_idle()
        assert observed == [LogContext(task_id='worker:report-callback-wake')]
    finally:
        await container.resources.aclose()


@pytest.mark.asyncio
async def test_recovery_without_saved_user_never_inherits_caller(monkeypatch):
    from tests.unit.report.test_durable_reports import setup

    env = setup()
    observed = []

    async def finalize(*args):
        observed.append(log_context())
        return False

    monkeypatch.setattr(env.workflow, "finalize_generation", finalize)
    with bind_log_context(user_id="trigger", session_id="wrong", request_uuid="request") as caller:
        await env.service._execute({"_id": "historical-batch", "leaseToken": "lease"}, recovery=True)
        assert log_context() == caller
    assert observed == [LogContext(task_id="historical-batch")]
