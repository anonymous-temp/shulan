"""Data-triggered disease directions and an UNVALIDATED attention rubric.

This is not a disease prediction/triage model. Reviewed guidance establishes
relevance, not the weights below. Retrieval output is untrusted candidate data;
it cannot introduce diagnoses, coefficients, citations or executable rules.
"""
from __future__ import annotations

from copy import deepcopy
import re

from .disease_assessment import EVIDENCE, _features, _selected_reference, apply_qualitative_assessment
from .disease_registry import (HISTORY_FOLLOWUP_RULES, coverage_ledger, diagnosis_followup_findings,
                               eligible_codes, renal_findings)
from .clinical_facts import gastritis_findings
from .historical_followup import FOLLOWUP_REFERENCES, historical_followup_findings
from .risk_model import build_disease_risks
from .barrier_context import (
    BARRIER_REFERENCES, BARRIER_LINKED_CODES, CATALOG_MECHANISM_REFERENCES,
    barrier_features, catalog_mechanism_reference_ids, patient_mechanism,
)

VERSION = "gut-barrier-attention-v8.20261010-objective-context"
MODE = "data_driven_attention"
NOTICE = ("健康关注指数采用自定义0–10分标尺，未经临床验证，用于安排随访关注顺序，不代表患病概率，"
          "不能用于诊断、治疗决策或急诊分诊。分值仅汇总已采纳的个体资料，文献命中或指南引用不加分。"
          "未列出的疾病不代表已排除；详细标尺见附录。")
_EXTRA_REFERENCES = {
    "Q12": {"citation": "Obstructive Sleep Apnea Screening Health Advisory", "year": 2017,
            "publisher": "American Academy of Sleep Medicine", "doi": None,
            "url": "https://aasm.org/advocacy/position-statements/sleep-apnea-screening-health-advisory/",
            "location": "BMI≥30成人可考虑OSA风险筛查；不凭肥胖、睡眠时长或量表确诊"},
    "Q15": {"citation": "KDIGO 2024 Clinical Practice Guideline for the Evaluation and Management of Chronic Kidney Disease",
            "year": 2024, "publisher": "Kidney International", "doi": "10.1016/j.kint.2023.10.018",
            "url": "https://kdigo.org/wp-content/uploads/2026/04/KDIGO-2024-CKD-Guideline.pdf",
            "location": "表1/3及实践要点1.1.1.2、1.1.3.2：单次异常不确诊慢性肾脏病；2.1.1—2.1.2支持已有CKD的GFR和尿白蛋白监测，不提供本报告关注分权重"},
    "Q16": {"citation": "Guidelines for diagnosis and treatment of chronic gastritis in China (2022, Shanghai)",
            "year": 2023, "publisher": "Journal of Digestive Diseases", "doi": "10.1111/1751-2980.13193",
            "url": "https://pubmed.ncbi.nlm.nih.gov/37245073/",
            "location": "摘要确认慢性胃炎病因、诊疗与管理范围；不引用未取得全文的具体推荐或设定复查间隔"},
    "Q17": {"citation": "Definition & Facts for Gastritis & Gastropathy", "year": 2019,
            "publisher": "National Institute of Diabetes and Digestive and Kidney Diseases", "doi": None,
            "url": "https://www.niddk.nih.gov/health-information/digestive-diseases/gastritis-gastropathy/definition-facts",
            "location": "胃炎涉及胃黏膜炎症；病因具有异质性，不据旧镜下结论确定当前病因或癌症风险"},
    "Q18": {"citation": "Diagnosis of Gastritis & Gastropathy", "year": 2019,
            "publisher": "National Institute of Diabetes and Digestive and Kidney Diseases", "doi": None,
            "url": "https://www.niddk.nih.gov/health-information/digestive-diseases/gastritis-gastropathy/diagnosis",
            "location": "结合病史、症状、用药以及有指征的内镜/病理和病因检测；不将未检测的幽门螺杆菌视为阳性"},
    "Q19": {"citation": "Parkinson’s disease in adults (NG71)", "year": 2017,
            "publisher": "National Institute for Health and Care Excellence", "doi": None,
            "url": "https://www.nice.org.uk/guidance/ng71/chapter/Recommendations",
            "location": "1.2.5：已诊断帕金森病的诊断复核与随访；不支持从肠道指标诊断或赋予风险分值"},
    "Q20": {"citation": "Depression in adults: treatment and management (NG222)", "year": 2022,
            "publisher": "National Institute for Health and Care Excellence", "doi": None,
            "url": "https://www.nice.org.uk/guidance/ng222/chapter/Recommendations",
            "location": "1.4.3及1.8：已有抑郁症治疗监测与复发预防；不据病史推定当前发作或量表得分"},
    "Q21": {"citation": "Rheumatoid arthritis in adults: management (NG100)", "year": 2018,
            "publisher": "National Institute for Health and Care Excellence", "doi": None,
            "url": "https://www.nice.org.uk/guidance/ng100/chapter/Recommendations",
            "location": "1.9.1及1.9.4：已确诊成人类风湿关节炎的专科监测与综合复评"},
    "Q22": {"citation": "Colorectal cancer (NG151)", "year": 2020,
            "publisher": "National Institute for Health and Care Excellence", "doi": None,
            "url": "https://www.nice.org.uk/guidance/ng151/chapter/Recommendations",
            "location": "1.6：已诊断结直肠癌的持续管理；1.6.1随访检测须先满足非转移性疾病根治性手术条件，不从既往病名推定适用"},
    "Q23": {"citation": "Cirrhosis in over 16s: assessment and management (NG50)", "year": 2016,
            "publisher": "National Institute for Health and Care Excellence", "doi": None,
            "url": "https://www.nice.org.uk/guidance/ng50/chapter/1-Introduction-and-overview",
            "location": "1.1.9及1.2：已有肝硬化的专科转诊和并发症监测；不由屏障指标评定代偿状态或计算肝癌概率"},
    "Q24": {"citation": "2020 American College of Rheumatology Guideline for the Management of Gout", "year": 2020,
            "publisher": "Arthritis Care & Research", "doi": "10.1002/acr.24180",
            "url": "https://acrjournals.onlinelibrary.wiley.com/doi/10.1002/acr.24180",
            "location": "表1无症状高尿酸血症条目：不将高尿酸血症等同痛风，不因其本身自动启动药物；不外推并存心肾疾病风险或提供评分权重"},
    "Q25": {"citation": "2023 AHA/ACC/ACCP/ASPC/NLA/PCNA Guideline for the Management of Patients With Chronic Coronary Disease",
            "year": 2023, "publisher": "Circulation", "doi": "10.1161/CIR.0000000000001168",
            "url": "https://www.ahajournals.org/doi/10.1161/CIR.0000000000001168",
            "location": "4.1及7.1：已诊断慢性冠心病的症状、功能、治疗和并发症随访；不根据肠道指标推断新发冠心病或心血管事件概率"},
    "Q26": {"citation": "Stroke rehabilitation in adults (NG236)", "year": 2023,
            "publisher": "National Institute for Health and Care Excellence", "doi": None,
            "url": "https://www.nice.org.uk/guidance/ng236/chapter/Recommendations",
            "location": "1.2及1.17.5：已发生卒中后的功能、康复和照护需求复评；不由肠道检测识别急性卒中或计算复发风险"},
    "Q27": {"citation": "Dementia: assessment, management and support for people living with dementia and their carers (NG97)",
            "year": 2018, "publisher": "National Institute for Health and Care Excellence", "doi": None,
            "url": "https://www.nice.org.uk/guidance/ng97/chapter/Recommendations",
            "location": "1.3.1—1.3.2：已有痴呆诊断的照护协调和计划复核；本通路仅采纳明确阿尔茨海默病记录，不把泛称痴呆识别成该病"},
    "Q28": {"citation": "Generalised anxiety disorder and panic disorder in adults: management (CG113)",
            "year": 2011, "publisher": "National Institute for Health and Care Excellence", "doi": None,
            "url": "https://www.nice.org.uk/guidance/cg113/chapter/Recommendations",
            "location": "1.2.6—1.2.7及1.2.48：症状、功能、既往治疗与复发相关复评；需核对焦虑诊断类型，不从肠道指标诊断或套用治疗方案"},
    "Q29": {"citation": "EULAR recommendations for the management of systemic lupus erythematosus: 2023 update",
            "year": 2024, "publisher": "Annals of the Rheumatic Diseases", "doi": "10.1136/ard-2023-224762",
            "url": "https://pubmed.ncbi.nlm.nih.gov/37827694/",
            "location": "总体原则：已有SLE的疾病活动、器官受累与治疗依从性监测；不提供风险分值权重"},
    "Q30": {"citation": "AASLD Practice Guidance on prevention, diagnosis, and treatment of hepatocellular carcinoma",
            "year": 2023, "publisher": "Hepatology", "doi": "10.1097/HEP.0000000000000466",
            "url": "https://pubmed.ncbi.nlm.nih.gov/37199193/",
            "location": "已明确肝细胞癌的多学科管理，结合分期、肝功能与既往治疗；不外推至未分型肝癌、肝转移或胆管癌，不提供复发/生存评分"},
    "Q31": {"citation": "Lung cancer: diagnosis and management (NG122)", "year": 2019,
            "publisher": "National Institute for Health and Care Excellence", "doi": None,
            "url": "https://www.nice.org.uk/guidance/ng122/chapter/Management",
            "location": "随访和患者观点章节：肺癌治疗后的专科持续照护；先核对病理、治疗及阶段，不从肠道指标判断肿瘤活动或给予特定复查间隔"},
}


def _signals(assessment):
    """Select risk directions from accepted measurements and clinical records."""
    features = _features(assessment)
    features.update(barrier_features(assessment) if features["adult"] else
                    {"barrierFact": None, "barrierMarkers": [], "barrierSummary": ""})
    features.update(
        renalFindings=renal_findings(assessment) if features["adult"] else [],
        gastritisFindings=gastritis_findings(assessment) if features["adult"] else [],
        historicalFollowupFindings=historical_followup_findings(assessment) if features["adult"] else {},
        diagnosisFollowupFindings=diagnosis_followup_findings(assessment) if features["adult"] else {})
    return features


def evidence_plan(assessment):
    """One explicit topic per direction; never silently truncate the plan.

    Eligibility is distinct from retrieval; only accepted objective facts
    enable patient-specific directions.
    """
    f = _signals(assessment)
    eligible = eligible_codes(f)
    plan = []
    def add(topic, query, codes, refs, reason, eligibility="supported_direction"):
        if eligibility == "supported_direction":
            codes = [code for code in codes if code in eligible]
            if not codes:
                return
        plan.append({"topic": topic, "query": query, "diseaseCodes": codes, "evidenceIds": refs,
                     "eligibility": eligibility, "reason": reason})
    if f["gastritisFindings"]:
        add("gastritis", "adult chronic non-atrophic gastritis evaluation clinical guideline",
            ["gastritis_followup"], ["Q16", "Q17", "Q18"], "accepted_hospital_endoscopy_history_for_qualitative_followup")
    historical = f["historicalFollowupFindings"]
    if "digestive_lesion_followup" in historical:
        add("digestive_lesion", "gastrointestinal subepithelial lesion characterization clinical guideline",
            ["digestive_lesion_followup"], ["Q36"], "dated_imaging_lesion_not_histological_diagnosis")
    if "urinary_stone_followup" in historical:
        refs = ["Q37"] + (["Q38"] if historical["urinary_stone_followup"].get("recordReviews") else [])
        add("urinary_stone", "adult renal stone follow up clinical guideline",
            ["urinary_stone_followup"], refs, "dated_stone_imaging_and_record_reconciliation_not_current_severity")
    for code in f["diagnosisFollowupFindings"]:
        rule = HISTORY_FOLLOWUP_RULES[code]
        add(code, rule["query"], [code], list(rule["evidenceIds"]), "accepted_documented_hospital_diagnosis_for_qualitative_followup")
    if f["bmi"] is not None and f["bmi"] >= 28:
        for topic, query, code, refs in (
            ("obesity", "adult obesity evaluation clinical practice guideline", "obesity", ["Q1"]),
            ("fatty_liver", "adult metabolic dysfunction associated steatotic liver disease clinical practice guideline", "nafld", ["Q1", "Q2"]),
            ("diabetes", "adult type 2 diabetes screening clinical practice guideline", "type2_diabetes", ["Q1", "Q3"]),
            ("dyslipidemia", "adult dyslipidemia evaluation clinical practice guideline", "hyperlipidemia", ["Q1", "Q4"]),
            ("hypertension", "adult hypertension screening clinical practice guideline", "hypertension", ["Q1"]),
        ):
            add(topic, query, [code], refs, "accepted_body_measurement_factor")
    elif f["lipids"]:
        add("dyslipidemia", "adult dyslipidemia evaluation clinical practice guideline", ["hyperlipidemia"], ["Q4"], "accepted_lipid_abnormality")
    if f["lipids"]:
        add("cardiovascular", "adult cardiovascular prevention clinical practice guideline", ["atherosclerosis"], ["Q4"], "accepted_cardiovascular_related_factor")
    if f["bmi"] is not None and f["bmi"] >= 30:
        add("sleep_apnea", "adult obstructive sleep apnea screening clinical guideline", ["sleep_apnea"], ["Q12"], "obesity_related_conditional_screening")
    if f["renalFindings"]:
        add("kidney_function", "adult chronic kidney disease evaluation clinical practice guideline",
            ["kidney_function_evaluation"], ["Q15"], "recent_verified_egfr_or_uacr_abnormality_not_ckd_diagnosis")
    if f["barrierFact"]:
        add("intestinal_barrier", "intestinal permeability human clinical evidence",
            ["intestinal_barrier_followup"],
            ["Q32", "Q33"], "accepted_current_barrier_abnormality_mechanism_and_attention")
        add("barrier_metabolism", "intestinal permeability human adipose inflammation metabolic liver evidence",
            ["metabolic_barrier_followup", "nafld", "obesity", "type2_diabetes"],
            ["Q34", "Q35"], "accepted_barrier_and_metabolic_context")
    # A finite search budget must first serve actual report directions. Keep
    # background and pending-data topics in the ledger even if not executed.
    return sorted(plan, key=lambda item: item["eligibility"] != "supported_direction")


def evidence_queries(assessment):
    return [item["query"] for item in evidence_plan(assessment)]


def _coverage(assessment, retrieval, selected_rows):
    """Full topic ledger: a successful broad search never covers other topics."""
    retrieval = retrieval if isinstance(retrieval, dict) else {"status": "not_requested"}
    queries = {row.get("text"): row for row in (retrieval.get("queries") or [])
               if isinstance(row, dict) and isinstance(row.get("text"), str)}
    candidates = [row for row in (retrieval.get("candidates") or []) if isinstance(row, dict)]
    selected_references = {row["code"]: (row.get("qualitative") or {}).get("evidenceIds", []) for row in selected_rows}
    coverage = []
    for item in evidence_plan(assessment):
        queried = queries.get(item["query"], {})
        matched = [row for row in candidates if queried.get("id") and isinstance(row.get("queryIds"), list)
                   and queried["id"] in row["queryIds"]]
        refs = item["evidenceIds"]
        candidate_reviews = []
        for row in matched:
            identities = [key for key in refs if _reference_identity_match(row, _reference(key))]
            applicable_ids = [key for key in identities if item["eligibility"] == "supported_direction"
                              and any(key in selected_references.get(code, []) for code in item["diseaseCodes"])]
            applicable = bool(applicable_ids)
            candidate_reviews.append({
                "candidateId": row.get("id"), "retrievalState": "retained_candidate",
                "citationState": "matched_registered_reference" if identities else "identity_not_verified",
                "matchedReferenceIds": identities,
                "applicableReferenceIds": applicable_ids,
                "applicabilityState": "registered_scope_matches_accepted_fact" if applicable else "not_established_for_individual_direction",
                "patientRule": item["reason"], "diseaseCodes": deepcopy(item["diseaseCodes"]),
                "adoptionState": "corroborates_registered_reference_only" if applicable else "not_adopted",
                "appliedTo": ["registered_reference_identity"] if applicable else [],
                "affectsDiseaseSelection": False, "affectsScore": False,
                "limitations": ["retrieval_metadata_is_not_full_text_review", "no_new_remote_recommendation_or_coefficient_applied"],
            })
        retrieved_refs = [key for key in refs if any(key in row["matchedReferenceIds"] for row in candidate_reviews)]
        fallback_status = ("not_requested" if retrieval.get("status") == "not_requested" else
                           "unavailable" if retrieval.get("status") in {"unavailable", "failed", "timeout"} else "not_attempted")
        status = queried.get("status") or fallback_status
        if status == "success" and not matched:
            status = "no_retained_candidates"
        coverage.append({**deepcopy(item), "retrievalStatus": status,
                         "candidateIds": [row["id"] for row in matched if row.get("id")],
                         "reviewedRemoteReferenceIds": retrieved_refs, "localReferenceIds": refs,
                         "candidateReviews": candidate_reviews,
                         "directionBasis": "registered_patient_rule" if item["diseaseCodes"] else "context_only",
                         "scoreBasis": "accepted_patient_facts_only_not_retrieval",
                         "basisSource": "reviewed_reference_retrieved" if retrieved_refs else "reviewed_reference_library" if refs else "none"})
    return coverage


def _reference_identity_match(candidate, reference):
    """Identifiers confirm bibliographic identity, never clinical adoption.

    A title resemblance or candidate-supplied score/recommendation is not an
    approved reference. Conflicting supplied DOI must not be hidden by a URL.
    """
    left, right = candidate.get("doi"), reference.get("doi")
    if isinstance(left, str) and isinstance(right, str) and left.strip() and right.strip():
        return left.strip().casefold() == right.strip().casefold()
    left, right = candidate.get("url"), reference.get("url")
    if isinstance(left, str) and isinstance(right, str) and left.rstrip("/") == right.rstrip("/"):
        return True
    pmid = candidate.get("pmid")
    return (isinstance(pmid, str) and pmid.isdigit()
            and str(reference.get("url") or "").rstrip("/") == "https://pubmed.ncbi.nlm.nih.gov/" + pmid)


def attention_score(facts, *, guideline_support):
    """Pure deterministic management rubric. NOT medically validated.

    Facts are server-produced domains, not raw request/model dictionaries.
    Same-domain duplicates don't raise either strength or corroboration counts.
    """
    domains = {}
    for fact in facts:
        if fact["domain"] not in domains or fact["strength"] > domains[fact["domain"]]["strength"]:
            domains[fact["domain"]] = fact
    # Legacy argument remains an explicit scope-eligibility gate, NOT a weight.
    # Keep the component name for stored audit readers, with typed empty value.
    if not domains or guideline_support not in {1, 2, True}:
        return None
    strongest = max(fact["strength"] for fact in domains.values())
    components = {"dataStrength": strongest, "independentFactors": min(2, len(domains) - 1),
                  "longitudinalSupport": min(2, max(fact.get("longitudinal", 0) for fact in domains.values())),
                  "guidanceApplicability": None}
    cap = 10
    score = min(cap, sum(value for value in components.values() if value is not None))
    return {"version": VERSION, "score": score, "components": components, "cap": cap,
            "label": "基础关注" if score <= 3 else "加强关注" if score <= 6 else "优先评估",
            "facts": deepcopy(list(domains.values())), "validated": False,
            "purpose": "followup_attention_not_probability_or_triage"}


def _fact(domain, *, strength=2, longitudinal=0, source="objective_related", location):
    return {"domain": domain, "strength": strength, "longitudinal": longitudinal,
            "sourceType": source, "location": location}


def _reference(identifier):
    if identifier in EVIDENCE:
        return _selected_reference(identifier)
    library = (FOLLOWUP_REFERENCES if identifier in FOLLOWUP_REFERENCES else
               BARRIER_REFERENCES if identifier in BARRIER_REFERENCES else
               CATALOG_MECHANISM_REFERENCES if identifier in CATALOG_MECHANISM_REFERENCES else _EXTRA_REFERENCES)
    ref = deepcopy(library[identifier])
    ref["citation"] = f"[评估{identifier[1:]}] " + ref["citation"]
    return ref


def _public_reference(identifier):
    reference = _reference(identifier)
    # The library's location describes internal evidence applicability, not a
    # bibliographic page locator. Keep it in appliedEvidence.scope only.
    reference['location'] = None
    return reference


def apply_dynamic_assessment(assessment, retrieval=None):
    """Select supported directions, never fill the historical template catalog.

    The catalog supplies reviewed wording for some directions, not a row-count
    requirement. Objective clinical findings can support additional directions.
    Evaluated registered-model rows remain authoritative, including valid zero.
    """
    # Rebuild the candidate seed, not the displayed rows. This also makes a
    # repeated internal evaluation safe after new non-template directions have
    # been selected. Preserve any existing registered-model results verbatim.
    seed = deepcopy(assessment)
    previous = {row["code"]: row for row in seed.get("diseaseRisks", [])}
    catalog, _ = build_disease_risks()
    catalog_mechanisms = {row["code"]: row["mechanism"] for row in catalog}
    seed["diseaseRisks"] = [deepcopy(previous.get(row["code"], row)) for row in catalog]
    output = apply_qualitative_assessment(seed, retrieval)
    f = _signals(output)
    eligible = eligible_codes(f)
    old_rows = {row["code"]: row for row in output.get("diseaseRisks", [])}
    old_rows.update({code: row for code, row in previous.items()
                     if code not in old_rows and row.get("status") == "evaluated"})
    selected = []
    body = _fact("body", location="context.bmi / objective body measurements",
                 source="objective_related", strength=3)
    lipids = None
    if f["lipids"]:
        current = any(row.get("temporalStatus") == "current" for row in f["lipids"])
        # Longitudinal scoring requires repeated same-analyte dates; never
        # count TC and LDL-C from one draw as two independent patient factors.
        dates = {}
        for row in f["lipids"]:
            date = str(row.get("date") or row.get("checkTime") or "")[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                # Different units or absent units cannot establish comparable
                # repeated laboratory observations for extra score credit.
                if row.get("unit"):
                    dates.setdefault((row["code"], row["unit"]), set()).add(date)
        repeated = any(len(values) >= 2 for values in dates.values())
        lipids = _fact("lipids", strength=4 if current else 3, longitudinal=2 if repeated else 0,
                       source="objective_current" if current else "objective_historical",
                       location="context.clinicalFindings accepted elevated LDL_C/TOTAL_CHOLESTEROL")

    def add(code, facts, *, name=None, basis=None, action=None, refs=None,
            qualitative_only=False,
            source_findings=None, record_reviews=None,
            followup_reason="documented_endoscopy_followup_without_validated_risk_index",
            system_code="digestive", system_name="消化系统"):
        if not facts or code not in eligible:
            return
        row = deepcopy(old_rows.get(code))
        if row is None:
            row = {"code": code, "systemCode": system_code, "systemName": system_name,
                   "name": name, "riskIndex": None, "riskLevel": None, "probability": None,
                   "status": "not_evaluated", "mechanism": None, "mechanismStatus": None,
                   "source": None, "reasons": []}
        if name:
            row["name"] = name
        finding = row.get("qualitative") or {}
        if refs is not None:
            finding = {"label": "鉴别方向", "basis": basis, "recommendation": action, "evidenceIds": refs}
        elif action is not None:
            finding["recommendation"] = action
        if not finding.get("evidenceIds"):
            return
        linked = bool(f["barrierFact"] and code in BARRIER_LINKED_CODES and not qualitative_only)
        if linked:
            facts = [*facts, deepcopy(f["barrierFact"])]
            finding["evidenceIds"] = list(dict.fromkeys([*finding["evidenceIds"], *BARRIER_LINKED_CODES[code]]))
            if code not in {"intestinal_barrier_followup", "metabolic_barrier_followup"}:
                finding["basis"] += f"本次已确认{f['barrierSummary']}，作为相关屏障功能关注因素。"
        finding["evidenceIds"] = list(dict.fromkeys([*finding["evidenceIds"], *catalog_mechanism_reference_ids(code)]))
        row["qualitative"] = finding
        if source_findings is not None:
            finding["sourceFindings"] = deepcopy(source_findings)
        if record_reviews is not None:
            finding["recordReviews"] = deepcopy(record_reviews)
        if row.get("status") != "evaluated" and qualitative_only:
            finding["label"] = "检查记录随访"
            row.update(riskIndex=None, riskLevel="随访关注", status="qualitative_followup",
                       probability=None, attention=None,
                       reasons=[followup_reason])
        elif row.get("status") != "evaluated":
            score = attention_score(facts, guideline_support=bool(finding["evidenceIds"]))
            row.update(riskIndex=score["score"], riskLevel=score["label"], status="attention_index",
                       probability=None, attention=score,
                       reasons=["custom_unvalidated_attention_index_not_disease_probability"])
        row["mechanism"] = patient_mechanism(code, barrier_linked=linked,
                                             catalog_mechanism=catalog_mechanisms.get(code))
        row["mechanismStatus"] = "individual_evidence_context_not_confirmed_diagnosis"
        selected.append(row)

    obese = f["bmi"] is not None and f["bmi"] >= 28
    if f["barrierFact"]:
        add("intestinal_barrier_followup", [f["barrierFact"]],
            name="肠道炎症与功能紊乱", refs=["Q32", "Q33"],
            basis=f"本次已确认{f['barrierSummary']}，提示需关注屏障功能及相关消化道表现。",
            action="结合腹痛、腹泻、便血及病程复核消化道情况；有持续症状或警示征时由医师选择针对性检查。")
        if obese or lipids:
            metabolic_facts = [f["barrierFact"]] + ([body] if obese else []) + ([lipids] if lipids else [])
            context_text = "、".join(label for present, label in (
                (obese, "体重相关代谢负荷"), (bool(lipids), "已确认血脂异常")) if present)
            add("metabolic_barrier_followup", metabolic_facts,
                name="糖脂代谢异常", system_code="metabolic", system_name="代谢系统",
                refs=["Q34", "Q35"],
                basis=f"本次已确认{f['barrierSummary']}，同时存在{context_text}，需关注屏障与代谢炎症的相关联系。",
                action="结合近期空腹血糖或HbA1c、血脂及体重腰围复核糖脂代谢状态。")
    if obese:
        for code in ("obesity", "nafld", "type2_diabetes", "hyperlipidemia", "hypertension"):
            add(code, [body] + ([lipids] if lipids and code == "hyperlipidemia" else []))
    elif lipids:
        add("hyperlipidemia", [lipids])
    if lipids:
        vascular_facts = [lipids] + ([body] if obese else [])
        add("atherosclerosis", vascular_facts, name="动脉粥样硬化性心血管病")
    for code, details in f["historicalFollowupFindings"].items():
        records = details["findings"]
        reviews = details.get("recordReviews", [])
        digestive = code == "digestive_lesion_followup"
        basis = "；".join(f"{item['date']} {item['itemName']}载有“{item['excerpt']}”" for item in records) + "。"
        if reviews:
            basis += "；".join(f"{item['date']} {item['excerpt']}" for item in reviews) + "。"
        refs = ["Q36"] if digestive else ["Q37"] + (["Q38"] if reviews else [])
        add(code, [_fact("imaging_record", source="objective_historical" if all(
                record["temporalStatus"] == "historical" for record in records) else "objective_current",
                location="context.clinicalFindings dated attributable imaging")],
            name="消化道占位性病变复核" if digestive else "泌尿系统结石及尿检复核" if reviews else "泌尿系统结石随访",
            system_code="digestive" if digestive else "urinary", system_name="消化系统" if digestive else "泌尿系统",
            qualitative_only=True, source_findings=records, record_reviews=reviews,
            followup_reason="dated_imaging_followup_without_validated_risk_index", refs=refs, basis=basis,
            action=("核对原始影像及后续消化专科、内镜或病理记录，由医生结合病变部位、大小和当前表现明确性质与复核安排。"
                    if digestive else "核对结石后续影像、大小与部位变化及当前症状，结合规范尿检复核泌尿系统状态；原检验缺失的单位及矛盾标记应先核对，不将异常自动归因为结石。"))
    if f["gastritisFindings"]:
        records = f["gastritisFindings"]
        latest = records[-1]
        historical = latest["temporalStatus"] == "historical"
        add("gastritis_followup", [_fact("gastric_endoscopy", source="objective_historical" if historical else "objective_current",
            location="context.clinicalFindings accepted dated hospital upper endoscopy")],
            name="既往胃炎随访" if historical else "胃炎检查记录随访", refs=["Q16", "Q17", "Q18"],
            qualitative_only=True,
            basis=f"{latest['date']} 胃镜记录为“{latest['excerpt']}”。本项依据该检查记录纳入随访，不据此判定目前炎症活动度或肠源性病因。",
            action="核对既往胃镜及病理、幽门螺杆菌检测与治疗史，补充目前上腹症状和用药情况；由消化专科结合资料决定是否进一步评估，不从该记录推定感染阳性或肿瘤风险。")
    for code, fact in f["diagnosisFollowupFindings"].items():
        rule = HISTORY_FOLLOWUP_RULES[code]
        add(code, [_fact("documented_diagnosis", source="objective_historical" if fact["temporalStatus"] == "historical" else "objective_current",
                        location="context.clinicalFindings dated hospital diagnosis document")],
            name=rule["name"] + "（诊断记录随访）", refs=list(rule["evidenceIds"]), qualitative_only=True,
            followup_reason="documented_diagnosis_followup_without_validated_risk_index",
            basis=f"{fact['date']} {fact['itemName']}载有“{fact['excerpt']}”。需结合当前症状及专科资料复核管理状态。",
            action=rule["action"])
    if f["bmi"] is not None and f["bmi"] >= 30:
        add("sleep_apnea", [body], name="阻塞性睡眠呼吸暂停（条件筛查）",
            system_code="sleep_respiratory", system_name="睡眠与呼吸系统", refs=["Q12"],
            basis=f"按现有体测资料计算 BMI {f['bmi']:.1f} kg/m²，肥胖是考虑睡眠呼吸暂停筛查的相关因素，需结合鼾声、憋醒或目击呼吸暂停等资料进一步评价。",
            action="复核体测并补问打鼾、夜间憋醒、日间嗜睡等，可由医师结合 STOP-BANG 等筛查工具决定是否进一步睡眠评估；睡眠时长和量表本身均不能确诊。")
    if f["renalFindings"]:
        renal = f["renalFindings"]
        findings = "、".join(dict.fromkeys(
            f"{item['date']} 的 {item['code']} {item['value']} {item['unit']}" for item in renal))
        fact = _fact("renal_laboratory", strength=4 if any(item["observationAgeDays"] == 0 for item in renal) else 3,
                     source="objective_current" if any(item["observationAgeDays"] == 0 for item in renal) else "objective_historical",
                     location="context.clinicalFindings verified recent hospital EGFR/UACR with exact value/unit/date")
        add("kidney_function_evaluation", [fact], name="肾功能或尿白蛋白异常（病因鉴别）",
            system_code="urinary", system_name="泌尿系统", refs=["Q15"],
            basis=f"近期检验记录显示{findings}，存在肾功能或尿白蛋白异常线索。单次异常不能确认慢性肾脏病，也不能由肠道屏障指标确定病因。",
            action="及时由医师复核肾功能、尿白蛋白/肌酐比与尿检，结合既往结果及用药、感染等情况区分急性变化和持续性异常；慢性病程需有至少3个月的证据，不应为确认慢性而延后当前异常的评估。")
    # Existing approved calculators are independent. Never silently discard or
    # rescale their values merely because this attention rubric lacks a rule.
    included = {row["code"] for row in selected}
    for row in old_rows.values():
        if row.get("status") == "evaluated" and row["code"] not in included:
            preserved = deepcopy(row)
            preserved["mechanism"] = patient_mechanism(row["code"],
                                                       catalog_mechanism=catalog_mechanisms.get(row["code"]) or row.get("mechanism"))
            finding = preserved.setdefault("qualitative", {})
            finding["evidenceIds"] = list(dict.fromkeys([*(finding.get("evidenceIds") or []),
                                                        *catalog_mechanism_reference_ids(row["code"])]))
            selected.append(preserved)
    evidence_ids = {key for row in selected for key in (row.get("qualitative") or {}).get("evidenceIds", [])}
    output["diseaseRisks"] = selected
    coverage = _coverage(assessment, retrieval, selected)
    output["diseaseAssessment"].update(
        mode=MODE, version=VERSION,
        references=[_public_reference(key) for key in sorted(evidence_ids)],
        appliedEvidence=[{"id": key, "url": _reference(key)["url"], "scope": _reference(key)["location"],
                          "reviewDate": "2026-10-06" if key in BARRIER_REFERENCES or key in FOLLOWUP_REFERENCES else "2026-10-01" if key in {"Q15", "Q23", "Q24", "Q25", "Q26", "Q27", "Q28", "Q29", "Q30", "Q31"} else "2026-09-28" if key in {"Q16", "Q17", "Q18", "Q19", "Q20", "Q21", "Q22"} else "2026-09-24",
                          "applicationSource": "registered_reference_library",
                          "applicationScope": "mechanism_association_not_risk_coefficient" if key in BARRIER_REFERENCES or key in CATALOG_MECHANISM_REFERENCES else "direction_and_followup_wording_only",
                          "numericalWeight": None} for key in sorted(evidence_ids)],
        selection={"candidateCodes": [row["code"] for row in selected],
                   "queries": evidence_queries(assessment), "topicPlan": evidence_plan(assessment),
                   "unmatchedSystemsNotExcluded": True},
        coverage=coverage,
        registry=coverage_ledger(f, selected, coverage),
        scoring={"validated": False, "scale": [0, 10], "purpose": "followup_attention_not_disease_prediction",
                 "version": VERSION, "rows": [{"code": row["code"], "attention": row.get("attention")}
                                               for row in selected]},
    )
    return output
