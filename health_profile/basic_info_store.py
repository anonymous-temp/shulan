"""Mongo single-document CAS for demographic governance and command recovery."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC
from datetime import datetime

from bson import BSON
from pymongo import ReadPreference
from pymongo import ReturnDocument
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import DuplicateKeyError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from .errors import HealthProfileConflictError


class BasicInfoConflict(HealthProfileConflictError):
    pass


class BasicInfoUnavailable(RuntimeError):
    pass


class MongoBasicInfoStore:
    """A dedicated collection; no TTL and no model/network calls.

    One bounded scope document atomically owns field epochs, immutable commands,
    source facts and published selections. Overflow fails explicitly rather than
    dropping pending commands or provenance. All writes require acknowledgement.
    """

    def __init__(self, collection, *, max_bytes=12_000_000):
        self.collection = (
            collection.with_options(
                read_preference=ReadPreference.PRIMARY,
                read_concern=ReadConcern('majority'),
                write_concern=WriteConcern(w='majority', j=True, wtimeout=5000),
            )
            if isinstance(collection, AsyncCollection)
            else collection
        )
        self.max_bytes = max_bytes

    async def get(self, key):
        return _aware(await self.collection.find_one({'_id': key}))

    async def find_mutation_scope(self, user_id, subject_id, mutation_key, operation_id):
        rows = (
            await self.collection.find(
                {
                    'recordKind': 'basic_info_scope',
                    'userId': user_id,
                    'subjectId': subject_id,
                    'mutations.' + mutation_key + '.operationId': operation_id,
                }
            )
            .sort([('lifecycleEpoch', 1)])
            .to_list(length=1)
        )
        return _aware(rows[0]) if rows else None

    async def create(self, document):
        self._bounded(document)
        try:
            result = await self.collection.insert_one(deepcopy(document))
            if not result.acknowledged:
                raise BasicInfoUnavailable('basic_info.unacknowledged')
            return deepcopy(document)
        except DuplicateKeyError:
            return await self.get(document['_id'])

    async def cas(self, before, after):
        self._bounded(after)
        after = deepcopy(after)
        after['revision'] = before['revision'] + 1
        if after.get('recordKind') == 'basic_info_scope':
            visible = ('selected', 'sources', 'fieldEpochs', 'blockedFields')
            after['dataRevision'] = before.get('dataRevision', 0) + int(
                any(before[key] != after[key] for key in visible)
            )
            after['pendingCount'] = sum(
                item['status'] in {'registered', 'pending'} for item in after['commands'].values()
            )
        return _aware(
            await self.collection.find_one_and_replace(
                {'_id': before['_id'], 'revision': before['revision']},
                after,
                return_document=ReturnDocument.AFTER,
            )
        )

    async def scan(self, *, user_id=None, after=None, limit=100):
        filters = {'recordKind': 'basic_info_scope', 'pendingCount': {'$gt': 0}}
        if user_id is not None:
            filters['userId'] = user_id
        if after is not None:
            filters['_id'] = {'$gte': after}
        return _aware(await self.collection.find(filters).sort([('_id', 1)]).to_list(length=limit))

    def _bounded(self, value):
        if len(BSON.encode(value)) > self.max_bytes:
            raise BasicInfoUnavailable('basic_info.capacity_exceeded')


def _bson_time(value):
    # Compare persisted instants at MongoDB UTC millisecond precision.
    if value is None:
        return None
    value = (value.replace(tzinfo=UTC) if value.tzinfo is None else value).astimezone(UTC)
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


def _aware(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    if isinstance(value, dict):
        return {key: _aware(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_aware(item) for item in value]
    return value
