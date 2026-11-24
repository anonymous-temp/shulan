"""Internal, reviewed disease-direction capabilities; never a patient catalog.

Registry presence, patient eligibility, retrieval coverage and a computable
score are deliberately different states. Templates and remote search results
cannot enable a rule. This metadata is internal and is not a callback contract.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import re

from .assessment import _date as normalize_clinical_date
from .continuous_factors import _normalized_gfr_value
from .exam_excerpt import exam_result_excerpt
from .risk_model import build_disease_risks
from .barrier_context import BARRIER_LINKED_CODES, catalog_mechanism_reference_ids


VERSION = "gut-barrier-direction-registry-v7.20261010-objective-context"
# Operational freshness gate, NOT a KDIGO diagnostic or repeat-test interval.
RENAL_OBSERVATION_MAX_AGE_DAYS = 30
TEMPLATE_SYSTEMS = (
    ("digestive", "消化系统"), ("metabolic", "代谢系统"),
    ("cardiovascular", "心脑血管系统"), ("neurological", "神经系统"),
    ("psychiatric", "精神系统"), ("immune", "免疫系统"),
    ("urinary", "泌尿系统"), ("oncology", "肿瘤"),
)

# Conditions name normalized, server-produced facts rather than raw text.
# Supported rows use the existing UNVALIDATED attention rubric unless an
# explicit qualitative-only policy is registered. No history-to-score mapping.
_RULES = {
    "intestinal_barrier_followup": ("adult_accepted_barrier_abnormality", ("intestinal_barrier",), ("Q32", "Q33"), "结合消化道症状、病程和警示征复核屏障功能"),
    "metabolic_barrier_followup": ("adult_barrier_with_metabolic_context", ("intestinal_barrier", "body", "lipids"), ("Q34", "Q35"), "结合血糖、血脂及体重复核糖脂代谢状态"),
    "obesity": ("adult_bmi_ge_28", ("body",), ("Q1",), "复核体测、腰围及代谢因素"),
    "nafld": ("adult_bmi_ge_28", ("body",), ("Q1", "Q2"), "结合肝功能、影像和饮酒史评价"),
    "type2_diabetes": ("adult_bmi_ge_28", ("body",), ("Q1", "Q3"), "完善血糖或HbA1c后评价"),
    "hyperlipidemia": ("adult_obesity_or_accepted_lipids", ("body", "lipids"), ("Q1", "Q4"), "复核近期完整血脂"),
    "hypertension": ("adult_bmi_ge_28", ("body",), ("Q1",), "规范、多次测量血压"),
    "atherosclerosis": ("adult_lipids", ("body", "lipids"), ("Q4",), "综合常规心血管因素"),
    "sleep_apnea": ("adult_bmi_ge_30", ("body",), ("Q12",), "补问鼾声、呼吸暂停及日间嗜睡后决定筛查"),
    "kidney_function_evaluation": ("adult_recent_verified_renal_abnormality", ("renal_laboratory",), ("Q15",), "及时由医师复核肾功能和尿白蛋白、排查急性因素；不得等满3个月才处理"),
    "gastritis_followup": ("adult_accepted_nonatrophic_gastritis_endoscopy", (), ("Q16", "Q17", "Q18"), "核对既往胃镜、病理及幽门螺杆菌检测和治疗史，结合目前症状决定后续评估"),
    "digestive_lesion_followup": ("adult_attributable_digestive_lesion_imaging", (), ("Q36",), "核对影像及后续内镜或病理，不将影像疑似病变视为已确诊肿瘤"),
    "urinary_stone_followup": ("adult_attributable_urinary_stone_imaging", (), ("Q37", "Q38"), "核对结石随访及规范尿检，不以历史资料计算当前疾病风险"),
}
# Deliberately bounded extensions. These routes review an explicitly documented
# diagnosis; they do NOT diagnose from symptoms or barrier measurements.
# Source scopes: NICE NG71 1.2.5; NG222 1.4.3/1.8; NG100 1.9; NG151 1.6.
HISTORY_FOLLOWUP_RULES = {
    "parkinsons": {
        "name": "帕金森病", "aliases": ("帕金森病",), "evidenceIds": ("Q19",),
        "query": "adult Parkinson disease follow up clinical guideline",
        "action": "由神经专科复核既往诊断、运动与非运动症状、功能变化及用药反应，按专科计划随访；不根据肠道检测判断疾病进展。",
    },
    "depression": {
        "name": "抑郁症", "aliases": ("抑郁症", "抑郁障碍"), "evidenceIds": ("Q20",),
        "query": "adult depression follow up clinical guideline",
        "action": "复核当前情绪、日常功能、既往治疗及复发情况，由精神心理专科结合规范评估安排随访；如有自伤或自杀想法，应及时寻求紧急专业帮助。",
    },
    "rheumatoid_arthritis": {
        "name": "类风湿关节炎", "aliases": ("类风湿关节炎", "类风湿性关节炎"), "evidenceIds": ("Q21",),
        "query": "adult rheumatoid arthritis follow up clinical guideline",
        "action": "由风湿免疫专科复核关节症状、疾病活动度、功能及用药监测，按既有方案随访；不自行调整治疗。",
    },
    "colorectal_cancer": {
        "name": "结直肠癌", "aliases": ("结直肠癌", "结肠癌", "直肠癌"), "evidenceIds": ("Q22",),
        "query": "adult colorectal cancer follow up clinical guideline",
        "action": "核对病理、分期、治疗经过和既有随访计划，由肿瘤或结直肠专科明确当前管理阶段；根治性治疗后的复查路径需先核实治疗条件，不从本次肠道指标推断复发或转移风险。",
    },
    "cirrhosis": {
        "name": "肝硬化", "aliases": ("肝硬化",), "evidenceIds": ("Q23",),
        "query": "adult cirrhosis follow up clinical guideline",
        "action": "由肝病专科核对既往诊断、病因、肝功能与并发症资料，按既有计划开展随访和适用的监测；不由肠道检测推定失代偿或肝癌风险。",
    },
    "hyperuricemia": {
        "name": "高尿酸血症", "aliases": ("高尿酸血症",), "evidenceIds": ("Q24",),
        "query": "adult asymptomatic hyperuricemia follow up clinical guideline",
        "action": "复核血尿酸和既往痛风发作、结石及用药史，区分无症状高尿酸血症与痛风，由医师决定后续管理；不因肠道屏障指标启动降尿酸药物。",
    },
    "coronary_heart_disease": {
        "name": "冠心病", "aliases": ("冠心病", "冠状动脉粥样硬化性心脏病"), "evidenceIds": ("Q25",),
        "query": "adult chronic coronary disease follow up clinical guideline",
        "action": "由心血管专科复核当前症状、功能状态、危险因素及既往治疗，检查用药依从性和不良反应，按既有方案随访；不由肠道指标计算心血管事件概率。",
    },
    "stroke": {
        "name": "脑卒中", "aliases": ("脑卒中", "缺血性脑卒中", "出血性脑卒中", "脑梗死"), "evidenceIds": ("Q26",),
        "query": "adult stroke follow up clinical guideline",
        "action": "由神经专科复核既往卒中类型、功能变化、康复及二级预防计划，结合当前生活能力安排随访；不从本次肠道指标推断新发或复发脑卒中。",
    },
    "alzheimers": {
        "name": "阿尔茨海默病", "aliases": ("阿尔茨海默病", "阿尔茨海默症"), "evidenceIds": ("Q27",),
        "query": "adult Alzheimer disease dementia follow up clinical guideline",
        "action": "由认知或神经专科结合既往诊断、认知与日常功能变化、用药及照护需求复核管理计划；不由肠道指标确定认知障碍程度或疾病进展。",
    },
    "anxiety": {
        "name": "焦虑症", "aliases": ("焦虑症", "广泛性焦虑障碍", "广泛性焦虑症", "惊恐障碍"), "evidenceIds": ("Q28",),
        "query": "adult anxiety disorder follow up clinical guideline",
        "action": "由精神心理专科复核诊断类型、当前症状、功能影响及既往治疗反应，结合规范评估安排随访；不以肠道检测替代临床诊断、量表或自行调整治疗。",
    },
    "systemic_lupus": {
        "name": "系统性红斑狼疮", "aliases": ("系统性红斑狼疮",), "evidenceIds": ("Q29",),
        "query": "adult systemic lupus erythematosus follow up clinical guideline",
        "action": "由风湿免疫专科复核疾病活动、器官受累、治疗反应及用药监测，按既有方案随访；不以肠道指标判断狼疮活动度。",
    },
    "chronic_kidney_disease": {
        "name": "慢性肾脏病", "aliases": ("慢性肾脏病", "慢性肾病"), "evidenceIds": ("Q15",),
        "query": "adult chronic kidney disease follow up clinical guideline",
        "action": "由肾脏专科复核既往病因及分期，结合当前eGFR、尿白蛋白和用药情况安排监测；不从一次异常或本次肠道指标推定慢性病程、分期及进展概率。",
    },
    "liver_cancer": {
        # The registered guidance is HCC-specific: an unspecified liver mass,
        # metastasis or the generic term 肝癌 must not inherit its care pathway.
        "name": "肝细胞癌", "aliases": ("肝细胞癌",), "evidenceIds": ("Q30",),
        "query": "adult hepatocellular carcinoma follow up clinical guideline",
        "action": "由肝病及肿瘤专科核对肝细胞癌诊断、分期、肝功能和既往治疗，结合多学科意见确定当前随访计划；不从肠道检测推断复发、转移或生存概率。",
    },
    "lung_cancer": {
        "name": "肺癌", "aliases": ("肺癌", "非小细胞肺癌", "小细胞肺癌"), "evidenceIds": ("Q31",),
        "query": "adult lung cancer follow up clinical guideline",
        "action": "由胸部肿瘤专科核对组织学类型、分期、治疗经过和当前症状，按既有治疗及随访计划管理；不从肠道指标判断复发、转移或肿瘤活动。",
    },
}
for _code, _rule in HISTORY_FOLLOWUP_RULES.items():
    _RULES[_code] = ("adult_documented_diagnosis_" + _code, (), _rule["evidenceIds"], _rule["action"])

# Only a diagnosis-bearing document, not the referral indication of an imaging
# report, can establish this narrow historical fact. Unknown sources stay empty.
_DIAGNOSIS_DOCUMENT = re.compile(r"^(?:门诊病历|住院病历|出院小结|出院记录|诊疗记录|专科病历|门诊诊断|出院诊断|诊断记录|疾病诊断|病理报告|病理诊断)$")
_UNCONFIRMED_DIAGNOSIS = re.compile(
    r"未确诊|未诊断|未证实|未见|无证据|不支持|不成立|并非|不是|否认|排除|误诊|撤销|更正|取消|疑似|可能|考虑|倾向|待查|待排|不能确定|"
    r"家族史|父亲|母亲|兄弟|姐妹|配偶|家属|子女|儿子|女儿|假如|如果|若|预防|宣教|科普|示例|样例|诊断标准|[?？]")
_DIAGNOSIS_REVISION = re.compile(
    r"^(?:但|然而|经)?(?:"
    r"(?:该|此|上述|前述|原|既往)?诊断(?:已|被|经复核)?(?:撤销|排除|取消|更正|不成立|不支持)|"
    r"(?:家属|患者|本人)?否认(?:既往|该|原)?诊断|"
    r"(?:可能)?误诊(?:记录)?$|"
    r"(?:复核|复查)(?:后)?(?:排除|不支持)(?:(?:该|原|上述)?诊断)?$|"
    r"无证据支持(?:(?:该|原|上述)?诊断)?$)")


def _diagnosis_mention(text, aliases):
    """Return the patient's confirmed excerpt and whether this patient is mentioned."""
    pattern = (r"^(?:患者|本人)?(?:既往|曾)?"
               r"(?:确诊(?:为)?|诊断为|诊断[:：]|出院诊断[:：]|最终诊断[:：])\s*[:：]?\s*"
               + aliases + r"$")
    excerpt = None
    mentioned = False
    related = False
    relative_scope = False
    illustrative_scope = False
    for clause in re.split(r"[。；;\r\n，,]", text):
        clause = clause.strip()
        if not clause:
            continue
        if re.match(r"^(?:宣教|科普|示例|样例|诊断标准|假如|如果|若)\s*(?:[:：]|$)", clause):
            illustrative_scope = True
        if illustrative_scope:
            # "患者" inside a teaching/example passage is still hypothetical.
            # Preserve an earlier real diagnosis without adopting this passage.
            continue
        if related and _DIAGNOSIS_REVISION.search(clause):
            return None, True
        relative = re.match(r"^(?:(?:患者|本人)的?)?(?:父亲|母亲|兄弟|姐妹|配偶|家属|子女|儿子|女儿)", clause)
        if re.match(r"^(?:患者|本人|现病史|个人史)", clause) and not relative:
            relative_scope = False
        heading = re.match(r"^(?:家族史|亲属病史|家属病史|(?:父亲|母亲|兄弟|姐妹|配偶|家属|子女|儿子|女儿)(?:病史)?)\s*(?:[:：]|$)", clause)
        if heading:
            # An empty family-history heading may qualify the next line. A
            # completed negative family history does not qualify patient facts.
            detail = clause[heading.end():].strip()
            relative_scope = detail not in {"无", "无特殊", "无特殊家族史"}
            related = False
            continue
        if relative_scope or relative or clause.startswith("家族史"):
            continue
        if re.search(aliases, clause):
            mentioned = True
            related = True
            if _UNCONFIRMED_DIAGNOSIS.search(clause):
                return None, True
            if re.fullmatch(pattern, clause):
                excerpt = clause
        elif re.match(r"^(?:确诊|诊断为|诊断[:：]|出院诊断[:：]|最终诊断[:：])", clause):
            # A later, explicitly named different diagnosis owns its revision.
            related = False
    return excerpt, mentioned


def diagnosis_followup_findings(assessment):
    """Explicit adult hospital diagnosis histories, never inferred diagnoses.

    Latest ambiguous/negative/conflicting mentions suppress an older diagnosis
    for that route. Every accepted fact retains its date and source index.
    """
    context = assessment.get("context") or {}
    target = normalize_clinical_date(context.get("sampleDate")) or normalize_clinical_date(context.get("currentDate"))
    if target is None:
        return {}
    mentions = {}
    for row in context.get("clinicalFindings") or []:
        if not isinstance(row, dict):
            continue
        observed = normalize_clinical_date(row.get("date"))
        if observed is None or observed > target or row.get("temporalStatus") not in {"current", "historical"}:
            continue
        if str(row.get("dataType") or "").upper() not in {"EXAM", "DIAGNOSIS"}:
            continue
        if not _DIAGNOSIS_DOCUMENT.fullmatch(str(row.get("itemName") or "").strip()):
            continue
        sources = [item for item in (row.get("source") or []) if isinstance(item, dict) and item.get("kind") == "hospitalData"]
        if not sources:
            continue
        text = exam_result_excerpt(row.get("examResult"))
        for code, rule in HISTORY_FOLLOWUP_RULES.items():
            if str(row.get("itemName") or "").startswith("病理") and code not in {"colorectal_cancer", "liver_cancer", "lung_cancer"}:
                continue
            aliases = "(?:" + "|".join(re.escape(term) for term in rule["aliases"]) + ")"
            if not re.search(aliases, text):
                continue
            excerpt, patient_mentioned = _diagnosis_mention(text, aliases)
            if not patient_mentioned:
                continue
            valid = bool(excerpt) and not (row.get("conflict") or row.get("conflictingResults")
                or row.get("status") in {"conflict", "needs_review"})
            mentions.setdefault(code, []).append((observed, {
                "code": code, "date": observed, "itemName": row["itemName"],
                "excerpt": excerpt,
                "temporalStatus": "historical" if observed < target else "current",
                "source": deepcopy(sources), "use": "documented_diagnosis_followup_not_current_severity",
            } if valid else None))
    accepted = {}
    for code, rows in mentions.items():
        latest = max(day for day, _ in rows)
        same_day = [fact for day, fact in rows if day == latest]
        if all(fact is not None for fact in same_day):
            accepted[code] = same_day[0]
    return accepted
_EXTENSIONS = (
    ("intestinal_barrier_followup", "肠道炎症与功能紊乱", "digestive", "消化系统"),
    ("metabolic_barrier_followup", "糖脂代谢异常", "metabolic", "代谢系统"),
    ("sleep_apnea", "阻塞性睡眠呼吸暂停（条件筛查）", "sleep_respiratory", "睡眠与呼吸系统"),
    ("kidney_function_evaluation", "肾功能或尿白蛋白异常（病因鉴别）", "urinary", "泌尿系统"),
    ("gastritis_followup", "既往胃炎随访", "digestive", "消化系统"),
    ("digestive_lesion_followup", "消化道占位性病变复核", "digestive", "消化系统"),
    ("urinary_stone_followup", "泌尿系统结石随访", "urinary", "泌尿系统"),
)
_PENDING = {
    "ibs": ("clinical_diagnosis", "gastroenterology_assessment"),
    "ibd": ("clinical_diagnosis", "intestinal_examination"),
    "cirrhosis": ("chronic_liver_history", "liver_imaging", "liver_function"),
    "hyperuricemia": ("verified_urate", "renal_function", "medications"),
    "coronary_heart_disease": ("cardiovascular_history", "symptoms", "conventional_risk_factors"),
    "stroke": ("blood_pressure", "rhythm", "cerebrovascular_history"),
    "alzheimers": ("cognitive_change", "functional_impact", "specialist_assessment"),
    "parkinsons": ("motor_symptoms", "neurological_examination"),
    "depression": ("mood_duration", "functional_impact", "validated_clinical_assessment"),
    "anxiety": ("anxiety_duration", "functional_impact", "validated_clinical_assessment"),
    "rheumatoid_arthritis": ("joint_symptoms", "specialist_assessment"),
    "systemic_lupus": ("systemic_manifestations", "indicated_immunological_tests"),
    "chronic_kidney_disease": ("renal_function", "urine_albumin", "verified_chronicity_at_least_3_months", "clinical_cause_evaluation"),
    "colorectal_cancer": ("family_history", "alarm_symptoms", "age_appropriate_screening"),
    "liver_cancer": ("viral_hepatitis_or_cirrhosis_history", "indicated_surveillance"),
    "lung_cancer": ("smoking_exposure", "symptoms", "age_appropriate_screening"),
}

_RENAL_ALIASES = {
    "EGFR": frozenset({
        "egfr", "估算肾小球滤过率", "肾小球滤过率估算值", "估计肾小球滤过率",
        "估计的肾小球滤过率", "estimatedglomerularfiltrationrate",
        "估算肾小球滤过率egfr", "肾小球滤过率egfr",
    }),
    "UACR": frozenset({
        "uacr", "acr", "尿白蛋白肌酐比", "尿白蛋白/肌酐比", "尿白蛋白肌酐比值",
        "尿白蛋白/肌酐比值", "尿微量白蛋白/肌酐比值", "尿微量白蛋白肌酐比值",
        "尿微量白蛋白肌酐比", "尿微量白蛋白/肌酐比", "尿白蛋白与肌酐比值",
        "尿白蛋白与肌酐比", "urinealbumintocreatinineratio",
        "urinaryalbumintocreatinineratio",
    }),
}


def _renal_code(value):
    """Exact names or one explicit same-analyte alias, never a substring.

    In particular, a bare GFR, a measured clearance, a change/ratio involving
    eGFR, and urine albumin concentration must not inherit the eGFR/UACR gate.
    """
    name = re.sub(r"[\s_‐‑–—－-]", "", str(value or "")).casefold()
    aliases = {alias: code for code, names in _RENAL_ALIASES.items() for alias in names}
    if name in aliases:
        return aliases[name]
    bracketed = re.fullmatch(r"([^()（）]+)[（(]([^()（）]+)[）)]", name)
    if not bracketed:
        return None
    base, suffix = bracketed.groups()
    base_code, suffix_code = aliases.get(base), aliases.get(suffix)
    if base == "肾小球滤过率" and suffix == "egfr":
        return "EGFR"  # Explicit eGFR alias disambiguates measured GFR.
    return base_code if base_code and base_code == suffix_code else None


def registry_entries():
    """Return fresh copies so callers cannot turn template-only rules on."""
    catalog, _ = build_disease_risks()
    rows = [{key: row[key] for key in ("code", "name", "systemCode", "systemName")} for row in catalog]
    catalog_codes = {row["code"] for row in rows}
    rows += [dict(zip(("code", "name", "systemCode", "systemName"), item))
             for item in _EXTENSIONS if item[0] not in catalog_codes]
    for row in rows:
        rule = _RULES.get(row["code"])
        row.update(
            enabled=rule is not None,
            predicate=rule[0] if rule else None,
            requiredFacts=[rule[0]] if rule else list(_PENDING[row["code"]]),
            excludedInputStates=["conflict", "negated", "unknown", "remote_candidate_only", "mechanism_only"],
            evidenceIds=list(dict.fromkeys([*rule[2], *BARRIER_LINKED_CODES.get(row["code"], ()),
                                            *catalog_mechanism_reference_ids(row["code"])])) if rule else [],
            scoringDomains=list(dict.fromkeys([*rule[1], *( ["intestinal_barrier"] if row["code"] in BARRIER_LINKED_CODES else [])])) if rule else [],
            allowedStatement="followup_or_differential_not_diagnosis" if rule else "no_individual_conclusion",
            followupAction=rule[3] if rule else "需专科适用依据和经审核触发规则后启用",
            ruleStatus="reviewed_direction_rule" if rule else "disabled_pending_reviewed_trigger",
            disabledReason=None if rule else "reviewed_trigger_and_applicability_rule_not_implemented",
            activationRequires=[] if rule else ["reviewed_trigger", "evidence_applicability_review", "clinical_fact_validation"],
            scorePolicy=("qualitative_only_no_validated_index" if row["code"] in {"gastritis_followup", "digestive_lesion_followup", "urinary_stone_followup"} or row["code"] in HISTORY_FOLLOWUP_RULES
                         else "unvalidated_attention_rubric" if rule else "not_implemented"),
        )
    return rows


def eligible_codes(features):
    """One gate shared by retrieval planning, report selection and audit."""
    f = features
    if not f.get("adult"):
        return set()
    obese = f.get("bmi") is not None and f["bmi"] >= 28
    predicates = {
        "adult_accepted_barrier_abnormality": bool(f.get("barrierFact")),
        "adult_barrier_with_metabolic_context": bool(f.get("barrierFact")) and (
            obese or bool(f.get("lipids"))),
        "adult_bmi_ge_28": obese,
        "adult_bmi_ge_30": f.get("bmi") is not None and f["bmi"] >= 30,
        "adult_obesity_or_accepted_lipids": obese or bool(f.get("lipids")),
        "adult_lipids": bool(f.get("lipids")),
        "adult_recent_verified_renal_abnormality": bool(f.get("renalFindings")),
        "adult_accepted_nonatrophic_gastritis_endoscopy": bool(f.get("gastritisFindings")),
        "adult_attributable_digestive_lesion_imaging": "digestive_lesion_followup" in (f.get("historicalFollowupFindings") or {}),
        "adult_attributable_urinary_stone_imaging": "urinary_stone_followup" in (f.get("historicalFollowupFindings") or {}),
    }
    for code in HISTORY_FOLLOWUP_RULES:
        predicates["adult_documented_diagnosis_" + code] = code in (f.get("diagnosisFollowupFindings") or {})
    return {code for code, rule in _RULES.items() if predicates[rule[0]]}


def renal_findings(assessment):
    """Conservative exact-value hospital-only renal observation gate.

    KDIGO 2024 Table 1/3 and PP1.1.1.2/1.1.3.2 support confirmatory
    evaluation, not an automatic CKD diagnosis. Serum creatinine alone is not
    converted into eGFR here. Historical persistence is not inferred.
    """
    context = assessment.get("context") or {}
    target_day = normalize_clinical_date(context.get("sampleDate")) or normalize_clinical_date(context.get("currentDate"))
    if target_day is None:
        return []
    target = date.fromisoformat(target_day)
    rows = context.get("clinicalFindings")
    observations = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if row.get("temporalStatus") not in {"current", "historical"}:
            continue
        if str(row.get("dataType") or "").strip().upper() != "INSPECTION":
            continue
        if not any(isinstance(source, dict) and source.get("kind") == "hospitalData" for source in row.get("source") or []):
            continue
        try:
            observed = date.fromisoformat(str(row.get("date")))
        except (ValueError, TypeError):
            continue
        if not 0 <= (target - observed).days <= RENAL_OBSERVATION_MAX_AGE_DAYS:
            continue
        unit = re.sub(r"\s+", "", str(row.get("unit") or "")).casefold().replace("²", "2").replace("^2", "2")
        code = _renal_code(row.get("itemName"))
        if not code:
            continue
        raw = str(row.get("value") if row.get("value") is not None else "").strip()
        value = None
        if re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)", raw):
            try:
                value = Decimal(raw)
            except InvalidOperation:
                pass
        if code == "EGFR":
            value = _normalized_gfr_value(value, row.get("unit"))
            unit = "ml/min/1.73m2" if value is not None else unit
        valid = (not row.get("conflictingResults") and not row.get("conflict")
                 and row.get("status") not in {"conflict", "needs_review"} and value is not None)
        valid = valid and ((code == "EGFR" and unit in {"ml/min/1.73m2", "ml/min/(1.73m2)"} and value > 0)
                           or (code == "UACR" and unit in {"mg/g", "mg/mmol"}))
        abnormal = valid and ((code == "EGFR" and value < 60) or
                              (code == "UACR" and value >= (30 if unit == "mg/g" else 3)))
        observations.setdefault(code, []).append({"code": code, "value": raw, "unit": row.get("unit"),
            "date": row["date"], "source": deepcopy(row["source"]), "observationAgeDays": (target - observed).days,
            "valid": bool(valid), "abnormal": bool(abnormal), "comparison": (value, unit)})
    accepted = []
    for items in observations.values():
        # A newer normal/ambiguous result must not be hidden by an older high
        # one. Detect cross-alias same-analyte conflicts before selection.
        latest_date = max(item["date"] for item in items)
        latest = [item for item in items if item["date"] == latest_date]
        if not all(item["valid"] for item in latest) or len({item["comparison"] for item in latest}) != 1:
            continue
        if latest[0]["abnormal"]:
            accepted.append({key: deepcopy(value) for key, value in latest[0].items()
                             if key not in {"valid", "abnormal", "comparison"}})
    return accepted


def coverage_ledger(features, selected_rows, topic_coverage):
    """Audit disabled rules and unsearched topics without manufacturing rows."""
    eligible = eligible_codes(features)
    selected = {row["code"]: row for row in selected_rows}
    disease_ledger = []
    for registration in registry_entries():
        code = registration["code"]
        row = selected.get(code, {})
        topics = [item for item in topic_coverage if code in item["diseaseCodes"]]
        model = row.get("status") == "evaluated"
        disease_ledger.append({**registration,
            "patientEligibility": "eligible_for_direction" if code in eligible else "rule_disabled" if not registration["enabled"] else "required_patient_facts_not_met",
            "selectionStatus": "registered_model" if model else "selected_direction" if row else "not_selected",
            "scoreStatus": "registered_model" if model else "unvalidated_attention" if row.get("status") == "attention_index" else "not_computed",
            "scoreNotComputedReason": ("qualitative_followup_without_validated_scoring_rule" if row.get("status") == "qualitative_followup"
                                       else registration["disabledReason"] if not registration["enabled"] else None),
            "score": row.get("riskIndex"),
            "retrievalTopics": [item["topic"] for item in topics],
            "retrievalQuestions": [item["query"] for item in topics],
            "retrievalStatuses": [item["retrievalStatus"] for item in topics] or ["not_planned"],
            "retrievedCandidateIds": list(dict.fromkeys(key for item in topics for key in item["candidateIds"])),
            "notSelectedDoesNotExcludeDisease": True,
        })
    systems = list(TEMPLATE_SYSTEMS) + [("dermatology", "皮肤系统"), ("sleep_respiratory", "睡眠与呼吸系统")]
    system_ledger = []
    for system, name in systems:
        rows = [row for row in disease_ledger if row["systemCode"] == system]
        system_ledger.append({"systemCode": system, "systemName": name,
            "templateSystem": (system, name) in TEMPLATE_SYSTEMS,
            "registeredDiseaseCodes": [row["code"] for row in rows],
            "enabledDirectionCount": sum(row["enabled"] for row in rows),
            "eligibleDiseaseCodes": [row["code"] for row in rows if row["code"] in eligible],
            "selectedDiseaseCodes": [row["code"] for row in rows if row["selectionStatus"] != "not_selected"],
            "retrievalTopics": list(dict.fromkeys(topic for row in rows for topic in row["retrievalTopics"])),
            "notSelectedDoesNotExcludeDisease": True})
    return {"version": VERSION, "renalObservationMaxAgeDays": RENAL_OBSERVATION_MAX_AGE_DAYS,
            "diseases": disease_ledger, "systems": system_ledger}
