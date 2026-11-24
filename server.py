"""Compatibility facade for the modular Tornado application.

New code should import from ``web``, ``bootstrap``, ``runtime`` or a business
module.  This module preserves the historical startup and test import surface
while those callers migrate.
"""

from __future__ import annotations

import asyncio

import tornado.web

import web.handlers.chat as _chat_handlers
import web.handlers.health_profile as _profile_handlers
import web.handlers.record as _record_handlers
from agents import AgentRuntime
from agents.runtime.application import create_application_agent_runtime
from bootstrap.factory import _create_health_profile_memory_provider
from bootstrap.factory import build_application_bundle
from bootstrap.factory import create_application_document_trace_store
from bootstrap.factory import create_application_health_report_interpreter
from config.env import DEBUG
from modules.analysis.application import HealthReportProcessingService
from modules.chat.application import ChatSessionManager
from modules.record.application import QASessionManager
from runtime import ApplicationLifecycle
from runtime import TaskSupervisor
from runtime.application import ApplicationBundle
from runtime.application import drain_application
from runtime.application import run_application
from runtime.application import schedule_supervised_task
from runtime.application import wait_with_drain_progress
from utils.log import getLogger
from web.handlers import *  # noqa: F403 - deliberate compatibility export.
from web.handlers.analysis import HealthReportHandler as _WebHealthReportHandler
from web.handlers.base import BaseHandler as _WebBaseHandler
from web.handlers.base import log_request_exception as _log_request_exception
from web.http_logging import log_access as _log_access

logger = getLogger(__name__)

# Historical contract tests and callers imported application errors from
# ``server``.  Keep these as explicit aliases to the classes used by the new
# handlers until those callers move to their owning modules.
ChatCustomMessageConflictError = _chat_handlers.ChatCustomMessageConflictError
ChatCustomMessageValidationError = _chat_handlers.ChatCustomMessageValidationError
ChatSessionNotFoundError = _chat_handlers.ChatSessionNotFoundError
HealthProfileConflictError = _profile_handlers.HealthProfileConflictError
HealthProfileNotFoundError = _profile_handlers.HealthProfileNotFoundError
HealthProfileValidationError = _profile_handlers.HealthProfileValidationError


def log_tornado_request(handler: tornado.web.RequestHandler) -> None:
    """Compatibility hook whose logger remains patchable via ``server``."""
    _log_access(handler, request_logger=logger)


def _compat_log_exception(self, typ, value, tb) -> None:
    _log_request_exception(self, value, tb, request_logger=logger)


BaseHandler = type(
    'BaseHandler',
    (_WebBaseHandler,),
    {'log_exception': _compat_log_exception, '__module__': __name__},
)


def _compat_analysis_initialize(
    self,
    processing_service_factory=None,
    processing_service=None,
    interpreter=None,
    report_source=None,
    health_profile_service=None,
    document_trace_store=None,
) -> None:
    if processing_service_factory is None and processing_service is None:
        interpreter = interpreter or create_application_health_report_interpreter(
            self.application.settings['agent_runtime'],
        )
        report_source = report_source or self.application.settings['health_report_source']
        health_profile_service = health_profile_service or self.application.settings['health_profile_service']
        processing_service = HealthReportProcessingService(
            interpreter=interpreter,
            report_source=report_source,
            health_profile_service=health_profile_service,
            document_trace_store=(
                document_trace_store if document_trace_store is not None else create_application_document_trace_store()
            ),
        )
    _WebHealthReportHandler.initialize(
        self,
        processing_service_factory=processing_service_factory,
        processing_service=processing_service,
    )


HealthReportHandler = type(
    'HealthReportHandler',
    (_WebHealthReportHandler,),
    {'initialize': _compat_analysis_initialize, '__module__': __name__},
)


def _legacy_service_handler(handler_type, setting_name: str):
    def initialize(self, service=None) -> None:
        handler_type.initialize(
            self,
            service or self.application.settings[setting_name],
        )

    return type(
        handler_type.__name__,
        (handler_type,),
        {'initialize': initialize, '__module__': __name__},
    )


for _name in (
    'QASessionCreateHandler',
    'QASessionAnswerHandler',
    'QASessionContinueHandler',
    'QASessionCompleteHandler',
    'QASessionAnswerStreamHandler',
    'QASessionGetHandler',
    'QASessionHistoryHandler',
):
    globals()[_name] = _legacy_service_handler(
        getattr(_record_handlers, _name),
        'qa_manager',
    )

for _name in (
    'ChatSendCompleteHandler',
    'ChatCustomMessageHandler',
    'ChatSendStreamHandler',
    'ChatHistoryHandler',
    'ChatSuggestionsHandler',
):
    globals()[_name] = _legacy_service_handler(
        getattr(_chat_handlers, _name),
        'chat_manager',
    )

for _name in (
    'HealthProfileGetHandler',
    'HealthProfileSubjectListHandler',
    'HealthProfileSubjectUpsertHandler',
    'HealthProfileFactListHandler',
    'HealthProfileFactResolveHandler',
    'HealthProfileFactDeleteHandler',
    'HealthProfileChangeListHandler',
):
    globals()[_name] = _legacy_service_handler(
        getattr(_profile_handlers, _name),
        'health_profile_service',
    )


def make_app(*, agent_runtime: AgentRuntime | None = None):
    """Create the historical Tornado Application return type."""
    owns_agent_runtime = agent_runtime is None
    if agent_runtime is None:
        agent_runtime = create_application_agent_runtime()
    return _make_app_with_agent_runtime(
        agent_runtime,
        owns_agent_runtime=owns_agent_runtime,
    )


def _make_app_with_agent_runtime(
    agent_runtime: AgentRuntime,
    *,
    health_profile_service=None,
    health_report_source=None,
    record_profile_source=None,
    chat_profile_source=None,
    health_profile_memory_provider=None,
    health_profile_ingestion_worker=None,
    health_report_processing_service_factory=None,
    lifecycle: ApplicationLifecycle | None = None,
    task_supervisor: TaskSupervisor | None = None,
    owns_agent_runtime: bool = False,
):
    """Compatibility composition wrapper around the new typed container."""
    bundle = build_application_bundle(
        agent_runtime,
        debug=DEBUG,
        owns_agent_runtime=owns_agent_runtime,
        health_profile_service=health_profile_service,
        health_report_source=health_report_source,
        record_profile_source=record_profile_source,
        chat_profile_source=chat_profile_source,
        health_profile_memory_provider=health_profile_memory_provider,
        health_profile_ingestion_worker=health_profile_ingestion_worker,
        health_report_processing_service_factory=(health_report_processing_service_factory),
        lifecycle=lifecycle,
        task_supervisor=task_supervisor,
        qa_manager_type=QASessionManager,
        chat_manager_type=ChatSessionManager,
    )
    application = bundle.web_application
    application.settings['log_function'] = log_tornado_request
    return application


def _create_health_report_processing_service(
    agent_runtime: AgentRuntime,
    *,
    report_source,
    health_profile_service,
    document_trace_store=None,
):
    return HealthReportProcessingService(
        interpreter=create_application_health_report_interpreter(agent_runtime),
        report_source=report_source,
        health_profile_service=health_profile_service,
        document_trace_store=(
            document_trace_store if document_trace_store is not None else create_application_document_trace_store()
        ),
    )


def _create_health_profile_chat_memory_provider():
    return _create_health_profile_memory_provider()


def run() -> None:
    """Historical process entry backed by ``ApplicationBundle``."""
    app = make_app()
    bundle = app.settings['application_bundle']
    try:
        run_application(bundle)
    finally:
        if not bundle.resources.closed:
            asyncio.run(bundle.aclose())


def _run_application(app) -> None:
    bundle = app.settings.get('application_bundle')
    if bundle is None:
        container = app.settings['app_container']
        bundle = ApplicationBundle(
            web_application=app,
            container=container,
            owns_agent_runtime=False,
            resources=container.resources,
        )
    run_application(bundle)


def _schedule_supervised_task(app, coroutine_factory, *, name: str):
    bundle = app.settings.get('application_bundle')
    if bundle is None:
        container = app.settings['app_container']
        bundle = ApplicationBundle(
            web_application=app,
            container=container,
            owns_agent_runtime=False,
            resources=container.resources,
        )
    return schedule_supervised_task(bundle, coroutine_factory, name=name)


_drain_application = drain_application
_wait_with_drain_progress = wait_with_drain_progress


if __name__ == '__main__':
    run()
