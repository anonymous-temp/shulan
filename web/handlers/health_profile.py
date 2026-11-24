"""Health-profile HTTP inbound adapters."""

from __future__ import annotations

import json

import tornado.web

from modules.health_profile.application import HealthProfileConflictError
from modules.health_profile.application import HealthProfileNotFoundError
from modules.health_profile.application import HealthProfileValidationError
from utils.log import getLogger
from web.handlers.base import BaseHandler

logger = getLogger(__name__)


class HealthProfileBaseHandler(BaseHandler):
    """Map profile application errors without leaking them into Tornado."""

    def initialize(self, service) -> None:
        self.health_profile_service = service

    async def post(self) -> None:
        try:
            body = self._json_object()
            data = await self.handle_profile(body)
            self.write_response(200, 'success', data)
        except tornado.web.HTTPError:
            raise
        except HealthProfileNotFoundError as error:
            raise tornado.web.HTTPError(404, log_message=str(error))
        except HealthProfileConflictError as error:
            raise tornado.web.HTTPError(409, log_message=str(error))
        except HealthProfileValidationError as error:
            raise tornado.web.HTTPError(422, log_message=str(error))
        except Exception as error:
            logger.error('处理用户健康画像请求失败: %s', type(error).__name__)
            raise tornado.web.HTTPError(
                500,
                log_message='处理用户健康画像请求失败',
            )

    def _json_object(self) -> dict:
        try:
            body = json.loads(self.request.body)
        except json.JSONDecodeError:
            raise tornado.web.HTTPError(400, log_message='无效的JSON格式')
        if not isinstance(body, dict):
            raise tornado.web.HTTPError(400, log_message='请求体必须为JSON对象')
        self.bind_log_identity(body)
        return body

    @staticmethod
    def _required(body: dict, field: str):
        if body.get(field) is None:
            raise tornado.web.HTTPError(
                400,
                log_message=f'缺少必填参数: {field}',
            )
        return body[field]

    async def handle_profile(self, body: dict):
        raise NotImplementedError


class HealthProfileGetHandler(HealthProfileBaseHandler):
    async def handle_profile(self, body: dict):
        if 'includePending' in body:
            raise tornado.web.HTTPError(
                422,
                log_message='includePending 已取消，请使用 factStatuses。'
                '仅查看已生效事实时传入 ["active"]，查看全部三种状态时可省略 factStatuses。',
            )
        optional_fields = {}
        if 'documentObservationView' in body:
            optional_fields['document_observation_view'] = body['documentObservationView']
        return await self.health_profile_service.get_profile(
            self._required(body, 'userId'),
            subject_id=body.get('subjectId'),
            include_evidence=body.get('includeEvidence', False),
            indicator_limit=body.get('indicatorLimit', 12),
            indicator_scope=body.get('indicatorScope', 'all'),
            fact_statuses=body.get('factStatuses'),
            **optional_fields,
        )


class HealthProfileSubjectListHandler(HealthProfileBaseHandler):
    async def handle_profile(self, body: dict):
        return await self.health_profile_service.list_subjects(
            self._required(body, 'userId'),
            include_archived=body.get('includeArchived', False),
        )


class HealthProfileDocumentObservationListHandler(HealthProfileBaseHandler):
    async def handle_profile(self, body: dict):
        optional_fields = {}
        if 'view' in body:
            optional_fields['view'] = body['view']
        return await self.health_profile_service.list_document_observations(
            self._required(body, 'userId'),
            subject_id=body.get('subjectId'),
            document_id=body.get('documentId'),
            cursor=body.get('cursor'),
            limit=body.get('limit', 20),
            **optional_fields,
        )


class HealthProfileSubjectUpsertHandler(HealthProfileBaseHandler):
    async def handle_profile(self, body: dict):
        optional_fields = {}
        if 'displayName' in body:
            optional_fields['display_name'] = body['displayName']
        if 'relationship' in body:
            optional_fields['relationship'] = body['relationship']
        result = await self.health_profile_service.upsert_subject(
            self._required(body, 'userId'),
            self._required(body, 'requestId'),
            subject_id=body.get('subjectId'),
            kind=body.get('kind'),
            expected_subject_version=body.get('expectedSubjectVersion'),
            **optional_fields,
        )
        return result.to_api()


class HealthProfileFactListHandler(HealthProfileBaseHandler):
    async def handle_profile(self, body: dict):
        return await self.health_profile_service.list_facts(
            self._required(body, 'userId'),
            subject_id=body.get('subjectId'),
            categories=body.get('categories'),
            statuses=body.get('statuses'),
            fact_types=body.get('factTypes'),
            effective_from=body.get('effectiveFrom'),
            effective_to=body.get('effectiveTo'),
            cursor=body.get('cursor'),
            limit=body.get('limit', 20),
        )


class HealthProfileFactResolveHandler(HealthProfileBaseHandler):
    async def handle_profile(self, body: dict):
        return await self.health_profile_service.resolve_fact(
            self._required(body, 'userId'),
            self._required(body, 'subjectId'),
            self._required(body, 'factId'),
            self._required(body, 'decision'),
            replacement=body.get('replacement'),
            reason=body.get('reason'),
            expected_fact_version=self._required(body, 'expectedFactVersion'),
            expected_profile_version=self._required(
                body,
                'expectedProfileVersion',
            ),
            request_id=self._required(body, 'requestId'),
        )


class HealthProfileFactDeleteHandler(HealthProfileBaseHandler):
    async def handle_profile(self, body: dict):
        return await self.health_profile_service.delete_fact(
            self._required(body, 'userId'),
            self._required(body, 'subjectId'),
            self._required(body, 'factId'),
            delete_scope=self._required(body, 'deleteScope'),
            reason=body.get('reason'),
            expected_fact_version=self._required(body, 'expectedFactVersion'),
            expected_profile_version=self._required(
                body,
                'expectedProfileVersion',
            ),
            request_id=self._required(body, 'requestId'),
        )


class HealthProfileChangeListHandler(HealthProfileBaseHandler):
    async def handle_profile(self, body: dict):
        return await self.health_profile_service.list_changes(
            self._required(body, 'userId'),
            subject_id=body.get('subjectId'),
            event_types=body.get('eventTypes'),
            cursor=body.get('cursor'),
            limit=body.get('limit', 20),
        )


__all__ = [
    'HealthProfileBaseHandler',
    'HealthProfileChangeListHandler',
    'HealthProfileDocumentObservationListHandler',
    'HealthProfileFactDeleteHandler',
    'HealthProfileFactListHandler',
    'HealthProfileFactResolveHandler',
    'HealthProfileGetHandler',
    'HealthProfileSubjectListHandler',
    'HealthProfileSubjectUpsertHandler',
]
