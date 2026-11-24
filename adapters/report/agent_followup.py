"""AgentHub-backed model adapter for the four follow-up reports.

只承担「把一次受控任务交给模型并拿回文本」。产品目录与 HTTP 回调不是
模型调用，继续由 ``AionFollowupReportGateway`` 走既有业务端口。

超时（1800s）、重试（3 次）、退避和四路并发闸门仍由
``modules/report/reports.py`` 的 ``safe_complete_async`` 持有，本层不叠加，
以免迁移后重试相乘。
"""

from __future__ import annotations

from uuid import uuid4

from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents.graphs.report import REPORT_FOLLOWUP_CAPABILITY
from modules.report.agent import REPORT_FOLLOWUP_SELECTION


class AgentFollowupModelGateway:
    """一次报告执行专属的后续报告模型网关。

    绑定不可变的 user_id / session_id / 执行标识，因此并发报告不会串身份。
    借用共享 AgentHub，不持有也不关闭平台资源。
    """

    def __init__(
        self,
        *,
        hub: AgentHub,
        user_id: str,
        session_id: str,
        execution_id: str,
    ) -> None:
        self._hub = hub
        self._user_id = user_id
        self._session_id = session_id
        self._execution_id = execution_id

    async def complete_text(self, *, task_type: str, user_prompt: str) -> str:
        """执行一次受控任务；task_type 由 Graph 按服务端允许列表校验。"""
        context = AgentRunContext.create(
            user_id=self._user_id,
            session_id=self._session_id,
            request_id=f'{self._execution_id}:{uuid4().hex}',
        )
        result = await self._hub.invoke(
            REPORT_FOLLOWUP_CAPABILITY,
            AgentRequest({'task_type': task_type, 'user_prompt': user_prompt}),
            context,
            selection=REPORT_FOLLOWUP_SELECTION,
        )
        return str(result.data.get('content') or '')


class FollowupModelGatewayFactory:
    """应用级单例：共享 AgentHub，不共享报告身份。"""

    def __init__(self, *, hub: AgentHub) -> None:
        if not isinstance(hub, AgentHub):
            raise TypeError('hub must be an AgentHub')
        self._hub = hub

    def create(
        self,
        *,
        user_id: str,
        session_id: str,
        execution_id: str | None = None,
    ) -> AgentFollowupModelGateway:
        return AgentFollowupModelGateway(
            hub=self._hub,
            user_id=user_id,
            session_id=session_id,
            execution_id=execution_id or uuid4().hex,
        )


__all__ = [
    'AgentFollowupModelGateway',
    'FollowupModelGatewayFactory',
]
