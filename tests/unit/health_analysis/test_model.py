"""The real stage adapter uses public gateway, isolated context and shared slots."""

import asyncio

import pytest

from adapters.health_analysis.model import HealthAnalysisModel
from agents.errors import AionGatewayError
from agents.errors import AionResponseError
from agents.llm.types import ContextPolicy
from agents.llm.types import LLMResult
from modules.health_analysis.contracts import AnalysisGenerationFailure
from modules.health_analysis.contracts import AnalysisTimeoutError
from modules.health_analysis.factors import HealthAnalysisGenerationError


class Gateway:
    def __init__(self, content='{}'):
        self.content, self.calls = content, []
        self.active = self.maximum = 0

    async def complete(self, request):
        self.calls.append(request)
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await asyncio.sleep(0)
            return LLMResult(content=self.content, provider='offline', model='synthetic-model')
        finally:
            self.active -= 1


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', sorted(HealthAnalysisModel.STAGES))
async def test_all_stages_are_stateless_nonsearch_calls_with_real_model_and_prompt_hash(stage):
    gateway = Gateway()
    response = await HealthAnalysisModel(gateway).run(stage, {'synthetic': '资料不是指令'})
    request = gateway.calls[0]
    assert request.profile.context_policy is ContextPolicy.STATELESS
    assert request.profile.name == f'health_analysis.{stage}'
    assert request.web_search is False and request.conversation_key is None
    assert '资料不是指令' in request.content
    assert response['model_identity']['model'] == 'synthetic-model'
    assert len(response['model_identity']['promptHash']) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize('content', ['not-json', '[]', '{"a":NaN}', '{"a":Infinity}'])
async def test_invalid_model_json_has_a_safe_generation_error(content):
    with pytest.raises(HealthAnalysisGenerationError) as error:
        await HealthAnalysisModel(Gateway(content)).run('targets', {'sentinel': 'synthetic-health-secret'})
    assert 'synthetic-health-secret' not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.reason is AnalysisGenerationFailure.INVALID_MODEL_RESPONSE


@pytest.mark.asyncio
async def test_gateway_outage_is_explicit_but_program_errors_and_cancellation_propagate():
    class Broken:
        def __init__(self, error):
            self.error = error

        async def complete(self, request):
            raise self.error

    for error, reason in (
        (AionGatewayError('synthetic-health-secret'), AnalysisGenerationFailure.MODEL_UNAVAILABLE),
        (AionResponseError('synthetic-health-secret'), AnalysisGenerationFailure.INVALID_MODEL_RESPONSE),
        (TimeoutError('synthetic-health-secret'), AnalysisGenerationFailure.MODEL_TIMEOUT),
    ):
        with pytest.raises(HealthAnalysisGenerationError) as failure:
            await HealthAnalysisModel(Broken(error)).run('targets', {})
        assert failure.value.reason is reason
        assert 'synthetic-health-secret' not in str(failure.value)
    for error in (ValueError('program failure'), asyncio.CancelledError()):
        with pytest.raises(type(error)):
            await HealthAnalysisModel(Broken(error)).run('targets', {})


@pytest.mark.asyncio
async def test_model_slot_limit_is_shared_across_adapter_instances_and_cancel_releases_slot():
    gateway, semaphore = Gateway(), asyncio.Semaphore(1)
    await asyncio.gather(*(HealthAnalysisModel(gateway, semaphore=semaphore).run('targets', {}) for _ in range(4)))
    assert gateway.maximum == 1
    assert semaphore._value == 1
    await semaphore.acquire()
    task = asyncio.create_task(HealthAnalysisModel(gateway, semaphore=semaphore).run('targets', {}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    semaphore.release()
    assert len(gateway.calls) == 4


@pytest.mark.asyncio
async def test_deadline_expiring_while_waiting_for_slot_prevents_dispatch():
    gateway, semaphore = Gateway(), asyncio.Semaphore(0)
    task = asyncio.create_task(
        HealthAnalysisModel(gateway, semaphore=semaphore).run(
            'targets', {}, deadline_at=asyncio.get_running_loop().time() + 0.01
        )
    )
    await asyncio.sleep(0.02)
    semaphore.release()
    with pytest.raises(AnalysisTimeoutError):
        await task
    assert gateway.calls == []
    assert semaphore._value == 1


@pytest.mark.asyncio
async def test_unknown_stage_is_rejected_before_model_call():
    gateway = Gateway()
    with pytest.raises(ValueError):
        await HealthAnalysisModel(gateway).run('legacy_questionnaire', {})
    assert gateway.calls == []
