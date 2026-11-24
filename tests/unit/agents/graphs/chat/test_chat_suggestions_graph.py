"""普通聊天个性化推荐问题 Agent 能力的无网络测试。"""

from __future__ import annotations

import unittest

from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents import AgentRuntime
from agents import GraphRegistry
from agents.errors import AgentConfigurationError
from agents.graphs.chat import CHAT_SUGGESTIONS_CAPABILITY
from agents.graphs.chat import CHAT_SUGGESTIONS_GRAPH_VERSION
from agents.graphs.chat import build_chat_suggestions_graph
from agents.graphs.chat import create_chat_suggestions_profile
from agents.graphs.chat import register_chat_suggestions
from agents.llm import ContextPolicy
from agents.llm import LLMResult
from agents.llm import ModelProfile
from agents.llm import ModelProfileRegistry
from tests.unit._support.agents import FakeLLMGateway
from langgraph.checkpoint.memory import InMemorySaver


class ChatSuggestionsProfileTest(unittest.TestCase):
    def test_profile_is_stateless_and_keeps_legacy_defaults(self) -> None:
        profile = create_chat_suggestions_profile(model=' model-1 ')

        self.assertEqual(profile.name, CHAT_SUGGESTIONS_CAPABILITY)
        self.assertIs(profile.context_policy, ContextPolicy.STATELESS)
        self.assertEqual(profile.prompt_name, 'chat/suggestions.txt')
        self.assertEqual(profile.model, 'model-1')
        self.assertFalse(profile.thinking)


class ChatSuggestionsRegistrationTest(unittest.TestCase):
    def test_registers_without_checkpoint(self) -> None:
        registry = GraphRegistry()
        runtime = AgentRuntime(
            hub=AgentHub(registry),
            registry=registry,
            profiles=ModelProfileRegistry(),
            llm_gateway=FakeLLMGateway(),
            context_repository=object(),
            lease_manager=object(),
            checkpointer=InMemorySaver(),
            _close_callback=lambda: None,
        )

        registration = register_chat_suggestions(
            runtime,
            model='fake-model',
        )

        self.assertEqual(
            registration.capability,
            CHAT_SUGGESTIONS_CAPABILITY,
        )
        self.assertEqual(
            registration.version,
            CHAT_SUGGESTIONS_GRAPH_VERSION,
        )
        self.assertIsNone(registration.graph.checkpointer)
        self.assertEqual(
            runtime.profiles.get(CHAT_SUGGESTIONS_CAPABILITY).model,
            'fake-model',
        )


class ChatSuggestionsGraphTest(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_stateless_request_and_maps_result(self) -> None:
        profile = create_chat_suggestions_profile(model='fake-model')
        gateway = FakeLLMGateway(complete_result=LLMResult(
            content='如何改善睡眠？\n饮食需要注意什么？',
            provider='fake-provider',
            model='fake-model',
        ))
        graph = build_chat_suggestions_graph(
            gateway=gateway,
            profile=profile,
        ).compile(checkpointer=None)
        registry = GraphRegistry()
        registry.register(
            CHAT_SUGGESTIONS_CAPABILITY,
            graph,
            version=CHAT_SUGGESTIONS_GRAPH_VERSION,
        )
        hub = AgentHub(registry)
        context = AgentRunContext.create(
            user_id='user-1',
            session_id='qa-session-1',
            request_id='request-1',
            run_id='run-1',
        )

        result = await hub.invoke(
            CHAT_SUGGESTIONS_CAPABILITY,
            AgentRequest({'prompt': '请生成两个问题'}),
            context,
        )

        self.assertEqual(
            result.data['content'],
            '如何改善睡眠？\n饮食需要注意什么？',
        )
        self.assertEqual(result.data['request_id'], 'request-1')
        self.assertEqual(result.data['run_id'], 'run-1')
        self.assertEqual(result.data['status'], 'completed')
        request = gateway.complete_requests[0]
        self.assertIs(request.profile, profile)
        self.assertEqual(request.content, '请生成两个问题')
        self.assertIsNone(request.conversation_key)
        self.assertFalse(request.effective_thinking)

    def test_rejects_stateful_profile(self) -> None:
        invalid_profile = ModelProfile(
            name=CHAT_SUGGESTIONS_CAPABILITY,
            context_policy=ContextPolicy.AION_FULL,
        )

        with self.assertRaisesRegex(AgentConfigurationError, 'STATELESS'):
            build_chat_suggestions_graph(
                gateway=FakeLLMGateway(),
                profile=invalid_profile,
            )


if __name__ == '__main__':
    unittest.main()
