"""Guardrails and deterministic fallback prose for dimension explanations."""

from __future__ import annotations

import re
from typing import Any

from modules.report.schemas import HealthAssessment


def apply_assessment_explanations(
    assessment: HealthAssessment,
    explanations: list[dict[str, Any]],
) -> None:
    """Apply prose only; deterministic scores and missing-data facts stay immutable."""

    by_code = {
        str(item.get("code") or ""): str(item.get("explanation") or "").strip()
        for item in explanations
        if isinstance(item, dict)
    }
    forbidden = (
        "AI",
        "JSON",
        "疾病概率",
        "诊断概率",
        "患病概率",
        "预后概率",
        "用户上传",
        "PDF",
        "文件名",
        "解析过程",
        "后台处理",
    )
    for dimension in assessment.dimensions:
        text = by_code.get(dimension.code, "")
        if not text or len(text) > 800 or any(token in text for token in forbidden):
            continue
        # Missing-data prose may be polished, but it must still explicitly say
        # that the dimension is not scored and retain every missing item.
        if not dimension.score_available:
            if "暂不评分" not in text and "暂不能评估" not in text:
                continue
            if any(item not in text for item in dimension.missing_evidence):
                continue
            if re.search(r"\d{1,3}\s*分", text):
                continue
        else:
            # Score and level already have dedicated fields in the card.
            # Explanations own only the health meaning and must not repeat the
            # values displayed immediately above them.
            if re.search(r"\d{1,3}\s*分", text):
                continue
            if dimension.level_label in text or "本维度" in text:
                continue
            if re.search(r"(?<![A-Za-z])\d+(?:\.\d+)?", text):
                continue
            if any(
                phrase in text
                for phrase in (
                    "相关风险需要持续管理",
                    "相关风险需要优先关注",
                    "相关风险目前相对稳定",
                    "相关健康风险需要持续管理",
                    "相关健康风险需要优先关注",
                    "相关健康风险目前相对稳定",
                )
            ):
                continue
            normalized_text = re.sub(r"\s+", "", text).casefold()
            if any(
                re.sub(r"\s+", "", evidence.item).casefold() in normalized_text
                for evidence in dimension.evidence_used
                if evidence.item
            ):
                continue
        dimension.explanation = text


def apply_abnormal_indicator_summaries(
    assessment: HealthAssessment,
    summaries: list[dict[str, Any]],
) -> None:
    """Attach model prose to the matching abnormal indicator only.

    The indicator value, reference range, source status and severity remain
    deterministic.  Model output may explain clinical meaning, but it cannot
    turn an abnormal result into a diagnosis or add management instructions.
    """

    by_code = {
        str(item.get("indicatorCode") or item.get("indicator_code") or ""): str(
            item.get("clinicalSignificance")
            or item.get("clinical_significance")
            or ""
        ).strip()
        for item in summaries
        if isinstance(item, dict)
    }
    forbidden = (
        "AI",
        "JSON",
        "用户上传",
        "PDF",
        "图片",
        "文件名",
        "解析过程",
        "后台处理",
        "原报告列出",
        "原报告将",
        "已确诊",
        "诊断为",
        "已经患有",
        "建议检查",
        "建议复查",
        "建议治疗",
        "建议用药",
        "模型",
        "算法",
        "推断",
        "不等同于",
        "直接测得",
    )
    for indicator in assessment.abnormal_indicators:
        text = by_code.get(indicator.indicator_code, "")
        if (
            not text
            or len(text) > 800
            or any(token in text for token in forbidden)
        ):
            continue
        allowed_numbers = set(
            re.findall(
                r"(?<![A-Za-z])\d+(?:\.\d+)?",
                " ".join(
                    value
                    for value in (
                        indicator.value,
                        indicator.reference_range,
                    )
                    if value
                ),
            )
        )
        mentioned_numbers = set(
            re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", text)
        )
        if mentioned_numbers - allowed_numbers:
            continue
        if any(
            marker in text
            for marker in (
                "治疗方案",
                "用药方案",
                "手术",
                "服用",
                "剂量",
            )
        ):
            continue
        if re.search(
            r"提示(?:与)?[^，。；]{0,30}(?:功能|能力)(?:表现)?(?:需要|需)关注",
            text,
        ):
            continue
        indicator.clinical_significance = text
