"""AgentHub-backed narrative adapter for the report business module.

协议与被替换的 ``AionNarrativeClient`` 完全一致：``generate(payload) -> dict``。
超时、重试、围栏清理和严格 JSON 对象解析全部保留在本层；Graph 只负责
一次模型调用，不在 Graph/Gateway 叠加第二层默认重试。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any
from uuid import uuid4

from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents.graphs.report import REPORT_NARRATIVE_CAPABILITY
from modules.report.agent import REPORT_NARRATIVE_SELECTION
from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY
from modules.report.fallback_policy import EVALUATE_MODEL_CONCURRENCY
from modules.report.fallback_policy import EVALUATE_SHARED_MODEL_CONCURRENCY
from modules.report.fallback_policy import ReportFallbackPolicy
from modules.report.narrative import NarrativeError
from modules.report.performance import add_report_elapsed_ms
from modules.report.performance import increment_report_metric

# 单次实际模型尝试的超时。限流排队时间不计入该预算。
MODEL_ATTEMPT_TIMEOUT_SECONDS = 90
# 整章成稿与整章审核以思考模式运行，单次输出数千字，90 秒不足以完成。
CHAPTER_ATTEMPT_TIMEOUT_SECONDS = 420
_CHAPTER_TASKS = frozenset({
    'write_report_chapter', 'review_report_chapter', 'humanize_report_chapter',
    'plan_source_bound_assessment_content', 'review_source_bound_assessment_content',
    # 整案分析、分节成稿与复核都读入完整的受检者资料并以思考模式运行。
    'analyze_assessment_case', 'write_assessment_case_section', 'review_assessment_case',
})

# Application-wide pool, with a separate per-report execution pool below.
MODEL_CONCURRENCY = EVALUATE_SHARED_MODEL_CONCURRENCY


class AgentNarrativeClient:
    """一次报告执行专属的叙事适配器。

    绑定不可变的 user_id / session_id / 执行标识，因此并发报告不会串身份。
    它借用共享的 AgentHub 和并发闸门，不持有、也不关闭平台资源。
    """

    def __init__(
        self,
        *,
        hub: AgentHub,
        semaphore: asyncio.Semaphore,
        policy: ReportFallbackPolicy,
        user_id: str,
        session_id: str,
        execution_id: str,
        execution_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._hub = hub
        self._semaphore = semaphore
        self._execution_semaphore = (
            execution_semaphore if execution_semaphore is not None else asyncio.Semaphore(EVALUATE_MODEL_CONCURRENCY)
        )
        self.policy = policy
        self._user_id = user_id
        self._session_id = session_id
        self._execution_id = execution_id

    async def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or not payload:
            raise NarrativeError('narrative payload must be a non-empty object')

        # 同一次 generate 的全部重试共用 request_id；每次实际模型尝试由
        # AgentRunContext 生成独立 run_id。标识不含任何健康数据。
        request_id = f'{self._execution_id}:{uuid4().hex}'
        attempts = self.policy.attempts_for(self.policy.llm_attempts)
        last_error: Exception | None = None

        for attempt in range(attempts):
            increment_report_metric('model.request_attempt')
            started = time.monotonic()
            service_started = None
            queue_recorded = False
            try:
                # 闸门在 wait_for 之外：排队等待不消耗 90 秒尝试预算。
                # Acquire the execution slot first: one report cannot enqueue
                # all its batches ahead of other reports in the shared pool.
                async with self._execution_semaphore:
                    async with self._semaphore:
                        add_report_elapsed_ms('model.queue_wait', (time.monotonic() - started) * 1000)
                        queue_recorded = True
                        context = AgentRunContext.create(
                            user_id=self._user_id,
                            session_id=self._session_id,
                            request_id=request_id,
                        )
                        service_started = time.monotonic()
                        try:
                            result = await asyncio.wait_for(
                                self._hub.invoke(
                                    REPORT_NARRATIVE_CAPABILITY,
                                    AgentRequest({'payload': payload}),
                                    context,
                                    selection=REPORT_NARRATIVE_SELECTION,
                                ),
                                timeout=(CHAPTER_ATTEMPT_TIMEOUT_SECONDS
                                    if payload.get('task') in _CHAPTER_TASKS
                                    else MODEL_ATTEMPT_TIMEOUT_SECONDS),
                            )
                        finally:
                            add_report_elapsed_ms('model.service', (time.monotonic() - service_started) * 1000)
                content = _strip_code_fence(str(result.data.get('content') or '').strip())
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError as exc:
                    increment_report_metric('model.invalid_json')
                    # The strict boundary rejects malformed model output. Keep
                    # the original response privately so a real request can be
                    # diagnosed without publishing a repaired or empty report.
                    from modules.report.evaluation_report import _write_private_diagnostic

                    _write_private_diagnostic('invalid-json-' + request_id.rsplit(':', 1)[-1], {
                        'task': payload.get('task'), 'section': payload.get('section'),
                        'attempt': attempt + 1, 'content': content,
                        'error': str(exc), 'usage': result.data.get('usage'),
                    })
                    last_error = exc
                    if attempt + 1 < attempts:
                        increment_report_metric('model.retry')
                        delay = self.policy.backoff_after(attempt)
                        if delay:
                            await asyncio.sleep(delay)
                        continue
                    raise NarrativeError('model response is not valid JSON') from exc
                if not isinstance(parsed, dict):
                    increment_report_metric('model.invalid_shape')
                    last_error = TypeError('model response must be an object')
                    if attempt + 1 < attempts:
                        increment_report_metric('model.retry')
                        delay = self.policy.backoff_after(attempt)
                        if delay:
                            await asyncio.sleep(delay)
                        continue
                    raise NarrativeError('model response must be an object')
                increment_report_metric('model.success')
                return parsed
            except Exception as exc:
                if isinstance(exc, NarrativeError):
                    raise
                # 平台异常在适配器边界翻译，报告业务不依赖 Agent/AION 异常类。
                last_error = exc
                if isinstance(exc, TimeoutError):
                    increment_report_metric('model.timeout')
                else:
                    increment_report_metric('model.request_failed')
                if attempt + 1 < attempts:
                    increment_report_metric('model.retry')
                    delay = self.policy.backoff_after(attempt)
                    if delay:
                        await asyncio.sleep(delay)
            finally:
                if not queue_recorded:
                    add_report_elapsed_ms('model.queue_wait', (time.monotonic() - started) * 1000)
                add_report_elapsed_ms(
                    'model.complete',
                    (time.monotonic() - started) * 1000,
                )
        raise NarrativeError('model request failed') from last_error


class NarrativeClientFactory:
    """应用级单例：共享 AgentHub 和并发闸门，不共享报告身份。

    闸门属于 Factory 而不是适配器实例——按报告创建适配器时，模型并发
    上限不能随并发报告数量倍增。Factory 借用 AgentHub，AgentRuntime 的
    生命周期由 ApplicationBundle/ResourceRegistry 管理，本类不关闭它。
    """

    def __init__(
        self,
        *,
        hub: AgentHub,
        policy: ReportFallbackPolicy = DEFAULT_REPORT_FALLBACK_POLICY,
        concurrency: int = MODEL_CONCURRENCY,
        per_report_concurrency: int = EVALUATE_MODEL_CONCURRENCY,
    ) -> None:
        if not isinstance(hub, AgentHub):
            raise TypeError('hub must be an AgentHub')
        self._hub = hub
        self._policy = policy
        self._semaphore = asyncio.Semaphore(max(1, int(concurrency)))
        self._per_report_concurrency = max(1, int(per_report_concurrency))

    def create(
        self,
        *,
        user_id: str,
        session_id: str,
        execution_id: str,
    ) -> AgentNarrativeClient:
        """为一次报告执行创建绑定身份的适配器。"""
        return AgentNarrativeClient(
            hub=self._hub,
            semaphore=self._semaphore,
            policy=self._policy,
            user_id=user_id,
            session_id=session_id,
            execution_id=execution_id,
            execution_semaphore=asyncio.Semaphore(self._per_report_concurrency),
        )


def _strip_code_fence(value: str) -> str:
    match = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', value, flags=re.DOTALL)
    return match.group(1).strip() if match else value.strip()


__all__ = [
    'AgentNarrativeClient',
    'NarrativeClientFactory',
]
