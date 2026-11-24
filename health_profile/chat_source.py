"""普通聊天用户消息的画像摄取任务投递。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Callable
from typing import Protocol
from typing import runtime_checkable

from pymongo import ASCENDING

from utils.log import getLogger
from utils.log.context import task_log_context

from .ingestion import HealthProfileJobQueue
from .models import ProfileIngestionSourceType

logger = getLogger(__name__)

_CHINA_TIMEZONE = timezone(timedelta(hours=8))


@dataclass(frozen=True, slots=True)
class ChatProfileSourceRecord:
    """可供画像处理器读取的单条用户消息快照。"""

    user_id: str
    session_id: str
    message_id: str
    source_revision: int
    ingestion_status: str
    ingestion_job_id: str | None
    content: str
    created_at: datetime
    delivery_mode: str | None = None


@dataclass(frozen=True, slots=True)
class ChatProfileDeliveryResult:
    """一次幂等投递的结果。"""

    record: ChatProfileSourceRecord
    ingestion_queued: bool


@dataclass(frozen=True, slots=True)
class ChatProfilePendingReference:
    user_id: str
    message_id: str


@runtime_checkable
class ChatProfileStore(Protocol):
    async def get_user_message(
        self,
        *,
        user_id: str,
        message_id: str,
    ) -> ChatProfileSourceRecord | None: ...

    async def mark_ingestion_queued(
        self,
        *,
        user_id: str,
        session_id: str,
        message_id: str,
        job_id: str,
        updated_at: datetime,
    ) -> ChatProfileSourceRecord | None: ...


@runtime_checkable
class ChatProfilePendingStore(Protocol):
    async def list_pending(
        self,
        *,
        limit: int,
    ) -> tuple[ChatProfilePendingReference, ...]: ...


class MongoChatProfileStore:
    """从业务会话中读取带稳定 ID 的单条用户消息。"""

    def __init__(self, collection) -> None:
        self._collection = collection
        self._indexes_ready = False
        self._index_lock = asyncio.Lock()

    async def ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        async with self._index_lock:
            if self._indexes_ready:
                return
            await self._collection.create_index(
                [('userId', ASCENDING), ('messages.messageId', ASCENDING)],
                name='session_chat_message_lookup',
            )
            await self._collection.create_index(
                [
                    ('chatProfileIngestion.status', ASCENDING),
                    ('chatProfileIngestion.updatedAt', ASCENDING),
                ],
                name='session_chat_profile_ingestion_scan',
            )
            self._indexes_ready = True

    async def get_user_message(
        self,
        *,
        user_id: str,
        message_id: str,
    ) -> ChatProfileSourceRecord | None:
        await self.ensure_indexes()
        document = await self._collection.find_one(
            {
                'userId': _required_text('user_id', user_id),
                'messages': {
                    '$elemMatch': {
                        'messageId': _required_text('message_id', message_id),
                        'role': 'user',
                    },
                },
            }
        )
        if document is None:
            return None
        return _record_from_session(document, message_id=message_id)

    async def mark_ingestion_queued(
        self,
        *,
        user_id: str,
        session_id: str,
        message_id: str,
        job_id: str,
        updated_at: datetime,
    ) -> ChatProfileSourceRecord | None:
        await self._collection.update_one(
            {
                'userId': _required_text('user_id', user_id),
                'sessionId': _required_text('session_id', session_id),
                'chatProfileIngestion': {
                    '$elemMatch': {
                        'messageId': _required_text(
                            'message_id',
                            message_id,
                        ),
                    },
                },
            },
            {
                '$set': {
                    'chatProfileIngestion.$.status': 'queued',
                    'chatProfileIngestion.$.jobId': _required_text(
                        'job_id',
                        job_id,
                    ),
                    'chatProfileIngestion.$.updatedAt': updated_at,
                },
            },
        )
        return await self.get_user_message(
            user_id=user_id,
            message_id=message_id,
        )

    async def mark_source_managed(self, record, updated_at):
        """CAS-retire legacy pending intent without claiming a job was queued."""
        await self._collection.update_one(
            {
                'userId': record.user_id,
                'sessionId': record.session_id,
                'chatProfileIngestion': {
                    '$elemMatch': {
                        'messageId': record.message_id,
                        'sourceRevision': record.source_revision,
                        'status': 'pending',
                    }
                },
            },
            {
                '$set': {
                    'chatProfileIngestion.$.status': 'source_managed',
                    'chatProfileIngestion.$.reasonCode': 'source.managed_by_publication',
                    'chatProfileIngestion.$.updatedAt': updated_at,
                }
            },
        )
        return await self.get_user_message(user_id=record.user_id, message_id=record.message_id)

    async def list_pending(
        self,
        *,
        limit: int,
    ) -> tuple[ChatProfilePendingReference, ...]:
        """按来源状态扫描尚未成功投递的用户消息。"""
        limit = _recovery_limit(limit)
        await self.ensure_indexes()
        cursor = self._collection.find(
            {
                'type': 'chat',
                'chatProfileIngestion': {
                    '$elemMatch': {'status': 'pending', 'deliveryMode': {'$in': [None, 'legacy']}},
                },
            },
            {
                'userId': 1,
                'sessionId': 1,
                'chatProfileIngestion': 1,
            },
        ).sort(
            [
                ('chatProfileIngestion.updatedAt', ASCENDING),
                ('sessionId', ASCENDING),
            ]
        )
        documents = await cursor.to_list(length=limit)
        references = []
        for document in documents:
            session_id = document.get('sessionId')
            states = document.get('chatProfileIngestion')
            if not isinstance(states, list):
                logger.warning(
                    f'聊天画像 pending 状态无法扫描: session_id={session_id}, reason_code=invalid_ingestion_state',
                )
                continue
            for state in states:
                if (
                    not isinstance(state, dict)
                    or state.get('status') != 'pending'
                    or state.get('deliveryMode') not in (None, 'legacy')
                ):
                    continue
                try:
                    reference = ChatProfilePendingReference(
                        user_id=_required_text(
                            'user_id',
                            document.get('userId'),
                        ),
                        message_id=_required_text(
                            'message_id',
                            state.get('messageId'),
                        ),
                    )
                except (TypeError, ValueError) as error:
                    logger.warning(
                        '聊天画像 pending 状态无法扫描: '
                        f'session_id={session_id}, '
                        f'error_type={type(error).__name__}, '
                        'reason_code=invalid_pending_reference',
                    )
                    continue
                references.append(reference)
                if len(references) >= limit:
                    return tuple(references)
        return tuple(references)


class ChatProfileSourceService:
    """随会话原子保存用户消息，再异步投递画像任务。"""

    SOURCE_REVISION = 1

    def __init__(
        self,
        store: ChatProfileStore,
        job_queue: HealthProfileJobQueue,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(store, ChatProfileStore):
            raise TypeError('store must implement ChatProfileStore')
        if not isinstance(job_queue, HealthProfileJobQueue):
            raise TypeError('job_queue must be a HealthProfileJobQueue')
        self._store = store
        self._job_queue = job_queue
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def prepare_user_message(
        self,
        session: dict[str, Any],
        message: dict[str, Any],
    ) -> None:
        """在整轮会话落库前加入可补偿的 pending 投递状态。"""
        if not isinstance(session, dict) or not isinstance(message, dict):
            raise TypeError('session and message must be dictionaries')
        _required_text('user_id', session.get('userId'))
        _required_text('session_id', session.get('sessionId'))
        if message.get('role') != 'user':
            raise ValueError('only user messages can enter profile ingestion')
        message_id = _required_text('message_id', message.get('messageId'))
        _required_text('content', message.get('content'))
        revision = message.get('messageRevision')
        if revision != self.SOURCE_REVISION:
            raise ValueError('chat message revision is unsupported')
        states = session.setdefault('chatProfileIngestion', [])
        if not isinstance(states, list):
            raise ValueError('chatProfileIngestion must be a list')
        existing = next(
            (item for item in states if isinstance(item, dict) and item.get('messageId') == message_id), None
        )
        if existing is not None:
            if existing.get('sourceRevision') != revision:
                raise ValueError('messageId already uses another revision')
            return
        states.append(
            {
                'messageId': message_id,
                'sourceRevision': revision,
                'status': 'pending',
                'jobId': None,
                'updatedAt': self._now(),
            }
        )

    @task_log_context(task='chat-profile-delivery')
    async def ensure_queued(
        self,
        *,
        user_id: str,
        message_id: str,
    ) -> ChatProfileDeliveryResult | None:
        """幂等投递；异常时保留消息对应的 pending 状态。"""
        try:
            record = await self._store.get_user_message(
                user_id=user_id,
                message_id=message_id,
            )
            if record is None:
                return None
            if record.delivery_mode not in (None, 'legacy'):
                # New/unknown owner modes never silently enter the legacy queue,
                # even before a public admission reservation is acknowledged.
                return ChatProfileDeliveryResult(record=record, ingestion_queued=False)
            if record.ingestion_status in {'pending', 'source_managed'} and (
                await self._job_queue.is_source_publication_managed(
                    record.user_id,
                    None,
                    'chat',
                    record.message_id,
                )
            ):
                if record.ingestion_status == 'pending':
                    marker = getattr(self._store, 'mark_source_managed', None)
                    if marker is None:
                        raise RuntimeError('source store cannot retire managed legacy delivery')
                    record = await marker(record, self._now())
                return ChatProfileDeliveryResult(record=record, ingestion_queued=False)
            if record.ingestion_status == 'queued':
                return ChatProfileDeliveryResult(
                    record=record,
                    ingestion_queued=True,
                )
            enqueue = await self._job_queue.enqueue(
                source_type=ProfileIngestionSourceType.CHAT,
                source_id=record.message_id,
                source_revision=record.source_revision,
                user_id=record.user_id,
                subject_id=None,
                idempotency_key=(f'chat:{record.session_id}:{record.message_id}:{record.source_revision}'),
            )
            queued = await self._store.mark_ingestion_queued(
                user_id=record.user_id,
                session_id=record.session_id,
                message_id=record.message_id,
                job_id=enqueue.job.job_id,
                updated_at=self._now(),
            )
            if queued is None:
                raise RuntimeError('已投递画像任务，但找不到用户消息快照')
            if queued.ingestion_status != 'queued' or queued.ingestion_job_id != enqueue.job.job_id:
                raise RuntimeError('用户消息的画像任务状态写回失败')
            return ChatProfileDeliveryResult(
                record=queued,
                ingestion_queued=True,
            )
        except Exception as error:
            logger.error(
                '投递聊天消息画像摄取任务失败: '
                f'user_id={user_id}, message_id={message_id}, '
                f'error_type={type(error).__name__}',
            )
            return None

    @task_log_context(clear=True)
    async def recover_pending(self, *, limit: int = 100) -> int:
        """补偿已保存但尚未成功投递的用户消息。"""
        if not isinstance(self._store, ChatProfilePendingStore):
            raise TypeError(
                'store must implement ChatProfilePendingStore for recovery',
            )
        references = await self._store.list_pending(
            limit=_recovery_limit(limit),
        )
        recovered = 0
        for reference in references:
            result = await self.ensure_queued(
                user_id=reference.user_id,
                message_id=reference.message_id,
            )
            if result is not None and result.ingestion_queued:
                recovered += 1
        return recovered

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('clock must return a timezone-aware datetime')
        return now


def _record_from_session(
    document: dict[str, Any],
    *,
    message_id: str,
) -> ChatProfileSourceRecord | None:
    if document.get('type') != 'chat':
        return None
    message = next(
        (
            item
            for item in document.get('messages', ())
            if (isinstance(item, dict) and item.get('messageId') == message_id and item.get('role') == 'user')
        ),
        None,
    )
    if message is None:
        return None
    state = next(
        (
            item
            for item in document.get('chatProfileIngestion', ())
            if isinstance(item, dict) and item.get('messageId') == message_id
        ),
        None,
    )
    if state is None:
        return None
    revision = state.get('sourceRevision')
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError('chat source revision must be a positive integer')
    return ChatProfileSourceRecord(
        user_id=_required_text('user_id', document.get('userId')),
        session_id=_required_text('session_id', document.get('sessionId')),
        message_id=_required_text('message_id', message_id),
        source_revision=revision,
        ingestion_status=state.get('status', 'pending'),
        ingestion_job_id=state.get('jobId'),
        content=_required_text('content', message.get('content')),
        created_at=_message_time(message.get('timestamp')),
        delivery_mode=deepcopy(state.get('deliveryMode')),
    )


def _message_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        normalized = value.strip()
        if normalized.endswith('Z'):
            normalized = f'{normalized[:-1]}+00:00'
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as error:
            raise ValueError('user message timestamp is invalid') from error
    else:
        raise ValueError('user message timestamp is required')
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # 业务服务的既有聊天时间是无时区的中国本地时间；显式指定时区，
        # 不依赖开发 Windows 或生产 Linux 的系统时区配置。
        return parsed.replace(tzinfo=_CHINA_TIMEZONE)
    return parsed


def _required_text(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be a string')
    normalized = value.strip()
    if not normalized:
        raise ValueError(f'{name} cannot be empty')
    return normalized


def _recovery_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError('limit must be an integer')
    if value < 1 or value > 1000:
        raise ValueError('limit must be between 1 and 1000')
    return value
