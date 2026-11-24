"""共享联网开关的配置、AgentHub 与真实 AION 请求边界测试；不访问网络。"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import openai
import pytest
import pytest_asyncio

from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents import GraphRegistry
from agents.errors import AionGatewayError
from agents.errors import LLMCapabilityNotSupportedError
from agents.graphs.validation import build_validation_graph
from agents.llm import AionLLMGateway
from agents.llm import ContextPolicy
from agents.llm import LLMRequest
from agents.llm import ModelProfile
from tests.unit._support.agents import InMemoryLLMContextRepository


class AionWire:
    """让已安装的 AION/SDK 完成参数序列化，只替换 HTTP 传输。"""

    def __init__(self):
        self.requests: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.fail = False
        self.model_failures = 0
        self.content = 'answer'
        self.truncate_stream = False
        self.search_status = 200
        self.search_results = [
            {'title': 'Search source', 'url': 'https://example.invalid/search', 'content': 'Search excerpt'},
            {'title': 'Fetched source', 'url': 'https://example.invalid/fetch'},
        ]
        self.fetch_responses = {'https://example.invalid/fetch': (200, {'content': 'Fetched excerpt'})}

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append((request.url.path, body))
        if request.url.path == '/v1/tools/search':
            return httpx.Response(self.search_status, json={'results': self.search_results})
        if request.url.path == '/v1/tools/fetch':
            status, response = self.fetch_responses[body['url']]
            return httpx.Response(status, json=response)
        assert request.url.path.endswith('/chat/completions')
        self.requests.append(body)
        if self.fail:
            return httpx.Response(400, json={'error': {'message': 'test failure'}})
        if self.model_failures:
            self.model_failures -= 1
            return httpx.Response(500, json={'error': {'message': 'temporary model failure'}})
        base = {'id': 'test-completion', 'created': 0, 'model': body['model']}
        usage = {'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8}
        if body.get('stream'):
            chunks = [
                {
                    **base,
                    'object': 'chat.completion.chunk',
                    'choices': [{'index': 0, 'delta': {'content': self.content}, 'finish_reason': None}],
                },
                {
                    **base,
                    'object': 'chat.completion.chunk',
                    'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                },
                {**base, 'object': 'chat.completion.chunk', 'choices': [], 'usage': usage},
            ]
            if self.truncate_stream:
                chunks = chunks[:1]
            content = ''.join(f'data: {json.dumps(chunk)}\n\n' for chunk in chunks)
            return httpx.Response(
                200,
                headers={'content-type': 'text/event-stream'},
                content=(content + 'data: [DONE]\n\n').encode(),
            )
        return httpx.Response(
            200,
            json={
                **base,
                'object': 'chat.completion',
                'choices': [
                    {'index': 0, 'message': {'role': 'assistant', 'content': self.content}, 'finish_reason': 'stop'},
                ],
                'usage': usage,
            },
        )


@pytest_asyncio.fixture
async def aion_wire(monkeypatch):
    wire = AionWire()
    sync_class, async_class = openai.OpenAI, openai.AsyncOpenAI
    sync_clients, async_clients = [], []

    def sync_factory(**kwargs):
        client = sync_class(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(wire.handle)))
        sync_clients.append(client)
        return client

    def async_factory(**kwargs):
        client = async_class(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)))
        async_clients.append(client)
        return client

    monkeypatch.setattr(openai, 'OpenAI', sync_factory)
    monkeypatch.setattr(openai, 'AsyncOpenAI', async_factory)
    try:
        yield wire
    finally:
        for client in sync_clients:
            client.close()
        for client in async_clients:
            await client.close()


def _profile(**kwargs):
    return ModelProfile(name='validation.search', context_policy=ContextPolicy.STATELESS, **kwargs)


def _gateway(provider='aliyun', repository=None, **kwargs):
    return AionLLMGateway(
        provider=provider,
        api_key='unit-test-key',
        default_model='deepseek-v4-flash' if provider == 'deepseek' else 'unit-test-model',
        context_repository=repository,
        **kwargs,
    )


async def _call(gateway, request, *, stream):
    if stream:
        chunks = [chunk async for chunk in gateway.stream(request)]
        assert ''.join(chunk.content or '' for chunk in chunks) == 'answer'
        usages = [chunk.usage for chunk in chunks if chunk.usage is not None]
        assert len(usages) == 1
        assert usages[0].total_tokens == 8
        return chunks
    else:
        result = await gateway.complete(request)
        assert result.content == 'answer'
        assert result.usage.total_tokens == 8
        return result


def test_default_is_disabled_and_old_positional_parameters_keep_their_meaning():
    profile = ModelProfile('test', ContextPolicy.STATELESS, None, 'model', True, {}, {'temperature': 0})
    request = LLMRequest(profile, 'question', None, None, False)
    assert profile.web_search is False
    assert request.web_search is None
    assert request.effective_web_search is False
    assert request.effective_thinking is False
    assert profile.model_kwargs == {'temperature': 0}


@pytest.mark.parametrize('value', [None, 'true', 'false', 0, 1, [], {}])
def test_profile_rejects_non_boolean_search(value):
    with pytest.raises(TypeError, match='web_search must be a boolean'):
        _profile(web_search=value)


@pytest.mark.parametrize('value', ['true', 'false', 0, 1, [], {}])
def test_request_rejects_non_boolean_search(value):
    with pytest.raises(TypeError, match='web_search must be a boolean or None'):
        LLMRequest(profile=_profile(), content='question', web_search=value)


@pytest.mark.parametrize('provider', ['aliyun', 'qwen'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize(
    ('profile_search', 'request_search', 'expected'),
    [(False, None, False), (True, None, True), (True, False, False), (False, True, True)],
)
@pytest.mark.asyncio
async def test_real_aion_serializes_effective_search(
    aion_wire, provider, stream, profile_search, request_search, expected
):
    profile = _profile(web_search=profile_search, thinking=True, model_kwargs={'temperature': 0.2})
    request = LLMRequest(profile=profile, content='question', web_search=request_search)

    await _call(_gateway(provider), request, stream=stream)

    assert request.effective_web_search is expected
    assert len(aion_wire.requests) == 1
    body = aion_wire.requests[0]
    assert body['enable_search'] is expected
    assert body['enable_thinking'] is True
    assert body['temperature'] == 0.2
    assert 'web_search' not in body
    assert 'search' not in body
    assert body['messages'] == [{'role': 'user', 'content': 'question'}]


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize(('profile_search', 'request_search'), [(False, None), (True, False)])
@pytest.mark.asyncio
async def test_installed_deepseek_remains_usable_with_search_disabled(
    aion_wire, stream, profile_search, request_search
):
    profile = _profile(web_search=profile_search)
    request = LLMRequest(profile=profile, content='question', web_search=request_search)

    result = await _call(_gateway('deepseek'), request, stream=stream)

    assert len(aion_wire.requests) == 1
    assert len(aion_wire.calls) == 1
    assert not {'search', 'web_search', 'enable_search'}.intersection(aion_wire.requests[0])
    metadata = result[-1] if stream else result
    assert metadata.search_sources == ()
    assert metadata.search_trace is None


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.asyncio
async def test_old_client_rejects_search_without_http_or_context_write(aion_wire, stream):
    repository = InMemoryLLMContextRepository()
    profile = replace(_profile(), context_policy=ContextPolicy.AION_FULL, web_search=True)
    request = LLMRequest(profile=profile, content='question', conversation_key='conversation-1')

    async def complete(content, **kwargs):
        pytest.fail('an old client must not receive enabled search')

    async def stream_async(content, **kwargs):
        pytest.fail('an old client must not receive enabled search')
        yield

    client = SimpleNamespace(
        complete_async=complete,
        stream_async=stream_async,
        export_context=lambda: {'turns': []},
    )
    gateway = _gateway('deepseek', repository, client_factory=lambda *args, **kwargs: client)

    with pytest.raises(LLMCapabilityNotSupportedError, match='explicit search parameter'):
        await _call(gateway, request, stream=stream)

    assert aion_wire.requests == []
    assert await repository.load('validation.search:conversation-1') is None


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize(
    'model_kwargs',
    [
        {'web_search': True},
        {'search': False},
        {'enable_search': True},
        {'search_query': 'hidden query'},
        {'extra_body': {'search': True}},
        {'extra_body': {'web_search': True}},
        {'extra_body': {'enable_search': True}},
        {'extra_body': {'search_query': 'hidden query'}},
        {'tools': [{'type': 'web_search'}]},
        {'extra_body': {'tools': [{'type': 'web_search_preview'}]}},
        {'extra_body': {'tools': [{'type': 'web_search_2025_08_26'}]}},
    ],
)
@pytest.mark.asyncio
async def test_passthrough_cannot_override_search_switch(aion_wire, stream, model_kwargs):
    with pytest.raises(ValueError, match='Configure web_search on ModelProfile or LLMRequest'):
        _profile(model_kwargs=model_kwargs)

    # 现有 Profile 允许修改 model_kwargs 映射；网关发送前也必须阻止绕过开关。
    profile = _profile()
    profile.model_kwargs.update(model_kwargs)
    request = LLMRequest(profile=profile, content='question', web_search=False)
    with pytest.raises(ValueError, match='Configure web_search on ModelProfile or LLMRequest'):
        await _call(_gateway(), request, stream=stream)
    assert aion_wire.requests == []


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.asyncio
async def test_search_toggle_does_not_reset_context_or_persist_temporary_context(aion_wire, stream):
    repository = InMemoryLLMContextRepository()
    profile = replace(_profile(), context_policy=ContextPolicy.AION_FULL)
    gateway = _gateway(repository=repository)
    first = LLMRequest(
        profile=profile,
        content='first question',
        supplemental_context='temporary reference',
        conversation_key='conversation-1',
        web_search=True,
    )
    second = LLMRequest(profile=profile, content='second question', conversation_key='conversation-1')

    await _call(gateway, first, stream=stream)
    await _call(gateway, second, stream=stream)

    assert aion_wire.requests[0]['enable_search'] is True
    assert aion_wire.requests[1]['enable_search'] is False
    assert aion_wire.requests[1]['messages'] == [
        {'role': 'user', 'content': 'first question'},
        {'role': 'assistant', 'content': 'answer'},
        {'role': 'user', 'content': 'second question'},
    ]
    snapshot = await repository.load('validation.search:conversation-1')
    assert snapshot.revision == 2
    assert len(snapshot.payload['turns']) == 2
    assert 'temporary reference' not in json.dumps(snapshot.payload)

    aion_wire.fail = True
    with pytest.raises(AionGatewayError):
        await _call(gateway, first, stream=stream)
    assert (await repository.load('validation.search:conversation-1')).revision == 2


@pytest.mark.asyncio
async def test_interrupted_search_stream_does_not_save_partial_turn(aion_wire):
    repository = InMemoryLLMContextRepository()
    profile = replace(_profile(web_search=True), context_policy=ContextPolicy.AION_FULL)
    request = LLMRequest(profile=profile, content='question', conversation_key='conversation-1')
    iterator = _gateway(repository=repository).stream(request)
    first = await anext(iterator)
    await iterator.aclose()

    assert first.content == 'answer'
    assert aion_wire.requests[0]['enable_search'] is True
    assert await repository.load('validation.search:conversation-1') is None


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.asyncio
async def test_agent_hub_graph_can_enable_search_per_call(aion_wire, stream):
    registry = GraphRegistry()
    registry.register('validation.search', build_validation_graph(gateway=_gateway(), profile=_profile()), version='v1')
    hub = AgentHub(registry)
    context = AgentRunContext.create(user_id='user', session_id='session', request_id='request', run_id='run')

    result = await hub.invoke(
        'validation.search',
        AgentRequest({'message': 'question', 'web_search': True, 'stream': stream}),
        context,
    )

    assert result.data['response'] == 'answer'
    assert aion_wire.requests[0]['enable_search'] is True


@pytest.mark.asyncio
async def test_positional_only_search_is_not_keyword_support():
    async def complete(content, search=False, /, **kwargs):
        pytest.fail('an unsupported signature must not be called')

    client = SimpleNamespace(complete_async=complete)
    gateway = _gateway(client_factory=lambda *args, **kwargs: client)
    with pytest.raises(LLMCapabilityNotSupportedError):
        await gateway.complete(LLMRequest(profile=_profile(web_search=True), content='question'))


@pytest.mark.parametrize('value', [False, 128, [], {}])
def test_search_query_requires_text(value):
    with pytest.raises(TypeError, match='search_query'):
        LLMRequest(profile=_profile(web_search=True), content='question', search_query=value)


@pytest.mark.parametrize('value', ['', '   '])
def test_search_query_requires_nonempty_text(value):
    with pytest.raises(ValueError, match='search_query'):
        LLMRequest(profile=_profile(web_search=True), content='question', search_query=value)


def test_search_query_requires_effective_search():
    with pytest.raises(ValueError, match='search_query'):
        LLMRequest(profile=_profile(web_search=True), content='question', web_search=False, search_query='query')


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.asyncio
async def test_aliyun_rejects_independent_query_without_forwarding_it(aion_wire, stream):
    request = LLMRequest(profile=_profile(web_search=True), content='question', search_query='independent query')
    with pytest.raises(LLMCapabilityNotSupportedError, match='search_query'):
        await _call(_gateway('aliyun'), request, stream=stream)
    assert aion_wire.calls == []


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.asyncio
async def test_old_client_without_explicit_search_remains_usable_when_disabled(stream):
    calls = []
    response = SimpleNamespace(content='answer', usage={'total_tokens': 8})

    async def complete(content, **kwargs):
        calls.append(kwargs)
        return response

    async def stream_async(content, **kwargs):
        calls.append(kwargs)
        yield response

    client = SimpleNamespace(complete_async=complete, stream_async=stream_async)
    gateway = _gateway(client_factory=lambda *args, **kwargs: client)
    await _call(gateway, LLMRequest(profile=_profile(), content='question'), stream=stream)
    assert len(calls) == 1
    assert not {'search', 'search_query'}.intersection(calls[0])
