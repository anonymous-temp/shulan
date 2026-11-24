"""Exact-byte acquisition, bounded reads and public-address-only downloads."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.analysis.document_download import HttpDocumentDownloader
from modules.analysis.application.documents import DocumentParsingError


def downloader(handler, **options):
    options.setdefault('resolver', AsyncMock(return_value=['93.184.216.34']))
    return HttpDocumentDownloader(transport=httpx.MockTransport(handler), **options)


@pytest.mark.asyncio
async def test_download_reads_complete_bytes_and_pins_validated_dns_and_tls_name():
    content = b'%PDF-1.7\n' + b'part' * 40

    def respond(request):
        assert request.url.host == '93.184.216.34'
        assert request.headers['host'] == 'example.test'
        assert request.extensions['sni_hostname'] == 'example.test'
        assert 'range' not in request.headers
        return httpx.Response(200, content=content)

    assert await downloader(respond).download('https://example.test/file.pdf') == content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'response',
    [
        httpx.Response(200, content=b''),
        httpx.Response(503),
        httpx.Response(206, content=b'partial'),
        httpx.Response(200, content=b'too big', headers={'content-length': '500'}),
        httpx.Response(200, content=b'incomplete', headers={'content-length': '15'}),
        httpx.Response(200, content=b'x' * 21),
    ],
)
async def test_unusable_or_oversized_responses_are_not_accepted(response):
    with pytest.raises(DocumentParsingError):
        await downloader(lambda r: response, max_bytes=20).download('https://example.test/file.pdf')


@pytest.mark.asyncio
async def test_rejects_private_dns_redirects_and_credentialed_urls():
    def forbidden(request):
        raise AssertionError('must not send request')

    for url in ('https://user:pass@example.test/file', 'https://example.test/file#section', 'file:///file'):
        with pytest.raises(ValueError):
            await downloader(forbidden).download(url)
    for addresses in (['127.0.0.1'], ['93.184.216.34', '10.0.0.1'], ['::ffff:93.184.216.34']):
        with pytest.raises(ValueError):
            await downloader(forbidden, resolver=AsyncMock(return_value=addresses)).download(
                'https://example.test/file'
            )
    with pytest.raises(ValueError, match='重定向'):
        await downloader(lambda r: httpx.Response(302, headers={'location': 'http://127.0.0.1'})).download(
            'https://example.test/file',
        )


@pytest.mark.asyncio
async def test_stream_and_dns_share_total_timeout_and_stream_is_closed():
    class Slow(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b'never'

        async def aclose(self):
            self.closed = True

    stream = Slow()
    with pytest.raises(DocumentParsingError):
        await downloader(lambda r: httpx.Response(200, stream=stream), timeout_seconds=0.02).download(
            'https://example.test/file',
        )
    assert stream.closed

    async def blocked(host):
        await asyncio.Event().wait()

    with pytest.raises(DocumentParsingError):
        await downloader(lambda r: None, resolver=blocked, timeout_seconds=0.01).download('https://example.test/file')
