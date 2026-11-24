"""知识检索路由使用的无状态模型配置。"""

from __future__ import annotations

from ..llm.profiles import ModelProfile
from ..llm.profiles import ModelProfileRegistry
from ..llm.types import ContextPolicy

KNOWLEDGE_ROUTE_PROFILE = 'knowledge.route'


def create_knowledge_route_profile(
    *,
    model: str | None = None,
) -> ModelProfile:
    return ModelProfile(
        name=KNOWLEDGE_ROUTE_PROFILE,
        context_policy=ContextPolicy.STATELESS,
        prompt_name='knowledge/retrieval_route.txt',
        model=model,
        thinking=False,
        model_kwargs={'response_format': {'type': 'json_object'}},
    )


def ensure_knowledge_route_profile(
    registry: ModelProfileRegistry,
    *,
    model: str | None = None,
) -> ModelProfile:
    """为多个业务 Graph 共享唯一的检索路由 Profile。"""
    if not isinstance(registry, ModelProfileRegistry):
        raise TypeError('registry must be a ModelProfileRegistry')
    if KNOWLEDGE_ROUTE_PROFILE in registry.names():
        return registry.get(KNOWLEDGE_ROUTE_PROFILE)
    return registry.register(create_knowledge_route_profile(model=model))
