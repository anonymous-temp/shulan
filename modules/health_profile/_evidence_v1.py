"""Frozen v1 statement semantics, independently executable from legacy Profile.

Copied once from health_profile/record_observations.py at 0b79755140655566b25d6ac55fa2253ec1e50a4d.
The 54-definition dependency closure of question_topic/normalize_record_statement
includes its own constants and helpers. Do not import the current legacy module
or change this revision when introducing a different interpretation policy.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

HISTORY_STATUS = 'condition.history_status'


FAMILY_REPORTED = 'family_history.reported'


OBSERVATION_TYPES = frozenset({HISTORY_STATUS, FAMILY_REPORTED})


OBSERVATION_LABELS = {HISTORY_STATUS: '个人病史说明', FAMILY_REPORTED: '家族病史说明'}


_BOUNDARY = re.compile(r'[，,。！？!?；;\n]|但是|不过|但|而是')


_UNKNOWN = re.compile(r'不清楚|不知道|不确定|不明白|记不清|不记得|忘了|忘记|说不清|不明确|未明确|不详|没听说|未听说')


_DENIED = re.compile(
    r'(?:^|我|本人|自己|目前|现在|一直|从来|家里|家族|亲属|血亲|都|均)'
    r'(?:没有|无(?!症状|痛|创)|未患|没患|没得|未确诊|未被确诊|否认)'
    r'|^(?:没有|无(?!症状|痛|创)|未患|没患|没得|未确诊|否认|从不|未见|未发现|不存在)'
)


_QUALIFIED = re.compile(r'不是没有|并非没有|不一定|未必|如果|假如|要是|除非|只有|只在|时候|情况下')


_POSITIVE = re.compile(
    r'患有|患了|确诊|诊断为|有(?:过)?(?:病史|疾病史|慢性病|疾病|病|症状|不适)'
    r'|有(?:某|一)(?:种|个).{0,8}(?:病|症状|不适)|不舒服|不适|有(?:人|家人|亲属|血亲).{0,12}(?:有|患|得)'
)


_INTEREST = re.compile(
    r'想(?:要)?|希望|想了解|了解|咨询|关注|预防|担心|改善|控制|管理|解释|是否|是不是|会不会|怀疑|疑似|可能|如果|假如|要是'
)


_HYPOTHETICAL = re.compile(r'怀疑|疑似|可能|如果|假如|要是|担心')


_CLINICAL_ASSERTION = r'(?:确诊(?:为)?|诊断(?:为|是|[:：])|患(?:有|了)?|得了|有(?:过)?)\s*{entity}'


_VAGUE_ENTITY = re.compile(
    r'某(?:个|种|些|一)|一种|不清楚|不明确|不详|不知道|身体不(?:适|舒服)|健康问题|身体问题'
    r'|^(?:有|无|没有|不适|症状|疾病|病史|疾病史|个人疾病史|家族史|家族病史|慢性病|某病|其他|其它)$'
)


_BLOOD = re.compile(
    r'家族|家人|家里|血亲|亲属|父亲|母亲|爸爸|妈妈|父母|兄弟|姐妹|哥哥|弟弟|姐姐|妹妹'
    r'|祖父|祖母|外祖父|外祖母|爷爷|奶奶|外公|外婆|儿子|女儿|子女'
)


_NON_BLOOD = re.compile(r'朋友|同事|邻居|配偶|丈夫|妻子|老公|老婆|继父|继母')


_SELF = re.compile(r'我(?:自己|本人)?|本人|自己')


_EXPLICIT_SELF_HISTORY = re.compile(
    r'(?:我(?:自己|本人)?|本人|自己)(?:目前|现在|以前|曾经|从来|已经)?(?:有|没有|患|无|没|未患|确诊|得|不患)'
)


_REFUSAL = re.compile(r'不想(?:回答|说)|不愿|不方便|拒绝|跳过')


_SUPPLEMENT_CONTROLS = frozenset(
    {'没有', '没有需要补充的内容', '没有补充', '目前记不清', '不清楚', '没什么补充', '不用更正'}
)


_EXCLUSIVE_STATES = frozenset({'没有', '无', '不清楚', '目前记不清', '暂无改善需求'})


_TREATMENT_OR_TEST_DENIAL = re.compile(r'(?:没有|未|不).{0,5}(?:治疗|检查|筛查|用药|服药|接受|就医)')


_CONDITION_MENTION = re.compile(
    r'糖尿病|高血压|高血脂|血脂异常|冠心病|脑卒中|脂肪肝|慢性肾病|地中海贫血|血友病'
    r'|[\u4e00-\u9fffA-Za-z\d]{1,24}(?:病|炎|癌|瘤|综合征|贫血)'
)


_RELATIVES = {
    '父亲': ('父亲', '爸爸', '父'),
    '母亲': ('母亲', '妈妈', '母'),
    '外祖父': ('外祖父', '外公'),
    '外祖母': ('外祖母', '外婆'),
    '祖父': ('祖父', '爷爷'),
    '祖母': ('祖母', '奶奶'),
    '哥哥': ('哥哥',),
    '弟弟': ('弟弟',),
    '姐姐': ('姐姐',),
    '妹妹': ('妹妹',),
    '儿子': ('儿子',),
    '女儿': ('女儿',),
}


_SUBJECT_TOKEN = re.compile(
    '|'.join(
        sorted(
            {alias for aliases in _RELATIVES.values() for alias in aliases if len(alias) > 1}
            | {
                '我',
                '本人',
                '自己',
                '家里',
                '家人',
                '血亲',
                '父母',
                '兄弟姐妹',
                '配偶',
                '老婆',
                '老公',
                '丈夫',
                '妻子',
                '朋友',
                '同事',
            },
            key=len,
            reverse=True,
        )
    )
)


_RESOLVED = re.compile(
    r'已经好了|已好了|已康复|已经康复|已治愈|已经治愈|现在好了|今年好了|目前好了|不再(?:头痛|疼痛|不适)|已恢复'
)


_PAST = re.compile(r'以前|曾经|去年|既往|过去|小时候|有过')


_STATE_CONTINUATION = re.compile(r'好了|康复|治愈|恢复|目前|现在|今年|持续|多年|一直|治疗|用药')


_COORDINATOR = re.compile(r'以及|和|及|、')


_ASSERTION_PREDICATE = re.compile(
    r'(?:没有|未|不曾|从未)(?:被(?:医生)?)?(?:明确)?(?:确诊|诊断)(?:过|为|是|[:：])?'
    r'|有没有|有无|患没患|没有|未患|没患|没得|未确诊|不患|否认|不存在|未见|未发现'
    r'|确诊(?:过|为)?|诊断(?:为|是|[:：])|患(?:有|了|过)?|得了|有过|有|无'
    r'|了解|咨询|筛查|检查|检测|治疗|预防'
)


_COORDINATE_BREAK = re.compile(
    r'(?:以及|和|及|、)\s*(?=(?:' + _SUBJECT_TOKEN.pattern + r')'
    r'|(?:目前|现在|以前|曾经|从未|已经)?(?:' + _ASSERTION_PREDICATE.pattern + r'))'
)


_NON_NOMINAL = re.compile(
    r'想|希望|需要|进行|接受|治疗|筛查|检查|检测|咨询|了解|预防|确诊|诊断|患|没有|不清楚|不知道'
    r'|记不清|忘|已经|目前|现在|以前|曾经|家族史|病史|是否|有没有|可能|如果|假如|要是|担心|疑似'
)


def _clause_boundaries(text: str) -> list[re.Match]:
    """A conjunction before a new owner/predicate is not a disease-list join."""
    return sorted([*_BOUNDARY.finditer(text), *_COORDINATE_BREAK.finditer(text)], key=lambda match: match.start())


@dataclass(frozen=True, slots=True)
class RecordStatement:
    fact_type: str
    value_type: str
    value: Any
    display_name: str
    subject_kind: str
    subject_hint: str | None
    evidence_text: str


def source_strings(value: Any) -> Iterator[str]:
    """Only original string values count as statements, never object key names."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for detail in value.values():
            yield from source_strings(detail)
    elif isinstance(value, list):
        for detail in value:
            yield from source_strings(detail)


def question_topic(question: str, dimension: str | None = None) -> str | None:
    if dimension in {'goal', 'medical_history', 'family_history'}:
        return dimension
    if isinstance(dimension, str) and dimension:
        return None
    if re.search(r'家族|血亲|父母|父亲|母亲|兄弟姐妹', question) and (
        re.search(r'病史|疾病|患病|诊断|确诊', question) or _CONDITION_MENTION.search(question)
    ):
        return 'family_history'
    if re.search(r'疾病史|既往病史|既往史|(?:医生|确诊|诊断).{0,14}疾病|疾病.{0,14}(?:确诊|诊断)', question):
        return 'medical_history'
    if re.search(r'(?:本次|这次|最希望|最关注).{0,25}(?:了解|改善|关注|健康|问题|困扰)|主诉', question):
        return 'goal'
    return None


def local_evidence(evidence: str, answer: Any) -> str:
    """Keep qualifiers around a model's selected substring in the same clause."""
    for raw in source_strings(answer):
        start = raw.find(evidence)
        if start < 0:
            continue
        end = start + len(evidence)
        left = max((m.end() for m in _BOUNDARY.finditer(raw) if m.end() <= start), default=0)
        right = min((m.start() for m in _BOUNDARY.finditer(raw) if m.start() >= end), default=len(raw))
        return raw[left:right].strip()
    return evidence


def _source_subject(text: str) -> str | None:
    """The explicit owner in a source clause takes precedence over the question."""
    tokens = list(_SUBJECT_TOKEN.finditer(text))
    if not tokens:
        return None
    token = tokens[-1][0]
    if token in {'我', '本人', '自己'}:
        return 'self'
    for relationship, aliases in _RELATIVES.items():
        if token in aliases:
            return relationship
    return 'non_blood' if _NON_BLOOD.search(token) else 'family'


def _relationship_key(hint: str | None) -> str | None:
    if hint:
        for relationship, aliases in _RELATIVES.items():
            if hint in aliases:
                return relationship
    return None


def scoped_evidence(
    evidence: str,
    answer: Any,
    *,
    name: str,
    family: bool,
    relationship: str | None,
) -> tuple[str, bool]:
    """Resolve an entity and its owner before interpreting existence or time.

    Full-answer evidence is legitimate. A different person's statement, or a
    denial of another disease, must not change this candidate's polarity.
    """
    candidates = []
    wanted = _relationship_key(relationship) if family else 'self'
    for raw in source_strings(answer):
        if evidence not in raw:
            continue
        spans = []
        left = 0
        for boundary in _clause_boundaries(raw):
            if raw[left : boundary.start()].strip():
                spans.append((left, boundary.start()))
            left = boundary.end()
        if raw[left:].strip():
            spans.append((left, len(raw)))
        owner, owner_start = None, 0
        for index, (start, end) in enumerate(spans):
            clause = raw[start:end].strip()
            explicit = _source_subject(clause)
            if explicit:
                owner, owner_start = explicit, start
            if name not in clause:
                continue
            actual_start = start if explicit or owner is None else owner_start
            actual_end = end
            # Preserve a following time/recovery clause for this same fact,
            # without crossing a new owner or a different clinical entity.
            for follow_start, follow_end in spans[index + 1 :]:
                following = raw[follow_start:follow_end].strip()
                following_owner = _source_subject(following)
                mentions = _CONDITION_MENTION.findall(following)
                if (
                    (following_owner is not None and following_owner != owner)
                    or any(mention != name for mention in mentions)
                    or not _STATE_CONTINUATION.search(following)
                ):
                    break
                actual_end = follow_end
            candidates.append((raw[actual_start:actual_end].strip(), owner))
    if not candidates:
        return local_evidence(evidence, answer), False
    exact = [item for item in candidates if item[1] == wanted] if wanted else []
    if exact:
        selected = exact
    elif family:
        selected = [item for item in candidates if item[1] not in {'self', 'non_blood', None}]
        # An explicitly self-reported answer to a family question remains self.
        selected = selected or [item for item in candidates if item[1] in {'self', None}]
    else:
        selected = [item for item in candidates if item[1] is None]
    if not selected:
        return candidates[0][0], False
    unique = list(dict.fromkeys(text for text, _ in selected))
    if len(unique) == 1:
        return unique[0], False
    polarities = {statement_presence(text, concrete=name) for text in unique}
    if len(polarities) == 1:
        return unique[0], False
    return evidence, True


def temporal_status(text: str) -> str | None:
    if _RESOLVED.search(text):
        return 'resolved'
    if _PAST.search(text):
        return 'past'
    return None


def _entity_context(text: str, name: str) -> str:
    """Interpret this entity's clause, retaining its own temporal continuation.

    Evidence can include an earlier clause to establish an inherited subject.
    That clause's different clinical entity does not supply this one's polarity.
    """
    clauses = _clause_boundaries(text)
    starts = [0, *(boundary.end() for boundary in clauses)]
    ends = [*(boundary.start() for boundary in clauses), len(text)]
    matching = [index for index, (start, end) in enumerate(zip(starts, ends, strict=True)) if name in text[start:end]]
    if len(matching) != 1:
        return text
    index = matching[0]
    end = ends[index]
    for follow_index in range(index + 1, len(starts)):
        following = text[starts[follow_index] : ends[follow_index]]
        if (
            _source_subject(following)
            or _CONDITION_MENTION.search(following)
            or not _STATE_CONTINUATION.search(following)
        ):
            break
        end = ends[follow_index]
    return text[starts[index] : end].strip()


def _assertion_context(text: str, name: str) -> str:
    """Project a shared list predicate onto one entity, for interpretation only.

    The original, contiguous evidence remains untouched. Only explicit noun-list
    joins may carry a predicate; a new subject, action or contrast ends its scope.
    This does not require the model to extract any of the preceding diseases.
    """
    context = _entity_context(text, name)
    boundary = _BOUNDARY.search(context)
    head = context[: boundary.start()] if boundary else context
    continuation = context[boundary.start() :] if boundary else ''
    occurrences = list(re.finditer(re.escape(name), head))
    if len(occurrences) != 1:
        return context
    occurrence = occurrences[0]
    joins = [
        join for join in _COORDINATOR.finditer(head) if not (occurrence.start() <= join.start() < occurrence.end())
    ]
    if not joins:
        return context
    starts = [0, *(join.end() for join in joins)]
    ends = [*(join.start() for join in joins), len(head)]
    parts = [head[start:end].strip() for start, end in zip(starts, ends, strict=True)]
    target_index = next(index for index, part in enumerate(parts) if name in part)
    target_suffix = parts[target_index].split(name, 1)[1].lstrip()
    if re.match(r'(?:的)?(?:筛查|检查|检测|治疗|咨询|预防|家族史)', target_suffix):
        # "有A和B的筛查/家族史" reports a test/background, not having B.
        return context
    # A qualifier at the end can govern the entire list ("A和B的可能").
    # Do not remove it when interpreting an earlier item: ambiguous scope must
    # retain the conservative whole-clause interpretation.
    if any(
        _HYPOTHETICAL.search(part) or _QUALIFIED.search(part) or _UNKNOWN.search(part)
        for part in parts[target_index + 1 :]
    ):
        return context
    prefix = parts[0][: parts[0].index(name)] if target_index == 0 else parts[0]
    predicates = list(_ASSERTION_PREDICATE.finditer(prefix))
    if not predicates:
        return context
    predicate = predicates[0]
    nouns = [parts[0][predicate.end() :].strip(), *parts[1:]]
    # The target's own suffix can carry time/recovery; earlier entries must be
    # plain nominal items. Do not turn a treatment or a new clause into a noun.
    for index, noun in enumerate(nouns[: target_index + 1]):
        nominal = noun[: noun.index(name)] + name if index == target_index else noun
        if (
            not re.fullmatch(r'[\w\u4e00-\u9fff·\- ]{2,80}', nominal)
            or _NON_NOMINAL.search(nominal)
            or _SUBJECT_TOKEN.search(nominal)
        ):
            return context
    return parts[0][: predicate.end()] + nouns[target_index] + continuation


def _has_clinical_assertion(text: str, name: str) -> bool:
    return bool(re.search(_CLINICAL_ASSERTION.format(entity=re.escape(name)), _assertion_context(text, name)))


def concrete_name(value: Any, display_name: str, evidence: str) -> str | None:
    candidates = []
    if isinstance(value, str):
        candidates.append(value.strip())
    elif isinstance(value, dict):
        candidates.extend(
            value.get(key) for key in ('name', 'condition', 'symptom', 'text') if isinstance(value.get(key), str)
        )
    label = re.sub(r'(?:家族史|病史)$', '', display_name.strip())
    candidates.insert(0, label)
    for name in candidates:
        if (
            isinstance(name, str)
            and 1 < len(name) <= 80
            and name in evidence
            and not _VAGUE_ENTITY.search(name)
            and not re.search(r'^(?:没有|未患|没患|否认|想|希望|患有|确诊|医生|家族|家里|父亲|母亲|有(?:过)?)', name)
            and not re.search(r'[，,。！？!?；;]|(?:我|本人|自己)(?:有|患|想)', name)
        ):
            return name
    return None


def statement_presence(text: str, *, concrete: str | None = None) -> str:
    """Conservative existence of the described item, independent of its name."""
    if concrete:
        text = _assertion_context(text, concrete)
    if text.strip('。！! ') in {'无', '没有', '暂无', '没有了', '暂无改善需求', '没有改善需求'}:
        return 'absent'
    if _QUALIFIED.search(text):
        return 'unknown'
    has_positive = bool(_POSITIVE.search(text))
    if concrete and re.search(_CLINICAL_ASSERTION.format(entity=re.escape(concrete)), text):
        has_positive = True
    if _UNKNOWN.search(text):
        existence_unknown = re.search(r'(?:不清楚|不知道|不确定|记不清).{0,6}(?:有没有|是否|有无|患没患)', text)
        return 'present' if has_positive and not existence_unknown else 'unknown'
    direct_denial = concrete and re.search(
        r'(?:没有|无|否认|未患|没患|未确诊|不患|不是|未见|未发现|不存在)'
        r'\s*(?:患(?:有|过)?|得过|确诊(?:过|为)?)?\s*' + re.escape(concrete),
        text,
    )
    diagnosis_denial = concrete and re.search(
        r'(?:没有|未|不曾|从未)(?:被(?:医生)?)?(?:明确)?(?:确诊|诊断)(?:过|为|是|[:：])?\s*' + re.escape(concrete),
        text,
    )
    trailing_denial = concrete and re.search(
        re.escape(concrete) + r'(?:病史)?\s*(?:没有|未见|未发现|不存在|未确诊|没有确诊|没确诊)[。！!\s]*$', text
    )
    if direct_denial or diagnosis_denial or trailing_denial or (not concrete and _DENIED.search(text)):
        return 'absent'
    if concrete and _DENIED.search(text):
        if _TREATMENT_OR_TEST_DENIAL.search(text) and _diagnosis_supported(text, concrete):
            return 'present'
        return 'unknown'
    return 'present'


def observation_value(
    description: str,
    *,
    presence: str,
    conditions: list[str] | None = None,
    relationship: str | None = None,
) -> dict[str, Any]:
    names = list(dict.fromkeys(conditions or []))
    return {
        'presence': presence,
        'specificity': 'specific' if names else 'unspecified',
        'description': description,
        'conditions': names,
        'relationship': relationship,
    }


def _observation(fact_type: str, text: str, *, presence: str, conditions=None, relationship=None) -> RecordStatement:
    return RecordStatement(
        fact_type,
        'object',
        observation_value(text, presence=presence, conditions=conditions, relationship=relationship),
        OBSERVATION_LABELS[fact_type],
        'self',
        '本人',
        text,
    )


def _family_context(text: str, topic: str | None) -> bool:
    if _NON_BLOOD.search(text):
        return False
    if _EXPLICIT_SELF_HISTORY.search(text) and not _BLOOD.search(text):
        return False
    return bool(_BLOOD.search(text) or topic == 'family_history')


def _interest_only(text: str, name: str | None) -> bool:
    if name:
        text = _assertion_context(text, name)
    if not _INTEREST.search(text):
        return False
    if _HYPOTHETICAL.search(text):
        return True
    return not (name and _has_clinical_assertion(text, name))


def _diagnosis_supported(text: str, name: str, relationship: str | None = None) -> bool:
    """A name mentioned in screening or an explanation is not a diagnosis."""
    text = _assertion_context(text, name)
    if _HYPOTHETICAL.search(text) or re.search(r'待排|待查|鉴别|排除', text):
        return False
    if re.search(_CLINICAL_ASSERTION.format(entity=re.escape(name)), text):
        return True
    if re.search(r'筛查|检查|检测', text):
        return False
    if re.search(
        re.escape(name)
        + r'(?:病史|史|已确诊|确诊过|已经确诊|确诊(?:\d+|[一二两三四五六七八九十多半几]+)(?:年|个月|月|天)|多年)',
        text,
    ):
        return True
    prefixes = [r'我', r'本人', r'自己']
    if relationship:
        prefixes.append(re.escape(relationship))
    return bool(re.fullmatch(r'(?:(?:' + '|'.join(prefixes) + r')\s*)?' + re.escape(name) + r'[。！!\s]*', text))


def _self_history(text: str, *, presence: str, names: list[str]) -> RecordStatement:
    if presence == 'present' and names and not temporal_status(text):
        return RecordStatement('condition.diagnosis', 'text', names[0], names[0], 'self', '本人', text)
    return _observation(HISTORY_STATUS, text, presence=presence, conditions=names)


def selection_observation(answer: Any, topic: str | None) -> RecordStatement | None:
    """Mixed exclusive shortcut states are evidence of conflict, not diagnoses."""
    if (
        topic not in {'medical_history', 'family_history', 'goal'}
        or not isinstance(answer, list)
        or not answer
        or not all(isinstance(item, str) for item in answer)
    ):
        return None
    choices = list(dict.fromkeys(item.strip() for item in answer))
    if len(choices) < 2 or not any(item in _EXCLUSIVE_STATES for item in choices):
        return None
    names = [item for item in choices if item not in _EXCLUSIVE_STATES and concrete_name(item, item, item) == item]
    description = json.dumps(answer, ensure_ascii=False)
    if topic == 'goal':
        return RecordStatement(
            'goal.health',
            'object',
            {
                'presence': 'conflicting',
                'specificity': 'specific' if names else 'unspecified',
                'description': description,
                'entities': names,
            },
            '本次健康关注',
            'self',
            '本人',
            description,
        )
    return _observation(
        FAMILY_REPORTED if topic == 'family_history' else HISTORY_STATUS,
        description,
        presence='conflicting',
        conditions=names,
    )


def normalize_record_statement(
    *,
    fact_type: str,
    value: Any,
    display_name: str,
    evidence: str,
    answer: Any,
    question: str,
    dimension: str | None,
    subject_kind: str,
    subject_hint: str | None,
    stable_relationship: bool,
) -> RecordStatement | None:
    """Validate selected entity against raw polarity and keep unclear history."""
    context = local_evidence(evidence, answer)
    topic = question_topic(question, dimension)
    if conflict := selection_observation(answer, topic):
        return conflict
    if not context or _REFUSAL.search(context):
        return None
    if re.search(r'补充|更正|修改', question) and context in _SUPPLEMENT_CONTROLS:
        return None
    name = concrete_name(value, display_name, context)
    if fact_type in OBSERVATION_TYPES and isinstance(value, dict):
        supplied_names = value.get('conditions', [])
        if isinstance(supplied_names, list) and len(supplied_names) == 1 and isinstance(supplied_names[0], str):
            name = concrete_name(supplied_names[0], supplied_names[0], context)
        elif not supplied_names:
            recovered = [item for item in _negative_condition_names(context) if _CONDITION_MENTION.fullmatch(item)]
            if len(recovered) == 1:
                name = recovered[0]
    if name and fact_type != 'goal.health':
        context, ambiguous = scoped_evidence(
            evidence,
            answer,
            name=name,
            family=fact_type in {'family_history.condition', FAMILY_REPORTED},
            relationship=value.get('relationship')
            if fact_type == FAMILY_REPORTED and isinstance(value, dict)
            else subject_hint,
        )
        if ambiguous:
            if fact_type == 'symptom.reported':
                return RecordStatement(
                    fact_type,
                    'object',
                    {'name': name, 'presence': 'conflicting', 'specificity': 'specific', 'description': context},
                    name,
                    'self',
                    '本人',
                    context,
                )
            return _observation(
                FAMILY_REPORTED if fact_type in {'family_history.condition', FAMILY_REPORTED} else HISTORY_STATUS,
                context,
                presence='conflicting',
                conditions=[name],
            )
    if fact_type in OBSERVATION_TYPES:
        if not isinstance(value, dict) or subject_kind != 'self':
            return None
        if set(value) - {'presence', 'specificity', 'description', 'conditions', 'relationship'}:
            return None
        description = value.get('description')
        if not isinstance(description, str) or not any(description in raw for raw in source_strings(answer)):
            return None
        supplied = value.get('conditions', [])
        if not isinstance(supplied, list) or len(supplied) > 16:
            return None
        names = []
        for condition in supplied:
            if not isinstance(condition, str) or concrete_name(condition, condition, context) != condition:
                return None
            names.append(condition)
        if not names:
            names = [name for name in _negative_condition_names(context) if _CONDITION_MENTION.fullmatch(name)]
        relationship = value.get('relationship')
        if relationship is not None and (
            not isinstance(relationship, str) or not relationship.strip() or relationship not in context
        ):
            return None
        if fact_type == FAMILY_REPORTED and relationship is None:
            relatives = set(
                re.findall(
                    r'父亲|母亲|爸爸|妈妈|哥哥|弟弟|姐姐|妹妹|祖父|祖母|外祖父|外祖母|爷爷|奶奶|外公|外婆|儿子|女儿',
                    context,
                )
            )
            if len(relatives) == 1:
                relationship = next(iter(relatives))
        presence = statement_presence(context, concrete=names[0] if len(names) == 1 else None)
        if (
            fact_type == HISTORY_STATUS
            and names
            and (
                _interest_only(context, names[0])
                or (topic == 'goal' and not _has_clinical_assertion(context, names[0]))
            )
        ):
            return RecordStatement('goal.health', 'text', context, '本次健康关注', 'self', '本人', context)
        if presence == 'present' and names and not _diagnosis_supported(context, names[0], relationship):
            presence = 'unknown'
        if value.get('presence') == 'conflicting':
            # A conflict may not be hidden behind a unilateral positive model value.
            presence = 'conflicting' if _DENIED.search(context) else presence
        if fact_type == FAMILY_REPORTED:
            if _EXPLICIT_SELF_HISTORY.search(context) and not _BLOOD.search(context):
                return _self_history(context, presence=presence, names=names)
            if not _family_context(context, topic):
                return None
        elif topic == 'family_history' or (_BLOOD.search(context) and not _SELF.search(context)):
            return None
        if fact_type == HISTORY_STATUS and presence == 'present' and names and not temporal_status(context):
            # Specific affirmative diagnoses belong to the existing diagnosis type.
            if _interest_only(context, names[0]) or (
                topic == 'goal' and not _has_clinical_assertion(context, names[0])
            ):
                return RecordStatement('goal.health', 'text', context, '本次健康关注', 'self', '本人', context)
            return RecordStatement('condition.diagnosis', 'text', names[0], names[0], 'self', '本人', context)
        return _observation(fact_type, context, presence=presence, conditions=names, relationship=relationship)

    presence = statement_presence(context, concrete=name)
    if fact_type == 'goal.health':
        if subject_kind != 'self':
            return None
        vague = not name or bool(_VAGUE_ENTITY.search(context))
        goal_value = (
            {'presence': presence, 'specificity': 'unspecified', 'description': context, 'entities': []}
            if vague
            else context
        )
        return RecordStatement(
            'goal.health', 'object' if vague else 'text', goal_value, '本次健康关注', 'self', '本人', context
        )

    family = fact_type == 'family_history.condition'
    if name and presence == 'present' and fact_type in {'condition.diagnosis', 'family_history.condition'}:
        if not _diagnosis_supported(context, name, subject_hint):
            presence = 'unknown'
    if family and _EXPLICIT_SELF_HISTORY.search(context) and not _BLOOD.search(context):
        return _self_history(context, presence=presence, names=[name] if name else [])
    if (
        family
        and not _family_context(context, topic)
        and not (stable_relationship and subject_kind == 'blood_relative' and subject_hint and subject_hint in context)
    ):
        return None
    if not family and subject_kind == 'self' and _BLOOD.search(context) and not _SELF.search(context):
        return None
    if _TREATMENT_OR_TEST_DENIAL.search(context) and not (
        name and presence == 'present' and _diagnosis_supported(context, name, subject_hint)
    ):
        return None
    if not family and (
        _interest_only(context, name)
        or (
            fact_type == 'condition.diagnosis'
            and topic == 'goal'
            and not (name and _has_clinical_assertion(context, name))
        )
    ):
        return RecordStatement('goal.health', 'text', context, '本次健康关注', 'self', '本人', context)
    if family:
        relationship = subject_hint if subject_hint and subject_hint in context else None
        if not name or presence != 'present' or not stable_relationship:
            return _observation(
                FAMILY_REPORTED,
                context,
                presence=presence,
                conditions=[name] if name else [],
                relationship=relationship,
            )
    elif fact_type == 'condition.diagnosis' and (not name or presence != 'present' or temporal_status(context)):
        if subject_kind != 'self':
            return None
        if not name and not (topic == 'medical_history' or _POSITIVE.search(context) or _VAGUE_ENTITY.search(context)):
            return None
        return _observation(HISTORY_STATUS, context, presence=presence, conditions=[name] if name else [])
    elif fact_type == 'symptom.reported' and (not name or presence != 'present'):
        if subject_kind != 'self':
            return None
        if name:
            return RecordStatement(
                'symptom.reported',
                'object',
                {'presence': presence, 'specificity': 'specific', 'description': context, 'name': name},
                name,
                'self',
                '本人',
                context,
            )
        return RecordStatement(
            'goal.health',
            'object',
            {'presence': presence, 'specificity': 'unspecified', 'description': context, 'entities': []},
            '本次健康关注',
            'self',
            '本人',
            context,
        )
    if not name:
        return None
    if family and temporal_status(context):
        # A known relative keeps the established subject path. Its text value
        # retains when the disease occurred, including any reported recovery.
        return RecordStatement(fact_type, 'text', context, name, subject_kind, subject_hint, context)
    if fact_type == 'symptom.reported' and (isinstance(value, dict) or temporal_status(context)):
        # Keep source-supported temporal/state qualifiers instead of reducing a
        # past, resolved symptom to a current affirmative text fact.
        symptom = {'name': name, 'presence': presence, 'specificity': 'specific', 'description': context}
        if state := temporal_status(context):
            symptom['status'] = state
        return RecordStatement(fact_type, 'object', symptom, name, subject_kind, subject_hint, context)
    return RecordStatement(fact_type, 'text', name, name, subject_kind, subject_hint, context)


def _negative_condition_names(text: str) -> list[str]:
    """Keep the named scope of a simple denial without inventing a disease."""
    names = []
    for match in re.finditer(
        r'(?:没有|未患(?:有|过)?|没得(?:过)?|否认|未确诊(?:为)?|未见|未发现|不存在|无)\s*([^，,。！？!?；;]{2,60})',
        text,
    ):
        name = re.sub(r'(?:病史|史)$', '', match[1].strip())
        name = re.sub(r'^(?:任何|已知的|明确的|确诊的)', '', name)
        if not name or re.search(r'治疗|检查|筛查|用药|服药|就医|疾病|病史|既往史|家族史|慢性病', name):
            continue
        if concrete_name(name, name, text) == name:
            names.append(name)
    return list(dict.fromkeys(names))
