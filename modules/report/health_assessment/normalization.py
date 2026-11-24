"""Normalize heterogeneous report facts without inventing missing values."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

SourceType = Literal[
    "hospital_data",
    "microbiome_data",
    "pdf_or_image",
    "session_context",
    "derived",
]


@dataclass(frozen=True)
class IndicatorFact:
    code: str
    name: str
    raw_value: str | None
    numeric_value: float | None
    unit: str | None
    date: str | None
    source: str
    source_type: SourceType
    priority: int
    order: int
    status: str | None = None
    reference_range: str | None = None


@dataclass(frozen=True)
class DiagnosisFact:
    code: str
    name: str
    source: str
    source_type: SourceType
    date: str | None
    control_poor: bool = False
    complication: bool = False


def compact_name(value: str) -> str:
    return re.sub(r"[\s_()（）\[\]【】/\\:：\-]+", "", value).casefold()


def numeric_value(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def normalize_date(value: object) -> str | None:
    if value is None:
        return None
    numbers = [int(item) for item in re.findall(r"\d+", str(value))]
    if len(numbers) >= 3:
        year, month, day = numbers[:3]
    elif len(numbers) == 1 and len(str(numbers[0])) == 8:
        raw = str(numbers[0])
        year, month, day = int(raw[:4]), int(raw[4:6]), int(raw[6:])
    else:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def date_rank(value: str | None) -> int:
    normalized = normalize_date(value)
    if not normalized:
        return 0
    return date.fromisoformat(normalized).toordinal()


def normalize_unit(
    code: str, value: float, unit: str | None
) -> tuple[float, str | None]:
    normalized = compact_name(unit or "")
    if code in {"height", "waist"} and normalized in {"cm", "厘米"}:
        return value, "cm"
    if code == "creatinine" and normalized in {"mgdl", "mg/dl"}:
        return value * 88.4, "μmol/L"
    if code == "uric_acid" and normalized in {"mgdl", "mg/dl"}:
        return value * 59.48, "μmol/L"
    if code in {"height", "waist"} and normalized in {"m", "米"}:
        return value * 100, "cm"
    if code == "weight" and normalized in {"kg", "千克", "公斤"}:
        return value, "kg"
    if code == "weight" and normalized in {"lb", "lbs", "磅"}:
        return value * 0.45359237, "kg"
    if code in {"hba1c", "body_fat"} and normalized in {"%", "百分比"}:
        return value, "%"
    if code in {
        "fasting_glucose",
        "ogtt_2h",
        "total_cholesterol",
        "triglyceride",
        "ldl_c",
        "hdl_c",
        "non_hdl_c",
    } and normalized in {"mgdl", "mg/dl"}:
        divisor = 88.57 if code in {"fasting_glucose", "ogtt_2h"} else 38.67
        if code == "triglyceride":
            divisor = 88.57
        return value / divisor, "mmol/L"
    if code in {
        "fasting_glucose",
        "ogtt_2h",
        "total_cholesterol",
        "triglyceride",
        "ldl_c",
        "hdl_c",
        "non_hdl_c",
    } and normalized in {"mmoll", "mmol/l"}:
        return value, "mmol/L"
    return value, unit


def parse_reference_range(value: str | None) -> tuple[float | None, float | None]:
    if not value:
        return None, None
    text = value.replace(" ", "")
    numbers = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", text)]
    if not numbers:
        return None, None
    if (
        any(token in text for token in ("<", "≤", "不高于", "小于"))
        and len(numbers) == 1
    ):
        return None, numbers[0]
    if (
        any(token in text for token in (">", "≥", "不低于", "大于"))
        and len(numbers) == 1
    ):
        return numbers[0], None
    if len(numbers) >= 2:
        return min(numbers[0], numbers[1]), max(numbers[0], numbers[1])
    return None, None
