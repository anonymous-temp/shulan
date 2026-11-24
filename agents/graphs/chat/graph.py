"""专业健康对话的统一完整与流式 LangGraph。"""

from __future__ import annotations

from typing import Any
from typing import TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END
from langgraph.graph import START
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime

from utils.log import getLogger

from ...errors import AgentConfigurationError
from ...knowledge.errors import KnowledgeRetrievalError
from ...knowledge.profile import KNOWLEDGE_ROUTE_PROFILE
from ...knowledge.protocol import KnowledgeGateway
from ...knowledge.routing import format_knowledge_context
from ...knowledge.routing import plan_knowledge_retrieval
from ...knowledge.routing import retrieve_knowledge
from ...knowledge.types import KnowledgeQuery
from ...knowledge.types import KnowledgeRetrievalPlan
from ...knowledge.types import KnowledgeSource
from ...llm.profiles import ModelProfile
from ...llm.protocol import LLMGateway
from ...llm.types import ContextPolicy
from ...llm.types import LLMRequest
from ...runtime.context import AgentRunContext
from .profile import CHAT_REPLY_PROFILE

CHAT_REPLY_CAPABILITY = 'chat.reply'
CHAT_REPLY_GRAPH_VERSION = 'chat-reply-v5'

logger = getLogger(__name__)


class ChatReplyState(TypedDict, total=False):
    """普通聊天图中可 checkpoint 的最小运行状态。

    完整对话历史由 AION Context Repository 管理，用户可见消息由业务
    Session Repository 管理；这里不复制任何一份持续增长的历史。
    """

    agent_thread_id: str
    user_message: str
    retrieval_query: str
    thinking: bool
    reasoning_effort: str
    stream: bool
    request_id: str
    run_id: str
    assistant_content: str
    reasoning_content: str
    usage: dict[str, Any]
    provider: str | None
    model: str | None
    retrieval_queries: list[dict[str, str]]
    retrieval_status: str
    knowledge_context: str | None
    status: str


def build_chat_reply_graph(
    *,
    gateway: LLMGateway,
    profile: ModelProfile,
    knowledge_gateway: KnowledgeGateway | None = None,
    retrieval_profile: ModelProfile | None = None,
) -> StateGraph:
    """构建普通聊天 Graph builder，由 AgentRuntime 注入 checkpointer。"""
    if not isinstance(gateway, LLMGateway):
        raise TypeError('gateway must implement LLMGateway')
    if not isinstance(profile, ModelProfile):
        raise TypeError('profile must be a ModelProfile')
    if profile.name != CHAT_REPLY_PROFILE:
        raise AgentConfigurationError(
            f'chat reply graph requires profile {CHAT_REPLY_PROFILE!r}',
        )
    if profile.context_policy is not ContextPolicy.AION_FULL:
        raise AgentConfigurationError(
            'chat reply graph requires AION_FULL context policy',
        )
    if (knowledge_gateway is None) != (retrieval_profile is None):
        raise AgentConfigurationError(
            'knowledge_gateway and retrieval_profile must be configured together',
        )
    if knowledge_gateway is not None:
        if not isinstance(knowledge_gateway, KnowledgeGateway):
            raise TypeError('knowledge_gateway must implement KnowledgeGateway')
        if not isinstance(retrieval_profile, ModelProfile):
            raise TypeError('retrieval_profile must be a ModelProfile')
        if retrieval_profile.name != KNOWLEDGE_ROUTE_PROFILE:
            raise AgentConfigurationError(
                f'knowledge retrieval requires profile {KNOWLEDGE_ROUTE_PROFILE!r}',
            )
        if retrieval_profile.context_policy is not ContextPolicy.STATELESS:
            raise AgentConfigurationError(
                'knowledge retrieval profile must be stateless',
            )

    async def start_run(state: ChatReplyState) -> ChatReplyState:
        get_stream_writer()(
            {
                'type': 'run_started',
                'data': {'profile': profile.name},
            }
        )
        return {
            'knowledge_context': None,
            'retrieval_queries': [],
            'retrieval_status': 'not_started',
            'status': 'routing',
        }

    async def route_knowledge(state: ChatReplyState) -> ChatReplyState:
        assert retrieval_profile is not None
        query_content = state.get('retrieval_query') or state.get('user_message')
        if not isinstance(query_content, str) or not query_content.strip():
            return {
                'retrieval_queries': [],
                'retrieval_status': 'skipped',
            }
        try:
            plan = await plan_knowledge_retrieval(
                gateway=gateway,
                profile=retrieval_profile,
                scenario='chat',
                content=query_content,
            )
        except KnowledgeRetrievalError as error:
            logger.warning(
                'Chat knowledge routing failed; continuing without retrieval: %s',
                type(error).__name__,
            )
            return {
                'retrieval_queries': [],
                'retrieval_status': 'routing_failed',
            }
        return {
            'retrieval_queries': [{'source': query.source.value, 'query': query.query} for query in plan.queries],
            'retrieval_status': 'planned' if plan.needed else 'skipped',
        }

    async def search_knowledge(state: ChatReplyState) -> ChatReplyState:
        assert knowledge_gateway is not None
        plan = _state_retrieval_plan(state)
        writer = get_stream_writer()
        writer(
            {
                'type': 'tool_started',
                'data': {
                    'tool': 'knowledge.retrieve',
                    'queryCount': len(plan.queries),
                },
            }
        )
        try:
            documents = await retrieve_knowledge(knowledge_gateway, plan)
        except Exception as error:
            logger.warning(
                'Chat knowledge retrieval failed; continuing without evidence: %s',
                type(error).__name__,
            )
            writer(
                {
                    'type': 'tool_finished',
                    'data': {
                        'tool': 'knowledge.retrieve',
                        'resultCount': 0,
                        'status': 'unavailable',
                    },
                }
            )
            return {
                'knowledge_context': None,
                'retrieval_status': 'unavailable',
            }
        knowledge_context = format_knowledge_context(documents)
        writer(
            {
                'type': 'tool_finished',
                'data': {
                    'tool': 'knowledge.retrieve',
                    'resultCount': len(documents),
                    'status': 'retrieved' if documents else 'empty',
                },
            }
        )
        return {
            'knowledge_context': knowledge_context,
            'retrieval_status': 'retrieved' if documents else 'empty',
        }

    async def call_model(
        state: ChatReplyState,
        runtime: Runtime[AgentRunContext],
    ) -> ChatReplyState:
        user_message = _require_state_text(
            state,
            'user_message',
            'chat reply graph requires a non-empty user_message',
        )
        agent_thread_id = _require_state_text(
            state,
            'agent_thread_id',
            'chat reply graph requires a non-empty agent_thread_id',
        )

        request = LLMRequest(
            profile=profile,
            content=user_message,
            supplemental_context=_combine_supplemental_context(
                session_health_context=runtime.context.session_health_context,
                health_profile_context=(runtime.context.health_profile_context),
                report_context=runtime.context.report_context,
                knowledge_context=state.get('knowledge_context'),
            ),
            conversation_key=agent_thread_id,
            thinking=state.get('thinking'),
            reasoning_effort=state.get('reasoning_effort'),
        )
        writer = get_stream_writer()
        if state.get('stream', False):
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            usage: dict[str, Any] = {}
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
                    content_parts.append(chunk.content)
                    writer(
                        {
                            'type': 'content_delta',
                            'data': {'content': chunk.content},
                        }
                    )
                if chunk.usage is not None:
                    usage = chunk.usage.to_dict()
            assistant_content = ''.join(content_parts)
            reasoning_content = ''.join(reasoning_parts)
            provider = None
            model = None
        else:
            result = await gateway.complete(request)
            assistant_content = result.content
            reasoning_content = result.reasoning_content or ''
            usage = {} if result.usage is None else result.usage.to_dict()
            provider = result.provider
            model = result.model

        writer(
            {
                'type': 'run_completed',
                'data': {'profile': profile.name},
            }
        )
        context = runtime.context
        return {
            'request_id': context.request_id,
            'run_id': context.run_id,
            'assistant_content': assistant_content,
            'reasoning_content': reasoning_content,
            'usage': usage,
            'provider': provider,
            'model': model,
            'knowledge_context': None,
            'retrieval_queries': [],
            'retrieval_query': '',
            'status': 'completed',
        }

    builder = StateGraph(
        ChatReplyState,
        context_schema=AgentRunContext,
    )
    builder.add_node('start_run', start_run)
    builder.add_node('call_model', call_model)
    builder.add_edge(START, 'start_run')
    if knowledge_gateway is not None:
        builder.add_node('route_knowledge', route_knowledge)
        builder.add_node('search_knowledge', search_knowledge)
        builder.add_edge('start_run', 'route_knowledge')
        builder.add_conditional_edges(
            'route_knowledge',
            _knowledge_route,
            {
                'search': 'search_knowledge',
                'answer': 'call_model',
            },
        )
        builder.add_edge('search_knowledge', 'call_model')
    else:
        builder.add_edge('start_run', 'call_model')
    builder.add_edge('call_model', END)
    return builder


def _require_state_text(
    state: ChatReplyState,
    key: str,
    error_message: str,
) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(error_message)
    return value


def _knowledge_route(state: ChatReplyState) -> str:
    return 'search' if state.get('retrieval_queries') else 'answer'


def _combine_supplemental_context(
    *,
    session_health_context: str | None,
    health_profile_context: str | None,
    report_context: str | None = None,
    knowledge_context: str | None,
) -> str | None:
    """四类资料分别标注，互不混为权威事实。

    本次建档资料、画像事实、AI 报告分析与外部知识证据分别归因；补充资料
    只影响本轮请求，不进入 AION 模型历史与 Chat checkpoint。
    """
    sections = []
    if session_health_context is not None:
        sections.append(
            '【本次建档健康资料】\n' + session_health_context.strip(),
        )
    if health_profile_context is not None:
        sections.append(
            '【用户健康画像记忆】\n' + health_profile_context.strip(),
        )
    if report_context is not None:
        sections.append('【本轮报告内容】\n' + report_context.strip())
    if knowledge_context is not None:
        sections.append('【本轮循证检索资料】\n' + knowledge_context.strip())
    return '\n\n'.join(sections) if sections else None


def _state_retrieval_plan(
    state: ChatReplyState,
) -> KnowledgeRetrievalPlan:
    queries = []
    for item in state.get('retrieval_queries', []):
        try:
            queries.append(
                KnowledgeQuery(
                    source=KnowledgeSource(item['source']),
                    query=item['query'],
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return KnowledgeRetrievalPlan(tuple(queries))
