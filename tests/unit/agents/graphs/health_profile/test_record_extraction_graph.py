"""建档候选事实提取 Graph 的无网络测试。"""

from __future__ import annotations

import unittest

from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents import AgentRuntime
from agents import GraphRegistry
from agents.errors import AgentConfigurationError
from agents.graphs.health_profile import PROFILE_RECORD_EXTRACTION_CAPABILITY
from agents.graphs.health_profile import PROFILE_RECORD_EXTRACTION_GRAPH_VERSION
from agents.graphs.health_profile import build_profile_record_extraction_graph
from agents.graphs.health_profile import create_profile_record_extraction_profile
from agents.graphs.health_profile import register_profile_record_extraction
from agents.llm import ContextPolicy
from agents.llm import LLMResult
from agents.llm import ModelProfile
from agents.llm import ModelProfileRegistry
from tests.unit._support.agents import FakeLLMGateway
from langgraph.checkpoint.memory import InMemorySaver


def _runtime(gateway):
    registry = GraphRegistry()
    runtime = AgentRuntime(
        hub=AgentHub(registry),
        registry=registry,
        profiles=ModelProfileRegistry(),
        llm_gateway=gateway,
        context_repository=object(),
        lease_manager=object(),
        checkpointer=InMemorySaver(),
        _close_callback=lambda: None,
    )
    registration = register_profile_record_extraction(runtime)
    return runtime, registration


class ProfileRecordExtractionGraphTest(unittest.IsolatedAsyncioTestCase):
    async def test_uses_stateless_llm_without_exposing_reasoning(self):
        gateway = FakeLLMGateway(complete_result=LLMResult(
            content='{"candidates":[]}',
            reasoning_content='不应进入 Graph 输出',
            provider='fake-provider',
            model='fake-model',
        ))
        runtime, registration = _runtime(gateway)

        result = await runtime.hub.invoke(
            PROFILE_RECORD_EXTRACTION_CAPABILITY,
            AgentRequest({'source_content': '{"answers":[]}'}),
            AgentRunContext.create(
                user_id='user-1',
                session_id='session-1',
                request_id='request-1',
                run_id='run-1',
            ),
        )

        self.assertEqual(result.data['content'], '{"candidates":[]}')
        self.assertNotIn('reasoning_content', result.data)
        self.assertEqual(result.data['status'], 'completed')
        self.assertEqual(
            registration.version,
            PROFILE_RECORD_EXTRACTION_GRAPH_VERSION,
        )
        self.assertIsNone(registration.graph.checkpointer)
        request = gateway.complete_requests[0]
        self.assertIs(request.profile.context_policy, ContextPolicy.STATELESS)
        self.assertEqual(
            request.profile.prompt_name,
            'health_profile/record_candidates.txt',
        )
        self.assertFalse(request.effective_thinking)
        self.assertIsNone(request.conversation_key)

    def test_rejects_stateful_profile(self):
        profile = ModelProfile(
            name=PROFILE_RECORD_EXTRACTION_CAPABILITY,
            context_policy=ContextPolicy.AION_FULL,
        )
        with self.assertRaisesRegex(AgentConfigurationError, 'STATELESS'):
            build_profile_record_extraction_graph(
                gateway=FakeLLMGateway(),
                profile=profile,
            )

    def test_profile_uses_expected_capability(self):
        profile = create_profile_record_extraction_profile(model=' model-1 ')
        self.assertEqual(profile.name, PROFILE_RECORD_EXTRACTION_CAPABILITY)
        self.assertEqual(profile.model, 'model-1')


if __name__ == '__main__':
    unittest.main()
