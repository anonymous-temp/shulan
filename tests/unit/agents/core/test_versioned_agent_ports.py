"""Opt-in versioned ports preserve legacy aliases and atomically stage module registration."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agents import AgentHub
from agents import AgentRequest
from agents import AgentSelection
from agents import BoundAgentPort
from agents import WorkflowAgentPort
from agents.errors import AgentCapabilityAlreadyRegisteredError
from agents.errors import AgentCapabilityNotFoundError
from agents.llm import ContextPolicy
from agents.llm import ModelProfile
from agents.llm import ModelProfileRegistry
from agents.runtime.container import AgentRuntime
from agents.runtime.context import AgentRunContext
from agents.runtime.registry import GraphRegistry
from tests.unit._support.agents.fake_llm import FakeLLMGateway


class Graph:
    def __init__(self):
        self.calls = []

    async def ainvoke(self, payload, config, context):
        self.calls.append((payload, config, context))
        payload['nested']['value'] = 'internal mutation'
        return {'answer': 'done'}

    async def astream(self, payload, config, context, **kwargs):
        self.calls.append((payload, config, context))
        yield {'type': 'completed', 'data': {'answer': 'done'}}


def runtime():
    registry = GraphRegistry()
    closed = []
    instance = AgentRuntime(
        AgentHub(registry),
        registry,
        ModelProfileRegistry(),
        FakeLLMGateway(),
        None,
        None,
        InMemorySaver(),
        lambda: closed.append('shared'),
    )
    return instance, closed


def test_explicit_version_never_changes_existing_alias_or_chooses_latest():
    registry = GraphRegistry()
    first = registry.register('report.generate', Graph(), version='v1')
    second = registry.register('report.generate', Graph(), version='v2', legacy=False)
    assert registry.get('report.generate') is first
    assert registry.get('report.generate', selection=second.selection) is second
    assert registry.versions('report.generate') == (first.selection, second.selection)
    with pytest.raises(AgentCapabilityNotFoundError):
        registry.get('report.generate', selection=AgentSelection('report.generate', 'v3'))
    with pytest.raises(AgentCapabilityAlreadyRegisteredError):
        registry.register('report.generate', Graph(), version='v3')


@pytest.mark.asyncio
async def test_port_pins_version_and_isolates_scope_checkpoint_and_payload():
    instance, closed = runtime()
    one, two = Graph(), Graph()
    instance.registry.register('report.generate', one, version='v1')
    instance.registry.register('report.generate', two, version='v2', legacy=False)
    port = BoundAgentPort(instance.hub, AgentSelection('report.generate', 'v2'), 'report')
    context = AgentRunContext.create(user_id='user', subject_id='s', session_id='session', run_id='run')
    request = AgentRequest({'nested': {'value': 'original'}})
    result = await port.invoke(request, context, thread_id='business-1')
    assert result.graph_version == 'v2'
    assert request.payload['nested']['value'] == 'original'
    assert not one.calls
    first_id = two.calls[-1][1]['configurable']['thread_id']
    await port.invoke(request, replace(context, run_id='retry'), thread_id='business-1')
    assert two.calls[-1][1]['configurable']['thread_id'] == first_id
    await port.invoke(request, replace(context, subject_id='other'), thread_id='business-1')
    assert two.calls[-1][1]['configurable']['thread_id'] != first_id
    await replace(port, role='research').invoke(request, context, thread_id='business-1')
    assert two.calls[-1][1]['configurable']['thread_id'] != first_id
    # Legacy Hub thread keys remain unchanged for existing checkpoint recovery.
    await instance.hub.invoke('report.generate', request, context, thread_id='old-key')
    assert one.calls[-1][1]['configurable']['thread_id'] == 'old-key'


def test_failed_optional_module_does_not_leak_graphs_profiles_or_close_shared_resources():
    instance, closed = runtime()
    instance.profiles.register(ModelProfile('existing', ContextPolicy.STATELESS))

    def configure(staged):
        staged.registry.register('report.generate', Graph(), version='v1')
        staged.profiles.register(ModelProfile('existing', ContextPolicy.STATELESS))

    assert not instance.install_module('report', configure, cleanup=(lambda: closed.append('report'),))
    assert instance.registry.capabilities() == ()
    assert instance.profiles.names() == ('existing',)
    assert closed == ['report']
    assert not instance.module_statuses['report']['ready']
    assert instance.install_module('chat', lambda staged: staged.registry.register('chat.reply', Graph(), version='v1'))
    assert instance.registry.capabilities() == ('chat.reply',)
    instance.close()
    assert closed == ['report', 'shared']


def test_disabled_or_invalid_async_module_does_not_publish_and_cleanup_is_once():
    instance, closed = runtime()
    assert not instance.install_module('disabled', lambda staged: pytest.fail('must not configure'), enabled=False)

    async def invalid(staged):
        staged.registry.register('bad', Graph(), version='v1')

    assert not instance.install_module('bad', invalid, cleanup=(lambda: closed.append('bad'),))
    assert instance.install_module(
        'good',
        lambda staged: staged.registry.register('good', Graph(), version='v1'),
        cleanup=(lambda: closed.append('good'),),
    )
    instance.close()
    instance.close()
    assert closed == ['bad', 'good', 'shared']


def test_failed_compilation_rolls_back_new_profile_and_required_module_raises():
    instance, closed = runtime()

    def configure(staged):
        staged.profiles.register(ModelProfile('new', ContextPolicy.STATELESS))
        staged.register_graph(
            'new', SimpleNamespace(compile=lambda **kwargs: (_ for _ in ()).throw(ValueError('bad'))), version='v1'
        )

    with pytest.raises(ValueError, match='bad'):
        instance.install_module('required', configure, required=True, cleanup=(lambda: closed.append('new'),))
    assert not instance.profiles.names()
    assert instance.registry.capabilities() == ()
    assert closed == ['new']


@pytest.mark.asyncio
async def test_fixed_domain_workflow_can_be_installed_without_any_graph_or_llm():
    instance, closed = runtime()
    calls = []

    async def pipeline(request, context, *, thread_id):
        calls.append((request.payload, context.run_id, thread_id))
        return {'businessTaskId': 'task-1', 'status': 'accepted'}

    selection = AgentSelection('gut_barrier.workflow', 'gut-v1')

    def configure(staged):
        staged.ports.register(selection, WorkflowAgentPort(pipeline, selection, 'gut_barrier'))

    assert instance.install_module('gut_barrier', configure)
    assert instance.registry.capabilities() == ()
    assert instance.module_statuses['gut_barrier']['capabilities'] == ('gut_barrier.workflow',)
    context = AgentRunContext.create(user_id='u', session_id='s')
    result = await instance.ports.get(selection).invoke(AgentRequest({'input': 'x'}), context)
    assert result.data == {'businessTaskId': 'task-1', 'status': 'accepted'}
    assert len(calls) == 1
    assert calls[0][0] == {'input': 'x'}
    assert calls[0][2].startswith('agent-v1:')
    assert not instance.install_module('duplicate', configure)
    assert instance.ports.get(selection).selection == selection


@pytest.mark.asyncio
async def test_production_registration_failure_rolls_back_only_one_domain(monkeypatch):
    from agents.runtime import application

    instance, closed = runtime()

    def register_report(staged):
        staged.profiles.register(ModelProfile('report.partial', ContextPolicy.STATELESS))
        staged.registry.register('report.partial', Graph(), version='v1')

    def fail(staged):
        raise RuntimeError('broken report builder')

    monkeypatch.setattr(application, 'register_report_narrative', register_report)
    monkeypatch.setattr(application, 'register_report_followup', fail)
    monkeypatch.setattr(
        application, 'register_chat_reply', lambda staged: staged.registry.register('chat.reply', Graph(), version='v1')
    )
    application.register_application_modules(instance, question_topics={'topics': [], 'rules': []})
    assert instance.module_statuses['report']['error'] == 'RuntimeError'
    assert not instance.module_statuses['report']['ready']
    assert 'report.partial' not in instance.profiles.names()
    assert 'report.partial' not in instance.registry.capabilities()
    assert instance.module_statuses['chat']['ready']
    result = await instance.hub.invoke(
        'chat.reply', AgentRequest({'nested': {'value': 'x'}}), AgentRunContext.create(user_id='u', session_id='s')
    )
    assert result.data['answer'] == 'done'
    assert not closed
    with pytest.raises(AgentCapabilityNotFoundError):
        instance.registry.get('report.partial')


def test_module_staging_reuses_registered_shared_retrieval_profile():
    from agents.knowledge.profile import ensure_knowledge_route_profile

    instance, closed = runtime()
    seen = []

    def configure(staged):
        seen.append(ensure_knowledge_route_profile(staged.profiles))

    assert instance.install_module('chat', configure)
    assert instance.install_module('record', configure)
    assert seen[0] is seen[1]
    assert instance.profiles.names() == ('knowledge.route',)
