"""Gut-barrier service composition using the shared report callback configuration."""

from __future__ import annotations

from urllib.parse import urlsplit

from pymongo.write_concern import WriteConcern

from adapters.gut_barrier.delivery import GutBarrierCallbackTransport
from adapters.gut_barrier.evidence import GutBarrierEvidence
from adapters.gut_barrier.sources import GutBarrierProducts
from adapters.gut_barrier.sources import GutBarrierSources
from adapters.gut_barrier.store import MongoGutBarrierStore
from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.policy import GUT_BARRIER_GENERATION_CONCURRENCY
from modules.gut_barrier.risk_evaluator import RiskModelRegistry
from modules.gut_barrier.service import GutBarrierService
from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY


def build_gut_barrier_service(
    *,
    parsing_client,
    resources,
    agent_hub=None,
    risk_models=None,
    risk_model_id=None,
    risk_model_version=None,
    report_followup_service=None,
    agent_runtime=None,
):
    # Deployment-owned injection only: no executable imports, model selection
    # or coefficients from patient request extras or untrusted documents.
    if any(value is not None for value in (risk_models, risk_model_id, risk_model_version)):
        if (
            not isinstance(risk_models, RiskModelRegistry)
            or not isinstance(risk_model_id, str)
            or not risk_model_id.strip()
            or not isinstance(risk_model_version, str)
            or not risk_model_version.strip()
            or risk_models.resolve(risk_model_id, risk_model_version) is None
        ):
            raise ValueError('gut-barrier risk model requires a registered, reviewed id and version')
    from config.env import GUT_BARRIER_PRODUCT_URL
    from config.env import REPORT_CALLBACK_BACKLOG_LOG_INTERVAL_SECONDS
    from config.env import REPORT_CALLBACK_BATCH_SIZE
    from config.env import REPORT_CALLBACK_CONCURRENCY
    from config.env import REPORT_CALLBACK_RETRY_DELAYS_SECONDS
    from config.env import REPORT_CALLBACK_RETRY_JITTER_RATIO
    from config.env import REPORT_CALLBACK_TIMEOUT_SECONDS
    from config.env import REPORT_CALLBACK_URL
    from config.env import REPORT_GENERATION_TIMEOUT_SECONDS
    from db.mongodb import database
    from modules.report.durable import ReportCallbackWorker

    # Gut-barrier is always enabled and shares the /api/v1/report receiver.
    callback_url = REPORT_CALLBACK_URL.strip()
    callback_error = 'REPORT_CALLBACK_URL must be a non-empty HTTP(S) URL without credentials or fragment'
    try:
        parsed = urlsplit(callback_url)
        parsed.port  # Validate malformed or out-of-range ports before admission.
    except ValueError as error:
        raise ValueError(callback_error) from error
    if (
        not callback_url
        or parsed.scheme not in {'http', 'https'}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError(callback_error)
    collection = database['gut_barrier_report_jobs'].with_options(
        write_concern=WriteConcern(w='majority', j=True, wtimeout=5000)
    )
    # Only the public Report port is reachable from this business domain.
    from adapters.gut_barrier.delegation import MongoGutBarrierDelegationStore
    from adapters.gut_barrier.delegation import PublicReportDelegationAdapter
    from modules.gut_barrier.delegation import DelegationPolicy
    from modules.gut_barrier.delegation import GutBarrierDelegationWorker

    store = MongoGutBarrierStore(collection)
    transport = GutBarrierCallbackTransport(
        store=store, timeout_seconds=REPORT_CALLBACK_TIMEOUT_SECONDS, concurrency=REPORT_CALLBACK_CONCURRENCY
    )
    resources.register_async(transport, name='gut-barrier-callback-http')
    from adapters.report.clients import EviMedClient
    from config import env as settings

    evidence_client = EviMedClient(
        base_url=getattr(settings, 'EVIMED_API_URL', ''),
        api_key=getattr(settings, 'EVIMED_API_KEY', ''),
        timeout_seconds=10,
        result_count=4,
        attempts=1,
    )
    evidence = GutBarrierEvidence(
        evimed_client=evidence_client, max_queries=12, max_results=36, per_query_results=3, timeout_seconds=120
    )
    resources.register_async(evidence_client, name='gut-barrier-evimed-http')
    resources.register_async(evidence, name='gut-barrier-public-evidence-http')
    worker = ReportCallbackWorker(
        store=store,
        transport=transport,
        clock=store.clock,
        concurrency=REPORT_CALLBACK_CONCURRENCY,
        timeout_seconds=REPORT_CALLBACK_TIMEOUT_SECONDS,
        batch_size=REPORT_CALLBACK_BATCH_SIZE,
        retry_delays_seconds=REPORT_CALLBACK_RETRY_DELAYS_SECONDS,
        jitter_ratio=REPORT_CALLBACK_RETRY_JITTER_RATIO,
        backlog_log_interval_seconds=REPORT_CALLBACK_BACKLOG_LOG_INTERVAL_SECONDS,
        callback_scope='gut_barrier',
    )
    from modules.gut_barrier.agent import GutBarrierAgent
    from modules.gut_barrier.agent import install_gut_barrier_agent
    from modules.report.artifacts import content_hash

    pipeline = GutBarrierPipeline(
        sources=GutBarrierSources(
            parsing_client=parsing_client, concurrency=DEFAULT_REPORT_FALLBACK_POLICY.source_parse_concurrency
        ),
        products=GutBarrierProducts(GUT_BARRIER_PRODUCT_URL),
        evidence=evidence,
        risk_models=risk_models,
        risk_model_id=risk_model_id,
        risk_model_version=risk_model_version,
    )
    main_agent = GutBarrierAgent(
        pipeline,
        timeout_seconds=REPORT_GENERATION_TIMEOUT_SECONDS,
        dependencies={
            'sources': 'gut-sources.v1',
            'evidence': 'gut-evidence.v1',
            'productCatalogDigest': content_hash(GUT_BARRIER_PRODUCT_URL),
            'riskModel': [risk_model_id, risk_model_version],
        },
    )
    service = GutBarrierService(
        store=store,
        pipeline=pipeline,
        main_agent=main_agent,
        transport=transport,
        callback_url=callback_url,
        callback_worker=worker,
        timeout_seconds=REPORT_GENERATION_TIMEOUT_SECONDS,
        concurrency=GUT_BARRIER_GENERATION_CONCURRENCY,
        delegation_worker=(
            GutBarrierDelegationWorker(
                store=MongoGutBarrierDelegationStore(store),
                port=PublicReportDelegationAdapter(report_followup_service),
            )
        ),
        delegation_policy=DelegationPolicy(),
    )
    if agent_runtime is not None:
        install_gut_barrier_agent(agent_runtime, service)
    return service
