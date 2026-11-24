"""Document-owned durable formal commands; PRIMARY reads and fenced CAS updates."""

from copy import deepcopy

from pymongo import ReadPreference
from pymongo.errors import DuplicateKeyError
from pymongo.write_concern import WriteConcern

from modules.analysis.formal_source_contracts import FORMAL_TERMINAL


class MongoDocumentFormalSourceStore:
    def __init__(self, collection):
        self.collection = collection.with_options(
            read_preference=ReadPreference.PRIMARY,
            write_concern=WriteConcern(w='majority', j=True, wtimeout=5000),
        )

    async def insert(self, row):
        try:
            result = await self.collection.insert_one(deepcopy(row))
            if not result.acknowledged:
                raise RuntimeError('formal Document acceptance was not acknowledged')
            return deepcopy(row)
        except DuplicateKeyError:
            return await self.get(row['_id'])

    async def get(self, command_id):
        return await self.collection.find_one({'_id': command_id})

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
            raise RuntimeError('formal Document progress was not acknowledged')
        return bool(result.modified_count)

    async def claim(self, command_id, *, owner, now):
        for _ in range(4):
            row = await self.get(command_id)
            if row is None or row['status'] in FORMAL_TERMINAL or row['nextAttemptAt'] > now:
                return None
            if row.get('leaseOwner') and row.get('leaseExpiresAt', 0) > now:
                return None
            replacement = {
                **deepcopy(row),
                'revision': row['revision'] + 1,
                'leaseOwner': owner,
                'leaseGeneration': row.get('leaseGeneration', 0) + 1,
                'leaseExpiresAt': now + row['policy']['lease_seconds'],
            }
            if await self.replace({**row, 'leaseOwner': None}, replacement, now=now):
                return replacement
        return None

    async def pending(self, *, now, limit):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('a bounded formal command scan is required')
        query = {
            'status': {'$nin': sorted(FORMAL_TERMINAL)},
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
