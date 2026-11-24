from __future__ import annotations

import asyncio
import unittest

import tornado.testing
import tornado.web

from web.lifecycle import ApplicationLifecycle
from web.lifecycle import LifecycleRequestHandler
from web.lifecycle import LifecycleState
from web.lifecycle import TaskAdmissionClosedError
from web.lifecycle import TaskSupervisor


class ApplicationLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_draining_rejects_new_requests_and_waits_for_existing(self):
        lifecycle = ApplicationLifecycle()
        lifecycle.mark_ready()
        lease = lifecycle.try_begin_request()

        self.assertIsNotNone(lease)
        self.assertEqual(lifecycle.active_request_count, 1)
        self.assertTrue(lifecycle.begin_draining())
        self.assertFalse(lifecycle.begin_draining())
        self.assertIsNone(lifecycle.try_begin_request())

        waiter = asyncio.create_task(lifecycle.wait_for_requests())
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        lease.release()
        lease.release()
        await waiter
        lifecycle.mark_stopped()

        self.assertEqual(lifecycle.state, LifecycleState.STOPPED)
        self.assertEqual(lifecycle.active_request_count, 0)


class TaskSupervisorTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_accepting_waits_without_cancelling_existing_task(self):
        supervisor = TaskSupervisor()
        started = asyncio.Event()
        release = asyncio.Event()
        completed = asyncio.Event()

        async def existing_work():
            started.set()
            await release.wait()
            completed.set()

        task = supervisor.create_task(existing_work(), name='existing-work')
        await started.wait()
        supervisor.stop_accepting()

        waiter = asyncio.create_task(supervisor.wait_idle())
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        self.assertFalse(task.cancelled())
        self.assertEqual(supervisor.active_task_count, 1)

        with self.assertRaises(TaskAdmissionClosedError):
            supervisor.create_task(asyncio.sleep(0), name='new-work')

        release.set()
        await waiter
        self.assertTrue(completed.is_set())
        self.assertEqual(supervisor.active_task_count, 0)


class _BlockingHandler(LifecycleRequestHandler):
    async def get(self):
        self.application.settings['request_started'].set()
        await self.application.settings['release_request'].wait()
        self.write({'status': 'completed'})


class LifecycleRequestHandlerTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        self.lifecycle = ApplicationLifecycle()
        self.lifecycle.mark_ready()
        self.request_started = asyncio.Event()
        self.release_request = asyncio.Event()
        return tornado.web.Application(
            [(r'/work', _BlockingHandler)],
            lifecycle=self.lifecycle,
            request_started=self.request_started,
            release_request=self.release_request,
        )

    @tornado.testing.gen_test
    async def test_existing_request_finishes_while_new_request_gets_503(self):
        existing_request = asyncio.ensure_future(
            self.http_client.fetch(self.get_url('/work')),
        )
        await self.request_started.wait()
        self.assertEqual(self.lifecycle.active_request_count, 1)

        self.lifecycle.begin_draining()
        rejected = await self.http_client.fetch(
            self.get_url('/work'),
            raise_error=False,
        )

        self.assertEqual(rejected.code, 503)
        self.assertEqual(rejected.headers['Connection'].lower(), 'close')
        self.assertEqual(self.lifecycle.active_request_count, 1)

        self.release_request.set()
        completed = await existing_request
        await self.lifecycle.wait_for_requests()

        self.assertEqual(completed.code, 200)
        self.assertEqual(self.lifecycle.active_request_count, 0)


if __name__ == '__main__':
    unittest.main()
