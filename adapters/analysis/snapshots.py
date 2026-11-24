"""Immutable, chunked snapshot storage; reads never initialize collections."""

from __future__ import annotations

import hashlib
import json

from pymongo.errors import DuplicateKeyError

from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.snapshots import RETRACTION_CONTRACT
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import canonical_json


class MongoDocumentSnapshotStore:
    CHUNK_BYTES = 1024 * 1024

    def __init__(self, *, snapshots, chunks):
        self.snapshots = snapshots
        self.chunks = chunks

    async def _insert_equal(self, collection, document):
        try:
            result = await collection.insert_one(document)
            if not result.acknowledged:
                raise RuntimeError('snapshot storage was not acknowledged')
        except DuplicateKeyError:
            if await collection.find_one({'_id': document['_id']}) != document:
                raise ValueError('immutable snapshot content conflicts') from None

    async def put(self, snapshot: DocumentSnapshot) -> None:
        encoded = canonical_json(snapshot.canonical).encode('utf-8')
        count = (len(encoded) + self.CHUNK_BYTES - 1) // self.CHUNK_BYTES
        owner = snapshot.scope.as_dict()
        for index in range(count):
            await self._insert_equal(
                self.chunks,
                {
                    '_id': f'{snapshot.snapshot_id}:{index}',
                    **owner,
                    'data': encoded[index * self.CHUNK_BYTES : (index + 1) * self.CHUNK_BYTES],
                },
            )
        await self._insert_equal(
            self.snapshots,
            {
                '_id': snapshot.snapshot_id,
                **owner,
                'contractVersion': snapshot.contract_version,
                'contentDigest': snapshot.content_digest,
                'chunks': count,
                'bytes': len(encoded),
            },
        )

    async def get(self, scope: DocumentScope, snapshot_id: str) -> DocumentSnapshot:
        document = await self.snapshots.find_one({'_id': snapshot_id, **scope.as_dict()})
        if document is None:
            raise DocumentNotFoundError('snapshot not found in scope')
        parts = []
        for index in range(document['chunks']):
            chunk = await self.chunks.find_one({'_id': f'{snapshot_id}:{index}', **scope.as_dict()})
            if chunk is None:
                raise RuntimeError('snapshot chunk is unavailable')
            parts.append(chunk['data'])
        encoded = b''.join(parts)
        if len(encoded) != document['bytes'] or hashlib.sha256(encoded).hexdigest() != document['contentDigest']:
            raise RuntimeError('snapshot integrity check failed')
        value = json.loads(encoded)
        payload = (
            {
                key: value[key]
                for key in ('publicationRevision', 'priorRevision', 'eventKind', 'lifecycleEpoch')
                if key in value
            }
            if value['contractVersion'] == RETRACTION_CONTRACT
            else value['payload']
        )
        snapshot = DocumentSnapshot(
            scope,
            value['contractVersion'],
            value['sourceId'],
            value['sourceRevision'],
            canonical_json(payload),
        )
        if snapshot.snapshot_id != snapshot_id or value['scope'] != scope.as_dict():
            raise RuntimeError('snapshot identity check failed')
        return snapshot
