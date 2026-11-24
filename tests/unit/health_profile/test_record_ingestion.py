"""建档问答候选映射与画像摄取处理器测试。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

from agents import AgentHub
from agents import AgentRuntime
from agents import GraphRegistry
from agents.graphs.health_profile import register_profile_record_extraction
from agents.llm import LLMResult
from agents.llm import ModelProfileRegistry
from tests.unit._support.agents import FakeLLMGateway
from health_profile import AssertionLevel
from health_profile import EffectivePeriod
from health_profile import FactSource
from health_profile import FactStatus
from health_profile import FactValueType
from health_profile import HealthProfileJob
from health_profile import HealthProfileView
from health_profile import HealthSubject
from health_profile import MemoryCandidate
from health_profile import ProfileIngestionSourceType
from health_profile import ProfileJobStatus
from health_profile import ProfileIngestionProcessingError
from health_profile import RecordAnswerCandidateExtractor
from health_profile import RecordBasicInfoMapper
from health_profile import RecordProfileProcessor
from health_profile import RecordProfileSourceRecord
from health_profile import RecordSubjectResolver
from health_profile import SubjectKind
from health_profile import SubjectRelationship
from health_profile import VerificationStatus
from langgraph.checkpoint.memory import InMemorySaver

from tests.unit.health_profile.test_report_ingestion import FakeProfileRepository
from tests.unit.health_profile.test_report_ingestion import ReportIngestionFixture


class FakeRecordStore:
    def __init__(self, record=None) -> None:
        self.record = record

    async def get_completed(self, *, user_id, session_id):
        if (
                self.record is not None
                and self.record.user_id == user_id
                and self.record.session_id == session_id
        ):
            return self.record
        return None

    async def mark_ingestion_queued(self, **kwargs):
        raise AssertionError('处理器不修改来源投递状态')


class FakeRecordProfileService:
    def __init__(self, subject, *, repository=None) -> None:
        self.subject = subject
        self.repository = repository
        self.calls = []
        self.subject_calls = []

    async def ensure_self_profile(self, user_id, *, display_name=None):
        self.calls.append((user_id, display_name))
        return SimpleNamespace(subject=self.subject)

    async def upsert_subject(
            self,
            user_id,
            request_id,
            *,
            subject_id=None,
            kind=None,
            display_name=None,
            relationship=None,
            expected_subject_version=None,
    ):
        if self.repository is None:
            raise AssertionError('测试未配置主体 Repository')
        self.subject_calls.append({
            'userId': user_id,
            'requestId': request_id,
            'kind': kind,
            'displayName': display_name,
            'relationship': relationship,
        })
        relation_type = relationship['relationType']
        candidate = HealthSubject(
            subject_id=f'subject-{relation_type}',
            user_id=user_id,
            kind=kind,
            display_name=display_name,
            relationship=SubjectRelationship.model_validate(relationship),
            created_at=RecordFixture.now,
            updated_at=RecordFixture.now,
            creation_request_id=request_id,
            creation_request_fingerprint=request_id,
        )
        stored, created = await self.repository.create_subject(candidate)
        await self.repository.ensure_initial_view(HealthProfileView(
            user_id=user_id,
            subject_id=stored.subject_id,
            subject_summary={'displayName': stored.display_name},
            updated_at=stored.updated_at,
        ))
        return SimpleNamespace(subject=stored, created=created)


class FakeAnswerExtractor:
    VERSION = 'fake-record-extractor-v1'

    def __init__(self, candidates=()) -> None:
        self.candidates = tuple(candidates)
        self.calls = []

    async def extract(self, record, *, subject_id):
        self.calls.append((record, subject_id))
        return self.candidates


class FailingAnswerExtractor:
    VERSION = 'failing-record-extractor-v1'

    async def extract(self, record, *, subject_id):
        raise ProfileIngestionProcessingError(
            'extractor.agent_failure',
            '测试提取失败',
        )


class RecordFixture:
    now = datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc)

    @classmethod
    def record(cls, **session_changes):
        session = {
            'userId': 'user-1',
            'sessionId': 'session-1',
            'type': 'qa',
            'status': 'completed',
            'round': 1,
            'completedAt': '2026-08-18T18:00:00+08:00',
            'extractedInfo': {
                'name': '测试用户',
                'gender': '女',
                'age': 42,
                'height': 165,
                'weight': None,
            },
            'extractedInfoSources': {
                'name': 'user_input',
                'gender': 'user_input',
                'age': 'user_input',
                'height': 'user_input',
            },
            'qaHistory': [
                {
                    'question': {
                        'questionId': 'q_symptom',
                        'questionType': 'radio',
                        'question': '您最近是否有身体不适？',
                        'options': ['没有', '头痛'],
                        'reasoning': '不得发送给提取模型',
                    },
                    'answer': ['头痛'],
                    'answeredAt': '2026-08-18T17:58:00+08:00',
                },
                {
                    'question': {
                        'questionId': 'q_family',
                        'questionType': 'text',
                        'question': '您的家族中是否有慢性疾病？',
                        'options': {},
                    },
                    'answer': '父亲患有糖尿病',
                    'answeredAt': '2026-08-18T17:59:00+08:00',
                },
            ],
        }
        session.update(session_changes)
        return RecordProfileSourceRecord(
            user_id='user-1',
            session_id='session-1',
            round_number=1,
            source_revision=1,
            ingestion_status='queued',
            ingestion_job_id='job-record-1',
            session=session,
        )

    @classmethod
    def job(cls):
        return HealthProfileJob(
            job_id='job-record-1',
            idempotency_key='user-1:record:session-1:1:1',
            idempotency_fingerprint='fingerprint-record-1',
            source_type=ProfileIngestionSourceType.RECORD,
            source_id='session-1',
            source_revision=1,
            user_id='user-1',
            status=ProfileJobStatus.RUNNING,
            attempts=1,
            lease_owner='worker-1',
            lease_token='lease-1',
            lease_expires_at=cls.now + timedelta(minutes=5),
            created_at=cls.now,
            updated_at=cls.now,
        )


def _runtime(output):
    gateway = FakeLLMGateway(complete_result=LLMResult(
        content=json.dumps(output, ensure_ascii=False),
        reasoning_content='不得进入候选或来源',
    ))
    registry = GraphRegistry()
    runtime = AgentRuntime(
        hub=AgentHub(registry),
        registry=registry,
        profiles=ModelProfileRegistry(),
        llm_gateway=gateway,
        context_repository=object(),
        lease_manager=object(),
        checkpointer=InMemorySaver(),
        _close_callback=lambda: None,
    )
    register_profile_record_extraction(runtime)
    return runtime, gateway


class RecordBasicInfoMapperTest(unittest.TestCase):
    def test_maps_only_explicit_user_input_with_standard_values(self):
        record = RecordFixture.record()

        candidates = RecordBasicInfoMapper().map(
            record,
            subject_id='subject-self',
        )

        self.assertEqual(
            [item.fact_type for item in candidates],
            [
                'demographic.sex',
                'demographic.age',
                'anthropometric.height',
            ],
        )
        self.assertEqual(candidates[0].value, 'female')
        self.assertEqual(
            candidates[1].value,
            {'number': 42.0, 'unit': 'years'},
        )
        self.assertEqual(
            candidates[2].value,
            {'number': 165.0, 'unit': 'cm'},
        )
        self.assertTrue(all(
            item.assertion_level is AssertionLevel.USER_CONFIRMED
            for item in candidates
        ))
        self.assertTrue(all(
            item.source.source_type == 'record'
            for item in candidates
        ))

    def test_legacy_or_report_derived_values_are_not_upgraded(self):
        legacy = RecordFixture.record(extractedInfoSources={})
        report_value = RecordFixture.record(extractedInfoSources={
            'gender': 'health_report',
            'height': 'health_report',
        })

        self.assertEqual(
            RecordBasicInfoMapper().map(
                legacy,
                subject_id='subject-self',
            ),
            (),
        )
        self.assertEqual(
            RecordBasicInfoMapper().map(
                report_value,
                subject_id='subject-self',
            ),
            (),
        )


class RecordAnswerCandidateExtractorTest(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_attached_health_data_but_excludes_products_and_reports(self):
        runtime, gateway = _runtime({'candidates': [{
            'answerIndex': 2,
            'factType': 'condition.diagnosis',
            'displayName': '高血压',
            'valueType': 'text',
            'value': '高血压',
            'subjectKind': 'self',
            'subjectHint': '本人',
            'evidenceText': '高血压',
            'confidence': 0.95,
        }]})
        record = RecordFixture.record(healthData={
            'fileList': [{'url': 'https://example.com/private-report.pdf'}],
            'hospitalData': [{
                'diagnosis': '高血压',
                'recordedAt': '2026-08-17T09:00:00+08:00',
            }],
            'weRunData': [{'date': '2026-08-17', 'steps': 8200}],
            'smartDeviceData': {'restingHeartRate': 72},
            'doctorProductPackages': [{
                'packageName': '不应进入画像提取的医生产品',
            }],
        })

        candidates = await RecordAnswerCandidateExtractor(
            runtime.hub,
        ).extract(record, subject_id='subject-self')

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.subject_id, 'subject-self')
        self.assertEqual(candidate.fact_type, 'condition.diagnosis')
        self.assertEqual(
            candidate.source.pointer,
            '/healthData/hospitalData/0',
        )
        self.assertEqual(
            candidate.effective_period.start,
            datetime(2026, 8, 17, 9, 0, tzinfo=timezone(timedelta(hours=8))),
        )
        self.assertIs(candidate.assertion_level, AssertionLevel.USER_REPORTED)

        sent = json.loads(gateway.complete_requests[0].content)
        attached = {
            item['sourceKind']: item
            for item in sent['answers']
            if item['sourceKind'] != 'qa_answer'
        }
        self.assertEqual(attached['hospital_data']['answer']['diagnosis'], '高血压')
        self.assertEqual(attached['we_run_data']['answer']['steps'], 8200)
        self.assertEqual(attached['smart_device_data']['answer'], 72)
        serialized = json.dumps(sent, ensure_ascii=False)
        self.assertNotIn('不应进入画像提取的医生产品', serialized)
        self.assertNotIn('private-report.pdf', serialized)

    async def test_attached_health_data_cannot_create_relative_candidate(self):
        runtime, _ = _runtime({'candidates': [{
            'answerIndex': 2,
            'factType': 'family_history.condition',
            'displayName': '糖尿病家族史',
            'valueType': 'text',
            'value': '糖尿病',
            'subjectKind': 'blood_relative',
            'subjectHint': '父亲',
            'evidenceText': '父亲患有糖尿病',
            'confidence': 1,
        }]})
        record = RecordFixture.record(healthData={
            'hospitalData': [{'note': '父亲患有糖尿病'}],
            'weRunData': [],
            'smartDeviceData': {},
            'doctorProductPackages': [],
            'fileList': [],
        })

        candidates = await RecordAnswerCandidateExtractor(
            runtime.hub,
        ).extract(record, subject_id='subject-self')

        self.assertEqual(candidates, ())

    async def test_extracts_self_candidate_and_defers_family_subject(self):
        runtime, gateway = _runtime({'candidates': [
            {
                'answerIndex': 0,
                'factType': 'symptom.reported',
                'displayName': '头痛',
                'valueType': 'text',
                'value': '头痛',
                'subjectKind': 'self',
                'subjectHint': '本人',
                'evidenceText': '头痛',
                'confidence': 0.95,
            },
            {
                'answerIndex': 1,
                'factType': 'family_history.condition',
                'displayName': '糖尿病家族史',
                'valueType': 'text',
                'value': '糖尿病',
                'subjectKind': 'blood_relative',
                'subjectHint': '父亲',
                'evidenceText': '父亲患有糖尿病',
                'confidence': 0.99,
            },
        ]})

        candidates = await RecordAnswerCandidateExtractor(
            runtime.hub,
        ).extract(
            RecordFixture.record(),
            subject_id='subject-self',
        )

        self.assertEqual(len(candidates), 2)
        candidate = candidates[0]
        self.assertEqual(candidate.fact_type, 'symptom.reported')
        self.assertEqual(candidate.subject_id, 'subject-self')
        self.assertIs(candidate.subject_kind_hint, SubjectKind.SELF)
        self.assertEqual(candidate.evidence_text, '头痛')
        self.assertIs(candidate.assertion_level, AssertionLevel.USER_REPORTED)
        self.assertEqual(candidate.confidence, 0.8)
        self.assertEqual(candidate.source.pointer, '/qaHistory/0/answer')
        sent = json.loads(gateway.complete_requests[0].content)
        self.assertNotIn('reasoning', sent['answers'][0])
        self.assertIn('symptom.reported', sent['allowedFactTypes'])
        family = candidates[1]
        self.assertIsNone(family.subject_id)
        self.assertEqual(family.subject_hint, '父亲')
        self.assertIs(
            family.subject_kind_hint,
            SubjectKind.BLOOD_RELATIVE,
        )

    async def test_other_subject_question_cannot_be_forced_into_self(self):
        runtime, _ = _runtime({'candidates': [{
            'answerIndex': 1,
            'factType': 'condition.diagnosis',
            'displayName': '糖尿病',
            'valueType': 'text',
            'value': '糖尿病',
            'subjectKind': 'self',
            'subjectHint': '本人',
            'evidenceText': '父亲患有糖尿病',
            'confidence': 1,
        }]})

        candidates = await RecordAnswerCandidateExtractor(
            runtime.hub,
        ).extract(
            RecordFixture.record(),
            subject_id='subject-self',
        )

        self.assertEqual(candidates, ())

    async def test_other_subject_answer_cannot_be_forced_into_self(self):
        runtime, _ = _runtime({'candidates': [{
            'answerIndex': 0,
            'factType': 'condition.diagnosis',
            'displayName': '糖尿病',
            'valueType': 'text',
            'value': '糖尿病',
            'subjectKind': 'self',
            'subjectHint': '本人',
            'evidenceText': '我的父亲患有糖尿病',
            'confidence': 1,
        }]})
        record = RecordFixture.record(qaHistory=[{
            'question': {
                'questionId': 'q_open',
                'questionType': 'text',
                'question': '还有什么需要补充？',
            },
            'answer': '我的父亲患有糖尿病',
            'answeredAt': '2026-08-18T17:59:00+08:00',
        }])

        candidates = await RecordAnswerCandidateExtractor(
            runtime.hub,
        ).extract(record, subject_id='subject-self')

        self.assertEqual(candidates, ())

    async def test_mixed_answer_keeps_self_and_multiple_relatives(self):
        answer = '我身高178厘米，父亲患有高血压，母亲患有糖尿病'
        runtime, _ = _runtime({'candidates': [
            {
                'answerIndex': 0,
                'factType': 'anthropometric.height',
                'displayName': '身高',
                'valueType': 'quantity',
                'value': {'number': 178, 'unit': 'cm'},
                'subjectKind': 'self',
                'subjectHint': '本人',
                'evidenceText': '我身高178厘米',
                'confidence': 0.99,
            },
            {
                'answerIndex': 0,
                'factType': 'family_history.condition',
                'displayName': '高血压家族史',
                'valueType': 'text',
                'value': '高血压',
                'subjectKind': 'blood_relative',
                'subjectHint': '父亲',
                'evidenceText': '父亲患有高血压',
                'confidence': 0.99,
            },
            {
                'answerIndex': 0,
                'factType': 'family_history.condition',
                'displayName': '糖尿病家族史',
                'valueType': 'text',
                'value': '糖尿病',
                'subjectKind': 'blood_relative',
                'subjectHint': '母亲',
                'evidenceText': '母亲患有糖尿病',
                'confidence': 0.99,
            },
        ]})
        record = RecordFixture.record(qaHistory=[{
            'question': {
                'questionId': 'q_open',
                'questionType': 'text',
                'question': '还有什么需要补充？',
            },
            'answer': answer,
            'answeredAt': '2026-08-18T17:59:00+08:00',
        }])

        candidates = await RecordAnswerCandidateExtractor(
            runtime.hub,
        ).extract(record, subject_id='subject-self')

        self.assertEqual(len(candidates), 3)
        self.assertEqual(len({item.candidate_id for item in candidates}), 3)
        self.assertEqual(
            [item.subject_id for item in candidates],
            ['subject-self', None, None],
        )
        self.assertEqual(
            [item.subject_hint for item in candidates[1:]],
            ['父亲', '母亲'],
        )

    async def test_same_answer_keeps_multiple_values_with_stable_ids(self):
        answer = '我最近既有头痛，也有头晕'
        extracted = [
            {
                'answerIndex': 0,
                'factType': 'symptom.reported',
                'displayName': '头痛',
                'valueType': 'text',
                'value': '头痛',
                'subjectKind': 'self',
                'subjectHint': '本人',
                'evidenceText': '头痛',
                'confidence': 0.99,
            },
            {
                'answerIndex': 0,
                'factType': 'symptom.reported',
                'displayName': '头晕',
                'valueType': 'text',
                'value': '头晕',
                'subjectKind': 'self',
                'subjectHint': '本人',
                'evidenceText': '头晕',
                'confidence': 0.99,
            },
        ]
        record = RecordFixture.record(qaHistory=[{
            'question': {
                'questionId': 'q_open',
                'questionType': 'text',
                'question': '最近有什么不适？',
            },
            'answer': answer,
            'answeredAt': '2026-08-18T17:59:00+08:00',
        }])
        runtime, _ = _runtime({'candidates': extracted})
        reversed_runtime, _ = _runtime({
            'candidates': list(reversed(extracted)),
        })

        candidates = await RecordAnswerCandidateExtractor(
            runtime.hub,
        ).extract(record, subject_id='subject-self')
        reversed_candidates = await RecordAnswerCandidateExtractor(
            reversed_runtime.hub,
        ).extract(record, subject_id='subject-self')

        self.assertEqual(len(candidates), 2)
        self.assertEqual(len({item.candidate_id for item in candidates}), 2)
        self.assertEqual(
            {item.value: item.candidate_id for item in candidates},
            {
                item.value: item.candidate_id
                for item in reversed_candidates
            },
        )

    async def test_non_verbatim_answer_evidence_is_rejected(self):
        runtime, _ = _runtime({'candidates': [{
            'answerIndex': 0,
            'factType': 'symptom.reported',
            'displayName': '头痛',
            'valueType': 'text',
            'value': '头痛',
            'subjectKind': 'self',
            'subjectHint': '本人',
            'evidenceText': '用户明确表示头痛',
            'confidence': 0.99,
        }]})

        candidates = await RecordAnswerCandidateExtractor(
            runtime.hub,
        ).extract(RecordFixture.record(), subject_id='subject-self')

        self.assertEqual(candidates, ())


class RecordSubjectResolverTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.self_subject = ReportIngestionFixture.subject()
        self.repository = FakeProfileRepository(self.self_subject)
        self.profile_service = FakeRecordProfileService(
            self.self_subject,
            repository=self.repository,
        )
        self.resolver = RecordSubjectResolver(
            self.repository,
            self.profile_service,
        )

    @staticmethod
    def family_candidate(*, candidate_id, subject_hint):
        return MemoryCandidate(
            candidate_id=candidate_id,
            subject_id=None,
            subject_hint=subject_hint,
            subject_kind_hint=SubjectKind.BLOOD_RELATIVE,
            fact_type='family_history.condition',
            display_name='糖尿病家族史',
            value_type=FactValueType.TEXT,
            value='糖尿病',
            assertion_level=AssertionLevel.USER_REPORTED,
            confidence=0.8,
            effective_period=EffectivePeriod(start=RecordFixture.now),
            source=FactSource(
                source_type='record',
                source_id=candidate_id,
                source_revision=1,
                pointer='/qaHistory/0/answer',
                observed_at=RecordFixture.now,
                excerpt_hash=f'hash-{candidate_id}',
            ),
        )

    async def test_reuses_stable_father_subject_across_sessions(self):
        first = await self.resolver.resolve(
            self.family_candidate(
                candidate_id='session-1-father',
                subject_hint='爸爸',
            ),
            user_id='user-1',
            self_subject=self.self_subject,
        )
        second = await self.resolver.resolve(
            self.family_candidate(
                candidate_id='session-2-father',
                subject_hint='父亲',
            ),
            user_id='user-1',
            self_subject=self.self_subject,
        )

        self.assertEqual(first.reason_code, 'stable_relation_created')
        self.assertEqual(second.reason_code, 'stable_relation_reused')
        self.assertEqual(
            first.subject.subject_id,
            second.subject.subject_id,
        )
        self.assertEqual(len(self.profile_service.subject_calls), 1)
        self.assertEqual(len(self.repository.additional_subjects), 1)
        self.assertEqual(
            first.subject.relationship.relation_type,
            'father',
        )

    async def test_ambiguous_sibling_relation_requires_confirmation(self):
        resolution = await self.resolver.resolve(
            self.family_candidate(
                candidate_id='session-1-brother',
                subject_hint='哥哥',
            ),
            user_id='user-1',
            self_subject=self.self_subject,
        )

        self.assertIsNone(resolution.subject)
        self.assertIsNone(resolution.candidate.subject_id)
        self.assertEqual(
            resolution.reason_code,
            'subject_requires_confirmation',
        )
        self.assertEqual(self.profile_service.subject_calls, [])


class RecordProfileProcessorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.record = RecordFixture.record()
        self.subject = ReportIngestionFixture.subject()
        self.repository = FakeProfileRepository(self.subject)
        symptom = MemoryCandidate(
            candidate_id='record-answer-symptom-1',
            subject_id=self.subject.subject_id,
            subject_hint='本人',
            fact_type='symptom.reported',
            display_name='头痛',
            value_type=FactValueType.TEXT,
            value='头痛',
            assertion_level=AssertionLevel.USER_REPORTED,
            confidence=0.8,
            effective_period=EffectivePeriod(start=RecordFixture.now),
            source=FactSource(
                source_type='record',
                source_id='session-1',
                source_revision=1,
                pointer='/qaHistory/0/answer',
                observed_at=RecordFixture.now,
                excerpt_hash='hash-1',
            ),
        )
        self.extractor = FakeAnswerExtractor((symptom,))
        self.profile_service = FakeRecordProfileService(
            self.subject,
            repository=self.repository,
        )
        self.id_sequence = 0

        def next_id(prefix):
            self.id_sequence += 1
            return f'{prefix}-{self.id_sequence}'

        self.processor = RecordProfileProcessor(
            store=FakeRecordStore(self.record),
            repository=self.repository,
            profile_service=self.profile_service,
            agent_hub=object(),
            answer_extractor=self.extractor,
            clock=lambda: RecordFixture.now,
            id_factory=next_id,
        )

    async def test_writes_active_basics_and_pending_agent_candidate(self):
        first = await self.processor.process(RecordFixture.job())
        replay = await self.processor.process(RecordFixture.job())

        self.assertEqual(first.candidate_count, 4)
        self.assertEqual(first.fact_count, 4)
        self.assertEqual(replay.fact_count, 0)
        self.assertEqual(replay.replayed_fact_count, 4)
        self.assertEqual(len(self.repository.facts), 4)
        by_type = {fact.fact_type: fact for fact in self.repository.facts}
        self.assertEqual(
            by_type['demographic.sex'].status,
            FactStatus.ACTIVE,
        )
        self.assertEqual(
            by_type['demographic.sex'].verification_status,
            VerificationStatus.CONFIRMED,
        )
        self.assertEqual(
            by_type['symptom.reported'].status,
            FactStatus.PENDING,
        )
        self.assertEqual(
            by_type['symptom.reported'].verification_status,
            VerificationStatus.UNCONFIRMED,
        )
        self.assertEqual(
            self.repository.view.category_counts,
            {'demographic': 2, 'anthropometric': 1},
        )
        self.assertEqual(self.repository.view.pending_confirmation_count, 1)
        self.assertEqual(
            self.profile_service.calls,
            [('user-1', '测试用户'), ('user-1', '测试用户')],
        )

    async def test_basic_facts_survive_extractor_failure_and_replay(self):
        failure_id_sequence = 0

        def failure_id(prefix):
            nonlocal failure_id_sequence
            failure_id_sequence += 1
            return f'{prefix}-failure-{failure_id_sequence}'

        failing = RecordProfileProcessor(
            store=FakeRecordStore(self.record),
            repository=self.repository,
            profile_service=self.profile_service,
            agent_hub=object(),
            answer_extractor=FailingAnswerExtractor(),
            clock=lambda: RecordFixture.now,
            id_factory=failure_id,
        )

        with self.assertRaises(ProfileIngestionProcessingError):
            await failing.process(RecordFixture.job())

        self.assertEqual(
            [fact.fact_type for fact in self.repository.facts],
            [
                'demographic.sex',
                'demographic.age',
                'anthropometric.height',
            ],
        )
        self.assertTrue(all(
            fact.status is FactStatus.ACTIVE
            for fact in self.repository.facts
        ))

        retry = RecordProfileProcessor(
            store=FakeRecordStore(self.record),
            repository=self.repository,
            profile_service=self.profile_service,
            agent_hub=object(),
            answer_extractor=FakeAnswerExtractor(),
            clock=lambda: RecordFixture.now,
            id_factory=lambda prefix: f'{prefix}-retry-test',
        )
        replay = await retry.process(RecordFixture.job())

        self.assertEqual(replay.candidate_count, 3)
        self.assertEqual(replay.fact_count, 0)
        self.assertEqual(replay.replayed_fact_count, 3)
        self.assertEqual(len(self.repository.facts), 3)

    async def test_writes_family_history_to_stable_relative_subject(self):
        family = MemoryCandidate(
            candidate_id='record-answer-family-1',
            subject_id=None,
            subject_hint='父亲',
            subject_kind_hint=SubjectKind.BLOOD_RELATIVE,
            fact_type='family_history.condition',
            display_name='糖尿病家族史',
            value_type=FactValueType.TEXT,
            value='糖尿病',
            assertion_level=AssertionLevel.USER_REPORTED,
            confidence=0.8,
            effective_period=EffectivePeriod(start=RecordFixture.now),
            source=FactSource(
                source_type='record',
                source_id='session-1',
                source_revision=1,
                pointer='/qaHistory/1/answer',
                observed_at=RecordFixture.now,
                excerpt_hash='hash-family-1',
            ),
        )
        id_sequence = 0

        def next_id(prefix):
            nonlocal id_sequence
            id_sequence += 1
            return f'{prefix}-family-{id_sequence}'

        processor = RecordProfileProcessor(
            store=FakeRecordStore(self.record),
            repository=self.repository,
            profile_service=self.profile_service,
            agent_hub=object(),
            answer_extractor=FakeAnswerExtractor((family,)),
            clock=lambda: RecordFixture.now,
            id_factory=next_id,
        )

        first = await processor.process(RecordFixture.job())
        replay = await processor.process(RecordFixture.job())

        family_facts = [
            fact for fact in self.repository.facts
            if fact.fact_type == 'family_history.condition'
        ]
        self.assertEqual(first.candidate_count, 4)
        self.assertEqual(first.fact_count, 4)
        self.assertEqual(replay.replayed_fact_count, 4)
        self.assertEqual(len(family_facts), 1)
        self.assertEqual(
            family_facts[0].subject_id,
            'subject-father',
        )
        self.assertEqual(family_facts[0].status, FactStatus.PENDING)
        self.assertEqual(len(self.profile_service.subject_calls), 1)


if __name__ == '__main__':
    unittest.main()
