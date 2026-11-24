"""Real fixed Document artifacts; synthetic admission is explicitly protocol-only."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from modules.analysis.application.documents import DocumentInterpretationError
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.complete_source import DocumentCompleteSourceService
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.complete_source_contracts import DocumentRetiredContribution
from modules.analysis.complete_source_contracts import DocumentSourceCapturePlan
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import canonical_json
from modules.report.document_source import extract_intrinsic_report_source
from tests.unit._support.current_document import SyntheticAdmission
from tests.unit.services.analysis.test_document_report_publication import environment
from tests.unit.services.analysis.test_document_report_publication import prepare
from tests.unit.services.analysis.test_document_report_publication import prohibit_reprocessing
from tests.unit.services.analysis.test_document_report_publication import publish
from tests.unit.services.analysis.test_document_snapshots import SCOPE
from utils.report_source_observations import report_source_items


def service(env, admission=None):
    return DocumentCompleteSourceService(
        snapshots=env.api.understanding.snapshots,
        understanding=env.api.understanding,
        report_publications=env.publisher,
        admission=admission,
    )


def plan(bundle, handles, *, ref='verified-plan', legacy=None, retired=()):
    return DocumentSourceCapturePlan(
        scope=SCOPE,
        source_id=bundle.material.source_id,
        source_revision=bundle.material.source_revision,
        active=tuple(handles),
        retired=tuple(retired),
        legacy_state='captured' if legacy else 'absent',
        legacy_source_revision=legacy,
        fence_evidence_ref='actual-fence',
        active_set_evidence_ref=ref,
        _fence_json=canonical_json(
            {
                'evidenceRef': 'actual-fence',
                'publishersQuiesced': True,
                'workersQuiesced': True,
                'legacyIntentsRetired': True,
            }
        ),
    )


async def change_interpretation(env, bundle, *, status=None, value=None, scope=SCOPE):
    document = env.flow.index_collection.documents[bundle.receipt.document_id]
    trace = env.flow.service._document_trace_store
    raw = await trace.read_payload(
        document['payloadRefs']['interpretation'],
        scope.user_id,
        scope.subject_id,
        'interpretation',
    )
    if status is not None:
        raw['processing']['status'] = status
    if value is not None:
        raw['observations'][0]['resultValue'] = value
    ref = 'correction-' + str(status or value)
    await trace.create({'_id': ref, 'userId': scope.user_id, 'subjectId': scope.subject_id})
    await trace.write_payload(ref, scope.user_id, 'interpretation', raw)
    document['payloadRefs']['interpretation'] = ref
    from modules.analysis.understanding import UNDERSTANDING_SELECTION

    return await env.api.prepare_bundle(
        scope=scope, selection=UNDERSTANDING_SELECTION, document_id=bundle.receipt.document_id, resume_incomplete=False
    )


@pytest.mark.asyncio
async def test_real_bundle_neutral_capture_is_pure_with_respect_to_publication_and_processing():
    env = environment()
    bundle, _ = await prepare(env)
    api = service(env)
    prohibit_reprocessing(env)
    handle = await api.capture_base_contribution(
        scope=SCOPE, understanding_snapshot_id=bundle.understanding.snapshot_id
    )
    snapshot, proof = await api._handle(SCOPE, handle)
    assert snapshot.payload['observations'] == bundle.facts.payload['observations']
    assert snapshot.payload['legacySourceRevision'] is None
    assert 'aiSummary' not in canonical_json(snapshot.canonical)
    assert proof.payload['understandingSnapshotRef'] == bundle.understanding.snapshot_id
    assert snapshot.source_revision == bundle.material.source_revision == 1
    assert not env.flow.collection.documents and not env.flow.jobs.jobs
    with pytest.raises(DocumentCompleteSourceUnavailable, match='admission provider'):
        await api.capture_source(scope=SCOPE, verified_plan_ref='unverified')


@pytest.mark.asyncio
async def test_real_v16_published_projection_maps_only_original_neutral_rows_and_real_revision():
    env = environment(extractor=extract_intrinsic_report_source)
    bundle, prepared = await prepare(env)
    published = await publish(env, prepared)
    api = service(env)
    before = (len(env.flow.jobs.jobs), len(env.flow.graph.calls))
    prohibit_reprocessing(env)
    env.publisher.coordinator.process = AsyncMock(side_effect=AssertionError('capture must not extract again'))
    handle = await api.capture_legacy_report_contribution(scope=SCOPE, publication_receipt_ref=published.receipt_ref)
    snapshot, proof = await api._handle(SCOPE, handle)
    raw = (
        await env.publisher.read_projection(
            scope=SCOPE,
            projection_ref=published.projection_ref,
            selection=published.manifest.selection,
        )
    ).payload['projection']
    expected = report_source_items(raw)
    assert [row['rawValue'] for row in snapshot.payload['observations']] == [row['resultValue'] for _, row in expected]
    assert snapshot.payload['legacySourceRevision'] == published.legacy_source_revision
    assert snapshot.payload['reportTime'] == bundle.understanding.payload['interpretation']['reportTime']
    assert proof.payload['projectionSnapshotRef'] == published.projection_ref
    assert before == (len(env.flow.jobs.jobs), len(env.flow.graph.calls))


@pytest.mark.asyncio
@pytest.mark.parametrize('quality', ['partial', 'unavailable'])
async def test_real_stored_incomplete_interpretation_cannot_be_relabelled_accepted(quality):
    env = environment()
    initial, _ = await prepare(env)
    if quality == 'unavailable':
        before = len(env.snapshots.documents)
        with pytest.raises(DocumentInterpretationError):
            await change_interpretation(env, initial, status=quality)
        assert len(env.snapshots.documents) == before
        assert not env.flow.jobs.jobs
        return
    incomplete = await change_interpretation(env, initial, status=quality)
    api = service(env)
    before = len(env.snapshots.documents)
    prohibit_reprocessing(env)
    with pytest.raises(DocumentCompleteSourceUnavailable, match='incomplete original'):
        await api.capture_base_contribution(scope=SCOPE, understanding_snapshot_id=incomplete.understanding.snapshot_id)
    assert len(env.snapshots.documents) == before
    assert not env.flow.jobs.jobs


@pytest.mark.asyncio
async def test_complete_source_reads_verify_fixed_plan_and_input_chain_without_latest():
    env = environment()
    bundle, _ = await prepare(env)
    admission = SyntheticAdmission()
    api = service(env, admission)
    handle = await api.capture_base_contribution(
        scope=SCOPE, understanding_snapshot_id=bundle.understanding.snapshot_id
    )
    admission.plans['verified-plan'] = plan(bundle, [handle])
    result = await api.capture_source(scope=SCOPE, verified_plan_ref='verified-plan')
    assert result.source_snapshot_ref == 'document_source_snapshot_' + result.source_content_digest
    prohibit_reprocessing(env)
    source = await api.read_source_observations(scope=SCOPE, snapshot_id=result.source_snapshot_ref)
    inventory = await api.read_source_inventory(scope=SCOPE, snapshot_id=result.inventory_snapshot_ref)
    assert (
        source['contractVersion'] == COMPLETE_SOURCE_CONTRACT and source['payload']['quality']['status'] == 'accepted'
    )
    assert source['payload']['coverage']['inventoryDigest'] == result.inventory_content_digest
    assert inventory['payload']['contributions'][0]['snapshotId'] == handle.snapshot_ref
    source['payload']['contributions'].clear()
    assert (await api.read_source_observations(scope=SCOPE, snapshot_id=result.source_snapshot_ref))['payload'][
        'contributions'
    ]
    assert not env.flow.jobs.jobs and not env.flow.collection.documents


@pytest.mark.asyncio
async def test_same_original_revision_correction_changes_complete_digest_not_material_revision():
    env = environment()
    original, _ = await prepare(env)
    corrected = await change_interpretation(env, original, value='479')
    admission = SyntheticAdmission()
    api = service(env, admission)
    a = await api.capture_base_contribution(scope=SCOPE, understanding_snapshot_id=original.understanding.snapshot_id)
    b = await api.capture_base_contribution(scope=SCOPE, understanding_snapshot_id=corrected.understanding.snapshot_id)
    admission.plans['first'] = plan(original, [a], ref='first')
    admission.plans['second'] = plan(corrected, [b], ref='second')
    first = await api.capture_source(scope=SCOPE, verified_plan_ref='first')
    second = await api.capture_source(scope=SCOPE, verified_plan_ref='second')
    assert original.material.snapshot_id == corrected.material.snapshot_id
    assert original.material.source_revision == corrected.material.source_revision == 1
    assert original.understanding.snapshot_id != corrected.understanding.snapshot_id
    assert first.source_content_digest != second.source_content_digest
    assert first.source_snapshot_ref != second.source_snapshot_ref
    old = await api.read_source_observations(scope=SCOPE, snapshot_id=first.source_snapshot_ref)
    new = await api.read_source_observations(scope=SCOPE, snapshot_id=second.source_snapshot_ref)
    assert old['sourceRevision'] == new['sourceRevision'] == 1
    assert old['payload']['contributions'][0]['observations'] != new['payload']['contributions'][0]['observations']


@pytest.mark.asyncio
async def test_active_and_retired_sets_cannot_overlap_and_reads_reject_changed_proof():
    env = environment()
    bundle, _ = await prepare(env)
    admission = SyntheticAdmission()
    api = service(env, admission)
    handle = await api.capture_base_contribution(
        scope=SCOPE, understanding_snapshot_id=bundle.understanding.snapshot_id
    )
    admission.plans['verified-plan'] = plan(
        bundle, [handle], legacy=1, retired=[DocumentRetiredContribution(handle, 'retracted', 'withdrawal-proof')]
    )
    with pytest.raises(DocumentCompleteSourceUnavailable, match='re-enter'):
        await api.capture_source(scope=SCOPE, verified_plan_ref='verified-plan')
    admission.plans['verified-plan'] = plan(bundle, [handle])
    receipt = await api.capture_source(scope=SCOPE, verified_plan_ref='verified-plan')
    admission.plans['verified-plan'] = replace(
        admission.plans['verified-plan'], legacy_state='captured', legacy_source_revision=2
    )
    with pytest.raises(DocumentCompleteSourceUnavailable, match='differs'):
        await api.read_source_observations(scope=SCOPE, snapshot_id=receipt.source_snapshot_ref)


@pytest.mark.asyncio
async def test_wrong_scope_and_missing_actual_capture_proof_are_unavailable_without_writes():
    env = environment()
    bundle, _ = await prepare(env)
    api = service(env)
    with pytest.raises(DocumentNotFoundError):
        await api.capture_base_contribution(
            scope=DocumentScope('other', SCOPE.subject_id), understanding_snapshot_id=bundle.understanding.snapshot_id
        )
    assert not env.flow.jobs.jobs


def test_plan_cannot_infer_quiescence_or_conflate_fresh_with_historical_source():
    env_scope = SCOPE
    from modules.analysis.complete_source_contracts import DocumentContributionHandle

    arguments = dict(
        scope=env_scope,
        source_id='report',
        source_revision=1,
        active=(DocumentContributionHandle('snapshot', 'proof'),),
        retired=(),
        legacy_state='absent',
        legacy_source_revision=None,
        fence_evidence_ref='fence',
        active_set_evidence_ref='plan',
    )
    with pytest.raises(DocumentCompleteSourceUnavailable):
        DocumentSourceCapturePlan(
            **arguments,
            _fence_json=canonical_json(
                {
                    'evidenceRef': 'fence',
                    'publishersQuiesced': True,
                    'workersQuiesced': False,
                    'legacyIntentsRetired': True,
                }
            ),
        )
    fresh = {
        'admissionKind': 'fresh',
        'evidenceRef': 'fence',
        'ownerFenceRef': {
            'provider': 'document',
            'contractVersion': 'profile-owner-admission-fence.v1',
            'snapshotId': 'owner-proof',
        },
        'ownerFenceDigest': 'a' * 64,
        'profilePresenceProofRef': 'profile_presence_' + 'b' * 64,
        'profilePresenceProofDigest': 'b' * 64,
    }
    accepted = DocumentSourceCapturePlan(**arguments, _fence_json=canonical_json(fresh))
    assert 'publishersQuiesced' not in accepted.fence
    with pytest.raises(DocumentCompleteSourceUnavailable):
        replace(accepted, legacy_state='captured', legacy_source_revision=1)
