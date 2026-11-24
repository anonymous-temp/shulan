"""MongoDB adapter for the Chat custom-message Outbox port."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from datetime import timezone
from typing import Any

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

from modules.chat.domain import ChatOutboxConflictError


ChatCustomMessageOutboxConflictError = ChatOutboxConflictError


class ChatCustomMessageOutbox:
    """以 ``(userId, sessionId, eventId)`` 为幂等键保存待发送消息。"""

    def __init__(self, collection) -> None:
        self._collection = collection
        self._indexes_ready = False
        self._index_lock = asyncio.Lock()

    async def ensure_indexes(self) -> None:
        """创建幂等和待投递扫描索引；同一进程只初始化一次。"""
        if self._indexes_ready:
            return
        async with self._index_lock:
            if self._indexes_ready:
                return
            await self._collection.create_index(
                [
                    ('userId', ASCENDING),
                    ('sessionId', ASCENDING),
                    ('eventId', ASCENDING),
                ],
                unique=True,
                name='chat_custom_message_event_unique',
            )
            await self._collection.create_index(
                [('status', ASCENDING), ('createdAt', ASCENDING)],
                name='chat_custom_message_pending_scan',
            )
            self._indexes_ready = True

    async def enqueue(
            self,
            *,
            user_id: str,
            session_id: str,
            event_id: str,
            message: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """幂等保存待发送事件，返回 ``(事件, 是否首次创建)``。"""
        await self.ensure_indexes()
        key = self._event_key(user_id, session_id, event_id)
        existing = await self._collection.find_one(key)
        if existing is not None:
            self._require_same_message(existing, message)
            return deepcopy(existing), False

        now = datetime.now(timezone.utc)
        document = {
            **key,
            'message': deepcopy(dict(message)),
            'status': 'pending',
            'attempts': 0,
            'createdAt': now,
            'updatedAt': now,
        }
        try:
            result = await self._collection.insert_one(document)
        except DuplicateKeyError:
            existing = await self._collection.find_one(key)
            if existing is None:
                raise
            self._require_same_message(existing, message)
            return deepcopy(existing), False
        if not result.acknowledged:
            raise RuntimeError('保存自定义消息待发送事件失败')
        return deepcopy(document), True

    async def get(
            self,
            user_id: str,
            session_id: str,
            event_id: str,
    ) -> dict[str, Any] | None:
        document = await self._collection.find_one(
            self._event_key(user_id, session_id, event_id),
        )
        return deepcopy(document) if document is not None else None

    async def list_pending_sessions(
            self,
            *,
            limit: int = 100,
    ) -> list[tuple[str, str]]:
        """按最早事件顺序返回存在待发送消息的会话。"""
        await self.ensure_indexes()
        cursor = (
            self._collection.find(
                {'status': 'pending'},
                {'userId': 1, 'sessionId': 1},
            )
            .sort('createdAt', ASCENDING)
            .limit(limit)
        )
        sessions: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        async for document in cursor:
            key = (document['userId'], document['sessionId'])
            if key not in seen:
                seen.add(key)
                sessions.append(key)
        return sessions

    async def list_pending(
            self,
            user_id: str,
            session_id: str,
    ) -> list[dict[str, Any]]:
        """按入队顺序读取一个会话的全部待发送事件。"""
        cursor = self._collection.find({
            'userId': user_id,
            'sessionId': session_id,
            'status': 'pending',
        }).sort('createdAt', ASCENDING)
        return [deepcopy(document) async for document in cursor]

    async def record_attempt(self, event: Mapping[str, Any]) -> None:
        await self._collection.update_one(
            self._event_key_from_document(event),
            {
                '$inc': {'attempts': 1},
                '$set': {'updatedAt': datetime.now(timezone.utc)},
            },
        )

    async def mark_delivered(
            self,
            event: Mapping[str, Any],
            message: Mapping[str, Any],
    ) -> None:
        now = datetime.now(timezone.utc)
        result = await self._collection.update_one(
            self._event_key_from_document(event),
            {
                '$set': {
                    'status': 'delivered',
                    'deliveredMessage': deepcopy(dict(message)),
                    'deliveredAt': now,
                    'updatedAt': now,
                },
                '$unset': {'lastError': ''},
            },
        )
        if not result.acknowledged:
            raise RuntimeError('更新自定义消息投递状态失败')

    async def mark_failed(
            self,
            event: Mapping[str, Any],
            error: str,
    ) -> None:
        result = await self._collection.update_one(
            self._event_key_from_document(event),
            {
                '$set': {
                    'status': 'failed',
                    'lastError': error,
                    'updatedAt': datetime.now(timezone.utc),
                },
            },
        )
        if not result.acknowledged:
            raise RuntimeError('更新自定义消息失败状态失败')

    @staticmethod
    def _event_key(
            user_id: str,
            session_id: str,
            event_id: str,
    ) -> dict[str, str]:
        return {
            'userId': user_id,
            'sessionId': session_id,
            'eventId': event_id,
        }

    @classmethod
    def _event_key_from_document(
            cls,
            document: Mapping[str, Any],
    ) -> dict[str, str]:
        return cls._event_key(
            document['userId'],
            document['sessionId'],
            document['eventId'],
        )

    @staticmethod
    def _require_same_message(
            existing: Mapping[str, Any],
            message: Mapping[str, Any],
    ) -> None:
        if existing.get('message') != dict(message):
            raise ChatCustomMessageOutboxConflictError(
                'eventId 已存在，但消息内容不一致',
            )


def create_application_chat_custom_message_outbox() -> ChatCustomMessageOutbox:
    from db.mongodb import collection_chat_custom_message_outbox

    return ChatCustomMessageOutbox(collection_chat_custom_message_outbox)


__all__ = [
    'ChatCustomMessageOutbox',
    'ChatCustomMessageOutboxConflictError',
    'create_application_chat_custom_message_outbox',
]
