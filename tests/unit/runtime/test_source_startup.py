"""Required source lookup indexes finish before HTTP admission and worker scans."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from runtime import application


@pytest.mark.parametrize('failure', [False, True])
def test_source_initialization_precedes_listen_and_failure_closes_resources(monkeypatch, failure):
    events = []

    async def initialize():
        events.append('initialize')
        if failure:
            raise RuntimeError('source index unavailable')

    async def close():
        events.append('close')

    container = SimpleNamespace(
        chat_service=SimpleNamespace(drainPendingCustomMessages=AsyncMock()),
        health_profile_ingestion_worker=SimpleNamespace(drain_pending=AsyncMock()),
        record_service=SimpleNamespace(recoverPendingCreations=AsyncMock()),
        report_service=None,
        report_callback_worker=None,
        startup_initializers=(initialize,),
    )
    bundle = SimpleNamespace(
        web_application=SimpleNamespace(settings={}),
        container=container,
        resources=SimpleNamespace(closed=False),
        aclose=close,
    )
    loop = Mock()
    loop.run_sync.side_effect = lambda callback: asyncio.run(callback())
    loop.start.side_effect = lambda: events.append('serve')
    server = Mock()
    server.listen.side_effect = lambda port: events.append('listen')
    monkeypatch.setattr(application.tornado.httpserver, 'HTTPServer', lambda app: server)
    monkeypatch.setattr(application.tornado.ioloop.IOLoop, 'current', lambda: loop)
    monkeypatch.setattr(application.tornado.ioloop, 'PeriodicCallback', Mock())
    monkeypatch.setattr(application.signal, 'signal', Mock())
    monkeypatch.setattr(application.signal, 'getsignal', Mock())
    monkeypatch.setattr(application, 'configure_tornado_logging', Mock())
    monkeypatch.setattr(application, 'shutdown_logging', Mock())
    if failure:
        with pytest.raises(RuntimeError, match='source index unavailable'):
            application.run_application(bundle)
        server.listen.assert_not_called()
        loop.start.assert_not_called()
        loop.add_callback.assert_not_called()
        assert events == ['initialize', 'close']
    else:
        application.run_application(bundle)
        assert events == ['initialize', 'listen', 'serve', 'close']
    server.stop.assert_called_once()
