"""Endpoint guards cover fresh, cached and durable callback delivery adapters."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import httpx
import pytest

from adapters.gut_barrier.delivery import GutBarrierCallbackTransport
from adapters.gut_barrier.sources import GutBarrierProducts
from adapters.report.callback import CallbackClient
from adapters.report.callback import CallbackDeliveryError
from adapters.report.delivery import ReportCallbackTransport
from adapters.report.followup import FollowupReportHttpGateway
from modules.report.ports import FollowupGatewayError


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setattr('config.env.ENVIRONMENT', 'production')


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['durable', 'gut_barrier', 'direct'])
@pytest.mark.parametrize(
    'url',
    [
        'https://test.slmc.top/api/basic-health-assessment/ai-full-back/callback',
        'https://OLD.TEST.SLMC.TOP./callback',
        'http://reports.example.org/callback',
    ],
)
async def test_production_blocks_stale_test_callback_targets_before_http(production, kind, url):
    receive = Mock(side_effect=AssertionError('unsafe callback must not be sent'))
    http = httpx.MockTransport(receive)
    store = SimpleNamespace(delivery_render_context=AsyncMock(return_value=None))
    if kind == 'direct':
        adapter = CallbackClient(url=url, transport=http)
        send = adapter.deliver({'type': 'evaluate'})
    else:
        adapter = (
            GutBarrierCallbackTransport(store=store, transport=http)
            if kind == 'gut_barrier'
            else ReportCallbackTransport(transport=http)
        )
        send = adapter.post_once(url, {'type': 'evaluate'}, idempotency_key='saved-delivery-id')
    try:
        with pytest.raises(ValueError, match='production HTTPS endpoint'):
            await send
        receive.assert_not_called()
        store.delivery_render_context.assert_not_awaited()
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['durable', 'gut_barrier', 'direct'])
async def test_production_callback_uses_supplied_endpoint_and_payload(production, kind):
    received = []

    def receive(request):
        received.append(request)
        return httpx.Response(204)

    url = 'https://reports.example.org/callback'
    body = {'type': 'evaluate', 'report': {'value': 0}}
    http = httpx.MockTransport(receive)
    if kind == 'direct':
        adapter = CallbackClient(url=url, transport=http)
        send = adapter.deliver(body)
    else:
        store = SimpleNamespace(delivery_render_context=AsyncMock(return_value=None))
        adapter = (
            GutBarrierCallbackTransport(store=store, transport=http)
            if kind == 'gut_barrier'
            else ReportCallbackTransport(transport=http)
        )
        send = adapter.post_once(url, body, idempotency_key='delivery-id')
    try:
        await send
        assert len(received) == 1
        assert str(received[0].url) == url
        assert received[0].method == 'POST'
        assert b'"value":0' in received[0].content
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['durable', 'gut_barrier', 'direct'])
async def test_callback_redirect_cannot_forward_payload_to_test_environment(production, kind):
    received = []

    def receive(request):
        received.append(request)
        return httpx.Response(307, headers={'Location': 'https://test.slmc.top/callback'})

    url = 'https://reports.example.org/callback'
    http = httpx.MockTransport(receive)
    if kind == 'direct':
        adapter = CallbackClient(url=url, transport=http)
    else:
        store = SimpleNamespace(delivery_render_context=AsyncMock(return_value=None))
        adapter = (
            GutBarrierCallbackTransport(store=store, transport=http)
            if kind == 'gut_barrier'
            else ReportCallbackTransport(transport=http)
        )
    try:
        if kind == 'direct':
            with pytest.raises(CallbackDeliveryError):
                await adapter.deliver({'type': 'evaluate'})
        else:
            result = await adapter.post_once(url, {'type': 'evaluate'}, idempotency_key='delivery-id')
            assert result.status == 307
        assert len(received) == 1
        assert str(received[0].url) == url
    finally:
        await adapter.aclose()


class _Response:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def raise_for_status(self):
        if self.status >= 400:
            raise AssertionError('error status not used in this fixture')

    async def json(self):
        return self.payload


class _Session:
    def __init__(self, response):
        self.get = Mock(return_value=response)
        self.post = Mock(return_value=response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['catalog', 'callback'])
async def test_production_followups_block_test_endpoint_without_network(production, monkeypatch, operation):
    session = Mock(side_effect=AssertionError('unsafe endpoint must not open a session'))
    monkeypatch.setattr('adapters.report.followup.aiohttp.ClientSession', session)
    gateway = FollowupReportHttpGateway(
        callback_url='https://test.slmc.top/callback',
        product_catalog_url='https://test.slmc.top/products',
    )
    with pytest.raises(FollowupGatewayError) as failure:
        if operation == 'catalog':
            await gateway.fetch_product_catalog()
        else:
            await gateway.deliver_json({'type': 'instruction'})
    assert failure.value.retryable is False
    session.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['catalog', 'callback'])
@pytest.mark.parametrize('status', [200, 302, 307, 308])
async def test_followups_use_explicit_endpoint_and_reject_redirects(production, monkeypatch, operation, status):
    catalog = {'products': [{'name': 'configured catalog'}]}
    session = _Session(_Response(status, catalog))
    monkeypatch.setattr('adapters.report.followup.aiohttp.ClientSession', lambda: session)
    gateway = FollowupReportHttpGateway(
        callback_url='https://reports.example.org/callback',
        product_catalog_url='https://products.example.org/catalog',
    )

    async def send():
        if operation == 'catalog':
            return await gateway.fetch_product_catalog()
        return await gateway.deliver_json({'type': 'instruction'})

    if status == 200:
        assert await send() == (catalog if operation == 'catalog' else 200)
    else:
        with pytest.raises(FollowupGatewayError) as failure:
            await send()
        assert failure.value.retryable is False
    request = session.get if operation == 'catalog' else session.post
    assert request.call_count == 1
    assert request.call_args.kwargs['allow_redirects'] is False
    assert request.call_args.args == (
        'https://products.example.org/catalog' if operation == 'catalog' else 'https://reports.example.org/callback',
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'catalog_kwargs, expected_url',
    [
        ({}, 'https://configured.example.org/catalog'),
        ({'product_catalog_url': None}, 'https://configured.example.org/catalog'),
        ({'product_catalog_url': 'https://explicit.example.org/catalog'}, 'https://explicit.example.org/catalog'),
    ],
    ids=['omitted', 'none', 'explicit'],
)
async def test_followup_catalog_uses_configured_or_explicit_endpoint(monkeypatch, catalog_kwargs, expected_url):
    catalog = {'products': [{'name': 'configured catalog'}]}
    session = _Session(_Response(200, catalog))
    monkeypatch.setattr('config.env.REPORT_PRODUCT_URL', 'https://configured.example.org/catalog')
    monkeypatch.setattr('adapters.report.followup.aiohttp.ClientSession', lambda: session)
    gateway = FollowupReportHttpGateway(callback_url='https://reports.example.org/callback', **catalog_kwargs)

    assert await gateway.fetch_product_catalog() == catalog
    session.get.assert_called_once()
    assert session.get.call_args.args == (expected_url,)


@pytest.mark.parametrize('configured_url', ['', '   '])
def test_followup_catalog_has_no_endpoint_fallback_when_configuration_is_empty(monkeypatch, configured_url):
    monkeypatch.setattr('config.env.REPORT_PRODUCT_URL', configured_url)
    with pytest.raises(ValueError, match='REPORT_PRODUCT_URL'):
        FollowupReportHttpGateway(callback_url='https://reports.example.org/callback')


@pytest.mark.asyncio
async def test_production_followups_block_configured_test_catalog_before_http(production, monkeypatch):
    session = Mock(side_effect=AssertionError('unsafe endpoint must not open a session'))
    monkeypatch.setattr('config.env.REPORT_PRODUCT_URL', 'https://test.slmc.top/api/gut-product/catalog')
    monkeypatch.setattr('adapters.report.followup.aiohttp.ClientSession', session)
    gateway = FollowupReportHttpGateway(callback_url='https://reports.example.org/callback')

    with pytest.raises(FollowupGatewayError) as failure:
        await gateway.fetch_product_catalog()
    assert failure.value.retryable is False
    session.assert_not_called()


@pytest.mark.asyncio
async def test_gut_barrier_products_block_test_endpoint_before_downloading(production):
    downloader = SimpleNamespace(download=AsyncMock())
    products = GutBarrierProducts('https://test.slmc.top/api/gut-product/catalog', downloader=downloader)
    with pytest.raises(ValueError, match='REPORT_PRODUCT_URL'):
        await products.fetch()
    downloader.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_gut_barrier_products_use_configured_endpoint_and_allow_disabled_catalog(production):
    downloader = SimpleNamespace(download=AsyncMock(return_value=b'{"products": []}'))
    products = GutBarrierProducts('https://products.example.org/catalog', downloader=downloader)
    assert await products.fetch() == {'products': []}
    downloader.download.assert_awaited_once_with('https://products.example.org/catalog')
    assert await GutBarrierProducts('', downloader=downloader).fetch() is None
    assert downloader.download.await_count == 1
