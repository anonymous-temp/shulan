"""健康建档 Tornado API 的无外部依赖契约测试。

测试在导入 ``server.py`` 前注入配置、数据库和服务替身，不读取 ``.env``，
也不连接 MongoDB、Redis 或真实模型。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import tornado.web
from tornado.iostream import StreamClosedError
from tornado.testing import AsyncHTTPTestCase

from adapters.record.documents import LegacyRecordDocumentAdapter
from agents import AgentHub
from agents import GraphRegistry
from modules.analysis.application.documents import DocumentParsingError
from modules.analysis.application.documents import DocumentSubmissionConflictError
from modules.analysis.application.service import HealthReportUpstreamError

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


class _HealthProfileError(RuntimeError):
    pass


class _HealthReportSourceConflictError(RuntimeError):
    pass


class _HealthReportUnsupportedFormatError(ValueError):
    pass


class _HealthReportUpstreamError(RuntimeError):
    pass


async def _unused_create_report(*args, **kwargs):
    raise AssertionError('建档 Handler 契约测试不应生成报告')


def _load_server_without_environment():
    """加载 server.py，同时阻止真实配置和外部服务初始化。"""
    stubs = {
        'config.env': _stub_module(
            'config.env',
            DEBUG=False,
            ENVIRONMENT='test',
            LOG_LEVEL='CRITICAL',
            REPORT_CALLBACK_POLL_INTERVAL_SECONDS=1,
            SERVER_PORT=0,
            SSE_TEXT_BATCH_CHARS=512,
            SSE_TEXT_BATCH_SECONDS=0.05,
        ),
        'db.mongodb': _stub_module(
            'db.mongodb',
            collection_health_reports=_UnusedCollection(),
            collection_sessions=_UnusedCollection(),
        ),
        'health_profile': _stub_module(
            'health_profile',
            HealthProfileConflictError=_HealthProfileError,
            HealthProfileNotFoundError=_HealthProfileError,
            HealthProfileValidationError=_HealthProfileError,
            HealthReportSourceConflictError=(_HealthReportSourceConflictError),
        ),
        'services.analysis': _stub_module(
            'services.analysis',
            HealthReportInterpreter=_UnusedService,
            HealthReportProcessingService=_UnusedService,
            HealthReportUnsupportedFormatError=(_HealthReportUnsupportedFormatError),
            HealthReportUpstreamError=_HealthReportUpstreamError,
        ),
        'services.chat': _stub_module(
            'services.chat',
            ChatCustomMessageConflictError=ValueError,
            ChatCustomMessageValidationError=ValueError,
            ChatSessionNotFoundError=LookupError,
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
        '_shulan_record_contract_target',
        PROJECT_ROOT / 'server.py',
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


server = _load_server_without_environment()
record_handler_module = server._record_handlers


class FakeQAManager:
    """只实现非流式建档 Handler 依赖的服务协议。"""

    def __init__(self) -> None:
        self.calls = []
        self.stream_events_consumed = []
        self.stream_persisted = False

    async def createSession(
        self,
        user_id,
        session_id,
        **health_data,
    ):
        self.calls.append(('create', user_id, session_id, health_data))
        return {
            'userId': user_id,
            'sessionId': session_id,
            'status': 'question',
            'progress': '33%',
            'questionId': 'q_sleep',
            'question': '您的睡眠如何？',
            'questionType': 'radio',
            'options': ['良好', '较差'],
        }

    async def submitAnswer(
        self,
        user_id,
        session_id,
        question_id,
        answer,
        other,
        *,
        thinking,
        reasoning_effort,
    ):
        self.reasoning_effort = reasoning_effort
        self.calls.append(
            (
                'answer',
                user_id,
                session_id,
                question_id,
                answer,
                other,
                thinking,
            )
        )
        return {
            'userId': user_id,
            'sessionId': session_id,
            'status': 'question',
            'progress': '40%',
            'questionId': 'q_sleep',
            'question': '您的睡眠如何？',
            'questionType': 'radio',
            'options': ['良好', '一般', '较差'],
            'reasoning': '优先了解睡眠',
            'intakeQuestionCount': 1,
            'qaRoundCount': 0,
        }

    async def submitAnswerStream(
        self,
        user_id,
        session_id,
        question_id,
        answer,
        other,
        *,
        thinking,
        reasoning_effort,
    ):
        self.reasoning_effort = reasoning_effort
        self.calls.append(
            (
                'answer-stream',
                user_id,
                session_id,
                question_id,
                answer,
                other,
                thinking,
            )
        )
        for event_type in ('start', 'reasoning', 'question', 'finish'):
            self.stream_events_consumed.append(event_type)
            yield {'data': {'type': event_type}}
        self.stream_persisted = True

    async def continueSession(self, user_id, session_id, round_number, *, thinking):
        self.calls.append(
            (
                'continue',
                user_id,
                session_id,
                round_number,
                thinking,
            )
        )
        raise record_handler_module.QASessionConflictError('继续问答接口已弃用')

    async def completeSession(self, user_id, session_id, round_number):
        self.calls.append(('complete', user_id, session_id, round_number))
        return {
            'userId': user_id,
            'sessionId': session_id,
            'status': 'finished',
            'progress': '100%',
            'completedAt': '2026-08-13T10:00:00',
        }


class RecordApiContractTest(AsyncHTTPTestCase):
    def get_app(self):
        self.qa_manager = FakeQAManager()
        return tornado.web.Application(
            [
                (r'/api/v1/qa/create', server.QASessionCreateHandler),
                (
                    r'/api/v1/qa/answer/complete',
                    server.QASessionAnswerHandler,
                ),
                (
                    r'/api/v1/qa/answer/stream',
                    server.QASessionAnswerStreamHandler,
                ),
                (r'/api/v1/qa/continue', server.QASessionContinueHandler),
                (r'/api/v1/qa/complete', server.QASessionCompleteHandler),
                (r'/api/v1/qa/status', server.QASessionGetHandler),
                (r'/api/v1/qa/history', server.QASessionHistoryHandler),
            ],
            qa_manager=self.qa_manager,
        )

    @staticmethod
    def _decode(response):
        return json.loads(response.body.decode('utf-8'))

    def test_create_preserves_request_mapping_and_response_envelope(self):
        response = self.fetch(
            '/api/v1/qa/create',
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps(
                {
                    'userId': 'user-1',
                    'sessionId': 'session-1',
                    'name': '测试用户',
                    'doctorProductPackages': ['基础健康包'],
                    'fileList': [
                        {
                            'url': 'https://example.com/report-1.pdf',
                            'fileType': 'pdf',
                        }
                    ],
                    'hospitalData': [{'diagnosis': '高血压'}],
                    'weRunData': [{'date': '2026-08-17', 'steps': 8200}],
                    'smartDeviceData': {'restingHeartRate': 72},
                }
            ),
        )

        self.assertEqual(response.code, 200)
        payload = self._decode(response)
        self.assertEqual(payload['code'], 200)
        self.assertEqual(payload['data']['questionId'], 'q_sleep')
        call = self.qa_manager.calls[0]
        self.assertEqual(call[:3], ('create', 'user-1', 'session-1'))
        self.assertEqual(call[3]['name'], '测试用户')
        self.assertIsNone(call[3]['basic_info'])
        self.assertEqual(
            call[3]['doctor_product_packages'],
            ['基础健康包'],
        )
        self.assertEqual(
            call[3]['file_list'],
            [
                {
                    'url': 'https://example.com/report-1.pdf',
                    'fileType': 'pdf',
                }
            ],
        )
        self.assertEqual(
            call[3]['hospital_data'],
            [{'diagnosis': '高血压'}],
        )
        self.assertEqual(
            call[3]['we_run_data'],
            [{'date': '2026-08-17', 'steps': 8200}],
        )
        self.assertEqual(
            call[3]['smart_device_data'],
            {'restingHeartRate': 72},
        )

    def test_create_allows_missing_doctor_product_packages(self):
        response = self.fetch(
            '/api/v1/qa/create',
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps(
                {
                    'userId': 'user-without-products',
                    'sessionId': 'session-without-products',
                    'name': '测试用户',
                }
            ),
        )

        self.assertEqual(response.code, 200)
        payload = self._decode(response)
        self.assertEqual(payload['code'], 200)
        self.assertEqual(payload['data']['status'], 'question')
        call = self.qa_manager.calls[0]
        self.assertEqual(
            call[:3],
            ('create', 'user-without-products', 'session-without-products'),
        )
        self.assertIsNone(call[3]['doctor_product_packages'])

    def test_create_file_type_contract_uses_real_normalization(self):
        from modules.record.application.service import QASessionManager

        original = self.qa_manager.createSession

        async def normalized_create(user_id, session_id, **data):
            data['file_list'] = QASessionManager._normalizeFileList(data.get('file_list'))
            return await original(user_id, session_id, **data)

        self.qa_manager.createSession = normalized_create
        for file_type, expected in (('pdf', 200), ('image', 200), ('jpg', 400), ('png', 400), ('', 400)):
            with self.subTest(file_type=file_type):
                response = self.fetch(
                    '/api/v1/qa/create',
                    method='POST',
                    headers={'Content-Type': 'application/json'},
                    body=json.dumps(
                        {
                            'userId': 'user-1',
                            'sessionId': 'session-types',
                            'fileList': [{'url': 'https://example.com/download', 'fileType': file_type}],
                        }
                    ),
                )
                self.assertEqual(response.code, expected)
                self.assertEqual(self._decode(response)['code'], expected)
                if expected == 400:
                    self.assertIsNone(self._decode(response)['data'])

    def test_create_allows_empty_doctor_product_packages(self):
        for value in (None, []):
            with self.subTest(value=value):
                self.qa_manager.calls.clear()
                response = self.fetch(
                    '/api/v1/qa/create',
                    method='POST',
                    headers={'Content-Type': 'application/json'},
                    body=json.dumps(
                        {
                            'userId': 'user-with-empty-products',
                            'sessionId': 'session-with-empty-products',
                            'doctorProductPackages': value,
                        }
                    ),
                )

                self.assertEqual(response.code, 200)
                self.assertEqual(
                    self.qa_manager.calls[0][3]['doctor_product_packages'],
                    value,
                )

    def test_create_maps_report_processing_errors_to_http_status(self):
        cases = (
            (_HealthReportSourceConflictError('报告来源冲突'), 409),
            (_HealthReportUnsupportedFormatError('格式不支持'), 415),
            (_HealthReportUpstreamError('报告解析失败'), 502),
        )
        original = self.qa_manager.createSession
        for error, expected_status in cases:
            with self.subTest(error=type(error).__name__):

                async def fail(*args, _error=error, **kwargs):
                    raise _error

                self.qa_manager.createSession = fail
                response = self.fetch(
                    '/api/v1/qa/create',
                    method='POST',
                    headers={'Content-Type': 'application/json'},
                    body=json.dumps(
                        {
                            'userId': 'user-report-error',
                            'sessionId': 'session-report-error',
                        }
                    ),
                )
                self.assertEqual(response.code, expected_status)
        self.qa_manager.createSession = original

    def test_create_upstream_failure_returns_502_and_logs_the_cause_without_traceback(self):
        message = '文档解析任务提交失败'

        async def fail(*args, **kwargs):
            try:
                raise DocumentParsingError(message)
            except DocumentParsingError as error:
                raise HealthReportUpstreamError(message) from error

        self.qa_manager.createSession = fail
        project_logger = unittest.mock.Mock()
        # Earlier isolated server imports may retain a separate BaseHandler module.
        with patch.dict(
            record_handler_module.QASessionCreateHandler.log_exception.__globals__, {'logger': project_logger}
        ):
            response = self.fetch(
                '/api/v1/qa/create',
                method='POST',
                headers={'Content-Type': 'application/json'},
                body=json.dumps({'userId': 'user-report-error', 'sessionId': 'session-report-error'}),
            )
        payload = self._decode(response)
        self.assertEqual(response.code, 502)
        self.assertEqual(payload['code'], 502)
        self.assertEqual(payload['message'], message)
        self.assertIsNone(payload['data'])
        project_logger.error.assert_called_once()
        logged = project_logger.error.call_args.args[0]
        self.assertIn(payload['uuid'], logged)
        self.assertIn('HTTPError -> HealthReportUpstreamError -> DocumentParsingError', logged)
        self.assertIn(message, logged)
        self.assertNotIn('exc_info', project_logger.error.call_args.kwargs)
        self.assertNotIn('\n', logged)

    def test_stream_failure_keeps_error_event_and_uses_a_compact_log(self):
        async def fail(*args, **kwargs):
            yield {'data': {'type': 'start'}}
            raise RuntimeError('upstream\nnot available')

        self.qa_manager.submitAnswerStream = fail
        with patch.object(record_handler_module, 'logger') as project_logger:
            response = self.fetch(
                '/api/v1/qa/answer/stream',
                method='POST',
                headers={'Content-Type': 'application/json'},
                body=json.dumps({'userId': 'u', 'sessionId': 's', 'questionId': 'q', 'answer': '回答'}),
            )
        self.assertEqual(response.code, 200)
        events = [json.loads(line[6:]) for line in response.body.decode().splitlines() if line.startswith('data: ')]
        self.assertEqual(events[-1], {'type': 'error', 'error': 'upstream\nnot available'})
        project_logger.error.assert_called_once()
        call = project_logger.error.call_args
        logged = call.args[0] % call.args[1:]
        self.assertIn('error_type=RuntimeError', logged)
        self.assertIn('message=upstream not available', logged)
        self.assertNotIn('\n', logged)
        self.assertNotIn('exc_info', call.kwargs)

    def test_complete_answer_preserves_default_thinking_and_response(self):
        response = self.fetch(
            '/api/v1/qa/answer/complete',
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps(
                {
                    'userId': 'user-1',
                    'sessionId': 'session-1',
                    'questionId': 'q_sleep',
                    'answer': ['睡眠良好'],
                }
            ),
        )

        self.assertEqual(response.code, 200)
        payload = self._decode(response)
        self.assertEqual(payload['data']['questionId'], 'q_sleep')
        self.assertEqual(payload['data']['reasoning'], '优先了解睡眠')
        self.assertEqual(payload['data']['intakeQuestionCount'], 1)
        self.assertEqual(payload['data']['qaRoundCount'], 0)
        self.assertEqual(
            self.qa_manager.calls,
            [
                (
                    'answer',
                    'user-1',
                    'session-1',
                    'q_sleep',
                    ['睡眠良好'],
                    None,
                    False,
                )
            ],
        )

    def test_explicit_thinking_is_forwarded_by_both_answer_endpoints(self):
        for mode in ('complete', 'stream'):
            response = self.fetch(
                f'/api/v1/qa/answer/{mode}',
                method='POST',
                headers={'Content-Type': 'application/json'},
                body=json.dumps(
                    {
                        'userId': 'user-1',
                        'sessionId': 'session-1',
                        'questionId': 'q_sleep',
                        'answer': ['睡眠良好'],
                        'thinking': True,
                    }
                ),
            )
            self.assertEqual(response.code, 200)
            self.assertTrue(self.qa_manager.calls[-1][-1])

    def test_effort_defaults_and_overrides_preserve_thinking(self):
        for mode in ('complete', 'stream'):
            for options in ({}, {'reasoningEffort': 'low'}, {'reasoningEffort': 'high'}, {'reasoningEffort': 'max'}):
                for thinking in (None, True):
                    body = {
                        'userId': 'user-1',
                        'sessionId': 'session-1',
                        'questionId': 'q_sleep',
                        'answer': ['睡眠良好'],
                        **options,
                    }
                    if thinking is not None:
                        body['thinking'] = thinking
                    response = self.fetch(
                        f'/api/v1/qa/answer/{mode}',
                        method='POST',
                        headers={'Content-Type': 'application/json'},
                        body=json.dumps(body),
                    )
                    assert response.code == 200
                    assert self.qa_manager.reasoning_effort == options.get('reasoningEffort', 'low')
                    assert self.qa_manager.calls[-1][-1] is (thinking is True)

    def test_invalid_effort_is_http_400_before_service_or_sse(self):
        for mode in ('complete', 'stream'):
            for effort in (None, '', 'medium', 'none', 'LOW', 1, True, [], {}):
                response = self.fetch(
                    f'/api/v1/qa/answer/{mode}',
                    method='POST',
                    headers={'Content-Type': 'application/json'},
                    body=json.dumps(
                        {
                            'userId': 'user-1',
                            'sessionId': 'session-1',
                            'questionId': 'q_sleep',
                            'answer': ['睡眠良好'],
                            'reasoningEffort': effort,
                        }
                    ),
                )
                assert response.code == 400
                assert response.headers['Content-Type'].startswith('application/json')
                assert self.qa_manager.calls == []

    def test_complete_answer_requires_answer_or_other(self):
        response = self.fetch(
            '/api/v1/qa/answer/complete',
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps(
                {
                    'userId': 'user-1',
                    'sessionId': 'session-1',
                    'questionId': 'q_sleep',
                }
            ),
        )

        self.assertEqual(response.code, 400)
        self.assertEqual(self.qa_manager.calls, [])

    def test_stream_question_preserves_interview_and_qa_counters(self):
        async def counter_stream(*args, **kwargs):
            yield {'data': {'type': 'question', 'status': 'question', 'intakeQuestionCount': 5, 'qaRoundCount': 2}}

        self.qa_manager.submitAnswerStream = counter_stream
        response = self.fetch(
            '/api/v1/qa/answer/stream',
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps({'userId': 'u', 'sessionId': 's', 'questionId': 'q', 'answer': '实际问题'}),
        )
        self.assertEqual(response.code, 200)
        events = [json.loads(line[6:]) for line in response.body.decode().splitlines() if line.startswith('data: ')]
        event = next(item['data'] for item in events if item.get('data', {}).get('type') == 'question')
        self.assertEqual((event['intakeQuestionCount'], event['qaRoundCount']), (5, 2))

    def test_stream_disconnect_still_consumes_and_persists_answer(self):
        original_flush = server.QASessionAnswerStreamHandler.flush
        flush_calls = 0

        def close_first_flush(handler, *args, **kwargs):
            nonlocal flush_calls
            flush_calls += 1
            if flush_calls == 1:
                future = asyncio.get_running_loop().create_future()
                future.set_exception(StreamClosedError())
                return future
            return original_flush(handler, *args, **kwargs)

        with (
            patch.object(record_handler_module, 'logger') as project_logger,
            patch.object(
                server.QASessionAnswerStreamHandler,
                'flush',
                close_first_flush,
            ),
        ):
            response = self.fetch(
                '/api/v1/qa/answer/stream',
                method='POST',
                headers={'Content-Type': 'application/json'},
                body=json.dumps(
                    {
                        'userId': 'user-1',
                        'sessionId': 'session-1',
                        'questionId': 'q_sleep',
                        'answer': ['睡眠良好'],
                    }
                ),
            )

        self.assertEqual(response.code, 200)
        self.assertEqual(
            self.qa_manager.stream_events_consumed,
            ['start', 'reasoning', 'question', 'finish'],
        )
        self.assertTrue(self.qa_manager.stream_persisted)
        self.assertFalse(self.qa_manager.calls[-1][-1])
        self.assertEqual(project_logger.info.call_count, 2)
        self.assertIn('客户端已断开', project_logger.info.call_args_list[0].args[0])
        self.assertIn('会话保存已完成', project_logger.info.call_args_list[1].args[0])

    def test_continue_is_retired_with_conflict(self):
        response = self.fetch(
            '/api/v1/qa/continue',
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps(
                {
                    'userId': 'user-1',
                    'sessionId': 'session-1',
                    'round': 1,
                    'thinking': False,
                }
            ),
        )

        self.assertEqual(response.code, 409)
        payload = self._decode(response)
        self.assertIn('弃用', payload['message'])
        self.assertEqual(
            self.qa_manager.calls,
            [('continue', 'user-1', 'session-1', 1, False)],
        )

    def test_complete_reads_already_finished_session(self):
        response = self.fetch(
            '/api/v1/qa/complete',
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps(
                {
                    'userId': 'user-1',
                    'sessionId': 'session-1',
                    'round': 1,
                }
            ),
        )

        self.assertEqual(response.code, 200)
        payload = self._decode(response)
        self.assertEqual(payload['data']['status'], 'finished')
        self.assertEqual(
            self.qa_manager.calls,
            [('complete', 'user-1', 'session-1', 1)],
        )

    def test_continue_and_complete_require_positive_round(self):
        for path in ('/api/v1/qa/continue', '/api/v1/qa/complete'):
            with self.subTest(path=path):
                self.qa_manager.calls.clear()
                response = self.fetch(
                    path,
                    method='POST',
                    headers={'Content-Type': 'application/json'},
                    body=json.dumps(
                        {
                            'userId': 'user-1',
                            'sessionId': 'session-1',
                        }
                    ),
                )
                self.assertEqual(response.code, 400)
                self.assertEqual(self.qa_manager.calls, [])

    def test_create_maps_optional_basic_info(self):
        response = self.fetch(
            '/api/v1/qa/create',
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps(
                {
                    'userId': 'user-1',
                    'sessionId': 'session-1',
                    'basicInfo': {'gender': '女', 'age': 42},
                }
            ),
        )

        self.assertEqual(response.code, 200)
        self.assertEqual(
            self.qa_manager.calls[0][3]['basic_info'],
            {'gender': '女', 'age': 42},
        )

    def test_flow_conflicts_map_to_http_409(self):
        for suffix, method in (
            ('create', 'createSession'),
            ('answer/complete', 'submitAnswer'),
            ('status', 'getSession'),
            ('history', 'getHistory'),
            ('complete', 'completeSession'),
        ):
            with self.subTest(suffix=suffix):
                original = getattr(self.qa_manager, method, None)
                setattr(
                    self.qa_manager,
                    method,
                    AsyncMock(side_effect=record_handler_module.QASessionConflictError('请重开')),
                )
                response = self.fetch(
                    '/api/v1/qa/' + suffix,
                    method='POST',
                    headers={'Content-Type': 'application/json'},
                    body=json.dumps({'userId': 'u', 'sessionId': 's', 'questionId': 'q', 'answer': '回答', 'round': 1}),
                )
                self.assertEqual(response.code, 409)
                setattr(self.qa_manager, method, original)

    def test_creation_processing_and_failure_remain_queryable_with_same_session(self):
        for status, http, code, retryable in (
            ('processing', 202, None, False),
            ('failed', 409, 'DOCUMENT_SUBMISSION_UNCONFIRMED', True),
            ('failed', 409, 'DOCUMENT_SUBMISSION_RETRY_EXHAUSTED', False),
        ):
            result = {
                'userId': 'u',
                'sessionId': 's',
                'status': status,
                'stage': 'processing_reports',
                'message': '报告处理中',
                'errorCode': code,
                'retryable': retryable,
                'retryAfterSeconds': 30 if retryable else None,
                'expiresAt': '2026-09-11T12:00:00+00:00',
            }
            self.qa_manager.createSession = AsyncMock(return_value=result)
            self.qa_manager.getSession = AsyncMock(return_value=result)
            body = json.dumps({'userId': 'u', 'sessionId': 's'})
            response = self.fetch('/api/v1/qa/create', method='POST', body=body)
            self.assertEqual(response.code, http)
            self.assertEqual(self._decode(response)['data'], result)
            recovered = self.fetch('/api/v1/qa/status', method='POST', body=body)
            self.assertEqual(recovered.code, 200)
            self.assertEqual(self._decode(recovered)['data'], result)

    def test_answer_returns_final_message_without_question(self):
        final = {
            'userId': 'u',
            'sessionId': 's',
            'status': 'finished',
            'phase': 'finished',
            'interactionCount': 3,
            'assistantMessage': '最后答复',
            'endReason': 'no_more_questions',
            'progress': '100%',
            'round': 1,
            'completedAt': '2026-09-09T12:00:00',
            'reasoning': '',
        }
        self.qa_manager.submitAnswer = AsyncMock(return_value=final)
        response = self.fetch(
            '/api/v1/qa/answer/complete',
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps(
                {'userId': 'u', 'sessionId': 's', 'questionId': 'q', 'answer': '没有了', 'thinking': False}
            ),
        )
        self.assertEqual(self._decode(response)['data'], final)


class RecordRuntimeInjectionTest(unittest.TestCase):
    def test_compatibility_handler_also_uses_compact_error_logs(self):
        handler = SimpleNamespace(
            request=SimpleNamespace(method='POST', path='/api/v1/qa/create', remote_ip='127.0.0.1'),
            request_uuid='REQUEST-COMPAT-1',
        )
        error = tornado.web.HTTPError(409, log_message='文档提交结果待核查')
        error.__cause__ = DocumentSubmissionConflictError('待核查')
        with patch.object(server, 'logger') as project_logger:
            server.BaseHandler.log_exception(handler, type(error), error, None)
        project_logger.error.assert_not_called()
        project_logger.warning.assert_called_once()
        call = project_logger.warning.call_args
        self.assertIn('request_uuid=REQUEST-COMPAT-1', call.args[0])
        self.assertIn('HTTPError -> DocumentSubmissionConflictError', call.args[0])
        self.assertNotIn('exc_info', call.kwargs)

    def test_tornado_logs_are_routed_through_project_logger(self):
        request = SimpleNamespace(
            method='POST',
            path='/api/v1/health-profile/get',
            remote_ip='127.0.0.1',
            request_time=lambda: 0.00317,
        )
        handler = SimpleNamespace(
            request=request,
            request_uuid='REQUEST-1',
            get_status=lambda: 404,
        )

        with patch.object(server, 'logger') as project_logger:
            server.log_tornado_request(handler)
            error = tornado.web.HTTPError(
                404,
                log_message='健康主体不存在',
            )
            server.BaseHandler.log_exception(
                handler,
                type(error),
                error,
                None,
            )

        access_log = project_logger.warning.call_args_list[0].args[0]
        exception_log = project_logger.warning.call_args_list[1].args[0]
        self.assertIn('HTTP 访问', access_log)
        self.assertIn('status_code=404', access_log)
        self.assertIn('elapsed_ms=3.17', access_log)
        self.assertIn('HTTP 请求异常', exception_log)
        self.assertIn('健康主体不存在', exception_log)

    def test_application_passes_same_runtime_to_qa_and_chat_services(self):
        # 组合根会读取 runtime.hub 交给报告管线；替身补上这个属性即可，
        # 下面仍按同一个对象校验注入身份。
        runtime = SimpleNamespace(hub=AgentHub(GraphRegistry()))
        calls = []

        class RecordingQAManager:
            def __init__(
                self,
                received_runtime,
                *,
                session_repository,
                session_cache,
                prompt_renderer,
                record_profile_source,
                record_document_port,
                health_profile_memory_provider,
                creation_store,
                reply_recovery_store,
                completion_store,
                creation_task_spawner,
            ):
                calls.append(
                    (
                        'qa',
                        received_runtime,
                        record_profile_source,
                        record_document_port,
                        health_profile_memory_provider,
                        session_repository,
                        session_cache,
                        prompt_renderer,
                        reply_recovery_store,
                        completion_store,
                    )
                )

        class RecordingChatManager:
            def __init__(
                self,
                received_runtime,
                *,
                session_repository,
                custom_message_outbox,
                qa_session_gate,
                chat_profile_source,
                health_profile_memory_provider,
                report_context_provider=None,
            ):
                calls.append(
                    (
                        'chat',
                        received_runtime,
                        qa_session_gate,
                        chat_profile_source,
                        health_profile_memory_provider,
                        session_repository,
                        custom_message_outbox,
                    )
                )

        with (
            patch.object(server, 'QASessionManager', RecordingQAManager),
            patch.object(server, 'ChatSessionManager', RecordingChatManager),
            # 本用例只验证运行时注入，报告管线接的是真实 AgentHub，
            # 这里用替身顶掉，避免为它造一个完整的 hub。
            patch(
                'bootstrap.factory.build_report_pipeline',
                return_value=_UnusedService(),
            ),
        ):
            profile_service = object()
            record_profile_source = object()
            chat_profile_source = object()
            health_profile_memory_provider = object()
            application = server._make_app_with_agent_runtime(
                runtime,
                health_profile_service=profile_service,
                health_report_source=_UnusedService(),
                record_profile_source=record_profile_source,
                chat_profile_source=chat_profile_source,
                health_profile_memory_provider=(health_profile_memory_provider),
                health_profile_ingestion_worker=_UnusedService(),
            )

        self.assertEqual(calls[0][:3], ('qa', runtime, record_profile_source))
        self.assertIsInstance(calls[0][3], LegacyRecordDocumentAdapter)
        self.assertIs(calls[0][4], health_profile_memory_provider)
        self.assertIsNotNone(calls[0][5])
        self.assertIsNotNone(calls[0][6])
        self.assertIsNotNone(calls[0][7])
        self.assertIsNotNone(calls[0][8])
        self.assertIsNotNone(calls[0][9])
        self.assertEqual(calls[1][:2], ('chat', runtime))
        self.assertIsInstance(calls[1][2], RecordingQAManager)
        self.assertIs(calls[1][3], chat_profile_source)
        self.assertIs(calls[1][4], health_profile_memory_provider)
        self.assertIsNotNone(calls[1][5])
        self.assertIsNotNone(calls[1][6])
        self.assertIs(application.settings['agent_runtime'], runtime)
        self.assertIs(
            application.settings['health_profile_service'],
            profile_service,
        )
        self.assertIs(
            application.settings['record_profile_source'],
            record_profile_source,
        )
        self.assertIs(
            application.settings['chat_profile_source'],
            chat_profile_source,
        )
        self.assertIs(
            application.settings['health_profile_memory_provider'],
            health_profile_memory_provider,
        )
        self.assertIs(
            application.settings['log_function'],
            server.log_tornado_request,
        )


if __name__ == '__main__':
    unittest.main()
