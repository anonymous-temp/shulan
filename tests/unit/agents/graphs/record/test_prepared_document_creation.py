"""Creation CAS checkpoints precede publication and survive a process restart."""

from copy import deepcopy
from dataclasses import asdict
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.basic_info import create_document_basic_info_profile_runtime
from adapters.analysis.basic_info import create_document_basic_info_source_readers
from adapters.analysis.publication import MongoDocumentPublicationLedger
from adapters.record.current_documents import create_current_record_document_port
from adapters.record.documents import LegacyRecordDocumentAdapter
from health_profile.models import HealthProfileView
from health_profile.models import HealthSubject
from modules.analysis.application.documents import DocumentBusyError
from modules.analysis.publication import PublicationConflictError
from modules.analysis.publication import PublicationReceipt
from modules.health_profile.application import HealthProfileService
from modules.health_profile.application import ProfileBootstrapResult
from modules.record.application.creation import QACreationCoordinator
from modules.record.application.creation import creation_http_status
from modules.record.documents import RECORD_CURRENT_DOCUMENT_CHECKPOINT
from modules.record.documents import RecordDocumentCheckpointError
from modules.record.documents import record_prepared_document_from_checkpoint
from tests.unit._support.current_document import SCOPE
from tests.unit._support.current_document import actual_analysis_preparation
from tests.unit._support.current_document import model_report
from tests.unit._support.current_document_profile import actual_analysis_composition
from tests.unit._support.current_document_profile import clinical_facts
from tests.unit._support.current_document_profile import settle

from .test_creation_recovery import creation_manager
from .test_creation_recovery import drain
from .test_creation_recovery import fail_initial_runtime_once
from .test_creation_recovery import receipt
from .test_prepared_documents import setup


def environment():
    document = setup()
    flow = creation_manager(record_document_port=document.adapter)
    flow.document = document
    flow.manager._loadHealthProfileContext = AsyncMock(return_value='original profile')
    return flow


def restart(flow):
    flow.manager._creation = QACreationCoordinator(flow.manager, flow.store, clock=lambda: flow.redis.now)
    flow.manager._creation.response_wait = 0.5
    flow.redis.now += 16


async def create(flow, count=1):
    return await flow.manager.createSession(
        'user-1',
        'session-1',
        file_list=[{'url': f'https://example.test/{index}.pdf'} for index in range(count)],
    )


@pytest.mark.asyncio
async def test_real_creation_saves_prepared_receipt_before_publish_and_recovers_lost_ack():
    flow = environment()
    doc = flow.document
    doc.publisher.lose_ack = True
    publish = doc.publisher.publish_prepared

    async def observed_publish(*, prepared):
        saved = await receipt(flow)
        assert saved['qaCreation']['checkpoint']['preparedDocuments']['0'] == prepared.to_checkpoint()
        return await publish(prepared=prepared)

    doc.publisher.publish_prepared = observed_publish
    first = await create(flow)
    assert first['status'] == 'failed' and first['retryable']
    saved = await receipt(flow)
    checkpoint = saved['qaCreation']['checkpoint']
    prepared = deepcopy(checkpoint['preparedDocuments']['0'])
    assert '0' not in checkpoint.get('files', {})
    assert prepared['preparationReceipt']['reused'] is False
    assert checkpoint['references']['0'] == prepared['documentId']
    assert len(doc.flow.graph.calls) == 1
    # Querying the failed creation remains a pure read.
    await flow.manager.getSession('user-1', 'session-1')
    await flow.manager.getHistory('user-1', 'session-1')
    assert len(doc.publisher.calls) == 1

    doc.api.prepare_bundle = AsyncMock(side_effect=AssertionError('fixed recovery must not prepare'))
    doc.ensure.side_effect = AssertionError('fixed recovery must not ensure self')
    doc.flow.index.get = AsyncMock(side_effect=AssertionError('fixed recovery must not read latest'))
    restart(flow)
    assert await flow.manager.recoverPendingCreations() == 1
    await drain(flow)
    complete = await receipt(flow)
    assert (await flow.manager.getSession('user-1', 'session-1'))['status'] == 'question'
    assert complete['recordDocumentProjections'][0]['prepared'] == prepared
    assert complete['recordDocumentProjections'][0]['publication']['legacySourceRevision'] == 7
    assert complete['documents'][0]['reused'] is False
    assert complete['parsedHealthReports'][0]['sourceRevision'] == 7
    assert 'recordDocumentProjections' not in await flow.manager.getSession('user-1', 'session-1')
    assert len(doc.publisher.accepted) == 1 and len(doc.publisher.calls) == 2
    doc.api.prepare_bundle.assert_not_awaited()
    flow.manager._loadHealthProfileContext.assert_awaited_once()


@pytest.mark.asyncio
async def test_creation_prepare_checkpoint_failure_does_not_publish_or_generate_first_question():
    flow = environment()
    doc = flow.document
    save = flow.store.compare_save

    async def fail_prepared(*args, **kwargs):
        draft = args[3]
        if draft['qaCreation']['checkpoint'].get('preparedDocuments'):
            raise OSError('prepare checkpoint persistence unavailable')
        return await save(*args, **kwargs)

    flow.store.compare_save = fail_prepared
    result = await create(flow)
    assert result['status'] == 'failed' and result['retryable']
    failed = await receipt(flow)
    assert not failed['qaCreation']['checkpoint'].get('preparedDocuments')
    assert not failed['qaCreation']['checkpoint'].get('files')
    assert doc.publisher.calls == []
    assert len(doc.flow.graph.calls) == 1
    flow.store.compare_save = save
    restart(flow)
    await flow.manager.recoverPendingCreations()
    await drain(flow)
    complete = await receipt(flow)
    assert (await flow.manager.getSession('user-1', 'session-1'))['status'] == 'question'
    # No prepared receipt was committed the first time. The legitimate second
    # preparation has its own actual trace/reuse receipt and reuses existing OCR.
    assert complete['documents'][0]['reused'] is True
    assert len(doc.publisher.calls) == len(doc.flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_first_question_retry_reuses_completed_projection_without_any_document_command():
    flow = environment()
    fail_initial_runtime_once(flow)
    first = await create(flow)
    assert first['status'] == 'failed' and first['retryable']
    before = await receipt(flow)
    outcome = deepcopy(before['qaCreation']['checkpoint']['files']['0'])
    flow.document.adapter.process_attachment = AsyncMock(side_effect=AssertionError('completed file is frozen'))
    restart(flow)
    await flow.manager.recoverPendingCreations()
    await drain(flow)
    after = await receipt(flow)
    assert after['recordDocumentProjections'] == [outcome['documentProjection']]
    assert after['parsedHealthReports'] == [outcome['report']]
    assert (await flow.manager.getSession('user-1', 'session-1'))['status'] == 'question'
    assert len(flow.document.publisher.calls) == 1
    flow.document.adapter.process_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_new_checkpoint_cannot_fall_back_to_legacy_document_processor():
    flow = environment()
    flow.document.publisher.lose_ack = True
    assert (await create(flow))['status'] == 'failed'
    legacy = SimpleNamespace(process=AsyncMock(), process_document=AsyncMock())
    flow.manager._record_document_port = LegacyRecordDocumentAdapter(processor=legacy)
    restart(flow)
    await flow.manager.recoverPendingCreations()
    await drain(flow)
    failed = await flow.manager.getSession('user-1', 'session-1')
    assert failed['status'] == 'failed' and failed['retryable'] is False
    assert failed['errorCode'] == 'CREATION_INPUT_INVALID'
    legacy.process.assert_not_awaited()
    legacy.process_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_new_projection_remains_readable_after_adapter_rollback():
    flow = environment()
    fail_initial_runtime_once(flow)
    assert (await create(flow))['status'] == 'failed'
    legacy = SimpleNamespace(process=AsyncMock(), process_document=AsyncMock())
    flow.manager._record_document_port = LegacyRecordDocumentAdapter(processor=legacy)
    restart(flow)
    await flow.manager.recoverPendingCreations()
    await drain(flow)
    assert (await flow.manager.getSession('user-1', 'session-1'))['status'] == 'question'
    legacy.process.assert_not_awaited()
    legacy.process_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_multiple_attachments_keep_separate_preparations_order_and_shared_report_deduplication():
    flow = environment()
    result = await create(flow, count=2)
    assert result['status'] == 'question'
    saved = await receipt(flow)
    projections = saved['recordDocumentProjections']
    assert len(projections) == 2
    assert [projection['prepared']['fileIndex'] for projection in projections] == [0, 1]
    assert len(saved['parsedHealthReports']) == 1
    assert len(flow.document.publisher.accepted) == 1
    assert len(flow.document.flow.graph.calls) == len(flow.document.flow.client.submit_calls) == 1
    assert [document['fileIndex'] for document in saved['documents']] == [0, 1]
    assert (
        projections[0]['prepared']['preparationReceipt']['traceId']
        != projections[1]['prepared']['preparationReceipt']['traceId']
    )


def self_profile():
    now = datetime(2026, 11, 14, tzinfo=UTC)
    return ProfileBootstrapResult(
        subject=HealthSubject(
            user_id=SCOPE.user_id,
            subject_id=SCOPE.subject_id,
            kind='self',
            lifecycle_epoch=3,
            created_at=now,
            updated_at=now,
        ),
        view=HealthProfileView(
            user_id=SCOPE.user_id,
            subject_id=SCOPE.subject_id,
            profile_version=1,
            updated_at=now,
        ),
        created=False,
    )


def result(source_id, *, status='published'):
    withheld = status == 'no_contribution'
    return {
        'status': status,
        'sourceId': source_id,
        'sourceRevision': None if withheld else 1,
        'sourceSnapshotRef': None if withheld else 'fixed-source-capture',
        'publicationRevision': None if withheld else 1,
        'publication': (
            asdict(PublicationReceipt(source_id, 1, 'content-digest', 'pending', 'durable-delivery'))
            if status == 'published'
            else None
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('failure', 'expected_status', 'expected_code'),
    [
        (ConnectionError, 503, 'CREATION_INTERNAL_ERROR'),
        (DocumentBusyError, 503, 'DOCUMENT_PROCESSING_BUSY'),
        ('busy_after_conflict', 503, 'DOCUMENT_PROCESSING_BUSY'),
        (PublicationConflictError, 409, 'DOCUMENT_SOURCE_CONFLICT'),
    ],
)
async def test_normal_record_creation_keeps_fixed_input_on_document_failure(
    monkeypatch, failure, expected_status, expected_code
):
    upstream = await actual_analysis_preparation([model_report()])
    ensure_self = AsyncMock(return_value=self_profile())
    consume = AsyncMock()

    delivery = SimpleNamespace(consume_prepared=consume)
    port = create_current_record_document_port(
        prepared=upstream.prepared, documents=delivery, ensure_self_profile=ensure_self
    )
    flow = creation_manager(record_document_port=port)
    flow.manager._loadHealthProfileContext = AsyncMock(return_value='initial snapshot')
    calls = []

    async def publish(**kwargs):
        calls.append(kwargs)
        assert kwargs['scope'] == SCOPE
        assert kwargs['lifecycle_epoch'] == 3
        saved = await receipt(flow)
        checkpoint = saved['qaCreation']['checkpoint']['preparedDocuments']['0']
        parsed = record_prepared_document_from_checkpoint(checkpoint)
        assert saved['qaCreation']['documentConsumerRevision'] == RECORD_CURRENT_DOCUMENT_CHECKPOINT
        assert checkpoint['preparationReceipt']['traceId'] == kwargs['prepared'].preparation_receipt.trace_id
        assert checkpoint['publicationRequestId'] == kwargs['request_id']
        assert parsed.source_revision == 1
        if len(calls) == 1:
            if failure == 'busy_after_conflict':
                raise DocumentBusyError('predecessor advanced') from PublicationConflictError('stale predecessor')
            raise failure('synthetic publication interruption')
        return result(parsed.source_id)

    consume.side_effect = publish
    failed = await create(flow)
    assert failed['status'] == 'failed'
    assert failed['retryable'] is (expected_status == 503)
    assert creation_http_status(failed) == expected_status
    assert failed['errorCode'] == expected_code
    saved = await receipt(flow)
    checkpoint = saved['qaCreation']['checkpoint']
    assert '0' not in checkpoint.get('files', {})
    fixed = deepcopy(checkpoint['preparedDocuments']['0'])
    assert await flow.manager.getSession('user-1', 'session-1') == failed
    assert consume.await_count == 1
    assert len(upstream.gateway.complete_requests) == len(upstream.upstream.client.submit_calls) == 1
    if not failed['retryable']:
        restart(flow)
        assert await flow.manager.recoverPendingCreations() == 0
        assert await receipt(flow) == saved
        assert consume.await_count == 1
        return
    ensure_self.side_effect = AssertionError('replay must not resolve the current subject')
    monkeypatch.setattr(
        upstream.prepared, 'prepare_bundle', AsyncMock(side_effect=AssertionError('replay must not prepare again'))
    )

    restarted = create_current_record_document_port(
        prepared=upstream.prepared, documents=delivery, ensure_self_profile=ensure_self
    )
    flow.manager._record_document_port = restarted
    restart(flow)
    assert await flow.manager.recoverPendingCreations() == 1
    await drain(flow)
    active = await receipt(flow)
    projected = active['recordDocumentProjections'][0]
    assert active['status'] == 'active'
    assert active['healthProfileContext'] == 'initial snapshot'
    assert active['hasHealthReport'] is True
    assert active['extractedInfo']['age'] is None
    assert active['extractedInfoSources'] == {}
    assert projected['prepared'] == fixed
    assert projected['publication'] == {
        'canonicalReportId': fixed['sourceId'],
        'status': 'published',
        'sourceRevision': 1,
        'sourceSnapshotRef': 'fixed-source-capture',
        'publicationRevision': 1,
        'receiptRef': 'durable-delivery',
    }
    assert calls[0] == calls[1]
    ensure_self.assert_awaited_once()
    upstream.prepared.prepare_bundle.assert_not_awaited()
    assert len(upstream.gateway.complete_requests) == len(upstream.upstream.client.submit_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['unchanged', 'no_contribution'])
async def test_current_consumption_preserves_real_revision_or_omits_archive_only_revision(status):
    upstream = await actual_analysis_preparation([model_report()])
    consume = AsyncMock()

    port = create_current_record_document_port(
        prepared=upstream.prepared,
        documents=SimpleNamespace(consume_prepared=consume),
        ensure_self_profile=AsyncMock(return_value=self_profile()),
    )
    saved = AsyncMock()

    async def publish(**kwargs):
        checkpoint = saved.await_args.args[0]
        assert kwargs['request_id'] == checkpoint.request_id
        return result(checkpoint.source_id, status=status)

    consume.side_effect = publish
    context = await port.process_attachment(
        user_id=SCOPE.user_id,
        session_id='record-current',
        file_index=0,
        attachment={'url': 'https://example.test/synthetic.pdf'},
        settled_document_ids=set(),
        on_prepared=saved,
    )
    assert context.report['status'] == 'pending'
    assert context.report['processing']['status'] == context.processing['status'] == 'complete'
    assert set(context.report) <= {'status', 'processing', 'reportId', 'userId', 'subjectId', 'sourceRevision'}
    assert context.publication.status == status
    assert ('sourceRevision' in context.report) is (status == 'unchanged')
    assert ('sourceRevision' in context.processing) is (status == 'unchanged')
    assert (await port.read_projection(prepared=context.prepared, publication=context.publication)) == context
    consume.assert_awaited_once()

    consume.side_effect = None
    consume.return_value = {**result(context.prepared.source_id), 'publicationRevision': 2}
    reference = {'documentId': context.prepared.document_id}
    fixed = context.prepared.to_checkpoint()
    with pytest.raises(RecordDocumentCheckpointError, match='different fixed evidence'):
        await port.process_attachment(
            user_id=SCOPE.user_id,
            session_id='record-current',
            file_index=0,
            attachment=reference,
            settled_document_ids=set(),
            prepared_checkpoint=fixed,
        )

    forged = deepcopy(fixed)
    forged['lifecycleEpoch'] += 1
    with pytest.raises(RecordDocumentCheckpointError, match='identity changed'):
        await port.process_attachment(
            user_id=SCOPE.user_id,
            session_id='record-current',
            file_index=0,
            attachment={'documentId': context.prepared.document_id},
            settled_document_ids=set(),
            prepared_checkpoint=forged,
        )


@pytest.mark.asyncio
async def test_record_creation_restarts_and_reuses_actual_document_profile_publication(monkeypatch):
    provider = await actual_analysis_composition(monkeypatch, [model_report()])
    ensure_self = AsyncMock(wraps=HealthProfileService(provider.repository).ensure_self_profile)

    def make_record_port():
        return create_current_record_document_port(
            prepared=provider.prepared,
            documents=provider.runtime.consumption,
            ensure_self_profile=ensure_self,
        )

    flow = creation_manager(record_document_port=make_record_port())
    flow.manager._loadHealthProfileContext = AsyncMock(return_value='initial profile')
    user_id = provider.scope.user_id
    session_id = 'record-original'
    put = provider.readers.records.put

    async def lose_publication_ack(kind, scope, identity, value):
        await put(kind, scope, identity, value)
        if kind == 'current_consumer_result':
            saved = await flow.manager._session_cache.get(user_id, session_id)
            prepared = saved['qaCreation']['checkpoint']['preparedDocuments']['0']
            assert prepared['lifecycleEpoch'] == provider.epoch
            assert prepared['scope'] == provider.scope.as_dict()
            assert prepared['sourceId'] == value['sourceId']
            assert value['publication']['state'] == 'received'
            raise ConnectionError('synthetic Document result acknowledgement lost')

    with monkeypatch.context() as fault:
        fault.setattr(provider.readers.records, 'put', lose_publication_ack)
        failure = await flow.manager.createSession(
            user_id, session_id, file_list=[{'url': 'https://example.test/synthetic.pdf'}]
        )
    assert failure['status'] == 'failed' and failure['retryable']
    assert creation_http_status(failure) == 503
    failed = await flow.manager._session_cache.get(user_id, session_id)
    checkpoint = failed['qaCreation']['checkpoint']
    fixed = deepcopy(checkpoint['preparedDocuments']['0'])
    assert '0' not in checkpoint.get('files', {})
    source_id = fixed['sourceId']
    accepted = await provider.ledger.read_source(provider.scope, source_id)
    assert accepted['publicationRevision'] == accepted['acceptedRevision'] == 1
    assert accepted['appliedRevision'] == 0

    provider.ledger = MongoDocumentPublicationLedger(sources=provider.ledger.sources, intents=provider.ledger.intents)
    provider.readers = create_document_basic_info_source_readers(
        prepared=provider.prepared, ledger=provider.ledger, admission_records=provider.readers.records.collection
    )
    provider.runtime = create_document_basic_info_profile_runtime(
        readers=provider.readers, profile_inbox=provider.profile.inbox, clock=lambda: provider.now[0]
    )
    flow.manager._record_document_port = make_record_port()
    restart(flow)
    with monkeypatch.context() as fault:
        fault.setattr(
            provider.prepared, 'prepare_bundle', AsyncMock(side_effect=AssertionError('replay must not prepare again'))
        )
        ensure_self.side_effect = AssertionError('replay must not resolve the current subject')
        assert await flow.manager.recoverPendingCreations() == 1
        await drain(flow)
        ensure_self.assert_awaited_once()
        provider.prepared.prepare_bundle.assert_not_awaited()
    ensure_self.side_effect = None

    stored = await flow.manager._session_cache.get(user_id, session_id)
    assert stored['status'] == 'active'
    assert stored['hasHealthReport'] is True
    assert stored['extractedInfo']['age'] is None
    assert stored['extractedInfoSources'] == {}
    assert stored['healthProfileContext'] == 'initial profile'
    _, report_status = flow.manager._formatHealthReports(stored['parsedHealthReports'])
    assert report_status == 'pending'
    assert all(
        key not in stored['parsedHealthReports'][0]
        for key in ('title', 'reportTime', 'aiSummary', 'basicInfo', 'sections', 'observations', 'unresolvedItems')
    )
    original = stored['recordDocumentProjections'][0]
    assert original['prepared'] == fixed
    publication = original['publication']
    assert publication['status'] == 'published'
    assert publication['publicationRevision'] == 1
    assert publication['sourceSnapshotRef'] == accepted['currentIntent']['event']['snapshotRef']['snapshotId']

    await settle(provider)
    await flow.manager.createSession(user_id, 'record-repeat', file_list=[{'documentId': fixed['documentId']}])
    repeated = await flow.manager._session_cache.get(user_id, 'record-repeat')
    assert repeated['status'] == 'active'
    reused = repeated['recordDocumentProjections'][0]
    assert reused['prepared']['preparationReceipt']['reused'] is True
    assert reused['prepared']['preparationReceipt']['traceId'] != fixed['preparationReceipt']['traceId']
    assert reused['prepared']['lifecycleEpoch'] == fixed['lifecycleEpoch']
    assert reused['publication'] == {**publication, 'status': 'unchanged'}
    settled = await provider.ledger.read_source(provider.scope, source_id)
    assert settled['publicationRevision'] == settled['appliedRevision'] == 1
    facts = await clinical_facts(provider)
    assert [fact.display_name for fact in facts] == ['ALT']
    assert len(provider.gateway.complete_requests) == len(provider.upstream.client.submit_calls) == 1
