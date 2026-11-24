"""Whole Report workflow admission is fixed and queries never invoke its Agent."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from modules.report.agent import ReportAgent
from tests.unit.report.test_durable_reports import REQUEST
from tests.unit.report.test_durable_reports import setup


@pytest.mark.asyncio
async def test_main_agent_executes_existing_pipeline_once_and_query_only_reads():
    env = setup()
    agent = ReportAgent(env.pipeline)
    env.service.main_agent = agent
    original = agent.port.runner
    calls = []

    async def record(request, context, **kwargs):
        calls.append((context, kwargs))
        return await original(request, context, **kwargs)

    from dataclasses import replace

    agent.port = replace(agent.port, runner=record)
    job = await env.service.submit(REQUEST)
    assert job['executionManifest'] == agent.execution_manifest()
    assert env.pipeline.calls == 0
    await env.service.drain_generation()
    assert env.pipeline.calls == 1 and len(calls) == 1
    assert calls[0][1]['thread_id'].startswith('agent-v1:')
    agent.execute = AsyncMock(side_effect=AssertionError('query entered Agent'))
    assert await env.workflow.artifacts_for(job['_id'])
    agent.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_config_cannot_replace_accepted_manifest_or_replay_completed_artifacts():
    env = setup()
    env.service.main_agent = ReportAgent(env.pipeline, dependencies={'revision': 'old'})
    job = await env.service.submit(REQUEST)
    saved = deepcopy(job['executionManifest'])
    env.service.main_agent = ReportAgent(env.pipeline, dependencies={'revision': 'new'})
    replay = await env.service.submit(REQUEST)
    assert replay['_id'] == job['_id'] and replay['executionManifest'] == saved
    await env.service.drain_generation()
    assert env.pipeline.calls == 0
    assert env.batches.documents[job['_id']]['generationStatus'] == 'failed'
    assert all(item['body']['status'] == 'failed' for item in env.artifacts.documents.values())


@pytest.mark.asyncio
async def test_legacy_unversioned_queue_fails_explicitly_but_started_task_uses_old_sealing():
    env = setup()
    job = await env.service.submit(REQUEST)
    env.service.main_agent = ReportAgent(env.pipeline)
    await env.service.drain_generation()
    assert env.pipeline.calls == 0
    assert env.batches.documents[job['_id']]['generationStatus'] == 'failed'
    assert any('REPORT_EXECUTION_VERSION_UNAVAILABLE' in str(item) for item in env.artifacts.documents.values())


@pytest.mark.asyncio
async def test_started_main_agent_is_not_reexecuted_during_recovery():
    env = setup()
    env.service.main_agent = ReportAgent(env.pipeline)
    await env.service.submit(REQUEST)
    await env.workflow.claim_generation('old-process')
    env.clock.advance(121)
    await env.service.recover_pending()
    assert env.pipeline.calls == 0
    assert any('REPORT_GENERATION_INTERRUPTED' in str(item) for item in env.artifacts.documents.values())


@pytest.mark.asyncio
async def test_manifest_uses_admission_copy_not_mutable_caller_data():
    env = setup()
    manifest = {'contractVersion': 'example', 'nested': {'revision': 'v1'}}
    ensure = env.workflow.ensure_indexes

    async def mutate():
        manifest['nested']['revision'] = 'v2'
        await ensure()

    env.workflow.ensure_indexes = mutate
    job = await env.workflow.admit(
        REQUEST.model_dump(by_alias=True), 'fixed', 'https://receiver.invalid', execution_manifest=manifest
    )
    assert job['executionManifest']['nested']['revision'] == 'v1'


@pytest.mark.asyncio
async def test_peer_followups_use_same_report_main_agent_and_preserve_source_delivery_ids():
    from tests.unit.report.test_delegation import request
    from tests.unit.report.test_delegation import setup as followups

    env = followups()
    agent = ReportAgent(None)
    env.service.attach_main_agent(agent)
    body = request()
    receipt = await env.service.submit(body)
    saved = env.store.batches.documents[receipt.task_id]
    assert saved['executionManifest'] == agent.execution_manifest()
    await env.service.drain_generation()
    assert len(env.generator.calls) == 1
    assert {item['body']['finalReportId'] for item in env.store.artifacts.documents.values()} == {'gut-main'}
    assert {item['reportType'] for item in env.store.artifacts.documents.values()} == {
        'explain_gutBarrier',
        'opinion',
        'instruction',
    }


def test_production_registration_pins_real_children_and_config_without_model_calls():
    from adapters.report.main_agent import build_report_main_agent
    from agents.graphs.report import register_report_followup
    from agents.graphs.report import register_report_narrative
    from modules.report.agent import REPORT_MAIN_SELECTION
    from tests.unit.agents.core.test_versioned_agent_ports import runtime

    instance, closed = runtime()
    register_report_narrative(instance)
    register_report_followup(instance)
    instance.llm_gateway.render_prompt = lambda profile: 'fixed prompt ' + profile.prompt_name
    instance.llm_gateway.request_model = lambda profile: ('fake', 'model')
    agent = build_report_main_agent(instance, setup().pipeline)
    assert isinstance(agent, ReportAgent)
    assert instance.ports.get(REPORT_MAIN_SELECTION) is agent.port
    assert instance.module_statuses['report-main']['ready']
    assert len(agent.execution_manifest()['dependencies']['children']) == 2
    assert not closed


def test_failed_main_registration_does_not_remove_other_capabilities():
    from adapters.report.main_agent import build_report_main_agent
    from modules.report.agent import UnavailableReportAgent
    from tests.unit.agents.core.test_versioned_agent_ports import Graph
    from tests.unit.agents.core.test_versioned_agent_ports import runtime

    instance, closed = runtime()
    instance.registry.register('unrelated.query', Graph(), version='v1')
    agent = build_report_main_agent(instance, setup().pipeline)
    assert isinstance(agent, UnavailableReportAgent)
    assert instance.registry.capabilities() == ('unrelated.query',)
    assert not instance.module_statuses['report-main']['ready']
    assert not instance.ports.selections() and not closed


@pytest.mark.asyncio
async def test_child_calls_use_exact_version_even_when_legacy_alias_names_another_graph():
    from adapters.report.agent_followup import FollowupModelGatewayFactory
    from adapters.report.agent_narrative import NarrativeClientFactory
    from adapters.report.main_agent import build_report_main_agent
    from agents import GraphRegistry
    from agents.graphs.report import register_report_followup
    from agents.graphs.report import register_report_narrative
    from modules.report.agent import REPORT_FOLLOWUP_SELECTION
    from modules.report.agent import REPORT_NARRATIVE_SELECTION
    from tests.unit.agents.core.test_versioned_agent_ports import runtime

    class Graph:
        def __init__(self, content):
            self.content, self.calls = content, 0

        async def ainvoke(self, payload, config, **kwargs):
            self.calls += 1
            return {'content': self.content}

    instance, _ = runtime()
    register_report_narrative(instance)
    register_report_followup(instance)
    instance.llm_gateway.render_prompt = lambda profile: 'fixed'
    instance.llm_gateway.request_model = lambda profile: ('fake', 'model')
    registry = GraphRegistry()
    selected, replacement = Graph('{"summary":"fixed"}'), Graph('unselected')
    for selection in (REPORT_NARRATIVE_SELECTION, REPORT_FOLLOWUP_SELECTION):
        registry.register(selection.capability, replacement, version='v2')
        registry.register(selection.capability, selected, version=selection.implementation_revision, legacy=False)
    instance.registry.publish(registry)
    agent = build_report_main_agent(instance, setup().pipeline)
    assert isinstance(agent, ReportAgent)
    args = {'user_id': 'u', 'session_id': 's', 'execution_id': 'task'}
    narrative = NarrativeClientFactory(hub=instance.hub).create(**args)
    followup = FollowupModelGatewayFactory(hub=instance.hub).create(**args)
    assert await narrative.generate({'task': 'original'}) == {'summary': 'fixed'}
    assert await followup.complete_text(task_type='opinion', user_prompt='original') == '{"summary":"fixed"}'
    assert selected.calls == 2 and replacement.calls == 0


@pytest.mark.asyncio
async def test_missing_dependency_has_explicit_execution_version_error():
    env = setup()

    def unavailable():
        raise ValueError('removed profile')

    env.service.main_agent = ReportAgent(env.pipeline, verify_dependencies=unavailable)
    job = await env.service.submit(REQUEST)
    await env.service.drain_generation()
    assert env.pipeline.calls == 0
    assert env.batches.documents[job['_id']]['generationStatus'] == 'failed'
    assert any('REPORT_EXECUTION_VERSION_UNAVAILABLE' in str(item) for item in env.artifacts.documents.values())


@pytest.mark.asyncio
async def test_document_consumer_is_pinned_and_mutation_cannot_run_new_content():
    from types import SimpleNamespace

    env = setup()
    selection = {'consumerRevision': 'report-fixed-documents.v1', 'outputSelection': {'implementation': 'v16'}}
    env.pipeline.document_source_factory = SimpleNamespace(document_input_manifest=lambda: selection)
    agent = ReportAgent(env.pipeline)
    env.service.main_agent = agent
    job = await env.service.submit(REQUEST)
    selection['outputSelection']['implementation'] = 'v17'
    assert job['executionManifest']['documentConsumer']['outputSelection']['implementation'] == 'v16'
    assert agent.execution_manifest()['documentConsumer']['outputSelection']['implementation'] == 'v16'
    await env.service.drain_generation()
    assert env.pipeline.calls == 0
    assert env.batches.documents[job['_id']]['generationStatus'] == 'failed'
    assert any('REPORT_EXECUTION_VERSION_UNAVAILABLE' in str(item) for item in env.artifacts.documents.values())


@pytest.mark.asyncio
async def test_restarted_agent_never_substitutes_a_new_document_consumer_for_an_accepted_task():
    from types import SimpleNamespace

    env = setup()
    env.pipeline.document_source_factory = SimpleNamespace(
        document_input_manifest=lambda: {'consumerRevision': 'report-legacy-documents.v1'}
    )
    env.service.main_agent = ReportAgent(env.pipeline)
    job = await env.service.submit(REQUEST)
    env.pipeline.document_source_factory = SimpleNamespace(
        document_input_manifest=lambda: {'consumerRevision': 'report-fixed-documents.v1'}
    )
    env.service.main_agent = ReportAgent(env.pipeline)
    again = await env.service.submit(REQUEST)
    assert again['executionManifest'] == job['executionManifest']
    await env.service.drain_generation()
    assert env.pipeline.calls == 0
    assert env.batches.documents[job['_id']]['generationStatus'] == 'failed'


@pytest.mark.parametrize(
    'descriptor', [None, {}, {'consumerRevision': ''}, {'consumerRevision': 'v1', 'value': float('nan')}]
)
def test_unknown_or_non_durable_document_input_versions_fail_before_admission(descriptor):
    from types import SimpleNamespace

    from modules.report.agent import ReportExecutionVersionError

    env = setup()
    env.pipeline.document_source_factory = SimpleNamespace(document_input_manifest=lambda: descriptor)
    with pytest.raises(ReportExecutionVersionError):
        ReportAgent(env.pipeline)
    assert not env.batches.documents and env.pipeline.calls == 0


def test_legacy_document_factory_manifest_is_explicit_detached_and_does_not_build_processor():
    from unittest.mock import Mock

    from adapters.report.document_source import ReportDocumentSourceFactory

    processor = Mock(side_effect=AssertionError('manifest must not construct or execute a processor'))
    factory = ReportDocumentSourceFactory(processor)
    value = factory.document_input_manifest()
    value['outputSelection']['implementation_revision'] = 'changed'
    assert factory.document_input_manifest()['outputSelection']['implementation_revision'] == 'report-source-v16'
    assert factory.document_input_manifest()['consumerRevision'] == 'report-legacy-documents.v1'
    processor.assert_not_called()
