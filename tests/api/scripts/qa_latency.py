"""Measure a running test server with synthetic users and real model calls.

Run manually: this creates test sessions and incurs model/retrieval usage.
No credentials or deployment configuration are read. No reports are generated.
The deployment revision must be confirmed separately; health has no build ID.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import httpx

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from utils.artifact_paths import external_artifact_path


def milliseconds(started):
    return round((time.perf_counter() - started) * 1000, 2)


async def stream_turn(client, path, payload, row):
    started = time.perf_counter()
    row['started_monotonic'] = started
    text = []
    result = None
    row.update(first_event_ms=None, first_reasoning_ms=None, first_body_ms=None, finish_ms=None)
    try:
        async with asyncio.timeout(120):
            async with client.stream('POST', path, json=payload) as response:
                row['http_status'] = response.status_code
                row['headers_ms'] = milliseconds(started)
                row['content_type'] = response.headers.get('content-type')
                row['buffering'] = response.headers.get('x-accel-buffering')
                response.raise_for_status()
                data_lines = []
                async for line in response.aiter_lines():
                    if line.startswith('data:'):
                        data_lines.append(line[5:].lstrip())
                        continue
                    if line or not data_lines:
                        continue
                    stamp = milliseconds(started)
                    envelope = json.loads('\n'.join(data_lines))
                    data_lines.clear()
                    data = envelope.get('data') or envelope
                    kind = data.get('type')
                    if row['first_event_ms'] is None:
                        row['first_event_ms'] = stamp
                    if kind == 'reasoning' and data.get('content') and row['first_reasoning_ms'] is None:
                        row['first_reasoning_ms'] = stamp
                    if kind == 'content' and data.get('content'):
                        if data['content'].strip() and row['first_body_ms'] is None:
                            row['first_body_ms'] = stamp
                        text.append(data['content'])
                    if kind == 'question' or (data.get('status') == 'finished' and data.get('assistantMessage')):
                        if row['first_body_ms'] is None:
                            row['first_body_ms'] = stamp
                        result = data
                    if kind == 'finish':
                        row['finish_ms'] = stamp
                    if kind == 'error':
                        row['error'] = 'server_error_event'
                        row['error_code'] = envelope.get('code')
    except Exception as error:
        row['error'] = type(error).__name__
    row['total_ms'] = milliseconds(started)
    row['body_chars'] = sum(map(len, text))
    row['success'] = row['first_body_ms'] is not None and row['finish_ms'] is not None and 'error' not in row
    if text:
        row['synthetic_reply'] = ''.join(text)
    if result:
        row['synthetic_question'] = result
        row['body_chars'] = len(result.get('assistantMessage', '')) + len(result.get('question', ''))
    return result


def intake_answer(question):
    text = question.get('question', '')
    if any(word in text for word in ('用药', '服用', '药物', '益生菌')):
        return '近期没有服用药物、益生菌或膳食补充剂。'
    if any(word in text for word in ('多久', '何时', '时间', '持续')):
        return '大约两个月，通常晚饭后出现，休息一小时左右会缓解。'
    if any(word in text for word in ('频率', '次数', '几次', '每天')):
        return '每周三四次，主要是晚饭后，不是每顿饭后都有。'
    if any(word in text for word in ('病史', '手术', '疾病')):
        return '没有确诊的消化系统疾病，也没有做过腹部手术。'
    if any(word in text for word in ('饮食', '吃饭', '进食', '习惯')):
        return '最近晚饭吃得较快，蔬菜吃得少，饭后很少活动。'
    return '晚饭后肚子发胀，有两个月了，没有明显腹痛、便血、呕吐或体重下降。'


async def measure(args):
    output = external_artifact_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        'started_at': datetime.now(timezone.utc).isoformat(),
        'base_url': args.base_url,
        'deployment_revision': args.revision,
        'measurement': 'client HTTP/SSE wall clock; start/reasoning excluded from body TTFT',
        'rows': [],
    }

    def persist(row):
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(
            json.dumps(
                {key: value for key, value in row.items() if not key.startswith('synthetic_')}, ensure_ascii=False
            ),
            flush=True,
        )

    async with httpx.AsyncClient(base_url=args.base_url, timeout=httpx.Timeout(120, connect=10)) as client:
        health = (await client.get('/health')).json()
        report['environment'] = health.get('data', {}).get('environment')
        if report['environment'] not in ('dev', 'test'):
            raise ValueError('This probe requires an explicitly identified dev/test server')
        prompts = [
            '你好，请用一句话介绍你能帮助我做什么。',
            '我想先整理饮食习惯，请告诉我从哪类信息开始记录。',
            '用一句话概括我可以怎样记录每天的用餐时间。',
        ]
        chat_groups = (
            []
            if getattr(args, 'record_only', False)
            else [('chat-default', None), ('chat-fast', False), ('chat-evidence', False)]
        )
        for label, thinking in chat_groups:
            identity = {'userId': 'test-latency-' + uuid4().hex, 'sessionId': 'test-latency-' + uuid4().hex}
            messages = (
                prompts
                if label != 'chat-evidence'
                else ['成年人日常盐摄入量的一般指南建议是什么？请简要回答并给出资料来源。']
            )
            for index, message in enumerate(messages, 1):
                payload = {**identity, 'message': message}
                if thinking is not None:
                    payload['thinking'] = thinking
                row = {'scenario': label, 'turn': index, 'thinking': thinking, **identity}
                report['rows'].append(row)
                await stream_turn(client, '/api/v1/chat/send/stream', payload, row)
                persist(row)
                if not row['success']:
                    break

        identity = {'userId': 'test-latency-' + uuid4().hex, 'sessionId': 'test-latency-' + uuid4().hex}
        started = time.perf_counter()
        row = {'scenario': 'record-create', 'started_monotonic': started, **identity}
        report['rows'].append(row)
        current = None
        try:
            async with asyncio.timeout(180):
                response = await client.post('/api/v1/qa/create', json={**identity, 'basicInfo': {'age': 38}})
                row['http_status'] = response.status_code
                row['accepted_ms'] = milliseconds(started)
                response.raise_for_status()
                current = response.json().get('data') or {}
                while current.get('status') == 'processing':
                    await asyncio.sleep(0.5)
                    response = await client.post('/api/v1/qa/status', json=identity)
                    response.raise_for_status()
                    current = response.json().get('data') or {}
                row['first_body_ms'] = milliseconds(started) if current.get('questionId') else None
                row['success'] = bool(current.get('questionId'))
                row['synthetic_question'] = current
        except Exception as error:
            row['error'] = type(error).__name__
            row['success'] = False
        row['total_ms'] = milliseconds(started)
        persist(row)
        if row['success']:
            for index in range(1, 4):
                row = {'scenario': 'record-fast', 'turn': index, 'thinking': False, **identity}
                report['rows'].append(row)
                payload = {
                    **identity,
                    'questionId': current['questionId'],
                    'other': intake_answer(current),
                    'thinking': False,
                }
                current = await stream_turn(client, '/api/v1/qa/answer/stream', payload, row)
                persist(row)
                if not row['success'] or not current or not current.get('questionId'):
                    break
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--revision', default='unconfirmed')
    asyncio.run(measure(parser.parse_args()))
