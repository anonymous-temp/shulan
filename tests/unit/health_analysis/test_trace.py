"""Persisted run identity and acknowledgement-loss behavior, without MongoDB."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from pymongo.errors import AutoReconnect

from adapters.health_analysis.trace import MongoRunTraceWriter
from modules.health_analysis.contracts import TraceDeliveryError
from modules.health_analysis.trace import trace_hash


class Collection:
    def __init__(self):
        self.rows = {}
        self.options = None
        self.acknowledged = True
        self.lose_ack = False
        self.missing_read = False

    def with_options(self, **options):
        self.options = options
        return self

    async def update_one(self, query, update, *, upsert):
        assert upsert is True
        self.rows.setdefault(query['_id'], deepcopy(update['$setOnInsert']))
        if self.lose_ack:
            self.lose_ack = False
            raise AutoReconnect('synthetic acknowledgement loss')
        return SimpleNamespace(acknowledged=self.acknowledged)

    async def find_one(self, query):
        return None if self.missing_read else deepcopy(self.rows.get(query['_id']))


@pytest.mark.asyncio
async def test_same_run_survives_new_writer_and_content_conflict_cannot_overwrite():
    collection = Collection()
    record = {'runId': 'test-run', 'uuid': 'test-http', 'result': {'healthReview': '合成评价'}}
    first = await MongoRunTraceWriter(collection).write(record)
    second = await MongoRunTraceWriter(collection).write(deepcopy(record))
    assert first == second
    assert first.durable and first.content_hash == trace_hash(record)
    changed = deepcopy(record)
    changed['result']['healthReview'] = '不同合成评价'
    with pytest.raises(TraceDeliveryError):
        await MongoRunTraceWriter(collection).write(changed)
    assert collection.rows['test-run']['record'] == record
    assert collection.options['write_concern'].document == {'j': True, 'wtimeout': 4000, 'w': 'majority'}
    assert collection.options['read_concern'].document == {'level': 'majority'}


@pytest.mark.asyncio
async def test_acknowledgement_loss_requires_retry_confirmation_and_retains_saved_record():
    collection = Collection()
    collection.lose_ack = True
    writer = MongoRunTraceWriter(collection)
    record = {'runId': 'test-run', 'uuid': 'test-http', 'result': {}}
    with pytest.raises(TraceDeliveryError):
        await writer.write(record)
    assert collection.rows['test-run']['record'] == record
    assert (await writer.write(record)).durable


@pytest.mark.asyncio
@pytest.mark.parametrize('flag', ['missing_read', 'unacknowledged'])
async def test_attempting_write_is_not_durable_confirmation(flag):
    collection = Collection()
    collection.missing_read = flag == 'missing_read'
    collection.acknowledged = flag != 'unacknowledged'
    with pytest.raises(TraceDeliveryError):
        await MongoRunTraceWriter(collection).write({'runId': 'test-run'})
