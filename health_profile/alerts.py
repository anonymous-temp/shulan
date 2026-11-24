"""精选关键提醒；报告数据复核与临床异常使用独立的入选规则。"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from decimal import Decimal
from decimal import InvalidOperation
from typing import Any

from agents.graphs.health_profile.focus import canonical_type
from agents.graphs.health_profile.focus import clinical_anchor
from agents.graphs.health_profile.focus import fact_rank
from agents.graphs.health_profile.focus import is_abnormal
from agents.graphs.health_profile.focus import is_background
from agents.graphs.health_profile.focus import source_flag
from agents.graphs.health_profile.interpretation import _clean_explanation_text

from .models import FactValueType
from .models import HealthFact
from .observation_dates import observation_recency
from .projection import current_profile_facts

MAX_IMPORTANT_ALERTS = 3
REVIEW_MULTIPLE = Decimal(5)
_NUMBER = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
_UNIT = r'[A-Za-zµμ%％][A-Za-zµμ%％/·⋅*^\d\s⁻¹²³-]*'
_INTERVAL = re.compile(rf'\s*({_NUMBER})\s*[-–—~～至]\s*({_NUMBER})\s*({_UNIT})?\s*')
_BOUND = re.compile(rf'\s*(<=|>=|<|>|≤|≥)\s*({_NUMBER})\s*({_UNIT})?\s*')
_BACKGROUND_NAME = re.compile(
    r'风险|评分|指数|多样性|年龄|合成|能力|模型|预测|易感|菌种数|菌属数|物种数|'
    r'菌群|有益菌|有害菌|综合评估|健康评估|'
    r'\b(?:score|index|diversity|age|risk|synthesis|species count)\b',
    re.IGNORECASE,
)
_ASSOCIATION_CONTENT = re.compile(
    r'丰度|浓度|水平|阳性|检出|相关|关联|感染|腹泻|胃肠炎|致病|'
    r'\b(?:abundance|concentration|levels?|positive|detected|detection|associated|association|'
    r'related|infection|diarr\w*|gastroenteritis|pathogen\w*)\b',
    re.IGNORECASE,
)
_MISMATCH = re.compile(r'不同菌种|不同检测|不能直接套用|不能直接用于|而非您的|并非您的')


def _payload(fact: HealthFact) -> dict[str, Any]:
    return {
        'factId': fact.fact_id,
        'factType': fact.fact_type,
        'category': fact.category.value,
        'displayName': fact.display_name,
        'value': fact.value,
        'interpretation': fact.interpretation,
    }


def _names(name: str) -> tuple[str, ...]:
    """只认完整名称和报告明确给出的括号别名，不扩展到近似菌名。"""
    parts = re.fullmatch(r'\s*([^()（）]+?)\s*[（(]([^()（）]+)[）)]\s*', name)
    if parts:
        aliases = tuple(part.strip() for part in parts.groups())
        if any(re.fullmatch(r'[A-Z][a-z]+(?: [a-z][a-z-]+)?', alias) for alias in aliases) and any(
            re.search(r'[\u4e00-\u9fff]', alias) for alias in aliases
        ):
            return aliases
    return (name.strip(),)


def _normalized_name(name: str) -> str:
    return re.sub(r'\s+', ' ', name.strip()).casefold()


def _specific_test(fact: HealthFact) -> bool:
    payload = _payload(fact)
    if not canonical_type(fact.fact_type).startswith('report_observation.'):
        return False
    if is_background(payload) or _BACKGROUND_NAME.search(fact.display_name):
        return False
    value = fact.value
    if value is None or (
        isinstance(value, Mapping) and all(value.get(field) is None for field in ('number', 'text', 'raw'))
    ):
        return False
    kind = value.get('kind') if isinstance(value, Mapping) else None
    if kind == 'measurement':
        return True
    if kind is not None:
        return False
    # 历史定性结果没有 kind；明确菌种/菌属名仍能识别具体检测对象。
    return any(
        re.fullmatch(r'[A-Z][a-z]+ [a-z][a-z-]+', name)
        or re.fullmatch(r'[\u4e00-\u9fff]+(?:菌|菌属|菌科|菌目|菌纲)', name)
        for name in _names(fact.display_name)
    )


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            return None
        numeric = float(result)
    except (InvalidOperation, ValueError):
        return None
    return result if math.isfinite(numeric) and (not result or numeric != 0) else None


def _unit(value: Any) -> str:
    return str(value or '').strip().replace('％', '%').replace('μ', 'µ')


def reference_deviation(fact: HealthFact) -> Decimal | None:
    """返回可信方向的偏离倍数；比较符、评分区间及不一致单位不计算。"""
    value, reference = fact.value, fact.reference_range
    if fact.value_type is not FactValueType.QUANTITY or not isinstance(value, Mapping) or not reference:
        return None
    if value.get('comparator') or re.search(r'[<>≤≥]', str(value.get('raw', ''))):
        return None
    number = _number(value.get('number'))
    if number is None or number < 0:
        return None
    value_unit = _unit(value.get('unit'))
    raw = value.get('raw')
    if raw is not None:
        parsed_raw = re.fullmatch(rf'\s*({_NUMBER})\s*({_UNIT})?\s*', str(raw))
        if not parsed_raw or _number(parsed_raw[1]) != number:
            return None
        raw_unit = _unit(parsed_raw[2])
        if value_unit and raw_unit and value_unit != raw_unit:
            return None
        value_unit = value_unit or raw_unit
    low, high = _number(reference.get('low')), _number(reference.get('high'))
    if any(reference.get(key) is not None and bound is None for key, bound in (('low', low), ('high', high))):
        return None
    text = reference.get('text')
    text_unit = ''
    if text:
        interval, bound = _INTERVAL.fullmatch(str(text)), _BOUND.fullmatch(str(text))
        if interval:
            parsed_low, parsed_high = _number(interval[1]), _number(interval[2])
            if parsed_low is None or parsed_high is None:
                return None
            text_unit = _unit(interval[3])
        elif bound:
            number_bound = _number(bound[2])
            if number_bound is None:
                return None
            parsed_low = number_bound if bound[1] in ('>', '>=', '≥') else None
            parsed_high = number_bound if parsed_low is None else None
            text_unit = _unit(bound[3])
        else:
            return None
        if (low is not None and low != parsed_low) or (high is not None and high != parsed_high):
            return None
        low, high = parsed_low, parsed_high
    reference_unit = _unit(reference.get('unit'))
    if reference_unit and text_unit and reference_unit != text_unit:
        return None
    if value_unit != (reference_unit or text_unit):
        return None
    if any(bound is not None and bound < 0 for bound in (low, high)):
        return None
    if low is not None and high is not None and low >= high:
        return None
    flag = source_flag(_payload(fact))
    if flag == 'high' and high is not None and high > 0 and number > high:
        return number / high
    if flag == 'low' and low is not None and low > 0 and number < low:
        return low / number if number else Decimal('Infinity')
    return None


def _matches_evidence(fact, insights, evidence_items):
    cited = {item['evidenceId'] for item in evidence_items if fact.fact_id in item.get('factIds', [])}
    for insight in insights:
        if insight.get('kind') != 'association' or fact.fact_id not in insight.get('factIds', []):
            continue
        if _MISMATCH.search(insight.get('explanation', '')):
            continue
        for support in insight.get('supports', []):
            if support.get('evidenceId') not in cited:
                continue
            # 引文已由解释校验逐字核验；这里再核对完整检测名及同句内容。
            for sentence in re.split(r'[。;；\n]|(?<=[a-z])\.(?=\s+[A-Z])', support.get('quote', '')):
                if len(re.sub(r'\s+', '', sentence)) < 12 or not _ASSOCIATION_CONTENT.search(sentence):
                    continue
                if re.search(r'相关评估需要结合|原始报告进行综合判断', sentence):
                    continue
                if re.search(
                    r'\b(?:SNPs?|polymorphisms?|genes?|strains?|antibod\w*|antigens?|IgG|IgM)\b|'
                    r'基因|菌株|抗体|抗原',
                    sentence,
                    re.IGNORECASE,
                ) and not re.search(r'\babundance\b|丰度|浓度|检出|\bdetected\b', sentence, re.IGNORECASE):
                    continue
                if any(_name_in_sentence(name, sentence) for name in _names(fact.display_name)):
                    return True
    return False


def _name_in_sentence(name, sentence):
    match = re.search(r'(?<![A-Za-z])' + re.escape(name) + r'(?![A-Za-z])', sentence, re.IGNORECASE)
    if not match:
        return False
    if re.fullmatch(r'[A-Z][a-z]+', name):
        following = re.match(r'\s+([a-z]+)\b', sentence[match.end() :])
        if following and following[1] not in {
            'abundance',
            'concentration',
            'levels',
            'is',
            'was',
            'were',
            'has',
            'had',
            'associated',
            'detected',
            'in',
            'relative',
            'and',
            'with',
            'infection',
            'can',
            'may',
        }:
            return False  # 菌属检测不能借用某一不同层级菌种的结果。
    return True


def _result_text(fact):
    value = fact.value
    if isinstance(value, Mapping):
        raw = value.get('raw')
        if raw is None:
            raw = value.get('text', value.get('number'))
        unit = _unit(value.get('unit'))
        return str(raw) + (unit if not unit or not str(raw).endswith(unit) else '')
    return str(value)


def _review_alert(fact):
    flag = {'high': '偏高', 'low': '偏低', 'positive': '阳性或检出'}.get(source_flag(_payload(fact)), '异常')
    reference = fact.reference_range or {}
    text = reference.get('text')
    if text and _unit(reference.get('unit')) and not str(text).endswith(_unit(reference.get('unit'))):
        text = str(text) + _unit(reference.get('unit'))
    if not text and (reference.get('low') is not None or reference.get('high') is not None):
        text = f'{reference.get("low", "未提供")}–{reference.get("high", "未提供")}' + _unit(reference.get('unit'))
    return {
        'alertId': 'alert_review_' + fact.fact_id,
        'severity': 'important',
        'title': (
            f'{fact.display_name}：报告标记{flag}，结果{_result_text(fact)}，'
            f'参考范围{text or "未提供"}；建议核对原报告。'
        ),
        'factIds': [fact.fact_id],
        'status': 'active',
    }


def current_alert_facts(facts):
    """提醒及综述共用的最新事实；明确同项含正常结果先覆盖旧观测。"""
    current = current_profile_facts(facts)
    # 同一个明确检测名的重复观测先取最新，正常结果也参与覆盖。
    aliases = {}
    for fact in current:
        names = tuple(_normalized_name(name) for name in _names(fact.display_name))
        shared = {aliases[name] for name in names if name in aliases}
        identity = min([*names, *shared])
        for alias, previous in tuple(aliases.items()):
            if previous in shared:
                aliases[alias] = identity
        for name in names:
            aliases[name] = identity

    def key(fact):
        value = fact.value if isinstance(fact.value, Mapping) else {}
        identity = canonical_type(fact.fact_type)
        if identity in {'condition.history_status', 'family_history.reported'}:
            conditions = value.get('conditions')
            names = (
                sorted({_normalized_name(name) for name in conditions if isinstance(name, str) and name.strip()})
                if isinstance(conditions, list)
                else []
            )
            relationship = value.get('relationship')
            relationship = _normalized_name(relationship) if isinstance(relationship, str) else ''
            description = value.get('description')
            # An unnamed condition or relative does not identify the same claim.
            # Keep its original scope instead of merging every answer-state row.
            unscoped = not names or (identity == 'family_history.reported' and not relationship)
            original = description.strip() if unscoped and isinstance(description, str) else ''
            if unscoped and not original:
                original = fact.fact_id
            identity += ':' + json.dumps(
                (fact.subject_id, names, relationship, original), ensure_ascii=False, separators=(',', ':')
            )
        elif identity.startswith('report_observation.'):
            identity = aliases[_normalized_name(_names(fact.display_name)[0])]
        elif fact.category.value in {'condition', 'symptom', 'allergy', 'medication'}:
            identity += ':' + _normalized_name(fact.display_name)
        return fact.category.value, identity, _unit(value.get('unit')), value.get('method'), value.get('specimen')

    def recency(item):
        return *observation_recency(item), item.version, item.fact_id

    latest = {}
    for fact in current:
        previous = latest.get(key(fact))
        if previous is None or recency(fact) > recency(previous):
            latest[key(fact)] = fact
    return latest


def select_profile_alerts(*, facts, alerts, insights=(), evidence_items=()):
    """在完整的临床/摘要投影完成后精选提醒，不裁剪指标或摘要。"""
    latest = current_alert_facts(facts)
    identities = {fact.fact_id: identity for identity, fact in latest.items()}

    def key(fact):
        return identities[fact.fact_id]

    indexed = {fact.fact_id: fact for fact in latest.values()}

    def order(tier, selected):
        deviation = max((reference_deviation(fact) or Decimal(0) for fact in selected), default=Decimal(0))
        rank = min(fact_rank(_payload(fact)) for fact in selected)
        return tier, -deviation, rank, tuple(sorted(key(fact)[:2] for fact in selected))

    critical, candidates = [], []
    internal_ids = {
        *(fact.fact_id for fact in facts),
        *(item['evidenceId'] for item in evidence_items),
        *(insight.get('topicId', '') for insight in insights),
    }
    risk_ids = {
        identity for insight in insights if insight.get('kind') == 'risk' for identity in insight.get('factIds', [])
    }
    for alert in alerts:
        selected = [indexed[identity] for identity in alert['factIds'] if identity in indexed]
        if not selected:
            continue
        alert = {
            **alert,
            'title': _clean_explanation_text(alert['title'], internal_ids) or selected[0].display_name + '指标异常',
            'factIds': [fact.fact_id for fact in selected],
        }
        if alert['severity'] == 'critical':
            critical.append((order(0, selected), alert))
            continue
        anchors = [fact for fact in selected if clinical_anchor(_payload(fact)) and fact.interpretation != 'critical']
        if not anchors:
            continue
        interpreted = alert['alertId'].startswith('alert_focus_') and any(fact.fact_id in risk_ids for fact in anchors)
        if not interpreted and not any(is_abnormal(_payload(fact)) for fact in anchors):
            continue
        candidates.append((order(0 if interpreted else 1, anchors), alert, {key(fact) for fact in anchors}))
    for fact in indexed.values():
        if not _specific_test(fact) or source_flag(_payload(fact)) is None:
            continue
        matched = _matches_evidence(fact, insights, evidence_items)
        deviation = reference_deviation(fact)
        if matched or (deviation is not None and deviation >= REVIEW_MULTIPLE):
            alert = _review_alert(fact)
            alert['title'] = _clean_explanation_text(alert['title'], internal_ids)
            candidates.append((order(2 if matched else 3, [fact]), alert, {key(fact)}))
    chosen, seen = [], set()
    for _, alert, keys in sorted(candidates, key=lambda item: (item[0], item[1]['alertId'])):
        if keys & seen:
            continue
        chosen.append(alert)
        seen.update(keys)
        if len(chosen) == MAX_IMPORTANT_ALERTS:
            break
    return [alert for _, alert in sorted(critical, key=lambda item: (item[0], item[1]['alertId']))] + chosen
