"""Mongo task records, fenced attempts and immutable factor assessment results."""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from uuid import uuid4

from bson import BSON
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from modules.questionnaire.factor_tasks import FactorPayloadTooLarge

MAX_DOCUMENT_BYTES = 15 * 1024 * 1024


def _bounded_document(document):
    if len(BSON.encode(document)) > MAX_DOCUMENT_BYTES:
        raise FactorPayloadTooLarge('Factor task exceeds its Mongo storage budget')


class MongoQuestionnaireFactorTaskStore:
    def __init__(self, collection, *, clock=time.time):
        self.collection = collection
        self.clock = clock
        self._indexes_ready = False
        self._index_lock = asyncio.Lock()

    async def ensure_indexes(self):
        if self._indexes_ready:
            return
        async with self._index_lock:
            if self._indexes_ready:
                return
            await self.collection.create_index(
                [
                    ('userId', 1),
                    ('questionnaireId', 1),
                    ('answerHash', 1),
                    ('preparationVersion', 1),
                    ('pipelineVersion', 1),
                ],
                unique=True,
                name='questionnaire_factor_identity',
            )
            await self.collection.create_index(
                [('status', 1), ('nextAttemptAt', 1), ('leaseUntil', 1)],
                name='questionnaire_factor_due',
            )
            self._indexes_ready = True

    async def admit(self, specification, *, terminal_error=None):
        await self.ensure_indexes()
        now = self.clock()
        document = {
            **deepcopy(specification),
            '_id': specification['assessmentId'],
            'status': 'failed' if terminal_error else 'queued',
            'attempts': 0,
            'createdAt': now,
            'updatedAt': now,
            'nextAttemptAt': now,
            'result': None,
            'retryable': not bool(terminal_error),
        }
        if terminal_error:
            document['errorCode'] = terminal_error
        try:
            _bounded_document(document)
        except FactorPayloadTooLarge:
            # BSON can exceed the JSON budget for many small array entries.
            # Persist a real terminal outcome, retaining only its source fingerprint.
            document.update(
                inputSnapshot=None,
                status='failed',
                retryable=False,
                errorCode='FACTOR_INPUT_TOO_LARGE',
            )
            _bounded_document(document)
        try:
            result = await self.collection.insert_one(document)
            if not result.acknowledged:
                raise RuntimeError('Factor task admission was not acknowledged')
            return document
        except DuplicateKeyError:
            existing = await self.get(document['userId'], document['questionnaireId'], document['_id'])
            if existing is None or existing['inputFingerprint'] != document['inputFingerprint']:
                raise ValueError('Factor task identity collides with a different frozen input')
            return existing

    async def get(self, user_id, questionnaire_id, assessment_id):
        # Result reads perform no index creation, enqueue or state transition.
        return await self.collection.find_one(
            {'_id': assessment_id, 'userId': user_id, 'questionnaireId': questionnaire_id},
        )

    async def claim(self, owner, *, lease_seconds, max_attempts):
        await self.ensure_indexes()
        now = self.clock()
        return await self.collection.find_one_and_update(
            {
                'attempts': {'$lt': max_attempts},
                '$or': [
                    {'status': 'queued', 'nextAttemptAt': {'$lte': now}},
                    {'status': 'running', 'leaseUntil': {'$lte': now}},
                ],
            },
            {
                '$set': {
                    'status': 'running',
                    'leaseOwner': owner,
                    'leaseToken': uuid4().hex,
                    'leaseUntil': now + lease_seconds,
                    'updatedAt': now,
                },
                '$inc': {'attempts': 1},
            },
            sort=[('createdAt', 1), ('_id', 1)],
            return_document=ReturnDocument.AFTER,
        )

    def _lease(self, job):
        return {
            '_id': job['_id'],
            'userId': job['userId'],
            'sessionId': job['sessionId'],
            'questionnaireId': job['questionnaireId'],
            'assessmentId': job['assessmentId'],
            'inputFingerprint': job['inputFingerprint'],
            'pipelineVersion': job['pipelineVersion'],
            'questionnaireAgentManifest': deepcopy(job.get('questionnaireAgentManifest')),
            'status': 'running',
            'leaseOwner': job['leaseOwner'],
            'leaseToken': job['leaseToken'],
            'leaseUntil': {'$gt': self.clock()},
        }

    async def _update(self, query, update):
        result = await self.collection.update_one(query, update)
        if not result.acknowledged:
            raise RuntimeError('Factor task write was not acknowledged')
        return bool(result.matched_count)

    async def renew(self, job, *, lease_seconds):
        return await self._update(self._lease(job), {'$set': {'leaseUntil': self.clock() + lease_seconds}})

    async def save_checkpoint(self, job, checkpoint):
        _bounded_document({**job, 'checkpoint': checkpoint})
        return await self._update(
            self._lease(job),
            {'$set': {'checkpoint': deepcopy(checkpoint), 'updatedAt': self.clock()}},
        )

    async def bind_execution(self, job, *, expected_manifest):
        """Freeze the accepted version and attempt metadata under the existing lease."""
        return await self._update(
            {
                **self._lease(job),
                'questionnaireAgentManifest': deepcopy(expected_manifest),
            },
            {
                '$set': {
                    'questionnaireAgentManifest': deepcopy(job['questionnaireAgentManifest']),
                    'questionnaireAgentExecution': deepcopy(job['questionnaireAgentExecution']),
                    'updatedAt': self.clock(),
                }
            },
        )

    async def complete(self, job, result):
        document = {key: value for key, value in job.items() if key != 'checkpoint'}
        _bounded_document({**document, 'result': result})
        return await self._update(
            self._lease(job),
            {
                '$set': {
                    'status': 'completed',
                    'result': deepcopy(result),
                    'retryable': False,
                    'updatedAt': self.clock(),
                },
                '$unset': {'leaseOwner': '', 'leaseToken': '', 'leaseUntil': '', 'checkpoint': '', 'errorCode': ''},
            },
        )

    async def fail(self, job, *, error_code, retryable, max_attempts, delay_seconds):
        retry = retryable and job['attempts'] < max_attempts
        return await self._update(
            self._lease(job),
            {
                '$set': {
                    'status': 'queued' if retry else 'failed',
                    'retryable': retry,
                    'errorCode': error_code,
                    'updatedAt': self.clock(),
                    'nextAttemptAt': self.clock() + delay_seconds,
                },
                '$unset': {'leaseOwner': '', 'leaseToken': '', 'leaseUntil': ''},
            },
        )

    async def recover_exhausted(self, *, max_attempts, limit=100):
        await self.ensure_indexes()
        recovered = 0
        for _ in range(limit):
            job = await self.collection.find_one_and_update(
                {'status': 'running', 'leaseUntil': {'$lte': self.clock()}, 'attempts': {'$gte': max_attempts}},
                {
                    '$set': {
                        'status': 'failed',
                        'retryable': False,
                        'errorCode': 'FACTOR_ATTEMPTS_EXHAUSTED',
                        'updatedAt': self.clock(),
                    },
                    '$unset': {'leaseOwner': '', 'leaseToken': '', 'leaseUntil': ''},
                },
                return_document=ReturnDocument.AFTER,
            )
            if job is None:
                break
            recovered += 1
        return recovered
