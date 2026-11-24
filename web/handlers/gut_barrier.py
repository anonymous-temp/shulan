"""New routes only; request/response bodies are excluded from HTTP previews."""
from __future__ import annotations

import json
import math
import uuid

import tornado.web
from loongspears.utils import time as timemark
from pydantic import ValidationError

from modules.gut_barrier.schemas import GutBarrierRequest
from runtime import TaskAdmissionClosedError
from utils.log.context import LogContext
from utils.log.context import bind_log_context
from utils.log.context import update_log_context
from web.handlers.base import BaseHandler


class GutBarrierBaseHandler(BaseHandler):
    def initialize(self, *, service=None, task_supervisor=None):
        self.service, self.task_supervisor = service, task_supervisor

    def prepare(self):
        # Deliberately do not call the legacy body-preview hook for this new
        # medical endpoint. The inherited request context/finish lease still apply.
        self.request_uuid = str(uuid.uuid4()).upper()
        self.request_start_time = timemark.now()
        self._log_context = update_log_context(request_uuid=self.request_uuid)
        self._request_lease = None
        lifecycle = self.application.settings.get("lifecycle")
        if lifecycle is not None:
            self._request_lease = lifecycle.try_begin_request()
            if self._request_lease is None:
                raise tornado.web.HTTPError(503, log_message="服务正在排空")
        self.set_header("Cache-Control", "no-store")

    def write(self, chunk):
        # Retain status-level access logging, not patient data or signed URLs.
        if isinstance(chunk, dict):
            self._http_log_code = chunk.get("code")
        tornado.web.RequestHandler.write(self, chunk)

    def parse(self, model):
        if len(self.request.body) > 2 * 1024 * 1024:
            raise tornado.web.HTTPError(413, log_message="请求体超过2MB限制")
        try:
            value = json.loads(self.request.body, parse_constant=_invalid_constant,
                               parse_float=_finite_float, parse_int=_bounded_integer,
                               object_pairs_hook=_unique_object)
            if not isinstance(value, dict):
                raise ValueError()
            self.bind_log_identity(value)
            return model.model_validate(value)
        except ValidationError as error:
            # Unknown keys can themselves contain patient data or signed URLs.
            known = {field.alias or key for key, field in model.model_fields.items()} | set(model.model_fields)
            fields = [str(item["loc"][0]) if item["loc"] and item["loc"][0] in known
                      else "未知字段" for item in error.errors()[:5]]
            raise tornado.web.HTTPError(400, log_message="字段校验失败：" + ", ".join(fields)) from None
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise tornado.web.HTTPError(400, log_message="请提交标准JSON对象，不含注释或HTML转义字符") from None

    def require_service(self):
        if self.service is None:
            raise tornado.web.HTTPError(503, log_message="肠道屏障报告服务尚未启用")


def _invalid_constant(value):
    raise ValueError("non-finite JSON number")


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _bounded_integer(value):
    parsed = int(value)
    if not -(2 ** 63) <= parsed < 2 ** 63:
        raise ValueError("JSON integer exceeds storage range; use a string for source values")
    return parsed


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result and not _same_json_value(result[key], value):
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _same_json_value(first, second):
    # bool is an int subclass: ordinary equality would silently equate true and 1.
    if type(first) is not type(second):
        return False
    if isinstance(first, dict):
        return (first.keys() == second.keys()
                and all(_same_json_value(first[key], second[key]) for key in first))
    if isinstance(first, list):
        return len(first) == len(second) and all(_same_json_value(a, b) for a, b in zip(first, second))
    return first == second


class GutBarrierGenerateHandler(GutBarrierBaseHandler):
    async def post(self):
        request = self.parse(GutBarrierRequest)
        self.require_service()
        if self.task_supervisor is None or not self.task_supervisor.accepting:
            raise tornado.web.HTTPError(503, log_message="报告任务暂不可受理")
        # Wake delivery independently after the terminal JSON is durable. The
        # periodic worker remains the fallback during shutdown or process loss.
        supervisor, service = self.task_supervisor, self.service

        def wake_delivery():
            if supervisor.accepting:
                try:
                    with bind_log_context(LogContext()):
                        supervisor.create_task(service.drain_delivery(), name="worker:gut-barrier-callback-wake")
                except TaskAdmissionClosedError:
                    pass

        service.store.on_delivery_ready = wake_delivery
        try:
            job = await self.service.submit(request)
        except Exception:
            raise tornado.web.HTTPError(503, log_message="报告任务尚未持久化，请稍后重试") from None
        try:
            with bind_log_context(LogContext()):
                self.task_supervisor.create_task(self.service.drain_generation(), name="gut-barrier:generation")
        except TaskAdmissionClosedError:
            # Admission is already durable. Startup recovery owns the queued job.
            pass
        self.write_response(200, "报告生成任务已受理", {
            "userId": job["userId"], "sessionId": job["sessionId"],
            "finalReportId": job["_id"], "status": "processing",
        })
        await self.finish()
