"""Agent Core 对业务层公开的稳定数据契约。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentSelection:
    """A complete public capability binding, never an implicit latest version."""

    capability: str
    implementation_revision: str
    contract_version: str = '1'

    def __post_init__(self) -> None:
        for name in ('capability', 'implementation_revision', 'contract_version'):
            value = getattr(self, name)
            _require_identifier(name, value)
            if value != value.strip():
                raise ValueError(f'{name} must be an exact identifier')


class AgentEventType(StrEnum):
    """AgentHub 对上层暴露的统一运行事件。"""

    RUN_STARTED = 'run_started'
    REASONING_DELTA = 'reasoning_delta'
    CONTENT_DELTA = 'content_delta'
    PROGRESS = 'progress'
    TOOL_STARTED = 'tool_started'
    TOOL_FINISHED = 'tool_finished'
    RUN_COMPLETED = 'run_completed'
    RUN_FAILED = 'run_failed'


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """一次 Agent 调用的 Graph 输入。"""

    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise TypeError('AgentRequest.payload must be a mapping')

    def to_graph_input(self) -> dict[str, Any]:
        """返回与调用方容器隔离的 Graph 输入。"""
        return dict(self.payload)


@dataclass(frozen=True, slots=True)
class AgentResult:
    """一次完整 Agent 调用的统一结果。"""

    capability: str
    run_id: str
    graph_version: str
    data: Mapping[str, Any]

    def __post_init__(self) -> None:
        _require_identifier('capability', self.capability)
        _require_identifier('run_id', self.run_id)
        _require_identifier('graph_version', self.graph_version)
        if not isinstance(self.data, Mapping):
            raise TypeError('AgentResult.data must be a mapping')


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """一次流式 Agent 调用中的内部事件。"""

    type: AgentEventType
    run_id: str
    data: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.type, AgentEventType):
            raise TypeError('AgentEvent.type must be an AgentEventType')
        _require_identifier('run_id', self.run_id)
        if not isinstance(self.data, Mapping):
            raise TypeError('AgentEvent.data must be a mapping')

    def to_dict(self) -> dict[str, Any]:
        """转换为可供 SSE 映射层消费的普通字典。"""
        return {
            'type': self.type.value,
            'runId': self.run_id,
            'data': dict(self.data),
        }


def _require_identifier(name: str, value: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be a string')
    if not value.strip():
        raise ValueError(f'{name} cannot be empty')
