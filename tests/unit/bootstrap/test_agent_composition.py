"""Main Agent registration is production wiring, not required for data queries."""

from unittest.mock import patch

import pytest

from bootstrap.factory import build_container
from modules.chat.agent import CHAT_MAIN_SELECTION
from tests.unit.bootstrap.test_factory import _dependencies
from tests.unit.bootstrap.test_factory import _RecordingManager
from tests.unit.questionnaire.test_lifecycle import harness as harness
from tests.unit.services.chat.test_chat_main_agent import manager_for


@pytest.mark.asyncio
async def test_composition_registers_existing_chat_main_without_running_a_model():
    manager, collection, graph, leases, _ = manager_for()
    runtime = manager._agent_runtime
    with patch('bootstrap.factory.create_application_health_report_interpreter'):
        container = build_container(
            runtime,
            **_dependencies(),
            qa_manager_type=_RecordingManager,
            chat_manager_type=lambda *args, **kwargs: manager,
            record_document_port=object(),  # Document construction is covered by its composition tests.
            report_pipeline=object(),
            report_followup_service=object(),
            gut_barrier_service=object(),
        )
    try:
        assert runtime.ports.get(CHAT_MAIN_SELECTION) is manager.agent_port
        assert container.chat_service is manager
        assert not graph.calls and collection.document is None and not leases.release_calls
    finally:
        await container.resources.aclose()


@pytest.mark.asyncio
async def test_composition_registers_record_main_and_injects_paired_durable_stores():
    from modules.record.agent import RECORD_AGENT_SELECTION
    from tests.unit.agents.graphs.record.test_dynamic_record import setup_manager

    manager, module, gateway, runtime = setup_manager()
    captured = {}

    def build_record(*args, **kwargs):
        captured.update(kwargs)
        return manager

    dependencies = _dependencies()
    with patch('bootstrap.factory.create_application_health_report_interpreter'):
        container = build_container(
            runtime,
            **dependencies,
            qa_manager_type=build_record,
            chat_manager_type=_RecordingManager,
            record_document_port=object(),  # Document construction is covered by its composition tests.
            report_pipeline=object(),
            report_followup_service=object(),
            gut_barrier_service=object(),
        )
    try:
        assert runtime.ports.get(RECORD_AGENT_SELECTION) is manager.record_agent.port
        assert captured['reply_recovery_store'] is dependencies['record_reply_recovery_store']
        assert captured['completion_store'] is dependencies['record_completion_store']
        assert not gateway.requests
        assert module.redis_client.value is None
    finally:
        await container.resources.aclose()


@pytest.mark.asyncio
async def test_composition_installs_questionnaire_without_generation_or_storage(harness):
    from modules.questionnaire.agent import QUESTIONNAIRE_MAIN_SELECTION

    manager, _, _, _, _ = manager_for()
    runtime = manager._agent_runtime
    with patch('bootstrap.factory.create_application_health_report_interpreter'):
        container = build_container(
            runtime,
            **_dependencies(),
            qa_manager_type=_RecordingManager,
            chat_manager_type=_RecordingManager,
            record_document_port=object(),  # Document construction is covered by its composition tests.
            report_pipeline=object(),
            report_followup_service=object(),
            gut_barrier_service=object(),
            questionnaire_service=harness.service,
        )
    try:
        assert runtime.ports.get(QUESTIONNAIRE_MAIN_SELECTION) is harness.service.agent_port
        harness.generator.generate.assert_not_awaited()
        assert not harness.store.docs
    finally:
        await container.resources.aclose()
