"""Independent HTTP content outcomes with real processing and isolated persistence.

No environment files, network providers, or external databases are used. Shared
clinical callers keep their existing failure gate even when HTTP can explain an
unreadable report successfully.
"""

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
import tornado.web
from tornado.testing import AsyncHTTPTestCase

from modules.analysis.application.documents import DocumentInterpretationError
from modules.analysis.application.service import HealthReportUpstreamError
from tests.unit.services.analysis.test_document_provenance import build_flow
from tests.unit.services.analysis.test_document_reuse import request
from tests.unit.services.analysis.test_document_workflow import extended_flow
from web.handlers.analysis import HealthReportHandler


def pending_result(**fields):
    return {
        'reportType': 'unknown',
        'title': None,
        'reportTime': None,
        'status': 'pending',
        'aiSummary': None,
        'observations': [],
        'expansion': None,
        'unresolvedItems': [],
        'processing': {'status': 'unavailable', 'retryable': False, 'issueCount': 0, 'issues': []},
        **fields,
    }


class AnalysisContentHttpTest(AsyncHTTPTestCase):
    def get_app(self):
        self.flow = extended_flow()
        return tornado.web.Application(
            [(r'/api/v1/interpret/health-report', HealthReportHandler, {'processing_service': self.flow.service})]
        )

    def post(self, **body):
        response = self.fetch(
            '/api/v1/interpret/health-report',
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps(
                body or {'fileId': 'https://example.test/report.pdf', 'userId': 'user-1', 'reportId': 'report-1'}
            ),
        )
        return response, json.loads(response.body)

    def assert_no_clinical_publication(self):
        assert not self.flow.collection.documents
        assert not self.flow.reports._revisions.documents
        assert not self.flow.jobs.jobs
        assert not self.flow.observations.collection.documents
        assert not any(name == 'publication' for _, name in self.flow.traces.payloads)

    def assert_content_response(self, response, payload, *, status='unavailable'):
        assert response.code == payload['code'] == 200
        data = payload['data']
        assert isinstance(data, dict)
        assert data['userId'] == 'user-1'
        assert data['documentId']
        assert data['status'] == 'pending'
        assert data['observations'] == []
        assert data['processing']['status'] == status
        assert data['processing']['warnings']
        assert 'sourceRevision' not in data
        assert 'sourceRevision' not in data['processing']
        assert 'issues' not in data['processing']
        assert 'failureKind' not in data['processing']
        assert 'contentOutcome' not in data['processing']
        assert 'metadataEvidence' not in data
        if status == 'unavailable':
            assert payload['message'] == data['processing']['warnings'][0]
        else:
            assert payload['message'] == 'success'
        self.assert_no_clinical_publication()
        return data

    def test_empty_successful_ocr_is_200_archived_and_reused_without_model_or_publication(self):
        self.flow.client.result = {'success': True, 'text': ' \n\t '}
        response, payload = self.post()
        data = self.assert_content_response(response, payload)
        assert data['unresolvedItems'] == []
        assert '正文' in payload['message']
        assert data['processing']['retryable'] is False
        assert not self.flow.graph.calls
        document = self.flow.index_collection.documents[data['documentId']]
        assert document['status'] == 'completed'
        assert 'docmind_result' in document['payloadRefs']
        assert 'interpretation' in document['payloadRefs']
        response, payload = self.post(userId='user-1', documentId=data['documentId'])
        repeated = self.assert_content_response(response, payload)
        assert repeated['processing'] == data['processing']
        assert len(self.flow.client.submit_calls) == 1
        assert not self.flow.graph.calls

    def test_failed_ocr_is_a_technical_error(self):
        self.flow.client.result = {'success': False, 'text': ''}
        response, payload = self.post()
        assert response.code == payload['code'] == 502
        assert payload['data'] is None
        assert not self.flow.graph.calls
        self.assert_no_clinical_publication()

    def test_content_only_report_id_replay_keeps_file_binding(self):
        self.flow.client.result = {'success': True, 'text': ''}
        first_response, first_payload = self.post()
        first = self.assert_content_response(first_response, first_payload)
        response, payload = self.post()
        repeated = self.assert_content_response(response, payload)
        assert repeated['reportId'] == first['reportId'] == 'report-1'
        assert repeated['documentId'] == first['documentId']
        assert len(self.flow.client.submit_calls) == 1

    def test_content_only_report_id_cannot_point_to_a_different_file(self):
        self.flow.client.result = {'success': True, 'text': ''}
        first_response, first_payload = self.post()
        self.assert_content_response(first_response, first_payload)
        self.flow.downloader.download.return_value = b'%PDF-1.7\ndifferent-content'
        response, payload = self.post()
        assert response.code == payload['code'] == 409
        assert payload['data'] is None
        assert len(self.flow.client.submit_calls) == 1
        self.assert_no_clinical_publication()

    def test_content_only_report_id_cannot_be_reused_by_another_owner(self):
        self.flow.client.result = {'success': True, 'text': ''}
        first_response, first_payload = self.post()
        self.assert_content_response(first_response, first_payload)
        response, payload = self.post(fileId='https://example.test/report.pdf', userId='user-2', reportId='report-1')
        assert response.code == payload['code'] == 409
        assert payload['data'] is None
        assert len(self.flow.client.submit_calls) == 1
        self.assert_no_clinical_publication()

    def test_malformed_successful_ocr_is_a_technical_error(self):
        self.flow.client.result = {'success': True, 'text': ['unexpected', 'shape']}
        response, payload = self.post()
        assert response.code == payload['code'] == 502
        assert payload['data'] is None
        self.assert_no_clinical_publication()

    def test_success_without_text_field_is_not_empty_report_content(self):
        self.flow.client.result = {'success': True, 'data': {}}
        response, payload = self.post()
        assert response.code == payload['code'] == 502
        self.assert_no_clinical_publication()

    def test_invalid_truncated_and_wrong_schema_outputs_return_explanatory_200(self):
        self.flow.client.result = {'success': True, 'text': '这是一段可读文字。'}
        for index, output in enumerate(('not-json', '{"reportType":"lab",', '{"observations":"wrong"}')):
            with self.subTest(output=output):
                self.flow.downloader.download.return_value = f'%PDF-1.7\ncase-{index}'.encode()
                self.flow.graph.content = output
                response, payload = self.post(
                    fileId=f'https://example.test/report-{index}.pdf', userId='user-1', reportId=f'report-{index}'
                )
                data = self.assert_content_response(response, payload)
                assert data['unresolvedItems'] == []
                assert '解读未完成' in payload['message']
                assert data['processing']['retryable'] is True
        assert 3 <= len(self.flow.graph.calls) <= 6

    def test_nonreport_content_with_valid_empty_output_returns_200(self):
        self.flow.client.result = {'success': True, 'text': '这是一段普通说明文字。'}
        self.flow.graph.content = json.dumps(pending_result())
        response, payload = self.post()
        self.assert_content_response(response, payload)
        assert '确认' in payload['message']

    def test_model_transport_failure_without_independent_information_remains_502(self):
        self.flow.client.result = {'success': True, 'text': '这是一段可读文字。'}
        self.flow.graph.ainvoke = AsyncMock(side_effect=RuntimeError('provider unavailable'))
        response, payload = self.post()
        assert response.code == payload['code'] == 502
        assert payload['data'] is None
        self.assert_no_clinical_publication()

    def test_model_timeout_without_usable_results_returns_502(self):
        self.flow.client.result = {'success': True, 'text': '报告正文'}
        self.flow.graph.ainvoke = AsyncMock(side_effect=TimeoutError)
        response, payload = self.post()
        assert response.code == payload['code'] == 502
        assert payload['data'] is None
        assert self.flow.graph.ainvoke.await_count == 1
        self.assert_no_clinical_publication()

    def test_raw_text_metadata_is_not_locally_extracted_when_the_model_is_unavailable(self):
        self.flow.client.result = {'success': True, 'text': '血液检验报告\n报告日期：2026-09-01'}
        self.flow.graph.ainvoke = AsyncMock(side_effect=RuntimeError('provider unavailable'))
        response, payload = self.post()
        assert response.code == payload['code'] == 502
        assert payload['data'] is None
        self.assert_no_clinical_publication()

    def test_pending_items_are_publicly_sanitized_and_never_published(self):
        pending = {
            'itemName': '血红蛋白',
            'resultValue': None,
            'unit': 'g/L',
            'referenceRange': None,
            'flag': None,
            'sourceId': 'private-row-source',
            'evidence': {'sourceId': 'private-row-source', 'quote': '血红蛋白 | | g/L'},
            'missingFields': ['resultValue'],
            'reason': '结果尚未确认。',
        }
        settled = pending_result(unresolvedItems=[pending])
        self.flow.interpreter.analyze = AsyncMock(return_value=settled)
        response, payload = self.post()
        data = self.assert_content_response(response, payload, status='partial')
        assert len(data['unresolvedItems']) == 1
        item = data['unresolvedItems'][0]
        assert item['itemName'] == '血红蛋白'
        assert item['resultValue'] is None
        assert item['missingFields'] == ['resultValue']
        assert item['reason']
        assert 'private-row-source' not in response.body.decode()
        assert 'evidence' not in item
        assert data['aiSummary'] is None
        archived = next(value for (_, name), value in self.flow.traces.payloads.items() if name == 'interpretation')
        assert archived['unresolvedItems'][0]['evidence'] == pending['evidence']

    def test_unknown_unavailable_issue_is_not_silently_classified_as_content(self):
        result = pending_result()
        result['processing']['issues'] = [{'code': 'unexpected_provider_failure'}]
        self.flow.interpreter.analyze = AsyncMock(return_value=result)
        response, payload = self.post()
        assert response.code == payload['code'] == 502
        self.assert_no_clinical_publication()

    def test_required_interpretation_archive_failure_remains_500(self):
        self.flow.client.result = {'success': True, 'text': ''}
        original_write = self.flow.traces.write_payload

        async def failing_write(trace_id, user_id, name, payload):
            if name == 'interpretation':
                raise RuntimeError('required archive unavailable')
            return await original_write(trace_id, user_id, name, payload)

        self.flow.traces.write_payload = failing_write
        response, payload = self.post()
        assert response.code == payload['code'] == 500
        assert payload['data'] is None
        self.assert_no_clinical_publication()
        document = next(iter(self.flow.index_collection.documents.values()))
        assert document['status'] == 'failed'
        assert document.get('leaseToken') is None
        self.flow.traces.write_payload = original_write
        response, payload = self.post(userId='user-1', documentId=document['_id'])
        self.assert_content_response(response, payload)
        assert len(self.flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_shared_clinical_entry_keeps_failure_for_publicly_displayable_metadata():
    flow = extended_flow()
    flow.client.result = {'success': True, 'text': '血液检验报告\n报告日期：2026-09-01'}
    flow.graph.ainvoke = AsyncMock(side_effect=RuntimeError('provider unavailable'))
    with pytest.raises(HealthReportUpstreamError) as caught:
        await flow.service.process_document(**request(source_type='record'))
    assert isinstance(caught.value.__cause__, DocumentInterpretationError)
    assert not flow.jobs.jobs
    assert not flow.collection.documents


@pytest.mark.asyncio
async def test_legacy_empty_ocr_returns_200_projection_without_publishing_a_report():
    flow = build_flow(raw_result={'success': True, 'text': ''})
    result = await flow.service.process(**request())
    assert result['processing']['status'] == 'unavailable'
    assert result['status'] == 'pending'
    assert result['observations'] == result['unresolvedItems'] == []
    assert result['subjectId'] == 'subject-self'
    assert 'sourceRevision' not in result
    assert not flow.jobs.jobs
    assert not flow.collection.documents
    assert any(name == 'interpretation' for _, name in flow.traces.payloads)


@pytest.mark.asyncio
async def test_legacy_content_outcome_requires_durable_final_trace_state():
    flow = build_flow(raw_result={'success': True, 'text': ''})
    original_update = flow.traces.update

    async def failing_update(trace_id, user_id, fields):
        if fields.get('status') == 'completed':
            raise RuntimeError('trace completion unavailable')
        return await original_update(trace_id, user_id, fields)

    flow.traces.update = failing_update
    with pytest.raises(RuntimeError, match='trace completion unavailable'):
        await flow.service.process(**request())
    assert not flow.jobs.jobs
    assert not flow.collection.documents


@pytest.mark.asyncio
async def test_concurrent_content_waiters_share_one_round_and_the_same_public_semantics():
    flow = extended_flow()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return pending_result()

    flow.interpreter.analyze = AsyncMock(side_effect=blocked)
    owner = asyncio.create_task(flow.service.process(**request()))
    await entered.wait()
    document_id = next(iter(flow.index_collection.documents))
    waiter = asyncio.create_task(flow.service.process(user_id='user-1', document_id=document_id))
    await asyncio.sleep(0.02)
    release.set()
    first, second = await asyncio.gather(owner, waiter)
    assert first['processing'] == second['processing']
    assert first['processing']['status'] == 'unavailable'
    assert first['documentId'] == second['documentId'] == document_id
    assert first['observations'] == second['observations'] == []
    assert flow.interpreter.analyze.await_count == 1
    assert not flow.jobs.jobs


@pytest.mark.asyncio
async def test_unknown_legacy_none_interpretation_is_not_relabelled_as_content():
    flow = build_flow()
    flow.interpreter.analyze = AsyncMock(return_value=None)
    with pytest.raises(HealthReportUpstreamError):
        await flow.service.process(**request())
    assert not flow.jobs.jobs
    assert not flow.collection.documents


@pytest.mark.asyncio
async def test_completed_historical_report_remains_saved_without_ocr_or_model_replay():
    flow = extended_flow()
    first = await flow.service.process_document(**request())
    snapshot = deepcopy(first.report)
    flow.interpreter.VERSION = 'future-content-validator'
    flow.interpreter.analyze = AsyncMock(side_effect=AssertionError('complete history must not be regenerated'))
    repeated = await flow.service.process_document(user_id='user-1', document_id=first.document_id)
    assert repeated.report == snapshot
    assert len(flow.client.submit_calls) == 1
