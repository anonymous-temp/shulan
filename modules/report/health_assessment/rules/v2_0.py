"""Fixed six-system health-management rules (version 2.1.0).
The six active systems are scored deterministically. The disease catalogue is deliberately extensible:
known product terms have an audited default risk grade, while unseen terms are
classified by organ-system keywords and remain ``pending`` unless the source
report supplies a risk grade.
"""

from __future__ import annotations

RULE_VERSION = "2.1.0"
DIMENSION_WEIGHT = 1 / 6

SYSTEM_DIMENSIONS = (
    ("immune_system", "免疫系统"),
    ("urinary_system", "泌尿系统"),
    ("nervous_system", "神经系统"),
    ("cardiovascular_system", "心脑血管系统"),
    ("digestive_system", "消化系统"),
    ("metabolic_system", "代谢系统"),
)

# Public order is fixed regardless of relevance or missing facts. Keep the
# inactive mental/tumour classification below so removing their dimensions
# cannot accidentally reclassify those diseases into another scored system.
DEFAULT_FILL_ORDER = (
    "cardiovascular_system",
    "metabolic_system",
    "digestive_system",
    "immune_system",
    "urinary_system",
    "nervous_system",
)

RISK_LABELS = {
    "high": "高风险",
    "medium": "中风险",
    "low": "低风险",
    "pending": "风险待评估",
}

# A single disease signal places its system in the same score band as the
# product colour: high -> priority attention, medium -> attention, low -> good.
# Pending/black is eligible for system selection but never fabricates a score.
RISK_SCORES = {"high": 45, "medium": 65, "low": 80}
RISK_RELEVANCE = {"high": 90, "medium": 65, "low": 40, "pending": 20}

DISEASE_RISK_BOUNDARY = (
    "高风险45分、中风险65分、低风险80分；风险待评估不直接计分。"
    "同一系统存在多项可评分风险时，以最高风险为基础，每增加一项"
    "高/中/低风险分别再扣8/5/2分，最低20分。"
)

# (canonical name, primary system, default risk, aliases, secondary systems)
DISEASE_RULES = (
    # 精神系统
    ("焦虑症", "mental_system", "medium", ("焦虑症", "焦虑障碍"), ()),
    ("抑郁症", "mental_system", "medium", ("抑郁症", "抑郁障碍"), ()),
    ("双相情感障碍", "mental_system", "low", ("双相情感障碍", "双相障碍"), ()),
    ("精神分裂症", "mental_system", "low", ("精神分裂症",), ()),
    # 免疫系统
    ("类风湿关节炎", "immune_system", "low", ("类风湿关节炎", "类风湿性关节炎"), ()),
    ("系统性红斑狼疮", "immune_system", "medium", ("系统性红斑狼疮", "红斑狼疮"), ()),
    ("强直性脊柱炎", "immune_system", "pending", ("强直性脊柱炎",), ()),
    ("自身免疫性肝炎", "immune_system", "pending", ("自身免疫性肝炎",), ("digestive_system",)),
    # 肿瘤
    ("结直肠癌", "tumor_risk", "medium", ("结直肠癌", "结肠癌", "直肠癌"), ("digestive_system",)),
    ("胃癌", "tumor_risk", "pending", ("胃癌",), ("digestive_system",)),
    ("甲状腺癌", "tumor_risk", "medium", ("甲状腺癌",), ("metabolic_system",)),
    ("肝癌", "tumor_risk", "pending", ("肝癌",), ("digestive_system",)),
    # 泌尿系统
    ("肾结石", "urinary_system", "medium", ("肾结石", "泌尿系结石", "尿路结石"), ("metabolic_system",)),
    ("慢性肾脏病", "urinary_system", "pending", ("慢性肾脏病", "慢性肾功能不全", "CKD"), ()),
    ("尿路感染", "urinary_system", "pending", ("尿路感染", "泌尿系感染"), ("immune_system",)),
    # 神经系统
    ("阿尔茨海默病", "nervous_system", "pending", ("阿尔茨海默病", "阿尔兹海默病", "老年痴呆"), ()),
    ("帕金森病", "nervous_system", "medium", ("帕金森病", "帕金森"), ()),
    ("癫痫", "nervous_system", "low", ("癫痫",), ()),
    ("多发性硬化症", "nervous_system", "low", ("多发性硬化症", "多发性硬化"), ("immune_system",)),
    # 心脑血管系统
    ("高血压", "cardiovascular_system", "medium", ("高血压",), ()),
    ("冠心病", "cardiovascular_system", "medium", ("冠心病", "冠状动脉粥样硬化性心脏病"), ()),
    ("脑卒中", "cardiovascular_system", "pending", ("脑卒中", "脑梗死", "脑出血"), ("nervous_system",)),
    ("心力衰竭", "cardiovascular_system", "pending", ("心力衰竭", "心衰"), ()),
    ("心肌梗死", "cardiovascular_system", "pending", ("心肌梗死", "心梗"), ()),
    # 消化系统
    ("便秘", "digestive_system", "medium", ("便秘",), ()),
    ("腹泻型肠易激综合征", "digestive_system", "low", ("腹泻型肠易激综合征", "腹泻型肠易激综合症", "IBS-D"), ()),
    ("炎症性肠病", "digestive_system", "high", ("炎症性肠病", "炎症性肠道疾病", "IBD"), ("immune_system",)),
    ("溃疡性结肠炎", "digestive_system", "high", ("溃疡性结肠炎",), ("immune_system",)),
    ("克罗恩病", "digestive_system", "medium", ("克罗恩病", "克隆病"), ("immune_system",)),
    ("非酒精性脂肪肝", "digestive_system", "pending", ("非酒精性脂肪肝", "非酒精性脂肪性肝病", "脂肪肝", "NAFLD", "MASLD"), ("metabolic_system",)),
    ("肝硬化", "digestive_system", "pending", ("肝硬化",), ()),
    ("胃炎", "digestive_system", "pending", ("胃炎", "慢性非萎缩性胃炎", "慢性萎缩性胃炎"), ()),
    ("胃肠炎", "digestive_system", "pending", ("胃肠炎",), ("immune_system",)),
    ("艰难梭菌感染", "digestive_system", "pending", ("艰难梭菌感染",), ("immune_system",)),
    ("食物中毒", "digestive_system", "pending", ("食物中毒",), ("immune_system",)),
    ("腹泻", "digestive_system", "pending", ("感染性腹泻", "血性腹泻", "腹泻"), ()),
    # 代谢系统
    ("肥胖", "metabolic_system", "medium", ("肥胖", "肥胖症"), ("cardiovascular_system",)),
    ("2型糖尿病", "metabolic_system", "medium", ("2型糖尿病", "II型糖尿病", "Ⅱ型糖尿病", "糖尿病"), ("cardiovascular_system",)),
    ("高脂血症", "metabolic_system", "medium", ("高脂血症", "血脂异常", "高胆固醇血症"), ("cardiovascular_system",)),
    ("高尿酸血症", "metabolic_system", "pending", ("高尿酸血症", "高尿酸"), ("urinary_system",)),
    ("痛风", "metabolic_system", "pending", ("痛风",), ("urinary_system",)),
)

SYSTEM_KEYWORDS = {
    "mental_system": ("焦虑", "抑郁", "精神", "双相", "情感障碍"),
    "immune_system": ("自身免疫", "免疫", "类风湿", "红斑狼疮", "强直性脊柱炎"),
    "tumor_risk": ("癌", "恶性肿瘤", "恶性占位"),
    "urinary_system": ("肾", "尿路", "泌尿", "膀胱", "输尿管", "前列腺"),
    "nervous_system": ("神经", "癫痫", "帕金森", "阿尔茨海默", "痴呆", "多发性硬化"),
    "cardiovascular_system": ("高血压", "冠心", "心肌", "心血管", "脑卒中", "脑梗", "脑出血", "动脉粥样"),
    "digestive_system": ("胃", "肠", "肝", "胆", "胰", "便秘", "腹泻", "消化", "克罗恩"),
    "metabolic_system": ("糖尿病", "肥胖", "血脂", "胆固醇", "高尿酸", "代谢"),
}

SYSTEM_MEANINGS = {
    "mental_system": "情绪、认知及精神健康相关风险",
    "immune_system": "免疫调节和自身免疫相关风险",
    "tumor_risk": "肿瘤相关健康风险",
    "urinary_system": "肾脏及泌尿系统相关健康风险",
    "nervous_system": "神经系统功能及相关疾病风险",
    "cardiovascular_system": "心脑血管危险因素及相关疾病风险",
    "digestive_system": "消化道、肝胆及肠道微生态相关健康风险",
    "metabolic_system": "体重、糖脂及尿酸代谢相关健康风险",
}
