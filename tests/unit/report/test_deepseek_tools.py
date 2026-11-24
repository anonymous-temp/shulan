import asyncio
import json

import httpx
import pytest

from adapters.report.deepseek_tools import DeepSeekDocumentTextClient
from adapters.report.deepseek_tools import DeepSeekToolError
from adapters.report.deepseek_tools import DeepSeekToolsClient


def _client(handler, **options):
    return DeepSeekToolsClient(api_key='unit-test-key', transport=httpx.MockTransport(handler), **options)


def test_search_parses_hits_and_counts_billing():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                'results': [
                    {
                        'title': 'Guideline A',
                        'url': 'https://example.org/a#2',
                        'content': 'body',
                        'summary': 's',
                        'site_name': 'Example',
                        'date': '2025-03-01T00:00:00Z',
                        'rerank_score': 2.1,
                        'authority': 4,
                    },
                    {'title': '', 'url': 'https://example.org/untitled'},
                    {'title': 'bad url', 'url': 'ftp://example.org/x'},
                ]
            },
        )

    async def run():
        client = _client(handler)
        try:
            return await client.search('  fecal   occult blood guideline ', count=99), client.usage.snapshot()
        finally:
            await client.aclose()

    hits, usage = asyncio.run(run())
    body = json.loads(requests[0].content)
    assert requests[0].url.path == '/v1/tools/search'
    assert requests[0].headers['authorization'] == 'Bearer unit-test-key'
    assert body == {'query': 'fecal occult blood guideline', 'count': 50, 'need_content': True}
    assert [hit.title for hit in hits] == ['Guideline A']
    assert (hits[0].host, hits[0].year, hits[0].authority) == ('example.org', 2025, 4)
    assert usage['searchCalls'] == 1 and usage['searchBilled'] == 1


def test_search_rejects_out_of_range_query_before_http():
    async def run():
        client = _client(lambda request: pytest.fail('no request expected'))
        try:
            await client.search('x' * 129)
        finally:
            await client.aclose()

    with pytest.raises(ValueError):
        asyncio.run(run())


def test_retryable_status_is_retried_and_other_4xx_is_not():
    calls = {'search': 0}

    def flaky(request):
        calls['search'] += 1
        return httpx.Response(429) if calls['search'] == 1 else httpx.Response(200, json={'results': []})

    async def run(handler, attempts=3):
        client = _client(handler, attempts=attempts)
        try:
            return await client.search('q')
        finally:
            await client.aclose()

    assert asyncio.run(run(flaky)) == []
    assert calls['search'] == 2
    with pytest.raises(DeepSeekToolError) as error:
        asyncio.run(run(lambda request: httpx.Response(402)))
    assert error.value.status == 402 and error.value.retryable is False


def test_fetch_returns_none_when_page_has_no_usable_content():
    async def run(handler):
        client = _client(handler)
        try:
            return await client.fetch('https://example.org/page')
        finally:
            await client.aclose()

    assert asyncio.run(run(lambda request: httpx.Response(404))) is None
    page = asyncio.run(run(lambda request: httpx.Response(200, json={'title': 'T', 'content': '# text'})))
    assert (page.title, page.content) == ('T', '# text')


def test_ocr_reads_pages_in_order_and_reports_in_stream_errors():
    def ok(request):
        assert request.url.path == '/v1/tools/ocr'
        assert b'name="purpose"' in request.content and b'text_recognition' in request.content
        payload = {
            'page_count': 2,
            'pages': [
                {'index': 1, 'text': 'second', 'finish_reason': 'length'},
                {'index': 0, 'text': 'first', 'finish_reason': 'stop'},
            ],
        }
        return httpx.Response(200, text='\n\n' + json.dumps(payload))

    def failed(request):
        return httpx.Response(200, text='\n' + json.dumps({'error': {'code': 'invalid_file', 'message': 'x'}}))

    async def run(handler):
        tools = _client(handler, attempts=1)
        try:
            return await tools.ocr('scan.pdf', b'%PDF-1.4'), await DeepSeekDocumentTextClient(tools).extract_text(
                None, filename='scan.pdf', content=b'%PDF-1.4'
            )
        finally:
            await tools.aclose()

    result, text = asyncio.run(run(ok))
    assert result.pages == ('first', 'second') and result.truncated_pages == (1,)
    assert text.startswith('===== PAGE 1 =====\nfirst')
    with pytest.raises(DeepSeekToolError):
        asyncio.run(run(failed))


def test_disabled_client_never_calls_the_network():
    async def run():
        client = DeepSeekToolsClient(api_key='', transport=httpx.MockTransport(lambda request: pytest.fail('no call')))
        assert client.enabled is False
        with pytest.raises(DeepSeekToolError):
            await client.search('q')

    asyncio.run(run())
