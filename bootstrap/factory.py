"""Build the concrete dependency graph exactly once at application startup."""

from __future__ import annotations

import asyncio
from typing import Any
from typing import Callable

from adapters.analysis import create_application_document_parsing_client
from adapters.analysis.document_download import HttpDocumentDownloader
from adapters.analysis.document_index import create_application_document_index_store
from adapters.analysis.document_index import create_application_document_output_index_store
from adapters.analysis.document_trace import create_application_document_trace_store
from adapters.analysis.image_probe import HttpImageFormatProbe
from adapters.analysis.tokenizer import count_report_input_tokens
from adapters.messaging import create_application_chat_custom_message_outbox
from adapters.persistence import create_application_chat_session_repository
from adapters.persistence import create_application_record_session_cache
from adapters.persistence import create_application_record_session_repository
from adapters.persistence.record_creation import create_application_record_creation_store
from adapters.persistence.record_creation import create_application_record_reply_recovery_store
from adapters.record import create_application_record_prompt_renderer
from adapters.record.completion import create_application_record_completion_store
from adapters.record.documents import LegacyRecordDocumentAdapter
from adapters.report import build_report_pipeline
from adapters.report import create_application_report_artifact_store
from adapters.report.agent_context_selector import AgentReportContextSelector
from adapters.report.chat_context import ReportChatContextProvider
from agents import AgentRuntime
from bootstrap.container import AppContainer
from modules.analysis.application import HealthReportInterpreter
from modules.analysis.application import HealthReportProcessingService
from modules.chat.application import ChatSessionManager
from modules.record.application import QASessionManager
from modules.report.task_coordinator import ReportTaskCoordinator
from runtime import ApplicationLifecycle
from runtime import LifecycleState
from runtime import ResourceRegistry
from runtime import TaskSupervisor
from utils.log import getLogger
from utils.log.context import LogContext
from utils.log.context import bind_log_context

logger = getLogger(__name__)


def build_container(
    agent_runtime: AgentRuntime,
    *,
    health_profile_service: Any = None,
    profile_snapshot_reader: Any = None,
    health_report_source: Any = None,
    record_profile_source: Any = None,
    chat_profile_source: Any = None,
    health_profile_memory_provider: Any = None,
    record_profile_memory_provider: Any = None,
    chat_profile_memory_provider: Any = None,
    document_observation_reader: Any = None,
    document_archive_reader: Any = None,
    health_profile_ingestion_worker: Any = None,
    document_parsing_client: Any = None,
    document_trace_store: Any = None,
    document_index: Any = None,
    document_downloader: Any = None,
    document_output_index: Any = None,
    document_output_profiles: Any = None,
    image_format_probe: Any = None,
    record_session_repository: Any = None,
    record_session_cache: Any = None,
    record_creation_store: Any = None,
    record_reply_recovery_store: Any = None,
    record_completion_store: Any = None,
    record_prompt_renderer: Any = None,
    record_document_port: Any = None,
    document_compatibility_service_factory: Callable[[], Any] | None = None,
    chat_session_repository: Any = None,
    chat_custom_message_outbox: Any = None,
    health_report_processing_service_factory: Callable[[], Any] | None = None,
    lifecycle: ApplicationLifecycle | None = None,
    task_supervisor: TaskSupervisor | None = None,
    resources: ResourceRegistry | None = None,
    report_pipeline: Any = None,
    report_document_source_factory: Any = None,
    report_task_coordinator: ReportTaskCoordinator | None = None,
    report_artifact_store: Any = None,
    report_service: Any = None,
    report_callback_worker: Any = None,
    report_followup_service: Any = None,
    report_followup_callback_worker: Any = None,
    gut_barrier_service: Any = None,
    questionnaire_service: Any = None,
    health_analysis_service: Any = None,
    health_analysis_basic_info_sync: Any = None,
    health_analysis_profile_reader: Any = None,
    health_analysis_chat_reader: Any = None,
    health_analysis_patient_sender_reader: Any = None,
    health_analysis_trace_writer: Any = None,
    qa_manager_type=QASessionManager,
    chat_manager_type=ChatSessionManager,
) -> AppContainer:
    """Create services/adapters and return their typed application boundary."""
    resources = resources if resources is not None else ResourceRegistry()
    task_supervisor = task_supervisor or TaskSupervisor()
    default_health_profile = health_profile_service is None
    health_profile_service = (
        health_profile_service if health_profile_service is not None else _create_health_profile_service(agent_runtime)
    )
    health_report_source = health_report_source if health_report_source is not None else _create_health_report_source()
    document_parsing_client = (
        document_parsing_client if document_parsing_client is not None else create_application_document_parsing_client()
    )
    default_document_processing = health_report_processing_service_factory is None
    # Production defaults install all three consumers on the shared current Document graph.
    default_document_sources = (
        default_document_processing
        and isinstance(agent_runtime, AgentRuntime)
        and document_compatibility_service_factory is None
        and record_document_port is None
        and report_document_source_factory is None
        and document_output_profiles is None
        and health_profile_ingestion_worker is None
    )
    document_sources = None
    questionnaire_source_delivery = None
    record_sources = None
    chat_sources = None
    source_workers = ()
    startup_initializers = ()
    publication_ledger = None
    if default_document_processing and isinstance(agent_runtime, AgentRuntime):
        from bootstrap.document_sources import create_application_document_source_resources

        document_resources = create_application_document_source_resources()
        publication_ledger = document_resources.ledger
    if health_report_processing_service_factory is None:
        image_format_probe = image_format_probe if image_format_probe is not None else HttpImageFormatProbe()
        document_trace_store = (
            document_trace_store if document_trace_store is not None else create_application_document_trace_store()
        )
        document_index = document_index if document_index is not None else create_application_document_index_store()
        document_downloader = document_downloader if document_downloader is not None else HttpDocumentDownloader()
        document_output_index = (
            document_output_index
            if document_output_index is not None
            else create_application_document_output_index_store()
        )

        from agents.graphs.analysis.current import ANALYSIS_CURRENT_CAPABILITY

        current_analysis = default_document_sources or (
            isinstance(agent_runtime, AgentRuntime)
            and (
                'analysis' in agent_runtime.module_statuses
                or ANALYSIS_CURRENT_CAPABILITY in agent_runtime.registry.capabilities()
            )
        )

        def health_report_processing_service_factory():
            interpreter = create_application_health_report_interpreter(
                agent_runtime,
                document_parsing_client=document_parsing_client,
                current=current_analysis,
            )
            return HealthReportProcessingService(
                interpreter=interpreter,
                **({'current_interpreter': interpreter} if current_analysis else {}),
                report_source=health_report_source,
                health_profile_service=health_profile_service,
                document_trace_store=document_trace_store,
                document_index=document_index,
                document_downloader=document_downloader,
                document_output_index=document_output_index,
                output_profiles=document_output_profiles,
                **({'publication_ledger': publication_ledger} if publication_ledger is not None else {}),
                document_task_spawner=lambda coroutine: task_supervisor.create_task(
                    coroutine, name='document-processing'
                ),
                image_format_probe=image_format_probe,
            )

    analysis_service_factory = health_report_processing_service_factory
    if default_document_sources:
        from bootstrap.document_sources import build_current_document_consumers
        from bootstrap.document_sources import build_current_document_source_graph
        from bootstrap.document_sources import durable_collection
        from modules.health_profile.application import create_application_profile_public_contracts
        from modules.health_profile.application import create_application_profile_source_admission
        from runtime.source_recovery import DocumentOutboxRecovery
        from runtime.source_recovery import SourceRecoveryRunner

        source_resolvers, owner_fence_readers = {}, {}
        if questionnaire_service is None:
            from bootstrap.questionnaire_sources import build_questionnaire_source_resources
            from db.mongodb import collection_questionnaires
            from modules.health_profile.application import ProfileUserSourceResolver

            questionnaire_sources = build_questionnaire_source_resources(
                questionnaire_collection=collection_questionnaires,
            )
            source_resolvers[('questionnaire', 'profile.questionnaire-source.v2')] = ProfileUserSourceResolver(
                questionnaire_sources.snapshot_reader,
            )
            owner_fence_readers['questionnaire'] = questionnaire_sources.fence_reader
        if record_profile_source is None and record_completion_store is None:
            from bootstrap.record_sources import create_application_record_source_resources
            from modules.health_profile.application import ProfileUserSourceResolver

            record_session_cache = (
                record_session_cache if record_session_cache is not None else create_application_record_session_cache()
            )
            record_sources = create_application_record_source_resources(
                session_cache=record_session_cache, lease_manager=agent_runtime.lease_manager
            )
            source_resolvers[('record', 'profile.record-source.v2')] = ProfileUserSourceResolver(
                record_sources.material_reader
            )
            owner_fence_readers['record'] = record_sources.owner
        if chat_profile_source is None and chat_session_repository is None:
            from bootstrap.chat_sources import create_application_chat_source_resources
            from modules.health_profile.application import ProfileUserSourceResolver

            chat_sources = create_application_chat_source_resources()
            source_resolvers[('chat', 'profile.chat-source.v2')] = ProfileUserSourceResolver(
                chat_sources.snapshot_reader
            )
            owner_fence_readers['chat'] = chat_sources.fence_reader
        document_sources = build_current_document_source_graph(
            resources=document_resources,
            processing=health_report_processing_service_factory(),
            profile_service=health_profile_service,
            agent_runtime=agent_runtime,
            profile_contracts_factory=create_application_profile_public_contracts,
            profile_admission_factory=create_application_profile_source_admission,
            source_resolvers=source_resolvers,
            owner_fence_readers=owner_fence_readers,
        )
        if questionnaire_service is None:
            from bootstrap.questionnaire_sources import build_questionnaire_source_delivery
            from modules.questionnaire.application import create_application_questionnaire_submitted_material_reader

            questionnaire_source_delivery = build_questionnaire_source_delivery(
                questionnaire_sources,
                inbox=document_sources.profile.inbox,
                presence=document_sources.admission.presence,
                fresh_admission=document_sources.admission.admission,
                submitted_reader=create_application_questionnaire_submitted_material_reader(),
                health_report_source=health_report_source,
                publication_ledger=document_resources.ledger,
            )
        workflow = getattr(report_artifact_store, 'workflow', None)
        report_batches = getattr(workflow, 'batches', None)
        if report_batches is None:
            from db.mongodb import collection_report_batches

            report_batches = durable_collection(collection_report_batches)
        consumers = build_current_document_consumers(graph=document_sources, report_batches=report_batches)
        record_document_port, report_document_source_factory = consumers.record, consumers.report

        def analysis_service_factory():
            return consumers.analysis.service

        if profile_snapshot_reader is None:
            profile_snapshot_reader = document_sources.profile.reader
        source_workers = tuple(
            (name, SourceRecoveryRunner(recover))
            for name, recover in (
                ('document-outbox', DocumentOutboxRecovery(document_sources.current.recovery).recover_pending),
                ('analysis-delivery', consumers.analysis.delivery.recover_pending),
            )
        )
        if questionnaire_source_delivery is not None:
            source_workers += (
                ('questionnaire-source', SourceRecoveryRunner(questionnaire_source_delivery.recover_pending)),
            )

    from modules.health_profile.application import HealthProfileService

    if isinstance(health_profile_service, HealthProfileService) and (
        document_archive_reader is not None or default_health_profile and document_sources is not None
    ):
        if document_archive_reader is None:
            from adapters.analysis.archived_extractions import create_document_archived_extraction_reader
            from adapters.profile_document_archives import ProfileDocumentArchiveAdapter

            document_archive_reader = ProfileDocumentArchiveAdapter(
                create_document_archived_extraction_reader(
                    processing=document_sources.processing,
                    current_source=document_sources.current.source,
                    lifecycle=document_sources.publication.lifecycle,
                )
            )
        health_profile_service.bind_document_archive_reader(
            document_archive_reader,
            require_profile_ownership=document_sources is not None,
            publications=document_sources.profile.inbox if document_sources is not None else None,
        )

    record_profile_source = (
        record_profile_source if record_profile_source is not None else _create_record_profile_source()
    )
    chat_profile_source = chat_profile_source if chat_profile_source is not None else _create_chat_profile_source()
    if chat_sources is not None:
        from bootstrap.chat_sources import build_chat_source_runtime
        from runtime.source_recovery import SourceRecoveryRunner

        chat_source_runtime = build_chat_source_runtime(
            resources=chat_sources,
            legacy=chat_profile_source,
            inbox=document_sources.profile.inbox,
            presence=document_sources.admission.presence,
            fresh_admission=document_sources.admission.admission,
        )
        chat_profile_source = chat_source_runtime.router
        source_workers += (('chat-source', SourceRecoveryRunner(chat_source_runtime.worker.recover_pending)),)
    # Explicit historical injection retains its existing two-domain behavior.
    # Default construction gives each domain its own renderer and public reads.
    record_profile_memory_provider = (
        record_profile_memory_provider if record_profile_memory_provider is not None else health_profile_memory_provider
    )
    chat_profile_memory_provider = (
        chat_profile_memory_provider if chat_profile_memory_provider is not None else health_profile_memory_provider
    )
    if record_profile_memory_provider is None or chat_profile_memory_provider is None:
        if profile_snapshot_reader is None or (document_sources is None and document_observation_reader is None):
            from modules.health_profile.application import create_application_profile_public_contracts

            profile_contracts = create_application_profile_public_contracts()
            if profile_snapshot_reader is None:
                profile_snapshot_reader = profile_contracts.reader
            if document_sources is None and document_observation_reader is None:
                from bootstrap.document_contracts import create_application_document_observation_reader

                document_observation_reader = create_application_document_observation_reader(
                    profile_legacy_ingestion_reader=profile_contracts.legacy_ingestion
                )
        if record_profile_memory_provider is None:
            from adapters.record.profile_context import create_record_profile_memory

            record_profile_memory_provider = create_record_profile_memory(
                profile_snapshot_reader, document_observations=document_observation_reader
            )
        if chat_profile_memory_provider is None:
            from adapters.chat.profile_context import create_chat_profile_context

            chat_profile_memory_provider = create_chat_profile_context(
                profile_reader=profile_snapshot_reader, observation_reader=document_observation_reader
            )
    if health_profile_memory_provider is None:
        # Legacy application settings remain a Chat-compatible read alias.
        health_profile_memory_provider = chat_profile_memory_provider
    record_session_repository = (
        record_session_repository
        if record_session_repository is not None
        else create_application_record_session_repository()
    )
    record_session_cache = (
        record_session_cache if record_session_cache is not None else create_application_record_session_cache()
    )
    record_prompt_renderer = (
        record_prompt_renderer if record_prompt_renderer is not None else create_application_record_prompt_renderer()
    )
    record_creation_store = (
        record_creation_store if record_creation_store is not None else create_application_record_creation_store()
    )
    record_reply_recovery_store = (
        record_reply_recovery_store
        if record_reply_recovery_store is not None
        else create_application_record_reply_recovery_store()
    )
    record_completion_store = (
        record_completion_store if record_completion_store is not None else create_application_record_completion_store()
    )
    if record_sources is not None:
        from bootstrap.record_sources import build_record_source_runtime
        from runtime.source_recovery import SourceRecoveryRunner

        record_source_runtime = build_record_source_runtime(
            resources=record_sources,
            legacy=record_profile_source,
            completion_store=record_completion_store,
            inbox=document_sources.profile.inbox,
            presence=document_sources.admission.presence,
            fresh_admission=document_sources.admission.admission,
        )
        record_profile_source = record_source_runtime.router
        record_completion_store = record_source_runtime.completion_store
        source_workers += (('record-source', SourceRecoveryRunner(record_source_runtime.worker.recover_pending)),)
    if questionnaire_service is None and isinstance(agent_runtime, AgentRuntime):
        from adapters.questionnaire_factory import build_questionnaire_service

        questionnaire_service = build_questionnaire_service(
            runtime=agent_runtime,
            interpreter=create_application_health_report_interpreter(
                agent_runtime, document_parsing_client=document_parsing_client, current=True
            ),
            session_repository=record_session_repository,
            session_cache=record_session_cache,
            task_supervisor=task_supervisor,
            **(
                {'profile_source_delivery': questionnaire_source_delivery}
                if questionnaire_source_delivery is not None
                else {}
            ),
        )
    if isinstance(agent_runtime, AgentRuntime) and hasattr(questionnaire_service, 'agent_port'):
        from modules.questionnaire.agent import install_questionnaire_agent

        if not install_questionnaire_agent(agent_runtime, questionnaire_service):
            logger.warning('Questionnaire main Agent unavailable; existing data queries remain available')
    questionnaire_dependencies = (
        {'questionnaire_gate': questionnaire_service} if questionnaire_service is not None else {}
    )
    if record_document_port is None:
        if (
            document_compatibility_service_factory is None
            and default_document_processing
            and isinstance(agent_runtime, AgentRuntime)
        ):
            from bootstrap.document_contracts import create_application_document_compatibility_service

            def document_compatibility_service_factory():
                return create_application_document_compatibility_service(
                    processing_service=health_report_processing_service_factory(),
                    profile_service=health_profile_service,
                    publication_ledger=publication_ledger,
                )

        if document_compatibility_service_factory is not None:
            from adapters.record.document_compatibility import create_prepared_record_document_port

            record_document_port = create_prepared_record_document_port(
                document_compatibility_service_factory(),
                legacy_port=LegacyRecordDocumentAdapter(processor_factory=health_report_processing_service_factory),
            )
        else:
            # Explicit legacy processor injection remains a compatibility path.
            record_document_port = LegacyRecordDocumentAdapter(
                processor_factory=health_report_processing_service_factory
            )
    record_service = qa_manager_type(
        agent_runtime,
        **questionnaire_dependencies,
        session_repository=record_session_repository,
        session_cache=record_session_cache,
        creation_store=record_creation_store,
        reply_recovery_store=record_reply_recovery_store,
        completion_store=record_completion_store,
        creation_task_spawner=lambda coroutine: task_supervisor.create_task(coroutine, name='qa-creation'),
        prompt_renderer=record_prompt_renderer,
        record_profile_source=record_profile_source,
        record_document_port=record_document_port,
        health_profile_memory_provider=record_profile_memory_provider,
    )
    if isinstance(agent_runtime, AgentRuntime) and hasattr(record_service, 'record_agent'):
        from modules.record.agent import install_record_agent

        if not install_record_agent(agent_runtime, record_service):
            logger.warning('Record main Agent unavailable; existing data queries remain available')
    chat_session_repository = (
        chat_session_repository if chat_session_repository is not None else create_application_chat_session_repository()
    )
    chat_custom_message_outbox = (
        chat_custom_message_outbox
        if chat_custom_message_outbox is not None
        else create_application_chat_custom_message_outbox()
    )
    if report_artifact_store is None and report_pipeline is None:
        report_artifact_store = create_application_report_artifact_store()
    chat_service = chat_manager_type(
        agent_runtime,
        session_repository=chat_session_repository,
        custom_message_outbox=chat_custom_message_outbox,
        qa_session_gate=record_service,
        **({'questionnaire_service': questionnaire_service} if questionnaire_service is not None else {}),
        chat_profile_source=chat_profile_source,
        health_profile_memory_provider=chat_profile_memory_provider,
        report_context_provider=(
            None
            if report_artifact_store is None
            else ReportChatContextProvider(
                store=report_artifact_store,
                selector=AgentReportContextSelector(hub=agent_runtime.hub),
            )
        ),
    )
    if isinstance(agent_runtime, AgentRuntime) and hasattr(chat_service, 'agent_port'):
        from modules.chat import install_chat_agent

        if not install_chat_agent(agent_runtime, chat_service):
            logger.warning('Chat main Agent unavailable; data services remain available')
    lifecycle = lifecycle or ApplicationLifecycle()
    if lifecycle.state is LifecycleState.STARTING:
        lifecycle.mark_ready()
    report_task_coordinator = report_task_coordinator or ReportTaskCoordinator()
    health_profile_ingestion_worker = (
        health_profile_ingestion_worker
        if health_profile_ingestion_worker is not None
        else _create_health_profile_ingestion_worker(
            agent_runtime,
            **({'source_snapshot_resolvers': document_sources.resolvers} if document_sources is not None else {}),
        )
    )
    if report_pipeline is None:
        if (
            report_document_source_factory is None
            and default_document_processing
            and isinstance(agent_runtime, AgentRuntime)
        ):
            from bootstrap.report_documents import create_application_report_document_source_factory

            report_document_source_factory = create_application_report_document_source_factory(
                processing_service_factory=health_report_processing_service_factory,
                profile_service=health_profile_service,
                **({'publication_ledger': publication_ledger} if publication_ledger is not None else {}),
            )
        if profile_snapshot_reader is None and isinstance(agent_runtime, AgentRuntime):
            from modules.health_profile.application import create_application_profile_public_contracts

            profile_snapshot_reader = create_application_profile_public_contracts().reader
        from adapters.report.research import create_application_report_research

        research_agent = create_application_report_research(agent_runtime)
        report_pipeline = build_report_pipeline(
            research_agent=research_agent,
            agent_hub=agent_runtime.hub,
            health_profile_service=health_profile_service,
            profile_snapshot_reader=profile_snapshot_reader,
            artifact_store=report_artifact_store,
            document_processing_service_factory=health_report_processing_service_factory,
            **(
                {'document_source_factory': report_document_source_factory}
                if report_document_source_factory is not None
                else {}
            ),
            **({'questionnaire_service': questionnaire_service} if questionnaire_service is not None else {}),
        )
        _register_owned_closeables(
            resources,
            report_pipeline,
            owner_name='report-pipeline-legacy',
        )
    workflow = getattr(report_artifact_store, 'workflow', None)
    if (
        report_service is None
        and workflow is not None
        and callable(getattr(report_pipeline, 'execute_and_callback', None))
    ):
        from adapters.report.delivery import ReportCallbackTransport
        from adapters.report.main_agent import build_report_main_agent
        from config.env import REPORT_CALLBACK_BACKLOG_LOG_INTERVAL_SECONDS
        from config.env import REPORT_CALLBACK_BATCH_SIZE
        from config.env import REPORT_CALLBACK_CONCURRENCY
        from config.env import REPORT_CALLBACK_RETRY_DELAYS_SECONDS
        from config.env import REPORT_CALLBACK_RETRY_JITTER_RATIO
        from config.env import REPORT_CALLBACK_TIMEOUT_SECONDS
        from config.env import REPORT_CALLBACK_URL
        from config.env import REPORT_EVALUATE_CALLBACK_URL
        from modules.report.durable import DurableReportService
        from modules.report.durable import ReportCallbackWorker

        report_main_agent = (
            build_report_main_agent(agent_runtime, report_pipeline) if isinstance(agent_runtime, AgentRuntime) else None
        )
        report_service = DurableReportService(
            main_agent=report_main_agent,
            store=workflow,
            artifact_store=report_artifact_store,
            pipeline=report_pipeline,
            callback_url=REPORT_CALLBACK_URL,
            evaluate_callback_url=REPORT_EVALUATE_CALLBACK_URL,
        )
        if report_callback_worker is None:
            transport = ReportCallbackTransport(
                timeout_seconds=REPORT_CALLBACK_TIMEOUT_SECONDS,
                concurrency=REPORT_CALLBACK_CONCURRENCY,
            )
            resources.register_async(transport, name='report-callback-http')
            report_callback_worker = ReportCallbackWorker(
                store=workflow,
                transport=transport,
                concurrency=REPORT_CALLBACK_CONCURRENCY,
                timeout_seconds=REPORT_CALLBACK_TIMEOUT_SECONDS,
                batch_size=REPORT_CALLBACK_BATCH_SIZE,
                retry_delays_seconds=REPORT_CALLBACK_RETRY_DELAYS_SECONDS,
                jitter_ratio=REPORT_CALLBACK_RETRY_JITTER_RATIO,
                backlog_log_interval_seconds=REPORT_CALLBACK_BACKLOG_LOG_INTERVAL_SECONDS,
            )

        def wake_report_delivery():
            if task_supervisor.accepting:
                with bind_log_context(LogContext()):
                    task_supervisor.create_task(
                        report_callback_worker.drain_pending(),
                        name='worker:report-callback-wake',
                    )

        workflow.on_delivery_ready = wake_report_delivery
    if report_followup_service is None and isinstance(agent_runtime, AgentRuntime):
        from adapters.report.delegation import build_report_followup_service
        from adapters.report.delivery import ReportCallbackTransport
        from config import env as settings
        from db.mongodb import database
        from modules.report.durable import ReportCallbackWorker

        try:
            report_followup_service = build_report_followup_service(
                hub=agent_runtime.hub,
                database=database,
                callback_url=settings.REPORT_CALLBACK_URL,
                product_catalog_url=settings.REPORT_PRODUCT_URL,
                generation_timeout_seconds=settings.REPORT_GENERATION_TIMEOUT_SECONDS,
            )
        except Exception as error:
            logger.warning('report follow-up service unavailable: error_type=%s', type(error).__name__)
        if report_followup_service is not None and report_followup_callback_worker is None:
            transport = ReportCallbackTransport(
                timeout_seconds=settings.REPORT_CALLBACK_TIMEOUT_SECONDS,
                concurrency=settings.REPORT_CALLBACK_CONCURRENCY,
            )
            resources.register_async(transport, name='report-followup-callback-http')
            report_followup_callback_worker = ReportCallbackWorker(
                store=report_followup_service.store,
                transport=transport,
                concurrency=settings.REPORT_CALLBACK_CONCURRENCY,
                timeout_seconds=settings.REPORT_CALLBACK_TIMEOUT_SECONDS,
                batch_size=settings.REPORT_CALLBACK_BATCH_SIZE,
                retry_delays_seconds=settings.REPORT_CALLBACK_RETRY_DELAYS_SECONDS,
                jitter_ratio=settings.REPORT_CALLBACK_RETRY_JITTER_RATIO,
                backlog_log_interval_seconds=settings.REPORT_CALLBACK_BACKLOG_LOG_INTERVAL_SECONDS,
                callback_scope='report_followup',
            )
    report_main_agent = getattr(report_service, 'main_agent', None)
    if report_followup_service is not None and report_main_agent is not None:
        report_followup_service.attach_main_agent(report_main_agent)
    if gut_barrier_service is None and isinstance(agent_runtime, AgentRuntime):
        from adapters.gut_barrier.factory import build_gut_barrier_service

        try:
            gut_barrier_service = build_gut_barrier_service(
                parsing_client=document_parsing_client,
                resources=resources,
                agent_hub=agent_runtime.hub,
                report_followup_service=report_followup_service,
                agent_runtime=agent_runtime,
            )
        except Exception as error:
            # A new optional capability must not prevent unrelated APIs starting.
            # Its own routes explicitly return 503 until configuration is fixed.
            logger.warning('gut barrier service unavailable: error_type=%s', type(error).__name__)
    if health_analysis_service is None and isinstance(agent_runtime, AgentRuntime):
        from bootstrap.health_analysis import build_health_analysis_confirmation_port
        from bootstrap.health_analysis import build_health_analysis_service

        if (
            health_analysis_basic_info_sync is None
            and document_sources is not None
            and document_sources.profile.basic_info is not None
        ):
            health_analysis_basic_info_sync = build_health_analysis_confirmation_port(
                document_sources.profile.basic_info
            )

        health_analysis_service = build_health_analysis_service(
            agent_runtime,
            basic_info_sync=health_analysis_basic_info_sync,
            profile_reader=health_analysis_profile_reader,
            chat_reader=health_analysis_chat_reader,
            patient_sender_reader=health_analysis_patient_sender_reader,
            trace_writer=health_analysis_trace_writer,
            profiles=profile_snapshot_reader,
            resources=resources,
        )
    if health_analysis_basic_info_sync is not None:
        from modules.health_analysis.basic_info_sync import BasicInfoCompensationWorker
        from runtime.source_recovery import SourceRecoveryRunner

        compensation = BasicInfoCompensationWorker(health_analysis_basic_info_sync)
        source_workers += (('health-analysis-basic-info', SourceRecoveryRunner(compensation.run_once)),)
    return AppContainer(
        agent_runtime=agent_runtime,
        analysis_service_factory=analysis_service_factory,
        source_workers=source_workers,
        startup_initializers=startup_initializers,
        record_service=record_service,
        chat_service=chat_service,
        health_profile_service=health_profile_service,
        health_report_source=health_report_source,
        record_profile_source=record_profile_source,
        chat_profile_source=chat_profile_source,
        health_profile_memory_provider=health_profile_memory_provider,
        record_profile_memory_provider=record_profile_memory_provider,
        chat_profile_memory_provider=chat_profile_memory_provider,
        health_profile_ingestion_worker=health_profile_ingestion_worker,
        report_pipeline=report_pipeline,
        report_task_coordinator=report_task_coordinator,
        report_artifact_store=report_artifact_store,
        report_service=report_service,
        report_callback_worker=report_callback_worker,
        report_followup_service=report_followup_service,
        report_followup_callback_worker=report_followup_callback_worker,
        gut_barrier_service=gut_barrier_service,
        questionnaire_service=questionnaire_service,
        health_analysis_service=health_analysis_service,
        lifecycle=lifecycle,
        task_supervisor=task_supervisor,
        resources=resources,
    )


__all__ = [
    'build_application_bundle',
    'build_container',
    'create_application_health_report_interpreter',
]


def build_application_bundle(
    agent_runtime: AgentRuntime,
    *,
    debug: bool,
    owns_agent_runtime: bool = False,
    resources: ResourceRegistry | None = None,
    **container_dependencies: Any,
):
    """Compose the container, Tornado application and ownership bundle."""
    from runtime.application import ApplicationBundle
    from web.application import create_web_application

    resources = resources if resources is not None else ResourceRegistry()
    if owns_agent_runtime and agent_runtime not in resources:
        resources.register_sync(agent_runtime, name='agent-runtime')
    try:
        container = build_container(
            agent_runtime,
            resources=resources,
            **container_dependencies,
        )
        application = create_web_application(container, debug=debug)
        bundle = ApplicationBundle(
            web_application=application,
            container=container,
            owns_agent_runtime=owns_agent_runtime,
            resources=resources,
        )
        application.settings['application_bundle'] = bundle
        return bundle
    except Exception:
        _close_resources_after_failed_build(resources)
        raise


def create_application_health_report_interpreter(
    agent_runtime: AgentRuntime,
    *,
    document_parsing_client: Any = None,
    current: bool = False,
) -> HealthReportInterpreter:
    """Construct the Analysis interpreter from its concrete document port."""
    if document_parsing_client is None:
        document_parsing_client = create_application_document_parsing_client()
    from adapters.analysis.microbiome_source import read_microbiome_source_facts
    from modules.analysis.application.current_interpreter import CurrentHealthReportInterpreter

    interpreter = CurrentHealthReportInterpreter if current else HealthReportInterpreter
    return interpreter(
        agent_runtime,
        docmind_client=document_parsing_client,
        count_input_tokens=count_report_input_tokens,
        source_fact_reader=read_microbiome_source_facts,
    )


def _create_health_profile_service(agent_runtime):
    from modules.health_profile.application import create_application_health_profile_service

    return create_application_health_profile_service(agent_runtime)


def _create_health_report_source():
    from modules.health_profile.application import create_application_health_report_source

    return create_application_health_report_source()


def _create_record_profile_source():
    from modules.health_profile.application import create_application_record_profile_source

    return create_application_record_profile_source()


def _create_chat_profile_source():
    from modules.health_profile.application import create_application_chat_profile_source

    return create_application_chat_profile_source()


def _create_health_profile_memory_provider():
    """Historical server import; default domain composition no longer uses it."""
    from modules.health_profile.application import create_application_health_profile_chat_memory_provider

    return create_application_health_profile_chat_memory_provider()


def _create_health_profile_ingestion_worker(agent_runtime, **kwargs):
    from modules.health_profile.application import create_application_health_profile_ingestion_worker

    return create_application_health_profile_ingestion_worker(agent_runtime, **kwargs)


def _register_owned_closeables(
    resources: ResourceRegistry,
    owner: Any,
    *,
    owner_name: str,
) -> None:
    """Register closeable objects created as direct members of an owner.

    The legacy Report builder returns a pipeline rather than an ownership
    bundle.  Until its owning module exposes that contract, the composition
    root records only direct closeable members and does not alter Report code.
    """
    candidates = [(owner_name, owner)]
    try:
        candidates.extend((f'{owner_name}.{name}', value) for name, value in vars(owner).items())
    except TypeError:
        pass
    for name, resource in candidates:
        if resource in resources:
            continue
        if callable(getattr(resource, 'aclose', None)):
            resources.register_async(resource, name=name)
        elif callable(getattr(resource, 'close', None)):
            resources.register_sync(resource, name=name)


def _close_resources_after_failed_build(resources: ResourceRegistry) -> None:
    """Start cleanup without replacing the original composition failure."""

    async def close_resources() -> None:
        try:
            await resources.aclose()
        except Exception:
            logger.exception('failed to close resources after startup failure')

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(close_resources())
        return
    task = loop.create_task(
        close_resources(),
        name='startup:close-owned-resources',
    )

    def consume_failure(completed: asyncio.Task[None]) -> None:
        try:
            completed.result()
        except Exception:
            # ``close_resources`` already contains its own defensive logging;
            # keep this callback robust if it is changed in the future.
            logger.exception('resource cleanup task failed unexpectedly')

    task.add_done_callback(consume_failure)
