"""把最终报告主叙事能力安装到 AgentRuntime。"""

from __future__ import annotations

from ...runtime.container import AgentRuntime
from ...runtime.registry import GraphRegistration
from .graph import REPORT_NARRATIVE_CAPABILITY
from .graph import REPORT_NARRATIVE_GRAPH_VERSION
from .graph import build_report_narrative_graph
from .followup_graph import REPORT_FOLLOWUP_CAPABILITY
from .followup_graph import REPORT_FOLLOWUP_GRAPH_VERSION
from .followup_graph import build_report_followup_graph
from .profile import REPORT_THINKING_TASKS
from .profile import create_report_followup_profile
from .profile import create_report_narrative_profile
from .profile import create_report_retrieve_context_profile
from .retrieval_graph import REPORT_RETRIEVE_CONTEXT_CAPABILITY
from .retrieval_graph import REPORT_RETRIEVE_CONTEXT_GRAPH_VERSION
from .retrieval_graph import build_report_retrieve_context_graph


def _narrative_reasoning() -> tuple[bool, str | None]:
    """Deployment switch for chapter-task thinking; isolated tests keep it off."""
    try:
        from config.env import REPORT_NARRATIVE_REASONING_EFFORT
        from config.env import REPORT_NARRATIVE_THINKING
    except Exception:
        return False, None
    return bool(REPORT_NARRATIVE_THINKING), REPORT_NARRATIVE_REASONING_EFFORT


def _narrative_prompt() -> str:
    """The former per-cell path keeps the prompt it was tuned with."""
    try:
        from config.env import REPORT_AUTHORING_MODE
    except Exception:
        return 'report/narrative.txt'
    return 'report/narrative_cell.txt' if REPORT_AUTHORING_MODE == 'cell' else 'report/narrative.txt'


def register_report_narrative(
        runtime: AgentRuntime,
        *,
        model: str | None = None,
) -> GraphRegistration:
    """注册不使用模型 Context 和 checkpoint 的主叙事能力。"""
    if not isinstance(runtime, AgentRuntime):
        raise TypeError('runtime must be an AgentRuntime')

    profile = runtime.profiles.register(
        create_report_narrative_profile(model=model, prompt_name=_narrative_prompt()),
    )
    thinking, effort = _narrative_reasoning()
    builder = build_report_narrative_graph(
        gateway=runtime.llm_gateway,
        profile=profile,
        thinking_tasks=REPORT_THINKING_TASKS if thinking else frozenset(),
        reasoning_effort=effort,
    )
    return runtime.register_graph(
        REPORT_NARRATIVE_CAPABILITY,
        builder,
        version=REPORT_NARRATIVE_GRAPH_VERSION,
        checkpointed=False,
    )


def register_report_followup(
        runtime: AgentRuntime,
        *,
        model: str | None = None,
) -> GraphRegistration:
    """注册不使用模型 Context 和 checkpoint 的后续报告任务能力。"""
    if not isinstance(runtime, AgentRuntime):
        raise TypeError('runtime must be an AgentRuntime')

    profile = runtime.profiles.register(
        create_report_followup_profile(model=model),
    )
    builder = build_report_followup_graph(
        gateway=runtime.llm_gateway,
        profile=profile,
    )
    return runtime.register_graph(
        REPORT_FOLLOWUP_CAPABILITY,
        builder,
        version=REPORT_FOLLOWUP_GRAPH_VERSION,
        checkpointed=False,
    )


def register_report_retrieve_context(
        runtime: AgentRuntime,
        *,
        model: str | None = None,
) -> GraphRegistration:
    """注册只选择已授权报告候选的无状态能力。"""
    if not isinstance(runtime, AgentRuntime):
        raise TypeError('runtime must be an AgentRuntime')

    profile = runtime.profiles.register(
        create_report_retrieve_context_profile(model=model),
    )
    builder = build_report_retrieve_context_graph(
        gateway=runtime.llm_gateway,
        profile=profile,
    )
    return runtime.register_graph(
        REPORT_RETRIEVE_CONTEXT_CAPABILITY,
        builder,
        version=REPORT_RETRIEVE_CONTEXT_GRAPH_VERSION,
        checkpointed=False,
    )
