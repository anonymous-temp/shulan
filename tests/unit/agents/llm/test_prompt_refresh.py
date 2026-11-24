"""旧会话必须使用当前部署的系统提示，同时保留已完成历史。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from agents.errors import AionGatewayError
from agents.llm import ContextPolicy
from agents.llm import LLMRequest
from agents.llm import ModelProfile
from tests.unit._support.agents import InMemoryLLMContextRepository
from tests.unit.agents.llm import test_aion_llm_gateway as gateway_fakes
from tests.unit.agents.llm.test_aion_llm_gateway import RecordingAionFactory
from tests.unit.agents.llm.test_aion_llm_gateway import _empty_context


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('failure', [False, True])
async def test_deployed_prompt_replaces_archived_prompt_without_losing_history(stream, failure):
    helper = gateway_fakes.AionLLMGatewayTest()
    helper.setUp()
    repository = InMemoryLLMContextRepository()
    key = 'validation.stateful:test-conversation'
    archived = _empty_context('你是一个随意聊天的助手')
    archived['turns'] = [
        {
            'user': {'role': 'user', 'content': '我最近睡眠不太好'},
            'assistant': {'role': 'assistant', 'content': '大概持续多久了？'},
        }
    ]
    original = deepcopy(archived)
    await repository.save(key, archived, expected_revision=0)
    failure_result = RuntimeError('test provider unavailable')
    chunk = SimpleNamespace(content='您好，最近睡眠有改善吗？', reasoning_content=None, usage=None)
    client = helper._client(
        context=True,
        result=failure_result if failure else None,
        chunks=(chunk, failure_result) if failure else (chunk,),
    )
    gateway = helper._gateway(RecordingAionFactory([client]), repository=repository)
    request = LLMRequest(
        profile=helper.stateful_profile,
        content='你好',
        conversation_key='test-conversation',
    )

    async def invoke():
        if stream:
            return [part async for part in gateway.stream(request)]
        return await gateway.complete(request)

    if failure:
        with pytest.raises(AionGatewayError, match='AION'):
            await invoke()
    else:
        await invoke()

    imported = client.imported_payloads[0]
    assert imported['system']['content'] == '渲染后的系统提示'
    assert imported['turns'][:1] == original['turns']
    assert archived == original
    snapshot = await repository.load(key)
    if failure:
        assert snapshot.revision == 1
        assert snapshot.payload == original
    else:
        assert snapshot.revision == 2
        assert snapshot.payload['system'] == imported['system']
        assert len(snapshot.payload['turns']) == 2


@pytest.mark.asyncio
async def test_profile_without_configured_prompt_preserves_its_archived_system():
    helper = gateway_fakes.AionLLMGatewayTest()
    repository = InMemoryLLMContextRepository()
    key = 'test.unconfigured:test-conversation'
    await repository.save(key, _empty_context('已有系统消息'), expected_revision=0)
    client = helper._client(context=True)
    gateway = helper._gateway(RecordingAionFactory([client]), repository=repository)

    await gateway.complete(
        LLMRequest(
            profile=ModelProfile(name='test.unconfigured', context_policy=ContextPolicy.AION_FULL),
            content='你好',
            conversation_key='test-conversation',
        )
    )

    snapshot = await repository.load(key)
    assert snapshot.payload['system']['content'] == '已有系统消息'
    assert snapshot.payload['turns'][0]['user']['content'] == '你好'
