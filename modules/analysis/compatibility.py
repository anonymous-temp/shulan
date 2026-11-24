"""Explicit, replayable commands for the historical Document publication sink.

This facade preserves the old source identity during consumer migration. It does
not turn the derived understanding contract into the new Facts publication API.
"""

from dataclasses import asdict
from dataclasses import dataclass
from typing import Protocol

from modules.analysis.contracts import OutputSelection
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import digest
from modules.analysis.understanding import DocumentPreparationReceipt
from modules.analysis.understanding import DocumentUnderstandingUnavailableError


class DocumentPreparationConflictError(RuntimeError):
    code = 'document.preparation_conflict'
    retryable = False


def _legacy_revision(value):
    # BSON Int64 is an int subclass. Keep the existing JS-safe opaque revision
    # contract while rejecting bools/floats rather than silently coercing them.
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 2**53 - 1:
        raise DocumentPreparationConflictError('legacy publisher returned an invalid source revision')
    return int(value)


@dataclass(frozen=True, slots=True)
class DocumentLegacyPublicationReceipt:
    """A fixed acknowledgement, not a live Profile ingestion status query."""

    preparation_receipt: DocumentPreparationReceipt
    canonical_report_id: str
    legacy_source_revision: int
    ingestion_status: str
    ingestion_job_id: str | None


class DocumentCompatibilityStore(Protocol):
    async def put_preparation(self, scope: DocumentScope, trace_id: str, binding: dict) -> None: ...
    async def get_preparation(self, scope: DocumentScope, trace_id: str) -> dict: ...
    async def get_publication(self, scope: DocumentScope, operation_id: str) -> dict | None: ...
    async def put_publication(self, scope: DocumentScope, operation_id: str, receipt: dict) -> dict: ...


class DocumentCompatibilityService:
    def __init__(self, *, understanding, publications: DocumentCompatibilityStore, legacy_publication, ensure_self):
        self.understanding = understanding
        self.publications = publications
        self.legacy_publication = legacy_publication
        self.ensure_self = ensure_self

    async def ensure_self_scope(self, user_id: str) -> DocumentScope:
        """An explicit first-use command; restore/read never calls this method."""
        if not isinstance(user_id, str) or not user_id or user_id.strip() != user_id:
            raise ValueError('an explicit user identity is required')
        subject_id = await self.ensure_self(user_id)
        return DocumentScope(user_id, subject_id)

    @staticmethod
    def _document(bundle):
        metadata = bundle.material.payload['documentMetadata']
        content = bundle.understanding.payload
        if (
            not isinstance(metadata.get('sourceFingerprint'), str)
            or not metadata['sourceFingerprint']
            or 'legacyPublicationVersion' not in metadata
        ):
            raise DocumentUnderstandingUnavailableError('the fixed legacy publication identity is unavailable')
        version = metadata['legacyPublicationVersion']
        if (
            metadata['canonicalReportId'] != bundle.material.source_id
            or metadata != content.get('documentMetadata')
            or (
                version != content['interpretationRevision']
                and (version, content['interpretationRevision']) != ('legacy', None)
            )
        ):
            raise DocumentPreparationConflictError('legacy publication identity differs from the fixed interpretation')
        return {
            '_id': metadata['documentId'],
            **bundle.material.scope.as_dict(),
            'canonicalReportId': metadata['canonicalReportId'],
            'sourceFingerprint': metadata['sourceFingerprint'],
            'interpretationVersion': version,
        }

    @classmethod
    def _binding(cls, bundle, selection, receipt):
        if (
            not isinstance(receipt, DocumentPreparationReceipt)
            or receipt.document_id != bundle.material.payload['documentId']
        ):
            raise DocumentPreparationConflictError('preparation receipt does not identify the fixed document')
        if receipt.reused != (receipt.document_id != receipt.trace_id):
            raise DocumentPreparationConflictError('preparation receipt has inconsistent reuse identity')
        return {
            'selection': asdict(selection),
            'materialSnapshotRef': bundle.material.snapshot_id,
            'factsSnapshotRef': bundle.facts.snapshot_id,
            'understandingSnapshotRef': bundle.understanding.snapshot_id,
            'preparationReceipt': asdict(receipt),
            'publicationDocument': cls._document(bundle),
            'interpretationContentDigest': bundle.understanding.payload['interpretationContentDigest'],
        }

    async def prepare_bundle(self, *, scope: DocumentScope, selection: OutputSelection, **kwargs):
        bundle = await self.understanding.prepare_bundle(scope=scope, selection=selection, **kwargs)
        binding = self._binding(bundle, selection, bundle.receipt)
        await self.publications.put_preparation(scope, bundle.receipt.trace_id, binding)
        return bundle

    async def read_bundle(self, *, scope, understanding_snapshot_id, selection):
        return await self.understanding.read_bundle(
            scope=scope, understanding_snapshot_id=understanding_snapshot_id, selection=selection
        )

    async def validate_prepared(self, *, scope, understanding_snapshot_id, selection, preparation_receipt):
        """Pure validation of the original durable preparation, without publication."""
        bundle = await self.read_bundle(
            scope=scope, understanding_snapshot_id=understanding_snapshot_id, selection=selection
        )
        binding = self._binding(bundle, selection, preparation_receipt)
        persisted = await self.publications.get_preparation(scope, preparation_receipt.trace_id)
        if persisted != binding:
            raise DocumentPreparationConflictError('publication requires the original durable preparation binding')
        return bundle

    async def publish_prepared(self, *, scope, understanding_snapshot_id, selection, preparation_receipt):
        bundle = await self.validate_prepared(
            scope=scope,
            understanding_snapshot_id=understanding_snapshot_id,
            selection=selection,
            preparation_receipt=preparation_receipt,
        )
        binding = self._binding(bundle, selection, preparation_receipt)
        operation_id = digest([scope.as_dict(), understanding_snapshot_id])
        receipt = await self.publications.get_publication(scope, operation_id)
        if receipt is None:
            saved = await self.legacy_publication.publish_document(
                binding['publicationDocument'], bundle.understanding.payload['interpretation']
            )
            record = saved.record
            if (
                record.user_id != scope.user_id
                or record.subject_id != scope.subject_id
                or record.report_id != bundle.material.source_id
                or record.canonical_report_id != bundle.material.source_id
                or record.document_id != preparation_receipt.document_id
                or record.source_fingerprint != binding['publicationDocument']['sourceFingerprint']
            ):
                raise DocumentPreparationConflictError('legacy publisher returned a different source identity')
            receipt = await self.publications.put_publication(
                scope,
                operation_id,
                {
                    'understandingSnapshotRef': understanding_snapshot_id,
                    'canonical_report_id': record.canonical_report_id,
                    'legacy_source_revision': _legacy_revision(record.source_revision),
                    'ingestion_status': record.ingestion_status,
                    'ingestion_job_id': record.ingestion_job_id,
                },
            )
        if receipt['understandingSnapshotRef'] != understanding_snapshot_id:
            raise DocumentPreparationConflictError('publication acknowledgement identifies a different snapshot')
        return DocumentLegacyPublicationReceipt(
            preparation_receipt=preparation_receipt,
            canonical_report_id=receipt['canonical_report_id'],
            legacy_source_revision=_legacy_revision(receipt['legacy_source_revision']),
            ingestion_status=receipt['ingestion_status'],
            ingestion_job_id=receipt['ingestion_job_id'],
        )
