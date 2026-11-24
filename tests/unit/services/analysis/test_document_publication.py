"""Publication crashes, concurrent writers and rollback cannot duplicate or reorder source updates."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.publication import MongoDocumentPublicationLedger
from modules.analysis.application.legacy_publication import LegacyPublicationGateway
from modules.analysis.publication import DocumentPublisher
from modules.analysis.publication import PublicationConflictError
from modules.analysis.publication import PublicationCutoverEvidence
from modules.analysis.publication import PublicationModeError
from modules.analysis.publication import PublicationReceipt
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import canonical_json
from tests.unit.services.analysis.fakes import MemoryMongoCollection
from tests.unit.services.analysis.test_document_snapshots import SCOPE
from tests.unit.services.analysis.test_document_snapshots import prepare
from tests.unit.services.analysis.test_document_snapshots import setup as snapshot_setup


def evidence(revision=0, **changes):
    return PublicationCutoverEvidence(
        **{
            'expected_publication_revision': revision,
            'receiver_applied_revision': revision,
            'receiver_accepted_retraction_revision': 0,
            'receiver_receipt_ref': 'receiver-reconciliation-1',
            'legacy_quiescence_receipt': 'old-workers-drained-1',
            **changes,
        }
    )


class Receiver:
    def __init__(self):
        self.calls = []
        self.state = 'applied'
        self.fail_after_accept = False

    async def deliver(self, event):
        self.calls.append(deepcopy(event))
        if self.fail_after_accept:
            self.fail_after_accept = False
            raise RuntimeError('receiver accepted but response was lost')
        return PublicationReceipt(
            event['sourceId'],
            event['publicationRevision'],
            event['contentDigest'],
            self.state,
            f'receipt-{event["eventId"]}',
        )


async def setup():
    env = snapshot_setup()
    snapshot = await prepare(env)
    ledger = MongoDocumentPublicationLedger(sources=MemoryMongoCollection(), intents=MemoryMongoCollection())
    receiver = Receiver()
    publisher = DocumentPublisher(snapshots=env.store, ledger=ledger, receiver=receiver)
    return SimpleNamespace(**vars(env), ledger=ledger, receiver=receiver, publisher=publisher, snapshot=snapshot)


async def activate(env):
    await env.ledger.switch_mode(SCOPE, env.snapshot.source_id, target='events', evidence=evidence())


async def publish(env, **changes):
    return await env.publisher.publish(
        **{
            'scope': SCOPE,
            'snapshot_id': env.snapshot.snapshot_id,
            'request_id': 'initial',
            'expected_prior_revision': None,
            **changes,
        }
    )


@pytest.mark.asyncio
async def test_explicit_activation_required_and_received_is_not_applied():
    env = await setup()
    with pytest.raises(PublicationModeError):
        await publish(env)
    assert env.receiver.calls == []
    await activate(env)
    env.receiver.state = 'pending'
    receipt = await publish(env)
    assert receipt.state == 'pending'
    state = await env.ledger.read_source(SCOPE, env.snapshot.source_id)
    assert state['publicationRevision'] == 1 and state['appliedRevision'] == 0
    with pytest.raises(PublicationConflictError, match='not been applied'):
        await publish(env, request_id='correct', event_kind='correct', expected_prior_revision=1)
    env.receiver.state = 'applied'
    await publish(env)
    assert (await env.ledger.read_source(SCOPE, env.snapshot.source_id))['appliedRevision'] == 1


@pytest.mark.asyncio
async def test_lost_response_and_restart_reuse_exact_frozen_event_and_permanent_request_id():
    env = await setup()
    await activate(env)
    env.receiver.fail_after_accept = True
    with pytest.raises(RuntimeError, match='response was lost'):
        await publish(env)
    env.publisher = DocumentPublisher(
        snapshots=env.store,
        ledger=MongoDocumentPublicationLedger(sources=env.ledger.sources, intents=env.ledger.intents),
        receiver=env.receiver,
    )
    await publish(env)
    await publish(env)
    assert len(env.receiver.calls) == 2 and env.receiver.calls[0] == env.receiver.calls[1]
    assert len(env.ledger.intents.documents) == 1
    with pytest.raises(PublicationConflictError, match='changed content'):
        await publish(env, event_kind='correct', expected_prior_revision=1)


@pytest.mark.asyncio
async def test_archive_failure_after_source_cas_is_recoverable_without_burning_revision():
    env = await setup()
    await activate(env)
    env.ledger.intents.fail_insert = True
    with pytest.raises(RuntimeError):
        await publish(env)
    assert env.receiver.calls == []
    state = await env.ledger.read_source(SCOPE, env.snapshot.source_id)
    assert state['publicationRevision'] == 1 and state['currentIntent']['event']['publicationRevision'] == 1
    env.ledger.intents.fail_insert = False
    await publish(env)
    assert env.receiver.calls[0]['publicationRevision'] == 1
    assert len(env.ledger.intents.documents) == 1


@pytest.mark.asyncio
async def test_applied_receipt_retries_watermark_after_ledger_outage_without_redelivery():
    env = await setup()
    await activate(env)
    original = env.ledger._cas

    async def fail_watermark(source, fields):
        if 'appliedRevision' in fields:
            raise RuntimeError('watermark outage')
        return await original(source, fields)

    env.ledger._cas = fail_watermark
    with pytest.raises(RuntimeError, match='watermark outage'):
        await publish(env)
    env.ledger._cas = original
    await publish(env)
    assert len(env.receiver.calls) == 1
    assert (await env.ledger.read_source(SCOPE, env.snapshot.source_id))['appliedRevision'] == 1


@pytest.mark.asyncio
async def test_concurrent_distinct_requests_cannot_both_allocate_same_prior_revision():
    env = await setup()
    await activate(env)
    results = await asyncio.gather(publish(env, request_id='a'), publish(env, request_id='b'), return_exceptions=True)
    assert sum(isinstance(r, PublicationReceipt) for r in results) == 1
    assert sum(isinstance(r, PublicationConflictError) for r in results) == 1
    assert len(env.receiver.calls) == len(env.ledger.intents.documents) == 1


@pytest.mark.asyncio
async def test_correction_keeps_material_revision_and_retraction_requires_current_prior():
    env = await setup()
    await activate(env)
    await publish(env)
    corrected = DocumentSnapshot(
        SCOPE,
        env.snapshot.contract_version,
        env.snapshot.source_id,
        1,
        canonical_json(
            {
                **env.snapshot.payload,
                'observations': [
                    {
                        'observationRef': '/observations/0',
                        'identityStatus': 'identity_unresolved',
                        'stableObservationId': None,
                        'label': 'corrected',
                        'rawValue': 2,
                    }
                ],
            }
        ),
    )
    await env.store.put(corrected)
    await publish(
        env, snapshot_id=corrected.snapshot_id, request_id='correct-1', event_kind='correct', expected_prior_revision=1
    )
    with pytest.raises(PublicationConflictError):
        await env.publisher.retract(
            scope=SCOPE,
            source_id=env.snapshot.source_id,
            source_revision=1,
            request_id='retract',
            expected_prior_revision=1,
        )
    await env.publisher.retract(
        scope=SCOPE,
        source_id=env.snapshot.source_id,
        source_revision=1,
        request_id='retract',
        expected_prior_revision=2,
    )
    assert [e['publicationRevision'] for e in env.receiver.calls] == [1, 2, 3]
    assert [e['priorRevision'] for e in env.receiver.calls] == [None, 1, 2]
    assert all(e['sourceRevision'] == 1 for e in env.receiver.calls)
    tombstone_event = env.receiver.calls[-1]
    tombstone = await env.api.read_retraction(scope=SCOPE, snapshot_id=tombstone_event['snapshotRef']['snapshotId'])
    assert tombstone.content_digest == tombstone_event['contentDigest']
    assert tombstone.canonical['sourceType'] == 'health_report'
    assert tombstone.canonical['publicationRevision'] == 3
    assert tombstone.canonical['priorRevision'] == 2
    assert tombstone.canonical['eventKind'] == 'retract'
    assert tombstone.canonical['sourceId'] == env.snapshot.source_id
    await env.publisher.retract(
        scope=SCOPE,
        source_id=env.snapshot.source_id,
        source_revision=1,
        request_id='retract',
        expected_prior_revision=2,
    )
    assert len(env.receiver.calls) == 3
    assert env.receiver.calls[1]['eventKind'] == 'correct'
    with pytest.raises(PublicationModeError, match='rollback'):
        await env.ledger.switch_mode(SCOPE, env.snapshot.source_id, target='legacy', evidence=evidence(3))


@pytest.mark.asyncio
async def test_legacy_pending_or_uncertain_write_blocks_cutover_and_events_disable_old_sink():
    env = await setup()
    source = SimpleNamespace(save=AsyncMock(side_effect=RuntimeError('unknown sink outcome')))
    gateway = LegacyPublicationGateway(source, env.ledger)
    with pytest.raises(RuntimeError):
        await gateway.save(user_id=SCOPE.user_id, subject_id=SCOPE.subject_id, report_id=env.snapshot.source_id)
    with pytest.raises(PublicationModeError, match='uncertain'):
        await activate(env)
    state = await env.ledger.read_source(SCOPE, env.snapshot.source_id)
    assert state['legacyTickets']
    # A trusted reconciliation must prove completion; time does not clear it.
    ticket = next(iter(state['legacyTickets']))
    await env.ledger.finish_legacy(SCOPE, env.snapshot.source_id, ticket)
    await activate(env)
    source.save.reset_mock()
    with pytest.raises(PublicationModeError, match='disabled'):
        await gateway.save(user_id=SCOPE.user_id, subject_id=SCOPE.subject_id, report_id=env.snapshot.source_id)
    source.save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'changes',
    [
        {'legacy_quiescence_receipt': ''},
        {'receiver_receipt_ref': ''},
        {'receiver_applied_revision': 1},
        {'receiver_accepted_retraction_revision': 1},
    ],
)
async def test_cutover_requires_reconciled_source_wide_evidence(changes):
    env = await setup()
    with pytest.raises(PublicationModeError):
        await env.ledger.switch_mode(SCOPE, env.snapshot.source_id, target='events', evidence=evidence(**changes))


@pytest.mark.asyncio
async def test_delayed_pending_receipt_cannot_demote_applied_revision():
    env = await setup()
    await activate(env)
    receipt = await publish(env)
    intent = next(iter(env.ledger.intents.documents.values()))
    await env.ledger.acknowledge(SCOPE, intent, replace(receipt, state='pending'))
    current = await env.ledger.intents.find_one({'_id': intent['_id']})
    assert current['receipt']['state'] == 'applied'
    assert (await env.ledger.read_source(SCOPE, env.snapshot.source_id))['appliedRevision'] == 1


@pytest.mark.asyncio
async def test_receipt_must_match_exact_snapshot_source_and_revision():
    env = await setup()
    await activate(env)
    receipt = await publish(env)
    intent = next(iter(env.ledger.intents.documents.values()))
    with pytest.raises(PublicationConflictError, match='does not identify'):
        await env.ledger.acknowledge(SCOPE, intent, replace(receipt, content_digest='wrong'))
    assert await env.ledger.read_source(DocumentScope('other', SCOPE.subject_id), env.snapshot.source_id) is None
