"""MongoDB and Redis implementations of Chat and Record session ports."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from modules.chat.ports import ChatSessionRepository
from modules.record.ports import RecordSessionCache
from modules.record.ports import RecordSessionRepository


class MongoChatSessionRepository:
    """Store Chat session documents in the shared MongoDB collection."""

    def __init__(self, collection) -> None:
        self._collection = collection

    async def get(self, user_id: str, session_id: str) -> dict | None:
        document = await self._collection.find_one(
            {
                'userId': user_id,
                'sessionId': session_id,
            }
        )
        return deepcopy(document) if document is not None else None

    async def save(
        self,
        user_id: str,
        session_id: str,
        session: Mapping[str, Any],
        *,
        upsert: bool,
    ) -> bool:
        result = await self._collection.replace_one(
            {'userId': user_id, 'sessionId': session_id},
            dict(session),
            upsert=upsert,
        )
        return bool(result.acknowledged)

    async def list_session_ids(self, user_id: str) -> list[str]:
        cursor = self._collection.find(
            {'userId': user_id},
            {'sessionId': 1},
        )
        return [document['sessionId'] async for document in cursor]


class MongoRecordSessionRepository:
    """Store completed Record snapshots in the shared MongoDB collection."""

    def __init__(self, collection) -> None:
        self._collection = collection
        self._owner_index_ready = False
        self._owner_index_lock = asyncio.Lock()

    async def get(self, user_id: str, session_id: str) -> dict | None:
        document = await self._collection.find_one(
            {
                'userId': user_id,
                'sessionId': session_id,
            }
        )
        return deepcopy(document) if document is not None else None

    async def save(
        self,
        user_id: str,
        session_id: str,
        session: Mapping[str, Any],
    ) -> bool:
        result = await self._collection.replace_one(
            {'userId': user_id, 'sessionId': session_id},
            dict(session),
            upsert=True,
        )
        return bool(result.acknowledged)

    async def save_questionnaire_owner(
        self,
        user_id: str,
        session_id: str,
        session: Mapping[str, Any],
        *,
        expected: Mapping[str, Any] | None,
    ) -> bool:
        """Fence marker writes against Chat takeover and other owner revisions."""
        if not self._owner_index_ready:
            async with self._owner_index_lock:
                if not self._owner_index_ready:
                    await self._collection.create_index(
                        [('userId', 1), ('sessionId', 1)],
                        unique=True,
                        partialFilterExpression={
                            'userId': {'$type': 'string'},
                            'sessionId': {'$type': 'string'},
                        },
                        name='session_business_identity',
                    )
                    self._owner_index_ready = True
        if expected is None:
            try:
                result = await self._collection.insert_one(dict(session))
            except Exception as error:
                if type(error).__name__ == 'DuplicateKeyError':
                    return False
                raise
            return bool(result.acknowledged)
        if expected.get('type') != 'questionnaire':
            return False
        result = await self._collection.replace_one(
            {
                'userId': user_id,
                'sessionId': session_id,
                'type': 'questionnaire',
                'questionnaireId': expected.get('questionnaireId'),
                'ownerRevision': expected.get('ownerRevision'),
            },
            dict(session),
            upsert=False,
        )
        return bool(result.acknowledged and result.matched_count == 1)


class RedisRecordSessionCache:
    """Store active Record session JSON under stable Redis keys."""

    def __init__(self, client) -> None:
        self._client = client

    async def get(self, user_id: str, session_id: str) -> dict | None:
        value = await self._client.get(
            _session_cache_key(user_id, session_id),
        )
        if not value:
            return None
        return json.loads(value)

    async def save(
        self,
        user_id: str,
        session_id: str,
        session: Mapping[str, Any],
        *,
        ttl_seconds: int,
    ) -> bool:
        value = json.dumps(dict(session), ensure_ascii=False)
        result = await self._client.setex(
            _session_cache_key(user_id, session_id),
            ttl_seconds,
            value,
        )
        return bool(result)

    async def delete(self, user_id: str, session_id: str) -> bool:
        await self._client.delete(_session_cache_key(user_id, session_id))
        return True


def _session_cache_key(user_id: str, session_id: str) -> str:
    return f'shulan:session:{user_id}:{session_id}'


def create_application_chat_session_repository() -> ChatSessionRepository:
    from db.mongodb import collection_sessions

    return MongoChatSessionRepository(collection_sessions)


def create_application_record_session_repository() -> RecordSessionRepository:
    from db.mongodb import collection_sessions

    return MongoRecordSessionRepository(collection_sessions)


def create_application_record_session_cache() -> RecordSessionCache:
    from db.redis import client

    return RedisRecordSessionCache(client)


__all__ = [
    'MongoChatSessionRepository',
    'MongoRecordSessionRepository',
    'RedisRecordSessionCache',
    'create_application_chat_session_repository',
    'create_application_record_session_cache',
    'create_application_record_session_repository',
]
