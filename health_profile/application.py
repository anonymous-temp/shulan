"""使用项目 MongoDB 集合组装用户健康画像服务。"""

from __future__ import annotations

from agents import AgentRuntime
from db.mongodb import collection_document_observations
from db.mongodb import collection_health_profile_events
from db.mongodb import collection_health_profile_evidence_results
from db.mongodb import collection_health_profile_facts
from db.mongodb import collection_health_profile_jobs
from db.mongodb import collection_health_profile_source_publications
from db.mongodb import collection_health_profile_views
from db.mongodb import collection_health_report_revisions
from db.mongodb import collection_health_reports
from db.mongodb import collection_health_subjects
from db.mongodb import collection_sessions

from .chat_ingestion import ChatProfileProcessor
from .chat_source import ChatProfileSourceService
from .chat_source import MongoChatProfileStore
from .document_observations import MongoDocumentObservationStore
from .evidence_results import MongoEvidenceResultStore
from .evidence_results import ProfileEvidenceResults
from .evidence_results import evidence_revision
from .ingestion import HealthProfileJobQueue
from .maintenance import HealthProfileJobMaintenanceService
from .memory import RepositoryHealthProfileChatMemoryProvider
from .models import ProfileIngestionSourceType
from .record_ingestion import RecordProfileProcessor
from .record_source import MongoRecordProfileStore
from .record_source import RecordProfileSourceService
from .report_ingestion import HealthReportProfileProcessor
from .report_source import HealthReportSourceService
from .report_source import MongoHealthReportStore
from .repository import MongoHealthProfileRepository
from .service import HealthProfileService
from .worker import HealthProfileIngestionWorker
from .worker import ProfileIngestionProcessorRouter


def _document_observations():
    return MongoDocumentObservationStore(collection_document_observations, collection_health_profile_jobs)


def create_application_health_profile_service(
    agent_runtime: AgentRuntime,
) -> HealthProfileService:
    if not isinstance(agent_runtime, AgentRuntime):
        raise TypeError('agent_runtime must be an AgentRuntime')
    repository = MongoHealthProfileRepository(
        subjects=collection_health_subjects,
        facts=collection_health_profile_facts,
        events=collection_health_profile_events,
        views=collection_health_profile_views,
        jobs=collection_health_profile_jobs,
    )
    return HealthProfileService(
        repository,
        agent_hub=agent_runtime.hub,
        lease_manager=agent_runtime.lease_manager,
        document_observation_store=_document_observations(),
        evidence_results=ProfileEvidenceResults(
            MongoEvidenceResultStore(collection_health_profile_evidence_results),
            agent_runtime.lease_manager,
        ),
        evidence_revision=_application_evidence_revision(agent_runtime),
    )


def _application_evidence_revision(runtime):
    from aion.prompts import prompt_manager

    from agents.graphs.health_profile.graph import PROFILE_EVIDENCE_CAPABILITY
    from agents.graphs.health_profile.profile import PROFILE_FOCUS_PROFILE
    from agents.graphs.health_profile.profile import PROFILE_SYNTHESIS_PROFILE
    from config.env import KNOWLEDGE_API_BASE_URL
    from config.env import LLM_DEFAULT_MODEL
    from config.env import LLM_PROVIDER

    profiles = []
    for name in (PROFILE_FOCUS_PROFILE, PROFILE_SYNTHESIS_PROFILE):
        profile = runtime.profiles.get(name)
        profiles.append(
            {
                'name': profile.name,
                'model': profile.model,
                'modelKwargs': dict(profile.model_kwargs),
                'prompt': prompt_manager.render(profile.prompt_name, **dict(profile.prompt_variables)),
            }
        )
    return evidence_revision(
        configuration={
            'graph': runtime.registry.get(PROFILE_EVIDENCE_CAPABILITY).version,
            'provider': LLM_PROVIDER,
            'defaultModel': LLM_DEFAULT_MODEL,
            'knowledgeEndpoint': KNOWLEDGE_API_BASE_URL,
            'knowledgeEnabled': runtime.knowledge_gateway.enabled,
        },
        profiles=profiles,
    )


def create_application_health_report_source() -> HealthReportSourceService:
    """组装报告历史存储及画像任务投递服务。"""
    repository = MongoHealthProfileRepository(
        subjects=collection_health_subjects,
        facts=collection_health_profile_facts,
        events=collection_health_profile_events,
        views=collection_health_profile_views,
        jobs=collection_health_profile_jobs,
        publications=collection_health_profile_source_publications,
    )
    return HealthReportSourceService(
        MongoHealthReportStore(collection_health_reports, collection_health_report_revisions),
        HealthProfileJobQueue(repository),
        observation_store=_document_observations(),
    )


def create_application_record_profile_source() -> RecordProfileSourceService:
    """组装已完成建档快照的画像任务投递服务。"""
    repository = MongoHealthProfileRepository(
        subjects=collection_health_subjects,
        facts=collection_health_profile_facts,
        events=collection_health_profile_events,
        views=collection_health_profile_views,
        jobs=collection_health_profile_jobs,
        publications=collection_health_profile_source_publications,
    )
    return RecordProfileSourceService(
        MongoRecordProfileStore(collection_sessions),
        HealthProfileJobQueue(repository),
    )


def create_application_chat_profile_source() -> ChatProfileSourceService:
    """组装普通聊天用户消息的画像任务投递服务。"""
    repository = MongoHealthProfileRepository(
        subjects=collection_health_subjects,
        facts=collection_health_profile_facts,
        events=collection_health_profile_events,
        views=collection_health_profile_views,
        jobs=collection_health_profile_jobs,
        publications=collection_health_profile_source_publications,
    )
    return ChatProfileSourceService(
        MongoChatProfileStore(collection_sessions),
        HealthProfileJobQueue(repository),
    )


def create_application_health_profile_chat_memory_provider() -> RepositoryHealthProfileChatMemoryProvider:
    """组装普通对话使用的只读、临时画像记忆提供器。"""
    repository = MongoHealthProfileRepository(
        subjects=collection_health_subjects,
        facts=collection_health_profile_facts,
        events=collection_health_profile_events,
        views=collection_health_profile_views,
        jobs=collection_health_profile_jobs,
    )
    return RepositoryHealthProfileChatMemoryProvider(repository, document_observation_store=_document_observations())


def create_application_health_profile_job_maintenance_service() -> HealthProfileJobMaintenanceService:
    """组装只供内部运维调用的画像任务查询与重驱服务。"""
    repository = MongoHealthProfileRepository(
        subjects=collection_health_subjects,
        facts=collection_health_profile_facts,
        events=collection_health_profile_events,
        views=collection_health_profile_views,
        jobs=collection_health_profile_jobs,
    )
    return HealthProfileJobMaintenanceService(repository)


def create_application_health_profile_ingestion_worker(
    agent_runtime: AgentRuntime,
    *,
    source_snapshot_resolvers=None,
) -> HealthProfileIngestionWorker:
    """组装消费持久化画像任务的应用级 Worker。"""
    if not isinstance(agent_runtime, AgentRuntime):
        raise TypeError('agent_runtime must be an AgentRuntime')
    repository = MongoHealthProfileRepository(
        subjects=collection_health_subjects,
        facts=collection_health_profile_facts,
        events=collection_health_profile_events,
        views=collection_health_profile_views,
        jobs=collection_health_profile_jobs,
        publications=collection_health_profile_source_publications,
    )
    queue = HealthProfileJobQueue(repository)
    report_store = MongoHealthReportStore(collection_health_reports, collection_health_report_revisions)
    record_store = MongoRecordProfileStore(collection_sessions)
    chat_store = MongoChatProfileStore(collection_sessions)
    public_contracts = _build_profile_public_contracts(
        repository,
        report_store,
        resolvers=source_snapshot_resolvers,
        agent_hub=agent_runtime.hub,
    )
    from .source_application import CompositeProfilePendingSource

    report_processor = HealthReportProfileProcessor(
        store=report_store,
        repository=repository,
    )
    profile_service = HealthProfileService(
        repository,
        agent_hub=agent_runtime.hub,
        lease_manager=agent_runtime.lease_manager,
    )
    record_processor = RecordProfileProcessor(
        store=record_store,
        repository=repository,
        profile_service=profile_service,
        agent_hub=agent_runtime.hub,
    )
    chat_processor = ChatProfileProcessor(
        store=chat_store,
        repository=repository,
        profile_service=profile_service,
        agent_hub=agent_runtime.hub,
    )
    from adapters.questionnaire_factory import create_questionnaire_report_reader
    from adapters.questionnaire_factory import create_questionnaire_store

    from .questionnaire_ingestion import QuestionnaireProfileProcessor
    from .questionnaire_source import QuestionnaireProfileSourceService

    questionnaire_store = create_questionnaire_store()
    questionnaire_processor = QuestionnaireProfileProcessor(
        store=questionnaire_store,
        repository=repository,
        profile_service=profile_service,
        agent_hub=agent_runtime.hub,
        documents=create_questionnaire_report_reader(),
        report_source=create_application_health_report_source(),
    )
    processor = ProfileIngestionProcessorRouter(
        {
            ProfileIngestionSourceType.HEALTH_REPORT: report_processor,
            ProfileIngestionSourceType.RECORD: record_processor,
            ProfileIngestionSourceType.CHAT: chat_processor,
            ProfileIngestionSourceType.QUESTIONNAIRE: questionnaire_processor,
        },
        source_guard=repository.is_source_publication_managed,
    )
    return HealthProfileIngestionWorker(
        queue,
        processor,
        source_types=processor.source_types,
        pending_sources={
            ProfileIngestionSourceType.QUESTIONNAIRE: QuestionnaireProfileSourceService(questionnaire_store, queue),
            ProfileIngestionSourceType.HEALTH_REPORT: CompositeProfilePendingSource(
                HealthReportSourceService(report_store, queue),
                public_contracts.worker,
            ),
            ProfileIngestionSourceType.RECORD: RecordProfileSourceService(
                record_store,
                queue,
            ),
            ProfileIngestionSourceType.CHAT: ChatProfileSourceService(
                chat_store,
                queue,
            ),
        },
    )


def create_application_questionnaire_profile_source(*, store=None):
    from adapters.questionnaire_factory import create_questionnaire_store

    from .questionnaire_source import QuestionnaireProfileSourceService

    repository = MongoHealthProfileRepository(
        subjects=collection_health_subjects,
        facts=collection_health_profile_facts,
        events=collection_health_profile_events,
        views=collection_health_profile_views,
        jobs=collection_health_profile_jobs,
        publications=collection_health_profile_source_publications,
    )
    return QuestionnaireProfileSourceService(store or create_questionnaire_store(), HealthProfileJobQueue(repository))


def _build_profile_public_contracts(repository, report_store, *, resolvers=None, agent_hub=None):
    from types import SimpleNamespace

    from .legacy_ingestion_status import ProfileLegacyIngestionStatusReader
    from .snapshots import ProfileSnapshotReader
    from .source_application import ProfilePublicationWorker
    from .source_application import ProfileSourceApplier
    from .source_material import LegacyHealthReportSnapshotResolver
    from .source_material import ProfileRetractionSnapshotResolver
    from .source_publications import MongoProfilePublicationStore
    from .source_publications import ProfileSourceInbox

    inbox = ProfileSourceInbox(
        MongoProfilePublicationStore(collection_health_profile_source_publications),
        repository=repository,
    )
    source_service = HealthReportSourceService(report_store, HealthProfileJobQueue(repository))
    registered = {
        ('health_report', 'health-report-observations.v1'): LegacyHealthReportSnapshotResolver(source_service),
        ('profile', 'profile-source-retraction.v1'): ProfileRetractionSnapshotResolver(),
    }
    if resolvers:
        registered.update(resolvers)
    inbox.current_document_source = registered.get(('document', 'document.source-observations.v2'))
    document_source = registered.get(('document', 'document.source-observations.v1'))
    if registered.get(('document', 'document.retraction.v1')) is document_source:
        inbox.document_source = document_source
    from .ingestion import HealthFactPolicy
    from .source_extraction import DurableProfileSourceExtractor

    extractor = DurableProfileSourceExtractor(agent_hub, policy_version=HealthFactPolicy.VERSION) if agent_hub else None
    applier = ProfileSourceApplier(inbox, resolvers=registered, extractor=extractor)
    return SimpleNamespace(
        basic_info=getattr(repository, 'basic_info', None),
        reader=ProfileSnapshotReader(repository, publications=inbox),
        legacy_ingestion=ProfileLegacyIngestionStatusReader(repository),
        inbox=inbox,
        worker=ProfilePublicationWorker(inbox, applier),
    )


def create_application_profile_public_contracts(*, resolvers=None, agent_runtime=None, basic_info_collection=None):
    """Build pure reads, explicit event intake and durable recovery; no new HTTP route."""
    repository = MongoHealthProfileRepository(
        subjects=collection_health_subjects,
        facts=collection_health_profile_facts,
        events=collection_health_profile_events,
        views=collection_health_profile_views,
        jobs=collection_health_profile_jobs,
        publications=collection_health_profile_source_publications,
        basic_info=basic_info_collection,
    )
    return _build_profile_public_contracts(
        repository,
        MongoHealthReportStore(collection_health_reports, collection_health_report_revisions),
        resolvers=resolvers,
        agent_hub=agent_runtime.hub if agent_runtime is not None else None,
    )


def create_application_profile_source_admission(*, inbox, owner_fence_readers):
    """Construct independent source admission/history ports; perform no I/O."""
    from types import SimpleNamespace

    from .source_admission import ProfileFreshSourceAdmission
    from .source_archive import ProfileSourceMaterialArchive
    from .source_presence import MongoProfileSourcePresenceReader

    presence = MongoProfileSourcePresenceReader(
        subjects=collection_health_subjects,
        reports=collection_health_reports,
        revisions=collection_health_report_revisions,
        jobs=collection_health_profile_jobs,
        facts=collection_health_profile_facts,
        publications=collection_health_profile_source_publications,
    )
    return SimpleNamespace(
        presence=presence,
        admission=ProfileFreshSourceAdmission(
            inbox=inbox,
            presence_reader=presence,
            owner_fence_readers=owner_fence_readers,
        ),
        archive=ProfileSourceMaterialArchive(
            report_store=MongoHealthReportStore(collection_health_reports, collection_health_report_revisions),
            publication_store=inbox.store,
        ),
    )
