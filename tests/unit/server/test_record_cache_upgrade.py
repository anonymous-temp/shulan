"""The QA upgrade utility must preserve completed, current and non-QA data."""

import json
from unittest.mock import patch

import pytest

from scripts.clear_legacy_qa_cache import clear_legacy_cache
from scripts.clear_legacy_qa_cache import is_legacy_qa
from scripts.clear_legacy_qa_cache import parse_args


@pytest.mark.parametrize(
    'session',
    [
        {'type': 'qa', 'status': 'completed'},
        {'type': 'chat', 'status': 'active'},
        {'type': 'qa', 'status': 'active', 'qaFlow': {'version': 2}},
        {'type': 'qa', 'status': 'active', 'qaFlow': {'version': 3}},
        {'status': 'active'},
        [],
        None,
    ],
)
def test_preserves_other_data(session):
    assert not is_legacy_qa(json.dumps(session))


@pytest.mark.asyncio
async def test_dry_run_and_compare_before_delete():
    class Cache:
        values = {
            'legacy': json.dumps({'type': 'qa', 'status': 'awaiting_decision'}),
            'changed': json.dumps({'type': 'qa', 'status': 'active'}),
        }

        async def scan_iter(self, **kwargs):
            assert kwargs['match'] == 'shulan:session:*'
            for key in list(self.values):
                yield key

        async def get(self, key):
            return self.values[key]

        async def eval(self, script, count, key, raw):
            assert count == 1 and 'GET' in script and 'DEL' in script
            if key == 'changed':
                return 0
            assert self.values.pop(key) == raw
            return 1

    cache = Cache()
    assert (await clear_legacy_cache(cache))['deleted'] == 0
    assert len(cache.values) == 2
    assert await clear_legacy_cache(cache, apply=True) == {'scanned': 2, 'legacy': 2, 'deleted': 1, 'changed': 1}


def test_apply_requires_draining_acknowledgement():
    assert not parse_args([]).apply
    with patch('sys.stderr'), pytest.raises(SystemExit):
        parse_args(['--apply'])
    assert parse_args(['--apply', '--requests-drained']).apply
