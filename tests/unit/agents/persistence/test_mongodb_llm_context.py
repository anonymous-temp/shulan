"""MongoLLMContextRepository 的无网络乐观并发测试。"""

from __future__ import annotations

import unittest
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from agents.errors import LLMContextConflictError
from agents.errors import LLMContextStorageError
from agents.persistence import MongoLLMContextRepository
from pymongo.errors import DuplicateKeyError


class FakeAsyncMongoCollection:
    """仅模拟 Repository 使用的三个异步 collection 方法。"""

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.find_calls: list[dict[str, Any]] = []
        self.insert_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []

    async def find_one(self, filter):
        copied_filter = deepcopy(dict(filter))
        self.find_calls.append(copied_filter)
        document = self.documents.get(copied_filter['_id'])
        return None if document is None else deepcopy(document)

    async def insert_one(self, document):
        copied = deepcopy(dict(document))
        self.insert_calls.append(copied)
        key = copied['_id']
        if key in self.documents:
            raise DuplicateKeyError('duplicate context key')
        self.documents[key] = copied
        return SimpleNamespace(inserted_id=key)

    async def update_one(self, filter, update):
        copied_filter = deepcopy(dict(filter))
        copied_update = deepcopy(dict(update))
        self.update_calls.append({
            'filter': copied_filter,
            'update': copied_update,
        })
        document = self.documents.get(copied_filter['_id'])
        if (
                document is None
                or document['revision'] != copied_filter['revision']
        ):
            return SimpleNamespace(matched_count=0)

        document.update(copied_update['$set'])
        document['revision'] += copied_update['$inc']['revision']
        return SimpleNamespace(matched_count=1)


class MongoLLMContextRepositoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.collection = FakeAsyncMongoCollection()
        self.repository = MongoLLMContextRepository(self.collection)

    async def test_load_returns_none_for_unknown_key(self) -> None:
        snapshot = await self.repository.load('context-1')

        self.assertIsNone(snapshot)
        self.assertEqual(self.collection.find_calls, [{'_id': 'context-1'}])

    async def test_first_save_inserts_revision_and_timestamps(self) -> None:
        payload = {'format': 'aion.chat.context', 'version': 1, 'turns': []}

        snapshot = await self.repository.save(
            'context-1',
            payload,
            expected_revision=0,
        )

        document = self.collection.documents['context-1']
        self.assertEqual(snapshot.revision, 1)
        self.assertEqual(snapshot.payload, payload)
        self.assertEqual(document['_id'], 'context-1')
        self.assertEqual(document['revision'], 1)
        self.assertEqual(document['payload'], payload)
        self.assertEqual(document['createdAt'], document['updatedAt'])
        self.assertIsNotNone(document['createdAt'].tzinfo)

    async def test_load_and_result_do_not_expose_stored_payload(self) -> None:
        saved = await self.repository.save(
            'context-1',
            {'turns': [{'content': '第一问'}]},
            expected_revision=0,
        )
        loaded = await self.repository.load('context-1')

        saved.payload['turns'][0]['content'] = '修改返回值'
        loaded.payload['turns'][0]['content'] = '修改加载值'

        self.assertEqual(
            self.collection.documents['context-1']['payload']['turns'][0][
                'content'
            ],
            '第一问',
        )

    async def test_update_matches_expected_revision_and_increments_it(self) -> None:
        first = await self.repository.save(
            'context-1',
            {'turns': []},
            expected_revision=0,
        )
        created_at = self.collection.documents['context-1']['createdAt']

        second = await self.repository.save(
            'context-1',
            {'turns': [{'content': '第二轮'}]},
            expected_revision=first.revision,
        )

        call = self.collection.update_calls[0]
        document = self.collection.documents['context-1']
        self.assertEqual(
            call['filter'],
            {'_id': 'context-1', 'revision': 1},
        )
        self.assertEqual(call['update']['$inc'], {'revision': 1})
        self.assertNotIn('createdAt', call['update']['$set'])
        self.assertEqual(second.revision, 2)
        self.assertEqual(document['revision'], 2)
        self.assertEqual(document['createdAt'], created_at)

    async def test_first_save_conflicts_with_existing_document(self) -> None:
        await self.repository.save(
            'context-1',
            {'turns': []},
            expected_revision=0,
        )

        with self.assertRaises(LLMContextConflictError):
            await self.repository.save(
                'context-1',
                {'turns': []},
                expected_revision=0,
            )

    async def test_update_rejects_stale_or_missing_revision(self) -> None:
        await self.repository.save(
            'context-1',
            {'turns': []},
            expected_revision=0,
        )

        with self.assertRaises(LLMContextConflictError):
            await self.repository.save(
                'context-1',
                {'turns': []},
                expected_revision=2,
            )

    async def test_load_rejects_invalid_stored_document(self) -> None:
        self.collection.documents['context-1'] = {
            '_id': 'context-1',
            'revision': 0,
            'payload': {},
        }

        with self.assertRaisesRegex(LLMContextStorageError, 'is invalid'):
            await self.repository.load('context-1')

    async def test_save_validates_input_before_accessing_mongodb(self) -> None:
        with self.assertRaisesRegex(ValueError, 'key cannot be empty'):
            await self.repository.save(' ', {}, expected_revision=0)
        with self.assertRaisesRegex(TypeError, 'payload must be a mapping'):
            await self.repository.save('context-1', [], expected_revision=0)
        with self.assertRaisesRegex(
                TypeError,
                'expected_revision must be an integer',
        ):
            await self.repository.save(
                'context-1',
                {},
                expected_revision=True,
            )

        self.assertEqual(self.collection.insert_calls, [])
        self.assertEqual(self.collection.update_calls, [])


if __name__ == '__main__':
    unittest.main()
