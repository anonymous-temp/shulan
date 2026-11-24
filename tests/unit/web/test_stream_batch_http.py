"""Real local HTTP delivery with fake services; no model/database connections."""

import asyncio
import json
from types import SimpleNamespace

import tornado.web
from tornado.testing import AsyncHTTPTestCase, gen_test

from web.handlers.chat import ChatSendStreamHandler
from web.handlers.record import QASessionAnswerStreamHandler


def payload(kind, text='', **fields):
    return {'code': 200, 'uuid': 'request', 'data': {'type': kind, 'content': text, **fields}}


class StreamBatchHttpTest(AsyncHTTPTestCase):
    def get_app(self):
        self.service = SimpleNamespace()
        return tornado.web.Application([
            ('/chat', ChatSendStreamHandler, {'service': self.service}),
            ('/record', QASessionAnswerStreamHandler, {'service': self.service}),
        ])

    def install(self, source):
        self.service.sendMessageStream = source
        self.service.submitAnswerStream = source

    def body(self):
        return json.dumps({'userId': 'u', 'sessionId': 's', 'message': '你好',
                           'questionId': 'q', 'answer': ['是']})

    @gen_test
    async def test_cached_text_arrives_over_http_before_paused_producer_resumes(self):
        for route in ('/chat', '/record'):
            release = asyncio.Event()
            cached_received = asyncio.Event()
            wire = bytearray()

            async def source(*args, **kwargs):
                yield payload('start')
                yield payload('reasoning', '首')
                yield payload('reasoning', '缓')
                yield payload('reasoning', '存')
                await release.wait()
                yield payload('question', questionId='next-q', options=['是', '否'])
                yield {'code': 200, 'uuid': 'request', 'data': {'type': 'finish'}}

            def consume(chunk):
                wire.extend(chunk)
                if '缓存'.encode() in wire:
                    cached_received.set()

            self.install(source)
            request = self.http_client.fetch(self.get_url(route), method='POST',
                                             body=self.body(), streaming_callback=consume)
            try:
                await asyncio.wait_for(cached_received.wait(), 1)
                assert not request.done()
            finally:
                release.set()
                response = await request
            assert response.code == 200
            assert response.headers['X-Accel-Buffering'] == 'no'
            events = [json.loads(line[6:]) for line in wire.decode().splitlines() if line.startswith('data: ')]
            assert [item['data']['type'] for item in events] == [
                'start', 'reasoning', 'reasoning', 'question', 'finish',
            ]
            assert events[1]['data']['content'] == '首'
            assert events[2]['data']['content'] == '缓存'
            assert events[3]['data']['questionId'] == 'next-q'
            assert events[3]['data']['options'] == ['是', '否']
            assert events[4]['data'] == {'type': 'finish'}

    @gen_test
    async def test_exception_flushes_tail_before_existing_bare_error_over_http(self):
        for route in ('/chat', '/record'):
            async def source(*args, **kwargs):
                yield payload('reasoning', '首')
                yield payload('reasoning', '有')
                yield payload('reasoning', '效尾部')
                raise ValueError('生成失败')

            self.install(source)
            response = await self.http_client.fetch(self.get_url(route), method='POST', body=self.body())
            events = [json.loads(line[6:]) for line in response.body.decode().splitlines() if line.startswith('data: ')]
            assert events == [payload('reasoning', '首'), payload('reasoning', '有效尾部'),
                              {'type': 'error', 'error': '生成失败'}]

    @gen_test
    async def test_size_threshold_flushes_before_producer_finishes(self):
        for route in ('/chat', '/record'):
            release = asyncio.Event()
            reached_size = asyncio.Event()
            wire = bytearray()

            async def source(*args, **kwargs):
                yield payload('reasoning', '首')
                for _ in range(4):
                    yield payload('reasoning', '中' * 128)
                await release.wait()
                yield payload('finish')

            def consume(chunk):
                wire.extend(chunk)
                if ('中' * 512).encode() in wire:
                    reached_size.set()

            self.install(source)
            request = self.http_client.fetch(self.get_url(route), method='POST',
                                             body=self.body(), streaming_callback=consume)
            try:
                await asyncio.wait_for(reached_size.wait(), 1)
                assert not request.done()
            finally:
                release.set()
                await request

    @gen_test
    async def test_ten_thousand_deltas_preserve_full_text_with_fewer_wire_events(self):
        for route in ('/chat', '/record'):
            text = '中文😀e\u0301👩\u200d💻\r\n'

            async def source(*args, **kwargs):
                yield payload('start')
                for index in range(10000):
                    yield payload('reasoning', text[index % len(text)])
                yield payload('finish')

            self.install(source)
            response = await self.http_client.fetch(self.get_url(route), method='POST', body=self.body())
            events = [json.loads(line[6:]) for line in response.body.decode().splitlines() if line.startswith('data: ')]
            assert events[0] == payload('start')
            assert events[-1] == payload('finish')
            assert ''.join(item['data']['content'] for item in events[1:-1]) == ''.join(
                text[index % len(text)] for index in range(10000)
            )
            assert len(events) < 100
