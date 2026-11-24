"""A new intake adopts repaired document revisions while old intake snapshots stay fixed."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from health_profile.report_ingestion import HealthReportProfileProcessor
from tests.unit.agents.graphs.record.test_creation_recovery import creation_manager
from tests.unit.agents.graphs.record.test_creation_recovery import drain
from tests.unit.agents.graphs.record.test_creation_recovery import fail_initial_runtime_once
from tests.unit.health_profile.test_report_ingestion import ReportIngestionFixture
from tests.unit.services.analysis.test_document_workflow import extended_flow
from tests.unit.services.analysis.test_report_occurrence_recovery import UpdatingRepository
from tests.unit.services.analysis.test_report_occurrence_recovery import bound_result

FILES = [{'url': 'https://example.test/versioned-report.pdf'}]


def versioned_flow():
    documents = extended_flow()
    old = bound_result(unit='umol/L', date=None)
    old['processing']['version'] = 'analysis-health-report-v6'
    documents.interpreter.VERSION = 'analysis-health-report-v6'
    documents.interpreter.analyze = AsyncMock(return_value=old)
    documents.repository = UpdatingRepository(ReportIngestionFixture.subject())
    documents.processor = HealthReportProfileProcessor(store=documents.reports, repository=documents.repository)
    creation = creation_manager(health_report_processing_service=documents.service)
    creation.manager._loadHealthProfileContext = AsyncMock(return_value=None)
    return documents, creation


def upgrade(documents):
    revised = bound_result(unit='umol/L', status='complete')
    revised['processing']['version'] = 'analysis-health-report-v7'
    # Physical source identity is independent of the interpretation algorithm version.
    assert revised['sourceBindings']['/observations/0'].startswith('v6:')
    documents.interpreter.VERSION = 'analysis-health-report-v7'
    documents.interpreter.analyze.return_value = revised


async def create(creation, session_id):
    await creation.manager.createSession('user-1', session_id, file_list=FILES)
    await drain(creation)
    return await creation.manager.getSession('user-1', session_id)


@pytest.mark.asyncio
async def test_v7_date_recovery_updates_new_session_and_profile_without_changing_published_session():
    documents, creation = versioned_flow()
    old_response = await create(creation, 'session-old')
    assert old_response['status'] == 'question'
    old_session = deepcopy(await creation.manager._session_cache.get('user-1', 'session-old'))
    old_report = old_session['parsedHealthReports'][0]
    assert old_report['reportTime'] is None
    canonical_id = old_response['documents'][0]['reportId']
    canonical_before = await documents.reports.get_report(canonical_id)
    initial_job = next(iter(documents.jobs.jobs.values()))
    await documents.processor.process(initial_job)
    assert not documents.repository.facts

    upgrade(documents)
    new_response = await create(creation, 'session-new')
    new_session = await creation.manager._session_cache.get('user-1', 'session-new')
    new_report = new_session['parsedHealthReports'][0]
    assert new_response['status'] == 'question' and new_report['reportTime'] == '2026-09-01'
    assert new_report['sourceRevision'] != old_report['sourceRevision']
    current_revision = await documents.reports.get_revision(canonical_id, new_report['sourceRevision'])
    assert current_revision.source_fingerprint == canonical_before.source_fingerprint
    assert current_revision.result['sourceBindings'] == canonical_before.result['sourceBindings']
    assert (await documents.reports.get_report(canonical_id)).result == canonical_before.result

    for job in documents.jobs.jobs.values():
        await documents.processor.process(job)
    assert len(documents.repository.facts) == len(documents.observations.collection.documents) == 1
    assert documents.repository.facts[0].effective_period.start.isoformat().startswith('2026-09-01')
    assert next(iter(documents.observations.collection.documents.values()))['dateStatus'] == 'known'

    assert (await create(creation, 'session-new'))['questionId'] == new_response['questionId']
    assert (await create(creation, 'session-old'))['questionId'] == old_response['questionId']
    replay = await documents.service.process_document(
        user_id='user-1', document_id=new_response['documents'][0]['documentId']
    )
    assert replay.source_revision == new_report['sourceRevision']
    for job in documents.jobs.jobs.values():
        await documents.processor.process(job)
    assert len(documents.repository.facts) == 1
    assert await creation.manager._session_cache.get('user-1', 'session-old') == old_session
    assert documents.interpreter.analyze.await_count == 2
    assert len(documents.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_exhausted_failed_session_remains_idempotent_but_new_session_can_recover_document():
    documents, creation = versioned_flow()
    for _ in range(3):
        fail_initial_runtime_once(creation)
        failed = await create(creation, 'session-old')
        assert failed['status'] == 'failed'
        creation.redis.now += 16
    assert failed['errorCode'] == 'CREATION_RETRY_EXHAUSTED' and failed['retryable'] is False
    old_receipt = deepcopy(await creation.manager._session_cache.get('user-1', 'session-old'))
    assert old_receipt['qaCreation']['attempts'] == 3
    assert old_receipt['qaCreation']['checkpoint']['files']['0']['report']['reportTime'] is None
    assert documents.interpreter.analyze.await_count == 1

    upgrade(documents)
    question_requests = len(creation.gateway.requests)
    assert (await create(creation, 'session-old'))['status'] == 'failed'
    assert len(creation.gateway.requests) == question_requests
    assert documents.interpreter.analyze.await_count == 1
    current = await create(creation, 'session-new')
    assert current['status'] == 'question'
    snapshot = await creation.manager._session_cache.get('user-1', 'session-new')
    assert snapshot['parsedHealthReports'][0]['reportTime'] == '2026-09-01'
    assert snapshot['parsedHealthReports'][0]['sourceRevision'] > 1
    assert await creation.manager._session_cache.get('user-1', 'session-old') == old_receipt
    assert documents.interpreter.analyze.await_count == 2
    assert len(documents.client.submit_calls) == 1
