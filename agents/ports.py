"""A domain receives one fixed capability, not access to the whole runtime."""

import hashlib
import json
from collections.abc import AsyncIterator
from collections.abc import Callable
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol
from typing import runtime_checkable

from .contracts import AgentEvent
from .contracts import AgentRequest
from .contracts import AgentResult
from .contracts import AgentSelection
from .hub import AgentHub
from .runtime.context import AgentRunContext
from .streaming import owned_stream


@runtime_checkable
class AgentPort(Protocol):
    async def invoke(
        self, request: AgentRequest, context: AgentRunContext, *, thread_id: str | None = None
    ) -> AgentResult: ...

    def stream(
        self, request: AgentRequest, context: AgentRunContext, *, thread_id: str | None = None
    ) -> AsyncIterator[AgentEvent]: ...


def scoped_thread(selection, domain, role, context, thread_id):
    if not isinstance(context, AgentRunContext):
        raise TypeError('context must be AgentRunContext')
    if thread_id is not None and (not isinstance(thread_id, str) or not thread_id or thread_id != thread_id.strip()):
        raise ValueError('thread_id must be an exact nonempty identifier')
    identity = [
        context.user_id,
        context.subject_id,
        context.session_id,
        domain,
        role,
        selection.capability,
        selection.contract_version,
        selection.implementation_revision,
        thread_id or context.run_id,
    ]
    return 'agent-v1:' + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def validate_binding(selection, domain, role):
    if not isinstance(selection, AgentSelection):
        raise TypeError('A port requires a complete selection')
    for value in (domain, role):
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError('domain and role must be exact nonempty identifiers')


@dataclass(frozen=True, slots=True)
class WorkflowAgentPort:
    """A whole domain workflow can implement the main Agent without a routing LLM.

    runner(request, context, *, thread_id) is an async domain-owned function
    returning its public result mapping. It retains its existing task, delivery,
    checkpoint and protected-rule logic. The port never retries it implicitly.
    """

    runner: Callable
    selection: AgentSelection
    domain: str
    role: str = 'main'
    streamer: Callable | None = None

    def __post_init__(self):
        validate_binding(self.selection, self.domain, self.role)
        if not callable(self.runner) or self.streamer is not None and not callable(self.streamer):
            raise TypeError('Workflow runner and optional streamer must be callable')

    async def invoke(self, request, context, *, thread_id=None):
        key = scoped_thread(self.selection, self.domain, self.role, context, thread_id)
        output = await self.runner(AgentRequest(deepcopy(dict(request.payload))), context, thread_id=key)
        if not isinstance(output, Mapping):
            raise TypeError('Workflow runner must return its public result mapping')
        return AgentResult(
            self.selection.capability, context.run_id, self.selection.implementation_revision, deepcopy(dict(output))
        )

    async def stream(self, request, context, *, thread_id=None):
        if self.streamer is None:
            raise NotImplementedError('This fixed workflow has no streaming operation')
        key = scoped_thread(self.selection, self.domain, self.role, context, thread_id)
        async with owned_stream(
            self.streamer(AgentRequest(deepcopy(dict(request.payload))), context, thread_id=key)
        ) as events:
            async for event in events:
                if not isinstance(event, AgentEvent) or event.run_id != context.run_id:
                    raise TypeError('Workflow stream must yield AgentEvent for the current run')
                yield event


@dataclass(frozen=True, slots=True)
class BoundAgentPort:
    hub: AgentHub
    selection: AgentSelection
    domain: str
    role: str = 'main'

    def __post_init__(self):
        if not isinstance(self.hub, AgentHub):
            raise TypeError('A bound port requires an AgentHub and a complete selection')
        validate_binding(self.selection, self.domain, self.role)

    def _thread(self, context, thread_id):
        return scoped_thread(self.selection, self.domain, self.role, context, thread_id)

    async def invoke(self, request, context, *, thread_id=None):
        return await self.hub.invoke(
            self.selection.capability,
            AgentRequest(deepcopy(dict(request.payload))),
            context,
            thread_id=self._thread(context, thread_id),
            selection=self.selection,
        )

    async def stream(self, request, context, *, thread_id=None):
        async with owned_stream(
            self.hub.stream(
                self.selection.capability,
                AgentRequest(deepcopy(dict(request.payload))),
                context,
                thread_id=self._thread(context, thread_id),
                selection=self.selection,
            )
        ) as events:
            async for event in events:
                yield event
