"""Deterministic judgement for assessment rows: names, units, ranges, targets and levels.

The clinical view keeps every source row; this module only adds structured
fields (``valueNumber``, ``referenceLow`` ... ``level``, ``direction`` and
``target``) and never edits an existing field. Rules are data tables so they
can be unit-tested:

* the clinical track uses the printed reference interval, the printed status
  or a known national criterion (BMI, waist, blood pressure);
* the target track uses ``assessment_targets.json`` only when every
  applicability condition (sex, age, pregnancy, recorded conditions) holds;
* ``level`` combines both tracks: clinically meaningful deviations are
  ``focus``; a mild or clinically minor deviation, or a value inside the
  clinical interval but outside an applicable target, is ``borderline``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from decimal import InvalidOperation
from functools import cache
from importlib.resources import files
from typing import Any

from utils.log import getLogger

logger = getLogger(__name__)

LEVEL_LABELS = {'focus': '重点关注', 'borderline': '亚临床边界', 'normal': '稳健达标', 'notTested': '未检测'}
_LEVEL_RANK = {'normal': 0, 'borderline': 1, 'focus': 2}
_MILD_DEVIATION = Decimal('0.10')
_MARKED_TARGET_DEVIATION = Decimal('0.50')
_NUMBER = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)'
_SEX = {
    '男': 'male',
    '男性': 'male',
    'm': 'male',
    'male': 'male',
    '女': 'female',
    '女性': 'female',
    'f': 'female',
    'female': 'female',
}


# ---------------------------------------------------------------- tables


@cache
def target_table() -> dict[str, Any]:
    resource = files(__package__).joinpath('assessment_targets.json')
    table = json.loads(resource.read_text(encoding='utf-8'))
    for entry in table.get('targets', ()):
        if entry['indicator'] not in table['indicators']:
            raise ValueError(f'target {entry["id"]} uses an unknown indicator')
        if entry.get('low') is None and entry.get('high') is None:
            raise ValueError(f'target {entry["id"]} has no bound')
        if not entry.get('sources') or not all(
            source.get('title') and source.get('year') and (source.get('url') or source.get('doi'))
            for source in entry['sources']
        ):
            raise ValueError(f'target {entry["id"]} has no verifiable source')
    return table


def _catalog() -> dict[str, dict[str, Any]]:
    return target_table()['indicators']


# ---------------------------------------------------------------- names and units


def _text(value) -> str:
    return unicodedata.normalize('NFKC', str(value or '')).strip()


def _name_key(value: str) -> str:
    text = _text(value).casefold().replace('µ', 'μ')
    text = re.sub(r'(?<![a-z])[ry]\s*-\s*(?=谷氨酰)', 'γ', text)
    return re.sub(r'[\W_]+', '', text)


@cache
def _alias_index() -> dict[str, tuple[str, ...]]:
    index: dict[str, list[str]] = {}
    for code, entry in _catalog().items():
        for alias in (entry['label'], *entry.get('aliases', ()), *entry.get('abbreviations', ())):
            index.setdefault(_name_key(alias), []).append(code)
    return {key: tuple(dict.fromkeys(codes)) for key, codes in index.items()}


_NAME_DECORATION = re.compile(r'^[*＊★☆△▲●◆#]+|[↑↓]+$')
_PARENTHESES = re.compile(r'[（(]([^（）()]*)[）)]')
_QUALIFIER_SUFFIX = re.compile(r'(?:测定|检测|定量|含量|浓度|水平|值|计算值|及.*(?:分级|判定))$')
_SPECIMEN_PREFIX = re.compile(r'^(?:血清|血浆|全血|静脉血|末梢血|血液|血)')
_LATIN_TOKEN = re.compile(r'(?<![A-Za-z0-9])[A-Za-zγ][A-Za-z0-9γ\-/+()]*[A-Za-z0-9)]?(?![A-Za-z0-9])')


def _name_forms(name: str) -> list[str]:
    """Whole name, its base without bracketed abbreviations, each abbreviation and stripped variants."""
    text = _NAME_DECORATION.sub('', _text(name))
    forms = [text]
    bracketed = _PARENTHESES.findall(text)
    base = _PARENTHESES.sub(' ', text).strip()
    forms.append(base)
    forms.extend(bracketed)
    for token in _LATIN_TOKEN.findall(base):
        if len(token) >= 2:
            forms.append(token)
            remainder = base.replace(token, ' ').strip()
            if remainder:
                forms.append(remainder)
    expanded = []
    for form in forms:
        expanded.append(form)
        stripped = _QUALIFIER_SUFFIX.sub('', form.strip())
        expanded.append(stripped)
        expanded.append(_SPECIMEN_PREFIX.sub('', stripped))
    return [form for form in dict.fromkeys(item.strip() for item in expanded) if form]


def indicator_for(name: str | None, unit: str | None = None, specimen: str | None = None) -> str | None:
    """Recognize the catalog indicator of a row written in any common hospital form."""
    if not name or specimen in {'尿', '粪便'}:
        return None
    text = _text(name)
    catalog = _catalog()
    index = _alias_index()
    candidates: list[str] = []
    for form in _name_forms(text):
        candidates.extend(index.get(_name_key(form), ()))
    for code in dict.fromkeys(candidates):
        exclude = catalog[code].get('excludeName')
        if exclude and re.search(exclude, text, re.I):
            continue
        return code
    return None


def normalize_unit(unit: str | None) -> str:
    text = _text(unit).casefold().replace('µ', 'μ').replace('micro', 'μ').replace('²', '2').replace('³', '3')
    text = text.replace('^', '')
    text = re.sub(r'\s+', '', text)
    text = re.sub(r'^u(?=(?:iu|u|mol|g|l|m)/)', 'μ', text)
    return re.sub(r'(?<=/)ul$', 'μl', text)


def _factor(code: str, unit: str | None):
    conversions = _catalog()[code].get('conversions', {})
    return conversions.get(normalize_unit(unit))


def to_canonical(code: str, value: Decimal, unit: str | None) -> Decimal | None:
    factor = _factor(code, unit)
    if factor is None:
        return None
    if factor == 'ifcc':
        return value * Decimal('0.09148') + Decimal('2.152')
    return value * Decimal(str(factor))


def from_canonical(code: str, value: Decimal | None, unit: str | None) -> Decimal | None:
    if value is None:
        return None
    factor = _factor(code, unit)
    if factor is None:
        return None
    if factor == 'ifcc':
        return (value - Decimal('2.152')) / Decimal('0.09148')
    return value / Decimal(str(factor))


# ---------------------------------------------------------------- values and references


def parse_value(value) -> tuple[Decimal | None, str | None]:
    """A single printed number, optionally censored (``<10``); ranges and pairs are not numbers."""
    text = re.sub(r'[↑↓↗↘]+|\s*\((?:H|L|高|低)\)$', '', _text(value)).strip()
    match = re.fullmatch(rf'(<=|>=|≤|≥|<|>)?\s*({_NUMBER})\s*((?:[A-Za-zμµ%][A-Za-zμµ%/·^0-9²³.]*)?)', text)
    if not match:
        return None, None
    try:
        number = Decimal(match[2])
    except InvalidOperation:
        return None, None
    if not number.is_finite():
        return None, None
    qualifier = {'≤': '<=', '≥': '>='}.get(match[1], match[1])
    return number, qualifier


@dataclass(frozen=True)
class Reference:
    kind: str  # range / upper / lower / qualitative
    low: Decimal | None = None
    high: Decimal | None = None
    low_inclusive: bool = True
    high_inclusive: bool = True


_NEGATIVE_REFERENCE = re.compile(r'(?:阴性|未检出|未查见|未见|无|negative|neg|[-−])(?:\s*\([-−]\))?', re.I)
_SEX_LABEL = re.compile(r'(男|女)(?:性)?\s*[:：]?')


def _sex_scoped(text: str, sex: str | None) -> str | None:
    """Pick the subject's part of a reference printed separately for men and women."""
    labels = list(_SEX_LABEL.finditer(text))
    if not labels:
        return text
    parts = {}
    for position, label in enumerate(labels):
        end = labels[position + 1].start() if position + 1 < len(labels) else len(text)
        parts[_SEX[label[1]]] = text[label.end() : end].strip(' ;；,，/')
    return parts.get(sex) if sex else (next(iter(parts.values())) if len(parts) == 1 else None)


def parse_reference(text, sex: str | None = None) -> Reference | None:
    raw = _text(text)
    if not raw or raw in {'--', '—', '/'}:
        return None
    if _NEGATIVE_REFERENCE.fullmatch(raw):
        return Reference('qualitative')
    scoped = _sex_scoped(raw, sex)
    if scoped is None:
        return None
    scoped = re.sub(r'^[^\d<>≤≥+-]*?[:：]\s*', '', scoped)
    scoped = scoped.replace('≤', '<=').replace('≥', '>=').replace('＜', '<').replace('＞', '>')
    interval = re.match(rf'\s*({_NUMBER})\s*(?:--|-|~|～|—|–|至|－)\s*({_NUMBER})', scoped)
    if interval:
        low, high = Decimal(interval[1]), Decimal(interval[2])
        if low <= high:
            return Reference('range', low, high)
        return None
    bound = re.match(rf'\s*(<=|>=|<|>)\s*({_NUMBER})', scoped)
    if bound:
        limit = Decimal(bound[2])
        if bound[1] in {'<', '<='}:
            return Reference('upper', high=limit, high_inclusive=bound[1] == '<=')
        return Reference('lower', low=limit, low_inclusive=bound[1] == '>=')
    return None


def compare(number: Decimal, qualifier: str | None, reference: Reference) -> str | None:
    """'within', 'up' or 'down' against a numeric reference; None when a censored value is ambiguous."""
    low, high = reference.low, reference.high
    if qualifier in {'<', '<='}:
        if low is not None and (number < low or number == low and qualifier == '<'):
            return 'down'
        if high is not None and number <= high and (low is None or low <= 0):
            return 'within'
        return None
    if qualifier in {'>', '>='}:
        if high is not None and (number > high or number == high and qualifier == '>'):
            return 'up'
        if low is not None and number >= low and high is None:
            return 'within'
        return None
    if low is not None and (number < low or number == low and not reference.low_inclusive):
        return 'down'
    if high is not None and (number > high or number == high and not reference.high_inclusive):
        return 'up'
    return 'within'


def _relative_excess(number: Decimal, direction: str, reference: Reference) -> Decimal:
    bound = reference.high if direction == 'up' else reference.low
    if bound is None:
        return Decimal(1)
    scale = (
        abs(bound)
        or (
            abs(reference.high - reference.low)
            if reference.high is not None and reference.low is not None
            else Decimal(1)
        )
        or Decimal(1)
    )
    return abs(number - bound) / scale


# ---------------------------------------------------------------- subject context


@dataclass(frozen=True)
class SubjectContext:
    sex: str | None = None
    age: int | None = None
    pregnancy: str = 'unknown'  # pregnant / notPregnant / unknown
    conditions: frozenset[str] = field(default_factory=frozenset)

    @property
    def adult(self) -> bool:
        return self.age is not None and self.age >= 18

    @property
    def reproductive_female(self) -> bool:
        return self.sex == 'female' and (self.age is None or 12 <= self.age <= 55)


_CONDITION_PATTERNS = {
    'diabetes': r'(?<!前期)(?<!妊娠)糖尿病(?!前期|风险|筛查|家族)|降糖药|二甲双胍|胰岛素(?:治疗|注射)|格列|DPP-?4',
    'ascvd': r'冠心病|冠状动脉粥样硬化性心脏病|心肌梗死|心梗|冠脉支架|支架植入|搭桥|PCI|脑梗|缺血性脑卒中|脑卒中|中风|'
    r'外周动脉疾病',
    'hypertension': r'高血压(?!家族)|降压药|氨氯地平|硝苯地平|缬沙坦|厄贝沙坦|氯沙坦|贝那普利',
    'chronicKidneyDisease': r'慢性肾(?:脏)?病|肾功能不全|肾衰|透析|\bCKD\b',
    'thyroidDisease': r'甲亢|甲减|甲状腺功能(?:亢进|减退)|桥本|甲状腺炎|优甲乐|左甲状腺素|甲巯咪唑|丙硫氧嘧啶',
    'thyroidSurgery': r'甲状腺(?:全切|次全切|切除|部分切除|术后|手术)|甲状腺癌术后',
    'gout': r'痛风',
    'liverDisease': r'慢性(?:乙型|丙型)?肝炎|肝硬化|乙肝(?:病毒)?携带|乙肝(?:患者|病史)|自身免疫性肝病',
    'lipidLoweringTherapy': r'他汀|依折麦布|降脂药|调脂药|PCSK9',
}
_NEGATION = re.compile(r'(?:否认|无|没有|未(?:患|曾|诊断|发现)?|不详|不清楚|不知道|否)[^，,；;。]{0,8}$')
_FAMILY = re.compile(r'家族|父亲|母亲|父母|兄弟|姐妹|子女|祖父|祖母|外公|外婆|亲属|血亲|家人|家里人|爷爷|奶奶|遗传')
_QUESTION = re.compile(r'[?？]|是否|哪些|有无|有没有|什么')
_PREGNANT = re.compile(
    r'妊娠(?!糖尿病史|期糖尿病史)|怀孕|孕\s*\d+\s*(?:\+\s*\d+\s*)?周|孕期|早孕|孕妇|孕[一二三四五六七八九十]+周'
)
_NOT_PREGNANT = re.compile(r'未孕|非孕|否认(?:妊娠|怀孕)|未怀孕|没有怀孕|绝经')
_HCG = re.compile(r'人绒毛膜促性腺激素|绒毛膜促性腺|(?<![A-Za-z])(?:[βb][-\s]?)?HCG(?![A-Za-z])', re.I)


def _clauses(text: str) -> list[str]:
    return [part.strip() for part in re.split(r'[，,；;。！!？?\n]+', text) if part.strip()]


def _affirmed(pattern: str, text: str) -> bool:
    for clause in _clauses(text):
        for match in re.finditer(pattern, clause, re.I):
            if _NEGATION.search(clause[: match.start()]) or _FAMILY.search(clause):
                continue
            if re.search(r'^(?:无|否|没有|不)$', clause[match.end() :].strip(' ：:是否有')):
                continue
            return True
    return False


def _history_texts(document) -> list[str]:
    """The subject's own recorded history: answers only, never a question's option list or a relative's disease."""
    texts = []
    for fact in getattr(document, 'facts', ()) or ():
        kind = getattr(fact, 'kind', None)
        name = str(getattr(fact, 'name', '') or '')
        value = str(getattr(fact, 'value', '') or '')
        if kind == 'self_reported' or kind == 'context' and re.search(r'病史|用药|手术|既往', name):
            question, separator, answer = value.removeprefix('问题：').partition('；回答：')
            if not separator:
                question, answer = (name, value) if _QUESTION.search(name) else ('', f'{name}：{value}')
            if _FAMILY.search(question) or _FAMILY.search(name):
                continue
            texts.append(answer)
        elif kind == 'clinical' and re.search(r'既往|病史|现病史|手术史|用药|临床诊断|就诊诊断', name):
            if not _FAMILY.search(name):
                texts.append(f'{name}：{value}')
    background = getattr(document, 'background', None) or {}
    for record in background.get('sourceReported', ()) if isinstance(background, dict) else ():
        if isinstance(record, dict) and not _FAMILY.search(str(record.get('name') or '')):
            texts.append(f'{record.get("name") or ""}：{record.get("value") or ""}')
    return texts


def _hcg_state(rows) -> str | None:
    states = set()
    for row in rows:
        if not _HCG.search(str(row.get('name') or '')):
            continue
        value = _text(row.get('value'))
        if re.fullmatch(r'(?:弱|强)?阳性.*|\+{1,4}|positive', value, re.I):
            states.add('pregnant')
            continue
        if _NEGATIVE_REFERENCE.fullmatch(value):
            states.add('notPregnant')
            continue
        number, qualifier = parse_value(value)
        reference = parse_reference(row.get('referenceRange'), 'female')
        if number is not None and reference is not None and reference.kind != 'qualitative':
            direction = compare(number, qualifier, reference)
            if direction == 'up':
                states.add('pregnant')
            elif direction is not None:
                states.add('notPregnant')
    if 'pregnant' in states:
        return 'pregnant'
    return 'notPregnant' if states else None


def subject_context(subject, document=None, rows=()) -> SubjectContext:
    """Sex, age, pregnancy and recorded conditions; uncertain evidence never relaxes a condition."""
    sex = _SEX.get(_text(getattr(subject, 'sex', None)).casefold())
    age = getattr(subject, 'age', None)
    age = int(age) if isinstance(age, int) or isinstance(age, str) and age.isdigit() else None
    texts = _history_texts(document) if document is not None else []
    joined = '\n'.join(texts)
    conditions = frozenset(code for code, pattern in _CONDITION_PATTERNS.items() if _affirmed(pattern, joined))
    pregnancy = 'notPregnant'
    if sex == 'female' and (age is None or 12 <= age <= 55):
        pregnancy = 'unknown'
        hcg = _hcg_state(rows)
        if hcg == 'pregnant' or _affirmed(_PREGNANT.pattern, joined):
            pregnancy = 'pregnant'
        elif hcg == 'notPregnant' or _NOT_PREGNANT.search(joined):
            pregnancy = 'notPregnant'
    elif sex is None:
        pregnancy = 'unknown'
    return SubjectContext(sex=sex, age=age, pregnancy=pregnancy, conditions=conditions)


# ---------------------------------------------------------------- targets


def target_applicability(entry: dict, context: SubjectContext) -> str | None:
    """'applicable', 'conditional' (pregnancy not documented) or None."""
    conditions = entry.get('conditions') or {}
    if conditions.get('sex') and conditions['sex'] != context.sex:
        return None
    if conditions.get('minAge') is not None and (context.age is None or context.age < conditions['minAge']):
        return None
    if conditions.get('maxAge') is not None and (context.age is None or context.age > conditions['maxAge']):
        return None
    if set(conditions.get('excludeConditions') or ()) & context.conditions:
        return None
    if not set(conditions.get('requiresConditions') or ()) <= context.conditions:
        return None
    if conditions.get('nonPregnant'):
        if context.pregnancy == 'pregnant':
            return None
        if context.pregnancy == 'unknown' and context.reproductive_female:
            return 'conditional'
    return 'applicable'


def applicable_target(code: str, context: SubjectContext) -> tuple[dict, str] | None:
    """The most specific applicable entry: required conditions, then sex, then table order."""
    ranked = []
    for position, entry in enumerate(target_table().get('targets', ())):
        if entry['indicator'] != code:
            continue
        applicability = target_applicability(entry, context)
        if applicability is None:
            continue
        conditions = entry.get('conditions') or {}
        ranked.append(
            (
                -len(conditions.get('requiresConditions') or ()),
                not conditions.get('sex'),
                position,
                entry,
                applicability,
            )
        )
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[:3])
    return ranked[0][3], ranked[0][4]


def _display_number(value: Decimal | None):
    if value is None:
        return None
    rounded = value.quantize(Decimal('0.01')) if abs(value) < 100 else value.quantize(Decimal('1'))
    normalized = rounded.normalize()
    return int(normalized) if normalized == normalized.to_integral_value() else float(normalized)


def _exact_number(value: Decimal | None):
    """The parsed source number without rounding (valueNumber, reference bounds)."""
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() and abs(value) < 10**15 else float(value)


def _target_view(entry: dict, applicability: str, code: str, unit: str | None) -> dict:
    canonical = _catalog()[code]['unit']
    same_unit = normalize_unit(unit) == normalize_unit(canonical) or _factor(code, unit) == 1
    low = Decimal(str(entry['low'])) if entry.get('low') is not None else None
    high = Decimal(str(entry['high'])) if entry.get('high') is not None else None
    if not same_unit:
        low, high = from_canonical(code, low, unit), from_canonical(code, high, unit)
    shown_unit = canonical if same_unit else _text(unit)
    if low is not None and high is not None:
        if entry.get('lowInclusive') is False or entry.get('highInclusive') is False:
            lower = f'{"≥" if entry.get("lowInclusive") else ">"}{_display_number(low)}'
            text = f'{lower} 且 {"≤" if entry.get("highInclusive") else "<"}{_display_number(high)}'
        else:
            text = f'{_display_number(low)}–{_display_number(high)}'
    elif high is not None:
        text = f'{"≤" if entry.get("highInclusive") else "<"}{_display_number(high)}'
    else:
        text = f'{"≥" if entry.get("lowInclusive") else ">"}{_display_number(low)}'
    text = f'{text} {shown_unit}'.strip()
    if entry.get('conditions', {}).get('sex'):
        text = ('男性' if entry['conditions']['sex'] == 'male' else '女性') + ' ' + text
    if applicability == 'conditional':
        text += '（适用于非妊娠状态）'
    return {
        'id': entry['id'],
        'label': entry['label'],
        'low': _display_number(low),
        'high': _display_number(high),
        'text': text,
        'basis': entry.get('basis'),
        'riskTier': entry.get('riskTier'),
        'evidenceLevel': entry.get('evidenceLevel'),
        'applicability': applicability,
        'sources': [
            {key: source.get(key) for key in ('title', 'year', 'doi', 'pmid', 'journal', 'url')}
            for source in entry.get('sources', ())
        ],
        'evidenceRefs': [],
    }


def _target_direction(entry: dict, value: Decimal) -> str:
    low = Decimal(str(entry['low'])) if entry.get('low') is not None else None
    high = Decimal(str(entry['high'])) if entry.get('high') is not None else None
    reference = Reference(
        'range' if low is not None and high is not None else 'upper' if high is not None else 'lower',
        low,
        high,
        bool(entry.get('lowInclusive', True)) if low is not None else True,
        bool(entry.get('highInclusive', True)) if high is not None else True,
    )
    return compare(value, None, reference) or 'within'


# ---------------------------------------------------------------- clinical significance

# A deviation in these directions has no recognised clinical meaning on its
# own; it can be subclinical (borderline) but never "focus".
_MINOR_DIRECTION = {
    'down': re.compile(
        r'转氨酶|氨基转移酶|(?<![A-Za-z])(?:AST|ALT|GGT|γ-?GT)(?![A-Za-z])|谷氨酰|胆红素|胆汁酸|C\s*-?反应蛋白|CRP|'
        r'甘油三酯|(?<![A-Za-z])TG(?![A-Za-z])|总胆固醇|低密度|(?<![A-Za-z])LDL|载脂蛋白\s*B|ApoB|脂蛋白\s*\(?a|'
        r'同型半胱氨酸|尿素|肌酐|尿酸|红细胞分布宽度|RDW|血小板分布宽度|PDW|平均血小板体积|MPV|大型血小板|血小板压积|'
        r'嗜酸|嗜碱|单核|肌酸激酶|(?<![A-Za-z])CK(?![A-Za-z])|乳酸脱氢酶|LDH|淀粉酶|脂肪酶|类风湿|抗链|ASO|IgE|血沉|'
        r'红细胞沉降率|ESR|胱抑素|β2-?微球蛋白|视黄醇结合蛋白|铁蛋白(?=.*男)|糖类抗原|癌胚抗原|甲胎蛋白|孕酮|泌乳素|'
        r'碱性磷酸酶|胆碱酯酶|腺苷脱氨酶|前白蛋白|心率|基础代谢',
        re.I,
    ),
    'up': re.compile(
        r'高密度脂蛋白|(?<![A-Za-z])HDL|载脂蛋白\s*A|ApoA|淋巴细胞百分比|单核细胞百分比|胆碱酯酶|孕酮|'
        r'平均红细胞血红蛋白浓度|MCHC|骨密度|T[- ]?值|T[- ]?Score|Z[- ]?Score|SOS|BQI',
        re.I,
    ),
}
# Any deviation of these is clinically meaningful whatever its size.
_STRICT = (
    (
        None,
        re.compile(
            r'^(?:\*?\s*)?(?:血清|血浆|血)?(?:钾|钠)(?:离子)?(?:\s*[（(].*)?$|血钾|血钠|血钙|糖化血红蛋白|HbA1c|'
            r'肌钙蛋白|钠尿肽|BNP|甲胎蛋白|癌胚抗原|糖类抗原|糖链抗原|前列腺特异|CA\s*\d|PSA|CYFRA|NSE|SCC',
            re.I,
        ),
    ),
    ('up', re.compile(r'葡萄糖|血糖|肌酐(?!清除)|尿蛋白', re.I)),
    ('down', re.compile(r'葡萄糖|血糖|肾小球滤过|eGFR|GFR', re.I)),
)
_MINOR_FINDING = re.compile(
    r'囊肿|钙化(?:灶|斑|点)?|牙石|软垢|龋|色素|退行性变|退变|骨质增生|轻度[返反]流|少量[返反]流|微量[返反]流|生理性|'
    r'窦性心动过缓|窦性心律不齐|窦性心动过速|不完全性右束支|电轴[左右]偏|早复极|舒张功能(?:减退|减低)|结节|息肉|增生|'
    r'屈光不正|近视|远视|散光|老视|副乳|脂肪瘤|血管瘤|胆囊壁(?:毛糙|增厚)|曲度变直|椎间隙变窄|骨刺|陈旧|纤维灶|条索',
)
_LARGE_LESION = re.compile(r'(?<![\d.])(?:[1-9]\d+(?:\.\d+)?\s*mm|[1-9]\d*(?:\.\d+)?\s*cm)', re.I)
_GRADE = re.compile(r'(C?-?TI|BI|Lung)\s*-?\s*RADS\s*[:：]?\s*(\d)([abc])?\s*类?', re.I)


def _minor_direction(name: str, direction: str | None) -> bool:
    pattern = _MINOR_DIRECTION.get(direction or '')
    return bool(pattern and pattern.search(name))


def _strict(name: str, direction: str | None) -> bool:
    return any((scope is None or scope == direction) and pattern.search(name) for scope, pattern in _STRICT)


def finding_level(text: str) -> str:
    """Level of a source abnormal finding without numeric interval: graded first, then benign follow-up patterns."""
    grades = [(system.casefold(), int(grade)) for system, grade, _ in _GRADE.findall(text)]
    if grades:
        levels = []
        for system, grade in grades:
            if 'bi' in system:
                levels.append('normal' if grade == 1 else 'focus' if grade == 0 or grade >= 4 else 'borderline')
            elif 'lung' in system:
                levels.append('focus' if grade >= 3 else 'borderline')
            else:
                levels.append('focus' if grade >= 4 else 'normal' if grade == 1 else 'borderline')
        return max(levels, key=_LEVEL_RANK.__getitem__)
    if (
        _MINOR_FINDING.search(text)
        and not re.search(r'息肉', text)
        or (re.search(r'息肉', text) and not _LARGE_LESION.search(text))
    ):
        if not re.search(r'实性.*(?:分叶|毛刺|微钙化)|占位|肿块|恶性|癌|转移|梗阻|积液(?!.*少量)', text):
            return 'borderline'
    return 'focus'


# ---------------------------------------------------------------- national criteria (clinical track)


def _bmi_level(value: Decimal) -> tuple[str, str | None]:
    if value >= 28:
        return 'focus', 'up'
    if value >= 24:
        return 'borderline', 'up'
    if value < Decimal('18.5'):
        return 'borderline', 'down'
    return 'normal', None


def _waist_level(value: Decimal, sex: str) -> tuple[str, str | None]:
    obese, pre = (90, 85) if sex == 'male' else (85, 80)
    if value >= obese:
        return 'focus', 'up'
    if value >= pre:
        return 'borderline', 'up'
    return 'normal', None


def _pressure_level(systolic: Decimal | None, diastolic: Decimal | None) -> tuple[str, str | None]:
    if systolic is not None and systolic >= 140 or diastolic is not None and diastolic >= 90:
        return 'focus', 'up'
    if systolic is not None and systolic >= 120 or diastolic is not None and diastolic >= 80:
        return 'borderline', 'up'
    if systolic is not None and systolic < 90 or diastolic is not None and diastolic < 60:
        return 'borderline', 'down'
    return 'normal', None


def _filtration_level(value: Decimal) -> tuple[str, str | None]:
    # KDIGO GFR categories: G1 >=90, G2 60-89, G3a-G5 <60 mL/min/1.73 m2.
    if value < 60:
        return 'focus', 'down'
    if value < 90:
        return 'borderline', 'down'
    return 'normal', None


def _heart_rate_level(value: Decimal) -> tuple[str, str | None]:
    if value > 100:
        return 'borderline', 'up'
    if value < 60:
        return 'borderline', 'down'
    return 'normal', None


_BODY_SHAPE = re.compile(r'体重指数|体质指数|(?<![A-Za-z])BMI(?![A-Za-z])|腰围|腰臀比|WHR|体脂|内脏脂肪', re.I)
_PRESSURE = re.compile(r'血压|收缩压|舒张压', re.I)
_HEART_RATE = re.compile(r'心率(?!变异)|^脉搏', re.I)


def _known_rule(row: dict, number: Decimal | None, context: SubjectContext) -> tuple[str, str | None] | None:
    name = _text(row.get('name'))
    unit = row.get('unit')
    if _BODY_SHAPE.search(name) and (context.pregnancy == 'pregnant' or not context.adult):
        return None
    if _PRESSURE.search(name) and not re.search(r'踝|下肢|脚|肺动脉', name):
        pair = re.fullmatch(rf'\s*({_NUMBER})\s*/\s*({_NUMBER})\s*(?:mmHg)?\s*', _text(row.get('value')), re.I)
        if pair:
            return _pressure_level(Decimal(pair[1]), Decimal(pair[2]))
        if number is not None and normalize_unit(unit) in {'mmhg', ''}:
            if '收缩' in name:
                return _pressure_level(number, None)
            if '舒张' in name:
                return _pressure_level(None, number)
        return None
    if number is None:
        return None
    code = indicator_for(name, unit)
    if code == 'bmi' and normalize_unit(unit) in {'kg/m2', ''}:
        return _bmi_level(number)
    if code == 'waist' and context.sex and normalize_unit(unit) == 'cm':
        return _waist_level(number, context.sex)
    if _HEART_RATE.search(name) and normalize_unit(unit) in {'次/分', 'bpm', '次/min', '/min', ''}:
        return _heart_rate_level(number)
    if re.search(r'肾小球滤过率|(?<![A-Za-z])e?GFR(?![A-Za-z])', name, re.I) and not re.search(r'尿|清除', name):
        return _filtration_level(number)
    return None


# ---------------------------------------------------------------- row judgement

_UP_WORDS = re.compile(r'偏高|升高|增高|高于|过高|超标|过量|肥胖|超重|[↑]|阳性', re.I)
_DOWN_WORDS = re.compile(r'偏低|降低|减低|低于|过低|不足|缺乏|[↓]|偏瘦|体重过低', re.I)


def _status_direction(status: str) -> str | None:
    if _UP_WORDS.search(status) and not _DOWN_WORDS.search(status):
        return 'up'
    if _DOWN_WORDS.search(status) and not _UP_WORDS.search(status):
        return 'down'
    return None


def _state_of(status) -> str | None:
    from .evaluation_builder import _status_state

    if status in {'参考范围内', '已检项目未见异常', '未见肿瘤细胞', '所见正常', '原文正常所见'}:
        return 'normal'
    if isinstance(status, str) and status.startswith(('设备判断：', '所见：', '原文：')):
        detail = status.split('：', 1)[1]
        if re.search(r'风险(?:较|很)?低|低风险|(?:大致|基本|总体)?正常|未见(?:明显)?异常', detail):
            return 'normal'
        if re.search(r'风险(?:较|很)?高|高风险|中等风险|风险中等|异常|减退|下降|减慢|升高|降低', detail):
            return 'abnormal'
        return None
    if status in {'有异常所见', '偏硬', '超重', '肥胖', '中心型肥胖', '腹型肥胖', '低体重', '过量'}:
        return 'abnormal'
    return _status_state(status)


def _qualitative_polarity(value) -> str | None:
    text = _text(value).casefold()
    if re.fullmatch(r'[-−]|阴性(?:\s*\(.*\))?|negative|neg|未检出|未查见', text):
        return 'negative'
    if re.fullmatch(r'(?:[1-4]\s*)?\+{1,4}|(?:弱|强)?阳性(?:\s*\(.*\))?|positive|pos', text):
        return 'positive'
    return None


@dataclass
class Judgement:
    value_number: Decimal | None = None
    value_qualifier: str | None = None
    reference: Reference | None = None
    clinical: str | None = None  # within / outside / None
    clinical_level: str | None = None
    direction: str | None = None
    target: dict | None = None
    target_direction: str | None = None
    level: str | None = None
    indicator: str | None = None
    unmatched: list[str] = field(default_factory=list)


def judge(row: dict, context: SubjectContext) -> Judgement:
    """Clinical track, target track and their combination for one row."""
    result = Judgement()
    name = _text(row.get('name'))
    value = row.get('value')
    status = row.get('templateStatus') or row.get('status')
    number, qualifier = parse_value(value)
    result.value_number, result.value_qualifier = number, qualifier
    reference = parse_reference(row.get('referenceRange'), context.sex)
    result.reference = reference
    if row.get('sourceKind') == 'recordedHistory':
        return result

    # Clinical track: national criteria first (BMI, waist, blood pressure, eGFR), then the printed interval.
    clinical_direction = None
    known = _known_rule(row, number, context)
    compared = number
    if number is None and (span := re.fullmatch(rf'\s*({_NUMBER})\s*[-~～]\s*({_NUMBER})\s*', _text(value))):
        compared = Decimal(span[2])  # a counted range such as 0-3 /HP is judged by its upper end
    if known is not None:
        level, clinical_direction = known
        result.clinical = 'within' if level == 'normal' else 'outside'
        result.clinical_level = None if level == 'normal' else level
    elif compared is not None and reference is not None and reference.kind != 'qualitative':
        number_for_reference = compared
        relation = compare(number_for_reference, qualifier, reference)
        if relation == 'within':
            result.clinical = 'within'
        elif relation in {'up', 'down'}:
            result.clinical, clinical_direction = 'outside', relation
            excess = _relative_excess(number_for_reference, relation, reference)
            if _strict(name, relation):
                result.clinical_level = 'focus'
            elif _minor_direction(name, relation) or excess <= _MILD_DEVIATION:
                result.clinical_level = 'borderline'
            else:
                result.clinical_level = 'focus'
    elif reference is not None and reference.kind == 'qualitative' or _qualitative_polarity(value):
        polarity = _qualitative_polarity(value)
        if polarity == 'positive':
            result.clinical, result.clinical_level, clinical_direction = 'outside', 'focus', 'up'
        elif polarity == 'negative':
            result.clinical = 'within'
    if result.clinical is None and _GRADE.search(f'{status or ""} {_text(value)}'):
        result.clinical_level = finding_level(f'{status or ""} {_text(value)}')
        result.clinical = 'within' if result.clinical_level == 'normal' else 'outside'
        result.clinical_level = None if result.clinical == 'within' else result.clinical_level
    if result.clinical is None and isinstance(status, str) and status.strip():
        state = _state_of(status)
        if state == 'normal':
            result.clinical = 'within'
        elif state == 'abnormal':
            result.clinical = 'outside'
            clinical_direction = _status_direction(status)
            finding = row.get('_sourceRole') in {'exam_finding', 'physical_finding'} or row.get('_verifiedFinding')
            if finding or number is None:
                text = ' '.join(filter(None, (name, _text(value), status)))
                result.clinical_level = finding_level(text) if finding or status == '有异常所见' else 'focus'
            elif _minor_direction(name, clinical_direction):
                result.clinical_level = 'borderline'
            else:
                result.clinical_level = 'focus'
    elif result.clinical is None and row.get('_verifiedFinding'):
        result.clinical, result.clinical_level = 'outside', finding_level(f'{name} {_text(value)}')

    # Target track.
    code = indicator_for(name, row.get('unit'), row.get('specimen'))
    result.indicator = code
    scalar = row.get('_sourceRole') in {None, 'direct_measurement', 'derived_measurement'}
    if code and number is not None and qualifier is None and scalar and '若确为' not in name:
        chosen = applicable_target(code, context)
        canonical = to_canonical(code, number, row.get('unit'))
        if chosen is not None and canonical is None:
            result.unmatched.append(f'unit:{code}:{row.get("unit")}')
        elif chosen is not None:
            entry, applicability = chosen
            result.target = _target_view(entry, applicability, code, row.get('unit'))
            result.target_direction = _target_direction(entry, canonical)
            result.target['state'] = 'within' if result.target_direction == 'within' else 'outside'
            result.target['direction'] = None if result.target_direction == 'within' else result.target_direction
            if applicability == 'conditional':
                result.target['affectsLevel'] = False
            else:
                result.target['affectsLevel'] = True

    # Combination.
    target_outside = bool(result.target and result.target['state'] == 'outside' and result.target['affectsLevel'])
    if result.clinical == 'outside':
        result.level = result.clinical_level or 'focus'
        result.direction = clinical_direction
    elif result.clinical == 'within':
        result.level = 'borderline' if target_outside else 'normal'
        result.direction = result.target['direction'] if target_outside else None
    elif target_outside:
        entry = applicable_target(code, context)[0]
        bound = entry.get('high') if result.target_direction == 'up' else entry.get('low')
        canonical = to_canonical(code, number, row.get('unit'))
        marked = bound not in (None, 0) and abs(canonical - Decimal(str(bound))) / abs(Decimal(str(bound))) >= (
            _MARKED_TARGET_DEVIATION
        )
        result.level = 'focus' if marked else 'borderline'
        result.direction = result.target_direction
    elif result.target and result.target['state'] == 'within' and result.target['affectsLevel']:
        result.level = 'normal'
    return result


def structured_fields(row: dict, context: SubjectContext) -> dict:
    """The additive public fields of one row (spec 2.7); all may be null."""
    verdict = judge(row, context)
    reference = verdict.reference
    for reason in verdict.unmatched:
        logger.info('评估目标表单位无法换算 %s（%s）', reason, row.get('name'))
    if verdict.indicator is None and verdict.value_number is not None and row.get('sourceKind') == 'clinical':
        logger.debug('评估目标表未识别的指标写法：%s（%s）', row.get('name'), row.get('unit'))
    return {
        'valueNumber': _exact_number(verdict.value_number),
        'valueQualifier': verdict.value_qualifier,
        'referenceLow': _exact_number(reference.low) if reference else None,
        'referenceHigh': _exact_number(reference.high) if reference else None,
        'referenceKind': reference.kind if reference else None,
        'level': verdict.level,
        'direction': verdict.direction,
        'target': {key: value for key, value in verdict.target.items() if key != 'affectsLevel'}
        if verdict.target
        else None,
    }


def annotate_rows(rows, context: SubjectContext) -> None:
    """Add (or refresh) the structured fields in place; source fields are never changed."""
    for row in rows or ():
        if not isinstance(row, dict) or 'name' not in row or 'value' not in row:
            continue
        row.update(structured_fields(row, context))


def worst_level(levels) -> str | None:
    known = [level for level in levels if level in _LEVEL_RANK]
    return max(known, key=_LEVEL_RANK.__getitem__) if known else None


def summarize_levels(rows, limit: int = 6) -> dict:
    """System or panel level with its most important anchors (spec 2.7)."""
    rows = [row for row in rows or () if isinstance(row, dict)]
    level = worst_level(row.get('level') for row in rows)
    ranked = sorted(
        (row for row in rows if row.get('level') in {'focus', 'borderline'}),
        key=lambda row: -_LEVEL_RANK[row['level']],
    )
    anchors, seen = [], set()
    for row in ranked:
        key = (row.get('name'), str(row.get('value')))
        if key in seen:
            continue
        seen.add(key)
        anchors.append(
            {
                'name': row.get('name'),
                'value': row.get('value'),
                'unit': row.get('unit'),
                'direction': row.get('direction'),
                'level': row.get('level'),
            }
        )
    return {'level': level, 'anchors': anchors[:limit], 'moreCount': max(0, len(anchors) - limit)}


def target_sources(rows) -> list[dict]:
    """Unique literature behind the displayed targets, for the report's reference list."""
    result, seen = [], set()
    for row in rows or ():
        for source in (row.get('target') or {}).get('sources') or ():
            key = source.get('doi') or source.get('pmid') or source.get('url')
            if key and key not in seen:
                seen.add(key)
                result.append(source)
    return result


__all__ = [
    'LEVEL_LABELS',
    'Reference',
    'SubjectContext',
    'annotate_rows',
    'applicable_target',
    'compare',
    'finding_level',
    'indicator_for',
    'judge',
    'normalize_unit',
    'parse_reference',
    'parse_value',
    'structured_fields',
    'subject_context',
    'summarize_levels',
    'target_applicability',
    'target_sources',
    'target_table',
    'to_canonical',
    'worst_level',
]
