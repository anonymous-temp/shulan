"""Project confirmed clinical observations and reported symptoms for template 01."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal
from decimal import InvalidOperation
from types import SimpleNamespace

from .assessment_clinical_rules import LEVEL_LABELS
from .assessment_clinical_rules import SubjectContext
from .assessment_clinical_rules import annotate_rows
from .assessment_clinical_rules import judge
from .assessment_clinical_rules import subject_context
from .assessment_clinical_rules import summarize_levels
from .assessment_clinical_rules import worst_level
from .assessment_reading_labels import clinical_observation_display_name as clinical_observation_display_name
from .clinical_report import _SYMPTOM_LABEL
from .clinical_report import _denied_title
from .clinical_report import _hospital_row_classifier
from .clinical_report import _problem_source_quote
from .clinical_report import _source_lesion_measurement_quotes
from .clinical_report import explicit_reported_options
from .clinical_report import printed_clinical_unit
from .clinical_report import reported_fact_kind
from .clinical_report import reported_symptom_fragments
from .clinical_source_projection import _finding_name
from .clinical_source_projection import ensure_patient_projection_index
from .clinical_source_projection import get_patient_projection
from .clinical_source_projection import get_patient_rows
from .clinical_source_projection import patient_reference_context
from .evaluation_builder import _Observation
from .evaluation_builder import _printed_reference_relation
from .evaluation_builder import _qualitative_polarity
from .evaluation_builder import _status_state
from .evaluation_builder import _urine_microscopy_not_seen
from .evaluation_builder import original_medical_advice_witnesses
from .evaluation_derived_indices import derived_index_context
from .evaluation_derived_indices import is_derived_index
from .evaluation_sources import _comparison_key
from .evaluation_sources import _row_subject
from .evaluation_sources import _time
from .evaluation_sources import subject_mismatch
from .evaluation_sources import urine_item_label
from .schemas import SubjectInfo

_BODY_GROUPS = (
    (
        'weightWaist',
        '体重指数（BMI）与腰围',
        re.compile(
            r'体重指数|体质指数|^BMI(?![A-Za-z])|^菌群送检登记 BMI$|腰围|^臀围|腰臀比|腰高比|^身高|^体重|\bWHR\b',
            re.I,
        ),
    ),
    (
        'bodyComposition',
        '深度人体成分核心指标',
        re.compile(
            r'人体成分|体成分|\bInBody\s*\d*\b|体脂|脂肪量|内脏脂肪|皮下脂肪|'
            r'骨骼肌(?:量|质量|指数)|肌肉量|去脂体重|瘦体重|基础代谢|细胞外水(?:分|比)|'
            r'细胞内水分|身体总水分|身体水分|总水分|骨矿物质|身体细胞量|上臂围度|生物电阻抗|'
            r'(?<![A-Za-z])(?:ECW/TBW|VFA|BMR|PBF|SMI|FFM|SMM)(?![A-Za-z])',
            re.I,
        ),
    ),
    (
        'restingVitals',
        '静息血压与心率基线',
        re.compile(r'血压|收缩压|舒张压|心率(?!变异)|^脉搏(?:次数|频率|\s|$)', re.I),
    ),
)
_CLINICAL_GROUPS = (
    (
        'cardiometabolic',
        '糖脂代谢、胰岛素敏感性与心血管系统评估',
        re.compile(
            r'血糖|葡萄糖|糖化|胰岛素|c[ -]?肽|homa|低密度|高密度|胆固醇|甘油三酯|载脂蛋白|脂蛋白\s*\(?a\)?|'
            r'血脂|心电图|心脏|心肌|心动图|左心|右心|心室|心房|左室|右室|左房|右房|瓣膜|[二三]尖瓣|主动脉瓣|肺动脉瓣|射血分数|'
            r'血压|心率|脉搏|颈动脉|颈部血管|颈[总内外]动脉|椎动脉|下肢动脉彩超|'
            r'钠尿肽|肌酸激酶|肌钙蛋白|肌红蛋白|乳酸脱氢酶|'
            r'(?<![A-Za-z])(?:CK(?:-?MB)?|LDH|cTn[IT]|BNP|NT-proBNP|OGTT|ECG|EKG)(?![A-Za-z])',
            re.I,
        ),
    ),
    (
        'organMetabolism',
        '肝胆胰消化、肾脏代谢与营养辅酶状态评估',
        re.compile(
            r'肝脏|脂肪肝|肝胆|(?<![乙丙甲戊型])肝(?!炎病毒)|胆囊|胆管|胆汁酸|胆红素|转氨酶|氨基(?:酸)?转移酶|'
            r'谷氨酰(?:基转移|转肽)酶|胆碱酯酶|'
            r'肌酐|尿素|尿酸|肾(?!上腺)|尿常规|胃|肠镜|粪便|大便|肛门|直肠|胰腺|淀粉酶|脂肪酶|'
            r'幽门螺杆菌|碳\s*(?:13|14).*呼气|尿素呼气|腹部(?:彩超|超声|CT)|'
            r'总蛋白|白蛋白|(?<!免疫)(?<!甲状腺)球蛋白|白球蛋白比|碱性磷酸酶|肝纤维化|甘脯二肽|岩藻糖苷酶|腺苷脱氨酶|'
            r'电解质|^[*＊]?\s*(?:血清|血浆|血)?(?:钠|钾|氯|钙|镁|磷)(?:离子)?\s*(?:[（(][A-Za-z+]{1,4}[)）])?\s*$|'
            r'(?<![A-Za-z])(?:AST|ALT|GGT|ALP|FIB[- ]?4|eGFR)(?![A-Za-z])|维生素(?!\s*b\s*12)',
            re.I,
        ),
    ),
    (
        'hematologyImmune',
        '血液学、铁代谢储备与一碳甲基化通路评估',
        re.compile(
            r'血常规|红细胞|血红蛋白|白细胞|血小板|淋巴细胞|单核细胞|粒细胞|铁蛋白|转铁蛋白|血清铁|同型半胱氨酸|'
            r'叶酸|维生素\s*b\s*12|c[ -]?反应蛋白|免疫球蛋白|类风湿因子|抗链球菌溶血素|'
            r'炎症|凝血|纤维蛋白原|D-?二聚体|血型|血沉|红细胞沉降率|降钙素原|补体|抗核抗体|抗双链|抗环瓜氨酸|'
            r'乙肝|乙型肝炎|丙肝|丙型肝炎|表面抗原|表面抗体|e抗原|e抗体|核心抗体|艾滋|梅毒|人类免疫缺陷|病毒|'
            r'结核|支原体|衣原体|过敏原|变应原|总IgE|乳头瘤|感染|'
            r'(?<![A-Za-z])(?:MCV|MCHC?|RDW|HCY|IgG|IgA|IgM|IgE|RF|ASO|ESR|PCT|ANA|ENA|HBsAg|HBsAb|HBeAg|HBeAb|'
            r'HBcAb|HCV|HIV|TPPA|TRUST|RPR|HPV|EBV|CMV|T-SPOT|APTT|INR|PT|TT|FIB|DD)(?![A-Za-z])',
            re.I,
        ),
    ),
    (
        'endocrineMusculoskeletal',
        '内分泌激素、骨骼肌肉与器官影像学评估',
        re.compile(
            r'甲状腺|甲状旁腺|甲状原氨酸|甲功|雌二醇|孕酮|睾酮|性激素|皮质醇|骨密度|骨骼|颈椎|胸椎|腰椎|椎间盘|肌量|'
            r'关节|肾上腺|泌乳素|促性腺激素|促卵泡刺激素|促黄体生成素|乳腺|钼靶|双乳|左乳|右乳|肺|前列腺|胸部|'
            r'经腹妇科|妇科|子宫|卵巢|附件|宫颈|阴道|白带|液基细胞|盆腔(?:超声|彩超|影像)|睾丸|精索|阴囊|'
            r'纵隔|胸片|头颅|颅脑|垂体|骨|肌|脊柱|四肢|淋巴结|皮肤|耳|鼻|咽|喉|扁桃体|口腔|牙|视力|屈光|眼|'
            r'内科|外科|查体|体格检查|一般情况|绒毛膜促性腺|抗缪勒管激素|抗苗勒管激素|抑制素|'
            r'(?<![A-Za-z])(?:TSH|FT[34]|T[34]|FSH|LH|PRL|AMH|TCT|TPOAb|TGAb|TRAb|DR|CT|MRI)(?![A-Za-z0-9])',
            re.I,
        ),
    ),
)
_CLINICAL_PANELS = {
    'cardiometabolic': (
        ('insulinSensitivity', '稳态胰岛素抵抗指数（HOMA-IR）', r'HOMA|稳态.*胰岛素抵抗'),
        ('fastingInsulin', '胰岛素与 C 肽', r'空腹胰岛素|(?:^|空腹)C[ -]?肽|^C[ -]?peptide|^胰岛素(?:INS)?$'),
        ('glycemia', '糖化血红蛋白与血糖', r'糖化|血糖|葡萄糖|糖耐量|OGTT'),
        ('triglycerides', '甘油三酯与相关指标', r'甘油三酯|^TG(?![A-Za-z])|残余胆固醇'),
        (
            'cholesterol',
            '胆固醇与载脂蛋白',
            r'胆固醇|[高低]密度脂蛋白|载脂蛋白|脂蛋白\s*\(?a\)?|^Apo|\b(?:LDL|HDL)(?:-?C)?\b|^TC$',
        ),
        ('cardiacMarkers', '心肌损伤相关检查', r'肌酸激酶|肌钙蛋白|乳酸脱氢酶|\b(?:CK(?:-?MB)?|LDH|cTn[IT])\b'),
        (
            'cardiac',
            '心脏相关检查',
            r'心电图|心脏|心动图|心肌|心室|心房|左室|右室|左房|右房|瓣膜|[二三]尖瓣|主动脉瓣|肺动脉瓣|射血分数|'
            r'钠尿肽|(?:NT-pro)?BNP|ECG|EKG',
        ),
        ('arterial', '颈动脉与外周动脉彩超', r'颈动脉|颈部血管|颈[总内外]动脉|椎动脉|下肢动脉彩超'),
    ),
    'organMetabolism': (
        ('stool', '粪便常规与隐血试验', r'粪便|大便|^隐血|镜检|^性状$|^颜色$|虫卵'),
        (
            'abdominalImaging',
            '腹部影像与肝胆胰脾肾所见',
            r'腹部|肝胆|彩超|脂肪肝|肝囊肿|肝血管瘤|肝内|胆囊|胰腺|脾|肾(?:脏)?(?:囊肿|结石|结晶)',
        ),
        ('cholinesterase', '血清胆碱酯酶', r'胆碱酯酶|^ChE$'),
        ('aminotransferases', '转氨酶与肝脏相关指标', r'转氨酶|氨基(?:酸)?转移酶|AST|ALT|肝纤维化|FIB[- ]?4'),
        (
            'hepatobiliaryLab',
            '肝胆胰生化与蛋白指标',
            r'胆红素|胆汁酸|谷氨酰(?:基转移|转肽)酶|磷酸酶|GGT|ALP|淀粉酶|脂肪酶|'
            r'蛋白|甘脯二肽|岩藻糖苷酶|腺苷脱氨酶',
        ),
        ('urinalysis', '尿常规与尿沉渣检查', r'尿(?:常规|液|沉渣|检)'),
        ('renal', '肾功能及相关代谢指标', r'肌酐|尿素(?!.*呼气)|尿酸|肾(?!上腺)|^pH$|eGFR'),
        ('electrolytes', '电解质及矿物质检查',
         r'电解质|^[*＊]?\s*(?:血清|血浆|血)?(?:钠|钾|氯|钙|镁|磷)(?:离子)?'
         r'\s*(?:[（(][A-Za-z+]{1,4}[)）])?\s*$'),
        ('helicobacter', '尿素呼气试验与幽门螺杆菌', r'呼气|幽门螺杆菌|^HP$'),
    ),
    'hematologyImmune': (
        ('iron', '血清铁蛋白与铁代谢', r'铁蛋白|转铁蛋白|^血清铁|^Ferritin$'),
        (
            'redCellIndices',
            '红细胞体积与分布及相关营养检查',
            r'平均红细胞|红细胞分布|MCV|MCH|RDW|叶酸|维生素\s*B\s*12|同型半胱氨酸',
        ),
        ('bloodCount', '血细胞计数与分类', r'血常规|全血细胞|白细胞|淋巴细胞|单核细胞|粒细胞|血红蛋白|红细胞|血小板'),
        (
            'immuneInflammation',
            '体液免疫、风湿免疫与炎症',
            r'免疫球蛋白|类风湿因子|抗链球菌溶血素|反应蛋白|炎症|补体|抗核抗体|抗双链|抗环瓜氨酸|血沉|红细胞沉降率|'
            r'(?<![A-Za-z])(?:RF|ASO|Ig[AGM]|hs[- ]?CRP|ESR|ANA|ENA)(?![A-Za-z])',
        ),
        (
            'infection',
            '感染与免疫学筛查',
            r'乙肝|乙型肝炎|丙肝|丙型肝炎|表面抗原|表面抗体|e抗原|e抗体|核心抗体|艾滋|梅毒|人类免疫缺陷|病毒|结核|'
            r'支原体|衣原体|过敏原|变应原|IgE|乳头瘤|降钙素原|感染|'
            r'(?<![A-Za-z])(?:HBsAg|HBsAb|HBeAg|HBeAb|HBcAb|HCV|HIV|TPPA|TRUST|RPR|HPV|EBV|CMV|T-SPOT|PCT)(?![A-Za-z])',
        ),
        (
            'coagulation',
            '凝血功能与血型',
            r'凝血|纤维蛋白原|D-?二聚体|血型|(?<![A-Za-z])(?:APTT|INR|PT|TT|FIB|DD|ABO|Rh)(?![A-Za-z])',
        ),
    ),
    'endocrineMusculoskeletal': (
        (
            'thyroid',
            '甲状腺彩超与甲状腺功能',
            r'甲状腺|甲功|甲状原氨酸|(?<![A-Za-z])(?:TSH|FT[34]|T[34])(?![A-Za-z0-9])',
        ),
        (
            'sexHormones',
            '性激素检查',
            r'性激素|孕酮|睾酮|雌二醇|泌乳素|促卵泡刺激素|促黄体生成素|绒毛膜促性腺|\b(?:FSH|LH|PRL|[βb]?-?HCG)\b',
        ),
        ('spinalImaging', '脊柱影像与骨关节所见', r'(?:颈|胸|腰)椎|椎间盘|脊柱|关节'),
        ('boneDensity', '骨密度测量', r'骨密度|T[- ]?Score|Z[- ]?Score'),
        ('breastImaging', '乳腺检查', r'乳腺|钼靶|双乳|左乳|右乳'),
        (
            'gynecologicImaging',
            '妇科检查与影像',
            r'妇科|子宫|卵巢|附件|宫颈|阴道|白带|液基细胞|TCT|盆腔(?:超声|彩超|影像)',
        ),
        (
            'otherImaging',
            '胸部及其他器官影像',
            r'胸部|肺|前列腺|纵隔|胸片|头颅|颅脑|垂体|睾丸|精索|阴囊|\b(?:CT|MRI|DR)\b',
        ),
        ('oralEye', '口腔与眼科查体', r'口腔|牙周|牙石|龋齿|牙|屈光|视力|眼'),
        ('physicalExam', '耳鼻喉、皮肤与一般查体', r'耳|鼻|咽|喉|扁桃体|皮肤|淋巴结|内科|外科|查体|体格检查|一般情况'),
    ),
    # 3.5: device studies stay in specialtyFindings; anything without a fixed system ends in "其他检查".
    'specialty': (
        ('arterial', '四肢动脉硬化检测', r'动脉硬化|四肢动脉|腕踝|踝肱|ABI|baPWV|PWV'),
        ('cerebrovascular', '脑血管功能检测', r'脑血流|脑血管|颅多普勒|TCD'),
        ('diabetesScreening', '糖尿病风险筛查', r'eZscan|糖尿病|糖代谢|外汗腺'),
    ),
    'otherClinical': (('otherExams', '其他检查', r''),),
}
_GROUP_NAMES = {
    **{code: name for code, name, _ in _CLINICAL_GROUPS},
    'specialty': '专项检测与筛查',
    'otherClinical': '其他检查',
}
_DEVICE_STUDY = re.compile(
    r'脑血流|脑血管(?!成像|造影)|颅多普勒|(?<![A-Za-z])(?:TCD|ABI|baPWV|PWV|eZscan)(?![A-Za-z])|动脉硬化|四肢动脉|腕踝|踝肱|'
    r'早期糖(?:代谢|尿病)(?:风险|筛查)|糖尿病(?:早期)?风险筛查|外汗腺',
    re.I,
)
_HCG = re.compile(r'人绒毛膜促性腺激素|绒毛膜促性腺|(?<![A-Za-z])(?:[βb][-\s]?)?HCG(?![A-Za-z])', re.I)
_DIGESTIVE_GROUPS = (
    (
        'oralGastric',
        '口腔与上消化道屏障（口-胃轴）',
        re.compile(r'口腔|口咽|牙周|牙石|龋齿|胃镜|胃|幽门螺杆菌|碳\s*(?:13|14).*呼气|尿素呼气|反流|食管'),
    ),
    (
        'hepatobiliaryPancreatic',
        '肝胆胰消化液与胆汁酸循环（肠-肝轴）',
        re.compile(r'肝|胆囊|胆管|胆汁酸|胰腺|淀粉酶|脂肪酶', re.I),
    ),
    ('lowerDigestive', '下消化道黏膜与器质完整性', re.compile(r'粪便|大便|结肠|肠镜|肛门|肛肠|肛检|直肠|痔')),
)
_DIGESTIVE_FUNCTION_MEASUREMENT = re.compile(
    r'呼气试验|幽门螺杆菌|^HP$|胃蛋白酶|胃泌素|胃酸|胆汁酸|淀粉酶|脂肪酶|(?:粪便|大便)性状', re.I
)
_SPECIALTY = re.compile(
    r'功能医学|遗传易感|疾病易感基因|遗传检测|器官年龄|器官生物年龄|表观遗传年龄|生物学年龄|'
    r'细胞内微量营养素|唾液.*皮质醇|食物(?:不耐受|敏感).*IgG',
    re.I,
)
_SCREENING_BASELINE = re.compile(
    r'肿瘤标志物|甲胎蛋白|癌胚抗原|糖[链类]抗原|前列腺特异性抗原|细胞角蛋白片段|肿瘤细胞|肿瘤早筛|泛癌早筛|'
    r'微小核酸|microRNA|'
    r'(?<![A-Za-z])(?:AFP|CEA|CYFRA\s*21-1|NSE|SCC|TPSA|FPSA|'
    r'CA\s*(?:19-9|125|15-3|72-4|50|242))(?![A-Za-z])',
    re.I,
)
_NORMAL_BASELINE_ROLES = (
    ('hepatic', re.compile(r'白蛋白|总胆红素|丙氨酸.*转移酶|(?<![A-Za-z])(?:ALT|Alb)(?![A-Za-z])', re.I)),
    ('pancreatic', re.compile(r'淀粉酶|脂肪酶')),
    ('renal', re.compile(r'肌酐|肾小球滤过|eGFR|GFR', re.I)),
    ('urinary', re.compile(r'^尿(?:液)?(?:隐血|潜血|蛋白(?:质)?)$')),
    ('cardiac', re.compile(r'心电图|钠尿肽|(?:NT-pro)?BNP', re.I)),
    ('arterial', re.compile(r'颈动脉|四肢动脉|动脉硬化|踝肱|腕踝|\b(?:ABI|baPWV|PWV)\b', re.I)),
    ('bloodCount', re.compile(r'^(?:血红蛋白|白细胞计数|血小板计数)(?:[（(].*)?$')),
    ('humoral', re.compile(r'免疫球蛋白|\bIg[AGM]\b', re.I)),
    ('endocrine', re.compile(r'促甲状腺激素|游离.*甲状(?:原氨酸|腺素)|\b(?:TSH|FT[34])\b', re.I)),
    ('bone', re.compile(r'骨密度')),
    ('organImaging', re.compile(r'腹部.*(?:超声|彩超)|肝胆胰脾.*(?:超声|彩超)|甲状腺.*(?:超声|彩超)|胸部.*CT|肺')),
)
# Sex-specific organ studies that also count as a confirmed baseline.
_SEXED_ORGAN_IMAGING = {
    'male': re.compile(r'前列腺|睾丸|精索|阴囊'),
    'female': re.compile(r'乳腺|钼靶|双乳|左乳|右乳|妇科|子宫|卵巢|附件|宫颈|液基细胞|TCT|HPV|乳头瘤'),
}
_ACTUAL_SPECIALTY = re.compile(
    r'人体成分(?:分析|测定|检测)|体成分(?:分析|测定|检测)|InBody|骨密度|脑血管功能|'
    r'四肢动脉|动脉硬化|eZscan|功能医学|遗传(?:检测|易感)|生物(?:学)?年龄|'
    r'超声|彩超|心电图|细胞病理|细胞学|微小核酸|microRNA|'
    r'(?:^|[^A-Za-z])(?:CT|MRI|DR)(?![A-Za-z])|核磁|胃镜|肠镜',
    re.I,
)
_RECOMMENDATION_RECORD = re.compile(r'设备.*(?:建议原文|目标建议)|(?:原报告|设备)建议原文')
_SOURCE_BACKGROUND_RECORD = re.compile(r'病史记录|生活方式记录|拒检项目|设备建议|目标建议')
_SOURCE_CONTEXT_RECORD = re.compile(r'登记|生物电阻抗原始表|原始异常箭头记录|主检(?:结论|意见|建议)|健康教育')
_EXAM_NAME_SUFFIX = re.compile(r'(?:影像)?原始所见|原始报告|报告原文|检查结论|检测结论|原文|所见')
_METHOD_HINTS = (
    ('composition', re.compile(r'人体成分|体成分|InBody', re.I)),
    ('electrocardiogram', re.compile(r'心电图|\bECG\b', re.I)),
    ('ultrasound', re.compile(r'超声|彩超')),
    ('computedTomography', re.compile(r'(?<![A-Za-z])CT(?![A-Za-z])', re.I)),
    ('magneticResonance', re.compile(r'核磁|(?<![A-Za-z])MRI(?![A-Za-z])', re.I)),
    ('arterialFunction', re.compile(r'四肢动脉|动脉硬化|\b(?:ABI|baPWV)\b', re.I)),
    ('cerebrovascularFunction', re.compile(r'脑血管功能|脑血流|\bTCD\b', re.I)),
    ('electrochemicalScreening', re.compile(r'eZscan', re.I)),
)
_UNAVAILABLE_MARKS = frozenset({'', '-', '--', '—', '/', '未检', '未检查', '未检测', '未提供', '无数据'})
_NUMBER = re.compile(r'^([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)\s*(.*)$')
_SYMPTOM_RECORD = re.compile(r'^(?:门诊|入院|临床|既往|病历)?(?:主诉|症状|就诊原因|病情描述)$')
_BOWEL_SYMPTOM = re.compile(r'便秘|腹泻|排便(?:困难|不畅|费力)|大便(?:干结|带血)|便血|血便|黑便')
_SYMPTOM_FAMILIES = (
    (
        'bowel',
        '排便困难或粪便偏硬',
        re.compile(
            r'便秘|(?:粪便|大便|排便)[^，,；;。]{0,8}(?:偏硬|干硬|干结|费力|困难|不畅|偏少|减少)|'
            r'硬便|硬块便|排便间隔延长'
        ),
    ),
    ('nasal', '鼻部不适', re.compile(r'鼻部不适|鼻塞|鼻痒|喷嚏|流(?:清)?鼻涕|流清涕')),
    ('reflux', '反酸或烧心', re.compile(r'反酸|烧心|反流')),
    ('shoulderNeck', '肩颈疼痛', re.compile(r'(?:肩颈|颈肩|颈部|颈椎|肩部)[^，。；]{0,4}(?:痛|不适|酸|僵|紧)')),
)
_BOWEL_FREQUENCY = re.compile(
    r'每(?:[一二两三四五六七八九十\d]+)?[周天日月](?:排便)?(?:仅|约)?[一二两三四五六七八九十\d]+次'
)
_SYMPTOM_TIMING = re.compile(
    r'近[一二两三四五六七八九十\d]+(?:个)?(?:月|周|天|日|年)|入园初期|换季(?:时)?|'
    r'\d{4}[-年/.]\d{1,2}[-月/.]\d{1,2}日?'
)
_NON_ASSERTION = re.compile(r'^(?:否认|没有|未见|未诉|无(?!力|法)|希望|打算|计划|想改善|目标)')
_STOOL_OCCULT = re.compile(r'^(?:粪便|大便)(?:隐血|潜血)(?:试验|检测)?$')
_URINE_SCREENING_ASSAY = re.compile(
    r'^尿(?:液)?(?:隐血|潜血|蛋白(?:质)?|亚硝酸盐|(?:葡萄)?糖|酮体|胆红素|(?:尿)?胆原|白细胞[酯脂]酶|维生素C)$',
    re.I,
)
_SHOULDER_EXAM = re.compile(r'^脊柱|颈椎|^肩关节|^肩部查体|^神经反射|^腱反射|^四肢关节')
_COMPOSITE_PHYSICAL_EXAM = re.compile(r'^体格检查|^(?:内科|外科)(?:查体|检查)|^五官(?:口腔)?检查')
_SCOPED_PHYSICAL_HEADING = re.compile(r'^(?:脊柱|颈椎|肩关节|四肢关节|神经反射|腱反射)')
_SCOPED_PHYSICAL_RESULT = re.compile(r'^(?:无殊|正常|无明显异常|未见明显异常|活动自如|生理曲度)')
_STOOL_FORM = re.compile(r'(?:粪便|大便)(?:性状|形态)|^性状$')
_SYMPTOM_CLAUSE = re.compile(r'[；;。！？!?\n，,、]+')
_SYMPTOM_CONTEXT_TIME = re.compile(r'(?:留取|采集|检查前|检查后)[^，,。；？?]{0,22}?(?:时|期间)(?=[，,。；？?]|$)')
_ASSAY_RESULT = re.compile(
    r'(?P<polarity>(?:弱|强)?阳性|阴性)\s*(?:[A-Za-z][A-Za-z0-9-]{0,11}\s*)?(?P<symbol>\([-+−]\))?',
    re.I,
)
_EXAM_CONCLUSION = re.compile(
    r'(?:^|[。.;；\n])\s*(?:诊断提示|诊断意见|诊断结论|检查结论|超声提示|病理诊断|结论)'
    r'\s*[:：]\s*([^。\n]+)[。.]?$'
)
_COMBINED_EXAM_NAME = re.compile(
    r'^(?:体检)?(?:超声|影像(?:学|检查)?)[与及和]心电(?:图|检查)?(?:结果|报告|检查|所见)$',
)
_EXAM_SUBHEADING = re.compile(
    r'(?:甲状腺(?:\s*[+＋及和]\s*(?:颈部)?淋巴结|(?:彩超|超声|影像)(?:检查)?)'
    r'|(?:上|下|全)?腹部(?:彩超|超声|CT)(?:检查)?|肝胆胰脾(?:彩超|超声)(?:检查)?'
    r'|(?:经腹|经阴道)妇科(?:彩超|超声|检查)?|妇科(?:彩超|超声|影像)(?:检查)?'
    r'|心电图(?:检查|[（(]\s*\d+\s*导联\s*[）)])?)',
    re.I,
)
_EXAM_SIGNATURE = re.compile(r'^(?:审核|报告|检查|签发)(?:日期|时间|医生|医师|人员)\s*[:：]|^心电结果$')
_ARTERIAL_METRIC = re.compile(r'(?<![A-Za-z])(?:ABI|baPWV|PWV)(?![A-Za-z])', re.I)
_STATED_NORMAL_RANGE = re.compile(
    r'(?:报告)?(?:标为|判为|判定为|在|均在|处于|属(?:于)?)(?:正常|标准)(?:范围(?:内)?)?'
    r'(?:\s*\(\s*([\d.]+\s*[-~～—–至]\s*[\d.]+)\s*\))?'
)
_BODY_FLAG_STATES = frozenset({'超重', '肥胖', '中心型肥胖', '腹型肥胖', '低体重'})
_COMPOSITION_COMPOSITE = re.compile(r'^(?:人体成分|体成分)(?:分析|测定|检测)?$|^InBody(?:\s*\d+)?$', re.I)
_COMPOSITION_COMPONENTS = (
    ('height', re.compile(r'身高')),
    ('weight', re.compile(r'^(?:人体成分|体成分)?体重(?:[（(].*)?$')),
    ('bmi', re.compile(r'体重指数|体质指数|(?<![A-Za-z])BMI(?![A-Za-z])', re.I)),
    ('fatRate', re.compile(r'体脂(?:肪)?(?:率|百分比)|^PBF$', re.I)),
    ('visceralFat', re.compile(r'内脏脂肪面积|^VFA$', re.I)),
    ('skeletalMuscle', re.compile(r'骨骼肌(?:量|质量)?|^SMM$', re.I)),
    ('waistHipRatio', re.compile(r'腰臀比|^WHR$', re.I)),
    ('basalMetabolism', re.compile(r'基础代谢(?:率)?|^BMR$', re.I)),
    ('fatMass', re.compile(r'体脂肪(?:重量)?$|脂肪量$')),
    ('extracellularRatio', re.compile(r'细胞外水分比率|ECW/TBW', re.I)),
)
_READER_NORMAL_ANCHORS = {
    'fastingInsulin': r'胰岛素|C[ -]?肽',
    'glycemia': r'糖化血红蛋白|葡萄糖|血糖',
    'cholesterol': r'低密度|高密度|载脂蛋白B|总胆固醇',
    'cardiac': r'钠尿肽|BNP|心电图',
    'arterial': r'四肢动脉|ABI|baPWV|PWV',
    'hepatobiliaryLab': r'^白蛋白|总胆红素|谷氨酰|碱性磷酸酶|淀粉酶|脂肪酶',
    'aminotransferases': r'丙氨酸|ALT',
    'renal': r'肌酐|尿素|肾小球|GFR|^尿酸(?:\b|$)',
    'urinalysis': r'^尿(?:液)?(?:蛋白|隐血|潜血|葡萄糖|白细胞[酯脂]酶|PH|pH)',
    'redCellIndices': r'平均红细胞体积|MCV',
    'iron': r'铁蛋白|转铁蛋白|血清铁|ferritin',
    'bloodCount': r'^(?:白细胞计数|红细胞计数|血红蛋白|血小板计数)$',
    'immuneInflammation': r'反应蛋白|免疫球蛋白|类风湿因子|抗链球菌溶血素',
    'thyroid': r'促甲状腺激素|游离.*甲状|TSH|FT[34]',
    'sexHormones': r'睾酮|雌二醇|泌乳素|促卵泡|促黄体|FSH|LH|PRL',
    'oralEye': r'视力',
    'helicobacter': r'呼气|幽门螺杆菌',
}
# Keep a source-verified reading even when its source prints no reference interval.
_READER_UNGRADED_ANCHORS = {
    'cardiac': re.compile(r'钠尿肽|BNP', re.I),
    'oralEye': re.compile(r'^(?:(?:裸眼|矫正)?(?:左眼|右眼)?视力|(?:左眼|右眼)(?:裸眼|矫正)?视力)'),
    'boneDensity': re.compile(r'(?:^| · )(?:T[- ]?Score|Z[- ]?Score|SOS)(?:$|\s)', re.I),
    'renal': re.compile(r'肾小球滤过率|\beGFR\b', re.I),
}
_URINE_COUNT_UNIT = re.compile(r'^(?:个|cells?|particles?)?\s*/\s*(?:μl|ul|lpf|hpf|lp|hp)$', re.I)
_FINDING_LIMITS = re.compile(r'(?:未见|未发现|未显示|未提示|无)(明显|明确|确切)')
_FINDING_EVIDENCE_LIMITS = re.compile(r'未(?:见|发现|显示|提示)[^，,。；;]{1,30}?(影像学?征象|影像学?表现|征象|迹象)')
_VALVE = r'(主动脉瓣|肺动脉瓣|二尖瓣|三尖瓣)'
_VALVE_REGURGITATION = re.compile(rf'{_VALVE}轻度[返反]流')
_VALVE_FLOW_SIGNAL = re.compile(rf'{_VALVE}(?:瓣口)?(?:探及|可见|见)少量(?:血液)?[返反]流信号')
_LUNG_REGION = r'((?:左肺|右肺)(?:上叶|中叶|下叶))'
_LUNG_CALCIFICATION = re.compile(rf'{_LUNG_REGION}(?:内)?(?:见)?(?:小|微小|点状)?钙化灶')
_LUNG_DENSE_NODULE = re.compile(rf'{_LUNG_REGION}(?:内)?(?:见)?结节(?:样|状)?致密影')
_EXAM_RESULT_HEADING = re.compile(
    r'(?:^|[。.;；\n])\s*(?:诊断提示|诊断意见|诊断结论|检查结论|超声提示|病理诊断|'
    r'检测结果和建议|检测结果|结论)\s*[:：]'
)
_BODY_SIZE = re.compile(
    r'身高|(?:^|成分|测量)体重(?:[（(]|$)|体重指数|体质指数|(?<![A-Za-z])BMI(?![A-Za-z])|腰围|臀围',
    re.I,
)
_BODY_CORE = re.compile(
    r'体脂(?:肪)?(?:率|百分比)|内脏脂肪面积|骨骼肌(?:量|质量)?|基础代谢|'
    r'体脂肪(?:重量)?$|脂肪量$|腰臀比|细胞外水分比率|\b(?:PBF|VFA|SMM|BMR|WHR)\b|ECW/TBW',
    re.I,
)


def _key(value: str | None) -> str:
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', value or '')).casefold()


def _has_value(value: str | int | float | bool | None) -> bool:
    return value is not None and (not isinstance(value, str) or value.strip() not in _UNAVAILABLE_MARKS)


def _assay_status(match: re.Match) -> str | None:
    result = match['polarity']
    symbol = match['symbol']
    if symbol is None or (result == '阴性') == (symbol in {'(-)', '(−)'}):
        return result
    return None


def _negative_urine_marker(item):
    # A dash is missing information except when a named urine strip assay
    # prints it as its result and uses a negative reference or status.
    return (
        _URINE_SCREENING_ASSAY.fullmatch(item.name)
        and unicodedata.normalize('NFKC', str(item.value)).strip() in {'-', '−'}
        and (
            str(item.reference_range or '').strip() in {'-', '−'}
            or _qualitative_polarity(item.reference_range) == 'negative'
            or _qualitative_polarity(item.source_status) == 'negative'
        )
    )


def _reference_direction(value, reference):
    """Read only a printed numeric interval; do not add an optimal target."""
    number = _NUMBER.fullmatch(unicodedata.normalize('NFKC', str(value)).strip())
    if not number or number[2]:
        return None
    numeric = r'([+-]?(?:\d+(?:\.\d*)?|\.\d+))'
    text = unicodedata.normalize('NFKC', str(reference or '')).strip()
    measurement = Decimal(number[1])
    if interval := re.fullmatch(rf'{numeric}\s*[-~～—–至]\s*{numeric}', text):
        low, high = Decimal(interval[1]), Decimal(interval[2])
        if low > high:
            return None
        if measurement < low:
            return '偏低'
        if measurement > high:
            return '偏高'
    elif bound := re.fullmatch(rf'(<=|>=|≤|≥|<|>)\s*{numeric}', text):
        limit = Decimal(bound[2])
        if bound[1] in {'<', '<=', '≤'} and (measurement > limit or measurement == limit and bound[1] == '<'):
            return '偏高'
        if bound[1] in {'>', '>=', '≥'} and (measurement < limit or measurement == limit and bound[1] == '>'):
            return '偏低'
    return None


def _source_status(item, exam_finding: bool):
    """Apply the original clinical range when the source did not print a status."""
    if _has_value(item.source_status):
        printed = unicodedata.normalize('NFKC', str(item.source_status)).strip()
        return _assay_status(match) if (match := _ASSAY_RESULT.fullmatch(printed)) else item.source_status
    value = unicodedata.normalize('NFKC', item.value).strip() if isinstance(item.value, str) else ''
    if re.search(r'甲状腺', item.name or ''):
        pattern = r'\b((?:C[-‐–]?\s*)?TI[-‐–]?\s*RADS)\s*([1-6](?:[ABC])?)(?![\dA-Za-z])'
        grades = dict.fromkeys(
            f'{prefix.upper()} {grade.upper()} 类' for prefix, grade in re.findall(pattern, value, re.I)
        )
        if len(grades) == 1:
            return next(iter(grades))
    if match := _ASSAY_RESULT.fullmatch(value):
        return _assay_status(match)
    if _negative_urine_marker(item) or _urine_microscopy_not_seen(item.name, item.value, item.unit):
        return '阴性'
    if re.fullmatch(
        r'正常(?:范围(?:内)?)?|未见(?:明显)?异常|未发现异常|无异常|异常|轻度异常|'
        r'(?:弱|强)?阳性(?:\s*\([^()]{1,12}\))?|阴性(?:\s*\([^()]{1,12}\))?|未检出|'
        r'偏高|偏低|升高|降低|normal|abnormal|positive|negative|neg',
        value,
        re.I,
    ):
        return item.value
    if conclusion := _EXAM_CONCLUSION.search(value):
        if normal := re.search(r'正常|未见(?:明显)?异常|阴性|未见肿瘤细胞', conclusion[1]):
            # Whole or organ-scoped negative conclusions may describe a normal
            # exam. Mixed conclusions keep the independently verified finding.
            if not exam_finding and (
                conclusion[1].strip() == normal[0]
                or re.fullmatch(r'[^，,；;]{0,20}未见(?:(?:明显)?异常|肿瘤细胞)', conclusion[1].strip())
            ):
                return normal[0]
    if exam_finding:
        return '有异常所见'
    if re.search(r'心电图', item.name) and re.search(r'正常范围心电图', value):
        return '已检项目未见异常'
    relation = _printed_reference_relation(
        _Observation(
            observation_id=item.observation_id or '',
            code=item.code,
            name=item.name,
            value=item.value,
            unit=printed_clinical_unit(item.value, item.unit),
            reference_range=item.reference_range,
            kind='clinical',
            source_type=item.source_type,
        ),
        allow_exact_match=True,
    )
    if relation == 'outside':
        return _reference_direction(item.value, item.reference_range) or '超出参考范围'
    return {'within': '参考范围内'}.get(relation)


def _arterial_qualifiers(name, value):
    """Promote explicitly stated ABI/PWV qualifiers from the same exam record."""
    if not isinstance(value, str) or not re.search(r'四肢动脉|踝肱|腕踝', name or ''):
        return None, None
    text = unicodedata.normalize('NFKC', value)
    segments = [part for part in re.split(r'[。;\n]+', text) if _ARTERIAL_METRIC.search(part)]
    statuses = []
    references = {}
    for segment in segments:
        keys = {key.upper() for key in _ARTERIAL_METRIC.findall(segment)}
        stated = list(_STATED_NORMAL_RANGE.finditer(segment))
        statuses.append(bool(stated) and not re.search(r'异常|偏高|偏低|超出|升高|降低', segment))
        if len(keys) == 1 and len(stated) == 1 and stated[0][1]:
            references.setdefault(next(iter(keys)), set()).add(stated[0][1])
    reference = ' / '.join(
        f'{metric} {next(iter(ranges))}' for metric, ranges in references.items() if len(ranges) == 1
    )
    status = '已检项目未见异常' if statuses and all(statuses) else None
    return reference or None, status


def _screening_conclusion(name, value):
    """Keep an instrument's actual risk assessment and its time qualifier together."""
    if not re.search(r'筛查|风险|eZscan', name, re.I) or not isinstance(value, str):
        return None
    pattern = r'(?:目前|当前|本次)[^，,；;。\n]{0,25}?风险(?:为|属|属于)?(?:较?[低高]|中等)'
    conclusions = list(dict.fromkeys(re.findall(pattern, value)))
    return '设备判断：' + conclusions[0] if len(conclusions) == 1 else None


def _row(item, finding=None, context=None, *, exam_finding=False):
    derived = is_derived_index(item.source_name)
    context = context or {}
    embedded_reference, embedded_status = _arterial_qualifiers(item.name, item.value)
    template_status = _source_status(item, exam_finding)
    if embedded_status and not _has_value(item.source_status):
        template_status = embedded_status
    return {
        'code': item.code or item.observation_id,
        'name': item.name,
        'value': item.value,
        'unit': item.unit,
        'referenceRange': item.reference_range if _has_value(item.reference_range) else embedded_reference,
        'status': item.source_status,
        'templateStatus': template_status,
        'observedAt': context.get('examDate') or item.observed_at,
        'reportDate': context.get('reportDate'),
        'specimen': context.get('specimen'),
        'clinicalDataType': context.get('clinicalDataType'),
        'method': context.get('method'),
        'sourceSection': context.get('sourceSection'),
        'sourcePage': context.get('sourcePage'),
        'sourceReportedAge': context.get('sourceReportedAge'),
        'interpretation': finding.explanation if finding else None,
        'sourceKind': 'derived' if derived else 'clinical',
        'sources': [reference.model_dump(mode='json', by_alias=True) for reference in item.sources],
        'evidenceRefs': list(finding.evidence_refs) if finding and finding.explanation else [],
    }


def _findings_index(data):
    """Use a name and exact record anchor, rather than copying a group's prose to its members."""
    index = defaultdict(list)
    for row in (*data.medical_assessment.findings, *data.medical_assessment.normal_findings):
        if row.explanation and row.value is not None:
            for source in row.sources:
                if source.source_code == 'A' and source.record_id:
                    index[(_key(row.name), source.record_id)].append(row)
    return index


def _interpretation(item, index):
    matches = {
        (row.explanation, tuple(row.evidence_refs)): row
        for source in item.sources
        if source.source_code == 'A' and source.record_id
        for row in index.get((_key(item.name), source.record_id), ())
        if str(row.value) == str(item.value) and (row.unit or '') == (item.unit or '')
    }
    return next(iter(matches.values())) if len(matches) == 1 else None


def _source_context(hospital):
    context = {}
    for row, source_id, key in hospital:
        extra = row.model_extra or {}
        section = next(
            (
                extra.get(key)
                for key in (
                    'sourceSection',
                    'source_section',
                    'checkItem',
                    'check_item',
                    'panelName',
                    'panel_name',
                )
                if isinstance(extra.get(key), str) and extra[key].strip()
            ),
            None,
        )
        page = extra.get('sourcePage', extra.get('source_page'))
        document = next(
            (
                extra.get(name)
                for name in (
                    'sourceSha256',
                    'source_sha256',
                    'sourceDocumentId',
                    'source_document_id',
                    'documentId',
                    'sourceFile',
                    'source_file',
                )
                if isinstance(extra.get(name), str) and extra[name].strip()
            ),
            None,
        )
        provenance = {
            'examDate': row.check_time if _time(row.check_time) is not None else None,
            'reportDate': row.report_time if _time(row.report_time) is not None else None,
            'specimen': key[3] or None,
            'clinicalDataType': row.data_type,
            'method': next(
                (
                    extra.get(name)
                    for name in ('method', 'testMethod', 'test_method')
                    if isinstance(extra.get(name), str) and extra[name].strip()
                ),
                None,
            ),
            'sourceSection': section,
            'sourcePage': page if type(page) is int else None,
            'sourceDocument': document.strip() if isinstance(document, str) else None,
        }
        for related in (source_id, *row._evaluation_duplicate_record_ids):
            context.setdefault(related, provenance)
    return context


def _exam_finding_sources(document):
    clinical = {fact.fact_id: fact.value for fact in document.facts if fact.kind == 'clinical'}
    titles = defaultdict(set)
    for problem in document.problems:
        if problem.certainty not in {'source_finding', 'confirmed', 'suspected'} or not problem.title:
            continue
        for source in problem.sources:
            if (
                source.fact_id in clinical
                and source.quote
                and source.quote in clinical[source.fact_id]
                and problem.title in source.quote
                and not _denied_title(problem.title, source.quote)
            ):
                titles[source.fact_id].add(problem.title)
    return titles


def _split_composite_exam(row, verified_titles):
    """Project explicitly headed source subrecords with the original fact links."""
    if (
        row['clinicalDataType'] != 'EXAM'
        or not _COMBINED_EXAM_NAME.fullmatch(row['name'])
        or not isinstance(row['value'], str)
    ):
        return []
    lines = row['value'].splitlines()
    headings = [
        (index, label)
        for index, line in enumerate(lines)
        if (label := line.strip().rstrip('：:')) and _EXAM_SUBHEADING.fullmatch(label)
    ]
    if len(headings) < 2 or lines[headings[0][0]].strip() == lines[headings[-1][0]].strip():
        return []
    entries = []
    for position, (start, label) in enumerate(headings):
        stop = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        parts = []
        for line in lines[start + 1 : stop]:
            if _EXAM_SIGNATURE.search(line.strip()):
                break
            if line.strip():
                parts.append(line)
        if not parts:
            return []
        if len(parts) == 2 and parts[0].strip() == parts[1].strip():
            parts.pop()  # The same check result and conclusion can be printed twice.
        source_text = '\n'.join(parts).strip()
        conclusion = parts[-1].strip().rstrip('。；')
        normal = re.fullmatch(r'(?:[^，,；;]{0,20}未见(?:明显)?异常|正常)', conclusion)
        verified = any(
            any(title in line and not _denied_title(title, line) for line in parts) for title in verified_titles
        )
        template_status = '有异常所见' if verified else conclusion if normal else None
        entries.append(
            {
                **row,
                'code': f'{row["code"]}:{start}',
                'name': label,
                'value': source_text,
                'unit': None,
                'referenceRange': None,
                'status': None,
                'templateStatus': template_status,
                'sourceSection': label,
                'interpretation': None,
                'evidenceRefs': [],
            }
        )
    return entries


def _literal_normal_exam(value):
    text = value.strip().rstrip('。；;')
    if re.search(r'大致|基本|总体', text):
        return False
    return bool(
        re.fullmatch(
            r'[^，,；;。]{0,35}(?:未见(?:明显)?异常|未见肿瘤细胞|正常(?:范围(?:内)?)?|骨量正常)',
            text,
        )
        or re.fullmatch(r'正常(?:范围)?心电图', text)
    )


def _atomic_exam_rows(row, verified_titles):
    """Use literal report conclusions and same-lesion measurements as display observations."""
    value = row['value']
    if (
        row.get('clinicalDataType') != 'EXAM'
        or not isinstance(value, str)
        or _SOURCE_CONTEXT_RECORD.search(row['name'])
        or _SOURCE_BACKGROUND_RECORD.search(row['name'])
    ):
        return [row]
    specialty_text = ' '.join(str(row.get(key) or '') for key in ('name', 'sourceSection', 'method'))
    if not _ACTUAL_SPECIALTY.search(specialty_text):
        return [row]
    headings = list(_EXAM_RESULT_HEADING.finditer(value))
    conclusions = (
        [part.strip().rstrip('.') for part in re.split(r'[。；;\n]+', value[headings[-1].end() :]) if part.strip()]
        if headings
        else []
    )
    # Verified source phrases are usable without an explicit result heading.
    conclusions.extend(
        sorted(
            title
            for title in verified_titles
            if title in value and not any(title in conclusion for conclusion in conclusions)
        )
    )
    if not conclusions and re.search(r'心电图', row['name']):
        conclusions = [
            part.strip()
            for part in re.split(r'[。；;\n]+', value)
            if re.fullmatch(r'(?:正常范围心电图|正常心电图)', part.strip())
        ]
    if not conclusions:
        return [row]
    atoms = []
    for position, conclusion in enumerate(dict.fromkeys(conclusions)):
        if not conclusion or re.match(r'^(?:建议|请|结合临床|定期|复查)', conclusion):
            continue
        exact_titles = [
            title for title in verified_titles if title in conclusion and not _denied_title(title, conclusion)
        ]
        name = exact_titles[0] if len(exact_titles) == 1 else conclusion
        excerpts = _source_lesion_measurement_quotes(value, name)
        if not excerpts:
            quote = _problem_source_quote(value, name)
            # A source conclusion is already a complete finding. Do not attach
            # an entire multi-organ paragraph merely because it shares a title.
            excerpts = [quote] if quote == conclusion else []
        parts = list(dict.fromkeys([conclusion, *excerpts]))
        normal = _literal_normal_exam(conclusion)
        atoms.append(
            {
                **row,
                'code': f'{row["code"]}:conclusion:{position}',
                'name': name,
                'value': '；'.join(parts),
                'unit': None,
                'referenceRange': None,
                'status': None,
                'templateStatus': '所见正常' if normal else '有异常所见' if exact_titles else None,
                'sourceSection': row['sourceSection'] or row['name'],
                'interpretation': None,
                'evidenceRefs': [],
                'sources': list(row['sources']),
            }
        )
    if not atoms:
        return [row]
    # Normal organ morphology is a useful comparison, separate from the
    # abnormal conclusion. Keep only an explicitly normal source sentence.
    if headings:
        for position, sentence in enumerate(re.split(r'[。\n]+', value[: headings[-1].start()])):
            sentence = sentence.strip()
            findings = re.findall(r'结节|囊(?:肿|性)|息肉|钙化|增强|减退|返流|反流|狭窄|扩张|增厚|增大', sentence)
            if not re.search(r'正常(?:范围)?|未见(?:明显)?异常', sentence) or any(
                not _denied_title(finding, sentence) for finding in findings
            ):
                continue
            if not re.match(r'^(?:双肾|左肾|右肾|肾脏|胰腺|脾|前列腺|各个房室|心内结构)', sentence):
                continue
            atoms.append(
                {
                    **row,
                    'code': f'{row["code"]}:normal:{position}',
                    'name': row['name'] + ' · ' + re.split(r'[，,]', sentence)[0],
                    'value': sentence,
                    'unit': None,
                    'referenceRange': None,
                    'status': None,
                    'templateStatus': '所见正常',
                    'sourceSection': row['sourceSection'] or row['name'],
                    'interpretation': None,
                    'evidenceRefs': [],
                    'sources': list(row['sources']),
                }
            )
    return atoms


def _clinical_rows(data, contexts, exam_findings, document, projection_index):
    """Consume only the same owned patient spans used by the writer and planner."""
    findings = _findings_index(data)
    advice_by_owner = defaultdict(list)
    for witness in original_medical_advice_witnesses(document, (fact.fact_id for fact in document.facts)):
        for identifier, _ in witness.fact_fingerprints:
            advice_by_owner[identifier].append(witness)
    original_items = defaultdict(list)
    for item in data.appendices.source_items:
        if item.kind != 'clinical' or (item.category or '').startswith('clinicalReportedBackground:'):
            continue
        for source in item.sources:
            if source.source_code == 'A' and source.record_id:
                original_items[source.record_id].append(item)
    for fact in document.facts:
        if fact.kind != 'clinical':
            continue
        projection = get_patient_projection(fact, projection_index=projection_index)
        if projection is None:
            continue
        owned_items = [
            item
            for item in original_items[fact.fact_id]
            if item.name == fact.name and item.value == fact.value and (item.unit or '') == (fact.unit or '')
        ]
        source_contexts = [
            contexts[source.record_id]
            for item in owned_items
            for source in item.sources
            if source.record_id in contexts
        ]
        context, context_variants = {}, {}
        for key in sorted({key for source in source_contexts for key in source}):
            values = []
            for source in source_contexts:
                value = source.get(key)
                if value is not None and not any(type(value) is type(prior) and value == prior for prior in values):
                    values.append(deepcopy(value))
            if len(values) == 1:
                context[key] = values[0]
            elif values:
                context_variants[key] = values
        source_context = projection.get('sourceContext') or {}
        context.update({key: value for key, value in source_context.items() if value is not None})
        section = context.get('sourceSection')
        context['sourceSection'] = section if _has_value(section) else fact.name
        context['clinicalDataType'] = source_context.get('dataType') or context.get('clinicalDataType')
        context['sourceDocument'] = (
            source_context.get('sourceSha256') or source_context.get('documentId') or context.get('sourceDocument')
        )
        contexts[fact.fact_id] = context
        for admitted in get_patient_rows(fact, projection_index=projection_index):
            if admitted.get('ownRecordId') != fact.fact_id:
                continue
            # A shared auxiliary source link never grants another record's
            # value, and an empty projection never falls back to raw prose.
            item = SimpleNamespace(
                name=admitted['name'],
                value=admitted['value'],
                unit=admitted.get('unit'),
                source_status=admitted.get('status'),
                reference_range=admitted.get('referenceRange'),
                observation_id=admitted['code'],
                code=admitted['code'],
                source_type=owned_items[0].source_type if owned_items else 'checkup',
            )
            if not (_has_value(item.value) or _negative_urine_marker(item)):
                continue
            verified = [
                title
                for title in exam_findings.get(fact.fact_id, ())
                if title in item.value and not _denied_title(title, item.value)
            ]
            is_finding = admitted.get('role') in {'exam_finding', 'physical_finding'}
            normal = is_finding and _literal_normal_exam(item.value)
            # A positive lesion in the signed conclusion owns its supporting
            # measurements even if a neighbouring organ contour is normal.
            conclusion = str(item.value).split('；', 1)[0].strip()
            positive_lesion = (
                is_finding and admitted.get('sectionRole') == 'source_conclusion'
                and conclusion == item.name and re.search(r'结节|息肉|囊肿|结石|钙化灶|增生|退变', conclusion)
                and not re.search(r'未见|未发现|排除|无|疑|可能|考虑', conclusion)
            )
            state = '所见正常' if normal else _source_status(item, bool(verified or positive_lesion))
            if not state and is_finding:
                state = _screening_conclusion(item.name, item.value)
                qualified = re.fullmatch(r'[^，,；;。]{0,25}(?:总体|大致|基本)正常', str(item.value))
                if not state and qualified:
                    state = '所见：' + item.value
            embedded_reference, embedded_status = _arterial_qualifiers(item.name, item.value)
            if embedded_status and not _has_value(item.source_status):
                state = embedded_status
            matching = [
                original for original in owned_items if original.name == item.name and original.value == item.value
            ]
            finding = _interpretation(matching[0], findings) if len(matching) == 1 else None
            sources = list(admitted['sources'])
            for original in owned_items:
                for source in original.sources:
                    rendered = source.model_dump(mode='json', by_alias=True)
                    if rendered not in sources:
                        sources.append(rendered)
            derivation = derived_index_context(fact)
            yield {
                'code': admitted['code'],
                'name': item.name,
                'value': item.value,
                'unit': item.unit,
                'referenceRange': item.reference_range if _has_value(item.reference_range) else embedded_reference,
                'status': item.source_status,
                'templateStatus': state,
                'observedAt': admitted.get('observedAt'),
                'reportDate': context.get('reportDate'),
                'specimen': context.get('specimen'),
                'clinicalDataType': context.get('clinicalDataType'),
                'method': context.get('method'),
                'sourceSection': context.get('sourceSection'),
                'sourcePage': context.get('sourcePage'),
                'sourceReportedAge': admitted.get('sourceReportedAge', context.get('sourceReportedAge')),
                'sourceKind': 'derived' if admitted.get('role') == 'derived_measurement' else 'clinical',
                'interpretation': finding.explanation if finding else None,
                'sources': sources,
                'evidenceRefs': list(finding.evidence_refs) if finding else [],
                '_ownRecordId': fact.fact_id,
                '_sourceContext': source_context,
                '_sourceContextVariants': context_variants,
                '_sourceImaging': fact.imaging,
                '_sourceRole': admitted.get('role'),
                '_sourceNature': admitted.get('sourceNature'),
                '_measuredClassification': (derivation or {}).get('valueKind') == 'measured_classification',
                '_calculation': (derivation or {}).get('formula'),
                '_verifiedFinding': bool(verified),
                '_decisionResult': admitted.get('sectionRole') == 'source_conclusion' or embedded_status is not None,
                '_sourceSectionRole': admitted.get('sectionRole'),
                '_sourceComponentName': next(
                    (
                        span.get('componentName')
                        for span in admitted.get('sourceSpans', ())
                        if span.get('componentName')
                    ),
                    None,
                ),
                '_sourceSpans': admitted.get('sourceSpans', []),
                '_followupAdvice': tuple(dict.fromkeys(
                    witness.text.strip() for witness in advice_by_owner.get(fact.fact_id, ())
                    if (item.name == fact.name or item.name in witness.text)
                    and re.search(r'就诊|复查|随诊|随访|进一步检查', witness.text)
                )),
                '_equivalentOwnRecordIds': [fact.fact_id],
            }


def _source_documents(row, contexts):
    if own := contexts.get(row.get('_ownRecordId'), {}).get('sourceDocument'):
        return {own}
    return {
        document
        for source in row['sources']
        if (document := contexts.get(source['recordId'], {}).get('sourceDocument'))
    }


def _source_method(row):
    if row.get('method'):
        return _key(row['method'])
    text = ' '.join(str(row.get(key) or '') for key in ('name', 'sourceSection'))
    return next((code for code, pattern in _METHOD_HINTS if pattern.search(text)), None)


def _same_source_exam(left, right, contexts, *, same_page=False):
    """Require source identity and compatible acquisition context before coalescing."""
    origin = _source_documents(left, contexts)
    if not origin or origin != _source_documents(right, contexts):
        return False
    ages = [
        row.get('sourceReportedAge')
        if row.get('sourceReportedAge') is not None
        else row.get('_sourceContext', {}).get('sourceReportedAge')
        for row in (left, right)
    ]
    if all(age is not None for age in ages) and ages[0] != ages[1]:
        return False
    subjects = [row.get('_sourceContext', {}).get('subject') or {} for row in (left, right)]
    if any(
        _has_value(value) and _has_value(subjects[1].get(key)) and value != subjects[1][key]
        for key, value in subjects[0].items()
    ):
        return False
    left_at, right_at = _time(left.get('observedAt')), _time(right.get('observedAt'))
    if left_at is None or right_at is None:
        if left.get('observedAt') != right.get('observedAt'):
            return False
        if left.get('sourcePage') is None or left.get('sourcePage') != right.get('sourcePage'):
            return False
    else:
        if left_at.date() != right_at.date():
            return False
        # A dated summary may refer to a timed original on that day; two
        # distinct recorded acquisition times remain independent readings.
        timed = [bool(re.search(r'[T ]\d{1,2}:\d{2}', str(row.get('observedAt')))) for row in (left, right)]
        if all(timed) and left_at != right_at:
            return False
    if (left.get('specimen') or '') != (right.get('specimen') or ''):
        return False
    left_method, right_method = _source_method(left), _source_method(right)
    if left_method and right_method and left_method != right_method:
        return False
    if same_page and (left.get('sourcePage') is None or left.get('sourcePage') != right.get('sourcePage')):
        return False
    return True


def _observation_value(value):
    text = unicodedata.normalize('NFKC', str(value)).strip()
    match = _NUMBER.fullmatch(text)
    if match and not match[2]:
        return 'number', Decimal(match[1])
    return 'text', re.sub(r'\s+', '', text).casefold()


def _merge_observation_sources(target, source, *, equivalent_reading=True):
    target['sources'].extend(item for item in source['sources'] if item not in target['sources'])
    target['_equivalentOwnRecordIds'] = list(
        dict.fromkeys(
            [
                *target.get('_equivalentOwnRecordIds', [target.get('_ownRecordId')]),
                *source.get('_equivalentOwnRecordIds', [source.get('_ownRecordId')]),
            ]
        )
    )
    target['_sourceSpans'] = list(target.get('_sourceSpans', ()))
    for span in source.get('_sourceSpans', ()):
        if span not in target['_sourceSpans']:
            target['_sourceSpans'].append(span)
    target['_verifiedFinding'] = target.get('_verifiedFinding', False) or source.get('_verifiedFinding', False)
    target['_decisionResult'] = target.get('_decisionResult', False) or source.get('_decisionResult', False)
    target['_sourceContext'] = deepcopy(target.get('_sourceContext', {}))
    for key, value in source.get('_sourceContext', {}).items():
        if target['_sourceContext'].get(key) is None and value is not None:
            target['_sourceContext'][key] = deepcopy(value)
    if re.search(r'[T ]\d{1,2}:\d{2}', str(source.get('observedAt'))) and not re.search(
        r'[T ]\d{1,2}:\d{2}', str(target.get('observedAt'))
    ):
        target['observedAt'] = source['observedAt']
    qualifiers = ('referenceRange', 'status', 'templateStatus', 'interpretation') if equivalent_reading else ()
    for key in (*qualifiers, 'sourceReportedAge'):
        if not _has_value(target.get(key)) and _has_value(source.get(key)):
            target[key] = source[key]
    if target.get('interpretation') == source.get('interpretation'):
        target['evidenceRefs'] = list(dict.fromkeys((*target['evidenceRefs'], *source['evidenceRefs'])))


def _compatible_source_qualifiers(left, right):
    return all(
        not (_has_value(left.get(key)) and _has_value(right.get(key)))
        or _observation_value(left[key]) == _observation_value(right[key])
        for key in ('referenceRange', 'status')
    )


def _literal_exam_projection(short, complete):
    """Recognize repeated source clauses without inventing their clinical equivalence."""
    target = str(_observation_value(complete)[1])
    clauses = [str(_observation_value(part.strip())[1]) for part in re.split(r'[。；;\n]+', str(short)) if part.strip()]
    return bool(clauses) and all(part in target for part in clauses)


def _exam_dimensions(row, *, component=False):
    if (
        not component
        and ':conclusion:' not in str(row.get('code'))
        and row.get('_sourceSectionRole') != 'source_conclusion'
        and not row.get('_verifiedFinding')
    ):
        return None
    text = unicodedata.normalize('NFKC', str(row['value']) + (' ' + str(row.get('unit') or '') if component else ''))
    dimensions = re.findall(
        r'((?:\d+(?:\.\d*)?|\.\d+)(?:\s*[×xX*]\s*'
        r'(?:\d+(?:\.\d*)?|\.\d+)){0,2})\s*(mm|cm|毫米|厘米)',
        text,
        re.I,
    )
    if not dimensions:
        return None
    return tuple(
        sorted(
            {
                (tuple(Decimal(part.strip()) for part in re.split(r'[×xX*]', value)), unit.casefold())
                for value, unit in dimensions
            }
        )
    )


def _deduplicate_clinical_rows(rows, contexts):
    """Project an observation once, keeping acquisition differences and conflicts."""
    result = []
    for original in rows:
        if _RECOMMENDATION_RECORD.search(original['name']):
            continue
        row = {**original, 'sources': list(original['sources']), 'evidenceRefs': list(original['evidenceRefs'])}
        equivalent = next(
            (
                other
                for other in result
                if _key(other['name']) == _key(row['name'])
                and (
                    _observation_value(other['value']) == _observation_value(row['value'])
                    or _exam_dimensions(row) is not None
                    and _exam_dimensions(row) == _exam_dimensions(other)
                )
                and _key(other.get('unit')) == _key(row.get('unit'))
                and _same_source_exam(
                    other,
                    row,
                    contexts,
                    same_page=row.get('clinicalDataType') != 'EXAM'
                    and not row.get('_sourceImaging')
                    and _exam_dimensions(row) is None,
                )
                and _compatible_source_qualifiers(other, row)
            ),
            None,
        )
        if equivalent is None:
            result.append(row)
        else:
            if _exam_dimensions(row) is not None and len(str(row['value'])) > len(str(equivalent['value'])):
                equivalent['value'] = row['value']
            _merge_observation_sources(equivalent, row)

    # An exam's short conclusion is already represented by the same text in
    # its full original. Different descriptions are retained independently.
    retained = []
    for row in result:
        if (
            row.get('clinicalDataType') != 'EXAM'
            or _SOURCE_BACKGROUND_RECORD.search(row['name'])
            or _SOURCE_CONTEXT_RECORD.search(row['name'])
        ):
            retained.append(row)
            continue
        value = _observation_value(row['value'])[1]
        name = _key(_EXAM_NAME_SUFFIX.sub('', row['name']))
        candidates = [
            other
            for other in result
            if other is not row
            and other.get('clinicalDataType') == 'EXAM'
            and not _SOURCE_BACKGROUND_RECORD.search(other['name'])
            and not _SOURCE_CONTEXT_RECORD.search(other['name'])
            and isinstance(value, str)
            and len(str(_observation_value(other['value'])[1])) > len(value) > 0
            and _literal_exam_projection(row['value'], other['value'])
            and _same_source_exam(row, other, contexts)
            and _compatible_source_qualifiers(row, other)
            and (name == _key(_EXAM_NAME_SUFFIX.sub('', other['name'])))
        ]
        if len(candidates) == 1:
            _merge_observation_sources(candidates[0], row)
        else:
            retained.append(row)
    return retained


def _urinary_name(name):
    return bool(urine_item_label(name) or re.match(r'^尿(?!素(?:氮|呼气|\b|[（(]|$)|酸(?:\b|[（(]|$))', name))


def _female(sex):
    return str(sex or '').strip().casefold() in {'女', '女性', 'f', 'female'}


def _tumor_marker(name, sex=None):
    """A tumor marker; HCG is a pregnancy/hormone test in women and a germ-cell marker otherwise."""
    return bool(_SCREENING_BASELINE.search(name or '') or _HCG.search(name or '') and not _female(sex))


def _group_code(name, definitions, *, specimen=None, section=None, sex=None):
    if specimen == '粪便':
        if definitions is _CLINICAL_GROUPS:
            return 'organMetabolism'
        if definitions is _DIGESTIVE_GROUPS:
            return 'lowerDigestive'
    if specimen == '尿' or (not specimen and _urinary_name(name)):
        if definitions is _CLINICAL_GROUPS:
            return 'organMetabolism'
        if definitions is _DIGESTIVE_GROUPS:
            return None
    if definitions is _CLINICAL_GROUPS:
        if _tumor_marker(name, sex):
            return None
        if _HCG.search(name or ''):
            return 'endocrineMusculoskeletal'
        if _DEVICE_STUDY.search(name or '') or _DEVICE_STUDY.search(section or ''):
            return 'specialty'
    if definitions is _BODY_GROUPS and section and definitions[1][2].search(section):
        if definitions[0][2].search(name) or definitions[1][2].search(name):
            return 'bodyComposition'
    for text in (name, section):
        if not text:
            continue
        for code, _, expression in definitions:
            if expression.search(text):
                return code
    return None


def clinical_group_for(name, *, specimen=None, section=None, sex=None):
    """The assessment system for a clinical measurement, independent of its status.

    Besides the four fixed systems, ``specialty`` marks a device study (3.5) and
    ``otherClinical`` any item without a fixed system; ``None`` is a tumor marker,
    which is shown as an abnormal specialty finding or a normal baseline.
    """
    if not name and not section:
        return None
    code = _group_code(name, _CLINICAL_GROUPS, specimen=specimen, section=section, sex=sex)
    if code is None and not _tumor_marker(name, sex):
        return 'otherClinical'
    return code


def _measurement_state(status):
    if status in {'参考范围内', '已检项目未见异常', '未见肿瘤细胞', '所见正常'}:
        return 'normal'
    if status in _BODY_FLAG_STATES or status in {'偏硬', '有异常所见'}:
        return 'abnormal'
    return _status_state(status)


def clinical_observation_assessment(row):
    """Expose printed reference and original finding authority independently."""
    assessment = patient_reference_context(row)
    value = row.get('value')
    own_record = row.get('_ownRecordId')
    finding_role = row.get('_sourceRole') in {'exam_finding', 'physical_finding'}
    owned_finding = finding_role and bool(own_record) and isinstance(value, str)
    witnesses = (
        [
            span['quote'].strip().rstrip('。；;，, \n')
            for span in row.get('_sourceSpans', ())
            if span.get('recordId') == own_record
            and span.get('role') == row.get('_sourceRole')
            and isinstance(span.get('quote'), str)
            and value.strip().rstrip('。；;，,') in span['quote']
        ]
        if owned_finding
        else []
    )
    # A source's admitted clause is authoritative literal evidence even
    # when its finding has no numeric range and no familiar "normal" word.
    # Its semantic direction remains for independent source review. A
    # normal child clause never changes its compound parent into normal.
    source_normal = bool(
        witnesses
        and row.get('_sourceNature') == 'normal_comparison'
        and any(text == value.strip().rstrip('。；;，,') for text in witnesses)
    )
    qualifiers = re.findall(r'大致|基本|总体', value) if source_normal else []
    if witnesses:
        qualifiers.extend(_FINDING_LIMITS.findall(value))
        qualifiers.extend(_FINDING_EVIDENCE_LIMITS.findall(value))
    qualified_normal = bool(qualifiers and source_normal)
    literal_normal = bool(
        not qualified_normal
        and witnesses
        and (source_normal or _literal_normal_exam(value) and any(_literal_normal_exam(text) for text in witnesses))
    )
    assessment['sourceFindingState'] = 'normal' if literal_normal else 'qualified_normal' if qualified_normal else None
    assessment['sourceFindingQuote'] = value if witnesses else None
    assessment['sourceFindingQualifier'] = '、'.join(dict.fromkeys(qualifiers)) if qualifiers else None
    if assessment['referenceAssessmentState'] in {'within', 'outside'}:
        authority = 'printed_reference'
    elif assessment['sourceStatusState'] is not None:
        authority = 'source_status'
    else:
        authority = 'literal_source_finding' if witnesses else None
    assessment['normalityAuthority'] = authority
    if assessment['assessmentState'] is None and not qualified_normal:
        assessment['assessmentState'] = _measurement_state(row.get('templateStatus'))
    return assessment


def _template_status(rows):
    labels = [row['templateStatus'] for row in rows]
    observed = list(dict.fromkeys(label for label in labels if _has_value(label)))
    if not observed:
        return None
    states = [_measurement_state(label) for label in labels]
    if 'abnormal' in states:
        abnormal = list(dict.fromkeys(label for label in labels if _measurement_state(label) == 'abnormal'))
        return '；'.join(abnormal)
    if None not in states and '阴性' in observed and set(observed) <= {'阴性', '参考范围内'}:
        return '阴性'
    if None not in states:
        return observed[0] if len(observed) == 1 else '已检项目未见异常'
    if all(_measurement_state(label) == 'normal' for label in observed):
        return None
    return '；'.join(observed)


def _target_comparisons(rows, context):
    """Applicable evidence-based targets for the panel rows; the clinical status is never relabelled."""
    if context is None:
        return []
    result = []
    for row in rows:
        if row.get('_sourceRole') not in {'derived_measurement', 'direct_measurement'}:
            continue
        verdict = judge(row, context)
        if verdict.target is None:
            continue
        target = verdict.target
        relation = {'up': '高于', 'down': '低于'}.get(target['direction'], '在')
        message = f'{target["label"]}{relation}目标区间' if target['direction'] else f'{target["label"]}在目标区间内'
        result.append({
            'recordIds': sorted(_owned_observation_ids(row)),
            'label': target['label'],
            'authority': 'target',
            'grade': {'up': '偏高', 'down': '偏低'}.get(target['direction'], '正常'),
            'reference': target['text'],
            'message': message,
            'referenceText': f'{target["label"]} {target["text"]}',
            'targetId': target['id'],
            'applicability': target['applicability'],
            'sources': target['sources'],
        })
    return result


def _reader_group_status(rows, targets=(), context=None):
    """Unified level wording (spec 1.4) with the named members behind each level."""
    short_names = {identifier: claim['label'] for claim in targets for identifier in claim['recordIds']}
    by_level = defaultdict(list)
    followups = []
    for row in rows:
        level = row.get('level')
        if level is None and context is not None:
            level = judge(row, context).level
        if level is None:
            continue
        name = short_names.get(row.get('_ownRecordId')) or row['name'].rsplit(' · ', 1)[-1]
        by_level[level].append(name)
        if level == 'focus':
            followups.extend(row.get('_followupAdvice', ()))
    labels = []
    for level in ('focus', 'borderline', 'normal'):
        names = list(dict.fromkeys(by_level.get(level, ())))
        if not names:
            continue
        if len(rows) == 1:
            labels.append(LEVEL_LABELS[level])
        elif len(names) > 3 and len('、'.join(names)) > 16:
            labels.append(f'{LEVEL_LABELS[level]}：{len(names)}项')
        else:
            labels.append(f'{LEVEL_LABELS[level]}：' + '、'.join(names))
    if any(re.search(r'查因|查明(?:病因|原因)|进一步(?:检查原因|明确病因)', text)
           or re.search(r'(?:^|[。；;])(?:请|建议)?(?:立即|尽快|急诊)', text) for text in followups):
        labels.insert(0, '需查因或及时就医')
    elif followups:
        labels.insert(0, '按医嘱复查')
    if not labels:
        labels.append('未给出参考区间')
    labels.extend(dict.fromkeys(claim['message'] for claim in targets if claim['grade'] != '正常'))
    return '；'.join(labels)


def _clinical_panel(row, panels):
    if (row['specimen'] == '尿' or (not row['specimen'] and _urinary_name(row['name']))) and any(
        code == 'urinalysis' for code, _, _ in panels
    ):
        return 'urinalysis'
    if row['specimen'] == '粪便' and any(code == 'stool' for code, _, _ in panels):
        return 'stool'
    for text in (row['name'], row['sourceSection']):
        if text:
            for code, _, pattern in panels:
                if re.search(pattern, text, re.I):
                    return code
    return 'related'


def _template_rows(code, rows):
    panels = (*_CLINICAL_PANELS[code], ('related', '其他相关检查', ''))
    buckets = defaultdict(list)
    for row in rows:
        buckets[_clinical_panel(row, panels[:-1])].append(row)
    result = []
    for key, name, _ in panels:
        if readings := buckets.get(key):
            names = {row['name'] for row in readings}
            result.append(
                {
                    'code': key,
                    'name': readings[0]['name'] if len(names) == 1 else name,
                    'rows': readings,
                    'status': _template_status(readings),
                }
            )
    return result


def _groups(rows, definitions, *, sex=None):
    clinical = definitions is _CLINICAL_GROUPS
    order = [*(code for code, _, _ in definitions), *(('otherClinical',) if clinical else ())]
    names = _GROUP_NAMES if clinical else {code: name for code, name, _ in definitions}
    buckets = {code: [] for code in order}
    for row in rows:
        match = _group_code(
            row['name'], definitions, specimen=row['specimen'], section=row['sourceSection'],
            **({'sex': sex} if clinical else {}),
        )
        if clinical and match is None and not _tumor_marker(row['name'], sex):
            # Every examination item keeps a home: no fixed system means "其他检查" (end of 3.5).
            match = 'otherClinical'
        if match in buckets:
            buckets[match].append(row)
    groups = []
    for code in order:
        name = names[code]
        if readings := buckets[code]:
            group = {'code': code, 'name': name, 'rows': readings, 'summary': None}
            if definitions is _CLINICAL_GROUPS:
                group['templateRows'] = _template_rows(code, readings)
            elif definitions is _BODY_GROUPS:
                body_flags = list(
                    dict.fromkeys(
                        row['templateStatus'] for row in readings if row['templateStatus'] in _BODY_FLAG_STATES
                    )
                )
                group['status'] = '、'.join(body_flags) if body_flags else _template_status(readings)
                recorded_only = all(row['sourceKind'] == 'recordedHistory' for row in readings)
                if recorded_only:
                    group['status'] = '送检登记'
                if code == 'weightWaist':
                    has_girth = any(
                        re.search(r'腰围|臀围|腰臀比|腰高比|\bWHR\b', row['name'], re.I) for row in readings
                    )
                    has_body_size = any(re.search(r'身高|体重|体质指数|^BMI', row['name'], re.I) for row in readings)
                    group['name'] = (
                        '送检登记 BMI'
                        if recorded_only
                        else '身高、体重与围度'
                        if has_girth and has_body_size
                        else '围度测量'
                        if has_girth
                        else '身高、体重与体重指数'
                    )
            groups.append(group)
    return groups


def _symptom_family(name, question=''):
    for key, label, pattern in _SYMPTOM_FAMILIES:
        if pattern.search(name):
            return key, label
    if (re.search(r'排便频率|排便次数', question) or '排便' in name) and _BOWEL_FREQUENCY.search(name):
        return 'bowel', '排便频率'
    return None, name


def _symptom_phrase(response, name, family, question):
    affirmed = [part.strip() for part in _SYMPTOM_CLAUSE.split(name) if part.strip()]
    parts = (part.strip() for part in _SYMPTOM_CLAUSE.split(response))
    # Retain the original measurement/time wording for the same episode.
    # Splitting at clause boundaries keeps a different concurrent complaint
    # and a later management goal from becoming part of this symptom.
    return '；'.join(
        part
        for part in parts
        if part
        and not _NON_ASSERTION.search(part)
        and any(witness in part for witness in affirmed)
        and _symptom_family(part, question)[0] == family
    )


def _symptom_value_key(value):
    value = _key(value)
    # A direct frequency answer and the same explicitly named frequency are
    # equivalent for display. Do not coerce different counts or time windows.
    return _BOWEL_FREQUENCY.sub(lambda match: match[0].replace('排便', ''), value)


def _shoulder_neck_signal(row):
    """Keep direct neck, spinal and neurological findings from a mixed examination."""
    name = row['name']
    value = str(row.get('value') or '')
    if _SHOULDER_EXAM.search(name):
        return row if value else None
    if not _COMPOSITE_PHYSICAL_EXAM.search(name):
        return None
    parts = [part.strip() for part in re.split(r'[。；;，,\n]+', value) if part.strip()]
    excerpts = []
    for index, part in enumerate(parts):
        heading = _SCOPED_PHYSICAL_HEADING.match(part)
        if not heading:
            continue
        if heading.end() < len(part):
            excerpts.append(part)
        elif index + 1 < len(parts) and _SCOPED_PHYSICAL_RESULT.search(parts[index + 1]):
            excerpts.append(f'{part} {parts[index + 1]}')
    if not excerpts:
        return None
    return {
        **row,
        'value': '；'.join(dict.fromkeys(excerpts)),
        'status': '相关查体',
        'templateStatus': None,
        'interpretation': None,
        'evidenceRefs': [],
    }


def _stool_occult(row):
    return row.get('specimen') != '尿' and (
        _STOOL_OCCULT.fullmatch(row['name'])
        or row.get('specimen') == '粪便'
        and re.fullmatch(r'(?:隐血|潜血)(?:试验|检测)?', row['name'])
    )


def project_symptom_clinical_evidence(symptom, row):
    """Scope supported observations and label useful negative findings as comparisons."""
    if not isinstance(row.get('name'), str):
        return None
    name = row['name']
    value = str(row.get('value') or '')
    status = row.get('templateStatus') or row.get('status')
    family, _ = _symptom_family(symptom)
    if family == 'shoulderNeck':
        return _shoulder_neck_signal(row)
    if family == 'bowel':
        stool_form = row.get('specimen') != '尿' and _STOOL_FORM.fullmatch(name)
        if (
            stool_form
            and re.fullmatch(r'(?:黄色)?(?:成形)?软便', value.strip())
            or _stool_occult(row)
            and _qualitative_polarity(value) == 'negative'
        ):
            contrast = '鉴别对照：便质软' if stool_form else '鉴别对照：隐血阴性'
            return {**row, 'status': contrast, 'templateStatus': contrast}
        if re.fullmatch(r'(?:本次|实测|测量)?体重(?:[（(](?:kg|公斤|千克|实测)[）)])?', name, re.I) and (
            _NUMBER.fullmatch(value.strip())
        ):
            return None
        if re.search(r'隐血|潜血|白细胞|红细胞', name):
            return None
        if stool_form:
            is_hard_stool = re.search(r'偏硬|干硬|硬块|干结', value)
            return row if _measurement_state(status) == 'abnormal' and is_hard_stool else None
    if _measurement_state(status) == 'normal':
        return None
    if family == 'reflux' and re.search(r'幽门螺杆菌|尿素呼气|碳\s*(?:13|14).*呼气', name):
        return None
    if re.search(r'疲劳|乏力|困倦', symptom) and re.search(
        r'甲状腺.*(?:超声|彩超|影像|B超)|转氨酶|(?:\bALT\b|\bAST\b)',
        name,
        re.I,
    ):
        # A separate hepatic lab or thyroid imaging finding does not prove a
        # symptom link. It remains visible in its own system.
        return None
    return (
        None
        if re.fullmatch(
            r'未见(?:明显)?异常|阴性(?:\s*\([^)]*\))?|未检出|未查见|正常|negative',
            value,
            re.I,
        )
        else row
    )


def symptom_clinical_evidence(symptom, row):
    """Check whether the report may use a signal for this complaint."""
    return project_symptom_clinical_evidence(symptom, row) is not None


def _same_phenotype_measurement(family, row):
    if family == 'bowel':
        return bool(row['specimen'] != '尿' and _STOOL_FORM.fullmatch(row['name']) or _stool_occult(row))
    if family == 'nasal':
        return bool(
            re.search(r'^鼻部检查|鼻内镜|鼻腔检查|鼻黏膜', row['name'])
            and re.search(r'水肿|充血|苍白|肿胀', str(row['value']))
        )
    if family == 'shoulderNeck':
        return bool(_SHOULDER_EXAM.search(row['name']) or _COMPOSITE_PHYSICAL_EXAM.search(row['name']))
    return False


def _symptoms(document, clinical_rows):
    rows_by_source = defaultdict(list)
    for row in clinical_rows:
        if row['sourceKind'] == 'clinical':
            for source in row['sources']:
                if source['recordId']:
                    rows_by_source[source['recordId']].append(row)
    facts = {fact.fact_id: fact for fact in document.facts}
    related = defaultdict(dict)
    for problem in document.problems:
        anchors = [
            link.fact_id
            for link in problem.sources
            if link.fact_id in facts and link.quote and link.quote in facts[link.fact_id].value
        ]
        clinical = {
            (row['name'], tuple(ref['recordId'] for ref in row['sources'])): row
            for anchor in anchors
            for row in rows_by_source[anchor]
        }
        for anchor in anchors:
            if facts[anchor].kind == 'self_reported':
                related[anchor].update(clinical)

    episodes = []
    for fact in facts.values():
        if reported_fact_kind(fact) != 'symptom' or not _has_value(fact.value):
            continue
        question, separator, answer = fact.value.partition('；回答：')
        options = explicit_reported_options(fact.value)
        collection_time = _SYMPTOM_CONTEXT_TIME.search(question) if options else None
        for name, response in reported_symptom_fragments(fact.value, fact.name):
            family, label = _symptom_family(name, question if separator else '')
            if family:
                scoped = _symptom_phrase(answer if separator else response, name, family, question)
                response = scoped or name
            response = response.strip().rstrip('。；;')
            scoped_time = _SYMPTOM_TIMING.search(response) if family else None
            history = collection_time[0] if collection_time and name in options else None
            if fact.date and (not family or fact.date in response or _time(fact.date)):
                history = fact.date
            episodes.append((family, label, response, history or (scoped_time[0] if scoped_time else None), fact))

    known_times = defaultdict(set)
    for family, _, _, episode, _ in episodes:
        if family and episode:
            known_times[family].add(episode)
    symptoms = {}
    for family, name, response, episode, fact in episodes:
        if family and not episode and len(known_times[family]) == 1:
            episode = next(iter(known_times[family]))
        key = (family, episode) if family else (_key(name), response, episode)
        symptom = symptoms.setdefault(
            key,
            {
                'code': f'S{len(symptoms) + 1}',
                'name': name,
                'value': '',
                'history': episode,
                'unit': None,
                'referenceRange': None,
                'status': '本人自述',
                'observedAt': None,
                'interpretation': None,
                'sourceKind': 'selfReported',
                'sources': [],
                'evidenceRefs': [],
                'clinicalSignals': [],
                'microbiomeSignals': [],
            },
        )
        if family == 'bowel' and name != '排便频率':
            symptom['name'] = name
        phrases = list(filter(None, symptom['value'].split('；')))
        for phrase in response.split('；'):
            if phrase and not any(_symptom_value_key(phrase) in _symptom_value_key(part) for part in phrases):
                phrases.append(phrase)
        symptom['value'] = '；'.join(phrases)
        source = {'sourceCode': 'B', 'recordId': fact.fact_id, 'observedAt': None}
        if source not in symptom['sources']:
            symptom['sources'].append(source)
        for row in related[fact.fact_id].values():
            signal = project_symptom_clinical_evidence(name, row)
            if signal is not None and signal not in symptom['clinicalSignals']:
                symptom['clinicalSignals'].append(signal)
    for key, symptom in symptoms.items():
        if symptom['history'] and symptom['history'] in symptom['value']:
            symptom['history'] = None
        family = key[0]
        if family not in {'bowel', 'nasal', 'shoulderNeck'} or symptom['name'] == '排便频率':
            continue
        episode = key[1]
        for row in clinical_rows:
            if not _same_phenotype_measurement(family, row):
                continue
            checked = _time(row['observedAt'])
            reported_at = _time(episode)
            if reported_at is not None and (checked is None or checked.date() != reported_at.date()):
                continue
            signal = project_symptom_clinical_evidence(symptom['name'], row)
            if signal is not None and signal not in symptom['clinicalSignals']:
                symptom['clinicalSignals'].append(signal)

    for row in clinical_rows:
        name = row['name'].strip()
        value = str(row['value']).strip().rstrip('。')
        if (
            row['sourceKind'] != 'clinical'
            or not row['observedAt']
            or not (
                _SYMPTOM_RECORD.fullmatch(name) or name in {'临床诊断', '就诊诊断'} and _SYMPTOM_LABEL.fullmatch(value)
            )
        ):
            continue
        if not _has_value(value) or re.match(r'^(?:无(?!力|法)|否认|未见|未诉|不详|未提供|暂无)', value):
            continue
        title = value if _SYMPTOM_LABEL.fullmatch(value) else name
        date = row['observedAt']
        key = (_key(title), value, date)
        if key in symptoms:
            continue
        symptoms[key] = {
            'code': f'S{len(symptoms) + 1}',
            'name': title,
            'value': value,
            'history': f'{date} 临床记录',
            'unit': None,
            'referenceRange': None,
            'status': '既往记录',
            'observedAt': date,
            'interpretation': None,
            'sourceKind': 'clinicalRecord',
            'sources': row['sources'],
            'evidenceRefs': [],
            'clinicalSignals': [row],
            'microbiomeSignals': [],
        }
    return list(symptoms.values())


def _digestive_context(rows, symptoms, *, contexts=None, source_rows=()):
    if contexts is None:
        groups = _groups(rows, _DIGESTIVE_GROUPS)
    else:
        # Reader context reuses admitted display atoms, not every record in
        # an abdominal study. The actual clause must concern a GI object;
        # a renal/ocular clause cannot acquire that role from its parent label.
        # A source inventory and the clinical main table have different
        # purposes. A directly measured digestive function can be a useful
        # normal counterexample here even when it needed no clinical panel.
        # Do not expand all normal anatomy from its parent examination.
        additional = [
            row
            for row in source_rows
            if row.get('_sourceRole') in {'direct_measurement', 'derived_measurement'}
            and _DIGESTIVE_FUNCTION_MEASUREMENT.search(row['name'])
            and not _SOURCE_CONTEXT_RECORD.search(row['name'])
            and not _SOURCE_BACKGROUND_RECORD.search(row['name'])
        ]
        buckets = defaultdict(list)
        for row in _reader_atoms([*rows, *additional], contexts):
            label = row['name'].rsplit(' · ', 1)[-1]
            text = label + ' ' + str(row['value'])
            code = next((code for code, _, pattern in _DIGESTIVE_GROUPS if pattern.search(text)), None)
            if code is not None:
                buckets[code].append(row)
        groups = [
            {'code': code, 'name': name, 'rows': buckets[code], 'summary': None}
            for code, name, _ in _DIGESTIVE_GROUPS
            if buckets[code]
        ]
    reported = [
        {
            **{key: symptom[key] for key in ('name', 'observedAt', 'status', 'sourceKind', 'sources')},
            'value': symptom.get('history') or symptom['value'],
        }
        for symptom in symptoms
        if symptom['sourceKind'] == 'selfReported' and _BOWEL_SYMPTOM.search(symptom['name'])
    ]
    if reported:
        lower = next((group for group in groups if group['code'] == 'lowerDigestive'), None)
        if lower is None:
            lower_name = next(name for code, name, _ in _DIGESTIVE_GROUPS if code == 'lowerDigestive')
            lower = {'code': 'lowerDigestive', 'name': lower_name, 'rows': []}
            groups.append(lower)
        lower['symptomContext'] = reported
    return groups


def _medication_records(data, document):
    """Retain recorded therapy history separately from measured observations."""
    rows = [
        _row(item)
        for item in data.appendices.source_items
        if item.category == 'clinicalReportedBackground:medications' and _has_value(item.value)
    ]
    for record in document.background.get('sourceReported', ()):
        if not _has_value(record.get('value')) or not (
            record.get('kind') in {'medication', 'medications', 'treatment'}
            or re.search(r'用药|治疗|药物', str(record.get('name') or ''))
        ):
            continue
        rows.append(
            {
                'name': record.get('name'),
                'value': record.get('value'),
                'observedAt': record.get('date'),
                'sourceKind': 'recordedHistory',
                'sourcePage': record.get('sourcePage'),
                'sourceSection': record.get('sourceSection'),
            }
        )
    return rows


def _eligible_measurement(row, comparable_units):
    if row.data_type != 'INSPECTION' or len(comparable_units) != 1:
        return None
    match = _NUMBER.fullmatch(str(row.test_result_value).strip())
    if not match or (
        match[2] and re.sub(r'\s+', '', unicodedata.normalize('NFKC', match[2])).casefold() != comparable_units[0]
    ):
        return None
    try:
        return Decimal(match[1])
    except InvalidOperation:
        return None


def _hospital_rows(request, parsed):
    classify = _hospital_row_classifier(parsed)
    subject = SubjectInfo(name=request.name or parsed.subject.name, sex=request.sex or parsed.subject.sex)
    for origin, rows in (
        ('request.hospitalData', request.hospital_data),
        ('parsed.supplementaryHospitalData', parsed.supplementary_hospital_data),
        ('parsed.clinicalHistory', parsed._evaluation_clinical_history),
    ):
        for index, row in enumerate(rows):
            if classify(row) != 'clinical' or subject_mismatch(subject, _row_subject(row)):
                continue
            yield row, row._evaluation_record_id or f'{origin}[{index}]', _comparison_key(row)


def _trends(hospital_rows):
    groups = defaultdict(dict)
    for row, source_id, key in hospital_rows:
        # Clinical trends use dated exams, rather than publication or archive dates.
        at = _time(row.check_time)
        if at is None:
            continue
        value = _eligible_measurement(row, key[2])
        if value is None:
            continue
        observation_key = (*key[:-1], at.tzinfo is not None)
        groups[observation_key].setdefault(at.date(), {}).setdefault(value, (row, source_id))

    trends = []
    for measurements in groups.values():
        # Competing values for a single date are retained in source detail and
        # cannot provide a stable baseline for a directional interpretation.
        if len(measurements) < 2 or any(len(values) != 1 for values in measurements.values()):
            continue
        points, numbers = [], []
        for date, values in sorted(measurements.items()):
            number, (row, source_id) = next(iter(values.items()))
            extra = row.model_extra or {}
            observed_at = row.check_time or date.isoformat()
            numbers.append(number)
            points.append(
                {
                    'name': row.item_name,
                    'value': row.test_result_value,
                    'unit': extra.get('unit') or extra.get('testResultUnit') or extra.get('resultUnit'),
                    'referenceRange': row.reference_range,
                    'status': row.result_status_name,
                    'observedAt': observed_at,
                    'sourceKind': 'clinical',
                    'sources': [{'sourceCode': 'A', 'recordId': source_id, 'observedAt': observed_at}],
                    'evidenceRefs': [],
                    'interpretation': None,
                }
            )
        comparisons = {(right > left) - (right < left) for left, right in zip(numbers, numbers[1:])} - {0}
        direction = '波动' if len(comparisons) > 1 else {1: '升高', -1: '下降', 0: '稳定'}[next(iter(comparisons), 0)]
        trends.append({'name': points[-1]['name'], 'points': points, 'direction': direction})
    return trends


def build_clinical_trends(request, parsed):
    return _trends(_hospital_rows(request, parsed))


def _body_measurement_identity(row):
    name = row['name']
    if re.search(r'体重指数|体质指数|(?<![A-Za-z])BMI(?![A-Za-z])', name, re.I):
        kind = 'bmi'
    elif name.startswith('腰围'):
        kind = 'waist'
    else:
        return None
    at = _time(row['observedAt'])
    match = _NUMBER.fullmatch(str(row['value']).strip())
    explicit_unit = str(row['unit'] or '').strip()
    if not at or not match or (match[2] and explicit_unit and _key(match[2]) != _key(explicit_unit)):
        return None
    try:
        return kind, at.date(), _key(explicit_unit or match[2]), Decimal(match[1])
    except InvalidOperation:
        return None


def _body_metrics(rows, contexts):
    selected = [row for row in rows if _group_code(row['name'], _BODY_GROUPS, section=row['sourceSection']) is not None]
    measured = defaultdict(list)
    for row in selected:
        if row['sourceKind'] == 'clinical' and (identity := _body_measurement_identity(row)):
            measured[identity].append(row)
    result = []
    for row in selected:
        candidates = measured.get(_body_measurement_identity(row), ())
        if row['sourceKind'] != 'derived' or len(candidates) != 1:
            if row.get('_measuredClassification'):
                row['sourceKind'] = 'clinical'
            result.append(row)
            continue
        # Keep the actual measurement and its source. A classification from
        # that exact value and date can be shown alongside it once.
        matched = candidates[0]
        for key in ('referenceRange', 'status', 'templateStatus'):
            if not _has_value(matched[key]):
                matched[key] = row[key]
        _merge_observation_sources(matched, row)
    return _deduplicate_body_composition(result, contexts)


def _registered_bmi(data, body_metrics):
    """Show a single conflict-free report registration when clinical BMI is absent."""
    if any(re.search(r'体重指数|体质指数|^BMI(?![A-Za-z])', row['name'], re.I) for row in body_metrics):
        return None
    entries = [
        item
        for item in data.appendices.source_items
        if item.kind == 'context'
        and item.category == 'structuralAssessment'
        and item.name == '原报告BMI'
        and re.fullmatch(r'\d+(?:\.\d+)?', str(item.value).strip())
        and any(ref.source_code == 'C' and ref.record_id for ref in item.sources)
    ]
    if len({(item.value, item.unit) for item in entries}) != 1:
        return None
    return {
        **_row(entries[0]),
        'name': '菌群送检登记 BMI',
        'sourceKind': 'recordedHistory',
        'referenceRange': None,
        'status': '送检登记',
        'templateStatus': '送检登记',
    }


def _composition_reading(name, value, unit=None):
    metric = next((key for key, pattern in _COMPOSITION_COMPONENTS if pattern.search(name)), None)
    if metric is None or not (match := _NUMBER.fullmatch(unicodedata.normalize('NFKC', str(value)).strip())):
        return None
    return metric, Decimal(match[1]), unicodedata.normalize('NFKC', unit or match[2]).strip().casefold()


def _same_body_exam(left, right, contexts):
    return _same_source_exam(left, right, contexts, same_page=True)


def _deduplicate_body_composition(rows, contexts):
    """Keep distinct same-device observations once, preserving all source links."""
    results = []
    for row in rows:
        if not isinstance(row['value'], str) or not _COMPOSITION_COMPOSITE.fullmatch(row['name']):
            results.append(row)
            continue
        parts = re.split(r'[；;\n]+', row['value'])
        if len(parts) == 1:
            results.append(row)
            continue
        remaining = []
        for part in parts:
            split = re.fullmatch(r'\s*([^\d]+?)\s*[:：]?\s*([+-]?\d.*)', part)
            reading = _composition_reading(split[1], split[2].rstrip('。')) if split else None
            matches = [
                other
                for other in rows
                if other is not row
                and reading is not None
                and _same_body_exam(row, other, contexts)
                and reading == _composition_reading(other['name'], other['value'], other['unit'])
            ]
            if len(matches) != 1:
                remaining.append(part)
                continue
            matched = matches[0]
            matched['sources'].extend(source for source in row['sources'] if source not in matched['sources'])
        if remaining:
            results.append({**row, 'value': '；'.join(remaining)})
    selected = []
    for row in sorted(results, key=lambda item: item.get('_sourceSectionRole') == 'source_component'):
        reading = _composition_reading(row.get('_sourceComponentName') or row['name'], row['value'], row['unit'])
        duplicate = next(
            (
                other
                for other in selected
                if reading is not None
                and _same_body_exam(row, other, contexts)
                and reading
                == _composition_reading(
                    other.get('_sourceComponentName') or other['name'], other['value'], other['unit']
                )
                and _compatible_source_qualifiers(row, other)
            ),
            None,
        )
        if duplicate is not None:
            _merge_observation_sources(duplicate, row)
        else:
            selected.append(row)
    return selected


def _attach_context_witnesses(rows, contexts):
    """Keep an audit paragraph's original id on the measurements it literally repeats."""
    for context in rows:
        if not _SOURCE_CONTEXT_RECORD.search(context['name']):
            continue
        text = unicodedata.normalize('NFKC', str(context['value']))
        for row in rows:
            if row is context or not _same_source_exam(context, row, contexts, same_page=True):
                continue
            numeric = _NUMBER.fullmatch(unicodedata.normalize('NFKC', str(row['value'])).strip())
            if not numeric or numeric[2]:
                continue
            label = re.split(r'\s+', row['name'].strip())[-1]
            match = re.search(re.escape(label) + r'\s*[:：]?\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))', text)
            if match and Decimal(match[1]) == Decimal(numeric[1]):
                row['sources'].extend(source for source in context['sources'] if source not in row['sources'])


def _reader_body_groups(rows, contexts):
    """Keep core body size, composition and resting readings in the overview."""
    core = []
    for row in rows:
        group = _group_code(row['name'], _BODY_GROUPS, section=row['sourceSection'])
        if (
            group == 'bodyComposition' and row.get('_sourceRole') in {'exam_finding', 'physical_finding'}
            and isinstance(row['value'], str) and '勾选' in row['value']
            and re.search(r'评价|均衡', row['value'])
        ):
            # The checked interpretation owns its whole clause (e.g. limbs
            # balanced vs total fat over range), not any neighbouring number.
            core.append({**row, 'name': '人体成分设备评价', 'sources': list(row['sources'])})
            continue
        if group == 'bodyComposition':
            primary_name = re.sub(r'[（(][^）)]*[）)]', '', row['name'])
            if not (_BODY_SIZE.search(primary_name) or _BODY_CORE.search(primary_name)):
                continue
        if row.get('clinicalDataType') == 'EXAM' and not _NUMBER.fullmatch(str(row['value']).strip()):
            continue
        if group == 'restingVitals' and re.search(r'踝|下肢|脚', row['name']):
            continue
        core.append({**row, 'sources': list(row['sources'])})
    # Pair an explicitly named systolic/diastolic reading from one acquisition.
    # All original numbers remain independently indexed in bodyMetrics.
    paired, consumed = [], set()
    for index, row in enumerate(core):
        if index in consumed:
            continue
        label = re.sub(r'收缩压|舒张压', '', row['name'])
        partner = [
            (other_index, other)
            for other_index, other in enumerate(core)
            if other_index != index
            and other_index not in consumed
            and (
                ('收缩压' in row['name'] and '舒张压' in other['name'])
                or ('舒张压' in row['name'] and '收缩压' in other['name'])
            )
            and re.sub(r'收缩压|舒张压', '', other['name']) == label
            and _key(row['unit']) == _key(other['unit'])
            and _same_source_exam(row, other, contexts, same_page=True)
            and _NUMBER.fullmatch(str(row['value']).strip())
            and _NUMBER.fullmatch(str(other['value']).strip())
        ]
        if len(partner) == 1:
            other_index, other = partner[0]
            consumed.add(other_index)
            systolic, diastolic = (row, other) if '收缩压' in row['name'] else (other, row)
            row = {
                **row,
                'name': label + '血压',
                'value': f'{systolic["value"]}/{diastolic["value"]}',
                'referenceRange': None,
                'templateStatus': _template_status([row, other]),
                'sources': list(row['sources']),
            }
            row['sources'].extend(source for source in other['sources'] if source not in row['sources'])
        paired.append(row)
    groups = _groups(paired, _BODY_GROUPS)
    # Body-composition equipment can also register height, weight and BMI.
    # Keep the acquisition provenance while putting those values with body size.
    composition = next((group for group in groups if group['code'] == 'bodyComposition'), None)
    if composition:
        sizes = [row for row in composition['rows'] if _BODY_SIZE.search(row['name'])]
        composition['rows'] = [row for row in composition['rows'] if row not in sizes]
        if sizes:
            weight = next((group for group in groups if group['code'] == 'weightWaist'), None)
            if weight is None:
                weight = {'code': 'weightWaist', 'name': '身高、体重与围度', 'rows': [], 'summary': None}
                groups.insert(0, weight)
            weight['rows'].extend(sizes)
            weight['status'] = _template_status(weight['rows'])
        composition['status'] = _template_status(composition['rows'])
    return [group for group in groups if group['rows']]


def _owned_observation_ids(row):
    return {identifier for identifier in row.get('_equivalentOwnRecordIds', [row.get('_ownRecordId')]) if identifier}


def _explicit_atom_witness(row, quotes):
    return any(
        quote and _key(quote) and _key(quote) in _key(span['quote'])
        for identifier in _owned_observation_ids(row)
        for quote in quotes.get(identifier, ())
        for span in row.get('_sourceSpans', ())
        if span.get('recordId') == identifier
    )


def _covered_source_atom(row, shown, contexts):
    """Only same-acquisition witnesses can cover another displayed atom."""
    spans = row.get('_sourceSpans', ())
    for other in shown:
        if not _same_source_exam(row, other, contexts):
            continue
        if spans and all(
            any(
                candidate['recordId'] == span['recordId']
                and candidate['start'] <= span['start']
                and span['end'] <= candidate['end']
                for candidate in other.get('_sourceSpans', ())
            )
            for span in spans
        ):
            return other
        component = next((span.get('componentName') for span in spans if span.get('componentName')), None)
        other_component = next(
            (span.get('componentName') for span in other.get('_sourceSpans', ()) if span.get('componentName')), None
        )
        same_label = _key(_finding_name(component or row['name'].rsplit(' · ', 1)[-1])) == _key(
            _finding_name(other_component or other['name'].rsplit(' · ', 1)[-1])
        )
        if same_label and (
            _observation_value(row['value']) == _observation_value(other['value'])
            or (row.get('_verifiedFinding') or row.get('_decisionResult'))
            and (other.get('_verifiedFinding') or other.get('_decisionResult'))
            and _compatible_source_qualifiers(row, other)
            and _literal_exam_projection(row['value'], other['value'])
            or row.get('_verifiedFinding')
            and other.get('_verifiedFinding')
            and _exam_dimensions(row) is not None
            and _exam_dimensions(row) == _exam_dimensions(other)
        ):
            return other
        if component and (property_match := re.fullmatch(r'(.+?)(长径|短径|直径|大小|厚度|内径)(?:约|为)?', component)):
            subject, property_name = property_match.groups()
            other_text = str(other['value'])
            dimensions = _exam_dimensions(row, component=True)
            if (
                dimensions is not None
                and len(dimensions) == 1
                and dimensions == _exam_dimensions(other)
                and _key(subject) in _key(other['name'] + ' ' + other_text)
                and property_name in other_text
            ):
                return other
        # Two explicitly normal overall conclusions of one named study may
        # have different source-summary labels. This never joins dated and
        # undated records or different reported ages.
        if (
            row.get('_decisionResult')
            and other.get('_decisionResult')
            and row.get('_sourceNature') == other.get('_sourceNature') == 'normal_comparison'
            and _literal_normal_exam(row['value'])
            and _literal_normal_exam(other['value'])
            and clinical_group_for(row['name'], section=row['sourceSection'])
            == clinical_group_for(other['name'], section=other['sourceSection'])
            and _clinical_panel(
                row, _CLINICAL_PANELS.get(clinical_group_for(row['name'], section=row['sourceSection']), ())
            )
            == _clinical_panel(
                other, _CLINICAL_PANELS.get(clinical_group_for(other['name'], section=other['sourceSection']), ())
            )
            and _source_method(row) is not None
            and _source_method(row) == _source_method(other)
        ):
            return other
    return None


def _reader_result_text(value):
    text = unicodedata.normalize('NFKC', str(value)).strip()
    return re.sub(r'(?<=\d)\s*[xX*×]\s*(?=\d)', '×', text)


def _reader_finding_contains(short, complete, contexts):
    """A located source clause may be displayed by one fuller owned result."""
    roles = {'exam_finding', 'physical_finding'}
    if (
        short.get('_sourceRole') not in roles
        or complete.get('_sourceRole') not in roles
        or not _same_source_exam(short, complete, contexts)
        or not _compatible_source_qualifiers(short, complete)
    ):
        return False

    def witnesses(row):
        return [
            span
            for span in row.get('_sourceSpans', ())
            if span.get('recordId') in _owned_observation_ids(row)
            and span.get('role') in roles
            and type(span.get('start')) is int
            and type(span.get('end')) is int
            and span['start'] < span['end']
            and isinstance(span.get('quote'), str)
        ]

    short_spans, complete_spans = witnesses(short), witnesses(complete)
    if not short_spans or not complete_spans:
        return False
    if all(
        any(
            span['recordId'] == other['recordId'] and other['start'] <= span['start'] < span['end'] <= other['end']
            for other in complete_spans
        )
        for span in short_spans
    ):
        return True
    group = clinical_group_for(short['name'], section=short['sourceSection'])
    panel = _clinical_panel(short, _CLINICAL_PANELS.get(group, ()))
    if (
        group is None
        or group != clinical_group_for(complete['name'], section=complete['sourceSection'])
        or panel is None
        or panel != _clinical_panel(complete, _CLINICAL_PANELS[group])
    ):
        return False
    # An absent method is not proof of another study. Across records it
    # needs the same original page as well as the literal result below.
    if not (_source_method(short) and _source_method(complete)) and (
        short.get('sourcePage') is None or short.get('sourcePage') != complete.get('sourcePage')
    ):
        return False
    short_text, complete_text = (_reader_result_text(row['value']) for row in (short, complete))
    if short.get('_sourceNature') == 'normal_comparison' or complete.get('_sourceNature') == 'normal_comparison':
        return False  # "normal" in one organ cannot cover another organ.
    qualifiers = r'疑似|考虑|可能|倾向|待排|不能排除|可疑'
    if set(re.findall(qualifiers, short_text)) != set(re.findall(qualifiers, complete_text)):
        return False  # Keep source certainty differences, even with shared words.
    if (
        len(_key(short_text)) >= 4
        and not _denied_title(short_text, complete_text)
        and _literal_exam_projection(short_text, complete_text)
    ):
        return True
    dimensions = _exam_dimensions(short, component=True)
    if dimensions is None or len(dimensions) != 1 or dimensions != _exam_dimensions(complete, component=True):
        return False
    size = re.search(
        r'(?:\d+(?:\.\d*)?|\.\d+)(?:\s*[×xX*]\s*(?:\d+(?:\.\d*)?|\.\d+)){0,2}'
        r'\s*(?:mm|cm|毫米|厘米)(?![A-Za-z])',
        short_text,
        re.I,
    )
    if size is None:
        return False
    head = re.sub(r'(?:长径|短径|直径|大小|约|为|[，,：:\s])+$', '', short_text[: size.start()])
    tail = [part.strip() for part in re.split(r'[，,。；;\n]', short_text[size.end() :]) if part.strip()]
    # The same size alone is insufficient: require the literal finding
    # label and every remaining qualifier in the same fuller result.
    return (
        len(_key(head)) >= 3
        and not _denied_title(head, complete_text)
        and all(_key(part) in _key(complete_text) for part in (head, *tail))
    )


def _reader_exam_alias(conclusion, detail, contexts):
    """Group a single located report summary with the exam description it owns."""
    if any(row.get('_sourceRole') != 'exam_finding' for row in (conclusion, detail)):
        return None
    if not _compatible_source_qualifiers(conclusion, detail):
        return None
    result, description = (_reader_result_text(row['value']).rstrip('。') for row in (conclusion, detail))
    methods = (_source_method(conclusion), _source_method(detail))
    if methods == ('ultrasound', 'ultrasound'):
        valve = _VALVE_REGURGITATION.fullmatch(result)
        flow = _VALVE_FLOW_SIGNAL.fullmatch(description)
        if valve and flow and valve[1] == flow[1] and _same_source_exam(conclusion, detail, contexts, same_page=True):
            return result
    if methods == ('computedTomography', 'computedTomography'):
        calcification = _LUNG_CALCIFICATION.fullmatch(result)
        nodule = _LUNG_DENSE_NODULE.fullmatch(description)
        if (
            calcification and nodule and calcification[1] == nodule[1]
            and _owned_observation_ids(conclusion) & _owned_observation_ids(detail)
            and _same_source_exam(conclusion, detail, contexts)
        ):
            # Preserve the source's separate descriptive language without
            # presenting either line as the count of lesions in the region.
            return f'{result}。同一检查的影像描述为{description}。'
    return None


def _reader_atoms(rows, contexts):
    # A formal finding and its same-lesion support outrank an overlapping
    # description. Full rows and all original source spans remain indexed.
    ordered = list(enumerate(rows))
    finding_positions = [
        position
        for position, (_, row) in enumerate(ordered)
        if row.get('_sourceRole') in {'exam_finding', 'physical_finding'}
    ]
    findings = sorted(
        (ordered[position] for position in finding_positions),
        key=lambda item: (
            -len(str(item[1]['value'])),
            0
            if item[1].get('_verifiedFinding')
            else 1
            if item[1].get('_sourceSectionRole') == 'source_conclusion'
            else 2,
            item[0],
        ),
    )
    for position, finding in zip(finding_positions, findings):
        ordered[position] = finding
    selected = []
    for _, original in ordered:
        row = deepcopy(original)
        aliases = []
        for prior in selected:
            if text := _reader_exam_alias(row, prior, contexts):
                aliases.append((prior, row, text))
            elif text := _reader_exam_alias(prior, row, contexts):
                aliases.append((prior, prior, text))
        if len(aliases) == 1:
            prior, conclusion, text = aliases[0]
            _merge_observation_sources(prior, row, equivalent_reading=False)
            prior.update(name=conclusion['name'], value=text)
            prior.update({key: conclusion.get(key) for key in ('status', 'templateStatus', 'referenceRange')})
            continue
        covering = [other for other in selected if _reader_finding_contains(row, other, contexts)]
        duplicate = covering[0] if len(covering) == 1 else None
        if not covering:
            duplicate = _covered_source_atom(row, selected, contexts)
        if duplicate:
            same = all(row[key] == duplicate[key] for key in ('name', 'value', 'unit'))
            _merge_observation_sources(duplicate, row, equivalent_reading=same)
        else:
            selected.append(row)
    return selected


def select_overview_clinical_anchors(
    view, group_codes, identifiers, *, source_quotes=None, decision_ids=(), clinical_selections=()
):
    """Select decision observations and explicit normal counterexamples.

    A system explanation's used IDs grant source permission only. Formal
    study conclusions represent their supporting technical parameters here;
    the clinical chapter and its complete inventory still keep every value.
    """
    contexts = view.get('_sourceContexts', {})
    identifiers = set(identifiers)
    decision_ids = set(decision_ids)
    source_quotes = source_quotes or {}
    selections = defaultdict(list)
    for selection in clinical_selections:
        if isinstance(selection, dict) and selection.get('recordId') and selection.get('quote'):
            selections[selection['recordId']].append(selection['quote'])
    buckets = defaultdict(list)
    for group in view.get('clinicalGroups', ()):
        if group_codes and group['code'] not in group_codes:
            continue
        for panel in group.get('templateRows', ()):
            eligible = [
                row
                for row in panel['rows']
                if (_owned_observation_ids(row) & identifiers or _explicit_atom_witness(row, source_quotes))
            ]
            formal = [
                row
                for row in eligible
                if (
                    row.get('_sourceRole') in {'exam_finding', 'physical_finding'}
                    and row.get('_sourceNature') != 'normal_comparison'
                    and (row.get('_verifiedFinding') or row.get('_decisionResult'))
                )
            ]
            for row in eligible:
                explicit = _explicit_atom_witness(row, selections)
                authority = clinical_observation_assessment(row)
                primary = authority['assessmentState'] == 'abnormal' or row.get('_verifiedFinding')
                comparison = authority['assessmentState'] == 'normal' or row.get('_sourceNature') == 'normal_comparison'
                if formal and row not in formal and not explicit:
                    if row.get('_sourceRole') in {'exam_finding', 'physical_finding'}:
                        if not comparison or row.get('_sourceSectionRole') != 'source_conclusion':
                            continue
                    method = _source_method(row)
                    if method and any(_source_method(result) == method for result in formal):
                        if row.get('_sourceRole') in {'direct_measurement', 'derived_measurement'}:
                            continue
                if (
                    primary
                    or row in formal
                    or comparison
                    or explicit
                    or (row.get('sourceKind') == 'derived' and _owned_observation_ids(row) & decision_ids)
                ):
                    buckets[(group['code'], panel['code'], panel['name'])].append(row)
    for group in view.get('bodyGroups', ()):
        for row in group['rows']:
            if _owned_observation_ids(row) & identifiers and (
                _owned_observation_ids(row) & decision_ids
                or clinical_observation_assessment(row)['assessmentState'] == 'abnormal'
            ):
                buckets[('body', group['code'], group['name'])].append(row)

    def acquisition(row):
        context = row.get('_sourceContext') or {}
        return (
            tuple(sorted(_source_documents(row, contexts))),
            str(row.get('observedAt') or ''),
            _source_method(row),
            row.get('sourceReportedAge'),
            row.get('specimen'),
            repr(context.get('subject') or {}),
        )

    result = []
    for (_, _, label), readings in buckets.items():
        acquisitions = defaultdict(list)
        for row in _reader_atoms(readings, contexts):
            acquisitions[acquisition(row)].append(row)
        for readings in acquisitions.values():
            values, sources, in_range, source_normal = [], [], [], []
            states = []
            for row in readings:
                name = row['name'].rsplit(' · ', 1)[-1]
                value = str(row['value'])
                assessment = clinical_observation_assessment(row)
                states.append('abnormal' if row.get('_verifiedFinding') else assessment['assessmentState'])
                if assessment['referenceAssessmentState'] == 'within' and not assessment['statusReferenceConflict']:
                    if name not in in_range:
                        in_range.append(name)
                elif assessment['assessmentState'] == 'normal' or row.get('_sourceNature') == 'normal_comparison':
                    rendered = value if _key(name) in _key(value) else f'{name}：{value}'
                    if rendered not in source_normal:
                        source_normal.append(rendered)
                else:
                    # The admitted conclusion carries location, degree and
                    # classification. Same-lesion dimensions supplement it;
                    # normal wall/flow/anatomy prose stays in the full atom.
                    conclusions = [
                        span['quote'].strip('。；;，, \n')
                        for span in row.get('_sourceSpans', ())
                        if span.get('sectionRole') == 'source_conclusion'
                        and span.get('recordId') == row.get('_ownRecordId')
                    ]
                    if conclusions and row.get('_sourceRole') in {'exam_finding', 'physical_finding'}:
                        value = '；'.join(dict.fromkeys(conclusions))
                        dimensions = re.findall(
                            r'(?:\d+(?:\.\d*)?|\.\d+)(?:\s*[×xX*]\s*(?:\d+(?:\.\d*)?|\.\d+)){0,2}'
                            r'\s*(?:mm|cm|毫米|厘米)(?![A-Za-z])',
                            str(row['value']),
                            re.I,
                        )
                        extra = [size for size in dict.fromkeys(dimensions) if _key(size) not in _key(value)]
                        if extra:
                            value += '（' + '、'.join(extra) + '）'
                    rendered = value if _key(name) in _key(value) else f'{name} {value}'
                    if row.get('unit'):
                        rendered += ' ' + row['unit']
                    if rendered not in values:
                        values.append(rendered)
                sources.extend(source for source in row['sources'] if source not in sources)
            if in_range:
                values.append('、'.join(in_range) + '在各自参考范围内')
            if source_normal:
                values.extend(source_normal)
            result.append(
                {
                    'name': label,
                    'value': '；'.join(values),
                    'unit': None,
                    'referenceRange': None,
                    'status': _template_status(readings),
                    'observedAt': readings[0].get('observedAt'),
                    'sourceReportedAge': readings[0].get('sourceReportedAge'),
                    'sources': sources,
                    'evidenceRefs': [],
                    '_overviewAssessmentState': 'abnormal'
                    if 'abnormal' in states
                    else 'normal'
                    if states and all(state == 'normal' for state in states)
                    else None,
                }
            )
    return result


def _literal_deviation_description(row):
    """Retain literal change descriptions without diagnosing from a study name."""
    if row.get('_sourceSectionRole') != 'source_description' or row.get('_sourceNature') != 'patient_finding':
        return False
    text = str(row['value']).strip()
    if re.match(r'^(?:未见|未发现|未查见|未检出|否认|无(?:明显)?|不伴)', text):
        return False
    # These are directions and morphology syntax, not disease names. A
    # result section's nonnumeric change remains observable even when the
    # verified diagnosis uses a different noun. Neutral technical prose
    # does not gain this role merely by sharing the same imaging record.
    return bool(
        re.search(
            r'[增减升降变][^\d\s，,。；;：:]{1,3}|'
            r'(?:不|欠)(?:均匀|规则|连续|完整|清晰|清楚|对称|齐)|'
            r'[^\d\s，,。；;：:]{1,10}化(?:灶)?(?:$|[，,。；;])|异常|受限|中断|破坏',
            text,
        )
    )


def _urine_reference_anchor(row):
    """Retain measured counts whose printed reference is exact or unspecified."""
    unit = unicodedata.normalize('NFKC', str(row.get('unit') or '')).strip().casefold()
    if not _URINE_COUNT_UNIT.fullmatch(unit) or row.get('_sourceRole') != 'direct_measurement':
        return False
    value = _NUMBER.fullmatch(str(row.get('value', '')).strip())
    if not value or value[2]:
        return False
    reference = row.get('referenceRange')
    if not _has_value(reference):
        return True
    exact = _NUMBER.fullmatch(str(reference).strip())
    return bool(exact and not exact[2])


def _source_summary_directions(document):
    """Index a summary's own named value and direction, scoped to its check-up."""
    result = defaultdict(list)
    for fact in document.facts:
        context = getattr(fact, '_clinical_source_context', {})
        if fact.kind != 'clinical' or not (
            context.get('recordRole') == 'clinical_summary' or re.search(r'主检|体检总结', fact.name)
        ):
            continue
        doc = context.get('sourceSha256') or context.get('documentId')
        if not doc or not fact.date:
            continue
        for clause in re.split(r'[。；;\n]', fact.value):
            # Anchor both the measured noun and its numeric value; a
            # hypothetical explanation about high or low levels is excluded.
            claim = re.fullmatch(
                r'\s*([^，,：:；;]{1,40}?)(偏高|偏低|升高|降低|增高)\s*[:：]\s*'
                r'([+-]?\d+(?:\.\d+)?)\s*([a-zA-Zμµ/%·⁹¹²³0-9]+)\s*', clause,
            )
            if claim:
                key = doc, fact.date, _key(claim[1]), Decimal(claim[3]), _key(claim[4])
                result[key].append(claim[2])
    return result


def _direct_panel_summary(rows, targets, claims):
    """A source-based interpretation when the source itself answers the comparison."""
    parts = []
    for row in rows:
        label = row['name'].rsplit(' · ', 1)[-1]
        value = str(row['value'])
        status = row.get('templateStatus')
        if row.get('_sourceNature') == 'measurement_component':
            label = row.get('_sourceComponentName') or label
        if _NUMBER.fullmatch(value.strip()) and (status or row.get('_sourceNature') == 'measurement_component'):
            outcome = f'{label} {value} {row["unit"] or ""}'.strip()
            if status:
                outcome += f'（{status}' + (f'，参考{row["referenceRange"]}' if row['referenceRange'] else '') + '）'
            if row.get('observedAt') is None and row.get('sourceReportedAge') is not None:
                outcome += f'（{row["sourceReportedAge"]}岁时检查，未列检查日期）'
            parts.append(outcome)
            numeric = _NUMBER.fullmatch(value.strip())
            if status not in {'偏高', '偏低'} or numeric[2]:
                continue
            context = row.get('_sourceContext') or {}
            key = (context.get('sourceSha256') or context.get('documentId'), row.get('observedAt'),
                   _key(row['name']), Decimal(numeric[1]), _key(row['unit']))
            for direction in claims.get(key, ()):
                if (direction in {'偏高', '升高', '增高'}) != (status == '偏高'):
                    parts.append(f'同次体检总结写“{label}{direction}”，与该检验单方向不一致，以检验单数值为准')
        elif status and row.get('_sourceRole') in {'exam_finding', 'physical_finding'}:
            result = value.split('；', 1)[0].strip()
            if result and len(result) <= 90:
                timing = str(row.get('observedAt') or '')[:10]
                age = row.get('sourceReportedAge')
                context = '、'.join(filter(None, (timing or ('日期未列' if age is not None else ''),
                                                   f'{age}岁时检查' if age is not None else '')))
                parts.append(f'{label} {result}' if not context else f'{context}：{label} {result}')
    if not parts:
        return None
    parts.extend(item['message'] for item in targets)
    # Long panels put the complete readings in the measured column; a
    # purely enumerative fallback would duplicate it instead of interpreting it.
    if len(parts) > 9 and not targets:
        normal = sum(_measurement_state(row.get('templateStatus')) == 'normal' for row in rows)
        if normal != len(rows):
            return None
        return f'本次所列{normal}个已判定分项均在参考范围内或检查结论正常，可作后续同项比较的基线。'
    return '；'.join(dict.fromkeys(parts)) + '。'


def _reader_clinical_groups(groups, data, document, contexts, context=None):
    """Select the reader's table without narrowing the source planning inventory."""
    plan = getattr(data, '_assessment_content_plan', None)
    selected_ids = (
        {
            fact_id
            for section in ('clusters', 'symptoms')
            for entry in (plan.get(section) or ())
            if isinstance(entry, dict)
            for fact_id in (entry.get('clinicalFactIds') or ())
            if isinstance(fact_id, str)
        }
        if isinstance(plan, dict)
        else set()
    )
    quotes = defaultdict(list)
    for problem in document.problems:
        for source in problem.sources:
            quotes[source.fact_id].append(source.quote)
    omitted = []
    directional_claims = _source_summary_directions(document)
    for group in groups:
        original = group['templateRows']
        template = []
        for panel in original:
            pattern = _READER_NORMAL_ANCHORS.get(panel['code'])
            readings = []
            for row in panel['rows']:
                name = row['name']
                if (
                    _SOURCE_CONTEXT_RECORD.search(name)
                    or _SOURCE_BACKGROUND_RECORD.search(name)
                    or re.search(r'主检(?:结论|意见|建议)|健康教育|设备勾选', name)
                ):
                    continue
                state = _measurement_state(row['templateStatus'])
                anchor = bool(pattern and re.search(pattern, name, re.I))
                # Fixed cellScopes describe the complete permitted inventory.
                # Their factIds are never evidence of reader selection.
                planned = bool(_owned_observation_ids(row) & selected_ids)
                scalar = row.get('_sourceRole') in {'direct_measurement', 'derived_measurement'}
                explicit = _explicit_atom_witness(row, quotes)
                nature = row.get('_sourceNature')
                graded = judge(row, context).level if context is not None else None
                keep = (
                    state == 'abnormal'
                    or row.get('_verifiedFinding')
                    or graded in {'focus', 'borderline'}
                    # Items without a fixed system are listed completely in "其他检查".
                    or group['code'] == 'otherClinical'
                )
                if scalar:
                    ungraded_pattern = _READER_UNGRADED_ANCHORS.get(panel['code'])
                    direct_anchor = bool(
                        row.get('_sourceRole') == 'direct_measurement'
                        and ungraded_pattern
                        and ungraded_pattern.search(name)
                    )
                    urine_anchor = panel['code'] == 'urinalysis' and _urine_reference_anchor(row)
                    keep = (
                        keep
                        or row['sourceKind'] == 'derived'
                        or (anchor and state == 'normal')
                        or planned
                        or direct_anchor
                        or urine_anchor
                    )
                elif nature == 'measurement_component':
                    component_name = row.get('_sourceComponentName') or name
                    ungraded = _READER_UNGRADED_ANCHORS.get(panel['code'])
                    keep = keep or explicit or bool(ungraded and ungraded.search(component_name))
                else:
                    if state == 'normal' or nature == 'normal_comparison':
                        overall = row.get('_decisionResult') and row.get('_sourceRole') == 'exam_finding'
                        source_conclusion = row.get('_sourceSectionRole') == 'source_conclusion'
                        keep = keep or explicit or overall or source_conclusion or bool(
                            anchor and (row.get('_decisionResult') or row.get('_sourceRole') == 'exam_finding')
                        )
                    else:
                        keep = keep or bool(
                            nature == 'patient_finding'
                            and row.get('_sourceRole') in {'exam_finding', 'physical_finding'}
                            and (
                                explicit
                                or row.get('_sourceSectionRole') == 'source_conclusion'
                                and (row.get('_sourceRole') == 'exam_finding' or planned)
                                or (row.get('_sourceImaging') or planned)
                                and _literal_deviation_description(row)
                            )
                        )
                if keep:
                    readings.append(row)
            if readings:
                readings = _reader_atoms(readings, contexts)
                targets = _target_comparisons(readings, context)
                references = list(dict.fromkeys(item['referenceText'] for item in targets))
                template.append({
                    **panel, 'rows': readings, 'status': _reader_group_status(readings, targets, context),
                    'comparisonReference': ('目标区间：' + '；'.join(references)) if references else None,
                    '_comparisonClaims': targets,
                    'summary': _direct_panel_summary(readings, targets, directional_claims),
                })
            else:
                omitted.append('clinicalRow.' + group['code'] + '.' + panel['code'])
        group['templateRows'] = template
    return omitted


def _normal_baselines(rows, sex=None):
    """Represent tested organ functions and screening, rather than one arbitrary lab per system."""
    screening, roles = [], {code: [] for code, _ in _NORMAL_BASELINE_ROLES}
    sexed = _SEXED_ORGAN_IMAGING.get('female' if _female(sex) else 'male' if str(sex or '').strip() else '')
    for row in rows:
        if row['sourceKind'] != 'clinical':
            continue
        state = _measurement_state(row['templateStatus'])
        if _tumor_marker(row['name'], sex) and state != 'abnormal':
            screening.append(row)
            continue
        if state != 'normal':
            continue
        urine = row['specimen'] == '尿' or _urinary_name(row['name'])
        if sexed is not None and sexed.search(row['name']) and not urine:
            roles['organImaging'].append(row)
            continue
        for code, pattern in _NORMAL_BASELINE_ROLES:
            if pattern.search(row['name']):
                # An albumin in urine is not a hepatic synthesis marker, and
                # urine cell counts are not peripheral blood counts.
                urine = row['specimen'] == '尿' or _urinary_name(row['name'])
                if urine and code != 'urinary':
                    continue
                roles[code].append(row)
                break
    groups = []
    if screening:
        groups.append({'code': 'screening', 'name': '已完成的肿瘤相关筛查结果', 'rows': screening})
    baseline = [row for readings in roles.values() for row in readings]
    if baseline:
        groups.append({'code': 'organFunction', 'name': '本次已检器官功能与生理基线', 'rows': baseline})
    composition = [
        row
        for row in rows
        if _group_code(row['name'], _BODY_GROUPS, section=row['sourceSection']) == 'bodyComposition'
        and _measurement_state(row['templateStatus']) == 'normal'
        and not _BODY_SIZE.search(row['name'])
        and not _BODY_CORE.search(row['name'])
    ]
    if composition:
        groups.append({'code': 'bodyComposition', 'name': '人体成分补充记录', 'rows': composition})
    return groups


def _specialty_findings(rows, contexts, sex=None):
    """Include actual device studies and imaging without promoting minor routine labs."""
    result = []
    for row in rows:
        text = ' '.join(str(row.get(key) or '') for key in ('name', 'sourceSection', 'method'))
        state = _measurement_state(row['templateStatus'])
        dedicated = bool(
            _SPECIALTY.search(text)
            or re.search(r'微小核酸|microRNA', text, re.I)
            # A device study (3.5) has no fixed system table, so it is always kept here.
            or _DEVICE_STUDY.search(row['name'] or '') or _DEVICE_STUDY.search(row.get('sourceSection') or '')
            or _tumor_marker(row['name'], sex)
            and state == 'abnormal'
        )
        if re.match(r'^\s*(?:未检|未做|未行|未提供|未纳入|暂无)', str(row['value'])):
            continue
        actual_study = re.search(r'骨密度|脑血管功能|脑血流|四肢动脉|动脉硬化|踝肱|腕踝|eZscan', text, re.I)
        if not dedicated and not (actual_study and (row.get('clinicalDataType') == 'EXAM' or state == 'abnormal')):
            continue
        if _SOURCE_BACKGROUND_RECORD.search(row['name']) or re.search(
            r'登记|原始异常箭头|生物电阻抗|设备勾选',
            row['name'],
        ):
            continue
        # The same acquisition can have a short conclusion plus the full
        # source. Keep the richer source only when its topic and text agree.
        duplicate = next(
            (
                other
                for other in result
                if _same_source_exam(row, other, contexts)
                and _key(_EXAM_NAME_SUFFIX.sub('', row['name'])) == _key(_EXAM_NAME_SUFFIX.sub('', other['name']))
                and _observation_value(row['value']) == _observation_value(other['value'])
            ),
            None,
        )
        if duplicate is None:
            result.append(row)
    return result


def _reader_specialty_findings(rows, shown, contexts):
    """Show each remaining study result once, rather than its raw technical table."""
    candidates = []
    for row in rows:
        if row.get('_sourceRole') not in {
            'direct_measurement',
            'derived_measurement',
            'exam_finding',
            'physical_finding',
        }:
            continue
        state = _measurement_state(row['templateStatus'])
        if row.get('_sourceNature') == 'measurement_component' and state != 'abnormal':
            continue
        if row.get('_sourceNature') == 'normal_comparison' and not row.get('_decisionResult'):
            continue
        # A named instrument's overall assessment still belongs in the
        # specialty section when another table uses it for clinical context.
        device_conclusion = _screening_conclusion(row['name'], row['value'])
        if device_conclusion or _covered_source_atom(row, shown, contexts) is None:
            candidates.append(row)
    selected = _reader_atoms(candidates, contexts)
    for row in selected:
        own_age = row.get('_sourceContext', {}).get('sourceReportedAge')
        origin = _source_documents(row, contexts)
        if own_age is None or not origin:
            continue
        topic = clinical_group_for(row['name'], section=row['sourceSection'])
        panel = _clinical_panel(row, _CLINICAL_PANELS.get(topic, ()))
        differing_context = any(
            other is not row
            and _source_documents(other, contexts) == origin
            and clinical_group_for(other['name'], section=other['sourceSection']) == topic
            and _clinical_panel(other, _CLINICAL_PANELS.get(topic, ())) == panel
            and (
                other.get('_sourceContext', {}).get('sourceReportedAge') is not None
                and own_age != other['_sourceContext']['sourceReportedAge']
                or bool(row.get('observedAt')) != bool(other.get('observedAt'))
            )
            for other in [*selected, *shown]
        )
        if differing_context:
            qualifier = f'{own_age}岁时检查'
            if not row.get('observedAt'):
                qualifier += '，检查日期未注明'
            row['name'] += '（' + qualifier + '）'
    return selected


def _link_device_followups(rows, document, contexts, projection_index):
    """Bind an explicitly separate device advice record to the matching acquisition only."""
    for fact in document.facts:
        if fact.kind != 'clinical' or not _RECOMMENDATION_RECORD.search(fact.name):
            continue
        context = contexts.get(fact.fact_id, {})
        method = _source_method({'name': fact.name, **context})
        if not method or not context.get('examDate') or not context.get('sourcePage'):
            continue
        advice_record = {
            '_ownRecordId': fact.fact_id, 'name': fact.name, 'sources': [], 'observedAt': context['examDate'],
            'specimen': context.get('specimen'), **context,
        }
        matching = [row for row in rows if row.get('observedAt') and _source_method(row) == method
                    and _same_source_exam(row, advice_record, contexts, same_page=True)]
        if len(matching) > 1:
            matching = [row for row in matching if _screening_conclusion(row['name'], row['value'])]
        if len(matching) != 1:
            continue
        projection = get_patient_projection(fact, projection_index=projection_index) or {}
        clauses = []
        for span in projection.get('adviceSpans', ()):
            # Keep the follow-up sentence (including a conditional or its
            # second action), without turning the whole education record
            # into an individual follow-up instruction.
            text = span['quote'].strip(' 。；;')
            if re.match(r'^(?:建议\s*[:：]?\s*)?(?:[零一二两三四五六七八九十百半\d]+个?[天周月年]后)?'
                        r'(?:定期)?(?:复查|复测|随访|复诊)|^(?:建议|请)(?:行|做|进行)?(?:OGTT|糖耐量)', text, re.I):
                for part in re.split(r'[，,]', text):
                    if re.search(r'复查|复测|随访|复诊|OGTT|糖耐量', part, re.I):
                        clauses.append(part.removeprefix('建议：').strip())
        if clauses:
            row = matching[0]
            row['_deviceFollowup'] = '，'.join(dict.fromkeys(clauses))
            reference = {'sourceCode': 'A', 'recordId': fact.fact_id, 'observedAt': fact.date}
            if reference not in row['sources']:
                row['sources'].append(reference)


_EVIDENCE_GRADES = {'指南': 'A', '系统综述': 'A', '专家共识': 'A', '科学声明': 'A', '行业标准': 'A', '人群研究': 'B'}


def _view_row_lists(view):
    """Every public row list of the clinical view, including copies used by other sections."""
    yield view.get('bodyMetrics') or ()
    for field in ('bodyGroups', 'clinicalGroups', 'normalBaselines', 'digestiveContext'):
        for group in view.get(field) or ():
            yield group.get('rows') or ()
            for panel in group.get('templateRows') or ():
                yield panel.get('rows') or ()
    yield view.get('specialtyFindings') or ()
    for symptom in view.get('symptoms') or ():
        yield symptom.get('clinicalSignals') or ()


def _annotate_view(view, context: SubjectContext):
    """Structured row fields (spec 2.7) and the level/anchors of each group and panel."""
    for rows in _view_row_lists(view):
        annotate_rows(rows, context)
    for field in ('clinicalGroups', 'bodyGroups'):
        for group in view.get(field) or ():
            for panel in group.get('templateRows') or ():
                panel['level'] = worst_level(row.get('level') for row in panel.get('rows') or ())
            panels = group.get('templateRows')
            members = [row for panel in panels for row in panel.get('rows') or ()] if panels else group.get('rows')
            group.update(summarize_levels(members))


def _reference_identity(doi, title):
    return (str(doi or '').strip().casefold() or None, re.sub(r'\W+', '', str(title or '')).casefold() or None)


def reference_index(library):
    """``{identity: number}`` of the bibliography entries, by DOI and by title."""
    numbers = {}
    for entry in library:
        for key in _reference_identity(entry.doi, entry.title):
            if key:
                numbers.setdefault(key, entry.number)
    return numbers


def cite_reference(library, numbers, source, *, study_type=None, section, added=None):
    """Number of ``source`` in the final bibliography; appended with the next free number if new."""
    from .evaluation_schema import ReferenceLiterature

    doi_key, title_key = _reference_identity(source.get('doi'), source.get('title'))
    number = numbers.get(doi_key) or numbers.get(title_key)
    if number is not None:
        return number
    number = max((entry.number for entry in library), default=0) + 1
    year = source.get('year')
    if not year and (found := re.search(r'(?<!\d)(?:19|20)\d{2}(?!\d)', str(source.get('citation') or ''))):
        year = found.group()
    library.append(ReferenceLiterature(
        number=number, title=source.get('title'), year=str(year or '') or None,
        journal=source.get('journal') or source.get('citation'), url=source.get('url'), doi=source.get('doi'),
        study_type=study_type, evidence_grade=_EVIDENCE_GRADES.get(study_type or '', 'C'),
        supporting_sections=[section],
    ))
    if added is not None:
        added.append(number)
    for key in (doi_key, title_key):
        if key:
            numbers[key] = number
    return number


def attach_target_references(view, data):
    """Number the target-table literature after the report bibliography and cite it on each target.

    The bibliography is final when the six-chapter projection is built, so a
    target source either reuses an existing entry (same DOI or title) or is
    appended with the next free number. The callback keeps every cited number.
    """
    appendices = getattr(data, 'appendices', None)
    library = getattr(appendices, 'reference_literature', None)
    if library is None:
        return []
    numbers = reference_index(library)
    added = []
    for rows in _view_row_lists(view):
        for row in rows:
            target = row.get('target') if isinstance(row, dict) else None
            if not target:
                continue
            refs = [
                cite_reference(library, numbers, source, study_type=target.get('evidenceLevel'),
                               section='assessmentPresentation.clinical.target', added=added)
                for source in target.get('sources') or ()
            ]
            target['evidenceRefs'] = list(dict.fromkeys(refs))
    return added


def build_clinical_view(request, parsed, document, data, *, reader_plan=False) -> dict:
    """Use the vetted report's source inventory and mark the role of each observation."""
    hospital = list(_hospital_rows(request, parsed))
    contexts = _source_context(hospital)
    projection_index = ensure_patient_projection_index(document)
    rows = _deduplicate_clinical_rows(
        list(_clinical_rows(data, contexts, _exam_finding_sources(document), document, projection_index)),
        contexts,
    )
    _attach_context_witnesses(rows, contexts)
    sex = getattr(parsed.subject, 'sex', None)
    context = subject_context(parsed.subject, document, rows)
    measurements = [
        row
        for row in rows
        if not (row.get('clinicalDataType') == 'EXAM' and _SOURCE_CONTEXT_RECORD.search(row['name']))
        and not _SOURCE_BACKGROUND_RECORD.search(row['name'])
    ]
    body_metrics = _body_metrics(measurements, contexts)
    if recorded_bmi := _registered_bmi(data, body_metrics):
        body_metrics.append(recorded_bmi)
    specialty = _specialty_findings(measurements, contexts, sex)
    grouped_rows = [
        row
        for row in measurements
        if _group_code(row['name'], _BODY_GROUPS, section=row['sourceSection']) is None
        and not _SPECIALTY.search(row['name'])
        and not _SPECIALTY.search(row['sourceSection'] or '')
        and not _SOURCE_BACKGROUND_RECORD.search(row['name'])
    ]
    symptoms = _symptoms(document, rows)
    clinical_groups = _groups(grouped_rows, _CLINICAL_GROUPS, sex=sex)
    omitted = _reader_clinical_groups(clinical_groups, data, document, contexts, context) if reader_plan else []
    source_body_groups = _groups(body_metrics, _BODY_GROUPS)
    body_groups = _reader_body_groups(body_metrics, contexts) if reader_plan else source_body_groups
    if reader_plan:
        for group in body_groups:
            if not all(row['sourceKind'] == 'recordedHistory' for row in group['rows']):
                group['status'] = _reader_group_status(group['rows'], context=context)
        shown_body_codes = {group['code'] for group in body_groups}
        omitted.extend('body.' + group['code'] for group in source_body_groups if group['code'] not in shown_body_codes)
        shown = [row for group in clinical_groups for panel in group['templateRows'] for row in panel['rows']]
        shown.extend(row for group in body_groups for row in group['rows'])
        specialty = _reader_specialty_findings(specialty, shown, contexts)
        _link_device_followups(specialty, document, contexts, projection_index)
    result = {
        'bodyMetrics': body_metrics,
        'bodyGroups': body_groups,
        'symptoms': symptoms,
        'medicationRecords': _medication_records(data, document),
        'clinicalGroups': clinical_groups,
        'specialtyFindings': specialty,
        'trends': _trends(hospital),
        'normalBaselines': _normal_baselines(measurements, sex),
        'digestiveContext': _digestive_context(
            [row for group in clinical_groups for panel in group['templateRows'] for row in panel['rows']]
            if reader_plan
            else grouped_rows,
            symptoms,
            contexts=contexts if reader_plan else None,
            source_rows=grouped_rows if reader_plan else (),
        ),
    }
    _annotate_view(result, context)
    if reader_plan:
        attach_target_references(result, data)
        result['_readerOmittedCells'] = omitted
        result['_sourceContexts'] = contexts
    return result
