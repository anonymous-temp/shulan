"""Cross-entrypoint content reuse against the production coordinator and Mongo adapter."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from adapters.analysis.document_index import MongoDocumentIndexStore
from adapters.record.documents import LegacyRecordDocumentAdapter
from modules.analysis.application.document_reuse import DocumentReuseCoordinator
from modules.analysis.application.documents import DocumentBusyError
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.application.documents import DocumentSubmissionConflictError
from modules.analysis.application.service import HealthReportUpstreamError
from modules.record.application import QASessionManager
from tests.unit._support.document_profile_composition.analysis.document_reuse import (
    AtomicCollection as AtomicCollection,
)
from tests.unit._support.document_profile_composition.analysis.document_reuse import reuse_flow as reuse_flow
from tests.unit.services.analysis.test_document_provenance import arguments
from tests.unit.services.analysis.test_document_provenance import build_flow


def request(**changes):
    return arguments(report_id=None, **changes)


@pytest.mark.asyncio
async def test_three_entrypoints_use_one_content_one_source_and_individual_traces():
    flow = reuse_flow()
    outcomes = []
    for name in ('analysis', 'record', 'report'):
        outcomes.append(
            await flow.service.process_document(
                **request(
                    file_id=f'https://example.test/{name}.pdf',
                    source_type=name,
                    session_id=f'session-{name}',
                )
            )
        )
    assert len({item.document_id for item in outcomes}) == 1
    assert len({item.trace_id for item in outcomes}) == 3
    assert [item.reused for item in outcomes] == [False, True, True]
    assert len(flow.client.submit_calls) == len(flow.graph.calls) == len(flow.jobs.jobs) == 1
    assert flow.downloader.download.await_count == 3
    assert len(flow.index_collection.documents) == 1
    assert sum(name == 'original_file' for _, name in flow.traces.payloads) == 1


@pytest.mark.asyncio
async def test_reference_skips_download_and_keeps_archives_accessible_after_url_expires():
    flow = reuse_flow()
    first = await flow.service.process_document(**request())
    flow.downloader.download.side_effect = AssertionError('must not download')
    second = await flow.service.process_document(user_id='user-1', document_id=first.document_id, source_type='report')
    assert second.text == first.text
    assert second.report == first.report
    assert second.reused
    assert await flow.service.read_document_payload(first.document_id, user_id='user-1') == {
        'content': b'%PDF-1.7\nsynthetic',
    }


@pytest.mark.asyncio
async def test_replacement_at_same_url_is_a_new_document():
    flow = reuse_flow()
    first = await flow.service.process_document(**request())
    flow.downloader.download.return_value += b'new content'
    second = await flow.service.process_document(**request())
    assert first.document_id != second.document_id
    assert len(flow.client.submit_calls) == len(flow.jobs.jobs) == 2


@pytest.mark.asyncio
async def test_alias_report_ids_share_one_profile_source_and_pending_compensation():
    flow = reuse_flow()
    flow.jobs.fail_create = True
    first = await flow.service.process_document(**arguments(report_id='caller-1'))
    second = await flow.service.process_document(**arguments(report_id='caller-2'))
    assert first.ingestion_status == second.ingestion_status == 'pending'
    assert first.report['reportId'] == 'caller-1'
    assert second.report['reportId'] == 'caller-2'
    flow.jobs.fail_create = False
    await flow.source.recover_pending()
    alias = await flow.reports.get_report('caller-2')
    assert alias.canonical_report_id == (await flow.reports.get_report('caller-1')).canonical_report_id
    assert alias.ingestion_status == 'queued'
    assert len(flow.jobs.jobs) == 1
    assert next(iter(flow.jobs.jobs.values())).source_id == alias.canonical_report_id
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_independent_service_instances_share_database_execution_not_a_local_lock():
    flow = reuse_flow()
    other = build_flow()
    other.service._document_trace_store = flow.traces
    other.service._report_source = flow.source
    other.service._document_reuse = DocumentReuseCoordinator(
        other.service,
        MongoDocumentIndexStore(flow.index_collection),
        flow.downloader,
    )
    other.service._document_reuse.POLL_SECONDS = 0.005
    outcomes = await asyncio.gather(
        *(
            (flow if n % 2 else other).service.process_document(
                **request(source_type=('analysis', 'record', 'report')[n % 3])
            )
            for n in range(12)
        )
    )
    assert len({item.document_id for item in outcomes}) == 1
    assert len(flow.client.submit_calls) + len(other.client.submit_calls) == 1
    assert len(flow.graph.calls) + len(other.graph.calls) == 1
    assert len(flow.jobs.jobs) == 1
    assert len(flow.traces.documents) == 12


@pytest.mark.asyncio
async def test_model_failure_restarts_only_interpretation_and_result_save_failure_restarts_neither():
    flow = reuse_flow()
    original_answer = flow.graph.content
    flow.graph.content = 'invalid JSON'
    with pytest.raises(HealthReportUpstreamError):
        await flow.service.process_document(**request())
    calls_after_failure = len(flow.graph.calls)
    flow.graph.content = original_answer
    original_save = flow.source.save
    flow.source.save = AsyncMock(side_effect=RuntimeError('save unavailable'))
    with pytest.raises(RuntimeError):
        await flow.service.process_document(**request())
    flow.source.save = original_save
    outcome = await flow.service.process_document(**request())
    assert outcome.text
    assert len(flow.client.submit_calls) == 1
    assert len(flow.graph.calls) == calls_after_failure + 1
    assert len(flow.jobs.jobs) == 1


@pytest.mark.asyncio
async def test_known_remote_task_resumes_polling_after_failure():
    flow = reuse_flow()
    original_poll = flow.client.get_result
    flow.client.get_result = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('connection lost'))
    with pytest.raises(HealthReportUpstreamError):
        await flow.service.process_document(**request())
    flow.client.get_result = original_poll
    await flow.service.process_document(**request())
    assert len(flow.client.submit_calls) == 1
    assert len(flow.jobs.jobs) == 1


@pytest.mark.asyncio
async def test_uncertain_submission_waits_for_recovery_cooldown():
    flow = reuse_flow()

    def uncertain(*a, **k):
        flow.client.submit_calls.append('attempt')
        raise RuntimeError('no response')

    flow.client.submit_local_file = uncertain
    with patch('modules.analysis.application.document_reuse.logger') as project_logger:
        with pytest.raises(HealthReportUpstreamError, match='文档解析任务提交失败'):
            await flow.service.process_document(**request())
        with pytest.raises(DocumentSubmissionConflictError, match='待核查'):
            await flow.service.process_document(**request())
    assert flow.client.submit_calls == ['attempt']
    document = next(iter(flow.index_collection.documents.values()))
    assert document['status'] == 'submission_unknown'
    project_logger.error.assert_called_once()
    project_logger.warning.assert_called_once()
    calls = [project_logger.error.call_args, project_logger.warning.call_args]
    messages = [call.args[0] % call.args[1:] for call in calls]
    assert len(messages) == 2
    assert '文档处理失败' in messages[0]
    assert 'root_message=no response' in messages[0]
    assert 'stage=submitting' in messages[0]
    assert '文档提交状态待核查' in messages[1]
    assert 'action=wait_recovery' in messages[1]
    for call, message in zip(calls, messages):
        assert f'document_id={document["_id"]}' in message
        assert 'trace_id=document_' in message
        assert 'status=submission_unknown' in message
        assert 'task_id=-' in message
        assert 'exc_info' not in call.kwargs
        assert '\n' not in message


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'user_id': 'other'}, {'subject_id': 'other'}])
async def test_references_cannot_cross_owners_and_equal_bytes_are_not_shared_across_owners(change):
    flow = reuse_flow()
    first = await flow.service.process_document(**request())
    with pytest.raises(DocumentNotFoundError):
        await flow.service.process_document(**{'user_id': 'user-1', 'document_id': first.document_id, **change})
    second = await flow.service.process_document(**request(**change))
    assert first.document_id != second.document_id
    assert len(flow.jobs.jobs) == 2


@pytest.mark.asyncio
async def test_cancellation_of_waiter_does_not_cancel_the_owner_or_cause_resubmission():
    flow = reuse_flow()
    entered, release = asyncio.Event(), asyncio.Event()
    analyze = flow.interpreter.analyze

    async def blocked(*a, **k):
        entered.set()
        await release.wait()
        return await analyze(*a, **k)

    flow.interpreter.analyze = blocked
    waiter = asyncio.create_task(flow.service.process_document(**request()))
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert len(flow.coordinator.tasks) == 1
    release.set()
    second = await flow.service.process_document(**request())
    assert second.text
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_wait_timeout_does_not_create_another_remote_task():
    flow = reuse_flow()
    flow.coordinator.WAIT_SECONDS = 0.01
    entered, release = asyncio.Event(), asyncio.Event()
    analyze = flow.interpreter.analyze

    async def blocked(*a, **k):
        entered.set()
        await release.wait()
        return await analyze(*a, **k)

    flow.interpreter.analyze = blocked
    with pytest.raises(DocumentBusyError):
        await flow.service.process_document(**request())
    await asyncio.wait_for(entered.wait(), 5)
    release.set()
    await asyncio.gather(*flow.coordinator.tasks)
    await flow.service.process_document(**request())
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_stale_lease_cannot_publish_and_expired_lease_can_be_claimed():
    flow = reuse_flow()
    await flow.service.process_document(**request())
    item = next(iter(flow.index_collection.documents.values()))
    now = flow.coordinator.now()
    item.update(status='running', leaseToken='old', leaseExpiresAt=now - timedelta(seconds=1))
    with pytest.raises(DocumentBusyError):
        await flow.index.update_owned(item, 'old', now, {'status': 'completed'})
    new = await flow.index.claim(item, 'new', now, now + timedelta(seconds=120))
    assert new['leaseToken'] == 'new'
    with pytest.raises(DocumentBusyError):
        await flow.index.update_owned(item, 'old', now, {'status': 'completed'})
    assert await flow.index.claim(item, 'third', now, now + timedelta(seconds=120)) is None


@pytest.mark.asyncio
async def test_record_collapses_business_inputs_but_retains_all_usage_references():
    flow = reuse_flow()
    manager = QASessionManager.__new__(QASessionManager)
    manager._record_document_port = LegacyRecordDocumentAdapter(processor=flow.service)
    documents = []
    reports = await manager._processHealthReports(
        user_id='user-1',
        session_id='qa-1',
        documents=documents,
        file_list=[{'url': f'https://example.test/{n}.pdf', 'fileType': 'pdf'} for n in range(2)],
    )
    assert len(reports) == 1 and len(documents) == 2
    assert [item['fileIndex'] for item in documents] == [0, 1]
    assert 'documentId' not in reports[0]
    await manager._processHealthReports(
        user_id='user-1',
        session_id='qa-2',
        file_list=[{'documentId': documents[0]['documentId']}],
    )
    assert flow.downloader.download.await_count == 2
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'changes',
    [
        {'document_id': 'document-test'},
        {'file_id': None},
        {'content': b''},
        {'file_id': None, 'document_id': 'document-test', 'file_type': 'pdf'},
    ],
)
async def test_invalid_sources_fail_without_downloading(changes):
    flow = reuse_flow()
    with pytest.raises(ValueError):
        await flow.service.process_document(**request(**changes))
    flow.downloader.download.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_record_create_query_and_completion_keep_the_file_snapshot(streaming):
    import json

    from tests.unit.agents.graphs.record.test_dynamic_record import PatientGateway
    from tests.unit.agents.graphs.record.test_dynamic_record import assessment
    from tests.unit.agents.graphs.record.test_record_agent import _build_runtime
    from tests.unit.agents.graphs.record.test_record_agent import _load_record_service

    flow = reuse_flow()
    module = _load_record_service()
    gateway = PatientGateway()
    manager = module.QASessionManager(
        _build_runtime(gateway),
        health_report_processing_service=flow.service,
    )
    response = await manager.createSession('user-1', 'qa-snapshot', file_list=[{'url': 'https://example.test/a.pdf'}])
    stored = json.loads(module.redis_client.last_value)
    assert response['documents'] == stored['documents']
    assert len(stored['parsedHealthReports']) == 1
    module.redis_client.value = json.dumps(stored)
    assert (await manager.getSession('user-1', 'qa-snapshot'))['documents'] == stored['documents']
    assert (await manager.getHistory('user-1', 'qa-snapshot'))['documents'] == stored['documents']
    assert (await manager.createSession('user-1', 'qa-snapshot'))['documents'] == stored['documents']

    async def answer(current, text):
        args = ('user-1', 'qa-snapshot', current['questionId'], text)
        if streaming:
            events = [event async for event in manager.submitAnswerStream(*args, thinking=False)]
            assert events[-1]['data']['type'] == 'finish'
            result = next(event['data'] for event in events if 'status' in event['data'])
        else:
            result = await manager.submitAnswer(*args, thinking=False)
        assert result['documents'] == stored['documents']
        return result

    current = response
    for text in ('餐后腹胀', '没有已知疾病', '没有已知血亲家族病史'):
        current = await answer(current, text)
    for _ in range(12):
        gateway.next_assessment = assessment(sufficient=True, gaps=[])
        current = await answer(current, current['options'][0])
    assert current['phase'] == 'supplement'
    assert (current['intakeQuestionCount'], current['qaRoundCount']) == (15, 0)
    gateway.next_assessment = assessment(intent='no_more', substantive=False)
    final = await answer(current, '没有了')
    assert final['status'] == 'finished'
    assert final['assistantMessage']
    assert final['interactionCount'] == 15
    assert (final['intakeQuestionCount'], final['qaRoundCount']) == (15, 0)
    assert (await manager.completeSession('user-1', 'qa-snapshot', round_number=1))['documents'] == stored['documents']
    assert (await manager.getSession('user-1', 'qa-snapshot'))['documents'] == stored['documents']
    assert (await manager.getHistory('user-1', 'qa-snapshot'))['documents'] == stored['documents']
    assert module.collection_sessions.document['documents'] == stored['documents']
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_lease_heartbeat_prevents_another_instance_from_stealing_active_work():
    flow = reuse_flow()
    flow.coordinator.LEASE_SECONDS = 0.12
    flow.coordinator.HEARTBEAT_SECONDS = 0.02
    entered, release = asyncio.Event(), asyncio.Event()
    analyze = flow.interpreter.analyze

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return await analyze(*args, **kwargs)

    flow.interpreter.analyze = blocked
    waiter = asyncio.create_task(flow.service.process_document(**request()))
    await entered.wait()
    await asyncio.sleep(0.15)
    document = next(iter(flow.index_collection.documents.values()))
    now = flow.coordinator.now()
    stolen = await flow.index.claim(document, 'thief', now, now + timedelta(seconds=5))
    release.set()
    await waiter
    assert stolen is None
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_incomplete_archive_is_not_a_successful_reusable_document():
    flow = reuse_flow()
    write = flow.traces.write_payload

    async def broken(trace, user, name, payload):
        if name == 'docmind_result':
            raise RuntimeError('chunk write failed')
        await write(trace, user, name, payload)

    flow.traces.write_payload = broken
    with pytest.raises(RuntimeError):
        await flow.service.process_document(**request())
    document = next(iter(flow.index_collection.documents.values()))
    assert document['status'] == 'failed'
    assert 'docmind_result' not in document['payloadRefs']
    assert not flow.jobs.jobs
    flow.traces.write_payload = write
    await flow.service.process_document(**request())
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_competing_caller_ids_do_not_poison_either_file_identity():
    from health_profile import HealthReportSourceConflictError

    flow = reuse_flow()
    flow.downloader.download.side_effect = lambda url: b'%PDF-1.7\n' + url.encode()
    urls = ['https://example.test/one.pdf', 'https://example.test/two.pdf']
    results = await asyncio.gather(
        *(flow.service.process_document(**arguments(file_id=url, report_id='competing-caller-id')) for url in urls),
        return_exceptions=True,
    )
    assert sum(isinstance(result, HealthReportSourceConflictError) for result in results) == 1
    recovered = [await flow.service.process_document(**request(file_id=url)) for url in urls]
    assert len({item.document_id for item in recovered}) == 2
    assert len(flow.client.submit_calls) == 2


@pytest.mark.asyncio
async def test_rejected_task_admission_releases_the_unused_execution_lease():
    flow = reuse_flow()

    def reject(coroutine):
        raise RuntimeError('task admission closed')

    flow.coordinator.task_spawner = reject
    with pytest.raises(RuntimeError, match='task admission'):
        await flow.service.process_document(**request())
    document = next(iter(flow.index_collection.documents.values()))
    assert document['leaseToken'] is None
    assert not flow.client.submit_calls
    flow.coordinator.task_spawner = asyncio.create_task
    await flow.service.process_document(**request())
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [{'user_id': {'$ne': ''}}, {'subject_id': {'$ne': ''}}])
async def test_archive_accessor_rejects_non_string_owner_filters(changes):
    flow = reuse_flow()
    first = await flow.service.process_document(**request())
    with pytest.raises(ValueError):
        await flow.service.read_document_payload(first.document_id, **{'user_id': 'user-1', **changes})
