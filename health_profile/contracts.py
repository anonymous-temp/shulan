"""Versioned public Profile data contracts; no consumer prompts or provider schemas."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_serializer
from pydantic import model_validator


def _camel(name: str) -> str:
    head, *tail = name.split('_')
    return head + ''.join(item.title() for item in tail)


class ProfileContract(BaseModel):
    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True, extra='forbid', frozen=True)

    @model_validator(mode='before')
    @classmethod
    def copy_input(cls, value):
        return deepcopy(value) if isinstance(value, dict) else value


class ProfileScope(ProfileContract):
    """Server-resolved ownership. A caller must not substitute another user's subject."""

    user_id: str = Field(min_length=1, max_length=256)
    subject_id: str = Field(min_length=1, max_length=256)

    @field_validator('user_id', 'subject_id')
    @classmethod
    def nonblank(cls, value):
        if value != value.strip() or not value.strip():
            raise ValueError('scope identifiers must be nonblank and already normalized')
        return value


class ProfileSourceCutoverAuthorizationV1(ProfileContract):
    """Explicit source-owner attestation; Profile does not stop external publishers."""

    scope: ProfileScope
    source_type: Literal['health_report', 'record', 'questionnaire', 'chat']
    source_id: str = Field(min_length=1, max_length=256)
    authorization_id: str = Field(min_length=1, max_length=256)
    legacy_publishers_quiesced: Literal[True]
    legacy_workers_quiesced: Literal[True]
    legacy_source_intents_retired: Literal[True] | None = None
    material_contract_version: str | None = None
    material_snapshot_ref: ProfileSourceSnapshotRefV1 | None = None
    material_content_digest: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    processing_revision: str | None = None
    legacy_quiescence_evidence_ref: str | None = None
    authorized_at: datetime

    @field_validator('authorized_at')
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('authorized_at must be timezone-aware')
        return value


class ProfileSourceCutoverStateV1(ProfileContract):
    authorization: ProfileSourceCutoverAuthorizationV1
    status: Literal['blocked', 'ready']
    updated_at: datetime


class ProfileSourceSnapshotRefV1(ProfileContract):
    provider: str = Field(min_length=1, max_length=96, pattern=r'^[a-z][a-z0-9_.-]*$')
    contract_version: str = Field(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9_.-]+$')
    snapshot_id: str = Field(min_length=1, max_length=512)


class ProfileSourceEventV1(ProfileContract):
    """Immutable source envelope for one scope/type/source publication chain.

    source_revision is the provider's internal material revision: a later
    publication in this same chain must not regress it. Never compare revisions
    across sources. publication_revision/prior_revision order publication delivery.
    legacy_source_revision and external API sourceRevision are separate opaque
    source identifiers, including hashed identifiers, not material ordering keys.
    """

    schema_version: Literal['profile-source-event.v1'] = 'profile-source-event.v1'
    event_id: str = Field(min_length=1, max_length=256)
    scope: ProfileScope
    source_type: Literal['health_report', 'record', 'questionnaire', 'chat']
    source_id: str = Field(min_length=1, max_length=256)
    source_revision: int = Field(strict=True, ge=1)
    legacy_source_revision: int | None = Field(default=None, strict=True, ge=1)
    publication_revision: int = Field(strict=True, ge=1)
    prior_revision: int | None = Field(default=None, strict=True, ge=1)
    event_kind: Literal['publish', 'correct', 'retract']
    occurred_at: datetime
    content_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    snapshot_ref: ProfileSourceSnapshotRefV1

    @field_validator('occurred_at')
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('occurred_at must be timezone-aware')
        return value

    @field_validator('event_id', 'source_id')
    @classmethod
    def nonblank(cls, value):
        if value != value.strip() or not value.strip():
            raise ValueError('source identifiers must be nonblank and already normalized')
        return value

    @model_validator(mode='after')
    def contiguous_revision(self):
        if self.publication_revision == 1:
            if self.prior_revision is not None or self.event_kind != 'publish':
                raise ValueError('first publication must publish with no prior revision')
        elif self.prior_revision != self.publication_revision - 1 or self.event_kind == 'publish':
            raise ValueError('later publications must correct/retract the immediately preceding revision')
        return self


class ProfilePublicationWatermarkV1(ProfileContract):
    scope: ProfileScope
    source_type: str
    source_id: str
    accepted_publication_revision: int = 0
    applied_publication_revision: int = 0
    settled_publication_revision: int = 0
    accepted_retraction_revision: int = 0
    applied_retraction_revision: int = 0
    applying_publication_revision: int | None = None
    affected_subject_ids: tuple[str, ...] = ()


class ProfileSourceReceiptV1(ProfileContract):
    contract_version: Literal['profile-source-receipt.v1'] = 'profile-source-receipt.v1'
    event_id: str
    publication_id: str
    scope: ProfileScope
    source_type: str
    source_id: str
    publication_revision: int
    status: Literal['received', 'waiting', 'processing', 'retry_wait', 'partial', 'applied', 'failed', 'superseded']
    received_at: datetime
    updated_at: datetime
    attempts: int = 0
    error_code: str | None = None
    profile_revision: int | None = None
    profile_revisions: dict[str, int | None] = Field(default_factory=dict)
    processing_revision: str | None = None
    application_state: Literal['not_applied', 'incomplete', 'partial', 'applied', 'superseded_by_retraction'] = (
        'not_applied'
    )
    partial_application: dict[str, Any] | None = None
    superseded_by_publication_revision: int | None = None
    watermark: ProfilePublicationWatermarkV1


class ProfileFactSourceV1(ProfileContract):
    source_type: str
    source_id: str
    source_revision: int
    pointer: str | None = None
    observed_at: datetime | None = None
    excerpt_hash: str | None = None
    confirmation_basis: Literal['analysis_public'] | None = None
    recorded_at: datetime | None = None

    @model_serializer(mode='wrap')
    def serialize_source(self, handler, info):
        payload = handler(self)
        if self.confirmation_basis is None:
            payload.pop('confirmationBasis' if info.by_alias else 'confirmation_basis', None)
        if self.recorded_at is None:
            payload.pop('recordedAt' if info.by_alias else 'recorded_at', None)
        return payload


class ProfileFactV1(ProfileContract):
    fact_id: str
    fact_type: str
    category: str
    display_name: str
    value_type: str
    value: Any
    interpretation: str | None = None
    reference_range: dict[str, Any] | None = None
    assertion_level: str
    verification_status: str
    confidence: float
    effective_period: dict[str, datetime | None]
    valid_until: datetime | None = None
    status: str
    sources: tuple[ProfileFactSourceV1, ...]
    version: int
    created_at: datetime
    updated_at: datetime


class ProfileSubjectV1(ProfileContract):
    subject_id: str
    kind: str
    display_name: str | None = None
    relationship: dict[str, Any] | None = None
    status: str
    version: int


class ProfileSnapshotV1(ProfileContract):
    """One subject's current authoritative data, copied for a consumer's fixed input."""

    contract_version: Literal['profile-snapshot.v1'] = 'profile-snapshot.v1'
    snapshot_id: str
    user_id: str
    scope: ProfileScope | None = None
    status: Literal['ready', 'missing', 'unavailable']
    profile_revision: int | None = None
    read_at: datetime
    subject: ProfileSubjectV1 | None = None
    facts: tuple[ProfileFactV1, ...] = ()
    truncated: bool = False
    stale: bool = False
    coverage: dict[str, Any] = Field(default_factory=dict)
    source_watermarks: tuple[ProfilePublicationWatermarkV1, ...] = ()
    error_code: str | None = None


class ProfileFactPageV1(ProfileContract):
    """One bounded pure-read page, with optimistic continuation identity."""

    contract_version: Literal['profile-fact-page.v1'] = 'profile-fact-page.v1'
    sort_version: Literal['effective-start-desc-fact-id-asc.v1'] = 'effective-start-desc-fact-id-asc.v1'
    page_id: str
    user_id: str
    scope: ProfileScope | None = None
    status: Literal['ready', 'missing', 'unavailable', 'restart_required']
    profile_revision: int | None = None
    read_at: datetime
    subject: ProfileSubjectV1 | None = None
    facts: tuple[ProfileFactV1, ...] = ()
    next_cursor: str | None = None
    truncated: bool = False
    stale: bool = False
    coverage: dict[str, Any] = Field(default_factory=dict)
    source_watermarks: tuple[ProfilePublicationWatermarkV1, ...] = ()
    error_code: str | None = None


class ProfileLegacyIngestionStatusV1(ProfileContract):
    """Read-only status of one exact legacy health-report ingestion job."""

    contract_version: Literal['profile-legacy-ingestion-status.v1'] = 'profile-legacy-ingestion-status.v1'
    scope: ProfileScope
    canonical_report_id: str
    legacy_source_revision: int
    status: Literal['ready', 'missing', 'unavailable']
    read_at: datetime
    ingestion_status: Literal['pending', 'running', 'retry_wait', 'completed', 'dead_letter'] | None = None
    job_present: bool = False
    error_code: str | None = None


def profile_content_digest(value: Any) -> str:
    """Canonical SHA-256 for a public JSON snapshot, independent of transport bytes."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()
