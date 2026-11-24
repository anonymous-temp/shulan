"""普通聊天业务 Graph 的无网络契约测试。"""

from __future__ import annotations

import unittest

from langgraph.checkpoint.memory import InMemorySaver

from agents import AgentEventType
from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents import AgentRuntime
from agents import GraphRegistry
from agents.errors import AgentConfigurationError
from agents.graphs.chat import CHAT_REPLY_CAPABILITY
from agents.graphs.chat import CHAT_REPLY_GRAPH_VERSION
from agents.graphs.chat import build_chat_reply_graph
from agents.graphs.chat import create_chat_reply_profile
from agents.graphs.chat import register_chat_reply
from agents.knowledge import KnowledgeDocument
from agents.knowledge import KnowledgeSource
from agents.knowledge import create_knowledge_route_profile
from agents.llm import ContextPolicy
from agents.llm import LLMChunk
from agents.llm import LLMResult
from agents.llm import LLMUsage
from agents.llm import ModelProfile
from agents.llm import ModelProfileRegistry
from tests.unit._support.agents import FakeKnowledgeGateway
from tests.unit._support.agents import FakeLLMGateway


class ChatReplyProfileTest(unittest.TestCase):
    def test_profile_preserves_the_confirmed_chat_defaults(self) -> None:
        profile = create_chat_reply_profile(model=' model-1 ')

        self.assertEqual(profile.name, 'chat.reply')
        self.assertIs(profile.context_policy, ContextPolicy.AION_FULL)
        self.assertEqual(profile.prompt_name, 'chat/chat_system.txt')
        self.assertEqual(profile.model, 'model-1')
        self.assertTrue(profile.thinking)


class ChatReplyRegistrationTest(unittest.TestCase):
    def test_registers_profile_and_graph_with_runtime_checkpointer(self) -> None:
        registry = GraphRegistry()
        gateway = FakeLLMGateway()
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

        registration = register_chat_reply(runtime, model='fake-model')

        self.assertEqual(registration.capability, CHAT_REPLY_CAPABILITY)
        self.assertEqual(registration.version, CHAT_REPLY_GRAPH_VERSION)
        self.assertIs(
            runtime.registry.get(CHAT_REPLY_CAPABILITY),
            registration,
        )
        self.assertEqual(
            runtime.profiles.get('chat.reply').model,
            'fake-model',
        )


class ChatReplyGraphTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.profile = create_chat_reply_profile(model='fake-model')
        self.gateway = FakeLLMGateway(
            complete_result=LLMResult(
                content='这是模型回复',
                reasoning_content='这是推理内容',
                usage=LLMUsage(
                    prompt_tokens=12,
                    completion_tokens=6,
                    total_tokens=18,
                    raw={'prompt_cache_hit_tokens': 4},
                ),
                provider='fake-provider',
                model='fake-model',
            )
        )
        graph = build_chat_reply_graph(
            gateway=self.gateway,
            profile=self.profile,
        ).compile(checkpointer=InMemorySaver())
        registry = GraphRegistry()
        registry.register(
            CHAT_REPLY_CAPABILITY,
            graph,
            version=CHAT_REPLY_GRAPH_VERSION,
        )
        self.hub = AgentHub(registry)

    @staticmethod
    def _context(
        *,
        request_id: str,
        run_id: str,
        health_profile_context: str | None = None,
    ) -> AgentRunContext:
        return AgentRunContext.create(
            user_id='user-1',
            session_id='session-1',
            request_id=request_id,
            run_id=run_id,
            health_profile_context=health_profile_context,
        )

    async def test_complete_reply_maps_gateway_result_and_runtime_ids(self) -> None:
        context = self._context(request_id='request-1', run_id='run-1')

        result = await self.hub.invoke(
            CHAT_REPLY_CAPABILITY,
            AgentRequest(
                {
                    'agent_thread_id': 'agent-thread-1',
                    'user_message': '我最近睡眠不好',
                    'thinking': False,
                    'reasoning_effort': 'high',
                    'stream': False,
                }
            ),
            context,
            thread_id='agent-thread-1',
        )

        self.assertEqual(result.graph_version, CHAT_REPLY_GRAPH_VERSION)
        self.assertEqual(result.data['assistant_content'], '这是模型回复')
        self.assertEqual(result.data['reasoning_content'], '这是推理内容')
        self.assertEqual(result.data['usage']['totalTokens'], 18)
        self.assertEqual(result.data['provider'], 'fake-provider')
        self.assertEqual(result.data['model'], 'fake-model')
        self.assertEqual(result.data['request_id'], 'request-1')
        self.assertEqual(result.data['run_id'], 'run-1')
        self.assertEqual(result.data['status'], 'completed')

        request = self.gateway.complete_requests[0]
        self.assertIs(request.profile, self.profile)
        self.assertEqual(request.content, '我最近睡眠不好')
        self.assertEqual(request.conversation_key, 'agent-thread-1')
        self.assertFalse(request.effective_thinking)
        self.assertEqual(request.effective_reasoning_effort, 'high')

    async def test_follow_up_only_sends_current_turn_to_aion_gateway(self) -> None:
        await self.hub.invoke(
            CHAT_REPLY_CAPABILITY,
            AgentRequest(
                {
                    'agent_thread_id': 'agent-thread-1',
                    'user_message': '第一轮问题',
                    'stream': False,
                }
            ),
            self._context(request_id='request-1', run_id='run-1'),
            thread_id='agent-thread-1',
        )
        await self.hub.invoke(
            CHAT_REPLY_CAPABILITY,
            AgentRequest(
                {
                    'agent_thread_id': 'agent-thread-1',
                    'user_message': '第二轮追问',
                    'stream': False,
                }
            ),
            self._context(request_id='request-2', run_id='run-2'),
            thread_id='agent-thread-1',
        )

        self.assertEqual(
            [request.content for request in self.gateway.complete_requests],
            ['第一轮问题', '第二轮追问'],
        )
        self.assertEqual(
            [request.conversation_key for request in self.gateway.complete_requests],
            ['agent-thread-1', 'agent-thread-1'],
        )

    async def test_profile_memory_is_ephemeral_supplemental_context(self) -> None:
        memory = '长期画像事实：status=active，身高 168cm'

        result = await self.hub.invoke(
            CHAT_REPLY_CAPABILITY,
            AgentRequest(
                {
                    'agent_thread_id': 'agent-thread-memory',
                    'user_message': '请给我运动建议',
                    'stream': False,
                }
            ),
            self._context(
                request_id='request-memory',
                run_id='run-memory',
                health_profile_context=memory,
            ),
            thread_id='agent-thread-memory',
        )

        request = self.gateway.complete_requests[0]
        self.assertEqual(request.content, '请给我运动建议')
        self.assertIn('【用户健康画像记忆】', request.supplemental_context)
        self.assertIn(memory, request.supplemental_context)
        self.assertNotIn(memory, str(result.data))

    async def test_rejects_missing_required_input(self) -> None:
        context = self._context(request_id='request-1', run_id='run-1')

        with self.assertRaisesRegex(ValueError, 'user_message'):
            await self.hub.invoke(
                CHAT_REPLY_CAPABILITY,
                AgentRequest({'agent_thread_id': 'agent-thread-1'}),
                context,
                thread_id='agent-thread-1',
            )

        with self.assertRaisesRegex(ValueError, 'agent_thread_id'):
            await self.hub.invoke(
                CHAT_REPLY_CAPABILITY,
                AgentRequest({'user_message': '问题'}),
                context,
                thread_id='another-thread',
            )

    async def test_stream_reply_maps_gateway_chunks_to_agent_events(self) -> None:
        gateway = FakeLLMGateway(
            stream_chunks=[
                LLMChunk(reasoning_content='推理一'),
                LLMChunk(reasoning_content='推理二'),
                LLMChunk(content='正文一'),
                LLMChunk(content='正文二'),
            ]
        )
        graph = build_chat_reply_graph(
            gateway=gateway,
            profile=self.profile,
        ).compile(checkpointer=InMemorySaver())
        registry = GraphRegistry()
        registry.register(
            CHAT_REPLY_CAPABILITY,
            graph,
            version=CHAT_REPLY_GRAPH_VERSION,
        )
        hub = AgentHub(registry)

        events = [
            event
            async for event in hub.stream(
                CHAT_REPLY_CAPABILITY,
                AgentRequest(
                    {
                        'agent_thread_id': 'agent-thread-stream-1',
                        'user_message': '流式问题',
                        'thinking': False,
                        'reasoning_effort': 'high',
                        'stream': True,
                    }
                ),
                self._context(
                    request_id='request-stream-1',
                    run_id='run-stream-1',
                ),
                thread_id='agent-thread-stream-1',
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
            ['推理一', '推理二', '正文一', '正文二'],
        )
        self.assertEqual(gateway.complete_requests, [])
        request = gateway.stream_requests[0]
        self.assertIs(request.profile, self.profile)
        self.assertEqual(request.content, '流式问题')
        self.assertEqual(
            request.conversation_key,
            'agent-thread-stream-1',
        )
        self.assertFalse(request.effective_thinking)
        self.assertEqual(request.effective_reasoning_effort, 'high')

    async def test_stream_mode_does_not_leak_into_following_complete_run(
        self,
    ) -> None:
        gateway = FakeLLMGateway(
            complete_result=LLMResult(content='完整回复'),
            stream_chunks=[LLMChunk(content='流式回复')],
        )
        graph = build_chat_reply_graph(
            gateway=gateway,
            profile=self.profile,
        ).compile(checkpointer=InMemorySaver())
        registry = GraphRegistry()
        registry.register(
            CHAT_REPLY_CAPABILITY,
            graph,
            version=CHAT_REPLY_GRAPH_VERSION,
        )
        hub = AgentHub(registry)
        thread_id = 'agent-thread-shared-mode'

        events = [
            event
            async for event in hub.stream(
                CHAT_REPLY_CAPABILITY,
                AgentRequest(
                    {
                        'agent_thread_id': thread_id,
                        'user_message': '第一轮使用流式调用',
                        'stream': True,
                    }
                ),
                self._context(
                    request_id='request-stream',
                    run_id='run-stream',
                ),
                thread_id=thread_id,
            )
        ]
        result = await hub.invoke(
            CHAT_REPLY_CAPABILITY,
            AgentRequest(
                {
                    'agent_thread_id': thread_id,
                    'user_message': '第二轮使用完整调用',
                    'stream': False,
                }
            ),
            self._context(
                request_id='request-complete',
                run_id='run-complete',
            ),
            thread_id=thread_id,
        )

        self.assertEqual(events[-1].type, AgentEventType.RUN_COMPLETED)
        self.assertEqual(result.data['assistant_content'], '完整回复')
        self.assertEqual(
            [request.content for request in gateway.stream_requests],
            ['第一轮使用流式调用'],
        )
        self.assertEqual(
            [request.content for request in gateway.complete_requests],
            ['第二轮使用完整调用'],
        )

    async def test_retrieves_knowledge_only_when_router_requests_it(self) -> None:
        gateway = FakeLLMGateway(
            complete_results=[
                LLMResult(
                    content=('{"needRetrieval":true,"queries":[{"source":"guide","query":"2型糖尿病 二甲双胍"}]}')
                ),
                LLMResult(content='结合指南后的回答 [资料1]'),
            ]
        )
        knowledge = FakeKnowledgeGateway(
            {
                KnowledgeSource.GUIDE: [
                    KnowledgeDocument(
                        source=KnowledgeSource.GUIDE,
                        title='2型糖尿病防治指南',
                        content='指南中的二甲双胍用药建议。',
                        url='https://example.test/guide',
                    )
                ],
            }
        )
        graph = build_chat_reply_graph(
            gateway=gateway,
            profile=self.profile,
            knowledge_gateway=knowledge,
            retrieval_profile=create_knowledge_route_profile(),
        ).compile(checkpointer=InMemorySaver())
        registry = GraphRegistry()
        registry.register(
            CHAT_REPLY_CAPABILITY,
            graph,
            version=CHAT_REPLY_GRAPH_VERSION,
        )

        result = await AgentHub(registry).invoke(
            CHAT_REPLY_CAPABILITY,
            AgentRequest(
                {
                    'agent_thread_id': 'thread-knowledge',
                    'user_message': '二甲双胍有哪些用药注意事项？',
                    'retrieval_query': '二甲双胍有哪些用药注意事项？',
                }
            ),
            self._context(request_id='request-kb', run_id='run-kb'),
            thread_id='thread-knowledge',
        )

        self.assertEqual(result.data['assistant_content'], '结合指南后的回答 [资料1]')
        self.assertEqual(result.data['retrieval_status'], 'retrieved')
        self.assertEqual(len(knowledge.queries), 1)
        self.assertEqual(len(gateway.complete_requests), 2)
        final_request = gateway.complete_requests[1]
        self.assertEqual(final_request.content, '二甲双胍有哪些用药注意事项？')
        self.assertIn('2型糖尿病防治指南', final_request.supplemental_context)

    async def test_knowledge_failure_is_reported_as_unavailable_and_degrades(self) -> None:
        gateway = FakeLLMGateway(
            complete_results=[
                LLMResult(content=('{"needRetrieval":true,"queries":[{"source":"guide","query":"高血压 指南"}]}')),
                LLMResult(content='未取得外部资料时的普通回答'),
            ]
        )
        knowledge = FakeKnowledgeGateway(
            error=RuntimeError('temporary knowledge failure'),
        )
        graph = build_chat_reply_graph(
            gateway=gateway,
            profile=self.profile,
            knowledge_gateway=knowledge,
            retrieval_profile=create_knowledge_route_profile(),
        ).compile(checkpointer=InMemorySaver())
        registry = GraphRegistry()
        registry.register(
            CHAT_REPLY_CAPABILITY,
            graph,
            version=CHAT_REPLY_GRAPH_VERSION,
        )

        result = await AgentHub(registry).invoke(
            CHAT_REPLY_CAPABILITY,
            AgentRequest(
                {
                    'agent_thread_id': 'thread-knowledge-failure',
                    'user_message': '高血压应该如何管理？',
                }
            ),
            self._context(
                request_id='request-knowledge-failure',
                run_id='run-knowledge-failure',
            ),
            thread_id='thread-knowledge-failure',
        )

        self.assertEqual(result.data['retrieval_status'], 'unavailable')
        self.assertEqual(
            result.data['assistant_content'],
            '未取得外部资料时的普通回答',
        )
        self.assertIsNone(gateway.complete_requests[1].supplemental_context)

    async def test_does_not_reuse_previous_turn_knowledge_context(self) -> None:
        gateway = FakeLLMGateway(
            complete_results=[
                LLMResult(content=('{"needRetrieval":true,"queries":[{"source":"guide","query":"高血压指南"}]}')),
                LLMResult(content='第一轮回答'),
                LLMResult(content='{"needRetrieval":false,"queries":[]}'),
                LLMResult(content='第二轮回答'),
            ]
        )
        knowledge = FakeKnowledgeGateway(
            {
                KnowledgeSource.GUIDE: [
                    KnowledgeDocument(
                        source=KnowledgeSource.GUIDE,
                        title='高血压诊疗指南',
                        content='指南摘要。',
                    )
                ],
            }
        )
        graph = build_chat_reply_graph(
            gateway=gateway,
            profile=self.profile,
            knowledge_gateway=knowledge,
            retrieval_profile=create_knowledge_route_profile(),
        ).compile(checkpointer=InMemorySaver())
        registry = GraphRegistry()
        registry.register(
            CHAT_REPLY_CAPABILITY,
            graph,
            version=CHAT_REPLY_GRAPH_VERSION,
        )
        hub = AgentHub(registry)
        thread_id = 'thread-knowledge-follow-up'

        await hub.invoke(
            CHAT_REPLY_CAPABILITY,
            AgentRequest(
                {
                    'agent_thread_id': thread_id,
                    'user_message': '高血压应如何管理？',
                }
            ),
            self._context(request_id='request-kb-1', run_id='run-kb-1'),
            thread_id=thread_id,
        )
        await hub.invoke(
            CHAT_REPLY_CAPABILITY,
            AgentRequest(
                {
                    'agent_thread_id': thread_id,
                    'user_message': '谢谢',
                }
            ),
            self._context(request_id='request-kb-2', run_id='run-kb-2'),
            thread_id=thread_id,
        )

        self.assertIsNotNone(gateway.complete_requests[1].supplemental_context)
        self.assertIsNone(gateway.complete_requests[3].supplemental_context)
        self.assertEqual(len(knowledge.queries), 1)

    async def test_stream_exposes_internal_tool_events_before_model_content(self) -> None:
        gateway = FakeLLMGateway(
            complete_result=LLMResult(
                content=('{"needRetrieval":true,"queries":[{"source":"literature","query":"睡眠 健康"}]}')
            ),
            stream_chunks=[LLMChunk(content='循证回答')],
        )
        knowledge = FakeKnowledgeGateway(
            {
                KnowledgeSource.LITERATURE: [
                    KnowledgeDocument(
                        source=KnowledgeSource.LITERATURE,
                        title='睡眠与健康综述',
                        content='规律睡眠与健康相关。',
                    )
                ],
            }
        )
        graph = build_chat_reply_graph(
            gateway=gateway,
            profile=self.profile,
            knowledge_gateway=knowledge,
            retrieval_profile=create_knowledge_route_profile(),
        ).compile(checkpointer=InMemorySaver())
        registry = GraphRegistry()
        registry.register(
            CHAT_REPLY_CAPABILITY,
            graph,
            version=CHAT_REPLY_GRAPH_VERSION,
        )

        events = [
            event
            async for event in AgentHub(registry).stream(
                CHAT_REPLY_CAPABILITY,
                AgentRequest(
                    {
                        'agent_thread_id': 'thread-kb-stream',
                        'user_message': '睡眠与健康有什么证据？',
                        'stream': True,
                    }
                ),
                self._context(
                    request_id='request-kb-stream',
                    run_id='run-kb-stream',
                ),
                thread_id='thread-kb-stream',
            )
        ]

        self.assertEqual(
            [event.type for event in events],
            [
                AgentEventType.RUN_STARTED,
                AgentEventType.TOOL_STARTED,
                AgentEventType.TOOL_FINISHED,
                AgentEventType.CONTENT_DELTA,
                AgentEventType.RUN_COMPLETED,
            ],
        )
        self.assertIn(
            '睡眠与健康综述',
            gateway.stream_requests[0].supplemental_context,
        )

    def test_rejects_a_profile_without_aion_context(self) -> None:
        invalid_profile = ModelProfile(
            name='chat.reply',
            context_policy=ContextPolicy.STATELESS,
        )

        with self.assertRaisesRegex(AgentConfigurationError, 'AION_FULL'):
            build_chat_reply_graph(
                gateway=self.gateway,
                profile=invalid_profile,
            )


if __name__ == '__main__':
    unittest.main()
