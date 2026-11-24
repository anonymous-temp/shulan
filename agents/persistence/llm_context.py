"""AION Context v1 的持久化边界。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from typing import Protocol
from typing import runtime_checkable


@dataclass(frozen=True, slots=True)
class LLMContextSnapshot:
    """一个模型会话在指定 revision 的 Context v1 快照。"""

    key: str
    revision: int
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        _require_key(self.key)
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise TypeError('revision must be an integer')
        if self.revision < 1:
            raise ValueError('revision must be greater than zero')
        if not isinstance(self.payload, Mapping):
            raise TypeError('payload must be a mapping')


@runtime_checkable
class LLMContextRepository(Protocol):
    """加载并以乐观 revision 保存 AION Context v1。"""

    async def load(self, key: str) -> LLMContextSnapshot | None:
        ...

    async def save(
            self,
            key: str,
            payload: Mapping[str, Any],
            *,
            expected_revision: int,
    ) -> LLMContextSnapshot:
        ...


def _require_key(value: str) -> None:
    if not isinstance(value, str):
        raise TypeError('key must be a string')
    if not value.strip():
        raise ValueError('key cannot be empty')
