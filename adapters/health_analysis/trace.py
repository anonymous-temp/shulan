"""Majority-confirmed, immutable Health Analysis run records; no automatic TTL."""

from copy import deepcopy

from pymongo import ReadPreference
from pymongo.errors import PyMongoError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from modules.health_analysis.contracts import TraceDeliveryError
from modules.health_analysis.contracts import TraceReceipt
from modules.health_analysis.trace import trace_hash


class MongoRunTraceWriter:
    def __init__(self, collection):
        self.collection = collection.with_options(
            read_preference=ReadPreference.PRIMARY,
            read_concern=ReadConcern('majority'),
            write_concern=WriteConcern(w='majority', j=True, wtimeout=4000),
        )

    async def ensure_indexes(self):
        await self.collection.create_index([('record.uuid', 1)], name='health_analysis_uuid')

    async def write(self, record):
        run_id = record.get('runId')
        if not isinstance(run_id, str) or not run_id:
            raise TraceDeliveryError('Analysis trace requires a run identity')
        digest = trace_hash(record)
        try:
            result = await self.collection.update_one(
                {'_id': run_id},
                {'$setOnInsert': {'record': deepcopy(record), 'contentHash': digest}},
                upsert=True,
            )
            if not result.acknowledged:
                raise TraceDeliveryError('Analysis trace write was not acknowledged')
            saved = await self.collection.find_one({'_id': run_id})
        except PyMongoError as error:
            raise TraceDeliveryError('Analysis trace store unavailable') from error
        if saved is None or saved.get('contentHash') != digest or trace_hash(saved.get('record')) != digest:
            raise TraceDeliveryError('Analysis trace is missing or conflicts with the saved run')
        return TraceReceipt(run_id, digest, True)
