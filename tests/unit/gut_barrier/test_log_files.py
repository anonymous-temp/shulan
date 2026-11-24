"""Real HTTP access and existing business logs use the shared file sink."""

import json
import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import tornado.testing
import tornado.web

from adapters.report import document_source
from modules.gut_barrier import service
from modules.report.durable_ports import CallbackResponse
from runtime import ApplicationLifecycle
from runtime import TaskSupervisor
from tests.unit.gut_barrier.test_service import environment
from tests.unit.gut_barrier.test_service import request
from utils.log import ContextFilter
from utils.log.context import LogContext
from utils.log.context import bind_log_context
from utils.log.context import log_context
from utils.log.files import FileLogConfig
from utils.log.files import FileLogHandler
from utils.log.files import FileLogWriter
from web import http_logging
from web.handlers.base import log_tornado_request
from web.handlers.gut_barrier import GutBarrierGenerateHandler


class GutBarrierLogFilesTest(tornado.testing.AsyncHTTPTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.writer = FileLogWriter(FileLogConfig(self.root))
        self.addCleanup(self.writer.close)
        self.sink = FileLogHandler(self.writer)
        self.sink.addFilter(ContextFilter())
        self.sink.setFormatter(logging.Formatter(
            "seq=%(log_sequence)s task_id=%(task_id)s user_id=%(user_id)s "
            "session_id=%(session_id)s request_uuid=%(request_uuid)s %(message)s",
        ))
        self.logger = logging.Logger("gut-barrier-log-files", logging.INFO)
        self.logger.addHandler(self.sink)
        for target in (
            "web.http_logging.logger", "web.handlers.base.logger",
            "runtime.supervisor.logger", "modules.report.durable.logger",
        ):
            patcher = patch(target, self.logger)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Check the production logger before attaching the test sink: calling
        # getLogger here would itself repair a standard-library logger bypass.
        for logger in (service.logger, document_source.logger):
            assert logger.propagate is False
            assert any(isinstance(filter_, ContextFilter)
                       for handler in logger.handlers for filter_ in handler.filters)
            self.addCleanup(self.restore_logger, logger, logger.level)
            logger.setLevel(logging.INFO)
            logger.addHandler(self.sink)
        super().setUp()

    def restore_logger(self, logger, level):
        logger.removeHandler(self.sink)
        logger.setLevel(level)

    def get_app(self):
        self.env = environment(
            callback_url="https://receiver.invalid/callback",
            transport=SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(204))),
        )
        self.supervisor = TaskSupervisor()
        lifecycle = ApplicationLifecycle()
        lifecycle.mark_ready()
        return tornado.web.Application([
            ("/api/v1/gut-barrier/report", GutBarrierGenerateHandler, {
                "service": self.env.service, "task_supervisor": self.supervisor,
            }),
        ], lifecycle=lifecycle, log_function=log_tornado_request)

    def text(self, name):
        self.writer.flush()
        path = self.root / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    async def post(self, payload):
        body = payload if isinstance(payload, str) else json.dumps(payload)
        return await self.http_client.fetch(
            self.get_url("/api/v1/gut-barrier/report"), method="POST", body=body,
            headers={"Content-Type": "application/json"}, raise_error=False,
        )

    @tornado.testing.gen_test
    async def test_small_and_large_requests_bind_identity_without_body_previews(self):
        self.env.service.followup_handler = AsyncMock(
            side_effect=RuntimeError("PATIENT_SECRET SIGNED_SECRET"),
        )
        for body_enabled in (True, False):
            for large in (False, True):
                user = f"owner-{body_enabled}-{large}"
                payload = request(user).model_dump(mode="json", by_alias=True, exclude_none=True)
                payload["name"] = "PATIENT_SECRET"
                payload["fileList"] = [{
                    "url": "https://files.invalid/patient?token=SIGNED_SECRET", "fileType": "pdf",
                }]
                if large:
                    payload["hospitalData"] = [{"note": "x" * 18000} for _ in range(60)]
                    assert len(json.dumps(payload).encode()) > http_logging.env.HTTP_LOG_PARSE_BYTES
                with patch.object(http_logging.env, "HTTP_LOG_BODY_ENABLED", body_enabled), patch(
                    "web.lifecycle.log_request_start", side_effect=AssertionError("body preview called"),
                ), patch(
                    "web.lifecycle.capture_response", side_effect=AssertionError("response preview called"),
                ):
                    response = await self.post(payload)
                    assert response.code == 200
                    await self.supervisor.wait_idle()
                receipt = json.loads(response.body)
                batch = receipt["data"]["finalReportId"]
                user_log = self.text(f"user/{user}/user.log")
                assert user_log.count("响应完成") == 1
                assert receipt["uuid"] in user_log
                assert f'user_id="{user}"' in user_log and 'session_id="s"' in user_log
                assert f'task_id="{batch}"' in user_log
                assert f'task_id="{batch}:gutBarrier:v2"' in user_log
                assert "follow-up invocation failed" in user_log and "报告回调已确认" in user_log
                assert "seq=" in user_log
                assert "HTTP 请求开始" not in user_log
                assert batch not in self.text("system/system.log")
        all_log = self.text("all/all.log")
        assert all_log.count("follow-up invocation failed") == 4
        assert "PATIENT_SECRET" not in all_log and "SIGNED_SECRET" not in all_log
        assert "hospitalData" not in all_log and "fileList" not in all_log
        assert self.env.pipeline.generate.await_count == 4
        assert log_context() == LogContext()

    async def assert_scan_failure(self, method, task_id):
        user = f"requester-{method}"
        with patch.object(
            self.env.service, method, AsyncMock(side_effect=RuntimeError("scan unavailable")),
        ):
            response = await self.post(
                request(user).model_dump(mode="json", by_alias=True, exclude_none=True),
            )
            assert response.code == 200
            await self.supervisor.wait_idle()
        user_log = self.text(f"user/{user}/user.log")
        assert "响应完成" in user_log
        assert "supervised task failed" not in user_log
        system = self.text("system/system.log")
        assert system.count("supervised task failed") == 1
        assert f'task_id="{task_id}"' in system
        assert all("user_id=-" in line for line in system.splitlines())

    @tornado.testing.gen_test
    async def test_generation_scan_failure_is_system_owned(self):
        await self.assert_scan_failure("drain_generation", "gut-barrier:generation")

    @tornado.testing.gen_test
    async def test_callback_wake_failure_is_system_owned(self):
        await self.assert_scan_failure("drain_delivery", "worker:gut-barrier-callback-wake")

    @tornado.testing.gen_test
    async def test_invalid_json_keeps_request_uuid_and_clears_previous_user(self):
        response = await self.post(
            request("previous").model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        assert response.code == 200
        await self.supervisor.wait_idle()
        invalid = await self.post('{"userId":')
        assert invalid.code == 400
        request_uuid = json.loads(invalid.body)["uuid"]
        assert request_uuid in self.text("system/system.log")
        assert request_uuid not in self.text("user/previous/user.log")
        assert f'request_uuid="{request_uuid}"' in self.text("system/system.log")
        assert log_context() == LogContext()

    @tornado.testing.gen_test
    async def test_document_adapter_existing_warning_uses_shared_sink(self):
        adapter = document_source.SharedReportDocuments(
            SimpleNamespace(read_document_payload=AsyncMock(side_effect=PermissionError("PRIVATE"))),
            "document-owner", "document-session", "document-batch",
        )
        outcome = SimpleNamespace(
            report={"subjectId": "self", "processing": {"status": "partial", "retryable": False}},
            structured_data={}, file_name="test.pdf", resolved_file_type="pdf",
            text="", document_id="document-id", content_sha256="checksum",
        )
        with bind_log_context(
            user_id="document-owner", session_id="document-session", task_id="document-batch",
        ):
            assert await adapter._original_tables(outcome) == ()
        user_log = self.text("user/document-owner/user.log")
        assert user_log.count("Report original table verification unavailable") == 1
        assert 'task_id="document-batch"' in user_log and "seq=" in user_log
        assert user_log == self.text("all/all.log")
        assert not self.text("system/system.log")
        assert "PRIVATE" not in user_log
