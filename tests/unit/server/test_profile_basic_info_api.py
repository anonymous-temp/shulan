"""Real Profile service and HTTP adapters with synthetic Mongo I/O."""

import json
from unittest.mock import patch

from tornado.testing import AsyncHTTPTestCase
from tornado.testing import gen_test
from tornado.web import Application

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
from health_profile.basic_info_store import BasicInfoConflict
from health_profile.basic_info_store import BasicInfoUnavailable
from health_profile.errors import HealthProfileConflictError
from health_profile.errors import HealthProfileNotFoundError
from health_profile.errors import HealthProfileValidationError
from health_profile.models import SubjectStatus
from tests.unit.health_profile.test_basic_info import NOW
from tests.unit.health_profile.test_basic_info import BsonCollection
from tests.unit.health_profile.test_basic_info import values
from web.handlers.health_profile import HealthProfileFactListHandler
from web.handlers.health_profile import HealthProfileFactResolveHandler
from web.handlers.health_profile import HealthProfileGetHandler


def test_profile_errors_keep_the_existing_public_and_compatibility_identities():
    import health_profile
    import health_profile.service as service
    import modules.health_profile as public
    import modules.health_profile.application as application
    from tests.unit.server.test_health_profile_api_contract import server

    for name, expected in (
        ('HealthProfileConflictError', HealthProfileConflictError),
        ('HealthProfileNotFoundError', HealthProfileNotFoundError),
        ('HealthProfileValidationError', HealthProfileValidationError),
    ):
        for owner in (health_profile, service, public, application, server):
            assert getattr(owner, name) is expected
    assert public.BasicInfoConflict is application.BasicInfoConflict is BasicInfoConflict
    assert issubclass(BasicInfoConflict, HealthProfileConflictError)
    assert not issubclass(BasicInfoUnavailable, HealthProfileConflictError)


class ProfileBasicInfoHttpTest(AsyncHTTPTestCase):
    def get_app(self):
        subjects, facts, events, views, jobs, basic = [BsonCollection() for _ in range(6)]
        self.repository = MongoHealthProfileRepository(
            subjects=subjects, facts=facts, events=events, views=views, jobs=jobs, basic_info=basic
        )
        self.basic_info = self.repository.basic_info
        self.basic_info.clock = lambda: NOW
        self.profile = HealthProfileService(self.repository, clock=lambda: NOW)
        return Application(
            [
                ('/api/v1/health-profile/get', HealthProfileGetHandler, {'service': self.profile}),
                ('/api/v1/health-profile/facts/list', HealthProfileFactListHandler, {'service': self.profile}),
                ('/api/v1/health-profile/facts/resolve', HealthProfileFactResolveHandler, {'service': self.profile}),
            ]
        )

    async def _post(self, path, body):
        response = await self.http_client.fetch(
            self.get_url('/api/v1/health-profile/' + path),
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps(body),
            raise_error=False,
        )
        return response.code, json.loads(response.body)

    def _assert_error(self, response, status):
        actual, payload = response
        self.assertEqual(actual, status)
        self.assertEqual(payload['code'], status)
        self.assertIsNone(payload['data'])
        self.assertTrue(payload['message'])
        self.assertEqual(set(payload), {'code', 'message', 'uuid', 'timestamp', 'elapsed_ms', 'data'})

    async def _confirmed(self, user_id):
        receipt = await self.basic_info.register(user_id, 'test-initial', values=values(), confirmed_at=NOW)
        await self.basic_info.apply(user_id, receipt.command_id)
        return await self.basic_info.read(user_id)

    @gen_test
    async def test_archived_subject_conflicts_are_409_on_both_public_queries(self):
        user_id = 'test-basic-info-archived'
        subject = (await self.profile.ensure_self_profile(user_id)).subject
        await self.repository.update_subject(
            subject.model_copy(update={'status': SubjectStatus.ARCHIVED}), expected_version=subject.version
        )
        for path in ('get', 'facts/list'):
            with self.subTest(path=path):
                self._assert_error(await self._post(path, {'userId': user_id, 'subjectId': subject.subject_id}), 409)

    @gen_test
    async def test_existing_missing_subject_and_validation_statuses_are_preserved(self):
        for path in ('get', 'facts/list'):
            with self.subTest(path=path, status=404):
                self._assert_error(
                    await self._post(path, {'userId': 'test-missing-subject', 'subjectId': 'test-no-such-subject'}), 404
                )
        for path, options in (('get', {'factStatuses': []}), ('facts/list', {'limit': 0})):
            with self.subTest(path=path, status=422):
                self._assert_error(await self._post(path, {'userId': 'test-invalid-profile', **options}), 422)

    @gen_test
    async def test_unavailable_and_unrelated_runtime_errors_do_not_become_conflicts_or_new_503s(self):
        user_id = 'test-basic-info-unavailable'
        subject = (await self.profile.ensure_self_profile(user_id)).subject
        for error in (BasicInfoUnavailable('test-store-unavailable'), RuntimeError('test-unrelated-error')):
            with self.subTest(error=type(error)), patch.object(self.basic_info.store, 'get', side_effect=error):
                self._assert_error(await self._post('get', {'userId': user_id, 'subjectId': subject.subject_id}), 500)

    @gen_test
    async def test_demographic_replacement_with_unknown_time_is_publicly_nullable(self):
        user_id = 'test-basic-info-null-time'
        current = await self._confirmed(user_id)
        fact = await self.repository.get_fact(user_id, current.subject_id, current.fields['age'].fact_id)
        view = await self.repository.get_view(user_id, current.subject_id)
        code, payload = await self._post(
            'facts/resolve',
            {
                'userId': user_id,
                'subjectId': current.subject_id,
                'factId': fact.fact_id,
                'decision': 'replace',
                'replacement': {
                    'valueType': 'quantity',
                    'value': {'number': 44, 'unit': 'years'},
                    'effectivePeriod': {'start': None, 'end': None},
                },
                'expectedFactVersion': fact.version,
                'expectedProfileVersion': view.profile_version,
                'requestId': 'test-age-correction',
            },
        )
        self.assertEqual(code, 200)
        self.assertEqual(payload['data']['fact']['effectivePeriod'], {'start': None, 'end': None})
        code, payload = await self._post(
            'facts/list',
            {
                'userId': user_id,
                'subjectId': current.subject_id,
                'statuses': ['active'],
                'factTypes': ['demographic.age'],
            },
        )
        self.assertEqual(code, 200)
        self.assertEqual(len(payload['data']['items']), 1)
        self.assertEqual(payload['data']['items'][0]['value']['number'], 44)
        self.assertEqual(payload['data']['items'][0]['effectivePeriod'], {'start': None, 'end': None})

    @gen_test
    async def test_registered_basic_info_and_summary_do_not_depend_on_fact_statuses(self):
        user_id = 'test-basic-info-schema-two'
        current = await self._confirmed(user_id)
        before = await self.repository.get_view(user_id, current.subject_id)
        await self._fact(
            user_id,
            current.subject_id,
            category=FactCategory.LABORATORY,
            status=FactStatus.PENDING,
        )
        body = {'userId': user_id, 'subjectId': current.subject_id}
        status, response = await self._post('get', body)
        self.assertEqual(status, 200)
        baseline = response['data']
        self.assertEqual(baseline['overview']['pendingConfirmationCount'], 1)
        self.assertEqual(baseline['summary']['basedOnProfileVersion'], before.profile_version)

        for fact_statuses, expected_statuses in (
            (None, {'active', 'pending'}),
            (['active'], {'active'}),
            (['pending'], {'pending'}),
            (['disputed'], set()),
            (['active', 'pending', 'disputed'], {'active', 'pending'}),
        ):
            with self.subTest(fact_statuses=fact_statuses):
                code, payload = await self._post('get', {**body, 'factStatuses': fact_statuses})
                self.assertEqual(code, 200)
                data = payload['data']
                self.assertEqual(data['schemaVersion'], 2)
                self.assertGreater(data['basicInfoRevision'], 0)
                self.assertEqual(set(data['basicInfo']), {'name', 'gender', 'age'})
                self.assertEqual(data['profileVersion'], before.profile_version)
                self.assertEqual(
                    {item['status'] for group in data['factGroups'] for item in group['items']},
                    expected_statuses,
                )
                for field in ('overview', 'summary', 'indicatorGroups', 'alerts', 'evidence'):
                    self.assertEqual(data[field], baseline[field], field)

    async def _fact(self, user_id, subject_id, *, category, status):
        fact = HealthFact(
            fact_id='test-existing-fact',
            user_id=user_id,
            subject_id=subject_id,
            fact_type='demographic.age' if category is FactCategory.DEMOGRAPHIC else 'laboratory.test_value',
            category=category,
            display_name='合成观察',
            value_type=FactValueType.QUANTITY,
            value={'number': 42, 'unit': 'years' if category is FactCategory.DEMOGRAPHIC else 'mg/L'},
            assertion_level=AssertionLevel.USER_REPORTED,
            verification_status=VerificationStatus.UNCONFIRMED,
            confidence=1,
            effective_period=EffectivePeriod(start=NOW),
            status=status,
            sources=(FactSource(source_type='manual', source_id='test-existing-source', source_revision=1),),
            created_at=NOW,
            updated_at=NOW,
        )
        await self.repository.create_fact(fact)
        return fact

    @gen_test
    async def test_legacy_profile_without_registered_basic_info_keeps_schema_one(self):
        user_id = 'test-legacy-schema-one'
        subject = (await self.profile.ensure_self_profile(user_id)).subject
        await self._fact(user_id, subject.subject_id, category=FactCategory.DEMOGRAPHIC, status=FactStatus.ACTIVE)
        code, payload = await self._post('get', {'userId': user_id, 'subjectId': subject.subject_id})
        self.assertEqual(code, 200)
        self.assertEqual(payload['data']['schemaVersion'], 1)
        self.assertNotIn('basicInfo', payload['data'])
        self.assertNotIn('basicInfoRevision', payload['data'])
        self.assertEqual(payload['data']['overview']['age'], 42)

    @gen_test
    async def test_clinical_replacement_still_requires_a_known_observation_time(self):
        user_id = 'test-clinical-time-required'
        subject = (await self.profile.ensure_self_profile(user_id)).subject
        fact = await self._fact(
            user_id, subject.subject_id, category=FactCategory.LABORATORY, status=FactStatus.PENDING
        )
        view = await self.repository.get_view(user_id, subject.subject_id)
        response = await self._post(
            'facts/resolve',
            {
                'userId': user_id,
                'subjectId': subject.subject_id,
                'factId': fact.fact_id,
                'decision': 'replace',
                'replacement': {
                    'valueType': 'quantity',
                    'value': {'number': 43, 'unit': 'mg/L'},
                    'effectivePeriod': {'start': None},
                },
                'expectedFactVersion': fact.version,
                'expectedProfileVersion': view.profile_version,
                'requestId': 'test-clinical-correction',
            },
        )
        self._assert_error(response, 422)
        unchanged = await self.repository.get_fact(user_id, subject.subject_id, fact.fact_id)
        self.assertEqual(unchanged.version, fact.version)
        self.assertEqual(unchanged.effective_period.start, NOW)
