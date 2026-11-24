"""Opt-in, bounded LLM comparison using anonymous fixtures and in-memory persistence.

Run with --help. This command never opens MongoDB/Redis or submits a DocMind task.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from unittest.mock import patch

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.unit._support.analysis_benchmark import MemoryTrace
from tests.unit._support.analysis_benchmark import NoDocMind
from tests.unit._support.analysis_benchmark import _quality
from tests.unit._support.analysis_benchmark import scenarios
from utils.artifact_paths import external_artifact_path

ROOT = Path(__file__).resolve().parents[3]


class StopBenchmark(Exception):
    pass


def _settings(config):
    from dotenv import dotenv_values

    keys = {'SHULAN_LLM_PROVIDER', 'SHULAN_LLM_API_KEY', 'SHULAN_LLM_DEFAULT_MODEL'}
    selected = []
    if config is not None:
        for line in Path(config).read_text(encoding='utf-8-sig').splitlines():
            match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
            if match and match[1] in keys:
                selected.append(line)
    values = dotenv_values(stream=io.StringIO('\n'.join(selected)))
    result = {key: os.environ.get(key) or values.get(key) for key in keys}
    if not all(result.values()):
        raise ValueError('provider, API key and explicit model are required')
    return result


async def benchmark(args, settings):
    output = external_artifact_path(args.output)
    import aion
    from langgraph.checkpoint.memory import InMemorySaver

    from adapters.analysis.tokenizer import count_report_input_tokens
    from agents import AgentHub
    from agents import AgentRuntime
    from agents import GraphRegistry
    from agents.graphs.analysis.graph import build_analysis_health_report_graph
    from agents.graphs.analysis.profile import create_analysis_health_report_profile
    from agents.llm import ModelProfileRegistry
    from agents.llm.aion_gateway import AionLLMGateway
    from modules.analysis.application.input_preparation import prepare_input
    from modules.analysis.application.service import HealthReportInterpreter

    baseline_source = Path(args.baseline_service).read_text(encoding='utf-8')
    spec = importlib.util.spec_from_file_location('_analysis_benchmark_baseline', args.baseline_service)
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = baseline
    spec.loader.exec_module(baseline)
    baseline.HealthReportInterpreter.REUSABLE_RESULT_VERSIONS = (
        'analysis-health-report-v3',
        'analysis-health-report-v4',
    )
    baseline.HealthReportInterpreter.MAX_SPLIT_DEPTH = 2
    prompts = {
        'baseline': Path(args.baseline_prompt).read_text(encoding='utf-8'),
        'current': (ROOT / 'prompts/analysis/health_report.txt').read_text(encoding='utf-8'),
    }
    calls = 0
    clients = []

    def factory(*values, **kwargs):
        nonlocal calls
        calls += 1
        if calls > args.max_model_calls:
            raise RuntimeError('benchmark model-call budget exhausted')
        client = aion.create_chat_client(*values, **kwargs)
        # Measure actual first attempts and keep the authorized request cap exact.
        # Both compared versions use this same benchmark-only transport policy.
        client.async_client = client.async_client.with_options(max_retries=0)
        clients.append(client)
        return client

    rows = []
    report = {
        'provider': settings['SHULAN_LLM_PROVIDER'],
        'model': settings['SHULAN_LLM_DEFAULT_MODEL'],
        'baselineSourceSha256': hashlib.sha256(baseline_source.encode()).hexdigest(),
        'currentSourceSha256': hashlib.sha256(
            (ROOT / 'modules/analysis/application/service.py').read_bytes()
        ).hexdigest(),
        'rounds': args.rounds,
        'cases': rows,
        'docmindReplayed': True,
        'productionWrites': False,
        'benchmarkTransportRetries': 0,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        async with asyncio.timeout(args.total_timeout):
            for round_number in range(args.rounds):
                for name, raw in scenarios().items():
                    prepared = prepare_input(raw['text'], docmind=raw, count_tokens=count_report_input_tokens)
                    expected = [record for chunk in prepared.chunks for record in chunk.records]
                    modes = ('current', 'baseline') if round_number % 2 == 0 else ('baseline', 'current')
                    for mode in modes:
                        print(
                            json.dumps({'round': round_number + 1, 'case': name, 'mode': mode, 'state': 'started'}),
                            flush=True,
                        )
                        gateway = AionLLMGateway(
                            provider=settings['SHULAN_LLM_PROVIDER'],
                            api_key=settings['SHULAN_LLM_API_KEY'],
                            default_model=settings['SHULAN_LLM_DEFAULT_MODEL'],
                            client_factory=factory,
                            prompt_renderer=lambda *a, text=prompts[mode], **k: text,
                        )
                        profile = create_analysis_health_report_profile()
                        registry, profiles = GraphRegistry(), ModelProfileRegistry()
                        profiles.register(profile)
                        registry.register(
                            'analysis.health_report',
                            build_analysis_health_report_graph(gateway=gateway, profile=profile).compile(),
                            version='benchmark',
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
                        interpreter_type = (
                            HealthReportInterpreter if mode == 'current' else baseline.HealthReportInterpreter
                        )
                        interpreter_kwargs = {'docmind_client': NoDocMind()}
                        if mode == 'current':
                            interpreter_kwargs['count_input_tokens'] = count_report_input_tokens
                        interpreter = interpreter_type(runtime, **interpreter_kwargs)
                        trace = MemoryTrace(raw, f'benchmark-{name}-{round_number}-{mode}')
                        tick = time.perf_counter()
                        result, error_type = None, None
                        try:
                            result = await interpreter.analyze(raw['text'], user_id='anonymous-benchmark', trace=trace)
                        except Exception as error:
                            error_type = type(error).__name__
                        finally:
                            for client in clients:
                                await client.async_client.close()
                                client.client.close()
                            clients.clear()
                        elapsed = round((time.perf_counter() - tick) * 1000)
                        outputs = [value for key, value in trace.payloads.items() if key.startswith('analysis_output')]
                        metrics = trace.payloads.get('analysis_metrics') or {
                            'modelCalls': len(outputs),
                            'outputRecoveryCalls': sum(value.get('splitDepth', 0) > 0 for value in outputs),
                            'completionTokens': sum(
                                (value.get('usage') or {}).get('completionTokens', 0) for value in outputs
                            ),
                            'firstPassSuccess': result is not None
                            and not any(value.get('splitDepth', 0) for value in outputs),
                        }
                        row = {
                            'round': round_number + 1,
                            'case': name,
                            'mode': mode,
                            'modelStageElapsedMs': elapsed,
                            'completed': result is not None,
                            'qualityPass': _quality(result, expected, imaging=name == 'mixed_dates_imaging'),
                            'errorType': error_type,
                            'metrics': metrics,
                            'result': result,
                            'archives': trace.payloads,
                        }
                        rows.append(row)
                        print(
                            json.dumps(
                                {key: value for key, value in row.items() if key not in {'result', 'archives'}},
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                        if (
                            args.fail_fast
                            and mode == 'current'
                            and (not row['qualityPass'] or not metrics['firstPassSuccess'])
                        ):
                            raise StopBenchmark
    except StopBenchmark:
        report['stoppedReason'] = 'current_acceptance_failed'
    except TimeoutError:
        report['stoppedReason'] = 'total_timeout'
    finally:
        report['elapsedMs'] = round((time.perf_counter() - started) * 1000)
        report['modelCalls'] = min(calls, args.max_model_calls)
        samples = {mode: [row for row in rows if row['mode'] == mode] for mode in prompts}
        p95 = {
            mode: sorted(row['modelStageElapsedMs'] for row in values)[math.ceil(len(values) * 0.95) - 1]
            if values
            else None
            for mode, values in samples.items()
        }
        report['modelStageP95Ms'] = p95
        complete = all(
            len(values) == args.rounds * 3 and all(row['completed'] for row in values) for values in samples.values()
        )
        report['timingComparable'] = complete
        improvement = 1 - p95['current'] / p95['baseline'] if complete and p95['baseline'] else None
        report['p95Improvement'] = improvement
        report['acceptance'] = {
            'allCurrentFirstPass': len(samples['current']) == args.rounds * 3
            and all(
                row['qualityPass'] and row['metrics']['firstPassSuccess'] and row['metrics']['outputRecoveryCalls'] == 0
                for row in samples['current']
            ),
            'p95ImprovementAtLeast30Percent': improvement is not None and improvement >= 0.30,
            'endToEndUploadMeasured': False,
        }
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(
            json.dumps({key: value for key, value in report.items() if key != 'cases'}, ensure_ascii=False), flush=True
        )
    return (
        0 if all(report['acceptance'][key] for key in ('allCurrentFirstPass', 'p95ImprovementAtLeast30Percent')) else 1
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly enable billable LLM requests')
    parser.add_argument(
        '--fail-fast', action='store_true', help='Stop on a failed current first pass to conserve budget'
    )
    parser.add_argument('--config', help='Read only the three SHULAN_LLM_* settings from this file')
    parser.add_argument('--baseline-service', required=True)
    parser.add_argument('--baseline-prompt', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--rounds', type=int, default=3, choices=range(1, 4))
    parser.add_argument('--max-model-calls', type=int, default=36)
    parser.add_argument('--total-timeout', type=int, default=900)
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required; no external calls were made')
    try:
        args.output = external_artifact_path(args.output)
    except ValueError as error:
        parser.error(str(error))
    settings = _settings(args.config)
    # Application imports require service settings; use inert values and suppress
    # its dotenv loader so only the explicitly selected LLM credentials are read.
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
