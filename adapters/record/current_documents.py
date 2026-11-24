"""Record-owned consumption of the current fixed Document preparation and publication."""

from collections.abc import Awaitable
from collections.abc import Callable
from typing import Protocol

from adapters.record.prepared_documents import PreparedRecordDocumentAdapter
from modules.analysis.compatibility import DocumentPreparationConflictError
from modules.analysis.contracts import OutputSelection
from modules.analysis.current_understanding import DocumentCurrentBundle
from modules.analysis.current_understanding import DocumentPreparedInput
from modules.analysis.publication import PublicationReceipt
from modules.analysis.snapshots import DocumentScope
from modules.analysis.understanding import DocumentPreparationReceipt
from modules.analysis.understanding import DocumentSnapshotChangedError
from modules.health_profile.application import ProfileBootstrapResult
from modules.record.documents import RECORD_CURRENT_DOCUMENT_CHECKPOINT
from modules.record.documents import RecordCurrentDocumentConflict
from modules.record.documents import RecordCurrentDocumentPublicationV1
from modules.record.documents import RecordCurrentPreparedDocumentV1
from modules.record.documents import RecordDocumentCheckpointError
from modules.record.documents import RecordDocumentVersionError

_SELECTION_FIELDS = ('profile_name', 'contract_version', 'implementation_revision')


class RecordCurrentDocumentPreparationPort(Protocol):
    async def prepare_bundle(self, *, scope: DocumentScope, **kwargs) -> DocumentCurrentBundle: ...

    async def validate_prepared(
        self, *, scope: DocumentScope, prepared: DocumentPreparedInput
    ) -> DocumentCurrentBundle: ...


class RecordCurrentDocumentPublisherPort(Protocol):
    async def consume_prepared(
        self, *, scope: DocumentScope, prepared: DocumentPreparedInput, lifecycle_epoch: int, request_id: str
    ) -> dict: ...


def _scope(prepared):
    return DocumentScope(prepared.user_id, prepared.subject_id)


def _fixed_input(prepared):
    return DocumentPreparedInput(
        prepared.material_snapshot_id,
        prepared.facts_snapshot_id,
        prepared.understanding_snapshot_id,
        OutputSelection(*prepared.selection),
        DocumentPreparationReceipt(prepared.document_id, prepared.preparation_trace_id, prepared.preparation_reused),
    )


class _RecordCurrentDocumentPublisher:
    def __init__(self, documents: RecordCurrentDocumentPublisherPort):
        self.documents = documents

    async def publish_prepared(self, *, prepared):
        if not isinstance(prepared, RecordCurrentPreparedDocumentV1):
            raise RecordDocumentCheckpointError('current publication requires its exact prepared checkpoint')
        result = await self.documents.consume_prepared(
            scope=_scope(prepared),
            prepared=_fixed_input(prepared),
            lifecycle_epoch=prepared.lifecycle_epoch,
            request_id=prepared.request_id,
        )
        if not isinstance(result, dict) or set(result) != {
            'status',
            'sourceId',
            'sourceRevision',
            'sourceSnapshotRef',
            'publicationRevision',
            'publication',
        }:
            raise RecordDocumentCheckpointError('publication response does not identify the fixed current command')
        source_revision = result['sourceRevision']
        if result['sourceId'] != prepared.source_id or (
            source_revision is not None and source_revision != prepared.source_revision
        ):
            raise RecordCurrentDocumentConflict('publication response identifies another fixed source')
        publication = result['publication']
        receipt_ref = None
        if publication is not None:
            try:
                receipt = PublicationReceipt(**publication)
            except (TypeError, ValueError) as error:
                raise RecordDocumentCheckpointError('an actual Document publication receipt is required') from error
            if (
                result['status'] == 'no_contribution'
                or receipt.source_id != prepared.source_id
                or receipt.publication_revision != result['publicationRevision']
            ):
                raise RecordCurrentDocumentConflict('publication response identifies different fixed evidence')
            receipt_ref = receipt.receipt_ref
        return RecordCurrentDocumentPublicationV1(
            prepared.source_id,
            result['status'],
            source_revision,
            result['sourceSnapshotRef'],
            result['publicationRevision'],
            receipt_ref,
        )


class CurrentRecordDocumentAdapter(PreparedRecordDocumentAdapter):
    prepared_checkpoint_version = RECORD_CURRENT_DOCUMENT_CHECKPOINT
    prepared_checkpoint_type = RecordCurrentPreparedDocumentV1
    preparation_selection = {}

    def __init__(self, *, prepared, documents, ensure_self_profile, previous_port):
        super().__init__(
            understanding=prepared,
            publisher=_RecordCurrentDocumentPublisher(documents),
            ensure_self_scope=ensure_self_profile,
        )
        self.previous_port = previous_port

    def for_consumer_revision(self, revision):
        if revision == RECORD_CURRENT_DOCUMENT_CHECKPOINT:
            return self
        select = getattr(self.previous_port, 'for_consumer_revision', None)
        if callable(select):
            return select(revision)
        raise RecordDocumentVersionError('the accepted Record document consumer is unavailable')

    async def _resolve_scope(self, user_id):
        resolved = await self.ensure_self_scope(user_id)
        subject = resolved.subject if isinstance(resolved, ProfileBootstrapResult) else None
        if subject is None or subject.user_id != user_id or subject.kind != 'self' or subject.status != 'active':
            raise RecordDocumentCheckpointError('current preparation requires an active self subject')
        return DocumentScope(user_id, subject.subject_id), {'lifecycle_epoch': subject.lifecycle_epoch}

    @classmethod
    def _freeze(cls, bundle, **kwargs):
        if not isinstance(bundle, DocumentCurrentBundle):
            raise RecordDocumentCheckpointError('an actual current prepared bundle is required')
        selection = bundle.understanding.payload['selection']
        return super()._freeze(bundle, selection=tuple(selection[key] for key in _SELECTION_FIELDS), **kwargs)

    async def _read(self, prepared):
        if not isinstance(prepared, RecordCurrentPreparedDocumentV1):
            raise RecordDocumentVersionError('a current preparation checkpoint is required')
        try:
            bundle = await self.understanding.validate_prepared(scope=_scope(prepared), prepared=_fixed_input(prepared))
            return self._validate_bundle(bundle, prepared)
        except (
            DocumentPreparationConflictError,
            DocumentSnapshotChangedError,
            RecordDocumentCheckpointError,
        ) as error:
            raise RecordCurrentDocumentConflict('the original durable Document preparation differs') from error

    @staticmethod
    def _validate_bundle(bundle, prepared):
        if not isinstance(bundle, DocumentCurrentBundle):
            raise RecordDocumentCheckpointError('an actual current prepared bundle is required')
        PreparedRecordDocumentAdapter._validate_bundle(bundle, prepared)
        quality = bundle.facts.payload['quality']
        # The immutable archive is still owned by Document. Only the scoped
        # Profile read can contribute report content to the Record conversation.
        return {
            'status': 'pending',
            'processing': {key: quality[key] for key in ('status', 'retryable') if key in quality},
        }


def create_current_record_document_port(
    *,
    prepared: RecordCurrentDocumentPreparationPort,
    documents: RecordCurrentDocumentPublisherPort,
    ensure_self_profile: Callable[[str], Awaitable[ProfileBootstrapResult]],
    previous_port=None,
):
    """Explicit composition. Bootstrap selects the current consumer after the remaining integrations."""
    return CurrentRecordDocumentAdapter(
        prepared=prepared,
        documents=documents,
        ensure_self_profile=ensure_self_profile,
        previous_port=previous_port,
    )
