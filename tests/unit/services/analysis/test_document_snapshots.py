"""Fixed material/fact reads and projections cannot implicitly publish Profile sources."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.snapshots import MongoDocumentSnapshotStore
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.application.extraction import merge_document_results
from modules.analysis.contracts import OutputSelection
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import DocumentSnapshotService
from modules.analysis.snapshots import _observations
from tests.unit.services.analysis.fakes import MemoryMongoCollection
from tests.unit.services.analysis.test_document_reuse import reuse_flow

SCOPE = DocumentScope('user-1', 'subject-self')


def setup():
    flow = reuse_flow()
    store = MongoDocumentSnapshotStore(snapshots=MemoryMongoCollection(), chunks=MemoryMongoCollection())
    api = DocumentSnapshotService(processing_service=flow.service, snapshots=store)
    return SimpleNamespace(flow=flow, store=store, api=api)


async def prepare(env):
    return await env.api.prepare_document(scope=SCOPE, file_id='https://example.test/synthetic.pdf')


@pytest.mark.asyncio
async def test_prepare_captures_facts_without_subject_bootstrap_or_publication_then_legacy_can_publish_once():
    env = setup()
    snapshot = await prepare(env)
    flow = env.flow
    flow.service._health_profile_service.ensure_self_profile.assert_not_awaited()
    assert not flow.collection.documents
    assert flow.jobs.jobs == {}
    assert len(flow.client.submit_calls) == len(flow.graph.calls) == 1
    assert snapshot.source_revision == 1
    assert snapshot.payload['legacySourceRevision'] is None
    outcome = await flow.service.process_document(
        user_id=SCOPE.user_id, subject_id=SCOPE.subject_id, document_id=snapshot.payload['documentId']
    )
    assert outcome.canonical_report_id == snapshot.source_id
    assert len(flow.jobs.jobs) == len(flow.client.submit_calls) == len(flow.graph.calls) == 1
    again = await flow.service.process_document(
        user_id=SCOPE.user_id, subject_id=SCOPE.subject_id, document_id=snapshot.payload['documentId']
    )
    assert again.source_revision == outcome.source_revision
    assert len(flow.jobs.jobs) == 1


@pytest.mark.asyncio
async def test_pure_snapshot_reads_and_capture_never_call_interpreter_or_publish():
    env = setup()
    original = await prepare(env)
    flow = env.flow
    flow.downloader.download.side_effect = AssertionError('read must not download')
    flow.interpreter.analyze = AsyncMock(side_effect=AssertionError('read must not interpret'))
    flow.source.save = AsyncMock(side_effect=AssertionError('read must not publish'))
    captured = await env.api.capture_facts(scope=SCOPE, document_id=original.payload['documentId'])
    assert captured == original
    material = await env.api.read_material(scope=SCOPE, snapshot_id=original.payload['materialSnapshotRef'])
    assert material.payload['parsedDocument']['text']
    read = await env.api.read_facts(scope=SCOPE, snapshot_id=original.snapshot_id)
    read.payload['observations'].clear()
    assert (await env.api.read_facts(scope=SCOPE, snapshot_id=original.snapshot_id)) == original
    flow.service._health_profile_service.ensure_self_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_scope_and_contract_are_enforced_without_creating_subject():
    env = setup()
    snapshot = await prepare(env)
    for scope in (DocumentScope('other', SCOPE.subject_id), DocumentScope(SCOPE.user_id, 'other')):
        with pytest.raises(DocumentNotFoundError):
            await env.api.read_facts(scope=scope, snapshot_id=snapshot.snapshot_id)
        with pytest.raises(DocumentNotFoundError):
            await env.api.capture_facts(scope=scope, document_id=snapshot.payload['documentId'])
    with pytest.raises(TypeError):
        await env.api.read_facts(scope=None, snapshot_id=snapshot.snapshot_id)
    with pytest.raises(ValueError, match='contract'):
        await env.api.read_material(scope=SCOPE, snapshot_id=snapshot.snapshot_id)
    env.flow.service._health_profile_service.ensure_self_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_snapshot_survives_archive_rewrite_and_keeps_material_revision_separate_from_extraction():
    env = setup()
    first = await prepare(env)
    document = env.flow.index_collection.documents[first.payload['documentId']]
    trace = document['payloadRefs']['interpretation']
    result = env.flow.traces.payloads[(trace, 'interpretation')]
    result['observations'] = [{'itemName': 'raw measurement', 'resultValue': '2.5', 'unit': 'mmol/L'}]
    result['metrics'] = [{'private': 'Report-only'}]
    result['taxa'] = [{'private': 'Report-only'}]
    result['aiSummary'] = 'not an observed fact'
    document['interpretationVersion'] = 'second-extractor'
    second = await env.api.capture_facts(scope=SCOPE, document_id=document['_id'])
    assert first.snapshot_id != second.snapshot_id
    assert first.source_revision == second.source_revision == 1
    assert first.payload['materialSnapshotRef'] == second.payload['materialSnapshotRef']
    assert (await env.api.read_facts(scope=SCOPE, snapshot_id=first.snapshot_id)).payload == first.payload
    row = second.payload['observations'][0]
    assert row['rawValue'] == '2.5'
    assert row['identityStatus'] == 'identity_unresolved' and row['stableObservationId'] is None
    assert not {'metrics', 'taxa', 'aiSummary', 'supplementaryHospitalData'} & second.payload.keys()


@pytest.mark.asyncio
async def test_only_existing_physical_binding_marks_identity_verified_and_original_evidence_is_preserved():
    env = setup()
    first = await prepare(env)
    document = env.flow.index_collection.documents[first.payload['documentId']]
    result = env.flow.traces.payloads[(document['payloadRefs']['interpretation'], 'interpretation')]
    result.update(
        observations=[
            {'itemName': 'name', 'resultValue': 1, 'rawFlag': 'H', 'evidence': {'quote': 'name 1', 'table': 3}}
        ],
        sourceBindings={'/observations/0': 'v6:original-physical-cell'},
    )
    facts = await env.api.capture_facts(scope=SCOPE, document_id=document['_id'])
    row = facts.payload['observations'][0]
    assert row['stableObservationId'] == 'v6:original-physical-cell' and row['identityStatus'] == 'verified'
    assert row['flag'] == 'H' and row['evidence'] == {'quote': 'name 1', 'table': 3}


@pytest.mark.asyncio
async def test_projection_selection_is_exact_and_cannot_change_snapshot_or_publish():
    env = setup()
    snapshot = await prepare(env)
    selection = OutputSelection('record-private', '1', 'adapter-1')
    projector = SimpleNamespace(selection=selection, project=AsyncMock(return_value={'record': 'consumer-owned'}))
    projected = await env.api.project(
        scope=SCOPE, snapshot_id=snapshot.snapshot_id, selection=selection, projector=projector
    )
    assert projected.payload == {'record': 'consumer-owned'}
    assert projected.facts_snapshot_id == snapshot.snapshot_id
    with pytest.raises(ValueError, match='exact'):
        await env.api.project(
            scope=SCOPE,
            snapshot_id=snapshot.snapshot_id,
            selection=OutputSelection('record-private', '2', 'adapter-1'),
            projector=projector,
        )
    assert projector.project.await_count == 1
    assert env.flow.jobs.jobs == {}


@pytest.mark.asyncio
async def test_chunk_failure_does_not_publish_manifest_and_retry_reuses_immutable_chunks():
    env = setup()
    snapshot = await prepare(env)
    store = MongoDocumentSnapshotStore(snapshots=MemoryMongoCollection(), chunks=MemoryMongoCollection())
    store.CHUNK_BYTES = 80
    store.snapshots.fail_insert = True
    with pytest.raises(RuntimeError):
        await store.put(snapshot)
    assert store.snapshots.documents == {}
    chunks = deepcopy(store.chunks.documents)
    store.snapshots.fail_insert = False
    await store.put(snapshot)
    await store.put(snapshot)
    assert store.chunks.documents == chunks
    assert await store.get(SCOPE, snapshot.snapshot_id) == snapshot
    store.chunks.documents[next(iter(chunks))]['data'] = b'corrupt'
    with pytest.raises(RuntimeError, match='integrity'):
        await store.get(SCOPE, snapshot.snapshot_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('kwargs', [{'report_id': 'old'}, {'subject_id': 'other'}, {'publish': True}])
async def test_preparation_cannot_reenter_legacy_publication(kwargs):
    env = setup()
    with pytest.raises(ValueError):
        await env.api.prepare_document(scope=SCOPE, file_id='https://example.test/a.pdf', **kwargs)
    assert env.flow.jobs.jobs == {} and not env.flow.client.submit_calls


def test_independent_root_observation_with_source_section_is_not_lost():
    source = {'reportTime': '2026-01-01', 'observations': [{'itemName': 'child', 'resultValue': 1}]}
    result = {
        'observations': [{'itemName': 'independent root', 'resultValue': 2, 'sourceSection': 0}],
        'sections': [source],
    }
    facts = _observations(result)
    assert [item['label'] for item in facts] == ['independent root', 'child']
    assert [item['observationRef'] for item in facts] == ['/observations/0', '/sections/0/observations/0']


def test_only_exact_dated_section_projection_is_removed_and_section_pointer_retained():
    result = merge_document_results(
        [
            {
                'reportType': 'lab',
                'status': 'normal',
                'reportTime': '2026年1月1日',
                'observations': [{'itemName': 'first', 'resultValue': 1}],
            },
            {
                'reportType': 'lab',
                'status': 'normal',
                'reportTime': '2026-01-02',
                'observations': [{'itemName': 'second', 'resultValue': 2}],
            },
        ]
    )
    assert result['observations'][0]['sourceSection'] == 0
    facts = _observations(result)
    assert [item['label'] for item in facts] == ['first', 'second']
    assert [item['observationRef'] for item in facts] == ['/sections/0/observations/0', '/sections/1/observations/0']


def test_joined_imaging_display_is_not_an_additional_source_observation():
    result = merge_document_results(
        [
            {
                'reportType': 'imaging',
                'status': 'normal',
                'expansion': {'findings': 'A', 'diagnosis': 'X'},
                'observations': [],
            },
            {
                'reportType': 'imaging',
                'status': 'normal',
                'expansion': {'findings': 'B', 'diagnosis': 'Y'},
                'observations': [],
            },
        ]
    )
    assert result['expansion']['findings'] == 'A\nB'
    facts = _observations(result)
    assert [item['rawValue'] for item in facts] == ['A', 'X', 'B', 'Y']
    assert all(item['observationRef'].startswith('/sections/') for item in facts)


def test_evidenced_root_imaging_occurrence_survives_even_with_same_display_text():
    result = {
        'reportType': 'imaging',
        'expansion': {'findings': 'A'},
        'expansionEvidence': {'findings': {'sourceId': 'root-page', 'quote': 'A'}},
        'sections': [{'sectionIndex': 0, 'reportType': 'imaging', 'expansion': {'findings': 'A'}}],
    }
    facts = _observations(result)
    assert [item['observationRef'] for item in facts] == ['/expansion/findings', '/sections/0/expansion/findings']
    assert facts[0]['evidence'] == {'sourceId': 'root-page', 'quote': 'A'}


def test_original_source_reference_is_preserved_without_inventing_physical_identity():
    facts = _observations({'observations': [{'itemName': 'test', 'resultValue': 2, 'sourceId': 'page-1'}]})
    assert facts[0]['sourceId'] == 'page-1'
    assert facts[0]['identityStatus'] == 'identity_unresolved'
    assert facts[0]['stableObservationId'] is None
