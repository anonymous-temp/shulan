"""Shared synthetic fixture closure extracted from tests.unit.services.analysis.test_document_formal_source_store."""

from types import SimpleNamespace

from bson import BSON

from tests.unit._support.document_profile_composition.analysis.document_material_reader import BsonCollection
from tests.unit._support.document_profile_composition.analysis.fakes import MemoryMongoCursor


class Cursor(MemoryMongoCursor):
    def limit(self, size):
        self.rows = self.rows[:size]
        return self


class Collection(BsonCollection):
    def matches(self, row, query):
        rest = {}
        for key, expected in query.items():
            if isinstance(expected, dict) and '$nin' in expected:
                if self.value(row, key) in expected['$nin']:
                    return False
            else:
                rest[key] = expected
        return super().matches(row, rest)

    def find(self, query, projection=None):
        return Cursor([row for row in self.documents.values() if self.matches(row, query)])

    async def replace_one(self, query, value):
        existing = await self.find_one(query)
        if existing is None:
            return SimpleNamespace(acknowledged=True, modified_count=0)
        self.documents[existing['_id']] = BSON.encode(value).decode()
        return SimpleNamespace(acknowledged=True, modified_count=1)
