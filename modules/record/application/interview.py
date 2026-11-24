"""Bounded interview policy; semantic understanding remains with Record agents."""

import re
from copy import deepcopy

from modules.record.application import intake as intake_policy
from modules.record.application import lifestyle

MIN_INTAKE_TURNS = 10
INTAKE_TARGET = 15
MAX_INTAKE_TURNS = 15
MAX_QA_ROUNDS = 5
MAX_VAGUE_ANSWERS = 2
OPENING_MAX_OPTIONS = 12
_LEGACY_POLICY = {'version': 1, 'minIntake': 1, 'targetIntake': 5, 'maxIntake': 10, 'maxInteractions': 20}

# Names describe selectable concerns, not diagnoses inferred for this patient.
GOAL_OPTIONS = (
    '便秘',
    '反酸',
    '胃胀',
    '腹泻',
    '入睡困难',
    '早醒',
    '多梦',
    '容易疲劳',
    '乏力',
    '减轻体重',
    '增加肌肉',
    '痤疮',
    '皮肤敏感',
    '脱发',
    '肩颈疼痛',
    '腰背疼痛',
    '关节疼痛',
    '缓解压力',
    '情绪低落',
    '月经不规律',
    '更年期不适',
    '反复感冒',
    '过敏',
    '提高骨密度',
    '控制血糖',
    '控制血脂',
    '控制血压',
)
DEFAULT_GOAL_OPTIONS = (
    '便秘',
    '反酸',
    '入睡困难',
    '容易疲劳',
    '减轻体重',
    '增加肌肉',
    '痤疮',
    '脱发',
    '肩颈疼痛',
    '缓解压力',
)
GOAL_STATUS_OPTIONS = ('暂无改善需求', '不清楚')
_RELATED_GOALS = (
    (('血糖', '糖尿病'), ('控制血糖',)),
    (('血脂', '胆固醇', '甘油三酯'), ('控制血脂',)),
    (('血压', '高血压'), ('控制血压',)),
    (('体重', '肥胖', '超重'), ('减轻体重',)),
    (('胃', '消化', '便秘', '排便'), ('胃胀', '便秘', '反酸', '腹泻')),
    (('睡眠', '失眠'), ('入睡困难', '早醒', '多梦')),
    (('骨密度', '骨质疏松'), ('提高骨密度',)),
)


def new_policy() -> dict:
    return {
        'version': 2,
        'minIntake': MIN_INTAKE_TURNS,
        'targetIntake': INTAKE_TARGET,
        'maxIntake': MAX_INTAKE_TURNS,
        'maxInteractions': MAX_INTAKE_TURNS + MAX_QA_ROUNDS * MAX_VAGUE_ANSWERS,
    }


def is_expanded(flow: dict) -> bool:
    return isinstance(flow.get('interviewPolicy'), dict) and flow['interviewPolicy'].get('version') == 2


def budget(flow: dict) -> dict:
    """Missing policy belongs to an existing session, never an implicit upgrade."""
    expected = new_policy() if is_expanded(flow) else dict(_LEGACY_POLICY)
    snapshot = flow.get('interviewPolicy')
    if not isinstance(snapshot, dict) or snapshot.get('version') not in (1, 2):
        return expected
    if any(type(snapshot.get(key)) is not int or snapshot[key] <= 0 for key in expected):
        return expected
    if not snapshot['minIntake'] <= snapshot['targetIntake'] <= snapshot['maxIntake']:
        return expected
    return {key: snapshot[key] for key in expected}


_FIXED_TOPICS = (
    ('goal', '本次关注与健康目标', '用户本次最希望了解或改善什么，不以报告异常替用户构造主诉'),
    ('medical_history', '个人疾病史', '了解医生明确诊断的个人疾病史；指标异常不等于确诊'),
    ('family_history', '血亲家族病史', '了解父母、兄弟姐妹等血亲的疾病背景，不能写成本人疾病'),
)
_DEFAULT_LIFESTYLE = (
    'diet_structure',
    'activity',
    'sleep',
    'stress_coping',
    'meal_pattern',
    'hydration',
    'sedentary',
    'alcohol',
    'smoking',
)
_HISTORY_DISEASE_OPTIONS = {
    'medical_history': ('高血压', '糖尿病', '血脂异常', '冠心病', '脑卒中', '慢性肾病', '脂肪肝'),
    'family_history': ('高血压', '糖尿病', '冠心病', '脑卒中', '肺癌', '结直肠癌', '地中海贫血', '血友病'),
}


def goal_choice_fields(flow: dict | None = None) -> dict:
    """Bounded concrete suggestions; related records only order candidate labels."""
    flow = flow or {}
    facts = flow.get('intake', {}).get('facts', {})
    background = ' '.join(
        fact.get('summary', '')
        for fact in facts.values()
        if fact.get('status') == 'known'
        and any(item.get('source') in ('本次报告', '长期画像', '附带资料') for item in fact.get('evidence', []))
    )
    related = [
        label for words, labels in _RELATED_GOALS if any(word in background for word in words) for label in labels
    ]
    concrete = list(dict.fromkeys([*related, *DEFAULT_GOAL_OPTIONS]))[: OPENING_MAX_OPTIONS - len(GOAL_STATUS_OPTIONS)]
    return {'questionType': 'checkbox', 'options': [*concrete, *GOAL_STATUS_OPTIONS]}


def history_choice_fields(target: dict | None) -> dict:
    """Compatibility name for the controlled choices of every intake purpose."""
    target = target or {}
    if target.get('supplementation'):
        return {'questionType': 'radio', 'options': ['没有需要补充的内容', '目前记不清']}
    dimension = target.get('dimension')
    if dimension == 'goal':
        suggested = target.get('choiceConstraints', {}).get('options', [])
        concrete = list(dict.fromkeys(item for item in suggested if item in GOAL_OPTIONS))
        if not concrete:
            return goal_choice_fields()
        return {
            'questionType': 'checkbox',
            'options': [*concrete[: OPENING_MAX_OPTIONS - len(GOAL_STATUS_OPTIONS)], *GOAL_STATUS_OPTIONS],
        }
    if dimension in lifestyle.TOPICS:
        fields = lifestyle.question_fields(dimension)
        return {key: fields[key] for key in ('questionType', 'options')}
    diseases = _HISTORY_DISEASE_OPTIONS.get(target.get('dimension'))
    if not diseases:
        return {}
    return {'questionType': 'checkbox', 'options': ['没有', '不清楚', *diseases]}


def ensure_counters(session: dict) -> None:
    """Upgrade only a working draft, preserving old questions and recorded counts."""
    flow = session['qaFlow']
    # Freeze before deriving newer counters: their presence cannot identify the
    # policy that originally created an unmarked HEAD-era snapshot.
    flow['interviewPolicy'] = budget(flow)
    history = session.get('qaHistory') or []
    intake_history = sum(item.get('question', {}).get('phase', 'intake') == 'intake' for item in history)
    flow['intakeQuestionCount'] = max(flow.get('intakeQuestionCount', 0), flow.get('intakeCount', 0), intake_history)
    if 'qaRoundCount' not in flow:
        # The old policy returned to supplement after direct answers, so phase=qa
        # alone misses already-used replies. Ambiguous legacy turns count
        # conservatively; only clear control acknowledgements are excluded.
        controls = {'有', '嗯', '嗯嗯', '好的', '好', '没有了', '没有其他问题', '没了'}
        historical_replies = sum(
            item.get('question', {}).get('phase') in ('qa', 'supplement')
            and str(item.get('answer', '')).strip() not in controls
            for item in history
        )
        flow['qaRoundCount'] = min(
            MAX_QA_ROUNDS,
            max(historical_replies, flow.get('interactionCount', 0) - flow.get('intakeCount', 0))
            if flow['phase'] != 'intake'
            else historical_replies,
        )
    flow.setdefault('qaVagueCount', flow.get('supplementClarifications', 0))
    flow.setdefault('preQaAnswered', flow['phase'] != 'intake')


def progress(flow: dict) -> str:
    if flow['phase'] == 'finished':
        return '100%'
    if flow['phase'] == 'intake':
        count = flow.get('intakeQuestionCount', flow.get('intakeCount', 0))
        if not is_expanded(flow):
            if budget(flow)['maxIntake'] == 6:
                return f'{min(50, count * 10)}%'
            return f'{min(50, flow.get("interactionCount", count) * 5)}%'
        return f'{min(50, round(count * 50 / budget(flow)["maxIntake"]))}%'
    if flow['phase'] == 'qa':
        return f'{min(95, 60 + flow.get("qaRoundCount", 0) * 8)}%'
    return '60%'


def _known_facts(flow: dict, dimensions) -> list[dict]:
    facts = flow['intake']['facts'].values()
    return [deepcopy(item) for item in facts if item['dimension'] in dimensions][:12]


def fixed_target(flow: dict, index: int) -> dict:
    dimension, topic, needed = _FIXED_TOPICS[index]
    dimensions = (dimension, 'chief_complaint') if dimension == 'goal' else (dimension,)
    known = _known_facts(flow, dimensions)
    if dimension == 'goal':
        known += [
            {'status': 'known', 'summary': item['text'], 'evidence': item.get('evidence', [])}
            for item in flow['intake']['complaints']
        ]
    target = {
        'topic': topic,
        'aspect': 'family_history'
        if dimension == 'family_history'
        else 'medical_history'
        if dimension == 'medical_history'
        else 'symptoms',
        'dimension': dimension,
        'scope': 'general',
        'complaintIds': [],
        'complaints': [],
        'informationNeeded': needed,
        'reason': '依次了解本次关注、个人疾病史和血亲家族背景，避免重复收集已有信息',
        'supplementation': bool(known),
        'knownFacts': known,
        'essential': True,
    }
    if dimension == 'goal' and not known:
        target['choiceConstraints'] = goal_choice_fields(flow)
    if choices := history_choice_fields(target):
        target['choiceConstraints'] = choices
    return target


def asked_lifestyle_dimensions(session: dict) -> set[str]:
    """Use published questions, with persisted markers only for legacy snapshots."""
    lifestyle = intake_policy.LIFESTYLE_DIMENSIONS | {'habits'}
    questions = [
        item['question']
        for item in session.get('qaHistory') or []
        if isinstance(item, dict) and isinstance(item.get('question'), dict)
    ]
    if isinstance(session.get('currentQuestion'), dict):
        questions.append(session['currentQuestion'])
    dimensions = {question['intakeDimension'] for question in questions if question.get('intakeDimension') in lifestyle}
    if not questions or any(
        question.get('phase', 'intake') == 'intake' and not question.get('intakeDimension') for question in questions
    ):
        dimensions.update(
            key.rsplit(':', 1)[-1]
            for key in session['qaFlow'].get('intake', {}).get('asked', [])
            if key.rsplit(':', 1)[-1] in lifestyle
        )
    return dimensions


def asked_lifestyle_categories(session: dict) -> set[str]:
    return _categories(asked_lifestyle_dimensions(session))


def selection_context(session: dict) -> dict:
    """Count published purposes; draft selection markers never add coverage."""
    questions = [
        item['question']
        for item in session.get('qaHistory') or []
        if isinstance(item, dict) and isinstance(item.get('question'), dict)
    ]
    if isinstance(session.get('currentQuestion'), dict):
        questions.append(session['currentQuestion'])
    counts = {}
    last = None
    represented = set()
    seen_ids = set()
    for question in questions:
        dimension = question.get('intakeDimension')
        if question.get('phase', 'intake') != 'intake' or dimension not in intake_policy.LIFESTYLE_DIMENSIONS:
            continue
        identity = question.get('questionId')
        if identity and identity in seen_ids:
            continue
        if identity:
            seen_ids.add(identity)
        represented.add(dimension)
        last = intake_policy.LIFESTYLE_CATEGORY_BY_DIMENSION[dimension]
        counts[last] = counts.get(last, 0) + 1
    dimensions = asked_lifestyle_dimensions(session)
    for dimension in dimensions - represented:
        category = intake_policy.LIFESTYLE_CATEGORY_BY_DIMENSION.get(dimension)
        if category is not None:
            counts[category] = counts.get(category, 0) + 1
    if last is None and not represented:
        for marker in session['qaFlow'].get('intake', {}).get('asked', []):
            category = intake_policy.LIFESTYLE_CATEGORY_BY_DIMENSION.get(marker.rsplit(':', 1)[-1])
            if category is not None:
                last = category
    return {
        'asked_dimensions': dimensions,
        'asked_categories': _categories(dimensions),
        'category_counts': counts,
        'last_category': last,
    }


def _categories(dimensions) -> set[str]:
    return {
        intake_policy.LIFESTYLE_CATEGORY_BY_DIMENSION[dimension]
        for dimension in dimensions
        if dimension in intake_policy.LIFESTYLE_CATEGORY_BY_DIMENSION
    }


def _available_dimensions(flow: dict) -> frozenset[str]:
    return intake_policy.LIFESTYLE_DIMENSIONS if is_expanded(flow) else frozenset(_DEFAULT_LIFESTYLE)


_ZERO_PREFIX = (
    r'(?:(?:用户|我|本人)(?:本人)?(?:表示|说)?(?:自己)?(?:现在|目前|平时|一直|通常|日常)?'
    r'|(?:现在|目前|平时|一直|通常|日常))?(?:完全)?'
)
_NO_ALCOHOL = re.compile(
    _ZERO_PREFIX
    + r'(?:不(?:喝(?:任何)?酒|饮酒)(?:了)?|从(?:来)?(?:不|未)(?:喝酒?|饮酒)|滴酒不沾|(?:已|已经)?戒酒(?:了)?'
    r'|(?:已|已经)?不喝了|没有(?:任何)?(?:饮酒|喝酒)(?:习惯)?|无饮酒习惯)'
)
_NO_EXERCISE = re.compile(
    _ZERO_PREFIX + r'(?:不(?:做)?(?:运动|锻炼)|没有(?:安排|专门)?(?:运动|锻炼)|从(?:来)?(?:不|未)(?:运动|锻炼)'
    r'|没有(?:运动|锻炼)习惯|无(?:运动|锻炼)(?:安排|习惯)|没有主动活动)'
)


def _complete_denial(summary: str, pattern) -> bool:
    """Only unqualified current denial can close an otherwise unknown detail.

    A matching substring cannot establish whole-scope non-use. Time windows,
    beverage/activity restrictions and mixed histories remain for the existing
    semantic analysis; do not silently discard its valid follow-up candidates.
    """
    clauses = [re.sub(r'\s+', '', part) for part in re.split(r'[，,。.!！?？；;\n]+', summary) if part.strip()]
    return bool(clauses) and all(pattern.fullmatch(part) for part in clauses)


def _not_applicable(dimension: str, facts: list[dict]) -> bool:
    if dimension in ('exercise_frequency', 'exercise_duration'):
        relevant = [
            fact for fact in facts if fact['dimension'] in ('exercise_frequency', 'exercise_duration', 'activity')
        ]
        return bool(relevant) and all(
            fact['status'] == 'known'
            and (
                _complete_denial(fact['summary'], _NO_EXERCISE)
                or (
                    fact['dimension'] == 'exercise_frequency'
                    and fact['summary'].strip() in ('基本没有', '几乎没有', '没有')
                )
            )
            for fact in relevant
        )
    if dimension != 'alcohol_amount':
        return False
    relevant = [fact for fact in facts if fact['dimension'] in ('alcohol', 'alcohol_amount')]
    return bool(relevant) and all(
        fact['status'] == 'known' and _complete_denial(fact['summary'], _NO_ALCOHOL) for fact in relevant
    )


def _diversity_rank(dimension, categories, counts, last_category):
    category = intake_policy.LIFESTYLE_CATEGORY_BY_DIMENSION.get(dimension)
    return category == last_category, counts.get(category, int(category in categories))


def lifestyle_targets(
    flow: dict,
    understanding,
    gaps: list,
    *,
    asked_categories: set[str] | None = None,
    asked_dimensions: set[str] | None = None,
    category_counts: dict[str, int] | None = None,
    last_category: str | None = None,
) -> list[dict]:
    """Rank relevance first, then alternate and balance published categories."""
    state = flow['intake']
    asked = {key.rsplit(':', 1)[-1] for key in state['asked']} if asked_dimensions is None else asked_dimensions
    categories = _categories(asked) if asked_categories is None else asked_categories
    counts = category_counts or {}
    facts = list(state['facts'].values())
    dimensions = _available_dimensions(flow)
    resolved = intake_policy.resolve_gaps(flow, understanding, gaps)
    resolved.sort(
        key=lambda gap: (
            gap.relevance != 'focused',
            *_diversity_rank(gap.dimension, categories, counts, last_category),
        )
    )
    result = []
    seen = set()
    for gap in resolved:
        dimension = gap.dimension
        if dimension not in dimensions or dimension in seen or dimension in asked:
            continue
        if any(item['dimension'] == dimension for item in facts) or _not_applicable(dimension, facts):
            continue
        if not is_expanded(flow) and any(item['dimension'] == 'habits' for item in facts):
            continue
        seen.add(dimension)
        result.append(
            {
                **gap.model_dump(exclude={'complaintIndexes'}),
                'optionalBackground': False,
                'complaintIds': list(gap.complaintIds),
                'complaints': [item for item in state['complaints'] if item['id'] in gap.complaintIds],
                'supplementation': False,
                'knownFacts': [],
                'lifestyleCategory': intake_policy.LIFESTYLE_CATEGORY_BY_DIMENSION[dimension],
                'informationNeeded': lifestyle.question_fields(dimension)['question']
                if is_expanded(flow)
                else gap.informationNeeded,
            }
        )
    return result


def default_lifestyle_target(
    flow: dict,
    *,
    required: bool = False,
    asked_categories: set[str] | None = None,
    asked_dimensions: set[str] | None = None,
    category_counts: dict[str, int] | None = None,
    last_category: str | None = None,
) -> dict | None:
    state = flow['intake']
    asked = {key.rsplit(':', 1)[-1] for key in state['asked']} if asked_dimensions is None else asked_dimensions
    categories = _categories(asked) if asked_categories is None else asked_categories
    counts = category_counts or {}
    facts = list(state['facts'].values())
    known = {item['dimension'] for item in facts}
    order = lifestyle.DEFAULT_ORDER if is_expanded(flow) else _DEFAULT_LIFESTYLE
    candidates = [item for item in order if item not in asked | known and not _not_applicable(item, facts)]
    candidates.sort(key=lambda item: _diversity_rank(item, categories, counts, last_category))
    dimension = next(iter(candidates), None)
    if dimension is None or ('habits' in known and not is_expanded(flow)):
        if is_expanded(flow) or not required:
            return None
        dimension = 'habits'
    known_facts = _known_facts(flow, intake_policy.LIFESTYLE_DIMENSIONS | {'habits'}) if dimension == 'habits' else []
    return {
        'topic': intake_policy.DIMENSION_LABELS[dimension],
        'aspect': 'habits',
        'dimension': dimension,
        'scope': 'general',
        'complaintIds': [],
        'complaints': [],
        'informationNeeded': lifestyle.question_fields(dimension)['question']
        if is_expanded(flow)
        else '了解一项尚未说明的日常生活习惯',
        'reason': '补充用户日常生活背景，探索可能相关的生活因素，不据此确认个人病因',
        'supplementation': bool(known_facts),
        'knownFacts': known_facts,
        'essential': False,
        'relevance': 'general',
        'lifestyleCategory': intake_policy.LIFESTYLE_CATEGORY_BY_DIMENSION.get(dimension),
    }


def select_lifestyle_target(flow: dict, *, candidates=(), sufficient: bool = False, **selection) -> dict | None:
    """Use one budget-aware continuation rule in normal and fallback paths."""
    policy = budget(flow)
    count = flow['intakeQuestionCount']
    if count >= policy['maxIntake']:
        return None
    if is_expanded(flow):
        return candidates[0] if candidates else default_lifestyle_target(flow, **selection)
    if count < policy['minIntake']:
        return candidates[0] if candidates else default_lifestyle_target(flow, required=True, **selection)
    if count < policy['targetIntake']:
        if candidates:
            return candidates[0]
        if not sufficient:
            return default_lifestyle_target(flow, **selection)
    elif not sufficient:
        return next((item for item in candidates if item.get('essential')), None)
    return None


def select_target(flow: dict, target: dict) -> dict:
    state = flow['intake']
    if choices := history_choice_fields(target):
        target['choiceConstraints'] = choices
    for identity in target.get('complaintIds') or ['general']:
        key = f'{identity}:{target["dimension"]}'
        if key not in state['asked']:
            state['asked'].append(key)
    flow['focus'] = {key: target[key] for key in ('topic', 'aspect', 'dimension', 'scope', 'complaintIds')}
    return target
