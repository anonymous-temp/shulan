"""Successful Chat replies require an acknowledged business-session save."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.unit.services.chat.test_chat_main_agent import manager_for
from tests.unit.services.chat.test_chat_service_agent import FakeChatProfileSource


def environment():
    profile = FakeChatProfileSource()
    manager, collection, graph, leases, _ = manager_for(chat_profile_source=profile)
    return SimpleNamespace(manager=manager, collection=collection, graph=graph, leases=leases, profile=profile)


def fail_save(env, mode):
    original = env.collection.replace_one

    async def replace(*args, **kwargs):
        if mode == 'false':
            return SimpleNamespace(acknowledged=False)
        if mode == 'lost_ack':
            await original(*args, **kwargs)
        raise OSError('session write acknowledgement unavailable')

    env.collection.replace_one = AsyncMock(side_effect=replace)


async def collect(env, responses):
    async for item in env.manager.sendMessageStream('test-user', 'test-session', 'original user message'):
        responses.append(item)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['false', 'exception', 'lost_ack'])
async def test_save_failure_ends_stream_with_existing_error_without_success_or_queue(mode):
    env = environment()
    fail_save(env, mode)
    responses = []
    await collect(env, responses)
    assert [item['data']['type'] for item in responses] == [
        'start',
        'reasoning',
        'reasoning',
        'content',
        'content',
        'error',
    ]
    error = responses[-1]
    assert error['code'] == 500 and error['message'] == '服务器内部异常'
    assert set(error) == {'code', 'message', 'uuid', 'timestamp', 'elapsed_ms', 'data'}
    assert set(error['data']) == {'type', 'error', 'userId', 'sessionId'}
    assert len({item['uuid'] for item in responses}) == 1
    assert not env.profile.queue_calls
    assert len(env.graph.stream_calls) == env.collection.replace_one.await_count == 1
    assert len(env.leases.release_calls) == 1
    if mode == 'lost_ack':
        # Unknown acknowledgement must not pretend the data was rolled back.
        assert env.collection.document['messages'][-1]['content'] == 'Agent 正文一Agent 正文二'
    else:
        assert env.collection.document is None


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['false', 'exception', 'lost_ack'])
async def test_non_stream_existing_failure_contract_never_returns_a_successful_reply(mode):
    env = environment()
    fail_save(env, mode)
    with pytest.raises(Exception, match='保存会话失败|acknowledgement unavailable'):
        await env.manager.sendMessage('test-user', 'test-session', 'original user message')
    assert not env.profile.queue_calls
    assert len(env.graph.calls) == env.collection.replace_one.await_count == 1
    assert len(env.leases.release_calls) == 1
    assert bool(env.collection.document) == (mode == 'lost_ack')


@pytest.mark.asyncio
async def test_finish_waits_for_save_ack_and_matches_complete_persisted_content_and_reasoning():
    env = environment()
    entered, release = asyncio.Event(), asyncio.Event()
    replace = env.collection.replace_one
    frozen = []

    async def save(*args, **kwargs):
        frozen.append(deepcopy(args[1]))
        entered.set()
        await release.wait()
        return await replace(*args, **kwargs)

    env.collection.replace_one = AsyncMock(side_effect=save)
    responses = []
    task = asyncio.create_task(collect(env, responses))
    await entered.wait()
    assert not task.done() and env.collection.document is None
    assert responses[-1]['data']['type'] == 'content'
    assert not env.profile.queue_calls
    release.set()
    await task
    assert responses[-1]['data']['type'] == 'finish' and responses[-1]['code'] == 200
    assert not any(item['data']['type'] == 'error' for item in responses)
    saved = env.collection.document
    assert saved == frozen[0]
    assert saved['messages'][-2]['content'] == 'original user message'
    for event_type, field in [('content', 'content'), ('reasoning', 'reasoning')]:
        text = ''.join(item['data']['content'] for item in responses if item['data']['type'] == event_type)
        assert text and saved['messages'][-1][field] == text
    assert len(env.profile.queue_calls) == len(env.graph.stream_calls) == 1
    assert len(env.leases.release_calls) == 1


@pytest.mark.asyncio
async def test_non_stream_success_still_returns_the_exact_saved_reply():
    env = environment()
    result = await env.manager.sendMessage('test-user', 'test-session', 'original user message')
    saved = env.collection.document['messages'][-1]
    assert result['reply'] == saved['content'] and result['reasoning'] == saved['reasoning']
    assert len(env.profile.queue_calls) == len(env.graph.calls) == 1


@pytest.mark.asyncio
async def test_cancellation_during_save_does_not_emit_a_terminal_success_or_enqueue_profile():
    env = environment()
    entered = asyncio.Event()

    async def save(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    env.collection.replace_one = AsyncMock(side_effect=save)
    responses = []
    task = asyncio.create_task(collect(env, responses))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert responses[-1]['data']['type'] == 'content'
    assert not any(item['data']['type'] in {'finish', 'error'} for item in responses)
    assert env.collection.document is None and not env.profile.queue_calls
    assert len(env.leases.release_calls) == 1
    assert len(env.graph.stream_calls) == 1
