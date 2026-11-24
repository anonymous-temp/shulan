"""Report parent keeps candidate and terminal-output responsibilities."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.report.evidence import EvidenceQuery
from modules.report.research import original_records
from modules.report.research_integration import supplement_evidence
from tests.unit.report.test_research_agent import original


def plan():
    return SimpleNamespace(
        topics=(SimpleNamespace(topic_id='topic-1', query_enabled=True),),
        queries=(EvidenceQuery('original plan query', ('topic-1',), (), intent='gut_function', target='function'),),
    )


@pytest.mark.asyncio
async def test_parent_observes_research_originals_and_audits_actual_query():
    record = original_records(original(), 'topic-1', 'actual-call-1')[0]
    research = SimpleNamespace(
        research=AsyncMock(
            return_value={
                'records': [record.model_dump(by_alias=True)],
                'gaps': [],
                'queries': [
                    {
                        'callId': 'actual-call-1',
                        'topicId': 'topic-1',
                        'query': 'revised model query',
                        'source': 'literature',
                        'originalCount': 1,
                    }
                ],
            }
        )
    )
    result = await supplement_evidence(
        research, plan(), SimpleNamespace(by_topic={}), scope=('u', 's', 'report-1'), deadline=time.monotonic() + 10
    )
    assert len(result.records) == 1
    audit = result.query_audits[0]
    assert audit.query_text == 'revised model query' and audit.query_id == 'actual-call-1'
    assert audit.public_count == audit.interpreted_count == 0
    assert audit.retained_count == 1
    assert research.research.await_args.kwargs['report_id'] == 'report-1'


@pytest.mark.asyncio
@pytest.mark.parametrize('covered,expired', [(True, False), (False, True)])
async def test_covered_or_expired_parent_never_dispatches(covered, expired):
    research = SimpleNamespace(research=AsyncMock())
    selected = SimpleNamespace(by_topic={'topic-1': ['evidence']} if covered else {})
    result = await supplement_evidence(
        research, plan(), selected, scope=('u', 's', 'r'), deadline=time.monotonic() + (-1 if expired else 30)
    )
    assert not result.records
    research.research.assert_not_awaited()


@pytest.mark.asyncio
async def test_research_failure_does_not_replace_existing_parent_candidates():
    research = SimpleNamespace(research=AsyncMock(side_effect=RuntimeError('offline failure')))
    result = await supplement_evidence(
        research, plan(), SimpleNamespace(by_topic={}), scope=('u', 's', 'r'), deadline=time.monotonic() + 10
    )
    assert not result.records and not result.query_audits
