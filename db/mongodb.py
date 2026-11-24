from database import mongodb

from config.env import MONGODB_COLL_AGENT_LLM_CONTEXTS
from config.env import MONGODB_COLL_AGENT_TOOL_RUNS
from config.env import MONGODB_COLL_CHAT_CUSTOM_MESSAGE_OUTBOX
from config.env import MONGODB_COLL_DOCUMENT_EXTRACTIONS
from config.env import MONGODB_COLL_DOCUMENT_INDEX
from config.env import MONGODB_COLL_DOCUMENT_OBSERVATIONS
from config.env import MONGODB_COLL_DOCUMENT_TRACE_CHUNKS
from config.env import MONGODB_COLL_DOCUMENT_TRACES
from config.env import MONGODB_COLL_HEALTH_PROFILE_EVENTS
from config.env import MONGODB_COLL_HEALTH_PROFILE_EVIDENCE_RESULTS
from config.env import MONGODB_COLL_HEALTH_PROFILE_FACTS
from config.env import MONGODB_COLL_HEALTH_PROFILE_JOBS
from config.env import MONGODB_COLL_HEALTH_PROFILE_SOURCE_PUBLICATIONS
from config.env import MONGODB_COLL_HEALTH_PROFILE_VIEWS
from config.env import MONGODB_COLL_HEALTH_REPORT_REVISIONS
from config.env import MONGODB_COLL_HEALTH_REPORTS
from config.env import MONGODB_COLL_HEALTH_SUBJECTS
from config.env import MONGODB_COLL_QUESTIONNAIRE_CHUNKS
from config.env import MONGODB_COLL_QUESTIONNAIRE_DOCUMENTS
from config.env import MONGODB_COLL_QUESTIONNAIRE_FACTOR_TASKS
from config.env import MONGODB_COLL_QUESTIONNAIRES
from config.env import MONGODB_COLL_REPORT_ARTIFACTS
from config.env import MONGODB_COLL_REPORT_BATCHES
from config.env import MONGODB_COLL_REPORT_CALLBACK_DELIVERIES
from config.env import MONGODB_COLL_SESSIONS
from config.env import MONGODB_DATABASE
from config.env import MONGODB_URI

__all__ = [
    'collection_sessions',
    'collection_questionnaires',
    'collection_questionnaire_documents',
    'collection_questionnaire_chunks',
    'collection_questionnaire_factor_tasks',
    'collection_health_reports',
    'collection_document_traces',
    'collection_document_index',
    'collection_document_trace_chunks',
    'collection_agent_llm_contexts',
    'collection_agent_tool_runs',
    'collection_chat_custom_message_outbox',
    'collection_health_subjects',
    'collection_health_profile_facts',
    'collection_health_profile_events',
    'collection_health_profile_views',
    'collection_health_profile_jobs',
    'collection_health_profile_source_publications',
    'collection_health_profile_evidence_results',
    'collection_report_batches',
    'collection_report_artifacts',
    'collection_report_callback_deliveries',
]

client = mongodb.Client.create_async(MONGODB_URI)
database = client[MONGODB_DATABASE]

collection_sessions = database[MONGODB_COLL_SESSIONS]
collection_health_reports = database[MONGODB_COLL_HEALTH_REPORTS]
collection_document_traces = database[MONGODB_COLL_DOCUMENT_TRACES]
collection_document_index = database[MONGODB_COLL_DOCUMENT_INDEX]
collection_document_extractions = database[MONGODB_COLL_DOCUMENT_EXTRACTIONS]
collection_document_observations = database[MONGODB_COLL_DOCUMENT_OBSERVATIONS]
collection_health_report_revisions = database[MONGODB_COLL_HEALTH_REPORT_REVISIONS]
collection_document_trace_chunks = database[MONGODB_COLL_DOCUMENT_TRACE_CHUNKS]
collection_agent_llm_contexts = database[MONGODB_COLL_AGENT_LLM_CONTEXTS]
collection_agent_tool_runs = database[MONGODB_COLL_AGENT_TOOL_RUNS]
collection_chat_custom_message_outbox = database[MONGODB_COLL_CHAT_CUSTOM_MESSAGE_OUTBOX]
collection_health_subjects = database[MONGODB_COLL_HEALTH_SUBJECTS]
collection_health_profile_facts = database[MONGODB_COLL_HEALTH_PROFILE_FACTS]
collection_health_profile_evidence_results = database[MONGODB_COLL_HEALTH_PROFILE_EVIDENCE_RESULTS]
collection_health_profile_events = database[MONGODB_COLL_HEALTH_PROFILE_EVENTS]
collection_health_profile_views = database[MONGODB_COLL_HEALTH_PROFILE_VIEWS]
collection_health_profile_jobs = database[MONGODB_COLL_HEALTH_PROFILE_JOBS]
collection_health_profile_source_publications = database[MONGODB_COLL_HEALTH_PROFILE_SOURCE_PUBLICATIONS]
collection_report_batches = database[MONGODB_COLL_REPORT_BATCHES]
collection_report_artifacts = database[MONGODB_COLL_REPORT_ARTIFACTS]
collection_report_callback_deliveries = database[MONGODB_COLL_REPORT_CALLBACK_DELIVERIES]

collection_questionnaires = database[MONGODB_COLL_QUESTIONNAIRES]
collection_questionnaire_documents = database[MONGODB_COLL_QUESTIONNAIRE_DOCUMENTS]
collection_questionnaire_chunks = database[MONGODB_COLL_QUESTIONNAIRE_CHUNKS]
collection_questionnaire_factor_tasks = database[MONGODB_COLL_QUESTIONNAIRE_FACTOR_TASKS]
