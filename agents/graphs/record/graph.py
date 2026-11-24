"""健康建档中的无状态模型调用 Graph。"""

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
from .profile import RECORD_PLAN_PROFILE
from .profile import RECORD_QUESTION_PROFILE

RECORD_PLAN_CAPABILITY = 'record.plan'
RECORD_QUESTION_CAPABILITY = 'record.question'
RECORD_PLAN_GRAPH_VERSION = 'record-patient-v11'
RECORD_QUESTION_GRAPH_VERSION = 'record-response-v19'

logger = getLogger(__name__)


class RecordModelState(TypedDict, total=False):
    """一次建档模型任务的临时状态，不写入持久化 checkpoint。"""

    prompt: str
    retrieval_query: str
    thinking: bool
    reasoning_effort: str
    stream: bool
    request_id: str
    run_id: str
    content: str
    reasoning_content: str
    usage: dict[str, Any]
    provider: str | None
    model: str | None
    retrieval_queries: list[dict[str, str]]
    retrieval_status: str
    knowledge_context: str | None
    retain_knowledge_context: bool
    reuse_knowledge: bool
    status: str


def build_record_model_graph(
    *,
    gateway: LLMGateway,
    profile: ModelProfile,
    knowledge_gateway: KnowledgeGateway | None = None,
    retrieval_profile: ModelProfile | None = None,
) -> StateGraph:
    """为患者分析或回应 Profile 构建统一的完整/流式模型 Graph。"""
    if not isinstance(gateway, LLMGateway):
        raise TypeError('gateway must implement LLMGateway')
    if not isinstance(profile, ModelProfile):
        raise TypeError('profile must be a ModelProfile')
    if profile.name not in (RECORD_PLAN_PROFILE, RECORD_QUESTION_PROFILE):
        raise AgentConfigurationError('unsupported record model profile')
    if profile.context_policy is not ContextPolicy.STATELESS:
        raise AgentConfigurationError(
            'record model graph requires STATELESS context policy',
        )
    retrieval_enabled = (
        profile.name == RECORD_QUESTION_PROFILE and knowledge_gateway is not None and retrieval_profile is not None
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

    async def start_run(state: RecordModelState) -> RecordModelState:
        get_stream_writer()(
            {
                'type': 'run_started',
                'data': {'profile': profile.name},
            }
        )
        reuse = state.get('reuse_knowledge') is True
        return {
            'knowledge_context': state.get('knowledge_context') if reuse else None,
            'retrieval_queries': [],
            'retrieval_status': 'reused' if reuse else 'not_started',
            'status': 'routing' if retrieval_enabled else 'running',
        }

    async def route_knowledge(state: RecordModelState) -> RecordModelState:
        assert retrieval_profile is not None
        # A bounded presentation repair reuses this turn's retrieval outcome,
        # including an empty/unavailable result, instead of routing again.
        if state.get('reuse_knowledge') is True:
            return {'retrieval_queries': [], 'retrieval_status': 'reused'}
        # An explicit empty query means the application already determined that
        # this turn only collects patient information. Missing queries retain
        # the direct graph caller's existing prompt-based routing behavior.
        query_content = state.get('retrieval_query', state.get('prompt'))
        if not isinstance(query_content, str) or not query_content.strip():
            return {
                'retrieval_queries': [],
                'retrieval_status': 'skipped',
            }
        try:
            plan = await plan_knowledge_retrieval(
                gateway=gateway,
                profile=retrieval_profile,
                scenario='record_question',
                content=query_content,
            )
        except KnowledgeRetrievalError as error:
            logger.warning(
                'Record knowledge routing failed; continuing without retrieval: %s',
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

    async def search_knowledge(state: RecordModelState) -> RecordModelState:
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
                'Record knowledge retrieval failed; continuing without evidence: %s',
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
        state: RecordModelState,
        runtime: Runtime[AgentRunContext],
    ) -> RecordModelState:
        prompt = state.get('prompt')
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError('record model graph requires a non-empty prompt')

        request = LLMRequest(
            profile=profile,
            content=prompt,
            supplemental_context=state.get('knowledge_context'),
            thinking=state.get('thinking'),
            reasoning_effort=state.get('reasoning_effort'),
        )
        writer = get_stream_writer()

        expose_reasoning = profile.name == RECORD_PLAN_PROFILE and state.get('thinking', profile.thinking)

        if state.get('stream', False):
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            usage: dict[str, Any] = {}
            async for chunk in gateway.stream(request):
                if chunk.reasoning_content and expose_reasoning:
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
            content = ''.join(content_parts)
            reasoning_content = ''.join(reasoning_parts)
            provider = None
            model = None
        else:
            result = await gateway.complete(request)
            content = result.content
            reasoning_content = (result.reasoning_content or '') if expose_reasoning else ''
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
            'content': content,
            'reasoning_content': reasoning_content,
            'usage': usage,
            'provider': provider,
            'model': model,
            # Internal application callers can retain evidence for one repair.
            # The default graph result continues to discard retrieval content.
            'knowledge_context': state.get('knowledge_context')
            if state.get('retain_knowledge_context') is True
            else None,
            'retrieval_queries': [],
            'retrieval_query': '',
            'status': 'completed',
        }

    builder = StateGraph(
        RecordModelState,
        context_schema=AgentRunContext,
    )
    builder.add_node('start_run', start_run)
    builder.add_node('call_model', call_model)
    builder.add_edge(START, 'start_run')
    if retrieval_enabled:
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


def _knowledge_route(state: RecordModelState) -> str:
    return 'search' if state.get('retrieval_queries') else 'answer'


def _state_retrieval_plan(
    state: RecordModelState,
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
