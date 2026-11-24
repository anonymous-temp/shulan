"""Agent-backed selector for an already-authorized report catalog."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents.graphs.report import REPORT_RETRIEVE_CONTEXT_CAPABILITY
from modules.report.ports import ReportContextSelection
from utils.log import getLogger

logger = getLogger(__name__)


class AgentReportContextSelector:
    """只在确定性规则无法区分多份报告时调用 Agent。"""

    def __init__(self, *, hub: AgentHub) -> None:
        if not isinstance(hub, AgentHub):
            raise TypeError('hub must be an AgentHub')
        self._hub = hub

    async def select(
        self,
        *,
        user_id: str,
        session_id: str,
        message: str,
        candidates: list[dict[str, Any]],
    ) -> ReportContextSelection | None:
        try:
            context = AgentRunContext.create(
                user_id=user_id,
                session_id=session_id,
                request_id=f'report-context:{uuid4().hex}',
            )
            result = await self._hub.invoke(
                REPORT_RETRIEVE_CONTEXT_CAPABILITY,
                AgentRequest(
                    {
                        'message': message,
                        'candidates': candidates,
                    }
                ),
                context,
            )
            parsed = _parse_selection(result.data.get('content'))
            if parsed is None or parsed.get('selected') is not True:
                return None
            final_report_id = parsed.get('finalReportId')
            report_type = parsed.get('reportType')
            if not isinstance(final_report_id, str) or not isinstance(report_type, str):
                return None
            for candidate in candidates:
                if candidate.get('finalReportId') != final_report_id:
                    continue
                allowed_types = candidate.get('availableReportTypes') or []
                if report_type in allowed_types:
                    return ReportContextSelection(
                        final_report_id=final_report_id,
                        report_type=report_type,
                    )
            logger.warning(
                'report context selector returned an unauthorized reference: user_id=%s session_id=%s',
                user_id,
                session_id,
            )
        except Exception as error:
            logger.warning(
                'report context selector unavailable; clarification required: user_id=%s session_id=%s error=%s',
                user_id,
                session_id,
                type(error).__name__,
            )
        return None


def _parse_selection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    match = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', text, flags=re.DOTALL)
    if match:
        text = match.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = ['AgentReportContextSelector']
