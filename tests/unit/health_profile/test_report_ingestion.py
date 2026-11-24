"""体检报告候选映射、事实写入和摄取 Worker 测试。"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from health_profile import FactStatus
from health_profile import FactTypeRegistry
from health_profile import HealthProfileIngestionWorker
from health_profile import HealthProfileJobQueue
from health_profile import HealthReportCandidateMapper
from health_profile import HealthReportProfileProcessor
from health_profile import HealthSubject
from health_profile import ProfileIngestionProcessingError
from health_profile import ProfileIngestionProcessingResult
from health_profile import ProfileIngestionProcessorRouter
from health_profile import ProfileIngestionSourceType
from health_profile import SubjectKind
from health_profile import SubjectStatus
from health_profile import VerificationStatus
from health_profile.models import HealthProfileView
from tests.unit._support.document_profile_composition.profile.report_ingestion import (
    ReportIngestionFixture as ReportIngestionFixture,
)


class FakeReportStore:
    def __init__(self, record=None) -> None:
        self.record = record

    async def get_report(self, report_id):
        if self.record is not None and self.record.report_id == report_id:
            return self.record
        return None

    async def create_report(self, record):
        raise AssertionError('处理器不创建来源报告')

    async def mark_ingestion_queued(self, **kwargs):
        raise AssertionError('处理器不修改投递状态')


class FakeProfileRepository:
    """实现 HealthProfileRepository 契约的最小内存版本。"""

    def __init__(self, subject: HealthSubject) -> None:
        self.subject = subject
        self.additional_subjects = []
        self.additional_views = {}
        self.view = HealthProfileView(
            user_id=subject.user_id,
            subject_id=subject.subject_id,
            profile_version=1,
            subject_summary={'displayName': subject.display_name},
            updated_at=subject.updated_at,
        )
        self.facts = []
        self.events = []

    async def ensure_indexes(self):
        return None

    async def get_self_subject(self, user_id):
        return self.subject if self.subject.kind is SubjectKind.SELF else None

    async def get_subject(self, user_id, subject_id):
        if self.subject.user_id == user_id and self.subject.subject_id == subject_id:
            return self.subject
        return next(
            (
                subject
                for subject in self.additional_subjects
                if subject.user_id == user_id and subject.subject_id == subject_id
            ),
            None,
        )

    async def list_subjects(self, user_id, *, include_archived=False):
        subjects = tuple(
            subject
            for subject in (self.subject, *self.additional_subjects)
            if (subject.user_id == user_id and (include_archived or subject.status is not SubjectStatus.ARCHIVED))
        )
        return subjects

    async def create_self_subject(self, subject):
        raise AssertionError('处理器不创建主体')

    async def create_subject(self, subject):
        existing = next(
            (
                item
                for item in self.additional_subjects
                if (
                    item.user_id == subject.user_id
                    and (
                        item.subject_id == subject.subject_id
                        or (
                            subject.creation_request_id is not None
                            and item.creation_request_id == subject.creation_request_id
                        )
                    )
                )
            ),
            None,
        )
        if existing is not None:
            return existing, False
        self.additional_subjects.append(subject)
        return subject, True

    async def update_subject(self, subject, *, expected_version):
        raise AssertionError('处理器不更新主体')

    async def get_view(self, user_id, subject_id):
        if self.view.user_id == user_id and self.view.subject_id == subject_id:
            return self.view
        view = self.additional_views.get(subject_id)
        if view is not None and view.user_id == user_id:
            return view
        return None

    async def ensure_initial_view(self, view):
        existing = await self.get_view(view.user_id, view.subject_id)
        if existing is not None:
            return existing
        if view.subject_id == self.subject.subject_id:
            self.view = view
        else:
            self.additional_views[view.subject_id] = view
        return view

    async def advance_subject_view(self, subject, *, request_id, updated_at):
        raise AssertionError('处理器不修改主体投影')

    async def list_facts(self, user_id, subject_id, *, statuses):
        allowed = set(statuses)
        return tuple(
            fact
            for fact in self.facts
            if fact.user_id == user_id and fact.subject_id == subject_id and fact.status in allowed
        )

    async def get_fact(self, user_id, subject_id, fact_id):
        return next(
            (
                fact
                for fact in self.facts
                if fact.user_id == user_id and fact.subject_id == subject_id and fact.fact_id == fact_id
            ),
            None,
        )

    async def list_fact_page(self, *args, **kwargs):
        raise AssertionError('处理器不分页查询事实')

    async def replace_fact(self, fact, **kwargs):
        raise AssertionError('处理器不替换事实')

    async def create_fact(self, fact):
        existing = next(
            (
                item
                for item in self.facts
                if item.user_id == fact.user_id
                and item.creation_request_id == fact.creation_request_id
                and item.creation_request_slot == fact.creation_request_slot
            ),
            None,
        )
        if existing is not None:
            return existing, False
        self.facts.append(fact)
        return fact, True

    async def mark_fact_deleted(self, **kwargs):
        raise AssertionError('处理器不删除事实')

    async def get_deleted_fact_tombstone(self, *args, **kwargs):
        raise AssertionError('处理器不读取墓碑')

    async def advance_fact_view(
        self,
        *,
        user_id,
        subject_id,
        expected_profile_version,
        request_id,
        category_counts,
        key_fact_ids,
        pending_confirmation_count,
        last_source_at,
        updated_at,
    ):
        view = await self.get_view(user_id, subject_id)
        if view is None:
            raise AssertionError('事实主体缺少画像投影')
        if view.last_request_id == request_id:
            return view
        if view.profile_version != expected_profile_version:
            return None
        view = view.model_copy(
            update={
                'profile_version': view.profile_version + 1,
                'category_counts': dict(category_counts),
                'key_fact_ids': tuple(key_fact_ids),
                'pending_confirmation_count': pending_confirmation_count,
                'last_source_at': last_source_at,
                'last_request_id': request_id,
                'updated_at': updated_at,
            }
        )
        if subject_id == self.subject.subject_id:
            self.view = view
        else:
            self.additional_views[subject_id] = view
        return view

    async def get_event_by_request(self, user_id, request_id):
        return next(
            (event for event in self.events if event.user_id == user_id and event.request_id == request_id), None
        )

    async def list_event_page(self, *args, **kwargs):
        raise AssertionError('处理器不分页查询事件')

    async def ensure_subject_created_event(self, event):
        raise AssertionError('处理器不创建主体事件')

    async def append_event(self, event):
        existing = await self.get_event_by_request(
            event.user_id,
            event.request_id,
        )
        if existing is not None:
            return existing
        self.events.append(event)
        return event


class HealthReportCandidateMapperTest(unittest.TestCase):
    def test_report_flag_aliases_share_direction_without_turning_risk_labels_into_measurements(self):
        for flag, expected in (
            ('超标', 'high'),
            ('缺乏', 'low'),
            ('正常', 'normal'),
            ('风险升高', None),
            ('需求高', None),
            ('提示危急值', 'critical'),
        ):
            with self.subTest(flag=flag):
                original = ReportIngestionFixture.record().result
                record = ReportIngestionFixture.record(
                    result={
                        **original,
                        'expansion': [{**original['expansion'][0], 'flag': flag}],
                    }
                )
                self.assertEqual(HealthReportCandidateMapper().map(record).candidates[0].interpretation, expected)

    def test_maps_standard_and_preserves_unregistered_items_with_report_time(self):
        result = HealthReportCandidateMapper().map(
            ReportIngestionFixture.record(),
        )

        self.assertEqual(result.source_item_count, 2)
        self.assertEqual(result.skipped_item_count, 0)
        self.assertEqual(len(result.candidates), 2)
        self.assertTrue(result.candidates[1].fact_type.startswith('report_observation.'))
        candidate = result.candidates[0]
        self.assertEqual(candidate.fact_type, 'laboratory.uric_acid')
        self.assertEqual(candidate.value['number'], 468)
        self.assertEqual(candidate.value['unit'], 'μmol/L')
        self.assertEqual(candidate.interpretation, 'high')
        self.assertEqual(candidate.reference_range['low'], 208.0)
        self.assertEqual(candidate.reference_range['high'], 428.0)
        self.assertEqual(
            candidate.effective_period.start.isoformat(),
            '2026-08-16T00:00:00+08:00',
        )

    def test_missing_report_time_is_skipped_and_imaging_remains_pending(self):
        without_time = ReportIngestionFixture.record(
            result={
                **ReportIngestionFixture.record().result,
                'reportTime': None,
            },
        )
        imaging = ReportIngestionFixture.record(
            result={
                'reportType': 'imaging',
                'reportTime': '2026-08-16',
                'expansion': {'diagnosis': '影像诊断文本'},
            }
        )

        missing_result = HealthReportCandidateMapper().map(without_time)
        imaging_result = HealthReportCandidateMapper().map(imaging)

        self.assertEqual(missing_result.candidates, ())
        self.assertIn('report_time_unavailable', missing_result.reason_codes)
        self.assertEqual(len(imaging_result.candidates), 1)
        self.assertEqual(imaging_result.candidates[0].fact_type, 'condition.diagnosis')
        self.assertEqual(imaging_result.candidates[0].assertion_level.value, 'user_reported')

    def test_registry_contains_first_laboratory_types(self):
        definition = FactTypeRegistry.default().require(
            'laboratory.uric_acid',
        )
        self.assertEqual(definition.category.value, 'laboratory')


class HealthReportProfileProcessorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.record = ReportIngestionFixture.record()
        self.repository = FakeProfileRepository(
            ReportIngestionFixture.subject(),
        )
        self.id_sequence = 0

        def next_id(prefix):
            self.id_sequence += 1
            return f'{prefix}-{self.id_sequence}'

        self.processor = HealthReportProfileProcessor(
            store=FakeReportStore(self.record),
            repository=self.repository,
            clock=lambda: ReportIngestionFixture.now,
            id_factory=next_id,
        )

    async def test_writes_fact_event_and_projection_idempotently(self):
        first = await self.processor.process(ReportIngestionFixture.job())
        replay = await self.processor.process(ReportIngestionFixture.job())

        self.assertEqual(first.candidate_count, 2)
        self.assertEqual(first.fact_count, 2)
        self.assertEqual(replay.fact_count, 0)
        self.assertEqual(replay.replayed_fact_count, 2)
        self.assertEqual(len(self.repository.facts), 2)
        fact = self.repository.facts[0]
        self.assertEqual(fact.status, FactStatus.ACTIVE)
        self.assertEqual(
            fact.verification_status,
            VerificationStatus.VERIFIED_SOURCE,
        )
        self.assertEqual(fact.sources[0].source_id, 'report-1')
        self.assertEqual(len(self.repository.events), 2)
        event = self.repository.events[0]
        self.assertEqual(event.event_type, 'fact_ingested')
        self.assertNotIn('value', event.operation_result)
        self.assertEqual(self.repository.view.profile_version, 3)
        self.assertEqual(
            self.repository.view.category_counts,
            {'laboratory': 1, 'report_observation': 1},
        )
        self.assertEqual(
            self.repository.view.key_fact_ids,
            tuple(item.fact_id for item in self.repository.facts),
        )

    async def test_partial_mapping_logs_reason_without_health_values(self):
        with patch('health_profile.report_ingestion.logger') as logger:
            await self.processor.process(ReportIngestionFixture.job())

        messages = '\n'.join(call.args[0] for call in logger.warning.call_args_list)
        self.assertIn('unmapped_item_preserved', messages)
        self.assertIn('skipped_item_count=0', messages)
        self.assertNotIn('尚未注册的指标', messages)
        self.assertNotIn('1.2', messages)

    async def test_legacy_unassigned_subject_job_completes_without_writing(self):
        unresolved_record = ReportIngestionFixture.record(subject_id=None)
        processor = HealthReportProfileProcessor(
            store=FakeReportStore(unresolved_record),
            repository=self.repository,
        )

        with patch('health_profile.report_ingestion.logger') as logger:
            result = await processor.process(
                ReportIngestionFixture.job(subject_id=None),
            )

        self.assertEqual(result.candidate_count, 0)
        self.assertEqual(result.fact_count, 0)
        self.assertEqual(self.repository.facts, [])
        messages = '\n'.join(call.args[0] for call in logger.info.call_args_list)
        self.assertIn('reason_code=subject_unassigned', messages)
        self.assertNotIn('血常规', messages)


class FakeWorkerQueue(HealthProfileJobQueue):
    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.completed = []
        self.failed = []

    async def claim(self, worker_id):
        return None if not self.jobs else self.jobs.pop(0)

    async def complete(self, job, **values):
        self.completed.append((job, values))
        return job

    async def fail(self, job, **values):
        self.failed.append((job, values))
        return job


class FakeWorkerProcessor:
    def __init__(self, *, error=None) -> None:
        self.error = error
        self.calls = []

    async def process(self, job):
        self.calls.append(job)
        if self.error is not None:
            raise self.error
        return ProfileIngestionProcessingResult(
            graph_version='graph-v1',
            policy_version='policy-v1',
            extractor_version='extractor-v1',
            candidate_count=1,
            fact_count=1,
            replayed_fact_count=0,
            rejected_candidate_count=0,
        )


class FakePendingDeliverySource:
    def __init__(self, recovered=1) -> None:
        self.recovered = recovered
        self.calls = []

    async def recover_pending(self, *, limit=100):
        self.calls.append(limit)
        return self.recovered


class HealthProfileIngestionWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_processor_router_dispatches_only_registered_sources(self):
        report_processor = FakeWorkerProcessor()
        router = ProfileIngestionProcessorRouter(
            {
                ProfileIngestionSourceType.HEALTH_REPORT: report_processor,
            }
        )

        result = await router.process(ReportIngestionFixture.job())

        self.assertEqual(result.fact_count, 1)
        self.assertEqual(
            router.source_types,
            (ProfileIngestionSourceType.HEALTH_REPORT,),
        )
        with self.assertRaises(ProfileIngestionProcessingError) as raised:
            await router.process(
                ReportIngestionFixture.job(
                    source_type=ProfileIngestionSourceType.RECORD,
                )
            )
        self.assertEqual(raised.exception.error_code, 'source.unsupported')

    async def test_worker_completes_successful_job_with_versions(self):
        queue = FakeWorkerQueue([ReportIngestionFixture.job()])
        processor = FakeWorkerProcessor()
        worker = HealthProfileIngestionWorker(
            queue,
            processor,
            worker_id='worker-1',
        )

        processed = await worker.drain_pending()

        self.assertEqual(processed, 1)
        self.assertEqual(len(queue.completed), 1)
        self.assertEqual(queue.completed[0][1]['graph_version'], 'graph-v1')
        self.assertEqual(queue.completed[0][1]['processing_summary'].fact_count, 1)
        self.assertEqual(queue.failed, [])

    async def test_worker_stops_claiming_after_current_job_finishes(self):
        first = ReportIngestionFixture.job(job_id='job-1')
        second = ReportIngestionFixture.job(job_id='job-2')
        queue = FakeWorkerQueue([first, second])
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingProcessor(FakeWorkerProcessor):
            async def process(self, job):
                self.calls.append(job)
                started.set()
                await release.wait()
                return ProfileIngestionProcessingResult(
                    graph_version='graph-v1',
                    policy_version='policy-v1',
                    extractor_version='extractor-v1',
                    candidate_count=1,
                    fact_count=1,
                    replayed_fact_count=0,
                    rejected_candidate_count=0,
                )

        processor = BlockingProcessor()
        worker = HealthProfileIngestionWorker(
            queue,
            processor,
            worker_id='worker-1',
            batch_size=2,
        )
        drain = asyncio.create_task(worker.drain_pending())
        await started.wait()

        worker.stop_accepting()
        release.set()
        processed = await drain

        self.assertEqual(processed, 1)
        self.assertEqual([job.job_id for job in processor.calls], ['job-1'])
        self.assertEqual([job.job_id for job in queue.jobs], ['job-2'])
        self.assertFalse(worker.accepting)

    async def test_worker_recovers_pending_sources_on_bounded_interval(self):
        queue = FakeWorkerQueue([])
        source = FakePendingDeliverySource(recovered=2)
        monotonic_time = 100.0
        worker = HealthProfileIngestionWorker(
            queue,
            FakeWorkerProcessor(),
            worker_id='worker-1',
            pending_sources={
                ProfileIngestionSourceType.HEALTH_REPORT: source,
            },
            recovery_batch_size=25,
            recovery_interval_seconds=30,
            monotonic_clock=lambda: monotonic_time,
        )

        with patch('health_profile.worker.logger') as logger:
            await worker.drain_pending()
            await worker.drain_pending()
            monotonic_time = 131.0
            await worker.drain_pending()

        self.assertEqual(source.calls, [25, 25])
        messages = '\n'.join(call.args[0] for call in logger.info.call_args_list)
        self.assertIn('source_type=health_report', messages)
        self.assertIn('recovered_count=2', messages)

    async def test_worker_persists_sanitized_processing_error(self):
        queue = FakeWorkerQueue([ReportIngestionFixture.job()])
        processor = FakeWorkerProcessor(
            error=ProfileIngestionProcessingError(
                'subject.unresolved',
                '报告内可能包含的详细错误不应进入任务',
            )
        )
        worker = HealthProfileIngestionWorker(
            queue,
            processor,
            worker_id='worker-1',
        )

        with patch('health_profile.worker.logger') as logger:
            await worker.drain_pending()

        self.assertEqual(queue.completed, [])
        self.assertEqual(
            queue.failed[0][1]['error_code'],
            'subject.unresolved',
        )
        messages = '\n'.join(call.args[0] for call in logger.warning.call_args_list)
        self.assertIn('error_code=subject.unresolved', messages)
        self.assertIn('用户画像摄取任务等待重试', messages)
        self.assertNotIn('报告内可能包含的详细错误', messages)


if __name__ == '__main__':
    unittest.main()
