# -*- coding: utf-8 -*-
"""Analysis application service for health-report interpretation.

使用阿里云文档解析服务完成文件 -> 正文的解析，再用大模型按报告类型抽取结构化结果。
与通用解析接口不同：本模块返回的是体检报告专属字段（如检验各项指标、影像所见/诊断等），
而非标题/摘要/正文等通用文档元数据。
"""

import asyncio
import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace
from urllib.parse import urlparse
from uuid import uuid4

from agents import AgentRequest
from agents import AgentRunContext
from agents import AgentRuntime
from agents.errors import AgentProtocolError
from agents.graphs.analysis import ANALYSIS_HEALTH_REPORT_CAPABILITY
from agents.graphs.analysis.profile import ANALYSIS_HEALTH_REPORT_PROFILE
from agents.llm.aion_gateway import AionLLMGateway
from modules.analysis.analysis_formal_contracts import ANALYSIS_CURRENT_PUBLICATION
from modules.analysis.application.analysis_display import display_result
from modules.analysis.application.analysis_display import has_displayable
from modules.analysis.application.analysis_outcomes import empty_analysis
from modules.analysis.application.analysis_outcomes import nodes
from modules.analysis.application.analysis_outcomes import settle
from modules.analysis.application.analysis_outcomes import settle_summaries
from modules.analysis.application.analysis_validation import VALIDATION_MODE
from modules.analysis.application.analysis_validation import validate_analysis
from modules.analysis.application.content_results import TECHNICAL_TIMEOUT_ISSUES
from modules.analysis.application.content_results import classify_outcome
from modules.analysis.application.documents import DocumentInterpretationError
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.application.documents import DocumentParsingError
from modules.analysis.application.documents import DocumentProcessingTrace
from modules.analysis.application.documents import PreparedDocumentRound
from modules.analysis.application.documents import ProcessedHealthReport
from modules.analysis.application.documents import parse_document
from modules.analysis.application.documents import submit_document
from modules.analysis.application.extraction import analysis_limiter
from modules.analysis.application.extraction import merge_document_results
from modules.analysis.application.extraction import normalize_document_time
from modules.analysis.application.input_preparation import MAX_INPUT_TOKENS
from modules.analysis.application.input_preparation import TOKEN_ESTIMATOR
from modules.analysis.application.input_preparation import input_token_budget
from modules.analysis.application.input_preparation import prepare_input
from modules.analysis.application.model_json import escape_string_controls
from modules.analysis.application.source_facts import SourceFacts
from modules.analysis.application.source_facts import reconcile_source_facts
from modules.analysis.application.source_facts import reconciled_issues
from modules.analysis.contracts import OutputSelection
from modules.analysis.ports import DocumentParsingClient
from modules.analysis.ports import DocumentTraceStore
from modules.analysis.ports import ImageFormatProbe
from modules.analysis.ports import ImageProbeError
from modules.analysis.ports import InputTokenCounter
from modules.analysis.ports import UnsupportedImageFormatError
from utils.log import getLogger
from utils.report_files import IMAGE_EXTENSIONS
from utils.report_files import REPORT_EXTENSIONS
from utils.report_files import resolve_report_filename
from utils.report_flags import report_flag_is_abnormal

logger = getLogger(__name__)
_CORRECTION_PREFIX = '请将下面报告重新输出为完整、符合约定字段结构的 JSON；不要解释或使用 Markdown。\n'
_DEEPSEEK_CONTEXT_WINDOWS = {'deepseek-flash': 1048576, 'deepseek-v4-pro': 1048576}


@dataclass
class _AnalysisMetrics:
    plannedCalls: int = 0
    modelCalls: int = 0
    outputRecoveryCalls: int = 0
    jsonSyntaxRecoveries: int = 0
    reusedParts: int = 0
    queueWaitMs: int = 0
    modelElapsedMs: int = 0
    recoveryQueueWaitMs: int = 0
    recoveryElapsedMs: int = 0
    promptTokens: int = 0
    completionTokens: int = 0
    totalTokens: int = 0
    usageMetricsKnown: bool = True
    transportRetries: int = 0
    transportMetricsKnown: bool = True
    inputChars: int = 0


@dataclass
class _AnalysisAttempt:
    data: dict | None = None
    failure: str | None = None
    validation: object = None


async def _collect_analysis_parts(coroutines, *, timeout=None, concurrency=4):
    """Bound ready work; a bad slice never cancels unrelated completed/queued slices."""
    results = [None] * len(coroutines)
    pending = iter(enumerate(coroutines))
    started = set()

    async def worker():
        for index, coroutine in pending:
            started.add(index)
            results[index] = await coroutine

    tasks = [asyncio.create_task(worker()) for _ in range(min(concurrency, len(coroutines)))]
    try:
        async with asyncio.timeout(timeout):
            await asyncio.gather(*tasks)
    except TimeoutError:
        pass
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for index, coroutine in enumerate(coroutines):
            if index not in started:
                coroutine.close()
    return results, [index for index, result in enumerate(results) if result is None]


# 报告类型枚举
REPORT_TYPES = ('lab', 'imaging', 'checkup', 'microbiome', 'unknown')

# 检验报告各指标的字段集合，用于归一化校验
LAB_ITEM_KEYS = ('itemName', 'resultValue', 'referenceRange', 'unit', 'flag')


class HealthReportUnsupportedFormatError(ValueError):
    """体检报告文件格式不受支持。"""


class HealthReportUpstreamError(RuntimeError):
    """体检报告文件解析或模型解读失败。"""


class HealthReportInterpreter:
    """体检报告解读器。"""

    # Ordinary reports go to the model whole. Very long text is partitioned only by size.
    MAX_ANALYZE_INPUT_TOKENS = MAX_INPUT_TOKENS
    VERSION = 'analysis-health-report-v12'
    CAPABILITY = ANALYSIS_HEALTH_REPORT_CAPABILITY
    PROFILE = ANALYSIS_HEALTH_REPORT_PROFILE
    CALL_TIMEOUT_SECONDS = 90
    ANALYSIS_TIMEOUT_SECONDS = 240

    def __init__(
        self,
        agent_runtime: AgentRuntime,
        *,
        docmind_client: DocumentParsingClient,
        count_input_tokens: InputTokenCounter,
        source_fact_reader=None,
    ):
        if not isinstance(agent_runtime, AgentRuntime):
            raise TypeError('agent_runtime must be an AgentRuntime')
        if not isinstance(docmind_client, DocumentParsingClient):
            raise TypeError(
                'docmind_client must implement DocumentParsingClient',
            )
        self._agent_runtime = agent_runtime
        self.docmind_client = docmind_client
        self._count_input_tokens = count_input_tokens
        self._source_fact_reader = source_fact_reader

    def extract_text(self, file_url, *, file_type=None):
        """解析一份网络体检报告并返回正文。

        这里只执行 DocMind 文件解析，不调用模型：
        - str   ：文件正文；
        - False ：文件解析失败（上游任务失败）；
        - None  ：解析过程中发生异常。
        """
        try:
            file_name, inferred_file_type = self._resolve_name(file_url)
            if file_type is None:
                file_type = inferred_file_type
            elif not file_name or not inferred_file_type:
                file_name = f'health-report.{file_type}'
            task_id = submit_document(
                self.docmind_client,
                file_url=file_url,
                filename=file_name,
                file_type=file_type,
            )
        except Exception as error:
            logger.error('提交文档解析任务失败: error_type=%s', type(error).__name__)
            return None

        result = self.docmind_client.get_result(task_id, poll_interval=1)

        if not result['success']:
            logger.error('文档解析失败')
            return False

        return result['text']

    async def extract_document(self, *, trace, file_url, filename, file_type, content=None):
        """Production parsing entry: retain the complete SDK response before interpretation."""
        return await parse_document(
            self.docmind_client,
            trace=trace,
            file_url=file_url,
            filename=filename,
            file_type=file_type,
            content=content,
        )

    @staticmethod
    def _resolve_name(file_url):
        """从 URL 推断文件名与扩展名。

        体检报告的 fileId 常形如：
            https://host/api/v1/file/preview?objectName=xxx.pdf
        真实文件名位于 query 的 objectName 中，path 末尾不含扩展名，
        因此优先从 query 推断，回退到 path。
        """
        return resolve_report_filename(file_url)

    @staticmethod
    def _hint_prefix(report_type_hint):
        return f'报告类型提示：{report_type_hint}，仍以原文为准。\n' if report_type_hint else ''

    def _input_token_limit(self, report_type_hint):
        gateway = getattr(getattr(self, '_agent_runtime', None), 'llm_gateway', None)
        if not isinstance(gateway, AionLLMGateway):
            return self.MAX_ANALYZE_INPUT_TOKENS
        profile = self._agent_runtime.profiles.get(self.PROFILE)
        provider, model = gateway.request_model(profile)
        window = _DEEPSEEK_CONTEXT_WINDOWS.get(model or 'deepseek-flash') if provider.lower() == 'deepseek' else None
        return min(
            self.MAX_ANALYZE_INPUT_TOKENS,
            input_token_budget(
                window,
                profile.model_kwargs['max_tokens'],
                gateway.render_prompt(profile) or '',
                self._hint_prefix(report_type_hint) + _CORRECTION_PREFIX,
                count_tokens=self._count_input_tokens,
            ),
        )

    async def analyze(
        self,
        content,
        *,
        user_id='analysis-anonymous',
        session_id=None,
        subject_id=None,
        trace=None,
        report_type_hint=None,
        docmind=None,
        source_snapshot=None,
    ):
        """DocMind text -> prompt/LLM -> JSON parsing and schema validation."""
        if not content or not content.strip():
            return None
        if report_type_hint is not None and report_type_hint not in REPORT_TYPES:
            raise ValueError('report_type_hint 无效')
        if docmind is None:
            docmind = getattr(trace, 'parsed_document', None)
        prepared = prepare_input(
            content,
            token_limit=self._input_token_limit(report_type_hint),
            count_tokens=self._count_input_tokens,
        )
        session_id = session_id or f'analysis-{uuid4()}'
        metrics = _AnalysisMetrics(plannedCalls=len(prepared.chunks))
        started = asyncio.get_running_loop().time()
        final = None
        try:
            source_facts = await self._read_source_facts(
                content,
                docmind=docmind,
                trace=trace,
                snapshot=source_snapshot,
            )
            results, failures = await _collect_analysis_parts(
                [
                    self._analyze_part(
                        prepared,
                        chunk,
                        f'{index:05d}',
                        user_id=user_id,
                        session_id=session_id,
                        subject_id=subject_id,
                        trace=trace,
                        report_type_hint=report_type_hint,
                        metrics=metrics,
                        single=len(prepared.chunks) == 1,
                    )
                    for index, chunk in enumerate(prepared.chunks)
                ],
                timeout=self.ANALYSIS_TIMEOUT_SECONDS,
            )
            for index in failures:
                saved = (
                    await trace.read_payload(f'analysis_part_{index:05d}')
                    if trace is not None and hasattr(trace, 'read_payload')
                    else None
                )
                raw = (
                    saved.get('rawData')
                    if isinstance(saved, dict)
                    and saved.get('identity')
                    == self._checkpoint_identity(prepared, prepared.chunks[index], report_type_hint, trace)
                    else None
                )
                results[index] = self._settle_part(
                    raw or empty_analysis(),
                    [{'code': 'analysis_deadline', 'chunkId': f'{index:05d}', 'sourceIds': [], 'recoverable': True}],
                )
            results = await self._after_parts(
                results,
                prepared=prepared,
                failures=failures,
                user_id=user_id,
                session_id=session_id,
                subject_id=subject_id,
                trace=trace,
                report_type_hint=report_type_hint,
                metrics=metrics,
                deadline_at=started + self.ANALYSIS_TIMEOUT_SECONDS,
            )
            issues = [issue for part in results for issue in part['processing']['issues']]
            merged = merge_document_results(results) if results else empty_analysis()
            issues = reconciled_issues(merged, source_facts, issues)
            merged = reconcile_source_facts(merged, source_facts)
            for node in nodes(merged):
                for key in ('processing', 'analysisEvidence', '_verifiedData', 'sourceBindings', '_basicInfoMerged'):
                    node.pop(key, None)
                if source_facts and source_facts.observations and node.get('reportType') == 'microbiome':
                    node['status'] = self._source_status(node.get('observations', []))
            if source_facts and source_facts.observations:
                merged['status'] = self._source_status(merged['observations'])
                if any(
                    child.get('reportType') != 'microbiome'
                    and child.get('status') == 'abnormal'
                    and (
                        child.get('observations')
                        or child.get('reportType') == 'imaging'
                        and any((child.get('expansion') or {}).get(key) for key in ('findings', 'diagnosis'))
                    )
                    for child in merged.get('sections', [])
                ):
                    merged['status'] = 'abnormal'
            final = settle(merged, issues=issues, version=self.VERSION)
            if not issues:
                final['processing']['retryable'] = False
            settle_summaries(
                final,
                rebuild=len(prepared.chunks) > 1
                or metrics.outputRecoveryCalls > 0
                or bool(source_facts and source_facts.observations),
            )
            self._classify(final)
            return final
        finally:
            summary = {
                **asdict(metrics),
                'elapsedMs': round((asyncio.get_running_loop().time() - started) * 1000),
                'processingMode': 'llm_json',
                'validationMode': VALIDATION_MODE,
                'originalChars': len(content),
                'inputTokenEstimator': TOKEN_ESTIMATOR,
                'estimatedOriginalTokens': prepared.estimated_tokens,
                'inputTokenLimit': prepared.token_limit,
                'estimatedPartTokens': [chunk.estimated_tokens for chunk in prepared.chunks],
                'preparedChars': len(prepared.text),
                'validationPassed': final is not None and not final['processing']['issues'],
                'completed': final is not None and final['processing']['status'] == 'complete',
                'firstPassSuccess': final is not None
                and not final['processing']['issues']
                and metrics.outputRecoveryCalls == 0
                and metrics.reusedParts == 0
                and metrics.transportRetries == 0,
                'processingStatus': final['processing']['status'] if final else None,
                'contentOutcome': final['processing']['contentOutcome'] if final else None,
                'failureKind': final['processing']['failureKind'] if final else 'technical',
            }
            if not metrics.transportMetricsKnown:
                summary['transportRetries'] = None
            if trace is not None:
                try:
                    await trace.write_payload('analysis_metrics', summary)
                except Exception as error:
                    logger.error(
                        '体检报告处理统计归档失败: trace_id=%r error_type=%s',
                        getattr(trace, 'trace_id', None),
                        type(error).__name__,
                    )
            logger.info('体检报告处理统计: trace_id=%r metrics=%s', getattr(trace, 'trace_id', None), summary)

    async def _after_parts(self, results, **kwargs):
        return results

    def _validate_reply(self, data, *, text, attempt):
        return validate_analysis(data)

    @staticmethod
    def _classify(result):
        outcome = classify_outcome(result)
        result['processing']['contentOutcome'] = outcome
        result['processing']['failureKind'] = (
            None if result['processing']['status'] == 'complete' else 'content' if outcome else 'technical'
        )

    @staticmethod
    def _source_status(items):
        if any(report_flag_is_abnormal(item.get('rawFlag') or item.get('flag')) for item in items):
            return 'abnormal'
        return 'normal' if items and all(item.get('flag') == '正常' for item in items) else 'pending'

    async def _read_source_facts(self, text, *, docmind, trace, snapshot):
        """An injected reader sees only this call's archived, owner-scoped bytes."""
        reader = getattr(self, '_source_fact_reader', None)
        if reader is None or not re.search(r'菌群|宏基因|微生态', text):
            return None
        source = dict(snapshot or {})
        if not source and trace is not None and hasattr(trace, 'read_payload'):
            document = getattr(trace, 'document', {})
            if document.get('fileType') not in (None, 'pdf'):
                return None
            payload = await trace.read_payload('original_file')
            source = {
                'content': payload.get('content') if isinstance(payload, dict) else None,
                'fileName': document.get('fileName'),
                'contentSha256': document.get('contentSha256'),
            }
        raw = source.get('content')
        if not isinstance(raw, bytes) or not raw.startswith(b'%PDF-'):
            return None
        if source.get('contentSha256') and hashlib.sha256(raw).hexdigest() != source['contentSha256']:
            raise DocumentParsingError('原始文件归档校验失败')
        facts = await asyncio.to_thread(
            reader,
            content=raw,
            text=text,
            file_name=source.get('fileName') or 'source.pdf',
            docmind=docmind,
        )
        if facts is not None and not isinstance(facts, SourceFacts):
            raise TypeError('source_fact_reader must return SourceFacts or None')
        return facts

    def _settle_part(self, raw, issues):
        result = settle(self._normalize(raw), issues=issues, version=self.VERSION)
        if not issues and not any(node.get('unresolvedItems') for node in nodes(result)):
            result['processing'].update(status='complete', retryable=False)
        self._classify(result)
        return result

    def _checkpoint_identity(self, prepared, chunk, report_type_hint, trace):
        return self._chunk_identity(prepared, chunk, report_type_hint)

    def _chunk_identity(self, prepared, chunk, report_type_hint):
        return {
            'version': self.VERSION,
            'start': chunk.start,
            'end': chunk.end,
            'sha256': hashlib.sha256(chunk.text.encode('utf-8')).hexdigest(),
            'sourceHash': prepared.source_hash,
            'inputTokenLimit': prepared.token_limit,
            'sourceKind': 'original',
            'reportTypeHint': report_type_hint,
        }

    async def _analyze_part(
        self,
        prepared,
        chunk,
        key,
        *,
        user_id,
        session_id,
        subject_id,
        trace,
        report_type_hint,
        metrics,
        single=False,
    ):
        identity = self._checkpoint_identity(prepared, chunk, report_type_hint, trace)
        name = f'analysis_part_{key}'
        resume_correction, saved_raw = False, None
        if trace is not None and hasattr(trace, 'read_payload'):
            saved = await trace.read_payload(name)
            if (
                isinstance(saved, dict)
                and saved.get('identity') == identity
                and saved.get('validationVersion') == self.VERSION
            ):
                result = saved.get('result')
                if (
                    isinstance(result, dict)
                    and result.get('processing', {}).get('status') == 'complete'
                    and not any(node.get('unresolvedItems') for node in nodes(result))
                ):
                    metrics.reusedParts += 1
                    return {**result, '_verifiedData': saved.get('rawData', empty_analysis())}
                resume_correction = saved.get('correctionPending') is True
                saved_raw = saved.get('rawData')

        async def invoke(*, correction=False):
            prefix = self._hint_prefix(report_type_hint)
            if correction:
                prefix += _CORRECTION_PREFIX
            waiting = asyncio.get_running_loop().time()
            async with analysis_limiter():
                queue_ms = round((asyncio.get_running_loop().time() - waiting) * 1000)
                metrics.queueWaitMs += queue_ms
                metrics.modelCalls += 1
                metrics.inputChars += len(prefix) + len(chunk.text)
                metrics.outputRecoveryCalls += int(correction)
                if correction:
                    metrics.recoveryQueueWaitMs += queue_ms
                return await self._invoke_analysis(
                    prefix + chunk.text,
                    user_id=user_id,
                    session_id=session_id,
                    subject_id=subject_id,
                    trace=trace,
                    suffix=f'_{key}_repair' if correction else '' if single else f'_{key}',
                    attempt={'chunkId': key, 'source': identity, 'queueWaitMs': queue_ms, 'outputRecovery': correction},
                    log_context=(
                        f'trace_id={getattr(trace, "trace_id", None)!r} session_id={session_id!r} chunk_id={key}'
                    ),
                    metrics=metrics,
                )

        attempt = await invoke(correction=resume_correction)
        raw = attempt.validation.data if attempt.validation is not None else saved_raw or empty_analysis()
        issues = list(attempt.validation.issues) if attempt.validation is not None else []
        if trace is not None and attempt.failure and not resume_correction:
            await trace.write_payload(
                name,
                {
                    'identity': identity,
                    'rawData': raw,
                    'correctionPending': attempt.failure == 'validation',
                    'validationVersion': self.VERSION,
                    'result': self._settle_part(
                        raw, issues or [{'code': attempt.failure, 'sourceIds': [], 'recoverable': True}]
                    ),
                },
            )
        # A structurally invalid JSON object gets one correction on the same input.
        if attempt.failure == 'validation' and not resume_correction:
            corrected = await invoke(correction=True)
            if corrected.validation is not None:
                if (
                    corrected.validation.valid
                    or attempt.validation is None
                    or corrected.validation.accepted_count >= attempt.validation.accepted_count
                ):
                    raw = corrected.validation.data
                issues = list(corrected.validation.issues)
            attempt = corrected
        if attempt.failure and attempt.failure != 'validation':
            issues.append(
                {
                    'code': attempt.failure,
                    'sourceIds': [],
                    'recoverable': attempt.failure != 'model_configuration_failed',
                }
            )
        issues = [{**issue, 'chunkId': key} for issue in issues]
        result = self._settle_part(raw, issues)
        if trace is not None:
            await trace.write_payload(
                name, {'identity': identity, 'rawData': raw, 'result': result, 'validationVersion': self.VERSION}
            )
        return {**result, '_verifiedData': raw}

    async def _invoke_analysis(
        self,
        text,
        *,
        user_id,
        session_id,
        subject_id,
        trace,
        suffix,
        attempt,
        log_context,
        metrics,
        source_text=None,
    ):
        context = AgentRunContext.create(
            user_id=user_id,
            session_id=session_id or f'analysis-{uuid4()}',
            subject_id=subject_id,
        )
        attempt = {**attempt, 'sessionId': context.session_id, 'requestId': context.request_id, 'runId': context.run_id}
        log_context = f'{log_context} request_id={context.request_id} run_id={context.run_id}'
        if trace is not None:
            await trace.update(stage='interpreting', requestId=context.request_id, runId=context.run_id)
            await trace.write_payload(
                f'analysis_input{suffix}',
                {
                    'text': text,
                    'truncated': False,
                    'capability': self.CAPABILITY,
                    **attempt,
                },
            )
        started = asyncio.get_running_loop().time()
        try:
            async with asyncio.timeout(self.CALL_TIMEOUT_SECONDS):
                result = await self._agent_runtime.hub.invoke(
                    self.CAPABILITY,
                    AgentRequest({'report_text': text}),
                    context,
                )
        except asyncio.CancelledError:
            elapsed_ms = round((asyncio.get_running_loop().time() - started) * 1000)
            metrics.modelElapsedMs += elapsed_ms
            if attempt.get('outputRecovery'):
                metrics.recoveryElapsedMs += elapsed_ms
            metrics.usageMetricsKnown = False
            metrics.transportMetricsKnown = False
            raise
        except Exception as error:
            elapsed_ms = round((asyncio.get_running_loop().time() - started) * 1000)
            metrics.modelElapsedMs += elapsed_ms
            if attempt.get('outputRecovery'):
                metrics.recoveryElapsedMs += elapsed_ms
            metrics.usageMetricsKnown = False
            retries = getattr(error, 'transport_retries', None)
            metrics.transportMetricsKnown &= type(retries) is int
            if type(retries) is int:
                metrics.transportRetries += retries
            failure = 'model_call_timeout' if isinstance(error, TimeoutError) else 'model_call_failed'
            permanent = False
            cause, seen = error, set()
            while cause is not None and id(cause) not in seen:
                seen.add(id(cause))
                status_code = getattr(cause, 'status_code', None)
                if status_code in (400, 413, 422):
                    failure = 'model_input_rejected'
                    break
                permanent |= status_code in (401, 403, 404)
                permanent |= type(cause).__name__ in {'AgentConfigurationError', 'LLMCapabilityNotSupportedError'}
                cause = cause.__cause__
            if permanent and failure == 'model_call_failed':
                failure = 'model_configuration_failed'
            if trace is not None:
                await trace.update(agentErrorType=type(error).__name__)
                await trace.write_payload(
                    f'analysis_output{suffix}',
                    {
                        **attempt,
                        'errorType': type(error).__name__,
                        'failureCode': failure,
                        'elapsedMs': elapsed_ms,
                        'transportRetries': retries,
                    },
                )
            logger.warning(
                '体检报告模型调用未完成: %s output_recovery=%s failure=%s error_type=%s elapsed_ms=%s',
                log_context,
                attempt.get('outputRecovery', False),
                failure,
                type(error).__name__,
                elapsed_ms,
            )
            return _AnalysisAttempt(failure=failure)
        diagnostics = {}
        data = self._parse_reply(result.data.get('content'), diagnostics=diagnostics)
        metrics.jsonSyntaxRecoveries += int(bool(diagnostics.get('recovery')))
        elapsed_ms = round((asyncio.get_running_loop().time() - started) * 1000)
        metrics.modelElapsedMs += elapsed_ms
        if attempt.get('outputRecovery'):
            metrics.recoveryElapsedMs += elapsed_ms
        usage = result.data.get('usage')
        usage = usage if isinstance(usage, dict) else {}
        metrics.usageMetricsKnown &= all(
            type(usage.get(key)) is int for key in ('promptTokens', 'completionTokens', 'totalTokens')
        )
        completion_tokens = usage.get('completionTokens')
        if type(completion_tokens) is int:
            metrics.completionTokens += completion_tokens
        for field in ('promptTokens', 'totalTokens'):
            if type(usage.get(field)) is int:
                setattr(metrics, field, getattr(metrics, field) + usage[field])
        retries = result.data.get('transport_retries')
        metrics.transportMetricsKnown &= type(retries) is int
        if type(retries) is int:
            metrics.transportRetries += retries
        output_limit = result.data.get('output_token_limit')
        limit_suspected = (
            data is None
            and type(output_limit) is int
            and type(completion_tokens) is int
            and completion_tokens >= output_limit
        )
        validation = (
            self._validate_reply(data, text=source_text if source_text is not None else text, attempt=attempt)
            if data is not None
            else None
        )
        if trace is not None:
            await trace.write_payload(
                f'analysis_output{suffix}',
                {
                    **{key: result.data.get(key) for key in ('content', 'provider', 'model', 'usage', 'status')},
                    **attempt,
                    'jsonParse': diagnostics,
                    'elapsedMs': elapsed_ms,
                    'maxOutputTokens': output_limit,
                    'outputLimitSuspected': limit_suspected,
                    'transportRetries': retries,
                    'validation': None
                    if validation is None
                    else {
                        'valid': validation.valid,
                        'issues': validation.issues,
                        'diagnostics': validation.diagnostics,
                    },
                },
            )
        if data is None:
            truncated = limit_suspected or (
                diagnostics['errorType'] == 'JSONDecodeError' and 'Unterminated' in diagnostics['message']
            )
            logger.warning(
                '体检报告%s: %s output_recovery=%s error_type=%s reason=%r line=%s column=%s position=%s '
                'reply_chars=%s provider=%r model=%r prompt_tokens=%s completion_tokens=%s total_tokens=%s '
                'max_output_tokens=%s output_limit_suspected=%s elapsed_ms=%s',
                '模型输出截断' if truncated else '模型回复 JSON 解析失败',
                log_context,
                attempt.get('outputRecovery', False),
                diagnostics['errorType'],
                diagnostics['message'],
                diagnostics.get('line'),
                diagnostics.get('column'),
                diagnostics.get('position'),
                diagnostics['replyChars'],
                result.data.get('provider'),
                result.data.get('model'),
                usage.get('promptTokens'),
                completion_tokens,
                usage.get('totalTokens'),
                output_limit,
                limit_suspected,
                elapsed_ms,
            )
            return _AnalysisAttempt(failure='truncated' if truncated else 'json')
        if not validation.valid:
            logger.warning(
                '体检报告 JSON 数据校验未通过: %s output_recovery=%s issue_count=%s issues=%s',
                log_context,
                attempt.get('outputRecovery', False),
                len(validation.issues),
                validation.issues,
            )
            return _AnalysisAttempt(failure='validation', validation=validation)
        pending_count = sum(len(node.get('unresolvedItems', [])) for node in nodes(validation.data))
        if pending_count:
            logger.info('体检报告项目结果待确认: %s item_count=%s', log_context, pending_count)
        return _AnalysisAttempt(data=validation.data, validation=validation)

    @staticmethod
    def _parse_reply(reply, *, diagnostics=None):
        """完整解析 JSON；只对字符串中的未转义空白作一次无损语法修复。"""
        if diagnostics is None:
            diagnostics = {}
        diagnostics.update(status='failed', replyChars=len(reply) if isinstance(reply, str) else None)
        if not isinstance(reply, str):
            diagnostics.update(errorType=AgentProtocolError.__name__, message='Expected text content')
            return None

        text = reply.strip()
        offset = len(reply) - len(reply.lstrip())
        # 仅接受完整包裹的 JSON 代码块，不截取大括号，也不补齐截断的对象或数组。
        fence = re.fullmatch(r'```(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```', text, re.IGNORECASE | re.DOTALL)
        if fence is not None:
            offset += fence.start(1)
            text = fence.group(1)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            # One local formatting repair, followed by a complete strict parse.
            # Never repair truncation, missing fields, numbers, or punctuation.
            repaired = escape_string_controls(text) if error.msg.startswith('Invalid control character') else None
            if repaired is not None:
                fixed, count = repaired
                try:
                    data = json.loads(fixed)
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(data, dict):
                        diagnostics.update(
                            status='parsed',
                            recovery={'kind': 'unescaped_string_controls', 'escapedControlCount': count},
                        )
                        return data
            original = json.JSONDecodeError(error.msg, reply, offset + error.pos)
            diagnostics.update(
                errorType=type(error).__name__,
                message=error.msg,
                line=original.lineno,
                column=original.colno,
                position=original.pos,
            )
            return None

        if not isinstance(data, dict):
            diagnostics.update(errorType=AgentProtocolError.__name__, message='JSON root must be an object')
            return None
        diagnostics['status'] = 'parsed'
        return data

    def _normalize(self, data, *, allow_sections=True, sources_validated=False):
        """Format validated model JSON for the existing API; do not inspect the source."""
        report_type = data.get('reportType') if data.get('reportType') in REPORT_TYPES else 'unknown'
        observations = data.get('observations')
        if observations is None and isinstance(data.get('expansion'), list):
            observations = data['expansion']
        result = {
            'reportType': report_type,
            'title': data.get('title'),
            'reportTime': data.get('reportTime'),
            'aiSummary': data.get('aiSummary'),
            'status': data.get('status') if data.get('status') in ('normal', 'abnormal', 'pending') else 'pending',
            'observations': self._normalize_observations(observations, sources_validated=sources_validated),
            'expansion': self._normalize_expansion(
                report_type, data.get('expansion'), sources_validated=sources_validated
            ),
            'unresolvedItems': deepcopy(data.get('unresolvedItems', [])),
        }
        if report_type in {'lab', 'microbiome'}:
            result['observations'] = self._merge_observation_arrays(result['expansion'], result['observations'])
        if allow_sections and data.get('sections'):
            parts = [
                self._normalize(part, allow_sections=False, sources_validated=sources_validated)
                for part in data['sections']
            ]
            result['sections'] = parts

            # Keep the established flat API projection without duplicating a
            # parent mirror of a child row. This only compares returned JSON fields.
            def identity(item, report_time):
                return (
                    *(item.get(key) for key in (*LAB_ITEM_KEYS, 'method', 'specimen', 'kind')),
                    normalize_document_time(item.get('observedAt') or report_time),
                )

            children = {identity(item, part['reportTime']) for part in parts for item in part['observations']}
            result['observations'] = [
                item for item in result['observations'] if identity(item, result['reportTime']) not in children
            ]
            for index, part in enumerate(parts):
                part['sectionIndex'] = index
                for item in part['observations']:
                    copied = deepcopy(item)
                    if not copied.get('observedAt') and part.get('reportTime'):
                        copied['observedAt'] = part['reportTime']
                    copied['sourceSection'] = index
                    result['observations'].append(copied)
            if report_type == 'imaging':
                for key in ('findings', 'diagnosis'):
                    if not result['expansion'].get(key):
                        values = [
                            part['expansion'].get(key)
                            for part in parts
                            if part['reportType'] == 'imaging' and isinstance(part.get('expansion'), dict)
                        ]
                        result['expansion'][key] = '\n'.join(value for value in values if value) or None
        if report_type in ('lab', 'microbiome'):
            result['expansion'] = deepcopy(result['observations'])
        return result

    @staticmethod
    def _merge_observation_arrays(expansion, observations):
        remaining = list(observations)
        merged = []
        for original in expansion:
            item = dict(original)
            for index, observation in enumerate(remaining):
                same_result = all(item.get(key) == observation.get(key) for key in ('itemName', 'resultValue', 'unit'))
                conflicting = any(
                    item.get(key) is not None and observation.get(key) is not None and item[key] != observation[key]
                    for key in item.keys() | observation.keys()
                )
                if same_result and not conflicting:
                    item.update({key: value for key, value in observation.items() if value is not None})
                    remaining.pop(index)
                    break
            merged.append(item)
        return merged + remaining

    @staticmethod
    def _normalize_observations(observations, *, sources_validated=False):
        if not isinstance(observations, list):
            return []
        items = []
        for item in observations:
            if not isinstance(item, dict):
                continue
            normalized = {key: item.get(key) for key in LAB_ITEM_KEYS}
            for key in ('observedAt', 'method', 'specimen'):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    normalized[key] = value.strip()
            if isinstance(item.get('kind'), str) and item['kind'] in {'measurement', 'assessment'}:
                normalized['kind'] = item['kind']
            if sources_validated:
                for key in ('sourceId', 'evidence', 'metadataEvidence', 'rawFlag'):
                    if key in item:
                        normalized[key] = deepcopy(item[key])
            items.append(normalized)
        return items

    @staticmethod
    def _normalize_expansion(report_type, expansion, *, sources_validated=False):
        """按报告类型校验并归一化 expansion。"""
        if report_type in {'lab', 'microbiome'}:
            if not isinstance(expansion, list):
                return []
            items = []
            for item in expansion:
                if not isinstance(item, dict):
                    continue
                items.extend(
                    HealthReportInterpreter._normalize_observations([item], sources_validated=sources_validated)
                )
            return items

        if report_type == 'imaging':
            if not isinstance(expansion, dict):
                return {'findings': None, 'diagnosis': None}
            return {
                'findings': expansion.get('findings'),
                'diagnosis': expansion.get('diagnosis'),
            }

        if report_type == 'checkup':
            if not isinstance(expansion, dict):
                return {'checkupOrgName': None, 'checkupTime': None}
            return {
                'checkupOrgName': expansion.get('checkupOrgName'),
                'checkupTime': expansion.get('checkupTime'),
            }

        return None


class HealthReportProcessingService:
    """复用体检报告校验、解析、解读、主体绑定和可靠保存流程。"""

    SUPPORTED_FORMATS = REPORT_EXTENSIONS

    def __init__(
        self,
        *,
        interpreter,
        report_source,
        health_profile_service,
        document_trace_store: DocumentTraceStore,
        image_format_probe: ImageFormatProbe | None = None,
        document_index=None,
        document_downloader=None,
        document_task_spawner=None,
        document_output_index=None,
        output_profiles=None,
        publication_ledger=None,
        current_interpreter=None,
    ) -> None:
        if not isinstance(document_trace_store, DocumentTraceStore):
            raise TypeError('document_trace_store must implement DocumentTraceStore')
        self._interpreter = interpreter
        self._current_interpreter = current_interpreter
        self._report_source = report_source
        self._publication_ledger = publication_ledger
        self._health_profile_service = health_profile_service
        self._image_format_probe = image_format_probe
        self._document_trace_store = document_trace_store
        from .output_profiles import DocumentOutputCoordinator
        from .output_profiles import DocumentOutputProfiles

        self.output_profiles = output_profiles or DocumentOutputProfiles()
        self._document_outputs = (
            None
            if document_output_index is None
            else DocumentOutputCoordinator(
                self,
                document_output_index,
                self.output_profiles,
                task_spawner=document_task_spawner,
            )
        )
        if (document_index is None) != (document_downloader is None):
            raise TypeError('document_index 与 document_downloader 必须同时配置')
        self.supports_document_reuse = document_index is not None
        self._document_reuse = None
        if document_index is not None:
            from .document_reuse import DocumentReuseCoordinator

            self._document_reuse = DocumentReuseCoordinator(
                self,
                document_index,
                document_downloader,
                task_spawner=document_task_spawner,
            )

    @property
    def _legacy_publication(self):
        from .legacy_publication import LegacyPublicationGateway

        return LegacyPublicationGateway(self._report_source, self._publication_ledger)

    async def process(self, **kwargs) -> dict:
        """Independent API projection; shared clinical callers still use process_document."""
        try:
            processed = await self._process(require_text=False, **kwargs)
        except HealthReportUpstreamError as error:
            cause, seen = error.__cause__, set()
            while cause is not None and id(cause) not in seen:
                seen.add(id(cause))
                if isinstance(cause, DocumentInterpretationError) and isinstance(cause.result, dict):
                    timed_out = any(
                        issue.get('code') in TECHNICAL_TIMEOUT_ISSUES
                        for issue in (cause.result.get('processing') or {}).get('issues', [])
                    )
                    if cause.content_outcome is not None or (not timed_out and has_displayable(cause.result)):
                        result = display_result(cause.result)
                        result.update({key: value for key, value in cause.result_context.items() if value is not None})
                        result.pop('sourceRevision', None)
                        result['processing'].pop('sourceRevision', None)
                        logger.info(
                            '体检报告内容处理结果: trace_id=%s content_outcome=%s status=%s',
                            cause.trace_id,
                            cause.content_outcome,
                            result['processing']['status'],
                        )
                        return result
                cause = cause.__cause__
            raise
        if processed.response_is_projected:
            return deepcopy(processed.report)
        return display_result(processed.report)

    async def process_document(self, **kwargs) -> ProcessedHealthReport:
        """Shared internal entry for callers that also need the complete parsed text."""
        return await self._process(require_text=True, **kwargs)

    async def prepare_document(
        self, *, user_id: str, subject_id: str, lifecycle_epoch: int | None = None, **kwargs
    ) -> ProcessedHealthReport:
        """Run the single current extraction without publication or caller version choices."""
        from .current_interpreter import CURRENT_HEALTH_REPORT_SELECTION

        subject_id = _required_processing_text('subjectId', subject_id, missing_message='缺少 subjectId')
        forbidden = {
            'publish',
            'require_text',
            'interpretation_revision',
            'output_selection',
            'output_profile',
            '_interpreter',
            '_current_interpreter',
            '_analysis_publication',
            '_archive_lifecycle_epoch',
            'report_type_hint',
        }
        if forbidden & kwargs.keys():
            raise ValueError('current preparation fixes extraction, scope and unpublished mode')
        if self._current_interpreter is None:
            raise DocumentParsingError('current report extraction is not configured')
        if self._current_interpreter.VERSION != CURRENT_HEALTH_REPORT_SELECTION.implementation_revision:
            raise DocumentParsingError('configured current report extraction has a different identity')
        if self._document_reuse is None:
            raise ValueError('文件引用服务未配置')
        outcome = await self._document_reuse.process(
            user_id=user_id,
            subject_id=subject_id,
            publish=False,
            require_text=True,
            _current_interpreter=self._current_interpreter,
            _archive_lifecycle_epoch=lifecycle_epoch,
            **kwargs,
        )
        snapshot = outcome.preparation_binding.input_snapshot
        return replace(
            outcome,
            input_snapshot=snapshot,
            output_selection=CURRENT_HEALTH_REPORT_SELECTION,
            structured_data=deepcopy(outcome.report),
        )

    async def read_prepared_round(
        self, *, user_id: str, subject_id: str, document_id: str, interpretation_round: int
    ) -> PreparedDocumentRound:
        """Read only this scope's fixed current round; never replace it with latest data."""
        from modules.analysis.preparation import DocumentPreparationBinding
        from modules.analysis.preparation import payload_digest

        from .current_interpreter import CURRENT_HEALTH_REPORT_SELECTION

        user_id = _required_processing_text('userId', user_id, missing_message='缺少 userId')
        subject_id = _required_processing_text('subjectId', subject_id, missing_message='缺少 subjectId')
        document_id = _required_processing_text('documentId', document_id, missing_message='缺少 documentId')
        if type(interpretation_round) is not int or interpretation_round < 0:
            raise ValueError('interpretation_round must be a nonnegative integer')
        if self._document_reuse is None:
            raise ValueError('文件引用服务未配置')
        reuse = self._document_reuse
        document = await reuse.index.get_archived_round(document_id, user_id, subject_id, interpretation_round)
        if document is None:
            raise DocumentNotFoundError('the fixed current round is unavailable in this scope')
        if (
            document.get('_id') != document_id
            or document.get('userId') != user_id
            or document.get('subjectId') != subject_id
            or document.get('interpretationResultRound') != interpretation_round
        ):
            raise DocumentParsingError('fixed round identity differs')
        revision = CURRENT_HEALTH_REPORT_SELECTION.implementation_revision
        if (
            document.get('interpretationVersion') != revision
            or document.get('interpretationValidatedVersion') != revision
        ):
            raise DocumentParsingError('fixed round is not a current validated extraction')
        parsed = await reuse._payload(document, 'docmind_result')
        interpretation = await reuse._payload(document, 'interpretation')
        if payload_digest(parsed) != document.get('parseDigest') or payload_digest(interpretation) != document.get(
            'interpretationDigest'
        ):
            raise DocumentParsingError('fixed round artifacts differ from their archived digests')
        if await reuse.index.get_archived_round(document_id, user_id, subject_id, interpretation_round) != document:
            raise DocumentParsingError('fixed round changed during read')
        return PreparedDocumentRound(
            user_id,
            subject_id,
            document_id,
            DocumentPreparationBinding.capture(document, parsed, interpretation),
            deepcopy(parsed),
            deepcopy(interpretation),
            CURRENT_HEALTH_REPORT_SELECTION,
        )

    async def process_documents(
        self,
        files,
        *,
        user_id,
        subject_id=None,
        output_profile=None,
        output_selection=None,
        source_type='report',
        source_id=None,
        session_id=None,
        resume_incomplete=True,
    ):
        """Ordered file-group extraction; every input keeps its own usage trace."""
        if not isinstance(files, (list, tuple)) or not files:
            raise ValueError('files 必须为非空文件列表')
        if self._document_outputs is None:
            raise ValueError('文件扩展结果存储未配置')
        profile = self._document_outputs.resolve_profile(
            output_profile=output_profile, output_selection=output_selection, default='report_source'
        )
        if profile.name == 'health_report':
            raise ValueError('health_report 按单份文件输出')
        results, references, seen = [], [], set()
        settled = set()
        for index, file in enumerate(files):
            if not isinstance(file, dict):
                raise ValueError('files 元素必须为对象')
            result = await self.process_document(
                file_id=file.get('url'),
                document_id=file.get('documentId'),
                file_type=file.get('fileType'),
                content=file.get('content'),
                file_name=file.get('fileName'),
                user_id=user_id,
                subject_id=subject_id,
                source_type=source_type,
                source_id=source_id,
                session_id=session_id,
                file_index=index,
                resume_incomplete=resume_incomplete,
                _settled_document_ids=settled,
            )
            references.append(
                {
                    'fileIndex': index,
                    'documentId': result.document_id,
                    'reportId': result.report['reportId'],
                    'reused': result.reused,
                }
            )
            if result.document_id not in seen:
                results.append(result)
                seen.add(result.document_id)
        output = await self._document_outputs.process(
            results,
            user_id=user_id,
            subject_id=results[0].report['subjectId'],
            output_selection=profile.selection,
        )
        return replace(output, documents=tuple(references))

    async def _process(
        self,
        *,
        require_text,
        output_profile=None,
        output_selection=None,
        report_type_hint=None,
        **kwargs,
    ) -> ProcessedHealthReport:
        if {'_current_interpreter', '_interpreter', 'current_interpreter', 'interpretation_revision'} & kwargs.keys():
            raise ValueError('the default entry does not accept execution selectors')
        if report_type_hint is not None and report_type_hint not in REPORT_TYPES:
            raise ValueError('report_type_hint 无效')
        if output_selection is not None and not isinstance(output_selection, OutputSelection):
            raise ValueError('output_selection 必须为 OutputSelection')
        if output_selection is not None and output_selection.profile_name == 'health_report':
            raise ValueError('health_report does not accept execution version selections')
        name = (
            output_profile
            if output_profile is not None
            else (output_selection.profile_name if output_selection is not None else 'health_report')
        )
        extension = name != 'health_report' or report_type_hint is not None or output_selection is not None
        profile = None
        if extension:
            if self._document_outputs is None or self._document_reuse is None:
                raise ValueError('文件扩展结果存储未配置')
            profile = self._document_outputs.resolve_profile(
                output_profile=output_profile, output_selection=output_selection
            )
        if self._document_reuse is not None:
            try:
                publication = kwargs.get('_analysis_publication')
                if getattr(publication, 'revision', None) == ANALYSIS_CURRENT_PUBLICATION:
                    from .current_interpreter import CURRENT_HEALTH_REPORT_SELECTION

                    if self._current_interpreter is None or (
                        self._current_interpreter.VERSION != CURRENT_HEALTH_REPORT_SELECTION.implementation_revision
                    ):
                        raise DocumentParsingError('current report extraction is not configured')
                    if extension:
                        raise ValueError('the current Analysis entry fixes its output selection')
                    kwargs['_current_interpreter'] = self._current_interpreter
                outcome = await self._document_reuse.process(require_text=require_text or extension, **kwargs)
                if (
                    extension
                    and profile.name == 'health_report'
                    and report_type_hint is None
                    and outcome.input_snapshot is not None
                    and outcome.input_snapshot.interpretation_revision == profile.version
                    and outcome.input_snapshot.validation_revision == profile.version
                ):
                    return replace(
                        outcome, structured_data=deepcopy(outcome.report), output_selection=profile.selection
                    )
                if extension:
                    return await self._document_outputs.process(
                        [outcome],
                        user_id=outcome.report['userId'],
                        subject_id=outcome.report['subjectId'],
                        output_selection=profile.selection,
                        report_type_hint=report_type_hint,
                    )
                return replace(outcome, structured_data=deepcopy(outcome.report))
            except DocumentParsingError as error:
                raise HealthReportUpstreamError(str(error)) from error
        if kwargs.pop('document_id', None) is not None:
            raise ValueError('文件引用服务未配置')
        return await self._process_legacy(require_text=require_text, **kwargs)

    async def read_document_payload(self, document_id, *, user_id, subject_id=None, name='original_file'):
        """Internal ownership-checked archive access for downstream document processors."""
        if self._document_reuse is None:
            raise ValueError('文件引用服务未配置')
        return await self._document_reuse.read_payload(document_id, user_id, subject_id, name)

    async def _process_legacy(
        self,
        *,
        file_id: str,
        user_id: str,
        subject_id: str | None = None,
        report_id: str | None = None,
        file_type: str | None = None,
        source_type: str = 'analysis',
        source_id: str | None = None,
        session_id: str | None = None,
        file_index: int | None = None,
        content: bytes | None = None,
        require_text: bool,
    ) -> ProcessedHealthReport:
        """处理一份报告；未指定主体时绑定用户唯一的本人主体。"""
        file_id = _required_processing_text(
            'fileId',
            file_id,
            missing_message='缺少必填参数: fileId',
        )
        parsed_url = urlparse(file_id)
        if parsed_url.scheme not in {'http', 'https'}:
            raise ValueError('fileId必须是HTTP或HTTPS协议的文件地址')
        user_id = _required_processing_text(
            'userId',
            user_id,
            missing_message='缺少必填参数: userId',
        )
        subject_id = _optional_processing_text('subjectId', subject_id)
        source_id = _optional_processing_text('source_id', source_id)
        session_id = _optional_processing_text('session_id', session_id)
        if source_type not in {'analysis', 'record', 'report'}:
            raise ValueError('source_type 必须为 analysis、record 或 report')
        if file_index is not None and (type(file_index) is not int or file_index < 0):
            raise ValueError('file_index 必须为非负整数')
        if content is not None and (not isinstance(content, bytes) or not content):
            raise ValueError('content 必须为非空 bytes')
        resolved_report_id = self._report_source.resolve_report_id(report_id)
        trace = DocumentProcessingTrace(self._document_trace_store, user_id=user_id)
        await trace.start(
            reportId=resolved_report_id,
            subjectId=subject_id,
            fileUrl=file_id,
            declaredFileType=file_type,
            source={'entrypoint': source_type, 'sourceId': source_id, 'sessionId': session_id, 'fileIndex': file_index},
            contentSha256=None if content is None else hashlib.sha256(content).hexdigest(),
        )
        try:
            return await self._process_traced(
                trace=trace,
                file_id=file_id,
                file_type=file_type,
                user_id=user_id,
                subject_id=subject_id,
                report_id=resolved_report_id,
                session_id=session_id,
                content=content,
                require_text=require_text,
            )
        except (Exception, asyncio.CancelledError) as error:
            if isinstance(error, DocumentInterpretationError):
                error.trace_id = trace.trace_id
                error.document_id = trace.trace_id
                error.result_context = {
                    'userId': user_id,
                    'subjectId': subject_id,
                    'reportId': resolved_report_id,
                    'documentId': trace.trace_id,
                    **error.result_context,
                }
                if error.content_outcome == 'no_readable_text':
                    await trace.write_payload('interpretation', error.result)
            content_response = not require_text and getattr(error, 'content_outcome', None)
            try:
                await trace.update(
                    status=(
                        'cancelled'
                        if isinstance(error, asyncio.CancelledError)
                        else 'completed'
                        if content_response
                        else 'failed'
                    ),
                    errorType=None if content_response else type(error).__name__,
                    contentOutcome=getattr(error, 'content_outcome', None),
                    reasonCode=getattr(error, 'reason_code', None),
                )
            except Exception as archive_error:
                logger.error(
                    '文件处理失败状态保存失败: trace_id=%s, error_type=%s', trace.trace_id, type(archive_error).__name__
                )
                if getattr(error, 'content_outcome', None):
                    # This is the legacy path's durable outcome classification.
                    # It must succeed before an explanatory result is returned.
                    raise
            if isinstance(error, DocumentParsingError):
                raise HealthReportUpstreamError(str(error)) from error
            raise

    async def _process_traced(
        self, *, trace, file_id, file_type, user_id, subject_id, report_id, session_id, content, require_text
    ) -> ProcessedHealthReport:
        filename, inferred_file_type = self._interpreter._resolve_name(file_id)
        resolved_file_type, requested_image = await self._resolve_file_type(
            file_id, file_type, inferred_file_type, content
        )
        filename = filename if inferred_file_type else f'health-report.{resolved_file_type}'
        await trace.update(stage='binding_subject', fileName=filename, resolvedFileType=resolved_file_type)
        if subject_id is None:
            bootstrap = await self._health_profile_service.ensure_self_profile(user_id)
            subject_id = bootstrap.subject.subject_id
        await trace.update(stage='checking_existing', subjectId=subject_id)
        fingerprint_file_id = file_id if inferred_file_type else f'{file_id}#fileType={resolved_file_type}'
        if content is not None:
            fingerprint_file_id += f'#contentSha256={hashlib.sha256(content).hexdigest()}'
        source_fingerprint = self._report_source.source_fingerprint(
            user_id=user_id,
            subject_id=subject_id,
            file_id=fingerprint_file_id,
        )
        await trace.update(sourceFingerprint=source_fingerprint)
        lookup_options = {}
        if content is None and not inferred_file_type and requested_image and resolved_file_type == 'jpg':
            lookup_options['source_fingerprint_aliases'] = (
                self._report_source.source_fingerprint(
                    user_id=user_id,
                    subject_id=subject_id,
                    file_id=f'{file_id}#fileType=jpeg',
                ),
            )
        existing = await self._report_source.get_existing(
            report_id=report_id,
            source_fingerprint=source_fingerprint,
            **lookup_options,
        )
        if existing is not None:
            return await self._reuse_record(trace, existing.record, user_id, subject_id, require_text)

        try:
            text = await self._interpreter.extract_document(
                trace=trace,
                file_url=file_id,
                filename=filename,
                file_type=resolved_file_type,
                content=content,
            )
        except DocumentInterpretationError as error:
            error.result_context['subjectId'] = subject_id
            raise
        await trace.update(stage='interpreting')
        result = await self._interpreter.analyze(
            text,
            user_id=user_id,
            subject_id=subject_id,
            session_id=session_id,
            trace=trace,
            source_snapshot={'content': content, 'fileName': filename},
        )
        if result is None:
            raise DocumentInterpretationError('体检报告未获得可用结果')
        await trace.write_payload('interpretation', result)
        if result.get('processing', {}).get('status') == 'unavailable':
            error = DocumentInterpretationError(
                '体检报告未获得可用结果',
                processing=result.get('processing'),
                result=result,
                content_outcome=classify_outcome(result),
                retryable=result.get('processing', {}).get('retryable', True),
            )
            if error.content_outcome is None:
                error.content_failure = False
            error.result_context['subjectId'] = subject_id
            raise error
        await trace.update(stage='saving_report', sourceFingerprint=source_fingerprint)
        saved = await self._legacy_publication.save(
            report_id=report_id,
            user_id=user_id,
            subject_id=subject_id,
            source_fingerprint=source_fingerprint,
            result=result,
            document_id=trace.trace_id,
        )
        record = saved.record
        # A concurrent caller may have saved the same report first. Return its text and interpretation together.
        document_id = getattr(record, 'document_id', None)
        if (document_id is not None and document_id != trace.trace_id) or not getattr(saved, 'created', True):
            return await self._reuse_record(trace, record, user_id, subject_id, require_text)
        await trace.update(
            stage='completed',
            status='completed',
            storedDocumentId=document_id,
            profileIngestion={
                'status': getattr(record, 'ingestion_status', None),
                'jobId': getattr(record, 'ingestion_job_id', None),
            },
        )
        return ProcessedHealthReport(
            report=record.public_result(),
            text=text,
            document_id=document_id,
            trace_id=trace.trace_id,
            ingestion_status=getattr(record, 'ingestion_status', None),
            ingestion_job_id=getattr(record, 'ingestion_job_id', None),
            reused=False,
        )

    async def _reuse_record(self, trace, record, user_id, subject_id, require_text):
        document_id = getattr(record, 'document_id', None)
        text = None
        if require_text:
            if document_id is None:
                raise HealthReportUpstreamError('历史报告缺少原文归档，请使用新的 reportId 显式重新解析')
            parsed = await self._document_trace_store.read_payload(document_id, user_id, subject_id, 'docmind_result')
            text = parsed.get('text') if isinstance(parsed, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise HealthReportUpstreamError('报告原文归档不可用')
        await trace.update(
            stage='reused',
            status='reused',
            reusedDocumentId=document_id,
            profileIngestion={
                'status': getattr(record, 'ingestion_status', None),
                'jobId': getattr(record, 'ingestion_job_id', None),
            },
        )
        return ProcessedHealthReport(
            report=record.public_result(),
            text=text,
            document_id=document_id,
            trace_id=trace.trace_id,
            ingestion_status=getattr(record, 'ingestion_status', None),
            ingestion_job_id=getattr(record, 'ingestion_job_id', None),
            reused=True,
        )

    async def _resolve_file_type(self, file_id, file_type, inferred_file_type, content):
        requested_file_type = _optional_processing_text(
            'fileType',
            file_type,
        )
        if requested_file_type is not None:
            requested_file_type = requested_file_type.removeprefix('.').lower()
        requested_image = requested_file_type == 'image'
        if content is not None:
            if requested_file_type and not requested_image and requested_file_type not in self.SUPPORTED_FORMATS:
                raise HealthReportUnsupportedFormatError('不支持的文件格式')
            actual = _content_file_type(content)
            if actual is None:
                raise HealthReportUnsupportedFormatError('已下载内容不是受支持的 PDF 或图片')
            expected = inferred_file_type or (None if requested_image else requested_file_type)
            if (expected and not _same_file_type(expected, actual)) or (requested_image and actual == 'pdf'):
                raise ValueError('fileType 或文件 URL 与已下载的文件类型不一致')
            if requested_file_type and not requested_image and not _same_file_type(requested_file_type, actual):
                raise ValueError('fileType 与已下载的文件类型不一致')
            return inferred_file_type or actual, requested_image
        if requested_image:
            if inferred_file_type == 'pdf':
                raise ValueError('fileType=image 与文件 URL 中的 PDF 扩展名不一致')
            if inferred_file_type:
                if inferred_file_type not in IMAGE_EXTENSIONS:
                    raise HealthReportUnsupportedFormatError(
                        f'不支持的图片格式: {inferred_file_type}',
                    )
                requested_file_type = inferred_file_type
            else:
                if self._image_format_probe is None:
                    raise HealthReportUpstreamError('图片文件格式探测服务未配置')
                try:
                    requested_file_type = await self._image_format_probe.detect(file_id)
                except UnsupportedImageFormatError as error:
                    raise HealthReportUnsupportedFormatError(str(error)) from error
                except ImageProbeError as error:
                    raise HealthReportUpstreamError(str(error)) from error
                if requested_file_type not in IMAGE_EXTENSIONS:
                    raise HealthReportUnsupportedFormatError('不支持的图片格式')
        if (
            inferred_file_type
            and requested_file_type
            and not _same_file_type(
                inferred_file_type,
                requested_file_type,
            )
        ):
            raise ValueError('fileType 与文件 URL 中的扩展名不一致')
        resolved_file_type = requested_file_type or inferred_file_type
        if not resolved_file_type:
            raise ValueError(
                '无法识别文件格式，请提供文件扩展名或 fileType',
            )
        if resolved_file_type not in self.SUPPORTED_FORMATS:
            raise HealthReportUnsupportedFormatError(
                f'不支持的文件格式: {resolved_file_type}（仅支持 PDF 与图像）',
            )

        return inferred_file_type or resolved_file_type, requested_image


def _required_processing_text(
    name: str,
    value: str,
    *,
    missing_message: str,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(missing_message)
    return value.strip()


def _optional_processing_text(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} 必须为非空字符串')
    return value.strip()


def _same_file_type(left: str, right: str) -> bool:
    aliases = {'jpg': 'jpeg', 'jpeg': 'jpeg'}
    return aliases.get(left, left) == aliases.get(right, right)


def _content_file_type(content: bytes) -> str | None:
    if content[:1024].lstrip().startswith(b'%PDF-'):
        return 'pdf'
    for prefix, extension in (
        (b'\xff\xd8\xff', 'jpg'),
        (b'\x89PNG\r\n\x1a\n', 'png'),
        (b'GIF87a', 'gif'),
        (b'GIF89a', 'gif'),
        (b'BM', 'bmp'),
    ):
        if content.startswith(prefix):
            return extension
    return None
