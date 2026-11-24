"""指标扩展经现有 HTTP 路由完整透传；所有来源均为合成数据。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import tornado.web
from tornado.testing import AsyncHTTPTestCase

from health_profile.report_ingestion import HealthReportCandidateMapper
from tests.unit.health_profile.test_indicator_expansion import projection
from tests.unit.health_profile.test_indicator_expansion import report
from tests.unit.server.test_analysis_api_contract import FakeHealthProfileService
from tests.unit.server.test_analysis_api_contract import FakeHealthReportSource
from tests.unit.server.test_analysis_api_contract import FakeInterpreter
from tests.unit.server.test_analysis_api_contract import server as analysis_server
from tests.unit.server.test_health_profile_api_contract import server as profile_server
from tests.unit.services.analysis.fakes import InMemoryDocumentTraceStore


class IndicatorExpansionApiContractTest(AsyncHTTPTestCase):
    def get_app(self):
        self.profile_service = SimpleNamespace(get_profile=AsyncMock())
        self.interpreter = FakeInterpreter()
        self.source = FakeHealthReportSource()
        return tornado.web.Application(
            [
                (
                    r'/api/v1/health-profile/get',
                    profile_server.HealthProfileGetHandler,
                    {'service': self.profile_service},
                ),
                (
                    r'/api/v1/interpret/health-report',
                    analysis_server.HealthReportHandler,
                    {
                        'interpreter': self.interpreter,
                        'report_source': self.source,
                        'health_profile_service': FakeHealthProfileService(),
                        'document_trace_store': InMemoryDocumentTraceStore(),
                    },
                ),
            ]
        )

    def post(self, path, body):
        response = self.fetch(path, method='POST', headers={'Content-Type': 'application/json'}, body=json.dumps(body))
        assert response.code == 200
        return json.loads(response.body)['data']

    def test_get_preserves_mixed_value_types_groups_and_full_sources(self):
        mapped = HealthReportCandidateMapper().map(
            report(
                [
                    {'itemName': '身高', 'resultValue': '170', 'unit': 'cm'},
                    {'itemName': '血糖', 'resultValue': '<5', 'unit': 'mmol/L'},
                    {'itemName': '菌群多样性', 'resultValue': '3.5'},
                    {'itemName': '肠道年龄', 'resultValue': '30', 'unit': '岁'},
                    {'itemName': '测试未标准化项目', 'resultValue': '阴性'},
                ]
            )
        )
        expected = projection(mapped.candidates)
        self.profile_service.get_profile.return_value = expected
        actual = self.post('/api/v1/health-profile/get', {'userId': 'test-api-user'})
        assert actual == expected
        assert [group['code'] for group in actual['indicatorGroups']] == [
            'anthropometric',
            'laboratory',
            'microbiome',
            'assessment',
            'report_observation',
        ]
        assert actual['indicatorGroups'][1]['indicators'][0]['current']['comparator'] == '<'
        assert actual['indicatorGroups'][-1]['indicators'][0]['current']['value'] == '阴性'

    def test_interpret_preserves_microbiome_observations_in_response_and_storage(self):
        self.interpreter.analysis = {
            'reportType': 'microbiome',
            'reportTime': '2026-09-01',
            'expansion': [],
            'observations': [{'itemName': '菌群多样性', 'resultValue': '3.5', 'method': '测试方法'}],
        }
        actual = self.post(
            '/api/v1/interpret/health-report',
            {
                'userId': 'test-api-user',
                'reportId': 'test-api-report',
                'fileId': 'https://example.test/report.pdf',
            },
        )
        assert actual['reportType'] == 'microbiome'
        assert actual['observations'] == self.interpreter.analysis['observations']
        assert self.source.save_calls[0]['result'] == self.interpreter.analysis
