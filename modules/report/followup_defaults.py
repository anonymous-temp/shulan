"""Schema-complete neutral fallbacks: missing data never means normal.

Keep declared fields and nested objects; never manufacture a diagnosis, risk
level, evidence row, product or individual dose when model output is unavailable.
"""
from __future__ import annotations

from typing import Any

from modules.report.explain_defaults import default_explain_manual
from modules.report.gut_barrier_defaults import default_gut_barrier_manual

# ---------- 依赖项 ----------

INFO: dict[str, Any] = {
    "intestine_type": None,
    "best_bacteria": None,
    "unbalance_type": None,
    "body_mass": None,
    "risk_warning": None,
    "constitution": None,  # 个体体质特征（gutBarrier 程序按模板拼装；evaluate 为空串）
    "evidence_refs": [],
}

RECOMMEND: dict[str, Any] = {
    "mdf_code": None,
    "MDF": [],
    "pfmt_code": None,
    "PFMT": None,
    "recommended_combination_method": None,
    "mdf_product_rationale": None,
    "pfmt_product_rationale": None,
    "combination_rationale": None,
    "mdf_action_focus": None,
    "mdf_observation_focus": None,
    "pfmt_action_focus": None,
    "pfmt_observation_focus": None,
}

# ---------- 干预方案 (intervene) ----------

DIET: dict[str, Any] = {
    **dict.fromkeys(("diet", "meals", "fiber_advice", "fat_advice", "carb_advice",
                     "plant_diversity", "bloating_management", "diet_monitoring")),
    "target": dict.fromkeys(("energy_kcal", "protein_g", "fat_g", "carbs_g", "fiber_g", "sodium_mg", "iodine_ug")),
    "food_execution": dict.fromkeys(("priority_choices", "moderate_intake", "reduce_intake", "bloating_adjustment",
                                     "breakfast", "lunch", "dinner", "snack", "hydration", "record")),
}

EXERCISE: dict[str, Any] = {
    "behavior": {
        **dict.fromkeys(("sleep", "nap", "bowel_record", "bloating_record", "diet_record",
                         "substance_management", "medication_management")),
        "next_follow_up": [],
    },
    "exercise": {
        **{key: dict.fromkeys(("frequency", "intensity", "duration", "type"))
           for key in ("aerobic_exercise", "resistance_training", "post_meal_activity")},
        "reminder": None, "weekly_execution_plan": [],
    },
}
SUPPLEMENTS = {"general_principles": None, "supplement_list": []}
USAGE = {"pfmt_usage": [], "mdf_usage": []}
_UNAVAILABLE = "本项评估结果暂缺，需结合有效检测资料完成评估。"
OPINION_SUMMARY = {
    "conclusion": _UNAVAILABLE, "clinical_context": {"临床定位": None}, "risk": None, "evidence_refs": [],
}
OPINION_BASIS = {"abnormal_findings": [], "evidence_refs": []}
OPINION_ADVICE = {
    "report_interpretation": ["个体化建议暂缺，请结合已完成的检测结果及临床资料评估。"],
    "Plan": dict.fromkeys(("未来1-2周", "未来1个月", "未来2-3个月", "未来6-12个月")),
    "not_recommended_behaviors": dict.fromkeys(("不建议依据微量DNA抗感染", "不建议以单一指标作为唯一干预目标",
                                                 "不建议仅凭功能预测盲目补充维生素")),
    "medical_alerts": [],
}

EXCEPTION_HANDLING: list[str] = [
    "有效存储：签收后即刻置于 -18℃ 及以下冷冻环境，有效存储期 7 天；",
    "禁止行为：严禁常温久放、反复冻融、高温暴晒、潮湿环境存放；",
    "短途取用：单次取出当次所需数量，剩余胶囊立即放回冷冻层，常温放置不可超过 10 分钟；",
    "异常品处置：有效存储期内如出现外壳破损、内容物渗漏、异味、变色，或铝塑包装漏气、破损，禁止服用，第一时间联系专属医师或工作人员更换处理。",
]

# ---------- 诊断意见 (opinion) ----------

# ---------- 报告解读 (explain) ----------

# [已停用] 报告解读改为《解读参考手册》结构，三段式正文（summary/basis/advice）不再产出。
# 新的固定文案与兜底内容在 modules/report/explain_defaults.py；恢复旧结构时取消下面的注释。
# EXPLAIN_SUMMARY: dict[str, Any] = {
#     "title": "本次检查未提示需要立即住院处理的主要问题，以日常健康管理为主。",
#     "conclusion": (
#         "本次受检项目未提示需要处理的异常，整体处于相对稳定的状态。"
#         "建议保持均衡膳食、规律作息与适度运动，并按计划完成复测以观察指标趋势。"
#     ),
#     "risk": "低风险",
#     "evidence_refs": [],
# }
#
# EXPLAIN_BASIS: dict[str, Any] = {
#     "abnormal_findings": [
#         {
#             "problem": "日常微生态健康维持",
#             "priority": "长期主线",
#             "indicators": [],
#             "biological_mechanism": (
#                 "当前检测数据未提示明显的高危微生态失衡信号。肠道微生态目前处于相对平稳的代偿状态，"
#                 "在此阶段，坚持良好的生活方式和规律的微生态养护，是预防肠道老化、维持机体免疫力和"
#                 "正常代谢的核心基础。"
#             ),
#             "recommendations": [
#                 "微生态养护：现阶段建议以日常膳食补充基础型益生元（如菊粉、低聚果糖）或适量优质发酵食品为主，持续滋养肠道原生有益菌，巩固现有的微生态屏障。",
#                 "医学营养干预：建议采取类似地中海饮食的抗炎模式，确保每日摄入充足的全谷物和深色蔬菜，为肠道有益微生物提供充足的优质代谢底物。",
#             ],
#             "tips": [
#                 "保持规律作息、合理舒缓压力并适度运动，避免长期精神紧张或滥用抗生素，这是保护现有微生态屏障免受破坏的关键。",
#                 "建议将肠道微生态检测纳入您的年度常规健康管理，动态监测菌群的长期变化趋势。",
#             ],
#             "evidence_refs": [],
#             "evidence_level": "中（多中心临床观察/高水平统计相关性）",
#         }
#     ],
#     "evidence_refs": [],
# }
#
# EXPLAIN_ADVICE: dict[str, Any] = {
#     "report_interpretation": list(OPINION_ADVICE["report_interpretation"]),
#     "Plan": dict(OPINION_ADVICE["Plan"]),
#     "daily_execution": {
#         "吃饭": "以地中海饮食为基础，每餐约 1/2 蔬菜 + 1/4 蛋白 + 1/4 主食，减少油炸与含糖饮料。",
#         "运动": "每周中等强度有氧不少于 150 分钟，另加 2-3 次抗阻训练，餐后轻快步行 10-20 分钟。",
#         "睡眠": "固定作息，保证每日 7-8 小时睡眠，午休控制在 20-30 分钟。",
#         "烟酒和压力": "限酒、减少吸烟，通过规律运动与呼吸放松缓解压力，避免长期精神紧张。",
#     },
#     "medical_alerts": list(OPINION_ADVICE["medical_alerts"]),
# }


# ---------- 临床医嘱 - 三位一体非药物干预方案 (non_drug_intervention) ----------
# 组织顺序固定：生活方式干预 → 肠道菌群调节 → 营养素推荐。

# 1) 配套生活方式干预：沿用原「医嘱说明」的 6 个条目与顺序。
#    每个条目 {key, title, summary, points}；key 顺序即展示顺序。
LIFESTYLE_ITEM_TITLES: dict[str, str] = {
    "diet": "膳食结构调整",
    "routine_exercise": "作息与运动管理",
    "follow_up": "复诊与复评",
    "hydration": "饮水与饮食管控",
    "medication": "用药禁忌",
    "self_monitoring": "自我监测",
    "substance": "烟酒管控",
}

# 通用版内容：模型缺字段或完全不可用时使用，也作为「未使用相关产品」时的兜底。
LIFESTYLE_DEFAULTS: dict[str, dict[str, Any]] = {
    "diet": {
        "summary": "以均衡膳食为基础，多蔬果全谷，少油少糖少盐。",
        "points": ["每餐约1/2蔬菜+1/4蛋白+1/4主食", "膳食纤维逐步增至25-30g/日", "少吃油炸、加工肉和含糖饮料"],
    },
    "routine_exercise": {
        "summary": "每日睡足7～8小时，每周≥150分钟中等强度运动。",
        "points": ["快走、骑行等有氧为主，每次30分钟", "每周2-3次力量训练", "餐后轻快步行10-20分钟"],
    },
    "follow_up": {
        "summary": "建议首次干预后3个月内复测，复测结果更新后须重新评估并定制适配方案。",
        "points": ["记录三餐、腹胀与排便变化", "每两周复盘一次执行情况"],
    },
    "hydration": {
        "summary": "每日分次足量饮水，少喝含糖饮料与冰镇饮品。",
        "points": [],
    },
    # 用药禁忌：无 PFMT 时用模型内容（缺失时用这条通用文案）；有 PFMT 时由 LIFESTYLE_PRODUCT_CLAUSES 覆盖
    "medication": {
        "summary": "避免自行使用抗生素、泻药等影响胃肠道的药物，用药遵医嘱。",
        "points": [],
    },
    # 自我监测：固定文案，模型不生成
    "self_monitoring": {
        "summary": "每周测体重腰围、每月测血压，记录排便与饮食运动。",
        "points": [],
    },
    "substance": {
        "summary": "戒烟限酒，减少烟酒对肠道黏膜的刺激。",
        "points": [],
    },
}

# 与产品相关的确定性条款：命中时覆盖模型内容，模型不得改写。
LIFESTYLE_PRODUCT_CLAUSES: dict[str, str] = {
    # MDF 或 PFMT 任一在方案中
    "hydration": "调理周期内请勿过量饮酒，避免大量饮用冰镇饮品。",
    # 仅 PFMT
    "medication": (
        "移植疗程内，非医嘱禁止自行服用抗生素及强效抑菌类药物，以免破坏移植菌群定植；"
        "如确需使用抗生素，须与本品间隔 ≥ 4 小时，或遵医嘱调整服用时序。"
    ),
    "substance": "移植及定植周期内严格禁酒，减少吸烟，降低肠道炎症对菌群定植的干扰。",
}

# 生活方式干预的开头话术（固定文案，不随就医分级变化；就医要求统一由就医提示表达）。
LIFESTYLE_INTRO = "基于循证医学，建议您优先采用以下生活方式干预进行改善。"

# 复诊与复评的 summary 始终固定，模型只补充记录类要点。
LIFESTYLE_FIXED_FOLLOW_UP = LIFESTYLE_DEFAULTS["follow_up"]["summary"]
# 完全固定、不采用模型内容的条目
LIFESTYLE_FIXED_ITEMS: tuple[str, ...] = ("self_monitoring",)


def lifestyle_intervention_base(
    *,
    has_mdf: bool = False,
    has_pfmt: bool = False,
    personalized: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """组装生活方式干预：{intro, items}，items 为固定顺序的 7 个条目（见 LIFESTYLE_ITEM_TITLES）。

    ``personalized`` 是模型输出（已规整为 {key: {summary, points}}），缺失的条目
    回退到 ``LIFESTYLE_DEFAULTS``；与产品相关的条款最后覆盖，保证禁忌口径唯一。
    """
    personalized = personalized or {}
    items: list[dict[str, Any]] = []
    for key, title in LIFESTYLE_ITEM_TITLES.items():
        source = personalized.get(key) or LIFESTYLE_DEFAULTS[key]
        summary = source.get("summary") or LIFESTYLE_DEFAULTS[key]["summary"]
        points = list(source.get("points") or [])

        if key in LIFESTYLE_FIXED_ITEMS:
            summary = LIFESTYLE_DEFAULTS[key]["summary"]
            points = list(LIFESTYLE_DEFAULTS[key]["points"])
        elif key == "follow_up":
            summary = LIFESTYLE_FIXED_FOLLOW_UP
        elif key == "hydration" and (has_mdf or has_pfmt):
            summary = LIFESTYLE_PRODUCT_CLAUSES["hydration"]
        elif key in ("medication", "substance") and has_pfmt:
            summary = LIFESTYLE_PRODUCT_CLAUSES[key]
            points = []  # PFMT 禁忌条款不掺杂模型要点，避免口径冲突

        items.append({"key": key, "title": title, "summary": summary, "points": points})
    return {"intro": LIFESTYLE_INTRO, "items": items}


# 2) 肠道菌群调节 (gut_microbiota_regulation)
#    结构：intro → 菌群解读 → 益生菌方案(产品植入) → 益生元与膳食底物 → 肠屏障修复 → 饮食-菌群协同
#    益生菌方案中的产品、菌株、活菌数、用法与推荐理由一律取自 recommend（产品目录 + 推荐任务），
#    与「核心微生态方案推荐」共用同一套理由，避免两处说法不一致；严禁模型另行生成菌株号或 CFU 剂量。

# 开头话术逻辑：自身菌群状况 → 可能导致或加重评估报告中的疾病/风险/症状 → 因此建议调节菌群。
_GUT_INTRO_TAIL = "建议您通过调节肠道菌群，结合生活方式干预进一步改善健康情况。"
GUT_INTRO_FULL = "您目前存在{state}，可能导致或加重{risks}，" + _GUT_INTRO_TAIL
GUT_INTRO_RISK_ONLY = "您目前的肠道菌群状况可能导致或加重{risks}，" + _GUT_INTRO_TAIL
GUT_INTRO_STATE_ONLY = "您目前存在{state}，可能影响肠道与代谢健康，" + _GUT_INTRO_TAIL
GUT_INTRO_DEFAULT = "肠道菌群状况与肠道及全身代谢健康密切相关，" + _GUT_INTRO_TAIL


def gut_intro(state: str = "", risks: str = "") -> str:
    """按「菌群状况 + 关联的疾病/风险」拼开头；缺哪段就用对应的句式。"""
    if state and risks:
        return GUT_INTRO_FULL.format(state=state, risks=risks)
    if risks:
        return GUT_INTRO_RISK_ONLY.format(risks=risks)
    if state:
        return GUT_INTRO_STATE_ONLY.format(state=state)
    return GUT_INTRO_DEFAULT
GUT_FINDINGS_DEFAULT: dict[str, Any] = {
    "summary": "建议结合本次评估结果，通过膳食底物与规律生活方式持续维护肠道菌群平衡。",
    "points": [],
    "evidence_refs": [],
}

GUT_PROBIOTICS_NONE_SUMMARY = "当前方案以生活方式干预为主，暂不需要额外补充益生菌制剂。"
GUT_PROBIOTICS_SUMMARY = "根据您的菌群状况与健康问题，为您匹配以下个性化微生态调节方案。"
GUT_MDF_RATIONALE_DEFAULT = "协助平衡肠道菌群，改善代谢状态。"
GUT_PFMT_RATIONALE_DEFAULT = "重建菌群丰富度与群落稳定性，恢复肠道微生态平衡。"
# 推荐理由后的固定说明（写死，单独放在 rationale_note，前端灰色显示；模型不写）
GUT_MDF_RATIONALE_SUFFIX = "（按您的菌群缺口个性化调配）"
GUT_PFMT_RATIONALE_SUFFIX = "（按您的肠型与性别一对一配型）"


def _rationale_core(text: str, default: str, suffix: str) -> str:
    """模型理由（缺失用默认），去掉模型可能自带的固定说明；固定说明放在 rationale_note 单独输出。"""
    text = (text or "").strip() or default
    core = text.replace(suffix, "").rstrip()
    if not core.endswith(("。", "！", "；")):
        core += "。"
    return core

GUT_PREBIOTICS_DEFAULT: dict[str, Any] = {
    "summary": "通过日常饮食补充膳食纤维等底物，为肠道有益菌提供“口粮”。",
    "points": [
        "多吃燕麦、豆类、菌菇等富含可溶性纤维的食物",
        "适量摄入放凉的米饭、土豆等抗性淀粉来源",
        "初期可能轻度产气，宜从小量开始逐步增加",
    ],
}

GUT_BARRIER_NOT_APPLICABLE: dict[str, Any] = {"applicable": False, "summary": "", "points": []}

GUT_DIET_SYNERGY_DEFAULT = (
    "膳食纤维与抗性淀粉为有益菌提供底物，有益菌产生的短链脂肪酸滋养肠壁，"
    "形成“饮食养菌、菌护肠壁、肠稳全身”的良性循环。"
)

# 判断「肠屏障相关检测」是否出现在源数据中的关键词；未出现时不输出肠屏障修复方案。
GUT_BARRIER_MARKERS: tuple[str, ...] = (
    "连蛋白", "zonulin", "Zonulin", "脂多糖", "LPS", "内毒素", "二胺氧化酶", "DAO", "D-乳酸",
)


def _strain_label(strain: Any) -> str:
    """产品目录中的菌株可能是字符串或对象，统一转成展示用文本。"""
    if isinstance(strain, str):
        return strain.strip()
    if isinstance(strain, dict):
        for key in ("strainName", "name", "productName", "label"):
            value = strain.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def gut_probiotic_products(
    recommend: dict[str, Any] | None,
    *,
    has_mdf: bool,
    has_pfmt: bool,
) -> list[dict[str, Any]]:
    """从 recommend（产品目录已校准的数据）确定性组装益生菌产品卡片。"""
    recommend = recommend or {}
    products: list[dict[str, Any]] = []
    if has_mdf and recommend.get("mdf_code"):
        strains = [label for label in map(_strain_label, recommend.get("MDF") or []) if label]
        raw_usage = recommend.get("mdf_usageInstructions", "")
        products.append({
            "type": "MDF",
            "name": "肠道微生态靶向调节剂(MDF)",
            "code": recommend.get("mdf_code", ""),
            "strains": strains,
            "specification": recommend.get("mdf_productSpecification", ""),
            "shelf_life": recommend.get("mdf_shelfLife", ""),
            # 营养底物：产品固定配方，与菌株组成同级（原「功效成分」不再使用）
            "substrates": [item["name"] for item in MDF_SUBSTRATES],
            "rationale": _rationale_core(
                recommend.get("mdf_product_rationale", ""), GUT_MDF_RATIONALE_DEFAULT, GUT_MDF_RATIONALE_SUFFIX,
            ),
            # 推荐理由后的固定说明，前端以灰色字体展示
            "rationale_note": GUT_MDF_RATIONALE_SUFFIX,
            "action_focus": recommend.get("mdf_action_focus", ""),
            "observation_focus": recommend.get("mdf_observation_focus", ""),
            # 以下三项由 attach_product_details 用润色后的用法、异常处置、注意事项覆盖
            "usage": [raw_usage] if raw_usage else [],
            "storage": [],
            "notes": [],
        })
    if has_pfmt and recommend.get("pfmt_code") and recommend.get("PFMT"):
        raw_usage = recommend.get("pfmt_usageInstructions", "")
        products.append({
            "type": "PFMT",
            "name": recommend.get("PFMT", ""),
            "code": recommend.get("pfmt_code", ""),
            "source": PFMT_SOURCE,
            "viable_count_per_capsule": recommend.get("pfmt_viableCountPerCapsule", ""),
            "total_viable_count_per_box": recommend.get("pfmt_totalViableCountPerBox", ""),
            "rationale": _rationale_core(
                recommend.get("pfmt_product_rationale", ""), GUT_PFMT_RATIONALE_DEFAULT, GUT_PFMT_RATIONALE_SUFFIX,
            ),
            "rationale_note": GUT_PFMT_RATIONALE_SUFFIX,
            "action_focus": recommend.get("pfmt_action_focus", ""),
            "observation_focus": recommend.get("pfmt_observation_focus", ""),
            "usage": [raw_usage] if raw_usage else [],
            "storage": [],
            "notes": [],
        })
    return products


def attach_product_details(
    non_drug_intervention: dict[str, Any],
    *,
    usage: dict[str, Any] | None = None,
    exception_handling: list[str] | None = None,
    notes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把用法用量、存储与异常处置、注意事项并入对应的产品卡片（原地修改并返回）。

    这三项由独立子任务生成，完成后再合并，保证「微生态调节」里的每个产品
    自带完整信息，前端无需再从顶层字段拼装。
    """
    usage = usage or {}
    notes = notes or {}
    gut = non_drug_intervention.get("gut_microbiota_regulation") or {}
    for product in (gut.get("probiotics") or {}).get("products") or []:
        key = "mdf" if product.get("type") == "MDF" else "pfmt"
        polished = usage.get(f"{key}_usage")
        if isinstance(polished, list) and polished:
            product["usage"] = list(polished)
        product["notes"] = list(notes.get(f"{key}_notes") or [])
        # 冷冻存储与异常处置只针对 PFMT 胶囊
        product["storage"] = list(exception_handling or []) if key == "pfmt" else []
    return non_drug_intervention


# PFMT 菌群来源：产品固定文案，与单粒活菌载量等同级展示。
PFMT_SOURCE = "健康人源肠道菌群（全自动微滤纯化工艺）"

# PFMT 用法用量：产品固定方案，不取产品目录的 usageInstructions，也不经模型润色。
# PFMT_USAGE 逐条展示在 usage.pfmt_usage 与产品卡片 usage；PFMT_USAGE_INSTRUCTIONS 写入 recommend.pfmt_usageInstructions。
PFMT_USAGE: tuple[str, ...] = (
    "服用剂量：首次18粒，后续连续服用三天，每日1次，每次10粒，不可中断。",
    "服用时段：晨起空腹。",
)
PFMT_USAGE_INSTRUCTIONS = "首次18粒，后续连续服用三天，每日1次，每次10粒，不可中断；晨起空腹服用。"

# PFMT 活菌量：产品固定规格，不取产品目录的 viableCountPerCapsule / totalViableCountPerBox。
# 写入 recommend.pfmt_viableCountPerCapsule / pfmt_totalViableCountPerBox 及产品卡片同名字段（字段名不变）。
PFMT_VIABLE_COUNT_PER_CAPSULE = "1.6 x 10^11 CFU（1600亿）"
PFMT_TOTAL_VIABLE_COUNT_PER_BOX = "7.68 x 10^12 CFU（7.68万亿）"

# ---------- MDF 营养底物：产品固定配方（每个 MDF 均包含），由产品维护 ----------
# 在 MDF 产品卡片中与「菌株组成」同级展示，只输出名称；content（每粒标签含量）仅作内部留档，不对外展示。
MDF_SUBSTRATES: list[dict[str, str]] = [
    {"name": "瑞士乳杆菌R0052", "content": "40亿CFU"},
    {"name": "长双歧杆菌长亚种R0175", "content": "20亿CFU"},
    {"name": "羟基酪醇-环糊精复合物", "content": "100mg"},
    {"name": "唾液联合乳杆菌LI01", "content": "10亿CFU"},
    {"name": "嗜粘蛋白阿克曼菌AKM-Lab-01", "content": "150亿TFU"},
    {
        "name": "乳酸菌复合粉（副干酪乳酪杆菌L26、乳酸乳球菌乳亚种R1058、短双歧杆菌HA129、"
                "长双歧杆菌婴儿亚种HA116、发酵粘液乳杆菌HA179）",
        "content": "430亿CFU",
    },
]


def prebiotics_base(
    *,
    has_mdf: bool,
    personalized: dict[str, Any] | None,
) -> dict[str, Any]:
    """益生元与底物支持：仅在方案不含 MDF 时给出日常饮食底物建议；
    含 MDF 时营养底物已在 MDF 产品卡片中展示，这里返回空内容（前端隐藏该块）。"""
    from copy import deepcopy

    if has_mdf:
        return {"summary": "", "points": []}
    diet = (personalized or {}).get("prebiotics") or deepcopy(GUT_PREBIOTICS_DEFAULT)
    return {"summary": diet.get("summary", ""), "points": diet.get("points", [])}


def gut_microbiota_regulation_base(
    *,
    recommend: dict[str, Any] | None = None,
    has_mdf: bool = False,
    has_pfmt: bool = False,
    personalized: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装肠道菌群调节模块。``personalized`` 为已规整的模型输出，缺失部分逐项回退。

    probiotics 内含完整产品信息：联合干预依据 + 每个产品的规格、推荐理由、作用方向、
    观察重点、用法用量、存储处置与注意事项。
    """
    from copy import deepcopy

    personalized = personalized or {}
    products = gut_probiotic_products(
        recommend, has_mdf=has_mdf, has_pfmt=has_pfmt,
    )
    return {
        "intro": gut_intro(personalized.get("microbiota_state", ""), personalized.get("linked_risks", "")),
        "microbiota_findings": personalized.get("microbiota_findings") or deepcopy(GUT_FINDINGS_DEFAULT),
        "probiotics": {
            "summary": GUT_PROBIOTICS_SUMMARY if products else GUT_PROBIOTICS_NONE_SUMMARY,
            # [已停用] 个性化干预依据：与菌群解读、开头话术内容重复，先注释；恢复时取消注释
            # "combination_rationale": (recommend or {}).get("combination_rationale", "") if products else "",
            "products": products,
        },
        # 有 MDF 时为配方中的配套营养底物；否则为日常饮食底物建议
        "prebiotics": prebiotics_base(has_mdf=has_mdf, personalized=personalized),
        "gut_barrier": personalized.get("gut_barrier") or deepcopy(GUT_BARRIER_NOT_APPLICABLE),
        "diet_synergy": personalized.get("diet_synergy") or GUT_DIET_SYNERGY_DEFAULT,
    }


# 3) 营养素推荐：开场白与安全提示为固定文案，模型只生成 items。
SUPPLEMENTS_INTRO = "在调理过程中，我们建议您补充以下营养素，以帮助调节您的健康状态。"
SUPPLEMENTS_TIP = "营养素补充不能替代药物治疗；正在服药、孕期或哺乳期人群请先咨询医师。"
# 每个营养素由模型生成的字段：名称 / 每日剂量 / 剂型 / 理由 / 服用方式 / 注意事项。
# 循证依据只来自评估报告 evidence 中的文献（模型填 evidence_refs 编号，不自行查找文献或链接），
# evidence（展示用文字）由程序根据 evidence_refs 生成。
SUPPLEMENT_FIELDS: tuple[str, ...] = (
    "name", "daily_dose", "dosage_form", "reason", "usage", "caution",
)

# 没有有效个体结果时不补造营养素名称或剂量；固定安全提示仍保留。
NUTRIENT_SUPPLEMENT_ITEMS_DEFAULT: list[dict[str, Any]] = []


def short_usage(text: str, limit: int = 12) -> str:
    """服用方式只保留第一个分句，如「随早餐服用，与含脂食物同服吸收更好」→「随早餐服用」。"""
    import re
    first = re.split(r"[，,；;。]", str(text or "").strip(), maxsplit=1)[0].strip()
    return first[:limit]


# ---------- 营养素循证依据：候选文献池 + 兜底回填 ----------
# 营养素的循证依据只能来自评估报告自带的 evidence，一条都不许自己造。
# 但只把 15 篇文献整包丢给模型，它经常一篇都挑不出来，这一栏就长期是空的。
# 所以这里先由程序从源报告里筛出「与营养 / 膳食 / 生活方式干预相关」的候选池：
#   ① 交给模型时附上这份带编号的短名单，让它在名单里挑；
#   ② 模型没挑或挑的编号不存在时，程序再按营养素名称回填，保证这一栏不空着。

# ① 源报告 diagnostic_advice.health_recommendations 里这些方向引用过的文献，最贴合营养素
SUPPLEMENT_EVIDENCE_CATEGORIES: tuple[str, ...] = ("diet", "nutrition", "supplement")

# ② evidence[].topic 命中这些主题的，进候选池
SUPPLEMENT_EVIDENCE_TOPICS: tuple[str, ...] = ("人体干预与生活方式", "健康总结", "营养")

# ③ 标题 / 结论 / 关键证据命中这些词的，进候选池（中英文都要，源文献多为英文）
SUPPLEMENT_EVIDENCE_KEYWORDS: tuple[str, ...] = (
    "营养", "膳食", "饮食", "维生素", "矿物质", "微量元素", "补充剂", "膳食纤维",
    "饮水", "水分", "蛋白", "脂肪酸",
    "nutrition", "nutrient", "diet", "dietary", "vitamin", "mineral", "supplement",
    "fiber", "fibre", "calcium", "magnesium", "iron", "zinc", "folate", "folic",
    "omega", "fatty acid", "protein", "hydration", "water intake", "potassium", "sodium",
)

# 候选池上限：给模型的短名单太长等于没筛
SUPPLEMENT_EVIDENCE_POOL_MAX = 8
# 单个营养素最多挂几条文献
SUPPLEMENT_EVIDENCE_REFS_MAX = 2

# 营养素名称 → 用于和英文文献匹配的同义词。回填时按命中词数打分。
SUPPLEMENT_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "维生素d": ("vitamin d", "25-hydroxy", "calcifediol", "cholecalciferol", "维生素d"),
    "维生素c": ("vitamin c", "ascorbic", "维生素c"),
    "维生素b": ("vitamin b", "b vitamin", "thiamin", "riboflavin", "cobalamin", "维生素b"),
    "维生素k": ("vitamin k", "menaquinone", "维生素k"),
    "维生素a": ("vitamin a", "retinol", "维生素a"),
    "维生素e": ("vitamin e", "tocopherol", "维生素e"),
    "叶酸": ("folate", "folic", "叶酸"),
    "钙": ("calcium", "钙"),
    "镁": ("magnesium", "镁"),
    "铁": ("iron", "ferritin", "铁"),
    "锌": ("zinc", "锌"),
    "钾": ("potassium", "钾"),
    "硒": ("selenium", "硒"),
    "鱼油": ("omega", "fish oil", "fatty acid", "epa", "dha", "鱼油"),
    "omega": ("omega", "fish oil", "fatty acid", "epa", "dha"),
    "辅酶q10": ("coenzyme q10", "ubiquinone", "辅酶"),
    "膳食纤维": ("fiber", "fibre", "膳食纤维", "dietary fiber"),
    "蛋白": ("protein", "蛋白"),
    "复合维生素": ("multivitamin", "vitamin", "nutrition", "nutrient", "diet", "膳食", "营养"),
    "矿物质": ("mineral", "矿物质", "nutrition", "营养"),
}


def _evidence_match_text(entry: dict[str, Any]) -> str:
    """把一条文献里可用于匹配的字段拼成一段小写文本。"""
    fields = ("title", "topic", "key_evidence", "conclusion", "summary", "study_type")
    return " ".join(str(entry.get(f) or "") for f in fields).lower()


def supplement_evidence_pool(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """从评估报告里筛出可供营养素模块引用的候选文献，按相关度排序。

    只返回源报告 evidence 中真实存在的条目，编号统一转成字符串。
    """
    data = data if isinstance(data, dict) else {}
    by_number: dict[str, dict[str, Any]] = {
        str(e.get("number")): e
        for e in (data.get("evidence") or [])
        if isinstance(e, dict) and e.get("number") is not None
    }
    picked: list[str] = []

    def _take(value: Any) -> None:
        number = str(value).strip()
        if number in by_number and number not in picked:
            picked.append(number)

    # ① 健康建议里营养 / 膳食方向已经引用过的文献，优先级最高
    advice = data.get("diagnostic_advice") or {}
    for rec in advice.get("health_recommendations") or []:
        if not isinstance(rec, dict):
            continue
        if str(rec.get("category") or "").strip().lower() not in SUPPLEMENT_EVIDENCE_CATEGORIES:
            continue
        for ref in rec.get("evidence_refs") or []:
            _take(ref)

    # ② 主题命中
    for number, entry in by_number.items():
        topic = str(entry.get("topic") or "")
        if any(keyword in topic for keyword in SUPPLEMENT_EVIDENCE_TOPICS):
            _take(number)

    # ③ 标题 / 结论关键词命中
    for number, entry in by_number.items():
        text = _evidence_match_text(entry)
        if any(keyword in text for keyword in SUPPLEMENT_EVIDENCE_KEYWORDS):
            _take(number)

    return [by_number[number] for number in picked[:SUPPLEMENT_EVIDENCE_POOL_MAX]]


def format_supplement_evidence_candidates(pool: list[dict[str, Any]] | None) -> str:
    """把候选池渲染成提示词里的短名单文本。池为空时给出明确的禁止编造说明。"""
    if not pool:
        return "（本次评估报告没有与营养相关的可引用文献，所有 evidence_refs 一律输出 []，不得自行编造编号。）"
    lines = []
    for entry in pool:
        number = str(entry.get("number"))
        title = str(entry.get("title") or "").strip().rstrip(".。")
        topic = str(entry.get("topic") or "").strip()
        key = str(entry.get("key_evidence") or "").strip()
        line = f"[{number}] {title}"
        if topic:
            line += f"（主题：{topic}）"
        if key:
            line += f" 要点：{key[:60]}"
        lines.append(line)
    return "\n".join(lines)


def _alias_words_for(name: str) -> tuple[str, ...]:
    """营养素名称 → 匹配用同义词；没命中别名表时退回名称本身。"""
    lowered = str(name or "").lower()
    words: list[str] = []
    for key, aliases in SUPPLEMENT_NAME_ALIASES.items():
        if key in lowered:
            words.extend(aliases)
    return tuple(dict.fromkeys(words)) or (lowered,)


def backfill_supplement_evidence(
    non_drug_intervention: dict[str, Any],
    data: dict[str, Any] | None,
    *,
    limit: int = SUPPLEMENT_EVIDENCE_REFS_MAX,
) -> None:
    """模型没给（或给的编号不存在）时，按营养素名称从候选池回填循证依据。

    打分规则：营养素同义词在文献文本里命中几个就得几分；全都不命中时，
    退回候选池里排最前的那条（即与膳食 / 营养最相关的文献），保证不空着。
    走兜底文案的「复合维生素矿物质」同样适用。
    """
    supplements = non_drug_intervention.get("supplements") or {}
    items = supplements.get("items") or []
    if not items:
        return
    pool = supplement_evidence_pool(data)
    if not pool:
        return
    valid = {str(entry.get("number")) for entry in pool}
    # 源报告里真实存在的全部编号：模型挑的只要在这里面就尊重原样，不强行改成候选池里的
    existing = {
        str(e.get("number"))
        for e in ((data or {}).get("evidence") or [])
        if isinstance(e, dict) and e.get("number") is not None
    }
    for item in items:
        refs = [str(r).strip() for r in item.get("evidence_refs") or [] if str(r).strip()]
        # 先剔掉源报告里不存在的编号（模型偶尔会编号码），剔完还有就保留模型的选择
        refs = [ref for ref in refs if ref in existing]
        item["evidence_refs"] = refs
        if refs:
            continue
        aliases = _alias_words_for(item.get("name"))
        scored: list[tuple[int, int, str]] = []
        for order, entry in enumerate(pool):
            text = _evidence_match_text(entry)
            score = sum(1 for word in aliases if word and word in text)
            scored.append((-score, order, str(entry.get("number"))))
        scored.sort()
        best = [number for negative, _, number in scored if -negative > 0][:limit]
        if not best:
            best = [scored[0][2]]
        item["evidence_refs"] = [number for number in best if number in valid]


def attach_supplement_evidence(
    non_drug_intervention: dict[str, Any],
    evidence_list: list[dict[str, Any]] | None,
) -> None:
    """根据 evidence_refs 生成每个营养素的循证依据展示文字，如「《文献名》[3]」。
    只保留评估报告 evidence 中真实存在的编号；没有则为空字符串（前端显示「-」）。"""
    by_number = {
        str(e.get("number")): e
        for e in (evidence_list or [])
        if isinstance(e, dict) and e.get("number") is not None
    }
    items = (non_drug_intervention.get("supplements") or {}).get("items") or []
    for item in items:
        refs = [str(r) for r in item.get("evidence_refs") or [] if str(r) in by_number]
        # 展示时按编号升序，避免出现「[2]；[1]」这种倒序
        refs.sort(key=lambda r: (0, int(r)) if r.isdigit() else (1, 0))
        item["evidence_refs"] = refs
        item["evidence"] = "；".join(
            f"《{str(by_number[r].get('title', '')).strip().rstrip('.。')}》[{r}]" for r in refs
        )


def supplements_base(items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    from copy import deepcopy
    return {
        "intro": SUPPLEMENTS_INTRO,
        "items": deepcopy(items if items is not None else NUTRIENT_SUPPLEMENT_ITEMS_DEFAULT),
        "tip": SUPPLEMENTS_TIP,
    }


def non_drug_intervention_base(
    *,
    has_mdf: bool = False,
    has_pfmt: bool = False,
    recommend: dict[str, Any] | None = None,
    lifestyle: dict[str, dict[str, Any]] | None = None,
    gut: dict[str, Any] | None = None,
    supplement_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """按固定顺序组装三位一体非药物干预方案：生活方式干预 → 微生态调节 → 营养素推荐。

    本方案即医生出具的处方与医嘱，不包含「建议就医」类提示（原 medical_alert 已移除）。
    """
    return {
        "lifestyle_intervention": lifestyle_intervention_base(
            has_mdf=has_mdf, has_pfmt=has_pfmt, personalized=lifestyle,
        ),
        "gut_microbiota_regulation": gut_microbiota_regulation_base(
            recommend=recommend, has_mdf=has_mdf, has_pfmt=has_pfmt, personalized=gut,
        ),
        "supplements": supplements_base(supplement_items),
    }


# ---------- 临床医嘱 - 推荐检测 (recommended_tests) ----------
# 开头话术由程序按检测项名称拼装；模型只给出检测项与理由。

# 开头话术固定，不再重复罗列检测项名称（名称已在下方表格中）。
RECOMMENDED_TESTS_INTRO = "为进一步完善您的健康评估，建议补充以下检测："
RECOMMENDED_TESTS_MAX_ITEMS = 3

# 源数据里没有菌群检测时，首推肠道菌群检测（方案中的肠道调节依赖它）。
# 建议时限只能取以下几档，模型给出其他值时回退到默认档。
RECOMMENDED_TEST_TIMEFRAMES: tuple[str, ...] = ("2周内", "1个月内", "3个月内", "6个月内")
RECOMMENDED_TEST_TIMEFRAME_DEFAULT = "3个月内"

RECOMMENDED_TESTS_DEFAULT_NO_MICROBIOME: list[dict[str, Any]] = [
    {"name": "肠道菌群", "reason": "了解肠道菌群构成与平衡状态，为个性化肠道调节提供依据。", "timeframe": "3个月内"},
]
# 已有菌群数据时，补充基础的血液生化检查，完善代谢相关资料。
RECOMMENDED_TESTS_DEFAULT_WITH_MICROBIOME: list[dict[str, Any]] = [
    {"name": "血常规与血生化（血糖、血脂、肝肾功能）", "reason": "完善基础代谢资料，与菌群结果相互印证。", "timeframe": "3个月内"},
]
# 用于判断源数据中是否已有菌群检测的关键词。
MICROBIOME_DATA_MARKERS: tuple[str, ...] = ("菌群", "菌属", "菌种", "肠型", "丰度", "杆菌", "球菌")


def _has_microbiome_data(evaluate: dict[str, Any] | None) -> bool:
    import json
    text = json.dumps(evaluate or {}, ensure_ascii=False)
    return any(marker in text for marker in MICROBIOME_DATA_MARKERS)


def recommended_tests_base(
    items: list[dict[str, Any]] | None = None,
    *,
    evaluate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装推荐检测模块；items 为空时按源数据情况给出基础推荐。"""
    from copy import deepcopy

    if not items:
        items = deepcopy(
            RECOMMENDED_TESTS_DEFAULT_WITH_MICROBIOME
            if _has_microbiome_data(evaluate)
            else RECOMMENDED_TESTS_DEFAULT_NO_MICROBIOME
        )
    return {
        "intro": RECOMMENDED_TESTS_INTRO,
        "items": items,
    }


FOLLOWUP_DEFAULTS: dict[str, Any] = {
    "info": INFO,
    "recommend": RECOMMEND,
    "diet": DIET,
    "exercise": EXERCISE,
    "supplements": SUPPLEMENTS,
    "usage": USAGE,
    "exception_handling": EXCEPTION_HANDLING,
    "opinion_summary": OPINION_SUMMARY,
    "opinion_basis": OPINION_BASIS,
    "opinion_advice": OPINION_ADVICE,
    # [已停用] explain_summary / explain_basis / explain_advice：
    # 报告解读改为手册结构，五个子任务的兜底由 explain_defaults.default_section() 按上下文拼装。
    # 非药物干预方案 / notes 的兜底内容由上方 *_base 函数按当前方案动态拼装，
    # 不在这里放静态副本，避免产品禁忌条款不同步。
}


# ---------- 确定性文案（注意事项） ----------
# 模型只负责润色。放在这里让 reports.py 和 default_followup_report() 共用一份。

def notes_base(*, has_mdf: bool = False, has_pfmt: bool = False) -> dict[str, list[str]]:
    """按当前推荐方案拼装注意事项原文（未经模型润色）。"""
    notes: dict[str, list[str]] = {"pfmt_notes": [], "mdf_notes": []}
    if has_pfmt:
        notes["pfmt_notes"] = [
            "本制剂仅用于肠道微生态失衡人群的菌群重建；用于疾病治疗时，须在符合条件的医疗机构内、由医师指导下使用；",
            "本制剂为一对一精准配型定制，专属本人使用，严禁转借、赠予他人；配型不匹配易引发肠道不适并降低定植效用；",
            "特殊人群使用前必须提前告知医师评估：孕妇、哺乳期女性、婴幼儿、免疫缺陷人群、术后恢复期人群、长期服用免疫抑制剂人群；",
            "服用前确认无胶囊载体、菌群原料过敏史；存在过敏者禁止使用；",
            "服用期间如出现持续腹痛、严重腹泻、恶心、皮疹、发热等不适，立即停止服用，并联系配型医师评估处置；",
            "所有供体均经传染病、致病菌、耐药菌多层筛查，但无法完全规避个体差异化耐受反应；严格遵循配套生活方式干预方案可提升定植成功率；",
            "不可与高温食物、抗生素同服；",
        ]
    if has_mdf:
        notes["mdf_notes"] = [
            "本品为益生菌类微生态调节制剂，属非药物干预产品，不能代替药物治疗疾病；",
            "对本品任一菌株或辅料成分过敏者禁用；孕期、哺乳期女性及婴幼儿食用前请咨询医师；",
            "食用过程中如出现持续腹胀、腹泻、皮疹等不适，请立即停用并及时联系医师或专属健康管理师；",
            "贮存：置阴凉、干燥、避光处密封保存；环境温度持续高于 25℃ 时建议冰箱 2～8℃ 冷藏；",
            "本标签仅对应本次菌群检测定制方案，更新复测报告后需重新定制适配制剂。",
        ]
    return notes


def _fallback_supplement_evidence(
    non_drug_intervention: dict[str, Any],
    evaluate: dict[str, Any],
) -> list[dict[str, Any]]:
    """兜底报告的顶层 evidence：只收营养素实际引用到的文献，并重编号为连续的 1..n。

    与正常路径一致，正文里的 evidence_refs 同步替换成新编号。
    """
    from copy import deepcopy

    items = (non_drug_intervention.get("supplements") or {}).get("items") or []
    by_number = {
        str(e.get("number")): e
        for e in (evaluate.get("evidence") or [])
        if isinstance(e, dict) and e.get("number") is not None
    }
    used: list[str] = []
    for item in items:
        for ref in item.get("evidence_refs") or []:
            if str(ref) in by_number and str(ref) not in used:
                used.append(str(ref))
    if not used:
        return []
    used.sort(key=lambda r: (0, int(r)) if r.isdigit() else (1, 0))
    remap = {old: str(index) for index, old in enumerate(used, start=1)}

    evidence: list[dict[str, Any]] = []
    for old in used:
        entry = deepcopy(by_number[old])
        entry["number"] = int(remap[old])
        evidence.append(entry)
    for item in items:
        refs = [remap[str(r)] for r in item.get("evidence_refs") or [] if str(r) in remap]
        item["evidence_refs"] = refs
        item["evidence"] = "；".join(
            f"《{str(by_number[old].get('title', '')).strip().rstrip('.。')}》[{remap[old]}]"
            for old in used
            if remap[old] in refs
        )
    return evidence


def default_followup_report(
    report_type: str,
    *,
    evaluate: dict[str, Any],
    user_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Return complete renderable fields without a substitute clinical assessment."""
    from copy import deepcopy

    subject = evaluate.get("subject") or {}
    sample = evaluate.get("sample") or {}
    common: dict[str, Any] = {
        "type": report_type,
        "userId": str(user_id),
        "sessionId": str(session_id),
        "subject": {
            "name": subject.get("name"),
            "gender": subject.get("gender"),
            "age": subject.get("age"),
            "report_date": sample.get("report_date"),
        },
    }
    if report_type == "intervene":
        return {
            **common,
            "recommend": deepcopy(RECOMMEND),
            "diet": deepcopy(DIET),
            "supplements": deepcopy(SUPPLEMENTS),
            "behavior": deepcopy(EXERCISE["behavior"]),
            "exercise": deepcopy(EXERCISE["exercise"]),
            "evidence": [],
        }
    if report_type == "instruction":
        # 通用安全提示不代替个体化营养处方；没有推荐项时不回填文献。
        non_drug_intervention = non_drug_intervention_base()
        backfill_supplement_evidence(non_drug_intervention, evaluate)
        attach_supplement_evidence(non_drug_intervention, evaluate.get("evidence"))
        evidence = _fallback_supplement_evidence(non_drug_intervention, evaluate)
        return {
            **common,
            "info": deepcopy(INFO),
            "recommend": deepcopy(RECOMMEND),
            "usage": {"pfmt_usage": [], "mdf_usage": []},
            "exception_handling": [],
            "notes": notes_base(),
            "non_drug_intervention": non_drug_intervention,
            "recommended_tests": recommended_tests_base(evaluate=evaluate),
            "evidence": evidence,
        }
    if report_type == "opinion":
        return {
            **common,
            "diagnosis_summary": deepcopy(OPINION_SUMMARY),
            "diagnostic_basis": deepcopy(OPINION_BASIS),
            "diagnostic_advice": deepcopy(OPINION_ADVICE),
            "evidence": [],
        }
    if report_type == "explain":
        # 手册的兜底不是静态套话：仍按这位受检者的评估报告确定性拼装，
        # 模型全挂时手册里也是真实数据。详见 explain_defaults.default_explain_manual()。
        return default_explain_manual(
            evaluate=evaluate, user_id=user_id, session_id=session_id,
        )
    if report_type == "explain_gutBarrier":
        # 肠道与炎症报告解读：兜底同样按这份报告的真实内容确定性拼装
        return default_gut_barrier_manual(
            evaluate=evaluate, user_id=user_id, session_id=session_id,
        )
    raise ValueError(f"unsupported follow-up report type: {report_type}")


__all__ = [
    "FOLLOWUP_DEFAULTS",
    "default_followup_report",
    "attach_product_details",
    "gut_microbiota_regulation_base",
    "lifestyle_intervention_base",
    "non_drug_intervention_base",
    "recommended_tests_base",
    "supplements_base",
    "notes_base",
]
