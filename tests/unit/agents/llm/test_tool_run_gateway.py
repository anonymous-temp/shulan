"""Real AION 0.6 tool protocol and SDK, exclusively through an offline HTTP transport."""

import asyncio
import json
import time
from dataclasses import replace

import aion
import httpx
import pytest
from openai import AsyncOpenAI

from agents.llm import AionLLMGateway
from agents.llm import ContextPolicy
from agents.llm import LLMRequest
from agents.llm import ModelProfile
from agents.llm.tool_gateway import AionToolRunGateway
from agents.tools import ToolBudget
from agents.tools import ToolDefinition
from agents.tools import ToolOutcome
from agents.tools import ToolRunContext
from agents.tools import ToolRunRequest
from agents.tools.contracts import ToolRunConflict
from agents.tools.ledger import ToolRunLedger
from tests.unit._support.agents.tool_runs import MemoryToolRunRepository


def packet(*, tool=None, arguments=None, call_id='call-1', text='final answer', usage=True):
    base = {'id': 'offline', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'test-model'}

    def chunk(delta, finish=None):
        return {**base, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}

    events = [chunk({'content': 'private draft', 'reasoning_content': 'private reasoning'})]
    if tool:
        encoded = json.dumps(arguments or {'query': 'evidence'})
        events.extend(
            [
                chunk(
                    {
                        'tool_calls': [
                            {
                                'index': 0,
                                'id': call_id,
                                'type': 'function',
                                'function': {'name': tool, 'arguments': encoded[:4]},
                            }
                        ]
                    }
                ),
                chunk({'tool_calls': [{'index': 0, 'function': {'arguments': encoded[4:]}}]}),
                chunk({}, 'tool_calls'),
            ]
        )
    else:
        events = [chunk({'content': text}), chunk({}, 'stop')]
    if usage:
        events.append(
            {**base, 'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}}
        )
    return ''.join('data: ' + json.dumps(event) + '\n\n' for event in events) + 'data: [DONE]\n\n'


class Wire:
    def __init__(self, *packets):
        self.packets = list(packets)
        self.requests = []
        self.clients = []
        self.sdks = []

    def factory(self, provider, **options):
        client = aion.create_chat_client(provider, **options)
        self.clients.append(client)
        self.sdks.append(client.async_client)
        client.async_client = AsyncOpenAI(
            api_key='offline',
            base_url='https://offline.invalid',
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.respond)),
        )
        return client

    def respond(self, request):
        self.requests.append(json.loads(request.content))
        assert self.packets, 'Unexpected extra model dispatch'
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=self.packets.pop(0))

    def gateway(self, ledger=None, **kwargs):
        llm = AionLLMGateway(
            provider='deepseek', api_key='offline', default_model='test-model', client_factory=self.factory
        )
        return AionToolRunGateway(llm, ledger or ToolRunLedger(MemoryToolRunRepository()), poll_seconds=0.01, **kwargs)

    async def close(self):
        for client in self.clients:
            client.client.close()
            await client.async_client.close()
        for sdk in self.sdks:
            await sdk.close()


def request(handler=None, *, effect='read', max_model=3, max_tool=2):
    definition = ToolDefinition(
        'retrieve',
        'v1',
        'Retrieve evidence',
        {
            'type': 'object',
            'properties': {'query': {'type': 'string'}},
            'required': ['query'],
            'additionalProperties': False,
        },
        handler or (lambda arguments, context: {'evidence': arguments['query']}),
        effect=effect,
    )
    return ToolRunRequest(
        LLMRequest(ModelProfile('research', ContextPolicy.STATELESS), 'Find evidence'),
        ToolRunContext('user', 'root', 'root', 'report', subject_id='subject'),
        ToolBudget(max_model, max_tool, time.time() + 60),
        (definition,),
        frozenset({definition.identity}),
    )


@pytest.mark.asyncio
async def test_native_round_trip_aggregates_usage_hides_drafts_and_replays_final():
    calls = []

    async def retrieve(arguments, context):
        calls.append((arguments, context))
        return ToolOutcome({'evidence': 'grounded source'}, 'document:revision-1')

    wire = Wire(packet(tool='retrieve'), packet())
    gateway = wire.gateway()
    task = request(retrieve)
    try:
        events = [event async for event in gateway.stream_events(task)]
        result = events[-1].result
        assert result.status == 'completed'
        assert result.result.content == 'final answer'
        assert result.usage.total_tokens == 24
        assert result.usage_complete
        assert 'private draft' not in repr(events)
        assert 'private reasoning' not in repr(events)
        assert [event.kind for event in events] == [
            'model_done',
            'tool_started',
            'tool_finished',
            'model_done',
            'completed',
        ]
        assert len(calls) == 1
        assert calls[0][1].call_id == 'call-1'
        assert calls[0][1].tool_version == 'v1'
        assert calls[0][1].run.subject_id == 'subject'
        assert wire.requests[1]['messages'][-1]['tool_call_id'] == 'call-1'
        assert 'grounded source' in wire.requests[1]['messages'][-1]['content']
        assert (await gateway.run(task)).result == result.result
        assert len(wire.requests) == 2
        with pytest.raises(ToolRunConflict):
            await gateway.run(
                replace(
                    task, tools=(replace(task.tools[0], version='v2'),), allowed_tools=frozenset({('retrieve', 'v2')})
                )
            )
    finally:
        await wire.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('max_model,max_tool,models,tools', [(1, 2, 1, 1), (3, 0, 1, 0), (3, 1, 2, 1)])
async def test_hard_dispatch_limits_stop_native_loop(max_model, max_tool, models, tools):
    executed = []
    wire = Wire(packet(tool='retrieve'), packet(tool='retrieve', call_id='call-2'), packet())
    gateway = wire.gateway()
    task = request(
        lambda args, context: executed.append(context.call_id) or 'ok', max_model=max_model, max_tool=max_tool
    )
    try:
        result = await gateway.run(task)
        assert result.status == 'failed'
        assert result.result is None
        assert len(wire.requests) == models
        assert len(executed) == tools
        assert not result.usage_complete
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_authorization_is_checked_before_handler_dispatch():
    executed = []
    wire = Wire(packet(tool='retrieve'))
    gateway = wire.gateway(authorize=lambda definition, args, context: False)
    try:
        result = await gateway.run(request(lambda args, context: executed.append(args)))
        assert result.status == 'failed'
        assert not executed
        assert result.reason == 'ToolExecutionError'
        assert result.receipts[-1]['status'] == 'failed'
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_command_without_owner_receipt_is_unknown_and_never_replayed():
    executed = []
    wire = Wire(packet(tool='retrieve'))
    gateway = wire.gateway()
    task = request(lambda args, context: executed.append(context.idempotency_key) or 'changed', effect='command')
    try:
        result = await gateway.run(task)
        assert result.status == 'interrupted'
        assert any(receipt['status'] == 'unknown' for receipt in result.receipts)
        assert (await gateway.run(task)).status == 'interrupted'
        assert len(executed) == len(wire.requests) == 1
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_running_tool_cancellation_records_unknown_before_return():
    started = asyncio.Event()

    async def tool(args, context):
        started.set()
        await asyncio.Event().wait()

    wire = Wire(packet(tool='retrieve'))
    gateway = wire.gateway()
    task = request(tool, effect='command')
    try:
        running = asyncio.create_task(gateway.run(task))
        await asyncio.wait_for(started.wait(), 5)
        await gateway.ledger.cancel(task.context)
        result = await asyncio.wait_for(running, 5)
        assert result.status == 'interrupted'
        assert any(receipt['status'] == 'unknown' for receipt in result.receipts)
        assert len(wire.requests) == 1
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_partial_usage_is_explicit_and_optional_default_is_native():
    received = []
    wire = Wire(packet(tool='retrieve', arguments={'query': 'q'}), packet(usage=False))
    gateway = wire.gateway()
    task = request(lambda args, context: received.append(args) or 'ok')
    schema = {
        'type': 'object',
        'properties': {'query': {'type': 'string'}, 'limit': {'type': 'integer', 'default': 3}},
        'required': ['query'],
        'additionalProperties': False,
    }
    task = replace(task, tools=(replace(task.tools[0], parameters=schema),))
    try:
        result = await gateway.run(task)
        assert result.status == 'completed'
        assert result.usage.total_tokens == 12
        assert not result.usage_complete
        assert received == [{'query': 'q', 'limit': 3}]
    finally:
        await wire.close()


def test_registered_tools_are_explicit_stateless_and_cannot_mix_provider_search():
    task = request()
    with pytest.raises(PermissionError):
        replace(task, allowed_tools=frozenset())
    with pytest.raises(ValueError, match='web_search'):
        replace(task, llm_request=replace(task.llm_request, web_search=True))
    with pytest.raises(ValueError, match='stateless'):
        replace(
            task, llm_request=LLMRequest(ModelProfile('full', ContextPolicy.AION_FULL), 'input', conversation_key='c')
        )
    with pytest.raises(ValueError, match='protocol'):
        replace(
            task,
            llm_request=replace(
                task.llm_request, profile=ModelProfile('p', ContextPolicy.STATELESS, model_kwargs={'tools': []})
            ),
        )


@pytest.mark.asyncio
async def test_native_child_task_shares_parent_budget_and_rolls_up_usage_once():
    wire = Wire(packet(tool='retrieve'), packet(text='child result'), packet(text='parent result'))
    gateway = wire.gateway()
    root = request(max_model=3, max_tool=1)
    child = replace(
        request(max_model=1, max_tool=0),
        context=replace(root.context, run_id='child', parent_run_id='root', role='research'),
        budget=ToolBudget(1, 0, root.budget.deadline_at),
    )

    async def delegate(arguments, context):
        result = await gateway.run(child)
        assert result.status == 'completed'
        return {'evidence': result.result.content}

    root = replace(root, tools=(replace(root.tools[0], handler=delegate),))
    try:
        result = await gateway.run(root)
        assert result.status == 'completed'
        assert result.result.content == 'parent result'
        assert result.usage.total_tokens == 36
        assert result.usage_complete
        snapshot = await gateway.ledger.snapshot(root.context)
        assert snapshot['used_model'] == 3
        assert snapshot['reserved_model'] == 0
        assert len(wire.requests) == 3
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_consumer_cancellation_persists_unknown_and_cannot_rerun():
    started = asyncio.Event()

    async def handler(arguments, context):
        started.set()
        await asyncio.Event().wait()

    wire = Wire(packet(tool='retrieve'))
    gateway = wire.gateway()
    task = request(handler, effect='command')
    running = asyncio.create_task(gateway.run(task))
    try:
        await asyncio.wait_for(started.wait(), 5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert (await gateway.ledger.snapshot(task.context))['status'] == 'interrupted'
        assert (await gateway.run(task)).status == 'interrupted'
        assert len(wire.requests) == 1
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_expired_request_has_no_model_dispatch():
    wire = Wire()
    gateway = wire.gateway()
    task = request()
    task = replace(task, budget=replace(task.budget, deadline_at=time.time() - 1))
    result = await gateway.run(task)
    assert result.status == 'cancelled'
    assert not result.receipts
    assert not wire.requests
