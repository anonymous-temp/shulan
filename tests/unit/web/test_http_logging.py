"""Real Tornado responses and streams are unchanged by diagnostic previews."""

import asyncio
import json
import logging
from dataclasses import replace
from unittest.mock import patch

import tornado.testing
import tornado.web
from tornado.iostream import StreamClosedError

from runtime import ApplicationLifecycle
from web import http_logging
from web.handlers.base import BaseHandler
from web.handlers.base import log_tornado_request
from web.handlers.chat import ChatSendStreamHandler
from web.handlers.health import HealthCheckHandler
from web.handlers.health import LivenessHandler
from web.handlers.health import ReadinessHandler
from web.handlers.record import QASessionAnswerStreamHandler
from web.handlers.report import ReportGenerateHandler


class _Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class _Echo(BaseHandler):
    async def post(self):
        body = json.loads(self.request.body)
        self.write_response(body.get('code', 200), 'success', body)


class _Fail(BaseHandler):
    async def post(self):
        self.write_response(200, 'discarded', {'obsolete': True})
        raise tornado.web.HTTPError(500, log_message='最终错误')


class _Accept(BaseHandler):
    async def post(self):
        self.write_response(200, 'success', {'bytes': len(self.request.body)})


class _Chunks(BaseHandler):
    async def post(self):
        self.set_header('Content-Type', 'text/plain')
        for _ in range(50):
            self.write('正文' * 1000)


class _Report(ReportGenerateHandler):
    def initialize(self):
        pass

    async def post(self):
        body = json.loads(self.request.body)
        if body.get('fail'):
            raise tornado.web.HTTPError(400, log_message='报告参数错误')
        self.write_response(200, '已受理', {'status': 'processing'})
        await self.finish()


class _Service:
    def __init__(self):
        self.saved = False
        self.error_event = False
        self.content = '中文流内容'
        self.events = None
        self.failure = None
        self.delay = 0

    async def _events(self):
        events = self.events
        if events is None:
            events = [
                {'code': 200, 'data': {'type': event_type, 'content': self.content}}
                for event_type in ('start', 'content', 'error' if self.error_event else 'finish')
            ]
        for event in events:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield event
        if self.failure:
            raise self.failure
        self.saved = True

    def sendMessageStream(self, *args, **kwargs):
        return self._events()

    def submitAnswerStream(self, *args, **kwargs):
        return self._events()


class HttpLoggingTest(tornado.testing.AsyncHTTPTestCase):
    def setUp(self):
        self.records = _Records()
        self.logger = logging.Logger('http-preview-test', logging.DEBUG)
        self.logger.addHandler(self.records)
        self.patches = [
            patch.object(http_logging, 'logger', self.logger),
            patch('web.handlers.base.logger', self.logger),
            patch.object(http_logging.env, 'HTTP_LOG_BODY_ENABLED', True),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        super().setUp()

    def get_app(self):
        self.lifecycle = ApplicationLifecycle()
        self.lifecycle.mark_ready()
        self.service = _Service()
        return tornado.web.Application(
            [
                ('/echo', _Echo),
                ('/fail', _Fail),
                ('/report', _Report),
                ('/accept', _Accept),
                ('/chunks', _Chunks),
                ('/health', HealthCheckHandler),
                ('/health/live', LivenessHandler),
                ('/health/ready', ReadinessHandler),
                ('/chat', ChatSendStreamHandler, {'service': self.service}),
                ('/qa', QASessionAnswerStreamHandler, {'service': self.service}),
            ],
            lifecycle=self.lifecycle,
            log_function=log_tornado_request,
        )

    def post(self, path='/echo', body=None):
        return self.fetch(
            path, method='POST', headers={'Content-Type': 'application/json'}, body=json.dumps(body or {})
        )

    def messages(self):
        return [record.getMessage() for record in self.records.records]

    def test_request_response_full_values_and_correlation(self):
        data = {'userId': '用户1', 'message': 'hello\nworld', 'password': 'secret'}
        response = self.post('/echo?tag=a&tag=b&token=hidden', data)
        returned = json.loads(response.body)
        assert returned['data'] == data
        start, finish = self.messages()
        assert returned['uuid'] in start and returned['uuid'] in finish
        assert json.loads(start.split(' request=', 1)[1]) == {
            'query': {'tag': ['a', 'b'], 'token': '[REDACTED]'},
            'body': {**data, 'password': '[REDACTED]'},
        }
        assert json.loads(finish.split(' body=', 1)[1])['data']['message'] == data['message']
        assert 'secret' not in start + finish
        assert all(len(message.splitlines()) == 1 for message in (start, finish))

    def test_long_reply_is_not_truncated_on_wire(self):
        data = {'message': '前' + '中' * 10000 + '后', 'items': list(range(100))}
        response = self.post(body=data)
        assert json.loads(response.body)['data'] == data
        assert all('***' in message and '省略' not in message for message in self.messages())
        logged = json.loads(self.messages()[-1].split(' body=', 1)[1])['data']
        assert logged['items'] == [*range(20), '***']

    def test_smallest_budget_and_multiple_writes_remain_bounded(self):
        with patch.object(http_logging, 'LIMITS', replace(http_logging.LIMITS, total_bytes=1024)):
            response = self.post('/chunks')
        assert response.body.decode() == '正文' * 50000
        access = self.messages()[-1]
        assert 'body_format=chunks' in access
        body = access.split(' body=', 1)[1]
        assert len(body.encode()) <= 1024
        assert '正文' in body and '***' in body and '省略' not in body
        assert isinstance(json.loads(body), list)

    def test_raw_empty_and_invalid_json_requests_still_return(self):
        for raw, expected in ((b'', 'empty'), (b'{invalid', 'text'), (b'null', None), (b'""', ''), (b'[]', [])):
            self.records.records.clear()
            response = self.fetch('/accept', method='POST', headers={'Content-Type': 'application/json'}, body=raw)
            assert response.code == 200
            body = json.loads(self.messages()[0].split(' request=', 1)[1])['body']
            if isinstance(expected, str) and expected in {'empty', 'text'}:
                assert body['format'] == expected
            else:
                assert body == expected

    def test_oversized_request_skips_json_parse_but_reaches_handler_unchanged(self):
        raw = b'{"text":"' + b'x' * 10000 + b'"}'
        with patch.object(http_logging.env, 'HTTP_LOG_PARSE_BYTES', 1024):
            response = self.fetch('/accept', method='POST', headers={'Content-Type': 'application/json'}, body=raw)
        assert json.loads(response.body)['data']['bytes'] == len(raw)
        body = json.loads(self.messages()[0].split(' request=', 1)[1])['body']
        assert body['parse_skipped'] is True
        assert body['bytes'] == len(raw)
        assert body['preview'].count('***') == 1
        assert '省略' not in body['preview'] and '字节' not in body['preview']
        sampled = http_logging._raw_body(raw, 'application/json')
        assert sampled['preview'].count('***') == 1
        assert '省略' not in sampled['preview']

    def test_form_and_binary_request_previews(self):
        self.fetch(
            '/accept',
            method='POST',
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            body='name=a&name=b&password=hidden',
        )
        body = json.loads(self.messages()[0].split(' request=', 1)[1])['body']
        assert body == {'name': ['a', 'b'], 'password': '[REDACTED]'}
        self.records.records.clear()
        self.fetch('/accept', method='POST', headers={'Content-Type': 'application/octet-stream'}, body=b'not printed')
        body = json.loads(self.messages()[0].split(' request=', 1)[1])['body']
        assert body == {'format': 'binary', 'bytes': 11, 'content_type': 'application/octet-stream'}
        self.records.records.clear()
        self.fetch(
            '/accept', method='POST', headers={'Content-Type': 'application/octet-stream'}, body=b'{"file":"contents"}'
        )
        body = json.loads(self.messages()[0].split(' request=', 1)[1])['body']
        assert body['format'] == 'binary'
        assert 'contents' not in self.messages()[0]

    def test_multipart_logs_file_metadata_without_contents(self):
        raw = (
            b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="report.pdf"\r\n'
            b'Content-Type: application/pdf\r\n\r\nprivate-file-content\r\n--boundary--\r\n'
        )
        response = self.fetch(
            '/accept', method='POST', headers={'Content-Type': 'multipart/form-data; boundary=boundary'}, body=raw
        )
        assert response.code == 200
        log = self.messages()[0]
        assert 'private-file-content' not in log
        body = json.loads(log.split(' request=', 1)[1])['body']
        assert body['file_count'] == 1
        assert body['files'] == [
            {'field': 'file', 'filename': 'report.pdf', 'content_type': 'application/pdf', 'bytes': 20}
        ]

    def test_final_error_replaces_discarded_response_preview(self):
        response = self.post('/fail')
        assert response.code == 500
        logs = '\n'.join(self.messages())
        assert 'obsolete' not in logs and 'discarded' not in logs
        assert '最终错误' in logs
        assert logs.count('响应完成') == 1
        assert self.lifecycle.active_request_count == 0

    def test_report_success_and_error_use_same_logging_boundary(self):
        for data, status in (({}, 200), ({'fail': True}, 400)):
            self.records.records.clear()
            response = self.post('/report', data)
            assert response.code == status
            access = next(message for message in self.messages() if '响应完成' in message)
            assert json.loads(access.split(' body=', 1)[1]) == json.loads(response.body)

    def test_draining_rejection_still_logs_request_and_503_response(self):
        self.lifecycle.begin_draining()
        response = self.post(body={'message': 'draining request'})
        assert response.code == 503
        assert 'draining request' in self.messages()[0]
        assert 'status_code=503' in self.messages()[-1]
        assert json.loads(self.messages()[-1].split(' body=', 1)[1])['code'] == 503

    def test_business_code_is_logged_separately_from_http_status(self):
        response = self.post(body={'code': 409})
        assert response.code == 200
        assert 'status_code=200 code=409' in self.messages()[-1]
        assert self.records.records[-1].levelno == logging.WARNING

    def test_preview_or_log_sink_failure_does_not_break_requests(self):
        with patch.object(http_logging, 'preview', side_effect=RuntimeError('broken preview')):
            response = self.post(body={'value': 'still returned'})
        assert response.code == 200
        assert json.loads(response.body)['data']['value'] == 'still returned'
        with patch.object(self.records, 'emit', side_effect=RuntimeError('broken sink')):
            assert self.post().code == 200
        assert self.lifecycle.active_request_count == 0

    def test_disabled_body_and_options_do_not_build_previews(self):
        with (
            patch.object(http_logging.env, 'HTTP_LOG_BODY_ENABLED', False),
            patch.object(http_logging, 'preview') as render,
        ):
            assert self.post().code == 200
            render.assert_not_called()
        self.records.records.clear()
        with patch.object(http_logging, 'preview') as render:
            assert self.fetch('/echo', method='OPTIONS').code == 204
            render.assert_not_called()
        assert len(self.messages()) == 1

    def test_health_endpoints_log_real_get_request_and_complete_response(self):
        self.logger.setLevel(logging.INFO)
        for path in ('/health', '/health/live', '/health/ready'):
            self.records.records.clear()
            response = self.fetch(path)
            assert response.code == 200
            returned = json.loads(response.body)
            start, finish = self.messages()
            assert f'method=GET path={path} ' in start
            assert returned['uuid'] in start and returned['uuid'] in finish
            assert json.loads(start.split(' request=', 1)[1]) == {
                'query': {},
                'body': {'format': 'empty', 'bytes': 0},
            }
            assert 'status_code=200 code=200' in finish
            assert json.loads(finish.split(' body=', 1)[1]) == returned
            assert returned['data']['ready'] is True

    def test_unready_health_response_keeps_503_body_in_error_log(self):
        self.lifecycle.begin_draining()
        response = self.fetch('/health/ready')
        assert response.code == 503
        start, finish = self.messages()
        assert 'HTTP 请求开始' in start
        assert 'status_code=503 code=503' in finish
        assert json.loads(finish.split(' body=', 1)[1]) == json.loads(response.body)
        assert self.records.records[-1].levelno == logging.ERROR

    def test_health_body_switch_still_skips_previews_but_keeps_business_code(self):
        with (
            patch.object(http_logging.env, 'HTTP_LOG_BODY_ENABLED', False),
            patch.object(http_logging, 'preview') as render,
        ):
            assert self.fetch('/health/live').code == 200
            render.assert_not_called()
        start, finish = self.messages()
        assert ' request=' not in start and ' body=' not in finish
        assert 'status_code=200 code=200' in finish

    def test_sse_debug_events_preserve_both_stream_contracts(self):
        body = {'userId': 'u', 'sessionId': 's', 'message': 'hello', 'answer': 'yes', 'questionId': 'q'}
        for path in ('/chat', '/qa'):
            self.records.records.clear()
            response = self.post(path, body)
            frames = [json.loads(line[6:]) for line in response.body.decode().splitlines() if line.startswith('data: ')]
            assert [frame['data']['type'] for frame in frames] == ['start', 'content', 'finish']
            events = [record for record in self.records.records if 'HTTP 流式事件:' in record.getMessage()]
            assert [json.loads(record.getMessage().split(' body=', 1)[1]) for record in events] == frames
            assert all(record.levelno == logging.DEBUG for record in events)
            assert all(f'seq={index}' in record.getMessage() for index, record in enumerate(events, 1))
            assert 'stream_events=3 flushed_events=3 disconnected=False' in self.messages()[-1]
            assert self.service.saved

    def test_info_level_builds_final_sse_body_without_per_event_debug_previews(self):
        self.logger.setLevel(logging.INFO)
        with patch.object(http_logging, 'preview', wraps=http_logging.preview) as render:
            response = self.post('/chat', {'userId': 'u', 'sessionId': 's', 'message': 'hello'})
        assert response.code == 200
        # One request preview and one final summary; no per-event DEBUG rendering.
        assert render.call_count == 2
        assert not any('HTTP 流式事件:' in message for message in self.messages())
        assert 'body_format=sse_summary' in self.messages()[-1]
        assert json.loads(self.messages()[-1].split(' body=', 1)[1])['data']['content'] == self.service.content

    def test_sse_long_text_is_shortened_only_in_logs_for_both_handlers(self):
        self.service.content = '开始' + '内容' * 100 + '结束'
        request = {'userId': 'u', 'sessionId': 's', 'message': 'hello', 'answer': 'yes', 'questionId': 'q'}
        for path in ('/chat', '/qa'):
            self.records.records.clear()
            with patch.object(http_logging, 'LIMITS', replace(http_logging.LIMITS, string_chars=32)):
                response = self.post(path, request)
            frames = [json.loads(line[6:]) for line in response.body.decode().splitlines() if line.startswith('data: ')]
            assert all(frame['data']['content'] == self.service.content for frame in frames)
            events = [message for message in self.messages() if 'HTTP 流式事件:' in message]
            assert len(events) == len(frames) == 3
            expected = self.service.content[:16] + '***' + self.service.content[-16:]
            assert all(json.loads(event.split(' body=', 1)[1])['data']['content'] == expected for event in events)
            assert json.loads(self.messages()[-1].split(' body=', 1)[1])['data']['content'] == expected
            assert self.service.saved

    def test_stream_error_is_visible_at_info_with_final_body(self):
        self.logger.setLevel(logging.INFO)
        self.service.error_event = True
        assert self.post('/chat', {'userId': 'u', 'sessionId': 's', 'message': 'hello'}).code == 200
        assert any('HTTP 流式错误:' in message for message in self.messages())
        assert 'stream_errors=1' in self.messages()[-1]
        assert json.loads(self.messages()[-1].split(' body=', 1)[1])['data']['type'] == 'error'
        assert self.records.records[-1].levelno == logging.ERROR

    def test_info_summary_combines_real_envelopes_and_preserves_question_result(self):
        self.logger.setLevel(logging.INFO)
        base = {'code': 200, 'message': 'success', 'uuid': 'SERVICE-UUID', 'timestamp': 123, 'elapsed_ms': 1}
        self.service.events = [
            {**base, 'data': {'type': 'start', 'userId': 'u', 'sessionId': 's'}},
            {**base, 'data': {'type': 'reasoning', 'content': '思考'}},
            {**base, 'data': {'type': 'reasoning', 'content': '过程'}},
            {**base, 'data': {'type': 'content', 'content': '完整'}},
            {**base, 'data': {'type': 'content', 'content': '回复'}},
            {**base, 'data': {'type': 'question', 'questionId': 'q2', 'question': '如何？', 'options': ['甲', '乙']}},
            {**base, 'timestamp': 124, 'elapsed_ms': 10, 'data': {'type': 'finish'}},
        ]
        request = {'userId': 'u', 'sessionId': 's', 'message': 'hello', 'answer': 'yes', 'questionId': 'q'}
        for path in ('/chat', '/qa'):
            self.records.records.clear()
            response = self.post(path, request)
            frames = [json.loads(line[6:]) for line in response.body.decode().splitlines() if line.startswith('data: ')]
            assert frames == self.service.events
            finish = [message for message in self.messages() if '响应完成' in message]
            assert len(finish) == 1
            assert 'code=200' in finish[0] and 'stream_state=finished' in finish[0]
            result = json.loads(finish[0].split(' body=', 1)[1])
            assert result == {
                **base,
                'timestamp': 124,
                'elapsed_ms': 10,
                'data': {
                    'type': 'finish',
                    'userId': 'u',
                    'sessionId': 's',
                    'reasoning': '思考过程',
                    'content': '完整回复',
                    'questionId': 'q2',
                    'question': '如何？',
                    'options': ['甲', '乙'],
                },
            }
            assert 'request_uuid=SERVICE-UUID' not in finish[0]

    def test_empty_and_unfinished_streams_are_not_reported_as_successful_finish(self):
        self.logger.setLevel(logging.INFO)
        for events, state in (([], 'empty'), ([{'data': {'type': 'content', 'content': '片段'}}], 'incomplete')):
            self.records.records.clear()
            self.service.events = events
            assert self.post('/chat', {'userId': 'u', 'sessionId': 's', 'message': 'hello'}).code == 200
            finish = self.messages()[-1]
            assert f'stream_state={state}' in finish and 'code=-' in finish
            result = json.loads(finish.split(' body=', 1)[1])
            assert 'code' not in result
            if not events:
                assert result == {}
            else:
                assert result['data'] == {'type': 'content', 'content': '片段'}

    def test_bare_handler_error_retains_partial_content_and_no_fake_business_code(self):
        self.logger.setLevel(logging.INFO)
        self.service.events = [{'code': 200, 'message': 'success', 'data': {'type': 'content', 'content': '片段'}}]
        self.service.failure = ValueError('中途失败')
        assert self.post('/chat', {'userId': 'u', 'sessionId': 's', 'message': 'hello'}).code == 200
        finish = self.messages()[-1]
        assert 'stream_state=error' in finish and 'code=-' in finish
        result = json.loads(finish.split(' body=', 1)[1])
        assert result == {'data': {'type': 'error', 'content': '片段', 'error': '中途失败'}}

    def test_business_error_code_and_level_survive_later_success_event(self):
        self.logger.setLevel(logging.INFO)
        self.service.events = [
            {'code': 409, 'message': '冲突', 'data': {'type': 'error', 'error': '冲突'}},
            {'code': 200, 'message': 'success', 'data': {'type': 'finish'}},
        ]
        assert self.post('/chat', {'userId': 'u', 'sessionId': 's', 'message': 'hello'}).code == 200
        finish = self.messages()[-1]
        assert 'status_code=200 code=409' in finish and 'stream_state=error' in finish
        assert self.records.records[-1].levelno == logging.WARNING
        assert json.loads(finish.split(' body=', 1)[1]) == self.service.events[0]

    def test_disabled_body_never_allocates_stream_summary(self):
        with (
            patch.object(http_logging.env, 'HTTP_LOG_BODY_ENABLED', False),
            patch.object(http_logging, 'StreamLogSummary') as summary,
        ):
            assert self.post('/chat', {'userId': 'u', 'sessionId': 's', 'message': 'hello'}).code == 200
            summary.assert_not_called()
        assert ' body=' not in self.messages()[-1]
        assert 'code=200' in self.messages()[-1]

    def test_summary_failure_cannot_interrupt_stream_or_persistence(self):
        with patch.object(http_logging.StreamLogSummary, 'observe', side_effect=RuntimeError('log failure')):
            response = self.post('/chat', {'userId': 'u', 'sessionId': 's', 'message': 'hello'})
        assert response.code == 200 and self.service.saved
        assert response.body.count(b'data: ') == 3

    def test_disconnect_logs_failed_and_skipped_events_without_stopping_persistence(self):
        original_flush = ChatSendStreamHandler.flush
        calls = 0

        def fail_first(handler, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                future = asyncio.get_running_loop().create_future()
                future.set_exception(StreamClosedError())
                return future
            return original_flush(handler, *args, **kwargs)

        with patch.object(ChatSendStreamHandler, 'flush', fail_first):
            response = self.post('/chat', {'userId': 'u', 'sessionId': 's', 'message': 'hello'})
        assert response.code == 200
        assert self.service.saved
        events = [message for message in self.messages() if 'HTTP 流式事件:' in message]
        assert 'outcome=flush_failed' in events[0]
        assert all('outcome=skipped_disconnected' in message for message in events[1:])
        assert 'flushed_events=0 disconnected=True' in self.messages()[-1]
        assert len([message for message in self.messages() if '响应完成' in message]) == 1
        assert json.loads(self.messages()[-1].split(' body=', 1)[1])['data']['content'] == self.service.content

    def test_real_disconnect_logs_background_result_once_without_debug(self):
        self.logger.setLevel(logging.INFO)
        self.service.delay = 0.03

        async def disconnect():
            reader, writer = await asyncio.open_connection('127.0.0.1', self.get_http_port())
            body = json.dumps({'userId': 'u', 'sessionId': 's', 'message': 'hello'}).encode()
            writer.write(
                b'POST /chat HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n'
                + f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()
                + body
            )
            await writer.drain()
            await asyncio.wait_for(reader.readuntil(b'data: '), 2)
            await asyncio.wait_for(reader.readuntil(b'\n\n'), 2)
            writer.transport.abort()
            for _ in range(100):
                if self.service.saved and any('响应完成' in message for message in self.messages()):
                    return
                await asyncio.sleep(0.01)
            raise AssertionError('Missing background persistence or completion log')

        self.io_loop.run_sync(disconnect, timeout=4)
        finish = [message for message in self.messages() if '响应完成' in message]
        assert len(finish) == 1
        assert 'disconnected=True' in finish[0]
        assert json.loads(finish[0].split(' body=', 1)[1])['data']['content'] == self.service.content

    def test_completion_log_waits_for_final_flush_failure(self):
        self.logger.setLevel(logging.INFO)
        original_flush = ChatSendStreamHandler.flush

        def fail_final_flush(handler, *args, **kwargs):
            result = original_flush(handler, *args, **kwargs)
            if kwargs.get('include_footers'):
                failed = asyncio.get_running_loop().create_future()
                failed.set_exception(StreamClosedError())
                return failed
            return result

        with patch.object(ChatSendStreamHandler, 'flush', fail_final_flush):
            assert self.post('/chat', {'userId': 'u', 'sessionId': 's', 'message': 'hello'}).code == 200
        finish = [message for message in self.messages() if '响应完成' in message]
        assert len(finish) == 1
        assert 'disconnected=True' in finish[0]
        assert self.service.saved
