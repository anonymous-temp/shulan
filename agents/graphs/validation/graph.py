"""用于验证 AgentHub 与 LLMGateway 协议的隔离 LangGraph。"""

from __future__ import annotations

from typing import TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END
from langgraph.graph import START
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime

from ...llm.profiles import ModelProfile
from ...llm.protocol import LLMGateway
from ...llm.types import LLMRequest
from ...llm.types import LLMUsage
from ...runtime.context import AgentRunContext


class ValidationState(TypedDict, total=False):
    """隔离验证图的可序列化状态。"""

    message: str
    stream: bool
    conversation_key: str
    thinking: bool
    web_search: bool
    search_query: str
    response: str
    reasoning: str
    usage: dict
    search_sources: list[dict]
    search_trace: dict | None


def build_validation_graph(
    *,
    gateway: LLMGateway,
    profile: ModelProfile,
):
    """构建一张可同时验证完整和流式 Gateway 的单节点图。"""
    if not isinstance(gateway, LLMGateway):
        raise TypeError('gateway must implement LLMGateway')
    if not isinstance(profile, ModelProfile):
        raise TypeError('profile must be a ModelProfile')

    async def call_model(
        state: ValidationState,
        runtime: Runtime[AgentRunContext],
    ) -> ValidationState:
        message = state.get('message')
        if not isinstance(message, str) or not message.strip():
            raise ValueError('validation graph requires a non-empty message')

        request = LLMRequest(
            profile=profile,
            content=message,
            conversation_key=state.get('conversation_key'),
            thinking=state.get('thinking'),
            web_search=state.get('web_search'),
            search_query=state.get('search_query'),
        )
        writer = get_stream_writer()
        writer(
            {
                'type': 'run_started',
                'data': {'profile': profile.name},
            }
        )

        search_sources = []
        search_trace = None
        if state.get('stream', False):
            response_parts: list[str] = []
            reasoning_parts: list[str] = []
            usage: LLMUsage | None = None
            async for chunk in gateway.stream(request):
                if chunk.reasoning_content:
                    reasoning_parts.append(chunk.reasoning_content)
                    writer(
                        {
                            'type': 'reasoning_delta',
                            'data': {'content': chunk.reasoning_content},
                        }
                    )
                if chunk.content:
                    response_parts.append(chunk.content)
                    writer(
                        {
                            'type': 'content_delta',
                            'data': {'content': chunk.content},
                        }
                    )
                if chunk.usage is not None:
                    usage = chunk.usage
                if chunk.search_trace is not None:
                    search_trace = dict(chunk.search_trace)
                if chunk.search_sources:
                    search_sources = [dict(source) for source in chunk.search_sources]
            response = ''.join(response_parts)
            reasoning = ''.join(reasoning_parts)
        else:
            result = await gateway.complete(request)
            response = result.content
            reasoning = result.reasoning_content or ''
            usage = result.usage
            search_sources = [dict(source) for source in result.search_sources]
            search_trace = None if result.search_trace is None else dict(result.search_trace)
            if reasoning:
                writer(
                    {
                        'type': 'reasoning_delta',
                        'data': {'content': reasoning},
                    }
                )
            if response:
                writer(
                    {
                        'type': 'content_delta',
                        'data': {'content': response},
                    }
                )

        writer(
            {
                'type': 'run_completed',
                'data': {'profile': profile.name},
            }
        )
        return {
            'response': response,
            'reasoning': reasoning,
            'usage': {} if usage is None else usage.to_dict(),
            'search_sources': search_sources,
            'search_trace': search_trace,
        }

    builder = StateGraph(
        ValidationState,
        context_schema=AgentRunContext,
    )
    builder.add_node('call_model', call_model)
    builder.add_edge(START, 'call_model')
    builder.add_edge('call_model', END)
    return builder.compile()
