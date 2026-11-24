"""Mongo durability and composition for the public compatibility commands."""

from pymongo.errors import DuplicateKeyError

from adapters.analysis.snapshots import MongoDocumentSnapshotStore
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.application.legacy_publication import LegacyPublicationGateway
from modules.analysis.compatibility import DocumentCompatibilityService
from modules.analysis.compatibility import DocumentPreparationConflictError
from modules.analysis.snapshots import digest
from modules.analysis.understanding import DocumentUnderstandingService


class MongoDocumentCompatibilityStore:
    def __init__(self, collection):
        self.collection = collection

    @staticmethod
    def _query(kind, scope, identity):
        return {'_id': f'document_{kind}_' + digest([scope.as_dict(), identity]), **scope.as_dict(), 'kind': kind}

    async def _get(self, kind, scope, identity):
        return await self.collection.find_one(self._query(kind, scope, identity))

    async def _insert(self, kind, scope, identity, payload):
        document = {**self._query(kind, scope, identity), 'payload': payload}
        try:
            result = await self.collection.insert_one(document)
            if not result.acknowledged:
                raise RuntimeError('Document compatibility record was not acknowledged')
        except DuplicateKeyError:
            existing = await self._get(kind, scope, identity)
            if existing is None:
                raise DocumentPreparationConflictError(
                    'Document compatibility record has a conflicting owner'
                ) from None
            return existing['payload']
        return payload

    async def put_preparation(self, scope, trace_id, binding):
        if await self._insert('preparation', scope, trace_id, binding) != binding:
            raise DocumentPreparationConflictError('the preparation trace was already bound to different inputs')

    async def get_preparation(self, scope, trace_id):
        document = await self._get('preparation', scope, trace_id)
        if document is None:
            raise DocumentNotFoundError('durable preparation receipt not found in scope')
        return document['payload']

    async def get_publication(self, scope, operation_id):
        document = await self._get('legacy_receipt', scope, operation_id)
        return None if document is None else document['payload']

    async def put_publication(self, scope, operation_id, receipt):
        existing = await self._insert('legacy_receipt', scope, operation_id, receipt)
        for field in ('understandingSnapshotRef', 'canonical_report_id', 'legacy_source_revision'):
            if existing[field] != receipt[field]:
                raise DocumentPreparationConflictError('the immutable legacy acknowledgement conflicts')
        # Delivery state can advance between simultaneous calls. Keep the first
        # actual acknowledgement; this endpoint is not a live ingestion query.
        return existing


def create_document_compatibility_service(
    *, processing_service, profile_service, snapshots, snapshot_chunks, compatibility_records, publication_ledger=None
):
    """Construct without I/O; the composition root owns all resources and workers.

    Inject three dedicated Mongo collections using acknowledged durable writes
    (production: majority WriteConcern and primary reads). Their built-in unique
    _id index is sufficient; construction and reads perform no DDL. The snapshot
    adapter chunks large content. Compatibility records hold only fixed refs and
    receipts, not report bodies. No TTL may discard active recovery references.

    If a cutover ledger is installed, old processing and this facade must share
    the same instance. Do not silently install a gate on only one writer.
    """
    if processing_service._publication_ledger is not publication_ledger:
        raise ValueError('legacy processing and compatibility publication must share their cutover ledger')

    async def ensure_self(user_id):
        bootstrap = await profile_service.ensure_self_profile(user_id)
        return bootstrap.subject.subject_id

    return DocumentCompatibilityService(
        understanding=DocumentUnderstandingService(
            processing_service=processing_service,
            snapshots=MongoDocumentSnapshotStore(snapshots=snapshots, chunks=snapshot_chunks),
        ),
        publications=MongoDocumentCompatibilityStore(compatibility_records),
        legacy_publication=LegacyPublicationGateway(processing_service._report_source, publication_ledger),
        ensure_self=ensure_self,
    )
