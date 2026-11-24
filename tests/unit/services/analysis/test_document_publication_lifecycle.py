"""Real ledger persistence with deterministic receiver faults; no live services."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

import pytest

from adapters.analysis.publication import MongoDocumentPublicationLedger
from modules.analysis.publication import DocumentPublisher
from modules.analysis.publication import PublicationConflictError
from modules.analysis.publication import PublicationReceipt
from modules.analysis.publication_lifecycle import DocumentPublicationLifecycleReader
from modules.analysis.publication_recovery import DocumentPublicationRecovery
from modules.analysis.publication_recovery import PublicationLeaseLost
from modules.analysis.publication_recovery import PublicationRecoveryPolicy
from modules.analysis.snapshots import DocumentScope
from tests.unit._support.document_bson import LedgerBsonCollection as BsonCollection
from tests.unit.services.analysis.test_document_publication import activate
from tests.unit.services.analysis.test_document_publication import publish
from tests.unit.services.analysis.test_document_publication import setup as old_setup
from tests.unit.services.analysis.test_document_snapshots import SCOPE


class Receiver:
    def __init__(self):
        self.calls = []
        self.receipts = {}
        self.status = 'applied'
        self.failure = None
        self.wait = None

    async def lookup(self, event, *, known_receipt=None):
        return self.receipts.get(event['eventId'])

    async def deliver(self, event):
        self.calls.append(deepcopy(event))
        if self.wait is not None:
            await self.wait.wait()
        if self.failure == 'before':
            raise RuntimeError('transport failed before acceptance')
        revision = event['publicationRevision']
        applied = revision if self.status == 'applied' else 0
        retract = event['eventKind'] == 'retract'
        receipt = PublicationReceipt(
            event['sourceId'],
            revision,
            event['contentDigest'],
            self.status,
            'receipt_' + event['eventId'],
            application_state='applied' if self.status == 'applied' else 'not_applied',
            accepted_revision=revision,
            applied_revision=applied,
            settled_revision=applied,
            accepted_retraction_revision=revision if retract else 0,
            applied_retraction_revision=applied if retract else 0,
        )
        self.receipts[event['eventId']] = receipt
        if self.failure == 'after':
            raise RuntimeError('transport lost accepted ACK')
        return receipt


async def environment(**policy):
    env = await old_setup()
    env.ledger = MongoDocumentPublicationLedger(sources=BsonCollection(), intents=BsonCollection())
    env.receiver = Receiver()
    env.clock = SimpleNamespace(now=datetime.now(timezone.utc))
    env.recovery = DocumentPublicationRecovery(
        ledger=env.ledger,
        receiver=env.receiver,
        policy=PublicationRecoveryPolicy(**policy),
        clock=lambda: env.clock.now,
    )
    env.publisher = DocumentPublisher(
        snapshots=env.store,
        ledger=env.ledger,
        receiver=env.receiver,
        recovery=env.recovery,
    )
    env.lifecycle = DocumentPublicationLifecycleReader(ledger=env.ledger, snapshots=env.store)
    await activate(env)
    return env


def advance(env, seconds=61):
    env.clock.now += timedelta(seconds=seconds)


def intent(env):
    return next(iter(env.ledger.intents.documents.values()))


@pytest.mark.asyncio
async def test_lost_accepted_ack_is_reconciled_without_second_dispatch_or_model():
    env = await environment()
    env.receiver.failure = 'after'
    with pytest.raises(RuntimeError, match='ACK'):
        await publish(env)
    frozen = deepcopy(intent(env)['event'])
    advance(env)
    result = await env.recovery.recover_pending()
    assert result.processed == 1 and not result.truncated
    assert len(env.receiver.calls) == 1 and intent(env)['event'] == frozen
    assert intent(env)['dispatchAttempts'] == 1 and intent(env)['recoveryState'] == 'applied'
    assert (await env.lifecycle.read(scope=SCOPE, source_id=env.snapshot.source_id)).status == 'active'
    assert await publish(env) == env.receiver.receipts[frozen['eventId']]


@pytest.mark.asyncio
async def test_background_repairs_missing_outbox_without_original_http_call():
    env = await environment()
    env.ledger.intents.fail_insert = True
    with pytest.raises(RuntimeError):
        await publish(env)
    assert not env.receiver.calls
    env.ledger.intents.fail_insert = False
    result = await env.recovery.recover_pending()
    assert result.repaired == result.processed == 1
    state = await env.ledger.read_source(SCOPE, env.snapshot.source_id)
    assert state['publicationRevision'] == state['appliedRevision'] == 1 and state['outboxPending'] is False


@pytest.mark.asyncio
async def test_failed_ordinary_event_keeps_rescue_retraction_and_late_applied_cannot_resurrect():
    env = await environment()
    env.receiver.status = 'failed'
    failed = await publish(env)
    old = deepcopy(intent(env))
    assert failed.state == 'failed'
    with pytest.raises(PublicationConflictError):
        await publish(env, request_id='ordinary', event_kind='correct', expected_prior_revision=1)
    env.receiver.status = 'applied'
    await env.publisher.retract(
        scope=SCOPE,
        source_id=env.snapshot.source_id,
        source_revision=1,
        request_id='rescue',
        expected_prior_revision=1,
    )
    assert (await env.lifecycle.read(scope=SCOPE, source_id=env.snapshot.source_id)).status == 'withdrawn'
    delayed = replace(failed, state='applied', application_state='applied', applied_revision=1, settled_revision=1)
    await env.ledger.acknowledge(SCOPE, old, delayed)
    assert (await env.lifecycle.read(scope=SCOPE, source_id=env.snapshot.source_id)).status == 'withdrawn'
    assert await env.lifecycle.read_active_material(scope=SCOPE, source_id=env.snapshot.source_id) is None


@pytest.mark.asyncio
async def test_reserved_retraction_hides_old_material_even_when_acceptance_ack_is_lost():
    env = await environment()
    await publish(env)
    env.receiver.failure = 'before'
    with pytest.raises(RuntimeError):
        await env.publisher.retract(
            scope=SCOPE,
            source_id=env.snapshot.source_id,
            source_revision=1,
            request_id='withdraw',
            expected_prior_revision=1,
        )
    state = await env.lifecycle.read(scope=SCOPE, source_id=env.snapshot.source_id)
    assert state.status == 'retracting' and not state.legacy_observations_visible
    assert await env.lifecycle.read_active_material(scope=SCOPE, source_id=env.snapshot.source_id) is None


@pytest.mark.asyncio
async def test_dispatch_budget_survives_restart_and_redrive_keeps_total_usage():
    env = await environment(max_dispatches=2)
    env.receiver.failure = 'before'
    with pytest.raises(RuntimeError):
        await publish(env)
    advance(env)
    await env.recovery.recover_pending()
    assert intent(env)['recoveryState'] == 'stalled' and intent(env)['dispatchAttempts'] == 2
    advance(env)
    assert (await env.recovery.recover_pending()).processed == 0
    assert len(env.receiver.calls) == 2
    await env.recovery.redrive(scope=SCOPE, intent_id=intent(env)['_id'], reason='receiver repaired')
    env.receiver.failure = None
    await env.recovery.recover_pending()
    assert intent(env)['dispatchAttempts'] == 3 and intent(env)['redriveCount'] == 1


@pytest.mark.asyncio
async def test_restart_cannot_expand_frozen_budgets_without_explicit_redrive():
    env = await environment(max_dispatches=2, max_transport_failures=2)
    env.receiver.failure = 'before'
    with pytest.raises(RuntimeError):
        await publish(env)
    env.recovery = DocumentPublicationRecovery(
        ledger=env.ledger,
        receiver=env.receiver,
        policy=PublicationRecoveryPolicy(max_dispatches=4, max_transport_failures=4),
        clock=lambda: env.clock.now,
    )
    advance(env)
    await env.recovery.recover_pending()
    advance(env)
    await env.recovery.recover_pending()
    assert len(env.receiver.calls) == 2 and intent(env)['recoveryState'] == 'stalled'
    assert intent(env)['recoveryPolicy']['max_dispatches'] == 2
    await env.recovery.redrive(scope=SCOPE, intent_id=intent(env)['_id'], reason='owner approved increased budget')
    assert intent(env)['previousRecoveryPolicy']['max_dispatches'] == 2
    assert intent(env)['recoveryPolicy']['max_dispatches'] == 4
    assert intent(env)['dispatchAttempts'] == 2


@pytest.mark.asyncio
async def test_cancellation_keeps_dispatch_reservation_and_expired_owner_cannot_finish():
    env = await environment()
    env.receiver.wait = asyncio.Event()
    pending = asyncio.create_task(publish(env))
    while not env.receiver.calls:
        await asyncio.sleep(0)
    owner = deepcopy(intent(env))
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert intent(env)['dispatchAttempts'] == 1
    advance(env)
    env.receiver.wait = None
    await env.recovery.recover_pending()
    assert intent(env)['dispatchAttempts'] == 2
    with pytest.raises(PublicationLeaseLost):
        await env.ledger.finish_attempt(owner, now=env.recovery.now_ms(), state='pending')


@pytest.mark.asyncio
async def test_receipt_saved_before_watermark_outage_recovers_without_redelivery():
    env = await environment()
    original = env.ledger._cas

    async def broken(source, fields):
        if 'appliedRevision' in fields:
            raise RuntimeError('source unavailable')
        return await original(source, fields)

    env.ledger._cas = broken
    with pytest.raises(RuntimeError):
        await publish(env)
    env.ledger._cas = original
    advance(env)
    await env.recovery.recover_pending()
    assert len(env.receiver.calls) == 1 and intent(env)['recoveryState'] == 'applied'


@pytest.mark.asyncio
async def test_lifecycle_is_pure_scope_checked_and_detects_changed_active_binding():
    env = await environment()
    await publish(env)
    before = deepcopy(env.ledger.sources.documents)
    assert await env.lifecycle.read_active_material(scope=SCOPE, source_id=env.snapshot.source_id) == env.snapshot
    assert before == env.ledger.sources.documents
    other = DocumentScope('other', SCOPE.subject_id)
    assert (await env.lifecycle.read(scope=other, source_id=env.snapshot.source_id)).status == 'legacy'
    original = env.store.get

    async def changed(scope, snapshot_id):
        result = await original(scope, snapshot_id)
        next(iter(env.ledger.sources.documents.values()))['reservedRetractionRevision'] = 2
        return result

    env.store.get = changed
    with pytest.raises(RuntimeError, match='changed'):
        await env.lifecycle.read_active_material(scope=SCOPE, source_id=env.snapshot.source_id)


@pytest.mark.asyncio
async def test_receiver_failure_does_not_abort_other_sources_in_same_bounded_scan():
    env = await environment()
    env.receiver.failure = 'before'
    with pytest.raises(RuntimeError):
        await publish(env)
    first_id = intent(env)['_id']
    second = replace(env.snapshot, source_id='other_source')
    await env.store.put(second)
    env.snapshot = second
    await activate(env)
    with pytest.raises(RuntimeError):
        await publish(env, request_id='other')
    old_lookup = env.receiver.lookup

    async def lookup(event, *, known_receipt=None):
        if event['eventId'] == first_id:
            raise OSError('one receiver unavailable')
        return await old_lookup(event)

    env.receiver.lookup = lookup
    env.receiver.failure = None
    advance(env)
    batch = await env.recovery.recover_pending()
    assert batch.processed == batch.deferred == 1


@pytest.mark.asyncio
async def test_deadline_is_persisted_and_requires_explicit_redrive():
    env = await environment(deadline_seconds=1)
    env.receiver.status = 'waiting'
    await publish(env)
    advance(env)
    await env.recovery.recover_pending()
    assert intent(env)['recoveryState'] == 'stalled'
    assert intent(env)['recoveryError'] == 'document.publication_deadline_exceeded'
    assert len(env.receiver.calls) == 1


@pytest.mark.asyncio
async def test_invalid_receipt_watermarks_do_not_change_durable_state():
    env = await environment()
    receipt = await publish(env)
    before = deepcopy(env.ledger.intents.documents)
    with pytest.raises(PublicationConflictError):
        await env.ledger.acknowledge(SCOPE, intent(env), replace(receipt, accepted_revision=2))
    assert before == env.ledger.intents.documents


@pytest.mark.parametrize(
    'changes',
    [
        {'state': 'applied', 'application_state': 'applied', 'applied_revision': 0},
        {'state': 'partial', 'application_state': 'partial', 'settled_revision': 1},
        {'state': 'superseded', 'application_state': 'superseded_by_retraction'},
    ],
)
def test_terminal_receipt_requires_corresponding_application_or_retraction_proof(changes):
    with pytest.raises(ValueError):
        PublicationReceipt('report', 1, 'd' * 64, receipt_ref='receipt', **changes)


@pytest.mark.parametrize('changes', [{'max_dispatches': True}, {'io_timeout_seconds': 30}, {'poll_seconds': 0}])
def test_invalid_recovery_budget_is_rejected(changes):
    with pytest.raises(ValueError):
        PublicationRecoveryPolicy(**changes)
