"""Bounded submission recovery, ownership and preservation of the canonical report."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from adapters.analysis.docmind import BoundedDocMindClient
from modules.analysis.application.documents import DocumentBusyError
from modules.analysis.application.documents import DocumentSubmissionConflictError
from modules.analysis.application.documents import DocumentSubmissionRejectedError
from modules.analysis.application.service import HealthReportUpstreamError
from scripts.recover_qa_creation import recover_document

from .test_document_reuse import request
from .test_document_reuse import reuse_flow


def move_past_cooldown(flow):
    now = flow.coordinator.now() + timedelta(seconds=301)
    flow.coordinator.now = lambda: now


@pytest.mark.asyncio
async def test_only_one_controlled_resubmission_is_shared_across_sessions_and_entrypoints():
    flow = reuse_flow()
    submit = Mock(side_effect=TimeoutError('response lost'))
    flow.client.submit_local_file = submit
    with pytest.raises(HealthReportUpstreamError):
        await flow.service.process_document(**request(session_id='a'))
    move_past_cooldown(flow)
    with pytest.raises(HealthReportUpstreamError):
        await flow.service.process_document(**request(session_id='b', source_type='record'))
    move_past_cooldown(flow)
    with pytest.raises(DocumentSubmissionConflictError) as error:
        await flow.service.process_document(**request(session_id='c', source_type='report'))
    assert error.value.retry_after_seconds is None
    assert submit.call_count == 2
    document = next(iter(flow.index_collection.documents.values()))
    assert document['resubmissionCount'] == 1
    assert len(document['recoveryHistory']) == 1
    assert document['recoveryHistory'][0]['previousTraceId'] == document['_id']
    assert len(flow.traces.documents) == 3
    assert not flow.jobs.jobs


@pytest.mark.asyncio
async def test_competing_recovery_claims_cannot_duplicate_resubmission_or_accept_late_write():
    flow = reuse_flow()
    original = flow.client.submit_local_file
    flow.client.submit_local_file = Mock(side_effect=TimeoutError())
    with pytest.raises(HealthReportUpstreamError):
        await flow.service.process_document(**request())
    document = next(iter(flow.index_collection.documents.values()))
    now = flow.coordinator.now()
    assert await flow.index.claim_resubmission(deepcopy(document), 'early', now, now + timedelta(seconds=120)) is None
    move_past_cooldown(flow)
    now = flow.coordinator.now()
    snapshot = deepcopy(document)
    claims = await asyncio.gather(
        *(flow.index.claim_resubmission(snapshot, token, now, now + timedelta(seconds=120)) for token in ('one', 'two'))
    )
    assert sum(item is not None for item in claims) == 1
    with pytest.raises(DocumentBusyError):
        await flow.index.update_owned(snapshot, 'stale-owner', now, {'taskId': 'late-task'})
    winner = next(item for item in claims if item)
    await flow.index.update_owned(winner, winner['leaseToken'], now, {'leaseToken': None, 'leaseExpiresAt': None})
    flow.client.submit_local_file = original
    result = await flow.service.process_document(user_id='user-1', document_id=document['_id'])
    assert result.document_id == document['_id']
    assert len(flow.client.submit_calls) == len(flow.jobs.jobs) == 1


@pytest.mark.asyncio
async def test_local_preparation_failure_is_retriable_without_marking_submission_unknown():
    flow = reuse_flow()
    with patch('modules.analysis.application.documents.prepare_document', side_effect=OSError('disk full')):
        with pytest.raises(OSError):
            await flow.service.process_document(**request())
    document = next(iter(flow.index_collection.documents.values()))
    assert document['status'] == 'failed'
    assert not document.get('submissionStarted')
    assert not flow.client.submit_calls
    await flow.service.process_document(**request())
    assert len(flow.client.submit_calls) == 1
    assert document.get('resubmissionCount', 0) == 0


@pytest.mark.asyncio
async def test_explicit_rejection_does_not_consume_uncertain_resubmission():
    flow = reuse_flow()
    flow.client.submit_local_file = Mock(
        side_effect=DocumentSubmissionRejectedError('invalid credentials', retryable=False)
    )
    with pytest.raises(HealthReportUpstreamError):
        await flow.service.process_document(**request())
    document = next(iter(flow.index_collection.documents.values()))
    assert document['status'] == 'failed'
    assert document['submissionStarted'] is False and document['retryable'] is False
    with pytest.raises(HealthReportUpstreamError):
        await flow.service.process_document(**request())
    assert flow.client.submit_local_file.call_count == 1
    assert document.get('resubmissionCount', 0) == 0


@pytest.mark.asyncio
async def test_operator_binding_known_task_resumes_original_without_new_submission():
    flow = reuse_flow()
    flow.client.submit_local_file = Mock(side_effect=TimeoutError())
    with pytest.raises(HealthReportUpstreamError):
        await flow.service.process_document(**request())
    document = next(iter(flow.index_collection.documents.values()))
    options = dict(user_id='user-1', subject_id='subject-self', document_id=document['_id'], task_id='verified-task')
    preview = await recover_document(flow.index, **options)
    assert preview['applied'] is False and document['status'] == 'submission_unknown'
    result = await recover_document(flow.index, **options, apply=True, reason='matched provider request log')
    assert result['applied']
    await flow.service.process_document(user_id='user-1', document_id=document['_id'])
    assert flow.client.submit_local_file.call_count == 1
    assert len(flow.jobs.jobs) == 1
    with pytest.raises(ValueError, match='不属于'):
        await recover_document(flow.index, **{**options, 'user_id': 'someone-else'})


@pytest.mark.asyncio
async def test_terminal_parse_failure_keeps_known_task_and_stops_automatic_submission():
    flow = reuse_flow()
    flow.client.get_result = Mock(return_value={'success': False, 'error': 'unsupported content'})
    with pytest.raises(HealthReportUpstreamError):
        await flow.service.process_document(**request())
    with pytest.raises(HealthReportUpstreamError):
        await flow.service.process_document(**request())
    document = next(iter(flow.index_collection.documents.values()))
    assert document['taskId'] == 'task-local-1'
    assert document['retryable'] is False
    assert len(flow.client.submit_calls) == flow.client.get_result.call_count == 1


def client_for(provider):
    client = BoundedDocMindClient.__new__(BoundedDocMindClient)
    client.client = provider
    return client


def test_docmind_submission_disables_sdk_retries_and_preserves_uncertainty():
    provider = SimpleNamespace(
        submit_doc_structure_job_with_options=Mock(
            return_value=SimpleNamespace(body=SimpleNamespace(data=SimpleNamespace(id='task')))
        )
    )
    client = client_for(provider)
    assert client.submit_online_file('https://example.test/report.pdf') == 'task'
    request, runtime = provider.submit_doc_structure_job_with_options.call_args.args
    assert request.file_name == 'report.pdf'
    assert runtime.autoretry is False and runtime.max_attempts == 1
    assert runtime.read_timeout == 30000 and runtime.connect_timeout == 10000
    provider.submit_doc_structure_job_with_options.side_effect = TimeoutError('lost')
    with pytest.raises(TimeoutError):
        client.submit_online_file('https://example.test/report.pdf')
    error = RuntimeError('denied')
    error.status_code = 403
    provider.submit_doc_structure_job_with_options.side_effect = error
    with pytest.raises(DocumentSubmissionRejectedError):
        client.submit_online_file('https://example.test/report.pdf')


def test_polling_deadline_returns_control_without_forgetting_task_id():
    provider = SimpleNamespace(
        get_doc_structure_result_with_options=Mock(return_value=SimpleNamespace(body=SimpleNamespace(completed=False)))
    )
    client = client_for(provider)
    with (
        patch('adapters.analysis.docmind.time.monotonic', side_effect=[0, 0, 179, 181]),
        patch('adapters.analysis.docmind.time.sleep'),
    ):
        with pytest.raises(TimeoutError):
            client.get_result('saved-task', poll_interval=1)
    request = provider.get_doc_structure_result_with_options.call_args.args[0]
    assert request.id == 'saved-task'
    assert provider.get_doc_structure_result_with_options.call_count == 1


def test_polling_keeps_complete_vendor_data():
    data = {'layouts': [{'text': 'first'}, {'text': 'second'}], 'extra': {'pages': 2}}
    client = client_for(
        SimpleNamespace(
            get_doc_structure_result_with_options=Mock(
                return_value=SimpleNamespace(body=SimpleNamespace(completed=True, status='success', data=data))
            )
        )
    )
    assert client.get_result('saved-task') == {'success': True, 'data': data, 'text': 'first\nsecond'}
