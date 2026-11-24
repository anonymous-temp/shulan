"""Mongo adapter's acknowledged CAS protocol, without connecting to a database."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from pymongo.errors import DuplicateKeyError

from agents.persistence.tool_runs import MongoToolRunRepository


class Collection:
    def __init__(self):
        self.document = None
        self.acknowledged = True
        self.updates = []

    async def find_one(self, filter):
        if self.document and self.document['_id'] == filter['_id']:
            return deepcopy(self.document)

    async def insert_one(self, document):
        if self.document:
            raise DuplicateKeyError('duplicate')
        self.document = document
        return SimpleNamespace(acknowledged=self.acknowledged)

    async def update_one(self, filter, update):
        self.updates.append((filter, update))
        matches = self.document and all(self.document[key] == value for key, value in filter.items())
        if matches:
            self.document.update(update['$set'])
        return SimpleNamespace(acknowledged=self.acknowledged, matched_count=int(bool(matches)))


@pytest.mark.asyncio
async def test_unique_admission_compare_and_swap_and_no_id_update():
    collection = Collection()
    repository = MongoToolRunRepository(collection)
    document = {'_id': 'root', 'revision': 0, 'runs': {}}
    assert await repository.create(document)
    assert not await repository.create(document)
    assert await repository.compare_save('root', 0, {**document, 'revision': 1})
    assert not await repository.compare_save('root', 0, {**document, 'revision': 2})
    assert collection.updates[0][0] == {'_id': 'root', 'revision': 0}
    assert '_id' not in collection.updates[0][1]['$set']
    assert (await repository.load('root'))['revision'] == 1


@pytest.mark.asyncio
async def test_unacknowledged_writes_fail_closed():
    collection = Collection()
    collection.acknowledged = False
    repository = MongoToolRunRepository(collection)
    document = {'_id': 'root', 'revision': 0}
    with pytest.raises(RuntimeError, match='not acknowledged'):
        await repository.create(document)
    with pytest.raises(RuntimeError, match='not acknowledged'):
        await repository.compare_save('root', 0, {**document, 'revision': 1})
