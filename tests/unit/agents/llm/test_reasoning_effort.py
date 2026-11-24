"""Exercise reasoning settings through the installed AION and SDK, offline."""

import pytest

from agents.errors import LLMCapabilityNotSupportedError
from agents.llm import AionLLMGateway
from agents.llm import ContextPolicy
from agents.llm import LLMRequest
from agents.llm import ModelProfile

from . import test_web_search

aion_wire = test_web_search.aion_wire


def profile(**kwargs):
    return ModelProfile(name='effort.test', context_policy=ContextPolicy.STATELESS, **kwargs)


async def call(gateway, request, streaming):
    if streaming:
        return [chunk async for chunk in gateway.stream(request)]
    return await gateway.complete(request)


def test_default_profile_request_and_legacy_precedence():
    assert LLMRequest(profile=profile(), content='test').effective_reasoning_effort == 'low'
    configured = profile(reasoning_effort='high')
    assert LLMRequest(profile=configured, content='test').effective_reasoning_effort == 'high'
    assert LLMRequest(profile=configured, content='test', reasoning_effort='max').effective_reasoning_effort == 'max'
    legacy = profile(model_kwargs={'reasoning_effort': 'high'})
    assert LLMRequest(profile=legacy, content='test').effective_reasoning_effort == 'high'
    assert LLMRequest(profile=legacy, content='test', reasoning_effort='low').effective_reasoning_effort == 'low'


@pytest.mark.parametrize('invalid', ['', 'medium', 'none', 'LOW', 1, True, [], {}])
def test_invalid_effort_rejected(invalid):
    with pytest.raises(ValueError, match='reasoning_effort'):
        profile(reasoning_effort=invalid)
    with pytest.raises(ValueError, match='reasoning_effort'):
        LLMRequest(profile=profile(), content='test', reasoning_effort=invalid)
    with pytest.raises(ValueError, match='reasoning_effort'):
        profile(model_kwargs={'reasoning_effort': invalid})


def test_extra_body_cannot_override_effort():
    with pytest.raises(ValueError, match='extra_body'):
        profile(model_kwargs={'extra_body': {'reasoning_effort': 'max'}})


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('thinking', [False, True])
@pytest.mark.parametrize('effort', [None, 'low', 'high', 'max'])
async def test_real_aion_wire_effort_and_thinking(aion_wire, streaming, thinking, effort):
    gateway = AionLLMGateway(provider='DeepSeek', api_key='offline-test', default_model='deepseek-flash')
    await call(
        gateway,
        LLMRequest(
            profile=profile(thinking=thinking),
            content='test',
            reasoning_effort=effort,
        ),
        streaming,
    )
    body = aion_wire.requests[-1]
    assert body['thinking'] == {'type': 'enabled' if thinking else 'disabled'}
    if thinking:
        assert body['reasoning_effort'] == (effort or 'low')
    else:
        assert 'reasoning_effort' not in body


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_request_overrides_legacy_setting_on_wire(aion_wire, streaming):
    gateway = AionLLMGateway(provider='DeepSeek', api_key='offline-test', default_model='deepseek-flash')
    await call(
        gateway,
        LLMRequest(
            profile=profile(thinking=True, model_kwargs={'reasoning_effort': 'max'}),
            content='test',
            reasoning_effort='low',
        ),
        streaming,
    )
    assert aion_wire.requests[-1]['reasoning_effort'] == 'low'


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_explicit_effort_requires_provider_support(aion_wire, streaming):
    gateway = AionLLMGateway(provider='Aliyun', api_key='offline-test', default_model='test-model')
    with pytest.raises(LLMCapabilityNotSupportedError, match='reasoning_effort'):
        await call(
            gateway,
            LLMRequest(
                profile=profile(thinking=True),
                content='test',
                reasoning_effort='low',
            ),
            streaming,
        )
    assert aion_wire.requests == []
