"""Typed dependencies shared by web routing and the process runtime."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from agents import AgentRuntime
from runtime import ApplicationLifecycle
from runtime import ResourceRegistry
from runtime import TaskSupervisor


@dataclass(frozen=True, slots=True)
class AppContainer:
    """Fully constructed application dependencies.

    Concrete adapters are intentionally created outside this value, in the
    composition root.  Handlers receive only the application services they
    need through ``web.routes``.
    """

    agent_runtime: AgentRuntime
    analysis_service_factory: Any
    record_service: Any
    chat_service: Any
    health_profile_service: Any
    health_report_source: Any
    record_profile_source: Any
    chat_profile_source: Any
    health_profile_memory_provider: Any
    health_profile_ingestion_worker: Any
    report_pipeline: Any
    report_task_coordinator: Any
    lifecycle: ApplicationLifecycle
    task_supervisor: TaskSupervisor
    # 未提供时 Report 查询接口不可用；生产路径由组合根注入。
    report_artifact_store: Any = None
    report_service: Any = None
    report_callback_worker: Any = None
    report_followup_service: Any = None
    report_followup_callback_worker: Any = None
    # Independent domain and collection; never joins the five-report workflow.
    gut_barrier_service: Any = None
    questionnaire_service: Any = None
    health_analysis_service: Any = None
    resources: ResourceRegistry = field(default_factory=ResourceRegistry)
    record_profile_memory_provider: Any = None
    chat_profile_memory_provider: Any = None
    source_workers: tuple = ()
    startup_initializers: tuple[Callable[[], Awaitable[None]], ...] = ()

    def compatibility_settings(self) -> dict[str, Any]:
        """Settings retained for legacy adapters and downstream tests.

        Business handlers use explicit route injection.  Settings remain only
        for lifecycle and historical imports that have not yet been retired.
        """
        return {
            'agent_runtime': self.agent_runtime,
            'qa_manager': self.record_service,
            'chat_manager': self.chat_service,
            'health_profile_service': self.health_profile_service,
            'health_report_source': self.health_report_source,
            'record_profile_source': self.record_profile_source,
            'chat_profile_source': self.chat_profile_source,
            'health_profile_memory_provider': self.health_profile_memory_provider,
            'record_profile_memory_provider': self.record_profile_memory_provider,
            'chat_profile_memory_provider': self.chat_profile_memory_provider,
            'health_profile_ingestion_worker': self.health_profile_ingestion_worker,
            'lifecycle': self.lifecycle,
            'task_manager': self.task_supervisor,
            'task_supervisor': self.task_supervisor,
        }
