"""普通聊天 Tornado API 契约测试。

测试在导入 ``server.py`` 前注入配置、数据库和服务替身，不读取 ``.env``，
也不连接 MongoDB、Redis 或真实模型。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import tornado.web
from tornado.iostream import StreamClosedError
from tornado.testing import AsyncHTTPTestCase

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _stub_module(name: str, **attributes) -> ModuleType:
    module = ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    return module


class _UnusedService:
    pass


class _UnusedCollection:
    pass


async def _unused_create_report(*args, **kwargs):
    raise AssertionError('聊天 Handler 契约测试不应生成报告')


async def _raise_async(error):
    raise error


async def _return_async(value):
    return value


def _load_server_without_environment():
    """加载 server.py，同时阻止真实配置和外部服务初始化。"""
    stubs = {
        'config.env': _stub_module(
            'config.env',
            DEBUG=False,
            ENVIRONMENT='test',
            LOG_LEVEL='CRITICAL',
            SERVER_PORT=0,
            SSE_TEXT_BATCH_CHARS=512,
            SSE_TEXT_BATCH_SECONDS=0.05,
            REPORT_CALLBACK_POLL_INTERVAL_SECONDS=5,
        ),
        'db.mongodb': _stub_module(
            'db.mongodb',
            collection_health_reports=_UnusedCollection(),
            collection_sessions=_UnusedCollection(),
        ),
        'services.analysis': _stub_module(
            'services.analysis',
            HealthReportInterpreter=_UnusedService,
            HealthReportProcessingService=_UnusedService,
            HealthReportUnsupportedFormatError=ValueError,
            HealthReportUpstreamError=RuntimeError,
        ),
        'services.chat': _stub_module(
            'services.chat',
            ChatCustomMessageConflictError=type(
                'ChatCustomMessageConflictError',
                (ValueError,),
                {},
            ),
            ChatCustomMessageValidationError=type(
                'ChatCustomMessageValidationError',
                (ValueError,),
                {},
            ),
            ChatSessionNotFoundError=type(
                'ChatSessionNotFoundError',
                (LookupError,),
                {},
            ),
            ChatSessionManager=_UnusedService,
        ),
        'services.record': _stub_module(
            'services.record',
            QASessionConflictError=type(
                'QASessionConflictError',
                (ValueError,),
                {},
            ),
            QASessionManager=_UnusedService,
        ),
        'services.report': _stub_module(
            'services.report',
            create_report=_unused_create_report,
        ),
    }
    spec = importlib.util.spec_from_file_location(
        '_shulan_server_contract_target',
        PROJECT_ROOT / 'server.py',
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


server = _load_server_without_environment()


class FakeChatManager:
    """只实现 Handler 所依赖的聊天服务协议。"""

    def __init__(self):
        self.calls = []
        # 报告引用另存，保持既有 calls 元组形状不变。
        self.report_references = []
        self.stream_error = None
        self.stream_delay = 0
        self.stream_events_consumed = []
        self.stream_persisted = False
        self.history = {
            'userId': 'user-1',
            'sessionId': 'session-1',
            'type': 'chat',
            'messages': [],
        }

    async def sendMessage(
        self,
        user_id,
        session_id,
        message,
        thinking=True,
        reasoning_effort='low',
        *,
        final_report_id=None,
        report_type=None,
    ):
        self.calls.append(('complete', user_id, session_id, message, thinking))
        self.reasoning_effort = reasoning_effort
        self.report_references.append((final_report_id, report_type))
        return {
            'userId': user_id,
            'sessionId': session_id,
            'message': message,
            'reply': '模型回复',
            'reasoning': '推理过程' if thinking else '',
            'timestamp': '2026-08-07T00:00:00',
        }

    async def insertCustomMessage(
        self,
        user_id,
        session_id,
        event_id,
        content,
        payload=None,
        *,
        display_type=None,
        custom_type=None,
        title=None,
    ):
        self.calls.append(
            (
                'custom',
                user_id,
                session_id,
                event_id,
                content,
                payload,
                display_type,
                custom_type,
                title,
            )
        )
        result = {
            'userId': user_id,
            'sessionId': session_id,
            'eventId': event_id,
            'created': True,
            'queued': False,
            'status': 'delivered',
            'message': {
                'messageId': 'message-1',
                'eventId': event_id,
                'role': 'custom',
                'displayType': display_type or 'CARD',
                'customType': custom_type or 'DOCTOR_MESSAGE_CARD',
                'content': content,
                'payload': payload or {},
                'aiContextMode': 'none',
                'timestamp': '2026-08-14T00:00:00',
            },
        }
        if title is not None:
            result['message']['title'] = title
        return result

    async def sendMessageStream(
        self,
        user_id,
        session_id,
        message,
        thinking=True,
        reasoning_effort='low',
        *,
        final_report_id=None,
        report_type=None,
    ):
        self.calls.append(('stream', user_id, session_id, message, thinking))
        self.reasoning_effort = reasoning_effort
        self.report_references.append((final_report_id, report_type))
        if self.stream_error is not None:
            raise self.stream_error
        for event_type, content in (
            ('start', None),
            ('reasoning', '推理'),
            ('content', '正文'),
            ('finish', None),
        ):
            self.stream_events_consumed.append(event_type)
            data = {
                'type': event_type,
                'userId': user_id,
                'sessionId': session_id,
            }
            if content is not None:
                data['content'] = content
            yield {
                'code': 200,
                'message': 'success',
                'uuid': 'STREAM-UUID',
                'timestamp': 1786032000,
                'elapsed_ms': 1,
                'data': data,
            }
            if self.stream_delay:
                await asyncio.sleep(self.stream_delay)
        self.stream_persisted = True

    async def getHistory(self, user_id, session_id):
        self.calls.append(('history', user_id, session_id))
        return self.history

    async def getSuggestions(self, user_id=None, count=3):
        self.calls.append(('suggestions', user_id, count))
        return [f'建议问题{i + 1}' for i in range(count)]


class ChatApiContractTest(AsyncHTTPTestCase):
    def get_app(self):
        self.chat_manager = FakeChatManager()
        return tornado.web.Application(
            [
                (r'/api/v1/chat/send/complete', server.ChatSendCompleteHandler),
                (r'/api/v1/chat/messages/custom', server.ChatCustomMessageHandler),
                (r'/api/v1/chat/send/stream', server.ChatSendStreamHandler),
                (r'/api/v1/chat/history', server.ChatHistoryHandler),
                (r'/api/v1/chat/suggestions', server.ChatSuggestionsHandler),
            ],
            chat_manager=self.chat_manager,
        )

    @staticmethod
    def _json_body(response):
        return json.loads(response.body.decode('utf-8'))

    def _post_json(self, path, body):
        return self.fetch(
            path,
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps(body, ensure_ascii=False),
        )

    def test_complete_response_envelope_and_default_thinking(self):
        response = self._post_json(
            '/api/v1/chat/send/complete',
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'message': '你好',
            },
        )

        payload = self._json_body(response)
        assert response.code == 200
        assert payload['code'] == 200
        assert payload['message'] == 'success'
        assert isinstance(payload['uuid'], str)
        assert isinstance(payload['timestamp'], int)
        assert isinstance(payload['elapsed_ms'], int)
        assert payload['data']['reply'] == '模型回复'
        assert self.chat_manager.calls == [
            ('complete', 'user-1', 'session-1', '你好', True),
        ]

    def test_explicit_thinking_is_forwarded_by_both_endpoints(self):
        for mode in ('complete', 'stream'):
            for thinking in (True, False):
                response = self._post_json(
                    f'/api/v1/chat/send/{mode}',
                    {'userId': 'user-1', 'sessionId': 'session-1', 'message': '你好', 'thinking': thinking},
                )
                assert response.code == 200
                assert self.chat_manager.calls[-1] == (mode, 'user-1', 'session-1', '你好', thinking)

    def test_effort_defaults_and_overrides_preserve_thinking(self):
        for mode in ('complete', 'stream'):
            for options in ({}, {'reasoningEffort': 'low'}, {'reasoningEffort': 'high'}, {'reasoningEffort': 'max'}):
                for thinking in (None, False):
                    body = {'userId': 'user-1', 'sessionId': 'session-1', 'message': '你好', **options}
                    if thinking is not None:
                        body['thinking'] = thinking
                    response = self._post_json(f'/api/v1/chat/send/{mode}', body)
                    assert response.code == 200
                    assert self.chat_manager.reasoning_effort == options.get('reasoningEffort', 'low')
                    assert self.chat_manager.calls[-1][-1] is (thinking is None)

    def test_invalid_effort_is_http_400_before_service_or_sse(self):
        for mode in ('complete', 'stream'):
            for effort in (None, '', 'medium', 'none', 'LOW', 1, True, [], {}):
                response = self._post_json(
                    f'/api/v1/chat/send/{mode}',
                    {
                        'userId': 'user-1',
                        'sessionId': 'session-1',
                        'message': '你好',
                        'reasoningEffort': effort,
                    },
                )
                assert response.code == 400
                assert response.headers['Content-Type'].startswith('application/json')
                assert self.chat_manager.calls == []

    def test_complete_rejects_missing_message(self):
        response = self._post_json(
            '/api/v1/chat/send/complete',
            {'userId': 'user-1', 'sessionId': 'session-1'},
        )

        payload = self._json_body(response)
        assert response.code == 400
        assert payload['code'] == 400
        assert payload['message'] == '缺少必填参数: message'
        assert payload['data'] is None

    def test_custom_message_preserves_business_role_contract(self):
        response = self._post_json(
            '/api/v1/chat/messages/custom',
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'eventId': 'report-signed-1',
                'displayType': 'CARD',
                'customType': 'HEALTH_REPORT_CARD',
                'title': '健康评估报告',
                'content': '您的健康报告已经由医生审核完成，请点击查看详情。',
                'payload': {
                    'reportId': 'report-1',
                },
                'role': 'system',
            },
        )

        payload = self._json_body(response)
        assert response.code == 200
        assert payload['data']['created'] is True
        assert payload['data']['queued'] is False
        assert payload['data']['status'] == 'delivered'
        assert payload['data']['message']['role'] == 'custom'
        assert payload['data']['message']['displayType'] == 'CARD'
        assert payload['data']['message']['customType'] == ('HEALTH_REPORT_CARD')
        assert payload['data']['message']['title'] == '健康评估报告'
        assert payload['data']['message']['aiContextMode'] == 'none'
        assert self.chat_manager.calls == [
            (
                'custom',
                'user-1',
                'session-1',
                'report-signed-1',
                '您的健康报告已经由医生审核完成，请点击查看详情。',
                {'reportId': 'report-1'},
                'CARD',
                'HEALTH_REPORT_CARD',
                '健康评估报告',
            )
        ]

    def test_custom_message_returns_202_after_persistent_queue_accepts_it(self):
        queued = {
            'userId': 'user-1',
            'sessionId': 'session-1',
            'eventId': 'report-signed-1',
            'created': True,
            'queued': True,
            'status': 'pending',
            'message': None,
        }
        self.chat_manager.insertCustomMessage = lambda *args, **kwargs: _return_async(queued)

        response = self._post_json(
            '/api/v1/chat/messages/custom',
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'eventId': 'report-signed-1',
                'displayType': 'CARD',
                'customType': 'HEALTH_REPORT_CARD',
                'content': '您的健康报告已经由医生审核完成。',
            },
        )

        payload = self._json_body(response)
        assert response.code == 202
        assert payload['code'] == 202
        assert payload['message'] == 'queued'
        assert payload['data'] == queued

    def test_custom_message_rejects_missing_and_invalid_fields(self):
        response = self._post_json(
            '/api/v1/chat/messages/custom',
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'eventId': 'report-signed-1',
            },
        )
        assert response.code == 400
        assert self._json_body(response)['message'] == ('缺少必填参数: content')

        validation_error = server.ChatCustomMessageValidationError(
            'payload 必须为对象',
        )
        self.chat_manager.insertCustomMessage = lambda *args, **kwargs: _raise_async(validation_error)
        response = self._post_json(
            '/api/v1/chat/messages/custom',
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'eventId': 'report-signed-1',
                'content': '您的健康报告已由医生审核完成。',
                'payload': [],
            },
        )
        assert response.code == 422
        assert self._json_body(response)['message'] == 'payload 必须为对象'

    def test_custom_message_maps_not_found_and_conflict(self):
        for error, status_code in (
            (
                server.ChatSessionNotFoundError('对话会话不存在'),
                404,
            ),
            (
                server.ChatCustomMessageConflictError(
                    'eventId 已存在，但消息内容不一致',
                ),
                409,
            ),
        ):
            self.chat_manager.insertCustomMessage = lambda *args, _error=error, **kwargs: _raise_async(_error)
            response = self._post_json(
                '/api/v1/chat/messages/custom',
                {
                    'userId': 'user-1',
                    'sessionId': 'session-1',
                    'eventId': 'report-signed-1',
                    'content': '您的健康报告已由医生审核完成。',
                },
            )
            assert response.code == status_code
            assert self._json_body(response)['message'] == str(error)

    def test_stream_preserves_sse_headers_and_event_order(self):
        response = self._post_json(
            '/api/v1/chat/send/stream',
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'message': '你好',
            },
        )

        frames = [
            json.loads(line.removeprefix('data: '))
            for line in response.body.decode('utf-8').splitlines()
            if line.startswith('data: ')
        ]
        assert response.code == 200
        assert response.headers['Content-Type'].startswith('text/event-stream')
        assert response.headers['Cache-Control'] == 'no-cache'
        assert response.headers['X-Accel-Buffering'] == 'no'
        assert [frame['data']['type'] for frame in frames] == [
            'start',
            'reasoning',
            'content',
            'finish',
        ]
        assert self.chat_manager.calls == [
            ('stream', 'user-1', 'session-1', '你好', True),
        ]

    def test_stream_handler_error_keeps_current_bare_error_event(self):
        self.chat_manager.stream_error = ValueError('会话不可用')

        response = self._post_json(
            '/api/v1/chat/send/stream',
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'message': '你好',
            },
        )

        frames = [
            json.loads(line.removeprefix('data: '))
            for line in response.body.decode('utf-8').splitlines()
            if line.startswith('data: ')
        ]
        assert response.code == 200
        assert frames == [{'type': 'error', 'error': '会话不可用'}]

    def test_stream_disconnect_still_consumes_and_persists_full_reply(self):
        original_flush = server.ChatSendStreamHandler.flush
        flush_calls = 0

        def close_first_flush(handler, *args, **kwargs):
            nonlocal flush_calls
            flush_calls += 1
            if flush_calls == 1:
                future = asyncio.get_running_loop().create_future()
                future.set_exception(StreamClosedError())
                return future
            return original_flush(handler, *args, **kwargs)

        with patch.object(
            server.ChatSendStreamHandler,
            'flush',
            close_first_flush,
        ):
            response = self._post_json(
                '/api/v1/chat/send/stream',
                {
                    'userId': 'user-1',
                    'sessionId': 'session-1',
                    'message': '客户端中途断开',
                },
            )

        assert response.code == 200
        assert self.chat_manager.stream_events_consumed == [
            'start',
            'reasoning',
            'content',
            'finish',
        ]
        assert self.chat_manager.stream_persisted is True
        assert self.chat_manager.calls == [
            ('stream', 'user-1', 'session-1', '客户端中途断开', True),
        ]

    def test_real_tcp_disconnect_still_finishes_background_stream(self):
        self.chat_manager.stream_delay = 0.05

        async def disconnect_after_first_event():
            reader, writer = await asyncio.open_connection(
                '127.0.0.1',
                self.get_http_port(),
            )
            body = json.dumps(
                {
                    'userId': 'user-1',
                    'sessionId': 'session-1',
                    'message': '真实连接中断',
                },
                ensure_ascii=False,
            ).encode('utf-8')
            request = (
                b'POST /api/v1/chat/send/stream HTTP/1.1\r\n'
                + f'Host: 127.0.0.1:{self.get_http_port()}\r\n'.encode()
                + b'Content-Type: application/json\r\n'
                + f'Content-Length: {len(body)}\r\n'.encode()
                + b'Connection: close\r\n\r\n'
                + body
            )
            writer.write(request)
            await writer.drain()
            await asyncio.wait_for(reader.readuntil(b'data: '), timeout=1)
            await asyncio.wait_for(reader.readuntil(b'\n\n'), timeout=1)
            writer.transport.abort()

            async def wait_until_persisted():
                while not self.chat_manager.stream_persisted:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_until_persisted(), timeout=2)

        self.io_loop.run_sync(disconnect_after_first_event, timeout=3)

        assert self.chat_manager.stream_events_consumed == [
            'start',
            'reasoning',
            'content',
            'finish',
        ]
        assert self.chat_manager.stream_persisted is True

    def test_history_success_and_not_found(self):
        response = self._post_json(
            '/api/v1/chat/history',
            {'userId': 'user-1', 'sessionId': 'session-1'},
        )
        assert response.code == 200
        assert self._json_body(response)['data'] == self.chat_manager.history

        self.chat_manager.history = None
        response = self._post_json(
            '/api/v1/chat/history',
            {'userId': 'user-1', 'sessionId': 'missing'},
        )
        assert response.code == 404
        assert self._json_body(response)['message'] == '对话会话不存在'

    def test_suggestions_count_and_validation(self):
        response = self.fetch('/api/v1/chat/suggestions?userId=user-1&count=2')
        assert response.code == 200
        assert self._json_body(response)['data'] == {
            'suggestions': ['建议问题1', '建议问题2'],
        }

        response = self.fetch('/api/v1/chat/suggestions?count=11')
        assert response.code == 400
        assert self._json_body(response)['message'] == ('参数 count 超出可选数量范围，必须在 1-10 之间')
