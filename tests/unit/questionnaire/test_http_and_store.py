"""HTTP boundary, persistence scoping and legacy-workflow collision contracts."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest
import tornado.web
from tornado.testing import AsyncHTTPTestCase

from adapters.persistence.questionnaires import MongoQuestionnaireStore
from modules.questionnaire.schemas import QuestionnaireConflictError
from modules.questionnaire.schemas import QuestionnaireNotFoundError
from modules.questionnaire.schemas import QuestionnaireValidationError
from modules.record.application.creation import QACreationCoordinator
from modules.record.application.service import QASessionConflictError
from modules.record.application.service import QASessionManager
from web.handlers.questionnaire import QuestionnaireAnswersHandler
from web.handlers.questionnaire import QuestionnaireCollectionHandler
from web.handlers.questionnaire import QuestionnaireFactorAssessmentHandler


class TestQuestionnaireHTTP(AsyncHTTPTestCase):
    def get_app(self):
        self.service = SimpleNamespace(
            create=AsyncMock(return_value={'status': 'queued', 'questionnaireId': 'q'}),
            get=AsyncMock(return_value={'status': 'ready', 'questionnaireId': 'q'}),
            submit=AsyncMock(return_value={'status': 'submitted', 'answers': {}}),
            get_factor_assessment=AsyncMock(return_value={'status': 'queued', 'result': None}),
        )
        return tornado.web.Application(
            [
                (r'/api/v1/record/questionnaires', QuestionnaireCollectionHandler, {'service': self.service}),
                (
                    r'/api/v1/record/questionnaires/([^/]+)/answers',
                    QuestionnaireAnswersHandler,
                    {'service': self.service},
                ),
                (
                    r'/api/v1/record/questionnaires/([^/]+)/factor-assessment',
                    QuestionnaireFactorAssessmentHandler,
                    {'service': self.service},
                ),
            ]
        )

    def post(self, path, body):
        return self.fetch(path, method='POST', body=json.dumps(body), headers={'Content-Type': 'application/json'})

    def test_create_and_restore_contract(self):
        response = self.post('/api/v1/record/questionnaires', {'userId': 'u', 'sessionId': 's'})
        assert response.code == 202
        assert response.headers['Cache-Control'] == 'no-store'
        self.service.create.assert_awaited_once_with('u', 's', file_list=None)
        response = self.fetch('/api/v1/record/questionnaires?userId=u&sessionId=s')
        assert response.code == 200
        self.service.get.assert_awaited_once_with('u', 's')

    def test_submit_body_contains_only_answers_identity_in_path_and_query(self):
        response = self.post('/api/v1/record/questionnaires/q/answers?userId=u', {'answers': {}})
        assert response.code == 200
        self.service.submit.assert_awaited_once_with('u', 'q', {})
        response = self.post('/api/v1/record/questionnaires/q/answers?userId=u', {'answers': {}, 'userId': 'other'})
        assert response.code == 400
        assert self.service.submit.await_count == 1

    def test_malformed_json_and_schema_fail_before_service(self):
        for raw in ('[]', '{"answers": NaN}', 'null', '{'):
            response = self.fetch(
                '/api/v1/record/questionnaires/q/answers?userId=u',
                method='POST',
                body=raw,
                headers={'Content-Type': 'application/json'},
            )
            assert response.code == 400
        self.service.submit.assert_not_awaited()

    def test_explicit_error_mapping(self):
        for error, code in (
            (QuestionnaireNotFoundError('missing'), 404),
            (QuestionnaireConflictError('different answers'), 409),
            (QuestionnaireValidationError('invalid option'), 400),
        ):
            self.service.submit.side_effect = error
            response = self.post('/api/v1/record/questionnaires/q/answers?userId=u', {'answers': {}})
            assert response.code == code

    def test_uninitialized_service_still_reports_real_unavailability(self):
        def uninitialized(handler, service):
            handler.questionnaire_service = None

        with patch.object(QuestionnaireCollectionHandler, 'initialize', uninitialized):
            response = self.post('/api/v1/record/questionnaires', {'userId': 'u', 'sessionId': 's'})
        assert response.code == 503
        self.service.create.assert_not_awaited()

    def test_health_bodies_skip_preview_hooks_but_keep_access_metadata(self):
        marker = 'private synthetic report and answer content'
        self.service.submit.return_value = {'status': 'submitted', 'answers': {'note': marker}}
        logged = []
        self._app.settings['log_function'] = lambda handler: logged.append(
            {
                'code': handler._http_log_code,
                'uuid': handler.request_uuid,
                'parts': handler._http_log_parts,
                'elapsed': handler.request.request_time(),
            }
        )
        with (
            patch('web.lifecycle.log_request_start') as request_preview,
            patch(
                'web.lifecycle.capture_response',
            ) as response_preview,
        ):
            response = self.post('/api/v1/record/questionnaires/q/answers?userId=u', {'answers': {'note': marker}})
        assert response.code == 200
        request_preview.assert_not_called()
        response_preview.assert_not_called()
        assert logged[0]['code'] == 200 and logged[0]['uuid'] and logged[0]['elapsed'] >= 0
        assert logged[0]['parts'] == []
        assert marker not in repr(logged)

    def test_identity_is_required(self):
        assert self.fetch('/api/v1/record/questionnaires?userId=u').code == 400
        assert self.post('/api/v1/record/questionnaires/q/answers', {'answers': {}}).code == 400
        assert self.post('/api/v1/record/questionnaires', {'userId': 'u'}).code == 400

    def test_factor_assessment_read_status_and_cache_contract(self):
        for status in ('queued', 'running', 'completed', 'failed', 'unavailable'):
            self.service.get_factor_assessment.return_value = {'status': status, 'result': None}
            response = self.fetch('/api/v1/record/questionnaires/q/factor-assessment?userId=u')
            assert response.code == (202 if status in ('queued', 'running') else 200)
            assert response.headers['Cache-Control'] == 'no-store'
            assert json.loads(response.body)['data']['status'] == status
            self.service.get_factor_assessment.assert_awaited_with('u', 'q')
        self.service.submit.assert_not_awaited()
        self.service.create.assert_not_awaited()

    def test_factor_assessment_read_requires_owner_and_submission(self):
        assert self.fetch('/api/v1/record/questionnaires/q/factor-assessment').code == 400
        for error, status in ((QuestionnaireNotFoundError('missing'), 404), (QuestionnaireConflictError('ready'), 409)):
            self.service.get_factor_assessment.side_effect = error
            response = self.fetch('/api/v1/record/questionnaires/q/factor-assessment?userId=u')
            assert response.code == status
        assert self.post('/api/v1/record/questionnaires/q/factor-assessment?userId=u', {}).code == 405


def collection():
    return SimpleNamespace(
        create_index=AsyncMock(),
        find_one=AsyncMock(),
        insert_one=AsyncMock(return_value=SimpleNamespace(acknowledged=True)),
        replace_one=AsyncMock(return_value=SimpleNamespace(acknowledged=True, matched_count=1)),
        update_one=AsyncMock(return_value=SimpleNamespace(acknowledged=True, matched_count=1)),
        find=Mock(),
    )


@pytest.mark.asyncio
async def test_store_uses_owner_scoped_filters_and_no_ttl_deletion():
    coll = collection()
    store = MongoQuestionnaireStore(coll)
    coll.find_one.return_value = None
    await store.get_by_id('user', 'qid')
    assert coll.find_one.call_args.args[0] == {'userId': 'user', 'questionnaireId': 'qid'}
    await store.get_submitted('user', 'qid')
    assert coll.find_one.call_args.args[0] == {
        'userId': 'user',
        'questionnaireId': 'qid',
        'status': 'submitted',
    }
    await store.get_current('user', 'session')
    assert coll.find_one.call_args.args[0] == {'userId': 'user', 'sessionId': 'session'}
    coll.create_index.assert_not_awaited()
    await store.ensure_indexes()
    assert all('expireAfterSeconds' not in call.kwargs for call in coll.create_index.call_args_list)
    assert any(call.kwargs.get('unique') for call in coll.create_index.call_args_list)


@pytest.mark.asyncio
async def test_store_compare_save_does_not_upsert_and_cache_failure_is_not_state_loss():
    coll = collection()
    cache = SimpleNamespace(setex=AsyncMock(side_effect=RuntimeError('offline')), delete=AsyncMock())
    store = MongoQuestionnaireStore(coll, cache=cache, clock=lambda: 100)
    snapshot = {
        'userId': 'u',
        'questionnaireId': 'q',
        'revision': 'new',
        'status': 'ready',
        'expiresAt': 200,
    }
    assert await store.save(snapshot, expected_revision='old')
    assert coll.replace_one.call_args.args[0] == {
        'userId': 'u',
        'questionnaireId': 'q',
        'revision': 'old',
    }
    assert coll.replace_one.call_args.kwargs == {'upsert': False}
    assert cache.setex.call_args.args[1] == 100


@pytest.mark.asyncio
async def test_cache_never_replaces_current_mongo_revision_or_extends_expiry():
    coll = collection()
    snapshot = {
        'userId': 'u',
        'questionnaireId': 'q',
        'revision': 'new',
        'status': 'ready',
        'expiresAt': 200,
    }
    coll.find_one.return_value = snapshot
    stale = dict(snapshot, revision='old', status='submitted')
    cache = SimpleNamespace(get=AsyncMock(return_value=json.dumps(stale)), setex=AsyncMock())
    store = MongoQuestionnaireStore(coll, cache=cache, clock=lambda: 100)
    assert await store.get_by_id('u', 'q') == snapshot
    cache.setex.assert_not_awaited()
    cache.get.side_effect = RuntimeError('cache unavailable')
    assert await store.get_by_id('u', 'q') == snapshot
    cache.setex.assert_not_awaited()


@pytest.mark.asyncio
async def test_factor_source_read_never_writes_indexes_or_reads_cache():
    coll = collection()
    cache = SimpleNamespace(get=AsyncMock())
    store = MongoQuestionnaireStore(coll, cache=cache)
    coll.find_one.return_value = {'status': 'submitted', 'userId': 'u', 'questionnaireId': 'q'}
    assert (await store.get_for_factor_assessment('u', 'q'))['status'] == 'submitted'
    coll.create_index.assert_not_awaited()
    cache.get.assert_not_awaited()
    assert coll.find_one.call_args.args[0] == {'userId': 'u', 'questionnaireId': 'q'}


@pytest.mark.asyncio
async def test_factor_outbox_mark_is_scoped_partial_write_and_advances_revision():
    coll = collection()
    store = MongoQuestionnaireStore(coll)
    assert await store.mark_factor_assessment_queued('u', 'q', 'a')
    query, update = coll.update_one.call_args.args
    assert query == {'userId': 'u', 'questionnaireId': 'q', 'status': 'submitted', 'factorAssessment.assessmentId': 'a'}
    assert update['$set']['factorAssessment.status'] == 'queued'
    assert update['$set']['revision']
    assert 'submissionResponse' not in repr(update) and 'profileIngestion' not in repr(update)


@pytest.mark.asyncio
async def test_both_legacy_creation_paths_check_durable_questionnaire_ownership_inside_lease():
    from modules.record.agent import RecordAgent

    manager = object.__new__(QASessionManager)
    manager.record_agent = RecordAgent(manager)
    held = [False]

    async def has_session(user_id, session_id):
        assert held[0], 'ownership check must be inside the business-session lease'
        assert (user_id, session_id) == ('u', 's')
        return True

    @asynccontextmanager
    async def lease(user_id, session_id):
        held[0] = True
        try:
            yield AsyncMock()
        finally:
            held[0] = False

    manager._questionnaire_gate = SimpleNamespace(hasSession=has_session)
    manager._load_session = AsyncMock(return_value=None)
    manager._sessionLease = lease
    creation_store = SimpleNamespace(compare_save=AsyncMock())
    coordinator = QACreationCoordinator(manager, creation_store)
    with pytest.raises(QASessionConflictError, match='whole-questionnaire'):
        await coordinator.create('u', 's', {})
    creation_store.compare_save.assert_not_awaited()
    async with lease('u', 's') as guard:
        with pytest.raises(QASessionConflictError, match='whole-questionnaire'):
            await manager._createSessionUnderLease('u', 's', lease_guard=guard)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'files',
    [
        [],
        [{'url': 'https://example.invalid/report.pdf'}],
        [{'url': 'https://example.invalid/report.pdf', 'fileType': 'pdf'}],
        [{'url': 'https://example.invalid/report.png', 'fileType': 'image'}],
        [{'documentId': 'owned-document'}],
    ],
)
async def test_public_file_reference_contract_reaches_real_stager(files):
    from adapters.questionnaire_documents import QuestionnaireDocumentStager
    from modules.questionnaire.schemas import normalize_file_list

    stager = QuestionnaireDocumentStager(
        interpreter=None,
        archive=SimpleNamespace(),
        downloader=SimpleNamespace(download=AsyncMock()),
    )
    stager._stage_one = AsyncMock(return_value=({'status': 'complete'}, {'artifactId': 'a'}))
    accepted = normalize_file_list(files)
    result = await stager.stage(user_id='u', questionnaire_id='q', file_list=accepted)
    assert len(result['reports']) == len(files)
    assert stager._stage_one.await_count == len(files)
    for index, call in enumerate(stager._stage_one.call_args_list):
        assert call.args == ('u', 'q', index, files[index])


@pytest.mark.parametrize(
    'item',
    [
        {'url': 'https://example.invalid/file', 'fileId': 'unexpected'},
        {'url': 'https://example.invalid/file', 'fileName': 'unexpected'},
        {'url': 'https://example.invalid/file', 'name': 'unexpected'},
        {'documentId': 'owned-document', 'fileType': 'pdf'},
        {'url': 'https://example.invalid/file', 'fileType': None},
        {'url': 'file:///local/report.pdf'},
        {'url': 'https://example.invalid/file', 'documentId': 'both'},
    ],
)
def test_unsupported_file_metadata_fails_at_public_boundary(item):
    from modules.questionnaire.schemas import normalize_file_list

    with pytest.raises(QuestionnaireValidationError):
        normalize_file_list([item])


@pytest.mark.asyncio
async def test_production_owner_write_filters_type_questionnaire_id_and_revision():
    from adapters.persistence.sessions import MongoRecordSessionRepository

    coll = collection()
    repository = MongoRecordSessionRepository(coll)
    expected = {'type': 'questionnaire', 'questionnaireId': 'old-q', 'ownerRevision': 'old-owner'}
    marker = {
        'userId': 'u',
        'sessionId': 's',
        'type': 'questionnaire',
        'questionnaireId': 'new-q',
        'ownerRevision': 'new-owner',
    }
    assert await repository.save_questionnaire_owner('u', 's', marker, expected=expected)
    assert coll.replace_one.call_args.args[0] == {
        'userId': 'u',
        'sessionId': 's',
        'type': 'questionnaire',
        'questionnaireId': 'old-q',
        'ownerRevision': 'old-owner',
    }
    assert coll.replace_one.call_args.kwargs == {'upsert': False}
    coll.replace_one.return_value.matched_count = 0
    assert not await repository.save_questionnaire_owner('u', 's', marker, expected=expected)
    calls = coll.replace_one.await_count
    assert not await repository.save_questionnaire_owner('u', 's', marker, expected={'type': 'chat'})
    assert coll.replace_one.await_count == calls


@pytest.mark.asyncio
async def test_first_owner_claim_is_insert_only_with_unique_business_identity():
    from pymongo.errors import DuplicateKeyError

    from adapters.persistence.sessions import MongoRecordSessionRepository

    coll = collection()
    repository = MongoRecordSessionRepository(coll)
    marker = {'userId': 'u', 'sessionId': 's', 'type': 'questionnaire', 'questionnaireId': 'q'}
    assert await repository.save_questionnaire_owner('u', 's', marker, expected=None)
    assert coll.create_index.call_args.args[0] == [('userId', 1), ('sessionId', 1)]
    assert coll.create_index.call_args.kwargs['unique'] is True
    assert coll.create_index.call_args.kwargs['partialFilterExpression'] == {
        'userId': {'$type': 'string'},
        'sessionId': {'$type': 'string'},
    }
    coll.replace_one.assert_not_awaited()
    coll.insert_one.side_effect = DuplicateKeyError('new owner already claimed this identity')
    assert not await repository.save_questionnaire_owner('u', 's', marker, expected=None)
    coll.replace_one.assert_not_awaited()
