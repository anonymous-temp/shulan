"""Peer GutBarrier main Agent preserves deterministic results and durable boundaries."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from modules.gut_barrier.agent import GUT_BARRIER_MAIN_SELECTION
from modules.gut_barrier.agent import GutBarrierAgent
from modules.gut_barrier.agent import install_gut_barrier_agent
from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.rule_tools import RULE_TOOL_VERSIONS
from modules.gut_barrier.rule_tools import GutBarrierRuleTools
from modules.gut_barrier.rule_tools import RuleBinding
from tests.unit.gut_barrier.test_service import environment
from tests.unit.gut_barrier.test_service import request


@pytest.mark.asyncio
async def test_whole_pipeline_runs_in_main_agent_and_delivery_never_reenters_it():
    env = environment()
    env.service.main_agent = agent = GutBarrierAgent(env.pipeline, timeout_seconds=1800)
    job = await env.service.submit(request())
    assert job['executionManifest'] == agent.execution_manifest()
    env.pipeline.generate.assert_not_awaited()
    await env.service.drain_generation()
    saved = env.collection.documents[job['_id']]
    assert saved['status'] == 'success'
    env.pipeline.generate.assert_awaited_once()
    agent.generate = AsyncMock(side_effect=AssertionError('delivery started Agent'))
    await env.service.drain_delivery()
    agent.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_manifest_fails_and_started_generation_seals_without_pipeline():
    env = environment()
    old = await env.service.submit(request())
    env.service.main_agent = GutBarrierAgent(env.pipeline, timeout_seconds=1800)
    await env.service.drain_generation()
    assert env.collection.documents[old['_id']]['body']['error']['code'] == 'GUT_BARRIER_EXECUTION_VERSION_UNAVAILABLE'
    env.pipeline.generate.assert_not_awaited()
    new = await env.service.submit(request(value='20'))
    await env.store.claim_generation()
    env.clock.advance(121)
    await env.service.recover_pending()
    assert env.collection.documents[new['_id']]['body']['error']['code'] == 'GENERATION_INTERRUPTED'
    env.pipeline.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_manifest_cannot_replace_existing_admission():
    env = environment()
    env.service.main_agent = GutBarrierAgent(env.pipeline, timeout_seconds=1800, dependencies={'v': '1'})
    old = await env.service.submit(request())
    accepted = deepcopy(old['executionManifest'])
    env.service.main_agent = GutBarrierAgent(env.pipeline, timeout_seconds=1800, dependencies={'v': '2'})
    replay = await env.service.submit(request())
    assert replay['_id'] == old['_id'] and replay['executionManifest'] == accepted
    await env.service.drain_generation()
    env.pipeline.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_rules_keep_calculation_output_and_archive_only_internal_receipts():
    pipeline = GutBarrierPipeline()
    report, audit = await pipeline.generate(request(), report_id='same', evaluation_date='2026-01-01')
    agent = GutBarrierAgent(pipeline, timeout_seconds=1800)
    result, main_audit = await agent.generate(
        {
            '_id': 'same',
            'userId': 'u',
            'sessionId': 's',
            'request': request().model_dump(mode='json', by_alias=True),
            'executionManifest': agent.execution_manifest(),
        },
        evaluation_date='2026-01-01',
    )
    assert result == report
    receipts = main_audit.pop('ruleToolReceipts')
    main_audit.pop('agentExecution')
    audit.pop('ruleToolReceipts')
    assert main_audit == audit
    assert len(receipts) == 7 and all(row['status'] == 'completed' for row in receipts)
    assert all(row['runId'] == 'gut-main:same' for row in receipts)
    assert not any('ruleToolReceipts' in str(section) for section in result['sections'])


@pytest.mark.asyncio
async def test_rule_permissions_call_ids_budget_and_parameters_are_checked_before_dispatch():
    seen = []

    def handler(value):
        seen.append(value)
        return {'value': value}

    bindings = {'assess': RuleBinding(RULE_TOOL_VERSIONS['assess'], handler)}
    tools = GutBarrierRuleTools(bindings, run_id='run', allowed={'assess'}, max_calls=1)
    with pytest.raises(PermissionError):
        await tools.invoke('review', {}, call_id='a')
    with pytest.raises(TypeError):
        await tools.invoke('assess', wrong=1, call_id='a')
    assert not seen
    assert await tools.invoke('assess', 1, call_id='a') == {'value': 1}
    with pytest.raises(ValueError):
        await tools.invoke('assess', 2, call_id='a')
    with pytest.raises(RuntimeError, match='budget'):
        await tools.invoke('assess', 2, call_id='b')
    assert seen == [1]


def test_main_registration_is_peer_port_and_has_no_global_supervisor():
    from tests.unit.agents.core.test_versioned_agent_ports import runtime

    instance, closed = runtime()
    env = environment()
    env.service.main_agent = GutBarrierAgent(env.pipeline, timeout_seconds=1800)
    assert install_gut_barrier_agent(instance, env.service)
    assert instance.ports.get(GUT_BARRIER_MAIN_SELECTION) is env.service.main_agent.port
    assert instance.registry.capabilities() == () and not closed


@pytest.mark.asyncio
async def test_registration_failure_preserves_delivery_and_prevents_generation():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from modules.gut_barrier.agent import GutBarrierExecutionVersionError

    env = environment()
    env.service.main_agent = GutBarrierAgent(env.pipeline, timeout_seconds=1800)
    job = await env.service.submit(request())
    worker = SimpleNamespace(stop_accepting=Mock())
    env.service.callback_worker = worker
    assert not install_gut_barrier_agent(SimpleNamespace(install_module=lambda *a: False), env.service)
    with pytest.raises(RuntimeError, match='draining'):
        await env.service.submit(request())
    with pytest.raises(GutBarrierExecutionVersionError):
        await env.service.main_agent.generate(job, evaluation_date='2026-01-01')
    worker.stop_accepting.assert_not_called()
    env.pipeline.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_rule_cancellation_and_invalid_result_do_not_publish_success_receipts():
    import asyncio

    async def cancelled(value):
        raise asyncio.CancelledError

    tools = GutBarrierRuleTools({'assess': RuleBinding(RULE_TOOL_VERSIONS['assess'], cancelled)}, run_id='r')
    with pytest.raises(asyncio.CancelledError):
        await tools.invoke('assess', {}, call_id='first')
    assert tools.audit()[0]['status'] == 'cancelled'
    tools = GutBarrierRuleTools({'assess': RuleBinding(RULE_TOOL_VERSIONS['assess'], lambda: 'wrong')}, run_id='r')
    with pytest.raises(TypeError, match='result'):
        await tools.invoke('assess', call_id='first')
    assert tools.audit()[0]['status'] == 'failed'


@pytest.mark.asyncio
async def test_admission_detaches_manifest_before_any_await():
    env = environment()
    manifest = {'contractVersion': 'fixed', 'nested': {'v': 'original'}}
    ensure = env.store.ensure_indexes

    async def mutate():
        manifest['nested']['v'] = 'mutated'
        await ensure()

    env.store.ensure_indexes = mutate
    job = await env.store.admit(
        fingerprint='fixed',
        request=request().model_dump(by_alias=True),
        callback_url='',
        version='fixed',
        execution_manifest=manifest,
    )
    assert job['executionManifest']['nested']['v'] == 'original'
