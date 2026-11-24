"""专业健康对话上下文在 AgentHub、Graph 和 AION 间的隔离契约。"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents import GraphRegistry
from agents.graphs.chat import CHAT_REPLY_CAPABILITY
from agents.graphs.chat import build_chat_reply_graph
from agents.graphs.chat import create_chat_reply_profile
from agents.llm import AionLLMGateway
from tests.unit._support.agents import InMemoryLLMContextRepository
from tests.unit.agents.llm.test_aion_llm_gateway import MockAionClient
from tests.unit.agents.llm.test_aion_llm_gateway import RecordingAionFactory
from tests.unit.agents.llm.test_aion_llm_gateway import _empty_context


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_health_context_reaches_model_for_greeting_and_is_not_retained(stream):
    # 真实 Gateway/Graph，假 AION Client；不访问模型服务或用户数据库。
    repository = InMemoryLLMContextRepository()
    key = 'chat.reply:test-thread'
    await repository.save(key, _empty_context('旧闲聊角色'), expected_revision=0)
    clients = [
        MockAionClient(
            model='test-model',
            system_content='旧闲聊角色',
            context=True,
            stream_chunks=(SimpleNamespace(content='测试回复', reasoning_content=None, usage=None),),
        )
        for _ in range(2)
    ]
    prompt_root = Path(__file__).resolve().parents[5] / 'prompts'
    gateway = AionLLMGateway(
        provider='test-provider',
        api_key='test-unused',
        context_repository=repository,
        client_factory=RecordingAionFactory(clients),
        prompt_renderer=lambda name, **_: (prompt_root / name).read_text(encoding='utf-8'),
    )
    checkpointer = InMemorySaver()
    graph = build_chat_reply_graph(gateway=gateway, profile=create_chat_reply_profile()).compile(
        checkpointer=checkpointer,
    )
    registry = GraphRegistry()
    registry.register(CHAT_REPLY_CAPABILITY, graph, version='test-guidance')
    hub = AgentHub(registry)
    session_data = '测试独有建档线索：用户曾自述熬夜'
    memory = '测试独有画像线索：status=pending，睡眠待确认'

    for index in range(2):
        context = AgentRunContext.create(
            user_id='test-user',
            session_id='test-session',
            session_health_context=session_data if index == 0 else None,
            health_profile_context=memory if index == 0 else None,
        )
        request = AgentRequest(
            {
                'agent_thread_id': 'test-thread',
                'user_message': '你好',
                'stream': stream,
            }
        )
        if stream:
            result = [
                event
                async for event in hub.stream(
                    CHAT_REPLY_CAPABILITY,
                    request,
                    context,
                    thread_id='test-thread',
                )
            ]
        else:
            result = await hub.invoke(CHAT_REPLY_CAPABILITY, request, context, thread_id='test-thread')
        model_call = (clients[index].stream_calls if stream else clients[index].complete_calls)[0]
        assert model_call['content'].startswith('你好')
        if index == 0:
            assert session_data in model_call['content']
            assert memory in model_call['content']
        else:
            assert model_call['content'] == '你好'
        assert session_data not in repr(context)
        assert memory not in repr(context)
        assert session_data not in str(result)
        assert memory not in str(result)
        expected_prompt = (prompt_root / 'chat/chat_system.txt').read_text(encoding='utf-8')
        assert clients[index].context_payload['system']['content'] == expected_prompt

    snapshot = await repository.load(key)
    assert [turn['user']['content'] for turn in snapshot.payload['turns']] == ['你好', '你好']
    assert session_data not in str(snapshot.payload)
    assert memory not in str(snapshot.payload)
    # 不仅最终 State：全部中间 checkpoint 都不能持久化临时健康资料。
    async for checkpoint in checkpointer.alist({'configurable': {'thread_id': 'test-thread'}}):
        assert session_data not in str(checkpoint)
        assert memory not in str(checkpoint)


@pytest.mark.parametrize('value', ['', '   ', 123, {}, []])
def test_session_health_context_rejects_invalid_data(value):
    with pytest.raises((TypeError, ValueError)):
        AgentRunContext.create(
            user_id='test-user',
            session_id='test-session',
            session_health_context=value,
        )
