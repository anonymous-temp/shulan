"""用户健康画像的应用领域服务。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents.graphs.health_profile import PROFILE_EVIDENCE_CAPABILITY
from agents.graphs.health_profile.focus import POLICY_VERSION
from agents.persistence.locks import AgentLeaseManager
from utils.log import getLogger

from .alerts import select_profile_alerts
from .document_archive_contracts import ProfileDocumentArchiveChangedError
from .document_archive_contracts import ProfileDocumentArchiveReadError
from .document_views import ProfileDocumentObservationViews
from .document_views import validate_document_view
from .errors import HealthProfileConflictError
from .errors import HealthProfileNotFoundError
from .errors import HealthProfileValidationError
from .evidence_results import ProfileEvidenceResults
from .evidence_results import evidence_key
from .models import AssertionLevel
from .models import EffectivePeriod
from .models import FactCategory
from .models import FactSource
from .models import FactStatus
from .models import FactValueType
from .models import HealthFact
from .models import HealthProfileView
from .models import HealthSubject
from .models import ProfileActor
from .models import ProfileEvent
from .models import SubjectKind
from .models import SubjectRelationship
from .models import VerificationStatus
from .pagination import decode_cursor
from .pagination import encode_cursor
from .projection import apply_profile_insights
from .projection import build_profile_projection
from .projection import evidence_fact_payloads
from .projection import public_fact
from .projection import public_subject
from .repository import HealthProfileRepository
from .summary import build_profile_summary

logger = getLogger(__name__)

Clock = Callable[[], datetime]
IdFactory = Callable[[str], str]
_MISSING = object()
_DEFAULT_FACT_STATUSES = (
    FactStatus.ACTIVE,
    FactStatus.PENDING,
    FactStatus.DISPUTED,
)
_PUBLIC_FACT_STATUSES = frozenset(
    {
        FactStatus.ACTIVE,
        FactStatus.PENDING,
        FactStatus.DISPUTED,
        FactStatus.SUPERSEDED,
        FactStatus.RETRACTED,
        FactStatus.EXPIRED,
    }
)
_PUBLIC_EVENT_TYPES = frozenset(
    {
        'subject_created',
        'subject_updated',
        'fact_ingested',
        'fact_confirmed',
        'fact_retracted',
        'fact_superseded',
        'fact_deleted',
    }
)


@dataclass(frozen=True, slots=True)
class ProfileBootstrapResult:
    subject: HealthSubject
    view: HealthProfileView
    created: bool


@dataclass(frozen=True, slots=True)
class SubjectUpsertResult:
    subject: HealthSubject
    created: bool

    def to_api(self) -> dict[str, Any]:
        return {
            'created': self.created,
            'subject': public_subject(self.subject),
        }


@dataclass(frozen=True, slots=True)
class FactReplacement:
    value_type: FactValueType
    value: Any
    effective_period: EffectivePeriod

    def fingerprint_value(self) -> dict[str, Any]:
        return {
            'valueType': self.value_type.value,
            'value': self.value,
            'effectivePeriod': self.effective_period.model_dump(
                mode='json',
                by_alias=True,
            ),
        }


class HealthProfileService:
    """协调主体、事实、画像投影和循证 Agent 能力。"""

    def __init__(
        self,
        repository: HealthProfileRepository,
        *,
        agent_hub: AgentHub | None = None,
        lease_manager: AgentLeaseManager | None = None,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        document_observation_store=None,
        document_archive_reader=None,
        evidence_results: ProfileEvidenceResults | None = None,
        evidence_revision: str = POLICY_VERSION,
    ) -> None:
        if not isinstance(repository, HealthProfileRepository):
            raise TypeError('repository must implement HealthProfileRepository')
        if agent_hub is not None and not isinstance(agent_hub, AgentHub):
            raise TypeError('agent_hub must be an AgentHub')
        if lease_manager is not None and not isinstance(lease_manager, AgentLeaseManager):
            raise TypeError('lease_manager must implement AgentLeaseManager')
        self._repository = repository
        self._basic_info = getattr(repository, 'basic_info', None)
        self._agent_hub = agent_hub
        self._lease_manager = lease_manager
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or _new_id
        self._document_observations = document_observation_store
        self._document_views = None
        self._document_archive_reader = None
        self._document_reader_started = False
        if document_archive_reader is not None:
            self.bind_document_archive_reader(document_archive_reader)
        self._evidence_results = evidence_results
        self._evidence_revision = _required_text('evidence_revision', evidence_revision)

    def bind_document_archive_reader(self, reader, *, require_profile_ownership=False, publications=None) -> None:
        """Bind the public document read port once during application assembly."""
        if reader is self._document_archive_reader:
            return
        if self._document_reader_started or self._document_archive_reader is not None:
            raise RuntimeError('Document archive reader is already fixed for this Profile service')
        if not all(
            callable(getattr(reader, name, None))
            for name in (
                'list_archived',
                'read_fixed',
                'read_source_state',
            )
        ):
            raise TypeError('Document archive reader must implement the Profile read port')
        self._document_archive_reader = reader
        self._document_views = ProfileDocumentObservationViews(
            archives=reader,
            repository=self._repository,
            legacy=self._document_observations,
            require_profile_ownership=require_profile_ownership,
            publications=publications,
        )

    async def ensure_self_profile(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
    ) -> ProfileBootstrapResult:
        """为用户按需创建唯一的本人主体和空画像投影。"""
        user_id = _required_text('user_id', user_id)
        display_name = _optional_text(display_name, name='display_name')
        await self._repository.ensure_indexes()
        existing = await self._repository.get_self_subject(user_id)
        created = False
        if existing is None:
            now = self._aware_now()
            candidate = HealthSubject(
                subject_id=self._id_factory('subject'),
                user_id=user_id,
                kind=SubjectKind.SELF,
                display_name=display_name,
                created_at=now,
                updated_at=now,
            )
            subject, created = await self._repository.create_self_subject(
                candidate,
            )
        else:
            subject = existing

        now = self._aware_now()
        event = await self._repository.ensure_subject_created_event(
            ProfileEvent(
                event_id=f'event_subject_created_{subject.subject_id}',
                user_id=user_id,
                subject_id=subject.subject_id,
                sequence=1,
                event_type='subject_created',
                reason_code='profile_bootstrap',
                actor=ProfileActor(type='system'),
                request_id=f'profile_bootstrap:{subject.subject_id}',
                created_at=subject.created_at,
            ),
        )
        view = await self._repository.ensure_initial_view(
            HealthProfileView(
                user_id=user_id,
                subject_id=subject.subject_id,
                profile_version=event.sequence,
                subject_summary={
                    'displayName': subject.display_name,
                },
                updated_at=now,
            ),
        )
        return ProfileBootstrapResult(
            subject=subject,
            view=view,
            created=created,
        )

    async def get_profile(
        self,
        user_id: str,
        *,
        subject_id: str | None = None,
        include_evidence: bool = False,
        indicator_limit: int = 12,
        indicator_scope: str | Sequence[str] = 'all',
        fact_statuses: Sequence[str] | None = None,
        document_observation_view: str = 'standard',
    ) -> dict[str, Any]:
        """获取本人或指定健康主体的当前画像。

        `fact_statuses` 筛选当前健康事实列表，`indicator_scope` 筛选指标区。
        摘要与待确认计数始终返回。
        """
        user_id = _required_text('user_id', user_id)
        subject_id = _optional_text(subject_id, name='subject_id')
        try:
            validate_document_view(document_observation_view, None, 20)
        except (TypeError, ValueError) as error:
            raise HealthProfileValidationError(str(error)) from error
        if not isinstance(include_evidence, bool):
            raise HealthProfileValidationError(
                'includeEvidence 必须为 boolean',
            )
        normalized_indicator_scope = _normalize_indicator_scope(indicator_scope)
        normalized_fact_statuses = _normalize_fact_group_statuses(fact_statuses)
        if isinstance(indicator_limit, bool) or not isinstance(indicator_limit, int) or not 1 <= indicator_limit <= 50:
            raise HealthProfileValidationError(
                'indicatorLimit 必须为 1 到 50 的整数',
            )

        if subject_id is None:
            bootstrap = await self.ensure_self_profile(user_id)
            subject = bootstrap.subject
            view = bootstrap.view
        else:
            await self._repository.ensure_indexes()
            subject = await self._repository.get_subject(user_id, subject_id)
            if subject is None:
                raise HealthProfileNotFoundError('健康主体不存在')
            view = await self._repository.get_view(user_id, subject_id)
            if view is None:
                view = await self._repository.ensure_initial_view(
                    HealthProfileView(
                        user_id=user_id,
                        subject_id=subject.subject_id,
                        subject_summary={
                            'displayName': subject.display_name,
                        },
                        updated_at=self._aware_now(),
                    ),
                )

        facts = await self._repository.list_facts(
            user_id,
            subject.subject_id,
            statuses=(
                FactStatus.ACTIVE,
                FactStatus.PENDING,
                FactStatus.DISPUTED,
            ),
        )
        now = self._aware_now()
        basic_info = None
        if self._basic_info is not None:
            basic_info = await self._basic_info.read(user_id, subject_id=subject.subject_id, as_of=now)
            facts = await self._basic_info.select_facts(user_id, subject.subject_id, facts, snapshot=basic_info)
        result = build_profile_projection(
            user_id=user_id,
            subject=subject,
            view=view,
            facts=facts,
            indicator_limit=indicator_limit,
            indicator_scope=normalized_indicator_scope,
            fact_statuses=normalized_fact_statuses,
            generated_at=now,
        )
        if basic_info is not None and basic_info.revision:
            result['basicInfo'] = {
                key: item.model_dump(mode='json', by_alias=True) for key, item in basic_info.fields.items()
            }
            result['basicInfoRevision'] = basic_info.revision
            result['schemaVersion'] = max(2, result['schemaVersion'])
            for key, item in basic_info.fields.items():
                result['overview'][key] = item.value
        interpretation = await self._get_interpretation(
            user_id=user_id,
            subject_id=subject.subject_id,
            facts=facts,
            requested=include_evidence,
            generated_at=now,
            projection=result,
        )
        result['evidence'] = interpretation['evidence']
        result['alerts'] = select_profile_alerts(
            facts=facts,
            alerts=result['alerts'],
            insights=interpretation['insights'],
            evidence_items=result['evidence']['items'],
        )
        result['summary'] = build_profile_summary(
            facts=facts,
            profile_version=result['profileVersion'],
            generated_at=result['evidence']['generatedAt'] or now,
            alerts=result['alerts'],
            insights=interpretation['insights'],
            evidence_items=result['evidence']['items'],
        )
        if (
            self._document_observations is not None
            or self._document_views is not None
            or document_observation_view == 'complete'
        ):
            try:
                result['documentObservations'] = await self._read_document_observations(
                    user_id,
                    subject.subject_id,
                    view=document_observation_view,
                )
            except (HealthProfileValidationError, HealthProfileConflictError):
                raise
            except Exception as error:
                logger.warning('原报告指标读取失败: error_type=%s', type(error).__name__)
                result['documentObservations'] = {'items': [], 'nextCursor': None, 'status': 'unavailable'}
        return result

    async def list_document_observations(
        self,
        user_id,
        *,
        subject_id=None,
        document_id=None,
        cursor=None,
        limit=20,
        view='standard',
    ):
        user_id = _required_text('user_id', user_id)
        subject_id = _optional_text(subject_id, name='subjectId')
        document_id = _optional_text(document_id, name='documentId')
        try:
            validate_document_view(view, cursor, limit)
        except (TypeError, ValueError) as error:
            raise HealthProfileValidationError(str(error)) from error
        if subject_id is None:
            subject = (await self.ensure_self_profile(user_id)).subject
        else:
            subject = await self._repository.get_subject(user_id, subject_id)
            if subject is None:
                raise HealthProfileNotFoundError('健康主体不存在')
        return await self._read_document_observations(
            user_id,
            subject.subject_id,
            document_id=document_id,
            cursor=cursor,
            limit=limit,
            view=view,
        )

    async def _read_document_observations(
        self,
        user_id,
        subject_id,
        *,
        document_id=None,
        cursor=None,
        limit=20,
        view='standard',
    ):
        self._document_reader_started = True
        try:
            if self._document_views is not None:
                return await self._document_views.list_observations(
                    user_id,
                    subject_id,
                    document_id=document_id,
                    cursor=cursor,
                    limit=limit,
                    view=view,
                )
            if view == 'complete':
                return {'items': [], 'documents': [], 'nextCursor': None, 'status': 'unavailable'}
            if self._document_observations is None:
                return {'items': [], 'nextCursor': None}
            return await self._document_observations.list_observations(
                user_id,
                subject_id,
                document_id=document_id,
                cursor=cursor,
                limit=limit,
            )
        except ProfileDocumentArchiveChangedError as error:
            raise HealthProfileConflictError('Document material changed; restart the query') from error
        except ProfileDocumentArchiveReadError:
            return {
                'items': [],
                'nextCursor': None,
                'status': 'unavailable',
                **({'documents': []} if view == 'complete' else {}),
            }
        except (TypeError, ValueError) as error:
            raise HealthProfileValidationError(str(error)) from error

    async def list_subjects(
        self,
        user_id: str,
        *,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """列出当前用户隔离范围内的稳定健康主体。"""
        user_id = _required_text('user_id', user_id)
        if not isinstance(include_archived, bool):
            raise HealthProfileValidationError(
                'includeArchived 必须为 boolean',
            )
        await self.ensure_self_profile(user_id)
        subjects = await self._repository.list_subjects(
            user_id,
            include_archived=include_archived,
        )
        subjects = tuple(
            sorted(
                subjects,
                key=lambda subject: (
                    subject.kind is not SubjectKind.SELF,
                    subject.created_at,
                    subject.subject_id,
                ),
            )
        )
        return {
            'userId': user_id,
            'subjects': [public_subject(subject) for subject in subjects],
        }

    async def upsert_subject(
        self,
        user_id: str,
        request_id: str,
        *,
        subject_id: str | None = None,
        kind: str | SubjectKind | None = None,
        display_name: str | None | object = _MISSING,
        relationship: (Mapping[str, Any] | SubjectRelationship | None | object) = _MISSING,
        expected_subject_version: int | None = None,
    ) -> SubjectUpsertResult:
        """幂等创建非本人主体，或按版本更新既有主体。"""
        user_id = _required_text('user_id', user_id)
        request_id = _required_text('request_id', request_id)
        subject_id = _optional_text(subject_id, name='subject_id')
        normalized_kind = _normalize_kind(kind)
        normalized_display_name = _normalize_optional_change(
            display_name,
            name='displayName',
        )
        normalized_relationship = _normalize_relationship_change(relationship)
        _validate_expected_version(
            expected_subject_version,
            required=subject_id is not None,
        )
        if subject_id is None and expected_subject_version is not None:
            raise HealthProfileValidationError(
                '创建健康主体时不能提供 expectedSubjectVersion',
            )

        fingerprint = _request_fingerprint(
            {
                'subjectId': subject_id,
                'kind': (None if normalized_kind is None else normalized_kind.value),
                'displayName': _fingerprint_value(normalized_display_name),
                'relationship': _fingerprint_relationship(normalized_relationship),
                'expectedSubjectVersion': expected_subject_version,
            }
        )
        replay = await self._repository.get_event_by_request(
            user_id,
            request_id,
        )
        if replay is not None:
            return _replay_subject_result(replay, fingerprint=fingerprint)

        lease_subject = subject_id or 'subject-directory'
        async with self._mutation_lease(
            user_id,
            lease_subject,
            request_id,
        ):
            replay = await self._repository.get_event_by_request(
                user_id,
                request_id,
            )
            if replay is not None:
                return _replay_subject_result(
                    replay,
                    fingerprint=fingerprint,
                )
            await self.ensure_self_profile(user_id)
            if subject_id is None:
                return await self._create_subject(
                    user_id=user_id,
                    request_id=request_id,
                    fingerprint=fingerprint,
                    kind=normalized_kind,
                    display_name=normalized_display_name,
                    relationship=normalized_relationship,
                )
            return await self._update_subject(
                user_id=user_id,
                request_id=request_id,
                fingerprint=fingerprint,
                subject_id=subject_id,
                kind=normalized_kind,
                display_name=normalized_display_name,
                relationship=normalized_relationship,
                expected_version=expected_subject_version,
            )

    async def list_facts(
        self,
        user_id: str,
        *,
        subject_id: str | None = None,
        categories: Sequence[str] | None = None,
        statuses: Sequence[str] | None = None,
        fact_types: Sequence[str] | None = None,
        effective_from: str | None = None,
        effective_to: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """按稳定顺序分页读取不含删除墓碑的健康事实。"""
        user_id = _required_text('user_id', user_id)
        subject_id = _optional_text(subject_id, name='subject_id')
        normalized_categories = _normalize_categories(categories)
        normalized_statuses = _normalize_fact_statuses(statuses)
        normalized_fact_types = _normalize_text_list(
            'factTypes',
            fact_types,
        )
        from_time = _parse_optional_datetime(
            'effectiveFrom',
            effective_from,
        )
        to_time = _parse_optional_datetime('effectiveTo', effective_to)
        if from_time is not None and to_time is not None and from_time > to_time:
            raise HealthProfileValidationError(
                'effectiveFrom 不能晚于 effectiveTo',
            )
        limit = _validate_limit(limit)

        subject, view = await self._get_subject_and_view(user_id, subject_id)
        basic_info = (
            await self._basic_info.read(user_id, subject_id=subject.subject_id, as_of=self._aware_now())
            if self._basic_info is not None
            else None
        )
        filter_hash = _request_fingerprint(
            {
                'userId': user_id,
                'subjectId': subject.subject_id,
                'categories': [item.value for item in normalized_categories],
                'statuses': [item.value for item in normalized_statuses],
                'factTypes': list(normalized_fact_types),
                'effectiveFrom': _isoformat(from_time) if from_time else None,
                'effectiveTo': _isoformat(to_time) if to_time else None,
            }
        )
        after = None
        if cursor is not None:
            try:
                decoded = decode_cursor('health-profile-facts', cursor)
                if decoded.get('filterHash') != filter_hash:
                    raise ValueError('cursor 与当前查询条件不匹配')
                if basic_info is not None:
                    revision, epoch = decoded.get('basicInfoRevision'), decoded.get('lifecycleEpoch')
                    if type(revision) is not int or revision < 0 or type(epoch) is not int or epoch < 1:
                        raise ValueError('cursor 缺少合法的画像修订或生命周期绑定')
                    if revision != basic_info.revision or epoch != basic_info.lifecycle_epoch:
                        raise HealthProfileConflictError('基础信息已变化，请从第一页重新查询')
                after = (
                    _parse_datetime('cursor.start', decoded.get('start')) if decoded.get('start') is not None else None,
                    _required_text('cursor.factId', decoded.get('factId')),
                )
            except (TypeError, ValueError) as error:
                raise HealthProfileValidationError(str(error)) from error

        query = dict(
            categories=normalized_categories,
            statuses=normalized_statuses,
            fact_types=normalized_fact_types,
            effective_from=from_time,
            effective_to=to_time,
            after=after,
            limit=limit + 1,
        )
        page = (
            await self._basic_info.list_current_fact_page(user_id, subject.subject_id, snapshot=basic_info, **query)
            if self._basic_info is not None
            else await self._repository.list_fact_page(user_id, subject.subject_id, **query)
        )
        if self._basic_info is not None:
            latest = await self._basic_info.read(user_id, subject_id=subject.subject_id, as_of=basic_info.as_of)
            if latest.revision != basic_info.revision or latest.lifecycle_epoch != basic_info.lifecycle_epoch:
                raise HealthProfileConflictError('基础信息已变化，请从第一页重新查询')
        items = page[:limit]
        next_cursor = None
        if len(page) > limit and items:
            last = items[-1]
            next_cursor = encode_cursor(
                'health-profile-facts',
                {
                    'filterHash': filter_hash,
                    'start': _isoformat(last.effective_period.start),
                    'factId': last.fact_id,
                    **(
                        {'basicInfoRevision': basic_info.revision, 'lifecycleEpoch': basic_info.lifecycle_epoch}
                        if basic_info is not None
                        else {}
                    ),
                },
            )
        return {
            'userId': user_id,
            'subjectId': subject.subject_id,
            'profileVersion': view.profile_version,
            'items': [public_fact(fact) for fact in items],
            'nextCursor': next_cursor,
        }

    async def list_changes(
        self,
        user_id: str,
        *,
        subject_id: str | None = None,
        event_types: Sequence[str] | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """分页读取不含事实值和模型 reasoning 的画像审计事件。"""
        user_id = _required_text('user_id', user_id)
        subject_id = _optional_text(subject_id, name='subject_id')
        normalized_event_types = _normalize_event_types(event_types)
        limit = _validate_limit(limit)
        subject, _ = await self._get_subject_and_view(user_id, subject_id)
        filter_hash = _request_fingerprint(
            {
                'userId': user_id,
                'subjectId': subject.subject_id,
                'eventTypes': list(normalized_event_types),
            }
        )
        before_sequence = None
        if cursor is not None:
            try:
                decoded = decode_cursor('health-profile-events', cursor)
                if decoded.get('filterHash') != filter_hash:
                    raise ValueError('cursor 与当前查询条件不匹配')
                before_sequence = decoded.get('sequence')
                if isinstance(before_sequence, bool) or not isinstance(before_sequence, int) or before_sequence < 1:
                    raise ValueError('cursor.sequence 无效')
            except (TypeError, ValueError) as error:
                raise HealthProfileValidationError(str(error)) from error
        page = await self._repository.list_event_page(
            user_id,
            subject.subject_id,
            event_types=normalized_event_types,
            before_sequence=before_sequence,
            limit=limit + 1,
        )
        items = page[:limit]
        next_cursor = None
        if len(page) > limit and items:
            next_cursor = encode_cursor(
                'health-profile-events',
                {
                    'filterHash': filter_hash,
                    'sequence': items[-1].sequence,
                },
            )
        return {
            'userId': user_id,
            'subjectId': subject.subject_id,
            'items': [_public_event(event) for event in items],
            'nextCursor': next_cursor,
        }

    async def resolve_fact(
        self,
        user_id: str,
        subject_id: str,
        fact_id: str,
        decision: str,
        *,
        replacement: Mapping[str, Any] | None = None,
        reason: str | None = None,
        expected_fact_version: int,
        expected_profile_version: int,
        request_id: str,
    ) -> dict[str, Any]:
        """确认、拒绝或更正 pending/disputed 健康事实。"""
        user_id = _required_text('user_id', user_id)
        subject_id = _required_text('subject_id', subject_id)
        fact_id = _required_text('fact_id', fact_id)
        request_id = _required_text('request_id', request_id)
        decision = _required_text('decision', decision).lower()
        if decision not in {'confirm', 'reject', 'replace'}:
            raise HealthProfileValidationError(
                'decision 必须为 confirm、reject 或 replace',
            )
        normalized_reason = _optional_limited_text(
            reason,
            name='reason',
            max_length=1000,
        )
        normalized_replacement = _normalize_fact_replacement(replacement)
        if decision == 'replace' and normalized_replacement is None:
            raise HealthProfileValidationError(
                'decision=replace 时必须提供 replacement',
            )
        if decision != 'replace' and replacement is not None:
            raise HealthProfileValidationError(
                '仅 decision=replace 时允许提供 replacement',
            )
        expected_fact_version = _positive_integer(
            'expectedFactVersion',
            expected_fact_version,
        )
        expected_profile_version = _positive_integer(
            'expectedProfileVersion',
            expected_profile_version,
        )
        fingerprint = _request_fingerprint(
            {
                'operation': 'fact_resolve',
                'subjectId': subject_id,
                'factId': fact_id,
                'decision': decision,
                'replacement': (None if normalized_replacement is None else normalized_replacement.fingerprint_value()),
                'reason': normalized_reason,
                'expectedFactVersion': expected_fact_version,
                'expectedProfileVersion': expected_profile_version,
            }
        )
        replay = await self._repository.get_event_by_request(
            user_id,
            request_id,
        )
        if replay is not None:
            return await self._replay_resolve_result(
                replay,
                fingerprint=fingerprint,
            )

        async with self._mutation_lease(user_id, subject_id, request_id):
            replay = await self._repository.get_event_by_request(
                user_id,
                request_id,
            )
            if replay is not None:
                return await self._replay_resolve_result(
                    replay,
                    fingerprint=fingerprint,
                )
            _, view = await self._get_subject_and_view(user_id, subject_id)
            fact = await self._repository.get_fact(
                user_id,
                subject_id,
                fact_id,
            )
            if fact is None:
                raise HealthProfileNotFoundError('健康事实不存在')
            if (
                normalized_replacement is not None
                and normalized_replacement.effective_period.start is None
                and fact.category is not FactCategory.DEMOGRAPHIC
            ):
                raise HealthProfileValidationError('replacement.effectivePeriod.start 不能为空')
            if (
                decision == 'confirm'
                and fact.category is not FactCategory.DEMOGRAPHIC
                and (fact.effective_period.start is None or fact.value is None)
            ):
                raise HealthProfileValidationError('请使用更正补充实际观察时间和结果值')
            recovered = fact.last_request_id == request_id and fact.last_request_fingerprint == fingerprint
            if not recovered:
                if view.profile_version != expected_profile_version:
                    raise HealthProfileConflictError(
                        'expectedProfileVersion 与当前画像版本不一致',
                    )
                if fact.version != expected_fact_version:
                    raise HealthProfileConflictError(
                        'expectedFactVersion 与当前事实版本不一致',
                    )
                if fact.status not in {
                    FactStatus.PENDING,
                    FactStatus.DISPUTED,
                } and not (fact.status is FactStatus.ACTIVE and fact.category is FactCategory.DEMOGRAPHIC):
                    raise HealthProfileConflictError(
                        '该健康事实已不处于待确认或争议状态',
                    )

            mutation_fence = None
            if self._basic_info is not None:
                from .basic_info import field_for_fact

                basic_field = field_for_fact(fact)
                if basic_field is not None:
                    mutation_fence = await self._basic_info.guard_mutation(
                        user_id,
                        subject_id,
                        basic_field,
                        request_id,
                        deleted=decision == 'reject',
                        fact=fact,
                        request_fingerprint=fingerprint,
                    )
            now = mutation_fence['confirmedAt'] if mutation_fence is not None else self._aware_now()
            if decision == 'replace':
                assert normalized_replacement is not None
                result_fact, changed_fact = await self._replace_pending_fact(
                    fact=fact,
                    replacement=normalized_replacement,
                    request_id=request_id,
                    fingerprint=fingerprint,
                    expected_fact_version=expected_fact_version,
                    recovered=recovered,
                    now=now,
                    lifecycle_epoch=mutation_fence['lifecycleEpoch'] if mutation_fence is not None else None,
                )
                event_type = 'fact_superseded'
                reason_code = 'user_correction'
            else:
                target_status = FactStatus.ACTIVE if decision == 'confirm' else FactStatus.RETRACTED
                changed_fact = (
                    fact
                    if recovered
                    else fact.model_copy(
                        update={
                            'status': target_status,
                            'assertion_level': (
                                AssertionLevel.USER_CONFIRMED if decision == 'confirm' else fact.assertion_level
                            ),
                            'verification_status': (
                                VerificationStatus.CONFIRMED if decision == 'confirm' else fact.verification_status
                            ),
                            'confidence': 1.0 if decision == 'confirm' else fact.confidence,
                            'updated_at': now,
                            'version': fact.version + 1,
                            'last_request_id': request_id,
                            'last_request_fingerprint': fingerprint,
                            'basic_info_lifecycle_epoch': (
                                mutation_fence['lifecycleEpoch']
                                if mutation_fence is not None
                                else fact.basic_info_lifecycle_epoch
                            ),
                        }
                    )
                )
                if not recovered:
                    stored = await self._repository.replace_fact(
                        changed_fact,
                        expected_version=expected_fact_version,
                        allowed_statuses=(
                            FactStatus.PENDING,
                            FactStatus.DISPUTED,
                            *((FactStatus.ACTIVE,) if fact.category is FactCategory.DEMOGRAPHIC else ()),
                        ),
                    )
                    if stored is None:
                        raise HealthProfileConflictError(
                            '健康事实已被其他请求修改',
                        )
                    changed_fact = stored
                result_fact = changed_fact
                event_type = 'fact_confirmed' if decision == 'confirm' else 'fact_retracted'
                reason_code = 'user_confirmation' if decision == 'confirm' else 'user_rejection'

            if mutation_fence is not None and recovered:
                changed_fact = await self._tag_recovered_manual_fact(changed_fact, mutation_fence['lifecycleEpoch'])
                if result_fact.fact_id == changed_fact.fact_id:
                    result_fact = changed_fact
                else:
                    result_fact = await self._tag_recovered_manual_fact(result_fact, mutation_fence['lifecycleEpoch'])
            if self._basic_info is not None and decision != 'reject':
                await self._basic_info.accept_manual_fact(result_fact, request_id, fence=mutation_fence)
            updated_view = await self._advance_view_after_fact_mutation(
                user_id=user_id,
                subject_id=subject_id,
                expected_profile_version=expected_profile_version,
                request_id=request_id,
                updated_at=now,
            )
            if self._basic_info is not None:
                updated_view = await self._basic_info.publish_governance(user_id, subject_id) or updated_view
            event = await self._repository.append_event(
                ProfileEvent(
                    event_id=self._id_factory('event'),
                    user_id=user_id,
                    subject_id=subject_id,
                    sequence=updated_view.profile_version,
                    event_type=event_type,
                    fact_id=fact_id,
                    previous_fact_version=expected_fact_version,
                    new_fact_version=result_fact.version,
                    reason_code=reason_code,
                    actor=ProfileActor(type='user', id=user_id),
                    request_id=request_id,
                    source_ref={
                        'sourceType': 'manual',
                        'reason': normalized_reason,
                        'replacementFactId': (result_fact.fact_id if decision == 'replace' else None),
                    },
                    operation_result={
                        'operation': 'fact_resolve',
                        'requestFingerprint': fingerprint,
                        'decision': decision,
                        'previousFactId': fact_id,
                        'resultFactId': result_fact.fact_id,
                        'resultFactVersion': result_fact.version,
                        'profileVersion': updated_view.profile_version,
                    },
                    created_at=now,
                )
            )
            return await self._replay_resolve_result(
                event,
                fingerprint=fingerprint,
            )

    async def delete_fact(
        self,
        user_id: str,
        subject_id: str,
        fact_id: str,
        *,
        delete_scope: str,
        reason: str | None = None,
        expected_fact_version: int,
        expected_profile_version: int,
        request_id: str,
    ) -> dict[str, Any]:
        """立即隐藏并清除画像事实值，保留非敏感删除墓碑。"""
        user_id = _required_text('user_id', user_id)
        subject_id = _required_text('subject_id', subject_id)
        fact_id = _required_text('fact_id', fact_id)
        request_id = _required_text('request_id', request_id)
        if _required_text('delete_scope', delete_scope) != 'fact':
            raise HealthProfileValidationError(
                'deleteScope 当前仅支持 fact',
            )
        normalized_reason = _optional_limited_text(
            reason,
            name='reason',
            max_length=1000,
        )
        expected_fact_version = _positive_integer(
            'expectedFactVersion',
            expected_fact_version,
        )
        expected_profile_version = _positive_integer(
            'expectedProfileVersion',
            expected_profile_version,
        )
        fingerprint = _request_fingerprint(
            {
                'operation': 'fact_delete',
                'subjectId': subject_id,
                'factId': fact_id,
                'deleteScope': 'fact',
                'reason': normalized_reason,
                'expectedFactVersion': expected_fact_version,
                'expectedProfileVersion': expected_profile_version,
            }
        )
        replay = await self._repository.get_event_by_request(
            user_id,
            request_id,
        )
        if replay is not None:
            return _replay_delete_result(replay, fingerprint=fingerprint)

        async with self._mutation_lease(user_id, subject_id, request_id):
            replay = await self._repository.get_event_by_request(
                user_id,
                request_id,
            )
            if replay is not None:
                return _replay_delete_result(replay, fingerprint=fingerprint)
            _, view = await self._get_subject_and_view(user_id, subject_id)
            fact = await self._repository.get_fact(
                user_id,
                subject_id,
                fact_id,
            )
            tombstone = None
            if fact is None:
                tombstone = await self._repository.get_deleted_fact_tombstone(
                    user_id,
                    subject_id,
                    fact_id,
                )
                if (
                    tombstone is None
                    or tombstone.get('deletionRequestId') != request_id
                    or tombstone.get('deletionRequestFingerprint') != fingerprint
                ):
                    raise HealthProfileNotFoundError('健康事实不存在')
            else:
                if view.profile_version != expected_profile_version:
                    raise HealthProfileConflictError(
                        'expectedProfileVersion 与当前画像版本不一致',
                    )
                if fact.version != expected_fact_version:
                    raise HealthProfileConflictError(
                        'expectedFactVersion 与当前事实版本不一致',
                    )

            now = self._aware_now()
            if tombstone is None:
                if self._basic_info is not None:
                    from .basic_info import field_for_fact

                    basic_field = field_for_fact(fact)
                    if basic_field is not None:
                        await self._basic_info.guard_mutation(
                            user_id,
                            subject_id,
                            basic_field,
                            request_id,
                            deleted=True,
                            erase=True,
                            fact=fact,
                        )
                previous = await self._repository.mark_fact_deleted(
                    user_id=user_id,
                    subject_id=subject_id,
                    fact_id=fact_id,
                    expected_version=expected_fact_version,
                    request_id=request_id,
                    request_fingerprint=fingerprint,
                    deleted_at=now,
                )
                if previous is None:
                    raise HealthProfileConflictError(
                        '健康事实已被其他请求修改',
                    )
                deleted_at = now
            else:
                deleted_at = _parse_datetime(
                    'deletedAt',
                    tombstone.get('deletedAt'),
                )
            updated_view = await self._advance_view_after_fact_mutation(
                user_id=user_id,
                subject_id=subject_id,
                expected_profile_version=expected_profile_version,
                request_id=request_id,
                updated_at=now,
            )
            if self._basic_info is not None:
                updated_view = await self._basic_info.publish_governance(user_id, subject_id) or updated_view
            event = await self._repository.append_event(
                ProfileEvent(
                    event_id=self._id_factory('event'),
                    user_id=user_id,
                    subject_id=subject_id,
                    sequence=updated_view.profile_version,
                    event_type='fact_deleted',
                    fact_id=fact_id,
                    previous_fact_version=expected_fact_version,
                    reason_code='user_deletion',
                    actor=ProfileActor(type='user', id=user_id),
                    request_id=request_id,
                    source_ref={
                        'sourceType': 'manual',
                    },
                    operation_result={
                        'operation': 'fact_delete',
                        'requestFingerprint': fingerprint,
                        'factId': fact_id,
                        'status': 'deleted',
                        'profileVersion': updated_view.profile_version,
                        'deletedAt': _isoformat(deleted_at),
                    },
                    created_at=now,
                )
            )
            return _replay_delete_result(event, fingerprint=fingerprint)

    async def _get_subject_and_view(
        self,
        user_id: str,
        subject_id: str | None,
    ) -> tuple[HealthSubject, HealthProfileView]:
        if subject_id is None:
            bootstrap = await self.ensure_self_profile(user_id)
            return bootstrap.subject, bootstrap.view
        await self._repository.ensure_indexes()
        subject = await self._repository.get_subject(user_id, subject_id)
        if subject is None:
            raise HealthProfileNotFoundError('健康主体不存在')
        view = await self._repository.get_view(user_id, subject_id)
        if view is None:
            view = await self._repository.ensure_initial_view(
                HealthProfileView(
                    user_id=user_id,
                    subject_id=subject_id,
                    subject_summary={'displayName': subject.display_name},
                    updated_at=self._aware_now(),
                ),
            )
        return subject, view

    async def _replace_pending_fact(
        self,
        *,
        fact: HealthFact,
        replacement: FactReplacement,
        request_id: str,
        fingerprint: str,
        expected_fact_version: int,
        recovered: bool,
        now: datetime,
        lifecycle_epoch: int | None = None,
    ) -> tuple[HealthFact, HealthFact]:
        if lifecycle_epoch is None:
            lifecycle_epoch = fact.basic_info_lifecycle_epoch
        replacement_fact_id = (
            fact.replacement_fact_id if recovered and fact.replacement_fact_id is not None else self._id_factory('fact')
        )
        changed_fact = (
            fact
            if recovered
            else fact.model_copy(
                update={
                    'status': FactStatus.SUPERSEDED,
                    'updated_at': now,
                    'version': fact.version + 1,
                    'last_request_id': request_id,
                    'last_request_fingerprint': fingerprint,
                    'replacement_fact_id': replacement_fact_id,
                    'basic_info_lifecycle_epoch': lifecycle_epoch,
                }
            )
        )
        if not recovered:
            stored = await self._repository.replace_fact(
                changed_fact,
                expected_version=expected_fact_version,
                allowed_statuses=(
                    FactStatus.PENDING,
                    FactStatus.DISPUTED,
                    *((FactStatus.ACTIVE,) if fact.category is FactCategory.DEMOGRAPHIC else ()),
                ),
            )
            if stored is None:
                raise HealthProfileConflictError(
                    '健康事实已被其他请求修改',
                )
            changed_fact = stored

        candidate = HealthFact(
            fact_id=replacement_fact_id,
            user_id=fact.user_id,
            subject_id=fact.subject_id,
            fact_type=fact.fact_type,
            category=fact.category,
            display_name=fact.display_name,
            value_type=replacement.value_type,
            value=replacement.value,
            interpretation=None,
            reference_range=None,
            assertion_level=AssertionLevel.USER_CONFIRMED,
            verification_status=VerificationStatus.CONFIRMED,
            confidence=1.0,
            effective_period=replacement.effective_period,
            status=FactStatus.ACTIVE,
            sources=(
                FactSource(
                    source_type='manual',
                    source_id=request_id,
                    source_revision=1,
                    observed_at=now,
                ),
            ),
            created_at=now,
            updated_at=now,
            creation_request_id=request_id,
            creation_request_slot='replacement',
            creation_request_fingerprint=fingerprint,
            basic_info_lifecycle_epoch=lifecycle_epoch,
        )
        result_fact, _ = await self._repository.create_fact(candidate)
        if result_fact.creation_request_fingerprint != fingerprint:
            raise HealthProfileConflictError(
                'requestId 已被不同的事实更正请求使用',
            )
        return result_fact, changed_fact

    async def _tag_recovered_manual_fact(self, fact, lifecycle_epoch):
        if fact.basic_info_lifecycle_epoch == lifecycle_epoch:
            return fact
        if fact.basic_info_lifecycle_epoch is not None:
            raise HealthProfileConflictError('事实不属于原操作生命周期')
        stored = await self._repository.replace_fact(
            fact.model_copy(update={'basic_info_lifecycle_epoch': lifecycle_epoch}),
            expected_version=fact.version,
            allowed_statuses=(fact.status,),
        )
        if stored is None:
            raise HealthProfileConflictError('健康事实已被其他请求修改')
        return stored

    async def _advance_view_after_fact_mutation(
        self,
        *,
        user_id: str,
        subject_id: str,
        expected_profile_version: int,
        request_id: str,
        updated_at: datetime,
    ) -> HealthProfileView:
        facts = await self._repository.list_facts(
            user_id,
            subject_id,
            statuses=tuple(_PUBLIC_FACT_STATUSES),
        )
        active = tuple(fact for fact in facts if fact.status is FactStatus.ACTIVE)
        category_counts: dict[str, int] = {}
        for fact in active:
            name = fact.category.value
            category_counts[name] = category_counts.get(name, 0) + 1
        key_fact_ids = tuple(fact.fact_id for fact in active if fact.interpretation in {'low', 'high', 'critical'})[:20]
        pending_count = sum(fact.status in {FactStatus.PENDING, FactStatus.DISPUTED} for fact in facts)
        source_times = [source.observed_at or fact.effective_period.start for fact in active for source in fact.sources]
        view = await self._repository.advance_fact_view(
            user_id=user_id,
            subject_id=subject_id,
            expected_profile_version=expected_profile_version,
            request_id=request_id,
            category_counts=category_counts,
            key_fact_ids=key_fact_ids,
            pending_confirmation_count=pending_count,
            last_source_at=max((item for item in source_times if item is not None), default=None),
            updated_at=updated_at,
        )
        if view is None:
            raise HealthProfileConflictError(
                'expectedProfileVersion 与当前画像版本不一致',
            )
        return view

    async def _replay_resolve_result(
        self,
        event: ProfileEvent,
        *,
        fingerprint: str,
    ) -> dict[str, Any]:
        operation = event.operation_result
        if not isinstance(operation, Mapping) or operation.get('operation') != 'fact_resolve':
            raise HealthProfileConflictError(
                'requestId 已被其他健康画像操作使用',
            )
        if operation.get('requestFingerprint') != fingerprint:
            raise HealthProfileConflictError(
                'requestId 已被不同的请求内容使用',
            )
        result_fact_id = operation.get('resultFactId')
        result_fact = await self._repository.get_fact(
            event.user_id,
            event.subject_id,
            result_fact_id,
        )
        if result_fact is None or result_fact.version != operation.get('resultFactVersion'):
            raise HealthProfileConflictError(
                '该幂等请求已完成，但结果事实后来已发生变化，请重新读取',
            )
        return {
            'decision': operation['decision'],
            'previousFactId': operation['previousFactId'],
            'fact': public_fact(result_fact),
            'profileVersion': operation['profileVersion'],
        }

    async def _create_subject(
        self,
        *,
        user_id: str,
        request_id: str,
        fingerprint: str,
        kind: SubjectKind | None,
        display_name: str | None | object,
        relationship: SubjectRelationship | None | object,
    ) -> SubjectUpsertResult:
        if kind is None:
            raise HealthProfileValidationError('创建时缺少必填参数: kind')
        if kind in {SubjectKind.SELF, SubjectKind.UNKNOWN}:
            raise HealthProfileValidationError(
                '本接口不能创建 self 或 unknown 主体',
            )
        if relationship is _MISSING or relationship is None:
            raise HealthProfileValidationError(
                '非本人主体必须提供 relationship',
            )
        assert isinstance(relationship, SubjectRelationship)
        _validate_relationship(kind, relationship)
        if display_name is _MISSING:
            display_name = None

        now = self._aware_now()
        candidate = HealthSubject(
            subject_id=self._id_factory('subject'),
            user_id=user_id,
            kind=kind,
            display_name=display_name,
            relationship=relationship,
            created_at=now,
            updated_at=now,
            creation_request_id=request_id,
            creation_request_fingerprint=fingerprint,
            last_request_id=request_id,
            last_request_fingerprint=fingerprint,
        )
        subject, inserted = await self._repository.create_subject(candidate)
        if subject.creation_request_fingerprint != fingerprint:
            raise HealthProfileConflictError(
                'requestId 已被不同的主体创建请求使用',
            )
        view = await self._repository.ensure_initial_view(
            HealthProfileView(
                user_id=user_id,
                subject_id=subject.subject_id,
                subject_summary={'displayName': subject.display_name},
                updated_at=subject.updated_at,
            )
        )
        event = ProfileEvent(
            event_id=self._id_factory('event'),
            user_id=user_id,
            subject_id=subject.subject_id,
            sequence=view.profile_version,
            event_type='subject_created',
            reason_code='subject_upsert_create',
            actor=ProfileActor(type='user', id=user_id),
            request_id=request_id,
            operation_result=_operation_result(
                subject,
                created=True,
                fingerprint=fingerprint,
            ),
            created_at=subject.created_at,
        )
        stored_event = await self._repository.append_event(event)
        if not inserted:
            return _replay_subject_result(stored_event, fingerprint=fingerprint)
        return SubjectUpsertResult(subject=subject, created=True)

    async def _update_subject(
        self,
        *,
        user_id: str,
        request_id: str,
        fingerprint: str,
        subject_id: str,
        kind: SubjectKind | None,
        display_name: str | None | object,
        relationship: SubjectRelationship | None | object,
        expected_version: int | None,
    ) -> SubjectUpsertResult:
        assert expected_version is not None
        existing = await self._repository.get_subject(user_id, subject_id)
        if existing is None:
            raise HealthProfileNotFoundError('健康主体不存在')
        if kind is not None and kind is not existing.kind:
            raise HealthProfileValidationError('健康主体 kind 创建后不可修改')

        next_display_name = existing.display_name if display_name is _MISSING else display_name
        next_relationship = existing.relationship if relationship is _MISSING else relationship
        if existing.kind is SubjectKind.SELF:
            if relationship is not _MISSING and relationship is not None:
                raise HealthProfileValidationError(
                    'self 主体不能设置 relationship',
                )
            next_relationship = None
        else:
            if next_relationship is None:
                raise HealthProfileValidationError(
                    '非本人主体必须保留 relationship',
                )
            assert isinstance(next_relationship, SubjectRelationship)
            _validate_relationship(existing.kind, next_relationship)

        now = self._aware_now()
        mutation_fence = None
        if self._basic_info is not None and display_name is not _MISSING:
            if existing.version != expected_version and existing.last_request_id != request_id:
                raise HealthProfileConflictError('expectedSubjectVersion 与当前主体版本不一致')
            mutation_fence = await self._basic_info.guard_mutation(
                user_id,
                subject_id,
                'name',
                request_id,
                deleted=next_display_name is None,
                request_fingerprint=fingerprint,
            )
            now = mutation_fence['confirmedAt']
        candidate = existing.model_copy(
            update={
                'display_name': next_display_name,
                'relationship': next_relationship,
                'updated_at': now,
                'version': existing.version + 1,
                'last_request_id': request_id,
                'last_request_fingerprint': fingerprint,
            }
        )
        updated = await self._repository.update_subject(
            candidate,
            expected_version=expected_version,
        )
        if updated is None:
            current = await self._repository.get_subject(user_id, subject_id)
            if (
                current is not None
                and current.last_request_id == request_id
                and current.last_request_fingerprint == fingerprint
            ):
                updated = current
            else:
                raise HealthProfileConflictError(
                    'expectedSubjectVersion 与当前主体版本不一致',
                )
        if self._basic_info is not None and display_name is not _MISSING:
            await self._basic_info.accept_manual_name(
                user_id, subject_id, next_display_name, request_id, fence=mutation_fence
            )
        view = await self._repository.advance_subject_view(
            updated,
            request_id=request_id,
            updated_at=now,
        )
        if self._basic_info is not None:
            view = await self._basic_info.publish_governance(user_id, subject_id) or view
        event = await self._repository.append_event(
            ProfileEvent(
                event_id=self._id_factory('event'),
                user_id=user_id,
                subject_id=subject_id,
                sequence=view.profile_version,
                event_type='subject_updated',
                reason_code='subject_upsert_update',
                actor=ProfileActor(type='user', id=user_id),
                request_id=request_id,
                operation_result=_operation_result(
                    updated,
                    created=False,
                    fingerprint=fingerprint,
                ),
                created_at=now,
            )
        )
        return _replay_subject_result(event, fingerprint=fingerprint)

    async def _get_interpretation(
        self,
        *,
        user_id: str,
        subject_id: str,
        facts,
        requested: bool,
        generated_at: datetime,
        projection: dict[str, Any],
    ) -> dict[str, Any]:
        if not requested:
            return {
                'evidence': {
                    'status': 'not_requested',
                    'knowledgeBaseRevision': None,
                    'generatedAt': None,
                    'items': [],
                },
                'insights': [],
                'complete': False,
            }
        payloads = evidence_fact_payloads(facts)
        clinical_context = {key: projection['overview'][key] for key in ('age', 'gender', 'bmi')}

        async def generate():
            if not payloads or self._agent_hub is None:
                return {
                    'evidence': {
                        'status': 'empty' if not payloads else 'unavailable',
                        'knowledgeBaseRevision': None,
                        'generatedAt': _isoformat(generated_at),
                        'items': [],
                    },
                    'insights': [],
                    'complete': not payloads,
                }
            context = AgentRunContext.create(
                user_id=user_id,
                session_id=f'health-profile:{subject_id}',
                subject_id=subject_id,
            )
            result = await self._agent_hub.invoke(
                PROFILE_EVIDENCE_CAPABILITY,
                AgentRequest(
                    {
                        'facts': payloads,
                        'profileVersion': projection['profileVersion'],
                        'clinicalContext': clinical_context,
                    }
                ),
                context,
            )
            return {
                'evidence': result.data.get('evidence'),
                'insights': result.data.get('insights', []),
                'complete': result.data.get('complete', False),
            }

        try:
            if self._evidence_results is None:
                data = await generate()
            else:
                key = evidence_key(
                    user_id=user_id,
                    subject_id=subject_id,
                    profile_version=projection['profileVersion'],
                    facts=payloads,
                    clinical_context=clinical_context,
                    revision=self._evidence_revision,
                )
                data = await self._evidence_results.get_or_create(
                    key=key,
                    user_id=user_id,
                    subject_id=subject_id,
                    profile_version=projection['profileVersion'],
                    factory=generate,
                )
            evidence = data.get('evidence')
            if not isinstance(evidence, Mapping):
                raise TypeError('profile evidence graph returned invalid data')
            normalized = dict(evidence)
            normalized.setdefault('generatedAt', _isoformat(generated_at))
            apply_profile_insights(
                projection,
                data.get('insights', []),
                payloads,
            )
            return {
                'evidence': normalized,
                'insights': data.get('insights', []),
                'complete': data.get('complete', False),
            }
        except Exception as error:
            logger.warning(
                '用户健康画像循证检索不可用，subject_id=%s category=%s',
                subject_id,
                type(error).__name__,
            )
            return {
                'evidence': {
                    'status': 'unavailable',
                    'knowledgeBaseRevision': None,
                    'generatedAt': _isoformat(generated_at),
                    'items': [],
                },
                'insights': [],
                'complete': False,
            }

    @asynccontextmanager
    async def _mutation_lease(
        self,
        user_id: str,
        subject_id: str,
        request_id: str,
    ):
        if self._lease_manager is None:
            yield
            return
        thread_id = f'health-profile:{user_id}:{subject_id}'
        lease = await self._lease_manager.try_acquire(thread_id, request_id)
        if lease is None:
            raise HealthProfileConflictError(
                '该健康主体正在执行其他修改，请稍后重试',
            )
        try:
            yield
        finally:
            released = await self._lease_manager.release(lease)
            if not released:
                logger.warning(
                    '用户健康画像修改 lease 已失效: %s',
                    thread_id,
                )

    def _aware_now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError('clock must return datetime')
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('clock must return a timezone-aware datetime')
        return value.astimezone(timezone.utc)


def _normalize_categories(
    values: Sequence[str] | None,
) -> tuple[FactCategory, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise HealthProfileValidationError('categories 必须为 array[string]')
    normalized = set()
    for value in values:
        try:
            normalized.add(FactCategory(value))
        except (TypeError, ValueError) as error:
            raise HealthProfileValidationError(
                f'不支持的事实分类: {value!r}',
            ) from error
    return tuple(sorted(normalized, key=lambda item: item.value))


def _normalize_fact_statuses(
    values: Sequence[str] | None,
) -> tuple[FactStatus, ...]:
    if values is None:
        return _DEFAULT_FACT_STATUSES
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise HealthProfileValidationError('statuses 必须为 array[string]')
    normalized = set()
    for value in values:
        try:
            status = FactStatus(value)
        except (TypeError, ValueError) as error:
            raise HealthProfileValidationError(
                f'不支持的事实状态: {value!r}',
            ) from error
        if status not in _PUBLIC_FACT_STATUSES:
            raise HealthProfileValidationError(
                f'事实状态不能通过本接口读取: {status.value}',
            )
        normalized.add(status)
    if not normalized:
        raise HealthProfileValidationError('statuses 不能为空数组')
    return tuple(sorted(normalized, key=lambda item: item.value))


def _normalize_indicator_scope(value: str | Sequence[str]) -> frozenset[str]:
    allowed = frozenset({'all', 'abnormal', 'flagged', 'pending', 'unjudged'})
    if isinstance(value, (str, bytes)):
        parts = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, Mapping):
        parts = tuple(value)
    else:
        raise HealthProfileValidationError(
            'indicatorScope 必须为 string 或 array[string]',
        )
    normalized = set()
    for item in parts:
        text = _required_text('indicatorScope', item).casefold()
        if text not in allowed:
            raise HealthProfileValidationError(
                'indicatorScope 仅支持 all、abnormal、flagged、pending 或 unjudged',
            )
        normalized.add(text)
    if not normalized:
        raise HealthProfileValidationError('indicatorScope 不能为空数组')
    if 'all' in normalized:
        return frozenset({'all'})
    return frozenset(normalized)


def _normalize_fact_group_statuses(
    values: Sequence[str] | None,
) -> frozenset[FactStatus] | None:
    if values is None:
        return None
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise HealthProfileValidationError('factStatuses 必须为 array[string]')
    normalized = set()
    for value in values:
        try:
            status = FactStatus(value)
        except (TypeError, ValueError) as error:
            raise HealthProfileValidationError(
                f'不支持的事实状态: {value!r}',
            ) from error
        if status not in _DEFAULT_FACT_STATUSES:
            raise HealthProfileValidationError(
                f'事实状态不能通过本接口读取: {status.value}',
            )
        normalized.add(status)
    if not normalized:
        raise HealthProfileValidationError('factStatuses 不能为空数组')
    return frozenset(normalized)


def _normalize_text_list(
    name: str,
    values: Sequence[str] | None,
) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise HealthProfileValidationError(f'{name} 必须为 array[string]')
    return tuple(sorted(dict.fromkeys(_required_text(name, value) for value in values)))


def _normalize_event_types(
    values: Sequence[str] | None,
) -> tuple[str, ...]:
    if values is None:
        return tuple(sorted(_PUBLIC_EVENT_TYPES))
    normalized = _normalize_text_list('eventTypes', values)
    unsupported = set(normalized) - _PUBLIC_EVENT_TYPES
    if unsupported:
        raise HealthProfileValidationError(
            f'不支持的 eventTypes: {", ".join(sorted(unsupported))}',
        )
    if not normalized:
        raise HealthProfileValidationError('eventTypes 不能为空数组')
    return normalized


def _normalize_fact_replacement(
    value: Mapping[str, Any] | None,
) -> FactReplacement | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise HealthProfileValidationError('replacement 必须为 object')
    allowed = {'valueType', 'value', 'effectivePeriod'}
    if set(value) - allowed:
        raise HealthProfileValidationError('replacement 包含不支持的字段')
    missing = allowed - set(value)
    if missing:
        raise HealthProfileValidationError(
            f'replacement 缺少字段: {", ".join(sorted(missing))}',
        )
    try:
        value_type = FactValueType(value['valueType'])
    except (TypeError, ValueError) as error:
        raise HealthProfileValidationError(
            'replacement.valueType 不受支持',
        ) from error
    normalized_value = _validate_fact_value(value_type, value['value'])
    try:
        effective_period = EffectivePeriod.model_validate(
            value['effectivePeriod'],
        )
    except ValidationError as error:
        raise HealthProfileValidationError(
            'replacement.effectivePeriod 不合法',
        ) from error
    if effective_period.start is not None:
        _require_aware_datetime(
            'replacement.effectivePeriod.start',
            effective_period.start,
        )
    if effective_period.end is not None:
        _require_aware_datetime(
            'replacement.effectivePeriod.end',
            effective_period.end,
        )
    return FactReplacement(
        value_type=value_type,
        value=normalized_value,
        effective_period=effective_period,
    )


def _validate_fact_value(value_type: FactValueType, value: Any) -> Any:
    if value_type in {FactValueType.TEXT, FactValueType.CODED}:
        return _required_text('replacement.value', value)
    if value_type is FactValueType.BOOLEAN:
        if not isinstance(value, bool):
            raise HealthProfileValidationError(
                'boolean 类型的 replacement.value 必须为 boolean',
            )
        return value
    if value_type is FactValueType.QUANTITY:
        if not isinstance(value, Mapping):
            raise HealthProfileValidationError(
                'quantity 类型的 replacement.value 必须为 object',
            )
        number = value.get('number')
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise HealthProfileValidationError(
                'replacement.value.number 必须为 number',
            )
        unit = _required_text('replacement.value.unit', value.get('unit'))
        return {'number': number, 'unit': unit}
    if value_type is FactValueType.DATE:
        normalized = _required_text('replacement.value', value)
        try:
            datetime.fromisoformat(normalized.replace('Z', '+00:00'))
        except ValueError as error:
            raise HealthProfileValidationError(
                'date 类型的 replacement.value 必须为 ISO 8601 时间',
            ) from error
        return normalized
    if value_type in {FactValueType.PERIOD, FactValueType.OBJECT}:
        if not isinstance(value, Mapping):
            raise HealthProfileValidationError(
                f'{value_type.value} 类型的 replacement.value 必须为 object',
            )
        return dict(value)
    raise HealthProfileValidationError('replacement.valueType 不受支持')


def _validate_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise HealthProfileValidationError('limit 必须为 1 到 100 的整数')
    return value


def _parse_optional_datetime(name: str, value: str | None) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(name, value)


def _parse_datetime(name: str, value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError as error:
            raise HealthProfileValidationError(
                f'{name} 必须为带时区的 ISO 8601 时间',
            ) from error
    else:
        raise HealthProfileValidationError(
            f'{name} 必须为带时区的 ISO 8601 时间',
        )
    return _require_aware_datetime(name, parsed).astimezone(timezone.utc)


def _require_aware_datetime(name: str, value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HealthProfileValidationError(f'{name} 必须包含时区')
    return value


def _positive_integer(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HealthProfileValidationError(f'{name} 必须为正整数')
    return value


def _optional_limited_text(
    value: str | None,
    *,
    name: str,
    max_length: int,
) -> str | None:
    if value is None:
        return None
    normalized = _required_text(name, value)
    if len(normalized) > max_length:
        raise HealthProfileValidationError(
            f'{name} 不能超过 {max_length} 个字符',
        )
    return normalized


def _public_event(event: ProfileEvent) -> dict[str, Any]:
    source_type = None
    if isinstance(event.source_ref, Mapping):
        source_type = event.source_ref.get('sourceType')
    return {
        'eventId': event.event_id,
        'sequence': event.sequence,
        'eventType': event.event_type,
        'factId': event.fact_id,
        'reasonCode': event.reason_code,
        'actorType': event.actor.type,
        'sourceType': source_type,
        'createdAt': _isoformat(event.created_at),
    }


def _replay_delete_result(
    event: ProfileEvent,
    *,
    fingerprint: str,
) -> dict[str, Any]:
    operation = event.operation_result
    if not isinstance(operation, Mapping) or operation.get('operation') != 'fact_delete':
        raise HealthProfileConflictError(
            'requestId 已被其他健康画像操作使用',
        )
    if operation.get('requestFingerprint') != fingerprint:
        raise HealthProfileConflictError(
            'requestId 已被不同的请求内容使用',
        )
    return {
        'factId': operation['factId'],
        'status': operation['status'],
        'profileVersion': operation['profileVersion'],
        'deletedAt': operation['deletedAt'],
    }


def _normalize_kind(value: str | SubjectKind | None) -> SubjectKind | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, SubjectKind) else SubjectKind(value)
    except (TypeError, ValueError) as error:
        allowed = ', '.join(item.value for item in SubjectKind)
        raise HealthProfileValidationError(
            f'kind 不受支持，可选值: {allowed}',
        ) from error


def _normalize_optional_change(value: Any, *, name: str) -> Any:
    if value is _MISSING or value is None:
        return value
    return _required_text(name, value)


def _normalize_relationship_change(value: Any) -> Any:
    if value is _MISSING or value is None:
        return value
    if isinstance(value, SubjectRelationship):
        return value
    if not isinstance(value, Mapping):
        raise HealthProfileValidationError('relationship 必须为 object 或 null')
    try:
        return SubjectRelationship.model_validate(value)
    except ValidationError as error:
        raise HealthProfileValidationError(
            'relationship 字段不合法',
        ) from error


def _validate_relationship(
    kind: SubjectKind,
    relationship: SubjectRelationship,
) -> None:
    expected_blood_relative = kind is SubjectKind.BLOOD_RELATIVE
    if relationship.is_blood_relative is not expected_blood_relative:
        raise HealthProfileValidationError(
            'relationship.isBloodRelative 与 kind 不一致',
        )


def _validate_expected_version(value: int | None, *, required: bool) -> None:
    if value is None:
        if required:
            raise HealthProfileValidationError(
                '更新时缺少必填参数: expectedSubjectVersion',
            )
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HealthProfileValidationError(
            'expectedSubjectVersion 必须为正整数',
        )


def _operation_result(
    subject: HealthSubject,
    *,
    created: bool,
    fingerprint: str,
) -> dict[str, Any]:
    return {
        'created': created,
        'requestFingerprint': fingerprint,
        'subject': subject.model_dump(mode='json', by_alias=True),
    }


def _replay_subject_result(
    event: ProfileEvent,
    *,
    fingerprint: str,
) -> SubjectUpsertResult:
    operation = event.operation_result
    if not isinstance(operation, Mapping):
        raise HealthProfileConflictError(
            'requestId 已被其他健康画像操作使用',
        )
    if operation.get('requestFingerprint') != fingerprint:
        raise HealthProfileConflictError(
            'requestId 已被不同的请求内容使用',
        )
    raw_subject = operation.get('subject')
    try:
        subject = HealthSubject.model_validate(raw_subject)
    except ValidationError as error:
        raise RuntimeError('已保存的主体幂等结果无法解析') from error
    return SubjectUpsertResult(
        subject=subject,
        created=operation.get('created') is True,
    )


def _request_fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _fingerprint_value(value: Any) -> Any:
    return (
        {'provided': False}
        if value is _MISSING
        else {
            'provided': True,
            'value': value,
        }
    )


def _fingerprint_relationship(value: Any) -> Any:
    if value is _MISSING:
        return {'provided': False}
    return {
        'provided': True,
        'value': (None if value is None else value.model_dump(mode='json', by_alias=True)),
    }


def _new_id(prefix: str) -> str:
    return f'{prefix}_{uuid4().hex}'


def _required_text(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise HealthProfileValidationError(f'{name} 必须为 string')
    normalized = value.strip()
    if not normalized:
        raise HealthProfileValidationError(f'{name} 不能为空')
    return normalized


def _optional_text(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(name, value)


def _isoformat(value: datetime) -> str:
    return None if value is None else value.isoformat().replace('+00:00', 'Z')
