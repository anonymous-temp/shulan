"""LangGraph 节点依赖的知识检索协议。"""

from __future__ import annotations

from typing import Protocol
from typing import runtime_checkable

from .types import KnowledgeDocument
from .types import KnowledgeQuery


@runtime_checkable
class KnowledgeGateway(Protocol):
    """屏蔽知识库供应商和 HTTP 响应差异。"""

    @property
    def enabled(self) -> bool:
        ...

    async def retrieve(
            self,
            query: KnowledgeQuery,
    ) -> tuple[KnowledgeDocument, ...]:
        ...


class DisabledKnowledgeGateway:
    """未配置知识库时的显式空实现。"""

    @property
    def enabled(self) -> bool:
        return False

    async def retrieve(
            self,
            query: KnowledgeQuery,
    ) -> tuple[KnowledgeDocument, ...]:
        if not isinstance(query, KnowledgeQuery):
            raise TypeError('query must be a KnowledgeQuery')
        return ()
