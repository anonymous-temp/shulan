"""Tornado inbound adapter for ``POST /api/v1/report``."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import tornado.web
from pydantic import ValidationError

from modules.report.pipeline import ReportPipeline
from modules.report.schemas import ReportRequest
from modules.report.task_coordinator import ReportTaskCoordinator
from modules.report.task_coordinator import report_request_fingerprint
from runtime import TaskSupervisor
from utils.log import getLogger
from utils.log.context import LogContext
from utils.log.context import bind_log_context
from web.lifecycle import LifecycleRequestHandler
from web.lifecycle import TaskAdmissionClosedError

__all__ = ['ReportGenerateHandler']

logger = getLogger(__name__)


class ReportGenerateHandler(LifecycleRequestHandler):
    """Validate and acknowledge a report request before background work."""

    def initialize(
        self,
        *,
        pipeline: ReportPipeline,
        task_supervisor: TaskSupervisor,
        task_coordinator: ReportTaskCoordinator,
        service: Any = None,
    ) -> None:
        self.pipeline = pipeline
        self.task_supervisor = task_supervisor
        self.task_coordinator = task_coordinator
        self.report_service = service

    def prepare(self) -> None:
        self.request_uuid = str(uuid.uuid4()).upper()
        self.request_started = time.monotonic()
        super().prepare()

    def set_default_headers(self) -> None:
        self.set_header('Content-Type', 'application/json; charset=UTF-8')
        self.set_header('Access-Control-Allow-Origin', '*')
        self.set_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.set_header('Access-Control-Allow-Headers', 'Content-Type')

    def options(self) -> None:
        self.set_status(204)
        self.finish()

    async def post(self) -> None:
        try:
            body = json.loads(self.request.body)
            self.bind_log_identity(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise tornado.web.HTTPError(400, log_message='无效的JSON格式') from exc
        if not isinstance(body, dict):
            raise tornado.web.HTTPError(400, log_message='请求体必须是JSON对象')
        try:
            request = ReportRequest.model_validate(body)
        except ValidationError as exc:
            raise tornado.web.HTTPError(
                400,
                log_message=_validation_message(exc),
            ) from exc

        if self.report_service is not None:
            await self._admit_durable(request)
            return

        # Explicitly injected legacy pipelines retain their test/integration
        # compatibility path. The production composition root supplies service.
        # 批次身份必须在受理响应之前确定：运行中的相同规范化请求与短期
        # 完成结果重投复用原 finalReportId，过期后真正重新生成才分配新批次。
        fingerprint = report_request_fingerprint(request)
        final_report_id = self.task_coordinator.acquire_batch(fingerprint)

        try:
            self.task_supervisor.create_task(
                self._run_background(
                    request,
                    fingerprint=fingerprint,
                    final_report_id=final_report_id,
                ),
                name=f'report:{self.request_uuid}',
            )
        except TaskAdmissionClosedError as exc:
            raise tornado.web.HTTPError(
                503,
                log_message='服务正在排空，暂不接收新的报告任务',
            ) from exc

        self.write_response(
            200,
            '报告生成任务已受理',
            {
                'userId': request.user_id,
                'sessionId': request.session_id,
                'finalReportId': final_report_id,
                'status': 'processing',
            },
        )
        await self.finish()

    async def _admit_durable(self, request: ReportRequest) -> None:
        try:
            batch = await self.report_service.submit(request)
        except Exception as error:
            logger.error('报告任务无法持久化受理: error_type=%s', type(error).__name__)
            raise tornado.web.HTTPError(503, log_message='报告任务存储暂不可用，请稍后重试') from None
        try:
            # Scans may claim other users' jobs; register completion callbacks
            # without the triggering request identity as well.
            with bind_log_context(LogContext()):
                self.task_supervisor.create_task(
                    self.report_service.drain_generation(), name=f'report-generation:{self.request_uuid}',
                )
        except TaskAdmissionClosedError:
            # It is already durable; the next process resumes queued work.
            logger.info('报告任务已持久化，等待恢复调度: final_report_id=%s', batch['finalReportId'])
        self.write_response(200, '报告生成任务已受理', {
            'userId': request.user_id, 'sessionId': request.session_id,
            'finalReportId': batch['finalReportId'], 'status': 'processing',
        })
        await self.finish()

    async def _run_background(
        self,
        request: ReportRequest,
        *,
        fingerprint: str | None = None,
        final_report_id: str | None = None,
    ) -> None:
        # Let Tornado flush the processing acknowledgement before source work.
        await asyncio.sleep(0.01)
        if fingerprint is None:
            fingerprint = report_request_fingerprint(request)
        try:
            if not hasattr(self.pipeline, 'replay_execution'):
                await self.pipeline.execute_and_callback(
                    request,
                    final_report_id=final_report_id,
                )
                return
            await self.task_coordinator.execute(
                fingerprint,
                runner=lambda: self.pipeline.execute_and_callback(
                    request,
                    final_report_id=final_report_id,
                ),
                replay=self.pipeline.replay_execution,
            )
        except Exception as error:
            logger.error(
                'report background task or callback failed: user_id=%s session_id=%s error_type=%s',
                request.user_id,
                request.session_id,
                type(error).__name__,
            )

    def write_response(self, code: int, message: str, data: Any = None) -> None:
        self.write(
            {
                'code': code,
                'message': message,
                'uuid': self.request_uuid,
                'timestamp': int(time.time()),
                'elapsed_ms': round(
                    (time.monotonic() - self.request_started) * 1000,
                    3,
                ),
                'data': data,
            }
        )

    def write_error(self, status_code: int, **kwargs: Any) -> None:
        # TODO 字典不应该每次调用 write_error 的时候重复加载
        message = {
            400: '参数错误',
            413: '文件大小超过限制',
            415: '不支持的文件格式',
            422: '校验失败',
            500: '服务器内部异常',
            503: '服务正在排空，请稍后重试',
        }.get(status_code, '请求失败')
        if exc_info := kwargs.get('exc_info'):
            exception = exc_info[1]
            if isinstance(exception, tornado.web.HTTPError) and exception.log_message:
                message = exception.log_message
        self.set_status(status_code)
        self.finish(
            {
                'code': status_code,
                'message': message,
                'uuid': getattr(
                    self,
                    'request_uuid',
                    str(uuid.uuid4()).upper(),
                ),
                'timestamp': int(time.time()),
                'elapsed_ms': round(
                    (time.monotonic() - getattr(self, 'request_started', time.monotonic())) * 1000,
                    3,
                ),
                'data': None,
            }
        )


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors(include_url=False, include_input=False)[0]
    location = '.'.join(str(item) for item in first.get('loc', []))
    message = str(first.get('msg') or '校验失败')
    return f'{location}: {message}' if location else message
