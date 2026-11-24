"""The live probe must preserve model chunks and time body rather than control events."""

import json
from dataclasses import replace

import httpx
import openai
import pytest

from agents.graphs.chat import create_chat_reply_profile
from agents.graphs.record.profile import create_record_plan_profile
from agents.llm import LLMChunk
from agents.llm import LLMRequest
from tests.api.scripts.compare_qa_models import summarize
from tests.api.scripts.local_qa_latency import TimedGateway
from tests.api.scripts.local_qa_latency import build_gateway
from tests.api.scripts.qa_latency import stream_turn
from tests.unit._support.agents import FakeLLMGateway


@pytest.mark.asyncio
async def test_model_instrumentation_forwards_normalized_chunks_unchanged():
    chunks = [LLMChunk(), LLMChunk(reasoning_content='思考'), LLMChunk(content='正文')]
    gateway = TimedGateway(FakeLLMGateway(stream_chunks=chunks))
    request = LLMRequest(profile=create_chat_reply_profile(), content='测试', conversation_key='test-session')
    assert [chunk async for chunk in gateway.stream(request)] == chunks
    assert gateway.calls[0]['output_chars'] == 2
    assert 'error_type' not in gateway.calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [True, False])
async def test_body_time_excludes_start_reasoning_and_whitespace(body):
    events = [
        {'type': 'start'},
        {'type': 'reasoning', 'content': '分析中'},
        {'type': 'content', 'content': ' \n'},
    ]
    if body:
        events.append({'type': 'content', 'content': '正文'})
    events.append({'type': 'finish'})
    content = ''.join('data: ' + json.dumps({'data': data}) + '\n\n' for data in events)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=content))
    async with httpx.AsyncClient(base_url='https://unit-test.invalid', transport=transport) as client:
        row = {}
        await stream_turn(client, '/stream', {}, row)
    assert row['success'] is body
    if body:
        assert row['first_body_ms'] >= row['first_reasoning_ms'] >= row['first_event_ms']
    else:
        assert row['first_body_ms'] is None


@pytest.mark.asyncio
async def test_qwen_plan_routing_preserves_wire_contract_and_other_models(monkeypatch):
    """Exercise the real AION/SDK serializer with a fake HTTP response, not live Qwen."""
    requests = []
    clients = []
    settings = {
        'provider': 'DeepSeek',
        'model': 'deepseek-flash',
        'api_key': 'synthetic-deepseek-key',
        'profile_overrides': {
            'record.plan': {
                'provider': 'Aliyun',
                'model': 'qwen3.8-flash',
                'api_key': 'synthetic-qwen-key',
                'base_url': 'https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
                'model_kwargs': {
                    'response_format': {'type': 'json_object'},
                    'extra_body': {'preserve_thinking': False},
                },
            }
        },
    }
    usage = {
        'prompt_tokens': 2048,
        'completion_tokens': 8,
        'total_tokens': 2056,
        'prompt_tokens_details': {'cached_tokens': 1024},
    }

    def respond(request):
        body = json.loads(request.content)
        requests.append((request, body))
        if body.get('stream'):
            events = [
                {'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]},
                {'choices': [{'index': 0, 'delta': {'content': '{"ok":true}'}, 'finish_reason': None}]},
                {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
                {'choices': [], 'usage': usage},
            ]
            content = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events) + 'data: [DONE]\n\n'
            return httpx.Response(200, text=content, headers={'content-type': 'text/event-stream'}, request=request)
        return httpx.Response(
            200,
            json={
                'id': 'synthetic-response',
                'created': 0,
                'object': 'chat.completion',
                'model': body['model'],
                'choices': [
                    {
                        'index': 0,
                        'message': {'role': 'assistant', 'content': '{"ok":true}'},
                        'finish_reason': 'stop',
                    }
                ],
                'usage': usage,
            },
            request=request,
        )

    sdk_class = openai.AsyncOpenAI

    def sdk_factory(**kwargs):
        return sdk_class(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))

    monkeypatch.setattr(openai, 'AsyncOpenAI', sdk_factory)
    gateway = build_gateway(settings, None, clients)
    plan = LLMRequest(profile=create_record_plan_profile(), content='请返回 JSON')
    question = replace(plan, profile=replace(plan.profile, name='record.question'))
    try:
        result = await gateway.complete(plan)
        chunks = [chunk async for chunk in gateway.stream(plan)]
        await gateway.complete(question)
    finally:
        for client in clients:
            await client.async_client.close()
            client.client.close()
    qwen_request, qwen_body = requests[0]
    assert qwen_request.url.host == 'workspace.cn-beijing.maas.aliyuncs.com'
    assert qwen_request.url.path == '/compatible-mode/v1/chat/completions'
    assert qwen_request.headers['authorization'] == 'Bearer synthetic-qwen-key'
    assert qwen_body['model'] == 'qwen3.8-flash'
    assert qwen_body['enable_thinking'] is False
    assert qwen_body['enable_search'] is False
    assert qwen_body['preserve_thinking'] is False
    assert 'extra_body' not in qwen_body
    assert qwen_body['response_format'] == {'type': 'json_object'}
    assert [message['role'] for message in qwen_body['messages']] == ['system', 'user']
    assert requests[1][1]['stream_options'] == {'include_usage': True}
    assert ''.join(chunk.content or '' for chunk in chunks) == result.content == '{"ok":true}'
    assert chunks[-1].usage.raw['prompt_tokens_details']['cached_tokens'] == 1024
    assert result.usage.raw['prompt_tokens_details']['cached_tokens'] == 1024
    deepseek_request, deepseek_body = requests[2]
    assert deepseek_request.headers['authorization'] == 'Bearer synthetic-deepseek-key'
    assert deepseek_body['model'] == 'deepseek-flash'
    assert 'preserve_thinking' not in deepseek_body
    assert 'response_format' not in deepseek_body
    assert [call['provider'] for call in gateway.calls] == ['Aliyun', 'Aliyun', 'DeepSeek']
    assert 'synthetic-qwen-key' not in json.dumps(gateway.calls)
    assert plan.profile.model is None
    assert plan.profile.model_kwargs == {}


def test_route_requires_separate_credential_before_creating_clients():
    clients = []
    settings = {
        'provider': 'DeepSeek',
        'model': 'deepseek-flash',
        'api_key': 'synthetic-default-key',
        'profile_overrides': {'record.plan': {'provider': 'Aliyun', 'model': 'qwen3.8-flash'}},
    }
    with pytest.raises(ValueError, match='own provider, model and api_key'):
        build_gateway(settings, None, clients)
    assert clients == []


def test_comparison_does_not_hide_failed_or_slow_attempts():
    runs = [
        {
            'arm': 'candidate',
            'rows': [
                {'scenario': 'record-fast', 'success': False, 'first_body_ms': None},
                {'scenario': 'record-fast', 'success': True, 'first_body_ms': 4000, 'validation_rejections': [{}]},
                {'scenario': 'record-fast', 'success': True, 'first_body_ms': 8000},
            ],
        }
    ]
    assert summarize(runs)['candidate/record-fast'] == {
        'attempts': 3,
        'successes': 2,
        'within_5s': 1,
        'rejections': 1,
        'body_ms_median_success_only': 6000,
        'body_ms_max_success_only': 8000,
    }
