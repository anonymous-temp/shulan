"""Versioned public demographic evidence and durable confirmation receipts."""

from __future__ import annotations

from datetime import date
from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import Field
from pydantic import field_validator

from .contracts import ProfileContract
from .contracts import ProfileFactV1
from .contracts import ProfileSnapshotV1

BasicInfoField = Literal['name', 'gender', 'age']
SourceReliability = Literal['low', 'medium', 'very_high', 'unknown']
AgeBasis = Literal['reported_at_report', 'confirmed_at_time', 'dob_derived']


class BasicInfoValuesV1(ProfileContract):
    name: str = Field(min_length=1)
    age: int = Field(strict=True, ge=0, le=150)
    gender: Literal['male', 'female']

    @field_validator('name', mode='before')
    @classmethod
    def name_text(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError('basic_info.invalid_name')
        return value.strip()

    @field_validator('gender', mode='before')
    @classmethod
    def gender_text(cls, value):
        return {'男': 'male', '女': 'female'}.get(value, value) if isinstance(value, str) else value

    def as_dict(self):
        return self.model_dump(mode='json', by_alias=True)


class BasicInfoEvidenceV1(ProfileContract):
    field: BasicInfoField
    value: Any
    source_reliability: Literal['low', 'medium']
    observed_at: str | datetime | None = None
    reason: str = Field(min_length=1, max_length=2000)
    evidence: dict[str, Any]

    @field_validator('observed_at')
    @classmethod
    def aware_observation(cls, value):
        if isinstance(value, str):
            try:
                date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)
            except ValueError as error:
                raise ValueError('basic_info.invalid_observation_time') from error
        return value


class BasicInfoReportRetirement(ProfileContract):
    """Verified current Document retirement carried in a fixed Profile application plan."""

    field: BasicInfoField
    contribution_id: str = Field(min_length=1, max_length=256)
    identity_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    retirement_evidence_ref: str = Field(min_length=1, max_length=512)


class BasicInfoFieldReceiptV1(ProfileContract):
    status: Literal['pending', 'applied', 'no_change', 'superseded', 'blocked', 'cancelled']
    fact_id: str | None = None
    source_reliability: SourceReliability = 'very_high'


class BasicInfoCommandReceiptV1(ProfileContract):
    contract_version: Literal['profile-basic-info-command.v1'] = 'profile-basic-info-command.v1'
    command_id: str
    operation_id: str
    user_id: str
    subject_id: str
    lifecycle_epoch: int
    field_epochs: dict[str, int]
    payload_hash: str
    confirmed_at: datetime
    status: Literal['registered', 'pending', 'applied', 'no_change', 'superseded', 'blocked', 'cancelled']
    fields: dict[str, BasicInfoFieldReceiptV1]
    profile_version: int | None = None
    error_code: str | None = None

    def as_dict(self):
        return self.model_dump(mode='json', by_alias=True)


class BasicInfoCommandPageV1(ProfileContract):
    items: tuple[BasicInfoCommandReceiptV1, ...]
    next_cursor: str | None = None

    def as_dict(self):
        return self.model_dump(mode='json', by_alias=True)


class BasicInfoCurrentFieldV2(ProfileContract):
    value: Any = None
    source_reliability: SourceReliability = 'unknown'
    observed_at: str | datetime | None = None
    confirmed_at: datetime | None = None
    fact_id: str | None = None
    source_ref: dict[str, Any] | None = None
    protected: bool = False
    governance_epoch: int = 0
    age_basis: AgeBasis | None = None
    as_of: datetime | None = None


class BasicInfoSourceV2(BasicInfoCurrentFieldV2):
    source_key: str
    field: BasicInfoField
    active: bool
    current: bool
    deleted: bool = False


class BasicInfoSnapshotV2(ProfileContract):
    contract_version: Literal['profile-basic-info.v2'] = 'profile-basic-info.v2'
    user_id: str
    subject_id: str | None = None
    lifecycle_epoch: int | None = None
    revision: int = 0
    as_of: datetime
    fields: dict[str, BasicInfoCurrentFieldV2]
    sources: tuple[BasicInfoSourceV2, ...] = ()

    def as_dict(self):
        return self.model_dump(mode='json', by_alias=True)


class ProfileFactV2(ProfileFactV1):
    source_reliability: SourceReliability | None = None
    current_basic_info: bool | None = None


class ProfileSnapshotV2(ProfileSnapshotV1):
    contract_version: Literal['profile-snapshot.v2'] = 'profile-snapshot.v2'
    facts: tuple[ProfileFactV2, ...] = ()
    basic_info: BasicInfoSnapshotV2 | None = None
