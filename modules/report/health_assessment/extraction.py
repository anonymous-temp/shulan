"""Extract scoreable facts from the existing request and parsed report."""

from __future__ import annotations

import re
from typing import Any

from modules.report.health_assessment.normalization import (
    DiagnosisFact,
    IndicatorFact,
    compact_name,
    normalize_date,
    normalize_unit,
    numeric_value,
    parse_reference_range,
)
from modules.report.schemas import ParsedMicrobiomeReport, ReportRequest, SourceMetric

_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fasting_glucose", ("空腹血糖", "fpg", "fastingglucose")),
    ("hba1c", ("糖化血红蛋白", "hba1c", "a1c")),
    ("ogtt_2h", ("ogtt2h", "ogtt2小时", "糖负荷2小时血糖", "餐后2小时血糖")),
    ("total_cholesterol", ("总胆固醇", "tc")),
    ("triglyceride", ("甘油三酯", "tg")),
    ("ldl_c", ("低密度脂蛋白", "ldlc", "ldl")),
    ("hdl_c", ("高密度脂蛋白", "hdlc", "hdl")),
    ("non_hdl_c", ("非高密度脂蛋白", "nonhdlc")),
    ("systolic_bp", ("收缩压", "sbp")),
    ("diastolic_bp", ("舒张压", "dbp")),
    ("height", ("身高", "height")),
    ("weight", ("体重", "体质量", "weight")),
    ("waist", ("腰围", "waist")),
    ("body_fat", ("体脂率", "体脂百分比", "bodyfat")),
    ("visceral_fat", ("内脏脂肪", "visceralfat")),
    ("skeletal_muscle", ("骨骼肌", "肌肉量", "skeletalmuscle")),
    ("creatinine", ("血肌酐", "肌酐", "creatinine", "scr")),
    ("egfr", ("egfr", "估算肾小球滤过率", "肾小球滤过率")),
    ("uacr", ("uacr", "尿白蛋白肌酐比", "尿微量白蛋白肌酐比")),
    ("uric_acid", ("血尿酸", "尿酸", "uricacid", "ua")),
)

_DISPLAY_NAMES = {
    "fasting_glucose": "空腹血糖",
    "hba1c": "糖化血红蛋白",
    "ogtt_2h": "OGTT 2小时血糖",
    "total_cholesterol": "总胆固醇",
    "triglyceride": "甘油三酯",
    "ldl_c": "LDL-C",
    "hdl_c": "HDL-C",
    "non_hdl_c": "非HDL-C",
    "systolic_bp": "收缩压",
    "diastolic_bp": "舒张压",
    "height": "身高",
    "weight": "体重",
    "waist": "腰围",
    "body_fat": "体脂率",
    "visceral_fat": "内脏脂肪",
    "skeletal_muscle": "骨骼肌量",
    "creatinine": "血肌酐",
    "egfr": "eGFR",
    "uacr": "UACR",
    "uric_acid": "血尿酸",
    "smoking": "吸烟状态",
}


def extract_assessment_facts(
    request: ReportRequest,
    parsed: ParsedMicrobiomeReport,
    session_context: dict[str, Any],
) -> tuple[list[IndicatorFact], list[DiagnosisFact]]:
    facts: list[IndicatorFact] = []
    diagnoses: list[DiagnosisFact] = []
    for order, item in enumerate(request.hospital_data):
        code = _indicator_code(item.item_name)
        raw = item.test_result_value
        if (
            raw is not None
            and "血压" in item.item_name
            and re.search(r"\d{2,3}\s*[/／]\s*\d{2,3}", str(raw))
        ):
            facts.extend(
                _extract_vitals_from_text(
                    f"血压{raw}",
                    source=item.item_name,
                    source_type="hospital_data",
                    priority=1,
                    order=order,
                    date=item.report_time or item.check_time,
                )
            )
            raw = None
        if code and raw is not None:
            facts.append(
                _fact(
                    code=code,
                    raw=raw,
                    unit=_unit_from_text(item.item_name, str(raw)),
                    date=item.report_time or item.check_time,
                    source=item.item_name,
                    source_type="hospital_data",
                    priority=1,
                    order=order,
                    status=item.result_status_name,
                    reference_range=item.reference_range,
                )
            )
        if item.exam_result:
            if _contains_explicit_vital_measurement(item.exam_result):
                facts.extend(
                    _extract_vitals_from_text(
                        item.exam_result,
                        source=item.item_name,
                        source_type="hospital_data",
                        priority=1,
                        order=order,
                        date=item.report_time or item.check_time,
                    )
                )
            diagnoses.extend(
                _diagnoses_from_text(
                    f"{item.item_name} {item.exam_result}",
                    source=item.item_name,
                    source_type="hospital_data",
                    date=item.report_time or item.check_time,
                    require_confirmation=True,
                )
            )

    facts.extend(_microbiome_facts(parsed))
    session_text, structured = _flatten_session_context(session_context)
    session_order = len(facts) + 1000
    for key, value in structured:
        code = _indicator_code(key)
        if code and numeric_value(value) is not None:
            facts.append(
                _fact(
                    code=code,
                    raw=value,
                    unit=_unit_from_text(key, str(value)),
                    date=None,
                    source=f"健康问答：{key}",
                    source_type="session_context",
                    priority=4,
                    order=session_order,
                )
            )
            session_order += 1
    facts.extend(
        _extract_vitals_from_text(
            session_text,
            source="健康问答",
            source_type="session_context",
            priority=4,
            order=session_order,
            date=None,
        )
    )
    smoking = _smoking_fact(session_text, session_order + 50)
    if smoking:
        facts.append(smoking)
    diagnoses.extend(
        _diagnoses_from_text(
            session_text,
            source="用户确认的健康问答",
            source_type="session_context",
            date=None,
            require_confirmation=True,
        )
    )
    return _dedupe_facts(facts), _dedupe_diagnoses(diagnoses)


def _contains_explicit_vital_measurement(value: str) -> bool:
    """Reject chart axes/layout text before generic vital extraction."""

    compact = " ".join(value.split())
    if len(compact) > 300 or any(
        marker in compact
        for marker in (
            "人体成分测试历史记录",
            "肌肉脂肪分析",
            "细胞外水分比率分析",
            "根据理想体重",
        )
    ):
        return False
    return bool(
        re.search(
            r"(?:身高|体重|体质量|腰围|体脂率|体脂百分比|收缩压|舒张压)"
            r"\s*[:：]\s*\d",
            compact,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"血压\s*[:：]?\s*\d{2,3}\s*[/／]\s*\d{2,3}",
            compact,
            flags=re.IGNORECASE,
        )
    )


def extract_patient_evidence_facts(
    request: ReportRequest,
    parsed: ParsedMicrobiomeReport,
    session_context: dict[str, Any],
) -> tuple[list[IndicatorFact], list[DiagnosisFact], str]:
    """Expose normalized facts for evidence applicability without scoring.

    This intentionally reuses the assessment extractor so BMI/waist facts and
    the scoring engine cannot silently disagree about units or source
    precedence.  The flattened session text is returned for bounded extraction
    of symptoms, bowel habits, medication and diet context.
    """

    facts, diagnoses = extract_assessment_facts(request, parsed, session_context)
    session_text, _structured = _flatten_session_context(session_context)
    return facts, diagnoses, session_text


def _microbiome_facts(parsed: ParsedMicrobiomeReport) -> list[IndicatorFact]:
    source_type = (
        "pdf_or_image" if parsed.source_type in {"pdf", "image"} else "microbiome_data"
    )
    priority = 3 if source_type == "pdf_or_image" else 2
    date = parsed.sample.report_date or parsed.sample.sample_date
    result = [
        _metric_fact(metric, parsed.source_name, source_type, priority, index, date)
        for index, metric in enumerate(parsed.metrics)
    ]
    for index, snapshot in enumerate(parsed.source_history):
        if snapshot.selected:
            continue
        snapshot_type = (
            "pdf_or_image"
            if snapshot.source_type in {"pdf", "image"}
            else "microbiome_data"
        )
        snapshot_priority = 3 if snapshot_type == "pdf_or_image" else 2
        snapshot_date = snapshot.sample.report_date or snapshot.sample.sample_date
        result.extend(
            _metric_fact(
                metric,
                snapshot.source_name,
                snapshot_type,
                snapshot_priority,
                10000 + index * 100 + metric_index,
                snapshot_date,
            )
            for metric_index, metric in enumerate(snapshot.metrics)
        )
    for index, taxon in enumerate(parsed.taxa):
        if not taxon.source_status and not taxon.reference_range:
            continue
        result.append(
            IndicatorFact(
                code=f"microbe_composition:{taxon.taxon_id}",
                name=taxon.chinese_name or taxon.latin_name,
                raw_value=taxon.value,
                numeric_value=numeric_value(taxon.value),
                unit=taxon.unit,
                date=normalize_date(date),
                source=parsed.source_name,
                source_type=source_type,
                priority=priority,
                order=20000 + index,
                status=taxon.source_status,
                reference_range=taxon.reference_range,
            )
        )
    return result


def _metric_fact(
    metric: SourceMetric,
    source: str,
    source_type: str,
    priority: int,
    order: int,
    report_date: str | None,
) -> IndicatorFact:
    code = f"microbiome:{metric.code.casefold()}"
    name_text = compact_name(f"{metric.code}{metric.name}")
    if any(
        token in name_text
        for token in (
            "屏障",
            "barrier",
            "zonulin",
            "钙卫蛋白",
            "calprotectin",
        )
    ) or any(
        token in name_text
        for token in ("抗炎", "免疫力", "免疫调节", "mucosalinflammation")
    ):
        code = f"barrier_core:{metric.code.casefold()}"
    elif any(
        token in name_text
        for token in (
            "短链脂肪酸",
            "scfa",
            "丁酸",
            "乙酸",
            "丙酸",
            "甲酸",
            "戊酸",
            "二胺氧化酶",
            "dao",
            "d乳酸",
            "dlactate",
            "lps",
            "lbp",
            "内毒素",
            "endotoxin",
        )
    ):
        code = f"barrier_aux:{metric.code.casefold()}"
    elif metric.code.casefold().startswith("panel_"):
        code = f"microbe_composition:{metric.code.casefold()}"
    return IndicatorFact(
        code=code,
        name=metric.name,
        raw_value=metric.value,
        numeric_value=numeric_value(metric.value),
        unit=metric.unit,
        date=normalize_date(report_date),
        source=source,
        source_type=source_type,  # type: ignore[arg-type]
        priority=priority,
        order=order,
        status=metric.status,
        reference_range=metric.reference_range,
    )


def _indicator_code(name: str) -> str | None:
    compact = compact_name(name)
    # BMI is not a body mass measurement. The existing engine computes BMI
    # from validated height/weight; substring matching must not turn an index
    # into kg or create a spurious conflict with the actual weight.
    if any(term in compact for term in ("体重指数", "体质量指数", "身体质量指数", "bodymassindex", "bmi")):
        return None
    if len(compact) > 48 or any(
        marker in compact
        for marker in (
            "尿酸结晶",
            "人体成分测试历史记录",
            "肌肉脂肪分析",
            "细胞外水分比率",
            "根据理想体重",
        )
    ):
        return None
    if "crp" in compact or "hscr" in compact or "c反应蛋白" in compact:
        # ``hs-CRP`` contains the character sequence ``sCr`` but is not serum
        # creatinine. Short laboratory aliases must not override the explicit
        # C-reactive-protein name.
        return None
    # Longer, more specific aliases must win over short aliases such as TC/UA.
    matches = [
        (len(compact_name(alias)), code)
        for code, aliases in _ALIASES
        for alias in aliases
        if compact_name(alias) in compact
    ]
    if len({code for _length, code in matches}) > 1:
        return None
    return max(matches, default=(0, None))[1]


def _fact(
    *,
    code: str,
    raw: object,
    unit: str | None,
    date: object,
    source: str,
    source_type: str,
    priority: int,
    order: int,
    status: str | None = None,
    reference_range: str | None = None,
) -> IndicatorFact:
    number = numeric_value(raw)
    if number is not None and not unit:
        unit = _infer_unit(code, number, reference_range)
    if number is not None:
        number, unit = normalize_unit(code, number, unit)
    return IndicatorFact(
        code=code,
        name=_DISPLAY_NAMES.get(code, source),
        raw_value=str(raw).strip() if raw is not None else None,
        numeric_value=number,
        unit=unit,
        date=normalize_date(date),
        source=source,
        source_type=source_type,  # type: ignore[arg-type]
        priority=priority,
        order=order,
        status=status,
        reference_range=reference_range,
    )


def _infer_unit(
    code: str,
    value: float,
    reference_range: str | None,
) -> str | None:
    """Infer only units that are unambiguous from the indicator and range.

    Some hospital integrations omit the unit but retain the laboratory
    reference range.  We accept those common SI ranges so the source result is
    still scoreable; ambiguous values continue to use only the laboratory's
    own status/reference-range judgement.
    """

    if code in {"systolic_bp", "diastolic_bp"}:
        return "mmHg"
    if code == "hba1c" or code == "body_fat":
        return "%"
    if code == "egfr":
        return "mL/min/1.73m²"
    if code == "uacr":
        return "mg/g"
    if code == "weight":
        return "kg"
    if code in {"height", "waist"} and value > 3:
        return "cm"

    lower, upper = parse_reference_range(reference_range)
    range_max = (
        max(item for item in (lower, upper) if item is not None)
        if (lower is not None or upper is not None)
        else None
    )
    if range_max is None:
        return None
    if code in {"fasting_glucose", "ogtt_2h"}:
        return "mmol/L" if range_max <= 30 else "mg/dL"
    if code in {
        "total_cholesterol",
        "triglyceride",
        "ldl_c",
        "hdl_c",
        "non_hdl_c",
    }:
        return "mmol/L" if range_max <= 30 else "mg/dL"
    if code in {"creatinine", "uric_acid"}:
        return "μmol/L" if range_max > 20 else "mg/dL"
    return None


def _unit_from_text(name: str, value: str) -> str | None:
    text = f"{name} {value}"
    patterns = (
        (r"mmol\s*/\s*l", "mmol/L"),
        (r"mg\s*/\s*dl", "mg/dL"),
        (r"μmol\s*/\s*l|umol\s*/\s*l", "μmol/L"),
        (r"ml\s*/\s*min(?:\s*/\s*1\.73m2)?", "mL/min/1.73m²"),
        (r"mg\s*/\s*g", "mg/g"),
        (r"lbs?|磅", "lb"),
        (r"kg", "kg"),
        (r"cm", "cm"),
        (r"%", "%"),
        (r"mmhg", "mmHg"),
    )
    compact = text.casefold().replace(" ", "")
    for pattern, unit in patterns:
        if re.search(pattern, compact, flags=re.IGNORECASE):
            return unit
    return None


def _extract_vitals_from_text(
    text: str,
    *,
    source: str,
    source_type: str,
    priority: int,
    order: int,
    date: object,
) -> list[IndicatorFact]:
    results: list[IndicatorFact] = []
    patterns = (
        ("height", r"身高\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(cm|厘米|米|m)?"),
        (
            "weight",
            r"体(?:重|质量)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(kg|千克|公斤|lbs?|磅)?",
        ),
        ("waist", r"腰围\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(cm|厘米)?"),
        ("body_fat", r"体脂(?:率|百分比)?\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(%)?"),
    )
    for offset, (code, pattern) in enumerate(patterns):
        if match := re.search(pattern, text, flags=re.IGNORECASE):
            results.append(
                _fact(
                    code=code,
                    raw=match.group(1),
                    unit=match.group(2)
                    if match.lastindex and match.lastindex >= 2
                    else None,
                    date=date,
                    source=source,
                    source_type=source_type,
                    priority=priority,
                    order=order + offset,
                )
            )
    if match := re.search(
        r"(?:血压)?\s*(\d{2,3})\s*[/／]\s*(\d{2,3})\s*(?:mmhg)?",
        text,
        flags=re.IGNORECASE,
    ):
        results.extend(
            (
                _fact(
                    code="systolic_bp",
                    raw=match.group(1),
                    unit="mmHg",
                    date=date,
                    source=source,
                    source_type=source_type,
                    priority=priority,
                    order=order + 20,
                ),
                _fact(
                    code="diastolic_bp",
                    raw=match.group(2),
                    unit="mmHg",
                    date=date,
                    source=source,
                    source_type=source_type,
                    priority=priority,
                    order=order + 21,
                ),
            )
        )
    return results


def _flatten_session_context(value: Any) -> tuple[str, list[tuple[str, Any]]]:
    texts: list[str] = []
    pairs: list[tuple[str, Any]] = []

    def walk(item: Any, key: str = "") -> None:
        if isinstance(item, dict):
            for child_key, child in item.items():
                walk(child, str(child_key))
        elif isinstance(item, list):
            for child in item:
                walk(child, key)
        elif isinstance(item, (str, int, float, bool)):
            texts.append(f"{key}：{item}" if key else str(item))
            pairs.append((key, item))

    walk(value)
    return "\n".join(texts), pairs


def _smoking_fact(text: str, order: int) -> IndicatorFact | None:
    compact = text.replace(" ", "")
    if re.search(r"不吸烟|从不吸烟|无吸烟史", compact):
        value, status = "不吸烟", "never"
    elif re.search(r"已戒烟|戒烟", compact):
        value, status = "已戒烟", "former"
    elif re.search(r"经常吸烟|目前吸烟|每日吸烟|吸烟", compact):
        value, status = "目前吸烟", "current"
    else:
        return None
    return IndicatorFact(
        code="smoking",
        name="吸烟状态",
        raw_value=value,
        numeric_value=None,
        unit=None,
        date=None,
        source="健康问答",
        source_type="session_context",
        priority=4,
        order=order,
        status=status,
    )


_DIAGNOSES = (
    ("diabetes", "糖尿病", ("糖尿病", "diabetes")),
    ("hypertension", "高血压", ("高血压", "hypertension")),
    (
        "cardiovascular_disease",
        "心血管疾病",
        ("冠心病", "心肌梗死", "脑卒中", "心力衰竭"),
    ),
    ("obesity", "肥胖症", ("肥胖症",)),
    ("chronic_kidney_disease", "慢性肾脏病", ("慢性肾脏病", "慢性肾功能不全", "ckd")),
    ("gout", "痛风", ("痛风", "gout")),
    (
        "inflammatory_bowel_disease",
        "炎症性肠病",
        ("克罗恩病", "溃疡性结肠炎", "炎症性肠病"),
    ),
)


def _diagnoses_from_text(
    text: str,
    *,
    source: str,
    source_type: str,
    date: object,
    require_confirmation: bool,
) -> list[DiagnosisFact]:
    normalized = text.casefold()
    confirmed_context = bool(
        re.search(r"诊断|确诊|既往史|病史|患有|伴有|已患", normalized)
    )
    denied = bool(
        re.search(
            r"否认|未诊断|无[^\n。；;]{0,8}(?:病史|糖尿病|高血压|冠心病)", normalized
        )
    )
    if require_confirmation and (not confirmed_context or denied):
        return []
    poor = bool(re.search(r"控制不佳|未控制|达标不佳|持续异常|失控", normalized))
    complication = bool(re.search(r"并发症|并发|终末期|器官损害", normalized))
    results = []
    for code, name, aliases in _DIAGNOSES:
        if any(alias.casefold() in normalized for alias in aliases):
            results.append(
                DiagnosisFact(
                    code=code,
                    name=name,
                    source=source,
                    source_type=source_type,  # type: ignore[arg-type]
                    date=normalize_date(date),
                    control_poor=poor,
                    complication=complication,
                )
            )
    return results


def _dedupe_facts(items: list[IndicatorFact]) -> list[IndicatorFact]:
    output: list[IndicatorFact] = []
    seen = set()
    for item in items:
        key = (item.code, item.raw_value, item.date, item.source, item.status)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _dedupe_diagnoses(items: list[DiagnosisFact]) -> list[DiagnosisFact]:
    output: list[DiagnosisFact] = []
    seen = set()
    for item in items:
        key = (item.code, item.source, item.date)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output
