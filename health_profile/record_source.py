"""已完成建档问答的画像摄取任务投递。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
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


@dataclass(frozen=True, slots=True)
class RecordProfileSourceRecord:
    """可供后续处理器读取的已完成建档快照。"""

    user_id: str
    session_id: str
    round_number: int
    source_revision: int
    ingestion_status: str
    ingestion_job_id: str | None
    session: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RecordProfileDeliveryResult:
    """一次幂等投递的结果。"""

    record: RecordProfileSourceRecord
    ingestion_queued: bool


@dataclass(frozen=True, slots=True)
class RecordProfilePendingReference:
    user_id: str
    session_id: str


@runtime_checkable
class RecordProfileStore(Protocol):
    async def get_completed(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> RecordProfileSourceRecord | None: ...

    async def mark_ingestion_queued(
        self,
        *,
        user_id: str,
        session_id: str,
        job_id: str,
        updated_at: datetime,
    ) -> RecordProfileSourceRecord | None: ...


@runtime_checkable
class RecordProfilePendingStore(Protocol):
    async def list_pending(
        self,
        *,
        limit: int,
    ) -> tuple[RecordProfilePendingReference, ...]: ...


class MongoRecordProfileStore:
    """从业务会话集合读取稳定的已完成建档快照。"""

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
                [
                    ('qaProfileIngestion.status', ASCENDING),
                    ('qaProfileIngestion.updatedAt', ASCENDING),
                ],
                name='session_qa_profile_ingestion_scan',
            )
            self._indexes_ready = True

    async def get_completed(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> RecordProfileSourceRecord | None:
        await self.ensure_indexes()
        document = await self._collection.find_one(
            {
                'userId': _required_text('user_id', user_id),
                'sessionId': _required_text('session_id', session_id),
            }
        )
        if document is None:
            return None
        return _record_from_session(document)

    async def mark_ingestion_queued(
        self,
        *,
        user_id: str,
        session_id: str,
        job_id: str,
        updated_at: datetime,
    ) -> RecordProfileSourceRecord | None:
        await self._collection.update_one(
            {
                'userId': _required_text('user_id', user_id),
                'sessionId': _required_text('session_id', session_id),
            },
            {
                '$set': {
                    'qaProfileIngestion.status': 'queued',
                    'qaProfileIngestion.jobId': _required_text(
                        'job_id',
                        job_id,
                    ),
                    'qaProfileIngestion.updatedAt': updated_at,
                },
            },
        )
        return await self.get_completed(
            user_id=user_id,
            session_id=session_id,
        )

    async def mark_source_managed(self, record, updated_at):
        """CAS-retire legacy pending intent without claiming a job was queued."""
        await self._collection.update_one(
            {
                'userId': record.user_id,
                'sessionId': record.session_id,
                'qaProfileIngestion.status': 'pending',
                'qaSourceRevision': record.session.get('qaSourceRevision'),
            },
            {
                '$set': {
                    'qaProfileIngestion.status': 'source_managed',
                    'qaProfileIngestion.reasonCode': 'source.managed_by_publication',
                    'qaProfileIngestion.updatedAt': updated_at,
                }
            },
        )
        return await self.get_completed(user_id=record.user_id, session_id=record.session_id)

    async def list_pending(
        self,
        *,
        limit: int,
    ) -> tuple[RecordProfilePendingReference, ...]:
        """扫描已完成但尚未成功投递画像任务的建档快照。"""
        limit = _recovery_limit(limit)
        await self.ensure_indexes()
        cursor = self._collection.find(
            {
                'qaProfileIngestion.status': 'pending',
                'qaProfileIngestion.deliveryMode': {'$in': [None, 'legacy']},
                '$or': [
                    {'type': 'qa', 'status': 'completed'},
                    {
                        'type': {'$exists': False},
                        'status': 'completed',
                    },
                    {
                        'type': 'chat',
                        'qaCompletedAt': {'$exists': True, '$ne': None},
                    },
                ],
            },
            {'userId': 1, 'sessionId': 1},
        ).sort(
            [
                ('qaProfileIngestion.updatedAt', ASCENDING),
                ('sessionId', ASCENDING),
            ]
        )
        documents = await cursor.to_list(length=limit)
        references = []
        for document in documents:
            try:
                references.append(
                    RecordProfilePendingReference(
                        user_id=_required_text(
                            'user_id',
                            document.get('userId'),
                        ),
                        session_id=_required_text(
                            'session_id',
                            document.get('sessionId'),
                        ),
                    )
                )
            except (TypeError, ValueError) as error:
                logger.warning(
                    '建档画像 pending 状态无法扫描: '
                    f'error_type={type(error).__name__}, '
                    'reason_code=invalid_pending_reference',
                )
        return tuple(references)


class RecordProfileSourceService:
    """先保存完成快照，再幂等投递仅含稳定引用的画像任务。"""

    SOURCE_REVISION = 1

    def __init__(
        self,
        store: RecordProfileStore,
        job_queue: HealthProfileJobQueue,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(store, RecordProfileStore):
            raise TypeError('store must implement RecordProfileStore')
        if not isinstance(job_queue, HealthProfileJobQueue):
            raise TypeError('job_queue must be a HealthProfileJobQueue')
        self._store = store
        self._job_queue = job_queue
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def prepare_completed_snapshot(self, session: dict[str, Any]) -> None:
        """在完成快照首次落库前写入可补偿的 pending 标记。"""
        if not isinstance(session, dict):
            raise TypeError('session must be a dict')
        if session.get('type', 'qa') != 'qa':
            raise ValueError('only qa sessions can be prepared')
        if session.get('status') != 'completed':
            raise ValueError('only completed qa sessions can be prepared')
        _required_text('user_id', session.get('userId'))
        _required_text('session_id', session.get('sessionId'))
        ingestion = session.get('qaProfileIngestion')
        mode = ingestion.get('deliveryMode') if isinstance(ingestion, dict) else None
        if mode == 'profile-source-event.v1':
            return
        if mode not in (None, 'legacy'):
            raise ValueError('record profile delivery mode is unsupported')
        session['qaSourceRevision'] = self.SOURCE_REVISION
        if isinstance(ingestion, dict) and ingestion.get('status') == 'queued':
            return
        session['qaProfileIngestion'] = {
            'status': 'pending',
            'jobId': None,
            'updatedAt': self._now(),
        }

    @task_log_context(task='record-profile-delivery')
    async def ensure_queued(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> RecordProfileDeliveryResult | None:
        """幂等投递；失败时保留来源快照的 pending 状态。"""
        try:
            record = await self._store.get_completed(
                user_id=user_id,
                session_id=session_id,
            )
            if record is None:
                return None
            ingestion = record.session.get('qaProfileIngestion')
            mode = ingestion.get('deliveryMode') if isinstance(ingestion, dict) else None
            if mode not in (None, 'legacy'):
                # A source-owner delivery mode is not a quiescence proof. It
                # only prevents this legacy sender from admitting a new job.
                return RecordProfileDeliveryResult(record=record, ingestion_queued=False)
            if record.ingestion_status in {'pending', 'source_managed'} and (
                await self._job_queue.is_source_publication_managed(
                    record.user_id,
                    None,
                    'record',
                    record.session_id,
                )
            ):
                if record.ingestion_status == 'pending':
                    marker = getattr(self._store, 'mark_source_managed', None)
                    if marker is None:
                        raise RuntimeError('source store cannot retire managed legacy delivery')
                    record = await marker(record, self._now())
                return RecordProfileDeliveryResult(record=record, ingestion_queued=False)
            if record.ingestion_status == 'queued':
                return RecordProfileDeliveryResult(
                    record=record,
                    ingestion_queued=True,
                )
            enqueue = await self._job_queue.enqueue(
                source_type=ProfileIngestionSourceType.RECORD,
                source_id=record.session_id,
                source_revision=record.source_revision,
                user_id=record.user_id,
                # 建档处理器下一阶段负责创建本人主体，并解析家属主体。
                subject_id=None,
                idempotency_key=(f'record:{record.session_id}:{record.round_number}:{record.source_revision}'),
            )
            queued = await self._store.mark_ingestion_queued(
                user_id=record.user_id,
                session_id=record.session_id,
                job_id=enqueue.job.job_id,
                updated_at=self._now(),
            )
            if queued is None:
                raise RuntimeError('已投递画像任务，但找不到已完成建档快照')
            if queued.ingestion_status != 'queued' or queued.ingestion_job_id != enqueue.job.job_id:
                raise RuntimeError('建档快照的画像任务状态写回失败')
            return RecordProfileDeliveryResult(
                record=queued,
                ingestion_queued=True,
            )
        except Exception as error:
            # 建档快照是来源真相，画像是异步派生数据。投递异常不能让已经
            # 完成的建档回滚；pending 标记由后续补偿扫描器重新处理。
            logger.error(
                '投递建档问答画像摄取任务失败: '
                f'user_id={user_id}, session_id={session_id}, '
                f'error_type={type(error).__name__}',
            )
            return None

    @task_log_context(clear=True)
    async def recover_pending(self, *, limit: int = 100) -> int:
        """补偿已保存但尚未成功投递的建档快照。"""
        if not isinstance(self._store, RecordProfilePendingStore):
            raise TypeError(
                'store must implement RecordProfilePendingStore for recovery',
            )
        references = await self._store.list_pending(
            limit=_recovery_limit(limit),
        )
        recovered = 0
        for reference in references:
            result = await self.ensure_queued(
                user_id=reference.user_id,
                session_id=reference.session_id,
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
) -> RecordProfileSourceRecord | None:
    session_type = document.get('type', 'qa')
    if session_type == 'qa':
        if document.get('status') != 'completed':
            return None
        round_number = document.get('round', 1)
    elif session_type == 'chat':
        if not document.get('qaCompletedAt'):
            return None
        round_number = document.get('qaRound', 1)
    else:
        return None
    if isinstance(round_number, bool) or not isinstance(round_number, int) or round_number < 1:
        raise ValueError('completed record round must be a positive integer')
    ingestion = document.get('qaProfileIngestion')
    if not isinstance(ingestion, dict):
        ingestion = {}
    revision = document.get(
        'qaSourceRevision',
        RecordProfileSourceService.SOURCE_REVISION,
    )
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError('qa source revision must be a positive integer')
    return RecordProfileSourceRecord(
        user_id=_required_text('user_id', document.get('userId')),
        session_id=_required_text('session_id', document.get('sessionId')),
        round_number=round_number,
        source_revision=revision,
        ingestion_status=ingestion.get('status', 'pending'),
        ingestion_job_id=ingestion.get('jobId'),
        session=deepcopy(document),
    )


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
