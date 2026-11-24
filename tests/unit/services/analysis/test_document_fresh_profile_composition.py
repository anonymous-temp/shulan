"""Actual public providers with BSON-backed offline storage, never synthetic proof."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.complete_source import create_document_complete_source_publisher
from adapters.analysis.complete_source import create_document_complete_source_service
from adapters.analysis.source_admission import create_document_fresh_source_admission
from health_profile.report_source import MongoHealthReportStore
from health_profile.source_admission_contracts import ProfileFreshSourceAuthorizationV1
from health_profile.source_archive import ProfileSourceMaterialArchive
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.publication import PublicationModeError
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT
from tests.unit._support.document_profile_composition.analysis.document_fresh_profile_composition import (
    composition as composition,
)
from tests.unit._support.document_profile_composition.analysis.document_fresh_profile_composition import (
    options as options,
)
from tests.unit.health_profile.test_public_contracts import visible


async def capture(flow):
    plan_ref = await flow.admission.prepare_fresh_plan(scope=flow.scope, prepared=flow.item, request_id='fresh')
    captured = await flow.api.capture_source(scope=flow.scope, verified_plan_ref=plan_ref)
    plan = await flow.admission.read_verified_plan(scope=flow.scope, plan_ref=plan_ref)
    fence = plan.fence
    authorization = ProfileFreshSourceAuthorizationV1(
        scope=flow.profile.scope,
        source_type='health_report',
        source_id=flow.bundle.material.source_id,
        authorization_id='fixed-fresh',
        owner_fence_ref=fence['ownerFenceRef'],
        owner_fence_digest=fence['ownerFenceDigest'],
        presence_proof_ref=fence['profilePresenceProofRef'],
        presence_proof_digest=fence['profilePresenceProofDigest'],
        material_contract_version=COMPLETE_SOURCE_CONTRACT,
        material_snapshot_ref={
            'provider': 'document',
            'contractVersion': COMPLETE_SOURCE_CONTRACT,
            'snapshotId': captured.source_snapshot_ref,
        },
        material_content_digest=captured.source_content_digest,
        processing_revision=flow.resolver.VERSION,
        authorized_at=flow.profile.now[0],
    )
    return plan_ref, captured, authorization


@pytest.mark.asyncio
async def test_real_fresh_ready_archive_publication_and_historical_replay(monkeypatch):
    flow = await composition()
    plan_ref, captured, authorization = await capture(flow)
    scope, source_id = flow.scope, flow.bundle.material.source_id
    with pytest.raises(PublicationModeError):
        await flow.publisher.publish(
            scope=scope, snapshot_id=captured.source_snapshot_ref, request_id='event', expected_prior_revision=None
        )
    reserved = await flow.fresh.reserve_fresh_source(authorization, scope=flow.profile.scope)
    assert reserved.status == 'blocked'
    with pytest.raises(DocumentCompleteSourceUnavailable, match='not ready'):
        await flow.admission.activate(
            scope=scope, plan_ref=plan_ref, source_reader=flow.api, source_snapshot_id=captured.source_snapshot_ref
        )
    ready = await flow.fresh.authorize_fresh_source(authorization, scope=flow.profile.scope)
    assert ready.status == 'ready'
    archive = ProfileSourceMaterialArchive(
        report_store=MongoHealthReportStore(flow.collections['reports'], flow.collections['revisions']),
        publication_store=flow.profile.inbox.store,
        clock=lambda: flow.profile.now[0],
    )
    original = flow.bundle.understanding.payload
    archived = await archive.archive_source_material(
        scope=flow.profile.scope,
        canonical_report_id=source_id,
        report_id=source_id,
        source_fingerprint=original['documentMetadata']['sourceFingerprint'],
        document_id=flow.bundle.material.payload['documentId'],
        result=original['interpretation'],
    )
    assert archived.legacy_source_revision == 1
    assert not flow.profile.collections['jobs'].documents
    assert (
        await flow.presence.read_legacy_presence(flow.profile.scope, source_type='health_report', source_id=source_id)
    ).status == 'present'
    await flow.admission.activate(
        scope=scope, plan_ref=plan_ref, source_reader=flow.api, source_snapshot_id=captured.source_snapshot_ref
    )
    assert (await flow.admission.read_status(scope=scope, source_id=source_id))['mode'] == 'events'
    assert not await flow.admission.is_admission_fence_held(
        scope=scope,
        snapshot_id=authorization.owner_fence_ref.snapshot_id,
        content_digest=authorization.owner_fence_digest,
    )
    monkeypatch.setattr(
        flow.presence, 'read_legacy_presence', AsyncMock(side_effect=AssertionError('no new absence after ready'))
    )
    monkeypatch.setattr(
        flow.admission.history, 'read_absence', AsyncMock(side_effect=AssertionError('no current owner history'))
    )
    assert await flow.fresh.authorize_fresh_source(authorization, scope=flow.profile.scope) == ready
    assert await flow.admission.prepare_fresh_plan(scope=scope, prepared=flow.item, request_id='fresh') == plan_ref
    assert await flow.api.read_source_observations(scope=scope, snapshot_id=captured.source_snapshot_ref)
    received = await flow.publisher.publish(
        scope=scope, snapshot_id=captured.source_snapshot_ref, request_id='event', expected_prior_revision=None
    )
    assert received.publication_revision == 1 and received.state != 'applied'
    assert await flow.profile.worker.recover_pending() == 1
    flow.profile.now[0] += timedelta(minutes=1)
    await flow.recovery.recover_pending()
    assert (await flow.lifecycle.read(scope=scope, source_id=source_id)).status == 'active'
    assert await visible(flow.profile)
    await flow.publisher.retract(
        scope=scope, source_id=source_id, source_revision=1, request_id='withdraw', expected_prior_revision=1
    )
    assert await flow.profile.worker.recover_pending() == 1
    flow.profile.now[0] += timedelta(minutes=1)
    await flow.recovery.recover_pending()
    assert not await visible(flow.profile)
    assert (await flow.lifecycle.read(scope=scope, source_id=source_id)).status == 'withdrawn'
    assert flow.collections['reports'].documents  # Genuine old history survives the source withdrawal.


@pytest.mark.asyncio
async def test_real_profile_history_precheck_leaves_legacy_gateway_available():
    flow = await composition()
    flow.collections['jobs'].documents.append(
        {
            '_id': 'old-completed',
            'userId': flow.scope.user_id,
            'subjectId': None,
            'sourceType': 'health_report',
            'sourceId': flow.bundle.material.source_id,
            'status': 'completed',
        }
    )
    with pytest.raises(DocumentCompleteSourceUnavailable, match='absence is unavailable'):
        await flow.admission.prepare_fresh_plan(scope=flow.scope, prepared=flow.item, request_id='fresh')
    assert await flow.env.ledger.read_source(flow.scope, flow.bundle.material.source_id) is None
    legacy = await flow.env.api.publish_prepared(
        scope=flow.scope,
        understanding_snapshot_id=flow.item.understanding_snapshot_ref,
        selection=flow.item.selection,
        preparation_receipt=flow.item.preparation_receipt,
    )
    assert legacy.canonical_report_id == flow.bundle.material.source_id and legacy.legacy_source_revision == 1


@pytest.mark.asyncio
async def test_real_profile_history_race_before_issued_proof_restores_legacy(monkeypatch):
    flow = await composition()
    hold = flow.env.ledger.hold_fresh_admission

    async def raced(*args, **kwargs):
        fence = await hold(*args, **kwargs)
        flow.collections['jobs'].documents.append(
            {
                '_id': 'raced-job',
                'userId': flow.scope.user_id,
                'subjectId': None,
                'sourceType': 'health_report',
                'sourceId': flow.bundle.material.source_id,
                'status': 'failed',
            }
        )
        return fence

    monkeypatch.setattr(flow.env.ledger, 'hold_fresh_admission', raced)
    with pytest.raises(DocumentCompleteSourceUnavailable, match='absence is unavailable'):
        await flow.admission.prepare_fresh_plan(scope=flow.scope, prepared=flow.item, request_id='fresh')
    status = await flow.admission.read_status(scope=flow.scope, source_id=flow.bundle.material.source_id)
    assert status['mode'] == 'legacy' and not status['ownerProofIssued']
    assert (
        await flow.profile.inbox.get_cutover(flow.profile.scope, 'health_report', flow.bundle.material.source_id)
        is None
    )
    assert not flow.admission_records.documents or all(
        row['kind'] != 'owner_fence' for row in flow.admission_records.documents.values()
    )
    await flow.env.ledger.begin_legacy(flow.scope, flow.bundle.material.source_id, 'resumed')


@pytest.mark.asyncio
async def test_real_profile_ready_and_document_activation_lost_ack_replay_same_authorization(monkeypatch):
    flow = await composition()
    plan_ref, captured, authorization = await capture(flow)
    await flow.fresh.authorize_fresh_source(authorization, scope=flow.profile.scope)
    update = flow.env.ledger.sources.update_one
    failed = False

    async def lost_ack(query, values):
        nonlocal failed
        result = await update(query, values)
        if values['$set'].get('mode') == 'events' and not failed:
            failed = True
            raise RuntimeError('lost activation ACK')
        return result

    monkeypatch.setattr(flow.env.ledger.sources, 'update_one', lost_ack)
    kwargs = dict(
        scope=flow.scope, plan_ref=plan_ref, source_reader=flow.api, source_snapshot_id=captured.source_snapshot_ref
    )
    with pytest.raises(RuntimeError, match='lost activation'):
        await flow.admission.activate(**kwargs)
    await flow.admission.activate(**kwargs)
    assert (await flow.admission.read_status(scope=flow.scope, source_id=flow.bundle.material.source_id))[
        'mode'
    ] == 'events'
    with pytest.raises(PublicationModeError):
        await flow.env.ledger.begin_legacy(flow.scope, flow.bundle.material.source_id, 'must-stay-blocked')


def test_public_factories_construct_with_no_model_database_or_activation_calls():
    class LazyCollection:
        def __init__(self):
            self.options = []

        def with_options(self, **kwargs):
            self.options.append(kwargs)
            return self

    ledger = object()
    compatibility_rows, report_rows, admission_rows, observations = (LazyCollection() for _ in range(4))
    compatibility = SimpleNamespace(
        legacy_publication=SimpleNamespace(ledger=ledger),
        publications=SimpleNamespace(collection=compatibility_rows),
        understanding=SimpleNamespace(snapshots=object()),
    )
    report_publications = SimpleNamespace(compatibility=compatibility, commands=SimpleNamespace(collection=report_rows))
    profile = SimpleNamespace(read_legacy_presence=AsyncMock(), get_cutover=AsyncMock())
    admission = create_document_fresh_source_admission(
        compatibility=compatibility,
        report_publications=report_publications,
        ledger=ledger,
        admission_records=admission_rows,
        observation_collection=observations,
        profile_presence=profile,
        profile_inbox=profile,
    )
    source = create_document_complete_source_service(
        snapshots=compatibility.understanding.snapshots,
        understanding=compatibility.understanding,
        report_publications=report_publications,
        admission=admission,
    )
    recovery = SimpleNamespace(ledger=ledger, receiver=object())
    publisher = create_document_complete_source_publisher(
        source_reader=source,
        snapshots=source.snapshots,
        publication_runtime=SimpleNamespace(ledger=ledger, recovery=recovery),
    )
    assert publisher.ledger is ledger and publisher.recovery is recovery
    assert admission_rows.options[0]['write_concern'].document == {'w': 'majority'}
    assert all(value.options for value in (compatibility_rows, report_rows, admission_rows, observations))
    profile.read_legacy_presence.assert_not_awaited()
    profile.get_cutover.assert_not_awaited()
