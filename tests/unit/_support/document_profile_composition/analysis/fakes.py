"""Shared synthetic fixture closure extracted from tests.unit.services.analysis.fakes."""

from copy import deepcopy
from types import SimpleNamespace


class InMemoryDocumentTraceStore:
    def __init__(self):
        self.documents = {}
        self.payloads = {}

    async def create(self, document):
        self.documents[document['_id']] = deepcopy(document)

    async def update(self, trace_id, user_id, fields):
        document = self.documents[trace_id]
        assert document['userId'] == user_id
        document.update(deepcopy(fields))

    async def write_payload(self, trace_id, user_id, name, payload):
        assert self.documents[trace_id]['userId'] == user_id
        self.payloads[(trace_id, name)] = deepcopy(payload)

    async def read_payload(self, trace_id, user_id, subject_id, name):
        document = self.documents[trace_id]
        if document['userId'] != user_id or document['subjectId'] != subject_id:
            raise LookupError('wrong owner')
        return deepcopy(self.payloads[(trace_id, name)])


class FakeDocumentCollection:
    def __init__(self):
        self.documents = {}
        self.indexes = []
        self.fail_insert = False

    async def create_index(self, fields, **options):
        self.indexes.append((fields, options))

    async def insert_one(self, document):
        if self.fail_insert:
            raise RuntimeError('storage unavailable')
        assert document['_id'] not in self.documents
        self.documents[document['_id']] = deepcopy(document)
        return SimpleNamespace(acknowledged=True)

    async def find_one(self, filters):
        for document in self.documents.values():
            if all(document.get(key) == value for key, value in filters.items()):
                return deepcopy(document)
        return None

    async def update_one(self, filters, update):
        found = await self.find_one(filters)
        if found is None:
            return SimpleNamespace(acknowledged=True, matched_count=0)
        document = self.documents[found['_id']]
        for path, value in update.get('$set', {}).items():
            target = document
            keys = path.split('.')
            for key in keys[:-1]:
                target = target.setdefault(key, {})
            target[keys[-1]] = deepcopy(value)
        for key, value in update.get('$push', {}).items():
            document.setdefault(key, []).append(deepcopy(value))
        for key, value in update.get('$addToSet', {}).items():
            values = document.setdefault(key, [])
            if value not in values:
                values.append(deepcopy(value))
        return SimpleNamespace(acknowledged=True, matched_count=1)


class MemoryMongoCollection(FakeDocumentCollection):
    """Atomic in-memory implementation of the predicates used by document stores."""

    @staticmethod
    def value(document, path):
        def read(current, keys):
            if not keys:
                return current
            if isinstance(current, list):
                values = [read(item, keys) for item in current]
                return [value for value in values if value is not None]
            if not isinstance(current, dict):
                return None
            return read(current.get(keys[0]), keys[1:])

        return read(document, path.split('.'))

    @staticmethod
    def has_field(document, path):
        for key in path.split('.'):
            if not isinstance(document, dict) or key not in document:
                return False
            document = document[key]
        return True

    def matches(self, item, filters):
        for key, expected in filters.items():
            if key == '$or':
                if not any(self.matches(item, branch) for branch in expected):
                    return False
                continue
            actual = self.value(item, key)
            if isinstance(expected, dict):
                for operator, value in expected.items():
                    if operator == '$exists' and self.has_field(item, key) != value:
                        return False
                    if operator == '$in' and actual not in value:
                        return False
                    if operator == '$gt' and (actual is None or actual <= value):
                        return False
                    if operator == '$lte' and (actual is None or actual > value):
                        return False
                    if operator == '$type' and value == 'string' and not isinstance(actual, str):
                        return False
            elif isinstance(actual, list):
                if (actual != expected) if isinstance(expected, list) else (expected not in actual):
                    return False
            elif actual != expected:
                return False
        return True

    async def find_one(self, filters):
        return next((deepcopy(row) for row in self.documents.values() if self.matches(row, filters)), None)

    def find(self, filters, projection=None):
        return MemoryMongoCursor([row for row in self.documents.values() if self.matches(row, filters)])

    async def insert_one(self, document):
        from pymongo.errors import DuplicateKeyError

        if document['_id'] in self.documents:
            raise DuplicateKeyError('duplicate id')
        for fields, options in self.indexes:
            partial = options.get('partialFilterExpression', {})
            if options.get('sparse') and not any(self.has_field(document, key) for key, _ in fields):
                continue
            if options.get('unique') and self.matches(document, partial):
                for row in self.documents.values():
                    if options.get('sparse') and not any(self.has_field(row, key) for key, _ in fields):
                        continue
                    if self.matches(row, partial) and all(
                        self.value(row, key) == self.value(document, key) for key, _ in fields
                    ):
                        raise DuplicateKeyError('duplicate index')
        return await super().insert_one(document)

    async def find_one_and_update(self, filters, update, **kwargs):
        found = await self.find_one(filters)
        if found is None:
            return None
        await self.update_one(filters, update)
        return await self.find_one({'_id': found['_id']})


class MemoryMongoCursor:
    def __init__(self, rows):
        self.rows = deepcopy(rows)

    def sort(self, fields):
        for key, direction in reversed(fields):
            self.rows.sort(key=lambda row: MemoryMongoCollection.value(row, key), reverse=direction < 0)
        return self

    def hint(self, index):
        self.index_hint = index
        return self

    async def to_list(self, length=None):
        return deepcopy(self.rows[:length])
