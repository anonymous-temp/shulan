"""厂商无关 LLMGateway 和隔离验证图的无网络测试。"""

from __future__ import annotations

import unittest

from agents import AgentEventType
from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents import GraphRegistry
from agents.errors import ModelProfileAlreadyRegisteredError
from agents.errors import ModelProfileNotFoundError
from agents.graphs.validation import build_validation_graph
from agents.llm import ContextPolicy
from agents.llm import LLMChunk
from agents.llm import LLMRequest
from agents.llm import LLMResult
from agents.llm import LLMUsage
from agents.llm import ModelProfile
from agents.llm import ModelProfileRegistry
from tests.unit._support.agents import FakeLLMGateway


class ModelProfileTest(unittest.TestCase):
    def test_registry_rejects_duplicates_and_unknown_names(self) -> None:
        registry = ModelProfileRegistry()
        profile = ModelProfile(
            name='chat.reply',
            context_policy=ContextPolicy.AION_FULL,
            prompt_name='chat/chat_system.txt',
            thinking=True,
        )
        registry.register(profile)

        self.assertIs(registry.get('chat.reply'), profile)
        self.assertEqual(registry.names(), ('chat.reply',))
        with self.assertRaises(ModelProfileAlreadyRegisteredError):
            registry.register(profile)
        with self.assertRaises(ModelProfileNotFoundError):
            registry.get('report.generate')

    def test_profile_names_and_optional_text_are_normalized(self) -> None:
        profile = ModelProfile(
            name='  validation.echo  ',
            context_policy=ContextPolicy.STATELESS,
            prompt_name='  chat/chat_system.txt  ',
            model='  fake-model  ',
        )
        registry = ModelProfileRegistry()
        registry.register(profile)

        self.assertEqual(profile.name, 'validation.echo')
        self.assertEqual(profile.prompt_name, 'chat/chat_system.txt')
        self.assertEqual(profile.model, 'fake-model')
        self.assertIs(registry.get(' validation.echo '), profile)

    def test_effective_thinking_uses_request_override(self) -> None:
        profile = ModelProfile(
            name='test.profile',
            context_policy=ContextPolicy.STATELESS,
            thinking=True,
        )

        inherited = LLMRequest(profile=profile, content='问题')
        overridden = LLMRequest(
            profile=profile,
            content='问题',
            thinking=False,
        )

        self.assertTrue(inherited.effective_thinking)
        self.assertFalse(overridden.effective_thinking)

    def test_supplemental_context_only_changes_effective_content(self) -> None:
        profile = ModelProfile(
            name='chat.reply',
            context_policy=ContextPolicy.AION_FULL,
        )
        request = LLMRequest(
            profile=profile,
            content='用户原始问题',
            supplemental_context='[资料1] 医学指南摘要',
            conversation_key='conversation-1',
        )

        self.assertEqual(request.content, '用户原始问题')
        self.assertIn('用户原始问题', request.effective_content)
        self.assertIn('[资料1] 医学指南摘要', request.effective_content)

        with self.assertRaisesRegex(ValueError, 'cannot be empty'):
            LLMRequest(
                profile=profile,
                content='问题',
                supplemental_context='  ',
                conversation_key='conversation-1',
            )

    def test_context_policy_validates_conversation_key(self) -> None:
        stateful = ModelProfile(
            name='chat.reply',
            context_policy=ContextPolicy.AION_FULL,
        )
        stateless = ModelProfile(
            name='report.generate',
            context_policy=ContextPolicy.STATELESS,
        )

        with self.assertRaisesRegex(ValueError, 'require conversation_key'):
            LLMRequest(profile=stateful, content='问题')
        with self.assertRaisesRegex(ValueError, 'cannot set conversation_key'):
            LLMRequest(
                profile=stateless,
                content='问题',
                conversation_key='unexpected',
            )

        request = LLMRequest(
            profile=stateful,
            content='问题',
            conversation_key='conversation-1',
        )
        self.assertEqual(request.conversation_key, 'conversation-1')

    def test_usage_rejects_negative_or_boolean_tokens(self) -> None:
        with self.assertRaisesRegex(ValueError, 'cannot be negative'):
            LLMUsage(prompt_tokens=-1)
        with self.assertRaisesRegex(TypeError, 'must be an integer'):
            LLMUsage(total_tokens=True)

    def test_profile_rejects_reserved_model_kwargs(self) -> None:
        with self.assertRaisesRegex(ValueError, 'reserved keys: thinking'):
            ModelProfile(
                name='invalid.profile',
                context_policy=ContextPolicy.STATELESS,
                model_kwargs={'thinking': True},
            )


class ValidationGraphTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.profile = ModelProfile(
            name='validation.echo',
            context_policy=ContextPolicy.STATELESS,
            model='fake-model',
            thinking=True,
        )
        self.context = AgentRunContext.create(
            user_id='user-1',
            session_id='session-1',
            request_id='request-1',
            run_id='run-1',
        )

    @staticmethod
    def _build_hub(gateway, profile):
        registry = GraphRegistry()
        registry.register(
            'validation.llm',
            build_validation_graph(gateway=gateway, profile=profile),
            version='validation-v1',
        )
        return AgentHub(registry)

    async def test_complete_path_uses_fake_gateway(self) -> None:
        usage = LLMUsage(
            prompt_tokens=10,
            completion_tokens=4,
            total_tokens=14,
            raw={'prompt_cache_hit_tokens': 3},
        )
        gateway = FakeLLMGateway(
            complete_result=LLMResult(
                content='完整回答',
                reasoning_content='完整推理',
                usage=usage,
                provider='fake',
                model='fake-model',
            ),
        )
        hub = self._build_hub(gateway, self.profile)

        result = await hub.invoke(
            'validation.llm',
            AgentRequest({'message': '测试完整调用', 'stream': False}),
            self.context,
        )

        self.assertEqual(result.data['response'], '完整回答')
        self.assertEqual(result.data['reasoning'], '完整推理')
        self.assertEqual(result.data['usage']['totalTokens'], 14)
        self.assertEqual(len(gateway.complete_requests), 1)
        self.assertEqual(gateway.complete_requests[0].profile, self.profile)
        self.assertTrue(gateway.complete_requests[0].effective_thinking)
        self.assertEqual(gateway.stream_requests, [])

    async def test_stream_path_maps_fake_chunks_to_agent_events(self) -> None:
        usage = LLMUsage(total_tokens=8)
        gateway = FakeLLMGateway(stream_chunks=[
            LLMChunk(reasoning_content='思考一'),
            LLMChunk(reasoning_content='思考二'),
            LLMChunk(content='正文一'),
            LLMChunk(content='正文二'),
            LLMChunk(usage=usage),
        ])
        hub = self._build_hub(gateway, self.profile)

        events = [
            event
            async for event in hub.stream(
                'validation.llm',
                AgentRequest({'message': '测试流式调用', 'stream': True}),
                self.context,
            )
        ]

        self.assertEqual(
            [event.type for event in events],
            [
                AgentEventType.RUN_STARTED,
                AgentEventType.REASONING_DELTA,
                AgentEventType.REASONING_DELTA,
                AgentEventType.CONTENT_DELTA,
                AgentEventType.CONTENT_DELTA,
                AgentEventType.RUN_COMPLETED,
            ],
        )
        self.assertEqual(
            [event.data.get('content') for event in events[1:5]],
            ['思考一', '思考二', '正文一', '正文二'],
        )
        self.assertEqual(len(gateway.stream_requests), 1)
        self.assertEqual(gateway.complete_requests, [])

    async def test_stateful_profile_forwards_conversation_key(self) -> None:
        profile = ModelProfile(
            name='validation.stateful',
            context_policy=ContextPolicy.AION_FULL,
        )
        gateway = FakeLLMGateway(
            complete_result=LLMResult(content='已记录'),
        )
        hub = self._build_hub(gateway, profile)

        await hub.invoke(
            'validation.llm',
            AgentRequest({
                'message': '需要上下文的消息',
                'conversation_key': 'conversation-1',
            }),
            self.context,
        )

        self.assertEqual(
            gateway.complete_requests[0].conversation_key,
            'conversation-1',
        )


if __name__ == '__main__':
    unittest.main()
