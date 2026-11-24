"""知识检索的厂商无关类型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from enum import StrEnum
from typing import Any


class KnowledgeSource(StrEnum):
    """当前知识库对外提供的检索通道。"""

    LITERATURE = 'literature'
    GUIDE = 'guide'
    INSTRUCTION = 'instruction'
    CLINICAL_TRIAL = 'clinical_trial'


@dataclass(frozen=True, slots=True)
class KnowledgeQuery:
    """一次受控的知识库查询。"""

    source: KnowledgeSource
    query: str
    count: int = 3
    include_source_text: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.include_source_text, bool):
            raise TypeError('include_source_text must be a boolean')
        if not isinstance(self.source, KnowledgeSource):
            raise TypeError('source must be a KnowledgeSource')
        query = _normalize_text('query', self.query)
        if len(query) > 256:
            raise ValueError('query cannot exceed 256 characters')
        object.__setattr__(self, 'query', query)
        if isinstance(self.count, bool) or not isinstance(self.count, int):
            raise TypeError('count must be an integer')
        if self.count < 1 or self.count > 10:
            raise ValueError('count must be between 1 and 10')


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    """交给 Graph 的规范化检索条目。"""

    source: KnowledgeSource
    title: str
    content: str
    url: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.source, KnowledgeSource):
            raise TypeError('source must be a KnowledgeSource')
        object.__setattr__(self, 'title', _normalize_text('title', self.title))
        object.__setattr__(
            self,
            'content',
            _normalize_text('content', self.content),
        )
        if self.url is not None:
            object.__setattr__(self, 'url', _normalize_text('url', self.url))
        if not isinstance(self.metadata, Mapping):
            raise TypeError('metadata must be a mapping')
        object.__setattr__(self, 'metadata', dict(self.metadata))


@dataclass(frozen=True, slots=True)
class KnowledgeRetrievalPlan:
    """模型给出并经本地校验后的检索计划。"""

    queries: tuple[KnowledgeQuery, ...] = ()

    @property
    def needed(self) -> bool:
        return bool(self.queries)


def _normalize_text(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be a string')
    normalized = value.strip()
    if not normalized:
        raise ValueError(f'{name} cannot be empty')
    return normalized
