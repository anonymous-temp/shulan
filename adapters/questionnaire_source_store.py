"""Questionnaire-owned PRIMARY task records and immutable bounded material chunks."""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy

from pymongo import ReadPreference
from pymongo.collation import Collation
from pymongo.errors import DuplicateKeyError
from pymongo.write_concern import WriteConcern

from modules.questionnaire.profile_source import QuestionnaireSourceConflict
from modules.questionnaire.profile_source import canonical_bytes
from modules.questionnaire.profile_source import raw_submission
from modules.questionnaire.profile_source import saved_binding
from modules.questionnaire.profile_source import source_digest

CHUNK_BYTES = 512 * 1024
MAX_BLOB_BYTES = 96 * 1024 * 1024
TERMINAL = {'applied', 'partial', 'failed', 'superseded'}


def primary_collection(collection, *, writable=False):
    options = {'read_preference': ReadPreference.PRIMARY}
    if writable:
        options['write_concern'] = WriteConcern(w='majority', j=True, wtimeout=5000)
    return collection.with_options(**options)


class MongoQuestionnaireSavedSourceReader:
    def __init__(self, collection):
        self.collection = primary_collection(collection)

    async def read_submission(self, *, user_id, questionnaire_id):
        rows = (
            await self.collection.find(
                {
                    'userId': user_id,
                    'questionnaireId': questionnaire_id,
                }
            )
            .max_time_ms(5000)
            .limit(2)
            .to_list(2)
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise QuestionnaireSourceConflict('Questionnaire submission identity is not unique')
        result = deepcopy(rows[0])
        if result.get('status') != 'submitted':
            return None
        return result

    async def read_source(self, *, user_id, questionnaire_id):
        source = await self.read_submission(user_id=user_id, questionnaire_id=questionnaire_id)
        return saved_binding(source)

    async def read_raw(self, *, user_id, questionnaire_id):
        source = await self.read_submission(user_id=user_id, questionnaire_id=questionnaire_id)
        binding = saved_binding(source)
        if binding is None:
            raise LookupError('The fixed new-mode Questionnaire submission is unavailable')
        return binding, raw_submission(source)

    async def discover(self, *, after, limit):
        fields = ('userId', 'questionnaireId')
        query = {
            'status': 'submitted',
            'profileIngestion.deliveryMode': {'$exists': True, '$nin': [None, 'legacy']},
            **{field: {'$type': 'string', '$ne': ''} for field in fields},
        }
        if after is not None:
            query['$or'] = [
                {**{key: after[key] for key in fields[:index]}, field: {'$gt': after[field]}}
                for index, field in enumerate(fields)
            ]
        rows = (
            await self.collection.find(query, {'_id': 0, 'userId': 1, 'questionnaireId': 1})
            .sort([(field, 1) for field in fields])
            .collation(Collation(locale='simple'))
            .max_time_ms(5000)
            .limit(limit)
            .to_list(limit)
        )
        return [{field: row[field] for field in fields} for row in rows]


class MongoQuestionnaireSourceStore:
    """One injected dedicated collection, no TTL, DDL or provider I/O in construction."""

    def __init__(self, collection):
        self.collection = primary_collection(collection, writable=True)

    async def insert(self, row):
        row = deepcopy(row)
        try:
            result = await self.collection.insert_one(row)
            if not result.acknowledged:
                raise RuntimeError('Questionnaire source write was not acknowledged')
            return row
        except DuplicateKeyError:
            return await self.get(row['_id'])

    async def immutable(self, row):
        stored = await self.insert(row)
        if stored != row:
            raise QuestionnaireSourceConflict('Immutable Questionnaire source identity has conflicting content')
        return deepcopy(stored)

    async def get(self, identity):
        return await self.collection.find_one({'_id': identity})

    async def replace(self, expected, replacement, *, now):
        query = {'_id': expected['_id'], 'revision': expected['revision']}
        if expected.get('leaseOwner'):
            query.update(
                leaseOwner=expected['leaseOwner'],
                leaseGeneration=expected['leaseGeneration'],
                leaseExpiresAt={'$gt': now},
            )
        else:
            query['$or'] = [{'leaseOwner': None}, {'leaseExpiresAt': {'$lte': now}}]
        result = await self.collection.replace_one(query, deepcopy(replacement))
        if not result.acknowledged:
            raise RuntimeError('Questionnaire source CAS was not acknowledged')
        return bool(result.modified_count)

    async def claim(self, identity, *, now, owner):
        for _ in range(4):
            row = await self.get(identity)
            if row is None or row.get('recordKind') != 'task' or row['status'] in TERMINAL:
                return None
            if row['nextAttemptAt'] > now or row.get('leaseOwner') and row.get('leaseExpiresAt', 0) > now:
                return None
            replacement = {
                **deepcopy(row),
                'revision': row['revision'] + 1,
                'leaseOwner': owner,
                'leaseGeneration': row.get('leaseGeneration', 0) + 1,
                'leaseExpiresAt': now + row['policy']['lease_seconds'],
                'attempts': row['attempts'] + 1,
            }
            if await self.replace({**row, 'leaseOwner': None}, replacement, now=now):
                return replacement
        return None

    async def pending(self, *, now, limit):
        query = {
            'recordKind': 'task',
            'status': {'$nin': sorted(TERMINAL)},
            'nextAttemptAt': {'$lte': now},
            '$or': [{'leaseOwner': None}, {'leaseExpiresAt': {'$lte': now}}],
        }
        return [
            row['_id']
            for row in await self.collection.find(query)
            .sort([('nextAttemptAt', 1), ('_id', 1)])
            .limit(limit)
            .to_list(limit)
        ]

    async def scan_state(self):
        return await self.insert(
            {'_id': 'questionnaire_source_scan_v1', 'recordKind': 'scan', 'revision': 1, 'after': None}
        )

    async def advance_scan(self, previous, after):
        result = await self.collection.replace_one(
            {'_id': previous['_id'], 'revision': previous['revision']},
            {**previous, 'revision': previous['revision'] + 1, 'after': deepcopy(after)},
        )
        if not result.acknowledged:
            raise RuntimeError('Questionnaire source discovery cursor was not acknowledged')
        return bool(result.modified_count)

    async def record_discovery_error(self, source, code):
        await self.insert(
            {
                '_id': 'qsource_scan_error_' + source_digest(source),
                'recordKind': 'discovery_error',
                'source': deepcopy(source),
                'errorCode': code,
            }
        )

    async def put_blob(self, value):
        """Write fixed chunks first; only a complete immutable manifest makes them readable."""
        encoded = canonical_bytes(value)  # Freeze all caller-owned values before the first await.
        if len(encoded) > MAX_BLOB_BYTES:
            raise QuestionnaireSourceConflict('Questionnaire source material exceeds its storage budget')
        digest = hashlib.sha256(encoded).hexdigest()
        identity = 'questionnaire_blob_' + digest
        chunks = []
        for index, offset in enumerate(range(0, len(encoded), CHUNK_BYTES)):
            chunk = encoded[offset : offset + CHUNK_BYTES]
            chunk_digest = hashlib.sha256(chunk).hexdigest()
            chunks.append(chunk_digest)
            await self.immutable(
                {
                    '_id': identity + ':' + str(index),
                    'recordKind': 'material_chunk',
                    'blobId': identity,
                    'ordinal': index,
                    'contentDigest': chunk_digest,
                    'content': chunk,
                }
            )
        reference = {
            'contractVersion': 'questionnaire.source-blob.v1',
            'blobId': identity,
            'contentDigest': digest,
            'byteLength': len(encoded),
            'chunkSize': CHUNK_BYTES,
            'chunkDigests': chunks,
        }
        await self.immutable({'_id': identity, 'recordKind': 'material_manifest', 'reference': reference})
        return deepcopy(reference)

    async def read_blob(self, reference):
        reference = deepcopy(reference)
        if not isinstance(reference, dict) or reference.get('contractVersion') != 'questionnaire.source-blob.v1':
            raise QuestionnaireSourceConflict('Unsupported Questionnaire material manifest')
        length = reference.get('byteLength')
        if type(length) is not int or not 1 <= length <= MAX_BLOB_BYTES or reference.get('chunkSize') != CHUNK_BYTES:
            raise QuestionnaireSourceConflict('Invalid Questionnaire material bounds')
        chunks = reference.get('chunkDigests')
        if not isinstance(chunks, list) or len(chunks) != math.ceil(length / CHUNK_BYTES):
            raise QuestionnaireSourceConflict('Incomplete Questionnaire material chunk inventory')
        identity = reference.get('blobId')
        if identity != 'questionnaire_blob_' + reference.get('contentDigest', ''):
            raise QuestionnaireSourceConflict('Questionnaire material manifest identity differs')
        manifest = await self.get(identity)
        if (
            manifest is None
            or manifest.get('recordKind') != 'material_manifest'
            or manifest.get('reference') != reference
        ):
            raise LookupError('The exact complete Questionnaire material manifest is unavailable')
        result = bytearray()
        for index, digest in enumerate(chunks):
            row = await self.get(identity + ':' + str(index))
            if row is None:
                raise LookupError('An immutable Questionnaire material chunk is unavailable')
            chunk = row.get('content')
            if (
                row.get('recordKind') != 'material_chunk'
                or row.get('blobId') != identity
                or row.get('ordinal') != index
                or not isinstance(chunk, bytes)
                or len(chunk) != min(CHUNK_BYTES, length - index * CHUNK_BYTES)
                or row.get('contentDigest') != digest
                or hashlib.sha256(chunk).hexdigest() != digest
            ):
                raise QuestionnaireSourceConflict('Questionnaire material chunk identity or digest changed')
            result.extend(chunk)
        if hashlib.sha256(result).hexdigest() != reference['contentDigest']:
            raise QuestionnaireSourceConflict('Questionnaire complete material digest changed')
        return json.loads(result)
