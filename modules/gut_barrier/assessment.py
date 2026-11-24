"""Pure assessment functions; no network, product prescribing, or persistence."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from .rules import (
    MARKER_NAMES, MARKER_UNITS, PATTERNS, RULE_SOURCE, RULE_VERSION, PROJECT_REFERENCE_RANGES,
    decimal_value, grade_score, normalize_measurement_result, score_marker,
    is_retired_marker_name,
)
from .risk_model import build_disease_risks
from .clinical_context import hospital_safety_signals
from .continuous_factors import _normalized_gfr_value, clinical_factor_code
from .schemas import GutBarrierRequest
from .survey import normalize_health_survey


def _first(row: dict[str, Any], *keys: str) -> Any:
    return next((row[key] for key in keys if row.get(key) is not None), None)


def _marker_code(row: dict[str, Any]) -> str | None:
    aliases = {
        "dlactate": "D_LACTATE", "dlacticacid": "D_LACTATE", "d乳酸": "D_LACTATE",
        "dao": "DAO", "diamineoxidase": "DAO", "二胺氧化酶": "DAO",
        "lps": "LPS", "bacterialendotoxin": "LPS", "细菌内毒素": "LPS", "细菌脂多糖": "LPS", "脂多糖": "LPS",
    }
    for key in ("code", "name", "itemName", "item_name", "testName", "indicatorName"):
        compact = re.sub(r"[\s_‐‑–—－-]", "", str(row.get(key) or "")).casefold()
        if compact in aliases:
            return aliases[compact]
        # Accept common specimen prefixes / parenthesized aliases, not arbitrary
        # substring matches (e.g. hs-CRP/albumin is a ratio, not hs-CRP).
        base = re.sub(r"[（(][^()（）]{1,80}[）)]$", "", compact)
        base = re.sub(r"^(血清|血浆)", "", base)
        if base in aliases:
            return aliases[base]
    return None


def _date(value: Any) -> str | None:
    if isinstance(value, (datetime, date)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:日|\b|T)", value.strip())
    if not match:
        return None
    try:
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        return None


def _clinical_rows(value: Any, inherited_date: Any = None):
    """Yield named source findings without inferring diagnoses from their prose."""
    if isinstance(value, list):
        for child in value:
            yield from _clinical_rows(child, inherited_date)
    elif isinstance(value, dict):
        own_date = _first(value, "sampleDate", "date", "checkTime", "reportTime") or inherited_date
        if _first(value, "itemName", "item_name", "name") is not None:
            yield value, own_date
        for key in ("items", "results", "indicators", "data", "inspectionItems", "testItems", "details"):
            if key in value:
                yield from _clinical_rows(value[key], own_date)


def _lipid_code(name: Any) -> str | None:
    compact = re.sub(r"[\s_‐‑–—－-]", "", str(name or "")).casefold()
    base = re.sub(r"[（(][^()（）]{1,80}[）)]$", "", compact)
    aliases = {
        "ldl": "LDL_C", "ldlc": "LDL_C", "低密度脂蛋白": "LDL_C", "低密度脂蛋白胆固醇": "LDL_C",
        "lowdensitylipoproteincholesterol": "LDL_C",
        "tc": "TOTAL_CHOLESTEROL", "总胆固醇": "TOTAL_CHOLESTEROL",
        "totalcholesterol": "TOTAL_CHOLESTEROL",
    }
    return aliases.get(compact) or aliases.get(base)


def _clinical_findings(request: GutBarrierRequest, context: dict) -> list[dict]:
    target = _date(context.get("sampleDate")) or _date(context.get("currentDate")) or date.today().isoformat()
    grouped = {}
    for index, (row, raw_date) in enumerate(_clinical_rows(request.hospital_data)):
        name = _first(row, "itemName", "item_name", "name")
        if is_retired_marker_name(name) or is_retired_marker_name(row.get("code")):
            continue
        sample_date = _date(raw_date)
        if sample_date is None:
            temporal = "date_unknown"
        elif sample_date == target:
            temporal = "current"
        elif sample_date < target:
            temporal = "historical"
        else:
            temporal = "future_date_requires_review"
        finding = {
            "itemName": name, "code": _lipid_code(name),
            "dataType": _first(row, "dataType", "data_type"),
            "date": sample_date, "rawDate": raw_date, "temporalStatus": temporal,
            "value": _first(row, "testResultValue", "test_result_value", "value", "resultValue"),
            "unit": _first(row, "unit", "resultUnit", "testResultUnit"),
            "referenceRange": _first(row, "referenceRange", "reference_range"),
            "resultStatus": _first(row, "resultStatus", "result_status"),
            "resultStatusName": _first(row, "resultStatusName", "result_status_name"),
            "examResult": _first(row, "examResult", "exam_result", "conclusion"),
            "source": [{"kind": "hospitalData", "index": index}],
        }
        key = (finding["code"] or str(name).strip(), sample_date or str(raw_date), str(finding["dataType"]))
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = finding
            continue
        source = finding.pop("source")
        existing["source"].extend(source)
        comparable = ("value", "unit", "examResult", "resultStatus", "resultStatusName", "referenceRange")
        equivalent_gfr = False
        if clinical_factor_code(existing) == clinical_factor_code(finding) == "GFR":
            previous_gfr = _normalized_gfr_value(existing.get("value"), existing.get("unit"))
            incoming_gfr = _normalized_gfr_value(finding.get("value"), finding.get("unit"))
            equivalent_gfr = previous_gfr is not None and previous_gfr == incoming_gfr
            if equivalent_gfr:
                comparable = tuple(field for field in comparable if field not in {"value", "unit"})
        # Identical duplicated findings collapse; conflicting findings remain
        # visible but are never promoted to a risk signal by picking one side.
        if any(existing.get(field) != finding.get(field) for field in comparable):
            existing["conflictingResults"] = True
            existing.setdefault("alternatives", []).append({**finding, "source": source})
        elif equivalent_gfr and any(existing.get(field) != finding.get(field) for field in ("value", "unit")):
            # Preserve the original equivalent representation and its source;
            # only the comparison is normalized, never the received finding.
            existing.setdefault("alternatives", []).append({**finding, "source": source})
    return list(grouped.values())


def _lipid_status(finding: dict) -> str | None:
    """Normalize explicit laboratory flags consistently; numeric enums stay unknown."""
    status = finding.get("resultStatusName")
    if status is None or status == "":
        status = finding.get("resultStatus")
    if not isinstance(status, str):
        return None
    value = status.strip().casefold()
    if value in {"偏高", "升高", "高", "h", "high", "↑", "偏高(↑)", "偏高（↑）"}:
        return "high"
    if value in {"正常", "normal", "n"}:
        return "normal"
    if value in {"偏低", "降低", "低", "low", "l", "↓"}:
        return "low"
    return None


def _elevated_lipid_findings(context: dict) -> list[dict]:
    elevated = []
    findings = context.get("clinicalFindings")
    for finding in findings if isinstance(findings, list) else []:
        if not isinstance(finding, dict) or finding.get("conflictingResults"):
            continue
        if finding.get("code") not in {"LDL_C", "TOTAL_CHOLESTEROL"}:
            continue
        if finding.get("temporalStatus") not in {"current", "historical"}:
            continue
        if finding.get("unit") is not None and not isinstance(finding["unit"], str):
            continue
        # Do not assume a numeric status enum, or search for disease words in
        # negative/free-form narratives such as '未见升高' or '否认高脂血症'.
        if _lipid_status(finding) == "high":
            elevated.append(finding)
    return elevated


def _rows(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _rows(item)
    elif isinstance(value, dict):
        if _marker_code(value):
            yield value
        for key in ("items", "results", "indicators", "data", "inspectionItems", "testItems", "details"):
            if key in value:
                parent_date = _first(value, "sampleDate", "checkTime", "reportTime", "date")
                for child in _rows(value[key]):
                    inherited = dict(child)
                    if parent_date is not None and not any(inherited.get(k) is not None for k in ("sampleDate", "checkTime", "reportTime", "date")):
                        inherited["date"] = parent_date
                    if "isCurrent" not in inherited and "isCurrent" in value:
                        inherited["isCurrent"] = value["isCurrent"]
                    yield inherited


def _direct_records(request: GutBarrierRequest, context: dict) -> list[dict]:
    """Build current marker candidates from the dedicated request object only."""
    records = []
    direct_fields = (
        ("d_lactic_acid", "dLacticAcid", "D_LACTATE"),
        ("diamine_oxidase", "diamineOxidase", "DAO"),
        ("bacterial_endotoxin", "bacterialEndotoxin", "LPS"),
    )
    direct = request.gut_health_indicator
    if direct is not None:
        for field, public, code in direct_fields:
            marker = getattr(direct, field)
            if marker and marker.value is not None:
                records.append({"code": code, "value": marker.value, "unit": marker.unit, "date": context.get("sampleDate"), "isCurrent": True, "source": {"kind": "request", "path": f"gutHealthIndicator.{public}"}})
    return records


def _collect(request: GutBarrierRequest, observations: list[dict] | None, context: dict) -> list[dict]:
    records = _direct_records(request, context)
    for kind, data in (("hospitalData", request.hospital_data), ("observations", observations or [])):
        for index, row in enumerate(_rows(data)):
            if any(is_retired_marker_name(row.get(key)) for key in ("code", "name", "itemName", "item_name", "testName", "indicatorName")):
                continue
            records.append({
                "code": _marker_code(row),
                "value": _first(row, "value", "testResultValue", "test_result_value", "resultValue", "result"),
                "unit": _first(row, "unit", "resultUnit", "testResultUnit", "test_result_unit"),
                "date": _first(row, "sampleDate", "sample_date", "date", "checkTime", "check_time", "reportTime", "report_time"),
                "isCurrent": _first(row, "isCurrent", "is_current"),
                "source": row.get("source") or {"kind": kind, "index": index},
                "method": _first(row, "method", "assayMethod"),
                "referenceRange": _first(row, "referenceRange", "reference_range"),
            })
    return records


def _public_bounds(bounds: dict[str, Any]) -> dict[str, Any]:
    return {
        "lower": float(bounds["lower"]) if bounds.get("lower") is not None else None,
        "upper": float(bounds["upper"]) if bounds.get("upper") is not None else None,
        "lowerInclusive": bounds.get("lowerInclusive"),
        "upperInclusive": bounds.get("upperInclusive"),
    }


def _evaluate_marker(code: str, records: list[dict], context: dict) -> dict:
    result = {
        "code": code, "name": MARKER_NAMES[code], "value": None,
        "raw": None, "rawUnit": None, "displayValue": None,
        "operator": None, "bounds": None,
        "unit": MARKER_UNITS[code], "status": "missing", "score": None,
        "level": None, "abnormal": None, "source": [], "reasons": [],
        "projectReferenceRange": PROJECT_REFERENCE_RANGES[code],
        "laboratoryReferenceRanges": [],
        "method": None,
    }
    candidates = [record for record in records if record["code"] == code]
    if not candidates:
        return result
    target = _date(context.get("sampleDate")) or _date(context.get("currentDate")) or date.today().isoformat()
    declared_dates = {_date(record.get("date")) for record in candidates if record.get("isCurrent") is True and record.get("date") is not None}
    result["source"] = [dict(record) for record in candidates]
    if len(declared_dates - {None}) > 1:
        result.update(status="needs_review", reasons=["conflicting_current_sample_dates"])
        return result
    current = []
    for record in candidates:
        normalized_date = _date(record.get("date"))
        if normalized_date is not None and normalized_date != target:
            continue
        explicit_current = record.get("isCurrent") is True
        matches = normalized_date == target and record.get("isCurrent") is not False
        if explicit_current or matches:
            current.append(record)
    if not current:
        result.update(status="needs_review", reasons=["current_sample_not_confirmed"])
        return result
    # Lab-provided ranges remain separate from the project scoring thresholds.
    # Only named current marker records can supply their own reference metadata.
    result["laboratoryReferenceRanges"] = [
        {"value": record["referenceRange"].strip(), "source": deepcopy(record["source"])}
        for record in current
        if isinstance(record.get("referenceRange"), str) and record["referenceRange"].strip()
    ]
    errors, measurements = [], []
    for record in current:
        value, error = normalize_measurement_result(code, record["value"], record["unit"])
        if error:
            errors.append(error)
        else:
            measurements.append(value)
    dates = {_date(record.get("date")) for record in current if record.get("date") is not None}
    if None in dates:
        errors.append("invalid_sample_date")
    if len(dates - {None}) > 1:
        errors.append("conflicting_current_sample_dates")
    method_values = [record["method"] for record in current if record.get("method")]
    if any(not isinstance(method, str) for method in method_values):
        errors.append("invalid_assay_method")
    methods = {method.strip() for method in method_values if isinstance(method, str) and method.strip()}
    if len(methods) > 1:
        errors.append("conflicting_assay_methods")
    signatures = {
        (
            item["operator"], item["bounds"].get("lower"), item["bounds"].get("upper"),
            item["bounds"].get("lowerInclusive"), item["bounds"].get("upperInclusive"),
        )
        for item in measurements
    }
    if len(signatures) > 1:
        errors.append("conflicting_source_values")
    if errors:
        result.update(status="needs_review", reasons=sorted(set(errors)))
        return result
    result["method"] = next(iter(methods), None)
    measurement = measurements[0]
    if measurement["operator"] != "=":
        result.update(
            raw=measurement["raw"], rawUnit=measurement.get("rawUnit"),
            displayValue=measurement.get("displayValue"), operator=measurement["operator"],
            bounds=_public_bounds(measurement["bounds"]), status="needs_review",
            reasons=["inequality_result_exact_value_unknown"],
        )
        result["reasons"].append("barrier_score_requires_exact_value")
        return result
    value = measurement["value"]
    result.update(raw=measurement["raw"], rawUnit=measurement.get("rawUnit"),
                  displayValue=measurement.get("displayValue"), operator=measurement["operator"],
                  bounds=_public_bounds(measurement["bounds"]))
    result["value"] = float(value)
    result["status"] = "evaluated"
    result.update(score_marker(code, value))
    return result


def _barrier(markers: list[dict]) -> dict:
    barrier_markers = [marker for marker in markers if marker["code"] in MARKER_NAMES]
    result = {"score": None, "grade": None, "pattern": None, "patternCode": None, "type": None, "status": "incomplete", "reasons": []}
    if any(marker["status"] == "missing" for marker in barrier_markers):
        result["reasons"] = ["missing_barrier_markers"]
        return result
    if any(marker["score"] is None for marker in barrier_markers):
        result.update(status="needs_review", reasons=["barrier_measurement_requires_review"])
        return result
    total = sum((Decimal(str(marker["score"])) for marker in barrier_markers), Decimal("0"))
    grade, reasons = grade_score(total)
    result.update(score=float(total), grade=grade, status="evaluated", reasons=reasons)
    if all(marker["abnormal"] is not None for marker in barrier_markers):
        pattern = "".join("+" if marker["abnormal"] else "-" for marker in barrier_markers)
        result.update(pattern=pattern, patternCode=pattern.replace("+", "1").replace("-", "0"), type=PATTERNS[pattern])
    else:
        result["reasons"].append("subhealth_pattern_definition_pending")
    if result["reasons"]:
        result["status"] = "needs_review"
    return result


def _risks(request: GutBarrierRequest, markers: list[dict], context: dict) -> list[dict]:
    rows = []

    def add(code, name, axis, evaluation, basis, recommendations):
        rows.append({"code": code, "name": name, "axis": axis, "evaluation": evaluation, "basis": basis, "recommendations": recommendations, "riskIndex": None, "probability": None, "status": "qualitative_only"})

    abnormal = [marker["name"] for marker in markers if marker["abnormal"] is True]
    if abnormal:
        add("gastrointestinal", "消化道健康", "肠道屏障", "建议结合症状评估消化道原因", ["、".join(abnormal) + "相关指标异常"], ["结合真实病史及症状决定是否需要消化专科评估"])
    bmi = decimal_value(context.get("bmi"))
    adult_obesity = request.age is not None and request.age >= 18 and bmi is not None and bmi >= 28
    if adult_obesity:
        add("metabolic", "糖代谢及脂肪性肝病相关风险", "肠–代谢／肠–肝轴", "存在体脂相关危险因素", [f"已提供BMI {bmi} kg/m²"], ["结合饮酒、家族史及实际情况评估血糖、血脂和肝功能"])
    cardiovascular_basis = []
    lipid_findings = _elevated_lipid_findings(context)
    for finding in lipid_findings:
        prefix = "既往" if finding["temporalStatus"] == "historical" else "本次"
        measured = "" if finding.get("value") is None else f"，原值{finding['value']}"
        if measured and finding.get("unit"):
            measured += f" {finding['unit']}"
        cardiovascular_basis.append(f"{prefix}{finding['date']} {finding['itemName']}被原报告标记升高{measured}")
    if cardiovascular_basis:
        timepoints = {finding["temporalStatus"] for finding in lipid_findings}
        if timepoints == {"current"}:
            recommendation = "本次血脂存在升高记录；结合血压、血糖、吸烟及家族史评估总体心血管风险，并安排血脂随访"
        elif timepoints == {"historical"}:
            recommendation = "既往血脂异常记录需结合近期复查判断当前状态；同时评估血压、血糖、吸烟及家族史"
        else:
            recommendation = "本次与既往均有血脂升高记录；结合检测条件观察变化，并综合血压、血糖、吸烟及家族史评估总体风险"
        add("cardiovascular", "心血管健康", "传统心血管危险因素", "存在需进一步评估的危险因素记录", cardiovascular_basis, [recommendation])
    return rows


def assess_case(request: GutBarrierRequest, *, observations: list[dict] | None = None,
                context: dict | None = None,
                indicator_source: Literal["all_sources", "gutHealthIndicator"] = "all_sources") -> dict:
    """Assess supported observations without manufacturing missing clinical facts.

    The reusable domain evaluator can assess supplied source observations.
    The current report pipeline fixes indicator_source to gutHealthIndicator:
    hospital/uploaded markers remain provenance, never fallback measurements.
    This internal policy is not selectable by request or source metadata.

    External observations use code/name, value, unit, date/sampleDate, isCurrent,
    source, and optional method. In all_sources mode, historical/undated records
    require an explicit current-sample designation or matching context.sampleDate.
    """
    if indicator_source not in {"all_sources", "gutHealthIndicator"}:
        raise ValueError("unsupported indicator source")
    context = dict(context or {})
    survey = normalize_health_survey(request.health_survey, evaluation_date=context.get("currentDate"))
    # Questionnaire context is auxiliary; its body measurements stay in display rows.
    # Source-qualified clinical context wins over questionnaire self-report.
    # Questionnaire submit time never becomes a laboratory sample date.
    context = {**survey["context"], **context}
    if request.health_survey is not None:
        context["healthSurveyRows"] = survey["rows"]
        context["healthSurveyIssues"] = survey["issues"]
    clinical_findings = _clinical_findings(request, context)
    if clinical_findings:
        existing_findings = context.get("clinicalFindings", [])
        context["clinicalFindings"] = (list(existing_findings) if isinstance(existing_findings, list) else []) + clinical_findings
    if isinstance(context.get("clinicalFindings"), list):
        context["clinicalFindings"] = [row for row in context["clinicalFindings"]
                                       if not isinstance(row, dict) or not any(
                                           is_retired_marker_name(row.get(key)) for key in ("code", "itemName", "name"))]
    # Only source-qualified hospital findings become conservative safety holds;
    # they are not promoted to a diagnosis or a treatment indication.
    context["hospitalSafetySignals"] = hospital_safety_signals(context.get("clinicalFindings", []))
    hospital_signal_codes = {item["code"] for item in context["hospitalSafetySignals"]}
    if "acute_infection" in hospital_signal_codes:
        context["acuteInfection"] = True
    if "acute_inflammation" in hospital_signal_codes:
        context["acuteInflammation"] = True
    records = _collect(request, observations, context)
    marker_records = (_direct_records(request, context)
                      if indicator_source == "gutHealthIndicator" else records)
    markers = [_evaluate_marker(code, marker_records, context) for code in MARKER_NAMES]
    missing = [marker["code"] for marker in markers if marker["status"] == "missing"]
    missing.extend(key for key in ("recentInfection", "medications", "allergies", "immuneStatus", "liverAndKidneyFunction", "nutrientStatus") if key not in context or context[key] is None)
    for key, completeness in (("medications", "medicationHistoryComplete"), ("allergies", "allergyHistoryComplete")):
        if context.get(completeness) is False and key not in missing:
            missing.append(key)
    subject = {"userId": request.user_id, "sessionId": request.session_id, "name": request.name, "age": request.age, "sex": request.sex, "context": context}
    for key in ("symptoms", "bmi", "heightCm", "weightKg", "waistCm", "conditions", "medications", "enterotype", "habits", "clinicalFindings"):
        if key in context:
            subject[key] = context[key]
    disease_risks, risk_model = build_disease_risks()
    return {
        "subject": subject,
        "context": context,
        "markers": markers,
        "barrier": _barrier(markers),
        "risks": _risks(request, markers, context),
        "diseaseRisks": disease_risks,
        "riskModel": risk_model,
        "missingData": missing,
        "provenance": {"ruleSource": dict(RULE_SOURCE), "observations": records, "warnings": ["no_validated_disease_probability_model"]},
        "ruleVersion": RULE_VERSION,
    }
