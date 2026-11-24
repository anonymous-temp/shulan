"""业务代码调用已注册 LangGraph 的统一入口。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from collections.abc import Mapping
from typing import Any

from .contracts import AgentEvent
from .contracts import AgentEventType
from .contracts import AgentRequest
from .contracts import AgentResult
from .contracts import AgentSelection
from .errors import AgentProtocolError
from .runtime.context import AgentRunContext
from .runtime.registry import GraphRegistry
from .streaming import owned_stream


class AgentHub:
    """统一执行已注册 Graph，并隔离业务层与 LangGraph 调用细节。"""

    def __init__(self, registry: GraphRegistry):
        if not isinstance(registry, GraphRegistry):
            raise TypeError('registry must be a GraphRegistry')
        self._registry = registry

    async def invoke(
        self,
        capability: str,
        request: AgentRequest,
        context: AgentRunContext,
        *,
        thread_id: str | None = None,
        selection: AgentSelection | None = None,
    ) -> AgentResult:
        """执行完整调用并返回统一结果。"""
        registration = self._registry.get(capability, selection=selection)
        output = await registration.graph.ainvoke(
            request.to_graph_input(),
            config=self._graph_config(thread_id or context.run_id),
            context=context,
        )
        if not isinstance(output, Mapping):
            raise AgentProtocolError(
                f'Graph {capability!r} returned a non-mapping result',
            )
        return AgentResult(
            capability=registration.capability,
            run_id=context.run_id,
            graph_version=registration.version,
            data=dict(output),
        )

    async def stream(
        self,
        capability: str,
        request: AgentRequest,
        context: AgentRunContext,
        *,
        thread_id: str | None = None,
        selection: AgentSelection | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """执行流式调用并标准化 LangGraph custom 事件。"""
        registration = self._registry.get(capability, selection=selection)
        async with owned_stream(
            registration.graph.astream(
                request.to_graph_input(),
                config=self._graph_config(thread_id or context.run_id),
                context=context,
                stream_mode='custom',
                version='v2',
            )
        ) as events:
            async for part in events:
                yield self._normalize_event(part, default_run_id=context.run_id)

    @staticmethod
    def _graph_config(thread_id: str) -> dict[str, dict[str, str]]:
        if not isinstance(thread_id, str):
            raise TypeError('thread_id must be a string')
        if not thread_id.strip():
            raise ValueError('thread_id cannot be empty')
        return {'configurable': {'thread_id': thread_id}}

    @staticmethod
    def _normalize_event(part: Any, *, default_run_id: str) -> AgentEvent:
        if isinstance(part, AgentEvent):
            return part
        if not isinstance(part, Mapping):
            raise AgentProtocolError('Graph stream event must be a mapping')

        payload: Any = part
        if part.get('type') == 'custom' and 'data' in part:
            payload = part['data']
        if isinstance(payload, AgentEvent):
            return payload
        if not isinstance(payload, Mapping):
            raise AgentProtocolError('Graph custom event data must be a mapping')

        event_type = payload.get('type')
        try:
            normalized_type = AgentEventType(event_type)
        except (TypeError, ValueError) as error:
            raise AgentProtocolError(
                f'Unsupported Agent event type: {event_type!r}',
            ) from error

        run_id = payload.get('runId', payload.get('run_id', default_run_id))
        data = payload.get('data', {})
        if not isinstance(data, Mapping):
            raise AgentProtocolError('Agent event data must be a mapping')
        return AgentEvent(
            type=normalized_type,
            run_id=run_id,
            data=dict(data),
        )
