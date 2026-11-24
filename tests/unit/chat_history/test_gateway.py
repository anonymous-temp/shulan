"""Exercise the shared query tool through the existing real AION tool protocol."""

import json
import time

import aion
import httpx
import pytest
from openai import AsyncOpenAI

from adapters.chat_history import OpenIMChatHistoryReader
from agents.llm import AionLLMGateway
from agents.llm import ContextPolicy
from agents.llm import LLMRequest
from agents.llm import ModelProfile
from agents.llm.tool_gateway import AionToolRunGateway
from agents.tools import ToolBudget
from agents.tools import ToolRunContext
from agents.tools import ToolRunRequest
from agents.tools.ledger import ToolRunLedger
from modules.chat_history import build_chat_history_tool
from tests.unit._support.agents.tool_runs import MemoryToolRunRepository


def model_packet(*, use_tool):
    base = {'id': 'test-completion', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'test-model'}

    def chunk(delta, finish=None):
        return {**base, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}

    if use_tool:
        events = [
            chunk(
                {
                    'tool_calls': [
                        {
                            'index': 0,
                            'id': 'test-history-call',
                            'type': 'function',
                            'function': {'name': 'query_chat_history', 'arguments': '{"groupId":"test-group"}'},
                        }
                    ]
                }
            ),
            chunk({}, 'tool_calls'),
        ]
    else:
        events = [chunk({'content': 'Synthetic history received'}), chunk({}, 'stop')]
    events.append({**base, 'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}})
    return ''.join('data: ' + json.dumps(event) + '\n\n' for event in events) + 'data: [DONE]\n\n'


class ModelWire:
    def __init__(self):
        self.packets = [model_packet(use_tool=True), model_packet(use_tool=False)]
        self.requests = []
        self.clients = []
        self.original_sdks = []

    def factory(self, provider, **options):
        client = aion.create_chat_client(provider, **options)
        self.clients.append(client)
        self.original_sdks.append(client.async_client)
        client.async_client = AsyncOpenAI(
            api_key='test-offline-key',
            base_url='https://test-model.invalid',
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.respond)),
        )
        return client

    def respond(self, request):
        self.requests.append(json.loads(request.content))
        assert self.packets, 'Unexpected additional model request'
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=self.packets.pop(0))

    async def close(self):
        for client in self.clients:
            client.client.close()
            await client.async_client.close()
        for sdk in self.original_sdks:
            await sdk.close()


@pytest.mark.asyncio
async def test_query_tool_runs_with_explicit_tool_permissions_and_replays_saved_result():
    history_requests = []
    http_clients = []
    data = {
        'chatLogs': [
            {
                'chatLog': {
                    'serverMsgID': 'test-server-message',
                    'contentType': 105,
                    'content': 'https://files.invalid/download?objectName=test%2Freport.pdf',
                },
                'isRevoked': False,
            }
        ],
        'chatLogsNum': 1,
    }

    def respond(request):
        history_requests.append(request)
        return httpx.Response(200, json={'state': 200, 'results': {'errCode': 0, 'data': data}})

    def http_factory(**options):
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond), **options)
        http_clients.append(client)
        return client

    reader = OpenIMChatHistoryReader('https://test-history.invalid/search', client_factory=http_factory)
    tool = build_chat_history_tool(reader)
    wire = ModelWire()
    llm = AionLLMGateway(
        provider='deepseek',
        api_key='test-offline-key',
        default_model='test-model',
        client_factory=wire.factory,
    )
    gateway = AionToolRunGateway(llm, ToolRunLedger(MemoryToolRunRepository()), poll_seconds=0.01)
    request = ToolRunRequest(
        LLMRequest(ModelProfile('test-chat-history', ContextPolicy.STATELESS), 'Read the synthetic group history'),
        ToolRunContext('test-user', 'test-root-run', 'test-root-run', 'chat_history'),
        ToolBudget(2, 1, time.time() + 30),
        (tool,),
        frozenset({tool.identity}),
    )
    try:
        result = await gateway.run(request)
        assert result.status == 'completed'
        assert result.result.content == 'Synthetic history received'
        assert len(history_requests) == 1
        assert json.loads(history_requests[0].content) == {'groupId': 'test-group'}
        assert http_clients[0].is_closed
        assert len(wire.requests) == 2
        model_tool = wire.requests[0]['tools'][0]['function']
        assert model_tool['name'] == tool.name
        assert model_tool['parameters']['additionalProperties'] is False
        returned = wire.requests[1]['messages'][-1]
        assert returned['tool_call_id'] == 'test-history-call'
        assert json.loads(returned['content']) == data
        assert (await gateway.run(request)).result == result.result
        assert len(history_requests) == 1
        assert len(wire.requests) == 2
    finally:
        await wire.close()
