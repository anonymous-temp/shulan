"""Migrated legacy intents leave fixed recovery batches without invented jobs."""

from copy import deepcopy
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

import pytest

from health_profile.chat_source import ChatProfileSourceService
from health_profile.chat_source import MongoChatProfileStore
from health_profile.ingestion import HealthProfileJobQueue
from health_profile.record_source import MongoRecordProfileStore
from health_profile.record_source import RecordProfileSourceService

from .test_profile_read_and_subjects import FakeCursor
from .test_profile_read_and_subjects import _nested_value
from .test_profile_read_and_subjects import _set_nested
from .test_record_profile_source import InMemoryJobRepository


def matches(row, query):
    for key, wanted in query.items():
        if key == '$or':
            if not any(matches(row, branch) for branch in wanted):
                return False
            continue
        actual = _nested_value(row, key)
        if isinstance(wanted, dict):
            if '$elemMatch' in wanted and not any(matches(item, wanted['$elemMatch']) for item in actual or []):
                return False
            if '$exists' in wanted and (actual is not None) != wanted['$exists']:
                return False
            if '$ne' in wanted and actual == wanted['$ne']:
                return False
        elif actual != wanted:
            return False
    return True


class Sessions:
    def __init__(self, documents):
        self.documents = deepcopy(documents)
        self.updates = []

    async def create_index(self, *args, **kwargs):
        pass

    async def find_one(self, query):
        return next((deepcopy(row) for row in self.documents if matches(row, query)), None)

    def find(self, query, projection=None):
        return FakeCursor([row for row in self.documents if matches(row, query)])

    async def update_one(self, query, update):
        self.updates.append(deepcopy(query))
        for row in self.documents:
            if not matches(row, query):
                continue
            targets = {
                key: next(item for item in row[key] if matches(item, value['$elemMatch']))
                for key, value in query.items()
                if isinstance(value, dict) and '$elemMatch' in value
            }
            for key, value in update.get('$set', {}).items():
                if '.$.' in key:
                    prefix, field = key.split('.$.')
                    target = targets[prefix]
                    target[field] = deepcopy(value)
                else:
                    _set_nested(row, key, value)
            return SimpleNamespace(acknowledged=True, matched_count=1)
        return SimpleNamespace(acknowledged=True, matched_count=0)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['chat', 'record'])
async def test_managed_pending_intents_release_fixed_scan_batch_and_do_not_fake_jobs(kind):
    now = datetime(2026, 11, 9, tzinfo=UTC)
    documents = []
    for index in range(3):
        row = {
            'userId': 'u',
            'sessionId': f'session-{index}',
            'type': 'qa',
            'status': 'completed',
            'qaRound': 1,
            'qaSourceRevision': 1,
            'qaProfileIngestion': {'status': 'pending', 'jobId': None, 'updatedAt': now},
        }
        if kind == 'chat':
            row.update(
                type='chat',
                messages=[
                    {
                        'messageId': f'message-{index}',
                        'messageRevision': 1,
                        'role': 'user',
                        'content': 'user statement',
                        'timestamp': now,
                    }
                ],
                chatProfileIngestion=[
                    {
                        'messageId': f'message-{index}',
                        'sourceRevision': 1,
                        'status': 'pending',
                        'jobId': None,
                        'updatedAt': now,
                    }
                ],
            )
        documents.append(row)
    collection = Sessions(documents)
    repository = InMemoryJobRepository()

    async def managed(user_id, subject_id, source_type, source_id):
        return source_id.endswith(('-0', '-1'))

    repository.is_source_publication_managed = managed
    queue = HealthProfileJobQueue(repository, clock=lambda: now)
    service = (
        ChatProfileSourceService(MongoChatProfileStore(collection), queue, clock=lambda: now)
        if kind == 'chat'
        else RecordProfileSourceService(MongoRecordProfileStore(collection), queue, clock=lambda: now)
    )
    assert await service.recover_pending(limit=2) == 0
    assert not repository.jobs
    assert await service.recover_pending(limit=2) == 1
    assert len(repository.jobs) == 1
    for row in collection.documents[:2]:
        state = row['chatProfileIngestion'][0] if kind == 'chat' else row['qaProfileIngestion']
        assert state['status'] == 'source_managed' and state['jobId'] is None
        assert state['reasonCode'] == 'source.managed_by_publication'
    cas = collection.updates[0]
    assert (
        cas['chatProfileIngestion']['$elemMatch']['status'] if kind == 'chat' else cas['qaProfileIngestion.status']
    ) == 'pending'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['chat', 'record'])
async def test_retirement_does_not_overwrite_newer_intent_revision(kind):
    now = datetime(2026, 11, 9, tzinfo=UTC)
    collection = Sessions(
        [
            {
                'userId': 'u',
                'sessionId': 's',
                'type': 'qa',
                'status': 'completed',
                'qaRound': 1,
                'qaSourceRevision': 2,
                'qaProfileIngestion': {'status': 'pending'},
                'messages': [{'messageId': 'm', 'role': 'user', 'content': 'user', 'timestamp': now}],
                'chatProfileIngestion': [{'messageId': 'm', 'sourceRevision': 2, 'status': 'pending'}],
            }
        ]
    )
    if kind == 'chat':
        collection.documents[0]['type'] = 'chat'
        store = MongoChatProfileStore(collection)
        record = SimpleNamespace(user_id='u', session_id='s', message_id='m', source_revision=1)
    else:
        store = MongoRecordProfileStore(collection)
        record = SimpleNamespace(user_id='u', session_id='s', source_revision=1, session={'qaSourceRevision': 1})
    current = await store.mark_source_managed(record, now)
    assert current.source_revision == 2 and current.ingestion_status == 'pending'
