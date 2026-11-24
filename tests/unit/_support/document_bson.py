"""BSON round-trip collection for fixed document archive tests."""

from copy import deepcopy
from datetime import datetime
from datetime import timezone

from bson import BSON

from tests.unit.services.analysis.fakes import MemoryMongoCollection


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def limit(self, limit):
        self.rows = self.rows[:limit]
        return self

    async def to_list(self, length):
        return deepcopy(self.rows[:length])


class BsonCollection(MemoryMongoCollection):
    def matches(self, item, filters):
        remaining = {}
        for key, value in filters.items():
            if isinstance(value, dict) and '$gte' in value:
                actual, expected = self.value(item, key), value['$gte']
                if isinstance(actual, datetime) and actual.tzinfo is None:
                    actual = actual.replace(tzinfo=timezone.utc)
                if actual is None or actual < expected:
                    return False
            else:
                remaining[key] = value
        return super().matches(item, remaining)

    def find(self, query, projection=None):
        return Cursor([row for row in self.documents.values() if self.matches(row, query)])

    async def insert_one(self, document):
        return await super().insert_one(BSON.encode(document).decode())

    async def update_one(self, filters, update):
        result = await super().update_one(filters, update)
        self.documents = {key: BSON.encode(value).decode() for key, value in self.documents.items()}
        return result


class LedgerBsonCollection(MemoryMongoCollection):
    async def insert_one(self, document):
        return await super().insert_one(BSON.encode(document).decode())

    async def update_one(self, query, update):
        return await super().update_one(BSON.encode(query).decode(), BSON.encode(update).decode())
