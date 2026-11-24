"""Shared synthetic fixture closure extracted from tests.unit.health_profile.test_source_admission."""

from copy import deepcopy
from types import SimpleNamespace

from bson import BSON
from pymongo.errors import DuplicateKeyError


def nested(row, path):
    for part in path.split('.'):
        if not isinstance(row, dict):
            return None
        row = row.get(part)
    return row


def matches(row, query):
    for key, value in query.items():
        if key == '$or':
            if not any(matches(row, branch) for branch in value):
                return False
            continue
        actual = nested(row, key)
        if isinstance(value, dict):
            if '$elemMatch' in value and not any(matches(item, value['$elemMatch']) for item in actual or []):
                return False
            if '$in' in value and actual not in value['$in']:
                return False
            if '$ne' in value and actual == value['$ne']:
                return False
        elif actual != value:
            return False
    return True


def project(row, projection):
    if projection is None:
        return deepcopy(row)
    result = {}
    for key, enabled in projection.items():
        if not enabled:
            continue
        value = nested(row, key)
        if value is None:
            continue
        target = result
        parts = key.split('.')
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = deepcopy(value)
    return result


class Cursor:
    def __init__(self, rows, owner):
        self.rows = rows
        self.owner = owner
        self.cap = None

    def sort(self, _):
        self.rows.sort(key=lambda row: str(row.get('_id')))
        return self

    def max_time_ms(self, value):
        self.owner.timeouts.append(value)
        return self

    def limit(self, value):
        self.cap = value
        return self

    async def to_list(self, length):
        return deepcopy(self.rows[: min(length, self.cap or length)])


class Collection:
    def __init__(self, rows=()):
        self.documents = deepcopy(list(rows))
        self.preferences = []
        self.timeouts = []
        self.queries = []
        self.writes = []
        self.fail = False
        self.after_read = None

    def with_options(self, *, read_preference):
        self.preferences.append(read_preference)
        return self

    def find(self, query, projection=None):
        if self.fail:
            raise RuntimeError('offline read failure')
        self.queries.append(deepcopy(query))
        rows = [project(row, projection) for row in self.documents if matches(row, query)]
        if self.after_read:
            self.after_read()
        return Cursor(rows, self)

    async def find_one(self, query, projection=None, **kwargs):
        self.timeouts.append(kwargs.get('max_time_ms'))
        rows = await self.find(query, projection).to_list(1)
        return rows[0] if rows else None

    async def create_index(self, fields, **kwargs):
        self.writes.append(('index', fields))

    async def insert_one(self, row):
        if any(item.get('_id') == row.get('_id') for item in self.documents):
            raise DuplicateKeyError('duplicate')
        self.documents.append(BSON(BSON.encode(row)).decode())
        self.writes.append(('insert', row['_id']))
        return SimpleNamespace(acknowledged=True)

    async def find_one_and_replace(self, query, row, **kwargs):
        for index, old in enumerate(self.documents):
            if matches(old, query):
                self.documents[index] = BSON(BSON.encode(row)).decode()
                self.writes.append(('replace', row['_id']))
                return deepcopy(self.documents[index])
        return None
