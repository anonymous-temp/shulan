"""使用异步 PyMongo collection 持久化 AION Context v1。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Protocol

from pymongo.errors import DuplicateKeyError

from .llm_context import LLMContextSnapshot
from ..errors import LLMContextConflictError
from ..errors import LLMContextStorageError


class AsyncMongoCollection(Protocol):
    """MongoLLMContextRepository 使用的最小异步 collection 接口。"""

    async def find_one(self, filter: Mapping[str, Any]) -> Any:
        ...

    async def insert_one(self, document: Mapping[str, Any]) -> Any:
        ...

    async def update_one(
            self,
            filter: Mapping[str, Any],
            update: Mapping[str, Any],
    ) -> Any:
        ...


class MongoLLMContextRepository:
    """将每个模型会话保存为带乐观 revision 的独立 MongoDB 文档。"""

    def __init__(self, collection: AsyncMongoCollection) -> None:
        for method_name in ('find_one', 'insert_one', 'update_one'):
            if not callable(getattr(collection, method_name, None)):
                raise TypeError(
                    'collection must provide async find_one, insert_one '
                    'and update_one methods',
                )
        self._collection = collection

    async def load(self, key: str) -> LLMContextSnapshot | None:
        """按内部上下文键读取最新 Context 快照。"""
        _require_key(key)
        try:
            document = await self._collection.find_one({'_id': key})
        except Exception as error:
            raise LLMContextStorageError(
                f'Cannot load LLM context {key!r}',
            ) from error

        if document is None:
            return None
        return _snapshot_from_document(key, document)

    async def save(
            self,
            key: str,
            payload: Mapping[str, Any],
            *,
            expected_revision: int,
    ) -> LLMContextSnapshot:
        """仅当存储中的 revision 符合预期时保存 Context。"""
        _require_key(key)
        _require_payload(payload)
        _require_expected_revision(expected_revision)

        next_revision = expected_revision + 1
        stored_payload = deepcopy(dict(payload))
        now = datetime.now(timezone.utc)

        if expected_revision == 0:
            document = {
                '_id': key,
                'revision': next_revision,
                'payload': stored_payload,
                'createdAt': now,
                'updatedAt': now,
            }
            try:
                await self._collection.insert_one(document)
            except DuplicateKeyError as error:
                raise _conflict(key, expected_revision) from error
            except Exception as error:
                raise LLMContextStorageError(
                    f'Cannot create LLM context {key!r}',
                ) from error
        else:
            try:
                result = await self._collection.update_one(
                    {'_id': key, 'revision': expected_revision},
                    {
                        '$set': {
                            'payload': stored_payload,
                            'updatedAt': now,
                        },
                        '$inc': {'revision': 1},
                    },
                )
            except Exception as error:
                raise LLMContextStorageError(
                    f'Cannot update LLM context {key!r}',
                ) from error

            matched_count = getattr(result, 'matched_count', None)
            if isinstance(matched_count, bool) or not isinstance(
                    matched_count,
                    int,
            ):
                raise LLMContextStorageError(
                    'MongoDB update result has no integer matched_count',
                )
            if matched_count == 0:
                raise _conflict(key, expected_revision)

        return LLMContextSnapshot(
            key=key,
            revision=next_revision,
            payload=deepcopy(stored_payload),
        )


def _snapshot_from_document(
        expected_key: str,
        document: Any,
) -> LLMContextSnapshot:
    if not isinstance(document, Mapping):
        raise LLMContextStorageError(
            f'Stored LLM context {expected_key!r} is not a document',
        )
    if document.get('_id') != expected_key:
        raise LLMContextStorageError(
            f'Stored LLM context key does not match {expected_key!r}',
        )
    try:
        return LLMContextSnapshot(
            key=expected_key,
            revision=document['revision'],
            payload=deepcopy(dict(document['payload'])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise LLMContextStorageError(
            f'Stored LLM context {expected_key!r} is invalid',
        ) from error


def _conflict(key: str, expected_revision: int) -> LLMContextConflictError:
    return LLMContextConflictError(
        f'LLM context revision conflict for {key!r}: '
        f'expected {expected_revision}',
    )


def _require_key(value: str) -> None:
    if not isinstance(value, str):
        raise TypeError('key must be a string')
    if not value.strip():
        raise ValueError('key cannot be empty')


def _require_payload(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping):
        raise TypeError('payload must be a mapping')


def _require_expected_revision(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError('expected_revision must be an integer')
    if value < 0:
        raise ValueError('expected_revision cannot be negative')
