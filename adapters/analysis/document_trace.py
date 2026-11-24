"""MongoDB document provenance with chunked, checksummed BSON payloads."""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any
from uuid import uuid4

from bson import BSON
from pymongo import ASCENDING
from pymongo import DESCENDING


class MongoDocumentTraceStore:
    """Metadata and payloads are separate; incomplete chunks are never published."""

    CHUNK_BYTES = 1024 * 1024

    def __init__(self, collection, chunks) -> None:
        self._collection = collection
        self._chunks = chunks
        self._ready = False
        self._lock = asyncio.Lock()

    async def ensure_indexes(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            await self._collection.create_index(
                [('userId', ASCENDING), ('reportId', ASCENDING), ('createdAt', DESCENDING)],
                name='document_trace_report',
            )
            await self._collection.create_index(
                [('userId', ASCENDING), ('source.entrypoint', ASCENDING), ('source.sourceId', ASCENDING)],
                name='document_trace_source',
            )
            await self._collection.create_index(
                [('status', ASCENDING), ('updatedAt', ASCENDING)], name='document_trace_status'
            )
            await self._chunks.create_index(
                [('userId', ASCENDING), ('traceId', ASCENDING), ('payloadId', ASCENDING), ('index', ASCENDING)],
                unique=True,
                name='document_trace_payload_chunk',
            )
            self._ready = True

    async def create(self, document: dict[str, Any]) -> None:
        await self.ensure_indexes()
        result = await self._collection.insert_one(document)
        if not result.acknowledged:
            raise RuntimeError('文件处理留痕未可靠保存')

    async def update(self, trace_id: str, user_id: str, fields: dict[str, Any]) -> None:
        update = {'$set': fields}
        if 'stage' in fields:
            update['$push'] = {
                'events': {
                    'stage': fields['stage'],
                    'status': fields.get('status', 'running'),
                    'at': fields['updatedAt'],
                }
            }
        result = await self._collection.update_one(
            {'_id': trace_id, 'userId': user_id},
            update,
        )
        if not result.acknowledged or result.matched_count != 1:
            raise RuntimeError('文件处理留痕更新失败')

    async def write_payload(self, trace_id: str, user_id: str, name: str, payload: Any) -> None:
        if name not in {
            'original_file',
            'docmind_result',
            'analysis_input',
            'analysis_output',
            'analysis_metrics',
            'analysis_response',
            'publication',
            'interpretation',
            'output_raw',
            'output_result',
            'source_result',
        } and not (
            re.fullmatch(r'analysis_(?:input|output|part)_[0-9]{5}(?:_[0-9]){0,2}', name)
            or re.fullmatch(r'analysis_(?:input|output)_[0-9]{5}_repair', name)
        ):
            raise ValueError(f'未知文档留痕内容类型: {name}')
        encoded = BSON.encode({'payload': payload})
        payload_id = uuid4().hex
        chunk_count = (len(encoded) + self.CHUNK_BYTES - 1) // self.CHUNK_BYTES
        for index in range(chunk_count):
            result = await self._chunks.insert_one(
                {
                    '_id': f'{trace_id}:{payload_id}:{index}',
                    'traceId': trace_id,
                    'userId': user_id,
                    'payloadId': payload_id,
                    'name': name,
                    'index': index,
                    'data': encoded[index * self.CHUNK_BYTES : (index + 1) * self.CHUNK_BYTES],
                }
            )
            if not result.acknowledged:
                raise RuntimeError('文件处理原始数据分块保存失败')
        # A fresh payload ID also makes an interrupted storage retry safe.
        await self.update(
            trace_id,
            user_id,
            {
                f'payloads.{name}': {
                    'payloadId': payload_id,
                    'encoding': 'bson',
                    'bytes': len(encoded),
                    'chunks': chunk_count,
                    'sha256': hashlib.sha256(encoded).hexdigest(),
                }
            },
        )

    async def read_payload(self, trace_id: str, user_id: str, subject_id: str, name: str) -> Any:
        document = await self._collection.find_one({'_id': trace_id, 'userId': user_id, 'subjectId': subject_id})
        if document is None:
            raise LookupError('文件处理留痕不存在或不属于该用户及主体')
        reference = document.get('payloads', {}).get(name)
        if reference is None:
            raise LookupError('文件处理留痕缺少完整数据')
        parts = []
        for index in range(reference['chunks']):
            chunk = await self._chunks.find_one(
                {
                    '_id': f'{trace_id}:{reference["payloadId"]}:{index}',
                    'userId': user_id,
                }
            )
            if chunk is None:
                raise RuntimeError('文件处理留痕分块缺失')
            parts.append(chunk['data'])
        encoded = b''.join(parts)
        if len(encoded) != reference['bytes'] or hashlib.sha256(encoded).hexdigest() != reference['sha256']:
            raise RuntimeError('文件处理留痕校验失败')
        return BSON(encoded).decode()['payload']


def create_application_document_trace_store() -> MongoDocumentTraceStore:
    from db.mongodb import collection_document_trace_chunks
    from db.mongodb import collection_document_traces

    return MongoDocumentTraceStore(collection_document_traces, collection_document_trace_chunks)
