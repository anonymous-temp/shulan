"""One isolated upload-to-first-question acceptance run through local HTTP.

Run offline with ``python -m tests.unit.tools.qa_document_acceptance --smoke``.
Run live with ``python -m tests.api.scripts.qa_document_acceptance --live``.
``--smoke`` uses synthetic OCR/model replies. ``--live`` downloads the supplied
public URL and calls DocMind plus the configured LLM. Both modes use only memory
for document archives, report sources, profile queues, QA receipts and sessions.
No workers or callbacks run. Summaries belong outside the repository.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import sys
import time
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from utils.artifact_paths import external_artifact_path

ROOT = Path(__file__).resolve().parents[4]
SERVICE_KEYS = {
    'SHULAN_LLM_PROVIDER',
    'SHULAN_LLM_API_KEY',
    'SHULAN_LLM_DEFAULT_MODEL',
    'SHULAN_ALIYUN_DOCMIND_ACCESSKEY_ID',
    'SHULAN_ALIYUN_DOCMIND_ACCESSKEY_SECRET',
    'SHULAN_ALIYUN_DOCMIND_ENDPOINT',
}


def settings_from_file(path):
    """Select six service settings; never load database settings into the process."""
    from dotenv import dotenv_values

    selected = []
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
        if match and match[1] in SERVICE_KEYS:
            selected.append(line)
    values = dotenv_values(stream=io.StringIO('\n'.join(selected)), interpolate=False)
    result = {key: values.get(key) for key in SERVICE_KEYS}
    if not all(result.values()):
        raise ValueError('The six explicit LLM and DocMind settings are required')
    return result


def inert_environment():
    return {
        'SHULAN_ENVIRONMENT': 'test',
        'SHULAN_LOG_LEVEL': 'CRITICAL',
        'SHULAN_LLM_API_KEY': 'unused',
        'SHULAN_LLM_PROVIDER': 'unused',
        'SHULAN_LLM_DEFAULT_MODEL': 'unused',
        'SHULAN_ALIYUN_DOCMIND_ACCESSKEY_ID': 'unused',
        'SHULAN_ALIYUN_DOCMIND_ACCESSKEY_SECRET': 'unused',
        'SHULAN_ALIYUN_DOCMIND_ENDPOINT': 'https://unused.invalid',
        'SHULAN_MONGODB_URI': 'mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=10',
        'SHULAN_MONGODB_DATABASE': 'unused',
        'SHULAN_MONGODB_COLL_SESSIONS': 'unused',
        'SHULAN_MONGODB_COLL_HEALTH_REPORTS': 'unused',
        'SHULAN_REDIS_HOST': '127.0.0.1',
        'SHULAN_REDIS_PORT': '1',
        'SHULAN_REDIS_PASSWORD': '',
        'SHULAN_KNOWLEDGE_API_KEY': '',
        'SHULAN_EVIMED_API_KEY': '',
        'SHULAN_REPORT_CALLBACK_URL': 'https://unused.invalid/callback',
        'SHULAN_REPORT_PRODUCT_URL': 'https://unused.invalid/products',
    }


async def acceptance(args, settings, *, dependency_factory=None):
    output = external_artifact_path(args.output)
    import httpx
    import tornado.httpserver
    import tornado.netutil
    import tornado.web

    from tests.unit.agents.graphs.record.test_creation_recovery import creation_manager
    from tests.unit.services.analysis.test_document_reuse import reuse_flow
    from web.handlers.record import QASessionCreateHandler
    from web.handlers.record import QASessionGetHandler

    documents = reuse_flow()
    qa = creation_manager(health_report_processing_service=documents.service)
    clients = []
    parser = documents.client
    if args.live:
        clients, parser = dependency_factory(documents, qa, settings, args.max_model_calls)
    qa.manager._creation.response_wait = 0.01
    application = tornado.web.Application(
        [
            (r'/api/v1/qa/create', QASessionCreateHandler, {'service': qa.manager}),
            (r'/api/v1/qa/status', QASessionGetHandler, {'service': qa.manager}),
        ]
    )
    server = tornado.httpserver.HTTPServer(application)
    sockets = tornado.netutil.bind_sockets(0, address='127.0.0.1')
    server.add_sockets(sockets)
    base_url = f'http://127.0.0.1:{sockets[0].getsockname()[1]}'
    identity = {'userId': 'isolated-acceptance-user', 'sessionId': 'isolated-acceptance-session'}
    summary = {
        'mode': 'live' if args.live else 'smoke',
        'storage': 'memory',
        'productionWrites': False,
        'callbacksSent': 0,
        'knowledgeEnabled': False,
        'httpStatuses': [],
    }
    current = {}
    tick = time.perf_counter()
    try:
        async with asyncio.timeout(args.timeout_seconds), httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                base_url + '/api/v1/qa/create',
                json={**identity, 'fileList': [{'url': args.file_url or 'https://example.test/acceptance.pdf'}]},
            )
            summary['httpStatuses'].append(response.status_code)
            current = response.json().get('data') or {}
            while current.get('status') == 'processing':
                await asyncio.sleep(0.25)
                response = await client.post(base_url + '/api/v1/qa/status', json=identity)
                summary['httpStatuses'].append(response.status_code)
                current = response.json().get('data') or {}
        summary['qaCreated'] = current.get('status') == 'question'
        summary['documentProcessing'] = current.get('documentProcessing', [])
        summary['documentUsable'] = any(
            item.get('status') in {'complete', 'partial'} for item in summary['documentProcessing']
        )
        summary['qaResult'] = {
            key: current[key]
            for key in ('status', 'assistantMessage', 'question', 'questionType', 'options', 'errorCode', 'message')
            if key in current
        }
    except Exception as error:
        summary['errorType'] = type(error).__name__
        summary['qaCreated'] = False
    finally:
        server.stop()
        pending = list(qa.manager._creation.tasks.values()) + list(documents.coordinator.tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await server.close_all_connections()
        for client in clients:
            await client.async_client.close()
        metrics = [value for (_, name), value in documents.traces.payloads.items() if name == 'analysis_metrics']
        summary.update(
            elapsedMs=round((time.perf_counter() - tick) * 1000),
            docmindSubmissions=parser.submissions if args.live else len(parser.submit_calls),
            docmindSubmitMs=round(parser.submit_ms) if args.live else None,
            docmindWaitMs=round(parser.wait_ms) if args.live else None,
            modelLogicalCalls=len(clients) if args.live else len(documents.graph.calls) + len(qa.gateway.requests),
            documentCount=len(documents.index_collection.documents),
            savedReportCount=len(documents.collection.documents),
            queuedProfileJobs=len(documents.jobs.jobs),
            analysisMetrics=metrics,
            documentStates=dict(Counter(row['status'] for row in documents.index_collection.documents.values())),
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({key: value for key, value in summary.items() if key not in {'qaResult', 'analysisMetrics'}}))
    return 0 if summary.get('qaCreated') and summary.get('documentUsable') else 1


def main(*, online=False, dependency_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--live', action='store_true')
    mode.add_argument('--smoke', action='store_true')
    parser.add_argument('--config', help='Read only six DocMind and LLM settings')
    parser.add_argument('--file-url', help='Public PDF/image download URL used by the production downloader')
    parser.add_argument('--output', required=True)
    parser.add_argument(
        '--max-model-calls', type=int, default=64, help='Logical call cap; provider retries stay configured'
    )
    parser.add_argument('--timeout-seconds', type=float, default=900)
    args = parser.parse_args()
    if args.live and (not args.config or not args.file_url):
        parser.error('--live requires --config and --file-url')
    if args.max_model_calls < 1 or args.timeout_seconds <= 0:
        parser.error('budgets must be positive')
    try:
        args.output = external_artifact_path(args.output)
    except ValueError as error:
        parser.error(str(error))
    if args.live != online:
        command = ('tests.api.scripts.qa_document_acceptance --live' if args.live
                   else 'tests.unit.tools.qa_document_acceptance --smoke')
        parser.error(f'Use python -m {command} with the same arguments')
    settings = settings_from_file(args.config) if args.live else {}
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {**inert_environment(), **settings}))
        stack.enter_context(patch('loongspears.utils.env.load_env', return_value=None))
        # Fail immediately if a future import accidentally tries to construct a real database client.
        stack.enter_context(patch('database.mongodb.Client.create_async', side_effect=AssertionError('no database')))
        stack.enter_context(patch('database.redis.Client.connect', side_effect=AssertionError('no Redis')))
        return asyncio.run(acceptance(args, settings, dependency_factory=dependency_factory))
