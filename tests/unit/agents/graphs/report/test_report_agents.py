"""Report 三个无状态 Agent 能力的无网络测试。"""

from __future__ import annotations

import json
import unittest

from langgraph.checkpoint.memory import InMemorySaver

from adapters.report.agent_context_selector import AgentReportContextSelector
from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents import AgentRuntime
from agents import GraphRegistry
from agents.errors import AgentConfigurationError
from agents.graphs.report import REPORT_FOLLOWUP_CAPABILITY
from agents.graphs.report import REPORT_NARRATIVE_CAPABILITY
from agents.graphs.report import REPORT_RETRIEVE_CONTEXT_CAPABILITY
from agents.graphs.report import build_report_followup_graph
from agents.graphs.report import build_report_retrieve_context_graph
from agents.graphs.report import create_report_followup_profile
from agents.graphs.report import create_report_narrative_profile
from agents.graphs.report import create_report_retrieve_context_profile
from agents.graphs.report import register_report_followup
from agents.graphs.report import register_report_narrative
from agents.graphs.report import register_report_retrieve_context
from agents.llm import ContextPolicy
from agents.llm import LLMResult
from agents.llm import ModelProfile
from agents.llm import ModelProfileRegistry
from tests.unit._support.agents import FakeLLMGateway


def _runtime(gateway: FakeLLMGateway) -> AgentRuntime:
    registry = GraphRegistry()
    return AgentRuntime(
        hub=AgentHub(registry),
        registry=registry,
        profiles=ModelProfileRegistry(),
        llm_gateway=gateway,
        context_repository=object(),
        lease_manager=object(),
        checkpointer=InMemorySaver(),
        _close_callback=lambda: None,
    )


class ReportProfileTest(unittest.TestCase):
    def test_profiles_are_stateless_and_keep_separate_prompts(self) -> None:
        narrative = create_report_narrative_profile()
        followup = create_report_followup_profile()
        retrieval = create_report_retrieve_context_profile()

        self.assertIs(narrative.context_policy, ContextPolicy.STATELESS)
        self.assertIs(followup.context_policy, ContextPolicy.STATELESS)
        self.assertIs(retrieval.context_policy, ContextPolicy.STATELESS)
        self.assertEqual(narrative.prompt_name, 'report/narrative.txt')
        self.assertEqual(followup.prompt_name, 'report/followups/system.txt')
        self.assertEqual(retrieval.prompt_name, 'report/retrieve_context.txt')
        self.assertEqual(retrieval.model_kwargs, {'temperature': 0})


class ReportRegistrationTest(unittest.TestCase):
    def test_registers_all_report_capabilities_without_checkpoints(self) -> None:
        runtime = _runtime(FakeLLMGateway())

        registrations = (
            register_report_narrative(runtime),
            register_report_followup(runtime),
            register_report_retrieve_context(runtime),
        )

        self.assertEqual(
            runtime.registry.capabilities(),
            (
                REPORT_FOLLOWUP_CAPABILITY,
                REPORT_NARRATIVE_CAPABILITY,
                REPORT_RETRIEVE_CONTEXT_CAPABILITY,
            ),
        )
        self.assertTrue(
            all(registration.graph.checkpointer is None for registration in registrations),
        )


class ReportGraphTest(unittest.IsolatedAsyncioTestCase):
    async def test_retrieval_forwards_only_message_and_authorized_catalog(self) -> None:
        gateway = FakeLLMGateway(complete_result=LLMResult(
            content=(
                '{"selected":true,"finalReportId":"rpt-2",'
                '"reportType":"opinion"}'
            ),
            provider='fake-provider',
            model='fake-model',
        ))
        runtime = _runtime(gateway)
        registration = register_report_retrieve_context(runtime)
        context = AgentRunContext.create(
            user_id='user-1',
            session_id='session-1',
            request_id='request-1',
            run_id='run-1',
        )

        result = await runtime.hub.invoke(
            REPORT_RETRIEVE_CONTEXT_CAPABILITY,
            AgentRequest({
                'message': '请解释8月的诊断意见',
                'candidates': [
                    {
                        'finalReportId': 'rpt-1',
                        'createdAt': '2026-07-01T00:00:00Z',
                        'availableReportTypes': ['evaluate'],
                    },
                    {
                        'finalReportId': 'rpt-2',
                        'createdAt': '2026-08-01T00:00:00Z',
                        'availableReportTypes': ['evaluate', 'opinion'],
                    },
                ],
            }),
            context,
        )

        self.assertEqual(result.data['run_id'], 'run-1')
        self.assertEqual(result.data['status'], 'completed')
        self.assertIsNone(registration.graph.checkpointer)
        request = gateway.complete_requests[0]
        sent = json.loads(request.content)
        self.assertEqual(sent['userMessage'], '请解释8月的诊断意见')
        self.assertEqual(len(sent['authorizedCandidates']), 2)
        self.assertNotIn('body', request.content)
        self.assertIsNone(request.conversation_key)

    async def test_followup_rejects_unknown_task(self) -> None:
        profile = create_report_followup_profile()
        graph = build_report_followup_graph(
            gateway=FakeLLMGateway(),
            profile=profile,
        ).compile(checkpointer=None)
        registry = GraphRegistry()
        registry.register(
            REPORT_FOLLOWUP_CAPABILITY,
            graph,
            version='test',
        )
        hub = AgentHub(registry)

        with self.assertRaisesRegex(AgentConfigurationError, 'unknown'):
            await hub.invoke(
                REPORT_FOLLOWUP_CAPABILITY,
                AgentRequest({
                    'task_type': 'arbitrary_prompt',
                    'user_prompt': '任意指令',
                }),
                AgentRunContext.create(
                    user_id='user-1',
                    session_id='session-1',
                ),
            )

    def test_retrieval_rejects_stateful_profile(self) -> None:
        invalid = ModelProfile(
            name=REPORT_RETRIEVE_CONTEXT_CAPABILITY,
            context_policy=ContextPolicy.AION_FULL,
        )
        with self.assertRaisesRegex(AgentConfigurationError, 'STATELESS'):
            build_report_retrieve_context_graph(
                gateway=FakeLLMGateway(),
                profile=invalid,
            )

    async def test_selector_accepts_only_a_reference_from_the_catalog(self) -> None:
        gateway = FakeLLMGateway(complete_result=LLMResult(
            content=(
                '{"selected":true,"finalReportId":"rpt-outside",'
                '"reportType":"evaluate"}'
            ),
        ))
        runtime = _runtime(gateway)
        register_report_retrieve_context(runtime)
        selector = AgentReportContextSelector(hub=runtime.hub)

        selected = await selector.select(
            user_id='user-1',
            session_id='session-1',
            message='请解释报告',
            candidates=[
                {
                    'finalReportId': 'rpt-1',
                    'createdAt': '2026-07-01T00:00:00Z',
                    'availableReportTypes': ['evaluate'],
                },
                {
                    'finalReportId': 'rpt-2',
                    'createdAt': '2026-08-01T00:00:00Z',
                    'availableReportTypes': ['evaluate', 'opinion'],
                },
            ],
        )

        self.assertIsNone(selected)

    async def test_selector_returns_an_authorized_reference(self) -> None:
        gateway = FakeLLMGateway(complete_result=LLMResult(
            content=(
                '{"selected":true,"finalReportId":"rpt-2",'
                '"reportType":"opinion"}'
            ),
        ))
        runtime = _runtime(gateway)
        register_report_retrieve_context(runtime)
        selector = AgentReportContextSelector(hub=runtime.hub)

        selected = await selector.select(
            user_id='user-1',
            session_id='session-1',
            message='请解释8月的诊断意见',
            candidates=[
                {
                    'finalReportId': 'rpt-1',
                    'createdAt': '2026-07-01T00:00:00Z',
                    'availableReportTypes': ['evaluate'],
                },
                {
                    'finalReportId': 'rpt-2',
                    'createdAt': '2026-08-01T00:00:00Z',
                    'availableReportTypes': ['evaluate', 'opinion'],
                },
            ],
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.final_report_id, 'rpt-2')
        self.assertEqual(selected.report_type, 'opinion')


if __name__ == '__main__':
    unittest.main()
