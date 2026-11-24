"""Record-owned completion replay and discovery, without live Redis or MongoDB."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pymongo.errors import DuplicateKeyError

from adapters.persistence.record_creation import RedisRecordReplyRecoveryStore
from adapters.record.completion import MongoRecordCompletionStore


def completion():
    return {
        'userId': 'u',
        'sessionId': 's',
        'type': 'qa',
        'status': 'completed',
        'createdAt': '2026-11-10T10:00:00',
        'completedAt': '2026-11-10T10:01:00',
        'recordCompletionId': 'immutable-commit',
        'qaFlow': {'phase': 'finished'},
        'qaHistory': [{'answer': 'original answer'}],
        'answers': {'q': 'original answer'},
        'qaProfileIngestion': {'status': 'pending'},
    }


@pytest.mark.asyncio
async def test_completion_replay_only_inserts_once_and_preserves_later_chat_content():
    row = None

    async def read(query):
        assert query == {'userId': 'u', 'sessionId': 's'}
        return deepcopy(row)

    async def insert(value):
        nonlocal row
        row = deepcopy(value)
        return SimpleNamespace(acknowledged=True)

    collection = SimpleNamespace(
        create_index=AsyncMock(), find_one=AsyncMock(side_effect=read), insert_one=AsyncMock(side_effect=insert)
    )
    store = MongoRecordCompletionStore(collection)
    fixed = completion()
    assert await store.publish_once('u', 's', fixed)
    row.update(type='chat', qaCompletedAt=row['completedAt'], messages=[{'content': 'later Chat message'}])
    later = deepcopy(row)
    assert await store.publish_once('u', 's', fixed)
    assert row == later
    assert collection.insert_one.await_count == 1
    assert collection.create_index.await_count == 1
    index = collection.create_index.call_args.kwargs
    assert index['name'] == 'session_business_identity' and index['unique']
    assert index['partialFilterExpression'] == {'userId': {'$type': 'string'}, 'sessionId': {'$type': 'string'}}


@pytest.mark.asyncio
@pytest.mark.parametrize('matches', [False, True])
async def test_insert_race_requires_same_immutable_completion(matches):
    fixed = completion()
    winner = {**fixed, 'recordCompletionId': fixed['recordCompletionId'] if matches else 'different-owner'}
    collection = SimpleNamespace(
        create_index=AsyncMock(),
        find_one=AsyncMock(side_effect=[None, winner]),
        insert_one=AsyncMock(side_effect=DuplicateKeyError('synthetic race')),
    )
    store = MongoRecordCompletionStore(collection)
    if matches:
        assert await store.publish_once('u', 's', fixed)
    else:
        with pytest.raises(ValueError, match='conflicts'):
            await store.publish_once('u', 's', fixed)
    assert collection.insert_one.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', [None, 'qaHistory', 'completedAt'])
async def test_legacy_completion_requires_exact_preserved_evidence_without_replacing_it(changed):
    fixed = completion()
    legacy = deepcopy(fixed)
    legacy.pop('recordCompletionId')
    legacy.update(type='chat', qaCompletedAt=legacy.pop('completedAt'), messages=['preserve'])
    if changed == 'qaHistory':
        legacy['qaHistory'] = [{'answer': 'different answer'}]
    elif changed == 'completedAt':
        legacy['qaCompletedAt'] = 'another completion'
    collection = SimpleNamespace(
        create_index=AsyncMock(), find_one=AsyncMock(return_value=legacy), insert_one=AsyncMock()
    )
    store = MongoRecordCompletionStore(collection)
    if changed:
        with pytest.raises(ValueError, match='conflicts'):
            await store.publish_once('u', 's', fixed)
    else:
        assert await store.publish_once('u', 's', fixed)
    collection.insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_legacy_business_identity_is_not_deleted_or_silently_migrated():
    collection = SimpleNamespace(
        create_index=AsyncMock(side_effect=DuplicateKeyError('existing duplicates')),
        find_one=AsyncMock(),
        insert_one=AsyncMock(),
    )
    store = MongoRecordCompletionStore(collection)
    collection.create_index.assert_not_awaited()  # No startup work.
    with pytest.raises(DuplicateKeyError):
        await store.publish_once('u', 's', completion())
    collection.find_one.assert_not_awaited()
    collection.insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_owner_is_rejected_before_any_database_work():
    collection = SimpleNamespace(create_index=AsyncMock(), find_one=AsyncMock(), insert_one=AsyncMock())
    store = MongoRecordCompletionStore(collection)
    with pytest.raises(ValueError, match='invalid'):
        await store.publish_once('other', 's', completion())
    collection.create_index.assert_not_awaited()


class ScanRedis:
    def __init__(self, rows):
        self.rows = rows
        self.scans = 0

    async def scan_iter(self, **kwargs):
        assert kwargs == {'match': 'shulan:session:*', 'count': 100}
        self.scans += 1
        for key in self.rows:
            yield key

    async def get(self, key):
        return self.rows[key]


@pytest.mark.asyncio
async def test_scan_cursor_progresses_past_irrelevant_and_malformed_sessions():
    rows = {f'irrelevant-{index}': json.dumps({'type': 'chat'}) for index in range(5)}
    rows.update(
        {
            'bad-json': '{',
            'bad-pending': json.dumps(
                {'type': 'qa', 'status': 'active', 'qaFlow': {'version': 2, 'pendingSubmission': []}}
            ),
            'expired': None,
            'pending': json.dumps({'type': 'qa', 'userId': 'u', 'sessionId': 's', 'qaCommit': {'nextAttemptAt': 99}}),
            'failed': json.dumps(
                {'type': 'qa', 'userId': 'u', 'sessionId': 'f', 'qaCommit': {'nextAttemptAt': 0, 'status': 'failed'}}
            ),
        }
    )
    client = ScanRedis(rows)
    store = RedisRecordReplyRecoveryStore(client)
    assert await store.pending_replies(now=100, limit=1) == []
    assert await store.pending_replies(now=100, limit=1) == [('u', 's')]
    assert await store.pending_replies(now=100, limit=1) == []
    assert client.scans == 1
    await store.pending_replies(now=100, limit=1)
    assert client.scans == 2
