"""把普通聊天能力安装到 AgentRuntime。"""

from __future__ import annotations

from ...runtime.container import AgentRuntime
from ...knowledge.profile import ensure_knowledge_route_profile
from ...runtime.registry import GraphRegistration
from .graph import CHAT_REPLY_CAPABILITY
from .graph import CHAT_REPLY_GRAPH_VERSION
from .graph import build_chat_reply_graph
from .profile import create_chat_reply_profile
from .profile import create_chat_suggestions_profile
from .suggestions_graph import CHAT_SUGGESTIONS_CAPABILITY
from .suggestions_graph import CHAT_SUGGESTIONS_GRAPH_VERSION
from .suggestions_graph import build_chat_suggestions_graph


def register_chat_reply(
        runtime: AgentRuntime,
        *,
        model: str | None = None,
) -> GraphRegistration:
    """注册普通聊天的 Profile 和使用生产 checkpointer 的 Graph。"""
    if not isinstance(runtime, AgentRuntime):
        raise TypeError('runtime must be an AgentRuntime')

    profile = runtime.profiles.register(
        create_chat_reply_profile(model=model),
    )
    knowledge_gateway = None
    retrieval_profile = None
    if runtime.knowledge_gateway.enabled:
        knowledge_gateway = runtime.knowledge_gateway
        retrieval_profile = ensure_knowledge_route_profile(
            runtime.profiles,
            model=model,
        )
    builder = build_chat_reply_graph(
        gateway=runtime.llm_gateway,
        profile=profile,
        knowledge_gateway=knowledge_gateway,
        retrieval_profile=retrieval_profile,
    )
    return runtime.register_graph(
        CHAT_REPLY_CAPABILITY,
        builder,
        version=CHAT_REPLY_GRAPH_VERSION,
    )


def register_chat_suggestions(
        runtime: AgentRuntime,
        *,
        model: str | None = None,
) -> GraphRegistration:
    """注册无状态的个性化推荐问题能力。"""
    if not isinstance(runtime, AgentRuntime):
        raise TypeError('runtime must be an AgentRuntime')

    profile = runtime.profiles.register(
        create_chat_suggestions_profile(model=model),
    )
    builder = build_chat_suggestions_graph(
        gateway=runtime.llm_gateway,
        profile=profile,
    )
    return runtime.register_graph(
        CHAT_SUGGESTIONS_CAPABILITY,
        builder,
        version=CHAT_SUGGESTIONS_GRAPH_VERSION,
        checkpointed=False,
    )
