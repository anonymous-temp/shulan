"""Fixed preparation checkpoints survive publication failures without reprocessing."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.compatibility import create_document_compatibility_service
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.compatibility import DocumentPreparationConflictError
from modules.analysis.contracts import OutputSelection
from modules.analysis.contracts import UnsupportedOutputVersionError
from modules.analysis.publication import PublicationModeError
from modules.analysis.understanding import UNDERSTANDING_SELECTION
from modules.analysis.understanding import DocumentUnderstandingUnavailableError
from tests.unit._support.document_profile_composition.analysis.document_compatible_publication import (
    BsonCollection as BsonCollection,
)
from tests.unit._support.document_profile_composition.analysis.document_compatible_publication import (
    environment as environment,
)
from tests.unit._support.document_profile_composition.analysis.document_compatible_publication import restart as restart
from tests.unit.services.analysis.test_document_publication import evidence
from tests.unit.services.analysis.test_document_snapshots import SCOPE


async def prepare(env, **kwargs):
    return await env.api.prepare_bundle(
        scope=SCOPE,
        selection=UNDERSTANDING_SELECTION,
        **(kwargs or {'file_id': 'https://example.test/prepared.pdf'}),
    )


async def publish(env, bundle, **kwargs):
    return await env.api.publish_prepared(
        **{
            'scope': SCOPE,
            'understanding_snapshot_id': bundle.understanding.snapshot_id,
            'selection': UNDERSTANDING_SELECTION,
            'preparation_receipt': bundle.receipt,
            **kwargs,
        }
    )


def prohibit_processing(env):
    env.flow.coordinator.process = AsyncMock(side_effect=AssertionError('restore must not process'))
    env.flow.coordinator.index.get = AsyncMock(side_effect=AssertionError('restore must not read latest'))
    env.flow.traces.read_payload = AsyncMock(side_effect=AssertionError('restore must not read mutable archive'))
    env.flow.service._health_profile_service.ensure_self_profile = AsyncMock(
        side_effect=AssertionError('restore must not ensure self')
    )


@pytest.mark.asyncio
async def test_first_user_scope_is_an_explicit_command_and_read_restore_are_pure():
    env = environment()
    profile = env.flow.service._health_profile_service
    profile.ensure_self_profile.assert_not_awaited()
    scope = await env.api.ensure_self_scope(SCOPE.user_id)
    assert scope == SCOPE
    profile.ensure_self_profile.assert_awaited_once_with(SCOPE.user_id)
    bundle = await prepare(env)
    assert not env.flow.collection.documents and not env.flow.jobs.jobs
    assert len(env.records.documents) == 1
    prohibit_processing(env)
    env.api = restart(env)
    restored = await env.api.read_bundle(
        scope=SCOPE, selection=UNDERSTANDING_SELECTION, understanding_snapshot_id=bundle.understanding.snapshot_id
    )
    assert restored == replace(bundle, receipt=None)
    receipt = await publish(env, bundle)
    assert receipt.preparation_receipt == bundle.receipt
    assert receipt.canonical_report_id == bundle.material.source_id
    assert receipt.legacy_source_revision == 1 and receipt.ingestion_status == 'queued'
    assert len(env.flow.jobs.jobs) == len(env.flow.client.submit_calls) == len(env.flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_any_fixed_preparation_receipt_can_publish_not_only_the_latest_trace():
    env = environment()
    first = await prepare(env)
    second = await prepare(env)
    assert first.receipt.trace_id != second.receipt.trace_id
    assert first.understanding == second.understanding
    prohibit_processing(env)
    receipts = await asyncio.gather(*(publish(env, b) for b in (second, first, second, first)))
    assert {r.legacy_source_revision for r in receipts} == {1}
    assert [r.preparation_receipt for r in receipts] == [second.receipt, first.receipt, second.receipt, first.receipt]
    assert len(env.flow.jobs.jobs) == 1
    assert len(env.records.documents) == 3


@pytest.mark.asyncio
async def test_ack_lost_after_receipt_write_replays_without_another_publisher_call():
    env = environment()
    bundle = await prepare(env)
    original = env.api.publications.put_publication

    async def lost(*args):
        await original(*args)
        raise RuntimeError('receipt ACK lost')

    env.api.publications.put_publication = lost
    with pytest.raises(RuntimeError, match='ACK lost'):
        await publish(env, bundle)
    prohibit_processing(env)
    env.api = restart(env)
    env.api.legacy_publication.publish_document = AsyncMock(side_effect=AssertionError('must read saved receipt'))
    receipt = await publish(env, bundle)
    assert receipt.legacy_source_revision == 1 and len(env.flow.jobs.jobs) == 1


@pytest.mark.asyncio
async def test_uncertain_sink_ack_replays_same_identity_and_retains_legacy_cutover_blocker():
    env = environment()
    bundle = await prepare(env)
    original = env.flow.source.save

    async def lost(**kwargs):
        await original(**kwargs)
        raise RuntimeError('sink ACK lost')

    env.flow.source.save = lost
    with pytest.raises(RuntimeError, match='sink ACK lost'):
        await publish(env, bundle)
    env.flow.source.save = original
    env.api = restart(env)
    prohibit_processing(env)
    receipt = await publish(env, bundle)
    assert receipt.legacy_source_revision == 1 and len(env.flow.jobs.jobs) == 1
    state = await env.ledger.read_source(SCOPE, bundle.material.source_id)
    assert len(state['legacyTickets']) == 1
    with pytest.raises(PublicationModeError, match='uncertain'):
        await env.ledger.switch_mode(SCOPE, bundle.material.source_id, target='events', evidence=evidence())


@pytest.mark.asyncio
async def test_frozen_actual_version_and_raw_input_survive_later_interpretation_changes():
    env = environment()
    base = await prepare(env)
    await publish(env, base)
    document = env.flow.index_collection.documents[base.receipt.document_id]
    raw = env.flow.traces.payloads[(document['payloadRefs']['interpretation'], 'interpretation')]
    raw['observations'][0]['resultValue'] = '777'
    document['interpretationVersion'] = 'actual-old-input-version'
    changed = await prepare(env, document_id=document['_id'])
    fixed_raw = deepcopy(changed.understanding.payload['interpretation'])
    raw['observations'][0]['resultValue'] = '999'
    document['interpretationVersion'] = 'future-version'
    source_save = env.flow.source.save_revision

    async def lost(**kwargs):
        await source_save(**kwargs)
        raise RuntimeError('revision ACK lost')

    env.flow.source.save_revision = lost
    prohibit_processing(env)
    with pytest.raises(RuntimeError, match='revision ACK lost'):
        await publish(env, changed)
    env.flow.source.save_revision = AsyncMock(wraps=source_save)
    env.api = restart(env)
    receipt = await publish(env, changed)
    call = env.flow.source.save_revision.call_args.kwargs
    assert ':actual-old-input-version:' in call['source_key']
    assert call['result']['observations'] == fixed_raw['observations']
    assert receipt.legacy_source_revision > 1
    assert type(receipt.legacy_source_revision) is int
    assert (await publish(env, changed)) == receipt
    assert len(env.flow.reports._revisions.documents) == 1
    assert len(env.flow.client.submit_calls) == len(env.flow.graph.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper', ['trace', 'reused', 'document', 'scope', 'selection', 'snapshot'])
async def test_unbound_or_modified_checkpoint_is_rejected_before_publication(tamper):
    env = environment()
    bundle = await prepare(env)
    args = {}
    if tamper == 'trace':
        args['preparation_receipt'] = replace(bundle.receipt, trace_id='unissued-trace', reused=True)
    elif tamper == 'reused':
        args['preparation_receipt'] = replace(bundle.receipt, reused=not bundle.receipt.reused)
    elif tamper == 'document':
        args['preparation_receipt'] = replace(bundle.receipt, document_id='another')
    elif tamper == 'scope':
        args['scope'] = replace(SCOPE, subject_id='other-subject')
    elif tamper == 'selection':
        args['selection'] = OutputSelection('legacy_understanding', '2', 'legacy-understanding-v1')
    else:
        document = env.flow.index_collection.documents[bundle.receipt.document_id]
        raw = env.flow.traces.payloads[(document['payloadRefs']['interpretation'], 'interpretation')]
        raw['aiSummary'] = 'changed interpretation'
        second = await prepare(env, document_id=document['_id'])
        args['understanding_snapshot_id'] = second.understanding.snapshot_id
    prohibit_processing(env)
    with pytest.raises((DocumentNotFoundError, DocumentPreparationConflictError, UnsupportedOutputVersionError)):
        await publish(env, bundle, **args)
    assert not env.flow.collection.documents and not env.flow.jobs.jobs


@pytest.mark.asyncio
async def test_basic_bridge_receipt_is_not_a_compatibility_preparation_authorization():
    env = environment()
    bundle = await env.api.understanding.prepare_bundle(
        scope=SCOPE, selection=UNDERSTANDING_SELECTION, file_id='https://example.test/raw-bridge.pdf'
    )
    with pytest.raises(DocumentNotFoundError, match='preparation'):
        await publish(env, bundle)
    assert not env.flow.collection.documents


@pytest.mark.asyncio
@pytest.mark.parametrize('version', ['absent', None])
async def test_partial_quality_and_legacy_absent_version_keep_their_real_old_publication_identity(version):
    env = environment()
    base = await prepare(env)
    await publish(env, base)
    document = env.flow.index_collection.documents[base.receipt.document_id]
    raw = env.flow.traces.payloads[(document['payloadRefs']['interpretation'], 'interpretation')]
    raw['processing']['status'] = 'partial'
    raw['observations'][0]['resultValue'] = 'new value'
    if version == 'absent':
        del document['interpretationVersion']
    else:
        document['interpretationVersion'] = None
    document.pop('interpretationValidatedVersion', None)
    bundle = await prepare(env, document_id=document['_id'], resume_incomplete=False)
    assert bundle.understanding.payload['quality']['status'] == 'partial'
    assert bundle.understanding.payload['inputVersionStatus'] == 'legacy_unversioned'
    original = env.flow.source.save_revision
    env.flow.source.save_revision = AsyncMock(wraps=original)
    prohibit_processing(env)
    await publish(env, bundle)
    marker = 'legacy' if version == 'absent' else 'None'
    assert f':{marker}:' in env.flow.source.save_revision.call_args.kwargs['source_key']


@pytest.mark.asyncio
async def test_gate_prevents_new_legacy_writes_but_existing_receipt_is_a_read():
    env = environment()
    bundle = await prepare(env)
    await env.ledger.switch_mode(SCOPE, bundle.material.source_id, target='events', evidence=evidence())
    with pytest.raises(PublicationModeError):
        await publish(env, bundle)
    assert not env.flow.collection.documents
    await env.ledger.switch_mode(SCOPE, bundle.material.source_id, target='legacy', evidence=evidence())
    first = await publish(env, bundle)
    await env.ledger.switch_mode(SCOPE, bundle.material.source_id, target='events', evidence=evidence())
    assert await publish(env, bundle) == first


@pytest.mark.asyncio
async def test_missing_frozen_publication_identity_is_not_reconstructed_from_current_document():
    env = environment()
    bundle = await prepare(env)
    metadata = bundle.material.payload['documentMetadata']
    metadata.pop('sourceFingerprint')
    invalid = SimpleNamespace(
        material=SimpleNamespace(payload={'documentMetadata': metadata}), understanding=bundle.understanding
    )
    with pytest.raises(DocumentUnderstandingUnavailableError):
        env.api._document(invalid)


def test_factory_rejects_split_cutover_gates_without_io():
    env = environment()
    with pytest.raises(ValueError, match='share'):
        create_document_compatibility_service(
            processing_service=env.flow.service,
            profile_service=env.flow.service._health_profile_service,
            snapshots=env.snapshots,
            snapshot_chunks=env.chunks,
            compatibility_records=env.records,
        )
    assert not env.records.documents and not env.snapshots.documents


@pytest.mark.asyncio
@pytest.mark.parametrize('revision', [True, 0, -1, 1.5, 2**53])
async def test_non_integer_or_out_of_contract_source_revision_never_becomes_a_success_receipt(revision):
    env = environment()
    bundle = await prepare(env)
    original = env.api.legacy_publication.publish_document

    async def invalid(*args):
        saved = await original(*args)
        return replace(saved, record=replace(saved.record, source_revision=revision))

    env.api.legacy_publication.publish_document = invalid
    with pytest.raises(DocumentPreparationConflictError, match='revision'):
        await publish(env, bundle)
    assert not any(row['kind'] == 'legacy_receipt' for row in env.records.documents.values())
