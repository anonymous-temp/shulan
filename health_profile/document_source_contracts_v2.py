"""The single current neutral Document source contract consumed by Profile."""

from datetime import date
from datetime import datetime
from math import isfinite
from typing import Any
from typing import Literal
from typing import Protocol

from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

from .contracts import ProfileContract
from .contracts import ProfileScope
from .contracts import ProfileSourceSnapshotRefV1

DOCUMENT_SOURCE_CONTRACT = 'document.source-observations.v2'
DOCUMENT_INVENTORY_CONTRACT = 'document.source-inventory.v2'
DOCUMENT_CONTRIBUTION_CONTRACT = 'document.source-contribution.v2'
DOCUMENT_PROCESSING_REVISION = 'profile-document-source-v2'


class DocumentSourceSelection(ProfileContract):
    profile_name: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=128)
    implementation_revision: str = Field(min_length=1, max_length=128)


class DocumentPhysicalOccurrence(ProfileContract):
    """An original position verified by the Document provider."""

    document_id: str = Field(min_length=1, max_length=256)
    source_checksum: str = Field(pattern=r'^[0-9a-f]{64}$')
    locator_revision: str = Field(min_length=1, max_length=128)
    locator: str = Field(min_length=1, max_length=1024)
    proof_snapshot_ref: str | None = Field(default=None, pattern=r'^document_snapshot_[0-9a-f]{64}$')

    @field_validator('document_id', 'locator_revision', 'locator')
    @classmethod
    def exact_text(cls, value):
        if not value or value != value.strip():
            raise ValueError('physical occurrences require exact position identifiers')
        return value


class DocumentFixedObservationRef(ProfileContract):
    document_id: str = Field(min_length=1, max_length=256)
    interpretation_round: int = Field(strict=True, ge=1)
    observation_ref: str = Field(min_length=1, max_length=1024, pattern=r'^/')


class DocumentSourceObservation(ProfileContract):
    verification_status: Literal['verified_source', 'unconfirmed', 'conflicted', 'confirmed'] | None = None
    confirmation_basis: Literal['analysis_public'] | None = None
    patient_slot: str | None = Field(default=None, min_length=1, max_length=128)
    source_document_id: str | None = Field(default=None, min_length=1, max_length=256)
    observation_ref: str = Field(min_length=1, max_length=1024, pattern=r'^/')
    identity_status: Literal['verified', 'identity_unresolved']
    stable_observation_id: str | None = None
    label: str | None
    raw_value: Any
    unit: str | None = None
    reference_range: Any = None
    flag: str | None = None
    observed_at: str | None = None
    kind: str | None = None
    method: str | None = None
    specimen: str | None = None
    narrative_type: str | None = None
    evidence: list[Any] = Field(default_factory=list, max_length=100)
    source_id: str | None = None
    source_category: str | None = None
    source_observation_id: str | None = None
    source_section: int | None = Field(default=None, strict=True, ge=0)
    physical_occurrence: DocumentPhysicalOccurrence | None = None

    @model_validator(mode='after')
    def identity(self):
        if (self.identity_status == 'verified') != bool(self.stable_observation_id):
            raise ValueError('observation identity requires its fixed source proof')
        if (self.verification_status == 'confirmed') != (self.confirmation_basis == 'analysis_public'):
            raise ValueError('automatic confirmation requires its fixed Analysis provenance')
        return self


class DocumentMaterialRef(ProfileContract):
    snapshot_id: str = Field(min_length=1, max_length=512)
    contract_version: Literal['document.material.v1'] = 'document.material.v1'
    content_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1, max_length=256)
    source_revision: int = Field(strict=True, ge=1)


class DocumentSourceQuality(ProfileContract):
    status: Literal['accepted']
    details: dict[str, Any] = Field(default_factory=dict)


class DocumentPatientIdentity(ProfileContract):
    source_document_id: str = Field(min_length=1, max_length=256)
    patient_slot: str | None = Field(default=None, min_length=1, max_length=128)
    field: Literal['name', 'gender']
    value: str = Field(min_length=1, max_length=512)

    @model_validator(mode='after')
    def valid_value(self):
        if not self.value.strip() or (self.field == 'gender' and self.value not in ('男', '女')):
            raise ValueError('patient routing requires a recognized identity field')
        return self


class DocumentBasicInfoQuote(ProfileContract):
    quote: str = Field(min_length=1)
    start: int = Field(strict=True, ge=0)
    end: int = Field(strict=True, gt=0)

    @model_validator(mode='after')
    def fixed_span(self):
        if not self.quote.strip() or self.end - self.start != len(self.quote):
            raise ValueError('basic information evidence requires an exact nonempty span')
        return self


class DocumentBasicInfo(ProfileContract):
    patient_slot: str | None = Field(default=None, min_length=1, max_length=128)
    source_document_id: str | None = Field(default=None, min_length=1, max_length=256)
    field: Literal['name', 'gender', 'age']
    value: Any
    source_reliability: Literal['low', 'medium']
    observed_at: str | None
    reason: str = Field(min_length=1, max_length=2000)
    evidence: DocumentBasicInfoQuote
    subject_status: Literal['single']
    subject_ref: str | None
    subject_evidence: DocumentBasicInfoQuote | None
    contribution_id: str = Field(min_length=1, max_length=256)
    understanding_snapshot_ref: str = Field(pattern=r'^document_snapshot_[0-9a-f]{64}$')
    facts_snapshot_ref: str = Field(pattern=r'^document_snapshot_[0-9a-f]{64}$')
    interpretation_round: int = Field(strict=True, ge=1)
    interpretation_revision: str = Field(min_length=1, max_length=128)

    @field_validator('observed_at')
    @classmethod
    def original_date(cls, value):
        if value is not None:
            if len(value) == 10:
                date.fromisoformat(value)
            else:
                if len(value) <= 10 or value[10] not in ('T', ' '):
                    raise ValueError('observation time requires an ISO calendar date')
                datetime.fromisoformat(value)
        return value

    @model_validator(mode='after')
    def patient_value(self):
        value = self.value
        valid = (
            isinstance(value, str) and bool(value.strip())
            if self.field == 'name'
            else value in ('男', '女')
            if self.field == 'gender'
            else type(value) in (int, float) and isfinite(value) and 0 <= value <= 150
        )
        if not valid or not self.reason.strip():
            raise ValueError('basic information needs one valid patient value and source explanation')
        return self


class DocumentContributionRef(ProfileContract):
    contribution_id: str = Field(min_length=1, max_length=256)
    kind: Literal['clinical', 'basic_info']
    contract_version: Literal['document.source-contribution.v2']
    snapshot_id: str = Field(min_length=1, max_length=512)
    content_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_revision: int = Field(strict=True, ge=1)
    implementation_selection: DocumentSourceSelection
    basic_info: tuple[DocumentBasicInfo, ...] = Field(max_length=1)
    clinical_content_contract: Literal['document.clinical-content.v1'] | None
    clinical_content_digest: str | None = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def independent_contribution(self):
        if self.kind == 'clinical':
            if self.basic_info or self.clinical_content_contract is None or self.clinical_content_digest is None:
                raise ValueError('clinical material requires its own fixed content digest')
        elif (
            len(self.basic_info) != 1
            or self.clinical_content_contract is not None
            or self.clinical_content_digest is not None
        ):
            raise ValueError('basic information must be independent from clinical input')
        if any(row.contribution_id != self.contribution_id for row in self.basic_info):
            raise ValueError('basic information must bind its own contribution')
        return self


class DocumentClinicalContribution(DocumentContributionRef):
    report_type: str = Field(min_length=1, max_length=64)
    report_time: str | None
    observations: tuple[DocumentSourceObservation, ...] = Field(max_length=1000)

    @model_validator(mode='after')
    def clinical_only(self):
        if self.kind != 'clinical':
            raise ValueError('clinical data cannot be a basic-only contribution')
        return self

    def reference(self):
        return DocumentContributionRef.model_validate(
            self.model_dump(exclude={'report_type', 'report_time', 'observations'})
        )


class DocumentRetiredContribution(DocumentContributionRef):
    retirement_kind: Literal['superseded', 'retracted', 'corrected']
    retirement_evidence_ref: str = Field(min_length=1, max_length=512)
    superseded_by_contribution_id: str | None


class DocumentCurrentFence(ProfileContract):
    admission_kind: Literal['current']
    evidence_ref: str = Field(min_length=1, max_length=512)


class _DocumentIdentity(ProfileContract):
    scope: ProfileScope
    source_type: Literal['health_report']
    source_id: str = Field(min_length=1, max_length=256)
    source_revision: int = Field(strict=True, ge=1)


class DocumentSourceCoverage(_DocumentIdentity):
    contract_version: Literal['document.source-coverage.v2']
    lifecycle_epoch: int = Field(strict=True, ge=1)
    inventory_snapshot_ref: ProfileSourceSnapshotRefV1
    inventory_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    ordered_contribution_ids: tuple[str, ...] = Field(max_length=1000)
    complete: Literal[True]


class DocumentSourcePayload(ProfileContract):
    quality: DocumentSourceQuality
    material_snapshot_refs: tuple[DocumentMaterialRef, ...] = Field(min_length=1, max_length=1000)
    basic_info: tuple[DocumentBasicInfo, ...] = Field(default=(), max_length=1000)
    clinical_available: bool = Field(strict=True)
    contributions: tuple[DocumentContributionRef, ...] = Field(max_length=1000)
    clinical_contributions: tuple[DocumentClinicalContribution, ...] = Field(default=(), max_length=1000)
    coverage: DocumentSourceCoverage


class DocumentCompleteSource(_DocumentIdentity):
    contract_version: Literal['document.source-observations.v2']
    payload: DocumentSourcePayload


class DocumentInventoryPayload(ProfileContract):
    fence: DocumentCurrentFence
    lifecycle_epoch: int = Field(strict=True, ge=1)
    active_set_evidence_ref: str = Field(min_length=1, max_length=512)
    retired_contributions: tuple[DocumentRetiredContribution, ...] = Field(max_length=1000)
    contributions: tuple[DocumentContributionRef, ...] = Field(max_length=1000)


class DocumentSourceInventory(_DocumentIdentity):
    contract_version: Literal['document.source-inventory.v2']
    payload: DocumentInventoryPayload


class ProfileDocumentSourceReadiness(ProfileContract):
    contract_version: Literal['profile-document-source-readiness.v2'] = 'profile-document-source-readiness.v2'
    scope: ProfileScope
    source_id: str
    source_revision: int
    lifecycle_epoch: int = Field(strict=True, ge=1)
    snapshot_ref: ProfileSourceSnapshotRefV1
    content_digest: str
    processing_revision: Literal['profile-document-source-v2'] = DOCUMENT_PROCESSING_REVISION
    inventory_snapshot_ref: ProfileSourceSnapshotRefV1
    inventory_digest: str
    contribution_ids: tuple[str, ...]


class ProfileDocumentSourceReader(Protocol):
    async def read_source_observations(self, *, scope: ProfileScope, snapshot_id: str) -> dict: ...
    async def read_fixed_observation_refs(self, *, scope: ProfileScope, snapshot_id: str) -> dict: ...
    async def read_source_inventory(self, *, scope: ProfileScope, snapshot_id: str) -> dict: ...
    async def read_retraction(self, *, scope: ProfileScope, snapshot_id: str) -> dict: ...
