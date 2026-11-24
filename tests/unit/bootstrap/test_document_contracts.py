"""Composition must not parse, publish, open a model or alter query execution."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pymongo import ReadPreference

from bootstrap.document_contracts import build_document_compatibility_service
from modules.analysis.compatibility import DocumentCompatibilityService


class Collection:
    def __init__(self, database, name):
        self.database = database
        self.name = name
        self.options = {}

    def with_options(self, **kwargs):
        self.options = kwargs
        return self


class Database:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, Collection(self, name))


def test_public_document_composition_pins_names_and_durable_options_without_io():
    database = Database()
    source = Mock()
    processor = SimpleNamespace(_publication_ledger=None, _report_source=source)
    profile = Mock()
    service = build_document_compatibility_service(
        processing_service=processor, profile_service=profile, document_collection=database['custom_documents']
    )
    assert isinstance(service, DocumentCompatibilityService)
    assert set(database.collections) == {
        'custom_documents',
        'custom_documents_snapshots',
        'custom_documents_snapshot_chunks',
        'custom_documents_compatibility',
    }
    for name, collection in database.collections.items():
        if name == 'custom_documents':
            continue
        assert collection.options['read_preference'] == ReadPreference.PRIMARY
        assert collection.options['write_concern'].document == {'w': 'majority', 'j': True, 'wtimeout': 5000}
    assert not source.mock_calls and not profile.mock_calls


def test_public_composition_rejects_split_publication_gates():
    processor = SimpleNamespace(_publication_ledger=object(), _report_source=Mock())
    with pytest.raises(ValueError, match='share'):
        build_document_compatibility_service(
            processing_service=processor, profile_service=Mock(), document_collection=Database()['documents']
        )


def test_record_container_uses_explicit_public_port_without_starting_processing():
    from bootstrap.factory import build_container
    from tests.unit.bootstrap.test_factory import _dependencies
    from tests.unit.bootstrap.test_factory import _RecordingManager

    port = object()
    commands = Mock(side_effect=AssertionError('explicit port must not build Document resources'))
    container = build_container(
        object(),
        **_dependencies(),
        record_document_port=port,
        document_compatibility_service_factory=commands,
        report_pipeline=object(),
        qa_manager_type=_RecordingManager,
        chat_manager_type=_RecordingManager,
        report_followup_service=object(),
        gut_barrier_service=object(),
    )
    assert container.record_service.dependencies['record_document_port'] is port
    commands.assert_not_called()


def test_record_with_injected_workers_wires_explicit_prepared_consumers(monkeypatch):
    from unittest.mock import patch

    from adapters.record.documents import LegacyRecordDocumentAdapter
    from adapters.record.prepared_documents import PreparedRecordDocumentAdapter
    from bootstrap.factory import build_container
    from modules.record.documents import LEGACY_RECORD_DOCUMENT_CONSUMER
    from modules.record.documents import RECORD_DOCUMENT_CHECKPOINT
    from tests.unit.bootstrap.test_factory import _dependencies
    from tests.unit.bootstrap.test_factory import _RecordingManager
    from tests.unit.services.chat.test_chat_main_agent import manager_for

    manager, collection, graph, leases, _ = manager_for()
    commands = Mock()
    build = Mock(return_value=commands)
    monkeypatch.setattr('bootstrap.document_contracts.create_application_document_compatibility_service', build)
    dependencies = _dependencies()
    with (
        patch('bootstrap.factory.create_application_health_report_interpreter'),
        patch('bootstrap.factory.HealthReportProcessingService'),
    ):
        container = build_container(
            manager._agent_runtime,
            **dependencies,
            report_pipeline=object(),
            report_followup_service=object(),
            gut_barrier_service=object(),
            qa_manager_type=_RecordingManager,
            chat_manager_type=_RecordingManager,
        )
    port = container.record_service.dependencies['record_document_port']
    assert isinstance(port, PreparedRecordDocumentAdapter)
    assert port.for_consumer_revision(RECORD_DOCUMENT_CHECKPOINT) is port
    assert isinstance(port.for_consumer_revision(LEGACY_RECORD_DOCUMENT_CONSUMER), LegacyRecordDocumentAdapter)
    assert build.call_count == 1 and not commands.mock_calls and not graph.calls
    assert collection.document is None and not leases.release_calls
    assert build.call_args.kwargs['profile_service'] is dependencies['health_profile_service']


def test_record_explicit_legacy_processor_factory_does_not_create_new_resources(monkeypatch):
    from adapters.record.documents import LegacyRecordDocumentAdapter
    from bootstrap.factory import build_container
    from tests.unit.bootstrap.test_factory import _dependencies
    from tests.unit.bootstrap.test_factory import _RecordingManager

    build = Mock(side_effect=AssertionError('legacy compatibility must remain explicit'))
    monkeypatch.setattr('bootstrap.document_contracts.create_application_document_compatibility_service', build)
    processor_factory = Mock()
    container = build_container(
        object(),
        **_dependencies(),
        health_report_processing_service_factory=processor_factory,
        report_pipeline=object(),
        report_followup_service=object(),
        gut_barrier_service=object(),
        qa_manager_type=_RecordingManager,
        chat_manager_type=_RecordingManager,
    )
    assert isinstance(container.record_service.dependencies['record_document_port'], LegacyRecordDocumentAdapter)
    build.assert_not_called()
    processor_factory.assert_not_called()


def test_material_reader_shares_exact_snapshot_collections_and_has_no_processor_dependency():
    from bootstrap.document_contracts import build_document_material_reader
    from modules.analysis.materials import DocumentMaterialReader

    database = Database()
    document_collection = database['documents']
    processor = SimpleNamespace(_publication_ledger=None, _report_source=Mock())
    build_document_compatibility_service(
        processing_service=processor, profile_service=Mock(), document_collection=document_collection
    )
    snapshots = database['documents_snapshots']
    reader = build_document_material_reader(
        document_collection=document_collection, traces=database['traces'], trace_chunks=database['trace_chunks']
    )
    assert isinstance(reader, DocumentMaterialReader)
    assert database['documents_snapshots'] is snapshots
    assert all(
        row.options['read_preference'] == ReadPreference.PRIMARY
        for name, row in database.collections.items()
        if name != 'documents'
    )
    assert not hasattr(reader, 'processing_service')
