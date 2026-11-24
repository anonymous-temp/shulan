"""Agent capability 与已编译 LangGraph 的注册表。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from typing import Protocol

from ..contracts import AgentSelection
from ..errors import AgentCapabilityAlreadyRegisteredError
from ..errors import AgentCapabilityNotFoundError


class CompiledAgentGraph(Protocol):
    """AgentHub 实际依赖的最小已编译 Graph 协议。"""

    async def ainvoke(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        *,
        context: Any = None,
        **kwargs: Any,
    ) -> Any: ...

    def astream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        *,
        context: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[Any]: ...


@dataclass(frozen=True, slots=True)
class GraphRegistration:
    """一个 capability 的已编译 Graph 和版本。"""

    capability: str
    graph: CompiledAgentGraph
    version: str
    contract_version: str = '1'

    @property
    def selection(self) -> AgentSelection:
        return AgentSelection(self.capability, self.version, self.contract_version)


class GraphRegistry:
    """在应用启动阶段注册 Graph，运行阶段按 capability 查询。"""

    def __init__(self) -> None:
        self._registrations: dict[AgentSelection, GraphRegistration] = {}
        self._legacy: dict[str, AgentSelection] = {}

    def register(
        self,
        capability: str,
        graph: CompiledAgentGraph,
        *,
        version: str,
        contract_version: str = '1',
        legacy: bool = True,
    ) -> GraphRegistration:
        normalized_capability = _require_text('capability', capability)
        normalized_version = _require_text('version', version)
        if not isinstance(legacy, bool):
            raise TypeError('legacy must be a boolean')
        selection = AgentSelection(normalized_capability, normalized_version, contract_version)
        if selection in self._registrations or legacy and normalized_capability in self._legacy:
            raise AgentCapabilityAlreadyRegisteredError(
                f'Agent capability already registered: {normalized_capability}',
            )
        registration = GraphRegistration(
            capability=normalized_capability,
            graph=graph,
            version=normalized_version,
            contract_version=contract_version,
        )
        self._registrations[selection] = registration
        if legacy:
            self._legacy[normalized_capability] = selection
        return registration

    def get(self, capability: str, *, selection: AgentSelection | None = None) -> GraphRegistration:
        normalized_capability = _require_text('capability', capability)
        if selection is not None and not isinstance(selection, AgentSelection):
            raise TypeError('selection must be AgentSelection')
        if selection is not None and selection.capability != normalized_capability:
            raise ValueError('selection must match capability')
        try:
            return self._registrations[selection if selection is not None else self._legacy[normalized_capability]]
        except KeyError as error:
            raise AgentCapabilityNotFoundError(
                f'Unknown Agent capability: {normalized_capability}',
            ) from error

    def capabilities(self) -> tuple[str, ...]:
        """按名称返回稳定排序的已注册能力。"""
        return tuple(sorted({item.capability for item in self._registrations}))

    def versions(self, capability: str) -> tuple[AgentSelection, ...]:
        name = _require_text('capability', capability)
        return tuple(
            sorted(
                (item for item in self._registrations if item.capability == name),
                key=lambda item: (item.contract_version, item.implementation_revision),
            )
        )

    def merged(self, staged: 'GraphRegistry') -> 'GraphRegistry':
        """Validate a whole module before exposing any of its registrations."""
        result = GraphRegistry()
        result._registrations = dict(self._registrations)
        result._legacy = dict(self._legacy)
        for selection, item in staged._registrations.items():
            result.register(
                item.capability,
                item.graph,
                version=item.version,
                contract_version=item.contract_version,
                legacy=staged._legacy.get(item.capability) == selection,
            )
        return result

    def publish(self, validated: 'GraphRegistry') -> None:
        self._registrations = dict(validated._registrations)
        self._legacy = dict(validated._legacy)


def _require_text(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be a string')
    normalized = value.strip()
    if not normalized:
        raise ValueError(f'{name} cannot be empty')
    return normalized
