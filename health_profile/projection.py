"""把规范化健康事实投影为面向 API/UI 的只读结构。"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date
from datetime import datetime
from typing import Any

from agents.graphs.health_profile.focus import association_anchor
from agents.graphs.health_profile.focus import canonical_type
from agents.graphs.health_profile.focus import fact_rank
from agents.graphs.health_profile.focus import is_abnormal
from agents.graphs.health_profile.focus import is_background
from agents.graphs.health_profile.focus import is_report_observation
from agents.graphs.health_profile.focus import source_flag

from .indicators import BY_TYPE
from .models import FactCategory
from .models import FactStatus
from .models import FactValueType
from .models import HealthFact
from .models import HealthProfileView
from .models import HealthSubject
from .observation_dates import observation_recency
from .observation_dates import observation_timestamp
from .observation_dates import recorded_report_source
from .result_judgment import indicator_result_judgment

_LATEST_ACTIVE_FACT_CATEGORIES = {
    FactCategory.DEMOGRAPHIC,
    FactCategory.ANTHROPOMETRIC,
    FactCategory.LABORATORY,
    FactCategory.ASSESSMENT,
    FactCategory.REPORT_OBSERVATION,
}
_FACT_GROUPS = (
    (FactCategory.DEMOGRAPHIC, '人口统计'),
    (FactCategory.ANTHROPOMETRIC, '身体测量'),
    (FactCategory.LABORATORY, '检验指标'),
    (FactCategory.ASSESSMENT, '报告评估'),
    (FactCategory.REPORT_OBSERVATION, '未标准化检测结果'),
    (FactCategory.CONDITION, '疾病与诊断'),
    (FactCategory.SYMPTOM, '症状与不适'),
    (FactCategory.MEDICATION, '当前用药'),
    (FactCategory.ALLERGY, '过敏信息'),
    (FactCategory.LIFESTYLE, '生活方式'),
    (FactCategory.FAMILY_HISTORY, '家族史'),
    (FactCategory.GOAL, '健康目标'),
)
_FACT_STATUS_ORDER = {
    FactStatus.ACTIVE: 0,
    FactStatus.PENDING: 1,
    FactStatus.DISPUTED: 2,
}


def public_subject(subject: HealthSubject) -> dict[str, Any]:
    """只返回调用方需要的主体字段，隐藏内部幂等元数据。"""
    return {
        'subjectId': subject.subject_id,
        'kind': subject.kind.value,
        'displayName': subject.display_name,
        'relationship': (
            None if subject.relationship is None else subject.relationship.model_dump(mode='json', by_alias=True)
        ),
        'status': subject.status.value,
        'version': subject.version,
    }


def public_fact(fact: HealthFact) -> dict[str, Any]:
    """返回事实公共契约，隐藏租户标识和内部幂等元数据。"""
    return {
        'factId': fact.fact_id,
        'factType': fact.fact_type,
        'category': fact.category.value,
        'displayName': fact.display_name,
        'valueType': fact.value_type.value,
        'value': fact.value,
        'interpretation': fact.interpretation,
        'referenceRange': fact.reference_range,
        'assertionLevel': fact.assertion_level.value,
        'verificationStatus': fact.verification_status.value,
        'confidence': fact.confidence,
        'status': fact.status.value,
        'effectivePeriod': fact.effective_period.model_dump(
            mode='json',
            by_alias=True,
        ),
        'sources': [source.model_dump(mode='json', by_alias=True) for source in fact.sources],
        'version': fact.version,
        'updatedAt': _isoformat(fact.updated_at),
    }


def build_profile_projection(
    *,
    user_id: str,
    subject: HealthSubject,
    view: HealthProfileView,
    facts: tuple[HealthFact, ...],
    indicator_limit: int,
    generated_at: datetime,
    indicator_scope: str = 'all',
    fact_statuses: frozenset[FactStatus] | None = None,
) -> dict[str, Any]:
    """从当前事实构建确定性画像，不把展示摘要当成医学诊断。

    `indicator_scope` 筛选指标分组，`fact_statuses` 仅筛选事实分组。
    计数统计全画像的待确认与争议事实，摘要及关键提醒基于生效事实。
    """
    active = tuple(fact for fact in facts if fact.status is FactStatus.ACTIVE)
    current = current_profile_facts(active)
    pending_count = sum(fact.status in {FactStatus.PENDING, FactStatus.DISPUTED} for fact in facts)
    overview = _build_overview(active, generated_at.date())
    overview['pendingConfirmationCount'] = pending_count

    return {
        'userId': user_id,
        'subject': public_subject(subject),
        'profileVersion': view.profile_version,
        'schemaVersion': view.schema_version,
        'updatedAt': _isoformat(view.updated_at),
        'overview': overview,
        'indicatorGroups': _build_indicator_groups(
            facts,
            indicator_limit=indicator_limit,
            indicator_scopes=_normalize_indicator_scopes(indicator_scope),
        ),
        'factGroups': _build_fact_groups(facts, fact_statuses=fact_statuses),
        'alerts': _build_alerts(current),
        'summary': _build_summary(
            current,
            profile_version=view.profile_version,
            generated_at=generated_at,
        ),
    }


_INDICATOR_SCOPES = frozenset({'all', 'abnormal', 'flagged', 'pending', 'unjudged'})


def _normalize_indicator_scopes(value: str | frozenset[str] | None) -> frozenset[str]:
    """兼容旧的单值调用方；多值以逗号分隔字符串表达。"""
    if value is None:
        return frozenset({'all'})
    if isinstance(value, frozenset):
        scopes = {str(item) for item in value}
    else:
        text = str(value).strip()
        if not text:
            return frozenset({'all'})
        scopes = {part.strip().casefold() for part in text.split(',') if part.strip()}
    unsupported = scopes - _INDICATOR_SCOPES
    if not scopes or unsupported:
        raise ValueError(f'不支持的指标筛选范围: {sorted(unsupported) or sorted(scopes)!r}')
    if 'all' in scopes:
        return frozenset({'all'})
    return frozenset(scopes)


def evidence_fact_payloads(
    facts: tuple[HealthFact, ...],
) -> list[dict[str, Any]]:
    """当前事实供解释使用；正常值可作上下文，背景评分不参与循证。"""
    payloads = []
    for fact in current_profile_facts(facts):
        payload = _explanation_fact(fact)
        if fact.category is FactCategory.DEMOGRAPHIC or is_background(payload):
            continue
        # 未标准化结果只有来源异常或明确检出时才可作为具体线索。
        if is_report_observation(payload) and not association_anchor(payload):
            continue
        payloads.append(payload)
    return payloads


def current_profile_facts(facts: tuple[HealthFact, ...]) -> tuple[HealthFact, ...]:
    """先选择当前测量再判断异常；实体事实不按通用 factType 合并。"""
    latest: dict[tuple[FactCategory, str], HealthFact] = {}
    entities = []
    for fact in facts:
        if fact.status is not FactStatus.ACTIVE:
            continue
        if fact.category not in _LATEST_ACTIVE_FACT_CATEGORIES:
            entities.append(fact)
            continue
        key = (fact.category, canonical_type(fact.fact_type))
        previous = latest.get(key)
        if previous is None or _fact_recency_key(fact) > _fact_recency_key(previous):
            latest[key] = fact
    return tuple(sorted([*latest.values(), *entities], key=lambda fact: fact_rank(_explanation_fact(fact))))


def _explanation_fact(fact: HealthFact) -> dict[str, Any]:
    return {
        'factId': fact.fact_id,
        'factType': fact.fact_type,
        'category': fact.category.value,
        'displayName': fact.display_name,
        'value': fact.value,
        'interpretation': fact.interpretation,
        'referenceRange': fact.reference_range,
        'observedAt': _isoformat(fact.effective_period.start),
        'assertionLevel': fact.assertion_level.value,
        'verificationStatus': fact.verification_status.value,
        'sourceTypes': sorted({source.source_type for source in fact.sources}),
    }


def _build_overview(
    facts: tuple[HealthFact, ...],
    today: date,
) -> dict[str, Any]:
    latest = _latest_by_fact_type(facts)
    gender = _fact_value(
        latest,
        (
            'demographic.sex',
            'demographic.gender',
            'gender',
        ),
    )
    birth_value = _fact_value(
        latest,
        (
            'demographic.date_of_birth',
            'demographic.birth_date',
            'date_of_birth',
        ),
    )
    recorded_age = _quantity_value(
        latest,
        (
            'demographic.age',
            'age',
        ),
    )
    height = _overview_measurement(
        latest,
        (
            'anthropometric.height',
            'height',
        ),
        target_unit='cm',
    )
    weight = _overview_measurement(
        latest,
        (
            'anthropometric.weight',
            'weight',
        ),
        target_unit='kg',
    )
    birth_date = _parse_date(birth_value)
    age = _calculate_age(birth_date, today) if birth_date is not None else _normalized_age(recorded_age)
    bmi = None
    if height is not None and weight is not None and height > 0:
        height_m = height / 100
        bmi = round(weight / (height_m * height_m), 1)
    return {
        'gender': gender,
        'dateOfBirth': (None if birth_date is None else birth_date.isoformat()),
        'age': age,
        'heightCm': height,
        'weightKg': weight,
        'bmi': bmi,
    }


def _build_indicator_groups(
    facts: tuple[HealthFact, ...],
    *,
    indicator_limit: int,
    indicator_scopes: frozenset[str] = frozenset({'all'}),
) -> list[dict[str, Any]]:
    include_active = bool(indicator_scopes & {'all', 'abnormal', 'flagged', 'unjudged'})
    include_pending = 'pending' in indicator_scopes
    if not include_active and not include_pending:
        return []

    grouped: dict[str, list[HealthFact]] = defaultdict(list)
    pending_grouped: dict[str, list[HealthFact]] = defaultdict(list)
    for fact in facts:
        if fact.category in {
            FactCategory.LABORATORY,
            FactCategory.ANTHROPOMETRIC,
            FactCategory.ASSESSMENT,
            FactCategory.REPORT_OBSERVATION,
        }:
            if fact.status is FactStatus.ACTIVE:
                grouped[fact.fact_type].append(fact)
            elif include_pending and fact.status in {FactStatus.PENDING, FactStatus.DISPUTED}:
                pending_grouped[fact.fact_type].append(fact)
    if not grouped and not pending_grouped:
        return []

    indicators: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact_type in sorted(grouped):
        if not include_active:
            break
        observations = sorted(grouped[fact_type], key=_fact_recency_key)[-indicator_limit:]
        current = observations[-1]
        judgment = indicator_result_judgment(current)
        if not _current_matches_scopes(current, indicator_scopes, judgment):
            continue
        public_observations = [_observation(fact) for fact in observations]
        definition = BY_TYPE.get(fact_type)
        group = definition.group if definition else current.category.value
        recorded = _recorded_trend(grouped[fact_type]) if indicator_limit > 1 else None
        indicators[group].append(
            {
                'code': fact_type.rsplit('.', 1)[-1].upper(),
                'name': current.display_name,
                'factType': current.fact_type,
                'valueType': current.value_type.value,
                'kind': (
                    'assessment'
                    if current.category is FactCategory.ASSESSMENT
                    else 'unmapped'
                    if current.category is FactCategory.REPORT_OBSERVATION
                    else 'measurement'
                ),
                'current': _observation(current, judgment=judgment),
                'trend': _trend(observations) if group in {'laboratory', 'anthropometric'} else 'unknown',
                **({'recordedTrend': recorded} if recorded is not None else {}),
                'observations': public_observations,
            }
        )
    if include_pending:
        for group, entries in _pending_indicator_entries(pending_grouped).items():
            indicators[group].extend(entries)
            indicators[group].sort(key=lambda indicator: indicator['factType'])
    return [
        {
            'code': code,
            'name': name,
            'indicators': indicators[code],
        }
        for code, name in (
            ('anthropometric', '身体测量'),
            ('laboratory', '检验指标'),
            ('microbiome', '菌群检测'),
            ('assessment', '报告评估'),
            ('report_observation', '未标准化检测结果'),
        )
        if indicators.get(code)
    ]


def _current_matches_scopes(
    current: HealthFact, indicator_scopes: frozenset[str], judgment: dict[str, str | None]
) -> bool:
    """active 指标是否被所选筛选范围保留；all 之外按最新观测判定。"""
    if 'all' in indicator_scopes:
        return True
    payload = _explanation_fact(current)
    if 'flagged' in indicator_scopes and source_flag(payload) is not None:
        return True
    if 'unjudged' in indicator_scopes and judgment['status'] == 'unknown':
        return True
    # abnormal 是 flagged 的临床子集，背景类会被 source_flag 保留，需单独排除。
    return 'abnormal' in indicator_scopes and is_abnormal(payload)


def _pending_indicator_entries(
    grouped: dict[str, list[HealthFact]],
) -> dict[str, list[dict[str, Any]]]:
    """待确认/争议指标每个 factType 各取最新一条；无历史观测，不生成趋势。"""
    indicators: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact_type in sorted(grouped):
        latest_by_status: dict[FactStatus, HealthFact] = {}
        for fact in grouped[fact_type]:
            previous = latest_by_status.get(fact.status)
            if previous is None or _fact_recency_key(fact) > _fact_recency_key(previous):
                latest_by_status[fact.status] = fact
        for status in (FactStatus.DISPUTED, FactStatus.PENDING):
            fact = latest_by_status.get(status)
            if fact is None:
                continue
            definition = BY_TYPE.get(fact.fact_type)
            group = definition.group if definition else fact.category.value
            indicators[group].append(
                {
                    'code': fact_type.rsplit('.', 1)[-1].upper(),
                    'name': fact.display_name,
                    'factType': fact.fact_type,
                    'valueType': fact.value_type.value,
                    'kind': (
                        'assessment'
                        if fact.category is FactCategory.ASSESSMENT
                        else 'unmapped'
                        if fact.category is FactCategory.REPORT_OBSERVATION
                        else 'measurement'
                    ),
                    'current': _observation(fact, judgment=indicator_result_judgment(fact)),
                    'trend': 'unknown',
                    'observations': [_observation(fact)],
                }
            )
    return indicators


def _observation(
    fact: HealthFact,
    *,
    judgment: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    number, unit = _quantity_parts(fact.value)
    result = {
        'factId': fact.fact_id,
        'value': number if number is not None else fact.value,
        'unit': unit,
        'observedAt': _isoformat(fact.effective_period.start),
        'valueType': fact.value_type.value,
        'status': fact.status.value,
        'verificationStatus': fact.verification_status.value,
        'sources': [source.model_dump(mode='json', by_alias=True) for source in fact.sources],
        'comparator': fact.value.get('comparator') if isinstance(fact.value, dict) else None,
        'rawValue': (
            fact.value.get('raw', fact.value.get('number', number)) if isinstance(fact.value, dict) else fact.value
        ),
        'method': fact.value.get('method') if isinstance(fact.value, dict) else None,
        'specimen': fact.value.get('specimen') if isinstance(fact.value, dict) else None,
    }
    if fact.effective_period.start is None and (source := recorded_report_source(fact.sources)) is not None:
        result['recordedAt'] = _isoformat(source.recorded_at)
    if judgment is not None:
        if isinstance(fact.value, dict) and result['rawValue'] is not None:
            raw_unit = fact.value.get('unit')
            if isinstance(raw_unit, str) and raw_unit and raw_unit != unit:
                result['rawUnit'] = raw_unit
        result.update(
            {
                'referenceRange': _reference_range_text(fact.reference_range),
                'interpretation': fact.interpretation or 'unknown',
                'resultJudgment': judgment,
            }
        )
    return result


def _build_alerts(facts: tuple[HealthFact, ...]) -> list[dict[str, Any]]:
    alerts = []
    for fact in facts:
        if not is_abnormal(_explanation_fact(fact)):
            continue
        alerts.append(
            {
                'alertId': f'alert_{fact.fact_id}',
                'severity': ('critical' if fact.interpretation == 'critical' else 'important'),
                'title': f'{fact.display_name}指标异常',
                'factIds': [fact.fact_id],
                'status': 'active',
            }
        )
    return alerts


def _build_summary(
    facts: tuple[HealthFact, ...],
    *,
    profile_version: int,
    generated_at: datetime,
) -> dict[str, Any]:
    # 延迟导入：提醒和综述都复用当前事实选择，避免模块导入环。
    from .summary import build_profile_summary

    return build_profile_summary(
        facts=facts,
        profile_version=profile_version,
        generated_at=generated_at,
    )


def apply_profile_insights(
    projection: dict[str, Any],
    insights: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> None:
    """把已验证风险解释投影为提醒候选；综述在最终精选后统一生成。"""
    risk_insights = [insight for insight in insights if insight['kind'] == 'risk']
    if not risk_insights:
        return
    by_id = {fact['factId']: fact for fact in facts}
    covered = set()
    risk_alerts = []
    for insight in risk_insights:
        covered.update(insight['factIds'])
        identity = insight['topicId'] + '\n' + '\n'.join(sorted(insight['factIds']))
        risk_alerts.append(
            {
                'alertId': 'alert_focus_' + hashlib.sha256(identity.encode()).hexdigest()[:24],
                'severity': 'important',
                'title': '建议评估：' + insight['title'],
                'factIds': insight['factIds'],
                'status': 'active',
            }
        )
    remaining = [
        alert
        for alert in projection['alerts']
        if alert['severity'] == 'critical' or not set(alert['factIds']).issubset(covered)
    ]

    def alert_rank(alert):
        rank = min(fact_rank(by_id[key]) for key in alert['factIds'])
        return rank[0], alert['severity'] != 'critical', rank[1:], alert['alertId']

    projection['alerts'] = sorted([*remaining, *risk_alerts], key=alert_rank)


def _build_fact_groups(
    facts: tuple[HealthFact, ...],
    *,
    fact_statuses: frozenset[FactStatus] | None = None,
) -> list[dict[str, Any]]:
    statuses = (
        fact_statuses if fact_statuses is not None else {FactStatus.ACTIVE, FactStatus.PENDING, FactStatus.DISPUTED}
    )

    latest_active: dict[tuple[FactCategory, str], HealthFact] = {}
    selected: list[HealthFact] = []
    for fact in facts:
        if fact.status not in statuses:
            continue
        if fact.status is FactStatus.ACTIVE and fact.category in _LATEST_ACTIVE_FACT_CATEGORIES:
            key = (fact.category, fact.fact_type.casefold())
            current = latest_active.get(key)
            if current is None or _fact_recency_key(fact) > _fact_recency_key(current):
                latest_active[key] = fact
            continue
        selected.append(fact)
    selected.extend(latest_active.values())

    grouped: dict[FactCategory, list[HealthFact]] = defaultdict(list)
    for fact in selected:
        grouped[fact.category].append(fact)

    groups = []
    for category, name in _FACT_GROUPS:
        items = grouped.get(category)
        if not items:
            continue
        if category is FactCategory.CONDITION and any(fact.fact_type == 'condition.history_status' for fact in items):
            name = '病史说明' if all(fact.fact_type == 'condition.history_status' for fact in items) else '疾病与病史'
        items.sort(key=_fact_group_sort_key)
        groups.append(
            {
                'code': category.value,
                'name': name,
                'items': [public_fact(fact) for fact in items],
            }
        )
    return groups


def _fact_group_sort_key(fact: HealthFact) -> tuple[int, float, str, str]:
    return (
        _FACT_STATUS_ORDER[fact.status],
        -observation_timestamp(fact.effective_period.start),
        fact.fact_type.casefold(),
        fact.fact_id,
    )


def _fact_recency_key(fact: HealthFact) -> tuple[float, float, float, str]:
    return *observation_recency(fact), fact.fact_id


def _latest_by_fact_type(
    facts: tuple[HealthFact, ...],
) -> dict[str, HealthFact]:
    latest: dict[str, HealthFact] = {}
    for fact in sorted(
        facts,
        key=_fact_recency_key,
        reverse=True,
    ):
        latest.setdefault(fact.fact_type.casefold(), fact)
    return latest


def _fact_value(
    latest: dict[str, HealthFact],
    names: tuple[str, ...],
) -> Any:
    for name in names:
        fact = latest.get(name)
        if fact is not None:
            return fact.value
    return None


def _quantity_value(
    latest: dict[str, HealthFact],
    names: tuple[str, ...],
) -> float | int | None:
    value = _fact_value(latest, names)
    number, _ = _quantity_parts(value)
    return number


def _quantity_parts(value: Any) -> tuple[float | int | None, str | None]:
    if isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        return value, None
    if not isinstance(value, dict):
        return None, None
    number = value.get('normalizedNumber', value.get('number'))
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        number = None
    unit = value.get('normalizedUnit', value.get('unit'))
    if unit is not None and not isinstance(unit, str):
        unit = str(unit)
    return number, unit


def _overview_measurement(latest: dict[str, HealthFact], names: tuple[str, ...], *, target_unit: str):
    value = _fact_value(latest, names)
    if isinstance(value, dict) and value.get('comparator'):
        return None
    number, unit = _quantity_parts(value)
    if number is None:
        return None
    # 兼容既有无单位基本信息；新增报告测量在摄取时必须明确单位。
    scales = (
        {'cm': 1, '厘米': 1, 'm': 100, '米': 100, 'mm': 0.1, '毫米': 0.1}
        if target_unit == 'cm'
        else {'kg': 1, '千克': 1, '公斤': 1, 'g': 0.001, '克': 0.001}
    )
    scale = scales.get(unit.casefold() if unit else target_unit)
    return None if scale is None else round(number * scale, 6)


def _trend(observations: list[HealthFact]) -> str:
    dated = [fact for fact in observations if fact.effective_period.start is not None]
    if len(dated) < 2:
        return 'unknown'
    older, newer = dated[-2:]
    if observation_timestamp(older.effective_period.start) >= observation_timestamp(newer.effective_period.start):
        return 'unknown'
    return _quantity_trend(older, newer)


def _recorded_trend(observations: list[HealthFact]) -> dict[str, str] | None:
    reports: dict[str, list[tuple[float, HealthFact]]] = defaultdict(list)
    for fact in observations:
        if fact.effective_period.start is not None or fact.value_type is not FactValueType.QUANTITY:
            continue
        source = recorded_report_source(fact.sources)
        if source is not None:
            reports[source.source_id].append((observation_timestamp(source.recorded_at), fact))
    if len(reports) < 2:
        return None
    older, newer = sorted(reports.values(), key=lambda readings: max(date for date, _ in readings))[-2:]
    if len(older) != 1 or len(newer) != 1 or older[0][0] >= newer[0][0]:
        return None
    direction = _quantity_trend(older[0][1], newer[0][1])
    return None if direction == 'unknown' else {'direction': direction, 'basis': 'recordedAt'}


def _quantity_trend(older: HealthFact, newer: HealthFact) -> str:
    previous, previous_unit = _quantity_parts(older.value)
    current, current_unit = _quantity_parts(newer.value)
    if previous_unit != current_unit or not current_unit:
        return 'unknown'
    for key in ('comparator', 'method', 'specimen'):
        left = older.value.get(key) if isinstance(older.value, dict) else None
        right = newer.value.get(key) if isinstance(newer.value, dict) else None
        if (key == 'comparator' and (left or right)) or left != right:
            return 'unknown'
    if previous is None or current is None:
        return 'unknown'
    if current > previous:
        return 'up'
    if current < previous:
        return 'down'
    return 'stable'


def _reference_range_text(value: dict[str, Any] | None) -> str | None:
    if not value:
        return None
    text = value.get('text')
    if isinstance(text, str) and text.strip():
        return text.strip()
    low = value.get('low')
    high = value.get('high')
    unit = value.get('unit')
    if low is None and high is None:
        return None
    if low is None:
        bounds = f'\u2264{high}'
    elif high is None:
        bounds = f'\u2265{low}'
    else:
        bounds = f'{low}-{high}'
    return f'{bounds} {unit}' if unit else bounds


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _calculate_age(birth_date: date, today: date) -> int | None:
    if birth_date > today:
        return None
    return today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))


def _normalized_age(value: float | int | None) -> float | int | None:
    if value is None or not 0 <= value <= 150:
        return None
    return int(value) if float(value).is_integer() else value


def _isoformat(value: datetime) -> str:
    return None if value is None else value.isoformat().replace('+00:00', 'Z')
