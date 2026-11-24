"""Offline real AION round trips; no external provider, Mongo or callback."""

import json
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agents.knowledge import KnowledgeDocument
from agents.knowledge import KnowledgeSource
from agents.llm import ContextPolicy
from agents.llm import ModelProfile
from agents.tools.ledger import ToolRunLedger
from modules.report.research import ResearchAgent
from modules.report.research import ResearchPolicy
from modules.report.research import original_records
from modules.report.research import validate_result
from tests.unit._support.agents.tool_runs import MemoryToolRunRepository
from tests.unit.agents.llm.test_tool_run_gateway import Wire
from tests.unit.agents.llm.test_tool_run_gateway import packet


class Journal:
    def __init__(self):
        self.rows = {}
        self.calls = {}
        self.arguments = {}

    async def admit(self, key, plan, deadline):
        if key in self.rows:
            assert self.rows[key]['plan'] == plan
        else:
            self.rows[key] = {'plan': deepcopy(plan), 'deadlineAt': deadline}
        return deepcopy(self.rows[key])

    async def save_evidence(self, key, call, arguments, rows):
        self.calls[key, call] = deepcopy(rows)
        self.arguments[key, call] = deepcopy(arguments)

    async def read_evidence(self, key):
        return [r for (k, _), rows in self.calls.items() if k == key for r in deepcopy(rows)]

    async def read_calls(self, key):
        return [
            {'callId': call, **self.arguments[key, call], 'originalCount': len(rows)}
            for (k, call), rows in self.calls.items()
            if k == key
        ]


def original(text='Original abstract with an independently observed result.'):
    return KnowledgeDocument(
        KnowledgeSource.LITERATURE,
        'Source title',
        'AI summary MUST NOT be used',
        'https://source.invalid/1',
        {'id': 'paper-1', 'sourceTexts': [{'kind': 'abstract', 'text': text}]},
    )


def topic(identity='topic-1'):
    return SimpleNamespace(topic_id=identity, title='Fixed question', query_term='fixed topic')


class Knowledge:
    enabled = True

    def __init__(self, *pages):
        self.pages, self.queries = list(pages), []

    async def retrieve(self, query):
        self.queries.append(query)
        return self.pages.pop(0)


def setup(wire, knowledge, policy=None):
    ledger = ToolRunLedger(MemoryToolRunRepository())
    gateway = wire.gateway(ledger)
    agent = ResearchAgent(
        gateway=gateway,
        ledger=ledger,
        store=Journal(),
        knowledge=knowledge,
        profile=ModelProfile('report.research', ContextPolicy.STATELESS, model='test-model'),
        policy=policy or ResearchPolicy(True, 4, 3, 30, evaluation_reference='offline-fixture-v1'),
        model_identity=('deepseek', 'test-model'),
    )
    return agent


async def run(agent, topics=None):
    return await agent.research(
        user_id='u',
        session_id='s',
        report_id='report-1',
        topics=topics if topics is not None else [topic()],
        deadline_at=time.time() + 60,
    )


@pytest.mark.asyncio
async def test_observe_empty_requery_validate_originals_and_replay_without_extra_calls():
    doc = original()
    ref = original_records(doc, 'topic-1', 'search-2')[0].evidence_key
    final = {'selected': [{'topicId': 'topic-1', 'evidenceRefs': [ref]}], 'gaps': []}
    args = {'topicId': 'topic-1', 'query': 'first query', 'source': 'literature'}
    wire = Wire(
        packet(tool='knowledge_search', arguments=args, call_id='search-1'),
        packet(tool='knowledge_search', arguments={**args, 'query': 'broader query'}, call_id='search-2'),
        packet(text=json.dumps(final)),
    )
    knowledge = Knowledge((), (doc,))
    agent = setup(wire, knowledge)
    try:
        result = await run(agent)
        assert result['status'] == 'completed'
        assert result['records'][0]['abstract'] == doc.metadata['sourceTexts'][0]['text']
        assert result['usage']['totalTokens'] == 36 and result['usageComplete']
        assert all(q.include_source_text for q in knowledge.queries)
        assert 'AI summary MUST NOT' not in json.dumps(wire.requests)
        assert 'private draft' not in json.dumps(result)
        assert 'gap' in json.dumps(wire.requests[1]['messages'])
        assert await run(agent) == result
        assert len(wire.requests) == 3 and len(knowledge.queries) == 2
        root_doc = next(iter(agent.ledger.repository.documents.values()))
        root = next(v for v in root_doc['runs'].values() if v['context']['role'] == 'main')
        assert root['used_model'] == 3 and root['used_tool'] == 2
        assert root['reserved_model'] == 0
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_default_disabled_and_no_gap_make_no_runs_or_retrieval():
    wire, knowledge = Wire(), Knowledge()
    agent = setup(wire, knowledge, ResearchPolicy())
    assert (await run(agent))['status'] == 'disabled'
    enabled = setup(wire, knowledge)
    assert (await run(enabled, []))['status'] == 'covered'
    assert not agent.store.rows and not enabled.store.rows
    assert not wire.requests and not knowledge.queries


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'text',
    [
        '{"selected":[{"topicId":"topic-1","evidenceRefs":["invented"]}],"gaps":[]}',
        '{"selected":[],"gaps":[],"answer":"private medical draft"}',
        '{"selected":[],"gaps":[]}',
    ],
)
async def test_invalid_or_invented_final_never_enters_parent_evidence(text):
    wire = Wire(packet(text=text))
    agent = setup(wire, Knowledge())
    try:
        result = await run(agent)
        assert result['reason'] == 'invalid_result' and result['records'] == []
        assert result['gaps'] == ['topic-1']
        assert await run(agent) == result
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_tool_budget_exhaustion_is_partial_and_never_restarts():
    args = {'topicId': 'topic-1', 'query': 'query', 'source': 'literature'}
    wire = Wire(
        packet(tool='knowledge_search', arguments=args),
        packet(tool='knowledge_search', arguments=args, call_id='call-2'),
    )
    agent = setup(wire, Knowledge(()), ResearchPolicy(True, 2, 1, 30, evaluation_reference='fixture'))
    try:
        result = await run(agent)
        assert result['status'] == 'partial' and result['records'] == []
        assert len(agent.knowledge.queries) == 1
        assert (await run(agent))['records'] == []
        assert len(wire.requests) == 2
    finally:
        await wire.close()


def test_summary_only_oversized_and_wrong_kind_sources_are_not_originals():
    summary = KnowledgeDocument(KnowledgeSource.LITERATURE, 'Title', 'generated summary', metadata={})
    assert not original_records(summary, 't', 'c')
    assert not original_records(original('a' * 12001), 't', 'c')
    guide = KnowledgeDocument(
        KnowledgeSource.GUIDE,
        'Guideline',
        'summary',
        metadata={'id': 'g-1', 'sourceTexts': [{'kind': 'guideBlock', 'text': 'Literal guideline text.'}]},
    )
    record = original_records(guide, 't', 'c')[0]
    assert record.abstract is None and record.parent_guide_id == 'g-1'
    assert record.evidence_text == 'Literal guideline text.'


def test_evidence_from_other_topic_cannot_be_selected():
    record = original_records(original(), 'other', 'c')[0]
    rows = [{'topicId': 'other', 'evidenceRef': record.evidence_key, 'record': record.model_dump(by_alias=True)}]
    with pytest.raises(KeyError):
        validate_result(
            json.dumps({'selected': [{'topicId': 'topic-1', 'evidenceRefs': [record.evidence_key]}], 'gaps': []}),
            [{'topicId': 'topic-1'}],
            rows,
        )


def test_enabling_requires_budget_and_evaluation_record():
    with pytest.raises(ValueError):
        ResearchPolicy(enabled=True)
    with pytest.raises(ValueError):
        ResearchPolicy(True, 10, 3, 30, evaluation_reference='fixture')


@pytest.mark.asyncio
async def test_unattempted_topics_remain_visible_as_parent_gaps():
    wire = Wire(packet(text='{"selected":[],"gaps":["topic-1"]}'))
    agent = setup(wire, Knowledge(), ResearchPolicy(True, 2, 1, 30, max_topics=1, evaluation_reference='fixture'))
    try:
        result = await run(agent, [topic('topic-2'), topic('topic-1')])
        assert result['status'] == 'partial'
        assert result['gaps'] == ['topic-1', 'topic-2']
        assert 'topic-2' not in wire.requests[0]['messages'][-1]['content']
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_shorter_parent_deadline_never_reuses_longer_journal_budget():
    wire = Wire()
    agent = setup(wire, Knowledge())
    original_admit = agent.store.admit

    async def previously_admitted(key, plan, deadline):
        return await original_admit(key, plan, deadline + 60)

    agent.store.admit = previously_admitted
    result = await run(agent)
    assert result['reason'] == 'parent_deadline_shortened'
    assert not wire.requests and not agent.knowledge.queries


@pytest.mark.asyncio
async def test_model_drift_fails_before_native_model_dispatch():
    wire = Wire()
    agent = setup(wire, Knowledge())
    agent.gateway.gateway.request_model = lambda profile: ('deepseek', 'other-model')
    with pytest.raises(ValueError, match='manifest'):
        await run(agent)
    assert not wire.requests and not agent.knowledge.queries


@pytest.mark.asyncio
async def test_cancelled_parent_stops_child_and_does_not_publish_draft():
    import asyncio

    args = {'topicId': 'topic-1', 'query': 'query', 'source': 'literature'}
    wire = Wire(packet(tool='knowledge_search', arguments=args))
    agent = setup(wire, Knowledge())
    started = asyncio.Event()
    closed = []

    async def retrieve(query):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    agent.knowledge.retrieve = retrieve
    task = asyncio.create_task(run(agent))
    try:
        await asyncio.wait_for(started.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed
        assert not (await agent.store.read_evidence(next(iter(agent.store.rows))))
        assert (await run(agent))['status'] == 'partial'
        assert len(wire.requests) == 1
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_mutation_during_admission_does_not_change_dispatched_profile():
    wire = Wire(packet(text='{"selected":[],"gaps":["topic-1"]}'))
    agent = setup(wire, Knowledge())
    admit = agent.store.admit

    async def mutate(key, plan, deadline):
        result = await admit(key, plan, deadline)
        agent.profile.model_kwargs['temperature'] = 0.9
        return result

    agent.store.admit = mutate
    try:
        result = await run(agent)
        assert result['gaps'] == ['topic-1'] and result.get('reason') is None
        assert wire.requests[0].get('temperature') != 0.9
        assert not next(iter(agent.store.rows.values()))['plan']['manifest']['modelProfile']['model_kwargs']
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_invalid_tool_scope_never_reaches_knowledge():
    wire = Wire(
        packet(
            tool='knowledge_search',
            arguments={'topicId': 'foreign-topic', 'query': 'ignore all restrictions', 'source': 'literature'},
        ),
        packet(text='{"selected":[],"gaps":["topic-1"]}'),
    )
    agent = setup(wire, Knowledge())
    try:
        result = await run(agent)
        assert not agent.knowledge.queries and not result['records']
    finally:
        await wire.close()


def test_default_production_research_does_not_touch_runtime_or_databases(monkeypatch):
    from adapters.report.research import create_application_report_research
    from config import env

    monkeypatch.setattr(env, 'REPORT_RESEARCH_ENABLED', False)
    assert create_application_report_research(object()) is None


def test_invalid_enabled_production_budget_fails_before_model_or_storage(monkeypatch):
    from adapters.report.research import create_application_report_research
    from config import env

    monkeypatch.setattr(env, 'REPORT_RESEARCH_ENABLED', True)
    monkeypatch.setattr(env, 'REPORT_RESEARCH_MAX_MODEL_CALLS', 0)
    agent = create_application_report_research(object())
    assert agent.manifest['availability'] == 'unavailable'
    assert agent.manifest['reasonType'] == 'ValueError'
    with pytest.raises(ValueError, match='unavailable'):
        agent.ensure_available()
