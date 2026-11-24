"""Immutable original bytes and failed-call materials never resume mutable work."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bson import BSON

from adapters.analysis.document_trace import MongoDocumentTraceStore
from adapters.analysis.materials import create_document_material_reader
from adapters.analysis.snapshots import MongoDocumentSnapshotStore
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.application.documents import DocumentParsingError
from modules.analysis.materials import DocumentMaterialIntegrityError
from modules.analysis.materials import DocumentMaterialUnavailableError
from modules.analysis.materials import DocumentSourceCall
from modules.analysis.snapshots import DocumentScope
from modules.analysis.understanding import UNDERSTANDING_SELECTION
from modules.analysis.understanding import DocumentSnapshotChangedError
from modules.analysis.understanding import DocumentUnderstandingService
from tests.unit._support.document_bson import BsonCollection
from tests.unit.services.analysis.test_document_snapshots import SCOPE
from tests.unit.services.analysis.test_document_workflow import extended_flow


def environment():
    flow = extended_flow()
    traces, chunks, snapshots, snapshot_chunks = (BsonCollection() for _ in range(4))
    trace_store = MongoDocumentTraceStore(traces, chunks)
    flow.service._document_trace_store = trace_store
    store = MongoDocumentSnapshotStore(snapshots=snapshots, chunks=snapshot_chunks)
    reader = create_document_material_reader(
        snapshots=snapshots, snapshot_chunks=snapshot_chunks, traces=traces, trace_chunks=chunks
    )
    api = DocumentUnderstandingService(processing_service=flow.service, snapshots=store)
    return SimpleNamespace(
        flow=flow,
        traces=traces,
        chunks=chunks,
        store=store,
        snapshots=snapshots,
        reader=reader,
        api=api,
        trace_store=trace_store,
    )


async def prepare(env):
    return await env.api.prepare_bundle(
        scope=SCOPE, selection=UNDERSTANDING_SELECTION, file_id='https://example.test/material.pdf'
    )


def prohibit_processing(env):
    env.flow.coordinator.process = AsyncMock(side_effect=AssertionError('must not resume'))
    env.flow.coordinator.index.get = AsyncMock(side_effect=AssertionError('must not read current index'))
    env.flow.downloader.download = AsyncMock(side_effect=AssertionError('must not download'))
    env.flow.interpreter.analyze = AsyncMock(side_effect=AssertionError('must not model'))
    env.flow.service._health_profile_service.ensure_self_profile = AsyncMock(
        side_effect=AssertionError('must not ensure')
    )
    env.flow.source.save = AsyncMock(side_effect=AssertionError('must not publish'))


async def failed(env, version='analysis-health-report-v5'):
    env.flow.interpreter.VERSION = version
    env.flow.interpreter.analyze = AsyncMock(side_effect=DocumentParsingError('synthetic interpretation failure'))
    call = DocumentSourceCall(
        'report',
        'final-1',
        'session-1',
        0,
        None,
        'https://example.test/failed.pdf',
        datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    with pytest.raises(DocumentParsingError) as failure:
        await env.api.prepare_bundle(
            scope=SCOPE,
            selection=UNDERSTANDING_SELECTION,
            file_id=call.file_url,
            source_type=call.entrypoint,
            source_id=call.source_id,
            session_id=call.session_id,
            file_index=call.file_index,
        )
    return call, failure.value


async def capture(env, call, failure):
    return await env.reader.capture_failed_material(scope=SCOPE, call=call, failure=failure)


@pytest.mark.asyncio
async def test_original_read_uses_fixed_ref_with_scope_hash_size_without_index_or_processing():
    env = environment()
    bundle = await prepare(env)
    prohibit_processing(env)
    env.flow.index_collection.documents.clear()
    assert (
        await env.reader.read_original(scope=SCOPE, material_snapshot_ref=bundle.material.snapshot_id)
        == b'%PDF-1.7\nsynthetic'
    )
    assert (
        await env.reader.read_material(scope=SCOPE, material_snapshot_ref=bundle.material.snapshot_id)
        == bundle.material
    )
    assert not env.flow.collection.documents and not env.flow.jobs.jobs
    assert len(env.flow.client.submit_calls) == len(env.flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_original_ref_rewrite_or_cross_scope_never_exposes_other_bytes():
    env = environment()
    bundle = await prepare(env)
    for scope in (DocumentScope('other-user', SCOPE.subject_id), DocumentScope(SCOPE.user_id, 'other-subject')):
        with pytest.raises(DocumentNotFoundError):
            await env.reader.read_original(scope=scope, material_snapshot_ref=bundle.material.snapshot_id)
    ref = bundle.material.payload['originalArtifactRef']
    await env.trace_store.write_payload(ref, SCOPE.user_id, 'original_file', {'content': b'different bytes'})
    with pytest.raises(DocumentMaterialIntegrityError):
        await env.reader.read_original(scope=SCOPE, material_snapshot_ref=bundle.material.snapshot_id)


@pytest.mark.asyncio
async def test_original_trace_source_identity_cannot_be_substituted_even_with_identical_content():
    env = environment()
    bundle = await prepare(env)
    env.traces.documents[bundle.material.payload['originalArtifactRef']]['canonicalReportId'] = 'other-source'
    with pytest.raises(DocumentMaterialIntegrityError):
        await env.reader.read_original(scope=SCOPE, material_snapshot_ref=bundle.material.snapshot_id)


@pytest.mark.asyncio
async def test_material_only_failure_freezes_actual_version_and_survives_later_index_recovery():
    env = environment()
    call, failure = await failed(env)
    trace = env.traces.documents[failure.trace_id]
    assert trace['failureMaterialBinding']['interpretationRevision'] == 'analysis-health-report-v5'
    document = env.flow.index_collection.documents[failure.document_id]
    document.update(interpretationVersion='analysis-health-report-v99', interpretationRound=80, status='completed')
    prohibit_processing(env)
    material = await capture(env, call, failure)
    assert material.payload['interpretationRevision'] == 'analysis-health-report-v5'
    assert material.payload['validationRevision'] is None
    assert material.payload['failureTrace']['call'] == call.as_dict()
    assert material.payload['parsedDocument']['text']
    assert material.payload['documentMetadata']['status'] == 'failed'
    assert len(env.snapshots.documents) == 1  # No Facts or understanding success is required.
    assert (
        await env.reader.read_original(scope=SCOPE, material_snapshot_ref=material.snapshot_id)
        == b'%PDF-1.7\nsynthetic'
    )
    assert not env.flow.collection.documents and not env.flow.jobs.jobs
    assert len(env.flow.client.submit_calls) == 1
    env.traces.documents[failure.trace_id]['failureMaterialBinding'] = None
    assert await env.reader.read_material(scope=SCOPE, material_snapshot_ref=material.snapshot_id) == material


@pytest.mark.asyncio
async def test_document_provider_does_not_apply_report_legacy_version_policy():
    env = environment()
    call, failure = await failed(env, version='analysis-health-report-v12')
    material = await capture(env, call, failure)
    assert material.payload['interpretationRevision'] == 'analysis-health-report-v12'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'field,value',
    [
        ('entrypoint', 'chat'),
        ('source_id', 'other'),
        ('session_id', 'other'),
        ('file_index', 1),
        ('file_url', 'https://example.test/other.pdf'),
        ('requested_document_id', 'other'),
        ('trace_id', 'other'),
    ],
)
async def test_failed_material_requires_the_complete_call_identity(field, value):
    env = environment()
    call, failure = await failed(env)
    assert await capture(env, replace(call, **{field: value}), failure) is None
    assert not env.snapshots.documents


@pytest.mark.asyncio
async def test_ambiguous_or_older_failed_calls_are_not_selected_by_latest():
    env = environment()
    call, failure = await failed(env)
    assert (
        await capture(env, replace(call, started_at=datetime.now(timezone.utc) + timedelta(seconds=1)), failure) is None
    )
    duplicate = deepcopy(env.traces.documents[failure.trace_id])
    duplicate['_id'] = duplicate['traceId'] = 'duplicate'
    env.traces.documents['duplicate'] = duplicate
    assert await capture(env, call, failure) is None
    # A known exact trace may still be selected with every other identity check.
    assert await capture(env, replace(call, trace_id=failure.trace_id), failure) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [PermissionError('denied'), RuntimeError('transport'), DocumentParsingError('parse')])
async def test_permissions_and_non_content_failure_chains_do_not_authorize_capture(error):
    env = environment()
    call, failure = await failed(env)
    if isinstance(error, DocumentParsingError):
        error.content_failure = False
    if type(error) is not RuntimeError:
        error.__cause__ = failure
    assert await capture(env, call, error) is None
    assert not env.snapshots.documents


@pytest.mark.asyncio
async def test_wrapped_parsing_failure_keeps_old_boundary_but_http_permission_and_wrong_trace_are_rejected():
    env = environment()
    call, failure = await failed(env)
    wrapper = RuntimeError('upstream wrapper')
    wrapper.__cause__ = failure
    assert await capture(env, call, wrapper) is not None
    wrapper.status_code = 403
    assert await capture(env, call, wrapper) is None
    failure.trace_id = 'another-attempt'
    assert await capture(env, call, failure) is None


@pytest.mark.asyncio
async def test_unbound_legacy_trace_is_not_backfilled_from_current_metadata():
    env = environment()
    call, failure = await failed(env)
    env.traces.documents[failure.trace_id].pop('failureMaterialBinding')
    prohibit_processing(env)
    with pytest.raises(DocumentMaterialUnavailableError, match='no fixed'):
        await capture(env, call, failure)


@pytest.mark.asyncio
async def test_same_ref_parser_rewrite_after_failure_is_rejected_without_model_recovery():
    env = environment()
    call, failure = await failed(env)
    binding = env.traces.documents[failure.trace_id]['failureMaterialBinding']
    await env.trace_store.write_payload(
        binding['parseArtifactRef'], SCOPE.user_id, 'docmind_result', {'success': True, 'text': 'replacement'}
    )
    prohibit_processing(env)
    with pytest.raises(DocumentMaterialIntegrityError, match='parser'):
        await capture(env, call, failure)


@pytest.mark.asyncio
async def test_capture_rejects_a_changed_failure_binding_during_archive_reads():
    env = environment()
    call, failure = await failed(env)
    original = env.reader.archive.read_payload

    async def changed(*args):
        result = await original(*args)
        env.traces.documents[failure.trace_id]['failureMaterialBinding']['interpretationRevision'] = 'new'
        return result

    env.reader.archive.read_payload = changed
    with pytest.raises(DocumentSnapshotChangedError):
        await capture(env, call, failure)
    assert not env.snapshots.documents


def test_factory_and_reader_require_no_io_or_lazy_ddl():
    env = environment()
    assert not env.traces.indexes and not env.snapshots.indexes


@pytest.mark.asyncio
async def test_old_material_without_fixed_size_and_wrong_contract_are_explicitly_unavailable():
    env = environment()
    bundle = await prepare(env)
    old_material = await env.api.capture_material(scope=SCOPE, document_id=bundle.receipt.document_id)
    prohibit_processing(env)
    with pytest.raises(DocumentMaterialUnavailableError):
        await env.reader.read_original(scope=SCOPE, material_snapshot_ref=old_material.snapshot_id)
    with pytest.raises(ValueError, match='material snapshot'):
        await env.reader.read_original(scope=SCOPE, material_snapshot_ref=bundle.facts.snapshot_id)


@pytest.mark.asyncio
async def test_missing_original_archive_never_selects_current_index_or_reprocesses():
    env = environment()
    bundle = await prepare(env)
    env.traces.documents.pop(bundle.material.payload['originalArtifactRef'])
    prohibit_processing(env)
    with pytest.raises(DocumentNotFoundError):
        await env.reader.read_original(scope=SCOPE, material_snapshot_ref=bundle.material.snapshot_id)


@pytest.mark.asyncio
async def test_millisecond_bson_precision_accepts_same_call_without_matching_the_previous_millisecond():
    env = environment()
    call, failure = await failed(env)
    created = datetime(2026, 11, 10, 12, 0, 0, 123900, tzinfo=timezone.utc)
    started = created.replace(microsecond=123100)
    env.traces.documents[failure.trace_id]['createdAt'] = BSON.encode({'date': created}).decode()['date']
    call = replace(call, started_at=started)
    assert await capture(env, call, failure) is not None
    env.traces.documents[failure.trace_id]['createdAt'] = created.replace(microsecond=122000, tzinfo=None)
    assert await capture(env, call, failure) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['trace_id', 'document_id', None])
async def test_same_type_error_wrapper_cannot_hide_inner_failed_call_identity(field):
    env = environment()
    call, failure = await failed(env)
    outer = DocumentParsingError('outer parsing wrapper')
    outer.__cause__ = failure
    if field is not None:
        setattr(failure, field, 'another-call')
        assert await capture(env, call, outer) is None
        assert not env.snapshots.documents
    else:
        assert await capture(env, call, outer) is not None


@pytest.mark.asyncio
async def test_failed_polling_original_rejects_foreign_call_scope_and_changed_bytes(monkeypatch):
    from dataclasses import replace

    from tests.unit._support.current_document import actual_analysis_preparation

    flow = await actual_analysis_preparation([])
    reader = create_document_material_reader(
        snapshots=flow.snapshots,
        snapshot_chunks=flow.snapshot_chunks,
        traces=flow.traces,
        trace_chunks=flow.trace_chunks,
    )

    def polling_failure(*args, **kwargs):
        raise TimeoutError('synthetic OCR polling failure')

    monkeypatch.setattr(flow.upstream.client, 'get_result', polling_failure)
    scope = flow.scope
    url = 'https://example.test/failed-polling.pdf'
    started_at = datetime.now(timezone.utc)
    with pytest.raises(DocumentParsingError) as failure:
        await flow.prepared.prepare_bundle(
            scope=scope,
            file_id=url,
            file_type='pdf',
            source_type='report',
            source_id='report-batch',
            session_id='report-session',
            file_index=10,
        )
    error = failure.value
    call = DocumentSourceCall('report', 'report-batch', 'report-session', 10, None, url, started_at, error.trace_id)
    for changed in (
        replace(call, source_id='other-report'),
        replace(call, file_index=9),
        replace(call, file_url='https://example.test/other.pdf'),
        replace(call, trace_id=None),
        replace(call, started_at=started_at + timedelta(seconds=1)),
    ):
        assert await reader.capture_failed_original(scope=scope, call=changed, failure=error) is None
    for changed in (DocumentScope('other-user', scope.subject_id), DocumentScope(scope.user_id, 'other-subject')):
        assert await reader.capture_failed_original(scope=changed, call=call, failure=error) is None
    assert not flow.snapshots.documents

    archived = await reader.capture_failed_original(scope=scope, call=call, failure=error)
    assert archived.payload['failureTrace']['stage'] == 'polling'
    assert 'parsedDocument' not in archived.payload
    assert await reader.read_original(scope=scope, material_snapshot_ref=archived.snapshot_id) == (
        flow.upstream.downloader.download.return_value
    )
    await flow.analysis._document_trace_store.write_payload(
        error.trace_id, scope.user_id, 'original_file', {'content': b'original bytes changed'}
    )
    with pytest.raises(DocumentMaterialIntegrityError):
        await reader.capture_failed_original(scope=scope, call=call, failure=error)
