"""AION retrieval metadata is separate from generated prose, with old-SDK compatibility."""

from types import SimpleNamespace

import pytest
from aion.types import SearchChatResult
from aion.types import SearchOperation
from aion.types import SearchSource
from aion.types import SearchTrace

from agents.errors import AionResponseError
from agents.llm.aion_gateway import AionLLMGateway
from agents.llm.types import LLMChunk
from agents.llm.types import LLMResult


def normalize(result):
    gateway = AionLLMGateway(provider='deepseek', api_key='offline', default_model='offline')
    return gateway._normalize_result(SimpleNamespace(provider='deepseek', model='offline'), result)


def test_preserves_real_retrieval_metadata_and_detaches_sdk_mutations():
    source = {'url': 'https://example.invalid/guideline', 'text': '原始检索段落', 'locator': 'section 2'}
    result = normalize(SimpleNamespace(content='回答', search_sources=[source]))
    assert result.search_sources == (source,)
    source['text'] = 'changed'
    assert result.search_sources[0]['text'] == '原始检索段落'


def test_generated_urls_are_not_retrieval_sources_on_legacy_sdk():
    result = normalize(SimpleNamespace(content='{"sources":[{"url":"https://example.invalid"}]}'))
    assert result.search_sources == ()


@pytest.mark.parametrize('sources', ['text', [{'url': 'x'}, 'text'], {'url': 'x'}])
def test_rejects_unstructured_sdk_material(sources):
    with pytest.raises(AionResponseError):
        normalize(SimpleNamespace(content='answer', search_sources=sources))


def test_result_type_rejects_unstructured_sources():
    with pytest.raises(TypeError, match='search_sources'):
        LLMResult(content='answer', search_sources=('not a document',))


def test_official_search_trace_excerpts_become_sources_without_using_generated_prose():
    trace = SearchTrace(
        query='guideline query',
        sources=(
            SearchSource(1, 'Guideline', 'https://example.invalid/guideline', 'Original excerpt', 'search'),
            SearchSource(2, 'Unavailable', 'https://example.invalid/unreadable', None, None),
        ),
        operations=(SearchOperation('search', 'guideline query', '2026-11-07T00:00:00Z', 'success'),),
    )
    result = normalize(SearchChatResult(content='See https://forged.invalid/source', search_trace=trace))
    assert result.search_sources == (
        {
            'id': '1',
            'title': 'Guideline',
            'url': 'https://example.invalid/guideline',
            'text': 'Original excerpt',
            'excerpt_origin': 'search',
            'locator': {'source_id': 1, 'excerpt_origin': 'search'},
        },
    )
    assert result.search_trace['query'] == 'guideline query'
    assert result.search_trace['sources'][1] == {
        'source_id': 2,
        'title': 'Unavailable',
        'url': 'https://example.invalid/unreadable',
        'excerpt': None,
        'excerpt_origin': None,
    }
    assert result.search_trace['operations'] == [
        {
            'kind': 'search',
            'target': 'guideline query',
            'started_at': '2026-11-07T00:00:00Z',
            'status': 'success',
            'status_code': None,
            'error': None,
        }
    ]


def test_trace_mapping_is_detached_and_only_explicit_fields_are_exposed():
    source = {
        'source_id': 3,
        'title': 'Page',
        'url': 'https://example.invalid/page',
        'excerpt': 'Fetched source text',
        'excerpt_origin': 'fetch',
        'private_sdk_field': 'excluded',
    }
    trace = {
        'query': 'query',
        'sources': [source],
        'operations': [],
        'private_sdk_field': 'excluded',
    }
    result = normalize(SimpleNamespace(content='answer', search_trace=trace))
    chunk = AionLLMGateway._normalize_chunk(SimpleNamespace(content=None, search_trace=trace))
    source['excerpt'] = 'mutated'
    trace['sources'].clear()
    assert result.search_sources[0]['text'] == 'Fetched source text'
    assert chunk.search_sources == result.search_sources
    assert chunk.search_trace == result.search_trace
    assert set(result.search_trace) == {'query', 'sources', 'operations'}
    assert 'private_sdk_field' not in result.search_trace['sources'][0]


@pytest.mark.parametrize('kind', [LLMResult, LLMChunk])
def test_result_and_chunk_reject_non_mapping_trace(kind):
    with pytest.raises(TypeError, match='search_trace'):
        kind(content='answer', search_trace='not a trace')


def test_legacy_stream_content_does_not_create_retrieval_metadata():
    chunk = AionLLMGateway._normalize_chunk(SimpleNamespace(content='https://forged.invalid/source'))
    assert chunk.search_sources == ()
    assert chunk.search_trace is None
