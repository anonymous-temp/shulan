"""集中持有 Shulan Agent 生产运行时依赖。"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver

from ..errors import AgentRuntimeClosedError
from ..knowledge.protocol import DisabledKnowledgeGateway
from ..knowledge.protocol import KnowledgeGateway
from ..llm.profiles import ModelProfileRegistry
from ..llm.protocol import LLMGateway
from ..persistence.llm_context import LLMContextRepository
from ..persistence.locks import AgentLeaseManager
from .ports import AgentPortRegistry
from .registry import CompiledAgentGraph
from .registry import GraphRegistration
from .registry import GraphRegistry

if TYPE_CHECKING:
    from ..hub import AgentHub


class AgentGraphBuilder(Protocol):
    """AgentRuntime 统一编译的最小 StateGraph builder 接口。"""

    def compile(
        self,
        checkpointer: BaseCheckpointSaver | None,
        **kwargs: Any,
    ) -> CompiledAgentGraph: ...


@dataclass(slots=True)
class AgentRuntime:
    """Graph、模型和持久化组件的应用级单例容器。"""

    hub: AgentHub
    registry: GraphRegistry
    profiles: ModelProfileRegistry
    llm_gateway: LLMGateway
    context_repository: LLMContextRepository
    lease_manager: AgentLeaseManager
    checkpointer: BaseCheckpointSaver
    _close_callback: Callable[[], None] = field(repr=False)
    knowledge_gateway: KnowledgeGateway = field(
        default_factory=DisabledKnowledgeGateway,
    )
    ports: AgentPortRegistry = field(default_factory=AgentPortRegistry)
    _closed: bool = field(default=False, init=False, repr=False)
    module_statuses: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    _module_cleanups: list[Callable[[], None]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.knowledge_gateway, KnowledgeGateway):
            raise TypeError('knowledge_gateway must implement KnowledgeGateway')

    def register_graph(
        self,
        capability: str,
        builder: AgentGraphBuilder,
        *,
        version: str,
        checkpointed: bool = True,
        contract_version: str = '1',
        legacy: bool = True,
    ) -> GraphRegistration:
        """编译并注册 Graph；无状态任务可以显式关闭 checkpoint。"""
        self._require_open()
        if not isinstance(checkpointed, bool):
            raise TypeError('checkpointed must be a boolean')
        compile_graph = getattr(builder, 'compile', None)
        if not callable(compile_graph):
            raise TypeError('builder must provide a compile method')
        graph = compile_graph(
            checkpointer=self.checkpointer if checkpointed else None,
        )
        return self.registry.register(
            capability, graph, version=version, contract_version=contract_version, legacy=legacy
        )

    def install_module(
        self,
        name: str,
        configure: Callable[['AgentRuntime'], None],
        *,
        cleanup: tuple[Callable[[], None], ...] = (),
        required: bool = False,
        enabled: bool = True,
    ) -> bool:
        """Stage graphs and profiles together; failed optional modules expose neither.

        Configure is synchronous startup code. Only explicitly supplied module-owned
        resources are cleaned up; the staged runtime cannot close shared clients.
        Application registration uses this boundary for every domain batch.
        """
        self._require_open()
        if not isinstance(name, str) or not name.strip() or name in self.module_statuses:
            raise ValueError('module name must be nonempty and not already installed')
        if not isinstance(enabled, bool):
            raise TypeError('enabled must be a boolean')
        if not enabled:
            self.module_statuses[name] = {'ready': False, 'error': 'disabled', 'cleanupErrors': [], 'capabilities': ()}
            return False
        from ..hub import AgentHub

        registry = GraphRegistry()
        staged = replace(
            self,
            registry=registry,
            hub=AgentHub(registry),
            profiles=ModelProfileRegistry(fallback=self.profiles),
            ports=AgentPortRegistry(),
            _close_callback=lambda: None,
        )
        try:
            outcome = configure(staged)
            if inspect.isawaitable(outcome):
                if inspect.iscoroutine(outcome):
                    outcome.close()
                raise TypeError('Module configuration must be synchronous')
            staged._require_open()
            graphs = self.registry.merged(staged.registry)
            profiles = self.profiles.merged(staged.profiles)
            ports = self.ports.merged(staged.ports)
        except Exception as error:
            cleanup_errors = []
            for close in reversed(cleanup):
                try:
                    close()
                except Exception as cleanup_error:
                    cleanup_errors.append(type(cleanup_error).__name__)
            self.module_statuses[name] = {
                'ready': False,
                'error': type(error).__name__,
                'cleanupErrors': cleanup_errors,
                'capabilities': (),
            }
            if required:
                raise
            return False
        # Startup is synchronous: no await or user callback splits publication.
        self.profiles.publish(profiles)
        self.registry.publish(graphs)
        self.ports.publish(ports)
        self._module_cleanups.extend(cleanup)
        self.module_statuses[name] = {
            'ready': True,
            'error': None,
            'cleanupErrors': [],
            'capabilities': tuple(
                sorted(set(staged.registry.capabilities()) | {item.capability for item in staged.ports.selections()})
            ),
        }
        return True

    def close(self) -> None:
        """幂等关闭运行时拥有的 MongoDB checkpointer 资源。"""
        if self._closed:
            return
        errors = []
        for close in reversed(self._module_cleanups):
            try:
                close()
            except Exception as error:
                errors.append(error)
        try:
            self._close_callback()
        finally:
            self._closed = True
            self._module_cleanups.clear()
        if errors:
            raise ExceptionGroup('Agent module cleanup failed', errors)

    @property
    def closed(self) -> bool:
        return self._closed

    def _require_open(self) -> None:
        if self._closed:
            raise AgentRuntimeClosedError('Agent runtime is already closed')
