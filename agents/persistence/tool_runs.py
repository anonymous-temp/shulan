"""CAS persistence for a bounded root run and its private child allocations."""

from collections.abc import Mapping
from copy import deepcopy
from typing import Protocol

from pymongo.errors import DuplicateKeyError


class ToolRunRepository(Protocol):
    async def load(self, key: str) -> dict | None: ...
    async def create(self, document: Mapping) -> bool: ...
    async def compare_save(self, key: str, revision: int, document: Mapping) -> bool: ...


class MongoToolRunRepository:
    """One Mongo document is the atomic budget/receipt boundary; no transactions needed."""

    def __init__(self, collection):
        self.collection = collection

    async def load(self, key):
        return await self.collection.find_one({'_id': key})

    async def create(self, document):
        try:
            result = await self.collection.insert_one(deepcopy(dict(document)))
        except DuplicateKeyError:
            return False
        if not result.acknowledged:
            raise RuntimeError('Tool run admission was not acknowledged')
        return True

    async def compare_save(self, key, revision, document):
        values = {name: deepcopy(value) for name, value in document.items() if name != '_id'}
        result = await self.collection.update_one({'_id': key, 'revision': revision}, {'$set': values})
        if not result.acknowledged:
            raise RuntimeError('Tool run update was not acknowledged')
        return result.matched_count == 1
