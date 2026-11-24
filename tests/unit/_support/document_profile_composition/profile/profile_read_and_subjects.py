"""Shared synthetic fixture closure extracted from tests.unit.health_profile.test_profile_read_and_subjects."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError


def _nested_value(document, path):
    value = document
    for name in path.split('.'):
        if not isinstance(value, dict) or name not in value:
            return None
        value = value[name]
    return value


def _matches(document, filters):
    for name, expected in filters.items():
        if name == '$or':
            if not any(_matches(document, branch) for branch in expected):
                return False
            continue
        actual = _nested_value(document, name)
        if isinstance(expected, dict):
            if '$in' in expected and actual not in expected['$in']:
                return False
            if '$ne' in expected and actual == expected['$ne']:
                return False
            if '$lt' in expected and not actual < expected['$lt']:
                return False
            if '$gt' in expected and not actual > expected['$gt']:
                return False
            if '$gte' in expected and not actual >= expected['$gte']:
                return False
            if '$lte' in expected and not actual <= expected['$lte']:
                return False
        elif actual != expected:
            return False
    return True


def _set_nested(document, path, value):
    target = document
    names = path.split('.')
    for name in names[:-1]:
        target = target.setdefault(name, {})
    target[names[-1]] = deepcopy(value)


class FakeCursor:
    def __init__(self, documents):
        self.documents = [deepcopy(item) for item in documents]

    def sort(self, fields):
        for name, direction in reversed(fields):
            self.documents.sort(
                key=lambda item: (
                    _nested_value(item, name) is None,
                    _nested_value(item, name),
                ),
                reverse=direction < 0,
            )
        return self

    async def to_list(self, length=None):
        return deepcopy(self.documents if length is None else self.documents[:length])


class FakeCollection:
    def __init__(self):
        self.documents = []
        self.indexes = []

    async def create_index(self, fields, **options):
        self.indexes.append((deepcopy(fields), deepcopy(options)))
        return options['name']

    async def find_one(self, filters):
        for document in self.documents:
            if _matches(document, filters):
                return deepcopy(document)
        return None

    def find(self, filters):
        return FakeCursor([document for document in self.documents if _matches(document, filters)])

    async def insert_one(self, document):
        if any(item.get('_id') == document.get('_id') for item in self.documents):
            raise DuplicateKeyError('duplicate _id')
        creation_request = document.get('creationRequestId')
        if creation_request is not None and any(
            item.get('userId') == document.get('userId')
            and item.get('creationRequestId') == creation_request
            and item.get('creationRequestSlot') == document.get('creationRequestSlot')
            for item in self.documents
        ):
            raise DuplicateKeyError('duplicate creation request')
        self.documents.append(deepcopy(document))
        return SimpleNamespace(acknowledged=True)

    async def find_one_and_replace(
        self,
        filters,
        replacement,
        *,
        return_document,
    ):
        for index, document in enumerate(self.documents):
            if _matches(document, filters):
                updated = deepcopy(replacement)
                updated['_id'] = document['_id']
                self.documents[index] = updated
                result = document if return_document is ReturnDocument.BEFORE else updated
                return deepcopy(result)
        return None

    async def find_one_and_update(
        self,
        filters,
        update,
        *,
        return_document,
    ):
        for index, document in enumerate(self.documents):
            if not _matches(document, filters):
                continue
            updated = deepcopy(document)
            for name, value in update.get('$set', {}).items():
                _set_nested(updated, name, value)
            for name, value in update.get('$inc', {}).items():
                _set_nested(
                    updated,
                    name,
                    (_nested_value(updated, name) or 0) + value,
                )
            self.documents[index] = updated
            return deepcopy(updated)
        return None
