"""报告、建档、聊天摄取到跨会话画像记忆的闭环测试。"""

from __future__ import annotations

import unittest

from health_profile import AssertionLevel
from health_profile import ChatProfileProcessor
from health_profile import EffectivePeriod
from health_profile import FactSource
from health_profile import FactStatus
from health_profile import FactValueType
from health_profile import HealthReportProfileProcessor
from health_profile import MemoryCandidate
from health_profile import RecordProfileProcessor
from health_profile import RepositoryHealthProfileChatMemoryProvider
from health_profile import SubjectKind
from tests.unit.health_profile.test_chat_ingestion import ChatFixture
from tests.unit.health_profile.test_chat_ingestion import FakeChatExtractor
from tests.unit.health_profile.test_chat_ingestion import FakeChatStore
from tests.unit.health_profile.test_record_ingestion import FakeAnswerExtractor
from tests.unit.health_profile.test_record_ingestion import FakeRecordProfileService
from tests.unit.health_profile.test_record_ingestion import FakeRecordStore
from tests.unit.health_profile.test_record_ingestion import RecordFixture
from tests.unit.health_profile.test_report_ingestion import FakeProfileRepository
from tests.unit.health_profile.test_report_ingestion import FakeReportStore
from tests.unit.health_profile.test_report_ingestion import ReportIngestionFixture


class HealthProfileIngestionMemoryFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_sources_become_memory_for_a_new_chat_session(self) -> None:
        subject = ReportIngestionFixture.subject()
        repository = FakeProfileRepository(subject)
        profile_service = FakeRecordProfileService(
            subject,
            repository=repository,
        )
        sequence = 0

        def next_id(prefix: str) -> str:
            nonlocal sequence
            sequence += 1
            return f'{prefix}-flow-{sequence}'

        report_result = await HealthReportProfileProcessor(
            store=FakeReportStore(ReportIngestionFixture.record()),
            repository=repository,
            clock=lambda: ReportIngestionFixture.now,
            id_factory=next_id,
        ).process(ReportIngestionFixture.job())

        record_family = MemoryCandidate(
            candidate_id='record-family-diabetes',
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
                excerpt_hash='record-family-hash',
            ),
        )
        record_result = await RecordProfileProcessor(
            store=FakeRecordStore(RecordFixture.record()),
            repository=repository,
            profile_service=profile_service,
            agent_hub=object(),
            answer_extractor=FakeAnswerExtractor((record_family,)),
            clock=lambda: RecordFixture.now,
            id_factory=next_id,
        ).process(RecordFixture.job())

        chat_symptom = MemoryCandidate(
            candidate_id='chat-self-headache',
            subject_id=subject.subject_id,
            subject_hint='本人',
            subject_kind_hint=SubjectKind.SELF,
            fact_type='symptom.reported',
            display_name='头痛',
            value_type=FactValueType.TEXT,
            value='最近经常头痛',
            assertion_level=AssertionLevel.USER_REPORTED,
            confidence=0.8,
            effective_period=EffectivePeriod(start=ChatFixture.now),
            source=FactSource(
                source_type='chat',
                source_id='message-1',
                source_revision=1,
                pointer='/messages/message-1/content',
                observed_at=ChatFixture.now,
                excerpt_hash='chat-self-hash',
            ),
        )
        chat_family = MemoryCandidate(
            candidate_id='chat-family-hypertension',
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
                excerpt_hash='chat-family-hash',
            ),
        )
        chat_result = await ChatProfileProcessor(
            store=FakeChatStore(ChatFixture.record()),
            repository=repository,
            profile_service=profile_service,
            agent_hub=object(),
            extractor=FakeChatExtractor((chat_symptom, chat_family)),
            clock=lambda: ChatFixture.now,
            id_factory=next_id,
        ).process(ChatFixture.job())

        memory = await RepositoryHealthProfileChatMemoryProvider(
            repository,
        ).build_context('user-1')

        self.assertEqual(report_result.fact_count, 2)
        self.assertEqual(record_result.fact_count, 4)
        self.assertEqual(chat_result.fact_count, 2)
        self.assertEqual(len(repository.facts), 8)
        self.assertEqual(
            sum(fact.status is FactStatus.ACTIVE for fact in repository.facts),
            5,
        )
        self.assertEqual(
            sum(fact.status is FactStatus.PENDING for fact in repository.facts),
            3,
        )
        fathers = [
            item
            for item in repository.additional_subjects
            if item.relationship is not None and item.relationship.relation_type == 'father'
        ]
        self.assertEqual(len(fathers), 1)
        father_id = fathers[0].subject_id
        self.assertEqual(
            {fact.subject_id for fact in repository.facts if fact.fact_type == 'family_history.condition'},
            {father_id},
        )
        self.assertEqual(len(profile_service.subject_calls), 1)

        self.assertIsNotNone(memory)
        assert memory is not None
        for expected in (
            'laboratory.uric_acid',
            'demographic.sex',
            'demographic.age',
            'anthropometric.height',
            'symptom.reported',
            '糖尿病',
            '高血压',
            '"relationType":"father"',
            '"status":"active"',
            '"status":"pending"',
        ):
            self.assertIn(expected, memory)
        self.assertNotIn('您最近是否有身体不适', memory)
        self.assertNotIn('不得发送给提取模型', memory)


if __name__ == '__main__':
    unittest.main()
