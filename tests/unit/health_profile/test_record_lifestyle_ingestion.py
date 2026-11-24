"""Record lifestyle facts remain source-grounded and pending through profile consumption."""

from __future__ import annotations

import json
import unittest

from health_profile import AssertionLevel
from health_profile import FactStatus
from health_profile import RecordAnswerCandidateExtractor
from health_profile import RecordProfileProcessor
from health_profile import RepositoryHealthProfileChatMemoryProvider
from health_profile import VerificationStatus
from health_profile.ingestion import LIFESTYLE_FACT_LABELS
from health_profile.ingestion import FactTypeRegistry
from health_profile.projection import build_profile_projection
from tests.unit.health_profile.test_record_ingestion import FakeRecordProfileService
from tests.unit.health_profile.test_record_ingestion import FakeRecordStore
from tests.unit.health_profile.test_record_ingestion import RecordFixture
from tests.unit.health_profile.test_record_ingestion import _runtime
from tests.unit.health_profile.test_report_ingestion import FakeProfileRepository
from tests.unit.health_profile.test_report_ingestion import ReportIngestionFixture


def lifestyle_input():
    details = (
        ('diet_pattern', '近三个月每周五天喝含糖饮料', 'reported', {'frequency': '每周五天', 'duration': '近三个月'}),
        ('meal_regularity', '近两个月常不吃早餐', 'reported', {'duration': '近两个月'}),
        ('hydration', '每天喝水约800毫升', 'reported', {'frequency': '每天', 'amount': '约800毫升'}),
        (
            'physical_activity',
            '每周跑步两次，每次30分钟',
            'reported',
            {'frequency': '每周', 'amount': '两次', 'duration': '每次30分钟'},
        ),
        ('sedentary_behavior', '工作日每天坐8小时', 'reported', {'amount': '8小时'}),
        ('sleep', '近半年常在凌晨一点后入睡', 'reported', {'duration': '近半年', 'timing': '凌晨一点后'}),
        ('smoking_status', '我从不吸烟', 'denied', {}),
        ('alcohol_use', '饮酒频次记不清', 'unknown', {}),
        ('stress_coping', '忙碌时每晚散步20分钟放松', 'reported', {'frequency': '每晚', 'duration': '20分钟'}),
    )
    history, output = [], []
    for index, (suffix, evidence, status, specifics) in enumerate(details):
        history.append(
            {
                'question': {'questionId': f'q_habit_{index}', 'question': '请说说相关生活习惯'},
                'answer': evidence,
                'answeredAt': RecordFixture.now.isoformat(),
            }
        )
        output.append(
            {
                'answerIndex': index,
                'factType': f'lifestyle.{suffix}',
                'displayName': '模型给出的名称不可携带推断',
                'valueType': 'object',
                'value': {'status': status, 'description': evidence, **specifics},
                'subjectKind': 'self',
                'subjectHint': '本人',
                'evidenceText': evidence,
                'confidence': 0.99,
            }
        )
    return RecordFixture.record(qaHistory=history), output


class RecordLifestyleFactsTest(unittest.IsolatedAsyncioTestCase):
    async def test_habits_keep_explicit_details_and_pending_status_in_profile_and_chat(self):
        record, output = lifestyle_input()
        runtime, gateway = _runtime({'candidates': output})
        subject = ReportIngestionFixture.subject()
        repository = FakeProfileRepository(subject)
        sequence = 0

        def next_id(prefix):
            nonlocal sequence
            sequence += 1
            return f'{prefix}-lifestyle-{sequence}'

        processor = RecordProfileProcessor(
            store=FakeRecordStore(record),
            repository=repository,
            profile_service=FakeRecordProfileService(subject, repository=repository),
            agent_hub=runtime.hub,
            clock=lambda: RecordFixture.now,
            id_factory=next_id,
        )
        first = await processor.process(RecordFixture.job())
        replay = await processor.process(RecordFixture.job())
        habits = [fact for fact in repository.facts if fact.fact_type.startswith('lifestyle.')]
        self.assertEqual(first.fact_count, 12)
        self.assertEqual(replay.fact_count, 0)
        self.assertEqual(replay.replayed_fact_count, 12)
        self.assertEqual(len(habits), 9)
        by_type = {fact.fact_type: fact for fact in habits}
        for index, expected in enumerate(output):
            fact = by_type[expected['factType']]
            self.assertEqual(fact.value, expected['value'])
            self.assertIs(fact.status, FactStatus.PENDING)
            self.assertIs(fact.assertion_level, AssertionLevel.USER_REPORTED)
            self.assertIs(fact.verification_status, VerificationStatus.UNCONFIRMED)
            self.assertEqual(fact.confidence, 0.8)
            self.assertEqual(fact.display_name, LIFESTYLE_FACT_LABELS[fact.fact_type])
            self.assertEqual(fact.sources[0].pointer, f'/qaHistory/{index}/answer')
            self.assertEqual(fact.sources[0].source_id, record.session_id)
            self.assertIsNotNone(fact.sources[0].excerpt_hash)
        sent = json.loads(gateway.complete_requests[0].content)
        self.assertTrue(set(LIFESTYLE_FACT_LABELS).issubset(sent['allowedFactTypes']))
        projection = build_profile_projection(
            user_id='user-1',
            subject=subject,
            view=repository.view,
            facts=tuple(repository.facts),
            indicator_limit=10,
            generated_at=RecordFixture.now,
        )
        group = next(group for group in projection['factGroups'] if group['code'] == 'lifestyle')
        self.assertEqual({fact['factType'] for fact in group['items']}, set(LIFESTYLE_FACT_LABELS))
        self.assertEqual(projection['overview']['pendingConfirmationCount'], 9)
        memory = await RepositoryHealthProfileChatMemoryProvider(
            repository,
            max_pending_facts=len(LIFESTYLE_FACT_LABELS),
            max_chars=12000,
        ).build_context('user-1')
        self.assertIsNotNone(memory)
        for fact_type in LIFESTYLE_FACT_LABELS:
            self.assertIn(fact_type, memory)
        self.assertIn('"status":"denied"', memory)
        self.assertIn('"status":"unknown"', memory)
        self.assertIn('"status":"pending"', memory)

    async def test_unsupported_model_details_fall_back_to_raw_behavior_and_stable_deduplicated_identity(self):
        record, output = lifestyle_input()
        invalid_values = (
            {'status': 'reported', 'description': output[0]['evidenceText'], 'frequency': '每天'},
            {'status': 'normal', 'description': output[0]['evidenceText']},
            {'status': 'reported', 'description': '高糖饮食'},
        )
        invalid = [{**output[0], 'value': value} for value in invalid_values]
        invalid.append(
            {
                **output[0],
                'valueType': 'text',
                'value': '喝含糖饮料导致血糖升高',
            }
        )
        runtime, _ = _runtime({'candidates': invalid + [output[5]]})
        candidates = await RecordAnswerCandidateExtractor(runtime.hub).extract(
            record,
            subject_id='subject-self',
        )
        self.assertEqual(len(candidates), 2)
        by_type = {candidate.fact_type: candidate for candidate in candidates}
        behavior = by_type['lifestyle.diet_pattern']
        self.assertEqual(behavior.value_type.value, 'text')
        self.assertEqual(behavior.value, output[0]['evidenceText'])
        self.assertIs(behavior.assertion_level, AssertionLevel.USER_REPORTED)
        self.assertEqual(behavior.source.pointer, '/qaHistory/0/answer')
        self.assertEqual(by_type['lifestyle.sleep'].value, output[5]['value'])
        other_runtime, _ = _runtime({'candidates': list(reversed(invalid))})
        replay = await RecordAnswerCandidateExtractor(other_runtime.hub).extract(
            record,
            subject_id='subject-self',
        )
        self.assertEqual(len(replay), 1)
        self.assertEqual(replay[0].candidate_id, behavior.candidate_id)

    async def test_model_association_is_discarded_while_explicit_behavior_is_kept(self):
        record, output = lifestyle_input()
        expected = output[0]['value'].copy()
        output[0]['value'].update(
            {
                'cause': '报告异常的诱因',
                'possibleCause': {'association': '模型推测'},
            }
        )
        runtime, _ = _runtime({'candidates': [output[0]]})
        candidates = await RecordAnswerCandidateExtractor(runtime.hub).extract(
            record,
            subject_id='subject-self',
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].value, expected)

    async def test_even_user_causal_language_is_not_persisted_as_a_lifestyle_fact(self):
        record, output = lifestyle_input()
        evidence = '我每天喝含糖饮料，觉得这导致血糖升高'
        record.session['qaHistory'][0]['answer'] = evidence
        runtime, _ = _runtime(
            {
                'candidates': [
                    {
                        **output[0],
                        'evidenceText': evidence,
                        'valueType': 'text',
                        'value': evidence,
                    },
                    {
                        **output[0],
                        'evidenceText': '每天喝含糖饮料',
                        'valueType': 'text',
                        'value': '每天喝含糖饮料',
                    },
                ]
            }
        )
        candidates = await RecordAnswerCandidateExtractor(runtime.hub).extract(
            record,
            subject_id='subject-self',
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].value, '每天喝含糖饮料')

    async def test_explicit_structured_details_and_negation_do_not_require_a_closed_object_shape(self):
        record, output = lifestyle_input()
        chosen = [output[2], output[4], output[6], output[7]]
        chosen[0]['value']['amount'] = {'number': 800, 'unit': '毫升'}
        chosen[1]['value']['context'] = '工作日'
        chosen[2]['value']['negation'] = True
        chosen[3]['value']['uncertainty'] = '记不清'
        runtime, _ = _runtime({'candidates': chosen})
        candidates = await RecordAnswerCandidateExtractor(runtime.hub).extract(
            record,
            subject_id='subject-self',
        )
        self.assertEqual(len(candidates), 4)
        self.assertEqual([candidate.value for candidate in candidates], [item['value'] for item in chosen])

    async def test_raw_fallback_preserves_denial_unknown_and_chinese_numeric_details(self):
        record, output = lifestyle_input()
        chosen = [output[6], output[7], output[3]]
        chosen[0]['value'] = {'status': 'reported', 'description': '目前吸烟'}
        chosen[1]['value'] = {'status': 'denied', 'description': '不饮酒'}
        chosen[2]['value'] = {'status': 'reported', 'description': '每周运动2次', 'duration': '30分钟'}
        runtime, _ = _runtime({'candidates': chosen})
        candidates = await RecordAnswerCandidateExtractor(runtime.hub).extract(
            record,
            subject_id='subject-self',
        )
        self.assertEqual(len(candidates), 3)
        for candidate, item in zip(candidates, chosen):
            self.assertEqual(candidate.value_type.value, 'text')
            self.assertEqual(candidate.value, item['evidenceText'])
            self.assertIs(candidate.assertion_level, AssertionLevel.USER_REPORTED)
            self.assertEqual(candidate.confidence, 0.8)
        self.assertEqual(candidates[0].value, '我从不吸烟')
        self.assertEqual(candidates[1].value, '饮酒频次记不清')
        self.assertEqual(candidates[2].value, '每周跑步两次，每次30分钟')

    def test_registry_is_bounded_and_keeps_legacy_smoking_values(self):
        registry = FactTypeRegistry.default()
        self.assertIsNotNone(registry.get('lifestyle.sleep'))
        self.assertIsNone(registry.get('lifestyle.inferred_cause'))
        self.assertIsNone(registry.get('lifestyle.any_model_type'))
        self.assertEqual(
            {kind.value for kind in registry.require('lifestyle.smoking_status').permitted_value_types},
            {'coded', 'text', 'object'},
        )
