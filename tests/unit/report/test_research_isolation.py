"""Invalid optional research isolates its dependent AI path, never shared startup."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from adapters.report.research import create_application_report_research

SETTINGS = {
    'REPORT_RESEARCH_ENABLED': True,
    'REPORT_RESEARCH_MAX_MODEL_CALLS': 4,
    'REPORT_RESEARCH_MAX_TOOL_CALLS': 3,
    'REPORT_RESEARCH_TIMEOUT_SECONDS': 30,
    'REPORT_RESEARCH_EVALUATION_REFERENCE': 'offline-test',
    'REPORT_RESEARCH_MODEL': 'model',
}


@pytest.mark.parametrize(
    'changes,knowledge',
    [
        ({'REPORT_RESEARCH_MAX_MODEL_CALLS': 0}, True),
        ({'REPORT_RESEARCH_MAX_TOOL_CALLS': 0}, True),
        ({'REPORT_RESEARCH_TIMEOUT_SECONDS': 0}, True),
        ({'REPORT_RESEARCH_MODEL': ''}, True),
        ({'REPORT_RESEARCH_EVALUATION_REFERENCE': ''}, True),
        ({}, False),
    ],
)
def test_invalid_enabled_research_is_explicitly_unavailable(changes, knowledge):
    runtime = SimpleNamespace(knowledge_gateway=SimpleNamespace(enabled=knowledge))
    with patch.multiple('config.env', **{**SETTINGS, **changes}):
        agent = create_application_report_research(runtime)
    assert agent is not None
    assert agent.manifest['availability'] == 'unavailable'
    assert agent.manifest['reasonType'] == 'ValueError'
    with pytest.raises(ValueError, match='unavailable'):
        agent.ensure_available()


def test_disabled_research_does_not_inspect_runtime_or_construct_resources():
    class Bomb:
        def __getattr__(self, name):
            raise AssertionError('disabled research must not inspect runtime')

    with patch.multiple('config.env', **{**SETTINGS, 'REPORT_RESEARCH_ENABLED': False}):
        assert create_application_report_research(Bomb()) is None


@pytest.mark.asyncio
async def test_invalid_research_does_not_close_shared_application_resources():
    from agents import AgentHub
    from agents import GraphRegistry
    from bootstrap.factory import build_application_bundle
    from runtime import ResourceRegistry
    from tests.unit.bootstrap.test_factory import _dependencies
    from tests.unit.bootstrap.test_factory import _RecordingManager
    from tests.unit.bootstrap.test_factory import _SyncResource

    resources, closed = ResourceRegistry(), []
    resources.register_sync(_SyncResource(closed, 'shared'), name='shared')
    pipeline = SimpleNamespace()

    def build(**kwargs):
        pipeline.research_agent = kwargs['research_agent']
        return pipeline

    with (
        patch.multiple('config.env', **{**SETTINGS, 'REPORT_RESEARCH_MAX_MODEL_CALLS': 0}),
        patch('bootstrap.factory.build_report_pipeline', side_effect=build),
        patch('web.application.create_web_application', return_value=SimpleNamespace(settings={})),
    ):
        bundle = build_application_bundle(
            SimpleNamespace(hub=AgentHub(GraphRegistry())),
            resources=resources,
            debug=False,
            **_dependencies(),
            report_artifact_store=object(),
            qa_manager_type=_RecordingManager,
            chat_manager_type=_RecordingManager,
        )
    assert bundle.container.record_service is not None and bundle.container.chat_service is not None
    assert pipeline.research_agent.manifest['availability'] == 'unavailable'
    assert not closed
    await bundle.aclose()
    assert closed == ['shared']


@pytest.mark.asyncio
async def test_unavailable_dependency_marks_only_report_main_unready_and_queries_stay_usable():
    from adapters.report.main_agent import build_report_main_agent
    from agents.graphs.report import register_report_followup
    from agents.graphs.report import register_report_narrative
    from modules.report.agent import UnavailableReportAgent
    from tests.unit.agents.core.test_versioned_agent_ports import Graph
    from tests.unit.agents.core.test_versioned_agent_ports import runtime
    from tests.unit.report.test_durable_reports import REQUEST
    from tests.unit.report.test_durable_reports import setup

    instance, closed = runtime()
    register_report_narrative(instance)
    register_report_followup(instance)
    instance.registry.register('other.domain', Graph(), version='v1')
    instance.llm_gateway.render_prompt = lambda profile: 'fixed prompt'
    instance.llm_gateway.request_model = lambda profile: ('fake', 'model')
    env = setup()
    with patch.multiple('config.env', **{**SETTINGS, 'REPORT_RESEARCH_MAX_MODEL_CALLS': 0}):
        env.pipeline.research_agent = create_application_report_research(instance)
    env.service.main_agent = build_report_main_agent(instance, env.pipeline)
    assert isinstance(env.service.main_agent, UnavailableReportAgent)
    assert not instance.module_statuses['report-main']['ready']
    assert instance.module_statuses['report-main']['error'] == 'ResearchUnavailableError'
    assert 'other.domain' in instance.registry.capabilities() and not closed
    job = await env.service.submit(REQUEST)
    await env.service.drain_generation()
    assert env.pipeline.calls == 0
    assert env.batches.documents[job['_id']]['generationStatus'] == 'failed'
    env.service.main_agent.execute = AsyncMock(side_effect=AssertionError('query entered AI'))
    assert await env.workflow.artifacts_for(job['_id'])
    env.service.main_agent.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('covered', [False, True])
async def test_unavailable_research_cannot_be_swallowed_by_optional_supplement(covered):
    from modules.report.research_integration import supplement_evidence
    from tests.unit.report.test_research_integration import plan

    with patch.multiple('config.env', **{**SETTINGS, 'REPORT_RESEARCH_MAX_MODEL_CALLS': 0}):
        agent = create_application_report_research(object())
    with pytest.raises(ValueError, match='unavailable'):
        await supplement_evidence(
            agent,
            plan(),
            SimpleNamespace(by_topic={'topic-1': ['existing']} if covered else {}),
            scope=('u', 's', 'r'),
            deadline=time.monotonic() + 30,
        )


@pytest.mark.asyncio
async def test_research_becomes_unavailable_after_main_admission_without_business_calls():
    from modules.report.agent import ReportAgent
    from tests.unit.report.test_durable_reports import REQUEST
    from tests.unit.report.test_durable_reports import setup

    env = setup()
    env.service.main_agent = ReportAgent(env.pipeline)
    job = await env.service.submit(REQUEST)
    with patch.multiple('config.env', **{**SETTINGS, 'REPORT_RESEARCH_MAX_MODEL_CALLS': 0}):
        env.pipeline.research_agent = create_application_report_research(object())
    await env.service.drain_generation()
    assert env.pipeline.calls == 0
    assert env.batches.documents[job['_id']]['generationStatus'] == 'failed'


@pytest.mark.asyncio
async def test_normal_research_manifest_and_model_drift_are_explicit():
    from modules.report.research_integration import supplement_evidence
    from tests.unit.agents.llm.test_tool_run_gateway import Wire
    from tests.unit.report.test_research_agent import Knowledge
    from tests.unit.report.test_research_agent import setup
    from tests.unit.report.test_research_integration import plan

    wire = Wire()
    try:
        agent = setup(wire, Knowledge())
        original = agent.manifest
        agent.ensure_available()
        assert agent.manifest == original and 'availability' not in original
        agent.gateway.gateway.request_model = lambda profile: ('other', 'model')
        with pytest.raises(ValueError, match='manifest'):
            await supplement_evidence(
                agent, plan(), SimpleNamespace(by_topic={}), scope=('u', 's', 'r'), deadline=time.monotonic() + 30
            )
        assert not agent.store.rows and not agent.knowledge.queries
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_valid_production_factory_keeps_original_manifest_without_io(monkeypatch):
    import sys

    from modules.report.research import ResearchAgent
    from tests.unit.agents.llm.test_tool_run_gateway import Wire
    from tests.unit.report.test_research_agent import Knowledge

    class Collection:
        name = 'reports'

        def __init__(self):
            self.database = {'reports_research': self}

        def with_options(self, **kwargs):
            return self

        def __getattr__(self, name):
            raise AssertionError('constructing research must not perform storage I/O')

    collection, wire = Collection(), Wire()
    monkeypatch.setitem(
        sys.modules,
        'db.mongodb',
        SimpleNamespace(collection_agent_tool_runs=collection, collection_report_batches=collection),
    )
    runtime = SimpleNamespace(llm_gateway=wire.gateway().gateway, knowledge_gateway=Knowledge())
    try:
        with patch.multiple('config.env', **SETTINGS):
            agent = create_application_report_research(runtime)
        assert isinstance(agent, ResearchAgent)
        manifest = agent.manifest
        agent.ensure_available()
        assert agent.manifest == manifest and 'availability' not in manifest
        assert manifest['providerModel'] == ['deepseek', 'model']
        assert not wire.clients and not runtime.knowledge_gateway.queries
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_model_lookup_failure_after_admission_is_not_a_successful_supplement():
    from modules.report.research import ResearchUnavailableError
    from modules.report.research_integration import supplement_evidence
    from tests.unit.agents.llm.test_tool_run_gateway import Wire
    from tests.unit.report.test_research_agent import Knowledge
    from tests.unit.report.test_research_agent import setup
    from tests.unit.report.test_research_agent import topic
    from tests.unit.report.test_research_integration import plan

    wire = Wire()
    try:
        agent = setup(wire, Knowledge())
        admit = agent.store.admit

        def unavailable(profile):
            raise LookupError('removed')

        async def remove_configuration(*args):
            frozen = await admit(*args)
            agent.gateway.gateway.request_model = unavailable
            return frozen

        agent.store.admit = remove_configuration
        task = plan()
        selected_topic = topic()
        selected_topic.query_enabled = True
        task.topics = (selected_topic,)
        with pytest.raises(ResearchUnavailableError, match='configuration is unavailable'):
            await supplement_evidence(
                agent, task, SimpleNamespace(by_topic={}), scope=('u', 's', 'r'), deadline=time.monotonic() + 30
            )
        assert len(agent.store.rows) == 1
        assert not wire.requests and not agent.knowledge.queries
    finally:
        await wire.close()
