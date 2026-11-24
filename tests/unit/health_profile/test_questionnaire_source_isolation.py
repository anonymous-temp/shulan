"""The old Questionnaire sender never takes ownership of new or unknown modes."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from health_profile.models import ProfileIngestionSourceType
from health_profile.questionnaire_source import QuestionnaireProfileSourceService

ABSENT = object()


def submitted(*, status='pending', mode=ABSENT):
    value = {
        'userId': 'user',
        'questionnaireId': 'questionnaire',
        'sourceRevision': 1,
        'profileIngestion': {'status': status},
    }
    if mode is not ABSENT:
        value['profileIngestion']['deliveryMode'] = mode
    return value


def service(snapshot, *, pending=None):
    store = SimpleNamespace(
        get_submitted=AsyncMock(return_value=snapshot),
        mark_ingestion_queued=AsyncMock(return_value=True),
        list_pending_ingestion=AsyncMock(return_value=pending if pending is not None else [snapshot]),
    )
    queue = SimpleNamespace(enqueue=AsyncMock(return_value=SimpleNamespace(job=SimpleNamespace(job_id='legacy-job'))))
    return QuestionnaireProfileSourceService(store, queue), store, queue


@pytest.mark.parametrize('mode', ['profile-source-event.v1', 'unknown.v2', '', ' legacy ', 'LEGACY', 0, False, [], {}])
@pytest.mark.parametrize('status', ['pending', 'queued'])
@pytest.mark.asyncio
async def test_unsupported_mode_is_rejected_before_queue_shortcut_enqueue_or_mark(mode, status):
    snapshot = submitted(status=status, mode=mode)
    original = deepcopy(snapshot)
    sender, store, queue = service(snapshot)
    assert await sender.ensure_queued(user_id='user', questionnaire_id='questionnaire') is None
    store.get_submitted.assert_awaited_once_with('user', 'questionnaire')
    queue.enqueue.assert_not_awaited()
    store.mark_ingestion_queued.assert_not_awaited()
    assert snapshot == original


@pytest.mark.parametrize('mode', [ABSENT, None, 'legacy'])
@pytest.mark.parametrize('status', ['pending', 'queued'])
@pytest.mark.asyncio
async def test_supported_legacy_modes_preserve_queue_identity_and_queued_shortcut(mode, status):
    snapshot = submitted(status=status, mode=mode)
    sender, store, queue = service(snapshot)
    assert await sender.ensure_queued(user_id='user', questionnaire_id='questionnaire') is snapshot
    if status == 'queued':
        queue.enqueue.assert_not_awaited()
        store.mark_ingestion_queued.assert_not_awaited()
    else:
        queue.enqueue.assert_awaited_once_with(
            source_type=ProfileIngestionSourceType.QUESTIONNAIRE,
            source_id='questionnaire',
            source_revision=1,
            user_id='user',
            subject_id=None,
            idempotency_key='questionnaire:questionnaire:1',
        )
        store.mark_ingestion_queued.assert_awaited_once_with('user', 'questionnaire', 'legacy-job')


@pytest.mark.asyncio
async def test_missing_ingestion_object_still_uses_historical_legacy_path():
    snapshot = submitted()
    snapshot.pop('profileIngestion')
    sender, store, queue = service(snapshot)
    assert await sender.ensure_queued(user_id='user', questionnaire_id='questionnaire') is snapshot
    assert queue.enqueue.await_count == store.mark_ingestion_queued.await_count == 1


@pytest.mark.parametrize('ingestion', [None, False, [], '', 1])
@pytest.mark.asyncio
async def test_malformed_ingestion_container_never_defaults_to_legacy(ingestion):
    snapshot = submitted()
    snapshot['profileIngestion'] = ingestion
    sender, store, queue = service(snapshot)
    assert await sender.ensure_queued(user_id='user', questionnaire_id='questionnaire') is None
    assert await sender.recover_pending() == 0
    queue.enqueue.assert_not_awaited()
    store.mark_ingestion_queued.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_does_not_count_new_queued_or_unknown_rows_as_delivered():
    rows = [
        submitted(status='queued', mode='profile-source-event.v1'),
        submitted(mode={'unknown': True}),
        submitted(mode='legacy'),
        submitted(status='queued'),
    ]
    for index, row in enumerate(rows):
        row['questionnaireId'] = f'questionnaire-{index}'
    sender, store, queue = service(None, pending=rows)
    lookup = {row['questionnaireId']: row for row in rows}
    store.get_submitted.side_effect = lambda user, identity: lookup[identity]
    assert await sender.recover_pending(limit=4) == 2
    assert [call.args for call in store.get_submitted.await_args_list] == [
        ('user', 'questionnaire-2'),
        ('user', 'questionnaire-3'),
    ]
    assert queue.enqueue.await_count == store.mark_ingestion_queued.await_count == 1
    store.list_pending_ingestion.assert_awaited_once_with(limit=4)


@pytest.mark.asyncio
async def test_recovery_rechecks_current_owner_after_a_legacy_scan_result():
    old = submitted(mode='legacy')
    current = submitted(status='queued', mode='profile-source-event.v1')
    sender, store, queue = service(current, pending=[old])
    assert await sender.recover_pending() == 0
    queue.enqueue.assert_not_awaited()
    store.mark_ingestion_queued.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_transport_failure_remains_pending_and_recovers_same_identity():
    snapshot = submitted(mode='legacy')
    sender, store, queue = service(snapshot)
    queue.enqueue.side_effect = [RuntimeError('offline'), SimpleNamespace(job=SimpleNamespace(job_id='same-job'))]
    assert await sender.ensure_queued(user_id='user', questionnaire_id='questionnaire') is None
    store.mark_ingestion_queued.assert_not_awaited()
    assert await sender.recover_pending() == 1
    assert queue.enqueue.await_args_list[0].kwargs == queue.enqueue.await_args_list[1].kwargs
    store.mark_ingestion_queued.assert_awaited_once_with('user', 'questionnaire', 'same-job')
