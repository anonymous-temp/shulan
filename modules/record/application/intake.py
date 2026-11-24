"""Evidence-backed intake coverage and question selection, without model I/O."""

import re
from copy import deepcopy
from typing import Literal
from uuid import uuid4

from pydantic import AliasChoices
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import field_validator
from pydantic import model_validator

from modules.record.application import lifestyle
from modules.record.application.validation import RecordValidationError

Dimension = Literal[
    'chief_complaint',
    'priority',
    'goal',
    'character',
    'onset',
    'frequency',
    'impact',
    'associated_symptoms',
    'habits',
    'diet_structure',
    'meal_pattern',
    'hydration',
    'activity',
    'sleep',
    'sedentary',
    'smoking',
    'alcohol',
    'stress_coping',
    'staple_portion',
    'sweet_intake',
    'salt_processed_food',
    'late_meals',
    'water_intake',
    'sleep_duration',
    'sleep_consistency',
    'bedtime_habits',
    'exercise_frequency',
    'exercise_duration',
    'activity_breaks',
    'passive_smoking',
    'alcohol_amount',
    'rest_schedule',
    'work_breaks',
    'weight_change',
    'medical_history',
    'medication',
    'family_history',
    'other_symptoms',
]
DIMENSION_LABELS = {
    'chief_complaint': '主要困扰',
    'priority': '困扰优先级',
    'goal': '希望了解的问题',
    'character': '不适感受',
    'onset': '出现时间',
    'frequency': '出现频率',
    'impact': '生活影响',
    'associated_symptoms': '伴随表现',
    'habits': '生活习惯',
    'diet_structure': '饮食结构',
    'meal_pattern': '进食规律',
    'hydration': '饮水情况',
    'activity': '日常活动',
    'sleep': '睡眠情况',
    'sedentary': '久坐情况',
    'smoking': '吸烟情况',
    'alcohol': '饮酒情况',
    'stress_coping': '压力应对与放松',
    'weight_change': '体重变化',
    'medical_history': '既往病史',
    'medication': '用药情况',
    'family_history': '家族病史',
    'other_symptoms': '其他不适',
    **lifestyle.LABELS,
}
DETAIL_DIMENSIONS = frozenset(('character', 'onset', 'frequency', 'impact', 'associated_symptoms'))
LIFESTYLE_CATEGORIES = lifestyle.CATEGORIES
LIFESTYLE_CATEGORY_BY_DIMENSION = lifestyle.CATEGORY_BY_DIMENSION
LIFESTYLE_DIMENSIONS = frozenset(LIFESTYLE_CATEGORY_BY_DIMENSION)
BACKGROUND_DIMENSIONS = (
    frozenset(('habits', 'medical_history', 'medication', 'family_history', 'weight_change')) | LIFESTYLE_DIMENSIONS
)
BACKGROUND_ACCEPT = '可以聊聊'
BACKGROUND_DECLINE = '暂时不想补充'
STATUS_LABELS = {'known': '已了解', 'unknown': '用户说不清', 'declined': '用户不愿回答', 'conflicting': '存在矛盾'}
_UNSPECIFIED_ENTITY = re.compile(r'^(?:某(?:个|种|些|一)|其他(?:疾病|症状|情况))')
_UNSPECIFIED_ENTITY_NAMES = frozenset(
    ('身体不舒服', '身体不适', '某个不适', '不舒服', '有病史', '慢性病', '疾病', '症状', '不清楚', '不知道', '未知')
)


def normalize_dimension(value):
    return (
        {label: key for key, label in DIMENSION_LABELS.items()}.get(value, value) if isinstance(value, str) else value
    )


class IntakeModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)


class IntakeEvidence(IntakeModel):
    source: Literal['本次回答', '历史回答', '本次报告', '长期画像', '附带资料'] = Field(
        validation_alias=AliasChoices('来源', 'source')
    )
    quote: str = Field(validation_alias=AliasChoices('原文', 'quote'), min_length=1, max_length=2000)


class ComplaintObservation(IntakeModel):
    id: str | None = Field(default=None, validation_alias=AliasChoices('编号', 'id'))
    text: str = Field(validation_alias=AliasChoices('困扰', 'text'), min_length=1, max_length=600)
    primary: bool = Field(default=False, validation_alias=AliasChoices('主要困扰', 'primary'))
    evidence: list[IntakeEvidence] = Field(
        validation_alias=AliasChoices('依据', 'evidence'), min_length=1, max_length=6
    )


class IntakeFact(IntakeModel):
    dimension: Dimension = Field(validation_alias=AliasChoices('内容', 'dimension'))
    scope: Literal['complaint', 'primary', 'general'] = Field(validation_alias=AliasChoices('范围', 'scope'))
    complaintIndexes: list[int] = Field(
        default_factory=list, validation_alias=AliasChoices('困扰索引', 'complaintIndexes'), max_length=10
    )
    status: Literal['known', 'unknown', 'declined', 'conflicting'] = Field(
        validation_alias=AliasChoices('状态', 'status')
    )
    summary: str = Field(validation_alias=AliasChoices('已知情况', 'summary'), min_length=1, max_length=1500)
    evidence: list[IntakeEvidence] = Field(
        validation_alias=AliasChoices('依据', 'evidence'), min_length=1, max_length=6
    )
    # Optional on older model results and persisted sessions. These describe the
    # user's claim, independently of the coverage status used for question choice.
    presence: Literal['present', 'absent', 'unknown', 'conflicting'] | None = None
    specificity: Literal['specific', 'unspecified'] | None = None
    entities: list[str] = Field(default_factory=list, max_length=20)

    _dimension = field_validator('dimension', mode='before')(normalize_dimension)

    @model_validator(mode='after')
    def validate_answer_meaning(self):
        if self.presence is None and self.specificity is None and not self.entities:
            return self
        if self.presence is None or self.specificity is None:
            raise RecordValidationError('incomplete_answer_meaning', '存在性与具体程度须一起提供')
        if self.specificity == 'specific' and not self.entities:
            raise RecordValidationError('specific_answer_requires_entity', '明确的回答须保留用户实际说明的具体内容')
        if self.specificity == 'unspecified' and self.entities:
            raise RecordValidationError('unspecified_answer_has_entity', '具体内容未明确时不填写实体名称')
        if any(not entity.strip() for entity in self.entities) or len(set(self.entities)) != len(self.entities):
            raise RecordValidationError('invalid_answer_entities', '具体内容不能为空或重复')
        if any(_UNSPECIFIED_ENTITY.search(entity) or entity in _UNSPECIFIED_ENTITY_NAMES for entity in self.entities):
            raise RecordValidationError('answer_entity_unspecified', '泛化描述须保留为未明确，不能当作具体实体')
        if any(not any(entity in evidence.quote for evidence in self.evidence) for entity in self.entities):
            raise RecordValidationError('answer_entity_requires_evidence', '具体内容须直接来自用户或资料原文')
        if self.presence == 'conflicting' and self.status != 'conflicting':
            raise RecordValidationError('answer_meaning_conflict', '矛盾回答须保留冲突状态')
        return self


class IntakeUnderstanding(IntakeModel):
    _resolved_complaint_ids: list[str] | None = PrivateAttr(default=None)
    complaints: list[ComplaintObservation] = Field(validation_alias=AliasChoices('困扰', 'complaints'), max_length=10)
    priorityClarificationNeeded: bool = Field(
        default=False, validation_alias=AliasChoices('需确认重点', 'priorityClarificationNeeded')
    )
    preferredIndex: int | None = Field(default=None, validation_alias=AliasChoices('优先困扰索引', 'preferredIndex'))
    facts: list[IntakeFact] = Field(validation_alias=AliasChoices('已了解的信息', 'facts'), max_length=30)
    backgroundConsent: Literal['unspecified', 'accepted', 'declined'] = Field(
        default='unspecified', validation_alias=AliasChoices('背景补充意愿', 'backgroundConsent')
    )


def new_intake() -> dict:
    return {
        'schemaVersion': 3,
        'backgroundConsent': 'unasked',
        'backgroundDimension': None,
        'acceptedBackgroundDimensions': [],
        'complaints': [],
        'focusIds': [],
        'preferredId': None,
        'priorityAsked': False,
        # Kept as a compatibility projection; selection never reads this field.
        'primaryId': None,
        'facts': {},
        'asked': [],
        'conflictAsked': [],
        'detailStreak': 0,
    }


def normalize_state(flow: dict) -> dict:
    """Upgrade a draft projection, without changing a cached/historical session."""
    original = flow.get('intake') or {}
    state = new_intake()
    state.update(deepcopy(original))
    known = {item['id'] for item in state['complaints']}
    if original.get('schemaVersion') not in (2, 3):
        primary = original.get('primaryId')
        state['focusIds'] = [primary] if primary in known else [item['id'] for item in state['complaints']]
        # Older primary flags were also assigned by models, not just users.
        state['preferredId'] = None
        state['priorityAsked'] = any(key.endswith(':priority') for key in state['asked'])
    state['schemaVersion'] = 3
    state['focusIds'] = [identity for identity in state['focusIds'] if identity in known]
    if state['preferredId'] not in known:
        state['preferredId'] = None
    if not original:
        state['detailStreak'] = min(2, (flow.get('focus') or {}).get('depth', 0))
    for key, fact in state['facts'].items():
        identity = key.rsplit(':', 1)[0]
        fact.pop('complaintIndexes', None)
        fact['scope'] = 'general' if identity == 'general' else 'complaint'
        fact['complaintIds'] = [] if identity == 'general' else [identity]
        if fact['dimension'] == 'priority' and fact['status'] in ('known', 'unknown', 'declined'):
            state['priorityAsked'] = True
    return state


def validate_targets(understanding: IntakeUnderstanding, item) -> None:
    indexes = item.complaintIndexes
    if len(indexes) != len(set(indexes)) or any(
        index < 0 or index >= len(understanding.complaints) for index in indexes
    ):
        raise RecordValidationError('invalid_complaint_reference', '困扰索引必须引用本轮困扰列表，不能重复或越界')
    if item.scope == 'general' and indexes:
        raise RecordValidationError('invalid_complaint_reference', '共同背景不能同时指定某个困扰')
    if item.scope == 'complaint' and not indexes:
        raise RecordValidationError('complaint_reference_required', '困扰信息必须指定本轮困扰索引')
    if item.dimension in DETAIL_DIMENSIONS and item.scope == 'general':
        raise RecordValidationError('detail_requires_complaint_scope', '症状信息必须归属具体困扰，不能泛化为共同背景')


def _fragments(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _fragments(item)
    elif isinstance(value, list):
        for item in value:
            yield from _fragments(item)


def _existing_complaint(complaints: list, observation: ComplaintObservation) -> dict | None:
    if observation.id:
        return next((item for item in complaints if item['id'] == observation.id), None)
    matches = [item for item in complaints if item['text'] == observation.text]
    if len(matches) > 1:
        raise RecordValidationError(
            'ambiguous_complaint_identity', '同名困扰有多个已有编号，请明确沿用对应 id（编号），不能只凭名称关联'
        )
    return matches[0] if matches else None


def validate_understanding(understanding: IntakeUnderstanding, session: dict, answer, sources: dict) -> None:
    state = normalize_state(session['qaFlow'])
    if understanding.backgroundConsent != 'unspecified' and (
        state['backgroundConsent'] != 'invited' or not any(_fragments(answer))
    ):
        raise RecordValidationError('background_consent_source', '背景补充意愿仅根据用户对本次补充邀请的回答填写')
    known = {item['id'] for item in state['complaints']}
    evidence_sources = {
        '本次回答': list(_fragments(answer)),
        '历史回答': [text for item in session.get('qaHistory', []) for text in _fragments(item.get('answer'))],
        '本次报告': list(_fragments(sources.get('本次报告'))),
        '长期画像': list(_fragments(sources.get('长期健康画像'))),
        '附带资料': list(_fragments(sources.get('附带健康资料'))),
    }
    if understanding.preferredIndex is not None and not 0 <= understanding.preferredIndex < len(
        understanding.complaints
    ):
        raise RecordValidationError('invalid_complaint_preference', '优先困扰必须引用本轮困扰列表')
    seen = set()
    ids = []
    for index, complaint in enumerate(understanding.complaints):
        existing = _existing_complaint(state['complaints'], complaint)
        key = complaint.id or (existing['id'] if existing else complaint.text)
        if key in seen or (complaint.id and complaint.id not in known):
            raise RecordValidationError('invalid_complaint_identity', '困扰编号必须沿用已有编号，不能重复或编造')
        seen.add(key)
        ids.append(existing['id'] if existing else f'new:{index}')
        if any(item.source not in ('本次回答', '历史回答') for item in complaint.evidence):
            raise RecordValidationError(
                'complaint_requires_user_evidence', '主要困扰只能依据用户陈述，不能将报告发现当作主诉'
            )
    for item in [*understanding.complaints, *understanding.facts]:
        for evidence in item.evidence:
            if not any(source.strip() for source in evidence_sources[evidence.source]):
                raise RecordValidationError(
                    'intake_source_unavailable', '指定来源必须有实际文本内容，不能用 AI 问题或说明代替用户回答'
                )
    for fact in understanding.facts:
        validate_targets(understanding, fact)
        if fact.presence is not None:
            for entity in fact.entities:
                if not any(
                    entity in evidence.quote and any(entity in raw for raw in evidence_sources[evidence.source])
                    for evidence in fact.evidence
                ):
                    raise RecordValidationError('answer_entity_requires_source', '具体内容必须有对应来源原文支持')
        if fact.dimension in ('chief_complaint', 'priority', 'goal'):
            if any(item.source not in ('本次回答', '历史回答') for item in fact.evidence):
                raise RecordValidationError('goal_requires_user_evidence', '主诉、优先级与诉求只能依据用户陈述')
    # Validate the same physical slots that will be merged after assigning IDs.
    _aggregate_facts(state, understanding, ids)


def merge_understanding(flow: dict, understanding: IntakeUnderstanding) -> dict:
    state = normalize_state(flow)
    flow['intake'] = state
    matched = [_existing_complaint(state['complaints'], observation) for observation in understanding.complaints]
    ids = []
    for observation, item in zip(understanding.complaints, matched):
        if item is None:
            item = {'id': f'complaint_{uuid4().hex}'}
            state['complaints'].append(item)
        item.update(text=observation.text, evidence=[e.model_dump() for e in observation.evidence])
        ids.append(item['id'])
    understanding._resolved_complaint_ids = list(ids)
    if ids:
        state['focusIds'] = list(ids)
        state['primaryId'] = state['focusIds'][0] if state['focusIds'] else None
    if understanding.preferredIndex is not None:
        state['preferredId'] = ids[understanding.preferredIndex]
    state['priorityClarificationNeeded'] = understanding.priorityClarificationNeeded
    for fact in understanding.facts:
        if fact.dimension == 'priority' and fact.status in ('known', 'unknown', 'declined'):
            state['priorityAsked'] = True
            if understanding.preferredIndex is None:
                state['preferredId'] = None
    state['facts'].update(_aggregate_facts(state, understanding, ids))
    return state


def _aggregate_facts(state: dict, understanding: IntakeUnderstanding, ids: list[str]) -> dict:
    """Combine this turn's compatible subfacts; never append to an older snapshot."""
    aggregated = {}
    summaries = {}
    for fact in understanding.facts:
        targets = _target_ids(state, understanding, fact, ids)
        if not targets:
            raise RecordValidationError(
                'primary_complaint_required',
                '未明确主要困扰时，不能把事实归属到主要困扰；'
                '首次无用户回答时困扰应为空，报告或画像支持的背景事实使用 general，不能编造主诉。',
            )
        for identity in targets:
            key = f'{identity}:{fact.dimension}'
            if key not in aggregated:
                aggregated[key] = {
                    'dimension': fact.dimension,
                    'scope': 'general' if identity == 'general' else 'complaint',
                    'complaintIds': [] if identity == 'general' else [identity],
                    'status': fact.status,
                    'summary': '',
                    'evidence': [],
                }
                summaries[key] = []
            value = aggregated[key]
            if value['status'] != fact.status:
                raise RecordValidationError(
                    'duplicate_intake_fact',
                    '同一困扰和内容的多条信息状态不同，请合并说明矛盾或不确定之处并给出一致状态，不能覆盖其中一条',
                )
            if fact.summary not in summaries[key]:
                summaries[key].append(fact.summary)
            value['summary'] = '\n'.join(summaries[key])
            if fact.presence is not None:
                observation = {
                    'presence': fact.presence,
                    'specificity': fact.specificity,
                    'entities': list(fact.entities),
                    'description': fact.summary,
                    'evidence': [item.model_dump() for item in fact.evidence],
                }
                observations = value.setdefault('observations', [])
                if observation not in observations:
                    observations.append(observation)
            for item in fact.evidence:
                evidence = item.model_dump()
                if evidence not in value['evidence']:
                    value['evidence'].append(evidence)
    return aggregated


def _legacy_ids(state: dict, understanding: IntakeUnderstanding, ids: list[str]) -> list[str]:
    marked = [identity for identity, item in zip(ids, understanding.complaints) if item.primary]
    candidates = marked or ids or state['focusIds']
    if len(candidates) > 1:
        raise RecordValidationError(
            'ambiguous_complaint_reference',
            '旧 primary 范围无法确定唯一困扰，请使用 complaint 范围并提供 complaintIndexes（困扰索引）明确归属',
        )
    return candidates


def _target_ids(state: dict, understanding: IntakeUnderstanding, item, ids: list[str]) -> list[str]:
    if item.scope == 'general':
        return ['general']
    if item.complaintIndexes:
        return [ids[index] for index in item.complaintIndexes]
    return _legacy_ids(state, understanding, ids) if item.scope == 'primary' else []


def resolve_gaps(flow: dict, understanding: IntakeUnderstanding, gaps: list) -> list:
    """Bind this assessment's indexes to the IDs assigned by its preceding merge."""
    state = flow['intake']
    ids = understanding._resolved_complaint_ids
    if ids is None or len(ids) != len(understanding.complaints):
        raise RecordValidationError('unresolved_complaint_reference', '必须先合并本轮困扰，再关联信息缺口')
    resolved = []
    for gap in gaps:
        validate_targets(understanding, gap)
        value = gap.model_copy(deep=True)
        value.complaintIds = [
            identity for identity in _target_ids(state, understanding, gap, ids) if identity != 'general'
        ]
        resolved.append(value)
    return resolved


def _gap_ids(state: dict, gap) -> list[str]:
    if gap.complaintIds:
        return gap.complaintIds
    if gap.scope == 'general':
        if gap.optionalBackground:
            return ['general']
        if gap.dimension in BACKGROUND_DIMENSIONS and state['focusIds']:
            return state['focusIds']
        return ['general']
    return state['focusIds'] if gap.scope == 'primary' else []


def _related_keys(state: dict, gap, identity: str) -> list[str]:
    keys = [f'{identity}:{gap.dimension}']
    if gap.dimension in BACKGROUND_DIMENSIONS and identity != 'general':
        keys.append(f'general:{gap.dimension}')
    # Legacy broad habits cannot be reliably split. Keep them as conservative
    # coverage instead of asking the patient to repeat an unclassified answer.
    if gap.dimension in LIFESTYLE_DIMENSIONS:
        keys.extend((f'{identity}:habits', 'general:habits'))
        if identity == 'general':
            keys.extend(f'{owner}:habits' for owner in state['focusIds'])
    if gap.dimension == 'habits':
        keys.extend(f'{owner}:{dimension}' for owner in (identity, 'general') for dimension in LIFESTYLE_DIMENSIONS)
    return keys


def update_background_consent(flow: dict, answer, assessed: str = 'unspecified') -> None:
    """Only an invitation that was actually published can grant background consent."""
    state = normalize_state(flow)
    flow['intake'] = state
    if state['backgroundConsent'] != 'invited' or answer is None:
        return
    value = answer
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    elif isinstance(value, dict) and set(value) == {'content'}:
        value = value['content']
    if isinstance(value, str):
        value = value.strip()
    if value == BACKGROUND_DECLINE:
        consent = 'declined'
    elif value == BACKGROUND_ACCEPT:
        consent = 'accepted'
    else:
        consent = assessed if assessed != 'unspecified' else 'deferred'
    state['backgroundConsent'] = consent
    dimension = state['backgroundDimension']
    if consent == 'accepted' and dimension and dimension not in state['acceptedBackgroundDimensions']:
        state['acceptedBackgroundDimensions'].append(dimension)


def candidates(flow: dict, gaps: list) -> list:
    """Filter answered/unavailable slots and repeated purposes before ranking."""
    state = normalize_state(flow)
    result = []
    for gap in gaps:
        if not state['complaints'] and gap.scope != 'general':
            continue
        if gap.optionalBackground and state['backgroundConsent'] in ('declined', 'deferred'):
            continue
        if state['complaints'] and gap.dimension == 'chief_complaint':
            continue
        if gap.dimension == 'priority' and (
            not state.get('priorityClarificationNeeded') or state['priorityAsked'] or state['preferredId']
        ):
            continue
        remaining = []
        for identity in _gap_ids(state, gap):
            keys = _related_keys(state, gap, identity)
            fact = next((state['facts'][key] for key in keys if key in state['facts']), None)
            conflict = (
                fact and fact['status'] == 'conflicting' and not any(key in state['conflictAsked'] for key in keys)
            )
            if fact and not conflict:
                continue
            if any(key in state['asked'] for key in keys) and not conflict:
                continue
            remaining.append(identity)
        if remaining:
            value = gap.model_copy(deep=True)
            value.complaintIds = [identity for identity in remaining if identity != 'general']
            result.append(value)
    if state['preferredId']:
        result.sort(key=lambda item: state['preferredId'] not in item.complaintIds)
    # Keep the assessment's relevance order; do not pivot just to vary topics.
    # An independent background invitation must not displace the user's concern.
    result.sort(key=lambda item: item.optionalBackground)
    return result


def select_gap(flow: dict, eligible: list) -> dict | None:
    if not eligible:
        return None
    state = flow['intake']
    gap = eligible[0]
    if gap.optionalBackground and gap.dimension not in state['acceptedBackgroundDimensions']:
        state['backgroundConsent'] = 'invited'
        state['backgroundDimension'] = gap.dimension
        flow['focus'] = None
        return {
            **gap.model_dump(exclude={'complaintIndexes'}),
            'complaintIds': [],
            'complaints': [],
            'backgroundInvitation': True,
        }
    for identity in _gap_ids(state, gap):
        key = f'{identity}:{gap.dimension}'
        keys = _related_keys(state, gap, identity)
        fact = next((state['facts'][item] for item in keys if item in state['facts']), None)
        if any(item in state['asked'] for item in keys) or (fact and fact['status'] == 'conflicting'):
            if key not in state['conflictAsked']:
                state['conflictAsked'].append(key)
        if key not in state['asked']:
            state['asked'].append(key)
    if gap.dimension == 'priority':
        state['priorityAsked'] = True
    if gap.complaintIds:
        state['focusIds'] = list(gap.complaintIds)
        state['primaryId'] = gap.complaintIds[0]
    state['detailStreak'] = state['detailStreak'] + 1 if gap.dimension in DETAIL_DIMENSIONS else 0
    flow['focus'] = {
        'topic': gap.topic,
        'aspect': gap.aspect,
        'dimension': gap.dimension,
        'scope': gap.scope,
        'complaintIds': list(gap.complaintIds),
    }
    return {
        **gap.model_dump(exclude={'complaintIndexes'}),
        'complaintIds': list(gap.complaintIds),
        'complaints': [
            {'id': item['id'], 'text': item['text']} for item in state['complaints'] if item['id'] in gap.complaintIds
        ],
    }


def patient_projection(flow: dict) -> dict:
    """Expose patient facts and questions already asked, without counters/phase."""
    state = normalize_state(flow)
    complaints = {item['id']: {'编号': item['id'], '困扰': item['text']} for item in state['complaints']}

    def targets(identity):
        return [complaints[identity]] if identity in complaints else []

    return {
        '背景补充意愿': {
            'unasked': '尚未邀请',
            'invited': '已邀请，等待用户回答',
            'accepted': '用户愿意补充',
            'declined': '用户不愿补充，不再转向独立背景',
            'deferred': '用户尚未明确同意，不再转向独立背景',
        }[state['backgroundConsent']],
        '本次邀请的背景': DIMENSION_LABELS.get(state['backgroundDimension']),
        '已同意补充的背景': [DIMENSION_LABELS[dimension] for dimension in state['acceptedBackgroundDimensions']],
        '已识别的困扰': [
            {
                **complaints[c['id']],
                '主要困扰': c['id'] in state['focusIds'],
                '用户优先关注': c['id'] == state['preferredId'],
            }
            for c in state['complaints']
        ],
        '已了解的信息': [
            {
                '内容': DIMENSION_LABELS[item['dimension']],
                '状态': STATUS_LABELS[item['status']],
                '已知情况': item['summary'],
                '依据': item['evidence'],
                '范围': item['scope'],
                '困扰': targets(key.rsplit(':', 1)[0]),
            }
            for key, item in state['facts'].items()
        ],
        '已问过的内容': [DIMENSION_LABELS[key.rsplit(':', 1)[-1]] for key in state['asked']],
        '已问内容的归属': [
            {
                '内容': DIMENSION_LABELS[key.rsplit(':', 1)[-1]],
                '困扰': targets(key.rsplit(':', 1)[0]),
                '范围': 'general' if key.startswith('general:') else 'complaint',
            }
            for key in state['asked']
        ],
    }
