"""Actual Analysis entry with durable Document and Profile providers, using offline BSON stores."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.analysis_formal import create_analysis_formal_processing_service
from modules.analysis.analysis_formal_contracts import AnalysisFormalCommand
from tests.unit.services.analysis.test_document_formal_source import apply
from tests.unit.services.analysis.test_document_formal_source import flow
from tests.unit.services.analysis.test_document_formal_source_store import Collection


async def setup():
    current = await flow()
    current.analysis_records = Collection()
    current.analysis_records.with_options = lambda **kwargs: current.analysis_records
    current.analysis = build(current)
    return current


def build(current):
    return create_analysis_formal_processing_service(
        processing=current.env.flow.service,
        compatibility=current.env.api,
        formal=current.formal,
        managed=current.runtime,
        profile_archive=current.archive,
        command_records=current.analysis_records,
        clock=lambda: current.profile.now[0].timestamp(),
    )


async def process(current, alias='analysis-alias', **kwargs):
    return await current.analysis.service.process(
        user_id=current.scope.user_id,
        subject_id=current.scope.subject_id,
        document_id=current.item.preparation_receipt.document_id,
        report_id=alias,
        **kwargs,
    )


async def recover(current, count=4):
    for _ in range(count):
        current.profile.now[0] += timedelta(seconds=31)
        await current.formal.recover_pending()
        await current.analysis.delivery.recover_pending()


@pytest.mark.asyncio
async def test_real_analysis_managed_base_and_alias_keep_public_projection_without_legacy_queue():
    current = await setup()
    result = await process(current)
    assert (
        result['reportId'] == 'analysis-alias' and result['documentId'] == current.item.preparation_receipt.document_id
    )
    assert result['userId'] == current.scope.user_id and result['processing']['status'] == 'complete'
    assert 'sourceRevision' not in result
    assert not current.env.flow.jobs.jobs
    assert not current.formal_records.documents
    await recover(current)
    alias = await current.env.flow.source.get_owned('analysis-alias', current.scope.user_id, current.scope.subject_id)
    assert alias is not None and alias.ingestion_status == 'source_managed'
    assert alias.canonical_report_id == current.bundle.material.source_id
    assert len(current.env.flow.graph.calls) == len(current.env.flow.client.submit_calls) == 1
    assert list(current.analysis_records.documents.values())[0]['status'] == 'completed'
    await apply(current)
    again = await process(current)
    assert again == result
    assert not current.env.flow.jobs.jobs


@pytest.mark.asyncio
async def test_legacy_analysis_keeps_real_alias_and_original_base_queue():
    current = await setup()
    await current.env.api.publish_prepared(
        scope=current.scope,
        understanding_snapshot_id=current.item.understanding_snapshot_ref,
        selection=current.item.selection,
        preparation_receipt=current.item.preparation_receipt,
    )
    result = await process(current)
    assert result['reportId'] == 'analysis-alias' and 'sourceRevision' not in result
    await recover(current)
    assert len(current.env.flow.jobs.jobs) == 1
    assert list(current.analysis_records.documents.values())[0]['status'] == 'completed'


@pytest.mark.asyncio
async def test_profile_outage_preserves_business_content_and_recovers_fixed_base_and_alias_without_query():
    current = await setup()
    old = current.fresh.reserve_fresh_source
    current.fresh.reserve_fresh_source = AsyncMock(side_effect=RuntimeError('offline'))
    result = await process(current)
    assert result['processing']['status'] == 'complete' and 'sourceRevision' not in result
    await recover(current, count=2)
    row = list(current.analysis_records.documents.values())[0]
    assert row['status'] == 'pending'
    original_command = row['command']
    current.fresh.reserve_fresh_source = old
    current.analysis = build(current)
    await recover(current)
    row = await current.analysis.delivery.store.get(row['_id'])
    assert row['status'] == 'completed' and row['command'] == original_command
    assert not current.env.flow.jobs.jobs and len(current.env.flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_alias_ack_lost_after_real_archive_replays_first_snapshot_without_republication():
    current = await setup()
    original = current.archive.archive_source_material
    calls = []

    async def unknown(**kwargs):
        result = await original(**kwargs)
        if kwargs['report_id'] == 'analysis-alias':
            calls.append(kwargs)
            if len(calls) == 1:
                raise TimeoutError('alias receipt lost')
        return result

    current.archive.archive_source_material = unknown
    result = await process(current)
    assert result['reportId'] == 'analysis-alias'
    await recover(current, count=2)
    row = list(current.analysis_records.documents.values())[0]
    assert row['status'] == 'pending'
    current.analysis = build(current)
    await recover(current)
    saved = await current.analysis.delivery.store.get(row['_id'])
    assert saved['status'] == 'completed' and saved['progress']['alias']['kind'] == 'existing_alias'
    assert len(calls) == 1 and len(current.env.ledger.intents.documents) == 1


@pytest.mark.asyncio
async def test_delivery_receipt_is_pure_and_unknown_scope_cannot_recover():
    from modules.analysis.snapshots import DocumentScope

    current = await setup()
    await process(current)
    await recover(current)
    row = list(current.analysis_records.documents.values())[0]
    command = AnalysisFormalCommand.from_dict(row['command'])
    current.analysis.delivery.steps.step = AsyncMock(side_effect=AssertionError('pure read'))
    receipt = await current.analysis.delivery.read_receipt(scope=current.scope, command_id=command.command_id)
    assert receipt['status'] == 'completed'
    assert await current.analysis.delivery.recover_pending() == 0
    with pytest.raises(PermissionError):
        await current.analysis.delivery.read_receipt(
            scope=DocumentScope('other', current.scope.subject_id), command_id=command.command_id
        )


@pytest.mark.asyncio
async def test_historical_noncanonical_report_alias_keeps_first_snapshot_without_new_source():
    current = await setup()
    source = current.env.flow.source
    raw = current.bundle.understanding.payload['interpretation']
    saved = await source.save(
        report_id='historical',
        user_id=current.scope.user_id,
        subject_id=current.scope.subject_id,
        source_fingerprint=source.source_fingerprint(
            user_id=current.scope.user_id,
            subject_id=current.scope.subject_id,
            file_id='https://example.test/original.pdf',
        ),
        result=raw,
    )
    previous_calls = len(current.env.flow.graph.calls)
    result = await current.analysis.service.process(
        file_id='https://example.test/original.pdf',
        user_id=current.scope.user_id,
        subject_id=current.scope.subject_id,
        report_id='historical',
    )
    assert result['reportId'] == 'historical'
    assert result['observations'] == saved.record.public_result()['observations']
    assert not current.analysis_records.documents and not current.formal_records.documents
    assert len(current.env.flow.graph.calls) == previous_calls


@pytest.mark.asyncio
async def test_existing_alias_on_another_document_is_rejected_before_formal_publication():
    from health_profile.report_source import HealthReportSourceConflictError

    current = await setup()
    await process(current)
    await recover(current)
    count = len(current.env.ledger.intents.documents)
    with pytest.raises(HealthReportSourceConflictError):
        await current.analysis.service.process(
            user_id=current.scope.user_id,
            subject_id=current.scope.subject_id,
            content=b'%PDF-1.7 other content for same alias',
            file_name='other.pdf',
            report_id='analysis-alias',
        )
    assert len(current.env.ledger.intents.documents) == count


@pytest.mark.asyncio
async def test_empty_ocr_keeps_existing_content_response_and_alias_reservation_without_publication():
    from modules.analysis.application.content_results import empty_content_result
    from modules.analysis.application.documents import DocumentInterpretationError

    current = await setup()
    raw = empty_content_result()

    async def empty_ocr(*, trace, **kwargs):
        await trace.write_payload('docmind_result', {'success': True, 'text': ''})
        raise DocumentInterpretationError('no text', result=raw, content_outcome='no_readable_text')

    current.env.flow.interpreter.extract_document = empty_ocr
    result = await current.analysis.service.process(
        user_id=current.scope.user_id,
        subject_id=current.scope.subject_id,
        content=b'%PDF-1.7 empty analysis',
        file_name='empty.pdf',
        report_id='empty-alias',
    )
    assert result['reportId'] == 'empty-alias' and result['processing']['status'] == 'unavailable'
    assert 'sourceRevision' not in result and 'sourceRevision' not in result['processing']
    row = next(row for row in current.analysis_records.documents.values() if 'command' in row)
    command = AnalysisFormalCommand.from_dict(row['command'])
    ready = await current.analysis.delivery.store.get(command.command_id + ':ready')
    assert ready['status'] == 'completed'
    archived = await current.env.flow.service._document_trace_store.read_payload(
        command.trace_id, current.scope.user_id, current.scope.subject_id, 'analysis_response'
    )
    assert archived == result
    await recover(current)
    assert list(current.analysis_records.documents.values())[0]['status'] == 'not_published'
    assert not current.formal_records.documents


@pytest.mark.asyncio
async def test_technical_document_failure_keeps_original_error_chain_and_no_publication():
    from modules.analysis.application.documents import DocumentParsingError
    from modules.analysis.application.service import HealthReportUpstreamError

    current = await setup()
    current.env.flow.interpreter.extract_document = AsyncMock(side_effect=DocumentParsingError('parser failed'))
    with pytest.raises(HealthReportUpstreamError) as error:
        await current.analysis.service.process(
            user_id=current.scope.user_id,
            subject_id=current.scope.subject_id,
            content=b'%PDF-1.7 technical failure',
            file_name='failed.pdf',
            report_id='failed-alias',
        )
    assert isinstance(error.value.__cause__, DocumentParsingError)
    await recover(current)
    assert list(current.analysis_records.documents.values())[0]['status'] == 'pending'
    current.profile.now[0] += timedelta(days=2)
    await recover(current)
    assert list(current.analysis_records.documents.values())[0]['errorCode'] == 'formal_deadline_exhausted'
    assert not current.formal_records.documents


@pytest.mark.asyncio
async def test_business_replay_does_not_overwrite_first_archived_alias_content_after_base_correction():
    from tests.unit.services.analysis.test_document_complete_source import change_interpretation

    current = await setup()
    await process(current)
    await recover(current)
    await apply(current)
    original = await current.env.flow.source.get_owned(
        'analysis-alias', current.scope.user_id, current.scope.subject_id
    )
    await change_interpretation(current.env, current.bundle, scope=current.scope, value='477')
    result = await process(current)
    assert 'sourceRevision' not in result
    alias = await current.env.flow.source.get_owned('analysis-alias', current.scope.user_id, current.scope.subject_id)
    assert alias.result == original.result and alias.source_revision == original.source_revision
    assert len(current.env.flow.jobs.jobs) == 0


@pytest.mark.asyncio
async def test_invalid_strategy_rejects_before_model_or_document_work():
    from types import SimpleNamespace

    current = await setup()
    current.env.flow.service._document_reuse.index.get = AsyncMock(side_effect=AssertionError('no read'))
    with pytest.raises(ValueError, match='exact Analysis'):
        await current.env.flow.service.process(
            user_id=current.scope.user_id,
            subject_id=current.scope.subject_id,
            document_id=current.item.preparation_receipt.document_id,
            _analysis_publication=SimpleNamespace(revision='unknown'),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize('interruption', ['cancel', 'timeout'])
async def test_detached_document_finishes_formal_base_and_alias_without_new_http_request(interruption):
    import asyncio

    from modules.analysis.application.documents import DocumentBusyError

    current = await setup()
    interpreter = current.env.flow.interpreter
    original = interpreter.analyze
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    interpreter.analyze = blocked
    reuse = current.env.flow.service._document_reuse
    if interruption == 'timeout':
        reuse.WAIT_SECONDS = 0.02
    task = asyncio.create_task(
        current.analysis.service.process(
            user_id=current.scope.user_id,
            subject_id=current.scope.subject_id,
            content=b'%PDF-1.7 detached Analysis request',
            file_name='detached.pdf',
            report_id='detached-alias',
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    assert len(current.analysis_records.documents) == 1
    assert not current.formal_records.documents
    if interruption == 'cancel':
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(DocumentBusyError):
            await task
    release.set()
    await asyncio.gather(*list(reuse.tasks))
    current.analysis = build(current)
    await recover(current)
    row = list(current.analysis_records.documents.values())[0]
    assert row['status'] == 'completed'
    alias = await current.env.flow.source.get_owned('detached-alias', current.scope.user_id, current.scope.subject_id)
    assert alias is not None and alias.ingestion_status == 'source_managed'
    assert len(current.env.flow.graph.calls) == len(current.env.flow.client.submit_calls) == 2
    assert not current.env.flow.jobs.jobs


@pytest.mark.asyncio
async def test_background_preparation_rejects_later_round_instead_of_publishing_new_input():
    from modules.analysis.analysis_formal_preparation import capture_analysis_preparation
    from modules.analysis.formal_source_contracts import DocumentFormalSourceConflict

    current = await setup()
    document = await current.env.flow.service._document_reuse.index.get(
        current.item.preparation_receipt.document_id,
        current.scope.user_id,
        current.scope.subject_id,
    )
    command = AnalysisFormalCommand(
        current.scope,
        'missed-request',
        document['_id'],
        document['canonicalReportId'],
        document['contentSha256'],
        document['interpretationVersion'],
        document['interpretationResultRound'] - 1,
        None,
    )
    await current.analysis.delivery.submit(command)
    with pytest.raises(DocumentFormalSourceConflict, match='later interpretation'):
        await capture_analysis_preparation(
            compatibility=current.env.api,
            index=current.env.flow.service._document_reuse.index,
            command=command,
        )
    await recover(current)
    assert list(current.analysis_records.documents.values())[0]['status'] == 'failed'
    assert not current.formal_records.documents


@pytest.mark.asyncio
async def test_preparation_ack_loss_replays_original_snapshot_after_later_document_changes():
    from modules.analysis.analysis_formal_preparation import capture_analysis_preparation
    from tests.unit.services.analysis.test_document_complete_source import change_interpretation

    current = await setup()
    reuse = current.env.flow.service._document_reuse
    document = await reuse.index.get(
        current.item.preparation_receipt.document_id, current.scope.user_id, current.scope.subject_id
    )
    command = AnalysisFormalCommand(
        current.scope,
        'lost-preparation-ack',
        document['_id'],
        document['canonicalReportId'],
        document['contentSha256'],
        document['interpretationVersion'],
        document['interpretationResultRound'],
        None,
    )
    await current.env.flow.service._document_trace_store.create(
        {'_id': command.trace_id, 'userId': current.scope.user_id, 'subjectId': current.scope.subject_id}
    )
    await current.analysis.delivery.submit(command)
    original = current.env.api.publications.put_preparation

    async def lost_ack(*args, **kwargs):
        await original(*args, **kwargs)
        raise TimeoutError('lost preparation acknowledgement')

    current.env.api.publications.put_preparation = lost_ack
    await capture_analysis_preparation(compatibility=current.env.api, index=reuse.index, command=command)
    current.env.api.publications.put_preparation = original
    await change_interpretation(current.env, current.bundle, scope=current.scope, value='477')
    prepared, bundle = await capture_analysis_preparation(
        compatibility=current.env.api, index=reuse.index, command=command
    )
    assert bundle.understanding.payload['interpretation'] == current.bundle.understanding.payload['interpretation']
    assert prepared.preparation_receipt.trace_id == 'lost-preparation-ack'
    await recover(current)
    assert list(current.analysis_records.documents.values())[0]['status'] == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('retryable', [True, None])
async def test_retryable_prior_failure_read_before_claim_does_not_terminate_new_alias(retryable):
    current = await setup()
    document = current.env.flow.index_collection.documents[current.item.preparation_receipt.document_id]
    document.update(status='failed', leaseToken=None, leaseExpiresAt=None)
    if retryable is None:
        document.pop('retryable', None)
    else:
        document['retryable'] = retryable
    original = current.analysis.delivery.submit
    observations = []

    async def preempt(command):
        receipt = await original(command)
        observations.append(await current.analysis.delivery.advance(command.command_id))
        return receipt

    current.analysis.delivery.submit = preempt
    result = await process(current)
    assert observations[0]['status'] == 'pending'
    assert result['processing']['status'] == 'complete'
    await recover(current)
    assert list(current.analysis_records.documents.values())[0]['status'] == 'completed'
    alias = await current.env.flow.source.get_owned('analysis-alias', current.scope.user_id, current.scope.subject_id)
    assert alias is not None
    assert len(current.env.flow.graph.calls) == len(current.env.flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_nonretryable_failure_does_not_publish_or_wait_for_new_attempt():
    from modules.analysis.application.documents import DocumentParsingError
    from modules.analysis.application.service import HealthReportUpstreamError

    current = await setup()
    current.env.flow.interpreter.extract_document = AsyncMock(
        side_effect=DocumentParsingError('invalid file', retryable=False)
    )
    with pytest.raises(HealthReportUpstreamError):
        await current.analysis.service.process(
            user_id=current.scope.user_id,
            subject_id=current.scope.subject_id,
            content=b'%PDF-1.7 unrecoverable',
            file_name='invalid.pdf',
            report_id='invalid-alias',
        )
    await recover(current)
    row = list(current.analysis_records.documents.values())[0]
    assert row['status'] == 'failed' and row['errorCode'] == 'DocumentFormalSourceConflict'
    assert not current.formal_records.documents


@pytest.mark.asyncio
async def test_response_is_durable_before_source_publication_and_does_not_wait_for_profile():
    current = await setup()
    current.fresh.reserve_fresh_source = AsyncMock(side_effect=AssertionError('HTTP must not publish'))
    current.analysis.service.publication.formal.execute = AsyncMock(side_effect=AssertionError('HTTP must not execute'))
    current.analysis.delivery.execute = AsyncMock(side_effect=AssertionError('HTTP must not deliver'))
    result = await process(current)
    assert result['processing']['status'] == 'complete'
    assert not current.formal_records.documents
    command_row = next(row for row in current.analysis_records.documents.values() if 'command' in row)
    ready = await current.analysis.delivery.store.get(command_row['_id'] + ':ready')
    assert ready['status'] == 'completed' and 'response' not in ready['content']
    command = AnalysisFormalCommand.from_dict(command_row['command'])
    archived = await current.env.flow.service._document_trace_store.read_payload(
        command.trace_id, current.scope.user_id, current.scope.subject_id, 'analysis_response'
    )
    assert archived == result
    assert ready['content']['prepared']['understandingSnapshotRef']
    assert not current.fresh.reserve_fresh_source.called


@pytest.mark.asyncio
async def test_readiness_storage_failure_blocks_success_and_independent_base_worker():
    current = await setup()
    original = current.analysis.delivery.store.insert

    async def fail_ready(row):
        if row['_id'].endswith(':ready'):
            raise RuntimeError('required readiness write failed')
        return await original(row)

    current.analysis.delivery.store.insert = fail_ready
    with pytest.raises(RuntimeError, match='readiness write failed'):
        await process(current)
    await recover(current, count=2)
    assert not current.formal_records.documents
    assert not current.env.ledger.intents.documents
    current.analysis.delivery.store.insert = original
    await recover(current)
    command_row = next(row for row in current.analysis_records.documents.values() if 'command' in row)
    assert command_row['status'] == 'completed'
    assert len(current.env.flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_readiness_ack_loss_reads_back_the_same_fixed_response():
    current = await setup()
    original = current.analysis.delivery.store.insert

    async def lost_ack(row):
        await original(row)
        if row['_id'].endswith(':ready'):
            raise TimeoutError('ready acknowledgement lost')
        return await original(row)

    current.analysis.delivery.store.insert = lost_ack
    result = await process(current)
    assert result['processing']['status'] == 'complete'
    assert not current.formal_records.documents
    current.analysis.delivery.store.insert = original
    await recover(current)
    assert len(current.env.flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_corrupt_ready_result_cannot_be_published_by_independent_workers():
    current = await setup()
    await process(current)
    ready = next(row for row in current.analysis_records.documents.values() if row['_id'].endswith(':ready'))
    ready['content']['responseDigest'] = 'tampered fixed result'
    await recover(current, count=2)
    assert not current.formal_records.documents
    row = next(row for row in current.analysis_records.documents.values() if 'command' in row)
    assert row['errorCode'] == 'DocumentFormalSourceConflict'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'fields',
    [
        {'title': 'metadata only'},
        {'unresolvedItems': [{'itemName': 'missing', 'resultValue': None, 'reason': 'missing result'}]},
        {'sections': [{'reportType': 'lab', 'unresolvedItems': [{'itemName': 'child', 'resultValue': None}]}]},
    ],
)
async def test_fixed_response_archive_equals_final_http_projection_for_nonclinical_results(fields):
    from modules.analysis.application.analysis_outcomes import empty_analysis
    from modules.analysis.application.analysis_outcomes import settle

    current = await setup()
    raw = settle({**empty_analysis(), **fields})
    current.env.flow.interpreter.analyze = AsyncMock(return_value=raw)
    result = await current.analysis.service.process(
        user_id=current.scope.user_id,
        subject_id=current.scope.subject_id,
        content=b'%PDF-1.7 no clinical observations',
        file_name='metadata.pdf',
        report_id='metadata-alias',
    )
    command_row = next(row for row in current.analysis_records.documents.values() if 'command' in row)
    command = AnalysisFormalCommand.from_dict(command_row['command'])
    archived = await current.env.flow.service._document_trace_store.read_payload(
        command.trace_id, current.scope.user_id, current.scope.subject_id, 'analysis_response'
    )
    assert archived == result
    assert result['processing']['status'] == 'partial' and result['aiSummary'] is None
    trace = await current.env.flow.service._document_trace_store._collection.find_one({'_id': command.trace_id})
    assert trace['profileIngestion'] == {'status': 'not_submitted', 'jobId': None}
    await recover(current)
    assert not current.formal_records.documents


@pytest.mark.asyncio
async def test_unavailable_content_cannot_return_success_before_required_readiness_is_durable():
    from modules.analysis.application.content_results import empty_content_result
    from modules.analysis.application.documents import DocumentInterpretationError

    current = await setup()

    async def empty_ocr(*, trace, **kwargs):
        await trace.write_payload('docmind_result', {'success': True, 'text': ''})
        raise DocumentInterpretationError('no text', result=empty_content_result(), content_outcome='no_readable_text')

    current.env.flow.interpreter.extract_document = empty_ocr
    original = current.analysis.delivery.store.insert

    async def fail_ready(row):
        if row['_id'].endswith(':ready'):
            raise RuntimeError('unavailable readiness write failed')
        return await original(row)

    current.analysis.delivery.store.insert = fail_ready
    with pytest.raises(RuntimeError, match='unavailable readiness write failed'):
        await current.analysis.service.process(
            user_id=current.scope.user_id,
            subject_id=current.scope.subject_id,
            content=b'%PDF-1.7 unavailable readiness',
            file_name='unavailable.pdf',
            report_id='unavailable-alias',
        )
    assert not current.formal_records.documents
    current.analysis.delivery.store.insert = original
    await recover(current)
    row = next(row for row in current.analysis_records.documents.values() if 'command' in row)
    assert row['status'] == 'not_published'
    assert await current.analysis.delivery.store.get(row['_id'] + ':ready') is not None
    assert not current.formal_records.documents


@pytest.mark.asyncio
async def test_delivery_cannot_submit_material_different_from_the_fixed_ready_response():
    from copy import deepcopy

    from modules.analysis.formal_source_contracts import DocumentFormalSourceConflict

    current = await setup()
    await process(current)
    row = next(row for row in current.analysis_records.documents.values() if 'command' in row)
    command = AnalysisFormalCommand.from_dict(row['command'])
    ready = await current.analysis.delivery.store.get(command.command_id + ':ready')
    corrupted = deepcopy(ready['content']['prepared'])
    corrupted['understandingSnapshotRef'] = 'another-fixed-result'
    with pytest.raises(DocumentFormalSourceConflict, match='differs from its readiness'):
        await current.analysis.delivery.steps.step(
            command, {'phase': 'submit_base', 'progress': {'prepared': corrupted}}
        )
    assert not current.formal_records.documents


@pytest.mark.asyncio
@pytest.mark.parametrize('acknowledgement_lost', [False, True])
async def test_fixed_response_storage_failure_or_lost_ack_keeps_delivery_behind_durable_response(acknowledgement_lost):
    current = await setup()
    store = current.env.flow.service._document_trace_store
    original = store.write_payload

    async def fail_response(trace_id, user_id, name, payload):
        if name == 'analysis_response':
            if acknowledgement_lost:
                await original(trace_id, user_id, name, payload)
            raise RuntimeError('fixed response archive unavailable')
        return await original(trace_id, user_id, name, payload)

    store.write_payload = fail_response
    if acknowledgement_lost:
        assert (await process(current))['processing']['status'] == 'complete'
    else:
        with pytest.raises(RuntimeError, match='fixed response archive unavailable'):
            await process(current)
        await recover(current, count=2)
        assert not current.formal_records.documents
        assert not current.env.ledger.intents.documents
    store.write_payload = original
    await recover(current)
    row = next(row for row in current.analysis_records.documents.values() if 'command' in row)
    assert row['status'] == 'completed'
    assert len(current.env.flow.graph.calls) == 1
