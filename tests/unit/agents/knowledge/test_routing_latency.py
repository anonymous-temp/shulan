"""Slow optional retrieval must not indefinitely delay interactive answers."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from agents.errors import AionGatewayError
from agents.knowledge import KnowledgeDocument
from agents.knowledge import KnowledgeQuery
from agents.knowledge import KnowledgeSource
from agents.knowledge import create_knowledge_route_profile
from agents.knowledge import routing
from agents.knowledge.errors import KnowledgeRetrievalError
from agents.knowledge.types import KnowledgeRetrievalPlan
from tests.unit._support.agents import FakeKnowledgeGateway
from tests.unit._support.agents import FakeLLMGateway


async def never_finishes(*_args):
    await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['timeout', 'upstream'])
async def test_route_failure_uses_graph_degradation_contract(monkeypatch, failure):
    gateway = FakeLLMGateway()
    gateway.complete = AsyncMock(
        side_effect=never_finishes if failure == 'timeout' else AionGatewayError('unavailable')
    )
    monkeypatch.setattr(routing, 'KNOWLEDGE_ROUTE_TIMEOUT_SECONDS', 0.01)

    with pytest.raises(KnowledgeRetrievalError) as raised:
        await asyncio.wait_for(
            routing.plan_knowledge_retrieval(
                gateway=gateway,
                profile=create_knowledge_route_profile(),
                scenario='chat',
                content='血压管理指南',
            ),
            timeout=1,
        )

    assert isinstance(raised.value.__cause__, TimeoutError if failure == 'timeout' else AionGatewayError)


@pytest.mark.asyncio
async def test_query_timeout_keeps_successful_parallel_result(monkeypatch):
    document = KnowledgeDocument(source=KnowledgeSource.GUIDE, title='指南', content='可用资料')
    cancelled = asyncio.Event()
    gateway = FakeKnowledgeGateway()

    async def retrieve(query):
        if query.source == KnowledgeSource.GUIDE:
            return (document,)
        try:
            await never_finishes()
        finally:
            cancelled.set()

    gateway.retrieve = retrieve
    monkeypatch.setattr(routing, 'KNOWLEDGE_QUERY_TIMEOUT_SECONDS', 0.01)
    plan = KnowledgeRetrievalPlan(
        (
            KnowledgeQuery(source=KnowledgeSource.GUIDE, query='血压管理'),
            KnowledgeQuery(source=KnowledgeSource.LITERATURE, query='血压管理'),
        )
    )

    results = await asyncio.wait_for(routing.retrieve_knowledge(gateway, plan), timeout=1)

    assert results == (document,)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_all_queries_timeout_uses_existing_unavailable_path(monkeypatch):
    gateway = FakeKnowledgeGateway()
    gateway.retrieve = AsyncMock(side_effect=never_finishes)
    monkeypatch.setattr(routing, 'KNOWLEDGE_QUERY_TIMEOUT_SECONDS', 0.01)
    plan = KnowledgeRetrievalPlan((KnowledgeQuery(source=KnowledgeSource.GUIDE, query='血压管理'),))

    with pytest.raises(KnowledgeRetrievalError):
        await asyncio.wait_for(routing.retrieve_knowledge(gateway, plan), timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['route', 'search'])
async def test_caller_cancellation_is_not_converted_to_fallback(operation):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def wait_for_cancel(*_args):
        started.set()
        try:
            await never_finishes()
        finally:
            stopped.set()

    if operation == 'route':
        gateway = FakeLLMGateway()
        gateway.complete = wait_for_cancel
        work = routing.plan_knowledge_retrieval(
            gateway=gateway,
            profile=create_knowledge_route_profile(),
            scenario='chat',
            content='血压管理',
        )
    else:
        gateway = FakeKnowledgeGateway()
        gateway.retrieve = wait_for_cancel
        work = routing.retrieve_knowledge(
            gateway, KnowledgeRetrievalPlan((KnowledgeQuery(source=KnowledgeSource.GUIDE, query='血压管理'),))
        )
    task = asyncio.create_task(work)
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
