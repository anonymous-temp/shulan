"""Durable research input/evidence binding using a synthetic Mongo collection."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from pymongo.errors import DuplicateKeyError

from adapters.report.research_store import MongoResearchStore


class Collection:
    def __init__(self):
        self.documents = {}
        self.acknowledged = True

    async def insert_one(self, row):
        if row['_id'] in self.documents:
            raise DuplicateKeyError('synthetic duplicate')
        self.documents[row['_id']] = deepcopy(row)
        return SimpleNamespace(acknowledged=self.acknowledged)

    async def find_one(self, query):
        return deepcopy(self.documents.get(query['_id']))

    async def update_one(self, query, update):
        row = self.documents.get(query['_id'])
        path, value = next(iter(update['$set'].items()))
        call_key = path.removeprefix('calls.')
        assert query[path] == {'$exists': False}
        changed = row is not None and call_key not in row['calls']
        if changed:
            row['calls'][call_key] = deepcopy(value)
        return SimpleNamespace(acknowledged=self.acknowledged, modified_count=int(changed))


@pytest.mark.asyncio
async def test_frozen_deadline_version_and_idempotent_original_evidence():
    collection = Collection()
    store = MongoResearchStore(collection)
    plan = {'userId': 'user', 'manifest': {'revision': 'v1'}}
    assert (await store.admit('run', plan, 100))['deadlineAt'] == 100
    plan['manifest']['revision'] = 'v2'
    with pytest.raises(ValueError, match='another input'):
        await store.admit('run', plan, 200)
    plan['manifest']['revision'] = 'v1'
    assert (await store.admit('run', plan, 200))['deadlineAt'] == 100
    rows = [{'evidenceRef': 'original-1', 'topicId': 'topic-1', 'record': {'abstract': 'literal'}}]
    args = {'topicId': 'topic-1', 'query': 'actual query', 'source': 'literature'}
    await store.save_evidence('run', '$bad.path', args, rows)
    await store.save_evidence('run', '$bad.path', args, rows)
    assert await store.read_evidence('run') == rows
    assert (await store.read_calls('run'))[0]['query'] == 'actual query'
    with pytest.raises(ValueError, match='conflicts'):
        await store.save_evidence('run', '$bad.path', args, [])
    assert len(collection.documents['run']['calls']) == 1


@pytest.mark.asyncio
async def test_unacknowledged_evidence_is_not_delivered_to_model():
    collection = Collection()
    store = MongoResearchStore(collection)
    await store.admit('run', {'userId': 'u'}, 100)
    collection.acknowledged = False
    with pytest.raises(RuntimeError, match='durable'):
        await store.save_evidence('run', 'call', {'query': 'q'}, [])
