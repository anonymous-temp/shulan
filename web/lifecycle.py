"""Tornado 生命周期适配器及旧导入路径兼容层。"""

from __future__ import annotations

import json
import logging

import tornado.web

from config import env
from runtime import ApplicationLifecycle
from runtime import LifecycleSnapshot
from runtime import LifecycleState
from runtime import RequestLease
from runtime import SupervisedTaskSnapshot
from runtime import TaskAdmissionClosedError
from runtime import TaskSupervisor
from utils.log.context import LogContext
from utils.log.context import bind_log_context
from utils.log.context import log_context
from utils.log.context import update_log_context
from web.http_logging import capture_response
from web.http_logging import log_request_start


class LifecycleRequestHandler(tornado.web.RequestHandler):
    """为业务 Handler 提供统一的 draining 拒绝与请求租约。"""

    lifecycle_exempt = False

    async def _execute(self, *args, **kwargs):
        # on_finish can run before an SSE coroutine finishes its background work.
        # Reset in the same task, only after Tornado's entire execution returns.
        with bind_log_context(LogContext()):
            self._log_context = log_context()
            return await super()._execute(*args, **kwargs)

    def bind_log_identity(self, body) -> None:
        if not isinstance(body, dict):
            return
        fields = {}
        for source, target in (('userId', 'user_id'), ('sessionId', 'session_id')):
            value = body.get(source)
            if isinstance(value, str) and value.strip():
                fields[target] = value
        self._log_context = update_log_context(**fields)

    def clear(self) -> None:
        super().clear()
        # Tornado also clears pending output before replacing it with an error.
        self._http_log_parts: list[str] = []
        self._http_log_bytes = 0
        self._http_log_writes = 0
        self._http_log_code = None
        self._http_log_access_logged = False
        self._http_log_stream_summary = None
        self._http_log_stream_events = 0
        self._http_log_stream_flushed = 0
        self._http_log_stream_errors = 0
        self._http_log_stream_failed = False
        self._http_log_stream_finished = False
        self._http_log_stream_level = logging.INFO

    def write(self, chunk) -> None:
        super().write(chunk)
        capture_response(self, chunk)

    def prepare(self) -> None:
        self._log_context = update_log_context(request_uuid=getattr(self, 'request_uuid', None))
        try:
            if self.request.body and len(self.request.body) <= env.HTTP_LOG_PARSE_BYTES:
                self.bind_log_identity(json.loads(self.request.body))
            elif not self.request.body:
                self.bind_log_identity({
                    name: self.get_argument(name, None) for name in ('userId', 'sessionId')
                })
        except (ValueError, UnicodeDecodeError, RecursionError, tornado.web.HTTPError):
            pass
        log_request_start(self)
        super().prepare()
        self._request_lease: RequestLease | None = None
        lifecycle = self.application.settings.get('lifecycle')
        if lifecycle is None or self.lifecycle_exempt:
            return
        if not isinstance(lifecycle, ApplicationLifecycle):
            raise RuntimeError('application lifecycle is misconfigured')
        lease = lifecycle.try_begin_request()
        if lease is None:
            self.set_header('Connection', 'close')
            self.set_header('Retry-After', '5')
            raise tornado.web.HTTPError(
                503,
                log_message='服务正在排空，暂不接收新请求',
            )
        self._request_lease = lease

    def on_finish(self) -> None:
        lease = getattr(self, '_request_lease', None)
        if lease is not None:
            lease.release()
            self._request_lease = None
        super().on_finish()


__all__ = (
    'ApplicationLifecycle',
    'LifecycleRequestHandler',
    'LifecycleSnapshot',
    'LifecycleState',
    'RequestLease',
    'SupervisedTaskSnapshot',
    'TaskAdmissionClosedError',
    'TaskSupervisor',
)
