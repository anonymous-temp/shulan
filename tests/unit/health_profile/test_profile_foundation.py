"""用户健康画像领域与 MongoDB 骨架的无网络测试。"""

from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace

from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from health_profile import AssertionLevel
from health_profile import EffectivePeriod
from health_profile import FactCategory
from health_profile import FactSource
from health_profile import FactStatus
from health_profile import FactValueType
from health_profile import HealthFact
from health_profile import HealthProfileService
from health_profile import MongoHealthProfileRepository
from health_profile import VerificationStatus


class FakeCollection:
    def __init__(self) -> None:
        self.documents: list[dict] = []
        self.indexes: list[tuple[list, dict]] = []

    async def create_index(self, fields, **options):
        self.indexes.append((deepcopy(fields), deepcopy(options)))
        return options['name']

    async def find_one(self, filters):
        for document in self.documents:
            if all(document.get(key) == value for key, value in filters.items()):
                return deepcopy(document)
        return None

    async def insert_one(self, document):
        if any(item.get('_id') == document.get('_id') for item in self.documents):
            raise DuplicateKeyError('duplicate _id')
        self.documents.append(deepcopy(document))
        return SimpleNamespace(acknowledged=True)


class ProfileFoundationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.subjects = FakeCollection()
        self.facts = FakeCollection()
        self.events = FakeCollection()
        self.views = FakeCollection()
        self.jobs = FakeCollection()
        self.repository = MongoHealthProfileRepository(
            subjects=self.subjects,
            facts=self.facts,
            events=self.events,
            views=self.views,
            jobs=self.jobs,
        )
        self.id_sequence = 0

        def next_id(prefix: str) -> str:
            self.id_sequence += 1
            return f'{prefix}_{self.id_sequence}'

        self.now = datetime(2026, 8, 18, 8, 30, tzinfo=timezone.utc)
        self.service = HealthProfileService(
            self.repository,
            clock=lambda: self.now,
            id_factory=next_id,
        )

    async def test_creates_one_self_subject_event_and_empty_view(self) -> None:
        first = await self.service.ensure_self_profile(
            ' user-1 ',
            display_name=' 张三 ',
        )
        second = await self.service.ensure_self_profile(
            'user-1',
            display_name='不会覆盖既有名称',
        )

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.subject, second.subject)
        self.assertEqual(first.subject.user_id, 'user-1')
        self.assertEqual(first.subject.subject_id, 'subject_1')
        self.assertEqual(first.subject.display_name, '张三')
        self.assertEqual(first.view.profile_version, 1)
        self.assertEqual(first.view.category_counts, {})
        self.assertEqual(first.view.key_fact_ids, ())
        self.assertEqual(first.view.pending_confirmation_count, 0)
        self.assertEqual(len(self.subjects.documents), 1)
        self.assertEqual(len(self.events.documents), 1)
        self.assertEqual(len(self.views.documents), 1)
        self.assertEqual(len(self.facts.documents), 0)
        self.assertEqual(len(self.jobs.documents), 0)
        self.assertEqual(
            self.events.documents[0]['eventType'],
            'subject_created',
        )
        self.assertIsInstance(
            self.events.documents[0]['createdAt'],
            datetime,
        )

    async def test_different_users_never_share_a_subject_or_view(self) -> None:
        first = await self.service.ensure_self_profile('user-1')
        second = await self.service.ensure_self_profile('user-2')

        self.assertNotEqual(first.subject.subject_id, second.subject.subject_id)
        self.assertEqual(
            {item['userId'] for item in self.subjects.documents},
            {'user-1', 'user-2'},
        )
        self.assertEqual(
            {item['_id'] for item in self.views.documents},
            {
                f'user-1:{first.subject.subject_id}',
                f'user-2:{second.subject.subject_id}',
            },
        )

    async def test_creates_all_required_indexes_only_once(self) -> None:
        await self.repository.ensure_indexes()
        await self.repository.ensure_indexes()

        self.assertEqual(len(self.subjects.indexes), 5)
        self.assertEqual(len(self.facts.indexes), 6)
        self.assertEqual(len(self.events.indexes), 3)
        self.assertEqual(len(self.views.indexes), 2)
        self.assertEqual(len(self.jobs.indexes), 4)
        self.assertEqual(
            self.subjects.indexes[2][1]['partialFilterExpression'],
            {'kind': 'self', 'status': 'active'},
        )
        self.assertTrue(self.subjects.indexes[2][1]['unique'])
        self.assertEqual(
            self.subjects.indexes[3][1]['partialFilterExpression'],
            {'creationRequestId': {'$type': 'string'}},
        )
        self.assertTrue(self.subjects.indexes[3][1]['unique'])
        self.assertEqual(
            self.facts.indexes[4],
            (
                [('userId', 1), ('subjectId', 1), ('physicalOccurrenceAliases', 1)],
                {'name': 'health_fact_subject_occurrence_alias'},
            ),
        )
        self.assertEqual(
            self.facts.indexes[5][0],
            [
                ('userId', 1),
                ('creationRequestId', 1),
                ('creationRequestSlot', 1),
            ],
        )
        self.assertTrue(self.facts.indexes[5][1]['unique'])

    async def test_rejects_empty_user_and_naive_clock(self) -> None:
        with self.assertRaisesRegex(ValueError, 'user_id'):
            await self.service.ensure_self_profile('  ')

        service = HealthProfileService(
            self.repository,
            clock=lambda: datetime(2026, 8, 18),
        )
        with self.assertRaisesRegex(ValueError, 'timezone-aware'):
            await service.ensure_self_profile('user-1')


class HealthFactModelTest(unittest.TestCase):
    def test_model_inferred_fact_cannot_become_active_directly(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            'model inferred fact cannot become active directly',
        ):
            HealthFact(
                fact_id='fact-1',
                user_id='user-1',
                subject_id='subject-1',
                fact_type='condition.diabetes',
                category=FactCategory.CONDITION,
                display_name='糖尿病',
                value_type=FactValueType.BOOLEAN,
                value=True,
                assertion_level=AssertionLevel.MODEL_INFERRED,
                verification_status=VerificationStatus.UNCONFIRMED,
                confidence=0.8,
                effective_period=EffectivePeriod(
                    start=datetime(2026, 8, 18, tzinfo=timezone.utc),
                ),
                status=FactStatus.ACTIVE,
                sources=(
                    FactSource(
                        source_type='chat',
                        source_id='message-1',
                        source_revision=1,
                    ),
                ),
                created_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
                updated_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
            )

    def test_effective_period_rejects_reverse_order(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            'end cannot precede start',
        ):
            EffectivePeriod(
                start=datetime(2026, 8, 19, tzinfo=timezone.utc),
                end=datetime(2026, 8, 18, tzinfo=timezone.utc),
            )


if __name__ == '__main__':
    unittest.main()
