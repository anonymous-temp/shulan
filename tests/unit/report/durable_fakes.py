"""Small atomic Mongo test double with explicit query/update semantics.

Unknown operators fail instead of silently matching everything. No .env,
MongoDB, callback receiver or model is contacted by these tests.
"""

from copy import deepcopy
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

from pymongo.errors import DuplicateKeyError


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 14, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def _query_value(row, key):
    current = row
    for part in key.split('.'):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def matches(row, query):
    for key, expected in query.items():
        if key == '$or':
            if not any(matches(row, item) for item in expected):
                return False
            continue
        if key == '$nor':
            if any(matches(row, item) for item in expected):
                return False
            continue
        if key == '$and':
            if not all(matches(row, item) for item in expected):
                return False
            continue
        exists, actual = _query_value(row, key)
        if isinstance(expected, dict) and any(name.startswith('$') for name in expected):
            for operator, value in expected.items():
                if operator == '$exists':
                    okay = exists == value
                elif operator == '$type':
                    assert value == 'string'
                    okay = isinstance(actual, str)
                elif operator == '$ne':
                    okay = actual != value
                elif operator == '$in':
                    okay = actual in value
                elif operator in {'$lt', '$lte', '$gt', '$gte'}:
                    okay = actual is not None and {
                        '$lt': lambda: actual < value, '$lte': lambda: actual <= value,
                        '$gt': lambda: actual > value, '$gte': lambda: actual >= value,
                    }[operator]()
                else:
                    raise AssertionError(f'unsupported query operator {operator}')
                if not okay:
                    return False
        elif actual != expected:
            return False
    return True


def project(row, projection):
    if not projection:
        return deepcopy(row)
    if any(value for key, value in projection.items() if key != '_id'):
        return deepcopy({key: value for key, value in row.items()
                         if projection.get(key) or key == '_id' and projection.get('_id', 1)})
    return deepcopy({key: value for key, value in row.items() if projection.get(key, 1)})


class Cursor:
    def __init__(self, rows, projection=None):
        self.rows = deepcopy(rows)
        self.projection = projection

    def sort(self, key, direction=1):
        fields = key if isinstance(key, list) else [(key, direction)]
        for name, order in reversed(fields):
            self.rows.sort(key=lambda row: (row.get(name) is not None, row.get(name)), reverse=order < 0)
        return self

    def limit(self, count):
        self.rows = self.rows[:count]
        return self

    async def to_list(self, length=None):
        return [project(row, self.projection) for row in self.rows[:length]]


class Collection:
    def __init__(self):
        self.documents = {}
        self.indexes = []
        self.unavailable = False

    def _available(self):
        if self.unavailable:
            raise ConnectionError('simulated database outage')

    async def create_index(self, fields, **options):
        self._available()
        if (fields, options) not in self.indexes:
            self.indexes.append((deepcopy(fields), deepcopy(options)))
        return options['name']

    def _unique(self, document, exclude_id=None):
        if document['_id'] in self.documents and document['_id'] != exclude_id:
            raise DuplicateKeyError('duplicate id')
        for fields, options in self.indexes:
            partial = options.get('partialFilterExpression', {})
            if not options.get('unique') or not matches(document, partial):
                continue
            for row in self.documents.values():
                if row['_id'] != exclude_id and matches(row, partial) and all(
                    row.get(key) == document.get(key) for key, _ in fields
                ):
                    raise DuplicateKeyError('duplicate unique index')

    async def insert_one(self, document):
        self._available()
        self._unique(document)
        self.documents[document['_id']] = deepcopy(document)
        return SimpleNamespace(acknowledged=True, inserted_id=document['_id'])

    def find(self, query, projection=None):
        self._available()
        return Cursor([row for row in self.documents.values() if matches(row, query)], projection)

    async def find_one(self, query, projection=None, sort=None):
        cursor = self.find(query, projection)
        if sort:
            cursor.sort(sort)
        rows = await cursor.to_list(1)
        return rows[0] if rows else None

    @staticmethod
    def _apply(row, update, inserting=False):
        for operation, fields in update.items():
            if operation == '$setOnInsert' and not inserting:
                continue
            for key, value in fields.items():
                if operation in {'$set', '$setOnInsert'}:
                    row[key] = deepcopy(value)
                elif operation == '$unset':
                    row.pop(key, None)
                elif operation == '$inc':
                    row[key] = row.get(key, 0) + value
                elif operation == '$push':
                    row.setdefault(key, []).append(deepcopy(value))
                else:
                    raise AssertionError(f'unsupported update operator {operation}')

    def _update(self, query, update, upsert=False, sort=None):
        self._available()
        rows = Cursor([row for row in self.documents.values() if matches(row, query)])
        if sort:
            rows.sort(sort)
        if not rows.rows:
            if not upsert:
                return None, SimpleNamespace(acknowledged=True, matched_count=0, upserted_id=None)
            row = {key: deepcopy(value) for key, value in query.items() if not key.startswith('$')}
            self._apply(row, update, inserting=True)
            self._unique(row)
            self.documents[row['_id']] = row
            return deepcopy(row), SimpleNamespace(acknowledged=True, matched_count=0, upserted_id=row['_id'])
        row = deepcopy(rows.rows[0])
        self._apply(row, update)
        self._unique(row, exclude_id=row['_id'])
        self.documents[row['_id']] = row
        return deepcopy(row), SimpleNamespace(acknowledged=True, matched_count=1, upserted_id=None)

    async def update_one(self, query, update, upsert=False):
        return self._update(query, update, upsert=upsert)[1]

    async def update_many(self, query, update):
        self._available()
        count = 0
        for row in list(self.documents.values()):
            if matches(row, query):
                self._update({'_id': row['_id']}, update)
                count += 1
        return SimpleNamespace(acknowledged=True, matched_count=count)

    async def find_one_and_update(self, query, update, *, return_document=None, sort=None):
        # No await between matching and writing: models one atomic server operation.
        return self._update(query, update, sort=sort)[0]

    async def count_documents(self, query):
        self._available()
        return sum(matches(row, query) for row in self.documents.values())
