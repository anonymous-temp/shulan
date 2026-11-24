"""Agent 生产运行时容器的无网络组装与生命周期测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from agents import AgentRuntimeSettings
from agents import create_agent_runtime
from agents.errors import AgentRuntimeClosedError
from agents.knowledge import EviMedKnowledgeGateway
from agents.llm import AionLLMGateway
from agents.persistence import MongoLLMContextRepository
from agents.persistence import RedisAgentLeaseManager
from langgraph.checkpoint.memory import InMemorySaver


class FakeContextCollection:
    async def find_one(self, filter):
        return None

    async def insert_one(self, document):
        return SimpleNamespace(inserted_id=document['_id'])

    async def update_one(self, filter, update):
        return SimpleNamespace(matched_count=1)


class FakeRedis:
    async def set(self, name, value, *, ex, nx):
        return True

    async def eval(self, script, numkeys, *keys_and_args):
        return 1


class FakeMongoClient:
    def __init__(self, uri: str) -> None:
        self.uri = uri
        self.closed = False

    def close(self) -> None:
        self.closed = True


class ClosingMemorySaver(InMemorySaver):
    def __init__(self, client, **kwargs) -> None:
        super().__init__()
        self.client = client
        self.kwargs = kwargs
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.client.close()


class RecordingBuilder:
    def __init__(self) -> None:
        self.checkpointer = None
        self.graph = SimpleNamespace(ainvoke=None, astream=None)

    def compile(self, *, checkpointer):
        self.checkpointer = checkpointer
        return self.graph


class AgentRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.created_clients: list[FakeMongoClient] = []
        self.created_savers: list[ClosingMemorySaver] = []

        def client_factory(uri: str) -> FakeMongoClient:
            client = FakeMongoClient(uri)
            self.created_clients.append(client)
            return client

        def checkpointer_factory(client, **kwargs) -> ClosingMemorySaver:
            saver = ClosingMemorySaver(client, **kwargs)
            self.created_savers.append(saver)
            return saver

        self.runtime = create_agent_runtime(
            AgentRuntimeSettings(
                provider=' deepseek ',
                api_key=' secret-key ',
                default_model=' model-1 ',
                mongodb_uri=' mongodb://example ',
                mongodb_database=' shulan ',
                checkpoint_collection=' checkpoints ',
                checkpoint_writes_collection=' checkpoint_writes ',
                lease_ttl_seconds=120,
            ),
            context_collection=FakeContextCollection(),
            redis_client=FakeRedis(),
            mongo_client_factory=client_factory,
            checkpointer_factory=checkpointer_factory,
        )

    def tearDown(self) -> None:
        self.runtime.close()

    def test_assembles_all_runtime_dependencies_once(self) -> None:
        self.assertIsInstance(self.runtime.llm_gateway, AionLLMGateway)
        self.assertIsInstance(
            self.runtime.context_repository,
            MongoLLMContextRepository,
        )
        self.assertIsInstance(
            self.runtime.lease_manager,
            RedisAgentLeaseManager,
        )
        self.assertIsInstance(
            self.runtime.knowledge_gateway,
            EviMedKnowledgeGateway,
        )
        self.assertFalse(self.runtime.knowledge_gateway.enabled)
        self.assertIs(self.runtime.hub._registry, self.runtime.registry)
        self.assertEqual(len(self.created_clients), 1)
        self.assertEqual(self.created_clients[0].uri, 'mongodb://example')
        self.assertEqual(self.created_savers[0].kwargs, {
            'database_name': 'shulan',
            'checkpoint_collection_name': 'checkpoints',
            'writes_collection_name': 'checkpoint_writes',
        })

    def test_register_graph_always_uses_runtime_checkpointer(self) -> None:
        builder = RecordingBuilder()

        registration = self.runtime.register_graph(
            'chat.reply',
            builder,
            version='chat-v1',
        )

        self.assertIs(builder.checkpointer, self.runtime.checkpointer)
        self.assertIs(registration.graph, builder.graph)
        self.assertIs(
            self.runtime.registry.get('chat.reply'),
            registration,
        )

    def test_stateless_graph_can_disable_runtime_checkpointer(self) -> None:
        builder = RecordingBuilder()

        self.runtime.register_graph(
            'record.plan',
            builder,
            version='record-plan-v1',
            checkpointed=False,
        )

        self.assertIsNone(builder.checkpointer)

    def test_register_graph_validates_checkpointed_flag(self) -> None:
        with self.assertRaisesRegex(TypeError, 'must be a boolean'):
            self.runtime.register_graph(
                'record.plan',
                RecordingBuilder(),
                version='record-plan-v1',
                checkpointed='no',
            )

    def test_close_is_idempotent_and_prevents_new_graphs(self) -> None:
        self.runtime.close()
        self.runtime.close()

        self.assertTrue(self.runtime.closed)
        self.assertTrue(self.created_savers[0].closed)
        self.assertTrue(self.created_clients[0].closed)
        with self.assertRaises(AgentRuntimeClosedError):
            self.runtime.register_graph(
                'chat.reply',
                RecordingBuilder(),
                version='chat-v1',
            )

    def test_settings_repr_does_not_expose_secrets(self) -> None:
        settings = AgentRuntimeSettings(
            provider='deepseek',
            api_key='visible-api-secret',
            mongodb_uri='mongodb://visible-db-secret',
            mongodb_database='shulan',
            knowledge_api_key='visible-knowledge-secret',
        )

        rendered = repr(settings)
        self.assertNotIn('visible-api-secret', rendered)
        self.assertNotIn('visible-db-secret', rendered)
        self.assertNotIn('visible-knowledge-secret', rendered)


class AgentRuntimeFailureTest(unittest.TestCase):
    def test_closes_mongo_client_when_checkpointer_creation_fails(self) -> None:
        client = FakeMongoClient('mongodb://example')

        with self.assertRaisesRegex(RuntimeError, 'index creation failed'):
            create_agent_runtime(
                AgentRuntimeSettings(
                    provider='deepseek',
                    api_key='secret',
                    mongodb_uri='mongodb://example',
                    mongodb_database='shulan',
                ),
                context_collection=FakeContextCollection(),
                redis_client=FakeRedis(),
                mongo_client_factory=lambda uri: client,
                checkpointer_factory=lambda client, **kwargs: (_ for _ in ()).throw(
                    RuntimeError('index creation failed'),
                ),
            )

        self.assertTrue(client.closed)


if __name__ == '__main__':
    unittest.main()
