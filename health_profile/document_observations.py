"""Queryable original report observations, including undated and unmapped values.

These rows are source evidence, not active health facts. Their immutable identity
also assigns an observation to one ingestion revision of the canonical report.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

from .pagination import decode_cursor
from .pagination import encode_cursor


def source_items(result):
    from .report_ingestion import _report_observations

    return _report_observations(result, for_ingestion=False)


def observation_identity(item, report_time=None):
    from .report_ingestion import _observation_time

    observed = _observation_time(item, report_time)
    values = {
        key: item.get(key)
        for key in (
            'itemName',
            'resultValue',
            'unit',
            'method',
            'specimen',
            'kind',
            'referenceRange',
            'flag',
        )
    }
    # Strings and their exact scalar equivalents denote the same printed source value.
    for key in ('itemName', 'resultValue', 'unit', 'method', 'specimen', 'referenceRange', 'flag'):
        if values[key] is not None:
            values[key] = str(values[key]).strip()
    values['observedAt'] = observed.isoformat() if observed else None
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def source_binding(result, pointer):
    bindings = result.get('sourceBindings')
    if not isinstance(bindings, dict):
        return None
    binding = bindings.get(pointer)
    if binding is None and pointer.startswith('/expansion/'):
        binding = bindings.get(pointer.replace('/expansion/', '/observations/', 1))
    return binding if isinstance(binding, str) and binding.startswith('v6:') else None


def _fill_missing(existing, incoming):
    """Only verified absence may be enriched; old values and source identity win."""
    merged = deepcopy(existing)
    for key in ('unit', 'method', 'specimen', 'kind', 'referenceRange', 'flag', 'observedAt'):
        if merged.get(key) in (None, '') and incoming.get(key) not in (None, ''):
            merged[key] = deepcopy(incoming[key])
    return merged


class MongoDocumentObservationStore:
    def __init__(self, collection, jobs=None):
        self.collection, self.jobs = collection, jobs
        self._ready = False

    async def ensure_indexes(self):
        if not self._ready:
            await self.collection.create_index(
                [('userId', ASCENDING), ('subjectId', ASCENDING), ('_id', ASCENDING)],
                name='document_observation_owner_page',
            )
            await self.collection.create_index(
                [('canonicalReportId', ASCENDING), ('ownerRevision', ASCENDING)],
                name='document_observation_source_revision',
            )
            self._ready = True

    async def capture(self, record):
        """Durably claim source rows; retries recover the same owning revision."""
        from .report_ingestion import _observation_time

        await self.ensure_indexes()
        owned = []
        owned_ids = set()
        canonical = record.canonical_report_id or record.report_id
        for pointer, original in source_items(record.result):
            if not isinstance(original, dict):
                continue
            item = deepcopy(original)
            if pointer.endswith(('/expansion/findings', '/expansion/diagnosis')):
                item['kind'] = 'narrative'
                item['narrativeType'] = 'diagnosis' if pointer.endswith('/diagnosis') else 'finding'
            binding = source_binding(record.result, pointer)
            digest = (
                'v6_' + hashlib.sha256(binding.encode('utf-8')).hexdigest()
                if binding
                else observation_identity(item, record.result.get('reportTime'))
            )
            row_id = f'{canonical}:{digest}'
            observed = _observation_time(item, record.result.get('reportTime'))
            if binding and observed:
                item['observedAt'] = observed.isoformat()
            document_ids = record.result.get('sourceDocumentIds') or [record.document_id]
            document = {
                '_id': row_id,
                'userId': record.user_id,
                'subjectId': record.subject_id,
                'documentId': record.document_id if len(document_ids) == 1 else None,
                'documentIds': document_ids,
                'canonicalReportId': canonical,
                'ownerRevision': record.source_revision,
                'sourcePointer': pointer,
                'observation': item,
                'observedAt': observed.isoformat() if observed else None,
                'dateStatus': 'known' if observed else 'unknown',
                'sourceReportTime': record.result.get('reportTime'),
                'createdAt': record.created_at,
            }
            if binding:
                document['metadataVersion'] = 1
            try:
                result = await self.collection.insert_one(document)
                if not result.acknowledged:
                    raise RuntimeError('原报告指标保存失败')
                saved = document
            except DuplicateKeyError:
                saved = await self.collection.find_one(
                    {'_id': row_id, 'userId': record.user_id, 'subjectId': record.subject_id}
                )
                if saved is None:
                    raise
                if binding:
                    saved = await self._enrich_source(saved, document)
            if saved['ownerRevision'] == record.source_revision and digest not in owned_ids:
                item = deepcopy(saved['observation'])
                item['sourceObservationId'] = digest
                item['sourcePointer'] = saved['sourcePointer']
                item['observedAt'] = saved['observedAt']
                owned.append(item)
                owned_ids.add(digest)
        return owned

    async def _enrich_source(self, previous, incoming):
        """CAS a monotonic source projection; immutable report revisions retain history."""
        for _ in range(8):
            if any(
                str(previous['observation'].get(key, '')).strip() != str(incoming['observation'].get(key, '')).strip()
                for key in ('itemName', 'resultValue')
            ):
                # A physical binding is not permission to replace a known result.
                return previous
            merged = _fill_missing(previous['observation'], incoming['observation'])
            if merged == previous['observation']:
                return previous
            fields = {
                'observation': merged,
                'observedAt': merged.get('observedAt'),
                'dateStatus': 'known' if merged.get('observedAt') else 'unknown',
                'ownerRevision': incoming['ownerRevision'],
                'metadataVersion': previous.get('metadataVersion', 1) + 1,
            }
            updated = await self.collection.update_one(
                {
                    '_id': previous['_id'],
                    'userId': previous['userId'],
                    'subjectId': previous['subjectId'],
                    'metadataVersion': previous.get('metadataVersion'),
                },
                {'$set': fields},
            )
            if not updated.acknowledged:
                raise RuntimeError('原报告指标补充信息未可靠保存')
            if updated.matched_count == 1:
                return {**previous, **fields}
            previous = await self.collection.find_one(
                {
                    '_id': previous['_id'],
                    'userId': previous['userId'],
                    'subjectId': previous['subjectId'],
                }
            )
            if previous is None:
                raise RuntimeError('原报告指标来源丢失')
        raise RuntimeError('原报告指标补充信息发生并发冲突，请重试')

    async def list_observations(self, user_id, subject_id, *, document_id=None, cursor=None, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit 必须为 1 到 100 的整数')
        scope = {'userId': user_id, 'subjectId': subject_id, 'documentId': document_id}
        filters = {key: value for key, value in scope.items() if value is not None and key != 'documentId'}
        if document_id is not None:
            filters['documentIds'] = document_id
        if cursor is not None:
            decoded = decode_cursor('document-observations', cursor)
            if decoded.get('scope') != scope or not isinstance(decoded.get('id'), str):
                raise ValueError('cursor 与当前查询条件不匹配')
            filters['_id'] = {'$gt': decoded['id']}
        await self.ensure_indexes()
        rows = await self.collection.find(filters).sort([('_id', ASCENDING)]).to_list(length=limit + 1)
        more = len(rows) > limit
        rows = rows[:limit]
        items, statuses = [], {}
        for row in rows:
            job_key = f'report:{row["canonicalReportId"]}:{row["ownerRevision"]}'
            if job_key not in statuses:
                job = (
                    None
                    if self.jobs is None
                    else await self.jobs.find_one({'idempotencyKey': job_key, 'userId': user_id})
                )
                statuses[job_key] = job.get('status', 'pending') if job else 'pending'
            items.append(
                {
                    'observationId': row['_id'],
                    'documentId': row['documentId'],
                    'documentIds': row['documentIds'],
                    'canonicalReportId': row['canonicalReportId'],
                    'sourceRevision': row['ownerRevision'],
                    'sourcePointer': row['sourcePointer'],
                    'observation': deepcopy(row['observation']),
                    'observedAt': row['observedAt'],
                    'dateStatus': row['dateStatus'],
                    'ingestionStatus': statuses[job_key],
                    'factEligibility': 'date_unknown' if row['dateStatus'] == 'unknown' else 'subject_to_fact_policy',
                }
            )
        return {
            'items': items,
            'nextCursor': encode_cursor(
                'document-observations',
                {
                    'scope': scope,
                    'id': rows[-1]['_id'],
                },
            )
            if more
            else None,
        }
