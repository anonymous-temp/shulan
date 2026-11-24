"""用户健康画像摄取内核。

本模块只定义“原始业务数据如何成为待治理候选”的稳定边界，不负责连接
报告、建档问答或普通聊天。来源接入会在后续迭代逐一完成。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import StrEnum
from uuid import uuid4

from .indicators import INDICATORS
from .indicators import is_report_observation_type
from .models import AssertionLevel
from .models import FactCategory
from .models import FactStatus
from .models import FactValueType
from .models import HealthProfileJob
from .models import MemoryCandidate
from .models import ProfileIngestionSourceType
from .models import ProfileJobStatus
from .models import ProfileProcessingSummary
from .models import SubjectKind
from .models import VerificationStatus
from .repository import HealthProfileJobRepository

LIFESTYLE_FACT_LABELS = {
    'lifestyle.diet_pattern': '饮食习惯',
    'lifestyle.meal_regularity': '进餐规律',
    'lifestyle.hydration': '饮水习惯',
    'lifestyle.physical_activity': '运动习惯',
    'lifestyle.sedentary_behavior': '久坐习惯',
    'lifestyle.sleep': '作息与睡眠',
    'lifestyle.smoking_status': '吸烟情况',
    'lifestyle.alcohol_use': '饮酒习惯',
    'lifestyle.stress_coping': '压力应对与放松',
}


class PolicyDisposition(StrEnum):
    ACTIVE = 'active'
    PENDING = 'pending'
    NEEDS_SUBJECT_CONFIRMATION = 'needs_subject_confirmation'
    REJECTED = 'rejected'


@dataclass(frozen=True, slots=True)
class FactTypeDefinition:
    fact_type: str
    category: FactCategory
    permitted_value_types: tuple[FactValueType, ...]
    permitted_source_types: tuple[ProfileIngestionSourceType, ...]
    permitted_subject_kinds: tuple[SubjectKind, ...]


@dataclass(frozen=True, slots=True)
class HealthFactPolicyDecision:
    disposition: PolicyDisposition
    reason_code: str
    definition: FactTypeDefinition | None = None
    fact_status: FactStatus | None = None
    verification_status: VerificationStatus | None = None


@dataclass(frozen=True, slots=True)
class ProfileJobEnqueueResult:
    job: HealthProfileJob
    created: bool


class HealthProfileJobConflictError(RuntimeError):
    """同一幂等键被不同来源载荷重复使用。"""


class HealthProfileJobLeaseError(RuntimeError):
    """工作节点尝试使用已失效或不属于自己的租约完成任务。"""


class FactTypeRegistry:
    """画像事实类型的显式白名单。

    标准类型明确声明来源、值类型和主体边界。唯一动态命名空间是报告映射器
    生成的受控原始检测项，不把通配类型开放给模型。
    """

    VERSION = 'health-fact-registry-v7'

    def __init__(self, definitions: tuple[FactTypeDefinition, ...], *, allow_report_observations: bool = False) -> None:
        mapping: dict[str, FactTypeDefinition] = {}
        for definition in definitions:
            fact_type = _required_text('fact_type', definition.fact_type)
            if fact_type in mapping:
                raise ValueError(f'duplicate fact type: {fact_type}')
            if not definition.permitted_value_types:
                raise ValueError(f'{fact_type} requires permitted value types')
            if not definition.permitted_source_types:
                raise ValueError(f'{fact_type} requires permitted sources')
            if not definition.permitted_subject_kinds:
                raise ValueError(f'{fact_type} requires permitted subjects')
            mapping[fact_type] = definition
        self._definitions = mapping
        self._allow_report_observations = allow_report_observations

    def get(self, fact_type: str) -> FactTypeDefinition | None:
        fact_type = _required_text('fact_type', fact_type)
        definition = self._definitions.get(fact_type)
        if definition is None and self._allow_report_observations and is_report_observation_type(fact_type):
            return FactTypeDefinition(
                fact_type,
                FactCategory.REPORT_OBSERVATION,
                (FactValueType.QUANTITY, FactValueType.TEXT),
                (ProfileIngestionSourceType.HEALTH_REPORT,),
                tuple(kind for kind in SubjectKind if kind is not SubjectKind.UNKNOWN),
            )
        return definition

    def require(self, fact_type: str) -> FactTypeDefinition:
        definition = self.get(fact_type)
        if definition is None:
            raise KeyError(f'unknown fact type: {fact_type}')
        return definition

    def list_definitions(self) -> tuple[FactTypeDefinition, ...]:
        return tuple(self._definitions.values())

    @classmethod
    def default(cls) -> 'FactTypeRegistry':
        sources = tuple(ProfileIngestionSourceType)
        known_subjects = tuple(kind for kind in SubjectKind if kind is not SubjectKind.UNKNOWN)
        definitions = (
            FactTypeDefinition(
                fact_type='demographic.date_of_birth',
                category=FactCategory.DEMOGRAPHIC,
                permitted_value_types=(FactValueType.DATE,),
                permitted_source_types=sources,
                permitted_subject_kinds=known_subjects,
            ),
            FactTypeDefinition(
                fact_type='demographic.sex',
                category=FactCategory.DEMOGRAPHIC,
                permitted_value_types=(FactValueType.CODED, FactValueType.TEXT),
                permitted_source_types=sources,
                permitted_subject_kinds=known_subjects,
            ),
            FactTypeDefinition(
                fact_type='demographic.name',
                category=FactCategory.DEMOGRAPHIC,
                permitted_value_types=(FactValueType.TEXT,),
                permitted_source_types=sources,
                permitted_subject_kinds=known_subjects,
            ),
            FactTypeDefinition(
                fact_type='demographic.age',
                category=FactCategory.DEMOGRAPHIC,
                permitted_value_types=(FactValueType.QUANTITY,),
                permitted_source_types=sources,
                permitted_subject_kinds=known_subjects,
            ),
            FactTypeDefinition(
                fact_type='anthropometric.height',
                category=FactCategory.ANTHROPOMETRIC,
                permitted_value_types=(FactValueType.QUANTITY,),
                permitted_source_types=sources,
                permitted_subject_kinds=known_subjects,
            ),
            FactTypeDefinition(
                fact_type='anthropometric.weight',
                category=FactCategory.ANTHROPOMETRIC,
                permitted_value_types=(FactValueType.QUANTITY,),
                permitted_source_types=sources,
                permitted_subject_kinds=known_subjects,
            ),
            FactTypeDefinition(
                fact_type='anthropometric.bmi',
                category=FactCategory.ANTHROPOMETRIC,
                permitted_value_types=(FactValueType.QUANTITY,),
                permitted_source_types=sources,
                permitted_subject_kinds=known_subjects,
            ),
            FactTypeDefinition(
                fact_type='condition.diagnosis',
                category=FactCategory.CONDITION,
                permitted_value_types=(
                    FactValueType.CODED,
                    FactValueType.TEXT,
                    FactValueType.BOOLEAN,
                ),
                permitted_source_types=sources,
                permitted_subject_kinds=known_subjects,
            ),
            FactTypeDefinition(
                fact_type='condition.finding',
                category=FactCategory.CONDITION,
                permitted_value_types=(FactValueType.TEXT,),
                permitted_source_types=(ProfileIngestionSourceType.HEALTH_REPORT,),
                permitted_subject_kinds=known_subjects,
            ),
            FactTypeDefinition(
                fact_type='condition.history_status',
                category=FactCategory.CONDITION,
                permitted_value_types=(FactValueType.OBJECT,),
                permitted_source_types=(ProfileIngestionSourceType.RECORD, ProfileIngestionSourceType.QUESTIONNAIRE),
                permitted_subject_kinds=(SubjectKind.SELF,),
            ),
            FactTypeDefinition(
                fact_type='symptom.reported',
                category=FactCategory.SYMPTOM,
                permitted_value_types=(
                    FactValueType.CODED,
                    FactValueType.TEXT,
                    FactValueType.OBJECT,
                ),
                permitted_source_types=sources,
                permitted_subject_kinds=known_subjects,
            ),
            FactTypeDefinition(
                fact_type='medication.current',
                category=FactCategory.MEDICATION,
                permitted_value_types=(
                    FactValueType.CODED,
                    FactValueType.TEXT,
                    FactValueType.OBJECT,
                ),
                permitted_source_types=sources,
                permitted_subject_kinds=known_subjects,
            ),
            FactTypeDefinition(
                fact_type='allergy.substance',
                category=FactCategory.ALLERGY,
                permitted_value_types=(
                    FactValueType.CODED,
                    FactValueType.TEXT,
                    FactValueType.OBJECT,
                ),
                permitted_source_types=sources,
                permitted_subject_kinds=known_subjects,
            ),
            FactTypeDefinition(
                fact_type='family_history.condition',
                category=FactCategory.FAMILY_HISTORY,
                permitted_value_types=(
                    FactValueType.CODED,
                    FactValueType.TEXT,
                    FactValueType.OBJECT,
                ),
                permitted_source_types=sources,
                permitted_subject_kinds=(SubjectKind.BLOOD_RELATIVE,),
            ),
            FactTypeDefinition(
                fact_type='family_history.reported',
                category=FactCategory.FAMILY_HISTORY,
                permitted_value_types=(FactValueType.OBJECT,),
                permitted_source_types=(ProfileIngestionSourceType.RECORD, ProfileIngestionSourceType.QUESTIONNAIRE),
                permitted_subject_kinds=(SubjectKind.SELF,),
            ),
            FactTypeDefinition(
                fact_type='goal.health',
                category=FactCategory.GOAL,
                permitted_value_types=(FactValueType.TEXT, FactValueType.OBJECT),
                permitted_source_types=(
                    ProfileIngestionSourceType.RECORD,
                    ProfileIngestionSourceType.QUESTIONNAIRE,
                    ProfileIngestionSourceType.CHAT,
                ),
                permitted_subject_kinds=(SubjectKind.SELF,),
            ),
        )
        lifestyle_definitions = tuple(
            FactTypeDefinition(
                fact_type=fact_type,
                category=FactCategory.LIFESTYLE,
                permitted_value_types=(
                    (FactValueType.TEXT, FactValueType.OBJECT, FactValueType.CODED)
                    if fact_type == 'lifestyle.smoking_status'
                    else (FactValueType.TEXT, FactValueType.OBJECT)
                ),
                permitted_source_types=sources,
                permitted_subject_kinds=known_subjects,
            )
            for fact_type in LIFESTYLE_FACT_LABELS
        )
        laboratory_definitions = tuple(
            FactTypeDefinition(
                fact_type=indicator.fact_type,
                category=indicator.category,
                permitted_value_types=(FactValueType.QUANTITY, FactValueType.TEXT),
                permitted_source_types=sources,
                permitted_subject_kinds=known_subjects,
            )
            for indicator in INDICATORS
            if indicator.fact_type not in {definition.fact_type for definition in definitions}
        )
        return cls(definitions + lifestyle_definitions + laboratory_definitions, allow_report_observations=True)


class HealthFactPolicy:
    """把候选事实转换为确定性的治理决策。"""

    VERSION = 'health-fact-policy-v1'

    def __init__(self, registry: FactTypeRegistry | None = None) -> None:
        self._registry = registry or FactTypeRegistry.default()

    def evaluate(
        self,
        candidate: MemoryCandidate,
        *,
        subject_kind: SubjectKind,
    ) -> HealthFactPolicyDecision:
        if not isinstance(candidate, MemoryCandidate):
            raise TypeError('candidate must be a MemoryCandidate')
        if not isinstance(subject_kind, SubjectKind):
            raise TypeError('subject_kind must be a SubjectKind')

        definition = self._registry.get(candidate.fact_type)
        if definition is None:
            return HealthFactPolicyDecision(
                disposition=PolicyDisposition.REJECTED,
                reason_code='unknown_fact_type',
            )
        report_candidate = candidate.source.source_type == ProfileIngestionSourceType.HEALTH_REPORT
        automatic_analysis = (
            report_candidate
            and candidate.report_verification is VerificationStatus.CONFIRMED
            and candidate.source.confirmation_basis == 'analysis_public'
            and candidate.assertion_level is AssertionLevel.OBSERVED
            and candidate.value is not None
        )
        needs_report_review = report_candidate and candidate.report_verification in {
            VerificationStatus.UNCONFIRMED,
            VerificationStatus.CONFLICTED,
        }
        if (
            candidate.effective_period.start is None
            and definition.category is not FactCategory.DEMOGRAPHIC
            and not needs_report_review
            and not (automatic_analysis and candidate.source.recorded_at is not None)
        ):
            return HealthFactPolicyDecision(
                disposition=PolicyDisposition.REJECTED,
                reason_code='observation_time_required',
                definition=definition,
            )
        if candidate.value_type not in definition.permitted_value_types:
            return HealthFactPolicyDecision(
                disposition=PolicyDisposition.REJECTED,
                reason_code='value_type_not_allowed',
                definition=definition,
            )
        try:
            source_type = ProfileIngestionSourceType(
                candidate.source.source_type,
            )
        except ValueError:
            return HealthFactPolicyDecision(
                disposition=PolicyDisposition.REJECTED,
                reason_code='source_type_not_allowed',
                definition=definition,
            )
        if source_type not in definition.permitted_source_types:
            return HealthFactPolicyDecision(
                disposition=PolicyDisposition.REJECTED,
                reason_code='source_type_not_allowed',
                definition=definition,
            )
        if candidate.subject_id is None or subject_kind is SubjectKind.UNKNOWN:
            return HealthFactPolicyDecision(
                disposition=PolicyDisposition.NEEDS_SUBJECT_CONFIRMATION,
                reason_code='subject_unresolved',
                definition=definition,
                fact_status=FactStatus.PENDING,
                verification_status=VerificationStatus.UNCONFIRMED,
            )
        if subject_kind not in definition.permitted_subject_kinds:
            return HealthFactPolicyDecision(
                disposition=PolicyDisposition.REJECTED,
                reason_code='subject_kind_not_allowed',
                definition=definition,
            )
        if needs_report_review:
            if candidate.report_verification is VerificationStatus.CONFLICTED:
                return HealthFactPolicyDecision(
                    disposition=PolicyDisposition.PENDING,
                    reason_code='report_evidence_conflict',
                    definition=definition,
                    fact_status=FactStatus.DISPUTED,
                    verification_status=VerificationStatus.CONFLICTED,
                )
            return self._pending(definition, 'report_observation_requires_review')
        if automatic_analysis:
            return HealthFactPolicyDecision(
                disposition=PolicyDisposition.ACTIVE,
                reason_code='confirmed_analysis_report',
                definition=definition,
                fact_status=FactStatus.ACTIVE,
                verification_status=VerificationStatus.CONFIRMED,
            )
        if candidate.assertion_level is AssertionLevel.MODEL_INFERRED:
            return self._pending(definition, 'model_inference_requires_review')
        if source_type is ProfileIngestionSourceType.HEALTH_REPORT and candidate.assertion_level in {
            AssertionLevel.OBSERVED,
            AssertionLevel.CLINICIAN_RECORDED,
        }:
            return HealthFactPolicyDecision(
                disposition=PolicyDisposition.ACTIVE,
                reason_code='verified_report_source',
                definition=definition,
                fact_status=FactStatus.ACTIVE,
                verification_status=VerificationStatus.VERIFIED_SOURCE,
            )
        if (
            source_type is ProfileIngestionSourceType.RECORD
            and candidate.assertion_level is AssertionLevel.USER_CONFIRMED
        ):
            return HealthFactPolicyDecision(
                disposition=PolicyDisposition.ACTIVE,
                reason_code='user_confirmed_record',
                definition=definition,
                fact_status=FactStatus.ACTIVE,
                verification_status=VerificationStatus.CONFIRMED,
            )
        return self._pending(definition, 'source_requires_review')

    @staticmethod
    def _pending(
        definition: FactTypeDefinition,
        reason_code: str,
    ) -> HealthFactPolicyDecision:
        return HealthFactPolicyDecision(
            disposition=PolicyDisposition.PENDING,
            reason_code=reason_code,
            definition=definition,
            fact_status=FactStatus.PENDING,
            verification_status=VerificationStatus.UNCONFIRMED,
        )


class HealthProfileJobQueue:
    """带幂等、租约、重试和死信状态的画像摄取任务队列。"""

    def __init__(
        self,
        repository: HealthProfileJobRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
        lease_duration: timedelta = timedelta(minutes=5),
        retry_base_delay: timedelta = timedelta(seconds=30),
        retry_max_delay: timedelta = timedelta(hours=1),
        max_attempts: int = 5,
    ) -> None:
        if not isinstance(repository, HealthProfileJobRepository):
            raise TypeError(
                'repository must implement HealthProfileJobRepository',
            )
        if lease_duration <= timedelta(0):
            raise ValueError('lease_duration must be positive')
        if retry_base_delay <= timedelta(0):
            raise ValueError('retry_base_delay must be positive')
        if retry_max_delay < retry_base_delay:
            raise ValueError('retry_max_delay cannot be shorter than base delay')
        if max_attempts < 1:
            raise ValueError('max_attempts must be at least 1')
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda prefix: f'{prefix}_{uuid4().hex}')
        self._lease_duration = lease_duration
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay
        self._max_attempts = max_attempts

    async def is_source_publication_managed(self, user_id, subject_id, source_type, source_id):
        guard = getattr(self._repository, 'is_source_publication_managed', None)
        return guard is not None and await guard(user_id, subject_id, source_type, source_id)

    async def enqueue(
        self,
        *,
        source_type: ProfileIngestionSourceType,
        source_id: str,
        source_revision: int,
        user_id: str,
        subject_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ProfileJobEnqueueResult:
        if not isinstance(source_type, ProfileIngestionSourceType):
            raise TypeError(
                'source_type must be a ProfileIngestionSourceType',
            )
        source_id = _required_text('source_id', source_id)
        user_id = _required_text('user_id', user_id)
        subject_id = _optional_text(subject_id)
        if source_revision < 1:
            raise ValueError('source_revision must be at least 1')
        requested_key = _optional_text(idempotency_key) or (f'{source_type.value}:{source_id}:{source_revision}')
        # 现有 MongoDB 索引按 idempotencyKey 全局唯一；在内核中显式加入
        # userId 命名空间，避免不同用户恰好使用相同来源 ID 时互相冲突。
        idempotency_key = f'{user_id}:{requested_key}'
        fingerprint = _job_fingerprint(
            source_type=source_type,
            source_id=source_id,
            source_revision=source_revision,
            user_id=user_id,
            subject_id=subject_id,
        )
        if await self.is_source_publication_managed(user_id, subject_id, source_type.value, source_id):
            existing = await self._repository.get_job_by_idempotency(user_id, idempotency_key)
            if existing is not None and existing.idempotency_fingerprint == fingerprint:
                return ProfileJobEnqueueResult(job=existing, created=False)
            raise HealthProfileJobConflictError('legacy source admission is fenced by Profile publication cutover')
        now = self._now()
        requested = HealthProfileJob(
            job_id=self._id_factory('profile_job'),
            idempotency_key=idempotency_key,
            idempotency_fingerprint=fingerprint,
            source_type=source_type,
            source_id=source_id,
            source_revision=source_revision,
            user_id=user_id,
            subject_id=subject_id,
            created_at=now,
            updated_at=now,
        )
        stored, created = await self._repository.create_job(requested)
        if stored.idempotency_fingerprint != fingerprint:
            raise HealthProfileJobConflictError(
                'idempotency key was already used by another source payload',
            )
        return ProfileJobEnqueueResult(job=stored, created=created)

    async def claim(
        self,
        worker_id: str,
        *,
        source_types: Sequence[ProfileIngestionSourceType] | None = None,
    ) -> HealthProfileJob | None:
        worker_id = _required_text('worker_id', worker_id)
        normalized_source_types = None
        if source_types is not None:
            normalized_source_types = tuple(dict.fromkeys(source_types))
            if not normalized_source_types:
                raise ValueError('source_types cannot be empty')
            if any(not isinstance(item, ProfileIngestionSourceType) for item in normalized_source_types):
                raise TypeError(
                    'source_types must contain ProfileIngestionSourceType',
                )
        now = self._now()
        return await self._repository.claim_next_job(
            worker_id=worker_id,
            lease_token=self._id_factory('lease'),
            now=now,
            lease_expires_at=now + self._lease_duration,
            source_types=normalized_source_types,
        )

    async def complete(
        self,
        job: HealthProfileJob,
        *,
        worker_id: str,
        lease_token: str,
        graph_version: str,
        policy_version: str,
        extractor_version: str,
        processing_summary: ProfileProcessingSummary | None = None,
    ) -> HealthProfileJob:
        _require_running_job(job)
        completed_at = self._now()
        completed = await self._repository.complete_job(
            job_id=job.job_id,
            worker_id=_required_text('worker_id', worker_id),
            lease_token=_required_text('lease_token', lease_token),
            completed_at=completed_at,
            graph_version=_required_text('graph_version', graph_version),
            policy_version=_required_text('policy_version', policy_version),
            extractor_version=_required_text(
                'extractor_version',
                extractor_version,
            ),
            processing_summary=(
                processing_summary.model_dump(mode='python', by_alias=True) if processing_summary is not None else None
            ),
        )
        if completed is None:
            raise HealthProfileJobLeaseError('profile job lease is no longer valid')
        return completed

    async def fail(
        self,
        job: HealthProfileJob,
        *,
        worker_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool = True,
    ) -> HealthProfileJob:
        _require_running_job(job)
        error_code = _error_code(error_code)
        failed_at = self._now()
        if retryable is False or job.attempts >= self._max_attempts:
            status = ProfileJobStatus.DEAD_LETTER
            next_retry_at = None
        else:
            status = ProfileJobStatus.RETRY_WAIT
            multiplier = 2 ** max(0, job.attempts - 1)
            delay = min(
                self._retry_base_delay * multiplier,
                self._retry_max_delay,
            )
            next_retry_at = failed_at + delay
        failed = await self._repository.fail_job(
            job_id=job.job_id,
            worker_id=_required_text('worker_id', worker_id),
            lease_token=_required_text('lease_token', lease_token),
            failed_at=failed_at,
            error_code=error_code,
            status=status,
            next_retry_at=next_retry_at,
        )
        if failed is None:
            raise HealthProfileJobLeaseError('profile job lease is no longer valid')
        return failed

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('clock must return a timezone-aware datetime')
        return now


def _job_fingerprint(
    *,
    source_type: ProfileIngestionSourceType,
    source_id: str,
    source_revision: int,
    user_id: str,
    subject_id: str | None,
) -> str:
    payload = {
        'sourceType': source_type.value,
        'sourceId': source_id,
        'sourceRevision': source_revision,
        'userId': user_id,
        'subjectId': subject_id,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _require_running_job(job: HealthProfileJob) -> None:
    if not isinstance(job, HealthProfileJob):
        raise TypeError('job must be a HealthProfileJob')
    if job.status is not ProfileJobStatus.RUNNING:
        raise ValueError('profile job must be running')


def _error_code(value: str) -> str:
    normalized = _required_text('error_code', value)
    if len(normalized) > 64 or re.fullmatch(r'[A-Za-z0-9_.-]+', normalized) is None:
        raise ValueError(
            'error_code may contain only letters, digits, dot, dash and underscore',
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
