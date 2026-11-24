"""Run bounded, alternating real-API control-model comparisons with synthetic users.

Read JSON stdin: baseline and candidate each require provider/model/api_key.
Optional base_url/model_kwargs belong to that route. No credentials are persisted.
The short trajectories measure latency and application validation, not clinical quality.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from copy import deepcopy
from pathlib import Path
from statistics import median
from unittest.mock import patch

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.api.scripts.local_qa_latency import run
from tests.unit._support.harnesses.record_dialogue_acceptance import isolated_environment
from utils.artifact_paths import external_artifact_path


def summarize(runs):
    groups = {}
    for run_report in runs:
        for row in run_report['rows']:
            key = run_report['arm'] + '/' + row['scenario']
            group = groups.setdefault(
                key, {'attempts': 0, 'successes': 0, 'within_5s': 0, 'body_ms': [], 'rejections': 0}
            )
            group['attempts'] += 1
            group['rejections'] += len(row.get('validation_rejections', []))
            if row['success']:
                group['successes'] += 1
                group['body_ms'].append(row['first_body_ms'])
                group['within_5s'] += row['first_body_ms'] <= 5000
    for group in groups.values():
        values = group.pop('body_ms')
        group['body_ms_median_success_only'] = median(values) if values else None
        group['body_ms_max_success_only'] = max(values) if values else None
    return groups


async def compare(settings, output, rounds):
    output = external_artifact_path(output)
    # Validate both arms before creating any sessions or incurring model usage.
    for name in ('baseline', 'candidate'):
        config = settings[name]
        if not all(config.get(key) for key in ('provider', 'model', 'api_key')):
            raise ValueError('Both arms require explicit provider, model and api_key')
    output.mkdir(parents=True, exist_ok=False)
    reports = []
    summary = {
        'method': 'single-concurrency local HTTP, synthetic three-answer trajectories, alternating AB/BA order',
        'limitations': 'adaptive trajectories may diverge; no fixed-state replay, load test or clinical review',
        'status': 'running',
        'runs': [],
        'statistics': {},
    }
    try:
        for index in range(rounds):
            for arm in ('baseline', 'candidate') if index % 2 == 0 else ('candidate', 'baseline'):
                config = deepcopy(settings['baseline'])
                config['record_only'] = True
                config['knowledge_api_key'] = settings.get('knowledge_api_key', '')
                if arm == 'candidate':
                    config['profile_overrides'] = {'record.plan': deepcopy(settings['candidate'])}
                path = output / f'{index + 1}-{arm}.json'
                print(json.dumps({'round': index + 1, 'arm': arm}), flush=True)
                await run(config, str(path))
                report = json.loads(path.read_text('utf-8'))
                report.update(arm=arm, round=index + 1)
                reports.append(report)
                path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                summary['runs'].append({'arm': arm, 'round': index + 1, 'file': path.name})
                summary['statistics'] = summarize(reports)
                (output / 'summary.json').write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8'
                )
        summary['status'] = 'completed'
    except BaseException as error:
        summary.update(status='interrupted', error_type=type(error).__name__)
        raise
    finally:
        (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='New output directory; existing directories are rejected')
    parser.add_argument('--rounds', type=int, choices=range(1, 6), default=2)
    args = parser.parse_args()
    settings = json.load(sys.stdin)
    logging.disable(logging.CRITICAL)
    with patch.dict(os.environ, isolated_environment()), patch('loongspears.utils.env.load_env', return_value=None):
        asyncio.run(compare(settings, args.output, args.rounds))
