"""不同画像来源共用的事实、审计事件和投影写入器。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from .basic_info_fact_bridge import ProfileBasicInfoFactBridge
from .basic_info_store import BasicInfoConflict
from .basic_info_store import BasicInfoUnavailable
from .ingestion import HealthFactPolicyDecision
from .models import FactStatus
from .models import HealthFact
from .models import HealthProfileJob
from .models import HealthProfileView
from .models import HealthSubject
from .models import MemoryCandidate
from .models import ProfileActor
from .models import ProfileEvent
from .models import fact_user_confirmed
from .processing import ProfileIngestionProcessingError
from .repository import HealthProfileRepository

_PUBLIC_FACT_STATUSES = (
    FactStatus.ACTIVE,
    FactStatus.PENDING,
    FactStatus.DISPUTED,
    FactStatus.SUPERSEDED,
    FactStatus.RETRACTED,
    FactStatus.EXPIRED,
)


class HealthProfileFactWriter:
    """以来源候选为幂等单元推进事实、事件和画像投影。"""

    def __init__(
        self,
        repository: HealthProfileRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
        basic_info=None,
    ) -> None:
        if not isinstance(repository, HealthProfileRepository):
            raise TypeError(
                'repository must implement HealthProfileRepository',
            )
        self._repository = repository
        self._basic_info = basic_info if basic_info is not None else getattr(repository, 'basic_info', None)
        self.basic_info_bridge = ProfileBasicInfoFactBridge(self._basic_info) if self._basic_info is not None else None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda prefix: f'{prefix}_{uuid4().hex}')

    async def write(self, *, job, candidate, decision, subject):
        try:
            return await self._write(job=job, candidate=candidate, decision=decision, subject=subject)
        except BasicInfoConflict as error:
            raise ProfileIngestionProcessingError(
                str(error), 'Source demographic admission conflict', retryable=False
            ) from error
        except BasicInfoUnavailable as error:
            raise ProfileIngestionProcessingError(
                str(error), 'Source demographic store unavailable', retryable=True
            ) from error

    async def _write(
        self,
        *,
        job: HealthProfileJob,
        candidate: MemoryCandidate,
        decision: HealthFactPolicyDecision,
        subject: HealthSubject,
    ) -> bool:
        if decision.definition is None or decision.fact_status is None or decision.verification_status is None:
            raise ProfileIngestionProcessingError(
                'policy.invalid_decision',
                '保存策略返回了不完整的事实决策',
            )
        if candidate.source.source_type == 'health_report' and (candidate.source.pointer or '').startswith(
            '/sourceObservations/v6_'
        ):
            return await self._write_bound_report(job=job, candidate=candidate, decision=decision, subject=subject)
        request_id = f'profile_ingest:{job.job_id}:{candidate.candidate_id}'
        fingerprint = _sha256_json(
            candidate.model_dump(
                mode='json',
                by_alias=True,
            )
        )
        previous_event = await self._repository.get_event_by_request(
            job.user_id,
            request_id,
        )
        if previous_event is not None:
            if previous_event.fact_id is None:
                raise ProfileIngestionProcessingError(
                    'fact.idempotency_conflict',
                    '候选事实已有事件但缺少事实引用',
                )
            previous_fact = await self._repository.get_fact(
                job.user_id,
                subject.subject_id,
                previous_event.fact_id,
            )
            if previous_fact is None or previous_fact.creation_request_fingerprint != fingerprint:
                raise ProfileIngestionProcessingError(
                    'fact.idempotency_conflict',
                    '候选事实重放内容与原始写入不一致',
                )
            if self.basic_info_bridge is not None:
                await self.basic_info_bridge.repair(
                    previous_fact,
                    subject,
                    request_id,
                    source_type=candidate.source.source_type,
                    source_id=candidate.source.source_id,
                )
            return False
        now = self._now()
        fact = HealthFact(
            fact_id=self._id_factory('fact'),
            user_id=job.user_id,
            subject_id=subject.subject_id,
            fact_type=candidate.fact_type,
            category=decision.definition.category,
            display_name=candidate.display_name,
            value_type=candidate.value_type,
            value=candidate.value,
            interpretation=candidate.interpretation,
            reference_range=candidate.reference_range,
            assertion_level=candidate.assertion_level,
            verification_status=decision.verification_status,
            confidence=candidate.confidence,
            effective_period=candidate.effective_period,
            status=decision.fact_status,
            sources=(candidate.source,),
            created_at=now,
            updated_at=now,
            creation_request_id=request_id,
            creation_request_slot='fact',
            creation_request_fingerprint=fingerprint,
        )
        fence = None
        if self.basic_info_bridge is not None:
            lookup = getattr(self._repository, 'get_fact_by_creation_request', None)
            existing = await lookup(job.user_id, request_id, slot='fact') if lookup is not None else None
            if existing is not None:
                if existing.subject_id != subject.subject_id or existing.creation_request_fingerprint != fingerprint:
                    raise ProfileIngestionProcessingError(
                        'fact.idempotency_conflict', 'Existing source fact identity conflicts'
                    )
                fact = fact.model_copy(
                    update={
                        'fact_id': existing.fact_id,
                        'basic_info_lifecycle_epoch': existing.basic_info_lifecycle_epoch,
                    }
                )
            fence = await self.basic_info_bridge.prepare(fact, subject, request_id, admitted_at=job.created_at)
            fact = self.basic_info_bridge.tag_fact(fact, fence)
        stored, created = await self._repository.create_fact(fact)
        if stored.creation_request_fingerprint != fingerprint:
            raise ProfileIngestionProcessingError(
                'fact.idempotency_conflict',
                '候选事实幂等键已被其他内容使用',
            )
        if self.basic_info_bridge is not None:
            stored = await self.basic_info_bridge.ensure_tag(fence, stored)
            await self.basic_info_bridge.publish(fence, stored)
        view = await self._advance_view(
            user_id=job.user_id,
            subject=subject,
            request_id=request_id,
            updated_at=now,
        )
        if self._basic_info is not None:
            view = await self._basic_info.publish_governance(job.user_id, subject.subject_id) or view
        await self._repository.append_event(
            ProfileEvent(
                event_id=self._id_factory('event'),
                user_id=job.user_id,
                subject_id=subject.subject_id,
                sequence=view.profile_version,
                event_type='fact_ingested',
                fact_id=stored.fact_id,
                new_fact_version=stored.version,
                reason_code=decision.reason_code,
                actor=ProfileActor(type='system'),
                request_id=request_id,
                source_ref={
                    'sourceType': candidate.source.source_type,
                    'sourceId': candidate.source.source_id,
                    'sourceRevision': candidate.source.source_revision,
                    'pointer': candidate.source.pointer,
                },
                operation_result={
                    'operation': 'profile_ingest',
                    'factId': stored.fact_id,
                    'status': stored.status.value,
                    'profileVersion': view.profile_version,
                },
                created_at=now,
            )
        )
        return created

    async def _write_bound_report(self, *, job, candidate, decision, subject):
        """A physical report occurrence owns one fact across metadata revisions."""
        request_id = f'profile_ingest:{job.job_id}:{candidate.candidate_id}'
        value = deepcopy(candidate.value)
        if isinstance(value, dict):
            for field in ('unit', 'method', 'specimen', 'kind'):
                value.pop(field, None)
        core = {
            'sourceId': candidate.source.source_id,
            'pointer': candidate.source.pointer,
            'displayName': candidate.display_name,
            'value': value,
        }
        fingerprint = _sha256_json(core)
        creation_id = 'report_occurrence:' + _sha256_json(
            {
                'subjectId': subject.subject_id,
                'sourceId': candidate.source.source_id,
                'pointer': candidate.source.pointer,
            }
        )
        event = await self._repository.get_event_by_request(job.user_id, request_id)
        if event is not None:
            fact = await self._repository.get_fact(job.user_id, subject.subject_id, event.fact_id)
            if (
                fact is None
                and await self._repository.get_deleted_fact_tombstone(job.user_id, subject.subject_id, event.fact_id)
                is not None
            ):
                return False
            if fact is None or fact.creation_request_fingerprint != fingerprint:
                raise ProfileIngestionProcessingError('fact.idempotency_conflict', '报告来源重放内容不一致')
            if self.basic_info_bridge is not None:
                await self.basic_info_bridge.repair(
                    fact,
                    subject,
                    request_id,
                    source_type=candidate.source.source_type,
                    source_id=candidate.source.source_id,
                )
            return False
        now = self._now()
        requested = HealthFact(
            fact_id='fact_report_' + _sha256_json(creation_id),
            user_id=job.user_id,
            subject_id=subject.subject_id,
            fact_type=candidate.fact_type,
            category=decision.definition.category,
            display_name=candidate.display_name,
            value_type=candidate.value_type,
            value=candidate.value,
            interpretation=candidate.interpretation,
            reference_range=candidate.reference_range,
            assertion_level=candidate.assertion_level,
            verification_status=decision.verification_status,
            confidence=candidate.confidence,
            effective_period=candidate.effective_period,
            status=decision.fact_status,
            sources=(candidate.source,),
            created_at=now,
            updated_at=now,
            creation_request_id=creation_id,
            creation_request_slot='fact',
            creation_request_fingerprint=fingerprint,
        )
        fence = None
        if self.basic_info_bridge is not None:
            fence = await self.basic_info_bridge.prepare(requested, subject, request_id, admitted_at=job.created_at)
            requested = self.basic_info_bridge.tag_fact(requested, fence)
        try:
            stored, created = await self._repository.create_fact(requested)
        except DuplicateKeyError:
            # Deletion intentionally removes the original creation key. A
            # deterministic physical-source fact ID keeps its tombstone effective
            # for later jobs without retaining the deleted medical content.
            if (
                await self._repository.get_deleted_fact_tombstone(job.user_id, subject.subject_id, requested.fact_id)
                is not None
            ):
                return False
            raise
        if stored.creation_request_fingerprint != fingerprint:
            raise ProfileIngestionProcessingError('fact.idempotency_conflict', '同一报告来源不能替换已确认名称或数值')
        if not created:
            stored = await self._enrich_bound_fact(stored, candidate, request_id, now)
        if self.basic_info_bridge is not None:
            stored = await self.basic_info_bridge.ensure_tag(fence, stored)
            await self.basic_info_bridge.publish(fence, stored)
        view = await self._advance_view(
            user_id=job.user_id,
            subject=subject,
            request_id=request_id,
            updated_at=now,
        )
        if self._basic_info is not None:
            view = await self._basic_info.publish_governance(job.user_id, subject.subject_id) or view
        await self._repository.append_event(
            ProfileEvent(
                event_id=self._id_factory('event'),
                user_id=job.user_id,
                subject_id=subject.subject_id,
                sequence=view.profile_version,
                event_type='fact_ingested' if created else 'fact_enriched',
                fact_id=stored.fact_id,
                new_fact_version=stored.version,
                reason_code=decision.reason_code,
                actor=ProfileActor(type='system'),
                request_id=request_id,
                source_ref={
                    'sourceType': candidate.source.source_type,
                    'sourceId': candidate.source.source_id,
                    'sourceRevision': candidate.source.source_revision,
                    'pointer': candidate.source.pointer,
                },
                operation_result={
                    'operation': 'profile_ingest' if created else 'profile_enrich',
                    'factId': stored.fact_id,
                    'status': stored.status.value,
                    'profileVersion': view.profile_version,
                },
                created_at=now,
            )
        )
        return created

    async def _enrich_bound_fact(self, stored, candidate, request_id, now):
        update_id = f'profile_report_enrich:{request_id}'
        for _ in range(8):
            # User decisions own edited, confirmed, disputed and withdrawn facts.
            # Report recovery must not resurrect them or alter their contents.
            if (
                stored.status not in {FactStatus.ACTIVE, FactStatus.PENDING}
                or fact_user_confirmed(stored)
                or (stored.last_request_id and not stored.last_request_id.startswith('profile_report_enrich:'))
                or any(source.source_type != 'health_report' for source in stored.sources)
            ):
                return stored
            updates = {}
            if isinstance(stored.value, dict) and isinstance(candidate.value, dict):
                value = deepcopy(stored.value)
                for field in ('unit', 'method', 'specimen', 'kind'):
                    if value.get(field) in (None, '') and candidate.value.get(field) not in (None, ''):
                        value[field] = deepcopy(candidate.value[field])
                if value != stored.value:
                    updates['value'] = value
            if stored.interpretation is None and candidate.interpretation is not None:
                updates['interpretation'] = candidate.interpretation
            reference = deepcopy(stored.reference_range)
            if reference is None and candidate.reference_range is not None:
                updates['reference_range'] = candidate.reference_range
            elif isinstance(reference, dict) and isinstance(candidate.reference_range, dict):
                for field, value in candidate.reference_range.items():
                    if reference.get(field) in (None, '') and value not in (None, ''):
                        reference[field] = deepcopy(value)
                if reference != stored.reference_range:
                    updates['reference_range'] = reference
            if not updates:
                return stored
            sources = stored.sources
            if candidate.source not in sources:
                sources = (*sources, candidate.source)
            updates.update(
                version=stored.version + 1,
                updated_at=now,
                sources=sources,
                last_request_id=update_id,
                last_request_fingerprint=_sha256_json(candidate.model_dump(mode='json')),
            )
            updated = await self._repository.replace_fact(
                stored.model_copy(update=updates),
                expected_version=stored.version,
                allowed_statuses=(FactStatus.ACTIVE, FactStatus.PENDING),
            )
            if updated is not None:
                return updated
            stored = await self._repository.get_fact(stored.user_id, stored.subject_id, stored.fact_id)
            if stored is None:
                raise ProfileIngestionProcessingError('fact.not_found', '报告来源事实已不可用')
        raise ProfileIngestionProcessingError('fact.concurrent_update', '报告补充信息发生并发修改，请重试')

    async def _advance_view(
        self,
        *,
        user_id: str,
        subject: HealthSubject,
        request_id: str,
        updated_at: datetime,
    ) -> HealthProfileView:
        view = await self._repository.get_view(user_id, subject.subject_id)
        if view is None:
            view = await self._repository.ensure_initial_view(
                HealthProfileView(
                    user_id=user_id,
                    subject_id=subject.subject_id,
                    subject_summary={'displayName': subject.display_name},
                    updated_at=updated_at,
                ),
            )
        facts = await self._repository.list_facts(
            user_id,
            subject.subject_id,
            statuses=_PUBLIC_FACT_STATUSES,
        )
        active = tuple(fact for fact in facts if fact.status is FactStatus.ACTIVE)
        category_counts: dict[str, int] = {}
        for fact in active:
            name = fact.category.value
            category_counts[name] = category_counts.get(name, 0) + 1
        key_fact_ids = tuple(fact.fact_id for fact in active if fact.interpretation in {'low', 'high', 'critical'})[:20]
        pending_count = sum(fact.status in {FactStatus.PENDING, FactStatus.DISPUTED} for fact in facts)
        source_times = [source.observed_at or fact.effective_period.start for fact in active for source in fact.sources]
        updated = await self._repository.advance_fact_view(
            user_id=user_id,
            subject_id=subject.subject_id,
            expected_profile_version=view.profile_version,
            request_id=request_id,
            category_counts=category_counts,
            key_fact_ids=key_fact_ids,
            pending_confirmation_count=pending_count,
            last_source_at=max((item for item in source_times if item is not None), default=None),
            updated_at=updated_at,
        )
        if updated is None:
            raise ProfileIngestionProcessingError(
                'profile.concurrent_update',
                '画像投影已被其他请求修改',
            )
        return updated

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('clock must return a timezone-aware datetime')
        return now


def _sha256_json(value) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()
