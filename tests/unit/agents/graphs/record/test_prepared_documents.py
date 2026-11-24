"""Fixed Record projections, durable prepare boundary, and publication replay."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from datetime import timezone
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.snapshots import MongoDocumentSnapshotStore
from adapters.record.prepared_documents import PreparedRecordDocumentAdapter
from health_profile.report_source import HealthReportSourceRecord
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.contracts import OutputSelection
from modules.analysis.snapshots import DocumentScope
from modules.analysis.understanding import DocumentUnderstandingService
from modules.record.application import QASessionManager
from modules.record.documents import RecordDocumentCheckpointError
from modules.record.documents import RecordDocumentPublicationV1
from modules.record.documents import RecordPreparedDocumentV1
from modules.record.documents import render_record_document_v1
from tests.unit.services.analysis.fakes import MemoryMongoCollection
from tests.unit.services.analysis.test_document_understanding import environment
from tests.unit.services.analysis.test_document_workflow import extended_flow


class Publisher:
    """A durable provider fake: ACK loss must not change an accepted identity."""

    def __init__(self):
        self.accepted = {}
        self.calls = []
        self.lose_ack = False

    async def publish_prepared(self, *, prepared):
        self.calls.append(prepared)
        key = prepared.understanding_snapshot_id
        receipt = self.accepted.setdefault(
            key, RecordDocumentPublicationV1(prepared.source_id, 7, 'queued', 'ingestion-7')
        )
        if self.lose_ack:
            self.lose_ack = False
            raise ConnectionError('publication committed, response lost')
        return receipt


def setup():
    env = environment()
    env.publisher = Publisher()
    env.ensure = AsyncMock(return_value=DocumentScope('user-1', 'subject-self'))
    env.adapter = PreparedRecordDocumentAdapter(
        understanding=env.api, publisher=env.publisher, ensure_self_scope=env.ensure
    )
    env.checkpoints = []

    async def checkpoint(prepared):
        env.checkpoints.append(deepcopy(prepared.to_checkpoint()))
        assert not env.publisher.calls

    env.checkpoint = checkpoint
    return env


def request(env, **changes):
    return {
        'user_id': 'user-1',
        'session_id': 'session-1',
        'file_index': 0,
        'attachment': {'url': 'https://example.test/record.pdf', 'fileType': 'pdf'},
        'settled_document_ids': set(),
        'on_prepared': env.checkpoint,
        **changes,
    }


def full_interpretation():
    return {
        'reportType': 'lab',
        'title': 'Synthetic attachment',
        'reportTime': '2026-10-01',
        'status': 'abnormal',
        'subject': {'name': 'Synthetic subject'},
        'basicInfo': {'age': 42, 'height': 163, 'weight': 60},
        'patientInfo': {'gender': 'F'},
        'aiSummary': 'derived, not a source observation',
        'observations': [
            {'itemName': 'B', 'resultValue': '2', 'unit': 'mmol/L', 'evidence': {'page': 1}, 'rawFlag': '*'},
            {'itemName': 'A', 'resultValue': '1', 'sourceId': 'private-source', 'metadataEvidence': {'page': 2}},
        ],
        'expansion': [{'kind': 'imaging', 'content': ['original finding'], 'evidence': {'page': 3}}],
        'unresolvedItems': [
            {'itemName': 'unresolved', 'resultValue': None, 'sourceSection': 0, 'evidence': {'page': 4}},
            {'itemName': None, 'resultValue': '1', 'sourceSection': True},
            'discard malformed item',
        ],
        'sections': [
            {
                'reportType': 'imaging',
                'reportTime': '2025-01-01',
                'expansion': 'first original section',
                'analysisEvidence': {'private': True},
                'metadataEvidence': {'private': True},
                'observations': [{'itemName': 'nested', 'resultValue': 'yes', 'sourceId': 'private'}],
            },
            {'reportType': 'lab', 'title': 'second section', 'expansion': None},
        ],
        'analysisEvidence': {'private': True},
        'sourceBindings': {'private': True},
        'ingestionObservations': [{'private': True}],
        'verifiedObservationPointers': ['private'],
        'metadataEvidence': {'private': True},
        'expansionEvidence': {'private': True},
        'otherLegacyField': {'values': [3, 1, 2]},
    }


def legacy_public(raw, prepared, revision):
    now = datetime.now(timezone.utc)
    return HealthReportSourceRecord(
        report_id=prepared.source_id,
        user_id=prepared.user_id,
        subject_id=prepared.subject_id,
        source_revision=revision,
        source_fingerprint='synthetic',
        result=raw,
        ingestion_status='queued',
        ingestion_job_id='job',
        created_at=now,
        updated_at=now,
    ).public_result()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'processing',
    [
        None,
        {'status': 'complete', 'version': 'original-version'},
        {'status': 'partial', 'retryable': False, 'issueCount': 2},
        {'status': 'partial', 'retryable': True, 'failureKind': 'technical'},
        {'status': 'partial', 'contentOutcome': 'no_verified_results'},
        {'status': 'unavailable', 'contentOutcome': 'no_readable_text'},
        {'status': 'unavailable', 'contentOutcome': 'no_verified_results'},
        {'status': 'partial', 'contentOutcome': 'interpretation_incomplete'},
    ],
)
async def test_owned_renderer_preserves_legacy_report_and_first_question_context(processing):
    env = setup()
    context = await env.adapter.process_attachment(**request(env))
    prepared = context.prepared
    raw = full_interpretation()
    if processing is not None:
        raw['processing'] = processing
    original = deepcopy(raw)
    for revision in (1, 7):
        publication = replace(context.publication, legacy_source_revision=revision)
        actual = render_record_document_v1(raw, prepared=prepared, publication=publication)
        expected = legacy_public(raw, prepared, revision)
        assert actual.report == expected
        assert list(actual.report) == list(expected)
        assert QASessionManager._formatHealthReports([actual.report]) == QASessionManager._formatHealthReports(
            [expected]
        )
        assert QASessionManager._extractBasicInfo([actual.report]) == QASessionManager._extractBasicInfo([expected])
        assert [item['itemName'] for item in actual.report['observations']] == ['B', 'A']
        assert [item['reportType'] for item in actual.report['sections']] == ['imaging', 'lab']
        assert actual.document['reused'] is False
    assert raw == original


@pytest.mark.asyncio
async def test_shared_presentation_upgrade_cannot_change_fixed_record_renderer(monkeypatch):
    env = setup()
    context = await env.adapter.process_attachment(**request(env))
    raw = full_interpretation()
    raw['processing'] = {'status': 'unavailable', 'contentOutcome': 'no_readable_text'}
    expected = render_record_document_v1(raw, prepared=context.prepared, publication=context.publication)
    monkeypatch.setattr('utils.report_content.report_public_content', lambda _: {'newPresentation': True})
    monkeypatch.setattr(
        'modules.analysis.application.content_results.CONTENT_MESSAGES', {'no_readable_text': 'changed'}
    )
    assert render_record_document_v1(raw, prepared=context.prepared, publication=context.publication) == expected


@pytest.mark.asyncio
async def test_prepare_is_checkpointed_before_publication_with_true_receipt_and_fixed_selection():
    env = setup()
    ready = AsyncMock()
    context = await env.adapter.process_attachment(**request(env, on_document_ready=ready))
    prepared = context.prepared
    assert env.checkpoints == [prepared.to_checkpoint()]
    assert context.document['reused'] is False
    assert prepared.preparation_trace_id
    assert context.report['sourceRevision'] == context.processing['sourceRevision'] == 7
    assert context.to_checkpoint()['documentProjection']['prepared'] == env.checkpoints[0]
    assert context.to_checkpoint()['documentProjection']['publication']['legacySourceRevision'] == 7
    assert len(env.flow.graph.calls) == len(env.flow.client.submit_calls) == 1
    assert not env.flow.collection.documents and not env.flow.jobs.jobs
    ready.assert_awaited_once_with(prepared.document_id)
    env.ensure.assert_awaited_once_with('user-1')
    restored = RecordPreparedDocumentV1.from_checkpoint(env.checkpoints[0])
    assert restored == prepared
    env.checkpoints[0]['preparationReceipt']['reused'] = True
    assert prepared.preparation_reused is False


@pytest.mark.asyncio
async def test_lost_publication_ack_replays_fixed_bundle_and_original_receipt_without_prepare():
    env = setup()
    env.publisher.lose_ack = True
    with pytest.raises(ConnectionError):
        await env.adapter.process_attachment(**request(env))
    checkpoint = env.checkpoints[0]
    prepared = RecordPreparedDocumentV1.from_checkpoint(checkpoint)
    document = env.flow.index_collection.documents[prepared.document_id]
    env.flow.traces.payloads[(document['payloadRefs']['interpretation'], 'interpretation')]['aiSummary'] = (
        'newer result'
    )
    env.api.prepare_bundle = AsyncMock(side_effect=AssertionError('replay must not prepare/latest'))
    env.ensure.side_effect = AssertionError('replay must not ensure subject')
    env.flow.index.get = AsyncMock(side_effect=AssertionError('no current index reads'))
    env.flow.traces.read_payload = AsyncMock(side_effect=AssertionError('no archive reads'))
    settled = set()
    actual = await env.adapter.process_attachment(
        **request(
            env,
            attachment={'documentId': prepared.document_id},
            prepared_checkpoint=checkpoint,
            settled_document_ids=settled,
            on_prepared=None,
        )
    )
    assert len(env.publisher.accepted) == 1
    assert env.publisher.calls == [prepared, prepared]
    assert actual.prepared == prepared
    assert actual.document['reused'] is False
    assert actual.report.get('aiSummary') != 'newer result'
    assert settled == {prepared.document_id}
    env.api.prepare_bundle.assert_not_awaited()
    assert env.ensure.await_count == 1


@pytest.mark.asyncio
async def test_checkpoint_write_failure_cannot_publish():
    env = setup()
    with pytest.raises(OSError, match='checkpoint'):
        await env.adapter.process_attachment(**request(env, on_prepared=AsyncMock(side_effect=OSError('checkpoint'))))
    assert env.publisher.calls == []
    assert not env.flow.collection.documents and not env.flow.jobs.jobs


@pytest.mark.asyncio
async def test_unwired_durable_callback_stops_before_any_prepare_or_scope_creation():
    env = setup()
    env.api.prepare_bundle = AsyncMock()
    with pytest.raises(RecordDocumentCheckpointError, match='on_prepared'):
        await env.adapter.process_attachment(**request(env, on_prepared=None))
    env.ensure.assert_not_awaited()
    env.api.prepare_bundle.assert_not_awaited()
    assert not env.publisher.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['renderer', 'selection', 'user', 'session', 'file', 'attachment', 'receipt'])
async def test_bad_checkpoint_fails_before_read_or_publication(mutation):
    env = setup()
    await env.adapter.process_attachment(**request(env))
    checkpoint = deepcopy(env.checkpoints[0])
    if mutation == 'renderer':
        checkpoint['rendererRevision'] = 'record-document-renderer.v2'
    elif mutation == 'selection':
        checkpoint['selection']['implementation_revision'] = 'new-deployment'
    elif mutation == 'user':
        checkpoint['scope']['userId'] = 'other-user'
    elif mutation == 'session':
        checkpoint['sessionId'] = 'other-session'
    elif mutation == 'file':
        checkpoint['fileIndex'] = 3
    elif mutation == 'attachment':
        checkpoint['attachment'] = {'url': 'https://example.test/other.pdf'}
    else:
        checkpoint['preparationReceipt']['documentId'] = 'other-document'
    env.api.read_bundle = AsyncMock()
    env.publisher.calls.clear()
    with pytest.raises(RecordDocumentCheckpointError):
        await env.adapter.process_attachment(**request(env, prepared_checkpoint=checkpoint))
    env.api.read_bundle.assert_not_awaited()
    assert not env.publisher.calls
    assert env.ensure.await_count == 1


@pytest.mark.asyncio
async def test_missing_fixed_snapshot_is_not_replaced_by_new_preparation():
    env = setup()
    await env.adapter.process_attachment(**request(env))
    env.api.read_bundle = AsyncMock(side_effect=DocumentNotFoundError('snapshot unavailable'))
    env.api.prepare_bundle = AsyncMock()
    env.publisher.calls.clear()
    with pytest.raises(DocumentNotFoundError):
        await env.adapter.process_attachment(**request(env, prepared_checkpoint=env.checkpoints[0]))
    env.api.prepare_bundle.assert_not_awaited()
    assert not env.publisher.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['factsSnapshotRef', 'materialSnapshotRef', 'sourceId', 'sourceRevision'])
async def test_fixed_bundle_mismatch_is_rejected_before_publication(field):
    env = setup()
    await env.adapter.process_attachment(**request(env))
    checkpoint = deepcopy(env.checkpoints[0])
    checkpoint[field] = 2 if field == 'sourceRevision' else 'wrong-fixed-reference'
    env.publisher.calls.clear()
    with pytest.raises(RecordDocumentCheckpointError, match='fixed'):
        await env.adapter.process_attachment(**request(env, prepared_checkpoint=checkpoint))
    assert not env.publisher.calls


@pytest.mark.asyncio
async def test_read_projection_does_not_ensure_prepare_or_publish():
    env = setup()
    original = await env.adapter.process_attachment(**request(env))
    env.ensure.side_effect = AssertionError('read must not ensure')
    env.api.prepare_bundle = AsyncMock(side_effect=AssertionError('read must not prepare'))
    env.publisher.publish_prepared = AsyncMock(side_effect=AssertionError('read must not publish'))
    read = await env.adapter.read_projection(prepared=original.prepared, publication=original.publication)
    assert read == original
    env.api.prepare_bundle.assert_not_awaited()
    env.publisher.publish_prepared.assert_not_awaited()
    assert env.ensure.await_count == 1


@pytest.mark.asyncio
async def test_restore_enforces_subject_scope_without_ensuring_another_subject():
    env = setup()
    await env.adapter.process_attachment(**request(env))
    checkpoint = deepcopy(env.checkpoints[0])
    checkpoint['scope']['subjectId'] = 'another-subject'
    env.publisher.calls.clear()
    with pytest.raises(DocumentNotFoundError):
        await env.adapter.process_attachment(**request(env, prepared_checkpoint=checkpoint))
    assert not env.publisher.calls
    assert env.ensure.await_count == 1


@pytest.mark.asyncio
async def test_repeated_prepare_keeps_same_bundle_but_actual_per_request_trace_and_reuse():
    env = setup()
    first = await env.adapter.process_attachment(**request(env))
    second = await env.adapter.process_attachment(**request(env, on_prepared=AsyncMock()))
    assert first.prepared.understanding_snapshot_id == second.prepared.understanding_snapshot_id
    assert first.prepared.preparation_trace_id != second.prepared.preparation_trace_id
    assert first.document['reused'] is False and second.document['reused'] is True
    assert len(env.publisher.accepted) == len(env.flow.graph.calls) == len(env.flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_attachment_is_frozen_before_scope_await():
    env = setup()
    entered, release = asyncio.Event(), asyncio.Event()

    async def ensure(user_id):
        entered.set()
        await release.wait()
        return DocumentScope(user_id, 'subject-self')

    env.adapter.ensure_self_scope = ensure
    attachment = {'url': 'https://example.test/original.pdf', 'fileType': 'pdf'}
    task = asyncio.create_task(env.adapter.process_attachment(**request(env, attachment=attachment)))
    await entered.wait()
    attachment['url'] = 'https://example.test/changed.pdf'
    release.set()
    result = await task
    assert result.prepared.to_checkpoint()['attachment']['url'] == 'https://example.test/original.pdf'
    assert env.flow.downloader.download.call_args.args[0] == 'https://example.test/original.pdf'


@pytest.mark.asyncio
async def test_recovery_checkpoint_is_frozen_before_snapshot_await():
    env = setup()
    first = await env.adapter.process_attachment(**request(env))
    checkpoint = deepcopy(env.checkpoints[0])
    entered, release = asyncio.Event(), asyncio.Event()
    read = env.api.read_bundle

    async def blocked_read(**kwargs):
        entered.set()
        await release.wait()
        return await read(**kwargs)

    env.api.read_bundle = blocked_read
    task = asyncio.create_task(
        env.adapter.process_attachment(**request(env, prepared_checkpoint=checkpoint, on_prepared=None))
    )
    await entered.wait()
    checkpoint['preparationReceipt']['reused'] = True
    checkpoint['selection']['implementation_revision'] = 'newest'
    checkpoint['scope']['subjectId'] = 'another'
    release.set()
    result = await task
    assert result.prepared == first.prepared
    assert result.document['reused'] is False


@pytest.mark.asyncio
async def test_same_named_report_projection_vnext_does_not_change_record_snapshot_or_renderer():
    flow = extended_flow()
    store = MongoDocumentSnapshotStore(snapshots=MemoryMongoCollection(), chunks=MemoryMongoCollection())
    understanding = DocumentUnderstandingService(processing_service=flow.service, snapshots=store)
    publisher = Publisher()
    adapter = PreparedRecordDocumentAdapter(
        understanding=understanding,
        publisher=publisher,
        ensure_self_scope=AsyncMock(return_value=DocumentScope('user-1', 'subject-self')),
    )
    checkpoint = AsyncMock()
    args = {
        'user_id': 'user-1',
        'session_id': 's',
        'file_index': 0,
        'attachment': {'url': 'https://example.test/source.pdf'},
        'settled_document_ids': set(),
        'on_prepared': checkpoint,
    }
    original = await adapter.process_attachment(**args)
    newer = AsyncMock(return_value={'subject': {}, 'metrics': [{'itemName': 'Report-only vnext'}]})
    flow.service.output_profiles.register_report_source(
        newer, version='report-source-next', contract_version='2', legacy=False
    )
    result = await flow.service.process_document(
        user_id='user-1',
        subject_id='subject-self',
        document_id=original.prepared.document_id,
        output_selection=OutputSelection('report_source', '2', 'report-source-next'),
    )
    assert result.structured_data['metrics'] == [{'itemName': 'Report-only vnext'}]
    newer.assert_awaited_once()
    flow.extractor.assert_not_awaited()
    restored = await adapter.process_attachment(
        **{
            **args,
            'on_prepared': None,
            'prepared_checkpoint': original.prepared.to_checkpoint(),
        }
    )
    assert restored == original
    assert 'metrics' not in restored.report
    assert len(flow.graph.calls) == len(flow.client.submit_calls) == 1
