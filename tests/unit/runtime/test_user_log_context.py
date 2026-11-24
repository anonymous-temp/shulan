from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from utils.log.context import LogContext
from utils.log.context import bind_log_context
from utils.log.context import log_context


@pytest.mark.asyncio
async def test_supervised_failure_callback_has_original_user_and_task(monkeypatch):
    from runtime import supervisor
    observed = []
    monkeypatch.setattr(supervisor.logger, 'error', lambda *a: observed.append(log_context()))
    tasks = supervisor.TaskSupervisor()
    async def fail():
        assert log_context().task_id == 'background'
        raise RuntimeError('failure')
    with bind_log_context(user_id='u', request_uuid='request'):
        tasks.create_task(fail(), name='background')
    await tasks.wait_idle()
    assert observed == [LogContext(user_id='u', request_uuid='request', task_id='background')]


@pytest.mark.asyncio
async def test_periodic_scan_failure_callback_is_system_log(monkeypatch):
    from runtime import supervisor
    from runtime.application import schedule_supervised_task
    tasks = supervisor.TaskSupervisor()
    observed = []
    monkeypatch.setattr(supervisor.logger, 'error', lambda *a: observed.append(log_context()))
    async def fail():
        assert log_context().user_id is None
        raise RuntimeError('scan unavailable')
    bundle = SimpleNamespace(container=SimpleNamespace(
        task_supervisor=tasks, lifecycle=SimpleNamespace(is_ready=True),
    ))
    with bind_log_context(user_id='trigger', request_uuid='request'):
        schedule_supervised_task(bundle, fail, name='worker:scan')
    await tasks.wait_idle()
    assert observed == [LogContext(task_id='worker:scan')]


@pytest.mark.asyncio
async def test_periodic_scan_rejection_is_system_log_and_restores_caller(monkeypatch):
    from runtime import application
    from runtime import supervisor
    tasks = supervisor.TaskSupervisor()
    tasks.stop_accepting()
    observed = []
    monkeypatch.setattr(application.logger, 'info', lambda *a: observed.append(log_context()))
    scan = AsyncMock()
    bundle = SimpleNamespace(container=SimpleNamespace(
        task_supervisor=tasks, lifecycle=SimpleNamespace(is_ready=True),
    ))
    with bind_log_context(user_id='trigger', request_uuid='request') as caller:
        assert application.schedule_supervised_task(bundle, scan, name='worker:scan') is None
        assert log_context() == caller
    scan.assert_not_awaited()
    assert observed == [LogContext()]


@pytest.mark.asyncio
async def test_real_chat_outbox_failures_bind_each_user(monkeypatch):
    from modules.chat.application import service
    manager = service.ChatSessionManager.__new__(service.ChatSessionManager)
    manager._custom_message_outbox = SimpleNamespace(
        list_pending_sessions=AsyncMock(return_value=[('a', 'sa'), ('b', 'sb')]),
        list_pending=AsyncMock(side_effect=RuntimeError('unavailable')),
    )
    observed = []
    monkeypatch.setattr(service.logger, 'error', lambda *a: observed.append(log_context()))
    with bind_log_context(user_id='trigger', request_uuid='not-a-task-request'):
        assert await manager.drainPendingCustomMessages() == 0
    assert [item.user_id for item in observed] == ['a', 'b']
    assert all(item.request_uuid is None for item in observed)


@pytest.mark.asyncio
async def test_real_qa_recovery_failure_identity(monkeypatch):
    from contextlib import asynccontextmanager

    from modules.record.application import creation
    @asynccontextmanager
    async def lease(*args):
        raise RuntimeError('lease unavailable')
        yield
    coordinator = creation.QACreationCoordinator.__new__(creation.QACreationCoordinator)
    coordinator.manager = SimpleNamespace(_sessionLease=lease)
    observed = []
    monkeypatch.setattr(creation.logger, 'error', lambda *a: observed.append(log_context()))
    await coordinator._run('u', 's')
    assert observed == [LogContext(user_id='u', session_id='s', task_id='qa-creation')]



@pytest.mark.asyncio
async def test_profile_worker_binds_job_including_error_writeback(monkeypatch):
    from health_profile import worker
    instance = worker.HealthProfileIngestionWorker.__new__(worker.HealthProfileIngestionWorker)
    seen = []
    async def process(job):
        seen.append(log_context())
        raise RuntimeError('processor failed')
    instance._processor = SimpleNamespace(process=process)
    async def fail(*args):
        seen.append(log_context())
    instance._fail = fail
    monkeypatch.setattr(worker.logger, 'error', lambda *a: seen.append(log_context()))
    job = SimpleNamespace(user_id='u', session_id='s', job_id='job', lease_token='lease',
                          source_type=SimpleNamespace(value='chat'), source_id='message', attempts=1)
    await instance._process_claimed(job)
    assert seen == [LogContext(user_id='u', session_id='s', task_id='job')] * 3


@pytest.mark.asyncio
async def test_profile_redrive_conflict_is_attributed_to_job_owner(monkeypatch):
    from health_profile import maintenance
    from health_profile.models import ProfileJobStatus
    service = maintenance.HealthProfileJobMaintenanceService.__new__(
        maintenance.HealthProfileJobMaintenanceService,
    )
    service._repository = SimpleNamespace(get_job=AsyncMock(return_value=SimpleNamespace(
        user_id='job-owner', job_id='job-1', status=ProfileJobStatus.COMPLETED,
    )))
    observed = []
    monkeypatch.setattr(maintenance.logger, 'warning', lambda *a: observed.append(log_context()))
    with bind_log_context(user_id='operator', request_uuid='operator-request') as caller:
        with pytest.raises(maintenance.HealthProfileJobRedriveConflictError):
            await service.redrive(job_id='job-1', expected_status=ProfileJobStatus.DEAD_LETTER,
                                  reason_code='fix', actor_id='operator')
        assert log_context() == caller
    assert observed == [LogContext(user_id='job-owner', task_id='job-1')]


@pytest.mark.asyncio
async def test_profile_cli_list_separates_job_records_and_scan_summary(monkeypatch):
    from health_profile import maintenance_cli
    from tests.unit.health_profile.test_profile_maintenance_cli import FakeMaintenanceService
    service = FakeMaintenanceService()
    observed = []
    monkeypatch.setattr(maintenance_cli.logger, 'info', lambda *a: observed.append(log_context()))
    args = maintenance_cli.build_parser().parse_args(['list'])
    with bind_log_context(user_id='trigger', request_uuid='request') as caller:
        assert await maintenance_cli.run_command(args, service=service) == 0
        assert log_context() == caller
    assert observed == [LogContext(user_id='profile-user-1', task_id='profile-job-1'), LogContext()]
