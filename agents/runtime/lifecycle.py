"""Agent 生产运行时的创建和资源所有权。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from pymongo import MongoClient

from ..hub import AgentHub
from ..knowledge import EviMedKnowledgeGateway
from ..llm.aion_gateway import AionLLMGateway
from ..llm.profiles import ModelProfileRegistry
from ..persistence.checkpoint import create_mongodb_checkpointer
from ..persistence.locks import RedisAgentLeaseManager
from ..persistence.mongodb_llm_context import MongoLLMContextRepository
from .container import AgentRuntime
from .registry import GraphRegistry
from utils.knowledge import Knowledge

MongoClientFactory = Callable[[str], Any]
CheckpointerFactory = Callable[..., BaseCheckpointSaver]


@dataclass(frozen=True, slots=True)
class AgentRuntimeSettings:
    """创建 AgentRuntime 所需且与 Web 框架无关的配置。"""

    provider: str
    api_key: str = field(repr=False)
    mongodb_uri: str = field(repr=False)
    mongodb_database: str
    checkpoint_collection: str = 'agent_checkpoints'
    checkpoint_writes_collection: str = 'agent_checkpoint_writes'
    lease_ttl_seconds: int = 300
    default_model: str | None = None
    knowledge_api_base_url: str = Knowledge.BASE_URL
    knowledge_api_key: str = field(default='', repr=False)
    knowledge_timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        for name in (
                'provider',
                'api_key',
                'mongodb_uri',
                'mongodb_database',
                'checkpoint_collection',
                'checkpoint_writes_collection',
        ):
            value = _normalize_text(name, getattr(self, name))
            object.__setattr__(self, name, value)
        if self.default_model is not None:
            object.__setattr__(
                self,
                'default_model',
                _normalize_text('default_model', self.default_model),
            )
        object.__setattr__(
            self,
            'knowledge_api_base_url',
            _normalize_text(
                'knowledge_api_base_url',
                self.knowledge_api_base_url,
            ),
        )
        if not isinstance(self.knowledge_api_key, str):
            raise TypeError('knowledge_api_key must be a string')
        object.__setattr__(
            self,
            'knowledge_api_key',
            self.knowledge_api_key.strip(),
        )
        if isinstance(self.knowledge_timeout_seconds, bool) or not isinstance(
                self.knowledge_timeout_seconds,
                (int, float),
        ):
            raise TypeError('knowledge_timeout_seconds must be a number')
        if self.knowledge_timeout_seconds <= 0:
            raise ValueError(
                'knowledge_timeout_seconds must be greater than zero',
            )
        if (
                isinstance(self.lease_ttl_seconds, bool)
                or not isinstance(self.lease_ttl_seconds, int)
        ):
            raise TypeError('lease_ttl_seconds must be an integer')
        if self.lease_ttl_seconds <= 0:
            raise ValueError('lease_ttl_seconds must be greater than zero')


def create_agent_runtime(
        settings: AgentRuntimeSettings,
        *,
        context_collection: Any,
        redis_client: Any,
        mongo_client_factory: MongoClientFactory = MongoClient,
        checkpointer_factory: CheckpointerFactory = create_mongodb_checkpointer,
) -> AgentRuntime:
    """创建应用级 AgentRuntime，并令其拥有同步 checkpoint client。"""
    if not isinstance(settings, AgentRuntimeSettings):
        raise TypeError('settings must be AgentRuntimeSettings')
    if not callable(mongo_client_factory):
        raise TypeError('mongo_client_factory must be callable')
    if not callable(checkpointer_factory):
        raise TypeError('checkpointer_factory must be callable')

    mongo_client = mongo_client_factory(settings.mongodb_uri)
    checkpointer: BaseCheckpointSaver | None = None
    try:
        checkpointer = checkpointer_factory(
            mongo_client,
            database_name=settings.mongodb_database,
            checkpoint_collection_name=settings.checkpoint_collection,
            writes_collection_name=settings.checkpoint_writes_collection,
        )
        context_repository = MongoLLMContextRepository(context_collection)
        lease_manager = RedisAgentLeaseManager(
            redis_client,
            ttl_seconds=settings.lease_ttl_seconds,
        )
        gateway = AionLLMGateway(
            provider=settings.provider,
            api_key=settings.api_key,
            default_model=settings.default_model,
            context_repository=context_repository,
        )
        knowledge_gateway = EviMedKnowledgeGateway(Knowledge(
            settings.knowledge_api_key,
            base_url=settings.knowledge_api_base_url,
            timeout_seconds=settings.knowledge_timeout_seconds,
        ))
        registry = GraphRegistry()
        profiles = ModelProfileRegistry()
        return AgentRuntime(
            hub=AgentHub(registry),
            registry=registry,
            profiles=profiles,
            llm_gateway=gateway,
            context_repository=context_repository,
            lease_manager=lease_manager,
            checkpointer=checkpointer,
            _close_callback=lambda: _close_checkpointer(checkpointer),
            knowledge_gateway=knowledge_gateway,
        )
    except Exception:
        if checkpointer is not None:
            _close_checkpointer(checkpointer)
        else:
            close_client = getattr(mongo_client, 'close', None)
            if callable(close_client):
                close_client()
        raise


def _close_checkpointer(checkpointer: BaseCheckpointSaver) -> None:
    close = getattr(checkpointer, 'close', None)
    if not callable(close):
        raise TypeError('checkpointer must provide a close method')
    close()


def _normalize_text(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be a string')
    normalized = value.strip()
    if not normalized:
        raise ValueError(f'{name} cannot be empty')
    return normalized
