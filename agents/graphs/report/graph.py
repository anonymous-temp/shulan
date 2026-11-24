"""最终报告主叙事的无状态 LangGraph。

Graph 只负责一件事：校验非空对象 payload、序列化 JSON、调用 Gateway。
超时、重试、围栏清理、严格 JSON 对象解析和降级仍由报告适配器持有，
不在此处叠加第二层默认重试。确定性医学规则、证据绑定、Schema 校验、
回调与结果持久化都不属于 Graph。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from typing import TypedDict

from langgraph.graph import END
from langgraph.graph import START
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime

from ...errors import AgentConfigurationError
from ...llm.profiles import ModelProfile
from ...llm.protocol import LLMGateway
from ...llm.types import ContextPolicy
from ...llm.types import LLMRequest
from ...runtime.context import AgentRunContext
from .profile import REPORT_NARRATIVE_PROFILE

REPORT_NARRATIVE_CAPABILITY = 'report.narrative'
REPORT_NARRATIVE_GRAPH_VERSION = 'report-narrative-v1'


class ReportNarrativeState(TypedDict, total=False):
    """一次主叙事任务的临时状态，不保存报告正文或 checkpoint。"""

    payload: Mapping[str, Any]
    request_id: str
    run_id: str
    content: str
    reasoning_content: str
    usage: dict[str, Any]
    provider: str | None
    model: str | None
    status: str


def build_report_narrative_graph(
        *,
        gateway: LLMGateway,
        profile: ModelProfile,
        thinking_tasks: frozenset[str] = frozenset(),
        reasoning_effort: str | None = None,
) -> StateGraph:
    """构建最终报告主叙事的无状态 Graph。

    ``thinking_tasks`` 中列出的 payload.task 以思考模式调用模型，其余任务沿用
    Profile 默认值（关闭思考）。集合为空时行为与此前完全一致。
    """
    if not isinstance(gateway, LLMGateway):
        raise TypeError('gateway must implement LLMGateway')
    if not isinstance(profile, ModelProfile):
        raise TypeError('profile must be a ModelProfile')
    if profile.name != REPORT_NARRATIVE_PROFILE:
        raise AgentConfigurationError(
            'report narrative graph requires profile '
            f'{REPORT_NARRATIVE_PROFILE!r}',
        )
    if profile.context_policy is not ContextPolicy.STATELESS:
        raise AgentConfigurationError(
            'report narrative graph requires STATELESS context policy',
        )

    async def call_model(
            state: ReportNarrativeState,
            runtime: Runtime[AgentRunContext],
    ) -> ReportNarrativeState:
        payload = state.get('payload')
        if not isinstance(payload, Mapping) or not payload:
            raise ValueError(
                'report narrative graph requires a non-empty payload object',
            )

        # 与既有适配器一致：中文不转义、无多余空白，保证同一份 payload
        # 生成同一段模型输入。
        message = json.dumps(
            dict(payload),
            ensure_ascii=False,
            separators=(',', ':'),
        )

        thinking = payload.get('task') in thinking_tasks
        result = await gateway.complete(LLMRequest(
            profile=profile,
            content=message,
            thinking=True if thinking else None,
            reasoning_effort=reasoning_effort if thinking else None,
        ))
        context = runtime.context
        return {
            'request_id': context.request_id,
            'run_id': context.run_id,
            'content': result.content,
            'reasoning_content': result.reasoning_content or '',
            'usage': {} if result.usage is None else result.usage.to_dict(),
            'provider': result.provider,
            'model': result.model,
            'status': 'completed',
        }

    builder = StateGraph(
        ReportNarrativeState,
        context_schema=AgentRunContext,
    )
    builder.add_node('call_model', call_model)
    builder.add_edge(START, 'call_model')
    builder.add_edge('call_model', END)
    return builder
