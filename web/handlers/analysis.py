"""Health-report interpretation HTTP adapter."""

from __future__ import annotations

import json

import tornado.web

from modules.analysis.application import HealthReportUnsupportedFormatError
from modules.analysis.application import HealthReportUpstreamError
from modules.analysis.application.documents import DocumentBusyError
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.application.documents import DocumentSubmissionConflictError
from modules.health_profile.application import HealthReportSourceConflictError
from utils.log import getLogger
from web.handlers.base import BaseHandler

logger = getLogger(__name__)


class HealthReportHandler(BaseHandler):
    """Interpret a health report while keeping HTTP concerns at the edge."""

    def initialize(
        self,
        processing_service_factory=None,
        processing_service=None,
    ) -> None:
        if processing_service is not None:
            self.processing_service = processing_service
            return
        if processing_service_factory is not None:
            self.processing_service = processing_service_factory()
            return
        raise TypeError(
            'HealthReportHandler requires processing_service_factory or processing_service',
        )

    async def post(self) -> None:
        try:
            await self.handle_report()
        except (HealthReportSourceConflictError, DocumentSubmissionConflictError) as error:
            raise tornado.web.HTTPError(409, log_message=str(error))
        except DocumentNotFoundError as error:
            raise tornado.web.HTTPError(404, log_message=str(error))
        except DocumentBusyError as error:
            raise tornado.web.HTTPError(503, log_message=str(error))
        except HealthReportUnsupportedFormatError as error:
            raise tornado.web.HTTPError(415, log_message=str(error))
        except HealthReportUpstreamError as error:
            raise tornado.web.HTTPError(502, log_message=str(error))
        except (TypeError, ValueError) as error:
            raise tornado.web.HTTPError(400, log_message=str(error))
        except tornado.web.HTTPError:
            raise
        except Exception as error:
            logger.error('处理体检报告请求失败: %s', type(error).__name__)
            raise tornado.web.HTTPError(500, log_message=str(error))

    async def handle_report(self) -> None:
        try:
            body = json.loads(self.request.body)
            self.bind_log_identity(body)
        except json.JSONDecodeError:
            raise tornado.web.HTTPError(400, log_message='无效的JSON格式')
        if not isinstance(body, dict):
            raise tornado.web.HTTPError(400, log_message='请求体必须为JSON对象')
        optional = {}
        if 'documentId' in body:
            optional['document_id'] = body['documentId']
        result = await self.processing_service.process(
            file_id=body.get('fileId'),
            user_id=body.get('userId'),
            subject_id=body.get('subjectId'),
            report_id=body.get('reportId'),
            **optional,
        )
        processing = result.get('processing') or {}
        message = 'success'
        if processing.get('status') == 'unavailable':
            message = next(iter(processing.get('warnings') or []), '暂未获得可用的报告信息。')
        self.write_response(200, message, result)


__all__ = ['HealthReportHandler']
