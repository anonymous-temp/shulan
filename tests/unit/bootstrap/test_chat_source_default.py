"""Default Chat assembly keeps raw source ownership and one new background scan."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from bootstrap.chat_sources import build_chat_source_resources
from bootstrap.chat_sources import build_chat_source_runtime
from bootstrap.factory import build_container
from modules.health_profile.application import ProfileUserSourceResolver
from tests.unit.bootstrap.test_chat_sources import Database
from tests.unit.bootstrap.test_factory import _dependencies
from tests.unit.bootstrap.test_factory import _RecordingManager
from tests.unit.services.chat.test_chat_main_agent import manager_for


@contextmanager
def composed(*, custom_source=False, custom_repository=False, default_document=True):
    manager, _, model, _, _ = manager_for()
    runtime = manager._agent_runtime
    dependencies = _dependencies()
    dependencies.pop('chat_profile_source')
    dependencies.pop('chat_session_repository')
    dependencies.pop('document_output_profiles')
    dependencies.pop('health_profile_ingestion_worker')
    old_source = SimpleNamespace(recover_pending=AsyncMock())
    old_repository = object()
    if custom_source:
        dependencies['chat_profile_source'] = old_source
    if custom_repository:
        dependencies['chat_session_repository'] = old_repository
    if not default_document:
        dependencies['health_report_processing_service_factory'] = lambda: object()
        dependencies['health_profile_ingestion_worker'] = object()
    enabled = not custom_source and not custom_repository and default_document
    database = Database()
    order, groups, runtimes = [], [], []
    graph = SimpleNamespace(
        profile=SimpleNamespace(reader=object(), inbox=object(), basic_info=None),
        admission=SimpleNamespace(presence=object(), admission=object()),
        publication=SimpleNamespace(lifecycle=object()),
        current=SimpleNamespace(source=object(), recovery=SimpleNamespace(recover_pending=AsyncMock())),
        resolvers={},
    )
    consumers = SimpleNamespace(
        record=object(),
        report=object(),
        analysis=SimpleNamespace(service=object(), delivery=SimpleNamespace(recover_pending=AsyncMock())),
    )

    def resources():
        order.append('resources')
        result = build_chat_source_resources(sessions=database['configured_sessions'])
        groups.append(result)
        return result

    def document_graph(**kwargs):
        order.append('profile')
        resolvers, fences = kwargs['source_resolvers'], kwargs['owner_fence_readers']
        if enabled:
            resolver = resolvers['chat', 'profile.chat-source.v2']
            assert isinstance(resolver, ProfileUserSourceResolver)
            assert resolver.reader is groups[0].snapshot_reader
            assert fences['chat'] is groups[0].fence_reader
        else:
            assert 'chat' not in fences
            assert not any(key[0] == 'chat' for key in resolvers)
        graph.resolvers = {('document', 'fixed'): object(), **resolvers}
        return graph

    def delivery(**kwargs):
        order.append('delivery')
        assert kwargs['resources'] is groups[0]
        assert kwargs['legacy'] is old_source
        assert kwargs['inbox'] is graph.profile.inbox
        assert kwargs['presence'] is graph.admission.presence
        assert kwargs['fresh_admission'] is graph.admission.admission
        result = build_chat_source_runtime(**kwargs)
        runtimes.append(result)
        return result

    with (
        patch(
            'bootstrap.chat_sources.create_application_chat_source_resources', side_effect=resources
        ) as create_resources,
        patch('bootstrap.chat_sources.build_chat_source_runtime', side_effect=delivery) as build_delivery,
        patch(
            'bootstrap.document_sources.create_application_document_source_resources',
            return_value=SimpleNamespace(ledger=object()),
        ),
        patch('bootstrap.document_sources.build_current_document_source_graph', side_effect=document_graph),
        patch('bootstrap.document_sources.build_current_document_consumers', return_value=consumers),
        patch('bootstrap.factory.create_application_health_report_interpreter'),
        patch('bootstrap.factory.HealthReportProcessingService'),
        patch('bootstrap.factory._create_chat_profile_source', return_value=old_source),
        patch('bootstrap.factory.create_application_chat_session_repository', return_value=old_repository),
        patch('bootstrap.factory._create_health_profile_ingestion_worker', return_value=object()) as profile_worker,
    ):
        container = build_container(
            runtime,
            **dependencies,
            report_pipeline=object(),
            report_followup_service=object(),
            gut_barrier_service=object(),
            questionnaire_service=object(),
            qa_manager_type=_RecordingManager,
            chat_manager_type=_RecordingManager,
        )
        yield SimpleNamespace(
            container=container,
            runtime=runtime,
            model=model,
            order=order,
            groups=groups,
            runtimes=runtimes,
            graph=graph,
            database=database,
            old_source=old_source,
            old_repository=old_repository,
            create_resources=create_resources,
            build_delivery=build_delivery,
            profile_worker=profile_worker,
        )


@pytest.mark.asyncio
async def test_default_chat_registers_pure_readers_before_profile_and_schedules_only_publication():
    with composed() as env:
        assert env.order == ['resources', 'profile', 'delivery']
        env.create_resources.assert_called_once_with()
        env.build_delivery.assert_called_once()
        source_runtime, resources = env.runtimes[0], env.groups[0]
        assert env.container.chat_profile_source is source_runtime.router
        assert env.container.chat_service.dependencies['chat_profile_source'] is source_runtime.router
        assert env.container.chat_service.dependencies['session_repository'] is env.old_repository
        assert source_runtime.worker is source_runtime.router.publication
        assert source_runtime.worker.store is resources.store
        assert source_runtime.worker.owner is resources.owner
        env.profile_worker.assert_called_once_with(env.runtime, source_snapshot_resolvers=env.graph.resolvers)
        assert not env.model.calls and not env.model.stream_calls
        assert env.container.task_supervisor.active_task_count == 0
        assert all(not c.documents and not c.reads for c in env.database.collections.values())
        workers = dict(env.container.source_workers)
        assert len(workers) == len(env.container.source_workers) == 3
        assert workers['chat-source'].recover == source_runtime.worker.recover_pending
        # An empty actual scan never delegates the legacy recovery also present
        # on the router.
        assert await workers['chat-source'].recover_pending() == 0
        env.old_source.recover_pending.assert_not_awaited()
    await env.container.resources.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('custom_source,custom_repository', [(True, False), (False, True), (True, True)])
async def test_explicit_chat_dependencies_keep_the_callers_legacy_source_and_storage(custom_source, custom_repository):
    with composed(custom_source=custom_source, custom_repository=custom_repository) as env:
        assert env.order == ['profile']
        env.create_resources.assert_not_called()
        env.build_delivery.assert_not_called()
        assert env.container.chat_profile_source is env.old_source
        assert env.container.chat_service.dependencies['chat_profile_source'] is env.old_source
        assert env.container.chat_service.dependencies['session_repository'] is env.old_repository
        assert 'chat-source' not in dict(env.container.source_workers)
        assert env.container.task_supervisor.active_task_count == 0
    await env.container.resources.aclose()


@pytest.mark.asyncio
async def test_historical_document_injection_does_not_install_new_chat_source():
    with composed(default_document=False) as env:
        assert env.order == []
        env.create_resources.assert_not_called()
        env.build_delivery.assert_not_called()
        env.profile_worker.assert_not_called()
        assert env.container.chat_profile_source is env.old_source
        assert env.container.source_workers == ()
    await env.container.resources.aclose()
