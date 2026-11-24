"""体检报告结构化解读的无状态 LangGraph。"""

from __future__ import annotations

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
from .profile import ANALYSIS_HEALTH_REPORT_PROFILE

ANALYSIS_HEALTH_REPORT_CAPABILITY = 'analysis.health_report'
ANALYSIS_HEALTH_REPORT_GRAPH_VERSION = 'analysis-health-report-v3'


class AnalysisHealthReportState(TypedDict, total=False):
    """一次报告解读任务的临时状态，不保存报告正文或 checkpoint。"""

    report_text: str
    request_id: str
    run_id: str
    content: str
    reasoning_content: str
    usage: dict[str, Any]
    provider: str | None
    model: str | None
    status: str
    output_token_limit: int | None
    transport_retries: int | None


def build_analysis_health_report_graph(
    *,
    gateway: LLMGateway,
    profile: ModelProfile,
) -> StateGraph:
    """构建通过模型抽取报告结构化结果的无状态 Graph。"""
    if not isinstance(gateway, LLMGateway):
        raise TypeError('gateway must implement LLMGateway')
    if not isinstance(profile, ModelProfile):
        raise TypeError('profile must be a ModelProfile')
    if profile.name not in {ANALYSIS_HEALTH_REPORT_PROFILE, 'analysis.health_report.current'}:
        raise AgentConfigurationError(
            f'analysis health report graph requires profile {ANALYSIS_HEALTH_REPORT_PROFILE!r}',
        )
    if profile.context_policy is not ContextPolicy.STATELESS:
        raise AgentConfigurationError(
            'analysis health report graph requires STATELESS context policy',
        )

    async def call_model(
        state: AnalysisHealthReportState,
        runtime: Runtime[AgentRunContext],
    ) -> AnalysisHealthReportState:
        report_text = state.get('report_text')
        if not isinstance(report_text, str) or not report_text.strip():
            raise ValueError(
                'analysis health report graph requires non-empty report_text',
            )

        result = await gateway.complete(
            LLMRequest(
                profile=profile,
                content=report_text,
            )
        )
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
            'output_token_limit': profile.model_kwargs.get('max_tokens'),
            'transport_retries': result.transport_retries,
        }

    builder = StateGraph(
        AnalysisHealthReportState,
        context_schema=AgentRunContext,
    )
    builder.add_node('call_model', call_model)
    builder.add_edge(START, 'call_model')
    builder.add_edge('call_model', END)
    return builder
