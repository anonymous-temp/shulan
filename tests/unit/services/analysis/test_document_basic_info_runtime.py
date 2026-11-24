"""Actual durable current Document providers with explicit synthetic Profile/execution Ports."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.basic_info import create_document_basic_info_prepared_service
from adapters.analysis.basic_info import create_document_basic_info_source_runtime
from adapters.analysis.publication import MongoDocumentPublicationLedger
from modules.analysis.basic_info_managed import DocumentCurrentManagedCommand
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.current_understanding import DocumentPreparedInput
from modules.analysis.publication import PublicationConflictError
from modules.analysis.publication import PublicationReceipt
from modules.analysis.publication_recovery import DocumentPublicationRecovery
from tests.unit._support.current_document import capture
from tests.unit._support.current_document import environment
from tests.unit._support.current_document import no_value
from tests.unit.services.analysis.fakes import MemoryMongoCollection
from tests.unit.services.analysis.test_document_material_reader import BsonCollection
from tests.unit.services.analysis.test_document_snapshots import SCOPE


def collection():
    value = BsonCollection()
    value.with_options = lambda **kwargs: value
    return value


class SyntheticProfileReceiver:
    def __init__(self):
        self.receipts, self.calls = {}, []
        self.lose_ack = False

    async def lookup(self, event, *, known_receipt=None):
        return self.receipts.get(event['eventId'])

    async def deliver(self, event):
        self.calls.append(deepcopy(event))
        revision = event['publicationRevision']
        retract = revision if event['eventKind'] == 'retract' else 0
        receipt = PublicationReceipt(
            event['sourceId'],
            revision,
            event['contentDigest'],
            'applied',
            'synthetic_' + event['eventId'],
            application_state='applied',
            accepted_revision=revision,
            applied_revision=revision,
            settled_revision=revision,
            accepted_retraction_revision=retract,
            applied_retraction_revision=retract,
        )
        self.receipts[event['eventId']] = receipt
        if self.lose_ack:
            self.lose_ack = False
            raise OSError('synthetic receiver ACK loss')
        return receipt


async def runtime(*, status='complete', edit=None):
    env = await environment(status=status, edit=edit)
    ledger = MongoDocumentPublicationLedger(sources=MemoryMongoCollection(), intents=MemoryMongoCollection())
    prepared_service = create_document_basic_info_prepared_service(
        understanding=env.api, preparation_records=collection()
    )
    fixed = replace(env.bundle, receipt=env.original.receipt)
    prepared = await prepared_service.remember(fixed)
    clock = [datetime(2026, 11, 12, tzinfo=UTC)]
    receiver = SyntheticProfileReceiver()
    recovery = DocumentPublicationRecovery(
        ledger=ledger, receiver=receiver, clock=lambda: clock[0], recovery_route='current'
    )
    records = collection()
    api = create_document_basic_info_source_runtime(
        prepared=prepared_service, ledger=ledger, admission_records=records, publication_recovery=recovery
    )
    return SimpleNamespace(
        env=env,
        api=api,
        ledger=ledger,
        receiver=receiver,
        recovery=recovery,
        prepared=prepared,
        records=records,
        prepared_service=prepared_service,
        clock=clock,
        source_id=fixed.material.source_id,
    )


async def admitted(flow):
    ref = await flow.api.admission.prepare_source_plan(
        scope=SCOPE, prepared=flow.prepared, request_id='fixed-fresh', lifecycle_epoch=3
    )
    captured = await flow.api.source.capture_source(scope=SCOPE, verified_plan_ref=ref)
    plan = await flow.api.admission.read_verified_plan(scope=SCOPE, plan_ref=ref)
    source = await flow.api.source.read_source_observations(scope=SCOPE, snapshot_id=captured.source_snapshot_ref)
    return ref, captured, plan, source


async def published(flow):
    ref, captured, plan, source = await admitted(flow)
    receipt = await flow.api.publisher.publish(
        scope=SCOPE, snapshot_id=captured.source_snapshot_ref, request_id='initial', expected_prior_revision=None
    )
    assert receipt.state == 'applied'
    return ref, captured, plan, source


def command(flow, captured, *, request_id='recover', understanding=None, retired=(), prior=1, epoch=3):
    return DocumentCurrentManagedCommand(
        SCOPE,
        flow.source_id,
        request_id,
        epoch,
        prior,
        captured.source_snapshot_ref,
        understanding_snapshot_ref=understanding,
        retire_contribution_ids=retired,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('route', [None, 'default'])
async def test_current_runtime_rejects_recovery_for_another_route(route):
    flow = await runtime()
    recovery = DocumentPublicationRecovery(ledger=flow.ledger, receiver=flow.receiver, recovery_route=route)

    with pytest.raises(ValueError, match='current recovery route'):
        create_document_basic_info_source_runtime(readers=flow.api.readers, publication_recovery=recovery)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['complete', 'partial'])
async def test_durable_fresh_inventory_fixes_lifecycle_and_separates_clinical(status):
    flow = await runtime(status=status)
    ref, captured, plan, source = await admitted(flow)
    assert len(plan.active) == 4
    assert source['payload']['coverage']['lifecycleEpoch'] == plan.lifecycle_epoch == 3
    assert source['payload']['clinicalAvailable'] is True
    assert {row['status'] for row in source['payload']['quality']['details']['originalQualities']} == {status}
    (clinical,) = source['payload']['clinicalContributions']
    (observation,) = clinical['observations']
    assert observation['verificationStatus'] == 'unconfirmed'
    assert observation['rawValue'] == '2.5'
    inventory = await flow.api.source.read_source_inventory(scope=SCOPE, snapshot_id=captured.inventory_snapshot_ref)
    assert inventory['payload']['lifecycleEpoch'] == 3
    flow.api.source.snapshots.put = AsyncMock(side_effect=AssertionError('historical read must not write'))
    assert (
        await flow.api.admission.prepare_source_plan(
            scope=SCOPE, prepared=flow.prepared, request_id='fixed-fresh', lifecycle_epoch=3
        )
        == ref
    )
    assert (
        await flow.api.source.read_source_observations(scope=SCOPE, snapshot_id=captured.source_snapshot_ref) == source
    )
    with pytest.raises(DocumentCompleteSourceUnavailable, match='lifecycle'):
        await flow.api.admission.prepare_source_plan(
            scope=SCOPE, prepared=flow.prepared, request_id='fixed-fresh', lifecycle_epoch=4
        )


@pytest.mark.asyncio
async def test_no_usable_initial_contributions_keeps_archive_without_fencing_or_publication():
    def missing(raw):
        for field in ('name', 'gender', 'age'):
            no_value(raw, field)
        raw['observations'] = []
        raw['unresolvedItems'] = []

    flow = await runtime(status='partial', edit=missing)
    with pytest.raises(DocumentCompleteSourceUnavailable, match='no admissible'):
        await flow.api.admission.prepare_source_plan(
            scope=SCOPE, prepared=flow.prepared, request_id='empty', lifecycle_epoch=3
        )
    assert await flow.ledger.read_source(SCOPE, flow.source_id) is None
    assert not flow.ledger.intents.documents
    assert await flow.env.api.read_bundle(
        scope=SCOPE, understanding_snapshot_id=flow.env.bundle.understanding.snapshot_id
    )


@pytest.mark.asyncio
async def test_lost_plan_ack_and_restart_recover_the_same_fixed_inventory(monkeypatch):
    flow = await runtime()
    put = flow.api.admission.records.put
    lost = False

    async def put_once(kind, *args):
        nonlocal lost
        await put(kind, *args)
        if kind == 'current_verified_plan' and not lost:
            lost = True
            raise OSError('synthetic fixed-plan ACK loss')

    monkeypatch.setattr(flow.api.admission.records, 'put', put_once)
    with pytest.raises(OSError, match='ACK loss'):
        await flow.api.admission.prepare_source_plan(
            scope=SCOPE, prepared=flow.prepared, request_id='fixed-fresh', lifecycle_epoch=3
        )
    monkeypatch.setattr(flow.api.admission.records, 'put', put)
    refs = [
        await flow.api.admission.prepare_source_plan(
            scope=SCOPE, prepared=flow.prepared, request_id='fixed-fresh', lifecycle_epoch=3
        )
        for _ in range(2)
    ]
    assert refs[0] == refs[1]
    assert (await flow.ledger.read_source(SCOPE, flow.source_id))['currentSourceBinding']['planRef'] == refs[0]
    await flow.api.source.capture_source(scope=SCOPE, verified_plan_ref=refs[0])


@pytest.mark.asyncio
async def test_publish_ack_loss_uses_existing_recovery_lookup_without_duplicate_delivery():
    flow = await runtime()
    _, captured, _, _ = await admitted(flow)
    flow.receiver.lose_ack = True
    with pytest.raises(OSError, match='ACK loss'):
        await flow.api.publisher.publish(
            scope=SCOPE, snapshot_id=captured.source_snapshot_ref, request_id='initial', expected_prior_revision=None
        )
    flow.clock[0] += timedelta(seconds=60)
    await flow.recovery.recover_pending()
    receipt = await flow.api.publisher.publish(
        scope=SCOPE, snapshot_id=captured.source_snapshot_ref, request_id='initial', expected_prior_revision=None
    )
    assert receipt.state == 'applied' and len(flow.receiver.calls) == 1
    assert len(flow.ledger.intents.documents) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('partial', [False, True])
async def test_recover_round_carries_every_old_basic_and_complete_clinical(partial):
    flow = await runtime()
    _, captured, old_plan, _ = await published(flow)
    flow.env.raw['basicInfo']['age'] = 43
    flow.env.raw['processing']['status'] = 'partial' if partial else 'complete'
    flow.env.document['interpretationResultRound'] = 2
    newer = await capture(flow.env)
    request = command(flow, captured, understanding=newer.understanding.snapshot_id)
    result = await flow.api.managed.execute(request)
    assert result['publicationRevision'] == 2
    source = await flow.api.source.read_source_observations(scope=SCOPE, snapshot_id=result['sourceSnapshotRef'])
    assert source['payload']['clinicalAvailable'] is True
    assert [field['value'] for field in source['payload']['basicInfo'] if field['field'] == 'age'] == [42, 43]
    plan = await flow.api.managed.read_verified_plan(scope=SCOPE, plan_ref=request.command_id)
    assert set(old_plan.active) <= set(plan.active)
    assert source['payload']['coverage']['lifecycleEpoch'] == 3
    flow.api.source.snapshots.put = AsyncMock(side_effect=AssertionError('pure recovery must not write'))
    assert (await flow.api.managed.read_receipt(scope=SCOPE, command_id=request.command_id)) == result
    with pytest.raises(PublicationConflictError, match='changed'):
        await flow.api.managed.execute(replace(request, lifecycle_epoch=4))


@pytest.mark.asyncio
async def test_explicit_all_retirement_has_real_material_and_old_contributions_cannot_revive():
    flow = await runtime()
    _, captured, plan, source = await published(flow)
    ids = tuple(row['contributionId'] for row in source['payload']['contributions'])
    request = command(flow, captured, retired=ids, request_id='retire-all')
    result = await flow.api.managed.execute(request)
    retired = await flow.api.source.read_source_observations(scope=SCOPE, snapshot_id=result['sourceSnapshotRef'])
    assert retired['payload']['contributions'] == [] and retired['payload']['basicInfo'] == []
    assert retired['payload']['clinicalAvailable'] is True
    assert retired['payload']['materialSnapshotRefs'] == source['payload']['materialSnapshotRefs']
    current = await flow.api.managed.read_verified_plan(scope=SCOPE, plan_ref=request.command_id)
    assert not current.active and len(current.retired) == len(plan.active)
    resurrect = replace(
        request,
        request_id='do-not-revive',
        prior_publication_revision=2,
        prior_source_snapshot_ref=result['sourceSnapshotRef'],
        retire_contribution_ids=(),
        understanding_snapshot_ref=flow.env.bundle.understanding.snapshot_id,
    )
    with pytest.raises(DocumentCompleteSourceUnavailable, match='retired contributions'):
        await flow.api.managed.execute(resurrect)
    await flow.api.publisher.retract(
        scope=SCOPE,
        source_id=flow.source_id,
        source_revision=1,
        request_id='withdraw',
        expected_prior_revision=2,
        lifecycle_epoch=3,
    )
    assert await flow.api.managed.read_receipt(scope=SCOPE, command_id=request.command_id) == result


@pytest.mark.asyncio
async def test_managed_ack_loss_is_idempotent_and_does_not_reevaluate_source(monkeypatch):
    flow = await runtime()
    _, captured, _, source = await published(flow)
    selected = next(row['contributionId'] for row in source['payload']['contributions'] if row['kind'] == 'basic_info')
    request = command(flow, captured, retired=(selected,), request_id='retire-name')
    put = flow.api.managed.records.put

    async def lose_result(kind, *args):
        await put(kind, *args)
        if kind == 'basic_info_managed_result':
            raise OSError('synthetic result ACK loss')

    monkeypatch.setattr(flow.api.managed.records, 'put', lose_result)
    with pytest.raises(OSError, match='result ACK loss'):
        await flow.api.managed.execute(request)
    monkeypatch.setattr(flow.api.managed.records, 'put', put)
    result = await flow.api.managed.execute(request)
    assert result['publicationRevision'] == 2 and len(flow.receiver.calls) == 2
    remaining = await flow.api.source.read_source_observations(scope=SCOPE, snapshot_id=result['sourceSnapshotRef'])
    assert len(remaining['payload']['basicInfo']) == 2 and remaining['payload']['clinicalAvailable']


@pytest.mark.asyncio
async def test_unapplied_stale_or_withdrawn_source_cannot_issue_a_managed_transition():
    flow = await runtime()
    _, captured, _, source = await published(flow)
    selected = source['payload']['contributions'][0]['contributionId']
    request = command(flow, captured, retired=(selected,))
    with pytest.raises(DocumentCompleteSourceUnavailable, match='lifecycle'):
        await flow.api.managed.execute(replace(request, lifecycle_epoch=4))
    await flow.api.publisher.retract(
        scope=SCOPE,
        source_id=flow.source_id,
        source_revision=1,
        request_id='withdraw',
        expected_prior_revision=1,
        lifecycle_epoch=3,
    )
    with pytest.raises(PublicationConflictError, match='stale'):
        await flow.api.managed.execute(request)


@pytest.mark.asyncio
async def test_managed_same_fixed_round_is_noop_and_does_not_allocate_another_publication():
    flow = await runtime()
    _, captured, _, _ = await published(flow)
    request = command(flow, captured, understanding=flow.env.bundle.understanding.snapshot_id)
    result = await flow.api.managed.execute(request)
    assert result['status'] == 'unchanged' and result['publicationRevision'] == 1
    assert len(flow.receiver.calls) == len(flow.ledger.intents.documents) == 1
    assert await flow.api.managed.read_receipt(scope=SCOPE, command_id=request.command_id) == result


@pytest.mark.asyncio
async def test_mutated_persisted_lifecycle_fails_closed_without_using_current_profile_or_latest_data():
    flow = await runtime()
    _, captured, _, _ = await admitted(flow)
    row = next(value for value in flow.records.documents.values() if value['kind'] == 'current_verified_plan')
    row['payload']['plan']['lifecycle_epoch'] = 4
    with pytest.raises(DocumentCompleteSourceUnavailable, match='identity differs'):
        await flow.api.source.read_source_observations(scope=SCOPE, snapshot_id=captured.source_snapshot_ref)
    assert not flow.receiver.calls


@pytest.mark.asyncio
async def test_concurrent_managed_commands_share_one_publication_cas(monkeypatch):
    flow = await runtime()
    _, captured, _, source = await published(flow)
    ids = [row['contributionId'] for row in source['payload']['contributions'] if row['kind'] == 'basic_info']
    reserve, entries, barrier = flow.ledger.reserve, [], asyncio.Event()

    async def synchronized(*args, **kwargs):
        entries.append(True)
        if len(entries) == 2:
            barrier.set()
        await barrier.wait()
        return await reserve(*args, **kwargs)

    monkeypatch.setattr(flow.ledger, 'reserve', synchronized)
    results = await asyncio.gather(
        *(
            flow.api.managed.execute(command(flow, captured, request_id=f'parallel-{i}', retired=(identity,)))
            for i, identity in enumerate(ids[:2])
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, PublicationConflictError) for result in results) == 1
    assert sum(isinstance(result, dict) and result['publicationRevision'] == 2 for result in results) == 1
    assert (await flow.ledger.read_source(SCOPE, flow.source_id))['publicationRevision'] == 2


@pytest.mark.asyncio
async def test_current_admission_has_no_migration_gate_and_exposes_only_current_inventory():
    flow = await runtime()
    ref, captured, plan, source = await admitted(flow)
    assert plan.fence == {'admissionKind': 'current', 'evidenceRef': ref}
    inventory = await flow.api.source.read_source_inventory(scope=SCOPE, snapshot_id=captured.inventory_snapshot_ref)
    assert inventory['payload']['fence']['evidenceRef'] == inventory['payload']['activeSetEvidenceRef'] == ref
    assert not {'legacyState', 'legacySourceRevision'} & inventory['payload'].keys()
    assert 'legacySourceRevision' not in source['payload']['coverage']
    assert all('legacySourceRevision' not in row for row in source['payload']['contributions'])
    receipt = await flow.api.publisher.publish(
        scope=SCOPE, snapshot_id=captured.source_snapshot_ref, request_id='initial', expected_prior_revision=None
    )
    assert receipt.state == 'applied'
    assert 'legacySourceRevision' not in flow.receiver.calls[0]


@pytest.mark.asyncio
async def test_current_preparation_dto_roundtrips_the_actual_fixed_metadata():
    flow = await runtime()
    assert DocumentPreparedInput.from_dict(flow.prepared.as_dict()) == flow.prepared
    assert await flow.prepared_service.validate_prepared(scope=SCOPE, prepared=flow.prepared)
    flow.env.port.read_prepared_round.side_effect = AssertionError('durable validation is a pure fixed read')
    assert await flow.prepared_service.validate_prepared(scope=SCOPE, prepared=flow.prepared)


@pytest.mark.asyncio
async def test_current_retraction_contains_original_lifecycle_and_cannot_be_rebound():
    flow = await runtime()
    await published(flow)
    with pytest.raises(PublicationConflictError, match='lifecycle'):
        await flow.api.publisher.retract(
            scope=SCOPE,
            source_id=flow.source_id,
            source_revision=1,
            request_id='withdraw-wrong',
            expected_prior_revision=1,
            lifecycle_epoch=4,
        )
    await flow.api.publisher.retract(
        scope=SCOPE,
        source_id=flow.source_id,
        source_revision=1,
        request_id='withdraw',
        expected_prior_revision=1,
        lifecycle_epoch=3,
    )
    event = flow.receiver.calls[-1]
    raw = await flow.api.source.read_retraction(scope=SCOPE, snapshot_id=event['snapshotRef']['snapshotId'])
    assert raw['lifecycleEpoch'] == 3 and raw['publicationRevision'] == 2
    assert 'legacySourceRevision' not in event
    flow.env.port.read_prepared_round.assert_awaited_once()


@pytest.mark.asyncio
async def test_first_current_operation_is_fixed_but_managed_plan_advances_its_own_evidence():
    flow = await runtime()
    first, captured, _, source = await published(flow)
    with pytest.raises(PublicationConflictError, match='original current source'):
        await flow.api.admission.prepare_source_plan(
            scope=SCOPE, prepared=flow.prepared, request_id='another-first-operation', lifecycle_epoch=3
        )
    selected = source['payload']['contributions'][1]['contributionId']
    request = command(flow, captured, retired=(selected,))
    result = await flow.api.managed.execute(request)
    current = await flow.api.source.read_source_observations(scope=SCOPE, snapshot_id=result['sourceSnapshotRef'])
    inventory = await flow.api.source.read_source_inventory(
        scope=SCOPE, snapshot_id=current['payload']['coverage']['inventorySnapshotRef']['snapshotId']
    )
    assert inventory['payload']['fence'] == {'admissionKind': 'current', 'evidenceRef': request.command_id}
    assert inventory['payload']['activeSetEvidenceRef'] == request.command_id != first
    assert (await flow.ledger.read_source(SCOPE, flow.source_id))['currentSourceBinding']['planRef'] == first


@pytest.mark.asyncio
async def test_initial_ledger_ack_loss_recovers_same_durable_operation(monkeypatch):
    flow = await runtime()
    admit = flow.ledger.admit_current_source

    async def lose_ack(*args, **kwargs):
        await admit(*args, **kwargs)
        raise OSError('synthetic initial ledger ACK loss')

    monkeypatch.setattr(flow.ledger, 'admit_current_source', lose_ack)
    with pytest.raises(OSError, match='ACK loss'):
        await flow.api.admission.prepare_source_plan(
            scope=SCOPE, prepared=flow.prepared, request_id='fixed-fresh', lifecycle_epoch=3
        )
    original = (await flow.ledger.read_source(SCOPE, flow.source_id))['currentSourceBinding']
    monkeypatch.setattr(flow.ledger, 'admit_current_source', admit)
    ref, _, plan, _ = await admitted(flow)
    assert ref == original['planRef'] and plan.lifecycle_epoch == original['lifecycleEpoch'] == 3
    assert not flow.receiver.calls and not flow.ledger.intents.documents
