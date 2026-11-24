"""不访问网络的 KnowledgeGateway 测试替身。"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence

from agents.knowledge.types import KnowledgeDocument
from agents.knowledge.types import KnowledgeQuery
from agents.knowledge.types import KnowledgeSource


class FakeKnowledgeGateway:
    def __init__(
            self,
            results: Mapping[
                KnowledgeSource,
                Sequence[KnowledgeDocument],
            ] | None = None,
            *,
            enabled: bool = True,
            error: Exception | None = None,
    ) -> None:
        self._enabled = enabled
        self._error = error
        self._results = {
            source: tuple(documents)
            for source, documents in (results or {}).items()
        }
        self.queries: list[KnowledgeQuery] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def retrieve(
            self,
            query: KnowledgeQuery,
    ) -> tuple[KnowledgeDocument, ...]:
        if not isinstance(query, KnowledgeQuery):
            raise TypeError('query must be a KnowledgeQuery')
        self.queries.append(query)
        if self._error is not None:
            raise self._error
        return self._results.get(query.source, ())
