"""Health-record questionnaire HTTP and SSE adapters."""

from __future__ import annotations

import json

import tornado.web
from tornado.iostream import StreamClosedError

from modules.analysis.application import HealthReportUnsupportedFormatError
from modules.analysis.application import HealthReportUpstreamError
from modules.analysis.application.documents import DocumentBusyError
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.application.documents import DocumentSubmissionConflictError
from modules.health_profile.application import HealthReportSourceConflictError
from modules.record.application import QASessionConflictError
from modules.record.application.creation import creation_http_status
from modules.record.application.validation import RecordGenerationError
from utils.exceptions import format_exception_summary
from utils.log import getLogger
from utils.log.context import request_log_context
from web.handlers.base import BaseHandler
from web.handlers.llm_options import reasoning_effort_from_body
from web.http_logging import ensure_stream_log
from web.http_logging import log_stream_event
from web.stream_batch import send_batched_sse

logger = getLogger(__name__)


def _json_object(handler: tornado.web.RequestHandler) -> dict:
    try:
        body = json.loads(handler.request.body)
    except json.JSONDecodeError:
        raise tornado.web.HTTPError(400, log_message='无效的JSON格式')
    if not isinstance(body, dict):
        raise tornado.web.HTTPError(400, log_message='请求体必须为JSON对象')
    handler.bind_log_identity(body)
    return body


def _required(body: dict, field: str):
    value = body.get(field)
    if value is None or value == '':
        raise tornado.web.HTTPError(400, log_message=f'缺少必填参数: {field}')
    return value


def _positive_round(body: dict) -> int:
    value = body.get('round')
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise tornado.web.HTTPError(400, log_message='round 必须为正整数')
    return value


class RecordHandler(BaseHandler):
    """Compatibility-aware base for explicitly injected QA services."""

    def initialize(self, service) -> None:
        self.qa_manager = service


class QASessionCreateHandler(RecordHandler):
    async def post(self) -> None:
        try:
            await self.handle_create()
        except (HealthReportSourceConflictError, DocumentSubmissionConflictError) as error:
            raise tornado.web.HTTPError(409, log_message=str(error))
        except QASessionConflictError as error:
            if getattr(error, 'code', None):
                self.set_status(409)
                self.write_response(
                    409,
                    str(error),
                    {
                        'status': 'failed',
                        'errorCode': error.code,
                        'retryable': False,
                        'retryAfterSeconds': None,
                        'message': str(error),
                    },
                )
                return
            raise tornado.web.HTTPError(409, log_message=str(error))
        except DocumentNotFoundError as error:
            raise tornado.web.HTTPError(404, log_message=str(error))
        except DocumentBusyError as error:
            raise tornado.web.HTTPError(503, log_message=str(error))
        except HealthReportUnsupportedFormatError as error:
            raise tornado.web.HTTPError(415, log_message=str(error))
        except HealthReportUpstreamError as error:
            raise tornado.web.HTTPError(502, log_message=str(error))
        except tornado.web.HTTPError:
            raise
        except ValueError as error:
            if type(error).__name__ in {
                '_HealthReportUnsupportedFormatError',
                'HealthReportUnsupportedFormatError',
            }:
                raise tornado.web.HTTPError(415, log_message=str(error))
            logger.error('创建问答会话业务错误: %s', error)
            raise tornado.web.HTTPError(400, log_message=str(error))
        except Exception as error:
            compatibility_status = {
                '_HealthReportSourceConflictError': 409,
                'HealthReportSourceConflictError': 409,
                '_HealthReportUnsupportedFormatError': 415,
                'HealthReportUnsupportedFormatError': 415,
                '_HealthReportUpstreamError': 502,
                'HealthReportUpstreamError': 502,
            }.get(type(error).__name__)
            if compatibility_status is not None:
                raise tornado.web.HTTPError(
                    compatibility_status,
                    log_message=str(error),
                )
            logger.error('创建问答会话失败: %s', type(error).__name__)
            raise tornado.web.HTTPError(500, log_message=str(error))

    async def handle_create(self) -> None:
        body = _json_object(self)
        basic_info = body.get('basicInfo')
        if basic_info is not None and not isinstance(basic_info, dict):
            raise tornado.web.HTTPError(400, log_message='basicInfo 必须为对象')
        session = await self.qa_manager.createSession(
            _required(body, 'userId'),
            _required(body, 'sessionId'),
            name=body.get('name'),
            file_list=body.get('fileList'),
            hospital_data=body.get('hospitalData'),
            we_run_data=body.get('weRunData'),
            doctor_product_packages=body.get('doctorProductPackages'),
            smart_device_data=body.get('smartDeviceData'),
            basic_info=basic_info,
        )
        code = creation_http_status(session)
        self.set_status(code)
        self.write_response(code, session.get('message', 'success'), session)


class QASessionAnswerHandler(RecordHandler):
    async def post(self) -> None:
        try:
            await self.handle_answer()
        except QASessionConflictError as error:
            raise tornado.web.HTTPError(409, log_message=str(error))
        except RecordGenerationError as error:
            raise tornado.web.HTTPError(502, log_message=str(error)) from error
        except tornado.web.HTTPError:
            raise
        except ValueError as error:
            logger.error('提交答案参数错误: %s', error)
            raise tornado.web.HTTPError(400, log_message=str(error))
        except Exception as error:
            logger.error('提交答案失败: %s', type(error).__name__)
            raise tornado.web.HTTPError(500, log_message=str(error))

    async def handle_answer(self) -> None:
        body = _json_object(self)
        reasoning_effort = reasoning_effort_from_body(body)
        answer = body.get('answer')
        other = body.get('other')
        if answer is None and other is None:
            raise tornado.web.HTTPError(
                400,
                log_message='answer 和 other 至少提供其中一个',
            )
        result = await self.qa_manager.submitAnswer(
            _required(body, 'userId'),
            _required(body, 'sessionId'),
            _required(body, 'questionId'),
            answer,
            other,
            thinking=body.get('thinking', False),
            reasoning_effort=reasoning_effort,
        )
        self.write_response(200, 'success', result)


class QASessionContinueHandler(RecordHandler):
    async def post(self) -> None:
        body = _json_object(self)
        try:
            result = await self.qa_manager.continueSession(
                _required(body, 'userId'),
                _required(body, 'sessionId'),
                _positive_round(body),
                thinking=body.get('thinking', False),
            )
        except QASessionConflictError as error:
            raise tornado.web.HTTPError(409, log_message=str(error))
        except tornado.web.HTTPError:
            raise
        except ValueError as error:
            raise tornado.web.HTTPError(400, log_message=str(error))
        except Exception as error:
            logger.error('继续问答失败: %s', type(error).__name__)
            raise tornado.web.HTTPError(500, log_message=str(error))
        self.write_response(200, 'success', result)


class QASessionCompleteHandler(RecordHandler):
    async def post(self) -> None:
        body = _json_object(self)
        try:
            result = await self.qa_manager.completeSession(
                _required(body, 'userId'),
                _required(body, 'sessionId'),
                _positive_round(body),
            )
        except QASessionConflictError as error:
            raise tornado.web.HTTPError(409, log_message=str(error))
        except tornado.web.HTTPError:
            raise
        except ValueError as error:
            raise tornado.web.HTTPError(400, log_message=str(error))
        except Exception as error:
            logger.error('完成问答失败: %s', type(error).__name__)
            raise tornado.web.HTTPError(500, log_message=str(error))
        self.write_response(200, 'success', result)


class QASessionAnswerStreamHandler(RecordHandler):
    def initialize(self, service) -> None:
        super().initialize(service)
        self._client_disconnected = False
        self._stream_completed = False

    def on_connection_close(self) -> None:
        self._mark_client_disconnected()

    @request_log_context
    def _mark_client_disconnected(self) -> None:
        if self._client_disconnected:
            return
        self._client_disconnected = True
        if not self._stream_completed:
            logger.info(
                '流式问答客户端已断开，继续在后台完成生成和会话保存: request_uuid=%s',
                getattr(self, 'request_uuid', '-'),
            )

    async def _write_sse_event(self, payload) -> bool:
        if self._client_disconnected:
            log_stream_event(self, payload, outcome='skipped_disconnected')
            return False
        data = json.dumps(payload, ensure_ascii=False)
        try:
            self.write(f'data: {data}\n\n')
            await self.flush()
        except StreamClosedError:
            self._mark_client_disconnected()
            log_stream_event(self, payload, outcome='flush_failed')
            return False
        log_stream_event(self, payload, outcome='flushed')
        return True

    async def post(self) -> None:
        self._http_log_stream_active = True
        try:
            await self.handle_answer_stream()
        except tornado.web.HTTPError:
            raise
        except StreamClosedError:
            self._mark_client_disconnected()
        except ValueError as error:
            logger.error('流式提交答案参数错误: %s', error)
            await self._write_sse_error(error)
        except Exception as error:
            logger.error(
                '流式提交答案失败: request_uuid=%s %s',
                getattr(self, 'request_uuid', '-'),
                format_exception_summary(error),
            )
            await self._write_sse_error(error)
        finally:
            ensure_stream_log(self)

    async def _write_sse_error(self, error: Exception) -> None:
        if self._client_disconnected:
            log_stream_event(self, {'type': 'error', 'error': str(error)}, outcome='skipped_disconnected')
            return
        if not self._headers_written:
            self.set_header('Content-Type', 'text/event-stream')
            self.set_header('Cache-Control', 'no-cache')
            self.set_header('Connection', 'keep-alive')
        await self._write_sse_event({'type': 'error', 'error': str(error)})

    async def handle_answer_stream(self) -> None:
        body = _json_object(self)
        reasoning_effort = reasoning_effort_from_body(body)
        answer = body.get('answer')
        other = body.get('other')
        if answer is None and other is None:
            raise tornado.web.HTTPError(
                400,
                log_message='answer 和 other 至少提供其中一个',
            )
        user_id = _required(body, 'userId')
        session_id = _required(body, 'sessionId')
        question_id = _required(body, 'questionId')
        self.set_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.set_header('Cache-Control', 'no-cache')
        self.set_header('Connection', 'keep-alive')
        self.set_header('X-Accel-Buffering', 'no')
        source = self.qa_manager.submitAnswerStream(
            user_id,
            session_id,
            question_id,
            answer,
            other,
            thinking=body.get('thinking', False),
            reasoning_effort=reasoning_effort,
        )
        await send_batched_sse(
            source, self._write_sse_event, disconnected=lambda: self._client_disconnected,
        )
        self._stream_completed = True
        if self._client_disconnected:
            logger.info(
                '流式问答后台生成及会话保存已完成: request_uuid=%s',
                getattr(self, 'request_uuid', '-'),
            )
        try:
            await self.finish()
        except StreamClosedError:
            self._mark_client_disconnected()


class QASessionGetHandler(RecordHandler):
    async def post(self) -> None:
        try:
            await self.handle_get()
        except QASessionConflictError as error:
            raise tornado.web.HTTPError(409, log_message=str(error))
        except RecordGenerationError as error:
            raise tornado.web.HTTPError(502, log_message=str(error)) from error
        except tornado.web.HTTPError:
            raise
        except Exception as error:
            logger.error('获取问答会话失败: %s', type(error).__name__)
            raise tornado.web.HTTPError(500, log_message=str(error))

    async def handle_get(self) -> None:
        body = _json_object(self)
        session = await self.qa_manager.getSession(
            _required(body, 'userId'),
            _required(body, 'sessionId'),
        )
        if session is None:
            raise tornado.web.HTTPError(404, log_message='会话不存在')
        self.write_response(200, 'success', session)


class QASessionHistoryHandler(RecordHandler):
    async def post(self) -> None:
        try:
            await self.handle_history()
        except QASessionConflictError as error:
            raise tornado.web.HTTPError(409, log_message=str(error))
        except RecordGenerationError as error:
            raise tornado.web.HTTPError(502, log_message=str(error)) from error
        except tornado.web.HTTPError:
            raise
        except Exception as error:
            logger.error('获取问答历史失败: %s', type(error).__name__)
            raise tornado.web.HTTPError(500, log_message=str(error))

    async def handle_history(self) -> None:
        body = _json_object(self)
        history = await self.qa_manager.getHistory(
            _required(body, 'userId'),
            _required(body, 'sessionId'),
        )
        if history is None:
            raise tornado.web.HTTPError(404, log_message='会话不存在')
        self.write_response(200, 'success', history)


__all__ = [
    'QASessionAnswerHandler',
    'QASessionAnswerStreamHandler',
    'QASessionCompleteHandler',
    'QASessionContinueHandler',
    'QASessionCreateHandler',
    'QASessionGetHandler',
    'QASessionHistoryHandler',
    'RecordHandler',
]
