"""Single synchronous Health Analysis endpoint with metadata-only diagnostics."""

from __future__ import annotations

import asyncio
import json

import tornado.web

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
from utils.log import getLogger
from web.handlers.base import BaseHandler

logger = getLogger(__name__)


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON object key')
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError('Non-standard JSON constant')


class HealthAnalysisHandler(BaseHandler):
    """Transport validation only; business confirmation belongs to the service."""

    log_body_enabled = False
    max_body_bytes = 2_000_000

    def initialize(self, service):
        self.health_analysis_service = service
        self._analysis_request_task = None
        self._client_disconnected = False

    def set_default_headers(self):
        super().set_default_headers()
        self.set_header('Cache-Control', 'no-store')

    def bind_log_identity(self, body) -> None:
        # Request UUID provides correlation. Raw user/group IDs and answers are
        # not copied into the ordinary logging context for this medical route.
        return None

    def on_connection_close(self) -> None:
        self._client_disconnected = True
        task = self._analysis_request_task
        if task is not None and not task.done():
            task.cancel()
        super().on_connection_close()

    def _release_disconnected_lease(self):
        if self._client_disconnected:
            lease = getattr(self, '_request_lease', None)
            if lease is not None:
                lease.release()
                self._request_lease = None

    def _body(self):
        if len(self.request.body) > self.max_body_bytes:
            raise tornado.web.HTTPError(413, log_message='请求正文超过大小限制')
        content_type = self.request.headers.get('Content-Type', '').split(';', 1)[0].lower().strip()
        if content_type != 'application/json':
            raise tornado.web.HTTPError(400, log_message='请求须使用 application/json')
        try:
            body = json.loads(
                self.request.body,
                parse_constant=_invalid_constant,
                object_pairs_hook=_json_object,
            )
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise tornado.web.HTTPError(400, log_message='请求正文须为合法 JSON 对象') from None
        if not isinstance(body, dict):
            raise tornado.web.HTTPError(400, log_message='请求正文须为合法 JSON 对象')
        if set(body) != {'userId', 'name', 'age', 'gender', 'groupId', 'surveyList'}:
            raise tornado.web.HTTPError(400, log_message='请求须包含 userId、name、age、gender、groupId 和 surveyList')
        return body

    async def post(self):
        self._analysis_request_task = asyncio.current_task()
        limits = getattr(self.health_analysis_service, 'limits', None)
        total_seconds = limits.total_timeout_seconds if isinstance(limits, AnalysisLimits) else 120
        request_deadline = asyncio.get_running_loop().time() + total_seconds
        budget = asyncio.timeout_at(request_deadline)
        try:
            try:
                async with budget:
                    body = self._body()
                    if self.health_analysis_service is None:
                        raise tornado.web.HTTPError(503, log_message='健康分析服务尚未配置')
                    result = await self.health_analysis_service.generate(
                        body,
                        request_uuid=self.request_uuid,
                        deadline_at=request_deadline,
                    )
            except TimeoutError:
                if budget.expired():
                    raise AnalysisTimeoutError('Request execution deadline exceeded') from None
                raise
            if not self._client_disconnected:
                self.write_response(200, 'success', result)
        except BasicInfoConflictError:
            raise tornado.web.HTTPError(409, log_message='基础信息主体或确认版本冲突') from None
        except BasicInfoDeliveryError:
            raise tornado.web.HTTPError(503, log_message='基础信息确认尚未可靠受理') from None
        except InvalidAnalysisInputError:
            raise tornado.web.HTTPError(400, log_message='健康分析参数或问卷结构不合法') from None
        except AnalysisCapacityError:
            raise tornado.web.HTTPError(413, log_message='健康分析资料超过容量限制') from None
        except AnalysisBusyError:
            raise tornado.web.HTTPError(503, log_message='健康分析服务繁忙，请稍后重试') from None
        except AnalysisGenerationError as error:
            stage = error.stage if isinstance(error.stage, AnalysisGenerationStage) else AnalysisGenerationStage.RESULT
            reason = error.reason
            if not isinstance(reason, AnalysisGenerationFailure):
                reason = AnalysisGenerationFailure.UNCLASSIFIED
            logger.error(
                'Health Analysis generation failed: request_uuid=%s, stage=%s, reason=%s',
                self.request_uuid,
                stage,
                reason,
            )
            raise tornado.web.HTTPError(502, log_message='健康分析生成暂不可用') from None
        except TraceDeliveryError:
            raise tornado.web.HTTPError(503, log_message='健康分析结果追溯保存未确认') from None
        except AnalysisTimeoutError:
            raise tornado.web.HTTPError(504, log_message='健康分析执行超时') from None
        finally:
            self._analysis_request_task = None
            self._release_disconnected_lease()

    def write_error(self, status_code, **kwargs):
        # The framework's error text may originate in arbitrary dependency
        # exceptions; only our fixed transport messages are returned publicly.
        if status_code == 503:
            self.set_header('Retry-After', '5')
        defaults = {
            400: '健康分析请求参数不合法',
            409: '基础信息主体或确认版本冲突',
            413: '健康分析资料超过容量限制',
            500: '健康分析服务内部异常',
            502: '健康分析生成暂不可用',
            503: '健康分析服务暂不可用',
            504: '健康分析执行超时',
        }
        safe_messages = {
            '请求正文超过大小限制',
            '请求须使用 application/json',
            '请求正文须为合法 JSON 对象',
            '请求须包含 userId、name、age、gender、groupId 和 surveyList',
            '健康分析服务尚未配置',
            '基础信息主体或确认版本冲突',
            '基础信息确认尚未可靠受理',
            '健康分析参数或问卷结构不合法',
            '健康分析资料超过容量限制',
            '健康分析服务繁忙，请稍后重试',
            '健康分析生成暂不可用',
            '健康分析结果追溯保存未确认',
            '健康分析执行超时',
            '服务正在排空，暂不接收新请求',
        }
        message = defaults.get(status_code, '健康分析请求未完成')
        info = kwargs.get('exc_info')
        original = info[1] if info else None
        candidate = getattr(original, 'log_message', None)
        if status_code != 500 and isinstance(candidate, str) and candidate in safe_messages:
            message = candidate
        self.write_response(status_code, message, None)
