"""Root installs one source graph, preserving explicit historical processor injection."""

import json
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import create_autospec
from unittest.mock import patch

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from adapters.analysis.document_trace import MongoDocumentTraceStore
from adapters.analysis.publication import MongoDocumentPublicationLedger
from agents import AgentHub
from agents import AgentRuntime
from agents import GraphRegistry
from agents.llm import LLMResult
from agents.llm import ModelProfileRegistry
from agents.persistence import AgentLeaseManager
from agents.runtime.application import register_application_modules
from bootstrap.factory import build_container
from health_profile.report_source import HealthReportSaveResult
from health_profile.report_source import HealthReportSourceRecord
from tests.unit._support.agents import FakeLLMGateway
from tests.unit._support.current_document import REPORT_TEXT
from tests.unit._support.current_document import UPLOAD
from tests.unit._support.current_document import bson_collection
from tests.unit._support.current_document import model_report
from tests.unit._support.current_document import preparation_upstream
from tests.unit._support.document_bson import LedgerBsonCollection
from tests.unit.bootstrap.test_factory import _dependencies
from tests.unit.bootstrap.test_factory import _RecordingManager
from tests.unit.services.chat.test_chat_main_agent import manager_for


@pytest.mark.asyncio
async def test_production_document_selection_uses_one_shared_gate_and_one_profile_worker():
    manager, _, _, _, _ = manager_for()
    runtime = manager._agent_runtime
    dependencies = _dependencies()
    dependencies.pop('document_output_profiles')
    dependencies.pop('health_profile_ingestion_worker')
    resources = SimpleNamespace(ledger=object())
    graph = SimpleNamespace(
        profile=SimpleNamespace(reader=object(), basic_info=None),
        publication=SimpleNamespace(lifecycle=object()),
        current=SimpleNamespace(source=object(), recovery=SimpleNamespace(recover_pending=AsyncMock())),
        resolvers={'actual-snapshots': object()},
    )
    consumers = SimpleNamespace(
        record=object(),
        report=object(),
        analysis=SimpleNamespace(service=object(), delivery=SimpleNamespace(recover_pending=AsyncMock())),
    )
    with (
        patch('bootstrap.document_sources.create_application_document_source_resources', return_value=resources),
        patch('bootstrap.document_sources.build_current_document_source_graph', return_value=graph) as build_graph,
        patch('bootstrap.document_sources.build_current_document_consumers', return_value=consumers) as build_consumers,
        patch('bootstrap.factory.create_application_health_report_interpreter'),
        patch('bootstrap.factory.HealthReportProcessingService') as processing,
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
        assert container.analysis_service_factory() is consumers.analysis.service
        assert container.record_service.dependencies['record_document_port'] is consumers.record
        assert build_graph.call_args.kwargs['processing'] is processing.return_value
        assert build_graph.call_args.kwargs['resources'] is resources
        assert build_consumers.call_args.kwargs['graph'] is graph
        assert processing.call_count == 1
        assert processing.call_args.kwargs['publication_ledger'] is resources.ledger
        profile_worker.assert_called_once_with(runtime, source_snapshot_resolvers=graph.resolvers)
        assert [name for name, _ in container.source_workers] == [
            'document-outbox',
            'analysis-delivery',
        ]
        assert container.task_supervisor.active_task_count == 0
    await container.resources.aclose()


@pytest.mark.asyncio
async def test_explicit_historical_processor_keeps_legacy_branch_and_no_new_source_workers():
    manager, _, _, _, _ = manager_for()
    factory = Mock(return_value=object())
    with patch('bootstrap.document_sources.create_application_document_source_resources') as resources:
        container = build_container(
            manager._agent_runtime,
            **_dependencies(),
            health_report_processing_service_factory=factory,
            report_pipeline=object(),
            report_followup_service=object(),
            gut_barrier_service=object(),
            questionnaire_service=object(),
            qa_manager_type=_RecordingManager,
            chat_manager_type=_RecordingManager,
        )
        assert container.analysis_service_factory is factory
        assert container.source_workers == ()
        resources.assert_not_called()
    await container.resources.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('override', ['health_profile_ingestion_worker', 'record_document_port'])
async def test_application_registration_interprets_new_upload_with_injected_consumer(override):
    upstream = preparation_upstream()
    upstream.client.result = {'success': True, 'text': REPORT_TEXT}
    gateway = FakeLLMGateway(complete_results=[LLMResult(content=json.dumps(model_report()))])
    registry = GraphRegistry()
    runtime = AgentRuntime(
        hub=AgentHub(registry),
        registry=registry,
        profiles=ModelProfileRegistry(),
        llm_gateway=gateway,
        context_repository=object(),
        lease_manager=create_autospec(AgentLeaseManager, instance=True),
        checkpointer=InMemorySaver(),
        _close_callback=lambda: None,
    )
    register_application_modules(runtime, question_topics={'topics': [], 'rules': []})
    assert runtime.module_statuses['analysis']['ready']
    assert runtime.registry.versions('analysis.health_report') == ()

    async def save_report(**fields):
        return HealthReportSaveResult(
            record=HealthReportSourceRecord(
                **fields,
                source_revision=1,
                ingestion_status='pending',
                ingestion_job_id=None,
                created_at=datetime(2026, 11, 15, tzinfo=UTC),
                updated_at=datetime(2026, 11, 15, tzinfo=UTC),
            ),
            created=True,
            ingestion_queued=False,
        )

    upstream.source.save.side_effect = save_report
    resources = SimpleNamespace(
        ledger=MongoDocumentPublicationLedger(sources=LedgerBsonCollection(), intents=LedgerBsonCollection())
    )
    dependencies = _dependencies()
    dependencies.pop('document_output_profiles')
    dependencies.pop('health_profile_ingestion_worker')
    dependencies.update(
        health_report_source=upstream.source,
        document_parsing_client=upstream.client,
        document_trace_store=MongoDocumentTraceStore(bson_collection(), bson_collection()),
        document_index=upstream.index,
        document_downloader=upstream.downloader,
    )
    dependencies[override] = object()
    with patch('bootstrap.document_sources.create_application_document_source_resources', return_value=resources):
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
    try:
        result = await container.analysis_service_factory().process(
            **UPLOAD, user_id='synthetic', subject_id='synthetic-self'
        )
        assert result['processing']['status'] == 'complete'
        assert [item['itemName'] for item in result['observations']] == ['ALT']
        assert len(gateway.complete_requests) == 1
        assert len(upstream.client.submit_calls) == 1
    finally:
        await container.resources.aclose()
