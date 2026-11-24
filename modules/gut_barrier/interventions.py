"""Deterministic, non-prescribing interventions for the gut-barrier report.

Every product, nutrient and medical-nutrition item remains explicitly
conditional until a clinician has checked the indication, contraindications,
concomitant medicines and the exact product.  Barrier biomarkers never start an
infusion, probiotic or microbiota transfer by themselves.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .catalog import match_mdf_products, match_pfmt_products, normalize_product_catalog, pattern_plans
from .evidence import references_for


_ABNORMAL_PATTERNS = {"+--", "-+-", "--+", "++-", "+-+", "-++", "+++"}
_MARKER_NAMES = {"D_LACTATE": "D-乳酸", "DAO": "二胺氧化酶", "LPS": "细菌脂多糖"}
_CATALOG_ISSUES = {
    "product_catalog_missing_or_invalid": "产品目录未返回，本次只能给出机制与成分审核要点，不匹配产品编码。",
    "product_catalog_results_invalid": "产品目录结构无法识别，需核对目录接口版本。",
    "mdf_catalog_empty": "目录中没有可用的 MDF 条目。",
    "pfmt_catalog_empty": "目录中没有可用的 FMT 条目。",
    "mdf_package_course_quantity_requires_reconciliation": "MDF 目录中“30粒/盒”与“2粒/日、30天”不一致，不向用户换算发放数量，需产品方核对。",
    "mdf_required_component_missing_strain_identifier": "MDF 固定成分中有菌种未给出菌株编号，不能将菌种层面的功能外推到该成分。",
    "mdf_catalog_rows_invalid": "MDF 目录列表结构异常，已忽略，不生成产品匹配。",
    "pfmt_catalog_rows_invalid": "FMT 目录列表结构异常，已忽略，不生成产品匹配。",
    "mdf_catalog_record_invalid": "MDF 目录含有非对象记录，已忽略该记录。",
    "mdf_required_strains_invalid": "MDF 固定菌株字段不是合法列表，已忽略，不按字符拆分菌株。",
    "mdf_optional_strains_invalid": "MDF 可选菌株字段不是合法列表，已忽略，不按字符拆分菌株。",
    "mdf_approved_patterns_invalid": "MDF 目录中的分型映射格式无效，不能用于精准配方匹配。",
}


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _resolved_context(assessment: dict, explicit: dict | None) -> dict:
    subject = _dict(assessment.get("subject"))
    merged: dict[str, Any] = {}
    merged.update(_dict(subject.get("context")))
    merged.update(_dict(assessment.get("context")))
    for key in (
        "age", "sex", "symptoms", "bmi", "heightCm", "weightKg", "waistCm",
        "conditions", "medications", "enterotype", "habits", "allergies",
        "immuneStatus", "liverAndKidneyFunction", "nutrientStatus",
    ):
        if subject.get(key) is not None:
            merged[key] = subject[key]
    merged.update(_dict(explicit))
    return merged


def _clinical_segments(value: Any):
    """Keep each field/answer independent when examining clinical wording.

    Joining answers with spaces lets a negative answer suppress the next
    positive answer. Boolean false and empty leaves are not assertions.
    """
    if value is None or value is False:
        return
    if value is True:
        yield "true"
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if item is True:
                yield str(key)
            elif item not in (None, False, "", [], {}):
                for segment in _clinical_segments(item):
                    yield f"{key} {segment}"
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _clinical_segments(item)
        return
    yield str(value)


_CLINICAL_CLAUSE = re.compile(r"[┋|;；\r\n,，。!?！？]+|(?<!\b[A-Za-z])\.(?=\s|$)|(?:但是|但|然而|不过)|\bbut\b", re.I)
_FAMILY_SUBJECT = re.compile(r"家族史|家族病史|父亲|母亲|父母|祖父|祖母|兄弟|姐妹|\b(?:family history|father|mother|parent|sibling)\b", re.I)
_HYPOTHETICAL = re.compile(r"如果|假如|如出现|如有|若(?:出现|有|发生)?|预防|防止|避免|\b(?:if|prevent|prevention|avoid)\b", re.I)
_NEGATED_PREFIX = re.compile(r"(?:不|未|不再|从不|不是)\s*$|(?:无|否认|未见|没有|不伴|未发现|不存在|未使用|未服用)[^,;.!?，；。！？]{0,24}$|\b(?:no|not|denies|denied|without)\b[^,;.!?]{0,40}$", re.I)
_NEGATED_SUFFIX = re.compile(r"^\s*(?:(?:过敏(?:史)?|病史|用药|使用|服用)\s*)?[（(:：\s]*(?:无|没有|否认|未见|不存在|阴性|已排除|no|false|absent|negative|not present)(?:[\s）),，;；。.!?！？]|$)", re.I)


def _affirmed_term(text: str, terms: tuple[str, ...]) -> bool:
    for clause in _CLINICAL_CLAUSE.split(text):
        for term in terms:
            for match in re.finditer(re.escape(term), clause, re.I):
                prefix, suffix = clause[:match.start()], clause[match.end():]
                if _FAMILY_SUBJECT.search(prefix) or _HYPOTHETICAL.search(prefix):
                    continue
                if _NEGATED_PREFIX.search(prefix) or _NEGATED_SUFFIX.search(suffix):
                    continue
                return True
    return False


def _has_any(
    context: dict,
    terms: tuple[str, ...],
    *,
    fields: tuple[str, ...] = ("symptoms", "conditions", "diagnoses", "medicalHistory"),
) -> bool:
    """Find an affirmed term in whitelisted clinical fields only.

    Source report summaries, raw microbiome rows and evidence prose are never
    searched. Field/answer/clause boundaries limit negation scope. Family
    statements and hypothetical advice are not personal positive findings.
    This is a bounded conservative check, not a diagnosis extractor.
    """
    return any(_affirmed_term(segment, terms)
               for field in fields for segment in _clinical_segments(context.get(field)))


def _age(context: dict) -> int | None:
    value = context.get("age")
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0 <= parsed <= 150 else None


def _explicit_true(context: dict, *keys: str) -> bool:
    for key in keys:
        value = context.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip().casefold() in {"true", "yes", "1", "是", "有", "阳性"}:
            return True
    return False


def _nutrient_state(context: dict, terms: tuple[str, ...]) -> str:
    """Return documented deficient/adequate/unknown nutrient state.

    The barrier biomarkers are deliberately not used to infer a nutrient
    deficiency.  Only the explicit ``nutrientStatus`` clinical field is read.
    """
    # Never flatten sibling nutrients: "zinc deficient, vitamin D normal" is
    # two independent findings, not a keyword window around either name.
    def compact(value):
        return re.sub(r"[\s_()（）-]", "", value).casefold()

    aliases = {compact(term) for term in terms}
    groups = ({"锌", "zinc"}, {"维生素d", "vitamind", "25ohd"})
    deficiency = r"缺乏|不足|偏低|低值|\bdeficien(?:t|cy)\b|\binsufficien(?:t|cy)\b|\blow\b"
    adequate = r"充足|正常|达标|\badequate\b|\bnormal\b|\bsufficient\b"

    def classify(value):
        if isinstance(value, dict):
            values = [value[key] for key in ("status", "assessment", "resultStatusName", "value")
                      if key in value and isinstance(value[key], str)]
            states = [classify(item) for item in values]
            return states[0] if states and len(set(states)) == 1 else "unknown"
        if not isinstance(value, str):
            return "unknown"
        text = value.casefold().strip()
        if re.search(r"可能|疑似|待|不确定|未明确|未确认|未证实|未确诊|尚不|曾经|既往|\b(?:possible|suspected|unknown|uncertain|unconfirmed|pending|history)\b", text):
            return "unknown"
        # Permit a nutrient name inside a negation ("未发现锌缺乏") without
        # widening the scope to sibling nutrients or unrelated clinical text.
        for term in sorted(terms, key=len, reverse=True):
            text = re.sub(re.escape(term.casefold()), "", text)
        # Negation belongs to this nutrient's own status, never another row.
        negated = re.compile(r"(?:无|未见|未发现|没有|否认|不|\bno\b|\bnot\b)\s*(?:明确|明显)?\s*(?:" + deficiency + r")")
        denied = bool(negated.search(text))
        remainder = negated.sub("", text)
        low = bool(re.search(deficiency, remainder))
        normal = denied or bool(re.search(adequate, remainder))
        if low and normal:
            return "unknown"
        return "deficient" if low else "adequate" if normal else "unknown"

    def candidates(value):
        if isinstance(value, dict):
            exact = [item for key, item in value.items() if compact(str(key)) in aliases]
            if exact:
                return [classify(item) for item in exact]
            # Also permit explicit named rows, but not a generic flatten of
            # arbitrary clinical objects or numeric values without a status.
            name = next((value[key] for key in ("name", "nutrient", "itemName")
                         if isinstance(value.get(key), str)), None)
            return [classify(value)] if name and compact(name) in aliases else []
        if isinstance(value, list):
            return [state for item in value for state in candidates(item)]
        if not isinstance(value, str):
            return []
        states = []
        for clause in re.split(r"[,;.!?，；。！？\n\r]+", value.casefold()):
            normalized = compact(clause)
            if not any(alias in normalized for alias in aliases):
                continue
            mentioned = sum(any(alias in normalized for alias in group) for group in groups)
            # Multiple nutrients in one free-text clause require explicit
            # separation; do not guess which adjective modifies which one.
            states.append(classify(clause) if mentioned == 1 else "unknown")
        return states

    states = candidates(context.get("nutrientStatus"))
    return states[0] if states and len(set(states)) == 1 else "unknown"


def _safety_profile(context: dict) -> dict:
    hospital_codes = {item.get("code") for item in _list(context.get("hospitalSafetySignals")) if isinstance(item, dict)}
    severe_immune = "severe_immune_compromise" in hospital_codes or _explicit_true(context, "severelyImmunocompromised") or _has_any(
        context,
        ("重度免疫抑制", "严重免疫抑制", "severely immunocompromised", "severe immunocompromise"),
        fields=("immuneStatus", "conditions", "diagnoses", "medicalHistory"),
    )
    immune_compromised = severe_immune or "immune_compromised" in hospital_codes or _explicit_true(context, "immunosuppressed", "immunocompromised") or _has_any(
        context,
        ("免疫抑制", "免疫功能低下", "immunosuppressed", "immunocompromised", "immunodeficiency"),
        fields=("immuneStatus", "conditions", "diagnoses", "medicalHistory", "medications"),
    )
    kidney_problem = "kidney_problem" in hospital_codes or _explicit_true(context, "kidneyImpairment", "renalImpairment") or _has_any(
        context,
        ("肾功能不全", "肾衰竭", "慢性肾病", "renal impairment", "renal failure", "kidney disease", "chronic kidney disease"),
        fields=("liverAndKidneyFunction", "conditions", "diagnoses", "medicalHistory"),
    )
    liver_problem = "liver_problem" in hospital_codes or _explicit_true(context, "liverImpairment", "hepaticImpairment") or _has_any(
        context,
        ("肝功能不全", "肝衰竭", "失代偿肝病", "hepatic impairment", "liver failure", "decompensated liver disease"),
        fields=("liverAndKidneyFunction", "conditions", "diagnoses", "medicalHistory"),
    )
    fish_allergy = _has_any(
        context, ("鱼", "海鲜", "fish", "seafood", "omega-3"), fields=("allergies",),
    )
    zinc_interaction = _has_any(
        context,
        ("四环素", "喹诺酮", "青霉胺", "tetracycline", "quinolone", "penicillamine"),
        fields=("medications",),
    )
    fiber_hold = "fiber_hold" in hospital_codes or _has_any(
        context,
        ("肠梗阻", "肠狭窄", "肠麻痹", "bowel obstruction", "intestinal obstruction", "ileus", "intestinal stricture"),
        fields=("conditions", "diagnoses", "medicalHistory"),
    )
    activity_hold = "activity_hold" in hospital_codes or _has_any(
        context,
        ("活动性胸痛", "静息胸痛", "晕厥", "不稳定心绞痛", "chest pain at rest", "syncope", "unstable angina"),
        fields=("symptoms", "conditions", "diagnoses", "medicalHistory"),
    )
    return {
        "severeImmuneCompromise": severe_immune,
        "immuneCompromised": immune_compromised,
        "kidneyProblem": kidney_problem,
        "liverProblem": liver_problem,
        "fishAllergy": fish_allergy,
        "zincInteraction": zinc_interaction,
        "fiberHold": fiber_hold,
        "activityHold": activity_hold,
        "zincStatus": _nutrient_state(context, ("锌", "zinc")),
        "vitaminDStatus": _nutrient_state(context, ("维生素d", "vitamin d", "vitamind", "25(oh)d")),
        "immuneStatusKnown": context.get("immuneStatus") not in (None, "", [], {}),
    }


def _product_allergy_match(product: dict, context: dict) -> list[str]:
    """Return explicit allergen groups found in both history and ingredients."""
    product_text = " ".join(
        str(product.get(key) or "") for key in ("name", "components")
    ).casefold()
    groups = {
        "fish_or_seafood": (("鱼", "海鲜", "fish", "seafood"), ("鱼", "海鲜", "fish", "seafood")),
        "milk": (("牛奶", "乳制品", "milk", "dairy"), ("牛奶", "乳制品", "milk", "dairy")),
        "soy": (("大豆", "豆类", "soy"), ("大豆", "soy")),
        "egg": (("鸡蛋", "蛋类", "egg"), ("鸡蛋", "egg")),
        "nut": (("坚果", "nut"), ("坚果", "nut")),
        "gluten_or_wheat": (("麸质", "小麦", "gluten", "wheat"), ("麸质", "小麦", "gluten", "wheat")),
    }
    matches: list[str] = []
    for code, (history_terms, ingredient_terms) in groups.items():
        if _has_any(context, history_terms, fields=("allergies",)) and any(
            term.casefold() in product_text for term in ingredient_terms
        ):
            matches.append(code)
    return matches


def _pattern(barrier: dict) -> str | None:
    pattern = barrier.get("pattern")
    if pattern in _ABNORMAL_PATTERNS | {"---"}:
        return pattern
    code = barrier.get("patternCode")
    if isinstance(code, str) and len(code) == 3 and set(code) <= {"0", "1"}:
        converted = code.replace("1", "+").replace("0", "-")
        return converted if converted in _ABNORMAL_PATTERNS | {"---"} else None
    return None


def _barrier_care(barrier: dict, safety: dict, *, pattern: str | None) -> dict:
    grade = barrier.get("grade")
    evaluated = barrier.get("status") in {"evaluated", "needs_review"} and pattern is not None
    severe = evaluated and pattern in _ABNORMAL_PATTERNS and grade in {"IV", "V"}
    if severe:
        organ_hold = safety["kidneyProblem"] or safety["liverProblem"]
        if organ_hold:
            affected = "、".join(
                name for active, name in (
                    (safety["kidneyProblem"], "肾功能问题"),
                    (safety["liverProblem"], "肝功能问题"),
                ) if active
            )
            return {
                "name": "肠道屏障医疗营养支持",
                "status": "safety_hold_liver_kidney",
                "started": False,
                "reason": f"已提供{affected}，本报告不展示或启动丙氨酰谷氨酰胺静脉剂量。",
                "condition": ["由营养支持团队结合明确诊断、实验室结果和完整静脉营养方案重新评估"],
                "actions": ["优先评估可行的经口/肠内营养", "不要根据屏障分级自行输注"],
                "dose": None,
                "monitoring": ["肝肾功能、电解质、酸碱状态及实际摄入"],
                "suppressedReasons": ["documented_liver_or_kidney_problem"],
                "references": ["GB-SOURCE-GRADING-2026-V1", "GB-FK-DIPEPTIVEN-PI-V1"],
            }
        return {
            "name": "肠道屏障医疗营养支持",
            "status": "conditional_specialist_review",
            "started": False,
            "reason": "分级较高时应先评估能否通过口服/肠内营养满足需求；屏障检测本身不是静脉输注指征。",
            "condition": [
                "只在肠内营养不足、且临床团队认为需要静脉营养时讨论",
                "由医疗机构核对肝肾功能、酸碱/电解质、总氨基酸方案及药物相互作用",
            ],
            "actions": ["肠内营养优先", "如仍不足，由营养科/临床医师决定是否进入静脉营养流程", "不根据报告自行购买或输注"],
            "dose": {
                "ingredient": "N(2)-L-丙氨酰-L-谷氨酰胺",
                "sourceProgramRange": "0.3–0.5 g/kg/day（用户提供项目方案）",
                "officialMaximum": "0.5 g/kg/day（产品处方信息上限）",
                "duration": "产品处方信息载明最长不超过3周",
                "route": "静脉营养体系中使用，不单独输注",
                "setting": "医疗机构，仅处方及监测条件完备时",
            },
            "monitoring": ["总静脉营养配方与氨基酸摄入", "肝肾功能、电解质与酸碱状态", "肠内摄入恢复后及时下调/停止"],
            "suppressedReasons": [],
            "references": ["GB-SOURCE-GRADING-2026-V1", "GB-FK-DIPEPTIVEN-PI-V1"],
        }
    if evaluated and pattern == "---":
        status, reason = "not_indicated", "三项屏障指标未见异常，没有根据启动静脉修复方案。"
    elif evaluated and pattern in _ABNORMAL_PATTERNS:
        status, reason = "oral_nutrition_first", "当前分级不支持由报告自动启动静脉治疗，先做可耐受的饮食/口服营养评估。"
    else:
        status, reason = "not_assessable", "屏障评估未完成或需人工复核，暂不生成医疗营养方案。"
    return {
        "name": "肠道屏障医疗营养支持", "status": status, "started": False,
        "reason": reason, "condition": [],
        "actions": ["根据真实饮食摄入、消化道症状与临床诊断决定下一步"],
        "dose": None, "monitoring": ["若症状持续，由临床医师确定是否需要额外检查"], "suppressedReasons": [],
        "references": ["GB-SOURCE-GRADING-2026-V1"],
    }


def _mdf(
    assessment: dict,
    context: dict,
    catalog: dict,
    safety: dict,
    *,
    pattern: str | None,
) -> tuple[dict, list[str]]:
    issues: list[str] = []
    abnormal = pattern in _ABNORMAL_PATTERNS
    active_inventory = [item for item in catalog.get("mdfProducts", []) if item.get("status") == "启用"]
    approved_matches = match_mdf_products(catalog, pattern=pattern) if abnormal else []
    match_audit = {
        "status": "not_applicable" if not abnormal else "no_exact_approved_match",
        "pattern": pattern,
        "requiredFields": [
            "approvedPatterns", "formulationApprovalStatus", "formulationApprovalVersion",
            "preparationForm", "requiredStrains[].strain",
        ],
        "matchCount": len(approved_matches),
        "approvalVersion": None,
    }
    common = {
        "name": "个性化靶向功能菌群调节剂（MDF）",
        "started": False,
        "product": None,
        "catalogUsage": None,
        "researchDose": None,
        "requiredStrains": [],
        "optionalCandidates": [],
        "selectedStrains": [],
        "catalogCandidateCount": len(active_inventory),
        "approvedFormulaMatch": match_audit,
        "suppressedReasons": [],
    }
    if not abnormal:
        return ({
            **common,
            "status": "not_indicated",
            "reason": "三项屏障指标未形成可用的异常组合，不根据本报告启动 MDF。",
            "condition": [], "actions": [], "monitoring": [],
            "references": ["GB-SOURCE-GRADING-2026-V1"],
        }, issues)
    if safety["immuneCompromised"]:
        issues.append("已提供免疫抑制/免疫功能低下信息，MDF 活菌或目录形式已停止自动匹配，需由专科核对制剂形式与感染风险。")
        return ({
            **common,
            "status": "safety_hold_immunocompromised",
            "reason": "已知免疫状态使活菌或未明确制剂形式不适合由报告自动建议。",
            "condition": ["由专科确认免疫状态、制剂是否含活微生物以及适用性"],
            "actions": ["本次不匹配产品、不展示目录用法或研究剂量"],
            "monitoring": ["如专科另行启动，按具体制剂监测感染和胃肠不良反应"],
            "suppressedReasons": ["documented_immunocompromise"],
            "references": ["GB-SOURCE-REPAIR-2026-V1", "GB-ISAPP-POSTBIOTIC-2021-V1"],
        }, issues)
    if not approved_matches:
        status = "no_approved_formula_match" if active_inventory else "catalog_unavailable"
        reason = (
            "目录存在启用条目，但没有同时提供本组合的批准映射、批准版本、制剂形式和全部固定菌株号；不选择“最接近”产品。"
            if active_inventory else
            "没有可核对的启用 MDF 目录条目，只保留功能目标和研究审查边界。"
        )
        issues.append("当前 MDF 目录没有满足本组合全部精准匹配条件的已批准配方，产品、菌株选择和目录用法保持为空。")
        return ({
            **common,
            "status": status,
            "reason": reason,
            "condition": ["补齐带版本的分型批准配方", "核对精确菌株号、活菌/灭活/后生元形式、禁忌与产品批次"],
            "actions": ["不用菌种名代替菌株号", "研究候选只在七型策略中展示，不拼成商品配方"],
            "monitoring": ["如以后经审核启动，预先设定症状目标与停止条件"],
            "references": ["GB-PRODUCT-CATALOG-20260919-V1", "GB-SOURCE-REPAIR-2026-V1", "GB-ISAPP-POSTBIOTIC-2021-V1"],
        }, issues)
    if len(approved_matches) != 1:
        match_audit["status"] = "multiple_exact_matches_need_human_selection"
        issues.append("同一分型命中多个已批准 MDF 配方，规则不自动选择首项。")
        return ({
            **common,
            "status": "multiple_approved_matches_need_review",
            "reason": "存在多个精确候选，需要人工比较适用条件、过敏原、制剂形式和批次。",
            "condition": ["由配方审核人员选择并记录依据"],
            "actions": ["本次不展示产品用法或已选菌株"],
            "monitoring": [],
            "references": ["GB-PRODUCT-CATALOG-20260919-V1", "GB-SOURCE-REPAIR-2026-V1"],
        }, issues)

    product = approved_matches[0]
    allergy_matches = _product_allergy_match(product, context)
    match_audit.update({
        "status": "exact_approved_match",
        "approvalVersion": product.get("formulationApprovalVersion"),
    })
    if allergy_matches:
        issues.append("已知过敏原与 MDF 产品成分相符，已停止该产品建议。")
        return ({
            **common,
            "status": "suppressed_due_to_allergy",
            "reason": "目录产品成分与已提供的过敏信息存在匹配，本报告不展示用法或启动方案。",
            "condition": ["由临床人员核对完整辅料和过敏史"],
            "actions": ["不使用该目录产品作为候选"],
            "monitoring": [],
            "approvedFormulaMatch": match_audit,
            "suppressedReasons": [f"allergen:{item}" for item in allergy_matches],
            "references": ["GB-PRODUCT-CATALOG-20260919-V1"],
        }, issues)

    required = deepcopy(product.get("requiredStrains", []))
    optional = deepcopy(product.get("optionalStrains", []))
    bloating = _has_any(context, ("腹胀", "bloating", "abdominal distension"), fields=("symptoms",))
    optional_strains = {item.get("strain") for item in optional}
    research_dose = None
    if bloating and {"NCFM", "Bi-07"}.issubset(optional_strains):
        for item in optional:
            if item.get("strain") in {"NCFM", "Bi-07"}:
                item["status"] = "eligible_pair_for_human_review"
                item["reason"] = "与非便秘型功能性肠病腹胀次要结果的精确菌株一致，但未经本次人工审核"
                item["references"] = ["GB-RCT-NCFM-BI07-21436726-V1"]
        research_dose = {
            "amount": "NCFM+Bi-07 研究复方合计 2×10^11 CFU/day",
            "duration": "8周",
            "outcome": "腹胀为改善的次要结果；主要全局结局未显示组间优势",
            "limitations": "独立研究剂量并非目录产品用量，也不证明可降低本次屏障指标",
        }
    return ({
        **common,
        "status": "approved_formula_match_requires_review",
        "reason": "本组合存在带批准元数据的精确目录配方，但仍须核对个体适用条件后才能决定是否使用。",
        "condition": ["审核免疫状态、用药、过敏、症状目标和产品批次", "记录批准版本与实际选择依据"],
        "product": {
            "code": product["code"], "name": product["name"],
            "specification": product.get("specification"), "components": product.get("components"),
            "preparationForm": product.get("preparationForm"),
        },
        "catalogUsage": {
            "directions": product.get("usageInstructions"), "package": product.get("specification"),
            "course": "仅为目录声明；尚未形成患者处方或执行记录",
        },
        "researchDose": research_dose,
        "requiredStrains": required,
        "optionalCandidates": optional,
        "approvedFormulaMatch": match_audit,
        "actions": ["固定菌株为批准目录配方成分，不表示本报告已开具", "可选菌株继续保持候选状态"],
        "monitoring": ["腹胀、腹痛、排便频率和便性", "不良反应及新发感染征象", "4–8周按预设目标复评"],
        "references": ["GB-PRODUCT-CATALOG-20260919-V1", "GB-SOURCE-REPAIR-2026-V1"] + (["GB-RCT-NCFM-BI07-21436726-V1"] if research_dose else []),
    }, issues)


def _pfmt(
    assessment: dict,
    context: dict,
    catalog: dict,
    safety: dict,
    *,
    pattern: str | None,
) -> tuple[dict, list[str]]:
    issues: list[str] = []
    abnormal = pattern in _ABNORMAL_PATTERNS
    sex = context.get("sex")
    enterotype = context.get("enterotype")
    matches = match_pfmt_products(catalog, sex=sex, enterotype=enterotype) if abnormal else []
    recurrent_cdiff = _has_any(
        context,
        ("复发性艰难梭菌", "recurrent c. difficile", "recurrent clostridioides difficile"),
        fields=("conditions", "diagnoses", "medicalHistory"),
    )
    refractory_severe_cdiff = _has_any(
        context,
        ("抗菌治疗无效的重症艰难梭菌", "抗菌治疗无效的暴发性艰难梭菌", "refractory severe c. difficile", "refractory fulminant c. difficile"),
        fields=("conditions", "diagnoses", "medicalHistory"),
    )
    trial_only_condition = _has_any(
        context,
        ("肠易激综合征", "炎症性肠病", "溃疡性结肠炎", "克罗恩病", "储袋炎", "IBS", "IBD", "ulcerative colitis", "crohn", "pouchitis"),
        fields=("conditions", "diagnoses", "medicalHistory"),
    )
    guideline_indication_signal = recurrent_cdiff or refractory_severe_cdiff
    candidate = matches[0] if len(matches) == 1 else None
    catalog_status = (
        "not_applicable" if not abnormal else
        "missing_enterotype" if not enterotype else
        "no_exact_match" if not matches else
        "multiple_exact_matches" if len(matches) > 1 else
        "exact_metadata_match"
    )
    catalog_match = {
        "status": catalog_status,
        "candidate": ({
            "code": candidate["code"], "name": candidate["name"],
            "applicableEnterotype": candidate.get("applicableEnterotype"),
            "applicableGender": candidate.get("applicableGender"),
        } if candidate else None),
        "meaning": "仅表示性别/肠型目录元数据吻合，不代表临床适应证或治疗批准。",
    }
    clinical_eligibility = {
        "status": "not_assessed",
        "basis": [],
        "requiresSpecialistReview": True,
    }
    if not abnormal:
        status, reason = "not_indicated", "三项屏障指标未形成可用的异常组合，不启动 FMT。"
        clinical_eligibility.update(status="not_indicated_by_barrier_assessment")
    elif safety["severeImmuneCompromise"]:
        status = "safety_hold_severe_immunocompromise"
        reason = "已提供重度免疫抑制信息；依据 AGA 指南边界，本报告停止 FMT 候选建议。"
        clinical_eligibility.update(status="guideline_suggests_against", basis=["severe_immunocompromise"])
        issues.append("已知重度免疫抑制，FMT 已被安全门控，不展示目录用法或作为治疗候选。")
    elif trial_only_condition and not guideline_indication_signal:
        status = "clinical_trial_only"
        reason = "已提供的胃肠疾病背景不属于常规 FMT 推荐范围；AGA 指南仅支持在临床试验中研究。"
        clinical_eligibility.update(status="clinical_trial_only", basis=["ibd_ibs_or_pouchitis_without_cdiff_indication"])
        issues.append("当前疾病背景下 FMT 仅限临床试验语境，不进行常规治疗推荐。")
    elif not guideline_indication_signal:
        status = "no_guideline_indication_identified"
        reason = "屏障指标、性别或肠型均不能单独构成 FMT 适应证；当前未识别到指南支持的临床指征。"
        clinical_eligibility.update(status="no_guideline_indication_identified")
        issues.append("FMT 当前没有已确认的指南适应证；目录匹配结果仅保留为内部元数据审核。")
    else:
        status = "specialist_review_only"
        reason = "上下文存在需由消化/感染专科核实的指南相关疾病信号；本报告不据此确认诊断或启动 FMT。"
        clinical_eligibility.update(
            status="possible_guideline_context_needs_confirmation",
            basis=[item for condition, item in ((recurrent_cdiff, "recurrent_cdiff"), (refractory_severe_cdiff, "refractory_severe_or_fulminant_cdiff")) if condition],
        )
        issues.append("FMT 需由专科确认艰难梭菌诊断、既往标准治疗、免疫状态、感染筛查和监管要求。")
    if abnormal and not enterotype:
        issues.append("未提供经确认的肠型，FMT 不进行产品元数据匹配；这不影响临床适应证仍须独立判断。")
    elif abnormal and enterotype and not matches:
        issues.append("FMT 性别/肠型仅允许与目录字段精确匹配，且不会把肠型I/II/III自动映射为ET-B/ET-P/ET-F。")
    elif len(matches) > 1:
        issues.append("FMT 同时匹配多个目录条目，规则不自动选首项。")
    if recurrent_cdiff:
        issues.append("上下文提到复发性艰难梭菌，仅作为转诊评估信号；本报告不确认诊断或代替标准治疗。")
    expose_product = candidate if guideline_indication_signal and not safety["severeImmuneCompromise"] else None
    return ({
        "name": "肠道菌群移植（FMT）", "status": status, "started": False,
        "reason": reason,
        "condition": ["必须先有经指南支持的临床适应证", "需要专科评估、供者/病原学筛查、知情同意与当地监管程序"],
        "clinicalEligibility": clinical_eligibility,
        "catalogMatch": catalog_match,
        "product": ({
            "code": expose_product["code"], "name": expose_product["name"],
            "applicableEnterotype": expose_product.get("applicableEnterotype"),
            "applicableGender": expose_product.get("applicableGender"),
        } if expose_product else None),
        "catalogUsage": None,
        "actions": ["不根据 D-乳酸、DAO、LPS 自动开始移植", "对 IBS 等非指南常规适应证人群，不在临床试验外常规推荐"],
        "monitoring": ["如专科最终启动，按机构流程监测感染、胃肠不良反应与长期安全性"],
        "suppressedReasons": (["severe_immunocompromise"] if safety["severeImmuneCompromise"] else []),
        "references": ["GB-AGA-FMT-2024-V1", "GB-FDA-FMT-SAFETY-2020-V1"] + (["GB-PRODUCT-CATALOG-20260919-V1"] if candidate else []),
    }, issues)


def _entry(name: str, status: str, reason: str, *, condition=(), actions=(), dose=None,
           duration: str | None = None, monitoring=(), references=(), suppressed_reasons=(), **extra) -> dict:
    result = {
        "name": name, "status": status, "reason": reason,
        "condition": list(condition), "actions": list(actions), "dose": dose,
        "duration": duration, "monitoring": list(monitoring), "references": list(references),
        "suppressedReasons": list(suppressed_reasons),
    }
    result.update(extra)
    return result


def _nutrition(assessment: dict, context: dict, safety: dict, *, pattern: str | None) -> list[dict]:
    barrier = _dict(assessment.get("barrier"))
    grade = barrier.get("grade")
    age = _age(context)
    adult = age is not None and age >= 18
    result: list[dict] = []
    if pattern == "---":
        if safety["fiberHold"]:
            result.append(_entry(
                "饮食形式评估", "safety_hold", "院内检查或病史提示消化道通行问题，屏障指标正常不代表可以增加膳食纤维。",
                actions=["先由临床团队核对肠梗阻、狭窄或肠麻痹相关记录及当前状态，再确定饮食形式"],
                monitoring=["腹痛、腹胀、呕吐与排便排气"], references=["GB-SOURCE-GRADING-2026-V1"],
            ))
        else:
            result.append(_entry(
                "食物多样性维持", "maintenance", "屏障三项未见异常，不需要补充剂式“修复”。",
                actions=["以谷物、豆类、蔬菜、水果、坚果和种子提供多样化食物基质", "如易腹胀，逐步增加而非一次大幅增量"],
                monitoring=["腹胀、腹痛和大便变化"], references=["GB-SOURCE-GRADING-2026-V1"],
            ))
    elif pattern not in _ABNORMAL_PATTERNS:
        result.append(_entry(
            "补充资料后再制定营养方案", "needs_review", "屏障组合未确定，暂不给出针对性补充剂。",
            actions=["先复核三项屏障指标的当前样本、单位与方法"], references=["GB-SOURCE-GRADING-2026-V1"],
        ))
        return result
    else:
        if safety["fiberHold"]:
            result.append(_entry(
                "总膳食纤维", "safety_hold", "已提供肠梗阻、狭窄或肠麻痹相关信息，本报告不建议增加纤维目标。",
                condition=["先由临床团队确认消化道通行与饮食形式"],
                actions=["暂不按普通屏障养护方案增加全谷、豆类或纤维补充剂"],
                dose=None, duration=None, monitoring=["腹痛、腹胀、呕吐、排便排气变化"],
                references=["GB-SOURCE-GRADING-2026-V1"], suppressed_reasons=["bowel_obstruction_or_stricture_context"],
            ))
        else:
            result.append(_entry(
                "总膳食纤维", "food_first", "为肠道微生物发酵提供食物基质，但需以胃肠耐受为前提。",
                condition=["无肠梗阻/狭窄等限制；明显腹胀或腹泻时分步增加"],
                actions=["优先从全谷、豆类、蔬菜、水果和坚果获取", "每3–7天根据腹胀和大便调整速度"],
                dose=({"amount": "25–35", "unit": "g/day", "source": "provided_program", "note": "为成人总纤维目标，不是 FOS 单品剂量"} if adult else None),
                duration="4周后首次复评耐受和达标情况", monitoring=["腹胀、腹痛、便性、排便频率和饮水"], references=["GB-SOURCE-GRADING-2026-V1"],
            ))

    if pattern in _ABNORMAL_PATTERNS:
        zinc_state = safety["zincStatus"]
        zinc_suppressed: list[str] = []
        if not adult:
            zinc_dose, zinc_status = None, "adult_dose_withheld"
            zinc_reason = "年龄不足或未确认，不输出成人锌剂量。"
        elif safety["kidneyProblem"] or safety["liverProblem"]:
            zinc_dose, zinc_status = None, "safety_hold_liver_kidney"
            zinc_reason = "已提供肝肾功能问题，锌补充需结合实验室和总营养方案重新评估。"
            zinc_suppressed.append("documented_liver_or_kidney_problem")
        elif safety["zincInteraction"]:
            zinc_dose, zinc_status = None, "interaction_review_required"
            zinc_reason = "已提供可能与锌发生给药间隔或吸收相互作用的药物，本报告不生成锌剂量。"
            zinc_suppressed.append("documented_zinc_medication_interaction")
        elif zinc_state == "deficient":
            zinc_dose = {"amount": "15", "unit": "mg elemental zinc/day", "source": "provided_program_candidate", "adultUpperLimit": "40 mg/day（所有来源合计）"}
            zinc_status = "documented_deficiency_needs_clinician_review"
            zinc_reason = "显式营养资料提示锌缺乏/不足，可将项目成人范围交由临床人员审核；尚未形成处方。"
        elif zinc_state == "adequate":
            zinc_dose, zinc_status = None, "not_indicated_by_nutrient_status"
            zinc_reason = "显式营养资料提示锌状态充足，不因屏障指标异常额外启动锌补充。"
        else:
            zinc_dose, zinc_status = None, "nutrient_status_required"
            zinc_reason = "屏障检测不能证明锌缺乏；未取得明确营养状态前不生成补充剂量。"
        result.append(_entry(
            "锌（仅缺乏/摄入不足时）", zinc_status, zinc_reason,
            condition=["先核对饮食、营养状态和所有含锌产品", "核对四环素/喹诺酮类抗生素和青霉胺等相互作用", "未满18岁不输出成人剂量"],
            actions=["若确认需要，使用元素锌计算总量", "不将长期高剂量作为屏障维护"], dose=zinc_dose,
            duration=("4周后复评；长期使用前重新审查铜缺乏风险" if zinc_dose else None),
            monitoring=["胃肠道不适", "总锌摄入", "若延长使用，由专业人员评估铜状态"],
            references=["GB-SOURCE-GRADING-2026-V1", "GB-NIH-ZINC-ODS-V1"], suppressed_reasons=zinc_suppressed,
        ))
        vitamin_d_state = safety["vitaminDStatus"]
        vitamin_d_suppressed: list[str] = []
        if not adult:
            vitd_dose, vitd_status = None, "adult_dose_withheld"
            vitd_reason = "年龄不足或未确认，不输出成人维生素D剂量。"
        elif safety["kidneyProblem"] or safety["liverProblem"]:
            vitd_dose, vitd_status = None, "safety_hold_liver_kidney"
            vitd_reason = "已提供肝肾功能问题，维生素D需结合25(OH)D、钙磷代谢和临床方案评估。"
            vitamin_d_suppressed.append("documented_liver_or_kidney_problem")
        elif vitamin_d_state == "deficient":
            vitd_dose = {"amount": "800–2000", "unit": "IU/day", "source": "provided_program_candidate", "adultUpperLimit": "4000 IU/day（一般成人可耐受最高摄入量）"}
            vitd_status = "documented_deficiency_needs_clinician_review"
            vitd_reason = "显式营养资料提示维生素D不足，可将项目成人范围交由临床人员审核；实际纠正方案取决于基线。"
        elif vitamin_d_state == "adequate":
            vitd_dose, vitd_status = None, "not_indicated_by_nutrient_status"
            vitd_reason = "显式营养资料提示维生素D状态充足，不因屏障指标异常额外启动补充。"
        else:
            vitd_dose, vitd_status = None, "nutrient_status_required"
            vitd_reason = "屏障检测不能判断维生素D状态；未取得25(OH)D等资料前不生成补充剂量。"
        result.append(_entry(
            "维生素D", vitd_status, vitd_reason,
            condition=["先确认年龄与成人剂量适用性", "核对25(OH)D、钙代谢和肾功能相关病史"],
            actions=["不把维生素D表述成直接降低 D-乳酸/DAO/LPS 的药物"], dose=vitd_dose,
            duration=("8–12周后按基线状态复评" if vitd_dose else None),
            monitoring=["25(OH)D、血钙和总摄入（如启动补充）"],
            references=["GB-SOURCE-GRADING-2026-V1", "GB-NIH-VITD-ODS-V1"], suppressed_reasons=vitamin_d_suppressed,
        ))

    if pattern and (pattern[0] == "+" or pattern[1] == "+"):
        glutamine_hold = safety["kidneyProblem"] or safety["liverProblem"]
        if glutamine_hold:
            glutamine_status = "safety_hold_liver_kidney"
            glutamine_reason = "已提供肝肾功能问题，本报告不展示口服谷氨酰胺项目剂量或研究剂量。"
            glutamine_dose = None
            glutamine_actions = ["先由临床营养/专科团队评估总体蛋白质和氨基酸方案"]
            glutamine_suppressed = ["documented_liver_or_kidney_problem"]
        elif not adult:
            glutamine_status = "adult_dose_withheld"
            glutamine_reason = "该研究和项目剂量为成人资料，年龄不足或未确认时不输出。"
            glutamine_dose = None
            glutamine_actions = ["按年龄和明确临床适应证另行评估"]
            glutamine_suppressed = ["adult_age_not_confirmed"]
        else:
            glutamine_status = "specialist_review_only"
            glutamine_reason = "人体试验仅支持特定的感染后 IBS-D 且通透性增高人群，不能仅凭本次屏障指标外推。"
            glutamine_dose = {"sourceProgramRange": "15–25 g/day（用户提供项目方案，待审核）", "conditionSpecificStudy": "5 g、每日3次（仅感染后 IBS-D 且通透性增高研究人群）", "route": "oral"}
            glutamine_actions = ["不自行将项目范围或研究剂量写成当前处方"]
            glutamine_suppressed = []
        result.append(_entry(
            "口服谷氨酰胺", glutamine_status, glutamine_reason,
            condition=["先确认是否符合研究人群或由临床营养师给出其他明确指征", "核对肝肾功能、饮食蛋白质及合并用药"],
            actions=glutamine_actions, dose=glutamine_dose,
            duration=("研究疗程8周；项目方案建议8–12周分期复评" if glutamine_dose else None),
            monitoring=["症状、耐受、肝肾功能（如临床启动）"],
            references=["GB-SOURCE-GRADING-2026-V1", "GB-RCT-GLUTAMINE-30108163-V1"],
            suppressed_reasons=glutamine_suppressed,
        ))
    if pattern and pattern[1] == "+":
        protein_hold = safety["kidneyProblem"] or safety["liverProblem"]
        result.append(_entry(
            "能量与蛋白质充足性", ("safety_review_liver_kidney" if protein_hold else "dietitian_review"),
            ("已提供肝肾功能问题，仅保留营养风险评估，不生成高蛋白目标。" if protein_hold else "DAO 异常组合需要先排查摄入不足，不在未知肾功能时给出高蛋白数字处方。"),
            condition=["核对饮食记录、体重变化、肾/肝功能与消化吸收情况"],
            actions=(["由临床营养师按器官功能评估能量与蛋白质需求"] if protein_hold else ["优先用正餐分配优质蛋白质，个体克数由临床营养师计算"]),
            monitoring=["食物摄入、体重趋势和胃肠耐受"], references=["GB-SOURCE-GRADING-2026-V1"],
            suppressed_reasons=(["documented_liver_or_kidney_problem"] if protein_hold else []),
        ))
    if pattern and pattern[2] == "+":
        if safety["fishAllergy"]:
            result.append(_entry(
                "Omega-3 食物来源", "suppressed_due_to_allergy", "已提供鱼类/海鲜过敏信息，不建议鱼类或鱼油方案。",
                condition=["由临床营养人员根据具体过敏原另选可耐受食物"],
                actions=["不输出每周鱼类份数或鱼油剂量"], dose=None, duration=None,
                monitoring=["过敏反应与替代食物耐受"], references=["GB-AHA-FISH-V1"],
                suppressed_reasons=["documented_fish_or_seafood_allergy"],
            ))
        else:
            result.append(_entry(
                "Omega-3 食物来源", "food_first", "优先以鱼类作为一般心代谢饮食支持，不声称能直接降低本次 LPS。",
                condition=["如妊娠、抗凝/抗血小板用药或考虑鱼油补充剂，先与临床人员确认"],
                actions=["成人可将每周2份鱼类作为一般饮食目标，其中可包括富含脂肪的鱼类", "不在本报告中自动给出鱼油或脂肪乳剂量"],
                dose=({"amount": "2", "unit": "servings fish/week", "source": "AHA food guidance"} if adult else None),
                duration="持续饮食模式，4周回顾可执行性", monitoring=["饮食可接受性；如使用补充剂则另行药物交互审查"], references=["GB-AHA-FISH-V1"],
            ))
    if grade in {"IV", "V"} and pattern in _ABNORMAL_PATTERNS:
        result.append(_entry(
            "肠内营养充足性评估", "medical_nutrition_review", "高分级时先确认能否通过口服/肠内途径满足需求。",
            condition=["体重下降、进食受限、持续腹泻/呕吐或重症情境时由营养支持团队评估"], actions=["肠内营养优先；只有不足时才进入静脉营养审查"], monitoring=["实际摄入、体重、水化状态、电解质和肠道耐受"], references=["GB-SOURCE-GRADING-2026-V1"],
        ))
    return result


def _lifestyle(context: dict, safety: dict) -> list[dict]:
    age = _age(context)
    result = [
        _entry(
            "饮食与症状日志", "recommended", "用真实反应找到可耐受的食物结构，避免根据单次指标大范围忌口。",
            actions=["连续记录2周：每餐食物、腹胀/腹痛、排便频率与便性", "少一次只调整一个变量，避免同时启动多种补充剂"], monitoring=["与饮食改变有时间关系的症状"], references=["GB-SOURCE-GRADING-2026-V1"],
        ),
        _entry(
            "肠道友好的日常饮食结构", "recommended", "以少加工的植物性食物为主，并根据腹胀/腹泻逐步调整。",
            actions=["减少超加工食品、大量添加糖和过量酒精", "排除已知过敏食物后，将可耐受的蔬菜、豆类、全谷和坚果分散到多餐"], monitoring=["食物多样性、过敏与胃肠耐受、排便"], references=["GB-SOURCE-GRADING-2026-V1"],
        ),
    ]
    if safety["fiberHold"]:
        result[1] = _entry(
            "肠道友好的日常饮食结构", "safety_hold", "已有消化道通行问题线索，暂不套用普通增纤维饮食。",
            actions=["先核对检查结果及当前临床状态，由临床团队确定饮食形式和允许摄入范围"],
            monitoring=["腹痛、腹胀、呕吐与排便排气"], references=["GB-SOURCE-GRADING-2026-V1"],
        )
    if age is not None and age >= 18:
        if safety["activityHold"]:
            result.append(_entry(
                "身体活动", "medical_clearance_required", "已提供活动性胸痛、晕厥或不稳定心血管相关信息，本报告不输出常规成人运动目标。",
                condition=["先由临床团队完成安全评估"], actions=["在明确允许的强度和监护条件下再制定计划"],
                dose=None, duration=None, monitoring=["胸痛、晕厥、异常气短等警示症状"], references=["GB-WHO-ACTIVITY-2020-V1"],
                suppressed_reasons=["documented_activity_safety_signal"],
            ))
        else:
            result.append(_entry(
                "身体活动", "recommended", "运动用于整体心代谢和生活方式管理，不是直接“修复”某一检验值。",
                condition=["心肺症状、血压未控、关节损伤或明显不适时先评估"], actions=["从现有水平逐步增加到每周150–300分钟中等强度有氧活动", "每周至少2天主要肌群力量练习"],
                dose={"moderateAerobic": "150–300 min/week", "strength": "at least 2 days/week", "population": "adults"}, duration="持续；每2周逐步增量", monitoring=["运动耐受、胸痛、头晕、异常气短"], references=["GB-WHO-ACTIVITY-2020-V1"],
            ))
        result.append(_entry(
            "睡眠与节律", "recommended", "稳定的睡眠和进餐节律用于整体健康管理。",
            actions=["成人以每晚至少7小时为一般目标", "尽量固定起床时间，睡前减少大量进食和酒精"], dose={"sleep": "at least 7 hours/night", "population": "adults"}, duration="持续", monitoring=["白天困倦、打鼾/呼吸暂停和睡眠规律性"], references=["GB-CDC-SLEEP-V1"],
        ))
    else:
        result.append(_entry(
            "运动与睡眠计划", "age_specific_review", "年龄未确认或未满18岁，不输出成人运动/睡眠数字目标。",
            actions=["按年龄、发育情况、基础疾病和体力水平由医护人员设定目标"], references=["GB-WHO-ACTIVITY-2020-V1", "GB-CDC-SLEEP-V1"],
        ))
    return result


def _followup(assessment: dict, *, pattern: str | None) -> list[dict]:
    context = _dict(assessment.get("context"))
    result = [
        _entry(
            "耐受性与执行复盘", "planned", "先判断饮食调整是否可耐受。", timepoint="1–2周",
            actions=["回顾饮食/症状日志、腹胀、腹痛、便性和不良反应"], monitoring=["每周症状天数和对生活的影响"], references=["GB-SOURCE-GRADING-2026-V1"],
        )
    ]
    if pattern in _ABNORMAL_PATTERNS:
        result.append(_entry(
            "屏障指标复评", "planned", "只有在可比的采样和检测条件下，趋势才有解释价值。", timepoint="4–8周",
            actions=["由临床人员决定是否在同一实验室/方法复测 D-乳酸、DAO、LPS", "与症状和真实干预同时解读"], monitoring=["D-乳酸、DAO、LPS、症状趋势"], references=["GB-SOURCE-GRADING-2026-V1"],
        ))
    if any(context.get(key) is True for key in ("acuteInfection", "acuteInflammation")):
        result.append(_entry(
            "炎症状态优先复核", "priority_review", "已提供的急性炎症背景需要优先临床复核，不直接按慢性或肠源性炎症解释。", timepoint="依临床情况及时评估",
            actions=["先评估感染、创伤和其他急性炎症情境"], monitoring=["体温与相关症状"], references=[],
        ))
    result.append(_entry(
        "营养项目复评", "conditional", "仅在实际启动相应补充时检查有效性与安全性。", timepoint="8–12周",
        actions=["如使用维生素D，根据基线复查25(OH)D/血钙", "如锌使用延长，复核总剂量及铜缺乏风险", "根据预先设定的症状目标决定停止、继续或更换"], monitoring=["只评估真实已执行的项目"], references=["GB-NIH-ZINC-ODS-V1", "GB-NIH-VITD-ODS-V1"],
    ))
    result.append(_entry(
        "需及时就医的情况", "safety_net", "以下情况不等待常规复评。", timepoint="发生时",
        actions=["便血/黑便、持续高热、剧烈或进行性腹痛、频繁呕吐、明显脱水、意识改变或快速恶化时及时就医"], monitoring=[], references=[],
    ))
    return result


def _reference_ids(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "references" and isinstance(item, list):
                yield from (reference_id for reference_id in item if isinstance(reference_id, str))
            else:
                yield from _reference_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from _reference_ids(item)


def _withhold_adult_dosing(value: Any) -> None:
    """Remove actionable adult quantities in-place for minors/unknown age."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"dose", "catalogUsage", "researchDose"}:
                value[key] = None
            else:
                _withhold_adult_dosing(item)
    elif isinstance(value, list):
        for item in value:
            _withhold_adult_dosing(item)


def build_interventions(
    assessment: dict,
    *,
    context: dict | None = None,
    product_catalog: dict | None = None,
) -> dict:
    """Build auditable intervention candidates without auto-prescribing.

    The return shape is stable for rendering.  ``selectedStrains`` stays empty
    unless a future, explicit human-review workflow supplies a verified choice.
    """
    assessment = _dict(assessment)
    resolved = _resolved_context(assessment, context)
    barrier = _dict(assessment.get("barrier"))
    current_pattern = _pattern(barrier)
    catalog = normalize_product_catalog(product_catalog)
    safety = _safety_profile(resolved)

    barrier_care = _barrier_care(barrier, safety, pattern=current_pattern)
    mdf, mdf_issues = _mdf(assessment, resolved, catalog, safety, pattern=current_pattern)
    pfmt, pfmt_issues = _pfmt(assessment, resolved, catalog, safety, pattern=current_pattern)
    plans = pattern_plans(current_pattern)
    nutrition = _nutrition(assessment, resolved, safety, pattern=current_pattern)
    lifestyle = _lifestyle(resolved, safety)
    followup = _followup(assessment, pattern=current_pattern)

    review_items: list[str] = []
    if barrier.get("status") in {"incomplete", "needs_review"} or current_pattern is None:
        review_items.append("屏障分级/组合未完全确定，需先复核当前样本、单位、方法和评分边界。")
    missing = [str(item) for item in _list(assessment.get("missingData")) if item]
    if missing:
        names = [_MARKER_NAMES.get(item, item) for item in missing]
        review_items.append("以下资料缺失或未确认：" + "、".join(names) + "。")
    if current_pattern in _ABNORMAL_PATTERNS:
        for issue in catalog.get("issues", []):
            message = _CATALOG_ISSUES.get(issue)
            if message and message not in review_items:
                review_items.append(message)
        review_items.append("MDF 可选菌株没有被规则自动选中；需根据精确菌株、制剂形式、目标症状和安全性人工审核。")
    review_items.extend(mdf_issues)
    review_items.extend(pfmt_issues)
    if safety["immuneCompromised"]:
        review_items.append("已提供免疫抑制/免疫功能低下信息，活菌、未明确制剂形式及菌群移植方案均不得自动进入执行建议。")
    if safety["kidneyProblem"] or safety["liverProblem"]:
        review_items.append("已提供肝肾功能问题，静脉/口服谷氨酰胺及营养补充剂量已被安全门控，需专科重新评估。")
    if safety["fishAllergy"]:
        review_items.append("已提供鱼类/海鲜过敏信息，鱼类和鱼油相关方案已被抑制。")
    if safety["zincInteraction"]:
        review_items.append("已提供可能与锌发生相互作用的药物，锌补充剂量已被抑制。")
    if safety["fiberHold"]:
        review_items.append("已提供肠梗阻/狭窄/肠麻痹相关信息，常规增纤维目标已被抑制。")
    if barrier.get("grade") in {"IV", "V"} and current_pattern in _ABNORMAL_PATTERNS:
        review_items.append("静脉丙氨酰谷氨酰胺仅为医疗机构内的条件性审查项，本报告未启动输注。")
    # Preserve order while removing duplicate human-readable messages.
    review_items = list(dict.fromkeys(review_items))

    payload = {
        "barrierCare": barrier_care,
        "microbiome": {"mdf": mdf, "pfmt": pfmt, "sevenPatternPlans": plans},
        "nutrition": nutrition,
        "lifestyle": lifestyle,
        "followup": followup,
        "references": [],
        "reviewItems": review_items,
    }
    if (_age(resolved) or 0) < 18:
        _withhold_adult_dosing(payload)
        age_message = (
            "未满18岁，本报告已隐藏所有成人剂量、产品用法和研究剂量，需由儿科/专科人员另行评估。"
            if _age(resolved) is not None else
            "年龄未确认，本报告已隐藏所有成人剂量、产品用法和研究剂量。"
        )
        payload["reviewItems"].append(age_message)
    reference_ids = list(_reference_ids(payload))
    payload["references"] = references_for(reference_ids)
    return payload
