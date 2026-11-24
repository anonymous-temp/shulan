"""普通聊天个性化推荐问题的无状态 LangGraph。"""

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
from .profile import CHAT_SUGGESTIONS_PROFILE

CHAT_SUGGESTIONS_CAPABILITY = 'chat.suggestions'
CHAT_SUGGESTIONS_GRAPH_VERSION = 'chat-suggestions-v1'


class ChatSuggestionsState(TypedDict, total=False):
    """一次个性化推荐问题生成任务的临时状态。"""

    prompt: str
    request_id: str
    run_id: str
    content: str
    usage: dict[str, Any]
    provider: str | None
    model: str | None
    status: str


def build_chat_suggestions_graph(
        *,
        gateway: LLMGateway,
        profile: ModelProfile,
) -> StateGraph:
    """构建不保存 checkpoint 或模型会话上下文的推荐问题 Graph。"""
    if not isinstance(gateway, LLMGateway):
        raise TypeError('gateway must implement LLMGateway')
    if not isinstance(profile, ModelProfile):
        raise TypeError('profile must be a ModelProfile')
    if profile.name != CHAT_SUGGESTIONS_PROFILE:
        raise AgentConfigurationError(
            f'chat suggestions graph requires profile '
            f'{CHAT_SUGGESTIONS_PROFILE!r}',
        )
    if profile.context_policy is not ContextPolicy.STATELESS:
        raise AgentConfigurationError(
            'chat suggestions graph requires STATELESS context policy',
        )

    async def call_model(
            state: ChatSuggestionsState,
            runtime: Runtime[AgentRunContext],
    ) -> ChatSuggestionsState:
        prompt = state.get('prompt')
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(
                'chat suggestions graph requires a non-empty prompt',
            )

        result = await gateway.complete(LLMRequest(
            profile=profile,
            content=prompt,
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
        ChatSuggestionsState,
        context_schema=AgentRunContext,
    )
    builder.add_node('call_model', call_model)
    builder.add_edge(START, 'call_model')
    builder.add_edge('call_model', END)
    return builder

