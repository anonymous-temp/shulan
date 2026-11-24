"""XU editorial release facts, separate from the frozen injury scoring rules.

Section 1.1's three stages are display concepts, not a
new classifier. This server-owned projection is never request metadata.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation

from .overall_stage import assess_overall_stage

REVIEW_VERSION = "gut-barrier-xu-review-v2.20261010-barrier-only"
SOURCE = {
    "name": "肠道屏障功能与炎症状态评估报告（XU修改标注）.docx",
    "sha256": "8c35dbfa6ec3b82db518e5c27e6a5b0b9e42f8f0b58320901e9ce2b4af3e1047",
    "location": "1.1、1.2及批注0–3；三档名称为展示调整；2026-10-10起当前报告只展示肠道屏障三项",
}
OVERVIEW_GUIDANCE = {
    "normal": "维持合理膳食、规律活动及作息，按症状和既往病史安排复评。",
    "abnormal": ("围绕可干预诱因、黏膜修复所需营养及菌群生态三方面安排管理：先评估症状与营养摄入，保障可耐受的能量和蛋白质，"
                 "再按适用条件衔接微生态干预及生活方式调整。以症状、三项屏障指标和已有异常检验的复评结果观察效果。"),
    "age_specific": "结合年龄、生长发育及实际症状制定方案，不套用成人营养剂量和制剂用法。",
}


def overview_management_constraints(guidance):
    """Remove only the known generic lead-in, retaining every safety suffix."""
    if not isinstance(guidance, str):
        return ""
    # Age-specific guidance contains a dosing safety limit, not a generic lead-in.
    for key in ("normal", "abnormal"):
        prefix = OVERVIEW_GUIDANCE[key]
        if guidance.startswith(prefix):
            return guidance[len(prefix):].strip()
    return guidance.strip()  # Unknown/custom clinical advice must not disappear.


def word_overview_summary(assessment):
    """New publication only: read actual findings into a patient summary.

    Injury grades and single-marker findings are read, not recalculated. The
    independent three-stage color strip does not become a numeric classifier.
    A supplied panel alone cannot confirm intestinal origin or chronicity.
    """
    if not isinstance(assessment, dict) or not assessment.get("barrier"):
        return None
    barrier = assessment["barrier"]
    score = _number(barrier.get("score"))
    age = _number((assessment.get("subject") or {}).get("age"))
    if age is None or age < 18:
        return "综合本次已获得的检测信息，您的肠道屏障状态需结合年龄适用的参考范围、实际症状及生长发育情况进行评估。"
    if score is None:
        return "综合本次已获得的检测信息，屏障三项尚不完整或结果需核对，暂不能确定IBFC评分，需补充或复核检测资料后再评估。"
    readout = format(score.normalize(), "f")
    text = f"您当前的肠道屏障评分 IBFC 为 {readout} 分"
    if score == 0:
        text += "，屏障三项未见评分异常。"
    else:
        elevated = barrier_abnormal_names(assessment)
        if elevated:
            text += f"，伴有{'、'.join(elevated)}指标异常"
        grade = barrier_injury_grade(assessment)
        text += f"，损伤分级为 {grade}" if grade else "，损伤分级需进一步确认"
        text += "，建议围绕肠道屏障修复开展管理。"
    context = assessment.get("context") or {}
    if context.get("acuteInfection") is True or context.get("acuteInflammation") is True:
        return text + "已有急性感染或炎症背景，应先处理当前临床问题，稳定后再复评。"
    if score == 0:
        return text + "以维持肠道稳态和持续观察为主。"
    if score > 0:
        return text + "目前可围绕可干预因素开展管理，及时改善肠道屏障功能。"
    return text + "目前可围绕可干预因素开展管理，并结合症状及复查结果观察变化。"


def barrier_abnormal_names(assessment):
    """Names from accepted exact single-marker findings, without inference."""
    if not isinstance(assessment, dict):
        return []
    names = {"D_LACTATE": "D-乳酸", "DAO": "DAO", "LPS": "LPS"}
    elevated = []
    for marker in assessment.get("markers") or []:
        marker_score = _number(marker.get("score"))
        if (marker.get("code") in names and marker.get("status") == "evaluated"
                and marker.get("operator") == "=" and marker_score is not None and marker_score > 0):
            name = names[marker["code"]]
            if name not in elevated:
                elevated.append(name)
    return elevated


def barrier_injury_grade(assessment):
    """Return an already calculated injury grade; normal is not grade I."""
    if not isinstance(assessment, dict):
        return None
    barrier = assessment.get("barrier") or {}
    grade = barrier.get("grade")
    score = _number(barrier.get("score"))
    return f"{grade} 级" if score is not None and score > 0 and grade in {"I", "II", "III", "IV", "V"} else None


def barrier_score_description(assessment):
    """New v11 score-only readout; never infer a three-stage classification."""
    if not isinstance(assessment, dict) or not isinstance(assessment.get("barrier"), dict):
        return None
    barrier = assessment["barrier"]
    score = _number(barrier.get("score"))
    if score is None:
        return "屏障三项尚不完整或结果需核对，IBFC评分待补充或复核。"
    readout = format(score.normalize(), "f")
    text = f"肠道屏障功能评分（IBFC）为 {readout} 分"
    if score == 0:
        return text + "，屏障三项未见评分异常。"
    pattern = barrier.get("type")
    return text + (f"，{pattern}。" if isinstance(pattern, str) and pattern else "。")


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number >= 0 else None


def apply_review_release(assessment):
    """Return a new assessment; legacy score/grade/pattern remain untouched."""
    result = deepcopy(assessment)
    result["overallStage"] = assess_overall_stage(result)
    result["reportReview"] = {
        "version": REVIEW_VERSION, "source": deepcopy(SOURCE),
        "pendingRules": ["marker_subhealth_positive_mapping", "three_stage_numeric_thresholds_unconfirmed"],
    }
    for row in result.get("diseaseRisks", []):
        if row.get("code") == "nafld":
            row["name"] = "代谢相关脂肪性肝病"
        row["scoreKind"] = ("registered_model" if row.get("status") == "evaluated" else
                            "unvalidated_attention" if row.get("status") == "attention_index" else "unavailable")
    return result


def combined_text(assessment):
    barrier = assessment.get("barrier") or {}
    if barrier.get("score") is None:
        return "本次屏障评估尚需补充或核对检测结果；已获得的分项结果见下文，并结合症状及临床资料安排后续评估。"
    if barrier.get("grade") == "normal":
        return "本次屏障评分未见异常，以维持肠道稳态为主；既有疾病或持续症状仍需按其临床情况随访。"
    return "本次以屏障相关指标异常为主要检测发现。重点评估消化症状、可干预诱因与营养状态，并随访屏障指标变化。"
