"""体检报告来源持久化与画像摄取任务投递。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Protocol
from typing import runtime_checkable
from uuid import uuid4

from pymongo import ASCENDING
from pymongo import DESCENDING
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from utils.log import getLogger
from utils.log.context import task_log_context
from utils.report_content import report_public_content
from utils.report_content import report_source_content

from .ingestion import HealthProfileJobQueue
from .models import ProfileIngestionSourceType

logger = getLogger(__name__)


class HealthReportSourceConflictError(RuntimeError):
    """同一个 reportId 被不同的报告来源重复使用。"""


@dataclass(frozen=True, slots=True)
class HealthReportSourceRecord:
    report_id: str
    user_id: str
    subject_id: str | None
    source_revision: int
    source_fingerprint: str
    result: dict[str, Any]
    ingestion_status: str
    ingestion_job_id: str | None
    created_at: datetime
    updated_at: datetime
    document_id: str | None = None
    canonical_report_id: str | None = None

    def public_result(self) -> dict[str, Any]:
        result = report_public_content(self.result)
        result.pop('ingestionObservations', None)
        result['reportId'] = self.report_id
        result['userId'] = self.user_id
        result['subjectId'] = self.subject_id
        if 'processing' in result or self.source_revision > 1:
            result['sourceRevision'] = self.source_revision
        return result


@dataclass(frozen=True, slots=True)
class HealthReportSaveResult:
    record: HealthReportSourceRecord
    created: bool
    ingestion_queued: bool


@dataclass(frozen=True, slots=True)
class HealthReportPendingReference:
    report_id: str
    source_revision: int = 1


@runtime_checkable
class HealthReportStore(Protocol):
    async def get_report(self, report_id: str) -> HealthReportSourceRecord | None: ...

    async def create_report(
        self,
        record: HealthReportSourceRecord,
    ) -> tuple[HealthReportSourceRecord, bool]: ...

    async def mark_ingestion_queued(
        self,
        *,
        report_id: str,
        job_id: str,
        updated_at: datetime,
    ) -> HealthReportSourceRecord | None: ...


@runtime_checkable
class HealthReportPendingStore(Protocol):
    async def list_pending(
        self,
        *,
        limit: int,
    ) -> tuple[HealthReportPendingReference, ...]: ...


class MongoHealthReportStore:
    """在现有 health_reports 集合中追加保存每一份报告。"""

    def __init__(self, collection, revisions=None) -> None:
        self._collection = collection
        self._revisions = revisions
        self._indexes_ready = False
        self._index_lock = asyncio.Lock()

    async def ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        async with self._index_lock:
            if self._indexes_ready:
                return
            await self._collection.create_index(
                [('reportId', ASCENDING)],
                unique=True,
                partialFilterExpression={'reportId': {'$type': 'string'}},
                name='health_report_id_unique',
            )
            await self._collection.create_index(
                [('userId', ASCENDING), ('createdAt', DESCENDING)],
                name='health_report_user_created',
            )
            await self._collection.create_index(
                [
                    ('userId', ASCENDING),
                    ('subjectId', ASCENDING),
                    ('createdAt', DESCENDING),
                ],
                name='health_report_subject_created',
            )
            await self._collection.create_index(
                [
                    ('profileIngestion.status', ASCENDING),
                    ('profileIngestion.updatedAt', ASCENDING),
                ],
                partialFilterExpression={
                    'reportId': {'$type': 'string'},
                },
                name='health_report_profile_ingestion_scan',
            )
            self._indexes_ready = True

    async def get_revision(self, report_id, revision, *, source_key=None):
        if revision == 1:
            return await self.get_report(report_id)
        if self._revisions is None:
            return None
        query = {'reportId': report_id, 'sourceRevision': revision}
        if source_key is not None:
            query['sourceKey'] = source_key
        document = await self._revisions.find_one(query)
        return None if document is None else _record_from_document(document)

    async def create_revision(self, record, source_key):
        if self._revisions is None:
            raise RuntimeError('报告来源版本存储未配置')
        await self._revisions.create_index(
            [('reportId', ASCENDING), ('sourceKey', ASCENDING)],
            unique=True,
            name='report_source_key_unique',
        )
        await self._revisions.create_index(
            [('profileIngestion.status', ASCENDING), ('createdAt', ASCENDING)],
            name='report_revision_pending',
        )
        document = _record_to_document(record)
        document['_id'] = f'{record.report_id}:{record.source_revision}'
        document['sourceKey'] = source_key
        try:
            saved = await self._revisions.insert_one(document)
            if not saved.acknowledged:
                raise RuntimeError('报告来源版本保存失败')
            return record, True
        except DuplicateKeyError:
            existing = await self._revisions.find_one({'_id': document['_id']})
            if existing is None or existing['sourceKey'] != source_key:
                raise HealthReportSourceConflictError('来源版本标识冲突')
            return _record_from_document(existing), False

    async def mark_revision_queued(self, record, job_id, updated_at):
        result = await self._revisions.update_one(
            {'reportId': record.report_id, 'sourceRevision': record.source_revision},
            {
                '$set': {
                    'profileIngestion.status': 'queued',
                    'profileIngestion.jobId': job_id,
                    'profileIngestion.updatedAt': updated_at,
                    'updatedAt': updated_at,
                }
            },
        )
        if not result.acknowledged or result.matched_count != 1:
            raise RuntimeError('报告来源版本投递状态保存失败')
        return await self.get_revision(record.report_id, record.source_revision)

    async def get_report(self, report_id: str) -> HealthReportSourceRecord | None:
        document = await self._collection.find_one(
            {
                'reportId': _required_text('report_id', report_id),
            }
        )
        return None if document is None else _record_from_document(document)

    async def create_report(
        self,
        record: HealthReportSourceRecord,
    ) -> tuple[HealthReportSourceRecord, bool]:
        if not isinstance(record, HealthReportSourceRecord):
            raise TypeError('record must be a HealthReportSourceRecord')
        await self.ensure_indexes()
        try:
            result = await self._collection.insert_one(
                _record_to_document(record),
            )
        except DuplicateKeyError:
            existing = await self.get_report(record.report_id)
            if existing is None:
                raise
            return existing, False
        if not result.acknowledged:
            raise RuntimeError('保存体检报告数据失败')
        return record, True

    async def mark_ingestion_queued(
        self,
        *,
        report_id: str,
        job_id: str,
        updated_at: datetime,
    ) -> HealthReportSourceRecord | None:
        await self._collection.update_one(
            {'reportId': _required_text('report_id', report_id)},
            {
                '$set': {
                    'profileIngestion.status': 'queued',
                    'profileIngestion.jobId': _required_text(
                        'job_id',
                        job_id,
                    ),
                    'profileIngestion.updatedAt': updated_at,
                    'updatedAt': updated_at,
                },
            },
        )
        return await self.get_report(report_id)

    async def mark_source_managed(self, record, updated_at):
        """Retire legacy pending delivery without claiming a job was queued."""
        collection = self._revisions if record.source_revision > 1 else self._collection
        if collection is None:
            raise RuntimeError('historical report revision storage is unavailable')
        query = {
            'reportId': record.report_id,
            'userId': record.user_id,
            'subjectId': record.subject_id,
            'profileIngestion.status': 'pending',
        }
        if record.source_revision > 1:
            query['sourceRevision'] = record.source_revision
        document = await collection.find_one_and_update(
            query,
            {
                '$set': {
                    'profileIngestion.status': 'source_managed',
                    'profileIngestion.reasonCode': 'source.managed_by_publication',
                    'profileIngestion.updatedAt': updated_at,
                    'updatedAt': updated_at,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if document is not None:
            return _record_from_document(document)
        return await self.get_revision(record.report_id, record.source_revision)

    async def list_pending(
        self,
        *,
        limit: int,
    ) -> tuple[HealthReportPendingReference, ...]:
        """扫描已经保存但尚未成功投递画像任务的报告。"""
        limit = _recovery_limit(limit)
        await self.ensure_indexes()
        cursor = self._collection.find(
            {
                'profileIngestion.status': 'pending',
                'reportId': {'$type': 'string'},
                'subjectId': {'$type': 'string'},
            },
            {'reportId': 1},
        ).sort(
            [
                ('profileIngestion.updatedAt', ASCENDING),
                ('createdAt', ASCENDING),
                ('reportId', ASCENDING),
            ]
        )
        documents = await cursor.to_list(length=limit)
        references = []
        if self._revisions is not None:
            revisions = (
                await self._revisions.find({'profileIngestion.status': 'pending'})
                .sort([('createdAt', ASCENDING), ('_id', ASCENDING)])
                .to_list(length=limit)
            )
            references.extend(HealthReportPendingReference(row['reportId'], row['sourceRevision']) for row in revisions)
        for document in documents:
            try:
                references.append(
                    HealthReportPendingReference(
                        report_id=_report_id(document.get('reportId')),
                    )
                )
            except (TypeError, ValueError) as error:
                logger.warning(
                    '报告画像 pending 状态无法扫描: '
                    f'error_type={type(error).__name__}, '
                    'reason_code=invalid_pending_reference',
                )
        return tuple(references)


class HealthReportSourceService:
    """保证报告先可靠保存，再将稳定来源引用投递给画像队列。"""

    SOURCE_REVISION = 1

    def __init__(
        self,
        store: HealthReportStore,
        job_queue: HealthProfileJobQueue,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        observation_store=None,
    ) -> None:
        if not isinstance(store, HealthReportStore):
            raise TypeError('store must implement HealthReportStore')
        if not isinstance(job_queue, HealthProfileJobQueue):
            raise TypeError('job_queue must be a HealthProfileJobQueue')
        self._store = store
        self._job_queue = job_queue
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: f'report_{uuid4().hex}')
        self._observation_store = observation_store

    def resolve_report_id(self, requested: str | None) -> str:
        if requested is None:
            return _report_id(self._id_factory())
        return _report_id(requested)

    async def get_owned(self, report_id: str, user_id: str, subject_id: str):
        record = await self._store.get_report(_report_id(report_id))
        if record is not None and (record.user_id != user_id or record.subject_id != subject_id):
            raise HealthReportSourceConflictError('reportId 已被其他用户或健康主体使用')
        return record

    async def get_owned_revision(self, report_id, source_revision, *, user_id, subject_id):
        """Read an exact legacy report revision without queuing work or creating subjects."""
        report_id = _report_id(report_id)
        user_id = _required_text('user_id', user_id)
        subject_id = _required_text('subject_id', subject_id)
        if isinstance(source_revision, bool) or not isinstance(source_revision, int) or source_revision < 1:
            raise ValueError('source_revision must be a positive legacy report revision')
        if source_revision == 1:
            record = await self._store.get_report(report_id)
        else:
            getter = getattr(self._store, 'get_revision', None)
            if getter is None:
                raise RuntimeError('historical report revision storage is unavailable')
            record = await getter(report_id, source_revision)
        if record is not None and (record.user_id != user_id or record.subject_id != subject_id):
            raise HealthReportSourceConflictError('report revision is outside the requested scope')
        if record is not None and record.source_revision != source_revision:
            raise HealthReportSourceConflictError('report revision does not match the requested revision')
        return record

    @staticmethod
    def source_fingerprint(
        *,
        user_id: str,
        subject_id: str | None,
        file_id: str,
    ) -> str:
        payload = {
            'userId': _required_text('user_id', user_id),
            'subjectId': _optional_text(subject_id),
            'fileId': _required_text('file_id', file_id),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    async def get_existing(
        self,
        *,
        report_id: str,
        source_fingerprint: str,
        source_fingerprint_aliases: tuple[str, ...] = (),
    ) -> HealthReportSaveResult | None:
        existing = await self._store.get_report(_report_id(report_id))
        if existing is None:
            return None
        if existing.source_fingerprint not in source_fingerprint_aliases:
            self._ensure_same_source(existing, source_fingerprint)
        queued = await self._ensure_queued(existing)
        return HealthReportSaveResult(
            record=queued,
            created=False,
            ingestion_queued=queued.ingestion_status == 'queued',
        )

    async def save(
        self,
        *,
        report_id: str,
        user_id: str,
        subject_id: str | None,
        source_fingerprint: str,
        result: dict[str, Any],
        document_id: str | None = None,
        canonical_report_id: str | None = None,
    ) -> HealthReportSaveResult:
        if not isinstance(result, dict):
            raise TypeError('result must be a dict')
        now = self._now()
        normalized_subject_id = _optional_text(subject_id)
        requested = HealthReportSourceRecord(
            report_id=_report_id(report_id),
            user_id=_required_text('user_id', user_id),
            subject_id=normalized_subject_id,
            source_revision=self.SOURCE_REVISION,
            source_fingerprint=_required_text(
                'source_fingerprint',
                source_fingerprint,
            ),
            result=report_source_content(result),
            ingestion_status=('pending' if normalized_subject_id is not None else 'awaiting_subject'),
            ingestion_job_id=None,
            created_at=now,
            updated_at=now,
            document_id=_optional_text(document_id),
            canonical_report_id=None if canonical_report_id is None else _report_id(canonical_report_id),
        )
        # v6 source occurrences need the same stable pointer in the initial and
        # supplemental ingestion jobs. The public first snapshot stays immutable.
        if (
            self._observation_store is not None
            and (
                requested.result.get('sourceBindings')
                or (requested.result.get('processing') or {}).get('version') == 'analysis-health-report-v12'
            )
            and await self._store.get_report(requested.report_id) is None
        ):
            requested.result['ingestionObservations'] = await self._observation_store.capture(requested)
        stored, created = await self._store.create_report(requested)
        self._ensure_same_source(stored, requested.source_fingerprint)
        historical_base_without_projection = (
            isinstance(stored.result.get('verifiedObservationPointers'), list)
            and 'reportSource' not in stored.result
            and not isinstance(stored.result.get('ingestionObservations'), list)
        )
        if self._observation_store is not None and (
            'verifiedObservationPointers' not in stored.result or historical_base_without_projection
        ):
            # Reserve legacy-admitted rows for the original revision before a
            # subsequent supplemental revision captures the omitted rows.
            await self._observation_store.capture(stored)
        queued = await self._ensure_queued(stored)
        return HealthReportSaveResult(
            record=queued,
            created=created,
            ingestion_queued=queued.ingestion_status == 'queued',
        )

    async def save_revision(self, *, canonical_report_id, user_id, subject_id, source_key, result):
        """Keep the original public result intact; supplemental facts use an exact immutable revision."""
        canonical = await self.get_owned(canonical_report_id, user_id, subject_id)
        if canonical is None or canonical.canonical_report_id != canonical.report_id:
            raise HealthReportSourceConflictError('补充结果缺少规范文件来源')
        # Opaque, JS-safe integer, not an ordering key. A digest collision is checked at insert.
        revision = 2 + int(hashlib.sha256(source_key.encode('utf-8')).hexdigest()[:12], 16)
        existing = await self._store.get_revision(canonical_report_id, revision, source_key=source_key)
        if existing is None:
            requested = replace(
                canonical,
                source_revision=revision,
                result=report_source_content(result),
                ingestion_status='pending',
                ingestion_job_id=None,
                created_at=self._now(),
                updated_at=self._now(),
            )
            if self._observation_store is None:
                raise RuntimeError('补充结果需要原报告指标存储')
            owned = await self._observation_store.capture(requested)
            requested.result['ingestionObservations'] = owned
            existing, created = await self._store.create_revision(requested, source_key)
        else:
            created = False
        queued = await self._ensure_queued(existing)
        return HealthReportSaveResult(queued, created, queued.ingestion_status == 'queued')

    @task_log_context(job='record', task='report-profile-delivery')
    async def _ensure_queued(
        self,
        record: HealthReportSourceRecord,
    ) -> HealthReportSourceRecord:
        if record.subject_id is not None and record.ingestion_status in {'pending', 'source_managed'}:
            source_id = record.canonical_report_id or record.report_id
            if await self._job_queue.is_source_publication_managed(
                record.user_id,
                record.subject_id,
                'health_report',
                source_id,
            ):
                if record.ingestion_status == 'source_managed':
                    return record
                marker = getattr(self._store, 'mark_source_managed', None)
                if marker is None:
                    raise RuntimeError('report store does not support publication cutover recovery')
                return await marker(record, self._now()) or record
        # 主体未知的报告仍是有效的解读结果，但不能默认污染本人画像。
        # 这类来源等待调用方明确主体，不创建注定失败的摄取任务。
        if record.canonical_report_id and record.canonical_report_id != record.report_id:
            canonical = await self.get_owned(record.canonical_report_id, record.user_id, record.subject_id)
            if canonical is None or canonical.canonical_report_id != canonical.report_id:
                raise HealthReportSourceConflictError('文件规范来源不存在或映射无效')
            self._ensure_same_source(canonical, record.source_fingerprint)
            if canonical.document_id != record.document_id:
                raise HealthReportSourceConflictError('文件规范来源引用不一致')
            canonical = await self._ensure_queued(canonical)
            if canonical.ingestion_status == 'queued':
                return (
                    await self._store.mark_ingestion_queued(
                        report_id=record.report_id,
                        job_id=canonical.ingestion_job_id,
                        updated_at=self._now(),
                    )
                    or record
                )
            return record
        if record.subject_id is None:
            return record
        if record.ingestion_status == 'queued':
            return record
        try:
            enqueue = await self._job_queue.enqueue(
                source_type=ProfileIngestionSourceType.HEALTH_REPORT,
                source_id=record.report_id,
                source_revision=record.source_revision,
                user_id=record.user_id,
                subject_id=record.subject_id,
                idempotency_key=(f'report:{record.report_id}:{record.source_revision}'),
            )
            if record.source_revision > 1:
                queued = await self._store.mark_revision_queued(record, enqueue.job.job_id, self._now())
            else:
                queued = await self._store.mark_ingestion_queued(
                    report_id=record.report_id,
                    job_id=enqueue.job.job_id,
                    updated_at=self._now(),
                )
            if queued is None:
                raise RuntimeError('已投递画像任务，但找不到对应体检报告')
            return queued
        except Exception as error:
            # 报告是来源真相，画像摄取属于异步派生任务。投递异常不能删除或
            # 覆盖报告；pending 状态留给后续补偿扫描器再次投递。
            logger.error(
                f'投递体检报告画像摄取任务失败: report_id={record.report_id}, error={error}',
            )
            return record

    @task_log_context(clear=True)
    async def recover_pending(self, *, limit: int = 100) -> int:
        """补偿已经保存但尚未成功投递的报告。"""
        if not isinstance(self._store, HealthReportPendingStore):
            raise TypeError(
                'store must implement HealthReportPendingStore for recovery',
            )
        references = await self._store.list_pending(
            limit=_recovery_limit(limit),
        )
        recovered = 0
        for reference in references:
            record = (
                await self._store.get_revision(reference.report_id, reference.source_revision)
                if reference.source_revision > 1
                else await self._store.get_report(reference.report_id)
            )
            if record is None:
                logger.warning(
                    '报告画像 pending 来源不存在: '
                    f'report_id={reference.report_id}, '
                    'reason_code=pending_source_not_found',
                )
                continue
            queued = await self._ensure_queued(record)
            if queued.ingestion_status == 'queued':
                recovered += 1
        return recovered

    @staticmethod
    def _ensure_same_source(
        record: HealthReportSourceRecord,
        fingerprint: str,
    ) -> None:
        if record.source_fingerprint != fingerprint:
            raise HealthReportSourceConflictError(
                'reportId 已被另一份体检报告使用',
            )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('clock must return a timezone-aware datetime')
        return now


def _record_to_document(record: HealthReportSourceRecord) -> dict[str, Any]:
    result = deepcopy(record.result)
    result.update(
        {
            '_id': record.report_id,
            'reportId': record.report_id,
            'userId': record.user_id,
            'subjectId': record.subject_id,
            'sourceRevision': record.source_revision,
            'sourceFingerprint': record.source_fingerprint,
            'documentId': record.document_id,
            'canonicalReportId': record.canonical_report_id,
            'profileIngestion': {
                'status': record.ingestion_status,
                'jobId': record.ingestion_job_id,
                'updatedAt': record.updated_at,
            },
            'createdAt': record.created_at,
            'updatedAt': record.updated_at,
        }
    )
    return result


def _record_from_document(document: dict[str, Any]) -> HealthReportSourceRecord:
    ingestion = document.get('profileIngestion')
    if not isinstance(ingestion, dict):
        ingestion = {}
    internal_fields = {
        '_id',
        'reportId',
        'userId',
        'subjectId',
        'sourceRevision',
        'sourceFingerprint',
        'documentId',
        'canonicalReportId',
        'profileIngestion',
        'createdAt',
        'updatedAt',
        'sourceKey',
    }
    return HealthReportSourceRecord(
        report_id=document['reportId'],
        user_id=document['userId'],
        subject_id=document.get('subjectId'),
        source_revision=document.get('sourceRevision', 1),
        source_fingerprint=document['sourceFingerprint'],
        result={key: deepcopy(value) for key, value in document.items() if key not in internal_fields},
        ingestion_status=ingestion.get('status', 'pending'),
        ingestion_job_id=ingestion.get('jobId'),
        created_at=document['createdAt'],
        updated_at=document['updatedAt'],
        document_id=document.get('documentId'),
        canonical_report_id=document.get('canonicalReportId'),
    )


def _report_id(value: str) -> str:
    normalized = _required_text('report_id', value)
    if (
        len(normalized) > 128
        or re.fullmatch(
            r'[A-Za-z0-9][A-Za-z0-9_.:-]*',
            normalized,
        )
        is None
    ):
        raise ValueError(
            'reportId 只能包含字母、数字、点、冒号、下划线和短横线',
        )
    return normalized


def _required_text(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be a string')
    normalized = value.strip()
    if not normalized:
        raise ValueError(f'{name} cannot be empty')
    return normalized


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    return _required_text('value', value)


def _recovery_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError('limit must be an integer')
    if value < 1 or value > 1000:
        raise ValueError('limit must be between 1 and 1000')
    return value
