"""Periodic source workers do not overlap and retain bounded scan continuation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from runtime.source_recovery import DocumentOutboxRecovery
from runtime.source_recovery import SourceRecoveryRunner


@pytest.mark.asyncio
async def test_slow_source_recovery_cannot_overlap_and_shutdown_stops_admission():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def recover():
        calls.append(True)
        entered.set()
        await release.wait()
        return 7

    runner = SourceRecoveryRunner(recover)
    first = asyncio.create_task(runner.recover_pending())
    await entered.wait()
    assert await runner.recover_pending() is None
    runner.stop_accepting()
    release.set()
    assert await first == 7
    assert await runner.recover_pending() is None
    assert len(calls) == 1 and not runner.running


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError, asyncio.CancelledError])
async def test_failed_or_cancelled_tick_releases_only_process_overlap_guard(error):
    operation = AsyncMock(side_effect=[error(), 3])
    runner = SourceRecoveryRunner(operation)
    with pytest.raises(error):
        await runner.recover_pending()
    assert await runner.recover_pending() == 3
    assert operation.await_count == 2


@pytest.mark.asyncio
async def test_document_outbox_uses_each_seek_cursor_and_wraps_after_last_page():
    recovery = SimpleNamespace(
        recover_pending=AsyncMock(
            side_effect=[
                SimpleNamespace(next_source_id='source-100', next_intent_id='intent-100'),
                SimpleNamespace(next_source_id=None, next_intent_id='intent-200'),
                SimpleNamespace(next_source_id=None, next_intent_id=None),
                SimpleNamespace(next_source_id=None, next_intent_id=None),
            ]
        )
    )
    runner = SourceRecoveryRunner(DocumentOutboxRecovery(recovery).recover_pending)
    for _ in range(4):
        await runner.recover_pending()
    assert [call.kwargs for call in recovery.recover_pending.await_args_list] == [
        {'after_source_id': None, 'after_intent_id': None},
        {'after_source_id': 'source-100', 'after_intent_id': 'intent-100'},
        {'after_source_id': None, 'after_intent_id': 'intent-200'},
        {'after_source_id': None, 'after_intent_id': None},
    ]


@pytest.mark.asyncio
async def test_failed_page_keeps_previous_cursors_for_idempotent_retry():
    recovery = SimpleNamespace(
        recover_pending=AsyncMock(
            side_effect=[
                SimpleNamespace(next_source_id='source-1', next_intent_id='intent-1'),
                TimeoutError(),
                SimpleNamespace(next_source_id=None, next_intent_id=None),
            ]
        )
    )
    cursor = DocumentOutboxRecovery(recovery)
    await cursor.recover_pending()
    with pytest.raises(TimeoutError):
        await cursor.recover_pending()
    await cursor.recover_pending()
    assert recovery.recover_pending.await_args_list[-1].kwargs == {
        'after_source_id': 'source-1',
        'after_intent_id': 'intent-1',
    }
