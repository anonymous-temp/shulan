"""Explicit Record formal-v2 consumption; historical consumers stay selectable."""

from dataclasses import asdict

from adapters.record.prepared_documents import PreparedRecordDocumentAdapter
from modules.analysis.contracts import OutputSelection
from modules.analysis.formal_source_contracts import DocumentFormalSourceCommand
from modules.analysis.report_publication_contracts import DocumentPreparedPublicationInput
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import digest
from modules.analysis.understanding import DocumentPreparationReceipt
from modules.record.documents import LEGACY_RECORD_DOCUMENT_CONSUMER
from modules.record.documents import RECORD_DOCUMENT_CHECKPOINT
from modules.record.documents import RECORD_FORMAL_DOCUMENT_CHECKPOINT
from modules.record.documents import RECORD_FORMAL_SOURCE_REVISION
from modules.record.documents import RecordDocumentCheckpointError
from modules.record.documents import RecordDocumentFormalUnavailable
from modules.record.documents import RecordDocumentPublicationPending
from modules.record.documents import RecordDocumentPublicationV1
from modules.record.documents import RecordDocumentVersionError
from modules.record.documents import RecordFormalPreparedDocumentV2


def _command(prepared, request_id):
    return DocumentFormalSourceCommand(
        scope=DocumentScope(prepared.user_id, prepared.subject_id),
        request_id=request_id,
        operation='base',
        inputs=(
            DocumentPreparedPublicationInput(
                prepared.material_snapshot_id,
                prepared.facts_snapshot_id,
                prepared.understanding_snapshot_id,
                OutputSelection(*prepared.selection),
                DocumentPreparationReceipt(
                    prepared.document_id, prepared.preparation_trace_id, prepared.preparation_reused
                ),
            ),
        ),
        command_revision=RECORD_FORMAL_SOURCE_REVISION,
    )


class FormalRecordDocumentPublisher:
    def __init__(self, formal):
        self.formal = formal

    async def publish_prepared(self, *, prepared):
        if not isinstance(prepared, RecordFormalPreparedDocumentV2):
            raise RecordDocumentCheckpointError('formal publication requires its accepted v2 checkpoint')
        fixed = RecordFormalPreparedDocumentV2.from_checkpoint(prepared.to_checkpoint())
        command = _command(fixed, fixed.formal_request_id)
        if command.command_id != fixed.formal_command_id:
            raise RecordDocumentCheckpointError('the fixed formal command identity changed')
        receipt = await self.formal.execute(command)
        if (
            not isinstance(receipt, dict)
            or receipt.get('commandId') != command.command_id
            or receipt.get('commandRevision') != fixed.formal_revision
            or receipt.get('status') not in {'pending', 'completed', 'not_published', 'failed'}
        ):
            raise RecordDocumentCheckpointError('formal receipt does not identify the accepted command')
        if receipt['status'] in {'failed', 'not_published'}:
            raise RecordDocumentFormalUnavailable(
                document_id=fixed.document_id, reason=receipt.get('errorCode') or receipt['status']
            )
        result = receipt.get('result')
        bases = result.get('basePublications') if isinstance(result, dict) else None
        if (
            not isinstance(bases, list)
            or len(bases) > 1
            or type(receipt.get('completedBaseCount')) is not int
            or receipt['completedBaseCount'] != len(bases)
            or (
                receipt.get('totalUniqueSources') is not None
                and (type(receipt['totalUniqueSources']) is not int or receipt['totalUniqueSources'] != 1)
            )
        ):
            raise RecordDocumentCheckpointError('formal base receipt has an unsupported shape')
        if not bases:
            if receipt['status'] != 'pending' or receipt.get('completedBaseCount') != 0:
                raise RecordDocumentCheckpointError('formal receipt claims completion without a base')
            raise RecordDocumentPublicationPending(command.command_id)
        base = bases[0]
        if (
            not isinstance(base, dict)
            or base.get('sourceId') != fixed.source_id
            or receipt.get('completedBaseCount') != 1
            or receipt.get('totalUniqueSources') != 1
        ):
            raise RecordDocumentCheckpointError('formal base receipt identifies another fixed source')
        if base.get('kind') == 'not_published':
            raise RecordDocumentFormalUnavailable(document_id=fixed.document_id, reason=base.get('reason'))
        revision = base.get('legacySourceRevision')
        if (
            base.get('kind') not in {'legacy', 'archive'}
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or not 1 <= revision <= 2**53 - 1
        ):
            raise RecordDocumentCheckpointError('formal publication has no actual legacy archive revision')
        if base['kind'] == 'archive' and (
            base.get('ingestionStatus') != 'source_managed' or base.get('jobId') is not None
        ):
            raise RecordDocumentCheckpointError('formal archive receipt cannot identify a legacy ingestion job')
        return RecordDocumentPublicationV1(
            canonical_report_id=base.get('canonicalReportId'),
            legacy_source_revision=int(revision),
            ingestion_status=base.get('ingestionStatus'),
            ingestion_job_id=base.get('jobId'),
        )


class FormalRecordDocumentAdapter(PreparedRecordDocumentAdapter):
    prepared_checkpoint_version = RECORD_FORMAL_DOCUMENT_CHECKPOINT
    prepared_checkpoint_type = RecordFormalPreparedDocumentV2

    def __init__(self, *, compatibility, formal, previous_port):
        super().__init__(
            understanding=compatibility,
            publisher=FormalRecordDocumentPublisher(formal),
            ensure_self_scope=compatibility.ensure_self_scope,
        )
        self.previous_port = previous_port

    def for_consumer_revision(self, revision):
        if revision == RECORD_FORMAL_DOCUMENT_CHECKPOINT:
            return self
        if revision not in {RECORD_DOCUMENT_CHECKPOINT, LEGACY_RECORD_DOCUMENT_CONSUMER}:
            raise RecordDocumentVersionError('the accepted Record document consumer is unavailable')
        select = getattr(self.previous_port, 'for_consumer_revision', None)
        if callable(select):
            return select(revision)
        actual = getattr(self.previous_port, 'prepared_checkpoint_version', LEGACY_RECORD_DOCUMENT_CONSUMER)
        if self.previous_port is None or actual != revision:
            raise RecordDocumentVersionError('the accepted historical Record document consumer is unavailable')
        return self.previous_port

    @staticmethod
    def _freeze(bundle, **kwargs):
        prepared = PreparedRecordDocumentAdapter._freeze(bundle, **kwargs)
        request_id = 'record-document-' + digest(prepared.to_checkpoint())
        command = _command(prepared, request_id)
        return RecordFormalPreparedDocumentV2(
            **asdict(prepared), formal_request_id=request_id, formal_command_id=command.command_id
        )


def create_formal_record_document_port(compatibility_service, formal_service, *, previous_port):
    """Pure explicit assembly; it neither accepts tasks nor installs the default consumer."""
    return FormalRecordDocumentAdapter(
        compatibility=compatibility_service, formal=formal_service, previous_port=previous_port
    )
