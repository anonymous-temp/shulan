"""Current full extraction followed by a separately checkpointed basic-info check."""

import asyncio
from copy import deepcopy

from modules.analysis.contracts import OutputSelection

from .analysis_outcomes import empty_analysis
from .analysis_validation import ValidationResult
from .basic_info import attach_basic_info
from .basic_info import basic_info_projections
from .basic_info import empty_basic_info
from .basic_info import merge_basic_check
from .basic_info import merge_basic_info
from .extraction import analysis_limiter
from .service import HealthReportInterpreter
from .service import _collect_analysis_parts

CURRENT_HEALTH_REPORT_SELECTION = OutputSelection('health_report', '2', 'analysis-health-report-v13')
_CHECK_PREFIX = (
    '第二阶段仅核查下面同一原文中的受检者姓名、性别、实际年龄；不重做临床抽取。'
    '仍返回完整JSON外形，reportType=unknown、status=pending、observations=[]、expansion=null；'
    '只在basicInfo/basicInfoMeta填写原文可支持的字段、low/medium、理由及证据。\n'
)
_REPAIR_PREFIX = '请仅修复输出的JSON格式或字段结构，完整重发原文中实际存在的信息，不猜测。\n'


class CurrentHealthReportInterpreter(HealthReportInterpreter):
    VERSION = 'analysis-health-report-v13'
    CAPABILITY = 'analysis.health_report.current'
    PROFILE = 'analysis.health_report.current'

    def _validate_reply(self, data, *, text, attempt):
        result = super()._validate_reply(data, text=text, attempt=attempt)
        basic_issues = attach_basic_info(
            result.data,
            data,
            text=text,
            offset=attempt.get('source', {}).get('start', 0),
        )
        # Basic-info proof/schema defects are independently recoverable. They
        # must not consume clinical repairs or invalidate settled clinical data.
        result.diagnostics.extend({'scope': 'basic_info', **issue} for issue in basic_issues)
        return result

    def _normalize(self, data, **kwargs):
        result = super()._normalize(data, **kwargs)
        values, metadata = empty_basic_info()
        result['basicInfo'] = deepcopy(data.get('basicInfo', values))
        result['basicInfoMeta'] = deepcopy(data.get('basicInfoMeta', metadata))
        if data.get('_profileSourcePatient') is not None:
            result['_profileSourcePatient'] = deepcopy(data['_profileSourcePatient'])
        if data.get('_basicInfoMerged') is not True:
            merge_basic_info(basic_info_projections([data]), result)
        result['_basicInfoMerged'] = True
        return result

    @staticmethod
    def _basic_evidence(previous, candidate):
        # Each complete reply keeps its own section-level patient constraints;
        # only the two already-validated projections share this source attempt.
        return merge_basic_check(
            merge_basic_info(basic_info_projections([previous]), {}),
            merge_basic_info(basic_info_projections([candidate]), {}),
        )

    def _checkpoint_identity(self, prepared, chunk, report_type_hint, trace):
        return {
            **self._chunk_identity(prepared, chunk, report_type_hint),
            'interpretationRound': getattr(trace, 'document', {}).get('interpretationRound', 0),
            'promptRevision': self.VERSION,
            'basicInfoContract': 'analysis.basic-info.v1',
        }

    async def _read_checkpoint(self, trace, name, identity):
        if trace is None or not hasattr(trace, 'read_payload'):
            return {}
        saved = await trace.read_payload(name)
        return saved if isinstance(saved, dict) and saved.get('identity') == identity else {}

    async def _save_checkpoint(self, trace, name, state):
        if trace is not None:
            await trace.write_payload(name, deepcopy(state))

    async def _call_part(
        self, chunk, key, identity, *, phase, user_id, session_id, subject_id, trace, report_type_hint, metrics
    ):
        if phase not in ('first', 'repair', 'basic_check'):
            raise ValueError('unknown current analysis phase')
        suffix = {'first': f'_{key}', 'repair': f'_{key}_repair', 'basic_check': f'_{key}_0'}[phase]
        prefix = self._hint_prefix(report_type_hint)
        if phase == 'repair':
            prefix += _REPAIR_PREFIX
        elif phase == 'basic_check':
            prefix += _CHECK_PREFIX
        waiting = asyncio.get_running_loop().time()
        async with analysis_limiter():
            queue_ms = round((asyncio.get_running_loop().time() - waiting) * 1000)
            metrics.queueWaitMs += queue_ms
            metrics.modelCalls += 1
            metrics.inputChars += len(prefix) + len(chunk.text)
            metrics.outputRecoveryCalls += int(phase == 'repair')
            return await self._invoke_analysis(
                prefix + chunk.text,
                source_text=chunk.text,
                user_id=user_id,
                session_id=session_id,
                subject_id=subject_id,
                trace=trace,
                suffix=suffix,
                attempt={
                    'chunkId': key,
                    'source': identity,
                    'phase': phase,
                    'queueWaitMs': queue_ms,
                    'outputRecovery': phase == 'repair',
                },
                log_context=f'trace_id={getattr(trace, "trace_id", None)!r} chunk_id={key}',
                metrics=metrics,
            )

    async def _analyze_part(
        self, prepared, chunk, key, *, user_id, session_id, subject_id, trace, report_type_hint, metrics, single=False
    ):
        identity = self._checkpoint_identity(prepared, chunk, report_type_hint, trace)
        name = f'analysis_part_{key}'
        saved = await self._read_checkpoint(trace, name, identity)
        if saved.get('firstPhaseDone') and isinstance(saved.get('result'), dict):
            metrics.reusedParts += 1
            return {**deepcopy(saved['result']), '_verifiedData': deepcopy(saved.get('rawData', empty_analysis()))}
        state = {'identity': identity, 'validationVersion': self.VERSION, **saved}
        raw = deepcopy(state.get('rawData', empty_analysis()))
        issues = list(state.get('issues', []))
        used = state.get('firstPhaseCalls', 0)
        while used < 2:
            phase = 'first' if used == 0 else 'repair'
            used += 1
            state.update(firstPhaseCalls=used, firstPhaseStarted=True)
            await self._save_checkpoint(trace, name, state)
            attempt = await self._call_part(
                chunk,
                key,
                identity,
                phase=phase,
                user_id=user_id,
                session_id=session_id,
                subject_id=subject_id,
                trace=trace,
                report_type_hint=report_type_hint,
                metrics=metrics,
            )
            if attempt.validation is not None:
                candidate = attempt.validation.data
                basic = self._basic_evidence(raw, candidate)
                if (
                    phase == 'first'
                    or attempt.validation.valid
                    or attempt.validation.accepted_count >= ValidationResult(raw, issues).accepted_count
                ):
                    raw = deepcopy(candidate)
                    if raw.get('sections'):
                        raw['_profileSourcePatient'] = {
                            key: deepcopy(raw[key]) for key in ('basicInfo', 'basicInfoMeta')
                        }
                    issues = list(attempt.validation.issues)
                # Clinical candidate selection never discards validated demographic
                # proofs, including patient ambiguity on otherwise empty containers.
                raw.update(basic)
                raw['_basicInfoMerged'] = True
            elif attempt.failure:
                issues.append({'code': attempt.failure, 'sourceIds': [], 'recoverable': True})
            state.update(rawData=deepcopy(raw), issues=issues)
            await self._save_checkpoint(trace, name, state)
            if attempt.failure not in ('validation', 'json', 'truncated'):
                break
        if not state.get('rawData'):
            issues = [{'code': 'analysis_deadline', 'sourceIds': [], 'recoverable': True}]
        result = self._settle_part(raw, [{**issue, 'chunkId': key} for issue in issues])
        state.update(firstPhaseDone=True, rawData=raw, result=result)
        await self._save_checkpoint(trace, name, state)
        return {**result, '_verifiedData': deepcopy(raw)}

    async def _after_parts(
        self,
        results,
        *,
        prepared,
        failures,
        user_id,
        session_id,
        subject_id,
        trace,
        report_type_hint,
        metrics,
        deadline_at,
    ):
        # No optional work can occupy the first-stage worker slots.
        if failures or asyncio.get_running_loop().time() >= deadline_at:
            return results
        tasks, indexes = [], []
        for index, result in enumerate(results):
            if any(meta.get('status') != 'extracted' for meta in result.get('basicInfoMeta', {}).values()):
                indexes.append(index)
                tasks.append(
                    self._check_basic(
                        result,
                        prepared,
                        prepared.chunks[index],
                        f'{index:05d}',
                        user_id=user_id,
                        session_id=session_id,
                        subject_id=subject_id,
                        trace=trace,
                        report_type_hint=report_type_hint,
                        metrics=metrics,
                    )
                )
        if not tasks:
            return results
        checked, _ = await _collect_analysis_parts(
            tasks,
            timeout=max(0, deadline_at - asyncio.get_running_loop().time()),
        )
        for index, replacement in zip(indexes, checked):
            results[index] = (
                replacement
                if replacement is not None
                else self._limited_check(results[index], '定向核查达到预算或中断，保留首阶段判断。')
            )
        return results

    @staticmethod
    def _limited_check(result, reason):
        result = deepcopy(result)
        for node in (result, result.get('_verifiedData', {})):
            for meta in node.get('basicInfoMeta', {}).values():
                if meta.get('status') != 'extracted' and reason not in meta.get('reason', ''):
                    meta['reason'] = meta.get('reason', '')[: max(0, 2000 - len(reason))] + reason[:2000]
        return result

    async def _check_basic(
        self, previous, prepared, chunk, key, *, user_id, session_id, subject_id, trace, report_type_hint, metrics
    ):
        identity = self._checkpoint_identity(prepared, chunk, report_type_hint, trace)
        name = f'analysis_part_{key}_0'
        saved = await self._read_checkpoint(trace, name, identity)
        if saved.get('done'):
            return deepcopy(saved['result'])
        if saved.get('started'):
            # An uncertain call already consumed this phase's single business attempt.
            return self._limited_check(previous, '原核查已消耗单次额度，结果未确认，保留首阶段判断。')
        state = {'identity': identity, 'started': True}
        await self._save_checkpoint(trace, name, state)
        attempt = await self._call_part(
            chunk,
            key,
            identity,
            phase='basic_check',
            user_id=user_id,
            session_id=session_id,
            subject_id=subject_id,
            trace=trace,
            report_type_hint=report_type_hint,
            metrics=metrics,
        )
        raw = attempt.validation.data if attempt.validation is not None else {}
        first_raw = deepcopy(previous.get('_verifiedData', previous))
        first_raw.setdefault('basicInfo', deepcopy(previous.get('basicInfo', empty_basic_info()[0])))
        first_raw.setdefault('basicInfoMeta', deepcopy(previous.get('basicInfoMeta', empty_basic_info()[1])))
        merged = deepcopy(first_raw)
        merged.update(self._basic_evidence(first_raw, raw))
        merged['_basicInfoMerged'] = True
        # Optional checking never rewrites clinical items or discards valid first-stage work.
        result = self._settle_part(merged, previous.get('processing', {}).get('issues', []))
        result['_verifiedData'] = merged
        if attempt.failure:
            result = self._limited_check(result, '定向核查未通过，保留首阶段可确认内容。')
        state.update(done=True, result=result, failure=attempt.failure)
        await self._save_checkpoint(trace, name, state)
        return result
