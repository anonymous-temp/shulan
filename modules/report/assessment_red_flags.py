"""Danger-signal rules for the six-chapter assessment (spec section 4).

P0 ("优先医疗排查") is granted only by the rules in this module. Every rule is a
row of data: a trigger (analyte threshold, qualitative result, imaging grade,
ECG pattern or a sized imaging finding), applicability (sex, age, menopause),
a timeframe, a department, the checks to complete and its sources. Original
advice that explicitly asks for prompt/urgent care is a supplementary source,
never the only path to P0.

The same module derives routine specialist follow-ups (nodules, polyps, cysts,
H. pylori, mildly raised tumour markers ...). Intervals come from the original
advice when it names one; otherwise the rule's default interval is used and
labelled ``basis='rule_default'``.

All fact identifiers are the caller's identifiers; this module never invents
measurements. Rules assume adult subjects.
"""

from __future__ import annotations

import re
import unicodedata
from copy import deepcopy

RULES_VERSION = 'assessment-red-flags.v1'

# ---------------------------------------------------------------------------
# Sources. Each rule cites keys from this table; ``supports`` says what the
# source establishes for the rule. Program defaults are labelled separately.
# ---------------------------------------------------------------------------
SOURCES = {
    'cmab2019': {
        'title': '健康体检重要异常结果管理专家共识（试行版）',
        'publisher': '中华医学会健康管理学分会；《中华健康管理学杂志》编辑委员会',
        'citation': '中华健康管理学杂志, 2019, 13(2): 97-101',
        'doi': '10.3760/cma.j.issn.1674-0815.2019.02.001',
        'year': 2019,
    },
    'kost1990': {
        'title': 'Critical limits for urgent clinician notification at US medical centers',
        'publisher': 'Kost GJ',
        'citation': 'JAMA, 1990, 263(5): 704-707',
        'doi': '10.1001/jama.263.5.704',
        'year': 1990,
    },
    'eje2014_hyponatraemia': {
        'title': 'Clinical practice guideline on diagnosis and treatment of hyponatraemia',
        'publisher': 'Spasovski G, et al.',
        'citation': 'Eur J Endocrinol, 2014, 170(3): G1-G47',
        'doi': '10.1530/EJE-13-1020',
        'year': 2014,
    },
    'internal_medicine_9': {
        'title': '内科学（第9版）：贫血严重程度划分',
        'publisher': '葛均波, 徐永健, 王辰 主编. 人民卫生出版社',
        'citation': '北京: 人民卫生出版社, 2018',
        'year': 2018,
        'note': '国内通行分级：血红蛋白 >90 g/L 且低于正常下限为轻度、60–90 g/L 为中度；本次核对依据为二手转述',
    },
    'ckd_k_cn2020': {
        'title': '中国慢性肾脏病患者血钾管理实践专家共识',
        'publisher': '中华医学会肾脏病学分会专家组',
        'citation': '中华肾脏病杂志, 2020, 36(10): 781-792',
        'doi': '10.3760/cma.j.cn441217-20200721-00139',
        'year': 2020,
        'note': '表 1 低钾血症分度：轻度 3.0~3.4、中度 2.5~2.9、重度 <2.5 mmol/L',
    },
    'ckd_hyperk_cn2020': {
        'title': '慢性肾脏病高钾血症风险评估及管理专家建议（2020版）',
        'publisher': '中国医师协会肾脏内科医师分会等',
        'citation': '中华医学杂志, 2020, 100(44): 3489-3493',
        'year': 2020,
        'note': '高钾血症分级：轻度 5.0-5.5、中度 >5.5-6.0、重度 >6.0 mmol/L',
    },
    'bsg2021_ida': {
        'title': 'British Society of Gastroenterology guidelines for the management of iron deficiency '
        'anaemia in adults',
        'publisher': 'Snook J, et al.',
        'citation': 'Gut, 2021, 70(11): 2030-2051',
        'doi': '10.1136/gutjnl-2021-325210',
        'year': 2021,
    },
    'crc_cn2020': {
        'title': '中国结直肠癌筛查与早诊早治指南（2020，北京）',
        'publisher': '国家癌症中心中国结直肠癌筛查与早诊早治指南制定专家组',
        'citation': '中华肿瘤杂志, 2021, 43(1): 16-38',
        'year': 2021,
    },
    'kdigo2024_ckd': {
        'title': 'KDIGO 2024 Clinical Practice Guideline for the Evaluation and Management of Chronic Kidney Disease',
        'publisher': 'Kidney Disease: Improving Global Outcomes (KDIGO) CKD Work Group',
        'citation': 'Kidney Int, 2024, 105(4S): S117-S314',
        'doi': '10.1016/j.kint.2023.10.018',
        'year': 2024,
    },
    'esc2024_af': {
        'title': '2024 ESC Guidelines for the management of atrial fibrillation',
        'publisher': 'Van Gelder IC, et al.',
        'citation': 'Eur Heart J, 2024, 45(36): 3314-3414',
        'doi': '10.1093/eurheartj/ehae176',
        'year': 2024,
    },
    'esc2023_acs': {
        'title': '2023 ESC Guidelines for the management of acute coronary syndromes',
        'publisher': 'Byrne RA, et al.',
        'citation': 'Eur Heart J, 2023, 44(38): 3720-3826',
        'doi': '10.1093/eurheartj/ehad191',
        'year': 2023,
    },
    'acc2018_brady': {
        'title': '2018 ACC/AHA/HRS Guideline on the Evaluation and Management of Patients With Bradycardia '
        'and Cardiac Conduction Delay',
        'publisher': 'Kusumoto FM, et al.',
        'citation': 'Circulation, 2019, 140(8): e382-e482',
        'doi': '10.1161/CIR.0000000000000628',
        'year': 2019,
    },
    'aha2010_qt': {
        'title': 'Prevention of torsade de pointes in hospital settings: a scientific statement from the '
        'American Heart Association and the American College of Cardiology Foundation',
        'publisher': 'Drew BJ, et al.',
        'citation': 'Circulation, 2010, 121(8): 1047-1060',
        'doi': '10.1161/CIRCULATIONAHA.109.192704',
        'year': 2010,
    },
    'acr_birads2013': {
        'title': 'ACR BI-RADS Atlas, Breast Imaging Reporting and Data System, 5th edition',
        'publisher': 'American College of Radiology',
        'citation': 'Reston, VA: American College of Radiology, 2013',
        'year': 2013,
    },
    'acr_tirads2017': {
        'title': 'ACR Thyroid Imaging, Reporting and Data System (TI-RADS): White Paper of the ACR TI-RADS Committee',
        'publisher': 'Tessler FN, et al.',
        'citation': 'J Am Coll Radiol, 2017, 14(5): 587-595',
        'doi': '10.1016/j.jacr.2017.01.046',
        'year': 2017,
    },
    'c_tirads2020': {
        'title': '2020 甲状腺结节超声恶性危险分层中国指南：C-TIRADS',
        'publisher': '中华医学会超声医学分会浅表器官和血管学组等',
        'citation': '中华超声影像学杂志, 2021, 30(3): 185-200',
        'year': 2021,
    },
    'acr_lungrads2022': {
        'title': 'Lung CT Screening Reporting & Data System (Lung-RADS) v2022',
        'publisher': 'American College of Radiology',
        'citation': 'American College of Radiology, 2022',
        'year': 2022,
    },
    'fleischner2017': {
        'title': 'Guidelines for Management of Incidental Pulmonary Nodules Detected on CT Images: '
        'From the Fleischner Society 2017',
        'publisher': 'MacMahon H, et al.',
        'citation': 'Radiology, 2017, 284(1): 228-243',
        'doi': '10.1148/radiol.2017161659',
        'year': 2017,
    },
    'acog2016_adnexal': {
        'title': 'Practice Bulletin No. 174: Evaluation and Management of Adnexal Masses',
        'publisher': 'American College of Obstetricians and Gynecologists',
        'citation': 'Obstet Gynecol, 2016, 128(5): e210-e226',
        'doi': '10.1097/AOG.0000000000001768',
        'year': 2016,
        'note': '绝经前“CA125 显著升高”为转诊标准之一；原文说明以往 ACOG 指导采用 >200 U/mL，属专家意见',
    },
    'psa_cn2022': {
        'title': '中国前列腺癌筛查与早诊早治指南（2022，北京）',
        'publisher': '国家癌症中心中国前列腺癌筛查与早诊早治指南制定专家组',
        'citation': '中华肿瘤杂志, 2022, 44(1): 29-53',
        'doi': '10.3760/cma.j.cn112152-20211226-00975',
        'year': 2022,
        'note': 'PSA 4 ng/mL 为临界水平；指南的筛查对象为高风险人群',
    },
    'hp_cn2022': {
        'title': '第六次全国幽门螺杆菌感染处理共识报告（非根除治疗部分）',
        'publisher': '中华医学会消化病学分会幽门螺杆菌学组',
        'citation': '中华消化杂志, 2022, 42(5): 289-303',
        'doi': '10.3760/cma.j.cn311367-20220206-00057',
        'year': 2022,
        'note': '证实幽门螺杆菌感染且无抗衡因素者均需接受根除治疗',
    },
    'esgar2022_gb_polyp': {
        'title': 'Management and follow-up of gallbladder polyps: updated joint guidelines between the ESGAR, '
        'EAES, EFISDS and ESGE',
        'publisher': 'Foley KG, et al.',
        'citation': 'Eur Radiol, 2022, 32(5): 3358-3368',
        'doi': '10.1007/s00330-021-08384-w',
        'year': 2022,
    },
    'easl2022_liver_cyst': {
        'title': 'EASL Clinical Practice Guidelines on the management of cystic liver diseases',
        'publisher': 'European Association for the Study of the Liver',
        'citation': 'J Hepatol, 2022, 77(4): 1083-1108',
        'doi': '10.1016/j.jhep.2022.06.002',
        'year': 2022,
    },
    'bosniak2019': {
        'title': 'Bosniak Classification of Cystic Renal Masses, Version 2019: An Update Proposal and Needs Assessment',
        'publisher': 'Silverman SG, et al.',
        'citation': 'Radiology, 2019, 292(2): 475-488',
        'doi': '10.1148/radiol.2019182646',
        'year': 2019,
    },
}

# What a program default is, stated once: no guideline gives the exact days.
PROGRAM_DEFAULT = {
    'title': '本规则表的操作时限',
    'publisher': '程序默认',
    'citation': (
        '来源只规定“及时/尽早”处理而未给出天数时，按危急（当日）、明显异常（1 周）、需进一步检查（1 个月）三档取值'
    ),
}

_IMMEDIATE = {'text': '立即（当日）就医', 'days': 0}
_ONE_WEEK = {'text': '1 周内就诊', 'days': 7}
_TWO_WEEKS = {'text': '2 周内就诊', 'days': 14}
_ONE_MONTH = {'text': '1 个月内就诊', 'days': 30}

# ---------------------------------------------------------------------------
# Analytes: how a structured laboratory row is recognised. ``units`` maps a
# normalised unit to the factor that converts it to the canonical unit.
# ---------------------------------------------------------------------------
ANALYTES = {
    'hemoglobin': {
        'include': r'血红蛋白|^\s*(?:HGB|Hb)\b',
        'exclude': r'糖化|平均|浓度|含量|分布|尿|粪|便|电泳|A1c|HbA',
        'unit': 'g/L',
        'units': {'g/l': 1, 'g/dl': 10},
    },
    'mcv': {'include': r'平均红细胞体积|\bMCV\b', 'exclude': r'分布', 'unit': 'fL', 'units': {'fl': 1}},
    'ferritin': {
        'include': r'铁蛋白|ferritin',
        'exclude': r'',
        'unit': 'ng/mL',
        'units': {'ng/ml': 1, 'μg/l': 1, 'ug/l': 1},
    },
    'wbc': {
        'include': r'白细胞计数|^\s*WBC\b',
        'exclude': r'尿|粪|便|镜检|酯酶|分类',
        'unit': '10^9/L',
        'units': {'10^9/l': 1, '10e9/l': 1, '×10^9/l': 1, '10*9/l': 1},
    },
    'platelet': {
        'include': r'血小板计数|^\s*PLT\b',
        'exclude': r'平均|分布|压积|比率',
        'unit': '10^9/L',
        'units': {'10^9/l': 1, '10e9/l': 1, '×10^9/l': 1, '10*9/l': 1},
    },
    'potassium': {
        'include': r'钾|^\s*K\b|K\+',
        'exclude': r'尿|钠钾|比值',
        'unit': 'mmol/L',
        'units': {'mmol/l': 1, 'meq/l': 1},
    },
    'sodium': {
        'include': r'钠|^\s*Na\b|Na\+',
        'exclude': r'尿|钠尿肽|BNP|比值',
        'unit': 'mmol/L',
        'units': {'mmol/l': 1, 'meq/l': 1},
    },
    'fasting_glucose': {
        'include': r'空腹(?:血)?(?:葡萄)?糖|^\s*(?:血)?葡萄糖|^\s*血糖|\bGLU\b',
        'exclude': r'尿|糖化|餐后|OGTT|耐量|随机|2\s*h|小时|胰岛素|C肽|蛋白',
        'unit': 'mmol/L',
        'units': {'mmol/l': 1, 'mg/dl': 1 / 18.0},
    },
    'random_glucose': {
        'include': r'随机(?:血)?(?:葡萄)?糖',
        'exclude': r'尿',
        'unit': 'mmol/L',
        'units': {'mmol/l': 1, 'mg/dl': 1 / 18.0},
    },
    'creatinine': {
        'include': r'肌酐|\bCREA?\b|^\s*Cr\b',
        'exclude': r'尿|清除率|比|/|白蛋白|胱抑素',
        'unit': 'μmol/L',
        'units': {'μmol/l': 1, 'umol/l': 1, 'µmol/l': 1, 'mg/dl': 88.4},
    },
    'egfr': {
        'include': r'肾小球滤过率|eGFR|\bGFR\b',
        'exclude': r'',
        'unit': 'mL/min/1.73m2',
        'units': {'ml/min/1.73m2': 1, 'ml/min/1.73m^2': 1, 'ml/min': 1, 'ml/(min·1.73m2)': 1},
    },
    'alt': {
        'include': r'丙氨酸(?:氨基)?转移酶|谷丙转氨酶|\bALT\b|\bGPT\b',
        'exclude': r'/|比',
        'unit': 'U/L',
        'units': {'u/l': 1, 'iu/l': 1},
    },
    'ast': {
        'include': r'天门?冬氨酸(?:氨基)?转移酶|谷草转氨酶|\bAST\b|\bGOT\b',
        'exclude': r'/|比|同工酶|线粒体',
        'unit': 'U/L',
        'units': {'u/l': 1, 'iu/l': 1},
    },
    'total_bilirubin': {
        'include': r'总胆红素|T-?BIL',
        'exclude': r'',
        'unit': 'μmol/L',
        'units': {'μmol/l': 1, 'umol/l': 1, 'µmol/l': 1, 'mg/dl': 17.1},
    },
    'heart_rate': {
        'include': r'^\s*(?:心率|脉搏)',
        'exclude': r'变异',
        'unit': '次/分',
        'units': {'次/分': 1, 'bpm': 1, '次/min': 1, '/min': 1},
    },
    'sbp': {'include': r'收缩压', 'exclude': r'', 'unit': 'mmHg', 'units': {'mmhg': 1}},
    'dbp': {'include': r'舒张压', 'exclude': r'', 'unit': 'mmHg', 'units': {'mmhg': 1}},
    'afp': {
        'include': r'甲胎蛋白|\bAFP\b',
        'exclude': r'异质体|L3',
        'unit': 'ng/mL',
        'units': {'ng/ml': 1, 'μg/l': 1, 'ug/l': 1, 'iu/ml': 1.21},
    },
    'cea': {
        'include': r'癌胚抗原|\bCEA\b',
        'exclude': r'',
        'unit': 'ng/mL',
        'units': {'ng/ml': 1, 'μg/l': 1, 'ug/l': 1},
        'default_uln': 5.0,
    },
    'ca199': {
        'include': r'CA\s*19\s*-?\s*9|(?:糖链|糖类)抗原\s*19\s*-?\s*9',
        'exclude': r'',
        'unit': 'U/mL',
        'units': {'u/ml': 1, 'ku/l': 1, 'iu/ml': 1},
        'default_uln': 37.0,
    },
    'ca125': {
        'include': r'CA\s*-?\s*125|(?:糖链|糖类)抗原\s*125',
        'exclude': r'',
        'unit': 'U/mL',
        'units': {'u/ml': 1, 'ku/l': 1, 'iu/ml': 1},
        'default_uln': 35.0,
    },
    'ca724': {
        'include': r'CA\s*72\s*-?\s*4|(?:糖链|糖类)抗原\s*72\s*-?\s*4',
        'exclude': r'',
        'unit': 'U/mL',
        'units': {'u/ml': 1, 'ku/l': 1, 'iu/ml': 1},
    },
    'ca153': {
        'include': r'CA\s*15\s*-?\s*3|(?:糖链|糖类)抗原\s*15\s*-?\s*3',
        'exclude': r'',
        'unit': 'U/mL',
        'units': {'u/ml': 1, 'ku/l': 1, 'iu/ml': 1},
    },
    'ca242': {
        'include': r'CA\s*-?\s*242|(?:糖链|糖类)抗原\s*242',
        'exclude': r'',
        'unit': 'U/mL',
        'units': {'u/ml': 1, 'ku/l': 1, 'iu/ml': 1},
    },
    'ca50': {
        'include': r'CA\s*-?\s*50(?!\d)|(?:糖链|糖类)抗原\s*50(?!\d)',
        'exclude': r'',
        'unit': 'U/mL',
        'units': {'u/ml': 1, 'ku/l': 1, 'iu/ml': 1},
    },
    'cyfra211': {
        'include': r'CYFRA\s*21\s*-?\s*1|细胞角蛋白\s*19',
        'exclude': r'',
        'unit': 'ng/mL',
        'units': {'ng/ml': 1, 'μg/l': 1, 'ug/l': 1},
    },
    'nse': {
        'include': r'神经元特异性烯醇化酶|\bNSE\b',
        'exclude': r'',
        'unit': 'ng/mL',
        'units': {'ng/ml': 1, 'μg/l': 1, 'ug/l': 1},
    },
    'scc': {
        'include': r'鳞状上皮细胞癌抗原|鳞状细胞癌(?:相关)?抗原|\bSCC(?:-?Ag)?\b',
        'exclude': r'',
        'unit': 'ng/mL',
        'units': {'ng/ml': 1, 'μg/l': 1, 'ug/l': 1},
    },
    'progrp': {'include': r'胃泌素释放肽前体|ProGRP', 'exclude': r'', 'unit': 'pg/mL', 'units': {'pg/ml': 1}},
    'tpsa': {
        'include': r'前列腺特异性抗原|\bt?PSA\b|\bTPSA\b',
        'exclude': r'游离|f-?PSA|FPSA|比值|/|密度',
        'unit': 'ng/mL',
        'units': {'ng/ml': 1, 'μg/l': 1, 'ug/l': 1},
    },
    'fpsa': {
        'include': r'游离前列腺特异性抗原|f-?PSA|FPSA',
        'exclude': r'比值|/|%',
        'unit': 'ng/mL',
        'units': {'ng/ml': 1, 'μg/l': 1, 'ug/l': 1},
    },
    'fobt': {
        'include': r'(?:粪|大便|便)[^，,。]{0,6}(?:隐血|潜血)|^\s*(?:隐血|潜血)(?:试验)?\s*$|\bFOBT\b|\bFIT\b|'
        r'(?:粪|大便|便).{0,4}\bOB\b|^\s*OB\b',
        'exclude': r'尿',
        'qualitative': True,
    },
    'urine_protein': {'include': r'尿(?:液)?蛋白|^\s*PRO\b', 'exclude': r'定量|白蛋白|/|肌酐|24', 'qualitative': True},
    'urine_blood': {'include': r'尿(?:液)?(?:隐血|潜血)|^\s*(?:BLD|ERY)\b', 'exclude': r'', 'qualitative': True},
    'urine_ketone': {'include': r'尿(?:液)?酮体|^\s*KET\b', 'exclude': r'', 'qualitative': True},
    'hp': {'include': r'幽门螺(?:旋|杆)菌|\bHp\b|\bH\.?\s*pylori|呼气试验|尿素酶', 'exclude': r'', 'qualitative': True},
    # Analytes used by the test-gap rules (assessment_test_gaps).
    'rdw': {'include': r'红细胞分布宽度|\bRDW', 'exclude': r'', 'unit': '%', 'units': {'%': 1, 'fl': 1}},
    'total_cholesterol': {
        'include': r'总胆固醇|^\s*TC\b|\bCHOL\b',
        'exclude': r'非|残余|/|比|高密度|低密度',
        'unit': 'mmol/L',
        'units': {'mmol/l': 1, 'mg/dl': 1 / 38.67},
    },
    'triglycerides': {
        'include': r'甘油三酯|三酰甘油|^\s*TG\b',
        'exclude': r'/|比',
        'unit': 'mmol/L',
        'units': {'mmol/l': 1, 'mg/dl': 1 / 88.57},
    },
    'ldl_c': {
        'include': r'低密度脂蛋白|LDL',
        'exclude': r'/|比|非|non',
        'unit': 'mmol/L',
        'units': {'mmol/l': 1, 'mg/dl': 1 / 38.67},
    },
    'hdl_c': {
        'include': r'高密度脂蛋白|HDL',
        'exclude': r'/|比|非|non',
        'unit': 'mmol/L',
        'units': {'mmol/l': 1, 'mg/dl': 1 / 38.67},
    },
    'tsh': {
        'include': r'促甲状腺(?:激)?素|\bTSH\b',
        'exclude': r'受体|抗体|TRAb|释放',
        'unit': 'mIU/L',
        'units': {'miu/l': 1, 'μiu/ml': 1, 'uiu/ml': 1, 'miu/ml': 1000},
    },
    'hba1c': {'include': r'糖化血红蛋白|HbA1c', 'exclude': r'', 'unit': '%', 'units': {'%': 1}},
    'fib4': {'include': r'FIB\s*-?\s*4', 'exclude': r'', 'unit': '', 'units': {}},
    'ggt': {
        'include': r'谷氨酰(?:基)?转(?:移|肽)酶|\bGGT\b|γ-?GT',
        'exclude': r'',
        'unit': 'U/L',
        'units': {'u/l': 1, 'iu/l': 1},
    },
    'bmi': {'include': r'体重指数|\bBMI\b', 'exclude': r'', 'unit': 'kg/m2', 'units': {'kg/m2': 1, 'kg/m²': 1}},
}

# Tumour markers judged against "2 x upper reference" (cmab2019: 其余肿瘤标志物
# 建议参考标准为≥2倍并结合其他检查结果). Department and checks per marker.
_MARKER_PLAN = {
    'cea': ('消化内科', ['复查癌胚抗原', '胃肠镜及胸腹部影像检查（由专科评估）'], '癌胚抗原（CEA）'),
    'ca199': ('消化内科', ['复查 CA19-9', '肝胆胰影像检查（由专科评估）'], '糖类抗原 CA19-9'),
    'ca724': ('消化内科', ['复查 CA72-4', '胃镜检查（由专科评估）'], '糖类抗原 CA72-4'),
    'ca242': ('消化内科', ['复查 CA242', '胰腺及胃肠检查（由专科评估）'], '糖类抗原 CA242'),
    'ca50': ('消化内科', ['复查 CA50', '胃肠及胰腺检查（由专科评估）'], '糖类抗原 CA50'),
    'ca153': ('乳腺外科', ['复查 CA15-3', '乳腺超声或钼靶检查'], '糖类抗原 CA15-3'),
    'cyfra211': ('呼吸内科', ['复查 CYFRA21-1', '胸部 CT 检查'], '细胞角蛋白 19 片段（CYFRA21-1）'),
    'nse': ('呼吸内科', ['复查 NSE（注意溶血干扰）', '胸部 CT 检查'], '神经元特异性烯醇化酶（NSE）'),
    'scc': ('呼吸内科', ['复查 SCC', '胸部 CT 及相关部位检查（女性同时妇科评估）'], '鳞状细胞癌相关抗原（SCC）'),
    'progrp': ('呼吸内科', ['复查 ProGRP', '胸部 CT 检查'], '胃泌素释放肽前体（ProGRP）'),
}


def _marker_rules():
    rules = []
    for analyte, (department, checks, title) in _MARKER_PLAN.items():
        rules.append(
            {
                'id': f'RF-TM-{analyte.upper()}-2XULN',
                'tier': 'P0',
                'group': 'marker:' + analyte,
                'title': title + '明显升高（≥2 倍参考上限）',
                'trigger': {'type': 'numeric', 'analyte': analyte, 'op': '>=', 'uln': 2.0},
                'timeframe': _TWO_WEEKS,
                'department': department,
                'checks': checks,
                'sources': ['cmab2019'],
                'topic': analyte,
            }
        )
        rules.append(
            {
                'id': f'RT-TM-{analyte.upper()}-MILD',
                'tier': 'routine',
                'group': 'marker:' + analyte,
                'title': title + '轻度升高',
                'trigger': {'type': 'numeric', 'analyte': analyte, 'op': '>', 'uln': 1.0},
                'interval': {'text': '1–3 个月内复查', 'days': 90},
                'department': department,
                'sources': ['cmab2019'],
                'topic': analyte,
            }
        )
    return rules


# ---------------------------------------------------------------------------
# Rule table. ``tier`` is 'P0' (priority medical work-up) or 'routine'.
# Rules sharing a ``group`` collapse to the most urgent match.
# ---------------------------------------------------------------------------
RULES = (
    # --- stool, GI -----------------------------------------------------------
    {
        'id': 'RF-FOBT-POSITIVE',
        'tier': 'P0',
        'group': 'fobt',
        'title': '粪便隐血阳性',
        'trigger': {'type': 'qualitative', 'analyte': 'fobt', 'min_grade': 1},
        'timeframe': _ONE_MONTH,
        'department': '消化内科',
        'checks': ['消化内科就诊', '结肠镜检查（由专科评估）'],
        'sources': ['cmab2019', 'crc_cn2020'],
        'topic': r'隐血|潜血|OB',
    },
    {
        'id': 'RF-IDA-GI',
        'tier': 'P0',
        'group': 'ida',
        'title': '缺铁性贫血需排查消化道失血',
        'trigger': {
            'type': 'all',
            'of': [
                {'type': 'numeric', 'analyte': 'hemoglobin', 'op': '<', 'lln': 1.0},
                {
                    'type': 'any',
                    'of': [
                        {'type': 'numeric', 'analyte': 'ferritin', 'op': '<', 'lln': 1.0},
                        {'type': 'numeric', 'analyte': 'ferritin', 'op': '<', 'value': 15.0},
                        {'type': 'numeric', 'analyte': 'mcv', 'op': '<', 'lln': 1.0},
                    ],
                },
            ],
        },
        'applies': {'any': [{'sex': 'male'}, {'menopause': True}]},
        'timeframe': _ONE_MONTH,
        'department': '消化内科',
        'checks': ['胃镜及结肠镜检查（双向内镜，由专科评估）', '复查血常规与铁代谢'],
        'sources': ['bsg2021_ida'],
        'topic': r'贫血|血红蛋白|铁',
    },
    {
        'id': 'RT-HP-POSITIVE',
        'tier': 'routine',
        'group': 'hp',
        'title': '幽门螺杆菌阳性',
        'trigger': {'type': 'qualitative', 'analyte': 'hp', 'min_grade': 1},
        'interval': {'text': '择期消化内科就诊，评估根除治疗', 'days': 90},
        'department': '消化内科',
        'sources': ['hp_cn2022'],
        'topic': r'幽门|Hp|HP',
    },
    # --- haematology ---------------------------------------------------------
    {
        'id': 'RF-HB-CRITICAL',
        'tier': 'P0',
        'group': 'hb_low',
        'title': '血红蛋白危急降低（≤60 g/L）',
        'trigger': {'type': 'numeric', 'analyte': 'hemoglobin', 'op': '<=', 'value': 60.0},
        'timeframe': _IMMEDIATE,
        'department': '血液科或急诊',
        'checks': ['复查血常规', '查明出血或贫血原因'],
        'sources': ['cmab2019'],
        'topic': r'贫血|血红蛋白',
    },
    {
        'id': 'RF-HB-MODERATE',
        'tier': 'P0',
        'group': 'hb_low',
        'title': '血红蛋白明显降低（<90 g/L，中度及以上贫血）',
        'trigger': {'type': 'numeric', 'analyte': 'hemoglobin', 'op': '<', 'value': 90.0},
        'timeframe': _ONE_WEEK,
        'department': '血液科',
        'checks': ['复查血常规与网织红细胞', '铁代谢、叶酸及维生素 B12'],
        'sources': ['internal_medicine_9'],
        'topic': r'贫血|血红蛋白',
    },
    {
        'id': 'RF-HB-HIGH',
        'tier': 'P0',
        'group': 'hb_high',
        'title': '血红蛋白明显升高（≥200 g/L）',
        'trigger': {'type': 'numeric', 'analyte': 'hemoglobin', 'op': '>=', 'value': 200.0},
        'timeframe': _ONE_WEEK,
        'department': '血液科',
        'checks': ['复查血常规', '红细胞增多原因评估'],
        'sources': ['cmab2019'],
        'topic': r'血红蛋白|红细胞',
    },
    {
        'id': 'RF-WBC-CRITICAL',
        'tier': 'P0',
        'group': 'wbc',
        'title': '白细胞危急异常',
        'trigger': {'type': 'numeric', 'analyte': 'wbc', 'op': '<=', 'value': 1.0},
        'timeframe': _IMMEDIATE,
        'department': '血液科或急诊',
        'checks': ['复查血常规及外周血涂片'],
        'sources': ['cmab2019'],
        'topic': r'白细胞',
    },
    {
        'id': 'RF-WBC-MARKED',
        'tier': 'P0',
        'group': 'wbc',
        'title': '白细胞明显异常（≤2.0 或 ≥30.0×10⁹/L）',
        'trigger': {
            'type': 'any',
            'of': [
                {'type': 'numeric', 'analyte': 'wbc', 'op': '<=', 'value': 2.0},
                {'type': 'numeric', 'analyte': 'wbc', 'op': '>=', 'value': 30.0},
            ],
        },
        'timeframe': _ONE_WEEK,
        'department': '血液科',
        'checks': ['复查血常规及外周血涂片'],
        'sources': ['cmab2019'],
        'topic': r'白细胞',
    },
    {
        'id': 'RF-PLT-CRITICAL',
        'tier': 'P0',
        'group': 'plt',
        'title': '血小板危急异常',
        'trigger': {
            'type': 'any',
            'of': [
                {'type': 'numeric', 'analyte': 'platelet', 'op': '<=', 'value': 30.0},
                {'type': 'numeric', 'analyte': 'platelet', 'op': '>=', 'value': 1000.0},
            ],
        },
        'timeframe': _IMMEDIATE,
        'department': '血液科或急诊',
        'checks': ['复查血常规及外周血涂片'],
        'sources': ['cmab2019'],
        'topic': r'血小板',
    },
    {
        'id': 'RF-PLT-LOW',
        'tier': 'P0',
        'group': 'plt',
        'title': '血小板明显降低（≤50×10⁹/L）',
        'trigger': {'type': 'numeric', 'analyte': 'platelet', 'op': '<=', 'value': 50.0},
        'timeframe': _ONE_WEEK,
        'department': '血液科',
        'checks': ['复查血常规及外周血涂片'],
        'sources': ['cmab2019'],
        'topic': r'血小板',
    },
    # --- electrolytes, glucose -----------------------------------------------
    {
        'id': 'RF-K-CRITICAL',
        'tier': 'P0',
        'group': 'potassium',
        'title': '血钾危急值（≤2.8 或 ≥6.2 mmol/L）',
        'trigger': {
            'type': 'any',
            'of': [
                {'type': 'numeric', 'analyte': 'potassium', 'op': '<=', 'value': 2.8},
                {'type': 'numeric', 'analyte': 'potassium', 'op': '>=', 'value': 6.2},
            ],
        },
        'timeframe': _IMMEDIATE,
        'department': '急诊',
        'checks': ['复查血钾', '心电图'],
        'sources': ['kost1990'],
        'topic': r'钾',
    },
    {
        'id': 'RF-K-MARKED',
        'tier': 'P0',
        'group': 'potassium',
        'title': '血钾明显异常（<3.0 或 >5.5 mmol/L）',
        'trigger': {
            'type': 'any',
            'of': [
                {'type': 'numeric', 'analyte': 'potassium', 'op': '<', 'value': 3.0},
                {'type': 'numeric', 'analyte': 'potassium', 'op': '>', 'value': 5.5},
            ],
        },
        'timeframe': _ONE_WEEK,
        'department': '肾内科或内分泌科',
        'checks': ['复查血钾及肾功能', '心电图'],
        'sources': ['ckd_k_cn2020', 'ckd_hyperk_cn2020'],
        'topic': r'钾',
    },
    {
        'id': 'RF-NA-CRITICAL',
        'tier': 'P0',
        'group': 'sodium',
        'title': '血钠危急值（≤120 或 ≥158 mmol/L）',
        'trigger': {
            'type': 'any',
            'of': [
                {'type': 'numeric', 'analyte': 'sodium', 'op': '<=', 'value': 120.0},
                {'type': 'numeric', 'analyte': 'sodium', 'op': '>=', 'value': 158.0},
            ],
        },
        'timeframe': _IMMEDIATE,
        'department': '急诊',
        'checks': ['复查血钠及血浆渗透压'],
        'sources': ['kost1990'],
        'topic': r'钠',
    },
    {
        'id': 'RF-NA-LOW',
        'tier': 'P0',
        'group': 'sodium',
        'title': '血钠明显降低（<130 mmol/L，中度及以上低钠）',
        'trigger': {'type': 'numeric', 'analyte': 'sodium', 'op': '<', 'value': 130.0},
        'timeframe': _ONE_WEEK,
        'department': '内分泌科或肾内科',
        'checks': ['复查血钠、血浆及尿渗透压', '查明低钠原因'],
        'sources': ['eje2014_hyponatraemia'],
        'topic': r'钠',
    },
    {
        'id': 'RF-GLU-LOW',
        'tier': 'P0',
        'group': 'glucose',
        'title': '空腹血糖危急降低（≤2.8 mmol/L）',
        'trigger': {'type': 'numeric', 'analyte': 'fasting_glucose', 'op': '<=', 'value': 2.8},
        'timeframe': _IMMEDIATE,
        'department': '急诊或内分泌科',
        'checks': ['复查血糖', '低血糖原因评估'],
        'sources': ['cmab2019'],
        'topic': r'血糖|葡萄糖',
    },
    {
        'id': 'RF-GLU-HIGH',
        'tier': 'P0',
        'group': 'glucose',
        'title': '血糖危急升高',
        'trigger': {
            'type': 'any',
            'of': [
                {'type': 'numeric', 'analyte': 'fasting_glucose', 'op': '>=', 'value': 16.7},
                {'type': 'numeric', 'analyte': 'random_glucose', 'op': '>=', 'value': 20.0},
                {
                    'type': 'all',
                    'of': [
                        {'type': 'numeric', 'analyte': 'fasting_glucose', 'op': '>=', 'value': 13.9},
                        {'type': 'qualitative', 'analyte': 'urine_ketone', 'min_grade': 1},
                    ],
                },
            ],
        },
        'timeframe': _IMMEDIATE,
        'department': '急诊或内分泌科',
        'checks': ['复查血糖、血酮及电解质'],
        'sources': ['cmab2019'],
        'topic': r'血糖|葡萄糖|糖尿病',
    },
    # --- kidney, liver --------------------------------------------------------
    {
        'id': 'RF-CREA-CRITICAL',
        'tier': 'P0',
        'group': 'creatinine',
        'title': '血肌酐危急升高（≥707 μmol/L）',
        'trigger': {'type': 'numeric', 'analyte': 'creatinine', 'op': '>=', 'value': 707.0},
        'timeframe': _IMMEDIATE,
        'department': '肾内科或急诊',
        'checks': ['复查肾功能及电解质'],
        'sources': ['cmab2019'],
        'topic': r'肌酐|肾',
    },
    {
        'id': 'RF-CREA-MARKED',
        'tier': 'P0',
        'group': 'creatinine',
        'title': '血肌酐明显升高（≥445 μmol/L）',
        'trigger': {'type': 'numeric', 'analyte': 'creatinine', 'op': '>=', 'value': 445.0},
        'timeframe': _ONE_WEEK,
        'department': '肾内科',
        'checks': ['复查肾功能及电解质', '泌尿系超声'],
        'sources': ['cmab2019'],
        'topic': r'肌酐|肾',
    },
    {
        'id': 'RF-EGFR-LT30',
        'tier': 'P0',
        'group': 'creatinine',
        'title': '估算肾小球滤过率 <30',
        'trigger': {'type': 'numeric', 'analyte': 'egfr', 'op': '<', 'value': 30.0},
        'timeframe': _ONE_MONTH,
        'department': '肾内科',
        'checks': ['复查肾功能', '尿白蛋白/肌酐比'],
        'sources': ['kdigo2024_ckd'],
        'topic': r'肾|肌酐|滤过',
    },
    {
        'id': 'RF-URINE-3PLUS',
        'tier': 'P0',
        'group': 'urine',
        'title': '尿蛋白或尿潜血 3+',
        'trigger': {
            'type': 'any',
            'of': [
                {'type': 'qualitative', 'analyte': 'urine_protein', 'min_grade': 3},
                {'type': 'qualitative', 'analyte': 'urine_blood', 'min_grade': 3},
            ],
        },
        'timeframe': _ONE_MONTH,
        'department': '肾内科',
        'checks': ['复查尿常规及尿沉渣', '尿白蛋白/肌酐比', '肾功能'],
        'sources': ['cmab2019'],
        'topic': r'尿蛋白|尿潜血|尿隐血|蛋白尿|血尿',
    },
    {
        'id': 'RF-URINE-WITH-RENAL',
        'tier': 'P0',
        'group': 'urine',
        'title': '尿蛋白或尿潜血阳性伴肾功能异常',
        'trigger': {
            'type': 'all',
            'of': [
                {
                    'type': 'any',
                    'of': [
                        {'type': 'qualitative', 'analyte': 'urine_protein', 'min_grade': 1},
                        {'type': 'qualitative', 'analyte': 'urine_blood', 'min_grade': 1},
                    ],
                },
                {
                    'type': 'any',
                    'of': [
                        {'type': 'numeric', 'analyte': 'creatinine', 'op': '>', 'uln': 1.0},
                        {'type': 'numeric', 'analyte': 'egfr', 'op': '<', 'value': 60.0},
                    ],
                },
            ],
        },
        'timeframe': _ONE_MONTH,
        'department': '肾内科',
        'checks': ['复查尿常规及尿沉渣', '尿白蛋白/肌酐比', '肾功能'],
        'sources': ['kdigo2024_ckd'],
        'topic': r'尿蛋白|尿潜血|尿隐血|肾|肌酐',
    },
    {
        'id': 'RF-AMINOTRANSFERASE-15X',
        'tier': 'P0',
        'group': 'transaminase',
        'title': '转氨酶危急升高（≥15 倍参考上限）',
        'trigger': {
            'type': 'any',
            'of': [
                {'type': 'numeric', 'analyte': 'alt', 'op': '>=', 'uln': 15.0},
                {'type': 'numeric', 'analyte': 'ast', 'op': '>=', 'uln': 15.0},
            ],
        },
        'timeframe': _IMMEDIATE,
        'department': '肝病科或急诊',
        'checks': ['复查肝功能及凝血功能'],
        'sources': ['cmab2019'],
        'topic': r'转氨酶|肝功能|ALT|AST',
    },
    {
        'id': 'RF-AMINOTRANSFERASE-5X',
        'tier': 'P0',
        'group': 'transaminase',
        'title': '转氨酶明显升高（≥5 倍参考上限）',
        'trigger': {
            'type': 'any',
            'of': [
                {'type': 'numeric', 'analyte': 'alt', 'op': '>=', 'uln': 5.0},
                {'type': 'numeric', 'analyte': 'ast', 'op': '>=', 'uln': 5.0},
            ],
        },
        'timeframe': _ONE_WEEK,
        'department': '肝病科或消化内科',
        'checks': ['复查肝功能', '肝炎病毒标志物及肝脏影像'],
        'sources': ['cmab2019'],
        'topic': r'转氨酶|肝功能|ALT|AST',
    },
    {
        'id': 'RF-TBIL-3X',
        'tier': 'P0',
        'group': 'bilirubin',
        'title': '总胆红素明显升高（≥3 倍参考上限）',
        'trigger': {'type': 'numeric', 'analyte': 'total_bilirubin', 'op': '>=', 'uln': 3.0},
        'timeframe': _ONE_WEEK,
        'department': '肝病科或消化内科',
        'checks': ['复查肝功能', '肝胆影像检查'],
        'sources': ['cmab2019'],
        'topic': r'胆红素|黄疸',
    },
    # --- vitals, ECG -----------------------------------------------------------
    {
        'id': 'RF-BP-SEVERE',
        'tier': 'P0',
        'group': 'bp',
        'title': '血压显著升高（≥180/110 mmHg）',
        'trigger': {
            'type': 'any',
            'of': [
                {'type': 'numeric', 'analyte': 'sbp', 'op': '>=', 'value': 180.0},
                {'type': 'numeric', 'analyte': 'dbp', 'op': '>=', 'value': 110.0},
            ],
        },
        'timeframe': {'text': '休息后复测，仍达此水平或伴不适时当日就医', 'days': 0},
        'department': '心内科或急诊',
        'checks': ['复测血压', '心血管科评估'],
        'sources': ['cmab2019'],
        'topic': r'血压',
    },
    {
        'id': 'RF-HR-EXTREME',
        'tier': 'P0',
        'group': 'hr',
        'title': '心率显著异常（≥150 或 ≤45 次/分）',
        'trigger': {
            'type': 'any',
            'of': [
                {'type': 'numeric', 'analyte': 'heart_rate', 'op': '>=', 'value': 150.0},
                {'type': 'numeric', 'analyte': 'heart_rate', 'op': '<=', 'value': 45.0},
            ],
        },
        'timeframe': _IMMEDIATE,
        'department': '心内科或急诊',
        'checks': ['心电图', '心血管科评估'],
        'sources': ['cmab2019'],
        'topic': r'心率|心动',
    },
    {
        'id': 'RF-ECG-STE',
        'tier': 'P0',
        'group': 'ecg_ischemia',
        'title': '心电图 ST 段抬高',
        'trigger': {'type': 'text', 'scope': 'ecg', 'pattern': r'ST\s*段?\s*(?:弓背(?:向上)?)?\s*抬高'},
        'timeframe': _IMMEDIATE,
        'department': '心内科或急诊',
        'checks': ['复查心电图', '心肌损伤标志物'],
        'sources': ['cmab2019', 'esc2023_acs'],
        'topic': r'心电图|ST|心肌',
    },
    {
        'id': 'RF-ECG-STD',
        'tier': 'P0',
        'group': 'ecg_ischemia',
        'title': '心电图 ST 段压低',
        'trigger': {'type': 'text', 'scope': 'ecg', 'pattern': r'ST\s*段?\s*(?:水平型|下斜型)?\s*(?:压低|下移)'},
        'timeframe': _ONE_WEEK,
        'department': '心内科',
        'checks': ['复查心电图', '心血管科评估是否需负荷试验或冠脉检查'],
        'sources': ['esc2023_acs'],
        'topic': r'心电图|ST|心肌',
    },
    {
        'id': 'RF-ECG-AF',
        'tier': 'P0',
        'group': 'ecg_af',
        'title': '心电图提示心房颤动或心房扑动',
        'trigger': {'type': 'text', 'scope': 'ecg', 'pattern': r'心房颤动|房颤|心房扑动|房扑'},
        'timeframe': _TWO_WEEKS,
        'department': '心内科',
        'checks': ['心血管科评估卒中风险与抗凝', '心脏超声'],
        'sources': ['esc2024_af'],
        'topic': r'房颤|心房|心电图',
    },
    {
        'id': 'RF-ECG-AVB3',
        'tier': 'P0',
        'group': 'ecg_avb',
        'title': '心电图提示三度或高度房室传导阻滞',
        'trigger': {
            'type': 'text',
            'scope': 'ecg',
            'pattern': r'(?:三|Ⅲ|III|3)\s*度\s*房室(?:传导)?阻滞|完全性房室(?:传导)?阻滞|高度房室(?:传导)?阻滞',
        },
        'timeframe': _IMMEDIATE,
        'department': '心内科或急诊',
        'checks': ['复查心电图', '心血管科评估起搏指征'],
        'sources': ['cmab2019', 'acc2018_brady'],
        'topic': r'阻滞|心电图',
    },
    {
        'id': 'RF-ECG-AVB2',
        'tier': 'P0',
        'group': 'ecg_avb',
        'title': '心电图提示二度房室传导阻滞',
        'trigger': {
            'type': 'text',
            'scope': 'ecg',
            'pattern': r'(?:二|Ⅱ|II|2)\s*度\s*(?:[ⅠⅡI12]\s*型\s*)?房室(?:传导)?阻滞|'
            r'莫氏|Mobitz|文氏',
        },
        'timeframe': _ONE_WEEK,
        'department': '心内科',
        'checks': ['动态心电图', '心血管科评估'],
        'sources': ['acc2018_brady'],
        'topic': r'阻滞|心电图',
    },
    {
        'id': 'RF-ECG-VT',
        'tier': 'P0',
        'group': 'ecg_vt',
        'title': '心电图提示室性心动过速',
        'trigger': {'type': 'text', 'scope': 'ecg', 'pattern': r'室性心动过速|室速'},
        'timeframe': _IMMEDIATE,
        'department': '心内科或急诊',
        'checks': ['复查心电图', '心血管科评估'],
        'sources': ['cmab2019'],
        'topic': r'心动过速|心电图',
    },
    {
        'id': 'RF-ECG-QTC',
        'tier': 'P0',
        'group': 'ecg_qtc',
        'title': '心电图 QTc 明显延长（≥500 ms）',
        'trigger': {'type': 'qtc', 'min_ms': 500},
        'timeframe': _ONE_WEEK,
        'department': '心内科',
        'checks': ['复查心电图及电解质', '核查延长 QT 的药物'],
        'sources': ['aha2010_qt', 'cmab2019'],
        'topic': r'QT|心电图',
    },
    # --- tumour markers ----------------------------------------------------------
    {
        'id': 'RF-TM-AFP-400',
        'tier': 'P0',
        'group': 'marker:afp',
        'title': '甲胎蛋白明显升高（>400 ng/mL）',
        'trigger': {'type': 'numeric', 'analyte': 'afp', 'op': '>', 'value': 400.0},
        'timeframe': _TWO_WEEKS,
        'department': '肝病科或肝胆外科',
        'checks': ['复查甲胎蛋白', '肝脏增强 CT 或 MRI'],
        'sources': ['cmab2019'],
        'topic': r'甲胎|AFP',
    },
    {
        'id': 'RT-TM-AFP-MILD',
        'tier': 'routine',
        'group': 'marker:afp',
        'title': '甲胎蛋白升高',
        'trigger': {'type': 'numeric', 'analyte': 'afp', 'op': '>', 'uln': 1.0},
        'interval': {'text': '1–3 个月内复查并做肝脏影像', 'days': 90},
        'department': '肝病科',
        'sources': ['cmab2019'],
        'topic': r'甲胎|AFP',
    },
    {
        'id': 'RF-TM-PSA-10',
        'tier': 'P0',
        'group': 'marker:psa',
        'title': '前列腺特异性抗原明显升高',
        'trigger': {
            'type': 'any',
            'of': [
                {'type': 'numeric', 'analyte': 'tpsa', 'op': '>', 'value': 10.0},
                {'type': 'psa_ratio', 'max_ratio': 0.15, 'min_tpsa': 4.0},
            ],
        },
        'applies': {'sex': 'male'},
        'timeframe': _ONE_MONTH,
        'department': '泌尿外科',
        'checks': ['复查 PSA 及游离 PSA', '前列腺 MRI（由专科评估）'],
        'sources': ['cmab2019'],
        'topic': r'PSA|前列腺',
    },
    {
        'id': 'RT-TM-PSA-4',
        'tier': 'routine',
        'group': 'marker:psa',
        'title': '前列腺特异性抗原超过 4 ng/mL',
        'trigger': {'type': 'numeric', 'analyte': 'tpsa', 'op': '>', 'value': 4.0},
        'applies': {'sex': 'male'},
        'interval': {'text': '3 个月内复查 PSA 并泌尿外科就诊', 'days': 90},
        'department': '泌尿外科',
        'sources': ['psa_cn2022'],
        'topic': r'PSA|前列腺',
    },
    {
        'id': 'RF-TM-CA125-POSTMENO',
        'tier': 'P0',
        'group': 'marker:ca125',
        'title': '绝经后 CA125 明显升高（>95 U/mL）',
        'trigger': {'type': 'numeric', 'analyte': 'ca125', 'op': '>', 'value': 95.0},
        'applies': {'menopause': True},
        'timeframe': _TWO_WEEKS,
        'department': '妇科',
        'checks': ['复查 CA125', '经阴道盆腔超声'],
        'sources': ['cmab2019'],
        'topic': r'CA125|CA-125',
    },
    {
        'id': 'RF-TM-CA125-200',
        'tier': 'P0',
        'group': 'marker:ca125',
        'title': 'CA125 明显升高（>200 U/mL）',
        'trigger': {'type': 'numeric', 'analyte': 'ca125', 'op': '>', 'value': 200.0},
        'timeframe': _TWO_WEEKS,
        'department': '妇科',
        'checks': ['复查 CA125', '盆腔超声'],
        'sources': ['acog2016_adnexal'],
        'topic': r'CA125|CA-125',
    },
    {
        'id': 'RT-TM-CA125-MILD',
        'tier': 'routine',
        'group': 'marker:ca125',
        'title': 'CA125 升高',
        'trigger': {'type': 'numeric', 'analyte': 'ca125', 'op': '>', 'uln': 1.0},
        'interval': {'text': '1–3 个月内复查（避开经期）', 'days': 90},
        'department': '妇科',
        'sources': ['cmab2019'],
        'topic': r'CA125|CA-125',
    },
    *_marker_rules(),
    # --- imaging grades ----------------------------------------------------------
    {
        'id': 'RF-BIRADS-4',
        'tier': 'P0',
        'group': 'breast',
        'title': '乳腺影像 BI-RADS 4 类及以上',
        'trigger': {'type': 'grade', 'system': 'birads', 'min': 4},
        'timeframe': _TWO_WEEKS,
        'department': '乳腺外科',
        'checks': ['乳腺专科评估', '影像引导下穿刺活检（由专科评估）'],
        'sources': ['acr_birads2013', 'cmab2019'],
        'topic': r'乳腺|乳房',
    },
    {
        'id': 'RT-BIRADS-3',
        'tier': 'routine',
        'group': 'breast',
        'title': '乳腺影像 BI-RADS 3 类',
        'trigger': {'type': 'grade', 'system': 'birads', 'min': 3, 'max': 3},
        'interval': {'text': '6 个月复查乳腺影像', 'days': 180},
        'department': '乳腺外科',
        'sources': ['acr_birads2013'],
        'topic': r'乳腺|乳房',
    },
    {
        'id': 'RT-BIRADS-0',
        'tier': 'routine',
        'group': 'breast_incomplete',
        'title': '乳腺影像 BI-RADS 0 类（评估未完成）',
        'trigger': {'type': 'grade', 'system': 'birads', 'min': 0, 'max': 0},
        'interval': {'text': '尽早补充乳腺影像评估', 'days': 30},
        'department': '乳腺外科',
        'sources': ['acr_birads2013'],
        'topic': r'乳腺|乳房',
    },
    {
        'id': 'RT-BREAST-NODULE',
        'tier': 'routine',
        'group': 'breast',
        'title': '乳腺结节',
        'trigger': {'type': 'finding', 'organ': r'乳腺|乳房', 'finding': r'结节|肿块|占位'},
        'interval': {'text': '6–12 个月复查乳腺超声', 'days': 180},
        'department': '乳腺外科',
        'sources': ['acr_birads2013'],
        'topic': r'乳腺|乳房',
    },
    {
        'id': 'RF-TIRADS-4',
        'tier': 'P0',
        'group': 'thyroid',
        'title': '甲状腺结节 TI-RADS 4 类及以上',
        'trigger': {'type': 'grade', 'system': 'tirads', 'min': 4},
        'timeframe': _ONE_MONTH,
        'department': '甲状腺外科或内分泌科',
        'checks': ['甲状腺专科评估', '按结节大小评估超声引导下细针穿刺（FNA）'],
        'sources': ['c_tirads2020', 'acr_tirads2017'],
        'topic': r'甲状腺',
    },
    {
        'id': 'RT-THYROID-NODULE',
        'tier': 'routine',
        'group': 'thyroid',
        'title': '甲状腺结节',
        'trigger': {
            'type': 'any',
            'of': [
                {'type': 'grade', 'system': 'tirads', 'min': 1, 'max': 3},
                {'type': 'finding', 'organ': r'甲状腺', 'finding': r'结节'},
            ],
        },
        'interval': {'text': '6–12 个月复查甲状腺超声', 'days': 365},
        'department': '甲状腺外科或内分泌科',
        'sources': ['acr_tirads2017'],
        'topic': r'甲状腺',
    },
    {
        # Lung-RADS 4A/4B/4X call for a 3-month CT or work-up; category 3
        # (probably benign) is a 6-month low-dose CT follow-up, not a priority.
        'id': 'RF-LUNGRADS-4',
        'tier': 'P0',
        'group': 'lung',
        'title': '肺结节 Lung-RADS 4 类及以上',
        'trigger': {'type': 'grade', 'system': 'lungrads', 'min': 4},
        'timeframe': _ONE_MONTH,
        'department': '呼吸内科或胸外科',
        'checks': ['呼吸或胸外科评估', '按分类安排 3 个月胸部 CT 复查或进一步检查'],
        'sources': ['acr_lungrads2022'],
        'topic': r'肺',
    },
    {
        'id': 'RT-LUNGRADS-3',
        'tier': 'routine',
        'group': 'lung',
        'title': '肺结节 Lung-RADS 3 类',
        'trigger': {'type': 'grade', 'system': 'lungrads', 'min': 3, 'max': 3},
        'interval': {'text': '6 个月复查低剂量胸部 CT', 'days': 180},
        'department': '呼吸内科',
        'sources': ['acr_lungrads2022'],
        'topic': r'肺',
    },
    {
        'id': 'RF-LUNG-NODULE-8MM',
        'tier': 'P0',
        'group': 'lung',
        'title': '肺实性结节 ≥8 mm',
        'trigger': {
            'type': 'finding',
            'organ': r'肺',
            'finding': r'结节',
            'min_mm': 8.0,
            'exclude': r'钙化|磨玻璃|亚实性|部分实性',
            # A recorded Lung-RADS category already decides the follow-up.
            'graded': r'Lung\s*-?\s*RADS',
        },
        'timeframe': _ONE_MONTH,
        'department': '呼吸内科或胸外科',
        'checks': ['胸部 CT 复查或 PET-CT/活检（由专科评估）'],
        'sources': ['fleischner2017'],
        'topic': r'肺',
    },
    {
        'id': 'RT-LUNG-NODULE',
        'tier': 'routine',
        'group': 'lung',
        'title': '肺结节',
        'trigger': {'type': 'finding', 'organ': r'肺', 'finding': r'结节', 'exclude': r'钙化'},
        'interval': {'text': '6–12 个月复查胸部 CT', 'days': 180},
        'department': '呼吸内科',
        'sources': ['fleischner2017'],
        'topic': r'肺',
    },
    # --- sized organ findings (cmab2019 B 类) -----------------------------------
    {
        'id': 'RF-GB-POLYP-LARGE',
        'tier': 'P0',
        'group': 'gallbladder_polyp',
        'title': '胆囊息肉 >10 mm',
        'trigger': {'type': 'finding', 'organ': r'胆囊', 'finding': r'息肉|附着|隆起', 'min_mm': 10.0, 'strict': True},
        'timeframe': _ONE_MONTH,
        'department': '肝胆外科',
        'checks': ['肝胆外科评估是否行胆囊切除'],
        'sources': ['cmab2019', 'esgar2022_gb_polyp'],
        'topic': r'胆囊.{0,6}(?:息肉|隆起)|息肉',
    },
    {
        'id': 'RF-GB-POLYP-8MM-50Y',
        'tier': 'P0',
        'group': 'gallbladder_polyp',
        'title': '胆囊息肉 >8 mm 且年龄 >50 岁',
        'trigger': {'type': 'finding', 'organ': r'胆囊', 'finding': r'息肉|附着|隆起', 'min_mm': 8.0, 'strict': True},
        'applies': {'min_age': 51},
        'timeframe': _ONE_MONTH,
        'department': '肝胆外科',
        'checks': ['肝胆外科评估是否行胆囊切除'],
        'sources': ['cmab2019'],
        'topic': r'胆囊.{0,6}(?:息肉|隆起)|息肉',
    },
    {
        'id': 'RT-GB-POLYP',
        'tier': 'routine',
        'group': 'gallbladder_polyp',
        'title': '胆囊息肉',
        'trigger': {'type': 'finding', 'organ': r'胆囊', 'finding': r'息肉|附着|隆起'},
        'interval': {'text': '6 个月复查腹部超声', 'days': 180},
        'department': '肝胆外科',
        'sources': ['esgar2022_gb_polyp'],
        'topic': r'胆囊.{0,6}(?:息肉|隆起)|息肉',
    },
    {
        'id': 'RF-LIVER-CYST-10CM',
        'tier': 'P0',
        'group': 'liver_cyst',
        'title': '肝囊肿直径 ≥10 cm',
        'trigger': {'type': 'finding', 'organ': r'肝', 'finding': r'囊肿|囊性', 'min_mm': 100.0},
        'timeframe': _ONE_MONTH,
        'department': '肝胆外科',
        'checks': ['肝胆外科评估'],
        'sources': ['cmab2019'],
        'topic': r'肝囊肿|肝.{0,6}囊',
    },
    {
        'id': 'RT-LIVER-CYST',
        'tier': 'routine',
        'group': 'liver_cyst',
        'title': '肝囊肿',
        'trigger': {'type': 'finding', 'organ': r'肝', 'finding': r'囊肿|囊性'},
        'interval': {'text': '单纯性囊肿无需专门随访，随年度体检复查超声', 'days': 365},
        'department': '肝胆外科',
        'sources': ['easl2022_liver_cyst'],
        'topic': r'肝囊肿|肝.{0,6}囊',
    },
    {
        'id': 'RF-KIDNEY-CYST-5CM',
        'tier': 'P0',
        'group': 'kidney_cyst',
        'title': '肾囊肿直径 ≥5 cm',
        'trigger': {'type': 'finding', 'organ': r'肾', 'finding': r'囊肿|囊性|无回声区', 'min_mm': 50.0},
        'timeframe': _ONE_MONTH,
        'department': '泌尿外科',
        'checks': ['泌尿外科评估', '肾脏增强 CT（由专科评估）'],
        'sources': ['cmab2019'],
        'topic': r'肾囊肿|肾.{0,6}囊',
    },
    {
        'id': 'RT-KIDNEY-CYST',
        'tier': 'routine',
        'group': 'kidney_cyst',
        'title': '肾囊肿',
        'trigger': {'type': 'finding', 'organ': r'肾', 'finding': r'囊肿|囊性|无回声区'},
        'interval': {'text': '单纯性囊肿随年度体检复查超声', 'days': 365},
        'department': '泌尿外科',
        'sources': ['bosniak2019'],
        'topic': r'肾囊肿|肾.{0,6}囊',
    },
    {
        'id': 'RF-PANCREAS-CYST',
        'tier': 'P0',
        'group': 'pancreas',
        'title': '胰腺囊肿 ≥3 cm 或主胰管扩张 >5 mm',
        'trigger': {
            'type': 'any',
            'of': [
                {'type': 'finding', 'organ': r'胰', 'finding': r'囊肿|囊性', 'min_mm': 30.0},
                {'type': 'finding', 'organ': r'主胰管', 'finding': r'扩张|增宽', 'min_mm': 5.0, 'strict': True},
            ],
        },
        'timeframe': _ONE_MONTH,
        'department': '消化内科或肝胆胰外科',
        'checks': ['胰腺增强 MRI/MRCP（由专科评估）'],
        'sources': ['cmab2019'],
        'topic': r'胰',
    },
)

_RULES_BY_ID = {rule['id']: rule for rule in RULES}
_URGENT_ADVICE = re.compile(
    r'(?:尽快|及时|尽早|立即|即刻|紧急|立刻)[^，,。；;]{0,8}?(?:就诊|就医|诊治|转诊|急诊|专科评估|进一步检查|进一步诊治|住院)'
    r'|转(?:入)?急诊|急诊就诊'
)
_NEGATED_ADVICE = re.compile(r'无需|无须|不必|不需要|不作为|暂不')
_FOLLOWUP_ADVICE = re.compile(r'复查|随访|随诊|复诊|定期|监测|观察')
_DEPARTMENTS = (
    '消化内科',
    '肝胆外科',
    '肝胆胰外科',
    '胃肠外科',
    '普外科',
    '甲状腺外科',
    '乳腺外科',
    '泌尿外科',
    '妇产科',
    '妇科',
    '心血管内科',
    '心内科',
    '内分泌科',
    '肾内科',
    '肾脏内科',
    '呼吸内科',
    '呼吸科',
    '胸外科',
    '血液科',
    '眼科',
    '耳鼻喉科',
    '口腔科',
    '骨科',
    '神经内科',
    '神经外科',
    '肝病科',
    '感染科',
    '风湿免疫科',
    '皮肤科',
    '营养科',
    '男科',
    '血管外科',
    '心胸外科',
    '肛肠科',
    '甲乳外科',
    '急诊',
)
_NEGATION = re.compile(r'未见|未探及|未发现|未显示|未提示|无明显|无|排除|否认|不支持|未见明显')
_SUMMARY_NAME = re.compile(r'^\s*(?:主检结论|总检结论|主检|小结|结论)')
_SIZE = re.compile(
    r'(\d+(?:\.\d+)?)\s*(?:[*×xX]\s*(\d+(?:\.\d+)?)\s*)?(?:[*×xX]\s*(\d+(?:\.\d+)?)\s*)?(mm|cm|毫米|厘米)',
    re.I,
)
_GRADE_PATTERNS = {
    'birads': re.compile(r'BI\s*-?\s*RADS\s*(?:分类|分级|类别)?\s*[:：]?\s*(?:为)?\s*([0-6])\s*([ABCabc])?', re.I),
    'tirads': re.compile(
        r'(?:C\s*-?\s*|ACR\s*)?TI\s*-?\s*RADS\s*(?:分类|分级|类别)?\s*[:：]?\s*(?:为)?\s*(?:TR\s*)?([1-6])\s*([ABCabc])?',
        re.I,
    ),
    'lungrads': re.compile(
        r'Lung\s*-?\s*RADS\s*(?:v?\s*\d{4})?\s*(?:分类|分级|类别)?\s*[:：]?\s*(?:为)?\s*([0-4])\s*([ABXSabxs])?',
        re.I,
    ),
}
_ECG_NAME = re.compile(r'心电图|\bECG\b|\bEKG\b|动态心电|Holter', re.I)
_QTC = re.compile(r'QTc\s*(?:间期)?\s*[:：=]?\s*(\d{3})|QT\s*/\s*QTc\s*[:：=]?\s*\d{3}\s*/\s*(\d{3})', re.I)
_CN_NUMBERS = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
_INTERVAL = re.compile(
    r'(?:每|间隔|隔)?\s*(\d+(?:\.\d+)?|[一二两三四五六七八九十]{1,3}|半)\s*(?:个)?\s*(天|日|周|星期|月|年)\s*'
    r'(?:后|内|左右|一次)?\s*(?:复查|随访|随诊|复诊|再查|检查|一次)?'
)


# ---------------------------------------------------------------------------
# Small parsers
# ---------------------------------------------------------------------------
def _get(row, *keys):
    for key in keys:
        value = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
        if value is not None:
            return value
    return None


def _fact_id(row):
    return _get(row, 'factId', 'fact_id', 'id')


def _norm(text):
    return unicodedata.normalize('NFKC', str(text or '')).strip()


def _unit_key(unit):
    text = _norm(unit).lower().replace(' ', '').replace('µ', 'μ').replace('×', '').replace('^', '')
    text = re.sub(r'^10(?:e|\*)?9/', '109/', text)
    return text


def _unit_factors(analyte):
    return {_unit_key(key): factor for key, factor in ANALYTES[analyte].get('units', {}).items()}


def _number(value):
    """Return ``(comparator, number)``; comparator is '', '<' or '>'."""
    text = _norm(value).replace(',', '')
    match = re.fullmatch(r'([<>≤≥]=?)?\s*(-?\d+(?:\.\d+)?)\s*[↑↓HL]?', text)
    if not match:
        return None
    comparator = (match.group(1) or '').replace('≤', '<').replace('≥', '>').rstrip('=')
    return comparator, float(match.group(2))


def subject_context(subject, facts=()):
    """Sex, age and menopause from the subject plus explicit self reports."""
    sex_text = _norm(_get(subject or {}, 'sex', 'gender')).lower()
    sex = (
        'male'
        if sex_text in {'男', 'male', 'm', '男性'}
        else 'female'
        if sex_text in {'女', 'female', 'f', '女性'}
        else None
    )
    age = _get(subject or {}, 'age')
    try:
        age = int(age) if age is not None else None
    except (TypeError, ValueError):
        age = None
    reported = ' '.join(
        f'{_get(row, "name") or ""} {_get(row, "value") or ""}' for row in facts if _get(row, 'kind') == 'self_reported'
    )
    menopause_reported = None
    if re.search(r'未绝经|尚未绝经|月经规律|月经正常', reported):
        menopause_reported = False
    elif re.search(r'已绝经|绝经\s*\d|绝经后|停经\s*\d+\s*年', reported):
        menopause_reported = True
    if sex == 'male':
        postmenopausal = False
    elif menopause_reported is not None:
        postmenopausal = menopause_reported
    else:
        postmenopausal = age is not None and age >= 50
    return {'sex': sex, 'age': age, 'postmenopausal': postmenopausal}


def reference_bounds(reference, sex=None):
    """``(low, high)`` from a printed reference text; either may be ``None``."""
    text = _norm(reference)
    if not text:
        return None, None
    parts = re.split(r'[;；]', text)
    if len(parts) > 1 and sex:
        wanted = '男' if sex == 'male' else '女'
        chosen = [part for part in parts if wanted in part]
        text = chosen[0] if chosen else parts[0]
    text = re.sub(r'^[^\d<>≤≥\-]*', '', text)
    match = re.search(r'(-?\d+(?:\.\d+)?)\s*[-~～—–至]+\s*(-?\d+(?:\.\d+)?)', text)
    if match:
        return float(match.group(1)), float(match.group(2))
    match = re.search(r'[<≤]\s*=?\s*(\d+(?:\.\d+)?)', text)
    if match:
        return None, float(match.group(1))
    match = re.search(r'[>≥]\s*=?\s*(\d+(?:\.\d+)?)', text)
    if match:
        return float(match.group(1)), None
    return None, None


def qualitative_grade(value, status=None, reference=None):
    """0 negative, 0.5 trace, 1-4 positive grades; ``None`` when not qualitative."""
    for text in (_norm(value), _norm(status)):
        if not text:
            continue
        if re.search(r'阴性|negative|未检出|未见|^-$|^（-）$|^\(-\)$', text, re.I):
            return 0.0
        if re.search(r'^±$|^\+-$|微量|trace|可疑', text, re.I):
            return 0.5
        match = re.search(r'([1-4])\s*\+', text)
        if match:
            return float(match.group(1))
        match = re.fullmatch(r'\(?(\+{1,4})\)?', text)
        if match:
            return float(len(match.group(1)))
        if re.search(r'弱阳性|阳性|positive|reactive', text, re.I):
            return 1.0
    number = _number(value)
    if number is not None:
        _, high = reference_bounds(reference)
        if high is not None:
            return 1.0 if number[1] > high else 0.0
    return None


def _matches_analyte(row, analyte):
    definition = ANALYTES[analyte]
    name = _norm(_get(row, 'name'))
    if not name or _SUMMARY_NAME.search(name):
        return False
    if not re.search(definition['include'], name, re.I):
        return False
    return not (definition['exclude'] and re.search(definition['exclude'], name, re.I))


def _canonical(row, analyte):
    """Return ``(comparator, value)`` in the analyte's canonical unit, or ``None``."""
    number = _number(_get(row, 'value'))
    if number is None:
        return None
    unit = _unit_key(_get(row, 'unit'))
    factor = 1.0
    if unit:
        factors = _unit_factors(analyte)
        if unit not in factors:
            # An unrecognised unit cannot be compared with a fixed threshold.
            return None
        factor = factors[unit]
    comparator, value = number
    if analyte == 'hemoglobin' and not unit and value < 25:
        factor = 10.0
    return comparator, value * factor


def _scaled_reference(row, analyte, sex):
    low, high = reference_bounds(_get(row, 'referenceRange', 'reference_range'), sex)
    definition = ANALYTES[analyte]
    unit = _unit_key(_get(row, 'unit'))
    factor = _unit_factors(analyte).get(unit, 1.0) if unit else 1.0
    low = low * factor if low is not None else None
    high = high * factor if high is not None else None
    if high is None and definition.get('default_uln') is not None and low is None:
        high = definition['default_uln']
    return low, high


def _compare(comparator, value, op, threshold):
    if comparator == '<':
        return op in {'<', '<='} and value <= threshold
    if comparator == '>':
        return op in {'>', '>='} and value >= threshold
    return {'<': value < threshold, '<=': value <= threshold, '>': value > threshold, '>=': value >= threshold}[op]


def _sizes_mm(text):
    sizes = []
    for match in _SIZE.finditer(text):
        numbers = [float(item) for item in match.groups()[:3] if item]
        unit = match.group(4).lower()
        factor = 10.0 if unit in {'cm', '厘米'} else 1.0
        sizes.append(max(numbers) * factor)
    return sizes


def _clauses(text):
    return [part for part in re.split(r'[，,]', text) if part.strip()]


def _negated(clause, keyword_match):
    prefix = clause[: keyword_match.start()]
    return bool(_NEGATION.search(prefix))


# ---------------------------------------------------------------------------
# Trigger evaluation
# ---------------------------------------------------------------------------
class _Evidence:
    def __init__(self):
        self.fact_ids = []
        self.notes = []

    def add(self, row, note):
        fid = _fact_id(row)
        if fid is not None and fid not in self.fact_ids:
            self.fact_ids.append(fid)
        if note and note not in self.notes:
            self.notes.append(note)

    def merge(self, other):
        self.fact_ids.extend(fid for fid in other.fact_ids if fid not in self.fact_ids)
        self.notes.extend(note for note in other.notes if note not in self.notes)


def _row_label(row):
    value = _get(row, 'value')
    unit = _get(row, 'unit') or ''
    return f'{_norm(_get(row, "name"))}：{_norm(value)}{(" " + _norm(unit)) if unit and value is not None else ""}'


def _evaluate(trigger, rows, ctx, evidence):
    kind = trigger['type']
    if kind == 'all':
        local = _Evidence()
        if not all(_evaluate(item, rows, ctx, local) for item in trigger['of']):
            return False
        evidence.merge(local)
        return True
    if kind == 'any':
        hit = False
        for item in trigger['of']:
            hit = _evaluate(item, rows, ctx, evidence) or hit
        return hit
    if kind == 'numeric':
        hit = False
        for row in rows['clinical']:
            if not _matches_analyte(row, trigger['analyte']):
                continue
            number = _canonical(row, trigger['analyte'])
            if number is None:
                continue
            comparator, value = number
            if 'value' in trigger:
                threshold = trigger['value']
            else:
                low, high = _scaled_reference(row, trigger['analyte'], ctx['sex'])
                if 'uln' in trigger:
                    if high is None:
                        status = _norm(_get(row, 'status'))
                        if trigger['uln'] <= 1.0 and re.search(r'偏高|升高|↑|^H$', status):
                            evidence.add(row, _row_label(row))
                            hit = True
                        continue
                    threshold = high * trigger['uln']
                else:
                    if low is None:
                        status = _norm(_get(row, 'status'))
                        if trigger['lln'] >= 1.0 and re.search(r'偏低|降低|↓|^L$', status):
                            evidence.add(row, _row_label(row))
                            hit = True
                        continue
                    threshold = low * trigger['lln']
            if _compare(comparator, value, trigger['op'], threshold):
                evidence.add(row, _row_label(row))
                hit = True
        return hit
    if kind == 'qualitative':
        hit = False
        for row in rows['clinical']:
            if not _matches_analyte(row, trigger['analyte']):
                continue
            grade = qualitative_grade(
                _get(row, 'value'), _get(row, 'status'), _get(row, 'referenceRange', 'reference_range')
            )
            if grade is not None and grade >= trigger['min_grade']:
                evidence.add(row, _row_label(row))
                hit = True
        return hit
    if kind == 'psa_ratio':
        total = [(row, _canonical(row, 'tpsa')) for row in rows['clinical'] if _matches_analyte(row, 'tpsa')]
        free = [(row, _canonical(row, 'fpsa')) for row in rows['clinical'] if _matches_analyte(row, 'fpsa')]
        for total_row, total_value in total:
            for free_row, free_value in free:
                if not total_value or not free_value or total_value[0] or free_value[0]:
                    continue
                if total_value[1] > trigger['min_tpsa'] and free_value[1] / total_value[1] < trigger['max_ratio']:
                    evidence.add(total_row, _row_label(total_row))
                    evidence.add(free_row, f'{_row_label(free_row)}（f/t={free_value[1] / total_value[1]:.2f}）')
                    return True
        return False
    if kind == 'grade':
        pattern = _GRADE_PATTERNS[trigger['system']]
        hit = False
        for row in rows['text']:
            text = f'{_norm(_get(row, "name"))} {_norm(_get(row, "value"))}'
            for match in pattern.finditer(text):
                grade = int(match.group(1))
                if trigger['min'] <= grade <= trigger.get('max', 99):
                    evidence.add(row, f'{_norm(_get(row, "name"))}：{match.group(0).strip()}')
                    hit = True
        return hit
    if kind == 'text':
        hit = False
        for row in rows['text']:
            if trigger.get('scope') == 'ecg' and not _ECG_NAME.search(_norm(_get(row, 'name'))):
                continue
            for clause in re.split(r'[，,。；;]', _norm(_get(row, 'value'))):
                match = re.search(trigger['pattern'], clause, re.I)
                if match and not _negated(clause, match):
                    evidence.add(row, f'{_norm(_get(row, "name"))}：{clause.strip()}')
                    hit = True
        return hit
    if kind == 'qtc':
        hit = False
        for row in rows['text']:
            if not _ECG_NAME.search(_norm(_get(row, 'name'))):
                continue
            for match in _QTC.finditer(_norm(_get(row, 'value'))):
                value = int(match.group(1) or match.group(2))
                if value >= trigger['min_ms']:
                    evidence.add(row, f'{_norm(_get(row, "name"))}：QTc {value} ms')
                    hit = True
        return hit
    if kind == 'finding':
        return _finding(trigger, rows, evidence)
    raise ValueError(f'unknown red-flag trigger {kind}')


def _finding(trigger, rows, evidence):
    """A non-negated organ finding, optionally with a minimum size in mm.

    ``exclude`` names a benign qualifier (e.g. calcified). A sentence carrying
    it never triggers; for unsized rules the qualifier anywhere in the same
    record also covers the record's unsized mentions of that finding.
    """
    organ, finding = re.compile(trigger['organ']), re.compile(trigger['finding'])
    exclude = re.compile(trigger['exclude']) if trigger.get('exclude') else None
    hit = False
    for row in rows['text']:
        name = _norm(_get(row, 'name'))
        text = f'{name}：{_norm(_get(row, "value"))}' if _SUMMARY_NAME.search(name) else _norm(_get(row, 'value'))
        record_qualified = exclude is not None and bool(exclude.search(text))
        if trigger.get('graded') and re.search(trigger['graded'], text, re.IGNORECASE):
            continue
        for sentence in re.split(r'[。；;\n]', text):
            if not organ.search(sentence) or not finding.search(sentence):
                continue
            if exclude is not None and exclude.search(sentence):
                continue
            if not any(
                (match := finding.search(clause)) and not _negated(clause, match) for clause in _clauses(sentence)
            ):
                continue
            sizes = _sizes_mm(sentence)
            if 'min_mm' in trigger:
                if not sizes:
                    continue
                size = max(sizes)
                if size < trigger['min_mm'] or (trigger.get('strict') and size == trigger['min_mm']):
                    continue
            elif record_qualified and not sizes:
                continue
            evidence.add(row, f'{name}：{sentence.strip()}')
            hit = True
    return hit


def _applies(condition, ctx):
    if not condition:
        return True
    if 'any' in condition:
        return any(_applies(item, ctx) for item in condition['any'])
    if condition.get('sex') and ctx['sex'] not in {None, condition['sex']}:
        return False
    if condition.get('menopause') is True and (ctx['sex'] == 'male' or not ctx['postmenopausal']):
        return False
    if condition.get('min_age') is not None and (ctx['age'] is None or ctx['age'] < condition['min_age']):
        return False
    if condition.get('max_age') is not None and (ctx['age'] is None or ctx['age'] > condition['max_age']):
        return False
    return True


def _rows(facts):
    clinical = [row for row in facts if _get(row, 'kind') == 'clinical']
    return {'clinical': clinical, 'text': clinical}


def evaluate_trigger(trigger, facts, ctx):
    """Evaluate one rule trigger; return ``(matched, factIds, notes)``."""
    evidence = _Evidence()
    matched = _evaluate(trigger, _rows(list(facts or ())), ctx, evidence)
    return matched, list(evidence.fact_ids), list(evidence.notes)


def applies(condition, ctx):
    """Public form of the sex/age/menopause applicability check."""
    return _applies(condition, ctx)


def _source_list(keys):
    return [{'key': key, **deepcopy(SOURCES[key])} for key in keys]


def interval_from_advice(lines):
    """``{'text', 'days'}`` from the first explicit follow-up interval, else ``None``."""
    for line in lines or ():
        text = _norm(line)
        if not _FOLLOWUP_ADVICE.search(text):
            continue
        match = _INTERVAL.search(text)
        if not match:
            continue
        raw = match.group(1)
        if raw == '半':
            amount = 0.5
        elif re.fullmatch(r'\d+(?:\.\d+)?', raw):
            amount = float(raw)
        else:
            amount = 0
            if raw.startswith('十'):
                amount = 10 + _CN_NUMBERS.get(raw[1:], 0) if len(raw) > 1 else 10
            elif '十' in raw:
                head, _, tail = raw.partition('十')
                amount = _CN_NUMBERS.get(head, 0) * 10 + _CN_NUMBERS.get(tail, 0)
            else:
                amount = _CN_NUMBERS.get(raw, 0)
        unit = match.group(2)
        days = {'天': 1, '日': 1, '周': 7, '星期': 7, '月': 30, '年': 365}[unit] * amount
        if days <= 0:
            continue
        return {'text': match.group(0).strip(), 'days': int(round(days))}
    return None


def department_from_advice(lines):
    for line in lines or ():
        text = _norm(line)
        positions = [(text.find(name), name) for name in _DEPARTMENTS if name in text]
        if positions:
            return min(positions)[1]
        match = re.search(r'([一-龥]{2,5}专科)', text)
        if match:
            return re.sub(r'^(?:建议|请|至|到|去|于|并|及|和)', '', match.group(1))
    return None


def _problem_links(rule, fact_ids, problems):
    topic = re.compile(rule['topic'], re.I) if rule.get('topic') else None
    owned = set(fact_ids)
    linked = []
    for problem in problems:
        identifiers = set(problem.get('factIds') or ())
        if not identifiers.intersection(owned):
            continue
        if topic is not None and not topic.search(_norm(problem.get('name'))):
            continue
        linked.append(problem)
    return linked


def _prefer_direct(fact_ids, facts_by_id):
    direct = [fid for fid in fact_ids if not _SUMMARY_NAME.search(_norm(_get(facts_by_id.get(fid, {}), 'name')))]
    return direct or list(fact_ids)


def evaluate_red_flags(facts, subject=None, problems=()):
    """Return ``{'priorityItems': [...], 'routineFollowups': [...], 'context': {...}}``.

    ``facts`` are source fact rows (``factId``/``kind``/``name``/``value``/``unit``/
    ``status``/``referenceRange``); ``problems`` are clinical problems with
    ``code``/``name``/``factIds``/``originalAdvice`` in the same identifiers.
    """
    facts = list(facts or ())
    facts_by_id = {_fact_id(row): row for row in facts if _fact_id(row) is not None}
    problems = [problem for problem in problems or () if isinstance(problem, dict)]
    ctx = subject_context(subject, facts)
    rows = _rows(facts)
    matched = {}
    for rule in RULES:
        if not _applies(rule.get('applies'), ctx):
            continue
        evidence = _Evidence()
        if not _evaluate(rule['trigger'], rows, ctx, evidence) or not evidence.fact_ids:
            continue
        matched.setdefault(rule['tier'], []).append((rule, evidence))
    priority, used_groups = [], {}
    for rule, evidence in matched.get('P0', ()):
        current = used_groups.get(rule['group'])
        if current is not None and current['timeframe']['days'] <= rule['timeframe']['days']:
            continue
        fact_ids = _prefer_direct(evidence.fact_ids, facts_by_id)
        links = _problem_links(rule, evidence.fact_ids, problems)
        item = {
            'code': rule['id'],
            'ruleId': rule['id'],
            'title': rule['title'],
            'priority': 'P0',
            'timeframe': {**deepcopy(rule['timeframe']), 'basis': 'program_default'},
            'department': rule['department'],
            'checks': list(rule['checks']),
            'factIds': fact_ids,
            'problemCodes': [problem['code'] for problem in links if problem.get('code')],
            'evidenceText': list(evidence.notes),
            'sources': [*_source_list(rule['sources']), {'key': 'program_default', **PROGRAM_DEFAULT}],
            'origin': 'red_flag_rule',
        }
        if current is not None:
            priority.remove(current)
        used_groups[rule['group']] = item
        priority.append(item)
    covered_problems = {code for item in priority for code in item['problemCodes']}
    for problem in problems:
        advice = [line for line in problem.get('originalAdvice') or () if isinstance(line, str)]
        urgent = [
            line for line in advice if _URGENT_ADVICE.search(_norm(line)) and not _NEGATED_ADVICE.search(_norm(line))
        ]
        if not urgent or problem.get('code') in covered_problems:
            continue
        clinical = [fid for fid in problem.get('factIds') or () if _get(facts_by_id.get(fid, {}), 'kind') == 'clinical']
        if not clinical:
            continue
        priority.append(
            {
                'code': 'RF-ADVICE-URGENT:' + str(problem.get('code')),
                'ruleId': 'RF-ADVICE-URGENT',
                'title': _norm(problem.get('name')) or '原检查建议尽快就诊',
                'priority': 'P0',
                'timeframe': {'text': '尽快就诊（原检查建议）', 'days': 14, 'basis': 'original_advice'},
                'department': department_from_advice(urgent) or '相关专科',
                'checks': [_norm(line) for line in urgent],
                'factIds': _prefer_direct(clinical, facts_by_id),
                'problemCodes': [problem['code']] if problem.get('code') else [],
                'evidenceText': [_norm(line) for line in urgent],
                'sources': [{'key': 'original_advice', 'title': '原检查建议', 'quote': _norm(urgent[0])}],
                'origin': 'original_advice',
            }
        )
    priority.sort(key=lambda item: (item['timeframe']['days'], item['code']))
    p0_groups = {
        rule['group'] for rule, _ in matched.get('P0', ()) if any(item['ruleId'] == rule['id'] for item in priority)
    }
    routine, routine_groups = [], set()
    for rule, evidence in matched.get('routine', ()):
        if rule['group'] in p0_groups or rule['group'] in routine_groups:
            continue
        routine_groups.add(rule['group'])
        links = _problem_links(rule, evidence.fact_ids, problems)
        advice = [line for problem in links for line in problem.get('originalAdvice') or () if isinstance(line, str)]
        interval = interval_from_advice(advice)
        interval = (
            {**interval, 'basis': 'original_advice'}
            if interval
            else {**deepcopy(rule['interval']), 'basis': 'rule_default'}
        )
        routine.append(
            {
                'code': rule['id'],
                'ruleId': rule['id'],
                'title': rule['title'],
                'interval': interval,
                'department': department_from_advice(advice) or rule['department'],
                'factIds': _prefer_direct(evidence.fact_ids, facts_by_id),
                'problemCodes': [problem['code'] for problem in links if problem.get('code')],
                'originalAdvice': list(dict.fromkeys(_norm(line) for line in advice)),
                'evidenceText': list(evidence.notes),
                'sources': _source_list(rule['sources']),
                'origin': 'routine_rule',
            }
        )
    return {
        'version': RULES_VERSION,
        'priorityItems': priority,
        'routineFollowups': routine,
        'context': ctx,
    }


def advice_followups(problems, facts, *, exclude_problem_codes=()):
    """Routine follow-ups that exist only as original advice on a clinical problem."""
    facts_by_id = {_fact_id(row): row for row in facts or () if _fact_id(row) is not None}
    excluded = set(exclude_problem_codes)
    result = []
    for problem in problems or ():
        code = problem.get('code')
        if code in excluded:
            continue
        advice = [_norm(line) for line in problem.get('originalAdvice') or () if isinstance(line, str)]
        advice = [line for line in advice if line and (_FOLLOWUP_ADVICE.search(line) or department_from_advice([line]))]
        clinical = [fid for fid in problem.get('factIds') or () if _get(facts_by_id.get(fid, {}), 'kind') == 'clinical']
        if not advice or not clinical:
            continue
        interval = interval_from_advice(advice)
        result.append(
            {
                'code': 'advice:' + str(code),
                'ruleId': 'RT-ORIGINAL-ADVICE',
                'title': _norm(problem.get('name')),
                'interval': {**interval, 'basis': 'original_advice'}
                if interval
                else {'text': '按原检查建议', 'days': None, 'basis': 'original_advice_without_interval'},
                'department': department_from_advice(advice),
                'factIds': _prefer_direct(clinical, facts_by_id),
                'problemCodes': [code] if code else [],
                'originalAdvice': list(dict.fromkeys(advice)),
                'evidenceText': [],
                'sources': [{'key': 'original_advice', 'title': '原检查建议', 'quote': advice[0]}],
                'origin': 'original_advice',
            }
        )
    return result


def rule_table():
    """The rule rows with resolved sources, for documentation and audit."""
    return [{**deepcopy(rule), 'sources': _source_list(rule['sources'])} for rule in RULES]
