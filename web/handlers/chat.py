"""Chat HTTP and SSE inbound adapters."""

from __future__ import annotations

import json

import tornado.web
from tornado.iostream import StreamClosedError

from modules.chat.application import ChatCustomMessageConflictError
from modules.chat.application import ChatCustomMessageValidationError
from modules.chat.application import ChatSessionNotFoundError
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


class ChatHandler(BaseHandler):
    def initialize(self, service) -> None:
        self.chat_manager = service


def _report_reference(body: dict) -> tuple[str | None, str | None]:
    """解析可选的报告引用；reportType 必须与 finalReportId 一起使用。"""
    final_report_id = body.get('finalReportId')
    report_type = body.get('reportType')
    for name, value in (('finalReportId', final_report_id), ('reportType', report_type)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f'{name} 必须为非空字符串')
    if report_type is not None and final_report_id is None:
        raise ValueError('reportType 必须与 finalReportId 一起提供')
    return (
        final_report_id.strip() if isinstance(final_report_id, str) else None,
        report_type.strip() if isinstance(report_type, str) else None,
    )


class ChatSendCompleteHandler(ChatHandler):
    async def post(self) -> None:
        try:
            await self.handle_send()
        except tornado.web.HTTPError:
            raise
        except ValueError as error:
            logger.error('对话消息参数错误: %s', error)
            raise tornado.web.HTTPError(400, log_message=str(error))
        except Exception as error:
            logger.error('处理对话消息失败: %s', type(error).__name__)
            raise tornado.web.HTTPError(500, log_message=str(error))

    async def handle_send(self) -> None:
        body = _json_object(self)
        reasoning_effort = reasoning_effort_from_body(body)
        final_report_id, report_type = _report_reference(body)
        result = await self.chat_manager.sendMessage(
            _required(body, 'userId'),
            _required(body, 'sessionId'),
            _required(body, 'message'),
            thinking=body.get('thinking', True),
            reasoning_effort=reasoning_effort,
            final_report_id=final_report_id,
            report_type=report_type,
        )
        self.write_response(200, 'success', result)


class ChatCustomMessageHandler(ChatHandler):
    async def post(self) -> None:
        try:
            await self.handle_insert()
        except tornado.web.HTTPError:
            raise
        except ChatSessionNotFoundError as error:
            raise tornado.web.HTTPError(404, log_message=str(error))
        except ChatCustomMessageConflictError as error:
            raise tornado.web.HTTPError(409, log_message=str(error))
        except ChatCustomMessageValidationError as error:
            raise tornado.web.HTTPError(422, log_message=str(error))
        except Exception as error:
            compatibility_status = {
                'ChatSessionNotFoundError': 404,
                'ChatCustomMessageConflictError': 409,
                'ChatCustomMessageValidationError': 422,
            }.get(type(error).__name__)
            if compatibility_status is not None:
                raise tornado.web.HTTPError(
                    compatibility_status,
                    log_message=str(error),
                )
            logger.error('插入自定义对话消息失败: %s', type(error).__name__)
            raise tornado.web.HTTPError(500, log_message='插入自定义消息失败')

    async def handle_insert(self) -> None:
        body = _json_object(self)
        result = await self.chat_manager.insertCustomMessage(
            _required(body, 'userId'),
            _required(body, 'sessionId'),
            _required(body, 'eventId'),
            _required(body, 'content'),
            payload=body.get('payload'),
            display_type=body.get('displayType'),
            custom_type=body.get('customType'),
            title=body.get('title'),
        )
        status_code = 202 if result.get('queued') else 200
        self.set_status(status_code)
        self.write_response(
            status_code,
            'queued' if status_code == 202 else 'success',
            result,
        )


class ChatSendStreamHandler(ChatHandler):
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
            logger.info('流式对话客户端已断开，继续在后台完成生成和会话保存')

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

    async def _write_sse_error(self, error) -> None:
        if self._client_disconnected:
            log_stream_event(self, {'type': 'error', 'error': str(error)}, outcome='skipped_disconnected')
            return
        if not self._headers_written:
            self.set_header('Content-Type', 'text/event-stream')
            self.set_header('Cache-Control', 'no-cache')
            self.set_header('Connection', 'keep-alive')
        await self._write_sse_event({'type': 'error', 'error': str(error)})

    async def post(self) -> None:
        self._http_log_stream_active = True
        try:
            await self.handle_stream()
        except tornado.web.HTTPError:
            raise
        except ValueError as error:
            logger.error('流式对话参数错误: %s', error)
            await self._write_sse_error(error)
        except Exception as error:
            logger.error('处理流式对话失败: %s', type(error).__name__)
            await self._write_sse_error(error)
        finally:
            ensure_stream_log(self)

    async def handle_stream(self) -> None:
        body = _json_object(self)
        reasoning_effort = reasoning_effort_from_body(body)
        user_id = _required(body, 'userId')
        session_id = _required(body, 'sessionId')
        message = _required(body, 'message')
        self.set_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.set_header('Cache-Control', 'no-cache')
        self.set_header('Connection', 'keep-alive')
        self.set_header('X-Accel-Buffering', 'no')
        final_report_id, report_type = _report_reference(body)
        source = self.chat_manager.sendMessageStream(
            user_id,
            session_id,
            message,
            thinking=body.get('thinking', True),
            reasoning_effort=reasoning_effort,
            final_report_id=final_report_id,
            report_type=report_type,
        )
        await send_batched_sse(
            source, self._write_sse_event, disconnected=lambda: self._client_disconnected,
        )
        self._stream_completed = True
        if self._client_disconnected:
            logger.info('流式对话后台生成及会话保存已完成')
        try:
            await self.finish()
        except StreamClosedError:
            self._mark_client_disconnected()


class ChatHistoryHandler(ChatHandler):
    async def post(self) -> None:
        try:
            await self.handle_history()
        except tornado.web.HTTPError:
            raise
        except Exception as error:
            logger.error('获取对话历史失败: %s', type(error).__name__)
            raise tornado.web.HTTPError(500, log_message=str(error))

    async def handle_history(self) -> None:
        body = _json_object(self)
        history = await self.chat_manager.getHistory(
            _required(body, 'userId'),
            _required(body, 'sessionId'),
        )
        if history is None:
            raise tornado.web.HTTPError(404, log_message='对话会话不存在')
        self.write_response(200, 'success', history)


class ChatSuggestionsHandler(ChatHandler):
    async def get(self) -> None:
        try:
            await self.handle_suggestions()
        except tornado.web.HTTPError:
            raise
        except Exception as error:
            logger.error('获取推荐问题失败: %s', type(error).__name__)
            raise tornado.web.HTTPError(500, log_message=str(error))

    async def handle_suggestions(self) -> None:
        user_id = self.get_argument('userId', default=None)
        count_text = self.get_argument('count', default='3')
        try:
            count = int(count_text)
            if count < 1 or count > 10:
                raise tornado.web.HTTPError(
                    400,
                    log_message='参数 count 超出可选数量范围，必须在 1-10 之间',
                )
        except ValueError:
            raise tornado.web.HTTPError(400, log_message='参数 count 必须为整数')
        suggestions = await self.chat_manager.getSuggestions(user_id, count)
        self.write_response(200, 'success', {'suggestions': suggestions})


__all__ = [
    'ChatCustomMessageHandler',
    'ChatHandler',
    'ChatHistoryHandler',
    'ChatSendCompleteHandler',
    'ChatSendStreamHandler',
    'ChatSuggestionsHandler',
]
