"""普通聊天用户消息画像任务投递测试。"""

from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace

from health_profile import ChatProfileSourceService
from health_profile import HealthProfileJobQueue
from health_profile import MongoChatProfileStore
from health_profile import ProfileIngestionSourceType

from tests.unit.health_profile.test_record_profile_source import (
    InMemoryJobRepository,
)


class FakeSessionCollection:
    def __init__(self, document=None) -> None:
        self.document = deepcopy(document)
        self.indexes = []

    async def create_index(self, fields, **options):
        self.indexes.append((deepcopy(fields), deepcopy(options)))
        return options['name']

    async def find_one(self, filters):
        if self.document is None:
            return None
        if self.document.get('userId') != filters.get('userId'):
            return None
        expected = filters.get('messages', {}).get('$elemMatch', {})
        if not any(
                message.get('messageId') == expected.get('messageId')
                and message.get('role') == expected.get('role')
                for message in self.document.get('messages', ())
        ):
            return None
        return deepcopy(self.document)

    def find(self, filters, projection=None):
        states = (
            []
            if self.document is None
            else self.document.get('chatProfileIngestion', [])
        )
        documents = []
        if any(
                isinstance(state, dict)
                and state.get('status') == 'pending'
                for state in states
        ):
            documents.append(self.document)
        return FakeCursor(documents)

    async def update_one(self, filters, update):
        if (
                self.document is None
                or self.document.get('userId') != filters.get('userId')
                or self.document.get('sessionId') != filters.get('sessionId')
        ):
            return SimpleNamespace(acknowledged=True, matched_count=0)
        expected = filters['chatProfileIngestion']['$elemMatch']['messageId']
        state = next((
            item for item in self.document.get('chatProfileIngestion', ())
            if item.get('messageId') == expected
        ), None)
        if state is None:
            return SimpleNamespace(acknowledged=True, matched_count=0)
        prefix = 'chatProfileIngestion.$.'
        for path, value in update.get('$set', {}).items():
            if path.startswith(prefix):
                state[path[len(prefix):]] = deepcopy(value)
        return SimpleNamespace(acknowledged=True, matched_count=1)


class FakeCursor:
    def __init__(self, documents):
        self.documents = [deepcopy(item) for item in documents]

    def sort(self, fields):
        return self

    async def to_list(self, length=None):
        return deepcopy(self.documents[:length])


class ChatProfileSourceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc)
        self.session = {
            'userId': 'user-1',
            'sessionId': 'session-1',
            'type': 'chat',
            'status': 'active',
            'messages': [],
            'chatProfileIngestion': [],
        }
        self.message = {
            'messageId': 'message-1',
            'messageRevision': 1,
            'role': 'user',
            'content': '我最近经常头痛',
            'timestamp': '2026-08-18T18:00:00+08:00',
        }
        self.collection = FakeSessionCollection()
        self.job_repository = InMemoryJobRepository()
        self.service = self._service(self.job_repository)

    def _service(self, repository):
        return ChatProfileSourceService(
            MongoChatProfileStore(self.collection),
            HealthProfileJobQueue(
                repository,
                clock=lambda: self.now,
                id_factory=lambda prefix: f'{prefix}-chat-1',
            ),
            clock=lambda: self.now,
        )

    def _persist_prepared_message(self):
        self.session['messages'].append(deepcopy(self.message))
        self.service.prepare_user_message(
            self.session,
            self.session['messages'][-1],
        )
        self.collection.document = deepcopy(self.session)

    def test_prepare_tracks_only_stable_user_message(self):
        self.session['messages'].append(deepcopy(self.message))

        self.service.prepare_user_message(
            self.session,
            self.session['messages'][0],
        )
        self.service.prepare_user_message(
            self.session,
            self.session['messages'][0],
        )

        self.assertEqual(len(self.session['chatProfileIngestion']), 1)
        state = self.session['chatProfileIngestion'][0]
        self.assertEqual(state['messageId'], 'message-1')
        self.assertEqual(state['status'], 'pending')
        self.assertEqual(state['updatedAt'], self.now)
        assistant = deepcopy(self.message)
        assistant['role'] = 'assistant'
        with self.assertRaisesRegex(ValueError, 'only user'):
            self.service.prepare_user_message(self.session, assistant)

    async def test_user_message_is_queued_idempotently(self):
        self._persist_prepared_message()

        first = await self.service.ensure_queued(
            user_id='user-1',
            message_id='message-1',
        )
        second = await self.service.ensure_queued(
            user_id='user-1',
            message_id='message-1',
        )

        self.assertTrue(first.ingestion_queued)
        self.assertTrue(second.ingestion_queued)
        self.assertEqual(first.record.content, '我最近经常头痛')
        self.assertEqual(len(self.job_repository.jobs), 1)
        job = next(iter(self.job_repository.jobs.values()))
        self.assertIs(job.source_type, ProfileIngestionSourceType.CHAT)
        self.assertEqual(job.source_id, 'message-1')
        self.assertIsNone(job.subject_id)
        self.assertEqual(
            job.idempotency_key,
            'user-1:chat:session-1:message-1:1',
        )
        state = self.collection.document['chatProfileIngestion'][0]
        self.assertEqual(state['status'], 'queued')
        self.assertEqual(state['jobId'], job.job_id)
        self.assertEqual(len(self.collection.indexes), 2)

    async def test_queue_failure_keeps_message_pending(self):
        self._persist_prepared_message()
        service = self._service(InMemoryJobRepository(fail_create=True))

        result = await service.ensure_queued(
            user_id='user-1',
            message_id='message-1',
        )

        self.assertIsNone(result)
        self.assertEqual(
            self.collection.document['chatProfileIngestion'][0]['status'],
            'pending',
        )

    async def test_pending_message_is_recovered_idempotently(self):
        self._persist_prepared_message()
        failing = self._service(InMemoryJobRepository(fail_create=True))
        await failing.ensure_queued(
            user_id='user-1',
            message_id='message-1',
        )
        recovery = self._service(self.job_repository)

        first = await recovery.recover_pending(limit=10)
        replay = await recovery.recover_pending(limit=10)

        self.assertEqual(first, 1)
        self.assertEqual(replay, 0)
        self.assertEqual(len(self.job_repository.jobs), 1)
        self.assertEqual(
            self.collection.document['chatProfileIngestion'][0]['status'],
            'queued',
        )

    async def test_assistant_and_custom_messages_are_not_sources(self):
        self.collection.document = {
            **deepcopy(self.session),
            'messages': [
                {
                    **deepcopy(self.message),
                    'messageId': 'assistant-1',
                    'role': 'assistant',
                    'reasoning': '模型推理',
                },
                {
                    **deepcopy(self.message),
                    'messageId': 'custom-1',
                    'role': 'custom',
                },
            ],
            'chatProfileIngestion': [],
        }

        assistant = await self.service.ensure_queued(
            user_id='user-1',
            message_id='assistant-1',
        )
        custom = await self.service.ensure_queued(
            user_id='user-1',
            message_id='custom-1',
        )

        self.assertIsNone(assistant)
        self.assertIsNone(custom)
        self.assertEqual(self.job_repository.jobs, {})


if __name__ == '__main__':
    unittest.main()
