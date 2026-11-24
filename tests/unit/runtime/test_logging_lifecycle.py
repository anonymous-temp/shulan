"""Logging closes after application work, including startup and resource failures."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('failure', [False, True])
def test_main_logs_configuration_and_always_flushes(monkeypatch, failure):
    import main

    events = []

    def run():
        events.append('run')
        if failure:
            raise RuntimeError('startup failed')

    monkeypatch.setattr(main, 'configure_tornado_logging', lambda: events.append('configure'))
    monkeypatch.setattr(main.env, 'summary', lambda: 'redacted configuration')
    logger = Mock()
    logger.info.side_effect = lambda *args: events.append('configuration')
    monkeypatch.setattr(main, 'getLogger', lambda name: logger)
    monkeypatch.setattr(main, 'shutdown_logging', lambda: events.append('flush'))
    monkeypatch.setitem(sys.modules, 'server', SimpleNamespace(run=run))
    if failure:
        with pytest.raises(RuntimeError, match='startup failed'):
            main.main()
    else:
        main.main()
    assert events == ['configure', 'configuration', 'run', 'flush']
    logger.info.assert_called_once_with('%s', 'redacted configuration')


@pytest.mark.parametrize('failure', [False, True])
def test_runtime_closes_resources_before_flushing(monkeypatch, failure):
    from runtime import application

    events = []
    container = SimpleNamespace(
        chat_service=SimpleNamespace(drainPendingCustomMessages=AsyncMock()),
        health_profile_ingestion_worker=SimpleNamespace(drain_pending=AsyncMock()),
        record_service=SimpleNamespace(recoverPendingCreations=AsyncMock()),
        report_service=None,
        report_callback_worker=None,
    )
    bundle = SimpleNamespace(web_application=SimpleNamespace(settings={}), container=container,
                             resources=SimpleNamespace(closed=False), aclose=AsyncMock())

    def close_resources(callback):
        assert callback is bundle.aclose
        events.append('resources')
        if failure:
            raise RuntimeError('resource close failed')

    loop = Mock()
    loop.start.side_effect = lambda: events.append('serve')
    loop.run_sync.side_effect = close_resources
    monkeypatch.setattr(application.tornado.httpserver, 'HTTPServer', Mock())
    monkeypatch.setattr(application.tornado.ioloop.IOLoop, 'current', lambda: loop)
    monkeypatch.setattr(application.tornado.ioloop, 'PeriodicCallback', Mock())
    monkeypatch.setattr(application.signal, 'signal', Mock())
    monkeypatch.setattr(application.signal, 'getsignal', Mock())
    monkeypatch.setattr(application, 'configure_tornado_logging', lambda: events.append('configure'))
    monkeypatch.setattr(application, 'shutdown_logging', lambda: events.append('flush'))
    if failure:
        with pytest.raises(RuntimeError, match='resource close failed'):
            application.run_application(bundle)
    else:
        application.run_application(bundle)
    assert events == ['configure', 'serve', 'resources', 'flush']
