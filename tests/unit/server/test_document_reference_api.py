"""HTTP document references exercise the real shared processing service with isolated I/O."""

import json

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

from tests.unit.services.analysis.test_document_reuse import reuse_flow
from web.handlers.analysis import HealthReportHandler


class DocumentReferenceApiTest(AsyncHTTPTestCase):
    def get_app(self):
        self.flow = reuse_flow()
        return Application([(r'/interpret', HealthReportHandler, {'processing_service': self.flow.service})])

    def post(self, payload):
        return self.fetch('/interpret', method='POST', body=json.dumps(payload))

    def test_url_reference_and_changed_content_contracts(self):
        body = {'userId': 'user-1', 'fileId': 'https://example.test/file.pdf'}
        first = self.post(body)
        self.assertEqual(first.code, 200)
        first_data = json.loads(first.body)['data']
        self.assertIn('documentId', first_data)
        reused = self.post({'userId': 'user-1', 'documentId': first_data['documentId']})
        self.assertEqual(reused.code, 200)
        self.assertEqual(json.loads(reused.body)['data'], first_data)
        self.assertEqual(self.flow.downloader.download.await_count, 1)
        self.flow.downloader.download.return_value += b'changed'
        new = self.post(body)
        self.assertNotEqual(json.loads(new.body)['data']['documentId'], first_data['documentId'])
        self.assertEqual(len(self.flow.client.submit_calls), 2)

    def test_inaccessible_references_and_exclusive_sources(self):
        first = self.post({'userId': 'user-1', 'fileId': 'https://example.test/file.pdf'})
        document_id = json.loads(first.body)['data']['documentId']
        for payload, code in (
            ({'userId': 'user-1', 'documentId': 'unknown'}, 404),
            ({'userId': 'other', 'documentId': document_id}, 404),
            ({'userId': 'user-1', 'documentId': document_id, 'fileId': 'https://example.test/file.pdf'}, 400),
            ({'userId': 'user-1'}, 400),
            ([], 400),
        ):
            self.assertEqual(self.post(payload).code, code)
        self.assertEqual(len(self.flow.client.submit_calls), 1)

    def test_explicit_report_id_cannot_be_rebound_to_changed_content(self):
        body = {'userId': 'user-1', 'fileId': 'https://example.test/file.pdf', 'reportId': 'caller-report'}
        self.assertEqual(self.post(body).code, 200)
        self.flow.downloader.download.return_value += b'changed'
        self.assertEqual(self.post(body).code, 409)
        self.assertEqual(len(self.flow.client.submit_calls), 1)
