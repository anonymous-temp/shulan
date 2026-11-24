"""The explicit legacy command preserves old identities without mutable rereads."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.application.documents import DocumentParsingError
from modules.analysis.application.output_profiles import DocumentOutputProfiles
from modules.analysis.compatibility import DocumentPreparationConflictError
from modules.analysis.contracts import OutputSelection
from modules.analysis.contracts import UnsupportedOutputVersionError
from modules.analysis.materials import DocumentMaterialIntegrityError
from modules.analysis.publication import DocumentPublisher
from modules.analysis.report_publication_contracts import LEGACY_REPORT_SOURCE_SELECTION
from modules.analysis.report_publication_contracts import DocumentPreparedPublicationInput
from modules.analysis.report_publication_contracts import DocumentReportPublicationManifest
from modules.analysis.snapshots import DocumentScope
from modules.analysis.understanding import UNDERSTANDING_SELECTION
from tests.unit._support.document_profile_composition.analysis.document_report_publication import (
    environment as environment,
)
from tests.unit._support.document_profile_composition.analysis.document_report_publication import restart as restart
from tests.unit.services.analysis.test_document_snapshots import SCOPE
from tests.unit.services.analysis.test_output_selection import legacy_cache_with_deployment_version


async def prepare(env, **kwargs):
    bundle = await env.api.prepare_bundle(
        scope=SCOPE,
        selection=UNDERSTANDING_SELECTION,
        **(kwargs or {'file_id': 'https://example.test/report-publication.pdf'}),
    )
    item = DocumentPreparedPublicationInput(
        bundle.material.snapshot_id,
        bundle.facts.snapshot_id,
        bundle.understanding.snapshot_id,
        UNDERSTANDING_SELECTION,
        bundle.receipt,
    )
    return bundle, item


async def publish(env, *items, **kwargs):
    return await env.publisher.publish_report_source(
        **{'scope': SCOPE, 'inputs': tuple(items), 'selection': LEGACY_REPORT_SOURCE_SELECTION, **kwargs}
    )


def prohibit_reprocessing(env):
    env.flow.coordinator.process = AsyncMock(side_effect=AssertionError('must not resume Document processing'))
    env.flow.index.get = AsyncMock(side_effect=AssertionError('must not read the mutable Document index'))
    env.flow.service.read_document_payload = AsyncMock(side_effect=AssertionError('must use fixed originals'))
    env.flow.interpreter.analyze = AsyncMock(side_effect=AssertionError('must not invoke a model'))
    env.flow.downloader.download = AsyncMock(side_effect=AssertionError('must not download'))


@pytest.mark.asyncio
async def test_command_executes_real_legacy_v16_from_fixed_material_and_replay_is_pure():
    from modules.report.document_source import extract_intrinsic_report_source

    env = environment(extractor=extract_intrinsic_report_source)
    bundle, item = await prepare(env)
    assert not env.flow.collection.documents
    prohibit_reprocessing(env)
    receipt = await publish(env, item)
    assert receipt.processing_status == 'complete' and receipt.legacy_source_revision > 1
    assert len(env.flow.jobs.jobs) == 2
    projection = await env.publisher.read_projection(
        scope=SCOPE, projection_ref=receipt.projection_ref, selection=LEGACY_REPORT_SOURCE_SELECTION
    )
    assert projection.payload['projection']['sourceChecksum'] == bundle.material.payload['contentSha256']
    assert projection.payload['projection']['supplementaryHospitalData']
    facts_publisher = DocumentPublisher(snapshots=env.publisher.snapshots, ledger=object(), receiver=object())
    for ref in (receipt.projection_ref, projection.payload['executionSnapshotRef']):
        with pytest.raises(ValueError, match='fixed facts'):
            await facts_publisher.publish(
                scope=SCOPE, snapshot_id=ref, request_id='never-send', expected_prior_revision=None
            )
    env.publisher = restart(env)
    env.publisher.coordinator.process = AsyncMock(side_effect=AssertionError('receipt replay is pure'))
    env.publisher.coordinator.profiles = DocumentOutputProfiles()
    env.api.publish_prepared = AsyncMock(side_effect=AssertionError('receipt replay is pure'))
    env.materials.read_original = AsyncMock(side_effect=AssertionError('receipt read needs no original bytes'))
    assert await publish(env, item) == receipt
    assert await env.publisher.read_publication(scope=SCOPE, receipt_ref=receipt.receipt_ref) == receipt
    assert len(env.flow.client.submit_calls) == len(env.flow.graph.calls) == 1
    with pytest.raises(DocumentNotFoundError):
        await env.publisher.read_publication(
            scope=DocumentScope('other', SCOPE.subject_id), receipt_ref=receipt.receipt_ref
        )


@pytest.mark.asyncio
async def test_old_output_cache_identity_and_legacy_mislabel_are_reused_without_extra_publication():
    env = environment()
    old = await env.flow.service.process_document(
        user_id=SCOPE.user_id,
        subject_id=SCOPE.subject_id,
        content=b'%PDF-1.7 old-report',
        file_name='old.pdf',
        output_selection=LEGACY_REPORT_SOURCE_SELECTION,
    )
    legacy = await legacy_cache_with_deployment_version(env.flow, old, 'mislabeled-deployment')
    bundle, item = await prepare(env, document_id=old.document_id)
    jobs = len(env.flow.jobs.jobs)
    prohibit_reprocessing(env)
    receipt = await publish(env, item)
    projection = await env.publisher.read_projection(
        scope=SCOPE, projection_ref=receipt.projection_ref, selection=LEGACY_REPORT_SOURCE_SELECTION
    )
    assert projection.payload['extractionId'] == legacy['_id']
    trace = env.traces.documents[projection.payload['outputTraceId']]
    assert trace['extractionId'] == projection.payload['extractionId']
    assert trace['traceId'] != old.output_trace_id  # The cache lookup has its own actual audit trace.
    assert receipt.legacy_source_revision == legacy['sourceRevision']
    assert len(env.flow.jobs.jobs) == jobs and env.flow.extractor.await_count == 1


@pytest.mark.asyncio
async def test_ordered_group_keeps_all_receipts_but_executes_first_document_occurrence_only():
    env = environment()
    a, ia = await prepare(env, content=b'%PDF-1.7 first', file_name='a.pdf')
    b, ib = await prepare(env, content=b'%PDF-1.7 second', file_name='b.pdf')
    again, ic = await prepare(env, document_id=a.receipt.document_id)
    old = await env.flow.service.process_documents(
        [{'documentId': item.preparation_receipt.document_id} for item in (ia, ib, ic)],
        user_id=SCOPE.user_id,
        subject_id=SCOPE.subject_id,
        output_selection=LEGACY_REPORT_SOURCE_SELECTION,
    )
    jobs = len(env.flow.jobs.jobs)
    prohibit_reprocessing(env)
    receipt = await publish(env, ia, ib, ic)
    projection = await env.publisher.read_projection(
        scope=SCOPE, projection_ref=receipt.projection_ref, selection=LEGACY_REPORT_SOURCE_SELECTION
    )
    assert receipt.manifest.inputs == (ia, ib, ic) and len(receipt.base_publications) == 3
    assert projection.payload['extractionId'] == old.extraction_id
    trace = env.traces.documents[projection.payload['outputTraceId']]
    assert trace['extractionId'] == projection.payload['extractionId']
    assert trace['traceId'] != old.output_trace_id  # The cache lookup has its own actual audit trace.
    assert receipt.legacy_source_revision == old.source_revision and len(env.flow.jobs.jobs) == jobs
    assert [item.document_id for item in env.flow.extractor.call_args.args[0]] == [
        a.receipt.document_id,
        b.receipt.document_id,
    ]
    assert env.flow.extractor.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper', ['refs', 'receipt', 'scope', 'version', 'original'])
async def test_all_inputs_are_validated_before_any_formal_source_write(tamper):
    env = environment()
    first, a = await prepare(env, content=b'%PDF-1.7 a', file_name='a.pdf')
    second, b = await prepare(env, content=b'%PDF-1.7 b', file_name='b.pdf')
    kwargs = {}
    if tamper == 'refs':
        b = replace(b, facts_snapshot_ref=first.facts.snapshot_id)
    elif tamper == 'receipt':
        b = replace(b, preparation_receipt=replace(b.preparation_receipt, trace_id='never-prepared', reused=True))
    elif tamper == 'scope':
        kwargs['scope'] = DocumentScope('other', SCOPE.subject_id)
    elif tamper == 'version':
        kwargs['selection'] = OutputSelection('report_source', '1', 'report-source-v17')
    else:
        owner = second.material.payload['originalArtifactRef']
        await env.flow.service._document_trace_store.write_payload(
            owner, SCOPE.user_id, 'original_file', {'content': b'wrong'}
        )
    prohibit_reprocessing(env)
    with pytest.raises(
        (
            DocumentNotFoundError,
            DocumentPreparationConflictError,
            UnsupportedOutputVersionError,
            DocumentMaterialIntegrityError,
            RuntimeError,
        )
    ):
        await publish(env, a, b, **kwargs)
    assert not env.flow.collection.documents and not env.flow.jobs.jobs and not env.commands.documents


@pytest.mark.asyncio
async def test_partial_inputs_preserve_base_effect_but_never_publish_extra_report_source():
    env = environment()
    base, _ = await prepare(env)
    doc = env.flow.index_collection.documents[base.receipt.document_id]
    trace = env.flow.service._document_trace_store
    raw = await trace.read_payload(
        doc['payloadRefs']['interpretation'], SCOPE.user_id, SCOPE.subject_id, 'interpretation'
    )
    raw['processing']['status'] = 'partial'
    # Model a new stored partial interpretation, before its immutable preparation.
    new_ref = 'partial-interpretation'
    await trace.create({'_id': new_ref, 'userId': SCOPE.user_id, 'subjectId': SCOPE.subject_id})
    await trace.write_payload(new_ref, SCOPE.user_id, 'interpretation', raw)
    doc['payloadRefs']['interpretation'] = new_ref
    bundle, item = await prepare(env, document_id=base.receipt.document_id, resume_incomplete=False)
    prohibit_reprocessing(env)
    receipt = await publish(env, item)
    assert receipt.processing_status == 'partial'
    assert receipt.legacy_source_revision is None and receipt.ingestion_status == 'not_submitted'
    assert len(env.flow.collection.documents) == len(env.flow.jobs.jobs) == 1
    assert not env.flow.reports._revisions.documents


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['execution', 'sink', 'trace', 'receipt'])
async def test_lost_ack_replays_fixed_inputs_and_preserves_real_legacy_revision(stage):
    env = environment()
    _, item = await prepare(env)
    if stage == 'execution':
        owner, name = env.publisher.commands, 'bind_execution'
    elif stage == 'sink':
        owner, name = env.flow.source, 'save_revision'
    elif stage == 'trace':
        owner, name = env.publisher.commands, 'bind_output_trace'
    else:
        owner, name = env.publisher.commands, 'put_receipt'
    original = getattr(owner, name)

    async def lost(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError('ACK lost')

    setattr(owner, name, lost)
    with pytest.raises(RuntimeError, match='ACK lost'):
        await publish(env, item)
    setattr(owner, name, original)
    prohibit_reprocessing(env)
    env.publisher = restart(env)
    env.publisher.read_legacy_revision = AsyncMock(side_effect=AssertionError('fixed execution is already saved'))
    receipt = await publish(env, item)
    assert receipt.legacy_source_revision > 1 and len(env.flow.jobs.jobs) == 2
    assert len(env.flow.reports._revisions.documents) == 1
    assert env.flow.extractor.await_count == 1
    if stage == 'sink':
        state = await env.ledger.read_source(SCOPE, receipt.canonical_report_id)
        assert state['legacyTickets']  # Unknown old writer is not declared quiescent.


@pytest.mark.asyncio
async def test_concurrent_identical_commands_share_one_execution_and_formal_revision():
    env = environment()
    _, item = await prepare(env)
    receipts = await asyncio.gather(*(publish(env, item) for _ in range(4)))
    assert all(receipt == receipts[0] for receipt in receipts)
    assert env.flow.extractor.await_count == 1 and len(env.flow.jobs.jobs) == 2


@pytest.mark.asyncio
async def test_unavailable_exact_extractor_fails_before_base_publication_and_never_uses_current_alias():
    env = environment()
    _, item = await prepare(env)
    env.publisher.coordinator.profiles = DocumentOutputProfiles()
    with pytest.raises(UnsupportedOutputVersionError):
        await publish(env, item)
    assert not env.flow.collection.documents and not env.commands.documents


@pytest.mark.asyncio
async def test_different_preparation_traces_keep_command_identity_separate_from_old_publication_identity():
    env = environment()
    bundle, a = await prepare(env)
    again, b = await prepare(env, document_id=bundle.receipt.document_id)
    assert a.understanding_snapshot_ref == b.understanding_snapshot_ref
    prohibit_reprocessing(env)
    first, second = await asyncio.gather(publish(env, a), publish(env, b))
    assert first.receipt_ref != second.receipt_ref
    assert first.legacy_source_revision == second.legacy_source_revision
    assert env.flow.extractor.await_count == 1 and len(env.flow.jobs.jobs) == 2


@pytest.mark.asyncio
async def test_same_document_with_different_frozen_understanding_is_rejected_before_base_publication():
    env = environment()
    bundle, first = await prepare(env)
    document = env.flow.index_collection.documents[bundle.receipt.document_id]
    trace = env.flow.service._document_trace_store
    raw = await trace.read_payload(
        document['payloadRefs']['interpretation'], SCOPE.user_id, SCOPE.subject_id, 'interpretation'
    )
    raw['aiSummary'] = 'another frozen interpretation'
    await trace.write_payload(document['payloadRefs']['interpretation'], SCOPE.user_id, 'interpretation', raw)
    _, changed = await prepare(env, document_id=bundle.receipt.document_id)
    assert first.understanding_snapshot_ref != changed.understanding_snapshot_ref
    with pytest.raises(DocumentPreparationConflictError, match='duplicate'):
        await publish(env, first, changed)
    assert not env.flow.collection.documents and not env.flow.jobs.jobs


@pytest.mark.asyncio
async def test_fixed_original_injection_rejects_hash_order_and_interpreter_modes_before_trace_write():
    env = environment()
    bundle, item = await prepare(env)
    manifest = DocumentReportPublicationManifest(SCOPE, (item,), LEGACY_REPORT_SOURCE_SELECTION)
    _, outcomes, originals, _ = await env.publisher._execution(manifest)
    count = len(env.traces.documents)
    with pytest.raises(ValueError, match='interpretation-free'):
        await env.publisher.coordinator.process(
            outcomes,
            user_id=SCOPE.user_id,
            subject_id=SCOPE.subject_id,
            output_selection=LEGACY_REPORT_SOURCE_SELECTION,
            report_type_hint='lab',
            fixed_originals=originals,
        )
    with pytest.raises(DocumentParsingError, match='固定原始文件'):
        await env.publisher.coordinator.process(
            outcomes,
            user_id=SCOPE.user_id,
            subject_id=SCOPE.subject_id,
            output_selection=LEGACY_REPORT_SOURCE_SELECTION,
            fixed_originals=(b'wrong',),
        )
    assert len(env.traces.documents) == count


@pytest.mark.asyncio
async def test_cancelled_waiter_recovers_the_existing_output_task_without_a_second_publication():
    env = environment()
    _, item = await prepare(env)
    entered, release = asyncio.Event(), asyncio.Event()
    result = deepcopy(env.flow.extractor.return_value)

    async def delayed(snapshots):
        entered.set()
        await release.wait()
        return result

    env.flow.extractor.side_effect = delayed
    caller = asyncio.create_task(publish(env, item))
    await entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert env.publisher.coordinator.tasks
    tasks = tuple(env.publisher.coordinator.tasks)
    release.set()
    await asyncio.gather(*tasks)
    prohibit_reprocessing(env)
    receipt = await publish(env, item)
    assert receipt.processing_status == 'complete'
    assert env.flow.extractor.await_count == 1 and len(env.flow.jobs.jobs) == 2


@pytest.mark.asyncio
async def test_output_trace_is_actual_coordinator_result_and_survives_pure_replay():
    env = environment()
    _, item = await prepare(env)
    execute = AsyncMock(wraps=env.publisher.coordinator.process)
    env.publisher.coordinator.process = execute
    receipt = await publish(env, item)
    projection = await env.publisher.read_projection(
        scope=SCOPE, projection_ref=receipt.projection_ref, selection=LEGACY_REPORT_SOURCE_SELECTION
    )
    trace_id = projection.payload['outputTraceId']
    assert isinstance(trace_id, str) and trace_id
    assert any(row.get('traceId') == trace_id for row in env.traces.documents.values())
    assert execute.await_count == 1
    env.publisher = restart(env)
    env.publisher.coordinator.process = AsyncMock(side_effect=AssertionError('pure replay must not make a new trace'))
    again = await publish(env, item)
    restored = await env.publisher.read_projection(
        scope=SCOPE, projection_ref=again.projection_ref, selection=LEGACY_REPORT_SOURCE_SELECTION
    )
    assert again == receipt and restored.payload['outputTraceId'] == trace_id
