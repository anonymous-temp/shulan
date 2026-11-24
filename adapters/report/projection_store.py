"""Report-owned immutable projection cache with explicit input/version bindings."""

from copy import deepcopy

from pymongo.errors import DuplicateKeyError


class MongoReportProjectionStore:
    def __init__(self, collection):
        self.collection = collection

    async def get(self, key):
        return await self.collection.find_one({'_id': key}, {'_id': 0})

    async def put_once(self, key, value):
        try:
            inserted = await self.collection.insert_one({'_id': key, **deepcopy(value)})
            if not inserted.acknowledged:
                raise RuntimeError('Report projection cache write was not acknowledged')
            return deepcopy(value)
        except DuplicateKeyError:
            existing = await self.get(key)
            if existing != value:
                raise ValueError('One Report projection identity produced conflicting output')
            return existing
