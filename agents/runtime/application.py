"""从 Shulan 应用配置创建并注册生产 AgentRuntime。"""

from __future__ import annotations

import json
from pathlib import Path

from utils.log import getLogger

from ..graphs.analysis import register_current_analysis_health_report
from ..graphs.chat import register_chat_reply
from ..graphs.chat import register_chat_suggestions
from ..graphs.health_profile import register_profile_chat_extraction
from ..graphs.health_profile import register_profile_evidence
from ..graphs.health_profile import register_profile_record_extraction
from ..graphs.record import register_record_graphs
from ..graphs.report import register_report_followup
from ..graphs.report import register_report_narrative
from ..graphs.report import register_report_retrieve_context
from .container import AgentRuntime
from .lifecycle import AgentRuntimeSettings
from .lifecycle import create_agent_runtime

logger = getLogger(__name__)


def create_application_agent_runtime() -> AgentRuntime:
    """使用项目配置和数据库连接组装应用级 AgentRuntime。"""
    # 延迟导入避免仅导入本模块时初始化应用配置和外部连接。
    from config.env import AGENT_LEASE_TTL_SECONDS
    from config.env import KNOWLEDGE_API_BASE_URL
    from config.env import KNOWLEDGE_API_KEY
    from config.env import KNOWLEDGE_API_TIMEOUT_SECONDS
    from config.env import LLM_API_KEY
    from config.env import LLM_DEFAULT_MODEL
    from config.env import LLM_PROVIDER
    from config.env import MONGODB_COLL_AGENT_CHECKPOINT_WRITES
    from config.env import MONGODB_COLL_AGENT_CHECKPOINTS
    from config.env import MONGODB_DATABASE
    from config.env import MONGODB_URI
    from db.mongodb import collection_agent_llm_contexts
    from db.redis import client as redis_client

    runtime = create_agent_runtime(
        AgentRuntimeSettings(
            provider=LLM_PROVIDER,
            api_key=LLM_API_KEY,
            default_model=LLM_DEFAULT_MODEL,
            mongodb_uri=MONGODB_URI,
            mongodb_database=MONGODB_DATABASE,
            checkpoint_collection=MONGODB_COLL_AGENT_CHECKPOINTS,
            checkpoint_writes_collection=(MONGODB_COLL_AGENT_CHECKPOINT_WRITES),
            lease_ttl_seconds=AGENT_LEASE_TTL_SECONDS,
            knowledge_api_base_url=KNOWLEDGE_API_BASE_URL,
            knowledge_api_key=KNOWLEDGE_API_KEY,
            knowledge_timeout_seconds=KNOWLEDGE_API_TIMEOUT_SECONDS,
        ),
        context_collection=collection_agent_llm_contexts,
        redis_client=redis_client,
    )
    try:
        question_topics_path = Path(__file__).resolve().parents[2] / 'config' / 'question_topics.json'
        try:
            with question_topics_path.open(encoding='utf-8') as file:
                question_topics = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            # 与原 QASessionManager 的容错保持一致，配置异常时使用空主题。
            logger.warning(
                'Cannot load record question topics from %s: %s',
                question_topics_path,
                error,
            )
            question_topics = {'topics': [], 'rules': []}
        register_application_modules(runtime, question_topics=question_topics)
    except Exception:
        runtime.close()
        raise
    return runtime


def register_application_modules(runtime: AgentRuntime, *, question_topics) -> None:
    """Stage domain registrations independently; shared runtime creation still fails closed.

    Unavailable domains have no partial graphs/profiles. Their AI calls receive
    AgentCapabilityNotFoundError; ordinary data queries require no capability.
    HTTP readiness/error mapping belongs to the application's composition root.
    """
    groups = (
        ('analysis', (register_current_analysis_health_report,)),
        ('chat', (register_chat_reply, register_chat_suggestions)),
        (
            'health_profile',
            (register_profile_evidence, register_profile_chat_extraction, register_profile_record_extraction),
        ),
        ('report', (register_report_narrative, register_report_followup, register_report_retrieve_context)),
        ('record', (lambda staged: register_record_graphs(staged, question_topics=question_topics),)),
    )
    for name, registrations in groups:

        def configure(staged, functions=registrations):
            for register in functions:
                register(staged)

        if not runtime.install_module(name, configure):
            logger.warning('Agent module %s unavailable: %s', name, runtime.module_statuses[name]['error'])
