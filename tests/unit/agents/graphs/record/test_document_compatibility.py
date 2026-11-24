"""Record creation plus real public Document commands and BSON persistence."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from adapters.record.document_compatibility import create_prepared_record_document_port
from adapters.record.documents import LegacyRecordDocumentAdapter
from modules.record.application.creation import failure_info
from modules.record.documents import RecordDocumentCheckpointError
from tests.unit.services.analysis.test_document_compatible_publication import environment
from tests.unit.services.analysis.test_document_compatible_publication import prohibit_processing
from tests.unit.services.analysis.test_document_compatible_publication import restart as restart_document

from .test_creation_recovery import creation_manager
from .test_creation_recovery import drain
from .test_creation_recovery import receipt
from .test_prepared_document_creation import create
from .test_prepared_document_creation import restart as restart_record


def composed(*, legacy=False):
    document = environment()
    adapter = (
        LegacyRecordDocumentAdapter(processor=document.flow.service)
        if legacy
        else create_prepared_record_document_port(document.api)
    )
    flow = creation_manager(record_document_port=adapter)
    flow.document = document
    flow.manager._loadHealthProfileContext = AsyncMock(return_value='original profile')
    return flow


def request(**kwargs):
    return {
        'user_id': 'user-1',
        'session_id': 'session-1',
        'file_index': 0,
        'attachment': {'url': 'https://example.test/record.pdf', 'fileType': 'pdf'},
        'settled_document_ids': set(),
        'on_prepared': AsyncMock(),
        **kwargs,
    }


def test_public_record_factory_is_pure_composition():
    document = environment()
    adapter = create_prepared_record_document_port(document.api)
    assert adapter.prepared_checkpoint_version == 'record-document-preparation.v1'
    assert not document.records.documents and not document.snapshots.documents
    document.flow.service._health_profile_service.ensure_self_profile.assert_not_awaited()
    assert not document.flow.client.submit_calls and not document.flow.graph.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('lost_at', ['sink', 'receipt'])
async def test_real_publication_ack_loss_recovers_original_record_checkpoint_without_processing(lost_at):
    flow = composed()
    doc = flow.document
    owner = doc.flow.source if lost_at == 'sink' else doc.api.publications
    method = 'save' if lost_at == 'sink' else 'put_publication'
    original = getattr(owner, method)

    async def lost(*args, **kwargs):
        saved = await receipt(flow)
        prepared = saved['qaCreation']['checkpoint']['preparedDocuments']['0']
        assert prepared['preparationReceipt']['reused'] is False
        assert any(row['kind'] == 'preparation' for row in doc.records.documents.values())
        await original(*args, **kwargs)
        raise ConnectionError('published but ACK lost')

    setattr(owner, method, lost)
    result = await create(flow)
    assert result['status'] == 'failed' and result['retryable']
    failed = await receipt(flow)
    prepared = deepcopy(failed['qaCreation']['checkpoint']['preparedDocuments']['0'])
    assert not failed['qaCreation']['checkpoint'].get('files')
    assert len(doc.flow.jobs.jobs) == 1 and not flow.gateway.requests
    setattr(owner, method, original)
    prohibit_processing(doc)
    doc.api = restart_document(doc)
    doc.api.prepare_bundle = AsyncMock(side_effect=AssertionError('must restore fixed preparation'))
    doc.api.ensure_self_scope = AsyncMock(side_effect=AssertionError('must keep frozen scope'))
    if lost_at == 'receipt':
        doc.api.legacy_publication.publish_document = AsyncMock(side_effect=AssertionError('must reuse receipt'))
    flow.manager._record_document_port = create_prepared_record_document_port(doc.api)
    restart_record(flow)
    assert await flow.manager.recoverPendingCreations() == 1
    await drain(flow)
    saved = await receipt(flow)
    assert (await flow.manager.getSession('user-1', 'session-1'))['status'] == 'question'
    assert saved['recordDocumentProjections'][0]['prepared'] == prepared
    assert saved['documents'][0]['reused'] is False
    assert len(doc.flow.jobs.jobs) == len(doc.flow.collection.documents) == 1
    assert len(doc.flow.graph.calls) == len(doc.flow.client.submit_calls) == 1
    assert len(flow.gateway.requests) == 2
    doc.api.prepare_bundle.assert_not_awaited()
    doc.api.ensure_self_scope.assert_not_awaited()


@pytest.mark.asyncio
async def test_bson_supplemental_revision_ack_replay_preserves_actual_identity_and_original_receipt():
    doc = environment()
    adapter = create_prepared_record_document_port(doc.api)
    base = await adapter.process_attachment(**request())
    document = doc.flow.index_collection.documents[base.prepared.document_id]
    raw = doc.flow.traces.payloads[(document['payloadRefs']['interpretation'], 'interpretation')]
    raw['observations'][0]['resultValue'] = '777'
    document['interpretationVersion'] = 'fixed-before-upgrade'
    checkpoint = AsyncMock()
    original = doc.flow.source.save_revision

    async def lost(**kwargs):
        await original(**kwargs)
        raise ConnectionError('supplement ACK lost')

    doc.flow.source.save_revision = lost
    with pytest.raises(ConnectionError, match='ACK'):
        await adapter.process_attachment(
            **request(attachment={'documentId': base.prepared.document_id}, on_prepared=checkpoint)
        )
    prepared = checkpoint.await_args.args[0]
    assert prepared.preparation_reused and prepared.preparation_trace_id != base.prepared.preparation_trace_id
    raw['observations'][0]['resultValue'] = '999'
    document['interpretationVersion'] = 'future-deployment'
    doc.flow.source.save_revision = original
    prohibit_processing(doc)
    doc.api = restart_document(doc)
    adapter = create_prepared_record_document_port(doc.api)
    restored = await adapter.process_attachment(
        **request(attachment={'documentId': prepared.document_id}, prepared_checkpoint=prepared.to_checkpoint())
    )
    assert restored.prepared == prepared
    assert restored.publication.legacy_source_revision > 1
    assert type(restored.publication.legacy_source_revision) is int
    assert restored.report['sourceRevision'] == restored.publication.legacy_source_revision
    assert restored.report['observations'][0]['resultValue'] == '777'
    assert len(doc.flow.reports._revisions.documents) == 1
    assert len(doc.flow.jobs.jobs) == 2
    assert len(doc.flow.client.submit_calls) == len(doc.flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_public_provider_binding_conflict_is_terminal_in_record_creation():
    flow = composed()
    doc = flow.document
    publish = doc.api.publish_prepared

    async def conflict(**kwargs):
        for row in doc.records.documents.values():
            if row['kind'] == 'preparation':
                row['payload']['interpretationContentDigest'] = 'conflicting-durable-binding'
        return await publish(**kwargs)

    doc.api.publish_prepared = conflict
    result = await create(flow)
    assert result['status'] == 'failed' and result['retryable'] is False
    assert result['errorCode'] == 'CREATION_INPUT_INVALID'
    assert not doc.flow.collection.documents and not doc.flow.jobs.jobs
    assert not flow.gateway.requests


@pytest.mark.asyncio
async def test_publication_receipt_must_echo_original_preparation_not_current_attempt():
    doc = environment()
    publish = doc.api.publish_prepared

    async def changed(**kwargs):
        actual = await publish(**kwargs)
        return replace(actual, preparation_receipt=replace(actual.preparation_receipt, reused=True))

    doc.api.publish_prepared = changed
    with pytest.raises(RecordDocumentCheckpointError, match='different preparation') as failure:
        await create_prepared_record_document_port(doc.api).process_attachment(**request())
    assert not failure_info(failure.value)['retryable']
    assert len(doc.flow.jobs.jobs) == 1


@pytest.mark.asyncio
async def test_public_provider_read_projection_has_no_publication_or_preparation_side_effects():
    doc = environment()
    adapter = create_prepared_record_document_port(doc.api)
    prepared = await adapter.process_attachment(**request())
    prohibit_processing(doc)
    for method in ('ensure_self_scope', 'prepare_bundle', 'publish_prepared'):
        setattr(doc.api, method, AsyncMock(side_effect=AssertionError('pure projection read')))
    restored = await adapter.read_projection(prepared=prepared.prepared, publication=prepared.publication)
    assert restored == prepared
    assert len(doc.flow.jobs.jobs) == 1


@pytest.mark.asyncio
async def test_actual_first_question_model_inputs_match_legacy_with_no_extra_model_calls():
    legacy, current = composed(legacy=True), composed()
    # Compare the same immutable source, preserving its actual report identity
    # and the derived dialogue refs instead of discarding them from assertions.
    current.document = legacy.document
    current.manager._record_document_port = create_prepared_record_document_port(legacy.document.api)
    old_result, new_result = await create(legacy), await create(current)
    assert old_result['status'] == new_result['status'] == 'question'
    assert [(r.profile.name, r.content) for r in legacy.gateway.requests] == [
        (r.profile.name, r.content) for r in current.gateway.requests
    ]
    assert len(legacy.gateway.requests) == len(current.gateway.requests) == 2
    for flow in (legacy, current):
        doc = flow.document
        assert len(doc.flow.client.submit_calls) == len(doc.flow.graph.calls) == 1
        assert len(doc.flow.jobs.jobs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('historic_without_revision', [False, True])
async def test_accepted_legacy_attachment_keeps_legacy_path_after_default_changes(historic_without_revision):
    flow = composed(legacy=True)
    legacy = flow.manager._record_document_port
    process = legacy.process_attachment
    legacy.process_attachment = AsyncMock(side_effect=ConnectionError('old request interrupted'))
    assert (await create(flow))['status'] == 'failed'
    saved = await receipt(flow)
    assert saved['qaCreation']['documentConsumerRevision'] == 'legacy-record-document.v1'
    if historic_without_revision:
        saved['qaCreation'].pop('documentConsumerRevision')
        await flow.manager._session_cache.save('user-1', 'session-1', saved, ttl_seconds=7200)
    legacy.process_attachment = process
    doc = flow.document
    doc.api.prepare_bundle = AsyncMock(side_effect=AssertionError('old accepted task must stay legacy'))
    flow.manager._record_document_port = create_prepared_record_document_port(doc.api, legacy_port=legacy)
    restart_record(flow)
    assert await flow.manager.recoverPendingCreations() == 1
    await drain(flow)
    saved = await receipt(flow)
    assert (await flow.manager.getSession('user-1', 'session-1'))['status'] == 'question'
    assert not saved.get('recordDocumentProjections')
    assert not doc.records.documents and not doc.snapshots.documents
    assert len(doc.flow.jobs.jobs) == len(doc.flow.graph.calls) == 1
    doc.api.prepare_bundle.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_accepted_legacy_consumer_cannot_silently_migrate_to_prepared():
    flow = composed(legacy=True)
    flow.manager._record_document_port.process_attachment = AsyncMock(side_effect=ConnectionError('interrupted'))
    assert (await create(flow))['status'] == 'failed'
    doc = flow.document
    flow.manager._record_document_port = create_prepared_record_document_port(doc.api)
    restart_record(flow)
    assert await flow.manager.recoverPendingCreations() == 1
    await drain(flow)
    current = await flow.manager.getSession('user-1', 'session-1')
    assert current['status'] == 'failed' and current['retryable'] is False
    assert not doc.records.documents and not doc.flow.graph.calls


@pytest.mark.asyncio
async def test_accepted_prepared_task_cannot_roll_back_even_before_first_preparation():
    flow = composed()
    doc = flow.document
    doc.api.ensure_self_scope = AsyncMock(side_effect=ConnectionError('scope command interrupted'))
    assert (await create(flow))['status'] == 'failed'
    saved = await receipt(flow)
    assert saved['qaCreation']['documentConsumerRevision'] == 'record-document-preparation.v1'
    assert not saved['qaCreation']['checkpoint'].get('preparedDocuments')
    legacy = LegacyRecordDocumentAdapter(processor=doc.flow.service)
    legacy.process_attachment = AsyncMock(side_effect=AssertionError('must not downgrade'))
    flow.manager._record_document_port = legacy
    restart_record(flow)
    assert await flow.manager.recoverPendingCreations() == 1
    await drain(flow)
    current = await flow.manager.getSession('user-1', 'session-1')
    assert current['status'] == 'failed' and current['retryable'] is False
    legacy.process_attachment.assert_not_awaited()
    assert not doc.records.documents and not doc.flow.graph.calls
