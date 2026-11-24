"""All default business sources share one complete Profile registration and recovery graph."""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from adapters.record.completion import MongoRecordCompletionStore
from bootstrap.chat_sources import build_chat_source_runtime
from bootstrap.chat_sources import create_application_chat_source_resources
from bootstrap.factory import build_container
from bootstrap.questionnaire_sources import build_questionnaire_source_delivery
from bootstrap.questionnaire_sources import build_questionnaire_source_resources
from bootstrap.record_sources import build_record_source_runtime
from bootstrap.record_sources import create_application_record_source_resources
from modules.health_profile.application import ProfileUserSourceResolver
from tests.unit.bootstrap.test_factory import _dependencies
from tests.unit.bootstrap.test_factory import _RecordingManager
from tests.unit.bootstrap.test_record_sources import Database
from tests.unit.services.chat.test_chat_main_agent import manager_for


@pytest.mark.asyncio
async def test_all_default_sources_register_together_and_keep_one_worker_per_domain():
    manager, _, model, _, _ = manager_for()
    runtime = manager._agent_runtime
    dependencies = _dependencies()
    for key in (
        'document_output_profiles',
        'health_profile_ingestion_worker',
        'record_profile_source',
        'record_completion_store',
        'chat_profile_source',
        'chat_session_repository',
    ):
        dependencies.pop(key)
    database = Database()
    sessions, questionnaires = database['configured_sessions'], database['configured_questionnaires']
    legacy_record = SimpleNamespace(recover_pending=AsyncMock())
    legacy_chat = SimpleNamespace(recover_pending=AsyncMock())
    completions = MongoRecordCompletionStore(sessions)
    chat_repository = object()
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
    resources, deliveries, order = {}, {}, []

    def capture(target, key, function):
        def build(*args, **kwargs):
            assert key not in target, 'each source resource/runtime must be constructed once'
            result = function(*args, **kwargs)
            target[key] = result
            order.append(('resources' if target is resources else 'delivery', key))
            return result

        return build

    def document_graph(**kwargs):
        assert set(resources) == {'questionnaire', 'record', 'chat'}
        order.append(('profile', 'shared'))
        expected = {
            ('questionnaire', 'profile.questionnaire-source.v2'): resources['questionnaire'].snapshot_reader,
            ('record', 'profile.record-source.v2'): resources['record'].material_reader,
            ('chat', 'profile.chat-source.v2'): resources['chat'].snapshot_reader,
        }
        actual = kwargs['source_resolvers']
        assert actual.keys() == expected.keys()
        for key, reader in expected.items():
            assert isinstance(actual[key], ProfileUserSourceResolver)
            assert actual[key].reader is reader
        assert kwargs['owner_fence_readers'] == {
            'questionnaire': resources['questionnaire'].fence_reader,
            'record': resources['record'].owner,
            'chat': resources['chat'].fence_reader,
        }
        graph.resolvers = {('document', 'fixed-document-contract'): object(), **actual}
        return graph

    def questionnaire_service(**kwargs):
        return SimpleNamespace(profile_source_delivery=kwargs['profile_source_delivery'])

    patches = (
        patch('db.mongodb.collection_sessions', sessions),
        patch('db.mongodb.collection_questionnaires', questionnaires),
        patch('db.mongodb.collection_questionnaire_documents', database['configured_questionnaire_documents']),
        patch('db.mongodb.collection_questionnaire_chunks', database['configured_questionnaire_chunks']),
        patch('bootstrap.factory._create_record_profile_source', return_value=legacy_record),
        patch('bootstrap.factory._create_chat_profile_source', return_value=legacy_chat),
        patch('bootstrap.factory.create_application_record_completion_store', return_value=completions),
        patch('bootstrap.factory.create_application_chat_session_repository', return_value=chat_repository),
        patch(
            'bootstrap.document_sources.create_application_document_source_resources', return_value=document_resources
        ),
        patch('bootstrap.document_sources.build_current_document_consumers', return_value=consumers),
        patch(
            'bootstrap.questionnaire_sources.build_questionnaire_source_resources',
            side_effect=capture(resources, 'questionnaire', build_questionnaire_source_resources),
        ),
        patch(
            'bootstrap.record_sources.create_application_record_source_resources',
            side_effect=capture(resources, 'record', create_application_record_source_resources),
        ),
        patch(
            'bootstrap.chat_sources.create_application_chat_source_resources',
            side_effect=capture(resources, 'chat', create_application_chat_source_resources),
        ),
        patch(
            'bootstrap.questionnaire_sources.build_questionnaire_source_delivery',
            side_effect=capture(deliveries, 'questionnaire', build_questionnaire_source_delivery),
        ),
        patch(
            'bootstrap.record_sources.build_record_source_runtime',
            side_effect=capture(deliveries, 'record', build_record_source_runtime),
        ),
        patch(
            'bootstrap.chat_sources.build_chat_source_runtime',
            side_effect=capture(deliveries, 'chat', build_chat_source_runtime),
        ),
        patch('adapters.questionnaire_factory.build_questionnaire_service', side_effect=questionnaire_service),
        patch('bootstrap.factory.create_application_health_report_interpreter'),
        patch('bootstrap.factory.HealthReportProcessingService'),
    )
    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        build_graph = stack.enter_context(
            patch('bootstrap.document_sources.build_current_document_source_graph', side_effect=document_graph)
        )
        build_worker = stack.enter_context(
            patch('bootstrap.factory._create_health_profile_ingestion_worker', return_value=profile_worker)
        )
        container = build_container(
            runtime,
            **dependencies,
            report_pipeline=object(),
            report_followup_service=object(),
            gut_barrier_service=object(),
            qa_manager_type=_RecordingManager,
            chat_manager_type=_RecordingManager,
        )
        build_graph.assert_called_once()
        assert all(kind == 'resources' for kind, _ in order[:3])
        assert order[3] == ('profile', 'shared')
        assert all(kind == 'delivery' for kind, _ in order[4:])
        q, record, chat = (deliveries[name] for name in ('questionnaire', 'record', 'chat'))
        assert container.questionnaire_service.profile_source_delivery is q
        assert q.publication.store is resources['questionnaire'].sources.store
        assert q.publication.attachments.publisher.legacy_publication.ledger is document_resources.ledger
        assert container.record_service.dependencies['questionnaire_gate'] is container.questionnaire_service
        assert container.record_service.dependencies['record_profile_source'] is record.router
        assert container.record_service.dependencies['completion_store'] is record.completion_store
        assert record.router.publications is record.completion_store.publications is record.worker
        assert record.completion_store.completions is completions
        assert record.worker.store is resources['record'].store
        assert resources['record'].owner.evidence.cache is container.record_service.dependencies['session_cache']
        assert resources['record'].owner.evidence.leases is runtime.lease_manager
        assert (
            container.chat_service.dependencies['chat_profile_source'] is container.chat_profile_source is chat.router
        )
        assert container.chat_service.dependencies['session_repository'] is chat_repository
        assert chat.worker is chat.router.publication
        assert chat.worker.store is resources['chat'].store
        build_worker.assert_called_once_with(runtime, source_snapshot_resolvers=graph.resolvers)
        assert container.health_profile_ingestion_worker is profile_worker
        workers = dict(container.source_workers)
        assert len(container.source_workers) == len(workers) == 5
        assert workers.keys() == {
            'document-outbox',
            'analysis-delivery',
            'questionnaire-source',
            'record-source',
            'chat-source',
        }
        assert workers['questionnaire-source'].recover == q.recover_pending
        assert workers['record-source'].recover == record.worker.recover_pending
        assert workers['chat-source'].recover == chat.worker.recover_pending
        assert container.task_supervisor.active_task_count == 0
        assert not model.calls and not model.stream_calls
        assert all(not c.documents and not c.reads and not c.indexes for c in database.collections.values())
        profile_worker.recover_pending.assert_not_awaited()
        graph.profile.worker.recover_pending.assert_not_awaited()
        legacy_record.recover_pending.assert_not_awaited()
        legacy_chat.recover_pending.assert_not_awaited()
    await container.resources.aclose()
