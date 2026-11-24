"""Map a read-only profile snapshot into bounded, typed report inputs.

这是纯映射逻辑，没有任何 I/O。规则：

* 只有**本人**主体的 ``active`` 事实、且断言等级不低于「用户确认」，才允许
  进入 typed 指标输入（参与 BMI 与 missing_fields 计算）。
* ``pending`` / ``disputed`` 只进独立的「未确认线索」分区，不作为确定事实。
* 血亲主体一律只进「家族史」分区，绝不当作本人疾病；非血亲主体直接丢弃。
* 未在允许列表中的 fact_type 一律丢弃，模型不能据此自由扩写正式结论。
* 画像事实使用最低优先级，当前请求与报告来源始终优先。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from modules.report.health_assessment.normalization import IndicatorFact
from modules.report.ports import ReportProfileFact
from modules.report.ports import ReportProfileSnapshot

# 现有来源优先级：当前请求 1、菌群报告 2/3、会话 4。历史画像排在最后，
# 只在没有更近来源时才生效（排序键为 ``-priority`` 取最大）。
PROFILE_FACT_PRIORITY = 5

PROFILE_SOURCE_NAME = '长期健康画像'
PROFILE_SOURCE_TYPE = 'health_profile'

# 允许进入 typed 指标输入的人体测量学事实：fact_type -> (指标编码, 期望单位)
ANTHROPOMETRIC_ALLOW_LIST: dict[str, tuple[str, str]] = {
    'anthropometric.height': ('height', 'cm'),
    'anthropometric.weight': ('weight', 'kg'),
    'anthropometric.waist': ('waist', 'cm'),
}

# 允许作为本人历史背景的事实：fact_type -> 中文分类标签
BACKGROUND_ALLOW_LIST: dict[str, str] = {
    'condition.diagnosis': '既往诊断',
    'condition.history_status': '个人病史说明',
    'symptom.reported': '既往症状',
    'goal.health': '本次健康关注',
    'medication.current': '当前用药',
    'allergy.substance': '过敏史',
    'lifestyle.smoking_status': '生活方式',
    'lifestyle.alcohol_use': '生活方式',
    'lifestyle.exercise_habit': '生活方式',
    'lifestyle.sleep_quality': '生活方式',
    'lifestyle.diet_pattern': '生活方式',
    'lifestyle.meal_regularity': '生活方式',
    'lifestyle.hydration': '生活方式',
    'lifestyle.physical_activity': '生活方式',
    'lifestyle.sedentary_behavior': '生活方式',
    'lifestyle.sleep': '生活方式',
    'lifestyle.stress_coping': '生活方式',
}

# 家族史事实：无论挂在哪个主体上，都只能作为家族史
FAMILY_HISTORY_ALLOW_LIST: dict[str, str] = {
    'family_history.condition': '家族史',
    'family_history.reported': '家族史说明',
}

SELF_SUBJECT_KIND = 'self'
BLOOD_RELATIVE_KIND = 'blood_relative'

# 允许进入 typed 输入的断言等级。模型推断与用户口述只作为背景文本，
# 不参与 BMI 等确定性计算。
TYPED_ASSERTION_LEVELS = frozenset(
    {
        'observed',
        'clinician_recorded',
        'user_confirmed',
    }
)

ACTIVE_STATUS = 'active'
UNCONFIRMED_STATUSES = frozenset({'pending', 'disputed'})

# 分区预算：条数与单条长度都有上限，超出即标记截断。
MAX_ITEMS_PER_PARTITION = 12
MAX_ITEM_CHARS = 160


@dataclass(frozen=True)
class ProfileContextPartitions:
    """三个互不混淆的画像分区。"""

    background: list[str] = field(default_factory=list)
    family_history: list[str] = field(default_factory=list)
    unconfirmed: list[str] = field(default_factory=list)
    truncated: bool = False


def profile_indicator_facts(
    snapshot: ReportProfileSnapshot | None,
) -> list[IndicatorFact]:
    """把本人已确认的人体测量学事实转成最低优先级的指标事实。"""
    if snapshot is None:
        return []
    output: list[IndicatorFact] = []
    for order, fact in enumerate(snapshot.facts):
        if fact.subject_kind != SELF_SUBJECT_KIND:
            continue
        if fact.status != ACTIVE_STATUS:
            continue
        if fact.assertion_level not in TYPED_ASSERTION_LEVELS:
            continue
        if 'health_report' in fact.sources and fact.verification_status not in {'verified_source', 'confirmed'}:
            continue
        mapping = ANTHROPOMETRIC_ALLOW_LIST.get(fact.fact_type)
        if mapping is None:
            continue
        code, expected_unit = mapping
        quantity = _quantity(fact.value)
        if quantity is None or fact.value.get('comparator'):
            continue
        number, unit = quantity
        if unit.casefold() != expected_unit.casefold():
            # 单位不符不做换算：宁可缺失，也不引入静默的换算错误。
            continue
        output.append(
            IndicatorFact(
                code=code,
                name=fact.display_name or code,
                raw_value=f'{number}{unit}',
                numeric_value=float(number),
                unit=unit,
                date=_date_of(fact),
                source=PROFILE_SOURCE_NAME,
                source_type=PROFILE_SOURCE_TYPE,
                priority=PROFILE_FACT_PRIORITY,
                order=order,
            )
        )
    return output


def profile_context_partitions(
    snapshot: ReportProfileSnapshot | None,
) -> ProfileContextPartitions:
    """把允许的事实渲染成三个有预算、可审计的文本分区。"""
    if snapshot is None:
        return ProfileContextPartitions()

    background: list[str] = []
    family: list[str] = []
    unconfirmed: list[str] = []
    dropped = False

    for fact in snapshot.facts:
        if fact.subject_kind not in {SELF_SUBJECT_KIND, BLOOD_RELATIVE_KIND} or fact.status not in {
            ACTIVE_STATUS,
            *UNCONFIRMED_STATUSES,
        }:
            continue
        family_label = FAMILY_HISTORY_ALLOW_LIST.get(fact.fact_type)
        if family_label is not None:
            _append(family, _render(fact, family_label))
            continue
        if fact.subject_kind == BLOOD_RELATIVE_KIND:
            # 血亲的其他事实只能作为家族史线索，不能成为本人事实。
            label = BACKGROUND_ALLOW_LIST.get(fact.fact_type)
            if label is not None:
                _append(family, _render(fact, f'血亲{label}'))
            continue
        if fact.subject_kind != SELF_SUBJECT_KIND:
            # 朋友、非血亲家属的健康事实与本次报告无关。
            continue
        label = BACKGROUND_ALLOW_LIST.get(fact.fact_type)
        report_observation = 'health_report' in fact.sources and fact.fact_type.startswith(
            (
                'anthropometric.',
                'laboratory.',
                'assessment.',
                'report_observation.',
                'condition.',
            )
        )
        if report_observation:
            label = '既往体检报告'
        if label is None:
            continue
        if fact.status == ACTIVE_STATUS and (
            not report_observation or fact.verification_status in {'verified_source', 'confirmed'}
        ):
            _append(background, _render(fact, label))
        elif fact.status in UNCONFIRMED_STATUSES:
            _append(unconfirmed, _render(fact, label))

    for bucket in (background, family, unconfirmed):
        if len(bucket) > MAX_ITEMS_PER_PARTITION:
            del bucket[MAX_ITEMS_PER_PARTITION:]
            dropped = True

    return ProfileContextPartitions(
        background=background,
        family_history=family,
        unconfirmed=unconfirmed,
        truncated=dropped or snapshot.truncated,
    )


def _render(fact: ReportProfileFact, label: str) -> str:
    """渲染单条事实，保留断言等级与时间，不隐藏其可信度。"""
    parts = [label, _value_text(fact)]
    when = _date_of(fact)
    if when:
        parts.append(when)
    # active 也不等于医生确诊，断言等级必须随文本一起呈现。
    parts.append(_ASSERTION_LABELS.get(fact.assertion_level, fact.assertion_level))
    if fact.status != ACTIVE_STATUS:
        parts.append(_STATUS_LABELS.get(fact.status, fact.status))
    return '｜'.join(part for part in parts if part)[:MAX_ITEM_CHARS]


_ASSERTION_LABELS = {
    'observed': '客观观测',
    'clinician_recorded': '医生记录',
    'user_confirmed': '用户确认',
    'user_reported': '用户自述',
    'model_inferred': '模型推断',
}

_STATUS_LABELS = {
    'pending': '待确认',
    'disputed': '存在冲突',
}


def _value_text(fact: ReportProfileFact) -> str:
    quantity = _quantity(fact.value)
    if quantity is not None:
        number, unit = quantity
        if 'health_report' not in fact.sources:
            return f'{fact.display_name}{number}{unit}'.strip()
        result = fact.value.get('raw') or f'{fact.value.get("comparator", "")}{number}'
        qualifiers = [fact.value.get(key) for key in ('specimen', 'method')]
        return ' '.join(str(part) for part in (fact.display_name, result, unit, *qualifiers) if part)
    return profile_fact_text(fact.fact_type, fact.display_name, fact.value)


def profile_fact_text(fact_type: str, display_name: str, value: Any) -> str:
    """Retain Record answer polarity, owner and time without diagnosing it."""
    if isinstance(value, dict):
        description = value.get('description')
        parts = [description.strip()] if isinstance(description, str) and description.strip() else []
        states = {'absent': '自述无此项', 'unknown': '情况不清楚', 'conflicting': '表述存在冲突'}
        presence = value.get('presence')
        state = states.get(presence.casefold()) if isinstance(presence, str) else None
        status = value.get('status')
        normalized_status = status.casefold() if isinstance(status, str) else None
        if value.get('negation') is True or normalized_status == 'denied':
            state = '自述无此项'
        elif value.get('unknown') is True or normalized_status == 'unknown':
            state = '情况不清楚'
        elif normalized_status == 'conflicting':
            state = '表述存在冲突'
        if state:
            parts.append(state)
        if value.get('specificity') == 'unspecified' or normalized_status == 'unspecified':
            parts.append('具体内容未明确')
        temporal = {'resolved': '已缓解', 'recovered': '已缓解', 'past': '既往记录', 'historical': '既往记录'}.get(
            normalized_status
        )
        if temporal:
            parts.append(temporal)
        elif status is not None and normalized_status not in {'denied', 'unknown', 'conflicting', 'unspecified'}:
            parts.append('状态：' + str(status))
        if fact_type.startswith('family_history.'):
            relationship = value.get('relationship')
            parts.append(
                '亲属：'
                + (relationship if isinstance(relationship, str) and relationship.strip() else '具体血亲未明确')
            )
        # Preserve the remaining structured details, including literal 0/false.
        details = {
            key: item
            for key, item in value.items()
            if key not in {'description', 'presence', 'specificity', 'relationship', 'status', 'negation', 'unknown'}
        }
        if details:
            parts.append(json.dumps(details, ensure_ascii=False, separators=(',', ':')))
        if not parts:
            parts.append(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
        return display_name + '：' + '；'.join(parts)
    if isinstance(value, bool):
        value = '是' if value else '否'
    if isinstance(value, list):
        value = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        if text and text != display_name:
            return f'{display_name}：{text}'
    return display_name


def _quantity(value: Any) -> tuple[float, str] | None:
    if not isinstance(value, dict):
        return None
    number = value.get('number')
    unit = value.get('unit')
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        return None
    if not isinstance(unit, str) or not unit.strip():
        return None
    return float(number), unit.strip()


def _date_of(fact: ReportProfileFact) -> str | None:
    dates = (fact.effective_start,) if 'health_report' in fact.sources else (fact.effective_start, fact.updated_at)
    for candidate in dates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:10]
    return None


def _append(bucket: list[str], text: str) -> None:
    if text and text not in bucket:
        bucket.append(text)


__all__ = [
    'ANTHROPOMETRIC_ALLOW_LIST',
    'BACKGROUND_ALLOW_LIST',
    'FAMILY_HISTORY_ALLOW_LIST',
    'PROFILE_FACT_PRIORITY',
    'ProfileContextPartitions',
    'profile_context_partitions',
    'profile_indicator_facts',
    'profile_fact_text',
]
