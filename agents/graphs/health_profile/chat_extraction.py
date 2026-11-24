"""从单条普通聊天用户消息提取候选健康事实的无状态 LangGraph。"""

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
from .profile import PROFILE_CHAT_EXTRACTION_PROFILE

PROFILE_CHAT_EXTRACTION_CAPABILITY = PROFILE_CHAT_EXTRACTION_PROFILE
PROFILE_CHAT_EXTRACTION_GRAPH_VERSION = 'profile-chat-extraction-v3'


class ProfileChatExtractionState(TypedDict, total=False):
    """单条用户消息只存在于本次无状态调用。"""

    source_content: str
    request_id: str
    run_id: str
    content: str
    usage: dict[str, Any]
    provider: str | None
    model: str | None
    status: str


def build_profile_chat_extraction_graph(
        *,
        gateway: LLMGateway,
        profile: ModelProfile,
) -> StateGraph:
    if not isinstance(gateway, LLMGateway):
        raise TypeError('gateway must implement LLMGateway')
    if not isinstance(profile, ModelProfile):
        raise TypeError('profile must be a ModelProfile')
    if profile.name != PROFILE_CHAT_EXTRACTION_PROFILE:
        raise AgentConfigurationError(
            'chat candidate graph requires profile '
            f'{PROFILE_CHAT_EXTRACTION_PROFILE!r}',
        )
    if profile.context_policy is not ContextPolicy.STATELESS:
        raise AgentConfigurationError(
            'chat candidate graph requires STATELESS context policy',
        )

    async def call_model(
            state: ProfileChatExtractionState,
            runtime: Runtime[AgentRunContext],
    ) -> ProfileChatExtractionState:
        source_content = state.get('source_content')
        if not isinstance(source_content, str) or not source_content.strip():
            raise ValueError(
                'chat candidate graph requires non-empty source_content',
            )
        result = await gateway.complete(LLMRequest(
            profile=profile,
            content=source_content,
        ))
        context = runtime.context
        return {
            'request_id': context.request_id,
            'run_id': context.run_id,
            'content': result.content,
            # reasoning_content 不进入 Graph 输出、任务或画像事实。
            'usage': {} if result.usage is None else result.usage.to_dict(),
            'provider': result.provider,
            'model': result.model,
            'status': 'completed',
        }

    builder = StateGraph(
        ProfileChatExtractionState,
        context_schema=AgentRunContext,
    )
    builder.add_node('extract_candidates', call_model)
    builder.add_edge(START, 'extract_candidates')
    builder.add_edge('extract_candidates', END)
    return builder
