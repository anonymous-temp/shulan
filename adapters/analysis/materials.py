"""Document-owned exact failed-call lookup and fixed archive reads, without DDL."""

from adapters.analysis.document_trace import MongoDocumentTraceStore
from adapters.analysis.snapshots import MongoDocumentSnapshotStore
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.materials import DocumentMaterialReader


class MongoDocumentMaterialArchive:
    def __init__(self, *, traces, trace_store):
        self.traces = traces
        self.trace_store = trace_store

    async def locate_failed(self, scope, call, *, stage='interpreting'):
        query = {
            **scope.as_dict(),
            **{f'source.{key}': value for key, value in call.source().items()},
            'requestedDocumentId': call.requested_document_id,
            'fileUrl': call.file_url,
            'createdAt': {'$gte': call.mongo_started_at},
            'status': 'failed',
            'stage': stage,
        }
        if call.trace_id is not None:
            query['_id'] = call.trace_id
        matches = await self.traces.find(query).limit(2).to_list(length=2)
        return matches[0] if len(matches) == 1 else None

    async def get_trace(self, scope, trace_id):
        trace = await self.traces.find_one({'_id': trace_id, **scope.as_dict()})
        if trace is None:
            raise DocumentNotFoundError('archive trace not found in scope')
        return trace

    async def read_payload(self, scope, trace_id, name):
        return await self.trace_store.read_payload(trace_id, scope.user_id, scope.subject_id, name)


def create_document_material_reader(*, snapshots, snapshot_chunks, traces, trace_chunks):
    """Pure composition; collections and retention are owned by the application.

    Snapshot writes need acknowledged durability (production majority), primary
    reads and retention of referenced trace chunks. Reads never initialize indexes.
    """
    return DocumentMaterialReader(
        snapshots=MongoDocumentSnapshotStore(snapshots=snapshots, chunks=snapshot_chunks),
        archive=MongoDocumentMaterialArchive(traces=traces, trace_store=MongoDocumentTraceStore(traces, trace_chunks)),
    )
