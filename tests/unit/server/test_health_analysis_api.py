"""Real HTTP transport with synthetic dependencies; no deployment credentials."""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import tornado.web
from pymongo.errors import WriteConcernError
from tornado.httpclient import HTTPRequest
from tornado.testing import AsyncHTTPTestCase
from tornado.testing import gen_test

from modules.health_analysis.basic_info_sync import BasicInfoConflictError
from modules.health_analysis.basic_info_sync import BasicInfoDeliveryError
from modules.health_analysis.contracts import AnalysisBusyError
from modules.health_analysis.contracts import AnalysisCapacityError
from modules.health_analysis.contracts import AnalysisGenerationError
from modules.health_analysis.contracts import AnalysisGenerationFailure
from modules.health_analysis.contracts import AnalysisGenerationStage
from modules.health_analysis.contracts import AnalysisLimits
from modules.health_analysis.contracts import AnalysisTimeoutError
from modules.health_analysis.contracts import InvalidAnalysisInputError
from modules.health_analysis.contracts import TraceDeliveryError
from runtime import ApplicationLifecycle
from tests.unit._support.health_analysis import payload as valid_payload
from web import http_logging
from web.handlers.base import log_tornado_request
from web.handlers.health_analysis import HealthAnalysisHandler

SENTINEL = 'synthetic-sensitive-health-value'
RESULT = {
    'healthReview': SENTINEL,
    'suggestedMaterials': '',
    'advancedQuestions': '',
    'advancedSurveySchema': None,
}


class _Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class _Service:
    def __init__(self):
        self.calls = []
        self.error = None
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.wait = False
        self.limits = None
        self.deadlines = []

    async def generate(self, payload, *, request_uuid, deadline_at=None):
        self.calls.append((payload, request_uuid))
        self.deadlines.append(deadline_at)
        if self.error:
            raise self.error
        if self.wait:
            self.started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return RESULT


class HealthAnalysisApiTest(AsyncHTTPTestCase):
    def setUp(self):
        self.records = _Records()
        self.logger = logging.Logger('health-analysis-transport', logging.DEBUG)
        self.logger.addHandler(self.records)
        for item in (
            patch.object(http_logging, 'logger', self.logger),
            patch('web.handlers.base.logger', self.logger),
            patch('web.handlers.health_analysis.logger', self.logger),
            patch.object(http_logging.env, 'HTTP_LOG_BODY_ENABLED', True),
        ):
            item.start()
            self.addCleanup(item.stop)
        super().setUp()

    def get_app(self):
        self.service = _Service()
        self.lifecycle = ApplicationLifecycle()
        self.lifecycle.mark_ready()
        return tornado.web.Application(
            [
                ('/analysis', HealthAnalysisHandler, {'service': self.service}),
                ('/missing', HealthAnalysisHandler, {'service': None}),
            ],
            lifecycle=self.lifecycle,
            log_function=log_tornado_request,
        )

    def post(self, payload=None, *, raw=None, path='/analysis', content_type='application/json'):
        if raw is None:
            raw = json.dumps(payload or valid_payload(), ensure_ascii=False)
        return self.fetch(path, method='POST', headers={'Content-Type': content_type}, body=raw)

    def assert_no_health_log(self):
        messages = '\n'.join(self.records.messages)
        self.assertNotIn(SENTINEL, messages)
        self.assertNotIn('test-user', messages)
        self.assertIn('request_uuid=', messages)
        self.assertIn('status_code=', messages)

    def test_synchronous_four_field_result_and_no_store(self):
        response = self.post({**valid_payload(), 'name': SENTINEL, 'surveyList': [SENTINEL]})
        payload = json.loads(response.body)
        self.assertEqual(response.code, 200)
        self.assertEqual(payload['code'], 200)
        self.assertEqual(payload['data'], RESULT)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.service.calls[0][1], payload['uuid'])
        self.assertIsInstance(self.service.deadlines[0], float)
        self.assert_no_health_log()

    def test_only_six_fields_are_accepted_without_caller_context(self):
        response = self.post({**valid_payload(), 'authorization': SENTINEL})
        self.assertEqual(response.code, 400)
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.post().code, 200)
        self.assertEqual(len(self.service.calls[-1]), 2)

    def test_safe_error_mapping_preserves_status_and_never_echoes_dependency_values(self):
        cases = [
            (BasicInfoConflictError(SENTINEL), 409),
            (BasicInfoDeliveryError(SENTINEL), 503),
            (InvalidAnalysisInputError(SENTINEL), 400),
            (AnalysisCapacityError(SENTINEL), 413),
            (AnalysisBusyError(SENTINEL), 503),
            (AnalysisGenerationError(SENTINEL), 502),
            (TraceDeliveryError(SENTINEL), 503),
            (AnalysisTimeoutError(SENTINEL), 504),
            (TypeError(SENTINEL), 500),
            (tornado.web.HTTPError(400, log_message=SENTINEL), 400),
            (tornado.web.HTTPError(403, log_message=SENTINEL), 403),
            (tornado.web.HTTPError(502, log_message=SENTINEL), 502),
        ]
        for error, status in cases:
            with self.subTest(status=status, error=type(error).__name__):
                self.service.error = error
                response = self.post()
                payload = json.loads(response.body)
                self.assertEqual((response.code, payload['code']), (status, status))
                self.assertIsNone(payload['data'])
                self.assertNotIn(SENTINEL, response.body.decode())
        self.assert_no_health_log()

    def test_generation_failure_logs_safe_diagnostics_with_response_uuid(self):
        self.service.error = AnalysisGenerationError(
            SENTINEL,
            stage=AnalysisGenerationStage.REVIEW,
            reason=AnalysisGenerationFailure.SURVEY_REVIEW_EXHAUSTED,
        )

        response = self.post()
        payload = json.loads(response.body)

        self.assertEqual((response.code, payload['code']), (502, 502))
        self.assertEqual(payload['message'], '健康分析生成暂不可用')
        self.assertIsNone(payload['data'])
        self.assertIn(
            f'Health Analysis generation failed: request_uuid={payload["uuid"]}, '
            'stage=review, reason=survey_review_exhausted',
            self.records.messages,
        )
        self.assertNotIn(SENTINEL, response.body.decode())
        self.assertNotIn('survey_review_exhausted', response.body.decode())
        self.assert_no_health_log()

    def test_illegal_json_nonfinite_duplicate_keys_and_wrong_content_type_do_not_reach_service(self):
        for raw in ('{' + SENTINEL, '{"x":NaN}', '{"x":1,"x":2}', '[]', 'null'):
            self.assertEqual(self.post(raw=raw).code, 400)
        self.assertEqual(self.post(raw=SENTINEL, content_type='text/plain').code, 400)
        self.assertEqual(self.service.calls, [])
        self.assert_no_health_log()

    def test_large_body_never_uses_raw_preview_fallback(self):
        raw = SENTINEL + 'x' * HealthAnalysisHandler.max_body_bytes
        response = self.post(raw=raw)
        self.assertEqual(response.code, 413)
        self.assertEqual(self.service.calls, [])
        self.assert_no_health_log()

    def test_missing_service_is_unavailable(self):
        self.assertEqual(self.post(path='/missing').code, 503)
        self.assertEqual(self.service.calls, [])

    def test_service_wait_is_inside_total_deadline_and_cancelled(self):
        self.service.limits = AnalysisLimits(
            total_timeout_seconds=0.05, source_timeout_seconds=0.01, trace_timeout_seconds=0.01
        )
        self.service.wait = True
        response = self.post()
        self.assertEqual(response.code, 504)
        self.assertTrue(self.service.cancelled.is_set())

    def test_unexpected_service_timeout_is_not_reclassified_as_total_deadline(self):
        self.service.error = TimeoutError(SENTINEL)
        self.assertEqual(self.post().code, 500)
        self.assert_no_health_log()

    def test_draining_refuses_before_service(self):
        self.lifecycle.begin_draining()
        response = self.post()
        self.assertEqual(response.code, 503)
        self.assertEqual(response.headers['Retry-After'], '5')
        self.assertEqual(self.service.calls, [])

    @gen_test
    async def test_disconnect_cancels_inflight_work_and_releases_request_lease(self):
        self.service.wait = True
        request = HTTPRequest(
            self.get_url('/analysis'),
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps(valid_payload()),
            request_timeout=0.1,
        )
        pending = self.http_client.fetch(request, raise_error=False)
        await asyncio.wait_for(self.service.started.wait(), 1)
        with self.assertRaises(Exception):
            await pending
        await asyncio.wait_for(self.service.cancelled.wait(), 1)
        self.assertEqual(self.lifecycle.snapshot().active_requests, 0)


class HealthAnalysisSharedProfileHttpTest(AsyncHTTPTestCase):
    def get_app(self):
        from adapters.health_analysis.trace import MongoRunTraceWriter
        from bootstrap.health_analysis import build_health_analysis_confirmation_port
        from bootstrap.health_analysis import build_health_analysis_service
        from health_profile import HealthProfileService
        from health_profile import MongoHealthProfileRepository
        from tests.unit._support.current_document_profile import ProfileBsonCollection
        from tests.unit.report.durable_fakes import Collection
        from web.handlers.health_profile import HealthProfileGetHandler

        collections = {
            key: ProfileBsonCollection() for key in ('subjects', 'facts', 'events', 'views', 'jobs', 'basic_info')
        }
        self.repository = MongoHealthProfileRepository(**collections)
        self.confirmation = self.repository.basic_info
        profile = HealthProfileService(self.repository)

        class TraceCollection(Collection):
            def with_options(self, **kwargs):
                return self

        self.traces = TraceCollection()
        self.model, knowledge = AsyncMock(), AsyncMock()
        self.model.complete.side_effect = AssertionError('a question-only request cannot call the model')
        knowledge.search.side_effect = AssertionError('a question-only request cannot search knowledge')
        self.source = AsyncMock()
        self.source.read.return_value = {'materials': [], 'status': 'empty'}
        self.service = build_health_analysis_service(
            SimpleNamespace(llm_gateway=self.model, knowledge_gateway=knowledge),
            basic_info_sync=build_health_analysis_confirmation_port(self.confirmation),
            profile_reader=self.source,
            chat_reader=self.source,
            trace_writer=MongoRunTraceWriter(self.traces),
        )
        lifecycle = ApplicationLifecycle()
        lifecycle.mark_ready()
        return tornado.web.Application(
            [
                ('/api/v1/health-analysis', HealthAnalysisHandler, {'service': self.service}),
                ('/api/v1/health-profile/get', HealthProfileGetHandler, {'service': profile}),
            ],
            lifecycle=lifecycle,
        )

    @gen_test
    async def test_confirmed_demographics_flow_from_analysis_into_actual_profile_query(self):
        headers = {'Content-Type': 'application/json'}

        async def post(path, payload):
            response = await self.http_client.fetch(
                self.get_url(path), method='POST', headers=headers, body=json.dumps(payload), raise_error=False
            )
            self.assertEqual(response.code, 200, response.body)
            return json.loads(response.body)['data']

        user = 'synthetic-health-analysis-http'
        first = {**valid_payload(), 'userId': user, 'surveyList': []}
        result = await post('/api/v1/health-analysis', first)
        self.assertEqual(set(result), set(RESULT))
        self.assertEqual(result['healthReview'], '')
        self.assertEqual(result['suggestedMaterials'], '')
        self.assertEqual(result['advancedQuestions'], '')
        self.assertTrue(result['advancedSurveySchema'])
        await post(
            '/api/v1/health-analysis',
            {**first, 'name': 'Synthetic Updated', 'age': 43, 'gender': '女'},
        )
        profile = await post('/api/v1/health-profile/get', {'userId': user})
        fields = profile['basicInfo']
        self.assertEqual(fields['age']['value'], 43)
        self.assertEqual(fields['age']['ageBasis'], 'confirmed_at_time')
        self.assertEqual(fields['age']['sourceReliability'], 'very_high')
        self.assertEqual(fields['name']['value'], 'Synthetic Updated')
        self.assertEqual(fields['gender']['value'], 'female')
        self.assertEqual(profile['overview']['age'], 43)
        self.assertEqual(
            [row['record']['confirmedBasicInfo']['age'] for row in self.traces.documents.values()], [42, 43]
        )

    async def _post_analysis(self):
        return await self.http_client.fetch(
            self.get_url('/api/v1/health-analysis'),
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps({**valid_payload(), 'surveyList': []}),
            raise_error=False,
        )

    @gen_test
    async def test_write_concern_without_original_receipt_returns_503_before_analysis(self):
        error = WriteConcernError('synthetic registration acknowledgement failure')
        with (
            patch.object(self.confirmation, 'register', side_effect=error) as register,
            patch.object(self.confirmation, 'get', wraps=self.confirmation.get) as get,
            patch.object(self.confirmation, 'apply', wraps=self.confirmation.apply) as apply,
        ):
            response = await self._post_analysis()

        self.assertEqual(response.code, 503, response.body)
        self.assertEqual(response.headers['Retry-After'], '5')
        get.assert_awaited_once_with(*register.await_args.args)
        apply.assert_not_awaited()
        self.source.read.assert_not_awaited()
        self.model.complete.assert_not_awaited()
        self.assertFalse(self.traces.documents)

    @gen_test
    async def test_write_concern_recovers_registered_command_from_original_receipt(self):
        register_command = self.confirmation.register

        async def register(*args, **kwargs):
            await register_command(*args, **kwargs)
            raise WriteConcernError('synthetic acknowledgement lost after registration')

        with (
            patch.object(self.confirmation, 'register', side_effect=register) as first,
            patch.object(self.confirmation, 'get', wraps=self.confirmation.get) as get,
        ):
            response = await self._post_analysis()

        self.assertEqual(response.code, 200, response.body)
        get.assert_awaited_once_with(*first.await_args.args)
        row = next(iter(self.traces.documents.values()))['record']
        self.assertEqual(row['basicInfoSync']['receipt']['operationId'], first.await_args.args[1])
        self.assertEqual(row['basicInfoSync']['applicationAttempt'], 'completed')

    @gen_test
    async def test_write_concern_during_apply_preserves_pending_command_for_compensation(self):
        from modules.health_analysis.basic_info_sync import BasicInfoCompensationWorker

        archives = self._use_real_report_reader(
            list_archived=AsyncMock(side_effect=AssertionError('an unmaterialized self has no archives'))
        )
        with patch.object(self.confirmation, 'apply', side_effect=WriteConcernError('synthetic apply failure')):
            response = await self._post_analysis()
        archives.list_archived.assert_not_awaited()

        self.assertEqual(response.code, 200, response.body)
        row = next(iter(self.traces.documents.values()))['record']
        sync = row['basicInfoSync']
        self.assertEqual(sync['applicationAttempt'], 'deferred')
        original = sync['receipt']
        await BasicInfoCompensationWorker(self.service.basic_info_sync.port).run_once()
        receipt = await self.confirmation.get(original['userId'], original['operationId'])
        self.assertEqual(receipt.command_id, original['commandId'])
        self.assertEqual(receipt.status, 'applied')
        self.model.complete.assert_not_awaited()
        self.assertEqual(len(self.traces.documents), 1)

    async def _archive_confirmed_profile(self, user_id):
        from health_profile.models import SubjectStatus

        subject = await self.repository.get_self_subject(user_id)
        await self.repository.update_subject(
            subject.model_copy(update={'status': SubjectStatus.ARCHIVED}), expected_version=subject.version
        )

    def _use_real_report_reader(self, *, list_archived):
        from adapters.health_analysis.profile import ProfileMaterialsAdapter
        from health_profile.report_materials import ProfileReportMaterialsReader
        from health_profile.snapshots import ProfileSnapshotReader

        archives = SimpleNamespace(list_archived=list_archived)
        self.service.profile_reader = ProfileMaterialsAdapter(
            ProfileReportMaterialsReader(profiles=ProfileSnapshotReader(self.repository), archives=archives)
        )
        return archives

    async def _check_fixed_profile_conflict(self, *, before_read):
        from health_profile.document_archive_contracts import ProfileDocumentArchivePage

        apply = self.confirmation.apply

        async def apply_confirmation(user_id, command_id):
            receipt = await apply(user_id, command_id)
            if before_read:
                await self._archive_confirmed_profile(user_id)
            return receipt

        async def list_archived(*, user_id, **kwargs):
            await self._archive_confirmed_profile(user_id)
            return ProfileDocumentArchivePage(())

        archives = self._use_real_report_reader(list_archived=AsyncMock(side_effect=list_archived))
        with patch.object(self.confirmation, 'apply', side_effect=apply_confirmation):
            response = await self._post_analysis()

        self.assertEqual(response.code, 409, response.body)
        self.assertEqual(archives.list_archived.await_count, 0 if before_read else 1)
        self.model.complete.assert_not_awaited()
        self.assertFalse(self.traces.documents)

    @gen_test
    async def test_fixed_profile_archived_before_report_read_is_conflict(self):
        await self._check_fixed_profile_conflict(before_read=True)

    @gen_test
    async def test_fixed_profile_archived_during_report_read_is_conflict(self):
        await self._check_fixed_profile_conflict(before_read=False)

    @gen_test
    async def test_transient_report_read_failure_still_returns_base_result(self):
        from health_profile.document_archive_contracts import ProfileDocumentArchiveReadError

        archives = self._use_real_report_reader(
            list_archived=AsyncMock(side_effect=ProfileDocumentArchiveReadError('synthetic temporary archive outage'))
        )
        response = await self._post_analysis()

        self.assertEqual(response.code, 200, response.body)
        archives.list_archived.assert_awaited_once()
        row = next(iter(self.traces.documents.values()))['record']
        self.assertEqual(row['sourceSnapshots']['profile']['status'], 'unavailable')
        self.assertEqual(json.loads(response.body)['data']['healthReview'], '')
