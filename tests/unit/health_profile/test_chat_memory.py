"""普通对话读取用户画像记忆的无网络测试。"""

from __future__ import annotations

import unittest
from datetime import datetime
from datetime import timezone

from health_profile import AssertionLevel
from health_profile import EffectivePeriod
from health_profile import FactCategory
from health_profile import FactSource
from health_profile import FactStatus
from health_profile import FactValueType
from health_profile import HealthFact
from health_profile import HealthSubject
from health_profile import RepositoryHealthProfileChatMemoryProvider
from health_profile import SubjectKind
from health_profile import SubjectRelationship
from health_profile import SubjectStatus
from health_profile import VerificationStatus
from tests.unit.health_profile.test_report_ingestion import (
    FakeProfileRepository,
)


class HealthProfileChatMemoryProviderTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 18, 8, 0, tzinfo=timezone.utc)
        self.self_subject = self._subject(
            subject_id='subject-self',
            kind=SubjectKind.SELF,
            display_name='本人',
        )
        self.repository = FakeProfileRepository(self.self_subject)

    def _subject(
            self,
            *,
            subject_id: str,
            kind: SubjectKind,
            display_name: str,
            relation_type: str | None = None,
            is_blood_relative: bool = False,
    ) -> HealthSubject:
        relationship = None
        if kind is not SubjectKind.SELF:
            relationship = SubjectRelationship(
                relation_type=relation_type or kind.value,
                is_blood_relative=is_blood_relative,
            )
        return HealthSubject(
            subject_id=subject_id,
            user_id='user-1',
            kind=kind,
            display_name=display_name,
            relationship=relationship,
            status=SubjectStatus.ACTIVE,
            created_at=self.now,
            updated_at=self.now,
        )

    def _fact(
            self,
            *,
            fact_id: str,
            subject_id: str = 'subject-self',
            fact_type: str = 'condition.diagnosis',
            category: FactCategory = FactCategory.CONDITION,
            display_name: str = '高血压',
            value='高血压',
            status: FactStatus = FactStatus.ACTIVE,
            day: int = 18,
            source_type: str = 'record',
    ) -> HealthFact:
        active = status is FactStatus.ACTIVE
        return HealthFact(
            fact_id=fact_id,
            user_id='user-1',
            subject_id=subject_id,
            fact_type=fact_type,
            category=category,
            display_name=display_name,
            value_type=(
                FactValueType.QUANTITY
                if isinstance(value, dict)
                else FactValueType.TEXT
            ),
            value=value,
            assertion_level=(
                AssertionLevel.USER_CONFIRMED
                if active
                else AssertionLevel.USER_REPORTED
            ),
            verification_status=(
                VerificationStatus.CONFIRMED
                if active
                else (
                    VerificationStatus.CONFLICTED
                    if status is FactStatus.DISPUTED
                    else VerificationStatus.UNCONFIRMED
                )
            ),
            confidence=0.8,
            effective_period=EffectivePeriod(start=datetime(
                2026,
                8,
                day,
                tzinfo=timezone.utc,
            )),
            status=status,
            sources=(FactSource(
                source_type=source_type,
                source_id=f'source-{fact_id}',
                source_revision=1,
            ),),
            created_at=self.now,
            updated_at=self.now,
        )

    async def test_renders_active_pending_and_blood_family_with_boundaries(
            self,
    ) -> None:
        father = self._subject(
            subject_id='subject-father',
            kind=SubjectKind.BLOOD_RELATIVE,
            display_name='父亲',
            relation_type='father',
            is_blood_relative=True,
        )
        friend = self._subject(
            subject_id='subject-friend',
            kind=SubjectKind.FRIEND,
            display_name='朋友小李',
            relation_type='friend',
        )
        self.repository.additional_subjects.extend((father, friend))
        self.repository.facts.extend((
            self._fact(fact_id='fact-active'),
            self._fact(
                fact_id='fact-pending',
                fact_type='symptom.reported',
                category=FactCategory.SYMPTOM,
                display_name='睡眠不佳',
                value='最近经常失眠',
                status=FactStatus.PENDING,
                source_type='chat',
            ),
            self._fact(
                fact_id='fact-family',
                subject_id='subject-father',
                fact_type='family_history.condition',
                category=FactCategory.FAMILY_HISTORY,
                display_name='家族疾病史',
                value='糖尿病',
                status=FactStatus.PENDING,
            ),
            self._fact(
                fact_id='fact-friend',
                subject_id='subject-friend',
                fact_type='symptom.reported',
                category=FactCategory.SYMPTOM,
                display_name='朋友症状',
                value='朋友小李头痛',
                status=FactStatus.PENDING,
            ),
        ))

        context = await RepositoryHealthProfileChatMemoryProvider(
            self.repository,
        ).build_context('user-1')

        self.assertIsNotNone(context)
        assert context is not None
        self.assertIn('fact-active', context)
        self.assertIn('fact-pending', context)
        self.assertIn('"status":"pending"', context)
        self.assertIn('fact-family', context)
        self.assertIn('"subjectKind":"blood_relative"', context)
        self.assertIn('"relationType":"father"', context)
        self.assertNotIn('fact-friend', context)
        self.assertNotIn('朋友小李头痛', context)
        self.assertNotIn('source-fact-active', context)

    async def test_keeps_latest_measurement_and_applies_pending_budget(
            self,
    ) -> None:
        self.repository.facts.extend((
            self._fact(
                fact_id='weight-old',
                fact_type='anthropometric.weight',
                category=FactCategory.ANTHROPOMETRIC,
                display_name='体重',
                value={'number': 70, 'unit': 'kg'},
                day=10,
            ),
            self._fact(
                fact_id='weight-new',
                fact_type='anthropometric.weight',
                category=FactCategory.ANTHROPOMETRIC,
                display_name='体重',
                value={'number': 68, 'unit': 'kg'},
                day=18,
            ),
            self._fact(
                fact_id='age-old',
                fact_type='demographic.age',
                category=FactCategory.DEMOGRAPHIC,
                display_name='年龄',
                value={'number': 41, 'unit': 'years'},
                day=10,
            ),
            self._fact(
                fact_id='age-new',
                fact_type='demographic.age',
                category=FactCategory.DEMOGRAPHIC,
                display_name='年龄',
                value={'number': 42, 'unit': 'years'},
                day=18,
            ),
            self._fact(
                fact_id='pending-old',
                display_name='旧待确认',
                value='旧待确认内容',
                status=FactStatus.PENDING,
                day=10,
            ),
            self._fact(
                fact_id='pending-new',
                display_name='新待确认',
                value='新待确认内容',
                status=FactStatus.PENDING,
                day=18,
            ),
        ))
        provider = RepositoryHealthProfileChatMemoryProvider(
            self.repository,
            max_pending_facts=1,
        )

        context = await provider.build_context('user-1')

        self.assertIsNotNone(context)
        assert context is not None
        self.assertIn('weight-new', context)
        self.assertNotIn('weight-old', context)
        self.assertIn('age-new', context)
        self.assertNotIn('age-old', context)
        self.assertIn('pending-new', context)
        self.assertNotIn('pending-old', context)

    async def test_empty_profile_does_not_create_prompt_context(self) -> None:
        context = await RepositoryHealthProfileChatMemoryProvider(
            self.repository,
        ).build_context('user-1')

        self.assertIsNone(context)


if __name__ == '__main__':
    unittest.main()
