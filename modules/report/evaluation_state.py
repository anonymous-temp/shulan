"""Deterministic user-state assessments, separate from literal source grades.

Source grades and source scores retain their original wording and values.
Missing overview scores belong to the model composer, never a normal-item ratio.
"""
from __future__ import annotations

import math
import re
import unicodedata

from modules.report import evaluation_catalog as catalog
from modules.report.evaluation_scores import AI_SCORE_PREFIX, AI_LEVEL_PREFIX, clear_legacy_coverage_scores, source_module_level


def _text(value):
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", text):
        return None
    number = float(text)
    return number if math.isfinite(number) else None


def _reference_direction(item):
    """Compare a literal scalar with one simple printed range, in its unit."""
    number = _number(item.value)
    if number is None or not item.reference_range:
        return None
    numeric = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
    text = _text(item.reference_range)
    match = re.fullmatch(rf"({numeric})\s*(?:-|~|～|–|—|至)\s*({numeric})\s*([^\d;；]*)", text)
    if match:
        low, high, unit = float(match[1]), float(match[2]), match[3].strip()
        if low > high or unit and _text(unit) != _text(item.unit):
            return None
        return "偏低" if number < low else "偏高" if number > high else "正常"
    match = re.fullmatch(rf"(<=|>=|≤|≥|<|>)\s*({numeric})\s*([^\d;；]*)", text)
    if match:
        operator, boundary, unit = match[1], float(match[2]), match[3].strip()
        if unit and _text(unit) != _text(item.unit):
            return None
        within = {"<": number < boundary, "<=": number <= boundary, "≤": number <= boundary,
            ">": number > boundary, ">=": number >= boundary, "≥": number >= boundary}[operator]
        return "正常" if within else "偏低" if operator in {">", ">=", "≥"} else "偏高"
    return None


def _mapped_standard_level(item):
    """Read a source direction for relationships, never rewrite its grade."""
    code = item.code
    status = _text(item.source_status)
    direction = _reference_direction(item)
    low = status in {"偏低", "极低", "低", "弱", "较弱", "不足", "缺乏", "低于参考", "失衡", "重度失衡"}
    normal = status in {"正常", "健康", "健康倾向", "平衡", "正常范围内"}
    high = status in {"偏高", "高", "超标", "高于参考"}
    if code in {"gmwi2", "microbiomeHealthScore", "antiInflammatoryCapacity", "gutImmunity"}:
        if normal:
            return "正常", "原报告分级映射"
        if status in {"亚健康", "亚健康倾向"}:
            return "亚健康", "原报告分级映射"
        if status in {"不健康", "不健康倾向", "疾病风险", "高风险"}:
            return "疾病风险", "原报告分级映射"
        if low or direction == "偏低":
            return "亚健康", "原报告分级或参考范围映射"
        if direction == "正常":
            return "正常", "原报告参考范围对照"
        return None, None
    if code in {"speciesCount", "diversity", "beneficialBacteria"}:
        return (("偏低", "原报告分级或参考范围映射") if low or direction == "偏低" else
            ("正常", "原报告分级或参考范围映射") if normal or direction == "正常" else (None, None))
    if code in {"beRatio", "fbRatio"}:
        value = ("偏低" if low else "偏高" if high else "平衡" if normal else
            "偏低" if status == "偏向拟杆菌门" else "偏高" if status == "偏向厚壁菌门" else
            "平衡" if direction == "正常" else direction)
        return value, "原报告分级或参考范围映射" if value else None
    if code in {"pathogenicPotential", "pathogenicBacteria"}:
        value = "超标" if high or direction == "偏高" else "正常" if normal or direction == "正常" else None
        return value, "原报告分级或参考范围映射" if value else None
    if code == "dietaryFiberNeed":
        value = ("需求增加" if status in {"需求增加", "增加", "需求增高", "需求高"} or high or low or direction == "偏低"
            else "正常" if normal or direction == "正常" else None)
        return value, "原报告分级或参考范围映射" if value else None
    return None, None


def _standard_level(item):
    if item.assessment_level is not None and getattr(item, "_assessment_origin", None) == "report":
        return item.assessment_level, "report"
    if isinstance(item.source_status, str) and item.source_status.strip():
        return item.source_status, "source"
    if isinstance(item.value, str) and item.value in {
            "正常", "异常", "平衡", "失衡", "超标", "偏低", "偏高", "弱", "较弱", "强", "需求增加"}:
        return item.value, "source"
    if item.assessment_level is not None and getattr(item, "_assessment_origin", None) in {"source", "reference"}:
        return item.assessment_level, item._assessment_origin
    direction = _reference_direction(item)
    return direction, "reference" if direction else None


def _sources(items):
    result = []
    for item in items:
        for source in item.sources:
            if source not in result:
                result.append(source.model_copy(deep=True))
    return result


def _microbiome_patient_meanings(micro):
    """Describe supported relationships, rather than repeat grading metadata."""
    items = [*micro.overall_structure, *micro.health_metrics]

    def grade(code):
        grades = {_mapped_standard_level(item)[0] for item in items
            if item.code == code and item.value is not None and item.sources and _mapped_standard_level(item)[0]}
        return next(iter(grades)) if len(grades) == 1 else None

    shifted = grade("gmwi2") in {"亚健康", "疾病风险"}
    composition_shift = any(grade(code) in {"偏高", "偏低"} for code in ("beRatio", "fbRatio"))
    rich, diverse = grade("speciesCount") == "正常", grade("diversity") == "正常"
    anti, immune = grade("antiInflammatoryCapacity") == "正常", grade("gutImmunity") == "正常"
    meanings = {}
    if shifted and rich and diverse:
        meanings["gmwi2"] = "健康指数偏移与丰富度、多样性保留并存，整体菌群状态仍是本次评估的关注重点。"
    if shifted and anti and immune:
        meanings["microbiomeHealthScore"] = "整体关注较高，同时抗炎与免疫相关功能评价仍有保留，需要按不同维度分别安排重点。"
    if rich and composition_shift:
        meanings["speciesCount"] = "丰富度相对保留，可将后续结构观察重点放在已出现偏离的组成指标。"
    if diverse and composition_shift:
        meanings["diversity"] = "多样性维度保留，已测结构指标中的关注集中在菌群比值。"
    if grade("beRatio") == "平衡" and grade("fbRatio") in {"偏高", "偏低"}:
        meanings["beRatio"] = "B/E这一组成维度保留，F/B方向仍是结构观察的重点。"
        meanings["fbRatio"] = "本次已测组成比值中，偏离主要集中于F/B方向，应与B/E的保留状态分别观察。"
    # Health-table meanings are authored and independently reviewed later.
    # Keep the measured grade here without a deterministic prose substitute.
    mapping = re.compile(r"(?:原报告标记为“[^”]*”；)?原报告(?:分级映射|分级或参考范围映射|参考范围对照)为“[^”]*”。")
    for item in items:
        if item.code not in dict(catalog.MICROBIOME_OVERVIEW_DEFINITIONS):
            continue
        explanation = item.explanation or ""
        automatic = (not explanation or explanation == catalog.OVERALL_STRUCTURE_EXPLANATIONS.get(item.code)
            or mapping.fullmatch(explanation) or explanation.startswith("原始评分保留；本报告菌群综合评级依据：")
            or explanation in {"原报告该分类菌表的实际检出种数。", "原报告该分类菌表的实际检出种数。统一评级按该分类所列菌项的原报告分级汇总。"})
        if automatic:
            item.explanation = meanings.get(item.code) if item.value is not None and item.sources else None


def populate_state_assessments(data):
    """Populate existing fields only; original measurements/grades stay intact."""
    clear_legacy_coverage_scores(data)
    micro = data.microbiome_assessment
    standard_codes = dict(catalog.MICROBIOME_OVERVIEW_DEFINITIONS)
    for item in (*micro.overall_structure, *micro.health_metrics):
        if item.code not in standard_codes or not item.sources:
            continue
        level, origin = _standard_level(item)
        if level is not None:
            item.assessment_level = level
            item._assessment_origin = origin

    # A printed panel count supplies a count, not a new aggregate grade inferred
    # from individual species. Explicit source summary rows already have sources.
    for code, category in (("beneficialBacteria", "beneficial"), ("pathogenicBacteria", "pathogenic")):
        target = next((item for item in micro.health_metrics if item.code == code and not item.sources), None)
        panel = next((item for item in micro.panel_counts if item.code == category and item.detected_count is not None), None)
        if target is None or panel is None:
            continue
        target.value, target.unit = panel.detected_count, "种"
        target.sources, target.availability = _sources([panel]), "available"

    slots = {item.code: item for item in data.health_overview.assessment_chart}
    # A producing department is not a clinical priority. With no supplied
    # medical total, the independent overview assessor owns the qualitative
    # judgement; a lung calcification must not outrank a positive stool test
    # merely because it is an imaging finding.
    for code in ("medical", "nutrition"):
        slot = slots[code]
        authored = ((slot.explanation or "").startswith(AI_SCORE_PREFIX)
            or AI_LEVEL_PREFIX in (slot.explanation or ""))
        if slot.score is None and not authored:
            if slot.assessment_level in {"单项医学记录", "优先关注影像异常", "优先关注临床异常",
                    "有来源功能低项", "已有单项功能结果"}:
                slot.assessment_level = None
            slot.explanation = None
    nutrition = data.nutrition_assessment
    items = [*nutrition.major_metabolism, *nutrition.amino_acids_and_metabolites,
        *nutrition.neurotransmitters, *(item for group in nutrition.detailed_metabolism if group.code != "otherSourceFunctions" for item in group.items),
        *(item for group in nutrition.vitamins_and_minerals for item in group.items)]
    for item in items:
        if (isinstance(item.source_status, str) and item.source_status.strip()
                and getattr(item, "_assessment_origin", None) != "report"):
            item.assessment_level, item._assessment_origin = item.source_status, "source"
    micro_ai = ((slots["microbiome"].explanation or "").startswith(AI_SCORE_PREFIX)
        or AI_LEVEL_PREFIX in (slots["microbiome"].explanation or ""))
    if slots["microbiome"].score is not None and not micro_ai:
        slots["microbiome"].explanation = None
    if not micro_ai:
        slots["microbiome"].assessment_level = source_module_level(data, "microbiome")
    _microbiome_patient_meanings(micro)
