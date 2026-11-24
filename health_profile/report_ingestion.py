"""体检报告结构化结果到健康事实的确定性摄取。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from utils.log import getLogger
from utils.report_flags import report_flag_interpretation

from .fact_writer import HealthProfileFactWriter
from .indicators import BY_ALIAS
from .indicators import normalize_indicator_name
from .indicators import report_observation_type
from .ingestion import HealthFactPolicy
from .ingestion import PolicyDisposition
from .models import AssertionLevel
from .models import EffectivePeriod
from .models import FactCategory
from .models import FactSource
from .models import FactValueType
from .models import HealthProfileJob
from .models import MemoryCandidate
from .models import ProfileIngestionSourceType
from .models import SubjectStatus
from .models import VerificationStatus
from .processing import ProfileIngestionProcessingError
from .processing import ProfileIngestionProcessingResult
from .report_source import HealthReportSourceRecord
from .report_source import HealthReportStore
from .repository import HealthProfileRepository

logger = getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReportMappingResult:
    candidates: tuple[MemoryCandidate, ...]
    source_item_count: int
    skipped_item_count: int
    reason_codes: tuple[str, ...]


_CHINA_TIMEZONE = timezone(timedelta(hours=8))


class HealthReportCandidateMapper:
    """映射报告原始检测结果；不从 AI 摘要生成患者事实。"""

    VERSION = 'health-report-candidate-mapper-v2'

    def __init__(self) -> None:
        self._aliases = BY_ALIAS

    def map(
        self, record: HealthReportSourceRecord, *, analysis_recorded_at: datetime | None = None
    ) -> ReportMappingResult:
        if not isinstance(record, HealthReportSourceRecord):
            raise TypeError('record must be a HealthReportSourceRecord')
        entries = _report_observations(record.result)
        if not entries:
            reason = (
                'no_structured_observations'
                if record.result.get('reportType') in {'lab', 'microbiome', 'checkup'}
                else 'unsupported_report_type'
            )
            return ReportMappingResult((), 0, 0, (reason,))
        observed_at = _parse_report_time(record.result.get('reportTime'))

        candidates: list[MemoryCandidate] = []
        reasons: list[str] = []
        for pointer, item in entries:
            if not isinstance(item, dict):
                reasons.append('invalid_lab_item')
                continue
            item_name = _optional_text(item.get('itemName'))
            report_verification = item.get('verificationStatus')
            trusted = (
                analysis_recorded_at is not None
                and report_verification == VerificationStatus.CONFIRMED
                and item.get('confirmationBasis') == 'analysis_public'
            )
            if report_verification == VerificationStatus.CONFIRMED and not trusted:
                report_verification = VerificationStatus.UNCONFIRMED
            preserve_pending = report_verification is not None
            if item_name is None:
                reasons.append('missing_item_name')
                if not preserve_pending or not (item.get('evidence') or item.get('resultValue') is not None):
                    continue
                trusted = False
                item_name = '报告待核实项'
                report_verification = VerificationStatus.UNCONFIRMED
            definition = self._aliases.get(
                normalize_indicator_name(item_name),
            )
            if item.get('kind') == 'assessment' and definition and definition.category is not FactCategory.ASSESSMENT:
                definition = None
            narrative = (
                pointer.endswith(('/expansion/findings', '/expansion/diagnosis')) or item.get('kind') == 'narrative'
            )
            item_time = _observation_time(item, None if preserve_pending else observed_at)
            if item_time is None and not trusted:
                reasons.append('report_time_unavailable')
                if not preserve_pending:
                    continue
            raw_value = _optional_text(item.get('resultValue'))
            if raw_value is None or isinstance(item.get('resultValue'), (dict, list, bool)):
                reasons.append('missing_result_value')
                if not preserve_pending:
                    continue
                raw_value = None
                definition = None
                trusted = False
            quantity = _quantity_value(
                item.get('resultValue'),
                item.get('unit'),
            )
            if definition and definition.category is FactCategory.ANTHROPOMETRIC:
                # 不把缺少单位或定性描述的身高/体重写成概览可直接计算的标准测量。
                supported_units = {
                    'anthropometric.height': {'cm', '厘米', 'm', '米', 'mm', '毫米'},
                    'anthropometric.weight': {'kg', '千克', '公斤', 'g', '克'},
                }.get(definition.fact_type)
                if quantity is None or (
                    supported_units is not None and str(quantity.get('unit') or '').casefold() not in supported_units
                ):
                    definition = None
            if quantity is not None:
                for field in ('method', 'specimen', 'kind'):
                    text = _optional_text(item.get(field))
                    if text is not None:
                        quantity[field] = text
            fact_type = definition.fact_type if definition else report_observation_type(item)
            if narrative:
                fact_type = (
                    'condition.diagnosis'
                    if pointer.endswith('/diagnosis') or item.get('narrativeType') == 'diagnosis'
                    else 'condition.finding'
                )
                quantity = None
            elif definition is None:
                reasons.append('unmapped_item_preserved')
            if not trusted and (raw_value is None or item_time is None):
                if report_verification != VerificationStatus.CONFLICTED:
                    report_verification = VerificationStatus.UNCONFIRMED
            excerpt = (
                item['resultValue']
                if narrative
                else {
                    key: value
                    for key, value in item.items()
                    if key not in ('verificationStatus', 'confirmationBasis', 'evidence')
                }
            )
            excerpt_hash = _sha256_json(excerpt)
            candidate_hash = hashlib.sha256(
                f'{record.report_id}:{pointer}:{fact_type}'.encode(
                    'utf-8',
                ),
            ).hexdigest()[:20]
            candidates.append(
                MemoryCandidate(
                    candidate_id=f'report_lab_{candidate_hash}',
                    subject_id=record.subject_id,
                    fact_type=fact_type,
                    display_name=item_name,
                    value_type=FactValueType.QUANTITY if quantity is not None else FactValueType.TEXT,
                    value=quantity if quantity is not None else raw_value,
                    interpretation=_interpretation(item.get('flag')),
                    reference_range=_reference_range(
                        item.get('referenceRange'),
                        item.get('unit'),
                    ),
                    assertion_level=(
                        AssertionLevel.USER_REPORTED
                        if narrative and not trusted and report_verification != VerificationStatus.VERIFIED_SOURCE
                        else AssertionLevel.OBSERVED
                    ),
                    report_verification=report_verification,
                    confidence=0.8 if narrative else 0.95,
                    effective_period=EffectivePeriod(start=item_time),
                    source=FactSource(
                        source_type=ProfileIngestionSourceType.HEALTH_REPORT.value,
                        source_id=record.report_id,
                        source_revision=record.source_revision,
                        pointer=pointer,
                        observed_at=item_time,
                        excerpt_hash=excerpt_hash,
                        confirmation_basis='analysis_public' if trusted else None,
                        recorded_at=analysis_recorded_at if trusted and item_time is None else None,
                    ),
                )
            )
        return ReportMappingResult(
            candidates=tuple(candidates),
            source_item_count=len(entries),
            skipped_item_count=len(entries) - len(candidates),
            reason_codes=tuple(dict.fromkeys(reasons)),
        )


class HealthReportProfileProcessor:
    """把一个报告摄取任务可恢复地写入事实、事件和画像投影。"""

    GRAPH_VERSION = 'profile-ingest-health-report-v2'

    def __init__(
        self,
        *,
        store: HealthReportStore,
        repository: HealthProfileRepository,
        mapper: HealthReportCandidateMapper | None = None,
        policy: HealthFactPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        if not isinstance(store, HealthReportStore):
            raise TypeError('store must implement HealthReportStore')
        if not isinstance(repository, HealthProfileRepository):
            raise TypeError('repository must implement HealthProfileRepository')
        self._store = store
        self._repository = repository
        self._mapper = mapper or HealthReportCandidateMapper()
        self._policy = policy or HealthFactPolicy()
        self._writer = HealthProfileFactWriter(
            repository,
            clock=clock,
            id_factory=id_factory,
        )

    async def process(
        self,
        job: HealthProfileJob,
    ) -> ProfileIngestionProcessingResult:
        if not isinstance(job, HealthProfileJob):
            raise TypeError('job must be a HealthProfileJob')
        if job.source_type is not ProfileIngestionSourceType.HEALTH_REPORT:
            raise ProfileIngestionProcessingError(
                'source.unsupported',
                '当前处理器只支持体检报告任务',
            )
        guard = getattr(self._repository, 'is_source_publication_managed', None)
        if guard is not None and await guard(job.user_id, job.subject_id, job.source_type.value, job.source_id):
            return ProfileIngestionProcessingResult(
                graph_version=self.GRAPH_VERSION,
                policy_version=self._policy.VERSION,
                extractor_version=self._mapper.VERSION,
                candidate_count=0,
                fact_count=0,
                replayed_fact_count=0,
                rejected_candidate_count=0,
                reason_codes=('source.managed_by_publication',),
            )
        if job.source_revision > 1 and hasattr(self._store, 'get_revision'):
            record = await self._store.get_revision(job.source_id, job.source_revision)
        else:
            record = await self._store.get_report(job.source_id)
        if record is None:
            raise ProfileIngestionProcessingError(
                'source.not_found',
                '找不到摄取任务引用的体检报告',
            )
        if record.user_id != job.user_id or record.source_revision != job.source_revision:
            raise ProfileIngestionProcessingError(
                'source.mismatch',
                '体检报告来源与摄取任务不一致',
            )
        if job.subject_id is None and record.subject_id is None:
            # 兼容修复上线前已经错误投递的无主体报告任务。报告内容继续
            # 保留，但没有充分依据时不能把健康事实默认写入本人画像。
            logger.info(
                '体检报告画像摄取已跳过: '
                f'job_id={job.job_id}, '
                f'report_id={record.report_id}, '
                'reason_code=subject_unassigned',
            )
            return ProfileIngestionProcessingResult(
                graph_version=self.GRAPH_VERSION,
                policy_version=self._policy.VERSION,
                extractor_version=self._mapper.VERSION,
                candidate_count=0,
                fact_count=0,
                replayed_fact_count=0,
                rejected_candidate_count=0,
                reason_codes=('subject_unassigned',),
            )
        if job.subject_id is None or record.subject_id is None:
            raise ProfileIngestionProcessingError(
                'subject.unresolved',
                '体检报告来源与任务的健康主体状态不一致',
            )
        if job.subject_id != record.subject_id:
            raise ProfileIngestionProcessingError(
                'subject.mismatch',
                '体检报告主体与摄取任务不一致',
            )

        await self._repository.ensure_indexes()
        subject = await self._repository.get_subject(
            job.user_id,
            job.subject_id,
        )
        if subject is None:
            raise ProfileIngestionProcessingError(
                'subject.not_found',
                '找不到体检报告对应的健康主体',
            )
        if subject.status is not SubjectStatus.ACTIVE:
            raise ProfileIngestionProcessingError(
                'subject.inactive',
                '体检报告对应的健康主体不可写入',
            )

        mapping = self._mapper.map(record)
        if mapping.skipped_item_count or mapping.reason_codes:
            logger.warning(
                '体检报告画像映射包含说明: '
                f'job_id={job.job_id}, '
                f'report_id={record.report_id}, '
                f'source_item_count={mapping.source_item_count}, '
                f'skipped_item_count={mapping.skipped_item_count}, '
                f'reason_codes={mapping.reason_codes}',
            )
        facts = 0
        replayed = 0
        rejected = 0
        for candidate in mapping.candidates:
            decision = self._policy.evaluate(
                candidate,
                subject_kind=subject.kind,
            )
            if decision.disposition is PolicyDisposition.REJECTED:
                logger.warning(
                    '体检报告画像候选未写入: '
                    f'job_id={job.job_id}, '
                    f'candidate_id={candidate.candidate_id}, '
                    f'fact_type={candidate.fact_type}, '
                    f'disposition={decision.disposition.value}, '
                    f'reason_code={decision.reason_code}',
                )
                rejected += 1
                continue
            if decision.disposition is PolicyDisposition.NEEDS_SUBJECT_CONFIRMATION:
                raise ProfileIngestionProcessingError(
                    'subject.unresolved',
                    '候选事实的健康主体尚未确认',
                )
            created = await self._writer.write(
                job=job,
                candidate=candidate,
                decision=decision,
                subject=subject,
            )
            if created:
                facts += 1
            else:
                replayed += 1
        return ProfileIngestionProcessingResult(
            graph_version=self.GRAPH_VERSION,
            policy_version=self._policy.VERSION,
            extractor_version=self._mapper.VERSION,
            candidate_count=len(mapping.candidates),
            fact_count=facts,
            replayed_fact_count=replayed,
            rejected_candidate_count=rejected,
            source_item_count=mapping.source_item_count,
            skipped_item_count=mapping.skipped_item_count,
            reason_codes=mapping.reason_codes,
        )


def _parse_report_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=_CHINA_TIMEZONE) if value.tzinfo is None else value
    text = _optional_text(value)
    if text is None:
        return None
    text = re.sub(r'^(?:报告|检测|检验|检查|采样|采集)?(?:日期|时间)\s*[:：]\s*', '', text)
    normalized = text.replace('年', '-').replace('月', '-').replace('日', '')
    normalized = normalized.replace('/', '-').strip()
    if normalized.endswith('Z'):
        normalized = f'{normalized[:-1]}+00:00'
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
    if parsed is None:
        for pattern in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
            try:
                parsed = datetime.strptime(normalized, pattern)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=_CHINA_TIMEZONE)
    return parsed


def _quantity_value(raw_value: Any, raw_unit: Any) -> dict[str, Any] | None:
    text = _optional_text(raw_value)
    if text is None:
        return None
    numeric_pattern = r'[-+]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'
    match = re.fullmatch(
        rf'\s*(?P<comparator><=|>=|≤|≥|<|>)?\s*(?P<number>{numeric_pattern})\s*(?P<percent>%|％)?\s*',
        text,
    )
    if match is None:
        return None
    number = float(match.group('number').replace(',', ''))
    if not math.isfinite(number):
        return None
    if number.is_integer():
        number = int(number)
    value: dict[str, Any] = {
        'number': number,
        'raw': text,
    }
    unit = _optional_text(raw_unit)
    if unit is None and match.group('percent'):
        unit = '%'
    if unit is not None:
        value['unit'] = unit
    if match.group('comparator'):
        value['comparator'] = match.group('comparator')
    return value


def _reference_range(raw_value: Any, raw_unit: Any) -> dict[str, Any] | None:
    text = _optional_text(raw_value)
    if text is None:
        return None
    result: dict[str, Any] = {'text': text}
    unit = _optional_text(raw_unit)
    if unit is not None:
        result['unit'] = unit
    numbers = re.findall(r'\d+(?:\.\d+)?', text.replace(',', ''))
    if len(numbers) >= 2:
        result['low'] = float(numbers[0])
        result['high'] = float(numbers[1])
    return result


def _interpretation(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    normalized = text.casefold().strip()
    if '危急' in normalized or 'critical' in normalized:
        return 'critical'
    return report_flag_interpretation(text)


def _observation_time(item, report_time=None):
    """Scoped rows must never borrow a different subreport's date."""
    observed = _parse_report_time(item.get('observedAt'))
    if any(key in item for key in ('sourceSection', 'sourceCategory', 'sourceObservationId')):
        return observed
    return observed or (report_time if isinstance(report_time, datetime) else _parse_report_time(report_time))


def _report_observations(result: dict[str, Any], *, for_ingestion=True) -> list[tuple[str, Any]]:
    """优先保留旧 expansion 指针，防止历史任务重放生成重复事实。"""
    if for_ingestion and isinstance(result.get('ingestionObservations'), list):
        return [
            (f'/sourceObservations/{item["sourceObservationId"]}', item) for item in result['ingestionObservations']
        ]
    entries: list[tuple[str, Any]] = []
    seen: set[str] = set()
    expansion = result.get('expansion')
    arrays = []
    if isinstance(expansion, list):
        arrays.append(('/expansion', expansion))
    elif isinstance(expansion, dict) and isinstance(expansion.get('observations'), list):
        arrays.append(('/expansion/observations', expansion['observations']))
    observations = result.get('observations')
    if isinstance(observations, list):
        arrays.append(('/observations', observations))
    for path, items in arrays:
        array_identities: set[str] = set()
        for index, item in enumerate(items):
            if isinstance(item, dict):
                identity = _sha256_json(
                    {
                        key: item.get(key)
                        for key in (
                            'itemName',
                            'resultValue',
                            'unit',
                            'observedAt',
                            'method',
                            'specimen',
                            'kind',
                            'referenceRange',
                            'flag',
                        )
                    }
                )
                # 同一数组内的重复项保留原始位置；只去掉兼容字段之间的镜像。
                if path != arrays[0][0] and identity in seen:
                    continue
                array_identities.add(identity)
            entries.append((f'{path}/{index}', item))
        seen.update(array_identities)
    sections = result.get('sections')
    if isinstance(sections, list):
        for index, section in enumerate(sections):
            if not isinstance(section, dict) or not isinstance(section.get('expansion'), dict):
                continue
            for key, name in (('findings', '影像所见'), ('diagnosis', '影像诊断')):
                value = section['expansion'].get(key)
                if isinstance(value, str) and value.strip():
                    entries.append(
                        (
                            f'/sections/{index}/expansion/{key}',
                            {
                                'itemName': name,
                                'resultValue': value.strip(),
                                'observedAt': section.get('reportTime'),
                                'sourceSection': index,
                            },
                        )
                    )
    elif result.get('reportType') == 'imaging' and isinstance(expansion, dict):
        for key, name in (('findings', '影像所见'), ('diagnosis', '影像诊断')):
            value = expansion.get(key)
            if isinstance(value, str) and value.strip():
                entries.append((f'/expansion/{key}', {'itemName': name, 'resultValue': value.strip()}))
    if isinstance(result.get('reportSource'), dict):
        from utils.report_source_observations import report_source_items

        entries.extend(
            (f'/reportSource{pointer}', item) for pointer, item in report_source_items(result['reportSource'])
        )
    legacy_pointers = result.get('verifiedObservationPointers')
    if isinstance(legacy_pointers, list) and 'reportSource' not in result:
        # Old jobs and their raw index capture share the original publication scope.
        # A supplemental revision has no historical allowlist.
        allowed = {pointer for pointer in legacy_pointers if isinstance(pointer, str)}
        return [(pointer, item) for pointer, item in entries if pointer in allowed]
    return entries


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    normalized = value.strip()
    return normalized or None
