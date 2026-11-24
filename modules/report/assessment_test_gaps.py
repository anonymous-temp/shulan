"""Test-gap rules for section 1.3 part 3 ("检测盲区") of the six-chapter assessment.

Each rule is data: a finding (or an age/sex screening condition), the tests it
calls for, the reason, applicability and sources. Only tests that are absent
from this person's clinical records are returned. Findings reuse the analyte
and trigger vocabulary of :mod:`assessment_red_flags`, so a threshold means the
same thing in both tables.
"""

from __future__ import annotations

import math
import re
import unicodedata
from copy import deepcopy

from .assessment_red_flags import SOURCES as RED_FLAG_SOURCES
from .assessment_red_flags import applies
from .assessment_red_flags import evaluate_trigger
from .assessment_red_flags import subject_context

RULES_VERSION = 'assessment-test-gaps.v1'

SOURCES = {
    **RED_FLAG_SOURCES,
    'bsh2014_b12': {
        'title': 'Guidelines for the diagnosis and treatment of cobalamin and folate disorders',
        'publisher': 'Devalia V, Hamilton MS, Molloy AM; British Committee for Standards in Haematology',
        'citation': 'Br J Haematol, 2014, 166(4): 496-513',
        'doi': '10.1111/bjh.12959',
        'year': 2014,
    },
    'esc2019_lipids': {
        'title': '2019 ESC/EAS Guidelines for the management of dyslipidaemias',
        'publisher': 'Mach F, et al.',
        'citation': 'Eur Heart J, 2020, 41(1): 111-188',
        'doi': '10.1093/eurheartj/ehz455',
        'year': 2019,
    },
    'ata2015_nodule': {
        'title': '2015 American Thyroid Association Management Guidelines for Adult Patients with Thyroid Nodules '
        'and Differentiated Thyroid Cancer',
        'publisher': 'Haugen BR, et al.',
        'citation': 'Thyroid, 2016, 26(1): 1-133',
        'doi': '10.1089/thy.2015.0020',
        'year': 2016,
    },
    'ata2012_hypothyroid': {
        'title': 'Clinical practice guidelines for hypothyroidism in adults: cosponsored by the American '
        'Association of Clinical Endocrinologists and the American Thyroid Association',
        'publisher': 'Garber JR, et al.',
        'citation': 'Thyroid, 2012, 22(12): 1200-1235',
        'doi': '10.1089/thy.2012.0205',
        'year': 2012,
    },
    'gastric_cn2017': {
        'title': '中国早期胃癌筛查流程专家共识意见（草案）（2017年，上海）',
        'publisher': '国家消化系统疾病临床医学研究中心；中华医学会消化内镜学分会；中华医学会健康管理学分会等',
        'citation': '中华健康管理学杂志, 2018, 12(1): 8-14',
        'year': 2018,
        'note': '筛查目标人群为年龄≥40岁且符合任一风险条件；血清学筛查包括胃蛋白酶原、胃泌素17等',
    },
    'iscd2023': {
        'title': '2023 ISCD Official Positions – Adult',
        'publisher': 'International Society for Clinical Densitometry',
        'citation': 'International Society for Clinical Densitometry, 2023',
        'url': 'https://iscd.org/wp-content/uploads/2024/03/2023-ISCD-Adult-Positions.pdf',
        'year': 2023,
        'note': '骨密度检测适用于 65 岁及以上女性、70 岁及以上男性',
    },
    'aace2020_op': {
        'title': 'AACE/ACE Clinical Practice Guidelines for the Diagnosis and Treatment of Postmenopausal '
        'Osteoporosis—2020 Update',
        'publisher': 'Camacho PM, et al.',
        'citation': 'Endocr Pract, 2020, 26(Suppl 1): 1-46',
        'year': 2020,
        'note': 'Measure serum 25(OH)D in patients at risk for vitamin D insufficiency, particularly those with '
        'osteoporosis',
    },
    'ada2025_s2': {
        'title': '2. Diagnosis and Classification of Diabetes: Standards of Care in Diabetes—2025',
        'publisher': 'American Diabetes Association Professional Practice Committee',
        'citation': 'Diabetes Care, 2025, 48(Suppl 1): S27-S49',
        'doi': '10.2337/dc25-S002',
        'year': 2025,
    },
    'ada2025_s11': {
        'title': '11. Chronic Kidney Disease and Risk Management: Standards of Care in Diabetes—2025',
        'publisher': 'American Diabetes Association Professional Practice Committee',
        'citation': 'Diabetes Care, 2025, 48(Suppl 1)',
        'doi': '10.2337/dc25-S011',
        'year': 2025,
    },
    'easl2024_masld': {
        'title': 'EASL-EASD-EASO Clinical Practice Guidelines on the management of metabolic '
        'dysfunction-associated steatotic liver disease (MASLD)',
        'publisher': 'European Association for the Study of the Liver, et al.',
        'citation': 'J Hepatol, 2024, 81(3): 492-542',
        'doi': '10.1016/j.jhep.2024.04.031',
        'year': 2024,
    },
    'esh2023_htn': {
        'title': '2023 ESH Guidelines for the management of arterial hypertension',
        'publisher': 'Mancia G, et al.',
        'citation': 'J Hypertens, 2023, 41(12): 1874-2071',
        'doi': '10.1097/HJH.0000000000003480',
        'year': 2023,
    },
}

# A test is "done" when a clinical record name matches ``done``. Rows that
# say a test was refused or not performed never count as done.
TESTS = {
    'folate': {'test': '血清叶酸', 'done': r'叶酸'},
    'vitaminB12': {'test': '维生素 B12', 'done': r'维生素\s*B\s*12|\bB12\b|钴胺'},
    'homocysteine': {'test': '同型半胱氨酸', 'done': r'同型半胱氨酸|\bHcy\b'},
    'ferritin': {'test': '血清铁蛋白及铁代谢', 'done': r'铁蛋白|ferritin'},
    'lpa': {'test': '脂蛋白(a)', 'done': r'脂蛋白\s*[\(（]\s*a\s*[\)）]|\bLp\s*\(?\s*a\b'},
    'apoB': {'test': '载脂蛋白 B（ApoB）', 'done': r'载脂蛋白\s*B|apo\s*-?\s*B\b'},
    'tsh': {'test': '促甲状腺激素（TSH）', 'done': r'促甲状腺(?:激)?素|\bTSH\b'},
    'ft4': {'test': '游离甲状腺素（FT4）', 'done': r'游离甲状腺素|\bFT4\b'},
    'ft3': {'test': '游离三碘甲状腺原氨酸（FT3）', 'done': r'游离三碘甲状腺原氨酸|\bFT3\b'},
    'tpoab': {'test': '甲状腺过氧化物酶抗体（TPOAb）', 'done': r'过氧化物酶抗体|TPO\s*-?\s*Ab|抗\s*TPO|A-?TPO'},
    'pepsinogen': {'test': '血清胃蛋白酶原Ⅰ/Ⅱ', 'done': r'胃蛋白酶原|\bPG\s*(?:I|Ⅰ|1)\b|\bPGI\b'},
    'gastrin17': {'test': '血清胃泌素-17', 'done': r'胃泌素\s*-?\s*17|\bG\s*-?\s*17\b'},
    'vitaminD': {'test': '血清 25-羟维生素 D', 'done': r'25\s*-?\s*羟|25\s*\(?\s*OH\s*\)?\s*D|维生素\s*D'},
    'dxa': {'test': '双能 X 线骨密度（DXA）', 'done': r'双能|DXA|DEXA'},
    'ogtt': {'test': '口服葡萄糖耐量试验（OGTT）', 'done': r'OGTT|糖耐量|葡萄糖耐量|餐后\s*2\s*(?:小时|h)'},
    'hba1c': {'test': '糖化血红蛋白（HbA1c）', 'done': r'糖化血红蛋白|HbA1c'},
    'fastingGlucose': {'test': '空腹血糖', 'done': r'空腹(?:血)?(?:葡萄)?糖|^\s*(?:血)?葡萄糖|^\s*血糖'},
    'hbsag': {
        'test': '乙肝表面抗原（HBsAg）',
        'done': r'乙(?:型)?肝(?:炎)?(?:病毒)?表面抗原|HBsAg|乙肝五项|乙肝两对半',
    },
    'antiHcv': {'test': '丙肝抗体（抗-HCV）', 'done': r'丙(?:型)?肝(?:炎)?|HCV'},
    'elastography': {'test': '肝脏瞬时弹性成像', 'done': r'弹性成像|FibroScan|肝脏硬度|LSM|受控衰减'},
    'uacr': {
        'test': '尿白蛋白/肌酐比（UACR）',
        'done': r'微量白蛋白|白蛋白\s*/\s*肌酐|尿白蛋白|UACR|\bACR\b|尿蛋白定量',
    },
    'fit': {'test': '粪便免疫化学检测（FIT）', 'done': r'隐血|潜血|\bOB\b|FOBT|\bFIT\b|肠镜'},
}

_NOT_DONE = re.compile(r'拒检|未做|未检|未查|弃检|放弃')
_DECLINED_ANSWER = re.compile(r'^\s*(?:没有|无|否|从不|不|未|没)\b|^\s*(?:没有|无|否|从不)')


def _abnormal(analyte, direction):
    key = 'uln' if direction == 'high' else 'lln'
    op = '>' if direction == 'high' else '<'
    return {'type': 'numeric', 'analyte': analyte, 'op': op, key: 1.0}


_DYSLIPIDEMIA = {
    'type': 'any',
    'of': [
        _abnormal('total_cholesterol', 'high'),
        _abnormal('triglycerides', 'high'),
        _abnormal('ldl_c', 'high'),
        _abnormal('hdl_c', 'low'),
    ],
}
_DIABETES_RANGE = {
    'type': 'any',
    'of': [
        {'type': 'numeric', 'analyte': 'fasting_glucose', 'op': '>=', 'value': 7.0},
        {'type': 'numeric', 'analyte': 'hba1c', 'op': '>=', 'value': 6.5},
    ],
}
_PREDIABETES_RANGE = {
    'type': 'any',
    'of': [
        {
            'type': 'all',
            'of': [
                {'type': 'numeric', 'analyte': 'fasting_glucose', 'op': '>=', 'value': 5.6},
                {'type': 'numeric', 'analyte': 'fasting_glucose', 'op': '<', 'value': 7.0},
            ],
        },
        {
            'type': 'all',
            'of': [
                {'type': 'numeric', 'analyte': 'hba1c', 'op': '>=', 'value': 5.7},
                {'type': 'numeric', 'analyte': 'hba1c', 'op': '<', 'value': 6.5},
            ],
        },
    ],
}
_FATTY_LIVER = {
    'type': 'any',
    'of': [
        {'type': 'finding', 'organ': r'肝', 'finding': r'脂肪肝|脂肪性肝|脂肪浸润|脂肪变'},
        {'type': 'reported', 'pattern': r'脂肪肝|脂肪性肝'},
    ],
}
_LIVER_ENZYMES = {
    'type': 'any',
    'of': [_abnormal('alt', 'high'), _abnormal('ast', 'high'), _abnormal('ggt', 'high')],
}

GAP_RULES = (
    {
        'id': 'TG-MACROCYTOSIS-RDW',
        'title': 'MCV 偏大或 RDW 升高',
        'trigger': {'type': 'any', 'of': [_abnormal('mcv', 'high'), _abnormal('rdw', 'high')]},
        'tests': ['folate', 'vitaminB12', 'homocysteine'],
        'reason': '平均红细胞体积偏大或红细胞分布宽度升高，需排查叶酸、维生素 B12 缺乏及相关的同型半胱氨酸升高。',
        'sources': ['bsh2014_b12'],
    },
    {
        'id': 'TG-ANEMIA-IRON',
        'title': '血红蛋白低于参考下限',
        'trigger': {'type': 'numeric', 'analyte': 'hemoglobin', 'op': '<', 'lln': 1.0},
        'tests': ['ferritin'],
        'reason': '血红蛋白低于参考下限，铁蛋白是判断缺铁的首选检查。',
        'sources': ['bsg2021_ida'],
    },
    {
        'id': 'TG-LIPID-LPA',
        'title': '血脂异常、颈动脉斑块或糖尿病范围血糖',
        'trigger': {
            'type': 'any',
            'of': [
                _DYSLIPIDEMIA,
                _DIABETES_RANGE,
                {'type': 'finding', 'organ': r'颈动脉|动脉', 'finding': r'斑块'},
            ],
        },
        'tests': ['lpa'],
        'reason': '存在血脂异常或动脉粥样硬化相关发现，脂蛋白(a)用于补充心血管风险评估，成年人至少检测一次。',
        'sources': ['esc2019_lipids'],
    },
    {
        'id': 'TG-LIPID-APOB',
        'title': '血脂异常、糖尿病范围血糖或肥胖',
        'trigger': {
            'type': 'any',
            'of': [
                _DYSLIPIDEMIA,
                _DIABETES_RANGE,
                {'type': 'numeric', 'analyte': 'bmi', 'op': '>=', 'value': 28.0},
            ],
        },
        'tests': ['apoB'],
        'reason': '甘油三酯升高、糖尿病或肥胖时，载脂蛋白 B 比低密度脂蛋白胆固醇更能反映致动脉粥样硬化颗粒负荷。',
        'sources': ['esc2019_lipids'],
    },
    {
        'id': 'TG-THYROID-NODULE',
        'title': '甲状腺结节',
        'trigger': {
            'type': 'any',
            'of': [
                {'type': 'finding', 'organ': r'甲状腺', 'finding': r'结节'},
                {'type': 'grade', 'system': 'tirads', 'min': 1},
            ],
        },
        'tests': ['tsh', 'ft4'],
        'reason': '甲状腺结节的初始评估需测定血清促甲状腺激素，必要时结合游离甲状腺素。',
        'sources': ['ata2015_nodule'],
    },
    {
        'id': 'TG-TSH-ABNORMAL',
        'title': '促甲状腺激素异常',
        'trigger': {'type': 'any', 'of': [_abnormal('tsh', 'high'), _abnormal('tsh', 'low')]},
        'tests': ['ft4', 'ft3', 'tpoab'],
        'reason': (
            '促甲状腺激素超出参考区间，需结合游离甲状腺激素判断功能状态，并以甲状腺过氧化物酶抗体判断自身免疫背景。'
        ),
        'sources': ['ata2012_hypothyroid'],
    },
    {
        'id': 'TG-GASTRIC-RISK',
        'title': '40 岁以上且有胃癌风险因素',
        'trigger': {
            'type': 'any',
            'of': [
                {'type': 'qualitative', 'analyte': 'hp', 'min_grade': 1},
                {
                    'type': 'reported',
                    'pattern': r'萎缩性胃炎|肥厚性胃炎|胃溃疡|胃息肉|残胃|胃(?:部)?(?:切除|手术)|肠化|恶性贫血',
                },
                {'type': 'reported', 'pattern': r'胃癌', 'scope': 'family'},
                {'type': 'reported', 'pattern': r'腌制|咸菜|偏咸|高盐|很咸'},
            ],
        },
        'applies': {'min_age': 40},
        'tests': ['pepsinogen', 'gastrin17'],
        'reason': (
            '年龄 40 岁以上并有幽门螺杆菌感染、胃部疾病史、胃癌家族史或高盐腌制饮食等风险因素，属于胃癌筛查目标人群。'
        ),
        'sources': ['gastric_cn2017'],
    },
    {
        'id': 'TG-LOW-BONE-MASS',
        'title': '骨量减少或骨质疏松',
        'trigger': {
            'type': 'any',
            'of': [
                {'type': 'finding', 'organ': r'骨', 'finding': r'骨量减少|骨量低|骨质疏松|骨密度(?:降低|减低)'},
                {'type': 'tscore', 'max': -1.0},
            ],
        },
        'tests': ['vitaminD'],
        'reason': '骨量减少或骨质疏松的评估需了解维生素 D 营养状态。',
        'sources': ['aace2020_op'],
    },
    {
        'id': 'TG-BMD-SCREEN-AGE',
        'title': '达到骨密度筛查年龄',
        'trigger': {'type': 'always'},
        'applies': {'any': [{'sex': 'female', 'min_age': 65}, {'sex': 'male', 'min_age': 70}]},
        'tests': ['dxa'],
        'reason': '65 岁及以上女性、70 岁及以上男性建议行双能 X 线骨密度检查。',
        'sources': ['iscd2023'],
    },
    {
        'id': 'TG-PREDIABETES',
        'title': '空腹血糖或糖化血红蛋白处于糖尿病前期范围',
        'trigger': _PREDIABETES_RANGE,
        'tests': ['ogtt', 'hba1c', 'fastingGlucose'],
        'reason': (
            '空腹血糖 5.6–6.9 mmol/L 或糖化血红蛋白 5.7%–6.4% 属糖尿病前期范围，'
            '需以口服葡萄糖耐量试验等明确糖代谢状态。'
        ),
        'sources': ['ada2025_s2'],
    },
    {
        'id': 'TG-DIABETES-RANGE',
        'title': '空腹血糖或糖化血红蛋白达到糖尿病范围',
        'trigger': _DIABETES_RANGE,
        'tests': ['hba1c', 'fastingGlucose', 'uacr'],
        'reason': '血糖达到糖尿病诊断水平，需以另一项血糖指标确认，并评估尿白蛋白/肌酐比以筛查肾脏受累。',
        'sources': ['ada2025_s2', 'ada2025_s11'],
    },
    {
        'id': 'TG-LIVER-VIRAL',
        'title': '肝酶升高或脂肪肝',
        'trigger': {'type': 'any', 'of': [_LIVER_ENZYMES, _FATTY_LIVER]},
        'tests': ['hbsag', 'antiHcv'],
        'reason': '肝酶升高或脂肪肝时，需排除乙型、丙型病毒性肝炎等其他肝病原因。',
        'sources': ['easl2024_masld'],
    },
    {
        'id': 'TG-LIVER-FIBROSIS',
        'title': 'FIB-4 ≥1.3',
        'trigger': {'type': 'fib4', 'min': 1.3},
        'tests': ['elastography'],
        'reason': 'FIB-4 指数不低于 1.3 时，需以肝脏瞬时弹性成像进一步评估肝纤维化。',
        'sources': ['easl2024_masld'],
    },
    {
        'id': 'TG-HYPERTENSION-UACR',
        'title': '血压升高或高血压病史',
        'trigger': {
            'type': 'any',
            'of': [
                {'type': 'numeric', 'analyte': 'sbp', 'op': '>=', 'value': 140.0},
                {'type': 'numeric', 'analyte': 'dbp', 'op': '>=', 'value': 90.0},
                {'type': 'reported', 'pattern': r'高血压'},
            ],
        },
        'tests': ['uacr'],
        'reason': '血压升高或有高血压病史时，尿白蛋白/肌酐比用于评估肾脏靶器官损害。',
        'sources': ['esh2023_htn'],
    },
    {
        'id': 'TG-PROTEINURIA',
        'title': '尿蛋白阳性',
        'trigger': {'type': 'qualitative', 'analyte': 'urine_protein', 'min_grade': 1},
        'tests': ['uacr'],
        'reason': '尿蛋白阳性时，需以尿白蛋白/肌酐比定量评估蛋白尿程度。',
        'sources': ['kdigo2024_ckd'],
    },
    {
        'id': 'TG-CRC-SCREEN-AGE',
        'title': '达到结直肠癌筛查年龄',
        'trigger': {'type': 'always'},
        'applies': {'min_age': 50, 'max_age': 75},
        'tests': ['fit'],
        'reason': '50–75 岁人群建议进行结直肠癌筛查，粪便免疫化学检测是常用的初筛方法。',
        'sources': ['crc_cn2020'],
    },
)


def _norm(text):
    return unicodedata.normalize('NFKC', str(text or '')).strip()


def _get(row, key, default=None):
    value = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
    return default if value is None else value


def _fact_id(row):
    for key in ('factId', 'fact_id', 'id'):
        value = _get(row, key)
        if value is not None:
            return value
    return None


def _done(test_code, facts):
    pattern = re.compile(TESTS[test_code]['done'], re.I)
    for row in facts:
        if _get(row, 'kind') != 'clinical':
            continue
        name, value = _norm(_get(row, 'name')), _norm(_get(row, 'value'))
        if _NOT_DONE.search(name) or _NOT_DONE.search(value):
            continue
        if pattern.search(name):
            return True
    return False


def _reported(trigger, facts):
    pattern = re.compile(trigger['pattern'])
    family = trigger.get('scope') == 'family'
    hits = []
    for row in facts:
        if _get(row, 'kind') != 'self_reported':
            continue
        name, value = _norm(_get(row, 'name')), _norm(_get(row, 'value'))
        is_family = bool(re.search(r'血亲|家族|亲属|父|母|兄|弟|姐|妹', name))
        if family != is_family or not value or _DECLINED_ANSWER.search(value):
            continue
        if pattern.search(value):
            hits.append(_fact_id(row))
    return hits


def _tscores(facts):
    hits = []
    for row in facts:
        if _get(row, 'kind') != 'clinical':
            continue
        text = _norm(_get(row, 'value'))
        if not re.search(r'双能|DXA|DEXA|骨密度', _norm(_get(row, 'name')) + text, re.I):
            continue
        for match in re.finditer(r'T\s*(?:值|-?\s*score)\s*[:：=]?\s*([-−]?\s*\d+(?:\.\d+)?)', text, re.I):
            hits.append((row, float(match.group(1).replace('−', '-').replace(' ', ''))))
    return hits


def _fib4(facts, ctx):
    matched, ids, _ = evaluate_trigger({'type': 'numeric', 'analyte': 'fib4', 'op': '>=', 'value': -1e9}, facts, ctx)
    values = []
    for row in facts:
        if _fact_id(row) in ids:
            try:
                values.append((row, float(_norm(_get(row, 'value')))))
            except ValueError:
                continue
    if values or ctx.get('age') is None:
        return values

    def first(analyte):
        found, identifiers, _ = evaluate_trigger(
            {'type': 'numeric', 'analyte': analyte, 'op': '>', 'value': 0.0},
            facts,
            ctx,
        )
        if not found:
            return None
        row = next(item for item in facts if _fact_id(item) == identifiers[0])
        try:
            return row, float(_norm(_get(row, 'value')))
        except ValueError:
            return None

    ast, alt, platelet = first('ast'), first('alt'), first('platelet')
    if not (ast and alt and platelet) or alt[1] <= 0 or platelet[1] <= 0:
        return []
    score = ctx['age'] * ast[1] / (platelet[1] * math.sqrt(alt[1]))
    return [(ast[0], score), (alt[0], score), (platelet[0], score)]


def _evaluate(trigger, facts, ctx):
    """Return the triggering fact IDs, or ``None`` when the finding is absent."""
    kind = trigger['type']
    if kind == 'always':
        return []
    if kind == 'any':
        found, ids = False, []
        for item in trigger['of']:
            result = _evaluate(item, facts, ctx)
            if result is not None:
                found = True
                ids.extend(fid for fid in result if fid not in ids)
        return ids if found else None
    if kind == 'all':
        ids = []
        for item in trigger['of']:
            result = _evaluate(item, facts, ctx)
            if result is None:
                return None
            ids.extend(fid for fid in result if fid not in ids)
        return ids
    if kind == 'reported':
        hits = _reported(trigger, facts)
        return hits or None
    if kind == 'tscore':
        hits = [_fact_id(row) for row, value in _tscores(facts) if value <= trigger['max']]
        return list(dict.fromkeys(hits)) or None
    if kind == 'fib4':
        hits = [_fact_id(row) for row, value in _fib4(facts, ctx) if value >= trigger['min']]
        return list(dict.fromkeys(hits)) or None
    matched, ids, _ = evaluate_trigger(trigger, facts, ctx)
    return ids if matched else None


def evaluate_test_gaps(facts, subject=None):
    """Return ``testGaps`` rows for tests this person's records do not contain.

    One row per missing test; the first matching rule (table order) owns the
    row and later rules suggesting the same test are listed in ``alsoRuleIds``.
    """
    facts = list(facts or ())
    ctx = subject_context(subject, facts)
    by_test = {}
    for rule in GAP_RULES:
        if not applies(rule.get('applies'), ctx):
            continue
        triggered = _evaluate(rule['trigger'], facts, ctx)
        if triggered is None:
            continue
        for test_code in rule['tests']:
            if _done(test_code, facts):
                continue
            if test_code in by_test:
                existing = by_test[test_code]
                existing['alsoRuleIds'].append(rule['id'])
                existing['triggeredBy'].extend(fid for fid in triggered if fid not in existing['triggeredBy'])
                continue
            by_test[test_code] = {
                'code': test_code,
                'test': TESTS[test_code]['test'],
                'reason': rule['reason'],
                'ruleId': rule['id'],
                'finding': rule['title'],
                'triggeredBy': list(triggered),
                'applies': deepcopy(rule.get('applies') or {}),
                'sources': [{'key': key, **deepcopy(SOURCES[key])} for key in rule['sources']],
                'alsoRuleIds': [],
            }
    return list(by_test.values())


def rule_table():
    """The rule rows with resolved sources, for documentation and audit."""
    return [
        {
            **deepcopy(rule),
            'tests': [{'code': code, 'test': TESTS[code]['test']} for code in rule['tests']],
            'sources': [{'key': key, **deepcopy(SOURCES[key])} for key in rule['sources']],
        }
        for rule in GAP_RULES
    ]
