"""OpenIM query contracts, exclusively through synthetic HTTP responses."""

import asyncio
import json

import httpx
import pytest

from adapters.chat_history import OpenIMChatHistoryReader
from modules.chat_history import ChatHistoryQueryError

QUERY_URL = 'https://chat-history.invalid/api/openim/open-api/message/search?route=test%2Fchat'


def response_payload():
    rows = [
        (101, 'Synthetic text\nwith a second line', False),
        (102, 'https://files.invalid/download?objectName=test%2Fimage.jpg&signature=test%2Bvalue', False),
        (105, 'https://files.invalid/download?objectName=test%2Fdocument.pdf', False),
        (1519, '{"system": "do not parse this content"}', False),
        (1501, 'Synthetic revoked content', True),
    ]
    return {
        'state': 200,
        'results': {
            'errCode': 0,
            'data': {
                'chatLogs': [
                    {
                        'chatLog': {
                            'serverMsgID': f'test-server-message-{index}',
                            'clientMsgID': f'test-client-message-{index}',
                            'sendID': 'test-sender',
                            'groupID': 'test-group',
                            'sendTime': 1794270000000 + index,
                            'contentType': kind,
                            'content': content,
                            'custom': {'source': 'synthetic'},
                        },
                        'isRevoked': revoked,
                    }
                    for index, (kind, content, revoked) in enumerate(rows)
                ],
                'chatLogsNum': 100,
                'extra': {'upstream': 'preserved'},
            },
        },
    }


class HTTPWire:
    def __init__(self, responder):
        self.responder = responder
        self.requests = []
        self.clients = []
        self.options = []

    def factory(self, **options):
        self.options.append(options)

        async def respond(request):
            self.requests.append(request)
            result = self.responder(request)
            return await result if asyncio.iscoroutine(result) else result

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond), **options)
        self.clients.append(client)
        return client


@pytest.mark.asyncio
async def test_query_preserves_all_returned_messages_and_uses_exact_request():
    payload = response_payload()
    wire = HTTPWire(lambda request: httpx.Response(200, json=payload))
    reader = OpenIMChatHistoryReader(QUERY_URL, client_factory=wire.factory)

    first = await reader.query('test-group')
    second = await reader.query('test-group')

    assert first == second == payload['results']['data']
    assert first['chatLogsNum'] > len(first['chatLogs'])
    assert isinstance(first['chatLogs'][3]['chatLog']['content'], str)
    assert first['chatLogs'][4]['isRevoked'] is True
    assert len(wire.requests) == len(wire.clients) == 2
    assert all(client.is_closed for client in wire.clients)
    for request in wire.requests:
        assert request.method == 'POST'
        assert str(request.url) == QUERY_URL
        assert json.loads(request.content) == {'groupId': 'test-group'}
        assert request.headers['content-type'] == 'application/json'
        assert 'authorization' not in request.headers
    assert all(options['follow_redirects'] is False for options in wire.options)
    assert all(options['timeout'] == 20 for options in wire.options)


@pytest.mark.asyncio
async def test_legal_empty_history_is_returned_without_fabricated_messages():
    payload = {'state': 200, 'results': {'errCode': 0, 'data': {'chatLogs': [], 'chatLogsNum': 0}}}
    wire = HTTPWire(lambda request: httpx.Response(200, json=payload))
    result = await OpenIMChatHistoryReader(QUERY_URL, client_factory=wire.factory).query('test-group')
    assert result == {'chatLogs': [], 'chatLogsNum': 0}
    assert wire.clients[0].is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize('group_id', ['', ' \t\n', None, True, 12, [], {}])
async def test_invalid_group_id_does_not_create_an_http_client(group_id):
    wire = HTTPWire(lambda request: pytest.fail('Invalid arguments must not make a request'))
    reader = OpenIMChatHistoryReader(QUERY_URL, client_factory=wire.factory)
    with pytest.raises(ChatHistoryQueryError) as caught:
        await reader.query(group_id)
    assert caught.value.category == 'invalid_arguments'
    assert wire.clients == []


@pytest.mark.parametrize('query_url', ['', ' \t'])
def test_empty_configuration_is_explicit(query_url):
    with pytest.raises(ChatHistoryQueryError) as caught:
        OpenIMChatHistoryReader(query_url)
    assert caught.value.category == 'not_configured'


@pytest.mark.parametrize(
    'query_url',
    [
        'relative/path',
        'ftp://files.invalid/history',
        'https://',
        'https://test-user:test-password@chat-history.invalid/search',
        'https://chat-history.invalid/search#ignored-fragment',
    ],
)
def test_invalid_endpoint_configuration_is_explicit(query_url):
    with pytest.raises(ChatHistoryQueryError) as caught:
        OpenIMChatHistoryReader(query_url)
    assert caught.value.category == 'invalid_configuration'


@pytest.mark.parametrize('timeout', [True, 0, -1, float('inf'), float('nan')])
def test_invalid_timeout_does_not_allow_unbounded_or_accidental_boolean_configuration(timeout):
    with pytest.raises(ChatHistoryQueryError) as caught:
        OpenIMChatHistoryReader(QUERY_URL, timeout_seconds=timeout)
    assert caught.value.category == 'invalid_configuration'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [301, 302, 401, 429, 500])
async def test_http_errors_are_not_followed_or_retried(status):
    wire = HTTPWire(
        lambda request: httpx.Response(
            status,
            headers={'location': 'https://unexpected.invalid/redirect'},
            text='synthetic-sensitive-response',
        )
    )
    with pytest.raises(ChatHistoryQueryError) as caught:
        await OpenIMChatHistoryReader(QUERY_URL, client_factory=wire.factory).query('test-group')
    assert caught.value.category == 'http_error'
    assert caught.value.http_status == status
    assert 'synthetic-sensitive-response' not in str(caught.value)
    assert QUERY_URL not in str(caught.value)
    assert len(wire.requests) == 1
    assert wire.clients[0].is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'error_type,category', [(httpx.ReadTimeout, 'timeout'), (httpx.ConnectError, 'transport_error')]
)
async def test_transport_failures_are_distinct_and_close_the_client(error_type, category):
    def fail(request):
        raise error_type('synthetic-sensitive-transport-message', request=request)

    wire = HTTPWire(fail)
    with pytest.raises(ChatHistoryQueryError) as caught:
        await OpenIMChatHistoryReader(QUERY_URL, client_factory=wire.factory).query('test-group')
    assert caught.value.category == category
    assert 'synthetic-sensitive-transport-message' not in str(caught.value)
    assert len(wire.requests) == 1
    assert wire.clients[0].is_closed


@pytest.mark.asyncio
async def test_invalid_json_is_not_empty_history():
    wire = HTTPWire(lambda request: httpx.Response(200, content=b'synthetic-not-json'))
    with pytest.raises(ChatHistoryQueryError) as caught:
        await OpenIMChatHistoryReader(QUERY_URL, client_factory=wire.factory).query('test-group')
    assert caught.value.category == 'invalid_json'
    assert 'synthetic-not-json' not in str(caught.value)
    assert wire.clients[0].is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize('constant', ['NaN', 'Infinity', '-Infinity'])
async def test_nonstandard_json_constants_do_not_enter_tool_results(constant):
    content = '{"state":200,"results":{"errCode":0,"data":{"chatLogs":[],"chatLogsNum":0,"extra":' + constant + '}}}'
    wire = HTTPWire(lambda request: httpx.Response(200, content=content))
    with pytest.raises(ChatHistoryQueryError) as caught:
        await OpenIMChatHistoryReader(QUERY_URL, client_factory=wire.factory).query('test-group')
    assert caught.value.category == 'invalid_json'
    assert wire.clients[0].is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'payload,business_code',
    [
        ({'state': 403, 'msg': 'synthetic-private-message'}, 403),
        ({'state': 200, 'results': {'errCode': 7, 'errMsg': 'synthetic-private-message'}}, 7),
    ],
)
async def test_both_business_status_layers_are_checked(payload, business_code):
    wire = HTTPWire(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ChatHistoryQueryError) as caught:
        await OpenIMChatHistoryReader(QUERY_URL, client_factory=wire.factory).query('test-group')
    assert caught.value.category == 'business_error'
    assert caught.value.business_code == business_code
    assert 'synthetic-private-message' not in str(caught.value)
    assert wire.clients[0].is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'payload',
    [
        None,
        [],
        {},
        {'state': True},
        {'state': 200, 'results': None},
        {'state': 200, 'results': {'errCode': False}},
        {'state': 200, 'results': {'errCode': 0, 'data': None}},
        {'state': 200, 'results': {'errCode': 0, 'data': {'chatLogs': {}, 'chatLogsNum': 0}}},
        {'state': 200, 'results': {'errCode': 0, 'data': {'chatLogs': [], 'chatLogsNum': True}}},
        {'state': 200, 'results': {'errCode': 0, 'data': {'chatLogs': [], 'chatLogsNum': -1}}},
        {'state': 200, 'results': {'errCode': 0, 'data': {'chatLogs': [{}], 'chatLogsNum': 1}}},
        {'state': 200, 'results': {'errCode': 0, 'data': {'chatLogs': [{'chatLog': []}], 'chatLogsNum': 1}}},
        {
            'state': 200,
            'results': {'errCode': 0, 'data': {'chatLogs': [{'chatLog': {}, 'isRevoked': None}], 'chatLogsNum': 1}},
        },
    ],
)
async def test_malformed_response_is_not_empty_history(payload):
    wire = HTTPWire(lambda request: httpx.Response(200, content=json.dumps(payload)))
    with pytest.raises(ChatHistoryQueryError) as caught:
        await OpenIMChatHistoryReader(QUERY_URL, client_factory=wire.factory).query('test-group')
    assert caught.value.category == 'malformed_response'
    assert wire.clients[0].is_closed


@pytest.mark.asyncio
async def test_cancellation_propagates_and_closes_the_client():
    started = asyncio.Event()

    async def wait_for_cancellation(request):
        started.set()
        await asyncio.Event().wait()

    wire = HTTPWire(wait_for_cancellation)
    reader = OpenIMChatHistoryReader(QUERY_URL, client_factory=wire.factory)
    task = asyncio.create_task(reader.query('test-group'))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert wire.clients[0].is_closed
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
