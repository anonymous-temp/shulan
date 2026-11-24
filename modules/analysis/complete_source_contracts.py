"""Document-owned handles and admission-plan Port, independent of Profile schemas."""

import json
import re
from dataclasses import dataclass
from typing import Protocol

from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import canonical_json


class DocumentCompleteSourceUnavailable(RuntimeError):
    code = 'document.complete_source_unavailable'


def exact_text(value, label):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f'{label} requires an exact nonblank identity')


@dataclass(frozen=True, slots=True)
class DocumentContributionHandle:
    snapshot_ref: str
    proof_ref: str

    def __post_init__(self):
        exact_text(self.snapshot_ref, 'contribution')
        exact_text(self.proof_ref, 'capture proof')


@dataclass(frozen=True, slots=True)
class DocumentRetiredContribution:
    handle: DocumentContributionHandle
    retirement_kind: str
    retirement_evidence_ref: str
    superseded_by_contribution_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.handle, DocumentContributionHandle):
            raise TypeError('a fixed retired contribution handle is required')
        if self.retirement_kind not in {'superseded', 'retracted', 'corrected'}:
            raise ValueError('unsupported retirement kind')
        exact_text(self.retirement_evidence_ref, 'retirement evidence')
        if self.superseded_by_contribution_id is not None:
            exact_text(self.superseded_by_contribution_id, 'replacement contribution')


@dataclass(frozen=True, slots=True)
class DocumentSourceCapturePlan:
    """Returned only by a trusted durable admission provider, not an HTTP body.

    The provider must verify actual source fences and current effective coverage.
    A typed plan is not itself proof; read_verified_plan owns that verification.
    """

    scope: DocumentScope
    source_id: str
    source_revision: int
    active: tuple[DocumentContributionHandle, ...]
    retired: tuple[DocumentRetiredContribution, ...]
    legacy_state: str
    legacy_source_revision: int | None
    fence_evidence_ref: str
    active_set_evidence_ref: str
    _fence_json: str

    def __post_init__(self):
        if not isinstance(self.scope, DocumentScope):
            raise TypeError('explicit Document scope is required')
        exact_text(self.source_id, 'canonical source')
        if type(self.source_revision) is not int or self.source_revision < 1:
            raise ValueError('material revision must be a positive integer')
        if not isinstance(self.active, tuple) or not 1 <= len(self.active) <= 1000:
            raise ValueError('complete active contribution set must be bounded and nonempty')
        if any(not isinstance(item, DocumentContributionHandle) for item in self.active):
            raise TypeError('fixed active handles are required')
        if not isinstance(self.retired, tuple) or len(self.retired) > 1000:
            raise ValueError('retired contribution audit must be bounded')
        if any(not isinstance(item, DocumentRetiredContribution) for item in self.retired):
            raise TypeError('fixed retired audit entries are required')
        if self.legacy_state not in {'absent', 'captured'}:
            raise ValueError('unknown legacy inventory coverage')
        if self.legacy_source_revision is not None and (
            type(self.legacy_source_revision) is not int or not 1 <= self.legacy_source_revision <= 2**53 - 1
        ):
            raise ValueError('legacy revision must be its actual JS-safe opaque integer')
        if (self.legacy_state == 'captured') != (self.legacy_source_revision is not None):
            raise ValueError('legacy inventory identity is inconsistent')
        exact_text(self.fence_evidence_ref, 'verified fence')
        exact_text(self.active_set_evidence_ref, 'verified effective set')
        fence = json.loads(self._fence_json)
        if not isinstance(fence, dict) or fence.get('evidenceRef') != self.fence_evidence_ref:
            raise ValueError('the verified fence reference differs from the persisted plan')
        if fence.get('admissionKind') == 'fresh':
            if set(fence) != {
                'admissionKind',
                'evidenceRef',
                'ownerFenceRef',
                'ownerFenceDigest',
                'profilePresenceProofRef',
                'profilePresenceProofDigest',
            }:
                raise ValueError('the exact fresh admission proof is required')
            owner = fence['ownerFenceRef']
            if not isinstance(owner, dict) or set(owner) != {'provider', 'contractVersion', 'snapshotId'}:
                raise ValueError('the exact owner fence reference is required')
            if owner['provider'] != 'document' or owner['contractVersion'] != 'profile-owner-admission-fence.v1':
                raise ValueError('unsupported fresh owner fence contract')
            exact_text(owner['snapshotId'], 'owner fence snapshot')
            for key in ('ownerFenceDigest', 'profilePresenceProofDigest'):
                if not isinstance(fence[key], str) or re.fullmatch('[0-9a-f]{64}', fence[key]) is None:
                    raise ValueError('fresh admission proof digest is invalid')
            if fence['profilePresenceProofRef'] != 'profile_presence_' + fence['profilePresenceProofDigest']:
                raise ValueError('fresh presence proof reference does not match its digest')
            if self.legacy_state != 'absent' or self.retired:
                raise DocumentCompleteSourceUnavailable('fresh admission cannot describe historical contributions')
        elif set(fence) != {'evidenceRef', 'publishersQuiesced', 'workersQuiesced', 'legacyIntentsRetired'} or not all(
            fence[key] is True for key in ('publishersQuiesced', 'workersQuiesced', 'legacyIntentsRetired')
        ):
            raise DocumentCompleteSourceUnavailable('actual producer/worker drain and intent retirement are required')
        object.__setattr__(self, '_fence_json', canonical_json(fence))

    @property
    def fence(self):
        return json.loads(self._fence_json)


class DocumentSourceAdmissionReader(Protocol):
    async def read_verified_plan(self, *, scope: DocumentScope, plan_ref: str) -> DocumentSourceCapturePlan:
        """Purely validate a previously durable proof; no latest scan or new fencing."""
        ...


@dataclass(frozen=True, slots=True)
class DocumentCompleteSourceReceipt:
    source_snapshot_ref: str
    source_content_digest: str
    inventory_snapshot_ref: str
    inventory_content_digest: str
    plan_ref: str
