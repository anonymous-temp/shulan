"""Content-result recovery uses durable OCR and never publishes pending facts."""

import json
from copy import deepcopy

import pytest

from adapters.analysis.document_trace import MongoDocumentTraceStore
from modules.analysis.application.service import HealthReportUpstreamError
from tests.unit.services.analysis.fakes import FakeDocumentCollection
from tests.unit.services.analysis.test_document_provenance import build_flow
from tests.unit.services.analysis.test_document_provenance import report_text
from tests.unit.services.analysis.test_document_reuse import request
from tests.unit.services.analysis.test_document_workflow import extended_flow
from tests.unit.services.analysis.test_document_workflow import lab


@pytest.mark.asyncio
async def test_exhausted_output_recovers_on_explicit_request_with_the_same_ocr_and_document():
    flow = extended_flow()
    expected = lab()
    flow.client.result = {'success': True, 'text': report_text(expected)}
    flow.graph.content = 'invalid-json'
    first = await flow.service.process(**request())
    assert first['processing']['status'] == 'unavailable'
    assert first['processing']['retryable'] is True
    assert first['status'] == 'pending'
    assert first['observations'] == []
    assert '解读未完成' in first['processing']['warnings'][0]
    assert 'sourceRevision' not in first
    assert not flow.jobs.jobs and not flow.collection.documents
    archived = await flow.service.read_document_payload(first['documentId'], user_id='user-1', name='interpretation')
    assert archived['processing']['contentOutcome'] == 'interpretation_incomplete'
    assert archived['processing']['failureKind'] == 'content'
    assert not archived.get('metadataEvidence')

    flow.graph.content = json.dumps(expected)
    second = await flow.service.process(user_id='user-1', document_id=first['documentId'])
    assert second['documentId'] == first['documentId']
    assert second['processing']['status'] == 'complete'
    assert second['processing']['warnings'] == []
    assert second['observations'][0]['resultValue'] == '468'
    assert second['unresolvedItems'] == []
    assert second['sourceRevision'] == 1
    assert len(flow.jobs.jobs) == 1
    assert len(flow.client.submit_calls) == 1
    archived = await flow.service.read_document_payload(first['documentId'], user_id='user-1', name='interpretation')
    assert archived['processing']['contentOutcome'] is None
    assert archived['processing']['failureKind'] is None


@pytest.mark.asyncio
async def test_nonretryable_empty_ocr_reuses_settled_round_without_resubmitting_it():
    flow = extended_flow()
    flow.client.result = {'success': True, 'text': ''}
    first = await flow.service.process(**request())
    document_id = first['documentId']
    before = deepcopy(flow.index_collection.documents[document_id])
    second = await flow.service.process(user_id='user-1', document_id=document_id)
    after = flow.index_collection.documents[document_id]
    assert first['processing']['status'] == second['processing']['status'] == 'unavailable'
    assert first['processing']['retryable'] is second['processing']['retryable'] is False
    assert after['interpretationRound'] == before['interpretationRound']
    assert after['interpretationValidatedVersion'] == before['interpretationValidatedVersion']
    assert after['payloadRefs'] == before['payloadRefs']
    assert len(flow.client.submit_calls) == 1
    assert not flow.graph.calls
    assert not flow.jobs.jobs and not flow.collection.documents


@pytest.mark.asyncio
async def test_empty_ocr_archive_failure_releases_lease_and_retry_preserves_the_original_ocr():
    flow = extended_flow()
    flow.client.result = {'success': True, 'text': ''}
    write = flow.traces.write_payload

    async def fail_interpretation(trace_id, user_id, name, payload):
        if name == 'interpretation':
            raise RuntimeError('required archive unavailable')
        await write(trace_id, user_id, name, payload)

    flow.traces.write_payload = fail_interpretation
    with pytest.raises(RuntimeError, match='required archive unavailable'):
        await flow.service.process(**request())
    document = next(iter(flow.index_collection.documents.values()))
    assert document['status'] == 'failed'
    assert document['leaseToken'] is document['leaseExpiresAt'] is None
    assert 'docmind_result' in document['payloadRefs']
    assert document['contentFailure'] is False
    flow.traces.write_payload = write
    result = await flow.service.process(user_id='user-1', document_id=document['_id'])
    assert result['processing']['status'] == 'unavailable'
    assert len(flow.client.submit_calls) == 1
    assert not flow.jobs.jobs and not flow.collection.documents


@pytest.mark.asyncio
async def test_legacy_content_result_retains_the_resolved_subject_identity():
    flow = build_flow(raw_result={'success': True, 'text': ''})
    result = await flow.service.process(**request())
    assert result['subjectId'] == 'subject-self'
    assert result['documentId'] in flow.traces.documents
    assert flow.traces.documents[result['documentId']]['subjectId'] == result['subjectId']


@pytest.mark.asyncio
async def test_legacy_required_content_classification_write_failure_is_not_a_200():
    flow = build_flow(raw_result={'success': True, 'text': ''})
    update = flow.traces.update

    async def fail_classification(trace_id, user_id, fields):
        if fields.get('contentOutcome'):
            raise RuntimeError('outcome status storage unavailable')
        await update(trace_id, user_id, fields)

    flow.traces.update = fail_classification
    with pytest.raises(RuntimeError, match='outcome status storage unavailable'):
        await flow.service.process(**request())
    assert not flow.jobs.jobs and not flow.collection.documents


@pytest.mark.asyncio
@pytest.mark.parametrize('source_type', ['record', 'report'])
async def test_content_only_result_remains_unavailable_to_shared_clinical_callers(source_type):
    flow = extended_flow()
    flow.client.result = {'success': True, 'text': '|项目|结果|\n|---|---|\n|血红蛋白||\n'}
    flow.graph.content = json.dumps(
        {
            'reportType': 'lab',
            'observations': [],
            'unresolvedItems': [{'itemName': '血红蛋白', 'resultValue': None, 'reason': '原文结果栏为空'}],
        }
    )
    public = await flow.service.process(**request())
    assert public['processing']['status'] == 'partial'
    assert public['unresolvedItems'][0]['itemName'] == '血红蛋白'
    assert public['unresolvedItems'][0]['resultValue'] is None
    with pytest.raises(HealthReportUpstreamError):
        await flow.service.process_document(user_id='user-1', document_id=public['documentId'], source_type=source_type)
    assert not flow.collection.documents and not flow.jobs.jobs
    assert not flow.observations.collection.documents


@pytest.mark.asyncio
async def test_pending_items_and_content_classification_survive_real_bson_archive_roundtrip():
    flow = extended_flow()
    flow.traces = MongoDocumentTraceStore(FakeDocumentCollection(), FakeDocumentCollection())
    flow.service._document_trace_store = flow.traces
    flow.client.result = {'success': True, 'text': '|项目|结果|单位|\n|---|---|---|\n|血红蛋白||g/L|\n'}
    flow.graph.content = json.dumps(
        {
            'reportType': 'lab',
            'observations': [],
            'unresolvedItems': [
                {'itemName': '血红蛋白', 'resultValue': None, 'unit': 'g/L', 'reason': '原文结果栏为空'}
            ],
        }
    )
    first = await flow.service.process(**request())
    archived = await flow.service.read_document_payload(first['documentId'], user_id='user-1', name='interpretation')
    assert archived['processing']['failureKind'] == 'content'
    assert archived['unresolvedItems'][0]['itemName'] == '血红蛋白'
    repeated = await flow.service.process(user_id='user-1', document_id=first['documentId'])
    assert repeated['unresolvedItems'] == first['unresolvedItems']
    assert repeated['observations'] == []
    assert len(flow.client.submit_calls) == 1
    assert not flow.collection.documents and not flow.jobs.jobs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'raw,reason',
    [
        ({'success': False, 'text': ''}, 'ocr_service_failed'),
        ({'success': 'true', 'text': ''}, 'ocr_response_invalid'),
        ({'success': True, 'text': []}, 'ocr_response_invalid'),
    ],
)
async def test_ocr_technical_classification_is_persisted_and_replayed(raw, reason):
    flow = extended_flow()
    flow.client.result = raw
    with pytest.raises(HealthReportUpstreamError) as caught:
        await flow.service.process(**request())
    assert caught.value.__cause__.reason_code == reason
    document = next(iter(flow.index_collection.documents.values()))
    assert document['reasonCode'] == reason
    with pytest.raises(HealthReportUpstreamError) as replayed:
        await flow.service.process(user_id='user-1', document_id=document['_id'])
    assert replayed.value.__cause__.reason_code == reason
    assert len(flow.client.submit_calls) == 1
    assert not flow.collection.documents and not flow.jobs.jobs
