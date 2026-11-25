"""环境变量集中加载与校验。

1. 本模块是整个项目读取 .env 的唯一入口。
2. 其他模块请用 `from config.env import xxx` 取配置，不要再散落调用`os.getenv()`。
3. .env 文件中的环境变量名称统一使用 SHULAN_ 前缀，在本模块中去除该前缀使用。
    比如 LLM_PROVIDER=SHULAN_LLM_PROVIDER。
4. 必需项缺失时在导入阶段抛 :class:`EnvConfigError`，而不是等到真正使用时检查。
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from loongspears.utils.env import EnvReader
from loongspears.utils.env import load_env

from config.report_endpoints import default_evaluate_callback_url
from config.report_endpoints import validate_report_endpoint

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / '.env'

# 默认加载项目根目录.env文件 & 真实的系统环境变量优先
ENV_LOAD_RESULT = load_env(ENV_FILE, override=False, required=False)
_env = EnvReader(prefix='SHULAN_')

# Project
# ENVIRONMENT 可选 dev / test / production
# LOG_LEVEL 可选 DEBUG / INFO / WARNING / ERROR / CRITICAL
ENVIRONMENT: str = _env.get_choice(
    'ENVIRONMENT',
    ('dev', 'test', 'production'),
    default='dev',
)
DEBUG: bool = _env.get_bool('DEBUG', default=False)
LOG_LEVEL: str = _env.get_choice(
    'LOG_LEVEL',
    ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'),
    default='INFO',
)
LOG_FILE_ENABLED: bool = _env.get_bool('LOG_FILE_ENABLED', default=True)
LOG_DIR = Path(_env.optional('LOG_DIR', default='logs'))
if not LOG_DIR.is_absolute():
    LOG_DIR = PROJECT_ROOT / LOG_DIR
LOG_MAX_BYTES: int = _env.get_int('LOG_MAX_BYTES', default=52428800, min_value=1)
LOG_RETENTION_DAYS: int = _env.get_int('LOG_RETENTION_DAYS', default=14, min_value=1)
LOG_BACKUP_COUNT: int = _env.get_int('LOG_BACKUP_COUNT', default=20, min_value=1)
LOG_USER_MAX_OPEN_FILES: int = _env.get_int('LOG_USER_MAX_OPEN_FILES', default=128, min_value=1)
LOG_QUEUE_CAPACITY: int = _env.get_int('LOG_QUEUE_CAPACITY', default=1000, min_value=1)
SERVER_PORT: int = _env.get_int('SERVER_PORT', default=9002, min_value=1, max_value=65535)

# HTTP diagnostic previews only; these limits never restrict API payloads.
HTTP_LOG_BODY_ENABLED: bool = _env.get_bool('HTTP_LOG_BODY_ENABLED', default=True)
HTTP_LOG_STRING_CHARS: int = _env.get_int('HTTP_LOG_STRING_CHARS', default=2000, min_value=32, max_value=100000)
HTTP_LOG_LIST_ITEMS: int = _env.get_int('HTTP_LOG_LIST_ITEMS', default=20, min_value=2, max_value=1000)
HTTP_LOG_DICT_ITEMS: int = _env.get_int('HTTP_LOG_DICT_ITEMS', default=50, min_value=1, max_value=1000)
HTTP_LOG_DEPTH: int = _env.get_int('HTTP_LOG_DEPTH', default=8, min_value=1, max_value=32)
HTTP_LOG_TOTAL_BYTES: int = _env.get_int('HTTP_LOG_TOTAL_BYTES', default=32768, min_value=1024, max_value=1048576)
HTTP_LOG_PARSE_BYTES: int = _env.get_int('HTTP_LOG_PARSE_BYTES', default=1048576, min_value=1024, max_value=16777216)

# Chat / Record SSE text batching: send when size OR elapsed time reaches the limit.
SSE_TEXT_BATCH_CHARS: int = _env.get_int('SSE_TEXT_BATCH_CHARS', default=512, min_value=1)
SSE_TEXT_BATCH_SECONDS: float = _env.get(
    'SSE_TEXT_BATCH_SECONDS',
    parser=float,
    default=0.05,
    validator=lambda value: math.isfinite(value) and value > 0,
)

# LLM API KEY
LLM_PROVIDER: str = _env.optional('LLM_PROVIDER')
LLM_API_KEY: str = _env.required('LLM_API_KEY', sensitive=True)
LLM_DEFAULT_MODEL: str = _env.optional('LLM_DEFAULT_MODEL')

# 阿里云文档智能解析平台（体检报告解析）
DOCMIND_ACCESSKEY_ID: str = _env.required('ALIYUN_DOCMIND_ACCESSKEY_ID', sensitive=True)
DOCMIND_ACCESSKEY_SECRET: str = _env.required('ALIYUN_DOCMIND_ACCESSKEY_SECRET', sensitive=True)
DOCMIND_ENDPOINT: str = _env.required('ALIYUN_DOCMIND_ENDPOINT')

# MongoDB
MONGODB_URI: str = _env.required('MONGODB_URI', sensitive=True)
MONGODB_DATABASE: str = _env.required('MONGODB_DATABASE')
MONGODB_COLL_SESSIONS: str = _env.required('MONGODB_COLL_SESSIONS')
MONGODB_COLL_HEALTH_REPORTS: str = _env.required('MONGODB_COLL_HEALTH_REPORTS')
MONGODB_COLL_DOCUMENT_TRACES: str = _env.optional('MONGODB_COLL_DOCUMENT_TRACES', default='document_traces')
MONGODB_COLL_DOCUMENT_INDEX: str = _env.optional('MONGODB_COLL_DOCUMENT_INDEX', default='document_index')
MONGODB_COLL_DOCUMENT_EXTRACTIONS: str = _env.optional(
    'MONGODB_COLL_DOCUMENT_EXTRACTIONS', default='document_extractions'
)
MONGODB_COLL_DOCUMENT_OBSERVATIONS: str = _env.optional(
    'MONGODB_COLL_DOCUMENT_OBSERVATIONS', default='document_observations'
)
MONGODB_COLL_HEALTH_REPORT_REVISIONS: str = _env.optional(
    'MONGODB_COLL_HEALTH_REPORT_REVISIONS', default='health_report_revisions'
)
MONGODB_COLL_DOCUMENT_TRACE_CHUNKS: str = _env.optional(
    'MONGODB_COLL_DOCUMENT_TRACE_CHUNKS', default='document_trace_chunks'
)
MONGODB_COLL_AGENT_LLM_CONTEXTS: str = _env.optional('MONGODB_COLL_AGENT_LLM_CONTEXTS', default='agent_llm_contexts')
MONGODB_COLL_AGENT_TOOL_RUNS: str = _env.optional('MONGODB_COLL_AGENT_TOOL_RUNS', default='agent_tool_runs')
MONGODB_COLL_AGENT_CHECKPOINTS: str = _env.optional('MONGODB_COLL_AGENT_CHECKPOINTS', default='agent_checkpoints')
MONGODB_COLL_AGENT_CHECKPOINT_WRITES: str = _env.optional(
    'MONGODB_COLL_AGENT_CHECKPOINT_WRITES', default='agent_checkpoint_writes'
)
MONGODB_COLL_CHAT_CUSTOM_MESSAGE_OUTBOX: str = _env.optional(
    'MONGODB_COLL_CHAT_CUSTOM_MESSAGE_OUTBOX',
    default='chat_custom_message_outbox',
)
MONGODB_COLL_HEALTH_SUBJECTS: str = _env.optional(
    'MONGODB_COLL_HEALTH_SUBJECTS',
    default='health_subjects',
)
MONGODB_COLL_HEALTH_PROFILE_FACTS: str = _env.optional(
    'MONGODB_COLL_HEALTH_PROFILE_FACTS',
    default='health_profile_facts',
)
MONGODB_COLL_HEALTH_PROFILE_EVENTS: str = _env.optional(
    'MONGODB_COLL_HEALTH_PROFILE_EVENTS',
    default='health_profile_events',
)
MONGODB_COLL_HEALTH_PROFILE_VIEWS: str = _env.optional(
    'MONGODB_COLL_HEALTH_PROFILE_VIEWS',
    default='health_profile_views',
)
MONGODB_COLL_HEALTH_PROFILE_JOBS: str = _env.optional(
    'MONGODB_COLL_HEALTH_PROFILE_JOBS',
    default='health_profile_jobs',
)
MONGODB_COLL_HEALTH_PROFILE_SOURCE_PUBLICATIONS: str = _env.optional(
    'MONGODB_COLL_HEALTH_PROFILE_SOURCE_PUBLICATIONS',
    default='health_profile_source_publications',
)
MONGODB_COLL_HEALTH_PROFILE_EVIDENCE_RESULTS: str = _env.optional(
    'MONGODB_COLL_HEALTH_PROFILE_EVIDENCE_RESULTS',
    default='health_profile_evidence_results',
)
# 最终报告批次与五类产物的独立存储，与上传体检报告、健康事实和
# Agent checkpoint 分开；长期保留，不设置自动过期。
MONGODB_COLL_REPORT_BATCHES: str = _env.optional(
    'MONGODB_COLL_REPORT_BATCHES',
    default='report_batches',
)
MONGODB_COLL_REPORT_ARTIFACTS: str = _env.optional(
    'MONGODB_COLL_REPORT_ARTIFACTS',
    default='report_artifacts',
)
MONGODB_COLL_REPORT_CALLBACK_DELIVERIES: str = _env.optional(
    'MONGODB_COLL_REPORT_CALLBACK_DELIVERIES',
    default='report_callback_deliveries',
)

# Independent whole-questionnaire staging and immutable submitted sources.
# Validate against every Mongo collection already declared in this section.
# Excluding this module's own names also makes reloading configuration safe.
_existing_mongodb_collections = frozenset(
    value
    for name, value in globals().items()
    if name.startswith('MONGODB_COLL_') and not name.startswith('MONGODB_COLL_QUESTIONNAIRE')
)
MONGODB_COLL_QUESTIONNAIRES: str = _env.get(
    'MONGODB_COLL_QUESTIONNAIRES',
    parser=str.strip,
    default='record_questionnaires',
    validator=lambda value: bool(value) and value not in _existing_mongodb_collections,
)
MONGODB_COLL_QUESTIONNAIRE_DOCUMENTS: str = _env.get(
    'MONGODB_COLL_QUESTIONNAIRE_DOCUMENTS',
    parser=str.strip,
    default='questionnaire_documents',
    validator=lambda value: (
        bool(value) and value not in _existing_mongodb_collections and value != MONGODB_COLL_QUESTIONNAIRES
    ),
)
MONGODB_COLL_QUESTIONNAIRE_CHUNKS: str = _env.get(
    'MONGODB_COLL_QUESTIONNAIRE_CHUNKS',
    parser=str.strip,
    default='questionnaire_document_chunks',
    validator=lambda value: (
        bool(value)
        and value not in _existing_mongodb_collections
        and value not in (MONGODB_COLL_QUESTIONNAIRES, MONGODB_COLL_QUESTIONNAIRE_DOCUMENTS)
    ),
)

MONGODB_COLL_QUESTIONNAIRE_FACTOR_TASKS: str = _env.get(
    'MONGODB_COLL_QUESTIONNAIRE_FACTOR_TASKS',
    parser=str.strip,
    default='questionnaire_factor_tasks',
    validator=lambda value: (
        bool(value)
        and value not in _existing_mongodb_collections
        and value
        not in (MONGODB_COLL_QUESTIONNAIRES, MONGODB_COLL_QUESTIONNAIRE_DOCUMENTS, MONGODB_COLL_QUESTIONNAIRE_CHUNKS)
    ),
)

MONGODB_COLL_HEALTH_ANALYSIS_RUNS: str = _env.get(
    'MONGODB_COLL_HEALTH_ANALYSIS_RUNS',
    parser=str.strip,
    default='health_analysis_runs',
    validator=lambda value: (
        bool(value)
        and value
        not in {
            item
            for name, item in globals().items()
            if name.startswith('MONGODB_COLL_') and name != 'MONGODB_COLL_HEALTH_ANALYSIS_RUNS'
        }
    ),
)

# Redis
# REDIS_MODE 可选 single / sentinel / cluster
# REDIS_PASSWORD 空密码等同于未设置，统一返回 None
REDIS_HOST: str = _env.optional('REDIS_HOST', default='localhost')
REDIS_PORT: int = _env.get_int('REDIS_PORT', default=6379, min_value=1, max_value=65535)
REDIS_MODE: str = _env.get_choice('REDIS_MODE', ('single', 'sentinel', 'cluster'), default='single')
REDIS_DB: int = _env.get_int('REDIS_DB', default=0, min_value=0)
REDIS_PASSWORD: str | None = _env.optional(
    'REDIS_PASSWORD',
    default=None,
    empty_as_default=True,
    sensitive=True,
)
REDIS_PROTOCOL_VERSION: int = _env.get_int('REDIS_PROTOCOL_VERSION', default=2)

# Agent runtime
# 必须大于单次模型最长执行时间；后续接入运行时心跳后仍作为失联保护上限。
AGENT_LEASE_TTL_SECONDS: int = _env.get_int('AGENT_LEASE_TTL_SECONDS', default=300, min_value=1)

# Agent 医学知识库检索。密钥为空时 KnowledgeGateway 禁用，Chat/Record
# 不执行检索路由和外部 HTTP 请求。
KNOWLEDGE_API_BASE_URL: str = _env.optional(
    'KNOWLEDGE_API_BASE_URL',
    default=('https://www.evimed.com/api-evimed/medicine-api/ai-api/review/api/v2'),
)
KNOWLEDGE_API_KEY: str = _env.optional(
    'KNOWLEDGE_API_KEY',
    default='',
    sensitive=True,
)
KNOWLEDGE_API_TIMEOUT_SECONDS: int = _env.get_int(
    'KNOWLEDGE_API_TIMEOUT_SECONDS',
    default=20,
    min_value=1,
    max_value=120,
)


# 备用群聊查询工具；完整接口地址由部署方填写，不影响未使用该工具的业务启动。
CHAT_HISTORY_QUERY_URL: str = _env.optional('CHAT_HISTORY_QUERY_URL', default='', sensitive=True)


# 最终报告持久回调。间隔按失败次数依次使用，超过序列后重复最后一项。
def _parse_report_callback_delays(raw: str) -> tuple[int, ...]:
    delays = tuple(int(value.strip()) for value in raw.split(','))
    if not delays or any(not 1 <= value <= 31536000 for value in delays):
        raise ValueError('回调重试间隔须为逗号分隔的正整数秒数，单项不超过一年')
    if any(right < left for left, right in zip(delays, delays[1:])):
        raise ValueError('回调重试间隔须按非递减顺序配置')
    return delays


REPORT_CALLBACK_URL: str = (
    _env.get(
        'REPORT_CALLBACK_URL',
        parser=lambda value: validate_report_endpoint(value, setting='REPORT_CALLBACK_URL', environment=ENVIRONMENT),
        sensitive=True,
    )
    or ''
)
REPORT_EVALUATE_CALLBACK_URL: str = _env.get(
    'REPORT_EVALUATE_CALLBACK_URL',
    parser=lambda value: validate_report_endpoint(
        value,
        setting='REPORT_EVALUATE_CALLBACK_URL',
        environment=ENVIRONMENT,
    ),
    default='',
    sensitive=True,
) or default_evaluate_callback_url(REPORT_CALLBACK_URL)
REPORT_CALLBACK_RETRY_DELAYS_SECONDS: tuple[int, ...] = _env.get(
    'REPORT_CALLBACK_RETRY_DELAYS_SECONDS',
    parser=_parse_report_callback_delays,
    default=(30, 120, 600, 3600),
)
REPORT_CALLBACK_POLL_INTERVAL_SECONDS: int = _env.get_int(
    'REPORT_CALLBACK_POLL_INTERVAL_SECONDS',
    default=5,
    min_value=1,
    max_value=3600,
)
REPORT_CALLBACK_CONCURRENCY: int = _env.get_int(
    'REPORT_CALLBACK_CONCURRENCY',
    default=4,
    min_value=1,
    max_value=64,
)
# 回调领取租约为 120 秒；网络请求最多使用一半，留出读取正文和确认写入的余量。
REPORT_CALLBACK_TIMEOUT_SECONDS: int = _env.get_int(
    'REPORT_CALLBACK_TIMEOUT_SECONDS',
    default=15,
    min_value=1,
    max_value=60,
)
REPORT_CALLBACK_BATCH_SIZE: int = _env.get_int(
    'REPORT_CALLBACK_BATCH_SIZE',
    default=100,
    min_value=1,
    max_value=10000,
)
REPORT_CALLBACK_RETRY_JITTER_RATIO: float = _env.get(
    'REPORT_CALLBACK_RETRY_JITTER_RATIO',
    parser=float,
    default=0.1,
    validator=lambda value: 0 <= value < 1,
)
REPORT_CALLBACK_BACKLOG_LOG_INTERVAL_SECONDS: int = _env.get_int(
    'REPORT_CALLBACK_BACKLOG_LOG_INTERVAL_SECONDS',
    default=300,
    min_value=1,
    max_value=86400,
)
REPORT_GENERATION_TIMEOUT_SECONDS: int = _env.get_int(
    'REPORT_GENERATION_TIMEOUT_SECONDS',
    default=1800,
    min_value=300,
    max_value=1800,
)

# Optional bounded research. Enabled runs require explicit budgets and a reviewed
# evaluation reference; the default leaves the existing generation path intact.
REPORT_RESEARCH_ENABLED: bool = _env.get_bool('REPORT_RESEARCH_ENABLED', default=False)
REPORT_RESEARCH_MAX_MODEL_CALLS: int = _env.get_int(
    'REPORT_RESEARCH_MAX_MODEL_CALLS', default=0, min_value=0, max_value=8
)
REPORT_RESEARCH_MAX_TOOL_CALLS: int = _env.get_int(
    'REPORT_RESEARCH_MAX_TOOL_CALLS', default=0, min_value=0, max_value=8
)
REPORT_RESEARCH_TIMEOUT_SECONDS: int = _env.get_int(
    'REPORT_RESEARCH_TIMEOUT_SECONDS', default=0, min_value=0, max_value=120
)
REPORT_RESEARCH_EVALUATION_REFERENCE: str = _env.optional('REPORT_RESEARCH_EVALUATION_REFERENCE', default='')
REPORT_RESEARCH_MODEL: str = _env.optional('REPORT_RESEARCH_MODEL', default='')

# Evaluate report authoring. 'case' (default) lets the model analyse the whole
# case once and write the six chapters by section, with the per-chapter path
# as its fallback. 'chapter' writes and reviews related cells together with
# the full record and evidence; 'cell' is the former per-cell path.
REPORT_AUTHORING_MODE: str = _env.get_choice('REPORT_AUTHORING_MODE', ('case', 'chapter', 'cell'), default='case')
# Thinking mode for chapter writing and review (other report tasks stay non-thinking).
REPORT_NARRATIVE_THINKING: bool = _env.get_bool('REPORT_NARRATIVE_THINKING', default=True)
REPORT_NARRATIVE_REASONING_EFFORT: str = _env.get_choice(
    'REPORT_NARRATIVE_REASONING_EFFORT', ('low', 'high', 'max'), default='high')
# Web evidence channel (DeepSeek Search/Fetch). The tool key defaults to the
# model key when the configured provider is DeepSeek.
REPORT_WEB_EVIDENCE_ENABLED: bool = _env.get_bool('REPORT_WEB_EVIDENCE_ENABLED', default=True)
DEEPSEEK_TOOLS_API_KEY: str = _env.optional('DEEPSEEK_TOOLS_API_KEY', default='', sensitive=True)
if not DEEPSEEK_TOOLS_API_KEY and (LLM_PROVIDER or '').strip().casefold() == 'deepseek':
    DEEPSEEK_TOOLS_API_KEY = LLM_API_KEY
# OCR fallback for scanned or image uploads in the report source reader.
REPORT_DOCUMENT_OCR_ENABLED: bool = _env.get_bool('REPORT_DOCUMENT_OCR_ENABLED', default=True)

REPORT_PRODUCT_URL: str = (
    _env.get(
        'REPORT_PRODUCT_URL',
        parser=lambda value: validate_report_endpoint(value, setting='REPORT_PRODUCT_URL', environment=ENVIRONMENT),
        sensitive=True,
    )
    or ''
)

# Optional production product catalog override; legacy name remains compatible.
# Gut-barrier is always enabled and uses REPORT_CALLBACK_URL without an override.
GUT_BARRIER_PRODUCT_URL: str = _env.optional(
    'GUT_BARRIER_PRODUCT_URL',
    default=os.getenv('GUT_BARRIER_PRODUCT_URL', REPORT_PRODUCT_URL).strip(),
    sensitive=True,
)
GUT_BARRIER_PRODUCT_URL = _env.get(
    'GUT_BARRIER_PRODUCT_URL',
    parser=str.strip,
    default=GUT_BARRIER_PRODUCT_URL,
    validator=lambda value: (
        validate_report_endpoint(
            value,
            setting='GUT_BARRIER_PRODUCT_URL',
            environment=ENVIRONMENT,
            allow_empty=True,
        )
        is not None
    ),
    sensitive=True,
)

# EviMed evidence retrieval for microbiome report interpretation
EVIMED_API_URL: str = _env.optional(
    'EVIMED_API_URL',
    default=os.getenv(
        'EVIDENCE_API_URL',
        'https://www.evimed.com/api-evimed/medicine-api/ai-api',
    ),
)
EVIMED_API_KEY: str = _env.optional(
    'EVIMED_API_KEY',
    default=os.getenv('EVIMED_API_KEY', ''),
    sensitive=True,
)
EVIMED_TIMEOUT: int = _env.get_int(
    'EVIMED_TIMEOUT',
    default=20,
    min_value=1,
    max_value=120,
)

# Optional public-evidence fallback. NCBI permits access without an API key;
# configuring a key/email raises the official rate limit and identifies the
# calling application. Crossref mailto enables its polite pool.
NCBI_API_KEY: str = _env.optional('NCBI_API_KEY', default='', sensitive=True)
NCBI_CONTACT_EMAIL: str = _env.optional('NCBI_CONTACT_EMAIL', default='')
CROSSREF_MAILTO: str = _env.optional('CROSSREF_MAILTO', default='')

# 所有变量读取完毕后一次性报告全部问题。
_env.raise_for_errors()


def summary() -> str:
    """返回已读取配置的脱敏摘要，供启动日志使用。"""
    return _env.summary()
