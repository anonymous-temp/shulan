"""HTTP facts cursors bind the actual current revision and subject lifecycle."""

from tornado.testing import AsyncHTTPTestCase
from tornado.testing import gen_test

from health_profile.models import FactCategory
from health_profile.models import FactStatus
from health_profile.models import SubjectStatus
from health_profile.pagination import decode_cursor
from health_profile.pagination import encode_cursor
from tests.unit.health_profile.test_basic_info import values
from tests.unit.server import test_profile_basic_info_api as basic_http


class ProfileCurrentCursorHttpTest(AsyncHTTPTestCase):
    get_app = basic_http.ProfileBasicInfoHttpTest.get_app
    _post = basic_http.ProfileBasicInfoHttpTest._post
    _fact = basic_http.ProfileBasicInfoHttpTest._fact

    async def _cycle(self, user_id, sid):
        for status in (SubjectStatus.ARCHIVED, SubjectStatus.ACTIVE):
            subject = await self.repository.get_subject(user_id, sid)
            await self.repository.update_subject(
                subject.model_copy(update={'status': status, 'version': subject.version + 1}),
                expected_version=subject.version,
            )

    async def _page(self, *, restored=False):
        user_id = 'test-current-cursor'
        subject = (await self.profile.ensure_self_profile(user_id)).subject
        if restored:
            await self._cycle(user_id, subject.subject_id)
        fact = await self._fact(user_id, subject.subject_id, category=FactCategory.LABORATORY, status=FactStatus.ACTIVE)
        await self.repository.create_fact(fact.model_copy(update={'fact_id': 'test-second-clinical'}))
        request = {'userId': user_id, 'subjectId': subject.subject_id, 'limit': 1}
        code, response = await self._post('facts/list', request)
        self.assertEqual(code, 200)
        self.assertEqual(len(response['data']['items']), 1)
        cursor = response['data']['nextCursor']
        self.assertTrue(cursor)
        return request, cursor, response['data']['items'][0]['factId']

    @gen_test
    async def test_zero_revision_epoch_three_cursor_continues_without_a_change(self):
        request, cursor, first_id = await self._page(restored=True)
        binding = decode_cursor('health-profile-facts', cursor)
        self.assertEqual(binding.get('basicInfoRevision'), 0)
        self.assertEqual(binding.get('lifecycleEpoch'), 3)
        code, response = await self._post('facts/list', {**request, 'cursor': cursor})
        self.assertEqual(code, 200)
        self.assertEqual(len(response['data']['items']), 1)
        self.assertNotEqual(response['data']['items'][0]['factId'], first_id)
        self.assertIsNone(response['data']['nextCursor'])

    @gen_test
    async def test_missing_or_malformed_cursor_binding_is_422_without_guessing(self):
        request, cursor, _ = await self._page()
        binding = {**decode_cursor('health-profile-facts', cursor), 'basicInfoRevision': 0, 'lifecycleEpoch': 1}
        malformed = []
        for field in ('basicInfoRevision', 'lifecycleEpoch'):
            missing = dict(binding)
            del missing[field]
            malformed.append(missing)
            for value in (None, True, '1', -1):
                malformed.append({**binding, field: value})
        malformed.append({**binding, 'lifecycleEpoch': 0})
        for bad in malformed:
            with self.subTest(binding=bad):
                code, response = await self._post(
                    'facts/list', {**request, 'cursor': encode_cursor('health-profile-facts', bad)}
                )
                self.assertEqual(code, 422)
                self.assertIsNone(response['data'])

    @gen_test
    async def test_well_formed_cursor_from_another_lifecycle_is_409(self):
        request, cursor, _ = await self._page(restored=True)
        binding = {**decode_cursor('health-profile-facts', cursor), 'basicInfoRevision': 0, 'lifecycleEpoch': 3}
        await self._cycle(request['userId'], request['subjectId'])
        code, response = await self._post(
            'facts/list', {**request, 'cursor': encode_cursor('health-profile-facts', binding)}
        )
        self.assertEqual(code, 409)
        self.assertIsNone(response['data'])

    @gen_test
    async def test_well_formed_cursor_with_a_changed_basic_revision_is_409(self):
        request, cursor, _ = await self._page(restored=True)
        binding = {**decode_cursor('health-profile-facts', cursor), 'basicInfoRevision': 0, 'lifecycleEpoch': 3}
        command = await self.basic_info.register(
            request['userId'], 'test-new-confirmation', values=values(), confirmed_at=self.basic_info.clock()
        )
        await self.basic_info.apply(request['userId'], command.command_id)
        code, response = await self._post(
            'facts/list', {**request, 'cursor': encode_cursor('health-profile-facts', binding)}
        )
        self.assertEqual(code, 409)
        self.assertIsNone(response['data'])
