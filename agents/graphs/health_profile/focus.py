"""画像专科排序与重点覆盖策略；只组织已有事实，不产生诊断。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

POLICY_VERSION = 'gut-metabolic-explanation-v7'
MAX_TOPICS = 6
ABNORMAL = frozenset({'low', 'high', 'critical', 'positive', 'abnormal'})
_STATE_OBSERVATIONS = frozenset({'condition.history_status', 'family_history.reported'})
_HISTORICAL_SYMPTOM_STATES = frozenset({'past', 'historical', 'resolved', 'recovered'})
_GROUPS = (
    ('metabolic.glucose', 'metabolic', ('glucose', 'hba1c'), ('血糖', '糖尿病', '糖化血红蛋白', '胰岛素抵抗')),
    (
        'metabolic.lipids',
        'metabolic',
        ('total_cholesterol', 'triglyceride', 'hdl_cholesterol', 'ldl_cholesterol'),
        ('血脂', '胆固醇', '甘油三酯'),
    ),
    ('metabolic.weight', 'metabolic', ('bmi',), ('肥胖', '超重', '体质指数', '减重')),
    ('metabolic.uric_acid', 'metabolic', ('uric_acid',), ('尿酸', '痛风')),
    (
        'gut.liver',
        'gut',
        ('alt', 'ast', 'ggt', 'alkaline_phosphatase', 'total_bilirubin', 'direct_bilirubin', 'indirect_bilirubin'),
        ('肝炎', '肝酶', '脂肪肝', '转氨酶', '胆汁'),
    ),
    ('gut.bowel', 'gut', ('fecal_occult_blood',), ('肠', '腹泻', '便秘', '腹痛', '腹胀', '便血', '粪便', '大便')),
    ('gut.stomach', 'gut', (), ('胃', '消化', '幽门螺杆菌', '反酸')),
    ('metabolic.lifestyle', 'metabolic', (), ('高糖饮食', '高脂饮食', '暴饮暴食', '久坐')),
)


def canonical_type(value: str) -> str:
    normalized = value.casefold()
    return 'laboratory.' + normalized[4:] if normalized.startswith('lab.') else normalized


def is_background(fact: Mapping[str, Any]) -> bool:
    """综合评分、菌群汇总指标和报告评估始终不是循证检索对象。"""
    fact_type = canonical_type(fact['factType'])
    value = fact.get('value')
    return (
        fact.get('category') == 'assessment'
        or fact_type.startswith(('assessment.', 'microbiome.'))
        or (
            fact_type.startswith('report_observation.')
            and isinstance(value, Mapping)
            and value.get('kind') == 'assessment'
        )
    )


def is_report_observation(fact: Mapping[str, Any]) -> bool:
    return canonical_type(fact['factType']).startswith('report_observation.')


def is_abnormal(fact: Mapping[str, Any]) -> bool:
    """临床异常判定：背景类与未标准化项目恒为 False；其余复述来源标记。

    未标准化项目即使带来源异常标记，也不生成临床提醒；
    它们能否进入循证检索由 association_anchor 单独判断；报告复核提示由 alerts 策略另行精选。
    """
    if (
        canonical_type(fact['factType']) in _STATE_OBSERVATIONS
        or _nonaffirmative_entity(fact)
        or is_background(fact)
        or is_report_observation(fact)
    ):
        return False
    if fact.get('category') in {'demographic', 'family_history', 'lifestyle', 'goal'}:
        return False
    return source_flag(fact) is not None


def source_flag(fact: Mapping[str, Any]) -> str | None:
    """只复述来源异常标记或明确的阳性、检出文字。"""
    if canonical_type(fact['factType']) in _STATE_OBSERVATIONS or _nonaffirmative_entity(fact):
        return None
    if fact.get('interpretation') in ABNORMAL:
        return fact['interpretation']
    if not canonical_type(fact['factType']).startswith(('laboratory.', 'report_observation.')):
        return None
    value = fact.get('value')
    if isinstance(value, Mapping):
        value = value.get('text', value.get('raw'))
    # 非零数值、模糊叙述和“未检出”都不能被推断为阳性。
    if isinstance(value, str) and value.strip().casefold() in {
        '阳性',
        'positive',
        '+',
        '检出',
        '已检出',
        'detected',
    }:
        return 'positive'
    return None


def clinical_anchor(fact: Mapping[str, Any]) -> bool:
    """可支撑临床风险解释的异常、疾病或肯定性症状。"""
    if canonical_type(fact['factType']) in _STATE_OBSERVATIONS or _nonaffirmative_entity(fact) or is_background(fact):
        return False
    if is_abnormal(fact):
        return True
    if fact.get('category') not in {'condition', 'symptom'}:
        return False
    value = fact.get('value')
    text = value if isinstance(value, str) else fact['displayName']
    return not re.search(r'^(?:无|否认|未见|没有|no\b|denies\b)', text.strip(), re.IGNORECASE)


def _nonaffirmative_entity(fact: Mapping[str, Any]) -> bool:
    if fact.get('category') not in {'condition', 'symptom'}:
        return False
    value = fact.get('value')
    if isinstance(value, str):
        return bool(
            re.search(r'^(?:无|否认|未见|没有|不清楚|未明确|no\b|denies\b|unknown\b)', value.strip(), re.IGNORECASE)
        )
    return (
        value is False
        or isinstance(value, Mapping)
        and (
            ('presence' in value and value['presence'] != 'present')
            or ('specificity' in value and value['specificity'] != 'specific')
            or value.get('status') in ('denied', 'unknown', 'unspecified', 'conflicting')
            or (
                fact.get('category') == 'symptom'
                and isinstance(value.get('status'), str)
                and value['status'].casefold() in _HISTORICAL_SYMPTOM_STATES
            )
            or value.get('negation') is True
            or value.get('unknown') is True
        )
    )


def association_anchor(fact: Mapping[str, Any]) -> bool:
    """具体未标准化检测项可支持资料关联；临床风险与报告复核提示分别判定。"""
    return is_report_observation(fact) and not is_background(fact) and source_flag(fact) is not None


def evidence_anchor(fact: Mapping[str, Any]) -> bool:
    """一个检索主题至少需要一条当前的临床或具体检测线索。"""
    return clinical_anchor(fact) or association_anchor(fact)


def fact_focus(fact: Mapping[str, Any]) -> tuple[str, str]:
    fact_type = canonical_type(fact['factType'])
    if is_background(fact):
        return (
            ('gut.microbiome', 'gut')
            if fact_type.startswith(('microbiome.', 'assessment.microbiome_'))
            else ('other.background', 'other')
        )
    code = fact_type.rsplit('.', 1)[-1]
    value = fact.get('value')
    text = fact['displayName'] + (' ' + value if isinstance(value, str) else '')
    for key, domain, codes, terms in _GROUPS:
        if code in codes or any(term in text for term in terms):
            return key, domain
    # 相同 factType 的不同疾病或症状不能被合并为一个实体。
    identity = fact_type + '\n' + fact['displayName']
    return 'other.' + hashlib.sha256(identity.encode()).hexdigest()[:16], 'other'


def fact_rank(fact: Mapping[str, Any]) -> tuple:
    _, domain = fact_focus(fact)
    critical = is_abnormal(fact) and fact.get('interpretation') == 'critical'
    anchor = evidence_anchor(fact)
    tier = 0 if critical else 1 if domain != 'other' and anchor else 2 if anchor else 3 if domain != 'other' else 4
    return tier, not is_abnormal(fact), fact['displayName'], fact['factId']


def balanced_topics(topics: list[dict[str, Any]], limit: int = MAX_TOPICS) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    ordered = sorted(topics, key=lambda topic: (topic['rank'], topic['topicId']))
    selected = []
    for domain in ('gut', 'metabolic'):
        match = next((topic for topic in ordered if topic['domain'] == domain), None)
        if match is not None and len(selected) < limit:
            selected.append(match)
    for topic in ordered:
        if topic not in selected and len(selected) < limit:
            selected.append(topic)
    return sorted(selected, key=lambda topic: (topic['rank'], topic['topicId']))


def default_topics(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for fact in sorted(facts, key=fact_rank):
        if not evidence_anchor(fact):
            continue
        key, domain = fact_focus(fact)
        topic = groups.setdefault(
            key,
            {
                'topicId': key,
                'domain': domain,
                'title': fact['displayName'],
                'factIds': [],
                'rank': fact_rank(fact),
                'query': '',
            },
        )
        topic['factIds'].append(fact['factId'])
    indexed = {fact['factId']: fact for fact in facts}
    for topic in groups.values():
        names = list(dict.fromkeys(indexed[key]['displayName'] for key in topic['factIds']))
        flags = {source_flag(indexed[key]) for key in topic['factIds']}
        qualifier = ' 检出' if 'positive' in flags else ' 异常'
        topic['query'] = (' '.join(names) + qualifier)[:256]
    return balanced_topics(list(groups.values()))


def json_object(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith('```'):
        stripped = re.sub(r'^```(?:json)?\s*|\s*```$', '', stripped)
    value = json.loads(stripped)
    if not isinstance(value, dict):
        raise ValueError('expected JSON object')
    return value
