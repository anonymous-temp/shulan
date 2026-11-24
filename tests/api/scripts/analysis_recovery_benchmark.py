"""Opt-in real-model acceptance on a local DocMind archive; never opens service databases.

Output contains source health data: use an access-controlled directory outside the repository.
Expected cases are independently transcribed anchors, not this extractor's own output.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.api.scripts.analysis_first_pass_benchmark import _settings
from tests.unit._support.analysis_benchmark import MemoryTrace
from tests.unit._support.analysis_benchmark import NoDocMind


def build_runtime(settings, *, max_calls=200):
    import aion
    from langgraph.checkpoint.memory import InMemorySaver

    from agents import AgentHub
    from agents import AgentRuntime
    from agents import GraphRegistry
    from agents.graphs.analysis.graph import build_analysis_health_report_graph
    from agents.graphs.analysis.profile import create_analysis_health_report_profile
    from agents.llm import ModelProfileRegistry
    from agents.llm.aion_gateway import AionLLMGateway

    clients = []

    def factory(*args, **kwargs):
        if len(clients) >= max_calls:
            raise RuntimeError('acceptance model-call budget exhausted')
        client = aion.create_chat_client(*args, **kwargs)
        clients.append(client)
        return client

    prompt = Path('prompts/analysis/health_report.txt').read_text(encoding='utf-8')
    gateway = AionLLMGateway(
        provider=settings['SHULAN_LLM_PROVIDER'],
        api_key=settings['SHULAN_LLM_API_KEY'],
        default_model=settings['SHULAN_LLM_DEFAULT_MODEL'],
        client_factory=factory,
        prompt_renderer=lambda *args, **kwargs: prompt,
    )
    profile = create_analysis_health_report_profile()
    registry, profiles = GraphRegistry(), ModelProfileRegistry()
    profiles.register(profile)
    registry.register(
        'analysis.health_report',
        build_analysis_health_report_graph(gateway=gateway, profile=profile).compile(),
        version='recovery-acceptance',
    )
    runtime = AgentRuntime(
        hub=AgentHub(registry),
        registry=registry,
        profiles=profiles,
        llm_gateway=gateway,
        context_repository=object(),
        lease_manager=object(),
        checkpointer=InMemorySaver(),
        _close_callback=lambda: None,
    )
    return runtime, clients


def audit(result, expected):
    evidence = result.get('analysisEvidence', [])
    missing = []
    for case in expected.get('required', []):
        candidates = [item for item in evidence if item.get('sourceId') == case['sourceId']]
        if not any(
            all(item.get(key) == value for key, value in case.items() if key != 'sourceId') for item in candidates
        ):
            missing.append(case['sourceId'])
    forbidden = [
        item
        for item in result.get('observations', [])
        if any(all(item.get(key) == value for key, value in case.items()) for case in expected.get('forbidden', []))
    ]
    expected_date = expected.get('reportTime')
    dated = sum(bool(item.get('observedAt')) for item in result.get('observations', []))
    date_pass = expected_date is None or (
        result.get('reportTime') == expected_date
        and all(
            any(
                item.get('sourceId') == case['sourceId'] and item.get('observedAt') == expected_date
                for item in evidence
            )
            for case in expected.get('required', [])
        )
    )
    return {
        'missingAnchors': missing,
        'forbiddenResultCount': len(forbidden),
        'reportDatePass': date_pass,
        'datedObservationCount': dated,
        'anchorPass': not missing and not forbidden and date_pass,
    }


async def benchmark(args, settings):
    from adapters.analysis.tokenizer import count_report_input_tokens
    from modules.analysis.application.input_preparation import prepare_input
    from modules.analysis.application.service import HealthReportInterpreter

    raw = json.loads(Path(args.docmind).read_text(encoding='utf-8'))
    expected = json.loads(Path(args.expected).read_text(encoding='utf-8'))
    runtime, clients = build_runtime(settings, max_calls=args.max_model_calls)
    interpreter = HealthReportInterpreter(
        runtime, docmind_client=NoDocMind(), count_input_tokens=count_report_input_tokens
    )
    prepared = prepare_input(raw['text'], docmind=raw, count_tokens=count_report_input_tokens)
    report = {
        'model': settings['SHULAN_LLM_DEFAULT_MODEL'],
        'provider': settings['SHULAN_LLM_PROVIDER'],
        'productionWrites': False,
        'docmindReplayed': True,
        'endToEndUploadMeasured': False,
        'plannedCalls': len(prepared.chunks),
        'plannedInputChars': sum(len(prepared.model_text(c)) for c in prepared.chunks),
        'originalChars': len(raw['text']),
        'runs': [],
        'implementationSha256': {
            path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for path in (
                'modules/analysis/application/service.py',
                'modules/analysis/application/input_preparation.py',
                'modules/analysis/application/analysis_validation.py',
                'prompts/analysis/health_report.txt',
            )
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        for index in range(args.rounds):
            trace = MemoryTrace(raw, f'isolated-recovery-{index}')
            print(json.dumps({'round': index + 1, 'state': 'started'}), flush=True)
            tick = time.perf_counter()
            result = await interpreter.analyze(raw['text'], trace=trace, user_id='isolated-recovery-acceptance')
            metrics = trace.payloads.get('analysis_metrics', {})
            first_inputs = [
                payload
                for name, payload in trace.payloads.items()
                if name.startswith('analysis_input') and not payload.get('outputRecovery')
            ]
            row = {
                'round': index + 1,
                'elapsedMs': round((time.perf_counter() - tick) * 1000),
                'quality': result['processing']['status'],
                'observations': len(result['observations']),
                'issues': dict(Counter(issue['code'] for issue in result['processing']['issues'])),
                'metrics': metrics,
                'firstPassCalls': len(first_inputs),
                'firstPassInputChars': sum(len(payload['text']) for payload in first_inputs),
                'totalInputChars': metrics.get('inputChars'),
                'audit': audit(result, expected),
            }
            report['runs'].append(row)
            output.with_name(output.stem + f'-round{index + 1}.json').write_text(
                json.dumps({'result': result, 'archives': trace.payloads}, ensure_ascii=False),
                encoding='utf-8',
            )
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        for client in clients:
            await client.async_client.close()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return (
        0
        if all(
            row['audit']['anchorPass']
            and row['quality'] != 'unavailable'
            and row['firstPassCalls'] <= 40
            and row['totalInputChars'] is not None
            and row['totalInputChars'] <= len(raw['text']) * 1.5
            for row in report['runs']
        )
        else 1
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--config', required=True)
    parser.add_argument('--docmind', required=True)
    parser.add_argument('--expected', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--rounds', type=int, choices=range(1, 4), default=3)
    parser.add_argument('--max-model-calls', type=int, default=200)
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required; no external calls were made')
    repository = Path(__file__).resolve().parents[1]
    output = Path(args.output).resolve()
    if output.is_relative_to(repository):
        parser.error('--output must be outside the repository because archives contain private source data')
    if args.max_model_calls < 1:
        parser.error('--max-model-calls must be positive')
    settings = _settings(args.config)
    inert = {
        'SHULAN_ENVIRONMENT': 'test',
        'SHULAN_LOG_LEVEL': 'CRITICAL',
        'SHULAN_ALIYUN_DOCMIND_ACCESSKEY_ID': 'unused',
        'SHULAN_ALIYUN_DOCMIND_ACCESSKEY_SECRET': 'unused',
        'SHULAN_ALIYUN_DOCMIND_ENDPOINT': 'https://unused.invalid',
        'SHULAN_MONGODB_URI': 'mongodb://127.0.0.1:1',
        'SHULAN_MONGODB_DATABASE': 'unused',
        'SHULAN_MONGODB_COLL_SESSIONS': 'unused',
        'SHULAN_MONGODB_COLL_HEALTH_REPORTS': 'unused',
        'SHULAN_REDIS_HOST': '127.0.0.1',
        'SHULAN_REDIS_PORT': '1',
        'SHULAN_REDIS_PASSWORD': '',
        'SHULAN_REPORT_CALLBACK_URL': 'https://unused.invalid/callback',
        'SHULAN_REPORT_PRODUCT_URL': 'https://unused.invalid/products',
    }
    with patch.dict(os.environ, {**inert, **settings}), patch('loongspears.utils.env.load_env', return_value=None):
        return asyncio.run(benchmark(args, settings))


if __name__ == '__main__':
    raise SystemExit(main())
