"""Tornado inbound adapters for reading stored final reports.

两个接口都经 Report 自有的只读端口访问，不直接操作 MongoDB 集合。
所有读取都校验用户归属：错误的引用明确失败，不会回退到其他报告。
"""

from __future__ import annotations

import json
from typing import Any

import tornado.web

from modules.report.artifacts import REPORT_TYPES
from utils.log import getLogger
from web.handlers.base import BaseHandler

logger = getLogger(__name__)

MAX_LIST_LIMIT = 100
DEFAULT_LIST_LIMIT = 20


class ReportQueryBaseHandler(BaseHandler):
    """共享入参解析与错误映射；具体读取由子类实现。"""

    def initialize(self, service: Any) -> None:
        # 与其他业务路由一致，统一使用 service 作为显式注入参数名。
        self.artifact_store = service

    async def post(self) -> None:
        try:
            body = self._json_object()
            data = await self.handle_query(body)
            self.write_response(200, 'success', data)
        except tornado.web.HTTPError:
            raise
        except Exception as error:
            # 不把存储异常或报告正文泄漏进响应。
            logger.error('查询最终报告失败: %s', type(error).__name__)
            raise tornado.web.HTTPError(500, log_message='查询最终报告失败')

    async def handle_query(self, body: dict) -> Any:  # pragma: no cover
        raise NotImplementedError

    def _json_object(self) -> dict:
        try:
            body = json.loads(self.request.body)
        except json.JSONDecodeError as exc:
            raise tornado.web.HTTPError(400, log_message='无效的JSON格式') from exc
        if not isinstance(body, dict):
            raise tornado.web.HTTPError(400, log_message='请求体必须为JSON对象')
        self.bind_log_identity(body)
        return body

    @staticmethod
    def _required_text(body: dict, field: str) -> str:
        value = body.get(field)
        if not isinstance(value, str) or not value.strip():
            raise tornado.web.HTTPError(
                400,
                log_message=f'缺少必填参数: {field}',
            )
        return value.strip()

    @staticmethod
    def _optional_text(body: dict, field: str) -> str | None:
        value = body.get(field)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise tornado.web.HTTPError(
                400,
                log_message=f'参数格式错误: {field}',
            )
        return value.strip()


class ReportListHandler(ReportQueryBaseHandler):
    """按用户查询报告批次及五类产物状态。"""

    async def handle_query(self, body: dict) -> Any:
        user_id = self._required_text(body, 'userId')
        session_id = self._optional_text(body, 'sessionId')
        cursor = self._optional_text(body, 'cursor')
        limit = body.get('limit', DEFAULT_LIST_LIMIT)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise tornado.web.HTTPError(400, log_message='limit 必须为整数')
        if not 1 <= limit <= MAX_LIST_LIMIT:
            raise tornado.web.HTTPError(
                400,
                log_message=f'limit 必须为 1 到 {MAX_LIST_LIMIT} 的整数',
            )
        return await self.artifact_store.list_batches(
            user_id=user_id,
            session_id=session_id,
            limit=limit,
            cursor=cursor,
        )


class ReportGetHandler(ReportQueryBaseHandler):
    """按 userId + finalReportId + reportType 读取指定产物。"""

    async def handle_query(self, body: dict) -> Any:
        user_id = self._required_text(body, 'userId')
        final_report_id = self._required_text(body, 'finalReportId')
        report_type = self._required_text(body, 'reportType')
        if report_type not in REPORT_TYPES:
            raise tornado.web.HTTPError(
                400,
                log_message=f'reportType 必须为 {"、".join(REPORT_TYPES)} 之一',
            )
        record = await self.artifact_store.get_artifact(
            user_id=user_id,
            final_report_id=final_report_id,
            report_type=report_type,
        )
        if record is None:
            # 不存在、不属于当前用户或尚未生成都明确失败，不静默切换报告。
            raise tornado.web.HTTPError(404, log_message='报告产物不存在或尚未生成')
        data = {
            'userId': record.user_id,
            'finalReportId': record.final_report_id,
            'sessionId': record.session_id,
            'reportType': record.report_type,
            'generationStatus': record.generation_status,
            # 所有产物都是 AI 生成、未经医生核验。
            'reviewStatus': record.review_status,
            'documents': list(record.documents),
            'body': record.body,
        }
        if hasattr(self.artifact_store, 'get_delivery'):
            data['delivery'] = await self.artifact_store.get_delivery(
                user_id=user_id, final_report_id=final_report_id, report_type=report_type,
            )
        return data


__all__ = ['ReportGetHandler', 'ReportListHandler']
