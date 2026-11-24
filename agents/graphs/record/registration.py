"""把健康建档能力安装到 AgentRuntime。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...knowledge.profile import ensure_knowledge_route_profile
from ...runtime.container import AgentRuntime
from .graph import RECORD_PLAN_CAPABILITY
from .graph import RECORD_PLAN_GRAPH_VERSION
from .graph import RECORD_QUESTION_CAPABILITY
from .graph import RECORD_QUESTION_GRAPH_VERSION
from .graph import build_record_model_graph
from .profile import build_record_question_prompt_variables
from .profile import create_record_plan_profile
from .profile import create_record_question_profile


def register_record_graphs(
    runtime: AgentRuntime,
    *,
    question_topics: Mapping[str, Any],
    model: str | None = None,
) -> None:
    """注册患者情况分析与回应生成两项无状态能力。"""
    if not isinstance(runtime, AgentRuntime):
        raise TypeError('runtime must be an AgentRuntime')

    plan_profile = runtime.profiles.register(
        create_record_plan_profile(model=model),
    )
    question_profile = runtime.profiles.register(
        create_record_question_profile(
            prompt_variables=build_record_question_prompt_variables(
                question_topics,
            ),
            model=model,
        ),
    )
    knowledge_gateway = None
    retrieval_profile = None
    if runtime.knowledge_gateway.enabled:
        knowledge_gateway = runtime.knowledge_gateway
        retrieval_profile = ensure_knowledge_route_profile(
            runtime.profiles,
            model=model,
        )
    runtime.register_graph(
        RECORD_PLAN_CAPABILITY,
        build_record_model_graph(
            gateway=runtime.llm_gateway,
            profile=plan_profile,
        ),
        version=RECORD_PLAN_GRAPH_VERSION,
        checkpointed=False,
    )
    runtime.register_graph(
        RECORD_QUESTION_CAPABILITY,
        build_record_model_graph(
            gateway=runtime.llm_gateway,
            profile=question_profile,
            knowledge_gateway=knowledge_gateway,
            retrieval_profile=retrieval_profile,
        ),
        version=RECORD_QUESTION_GRAPH_VERSION,
        checkpointed=False,
    )
