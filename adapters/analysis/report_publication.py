"""Durable Report compatibility command composition, owned by Document."""

from adapters.analysis.compatibility import MongoDocumentCompatibilityStore
from modules.analysis.compatibility import DocumentPreparationConflictError
from modules.analysis.report_publication import DocumentReportPublicationService


class MongoDocumentReportPublicationStore(MongoDocumentCompatibilityStore):
    async def get_command(self, scope, identity):
        document = await self._get('report_command', scope, identity)
        if document is None:
            return None
        execution = await self._get('report_execution', scope, identity)
        return {
            'manifest': document['payload'],
            'executionRef': execution['payload']['executionRef'] if execution else None,
        }

    async def put_command(self, manifest):
        value = await self._insert('report_command', manifest.scope, manifest.command_id, manifest.as_dict())
        if value != manifest.as_dict():
            raise DocumentPreparationConflictError('the immutable Report command conflicts')

    async def bind_execution(self, manifest, snapshot_ref):
        value = {'manifest': manifest.as_dict(), 'executionRef': snapshot_ref}
        stored = await self._insert('report_execution', manifest.scope, manifest.command_id, value)
        if value != stored:
            raise DocumentPreparationConflictError('the immutable Report execution inputs conflict')
        return stored['executionRef']

    async def bind_output_trace(self, manifest, *, binding, trace_id):
        if not isinstance(trace_id, str) or not trace_id:
            raise DocumentPreparationConflictError('the actual output trace is unavailable')
        value = {'binding': binding, 'traceId': trace_id}
        stored = await self._insert('report_output_trace', manifest.scope, manifest.command_id, value)
        if stored['binding'] != binding or not isinstance(stored.get('traceId'), str) or not stored['traceId']:
            raise DocumentPreparationConflictError('the fixed output trace binding conflicts')
        return stored['traceId']

    async def get_receipt(self, scope, identity):
        document = await self._get('report_receipt', scope, identity)
        return document['payload'] if document else None

    async def put_receipt(self, receipt):
        value = receipt.as_dict()
        stored = await self._insert('report_receipt', receipt.manifest.scope, receipt.receipt_ref, value)
        # The ingestion job can advance between concurrent acknowledgements, but
        # no input, projection, legacy revision or preparation identity may change.
        for key in ('manifest', 'projectionRef', 'basePublications', 'legacySourceRevision', 'processingStatus'):
            if value[key] != stored[key]:
                raise DocumentPreparationConflictError('the immutable Report acknowledgement conflicts')
        return stored


def create_document_report_publication_service(
    *,
    processing_service,
    compatibility_service,
    material_reader,
    command_records,
):
    """Compose the exact historical publisher without construction I/O.

    The root injects the existing processing service with the v16 legacy extractor
    registered; no business extractor is imported by Document. Compatibility and
    processing must share the very same publication gate. Their immutable snapshot
    store and material reader must address the same snapshot collections.

    command_records is a dedicated collection with acknowledged majority writes,
    primary reads and no TTL. The built-in unique _id index suffices; construction
    never issues DDL, opens a client, starts a worker or calls a model. Large fixed
    public inputs and projections use the existing chunked snapshot store, not
    inline command documents. The application retains original archive chunks.
    """
    if compatibility_service.legacy_publication.ledger is not processing_service._publication_ledger:
        raise ValueError('Report compatibility and processing must share their publication gate')
    if compatibility_service.understanding.processing is not processing_service:
        raise ValueError('Report compatibility must use the same Document processing owner')
    return DocumentReportPublicationService(
        compatibility=compatibility_service,
        materials=material_reader,
        snapshots=compatibility_service.understanding.snapshots,
        commands=MongoDocumentReportPublicationStore(command_records),
        coordinator=processing_service._document_outputs,
        read_legacy_revision=processing_service._report_source.get_owned_revision,
    )
