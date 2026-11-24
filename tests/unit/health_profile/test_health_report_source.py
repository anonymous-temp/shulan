"""体检报告追加存储与画像任务投递测试。"""

from __future__ import annotations

import unittest
from datetime import datetime
from datetime import timezone

from health_profile import HealthProfileJobQueue
from health_profile import HealthReportSourceConflictError
from health_profile import HealthReportSourceService
from health_profile import MongoHealthReportStore
from health_profile import ProfileIngestionSourceType
from tests.unit._support.document_profile_composition.profile.health_report_source import FakeCursor as FakeCursor
from tests.unit._support.document_profile_composition.profile.health_report_source import (
    FakeReportCollection as FakeReportCollection,
)
from tests.unit._support.document_profile_composition.profile.health_report_source import (
    InMemoryJobRepository as InMemoryJobRepository,
)
from tests.unit._support.document_profile_composition.profile.health_report_source import _set_path as _set_path


class HealthReportSourceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)
        self.collection = FakeReportCollection()
        self.job_repository = InMemoryJobRepository()
        self.id_sequence = 0

        def next_id(prefix: str) -> str:
            self.id_sequence += 1
            return f'{prefix}-{self.id_sequence}'

        self.service = HealthReportSourceService(
            MongoHealthReportStore(self.collection),
            HealthProfileJobQueue(
                self.job_repository,
                clock=lambda: self.now,
                id_factory=next_id,
            ),
            clock=lambda: self.now,
            id_factory=lambda: 'report-generated-1',
        )
        self.result = {
            'reportType': 'lab',
            'title': '血常规',
            'reportTime': '2026-08-18',
            'status': 'normal',
            'aiSummary': '本次报告未见明显异常。',
            'expansion': [],
        }

    async def test_jpeg_fingerprint_alias_reuses_existing_source_without_weakening_conflicts(self):
        url = 'https://example.test/download'
        old_fingerprint = self.fingerprint(file_id=f'{url}#fileType=jpeg')
        await self.save(fingerprint=old_fingerprint)
        result = await self.service.get_existing(
            report_id='report-1',
            source_fingerprint=self.fingerprint(file_id=f'{url}#fileType=jpg'),
            source_fingerprint_aliases=(old_fingerprint,),
        )
        self.assertEqual(result.record.source_fingerprint, old_fingerprint)
        self.assertEqual(len(self.collection.documents), 1)
        with self.assertRaises(HealthReportSourceConflictError):
            await self.service.get_existing(
                report_id='report-1',
                source_fingerprint=self.fingerprint(file_id='https://example.test/other#fileType=jpg'),
                source_fingerprint_aliases=(self.fingerprint(file_id='https://example.test/other#fileType=jpeg'),),
            )

    def fingerprint(self, *, file_id='https://example.test/report.pdf'):
        return self.service.source_fingerprint(
            user_id='user-1',
            subject_id='subject-self',
            file_id=file_id,
        )

    async def save(self, *, report_id='report-1', fingerprint=None):
        return await self.service.save(
            report_id=report_id,
            user_id='user-1',
            subject_id='subject-self',
            source_fingerprint=fingerprint or self.fingerprint(),
            result=self.result,
        )

    async def test_reports_are_appended_and_receive_stable_ids(self) -> None:
        first = await self.save(report_id='report-1')
        second = await self.save(report_id='report-2')

        self.assertTrue(first.created)
        self.assertTrue(second.created)
        self.assertEqual(len(self.collection.documents), 2)
        self.assertEqual(
            {item['reportId'] for item in self.collection.documents},
            {'report-1', 'report-2'},
        )
        self.assertEqual(first.record.public_result()['reportId'], 'report-1')
        self.assertEqual(len(self.collection.indexes), 4)

    async def test_saved_report_is_idempotent_and_job_is_queued_once(self) -> None:
        first = await self.save()
        repeated = await self.save()

        self.assertTrue(first.created)
        self.assertFalse(repeated.created)
        self.assertTrue(first.ingestion_queued)
        self.assertTrue(repeated.ingestion_queued)
        self.assertEqual(len(self.collection.documents), 1)
        self.assertEqual(len(self.job_repository.jobs), 1)
        job = next(iter(self.job_repository.jobs.values()))
        self.assertEqual(
            job.source_type,
            ProfileIngestionSourceType.HEALTH_REPORT,
        )
        self.assertEqual(job.source_id, 'report-1')
        self.assertEqual(job.subject_id, 'subject-self')
        self.assertEqual(
            self.collection.documents[0]['profileIngestion']['status'],
            'queued',
        )

    async def test_same_report_id_with_different_source_is_rejected(self) -> None:
        await self.save()

        with self.assertRaises(HealthReportSourceConflictError):
            await self.save(
                fingerprint=self.fingerprint(
                    file_id='https://example.test/another.pdf',
                ),
            )

    async def test_queue_failure_keeps_report_pending_for_compensation(self) -> None:
        service = HealthReportSourceService(
            MongoHealthReportStore(self.collection),
            HealthProfileJobQueue(
                InMemoryJobRepository(fail_create=True),
                clock=lambda: self.now,
            ),
            clock=lambda: self.now,
        )

        saved = await service.save(
            report_id='report-pending',
            user_id='user-1',
            subject_id='subject-self',
            source_fingerprint=service.source_fingerprint(
                user_id='user-1',
                subject_id='subject-self',
                file_id='https://example.test/report.pdf',
            ),
            result=self.result,
        )

        self.assertTrue(saved.created)
        self.assertFalse(saved.ingestion_queued)
        self.assertEqual(saved.record.ingestion_status, 'pending')
        self.assertEqual(
            self.collection.documents[0]['profileIngestion']['status'],
            'pending',
        )

    async def test_report_without_subject_waits_without_queueing_job(self) -> None:
        fingerprint = self.service.source_fingerprint(
            user_id='user-1',
            subject_id=None,
            file_id='https://example.test/report.pdf',
        )

        saved = await self.service.save(
            report_id='report-unassigned',
            user_id='user-1',
            subject_id=None,
            source_fingerprint=fingerprint,
            result=self.result,
        )
        replayed = await self.service.get_existing(
            report_id='report-unassigned',
            source_fingerprint=fingerprint,
        )

        self.assertTrue(saved.created)
        self.assertFalse(saved.ingestion_queued)
        self.assertEqual(saved.record.ingestion_status, 'awaiting_subject')
        self.assertIsNotNone(replayed)
        self.assertFalse(replayed.ingestion_queued)
        self.assertEqual(len(self.job_repository.jobs), 0)
        self.assertEqual(
            self.collection.documents[0]['profileIngestion']['status'],
            'awaiting_subject',
        )

    async def test_pending_report_is_recovered_idempotently(self) -> None:
        failing = HealthReportSourceService(
            MongoHealthReportStore(self.collection),
            HealthProfileJobQueue(
                InMemoryJobRepository(fail_create=True),
                clock=lambda: self.now,
            ),
            clock=lambda: self.now,
        )
        await failing.save(
            report_id='report-pending',
            user_id='user-1',
            subject_id='subject-self',
            source_fingerprint=failing.source_fingerprint(
                user_id='user-1',
                subject_id='subject-self',
                file_id='https://example.test/report.pdf',
            ),
            result=self.result,
        )
        recovery = HealthReportSourceService(
            MongoHealthReportStore(self.collection),
            HealthProfileJobQueue(
                self.job_repository,
                clock=lambda: self.now,
            ),
            clock=lambda: self.now,
        )

        first = await recovery.recover_pending(limit=10)
        replay = await recovery.recover_pending(limit=10)

        self.assertEqual(first, 1)
        self.assertEqual(replay, 0)
        self.assertEqual(len(self.job_repository.jobs), 1)
        self.assertEqual(
            self.collection.documents[0]['profileIngestion']['status'],
            'queued',
        )

    async def test_explicit_report_id_replay_can_skip_model_work(self) -> None:
        await self.save()

        existing = await self.service.get_existing(
            report_id='report-1',
            source_fingerprint=self.fingerprint(),
        )

        self.assertIsNotNone(existing)
        self.assertFalse(existing.created)
        self.assertEqual(existing.record.result, self.result)

    def test_report_id_format_is_strict(self) -> None:
        with self.assertRaisesRegex(ValueError, 'reportId'):
            self.service.resolve_report_id('report id with spaces')


if __name__ == '__main__':
    unittest.main()
