"""First upstream failure is 502/error; blocked resubmission is 409/warning."""

import json
from contextlib import ExitStack
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import tornado.web
from tornado.testing import AsyncHTTPTestCase

from adapters.record.documents import LegacyRecordDocumentAdapter
from modules.record.application import QASessionManager
from runtime import TaskSupervisor
from tests.unit.services.analysis.test_document_reuse import reuse_flow
from web.handlers.analysis import HealthReportHandler
from web.handlers.base import log_tornado_request
from web.handlers.record import QASessionCreateHandler


class DocumentSubmissionConflictTest(AsyncHTTPTestCase):
    def get_app(self):
        self.flow = reuse_flow()
        self.supervisor = TaskSupervisor()
        self.flow.coordinator.task_spawner = lambda coroutine: self.supervisor.create_task(
            coroutine, name='document-processing'
        )

        def uncertain(*args, **kwargs):
            self.flow.client.submit_calls.append('attempt')
            raise TimeoutError('submission response not received')

        self.flow.client.submit_local_file = uncertain
        manager = QASessionManager.__new__(QASessionManager)
        manager._record_document_port = LegacyRecordDocumentAdapter(processor=self.flow.service)

        async def create_session(user_id, session_id, **fields):
            return await manager._processHealthReports(
                user_id=user_id, session_id=session_id, file_list=fields['file_list']
            )

        return tornado.web.Application(
            [
                (
                    r'/api/v1/qa/create',
                    QASessionCreateHandler,
                    {'service': SimpleNamespace(createSession=create_session)},
                ),
                (r'/api/v1/interpret/health-report', HealthReportHandler, {'processing_service': self.flow.service}),
            ],
            log_function=log_tornado_request,
        )

    def _assert_failure_then_conflict(self, path, body, *, interrupted=False):
        with ExitStack() as stack:
            logs = [
                stack.enter_context(patch(f'{module}.logger'))
                for module in (
                    'modules.analysis.application.document_reuse',
                    'modules.record.application.service',
                    'web.handlers.base',
                    'web.handlers.analysis',
                    'web.handlers.record',
                    'runtime.supervisor',
                )
            ]

            def post():
                return self.fetch(
                    path, method='POST', headers={'Content-Type': 'application/json'}, body=json.dumps(body)
                )

            response = post()
            self.assertEqual(response.code, 502)
            self.assertEqual(json.loads(response.body)['code'], 502)
            self.assertTrue(logs[0].error.called, 'the actual upstream failure remains an error')
            self.assertTrue(logs[2].error.called, 'HTTP 502 must still be logged as an error')
            document = next(iter(self.flow.index_collection.documents.values()))
            self.assertEqual(document['status'], 'submission_unknown')
            if interrupted:
                # A crashed process can leave only the durable submission-started checkpoint.
                document.update(
                    status='running',
                    leaseToken='expired-owner',
                    leaseExpiresAt=self.flow.coordinator.now() - timedelta(seconds=1),
                )
            for log in logs:
                log.reset_mock()
            response = post()
            payload = json.loads(response.body)
            self.assertEqual(response.code, 409)
            self.assertEqual(payload['code'], 409)
            self.assertEqual(payload['message'], '文档提交结果待核查，暂不重复提交')
            self.assertIsNone(payload['data'])
            self.assertEqual(document['status'], 'submission_unknown')
            for log in logs:
                log.error.assert_not_called()
                log.exception.assert_not_called()
            logs[0].warning.assert_called_once()
            if path == '/api/v1/qa/create':
                logs[1].warning.assert_called_once()
            http_logs = [call.args[0] for call in logs[2].warning.call_args_list]
            self.assertEqual(len(http_logs), 2, 'request exception and access log both use warning')
            self.assertTrue(all('status_code=409' in message for message in http_logs))
            self.assertTrue(any(payload['uuid'] in message for message in http_logs))
        self.assertEqual(self.flow.client.submit_calls, ['attempt'])
        self.assertEqual(self.supervisor.active_task_count, 0)

    def test_record_resubmission_is_a_conflict_at_every_logging_layer(self):
        self._assert_failure_then_conflict(
            '/api/v1/qa/create',
            {'userId': 'user-1', 'sessionId': 'qa-1', 'fileList': [{'url': 'https://example.test/report.pdf'}]},
        )

    def test_interpret_resubmission_is_a_conflict_at_every_logging_layer(self):
        self._assert_failure_then_conflict(
            '/api/v1/interpret/health-report', {'userId': 'user-1', 'fileId': 'https://example.test/report.pdf'}
        )

    def test_recovering_an_interrupted_submission_is_not_a_new_background_failure(self):
        self._assert_failure_then_conflict(
            '/api/v1/qa/create',
            {'userId': 'user-1', 'sessionId': 'qa-1', 'fileList': [{'url': 'https://example.test/report.pdf'}]},
            interrupted=True,
        )
