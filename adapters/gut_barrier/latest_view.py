"""Latest editorial layout of an immutable public report, not reassessment.

Wire blocks/columns remain frozen. These transformations are solely for the
explicit v2 frontend release; unknown/clinical-safety blocks are retained.
"""
from __future__ import annotations

from copy import deepcopy
import re

from .frontend_template import uses_attention_index

MARKERS = ("检测指标", "检测结果", "参考范围", "结果评价", "单项评分")
RISKS = ("疾病", "风险指数", "风险等级", "机制关联")
MARKER_VIEW_COLUMNS = ["检测项目", "检测指标", "检测结果", "参考区间", "结果评价", "反映维度"]
RISK_VIEW_COLUMNS = ["疾病", "风险等级", "机制关联"]
XU_ATTENTION_COLUMNS = ["疾病", "健康关注指数", "关注等级", "机制关联"]
XU_MODEL_COLUMNS = ["疾病", "评估指数", "原有等级", "机制关联"]
COVER_FACT_LABELS = frozenset({"姓名", "性别", "年龄", "样本类型", "检测项目", "检测日期", "报告日期"})
_MARKER_ROLES = (
    (("DAO", "二胺氧化酶"), "肠道屏障功能", "上皮完整性相关信息"),
    (("LPS", "细菌脂多糖", "细菌内毒素"), "肠道屏障功能", "内毒素暴露相关信息"),
    (("D-乳酸", "D-Lactate"), "肠道屏障功能", "代谢与通透性相关信息"),
    (("hs-CRP", "超敏C反应蛋白"), "炎症指标", "全身炎症相关信息"),
)
_WORD_MARKER_DIMENSIONS = (
    "肠上皮细胞损伤",
    "内毒素入血、炎症级联激活",
    "紧密连接损伤、肠通透性增加",
    "慢性低度炎症",
)
_SYSTEM_ORDER = ("消化系统", "代谢系统", "心脑血管系统", "神经系统", "精神系统", "免疫系统", "泌尿系统", "肿瘤")


def cells(row):
    result = row.get("cells") if isinstance(row, dict) else row
    return result if isinstance(result, (tuple, list)) else []


def marker_role(value):
    if isinstance(value, str):
        for index, (aliases, group, dimension) in enumerate(_MARKER_ROLES):
            if any(alias.casefold() in value.casefold() for alias in aliases):
                return index, group, dimension
    return len(_MARKER_ROLES), "其他指标", None


def arrange_sections(sections: list, *, clean_review_layout: bool = False,
                     canonical_dynamic_layout: bool = False) -> list:
    """Order blocks while keeping chart source columns and every unknown block."""
    output = deepcopy(sections)
    if canonical_dynamic_layout:
        return output  # The current callback already owns clinical order.
    for section in output:
        if not isinstance(section, dict) or section.get("visible") is False:
            continue
        blocks = section.get("blocks", [])
        if section.get("id") == "overview":
            def rank(block):
                if clean_review_layout and block.get('title') == '本次评估资料范围':
                    return 0.5
                return {"受检者信息": 0, "1.1 检测综合评估结论": 1,
                        "肠道屏障功能评分（IBFC）": 2, "屏障评分分级": 3,
                        "1.3 肠道屏障功能评分（IBFC）与损伤分级": 4,
                        "1.4 肠道屏障受损类型与受损状况": 5,
                        "1.2 检测指标分项阐述": 6, "四项检测指标": 7, "三项检测指标": 7,
                        "指标意义": 8, "1.5 慢性低度炎症评估": 9,
                        "hs-CRP 项目功能分层": 10}.get(block.get("title"), 11)
            section["blocks"] = sorted(blocks, key=rank)
            for block in section["blocks"]:
                if tuple(block.get("columns", [])) == MARKERS:
                    block["rows"] = sorted(block.get("rows", []), key=lambda row: marker_role(cells(row)[0] if cells(row) else None)[0])
        elif section.get("id") == "disease_risks":
            tables = [b for b in blocks if tuple(b.get("columns", [])) == RISKS]
            def system_key(block):
                title = block.get("title") or ""
                return next((i for i, system in enumerate(_SYSTEM_ORDER) if system in title), len(_SYSTEM_ORDER))
            tables = sorted(tables, key=system_key)
            for index, block in enumerate(tables, 1):
                block["title"] = re.sub(r"^2\.\d+\s+", f"2.{index} ", block.get("title") or "")
            # Replace table slots only: introductions, clinical findings and
            # warning blocks are not lost or reclassified by the new layout.
            iterator = iter(tables)
            section["blocks"] = [next(iterator) if tuple(b.get("columns", [])) == RISKS else b for b in blocks]
    return output


def display_block(section_id: str, block: dict, sections: list, *, xu: bool = False,
                  source_stage_layout: bool = False, word_review_layout: bool = False,
                  canonical_dynamic_layout: bool = False, template_risk_layout: bool = False) -> dict | None:
    """Produce only a render copy. None suppresses known redundant UI, not data."""
    if block.get("kind") == "references":
        return block  # Bibliography namespaces bind to these exact display objects.
    out = deepcopy(block)
    kind, title = out.get("kind"), out.get("title")
    if canonical_dynamic_layout:
        # V8 titles, table shapes, values and clinical paragraphs are already
        # the publication contract. Only duplicate graphic/cover data is hidden.
        if section_id == "overview" and kind == "facts" and title == "受检者信息":
            out["items"] = [item for item in out.get("items", [])
                            if item.get("label") not in COVER_FACT_LABELS]
            return out if out["items"] else None
        if section_id == "overview" and kind == "cards" and title == "肠道屏障功能评分（IBFC）":
            items = out.get("items", [])
            if template_risk_layout:
                # New callbacks retain both legacy slots as null. Hide only
                # genuinely empty display cards; frozen nonempty grades and
                # any custom clinical detail remain unchanged and visible.
                items = [item for item in items if not (
                    item.get("label") in {"损伤等级", "总体阶段"}
                    and item.get("value") in (None, "") and item.get("detail") in (None, ""))]
                out["items"] = items
            if sum(item.get("label") == "IBFC评分" for item in items) == 1:
                consumed = {"IBFC评分"}
                if template_risk_layout and sum(item.get("label") == "评分占满分比例" for item in items) == 1:
                    # V11 shows this existing value beside IBFC in the figure;
                    # the immutable callback remains available in sourceReport.
                    consumed.add("评分占满分比例")
                out["items"] = [item for item in items if item.get("label") not in consumed]
                return out if out["items"] else None
            if template_risk_layout and not out.get("items"):
                return None
            return out
        return out
    if section_id == "overview":
        if kind == "facts" and title == "受检者信息":
            # Keep future/unknown fields in the body. Known fields are consumed
            # by the cover; the previously hidden report number stays hidden.
            out["items"] = [item for item in out.get("items", [])
                            if item.get("label") not in COVER_FACT_LABELS | {"报告编号"}]
            return out if out["items"] else None
        if kind == "table" and title == "1.1 检测综合评估结论":
            if out.get("columns") != ["评价项目", "综合结论"] or any(len(cells(row)) != 2 for row in out.get("rows", [])):
                return out
            # Only deduplicate inflammation when its dedicated text survives.
            has_inflammation = any(s.get("id") == "overview" and s.get("visible") is not False
                and any(b.get("kind") == "paragraph" and b.get("title") == "1.5 慢性低度炎症评估"
                        and b.get("text") for b in s.get("blocks", [])) for s in sections)
            # Do not parse clinical free text or drop arbitrary safety rows.
            return {"kind": "paragraph", "title": "1.1 综合评估指数（IBFC · 肠屏障功能评分）",
                    "text": "\n".join(str(row[1]) for r in out.get("rows", [])
                                      if len(row := cells(r)) >= 2 and row[1] is not None
                                      and not (row[0] == "炎症状态" and has_inflammation))}
        if kind == "cards" and title == "肠道屏障功能评分（IBFC）":
            items = out.get("items", [])
            if sum(item.get("label") == "IBFC评分" for item in items) != 1:
                return out  # Malformed/future cards have no matching graphic.
            # Score and grade are covered by the graphic. Preserve additional
            # card data, unless its standard type text is already in the body.
            has_type = any(s.get("id") == "overview" and s.get("visible") is not False
                and any(b.get("kind") == "paragraph" and b.get("title") == "1.4 肠道屏障受损类型与受损状况"
                        and b.get("text") for b in s.get("blocks", [])) for s in sections)
            out["items"] = [item for item in items if item.get("detail") is not None
                or item.get("label") not in {"IBFC评分", "损伤等级"} | ({"受损类型"} if has_type else set())]
            if xu:
                # The separate categorical stage component consumes this item;
                # never show a duplicate empty stage card or infer it from IBFC.
                out["items"] = [item for item in out["items"] if item.get("label") != "总体阶段"]
            if source_stage_layout:
                # The v6 source-style graphic shows the single score and any
                # nonstandard score detail. Unknown extra cards remain intact.
                out["items"] = [item for item in out["items"] if item.get("label") != "IBFC评分"]
            return out if out["items"] else None
        if kind == "bands" and title in {"屏障评分分级", "hs-CRP 项目功能分层"}:
            return None  # Consumed by the graphs, not repeated as separate tables.
        if kind == "paragraph" and title in {"1.3 肠道屏障功能评分（IBFC）与损伤分级", "1.2 检测指标分项阐述", "1.2 检测项目与结果总览"}:
            return None
        if kind == "paragraph" and title in {"1.4 肠道屏障受损类型与受损状况", "1.5 慢性低度炎症评估"}:
            out["title"] = None
        if kind == "table" and tuple(out.get("columns", [])) == MARKERS:
            out["title"] = "1.2 检测项目与结果总览"
            out["columns"] = MARKER_VIEW_COLUMNS[:]
            rows = []
            for row in out.get("rows", []):
                data = cells(row)
                if len(data) != 5:
                    # Unknown future shapes must not be partially reinterpreted.
                    return deepcopy(block)
                marker_index, group, dimension = marker_role(data[0])
                if word_review_layout and marker_index < len(_WORD_MARKER_DIMENSIONS):
                    dimension = _WORD_MARKER_DIMENSIONS[marker_index]
                new = [group, *data[:4], dimension]
                rows.append({**row, "cells": new} if isinstance(row, dict) else new)
            out["rows"] = rows
    elif section_id == "disease_risks":
        if kind == "table" and tuple(out.get("columns", [])) == RISKS:
            if xu:
                data = [cells(row) for row in out.get("rows", [])]
                if any(len(row) != 4 for row in data):
                    return deepcopy(block)
                attention_only = uses_attention_index(sections) and all(
                    not str(row[3] or "").startswith("本项数值来自独立的已配置模型")
                    and isinstance(row[2], str)
                    and ("关注" in row[2] or "优先评估" in row[2] or row[2] == "专项资料不足")
                    for row in data)
                # Restore all four original cells without changing numeric
                # types, missing values or model-specific interpretation.
                out["columns"] = (XU_ATTENTION_COLUMNS if attention_only else XU_MODEL_COLUMNS)[:]
                return out
            out["columns"] = RISK_VIEW_COLUMNS[:]
            rows = []
            for row in out.get("rows", []):
                data = cells(row)
                if len(data) != 4:
                    return deepcopy(block)
                mechanism = data[3]
                # Independent registered scores are excluded from the attention
                # chart. Keep their values visible even in the three-column view.
                registered = (str(mechanism or "").startswith("本项数值来自独立的已配置模型")
                              or not (isinstance(data[2], str) and ("关注" in data[2] or "优先评估" in data[2])))
                if registered and data[1] is not None:
                    mechanism = f"独立模型指数：{data[1]}。" + str(mechanism or "")
                new = [data[0], data[2], mechanism]
                rows.append({**row, "cells": new} if isinstance(row, dict) else new)
            out["rows"] = rows
        if kind == "list" and title == "本章小结":
            if not out.get("items"):
                return None
            if xu:
                count = sum(tuple(b.get("columns", [])) == RISKS for s in sections
                            if s.get("id") == "disease_risks" for b in s.get("blocks", []))
                return {"kind": "paragraph", "title": f"2.{count+1} 本章小结",
                        "text": "\n".join(str(item) for item in out["items"] if item is not None)}
            all_rows = [cells(row) for s in sections if s.get("id") == "disease_risks"
                        for b in s.get("blocks", []) if tuple(b.get("columns", [])) == RISKS
                        for row in b.get("rows", [])]
            cards = []
            for item in out.get("items", [])[:4]:
                matches = [r for r in all_rows if len(r) == 4 and isinstance(r[0], str)
                           and isinstance(item, str) and item.startswith(r[0] + "：")]
                if len(matches) != 1:
                    return out  # Never guess which disease an ambiguous title belongs to.
                row = matches[0]
                cards.append({"label": row[0], "value": row[2], "detail": item[len(row[0])+1:]})
            count = sum(tuple(b.get("columns", [])) == RISKS for s in sections
                        if s.get("id") == "disease_risks" for b in s.get("blocks", []))
            return {"kind": "cards", "title": f"2.{count+1} 本章小结", "items": cards, "_viewRole": "focus"}
    return out


def display_blocks(sections: list, *, xu=False, source_stage_layout=False,
                   word_review_layout=False, canonical_dynamic_layout=False,
                   template_risk_layout=False) -> dict:
    """One projection for both HTML and frontend display JSON.

    Object identities bind display copies to source chart data within this call
    only. They are never persisted or sent in the report.
    """
    return {id(block): display_block(section.get("id"), block, sections, xu=xu,
                                    source_stage_layout=source_stage_layout,
                                    word_review_layout=word_review_layout,
                                    canonical_dynamic_layout=canonical_dynamic_layout,
                                    template_risk_layout=template_risk_layout)
            for section in sections if isinstance(section, dict) and section.get("visible") is not False
            for block in section.get("blocks", []) if isinstance(block, dict)}
