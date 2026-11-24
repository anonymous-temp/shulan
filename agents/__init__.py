"""Shulan AI Agent public contracts, with runtime construction dependencies loaded on demand."""

from importlib import import_module

from .contracts import AgentEvent
from .contracts import AgentEventType
from .contracts import AgentRequest
from .contracts import AgentResult
from .contracts import AgentSelection
from .hub import AgentHub
from .ports import AgentPort
from .ports import BoundAgentPort
from .ports import WorkflowAgentPort
from .runtime.context import AgentRunContext
from .runtime.registry import GraphRegistry

__all__ = [
    'AgentEvent',
    'AgentEventType',
    'AgentHub',
    'AgentRuntime',
    'AgentRuntimeSettings',
    'AgentRequest',
    'AgentResult',
    'AgentSelection',
    'AgentPort',
    'BoundAgentPort',
    'WorkflowAgentPort',
    'AgentRunContext',
    'GraphRegistry',
    'create_agent_runtime',
]

_RUNTIME_EXPORTS = {
    'AgentRuntime': '.runtime.container',
    'AgentRuntimeSettings': '.runtime.lifecycle',
    'create_agent_runtime': '.runtime.lifecycle',
}


def __getattr__(name):
    if name not in _RUNTIME_EXPORTS:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    value = getattr(import_module(_RUNTIME_EXPORTS[name], __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
