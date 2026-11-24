"""Default Record wiring keeps early ownership resources and paired business adapters."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from adapters.record.completion import MongoRecordCompletionStore
from bootstrap.factory import build_container
from bootstrap.record_sources import build_record_source_runtime
from bootstrap.record_sources import create_application_record_source_resources
from modules.health_profile.application import ProfileUserSourceResolver
from tests.unit.bootstrap.test_factory import _dependencies
from tests.unit.bootstrap.test_factory import _RecordingManager
from tests.unit.bootstrap.test_record_sources import Database
from tests.unit.services.chat.test_chat_main_agent import manager_for


@contextmanager
def composed(*, explicit_source=False, explicit_completion=False, default_sources=True):
    manager, _, chat_graph, _, _ = manager_for()
    runtime = manager._agent_runtime
    dependencies = _dependencies()
    dependencies.pop('record_profile_source')
    dependencies.pop('record_completion_store')
    dependencies.pop('record_session_cache')
    if default_sources:
        dependencies.pop('document_output_profiles')
        dependencies.pop('health_profile_ingestion_worker')
    else:
        dependencies['health_report_processing_service_factory'] = lambda: object()
    database = Database()
    sessions = database['configured_sessions']
    cache = object()
    legacy = SimpleNamespace(recover_pending=AsyncMock())
    completions = MongoRecordCompletionStore(sessions)
    if explicit_source:
        dependencies['record_profile_source'] = legacy
    if explicit_completion:
        dependencies['record_completion_store'] = completions
    document_resources = SimpleNamespace(ledger=object())
    profile_worker = SimpleNamespace(recover_pending=AsyncMock())
    graph = SimpleNamespace(
        profile=SimpleNamespace(
            reader=object(), inbox=object(), basic_info=None, worker=SimpleNamespace(recover_pending=AsyncMock())
        ),
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
    order, groups, deliveries = [], [], []
    enabled = default_sources and not explicit_source and not explicit_completion

    def sources(**kwargs):
        order.append('resources')
        group = create_application_record_source_resources(**kwargs)
        groups.append(group)
        return group

    def document_graph(**kwargs):
        order.append('profile')
        registered, fences = kwargs['source_resolvers'], kwargs['owner_fence_readers']
        if enabled:
            assert len(groups) == 1
            resolver = registered['record', 'profile.record-source.v2']
            assert isinstance(resolver, ProfileUserSourceResolver)
            assert resolver.reader is groups[0].material_reader
            assert fences == {'record': groups[0].owner}
        else:
            assert registered == fences == {}
        graph.resolvers = {('document', 'fixed-document-contract'): object(), **registered}
        return graph

    def delivery(**kwargs):
        order.append('delivery')
        result = build_record_source_runtime(**kwargs)
        deliveries.append(result)
        return result

    with (
        patch('db.mongodb.collection_sessions', sessions),
        patch('bootstrap.factory.create_application_record_session_cache', return_value=cache) as create_cache,
        patch('bootstrap.factory._create_record_profile_source', return_value=legacy) as create_legacy,
        patch(
            'bootstrap.factory.create_application_record_completion_store', return_value=completions
        ) as create_completion,
        patch(
            'bootstrap.document_sources.create_application_document_source_resources', return_value=document_resources
        ),
        patch(
            'bootstrap.document_sources.build_current_document_source_graph', side_effect=document_graph
        ) as build_graph,
        patch('bootstrap.document_sources.build_current_document_consumers', return_value=consumers),
        patch(
            'bootstrap.record_sources.create_application_record_source_resources', side_effect=sources
        ) as build_resources,
        patch('bootstrap.record_sources.build_record_source_runtime', side_effect=delivery) as build_delivery,
        patch('bootstrap.factory.create_application_health_report_interpreter'),
        patch('bootstrap.factory.HealthReportProcessingService'),
        patch('bootstrap.factory._create_health_profile_ingestion_worker', return_value=profile_worker) as build_worker,
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
            dependencies=dependencies,
            database=database,
            sessions=sessions,
            cache=cache,
            legacy=legacy,
            completions=completions,
            graph=graph,
            profile_worker=profile_worker,
            groups=groups,
            deliveries=deliveries,
            order=order,
            build_graph=build_graph,
            build_resources=build_resources,
            build_delivery=build_delivery,
            build_worker=build_worker,
            create_cache=create_cache,
            create_legacy=create_legacy,
            create_completion=create_completion,
            chat_graph=chat_graph,
        )


@pytest.mark.asyncio
async def test_default_record_registers_early_ownership_and_injects_one_paired_runtime():
    with composed() as env:
        assert env.order == ['resources', 'profile', 'delivery']
        env.create_cache.assert_called_once_with()
        env.build_resources.assert_called_once_with(session_cache=env.cache, lease_manager=env.runtime.lease_manager)
        group, delivery = env.groups[0], env.deliveries[0]
        env.build_delivery.assert_called_once_with(
            resources=group,
            legacy=env.legacy,
            completion_store=env.completions,
            inbox=env.graph.profile.inbox,
            presence=env.graph.admission.presence,
            fresh_admission=env.graph.admission.admission,
        )
        env.create_legacy.assert_called_once_with()
        env.create_completion.assert_called_once_with()
        supplied = env.container.record_service.dependencies
        assert supplied['record_profile_source'] is delivery.router
        assert supplied['completion_store'] is delivery.completion_store
        assert supplied['session_cache'] is group.owner.evidence.cache is env.cache
        assert group.owner.evidence.leases is env.runtime.lease_manager
        assert group.owner.evidence.collection is env.sessions
        assert group.material_reader.publications.store is group.store is delivery.worker.store
        assert delivery.worker.completion_evidence is group.owner.evidence
        assert delivery.router.publications is delivery.completion_store.publications is delivery.worker
        assert delivery.completion_store.completions is env.completions
        env.build_worker.assert_called_once_with(env.runtime, source_snapshot_resolvers=env.graph.resolvers)
        assert env.container.health_profile_ingestion_worker is env.profile_worker
        workers = dict(env.container.source_workers)
        assert len(env.container.source_workers) == len(workers) == 3
        assert set(workers) == {'document-outbox', 'analysis-delivery', 'record-source'}
        assert workers['record-source'].recover == delivery.worker.recover_pending
        assert env.container.task_supervisor.active_task_count == 0
        assert not env.chat_graph.calls and not env.chat_graph.stream_calls
        assert all(not c.documents and not c.reads and not c.indexes for c in env.database.collections.values())
        env.profile_worker.recover_pending.assert_not_awaited()
        env.graph.profile.worker.recover_pending.assert_not_awaited()
        # The root runner scans the new store only. Legacy recovery remains in
        # the existing shared Profile worker and is not dispatched a second time.
        assert await workers['record-source'].recover_pending() == 0
        env.legacy.recover_pending.assert_not_awaited()
        env.profile_worker.recover_pending.assert_not_awaited()
        env.graph.profile.worker.recover_pending.assert_not_awaited()
    await env.container.resources.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit_source,explicit_completion', [(True, False), (False, True), (True, True)])
async def test_either_explicit_record_adapter_keeps_the_whole_legacy_pair(explicit_source, explicit_completion):
    with composed(explicit_source=explicit_source, explicit_completion=explicit_completion) as env:
        assert env.order == ['profile']
        env.build_resources.assert_not_called()
        env.build_delivery.assert_not_called()
        supplied = env.container.record_service.dependencies
        assert supplied['record_profile_source'] is env.legacy
        assert supplied['completion_store'] is env.completions
        assert supplied['session_cache'] is env.cache
        assert env.create_legacy.call_count == int(not explicit_source)
        assert env.create_completion.call_count == int(not explicit_completion)
        env.create_cache.assert_called_once_with()
        assert [name for name, _ in env.container.source_workers] == [
            'document-outbox',
            'analysis-delivery',
        ]
        assert not any(key[0] == 'record' for key in env.graph.resolvers)
        env.build_worker.assert_called_once_with(env.runtime, source_snapshot_resolvers=env.graph.resolvers)
        assert env.container.task_supervisor.active_task_count == 0
        assert all(not c.documents and not c.reads and not c.indexes for c in env.database.collections.values())
    await env.container.resources.aclose()


@pytest.mark.asyncio
async def test_explicit_historical_document_processing_keeps_record_legacy_without_new_resources():
    with composed(default_sources=False) as env:
        assert env.order == []
        env.build_graph.assert_not_called()
        env.build_resources.assert_not_called()
        env.build_delivery.assert_not_called()
        env.build_worker.assert_not_called()
        supplied = env.container.record_service.dependencies
        assert supplied['record_profile_source'] is env.legacy
        assert supplied['completion_store'] is env.completions
        assert supplied['session_cache'] is env.cache
        assert env.container.health_profile_ingestion_worker is env.dependencies['health_profile_ingestion_worker']
        assert env.container.source_workers == ()
        assert env.container.task_supervisor.active_task_count == 0
        assert not env.chat_graph.calls and not env.chat_graph.stream_calls
    await env.container.resources.aclose()
