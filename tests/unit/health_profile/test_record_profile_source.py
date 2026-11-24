"""已完成建档问答的画像任务投递测试。"""

from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace

from health_profile import HealthProfileJobQueue
from health_profile import MongoRecordProfileStore
from health_profile import ProfileIngestionSourceType
from health_profile import RecordProfileSourceService


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
        if all(self.document.get(key) == value for key, value in filters.items()):
            return deepcopy(self.document)
        return None

    def find(self, filters, projection=None):
        document = self.document
        if document is None:
            return FakeCursor([])
        ingestion = document.get('qaProfileIngestion', {})
        completed = (
            document.get('type') == 'qa'
            and document.get('status') == 'completed'
        ) or (
            document.get('type') == 'chat'
            and document.get('qaCompletedAt') is not None
        )
        if ingestion.get('status') == 'pending' and completed:
            return FakeCursor([document])
        return FakeCursor([])

    async def update_one(self, filters, update):
        if self.document is None or not all(
                self.document.get(key) == value
                for key, value in filters.items()
        ):
            return SimpleNamespace(acknowledged=True, matched_count=0)
        for path, value in update.get('$set', {}).items():
            _set_path(self.document, path, deepcopy(value))
        return SimpleNamespace(acknowledged=True, matched_count=1)


class FakeCursor:
    def __init__(self, documents):
        self.documents = [deepcopy(item) for item in documents]

    def sort(self, fields):
        return self

    async def to_list(self, length=None):
        return deepcopy(self.documents[:length])


def _set_path(document, path, value):
    target = document
    parts = path.split('.')
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


class InMemoryJobRepository:
    def __init__(self, *, fail_create=False) -> None:
        self.fail_create = fail_create
        self.jobs = {}

    async def create_job(self, job):
        if self.fail_create:
            raise RuntimeError('queue unavailable')
        existing = self.jobs.get(job.idempotency_key)
        if existing is not None:
            return existing, False
        self.jobs[job.idempotency_key] = job
        return job, True

    async def get_job_by_idempotency(self, user_id, idempotency_key):
        return self.jobs.get(idempotency_key)

    async def claim_next_job(self, **kwargs):
        raise AssertionError('本测试不消费任务')

    async def complete_job(self, **kwargs):
        raise AssertionError('本测试不完成任务')

    async def fail_job(self, **kwargs):
        raise AssertionError('本测试不处理失败任务')


class RecordProfileSourceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc)
        self.document = {
            'userId': 'user-1',
            'sessionId': 'session-1',
            'type': 'qa',
            'status': 'completed',
            'round': 2,
            'completedAt': '2026-08-18T17:59:00',
            'qaHistory': [
                {
                    'question': {'questionId': 'q-sleep'},
                    'answer': ['睡眠较差'],
                },
            ],
            'qaSourceRevision': 1,
            'qaProfileIngestion': {
                'status': 'pending',
                'jobId': None,
                'updatedAt': self.now,
            },
        }
        self.collection = FakeSessionCollection(self.document)
        self.job_repository = InMemoryJobRepository()
        self.service = self._service(self.job_repository)

    def _service(self, repository):
        return RecordProfileSourceService(
            MongoRecordProfileStore(self.collection),
            HealthProfileJobQueue(
                repository,
                clock=lambda: self.now,
                id_factory=lambda prefix: f'{prefix}-1',
            ),
            clock=lambda: self.now,
        )

    def test_prepare_marks_only_completed_qa_as_pending(self) -> None:
        session = deepcopy(self.document)
        session.pop('qaSourceRevision')
        session.pop('qaProfileIngestion')

        self.service.prepare_completed_snapshot(session)

        self.assertEqual(session['qaSourceRevision'], 1)
        self.assertEqual(session['qaProfileIngestion']['status'], 'pending')
        self.assertEqual(
            session['qaProfileIngestion']['updatedAt'],
            self.now,
        )
        session['status'] = 'active'
        with self.assertRaisesRegex(ValueError, 'completed'):
            self.service.prepare_completed_snapshot(session)

    async def test_completed_snapshot_is_queued_idempotently(self) -> None:
        first = await self.service.ensure_queued(
            user_id='user-1',
            session_id='session-1',
        )
        second = await self.service.ensure_queued(
            user_id='user-1',
            session_id='session-1',
        )

        self.assertTrue(first.ingestion_queued)
        self.assertTrue(second.ingestion_queued)
        self.assertEqual(len(self.job_repository.jobs), 1)
        job = next(iter(self.job_repository.jobs.values()))
        self.assertEqual(job.source_type, ProfileIngestionSourceType.RECORD)
        self.assertEqual(job.source_id, 'session-1')
        self.assertIsNone(job.subject_id)
        self.assertEqual(
            job.idempotency_key,
            'user-1:record:session-1:2:1',
        )
        self.assertEqual(
            self.collection.document['qaProfileIngestion']['status'],
            'queued',
        )
        self.assertEqual(len(self.collection.indexes), 1)

    async def test_queue_failure_keeps_completed_snapshot_pending(self) -> None:
        service = self._service(InMemoryJobRepository(fail_create=True))

        result = await service.ensure_queued(
            user_id='user-1',
            session_id='session-1',
        )

        self.assertIsNone(result)
        self.assertEqual(
            self.collection.document['qaProfileIngestion']['status'],
            'pending',
        )

    async def test_pending_snapshot_is_recovered_idempotently(self) -> None:
        failing = self._service(InMemoryJobRepository(fail_create=True))
        await failing.ensure_queued(
            user_id='user-1',
            session_id='session-1',
        )
        recovery = self._service(self.job_repository)

        first = await recovery.recover_pending(limit=10)
        replay = await recovery.recover_pending(limit=10)

        self.assertEqual(first, 1)
        self.assertEqual(replay, 0)
        self.assertEqual(len(self.job_repository.jobs), 1)
        self.assertEqual(
            self.collection.document['qaProfileIngestion']['status'],
            'queued',
        )

    async def test_incomplete_qa_is_not_queued(self) -> None:
        self.collection.document['status'] = 'awaiting_decision'

        result = await self.service.ensure_queued(
            user_id='user-1',
            session_id='session-1',
        )

        self.assertIsNone(result)
        self.assertEqual(self.job_repository.jobs, {})

    async def test_converted_chat_remains_a_readable_record_source(self) -> None:
        self.collection.document.update({
            'type': 'chat',
            'status': 'active',
            'qaRound': self.collection.document.pop('round'),
            'qaCompletedAt': self.collection.document.pop('completedAt'),
        })

        result = await self.service.ensure_queued(
            user_id='user-1',
            session_id='session-1',
        )

        self.assertTrue(result.ingestion_queued)
        self.assertEqual(result.record.round_number, 2)


if __name__ == '__main__':
    unittest.main()
