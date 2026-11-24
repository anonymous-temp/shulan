"""Report document composition keeps data queries and legacy injection independent."""

from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from agents import AgentHub
from agents import GraphRegistry
from bootstrap.factory import build_container
from tests.unit.bootstrap.test_factory import _dependencies
from tests.unit.bootstrap.test_factory import _RecordingManager


def test_explicit_document_factory_is_forwarded_without_executing_it():
    documents = Mock()
    with patch('bootstrap.factory.build_report_pipeline', return_value=SimpleNamespace()) as build:
        container = build_container(
            SimpleNamespace(hub=AgentHub(GraphRegistry())),
            **_dependencies(),
            report_document_source_factory=documents,
            report_artifact_store=object(),
            report_followup_service=object(),
            gut_barrier_service=object(),
            qa_manager_type=_RecordingManager,
            chat_manager_type=_RecordingManager,
        )
    assert build.call_args.kwargs['document_source_factory'] is documents
    assert not documents.mock_calls
    assert container.report_pipeline is build.return_value


def test_supplied_pipeline_does_not_construct_or_call_a_document_factory():
    documents = Mock()
    pipeline = object()
    container = build_container(
        object(),
        **_dependencies(),
        report_pipeline=pipeline,
        report_document_source_factory=documents,
        report_followup_service=object(),
        gut_barrier_service=object(),
        qa_manager_type=_RecordingManager,
        chat_manager_type=_RecordingManager,
    )
    assert container.report_pipeline is pipeline
    assert not documents.mock_calls


def test_report_pipeline_prefers_public_document_factory_over_legacy_processor():
    from adapters.report.factory import build_report_pipeline

    documents, processor = Mock(), Mock()
    pipeline = build_report_pipeline(
        agent_hub=AgentHub(GraphRegistry()),
        artifact_store=object(),
        document_source_factory=documents,
        document_processing_service_factory=processor,
    )
    assert pipeline.document_source_factory is documents
    assert not documents.mock_calls and not processor.mock_calls


def test_default_real_runtime_selects_fixed_consumer_without_running_it(monkeypatch):
    from tests.unit.services.chat.test_chat_main_agent import manager_for

    manager, collection, graph, leases, _ = manager_for()
    documents = Mock()
    build_documents = Mock(return_value=documents)
    monkeypatch.setattr('bootstrap.report_documents.create_application_report_document_source_factory', build_documents)
    with patch('bootstrap.factory.build_report_pipeline', return_value=SimpleNamespace()) as build:
        container = build_container(
            manager._agent_runtime,
            **_dependencies(),
            record_document_port=object(),
            profile_snapshot_reader=object(),
            report_artifact_store=object(),
            report_followup_service=object(),
            gut_barrier_service=object(),
            questionnaire_service=object(),
            qa_manager_type=_RecordingManager,
            chat_manager_type=_RecordingManager,
        )
    build_documents.assert_called_once()
    assert build.call_args.kwargs['document_source_factory'] is documents
    assert not documents.mock_calls and not graph.calls
    assert collection.document is None and not leases.release_calls
    assert container.record_service is not None and container.chat_service is not None


def test_explicit_legacy_processor_never_builds_fixed_consumer(monkeypatch):
    from tests.unit.services.chat.test_chat_main_agent import manager_for

    manager, _, graph, _, _ = manager_for()
    build_documents = Mock(side_effect=AssertionError('explicit legacy injection must remain unchanged'))
    monkeypatch.setattr('bootstrap.report_documents.create_application_report_document_source_factory', build_documents)
    processor = Mock()
    with patch('bootstrap.factory.build_report_pipeline', return_value=SimpleNamespace()) as build:
        build_container(
            manager._agent_runtime,
            **_dependencies(),
            health_report_processing_service_factory=processor,
            profile_snapshot_reader=object(),
            report_artifact_store=object(),
            report_followup_service=object(),
            gut_barrier_service=object(),
            questionnaire_service=object(),
            qa_manager_type=_RecordingManager,
            chat_manager_type=_RecordingManager,
        )
    assert 'document_source_factory' not in build.call_args.kwargs
    build_documents.assert_not_called()
    processor.assert_not_called()
    assert not graph.calls


def test_failed_default_configuration_stays_inside_report_capability(monkeypatch):
    from bootstrap.report_documents import UnavailableFixedReportDocumentFactory
    from tests.unit.services.chat.test_chat_main_agent import manager_for

    manager, _, graph, _, _ = manager_for()
    build_documents = Mock(side_effect=ValueError('synthetic private configuration detail'))
    monkeypatch.setattr('bootstrap.report_documents.build_report_document_source_factory', build_documents)
    # Only the default Report document wrapper constructs this processor here.
    with (
        patch('bootstrap.factory.HealthReportProcessingService'),
        patch('bootstrap.factory.build_report_pipeline', return_value=SimpleNamespace()) as build,
    ):
        container = build_container(
            manager._agent_runtime,
            **_dependencies(),
            record_document_port=object(),
            profile_snapshot_reader=object(),
            report_artifact_store=object(),
            report_followup_service=object(),
            gut_barrier_service=object(),
            questionnaire_service=object(),
            qa_manager_type=_RecordingManager,
            chat_manager_type=_RecordingManager,
        )
    documents = build.call_args.kwargs['document_source_factory']
    assert isinstance(documents, UnavailableFixedReportDocumentFactory)
    assert 'synthetic private' not in str(documents.document_input_manifest())
    assert container.record_service is not None and container.chat_service is not None
    assert not graph.calls


@pytest.mark.asyncio
async def test_unavailable_factory_never_disappears_into_legacy_or_network_fallback():
    from bootstrap.report_documents import UnavailableFixedReportDocumentFactory
    from modules.report.ports import DocumentServiceError
    from modules.report.schemas import RemoteFileInput

    unavailable = UnavailableFixedReportDocumentFactory()
    one = unavailable.document_input_manifest()
    one['outputSelection']['implementation_revision'] = 'tampered'
    assert unavailable.document_input_manifest()['outputSelection']['implementation_revision'] == 'report-source-v16'
    with pytest.raises(DocumentServiceError, match='configuration unavailable'):
        await unavailable.create(user_id='u', session_id='s', final_report_id='f').read(
            RemoteFileInput(url='https://example.test/file.pdf', file_type='pdf'), index=0
        )
