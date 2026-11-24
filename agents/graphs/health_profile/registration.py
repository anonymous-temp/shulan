"""把用户健康画像能力安装到 AgentRuntime。"""

from __future__ import annotations

from ...runtime.container import AgentRuntime
from ...runtime.registry import GraphRegistration
from .chat_extraction import PROFILE_CHAT_EXTRACTION_CAPABILITY
from .chat_extraction import PROFILE_CHAT_EXTRACTION_GRAPH_VERSION
from .chat_extraction import build_profile_chat_extraction_graph
from .graph import PROFILE_EVIDENCE_CAPABILITY
from .graph import PROFILE_EVIDENCE_GRAPH_VERSION
from .graph import build_profile_evidence_graph
from .profile import create_profile_chat_extraction_profile
from .profile import create_profile_focus_profile
from .profile import create_profile_record_extraction_profile
from .profile import create_profile_synthesis_profile
from .record_extraction import PROFILE_RECORD_EXTRACTION_CAPABILITY
from .record_extraction import PROFILE_RECORD_EXTRACTION_GRAPH_VERSION
from .record_extraction import build_profile_record_extraction_graph


def register_profile_evidence(
    runtime: AgentRuntime,
    *,
    model: str | None = None,
) -> GraphRegistration:
    """注册无 Context、无 checkpoint 的专科循证解释流程。"""
    if not isinstance(runtime, AgentRuntime):
        raise TypeError('runtime must be an AgentRuntime')
    return runtime.register_graph(
        PROFILE_EVIDENCE_CAPABILITY,
        build_profile_evidence_graph(
            gateway=runtime.knowledge_gateway,
            llm_gateway=runtime.llm_gateway,
            planner_profile=runtime.profiles.register(create_profile_focus_profile(model=model)),
            synthesis_profile=runtime.profiles.register(create_profile_synthesis_profile(model=model)),
        ),
        version=PROFILE_EVIDENCE_GRAPH_VERSION,
        checkpointed=False,
    )


def register_profile_record_extraction(
    runtime: AgentRuntime,
    *,
    model: str | None = None,
) -> GraphRegistration:
    """注册无 Context、无 checkpoint 的建档候选提取 Graph。"""
    if not isinstance(runtime, AgentRuntime):
        raise TypeError('runtime must be an AgentRuntime')
    profile = runtime.profiles.register(
        create_profile_record_extraction_profile(model=model),
    )
    return runtime.register_graph(
        PROFILE_RECORD_EXTRACTION_CAPABILITY,
        build_profile_record_extraction_graph(
            gateway=runtime.llm_gateway,
            profile=profile,
        ),
        version=PROFILE_RECORD_EXTRACTION_GRAPH_VERSION,
        checkpointed=False,
    )


def register_profile_chat_extraction(
    runtime: AgentRuntime,
    *,
    model: str | None = None,
) -> GraphRegistration:
    """注册无 Context、无 checkpoint 的单条聊天候选提取 Graph。"""
    if not isinstance(runtime, AgentRuntime):
        raise TypeError('runtime must be an AgentRuntime')
    profile = runtime.profiles.register(
        create_profile_chat_extraction_profile(model=model),
    )
    return runtime.register_graph(
        PROFILE_CHAT_EXTRACTION_CAPABILITY,
        build_profile_chat_extraction_graph(
            gateway=runtime.llm_gateway,
            profile=profile,
        ),
        version=PROFILE_CHAT_EXTRACTION_GRAPH_VERSION,
        checkpointed=False,
    )
