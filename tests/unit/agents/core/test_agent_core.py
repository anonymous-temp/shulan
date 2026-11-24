"""Agent Core 骨架的无网络契约测试。"""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from typing import Any
from typing import TypedDict

from agents import AgentEventType
from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents import GraphRegistry
from agents.errors import AgentCapabilityAlreadyRegisteredError
from agents.errors import AgentCapabilityNotFoundError
from agents.errors import AgentProtocolError
from langgraph.config import get_stream_writer
from langgraph.graph import END
from langgraph.graph import START
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime


class RecordingGraph:
    """记录 AgentHub 调用参数的最小已编译 Graph 替身。"""

    def __init__(self) -> None:
        self.invoke_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []
        self.invoke_output: Any = {'reply': '完成'}
        self.stream_parts: list[Any] = []

    async def ainvoke(self, input, config=None, *, context=None, **kwargs):
        self.invoke_calls.append({
            'input': input,
            'config': config,
            'context': context,
            'kwargs': kwargs,
        })
        return self.invoke_output

    async def astream(
            self,
            input,
            config=None,
            *,
            context=None,
            **kwargs,
    ) -> AsyncIterator[Any]:
        self.stream_calls.append({
            'input': input,
            'config': config,
            'context': context,
            'kwargs': kwargs,
        })
        for part in self.stream_parts:
            yield part


class CoreTestState(TypedDict, total=False):
    message: str
    reply: str


async def _core_test_node(
        state: CoreTestState,
        runtime: Runtime[AgentRunContext],
) -> CoreTestState:
    writer = get_stream_writer()
    writer({
        'type': 'run_started',
        'data': {'userId': runtime.context.user_id},
    })
    reply = f'{runtime.context.user_id}: {state["message"]}'
    writer({
        'type': 'content_delta',
        'data': {'content': reply},
    })
    return {'reply': reply}


def _build_core_test_graph():
    builder = StateGraph(
        CoreTestState,
        context_schema=AgentRunContext,
    )
    builder.add_node('reply', _core_test_node)
    builder.add_edge(START, 'reply')
    builder.add_edge('reply', END)
    return builder.compile()


class AgentRunContextTest(unittest.TestCase):
    def test_create_generates_request_and_run_ids(self) -> None:
        context = AgentRunContext.create(
            user_id='user-1',
            session_id='session-1',
            subject_id='subject-1',
        )

        self.assertTrue(context.request_id)
        self.assertTrue(context.run_id)
        self.assertNotEqual(context.request_id, context.run_id)
        self.assertEqual(context.subject_id, 'subject-1')

    def test_health_profile_context_is_validated_and_hidden_from_repr(
            self,
    ) -> None:
        context = AgentRunContext.create(
            user_id='user-1',
            session_id='session-1',
            health_profile_context='敏感健康画像内容',
        )

        self.assertEqual(context.health_profile_context, '敏感健康画像内容')
        self.assertNotIn('敏感健康画像内容', repr(context))
        with self.assertRaisesRegex(ValueError, 'health_profile_context'):
            AgentRunContext.create(
                user_id='user-1',
                session_id='session-1',
                health_profile_context=' ',
            )

    def test_rejects_empty_business_identifier(self) -> None:
        with self.assertRaisesRegex(ValueError, 'user_id cannot be empty'):
            AgentRunContext.create(user_id=' ', session_id='session-1')


class GraphRegistryTest(unittest.TestCase):
    def test_registers_and_lists_capabilities_in_stable_order(self) -> None:
        registry = GraphRegistry()
        chat_graph = RecordingGraph()
        report_graph = RecordingGraph()

        registry.register('report.generate', report_graph, version='1')
        registration = registry.register('chat.reply', chat_graph, version='1')

        self.assertIs(registration.graph, chat_graph)
        self.assertEqual(registry.get('chat.reply'), registration)
        self.assertEqual(
            registry.capabilities(),
            ('chat.reply', 'report.generate'),
        )

    def test_rejects_duplicate_capability(self) -> None:
        registry = GraphRegistry()
        registry.register('chat.reply', RecordingGraph(), version='1')

        with self.assertRaises(AgentCapabilityAlreadyRegisteredError):
            registry.register('chat.reply', RecordingGraph(), version='2')

    def test_unknown_capability_has_domain_error(self) -> None:
        registry = GraphRegistry()

        with self.assertRaisesRegex(
                AgentCapabilityNotFoundError,
                'Unknown Agent capability',
        ):
            registry.get('chat.reply')


class AgentHubTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.graph = RecordingGraph()
        self.registry = GraphRegistry()
        self.registry.register('chat.reply', self.graph, version='chat-v1')
        self.hub = AgentHub(self.registry)
        self.context = AgentRunContext.create(
            user_id='user-1',
            session_id='session-1',
            request_id='request-1',
            run_id='run-1',
        )

    async def test_invoke_wraps_graph_result_and_runtime_metadata(self) -> None:
        payload = {'message': '你好'}
        result = await self.hub.invoke(
            'chat.reply',
            AgentRequest(payload),
            self.context,
            thread_id='thread-1',
        )

        self.assertEqual(result.capability, 'chat.reply')
        self.assertEqual(result.graph_version, 'chat-v1')
        self.assertEqual(result.run_id, 'run-1')
        self.assertEqual(result.data, {'reply': '完成'})
        self.assertEqual(
            self.graph.invoke_calls[0]['config'],
            {'configurable': {'thread_id': 'thread-1'}},
        )
        self.assertIs(self.graph.invoke_calls[0]['context'], self.context)
        self.assertIsNot(self.graph.invoke_calls[0]['input'], payload)

    async def test_invoke_defaults_thread_id_to_run_id(self) -> None:
        await self.hub.invoke(
            'chat.reply',
            AgentRequest({'message': '你好'}),
            self.context,
        )

        self.assertEqual(
            self.graph.invoke_calls[0]['config'],
            {'configurable': {'thread_id': 'run-1'}},
        )

    async def test_invoke_rejects_non_mapping_graph_result(self) -> None:
        self.graph.invoke_output = 'invalid'

        with self.assertRaisesRegex(AgentProtocolError, 'non-mapping result'):
            await self.hub.invoke(
                'chat.reply',
                AgentRequest({'message': '你好'}),
                self.context,
            )

    async def test_stream_normalizes_langgraph_v2_custom_events(self) -> None:
        self.graph.stream_parts = [
            {
                'type': 'custom',
                'ns': (),
                'data': {
                    'type': 'run_started',
                    'data': {'capability': 'chat.reply'},
                },
            },
            {
                'type': 'custom',
                'ns': (),
                'data': {
                    'type': 'content_delta',
                    'runId': 'run-1',
                    'data': {'content': '你好'},
                },
            },
        ]

        events = [
            event
            async for event in self.hub.stream(
                'chat.reply',
                AgentRequest({'message': '你好'}),
                self.context,
                thread_id='thread-1',
            )
        ]

        self.assertEqual(
            [event.type for event in events],
            [AgentEventType.RUN_STARTED, AgentEventType.CONTENT_DELTA],
        )
        self.assertEqual(events[0].run_id, 'run-1')
        self.assertEqual(events[1].data, {'content': '你好'})
        self.assertEqual(
            self.graph.stream_calls[0]['kwargs'],
            {'stream_mode': 'custom', 'version': 'v2'},
        )

    async def test_stream_rejects_unknown_event_type(self) -> None:
        self.graph.stream_parts = [
            {'type': 'custom', 'data': {'type': 'unknown', 'data': {}}},
        ]

        with self.assertRaisesRegex(AgentProtocolError, 'Unsupported Agent event'):
            async for _ in self.hub.stream(
                    'chat.reply',
                    AgentRequest({}),
                    self.context,
            ):
                pass


class AgentHubLangGraphIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """验证 AgentHub 与当前已安装 LangGraph 的真实调用协议。"""

    def setUp(self) -> None:
        registry = GraphRegistry()
        registry.register(
            'test.echo',
            _build_core_test_graph(),
            version='test-v1',
        )
        self.hub = AgentHub(registry)
        self.context = AgentRunContext.create(
            user_id='user-real-graph',
            session_id='session-real-graph',
            request_id='request-real-graph',
            run_id='run-real-graph',
        )

    async def test_invoke_passes_typed_runtime_context_to_langgraph(self) -> None:
        result = await self.hub.invoke(
            'test.echo',
            AgentRequest({'message': '真实图'}),
            self.context,
        )

        self.assertEqual(result.data['reply'], 'user-real-graph: 真实图')

    async def test_stream_consumes_real_langgraph_custom_events(self) -> None:
        events = [
            event
            async for event in self.hub.stream(
                'test.echo',
                AgentRequest({'message': '真实流'}),
                self.context,
            )
        ]

        self.assertEqual(
            [event.type for event in events],
            [AgentEventType.RUN_STARTED, AgentEventType.CONTENT_DELTA],
        )
        self.assertEqual(
            events[1].data,
            {'content': 'user-real-graph: 真实流'},
        )


if __name__ == '__main__':
    unittest.main()
