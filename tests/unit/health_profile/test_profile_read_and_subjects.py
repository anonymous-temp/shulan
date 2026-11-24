"""用户画像读取、稳定主体和循证支持的无网络测试。"""

from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import datetime
from datetime import timezone

from langgraph.checkpoint.memory import InMemorySaver

from agents import AgentHub
from agents import AgentRuntime
from agents import GraphRegistry
from agents.graphs.health_profile import register_profile_evidence
from agents.knowledge import KnowledgeDocument
from agents.knowledge import KnowledgeSource
from agents.llm import ModelProfileRegistry
from agents.persistence import AgentLease
from health_profile import AssertionLevel
from health_profile import EffectivePeriod
from health_profile import FactCategory
from health_profile import FactSource
from health_profile import FactStatus
from health_profile import FactValueType
from health_profile import HealthFact
from health_profile import HealthProfileConflictError
from health_profile import HealthProfileNotFoundError
from health_profile import HealthProfileService
from health_profile import HealthProfileValidationError
from health_profile import MongoHealthProfileRepository
from health_profile import VerificationStatus
from tests.unit._support.profile_mongo import FakeCollection as FakeCollection
from tests.unit._support.profile_mongo import FakeCursor as FakeCursor
from tests.unit._support.profile_mongo import _matches as _matches
from tests.unit._support.profile_mongo import _nested_value as _nested_value
from tests.unit._support.profile_mongo import _set_nested as _set_nested


class FakeKnowledgeGateway:
    enabled = True

    def __init__(self):
        self.queries = []

    async def retrieve(self, query):
        self.queries.append(query)
        return (
            KnowledgeDocument(
                source=KnowledgeSource.GUIDE,
                title='肝功能异常评估指南',
                content='需要结合完整临床资料综合评估。',
                url='https://example.test/guide',
                metadata={'id': 'guide-1', 'publisher': '测试学会'},
            ),
        )


class FakeLeaseManager:
    def __init__(self):
        self.available = True
        self.acquire_calls = []
        self.release_calls = []

    async def try_acquire(self, thread_id, owner_id):
        self.acquire_calls.append((thread_id, owner_id))
        if not self.available:
            return None
        return AgentLease(
            key=thread_id,
            owner_id=owner_id,
            ttl_seconds=30,
        )

    async def renew(self, lease):
        return True

    async def release(self, lease):
        self.release_calls.append(lease)
        return True


def _build_hub(knowledge_gateway):
    registry = GraphRegistry()
    runtime = AgentRuntime(
        hub=AgentHub(registry),
        registry=registry,
        profiles=ModelProfileRegistry(),
        llm_gateway=object(),
        context_repository=object(),
        lease_manager=object(),
        checkpointer=InMemorySaver(),
        _close_callback=lambda: None,
        knowledge_gateway=knowledge_gateway,
    )
    register_profile_evidence(runtime)
    return runtime.hub


class ProfileReadAndSubjectsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.subjects = FakeCollection()
        self.facts = FakeCollection()
        self.events = FakeCollection()
        self.views = FakeCollection()
        self.jobs = FakeCollection()
        repository = MongoHealthProfileRepository(
            subjects=self.subjects,
            facts=self.facts,
            events=self.events,
            views=self.views,
            jobs=self.jobs,
        )
        self.now = datetime(2026, 8, 18, 8, 30, tzinfo=timezone.utc)
        self.id_sequence = 0

        def next_id(prefix):
            self.id_sequence += 1
            return f'{prefix}_{self.id_sequence}'

        self.knowledge_gateway = FakeKnowledgeGateway()
        self.lease_manager = FakeLeaseManager()
        self.service = HealthProfileService(
            repository,
            agent_hub=_build_hub(self.knowledge_gateway),
            lease_manager=self.lease_manager,
            clock=lambda: self.now,
            id_factory=next_id,
        )

    def _add_fact(
        self,
        *,
        user_id,
        subject_id,
        fact_id,
        status=FactStatus.PENDING,
        fact_type='anthropometric.weight',
        category=FactCategory.ANTHROPOMETRIC,
        display_name='体重',
        value=63,
        value_type=FactValueType.QUANTITY,
        unit='kg',
        interpretation=None,
        day=1,
    ):
        assertion_level = AssertionLevel.MODEL_INFERRED
        verification_status = VerificationStatus.UNCONFIRMED
        if status is FactStatus.ACTIVE:
            assertion_level = AssertionLevel.USER_CONFIRMED
            verification_status = VerificationStatus.CONFIRMED
        elif status is FactStatus.DISPUTED:
            verification_status = VerificationStatus.CONFLICTED
        fact_value = {'number': value, 'unit': unit} if value_type is FactValueType.QUANTITY else value
        fact = HealthFact(
            fact_id=fact_id,
            user_id=user_id,
            subject_id=subject_id,
            fact_type=fact_type,
            category=category,
            display_name=display_name,
            value_type=value_type,
            value=fact_value,
            interpretation=interpretation,
            assertion_level=assertion_level,
            verification_status=verification_status,
            confidence=0.8,
            effective_period=EffectivePeriod(
                start=datetime(
                    2026,
                    8,
                    day,
                    tzinfo=timezone.utc,
                )
            ),
            status=status,
            sources=(
                FactSource(
                    source_type='chat',
                    source_id=f'message-{fact_id}',
                    source_revision=1,
                ),
            ),
            created_at=self.now,
            updated_at=self.now,
        )
        self.facts.documents.append(fact.to_document(document_id=fact_id))
        return fact

    async def test_empty_self_profile_is_created_on_first_read(self):
        profile = await self.service.get_profile('user-1')

        self.assertEqual(profile['subject']['kind'], 'self')
        self.assertEqual(profile['profileVersion'], 1)
        self.assertEqual(profile['indicatorGroups'], [])
        self.assertEqual(profile['factGroups'], [])
        self.assertEqual(profile['alerts'], [])
        self.assertEqual(profile['overview']['pendingConfirmationCount'], 0)
        self.assertEqual(profile['evidence']['status'], 'not_requested')

    async def test_overview_reads_canonical_sex_and_observed_age(self):
        bootstrap = await self.service.ensure_self_profile('user-1')
        for fact_id, fact_type, display_name, value_type, value in (
            (
                'fact-sex',
                'demographic.sex',
                '性别',
                FactValueType.CODED,
                'female',
            ),
            (
                'fact-age',
                'demographic.age',
                '年龄',
                FactValueType.QUANTITY,
                {'number': 42, 'unit': 'years'},
            ),
        ):
            fact = HealthFact(
                fact_id=fact_id,
                user_id='user-1',
                subject_id=bootstrap.subject.subject_id,
                fact_type=fact_type,
                category=FactCategory.DEMOGRAPHIC,
                display_name=display_name,
                value_type=value_type,
                value=value,
                assertion_level=AssertionLevel.USER_CONFIRMED,
                verification_status=VerificationStatus.CONFIRMED,
                confidence=1,
                effective_period=EffectivePeriod(start=self.now),
                status=FactStatus.ACTIVE,
                sources=(
                    FactSource(
                        source_type='record',
                        source_id='session-1',
                        source_revision=1,
                        observed_at=self.now,
                    ),
                ),
                created_at=self.now,
                updated_at=self.now,
            )
            self.facts.documents.append(
                fact.to_document(document_id=fact.fact_id),
            )

        profile = await self.service.get_profile('user-1')

        self.assertEqual(profile['overview']['gender'], 'female')
        self.assertEqual(profile['overview']['age'], 42)
        self.assertIsNone(profile['overview']['dateOfBirth'])

    async def test_pending_facts_join_fact_groups_without_changing_old_projection(self):
        bootstrap = await self.service.ensure_self_profile('user-pending')
        subject_id = bootstrap.subject.subject_id
        self._add_fact(
            user_id='user-pending',
            subject_id=subject_id,
            fact_id='fact-pending-weight',
            value=63,
        )
        self._add_fact(
            user_id='user-pending',
            subject_id=subject_id,
            fact_id='fact-pending-condition',
            fact_type='condition.diagnosis',
            category=FactCategory.CONDITION,
            display_name='两肾细小结石',
            value='两肾细小结石',
            value_type=FactValueType.TEXT,
        )
        self._add_fact(
            user_id='user-pending',
            subject_id=subject_id,
            fact_id='fact-pending-condition-2',
            fact_type='condition.diagnosis',
            category=FactCategory.CONDITION,
            display_name='结肠肝曲结节（间质瘤可能）',
            value='结肠肝曲结节（间质瘤可能）',
            value_type=FactValueType.TEXT,
            day=2,
        )
        self._add_fact(
            user_id='user-pending',
            subject_id=subject_id,
            fact_id='fact-pending-symptom',
            fact_type='symptom.reported',
            category=FactCategory.SYMPTOM,
            display_name='无明显不适',
            value='无明显不适',
            value_type=FactValueType.TEXT,
        )
        facts_before = deepcopy(self.facts.documents)

        profile = await self.service.get_profile('user-pending')
        active_only_profile = await self.service.get_profile(
            'user-pending',
            fact_statuses=['active'],
        )

        self.assertEqual(profile['profileVersion'], 1)
        self.assertIsNone(profile['overview']['weightKg'])
        self.assertEqual(profile['overview']['pendingConfirmationCount'], 4)
        self.assertEqual(profile['indicatorGroups'], [])
        self.assertEqual(
            [group['code'] for group in profile['factGroups']],
            ['anthropometric', 'condition', 'symptom'],
        )
        groups = {group['code']: group for group in profile['factGroups']}
        self.assertEqual(
            [item['displayName'] for item in groups['condition']['items']],
            ['结肠肝曲结节（间质瘤可能）', '两肾细小结石'],
        )
        pending_weight = groups['anthropometric']['items'][0]
        self.assertEqual(pending_weight['status'], 'pending')
        self.assertEqual(pending_weight['value']['number'], 63)
        self.assertEqual(
            pending_weight['sources'][0]['sourceId'],
            'message-fact-pending-weight',
        )
        self.assertEqual(
            profile['summary']['overall'],
            '暂无可用于评估的数据。',
        )
        self.assertEqual(profile['alerts'], [])
        self.assertEqual(profile['evidence']['status'], 'not_requested')
        self.assertEqual(self.facts.documents, facts_before)
        self.assertIsNone(active_only_profile['overview']['weightKg'])
        self.assertEqual(active_only_profile['factGroups'], [])
        self.assertEqual(active_only_profile['summary'], profile['summary'])
        self.assertEqual(active_only_profile['overview'], profile['overview'])

    async def test_fact_groups_can_filter_to_active_and_active_wins(self):
        bootstrap = await self.service.ensure_self_profile('user-priority')
        subject_id = bootstrap.subject.subject_id
        self._add_fact(
            user_id='user-priority',
            subject_id=subject_id,
            fact_id='fact-active-weight',
            status=FactStatus.ACTIVE,
            value=61,
            day=1,
        )
        self._add_fact(
            user_id='user-priority',
            subject_id=subject_id,
            fact_id='fact-pending-weight',
            value=72,
            day=2,
        )
        self._add_fact(
            user_id='user-priority',
            subject_id=subject_id,
            fact_id='fact-active-weight-new',
            status=FactStatus.ACTIVE,
            value=65,
            day=3,
        )

        default_profile = await self.service.get_profile('user-priority')
        active_only_profile = await self.service.get_profile(
            'user-priority',
            fact_statuses=['active'],
        )

        self.assertEqual(default_profile['overview']['weightKg'], 65)
        self.assertEqual(
            default_profile['overview']['pendingConfirmationCount'],
            1,
        )
        default_items = default_profile['factGroups'][0]['items']
        self.assertEqual(
            [item['status'] for item in default_items],
            ['active', 'pending'],
        )
        self.assertEqual(
            [item['value']['number'] for item in default_items],
            [65, 72],
        )
        self.assertEqual(active_only_profile['overview']['weightKg'], 65)
        self.assertEqual(
            [item['factId'] for item in active_only_profile['factGroups'][0]['items']],
            ['fact-active-weight-new'],
        )
        self.assertEqual(active_only_profile['overview'], default_profile['overview'])
        self.assertEqual(active_only_profile['summary'], default_profile['summary'])

    async def test_pending_laboratory_fact_has_no_alert_or_evidence(self):
        bootstrap = await self.service.ensure_self_profile('user-pending-lab')
        self._add_fact(
            user_id='user-pending-lab',
            subject_id=bootstrap.subject.subject_id,
            fact_id='fact-pending-alt',
            fact_type='lab.alt',
            category=FactCategory.LABORATORY,
            display_name='丙氨酸氨基转移酶',
            value=58,
            unit='U/L',
            interpretation='high',
        )

        profile = await self.service.get_profile(
            'user-pending-lab',
            include_evidence=True,
        )

        self.assertEqual(profile['indicatorGroups'], [])
        pending_lab = profile['factGroups'][0]['items'][0]
        self.assertEqual(pending_lab['factId'], 'fact-pending-alt')
        self.assertEqual(pending_lab['status'], 'pending')
        self.assertEqual(profile['alerts'], [])
        self.assertEqual(profile['evidence']['status'], 'empty')
        self.assertEqual(self.knowledge_gateway.queries, [])

        self._add_fact(
            user_id='user-pending-lab',
            subject_id=bootstrap.subject.subject_id,
            fact_id='fact-active-alt',
            status=FactStatus.ACTIVE,
            fact_type='lab.alt',
            category=FactCategory.LABORATORY,
            display_name='丙氨酸氨基转移酶',
            value=40,
            unit='U/L',
            interpretation='normal',
        )
        profile_with_active = await self.service.get_profile('user-pending-lab')

        active_indicator = profile_with_active['indicatorGroups'][0]['indicators'][0]
        self.assertEqual(active_indicator['current']['factId'], 'fact-active-alt')
        self.assertEqual(active_indicator['current']['value'], 40)
        self.assertEqual(
            [item['factId'] for item in profile_with_active['factGroups'][0]['items']],
            ['fact-active-alt', 'fact-pending-alt'],
        )

    async def test_indicator_scope_abnormal_keeps_only_abnormal_indicators(self):
        bootstrap = await self.service.ensure_self_profile('user-scope')
        subject_id = bootstrap.subject.subject_id
        self._add_fact(
            user_id='user-scope',
            subject_id=subject_id,
            fact_id='fact-scope-alt-high',
            status=FactStatus.ACTIVE,
            fact_type='lab.alt',
            category=FactCategory.LABORATORY,
            display_name='丙氨酸氨基转移酶',
            value=86,
            unit='U/L',
            interpretation='high',
            day=2,
        )
        self._add_fact(
            user_id='user-scope',
            subject_id=subject_id,
            fact_id='fact-scope-glucose-normal',
            status=FactStatus.ACTIVE,
            fact_type='lab.glucose',
            category=FactCategory.LABORATORY,
            display_name='葡萄糖',
            value=5.1,
            unit='mmol/L',
            interpretation='normal',
        )
        self._add_fact(
            user_id='user-scope',
            subject_id=subject_id,
            fact_id='fact-scope-weight',
            status=FactStatus.ACTIVE,
            fact_type='anthropometric.weight',
            display_name='体重',
            value=70,
            unit='kg',
            interpretation='normal',
        )

        default_profile = await self.service.get_profile('user-scope')
        abnormal_profile = await self.service.get_profile(
            'user-scope',
            indicator_scope='abnormal',
        )

        self.assertEqual(
            default_profile['overview']['pendingConfirmationCount'],
            0,
        )
        self.assertEqual(
            {group['code'] for group in default_profile['indicatorGroups']},
            {'anthropometric', 'laboratory'},
        )
        self.assertEqual(
            [
                indicator['current']['factId']
                for group in abnormal_profile['indicatorGroups']
                for indicator in group['indicators']
            ],
            ['fact-scope-alt-high'],
        )
        abnormal_indicator = abnormal_profile['indicatorGroups'][0]['indicators'][0]
        self.assertEqual(abnormal_indicator['current']['interpretation'], 'high')
        self.assertEqual(len(abnormal_indicator['observations']), 1)
        self.assertEqual(abnormal_profile['alerts'][0]['factIds'], ['fact-scope-alt-high'])
        self.assertIsNotNone(abnormal_profile['summary'])

        # 指标区始终仅由 active 事实生成，factStatuses 不影响指标筛选。
        statuses_profile = await self.service.get_profile(
            'user-scope',
            indicator_scope='abnormal',
            fact_statuses=['pending'],
        )
        self.assertEqual(
            statuses_profile['indicatorGroups'],
            abnormal_profile['indicatorGroups'],
        )
        self.assertEqual(statuses_profile['factGroups'], [])

        with self.assertRaises(HealthProfileValidationError):
            await self.service.get_profile('user-scope', indicator_scope='unknown-scope')
        with self.assertRaises(HealthProfileValidationError):
            await self.service.get_profile('user-scope', indicator_scope=[])

    async def test_indicator_scope_flagged_and_pending_combinations(self):
        bootstrap = await self.service.ensure_self_profile('user-flagged')
        subject_id = bootstrap.subject.subject_id
        self._add_fact(
            user_id='user-flagged',
            subject_id=subject_id,
            fact_id='fact-flagged-alt-high',
            status=FactStatus.ACTIVE,
            fact_type='lab.alt',
            category=FactCategory.LABORATORY,
            display_name='丙氨酸氨基转移酶',
            value=86,
            unit='U/L',
            interpretation='high',
            day=2,
        )
        self._add_fact(
            user_id='user-flagged',
            subject_id=subject_id,
            fact_id='fact-flagged-glucose-normal',
            status=FactStatus.ACTIVE,
            fact_type='lab.glucose',
            category=FactCategory.LABORATORY,
            display_name='葡萄糖',
            value=5.1,
            unit='mmol/L',
            interpretation='normal',
        )
        self._add_fact(
            user_id='user-flagged',
            subject_id=subject_id,
            fact_id='fact-flagged-weight-pending',
            fact_type='anthropometric.weight',
            display_name='体重',
            value=72,
            unit='kg',
            day=1,
        )
        self._add_fact(
            user_id='user-flagged',
            subject_id=subject_id,
            fact_id='fact-flagged-condition-pending',
            fact_type='condition.diagnosis',
            category=FactCategory.CONDITION,
            display_name='两肾细小结石',
            value='两肾细小结石',
            value_type=FactValueType.TEXT,
        )

        flagged = await self.service.get_profile(
            'user-flagged',
            indicator_scope='flagged',
        )
        flagged_items = [indicator for group in flagged['indicatorGroups'] for indicator in group['indicators']]
        self.assertEqual([item['current']['factId'] for item in flagged_items], ['fact-flagged-alt-high'])

        pending_only = await self.service.get_profile(
            'user-flagged',
            indicator_scope='pending',
        )
        pending_items = [indicator for group in pending_only['indicatorGroups'] for indicator in group['indicators']]
        # 实体类（condition）不进入指标区；只有待确认的体重出现。
        self.assertEqual(
            [item['current']['factId'] for item in pending_items],
            ['fact-flagged-weight-pending'],
        )
        self.assertEqual(pending_items[0]['current']['status'], 'pending')
        self.assertEqual(pending_items[0]['trend'], 'unknown')

        combined = await self.service.get_profile(
            'user-flagged',
            indicator_scope=['flagged', 'pending'],
        )
        combined_items = [indicator for group in combined['indicatorGroups'] for indicator in group['indicators']]
        self.assertEqual(
            [item['current']['factId'] for item in combined_items],
            # 组顺序固定为身体测量在检验之前；active 条目先于待确认条目在组内表达。
            ['fact-flagged-weight-pending', 'fact-flagged-alt-high'],
        )
        # 概览与提醒仍 strict active-only：待确认指标不影响 alerts。
        self.assertEqual(combined['alerts'][0]['factIds'], ['fact-flagged-alt-high'])

    async def test_indicator_scope_unjudged_keeps_entries_without_interpretation(self):
        bootstrap = await self.service.ensure_self_profile('user-unjudged')
        subject_id = bootstrap.subject.subject_id
        self._add_fact(
            user_id='user-unjudged',
            subject_id=subject_id,
            fact_id='fact-unjudged-alt-high',
            status=FactStatus.ACTIVE,
            fact_type='lab.alt',
            category=FactCategory.LABORATORY,
            display_name='丙氨酸氨基转移酶',
            value=86,
            unit='U/L',
            interpretation='high',
            day=2,
        )
        self._add_fact(
            user_id='user-unjudged',
            subject_id=subject_id,
            fact_id='fact-unjudged-glucose-normal',
            status=FactStatus.ACTIVE,
            fact_type='lab.glucose',
            category=FactCategory.LABORATORY,
            display_name='葡萄糖',
            value=5.1,
            unit='mmol/L',
            interpretation='normal',
        )
        self._add_fact(
            user_id='user-unjudged',
            subject_id=subject_id,
            fact_id='fact-unjudged-weight',
            status=FactStatus.ACTIVE,
            fact_type='anthropometric.weight',
            display_name='体重',
            value=70,
            unit='kg',
        )

        unjudged = await self.service.get_profile(
            'user-unjudged',
            indicator_scope='unjudged',
        )
        unjudged_items = [indicator for group in unjudged['indicatorGroups'] for indicator in group['indicators']]
        # interpretation=None（来源无标记）即待判断；带标记与 normal 不出现。
        self.assertEqual(
            [item['current']['factId'] for item in unjudged_items],
            ['fact-unjudged-weight'],
        )
        self.assertEqual(unjudged_items[0]['current']['status'], 'active')
        self.assertEqual(unjudged_items[0]['current']['interpretation'], 'unknown')

        combined = await self.service.get_profile(
            'user-unjudged',
            indicator_scope=['flagged', 'unjudged'],
        )
        combined_items = [indicator for group in combined['indicatorGroups'] for indicator in group['indicators']]
        self.assertEqual(
            {item['current']['factId'] for item in combined_items},
            {'fact-unjudged-alt-high', 'fact-unjudged-weight'},
        )

    async def test_fact_statuses_filter_controls_fact_groups_only(self):
        bootstrap = await self.service.ensure_self_profile('user-statuses')
        subject_id = bootstrap.subject.subject_id
        self._add_fact(
            user_id='user-statuses',
            subject_id=subject_id,
            fact_id='fact-statuses-active',
            status=FactStatus.ACTIVE,
            value=65,
            day=3,
        )
        self._add_fact(
            user_id='user-statuses',
            subject_id=subject_id,
            fact_id='fact-statuses-pending',
            value=72,
            day=2,
        )
        self._add_fact(
            user_id='user-statuses',
            subject_id=subject_id,
            fact_id='fact-statuses-disputed',
            status=FactStatus.DISPUTED,
            value=80,
            day=1,
        )

        default = await self.service.get_profile('user-statuses')
        self.assertEqual(default['overview']['pendingConfirmationCount'], 2)
        self.assertEqual(default['overview']['weightKg'], 65)
        self.assertIsNotNone(default['summary'])
        all_ids = ['fact-statuses-active', 'fact-statuses-pending', 'fact-statuses-disputed']
        self.assertEqual([item['factId'] for group in default['factGroups'] for item in group['items']], all_ids)
        for statuses, expected_ids in (
            (None, all_ids),
            (['active'], ['fact-statuses-active']),
            (['pending'], ['fact-statuses-pending']),
            (['disputed'], ['fact-statuses-disputed']),
            (['pending', 'disputed'], ['fact-statuses-pending', 'fact-statuses-disputed']),
            (['active', 'pending', 'disputed'], all_ids),
        ):
            with self.subTest(statuses=statuses):
                selected = await self.service.get_profile('user-statuses', fact_statuses=statuses)
                self.assertEqual(
                    [item['factId'] for group in selected['factGroups'] for item in group['items']],
                    expected_ids,
                )
                for field in ('overview', 'indicatorGroups', 'summary', 'alerts', 'evidence', 'profileVersion'):
                    self.assertEqual(selected[field], default[field], field)

        for invalid in (['superseded'], [], 'active', [None]):
            with self.subTest(invalid=invalid), self.assertRaises(HealthProfileValidationError):
                await self.service.get_profile('user-statuses', fact_statuses=invalid)

    async def test_disputed_fact_is_counted_but_not_projected(self):
        bootstrap = await self.service.ensure_self_profile('user-disputed')
        self._add_fact(
            user_id='user-disputed',
            subject_id=bootstrap.subject.subject_id,
            fact_id='fact-disputed-weight',
            status=FactStatus.DISPUTED,
            value=80,
        )

        profile = await self.service.get_profile('user-disputed')

        self.assertIsNone(profile['overview']['weightKg'])
        self.assertEqual(profile['overview']['pendingConfirmationCount'], 1)
        self.assertEqual(profile['indicatorGroups'], [])
        disputed = profile['factGroups'][0]['items'][0]
        self.assertEqual(disputed['factId'], 'fact-disputed-weight')
        self.assertEqual(disputed['status'], 'disputed')
        self.assertEqual(disputed['verificationStatus'], 'conflicted')

    async def test_entity_fact_group_keeps_same_type_active_facts_separate(self):
        bootstrap = await self.service.ensure_self_profile('user-conditions')
        for fact_id, display_name, day in (
            ('fact-condition-1', '两肾细小结石', 1),
            ('fact-condition-2', '结肠肝曲结节', 2),
        ):
            self._add_fact(
                user_id='user-conditions',
                subject_id=bootstrap.subject.subject_id,
                fact_id=fact_id,
                status=FactStatus.ACTIVE,
                fact_type='condition.diagnosis',
                category=FactCategory.CONDITION,
                display_name=display_name,
                value=display_name,
                value_type=FactValueType.TEXT,
                day=day,
            )

        profile = await self.service.get_profile('user-conditions')

        self.assertEqual(profile['factGroups'][0]['code'], 'condition')
        self.assertEqual(
            [item['factId'] for item in profile['factGroups'][0]['items']],
            ['fact-condition-2', 'fact-condition-1'],
        )

    async def test_subject_create_update_list_and_request_replay(self):
        create = await self.service.upsert_subject(
            'user-1',
            'request-create-father',
            kind='blood_relative',
            display_name='父亲',
            relationship={
                'relationType': 'father',
                'isBloodRelative': True,
            },
        )
        replay = await self.service.upsert_subject(
            'user-1',
            'request-create-father',
            kind='blood_relative',
            display_name='父亲',
            relationship={
                'relationType': 'father',
                'isBloodRelative': True,
            },
        )

        self.assertTrue(create.created)
        self.assertEqual(create, replay)
        self.assertEqual(len(self.subjects.documents), 2)

        update = await self.service.upsert_subject(
            'user-1',
            'request-update-father',
            subject_id=create.subject.subject_id,
            display_name='爸爸',
            expected_subject_version=1,
        )
        update_replay = await self.service.upsert_subject(
            'user-1',
            'request-update-father',
            subject_id=create.subject.subject_id,
            display_name='爸爸',
            expected_subject_version=1,
        )

        self.assertFalse(update.created)
        self.assertEqual(update, update_replay)
        self.assertEqual(update.subject.display_name, '爸爸')
        self.assertEqual(update.subject.version, 2)
        listed = await self.service.list_subjects('user-1')
        self.assertEqual(len(listed['subjects']), 2)
        self.assertNotIn(
            'creationRequestId',
            listed['subjects'][1],
        )

        with self.assertRaises(HealthProfileConflictError):
            await self.service.upsert_subject(
                'user-1',
                'request-stale-update',
                subject_id=create.subject.subject_id,
                display_name='父亲',
                expected_subject_version=1,
            )

    async def test_retry_repairs_missing_event_without_advancing_view_twice(self):
        created = await self.service.upsert_subject(
            'user-1',
            'request-create-friend',
            kind='friend',
            display_name='朋友',
            relationship={
                'relationType': 'friend',
                'isBloodRelative': False,
            },
        )
        first = await self.service.upsert_subject(
            'user-1',
            'request-update-friend',
            subject_id=created.subject.subject_id,
            display_name='老朋友',
            expected_subject_version=1,
        )
        self.events.documents = [
            item for item in self.events.documents if item.get('requestId') != 'request-update-friend'
        ]

        repaired = await self.service.upsert_subject(
            'user-1',
            'request-update-friend',
            subject_id=created.subject.subject_id,
            display_name='老朋友',
            expected_subject_version=1,
        )

        self.assertEqual(repaired, first)
        friend_view = next(item for item in self.views.documents if item['subjectId'] == created.subject.subject_id)
        self.assertEqual(friend_view['profileVersion'], 2)
        self.assertEqual(
            friend_view['lastRequestId'],
            'request-update-friend',
        )
        self.assertEqual(
            sum(item.get('requestId') == 'request-update-friend' for item in self.events.documents),
            1,
        )

    async def test_subject_access_is_always_scoped_by_user_id(self):
        created = await self.service.upsert_subject(
            'user-1',
            'request-create-friend',
            kind='friend',
            display_name='朋友',
            relationship={
                'relationType': 'friend',
                'isBloodRelative': False,
            },
        )

        with self.assertRaises(HealthProfileNotFoundError):
            await self.service.get_profile(
                'user-2',
                subject_id=created.subject.subject_id,
            )

    async def test_laboratory_facts_build_trend_alert_and_evidence(self):
        bootstrap = await self.service.ensure_self_profile('user-1')
        for fact_id, value, day in (
            ('fact-alt-1', 36, 1),
            ('fact-alt-2', 58, 2),
        ):
            fact = HealthFact(
                fact_id=fact_id,
                user_id='user-1',
                subject_id=bootstrap.subject.subject_id,
                fact_type='lab.alt',
                category=FactCategory.LABORATORY,
                display_name='丙氨酸氨基转移酶',
                value_type=FactValueType.QUANTITY,
                value={'number': value, 'unit': 'U/L'},
                interpretation='high' if value == 58 else 'normal',
                reference_range={'text': '9-50 U/L'},
                assertion_level=AssertionLevel.OBSERVED,
                verification_status=VerificationStatus.VERIFIED_SOURCE,
                confidence=0.99,
                effective_period=EffectivePeriod(
                    start=datetime(
                        2026,
                        8,
                        day,
                        tzinfo=timezone.utc,
                    ),
                ),
                status=FactStatus.ACTIVE,
                sources=(
                    FactSource(
                        source_type='health_report',
                        source_id=f'report-{day}',
                        source_revision=1,
                    ),
                ),
                created_at=self.now,
                updated_at=self.now,
            )
            self.facts.documents.append(
                fact.to_document(document_id=fact.fact_id),
            )

        profile = await self.service.get_profile(
            'user-1',
            include_evidence=True,
        )

        indicator = profile['indicatorGroups'][0]['indicators'][0]
        self.assertEqual(indicator['current']['value'], 58)
        self.assertEqual(indicator['current']['referenceRange'], '9-50 U/L')
        self.assertEqual(indicator['trend'], 'up')
        self.assertEqual(
            [item['factId'] for item in profile['factGroups'][0]['items']],
            ['fact-alt-2'],
        )
        self.assertEqual(profile['alerts'][0]['factIds'], ['fact-alt-2'])
        # 无 LLM 网关时综合解释不可用；新语义下不再降级为摘要态条目。
        self.assertEqual(profile['evidence']['status'], 'empty')
        self.assertEqual(profile['evidence']['items'], [])
        self.assertEqual(len(self.knowledge_gateway.queries), 1)

    async def test_fact_list_uses_filter_bound_opaque_cursor(self):
        bootstrap = await self.service.ensure_self_profile('user-facts')
        for day in (3, 2, 1):
            self._add_fact(
                user_id='user-facts',
                subject_id=bootstrap.subject.subject_id,
                fact_id=f'fact-{day}',
                day=day,
                status=FactStatus.PENDING,
            )

        first = await self.service.list_facts(
            'user-facts',
            statuses=['pending'],
            limit=2,
        )
        second = await self.service.list_facts(
            'user-facts',
            statuses=['pending'],
            cursor=first['nextCursor'],
            limit=2,
        )

        self.assertEqual(
            [item['factId'] for item in first['items']],
            ['fact-3', 'fact-2'],
        )
        self.assertEqual(
            [item['factId'] for item in second['items']],
            ['fact-1'],
        )
        self.assertIsNone(second['nextCursor'])
        recent = await self.service.list_facts(
            'user-facts',
            statuses=['pending'],
            effective_from='2026-08-02T00:00:00Z',
        )
        self.assertEqual(
            [item['factId'] for item in recent['items']],
            ['fact-3', 'fact-2'],
        )
        with self.assertRaisesRegex(
            HealthProfileValidationError,
            '查询条件不匹配',
        ):
            await self.service.list_facts(
                'user-facts',
                statuses=['active'],
                cursor=first['nextCursor'],
                limit=2,
            )

    async def test_confirm_replace_delete_and_change_audit(self):
        bootstrap = await self.service.ensure_self_profile('user-governance')
        subject_id = bootstrap.subject.subject_id
        self._add_fact(
            user_id='user-governance',
            subject_id=subject_id,
            fact_id='fact-confirm',
        )
        confirmed = await self.service.resolve_fact(
            'user-governance',
            subject_id,
            'fact-confirm',
            'confirm',
            expected_fact_version=1,
            expected_profile_version=1,
            request_id='request-confirm',
        )
        confirmed_replay = await self.service.resolve_fact(
            'user-governance',
            subject_id,
            'fact-confirm',
            'confirm',
            expected_fact_version=1,
            expected_profile_version=1,
            request_id='request-confirm',
        )
        self.assertEqual(confirmed, confirmed_replay)
        self.assertEqual(confirmed['fact']['status'], 'active')
        self.assertEqual(confirmed['fact']['assertionLevel'], 'user_confirmed')
        self.assertEqual(confirmed['profileVersion'], 2)

        self._add_fact(
            user_id='user-governance',
            subject_id=subject_id,
            fact_id='fact-replace',
            value=70,
            day=2,
        )
        replaced = await self.service.resolve_fact(
            'user-governance',
            subject_id,
            'fact-replace',
            'replace',
            replacement={
                'valueType': 'quantity',
                'value': {'number': 62.5, 'unit': 'kg'},
                'effectivePeriod': {
                    'start': '2026-08-13T00:00:00Z',
                    'end': None,
                },
            },
            reason='用户更正录入错误',
            expected_fact_version=1,
            expected_profile_version=2,
            request_id='request-replace',
        )
        self.assertNotEqual(replaced['fact']['factId'], 'fact-replace')
        self.assertEqual(replaced['fact']['value']['number'], 62.5)
        self.assertEqual(replaced['fact']['status'], 'active')
        old_fact = await self.service._repository.get_fact(
            'user-governance',
            subject_id,
            'fact-replace',
        )
        self.assertEqual(old_fact.status, FactStatus.SUPERSEDED)

        deleted = await self.service.delete_fact(
            'user-governance',
            subject_id,
            replaced['fact']['factId'],
            delete_scope='fact',
            reason='用户要求删除',
            expected_fact_version=1,
            expected_profile_version=3,
            request_id='request-delete',
        )
        deleted_replay = await self.service.delete_fact(
            'user-governance',
            subject_id,
            replaced['fact']['factId'],
            delete_scope='fact',
            reason='用户要求删除',
            expected_fact_version=1,
            expected_profile_version=3,
            request_id='request-delete',
        )
        self.assertEqual(deleted, deleted_replay)
        self.assertEqual(deleted['profileVersion'], 4)
        self.assertEqual(deleted['status'], 'deleted')
        self.assertEqual(deleted['deletedAt'], '2026-08-18T08:30:00Z')
        tombstone = next(item for item in self.facts.documents if item['factId'] == replaced['fact']['factId'])
        self.assertEqual(tombstone['status'], 'deleted')
        self.assertNotIn('value', tombstone)
        self.assertNotIn('factType', tombstone)
        self.assertNotIn('sources', tombstone)
        delete_event = next(item for item in self.events.documents if item['eventType'] == 'fact_deleted')
        self.assertNotIn('reason', delete_event['sourceRef'])

        active = await self.service.list_facts(
            'user-governance',
            statuses=['active'],
        )
        self.assertEqual(
            [item['factId'] for item in active['items']],
            ['fact-confirm'],
        )
        changes = await self.service.list_changes(
            'user-governance',
            event_types=[
                'fact_confirmed',
                'fact_superseded',
                'fact_deleted',
            ],
        )
        self.assertEqual(
            [item['eventType'] for item in changes['items']],
            ['fact_deleted', 'fact_superseded', 'fact_confirmed'],
        )
        self.assertNotIn('operationResult', changes['items'][0])
        first_change_page = await self.service.list_changes(
            'user-governance',
            event_types=[
                'fact_confirmed',
                'fact_superseded',
                'fact_deleted',
            ],
            limit=2,
        )
        second_change_page = await self.service.list_changes(
            'user-governance',
            event_types=[
                'fact_confirmed',
                'fact_superseded',
                'fact_deleted',
            ],
            cursor=first_change_page['nextCursor'],
            limit=2,
        )
        self.assertEqual(
            [item['eventType'] for item in first_change_page['items']],
            ['fact_deleted', 'fact_superseded'],
        )
        self.assertEqual(
            [item['eventType'] for item in second_change_page['items']],
            ['fact_confirmed'],
        )
        with self.assertRaisesRegex(
            HealthProfileValidationError,
            '查询条件不匹配',
        ):
            await self.service.list_changes(
                'user-governance',
                event_types=['fact_confirmed'],
                cursor=first_change_page['nextCursor'],
                limit=2,
            )

    async def test_reject_pending_fact_retracts_same_fact(self):
        bootstrap = await self.service.ensure_self_profile('user-reject')
        subject_id = bootstrap.subject.subject_id
        self._add_fact(
            user_id='user-reject',
            subject_id=subject_id,
            fact_id='fact-reject',
        )

        with self.assertRaises(HealthProfileConflictError):
            await self.service.resolve_fact(
                'user-reject',
                subject_id,
                'fact-reject',
                'reject',
                expected_fact_version=1,
                expected_profile_version=2,
                request_id='request-reject-stale',
            )

        rejected = await self.service.resolve_fact(
            'user-reject',
            subject_id,
            'fact-reject',
            'reject',
            reason='这不是我的健康信息',
            expected_fact_version=1,
            expected_profile_version=1,
            request_id='request-reject',
        )

        self.assertEqual(rejected['previousFactId'], 'fact-reject')
        self.assertEqual(rejected['fact']['factId'], 'fact-reject')
        self.assertEqual(rejected['fact']['status'], 'retracted')
        self.assertEqual(rejected['fact']['version'], 2)
        self.assertEqual(rejected['profileVersion'], 2)
        changes = await self.service.list_changes(
            'user-reject',
            event_types=['fact_retracted'],
        )
        self.assertEqual(changes['items'][0]['eventType'], 'fact_retracted')

    async def test_fact_mutation_uses_subject_lease(self):
        bootstrap = await self.service.ensure_self_profile('user-lease')
        subject_id = bootstrap.subject.subject_id
        self._add_fact(
            user_id='user-lease',
            subject_id=subject_id,
            fact_id='fact-lease',
        )
        self.lease_manager.available = False

        with self.assertRaisesRegex(
            HealthProfileConflictError,
            '正在执行其他修改',
        ):
            await self.service.resolve_fact(
                'user-lease',
                subject_id,
                'fact-lease',
                'confirm',
                expected_fact_version=1,
                expected_profile_version=1,
                request_id='request-lease',
            )

        self.lease_manager.available = True
        confirmed = await self.service.resolve_fact(
            'user-lease',
            subject_id,
            'fact-lease',
            'confirm',
            expected_fact_version=1,
            expected_profile_version=1,
            request_id='request-lease',
        )

        thread_id = f'health-profile:user-lease:{subject_id}'
        self.assertEqual(
            self.lease_manager.acquire_calls,
            [
                (thread_id, 'request-lease'),
                (thread_id, 'request-lease'),
            ],
        )
        self.assertEqual(len(self.lease_manager.release_calls), 1)
        self.assertEqual(confirmed['fact']['status'], 'active')


if __name__ == '__main__':
    unittest.main()
