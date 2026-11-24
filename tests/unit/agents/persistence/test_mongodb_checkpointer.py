"""官方 MongoDBSaver 构造边界的无网络兼容测试。"""

from __future__ import annotations

import unittest

from agents.persistence import create_mongodb_checkpointer
from langgraph.checkpoint.mongodb import MongoDBSaver


class FakeCollection:
    def __init__(self) -> None:
        self.created_indexes: list[dict] = []

    def list_indexes(self):
        return [{"key": {"_id": 1}}]

    def create_index(self, keys, **kwargs):
        self.created_indexes.append({"keys": keys, **kwargs})


class FakeDatabase:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}

    def __getitem__(self, name: str) -> FakeCollection:
        return self.collections.setdefault(name, FakeCollection())


class FakeMongoClient:
    def __init__(self) -> None:
        self.databases: dict[str, FakeDatabase] = {}
        self.closed = False

    def __getitem__(self, name: str) -> FakeDatabase:
        return self.databases.setdefault(name, FakeDatabase())

    def close(self) -> None:
        self.closed = True


class MongoDBCheckpointerFactoryTest(unittest.TestCase):
    def test_creates_official_saver_with_isolated_collections(self) -> None:
        client = FakeMongoClient()

        saver = create_mongodb_checkpointer(
            client,
            database_name='shulan',
            checkpoint_collection_name='agent_checkpoints',
            writes_collection_name='agent_checkpoint_writes',
        )

        self.assertIsInstance(saver, MongoDBSaver)
        database = client.databases['shulan']
        self.assertIs(saver.db, database)
        self.assertIs(
            saver.checkpoint_collection,
            database.collections['agent_checkpoints'],
        )
        self.assertIs(
            saver.writes_collection,
            database.collections['agent_checkpoint_writes'],
        )
        self.assertEqual(
            database.collections['agent_checkpoints'].created_indexes[0],
            {
                'keys': [
                    ('thread_id', 1),
                    ('checkpoint_ns', 1),
                    ('checkpoint_id', -1),
                ],
                'unique': True,
            },
        )
        self.assertEqual(
            database.collections[
                'agent_checkpoint_writes'
            ].created_indexes[0]['keys'],
            [
                ('thread_id', 1),
                ('checkpoint_ns', 1),
                ('checkpoint_id', -1),
                ('task_id', 1),
                ('idx', 1),
            ],
        )

        saver.close()
        self.assertTrue(client.closed)


if __name__ == '__main__':
    unittest.main()
