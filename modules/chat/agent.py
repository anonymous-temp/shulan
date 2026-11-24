"""Chat's public main Agent and fixed compatibility manifest.

The main Agent owns the existing workflow, not a second model router. The
legacy reply graph keeps its original Context/checkpoint identity.
"""

from collections.abc import Callable
from contextlib import aclosing
from copy import deepcopy
from dataclasses import asdict
from dataclasses import dataclass

from agents import AgentSelection
from agents import WorkflowAgentPort
from agents.errors import AgentConfigurationError

CHAT_MAIN_SELECTION = AgentSelection('chat.main', 'chat-main-v1')
# Explicit migration bindings: never resolve these from a deployment's alias.
LEGACY_CHAT_MAIN_SELECTION = AgentSelection('chat.main', 'chat-main-v1')
CHAT_REPLY_SELECTION = AgentSelection('chat.reply', 'chat-reply-v5')
CHAT_SUGGESTIONS_SELECTION = AgentSelection('chat.suggestions', 'chat-suggestions-v1')
LEGACY_CHAT_AGENT_MANIFEST = {
    'contractVersion': 'chat-agent-manifest.v1',
    'main': asdict(LEGACY_CHAT_MAIN_SELECTION),
    'reply': asdict(AgentSelection('chat.reply', 'chat-reply-v5')),
    'suggestions': asdict(AgentSelection('chat.suggestions', 'chat-suggestions-v1')),
}


def chat_agent_manifest() -> dict:
    return {
        'contractVersion': 'chat-agent-manifest.v1',
        'main': asdict(CHAT_MAIN_SELECTION),
        'reply': asdict(CHAT_REPLY_SELECTION),
        'suggestions': asdict(CHAT_SUGGESTIONS_SELECTION),
    }


def chat_execution(context, *, thread_id, operation):
    """Task-local execution identity; never included in model/SSE messages."""
    return {
        'runId': context.run_id,
        'requestId': context.request_id,
        'mainThreadId': thread_id,
        'operation': operation,
    }


def bind_chat_session(session: dict, execution: dict) -> None:
    """Old sessions explicitly map to v5/v1; unsupported manifests fail closed."""
    expected = chat_agent_manifest()
    stored = session.get('chatAgentManifest')
    if stored is None:
        stored = deepcopy(LEGACY_CHAT_AGENT_MANIFEST)
    if stored != expected:
        raise AgentConfigurationError('Chat session requires an unavailable Agent manifest')
    session['chatAgentManifest'] = expected
    session['chatAgentExecution'] = dict(execution)


@dataclass(frozen=True, slots=True)
class ChatAgent:
    """One domain main Agent with fixed reply/suggestions operations."""

    runner: Callable
    streamer: Callable
    selection: AgentSelection = CHAT_MAIN_SELECTION

    def __post_init__(self):
        if self.selection != CHAT_MAIN_SELECTION:
            raise AgentConfigurationError('Unsupported Chat main Agent selection')

    async def invoke(self, request, context, *, thread_id=None):
        return await WorkflowAgentPort(self.runner, self.selection, 'chat').invoke(
            request, context, thread_id=thread_id
        )

    async def stream(self, request, context, *, thread_id=None):
        port = WorkflowAgentPort(self.runner, self.selection, 'chat', streamer=self.streamer)
        async with aclosing(port.stream(request, context, thread_id=thread_id)) as events:
            async for event in events:
                yield event


def install_chat_agent(runtime, manager) -> bool:
    """Composition-root hook; failed Chat dependencies do not block queries."""

    def configure(staged):
        # Check the committed dependency registry, not the new staging area.
        for selection in (CHAT_REPLY_SELECTION, CHAT_SUGGESTIONS_SELECTION):
            runtime.registry.get(selection.capability, selection=selection)
        staged.ports.register(CHAT_MAIN_SELECTION, manager.agent_port)

    installed = runtime.install_module('chat.main', configure)
    manager.set_agent_readiness(installed)
    return installed
