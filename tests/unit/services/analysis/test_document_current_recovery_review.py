"""Shared Document ledger recovery keeps the default and current transports isolated."""

from copy import deepcopy
from datetime import timedelta

import pytest
from pymongo import ReadPreference
from pymongo.write_concern import WriteConcern

from adapters.analysis.publication_runtime import create_document_publication_runtime
from modules.analysis.publication_recovery import PublicationRecoveryError
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import digest
from tests.unit._support.current_document_profile import composition
from tests.unit.health_profile.test_document_source_contract import retract as default_retraction
from tests.unit.health_profile.test_document_source_contract import setup_document
from tests.unit.services.analysis.test_document_basic_info_runtime import collection
from tests.unit.services.analysis.test_document_publication import evidence


async def shared_composition(monkeypatch):
    current = await composition(monkeypatch)
    default = await setup_document()
    default.scope_document = DocumentScope(default.scope.user_id, default.scope.subject_id)
    for store in (current.ledger.sources, current.ledger.intents):
        store.read_preference = ReadPreference.PRIMARY
        store.write_concern = WriteConcern(w='majority')
    default.recovery = create_document_publication_runtime(
        ledger=current.ledger,
        observation_collection=collection(),
        snapshots=object(),
        profile_inbox=default.inbox,
        legacy_ingestion_reader=object(),
        clock=lambda: current.now[0],
    ).recovery
    await current.ledger.switch_mode(
        default.scope_document, default.event.source_id, target='events', evidence=evidence()
    )
    return current, default


def _draft(event):
    return event.model_dump(
        mode='json', by_alias=True, exclude={'event_id', 'publication_revision', 'prior_revision', 'occurred_at'}
    )


def _published_intent(flow, scope, source_id, request_id):
    key = 'document_intent_' + digest([scope.as_dict(), source_id, request_id])
    return flow.ledger.intents.documents[key]


async def _settle(current, default):
    assert await current.profile.worker.recover_pending() == 1
    assert await default.worker.recover_pending() == 1
    current.now[0] += timedelta(minutes=1)


async def _recover(recovery, *, limit=1):
    cursor = None
    processed = 0
    for _ in range(3):
        batch = await recovery.recover_pending(limit=limit, after_intent_id=cursor)
        assert batch.deferred == 0
        processed += batch.processed
        cursor = batch.next_intent_id
        if cursor is None:
            return processed
    pytest.fail('shared retraction scanning did not reach the end of a bounded page')


@pytest.mark.asyncio
@pytest.mark.parametrize('first', ['default', 'current'])
@pytest.mark.parametrize('untagged', [False, True])
async def test_mixed_publication_and_shared_retraction_preserve_both_routes_budgets(monkeypatch, first, untagged):
    current, default = await shared_composition(monkeypatch)
    current_recovery = current.runtime.recovery
    recoveries = {'current': current_recovery, 'default': default.recovery}
    other = 'current' if first == 'default' else 'default'
    routes = (first, other)
    current_source_id = current.env.bundle.material.source_id
    default_source_id = default.event.source_id

    async def verify_pair(current_row, default_row, *, revision):
        rows = {'current': current_row, 'default': default_row}
        if untagged:
            for row in rows.values():
                row.pop('recoveryRoute', None)
        untouched = deepcopy(rows[other])
        assert await _recover(recoveries[first]) == 1
        assert rows[other] == untouched
        assert await _recover(recoveries[other]) == 1
        for route, scope, source_id in (
            ('current', current.scope, current_source_id),
            ('default', default.scope_document, default_source_id),
        ):
            row = rows[route]
            assert row['dispatchAttempts'] == 1
            assert row['transportFailures'] == 0
            assert row['recoveryState'] == 'applied'
            source = await current.ledger.read_source(scope, source_id)
            assert source['appliedRevision'] == source['settledRevision'] == revision
            if revision == 2:
                assert source['appliedRetractionRevision'] == revision

    current_received = await current.runtime.publisher.publish(
        scope=current.scope,
        snapshot_id=current.capture.source_snapshot_ref,
        request_id='current-publish',
        expected_prior_revision=None,
    )
    default_row = await current.ledger.reserve(default.scope_document, _draft(default.event), 'default-publish', None)
    default_received = await default.recovery.deliver_intent(scope=default.scope_document, intent_id=default_row['_id'])
    assert current_received.state == default_received.state == 'received'
    await _settle(current, default)
    await verify_pair(
        _published_intent(current, current.scope, current_source_id, 'current-publish'),
        _published_intent(current, default.scope_document, default_source_id, 'default-publish'),
        revision=1,
    )

    await current.runtime.publisher.retract(
        scope=current.scope,
        source_id=current_source_id,
        source_revision=1,
        request_id='current-retract',
        expected_prior_revision=1,
        lifecycle_epoch=current.epoch,
    )
    default_row = await current.ledger.reserve(
        default.scope_document, _draft(default_retraction(default)), 'default-retract', 1
    )
    await default.recovery.deliver_intent(scope=default.scope_document, intent_id=default_row['_id'])
    await _settle(current, default)
    await verify_pair(
        _published_intent(current, current.scope, current_source_id, 'current-retract'),
        _published_intent(current, default.scope_document, default_source_id, 'default-retract'),
        revision=2,
    )
    for route in routes:
        assert await _recover(recoveries[route]) == 0


@pytest.mark.asyncio
async def test_direct_default_delivery_rejects_current_intent_before_claim(monkeypatch):
    current, default = await shared_composition(monkeypatch)
    await current.runtime.publisher.publish(
        scope=current.scope,
        snapshot_id=current.capture.source_snapshot_ref,
        request_id='current-publish',
        expected_prior_revision=None,
    )
    source_id = current.env.bundle.material.source_id
    row = _published_intent(current, current.scope, source_id, 'current-publish')
    current.now[0] += timedelta(minutes=1)
    before = deepcopy(row)
    with pytest.raises(PublicationRecoveryError, match='another recovery route'):
        await default.recovery.deliver_intent(scope=current.scope, intent_id=row['_id'])
    with pytest.raises(PublicationRecoveryError, match='another recovery route'):
        await default.recovery.redrive(scope=current.scope, intent_id=row['_id'], reason='manual replay')
    assert row == before


@pytest.mark.asyncio
async def test_outbox_repair_filters_the_other_route_before_bounded_page(monkeypatch):
    current, default = await shared_composition(monkeypatch)
    current_source_id = current.env.bundle.material.source_id
    default_source_id = default.event.source_id
    with monkeypatch.context() as failed:

        async def unavailable(document):
            raise OSError('synthetic outbox write interruption')

        failed.setattr(current.ledger.intents, 'insert_one', unavailable)
        with pytest.raises(OSError, match='outbox write interruption'):
            await current.runtime.publisher.publish(
                scope=current.scope,
                snapshot_id=current.capture.source_snapshot_ref,
                request_id='current-publish',
                expected_prior_revision=None,
            )
        with pytest.raises(OSError, match='outbox write interruption'):
            await current.ledger.reserve(default.scope_document, _draft(default.event), 'default-publish', None)

    current_pending = await current.ledger.read_source(current.scope, current_source_id)
    default_pending = await current.ledger.read_source(default.scope_document, default_source_id)
    assert current_pending['outboxPending'] and default_pending['outboxPending']
    assert not current.ledger.intents.documents

    batch = await default.recovery.recover_pending(limit=1)
    assert (batch.repaired, batch.processed, batch.deferred, batch.truncated) == (1, 1, 0, False)
    assert await current.ledger.read_source(current.scope, current_source_id) == current_pending
    assert not (await current.ledger.read_source(default.scope_document, default_source_id))['outboxPending']
    batch = await current.runtime.recovery.recover_pending(limit=1)
    assert (batch.repaired, batch.processed, batch.deferred, batch.truncated) == (1, 1, 0, False)
    assert not (await current.ledger.read_source(current.scope, current_source_id))['outboxPending']
    await _settle(current, default)
    assert await _recover(default.recovery) == 1
    assert await _recover(current.runtime.recovery) == 1
