"""Explicit managed-source commands; legacy publishers retain their own contracts."""

from dataclasses import asdict
from dataclasses import dataclass
from typing import Protocol

from modules.analysis.complete_source_contracts import DocumentRetiredContribution
from modules.analysis.complete_source_contracts import DocumentSourceCapturePlan
from modules.analysis.complete_source_contracts import exact_text
from modules.analysis.contracts import UnsupportedOutputVersionError
from modules.analysis.report_publication_contracts import DocumentPreparedPublicationInput
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import digest

MANAGED_SOURCE_REVISION = 'document-managed-source-v1'
MANAGED_REPORT_REVISION = 'document-managed-report-v16-v1'
MAX_MANAGED_TRANSITIONS = 100


@dataclass(frozen=True, slots=True)
class DocumentManagedSourceCommand:
    scope: DocumentScope
    source_id: str
    request_id: str
    prior_publication_revision: int
    prior_source_snapshot_ref: str
    operation: str
    inputs: tuple[DocumentPreparedPublicationInput, ...]
    command_revision: str

    def __post_init__(self):
        if not isinstance(self.scope, DocumentScope):
            raise TypeError('an explicit Document scope is required')
        for value in (self.source_id, self.request_id, self.prior_source_snapshot_ref):
            exact_text(value, 'managed command identity')
        if type(self.prior_publication_revision) is not int or self.prior_publication_revision < 1:
            raise ValueError('an exact prior publication revision is required')
        if self.command_revision != MANAGED_SOURCE_REVISION:
            raise UnsupportedOutputVersionError('the managed source command implementation is unavailable')
        if self.operation not in {'replace_base', 'replace_report', 'retire_report'}:
            raise ValueError('unsupported managed source operation')
        if not isinstance(self.inputs, tuple) or not 1 <= len(self.inputs) <= 100:
            raise ValueError('one to one hundred ordered fixed inputs are required')
        if any(not isinstance(item, DocumentPreparedPublicationInput) for item in self.inputs):
            raise TypeError('fixed Document preparation inputs are required')
        if self.operation == 'replace_base' and len(self.inputs) != 1:
            raise ValueError('a base correction selects one fixed document')

    @property
    def command_id(self):
        return 'document_managed_command_' + digest([self.scope.as_dict(), self.source_id, self.request_id])

    def as_dict(self):
        return {
            'scope': self.scope.as_dict(),
            'sourceId': self.source_id,
            'requestId': self.request_id,
            'priorPublicationRevision': self.prior_publication_revision,
            'priorSourceSnapshotRef': self.prior_source_snapshot_ref,
            'operation': self.operation,
            'inputs': [item.as_dict() for item in self.inputs],
            'commandRevision': self.command_revision,
        }

    @classmethod
    def from_dict(cls, raw):
        expected = {
            'scope',
            'sourceId',
            'requestId',
            'priorPublicationRevision',
            'priorSourceSnapshotRef',
            'operation',
            'inputs',
            'commandRevision',
        }
        if set(raw) != expected or set(raw['scope']) != {'userId', 'subjectId'}:
            raise ValueError('the complete persisted managed command is required')
        return cls(
            DocumentScope(raw['scope']['userId'], raw['scope']['subjectId']),
            raw['sourceId'],
            raw['requestId'],
            raw['priorPublicationRevision'],
            raw['priorSourceSnapshotRef'],
            raw['operation'],
            tuple(DocumentPreparedPublicationInput.from_dict(item) for item in raw['inputs']),
            raw['commandRevision'],
        )


@dataclass(frozen=True, slots=True)
class DocumentManagedSourcePlan(DocumentSourceCapturePlan):
    """A later inventory carries original admission proof, never a new absence claim."""

    transition_ref: str

    def __post_init__(self):
        # Validate the unchanged structural/fence contract using the initial
        # shape, then validate managed history separately. The original plan
        # type remains strict for fresh initial admission.
        original = asdict(self)
        original.pop('transition_ref')
        original['scope'] = self.scope
        original['active'] = self.active
        original['retired'] = ()
        if self.fence.get('admissionKind') == 'fresh':
            original['legacy_state'], original['legacy_source_revision'] = 'absent', None
        DocumentSourceCapturePlan(**original)
        exact_text(self.transition_ref, 'fixed managed transition')
        if self.transition_ref != self.active_set_evidence_ref:
            raise ValueError('the transition must be the current active-set proof')
        if (
            not isinstance(self.retired, tuple)
            or len(self.retired) > 1000
            or any(not isinstance(item, DocumentRetiredContribution) for item in self.retired)
        ):
            raise ValueError('retired contribution audit exceeds the supported bound')
        if (self.legacy_state == 'captured') != (self.legacy_source_revision is not None):
            raise ValueError('managed legacy archive identity is inconsistent')
        if self.legacy_state not in {'absent', 'captured'}:
            raise ValueError('unknown managed legacy archive state')
        if self.legacy_source_revision is not None and (
            type(self.legacy_source_revision) is not int or not 1 <= self.legacy_source_revision <= 2**53 - 1
        ):
            raise ValueError('the actual archived opaque revision is required')


class DocumentManagedSourcePort(Protocol):
    async def execute(self, command: DocumentManagedSourceCommand) -> dict:
        """Persist and publish the selected transition; acknowledgement is not application."""
        ...

    async def read_receipt(self, *, scope: DocumentScope, command_id: str) -> dict:
        """Purely read the original command result, even after later correction/withdrawal."""
        ...
