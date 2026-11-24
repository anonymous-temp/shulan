"""把体检报告解读能力安装到 AgentRuntime。"""

from __future__ import annotations

from ...runtime.container import AgentRuntime
from ...runtime.registry import GraphRegistration
from .graph import ANALYSIS_HEALTH_REPORT_CAPABILITY
from .graph import ANALYSIS_HEALTH_REPORT_GRAPH_VERSION
from .graph import build_analysis_health_report_graph
from .profile import create_analysis_health_report_profile


def register_analysis_health_report(
        runtime: AgentRuntime,
        *,
        model: str | None = None,
) -> GraphRegistration:
    """注册不使用模型 Context 和 checkpoint 的报告解读能力。"""
    if not isinstance(runtime, AgentRuntime):
        raise TypeError('runtime must be an AgentRuntime')

    profile = runtime.profiles.register(
        create_analysis_health_report_profile(model=model),
    )
    builder = build_analysis_health_report_graph(
        gateway=runtime.llm_gateway,
        profile=profile,
    )
    return runtime.register_graph(
        ANALYSIS_HEALTH_REPORT_CAPABILITY,
        builder,
        version=ANALYSIS_HEALTH_REPORT_GRAPH_VERSION,
        checkpointed=False,
    )
