"""在关键提示精选后，确定性整合当前事实和已经验证的循证解释。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone

from agents.graphs.health_profile.focus import clinical_anchor
from agents.graphs.health_profile.focus import fact_focus
from agents.graphs.health_profile.focus import fact_rank
from agents.graphs.health_profile.focus import is_background
from agents.graphs.health_profile.focus import source_flag
from agents.graphs.health_profile.interpretation import _clean_explanation_text

from .alerts import _BACKGROUND_NAME
from .alerts import _matches_evidence
from .alerts import _payload
from .alerts import _result_text
from .alerts import _specific_test
from .alerts import current_alert_facts
from .models import FactCategory

EMPTY_OVERALL = '暂无可用于评估的数据。'
EMPTY_FOLLOW_UP = '暂无明确的随访建议。'
_NAME_LIMIT = 3
_DETAIL_LABELS = {
    'condition.history_status': '病史说明',
    FactCategory.CONDITION: '疾病记录',
    FactCategory.SYMPTOM: '症状记录',
    FactCategory.MEDICATION: '用药记录',
    FactCategory.ALLERGY: '过敏记录',
    FactCategory.LIFESTYLE: '生活方式',
    FactCategory.FAMILY_HISTORY: '家族史记录',
    FactCategory.GOAL: '健康目标',
}
_FLAG_LABELS = {'high': '偏高', 'low': '偏低', 'positive': '阳性或检出', 'critical': '危急异常', 'abnormal': '异常'}


def _names(facts):
    names = list(dict.fromkeys(fact.display_name for fact in facts))
    text = '、'.join(names[:_NAME_LIMIT])
    return text + (f'等 {len(facts)} 项' if len(facts) > _NAME_LIMIT else '')


def _reference_text(fact):
    reference = fact.reference_range or {}
    unit = str(reference.get('unit') or '')
    if reference.get('text'):
        text = str(reference['text'])
        return text + (unit if unit and not text.endswith(unit) else '')
    if reference.get('low') is not None or reference.get('high') is not None:
        low, high = reference.get('low'), reference.get('high')
        if low is None:
            return f'上限 {high}{unit}'
        if high is None:
            return f'下限 {low}{unit}'
        return f'{low}–{high}{unit}'
    return ''


def _result(fact):
    parts = [fact.display_name]
    value = fact.value
    if value is not None and (
        not isinstance(value, Mapping) or any(value.get(k) is not None for k in ('raw', 'text', 'number'))
    ):
        parts.append('结果 ' + _result_text(fact))
    flag = source_flag(_payload(fact))
    if flag:
        parts.append('来源标记' + _FLAG_LABELS[flag])
    if isinstance(value, Mapping):
        for field, label in (('method', '检测方法'), ('specimen', '标本')):
            if value.get(field):
                parts.append(label + ' ' + str(value[field]))
    reference = _reference_text(fact)
    if reference:
        parts.append('参考范围 ' + reference)
    return '，'.join(parts)


def _results(facts):
    text = '；'.join(_result(fact) for fact in facts[:_NAME_LIMIT])
    return text + (f'；另有 {len(facts) - _NAME_LIMIT} 项同类结果' if len(facts) > _NAME_LIMIT else '')


def _source_group(fact):
    if is_background(_payload(fact)) or (
        fact.category is FactCategory.REPORT_OBSERVATION and _BACKGROUND_NAME.search(fact.display_name)
    ):
        return '报告评估'
    if _specific_test(fact):
        return '具体检测'
    if fact.category is FactCategory.REPORT_OBSERVATION:
        return '其他报告结果'
    if fact.category in {FactCategory.ANTHROPOMETRIC, FactCategory.LABORATORY}:
        return '临床检测'
    return '其他健康信息'


def _supported_insights(insights, indexed, evidence_items):
    """仅复用已接受解释；再次守住当前事实、实际条目及具体检测对象边界。"""
    cited = {item['evidenceId']: set(item.get('factIds', ())) for item in evidence_items}
    for insight in insights:
        identities = set(insight.get('factIds', ()))
        if not identities or not identities.issubset(indexed):
            continue
        supports = insight.get('supports', ())
        if not supports or any(support.get('evidenceId') not in cited for support in supports):
            continue
        if not identities.issubset(set().union(*(cited[support['evidenceId']] for support in supports))):
            continue
        selected = [indexed[key] for key in identities]
        if insight.get('kind') == 'risk':
            if any(clinical_anchor(_payload(fact)) for fact in selected):
                yield insight
        elif insight.get('kind') == 'association' and all(
            _specific_test(fact) and source_flag(_payload(fact)) and _matches_evidence(fact, [insight], evidence_items)
            for fact in selected
        ):
            yield insight


def _entity_text(fact):
    value = fact.value
    if fact.fact_type in {'condition.history_status', 'family_history.reported'}:
        return _history_observation_text(fact.fact_type, value)
    if fact.fact_type == 'goal.health' and isinstance(value, Mapping):
        if value.get('presence') == 'absent':
            return '暂无改善需求'
        if value.get('presence') == 'unknown':
            return '本次关注尚不明确'
        description = value.get('description')
        if value.get('specificity') == 'unspecified' and isinstance(description, str) and description.strip():
            return description + '，具体内容未明确'
    if fact.category is FactCategory.SYMPTOM and isinstance(value, Mapping):
        name = value.get('name')
        name = name if isinstance(name, str) and name.strip() else fact.display_name
        presence = value.get('presence')
        if description := _qualified_description(value, (name,)):
            return description
        if presence == 'absent' or value.get('status') == 'denied' or value.get('negation') is True:
            return '自述无' + name
        if presence == 'unknown' or value.get('status') == 'unknown' or value.get('unknown') is True:
            return name + '情况不清楚'
        if presence == 'conflicting' or value.get('status') == 'conflicting':
            return name + '表述存在冲突'
        if value.get('specificity') == 'unspecified':
            return '不适表现未明确'
    if value is False:
        return '未报告' + fact.display_name
    if isinstance(value, str) and value != fact.display_name:
        return f'{fact.display_name}（{value}）'
    return fact.display_name


def _history_observation_text(fact_type, value):
    """Render an answer state as reported background, never as a diagnosis."""
    family = fact_type == 'family_history.reported'
    label = '血亲家族病史' if family else '个人疾病史'
    if not isinstance(value, Mapping):
        return label + '内容未明确'
    conditions = value.get('conditions')
    names = (
        [name for name in conditions if isinstance(name, str) and name.strip()] if isinstance(conditions, list) else []
    )
    if description := _qualified_description(value, names):
        return description
    target = '、'.join(names) if names else label
    presence = value.get('presence')
    if presence == 'absent':
        text = '自述无' + target
    elif presence == 'unknown':
        text = target + '情况不清楚'
    elif presence == 'conflicting':
        text = target + '表述存在冲突'
    elif presence == 'present' and value.get('specificity') == 'specific' and names:
        text = '家族中报告' + target if family else '病史说明涉及' + target
    elif presence == 'present':
        text = f'自述有{label}，病名未明确'
    else:
        description = value.get('description')
        return (
            label + '说明：' + (description if isinstance(description, str) and description.strip() else '内容未明确')
        )
    if family:
        relationship = value.get('relationship')
        text += '，' + (
            '亲属说明为' + relationship if isinstance(relationship, str) and relationship.strip() else '具体血亲未明确'
        )
    return text


def _qualified_description(value, names):
    """Do not widen a selected-condition or temporal denial into a whole history."""
    description = value.get('description')
    if not isinstance(description, str) or not description.strip():
        return None
    description = description.strip()
    controls = {'没有', '无', '不清楚', '不知道', '目前记不清', '有', '有的', '是', '是的'}
    if description in controls or description in names:
        return None
    if value.get('presence') == 'conflicting':
        return '表述存在冲突：' + description
    return description


def build_profile_summary(*, facts, profile_version, generated_at, alerts=(), insights=(), evidence_items=()):
    """保持五字段契约；排序、文案及空态不增加模型调用，也不写回健康事实。"""
    current = sorted(current_alert_facts(facts).values(), key=lambda fact: fact_rank(_payload(fact)))
    indexed = {fact.fact_id: fact for fact in current}
    internal_ids = {
        *(fact.fact_id for fact in facts),
        *(item['evidenceId'] for item in evidence_items),
        *(insight.get('topicId', '') for insight in insights),
    }

    def clean(text):
        return _clean_explanation_text(text, internal_ids)

    paragraphs, priorities, follow_up = [], [], []
    described = set()
    sections = []
    selected_ids = {key for alert in alerts for key in alert['factIds'] if key in indexed}
    critical_ids = {
        key for alert in alerts if alert['severity'] == 'critical' for key in alert['factIds'] if key in indexed
    }
    for alert in alerts:
        if alert['severity'] != 'critical':
            continue
        selected = [fact for fact in current if fact.fact_id in alert['factIds'] and fact.fact_id in critical_ids]
        if selected:
            paragraphs.append('需优先关注来源已标记的危急异常：' + _results(selected) + '。')
            priorities.append('及时联系医生评估' + _names(selected))
            described.update(fact.fact_id for fact in selected)

    for insight in _supported_insights(insights, indexed, evidence_items):
        explanation = clean(insight.get('explanation', ''))
        if not explanation:
            continue
        selected = [fact for fact in current if fact.fact_id in insight['factIds']]
        results = [
            fact for fact in selected if source_flag(_payload(fact)) and _source_group(fact) in {'临床检测', '具体检测'}
        ]
        if results:
            explanation = _results(results) + '。' + explanation
        action = clean(insight.get('action', '')) if insight['kind'] == 'risk' else ''
        follow = clean(insight.get('followUp', '')) if insight['kind'] == 'risk' else ''
        sections.append((min(fact_rank(_payload(fact)) for fact in selected), explanation, action, follow))
        described.update(insight['factIds'])

    # 临床事实不受模型主题数量或提醒上限裁剪；同类未解释项目合并概述。
    clinical = defaultdict(list)
    for fact in current:
        if fact.fact_id in described or not clinical_anchor(_payload(fact)):
            continue
        focus, domain = fact_focus(_payload(fact))
        clinical[focus if domain != 'other' else 'other.' + fact.category.value].append(fact)
    for group in clinical.values():
        tests = [fact for fact in group if _source_group(fact) == '临床检测']
        text = (
            '需要核对的检测结果：' + _results(tests) + '。' if tests else '需要关注的健康记录：' + _names(group) + '。'
        )
        records = [fact for fact in group if fact not in tests]
        if records and tests:
            text += '相关健康记录：' + '、'.join(_entity_text(fact) for fact in records[:_NAME_LIMIT]) + '。'
        action = '结合原始报告和医生意见复核' + _names(group)
        sections.append((min(fact_rank(_payload(fact)) for fact in group), text, action, ''))
        described.update(fact.fact_id for fact in group)

    # 报告复核行动只来自最终关键提示，不由菌群关联或普通偏离推导临床建议。
    for fact in current:
        if fact.fact_id not in selected_ids or fact.fact_id in critical_ids or not _specific_test(fact):
            continue
        action = fact.display_name + '：核对原报告中的结果、参考范围和检测方法。'
        text = '' if fact.fact_id in described else '重点核对的检测结果：' + _result(fact) + '。'
        sections.append((fact_rank(_payload(fact)), text, action, ''))
        described.add(fact.fact_id)

    for _, text, action, follow in sorted(sections, key=lambda section: (section[0], section[1])):
        if text:
            paragraphs.append(text)
        if action:
            priorities.append(action)
        if follow:
            follow_up.append(follow)

    remaining = defaultdict(list)
    for fact in current:
        if fact.fact_id not in described and source_flag(_payload(fact)):
            remaining[_source_group(fact)].append(fact)
    for label in ('临床检测', '具体检测', '报告评估', '其他报告结果', '其他健康信息'):
        group = remaining.get(label)
        if not group:
            continue
        prefix = '其他' if paragraphs else ''
        paragraphs.append(f'{prefix}来源标记异常的{label}共 {len(group)} 项：' + _names(group) + '。')
        described.update(fact.fact_id for fact in group)

    details = defaultdict(list)
    for fact in current:
        if fact.fact_id not in described and fact.category in _DETAIL_LABELS:
            key = fact.fact_type if fact.fact_type == 'condition.history_status' else fact.category
            details[key].append(fact)
    for category, group in details.items():
        visible = '、'.join(dict.fromkeys(_entity_text(fact) for fact in group[:_NAME_LIMIT]))
        if len(group) > _NAME_LIMIT:
            visible += f'等 {len(group)} 项'
        paragraphs.append(_DETAIL_LABELS[category] + '：' + visible + '。')

    if not paragraphs:
        tests = [fact for fact in current if _source_group(fact) in {'临床检测', '具体检测'}]
        normal = [fact for fact in tests if fact.interpretation == 'normal']
        if normal:
            paragraphs.append('当前有明确判定的检测项目均标记为正常。')
            if len(normal) != len(tests):
                paragraphs.append('另有检测结果未提供明确的异常判断依据。')
        elif tests:
            paragraphs.append('已有检测结果，但未提供明确的异常判断依据。')
        elif any(_source_group(fact) in {'报告评估', '其他报告结果'} for fact in current):
            paragraphs.append('已有报告结果，暂无可据此提出的具体建议。')
        else:
            paragraphs.append(EMPTY_OVERALL)
    if isinstance(generated_at, datetime):
        generated_at = generated_at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    return {
        'overall': '\n'.join(dict.fromkeys(text for paragraph in paragraphs if (text := clean(paragraph))))
        or EMPTY_OVERALL,
        'priorities': list(dict.fromkeys(text for action in priorities if (text := clean(action)))),
        'followUp': '；'.join(dict.fromkeys(follow_up)) or EMPTY_FOLLOW_UP,
        'generatedAt': generated_at,
        'basedOnProfileVersion': profile_version,
    }
