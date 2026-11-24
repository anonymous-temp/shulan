"""AionLLMGateway 的无网络适配与 Context 事务测试。"""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents import GraphRegistry
from agents.errors import AgentConfigurationError
from agents.errors import AionGatewayError
from agents.errors import LLMContextConflictError
from agents.graphs.validation import build_validation_graph
from agents.llm import AionLLMGateway
from agents.llm import ContextPolicy
from agents.llm import LLMRequest
from agents.llm import ModelProfile
from tests.unit._support.agents import InMemoryLLMContextRepository


def _empty_context(system_content: str | None = None) -> dict[str, Any]:
    return {
        'format': 'aion.chat.context',
        'version': 1,
        'system': (
            None
            if system_content is None
            else {'role': 'system', 'content': system_content}
        ),
        'turns': [],
    }


class MockAionClient:
    """模拟 AION Client 的公开方法，不模拟 OpenAI SDK。"""

    provider = 'deepseek'

    def __init__(
            self,
            *,
            model: str,
            system_content: str | None,
            context: bool,
            complete_result: Any = None,
            stream_chunks: tuple[Any, ...] = (),
    ) -> None:
        self.model = model
        self.context_enabled = context
        self.complete_result = complete_result or SimpleNamespace(
            content='mock reply',
            reasoning_content=None,
            usage=None,
        )
        self.stream_chunks = stream_chunks
        self.context_payload = _empty_context(system_content)
        self.imported_payloads: list[dict[str, Any]] = []
        self.complete_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def complete_async(self, content: str, *, reasoning_effort=None, **kwargs: Any) -> Any:
        if reasoning_effort is not None:
            kwargs['reasoning_effort'] = reasoning_effort
        self.complete_calls.append({'content': content, **kwargs})
        if isinstance(self.complete_result, Exception):
            raise self.complete_result
        if self.context_enabled and self.complete_result.content is not None:
            self.context_payload['turns'].append({
                'user': {'role': 'user', 'content': content},
                'assistant': {
                    'role': 'assistant',
                    'content': self.complete_result.content,
                },
            })
        return self.complete_result

    async def stream_async(
            self,
            content: str,
            reasoning_effort=None,
            **kwargs: Any,
    ) -> AsyncIterator[Any]:
        if reasoning_effort is not None:
            kwargs['reasoning_effort'] = reasoning_effort
        self.stream_calls.append({'content': content, **kwargs})
        response_parts: list[str] = []
        for chunk in self.stream_chunks:
            if isinstance(chunk, Exception):
                raise chunk
            if chunk.content is not None:
                response_parts.append(chunk.content)
            yield chunk
        if self.context_enabled and response_parts:
            self.context_payload['turns'].append({
                'user': {'role': 'user', 'content': content},
                'assistant': {
                    'role': 'assistant',
                    'content': ''.join(response_parts),
                },
            })

    def import_context(self, data) -> None:
        copied = deepcopy(dict(data))
        self.imported_payloads.append(copied)
        self.context_payload = copied

    def export_context(self) -> dict[str, Any]:
        return deepcopy(self.context_payload)


class RecordingAionFactory:
    def __init__(self, clients: list[MockAionClient]) -> None:
        self.clients = list(clients)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, provider: str, **kwargs: Any) -> MockAionClient:
        self.calls.append({'provider': provider, **kwargs})
        client = self.clients.pop(0)
        client.model = kwargs.get('model', client.model)
        return client


class AionLLMGatewayTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.stateless_profile = ModelProfile(
            name='validation.stateless',
            context_policy=ContextPolicy.STATELESS,
            prompt_name='validation/system.txt',
            model='profile-model',
            thinking=True,
        )
        self.stateful_profile = ModelProfile(
            name='validation.stateful',
            context_policy=ContextPolicy.AION_FULL,
            prompt_name='validation/system.txt',
            model='profile-model',
        )

    @staticmethod
    def _client(
            *,
            context: bool,
            result: Any = None,
            chunks: tuple[Any, ...] = (),
    ) -> MockAionClient:
        return MockAionClient(
            model='initial-model',
            system_content='渲染后的系统提示',
            context=context,
            complete_result=result,
            stream_chunks=chunks,
        )

    def _gateway(
            self,
            factory: RecordingAionFactory,
            *,
            repository=None,
    ) -> AionLLMGateway:
        return AionLLMGateway(
            provider=' deepseek ',
            api_key=' test-key ',
            default_model='default-model',
            context_repository=repository,
            client_factory=factory,
            prompt_renderer=lambda name, **variables: '渲染后的系统提示',
        )

    async def test_complete_maps_profile_result_usage_and_factory_args(self) -> None:
        result = SimpleNamespace(
            content='完整回答',
            reasoning_content='完整推理',
            usage={
                'prompt_tokens': 12,
                'completion_tokens': 8,
                'total_tokens': 20,
                'prompt_cache_hit_tokens': 5,
            },
        )
        client = self._client(context=False, result=result)
        factory = RecordingAionFactory([client])
        gateway = self._gateway(factory)

        normalized = await gateway.complete(LLMRequest(
            profile=self.stateless_profile,
            content='问题',
            thinking=False,
        ))

        self.assertEqual(normalized.content, '完整回答')
        self.assertEqual(normalized.reasoning_content, '完整推理')
        self.assertEqual(normalized.provider, 'deepseek')
        self.assertEqual(normalized.model, 'profile-model')
        self.assertEqual(normalized.usage.total_tokens, 20)
        self.assertEqual(
            normalized.usage.raw['prompt_cache_hit_tokens'],
            5,
        )
        self.assertEqual(factory.calls, [{
            'provider': 'deepseek',
            'api_key': 'test-key',
            'context': False,
            'system_content': '渲染后的系统提示',
            'model': 'profile-model',
        }])
        self.assertEqual(client.complete_calls, [{
            'content': '问题',
            'thinking': False,
        }])

    async def test_profile_forwards_deepseek_model_kwargs(self) -> None:
        rendered = []
        profile = ModelProfile(
            name='record.question',
            context_policy=ContextPolicy.STATELESS,
            prompt_name='record/question.txt',
            prompt_variables={'topics_text': '睡眠', 'rules_text': '避免重复'},
            model_kwargs={'reasoning_effort': 'high'},
            thinking=True,
        )
        client = self._client(context=False)
        factory = RecordingAionFactory([client])
        gateway = AionLLMGateway(
            provider='deepseek',
            api_key='test-key',
            default_model='default-model',
            client_factory=factory,
            prompt_renderer=lambda name, **variables: rendered.append(
                (name, variables),
            ) or '动态系统提示',
        )

        await gateway.complete(LLMRequest(
            profile=profile,
            content='生成下一题',
        ))

        self.assertEqual(rendered, [(
            'record/question.txt',
            {'topics_text': '睡眠', 'rules_text': '避免重复'},
        )])
        self.assertEqual(factory.calls[0]['system_content'], '动态系统提示')
        self.assertEqual(client.complete_calls, [{
            'content': '生成下一题',
            'thinking': True,
            'reasoning_effort': 'high',
        }])

    async def test_stream_maps_chunks_and_requests_usage(self) -> None:
        chunks = (
            SimpleNamespace(content=None, reasoning_content='推理', usage=None),
            SimpleNamespace(content='回答', reasoning_content=None, usage=None),
            SimpleNamespace(
                content=None,
                reasoning_content=None,
                usage={'total_tokens': 9},
            ),
        )
        client = self._client(context=False, chunks=chunks)
        factory = RecordingAionFactory([client])
        gateway = self._gateway(factory)

        normalized = [
            chunk
            async for chunk in gateway.stream(
                LLMRequest(profile=self.stateless_profile, content='流式问题'),
            )
        ]

        self.assertEqual(normalized[0].reasoning_content, '推理')
        self.assertEqual(normalized[1].content, '回答')
        self.assertEqual(normalized[2].usage.total_tokens, 9)
        self.assertEqual(client.stream_calls, [{
            'content': '流式问题',
            'thinking': True,
            'reasoning_effort': 'low',
            'usage': True,
        }])

    async def test_stream_forwards_deepseek_model_kwargs(self) -> None:
        profile = ModelProfile(
            name='record.question',
            context_policy=ContextPolicy.STATELESS,
            model_kwargs={'reasoning_effort': 'high'},
            thinking=True,
        )
        client = self._client(context=False)
        gateway = self._gateway(RecordingAionFactory([client]))

        chunks = [
            chunk
            async for chunk in gateway.stream(LLMRequest(
                profile=profile,
                content='生成下一题',
            ))
        ]

        self.assertEqual(chunks, [])
        self.assertEqual(client.stream_calls, [{
            'content': '生成下一题',
            'thinking': True,
            'usage': True,
            'reasoning_effort': 'high',
        }])

    async def test_stateful_complete_restores_and_persists_context(self) -> None:
        repository = InMemoryLLMContextRepository()
        first_client = self._client(context=True)
        second_client = self._client(context=True)
        factory = RecordingAionFactory([first_client, second_client])
        gateway = self._gateway(factory, repository=repository)
        request_one = LLMRequest(
            profile=self.stateful_profile,
            content='第一问',
            conversation_key='conversation-1',
        )
        request_two = LLMRequest(
            profile=self.stateful_profile,
            content='第二问',
            conversation_key='conversation-1',
        )

        await gateway.complete(request_one)
        await gateway.complete(request_two)

        repository_key = 'validation.stateful:conversation-1'
        snapshot = await repository.load(repository_key)
        self.assertEqual(snapshot.revision, 2)
        self.assertEqual(len(snapshot.payload['turns']), 2)
        self.assertEqual(len(second_client.imported_payloads), 1)
        self.assertEqual(
            second_client.imported_payloads[0]['turns'][0]['user']['content'],
            '第一问',
        )
        self.assertTrue(factory.calls[0]['context'])
        self.assertTrue(factory.calls[1]['context'])

    async def test_supplemental_context_is_sent_but_not_archived(self) -> None:
        repository = InMemoryLLMContextRepository()
        client = self._client(context=True)
        gateway = self._gateway(
            RecordingAionFactory([client]),
            repository=repository,
        )

        await gateway.complete(LLMRequest(
            profile=self.stateful_profile,
            content='原始用户问题',
            supplemental_context='[资料1] 只在本轮使用的指南摘要',
            conversation_key='conversation-1',
        ))

        self.assertIn(
            '[资料1] 只在本轮使用的指南摘要',
            client.complete_calls[0]['content'],
        )
        snapshot = await repository.load(
            'validation.stateful:conversation-1',
        )
        self.assertEqual(
            snapshot.payload['turns'][0]['user']['content'],
            '原始用户问题',
        )
        self.assertNotIn(
            '指南摘要',
            snapshot.payload['turns'][0]['user']['content'],
        )

    async def test_different_profiles_have_isolated_context_keys(self) -> None:
        repository = InMemoryLLMContextRepository()
        first_profile = self.stateful_profile
        second_profile = ModelProfile(
            name='validation.other',
            context_policy=ContextPolicy.AION_FULL,
        )
        factory = RecordingAionFactory([
            self._client(context=True),
            self._client(context=True),
        ])
        gateway = self._gateway(factory, repository=repository)

        await gateway.complete(LLMRequest(
            profile=first_profile,
            content='第一能力',
            conversation_key='same-conversation',
        ))
        await gateway.complete(LLMRequest(
            profile=second_profile,
            content='第二能力',
            conversation_key='same-conversation',
        ))

        first = await repository.load(
            'validation.stateful:same-conversation',
        )
        second = await repository.load('validation.other:same-conversation')
        self.assertEqual(first.payload['turns'][0]['user']['content'], '第一能力')
        self.assertEqual(second.payload['turns'][0]['user']['content'], '第二能力')

    async def test_stateful_request_requires_repository(self) -> None:
        factory = RecordingAionFactory([self._client(context=True)])
        gateway = self._gateway(factory)

        with self.assertRaisesRegex(
                AgentConfigurationError,
                'requires an LLMContextRepository',
        ):
            await gateway.complete(LLMRequest(
                profile=self.stateful_profile,
                content='问题',
                conversation_key='conversation-1',
            ))

    async def test_unchanged_context_does_not_increment_revision(self) -> None:
        repository = InMemoryLLMContextRepository()
        result = SimpleNamespace(
            content=None,
            reasoning_content='只有推理',
            usage=None,
        )
        client = self._client(context=True, result=result)
        factory = RecordingAionFactory([client])
        gateway = self._gateway(factory, repository=repository)

        with self.assertRaisesRegex(AionGatewayError, 'string content'):
            await gateway.complete(LLMRequest(
                profile=self.stateful_profile,
                content='问题',
                conversation_key='conversation-1',
            ))

        snapshot = await repository.load(
            'validation.stateful:conversation-1',
        )
        self.assertIsNone(snapshot)

    async def test_interrupted_stream_does_not_persist_context(self) -> None:
        repository = InMemoryLLMContextRepository()
        chunks = (
            SimpleNamespace(content='部分', reasoning_content=None, usage=None),
            SimpleNamespace(content='未消费', reasoning_content=None, usage=None),
        )
        client = self._client(context=True, chunks=chunks)
        factory = RecordingAionFactory([client])
        gateway = self._gateway(factory, repository=repository)
        request = LLMRequest(
            profile=self.stateful_profile,
            content='问题',
            conversation_key='conversation-1',
        )

        stream = gateway.stream(request)
        first = await anext(stream)
        await stream.aclose()

        self.assertEqual(first.content, '部分')
        snapshot = await repository.load(
            'validation.stateful:conversation-1',
        )
        self.assertIsNone(snapshot)

    async def test_repository_rejects_stale_revision(self) -> None:
        repository = InMemoryLLMContextRepository()
        await repository.save('context-1', _empty_context(), expected_revision=0)

        with self.assertRaises(LLMContextConflictError):
            await repository.save(
                'context-1',
                _empty_context(),
                expected_revision=0,
            )

    async def test_aion_failures_keep_original_cause(self) -> None:
        client = self._client(
            context=False,
            result=RuntimeError('provider failed'),
        )
        gateway = self._gateway(RecordingAionFactory([client]))

        with self.assertRaisesRegex(AionGatewayError, 'complete_async') as raised:
            await gateway.complete(LLMRequest(
                profile=self.stateless_profile,
                content='问题',
            ))
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)

    async def test_gateway_runs_through_agent_hub_validation_graph(self) -> None:
        result = SimpleNamespace(
            content='Graph 中的 AION 回答',
            reasoning_content='Graph 中的推理',
            usage={'total_tokens': 6},
        )
        factory = RecordingAionFactory([
            self._client(context=False, result=result),
        ])
        gateway = self._gateway(factory)
        registry = GraphRegistry()
        registry.register(
            'validation.aion',
            build_validation_graph(
                gateway=gateway,
                profile=self.stateless_profile,
            ),
            version='validation-aion-v1',
        )
        hub = AgentHub(registry)
        context = AgentRunContext.create(
            user_id='user-1',
            session_id='session-1',
            request_id='request-1',
            run_id='run-1',
        )

        response = await hub.invoke(
            'validation.aion',
            AgentRequest({'message': '通过 Graph 调用'}),
            context,
        )

        self.assertEqual(response.data['response'], 'Graph 中的 AION 回答')
        self.assertEqual(response.data['reasoning'], 'Graph 中的推理')
        self.assertEqual(response.data['usage']['totalTokens'], 6)


if __name__ == '__main__':
    unittest.main()
