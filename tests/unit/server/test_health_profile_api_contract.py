"""用户健康画像 Tornado API 的无外部依赖契约测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import tornado.web
from tornado.testing import AsyncHTTPTestCase

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


def _load_server_without_environment():
    stubs = {
        'config.env': _stub_module(
            'config.env',
            DEBUG=False,
            ENVIRONMENT='test',
            LOG_LEVEL='CRITICAL',
            SERVER_PORT=0,
            SSE_TEXT_BATCH_CHARS=512,
            SSE_TEXT_BATCH_SECONDS=0.05,
            LOG_FILE_ENABLED=False,
            LOG_DIR=None,
            LOG_MAX_BYTES=1048576,
            LOG_RETENTION_DAYS=1,
            LOG_BACKUP_COUNT=1,
            LOG_USER_MAX_OPEN_FILES=8,
            LOG_QUEUE_CAPACITY=16,
            HTTP_LOG_BODY_ENABLED=False,
            HTTP_LOG_STRING_CHARS=256,
            HTTP_LOG_LIST_ITEMS=8,
            HTTP_LOG_DICT_ITEMS=16,
            HTTP_LOG_DEPTH=4,
            HTTP_LOG_TOTAL_BYTES=8192,
            HTTP_LOG_PARSE_BYTES=65536,
            REPORT_CALLBACK_POLL_INTERVAL_SECONDS=5,
        ),
        'db.mongodb': _stub_module(
            'db.mongodb',
            collection_health_reports=_UnusedCollection(),
        ),
        'services.analysis': _stub_module(
            'services.analysis',
            HealthReportInterpreter=_UnusedService,
            HealthReportProcessingService=_UnusedService,
            HealthReportUnsupportedFormatError=ValueError,
            HealthReportUpstreamError=RuntimeError,
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
            QASessionConflictError=ValueError,
            QASessionManager=_UnusedService,
        ),
    }
    spec = importlib.util.spec_from_file_location(
        '_shulan_health_profile_contract_target',
        PROJECT_ROOT / 'server.py',
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


server = _load_server_without_environment()


class FakeUpsertResult:
    def __init__(self, data):
        self.data = data

    def to_api(self):
        return self.data


class FakeHealthProfileService:
    def __init__(self):
        self.calls = []
        self.error = None

    async def list_document_observations(self, user_id, **options):
        self.calls.append(('document-observations', user_id, options))
        if self.error is not None:
            raise self.error
        return {
            'items': [
                {
                    'observation': {'itemName': '菌群多样性', 'resultValue': '68'},
                    'observedAt': None,
                    'dateStatus': 'unknown',
                }
            ],
            'nextCursor': None,
        }

    async def get_profile(self, user_id, **options):
        self.calls.append(('get', user_id, options))
        if self.error is not None:
            raise self.error
        return {
            'userId': user_id,
            'subject': {'subjectId': 'subject-self', 'kind': 'self'},
            'profileVersion': 1,
            'indicatorGroups': [],
            'factGroups': [],
            'alerts': [],
            'evidence': {'status': 'not_requested', 'items': []},
        }

    async def list_subjects(self, user_id, **options):
        self.calls.append(('list', user_id, options))
        if self.error is not None:
            raise self.error
        return {
            'userId': user_id,
            'subjects': [{'subjectId': 'subject-self', 'kind': 'self'}],
        }

    async def upsert_subject(self, user_id, request_id, **options):
        self.calls.append(('upsert', user_id, request_id, options))
        if self.error is not None:
            raise self.error
        return FakeUpsertResult(
            {
                'created': True,
                'subject': {
                    'subjectId': 'subject-father',
                    'kind': 'blood_relative',
                    'displayName': options.get('display_name'),
                },
            }
        )

    async def list_facts(self, user_id, **options):
        self.calls.append(('facts-list', user_id, options))
        if self.error is not None:
            raise self.error
        return {
            'userId': user_id,
            'subjectId': options.get('subject_id') or 'subject-self',
            'profileVersion': 1,
            'items': [],
            'nextCursor': None,
        }

    async def resolve_fact(
        self,
        user_id,
        subject_id,
        fact_id,
        decision,
        **options,
    ):
        self.calls.append(
            (
                'facts-resolve',
                user_id,
                subject_id,
                fact_id,
                decision,
                options,
            )
        )
        if self.error is not None:
            raise self.error
        return {
            'decision': decision,
            'previousFactId': fact_id,
            'fact': {'factId': fact_id, 'status': 'active'},
            'profileVersion': 2,
        }

    async def delete_fact(self, user_id, subject_id, fact_id, **options):
        self.calls.append(
            (
                'facts-delete',
                user_id,
                subject_id,
                fact_id,
                options,
            )
        )
        if self.error is not None:
            raise self.error
        return {
            'factId': fact_id,
            'status': 'deleted',
            'profileVersion': 2,
            'deletedAt': '2026-08-18T08:30:00Z',
        }

    async def list_changes(self, user_id, **options):
        self.calls.append(('changes-list', user_id, options))
        if self.error is not None:
            raise self.error
        return {
            'userId': user_id,
            'subjectId': options.get('subject_id') or 'subject-self',
            'items': [],
            'nextCursor': None,
        }


class HealthProfileApiContractTest(AsyncHTTPTestCase):
    def get_app(self):
        self.service = FakeHealthProfileService()
        return tornado.web.Application(
            [
                (
                    r'/api/v1/health-profile/get',
                    server.HealthProfileGetHandler,
                ),
                (
                    r'/api/v1/health-profile/document-observations/list',
                    server.HealthProfileDocumentObservationListHandler,
                    {'service': self.service},
                ),
                (
                    r'/api/v1/health-profile/subjects/list',
                    server.HealthProfileSubjectListHandler,
                ),
                (
                    r'/api/v1/health-profile/subjects/upsert',
                    server.HealthProfileSubjectUpsertHandler,
                ),
                (
                    r'/api/v1/health-profile/facts/list',
                    server.HealthProfileFactListHandler,
                ),
                (
                    r'/api/v1/health-profile/facts/resolve',
                    server.HealthProfileFactResolveHandler,
                ),
                (
                    r'/api/v1/health-profile/facts/delete',
                    server.HealthProfileFactDeleteHandler,
                ),
                (
                    r'/api/v1/health-profile/changes/list',
                    server.HealthProfileChangeListHandler,
                ),
            ],
            health_profile_service=self.service,
        )

    def _post(self, path, body):
        return self.fetch(
            path,
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps(body, ensure_ascii=False),
        )

    @staticmethod
    def _payload(response):
        return json.loads(response.body.decode('utf-8'))

    def test_profile_get_maps_defaults_and_response_envelope(self):
        response = self._post(
            '/api/v1/health-profile/get',
            {'userId': 'user-1'},
        )

        payload = self._payload(response)
        self.assertEqual(response.code, 200)
        self.assertEqual(payload['code'], 200)
        self.assertEqual(payload['data']['profileVersion'], 1)
        self.assertEqual(payload['data']['factGroups'], [])
        self.assertEqual(
            self.service.calls,
            [
                (
                    'get',
                    'user-1',
                    {
                        'subject_id': None,
                        'include_evidence': False,
                        'indicator_limit': 12,
                        'indicator_scope': 'all',
                        'fact_statuses': None,
                    },
                )
            ],
        )

    def test_profile_get_forwards_scope_and_status_filters(self):
        response = self._post(
            '/api/v1/health-profile/get',
            {
                'userId': 'user-1',
                'indicatorScope': 'abnormal',
                'factStatuses': ['pending', 'disputed'],
            },
        )

        self.assertEqual(response.code, 200)
        self.assertEqual(
            self.service.calls[-1],
            (
                'get',
                'user-1',
                {
                    'subject_id': None,
                    'include_evidence': False,
                    'indicator_limit': 12,
                    'indicator_scope': 'abnormal',
                    'fact_statuses': ['pending', 'disputed'],
                },
            ),
        )

        array_scope = self._post(
            '/api/v1/health-profile/get',
            {'userId': 'user-1', 'indicatorScope': ['flagged', 'pending']},
        )
        self.assertEqual(array_scope.code, 200)
        self.assertEqual(
            self.service.calls[-1][2]['indicator_scope'],
            ['flagged', 'pending'],
        )

    def test_profile_get_rejects_legacy_include_pending_before_calling_service(self):
        message = (
            'includePending 已取消，请使用 factStatuses。'
            '仅查看已生效事实时传入 ["active"]，查看全部三种状态时可省略 factStatuses。'
        )
        for value in (True, False, None):
            with self.subTest(value=value):
                response = self._post('/api/v1/health-profile/get', {'userId': 'user-1', 'includePending': value})
                self.assertEqual(response.code, 422)
                payload = self._payload(response)
                self.assertEqual(payload['code'], 422)
                self.assertEqual(payload['message'], message)
                self.assertIsNone(payload['data'])
        self.assertEqual(self.service.calls, [])

    def test_document_observations_preserve_unknown_dates_and_paging_contract(self):
        path = '/api/v1/health-profile/document-observations/list'
        response = self._post(
            path,
            {
                'userId': 'user-1',
                'subjectId': 'subject-self',
                'documentId': 'document-1',
                'cursor': 'opaque-cursor',
                'limit': 7,
            },
        )
        self.assertEqual(response.code, 200)
        self.assertIsNone(self._payload(response)['data']['items'][0]['observedAt'])
        self.assertEqual(
            self.service.calls[-1],
            (
                'document-observations',
                'user-1',
                {
                    'subject_id': 'subject-self',
                    'document_id': 'document-1',
                    'cursor': 'opaque-cursor',
                    'limit': 7,
                },
            ),
        )
        self.assertEqual(self._post(path, {}).code, 400)
        self.service.error = server.HealthProfileValidationError('cursor 无效')
        self.assertEqual(self._post(path, {'userId': 'user-1'}).code, 422)

    def test_document_view_selection_and_conflicts_are_mapped_on_both_queries(self):
        response = self._post(
            '/api/v1/health-profile/document-observations/list',
            {'userId': 'user-1', 'view': 'complete'},
        )
        self.assertEqual(response.code, 200)
        self.assertEqual(self.service.calls[-1][2]['view'], 'complete')
        response = self._post(
            '/api/v1/health-profile/get',
            {'userId': 'user-1', 'documentObservationView': 'complete'},
        )
        self.assertEqual(response.code, 200)
        self.assertEqual(self.service.calls[-1][2]['document_observation_view'], 'complete')
        self.service.error = server.HealthProfileConflictError('fixed document changed')
        for path in ('/api/v1/health-profile/get', '/api/v1/health-profile/document-observations/list'):
            self.assertEqual(self._post(path, {'userId': 'user-1'}).code, 409)
        self.service.error = server.HealthProfileValidationError('invalid view')
        self.assertEqual(self._post('/api/v1/health-profile/get', {'userId': 'user-1'}).code, 422)

    def test_subject_list_and_upsert_preserve_public_field_mapping(self):
        response = self._post(
            '/api/v1/health-profile/subjects/list',
            {'userId': 'user-1', 'includeArchived': True},
        )
        self.assertEqual(response.code, 200)
        self.assertEqual(
            self.service.calls[-1],
            (
                'list',
                'user-1',
                {'include_archived': True},
            ),
        )

        response = self._post(
            '/api/v1/health-profile/subjects/upsert',
            {
                'userId': 'user-1',
                'requestId': 'request-1',
                'kind': 'blood_relative',
                'displayName': None,
                'relationship': {
                    'relationType': 'father',
                    'isBloodRelative': True,
                },
            },
        )
        payload = self._payload(response)
        self.assertEqual(response.code, 200)
        self.assertTrue(payload['data']['created'])
        self.assertIn('display_name', self.service.calls[-1][3])
        self.assertIsNone(self.service.calls[-1][3]['display_name'])
        self.assertEqual(
            self.service.calls[-1][3]['relationship']['relationType'],
            'father',
        )

    def test_missing_required_fields_are_400(self):
        response = self._post('/api/v1/health-profile/get', {})
        self.assertEqual(response.code, 400)
        self.assertEqual(
            self._payload(response)['message'],
            '缺少必填参数: userId',
        )

        response = self._post(
            '/api/v1/health-profile/subjects/upsert',
            {'userId': 'user-1'},
        )
        self.assertEqual(response.code, 400)
        self.assertEqual(
            self._payload(response)['message'],
            '缺少必填参数: requestId',
        )

    def test_domain_errors_map_to_stable_http_statuses(self):
        for error, expected_status in (
            (server.HealthProfileValidationError('字段不合法'), 422),
            (server.HealthProfileNotFoundError('健康主体不存在'), 404),
            (server.HealthProfileConflictError('主体版本冲突'), 409),
        ):
            with self.subTest(status=expected_status):
                self.service.error = error
                response = self._post(
                    '/api/v1/health-profile/get',
                    {'userId': 'user-1'},
                )
                self.assertEqual(response.code, expected_status)
                self.assertEqual(self._payload(response)['message'], str(error))

    def test_fact_and_change_handlers_preserve_request_contract(self):
        response = self._post(
            '/api/v1/health-profile/facts/list',
            {
                'userId': 'user-1',
                'subjectId': 'subject-1',
                'statuses': ['pending'],
                'limit': 10,
            },
        )
        self.assertEqual(response.code, 200)
        self.assertEqual(
            self.service.calls[-1],
            (
                'facts-list',
                'user-1',
                {
                    'subject_id': 'subject-1',
                    'categories': None,
                    'statuses': ['pending'],
                    'fact_types': None,
                    'effective_from': None,
                    'effective_to': None,
                    'cursor': None,
                    'limit': 10,
                },
            ),
        )

        response = self._post(
            '/api/v1/health-profile/facts/resolve',
            {
                'userId': 'user-1',
                'subjectId': 'subject-1',
                'factId': 'fact-1',
                'decision': 'confirm',
                'expectedFactVersion': 1,
                'expectedProfileVersion': 1,
                'requestId': 'request-confirm',
            },
        )
        self.assertEqual(response.code, 200)
        self.assertEqual(self.service.calls[-1][0], 'facts-resolve')
        self.assertEqual(
            self.service.calls[-1][-1]['request_id'],
            'request-confirm',
        )

        response = self._post(
            '/api/v1/health-profile/facts/delete',
            {
                'userId': 'user-1',
                'subjectId': 'subject-1',
                'factId': 'fact-1',
                'deleteScope': 'fact',
                'expectedFactVersion': 2,
                'expectedProfileVersion': 2,
                'requestId': 'request-delete',
            },
        )
        self.assertEqual(response.code, 200)
        self.assertEqual(self.service.calls[-1][0], 'facts-delete')

        response = self._post(
            '/api/v1/health-profile/changes/list',
            {
                'userId': 'user-1',
                'eventTypes': ['fact_deleted'],
            },
        )
        self.assertEqual(response.code, 200)
        self.assertEqual(self.service.calls[-1][0], 'changes-list')

    def test_fact_mutations_require_versions_and_request_id(self):
        response = self._post(
            '/api/v1/health-profile/facts/resolve',
            {
                'userId': 'user-1',
                'subjectId': 'subject-1',
                'factId': 'fact-1',
                'decision': 'confirm',
            },
        )
        self.assertEqual(response.code, 400)
        self.assertEqual(
            self._payload(response)['message'],
            '缺少必填参数: expectedFactVersion',
        )


if __name__ == '__main__':
    import unittest

    unittest.main()
