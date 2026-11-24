"""Fixed patient-facing copy and approved report grading definitions.

This module does not calculate patient scores or supply example patient data.
It maps already supplied values to the fixed report grading definitions.
Appendix B is deliberately absent: its bibliography belongs to actual citations.
"""

from decimal import Decimal, InvalidOperation
import re
import unicodedata

SCHEMA_VERSION = "evaluate.v2.0"
REPORT_TITLE = "用户整体健康评估报告"
REPORT_SUBTITLE = "多源资料综合健康评估"
ASSESSMENT_NATURE = "整体健康评估"
COVER_DISCLAIMER = "本报告供健康管理参考，不作为临床诊断或处方。"
INTRODUCTION_TEXT = (
    "本报告综合您已提供的检查、检测、问卷和画像资料，评估整体健康状态，明确需要优先关注的健康问题。"
    "按肠道、代谢、免疫、衰老、精神与睡眠、神经六个维度整理疾病与健康问题，结合实际资料确定健康管理重点。"
)
IMPORTANT_DECLARATIONS = (
    "健康管理关注评分用于安排改善重点，分数越高表示当前所需关注越低，不代表个人患病概率。",
    "健康管理建议不能替代医生面诊和药物治疗。",
    "用药、停药或调整用药方案，请先咨询医生并遵医嘱。",
)
MEDICAL_DESCRIPTION = (
    "以下列出体检发现、主要不适和相关病史，并分析生活习惯等因素与健康问题的关系。"
)
MICROBIOME_DESCRIPTION = (
    "评估菌群结构、关键菌群及相关功能，结合个人健康信息说明需要关注的问题。"
)
RISK_INDEX_GRADING_TEXT = (
    "菌群风险指数分级：0–20 低风险、20–40 中低风险、40–60 中风险、60–80 中高风险、80–100 高风险。"
)
DISEASE_RISK_DESCRIPTION = (
    "综合用户已提供的检查、检测、问卷和画像资料，按肠道、代谢、免疫、衰老、精神与睡眠、神经六个维度说明疾病与健康风险及需要关注的问题。"
)
DISEASE_RISK_NOTICE = ""
NUTRITION_DESCRIPTION = (
    "评估营养物质相关的菌群功能，结合实际饮食、消化耐受及健康状态确定营养关注重点。"
)
MAJOR_NUTRITION_SCORE_RANGE = "0–100分；≤10弱、10–30较弱、>30正常"
SOURCE_NUTRITION_DESCRIPTION = (
    "评估营养物质相关的菌群功能，结合实际饮食、消化耐受及健康状态确定营养关注重点。"
)
NEUTRAL_NUTRITION_DESCRIPTION = "评估营养物质相关的菌群功能及个人营养关注重点。"
NUTRITION_DISPLAY_LEVELS = ("极低", "偏低", "弱", "较弱", "正常", "偏高")
LOW_FUNCTION_LEVELS = frozenset({"极低", "偏低", "弱", "较弱", "合成能力弱"})
RESISTANCE_NOTICE = ""
PATHWAY_DESCRIPTION = "按代谢通路说明菌群功能，以及与营养代谢和健康的关系。"
APPENDIX_B_DESCRIPTION = "本报告引用的参考文献。"

SOURCE_DEFINITIONS = (
    ("A", "检查与其他检测资料", "汇总体检、专科检查及其他检测中的疾病和异常"),
    ("B", "问卷评估", "补充主要不适、病史和生活习惯"),
    ("C", "肠道菌群检测", "评估肠道菌群、相关功能和健康风险"),
)
RISK_CATEGORY_DEFINITIONS = (
    ("intestinal", "肠道相关疾病"),
    ("metabolic", "代谢相关疾病"),
    # The historical code is retained for callback compatibility.  This is
    # the broader immune dimension, not a diagnosis of autoimmunity.
    ("autoimmune", "免疫相关疾病"),
    ("aging", "衰老相关疾病"),
    ("mental", "精神与睡眠相关疾病"),
    ("neurological", "神经相关疾病"),
)
# Identities, never example patient values.  The current 29-page approved
# template has thirty disease questions and a separate ageing assessment.
FIXED_DISEASE_DEFINITIONS = (
    ("intestinal", "ulcerativeColitis", "溃疡性结肠炎"),
    ("intestinal", "inflammatoryBowelDisease", "炎症性肠病（总体）"),
    ("intestinal", "crohnDisease", "克罗恩病"),
    ("intestinal", "constipation", "便秘"),
    ("intestinal", "ibsDiarrhea", "腹泻型肠易激综合征"),
    ("intestinal", "recurrentClostridioidesDifficileInfection", "复发性艰难梭菌感染"),
    ("intestinal", "irritableBowelSyndrome", "肠易激综合征"),
    ("metabolic", "type2Diabetes", "II 型糖尿病"),
    ("metabolic", "kidneyStones", "肾结石"),
    ("metabolic", "coronaryHeartDisease", "冠心病"),
    ("metabolic", "nonalcoholicFattyLiver", "非酒精性脂肪肝"),
    ("metabolic", "hyperlipidemia", "高脂血症"),
    ("metabolic", "obesity", "肥胖"),
    ("metabolic", "hypertension", "高血压"),
    ("metabolic", "hyperuricemia", "高尿酸血症"),
    ("metabolic", "liverCirrhosis", "肝硬化"),
    ("metabolic", "metabolicAssociatedFattyLiverDisease", "代谢功能障碍相关脂肪性肝病"),
    ("autoimmune", "systemicLupusErythematosus", "系统性红斑狼疮"),
    ("autoimmune", "rheumatoidArthritis", "类风湿性关节炎"),
    ("autoimmune", "ankylosingSpondylitis", "强直性脊柱炎"),
    ("autoimmune", "autoimmuneHepatitis", "自身免疫性肝炎"),
    ("autoimmune", "type1Diabetes", "1型糖尿病"),
    ("mental", "anxiety", "焦虑症"),
    ("mental", "depressiveTendency", "抑郁倾向"),
    ("mental", "schizophrenia", "精神分裂症"),
    ("mental", "bipolarDisorder", "双相情感障碍"),
    ("mental", "autismSpectrumDisorder", "孤独症谱系障碍"),
    ("mental", "majorDepressiveDisorder", "重性抑郁障碍"),
    ("neurological", "parkinsonsDisease", "帕金森病"),
    ("neurological", "epilepsy", "癫痫"),
    ("autoimmune", "multipleSclerosis", "多发性硬化症"),
    ("neurological", "alzheimersDisease", "阿尔兹海默症"),
    ("neurological", "mildCognitiveImpairment", "轻度认知障碍"),
    ("aging", "frailty", "衰弱"),
    ("aging", "sarcopenia", "肌少症"),
    ("aging", "osteoporosis", "骨质疏松症"),
    ("aging", "atherosclerosis", "动脉粥样硬化"),
    ("aging", "cognitiveDecline", "认知下降"),
    ("metabolic", "thyroidCancer", "甲状腺癌"),
    ("intestinal", "colorectalCancer", "结直肠癌"),
    ("intestinal", "gastricCancer", "胃癌"),
    ("metabolic", "liverCancer", "肝癌"),
)
FIXED_DISEASE_ALIASES = {
    "ulcerativeColitis": ("溃疡性结肠炎", "ulcerative colitis", "UC"),
    "inflammatoryBowelDisease": ("炎症性肠病（总体）", "炎症性肠病总体", "炎症性肠病", "inflammatory bowel disease", "IBD"),
    "crohnDisease": ("克罗恩病", "克罗恩氏病", "Crohn disease", "Crohn's disease", "CD"),
    "constipation": ("便秘", "功能性便秘", "functional constipation"),
    # Generic IBS is deliberately not a synonym of the diarrhoea subtype.
    "ibsDiarrhea": ("腹泻型肠易激综合征", "腹泻型肠易激综合症", "IBS-D", "diarrhea predominant irritable bowel syndrome", "diarrhoea predominant irritable bowel syndrome"),
    "type2Diabetes": ("II型糖尿病", "Ⅱ型糖尿病", "2型糖尿病", "type 2 diabetes", "type 2 diabetes mellitus", "T2DM"),
    "kidneyStones": ("肾结石", "kidney stone", "kidney stones", "nephrolithiasis"),
    "coronaryHeartDisease": ("冠心病", "冠状动脉粥样硬化性心脏病", "coronary heart disease"),
    "nonalcoholicFattyLiver": ("非酒精性脂肪肝", "非酒精性脂肪性肝病", "非酒精性脂肪肝病", "nonalcoholic fatty liver disease", "NAFLD"),
    "hyperlipidemia": ("高脂血症", "血脂异常", "混合型高脂血症", "hyperlipidemia", "dyslipidemia"),
    "obesity": ("肥胖", "肥胖症", "obesity"),
    "hypertension": ("高血压", "hypertension"),
    "hyperuricemia": ("高尿酸血症", "hyperuricemia"),
    "liverCirrhosis": ("肝硬化", "liver cirrhosis", "cirrhosis"),
    "systemicLupusErythematosus": ("系统性红斑狼疮", "systemic lupus erythematosus", "SLE"),
    "rheumatoidArthritis": ("类风湿性关节炎", "类风湿关节炎", "rheumatoid arthritis", "RA"),
    "ankylosingSpondylitis": ("强直性脊柱炎", "ankylosing spondylitis"),
    "autoimmuneHepatitis": ("自身免疫性肝炎", "autoimmune hepatitis"),
    "anxiety": ("焦虑症", "焦虑障碍", "anxiety", "anxiety disorder"),
    "depressiveTendency": ("抑郁倾向", "抑郁症", "抑郁障碍", "重度抑郁障碍", "depression", "major depressive disorder", "MDD"),
    "schizophrenia": ("精神分裂症", "schizophrenia"),
    "bipolarDisorder": ("双相情感障碍", "双向情感障碍", "双相障碍", "bipolar disorder"),
    "parkinsonsDisease": ("帕金森病", "帕金森综合征", "帕金森综合症", "Parkinson disease", "Parkinson's disease", "parkinsonism"),
    "epilepsy": ("癫痫", "epilepsy"),
    "multipleSclerosis": ("多发性硬化症", "多发性硬化", "multiple sclerosis"),
    "alzheimersDisease": ("阿尔兹海默症", "阿尔茨海默症", "阿尔茨海默病", "Alzheimer disease", "Alzheimer's disease"),
    "thyroidCancer": ("甲状腺癌", "thyroid cancer"),
    "colorectalCancer": ("结直肠癌", "大肠癌", "colorectal cancer", "CRC"),
    "gastricCancer": ("胃癌", "gastric cancer", "stomach cancer"),
    "liverCancer": ("肝癌", "liver cancer", "hepatocellular carcinoma"),
    "irritableBowelSyndrome": ("肠易激综合征", "肠易激综合症", "irritable bowel syndrome", "IBS"),
    "autismSpectrumDisorder": ("孤独症", "孤独症谱系障碍", "自闭症", "自闭症谱系障碍", "autism spectrum disorder"),
    "recurrentClostridioidesDifficileInfection": ("复发性艰难梭菌感染", "复发艰难梭菌感染", "复发性艰难梭状芽孢杆菌感染", "recurrent Clostridioides difficile infection", "recurrent Clostridium difficile infection", "rCDI"),
    "metabolicAssociatedFattyLiverDisease": ("代谢功能障碍相关脂肪性肝病", "代谢相关脂肪性肝病", "metabolic dysfunction-associated steatotic liver disease", "MASLD"),
    "type1Diabetes": ("1型糖尿病", "I型糖尿病", "Ⅰ型糖尿病", "type 1 diabetes", "type 1 diabetes mellitus", "T1D", "T1DM"),
    "majorDepressiveDisorder": ("重性抑郁障碍",),
    "mildCognitiveImpairment": ("轻度认知障碍", "轻度认知功能障碍", "mild cognitive impairment", "MCI"),
    "frailty": ("衰弱", "衰弱综合征", "frailty"),
    "sarcopenia": ("肌少症", "肌肉减少症", "sarcopenia"),
    "osteoporosis": ("骨质疏松", "骨质疏松症", "osteoporosis"),
    "atherosclerosis": ("动脉粥样硬化", "atherosclerosis"),
    "cognitiveDecline": ("认知下降", "认知功能下降", "cognitive decline"),
}

# Source model directories only. Health-association screening panels keep
# their original separate module; none of these entries carries a patient value.
SOURCE_DISEASE_CATALOGS = {
    'GM': (
        ('intestinal', 'crohnDisease', '克罗恩病'),
        ('intestinal', 'ulcerativeColitis', '溃疡性结肠炎'),
        ('tumor', 'colorectalCancer', '结直肠癌'),
        ('intestinal', 'irritableBowelSyndrome', '肠易激综合征'),
        ('tumor', 'gastricCancer', '胃癌'),
        ('mental', 'schizophrenia', '精神分裂症'),
        ('mental', 'depressiveTendency', '抑郁症'),
        ('metabolic', 'type2Diabetes', '2型糖尿病'),
        ('metabolic', 'nonalcoholicFattyLiver', '非酒精性脂肪肝'),
        ('metabolic', 'liverCirrhosis', '肝硬化'),
    ),
    'G1': (
        ('neurological', 'autismSpectrumDisorder', '孤独症'),
        ('mental', 'depressiveTendency', '抑郁障碍'),
        ('intestinal', 'crohnDisease', '克罗恩病'),
        ('intestinal', 'ulcerativeColitis', '溃疡性结肠炎'),
        ('intestinal', 'irritableBowelSyndrome', '肠易激综合征'),
    ),
    'GS.meta': (
        ('intestinal', 'ulcerativeColitis', '溃疡性结肠炎'),
        ('tumor', 'colorectalCancer', '结直肠癌'),
        ('intestinal', 'irritableBowelSyndrome', '肠易激综合征'),
        ('tumor', 'gastricCancer', '胃癌'),
        ('mental', 'schizophrenia', '精神分裂症'),
        ('mental', 'depressiveTendency', '抑郁症'),
        ('metabolic', 'type2Diabetes', 'Ⅱ型糖尿病'),
        ('metabolic', 'liverCirrhosis', '肝硬化'),
    ),
}

# Each known version uses its actual source directory. Historical adult-ten
# callbacks remain recognisable by evaluation_disease_catalog's compatibility
# path; a new GS.meta projection does not add two unmeasured model slots.
# All actual source health modules belong in the adopted directory, including
# normal screens.  Only exact same-concept panels share a measured model row;
# a parent IBD panel is never a Crohn/UC model feature and mood != depression.
SOURCE_ASSOCIATION_DIRECTORY = {
    'GM': (
        ('intestinal', 'jkj_ganran', '胃肠道感染', None),
        ('intestinal', 'jkj_IBD', '炎症性肠病', None),
        ('intestinal', 'jkj_IBS', '肠易激综合征', 'irritableBowelSyndrome'),
        ('intestinal', 'jkj_futong', '腹痛', None),
        ('intestinal', 'jkj_chronic_diarrhea', '腹泻', None),
        ('intestinal', 'jkj_fuzhangpaiqi', '腹胀排气', None),
        ('intestinal', 'jkj_constipation', '功能性便秘', None),
        ('intestinal', 'jkj_weizhang', '胃胀', None),
        ('metabolic', 'jkj_HF', '心血管相关', None),
        ('metabolic', 'jkj_fat', '肥胖症', None),
        ('metabolic', 'jkj_T2D', 'Ⅱ型糖尿病', 'type2Diabetes'),
        ('allergy', 'jkj_allergy', '过敏症', None),
        ('metabolic', 'jkj_hepatopathy', '代谢相关脂肪性肝病', None),
        ('aging', 'jkj_osteoporosis', '骨质疏松症', None),
        ('mental', 'jkj_yiyu', '情绪障碍', None),
        ('mental', 'jkj_shuimian', '睡眠障碍', None),
        ('mental', 'jkj_jingfen', '精神分裂症', 'schizophrenia'),
        ('respiratory', 'jkj_shuimianhuxizanting', '睡眠呼吸暂停综合征', None),
        ('mental', 'jkj_aud', '酒精使用障碍', None),
        ('neurological', 'jkj_AD', '阿尔兹海默症', None),
        ('neurological', 'jkj_PD', '帕金森综合征', None),
    ),
    'G1': (
        ('nutrition', 'jkj_children_innutrition', '营养不良', None),
        ('nutrition', 'jkj_children_IDA', '缺铁性贫血', None),
        ('metabolic', 'jkj_children_obesity', '肥胖', None),
        ('allergy', 'jkj_children_AD', '湿疹', None),
        ('allergy', 'jkj_children_AR', '过敏性鼻炎', None),
        ('respiratory', 'jkj_children_asthma', '哮喘', None),
        ('respiratory', 'jkj_children_RRTI', '反复呼吸道感染', None),
        ('intestinal', 'jkj_children_CDD', '慢性腹泻', None),
        ('intestinal', 'jkj_children_FC', '功能性便秘', None),
        ('autoimmune', 'jkj_children_CeD', '乳糜泻（麸质过敏症）', None),
        ('allergy', 'jkj_children_HSP', '过敏性紫癜', None),
        ('allergy', 'jkj_children_FA', '食物过敏', None),
        ('intestinal', 'jkj_children_FD', '功能性消化不良', None),
        ('neurological', 'jkj_children_TD', '抽动障碍', None),
        ('neurological', 'jkj_children_ADHD', '多动症', None),
        ('neurological', 'jkj_children_ASD', '孤独症', 'autismSpectrumDisorder'),
        ('mental', 'jkj_children_yiyu', '抑郁障碍', 'depressiveTendency'),
    ),
}
SOURCE_RISK_CATEGORY_DEFINITIONS = RISK_CATEGORY_DEFINITIONS
EVALUATION_DISEASE_CATALOGS = {
    family: (*SOURCE_DISEASE_CATALOGS[family],
        *((group, 'nativeAssociation:' + key, title)
            for group, key, title, model_code in rows if model_code is None))
    for family, rows in SOURCE_ASSOCIATION_DIRECTORY.items()
}
EVALUATION_DISEASE_CATALOGS['GS.meta'] = SOURCE_DISEASE_CATALOGS['GS.meta']



def source_disease_risk_description(family):
    return NATIVE_DISEASE_RISK_DESCRIPTION


def _template_identity_key(value):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


def fixed_disease_code(value):
    """Match a complete source identity; never infer a disease subtype."""
    key = _template_identity_key(value)
    for suffix in ("风险评估", "风险评分", "风险指数", "菌群风险", "风险", "riskassessment", "riskscore", "risk"):
        if key.endswith(suffix):
            key = key[:-len(suffix)]
            break
    native = next((code for rows in EVALUATION_DISEASE_CATALOGS.values()
        for _, code, _ in rows if code.startswith('nativeAssociation:')
        and _template_identity_key(code) == key), None)
    return native or _FIXED_DISEASE_IDENTITIES.get(key)


# The approved PDF uses the overview order above, but orders two detailed
# tables differently. These are display orders, never new source identities.
FIXED_DISEASE_DETAIL_ORDER = {
    "mental": ("anxiety", "depressiveTendency", "bipolarDisorder", "schizophrenia",
        "autismSpectrumDisorder", "majorDepressiveDisorder"),
    "neurological": ("parkinsonsDisease", "alzheimersDisease", "epilepsy", "mildCognitiveImpairment"),
}


def disease_category(name=None, *, code=None, category=None, system=None):
    """Route a real item to one display dimension without inferring its cause.

    Source directories and disease identities stay unchanged. Historical
    tumor/allergy/respiratory/nutrition headings are accepted as routing hints;
    a remaining source disease stays visible in the broad immune dimension.
    """
    # An integrated row carries its adjudicated axis in the existing code.
    # Revalidating a stored callback must not repartition an unfamiliar name
    # using a legacy keyword fallback and invalidate the reviewed category snapshot.
    if isinstance(code, str) and code.startswith('integratedRisk:'):
        parts = code.split(':', 2)
        if len(parts) == 3 and parts[1] in dict(RISK_CATEGORY_DEFINITIONS) and parts[2]:
            return parts[1]
    identity = fixed_disease_code(code) or fixed_disease_code(name)
    placements = {item_code: group for group, item_code, _ in FIXED_DISEASE_DEFINITIONS}
    if identity in placements:
        return placements[identity]
    native_key = str(code or '').removeprefix('nativeAssociation:')
    native = next(((group, title) for rows in SOURCE_ASSOCIATION_DIRECTORY.values()
        for group, key, title, _ in rows if key == native_key), None)
    if native is not None:
        category, name = native
        if native_key == 'jkj_children_ASD':
            return 'mental'
    text = _template_identity_key(name)
    organ_scope = text + _template_identity_key(str(system or '') + ' ' + str(category or ''))
    # Tumour is accepted as a historical source heading, not a seventh
    # display dimension. Keep the supplied lesion identity and route it by
    # organ; the route never changes a nodule or polyp into a malignancy.
    if category == 'tumor' or re.search(
        r'癌|肿瘤|结节|息肉|囊肿|腺瘤|肿块|占位|胶质瘤|脑膜瘤|神经瘤|'
        r'carcinoma|cancer|tumou?r|neoplasm|glioma|meningioma|nodule|polyp|cyst|adenoma|mass', text
    ):
        for group, pattern in (
            ('intestinal', r'胃|肠|食管|食道|肛|gastro|stomach|intestin|colorect|colon|rectal|esophag|oesophag'),
            ('metabolic', r'肝|胆|胰|甲状腺|甲旁腺|肾|泌尿|膀胱|前列腺|乳腺|卵巢|子宫|内分泌|'
                r'liver|hepat|biliar|gallbladder|pancrea|thyroid|parathyroid|renal|kidney|urinar|bladder|'
                r'prostat|breast|ovari|uter|endocrin'),
            ('neurological', r'脑|脊髓|神经|胶质|brain|cerebr|spinal|neurolog|glioma|meningioma|schwannoma'),
            ('autoimmune', r'血液|骨髓|淋巴|肺|呼吸|皮肤|hematolog|haematolog|leukemia|lymph|lung|'
                r'pulmonary|respirator|skin|melanoma'),
            ('aging', r'骨|肌|软组织|bone|muscle|softtissue'),
        ):
            if re.search(pattern, organ_scope, re.I):
                return group
    for group, pattern in (
        ('autoimmune', r'多发性硬化|multiplesclerosis|[1iⅠ]型糖尿病|type1diabet|^t1dm?$|自身免疫|类风湿|红斑狼疮|桥本|银屑病|过敏|哮喘|湿疹|紫癜|免疫|感染|autoimmun|rheumatoid|lupus|hashimoto|psoriasis|allerg|asthma|eczema|immun|infect'),
        ('mental', r'孤独症|自闭症|autismspectrumdisorder|抑郁|焦虑|精神分裂|情绪障碍|双相|depress|anxiety|schizophren|bipolar|睡眠障碍|睡眠呼吸暂停|失眠|sleepdisorder|insomnia|sleepapn'),
        ('aging', r'衰老|衰弱|肌少|骨质疏松|认知下降|认知功能下降|动脉粥样硬化|aging|ageing|frailty|osteoporosis|sarcopen|cognitivedecline|^atherosclerosis$'),
        ('neurological', r'阿尔[茨兹]海默|帕金森|轻度认知|认知障碍|癫痫|神经|脑|alzheimer|parkinson|mildcognitive|^mci$|epilep|neurolog'),
        ('intestinal', r'克罗恩|结肠炎|炎症性肠病|肠易激|胃肠|肠道|艰难梭菌|艰难梭状|腹泻|便秘|胃炎|胃病|消化|crohn|colitis|irritablebowel|diarrh|constipation|clostridioidesdifficile|clostridiumdifficile|gastro|digest'),
        ('metabolic', r'糖尿病|肥胖|血脂异常|高血脂|高脂血|脂肪肝|脂肪性肝|肝硬化|肾结石|代谢|心血管|冠心|高血压|尿酸|营养|贫血|内分泌|diabet|obesity|dyslipid|hyperlipid|fattyliver|steatotic|metaboli|cardiovascular|hypertension|malnutrition|anemia'),
    ):
        if re.search(pattern, text, re.I):
            # An enteric infection belongs to the gut dimension, even though
            # the immune locator also recognises the literal word infection.
            if group == 'autoimmune' and re.search(r'胃肠|肠道|艰难梭|enteric|gastrointestin|clostridioidesdifficile|clostridiumdifficile', text):
                return 'intestinal'
            return group
    if category in dict(RISK_CATEGORY_DEFINITIONS):
        return category
    # Supplied system/department metadata is a bounded fallback for an
    # unfamiliar disease name; it never changes a recognised disease's axis.
    system_scope = _template_identity_key(str(system or '') + ' ' + str(category or ''))
    for group, pattern in (
        ('intestinal', r'肠|胃|消化|intestinal|digest|gastro'),
        ('metabolic', r'代谢|内分泌|营养|metaboli|endocrin|nutrition'),
        ('autoimmune', r'免疫|风湿|过敏|呼吸|immun|rheumat|allerg|respirator'),
        ('neurological', r'神经|脑|neurolog|brain'),
        ('mental', r'精神|心理|mental|psychiatr|psycholog'),
        ('aging', r'老年|衰老|老龄|aging|ageing|geriatr'),
    ):
        if re.search(pattern, system_scope, re.I):
            return group
    return {'allergy': 'autoimmune', 'respiratory': 'autoimmune',
        'nutrition': 'metabolic'}.get(category, 'autoimmune')


def fixed_disease_definitions(category, *, detail=False, family=None):
    if family in EVALUATION_DISEASE_CATALOGS:
        return tuple((code, name) for group, code, name in EVALUATION_DISEASE_CATALOGS[family]
            if disease_category(name, code=code, category=group) == category)
    definitions = tuple((code, name) for group, code, name in FIXED_DISEASE_DEFINITIONS if group == category)
    if detail and category in FIXED_DISEASE_DETAIL_ORDER:
        names = dict(definitions)
        return tuple((code, names[code]) for code in FIXED_DISEASE_DETAIL_ORDER[category])
    return definitions


_FIXED_DISEASE_IDENTITIES = {_template_identity_key(term): code
    for _, code, name in (*FIXED_DISEASE_DEFINITIONS,
        *(row for rows in SOURCE_DISEASE_CATALOGS.values() for row in rows),
        *(row for rows in EVALUATION_DISEASE_CATALOGS.values() for row in rows
            if not row[1].startswith('nativeAssociation:')))
    for term in (code, name, *FIXED_DISEASE_ALIASES.get(code, ()))}
# Source health-association headings are qualitative observations, never
# disease-model scores. Keep unclassified topics in their original section:
# allergy is not automatically autoimmune disease, nor is sleep apnoea a
# psychiatric or neurological diagnosis.
HEALTH_ASSOCIATION_CATEGORIES = {
    "胃肠道感染": "intestinal", "腹痛": "intestinal", "腹泻": "intestinal",
    "腹胀排气": "intestinal", "胃胀": "intestinal", "功能性便秘": "intestinal",
    "心血管相关": "metabolic", "肥胖症": "metabolic", "代谢相关脂肪性肝病": "metabolic",
    "骨质疏松症": "aging", "骨质疏松": "aging",
    "情绪障碍": "mental", "睡眠障碍": "mental", "酒精使用障碍": "mental",
}
PATHWAY_DEFINITIONS = (
    ("P1", "碳水化合物代谢"), ("P2", "脂质与胆汁酸代谢"),
    ("P3", "短链脂肪酸合成"), ("P4", "氨基酸合成"),
    ("P5", "维生素合成"), ("P6", "神经递质合成"),
    ("P7", "核苷酸代谢"), ("P8", "天然色素合成"),
    ("P9", "致病潜力/有害负荷"), ("P10", "防御与耐受"),
)
TRIGGER_DEFINITIONS = (
    ("diet", "饮食"), ("exercise", "运动/久坐"), ("sleep", "睡眠/节律"),
    ("stress", "心理/压力"), ("medications", "药物"),
    ("exposures", "烟酒/环境"), ("foodIntolerance", "食物不耐受"),
    ("familyHistory", "遗传易感"),
)
OVERVIEW_SCORE_DEFINITIONS = (
    ("medical", "医学评估"),
    ("microbiome", "肠道菌群健康度评分"),
    ("nutrition", "营养与功能评估"),
    ("overall", "整体健康综合评估"),
)
# Shared identities used by the state assessor. The overview itself renders
# supplied source modules; this list is not a required twelve-item display.
MICROBIOME_OVERVIEW_DEFINITIONS = (
    ("gmwi2", "肠道微生物组健康指数（GMWI2）"),
    ("microbiomeHealthScore", "肠道菌群健康度评分"),
    ("speciesCount", "菌群种类数"), ("diversity", "菌群多样性"),
    ("beRatio", "B/E 比值（双歧杆菌属/肠杆菌科）"),
    ("fbRatio", "F/B 比值（厚壁菌门/拟杆菌门）"),
    ("pathogenicPotential", "肠道菌群致病潜力"),
    ("beneficialBacteria", "有益菌检出"), ("pathogenicBacteria", "致病菌"),
    ("antiInflammatoryCapacity", "肠道抗炎能力"),
    ("gutImmunity", "肠道免疫力评估"), ("dietaryFiberNeed", "膳食纤维需求"),
)
OVERVIEW_COMPONENT_SCORE_DEFINITIONS = (
    ("gutImmunity", "肠道免疫力评估"), ("beScore", "B/E 比值（定植抗力）"),
    ("antiInflammatoryCapacity", "抗炎能力"), ("dietaryFiberNeed", "膳食纤维需求"),
    ("microbiomeHealthScore", "菌群健康度评分"), ("fbScore", "F/B 比值"),
)
OVERALL_STRUCTURE_DEFINITIONS = (
    ("gmwi2", "肠道微生物组健康指数（GMWI2）"),
    ("microbiomeHealthScore", "肠道菌群健康度评分"),
    ("speciesCount", "菌群种类数"), ("diversity", "菌群多样性"),
    ("beRatio", "B/E 比值（双歧杆菌属/肠杆菌科）"),
    ("fbRatio", "F/B 比值（厚壁菌门/拟杆菌门）"),
    ("enterotype", "肠型"), ("pathogenicPotential", "肠道菌群致病潜力"),
)
# The two supplied metagenome versions have their own literal inventories.
# These select members of existing arrays; they do not add callback fields.
NATIVE_STRUCTURE_DEFINITIONS = (
    ("speciesCount", "肠道菌群种类"), ("diversity", "肠道菌群多样性"),
    ("keystoneBacteria", "核心菌属构成"),
    ("gmwi2", "肠道微生物组健康指数（GMWI2）"),
    ("beRatio", "肠道菌群定植抗力"), ("fbRatio", "厚壁菌门/拟杆菌门（F/B）"),
    ("enterotype", "肠道菌群分型"), ("barrierRelatedBacteria", "肠道屏障功能损伤相关菌"),
)
NATIVE_HEALTH_DEFINITIONS = (
    ("beneficialBacteria", "有益菌"), ("scfaProducerBacteria", "产短链脂肪酸的菌"),
    ("pathogenicBacteria", "致病菌"), ("opportunisticBacteria", "条件致病菌"),
    ("pathogenicFungi", "致病性真菌"), ("pathogenicViruses", "致病性病毒"),
    ("pathogenicParasites", "致病性寄生虫"), ("virulenceGenes", "毒力基因"),
    ("antibioticResistanceGenes", "抗生素耐药基因"),
    ("antiInflammatoryCapacity", "肠道抗炎能力"), ("gutImmunity", "肠道免疫力"),
)


def source_structure_definitions(family=None):
    if family == "GM":
        return (*NATIVE_STRUCTURE_DEFINITIONS[:2], ("gutAge", "肠道年龄评估"),
            *NATIVE_STRUCTURE_DEFINITIONS[2:])
    if family == "G1":
        return (*NATIVE_STRUCTURE_DEFINITIONS,
            ("developmentalMaturity", "肠道菌群发育成熟度相关菌群"),
            ("childGrowth", "影响儿童生长指标相关肠道菌群评估"))
    return OVERALL_STRUCTURE_DEFINITIONS


def source_health_definitions(family=None):
    return NATIVE_HEALTH_DEFINITIONS if family in {"GM", "G1"} else HEALTH_METRIC_DEFINITIONS
# Plain indicator definitions, independent of the example patient's results.
OVERALL_STRUCTURE_EXPLANATIONS = {
    "gmwi2": "基于菌群组成评估健康状态的标准化指数。",
    "microbiomeHealthScore": "来源报告提供的菌群整体健康评分，评分含义依检测机构方法学解释。",
    "speciesCount": "来源报告检出的菌种数量，用于描述菌种丰富度。",
    "diversity": "用于描述菌群种类及其分布多样性的指标，按来源报告的方法和参考范围解读。",
    "beRatio": "双歧杆菌属与肠杆菌科的比值，反映肠道定植抗力。",
    "fbRatio": "厚壁菌门与拟杆菌门的比值，反映能量提取与代谢格局。",
    "enterotype": "按菌群组成特征划分的类型，属于菌群结构描述。",
    "pathogenicPotential": "毒力、侵袭相关功能基因谱的评估，属于菌群功能估计。",
}
HEALTH_METRIC_DEFINITIONS = (
    ("beneficialBacteria", "有益菌"), ("pathogenicBacteria", "致病菌"),
    ("opportunisticBacteria", "条件致病菌"), ("otherHealthBacteria", "其他健康相关菌"),
    ("pathogenicPotential", "肠道菌群致病潜力"),
    ("antiInflammatoryCapacity", "肠道抗炎能力"), ("gutImmunity", "肠道免疫力评估"),
    ("dietaryFiberNeed", "膳食纤维需求"), ("biofilmCapacity", "菌群生物膜合成能力"),
    ("oxidativeStressTolerance", "氧化胁迫耐受"),
)
SCFA_DEFINITIONS = (
    ("formate", "甲酸"), ("acetate", "乙酸"), ("propionate", "丙酸"),
    ("butyrate", "丁酸"), ("valerate", "戊酸"),
)
MAJOR_METABOLISM_DEFINITIONS = (
    ("carbohydrate", "碳水化合物"), ("fat", "脂肪"),
    ("minerals", "矿物质"), ("protein", "蛋白质"),
)
NEUROTRANSMITTER_DEFINITIONS = (
    ("serotonin", "5-羟色胺"), ("dopamine", "多巴胺"),
    ("acetylcholine", "乙酰胆碱"), ("gaba", "γ-氨基丁酸"),
    ("histamine", "组胺"), ("norepinephrine", "去甲肾上腺素"),
    ("endorphins", "内啡肽"),
)
NUTRIENT_GROUP_DEFINITIONS = (
    ("vitamins", "维生素"), ("traceElements", "微量元素"), ("pigments", "天然色素"),
)
QUESTIONNAIRE_SUMMARY_DEFINITIONS = (
    ("chiefComplaint", "主诉"), ("redFlags", "红旗筛查"),
    ("reportedDiseases", "自报疾病"), ("medicalHistory", "疾病史/检查史"),
    ("familyHistory", "家族史"), ("allergies", "过敏史"),
    ("medications", "药物与补充剂"), ("psychologicalScreening", "心理筛查"),
    ("aging", "衰老维度"), ("healthGoals", "健康目标排序"),
)
NEXT_STEP_DEFINITIONS = (
    ("clinicalReview", "临床复查优先", "优先完成本次评估中适用的临床复核与专科随诊。"),
    ("evidenceInterpretation", "循证解读", "由医生/健管师结合《循证解读报告》完成机制解读、禁忌核查与干预组合评估。"),
    ("interventionExecution", "执行干预", "具体方案由《个性化干预执行报告》承载，按个体计划与实际变化复评。"),
)

# The report uses these fixed standards when the supplied value is on the
# corresponding method scale. Native values and native source labels remain
# separate source records; an unknown model scale cannot borrow these cutoffs.
RISK_INDEX_STANDARDS = (
    {"code": "low", "name": "低风险", "meaning": "源模型风险指数分级", "referenceRange": "0–20"},
    {"code": "moderatelyLow", "name": "中低风险", "meaning": "源模型风险指数分级", "referenceRange": "20–40"},
    {"code": "moderate", "name": "中风险", "meaning": "源模型风险指数分级", "referenceRange": "40–60"},
    {"code": "moderatelyHigh", "name": "中高风险", "meaning": "源模型风险指数分级", "referenceRange": "60–80"},
    {"code": "high", "name": "高风险", "meaning": "源模型风险指数分级", "referenceRange": "80–100"},
)
NATIVE_RISK_INDEX_STANDARDS = (
    {"code": "low", "name": "低风险", "meaning": "菌群模型提示低风险。", "referenceRange": ""},
    {"code": "moderate", "name": "中风险", "meaning": "菌群模型提示中等风险。", "referenceRange": ""},
    {"code": "high", "name": "高风险", "meaning": "菌群模型提示高风险。", "referenceRange": ""},
)
NATIVE_DISEASE_RISK_DESCRIPTION = (
    "综合用户已提供的检查、检测、问卷和画像资料，按肠道、代谢、免疫、衰老、精神与睡眠、神经六个维度说明疾病与健康风险及需要关注的问题。"
)
NATIVE_NUTRITION_DESCRIPTION = (
    "评估营养物质相关的菌群功能，结合实际饮食、消化耐受及健康状态确定营养关注重点。"
)
ATTENTION_LEVELS = ("正常", "注意", "中风险", "高风险")
INTEGRATED_RISK_INDEX_STANDARDS = (
    {"code": "normal", "name": "正常", "meaning": "综合关注等级", "referenceRange": "0–24"},
    {"code": "attention", "name": "注意", "meaning": "综合关注等级", "referenceRange": "25–49"},
    {"code": "moderate", "name": "中风险", "meaning": "综合关注等级", "referenceRange": "50–74"},
    {"code": "high", "name": "高风险", "meaning": "综合关注等级", "referenceRange": "75–100"},
)
# Presentation only. Source-model grades and the request-local four-band
# decision receipts keep their original values and interpretation.
PRESENTATION_RISK_LEVELS = ("低风险", "中风险", "高风险")
LEGACY_ATTENTION_PRESENTATION_STANDARDS = (
    {"code": "low", "name": "低风险", "meaning": "综合关注等级", "referenceRange": "0≤指数<50"},
    {"code": "moderate", "name": "中风险", "meaning": "综合关注等级", "referenceRange": "50≤指数<75"},
    {"code": "high", "name": "高风险", "meaning": "综合关注等级", "referenceRange": "75≤指数≤100"},
)
PRESENTATION_RISK_INDEX_STANDARDS = (
    {"code": "low", "name": "低风险", "meaning": "综合健康评分", "referenceRange": "80≤评分≤100"},
    {"code": "moderate", "name": "中风险", "meaning": "综合健康评分", "referenceRange": "60≤评分<80"},
    {"code": "high", "name": "高风险", "meaning": "综合健康评分", "referenceRange": "0≤评分<60"},
)
APPENDIX_A_ROWS = (
    {"code": "gmwi2", "name": "GMWI2", "meaning": "基于菌群组成评估健康状态的标准化指数",
     "referenceRange": "≥1 健康；0.5–1 健康倾向；0–0.5 亚健康；<0 不健康倾向/不健康"},
    {"code": "beRatio", "name": "B/E 比值", "meaning": "双歧杆菌属/肠杆菌科，反映肠道定植抗力",
     "referenceRange": ">1 平衡；0.01–1 失衡；<0.01 重度失衡"},
    {"code": "fbRatio", "name": "F/B 比值", "meaning": "厚壁菌门/拟杆菌门，反映能量提取与代谢格局",
     "referenceRange": "<1 偏向拟杆菌门；>1 偏向厚壁菌门"},
    {"code": "diseaseRiskIndex", "name": "菌群疾病风险指数", "meaning": "菌群风险模型输出（0–100）",
     "referenceRange": "0–20 低；20–40 中低；40–60 中；60–80 中高；80–100 高"},
    {"code": "pathogenicPotential", "name": "菌群致病潜力", "meaning": "毒力/侵袭相关功能基因谱评估",
     "referenceRange": "2.5–11.662 正常；区间外为失衡"},
    {"code": "nutritionMetabolismScore", "name": "营养/代谢能力评分", "meaning": "菌群功能基因推断的合成/代谢能力",
     "referenceRange": "≤10 弱；10–30 较弱；>30 正常（检测方法学标准）"},
    {"code": "shortChainFattyAcidSynthesis", "name": "短链脂肪酸合成", "meaning": "甲酸/乙酸/丙酸/丁酸/戊酸合成能力",
     "referenceRange": ">60 合成能力强"},
)


def source_indicator_standards(family=None):
    """Base appendix definitions; publication aligns actual assay ranges.

    These definitions never overwrite measured values or original grades.
    """
    return APPENDIX_A_ROWS


def current_indicator_standards(rows, structure=(), *, integrated=False, health_scale=True):
    """Keep appendix definitions consistent with the adopted assay and index.

    The example's cutoffs must not override a measured row's own reference
    range. Unknown or conflicting ranges remain empty rather than inferred.
    """
    result = [dict(row) for row in rows]
    for row in result:
        range_key = 'reference_range' if 'reference_range' in row else 'referenceRange'
        if row.get('code') in {'gmwi2', 'beRatio', 'fbRatio', 'pathogenicPotential'}:
            actual = [item for item in structure if item.get('code') == row['code']
                and item.get('value') is not None]
            if actual:
                ranges = {str(item.get('referenceRange') or item.get('reference_range') or '').strip()
                    for item in actual}
                row[range_key] = next(iter(ranges)) if len(ranges) == 1 else ''
            else:
                row[range_key] = ''
        if row.get('code') == 'nutritionMetabolismScore':
            row[range_key] = '≤10 弱；10<评分≤30 较弱；>30 正常'
        if integrated and row.get('code') == 'diseaseRiskIndex':
            row.update(name='疾病与健康风险综合评分' if health_scale else '疾病与健康风险综合关注指数',
                meaning='结合检查、问卷和检测信息评估综合健康状态' if health_scale
                    else '结合检查、问卷和检测信息评估需要关注的程度')
            row[range_key] = '；'.join(item['referenceRange'] + ' ' + item['name']
                for item in (PRESENTATION_RISK_INDEX_STANDARDS if health_scale
                    else LEGACY_ATTENTION_PRESENTATION_STANDARDS))
    return result


def _fixed_standard_number(value):
    """Accept an exact finite scalar without coercing comparisons or booleans."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    text = str(value).strip()
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def presentation_risk_level(score):
    """Three-band evaluate display of an existing 0–100 attention index."""
    number = _fixed_standard_number(score)
    if number is None or number < 0 or number > 100:
        return None
    return "低风险" if number < 50 else "中风险" if number < 75 else "高风险"


def presentation_health_level(score):
    """Evaluate-only health score: higher is better, never a probability."""
    number = _fixed_standard_number(score)
    if number is None or number < 0 or number > 100:
        return None
    return "低风险" if number >= 80 else "中风险" if number >= 60 else "高风险"


def presentation_risk_label(level):
    """Map only independently assessed legacy labels, never source grades."""
    return {"正常": "低风险", "注意": "低风险", "低风险": "低风险",
        "中风险": "中风险", "高风险": "高风险"}.get(level) if isinstance(level, str) else None


def template_risk_grade(value, *, full_scale=None):
    """Grade an explicitly declared 0–100 model index, never a probability.

    The printed adjacent ranges share endpoints. For one deterministic result
    the lower band owns each shared endpoint: [0,20], (20,40], …, (80,100].
    Unknown scales, including an undeclared value that happens to be <=1,
    return None so the caller can preserve the native model result.
    """
    if _fixed_standard_number(full_scale) != Decimal(100):
        return None
    number = _fixed_standard_number(value)
    if number is None or number < 0 or number > 100:
        return None
    for boundary, standard in zip((20, 40, 60, 80, 100), RISK_INDEX_STANDARDS):
        if number <= boundary:
            return standard["name"]
    return None


def template_nutrition_grade(value):
    """Apply the fixed report method to an existing 0–100 function score."""
    number = _fixed_standard_number(value)
    if number is None or number < 0 or number > 100:
        return None
    if number <= 10:
        return "弱"
    if number <= 30:
        return "较弱"
    return "正常"


def template_indicator_grade(code, value, *, full_scale=None):
    """Apply only the intervals explicitly defined by template appendix A.

    F/B=1 and SCFA<=60 have no printed label in the template. They remain
    undefined here rather than inventing a new cutoff or overwriting a native
    result. Callers retain the original source value, range and sourceStatus.
    """
    if code == "diseaseRiskIndex":
        return template_risk_grade(value, full_scale=full_scale)
    if code == "nutritionMetabolismScore":
        return template_nutrition_grade(value)
    number = _fixed_standard_number(value)
    if number is None:
        return None
    if code == "gmwi2":
        if number >= 1:
            return "健康"
        if number >= Decimal("0.5"):
            return "健康倾向"
        return "亚健康" if number >= 0 else "不健康倾向/不健康"
    if number < 0:
        return None
    if code == "beRatio":
        if number > 1:
            return "平衡"
        return "失衡" if number >= Decimal("0.01") else "重度失衡"
    if code == "fbRatio":
        if number < 1:
            return "偏向拟杆菌门"
        return "偏向厚壁菌门" if number > 1 else None
    if code == "pathogenicPotential":
        return "正常" if Decimal("2.5") <= number <= Decimal("11.662") else "失衡"
    if code == "shortChainFattyAcidSynthesis":
        return "合成能力强" if number > 60 else None
    return None


# Finite source identities shared by evaluate projection, summaries and preview.
AMINO_ACID_ALIASES = {
    "lysine": ("赖氨酸", "lysine"), "serine": ("丝氨酸", "serine"),
    "leucine": ("亮氨酸", "leucine"), "tryptophan": ("色氨酸", "tryptophan"),
    "phenylalanine": ("苯丙氨酸", "phenylalanine"), "valine": ("缬氨酸", "valine"),
    "histidine": ("组氨酸", "histidine"), "cysteine": ("半胱氨酸", "cysteine"),
    "tyrosine": ("酪氨酸", "tyrosine"), "alanine": ("丙氨酸", "alanine"),
    "proline": ("脯氨酸", "proline"), "threonine": ("苏氨酸", "threonine"),
    "glutamate": ("谷氨酸", "glutamate", "glutamic acid"), "isoleucine": ("异亮氨酸", "isoleucine"),
    "arginine": ("精氨酸", "arginine"), "methionine": ("蛋氨酸", "甲硫氨酸", "methionine"),
    "glycine": ("甘氨酸", "glycine"), "aspartate": ("天冬氨酸", "aspartate", "aspartic acid"),
    "taurine": ("牛磺酸", "taurine"), "asparagine": ("天冬酰胺", "asparagine"),
    "glutamine": ("谷氨酰胺", "glutamine"),
}
AMINO_ACID_DEFINITIONS = tuple((code, aliases[0]) for code, aliases in AMINO_ACID_ALIASES.items())
NUTRIENT_ITEM_DEFINITIONS = {
    "vitamins": (("vitaminC", "维生素C"), ("vitaminE", "维生素E"), ("vitaminK2", "维生素K2"),
        ("vitaminB6", "维生素B6"), ("vitaminA", "维生素A"), ("vitaminB1", "维生素B1"),
        ("vitaminB2", "维生素B2"), ("vitaminB3", "维生素B3"), ("vitaminB5", "维生素B5"),
        ("vitaminB7", "维生素B7"), ("vitaminB12", "维生素B12"), ("folate", "叶酸")),
    "traceElements": (("iron", "铁"), ("zinc", "锌"), ("magnesium", "镁"), ("calcium", "钙"), ("selenium", "硒")),
    "pigments": (("carotenoids", "类胡萝卜素"), ("lycopene", "番茄红素"), ("lutein", "叶黄素"),
        ("isoflavones", "异黄酮"), ("anthocyanins", "花青素"), ("flavonoids", "类黄酮"), ("chlorophyll", "叶绿素")),
}
DETAILED_METABOLISM_DEFINITIONS = (
    ("carbohydrateMetabolism", "糖类代谢"), ("lipidMetabolism", "脂类代谢"),
    ("nucleotideMetabolism", "嘌呤代谢"),
)
DETAILED_METABOLISM_ITEM_DEFINITIONS = {
    "carbohydrateMetabolism": (("glycolysis", "糖酵解"), ("pentosePhosphate", "磷酸戊糖"),
        ("fructoseMannose", "果糖和甘露糖"), ("galactose", "半乳糖"),
        ("starchSucrose", "淀粉和蔗糖"), ("aminoSugarNucleotideSugar", "氨基糖和核苷酸糖"), ("pyruvate", "丙酮酸")),
    "lipidMetabolism": (("glycerides", "甘油酯"), ("arachidonicAcid", "花生四烯酸"),
        ("linolenicAcid", "亚麻酸"), ("glycerophospholipids", "甘油磷脂"), ("sphingolipids", "鞘脂类"), ("fattyAcids", "脂肪酸")),
    "nucleotideMetabolism": (("purines", "嘌呤代谢"),),
}
OTHER_METABOLITE_DEFINITIONS = (("bileAcids", "胆汁酸"), ("glutathione", "谷胱甘肽（三肽）"),
    ("lipoicAcid", "硫辛酸"), ("coenzymeQ", "辅酶Q"))
NATIVE_NUTRITION_MODULES = (
    ("nativeNutrition:yingyang2_tanshui2", "碳水化合物（糖类）分解代谢"),
    ("nativeNutrition:yingyang2_zhizhi2", "脂质合成和分解代谢"),
    ("nativeNutrition:yingyang2_dlzfs2", "短链脂肪酸合成代谢"),
    ("nativeNutrition:yingyang2_sjdz2", "神经递质合成代谢"),
    ("nativeNutrition:yingyang2_ajs2", "氨基酸合成代谢"),
    ("nativeNutrition:yingyang2_wss2", "维生素合成代谢"),
    ("nativeNutrition:yingyang2_hgs2", "核苷酸合成代谢"),
    ("nativeNutrition:yingyang2_trss2", "天然色素合成代谢"),
    ("nativeNutrition:yingyang2_yhdxw2", "有害代谢物合成代谢"),
    ("nativeNutrition:yingyang2_dxwz2", "毒性物质分解代谢"),
)
NATIVE_DETAILED_DEFINITIONS = (
    ("carbohydrateMetabolism", "碳水化合物（糖类）分解代谢"),
    ("lipidMetabolism", "脂质合成和分解代谢"),
    ("nucleotideMetabolism", "核苷酸合成代谢"),
    ("harmfulMetabolites", "有害代谢物合成代谢"),
    ("toxicSubstanceDegradation", "毒性物质分解代谢"),
)
NATIVE_DETAILED_ITEM_DEFINITIONS = {
    "carbohydrateMetabolism": (("galactose", "半乳糖"), ("starchSucrose", "淀粉和蔗糖")),
    "lipidMetabolism": (("linolenicAcid", "α-亚麻酸"), ("arachidonicAcid", "花生四烯酸"),
        ("unsaturatedFattyAcids", "不饱和脂肪酸"), ("etherLipids", "醚酯"),
        ("saturatedFattyAcids", "饱和脂肪酸"), ("glycerides", "甘油酯"),
        ("phosphatidylglycerol", "磷脂酰甘油"), ("linoleicAcid", "亚油酸"),
        ("secondaryBileAcids", "次级胆汁酸"), ("sphingolipids", "鞘脂"),
        ("steroids", "类固醇"), ("steroidHormones", "类固醇激素")),
    "nucleotideMetabolism": (("purines", "嘌呤"), ("pyrimidines", "嘧啶")),
    "harmfulMetabolites": (("homocysteine", "同型半胱氨酸"), ("lipopolysaccharides", "脂多糖")),
    "toxicSubstanceDegradation": tuple(("toxin:" + name, name) for name in (
        "阿特拉津", "苯甲酸酯", "己内酰胺", "二噁英", "乙苯", "对氟苯甲酸乙酯", "萘", "硝基甲苯", "多环芳烃", "甲苯", "二甲苯")),
}
NATIVE_AMINO_ACID_CODES = ("arginine", "cysteine", "glutamate", "glycine", "histidine",
    "isoleucine", "leucine", "lysine", "methionine", "phenylalanine", "proline", "threonine",
    "tryptophan", "tyrosine", "valine")
NATIVE_VITAMIN_CODES = ("vitaminA", "vitaminB1", "vitaminB2", "vitaminB3", "vitaminB5",
    "vitaminB6", "vitaminB7", "folate", "vitaminK")


def source_nutrition_definitions(family=None):
    """Actual source module inventories, with the same public array types."""
    if family not in {"GM", "G1"}:
        return dict(major=MAJOR_METABOLISM_DEFINITIONS, neuro=NEUROTRANSMITTER_DEFINITIONS,
            amino=(*AMINO_ACID_DEFINITIONS, *OTHER_METABOLITE_DEFINITIONS),
            groups=NUTRIENT_GROUP_DEFINITIONS, group_items=NUTRIENT_ITEM_DEFINITIONS,
            detailed=DETAILED_METABOLISM_DEFINITIONS, detail_items=DETAILED_METABOLISM_ITEM_DEFINITIONS,
            scfa=SCFA_DEFINITIONS)
    vitamins = {**dict(NUTRIENT_ITEM_DEFINITIONS["vitamins"]), 'vitaminK': '维生素K'}
    return dict(major=NATIVE_NUTRITION_MODULES,
        neuro=(("dopamine", "多巴胺"), ("gaba", "γ-氨基丁酸"), ("serotonin", "血清素")),
        amino=tuple((code, dict(AMINO_ACID_DEFINITIONS)[code]) for code in NATIVE_AMINO_ACID_CODES),
        groups=(("vitamins", "维生素"), ("pigments", "天然色素")),
        group_items={"vitamins": tuple((code, vitamins[code]) for code in NATIVE_VITAMIN_CODES),
            "pigments": (("carotenoids", "类胡萝卜素"), ("flavonoids", "类黄酮"))},
        detailed=NATIVE_DETAILED_DEFINITIONS, detail_items=NATIVE_DETAILED_ITEM_DEFINITIONS,
        scfa=(("butyrate", "丁酸"), ("propionate", "丙酸"), ("acetate", "乙酸")))
FIXED_METRIC_ALIASES = {
    **AMINO_ACID_ALIASES,
    "serotonin": ("5-羟色胺", "5羟色胺", "血清素", "serotonin", "5-HT"),
    "dopamine": ("多巴胺", "dopamine"), "acetylcholine": ("乙酰胆碱", "acetylcholine", "ACh"),
    "gaba": ("γ-氨基丁酸", "γ氨基丁酸", "gamma aminobutyric acid", "GABA"),
    "histamine": ("组胺", "histamine"), "norepinephrine": ("去甲肾上腺素", "norepinephrine", "noradrenaline"),
    "endorphins": ("内啡肽", "endorphins", "endorphin"),
    "carotenoids": ("类胡萝卜素", "β胡萝卜素", "carotenoids", "carotenoid"),
    "flavonoids": ("类黄酮", "flavonoids", "flavonoid"), "anthocyanins": ("花青素", "anthocyanins", "anthocyanin"),
    "isoflavones": ("异黄酮", "异黄酮素", "isoflavones", "isoflavone"),
    "folate": ("叶酸", "folate", "folic acid"), "bileAcids": ("胆汁酸", "bile acid", "bile acids"),
    "glutathione": ("谷胱甘肽", "谷胱甘肽（三肽）", "glutathione"),
    "lipoicAcid": ("硫辛酸", "lipoic acid"), "coenzymeQ": ("辅酶Q", "辅酶Q10", "coenzyme Q", "coenzyme Q10", "ubiquinone"),
    "glycerides": ("甘油酯", "甘油三酯", "glycerides", "glyceride", "glycerolipid", "glycerolipids"),
    "linolenicAcid": ("亚麻酸", "α-亚麻酸", "α亚麻酸", "alpha linolenic acid", "linolenic acid"),
    "glycerophospholipids": ("甘油磷脂", "glycerophospholipid", "glycerophospholipids"),
    "sphingolipids": ("鞘脂", "鞘脂类", "sphingolipid", "sphingolipids"),
    "fattyAcids": ("脂肪酸", "fatty acid", "fatty acids"), "purines": ("嘌呤", "嘌呤代谢", "purine", "purines"),
}


def fixed_metric_code(value, definitions):
    """Finite identity matching, with only literal synthesis/score suffixes."""
    key = _template_identity_key(value)
    identities = { _template_identity_key(term): code for code, name in definitions
        for term in (code, name, *FIXED_METRIC_ALIASES.get(code, ())) }
    if key in identities:
        return identities[key]
    for suffix in ("synthesisability", "synthesisscore", "metabolismscore", "合成能力", "合成评分", "代谢能力", "代谢评分", "synthesis", "合成", "评分", "score"):
        if key.endswith(suffix):
            key = key[:-len(suffix)]
            break
    return identities.get(key)
