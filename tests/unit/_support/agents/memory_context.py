"""仅用于无网络测试的进程内模型 Context Repository。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from agents.errors import LLMContextConflictError
from agents.persistence.llm_context import LLMContextSnapshot


class InMemoryLLMContextRepository:
    """使用 revision 乐观检查保存独立 Context 快照。"""

    def __init__(self) -> None:
        self._snapshots: dict[str, LLMContextSnapshot] = {}

    async def load(self, key: str) -> LLMContextSnapshot | None:
        snapshot = self._snapshots.get(key)
        if snapshot is None:
            return None
        return _copy_snapshot(snapshot)

    async def save(
            self,
            key: str,
            payload: Mapping[str, Any],
            *,
            expected_revision: int,
    ) -> LLMContextSnapshot:
        if not isinstance(payload, Mapping):
            raise TypeError('payload must be a mapping')
        if isinstance(expected_revision, bool):
            raise TypeError('expected_revision must be an integer')
        if not isinstance(expected_revision, int):
            raise TypeError('expected_revision must be an integer')
        if expected_revision < 0:
            raise ValueError('expected_revision cannot be negative')

        current = self._snapshots.get(key)
        current_revision = 0 if current is None else current.revision
        if current_revision != expected_revision:
            raise LLMContextConflictError(
                f'LLM context revision conflict for {key!r}: '
                f'expected {expected_revision}, current {current_revision}',
            )

        snapshot = LLMContextSnapshot(
            key=key,
            revision=current_revision + 1,
            payload=deepcopy(dict(payload)),
        )
        self._snapshots[key] = snapshot
        return _copy_snapshot(snapshot)


def _copy_snapshot(snapshot: LLMContextSnapshot) -> LLMContextSnapshot:
    return LLMContextSnapshot(
        key=snapshot.key,
        revision=snapshot.revision,
        payload=deepcopy(dict(snapshot.payload)),
    )
