"""Document quality rounds and immutable publications against the Mongo adapter."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from modules.analysis.application.documents import DocumentInterpretationError
from modules.analysis.application.documents import DocumentParsingError
from modules.analysis.application.service import HealthReportUpstreamError
from tests.unit.services.analysis.test_document_reuse import request
from tests.unit.services.analysis.test_document_workflow import extended_flow
from tests.unit.services.analysis.test_document_workflow import lab


def result(status, *, value='468', retryable=True):
    report = lab(value)
    report['processing'] = {
        'status': status,
        'issues': [] if status == 'complete' else [{'code': 'unresolved_source', 'sourceIds': ['t2r3']}],
        'retryable': retryable if status != 'complete' else False,
    }
    report['analysisEvidence'] = {'unpublished': 'only in the trace archive'}
    if status == 'unavailable':
        report.update(observations=[], aiSummary=None)
    return report


@pytest.mark.asyncio
async def test_partial_round_publishes_usable_result_and_recovery_returns_current_revision():
    flow = extended_flow()
    first_result, next_result = result('partial'), result('complete', value='470')
    flow.interpreter.analyze = AsyncMock(side_effect=[first_result, next_result])
    first = await flow.service.process_document(**request())
    assert first.report['processing']['status'] == 'partial'
    assert first.source_revision == first.report['sourceRevision'] == 1
    assert 'analysisEvidence' not in first.report
    original = await flow.reports.get_report(first.canonical_report_id)
    assert 'analysisEvidence' not in original.result
    archived = await flow.service.read_document_payload(first.document_id, user_id='user-1', name='interpretation')
    assert archived['analysisEvidence'] == first_result['analysisEvidence']

    recovered = await flow.service.process_document(user_id='user-1', document_id=first.document_id)
    assert recovered.report['observations'][0]['resultValue'] == '470'
    assert recovered.report['processing']['status'] == 'complete'
    assert recovered.source_revision == recovered.report['sourceRevision'] > 1
    assert 'ingestionObservations' not in recovered.report
    assert (await flow.reports.get_report(first.canonical_report_id)).result == original.result
    revision = await flow.reports.get_revision(first.canonical_report_id, recovered.source_revision)
    assert revision.result['observations'][0]['resultValue'] == '470'
    flow.interpreter.VERSION = 'future-version'
    again = await flow.service.process_document(user_id='user-1', document_id=first.document_id)
    assert again.report == recovered.report
    assert again.source_revision == recovered.source_revision
    assert flow.interpreter.analyze.await_count == 2
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_concurrent_partial_recovery_joins_one_round_and_does_not_loop_until_complete():
    flow = extended_flow()
    flow.interpreter.analyze = AsyncMock(return_value=result('partial'))
    first = await flow.service.process_document(**request())
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return result('partial')

    flow.interpreter.analyze = AsyncMock(side_effect=blocked)
    owner = asyncio.create_task(flow.service.process_document(user_id='user-1', document_id=first.document_id))
    await entered.wait()
    waiters = [
        asyncio.create_task(flow.service.process_document(user_id='user-1', document_id=first.document_id))
        for _ in range(8)
    ]
    await asyncio.sleep(0.02)
    release.set()
    outcomes = await asyncio.gather(owner, *waiters)
    assert flow.interpreter.analyze.await_count == 1
    assert all(item.report['processing']['status'] == 'partial' for item in outcomes)
    document = next(iter(flow.index_collection.documents.values()))
    assert document['status'] == 'completed' and document['interpretationRound'] == 2
    await flow.service.process_document(user_id='user-1', document_id=first.document_id)
    assert flow.interpreter.analyze.await_count == 2
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_unavailable_round_settles_without_publication_then_explicit_request_recovers():
    flow = extended_flow()
    flow.interpreter.analyze = AsyncMock(side_effect=[result('unavailable'), result('complete')])
    with pytest.raises(HealthReportUpstreamError) as caught:
        await flow.service.process_document(**request())
    error = caught.value.__cause__
    assert isinstance(error, DocumentInterpretationError)
    assert error.processing['status'] == 'unavailable'
    assert error.document_id and error.trace_id and error.source_revision is None
    assert error.automatic_retryable is False
    document = next(iter(flow.index_collection.documents.values()))
    assert document['status'] == 'completed' and document['processingStatus'] == 'unavailable'
    assert 'docmind_result' in document['payloadRefs']
    assert not flow.jobs.jobs
    recovered = await flow.service.process_document(user_id='user-1', document_id=error.document_id)
    assert recovered.report['processing']['status'] == 'complete'
    assert flow.interpreter.analyze.await_count == 2
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_nonretryable_partial_reuses_result_without_reopening_a_round():
    flow = extended_flow()
    flow.interpreter.analyze = AsyncMock(return_value=result('partial', retryable=False))
    first = await flow.service.process_document(**request())
    second = await flow.service.process_document(user_id='user-1', document_id=first.document_id)
    assert second.report == first.report
    assert flow.interpreter.analyze.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('quality', ['partial', 'unavailable'])
async def test_nonretryable_incomplete_content_keeps_its_settled_current_round(quality):
    flow = extended_flow()
    flow.interpreter.analyze = AsyncMock(side_effect=[result(quality, retryable=False), result('complete')])
    if quality == 'unavailable':
        with pytest.raises(HealthReportUpstreamError) as caught:
            await flow.service.process_document(**request())
        document_id = caught.value.__cause__.document_id
    else:
        first = await flow.service.process_document(**request())
        document_id = first.document_id
    before = deepcopy(flow.index_collection.documents[document_id])
    if quality == 'unavailable':
        with pytest.raises(HealthReportUpstreamError) as caught:
            await flow.service.process_document(user_id='user-1', document_id=document_id)
        assert caught.value.__cause__.processing['status'] == quality
        assert caught.value.__cause__.processing['retryable'] is False
        assert not flow.jobs.jobs
    else:
        repeated = await flow.service.process_document(user_id='user-1', document_id=document_id)
        assert repeated.report == first.report
        assert repeated.source_revision == first.source_revision
    after = flow.index_collection.documents[document_id]
    assert after['interpretationRound'] == before['interpretationRound']
    assert after['payloadRefs'] == before['payloadRefs']
    assert flow.interpreter.analyze.await_count == 1
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_revision_storage_failure_retries_publication_without_repeating_interpretation():
    flow = extended_flow()
    flow.interpreter.analyze = AsyncMock(side_effect=[result('partial'), result('complete', value='470')])
    first = await flow.service.process_document(**request())
    flow.reports._revisions.fail_insert = True
    with pytest.raises(RuntimeError, match='storage unavailable'):
        await flow.service.process_document(user_id='user-1', document_id=first.document_id)
    flow.reports._revisions.fail_insert = False
    recovered = await flow.service.process_document(user_id='user-1', document_id=first.document_id)
    assert recovered.report['processing']['status'] == 'complete'
    assert recovered.source_revision > 1
    assert flow.interpreter.analyze.await_count == 2
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_legacy_completed_snapshot_is_unchanged_even_after_version_upgrade():
    flow = extended_flow()
    old = lab()
    flow.interpreter.analyze = AsyncMock(return_value=deepcopy(old))
    first = await flow.service.process_document(**request())
    flow.interpreter.VERSION = 'future-version'
    second = await flow.service.process_document(user_id='user-1', document_id=first.document_id)
    assert second.report == first.report
    assert 'processing' not in second.report and 'sourceRevision' not in second.report
    assert flow.interpreter.analyze.await_count == 1


@pytest.mark.asyncio
async def test_extension_cache_tracks_the_published_document_revision():
    flow = extended_flow()
    flow.interpreter.analyze = AsyncMock(side_effect=[result('partial'), result('complete', value='470')])
    first = await flow.service.process_document(**request(output_profile='report_source'))
    second = await flow.service.process_document(
        user_id='user-1', document_id=first.document_id, output_profile='report_source'
    )
    assert first.extraction_id != second.extraction_id
    assert flow.extractor.await_count == 2
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_partial_report_extension_is_archived_but_cannot_publish_quarantined_candidates():
    flow = extended_flow()
    flow.interpreter.analyze = AsyncMock(return_value=result('partial', retryable=False))
    first = await flow.service.process_document(**request(output_profile='report_source'))
    second = await flow.service.process_document(
        user_id='user-1', document_id=first.document_id, output_profile='report_source'
    )
    assert first.structured_data['processing']['status'] == 'partial'
    assert first.source_revision is second.source_revision is None
    assert first.ingestion_status == second.ingestion_status == 'not_submitted'
    assert first.ingestion_job_id is second.ingestion_job_id is None
    assert flow.extractor.await_count == 1 and second.output_reused
    assert len(flow.jobs.jobs) == len(flow.observations.collection.documents) == 1
    assert not flow.reports._revisions.documents
    assert any(name == 'output_result' for _, name in flow.traces.payloads)


@pytest.mark.asyncio
async def test_hint_partial_is_quarantined_and_new_interpreter_version_can_recover_it():
    flow = extended_flow()
    flow.interpreter.analyze = AsyncMock(
        side_effect=[result('complete'), result('partial', retryable=False), result('complete')]
    )
    first = await flow.service.process_document(**request(output_profile='report_source', report_type_hint='lab'))
    assert first.report['processing']['status'] == 'complete'
    assert first.structured_data['processing']['status'] == 'partial'
    assert first.source_revision is None and first.ingestion_status == 'not_submitted'
    assert not flow.reports._revisions.documents
    flow.interpreter.VERSION = 'upgraded-hint-validator'
    recovered = await flow.service.process_document(
        user_id='user-1', document_id=first.document_id, output_profile='report_source', report_type_hint='lab'
    )
    assert recovered.source_revision > 1 and recovered.ingestion_status == 'queued'
    assert flow.interpreter.analyze.await_count == 3
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_reading_settled_partial_never_resumes_even_when_validator_has_upgraded():
    flow = extended_flow()
    flow.interpreter.analyze = AsyncMock(return_value=result('partial'))
    first = await flow.service.process_document(**request())
    flow.interpreter.VERSION = 'new-version'
    read = await flow.service.process_document(user_id='user-1', document_id=first.document_id, resume_incomplete=False)
    assert read.report == first.report and flow.interpreter.analyze.await_count == 1


@pytest.mark.asyncio
async def test_same_content_across_urls_recovers_at_most_once_in_one_file_group():
    flow = extended_flow()
    flow.interpreter.analyze = AsyncMock(return_value=result('partial'))
    outcome = await flow.service.process_documents(
        [{'url': 'https://example.test/a.pdf'}, {'url': 'https://example.test/b.pdf'}], user_id='user-1'
    )
    assert flow.interpreter.analyze.await_count == len(flow.client.submit_calls) == 1
    assert len(outcome.documents) == 2
    assert outcome.documents[0]['documentId'] == outcome.documents[1]['documentId']


@pytest.mark.asyncio
async def test_permanent_configuration_error_remains_operational_for_waiters_and_replays():
    flow = extended_flow()
    entered, release = asyncio.Event(), asyncio.Event()

    async def unavailable_configuration(*args, **kwargs):
        entered.set()
        await release.wait()
        error = DocumentParsingError('provider credentials rejected', retryable=False)
        error.content_failure = False
        raise error

    flow.interpreter.analyze = AsyncMock(side_effect=unavailable_configuration)
    owner = asyncio.create_task(flow.service.process_document(**request()))
    await entered.wait()
    document = next(iter(flow.index_collection.documents.values()))
    waiter = asyncio.create_task(flow.service.process_document(user_id='user-1', document_id=document['_id']))
    await asyncio.sleep(0.02)
    release.set()
    for error in await asyncio.gather(owner, waiter, return_exceptions=True):
        assert isinstance(error, HealthReportUpstreamError)
        assert error.__cause__.content_failure is False
    with pytest.raises(HealthReportUpstreamError) as caught:
        await flow.service.process_document(user_id='user-1', document_id=document['_id'])
    assert caught.value.__cause__.content_failure is False
    assert flow.interpreter.analyze.await_count == 1


@pytest.mark.asyncio
async def test_stale_waiter_cannot_reclaim_a_round_that_just_failed():
    flow = extended_flow()
    flow.interpreter.analyze = AsyncMock(return_value=result('complete'))
    await flow.service.process_document(**request())
    document = next(iter(flow.index_collection.documents.values()))
    document.update(status='running', leaseToken='owner')
    stale = deepcopy(document)
    document.update(status='failed', leaseToken=None, leaseExpiresAt=None, retryable=False, contentFailure=False)
    now = flow.coordinator.now()
    assert await flow.index.claim(stale, 'waiter', now, now + timedelta(seconds=120)) is None
