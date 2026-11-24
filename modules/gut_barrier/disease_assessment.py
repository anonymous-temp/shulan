"""Reviewed, non-numeric disease assessment, separate from a prediction model.

Search results are candidates, not executable medical rules. Only reviewed
guidance below can support a patient-specific statement; new retrieved papers
remain in the audit until clinically reviewed. No score/probability is invented.
"""
from __future__ import annotations

from copy import deepcopy
from math import isfinite

from .assessment import _elevated_lipid_findings


VERSION = "gut-barrier-qualitative-v2.20261010-objective-context"
REVIEW_DATE = "2026-09-24"
# Stable identifiers are shown beside claims and in the selected bibliography.
# Scope describes what was verified, not a claim that the source validates IBFC.
EVIDENCE = {
    "Q1": {
        "citation": "肥胖症诊疗指南（2024年版）", "year": 2024,
        "publisher": "国家卫生健康委员会", "doi": None,
        "url": "https://www.gov.cn/zhengce/zhengceku/202410/P020241020715873679737.pdf",
        "location": "成人 BMI 分类、代谢并发症评估；不将自述体重作为已核实诊断",
    },
    "Q2": {
        "citation": "AASLD Practice Guidance on the clinical assessment and management of nonalcoholic fatty liver disease",
        "year": 2023, "publisher": "Hepatology", "doi": "10.1097/HEP.0000000000000323",
        "url": "https://pubmed.ncbi.nlm.nih.gov/36727674/",
        "location": "肥胖与脂肪性肝病的关联；需综合代谢、肝功能及影像评价，不由屏障指标诊断",
    },
    "Q3": {
        "citation": "2. Diagnosis and Classification of Diabetes: Standards of Care in Diabetes—2026",
        "year": 2026, "publisher": "Diabetes Care", "doi": "10.2337/dc26-S002",
        "url": "https://pubmed.ncbi.nlm.nih.gov/41358893/",
        "location": "成人糖代谢风险因素与血糖、HbA1c 检测；不是屏障指标风险公式",
    },
    "Q4": {
        "citation": "2019 ACC/AHA Guideline on the Primary Prevention of Cardiovascular Disease: Executive Summary",
        "year": 2019, "publisher": "Circulation", "doi": "10.1161/CIR.0000000000000677",
        "url": "https://pubmed.ncbi.nlm.nih.gov/30879339/",
        "location": "常规危险因素及炎症信息的综合评价；不向年轻人外推 40–75 岁十年风险算法",
    },
}

_NEEDS = {
    "intestinal_barrier_followup": "腹痛、腹泻、便血及病程",
    "nafld": "肝脏影像、肝功能、饮酒及代谢病史",
    "ibs": "腹痛与排便的关系、症状持续时间及警示症状",
    "ibd": "持续腹泻、便血或体重下降等病史，以及粪便炎症指标或内镜资料",
    "cirrhosis": "慢性肝病史、肝脏影像、血小板及肝功能",
    "type2_diabetes": "空腹血糖、HbA1c 及相关病史",
    "obesity": "可核实的身高、体重、腰围及体成分",
    "hyperlipidemia": "近期完整血脂检查",
    "hyperuricemia": "血尿酸、肾功能及用药情况",
    "atherosclerosis": "血压、完整血脂、糖代谢及吸烟情况",
    "coronary_heart_disease": "胸痛等症状、血压、血脂、吸烟及心血管病史",
    "hypertension": "规范的诊室或家庭多次血压测量",
    "stroke": "血压、心律、血脂、吸烟及脑血管病史",
    "alzheimers": "认知变化、日常功能及家族史",
    "parkinsons": "运动症状、神经系统检查及相关病史",
    "depression": "情绪症状、持续时间、功能影响及规范量表",
    "anxiety": "焦虑症状、持续时间、功能影响及规范量表",
    "rheumatoid_arthritis": "关节肿痛、晨僵及专科检查",
    "systemic_lupus": "系统性症状及有指征时的免疫学检查",
    "chronic_kidney_disease": "肾功能、尿白蛋白及持续时间",
    "colorectal_cancer": "家族史、便血等警示症状及适龄筛查资料",
    "liver_cancer": "乙肝或丙肝、肝硬化等高危病史及相应随访资料",
    "lung_cancer": "吸烟量、职业暴露、症状及适龄筛查资料",
}


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) else None


def _features(assessment):
    context = assessment.get("context") or {}
    age = _number((assessment.get("subject") or {}).get("age"))
    adult = age is not None and age >= 18
    # Only independently supplied objective body measurements are eligible.
    bmi = _number(context.get("bmi")) if adult else None
    return {"adult": adult, "age": age, "bmi": bmi,
            "lipids": _elevated_lipid_findings(context) if adult else []}



def _selected_reference(identifier):
    reference = deepcopy(EVIDENCE[identifier])
    reference["citation"] = f"[评估{identifier[1:]}] " + reference["citation"]
    return reference


def apply_qualitative_assessment(assessment, retrieval=None):
    """Enrich internal assessment without replacing an approved numeric model.

    Concern labels explicitly denote qualitative attention, not validated high/
    medium/low probabilities. Fields awaiting an approved calculator stay null.
    """
    output = deepcopy(assessment)
    features = _features(output)
    bmi, adult = features["bmi"], features["adult"]
    obesity = bmi is not None and bmi >= 28
    rows = output.get("diseaseRisks", [])
    selected = set()
    for row in rows:
        code = row["code"]
        label, basis, action, refs = "专项资料不足", "", "", []
        if obesity and code == "obesity":
            label = "体重异常（定性）"
            basis = f"BMI 为 {bmi:.1f} kg/m²，达到中国成人肥胖 BMI 范围。"
            action = "优先复核体格测量与腰围，结合体成分及代谢检查评估当前健康影响。"
            refs = ["Q1"]
        elif obesity and code in {"nafld", "type2_diabetes", "hyperlipidemia", "hypertension"}:
            label = "存在相关因素（定性）"
            basis = f"BMI {bmi:.1f} kg/m² 提示体重相关代谢负担，是本病需要关注的相关因素。"
            action, refs = {
                "nafld": ("结合肝功能、饮酒史及临床需要的肝脏影像，评估是否存在脂肪性肝病。", ["Q1", "Q2"]),
                "type2_diabetes": ("优先完善空腹血糖或 HbA1c，结合其他危险因素决定筛查与随访。", ["Q1", "Q3"]),
                "hyperlipidemia": ("完善近期血脂谱，明确是否存在血脂异常，而非由体重代替血脂结果。", ["Q1"]),
                "hypertension": ("通过规范血压测量核实当前血压；体重异常本身不等同高血压。", ["Q1"]),
            }[code]
        if features["lipids"] and code == "hyperlipidemia":
            label, refs = "已有异常线索（定性）", ["Q4"]
            basis = "现有检验记录中存在 LDL-C 或总胆固醇升高，属于明确的血脂异常线索。"
            action = "结合原检查日期、近期血脂谱及既往治疗情况复评；历史记录不作为本次新测值。"
        if adult and code in {"atherosclerosis", "coronary_heart_disease", "stroke"} and (
                obesity or features["lipids"]):
            label, refs = "存在相关因素（定性）", ["Q4"]
            facts = []
            if obesity:
                facts.append(f"BMI {bmi:.1f} kg/m² 的体重因素")
                refs.append("Q1")
            if features["lipids"]:
                facts.append("既有血脂升高记录")
            basis = "存在" + "、".join(facts) + "。"
            action = "重点完善血压、血脂、糖代谢及吸烟史，综合评价长期心血管风险。"
            if features["age"] < 40:
                action += "本年龄阶段以危险因素管理为重点，不套用 40–75 岁人群的十年风险分层。"
        if not basis:
            basis = "现有资料不足以作本病的个体风险分层；需结合" + _NEEDS[code] + "。"
            action = "是否开展专项检查，应根据症状、年龄和病史决定。"
            if not adult:
                basis = "成人风险依据不适用于当前年龄资料；本病需按实际年龄及专项临床资料评价。"
        selected.update(refs)
        row["qualitative"] = {"label": label, "basis": basis, "recommendation": action, "evidenceIds": refs}
        # Independent numerical evaluator is authoritative when truly evaluated.
        if row.get("riskIndex") is None and row.get("status") != "evaluated":
            row["riskLevel"] = label
            row["status"] = "qualitative_only" if refs else "insufficient_specific_evidence"
    output["diseaseAssessment"] = {
        "version": VERSION, "reviewDate": REVIEW_DATE, "mode": "qualitative_not_probability",
        "references": [_selected_reference(key) for key in EVIDENCE if key in selected],
        "retrieval": deepcopy(retrieval or {"status": "not_requested", "candidates": [], "issues": []}),
        "appliedEvidence": [{"id": key, "url": EVIDENCE[key]["url"], "reviewDate": REVIEW_DATE,
                             "scope": EVIDENCE[key]["location"]} for key in EVIDENCE if key in selected],
    }
    return output
