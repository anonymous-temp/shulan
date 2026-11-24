"""The compatibility bridge freezes one coherent derived interpretation."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.contracts import OutputSelection
from modules.analysis.contracts import UnsupportedOutputVersionError
from modules.analysis.publication import DocumentPublisher
from modules.analysis.snapshots import DocumentScope
from modules.analysis.understanding import UNDERSTANDING_SELECTION
from modules.analysis.understanding import DocumentPreparedBundle
from modules.analysis.understanding import DocumentSnapshotChangedError
from modules.analysis.understanding import DocumentUnderstandingService
from modules.analysis.understanding import DocumentUnderstandingUnavailableError
from tests.unit.services.analysis.test_document_snapshots import SCOPE
from tests.unit.services.analysis.test_document_snapshots import setup


def environment():
    env = setup()
    env.api = DocumentUnderstandingService(processing_service=env.flow.service, snapshots=env.store)
    return env


async def prepare(env, **kwargs):
    return await env.api.prepare_bundle(
        scope=SCOPE,
        selection=UNDERSTANDING_SELECTION,
        **(kwargs or {'file_id': 'https://example.test/bridge.pdf'}),
    )


def stored(env, bundle):
    document = env.flow.index_collection.documents[bundle.material.payload['documentId']]
    interpretation = env.flow.traces.payloads[(document['payloadRefs']['interpretation'], 'interpretation')]
    return document, interpretation


@pytest.mark.asyncio
async def test_one_prepare_freezes_full_derived_interpretation_without_profile_publication():
    env = environment()
    first = await prepare(env)
    document, interpretation = stored(env, first)
    expected = deepcopy(interpretation)
    expected.update(
        {
            'subject': {'name': 'Synthetic subject'},
            'basicInfo': {'age': 42},
            'patientInfo': {'gender': 'F'},
            'name': 'Synthetic subject',
            'gender': 'F',
            'age': 42,
            'height': 163,
            'weight': 60,
            'title': 'Full report',
            'reportTime': '2026-10-01',
            'aiSummary': 'derived summary',
            'sections': [
                {
                    'reportType': 'lab',
                    'reportTime': '2026-10-01',
                    'observations': [{'itemName': 'Synthetic marker', 'resultValue': '5', 'evidence': {'page': 2}}],
                }
            ],
        }
    )
    interpretation.clear()
    interpretation.update(expected)
    bundle = await env.api.capture_bundle(scope=SCOPE, document_id=document['_id'], selection=UNDERSTANDING_SELECTION)
    assert bundle.receipt is None
    assert bundle.understanding.payload['interpretation'] == expected
    assert bundle.understanding.payload['classification'] == 'derived_interpretation'
    assert not {'subject', 'basicInfo', 'patientInfo', 'aiSummary'} & bundle.facts.payload.keys()
    assert bundle.understanding.payload['quality'] == bundle.facts.payload['quality']
    metadata = bundle.understanding.payload['documentMetadata']
    assert metadata['canonicalReportId'] == bundle.facts.source_id
    assert metadata['taskId'] == document['taskId']
    assert metadata['firstFileUrl'] == 'https://example.test/bridge.pdf'
    assert metadata['contentSha256'] == bundle.material.payload['contentSha256']
    assert bundle.understanding.payload['interpretationRevision'] == document['interpretationVersion']
    assert len(env.flow.client.submit_calls) == len(env.flow.graph.calls) == 1
    assert not env.flow.collection.documents and not env.flow.jobs.jobs
    env.flow.service._health_profile_service.ensure_self_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_reads_are_immutable_and_need_no_archive_index_or_processing():
    env = environment()
    bundle = await prepare(env)
    env.flow.coordinator.process = AsyncMock(side_effect=AssertionError('no processing during reads'))
    env.flow.index.get = AsyncMock(side_effect=AssertionError('no latest-index read'))
    env.flow.traces.read_payload = AsyncMock(side_effect=AssertionError('no live archive read'))
    read = await env.api.read_bundle(
        scope=SCOPE, understanding_snapshot_id=bundle.understanding.snapshot_id, selection=UNDERSTANDING_SELECTION
    )
    read.understanding.payload['interpretation'].clear()
    assert read == replace(bundle, receipt=None)
    assert read.receipt is None
    assert (
        await env.api.read_understanding(
            scope=SCOPE, snapshot_id=bundle.understanding.snapshot_id, selection=UNDERSTANDING_SELECTION
        )
    ).payload['interpretation']
    env.flow.coordinator.process.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_receipts_preserve_request_reuse_without_changing_snapshot_identities():
    env = environment()
    first = await prepare(env)
    second = await prepare(env)
    assert first.receipt.document_id == second.receipt.document_id == first.material.payload['documentId']
    assert first.receipt.trace_id != second.receipt.trace_id
    assert first.receipt.reused is False and second.receipt.reused is True
    assert replace(first, receipt=None) == replace(second, receipt=None)
    assert len(env.store.snapshots.documents) == 3
    assert len(env.flow.client.submit_calls) == len(env.flow.graph.calls) == 1
    assert not env.flow.collection.documents and not env.flow.jobs.jobs
    with pytest.raises(ValueError, match='receipt'):
        replace(first, receipt=replace(first.receipt, document_id='different-document'))


@pytest.mark.asyncio
async def test_partial_and_unversioned_cache_are_labeled_honestly():
    env = environment()
    initial = await prepare(env)
    document, interpretation = stored(env, initial)
    interpretation['processing'] = {'status': 'partial', 'retryable': True, 'issues': [{'kind': 'unreadable'}]}
    document['interpretationVersion'] = None
    document['interpretationValidatedVersion'] = None
    bundle = await prepare(env, document_id=document['_id'], resume_incomplete=False)
    assert bundle.facts.payload['quality']['status'] == 'partial'
    assert bundle.understanding.payload['quality'] == interpretation['processing']
    assert bundle.understanding.payload['inputVersionStatus'] == 'legacy_unversioned'
    assert bundle.understanding.payload['interpretationRevision'] is None
    assert len(env.flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_fixed_capture_uses_old_reference_not_current_latest():
    env = environment()
    bundle = await prepare(env)
    document, interpretation = stored(env, bundle)
    new_ref = 'new-interpretation-reference'
    env.flow.traces.payloads[(new_ref, 'interpretation')] = {**interpretation, 'aiSummary': 'new summary'}
    document['payloadRefs']['interpretation'] = new_ref
    document['interpretationVersion'] = 'new-version'
    snapshot = await env.api.capture_understanding(
        scope=SCOPE, facts_snapshot_id=bundle.facts.snapshot_id, selection=UNDERSTANDING_SELECTION
    )
    assert snapshot == bundle.understanding


@pytest.mark.asyncio
async def test_same_reference_rewrite_is_not_disguised_as_old_interpretation():
    env = environment()
    bundle = await prepare(env)
    _, interpretation = stored(env, bundle)
    interpretation['aiSummary'] = 'only derived wording changed; observations stay equal'
    with pytest.raises(DocumentUnderstandingUnavailableError, match='identity'):
        await env.api.capture_understanding(
            scope=SCOPE, facts_snapshot_id=bundle.facts.snapshot_id, selection=UNDERSTANDING_SELECTION
        )
    assert (
        await env.api.read_understanding(
            scope=SCOPE, snapshot_id=bundle.understanding.snapshot_id, selection=UNDERSTANDING_SELECTION
        )
    ) == bundle.understanding


@pytest.mark.asyncio
async def test_old_facts_without_full_binding_do_not_read_latest_to_reconstruct():
    env = environment()
    facts = await env.api.prepare_document(scope=SCOPE, file_id='https://example.test/legacy.pdf')
    env.flow.traces.read_payload = AsyncMock(side_effect=AssertionError('no invented legacy digest'))
    with pytest.raises(DocumentUnderstandingUnavailableError, match='legacy facts'):
        await env.api.capture_understanding(
            scope=SCOPE, facts_snapshot_id=facts.snapshot_id, selection=UNDERSTANDING_SELECTION
        )
    env.flow.traces.read_payload.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_input_between_prepare_and_capture_is_rejected(monkeypatch):
    env = environment()
    capture = env.api.capture_bundle

    async def racing_capture(**kwargs):
        document = env.flow.index_collection.documents[kwargs['document_id']]
        env.flow.traces.payloads[(document['payloadRefs']['interpretation'], 'interpretation')]['aiSummary'] = 'raced'
        return await capture(**kwargs)

    monkeypatch.setattr(env.api, 'capture_bundle', racing_capture)
    with pytest.raises(DocumentSnapshotChangedError, match='after preparation'):
        await prepare(env)
    assert not env.store.snapshots.documents
    assert len(env.flow.graph.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('part', ['interpretation', 'docmind_result', 'reference', 'version', 'running'])
async def test_capture_detects_recovery_race_even_if_observations_are_equal(monkeypatch, part):
    env = environment()
    bundle = await prepare(env)
    document, _ = stored(env, bundle)
    original = env.api._inputs
    calls = []

    async def racing_inputs(*args):
        calls.append(True)
        if len(calls) == 2:
            if part == 'running':
                document['status'] = 'running'
            elif part == 'version':
                document['interpretationValidatedVersion'] = 'changed'
            elif part == 'reference':
                old = document['payloadRefs']['interpretation']
                env.flow.traces.documents['new-ref'] = deepcopy(env.flow.traces.documents[old])
                env.flow.traces.payloads[('new-ref', 'interpretation')] = env.flow.traces.payloads[
                    (old, 'interpretation')
                ]
                document['payloadRefs']['interpretation'] = 'new-ref'
            else:
                env.flow.traces.payloads[(document['payloadRefs'][part], part)]['syntheticExtra'] = 'changed'
        return await original(*args)

    monkeypatch.setattr(env.api, '_inputs', racing_inputs)
    count = len(env.store.snapshots.documents)
    with pytest.raises(DocumentSnapshotChangedError, match='during capture'):
        await env.api.capture_bundle(scope=SCOPE, document_id=document['_id'], selection=UNDERSTANDING_SELECTION)
    assert len(env.store.snapshots.documents) == count


@pytest.mark.asyncio
async def test_exact_selection_scope_and_contract_are_checked_without_model_call():
    env = environment()
    with pytest.raises(UnsupportedOutputVersionError):
        await env.api.prepare_bundle(scope=SCOPE, selection=OutputSelection('legacy_understanding', '1', 'future'))
    assert not env.flow.graph.calls
    bundle = await prepare(env)
    with pytest.raises(DocumentNotFoundError):
        await env.api.read_bundle(
            scope=DocumentScope('other', SCOPE.subject_id),
            understanding_snapshot_id=bundle.understanding.snapshot_id,
            selection=UNDERSTANDING_SELECTION,
        )
    with pytest.raises(ValueError, match='contract'):
        await env.api.read_facts(scope=SCOPE, snapshot_id=bundle.understanding.snapshot_id)
    with pytest.raises(ValueError, match='bundle'):
        DocumentPreparedBundle(bundle.material, bundle.facts, bundle.material)


@pytest.mark.asyncio
async def test_understanding_is_rejected_by_the_facts_publisher():
    env = environment()
    bundle = await prepare(env)
    ledger, receiver = AsyncMock(), AsyncMock()
    publisher = DocumentPublisher(snapshots=env.store, ledger=ledger, receiver=receiver)
    with pytest.raises(ValueError, match='fixed facts'):
        await publisher.publish(
            scope=SCOPE,
            snapshot_id=bundle.understanding.snapshot_id,
            request_id='cannot-publish-derived',
            expected_prior_revision=None,
        )
    ledger.reserve.assert_not_called()
    receiver.deliver.assert_not_called()


@pytest.mark.asyncio
async def test_processor_without_binding_fails_explicitly_without_latest_lookup():
    env = environment()
    env.flow.coordinator.process = AsyncMock(return_value=SimpleNamespace(document_id='legacy'))
    env.api._inputs = AsyncMock(side_effect=AssertionError('cannot reconstruct current input as the old one'))
    with pytest.raises(DocumentUnderstandingUnavailableError, match='actual input binding'):
        await prepare(env)
    env.api._inputs.assert_not_awaited()
