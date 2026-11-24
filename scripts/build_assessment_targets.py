"""Build ``modules/report/assessment_targets.json`` from verified literature.

Each candidate indicator is researched in three steps:

1. Evidence collection: DeepSeek Search (web) and PubMed E-utilities return
   guideline, consensus and study pages for generic medical queries. No
   personal data is ever sent; queries are fixed in ``CANDIDATES``.
2. Proposal: ``deepseek-flash`` reads only the collected excerpts and proposes
   target intervals, each with the index of its source and a verbatim quote.
3. Verification (program): the quote must occur in the source text (PubMed
   abstract or the fetched page), every proposed bound must occur as a number
   in that quote, a non-empty unit must occur next to it, and the source must
   have a title, a year and a URL/DOI. Anything else is rejected and logged.

Results are cached per indicator in ``--work-dir`` (one JSON file each) with a
``STATUS`` line per finished step, so an interrupted run continues where it
stopped. ``--assemble`` only rebuilds the table from the cache:

    source .../local_env.sh   # exports SHULAN_LLM_API_KEY (DeepSeek)
    python scripts/build_assessment_targets.py --work-dir DIR
    python scripts/build_assessment_targets.py --work-dir DIR --only hsCrp --force
    python scripts/build_assessment_targets.py --work-dir DIR --assemble

The table is versioned with the code; the report never searches at run time.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date
from decimal import Decimal
from decimal import InvalidOperation
from pathlib import Path
from urllib.parse import unquote
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


OUTPUT = Path(__file__).resolve().parents[1] / 'modules' / 'report' / 'assessment_targets.json'
MODEL = 'deepseek-flash'
CHAT_URL = 'https://api.deepseek.com/chat/completions'
EUTILS = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils'

# Conditions that a target may exclude or require. The report detects them
# from the subject's recorded history (see assessment_clinical_rules.py).
CONDITIONS = {
    'diabetes': '糖尿病',
    'ascvd': '动脉粥样硬化性心血管疾病（冠心病、心肌梗死、缺血性脑卒中等）',
    'hypertension': '高血压',
    'chronicKidneyDisease': '慢性肾脏病',
    'thyroidDisease': '甲状腺疾病或正在服用甲状腺药物',
    'thyroidSurgery': '甲状腺术后',
    'gout': '痛风',
    'liverDisease': '慢性肝病',
    'lipidLoweringTherapy': '正在服用降脂药',
}

# Indicator catalog: aliases, abbreviations, canonical unit and conversion
# factors to that unit (value_canonical = value_source * factor). The report
# uses the same catalog to recognise differently written item names.
CATALOG = {
    'fpg': {
        'label': '空腹血糖',
        'aliases': ['空腹血糖', '空腹葡萄糖', '空腹血浆葡萄糖', '葡萄糖', '血糖', '血清葡萄糖', '血浆葡萄糖'],
        'abbreviations': ['GLU', 'FPG', 'FBG', 'FBS'],
        'unit': 'mmol/L',
        'conversions': {'mmol/l': 1, 'mg/dl': 0.0555},
        'excludeName': r'餐后|随机|尿|[12]\s*(?:h|小时)|糖耐量|OGTT|胰岛素|糖化|果糖胺|脑脊液|胸水|腹水',
    },
    'fastingInsulin': {
        'label': '空腹胰岛素',
        'aliases': ['空腹胰岛素', '胰岛素', '血清胰岛素', '空腹血清胰岛素'],
        'abbreviations': ['INS', 'FINS'],
        'unit': 'μIU/mL',
        'conversions': {'μiu/ml': 1, 'miu/l': 1, 'μu/ml': 1, 'mu/l': 1, 'pmol/l': 1 / 6},
        'excludeName': r'抗体|餐后|释放|[12]\s*(?:h|小时)|C\s*-?\s*肽|样生长因子|IGF',
    },
    'cPeptide': {
        'label': '空腹C肽',
        'aliases': ['空腹C肽', 'C肽', '血清C肽', 'C-肽'],
        'abbreviations': ['CP', 'C-P', 'CPR', 'C-peptide'],
        'unit': 'ng/mL',
        'conversions': {'ng/ml': 1, 'μg/l': 1, 'nmol/l': 3.02, 'pmol/l': 0.00302},
        'excludeName': r'餐后|释放|[12]\s*(?:h|小时)',
    },
    'hba1c': {
        'label': '糖化血红蛋白',
        'aliases': ['糖化血红蛋白', '糖化血红蛋白A1c', '血红蛋白A1c', '糖基化血红蛋白'],
        'abbreviations': ['HbA1c', 'A1C', 'GHb', 'HbA1C'],
        'unit': '%',
        'conversions': {'%': 1, 'mmol/mol': 'ifcc'},
        'excludeName': r'白蛋白',
    },
    'homaIr': {
        'label': 'HOMA-IR',
        'aliases': ['胰岛素抵抗指数', '稳态模型胰岛素抵抗指数', '稳态模型评估胰岛素抵抗指数'],
        'abbreviations': ['HOMA-IR', 'HOMAIR'],
        'unit': '',
        'conversions': {'': 1},
    },
    'tg': {
        'label': '甘油三酯',
        'aliases': ['甘油三酯', '三酰甘油', '甘油三脂', '血清甘油三酯'],
        'abbreviations': ['TG', 'TRIG', 'TRIGL'],
        'unit': 'mmol/L',
        'conversions': {'mmol/l': 1, 'mg/dl': 0.01129},
        'excludeName': r'/|比值|比率',
    },
    'tgHdl': {
        'label': 'TG/HDL-C',
        'aliases': ['甘油三酯/HDL-C 比值', '甘油三酯/高密度脂蛋白胆固醇比值', '甘油三酯与高密度脂蛋白胆固醇比值'],
        'abbreviations': ['TG/HDL-C', 'TG/HDL'],
        'unit': '',
        # The report computes the ratio from mmol/L values; a source ratio must
        # state its lipid unit (a mg/dL-based ratio is 2.29 times larger).
        'conversions': {'': 1, 'mmol/l': 1, 'mg/dl': 0.4366},
        'sourceUnits': ['mmol/l', 'mg/dl'],
    },
    'tc': {
        'label': '总胆固醇',
        'aliases': ['总胆固醇', '胆固醇', '血清总胆固醇'],
        'abbreviations': ['TC', 'CHOL', 'T-CHO', 'TCHO'],
        'unit': 'mmol/L',
        'conversions': {'mmol/l': 1, 'mg/dl': 0.02586},
        'excludeName': r'高密度|低密度|非高|残余|残粒|/|比值',
    },
    'hdlC': {
        'label': 'HDL-C',
        'aliases': ['高密度脂蛋白胆固醇', '高密度脂蛋白', '高密度脂蛋白-胆固醇'],
        'abbreviations': ['HDL-C', 'HDL', 'HDLC', 'HDL-CH'],
        'unit': 'mmol/L',
        'conversions': {'mmol/l': 1, 'mg/dl': 0.02586},
        'excludeName': r'非高|/|比值|亚型',
    },
    'ldlC': {
        'label': 'LDL-C',
        'aliases': ['低密度脂蛋白胆固醇', '低密度脂蛋白', '低密度脂蛋白-胆固醇'],
        'abbreviations': ['LDL-C', 'LDL', 'LDLC', 'LDL-CH'],
        'unit': 'mmol/L',
        'conversions': {'mmol/l': 1, 'mg/dl': 0.02586},
        'excludeName': r'小而密|sdLDL|氧化|/|比值|受体',
    },
    'nonHdlC': {
        'label': '非HDL-C',
        'aliases': ['非高密度脂蛋白胆固醇'],
        'abbreviations': ['non-HDL-C', 'nonHDL-C', 'NHDL-C'],
        'unit': 'mmol/L',
        'conversions': {'mmol/l': 1, 'mg/dl': 0.02586},
    },
    'apoB': {
        'label': 'ApoB',
        'aliases': ['载脂蛋白B', '载脂蛋白B100', '载脂蛋白B-100'],
        'abbreviations': ['ApoB', 'APO-B', 'ApoB100'],
        'unit': 'g/L',
        'conversions': {'g/l': 1, 'mg/dl': 0.01},
        'excludeName': r'/|比值',
    },
    'lpa': {
        'label': '脂蛋白(a)',
        'aliases': ['脂蛋白(a)', '脂蛋白a', '脂蛋白α'],
        'abbreviations': ['Lp(a)', 'LPA', 'LP-a'],
        'unit': 'mg/L',
        'conversions': {'mg/l': 1, 'mg/dl': 10, 'g/l': 1000},
    },
    'uricAcid': {
        'label': '血尿酸',
        'aliases': ['尿酸', '血尿酸', '血清尿酸'],
        'abbreviations': ['UA', 'URIC', 'SUA'],
        'unit': 'μmol/L',
        'conversions': {'μmol/l': 1, 'mg/dl': 59.48, 'mmol/l': 1000},
        'excludeName': r'尿液|尿尿酸|24\s*h|排泄|清除|/|比值',
    },
    'hsCrp': {
        'label': 'hs-CRP',
        'aliases': ['超敏C反应蛋白', '高敏C反应蛋白', '超敏CRP', '全程C反应蛋白超敏部分'],
        'abbreviations': ['hs-CRP', 'hsCRP', 'hCRP', 'HS-CRP'],
        'unit': 'mg/L',
        'conversions': {'mg/l': 1, 'mg/dl': 10},
    },
    'alt': {
        'label': 'ALT',
        'aliases': ['丙氨酸氨基转移酶', '丙氨酸转氨酶', '谷丙转氨酶', '丙氨酸氨基转氨酶'],
        'abbreviations': ['ALT', 'GPT', 'SGPT'],
        'unit': 'U/L',
        'conversions': {'u/l': 1, 'iu/l': 1},
        'excludeName': r'/|比值',
    },
    'ast': {
        'label': 'AST',
        'aliases': [
            '天门冬氨酸氨基转移酶',
            '天冬氨酸氨基转移酶',
            '天门冬氨酸转氨酶',
            '天冬氨酸转氨酶',
            '谷草转氨酶',
        ],
        'abbreviations': ['AST', 'GOT', 'SGOT'],
        'unit': 'U/L',
        'conversions': {'u/l': 1, 'iu/l': 1},
        'excludeName': r'/|比值|线粒体|同工酶',
    },
    'ggt': {
        'label': 'GGT',
        'aliases': [
            'γ-谷氨酰转移酶',
            'γ-谷氨酰基转移酶',
            '谷氨酰转肽酶',
            'γ-谷氨酰转肽酶',
            '谷氨酰基转移酶',
            '谷氨酰转移酶',
            '谷氨酰胺转移酶',
        ],
        'abbreviations': ['GGT', 'γ-GT', 'GGTP', 'r-GT', 'γGT'],
        'unit': 'U/L',
        'conversions': {'u/l': 1, 'iu/l': 1},
    },
    'alp': {
        'label': 'ALP',
        'aliases': ['碱性磷酸酶', '血清碱性磷酸酶'],
        'abbreviations': ['ALP', 'AKP'],
        'unit': 'U/L',
        'conversions': {'u/l': 1, 'iu/l': 1},
        'excludeName': r'骨|同工酶',
    },
    'ferritin': {
        'label': '铁蛋白',
        'aliases': ['铁蛋白', '血清铁蛋白'],
        'abbreviations': ['Fer', 'FER', 'SF', 'Ferritin', 'FERR'],
        'unit': 'ng/mL',
        'conversions': {'ng/ml': 1, 'μg/l': 1, 'pmol/l': 0.445},
        'excludeName': r'转铁|结合',
    },
    'mcv': {
        'label': 'MCV',
        'aliases': ['平均红细胞体积', '红细胞平均体积'],
        'abbreviations': ['MCV'],
        'unit': 'fL',
        'conversions': {'fl': 1, 'μm3': 1},
    },
    'tsh': {
        'label': 'TSH',
        'aliases': ['促甲状腺激素', '促甲状腺素', '促甲状腺刺激激素', '超敏促甲状腺激素', '促甲状腺激素(超敏)'],
        'abbreviations': ['TSH', 'sTSH', 'hTSH', 'S-TSH', 'TSH3'],
        'unit': 'mIU/L',
        'conversions': {'miu/l': 1, 'μiu/ml': 1, 'mu/l': 1, 'μu/ml': 1},
        'excludeName': r'受体|抗体|释放激素',
    },
    'hcy': {
        'label': '同型半胱氨酸',
        'aliases': ['同型半胱氨酸', '血同型半胱氨酸', '血清同型半胱氨酸'],
        'abbreviations': ['Hcy', 'HCY', 'HCYS'],
        'unit': 'μmol/L',
        'conversions': {'μmol/l': 1},
    },
    'vitaminD': {
        'label': '25-羟维生素D',
        'aliases': [
            '25-羟基维生素D',
            '25羟维生素D',
            '25-羟维生素D',
            '25羟基维生素D',
            '维生素D',
            '总维生素D',
            '25-羟基维生素D3',
            '25-羟维生素D总',
        ],
        'abbreviations': ['25(OH)D', '25-OH-D', '25OHD', '25-OHD', 'VD', 'VitD'],
        'unit': 'ng/mL',
        'conversions': {'ng/ml': 1, 'μg/l': 1, 'nmol/l': 0.4006},
        'excludeName': r'1\s*,\s*25|二羟|受体|结合蛋白',
    },
    'bmi': {
        'label': 'BMI',
        'aliases': ['体重指数', '体质指数', '身体质量指数', '体重指数BMI'],
        'abbreviations': ['BMI'],
        'unit': 'kg/m2',
        'conversions': {'kg/m2': 1, '': 1},
    },
    'waist': {
        'label': '腰围',
        'aliases': ['腰围'],
        'abbreviations': ['WC'],
        'unit': 'cm',
        'conversions': {'cm': 1, 'm': 100, 'mm': 0.1},
        'excludeName': r'臀|比',
    },
    'whr': {
        'label': '腰臀比',
        'aliases': ['腰臀比', '腰臀围比'],
        'abbreviations': ['WHR'],
        'unit': '',
        'conversions': {'': 1},
    },
    'bodyFat': {
        'label': '体脂率',
        'aliases': ['体脂率', '体脂百分比', '体脂肪率', '体脂肪百分比', '体脂肪含量百分比'],
        'abbreviations': ['PBF', 'BF%'],
        'unit': '%',
        'conversions': {'%': 1},
    },
    'visceralFat': {
        'label': '内脏脂肪面积',
        'aliases': ['内脏脂肪面积'],
        'abbreviations': ['VFA'],
        'unit': 'cm2',
        'conversions': {'cm2': 1},
    },
    'sbp': {
        'label': '收缩压',
        'aliases': ['收缩压', '血压收缩压', '血压(收缩压)'],
        'abbreviations': ['SBP'],
        'unit': 'mmHg',
        'conversions': {'mmhg': 1, 'kpa': 7.50062},
        'excludeName': r'踝|下肢|脚|肺动脉',
    },
    'dbp': {
        'label': '舒张压',
        'aliases': ['舒张压', '血压舒张压', '血压(舒张压)'],
        'abbreviations': ['DBP'],
        'unit': 'mmHg',
        'conversions': {'mmhg': 1, 'kpa': 7.50062},
        'excludeName': r'踝|下肢|脚|肺动脉',
    },
}

# Generic medical queries only. ``pmids`` are known primary sources used as
# search seeds; they are verified like any other hit. ``example`` is the
# unsourced value of the client's sample report; it is adopted only when a
# verified source states it.
CANDIDATES = [
    {
        'indicator': 'homaIr',
        'hint': 'HOMA-IR 无单位，sourceUnit 填空字符串；优先采用中国人群或一般成人人群的大样本研究切点。',
        'pmids': ['24131857'],
        'example': '<1.5',
        'web': [
            'HOMA-IR cut-off insulin resistance Chinese adults population study',
            'HOMA-IR 胰岛素抵抗 切点 中国人群 研究',
        ],
        'pubmed': [
            'HOMA-IR cut-off values insulin resistance general adult population',
            'HOMA-IR cutoff insulin resistance Chinese adults',
        ],
        'defaults': {'excludeConditions': ['diabetes'], 'minAge': 18},
    },
    {
        'indicator': 'fastingInsulin',
        'pmids': ['14623617'],
        'example': '<8 μIU/mL',
        'web': ['fasting insulin cut-off insulin resistance overweight adults metabolic markers'],
        'pubmed': ['fasting insulin cut point identify insulin resistant overweight individuals'],
        'defaults': {'excludeConditions': ['diabetes'], 'minAge': 18},
    },
    {
        'indicator': 'cPeptide',
        'example': '1.2–2.5 ng/mL',
        'web': ['fasting C-peptide optimal range healthy adults guideline'],
        'pubmed': ['fasting C-peptide reference interval healthy adults'],
        'defaults': {'excludeConditions': ['diabetes'], 'minAge': 18},
    },
    {
        'indicator': 'hba1c',
        'pmids': ['41358893'],
        'example': '<5.4%',
        'web': [
            'ADA Standards of Care diagnosis and classification of diabetes A1C 5.7 6.4 prediabetes table',
            'A1C 5.7% to 6.4% prediabetes criteria nonpregnant adults',
        ],
        'pubmed': [
            'HbA1c 5.7 6.4 prediabetes diagnostic criteria American Diabetes Association',
            'glycated haemoglobin prediabetes threshold 5.7% guideline',
        ],
        'defaults': {'excludeConditions': ['diabetes'], 'minAge': 18, 'nonPregnant': True},
    },
    {
        'indicator': 'fpg',
        'pmids': ['41358893'],
        'example': '4.4–5.2 mmol/L',
        'web': [
            'impaired fasting glucose 100 mg/dL 5.6 mmol/L ADA criteria prediabetes',
            '中国2型糖尿病防治指南 空腹血糖受损 6.1 7.0 糖代谢状态分类',
        ],
        'pubmed': [
            'impaired fasting glucose 5.6 mmol/L American Diabetes Association criteria',
            'fasting plasma glucose 100 mg/dl impaired fasting glucose definition',
        ],
        'defaults': {'excludeConditions': ['diabetes'], 'minAge': 18, 'nonPregnant': True},
    },
    {
        'indicator': 'tg',
        'pmids': ['37711173', '21502576'],
        'example': '<1.2 mmol/L',
        'web': [
            '中国血脂管理指南 2023 甘油三酯 合适水平 1.7 mmol/L',
            'AHA scientific statement triglycerides optimal fasting level 100 mg/dL',
        ],
        'pubmed': [
            'triglycerides and cardiovascular disease scientific statement optimal triglyceride level',
            'Chinese guidelines for lipid management 2023 triglyceride',
        ],
        'defaults': {'excludeConditions': ['lipidLoweringTherapy'], 'minAge': 18, 'nonPregnant': True},
    },
    {
        'indicator': 'tgHdl',
        'hint': '比值必须注明计算所用的血脂单位：sourceUnit 只能填 mmol/L 或 mg/dL；原文未写明单位体系的比值不能采用。',
        'pmids': ['14623617'],
        'example': '<1.3',
        'web': ['triglyceride to HDL cholesterol ratio cut point insulin resistance mmol/L'],
        'pubmed': [
            'triglyceride HDL cholesterol ratio cut-off insulin resistance',
            'triglyceride to HDL-C ratio insulin resistance Chinese adults cutoff',
        ],
        'defaults': {'excludeConditions': ['diabetes', 'lipidLoweringTherapy'], 'minAge': 18},
    },
    {
        'indicator': 'tc',
        'pmids': ['37711173'],
        'example': None,
        'web': ['中国血脂管理指南 2023 总胆固醇 合适水平 5.2 mmol/L 边缘升高'],
        'pubmed': ['Chinese guidelines for lipid management 2023 total cholesterol'],
        'defaults': {
            'excludeConditions': ['ascvd', 'diabetes', 'lipidLoweringTherapy'],
            'minAge': 18,
            'nonPregnant': True,
        },
    },
    {
        'indicator': 'ldlC',
        'pmids': ['37711173'],
        'example': '<2.6 mmol/L',
        'web': [
            '中国血脂管理指南 2023 LDL-C 理想水平 2.6 合适水平 3.4 mmol/L',
            'Chinese guidelines for lipid management 2023 LDL-C target risk stratification 1.8 2.6 3.4',
        ],
        'pubmed': ['Chinese guidelines for lipid management (2023) LDL-C', 'LDL-C target risk category guideline'],
        'defaults': {
            'excludeConditions': ['ascvd', 'diabetes', 'lipidLoweringTherapy'],
            'minAge': 18,
            'nonPregnant': True,
        },
    },
    {
        'indicator': 'nonHdlC',
        'pmids': ['37711173'],
        'example': None,
        'web': ['中国血脂管理指南 2023 非HDL-C 理想水平 3.4 合适水平 4.1'],
        'pubmed': ['non-HDL cholesterol target Chinese guidelines lipid management'],
        'defaults': {
            'excludeConditions': ['ascvd', 'diabetes', 'lipidLoweringTherapy'],
            'minAge': 18,
            'nonPregnant': True,
        },
    },
    {
        'indicator': 'apoB',
        'example': '<0.8 g/L',
        'web': [
            'ESC EAS dyslipidaemia guidelines ApoB goal 65 80 100 mg/dL risk',
            'apolipoprotein B treatment target mg/dL guideline moderate risk',
        ],
        'pubmed': ['apolipoprotein B target values cardiovascular risk categories guideline'],
        'defaults': {'excludeConditions': ['lipidLoweringTherapy'], 'minAge': 18, 'nonPregnant': True},
    },
    {
        'indicator': 'lpa',
        'pmids': ['37711173'],
        'example': None,
        'web': ['中国血脂管理指南 2023 脂蛋白(a) 300 mg/L ASCVD 风险'],
        'pubmed': ['lipoprotein(a) threshold cardiovascular risk consensus statement'],
        'defaults': {'minAge': 18},
    },
    {
        'indicator': 'hdlC',
        'pmids': ['37711173'],
        'example': None,
        'web': ['中国血脂管理指南 2023 HDL-C 降低 1.0 mmol/L'],
        'pubmed': ['Chinese guidelines lipid management HDL-C low 1.0 mmol/L'],
        'defaults': {'excludeConditions': ['lipidLoweringTherapy'], 'minAge': 18, 'nonPregnant': True},
    },
    {
        'indicator': 'uricAcid',
        'pmids': ['40692263'],
        'example': '男 ≤380 μmol/L',
        'web': [
            '中国高尿酸血症与痛风诊疗指南 2019 血尿酸 420 μmol/L 360 μmol/L',
            'hyperuricemia definition serum urate 420 μmol/L guideline',
        ],
        'pubmed': [
            'Chinese guidelines for diagnosis and treatment of hyperuricemia and gout 2019',
            'serum urate target 360 μmol/L gout guideline',
        ],
        'defaults': {'minAge': 18},
    },
    {
        'indicator': 'hsCrp',
        'pmids': ['12551878', '30879355', '41378999'],
        'example': '<1 mg/L',
        'web': ['CDC AHA scientific statement markers of inflammation cardiovascular disease hs-CRP 1.0 3.0 mg/L'],
        'pubmed': [
            'markers of inflammation and cardiovascular disease application to clinical and public health '
            'practice statement for healthcare professionals'
        ],
        'defaults': {'minAge': 18, 'nonPregnant': True},
    },
    {
        'indicator': 'alt',
        'pmids': ['27995906'],
        'example': None,
        'web': [
            'ACG clinical guideline evaluation of abnormal liver chemistries true healthy normal ALT males females'
        ],
        'pubmed': ['ACG Clinical Guideline: Evaluation of Abnormal Liver Chemistries'],
        'defaults': {'excludeConditions': ['liverDisease'], 'minAge': 18},
    },
    {
        'indicator': 'ast',
        'example': '18–25 U/L',
        'web': ['AST optimal range 18-25 U/L healthy adults evidence'],
        'pubmed': ['aspartate aminotransferase optimal range healthy adults'],
        'defaults': {'excludeConditions': ['liverDisease'], 'minAge': 18},
    },
    {
        'indicator': 'ggt',
        'example': '男 <30 U/L',
        'web': ['gamma-glutamyltransferase upper reference limit healthy men women guideline'],
        'pubmed': ['gamma-glutamyltransferase optimal level cardiovascular mortality cut-off'],
        'defaults': {'excludeConditions': ['liverDisease'], 'minAge': 18},
    },
    {
        'indicator': 'alp',
        'example': '≥50 U/L',
        'web': ['low alkaline phosphatase threshold zinc magnesium deficiency adults'],
        'pubmed': ['low serum alkaline phosphatase adults clinical significance threshold'],
        'defaults': {'minAge': 18},
    },
    {
        'indicator': 'ferritin',
        'pmids': ['33909381'],
        'example': '男 50–200 ng/mL',
        'web': ['WHO guideline use of ferritin concentrations to assess iron status 15 μg/L 150 200 iron overload'],
        'pubmed': ['WHO guideline ferritin concentrations iron status individuals populations'],
        'defaults': {'minAge': 18, 'nonPregnant': True},
    },
    {
        'indicator': 'mcv',
        'example': '87–92 fL',
        'web': ['mean corpuscular volume optimal range 87-92 fL evidence'],
        'pubmed': ['mean corpuscular volume optimal range adults mortality'],
        'defaults': {'minAge': 18},
    },
    {
        'indicator': 'tsh',
        'example': '1.0–2.5 mIU/L',
        'web': ['TSH upper limit 2.5 mIU/L euthyroid guideline laboratory medicine practice'],
        'pubmed': ['TSH reference range upper limit 2.5 mIU/L euthyroid controversy guideline'],
        'defaults': {'excludeConditions': ['thyroidDisease', 'thyroidSurgery'], 'minAge': 18, 'nonPregnant': True},
    },
    {
        'indicator': 'hcy',
        'example': None,
        'web': [
            '高同型半胱氨酸血症 诊疗专家共识 血浆 Hcy 15 μmol/L 10 μmol/L',
            'hyperhomocysteinemia definition 15 μmol/L guideline',
        ],
        'pubmed': ['hyperhomocysteinemia definition plasma homocysteine 15 micromol/L consensus'],
        'defaults': {'minAge': 18},
    },
    {
        'indicator': 'vitaminD',
        'pmids': ['21118827', '21646368'],
        'example': None,
        'web': [
            'Institute of Medicine 2011 report vitamin D 20 ng/mL 50 nmol/L requirements 97.5% population',
            'Endocrine Society guideline vitamin D deficiency 20 ng/mL insufficiency 21-29',
        ],
        'pubmed': [
            '2011 report on dietary reference intakes for calcium and vitamin D from the Institute of '
            'Medicine what clinicians need to know',
            'evaluation treatment and prevention of vitamin D deficiency Endocrine Society clinical practice '
            'guideline 2011',
        ],
        'defaults': {'minAge': 18},
    },
    {
        'indicator': 'bmi',
        'pmids': ['12046553'],
        'example': None,
        'web': [
            '中国成人体重判定 WS/T 428 体重指数 18.5 24.0 28.0',
            'Chinese adults BMI cut-off 24 28 overweight obesity',
        ],
        'pubmed': [
            'predictive values of body mass index and waist circumference for risk factors Chinese adults',
            'body mass index cut-off points overweight obesity Chinese adults 24 28',
        ],
        'defaults': {'minAge': 18, 'nonPregnant': True},
    },
    {
        'indicator': 'waist',
        'pmids': ['12046553'],
        'example': None,
        'web': [
            '中国成人体重判定 腰围 男性 90 cm 女性 85 cm 中心型肥胖',
            'waist circumference cut-off Chinese adults 85 80 cm',
        ],
        'pubmed': [
            'predictive values of body mass index and waist circumference for risk factors Chinese adults',
            'waist circumference cutoff central obesity Chinese men women',
        ],
        'defaults': {'minAge': 18, 'nonPregnant': True},
    },
    {
        'indicator': 'whr',
        'example': None,
        'web': ['WHO expert consultation waist circumference and waist-hip ratio 0.90 men 0.85 women'],
        'pubmed': ['waist-hip ratio cut-off 0.90 men 0.85 women abdominal obesity WHO'],
        'defaults': {'minAge': 18, 'nonPregnant': True},
    },
    {
        'indicator': 'bodyFat',
        'example': None,
        'web': [
            'body fat percentage obesity definition 25% men 35% women guideline',
            '体脂率 肥胖 诊断 男性 25% 女性 30% 中国 指南',
        ],
        'pubmed': ['body fat percentage cut-off obesity men women definition'],
        'defaults': {'minAge': 18, 'nonPregnant': True},
    },
    {
        'indicator': 'visceralFat',
        'example': None,
        'web': ['内脏脂肪面积 80 cm2 腹型肥胖 中国 指南', 'visceral fat area 100 cm2 visceral obesity criteria'],
        'pubmed': ['visceral fat area cut-off visceral obesity Chinese', 'visceral fat area 100 cm2 obesity disease'],
        'defaults': {'minAge': 18, 'nonPregnant': True},
    },
    {
        'indicator': 'sbp',
        'pmids': ['40151633'],
        'example': None,
        'pair': 'dbp',
        'web': [
            '中国高血压防治指南 2024 正常血压 120 80 mmHg 正常高值 血压水平分类',
            'normal blood pressure less than 120 80 mmHg guideline classification',
        ],
        'pubmed': [
            'Chinese guidelines for the management of hypertension blood pressure classification normal',
            'blood pressure categories normal elevated stage 1 hypertension guideline 120/80',
        ],
        'defaults': {'excludeConditions': ['hypertension'], 'minAge': 18, 'nonPregnant': True},
    },
    {
        'indicator': 'dbp',
        'pmids': ['40151633'],
        'example': None,
        'web': [
            '中国高血压防治指南 2024 正常血压 120 80 mmHg 正常高值 血压水平分类',
            'normal blood pressure less than 120 80 mmHg guideline classification',
        ],
        'pubmed': [
            'Chinese guidelines for the management of hypertension blood pressure classification normal',
            'blood pressure categories normal elevated stage 1 hypertension guideline 120/80',
        ],
        'defaults': {'excludeConditions': ['hypertension'], 'minAge': 18, 'nonPregnant': True},
    },
]


# Web pages are accepted only from journals, publishers, literature databases
# and official health bodies; news, commercial and self-media pages are not
# sources even when they quote a guideline correctly.
AUTHORITATIVE_HOSTS = (
    'ncbi.nlm.nih.gov',
    'nih.gov',
    'cdc.gov',
    'who.int',
    'nhc.gov.cn',
    'chinacdc.cn',
    'nccd.org.cn',
    'cma.org.cn',
    'diabetesjournals.org',
    'ahajournals.org',
    'acc.org',
    'escardio.org',
    'academic.oup.com',
    'thelancet.com',
    'nejm.org',
    'jamanetwork.com',
    'bmj.com',
    'nature.com',
    'springer.com',
    'wiley.com',
    'sciencedirect.com',
    'frontiersin.org',
    'plos.org',
    'journals.lww.com',
    'endocrine.org',
    'idf.org',
    'heart.org',
    'nice.org.uk',
    'sciopen.com',
    'jgc301.com',
    'europepmc.org',
    'yiigle.com',
    'medjournals.cn',
    'chinacirculation.org',
    'cmj.org',
    'karger.com',
    'tandfonline.com',
    'mdpi.com',
    'biomedcentral.com',
    'acpjournals.org',
    'thyroid.org',
    'liebertpub.com',
    'kidney-international.org',
    'kdigo.org',
    'aace.com',
    'easd.org',
    'eas-society.org',
    'iris.who.int',
)


def authoritative(url: str) -> bool:
    host = (urlsplit(url).hostname or '').lower()
    return any(host == item or host.endswith('.' + item) for item in AUTHORITATIVE_HOSTS)


# Evidence ranking used when several verified entries compete for one indicator.
EVIDENCE_RANK = {'指南': 0, '行业标准': 1, '专家共识': 2, '科学声明': 3, '系统综述': 4, '综述': 5, '人群研究': 6}

# Reviewed curation of verified entries, applied deterministically when the
# table is assembled. Keys are (indicator, source bound value).
TIERS = {
    # ESC/EAS 2019 ApoB goals are risk-category goals; only categories the
    # report can recognise from recorded history are kept.
    ('apoB', 65): {'requiresConditions': ['ascvd']},
    ('apoB', 80): {'requiresConditions': ['diabetes']},
    ('apoB', 100): None,
    # AHA/ACC risk-enhancing threshold, not a goal.
    ('apoB', 130): None,
    # WHO: 150 µg/L applies to menstruating women; men and non-menstruating women use 200 µg/L.
    ('ferritin', 150): {'sex': 'female', 'maxAge': 49},
    # The 10 µmol/L threshold is used for hypertensive patients; the general definition is 15 µmol/L.
    ('hcy', 10): {'requiresConditions': ['hypertension']},
    # Adult criteria: the age span of a source's study population is not an applicability limit.
    ('waist', 85): {'minAge': 18, 'maxAge': None},
    ('waist', 80): {'minAge': 18, 'maxAge': None},
    ('dbp', 80): {'minAge': 18, 'maxAge': None},
    ('sbp', 120): {'minAge': 18, 'maxAge': None},
    # Chinese hyperuricemia/gout guideline: <360 is the urate-lowering goal of gout;
    # <300 (tophi) and >=180 (floor) are treatment details, not population targets.
    ('uricAcid', 360): {'requiresConditions': ['gout']},
    ('uricAcid', 300): None,
    ('uricAcid', 180): None,
}
# Reviewed source preference when two verified entries compete: the Chinese
# diabetes page found for fasting glucose has no usable title or year, so the
# ADA expert committee statement (normal fasting glucose <5.6 mmol/L) is used.
PREFERRED_HOSTS = {'fpg': ('diabetesjournals.org',)}
EXCLUDED_INDICATORS = {
    'fastingInsulin': '仅有小样本研究给出胰岛素抵抗切点，无指南或共识，不作目标区间',
    'ast': '仅有单一人群（献血者）研究给出正常上限，无指南或共识支持，不作目标区间',
}


_LIPID_HINT = (
    '资料《2023 Chinese guideline for lipid management》全文表2（Reference standards for major lipid indicators in '
    'low-risk groups for primary prevention of ASCVD in China）按 Ideal level / Reasonable level / Slight elevation / '
    'Elevation / Reduction 列出各血脂指标的数值（单位 mmol/L）；若该表在资料中，优先逐字引用该表文本，取 Ideal level，'
    '没有 Ideal level 的指标取 Reasonable level（HDL-C 取 Reduction 的反向界值，即 ≥ 该数）。'
)
_PRESSURE_HINT = (
    '优先采用《Chinese Guidelines for the Prevention and Treatment of Hypertension (2024 revision)》全文中的'
    '血压水平分类表，取“正常血压”的{}界值。'
)
HINTS = {
    **dict.fromkeys(('tc', 'tg', 'ldlC', 'nonHdlC', 'hdlC'), _LIPID_HINT),
    'hsCrp': (
        '优先采用指南或权威声明原文中健康成人心血管风险较低的 hs-CRP 界值'
        '（风险分层中的低风险切点或风险增强因素切点以下）。'
    ),
    'bmi': (
        '优先采用中国肥胖问题工作组原始研究（Predictive values of body mass index and waist circumference）'
        '摘要中的切点。'
    ),
    'sbp': _PRESSURE_HINT.format('收缩压'),
    'dbp': _PRESSURE_HINT.format('舒张压'),
}


def _log(work: Path, message: str) -> None:
    line = f'{time.strftime("%Y-%m-%d %H:%M:%S")} {message}'
    print(line, flush=True)
    with (work / 'STATUS').open('a', encoding='utf-8') as handle:
        handle.write(line + '\n')


def normalize(text: str) -> str:
    """Normalize spacing, dashes, comparison signs and entities for literal comparison."""
    text = html.unescape(unicodedata.normalize('NFKC', str(text or '')))
    text = re.sub(r'[‐-―−﹘﹣－]', '-', text)
    text = text.replace('≤', '<=').replace('≥', '>=').replace('＜', '<').replace('＞', '>')
    text = text.replace('µ', 'μ')
    return re.sub(r'\s+', '', text).casefold()


def numbers_in(text: str) -> list[Decimal]:
    result = []
    for token in re.findall(r'(?<![\d.])\d+(?:\.\d+)?(?!\d)(?!\.\d)', unicodedata.normalize('NFKC', text or '')):
        try:
            result.append(Decimal(token))
        except InvalidOperation:
            continue
    return result


def normalize_unit(unit: str | None) -> str:
    text = unicodedata.normalize('NFKC', str(unit or '')).strip().casefold()
    if text in {'无单位', '无', 'none', 'null', 'ratio', '比值', '-'}:
        return ''
    text = text.replace('µ', 'μ').replace('micro', 'μ').replace('²', '2').replace('^', '').replace('³', '3')
    text = re.sub(r'\s+', '', text)
    text = re.sub(r'^u(?=(?:iu|u|mol|g|l|m)/)', 'μ', text)
    text = re.sub(r'(?<=/)ul$', 'μl', text)
    return text


def clean_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))


class Researcher:
    def __init__(self, api_key: str, work: Path) -> None:
        self.api_key = api_key
        self.work = work
        # Imported here: the adapter package reads the application environment, which --assemble does not need.
        from adapters.report.deepseek_tools import DeepSeekToolsClient

        self.tools = DeepSeekToolsClient(api_key=api_key, timeout_seconds=40.0)
        self.http = httpx.AsyncClient(timeout=40.0, follow_redirects=True)
        self.ncbi = asyncio.Semaphore(1)
        self.fetch_cache: dict[str, str] = {}

    async def close(self) -> None:
        await self.tools.aclose()
        await self.http.aclose()

    async def _ncbi(self, path: str, params: dict) -> str:
        async with self.ncbi:
            for attempt in range(3):
                try:
                    response = await self.http.get(f'{EUTILS}/{path}', params=params)
                    await asyncio.sleep(0.4)
                    if response.status_code == 200:
                        return response.text
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(2 + attempt * 2)
        return ''

    async def pubmed(self, query: str, limit: int = 5) -> list[dict]:
        found = await self._ncbi('esearch.fcgi', {'db': 'pubmed', 'term': query, 'retmax': limit, 'retmode': 'json'})
        try:
            ids = json.loads(found)['esearchresult']['idlist']
        except (ValueError, KeyError):
            return []
        return await self.pubmed_records(ids)

    async def pubmed_records(self, ids: list[str], *, require_abstract: bool = True) -> list[dict]:
        if not ids:
            return []
        xml = await self._ncbi('efetch.fcgi', {'db': 'pubmed', 'id': ','.join(ids), 'retmode': 'xml'})
        records = []
        for article in re.findall(r'<Pubmed(?:Book)?Article>.*?</Pubmed(?:Book)?Article>', xml, re.S):
            pmid = re.search(r'<PMID[^>]*>(\d+)</PMID>', article)
            title = re.search(r'<(?:Article|Book)Title[^>]*>(.*?)</(?:Article|Book)Title>', article, re.S)
            parts = []
            for attrs, body in re.findall(r'<AbstractText([^>]*)>(.*?)</AbstractText>', article, re.S):
                label = re.search(r'Label="([^"]+)"', attrs)
                parts.append((f'{label[1]}: ' if label else '') + re.sub(r'<[^>]+>', '', body))
            abstract = ' '.join(parts)
            year = re.search(r'<PubDate>.*?<Year>(\d{4})</Year>', article, re.S) or re.search(
                r'<PubDate>.*?<MedlineDate>(\d{4})', article, re.S
            )
            doi = re.search(r'<ArticleId IdType="doi">([^<]+)</ArticleId>', article)
            pmc = re.search(r'<ArticleId IdType="pmc">(PMC\d+)</ArticleId>', article)
            journal = re.search(r'<Title>(.*?)</Title>', article, re.S)
            types = re.findall(r'<PublicationType[^>]*>(.*?)</PublicationType>', article)
            if not pmid or not title or require_abstract and not abstract.strip():
                continue
            records.append(
                {
                    'kind': 'pubmed',
                    'pmid': pmid[1],
                    'title': html.unescape(re.sub(r'<[^>]+>', '', title[1])).strip(),
                    'year': int(year[1]) if year else None,
                    'doi': html.unescape(doi[1]).strip() if doi else None,
                    'journal': html.unescape(journal[1]).strip() if journal else None,
                    'publicationTypes': types,
                    'pmcid': pmc[1] if pmc else None,
                    'url': f'https://pubmed.ncbi.nlm.nih.gov/{pmid[1]}/',
                    'text': html.unescape(abstract).strip(),
                }
            )
        return records

    async def full_text(self, pmcid: str) -> str:
        """Open-access PMC full text (NCBI E-utilities); tables are kept as plain text."""
        text = await self._ncbi('efetch.fcgi', {'db': 'pmc', 'id': pmcid.removeprefix('PMC'), 'retmode': 'xml'})
        if not text or '<body' not in text:
            return ''
        body = re.sub(r'<ref-list>.*?</ref-list>', ' ', text, flags=re.S)
        body = re.sub(r'</(?:p|td|th|tr|title|label|caption)>', ' ', body)
        return html.unescape(re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', body))).strip()

    async def web(self, query: str, limit: int = 6) -> list[dict]:
        try:
            hits = await self.tools.search(query, count=limit, need_content=True)
        except Exception as error:  # noqa: BLE001 - one failed query does not stop the indicator
            print(f'search failed: {query}: {error}', flush=True)
            return []
        result = []
        for hit in hits:
            url = clean_url(hit.url)
            if len(url) > 400 or 'X-Amz' in url or 'sciencedirectassets' in url:
                continue
            result.append(
                {
                    'kind': 'web',
                    'title': hit.title.strip(),
                    'url': url,
                    'date': hit.date,
                    'site': hit.site_name,
                    'text': (hit.content or hit.summary or hit.snippet or '').strip(),
                }
            )
        return result

    async def page_text(self, url: str) -> str:
        if url in self.fetch_cache:
            return self.fetch_cache[url]
        try:
            page = await self.tools.fetch(url)
        except Exception as error:  # noqa: BLE001 - fall back to search content
            print(f'fetch failed: {url}: {error}', flush=True)
            page = None
        text = page.content if page else ''
        self.fetch_cache[url] = text
        return text

    async def ask(self, prompt: str) -> dict:
        body = {'model': MODEL, 'messages': [{'role': 'user', 'content': prompt}], 'temperature': 0}
        headers = {'Authorization': f'Bearer {self.api_key}'}
        for attempt in range(3):
            try:
                response = await self.http.post(CHAT_URL, json=body, headers=headers, timeout=240.0)
                if response.status_code == 200:
                    content = response.json()['choices'][0]['message'].get('content') or ''
                    content = content.replace('<|endoftext|>', '')
                    match = re.search(r'\{.*\}', content, re.S)
                    if match:
                        return json.loads(match[0])
            except (httpx.HTTPError, ValueError, KeyError) as error:
                print(f'model call failed: {error}', flush=True)
            await asyncio.sleep(5 + attempt * 5)
        return {}


def _excerpt(text: str, keywords: list[str], limit: int = 2600) -> str:
    """The passages most likely to state a threshold: keyword plus comparison numbers, in source order."""
    if len(text) <= limit:
        return text
    size = 700
    chunks = [text[start : start + size] for start in range(0, len(text), size // 2)]
    lowered_keys = [keyword.casefold() for keyword in keywords if len(keyword) >= 2]
    scored = []
    for index, chunk in enumerate(chunks):
        lowered = chunk.casefold()
        hits = sum(lowered.count(key) for key in lowered_keys)
        if not hits:
            continue
        bounds = len(re.findall(r'[<>≤≥＜＞]\s*=?\s*\d', chunk)) + len(re.findall(r'\d\s*(?:-|–|~|至)\s*\d', chunk))
        levels = len(
            re.findall(
                r'理想|合适|正常|目标|切点|界值|ideal|optimal|target|goal|cut-?off|normal|threshold|'
                r'desirable|reasonable|risk',
                lowered,
            )
        )
        scored.append((hits + 3 * bounds + 2 * levels, index))
    if not scored:
        return text[:limit]
    chosen, total = [], 0
    for _, index in sorted(scored, reverse=True):
        if any(abs(index - other) <= 1 for other in chosen):
            continue
        chosen.append(index)
        total += size
        if total >= limit:
            break
    return ' … '.join(chunks[index] for index in sorted(chosen))[:limit]


PROMPT = """你在为体检报告的“目标区间”表查找有出处的数值。指标：{label}（{indicator}），报告使用的单位：{unit}。
下面是检索到的资料（编号、类型、标题、年份、网址、摘录）。只能使用这些资料，不得凭记忆补充。{hint}

要求：
1. 只采用临床指南、专家共识、权威机构科学声明/标准，或高质量研究（大样本人群研究、系统综述）中明确写出的数值界值。
2. 目标区间指“健康成人宜达到的范围”或“风险较低的范围”，例如风险分层中的低风险界值、理想水平、正常值上限、诊断切点以下。
   不要采用实验室参考区间的统计学上下限，也不要采用治疗药物剂量。
3. 每个目标必须给出资料编号 sourceIndex，以及从该资料摘录中**逐字复制**的一句原文 quote
   （不改写、不翻译、不省略中间文字，长度 20–400 字符），quote 中必须出现该目标的每个数值和单位。
4. 若资料按性别、年龄、风险层级给出不同数值，分别列出，并在 sex（male/female/null）、minAge、maxAge、
   requiresConditions、excludeConditions 中写明适用条件。条件只能取以下代码：{conditions}。
5. low/high 用资料原文的数值和单位（sourceUnit）填写；单侧界值只填一侧；lowInclusive/highInclusive 表示是否含等号。
6. kind 取 upperLimit（只有上限）、lowerLimit（只有下限）或 range（上下限都有意义）。原文给出“正常上限为 29–33”
   这类上限的取值范围时，kind=upperLimit，high 取其中较大的数，low 留空；不要把它当成区间。
7. 优先引用原始出处（指南原文、PubMed 摘要或全文）；只有原始出处不在资料中时，才可引用转述页面。
8. 若资料不能可靠支持任何目标，返回 targets 为空数组，并在 reason 中说明。宁缺毋滥。
9. 另外判断示例数值“{example}”是否被上述某条资料逐字支持：exampleSupported 为 true/false，exampleReason 写一句原因。

只输出 JSON：
{{"targets": [{{"sourceIndex": 0, "quote": "...", "kind": "upperLimit", "low": null, "high": 1.0, "lowInclusive": false,
"highInclusive": false, "sourceUnit": "mg/L", "sex": null, "minAge": 18, "maxAge": null, "nonPregnant": true,
"requiresConditions": [], "excludeConditions": [], "riskTier": "低风险/理想水平等简短说明",
"basis": "一句中文依据说明（引用资料结论，不扩展）",
"evidenceLevel": "指南|专家共识|科学声明|行业标准|系统综述|人群研究"}}], "reason": "...",
"exampleSupported": false, "exampleReason": "..."}}

资料：
{documents}
"""


def _convert(value, unit: str, indicator: str) -> Decimal | None:
    catalog = CATALOG[indicator]
    factor = catalog['conversions'].get(normalize_unit(unit))
    if factor is None or value is None:
        return None
    number = Decimal(str(value))
    if factor == 'ifcc':
        return (number * Decimal('0.09148') + Decimal('2.152')).quantize(Decimal('0.1'))
    converted = number * Decimal(str(factor))
    if factor == 1:
        return number
    digits = 0 if converted >= 100 else 1 if converted >= 10 else 2
    return converted.quantize(Decimal(1).scaleb(-digits))


def _verify(candidate: dict, document: dict, text: str, indicator: str) -> tuple[bool, str]:
    quote = str(candidate.get('quote') or '').strip()
    if not (20 <= len(quote) <= 600):
        return False, 'quote length outside 20–600 characters'
    corpus = normalize(text)
    if not corpus or normalize(quote) not in corpus:
        return False, 'quote not found verbatim in the source text'
    quoted_numbers = numbers_in(quote)
    for key in ('low', 'high'):
        value = candidate.get(key)
        if value is None:
            continue
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return False, f'{key} is not a number'
        if number not in quoted_numbers:
            return False, f'{key}={value} does not occur in the quote'
    if candidate.get('low') is None and candidate.get('high') is None:
        return False, 'no bound'
    unit = normalize_unit(candidate.get('sourceUnit'))
    if CATALOG[indicator].get('sourceUnits') and unit not in CATALOG[indicator]['sourceUnits']:
        return False, f'ratio basis {candidate.get("sourceUnit")!r} not stated as mmol/L or mg/dL'
    if unit not in CATALOG[indicator]['conversions']:
        return False, f'unit {candidate.get("sourceUnit")!r} has no conversion to {CATALOG[indicator]["unit"]!r}'
    if unit and unit not in {'%'}:
        window = normalize(quote)
        written = {normalize(candidate.get('sourceUnit') or ''), unit, unit.replace('μ', 'u'), unit.replace('μ', 'µ')}
        if not any(form and form in window for form in written):
            # A unit stated once in the same source paragraph still qualifies.
            index = corpus.find(normalize(quote))
            nearby = corpus[max(0, index - 600) : index + len(normalize(quote)) + 1200]
            if not any(form and form in nearby for form in written):
                return False, f'unit {candidate.get("sourceUnit")!r} not stated near the quote'
    if not document.get('title') or not document.get('url'):
        return False, 'source has no title or URL'
    return True, 'verified'


async def research(researcher: Researcher, candidate: dict) -> dict:
    indicator = candidate['indicator']
    catalog = CATALOG[indicator]
    documents: list[dict] = list(await researcher.pubmed_records(candidate.get('pmids', []), require_abstract=False))
    for query in candidate['pubmed']:
        documents.extend(await researcher.pubmed(query))
    for query in candidate['web']:
        documents.extend(await researcher.web(query))
    unique, seen = [], set()
    for document in documents:
        key = document.get('pmid') or document['url']
        if key in seen:
            continue
        seen.add(key)
        unique.append(document)
    # Only literature records and authoritative hosts are shown to the model.
    documents = [doc for doc in unique if doc['kind'] == 'pubmed' or authoritative(doc['url'])][:16]
    for document in documents:
        if document['kind'] == 'pubmed' and document.get('pmcid'):
            full = await researcher.full_text(document['pmcid'])
            if not full:
                # Some publishers withhold the XML; the PMC article page is the same full text.
                full = await researcher.page_text(f'https://pmc.ncbi.nlm.nih.gov/articles/{document["pmcid"]}/')
            if full:
                document['fullText'] = full
    documents = [doc for doc in documents if doc.get('text') or doc.get('fullText')]
    # Web pages of journal articles take their bibliographic data from PubMed (by DOI in the URL).
    for document in documents:
        if document['kind'] != 'web':
            continue
        doi = re.search(r'(10\.\d{4,9}/[^\s?#&]+?)(?:/full|/abstract|/pdf|\.pdf)?(?:$|[?#&])', document['url'])
        if not doi:
            continue
        found = await researcher._ncbi('esearch.fcgi', {'db': 'pubmed', 'term': f'{doi[1]}[aid]', 'retmode': 'json'})
        try:
            ids = json.loads(found)['esearchresult']['idlist']
        except (ValueError, KeyError):
            ids = []
        if len(ids) == 1 and (records := await researcher.pubmed_records(ids, require_abstract=False)):
            record = records[0]
            document.update(
                {key: record[key] for key in ('title', 'year', 'doi', 'pmid', 'journal', 'publicationTypes')}
            )
    keywords = [catalog['label'], *catalog['aliases'], *catalog.get('abbreviations', [])]
    rendered = []
    for index, document in enumerate(documents):
        rendered.append(
            f'[{index}] 类型={document["kind"]} 标题={document["title"]} '
            f'年份={document.get("year") or document.get("date")} 网址={document["url"]}\n'
            f'摘录：{_excerpt(document["text"], keywords)}'
            + (f'\n全文摘录：{_excerpt(document["fullText"], keywords, 6000)}' if document.get('fullText') else '')
        )
    prompt = PROMPT.format(
        label=catalog['label'],
        indicator=indicator,
        unit=catalog['unit'] or '无单位',
        conditions=', '.join(CONDITIONS),
        example=candidate.get('example') or '无',
        hint=('\n特别要求：' + hint) if (hint := candidate.get('hint') or HINTS.get(indicator)) else '',
        documents='\n\n'.join(rendered),
    )
    proposal = await researcher.ask(prompt)
    accepted, rejected = [], []
    for proposed in proposal.get('targets') or []:
        index = proposed.get('sourceIndex')
        if not isinstance(index, int) or not 0 <= index < len(documents):
            rejected.append({'proposal': proposed, 'reason': 'sourceIndex out of range'})
            continue
        document = documents[index]
        if document['kind'] == 'pubmed':
            page = document['text'] + '\n' + document.get('fullText', '')
        else:
            page = await researcher.page_text(document['url'])
        ok, reason = _verify(proposed, document, page, indicator)
        method = None
        if ok and document['kind'] == 'pubmed':
            method = 'pubmedAbstract' if normalize(proposed['quote']) in normalize(document['text']) else 'pmcFullText'
        elif ok:
            method = 'fetchedPage'
        if not ok and document['kind'] == 'web':
            # The search tool's extract of the same URL is also page text.
            ok, extract_reason = _verify(proposed, document, document['text'], indicator)
            method = 'searchExtract' if ok else None
            reason = reason if not ok and reason != 'quote not found verbatim in the source text' else extract_reason
        if not ok:
            rejected.append({'proposal': proposed, 'source': document['url'], 'reason': reason})
            continue
        if document['kind'] == 'web' and not authoritative(document['url']):
            rejected.append({'proposal': proposed, 'source': document['url'], 'reason': 'non-authoritative web host'})
            continue
        year = document.get('year')
        titled = [int(item) for item in re.findall(r'(?<!\d)(20[0-3]\d)(?!\d)', document.get('title') or '')]
        if year is None and titled:
            year = max(titled)
        if year is None and document.get('date'):
            year = int(str(document['date'])[:4]) if str(document['date'])[:4].isdigit() else None
        if year is None:
            found = re.findall(r'\b(19[89]\d|20[0-2]\d)\b', page[:3000] + document['text'][:3000])
            year = int(found[0]) if found else None
        if year is None:
            rejected.append({'proposal': proposed, 'source': document['url'], 'reason': 'publication year unknown'})
            continue
        accepted.append({'proposal': proposed, 'source': document, 'method': method, 'year': year})
    return {
        'indicator': indicator,
        'checkedAt': date.today().isoformat(),
        'documents': [
            {key: value for key, value in doc.items() if key not in {'text', 'fullText'}} for doc in documents
        ],
        'proposal': proposal,
        'accepted': [
            {**item, 'source': {key: value for key, value in item['source'].items() if key not in {'text', 'fullText'}}}
            for item in accepted
        ],
        'rejected': rejected,
    }


def _number(value: Decimal | None):
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def _target_text(low, high, low_inclusive, high_inclusive, unit) -> str:
    unit = f' {unit}' if unit else ''
    if low is not None and high is not None:
        if low_inclusive is False or high_inclusive is False:
            lower = f'{"≥" if low_inclusive else ">"}{_number(low)}'
            return f'{lower} 且 {"≤" if high_inclusive else "<"}{_number(high)}{unit}'
        return f'{_number(low)}–{_number(high)}{unit}'
    if high is not None:
        return f'{"≤" if high_inclusive else "<"}{_number(high)}{unit}'
    return f'{"≥" if low_inclusive else ">"}{_number(low)}{unit}'


def clean_title(title: str) -> str:
    """Drop the page snippet or site suffix a search engine appends after ' - ' or ' | '."""
    text = re.sub(r'\s+', ' ', str(title or '')).strip()
    text = re.split(r'\s+[-|–]\s+(?=[^-|–]*$)', text)[0] if re.search(r'\s[-|–]\s', text) else text
    return text.strip(' -|•').strip()


async def _resolve(researcher: 'Researcher', source: dict, year: int | None) -> dict | None:
    """PubMed record of a web article, by the DOI in its URL or by its exact title."""
    url = unquote(source.get('url') or '')
    doi = re.search(r'(10\.\d{4,9}/[^\s?#&]+?)(?:/full|/abstract|/pdf|\.pdf)?(?:$|[?#&])', url)
    terms = [f'{doi[1]}[aid]'] if doi else []
    title = clean_title(source.get('title'))
    if len(title) >= 20 and re.search(r'[A-Za-z]{4}', title):
        # Unquoted words: PubMed phrase search fails on punctuation such as '2.' or an em dash.
        plain = re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', ' ', unicodedata.normalize('NFKC', title))).strip()
        if year:
            terms.append(f'{plain}[ti] AND {year}:{year + 1}[dp]')
        terms.append(f'{plain}[ti]')
    for term in terms:
        found = await researcher._ncbi('esearch.fcgi', {'db': 'pubmed', 'term': term, 'retmode': 'json', 'retmax': 20})
        try:
            ids = json.loads(found)['esearchresult']['idlist']
        except (ValueError, KeyError):
            continue
        records = await researcher.pubmed_records(ids, require_abstract=False) if ids else []
        wanted = re.sub(r'\W+', '', unicodedata.normalize('NFKC', title)).casefold()
        if not term.endswith('[aid]'):
            records = [
                record
                for record in records
                if re.sub(r'\W+', '', unicodedata.normalize('NFKC', record['title'])).casefold() == wanted
            ]
        if year and len(records) > 1:
            records = [record for record in records if record['year'] in {year, year + 1}] or records
            records.sort(key=lambda record: -(record['year'] or 0))
        if len(records) == 1 or (records and not term.endswith('[aid]') and year):
            return records[0]
    return None


async def refresh_metadata(work: Path) -> None:
    """Complete titles, years and DOIs of cached web sources without any model call."""
    researcher = Researcher(os.environ.get('SHULAN_LLM_API_KEY') or 'unused', work)
    try:
        for path in sorted(work.glob('*.json')):
            if path.name.startswith('assessment_targets'):
                continue
            result = json.loads(path.read_text(encoding='utf-8'))
            changed = False
            for item in result.get('accepted', ()):
                source = item['source']
                if source.get('kind') != 'web':
                    continue
                cleaned = clean_title(source.get('title'))
                if cleaned != source.get('title'):
                    source['title'], changed = cleaned, True
                if source.get('pmid'):
                    continue
                record = await _resolve(researcher, source, item.get('year'))
                if record is not None:
                    source.update({key: record[key] for key in ('title', 'doi', 'pmid', 'journal', 'publicationTypes')})
                    item['year'] = record['year'] or item.get('year')
                    changed = True
            if changed:
                path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
                _log(work, f'metadata refreshed {result["indicator"]}')
    finally:
        await researcher.close()


def _curated(indicator: str, proposal: dict) -> tuple[dict | None, str | None]:
    """Apply EXCLUDED_INDICATORS and TIERS; returns (proposal or None, reason)."""
    if indicator in EXCLUDED_INDICATORS:
        return None, EXCLUDED_INDICATORS[indicator]
    for key in ('high', 'low'):
        value = proposal.get(key)
        if value is None:
            continue
        try:
            tier_key = (indicator, Decimal(str(value)))
        except InvalidOperation:
            continue
        for (code, bound), override in TIERS.items():
            if code == indicator and Decimal(str(bound)) == tier_key[1]:
                if override is None:
                    return None, '风险分层目标，报告无法判定受检者属于该层'
                return {**proposal, **override}, None
    return proposal, None


GUIDELINE_TITLE = re.compile(
    r'guideline|consensus|statement|standards? of|recommendation|position|diagnosis and classification|'
    r'report on dietary reference|指南|共识|标准|声明|建议',
    re.I,
)


def _document_level(source: dict, claimed: str | None) -> str:
    """The evidence level of the cited document itself, not of the work it quotes."""
    types = set(source.get('publicationTypes') or ())
    host = (urlsplit(source.get('url') or '').hostname or '').lower()
    if (
        types & {'Practice Guideline', 'Guideline', 'Consensus Development Conference'}
        or GUIDELINE_TITLE.search(source.get('title') or '')
        or host.endswith(('who.int', 'nhc.gov.cn'))
    ):
        return claimed if claimed in EVIDENCE_RANK else '指南'
    if types & {'Meta-Analysis', 'Systematic Review'}:
        return '系统综述'
    if 'Review' in types or re.search(r'review|综述', source.get('title') or '', re.I):
        return '综述'
    return '人群研究'


def _rank(entry: dict) -> tuple:
    method_rank = {'pubmedAbstract': 0, 'pmcFullText': 0, 'fetchedPage': 1, 'searchExtract': 2}
    preferred = PREFERRED_HOSTS.get(entry['indicator'], ())
    host = (urlsplit(entry['sources'][0].get('url') or '').hostname or '').lower()
    return (
        not any(host == item or host.endswith('.' + item) for item in preferred) if preferred else False,
        EVIDENCE_RANK.get(entry.get('evidenceLevel') or '', 9),
        method_rank.get(_as_list(entry['verification'])[0]['method'], 3),
        -int(entry['sources'][0]['year'] or 0),
    )


def _merge(lower: dict, upper: dict, sex) -> dict:
    """A range from a verified lower bound and a verified upper bound (two quotes, both sources kept)."""
    merged = {**upper}
    merged['low'], merged['lowInclusive'] = lower['low'], lower['lowInclusive']
    merged['conditions'] = {**upper['conditions'], 'sex': sex}
    merged['basis'] = '；'.join(dict.fromkeys(filter(None, (lower.get('basis'), upper.get('basis')))))
    merged['sources'] = [*upper['sources'], *(item for item in lower['sources'] if item not in upper['sources'])]
    merged['verification'] = [*_as_list(upper['verification']), *_as_list(lower['verification'])]
    merged['evidenceLevel'] = min(
        (lower.get('evidenceLevel'), upper.get('evidenceLevel')), key=lambda level: EVIDENCE_RANK.get(level or '', 9)
    )
    merged['text'] = _target_text(
        Decimal(str(merged['low'])),
        Decimal(str(merged['high'])),
        merged['lowInclusive'],
        merged['highInclusive'],
        merged['unit'],
    )
    return merged


def _as_list(value):
    return value if isinstance(value, list) else [value]


def _select(targets: list[dict], dropped: list[dict]) -> list[dict]:
    """One entry per indicator and population: best range, or best lower plus best upper merged."""
    groups: dict[tuple, dict[str, list]] = {}
    for entry in targets:
        conditions = entry['conditions']
        key = (
            entry['indicator'],
            conditions['sex'],
            tuple(sorted(conditions['requiresConditions'])),
            conditions['minAge'],
            conditions['maxAge'],
        )
        shape = (
            'range'
            if entry['low'] is not None and entry['high'] is not None
            else ('upper' if entry['high'] is not None else 'lower')
        )
        groups.setdefault(key, {}).setdefault(shape, []).append(entry)
    chosen: dict[tuple, dict] = {}
    for key, shapes in groups.items():
        best = {shape: sorted(items, key=_rank) for shape, items in shapes.items()}
        if 'range' in best:
            entry = best['range'][0]
            losers = [item for items in best.values() for item in items if item is not entry]
        elif 'upper' in best and 'lower' in best:
            entry = _merge(best['lower'][0], best['upper'][0], key[1])
            losers = best['upper'][1:] + best['lower'][1:]
        else:
            items = next(iter(best.values()))
            entry, losers = items[0], items[1:]
        chosen[key] = entry
        for item in losers:
            dropped.append(
                {
                    'indicator': item['indicator'],
                    'value': item['text'],
                    'source': item['sources'][0]['title'],
                    'reason': '同一指标已有证据等级更高或更新的出处',
                }
            )
    # A sex-neutral lower bound completes each sex-specific upper bound of the same indicator.
    for key, entry in list(chosen.items()):
        indicator, sex, requires, min_age, max_age = key
        neutral = chosen.get((indicator, None, requires, min_age, max_age))
        if (
            sex
            and neutral is not None
            and entry['low'] is None
            and neutral['high'] is None
            and neutral['low'] is not None
        ):
            chosen[key] = _merge(neutral, entry, sex)
            chosen[(indicator, None, requires, min_age, max_age)]['_mergedInto'] = True
    result = []
    for key, entry in chosen.items():
        if entry.pop('_mergedInto', False):
            sexes = {other[1] for other in chosen if other[0] == key[0] and other[2:] == key[2:] and other[1]}
            if sexes >= {'male', 'female'}:
                continue
        result.append(entry)
    order = {candidate['indicator']: index for index, candidate in enumerate(CANDIDATES)}
    result.sort(
        key=lambda entry: (
            order[entry['indicator']],
            entry['conditions']['sex'] or '',
            entry['conditions']['minAge'] or 0,
        )
    )
    for index, entry in enumerate(result):
        same = [item for item in result[:index] if item['indicator'] == entry['indicator']]
        entry['id'] = f'{entry["indicator"]}-{len(same) + 1}'
    return result


def assemble(work: Path, output: Path) -> dict:
    targets, not_adopted, verified_not_adopted = [], [], []
    for candidate in CANDIDATES:
        indicator = candidate['indicator']
        path = work / f'{indicator}.json'
        if not path.exists():
            continue
        result = json.loads(path.read_text(encoding='utf-8'))
        catalog = CATALOG[indicator]
        for position, item in enumerate(result['accepted']):
            proposal, source = item['proposal'], item['source']
            if proposal.get('kind') == 'upperLimit':
                proposal = {**proposal, 'low': None}
            elif proposal.get('kind') == 'lowerLimit':
                proposal = {**proposal, 'high': None}
            proposal, reason = _curated(indicator, proposal)
            if proposal is None:
                verified_not_adopted.append(
                    {
                        'indicator': indicator,
                        'value': item['proposal'].get('high') or item['proposal'].get('low'),
                        'source': source.get('title'),
                        'reason': reason,
                    }
                )
                continue
            low = _convert(proposal.get('low'), proposal.get('sourceUnit') or '', indicator)
            high = _convert(proposal.get('high'), proposal.get('sourceUnit') or '', indicator)
            defaults = candidate.get('defaults', {})
            sex = proposal.get('sex') if proposal.get('sex') in {'male', 'female'} else None
            exclude = sorted(
                {
                    *defaults.get('excludeConditions', ()),
                    *(c for c in proposal.get('excludeConditions') or () if c in CONDITIONS),
                }
                - set(proposal.get('requiresConditions') or ())
            )
            entry = {
                'id': f'{indicator}-{position + 1}',
                'indicator': indicator,
                'label': catalog['label'],
                'aliases': catalog['aliases'],
                'abbreviations': catalog.get('abbreviations', []),
                'unit': catalog['unit'],
                'unitConversions': {key: value for key, value in catalog['conversions'].items()},
                'low': _number(low),
                'high': _number(high),
                'lowInclusive': bool(proposal.get('lowInclusive')) if low is not None else None,
                'highInclusive': bool(proposal.get('highInclusive')) if high is not None else None,
                'text': _target_text(
                    low, high, proposal.get('lowInclusive'), proposal.get('highInclusive'), catalog['unit']
                ),
                'riskTier': proposal.get('riskTier'),
                'conditions': {
                    'sex': sex,
                    'minAge': max(18, proposal['minAge'])
                    if isinstance(proposal.get('minAge'), int)
                    else defaults.get('minAge'),
                    'maxAge': proposal.get('maxAge') if isinstance(proposal.get('maxAge'), int) else None,
                    'nonPregnant': bool(proposal.get('nonPregnant') or defaults.get('nonPregnant')),
                    'excludeConditions': exclude,
                    'requiresConditions': [c for c in proposal.get('requiresConditions') or () if c in CONDITIONS],
                },
                'basis': proposal.get('basis'),
                'evidenceLevel': _document_level(source, proposal.get('evidenceLevel')),
                'sources': [
                    {
                        'title': source.get('title'),
                        'year': item['year'],
                        'doi': source.get('doi'),
                        'pmid': source.get('pmid'),
                        'journal': source.get('journal'),
                        'url': source.get('url'),
                    }
                ],
                'verification': {
                    'method': item['method'],
                    'quote': proposal.get('quote'),
                    'sourceValue': {
                        'low': proposal.get('low'),
                        'high': proposal.get('high'),
                        'unit': proposal.get('sourceUnit'),
                    },
                    'checkedAt': result['checkedAt'],
                },
            }
            targets.append(entry)
        if candidate.get('example'):
            not_adopted.append({'indicator': indicator, 'exampleValue': candidate['example']})
    targets = _select(targets, verified_not_adopted)
    # The sample report's unsourced numbers are adopted only if a verified entry states the same bounds.
    for item in not_adopted:
        example = item['exampleValue']
        sex = 'male' if '男' in example else 'female' if '女' in example else None
        wanted = set(numbers_in(example))
        entries = [
            entry
            for entry in targets
            if entry['indicator'] == item['indicator'] and (sex is None or entry['conditions']['sex'] in {None, sex})
        ]
        match = next(
            (
                entry
                for entry in entries
                if wanted == {Decimal(str(value)) for value in (entry['low'], entry['high']) if value is not None}
            ),
            None,
        )
        if match is not None:
            required = '、'.join(CONDITIONS[code] for code in match['conditions']['requiresConditions'])
            reason = '与已核对出处的界值一致' + (f'，但仅适用于{required}人群' if required else '')
            item.update(status='adopted', targetId=match['id'], reason=reason)
        elif entries:
            item.update(
                status='notAdopted',
                reason='已核对出处给出的界值为 '
                + '；'.join(entry['text'] for entry in entries)
                + '，示例数值无出处，不采用',
            )
        else:
            item.update(status='notAdopted', reason='未找到给出该数值的指南、共识或高质量研究，不入表')
    table = {
        'version': date.today().isoformat(),
        'generatedBy': 'scripts/build_assessment_targets.py',
        'model': MODEL,
        'note': '目标区间仅在条件适用时显示；不适用或无条目时只显示临床参考区间。每条出处已由程序核对原文数值。',
        'conditions': CONDITIONS,
        'indicators': CATALOG,
        'targets': targets,
        'exampleValues': not_adopted,
        'verifiedNotAdopted': verified_not_adopted,
    }
    output.write_text(json.dumps(table, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'targets': len(targets), 'indicators': len({item['indicator'] for item in targets})}


async def run(args) -> None:
    work = Path(args.work_dir).expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    api_key = os.environ.get('SHULAN_DEEPSEEK_TOOLS_API_KEY') or os.environ.get('SHULAN_LLM_API_KEY')
    if not api_key:
        raise SystemExit('SHULAN_LLM_API_KEY (DeepSeek) is not set')
    researcher = Researcher(api_key, work)
    selected = [c for c in CANDIDATES if not args.only or c['indicator'] in args.only]
    semaphore = asyncio.Semaphore(args.concurrency)

    async def one(candidate):
        path = work / f'{candidate["indicator"]}.json'
        if path.exists() and not args.force:
            return
        async with semaphore:
            _log(work, f'start {candidate["indicator"]}')
            result = await research(researcher, candidate)
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
            temporary.replace(path)
            _log(
                work,
                f'done {candidate["indicator"]} accepted={len(result["accepted"])} rejected={len(result["rejected"])}',
            )

    try:
        await asyncio.gather(*(one(candidate) for candidate in selected))
    finally:
        await researcher.close()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--work-dir', required=True, help='cache directory outside the repository')
    parser.add_argument('--only', nargs='*', default=None, help='indicator codes to (re)build')
    parser.add_argument('--force', action='store_true', help='rebuild cached indicators')
    parser.add_argument('--concurrency', type=int, default=3)
    parser.add_argument('--assemble', action='store_true', help='only rebuild the table from the cache')
    parser.add_argument('--refresh-metadata', action='store_true', help='complete cached web sources from PubMed')
    parser.add_argument('--output', default=str(OUTPUT))
    args = parser.parse_args(argv)
    if args.refresh_metadata:
        asyncio.run(refresh_metadata(Path(args.work_dir).expanduser().resolve()))
    elif not args.assemble:
        asyncio.run(run(args))
    summary = assemble(Path(args.work_dir).expanduser().resolve(), Path(args.output))
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
