"""Transport faults cross an explicit adapter, not business implementation imports."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.health_analysis.knowledge import HealthAnalysisKnowledge
from agents.knowledge.errors import KnowledgeRetrievalError
from modules.health_analysis.contracts import AnalysisTimeoutError
from modules.health_analysis.contracts import KnowledgeUnavailableError
from utils.knowledge import KnowledgeAPIError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'error',
    [
        KnowledgeAPIError('invalid_response'),
        KnowledgeRetrievalError(1, 2),
        httpx.ConnectError('synthetic'),
        TimeoutError(),
        ConnectionError(),
    ],
)
async def test_expected_dependency_failures_are_typed_for_the_domain(error):
    gateway = SimpleNamespace(enabled=True, retrieve=AsyncMock(side_effect=error))
    adapter = HealthAnalysisKnowledge(gateway)
    assert adapter.enabled
    with pytest.raises(KnowledgeUnavailableError):
        await adapter.retrieve(object())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'error',
    [
        TypeError('synthetic program error'),
        PermissionError('synthetic scope error'),
        asyncio.CancelledError(),
        AnalysisTimeoutError(),
    ],
)
async def test_program_permission_cancellation_and_global_deadline_propagate(error):
    adapter = HealthAnalysisKnowledge(SimpleNamespace(enabled=True, retrieve=AsyncMock(side_effect=error)))
    with pytest.raises(type(error)) as captured:
        await adapter.retrieve(object())
    assert captured.value is error


@pytest.mark.asyncio
async def test_public_gateway_material_is_returned_unchanged():
    documents = (object(),)
    gateway = SimpleNamespace(enabled=True, retrieve=AsyncMock(return_value=documents))
    query = object()
    assert await HealthAnalysisKnowledge(gateway).retrieve(query) is documents
    gateway.retrieve.assert_awaited_once_with(query)
    assert not HealthAnalysisKnowledge(None).enabled
