"""Pure internal factors for the supplied gut-barrier v2.7 scoring rubric.

These trial factors describe the supplied calculation, not validated disease
probabilities. No public request field, report text, or callback is added here.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import math
import re


ZERO = Decimal("0")
ONE = Decimal("1")
HALF = Decimal("0.5")
_BANDS = {
    "D_LACTATE": tuple(map(Decimal, ("14", "17", "22", "30"))),
    "DAO": tuple(map(Decimal, ("8.5", "11", "16", "20"))),
    "LPS": tuple(map(Decimal, ("18", "22", "28", "35"))),
}
LIVER_CODES = ("ALT", "AST", "ALP", "GGT", "TOTAL_BILIRUBIN", "DIRECT_BILIRUBIN", "ALBUMIN", "AG_RATIO")
KIDNEY_CODES = ("UREA", "CREATININE", "URIC_ACID", "GFR")
_ALIASES = {
    "ALT": ("alt", "gpt", "谷丙转氨酶", "丙氨酸氨基转移酶", "丙氨酸转氨酶", "丙氨酸氨基转移酶测定"),
    "AST": ("ast", "got", "谷草转氨酶", "天门冬氨酸氨基转移酶", "天门冬氨酸转氨酶", "天冬氨酸氨基转移酶"),
    "ALP": ("alp", "akp", "碱性磷酸酶"),
    "GGT": ("ggt", "γgt", "rgt", "γ谷氨酰转肽酶", "γ谷氨酰转移酶", "r谷氨酰转肽酶", "谷氨酰转肽酶", "谷氨酰转移酶", "γ谷氨酰基转移酶"),
    "TOTAL_BILIRUBIN": ("totalbilirubin", "tbil", "tb", "总胆红素"),
    "DIRECT_BILIRUBIN": ("directbilirubin", "dbil", "db", "直接胆红素", "结合胆红素"),
    "ALBUMIN": ("albumin", "alb", "白蛋白"),
    "AG_RATIO": ("agratio", "a/g", "ag", "白球比", "白/球比", "白蛋白/球蛋白", "白球比例", "白球比值", "白蛋白球蛋白比值", "白蛋白/球蛋白比值"),
    "UREA": ("urea", "尿素", "尿素氮", "bun", "ureanitrogen", "bloodureanitrogen"),
    "CREATININE": ("creatinine", "crea", "cr", "scr", "肌酐"),
    "URIC_ACID": ("uricacid", "ua", "尿酸"),
    "GFR": ("gfr", "egfr", "肾小球滤过率", "估算肾小球滤过率", "估计肾小球滤过率"),
    "LDL_C": ("ldlc", "ldl", "低密度脂蛋白", "低密度脂蛋白胆固醇", "lowdensitylipoproteincholesterol"),
    "TOTAL_CHOLESTEROL": ("totalcholesterol", "tc", "总胆固醇"),
}
_CODE_BY_ALIAS = {alias: code for code, aliases in _ALIASES.items() for alias in aliases}
_NUMERIC = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
_INTERVAL = re.compile(rf"^\s*[\[（(]?\s*({_NUMERIC})\s*(?:[-~～—–－]|至|,|，)\s*({_NUMERIC})\s*[\]）)]?\s*(?:[A-Za-zμµ%/²]+)?\s*$")


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _nonnegative(value):
    number = _decimal(value)
    if number is None or number < ZERO:
        raise ValueError("factor measurement must be a finite nonnegative exact value")
    return number


def marker_position(code, value):
    """Return Decimal position without rounding; upper tiers use confirmed p=1."""
    if code not in _BANDS:
        raise ValueError("unsupported barrier marker")
    number = _nonnegative(value)
    boundaries = _BANDS[code]
    if number < boundaries[0]:
        return number / boundaries[0] * Decimal("0.2")
    for lower, upper in zip(boundaries, boundaries[1:]):
        if number < upper:
            return (number - lower) / (upper - lower)
    return ONE


def reference_interval(reference):
    """Read one finite two-sided patient reference; never invent its width."""
    if isinstance(reference, str):
        match = _INTERVAL.fullmatch(reference)
        if not match:
            return None
        lower, upper = map(_decimal, match.groups())
    elif isinstance(reference, (list, tuple)) and len(reference) == 2:
        lower, upper = map(_decimal, reference)
    elif isinstance(reference, dict):
        lower, upper = _decimal(reference.get("lower")), _decimal(reference.get("upper"))
    else:
        return None
    return (lower, upper) if lower is not None and upper is not None and lower < upper else None


def reference_abnormality(value, reference):
    number, bounds = _decimal(value), reference_interval(reference)
    if number is None or number < ZERO or bounds is None:
        return None
    lower, upper = bounds
    deviation = max(lower - number, number - upper, ZERO)
    if deviation == ZERO:
        return ZERO
    return HALF if deviation < (upper - lower) / 2 else ONE


def gfr_abnormality(value):
    number = _nonnegative(value)
    return ZERO if number >= 90 else HALF if number >= 60 else Decimal("2")


def _normalized_gfr_value(value, unit):
    """Return indexed GFR in mL/min/1.73m² without assuming a missing unit."""
    number = _decimal(value)
    if number is None or number < ZERO or not isinstance(unit, str):
        return None
    normalized = re.sub(r"\s+", "", unit).casefold().replace("²", "2").replace("^2", "2").replace("·", "*")
    factors = {
        "ml/min/1.73m2": ONE, "ml/min/(1.73m2)": ONE, "ml/(min*1.73m2)": ONE,
        "ml/s/1.73m2": Decimal(60), "ml/s/(1.73m2)": Decimal(60), "ml/(s*1.73m2)": Decimal(60),
    }
    multiplier = factors.get(normalized)
    return number * multiplier if multiplier is not None else None


def bmi_strength(value):
    number = _decimal(value)
    if number is None or number < 28:
        return ZERO
    return ONE if number < 32 else Decimal("1.5") if number < 35 else Decimal("2")


def clinical_factor_code(row):
    """Recognize exact lab aliases for internal factors, never substring ratios."""
    for key in ("code", "itemName", "item_name", "name"):
        text = re.sub(r"[\s_‐‑–—－-]", "", str(row.get(key) or "")).casefold()
        base = re.sub(r"[（(][^()（）]{1,80}[）)]$", "", text)
        base = re.sub(r"^(血清|血浆)", "", base)
        found = _CODE_BY_ALIAS.get(text) or _CODE_BY_ALIAS.get(base)
        if found:
            return found
    return None


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:日|\b|T)", value.strip())
    try:
        return date(*map(int, match.groups())) if match else None
    except ValueError:
        return None


def _record_audit(row, reason=None):
    result = {key: deepcopy(row.get(key)) for key in
              ("itemName", "code", "date", "value", "unit", "referenceRange", "source")}
    if reason:
        result["reason"] = reason
    return result


def _json_value(value):
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _organ_factor(codes, findings, target):
    items = []
    total = ZERO
    for code in codes:
        candidates, excluded, conflict_dates = [], [], []
        for row in findings:
            if not isinstance(row, dict) or clinical_factor_code(row) != code:
                continue
            observed, value = _date(row.get("date")), _decimal(row.get("value"))
            reason = None
            if target is None:
                reason = "evaluation_date_missing"
            elif observed is None:
                reason = "measurement_date_missing"
            elif observed > target:
                reason = "measurement_after_evaluation"
            elif str(row.get("dataType") or "INSPECTION").upper() != "INSPECTION":
                reason = "not_laboratory_inspection"
            elif not row.get("source"):
                reason = "source_missing"
            elif row.get("conflictingResults") or row.get("conflict") or row.get("status") in {"conflict", "needs_review"}:
                reason = "conflicting_measurement"
                conflict_dates.append(observed)
            elif value is None or value < ZERO:
                reason = "exact_value_missing"
            elif code == "GFR":
                value = _normalized_gfr_value(value, row.get("unit"))
                if value is None:
                    reason = "gfr_unit_missing_or_unsupported"
            elif code != "GFR" and reference_interval(row.get("referenceRange")) is None:
                reason = "reference_interval_missing_or_invalid"
            if reason:
                excluded.append(_record_audit(row, reason))
                continue
            score = gfr_abnormality(value) if code == "GFR" else reference_abnormality(value, row.get("referenceRange"))
            candidates.append((observed, value, score, row))
        item = {"code": code, "score": 0.0, "status": "missing", "selected": None, "excluded": excluded}
        latest_conflict = max(conflict_dates, default=None)
        latest_valid = max((candidate[0] for candidate in candidates), default=None)
        if latest_conflict is not None and (latest_valid is None or latest_conflict >= latest_valid):
            # assessment may have already merged identical-name rows into one
            # flagged conflict, whereas aliases remain separate until here.
            # Neither representation may silently revive an older result.
            item.update(status="conflicting")
            item["excluded"].extend(_record_audit(row, "superseded_by_latest_conflict")
                                    for _, _, _, row in candidates)
            items.append(item)
            continue
        if candidates:
            latest_date = max(candidate[0] for candidate in candidates)
            latest = [candidate for candidate in candidates if candidate[0] == latest_date]
            fingerprints = {(value, "mL/min/1.73m²" if code == "GFR" else str(row.get("unit") or ""),
                             None if code == "GFR" else reference_interval(row.get("referenceRange")))
                            for _, value, _, row in latest}
            if len(fingerprints) > 1:
                item.update(status="conflicting", excluded=excluded + [
                    _record_audit(row, "same_date_conflicting_measurement") for _, _, _, row in latest])
            else:
                _, _, score, row = latest[0]
                item.update(status="accepted", score=float(score), selected=_record_audit(row))
                item["selected"]["source"] = [deepcopy(source) for _, _, _, record in latest
                                               for source in record.get("source") or []]
                total += score
            item["excluded"].extend(_record_audit(row, "older_valid_measurement")
                                    for observed, _, _, row in candidates if observed < latest_date)
        items.append(item)
    factor = min(ONE, total / 4)
    return factor, {"total": float(total), "factor": float(factor), "items": items}


def build_continuous_factors(assessment, *, bmi=None, lipid=None):
    """Return (Decimal values, JSON-safe internal audit) from normalized sources.

    ``bmi`` and ``lipid`` are the caller's already accepted clinical signals.
    In particular, ``lipid`` retains existing latest-result/conflict/provenance
    rules instead of implementing a second competing lipid acceptance policy.
    """
    context = assessment.get("context") or {}
    target = _date(context.get("sampleDate")) or _date(context.get("currentDate"))
    findings = context.get("clinicalFindings") or []
    findings = findings if isinstance(findings, list) else []
    markers = {row.get("code"): row for row in assessment.get("markers", []) if isinstance(row, dict)}
    values, positions = {}, []
    for code in _BANDS:
        row = markers.get(code, {})
        key = f"p_{code}"
        try:
            position = marker_position(code, row.get("value"))
            reason = "range_position_unknown" if row.get("operator") in {"<", "<=", ">", ">="} else None
        except ValueError:
            position, reason = ZERO, "exact_value_missing"
        values[key] = position
        positions.append({"code": code, "value": row.get("value"), "operator": row.get("operator"),
                          "position": float(position), "reason": reason, "source": deepcopy(row.get("source"))})
    values["L"], liver = _organ_factor(LIVER_CODES, findings, target)
    values["R"], kidney = _organ_factor(KIDNEY_CODES, findings, target)
    values["bmiStrength"] = bmi_strength(bmi)
    strength = _decimal((lipid or {}).get("strength"))
    values["lipidStrength"] = strength if strength in {HALF, ONE} else ZERO
    audit = {"evaluationDate": target.isoformat() if target else None,
             "positions": positions, "liver": liver, "kidney": kidney,
             "bmi": {"value": float(bmi) if _decimal(bmi) is not None else None,
                     "strength": float(values["bmiStrength"])},
             "lipids": {"strength": float(values["lipidStrength"]), "accepted": deepcopy(lipid)}}
    return values, _json_value(audit)
