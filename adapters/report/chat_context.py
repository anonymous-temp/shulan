"""Chat-facing report context adapter.

把 Report 的确定性选择与预算渲染接到 Chat 的出站端口上。只经
``ReportArtifactStore`` 只读门面访问，不直接操作 MongoDB 集合，也不会把
报告正文发送到外部循证检索接口。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from modules.report.chat_context import build_report_chat_context
from modules.report.ports import ReportContextSelector
from utils.log import getLogger

logger = getLogger(__name__)


class ReportChatContextProvider:
    """按需为一轮对话提供报告补充内容。"""

    def __init__(
        self,
        *,
        store: Any,
        selector: ReportContextSelector | None = None,
    ) -> None:
        self._store = store
        self._selector = selector

    async def build_context(
        self,
        *,
        user_id: str,
        session_id: str,
        message: str,
        final_report_id: str | None = None,
        report_type: str | None = None,
        session_reference: Mapping[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any] | None] | None:
        """返回（补充文本，可复用的报告引用）；无可用报告时返回 None。"""
        try:
            selected = await build_report_chat_context(
                self._store,
                user_id=user_id,
                session_id=session_id,
                message=message,
                final_report_id=final_report_id,
                report_type=report_type,
                session_reference=dict(session_reference or {}) or None,
                candidate_selector=self._selector,
            )
        except Exception:
            # 查询异常按无报告降级，普通聊天继续。
            logger.warning(
                "report chat context unavailable: user_id=%s session_id=%s",
                user_id,
                session_id,
                exc_info=True,
            )
            return None
        if selected is None or not selected.text.strip():
            return None
        reference = (
            {
                "finalReportId": selected.final_report_id,
                "reportType": selected.report_type,
            }
            if selected.final_report_id and selected.report_type
            else None
        )
        return selected.text, reference


__all__ = ["ReportChatContextProvider"]
