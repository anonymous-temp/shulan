"""Record keeps fixed formal source commands separate from first-question readiness."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bson.int64 import Int64

from adapters.record.formal_documents import create_formal_record_document_port
from modules.record.documents import LEGACY_RECORD_DOCUMENT_CONSUMER
from modules.record.documents import RECORD_DOCUMENT_CHECKPOINT
from modules.record.documents import RECORD_FORMAL_DOCUMENT_CHECKPOINT
from modules.record.documents import RecordDocumentCheckpointError
from modules.record.documents import RecordDocumentPublicationPending
from modules.record.documents import RecordDocumentVersionError
from modules.record.profile_source import LEGACY_RECORD_PROFILE_REVISION
from modules.record.profile_source import RECORD_PROFILE_SOURCE_REVISION

from .test_creation_recovery import creation_manager
from .test_creation_recovery import drain
from .test_creation_recovery import fail_initial_runtime_once
from .test_creation_recovery import receipt
from .test_prepared_document_creation import create
from .test_prepared_document_creation import restart
from .test_prepared_documents import request
from .test_prepared_documents import setup


class Formal:
    def __init__(self, document):
        self.document = document
        self.calls = []
        self.status = 'completed'
        self.fail_once = False
        self.before = None
        self.revision = Int64(2 + int('abcdef123456', 16))

    async def execute(self, command):
        self.calls.append(command.as_dict())
        if self.before:
            await self.before(command)
        if self.fail_once:
            self.fail_once = False
            raise ConnectionError('formal command ACK lost')
        prepared = command.inputs[0]
        bundle = await self.document.api.read_bundle(
            scope=command.scope,
            selection=prepared.selection,
            understanding_snapshot_id=prepared.understanding_snapshot_ref,
        )
        bases = (
            []
            if self.status != 'completed'
            else [
                {
                    'kind': 'archive',
                    'sourceId': bundle.material.source_id,
                    'canonicalReportId': bundle.material.source_id,
                    'legacySourceRevision': self.revision,
                    'ingestionStatus': 'source_managed',
                    'jobId': None,
                    'publication': {'state': 'received'},
                }
            ]
        )
        return {
            'commandId': command.command_id,
            'commandRevision': command.command_revision,
            'status': self.status,
            'completedBaseCount': len(bases),
            'totalUniqueSources': 1,
            'result': {'basePublications': bases},
            'errorCode': 'formal_deadline_exhausted' if self.status == 'failed' else None,
        }


def document():
    doc = setup()
    doc.previous = doc.adapter
    doc.formal = Formal(doc)
    compatibility = SimpleNamespace(
        ensure_self_scope=doc.ensure, prepare_bundle=doc.api.prepare_bundle, read_bundle=doc.api.read_bundle
    )
    doc.adapter = create_formal_record_document_port(compatibility, doc.formal, previous_port=doc.previous)
    return doc


def environment():
    doc = document()
    flow = creation_manager(record_document_port=doc.adapter)
    flow.document = doc
    flow.manager._loadHealthProfileContext = AsyncMock(return_value='original profile')
    return flow


@pytest.mark.asyncio
async def test_formal_pending_has_fixed_command_then_restore_reads_without_reprepare():
    doc = document()
    doc.formal.status = 'pending'
    with pytest.raises(RecordDocumentPublicationPending):
        await doc.adapter.process_attachment(**request(doc))
    fixed = deepcopy(doc.checkpoints[0])
    assert fixed['contractVersion'] == RECORD_FORMAL_DOCUMENT_CHECKPOINT
    assert fixed['formalCommand']['operation'] == 'base'
    assert doc.publisher.calls == []
    doc.adapter.understanding.prepare_bundle = AsyncMock(side_effect=AssertionError('no repeated preparation'))
    doc.ensure.side_effect = AssertionError('no subject creation during restore')
    doc.formal.status = 'completed'
    result = await doc.adapter.process_attachment(**request(doc, prepared_checkpoint=fixed))
    assert doc.formal.calls[0] == doc.formal.calls[1]
    assert result.prepared.to_checkpoint() == fixed
    assert type(result.publication.legacy_source_revision) is int
    assert result.report['sourceRevision'] == doc.formal.revision
    assert result.document['reused'] is False
    doc.adapter.understanding.prepare_bundle.assert_not_awaited()


@pytest.mark.asyncio
async def test_checkpoint_ack_precedes_formal_command_and_missing_ack_does_not_publish():
    doc = document()
    with pytest.raises(OSError):
        await doc.adapter.process_attachment(**request(doc, on_prepared=AsyncMock(side_effect=OSError('checkpoint'))))
    assert not doc.formal.calls and not doc.publisher.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'field,value', [('commandId', 'wrong'), ('commandRevision', 'future'), ('operation', 'report')]
)
async def test_unknown_or_mutated_formal_checkpoint_never_falls_back(field, value):
    doc = document()
    doc.formal.status = 'pending'
    with pytest.raises(RecordDocumentPublicationPending):
        await doc.adapter.process_attachment(**request(doc))
    fixed = deepcopy(doc.checkpoints[0])
    fixed['formalCommand'][field] = value
    with pytest.raises(RecordDocumentCheckpointError):
        await doc.adapter.process_attachment(**request(doc, prepared_checkpoint=fixed))
    assert len(doc.formal.calls) == 1 and not doc.publisher.calls


@pytest.mark.asyncio
async def test_first_question_waits_without_queries_and_normal_wait_does_not_use_transport_budget():
    flow = environment()
    flow.document.formal.status = 'pending'

    async def persisted_before_command(command):
        saved = await receipt(flow)
        fixed = saved['qaCreation']['checkpoint']['preparedDocuments']['0']
        assert fixed['formalCommand']['commandId'] == command.command_id
        assert fixed['preparationReceipt']['traceId'] == command.inputs[0].preparation_receipt.trace_id

    flow.document.formal.before = persisted_before_command
    first = await create(flow)
    assert first['status'] == 'processing' and first['stage'] == 'processing_reports'
    original = await receipt(flow)
    assert original['qaCreation']['attempts'] == 0
    assert not original['qaCreation']['checkpoint'].get('files')
    assert not flow.gateway.requests
    frozen = deepcopy(original['qaCreation']['checkpoint']['preparedDocuments']['0'])
    before = len(flow.document.formal.calls)
    await flow.manager.getSession('user-1', 'session-1')
    await flow.manager.getHistory('user-1', 'session-1')
    assert len(flow.document.formal.calls) == before
    for _ in range(4):
        restart(flow)
        assert await flow.manager.recoverPendingCreations() == 1
        await drain(flow)
        waiting = await receipt(flow)
        assert waiting['status'] == 'creating' and waiting['qaCreation']['attempts'] == 0
        assert waiting['qaCreation']['expiresAt'] == original['qaCreation']['expiresAt']
        assert not flow.gateway.requests
    flow.document.formal.status = 'completed'
    flow.document.adapter.understanding.prepare_bundle = AsyncMock(side_effect=AssertionError('no latest'))
    restart(flow)
    await flow.manager.recoverPendingCreations()
    await drain(flow)
    saved = await receipt(flow)
    assert saved['recordDocumentProjections'][0]['prepared'] == frozen
    assert (await flow.manager.getSession('user-1', 'session-1'))['status'] == 'question'
    assert all(value == flow.document.formal.calls[0] for value in flow.document.formal.calls)


@pytest.mark.asyncio
async def test_lost_formal_ack_counts_attempt_but_background_replays_fixed_command():
    flow = environment()
    flow.document.formal.fail_once = True
    first = await create(flow)
    assert first['status'] == 'failed' and first['retryable']
    assert (await receipt(flow))['qaCreation']['attempts'] == 1
    flow.document.adapter.understanding.prepare_bundle = AsyncMock(
        side_effect=AssertionError('no prepare after ACK loss')
    )
    restart(flow)
    await flow.manager.recoverPendingCreations()
    await drain(flow)
    assert (await flow.manager.getSession('user-1', 'session-1'))['status'] == 'question'
    assert flow.document.formal.calls[0] == flow.document.formal.calls[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['failed', 'not_published'])
async def test_terminal_formal_failure_settles_only_attachment_and_keeps_question_flow(status):
    flow = environment()
    flow.document.formal.status = status
    result = await create(flow)
    assert result['status'] == 'question'
    saved = await receipt(flow)
    assert saved['parsedHealthReports'] == [] and saved['documents'] == []
    assert saved['documentProcessing'][0]['status'] == 'unavailable'
    assert saved['documentProcessing'][0]['retryable'] is False
    assert 'sourceRevision' not in saved['documentProcessing'][0]
    assert not flow.document.publisher.calls


@pytest.mark.asyncio
async def test_completed_file_is_not_republished_when_first_question_retries_after_rollback():
    flow = environment()
    fail_initial_runtime_once(flow)
    assert (await create(flow))['status'] == 'failed'
    checkpoint = deepcopy((await receipt(flow))['qaCreation']['checkpoint']['files']['0'])
    flow.manager._record_document_port = flow.document.previous
    flow.document.formal.execute = AsyncMock(side_effect=AssertionError('completed projection must be reused'))
    restart(flow)
    await flow.manager.recoverPendingCreations()
    await drain(flow)
    assert (await flow.manager.getSession('user-1', 'session-1'))['status'] == 'question'
    assert (await receipt(flow))['recordDocumentProjections'][0] == checkpoint['documentProjection']
    flow.document.formal.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_formal_checkpoint_rejects_v1_rollback_before_publication():
    flow = environment()
    flow.document.formal.status = 'pending'
    assert (await create(flow))['status'] == 'processing'
    flow.manager._record_document_port = flow.document.previous
    restart(flow)
    await flow.manager.recoverPendingCreations()
    await drain(flow)
    result = await flow.manager.getSession('user-1', 'session-1')
    assert result['status'] == 'failed' and result['retryable'] is False
    assert not flow.document.publisher.calls


def test_factory_remains_pure_and_selects_existing_v1_exactly():
    doc = document()
    assert doc.adapter.for_consumer_revision(RECORD_DOCUMENT_CHECKPOINT) is doc.previous
    with pytest.raises(RecordDocumentVersionError):
        doc.adapter.for_consumer_revision('future')
    doc.ensure.assert_not_awaited()
    assert not doc.formal.calls


@pytest.mark.parametrize('user_id,session_id', [('u' * 257, 's'), ('u', 's' * 257), (' u ', 's'), ('u', ' s ')])
def test_only_first_acceptance_selects_legacy_for_public_identity_limits(user_id, session_id):
    flow = environment()
    flow.manager._record_profile_source = SimpleNamespace(
        consumer_revision=RECORD_PROFILE_SOURCE_REVISION,
        supports_revision=lambda revision: revision == RECORD_PROFILE_SOURCE_REVISION,
    )
    flow.manager._completion_store = SimpleNamespace(supports_record_profile_sources=True)
    assert (
        flow.manager._recordDocumentConsumerRevision(user_id=user_id, session_id=session_id)
        == LEGACY_RECORD_DOCUMENT_CONSUMER
    )
    assert (
        flow.manager._recordProfileConsumerRevision(user_id=user_id, session_id=session_id)
        == LEGACY_RECORD_PROFILE_REVISION
    )
    accepted = SimpleNamespace(session={'qaCreation': {'profileConsumerRevision': RECORD_PROFILE_SOURCE_REVISION}})
    assert (
        flow.manager._selectRecordProfileConsumerRevision(accepted, user_id=user_id, session_id=session_id)
        == RECORD_PROFILE_SOURCE_REVISION
    )
    accepted.session['qaCreation']['documentConsumerRevision'] = RECORD_FORMAL_DOCUMENT_CHECKPOINT
    accepted.data = {}
    assert (
        flow.manager._selectRecordDocumentPort(accepted, user_id=user_id, session_id=session_id)
        is flow.document.adapter
    )


@pytest.mark.asyncio
async def test_pending_with_another_transport_error_does_not_refund_the_failed_attempt():
    flow = environment()
    flow.document.formal.status = 'pending'
    process = flow.document.adapter.process_attachment

    async def mixed(**kwargs):
        if kwargs['file_index'] == 1:
            raise ConnectionError('second attachment transport failed')
        return await process(**kwargs)

    flow.document.adapter.process_attachment = mixed
    result = await create(flow, count=2)
    assert result['status'] == 'failed' and result['retryable']
    assert (await receipt(flow))['qaCreation']['attempts'] == 1
    assert not flow.gateway.requests


@pytest.mark.asyncio
async def test_cancellation_keeps_reserved_attempt_and_fixed_preparation():
    flow = environment()
    entered, hold = asyncio.Event(), asyncio.Event()
    execute = flow.document.formal.execute

    async def wait(command):
        entered.set()
        await hold.wait()
        return await execute(command)

    flow.document.formal.execute = wait
    flow.manager._creation.response_wait = 0.01
    assert (await create(flow))['status'] == 'processing'
    await entered.wait()
    task = next(iter(flow.manager._creation.tasks.values()))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = await receipt(flow)
    assert saved['qaCreation']['attempts'] == 1
    assert saved['qaCreation']['checkpoint']['preparedDocuments']['0']['formalCommand']
    assert not flow.gateway.requests


@pytest.mark.asyncio
async def test_normal_pending_does_not_extend_creation_ttl_or_run_after_expiry():
    flow = environment()
    flow.document.formal.status = 'pending'
    assert (await create(flow))['status'] == 'processing'
    saved = await receipt(flow)
    flow.redis.now = saved['qaCreation']['expiresAt'] + 1
    calls = len(flow.document.formal.calls)
    assert await flow.manager.recoverPendingCreations() == 0
    await drain(flow)
    assert len(flow.document.formal.calls) == calls


@pytest.mark.asyncio
async def test_terminal_one_file_keeps_other_attachment_and_first_question():
    flow = environment()
    execute = flow.document.formal.execute

    async def per_file(command):
        accepted = await receipt(flow)
        prepared = accepted['qaCreation']['checkpoint']['preparedDocuments']
        index = next(
            key for key, value in prepared.items() if value['formalCommand']['commandId'] == command.command_id
        )
        result = await execute(command)
        if index == '0':
            result.update(status='not_published', errorCode=None, completedBaseCount=1)
            result['result']['basePublications'] = [
                {'kind': 'not_published', 'sourceId': result['result']['basePublications'][0]['sourceId']}
            ]
        return result

    flow.document.formal.execute = per_file
    assert (await create(flow, count=2))['status'] == 'question'
    saved = await receipt(flow)
    assert len(saved['parsedHealthReports']) == len(saved['documents']) == 1
    assert [x['status'] for x in saved['documentProcessing']] == ['unavailable', 'complete']
    assert saved['documents'][0]['fileIndex'] == 1


@pytest.mark.asyncio
async def test_actual_acceptance_persists_long_identity_legacy_before_any_new_document_call():
    flow = environment()
    old = SimpleNamespace(process_attachment=AsyncMock())
    flow.document.previous.legacy_port = old
    flow.manager._record_profile_source = SimpleNamespace(
        consumer_revision=RECORD_PROFILE_SOURCE_REVISION,
        supports_revision=lambda value: value == RECORD_PROFILE_SOURCE_REVISION,
    )
    flow.manager._completion_store = SimpleNamespace(supports_record_profile_sources=True)
    flow.manager._creation.response_wait = 0.01
    run = flow.manager._creation._start
    flow.manager._creation._start = lambda *_: asyncio.create_task(asyncio.sleep(0))
    user_id = 'u' * 300
    result = await flow.manager.createSession(user_id, 'session-long')
    assert result['status'] == 'processing'
    saved = await flow.manager._session_cache.get(user_id, 'session-long')
    assert saved['userId'] == user_id
    assert saved['qaCreation']['profileConsumerRevision'] == LEGACY_RECORD_PROFILE_REVISION
    assert saved['qaCreation']['documentConsumerRevision'] == LEGACY_RECORD_DOCUMENT_CONSUMER
    assert not flow.document.formal.calls
    flow.manager._creation._start = run


@pytest.mark.asyncio
async def test_child_cancellation_with_pending_attachment_does_not_refund_attempt():
    flow = environment()
    flow.document.formal.status = 'pending'
    process = flow.document.adapter.process_attachment

    async def mixed(**kwargs):
        if kwargs['file_index'] == 1:
            raise asyncio.CancelledError()
        return await process(**kwargs)

    flow.document.adapter.process_attachment = mixed
    with pytest.raises(asyncio.CancelledError):
        await create(flow, count=2)
    saved = await receipt(flow)
    assert saved['qaCreation']['attempts'] == 1
    assert saved['qaCreation']['checkpoint']['preparedDocuments']['0']['formalCommand']
    assert not flow.gateway.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'patch',
    [
        {'legacySourceRevision': True},
        {'legacySourceRevision': 0},
        {'legacySourceRevision': 2**53},
        {'ingestionStatus': 'queued'},
        {'jobId': 'fabricated-legacy-job'},
        {'sourceId': 'other-source'},
        {'canonicalReportId': 'other-source'},
    ],
)
async def test_inconsistent_actual_base_receipt_does_not_fallback_or_complete(patch):
    doc = document()
    execute = doc.formal.execute

    async def changed(command):
        result = await execute(command)
        result['result']['basePublications'][0].update(patch)
        return result

    doc.formal.execute = changed
    with pytest.raises(RecordDocumentCheckpointError):
        await doc.adapter.process_attachment(**request(doc))
    assert len(doc.checkpoints) == len(doc.formal.calls) == 1
    assert not doc.publisher.calls
