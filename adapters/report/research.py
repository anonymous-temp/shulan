"""Production wiring for the opt-in Report research unit."""

from agents.llm import ContextPolicy
from agents.llm import ModelProfile
from agents.llm.tool_gateway import AionToolRunGateway
from agents.persistence.tool_runs import MongoToolRunRepository
from agents.tools.ledger import ToolRunLedger
from modules.report.research import ResearchAgent
from modules.report.research import ResearchPolicy
from modules.report.research import UnavailableResearchAgent
from utils.log import getLogger

from .research_store import MongoResearchStore

logger = getLogger(__name__)


def create_application_report_research(runtime):
    from config import env

    if not env.REPORT_RESEARCH_ENABLED:
        return None
    try:
        return _create_research(runtime, env)
    except Exception as error:
        # Research owns no shared resource lifecycle. Keep its failure explicit
        # so dependent AI admission fails while the application can still start.
        logger.warning('Report research configuration unavailable: error_type=%s', type(error).__name__)
        return UnavailableResearchAgent(type(error).__name__)


def _create_research(runtime, env):
    policy = ResearchPolicy(
        enabled=True,
        max_model_calls=env.REPORT_RESEARCH_MAX_MODEL_CALLS,
        max_tool_calls=env.REPORT_RESEARCH_MAX_TOOL_CALLS,
        timeout_seconds=env.REPORT_RESEARCH_TIMEOUT_SECONDS,
        evaluation_reference=env.REPORT_RESEARCH_EVALUATION_REFERENCE,
    )
    if not env.REPORT_RESEARCH_MODEL.strip() or not runtime.knowledge_gateway.enabled:
        raise ValueError('Enabled Report research requires an explicit model and available Knowledge gateway')
    from pymongo.write_concern import WriteConcern

    from db.mongodb import collection_agent_tool_runs
    from db.mongodb import collection_report_batches

    concern = WriteConcern(w='majority', j=True, wtimeout=5000)
    collection = collection_report_batches.database[collection_report_batches.name + '_research']
    ledger = ToolRunLedger(MongoToolRunRepository(collection_agent_tool_runs.with_options(write_concern=concern)))
    profile = ModelProfile(
        'report.research', ContextPolicy.STATELESS, model=env.REPORT_RESEARCH_MODEL, capture_transport_metrics=True
    )
    return ResearchAgent(
        gateway=AionToolRunGateway(runtime.llm_gateway, ledger),
        ledger=ledger,
        store=MongoResearchStore(collection.with_options(write_concern=concern)),
        knowledge=runtime.knowledge_gateway,
        profile=profile,
        policy=policy,
        model_identity=runtime.llm_gateway.request_model(profile),
    )
