"""Immutable Record admission records, separate from worker leases and Chat sessions."""

from copy import deepcopy

from pymongo import ReadPreference
from pymongo.errors import DuplicateKeyError
from pymongo.write_concern import WriteConcern

from modules.record.profile_publication import RecordSourceConflict
from modules.record.profile_source import content_digest


class MongoRecordAdmissionStore:
    def __init__(self, collection):
        self.collection = collection.with_options(
            read_preference=ReadPreference.PRIMARY,
            write_concern=WriteConcern(w='majority', j=True, wtimeout=5000),
        )

    async def get(self, identity):
        row = await self.collection.find_one({'_id': identity})
        return deepcopy(row) if row is not None else None

    async def put(self, row):
        # Only _id's existing uniqueness is needed; no startup DDL or TTL.
        try:
            await self.collection.insert_one(deepcopy(row))
        except DuplicateKeyError:
            pass
        stored = await self.get(row['_id'])
        if stored is None or stored.get('requestDigest') != row['requestDigest']:
            raise RecordSourceConflict('another fixed completion owns the Record admission fence')
        if content_digest(stored['request']) != stored['requestDigest']:
            raise RecordSourceConflict('stored Record admission request was changed')
        return stored
