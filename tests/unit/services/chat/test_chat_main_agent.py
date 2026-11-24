"""Whole Chat task ownership, version pinning and legacy identity compatibility."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from agents import AgentHub
from agents import AgentRunContext
from agents import AgentSelection
from agents import GraphRegistry
from agents.errors import AgentConfigurationError
from agents.errors import AgentProtocolError
from agents.persistence import LLMContextSnapshot
from modules.chat.agent import CHAT_MAIN_SELECTION
from modules.chat.agent import bind_chat_session
from modules.chat.agent import chat_agent_manifest
from modules.chat.agent import install_chat_agent
from tests.unit.services.chat import test_chat_service_agent as legacy


def manager_for(document=None, **kwargs):
    collection = legacy.FakeSessionCollection(document)
    manager, graph, leases, contexts = legacy.ChatSessionManagerAgentTest()._manager(collection, **kwargs)
    return manager, collection, graph, leases, contexts


def existing_session():
    return {
        'userId': 'u',
        'sessionId': 's',
        'type': 'chat',
        'status': 'active',
        'createdAt': '2026-11-09T00:00:00',
        'lastMessageAt': '2026-11-09T00:00:00',
        'agentThreadId': 'legacy-child-thread',
        'agentContextInitialized': True,
        'messages': [{'role': 'user', 'content': 'old question'}, {'role': 'assistant', 'content': 'old answer'}],
    }


class RecordingMain:
    def __init__(self, port):
        self.port = port
        self.calls = []

    async def invoke(self, request, context, *, thread_id=None):
        self.calls.append((deepcopy(dict(request.payload)), context, thread_id))
        return await self.port.invoke(request, context, thread_id=thread_id)

    async def stream(self, request, context, *, thread_id=None):
        self.calls.append((deepcopy(dict(request.payload)), context, thread_id))
        async for event in self.port.stream(request, context, thread_id=thread_id):
            yield event


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_complete_task_uses_main_and_preserves_legacy_child_identity(stream):
    snapshot = LLMContextSnapshot(key='chat.reply:legacy-child-thread', revision=1, payload={})
    manager, collection, graph, leases, contexts = manager_for(existing_session(), context_snapshots=[snapshot])
    spy = RecordingMain(manager.agent_port)
    manager._main_agent = spy
    if stream:
        responses = [event async for event in manager.sendMessageStream('u', 's', 'new question')]
        assert [item['data']['type'] for item in responses] == [
            'start',
            'reasoning',
            'reasoning',
            'content',
            'content',
            'finish',
        ]
        content = ''.join(item['data']['content'] for item in responses if item['data']['type'] == 'content')
        reasoning = ''.join(item['data']['content'] for item in responses if item['data']['type'] == 'reasoning')
        call = graph.stream_calls[0]
        assert all('chatAgentManifest' not in str(item) for item in responses)
    else:
        response = await manager.sendMessage('u', 's', 'new question')
        content, reasoning = response['reply'], response['reasoning']
        call = graph.calls[0]
    assert len(spy.calls) == 1
    assert call['input']['user_message'] == 'new question'
    assert call['config']['configurable']['thread_id'] == 'legacy-child-thread'
    assert contexts.load_calls == ['chat.reply:legacy-child-thread']
    assert collection.document['agentThreadId'] == 'legacy-child-thread'
    assert collection.document['chatAgentManifest'] == chat_agent_manifest()
    execution = collection.document['chatAgentExecution']
    assert execution['mainThreadId'].startswith('agent-v1:')
    assert execution['runId'] == spy.calls[0][1].run_id == call['context'].run_id
    assert collection.document['messages'][-1]['content'] == content
    assert collection.document['messages'][-1]['reasoning'] == reasoning
    assert len(leases.release_calls) == 1


@pytest.mark.asyncio
async def test_old_session_uses_explicit_v5_even_when_deployment_alias_changes():
    manager, collection, graph, _, _ = manager_for(existing_session())
    other = legacy.RecordingChatGraph()
    registry = GraphRegistry()
    registry.register('chat.reply', other, version='chat-reply-v99')
    registry.register('chat.reply', graph, version='chat-reply-v5', legacy=False)
    manager._agent_runtime.registry = registry
    manager._agent_runtime.hub = AgentHub(registry)
    await manager.sendMessage('u', 's', 'continue')
    assert len(graph.calls) == 1 and not other.calls
    assert collection.document['chatAgentManifest']['reply']['implementation_revision'] == 'chat-reply-v5'


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_unsupported_session_manifest_is_not_rebound(stream):
    original = existing_session()
    original['chatAgentManifest'] = {'contractVersion': 'future'}
    manager, collection, graph, leases, _ = manager_for(original)
    if stream:
        events = [event async for event in manager.sendMessageStream('u', 's', 'continue')]
        assert events[-1]['data']['type'] == 'error'
    else:
        with pytest.raises(AgentConfigurationError):
            await manager.sendMessage('u', 's', 'continue')
    assert not graph.calls and not graph.stream_calls
    assert collection.document == original
    assert len(leases.release_calls) == 1


@pytest.mark.asyncio
async def test_suggestions_run_whole_task_and_share_main_run_identity():
    manager, _, graph, _, _ = manager_for(
        {'userId': 'u', 'sessionId': 'qa', 'type': 'qa', 'status': 'completed', 'qaHistory': []}
    )
    spy = RecordingMain(manager.agent_port)
    manager._main_agent = spy
    assert len(await manager.getSuggestions('u', 2)) == 2
    assert spy.calls[0][0]['operation'] == 'suggestions'
    assert graph.calls[0]['context'].run_id == spy.calls[0][1].run_id
    assert graph.calls[0]['context'].session_id == 'qa'


@pytest.mark.asyncio
async def test_history_anonymous_suggestions_and_custom_messages_bypass_main():
    manager, _, graph, _, _ = manager_for(existing_session())
    manager._main_agent = AsyncMock()
    assert await manager.getHistory('u', 's')
    assert len(await manager.getSuggestions(count=2)) == 2
    await manager.insertCustomMessage('u', 's', event_id='notice', content='display only')
    manager._main_agent.invoke.assert_not_called()
    manager._main_agent.stream.assert_not_called()
    assert not graph.calls and not graph.stream_calls


@pytest.mark.asyncio
async def test_public_agent_scope_cannot_override_its_context():
    manager, collection, graph, _, _ = manager_for()
    request = manager._chatReplyRequest('other-user', 's', 'x', True, 'low', None, None)
    with pytest.raises(AgentProtocolError, match='scope'):
        await manager.agent_port.invoke(request, AgentRunContext.create(user_id='u', session_id='s'))
    assert not graph.calls and collection.document is None


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['missing_child', 'port_conflict'])
async def test_install_failure_blocks_all_ai_before_context_work_but_plain_operations_work(failure):
    manager, _, graph, leases, contexts = manager_for(existing_session())
    runtime = manager._agent_runtime
    stale_port = manager.agent_port
    if failure == 'missing_child':
        runtime.registry = GraphRegistry()
        runtime.hub = AgentHub(runtime.registry)
    else:
        runtime.ports.register(CHAT_MAIN_SELECTION, manager.agent_port)
    manager._withReportContext = AsyncMock(side_effect=AssertionError('context model must not run'))
    assert install_chat_agent(runtime, manager) is False
    assert runtime.module_statuses['chat.main']['ready'] is False
    assert await manager.getHistory('u', 's')
    assert len(await manager.getSuggestions(count=2)) == 2
    await manager.insertCustomMessage('u', 's', event_id='notice', content='display only')
    with pytest.raises(AgentConfigurationError, match='unavailable'):
        await manager.sendMessage('u', 's', 'x')
    with pytest.raises(AgentConfigurationError, match='unavailable'):
        await anext(manager.sendMessageStream('u', 's', 'x'))
    with pytest.raises(AgentConfigurationError, match='unavailable'):
        await manager.getSuggestions('u', 2)
    request = manager._chatReplyRequest('u', 's', 'x', True, 'low', None, None)
    context = AgentRunContext.create(user_id='u', session_id='s')
    with pytest.raises(AgentConfigurationError, match='unavailable'):
        await stale_port.invoke(request, context)
    with pytest.raises(AgentConfigurationError, match='unavailable'):
        await anext(stale_port.stream(request, context))
    manager._withReportContext.assert_not_awaited()
    assert not graph.calls and not graph.stream_calls and not contexts.load_calls


@pytest.mark.parametrize(
    ('constant', 'selection'),
    [
        ('CHAT_MAIN_SELECTION', AgentSelection('chat.main', 'chat-main-v2')),
        ('CHAT_REPLY_SELECTION', AgentSelection('chat.reply', 'chat-reply-v6')),
        ('CHAT_SUGGESTIONS_SELECTION', AgentSelection('chat.suggestions', 'chat-suggestions-v2')),
    ],
)
def test_unversioned_legacy_session_does_not_follow_future_main_or_child_versions(monkeypatch, constant, selection):
    from modules.chat import agent

    monkeypatch.setattr(agent, constant, selection)
    session = existing_session()
    with pytest.raises(AgentConfigurationError, match='unavailable'):
        bind_chat_session(session, {})
    assert 'chatAgentManifest' not in session


def test_main_is_installed_as_a_public_port_with_exact_manifest():
    manager, _, _, _, _ = manager_for()
    runtime = manager._agent_runtime
    assert install_chat_agent(runtime, manager)
    assert runtime.ports.get(CHAT_MAIN_SELECTION) is manager.agent_port


@pytest.mark.asyncio
async def test_model_failure_keeps_old_session_and_releases_business_lease():
    original = existing_session()
    manager, collection, graph, leases, _ = manager_for(original)
    graph.ainvoke = AsyncMock(side_effect=RuntimeError('model failed'))
    with pytest.raises(Exception, match='AI 回复失败'):
        await manager.sendMessage('u', 's', 'x')
    assert collection.document == original
    assert len(leases.release_calls) == 1


@pytest.mark.asyncio
async def test_closing_main_stream_between_events_releases_lease_immediately():
    manager, _, graph, leases, _ = manager_for()
    original = graph.astream
    closed = []

    async def tracked(*args, **kwargs):
        try:
            async for event in original(*args, **kwargs):
                yield event
        finally:
            closed.append(True)

    graph.astream = tracked
    stream = manager.sendMessageStream('u', 's', 'x')
    assert (await anext(stream))['data']['type'] == 'start'
    await stream.aclose()
    assert len(leases.release_calls) == 1
    assert closed == [True]


@pytest.mark.asyncio
async def test_parallel_users_do_not_share_main_execution_metadata():
    manager, collection, graph, _, _ = manager_for()
    original = graph.ainvoke
    entered = asyncio.Event()
    waiting = []

    async def overlap(*args, **kwargs):
        waiting.append(True)
        if len(waiting) == 2:
            entered.set()
        await entered.wait()
        return await original(*args, **kwargs)

    graph.ainvoke = overlap
    await asyncio.gather(manager.sendMessage('u1', 's', 'one'), manager.sendMessage('u2', 's', 'two'))
    saved = {call['document']['userId']: call['document'] for call in collection.replace_calls}
    assert set(saved) == {'u1', 'u2'}
    assert saved['u1']['chatAgentExecution']['mainThreadId'] != saved['u2']['chatAgentExecution']['mainThreadId']
    assert saved['u1']['agentThreadId'] != saved['u2']['agentThreadId']
    for call in graph.calls:
        assert saved[call['context'].user_id]['chatAgentExecution']['runId'] == call['context'].run_id


@pytest.mark.asyncio
async def test_cancelling_main_stream_while_waiting_releases_lease():
    manager, _, graph, leases, _ = manager_for()
    entered = asyncio.Event()

    async def wait_forever(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
        yield

    graph.astream = wait_forever
    stream = manager.sendMessageStream('u', 's', 'x')
    task = asyncio.create_task(anext(stream))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(leases.release_calls) == 1
