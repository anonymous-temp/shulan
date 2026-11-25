"""Composition helpers for concrete Report adapters."""

from __future__ import annotations

import json
from typing import Any

from adapters.report.agent_followup import FollowupModelGatewayFactory
from adapters.report.agent_narrative import NarrativeClientFactory
from adapters.report.artifact_store import MongoReportArtifactStore
from adapters.report.callback import CallbackClient
from adapters.report.clients import EviMedClient
from adapters.report.clients import SourceClient
from adapters.report.document_source import ReportArchiveLocator
from adapters.report.document_source import ReportDocumentSourceFactory
from adapters.report.followup import AionFollowupReportGateway
from adapters.report.group_context import GroupContextClient
from adapters.report.profile_snapshot import HealthProfileSnapshotGateway
from adapters.report.public_evidence import PublicEvidenceClient
from adapters.report.versioned_profile import VersionedReportProfileGateway
from agents import AgentHub
from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY
from modules.report.fallback_policy import REPORT_GENERATION_CONCURRENCY
from modules.report.pipeline import ReportPipeline
from utils.log import getLogger

logger = getLogger(__name__)


async def load_report_session(
    user_id: str,
    session_id: str,
    *,
    redis_client: Any,
    session_collection: Any,
    questionnaire_service: Any = None,
) -> dict[str, Any] | None:
    """Load optional QA context using the product's Redis then Mongo order."""

    if questionnaire_service is not None:
        context = await questionnaire_service.get_session_context(user_id, session_id)
        if context is not None:
            return context
        if await questionnaire_service.hasPendingSession(user_id, session_id):
            return None
    redis_key = f'shulan:session:{user_id}:{session_id}'
    cached: Any = None
    try:
        cached = await redis_client.get(redis_key)
    except Exception as error:
        logger.warning(
            'report session Redis lookup failed: user_id=%s session_id=%s error_type=%s',
            user_id,
            session_id,
            type(error).__name__,
        )
    if cached:
        try:
            value = json.loads(cached) if isinstance(cached, (str, bytes)) else cached
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            logger.warning(
                'report session Redis payload is invalid: user_id=%s session_id=%s',
                user_id,
                session_id,
            )
        else:
            if isinstance(value, dict):
                return value

    return await session_collection.find_one(
        {'userId': user_id, 'sessionId': session_id},
        {'_id': 0},
    )


def create_application_report_artifact_store() -> MongoReportArtifactStore:
    """构造最终报告存储；连接复用既有共享 MongoDB 客户端。"""
    from pymongo.write_concern import WriteConcern

    from adapters.report.durable_store import MongoReportWorkflowStore
    from db.mongodb import collection_report_artifacts
    from db.mongodb import collection_report_batches
    from db.mongodb import collection_report_callback_deliveries

    concern = WriteConcern(w='majority', j=True, wtimeout=5000)
    batches = collection_report_batches.with_options(write_concern=concern)
    artifacts = collection_report_artifacts.with_options(write_concern=concern)
    deliveries = collection_report_callback_deliveries.with_options(write_concern=concern)
    workflow = MongoReportWorkflowStore(batches=batches, artifacts=artifacts, deliveries=deliveries)

    return MongoReportArtifactStore(
        batches=batches,
        artifacts=artifacts,
        workflow=workflow,
    )


def build_local_evidence_extras() -> dict[str, Any]:
    """Optional DeepSeek tool collaborators shared by production and local runs.

    Returns keyword arguments for ``ReportPipeline``. Nothing is created when
    the tool key is absent or the feature is switched off, so deployments on
    another provider keep the library + PubMed behaviour unchanged.
    """
    from config.env import CROSSREF_MAILTO
    from config.env import DEEPSEEK_TOOLS_API_KEY
    from config.env import NCBI_API_KEY
    from config.env import REPORT_DOCUMENT_OCR_ENABLED
    from config.env import REPORT_WEB_EVIDENCE_ENABLED

    extras: dict[str, Any] = {}
    if not DEEPSEEK_TOOLS_API_KEY:
        return extras
    from adapters.report.deepseek_tools import DeepSeekDocumentTextClient
    from adapters.report.deepseek_tools import DeepSeekToolsClient
    from adapters.report.web_evidence import WebEvidenceClient

    if REPORT_WEB_EVIDENCE_ENABLED:
        extras['web_evidence_client'] = WebEvidenceClient(
            DeepSeekToolsClient(api_key=DEEPSEEK_TOOLS_API_KEY),
            ncbi_api_key=NCBI_API_KEY, crossref_mailto=CROSSREF_MAILTO)
    if REPORT_DOCUMENT_OCR_ENABLED:
        extras['document_client'] = DeepSeekDocumentTextClient(
            DeepSeekToolsClient(api_key=DEEPSEEK_TOOLS_API_KEY))
    return extras


def build_report_pipeline(
    *,
    agent_hub: AgentHub,
    health_profile_service: Any = None,
    profile_snapshot_reader: Any = None,
    artifact_store: Any = None,
    document_processing_service_factory: Any = None,
    document_source_factory: Any = None,
    questionnaire_service: Any = None,
    research_agent: Any = None,
) -> ReportPipeline:
    """Build the current Report pipeline from concrete outbound adapters."""

    if not isinstance(agent_hub, AgentHub):
        raise TypeError('agent_hub must be an AgentHub')

    from config.env import CROSSREF_MAILTO
    from config.env import EVIMED_API_KEY
    from config.env import EVIMED_API_URL
    from config.env import EVIMED_TIMEOUT
    from config.env import NCBI_API_KEY
    from config.env import NCBI_CONTACT_EMAIL
    from config.env import REPORT_CALLBACK_URL
    from config.env import REPORT_EVALUATE_CALLBACK_URL
    from config.env import REPORT_GENERATION_TIMEOUT_SECONDS
    from config.env import REPORT_PRODUCT_URL
    from db.mongodb import collection_document_index
    from db.mongodb import collection_document_traces
    from db.mongodb import collection_sessions
    from db.redis import client as redis_client

    async def load_session(user_id: str, session_id: str) -> dict[str, Any] | None:
        return await load_report_session(
            user_id,
            session_id,
            redis_client=redis_client,
            session_collection=collection_sessions,
            questionnaire_service=questionnaire_service,
        )

    policy = DEFAULT_REPORT_FALLBACK_POLICY
    # 非模型 I/O（产品目录、HTTP 回调）继续走业务端口。
    followup_gateway = AionFollowupReportGateway(
        callback_url=REPORT_CALLBACK_URL,
        product_catalog_url=REPORT_PRODUCT_URL,
    )
    # 后续报告的模型调用统一经 AgentHub，按报告绑定身份。
    followup_model_factory = FollowupModelGatewayFactory(hub=agent_hub)

    async def push_followup_reports(
        data: dict[str, Any],
        user_id: str,
        session_id: str,
    ) -> Any:
        from modules.report.reports import _push_report

        return await _push_report(
            data,
            user_id,
            session_id,
            policy=policy,
            gateway=followup_gateway,
            model_gateway=followup_model_factory.create(
                user_id=user_id,
                session_id=session_id,
            ),
        )

    async def replay_followup_reports(
        payloads: tuple[dict[str, Any], ...],
    ) -> Any:
        from modules.report.reports import replay_followup_reports

        return await replay_followup_reports(
            payloads,
            policy=policy,
            gateway=followup_gateway,
        )

    # 主叙事统一经 AgentHub；每次报告执行由工厂创建绑定身份的适配器，
    # 共享的模型并发闸门由工厂持有，不随并发报告数量倍增。
    narrative_client_factory = NarrativeClientFactory(
        hub=agent_hub,
        policy=policy,
    )

    source_client = SourceClient(policy=policy)
    return ReportPipeline(
        clinical_report_enabled=True,
        research_agent=research_agent,
        source_client=source_client,
        group_message_client=GroupContextClient(
            callback_url=REPORT_CALLBACK_URL,
            source_client=source_client,
            policy=policy,
        ),
        evidence_client=EviMedClient(
            base_url=EVIMED_API_URL,
            api_key=EVIMED_API_KEY,
            timeout_seconds=EVIMED_TIMEOUT,
            attempts=policy.evidence_attempts,
        ),
        public_evidence_client=PublicEvidenceClient(
            ncbi_api_key=NCBI_API_KEY,
            contact_email=NCBI_CONTACT_EMAIL,
            crossref_mailto=CROSSREF_MAILTO,
        ),
        callback_client=CallbackClient(url=REPORT_EVALUATE_CALLBACK_URL, policy=policy),
        session_loader=load_session,
        narrative_client=None,
        narrative_client_factory=narrative_client_factory,
        followup_report_handler=push_followup_reports,
        followup_replay_handler=replay_followup_reports,
        document_source_factory=(
            document_source_factory
            if document_source_factory is not None
            else ReportDocumentSourceFactory(
                document_processing_service_factory,
                ReportArchiveLocator(collection_document_traces, collection_document_index),
            )
            if document_processing_service_factory is not None
            else None
        ),
        artifact_store=(artifact_store if artifact_store is not None else create_application_report_artifact_store()),
        profile_snapshot_client=(
            VersionedReportProfileGateway(profile_snapshot_reader)
            if profile_snapshot_reader is not None
            else None
            if health_profile_service is None
            else HealthProfileSnapshotGateway(
                profile_service=health_profile_service,
            )
        ),
        generation_timeout_seconds=REPORT_GENERATION_TIMEOUT_SECONDS,
        max_concurrent_evaluate_reports=REPORT_GENERATION_CONCURRENCY,
        **build_local_evidence_extras(),
    )


__all__ = [
    'build_local_evidence_extras',
    'build_report_pipeline',
    'create_application_report_artifact_store',
    'load_report_session',
]
