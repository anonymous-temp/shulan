"""Pure source-presence proofs and explicit fresh-source admission contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Literal
from typing import Protocol

from pydantic import Field
from pydantic import field_validator

from .contracts import ProfileContract
from .contracts import ProfileScope
from .contracts import ProfileSourceSnapshotRefV1

SourceType = Literal['health_report', 'record', 'questionnaire', 'chat']


class ProfilePresenceSectionV1(ProfileContract):
    kind: Literal['reports', 'report_revisions', 'jobs', 'fact_contributions', 'publications']
    observed_count: int = Field(ge=0)
    truncated: bool = False
    evidence_digest: str = Field(pattern=r'^[0-9a-f]{64}$')


class ProfileSourcePresenceV1(ProfileContract):
    """An observed proof, not a durable authorization or a claim of atomic reads."""

    contract_version: Literal['profile-source-presence.v1'] = 'profile-source-presence.v1'
    scope: ProfileScope
    source_type: SourceType
    source_id: str = Field(min_length=1, max_length=256)
    status: Literal['absent', 'present', 'missing', 'unavailable']
    read_at: datetime
    sections: tuple[ProfilePresenceSectionV1, ...] = ()
    proof_ref: str | None = None
    content_digest: str | None = None
    error_code: str | None = None


class ProfileOwnerAdmissionFenceV1(ProfileContract):
    """A provider-verified owner proof. Prepared publish=False bundles are permitted.

    The registered reader verifies its own durable formal-history/intent ledger.
    A fixed proof read and a check that the fence is still held are separate calls.
    """

    contract_version: Literal['profile-owner-admission-fence.v1'] = 'profile-owner-admission-fence.v1'
    scope: ProfileScope
    source_type: SourceType
    source_id: str = Field(min_length=1, max_length=256)
    fence_id: str = Field(min_length=1, max_length=512)
    fence_generation: int = Field(strict=True, ge=1)
    prepared_manifest_ref: str = Field(min_length=1, max_length=512)
    formal_history_absent: Literal[True]
    legacy_intents_absent: Literal[True]
    active_legacy_writers: Literal[0]
    active_legacy_workers: Literal[0]
    legacy_admission_blocked: Literal[True]
    issued_at: datetime

    @field_validator('active_legacy_writers', 'active_legacy_workers', mode='before')
    @classmethod
    def strict_count(cls, value):
        if type(value) is not int:
            raise ValueError('active legacy counts must be integers')
        return value

    @field_validator('issued_at')
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('issued_at must be timezone-aware')
        return value


class ProfileOwnerAdmissionFencePort(Protocol):
    async def read_admission_fence(
        self, *, scope: ProfileScope, snapshot_ref: ProfileSourceSnapshotRefV1
    ) -> dict[str, Any]: ...

    async def is_admission_fence_held(
        self, *, scope: ProfileScope, snapshot_ref: ProfileSourceSnapshotRefV1, content_digest: str
    ) -> bool: ...


class ProfileFreshSourceAuthorizationV1(ProfileContract):
    admission_kind: Literal['fresh'] = 'fresh'
    scope: ProfileScope
    source_type: SourceType
    source_id: str = Field(min_length=1, max_length=256)
    authorization_id: str = Field(min_length=1, max_length=256)
    owner_fence_ref: ProfileSourceSnapshotRefV1
    owner_fence_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    presence_proof_ref: str = Field(min_length=1, max_length=512)
    presence_proof_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    material_contract_version: str
    material_snapshot_ref: ProfileSourceSnapshotRefV1
    material_content_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    processing_revision: str | None = None
    authorized_at: datetime

    @field_validator('authorized_at')
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('authorized_at must be timezone-aware')
        return value


class ProfileFreshSourceStateV1(ProfileContract):
    authorization: ProfileFreshSourceAuthorizationV1
    status: Literal['blocked', 'ready']
    updated_at: datetime
    presence_proof: ProfileSourcePresenceV1 | None = None


class ProfileSourcePresencePort(Protocol):
    async def read_legacy_presence(
        self, scope: ProfileScope, *, source_type: SourceType, source_id: str, limit: int = 1000
    ) -> ProfileSourcePresenceV1: ...


class ProfileArchivedSourceMaterialV1(ProfileContract):
    contract_version: Literal['profile-archived-source-material.v1'] = 'profile-archived-source-material.v1'
    scope: ProfileScope
    canonical_report_id: str
    report_id: str
    legacy_source_revision: int
    document_id: str
    source_fingerprint: str
    created: bool
    content_digest: str
