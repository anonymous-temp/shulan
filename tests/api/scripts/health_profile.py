"""对运行中的 Shulan 服务执行用户健康画像真实接口联调。

运行方式：``python -m tests.api.scripts.health_profile``。默认生成 ``test-``
用户并初始化本人画像；事实修改必须提供已有事实、版本参数和 ``--yes``。
脚本不读取 ``.env``、不创建测试专用 API，也不直接读写 MongoDB。
"""

from __future__ import annotations

import argparse
import json
import uuid
from typing import Any

import requests

from config.env import SERVER_PORT


def _new_id(prefix: str) -> str:
    return f'test-{prefix}-{uuid.uuid4().hex}'


def _post(
    base_url: str,
    path: str,
    body: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    print(f'\nPOST {path}')
    print(json.dumps(body, ensure_ascii=False, indent=2))
    response = requests.post(
        f'{base_url.rstrip("/")}{path}',
        json=body,
        timeout=timeout,
    )
    try:
        payload = response.json()
    except requests.JSONDecodeError:
        payload = {'raw': response.text}
    print(f'HTTP {response.status_code}')
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    response.raise_for_status()
    if not isinstance(payload, dict) or payload.get('code', 500) >= 400:
        raise RuntimeError(f'接口返回失败响应: {payload!r}')
    data = payload.get('data')
    if not isinstance(data, dict):
        raise RuntimeError('接口响应缺少 data 对象')
    return data


def _mutation_identity(args, subject_id: str) -> dict[str, Any]:
    if not args.fact_id:
        raise ValueError('--decision 需要同时提供 --fact-id')
    if args.fact_version is None or args.profile_version is None:
        raise ValueError(
            '--decision 需要同时提供 --fact-version 和 --profile-version',
        )
    return {
        'userId': args.user_id,
        'subjectId': subject_id,
        'factId': args.fact_id,
        'expectedFactVersion': args.fact_version,
        'expectedProfileVersion': args.profile_version,
    }


def _run_mutation(args, subject_id: str) -> dict[str, Any] | None:
    if args.decision is None:
        return None
    if not args.yes:
        raise ValueError('事实修改会写入真实数据，必须显式提供 --yes')
    base = _mutation_identity(args, subject_id)
    if args.decision == 'delete':
        body = {
            **base,
            'deleteScope': 'fact',
            'reason': args.reason,
            'requestId': _new_id('fact-delete'),
        }
        return _post(
            args.base_url,
            '/api/v1/health-profile/facts/delete',
            body,
            timeout=args.timeout,
        )

    body = {
        **base,
        'decision': args.decision,
        'reason': args.reason,
        'requestId': _new_id('fact-resolve'),
    }
    if args.decision == 'replace':
        try:
            value = json.loads(args.replacement_value)
        except json.JSONDecodeError as error:
            raise ValueError('--replacement-value 必须是合法 JSON') from error
        body['replacement'] = {
            'valueType': args.replacement_value_type,
            'value': value,
            'effectivePeriod': {
                'start': args.effective_start,
                'end': args.effective_end,
            },
        }
    return _post(
        args.base_url,
        '/api/v1/health-profile/facts/resolve',
        body,
        timeout=args.timeout,
    )


def _verify_mutation(
    args,
    result: dict[str, Any],
    facts: dict[str, Any],
) -> None:
    items = facts.get('items')
    if not isinstance(items, list):
        raise RuntimeError('事实列表响应缺少 items 数组')
    if args.decision == 'delete':
        if result.get('status') != 'deleted':
            raise RuntimeError('删除响应未返回 status=deleted')
        if any(item.get('factId') == args.fact_id for item in items):
            raise RuntimeError('删除事实仍出现在普通事实列表中')
        return
    fact = result.get('fact')
    expected = {
        'confirm': 'active',
        'reject': 'retracted',
        'replace': 'active',
    }[args.decision]
    if not isinstance(fact, dict) or fact.get('status') != expected:
        raise RuntimeError(f'{args.decision} 未返回预期状态 {expected}')
    if args.decision == 'replace' and fact.get('factId') == args.fact_id:
        raise RuntimeError('replace 未创建新的 factId')


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Shulan 用户健康画像真实 API 联调',
    )
    parser.add_argument(
        '--base-url',
        default=f'http://127.0.0.1:{SERVER_PORT}',
    )
    parser.add_argument('--user-id', default=_new_id('profile-user'))
    parser.add_argument('--subject-id')
    parser.add_argument('--limit', default=20, type=int)
    parser.add_argument('--timeout', default=30.0, type=float)
    parser.add_argument('--include-evidence', action='store_true')
    parser.add_argument(
        '--decision',
        choices=('confirm', 'reject', 'replace', 'delete'),
    )
    parser.add_argument('--fact-id')
    parser.add_argument('--fact-version', type=int)
    parser.add_argument('--profile-version', type=int)
    parser.add_argument('--reason', default='真实 API 联调')
    parser.add_argument('--replacement-value-type', default='quantity')
    parser.add_argument(
        '--replacement-value',
        default='{"number": 62.5, "unit": "kg"}',
    )
    parser.add_argument('--effective-start', default='2026-08-17T00:00:00Z')
    parser.add_argument('--effective-end')
    parser.add_argument('--yes', action='store_true')
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if not args.user_id.startswith('test-'):
        parser.error('--user-id 必须以 test- 开头')
    if not 1 <= args.limit <= 100:
        parser.error('--limit 必须为 1 到 100')

    profile_body = {
        'userId': args.user_id,
        'includeEvidence': args.include_evidence,
        'indicatorLimit': 12,
    }
    if args.subject_id:
        profile_body['subjectId'] = args.subject_id
    try:
        profile = _post(
            args.base_url,
            '/api/v1/health-profile/get',
            profile_body,
            timeout=args.timeout,
        )
        subject_id = profile['subject']['subjectId']
        _post(
            args.base_url,
            '/api/v1/health-profile/subjects/list',
            {'userId': args.user_id, 'includeArchived': False},
            timeout=args.timeout,
        )
        fact_body = {
            'userId': args.user_id,
            'subjectId': subject_id,
            'statuses': [
                'active',
                'pending',
                'disputed',
                'superseded',
                'retracted',
                'expired',
            ],
            'cursor': None,
            'limit': args.limit,
        }
        _post(
            args.base_url,
            '/api/v1/health-profile/facts/list',
            fact_body,
            timeout=args.timeout,
        )
        mutation = _run_mutation(args, subject_id)
        if mutation is not None:
            facts_after = _post(
                args.base_url,
                '/api/v1/health-profile/facts/list',
                fact_body,
                timeout=args.timeout,
            )
            _verify_mutation(args, mutation, facts_after)
        changes = _post(
            args.base_url,
            '/api/v1/health-profile/changes/list',
            {
                'userId': args.user_id,
                'subjectId': subject_id,
                'cursor': None,
                'limit': args.limit,
            },
            timeout=args.timeout,
        )
        forbidden = {'value', 'reasoning', 'operationResult', 'sourceRef'}
        for event in changes.get('items', []):
            leaked = forbidden.intersection(event)
            if leaked:
                raise RuntimeError(
                    f'公开变更记录包含禁止字段: {sorted(leaked)}',
                )
    except (
        KeyError,
        TypeError,
        ValueError,
        RuntimeError,
        requests.RequestException,
    ) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
