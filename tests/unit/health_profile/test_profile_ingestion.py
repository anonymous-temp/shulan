"""用户画像摄取策略和持久化任务生命周期测试。"""

from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from health_profile import AssertionLevel
from health_profile import EffectivePeriod
from health_profile import FactSource
from health_profile import FactTypeRegistry
from health_profile import FactValueType
from health_profile import HealthFactPolicy
from health_profile import HealthProfileJobConflictError
from health_profile import HealthProfileJobLeaseError
from health_profile import HealthProfileJobMaintenanceService
from health_profile import HealthProfileJobNotFoundError
from health_profile import HealthProfileJobQueue
from health_profile import HealthProfileJobRedriveConflictError
from health_profile import MemoryCandidate
from health_profile import MongoHealthProfileRepository
from health_profile import PolicyDisposition
from health_profile import ProfileIngestionSourceType
from health_profile import ProfileJobStatus
from health_profile import SubjectKind
from health_profile.models import ProfileProcessingSummary


class MemoryCandidatePolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 18, 8, 30, tzinfo=timezone.utc)
        self.policy = HealthFactPolicy(FactTypeRegistry.default())

    def candidate(self, **changes) -> MemoryCandidate:
        values = {
            'candidate_id': 'candidate-1',
            'fact_type': 'anthropometric.weight',
            'display_name': '体重',
            'value_type': FactValueType.QUANTITY,
            'value': {'value': 70, 'unit': 'kg'},
            'assertion_level': AssertionLevel.OBSERVED,
            'confidence': 0.98,
            'effective_period': EffectivePeriod(start=self.now),
            'source': FactSource(
                source_type='health_report',
                source_id='report-1',
                source_revision=1,
            ),
            'subject_id': 'subject-self',
        }
        values.update(changes)
        return MemoryCandidate(**values)

    def test_verified_report_observation_can_become_active(self) -> None:
        decision = self.policy.evaluate(
            self.candidate(),
            subject_kind=SubjectKind.SELF,
        )

        self.assertEqual(decision.disposition, PolicyDisposition.ACTIVE)
        self.assertEqual(decision.reason_code, 'verified_report_source')
        self.assertEqual(decision.definition.fact_type, 'anthropometric.weight')

    def test_chat_and_model_inference_remain_pending(self) -> None:
        chat = self.candidate(
            assertion_level=AssertionLevel.USER_REPORTED,
            source=FactSource(
                source_type='chat',
                source_id='message-1',
                source_revision=1,
            ),
        )
        inference = self.candidate(
            assertion_level=AssertionLevel.MODEL_INFERRED,
        )

        chat_decision = self.policy.evaluate(
            chat,
            subject_kind=SubjectKind.SELF,
        )
        inference_decision = self.policy.evaluate(
            inference,
            subject_kind=SubjectKind.SELF,
        )

        self.assertEqual(chat_decision.disposition, PolicyDisposition.PENDING)
        self.assertEqual(
            inference_decision.reason_code,
            'model_inference_requires_review',
        )

    def test_unresolved_subject_does_not_enter_self_profile(self) -> None:
        decision = self.policy.evaluate(
            self.candidate(subject_id=None, subject_hint='我父亲'),
            subject_kind=SubjectKind.UNKNOWN,
        )

        self.assertEqual(
            decision.disposition,
            PolicyDisposition.NEEDS_SUBJECT_CONFIRMATION,
        )
        self.assertEqual(decision.reason_code, 'subject_unresolved')

    def test_family_history_requires_a_blood_relative_subject(self) -> None:
        candidate = self.candidate(
            fact_type='family_history.condition',
            display_name='家族疾病史',
            value_type=FactValueType.TEXT,
            value='高血压',
        )

        rejected = self.policy.evaluate(
            candidate,
            subject_kind=SubjectKind.FRIEND,
        )
        accepted = self.policy.evaluate(
            candidate,
            subject_kind=SubjectKind.BLOOD_RELATIVE,
        )

        self.assertEqual(rejected.disposition, PolicyDisposition.REJECTED)
        self.assertEqual(rejected.reason_code, 'subject_kind_not_allowed')
        self.assertEqual(accepted.disposition, PolicyDisposition.ACTIVE)

    def test_unknown_fact_type_and_unexpected_reasoning_are_rejected(self) -> None:
        decision = self.policy.evaluate(
            self.candidate(fact_type='model.created.type'),
            subject_kind=SubjectKind.SELF,
        )
        self.assertEqual(decision.disposition, PolicyDisposition.REJECTED)
        self.assertEqual(decision.reason_code, 'unknown_fact_type')

        payload = self.candidate().model_dump()
        payload['reasoning'] = '不允许保存模型思维链'
        with self.assertRaises(ValidationError):
            MemoryCandidate.model_validate(payload)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class FakeMongoCollection:
    """覆盖摄取任务所需 Mongo 原子操作的内存替身。"""

    def __init__(self) -> None:
        self.documents: list[dict] = []
        self.indexes: list[tuple[list, dict]] = []

    async def create_index(self, fields, **options):
        self.indexes.append((deepcopy(fields), deepcopy(options)))
        return options['name']

    async def find_one(self, filters):
        document = next(
            (item for item in self.documents if _matches(item, filters)),
            None,
        )
        return None if document is None else deepcopy(document)

    def find(self, filters):
        return FakeMongoCursor([deepcopy(item) for item in self.documents if _matches(item, filters)])

    async def insert_one(self, document):
        if any(item.get('_id') == document.get('_id') for item in self.documents):
            raise DuplicateKeyError('duplicate _id')
        if any(
            item.get('idempotencyKey') == document.get('idempotencyKey')
            for item in self.documents
            if document.get('idempotencyKey') is not None
        ):
            raise DuplicateKeyError('duplicate idempotency key')
        self.documents.append(deepcopy(document))
        return SimpleNamespace(acknowledged=True)

    async def find_one_and_update(
        self,
        filters,
        update,
        *,
        sort=None,
        return_document=None,
    ):
        candidates = [item for item in self.documents if _matches(item, filters)]
        if not candidates:
            return None
        if sort:
            candidates.sort(key=lambda item: tuple(item.get(field) for field, _direction in sort))
        document = candidates[0]
        for field, value in update.get('$set', {}).items():
            document[field] = deepcopy(value)
        for field, value in update.get('$inc', {}).items():
            document[field] = document.get(field, 0) + value
        for field in update.get('$unset', {}):
            document.pop(field, None)
        for field, operation in update.get('$push', {}).items():
            values = list(document.get(field, ()))
            if isinstance(operation, dict) and '$each' in operation:
                values.extend(deepcopy(operation['$each']))
                slice_value = operation.get('$slice')
                if isinstance(slice_value, int):
                    values = values[:slice_value] if slice_value >= 0 else values[slice_value:]
            else:
                values.append(deepcopy(operation))
            document[field] = values
        return deepcopy(document)


class FakeMongoCursor:
    def __init__(self, documents) -> None:
        self.documents = documents

    def sort(self, fields):
        for field, direction in reversed(fields):
            self.documents.sort(
                key=lambda item: item.get(field),
                reverse=direction < 0,
            )
        return self

    def limit(self, limit):
        self.documents = self.documents[:limit]
        return self

    async def to_list(self, *, length):
        return deepcopy(self.documents[:length])


def _matches(document: dict, filters: dict) -> bool:
    for field, expected in filters.items():
        if field == '$or':
            if not any(_matches(document, item) for item in expected):
                return False
            continue
        actual = document.get(field)
        if isinstance(expected, dict):
            if '$in' in expected and actual not in expected['$in']:
                return False
            if '$lte' in expected and not (actual is not None and actual <= expected['$lte']):
                return False
            if '$gt' in expected and not (actual is not None and actual > expected['$gt']):
                return False
            continue
        if actual != expected:
            return False
    return True


class HealthProfileJobQueueTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.subjects = FakeMongoCollection()
        self.facts = FakeMongoCollection()
        self.events = FakeMongoCollection()
        self.views = FakeMongoCollection()
        self.jobs = FakeMongoCollection()
        self.repository = MongoHealthProfileRepository(
            subjects=self.subjects,
            facts=self.facts,
            events=self.events,
            views=self.views,
            jobs=self.jobs,
        )
        self.clock = MutableClock(
            datetime(2026, 8, 18, 8, 30, tzinfo=timezone.utc),
        )
        self.id_sequence = 0

        def next_id(prefix: str) -> str:
            self.id_sequence += 1
            return f'{prefix}-{self.id_sequence}'

        self.queue = HealthProfileJobQueue(
            self.repository,
            clock=self.clock,
            id_factory=next_id,
            lease_duration=timedelta(minutes=2),
            retry_base_delay=timedelta(seconds=30),
            max_attempts=2,
        )
        self.maintenance = HealthProfileJobMaintenanceService(
            self.repository,
            clock=self.clock,
        )

    async def enqueue(self, **changes):
        values = {
            'source_type': ProfileIngestionSourceType.CHAT,
            'source_id': 'session-1',
            'source_revision': 1,
            'user_id': 'user-1',
            'subject_id': 'subject-self',
        }
        values.update(changes)
        return await self.queue.enqueue(**values)

    async def test_enqueue_is_idempotent_and_scoped_by_user(self) -> None:
        first = await self.enqueue(idempotency_key='same-request')
        repeated = await self.enqueue(idempotency_key='same-request')
        another_user = await self.enqueue(
            user_id='user-2',
            idempotency_key='same-request',
        )

        self.assertTrue(first.created)
        self.assertFalse(repeated.created)
        self.assertEqual(first.job.job_id, repeated.job.job_id)
        self.assertTrue(another_user.created)
        self.assertEqual(len(self.jobs.documents), 2)

    async def test_reusing_key_for_different_payload_is_a_conflict(self) -> None:
        await self.enqueue(idempotency_key='same-request')

        with self.assertRaises(HealthProfileJobConflictError):
            await self.enqueue(
                source_revision=2,
                idempotency_key='same-request',
            )

    async def test_claim_and_complete_record_runtime_versions(self) -> None:
        await self.enqueue()
        claimed = await self.queue.claim('worker-1')

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.status, ProfileJobStatus.RUNNING)
        self.assertEqual(claimed.attempts, 1)
        completed = await self.queue.complete(
            claimed,
            worker_id='worker-1',
            lease_token=claimed.lease_token,
            graph_version='profile-graph-v1',
            policy_version='health-fact-policy-v1',
            extractor_version='profile-extractor-v1',
            processing_summary=ProfileProcessingSummary(
                source_item_count=3,
                candidate_count=2,
                fact_count=2,
                replayed_fact_count=0,
                rejected_candidate_count=0,
                skipped_item_count=1,
                reason_codes=('report_time_unavailable',),
            ),
        )

        self.assertEqual(completed.status, ProfileJobStatus.COMPLETED)
        self.assertEqual(completed.graph_version, 'profile-graph-v1')
        self.assertEqual(completed.policy_version, 'health-fact-policy-v1')
        self.assertEqual(completed.extractor_version, 'profile-extractor-v1')
        self.assertEqual(completed.processing_summary.source_item_count, 3)
        self.assertEqual(completed.processing_summary.skipped_item_count, 1)
        self.assertEqual(completed.processing_summary.reason_codes, ('report_time_unavailable',))
        self.assertIsNotNone(completed.completed_at)
        self.assertIsNone(completed.lease_token)

    async def test_worker_can_claim_only_registered_source_types(self) -> None:
        await self.enqueue(
            source_type=ProfileIngestionSourceType.RECORD,
            source_id='record-session-1',
        )
        await self.enqueue(
            source_type=ProfileIngestionSourceType.HEALTH_REPORT,
            source_id='report-1',
        )

        report = await self.queue.claim(
            'report-worker',
            source_types=(ProfileIngestionSourceType.HEALTH_REPORT,),
        )

        self.assertEqual(
            report.source_type,
            ProfileIngestionSourceType.HEALTH_REPORT,
        )
        self.assertIsNone(
            await self.queue.claim(
                'report-worker',
                source_types=(ProfileIngestionSourceType.HEALTH_REPORT,),
            )
        )
        record = await self.queue.claim(
            'record-worker',
            source_types=(ProfileIngestionSourceType.RECORD,),
        )
        self.assertEqual(record.source_type, ProfileIngestionSourceType.RECORD)

    async def test_retry_delay_and_dead_letter_are_persistent(self) -> None:
        await self.enqueue()
        first = await self.queue.claim('worker-1')
        retrying = await self.queue.fail(
            first,
            worker_id='worker-1',
            lease_token=first.lease_token,
            error_code='extractor.timeout',
        )

        self.assertEqual(retrying.status, ProfileJobStatus.RETRY_WAIT)
        self.assertEqual(
            retrying.next_retry_at,
            self.clock.value + timedelta(seconds=30),
        )
        self.assertIsNone(await self.queue.claim('worker-1'))

        self.clock.advance(timedelta(seconds=30))
        second = await self.queue.claim('worker-2')
        self.assertEqual(second.attempts, 2)
        dead = await self.queue.fail(
            second,
            worker_id='worker-2',
            lease_token=second.lease_token,
            error_code='extractor.invalid_output',
        )

        self.assertEqual(dead.status, ProfileJobStatus.DEAD_LETTER)
        self.assertIsNone(dead.next_retry_at)
        self.assertEqual(dead.error_code, 'extractor.invalid_output')

    async def test_expired_or_foreign_lease_cannot_complete(self) -> None:
        await self.enqueue()
        claimed = await self.queue.claim('worker-1')

        with self.assertRaises(HealthProfileJobLeaseError):
            await self.queue.complete(
                claimed,
                worker_id='worker-1',
                lease_token='foreign-token',
                graph_version='graph-v1',
                policy_version='policy-v1',
                extractor_version='extractor-v1',
            )

        self.clock.advance(timedelta(minutes=2))
        reclaimed = await self.queue.claim('worker-2')
        self.assertEqual(reclaimed.status, ProfileJobStatus.RUNNING)
        self.assertEqual(reclaimed.attempts, 2)
        self.assertNotEqual(reclaimed.lease_token, claimed.lease_token)
        with self.assertRaises(HealthProfileJobLeaseError):
            await self.queue.complete(
                claimed,
                worker_id='worker-1',
                lease_token=claimed.lease_token,
                graph_version='graph-v1',
                policy_version='policy-v1',
                extractor_version='extractor-v1',
            )

    async def test_completed_job_can_be_redriven_without_new_identity(self) -> None:
        enqueued = await self.enqueue()
        claimed = await self.queue.claim('worker-1')
        completed = await self.queue.complete(
            claimed,
            worker_id='worker-1',
            lease_token=claimed.lease_token,
            graph_version='graph-v1',
            policy_version='policy-v1',
            extractor_version='extractor-v1',
            processing_summary=ProfileProcessingSummary(
                candidate_count=0,
                fact_count=0,
                replayed_fact_count=0,
                rejected_candidate_count=0,
                reason_codes=('unsupported_report_type',),
            ),
        )
        self.clock.advance(timedelta(minutes=1))

        redriven = await self.maintenance.redrive(
            job_id=completed.job_id,
            expected_status=ProfileJobStatus.COMPLETED,
            reason_code='deploy.profile-extractor-v2',
            actor_id='operator:test',
        )

        self.assertEqual(redriven.job_id, enqueued.job.job_id)
        self.assertEqual(
            redriven.idempotency_key,
            enqueued.job.idempotency_key,
        )
        self.assertEqual(redriven.status, ProfileJobStatus.PENDING)
        self.assertEqual(redriven.attempts, 0)
        self.assertIsNone(redriven.completed_at)
        self.assertIsNone(redriven.processing_summary)
        self.assertIsNone(redriven.graph_version)
        self.assertEqual(len(redriven.redrive_history), 1)
        audit = redriven.redrive_history[0]
        self.assertEqual(audit.from_status, ProfileJobStatus.COMPLETED)
        self.assertEqual(audit.attempts, 1)
        self.assertEqual(audit.graph_version, 'graph-v1')
        self.assertEqual(audit.actor_id, 'operator:test')
        self.assertEqual(audit.processing_summary.reason_codes, ('unsupported_report_type',))

        replay = await self.queue.claim('worker-2')
        self.assertEqual(replay.job_id, completed.job_id)
        self.assertEqual(replay.attempts, 1)

    async def test_dead_letter_redrive_preserves_failure_audit(self) -> None:
        await self.enqueue()
        first = await self.queue.claim('worker-1')
        await self.queue.fail(
            first,
            worker_id='worker-1',
            lease_token=first.lease_token,
            error_code='extractor.timeout',
        )
        self.clock.advance(timedelta(seconds=30))
        second = await self.queue.claim('worker-2')
        dead = await self.queue.fail(
            second,
            worker_id='worker-2',
            lease_token=second.lease_token,
            error_code='extractor.invalid_output',
        )

        redriven = await self.maintenance.redrive(
            job_id=dead.job_id,
            expected_status=ProfileJobStatus.DEAD_LETTER,
            reason_code='fix.extractor-json',
            actor_id='operator:test',
        )

        self.assertEqual(redriven.status, ProfileJobStatus.PENDING)
        self.assertIsNone(redriven.error_code)
        self.assertEqual(
            redriven.redrive_history[0].error_code,
            'extractor.invalid_output',
        )

    async def test_redrive_requires_exact_terminal_status(self) -> None:
        pending = await self.enqueue()

        with self.assertRaises(ValueError):
            await self.maintenance.redrive(
                job_id=pending.job.job_id,
                expected_status=ProfileJobStatus.PENDING,
                reason_code='invalid-status',
                actor_id='operator:test',
            )
        with self.assertRaises(HealthProfileJobRedriveConflictError):
            await self.maintenance.redrive(
                job_id=pending.job.job_id,
                expected_status=ProfileJobStatus.COMPLETED,
                reason_code='stale-status',
                actor_id='operator:test',
            )
        with self.assertRaises(HealthProfileJobNotFoundError):
            await self.maintenance.redrive(
                job_id='missing-job',
                expected_status=ProfileJobStatus.COMPLETED,
                reason_code='missing-job',
                actor_id='operator:test',
            )

    async def test_recoverable_job_query_is_filtered_and_bounded(self) -> None:
        first = await self.enqueue(source_id='session-1')
        second = await self.enqueue(
            source_type=ProfileIngestionSourceType.RECORD,
            source_id='record-1',
        )
        for enqueued in (first, second):
            claimed = await self.queue.claim(
                'worker-1',
                source_types=(enqueued.job.source_type,),
            )
            await self.queue.complete(
                claimed,
                worker_id='worker-1',
                lease_token=claimed.lease_token,
                graph_version='graph-v1',
                policy_version='policy-v1',
                extractor_version='extractor-v1',
            )
            self.clock.advance(timedelta(seconds=1))

        records = await self.maintenance.list_recoverable_jobs(
            statuses=(ProfileJobStatus.COMPLETED,),
            source_types=(ProfileIngestionSourceType.RECORD,),
            limit=1,
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0].source_type,
            ProfileIngestionSourceType.RECORD,
        )


if __name__ == '__main__':
    unittest.main()
