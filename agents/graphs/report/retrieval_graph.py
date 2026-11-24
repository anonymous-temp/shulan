"""最终报告候选选择的无状态 LangGraph。

Graph 只能在业务层提供的已授权有限目录中选择一个报告批次和
报告类型。它不读取报告正文、不访问数据库，也不决定用户权限。
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
from .profile import REPORT_RETRIEVE_CONTEXT_PROFILE

REPORT_RETRIEVE_CONTEXT_CAPABILITY = 'report.retrieve_context'
REPORT_RETRIEVE_CONTEXT_GRAPH_VERSION = 'report-retrieve-context-v1'


class ReportRetrieveContextState(TypedDict, total=False):
    """一次报告候选选择的临时状态。"""

    message: str
    candidates: list[Mapping[str, Any]]
    request_id: str
    run_id: str
    content: str
    usage: dict[str, Any]
    provider: str | None
    model: str | None
    status: str


def build_report_retrieve_context_graph(
    *,
    gateway: LLMGateway,
    profile: ModelProfile,
) -> StateGraph:
    """构建仅处理已授权候选目录的选择 Graph。"""
    if not isinstance(gateway, LLMGateway):
        raise TypeError('gateway must implement LLMGateway')
    if not isinstance(profile, ModelProfile):
        raise TypeError('profile must be a ModelProfile')
    if profile.name != REPORT_RETRIEVE_CONTEXT_PROFILE:
        raise AgentConfigurationError(
            f'report retrieve context graph requires profile {REPORT_RETRIEVE_CONTEXT_PROFILE!r}',
        )
    if profile.context_policy is not ContextPolicy.STATELESS:
        raise AgentConfigurationError(
            'report retrieve context graph requires STATELESS context policy',
        )

    async def select_candidate(
        state: ReportRetrieveContextState,
        runtime: Runtime[AgentRunContext],
    ) -> ReportRetrieveContextState:
        message = state.get('message')
        if not isinstance(message, str) or not message.strip():
            raise ValueError(
                'report retrieve context graph requires a non-empty message',
            )
        candidates = _validated_candidates(state.get('candidates'))
        result = await gateway.complete(
            LLMRequest(
                profile=profile,
                content=json.dumps(
                    {
                        'userMessage': message,
                        'authorizedCandidates': candidates,
                    },
                    ensure_ascii=False,
                    separators=(',', ':'),
                ),
            )
        )
        context = runtime.context
        return {
            'request_id': context.request_id,
            'run_id': context.run_id,
            'content': result.content,
            'usage': {} if result.usage is None else result.usage.to_dict(),
            'provider': result.provider,
            'model': result.model,
            'status': 'completed',
        }

    builder = StateGraph(
        ReportRetrieveContextState,
        context_schema=AgentRunContext,
    )
    builder.add_node('select_candidate', select_candidate)
    builder.add_edge(START, 'select_candidate')
    builder.add_edge('select_candidate', END)
    return builder


def _validated_candidates(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) < 2:
        raise ValueError(
            'report retrieve context graph requires at least two candidates',
        )
    candidates: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError('report candidate must be an object')
        final_report_id = item.get('finalReportId')
        report_types = item.get('availableReportTypes')
        if not isinstance(final_report_id, str) or not final_report_id.strip():
            raise ValueError('report candidate requires finalReportId')
        if (
            not isinstance(report_types, list)
            or not report_types
            or not all(isinstance(name, str) and name.strip() for name in report_types)
        ):
            raise ValueError(
                'report candidate requires non-empty availableReportTypes',
            )
        candidates.append(
            {
                'finalReportId': final_report_id.strip(),
                'createdAt': item.get('createdAt'),
                'availableReportTypes': [name.strip() for name in report_types],
            }
        )
    return candidates


__all__ = [
    'REPORT_RETRIEVE_CONTEXT_CAPABILITY',
    'REPORT_RETRIEVE_CONTEXT_GRAPH_VERSION',
    'ReportRetrieveContextState',
    'build_report_retrieve_context_graph',
]
