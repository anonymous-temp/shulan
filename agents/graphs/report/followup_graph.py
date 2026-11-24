"""后续四类报告内部模型任务的无状态 LangGraph。

与主叙事共用 Agent 基础设施，但 Profile、Prompt 和调用参数完全隔离：
主叙事禁止推荐产品，本能力的固定 Prompt 是产品与干预导向的，两者不能合并。

Graph 只做三件事：按服务端允许列表校验 task_type、校验非空 user_prompt、
调用一次 Gateway。超时、重试、并发闸门、JSON 提取和空报告降级仍由
``modules/report/reports.py`` 持有，不在此处叠加第二层。
"""

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
from .profile import REPORT_FOLLOWUP_PROFILE

REPORT_FOLLOWUP_CAPABILITY = 'report.followup'
REPORT_FOLLOWUP_GRAPH_VERSION = 'report-followup-v1'

# 服务端固定的任务允许列表。task_type 不是对外请求字段，只用于区分
# 报告内部已生效的模型任务；调用方不能借它传入任意系统 Prompt 或模型。
REPORT_FOLLOWUP_TASK_TYPES: tuple[str, ...] = (
    # 依赖项
    'info',
    'recommend',
    # intervene
    'diet',
    'exercise',
    'supplements',
    # instruction
    'usage',
    'exception_handling',
    # 'doctor_advice',  # [已停用] 由 non_drug_intervention.lifestyle_intervention 取代
    'notes',
    # 三位一体非药物干预方案
    'lifestyle',
    'gut_microbiota',
    'nutrient_supplements',
    # 'medical_alert',  # [已移除] 处方/医嘱中不再输出就医提示
    'recommended_tests',
    # explain（解读参考手册：四组五件套 + 开场白/收尾脚本）
    'explain_part1',
    'explain_part2',
    'explain_part3',
    'explain_part4',
    'explain_opening',
    # [已停用] 旧三段式：'explain_summary', 'explain_basis', 'explain_advice'
    # 肠道与炎症报告解读（入参 type=gutBarrier）
    'gut_barrier_part1',
    'gut_barrier_part2',
    'gut_barrier_opening',
    # opinion
    'opinion_summary',
    'opinion_basis',
    'opinion_advice',
)


class ReportFollowupState(TypedDict, total=False):
    """一次后续报告模型任务的临时状态，不保存正文或 checkpoint。"""

    task_type: str
    user_prompt: str
    request_id: str
    run_id: str
    content: str
    usage: dict[str, Any]
    provider: str | None
    model: str | None
    status: str


def build_report_followup_graph(
        *,
        gateway: LLMGateway,
        profile: ModelProfile,
        task_types: tuple[str, ...] = REPORT_FOLLOWUP_TASK_TYPES,
) -> StateGraph:
    """构建后续报告内部模型任务的无状态 Graph。"""
    if not isinstance(gateway, LLMGateway):
        raise TypeError('gateway must implement LLMGateway')
    if not isinstance(profile, ModelProfile):
        raise TypeError('profile must be a ModelProfile')
    if profile.name != REPORT_FOLLOWUP_PROFILE:
        raise AgentConfigurationError(
            'report followup graph requires profile '
            f'{REPORT_FOLLOWUP_PROFILE!r}',
        )
    if profile.context_policy is not ContextPolicy.STATELESS:
        raise AgentConfigurationError(
            'report followup graph requires STATELESS context policy',
        )
    if not task_types:
        raise AgentConfigurationError(
            'report followup graph requires a non-empty task allow list',
        )
    # 启动阶段冻结，运行阶段只读。
    allowed = frozenset(task_types)

    async def call_model(
            state: ReportFollowupState,
            runtime: Runtime[AgentRunContext],
    ) -> ReportFollowupState:
        task_type = state.get('task_type')
        if task_type not in allowed:
            raise AgentConfigurationError(
                f'unknown report followup task_type: {task_type!r}',
            )
        user_prompt = state.get('user_prompt')
        if not isinstance(user_prompt, str) or not user_prompt.strip():
            raise ValueError(
                'report followup graph requires non-empty user_prompt',
            )

        result = await gateway.complete(LLMRequest(
            profile=profile,
            content=user_prompt,
        ))
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
        ReportFollowupState,
        context_schema=AgentRunContext,
    )
    builder.add_node('call_model', call_model)
    builder.add_edge(START, 'call_model')
    builder.add_edge('call_model', END)
    return builder
