"""Outer Agent stream closure releases the workflow/graph's lease immediately."""

import asyncio
from types import SimpleNamespace

import pytest

from agents import AgentEvent
from agents import AgentEventType
from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents import AgentSelection
from agents import BoundAgentPort
from agents import GraphRegistry
from agents import WorkflowAgentPort


def port(kind, streamer):
    selection = AgentSelection('example.main', 'v1')
    if kind == 'workflow':
        return WorkflowAgentPort(lambda *args, **kwargs: None, selection, 'example', streamer=streamer)
    registry = GraphRegistry()
    registry.register(selection.capability, SimpleNamespace(astream=streamer), version='v1')
    return BoundAgentPort(AgentHub(registry), selection, 'example')


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['workflow', 'graph'])
async def test_outer_aclose_releases_inner_workflow_before_return(kind):
    closed = []

    async def events(request, context, **kwargs):
        try:
            yield AgentEvent(AgentEventType.RUN_STARTED, context.run_id, {})
            yield AgentEvent(AgentEventType.RUN_COMPLETED, context.run_id, {})
        finally:
            closed.append('released')

    context = AgentRunContext.create(user_id='u', session_id='s')
    stream = port(kind, events).stream(AgentRequest({}), context)
    await anext(stream)
    assert closed == []
    await stream.aclose()
    assert closed == ['released']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['workflow', 'graph'])
async def test_cancelled_stream_releases_inner_workflow(kind):
    entered, closed = asyncio.Event(), []

    async def events(request, context, **kwargs):
        try:
            entered.set()
            await asyncio.Event().wait()
            yield AgentEvent(AgentEventType.RUN_COMPLETED, context.run_id, {})
        finally:
            closed.append('released')

    context = AgentRunContext.create(user_id='u', session_id='s')
    stream = port(kind, events).stream(AgentRequest({}), context)
    read = asyncio.create_task(anext(stream))
    await entered.wait()
    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read
    assert closed == ['released']
