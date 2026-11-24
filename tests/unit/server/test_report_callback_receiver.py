"""Exercise the development receiver over loopback HTTP without deployment services."""

import json
from contextlib import asynccontextmanager
from itertools import product
from unittest.mock import patch

import httpx
import pytest
from tornado.httpserver import HTTPServer
from tornado.testing import bind_unused_port

from adapters.report.delivery import ReportCallbackTransport
from modules.report.artifacts import REPORT_TYPES
from modules.report.durable import ReportCallbackWorker
from tests.api import serve_pages
from tests.api.scripts import report_callback_server
from tests.unit.report.test_durable_reports import complete
from tests.unit.report.test_durable_reports import setup

pytestmark = pytest.mark.asyncio


@pytest.fixture(params=['page_service', 'standalone'])
def app_factory(request):
    if request.param == 'page_service':
        return lambda save_dir=None: serve_pages.make_app(callback_save_dir=save_dir)
    return lambda save_dir=None: report_callback_server.make_app(save_dir=save_dir)


@asynccontextmanager
async def receiver(app):
    socket, port = bind_unused_port()
    server = HTTPServer(app)
    server.add_socket(socket)
    try:
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', trust_env=False) as client:
            yield client
    finally:
        server.stop()
        await server.close_all_connections()


async def test_all_report_results_and_repeated_deliveries_are_acknowledged_without_disk_writes(app_factory):
    async with receiver(app_factory()) as client:
        ready = await client.get('/callback')
        assert ready.status_code == ready.json()['code'] == 200
        preflight = await client.options('/callback', headers={
            'Origin': 'http://api-page.test', 'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'Content-Type, Idempotency-Key',
        })
        assert preflight.status_code == 204
        assert 'Idempotency-Key' in preflight.headers['Access-Control-Allow-Headers']
        with patch.object(report_callback_server, 'save_report') as save:
            for report_type, status in product(REPORT_TYPES, ['success', 'partial_success', 'failed']):
                body = {'finalReportId': 'rpt_mock', 'type': report_type, 'status': status,
                        'error': {'error_code': None if status == 'success' else 'GENERATION_FAILED'},
                        'custom': [0, False, None, [], {}]}
                for _ in range(2):
                    response = await client.post('/callback', json=body,
                                                 headers={'Idempotency-Key': f'rpt_mock:{report_type}'})
                    assert response.status_code == response.json()['code'] == 200
                    assert response.json()['data'] == {'received': True, 'saved': False}
            save.assert_not_called()


@pytest.mark.parametrize('body', [b'{', b'[]', b'null', b'"text"', b'\xff'])
async def test_invalid_json_and_non_object_bodies_are_rejected(app_factory, body):
    async with receiver(app_factory()) as client:
        response = await client.post('/callback', content=body, headers={'Content-Type': 'application/json'})
        assert response.status_code == response.json()['code'] == 400


async def test_optional_debug_copies_preserve_complete_payload_and_each_duplicate(app_factory, tmp_path):
    directory = tmp_path / 'receipts'
    body = {'finalReportId': '../../outside', 'type': '../report', 'status': 'failed',
            'custom': [0, False, None, [], {}, '模拟报告']}
    async with receiver(app_factory(directory)) as client:
        for _ in range(2):
            response = await client.post('/callback', json=body)
            assert response.status_code == 200
            assert response.json()['data'] == {'received': True, 'saved': True}
    copies = list(directory.glob('*.json'))
    assert len(copies) == 2
    for file in copies:
        assert file.read_text(encoding='utf-8') == json.dumps(body, ensure_ascii=False, indent=4)
    assert set(tmp_path.iterdir()) == {directory}


async def test_debug_storage_failure_does_not_reject_receipt_or_log_a_traceback(app_factory, tmp_path, caplog):
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('existing file', encoding='utf-8')
    async with receiver(app_factory(blocked / 'receipts')) as client:
        response = await client.post('/callback', json={'status': 'failed', 'privateBody': 'do-not-log'})
        assert response.status_code == 200
        assert response.json()['data'] == {'received': True, 'saved': False}
    warnings = [record for record in caplog.records if record.name == report_callback_server.__name__]
    assert any('本地副本保存失败' in record.message for record in warnings)
    assert all(record.exc_info is None for record in warnings)
    assert 'do-not-log' not in caplog.text


async def test_post_startup_symlink_into_checkout_cannot_write_or_change_ack(app_factory, tmp_path, caplog, symlink_or_skip):
    from utils.artifact_paths import PROJECT_ROOT

    directory = tmp_path / 'receipts'
    directory.mkdir()
    app = app_factory(directory)
    directory.rmdir()
    symlink_or_skip(directory, PROJECT_ROOT, target_is_directory=True)
    async with receiver(app) as client:
        response = await client.post('/callback', json={'status': 'failed', 'privateBody': 'not-a-log-entry'})
    assert response.status_code == 200
    assert response.json()['data'] == {'received': True, 'saved': False}
    warnings = [record for record in caplog.records if record.name == report_callback_server.__name__]
    assert any('error_type=ValueError' in record.message for record in warnings)
    assert all(record.exc_info is None for record in warnings)
    assert 'not-a-log-entry' not in caplog.text


@pytest.mark.parametrize('fail_after', [None, 0, 2])
async def test_real_callback_worker_confirms_all_results_and_stops_retrying(app_factory, fail_after):
    env = setup(fail_after=fail_after)
    async with receiver(app_factory()) as client:
        env.service.callback_url = str(client.base_url.join('/callback'))
        await complete(env)
        transport = ReportCallbackTransport(transport=httpx.AsyncHTTPTransport(trust_env=False))
        worker = ReportCallbackWorker(store=env.workflow, transport=transport, clock=env.clock, jitter=lambda: 1)
        try:
            assert await worker.drain_pending() == 4
            env.clock.advance(7200)
            assert await worker.drain_pending() == 0
        finally:
            await transport.aclose()
    assert len(env.deliveries.documents) == 4
    assert all(row['status'] == 'delivered' and row['attempts'] == 1
               for row in env.deliveries.documents.values())
    assert env.pipeline.calls == 1
