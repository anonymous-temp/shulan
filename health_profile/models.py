"""用户健康画像的领域模型。"""

from __future__ import annotations

import re
from datetime import UTC
from datetime import datetime
from enum import StrEnum
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_serializer
from pydantic import model_validator


def _to_camel(name: str) -> str:
    head, *tail = name.split('_')
    return head + ''.join(part.capitalize() for part in tail)


class HealthProfileModel(BaseModel):
    """统一使用 API/MongoDB 的 camelCase 字段。"""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra='forbid',
        frozen=True,
    )

    def to_document(self, *, document_id: str) -> dict[str, Any]:
        # MongoDB 保留 datetime，API 序列化在公开投影层完成。使用 JSON
        # 模式会把时间写成字符串，导致日期范围索引与比较失效。
        document = self.model_dump(mode='python', by_alias=True)
        document['_id'] = document_id
        return document


class SubjectKind(StrEnum):
    SELF = 'self'
    BLOOD_RELATIVE = 'blood_relative'
    NON_BLOOD_FAMILY = 'non_blood_family'
    FRIEND = 'friend'
    OTHER = 'other'
    UNKNOWN = 'unknown'


class SubjectStatus(StrEnum):
    ACTIVE = 'active'
    MERGED = 'merged'
    ARCHIVED = 'archived'


class FactCategory(StrEnum):
    DEMOGRAPHIC = 'demographic'
    ANTHROPOMETRIC = 'anthropometric'
    LABORATORY = 'laboratory'
    ASSESSMENT = 'assessment'
    REPORT_OBSERVATION = 'report_observation'
    CONDITION = 'condition'
    SYMPTOM = 'symptom'
    MEDICATION = 'medication'
    ALLERGY = 'allergy'
    LIFESTYLE = 'lifestyle'
    FAMILY_HISTORY = 'family_history'
    GOAL = 'goal'


class FactValueType(StrEnum):
    TEXT = 'text'
    CODED = 'coded'
    BOOLEAN = 'boolean'
    QUANTITY = 'quantity'
    DATE = 'date'
    PERIOD = 'period'
    OBJECT = 'object'


class FactStatus(StrEnum):
    ACTIVE = 'active'
    PENDING = 'pending'
    DISPUTED = 'disputed'
    SUPERSEDED = 'superseded'
    RETRACTED = 'retracted'
    EXPIRED = 'expired'
    DELETED = 'deleted'


class AssertionLevel(StrEnum):
    OBSERVED = 'observed'
    CLINICIAN_RECORDED = 'clinician_recorded'
    USER_CONFIRMED = 'user_confirmed'
    USER_REPORTED = 'user_reported'
    MODEL_INFERRED = 'model_inferred'


class VerificationStatus(StrEnum):
    VERIFIED_SOURCE = 'verified_source'
    CONFIRMED = 'confirmed'
    UNCONFIRMED = 'unconfirmed'
    CONFLICTED = 'conflicted'


class ProfileJobStatus(StrEnum):
    PENDING = 'pending'
    RUNNING = 'running'
    RETRY_WAIT = 'retry_wait'
    COMPLETED = 'completed'
    DEAD_LETTER = 'dead_letter'


class ProfileIngestionSourceType(StrEnum):
    """能够触发用户画像摄取任务的生产数据来源。"""

    HEALTH_REPORT = 'health_report'
    RECORD = 'record'
    QUESTIONNAIRE = 'questionnaire'
    CHAT = 'chat'


class SubjectRelationship(HealthProfileModel):
    relation_type: str
    is_blood_relative: bool

    @field_validator('relation_type')
    @classmethod
    def validate_relation_type(cls, value: str) -> str:
        return _required_text('relation_type', value)


class HealthSubject(HealthProfileModel):
    subject_id: str
    user_id: str
    kind: SubjectKind
    display_name: str | None = None
    relationship: SubjectRelationship | None = None
    aliases: tuple[str, ...] = ()
    status: SubjectStatus = SubjectStatus.ACTIVE
    created_at: datetime
    updated_at: datetime
    version: int = Field(default=1, ge=1)
    lifecycle_epoch: int = Field(default=1, ge=1)
    lifecycle_started_at: datetime | None = None
    creation_request_id: str | None = None
    creation_request_fingerprint: str | None = None
    last_request_id: str | None = None
    last_request_fingerprint: str | None = None

    @field_validator('subject_id', 'user_id')
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _required_text(info.field_name, value)

    @field_validator('display_name')
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator('aliases')
    @classmethod
    def validate_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_required_text('alias', item) for item in value)
        return tuple(dict.fromkeys(normalized))

    @model_validator(mode='after')
    def validate_self_relationship(self) -> 'HealthSubject':
        if self.kind is SubjectKind.SELF and self.relationship is not None:
            raise ValueError('self subject cannot have a relationship')
        return self


class FactSource(HealthProfileModel):
    source_type: str
    source_id: str
    source_revision: int = Field(ge=1)
    pointer: str | None = None
    observed_at: datetime | None = None
    excerpt_hash: str | None = None
    confirmation_basis: Literal['analysis_public'] | None = None
    recorded_at: datetime | None = None

    @field_validator('recorded_at')
    @classmethod
    def normalize_recording_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @model_validator(mode='after')
    def validate_confirmation_basis(self) -> 'FactSource':
        if self.confirmation_basis is not None and self.source_type != ProfileIngestionSourceType.HEALTH_REPORT:
            raise ValueError('automatic Analysis confirmation requires a health report source')
        if self.recorded_at is not None and (self.confirmation_basis is None or self.observed_at is not None):
            raise ValueError('recording time is reserved for undated automatically confirmed observations')
        return self

    @model_serializer(mode='wrap')
    def omit_unused_confirmation_metadata(self, handler):
        row = handler(self)
        if self.confirmation_basis is None:
            row.pop('confirmationBasis', None)
            row.pop('confirmation_basis', None)
        if self.recorded_at is None:
            row.pop('recordedAt', None)
            row.pop('recorded_at', None)
        return row


class EffectivePeriod(HealthProfileModel):
    start: datetime | None = None
    end: datetime | None = None

    @model_validator(mode='after')
    def validate_order(self) -> 'EffectivePeriod':
        if self.end is not None and self.start is not None and self.end < self.start:
            raise ValueError('effective period end cannot precede start')
        return self


class PhysicalFactReading(HealthProfileModel):
    fact_type: str
    category: FactCategory
    display_name: str
    value_type: FactValueType
    value: Any
    interpretation: str | None
    reference_range: dict[str, Any] | None
    assertion_level: AssertionLevel
    verification_status: VerificationStatus
    confidence: float = Field(ge=0, le=1)
    effective_period: EffectivePeriod
    status: FactStatus


class PhysicalFactOrigin(HealthProfileModel):
    """Source publication fence for a single Document physical fact."""

    source_id: str = Field(min_length=1, max_length=256)
    publication_revision: int = Field(strict=True, ge=1)
    reading: PhysicalFactReading | None


class PhysicalFactReservation(HealthProfileModel):
    source_id: str = Field(min_length=1, max_length=256)
    publication_id: str = Field(pattern=r'^publication_[0-9a-f]{64}$')
    publication_revision: int = Field(strict=True, ge=1)
    reading: PhysicalFactReading


class HealthFact(HealthProfileModel):
    fact_id: str
    user_id: str
    subject_id: str
    fact_type: str
    category: FactCategory
    display_name: str
    value_type: FactValueType
    value: Any
    interpretation: str | None = None
    reference_range: dict[str, Any] | None = None
    assertion_level: AssertionLevel
    verification_status: VerificationStatus
    confidence: float = Field(ge=0, le=1)
    effective_period: EffectivePeriod
    valid_until: datetime | None = None
    status: FactStatus
    sensitivity_level: str = 'health_sensitive'
    sources: tuple[FactSource, ...]
    created_at: datetime
    updated_at: datetime
    version: int = Field(default=1, ge=1)
    creation_request_id: str | None = None
    creation_request_slot: str | None = None
    creation_request_fingerprint: str | None = None
    last_request_id: str | None = None
    last_request_fingerprint: str | None = None
    replacement_fact_id: str | None = None
    basic_info_lifecycle_epoch: int | None = Field(default=None, ge=1)
    physical_occurrence_key: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    physical_occurrence_aliases: tuple[str, ...] = Field(default=(), max_length=1000)
    physical_origins: tuple[PhysicalFactOrigin, ...] = Field(default=(), max_length=1000)
    physical_reservations: tuple[PhysicalFactReservation, ...] = Field(default=(), max_length=1000)
    physical_reservation_only: bool = False

    @field_validator(
        'fact_id',
        'user_id',
        'subject_id',
        'fact_type',
        'display_name',
        'sensitivity_level',
    )
    @classmethod
    def validate_fact_text(cls, value: str, info) -> str:
        return _required_text(info.field_name, value)

    @model_validator(mode='after')
    def validate_sources(self) -> 'HealthFact':
        if not self.sources:
            raise ValueError('health fact requires at least one source')
        source_ids = {origin.source_id for origin in self.physical_origins}
        reserved_ids = {reservation.source_id for reservation in self.physical_reservations}
        if (
            bool(self.physical_origins or self.physical_reservation_only) != bool(self.physical_occurrence_key)
            or (self.physical_reservation_only and (self.status is not FactStatus.RETRACTED or self.physical_origins))
            or len(source_ids) != len(self.physical_origins)
            or len(reserved_ids) != len(self.physical_reservations)
            or (self.physical_reservations and self.physical_occurrence_key is None)
            or (self.physical_occurrence_key is not None and self.basic_info_lifecycle_epoch is None)
            or (self.physical_occurrence_aliases and self.physical_occurrence_key is None)
            or self.physical_occurrence_key in self.physical_occurrence_aliases
            or len(set(self.physical_occurrence_aliases)) != len(self.physical_occurrence_aliases)
            or any(re.fullmatch(r'[0-9a-f]{64}', key) is None for key in self.physical_occurrence_aliases)
        ):
            raise ValueError('a physical fact requires distinct publication origins and a fixed lifecycle')
        if self.assertion_level is AssertionLevel.MODEL_INFERRED and self.status is FactStatus.ACTIVE:
            raise ValueError('model inferred fact cannot become active directly')
        if self.effective_period.start is None and self.category is not FactCategory.DEMOGRAPHIC:
            report_sources = all(
                source.source_type == ProfileIngestionSourceType.HEALTH_REPORT for source in self.sources
            )
            automatic = self.verification_status is VerificationStatus.CONFIRMED and any(
                source.confirmation_basis == 'analysis_public' and source.recorded_at is not None
                for source in self.sources
            )
            if not report_sources or (self.status is FactStatus.ACTIVE and not automatic):
                raise ValueError('unknown observation time requires a demographic or eligible report fact')
        return self

    @model_serializer(mode='wrap')
    def omit_empty_physical_aliases(self, handler):
        row = handler(self)
        if not self.physical_occurrence_aliases:
            row.pop('physicalOccurrenceAliases', None)
            row.pop('physical_occurrence_aliases', None)
        return row


def fact_user_confirmed(fact: HealthFact) -> bool:
    return fact.assertion_level is AssertionLevel.USER_CONFIRMED or (
        fact.verification_status is VerificationStatus.CONFIRMED
        and not any(source.confirmation_basis == 'analysis_public' for source in fact.sources)
    )


def fact_in_lifecycle(fact: HealthFact, lifecycle_epoch: int) -> bool:
    """Internal source fence; untagged V1 facts retain their historical behavior.

    The existing storage marker also fences V2 clinical materialization. It is
    intentionally absent from public fact DTOs and frozen historical snapshots.
    """
    return fact.basic_info_lifecycle_epoch is None or fact.basic_info_lifecycle_epoch == lifecycle_epoch


class ProfileActor(HealthProfileModel):
    type: str
    id: str | None = None


class ProfileEvent(HealthProfileModel):
    event_id: str
    user_id: str
    subject_id: str
    sequence: int = Field(ge=1)
    event_type: str
    fact_id: str | None = None
    previous_fact_version: int | None = Field(default=None, ge=1)
    new_fact_version: int | None = Field(default=None, ge=1)
    reason_code: str
    actor: ProfileActor
    request_id: str | None = None
    source_ref: dict[str, Any] | None = None
    operation_result: dict[str, Any] | None = None
    created_at: datetime


class HealthProfileView(HealthProfileModel):
    user_id: str
    subject_id: str
    schema_version: int = Field(default=1, ge=1)
    profile_version: int = Field(default=1, ge=1)
    basic_info_revision: int = Field(default=0, ge=0)
    basic_info_lifecycle_epoch: int | None = Field(default=None, ge=1)
    subject_summary: dict[str, Any] = Field(default_factory=dict)
    category_counts: dict[str, int] = Field(default_factory=dict)
    key_fact_ids: tuple[str, ...] = ()
    pending_confirmation_count: int = Field(default=0, ge=0)
    last_source_at: datetime | None = None
    last_request_id: str | None = None
    updated_at: datetime


class ProfileProcessingSummary(HealthProfileModel):
    """仅持久化数量和稳定原因码，不包含健康事实值或模型输出。"""

    source_item_count: int | None = Field(default=None, ge=0)
    candidate_count: int = Field(ge=0)
    fact_count: int = Field(ge=0)
    replayed_fact_count: int = Field(ge=0)
    rejected_candidate_count: int = Field(ge=0)
    skipped_item_count: int = Field(default=0, ge=0)
    reason_codes: tuple[str, ...] = ()

    @field_validator('reason_codes')
    @classmethod
    def validate_reason_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > 32 or any(re.fullmatch(r'[a-z][a-z0-9_.]{0,95}', value) is None for value in values):
            raise ValueError('processing reasons must be bounded stable codes')
        return tuple(dict.fromkeys(values))


class ProfileJobRedriveRecord(HealthProfileModel):
    """画像摄取任务一次人工重驱的非健康数据审计摘要。"""

    redriven_at: datetime
    from_status: ProfileJobStatus
    attempts: int = Field(ge=0)
    reason_code: str
    actor_id: str
    error_code: str | None = None
    graph_version: str | None = None
    policy_version: str | None = None
    extractor_version: str | None = None
    completed_at: datetime | None = None
    processing_summary: ProfileProcessingSummary | None = None

    @field_validator(
        'reason_code',
        'actor_id',
    )
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _required_text(info.field_name, value)

    @field_validator(
        'error_code',
        'graph_version',
        'policy_version',
        'extractor_version',
    )
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @model_validator(mode='after')
    def validate_source_status(self) -> 'ProfileJobRedriveRecord':
        if self.from_status not in {
            ProfileJobStatus.COMPLETED,
            ProfileJobStatus.DEAD_LETTER,
        }:
            raise ValueError(
                'profile job can only be redriven from a terminal status',
            )
        return self


class HealthProfileJob(HealthProfileModel):
    job_id: str
    idempotency_key: str
    idempotency_fingerprint: str
    source_type: ProfileIngestionSourceType
    source_id: str
    source_revision: int = Field(ge=1)
    user_id: str
    subject_id: str | None = None
    status: ProfileJobStatus = ProfileJobStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    next_retry_at: datetime | None = None
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    graph_version: str | None = None
    policy_version: str | None = None
    extractor_version: str | None = None
    error_code: str | None = None
    completed_at: datetime | None = None
    processing_summary: ProfileProcessingSummary | None = None
    redrive_history: tuple[ProfileJobRedriveRecord, ...] = Field(
        default=(),
        max_length=20,
    )
    created_at: datetime
    updated_at: datetime

    @field_validator(
        'job_id',
        'idempotency_key',
        'idempotency_fingerprint',
        'source_id',
        'user_id',
    )
    @classmethod
    def validate_job_text(cls, value: str, info) -> str:
        return _required_text(info.field_name, value)

    @field_validator(
        'subject_id',
        'lease_owner',
        'lease_token',
        'graph_version',
        'policy_version',
        'extractor_version',
        'error_code',
    )
    @classmethod
    def validate_optional_job_text(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @model_validator(mode='after')
    def validate_lifecycle(self) -> 'HealthProfileJob':
        lease_values = (
            self.lease_owner,
            self.lease_token,
            self.lease_expires_at,
        )
        has_full_lease = all(value is not None for value in lease_values)
        has_any_lease = any(value is not None for value in lease_values)

        if self.status is ProfileJobStatus.RUNNING:
            if not has_full_lease:
                raise ValueError('running profile job requires a complete lease')
            if self.attempts < 1:
                raise ValueError('running profile job requires an attempt')
        elif has_any_lease:
            raise ValueError('only running profile job may hold a lease')

        if self.status is ProfileJobStatus.RETRY_WAIT:
            if self.next_retry_at is None or self.error_code is None:
                raise ValueError(
                    'retrying profile job requires retry time and error code',
                )
        elif self.next_retry_at is not None:
            raise ValueError('only retrying profile job may have retry time')

        if self.status is ProfileJobStatus.COMPLETED:
            if self.completed_at is None:
                raise ValueError('completed profile job requires completed time')
        elif self.completed_at is not None:
            raise ValueError('only completed profile job may have completed time')

        if self.status is ProfileJobStatus.DEAD_LETTER and self.error_code is None:
            raise ValueError('dead letter profile job requires an error code')
        return self


class MemoryCandidate(HealthProfileModel):
    """LLM 或确定性解析器输出、尚未写入正式画像的候选事实。"""

    candidate_id: str
    fact_type: str
    display_name: str
    value_type: FactValueType
    value: Any
    assertion_level: AssertionLevel
    confidence: float = Field(ge=0, le=1)
    effective_period: EffectivePeriod
    source: FactSource
    subject_id: str | None = None
    subject_hint: str | None = None
    subject_kind_hint: SubjectKind | None = None
    interpretation: str | None = None
    reference_range: dict[str, Any] | None = None
    evidence_text: str | None = None
    report_verification: VerificationStatus | None = Field(default=None, exclude=True)

    @field_validator('candidate_id', 'fact_type', 'display_name')
    @classmethod
    def validate_candidate_text(cls, value: str, info) -> str:
        return _required_text(info.field_name, value)

    @field_validator(
        'subject_id',
        'subject_hint',
        'interpretation',
        'evidence_text',
    )
    @classmethod
    def validate_optional_candidate_text(
        cls,
        value: str | None,
    ) -> str | None:
        return _optional_text(value)


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
    normalized = _required_text('value', value)
    return normalized
