"""Deterministic clinical indices computed from recorded check-up values.

Report authors are language models; arithmetic on laboratory values is done
here once, by formula, and added as ordinary source facts so that every
chapter cites the same number. Each index requires unambiguous inputs with
the expected units. The source field carries the formula for a calculated
value or the criterion applied to an unchanged measured value.
"""

from __future__ import annotations

import math
import re
import unicodedata
from decimal import Decimal
from decimal import InvalidOperation

from utils.log import getLogger

from .schemas import ReportSourceFact
from .schemas import stable_id

logger = getLogger(__name__)

DERIVED_SOURCE = '根据体检记录计算'
_CLASSIFICATION_SOURCE = '根据体检实测值分级'
_OPERANDS_LABEL = '；所用数值：'


def is_derived_index(source):
    return str(source or '').startswith((DERIVED_SOURCE, _CLASSIFICATION_SOURCE))


def derived_index_context(fact):
    """Expose the calculation or classification and its own operands for review."""
    source = fact.get('source') if isinstance(fact, dict) else fact.source
    if not is_derived_index(source):
        return None
    prefix, _, details = source.partition('：')
    rule, _, values = details.partition(_OPERANDS_LABEL)
    if not rule or not values:
        return None
    name = fact.get('name') if isinstance(fact, dict) else fact.name
    classified = (prefix == _CLASSIFICATION_SOURCE
                  or str(name or '').startswith('腰围及中心型肥胖判定'))
    return {
        'valueKind': 'measured_classification' if classified else 'calculated',
        'criterion' if classified else 'formula': rule,
        'inputs': values.split('；'),
    }

# Laboratory inputs are recognised through the shared indicator catalog
# (aliases, bracketed abbreviations, unit conversion to the canonical unit),
# so "葡萄糖(GLU)", "血清甘油三酯 mg/dL" or "胰岛素 pmol/L" are all usable.
_CATALOG_INPUTS = {
    'fpg': 'fpg',
    'insulin': 'fastingInsulin',
    'tc': 'tc',
    'tg': 'tg',
    'hdl': 'hdlC',
    'ldl': 'ldlC',
    'ast': 'ast',
    'alt': 'alt',
}
_INPUTS = {
    'fpg': (r'空腹(?:血糖|葡萄糖)|葡萄糖|血糖|\bGLU\b|\bFPG\b|fasting (?:plasma )?glucose', {'mmol/l'}),
    'insulin': (r'胰岛素|\bFINS\b|\bINS\b|fasting insulin', {'miu/l', 'μiu/ml', 'uiu/ml', 'µiu/ml', 'μu/ml', 'mu/l'}),
    'plt': (r'血小板计数|\bPLT\b|platelet count', {'10e9/l', '10^9/l', '×10^9/l', 'x10^9/l', '10⁹/l', '109/l'}),
    'height': (r'^身高$', {'cm'}),
    'weight': (r'^体重$', {'kg'}),
    'waist': (r'^腰围', {'cm'}),
    'sbp': (r'收缩压', {'mmhg'}),
    'dbp': (r'舒张压', {'mmhg'}),
}
_EXCLUDED_NAME = re.compile(r'主检|结论|建议|比值|/|降低|增高|增大|升高|偏[高低]|体成分|目标|控制|AST/ALT', re.I)


def _norm(text) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(text or ''))).casefold()


def _number(value) -> Decimal | None:
    text = _norm(value)
    if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)', text):
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _catalog_input(name, unit, value):
    """(key, canonical value) of a laboratory input written in any catalogued form."""
    from .assessment_clinical_rules import indicator_for
    from .assessment_clinical_rules import to_canonical

    code = indicator_for(name, unit)
    for key, indicator in _CATALOG_INPUTS.items():
        if code == indicator:
            converted = to_canonical(code, value, unit)
            if converted is None:
                logger.info('衍生指标输入单位无法换算：%s %s', name, unit)
                return None
            return key, converted.quantize(Decimal('0.0001')).normalize()
    return None


def _collect(facts):
    """Return unambiguous numeric inputs; an index with a duplicated input is skipped."""
    found: dict[str, list] = {}
    for fact in facts:
        if fact.kind != 'clinical' or is_derived_index(fact.source):
            continue
        name = unicodedata.normalize('NFKC', str(fact.name or '')).strip()
        if _EXCLUDED_NAME.search(name):
            continue
        value = _number(fact.value)
        if value is None:
            continue
        if catalogued := _catalog_input(name, fact.unit, value):
            found.setdefault(catalogued[0], []).append((catalogued[1], fact))
            continue
        unit = _norm(fact.unit)
        for key, (pattern, units) in _INPUTS.items():
            if key in _CATALOG_INPUTS:
                continue
            if re.search(pattern, name, re.I) and unit in units:
                found.setdefault(key, []).append((value, fact))
                break
    result = {}
    for key, rows in found.items():
        values = {value for value, _ in rows}
        if len(values) == 1:  # the same measurement repeated in two tables is still one value
            result[key] = rows[0]
    return result


_NON_FASTING = re.compile(
    r'非空腹|未空腹|餐后|随机|[0-9一二三]\s*(?:h|小时)|糖耐量|OGTT|\bnon[‐‑– -]?fasting\b|\bpostprandial\b|\brandom\b',
    re.I,
)


def _fasting_measurement(fact, key):
    name = unicodedata.normalize('NFKC', fact.name or '')
    if _NON_FASTING.search(name):
        return False
    alias = {'fpg': 'FPG', 'insulin': 'FINS'}.get(key)
    return bool(re.search(r'空腹|\bfasting\b', name, re.I)
                or (alias and re.search(rf'\b{alias}\b', name, re.I)))


def _routine_draw(fact):
    """A check-up draw is fasting by convention unless its own label says otherwise."""
    return not _NON_FASTING.search(unicodedata.normalize('NFKC', fact.name or ''))


def _fmt(value: float, digits: int = 2) -> str:
    return f'{value:.{digits}f}'


def derived_index_facts(facts, subject=None, *, reported_fasting=None, pregnant=False) -> list[ReportSourceFact]:
    """Compute indices; returns new facts only (inputs are never modified).

    ``reported_fasting`` is the questionnaire's fasting answer (a string), ``False``
    when it states the draw was not fasting, or ``None``. Adult BMI and waist
    criteria are not applied during a documented pregnancy.
    """
    inputs = _collect(facts)
    age = getattr(subject, 'age', None) if subject is not None else None
    age = int(age) if isinstance(age, int) or (isinstance(age, str) and age.isdigit()) else None
    sex = str(getattr(subject, 'sex', None) or '') if subject is not None else ''
    male = sex in {'男', '男性', 'male', 'M', 'm'}
    female = sex in {'女', '女性', 'female', 'F', 'f'}
    output: list[ReportSourceFact] = []

    def date_of(*keys):
        dates = {inputs[key][1].date for key in keys if inputs[key][1].date}
        return next(iter(dates)) if len(dates) == 1 else None

    def add(code, name, value, unit, formula, keys, *, status=None, reference=None, abnormal=False,
            classified=False):
        basis = '；'.join(
            ' '.join(filter(None, (inputs[key][1].name, inputs[key][1].value, inputs[key][1].unit))) for key in keys
        )
        source = _CLASSIFICATION_SOURCE if classified else DERIVED_SOURCE
        output.append(
            ReportSourceFact(
                fact_id=stable_id('derived-fact', f'{code}|{value}|{date_of(*keys)}'),
                kind='clinical',
                name=name if classified else name + '（计算值）',
                value=value,
                unit=unit,
                reference_range=reference,
                status=status,
                date=date_of(*keys),
                source=f'{source}：{formula}{_OPERANDS_LABEL}{basis}',
                abnormal=abnormal,
            )
        )

    def has(*keys):
        if not all(key in inputs for key in keys):
            return False
        if len(keys) == 1:
            return True
        dates = {inputs[key][1].date for key in keys}
        return len(dates) == 1 and None not in dates and '' not in dates

    def val(key) -> float:
        return float(inputs[key][0])

    def fasting(key):
        return has(key) and _fasting_measurement(inputs[key][1], key)

    def routine_fasting(key):
        # Check-up chemistry is drawn fasting unless its label or the questionnaire says otherwise.
        return fasting(key) or has(key) and reported_fasting is not False and _routine_draw(inputs[key][1])

    paired_samples = has('fpg', 'insulin')
    if paired_samples:
        contexts = [getattr(inputs[key][1], '_clinical_source_context', {}) for key in ('fpg', 'insulin')]
        origins = [context.get('sourceSha256') or context.get('documentId') for context in contexts]
        # A common date is insufficient when the sources identify different
        # check-ups. Older single-document feeds carry neither identifier.
        paired_samples = origins[0] == origins[1] and all(
            (left := contexts[0].get(field)) is None or (right := contexts[1].get(field)) is None or left == right
            for field in ('examDate', 'sourceReportedAge', 'subject')
        )
    # Check-up glucose and insulin are drawn fasting unless an item is labelled
    # postprandial/random/OGTT or the questionnaire says the draw was not fasting.
    labelled = paired_samples and fasting('insulin') and fasting('fpg')
    routine = (
        paired_samples and reported_fasting is not False
        and _routine_draw(inputs['fpg'][1]) and _routine_draw(inputs['insulin'][1])
        and not any(re.search(r'HOMA[- ]?IR', fact.name, re.I) for fact in facts if fact.kind == 'clinical')
    )
    if (labelled or routine) and min(val('fpg'), val('insulin')) > 0:
        homa = val('fpg') * val('insulin') / 22.5
        method = 'HOMA-IR = 空腹血糖(mmol/L) × 空腹胰岛素(mIU/L) ÷ 22.5'
        if not labelled:
            method += (f'；问卷记录体检前{reported_fasting}' if reported_fasting
                       else '；检验单未单独标注空腹，按体检常规空腹采血计算')
        add(
            'homa-ir',
            '胰岛素抵抗指数 HOMA-IR',
            _fmt(homa),
            None,
            method,
            ('fpg', 'insulin'),
        )
        if val('fpg') > 3.5:
            beta = 20 * val('insulin') / (val('fpg') - 3.5)
            add(
                'homa-beta',
                '胰岛β细胞功能指数 HOMA-β',
                _fmt(beta, 0),
                '%',
                'HOMA-β = 20 × 空腹胰岛素(mIU/L) ÷ (空腹血糖(mmol/L) − 3.5)',
                ('fpg', 'insulin'),
            )
    if has('tc', 'hdl'):
        add(
            'non-hdl',
            '非高密度脂蛋白胆固醇 non-HDL-C',
            _fmt(val('tc') - val('hdl')),
            'mmol/L',
            'non-HDL-C = 总胆固醇 − HDL-C',
            ('tc', 'hdl'),
        )
    if has('tc', 'hdl', 'ldl') and val('tc') - val('hdl') - val('ldl') >= 0:
        add(
            'remnant-c',
            '残余胆固醇',
            _fmt(val('tc') - val('hdl') - val('ldl')),
            'mmol/L',
            '残余胆固醇 = 总胆固醇 − HDL-C − LDL-C',
            ('tc', 'hdl', 'ldl'),
        )
    if has('tg', 'hdl') and val('hdl') > 0:
        add(
            'tg-hdl',
            '甘油三酯/HDL-C 比值',
            _fmt(val('tg') / val('hdl')),
            None,
            'TG ÷ HDL-C（均为 mmol/L）',
            ('tg', 'hdl'),
        )
    adult = age is not None and age >= 18 and not pregnant
    # Do not produce a screening value for younger adults, where FIB-4 has low accuracy.
    if has('ast', 'alt', 'plt') and age is not None and age >= 35 and val('alt') > 0 and val('plt') > 0:
        fib4 = age * val('ast') / (val('plt') * math.sqrt(val('alt')))
        add(
            'fib-4',
            '肝纤维化指数 FIB-4',
            _fmt(fib4),
            None,
            f'FIB-4 = 年龄({age}岁) × AST ÷ (血小板(10⁹/L) × √ALT)',
            ('ast', 'alt', 'plt'),
        )
    if has('height', 'weight') and val('height') > 0:
        bmi = val('weight') / (val('height') / 100) ** 2
        category = None
        if adult:
            category = '肥胖' if bmi >= 28 else '超重' if bmi >= 24 else '体重正常' if bmi >= 18.5 else '体重过低'
        add(
            'bmi-cn',
            '体重指数 BMI 及中国成人分级' if adult else '体重指数 BMI',
            _fmt(bmi, 1),
            'kg/m2',
            'BMI = 体重(kg) ÷ 身高(m)²' + ('；中国成人标准：24.0–27.9 超重，≥28.0 肥胖' if adult else ''),
            ('height', 'weight'),
            status=category,
            reference='18.5-23.9' if adult else None,
            abnormal=adult and (bmi >= 24 or bmi < 18.5),
        )
    if adult and has('waist') and (male or female):
        limit = 90 if male else 85
        central = val('waist') >= limit
        add(
            'waist-cn',
            '腰围及中心型肥胖判定',
            inputs['waist'][1].value,
            'cm',
            f'中国成人标准：{"男" if male else "女"}性腰围 ≥{limit} cm 为中心型肥胖',
            ('waist',),
            status='中心型肥胖' if central else '未达中心型肥胖标准',
            reference=f'<{limit}',
            abnormal=central,
            classified=True,
        )
    components = []
    if adult and has('waist') and (male or female):
        components.append(('腹型肥胖', val('waist') >= (90 if male else 85), 'waist'))
    if routine_fasting('fpg') and not pregnant:
        components.append(('空腹血糖≥6.1 mmol/L', val('fpg') >= 6.1, 'fpg'))
    if has('sbp', 'dbp'):
        components.append(('血压≥130/85 mmHg', val('sbp') >= 130 or val('dbp') >= 85, 'sbp'))
    if routine_fasting('tg'):
        components.append(('空腹TG≥1.70 mmol/L', val('tg') >= 1.70, 'tg'))
    if routine_fasting('hdl'):
        components.append(('空腹HDL-C<1.04 mmol/L', val('hdl') < 1.04, 'hdl'))
    if len(components) == 5 and has('waist', 'fpg', 'sbp', 'dbp', 'tg', 'hdl'):
        met = [label for label, positive, _ in components if positive]
        add(
            'mets-cds',
            '代谢综合征组分计数',
            f'{len(met)}/5',
            None,
            '中华医学会糖尿病学分会标准：5项中具备≥3项诊断代谢综合征（已用药或已确诊者按记录另计）',
            tuple(dict.fromkeys(key for _, _, key in components)) + (('dbp',) if has('dbp') else ()),
            status=('符合项：' + '、'.join(met)) if met else '无符合项',
            abnormal=len(met) >= 3,
        )
    return output


def append_derived_indices(document, subject=None) -> list[str]:
    """Add computed indices to the working document once; returns their fact IDs."""
    from .clinical_report import _reported_source_items

    # A questionnaire about "this check-up" cannot establish which draw was
    # fasting across multiple check-ups. Require one identifiable clinical
    # collection and an explicit, uncontradicted duration of at least 8 hours.
    collections = set()
    for fact in document.facts:
        context = getattr(fact, '_clinical_source_context', {})
        if context.get('dataType') != 'INSPECTION' or not (
            re.search(_INPUTS['fpg'][0], fact.name, re.I) or re.search(_INPUTS['insulin'][0], fact.name, re.I)
        ):
            continue
        collections.add((context.get('sourceSha256') or context.get('documentId'), fact.date))
    fasting = None
    if len(collections) == 1 and all(next(iter(collections))):
        answers = {item['text'].strip() for item in _reported_source_items(document)
                   if re.search(r'(?:这次|本次)(?:体检|检查|采血)前.*空腹', item['question'])}
        if len(answers) == 1:
            answer = answers.pop()
            duration = re.fullmatch(r'空腹\s*(\d+(?:\.\d+)?)\s*(?:个)?小时(?:以上)?', answer)
            if duration and Decimal(duration[1]) >= 8:
                fasting = answer
            elif re.search(r'未空腹|没有空腹|非空腹|不空腹|吃(?:了|过)|进食', answer) or (
                duration and Decimal(duration[1]) < 8
            ):
                fasting = False
    from .assessment_clinical_rules import subject_context

    rows = [{'name': fact.name, 'value': fact.value, 'referenceRange': fact.reference_range}
            for fact in document.facts if fact.kind == 'clinical']
    pregnant = subject_context(subject, document, rows).pregnancy == 'pregnant' if subject is not None else False
    existing = {fact.fact_id for fact in document.facts}
    added = []
    for fact in derived_index_facts(document.facts, subject, reported_fasting=fasting, pregnant=pregnant):
        if fact.fact_id not in existing:
            document.facts.append(fact)
            existing.add(fact.fact_id)
            added.append(fact.fact_id)
    return added


__all__ = ['append_derived_indices', 'derived_index_context', 'derived_index_facts', 'is_derived_index']
