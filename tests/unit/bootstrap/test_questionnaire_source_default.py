"""Default root selection registers Questionnaire resources before shared Profile construction."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from bootstrap.factory import build_container
from bootstrap.questionnaire_sources import build_questionnaire_source_delivery
from bootstrap.questionnaire_sources import build_questionnaire_source_resources
from modules.health_profile.application import ProfileUserSourceResolver
from tests.unit.bootstrap.test_factory import _dependencies
from tests.unit.bootstrap.test_factory import _RecordingManager
from tests.unit.bootstrap.test_questionnaire_sources import Database
from tests.unit.services.chat.test_chat_main_agent import manager_for


@contextmanager
def composed(*, questionnaire_service=None, default_sources=True):
    manager, _, chat_graph, _, _ = manager_for()
    runtime = manager._agent_runtime
    dependencies = _dependencies()
    if default_sources:
        dependencies.pop('document_output_profiles')
        dependencies.pop('health_profile_ingestion_worker')
    else:
        dependencies['health_report_processing_service_factory'] = lambda: object()
    database = Database()
    questionnaires = database['configured_questionnaires']
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
    order, groups, routers = [], [], []
    submitted = object()

    def resources(**kwargs):
        order.append('resources')
        group = build_questionnaire_source_resources(**kwargs)
        groups.append(group)
        return group

    def document_graph(**kwargs):
        order.append('profile')
        registered = kwargs['source_resolvers']
        fences = kwargs['owner_fence_readers']
        if questionnaire_service is None:
            assert len(groups) == 1
            resolver = registered['questionnaire', 'profile.questionnaire-source.v2']
            assert isinstance(resolver, ProfileUserSourceResolver)
            assert resolver.reader is groups[0].snapshot_reader
            assert fences == {'questionnaire': groups[0].fence_reader}
        else:
            assert registered == fences == {}
        # Document owns the shared graph; the real Q helpers are exercised below.
        graph.resolvers = {('document', 'fixed-document-contract'): object(), **registered}
        return graph

    def read_submitted():
        order.append('submitted-reader')
        return submitted

    def delivery(group, **kwargs):
        order.append('delivery')
        router = build_questionnaire_source_delivery(group, **kwargs)
        routers.append(router)
        return router

    def service(**kwargs):
        order.append('service')
        return SimpleNamespace(profile_source_delivery=kwargs.get('profile_source_delivery'))

    with (
        patch('db.mongodb.collection_questionnaires', questionnaires),
        patch(
            'bootstrap.document_sources.create_application_document_source_resources', return_value=document_resources
        ),
        patch(
            'bootstrap.document_sources.build_current_document_source_graph', side_effect=document_graph
        ) as build_graph,
        patch('bootstrap.document_sources.build_current_document_consumers', return_value=consumers),
        patch(
            'bootstrap.questionnaire_sources.build_questionnaire_source_resources', side_effect=resources
        ) as build_sources,
        patch(
            'bootstrap.questionnaire_sources.build_questionnaire_source_delivery', side_effect=delivery
        ) as build_delivery,
        patch(
            'modules.questionnaire.application.create_application_questionnaire_submitted_material_reader',
            side_effect=read_submitted,
        ) as build_submitted,
        patch('adapters.questionnaire_factory.build_questionnaire_service', side_effect=service) as build_service,
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
            questionnaire_service=questionnaire_service,
            qa_manager_type=_RecordingManager,
            chat_manager_type=_RecordingManager,
        )
        yield SimpleNamespace(
            container=container,
            runtime=runtime,
            dependencies=dependencies,
            database=database,
            questionnaires=questionnaires,
            graph=graph,
            document_resources=document_resources,
            profile_worker=profile_worker,
            groups=groups,
            routers=routers,
            submitted=submitted,
            order=order,
            build_graph=build_graph,
            build_sources=build_sources,
            build_delivery=build_delivery,
            build_submitted=build_submitted,
            build_service=build_service,
            build_worker=build_worker,
            chat_graph=chat_graph,
        )


@pytest.mark.asyncio
async def test_default_questionnaire_registers_early_readers_then_injects_one_actual_router_and_worker():
    with composed() as env:
        assert env.order == ['resources', 'profile', 'submitted-reader', 'delivery', 'service']
        env.build_sources.assert_called_once_with(questionnaire_collection=env.questionnaires)
        env.build_submitted.assert_called_once_with()
        group, router = env.groups[0], env.routers[0]
        env.build_delivery.assert_called_once_with(
            group,
            inbox=env.graph.profile.inbox,
            presence=env.graph.admission.presence,
            fresh_admission=env.graph.admission.admission,
            submitted_reader=env.submitted,
            health_report_source=env.dependencies['health_report_source'],
            publication_ledger=env.document_resources.ledger,
        )
        env.build_service.assert_called_once()
        assert env.build_service.call_args.kwargs['profile_source_delivery'] is router
        assert env.container.questionnaire_service.profile_source_delivery is router
        assert env.container.record_service.dependencies['questionnaire_gate'] is env.container.questionnaire_service
        assert router.publication.owner is group.sources.owner
        assert router.publication.store is group.sources.store
        assert router.publication.attachments.publisher.source_reader is group.snapshot_reader
        assert router.publication.attachments.publisher.legacy_publication.ledger is env.document_resources.ledger
        env.build_worker.assert_called_once_with(env.runtime, source_snapshot_resolvers=env.graph.resolvers)
        assert env.container.health_profile_ingestion_worker is env.profile_worker
        workers = dict(env.container.source_workers)
        assert len(env.container.source_workers) == len(workers) == 3
        assert set(workers) == {'document-outbox', 'analysis-delivery', 'questionnaire-source'}
        assert workers['questionnaire-source'].recover == router.recover_pending
        assert env.container.task_supervisor.active_task_count == 0
        assert not env.chat_graph.calls and not env.chat_graph.stream_calls
        assert all(not collection.documents for collection in env.database.collections.values())
        env.graph.profile.worker.recover_pending.assert_not_awaited()
        env.profile_worker.recover_pending.assert_not_awaited()
        # Explicitly dispatching the one root runner reaches only this router's
        # new publication service, never another Profile or attachment worker.
        router.publication.recover_pending = AsyncMock(return_value=3)
        assert await workers['questionnaire-source'].recover_pending() == 3
        router.publication.recover_pending.assert_awaited_once_with(limit=100)
        env.graph.profile.worker.recover_pending.assert_not_awaited()
        env.profile_worker.recover_pending.assert_not_awaited()
    await env.container.resources.aclose()


@pytest.mark.asyncio
async def test_explicit_questionnaire_service_preserves_compatibility_without_new_q_resources_or_worker():
    old = object()
    with composed(questionnaire_service=old) as env:
        assert env.container.questionnaire_service is old
        assert env.container.record_service.dependencies['questionnaire_gate'] is old
        assert env.order == ['profile']
        env.build_sources.assert_not_called()
        env.build_delivery.assert_not_called()
        env.build_submitted.assert_not_called()
        env.build_service.assert_not_called()
        assert [name for name, _ in env.container.source_workers] == [
            'document-outbox',
            'analysis-delivery',
        ]
        assert not any(key[0] == 'questionnaire' for key in env.graph.resolvers)
        env.build_worker.assert_called_once_with(env.runtime, source_snapshot_resolvers=env.graph.resolvers)
        assert env.container.task_supervisor.active_task_count == 0
    await env.container.resources.aclose()


@pytest.mark.asyncio
async def test_explicit_historical_document_dependencies_keep_questionnaire_legacy_default():
    with composed(default_sources=False) as env:
        assert env.order == ['service']
        env.build_graph.assert_not_called()
        env.build_sources.assert_not_called()
        env.build_delivery.assert_not_called()
        env.build_submitted.assert_not_called()
        env.build_worker.assert_not_called()
        env.build_service.assert_called_once()
        assert 'profile_source_delivery' not in env.build_service.call_args.kwargs
        assert env.container.questionnaire_service.profile_source_delivery is None
        assert env.container.source_workers == ()
        assert env.container.health_profile_ingestion_worker is env.dependencies['health_profile_ingestion_worker']
        assert env.container.task_supervisor.active_task_count == 0
        assert not env.chat_graph.calls and not env.chat_graph.stream_calls
    await env.container.resources.aclose()
