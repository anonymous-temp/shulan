from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import ANY
from unittest.mock import AsyncMock
from unittest.mock import patch

from adapters.analysis.microbiome_source import read_microbiome_source_facts
from adapters.analysis.tokenizer import count_report_input_tokens
from agents import AgentHub
from agents import GraphRegistry
from bootstrap.factory import build_application_bundle
from bootstrap.factory import build_container
from runtime import ResourceRegistry
from tests.unit.services.analysis.fakes import InMemoryDocumentTraceStore


class _RecordingManager:
    def __init__(self, runtime, **dependencies) -> None:
        self.runtime = runtime
        self.dependencies = dependencies


class _SyncResource:
    def __init__(self, events: list[str], name: str) -> None:
        self._events = events
        self._name = name

    def close(self) -> None:
        self._events.append(self._name)


def _dependencies() -> dict:
    return {
        'health_profile_service': object(),
        'health_report_source': object(),
        'record_profile_source': object(),
        'chat_profile_source': object(),
        'health_profile_memory_provider': object(),
        'health_profile_ingestion_worker': object(),
        'document_parsing_client': object(),
        'document_trace_store': InMemoryDocumentTraceStore(),
        'document_index': object(),
        'document_downloader': object(),
        'document_output_index': object(),
        'document_output_profiles': object(),
        'image_format_probe': object(),
        'record_session_repository': object(),
        'record_session_cache': object(),
        'record_creation_store': object(),
        'record_reply_recovery_store': object(),
        'record_completion_store': object(),
        'record_prompt_renderer': object(),
        'chat_session_repository': object(),
        'chat_custom_message_outbox': object(),
    }


class CompositionRootTest(unittest.IsolatedAsyncioTestCase):
    async def test_durable_report_composition_wakes_independent_delivery_worker(self) -> None:
        from modules.report.durable import DurableReportService
        from modules.report.durable import ReportCallbackWorker
        from modules.report.durable_ports import CallbackResponse
        from tests.unit.report.test_durable_reports import REQUEST
        from tests.unit.report.test_durable_reports import setup

        env = setup()
        transport = SimpleNamespace(
            post_once=AsyncMock(return_value=CallbackResponse(204)),
            aclose=AsyncMock(),
        )
        with patch('adapters.report.delivery.ReportCallbackTransport', return_value=transport):
            container = build_container(
                SimpleNamespace(hub=AgentHub(GraphRegistry())),
                **_dependencies(),
                report_pipeline=env.pipeline,
                report_artifact_store=env.archive,
                qa_manager_type=_RecordingManager,
                chat_manager_type=_RecordingManager,
            )
        self.assertIsInstance(container.report_service, DurableReportService)
        self.assertIsInstance(container.report_callback_worker, ReportCallbackWorker)
        try:
            await container.report_service.submit(REQUEST)
            await container.report_service.drain_generation()
            await container.task_supervisor.wait_idle()
            self.assertEqual(transport.post_once.await_count, 4)
            self.assertTrue(all(row['status'] == 'delivered' for row in env.deliveries.documents.values()))
            self.assertEqual(env.pipeline.calls, 1)
        finally:
            await container.resources.aclose()
        transport.aclose.assert_awaited_once()

    async def test_callback_settings_reach_worker_and_http_transport(self) -> None:
        from modules.report.durable_ports import CallbackResponse
        from tests.unit.report.test_durable_reports import REQUEST
        from tests.unit.report.test_durable_reports import setup

        env = setup()
        transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(503)), aclose=AsyncMock())
        with (
            patch.multiple(
                'config.env',
                REPORT_CALLBACK_RETRY_DELAYS_SECONDS=(9, 20),
                REPORT_CALLBACK_CONCURRENCY=2,
                REPORT_CALLBACK_TIMEOUT_SECONDS=45,
                REPORT_CALLBACK_BATCH_SIZE=17,
                REPORT_CALLBACK_RETRY_JITTER_RATIO=0,
                REPORT_CALLBACK_BACKLOG_LOG_INTERVAL_SECONDS=60,
            ),
            patch('adapters.report.delivery.ReportCallbackTransport', return_value=transport) as build_transport,
        ):
            container = build_container(
                SimpleNamespace(hub=AgentHub(GraphRegistry())),
                **_dependencies(),
                report_pipeline=env.pipeline,
                report_artifact_store=env.archive,
                qa_manager_type=_RecordingManager,
                chat_manager_type=_RecordingManager,
            )
        build_transport.assert_called_once_with(timeout_seconds=45, concurrency=2)
        worker = container.report_callback_worker
        worker.clock = env.clock
        self.assertEqual((worker.concurrency, worker.timeout_seconds, worker.batch_size), (2, 45, 17))
        self.assertEqual(worker.backlog_log_interval_seconds, 60)
        try:
            await container.report_service.submit(REQUEST)
            await container.report_service.drain_generation()
            await container.task_supervisor.wait_idle()
            self.assertEqual(transport.post_once.await_count, 4)
            for row in env.deliveries.documents.values():
                self.assertEqual((row['nextRetryAt'] - env.clock()).total_seconds(), 9)
            self.assertEqual(env.pipeline.calls, 1)
        finally:
            await container.resources.aclose()

    def test_build_container_injects_concrete_ports_once(self) -> None:
        runtime = object()
        dependencies = _dependencies()
        report_pipeline = object()

        with (
            patch('bootstrap.factory.HealthReportInterpreter') as interpreter,
            patch(
                'bootstrap.factory.HealthReportProcessingService',
            ) as processing_service,
        ):
            container = build_container(
                runtime,
                **dependencies,
                report_pipeline=report_pipeline,
                qa_manager_type=_RecordingManager,
                chat_manager_type=_RecordingManager,
            )
            analysis_service = container.analysis_service_factory()

        record = container.record_service
        chat = container.chat_service
        self.assertIs(record.runtime, runtime)
        self.assertIs(
            record.dependencies['session_repository'],
            dependencies['record_session_repository'],
        )
        self.assertIs(
            record.dependencies['session_cache'],
            dependencies['record_session_cache'],
        )
        self.assertIs(
            record.dependencies['prompt_renderer'],
            dependencies['record_prompt_renderer'],
        )
        self.assertIs(chat.dependencies['qa_session_gate'], record)
        self.assertIs(
            chat.dependencies['session_repository'],
            dependencies['chat_session_repository'],
        )
        self.assertIs(
            chat.dependencies['custom_message_outbox'],
            dependencies['chat_custom_message_outbox'],
        )
        interpreter.assert_called_once_with(
            runtime,
            docmind_client=dependencies['document_parsing_client'],
            count_input_tokens=count_report_input_tokens,
            source_fact_reader=read_microbiome_source_facts,
        )
        processing_service.assert_called_once_with(
            interpreter=interpreter.return_value,
            report_source=dependencies['health_report_source'],
            health_profile_service=dependencies['health_profile_service'],
            image_format_probe=dependencies['image_format_probe'],
            document_trace_store=dependencies['document_trace_store'],
            document_index=dependencies['document_index'],
            document_downloader=dependencies['document_downloader'],
            document_output_index=dependencies['document_output_index'],
            output_profiles=dependencies['document_output_profiles'],
            document_task_spawner=ANY,
        )
        self.assertIs(analysis_service, processing_service.return_value)
        self.assertIs(container.report_pipeline, report_pipeline)

    async def test_owned_legacy_pipeline_clients_join_resource_registry(
        self,
    ) -> None:
        events: list[str] = []

        class AsyncResource:
            def __init__(self, name: str) -> None:
                self.name = name

            async def aclose(self) -> None:
                events.append(self.name)

        evidence = AsyncResource('evidence')
        public_evidence = AsyncResource('public-evidence')
        pipeline = SimpleNamespace(
            evidence_client=evidence,
            public_evidence_client=public_evidence,
        )
        resources = ResourceRegistry()

        with patch(
            'bootstrap.factory.build_report_pipeline',
            return_value=pipeline,
        ):
            container = build_container(
                # 组合根现在会读取 agent_runtime.hub 传给报告管线工厂，
                # 参数表达式在 patch 生效前求值，因此桩运行时必须带 hub。
                SimpleNamespace(hub=AgentHub(GraphRegistry())),
                **_dependencies(),
                resources=resources,
                qa_manager_type=_RecordingManager,
                chat_manager_type=_RecordingManager,
            )

        self.assertIs(container.resources, resources)
        self.assertEqual(resources.resource_count, 2)
        await resources.aclose()
        self.assertEqual(events, ['public-evidence', 'evidence'])

    async def test_bundle_factory_owns_runtime_and_publishes_itself(self) -> None:
        events: list[str] = []
        runtime = _SyncResource(events, 'agent-runtime')
        application = SimpleNamespace(settings={})

        with patch(
            'web.application.create_web_application',
            return_value=application,
        ):
            bundle = build_application_bundle(
                runtime,
                debug=False,
                owns_agent_runtime=True,
                **_dependencies(),
                report_pipeline=object(),
                qa_manager_type=_RecordingManager,
                chat_manager_type=_RecordingManager,
            )

        self.assertIs(bundle.web_application, application)
        self.assertIs(bundle.resources, bundle.container.resources)
        self.assertIs(application.settings['application_bundle'], bundle)
        await bundle.aclose()
        self.assertEqual(events, ['agent-runtime'])

    async def test_startup_failure_preserves_error_and_schedules_cleanup(
        self,
    ) -> None:
        events: list[str] = []
        runtime = _SyncResource(events, 'agent-runtime')

        with (
            patch(
                'bootstrap.factory.build_container',
                side_effect=ValueError('composition failed'),
            ),
            self.assertRaisesRegex(ValueError, 'composition failed'),
        ):
            build_application_bundle(
                runtime,
                debug=False,
                owns_agent_runtime=True,
            )

        await asyncio.sleep(0)
        self.assertEqual(events, ['agent-runtime'])


if __name__ == '__main__':
    unittest.main()
