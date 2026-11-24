"""体检报告解读 Agent 能力的无网络测试。"""

from __future__ import annotations

import unittest

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents import AgentRuntime
from agents import GraphRegistry
from agents.errors import AgentConfigurationError
from agents.graphs.analysis import ANALYSIS_HEALTH_REPORT_CAPABILITY
from agents.graphs.analysis import ANALYSIS_HEALTH_REPORT_GRAPH_VERSION
from agents.graphs.analysis import build_analysis_health_report_graph
from agents.graphs.analysis import create_analysis_health_report_profile
from agents.graphs.analysis import register_analysis_health_report
from agents.llm import AionLLMGateway
from agents.llm import ContextPolicy
from agents.llm import LLMRequest
from agents.llm import LLMResult
from agents.llm import LLMUsage
from agents.llm import ModelProfile
from agents.llm import ModelProfileRegistry
from tests.unit._support.agents import FakeLLMGateway
from tests.unit.agents.llm.test_web_search import aion_wire as aion_wire


def _build_runtime(gateway):
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
    registration = register_analysis_health_report(runtime)
    return runtime, registration


class AnalysisHealthReportProfileTest(unittest.TestCase):
    def test_profile_preserves_legacy_prompt_and_model_defaults(self):
        profile = create_analysis_health_report_profile(model=' model-1 ')

        self.assertEqual(profile.name, ANALYSIS_HEALTH_REPORT_CAPABILITY)
        self.assertIs(profile.context_policy, ContextPolicy.STATELESS)
        self.assertEqual(profile.prompt_name, 'analysis/health_report.txt')
        self.assertEqual(profile.model, 'model-1')
        self.assertFalse(profile.thinking)
        self.assertEqual(
            profile.model_kwargs,
            {
                'max_tokens': 1024 * 100,
                'response_format': {'type': 'json_object'},
                'temperature': 0,
            },
        )


class AnalysisHealthReportGraphTest(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_report_as_stateless_request_and_maps_result(self):
        gateway = FakeLLMGateway(
            complete_result=LLMResult(
                content='{"reportType":"lab"}',
                reasoning_content='内部推理',
                usage=LLMUsage(total_tokens=42),
                provider='fake-provider',
                model='fake-model',
            )
        )
        runtime, registration = _build_runtime(gateway)
        context = AgentRunContext.create(
            user_id='user-1',
            session_id='analysis-session-1',
            request_id='request-1',
            run_id='run-1',
        )

        result = await runtime.hub.invoke(
            ANALYSIS_HEALTH_REPORT_CAPABILITY,
            AgentRequest({'report_text': '血红蛋白 120 g/L'}),
            context,
        )

        self.assertEqual(result.data['content'], '{"reportType":"lab"}')
        self.assertEqual(result.data['reasoning_content'], '内部推理')
        self.assertEqual(result.data['usage']['totalTokens'], 42)
        self.assertEqual(result.data['request_id'], 'request-1')
        self.assertEqual(result.data['run_id'], 'run-1')
        self.assertEqual(result.data['status'], 'completed')
        self.assertEqual(result.data['output_token_limit'], 1024 * 100)
        self.assertEqual(
            registration.version,
            ANALYSIS_HEALTH_REPORT_GRAPH_VERSION,
        )
        self.assertIsNone(registration.graph.checkpointer)

        request = gateway.complete_requests[0]
        self.assertEqual(request.content, '血红蛋白 120 g/L')
        self.assertIsNone(request.conversation_key)
        self.assertFalse(request.effective_thinking)

    def test_rejects_stateful_profile(self):
        invalid_profile = ModelProfile(
            name=ANALYSIS_HEALTH_REPORT_CAPABILITY,
            context_policy=ContextPolicy.AION_FULL,
        )

        with self.assertRaisesRegex(AgentConfigurationError, 'STATELESS'):
            build_analysis_health_report_graph(
                gateway=FakeLLMGateway(),
                profile=invalid_profile,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['deepseek', 'aliyun'])
async def test_analysis_budget_and_json_mode_reach_the_installed_aion_sdk(aion_wire, provider):
    gateway = AionLLMGateway(
        provider=provider,
        api_key='unit-test-key',
        default_model='unit-test-model',
        prompt_renderer=lambda *args, **kwargs: 'Return complete JSON.',
    )
    await gateway.complete(LLMRequest(profile=create_analysis_health_report_profile(), content='血红蛋白 120 g/L'))
    assert len(aion_wire.requests) == 1
    request = aion_wire.requests[0]
    assert request['max_tokens'] == 1024 * 100
    assert request['response_format'] == {'type': 'json_object'}
    assert request['temperature'] == 0
    if provider == 'deepseek':
        assert request['thinking'] == {'type': 'disabled'}
    else:
        assert request['enable_thinking'] is False


@pytest.mark.asyncio
async def test_analysis_counts_sdk_transport_retries_separately_from_model_calls():
    import aion
    import httpx
    from openai import AsyncOpenAI

    attempts = []

    async def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(503, json={'error': {'message': 'temporary', 'type': 'server_error'}})
        return httpx.Response(
            200,
            json={
                'id': 'test',
                'object': 'chat.completion',
                'created': 0,
                'model': 'unit-test-model',
                'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{}'}}],
            },
        )

    client = aion.create_chat_client('deepseek', api_key='unused', model='unit-test-model', context=False)
    await client.async_client.close()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.async_client = AsyncOpenAI(
        api_key='unused', base_url='https://unit-test.invalid', max_retries=1, http_client=http_client
    )
    gateway = AionLLMGateway(
        provider='deepseek',
        api_key='unused',
        client_factory=lambda *a, **k: client,
        prompt_renderer=lambda *a, **k: 'Return JSON.',
    )
    try:
        result = await gateway.complete(LLMRequest(profile=create_analysis_health_report_profile(), content='合成报告'))
        assert result.transport_retries == 1 and len(attempts) == 2
        assert http_client.event_hooks['request'] == []
        second = await gateway.complete(
            LLMRequest(profile=create_analysis_health_report_profile(), content='第二份合成报告')
        )
        assert second.transport_retries == 0 and len(attempts) == 3
        assert http_client.event_hooks['request'] == []
    finally:
        await client.async_client.close()
        client.client.close()


if __name__ == '__main__':
    unittest.main()


@pytest.mark.asyncio
async def test_current_registration_is_explicit_and_leaves_temporary_default_unchanged():
    from agents.graphs.analysis import register_current_analysis_health_report
    from modules.analysis.application.current_interpreter import CurrentHealthReportInterpreter

    gateway = FakeLLMGateway(complete_result=LLMResult(content='{"reportType":"lab"}'))
    runtime, default = _build_runtime(gateway)
    assert not gateway.complete_requests
    registration = register_current_analysis_health_report(runtime)
    assert not gateway.complete_requests
    assert runtime.registry.get(ANALYSIS_HEALTH_REPORT_CAPABILITY) is default
    assert len(runtime.registry.versions(CurrentHealthReportInterpreter.CAPABILITY)) == 1
    assert registration.version == CurrentHealthReportInterpreter.VERSION
    await runtime.hub.invoke(
        CurrentHealthReportInterpreter.CAPABILITY,
        AgentRequest({'report_text': 'synthetic complete report'}),
        AgentRunContext.create(user_id='test-current', session_id='test-current-session'),
    )
    assert gateway.complete_requests[0].profile.prompt_name == 'analysis/health_report_current.txt'
    assert gateway.complete_requests[0].profile.name == CurrentHealthReportInterpreter.PROFILE
