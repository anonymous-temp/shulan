"""Top-level process runtime for Tornado, workers and graceful shutdown."""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass
from typing import Any

import tornado.httpserver
import tornado.ioloop

from bootstrap.container import AppContainer
from config.env import ENVIRONMENT
from config.env import REPORT_CALLBACK_POLL_INTERVAL_SECONDS
from config.env import SERVER_PORT
from runtime.resources import ResourceRegistry
from runtime.supervisor import TaskAdmissionClosedError
from utils.log import configure_tornado_logging
from utils.log import getLogger
from utils.log import shutdown_logging
from utils.log.context import LogContext
from utils.log.context import bind_log_context

logger = getLogger(__name__)
DRAIN_PROGRESS_LOG_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ApplicationBundle:
    """Typed ownership boundary for the web app and process resources."""

    web_application: Any
    container: AppContainer
    owns_agent_runtime: bool = False
    resources: ResourceRegistry | None = None

    def __post_init__(self) -> None:
        resources = self.resources
        if resources is None:
            resources = getattr(self.container, 'resources', None)
        if resources is None:
            resources = ResourceRegistry()
        object.__setattr__(self, 'resources', resources)
        if self.owns_agent_runtime and self.container.agent_runtime not in resources:
            resources.register_sync(
                self.container.agent_runtime,
                name='agent-runtime',
            )

    def close(self) -> None:
        """同步关闭全部托管资源；仅同步资源时使用。"""
        assert self.resources is not None
        self.resources.close()

    async def aclose(self) -> None:
        """异步关闭全部托管资源，包括同步资源。"""
        assert self.resources is not None
        await self.resources.aclose()


def run_application(bundle: ApplicationBundle) -> None:
    """Run HTTP and periodic workers until graceful shutdown completes."""
    configure_tornado_logging()
    app = bundle.web_application
    container = bundle.container
    http_server = tornado.httpserver.HTTPServer(app)
    io_loop = tornado.ioloop.IOLoop.current()

    def schedule_custom_message_outbox():
        return schedule_supervised_task(
            bundle,
            container.chat_service.drainPendingCustomMessages,
            name='worker:chat-custom-message-outbox',
        )

    def schedule_health_profile_ingestion():
        return schedule_supervised_task(
            bundle,
            container.health_profile_ingestion_worker.drain_pending,
            name='worker:health-profile-ingestion',
        )

    def schedule_qa_creation():
        return schedule_supervised_task(
            bundle,
            container.record_service.recoverPendingCreations,
            name='worker:qa-creation-recovery',
        )

    qa_creation = tornado.ioloop.PeriodicCallback(schedule_qa_creation, 15000)
    custom_outbox = tornado.ioloop.PeriodicCallback(
        schedule_custom_message_outbox,
        1000,
    )
    profile_ingestion = tornado.ioloop.PeriodicCallback(
        schedule_health_profile_ingestion,
        1000,
    )
    report_schedules = []
    report_callbacks = []
    for component, method, name, interval_ms in (
        (
            container.record_service
            if callable(getattr(container.record_service, 'recoverPendingReplies', None))
            else None,
            'recoverPendingReplies',
            'worker:qa-reply-recovery',
            15000,
        ),
        (getattr(container, 'questionnaire_service', None), 'recover_pending', 'worker:questionnaire-recovery', 15000),
        (
            getattr(container, 'questionnaire_service', None),
            'drain_factor_assessments',
            'worker:questionnaire-factor-assessment',
            5000,
        ),
        (
            getattr(container, 'questionnaire_service', None),
            'recover_factor_assessments',
            'worker:questionnaire-factor-recovery',
            15000,
        ),
        (
            getattr(container, 'report_followup_service', None),
            'drain_generation',
            'worker:report-followup-generation',
            5000,
        ),
        (
            getattr(container, 'report_followup_service', None),
            'recover_pending',
            'worker:report-followup-recovery',
            5000,
        ),
        (
            getattr(container, 'report_followup_callback_worker', None),
            'drain_pending',
            'worker:report-followup-callback',
            REPORT_CALLBACK_POLL_INTERVAL_SECONDS * 1000,
        ),
        (getattr(container, 'gut_barrier_service', None), 'drain_delegation', 'worker:gut-barrier-delegation', 5000),
        (container.report_service, 'drain_generation', 'worker:report-generation', 5000),
        (container.report_service, 'recover_pending', 'worker:report-recovery', 5000),
        (
            container.report_callback_worker,
            'drain_pending',
            'worker:report-callback',
            REPORT_CALLBACK_POLL_INTERVAL_SECONDS * 1000,
        ),
        (getattr(container, 'gut_barrier_service', None), 'drain_generation', 'worker:gut-barrier-generation', 5000),
        (getattr(container, 'gut_barrier_service', None), 'recover_pending', 'worker:gut-barrier-recovery', 5000),
        (
            getattr(container, 'gut_barrier_service', None),
            'drain_delivery',
            'worker:gut-barrier-delivery',
            REPORT_CALLBACK_POLL_INTERVAL_SECONDS * 1000,
        ),
    ):
        if component is not None:

            def schedule_report(component=component, method=method, name=name):
                return schedule_supervised_task(bundle, getattr(component, method), name=name)

            report_schedules.append(schedule_report)
            report_callbacks.append(tornado.ioloop.PeriodicCallback(schedule_report, interval_ms))
    for name, worker in getattr(container, 'source_workers', ()):

        def schedule_source(worker=worker, name=name):
            return schedule_supervised_task(bundle, worker.recover_pending, name='worker:' + name)

        report_schedules.append(schedule_source)
        report_callbacks.append(tornado.ioloop.PeriodicCallback(schedule_source, 5000))
    periodic_callbacks = (custom_outbox, profile_ingestion, qa_creation, *report_callbacks)
    # Transitional observability keys; process orchestration itself uses the
    # typed container above rather than these settings.
    app.settings['custom_message_outbox_worker'] = custom_outbox
    app.settings['health_profile_ingestion_periodic'] = profile_ingestion
    shutdown_started = False

    def begin_shutdown(signum: int) -> None:
        nonlocal shutdown_started
        signal_name = signal.Signals(signum).name
        if shutdown_started:
            logger.warning(
                '服务仍在排空，重复停止信号不会中断存量工作: signal=%s active_requests=%s active_tasks=%s',
                signal_name,
                container.lifecycle.active_request_count,
                container.task_supervisor.active_task_count,
            )
            return
        shutdown_started = True
        io_loop.spawn_callback(
            drain_application,
            bundle,
            http_server,
            periodic_callbacks,
            io_loop,
            signal_name,
        )

    def handle_signal(signum, _frame) -> None:
        io_loop.add_callback_from_signal(begin_shutdown, signum)

    previous_signal_handlers = {}
    try:
        for initialize in getattr(container, 'startup_initializers', ()):
            io_loop.run_sync(initialize)
        http_server.listen(SERVER_PORT)
        for callback in periodic_callbacks:
            callback.start()
        io_loop.add_callback(schedule_custom_message_outbox)
        io_loop.add_callback(schedule_health_profile_ingestion)
        io_loop.add_callback(schedule_qa_creation)
        for schedule in report_schedules:
            io_loop.add_callback(schedule)
        logger.info('Environment: %s', ENVIRONMENT)
        logger.info('服务已启动，监听端口: %s', SERVER_PORT)
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_signal_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, handle_signal)
        io_loop.start()
    finally:
        http_server.stop()
        for callback in periodic_callbacks:
            callback.stop()
        for signum, previous_handler in previous_signal_handlers.items():
            signal.signal(signum, previous_handler)
        assert bundle.resources is not None
        try:
            if not bundle.resources.closed:
                io_loop.run_sync(bundle.aclose)
        finally:
            shutdown_logging()


def schedule_supervised_task(
    bundle: ApplicationBundle,
    coroutine_factory,
    *,
    name: str,
):
    container = bundle.container
    if not container.lifecycle.is_ready:
        return None
    with bind_log_context(LogContext()):
        try:
            return container.task_supervisor.create_task(
                coroutine_factory(),
                name=name,
            )
        except TaskAdmissionClosedError:
            logger.info('应用正在排空，跳过周期任务调度: name=%s', name)
            return None


async def drain_application(
    application_or_bundle,
    http_server,
    periodic_callbacks,
    io_loop,
    signal_name: str,
) -> None:
    """Drain either the typed runtime or a legacy Application test double."""
    if isinstance(application_or_bundle, ApplicationBundle):
        bundle = application_or_bundle
        app = bundle.web_application
        container = bundle.container
        lifecycle = container.lifecycle
        task_supervisor = container.task_supervisor
        ingestion_worker = container.health_profile_ingestion_worker
    else:
        app = application_or_bundle
        lifecycle = app.settings['lifecycle']
        task_supervisor = app.settings['task_supervisor']
        ingestion_worker = app.settings.get('health_profile_ingestion_worker')
    if not lifecycle.begin_draining():
        return
    logger.info(
        '服务开始无损排空: signal=%s active_requests=%s active_tasks=%s',
        signal_name,
        lifecycle.active_request_count,
        task_supervisor.active_task_count,
    )
    http_server.stop()
    for callback in periodic_callbacks:
        callback.stop()
    stop_accepting = getattr(ingestion_worker, 'stop_accepting', None)
    if callable(stop_accepting):
        stop_accepting()
    if isinstance(application_or_bundle, ApplicationBundle):
        for _, worker in getattr(container, 'source_workers', ()):
            worker.stop_accepting()
        stop_replies = getattr(container.record_service, 'stopReplyRecovery', None)
        if callable(stop_replies):
            stop_replies()
        for component in (
            container.report_service,
            container.report_callback_worker,
            getattr(container, 'report_followup_service', None),
            getattr(container, 'report_followup_callback_worker', None),
            getattr(container, 'gut_barrier_service', None),
            getattr(getattr(container, 'questionnaire_service', None), 'factor_tasks', None),
        ):
            if component is not None:
                component.stop_accepting()
    await wait_with_drain_progress(
        lifecycle.wait_for_requests(),
        phase='requests',
        count_getter=lambda: lifecycle.active_request_count,
    )
    task_supervisor.stop_accepting()
    await wait_with_drain_progress(
        task_supervisor.wait_idle(),
        phase='tasks',
        count_getter=lambda: task_supervisor.active_task_count,
        names_getter=lambda: [
            f'{snapshot.name}:{snapshot.running_seconds:.1f}s' for snapshot in task_supervisor.snapshots()
        ],
    )
    await http_server.close_all_connections()
    lifecycle.mark_stopped()
    logger.info('服务存量请求与任务均已完成，进程可以安全退出')
    io_loop.stop()


async def wait_with_drain_progress(
    awaitable,
    *,
    phase: str,
    count_getter,
    names_getter=None,
) -> None:
    waiter = asyncio.create_task(awaitable, name=f'shutdown:wait-{phase}')
    while not waiter.done():
        done, _ = await asyncio.wait(
            {waiter},
            timeout=DRAIN_PROGRESS_LOG_SECONDS,
        )
        if done:
            break
        names = names_getter() if names_getter is not None else []
        logger.info(
            '服务仍在等待存量工作自然完成: phase=%s active_count=%s names=%s',
            phase,
            count_getter(),
            names,
        )
    await waiter


__all__ = [
    'ApplicationBundle',
    'drain_application',
    'run_application',
    'schedule_supervised_task',
    'wait_with_drain_progress',
]
