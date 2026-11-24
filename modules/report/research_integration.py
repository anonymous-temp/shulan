"""Parent decision and observation boundary for optional Report research."""

import time

from modules.report.evidence import EvidenceRetrievalResult
from modules.report.research import ResearchUnavailableError
from modules.report.schemas import EvidenceChannelAudit
from modules.report.schemas import EvidenceQueryAudit
from modules.report.schemas import EvidenceRecord
from utils.log import getLogger

logger = getLogger(__name__)


async def supplement_evidence(agent, plan, selected, *, scope, deadline):
    verify = getattr(agent, 'ensure_available', None)
    if callable(verify):
        verify()
    empty = EvidenceRetrievalResult(records=[], failed_supports=set(), query_audits=[], relation_coverage=[])
    gaps = [topic for topic in plan.topics if topic.query_enabled and not selected.by_topic.get(topic.topic_id)]
    if not gaps or time.monotonic() >= deadline:
        return empty
    try:
        outcome = await agent.research(
            user_id=scope[0],
            session_id=scope[1],
            report_id=scope[2],
            topics=gaps,
            deadline_at=time.time() + max(0.0, deadline - time.monotonic()),
        )
        records = [EvidenceRecord.model_validate(row) for row in outcome.get('records', [])]
        allowed = {topic.topic_id for topic in gaps}
        if any(not row.supports or any(value not in allowed for value in row.supports) for row in records):
            raise ValueError('Research returned evidence outside delegated gaps')
        # Research audit remains private. These counters describe only original
        # candidates returned, never approved clinical claims or citations.
        audits = []
        for query in outcome.get('queries', []):
            planned = next((item for item in plan.queries if query['topicId'] in item.supports), None)
            if planned is None:
                continue
            related = [record for record in records if query['callId'] in record.query_ids]
            audits.append(
                EvidenceQueryAudit(
                    query_id=query['callId'],
                    query_text=query['query'],
                    intent=planned.intent,
                    target=planned.target,
                    screened_count=query['originalCount'],
                    retained_count=len(related),
                    channels=[
                        EvidenceChannelAudit(
                            channel=query['source'],
                            status='success',
                            provider='research_v1',
                        )
                    ],
                )
            )
        return EvidenceRetrievalResult(
            records=records, failed_supports=set(outcome.get('gaps', [])), query_audits=audits, relation_coverage=[]
        )
    except ResearchUnavailableError:
        raise
    except Exception as error:
        logger.warning(
            'Report research unavailable; preserving existing candidates: error_type=%s', type(error).__name__
        )
        return empty
