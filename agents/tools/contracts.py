"""Provider-neutral contracts for bounded, server-authorized tool runs."""

import math
import re
from collections.abc import Callable
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from agents.llm.types import ContextPolicy
from agents.llm.types import LLMRequest
from agents.llm.types import LLMResult
from agents.llm.types import LLMUsage


class ToolRunError(RuntimeError):
    pass


class ToolRunConflict(ToolRunError):
    """The same run/call identity was reused with different immutable inputs."""


class ToolRunStopped(ToolRunError):
    """No further model or tool work may be dispatched."""

    def __init__(self, message, *, code='stopped'):
        super().__init__(message)
        self.code = code


def exact_text(name, value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f'{name} must be an exact nonempty string')


@dataclass(frozen=True, slots=True)
class ToolRunContext:
    user_id: str
    root_run_id: str
    run_id: str
    domain: str
    role: str = 'main'
    parent_run_id: str | None = None
    subject_id: str | None = None

    def __post_init__(self):
        for name in ('user_id', 'root_run_id', 'run_id', 'domain', 'role'):
            exact_text(name, getattr(self, name))
        for name in ('parent_run_id', 'subject_id'):
            if getattr(self, name) is not None:
                exact_text(name, getattr(self, name))
        if (self.parent_run_id is None) != (self.run_id == self.root_run_id):
            raise ValueError('Only the root run may omit parent_run_id')


@dataclass(frozen=True, slots=True)
class ToolBudget:
    """Hard dispatch limits; token/cost reservations are accounting, not billing caps."""

    max_model_calls: int
    max_tool_calls: int
    deadline_at: float
    token_reservation: int = 0
    cost_reservation_units: int = 0

    def __post_init__(self):
        for name in ('max_model_calls', 'max_tool_calls', 'token_reservation', 'cost_reservation_units'):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f'{name} must be a nonnegative integer')
        if self.max_model_calls < 1:
            raise ValueError('At least one model call is required')
        if (
            isinstance(self.deadline_at, bool)
            or not isinstance(self.deadline_at, (int, float))
            or not math.isfinite(self.deadline_at)
        ):
            raise ValueError('deadline_at must be a finite Unix timestamp')


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    version: str
    description: str
    parameters: Mapping[str, Any]
    handler: Callable = field(repr=False, compare=False)
    effect: str = 'read'

    def __post_init__(self):
        exact_text('version', self.version)
        exact_text('description', self.description)
        if not isinstance(self.name, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.name) is None:
            raise ValueError('Invalid tool name')
        if not callable(self.handler) or self.effect not in {'read', 'command'}:
            raise ValueError('A tool requires a handler and a read/command effect')
        if not isinstance(self.parameters, Mapping):
            raise TypeError('parameters must be a JSON Schema mapping')
        object.__setattr__(self, 'parameters', deepcopy(dict(self.parameters)))

    @property
    def identity(self):
        return (self.name, self.version)


@dataclass(frozen=True, slots=True)
class ToolCallContext:
    run: ToolRunContext
    call_id: str
    idempotency_key: str
    tool_name: str
    tool_version: str


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    value: Any
    receipt_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ToolRunRequest:
    llm_request: LLMRequest
    context: ToolRunContext
    budget: ToolBudget
    tools: tuple[ToolDefinition, ...]
    allowed_tools: frozenset[tuple[str, str]]

    def __post_init__(self):
        if (
            not isinstance(self.llm_request, LLMRequest)
            or not isinstance(self.context, ToolRunContext)
            or not isinstance(self.budget, ToolBudget)
        ):
            raise TypeError('Invalid tool run request components')
        if self.llm_request.profile.context_policy is not ContextPolicy.STATELESS:
            raise ValueError('Bounded tool runs require isolated stateless model context')
        if self.llm_request.effective_web_search or self.llm_request.search_query is not None:
            raise ValueError('Tool runs cannot combine provider web_search with registered tools')
        reserved = {
            'tools',
            'tool_choice',
            'functions',
            'function_call',
            'tool_functions',
            'tool_options',
            'tool_trace',
        }
        for options in (
            self.llm_request.profile.model_kwargs,
            self.llm_request.profile.model_kwargs.get('extra_body', {}),
        ):
            if isinstance(options, Mapping) and reserved.intersection(options):
                raise ValueError('Tool protocol settings are owned by ToolRunGateway')
        if (
            not isinstance(self.tools, tuple)
            or not self.tools
            or any(not isinstance(tool, ToolDefinition) for tool in self.tools)
        ):
            raise ValueError('tools must be a nonempty tuple of ToolDefinition')
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ValueError('Select exactly one version per tool name')
        if not isinstance(self.allowed_tools, frozenset) or any(
            tool.identity not in self.allowed_tools for tool in self.tools
        ):
            raise PermissionError('Every requested tool version must be server-authorized')


@dataclass(frozen=True, slots=True)
class ToolRunResult:
    run_id: str
    status: str
    result: LLMResult | None = None
    usage: LLMUsage | None = None
    receipts: tuple[Mapping[str, Any], ...] = ()
    reason: str | None = None
    usage_complete: bool = False


@dataclass(frozen=True, slots=True)
class ToolRunEvent:
    """Internal execution events; only completed.result is publishable answer text."""

    kind: str
    run_id: str
    step_index: int = 0
    call_id: str | None = None
    tool_name: str | None = None
    result: ToolRunResult | None = None
