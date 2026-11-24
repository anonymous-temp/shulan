"""Report HTTP admission/query logs and asynchronous failures use the right files."""

import json
import logging
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import tornado.testing

from utils.log import ContextFilter
from utils.log.context import log_context
from utils.log.files import FileLogConfig
from utils.log.files import FileLogHandler
from utils.log.files import FileLogWriter
from web import http_logging
from web.handlers.base import log_tornado_request


class ReportLogFilesTest(tornado.testing.AsyncHTTPTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        # macOS temp paths use /var -> /private/var; the writer correctly
        # rejects linked ancestors, so supply the actual temporary directory.
        self.root = Path(self.directory.name).resolve()
        self.writer = FileLogWriter(FileLogConfig(self.root))
        self.addCleanup(self.writer.close)
        sink = FileLogHandler(self.writer)
        sink.addFilter(ContextFilter())
        sink.setFormatter(logging.Formatter('%(message)s'))
        self.logger = logging.Logger('report-log-files', logging.INFO)
        self.logger.addHandler(sink)
        for target in ('web.http_logging.logger', 'web.handlers.report.logger',
                       'web.handlers.base.logger', 'runtime.supervisor.logger'):
            patcher = patch(target, self.logger)
            patcher.start()
            self.addCleanup(patcher.stop)
        super().setUp()

    def get_app(self):
        from tests.unit.report.test_durable_api import TestDurableReportHTTP
        app = TestDurableReportHTTP.get_app(self)
        app.settings['log_function'] = log_tornado_request
        return app

    async def post(self, path, body):
        return await self.http_client.fetch(self.get_url(path), method='POST',
                                           body=json.dumps(body), raise_error=False)

    def text(self, name):
        self.writer.flush()
        path = self.root / name
        return path.read_text(encoding='utf-8') if path.exists() else ''

    @tornado.testing.gen_test
    async def test_generate_and_query_bind_user_after_large_body_parse_with_body_disabled(self):
        with patch.multiple(http_logging.env, HTTP_LOG_BODY_ENABLED=False, HTTP_LOG_PARSE_BYTES=1):
            for user in ('a', 'b'):
                response = await self.post('/api/v1/report', {'userId': user, 'sessionId': f's-{user}'})
                assert response.code == 200
                batch = json.loads(response.body)['data']['finalReportId']
                await self.supervisor.wait_idle()
                listed = await self.post('/api/v1/report/list', {'userId': user})
                detail = await self.post('/api/v1/report/get', {
                    'userId': user, 'finalReportId': batch, 'reportType': 'evaluate',
                })
                assert listed.code == detail.code == 200
                assert json.loads(detail.body)['data']['body']['userId'] == user
                text = self.text(f'user/{user}/user.log')
                for item in (response, listed, detail):
                    assert json.loads(item.body)['uuid'] in text
                assert text.count('响应完成') == 3
                assert 'HTTP 请求开始' not in text
                assert ' body=' not in text
        assert self.text('system/system.log').count('HTTP 请求开始') == 6
        assert log_context().user_id is None

    @tornado.testing.gen_test
    async def test_generation_scan_failure_is_system_log_and_admission_keeps_user(self):
        self.env.service.drain_generation = AsyncMock(side_effect=RuntimeError('scan unavailable'))
        response = await self.post('/api/v1/report', {'userId': 'requester', 'sessionId': 's'})
        await self.supervisor.wait_idle()
        assert response.code == 200
        assert json.loads(response.body)['data']['status'] == 'processing'
        user_log = self.text('user/requester/user.log')
        assert 'HTTP 请求开始' in user_log and user_log.count('响应完成') == 1
        assert 'supervised task failed' not in user_log
        assert self.text('system/system.log').count('supervised task failed') == 1
