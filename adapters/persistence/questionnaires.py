"""Mongo source of truth plus optional Redis cache for whole questionnaires.

No TTL index deletes ownership. Expiration is an explicit application transition;
submitted source snapshots are independent of the mutable shared Chat document.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from copy import deepcopy
from uuid import uuid4

from bson import BSON

logger = logging.getLogger(__name__)
MAX_DOCUMENT_BYTES = 15 * 1024 * 1024


def _bounded_document(snapshot, *, reserve_bytes=0):
    if len(BSON.encode(snapshot)) + reserve_bytes > MAX_DOCUMENT_BYTES:
        raise ValueError('Questionnaire snapshot exceeds its Mongo storage budget')


class MongoQuestionnaireStore:
    def __init__(self, collection, *, cache=None, clock=time.time):
        self._collection = collection
        self._cache = cache
        self._clock = clock
        self._indexes_ready = False
        self._index_lock = asyncio.Lock()

    @staticmethod
    def validate_capacity(snapshot, *, reserve_bytes=0):
        """Check projected publication/submission size without database writes."""
        _bounded_document(snapshot, reserve_bytes=reserve_bytes)

    async def ensure_indexes(self):
        if self._indexes_ready:
            return
        async with self._index_lock:
            if self._indexes_ready:
                return
            await self._collection.create_index(
                [('userId', 1), ('questionnaireId', 1)],
                unique=True,
                name='questionnaire_identity',
            )
            await self._collection.create_index(
                [('userId', 1), ('sessionId', 1), ('createdAt', -1)],
                name='questionnaire_session_history',
            )
            await self._collection.create_index(
                [('status', 1), ('nextAttemptAt', 1)],
                name='questionnaire_recovery',
            )
            await self._collection.create_index(
                [('status', 1), ('expiresAt', 1)],
                name='questionnaire_expiration',
            )
            await self._collection.create_index(
                [('profileIngestion.status', 1), ('submittedAt', 1)],
                name='questionnaire_profile_pending',
            )
            await self._collection.create_index(
                [('factorAssessment.status', 1), ('submittedAt', 1)],
                name='questionnaire_factor_pending',
            )
            self._indexes_ready = True

    @staticmethod
    def _clean(document):
        if document is None:
            return None
        result = deepcopy(document)
        result.pop('_id', None)
        return result

    @staticmethod
    def _cache_key(user_id, questionnaire_id):
        return f'shulan:questionnaire:{len(user_id)}:{user_id}:{questionnaire_id}'

    async def _cache_snapshot(self, snapshot):
        if self._cache is None:
            return
        try:
            key = self._cache_key(snapshot['userId'], snapshot['questionnaireId'])
            if snapshot['status'] == 'ready':
                ttl = math.ceil(snapshot['expiresAt'] - self._clock())
                if ttl > 0:
                    await self._cache.setex(key, ttl, json.dumps(snapshot, ensure_ascii=False, allow_nan=False))
                    return
            await self._cache.delete(key)
        except Exception as error:
            logger.warning('Questionnaire cache unavailable: %s', type(error).__name__)

    async def _read_cached_copy(self, authoritative):
        # Mongo is always consulted for current ownership/revision. A stale or
        # deleted Redis item can never change whether Chat is permitted.
        snapshot = self._clean(authoritative)
        if snapshot is None or self._cache is None or snapshot['status'] != 'ready':
            return snapshot
        try:
            cached = await self._cache.get(self._cache_key(snapshot['userId'], snapshot['questionnaireId']))
            if cached:
                value = json.loads(cached)
                if (
                    isinstance(value, dict)
                    and value.get('revision') == snapshot['revision']
                    and value.get('userId') == snapshot['userId']
                    and value.get('questionnaireId') == snapshot['questionnaireId']
                ):
                    return value
        except Exception as error:
            logger.warning('Questionnaire cache read failed: %s', type(error).__name__)
        # Reads deliberately do not refresh TTL.
        return snapshot

    async def insert(self, snapshot):
        _bounded_document(snapshot)
        await self.ensure_indexes()
        try:
            result = await self._collection.insert_one(deepcopy(snapshot))
        except Exception as error:
            if type(error).__name__ == 'DuplicateKeyError':
                return False
            raise
        return bool(result.acknowledged)

    async def save(self, snapshot, *, expected_revision):
        _bounded_document(snapshot)
        await self.ensure_indexes()
        result = await self._collection.replace_one(
            {
                'userId': snapshot['userId'],
                'questionnaireId': snapshot['questionnaireId'],
                'revision': expected_revision,
            },
            deepcopy(snapshot),
            upsert=False,
        )
        saved = bool(result.acknowledged and result.matched_count == 1)
        if saved:
            await self._cache_snapshot(snapshot)
        return saved

    async def get_by_id(self, user_id, questionnaire_id):
        document = await self._collection.find_one(
            {
                'userId': user_id,
                'questionnaireId': questionnaire_id,
            }
        )
        return await self._read_cached_copy(document)

    async def get_current(self, user_id, session_id):
        document = await self._collection.find_one(
            {'userId': user_id, 'sessionId': session_id},
            sort=[('createdAt', -1), ('_id', -1)],
        )
        return await self._read_cached_copy(document)

    async def get_submitted(self, user_id, questionnaire_id):
        return self._clean(
            await self._collection.find_one(
                {
                    'userId': user_id,
                    'questionnaireId': questionnaire_id,
                    'status': 'submitted',
                }
            )
        )

    async def get_for_factor_assessment(self, user_id, questionnaire_id):
        """Read source ownership without cache access, index DDL or recovery."""
        return self._clean(await self._collection.find_one({'userId': user_id, 'questionnaireId': questionnaire_id}))

    async def list_pending_factor_assessments(self, *, limit=100):
        await self.ensure_indexes()
        cursor = self._collection.find({'status': 'submitted', 'factorAssessment.status': 'pending'}).sort(
            [('submittedAt', 1), ('_id', 1)]
        )
        return [self._clean(doc) for doc in await cursor.to_list(length=max(1, min(limit, 1000)))]

    async def mark_factor_assessment_queued(self, user_id, questionnaire_id, assessment_id):
        await self.ensure_indexes()
        result = await self._collection.update_one(
            {
                'userId': user_id,
                'questionnaireId': questionnaire_id,
                'status': 'submitted',
                'factorAssessment.assessmentId': assessment_id,
            },
            {'$set': {'factorAssessment.status': 'queued', 'revision': uuid4().hex}},
        )
        return bool(result.acknowledged and result.matched_count == 1)

    async def list_recoverable(self, *, now, limit=100):
        await self.ensure_indexes()
        cursor = self._collection.find(
            {
                '$or': [
                    {'status': {'$in': ['queued', 'generating']}, 'nextAttemptAt': {'$lte': now}},
                    {'status': 'failed', 'retryable': True, 'nextAttemptAt': {'$lte': now}},
                    {'status': 'ready', 'pendingSubmission': {'$exists': True}},
                    {'status': 'ready', 'expiresAt': {'$lte': now}},
                    {'status': 'failed', 'retryable': False, 'cleanupAt': {'$lte': now}},
                    {'status': 'expired', 'cleanupPending': True},
                ],
            }
        ).sort([('updatedAt', 1), ('_id', 1)])
        return [self._clean(doc) for doc in await cursor.to_list(length=max(1, min(limit, 1000)))]

    async def list_pending_ingestion(self, *, limit=100):
        await self.ensure_indexes()
        cursor = self._collection.find(
            {
                'status': 'submitted',
                'profileIngestion.status': 'pending',
                'profileIngestion.deliveryMode': {'$in': [None, 'legacy']},
            }
        ).sort([('submittedAt', 1), ('_id', 1)])
        return [self._clean(doc) for doc in await cursor.to_list(length=max(1, min(limit, 1000)))]

    async def mark_ingestion_queued(self, user_id, questionnaire_id, job_id):
        await self.ensure_indexes()
        result = await self._collection.update_one(
            {'userId': user_id, 'questionnaireId': questionnaire_id, 'status': 'submitted'},
            {
                '$set': {
                    'profileIngestion.status': 'queued',
                    'profileIngestion.jobId': job_id,
                    'revision': uuid4().hex,
                }
            },
        )
        return bool(result.acknowledged and result.matched_count == 1)

    async def mark_ingestion_source_managed(self, user_id, questionnaire_id, *, source_revision):
        """Retire a legacy intent by source revision; do not invent a queued job."""
        result = await self._collection.update_one(
            {
                'userId': user_id,
                'questionnaireId': questionnaire_id,
                'sourceRevision': source_revision,
                'status': 'submitted',
                'profileIngestion.status': 'pending',
            },
            {
                '$set': {
                    'profileIngestion.status': 'source_managed',
                    'profileIngestion.reasonCode': 'source.managed_by_publication',
                    'revision': uuid4().hex,
                }
            },
        )
        return bool(result.acknowledged and result.matched_count == 1)
