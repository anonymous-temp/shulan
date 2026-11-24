"""Shared Tornado request/response adapter behavior."""

from __future__ import annotations

import uuid

import tornado.web
from loongspears.utils import time as timemark

from utils.exceptions import format_exception_summary
from utils.log import getLogger
from utils.log.context import request_log_context
from web.http_logging import log_access
from web.lifecycle import LifecycleRequestHandler

logger = getLogger(__name__)


@request_log_context
def log_request_exception(handler, error, tb, *, request_logger) -> None:
    """Log a handled HTTP failure once, with useful causes instead of a traceback."""
    request = handler.request
    status_code = error.status_code if isinstance(error, tornado.web.HTTPError) else 500
    summary = (
        format_exception_summary(error, tb)
        if getattr(handler, 'log_body_enabled', True)
        else f'error_type={type(error).__name__}; payload details omitted'
    )
    message = (
        'HTTP 请求异常: '
        f'request_uuid={getattr(handler, "request_uuid", "-")}, '
        f'status_code={status_code}, method={request.method}, '
        f'path={request.path}, remote_ip={request.remote_ip}, '
        f'{summary}'
    )
    if status_code >= 500:
        request_logger.error(message)
    else:
        request_logger.warning(message)


def log_tornado_request(handler: tornado.web.RequestHandler) -> None:
    """Route Tornado access logs through the project logger."""
    log_access(handler, request_logger=logger)


class BaseHandler(LifecycleRequestHandler):
    """Common response envelope, request identity and exception logging."""

    log_body_enabled = True

    def prepare(self) -> None:
        self.request_uuid = str(uuid.uuid4()).upper()
        self.request_start_time = timemark.now()
        super().prepare()

    def set_default_headers(self) -> None:
        self.set_header('Content-Type', 'application/json; charset=UTF-8')
        self.set_header('Access-Control-Allow-Origin', '*')
        self.set_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.set_header('Access-Control-Allow-Headers', 'Content-Type')

    def options(self, *args) -> None:
        self.set_status(204)
        self.finish()

    def write_response(self, code: int, message: str, data=None) -> None:
        elapsed_ms = timemark.elapsed(self.request_start_time, timemark.now())
        response = {
            'code': code,
            'message': message,
            'uuid': self.request_uuid,
            'timestamp': timemark.now().ts,
            'elapsed_ms': elapsed_ms,
            'data': data,
        }
        self.write(response)

    def write_error(self, status_code: int, **kwargs) -> None:
        error_map = {
            400: '参数错误',
            404: '资源不存在',
            409: '资源状态冲突',
            413: '文件大小超过限制',
            415: '不支持的文件格式',
            422: '校验失败',
            500: '服务器内部异常',
            502: '服务器上游异常',
            503: '服务正在排空，请稍后重试',
        }
        message = error_map.get(status_code, '未知错误')
        if 'exc_info' in kwargs:
            exception = kwargs['exc_info'][1]
            if hasattr(exception, 'log_message'):
                message = exception.log_message
        self.write_response(status_code, message, None)

    def log_exception(self, typ, value, tb) -> None:
        log_request_exception(self, value, tb, request_logger=logger)
