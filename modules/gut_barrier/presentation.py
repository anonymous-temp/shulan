"""Build a patient-facing ReportDocument from assessed facts and reviewed plans.

This layer does not score observations, infer missing history, approve treatment,
or select commercial products. Its blocks contain plain text only. Assessed
clinical facts are copied; auxiliary source bodies are reduced to traceable
summaries rather than duplicated throughout the public report.
"""

from __future__ import annotations

from copy import deepcopy

from .exam_excerpt import exam_result_excerpt


SCHEMA_VERSION = "gut-barrier-report.v1"
_STATUS = {
    "evaluated": "已评估", "missing": "未提供", "needs_review": "需核对后评估",
    "incomplete": "资料不足", "not_indicated": "目前无适用依据",
    "not_needed": "本期不需安排", "not_recommended": "本期不建议采用",
    "not_started": "尚未启动", "deferred": "暂不启动", "pending_review": "待医师评估",
    "requires_review": "待医师评估", "conditional": "满足条件后可选",
    "conditional_candidate": "满足条件后可选", "candidate": "备选方案",
    "eligible": "符合已核对条件", "recommended": "建议实施", "routine": "基础管理",
    "active": "已启动", "approved": "已确认", "contraindicated": "不适用",
    "unavailable": "资料不足，暂不匹配", "not_selected": "本期未选择",
    "qualitative_only": "定性评估", "requires_clinical_review": "需临床复核",
    "conditional_specialist_review": "满足条件后由专科评估", "oral_nutrition_first": "优先饮食与口服营养",
    "not_assessable": "资料待完善", "catalog_unavailable": "产品资料待核对",
    "catalog_match_for_review": "产品候选待审核", "not_eligible_for_matching": "匹配资料不足",
    "no_exact_catalog_match": "暂无适用目录产品", "specialist_review_only": "仅供专科评估",
    "maintenance": "维持管理", "food_first": "食物来源优先",
    "check_deficiency_and_interactions": "先核对营养状态与用药",
    "adult_dose_withheld": "需按年龄另行评估", "check_25ohd_before_use": "先评估维生素 D 状态",
    "dietitian_review": "临床营养评估", "medical_nutrition_review": "医疗营养评估",
    "age_specific_review": "按年龄评估", "clinical_review": "临床评估",
    "planned": "建议复评", "priority_review": "优先复核", "safety_net": "及时就医提示",
    "nutrient_status_required": "需先评估营养状态",
    "not_indicated_by_nutrient_status": "营养状态充足，暂不补充",
    "documented_deficiency_needs_clinician_review": "已有不足依据，补充方案待审核",
    "interaction_review_required": "需先核对药物相互作用",
    "safety_hold": "存在限制因素，暂不采用",
    "safety_hold_liver_kidney": "肝肾功能相关限制，暂缓补充",
    "safety_review_liver_kidney": "需结合肝肾功能评估",
    "safety_hold_immunocompromised": "免疫状态相关限制，暂不匹配",
    "safety_hold_severe_immunocompromise": "重度免疫抑制，不建议采用",
    "suppressed_due_to_allergy": "存在过敏相关限制，不予匹配",
    "no_approved_formula_match": "尚无适配的已批准配方",
    "multiple_approved_matches_need_review": "多个配方匹配，需审核选择",
    "approved_formula_match_requires_review": "配方已匹配，个体适用性待审核",
    "no_guideline_indication_identified": "当前未获得明确适应证",
    "clinical_trial_only": "仅在规范临床试验中评估",
    "possible_guideline_context_needs_confirmation": "需专科确认临床适应证",
    "not_indicated_by_barrier_assessment": "当前屏障结果不构成适应证",
    "medical_clearance_required": "需先确认医疗适用条件",
}
_STRUCTURED_LABELS = {
    "name": "名称", "title": "项目", "text": "", "label": "", "value": "数值", "unit": "单位",
    "amount": "用量", "min": "下限", "max": "上限", "frequency": "频次", "route": "途径",
    "duration": "周期", "days": "天数", "weeks": "周数", "instructions": "用法",
    "condition": "条件", "conditions": "条件", "monitoring": "复评", "goal": "目标",
    "dose": "用量", "dailyDose": "每日用量", "notes": "说明", "warning": "注意事项",
    "evidenceSummary": "研究说明", "reason": "依据", "recommendation": "建议",
    "strain": "菌株", "code": "编号", "latinName": "学名", "strainId": "菌株编号",
    "sourceProgramRange": "提供方案范围", "officialMaximum": "说明书上限",
    "setting": "实施场景", "directions": "目录用法", "course": "目录周期", "package": "包装",
    "outcome": "研究目标", "limitations": "适用说明", "form": "制剂形式",
    "ingredient": "成分", "adultUpperLimit": "成人总摄入上限",
    "conditionSpecificStudy": "特定人群研究方案", "note": "说明",
}
_PUBLIC_TERMS = {
    "D_LACTATE": "D-乳酸", "DAO": "DAO", "LPS": "LPS",
    "fiber_total": "膳食纤维", "protein_adequacy": "能量与蛋白质",
    "fish_food": "鱼类与食物来源 omega-3", "zinc_if_needed": "按需评估锌营养",
    "vitamin_d_if_needed": "按需评估维生素 D 营养",
    "glutamine_specialist_review": "谷氨酰胺营养支持专科评估",
    "enteral_nutrition_review": "肠内营养适用性评估",
    "cell_free_supernatant": "无细胞上清", "postbiotic": "后生元",
    "heat_inactivated_postbiotic": "热灭活后生元", "research_strain": "研究菌株",
    "live_or_catalog_unspecified": "活菌形式需核对产品标签", "live": "活菌",
    "oral": "口服", "mg elemental zinc/day": "mg 元素锌/日", "g/day": "g/日",
    "IU/day": "IU/日", "servings fish/week": "份鱼/周",
}


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list:
    return value if isinstance(value, list) else []


def _text(value: object, fallback: str = "") -> str:
    if value is None:
        return fallback
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (list, tuple)):
        return "；".join(part for item in value if (part := _text(item)))
    if isinstance(value, dict):
        # Do not expose arbitrary rule keys, review flags, or internal metadata.
        return "；".join(
            f"{label}：{_text(value[key])}" if label else _text(value[key])
            for key, label in _STRUCTURED_LABELS.items()
            if value.get(key) is not None and _text(value[key]) and not (key == "label" and value.get("name"))
        )
    return _PUBLIC_TERMS.get(str(value), str(value))


def _status(value: object) -> str:
    if isinstance(value, str) and value in _STATUS:
        return _STATUS[value]
    # Unknown machine statuses must not appear as patient-facing prose.
    return str(value) if isinstance(value, str) and any("\u4e00" <= c <= "\u9fff" for c in value) else "待核对适用条件"


def _measurement_value(item: dict, fallback: str = "") -> str:
    if item.get("operator") in {"<", "<=", ">", ">="} and item.get("raw") is not None:
        return _text(item.get("displayValue") or item["raw"])
    return _text(item.get("value"), fallback)


def _pattern(value: object) -> str | None:
    candidate = str(value or "").replace("1", "+").replace("0", "-")
    return candidate if len(candidate) == 3 and all(char in "+-" for char in candidate) else None


def _age_scope(subject: dict) -> str:
    age = subject.get("age")
    if isinstance(age, (int, float)) and not isinstance(age, bool) and age >= 0:
        return "adult" if age >= 18 else "minor"
    return "unknown"


def _paragraph(text: str, title: str | None = None) -> dict:
    return {"kind": "paragraph", "text": text, **({"title": title} if title else {})}


def _table(columns: list[str], rows: list, title: str | None = None) -> dict:
    return {"kind": "table", "columns": columns, "rows": rows, **({"title": title} if title else {})}


def _section(identifier: str, title: str, blocks: list[dict], subtitle: str = "") -> dict:
    return {"id": identifier, "title": title, "subtitle": subtitle, "blocks": blocks}


def _clinical_rows(findings: list) -> list:
    labels = {"historical": "既往检查", "current": "本次检查", "date_unknown": "日期待核对", "future_date_requires_review": "日期待核对"}
    rows = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        exam = item.get("examResult")
        result = (exam_result_excerpt(exam) or "未提供可用的检查结论") if isinstance(exam, str) and exam.strip() else " ".join(
            filter(None, [_text(item.get("value")), _text(item.get("unit"))]))
        details = [result, _text(item.get("resultStatusName") or item.get("resultStatus"))]
        if item.get("referenceRange"):
            details.append(f"原报告参考范围：{_text(item['referenceRange'])}")
        if item.get("conflictingResults"):
            details.append("原始记录存在差异，需核对。")
        rows.append([
            _text(item.get("date") or item.get("rawDate"), "未提供"),
            _text(item.get("itemName"), "检查项目"),
            labels.get(item.get("temporalStatus"), "时间状态待核对"),
            "\n".join(part for part in details if part),
        ])
    return rows


def _public_microbiome_sources(sources: list) -> list:
    """Keep one bounded semantic summary, never entire source or regimen text."""
    allowed = {
        "sourceId", "format", "status", "origin", "identityVerified", "sampleDate",
        "enterotype", "metrics", "sourceModelIndices", "warnings", "sha256",
    }
    return [{key: deepcopy(value) for key, value in source.items() if key in allowed}
            for source in sources if isinstance(source, dict)]


def build_report(assessment: dict, interventions: dict, *, report_id: str, source_summary: list | None = None) -> dict:
    """Build the current five-chapter document; frozen older reports are unchanged."""
    from .chapters import compose_report
    return compose_report(assessment, interventions, report_id=report_id, source_summary=source_summary)
