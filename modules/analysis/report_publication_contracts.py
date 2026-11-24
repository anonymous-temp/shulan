"""Frozen commands for the explicitly selected historical Report source publisher.

The command identity is separate from the old output cache/publication identity.
Different preparation traces may reuse one legacy source without changing its key.
"""

from dataclasses import asdict
from dataclasses import dataclass
from typing import Protocol

from modules.analysis.compatibility import DocumentLegacyPublicationReceipt
from modules.analysis.contracts import OutputSelection
from modules.analysis.contracts import UnsupportedOutputVersionError
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import digest
from modules.analysis.understanding import UNDERSTANDING_SELECTION
from modules.analysis.understanding import DocumentPreparationReceipt

LEGACY_REPORT_SOURCE_SELECTION = OutputSelection('report_source', '1', 'report-source-v16')
REPORT_PUBLICATION_COMMAND_REVISION = 'document.legacy-report-publication.v1'


def _reference(value):
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError('an exact nonempty persisted reference is required')


@dataclass(frozen=True, slots=True)
class DocumentPreparedPublicationInput:
    material_snapshot_ref: str
    facts_snapshot_ref: str
    understanding_snapshot_ref: str
    selection: OutputSelection
    preparation_receipt: DocumentPreparationReceipt

    def __post_init__(self):
        for value in (self.material_snapshot_ref, self.facts_snapshot_ref, self.understanding_snapshot_ref):
            _reference(value)
        if self.selection != UNDERSTANDING_SELECTION:
            raise UnsupportedOutputVersionError('the fixed understanding input implementation is unavailable')
        if not isinstance(self.preparation_receipt, DocumentPreparationReceipt):
            raise TypeError('the original preparation receipt is required')

    def as_dict(self):
        return {
            'materialSnapshotRef': self.material_snapshot_ref,
            'factsSnapshotRef': self.facts_snapshot_ref,
            'understandingSnapshotRef': self.understanding_snapshot_ref,
            'selection': asdict(self.selection),
            'preparationReceipt': asdict(self.preparation_receipt),
        }

    @classmethod
    def from_dict(cls, value):
        if set(value) != {
            'materialSnapshotRef',
            'factsSnapshotRef',
            'understandingSnapshotRef',
            'selection',
            'preparationReceipt',
        }:
            raise ValueError('the complete fixed publication input is required')
        return cls(
            material_snapshot_ref=value['materialSnapshotRef'],
            facts_snapshot_ref=value['factsSnapshotRef'],
            understanding_snapshot_ref=value['understandingSnapshotRef'],
            selection=OutputSelection(**value['selection']),
            preparation_receipt=DocumentPreparationReceipt(**value['preparationReceipt']),
        )


@dataclass(frozen=True, slots=True)
class DocumentReportPublicationManifest:
    scope: DocumentScope
    inputs: tuple[DocumentPreparedPublicationInput, ...]
    selection: OutputSelection
    command_revision: str = REPORT_PUBLICATION_COMMAND_REVISION

    def __post_init__(self):
        if not isinstance(self.scope, DocumentScope):
            raise TypeError('an explicit DocumentScope is required')
        if (
            not isinstance(self.inputs, tuple)
            or not self.inputs
            or not all(isinstance(item, DocumentPreparedPublicationInput) for item in self.inputs)
        ):
            raise TypeError('an ordered nonempty tuple of fixed inputs is required')
        if (
            self.selection != LEGACY_REPORT_SOURCE_SELECTION
            or self.command_revision != REPORT_PUBLICATION_COMMAND_REVISION
        ):
            raise UnsupportedOutputVersionError('the exact legacy Report publication implementation is unavailable')

    def as_dict(self):
        return {
            'scope': self.scope.as_dict(),
            'inputs': [item.as_dict() for item in self.inputs],
            'selection': asdict(self.selection),
            'commandRevision': self.command_revision,
        }

    @property
    def command_id(self):
        return 'document_report_publication_' + digest(self.as_dict())

    @classmethod
    def from_dict(cls, value):
        if set(value) != {'scope', 'inputs', 'selection', 'commandRevision'}:
            # A missing persisted selection must never acquire today's defaults.
            raise ValueError('a complete persisted publication manifest is required')
        scope = value['scope']
        if set(scope) != {'userId', 'subjectId'}:
            raise ValueError('a complete persisted publication scope is required')
        return cls(
            scope=DocumentScope(scope['userId'], scope['subjectId']),
            inputs=tuple(DocumentPreparedPublicationInput.from_dict(item) for item in value['inputs']),
            selection=OutputSelection(**value['selection']),
            command_revision=value['commandRevision'],
        )


@dataclass(frozen=True, slots=True)
class DocumentReportPublicationReceipt:
    manifest: DocumentReportPublicationManifest
    projection_ref: str
    base_publications: tuple[DocumentLegacyPublicationReceipt, ...]
    legacy_source_revision: int | None
    ingestion_status: str
    ingestion_job_id: str | None
    processing_status: str

    def __post_init__(self):
        _reference(self.projection_ref)
        if not isinstance(self.manifest, DocumentReportPublicationManifest):
            raise TypeError('the executed command manifest is required')
        if (
            not isinstance(self.base_publications, tuple)
            or len(self.base_publications) != len(self.manifest.inputs)
            or any(
                not isinstance(receipt, DocumentLegacyPublicationReceipt)
                or receipt.preparation_receipt != item.preparation_receipt
                for item, receipt in zip(self.manifest.inputs, self.base_publications)
            )
        ):
            raise ValueError('base receipts must identify the ordered original preparation inputs')
        if self.processing_status not in {'complete', 'partial', 'unavailable'}:
            raise ValueError('the actual processing status is required')
        if self.processing_status != 'complete':
            if (
                self.legacy_source_revision is not None
                or self.ingestion_status != 'not_submitted'
                or self.ingestion_job_id is not None
            ):
                raise ValueError('incomplete Report outputs must not claim an extra formal source publication')
        elif (
            not isinstance(self.legacy_source_revision, int)
            or isinstance(self.legacy_source_revision, bool)
            or not 1 <= self.legacy_source_revision <= 2**53 - 1
        ):
            raise ValueError('a complete publication requires its actual JS-safe legacy revision')
        else:
            object.__setattr__(self, 'legacy_source_revision', int(self.legacy_source_revision))

    @property
    def receipt_ref(self):
        return self.manifest.command_id

    @property
    def canonical_report_id(self):
        return self.base_publications[0].canonical_report_id

    def as_dict(self):
        return {
            'manifest': self.manifest.as_dict(),
            'projectionRef': self.projection_ref,
            'basePublications': [asdict(item) for item in self.base_publications],
            'legacySourceRevision': self.legacy_source_revision,
            'ingestionStatus': self.ingestion_status,
            'ingestionJobId': self.ingestion_job_id,
            'processingStatus': self.processing_status,
        }

    @classmethod
    def from_dict(cls, value):
        if set(value) != {
            'manifest',
            'projectionRef',
            'basePublications',
            'legacySourceRevision',
            'ingestionStatus',
            'ingestionJobId',
            'processingStatus',
        }:
            raise ValueError('the complete persisted publication receipt is required')
        bases = []
        for original in value['basePublications']:
            item = dict(original)
            item['preparation_receipt'] = DocumentPreparationReceipt(**item['preparation_receipt'])
            revision = item['legacy_source_revision']
            if not isinstance(revision, int) or isinstance(revision, bool) or not 1 <= revision <= 2**53 - 1:
                raise ValueError('invalid persisted base publication revision')
            item['legacy_source_revision'] = int(revision)
            bases.append(DocumentLegacyPublicationReceipt(**item))
        return cls(
            manifest=DocumentReportPublicationManifest.from_dict(value['manifest']),
            projection_ref=value['projectionRef'],
            base_publications=tuple(bases),
            legacy_source_revision=value['legacySourceRevision'],
            ingestion_status=value['ingestionStatus'],
            ingestion_job_id=value['ingestionJobId'],
            processing_status=value['processingStatus'],
        )


class DocumentReportPublicationPort(Protocol):
    async def publish_report_source(
        self, *, scope: DocumentScope, inputs: tuple[DocumentPreparedPublicationInput, ...], selection: OutputSelection
    ) -> DocumentReportPublicationReceipt: ...

    async def read_publication(self, *, scope: DocumentScope, receipt_ref: str) -> DocumentReportPublicationReceipt: ...

    async def read_projection(
        self, *, scope: DocumentScope, projection_ref: str, selection: OutputSelection
    ) -> DocumentSnapshot: ...
