"""AION 0.6 retrieval through its real SDK and an offline HTTP transport."""

from dataclasses import replace

import pytest

from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents import GraphRegistry
from agents.errors import AionGatewayError
from agents.graphs.validation import build_validation_graph
from agents.llm import ContextPolicy
from agents.llm import LLMRequest
from tests.unit._support.agents import InMemoryLLMContextRepository
from tests.unit.agents.llm.test_web_search import _call
from tests.unit.agents.llm.test_web_search import _gateway
from tests.unit.agents.llm.test_web_search import _profile
from tests.unit.agents.llm.test_web_search import aion_wire as aion_wire


def _metadata(result, *, stream):
    return result[-1] if stream else result


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize(('profile_search', 'request_search'), [(True, None), (False, True)])
@pytest.mark.asyncio
async def test_real_deepseek_search_fetch_model_and_tail_trace(aion_wire, stream, profile_search, request_search):
    profile = _profile(web_search=profile_search, thinking=True, model_kwargs={'temperature': 0.2})
    request = LLMRequest(
        profile=profile,
        content='Full task ' * 30,
        supplemental_context='Temporary report context',
        web_search=request_search,
        search_query='independent query',
    )

    result = await _call(_gateway('deepseek'), request, stream=stream)

    assert [path for path, _ in aion_wire.calls] == [
        '/v1/tools/search',
        '/v1/tools/fetch',
        '/chat/completions',
    ]
    assert aion_wire.calls[0][1] == {'query': 'independent query', 'count': 5, 'need_content': True}
    assert aion_wire.calls[1][1] == {'url': 'https://example.invalid/fetch'}
    model = aion_wire.requests[0]
    assert model['messages'][0]['content'].startswith(request.effective_content)
    assert 'Search excerpt' in model['messages'][0]['content']
    assert 'Fetched excerpt' in model['messages'][0]['content']
    assert model['temperature'] == 0.2
    assert model['thinking'] == {'type': 'enabled'}
    assert not {'search', 'search_query', 'web_search', 'enable_search'}.intersection(model)
    metadata = _metadata(result, stream=stream)
    assert [source['text'] for source in metadata.search_sources] == ['Search excerpt', 'Fetched excerpt']
    assert [source['id'] for source in metadata.search_sources] == ['1', '2']
    assert metadata.search_sources[1]['locator'] == {'source_id': 2, 'excerpt_origin': 'fetch'}
    assert metadata.search_trace['query'] == 'independent query'
    assert [(op['kind'], op['status']) for op in metadata.search_trace['operations']] == [
        ('search', 'success'),
        ('fetch', 'success'),
    ]
    assert all(op['started_at'] for op in metadata.search_trace['operations'])
    if stream:
        assert metadata.content is None and metadata.usage is None
        assert all(chunk.search_trace is None and not chunk.search_sources for chunk in result[:-1])


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('explicit_query', [False, True])
@pytest.mark.asyncio
async def test_deepseek_accepts_128_character_query(aion_wire, stream, explicit_query):
    query = 'q' * 128
    request = LLMRequest(
        profile=_profile(web_search=True),
        content='long content ' * 30 if explicit_query else query,
        search_query=query if explicit_query else None,
    )
    result = await _call(_gateway('deepseek'), request, stream=stream)
    assert aion_wire.calls[0][1]['query'] == query
    assert _metadata(result, stream=stream).search_trace['query'] == query


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('explicit_query', [False, True])
@pytest.mark.asyncio
async def test_deepseek_rejects_129_character_query_before_http_and_context_write(aion_wire, stream, explicit_query):
    repository = InMemoryLLMContextRepository()
    request = LLMRequest(
        profile=replace(_profile(web_search=True), context_policy=ContextPolicy.AION_FULL),
        content='question' if explicit_query else 'q' * 129,
        search_query='q' * 129 if explicit_query else None,
        conversation_key='conversation-1',
    )
    with pytest.raises(AionGatewayError) as failure:
        await _call(_gateway('deepseek', repository), request, stream=stream)
    assert isinstance(failure.value.__cause__, ValueError)
    assert '1 to 128' in str(failure.value.__cause__)
    assert failure.value.search_trace is None
    assert failure.value.search_reason is None
    assert aion_wire.calls == []
    assert await repository.load('validation.search:conversation-1') is None


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize(
    ('scenario', 'reason', 'operations'),
    [
        ('empty', 'no_results', [('search', 'empty')]),
        ('search_failed', 'request_failed', [('search', 'failed')]),
        ('unreadable', 'no_readable_evidence', [('search', 'success'), ('fetch', 'empty')]),
        ('fetch_failed', 'no_readable_evidence', [('search', 'success'), ('fetch', 'failed')]),
    ],
)
@pytest.mark.asyncio
async def test_retrieval_failures_keep_trace_and_never_call_model_or_save_context(
    aion_wire, stream, scenario, reason, operations
):
    repository = InMemoryLLMContextRepository()
    if scenario == 'empty':
        aion_wire.search_results = []
    elif scenario == 'search_failed':
        aion_wire.search_status = 500
    else:
        aion_wire.search_results = aion_wire.search_results[1:]
        aion_wire.fetch_responses['https://example.invalid/fetch'] = (
            (500, {'error': {'message': 'fetch failure'}}) if scenario == 'fetch_failed' else (200, {'content': '  '})
        )
    request = LLMRequest(
        profile=replace(_profile(web_search=True), context_policy=ContextPolicy.AION_FULL),
        content='question',
        conversation_key='conversation-1',
    )

    with pytest.raises(AionGatewayError) as failure:
        await _call(_gateway('deepseek', repository), request, stream=stream)

    assert failure.value.search_reason == reason
    trace = failure.value.search_trace
    assert trace['query'] == 'question'
    assert [(op['kind'], op['status']) for op in trace['operations']] == operations
    assert failure.value.__cause__.reason == reason
    if scenario.endswith('failed'):
        assert trace['operations'][-1]['status_code'] == 500
        assert trace['operations'][-1]['error'] == 'InternalServerError'
    assert len(aion_wire.calls) == len(operations)
    assert aion_wire.requests == []
    assert await repository.load('validation.search:conversation-1') is None


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.asyncio
async def test_partial_fetch_failure_keeps_only_readable_evidence(aion_wire, stream):
    aion_wire.fetch_responses['https://example.invalid/fetch'] = (500, {'error': {'message': 'fetch failure'}})
    request = LLMRequest(profile=_profile(web_search=True), content='question')
    result = _metadata(await _call(_gateway('deepseek'), request, stream=stream), stream=stream)

    assert len(result.search_sources) == 1
    assert result.search_sources[0]['text'] == 'Search excerpt'
    assert len(result.search_trace['sources']) == 2
    assert result.search_trace['sources'][1]['excerpt'] is None
    assert result.search_trace['operations'][-1]['status'] == 'failed'
    assert len(aion_wire.requests) == 1


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.asyncio
async def test_search_context_archives_only_original_turns_and_preserves_revision_on_model_failure(aion_wire, stream):
    repository = InMemoryLLMContextRepository()
    profile = replace(_profile(), context_policy=ContextPolicy.AION_FULL)
    gateway = _gateway('deepseek', repository)
    first = LLMRequest(
        profile=profile,
        content='first question',
        supplemental_context='temporary reference',
        conversation_key='conversation-1',
        web_search=True,
        search_query='independent query',
    )
    second = LLMRequest(profile=profile, content='second question', conversation_key='conversation-1')
    await _call(gateway, first, stream=stream)
    await _call(gateway, second, stream=stream)

    assert aion_wire.requests[1]['messages'] == [
        {'role': 'user', 'content': 'first question'},
        {'role': 'assistant', 'content': 'answer'},
        {'role': 'user', 'content': 'second question'},
    ]
    snapshot = await repository.load('validation.search:conversation-1')
    assert snapshot.revision == 2
    assert len(snapshot.payload['turns']) == 2
    aion_wire.fail = True
    with pytest.raises(AionGatewayError):
        await _call(gateway, first, stream=stream)
    current = await repository.load('validation.search:conversation-1')
    assert current.revision == snapshot.revision and current.payload == snapshot.payload


@pytest.mark.asyncio
async def test_interrupted_deepseek_search_stream_does_not_save_partial_turn(aion_wire):
    repository = InMemoryLLMContextRepository()
    request = LLMRequest(
        profile=replace(_profile(web_search=True), context_policy=ContextPolicy.AION_FULL),
        content='question',
        conversation_key='conversation-1',
    )
    iterator = _gateway('deepseek', repository).stream(request)
    first = await anext(iterator)
    await iterator.aclose()
    assert first.content == 'answer'
    assert first.search_trace is None
    assert await repository.load('validation.search:conversation-1') is None


@pytest.mark.asyncio
async def test_truncated_deepseek_model_stream_does_not_save_partial_turn(aion_wire):
    aion_wire.truncate_stream = True
    repository = InMemoryLLMContextRepository()
    request = LLMRequest(
        profile=replace(_profile(web_search=True), context_policy=ContextPolicy.AION_FULL),
        content='question',
        conversation_key='conversation-1',
    )
    chunks = []
    with pytest.raises(AionGatewayError):
        async for chunk in _gateway('deepseek', repository).stream(request):
            chunks.append(chunk)
    assert ''.join(chunk.content or '' for chunk in chunks) == 'answer'
    assert all(chunk.search_trace is None for chunk in chunks)
    assert await repository.load('validation.search:conversation-1') is None


@pytest.mark.parametrize('model_failures', [0, 1])
@pytest.mark.asyncio
async def test_transport_metrics_count_only_model_retries(aion_wire, model_failures):
    aion_wire.model_failures = model_failures
    request = LLMRequest(profile=_profile(web_search=True, capture_transport_metrics=True), content='question')
    result = await _gateway('deepseek').complete(request)
    assert result.transport_retries == model_failures
    assert len(aion_wire.calls) == 3 + model_failures
    assert len(aion_wire.requests) == 1 + model_failures


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.asyncio
async def test_validation_aggregates_usage_and_tail_trace_without_treating_generated_urls_as_sources(aion_wire, stream):
    aion_wire.content = 'See https://forged.invalid/pretend-reference'
    registry = GraphRegistry()
    registry.register(
        'validation.search', build_validation_graph(gateway=_gateway('deepseek'), profile=_profile()), version='v1'
    )
    hub = AgentHub(registry)
    context = AgentRunContext.create(user_id='user', session_id='session', request_id='request', run_id='run')
    result = await hub.invoke(
        'validation.search',
        AgentRequest(
            {'message': 'question', 'web_search': True, 'search_query': 'independent query', 'stream': stream}
        ),
        context,
    )
    assert result.data['response'] == aion_wire.content
    assert result.data['usage']['totalTokens'] == 8
    assert result.data['search_trace']['query'] == 'independent query'
    assert [source['url'] for source in result.data['search_sources']] == [
        'https://example.invalid/search',
        'https://example.invalid/fetch',
    ]
