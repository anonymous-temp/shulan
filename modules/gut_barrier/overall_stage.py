"""Display-only stage vocabulary, separate from IBFC injury grading.

The 2026-09-28 clarification scopes this change to the three names in the
section 1.1 graphic, not a new health/diagnosis classifier. Keep a traceable
label mapping and the existing injury facts without assigning a whole-person
health stage or adding a required clinical model to report completion.
"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from types import MappingProxyType
from typing import Any


OVERALL_STAGE_RULE_VERSION = "gut-barrier-overall-stage-display.20260928"
OVERALL_STAGE_DISPLAY_LABEL_MAPPING = MappingProxyType({
    "亚健康1级": "健康",
    "亚健康2级": "亚健康",
    "亚健康3级": "疾病",
})
OVERALL_STAGE_OPTIONS = tuple(
    (code, label + "人群") for code, label in zip(
        ("healthy", "subhealth", "disease"), OVERALL_STAGE_DISPLAY_LABEL_MAPPING.values(), strict=True,
    )
)
OVERALL_STAGE_SOURCE = MappingProxyType({
    "name": "肠道屏障功能与炎症状态评估报告（XU修改标注）.docx",
    "sha256": "8c35dbfa6ec3b82db518e5c27e6a5b0b9e42f8f0b58320901e9ce2b4af3e1047",
    "location": "1.1 综合评估指数及批注 1；2026-09-28 用户确认仅三档名称对应调整，不新增评分分界",
    "status": "display_label_mapping_confirmed",
})

_STATUSES = frozenset({"evaluated", "missing", "incomplete", "needs_review"})
_GRADES = frozenset({"normal", "I", "II", "III", "IV", "V"})


def _mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _known(value: Any, choices: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in choices else None


def _finite_number(value: Any) -> int | float | None:
    # Assessment outputs are already normalized numbers, not raw measurements.
    # In particular False and a bound such as ">3" must not become numeric 0/3.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def assess_overall_stage(assessment: Mapping[str, Any]) -> dict[str, Any]:
    """Return display metadata plus a small, JSON-safe internal fact record.

    Display-only status is independent of measurement completeness. Neither a
    normal IBFC nor a known disease assigns a whole-person health stage.
    Free-text and client-supplied ``overallStage``/``policy`` objects are ignored.
    This is an internal assessment structure, not a new public JSON contract.
    """
    source = _mapping(assessment)
    barrier = _mapping(source.get("barrier"))
    return {
        "code": None,
        "label": None,
        "status": "display_only",
        "reasons": [
            "ibfc_display_labels_only",
            "no_new_stage_thresholds_or_diagnosis",
        ],
        "scope": "ibfc_display",
        "ruleVersion": OVERALL_STAGE_RULE_VERSION,
        "source": dict(OVERALL_STAGE_SOURCE),
        "basis": {
            "barrierScore": _finite_number(barrier.get("score")),
            "barrierGrade": _known(barrier.get("grade"), _GRADES),
            "barrierStatus": _known(barrier.get("status"), _STATUSES),
        },
    }
