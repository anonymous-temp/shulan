"""Independent whole-questionnaire HTTP endpoints."""

from __future__ import annotations

import json
import uuid

import tornado.web
from loongspears.utils import time as timemark

from modules.questionnaire.schemas import QuestionnaireConflictError
from modules.questionnaire.schemas import QuestionnaireNotFoundError
from modules.questionnaire.schemas import QuestionnaireValidationError
from utils.log.context import update_log_context
from web.handlers.base import BaseHandler


def _body(handler, allowed, required):
    try:
        body = json.loads(
            handler.request.body,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Invalid JSON constant')),
        )
    except (ValueError, UnicodeDecodeError) as error:
        raise tornado.web.HTTPError(400, log_message='Request must contain standard JSON') from error
    if not isinstance(body, dict) or set(body) - set(allowed) or set(required) - set(body):
        raise tornado.web.HTTPError(400, log_message='Request has unsupported or missing fields')
    handler.bind_log_identity(body)
    return body


class QuestionnaireHandler(BaseHandler):
    def initialize(self, service):
        self.questionnaire_service = service

    def prepare(self):
        # This medical endpoint retains lifecycle admission and access metadata
        # without the legacy request-body preview hook.
        self.request_uuid = str(uuid.uuid4()).upper()
        self.request_start_time = timemark.now()
        self._log_context = update_log_context(request_uuid=self.request_uuid)
        self._request_lease = None
        lifecycle = self.application.settings.get('lifecycle')
        if lifecycle is not None:
            self._request_lease = lifecycle.try_begin_request()
            if self._request_lease is None:
                self.set_header('Retry-After', '5')
                raise tornado.web.HTTPError(503, log_message='Service is draining')

    def write(self, chunk):
        # Status-level access logs remain available; no health response preview.
        if isinstance(chunk, dict):
            self._http_log_code = chunk.get('code')
        tornado.web.RequestHandler.write(self, chunk)

    def set_default_headers(self):
        super().set_default_headers()
        self.set_header('Cache-Control', 'no-store')
        self.set_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')

    async def _invoke(self, action):
        if self.questionnaire_service is None:
            raise tornado.web.HTTPError(503, log_message='Questionnaire service is unavailable')
        try:
            result = await action()
        except QuestionnaireNotFoundError as error:
            raise tornado.web.HTTPError(404, log_message=str(error)) from error
        except QuestionnaireConflictError as error:
            raise tornado.web.HTTPError(409, log_message=str(error)) from error
        except QuestionnaireValidationError as error:
            raise tornado.web.HTTPError(400, log_message=str(error)) from error
        code = 202 if result['status'] in ('queued', 'generating', 'running') else 200
        self.set_status(code)
        self.write_response(code, 'success', result)


class QuestionnaireCollectionHandler(QuestionnaireHandler):
    async def post(self):
        body = _body(self, {'userId', 'sessionId', 'fileList'}, {'userId', 'sessionId'})
        await self._invoke(
            lambda: self.questionnaire_service.create(
                body['userId'],
                body['sessionId'],
                file_list=body.get('fileList'),
            )
        )

    async def get(self):
        user_id = self.get_query_argument('userId')
        session_id = self.get_query_argument('sessionId')
        self.bind_log_identity({'userId': user_id, 'sessionId': session_id})
        await self._invoke(lambda: self.questionnaire_service.get(user_id, session_id))


class QuestionnaireAnswersHandler(QuestionnaireHandler):
    async def post(self, questionnaire_id):
        body = _body(self, {'answers'}, {'answers'})
        user_id = self.get_query_argument('userId')
        self.bind_log_identity({'userId': user_id})
        await self._invoke(
            lambda: self.questionnaire_service.submit(
                user_id,
                questionnaire_id,
                body['answers'],
            )
        )


class QuestionnaireFactorAssessmentHandler(QuestionnaireHandler):
    async def get(self, questionnaire_id):
        user_id = self.get_query_argument('userId')
        self.bind_log_identity({'userId': user_id})
        await self._invoke(lambda: self.questionnaire_service.get_factor_assessment(user_id, questionnaire_id))
