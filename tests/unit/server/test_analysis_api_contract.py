"""体检报告解读 Tornado API 契约测试。

测试使用隔离替身，不读取 ``.env``，不连接 DocMind、LLM 或 MongoDB。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch

import tornado.web
from tornado.testing import AsyncHTTPTestCase

from config import env as isolated_test_environment
from modules.analysis.application.documents import DocumentParsingError
from tests.unit.services.analysis.fakes import InMemoryDocumentTraceStore
from utils.report_content import report_public_content

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _stub_module(name: str, **attributes) -> ModuleType:
    module = ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    return module


class _UnusedService:
    pass


class _UnusedCollection:
    pass


async def _unused_create_report(*args, **kwargs):
    raise AssertionError('体检报告 Handler 测试不应生成最终报告')


def _load_server_without_environment():
    stubs = {
        'config.env': _stub_module(
            'config.env',
            **{
                **{key: value for key, value in vars(isolated_test_environment).items() if key.isupper()},
                'DEBUG': False,
                'ENVIRONMENT': 'test',
                'LOG_LEVEL': 'CRITICAL',
                'SERVER_PORT': 0,
            },
        ),
        'db.mongodb': _stub_module(
            'db.mongodb',
            collection_health_reports=_UnusedCollection(),
            collection_sessions=_UnusedCollection(),
        ),
        'services.chat': _stub_module(
            'services.chat',
            ChatCustomMessageConflictError=ValueError,
            ChatCustomMessageValidationError=ValueError,
            ChatSessionNotFoundError=LookupError,
            ChatSessionManager=_UnusedService,
        ),
        'services.record': _stub_module(
            'services.record',
            QASessionConflictError=type(
                'QASessionConflictError',
                (ValueError,),
                {},
            ),
            QASessionManager=_UnusedService,
        ),
        'services.report': _stub_module(
            'services.report',
            create_report=_unused_create_report,
        ),
    }
    spec = importlib.util.spec_from_file_location(
        '_shulan_analysis_contract_target',
        PROJECT_ROOT / 'server.py',
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


server = _load_server_without_environment()


class FakeInterpreter:
    def __init__(self):
        self.content = '体检报告正文'
        self.analysis = {
            'reportType': 'lab',
            'title': '血常规',
            'status': 'normal',
            'expansion': [],
        }
        self.extract_calls = []
        self.analyze_calls = []

    def extract_text(self, file_url):
        self.extract_calls.append(file_url)
        return self.content

    async def extract_document(self, *, trace, file_url, **kwargs):
        content = self.extract_text(file_url)
        await trace.write_payload('docmind_result', {'text': content, 'success': bool(content)})
        if not content:
            raise DocumentParsingError('体检报告解析失败')
        return content

    async def analyze(self, content, *, user_id, **kwargs):
        self.analyze_calls.append((content, user_id))
        return deepcopy(self.analysis)

    @staticmethod
    def _resolve_name(file_url):
        return 'report.pdf', 'pdf'


class FakeReportRecord:
    def __init__(self, result, document_id=None):
        self._result = deepcopy(result)
        self.document_id = document_id

    def public_result(self):
        return report_public_content(self._result)


class FakeHealthReportSource:
    def __init__(self):
        self.existing = None
        self.get_calls = []
        self.save_calls = []

    @staticmethod
    def resolve_report_id(requested):
        return requested or 'report-generated-1'

    @staticmethod
    def source_fingerprint(*, user_id, subject_id, file_id):
        return f'{user_id}|{subject_id}|{file_id}'

    async def get_existing(self, *, report_id, source_fingerprint):
        self.get_calls.append((report_id, source_fingerprint))
        return self.existing

    async def save(self, **values):
        self.save_calls.append(deepcopy(values))
        result = deepcopy(values['result'])
        result.update(
            {
                'reportId': values['report_id'],
                'userId': values['user_id'],
                'subjectId': values['subject_id'],
            }
        )
        return SimpleNamespace(record=FakeReportRecord(result, values.get('document_id')))


class FakeHealthProfileService:
    def __init__(self, subject_id='subject-self-default'):
        self.subject_id = subject_id
        self.ensure_calls = []

    async def ensure_self_profile(self, user_id):
        self.ensure_calls.append(user_id)
        return SimpleNamespace(
            subject=SimpleNamespace(subject_id=self.subject_id),
        )


class AnalysisApiContractTest(AsyncHTTPTestCase):
    def get_app(self):
        self.interpreter = FakeInterpreter()
        self.report_source = FakeHealthReportSource()
        self.health_profile_service = FakeHealthProfileService()
        self.document_trace_store = InMemoryDocumentTraceStore()
        return tornado.web.Application(
            [
                (
                    r'/api/v1/interpret/health-report',
                    server.HealthReportHandler,
                    {
                        'interpreter': self.interpreter,
                        'report_source': self.report_source,
                        'health_profile_service': self.health_profile_service,
                        'document_trace_store': self.document_trace_store,
                    },
                )
            ],
        )

    def tearDown(self):
        super().tearDown()

    def _post(self, body):
        return self.fetch(
            '/api/v1/interpret/health-report',
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps(body, ensure_ascii=False),
        )

    @staticmethod
    def _payload(response):
        return json.loads(response.body.decode('utf-8'))

    def test_success_preserves_response_and_storage_contract(self):
        response = self._post(
            {
                'fileId': 'https://example.test/report.pdf',
                'userId': 'user-1',
                'subjectId': 'subject-self',
                'reportId': 'report-client-1',
            }
        )

        payload = self._payload(response)
        assert response.code == 200
        assert payload['code'] == 200
        assert payload['data']['reportType'] == 'lab'
        assert payload['data']['userId'] == 'user-1'
        assert payload['data']['subjectId'] == 'subject-self'
        assert payload['data']['reportId'] == 'report-client-1'
        assert self.interpreter.extract_calls == [
            'https://example.test/report.pdf',
        ]
        assert self.interpreter.analyze_calls == [
            ('体检报告正文', 'user-1'),
        ]
        assert self.report_source.save_calls == [
            {
                'report_id': 'report-client-1',
                'user_id': 'user-1',
                'subject_id': 'subject-self',
                'source_fingerprint': ('user-1|subject-self|https://example.test/report.pdf'),
                'result': self.interpreter.analysis,
                'document_id': next(iter(self.document_trace_store.documents)),
            }
        ]
        assert self.health_profile_service.ensure_calls == []

    def test_partial_result_remains_http_200_with_quality_and_trusted_observations(self):
        self.interpreter.analysis.update(
            {
                'observations': [{'itemName': '血红蛋白', 'resultValue': '120'}],
                'processing': {
                    'status': 'partial',
                    'issues': [{'code': 'source_unreadable', 'sourceId': 'private-source'}],
                    'issueCount': 1,
                    'retryable': True,
                },
            }
        )
        response = self._post({'fileId': 'https://example.test/report.pdf', 'userId': 'user-1'})
        data = self._payload(response)['data']
        assert response.code == 200
        assert data['processing'] == {
            'status': 'partial',
            'issueCount': 1,
            'retryable': True,
            'warnings': ['部分内容尚未确认，已保留可确认的结果。'],
        }
        assert 'private-source' not in response.body.decode('utf-8')
        assert data['observations'] == self.interpreter.analysis['observations']

    def test_missing_subject_defaults_to_self_profile(self):
        response = self._post(
            {
                'fileId': 'https://example.test/report.pdf',
                'userId': 'user-default-self',
                'reportId': 'report-default-self',
            }
        )

        payload = self._payload(response)
        assert response.code == 200
        assert payload['data']['subjectId'] == 'subject-self-default'
        assert self.health_profile_service.ensure_calls == [
            'user-default-self',
        ]
        assert self.report_source.save_calls[0]['subject_id'] == ('subject-self-default')

    def test_docmind_failure_and_agent_failure_keep_502_contract(self):
        self.interpreter.content = False
        response = self._post(
            {
                'fileId': 'https://example.test/report.pdf',
                'userId': 'user-1',
            }
        )
        assert response.code == 502
        assert self._payload(response)['message'] == '体检报告解析失败'

        self.interpreter.content = '体检报告正文'
        self.interpreter.analysis = None
        previous_save_count = len(self.report_source.save_calls)
        response = self._post(
            {
                'fileId': 'https://example.test/report.pdf',
                'userId': 'user-1',
            }
        )
        assert response.code == 502
        assert self._payload(response)['message'] == '体检报告未获得可用结果'
        assert len(self.report_source.save_calls) == previous_save_count

    def test_same_report_id_replays_saved_result_without_model_call(self):
        self.report_source.existing = SimpleNamespace(
            record=FakeReportRecord(
                {
                    'reportId': 'report-client-1',
                    'userId': 'user-1',
                    'subjectId': 'subject-self-default',
                    'reportType': 'lab',
                    'status': 'normal',
                    'expansion': [],
                }
            ),
        )

        response = self._post(
            {
                'fileId': 'https://example.test/report.pdf',
                'userId': 'user-1',
                'reportId': 'report-client-1',
            }
        )

        assert response.code == 200
        assert self._payload(response)['data']['reportId'] == 'report-client-1'
        assert self.interpreter.extract_calls == []
        assert self.interpreter.analyze_calls == []
        assert self.report_source.save_calls == []

    def test_rejects_missing_or_unsupported_file(self):
        response = self._post({'userId': 'user-1'})
        assert response.code == 400
        assert self._payload(response)['message'] == '缺少必填参数: fileId'

        with patch.object(
            self.interpreter,
            '_resolve_name',
            return_value=('report.txt', 'txt'),
        ):
            response = self._post(
                {
                    'fileId': 'https://example.test/report.txt',
                    'userId': 'user-1',
                }
            )
        assert response.code == 415


class AnalysisRuntimeInjectionTest(AsyncHTTPTestCase):
    def get_app(self):
        self.runtime = object()
        self.created_with = []
        created_with = self.created_with

        def create_interpreter(runtime):
            created_with.append(runtime)
            return FakeInterpreter()

        self.interpreter_patch = patch.object(
            server,
            'create_application_health_report_interpreter',
            create_interpreter,
        )
        self.interpreter_patch.start()
        return tornado.web.Application(
            [
                (
                    r'/api/v1/interpret/health-report',
                    server.HealthReportHandler,
                    {
                        'report_source': FakeHealthReportSource(),
                        'health_profile_service': FakeHealthProfileService(),
                        'document_trace_store': InMemoryDocumentTraceStore(),
                    },
                )
            ],
            agent_runtime=self.runtime,
        )

    def tearDown(self):
        self.interpreter_patch.stop()
        super().tearDown()

    def test_handler_creates_request_scoped_interpreter_with_runtime(self):
        response = self.fetch(
            '/api/v1/interpret/health-report',
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps({'userId': 'user-1'}),
        )

        assert response.code == 400
        assert self.created_with == [self.runtime]


if __name__ == '__main__':
    import unittest

    unittest.main()
