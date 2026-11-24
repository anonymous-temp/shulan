"""Actual Document and Profile consumers on shared preparations."""

import asyncio
from dataclasses import replace

import pytest

from adapters.analysis.publication import MongoDocumentPublicationLedger
from modules.analysis.application.documents import DocumentBusyError
from modules.analysis.compatibility import DocumentPreparationConflictError
from modules.analysis.publication import PublicationConflictError
from tests.unit._support.current_document import UPLOAD
from tests.unit._support.current_document import model_report
from tests.unit._support.current_document import no_value
from tests.unit._support.current_document_profile import settle
from tests.unit._support.current_report_projection import composition
from tests.unit._support.current_report_projection import next_round
from tests.unit._support.current_report_projection import projection
from tests.unit._support.current_report_projection import report_contributions
from tests.unit._support.current_report_projection import source
from tests.unit._support.current_report_projection import wire_projection


async def consume(flow, bundle, request, **options):
    return await flow.runtime.consumption.consume_prepared(
        scope=flow.scope,
        prepared=flow.prepared.input(bundle),
        lifecycle_epoch=options.pop('lifecycle_epoch', flow.epoch),
        request_id=request,
        **options,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('report_first', (True, False))
async def test_consumers_share_initial_source_and_reuse_fixed_receipt(monkeypatch, report_first):
    flow = await composition(monkeypatch, [model_report()])
    bundle = await flow.prepared.prepare_bundle(scope=flow.scope, **UPLOAD)
    fixed = await projection(flow, 'report-input', bundle)
    options = {'report_projection_ref': fixed.snapshot_id}
    first = await consume(flow, bundle, 'first', **(options if report_first else {}))
    assert first['status'] == 'published'
    assert first['publicationRevision'] == 1
    assert first['publication']['state'] == 'received'
    second_options = {} if report_first else options
    if not report_first:
        with pytest.raises(DocumentBusyError, match='must be applied'):
            await consume(flow, bundle, 'second', **second_options)
        await settle(flow)
    second = await consume(flow, bundle, 'second', **second_options)
    if report_first:
        assert second['status'] == 'unchanged'
        assert second['sourceSnapshotRef'] == first['sourceSnapshotRef']
        assert second['publicationRevision'] == 1
        assert second['publication'] == first['publication']
    else:
        assert second['status'] == 'published'
        assert second['publicationRevision'] == 2
    assert report_contributions(await source(flow, second['sourceSnapshotRef']))
    await settle(flow)

    flow.ledger = MongoDocumentPublicationLedger(sources=flow.ledger.sources, intents=flow.ledger.intents)
    wire_projection(flow)
    assert await consume(flow, bundle, 'first', **(options if report_first else {})) == first
    assert await consume(flow, bundle, 'second', **second_options) == second
    with pytest.raises(PublicationConflictError, match='changed its fixed inputs or lifecycle'):
        await consume(flow, bundle, 'first', **({} if report_first else options))
    with pytest.raises(PublicationConflictError, match='lifecycle'):
        await consume(flow, bundle, 'different-lifecycle', lifecycle_epoch=flow.epoch + 1)


@pytest.mark.asyncio
async def test_interruption_after_first_publication_recovers_without_rebinding(monkeypatch):
    flow = await composition(monkeypatch, [model_report()])
    bundle = await flow.prepared.prepare_bundle(scope=flow.scope, **UPLOAD)
    store = flow.readers.records
    persist = store.put

    async def fail_after_publication(kind, *args):
        if kind == 'current_consumer_result':
            raise OSError('result persistence interruption')
        await persist(kind, *args)

    with monkeypatch.context() as fault:
        fault.setattr(store, 'put', fail_after_publication)
        with pytest.raises(OSError, match='persistence interruption'):
            await consume(flow, bundle, 'recover')
    original = await flow.ledger.read_source(flow.scope, bundle.material.source_id)
    await settle(flow)

    flow.ledger = MongoDocumentPublicationLedger(sources=flow.ledger.sources, intents=flow.ledger.intents)
    wire_projection(flow)
    fixed = await projection(flow, 'later-fixed-report', bundle)
    newer = await consume(flow, bundle, 'newer-consumer', report_projection_ref=fixed.snapshot_id)
    assert newer['publicationRevision'] == 2
    resumed = await consume(flow, bundle, 'recover')
    assert resumed['publicationRevision'] == 1
    event = original['currentIntent']['event']
    assert resumed['sourceSnapshotRef'] == event['snapshotRef']['snapshotId']
    assert resumed['publication']['content_digest'] == event['contentDigest']
    saved = await flow.ledger.read_source(flow.scope, bundle.material.source_id)
    assert saved['publicationRevision'] == 2


@pytest.mark.asyncio
async def test_wrong_fixed_preparation_does_not_publish(monkeypatch):
    flow = await composition(monkeypatch, [model_report()])
    bundle = await flow.prepared.prepare_bundle(scope=flow.scope, **UPLOAD)
    prepared = flow.prepared.input(bundle)
    wrong = replace(prepared, facts_snapshot_ref=prepared.material_snapshot_ref)
    with pytest.raises(DocumentPreparationConflictError, match='durable preparation binding differs'):
        await flow.runtime.consumption.consume_prepared(
            scope=flow.scope, prepared=wrong, lifecycle_epoch=flow.epoch, request_id='wrong-prep'
        )
    assert await flow.ledger.read_source(flow.scope, bundle.material.source_id) is None


@pytest.mark.asyncio
async def test_explicit_report_replacement_requires_applied_primary_round(monkeypatch):
    original = model_report()
    corrected = model_report()
    corrected['observations'][0]['resultValue'] = '14'
    flow = await composition(monkeypatch, [original, corrected])
    first = await flow.prepared.prepare_bundle(scope=flow.scope, **UPLOAD)
    first_projection = await projection(flow, 'first-projection', first)
    initial = await consume(flow, first, 'initial-group', report_projection_ref=first_projection.snapshot_id)
    await settle(flow)
    (active,) = report_contributions(await source(flow, initial['sourceSnapshotRef']))
    newer = await next_round(flow, first)
    newer_projection = await projection(flow, 'newer-projection', newer)
    before = await flow.ledger.read_source(flow.scope, first.material.source_id)

    with pytest.raises(PublicationConflictError, match='apply the new primary round'):
        await consume(
            flow,
            newer,
            'wrong-replacement',
            report_projection_ref=newer_projection.snapshot_id,
            replace_report_contribution_id=active['contributionId'],
        )

    assert await flow.ledger.read_source(flow.scope, first.material.source_id) == before
    assert report_contributions(await source(flow, initial['sourceSnapshotRef'])) == (active,)


@pytest.mark.asyncio
async def test_same_round_reuse_keeps_original_event_if_result_persistence_is_interrupted(monkeypatch):
    flow = await composition(monkeypatch, [model_report()])
    bundle = await flow.prepared.prepare_bundle(scope=flow.scope, **UPLOAD)
    original = await consume(flow, bundle, 'record-first')
    await settle(flow)
    store = flow.readers.records
    persist = store.put

    async def fail_result(kind, *args):
        if kind == 'current_consumer_result':
            raise OSError('synthetic unchanged receipt interruption')
        await persist(kind, *args)

    with monkeypatch.context() as fault:
        fault.setattr(store, 'put', fail_result)
        with pytest.raises(OSError, match='unchanged receipt interruption'):
            await consume(flow, bundle, 'record-reuse')

    fixed = await projection(flow, 'later-fixed-report', bundle)
    newer = await consume(flow, bundle, 'later-report', report_projection_ref=fixed.snapshot_id)
    assert newer['publicationRevision'] == 2
    flow.ledger = MongoDocumentPublicationLedger(sources=flow.ledger.sources, intents=flow.ledger.intents)
    wire_projection(flow)

    recovered = await consume(flow, bundle, 'record-reuse')
    assert recovered['status'] == 'unchanged'
    assert recovered['publicationRevision'] == 1
    assert recovered['sourceSnapshotRef'] == original['sourceSnapshotRef']
    assert recovered['publication']['receipt_ref'] == original['publication']['receipt_ref']


async def prepare_consumer_correction(monkeypatch, *, third_value=None):
    second = model_report()
    second['observations'][0]['resultValue'] = '14'
    models = [model_report(), second]
    if third_value is not None:
        third = model_report()
        third['observations'][0]['resultValue'] = third_value
        if third_value == '14':
            for field in ('name', 'gender', 'age'):
                no_value(third, field)
        models.append(third)
    flow = await composition(monkeypatch, models)
    first = await flow.prepared.prepare_bundle(scope=flow.scope, **UPLOAD)
    assert (await consume(flow, first, 'initial-record'))['publicationRevision'] == 1
    await settle(flow)
    newer = await next_round(flow, first)
    fixed = await projection(flow, 'fixed-report-round-two', newer)
    return flow, newer, fixed


async def verify_report_contribution(flow, report_result, primary):
    ledger = await flow.ledger.read_source(flow.scope, report_result['sourceId'])
    assert ledger['publicationRevision'] == ledger['appliedRevision'] == 3
    assert ledger['acceptedInterpretationRounds'] == {primary.receipt.document_id: 2}
    current = await source(flow, ledger['currentIntent']['event']['snapshotRef']['snapshotId'])
    (report,) = report_contributions(current)
    historical = await source(flow, report_result['sourceSnapshotRef'])
    assert report_contributions(historical) == (report,)
    assert report_result['publicationRevision'] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('pause_point', ['before_execute', 'before_reserve', 'restart_before_resume'])
async def test_report_keeps_same_request_after_record_wins_an_unaccepted_predecessor(monkeypatch, pause_point):
    flow, newer, fixed = await prepare_consumer_correction(monkeypatch)
    paused, release = asyncio.Event(), asyncio.Event()
    managed = flow.runtime.managed
    ledger = flow.ledger
    execute, reserve = managed.execute, ledger.reserve
    active_report_task = None

    async def pause_before_execute(command):
        if asyncio.current_task() is active_report_task and not paused.is_set():
            assert command.understanding_snapshot_ref == newer.understanding.snapshot_id
            assert command.prior_publication_revision == 1
            paused.set()
            await release.wait()
        return await execute(command)

    async def pause_before_reserve(scope, event, request_id, expected_prior, **options):
        if asyncio.current_task() is active_report_task and not paused.is_set():
            assert event['eventKind'] == 'correct'
            assert expected_prior == 1
            paused.set()
            await release.wait()
        return await reserve(scope, event, request_id, expected_prior, **options)

    report_options = {'report_projection_ref': fixed.snapshot_id}
    with monkeypatch.context() as fault:
        if pause_point == 'before_execute':
            fault.setattr(managed, 'execute', pause_before_execute)
        else:
            fault.setattr(ledger, 'reserve', pause_before_reserve)
        async with asyncio.timeout(20):
            active_report_task = asyncio.create_task(consume(flow, newer, 'report-race', **report_options))
            try:
                await paused.wait()
                winner = await consume(flow, newer, 'record-race')
                assert winner['status'] == 'published' and winner['publicationRevision'] == 2
                await settle(flow)
                if pause_point == 'restart_before_resume':
                    active_report_task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await active_report_task
                else:
                    release.set()
                    try:
                        await active_report_task
                    except DocumentBusyError:
                        pass
            finally:
                if not active_report_task.done():
                    active_report_task.cancel()
                    await asyncio.gather(active_report_task, return_exceptions=True)

    flow.ledger = MongoDocumentPublicationLedger(sources=ledger.sources, intents=ledger.intents)
    wire_projection(flow)
    reconciled = await consume(flow, newer, 'report-race', **report_options)
    assert reconciled['status'] == 'published'
    await settle(flow)
    await verify_report_contribution(flow, reconciled, newer)
    assert await consume(flow, newer, 'report-race', **report_options) == reconciled


@pytest.mark.asyncio
async def test_report_resumes_after_a_peer_applies_its_projection(monkeypatch):
    flow, newer, fixed = await prepare_consumer_correction(monkeypatch)
    options = {'report_projection_ref': fixed.snapshot_id}
    with pytest.raises(DocumentBusyError, match='must be applied'):
        await consume(flow, newer, 'report-a', **options)
    await settle(flow)
    peer = await consume(flow, newer, 'report-b', **options)
    assert peer['status'] == 'published' and peer['publicationRevision'] == 3
    await settle(flow)

    flow.ledger = MongoDocumentPublicationLedger(sources=flow.ledger.sources, intents=flow.ledger.intents)
    wire_projection(flow)
    resumed = await consume(flow, newer, 'report-a', **options)
    assert resumed['status'] == 'unchanged'
    assert resumed['sourceSnapshotRef'] == peer['sourceSnapshotRef']
    await verify_report_contribution(flow, resumed, newer)
    assert await consume(flow, newer, 'report-a', **options) == resumed


@pytest.mark.asyncio
@pytest.mark.parametrize('intervening_action', ['newer_same_content', 'newer_clinical_content', 'withdraw'])
async def test_waiting_report_rejects_a_superseded_or_withdrawn_primary(monkeypatch, intervening_action):
    third_value = {'newer_same_content': '14', 'newer_clinical_content': '16', 'withdraw': None}[intervening_action]
    flow, newer, fixed = await prepare_consumer_correction(monkeypatch, third_value=third_value)
    options = {'report_projection_ref': fixed.snapshot_id}
    with pytest.raises(DocumentBusyError, match='must be applied'):
        await consume(flow, newer, 'waiting-report', **options)
    await settle(flow)

    material = newer.material
    if intervening_action == 'withdraw':
        retraction = await flow.runtime.publisher.retract(
            scope=flow.scope,
            source_id=material.source_id,
            source_revision=material.source_revision,
            request_id='withdraw-waiting-report',
            expected_prior_revision=2,
            lifecycle_epoch=flow.epoch,
        )
        assert retraction.publication_revision == 3
    else:
        third = await next_round(flow, newer)
        if intervening_action == 'newer_same_content':
            assert third.facts.payload['clinicalContentDigest'] == newer.facts.payload['clinicalContentDigest']
        else:
            assert third.facts.payload['clinicalContentDigest'] != newer.facts.payload['clinicalContentDigest']
        advanced = await consume(flow, third, 'record-third-round')
        revision = 2 if intervening_action == 'newer_same_content' else 3
        assert advanced['publicationRevision'] == revision
    await settle(flow)
    before = await flow.ledger.read_source(flow.scope, material.source_id)
    flow.ledger = MongoDocumentPublicationLedger(sources=flow.ledger.sources, intents=flow.ledger.intents)
    wire_projection(flow)

    with pytest.raises(PublicationConflictError):
        await consume(flow, newer, 'waiting-report', **options)
    assert await flow.ledger.read_source(flow.scope, material.source_id) == before


@pytest.mark.asyncio
async def test_accepted_report_step_keeps_its_receipt_after_racing_with_outbox_interruption(monkeypatch):
    flow, newer, fixed = await prepare_consumer_correction(monkeypatch)
    assert (await consume(flow, newer, 'record-correction'))['publicationRevision'] == 2
    await settle(flow)
    ledger = flow.ledger
    archive = ledger._archive

    async def interrupt_outbox_after_reservation(intent):
        if intent['event']['publicationRevision'] == 3:
            raise OSError('report outbox write interrupted')
        return await archive(intent)

    options = {'report_projection_ref': fixed.snapshot_id}
    with monkeypatch.context() as fault:
        fault.setattr(ledger, '_archive', interrupt_outbox_after_reservation)
        with pytest.raises(OSError, match='outbox write interrupted'):
            await consume(flow, newer, 'report-durable', **options)

    reserved = await ledger.read_source(flow.scope, newer.material.source_id)
    assert reserved['outboxPending'] is True
    event = reserved['currentIntent']['event']
    assert event['publicationRevision'] == 3
    assert await ledger.read_intent(flow.scope, event['eventId']) is None
    flow.ledger = MongoDocumentPublicationLedger(sources=ledger.sources, intents=ledger.intents)
    wire_projection(flow)
    await flow.runtime.recovery.recover_pending()
    await settle(flow)

    withdrawal = await flow.runtime.publisher.retract(
        scope=flow.scope,
        source_id=newer.material.source_id,
        source_revision=newer.material.source_revision,
        request_id='withdraw-accepted-report',
        expected_prior_revision=3,
        lifecycle_epoch=flow.epoch,
    )
    assert withdrawal.publication_revision == 4
    await settle(flow)

    replayed = await consume(flow, newer, 'report-durable', **options)
    assert replayed['status'] == 'published'
    assert replayed['publicationRevision'] == 3
    assert replayed['sourceSnapshotRef'] == event['snapshotRef']['snapshotId']
    assert replayed['publication']['content_digest'] == event['contentDigest']
    receipt = await flow.ledger.read_intent(flow.scope, event['eventId'])
    assert replayed['publication']['receipt_ref'] == receipt['receipt']['receipt_ref']
    final = await flow.ledger.read_source(flow.scope, newer.material.source_id)
    assert final['publicationRevision'] == final['appliedRetractionRevision'] == 4


@pytest.mark.asyncio
async def test_report_reconciles_unaccepted_projection_step_after_peer_reservation(monkeypatch):
    flow, newer, fixed = await prepare_consumer_correction(monkeypatch)
    assert (await consume(flow, newer, 'record-correction'))['publicationRevision'] == 2
    await settle(flow)
    ledger = flow.ledger
    reserve = ledger.reserve
    paused, release = asyncio.Event(), asyncio.Event()
    original_task = None

    async def pause_report_reservation(scope, event, request_id, expected_prior, **options):
        if asyncio.current_task() is original_task and not paused.is_set():
            assert event['eventKind'] == 'correct'
            assert expected_prior == 2
            paused.set()
            await release.wait()
        return await reserve(scope, event, request_id, expected_prior, **options)

    report_options = {'report_projection_ref': fixed.snapshot_id}
    with monkeypatch.context() as fault:
        fault.setattr(ledger, 'reserve', pause_report_reservation)
        async with asyncio.timeout(20):
            original_task = asyncio.create_task(consume(flow, newer, 'report-original', **report_options))
            try:
                await paused.wait()
                peer = await consume(flow, newer, 'report-peer', **report_options)
                assert peer['status'] == 'published' and peer['publicationRevision'] == 3
                await settle(flow)
                release.set()
                try:
                    await original_task
                except DocumentBusyError:
                    pass
            finally:
                if not original_task.done():
                    original_task.cancel()
                    await asyncio.gather(original_task, return_exceptions=True)

    flow.ledger = MongoDocumentPublicationLedger(sources=ledger.sources, intents=ledger.intents)
    wire_projection(flow)
    replayed = await consume(flow, newer, 'report-original', **report_options)
    assert replayed['status'] == 'unchanged'
    assert replayed['sourceSnapshotRef'] == peer['sourceSnapshotRef']
    await verify_report_contribution(flow, replayed, newer)
    assert await consume(flow, newer, 'report-original', **report_options) == replayed


@pytest.mark.asyncio
async def test_accepted_unchanged_report_preserves_result_after_ack_loss_and_retraction(monkeypatch):
    flow, newer, fixed = await prepare_consumer_correction(monkeypatch)
    assert (await consume(flow, newer, 'record-correction'))['publicationRevision'] == 2
    await settle(flow)
    options = {'report_projection_ref': fixed.snapshot_id}
    published = await consume(flow, newer, 'report-first', **options)
    await settle(flow)
    await verify_report_contribution(flow, published, newer)
    ledger = flow.ledger
    accept = ledger.accept_current_noop

    async def lose_acknowledgement(scope, source_id, *, proof):
        await accept(scope, source_id, proof=proof)
        raise OSError('report no-change acknowledgement interrupted')

    with monkeypatch.context() as fault:
        fault.setattr(ledger, 'accept_current_noop', lose_acknowledgement)
        with pytest.raises(OSError, match='no-change acknowledgement interrupted'):
            await consume(flow, newer, 'report-unchanged', **options)

    flow.ledger = MongoDocumentPublicationLedger(sources=ledger.sources, intents=ledger.intents)
    wire_projection(flow)
    withdrawal = await flow.runtime.publisher.retract(
        scope=flow.scope,
        source_id=newer.material.source_id,
        source_revision=newer.material.source_revision,
        request_id='withdraw-after-report-noop',
        expected_prior_revision=3,
        lifecycle_epoch=flow.epoch,
    )
    assert withdrawal.publication_revision == 4
    await settle(flow)

    replayed = await consume(flow, newer, 'report-unchanged', **options)
    assert replayed['status'] == 'unchanged'
    assert replayed['publicationRevision'] == 3
    assert replayed['sourceSnapshotRef'] == published['sourceSnapshotRef']
    assert replayed['publication'] is None
    assert await consume(flow, newer, 'report-unchanged', **options) == replayed
    final = await flow.ledger.read_source(flow.scope, newer.material.source_id)
    assert final['publicationRevision'] == final['appliedRetractionRevision'] == 4
