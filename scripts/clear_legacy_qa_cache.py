"""Inspect or clear only legacy unfinished QA snapshots after request draining.

Importing this module performs no configuration reads or network operations.
Run as ``python -m scripts.clear_legacy_qa_cache`` for a count-only dry run.
"""

import argparse
import asyncio
import json

_DELETE_UNCHANGED = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def is_legacy_qa(raw) -> bool:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if not isinstance(value, dict) or value.get('type') != 'qa':
        return False
    if value.get('status') not in ('active', 'awaiting_decision'):
        return False
    metadata = value.get('qaFlow')
    return metadata is None or (isinstance(metadata, dict) and metadata.get('version') in (None, 1))


async def clear_legacy_cache(client, *, apply=False) -> dict:
    counts = {'scanned': 0, 'legacy': 0, 'deleted': 0, 'changed': 0}
    async for key in client.scan_iter(match='shulan:session:*', count=100):
        counts['scanned'] += 1
        raw = await client.get(key)
        if not is_legacy_qa(raw):
            continue
        counts['legacy'] += 1
        if apply:
            deleted = await client.eval(_DELETE_UNCHANGED, 1, key, raw)
            counts['deleted' if deleted else 'changed'] += 1
    return counts


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='删除旧版未完成 QA；默认仅统计')
    parser.add_argument('--requests-drained', action='store_true', help='确认已停止旧请求并等待所有在途请求结束')
    args = parser.parse_args(argv)
    if args.apply and not args.requests_drained:
        parser.error('--apply 必须同时提供 --requests-drained')
    return args


async def main(args):
    # Only an explicit CLI run may load deployment configuration.
    from db.redis import client

    print(json.dumps(await clear_legacy_cache(client, apply=args.apply)))


if __name__ == '__main__':
    asyncio.run(main(parse_args()))
