"""API Lab static assets must reflect edits while the page server is running."""

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import quote

from tornado.testing import AsyncHTTPTestCase

from tests.api import serve_pages


class ApiLabPagesTest(AsyncHTTPTestCase):
    def get_app(self):
        self.directory = TemporaryDirectory(prefix='shulan-api-pages-')
        self.addCleanup(self.directory.cleanup)
        self.asset = Path(self.directory.name) / 'example.js'
        self.asset.write_text('const value = 1;', encoding='utf-8')
        with patch.object(serve_pages, 'PAGES_DIR', Path(self.directory.name)):
            return serve_pages.make_app()

    def test_refresh_revalidates_an_edited_asset_without_server_restart(self):
        original = self.fetch('/example.js')
        self.assertEqual(original.code, 200)
        etag = original.headers['Etag']
        self.asset.write_text('const value = 2;', encoding='utf-8')
        refreshed = self.fetch('/example.js', headers={'If-None-Match': etag})
        self.assertEqual(refreshed.code, 200)
        self.assertEqual(refreshed.body, b'const value = 2;')
        self.assertNotEqual(refreshed.headers['Etag'], etag)


class QuestionnaireLabPagesTest(AsyncHTTPTestCase):
    def get_app(self):
        return serve_pages.make_app()

    def test_questionnaire_is_a_static_production_api_client(self):
        response = self.fetch('/record-preview.html')
        self.assertEqual(response.code, 200)
        self.assertIn(b'questionnaire.create', response.body)
        self.assertIn(b'./common.js', response.body)
        self.assertIn(b'./record-factors.js', response.body)
        self.assertIn(b'id="questionnaire-factors"', response.body)
        self.assertNotIn(b'type="file"', response.body)
        for name in ('preview_interpreter', 'preview_question_planner', 'preview_busy'):
            self.assertNotIn(name, self._app.settings)
        self.assertEqual(self.fetch('/preview').code, 404)
        self.assertEqual(self.fetch('/questionnaire').code, 404)

    def test_proxy_keeps_answer_body_and_encoded_query_separate(self):
        target = 'http://backend.invalid/api/v1/record/questionnaires/survey-1/answers?userId=user%2B1'
        captured = []

        class UpstreamResponse(BytesIO):
            status = 202
            headers = {'Content-Type': 'application/json', 'Cache-Control': 'no-store'}

        def upstream(request, timeout):
            captured.append((request, timeout))
            return UpstreamResponse(b'{"code":202,"data":{"status":"queued"}}')

        with patch.object(serve_pages, 'urlopen', upstream):
            response = self.fetch(
                '/proxy?target=' + quote(target, safe=''),
                method='POST',
                headers={'Content-Type': 'application/json'},
                body=b'{"answers":{}}',
            )
        self.assertEqual(response.code, 202)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(captured[0][0].full_url, target)
        self.assertEqual(captured[0][0].data, b'{"answers":{}}')
        self.assertEqual(response.body, b'{"code":202,"data":{"status":"queued"}}')

    def test_proxy_preserves_read_only_factor_query_and_processing_status(self):
        target = 'http://backend.invalid/api/v1/record/questionnaires/survey-1/factor-assessment?userId=user%2B1'
        captured = []

        class UpstreamResponse(BytesIO):
            status = 202
            headers = {'Content-Type': 'application/json', 'Cache-Control': 'no-store'}

        def upstream(request, timeout):
            captured.append(request)
            return UpstreamResponse(b'{"code":202,"data":{"status":"running","result":null}}')

        with patch.object(serve_pages, 'urlopen', upstream):
            response = self.fetch('/proxy?target=' + quote(target, safe=''))
        self.assertEqual(response.code, 202)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(captured[0].get_method(), 'GET')
        self.assertEqual(captured[0].full_url, target)
        self.assertIsNone(captured[0].data)
        self.assertEqual(response.body, b'{"code":202,"data":{"status":"running","result":null}}')


class HealthAnalysisLabPagesTest(AsyncHTTPTestCase):
    def get_app(self):
        return serve_pages.make_app()

    def test_page_and_assets_use_independent_synchronous_contract(self):
        response = self.fetch('/health-analysis.html')
        self.assertEqual(response.code, 200)
        self.assertIn('健康分析'.encode(), response.body)
        self.assertIn(b'./health-analysis.js', response.body)
        self.assertIn(b'./common.js', response.body)
        self.assertIn(b'health_analysis_demo', response.body)
        for name in ('health-analysis.js', 'health-analysis.css'):
            self.assertEqual(self.fetch('/' + name).code, 200)
        contract = self.fetch('/api-contracts.js')
        self.assertIn(b'health-analysis.generate', contract.body)
        self.assertIn(b'/api/v1/health-analysis', contract.body)
        self.assertEqual(self.fetch('/health-analysis').code, 404)

    def test_proxy_preserves_flat_history_body_and_single_success_response(self):
        target = 'http://backend.invalid/api/v1/health-analysis'
        captured = []

        class UpstreamResponse(BytesIO):
            status = 200
            headers = {'Content-Type': 'application/json', 'Cache-Control': 'no-store'}

        def upstream(request, timeout):
            captured.append((request, timeout))
            return UpstreamResponse(
                b'{"code":200,"data":{"healthReview":"","suggestedMaterials":"",'
                b'"advancedQuestions":"","advancedSurveySchema":null}}'
            )

        body = b'{"userId":"test-demo-empty","groupId":"test-demo-group","surveyList":[]}'
        with patch.object(serve_pages, 'urlopen', upstream):
            response = self.fetch(
                '/proxy?target=' + quote(target, safe=''),
                method='POST',
                headers={'Content-Type': 'application/json'},
                body=body,
            )
        self.assertEqual(response.code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0].data, body)
        self.assertEqual(captured[0][1], 180)
