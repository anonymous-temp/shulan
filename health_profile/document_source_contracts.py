"""Profile consumption contract for complete Document-owned neutral observations.

The producer owns capture and its public inventory. These types never import its
private report projection or pretend that a reader can establish quiescence.
"""

from typing import Any
from typing import Literal
from typing import Protocol

from pydantic import Field
from pydantic import model_validator

from .contracts import ProfileContract
from .contracts import ProfileScope
from .contracts import ProfileSourceSnapshotRefV1
from .contracts import profile_content_digest

DOCUMENT_SOURCE_CONTRACT = 'document.source-observations.v1'
DOCUMENT_INVENTORY_CONTRACT = 'document.source-inventory.v1'
DOCUMENT_CONTRIBUTION_CONTRACT = 'document.source-contribution.v1'
DOCUMENT_PROCESSING_REVISION = 'profile-document-source-v1'


class DocumentSourceSelectionV1(ProfileContract):
    profile_name: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=128)
    implementation_revision: str = Field(min_length=1, max_length=128)


class DocumentSourceObservationV1(ProfileContract):
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

    @model_validator(mode='after')
    def identity(self):
        if (self.identity_status == 'verified') != bool(self.stable_observation_id):
            raise ValueError('observation identity must be explicitly verified or unresolved')
        profile_content_digest(self.model_dump(mode='json', by_alias=True))
        return self


class DocumentContributionRefV1(ProfileContract):
    contribution_id: str = Field(min_length=1, max_length=256)
    kind: Literal['base', 'legacy_report_source']
    contract_version: Literal['document.source-contribution.v1'] = DOCUMENT_CONTRIBUTION_CONTRACT
    snapshot_id: str = Field(min_length=1, max_length=512)
    content_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_revision: int = Field(strict=True, ge=1)
    legacy_source_revision: int | None = Field(default=None, strict=True, ge=1)
    implementation_selection: DocumentSourceSelectionV1


class DocumentContributionV1(DocumentContributionRefV1):
    report_type: str = Field(min_length=1, max_length=64)
    report_time: str | None = None
    observations: tuple[DocumentSourceObservationV1, ...] = Field(max_length=1000)

    def reference(self):
        return DocumentContributionRefV1.model_validate(
            self.model_dump(exclude={'report_type', 'report_time', 'observations'})
        )

    def canonical(self, *, scope, source_id):
        return {
            'scope': scope.model_dump(mode='json', by_alias=True),
            'contractVersion': self.contract_version,
            'sourceType': 'health_report',
            'sourceId': source_id,
            'sourceRevision': self.source_revision,
            'payload': self.model_dump(
                mode='json',
                by_alias=True,
                exclude={'contract_version', 'source_revision', 'snapshot_id', 'content_digest'},
            ),
        }


class DocumentMaterialRefV1(ProfileContract):
    snapshot_id: str = Field(min_length=1, max_length=512)
    contract_version: Literal['document.material.v1'] = 'document.material.v1'
    content_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1, max_length=256)
    source_revision: int = Field(strict=True, ge=1)


class DocumentSourceFenceV1(ProfileContract):
    evidence_ref: str = Field(min_length=1, max_length=512)
    publishers_quiesced: Literal[True]
    workers_quiesced: Literal[True]
    legacy_intents_retired: Literal[True]


class DocumentFreshSourceFenceV1(ProfileContract):
    admission_kind: Literal['fresh']
    evidence_ref: str = Field(min_length=1, max_length=512)
    owner_fence_ref: ProfileSourceSnapshotRefV1
    owner_fence_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    profile_presence_proof_ref: str = Field(min_length=1, max_length=512)
    profile_presence_proof_digest: str = Field(pattern=r'^[0-9a-f]{64}$')


class _DocumentIdentity(ProfileContract):
    scope: ProfileScope
    source_type: Literal['health_report'] = 'health_report'
    source_id: str = Field(min_length=1, max_length=256)
    source_revision: int = Field(strict=True, ge=1)


class DocumentSourceCoverageV1(_DocumentIdentity):
    contract_version: Literal['document.source-coverage.v1'] = 'document.source-coverage.v1'
    legacy_source_revision: int | None = Field(default=None, strict=True, ge=1)
    inventory_snapshot_ref: ProfileSourceSnapshotRefV1
    inventory_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    ordered_contribution_ids: tuple[str, ...] = Field(min_length=1, max_length=1000)
    complete: Literal[True]


class DocumentSourceQualityV1(ProfileContract):
    """Producer acceptance of this complete active set, not raw OCR job status."""

    status: Literal['accepted']
    details: dict[str, Any] = Field(default_factory=dict)


class DocumentSourcePayloadV1(ProfileContract):
    quality: DocumentSourceQualityV1
    material_snapshot_refs: tuple[DocumentMaterialRefV1, ...] = Field(min_length=1, max_length=1000)
    contributions: tuple[DocumentContributionV1, ...] = Field(min_length=1, max_length=1000)
    coverage: DocumentSourceCoverageV1


class DocumentCompleteSourceV1(_DocumentIdentity):
    contract_version: Literal['document.source-observations.v1'] = DOCUMENT_SOURCE_CONTRACT
    payload: DocumentSourcePayloadV1


class DocumentRetiredContributionV1(DocumentContributionRefV1):
    retirement_kind: Literal['superseded', 'retracted', 'corrected']
    retirement_evidence_ref: str = Field(min_length=1, max_length=512)
    superseded_by_contribution_id: str | None = None


class DocumentInventoryPayloadV1(ProfileContract):
    fence: DocumentSourceFenceV1 | DocumentFreshSourceFenceV1
    active_set_evidence_ref: str = Field(min_length=1, max_length=512)
    retired_contributions: tuple[DocumentRetiredContributionV1, ...] = Field(max_length=1000)
    legacy_state: Literal['absent', 'captured']
    legacy_source_revision: int | None = Field(default=None, strict=True, ge=1)
    contributions: tuple[DocumentContributionRefV1, ...] = Field(min_length=1, max_length=1000)


class DocumentSourceInventoryV1(_DocumentIdentity):
    contract_version: Literal['document.source-inventory.v1'] = DOCUMENT_INVENTORY_CONTRACT
    payload: DocumentInventoryPayloadV1


class ProfileDocumentSourceReadinessV1(ProfileContract):
    contract_version: Literal['profile-document-source-readiness.v1'] = 'profile-document-source-readiness.v1'
    scope: ProfileScope
    source_id: str
    source_revision: int
    snapshot_ref: ProfileSourceSnapshotRefV1
    content_digest: str
    processing_revision: Literal['profile-document-source-v1'] = DOCUMENT_PROCESSING_REVISION
    inventory_snapshot_ref: ProfileSourceSnapshotRefV1
    inventory_digest: str
    legacy_quiescence_evidence_ref: str | None = None
    admission_kind: Literal['legacy', 'fresh'] = 'legacy'
    admission_fence: DocumentFreshSourceFenceV1 | None = None
    legacy_source_revision: int | None
    contribution_ids: tuple[str, ...]


class ProfileDocumentSourceReader(Protocol):
    async def read_source_observations(self, *, scope: ProfileScope, snapshot_id: str) -> dict: ...
    async def read_source_inventory(self, *, scope: ProfileScope, snapshot_id: str) -> dict: ...
    async def read_retraction(self, *, scope: ProfileScope, snapshot_id: str) -> dict: ...
