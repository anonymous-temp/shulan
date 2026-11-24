"""用户健康画像的 MongoDB Repository。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from typing import Protocol
from typing import runtime_checkable

from pymongo import ASCENDING
from pymongo import DESCENDING
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .models import FactCategory
from .models import FactStatus
from .models import HealthFact
from .models import HealthProfileJob
from .models import HealthProfileView
from .models import HealthSubject
from .models import PhysicalFactReservation
from .models import ProfileEvent
from .models import ProfileIngestionSourceType
from .models import ProfileJobRedriveRecord
from .models import ProfileJobStatus


@runtime_checkable
class HealthProfileRepository(Protocol):
    async def ensure_indexes(self) -> None: ...

    async def get_self_subject(self, user_id: str) -> HealthSubject | None: ...

    async def get_subject(
        self,
        user_id: str,
        subject_id: str,
    ) -> HealthSubject | None: ...

    async def list_subjects(
        self,
        user_id: str,
        *,
        include_archived: bool = False,
    ) -> tuple[HealthSubject, ...]: ...

    async def create_self_subject(
        self,
        subject: HealthSubject,
    ) -> tuple[HealthSubject, bool]: ...

    async def create_subject(
        self,
        subject: HealthSubject,
    ) -> tuple[HealthSubject, bool]: ...

    async def update_subject(
        self,
        subject: HealthSubject,
        *,
        expected_version: int,
    ) -> HealthSubject | None: ...

    async def get_view(
        self,
        user_id: str,
        subject_id: str,
    ) -> HealthProfileView | None: ...

    async def ensure_initial_view(
        self,
        view: HealthProfileView,
    ) -> HealthProfileView: ...

    async def advance_subject_view(
        self,
        subject: HealthSubject,
        *,
        request_id: str,
        updated_at: datetime,
    ) -> HealthProfileView: ...

    async def list_facts(
        self,
        user_id: str,
        subject_id: str,
        *,
        statuses: Sequence[FactStatus],
    ) -> tuple[HealthFact, ...]: ...

    async def get_fact(
        self,
        user_id: str,
        subject_id: str,
        fact_id: str,
    ) -> HealthFact | None: ...

    async def list_fact_page(
        self,
        user_id: str,
        subject_id: str,
        *,
        categories: Sequence[FactCategory],
        statuses: Sequence[FactStatus],
        fact_types: Sequence[str],
        effective_from: datetime | None,
        effective_to: datetime | None,
        after: tuple[datetime, str] | None,
        limit: int,
    ) -> tuple[HealthFact, ...]: ...

    async def replace_fact(
        self,
        fact: HealthFact,
        *,
        expected_version: int,
        allowed_statuses: Sequence[FactStatus],
    ) -> HealthFact | None: ...

    async def create_fact(
        self,
        fact: HealthFact,
    ) -> tuple[HealthFact, bool]: ...

    async def mark_fact_deleted(
        self,
        *,
        user_id: str,
        subject_id: str,
        fact_id: str,
        expected_version: int,
        request_id: str,
        request_fingerprint: str,
        deleted_at: datetime,
    ) -> HealthFact | None: ...

    async def get_deleted_fact_tombstone(
        self,
        user_id: str,
        subject_id: str,
        fact_id: str,
    ) -> dict[str, Any] | None: ...

    async def advance_fact_view(
        self,
        *,
        user_id: str,
        subject_id: str,
        expected_profile_version: int,
        request_id: str,
        category_counts: dict[str, int],
        key_fact_ids: Sequence[str],
        pending_confirmation_count: int,
        last_source_at: datetime | None,
        updated_at: datetime,
    ) -> HealthProfileView | None: ...

    async def get_event_by_request(
        self,
        user_id: str,
        request_id: str,
    ) -> ProfileEvent | None: ...

    async def list_event_page(
        self,
        user_id: str,
        subject_id: str,
        *,
        event_types: Sequence[str],
        before_sequence: int | None,
        limit: int,
    ) -> tuple[ProfileEvent, ...]: ...

    async def ensure_subject_created_event(
        self,
        event: ProfileEvent,
    ) -> ProfileEvent: ...

    async def append_event(self, event: ProfileEvent) -> ProfileEvent: ...


@runtime_checkable
class HealthProfileJobRepository(Protocol):
    """画像摄取任务的独立持久化契约。"""

    async def create_job(
        self,
        job: HealthProfileJob,
    ) -> tuple[HealthProfileJob, bool]: ...

    async def get_job_by_idempotency(
        self,
        user_id: str,
        idempotency_key: str,
    ) -> HealthProfileJob | None: ...

    async def claim_next_job(
        self,
        *,
        worker_id: str,
        lease_token: str,
        now: datetime,
        lease_expires_at: datetime,
        source_types: Sequence[ProfileIngestionSourceType] | None = None,
    ) -> HealthProfileJob | None: ...

    async def complete_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_token: str,
        completed_at: datetime,
        graph_version: str,
        policy_version: str,
        extractor_version: str,
        processing_summary: dict[str, Any] | None = None,
    ) -> HealthProfileJob | None: ...

    async def fail_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_token: str,
        failed_at: datetime,
        error_code: str,
        status: ProfileJobStatus,
        next_retry_at: datetime | None,
    ) -> HealthProfileJob | None: ...


@runtime_checkable
class HealthProfileJobMaintenanceRepository(Protocol):
    """只供受控运维使用的画像任务查询与重驱契约。"""

    async def get_job(self, job_id: str) -> HealthProfileJob | None: ...

    async def list_jobs(
        self,
        *,
        statuses: Sequence[ProfileJobStatus],
        source_types: Sequence[ProfileIngestionSourceType] | None,
        limit: int,
    ) -> tuple[HealthProfileJob, ...]: ...

    async def redrive_job(
        self,
        *,
        job_id: str,
        expected_status: ProfileJobStatus,
        expected_updated_at: datetime,
        redrive_record: ProfileJobRedriveRecord,
    ) -> HealthProfileJob | None: ...


class MongoHealthProfileRepository:
    """集中管理画像的五个集合与索引。"""

    def __init__(
        self,
        *,
        subjects,
        facts,
        events,
        views,
        jobs,
        publications=None,
        basic_info=None,
    ) -> None:
        self._subjects = subjects
        self._facts = facts
        self._events = events
        self._views = views
        self._jobs = jobs
        self._publications = publications
        self.basic_info = None
        if basic_info is None:
            from pymongo.asynchronous.collection import AsyncCollection

            if isinstance(views, AsyncCollection):
                basic_info = views.database.get_collection(views.name + '_basic_info')
        if basic_info is not None:
            from .basic_info import ProfileBasicInfoService
            from .basic_info_store import MongoBasicInfoStore

            self.basic_info = ProfileBasicInfoService(self, MongoBasicInfoStore(basic_info))
        self._indexes_ready = False
        self._index_lock = asyncio.Lock()

    async def has_unsettled_source_jobs(self, scope, source_type, source_id):
        row = await self._jobs.find_one(
            {
                'userId': scope.user_id,
                **({'subjectId': scope.subject_id} if source_type == 'health_report' else {}),
                'sourceType': source_type,
                'sourceId': source_id,
                'status': {'$in': ['pending', 'running', 'retry_wait']},
            }
        )
        return row is not None

    async def is_source_publication_managed(self, user_id, subject_id, source_type, source_id):
        if self._publications is None:
            return False
        from .contracts import ProfileScope
        from .source_publications import source_key

        if source_type in {'record', 'chat', 'questionnaire'}:
            return (
                await self._publications.find_one(
                    {
                        'recordKind': 'cutover',
                        'scope.userId': user_id,
                        'authorization.sourceType': source_type,
                        'authorization.sourceId': source_id,
                    }
                )
                is not None
            )
        if subject_id is None:
            return False
        key = source_key(ProfileScope(user_id=user_id, subject_id=subject_id), source_type, source_id)
        row = await self._publications.find_one({'_id': 'cutover_' + key})
        return row is not None

    async def ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        async with self._index_lock:
            if self._indexes_ready:
                return
            await self._ensure_subject_indexes()
            await self._ensure_fact_indexes()
            await self._ensure_event_indexes()
            await self._ensure_view_indexes()
            await self._ensure_job_indexes()
            self._indexes_ready = True

    async def _ensure_subject_indexes(self) -> None:
        await self._subjects.create_index(
            [('userId', ASCENDING), ('subjectId', ASCENDING)],
            unique=True,
            name='health_subject_user_subject_unique',
        )
        await self._subjects.create_index(
            [('userId', ASCENDING), ('status', ASCENDING), ('kind', ASCENDING)],
            name='health_subject_user_status_kind',
        )
        await self._subjects.create_index(
            [('userId', ASCENDING), ('kind', ASCENDING)],
            unique=True,
            partialFilterExpression={'kind': 'self', 'status': 'active'},
            name='health_subject_active_self_unique',
        )
        await self._subjects.create_index(
            [('userId', ASCENDING), ('creationRequestId', ASCENDING)],
            unique=True,
            partialFilterExpression={
                'creationRequestId': {'$type': 'string'},
            },
            name='health_subject_creation_request_unique',
        )
        await self._subjects.create_index(
            [('userId', ASCENDING), ('lastRequestId', ASCENDING)],
            partialFilterExpression={'lastRequestId': {'$type': 'string'}},
            name='health_subject_last_request',
        )

    async def _ensure_fact_indexes(self) -> None:
        await self._facts.create_index(
            [('userId', ASCENDING), ('factId', ASCENDING)],
            unique=True,
            name='health_fact_user_fact_unique',
        )
        await self._facts.create_index(
            [
                ('userId', ASCENDING),
                ('subjectId', ASCENDING),
                ('status', ASCENDING),
                ('factType', ASCENDING),
            ],
            name='health_fact_subject_status_type',
        )
        await self._facts.create_index(
            [
                ('userId', ASCENDING),
                ('subjectId', ASCENDING),
                ('category', ASCENDING),
                ('effectivePeriod.start', DESCENDING),
            ],
            name='health_fact_subject_category_effective',
        )
        await self._facts.create_index(
            [('sources.sourceType', ASCENDING), ('sources.sourceId', ASCENDING)],
            name='health_fact_source',
        )
        await self._facts.create_index(
            [('userId', ASCENDING), ('subjectId', ASCENDING), ('physicalOccurrenceAliases', ASCENDING)],
            name='health_fact_subject_occurrence_alias',
        )
        await self._facts.create_index(
            [
                ('userId', ASCENDING),
                ('creationRequestId', ASCENDING),
                ('creationRequestSlot', ASCENDING),
            ],
            unique=True,
            partialFilterExpression={
                'creationRequestId': {'$type': 'string'},
            },
            name='health_fact_creation_request_unique',
        )

    async def _ensure_event_indexes(self) -> None:
        await self._events.create_index(
            [
                ('userId', ASCENDING),
                ('subjectId', ASCENDING),
                ('sequence', ASCENDING),
            ],
            unique=True,
            name='health_profile_event_sequence_unique',
        )
        await self._events.create_index(
            [
                ('userId', ASCENDING),
                ('subjectId', ASCENDING),
                ('createdAt', DESCENDING),
            ],
            name='health_profile_event_created',
        )
        await self._events.create_index(
            [
                ('requestId', ASCENDING),
                ('eventType', ASCENDING),
                ('factId', ASCENDING),
            ],
            unique=True,
            partialFilterExpression={'requestId': {'$type': 'string'}},
            name='health_profile_event_request_unique',
        )

    async def _ensure_view_indexes(self) -> None:
        await self._views.create_index(
            [('userId', ASCENDING), ('subjectId', ASCENDING)],
            unique=True,
            name='health_profile_view_subject_unique',
        )
        await self._views.create_index(
            [('userId', ASCENDING), ('updatedAt', DESCENDING)],
            name='health_profile_view_updated',
        )

    async def _ensure_job_indexes(self) -> None:
        await self._jobs.create_index(
            [('idempotencyKey', ASCENDING)],
            unique=True,
            name='health_profile_job_idempotency_unique',
        )
        await self._jobs.create_index(
            [('status', ASCENDING), ('nextRetryAt', ASCENDING)],
            name='health_profile_job_retry_scan',
        )
        await self._jobs.create_index(
            [('leaseExpiresAt', ASCENDING)],
            name='health_profile_job_lease_expiry',
        )
        await self._jobs.create_index(
            [('status', ASCENDING), ('updatedAt', DESCENDING)],
            name='health_profile_job_terminal_scan',
        )

    async def create_job(
        self,
        job: HealthProfileJob,
    ) -> tuple[HealthProfileJob, bool]:
        if not isinstance(job, HealthProfileJob):
            raise TypeError('job must be a HealthProfileJob')
        await self.ensure_indexes()
        try:
            result = await self._jobs.insert_one(
                job.to_document(document_id=job.job_id),
            )
        except DuplicateKeyError:
            existing = await self.get_job_by_idempotency(
                job.user_id,
                job.idempotency_key,
            )
            if existing is None:
                raise
            return existing, False
        if not result.acknowledged:
            raise RuntimeError('保存健康画像任务失败')
        return job, True

    async def get_job_by_idempotency(
        self,
        user_id: str,
        idempotency_key: str,
    ) -> HealthProfileJob | None:
        document = await self._jobs.find_one(
            {
                'userId': _required_text('user_id', user_id),
                'idempotencyKey': _required_text(
                    'idempotency_key',
                    idempotency_key,
                ),
            }
        )
        return None if document is None else HealthProfileJob.model_validate(_without_id(document))

    async def get_job(self, job_id: str) -> HealthProfileJob | None:
        document = await self._jobs.find_one(
            {
                '_id': _required_text('job_id', job_id),
            }
        )
        return None if document is None else HealthProfileJob.model_validate(_without_id(document))

    async def list_jobs(
        self,
        *,
        statuses: Sequence[ProfileJobStatus],
        source_types: Sequence[ProfileIngestionSourceType] | None = None,
        limit: int = 100,
    ) -> tuple[HealthProfileJob, ...]:
        await self.ensure_indexes()
        normalized_statuses = tuple(dict.fromkeys(statuses))
        if not normalized_statuses:
            raise ValueError('statuses cannot be empty')
        if any(not isinstance(item, ProfileJobStatus) for item in normalized_statuses):
            raise TypeError('statuses must contain ProfileJobStatus')
        if limit < 1 or limit > 1000:
            raise ValueError('limit must be between 1 and 1000')
        filters: dict[str, Any] = {
            'status': {'$in': [item.value for item in normalized_statuses]},
        }
        if source_types is not None:
            normalized_source_types = tuple(dict.fromkeys(source_types))
            if not normalized_source_types:
                raise ValueError('source_types cannot be empty')
            if any(not isinstance(item, ProfileIngestionSourceType) for item in normalized_source_types):
                raise TypeError(
                    'source_types must contain ProfileIngestionSourceType',
                )
            filters['sourceType'] = {
                '$in': [item.value for item in normalized_source_types],
            }
        cursor = (
            self._jobs.find(filters)
            .sort(
                [
                    ('updatedAt', DESCENDING),
                    ('jobId', ASCENDING),
                ]
            )
            .limit(limit)
        )
        documents = await cursor.to_list(length=limit)
        return tuple(HealthProfileJob.model_validate(_without_id(document)) for document in documents)

    async def redrive_job(
        self,
        *,
        job_id: str,
        expected_status: ProfileJobStatus,
        expected_updated_at: datetime,
        redrive_record: ProfileJobRedriveRecord,
    ) -> HealthProfileJob | None:
        if expected_status not in {
            ProfileJobStatus.COMPLETED,
            ProfileJobStatus.DEAD_LETTER,
        }:
            raise ValueError('only terminal profile jobs can be redriven')
        if not isinstance(redrive_record, ProfileJobRedriveRecord):
            raise TypeError('redrive_record must be a ProfileJobRedriveRecord')
        if redrive_record.from_status is not expected_status:
            raise ValueError(
                'redrive audit status must match expected_status',
            )
        document = await self._jobs.find_one_and_update(
            {
                '_id': _required_text('job_id', job_id),
                'status': expected_status.value,
                'updatedAt': expected_updated_at,
            },
            {
                '$set': {
                    'status': ProfileJobStatus.PENDING.value,
                    'attempts': 0,
                    'updatedAt': redrive_record.redriven_at,
                },
                '$unset': {
                    'nextRetryAt': '',
                    'leaseOwner': '',
                    'leaseToken': '',
                    'leaseExpiresAt': '',
                    'graphVersion': '',
                    'policyVersion': '',
                    'extractorVersion': '',
                    'errorCode': '',
                    'completedAt': '',
                    'processingSummary': '',
                },
                '$push': {
                    'redriveHistory': {
                        '$each': [
                            redrive_record.model_dump(
                                mode='python',
                                by_alias=True,
                            )
                        ],
                        '$slice': -20,
                    },
                },
            },
            return_document=ReturnDocument.AFTER,
        )
        return None if document is None else HealthProfileJob.model_validate(_without_id(document))

    async def claim_next_job(
        self,
        *,
        worker_id: str,
        lease_token: str,
        now: datetime,
        lease_expires_at: datetime,
        source_types: Sequence[ProfileIngestionSourceType] | None = None,
    ) -> HealthProfileJob | None:
        filters: dict[str, Any] = {
            '$or': [
                {'status': ProfileJobStatus.PENDING.value},
                {
                    'status': ProfileJobStatus.RETRY_WAIT.value,
                    'nextRetryAt': {'$lte': now},
                },
                {
                    'status': ProfileJobStatus.RUNNING.value,
                    'leaseExpiresAt': {'$lte': now},
                },
            ],
        }
        if source_types is not None:
            filters['sourceType'] = {
                '$in': [source_type.value for source_type in source_types],
            }
        document = await self._jobs.find_one_and_update(
            filters,
            {
                '$set': {
                    'status': ProfileJobStatus.RUNNING.value,
                    'leaseOwner': _required_text('worker_id', worker_id),
                    'leaseToken': _required_text('lease_token', lease_token),
                    'leaseExpiresAt': lease_expires_at,
                    'nextRetryAt': None,
                    'errorCode': None,
                    'updatedAt': now,
                },
                '$inc': {'attempts': 1},
            },
            sort=[('createdAt', ASCENDING), ('jobId', ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        return None if document is None else HealthProfileJob.model_validate(_without_id(document))

    async def complete_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_token: str,
        completed_at: datetime,
        graph_version: str,
        policy_version: str,
        extractor_version: str,
        processing_summary: dict[str, Any] | None = None,
    ) -> HealthProfileJob | None:
        document = await self._jobs.find_one_and_update(
            {
                'jobId': _required_text('job_id', job_id),
                'status': ProfileJobStatus.RUNNING.value,
                'leaseOwner': _required_text('worker_id', worker_id),
                'leaseToken': _required_text('lease_token', lease_token),
                'leaseExpiresAt': {'$gt': completed_at},
            },
            {
                '$set': {
                    'status': ProfileJobStatus.COMPLETED.value,
                    'graphVersion': _required_text(
                        'graph_version',
                        graph_version,
                    ),
                    'policyVersion': _required_text(
                        'policy_version',
                        policy_version,
                    ),
                    'extractorVersion': _required_text(
                        'extractor_version',
                        extractor_version,
                    ),
                    'completedAt': completed_at,
                    'processingSummary': processing_summary,
                    'updatedAt': completed_at,
                },
                '$unset': {
                    'leaseOwner': '',
                    'leaseToken': '',
                    'leaseExpiresAt': '',
                    'nextRetryAt': '',
                    'errorCode': '',
                },
            },
            return_document=ReturnDocument.AFTER,
        )
        return None if document is None else HealthProfileJob.model_validate(_without_id(document))

    async def fail_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_token: str,
        failed_at: datetime,
        error_code: str,
        status: ProfileJobStatus,
        next_retry_at: datetime | None,
    ) -> HealthProfileJob | None:
        if status not in {
            ProfileJobStatus.RETRY_WAIT,
            ProfileJobStatus.DEAD_LETTER,
        }:
            raise ValueError('failed job must retry or enter dead letter')
        if status is ProfileJobStatus.RETRY_WAIT and next_retry_at is None:
            raise ValueError('retrying job requires next_retry_at')
        if status is ProfileJobStatus.DEAD_LETTER and next_retry_at is not None:
            raise ValueError('dead letter job cannot have next_retry_at')
        update = {
            '$set': {
                'status': status.value,
                'errorCode': _required_text('error_code', error_code),
                'updatedAt': failed_at,
            },
            '$unset': {
                'leaseOwner': '',
                'leaseToken': '',
                'leaseExpiresAt': '',
                'completedAt': '',
            },
        }
        if next_retry_at is None:
            update['$unset']['nextRetryAt'] = ''
        else:
            update['$set']['nextRetryAt'] = next_retry_at
        document = await self._jobs.find_one_and_update(
            {
                'jobId': _required_text('job_id', job_id),
                'status': ProfileJobStatus.RUNNING.value,
                'leaseOwner': _required_text('worker_id', worker_id),
                'leaseToken': _required_text('lease_token', lease_token),
                'leaseExpiresAt': {'$gt': failed_at},
            },
            update,
            return_document=ReturnDocument.AFTER,
        )
        return None if document is None else HealthProfileJob.model_validate(_without_id(document))

    async def get_self_subject(self, user_id: str) -> HealthSubject | None:
        document = await self._subjects.find_one(
            {
                'userId': _required_text('user_id', user_id),
                'kind': 'self',
                'status': 'active',
            }
        )
        if document is None:
            return None
        subject = HealthSubject.model_validate(_without_id(document))
        return await self.basic_info.project_subject(subject) if self.basic_info is not None else subject

    async def get_subject(
        self,
        user_id: str,
        subject_id: str,
    ) -> HealthSubject | None:
        document = await self._subjects.find_one(
            {
                'userId': _required_text('user_id', user_id),
                'subjectId': _required_text('subject_id', subject_id),
            }
        )
        if document is None:
            return None
        subject = HealthSubject.model_validate(_without_id(document))
        return await self.basic_info.project_subject(subject) if self.basic_info is not None else subject

    async def list_subjects(
        self,
        user_id: str,
        *,
        include_archived: bool = False,
    ) -> tuple[HealthSubject, ...]:
        if not isinstance(include_archived, bool):
            raise TypeError('include_archived must be a boolean')
        filters: dict = {'userId': _required_text('user_id', user_id)}
        if not include_archived:
            filters['status'] = {'$ne': 'archived'}
        cursor = self._subjects.find(filters).sort(
            [
                ('kind', ASCENDING),
                ('createdAt', ASCENDING),
            ]
        )
        documents = await cursor.to_list(length=None)
        subjects = tuple(HealthSubject.model_validate(_without_id(document)) for document in documents)
        if self.basic_info is not None:
            return tuple([await self.basic_info.project_subject(subject) for subject in subjects])
        return subjects

    async def create_self_subject(
        self,
        subject: HealthSubject,
    ) -> tuple[HealthSubject, bool]:
        if not isinstance(subject, HealthSubject):
            raise TypeError('subject must be a HealthSubject')
        await self.ensure_indexes()
        existing = await self.get_self_subject(subject.user_id)
        if existing is not None:
            return existing, False
        if self.basic_info is not None:
            subject = await self.basic_info.reserved_subject(subject)
        try:
            result = await self._subjects.insert_one(
                subject.to_document(document_id=subject.subject_id),
            )
        except DuplicateKeyError:
            existing = await self.get_self_subject(subject.user_id)
            if existing is None:
                raise
            return existing, False
        if not result.acknowledged:
            raise RuntimeError('保存本人健康主体失败')
        return subject, True

    async def create_subject(
        self,
        subject: HealthSubject,
    ) -> tuple[HealthSubject, bool]:
        if not isinstance(subject, HealthSubject):
            raise TypeError('subject must be a HealthSubject')
        await self.ensure_indexes()
        try:
            result = await self._subjects.insert_one(
                subject.to_document(document_id=subject.subject_id),
            )
        except DuplicateKeyError:
            existing = None
            if subject.creation_request_id is not None:
                document = await self._subjects.find_one(
                    {
                        'userId': subject.user_id,
                        'creationRequestId': subject.creation_request_id,
                    }
                )
                if document is not None:
                    existing = HealthSubject.model_validate(
                        _without_id(document),
                    )
            if existing is None:
                existing = await self.get_subject(
                    subject.user_id,
                    subject.subject_id,
                )
            if existing is None:
                raise
            return existing, False
        if not result.acknowledged:
            raise RuntimeError('保存健康主体失败')
        return subject, True

    async def update_subject(
        self,
        subject: HealthSubject,
        *,
        expected_version: int,
    ) -> HealthSubject | None:
        if not isinstance(subject, HealthSubject):
            raise TypeError('subject must be a HealthSubject')
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError('expected_version must be a positive integer')
        previous = await self.get_subject(subject.user_id, subject.subject_id)
        if previous is not None and previous.status != subject.status:
            subject = subject.model_copy(
                update={
                    'lifecycle_epoch': previous.lifecycle_epoch + 1,
                    'lifecycle_started_at': subject.updated_at,
                }
            )
        document = subject.to_document(document_id=subject.subject_id)
        document.pop('_id', None)
        updated = await self._subjects.find_one_and_replace(
            {
                'userId': subject.user_id,
                'subjectId': subject.subject_id,
                'version': expected_version,
            },
            document,
            return_document=ReturnDocument.AFTER,
        )
        return None if updated is None else HealthSubject.model_validate(_without_id(updated))

    async def synchronize_basic_info_view(self, user_id, subject, revision):
        """Idempotently publish the demographic revision into the shared Profile CAS."""
        for _ in range(32):
            view = await self.get_view(user_id, subject.subject_id)
            if view is None:
                view = await self.ensure_initial_view(
                    HealthProfileView(
                        user_id=user_id,
                        subject_id=subject.subject_id,
                        updated_at=subject.updated_at,
                    )
                )
            if view.basic_info_lifecycle_epoch == subject.lifecycle_epoch and view.basic_info_revision >= revision:
                return view
            updated = view.model_copy(
                update={
                    'basic_info_revision': revision,
                    'basic_info_lifecycle_epoch': subject.lifecycle_epoch,
                    'profile_version': view.profile_version + 1,
                    'subject_summary': {**view.subject_summary, 'displayName': subject.display_name},
                    'updated_at': subject.updated_at,
                }
            )
            document = updated.to_document(document_id=f'{user_id}:{subject.subject_id}')
            document.pop('_id', None)
            row = await self._views.find_one_and_replace(
                {'userId': user_id, 'subjectId': subject.subject_id, 'profileVersion': view.profile_version},
                document,
                return_document=ReturnDocument.AFTER,
            )
            if row is not None:
                return HealthProfileView.model_validate(_without_id(row))
        raise RuntimeError('basic_info.concurrent_profile_view')

    async def get_view(
        self,
        user_id: str,
        subject_id: str,
    ) -> HealthProfileView | None:
        document = await self._views.find_one(
            {
                'userId': _required_text('user_id', user_id),
                'subjectId': _required_text('subject_id', subject_id),
            }
        )
        return None if document is None else HealthProfileView.model_validate(_without_id(document))

    async def ensure_initial_view(
        self,
        view: HealthProfileView,
    ) -> HealthProfileView:
        if not isinstance(view, HealthProfileView):
            raise TypeError('view must be a HealthProfileView')
        await self.ensure_indexes()
        key = {'userId': view.user_id, 'subjectId': view.subject_id}
        existing = await self._views.find_one(key)
        if existing is not None:
            return HealthProfileView.model_validate(_without_id(existing))
        try:
            result = await self._views.insert_one(
                view.to_document(
                    document_id=f'{view.user_id}:{view.subject_id}',
                )
            )
        except DuplicateKeyError:
            existing = await self._views.find_one(key)
            if existing is None:
                raise
            return HealthProfileView.model_validate(_without_id(existing))
        if not result.acknowledged:
            raise RuntimeError('保存初始健康画像投影失败')
        return view

    async def advance_subject_view(
        self,
        subject: HealthSubject,
        *,
        request_id: str,
        updated_at: datetime,
    ) -> HealthProfileView:
        if not isinstance(subject, HealthSubject):
            raise TypeError('subject must be a HealthSubject')
        if not isinstance(updated_at, datetime):
            raise TypeError('updated_at must be a datetime')
        request_id = _required_text('request_id', request_id)
        document = await self._views.find_one_and_update(
            {
                'userId': subject.user_id,
                'subjectId': subject.subject_id,
                'lastRequestId': {'$ne': request_id},
            },
            {
                '$set': {
                    'subjectSummary.displayName': subject.display_name,
                    'lastRequestId': request_id,
                    'updatedAt': updated_at,
                },
                '$inc': {'profileVersion': 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            existing = await self.get_view(
                subject.user_id,
                subject.subject_id,
            )
            if existing is not None:
                return existing
            initial = await self.ensure_initial_view(
                HealthProfileView(
                    user_id=subject.user_id,
                    subject_id=subject.subject_id,
                    subject_summary={'displayName': subject.display_name},
                    last_request_id=request_id,
                    updated_at=updated_at,
                )
            )
            return initial
        return HealthProfileView.model_validate(_without_id(document))

    async def list_facts(
        self,
        user_id: str,
        subject_id: str,
        *,
        statuses: Sequence[FactStatus],
    ) -> tuple[HealthFact, ...]:
        normalized_statuses = tuple(
            status if isinstance(status, FactStatus) else FactStatus(status) for status in statuses
        )
        if not normalized_statuses:
            return ()
        cursor = self._facts.find(
            {
                'userId': _required_text('user_id', user_id),
                'subjectId': _required_text('subject_id', subject_id),
                'status': {'$in': [status.value for status in normalized_statuses]},
                'physicalReservationOnly': {'$ne': True},
            }
        ).sort(
            [
                ('effectivePeriod.start', DESCENDING),
                ('createdAt', DESCENDING),
            ]
        )
        documents = await cursor.to_list(length=None)
        return tuple(HealthFact.model_validate(_without_id(document)) for document in documents)

    async def get_fact(
        self,
        user_id: str,
        subject_id: str,
        fact_id: str,
        *,
        include_reservations: bool = False,
    ) -> HealthFact | None:
        query = {
            'userId': _required_text('user_id', user_id),
            'subjectId': _required_text('subject_id', subject_id),
            'factId': _required_text('fact_id', fact_id),
            'status': {'$ne': FactStatus.DELETED.value},
        }
        if not include_reservations:
            query['physicalReservationOnly'] = {'$ne': True}
        document = await self._facts.find_one(query)
        return None if document is None else HealthFact.model_validate(_without_id(document))

    async def replace_physical_reservations(self, fact, *, version, expected):
        query = {
            'factId': fact.fact_id,
            'userId': fact.user_id,
            'subjectId': fact.subject_id,
            'version': version,
            'physicalOccurrenceKey': fact.physical_occurrence_key,
        }
        _match_physical_reservations(query, expected)
        result = await self._facts.find_one_and_update(
            query,
            {'$set': {'physicalReservations': _physical_reservations_document(fact.physical_reservations)}},
            return_document=ReturnDocument.AFTER,
        )
        return result is not None

    async def get_fact_by_creation_request(self, user_id, request_id, *, slot='fact'):
        """Find an existing materialization before freezing a recovery intent."""
        document = await self._facts.find_one(
            {
                'userId': _required_text('user_id', user_id),
                'creationRequestId': _required_text('request_id', request_id),
                'creationRequestSlot': _required_text('slot', slot),
                'status': {'$ne': FactStatus.DELETED.value},
                'physicalReservationOnly': {'$ne': True},
            }
        )
        return None if document is None else HealthFact.model_validate(_without_id(document))

    async def list_fact_page(
        self,
        user_id: str,
        subject_id: str,
        *,
        categories: Sequence[FactCategory],
        statuses: Sequence[FactStatus],
        fact_types: Sequence[str],
        effective_from: datetime | None,
        effective_to: datetime | None,
        after: tuple[datetime, str] | None,
        limit: int,
    ) -> tuple[HealthFact, ...]:
        filters: dict[str, Any] = {
            'userId': _required_text('user_id', user_id),
            'subjectId': _required_text('subject_id', subject_id),
            'status': {'$in': [status.value for status in statuses]},
            'physicalReservationOnly': {'$ne': True},
        }
        if categories:
            filters['category'] = {
                '$in': [category.value for category in categories],
            }
        if fact_types:
            filters['factType'] = {'$in': list(fact_types)}
        effective_range = {}
        if effective_from is not None:
            effective_range['$gte'] = effective_from
        if effective_to is not None:
            effective_range['$lte'] = effective_to
        if effective_range:
            filters['effectivePeriod.start'] = effective_range
        if after is not None:
            after_time, after_fact_id = after
            if after_time is None:
                filters['effectivePeriod.start'] = None
                filters['factId'] = {'$gt': after_fact_id}
            else:
                filters['$or'] = [
                    {'effectivePeriod.start': {'$lt': after_time}},
                    {'effectivePeriod.start': after_time, 'factId': {'$gt': after_fact_id}},
                    {'effectivePeriod.start': None},
                ]
        cursor = self._facts.find(filters).sort(
            [
                ('effectivePeriod.start', DESCENDING),
                ('factId', ASCENDING),
            ]
        )
        documents = await cursor.to_list(length=limit)
        return tuple(HealthFact.model_validate(_without_id(document)) for document in documents)

    async def replace_fact(
        self,
        fact: HealthFact,
        *,
        expected_version: int,
        allowed_statuses: Sequence[FactStatus],
        expected_reservations: Sequence[PhysicalFactReservation] | None = None,
    ) -> HealthFact | None:
        if not isinstance(fact, HealthFact):
            raise TypeError('fact must be a HealthFact')
        document = fact.to_document(document_id=fact.fact_id)
        document.pop('_id', None)
        query = {
            'userId': fact.user_id,
            'subjectId': fact.subject_id,
            'factId': fact.fact_id,
            'version': expected_version,
            'status': {'$in': [status.value for status in allowed_statuses]},
        }
        if fact.physical_occurrence_key is not None:
            expected = fact.physical_reservations if expected_reservations is None else expected_reservations
            _match_physical_reservations(query, expected)
        updated = await self._facts.find_one_and_replace(
            query,
            document,
            return_document=ReturnDocument.AFTER,
        )
        return None if updated is None else HealthFact.model_validate(_without_id(updated))

    async def create_fact(
        self,
        fact: HealthFact,
    ) -> tuple[HealthFact, bool]:
        if not isinstance(fact, HealthFact):
            raise TypeError('fact must be a HealthFact')
        await self.ensure_indexes()
        try:
            result = await self._facts.insert_one(
                fact.to_document(document_id=fact.fact_id),
            )
        except DuplicateKeyError:
            existing = None
            if fact.creation_request_id is not None:
                filters = {
                    'userId': fact.user_id,
                    'creationRequestId': fact.creation_request_id,
                }
                if fact.creation_request_slot is not None:
                    filters['creationRequestSlot'] = fact.creation_request_slot
                document = await self._facts.find_one(filters)
                if document is not None:
                    existing = HealthFact.model_validate(
                        _without_id(document),
                    )
            if existing is None:
                existing = await self.get_fact(
                    fact.user_id,
                    fact.subject_id,
                    fact.fact_id,
                )
            if existing is None:
                raise
            return existing, False
        if not result.acknowledged:
            raise RuntimeError('保存健康事实失败')
        return fact, True

    async def mark_fact_deleted(
        self,
        *,
        user_id: str,
        subject_id: str,
        fact_id: str,
        expected_version: int,
        request_id: str,
        request_fingerprint: str,
        deleted_at: datetime,
    ) -> HealthFact | None:
        user_id = _required_text('user_id', user_id)
        subject_id = _required_text('subject_id', subject_id)
        fact_id = _required_text('fact_id', fact_id)
        request_id = _required_text('request_id', request_id)
        request_fingerprint = _required_text(
            'request_fingerprint',
            request_fingerprint,
        )
        selector = {
            'userId': user_id,
            'subjectId': subject_id,
            'factId': fact_id,
            'version': expected_version,
            'status': {'$ne': FactStatus.DELETED.value},
        }
        existing = await self._facts.find_one(selector)
        if existing is None:
            return None
        tombstone = {
            'factId': fact_id,
            'userId': user_id,
            'subjectId': subject_id,
            'status': FactStatus.DELETED.value,
            'version': expected_version + 1,
            'deletionRequestId': request_id,
            'deletionRequestFingerprint': request_fingerprint,
            'deletedAt': deleted_at,
            'updatedAt': deleted_at,
        }
        for field in ('physicalOccurrenceKey', 'physicalOccurrenceAliases'):
            if existing.get(field):
                tombstone[field] = existing[field]
        previous = await self._facts.find_one_and_replace(
            selector,
            tombstone,
            return_document=ReturnDocument.BEFORE,
        )
        return None if previous is None else HealthFact.model_validate(_without_id(previous))

    async def get_deleted_fact_tombstone(
        self,
        user_id: str,
        subject_id: str,
        fact_id: str,
    ) -> dict[str, Any] | None:
        document = await self._facts.find_one(
            {
                'userId': _required_text('user_id', user_id),
                'subjectId': _required_text('subject_id', subject_id),
                'factId': _required_text('fact_id', fact_id),
                'status': FactStatus.DELETED.value,
            }
        )
        return None if document is None else _without_id(document)

    async def find_physical_occurrence_keys(
        self, user_id: str, subject_id: str, keys: set[str]
    ) -> tuple[tuple[str, str], ...]:
        if not keys:
            return ()
        requested = sorted(keys)
        keys_by_fact_id = {'fact_physical_' + key: key for key in requested}
        cursor = self._facts.find(
            {
                'userId': user_id,
                'subjectId': subject_id,
                '$or': [
                    {'factId': {'$in': list(keys_by_fact_id)}},
                    {'physicalOccurrenceAliases': {'$in': requested}},
                ],
            }
        )
        bindings = []
        for row in await cursor.to_list(length=None):
            primary = row.get('physicalOccurrenceKey')
            if primary is None and row.get('status') == FactStatus.DELETED.value:
                # Older tombstones retained the fact ID as the original position fence.
                primary = keys_by_fact_id.get(row.get('factId'))
            if primary is not None:
                bindings.extend(
                    (key, primary) for key in keys.intersection((primary, *row.get('physicalOccurrenceAliases', ())))
                )
        return tuple(bindings)

    async def advance_fact_view(
        self,
        *,
        user_id: str,
        subject_id: str,
        expected_profile_version: int,
        request_id: str,
        category_counts: dict[str, int],
        key_fact_ids: Sequence[str],
        pending_confirmation_count: int,
        last_source_at: datetime | None,
        updated_at: datetime,
    ) -> HealthProfileView | None:
        user_id = _required_text('user_id', user_id)
        subject_id = _required_text('subject_id', subject_id)
        request_id = _required_text('request_id', request_id)
        document = await self._views.find_one_and_update(
            {
                'userId': user_id,
                'subjectId': subject_id,
                'profileVersion': expected_profile_version,
                'lastRequestId': {'$ne': request_id},
            },
            {
                '$set': {
                    'categoryCounts': dict(category_counts),
                    'keyFactIds': list(key_fact_ids),
                    'pendingConfirmationCount': pending_confirmation_count,
                    'lastSourceAt': last_source_at,
                    'lastRequestId': request_id,
                    'updatedAt': updated_at,
                },
                '$inc': {'profileVersion': 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if document is not None:
            return HealthProfileView.model_validate(_without_id(document))
        existing = await self.get_view(user_id, subject_id)
        if existing is not None and existing.last_request_id == request_id:
            return existing
        return None

    async def get_event_by_request(
        self,
        user_id: str,
        request_id: str,
    ) -> ProfileEvent | None:
        document = await self._events.find_one(
            {
                'userId': _required_text('user_id', user_id),
                'requestId': _required_text('request_id', request_id),
            }
        )
        return None if document is None else ProfileEvent.model_validate(_without_id(document))

    async def list_event_page(
        self,
        user_id: str,
        subject_id: str,
        *,
        event_types: Sequence[str],
        before_sequence: int | None,
        limit: int,
    ) -> tuple[ProfileEvent, ...]:
        filters: dict[str, Any] = {
            'userId': _required_text('user_id', user_id),
            'subjectId': _required_text('subject_id', subject_id),
        }
        if event_types:
            filters['eventType'] = {'$in': list(event_types)}
        if before_sequence is not None:
            filters['sequence'] = {'$lt': before_sequence}
        cursor = self._events.find(filters).sort(
            [
                ('sequence', DESCENDING),
                ('eventId', ASCENDING),
            ]
        )
        documents = await cursor.to_list(length=limit)
        return tuple(ProfileEvent.model_validate(_without_id(document)) for document in documents)

    async def ensure_subject_created_event(
        self,
        event: ProfileEvent,
    ) -> ProfileEvent:
        return await self.append_event(event)

    async def append_event(self, event: ProfileEvent) -> ProfileEvent:
        if not isinstance(event, ProfileEvent):
            raise TypeError('event must be a ProfileEvent')
        await self.ensure_indexes()
        existing = await self._events.find_one({'_id': event.event_id})
        if existing is not None:
            return ProfileEvent.model_validate(_without_id(existing))
        try:
            result = await self._events.insert_one(
                event.to_document(
                    document_id=event.event_id,
                )
            )
        except DuplicateKeyError:
            existing = None
            if event.request_id is not None:
                existing_event = await self.get_event_by_request(
                    event.user_id,
                    event.request_id,
                )
                if existing_event is not None:
                    return existing_event
            existing = await self._events.find_one({'_id': event.event_id})
            if existing is None:
                raise
            return ProfileEvent.model_validate(_without_id(existing))
        if not result.acknowledged:
            raise RuntimeError('保存健康画像事件失败')
        return event


def _required_text(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be a string')
    normalized = value.strip()
    if not normalized:
        raise ValueError(f'{name} cannot be empty')
    return normalized


def _physical_reservations_document(reservations):
    return [reservation.model_dump(mode='python', by_alias=True) for reservation in reservations]


def _match_physical_reservations(query, expected):
    if expected:
        query['physicalReservations'] = _physical_reservations_document(expected)
    else:
        query['physicalReservations'] = {'$in': [None, []]}


def _without_id(document) -> dict:
    normalized = dict(document)
    normalized.pop('_id', None)
    return normalized
