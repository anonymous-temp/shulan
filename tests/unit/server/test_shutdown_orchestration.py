from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

import tornado.testing
import tornado.web

from tests.unit.server.test_record_api_contract import server
from web.lifecycle import ApplicationLifecycle
from web.lifecycle import LifecycleState
from web.lifecycle import TaskSupervisor


class _FakeHTTPServer:
    def __init__(self, events):
        self.events = events

    def stop(self):
        self.events.append('http.stop')

    async def close_all_connections(self):
        self.events.append('http.close-connections')


class _FakePeriodicCallback:
    def __init__(self, events, name):
        self.events = events
        self.name = name

    def stop(self):
        self.events.append(f'periodic.stop:{self.name}')


class _FakeIngestionWorker:
    def __init__(self, events):
        self.events = events
        self.accepting = True

    def stop_accepting(self):
        self.accepting = False
        self.events.append('ingestion.stop-accepting')


class _FakeIOLoop:
    def __init__(self, events):
        self.events = events

    def stop(self):
        self.events.append('ioloop.stop')


class ShutdownOrchestrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_waits_for_request_and_all_registered_tasks(self):
        events = []
        lifecycle = ApplicationLifecycle()
        lifecycle.mark_ready()
        request_lease = lifecycle.try_begin_request()
        supervisor = TaskSupervisor()
        first_release = asyncio.Event()
        late_release = asyncio.Event()

        async def existing_task():
            await first_release.wait()
            events.append('task.complete:existing')

        async def task_registered_by_existing_request():
            await late_release.wait()
            events.append('task.complete:late')

        first_task = supervisor.create_task(
            existing_task(),
            name='existing-task',
        )
        ingestion_worker = _FakeIngestionWorker(events)
        app = SimpleNamespace(settings={
            'lifecycle': lifecycle,
            'task_supervisor': supervisor,
            'health_profile_ingestion_worker': ingestion_worker,
        })
        http_server = _FakeHTTPServer(events)
        callbacks = (
            _FakePeriodicCallback(events, 'outbox'),
            _FakePeriodicCallback(events, 'profile'),
        )
        io_loop = _FakeIOLoop(events)

        shutdown = asyncio.create_task(server._drain_application(
            app,
            http_server,
            callbacks,
            io_loop,
            'SIGTERM',
        ))
        await asyncio.sleep(0)

        self.assertEqual(lifecycle.state, LifecycleState.DRAINING)
        self.assertIn('http.stop', events)
        self.assertFalse(ingestion_worker.accepting)
        self.assertTrue(supervisor.accepting)
        self.assertFalse(first_task.cancelled())

        late_task = supervisor.create_task(
            task_registered_by_existing_request(),
            name='late-task',
        )
        request_lease.release()
        for _ in range(10):
            if not supervisor.accepting:
                break
            await asyncio.sleep(0)

        self.assertFalse(supervisor.accepting)
        self.assertFalse(shutdown.done())
        self.assertFalse(first_task.cancelled())
        self.assertFalse(late_task.cancelled())

        first_release.set()
        late_release.set()
        await shutdown

        self.assertEqual(lifecycle.state, LifecycleState.STOPPED)
        self.assertEqual(supervisor.active_task_count, 0)
        self.assertEqual(events[-2:], [
            'http.close-connections',
            'ioloop.stop',
        ])


class HealthProbeLifecycleTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        self.lifecycle = ApplicationLifecycle()
        self.lifecycle.mark_ready()
        self.task_supervisor = TaskSupervisor()
        return tornado.web.Application(
            [
                (r'/health/live', server.LivenessHandler),
                (r'/health/ready', server.ReadinessHandler),
            ],
            lifecycle=self.lifecycle,
            task_supervisor=self.task_supervisor,
        )

    @tornado.testing.gen_test
    async def test_readiness_fails_but_liveness_survives_drain(self):
        ready = await self.http_client.fetch(self.get_url('/health/ready'))
        self.assertEqual(ready.code, 200)

        self.lifecycle.begin_draining()
        draining = await self.http_client.fetch(
            self.get_url('/health/ready'),
            raise_error=False,
        )
        live = await self.http_client.fetch(self.get_url('/health/live'))

        self.assertEqual(draining.code, 503)
        self.assertEqual(live.code, 200)


if __name__ == '__main__':
    unittest.main()
