"""Supplied five-grade rules with explicitly confirmed score boundaries.

Single-marker bins include their lower endpoint; IBFC grades include their
upper endpoint. This source scale is not a validated disease probability.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Any

RULE_VERSION = "gut-barrier-2026-five-grade-v4.20261008-closed-boundaries"
RULE_SOURCE = {
    "name": "肠道屏障功能损伤分级标准（2026修订版）(1).docx",
    "sha256": "f108ec74f6cfe805d27e4469369c7d488e9cb75c60e438cfebbeebbba356133d",
    "location": "三项评分表及五级分级；2026-10-08确认单项下界含、上界不含，末档含下界；IBFC的2、4.5、6、8端点归前档",
    "status": "source_rule_with_confirmed_score_boundaries",
}
MARKER_NAMES = {"D_LACTATE": "D-乳酸", "DAO": "二胺氧化酶", "LPS": "细菌脂多糖"}
MARKER_UNITS = {"D_LACTATE": "mg/L", "DAO": "U/L", "LPS": "U/L"}
PROJECT_REFERENCE_RANGES = {
    "D_LACTATE": "＜14 mg/L", "DAO": "＜8.5 U/L", "LPS": "＜18 U/L",
}
BOUNDARIES = {
    "D_LACTATE": tuple(map(Decimal, ("14", "17", "22", "30"))),
    "DAO": tuple(map(Decimal, ("8.5", "11", "16", "20"))),
    "LPS": tuple(map(Decimal, ("18", "22", "28", "35"))),
}
SCORES = {
    "D_LACTATE": (0, 1, 2, 3, 4),
    "DAO": (0, 1, 2.5, 3.5, 5),
    "LPS": (0, 1.5, 3, 4.5, 6),
}
LEVELS = ("正常", "亚健康", "轻度", "中度", "重度")
# Confirmed upper endpoints belong to the preceding grade. The same
# definitions drive the calculation and the bands sent to the renderer.
_GRADE_DEFINITIONS = (
    ("I", Decimal("2"), "＞0–2分"),
    ("II", Decimal("4.5"), "＞2–4.5分"),
    ("III", Decimal("6"), "＞4.5–6分"),
    ("IV", Decimal("8"), "＞6–8分"),
    ("V", None, "＞8分"),
)
GRADE_BANDS = tuple((grade, label) for grade, _, label in _GRADE_DEFINITIONS)
PATTERNS = {
    "+--": "炎症性损伤相关组合",
    "-+-": "应激性损伤相关组合",
    "--+": "LPS单项异常（来源待判）",
    "++-": "炎症与应激相关组合",
    "+-+": "炎症性损伤及移位相关组合",
    "-++": "应激性损伤及移位相关组合",
    "+++": "混合型受损",
    "---": "三项未见异常",
}


def decimal_value(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    # Bound representation, not clinical plausibility; never serialize infinity.
    return parsed if parsed.is_finite() and 0 <= parsed <= Decimal("1e100") else None


def _measurement_factor(code: str, unit: Any) -> tuple[Decimal | None, str | None]:
    if not isinstance(unit, str):
        return None, "missing_unit"
    normalized = unit.strip().lower().replace(" ", "").replace("／", "/").replace("μ", "u").replace("µ", "u")
    if code == "D_LACTATE":
        factors = {"mg/l": "1", "mg/dl": "10", "ug/ml": "1", "ug/l": "0.001"}
    elif code == "DAO":
        factors = {"u/l": "1", "iu/l": "1"}
    else:
        # EU/mL and mass-based endotoxin results are assay-dependent, not aliases.
        factors = {"u/l": "1"}
    if normalized not in factors:
        return None, "unsupported_unit"
    return Decimal(factors[normalized]), None


_COMPARISON_VALUE = re.compile(
    r"^(?P<operator><=|>=|<|>|≤|≥)\s*"
    r"(?P<number>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)$"
)


def normalize_measurement_result(code: str, value: Any, unit: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Normalize an exact or one-sided result without collapsing inequalities."""
    factor, error = _measurement_factor(code, unit)
    if error:
        return None, error
    raw = str(value).strip() if isinstance(value, str) else str(value)
    comparison = _COMPARISON_VALUE.fullmatch(raw) if isinstance(value, str) else None
    if comparison:
        operator = {"≤": "<=", "≥": ">="}.get(comparison.group("operator"), comparison.group("operator"))
        parsed = decimal_value(comparison.group("number"))
    else:
        operator = "="
        parsed = decimal_value(value)
    if parsed is None:
        return None, "non_numeric_or_invalid_value"
    normalized = parsed * factor
    if operator == "<" and normalized == 0:
        return None, "empty_measurement_interval"
    normalized_text = format(normalized, "f")
    # Trim fractional zeros only: Decimal('10') must never display as '1'.
    if "." in normalized_text:
        normalized_text = normalized_text.rstrip("0").rstrip(".")
    display_value = raw if factor == 1 else f"{operator}{normalized_text}"
    if operator == "=":
        bounds = {
            "lower": normalized, "upper": normalized,
            "lowerInclusive": True, "upperInclusive": True,
        }
    elif operator in {">", ">="}:
        bounds = {
            "lower": normalized, "upper": None,
            "lowerInclusive": operator == ">=", "upperInclusive": None,
        }
    else:
        bounds = {
            "lower": Decimal("0"), "upper": normalized,
            "lowerInclusive": True, "upperInclusive": operator == "<=",
        }
    return {
        "raw": raw, "rawUnit": unit.strip() if isinstance(unit, str) else None,
        "displayValue": display_value, "operator": operator, "bounds": bounds,
        "value": normalized if operator == "=" else None,
    }, None


def score_marker(code: str, value: Decimal) -> dict[str, Any]:
    boundaries = BOUNDARIES[code]
    index = sum(value >= boundary for boundary in boundaries)
    return {
        "status": "evaluated", "score": SCORES[code][index], "level": LEVELS[index],
        # The document does not define whether subhealth counts as a '+' pattern.
        "abnormal": None if index == 1 else index > 1,
        "reasons": ["subhealth_pattern_definition_pending"] if index == 1 else [],
    }


def grade_score(score: Decimal) -> tuple[str | None, list[str]]:
    if score == 0:
        return "normal", []
    if score < 0:
        return None, ["score_not_covered_by_source_grade"]
    for grade, maximum, _ in _GRADE_DEFINITIONS:
        if maximum is None or score <= maximum:
            return grade, []


RETIRED_MARKER_PATTERN = (
    r"(?<![a-z])(?:(?:hs|high[\s_-]*sensitivity)[\s_-]*)?"
    r"(?:[cＣｃ][rＲｒ][pＰｐ]|[cＣｃ][\s-]*反[应應]蛋白|[cＣｃ][\s-]*reactive[\s-]*protein)(?![a-z])"
)


def is_retired_marker_name(name: Any) -> bool:
    """Identify retired CRP rows without interpreting their measurement values."""
    if not isinstance(name, str):
        return False
    normalized = re.sub(r"[‐‑–—－]", "-", name).casefold()
    return bool(re.search(RETIRED_MARKER_PATTERN, normalized, re.I))
