import asyncio
import json
from unittest.mock import patch

import httpx
import tornado.testing
import tornado.web

from adapters.report.delivery import ReportCallbackTransport
from modules.report.durable import ReportCallbackWorker
from modules.report.task_coordinator import ReportTaskCoordinator
from runtime import ApplicationLifecycle
from runtime import TaskSupervisor
from tests.unit.report.test_durable_reports import complete
from tests.unit.report.test_durable_reports import setup
from web.handlers.report import ReportGenerateHandler
from web.handlers.report_query import ReportGetHandler
from web.handlers.report_query import ReportListHandler


class Receiver(tornado.web.RequestHandler):
    async def post(self):
        self.application.settings['received'].append((
            self.request.headers['Idempotency-Key'], json.loads(self.request.body),
        ))
        # Simulate committing business processing before a failed acknowledgement.
        # A repeated delivery must not apply the same result a second time.
        key = self.request.headers['Idempotency-Key']
        processed = self.application.settings['processed']
        if key not in processed:
            processed[key] = json.loads(self.request.body)
        if self.application.settings['receiver_available']:
            self.set_status(204)
        else:
            self.set_status(503)
            self.write({'message': 'temporarily unavailable'})


class TestDurableReportHTTP(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        self.env = setup()
        self.supervisor = TaskSupervisor()
        lifecycle = ApplicationLifecycle()
        lifecycle.mark_ready()
        return tornado.web.Application([
            ('/api/v1/report', ReportGenerateHandler, {
                'pipeline': self.env.pipeline, 'task_supervisor': self.supervisor,
                'task_coordinator': ReportTaskCoordinator(), 'service': self.env.service,
            }),
            ('/api/v1/report/list', ReportListHandler, {'service': self.env.archive}),
            ('/api/v1/report/get', ReportGetHandler, {'service': self.env.archive}),
            ('/callback', Receiver),
        ], lifecycle=lifecycle, task_supervisor=self.supervisor, received=[], processed={}, receiver_available=False)

    async def post(self, path, body):
        return await self.http_client.fetch(self.get_url(path), method='POST',
                                           body=json.dumps(body), raise_error=False)

    @tornado.testing.gen_test
    async def test_receipt_is_durable_before_generation_finishes(self):
        release = asyncio.Event()
        execute = self.env.pipeline.execute_and_callback

        async def blocked(*args, **kwargs):
            await release.wait()
            return await execute(*args, **kwargs)

        self.env.pipeline.execute_and_callback = blocked
        response = await self.post('/api/v1/report', {'userId': 'u', 'sessionId': 's'})
        try:
            assert response.code == 200
            payload = json.loads(response.body)['data']
            assert payload['status'] == 'processing'
            assert payload['finalReportId'] in self.env.batches.documents
            assert not self.env.artifacts.documents
        finally:
            release.set()
            await self.supervisor.wait_idle()
        again = await self.post('/api/v1/report', {'userId': 'u', 'sessionId': 's'})
        await self.supervisor.wait_idle()
        assert json.loads(again.body)['data']['finalReportId'] == payload['finalReportId']
        assert self.env.pipeline.calls == 1

    @tornado.testing.gen_test
    async def test_http_requests_fill_twenty_slots_and_refill_on_individual_completion(self):
        started = [asyncio.Event() for _ in range(22)]
        release = [asyncio.Event() for _ in range(22)]
        active = peak = 0
        execute = self.env.pipeline.execute_and_callback

        async def blocked(request, **kwargs):
            nonlocal active, peak
            index = int(request.session_id)
            active += 1
            peak = max(peak, active)
            started[index].set()
            try:
                await release[index].wait()
                return await execute(request, **kwargs)
            finally:
                active -= 1

        self.env.pipeline.execute_and_callback = blocked
        try:
            for index in range(22):
                response = await self.post('/api/v1/report', {'userId': 'u', 'sessionId': str(index)})
                assert response.code == 200
                assert json.loads(response.body)['data']['status'] == 'processing'
                self.env.clock.advance(0.01)
                if index < 20:
                    # Each request starts without waiting for an older request.
                    await asyncio.wait_for(started[index].wait(), 2)
            assert active == peak == 20
            assert not started[20].is_set() and not started[21].is_set()
            release[0].set()
            await asyncio.wait_for(started[20].wait(), 2)
            assert active == 20 and not started[21].is_set()
            release[20].set()
            await asyncio.wait_for(started[21].wait(), 2)
            assert active == peak == 20
        finally:
            for event in release:
                event.set()
            await asyncio.wait_for(self.supervisor.wait_idle(), 5)
        assert self.env.pipeline.calls == 22
        assert len(self.env.artifacts.documents) == len(self.env.deliveries.documents) == 88
        assert not self.env.service._generation_tasks

    @tornado.testing.gen_test
    async def test_storage_unavailable_returns_503_and_invalid_request_still_returns_400(self):
        self.env.batches.unavailable = True
        invalid = await self.post('/api/v1/report', {'userId': 'u'})
        unavailable = await self.post('/api/v1/report', {'userId': 'u', 'sessionId': 's'})
        assert invalid.code == 400
        assert unavailable.code == 503
        assert json.loads(unavailable.body)['code'] == 503
        assert not self.env.batches.documents and self.env.pipeline.calls == 0

    @tornado.testing.gen_test
    async def test_actual_http_receiver_recovers_without_regeneration_and_failure_result_is_queryable(self):
        # Exercise real loopback HTTP even when the runner has a proxy and no
        # localhost bypass. The proxy must never receive these test callbacks.
        self.enterContext(patch.dict('os.environ', {
            'HTTP_PROXY': 'http://127.0.0.1:1', 'http_proxy': 'http://127.0.0.1:1',
            'HTTPS_PROXY': 'http://127.0.0.1:1', 'https_proxy': 'http://127.0.0.1:1',
            'ALL_PROXY': 'http://127.0.0.1:1', 'all_proxy': 'http://127.0.0.1:1',
            'NO_PROXY': '', 'no_proxy': '',
        }))
        self.env.service.callback_url = self.get_url('/callback')
        self.env.pipeline.fail_after = 0
        batch = await complete(self.env)
        transport = ReportCallbackTransport(transport=httpx.AsyncHTTPTransport(trust_env=False))
        worker = ReportCallbackWorker(store=self.env.workflow, transport=transport,
                                       clock=self.env.clock, jitter=lambda: 1)
        try:
            assert await worker.drain_pending() == 4
            assert all(row['status'] == 'retry_wait' for row in self.env.deliveries.documents.values())
            self._app.settings['receiver_available'] = True
            self.env.clock.advance(31)
            assert await worker.drain_pending() == 4
            assert await worker.drain_pending() == 0
        finally:
            await transport.aclose()
        received = self._app.settings['received']
        assert len(received) == 8
        assert len({key for key, body in received}) == 4
        assert len(self._app.settings['processed']) == 4
        for _, body in received:
            assert body['status'] == 'failed'
            if body['type'] == 'evaluate':
                assert body['error']['errorCode'] == 'REPORT_GENERATION_FAILED'
            else:
                assert body['error']['error_code'] == 'REPORT_DEPENDENCY_FAILED'
        response = await self.post('/api/v1/report/get', {
            'userId': 'u', 'finalReportId': batch['_id'], 'reportType': 'instruction',
        })
        assert response.code == 200
        data = json.loads(response.body)['data']
        assert data['generationStatus'] == data['body']['status'] == 'failed'
        assert data['delivery']['status'] == 'delivered'
        assert data['delivery']['attempts'] == 2
        listing = await self.post('/api/v1/report/list', {'userId': 'u'})
        assert json.loads(listing.body)['data']['items'][0]['generationStatus'] == 'failed'
        assert self.env.pipeline.calls == 1
