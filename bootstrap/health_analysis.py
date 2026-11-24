"""Explicit Health Analysis assembly and injected Profile confirmation boundary."""

import asyncio

from modules.health_analysis.application import HealthAnalysisService
from modules.health_analysis.contracts import AnalysisLimits
from modules.health_analysis.contracts import SourceUnavailableError


class _UnavailableReader:
    async def read(self, scope):
        raise SourceUnavailableError('not_configured')


def build_health_analysis_confirmation_port(profile_basic_info):
    from adapters.health_analysis.basic_info import ProfileBasicInfoConfirmationAdapter

    return ProfileBasicInfoConfirmationAdapter(profile_basic_info)


def build_health_analysis_service(
    runtime,
    *,
    basic_info_sync=None,
    profile_reader=None,
    chat_reader=None,
    patient_sender_reader=None,
    trace_writer=None,
    profiles=None,
    resources=None,
    limits=None,
    call_concurrency=4,
):
    from adapters.health_analysis.knowledge import HealthAnalysisKnowledge
    from adapters.health_analysis.model import HealthAnalysisModel
    from modules.health_analysis.agent import HealthAnalysisAgent
    from modules.health_analysis.generation import HealthAnalysisGenerator

    limits = limits if limits is not None else AnalysisLimits()
    if isinstance(call_concurrency, bool) or not isinstance(call_concurrency, int) or call_concurrency <= 0:
        raise ValueError('Health analysis call concurrency must be positive')
    semaphore = asyncio.Semaphore(call_concurrency)
    model = HealthAnalysisModel(runtime.llm_gateway, semaphore=semaphore)
    generator = HealthAnalysisAgent(
        HealthAnalysisGenerator(
            model, knowledge=HealthAnalysisKnowledge(runtime.knowledge_gateway), semaphore=semaphore
        )
    )
    if profile_reader is None and profiles is not None:
        from adapters.health_analysis.profile import ProfileMaterialsAdapter
        from health_profile.report_fact_materials import ProfileReportFactsReader

        profile_reader = ProfileMaterialsAdapter(ProfileReportFactsReader(profiles))
    if chat_reader is None:
        from config import env

        if env.CHAT_HISTORY_QUERY_URL:
            from adapters.health_analysis.chat_history import ChatHistoryMaterialsAdapter
            from bootstrap.chat_history import create_application_chat_history_tool

            tool = create_application_chat_history_tool(timeout_seconds=limits.source_timeout_seconds)
            chat_reader = ChatHistoryMaterialsAdapter(tool, patient_sender_reader=patient_sender_reader)
            close = getattr(chat_reader, 'aclose', None)
            if resources is not None and close is not None:
                resources.register_async(chat_reader, name='health-analysis-chat-history')
    if trace_writer is None:
        from adapters.health_analysis.trace import MongoRunTraceWriter
        from config.env import MONGODB_COLL_HEALTH_ANALYSIS_RUNS
        from db.mongodb import database

        trace_writer = MongoRunTraceWriter(database[MONGODB_COLL_HEALTH_ANALYSIS_RUNS])
    return HealthAnalysisService(
        profile_reader if profile_reader is not None else _UnavailableReader(),
        chat_reader if chat_reader is not None else _UnavailableReader(),
        generator,
        trace_writer,
        limits=limits,
        basic_info_sync=basic_info_sync,
    )
