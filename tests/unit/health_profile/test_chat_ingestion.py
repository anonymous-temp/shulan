"""普通聊天候选提取与画像摄取处理器测试。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import patch

from agents import AgentHub
from agents import AgentRuntime
from agents import GraphRegistry
from agents.graphs.health_profile import register_profile_chat_extraction
from agents.llm import LLMResult
from agents.llm import ModelProfileRegistry
from tests.unit._support.agents import FakeLLMGateway
from health_profile import AssertionLevel
from health_profile import ChatMessageCandidateExtractor
from health_profile import ChatProfileProcessor
from health_profile import ChatProfileSourceRecord
from health_profile import EffectivePeriod
from health_profile import FactSource
from health_profile import FactStatus
from health_profile import FactValueType
from health_profile import HealthProfileJob
from health_profile import MemoryCandidate
from health_profile import ProfileIngestionSourceType
from health_profile import ProfileJobStatus
from health_profile import SubjectKind
from langgraph.checkpoint.memory import InMemorySaver

from tests.unit.health_profile.test_record_ingestion import (
    FakeRecordProfileService,
)
from tests.unit.health_profile.test_report_ingestion import FakeProfileRepository
from tests.unit.health_profile.test_report_ingestion import ReportIngestionFixture


class FakeChatStore:
    def __init__(self, record=None) -> None:
        self.record = record

    async def get_user_message(self, *, user_id, message_id):
        if (
                self.record is not None
                and self.record.user_id == user_id
                and self.record.message_id == message_id
        ):
            return self.record
        return None

    async def mark_ingestion_queued(self, **kwargs):
        raise AssertionError('处理器不修改来源投递状态')


class FakeChatExtractor:
    VERSION = 'fake-chat-extractor-v1'

    def __init__(self, candidates=()) -> None:
        self.candidates = tuple(candidates)
        self.calls = []

    async def extract(self, record, *, self_subject_id):
        self.calls.append((record, self_subject_id))
        return self.candidates


class ChatFixture:
    now = datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc)

    @classmethod
    def record(cls, **changes):
        values = {
            'user_id': 'user-1',
            'session_id': 'session-1',
            'message_id': 'message-1',
            'source_revision': 1,
            'ingestion_status': 'queued',
            'ingestion_job_id': 'job-chat-1',
            'content': '我最近经常头痛',
            'created_at': cls.now,
        }
        values.update(changes)
        return ChatProfileSourceRecord(**values)

    @classmethod
    def job(cls):
        return HealthProfileJob(
            job_id='job-chat-1',
            idempotency_key='user-1:chat:session-1:message-1:1',
            idempotency_fingerprint='fingerprint-chat-1',
            source_type=ProfileIngestionSourceType.CHAT,
            source_id='message-1',
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
    register_profile_chat_extraction(runtime)
    return runtime, gateway


class ChatMessageCandidateExtractorTest(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_only_user_message_as_pending_candidate(self):
        runtime, gateway = _runtime({'candidates': [{
            'factType': 'symptom.reported',
            'displayName': '头痛',
            'valueType': 'text',
            'value': '经常头痛',
            'subjectKind': 'self',
            'subjectHint': '本人',
            'evidenceText': '我最近经常头痛',
            'confidence': 0.96,
        }]})

        candidates = await ChatMessageCandidateExtractor(
            runtime.hub,
        ).extract(
            ChatFixture.record(),
            self_subject_id='subject-self',
        )

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.subject_id, 'subject-self')
        self.assertIs(candidate.subject_kind_hint, SubjectKind.SELF)
        self.assertEqual(candidate.evidence_text, '我最近经常头痛')
        self.assertEqual(candidate.confidence, 0.8)
        self.assertIs(candidate.assertion_level, AssertionLevel.USER_REPORTED)
        self.assertEqual(candidate.source.source_type, 'chat')
        self.assertEqual(candidate.source.source_id, 'message-1')
        sent = json.loads(gateway.complete_requests[0].content)
        self.assertEqual(sent['message']['content'], '我最近经常头痛')
        self.assertNotIn('reasoning', sent['message'])

    async def test_family_message_cannot_be_forced_into_self(self):
        runtime, _ = _runtime({'candidates': [{
            'factType': 'condition.diagnosis',
            'displayName': '糖尿病',
            'valueType': 'text',
            'value': '糖尿病',
            'subjectKind': 'self',
            'subjectHint': '本人',
            'evidenceText': '我的父亲患有糖尿病',
            'confidence': 1,
        }]})

        candidates = await ChatMessageCandidateExtractor(
            runtime.hub,
        ).extract(
            ChatFixture.record(content='我的父亲患有糖尿病'),
            self_subject_id='subject-self',
        )

        self.assertEqual(candidates, ())

    async def test_mixed_message_keeps_self_and_family_candidates(self):
        content = '我身高178厘米，体重75公斤；我的父亲患有高血压。'
        runtime, _ = _runtime({'candidates': [
            {
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
                'factType': 'anthropometric.weight',
                'displayName': '体重',
                'valueType': 'quantity',
                'value': {'number': 75, 'unit': 'kg'},
                'subjectKind': 'self',
                'subjectHint': '本人',
                'evidenceText': '体重75公斤',
                'confidence': 0.99,
            },
            {
                'factType': 'family_history.condition',
                'displayName': '高血压家族史',
                'valueType': 'text',
                'value': '高血压',
                'subjectKind': 'blood_relative',
                'subjectHint': '父亲',
                'evidenceText': '我的父亲患有高血压',
                'confidence': 0.99,
            },
        ]})

        candidates = await ChatMessageCandidateExtractor(
            runtime.hub,
        ).extract(
            ChatFixture.record(content=content),
            self_subject_id='subject-self',
        )

        self.assertEqual(len(candidates), 3)
        self.assertEqual(len({item.candidate_id for item in candidates}), 3)
        self.assertEqual(
            [item.subject_id for item in candidates],
            ['subject-self', 'subject-self', None],
        )
        self.assertEqual(candidates[2].subject_hint, '父亲')

    async def test_same_fact_type_is_allowed_for_different_relatives(self):
        content = '我的父亲患有高血压，母亲患有糖尿病'
        runtime, _ = _runtime({'candidates': [
            {
                'factType': 'family_history.condition',
                'displayName': '高血压家族史',
                'valueType': 'text',
                'value': '高血压',
                'subjectKind': 'blood_relative',
                'subjectHint': '父亲',
                'evidenceText': '我的父亲患有高血压',
                'confidence': 0.99,
            },
            {
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

        candidates = await ChatMessageCandidateExtractor(
            runtime.hub,
        ).extract(
            ChatFixture.record(content=content),
            self_subject_id='subject-self',
        )

        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            [item.subject_hint for item in candidates],
            ['父亲', '母亲'],
        )
        self.assertNotEqual(
            candidates[0].candidate_id,
            candidates[1].candidate_id,
        )

    async def test_same_subject_keeps_multiple_distinct_fact_values(self):
        content = '我已经确诊高血压和糖尿病'
        runtime, _ = _runtime({'candidates': [
            {
                'factType': 'condition.diagnosis',
                'displayName': '高血压',
                'valueType': 'text',
                'value': '高血压',
                'subjectKind': 'self',
                'subjectHint': '本人',
                'evidenceText': '确诊高血压',
                'confidence': 0.99,
            },
            {
                'factType': 'condition.diagnosis',
                'displayName': '糖尿病',
                'valueType': 'text',
                'value': '糖尿病',
                'subjectKind': 'self',
                'subjectHint': '本人',
                'evidenceText': '糖尿病',
                'confidence': 0.99,
            },
        ]})

        candidates = await ChatMessageCandidateExtractor(
            runtime.hub,
        ).extract(
            ChatFixture.record(content=content),
            self_subject_id='subject-self',
        )

        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            {item.value for item in candidates},
            {'高血压', '糖尿病'},
        )
        self.assertEqual(len({item.candidate_id for item in candidates}), 2)

    async def test_exact_duplicate_candidate_is_deduplicated(self):
        extracted = {
            'factType': 'symptom.reported',
            'displayName': '头痛',
            'valueType': 'text',
            'value': '经常头痛',
            'subjectKind': 'self',
            'subjectHint': '本人',
            'evidenceText': '我最近经常头痛',
            'confidence': 0.96,
        }
        runtime, _ = _runtime({'candidates': [extracted, extracted]})
        single_runtime, _ = _runtime({'candidates': [extracted]})

        with patch('health_profile.chat_ingestion.logger') as logger:
            candidates = await ChatMessageCandidateExtractor(
                runtime.hub,
            ).extract(
                ChatFixture.record(),
                self_subject_id='subject-self',
            )
        single = await ChatMessageCandidateExtractor(
            single_runtime.hub,
        ).extract(
            ChatFixture.record(),
            self_subject_id='subject-self',
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].candidate_id, single[0].candidate_id)
        message = logger.warning.call_args.args[0]
        self.assertIn('reason_code=duplicate_candidate_value', message)
        self.assertNotIn('经常头痛', message)

    async def test_non_verbatim_evidence_is_rejected(self):
        runtime, _ = _runtime({'candidates': [{
            'factType': 'symptom.reported',
            'displayName': '头痛',
            'valueType': 'text',
            'value': '经常头痛',
            'subjectKind': 'self',
            'subjectHint': '本人',
            'evidenceText': '用户长期频繁头痛',
            'confidence': 0.99,
        }]})

        with patch('health_profile.chat_ingestion.logger') as logger:
            candidates = await ChatMessageCandidateExtractor(
                runtime.hub,
            ).extract(
                ChatFixture.record(),
                self_subject_id='subject-self',
            )

        self.assertEqual(candidates, ())
        message = logger.warning.call_args.args[0]
        self.assertIn('reason_code=evidence_not_verbatim', message)
        self.assertNotIn('用户长期频繁头痛', message)


class ChatProfileProcessorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.record = ChatFixture.record()
        self.subject = ReportIngestionFixture.subject()
        self.repository = FakeProfileRepository(self.subject)
        self.profile_service = FakeRecordProfileService(
            self.subject,
            repository=self.repository,
        )
        self_candidate = MemoryCandidate(
            candidate_id='chat-symptom-1',
            subject_id=self.subject.subject_id,
            subject_hint='本人',
            subject_kind_hint=SubjectKind.SELF,
            fact_type='symptom.reported',
            display_name='头痛',
            value_type=FactValueType.TEXT,
            value='经常头痛',
            assertion_level=AssertionLevel.USER_REPORTED,
            confidence=0.8,
            effective_period=EffectivePeriod(start=ChatFixture.now),
            source=FactSource(
                source_type='chat',
                source_id='message-1',
                source_revision=1,
                pointer='/messages/message-1/content',
                observed_at=ChatFixture.now,
                excerpt_hash='hash-self',
            ),
        )
        family_candidate = MemoryCandidate(
            candidate_id='chat-family-1',
            subject_id=None,
            subject_hint='父亲',
            subject_kind_hint=SubjectKind.BLOOD_RELATIVE,
            fact_type='family_history.condition',
            display_name='高血压家族史',
            value_type=FactValueType.TEXT,
            value='高血压',
            assertion_level=AssertionLevel.USER_REPORTED,
            confidence=0.8,
            effective_period=EffectivePeriod(start=ChatFixture.now),
            source=FactSource(
                source_type='chat',
                source_id='message-1',
                source_revision=1,
                pointer='/messages/message-1/content',
                observed_at=ChatFixture.now,
                excerpt_hash='hash-family',
            ),
        )
        self.extractor = FakeChatExtractor((self_candidate, family_candidate))
        id_sequence = 0

        def next_id(prefix):
            nonlocal id_sequence
            id_sequence += 1
            return f'{prefix}-chat-{id_sequence}'

        self.processor = ChatProfileProcessor(
            store=FakeChatStore(self.record),
            repository=self.repository,
            profile_service=self.profile_service,
            agent_hub=object(),
            extractor=self.extractor,
            clock=lambda: ChatFixture.now,
            id_factory=next_id,
        )

    async def test_writes_self_and_stable_family_facts_as_pending(self):
        first = await self.processor.process(ChatFixture.job())
        replay = await self.processor.process(ChatFixture.job())

        self.assertEqual(first.candidate_count, 2)
        self.assertEqual(first.fact_count, 2)
        self.assertEqual(replay.replayed_fact_count, 2)
        self.assertEqual(len(self.repository.facts), 2)
        self.assertTrue(all(
            fact.status is FactStatus.PENDING
            for fact in self.repository.facts
        ))
        by_type = {fact.fact_type: fact for fact in self.repository.facts}
        self.assertEqual(
            by_type['symptom.reported'].subject_id,
            self.subject.subject_id,
        )
        self.assertEqual(
            by_type['family_history.condition'].subject_id,
            'subject-father',
        )
        self.assertEqual(len(self.profile_service.subject_calls), 1)


if __name__ == '__main__':
    unittest.main()
