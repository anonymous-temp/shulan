"""Callback polling configuration must not change generation/recovery schedules."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from runtime import application


@pytest.mark.parametrize('with_sources', [False, True])
@pytest.mark.parametrize('with_delegation', [False, True])
def test_callback_poll_interval_is_applied_without_delaying_initial_scan(with_delegation, with_sources):
    periodic = []

    def make_periodic(callback, interval):
        timer = SimpleNamespace(callback=callback, interval=interval, start=Mock(), stop=Mock())
        periodic.append(timer)
        return timer

    container = SimpleNamespace(
        chat_service=SimpleNamespace(drainPendingCustomMessages=AsyncMock()),
        health_profile_ingestion_worker=SimpleNamespace(drain_pending=AsyncMock()),
        record_service=SimpleNamespace(recoverPendingCreations=AsyncMock(), recoverPendingReplies=AsyncMock()),
        report_service=SimpleNamespace(drain_generation=AsyncMock(), recover_pending=AsyncMock()),
        report_callback_worker=SimpleNamespace(drain_pending=AsyncMock()),
    )
    if with_delegation:
        container.report_followup_service = SimpleNamespace(drain_generation=AsyncMock(), recover_pending=AsyncMock())
        container.report_followup_callback_worker = SimpleNamespace(drain_pending=AsyncMock())
        container.gut_barrier_service = SimpleNamespace(
            drain_generation=AsyncMock(),
            recover_pending=AsyncMock(),
            drain_delegation=AsyncMock(),
            drain_delivery=AsyncMock(),
        )
    if with_sources:
        container.source_workers = tuple(
            (name, SimpleNamespace(recover_pending=AsyncMock()))
            for name in ('document-formal', 'document-outbox', 'analysis-delivery')
        )
    bundle = SimpleNamespace(
        web_application=SimpleNamespace(settings={}), container=container, resources=SimpleNamespace(closed=True)
    )
    with (
        patch.object(application, 'REPORT_CALLBACK_POLL_INTERVAL_SECONDS', 13),
        patch.object(application.tornado.httpserver, 'HTTPServer'),
        patch.object(application.tornado.ioloop.IOLoop, 'current') as current_loop,
        patch.object(application.tornado.ioloop, 'PeriodicCallback', side_effect=make_periodic),
        patch.object(application.signal, 'signal'),
        patch.object(application.signal, 'getsignal'),
        patch.object(application, 'schedule_supervised_task', side_effect=lambda *args, name: name),
    ):
        application.run_application(bundle)
        intervals = {timer.callback(): timer.interval for timer in periodic}
        expected = {
            'worker:chat-custom-message-outbox': 1000,
            'worker:health-profile-ingestion': 1000,
            'worker:qa-creation-recovery': 15000,
            'worker:qa-reply-recovery': 15000,
            'worker:report-generation': 5000,
            'worker:report-recovery': 5000,
            'worker:report-callback': 13000,
        }
        if with_delegation:
            expected.update(
                {
                    'worker:report-followup-generation': 5000,
                    'worker:report-followup-recovery': 5000,
                    'worker:report-followup-callback': 13000,
                    'worker:gut-barrier-generation': 5000,
                    'worker:gut-barrier-recovery': 5000,
                    'worker:gut-barrier-delegation': 5000,
                    'worker:gut-barrier-delivery': 13000,
                }
            )
        if with_sources:
            expected.update({f'worker:{name}': 5000 for name, _ in container.source_workers})
        assert intervals == expected
        for timer in periodic:
            timer.start.assert_called_once()
            timer.stop.assert_called_once()
            current_loop.return_value.add_callback.assert_any_call(timer.callback)
