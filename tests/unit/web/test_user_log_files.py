"""HTTP/SSE wire contracts and user file attribution through real Tornado."""
import asyncio
import json
import logging
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import tornado.testing
import tornado.web

from tests.unit.web.test_http_logging import _Service
from utils.log import ContextFilter
from utils.log.context import log_context
from utils.log.files import FileLogConfig
from utils.log.files import FileLogHandler
from utils.log.files import FileLogWriter
from web import http_logging
from web.handlers.base import BaseHandler
from web.handlers.base import log_tornado_request
from web.handlers.chat import ChatSendStreamHandler
from web.handlers.health import LivenessHandler
from web.handlers.record import QASessionAnswerStreamHandler


class Echo(BaseHandler):
    async def post(self):
        body = json.loads(self.request.body)
        self.bind_log_identity(body)
        await asyncio.sleep(0)
        http_logging.logger.info('业务处理')
        await asyncio.to_thread(http_logging.logger.info, '线程内处理')
        if body.get('fail'):
            raise tornado.web.HTTPError(400, log_message='参数失败')
        self.write_response(200, 'success', body)


class UserLogFilesTest(tornado.testing.AsyncHTTPTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        # macOS exposes the trusted OS temporary directory through /var ->
        # /private/var. Resolve this test-owned root, not arbitrary log children;
        # the production writer must keep rejecting symlink traversal.
        self.root = Path(self.directory.name).resolve()
        self.writer = FileLogWriter(FileLogConfig(self.root))
        self.addCleanup(self.writer.close)
        sink = FileLogHandler(self.writer)
        sink.addFilter(ContextFilter())
        sink.setFormatter(logging.Formatter(
            'user_id=%(user_id)s request_uuid=%(request_uuid)s %(message)s',
        ))
        self.logger = logging.Logger('user-files-http', logging.INFO)
        self.logger.addHandler(sink)
        for target in ('web.http_logging.logger', 'web.handlers.base.logger',
                       'web.handlers.chat.logger', 'web.handlers.record.logger'):
            patcher = patch(target, self.logger)
            patcher.start()
            self.addCleanup(patcher.stop)
        super().setUp()

    def get_app(self):
        self.service = _Service()
        return tornado.web.Application([
            ('/echo', Echo),
            ('/health/live', LivenessHandler),
            ('/chat', ChatSendStreamHandler, {'service': self.service}),
            ('/qa', QASessionAnswerStreamHandler, {'service': self.service}),
        ], log_function=log_tornado_request)

    def text(self, name):
        self.writer.flush()
        path = self.root / name
        return path.read_text(encoding='utf-8') if path.exists() else ''

    def post(self, path, body):
        return self.fetch(path, method='POST', body=json.dumps(body),
                          headers={'Content-Type': 'application/json'})

    @tornado.testing.gen_test
    async def test_concurrent_users_threads_error_and_correlation(self):
        async def send(user, fail=False):
            response = await self.http_client.fetch(self.get_url('/echo'), method='POST',
                body=json.dumps({'userId': user, 'fail': fail}), raise_error=False)
            return user, json.loads(response.body)
        results = await asyncio.gather(send('a'), send('b', True))
        assert results[0][1]['data']['userId'] == 'a'
        assert results[1][1]['code'] == 400
        for user, response in results:
            text = self.text(f'user/{user}/user.log')
            assert '业务处理' in text and '线程内处理' in text
            assert 'HTTP 请求开始' in text and '响应完成' in text
            assert f'request_uuid="{response["uuid"]}"' in text
            assert all(line.startswith(f'user_id="{user}"') for line in text.splitlines())
        assert not self.text('system/system.log')
        assert log_context().user_id is None

    def test_body_disabled_still_routes_and_large_body_binds_after_parse(self):
        with patch.object(http_logging.env, 'HTTP_LOG_BODY_ENABLED', False):
            response = self.post('/echo', {'userId': 'off', 'text': 'private body'})
        assert response.code == 200
        assert 'private body' not in self.text('user/off/user.log')
        assert '响应完成' in self.text('user/off/user.log')
        with patch.object(http_logging.env, 'HTTP_LOG_PARSE_BYTES', 10):
            response = self.post('/echo', {'userId': 'large', 'text': 'x' * 1000})
        assert len(json.loads(response.body)['data']['text']) == 1000
        assert 'HTTP 请求开始' in self.text('system/system.log')
        assert '响应完成' in self.text('user/large/user.log')

    def test_health_and_invalid_json_do_not_inherit_previous_user(self):
        assert self.post('/echo', {'userId': 'previous'}).code == 200
        health = self.fetch('/health/live')
        assert json.loads(health.body)['data']['status'] == 'alive'
        invalid = self.fetch('/chat', method='POST', body='{"userId":',
                             headers={'Content-Type': 'application/json'})
        assert invalid.code == 400
        system = self.text('system/system.log')
        assert 'path=/health/live' in system and 'path=/chat' in system
        assert all(line.startswith('user_id=-') for line in system.splitlines())
        assert 'path=/health/live' not in self.text('user/previous/user.log')
        assert system.count('响应完成') == 2

    def test_query_identity_and_invalid_identity_are_observational(self):
        assert self.fetch('/health/live?userId=query-user&sessionId=session').code == 200
        assert 'path=/health/live' in self.text('user/query-user/user.log')
        payload = {'userId': ['invalid'], 'sessionId': {'invalid': True}}
        response = self.post('/echo', payload)
        assert json.loads(response.body)['data'] == payload
        assert '业务处理' in self.text('system/system.log')
        assert 'path=/echo' not in self.text('user/query-user/user.log')

    def test_sse_summary_at_info_and_abbreviation_for_chat_and_qa(self):
        self.service.content = '开始' + '中间' * 100 + '结束'
        with patch.object(http_logging, 'LIMITS', replace(http_logging.LIMITS, string_chars=8)):
            for path in ('/chat', '/qa'):
                user = path[1:]
                response = self.post(path, {'userId': user, 'sessionId': 's', 'message': 'hi',
                                            'questionId': 'q', 'answer': 'yes'})
                assert self.service.content in response.body.decode()
                text = self.text(f'user/{user}/user.log')
                assert 'body_format=sse_summary' in text and '***' in text
                assert 'HTTP 流式事件:' not in text
                assert text.count('响应完成') == 1
                body = json.loads(text.split(' body=', 1)[1])
                assert body['data']['content'].startswith('开始')
                assert body['data']['content'].endswith('结束')
        assert not self.text('system/system.log')

    @tornado.testing.gen_test
    async def test_disconnect_callback_and_background_summary_keep_user(self):
        self.service.delay = 0.03
        reader, writer = await asyncio.open_connection('127.0.0.1', self.get_http_port())
        body = json.dumps({'userId': 'disconnect', 'sessionId': 's', 'message': 'hi'}).encode()
        writer.write(b'POST /chat HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n'
                     + f'Content-Length: {len(body)}\r\n\r\n'.encode() + body)
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b'data: '), 2)
        await asyncio.wait_for(reader.readuntil(b'\n\n'), 2)
        writer.transport.abort()
        for _ in range(100):
            text = self.text('user/disconnect/user.log')
            if self.service.saved and '响应完成' in text:
                break
            await asyncio.sleep(0.01)
        assert self.service.saved
        assert '客户端已断开' in text
        assert 'disconnected=True' in text and text.count('响应完成') == 1
        assert not self.text('system/system.log')
