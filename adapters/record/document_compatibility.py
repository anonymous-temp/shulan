"""Record adaptation of Document's public, durable compatibility commands."""

from adapters.record.prepared_documents import PreparedRecordDocumentAdapter
from modules.analysis.compatibility import DocumentCompatibilityService
from modules.analysis.compatibility import DocumentLegacyPublicationReceipt
from modules.analysis.compatibility import DocumentPreparationConflictError
from modules.analysis.contracts import OutputSelection
from modules.analysis.snapshots import DocumentScope
from modules.analysis.understanding import DocumentPreparationReceipt
from modules.record.documents import RecordDocumentCheckpointError
from modules.record.documents import RecordDocumentPublicationV1


class _DocumentCompatibilityGateway:
    def __init__(self, commands: DocumentCompatibilityService):
        self.commands = commands

    async def ensure_self_scope(self, user_id):
        return await self.commands.ensure_self_scope(user_id)

    async def prepare_bundle(self, **kwargs):
        try:
            return await self.commands.prepare_bundle(**kwargs)
        except DocumentPreparationConflictError as error:
            raise RecordDocumentCheckpointError('Document preparation binding is inconsistent') from error

    async def read_bundle(self, **kwargs):
        try:
            return await self.commands.read_bundle(**kwargs)
        except DocumentPreparationConflictError as error:
            raise RecordDocumentCheckpointError('Document snapshot binding is inconsistent') from error

    async def publish_prepared(self, *, prepared):
        original = DocumentPreparationReceipt(
            document_id=prepared.document_id,
            trace_id=prepared.preparation_trace_id,
            reused=prepared.preparation_reused,
        )
        try:
            receipt = await self.commands.publish_prepared(
                scope=DocumentScope(prepared.user_id, prepared.subject_id),
                understanding_snapshot_id=prepared.understanding_snapshot_id,
                selection=OutputSelection(*prepared.selection),
                preparation_receipt=original,
            )
        except DocumentPreparationConflictError as error:
            raise RecordDocumentCheckpointError('Document publication binding is inconsistent') from error
        if not isinstance(receipt, DocumentLegacyPublicationReceipt) or receipt.preparation_receipt != original:
            raise RecordDocumentCheckpointError('Document publisher returned a different preparation receipt')
        return RecordDocumentPublicationV1(
            canonical_report_id=receipt.canonical_report_id,
            legacy_source_revision=receipt.legacy_source_revision,
            ingestion_status=receipt.ingestion_status,
            ingestion_job_id=receipt.ingestion_job_id,
        )


def create_prepared_record_document_port(compatibility_service: DocumentCompatibilityService, *, legacy_port=None):
    """Compose without I/O; the application owns Document resources and retention.

    The Record creation store must persist on_prepared before publication. Recovery
    uses the same public service with the original fixed references and receipt.
    """
    gateway = _DocumentCompatibilityGateway(compatibility_service)
    return PreparedRecordDocumentAdapter(
        understanding=gateway,
        publisher=gateway,
        ensure_self_scope=gateway.ensure_self_scope,
        legacy_port=legacy_port,
    )
