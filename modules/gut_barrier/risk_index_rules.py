"""Versioned, uncalibrated relative risk-factor rubric for the current report.

Weights are an explicit product rule, not coefficients taken from the mechanism
literature or a clinical probability model. Frozen reports never rerun it.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
from types import MappingProxyType

from .assessment import _lipid_status
from .dynamic_diseases import _signals
from .risk_model import build_disease_risks, SCORING_CODES
from .continuous_factors import build_continuous_factors
from .risk_grading import grade_items
from .risk_results import model_result
from .rules import LEVELS, grade_score, score_marker


VERSION = "gut-barrier-relative-risk-v2.7.20261010-clinical-source-fixes"
MARKER_MAXIMUM = {"D_LACTATE": 4, "DAO": 5, "LPS": 6}
# Trial grade factors, not disease probabilities or population percentiles.
IBFC_GRADE_SEVERITY = {
    "normal": Decimal("0"), "I": Decimal("0.2"), "II": Decimal("0.4"),
    "III": Decimal("0.6"), "IV": Decimal("0.8"), "V": Decimal("1"),
}
# The approved barrier coefficients remain unchanged when CRP is retired.
@dataclass(frozen=True)
class RiskProfile:
    """Fixed per-disease barrier coefficient for the current scoring rule."""

    barrier_weight: float

    def __post_init__(self):
        if (type(self.barrier_weight) not in {int, float}
                or not math.isfinite(self.barrier_weight) or not 0 <= self.barrier_weight <= 10):
            raise ValueError("barrier weight must be a finite number from 0 to 10")

    @property
    def weights(self):
        return (self.barrier_weight,)

    def audit(self):
        return {"selection": "fixed_disease_code", "barrierWeight": self.barrier_weight}


RISK_PROFILES = MappingProxyType({
    "ibd": RiskProfile(4),
    "intestinal_barrier_followup": RiskProfile(4),
    "nafld": RiskProfile(3),
    "ibs": RiskProfile(3),
    "cirrhosis": RiskProfile(1.5),
    "type2_diabetes": RiskProfile(2.2),
    "obesity": RiskProfile(2),
    "hyperlipidemia": RiskProfile(1.8),
    "hyperuricemia": RiskProfile(1.2),
    "atherosclerosis": RiskProfile(1),
    "coronary_heart_disease": RiskProfile(1),
    "stroke": RiskProfile(0.5),
    "hypertension": RiskProfile(0.5),
    "alzheimers": RiskProfile(0.5),
    "parkinsons": RiskProfile(0.5),
    "depression": RiskProfile(1),
    "anxiety": RiskProfile(1),
    "rheumatoid_arthritis": RiskProfile(1),
    "systemic_lupus": RiskProfile(1),
    "chronic_kidney_disease": RiskProfile(1),
    "liver_cancer": RiskProfile(0.8),
    "colorectal_cancer": RiskProfile(0.5),
    "lung_cancer": RiskProfile(0.3),
})

def risk_profile(code):
    """Select by stable disease code only; unknown codes remain unsupported."""
    return RISK_PROFILES.get(code) if isinstance(code, str) else None


_BMI_WEIGHTS = {"obesity": 2, "nafld": 1, "type2_diabetes": 1.25,
                "hyperlipidemia": .5, "hypertension": .5,
                "metabolic_barrier_followup": 1.25, "sleep_apnea": 1.5}
_LIPID_WEIGHTS = {"hyperlipidemia": 3, "atherosclerosis": 2,
                  "coronary_heart_disease": 2, "stroke": 1,
                  "nafld": 1, "metabolic_barrier_followup": 1}
def _number(value):
    return (Decimal(str(value)) if isinstance(value, (int, float))
            and not isinstance(value, bool) and math.isfinite(value) else None)


def risk_level(score):
    """Grade the displayed one-decimal index, so 6.65 rounds to 6.7/high."""
    value = _number(score)
    if value is None or not 1 <= value <= 10:
        return None
    value = value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return "低" if value < Decimal("3.4") else "中" if value < Decimal("6.7") else "高"


def _panel(assessment):
    markers = {m.get("code"): m for m in assessment.get("markers", []) if isinstance(m, dict)}
    normalized, reasons, inputs = {}, [], []
    for code, maximum in MARKER_MAXIMUM.items():
        marker = markers.get(code, {})
        score = _number(marker.get("score"))
        if (marker.get("status") != "evaluated" or marker.get("operator") != "="
                or _number(marker.get("value")) is None or score is None
                or not 0 <= score <= maximum or not marker.get("source")):
            reasons.append(f"{code}:measurement_requires_review")
            continue
        expected_score = score_marker(code, _number(marker["value"]))["score"]
        if expected_score is None or score != Decimal(str(expected_score)):
            reasons.append(f"{code}:measurement_requires_review")
            continue
        normalized[code] = score / Decimal(maximum)
        inputs.append({"code": code, "value": marker["value"], "unit": marker.get("unit"),
                       "normalizedSeverity": float(normalized[code]), "source": deepcopy(marker["source"])})
    context = assessment.get("context") or {}
    if context.get("acuteInfection") is True or context.get("acuteInflammation") is True:
        reasons.append("acute_context_requires_reassessment")
    return normalized, inputs, reasons


def _ibfc_basis(assessment, normalized):
    """Reconcile the derived IBFC with the three accepted Word-standard scores."""
    if not all(code in normalized for code in MARKER_MAXIMUM):
        return None, []
    markers = {m.get("code"): m for m in assessment["markers"] if isinstance(m, dict)}
    total = sum((_number(markers[code]["score"]) for code in MARKER_MAXIMUM), Decimal(0))
    grade, _ = grade_score(total)
    barrier = assessment.get("barrier") or {}
    if _number(barrier.get("score")) != total or barrier.get("grade") != grade:
        return None, ["IBFC:assessment_requires_review"]
    if grade not in IBFC_GRADE_SEVERITY:
        return None, ["IBFC:grade_requires_review"]
    return {"score": float(total), "grade": grade,
            "normalizedSeverity": float(IBFC_GRADE_SEVERITY[grade])}, []


def _latest_lipid_rows(assessment):
    """Use the same current-year batches for scoring and completeness."""
    context = assessment.get("context") or {}
    try:
        target = date.fromisoformat(context["currentDate"])
    except (ValueError, TypeError, KeyError):
        return []
    grouped = {}
    for row in context.get("clinicalFindings") or []:
        if not isinstance(row, dict) or row.get("code") not in {"LDL_C", "TOTAL_CHOLESTEROL"}:
            continue
        try:
            observed = date.fromisoformat(row["date"])
        except (ValueError, TypeError, KeyError):
            continue
        if (str(row.get("dataType") or "").upper() != "INSPECTION"
                or row.get("temporalStatus") not in {"current", "historical"}
                or not 0 <= (target - observed).days <= 365):
            continue
        grouped.setdefault(row["code"], []).append(row)
    return [[row for row in rows if row["date"] == max(other["date"] for other in rows)]
            for rows in grouped.values()]


def _lipid_conflict(rows):
    fingerprints = set()
    for row in rows:
        unit = row.get("unit")
        if (row.get("conflictingResults") or row.get("conflict")
                or str(row.get("status")) in {"conflict", "needs_review"}
                or (unit is not None and not isinstance(unit, str))):
            return True
        # Auxiliary source fields are untrusted JSON values. Never place an
        # unvalidated list/dict in a hash key or let it abort the full report.
        fingerprints.add((str(row.get("value")), unit, _lipid_status(row)))
    return len(fingerprints) != 1


def _lipid_observations(assessment):
    """Latest dated, unit-bearing result wins; old abnormalities cannot linger."""
    accepted = []
    for latest in _latest_lipid_rows(assessment):
        if _lipid_conflict(latest):
            continue
        row = latest[0]
        unit = str(row.get("unit") or "").lower().replace(" ", "")
        try:
            value = Decimal(str(row.get("value")))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if (row.get("conflictingResults") or row.get("conflict") or not value.is_finite()
                or value <= 0 or unit not in {"mmol/l", "mg/dl"}
                or not any(s.get("kind") == "hospitalData" for s in row.get("source") or [])):
            continue
        accepted.append(row)
    return accepted


def _lipid_factor(assessment):
    target = (assessment.get("context") or {}).get("currentDate")
    factors = []
    for row in _lipid_observations(assessment):
        if _lipid_status(row) != "high":
            continue
        factors.append({"domain": "lipids", "strength": 1 if row["date"] == target else .5,
                        "sourceType": "objective_current" if row["date"] == target else "objective_historical",
                        "source": deepcopy(row["source"]), "date": row["date"], "value": str(Decimal(str(row["value"]))), "unit": row["unit"]})
    return max(factors, key=lambda fact: fact["strength"]) if factors else None


def _factors(assessment, signals, code, lipid):
    """Each accepted domain contributes once; no raw symptom keyword scoring."""
    factors = []
    def add(domain, contribution, source_type, source):
        if contribution > 0:
            factors.append({"domain": domain, "contribution": float(contribution),
                            "sourceType": source_type, "source": deepcopy(source)})
    bmi = signals.get("bmi")
    if bmi is not None and bmi >= 28 and code in _BMI_WEIGHTS:
        severity = 2 if bmi >= 35 else 1.5 if bmi >= 32 else 1
        add("body", severity * _BMI_WEIGHTS[code],
            "objective_current",
            {"path": "context.bmi", "value": bmi})
    if lipid and code in _LIPID_WEIGHTS:
        add("lipids", lipid["strength"] * _LIPID_WEIGHTS[code], lipid["sourceType"], lipid)
    if code in {"chronic_kidney_disease", "kidney_function_evaluation"} and signals.get("renalFindings"):
        current = any(f.get("date") == (assessment.get("context") or {}).get("currentDate") for f in signals["renalFindings"])
        add("renal_observation", 3 if current else 1.5,
            "objective_current" if current else "objective_historical", signals["renalFindings"])
    diagnosis = (signals.get("diagnosisFollowupFindings") or {}).get(code)
    if diagnosis:
        add("condition_history", 3 if diagnosis.get("temporalStatus") == "current" else 1.5,
            "objective_current" if diagnosis.get("temporalStatus") == "current" else "objective_historical", diagnosis)
    history = (signals.get("historicalFollowupFindings") or {}).get(code)
    if history and history.get("findings"):
        current = any(f.get("temporalStatus") == "current" for f in history["findings"])
        add("structural_record", 2 if current else 1, "objective_current" if current else "objective_historical", history["findings"])
    if code == "gastritis_followup" and signals.get("gastritisFindings"):
        current = any(f.get("temporalStatus") == "current" for f in signals["gastritisFindings"])
        add("condition_history", 2 if current else 1, "objective_current" if current else "objective_historical", signals["gastritisFindings"])
    by_domain = {}
    for fact in factors:
        if fact["domain"] not in by_domain or fact["contribution"] > by_domain[fact["domain"]]["contribution"]:
            by_domain[fact["domain"]] = fact
    return list(by_domain.values())


def _clinical_completeness(assessment, signals, results, normalized, reasons):
    """Track available clinical domains without interpreting absence as negative.

    Completeness describes submitted information, not exclusion of every disease.
    Only hospital-supported laboratory and diagnosis domains are required.
    Independently supplied BMI may contribute to scoring, but its absence is
    not an incomplete domain because the current API has no objective body input.
    """
    lipid_rows = _lipid_observations(assessment)
    lipid_conflict = any(_lipid_conflict(rows) for rows in _latest_lipid_rows(assessment))
    lipid_known = any(_lipid_status(row) is not None for row in lipid_rows)
    diagnosis_status = "missing"
    if signals.get("diagnosisFollowupFindings") or signals.get("historicalFollowupFindings") or signals.get("gastritisFindings"):
        diagnosis_status = "available"
    domains = [
        {"code": "lipids", "status": "conflicting" if lipid_conflict else "available" if lipid_known else "missing"},
        {"code": "diagnosis", "status": diagnosis_status},
    ]
    missing = [domain["code"] for domain in domains if domain["status"] in {"missing", "conflicting"}]
    all_normal = not reasons and all(value == 0 for value in normalized.values())
    positive = any(item["factors"] for item in results) or bool(
        signals.get("diagnosisFollowupFindings") or signals.get("historicalFollowupFindings")
        or signals.get("gastritisFindings")) or any(
        model_result(row, assessment) for row in assessment.get("diseaseRisks", []) if isinstance(row, dict))
    return {"status": "incomplete" if missing else "complete", "domains": domains,
            "missingDomains": missing, "basicAttention": bool(all_normal and missing and not positive)}


def _organ_factors(values, barrier):
    d, dao, lps = (values["p_" + code] for code in ("D_LACTATE", "DAO", "LPS"))
    liver, renal = values["L"], values["R"]
    bmi, lipids = values["bmiStrength"], values["lipidStrength"]
    return {
        "ibd": d, "intestinal_barrier_followup": lps, "ibs": dao,
        "nafld": (dao + lps) / 2, "cirrhosis": max(d, dao, lps, liver),
        "type2_diabetes": barrier / 2 + Decimal("1.25") * bmi,
        "obesity": barrier / 2 + 2 * bmi,
        "hyperlipidemia": barrier / 2 + 3 * lipids + Decimal("0.3") * liver,
        "hyperuricemia": renal, "atherosclerosis": Decimal(0), "coronary_heart_disease": Decimal(0),
        "stroke": Decimal(0), "hypertension": Decimal(0),
        "alzheimers": barrier / 4,
        "parkinsons": barrier / 4,
        "depression": Decimal(0), "anxiety": Decimal(0),
        "rheumatoid_arthritis": Decimal(0), "systemic_lupus": Decimal(0),
        "chronic_kidney_disease": renal, "liver_cancer": liver, "colorectal_cancer": d,
        "lung_cancer": Decimal(0),
    }


def apply_risk_index_rules(assessment):
    """Compute the fixed v2.7 catalog from current facts; never reuse old scores."""
    output = deepcopy(assessment)
    normalized, inputs, reasons = _panel(output)
    ibfc_basis, ibfc_reasons = _ibfc_basis(output, normalized)
    reasons.extend(ibfc_reasons)
    age = _number((output.get("subject") or {}).get("age"))
    if age is None or not 18 <= age <= 100:
        reasons.append("adult_age_required")
    catalog, _ = build_disease_risks()
    rows = {row["code"]: row for row in catalog}
    signals = _signals(output)
    lipid = _lipid_factor(output) if not reasons else None
    values, factor_audit, organs = {}, {}, {}
    if not reasons:
        values, factor_audit = build_continuous_factors(output, bmi=signals.get("bmi"), lipid=lipid)
        b = IBFC_GRADE_SEVERITY[ibfc_basis["grade"]]
        organs = _organ_factors(values, b)
    results, raw_scores = [], {}
    for code in SCORING_CODES:
        profile = risk_profile(code)
        weights = profile.weights
        item = {"code": code, "systemCode": rows[code]["systemCode"],
                "status": "not_evaluated", "riskIndex": None, "riskLevel": None,
                "rawScore": None, "rankingScore": None, "baseRiskIndex": None, "baseRiskLevel": None,
                "profile": profile.audit(), "weights": list(weights),
                "components": None, "factors": [], "reasons": reasons[:]}
        if not reasons:
            barrier = b * Decimal(str(weights[0]))
            factors = _factors(output, signals, code, lipid)
            # Confirmed cross-channel policy: only domains already used by
            # this project's organ factor are removed from its clinical P.
            excluded = set()
            if code in {"type2_diabetes", "obesity"}:
                excluded.add("body")
            if code == "hyperlipidemia":
                excluded.add("lipids")
            excluded_factors = [fact for fact in factors if fact["domain"] in excluded]
            factors = [fact for fact in factors if fact["domain"] not in excluded]
            individual = min(Decimal(4), sum((Decimal(str(f["contribution"])) for f in factors), Decimal(0)))
            organ = organs[code]
            raw = 1 + barrier + 2 * organ + individual
            raw_scores[code] = raw
            item.update(status="evaluated", rawScore=float(raw), rankingScore=float(raw), baseRiskIndex=float(raw),
                        components={"baseline": 1, "barrier": float(barrier),
                                    "organFactor": float(organ), "organContribution": float(2 * organ),
                                    "individualFactors": float(individual)}, factors=factors,
                        excludedFactors=excluded_factors)
        results.append(item)
    allocation = grade_items(results, ibfc_basis["score"], raw_scores) if not reasons else None
    output["riskRuleAssessment"] = {
        "version": VERSION, "kind": "rule_based_relative_index", "validated": False,
        "status": "evaluated" if not reasons else "not_evaluated",
        "scale": {"minimum": 1, "maximum": 10, "precision": 1, "lowUpperExclusive": 3.4, "highLowerInclusive": 6.7},
        "baseFormula": "1+WB*B+2*organ_factor+min(4,domain_deduplicated_P)",
        "formula": "ibfc_point_counts_v2.7_three_marker_mapping_half_up_distinct_scores",
        "panelCoverage": {"available": len(normalized), "required": 3}, "inputs": inputs,
        "barrierBasis": ibfc_basis, "continuousFactors": factor_audit, "allocation": allocation,
        "clinicalCompleteness": _clinical_completeness(output, signals, results, normalized, reasons),
        "reasons": reasons, "notices": [], "items": results,
    }
    return output


def confirmed_rule_results(assessment):
    """Reconcile the ledger against accepted patient facts once per publication."""
    ledger = assessment.get("riskRuleAssessment") or {}
    if (ledger.get("version") != VERSION or ledger.get("kind") != "rule_based_relative_index"
            or ledger.get("validated") is not False or ledger.get("status") not in {"evaluated", "partial"}
            or ledger.get("panelCoverage") != {"available": 3, "required": 3}
            or ledger.get("reasons")):
        return {}
    expected = apply_risk_index_rules(assessment)["riskRuleAssessment"]
    if ledger != expected:
        return {}
    return {item["code"]: (item["riskIndex"], item["riskLevel"]) for item in ledger["items"]
            if item["status"] == "evaluated" and type(item["riskIndex"]) is float}


def confirmed_rule_result(assessment, code):
    """Single-entry compatibility helper for audit/validation consumers."""
    return confirmed_rule_results(assessment).get(code)
