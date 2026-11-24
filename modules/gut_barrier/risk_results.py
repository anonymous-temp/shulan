"""Shared model precedence and grade names for scoring and publication."""
from __future__ import annotations

import math


RISK_LEVEL_NAMES = {"低": "低", "中": "中", "高": "高",
                    "low": "低", "medium": "中", "high": "高",
                    "低风险": "低", "中风险": "中", "高风险": "高"}


def model_result(row, assessment):
    """A display status alone must not promote an attention score to a model."""
    audit = assessment.get("riskModel")
    audit = audit if isinstance(audit, dict) else {}
    items = audit.get("items")
    items = items if isinstance(items, list) else []
    value = row.get("riskIndex")
    return (row.get("status") == "evaluated" and not row.get("reasons")
            and not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and 0 <= value <= 10
            and audit.get("status") in {"evaluated", "partial"}
            and bool(audit.get("modelId")) and bool(audit.get("modelVersion"))
            and any(item.get("code") == row.get("code") and item.get("status") == "evaluated"
                    and not item.get("reasons") for item in items
                    if isinstance(item, dict)))
