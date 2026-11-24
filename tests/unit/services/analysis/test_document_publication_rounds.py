"""Source ledger acceptance and publication compete in a single Mongo CAS."""

import asyncio

import pytest

from adapters.analysis.publication import MongoDocumentPublicationLedger
from modules.analysis.publication import PublicationConflictError
from modules.analysis.publication import PublicationReceipt
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT_V2
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import digest
from tests.unit._support.document_bson import LedgerBsonCollection

SCOPE = DocumentScope('synthetic-user', 'synthetic-subject')
SOURCE = 'synthetic-source'
DOCUMENT = 'synthetic-document'
EPOCH = 3


def event(kind, snapshot):
    return {
        'scope': SCOPE.as_dict(),
        'sourceType': 'health_report',
        'sourceId': SOURCE,
        'sourceRevision': 1,
        'eventKind': kind,
        'contentDigest': digest(snapshot),
        'snapshotRef': {'provider': 'document', 'contractVersion': COMPLETE_SOURCE_CONTRACT_V2, 'snapshotId': snapshot},
    }


async def applied(ledger, intent):
    current = intent['event']
    await ledger.acknowledge(
        SCOPE,
        intent,
        PublicationReceipt(
            SOURCE,
            current['publicationRevision'],
            current['contentDigest'],
            'applied',
            'receipt_' + current['eventId'],
            application_state='applied',
        ),
    )


async def ledger_with_applied_source():
    ledger = MongoDocumentPublicationLedger(sources=LedgerBsonCollection(), intents=LedgerBsonCollection())
    binding = {'scope': SCOPE.as_dict(), 'sourceId': SOURCE, 'sourceRevision': 1, 'planRef': 'initial-plan'}
    binding['lifecycleEpoch'] = EPOCH
    await ledger.admit_current_source(SCOPE, SOURCE, binding=binding, interpretation_rounds={DOCUMENT: 1})
    intent = await ledger.reserve(
        SCOPE,
        event('publish', 'first-snapshot'),
        'initial-request',
        None,
        current_proof={'planRef': 'initial-plan', 'lifecycleEpoch': EPOCH, 'transition': None},
    )
    await applied(ledger, intent)
    return ledger, intent['event'], binding


def predecessor(previous, command, round_number):
    return {
        'commandId': command,
        'priorPublicationRevision': previous['publicationRevision'],
        'priorSourceSnapshotRef': previous['snapshotRef']['snapshotId'],
        'priorEventId': previous['eventId'],
        'priorContentDigest': previous['contentDigest'],
        'interpretationRounds': {DOCUMENT: round_number},
    }


def noop_proof(previous, command, round_number):
    transition = predecessor(previous, command, round_number)
    return {**transition, 'lifecycleEpoch': EPOCH, 'commandDigest': digest(transition)}


async def reserve_correction(ledger, previous, command, round_number):
    return await ledger.reserve(
        SCOPE,
        event('correct', command + '-snapshot'),
        command,
        previous['publicationRevision'],
        current_proof={
            'planRef': command,
            'lifecycleEpoch': EPOCH,
            'transition': predecessor(previous, command, round_number),
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('noop_wins', [True, False])
async def test_noop_and_correction_share_the_source_cas(monkeypatch, noop_wins):
    ledger, previous, _ = await ledger_with_applied_source()
    real_cas = ledger._cas
    contenders = asyncio.Barrier(2)
    winner_committed = asyncio.Event()
    first = {'noop': True, 'correct': True}

    async def cas(source, fields):
        kind = 'noop' if 'currentNoopAcknowledgement' in fields else 'correct' if 'currentIntent' in fields else None
        if kind and first[kind]:
            first[kind] = False
            await contenders.wait()
            if (kind == 'noop') == noop_wins:
                result = await real_cas(source, fields)
                assert result
                winner_committed.set()
                return result
            await winner_committed.wait()
            assert not await real_cas(source, fields)
            return False
        return await real_cas(source, fields)

    monkeypatch.setattr(ledger, '_cas', cas)
    proof = noop_proof(previous, 'noop-3', 3)
    noop, correction = await asyncio.wait_for(
        asyncio.gather(
            ledger.accept_current_noop(SCOPE, SOURCE, proof=proof),
            reserve_correction(ledger, previous, 'correct-2', 2),
            return_exceptions=True,
        ),
        timeout=3,
    )
    row = await ledger.read_source(SCOPE, SOURCE)
    if noop_wins:
        assert noop == proof
        assert isinstance(correction, PublicationConflictError) and 'earlier accepted' in str(correction)
        assert row['publicationRevision'] == row['appliedRevision'] == 1
        assert row['acceptedInterpretationRounds'] == {DOCUMENT: 3}
        assert await ledger.read_reserved(SCOPE, SOURCE, 'correct-2') is None
    else:
        assert isinstance(noop, PublicationConflictError) and 'applied predecessor' in str(noop)
        assert correction['event']['publicationRevision'] == row['publicationRevision'] == 2
        assert row['acceptedInterpretationRounds'] == {DOCUMENT: 2}
        assert await ledger.read_current_noop(SCOPE, SOURCE, 'noop-3') is None


@pytest.mark.asyncio
async def test_noop_lost_cas_ack_is_replayable_after_correction_and_slot_archive(monkeypatch):
    ledger, previous, binding = await ledger_with_applied_source()
    real_cas = ledger._cas
    proof = noop_proof(previous, 'noop-3', 3)

    async def lose_ack(source, fields):
        result = await real_cas(source, fields)
        if fields.get('currentNoopAcknowledgement') == proof:
            assert result
            raise OSError('synthetic source CAS acknowledgement lost')
        return result

    monkeypatch.setattr(ledger, '_cas', lose_ack)
    with pytest.raises(OSError, match='acknowledgement lost'):
        await ledger.accept_current_noop(SCOPE, SOURCE, proof=proof)
    restarted = MongoDocumentPublicationLedger(sources=ledger.sources, intents=ledger.intents)
    correction = await reserve_correction(restarted, previous, 'correct-4', 4)
    await applied(restarted, correction)
    later = noop_proof(correction['event'], 'noop-5', 5)
    assert await restarted.accept_current_noop(SCOPE, SOURCE, proof=later) == later
    assert await restarted.read_current_noop(SCOPE, SOURCE, proof['commandId']) == proof
    assert await restarted.accept_current_noop(SCOPE, SOURCE, proof=proof) == proof
    replayed = await reserve_correction(restarted, previous, 'correct-4', 4)
    assert replayed['event'] == correction['event'] and replayed['receipt']['state'] == 'applied'
    with pytest.raises(PublicationConflictError, match='changed content'):
        await reserve_correction(restarted, previous, 'correct-4', 5)
    with pytest.raises(PublicationConflictError, match='changed proof'):
        await restarted.accept_current_noop(SCOPE, SOURCE, proof={**proof, 'commandDigest': 'other'})
    with pytest.raises(PublicationConflictError, match='rounds are fixed'):
        await restarted.admit_current_source(SCOPE, SOURCE, binding=binding, interpretation_rounds={DOCUMENT: 2})
    assert await restarted.admit_current_source(SCOPE, SOURCE, binding=binding, interpretation_rounds={DOCUMENT: 1})
    source = await restarted.read_source(SCOPE, SOURCE)
    assert source['acceptedInterpretationRounds'] == {DOCUMENT: 5}
    assert source['publicationRevision'] == source['appliedRevision'] == 2
    pending, _ = await restarted.scan_pending(now=0, limit=10)
    assert all(row.get('event') is not None for row in pending)
    assert await restarted.read_reserved(SCOPE, SOURCE, proof['commandId']) is None


@pytest.mark.asyncio
async def test_reserved_correction_keeps_rounds_and_recovers_after_outbox_interruption(monkeypatch):
    ledger, previous, _ = await ledger_with_applied_source()
    archive = ledger._archive

    async def fail_correction_archive(intent):
        if intent['event']['eventKind'] == 'correct':
            raise OSError('synthetic outbox interrupted')
        return await archive(intent)

    monkeypatch.setattr(ledger, '_archive', fail_correction_archive)
    with pytest.raises(OSError, match='outbox interrupted'):
        await reserve_correction(ledger, previous, 'correct-3', 3)
    source = await ledger.read_source(SCOPE, SOURCE)
    assert source['publicationRevision'] == 2 and source['appliedRevision'] == 1
    assert source['acceptedInterpretationRounds'] == {DOCUMENT: 3}
    assert source['outboxPending'] and await ledger.read_reserved(SCOPE, SOURCE, 'correct-3') is None
    restarted = MongoDocumentPublicationLedger(sources=ledger.sources, intents=ledger.intents)
    corrected = await reserve_correction(restarted, previous, 'correct-3', 3)
    assert corrected['event'] == source['currentIntent']['event']
    await applied(restarted, corrected)
    with pytest.raises(PublicationConflictError, match='earlier accepted'):
        await reserve_correction(restarted, corrected['event'], 'correct-2', 2)
    assert await restarted.read_reserved(SCOPE, SOURCE, 'correct-2') is None
    source = await restarted.read_source(SCOPE, SOURCE)
    assert source['publicationRevision'] == source['appliedRevision'] == 2
    assert source['acceptedInterpretationRounds'] == {DOCUMENT: 3}
