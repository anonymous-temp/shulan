"""Freeze only rendered report content for the public callback contract.

Assessment, treatment selection and source audit remain internal. This projection
does not change clinical decisions and is applied after narrative arrangement.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import re
from urllib.parse import urlsplit

from .display_versions import (
    CANONICAL_DISPLAY_TEMPLATE, PRODUCT_FEEDBACK_DISPLAY_TEMPLATE, ORDERED_REFERENCE_DISPLAY_TEMPLATE,
    TEMPLATE_RISK_DISPLAY_TEMPLATE, PINNED_REFERENCE_DISPLAY_TEMPLATE, CLOSED_BOUNDARY_DISPLAY_TEMPLATE, BARRIER_ONLY_DISPLAY_TEMPLATE,
)
from .integration_content import complete_disease_content, PRODUCT_ATTENTION_NOTICE, QUALITATIVE_FOLLOWUP_LEVEL


CALLBACK_SCHEMA_VERSION = "2.0"
PUBLIC_REPORT_SCHEMA_VERSION = "gut-barrier-public.v1"
DYNAMIC_REPORT_SCHEMA_VERSION = "gut-barrier-public.v2"
SUPPORTED_PUBLIC_REPORT_VERSIONS = frozenset({PUBLIC_REPORT_SCHEMA_VERSION, DYNAMIC_REPORT_SCHEMA_VERSION})
CALLBACK_TYPE = "gutBarrier"
SUPPORTED_CALLBACK_TYPES = frozenset({CALLBACK_TYPE, "gut_barrier"})


def resolve_interpretation_blocks(report: dict, fallback: list) -> list:
    """Resolve order against verified candidates, never model-supplied prose."""
    candidates = report.get("narrativeCandidates")
    if not isinstance(candidates, list):
        return fallback
    allowed = {item.get("id"): item for item in candidates
               if isinstance(item, dict) and isinstance(item.get("id"), str)}
    selected = report.get("interpretation", candidates)
    if isinstance(selected, dict):
        selected = selected.get("paragraphIds", [])
    if not isinstance(selected, list):
        selected = candidates
    ids = []
    for item in selected:
        identifier = item.get("id") if isinstance(item, dict) else item
        if isinstance(identifier, str) and identifier in allowed and identifier not in ids:
            ids.append(identifier)
    for identifier, candidate in allowed.items():
        if candidate.get("required") is True and identifier not in ids:
            ids.append(identifier)
    resolved = [{"kind": "paragraph", "text": allowed[identifier].get("text", "")} for identifier in ids]
    # Older reports used one dedicated, unstructured paragraph section. Keep
    # that contract, but never replace a modern mixed-content chapter wholesale.
    if all(isinstance(block, dict) and block.get("kind") == "paragraph"
           and not block.get("title") for block in fallback):
        return resolved
    candidate_texts = [item.get("text", "") for item in allowed.values()]
    slots = [index for index, block in enumerate(fallback)
             if isinstance(block, dict) and block.get("kind") == "paragraph"
             and not block.get("title") and block.get("text") in candidate_texts]
    if not slots:
        return fallback
    output, cursor = [], 0
    for index, block in enumerate(fallback):
        if index not in slots:
            output.append(block)
        elif cursor < len(resolved):
            output.append(resolved[cursor])
            cursor += 1
        if index == slots[-1]:
            output.extend(resolved[cursor:])
    return output


def _display(value):
    # Match the renderer's text/label precedence without carrying arbitrary
    # nested source, rule or review properties into otherwise visible cells.
    if isinstance(value, dict):
        selected = value.get("text", value.get("label", ""))
        return _display(selected)
    if isinstance(value, (list, tuple)):
        return [_display(item) for item in value]
    return deepcopy(value)


def _fields(source: dict, keys: tuple[str, ...]) -> dict:
    return {key: _display(source.get(key)) for key in keys}


def _items(value) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _safe_url(value) -> bool:
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def resolve_reference_citation(item: dict):
    """Legacy title/name aliases remain accepted; explicit 0/false are not absent."""
    return next((item[key] for key in ("citation", "title", "name")
                 if item.get(key) is not None and item.get(key) != ""), None)


def _block(block: dict) -> dict:
    kind = block.get("kind")
    known = {"table", "facts", "cards", "bands", "list", "callout", "references"}
    result = {"kind": kind if kind in known else "paragraph", "title": _display(block.get("title"))}
    if kind == "table":
        result["columns"] = [_display(item) for item in _items(block.get("columns"))]
        rows = []
        for row in _items(block.get("rows")):
            cells = _items(row.get("cells")) if isinstance(row, dict) else row
            if not isinstance(cells, (list, tuple)):
                continue
            projected = [_display(cell) for cell in cells]
            if isinstance(row, dict):
                row_result = {"cells": projected, "current": row.get("current") is True}
                rows.append(row_result)
            else:
                rows.append(projected)
        result["rows"] = rows
    elif kind in {"facts", "cards", "bands"}:
        keys = {"facts": ("label", "value"), "cards": ("label", "value", "detail"),
                "bands": ("range", "label")}[kind]
        result["items"] = []
        for item in _items(block.get("items")):
            if not isinstance(item, dict):
                continue
            projected = _fields(item, keys)
            if kind == "bands":
                projected["selected"] = item.get("selected") is True
            result["items"].append(projected)
    elif kind == "list":
        result["items"] = [_display(item) for item in _items(block.get("items"))]
    elif kind == "references":
        result["items"] = []
        for item in _items(block.get("items")):
            if not isinstance(item, dict):
                continue
            projected = _fields(item, ("citation", "url", "year", "publisher", "doi", "location"))
            projected["citation"] = _display(resolve_reference_citation(item))
            url = item.get("url") or item.get("link")
            projected["url"] = deepcopy(url) if _safe_url(url) else None
            result["items"].append(projected)
    else:
        result["text"] = _display(block.get("text"))
        if kind == "callout":
            result["tone"] = "caution" if block.get("tone") == "caution" else "default"
    return result


def project_public_report(report: dict) -> dict:
    """Return the complete v1 presentation for internal/legacy preview use.

    New production callbacks use project_callback_report. Keeping this pure
    full projection prevents silently migrating previously frozen v1 reports.
    """
    if not isinstance(report, dict):
        raise TypeError("report must be a dictionary")
    meta = report.get("meta") if isinstance(report.get("meta"), dict) else {}
    sections = []
    for section in _items(report.get("sections")):
        if not isinstance(section, dict) or section.get("visible") is False:
            # Preserve fixed keys and ordinal position, not hidden source data.
            sections.append({"id": None, "title": None, "subtitle": None, "blocks": [], "visible": False})
            continue
        projected = _fields(section, ("id", "title", "subtitle"))
        projected["visible"] = True
        blocks = _items(section.get("blocks"))
        if section.get("id") == "interpretation":
            blocks = resolve_interpretation_blocks(report, blocks)
        projected["blocks"] = [_block(block) for block in blocks if isinstance(block, dict)]
        sections.append(projected)
    return {"schemaVersion": PUBLIC_REPORT_SCHEMA_VERSION,
            "meta": _fields(meta, ("title", "reportId")), "sections": sections}


_DYNAMIC_REFERENCE_TYPES = frozenset({
    "official_fact_sheet", "professional_guideline", "regulatory_safety_alert",
    "official_guideline", "professional_public_guidance", "official_public_guidance",
    "scientific_statement", "official_product_information", "expert_consensus",
})
_RISK_COLUMNS = ["疾病", "风险指数", "风险等级", "机制关联"]
_MARKER_COLUMNS = ["检测指标", "检测结果", "参考范围", "结果评价", "单项评分"]
CURRENT_MARKER_COLUMNS = ["检测项目", "检测指标", "检测结果", "参考区间", "结果评价", "反映维度"]
CURRENT_QC_TITLE = "4.1 质控校验"
CURRENT_QC_COLUMNS = ["校验项目", "本次记录", "校验项目", "本次记录"]
_QC_SAMPLE_TYPE = "血液"
_QC_TEST_PANEL = "肠道屏障三项"
# Confirmed copy for this dedicated report, not inferred from results or flags.
_QC_STATUS = "合格"
IBFC_SHARE_LABEL = "评分占满分比例"
IBFC_MAX_SCORE = 15
_MARKER_DIMENSIONS = (
    (("dao", "二胺氧化酶"), "肠道屏障功能", "肠上皮细胞损伤"),
    (("lps", "细菌脂多糖", "细菌内毒素"), "肠道屏障功能", "内毒素入血、炎症级联激活"),
    (("d-乳酸", "d-lactate"), "肠道屏障功能", "紧密连接损伤、肠通透性增加"),
    # Historical explicitly versioned projections retain the old row shape.
    (("hs-crp", "超敏c反应蛋白"), "炎症指标", "慢性低度炎症"),
)


def _row_cells(row):
    return row.get("cells", []) if isinstance(row, dict) else row


def score_share_percentage(score):
    """Provisional scale normalization, not a percentile or disease probability.

    Keep this arithmetic separate from clinical scoring so a future confirmed
    presentation rule can replace it without changing IBFC or frozen reports.
    """
    if (isinstance(score, bool) or not isinstance(score, (int, float))
            or not 0 <= score <= IBFC_MAX_SCORE):
        return None
    share = (Decimal(str(score)) * 100 / IBFC_MAX_SCORE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{share}%"


def _current_quality_control(report, *, barrier_only=False):
    """Bind subject facts to the service owner's confirmed dedicated-report QC."""
    overview = next((s for s in report["sections"] if s["id"] == "overview" and s["visible"]), {})
    facts = {}
    for block in overview.get("blocks", []):
        if block.get("kind") == "facts" and block.get("title") == "受检者信息":
            for item in block["items"]:
                if isinstance(item["label"], str):
                    facts.setdefault(item["label"], []).append(item["value"])

    def fact(label):
        values = facts.get(label, [])
        return values[0] if len(values) == 1 and isinstance(values[0], (str, int, float, bool)) else None

    return {"kind": "table", "title": CURRENT_QC_TITLE, "columns": CURRENT_QC_COLUMNS[:], "rows": [
        ["姓名", fact("姓名"), "性别", fact("性别")],
        ["年龄", fact("年龄"), "样本类型", _QC_SAMPLE_TYPE],
        ["送检项目", _QC_TEST_PANEL if barrier_only else "肠道屏障三项、hs-CRP", "样本状态", _QC_STATUS],
        ["样本制备质控", _QC_STATUS, "检测数据质控", _QC_STATUS],
        ["检验医师", None, "审核医师", None],
    ]}


def _append_current_result_blocks(report, *, barrier_only=False):
    """Only enrich newly published current reports; never a replayed receipt."""
    quality = _current_quality_control(report, barrier_only=barrier_only)
    for section in report["sections"]:
        if not section["visible"]:
            continue
        if section["id"] == "appendix":
            section["blocks"].insert(0, quality)
        elif section["id"] == "overview":
            for block in section["blocks"]:
                if block["kind"] == "facts" and block["title"] == "受检者信息":
                    for item in block["items"]:
                        if item["label"] == "样本类型":
                            item["value"] = _QC_SAMPLE_TYPE
                if block["kind"] == "cards" and block["title"] == "肠道屏障功能评分（IBFC）":
                    scores = [item["value"] for item in block["items"] if item["label"] == "IBFC评分"]
                    block["items"].append({"label": IBFC_SHARE_LABEL,
                                           "value": score_share_percentage(scores[0]) if len(scores) == 1 else None,
                                           "detail": f"IBFC 满分为 {IBFC_MAX_SCORE} 分"})
    return report


def _template_overview_blocks(report):
    """Align new v11 stage concepts without inventing clinical boundaries.

    This publication-only step never changes the assessment, source audit, or
    already-frozen callbacks. Legacy injury grades stay in the internal audit;
    the public three-stage concepts do not acquire an unapproved classifier.
    """
    for section in report.get("sections", []):
        if section.get("id") != "overview" or section.get("visible") is False:
            continue
        blocks = []
        for block in section.get("blocks", []):
            if block.get("title") == "本次评估资料范围":
                continue  # Source limitations remain in the internal audit.
            if block.get("kind") == "paragraph" and block.get("title") in {None, "1.4 肠道屏障受损类型与受损状况"}:
                fallback = "当前三项组合尚未确定，结合已获得的单项结果进一步完善分型评价。"
                text = block.get("text")
                if isinstance(text, str) and text.startswith(fallback):
                    remaining = text[len(fallback):].lstrip()
                    if not remaining:
                        continue
                    # Remove only the known generated placeholder; appended
                    # individual clinical/safety content must survive.
                    block["text"] = remaining
            if block.get("kind") == "table" and block.get("title") == "辅助资料摘要":
                continue
            if block.get("kind") == "bands" and block.get("title") == "屏障评分分级":
                block["items"] = [{"range": None, "label": label, "selected": False}
                                  for label in ("健康", "亚健康", "疾病")]
            if block.get("kind") == "cards" and block.get("title") == "肠道屏障功能评分（IBFC）":
                block["items"] = [item for item in block.get("items", []) if item.get("label") != "受损类型"]
                for item in block.get("items", []):
                    if item.get("label") in {"损伤等级", "总体阶段"}:
                        item["value"] = None
                        if item.get("detail") == "分级待复核":
                            item["detail"] = None
            blocks.append(block)
        section["blocks"] = blocks
    return report


def normalize_current_dynamic_report(report: dict, *, product_feedback=False, model_names=(),
                                     ordered_reference_layout=False, assessment=None,
                                     word_summary=False) -> dict:
    """One current dynamic view for delivery and HTML; never reassess values.

    Called only for a newly generated current release (or an explicit current
    preview), never while replaying an already-frozen callback. Preserve all
    block/item keys and diagram inputs. Fixed chapters remain client-owned.
    """
    output = deepcopy(report)
    for section in output.get("sections", []):
        if section.get("visible") is False:
            continue
        blocks = section.get("blocks", [])
        if section.get("id") == "overview":
            ranks = {"受检者信息": 0, "本次评估资料范围": 1,
                     "1.1 检测综合评估结论": 2, "1.1 综合评估指数（IBFC · 肠屏障功能评分）": 2,
                     "肠道屏障功能评分（IBFC）": 3, "屏障评分分级": 4,
                     "1.3 肠道屏障功能评分（IBFC）与损伤分级": 5,
                     "1.4 肠道屏障受损类型与受损状况": 6,
                     "四项检测指标": 7, "三项检测指标": 7, "1.2 检测项目与结果总览": 7,
                     "1.5 慢性低度炎症评估": 8, "hs-CRP 项目功能分层": 9}
            # Current receipts are already ordered and have anonymous clinical
            # paragraphs. Do not reorder them a second time by lost old titles.
            if any(b.get("title") in {"四项检测指标", "三项检测指标"} for b in blocks):
                blocks = sorted(blocks, key=lambda b: ranks.get(b.get("title"), 10))
            has_inflammation = any(b.get("title") == "1.5 慢性低度炎症评估"
                                   and b.get("kind") == "paragraph" and b.get("text") for b in blocks)
            revised = []
            for block in blocks:
                title = block.get("title")
                if (word_summary and title == "1.3 肠道屏障功能评分（IBFC）与损伤分级"
                        and block.get("kind") == "paragraph"):
                    from .review_release import barrier_score_description
                    readout = barrier_score_description(assessment)
                    if readout is not None:
                        from .chapters import _barrier_text
                        legacy = _barrier_text(assessment["barrier"], include_unknown_pattern=not bool(assessment.get("reportReview")))
                        if isinstance(block.get("text"), str) and block["text"].startswith(legacy):
                            # Only replace our own generated prefix. Preserve
                            # any appended individual clinical/safety prose.
                            block["text"] = readout + block["text"][len(legacy):]
                if (block.get("kind") == "table" and title == "1.1 检测综合评估结论"
                        and block.get("columns") == ["评价项目", "综合结论"]
                        and all(len(_row_cells(row)) == 2 for row in block.get("rows", []))):
                    conclusions = [_row_cells(row) for row in block["rows"]]
                    text = "\n".join(str(cells[1]) for cells in conclusions if cells[1] is not None
                                     and not (cells[0] == "炎症状态" and has_inflammation))
                    constraints = []
                    if word_summary:
                        from .review_release import word_overview_summary, overview_management_constraints
                        concise = word_overview_summary(assessment)
                        if concise is not None:
                            text = concise
                            constraints = [note for cells in conclusions if cells[0] == "干预方向"
                                           and (note := overview_management_constraints(cells[1]))]
                    revised.append({"kind": "paragraph", "title": "1.1 综合评估指数（IBFC · 肠屏障功能评分）",
                                    "text": text})
                    if constraints:
                        revised.append({"kind": "callout", "title": "个体管理提示", "tone": "caution",
                                        "text": "\n".join(constraints)})
                    continue
                if title in {"1.3 肠道屏障功能评分（IBFC）与损伤分级", "1.4 肠道屏障受损类型与受损状况",
                             "1.5 慢性低度炎症评估"} and block.get("kind") == "paragraph":
                    block["title"] = None
                if block.get("kind") == "table" and block.get("columns") == _MARKER_COLUMNS:
                    rows = []
                    for row in block.get("rows", []):
                        cells = _row_cells(row)
                        if not isinstance(cells, list) or len(cells) != 5:
                            break
                        name = str(cells[0] or "").casefold()
                        index, group, dimension = next(
                            ((i, group, dimension) for i, (aliases, group, dimension) in enumerate(_MARKER_DIMENSIONS)
                             if any(alias in name for alias in aliases)), (4, "其他指标", None))
                        values = [group, *cells[:4], dimension]
                        rows.append((index, {**row, "cells": values} if isinstance(row, dict) else values))
                    else:
                        block.update(title="1.2 检测项目与结果总览", columns=CURRENT_MARKER_COLUMNS[:],
                                     rows=[row for _, row in sorted(rows, key=lambda item: item[0])])
                revised.append(block)
            section["blocks"] = revised
        elif section.get("id") == "disease_risks":
            count = 0
            for block in blocks:
                if block.get("kind") == "table" and block.get("columns") == _RISK_COLUMNS:
                    count += 1
                    block["title"] = re.sub(r"^2\.\d+\s+", f"2.{count} ", block.get("title") or "")
            if product_feedback:
                for block in blocks:
                    if (block.get("kind") == "table"
                            and re.fullmatch(r"(?:2\.\d+\s+)?个体临床背景", block.get("title") or "")):
                        count += 1
                        block["title"] = f"2.{count} 个体临床背景"
                    if block.get("kind") == "callout" and block.get("title") == "本次关注指数":
                        block["text"] = PRODUCT_ATTENTION_NOTICE
            summaries = []
            for block in blocks:
                if re.fullmatch(r"(?:2\.\d+\s+)?本章小结", block.get("title") or ""):
                    block["title"] = f"2.{count + 1} 本章小结"
                    summaries.append(block)
            # The conclusion is last, including when clinical background follows
            # the disease tables in the internal document.
            section["blocks"] = complete_disease_content(
                [block for block in blocks if block not in summaries] + summaries,
                product_feedback=product_feedback, model_names=model_names,
                **({"ordered_reference_layout": True, "assessment": assessment}
                   if ordered_reference_layout else {}))
        elif section.get("id") == "appendix":
            section["title"] = "第四章 附录"
            for block in blocks:
                if block.get("kind") == "references" and block.get("title") in {"5.5 参考文献", "4.4 参考文献"}:
                    block["title"] = "4.4 参考文献"
    return output


_SOURCE_CITATION = re.compile(
    r"\[(?:(?P<case>评估\d+)|(?P<group>补充|补)?(?P<numbers>\d+(?:\s*[-–—,，、]\s*\d+)*))\]")
_MODEL_MECHANISM_PREFIX = "本项数值来自独立的已配置模型，不与本章自定义关注指数比较。"


def _publish_product_references(report, source):
    """Bind new patient citations to the final callback bibliography once.

    Display consumers can append their own fixed sources after these entries,
    but must not reorder this patient-owned prefix. Frozen receipts never enter
    this function. A missing source is not given a fabricated numeric target.
    """
    target = next((block for section in report["sections"] if section["id"] == "appendix"
                   for block in section["blocks"] if block["kind"] == "references"), None)
    if target is None:
        return
    references = target["items"]
    source_groups = [block for section in source["sections"] if section["id"] == "appendix"
                     for block in section["blocks"] if block["kind"] == "references"]
    primary = next((block for block in source_groups if "参考文献" in (block.get("title") or "")), {})
    supplemental = next((block for block in source_groups if block is not primary), {})
    named = {}
    for index, reference in enumerate(references, 1):
        citation = reference.get("citation")
        if isinstance(citation, str) and (match := re.match(r"^\[(评估\d+)\]\s*", citation)):
            named[match[1]] = index
            reference["citation"] = citation[match.end():]

    def index_for(reference):
        for index, existing in enumerate(references, 1):
            if (reference.get("url") and reference.get("url") == existing.get("url")
                    or reference.get("doi") and reference.get("doi") == existing.get("doi")
                    or reference == existing):
                return index
        references.append(deepcopy(reference))
        return len(references)

    def replace(match):
        if match.group("case"):
            index = named.get(match.group("case"))
            return f"[{index}]" if index is not None else match.group()
        group = supplemental if match.group("group") else primary
        items = group.get("items", [])
        numbers = []
        for part in re.split(r"[,，、]", match.group("numbers")):
            bounds = re.split(r"[-–—]", part)
            if len(bounds) > 2 or any(len(number.strip()) > 5 for number in bounds):
                return match.group()
            start, end = int(bounds[0]), int(bounds[-1])
            if end < start or end - start > 100 or start < 1 or end > len(items):
                return match.group()
            numbers.extend(range(start, end + 1))
        resolved = list(dict.fromkeys(index_for(items[number - 1]) for number in numbers))
        return "[" + ",".join(map(str, resolved)) + "]"

    def rewrite(value):
        if isinstance(value, str):
            return _SOURCE_CITATION.sub(replace, value)
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        return value

    for section in report["sections"]:
        section["blocks"] = [block if block["kind"] == "references" else rewrite(block)
                             for block in section["blocks"]]


def _product_disease_blocks(report, assessment, *, explicit_qualitative=False):
    """Separate model-scale and actual alarm information from mechanisms."""
    rows = _items(assessment.get("diseaseRisks"))
    model_names = {row.get("name") for row in rows
                   if isinstance(row, dict) and row.get("status") == "evaluated"}
    qualitative_names = {row.get("name") for row in rows
                         if isinstance(row, dict) and row.get("status") == "qualitative_followup"
                         and row.get("riskIndex") is None and row.get("riskLevel") == "随访关注"} - model_names
    for section in report["sections"]:
        if section["id"] != "disease_risks":
            continue
        for block in section["blocks"]:
            if block["kind"] == "table" and block.get("columns") == _RISK_COLUMNS:
                for row in block["rows"]:
                    cells = _row_cells(row)
                    if (explicit_qualitative and len(cells) == 4 and cells[0] in qualitative_names
                            and cells[1] is None and cells[2] == "随访关注"):
                        # Publish a completed non-numeric result explicitly;
                        # never replace an absent risk value with zero or text.
                        cells[2] = QUALITATIVE_FOLLOWUP_LEVEL
                    if (len(cells) == 4 and cells[0] in model_names
                            and isinstance(cells[3], str)):
                        cells[3] = cells[3].removeprefix(_MODEL_MECHANISM_PREFIX)
        if model_names:
            section["blocks"].append({"kind": "callout", "title": "独立模型说明", "tone": "default",
                "text": f"{'、'.join(sorted(str(name) for name in model_names if name))}的独立模型数值沿用相应标尺，不与关注指数比较，也不纳入总览图。"})
    return model_names


def project_callback_report(report: dict, *, archive_directory: str | Path | None = None,
                            reference_audit: dict | None = None) -> dict:
    """Publish patient-dependent values only, leaving fixed copy to the client.

    This is a presentation split, not reassessment. Retained field names do
    not change. The current template binds its marker table layout. Legacy static mechanisms stay null, while
    data-driven attention rows retain their patient-specific fourth column.
    Already-published v2 reports are sanitized without losing their selected
    references. Historical callbacks are not passed through this function.
    Offline historical internal reports require an explicit external archive;
    this option never changes the asset used for current generation.
    """
    full = project_public_report(report)
    already_dynamic = report.get("schemaVersion") == DYNAMIC_REPORT_SCHEMA_VERSION
    selected_template = (report.get("meta") or {}).get("displayTemplateVersion")
    pinned_layout = selected_template in {PINNED_REFERENCE_DISPLAY_TEMPLATE, CLOSED_BOUNDARY_DISPLAY_TEMPLATE, BARRIER_ONLY_DISPLAY_TEMPLATE}
    template_risk = selected_template == TEMPLATE_RISK_DISPLAY_TEMPLATE or pinned_layout
    ordered_references = selected_template == ORDERED_REFERENCE_DISPLAY_TEMPLATE or template_risk
    product_templates = {PRODUCT_FEEDBACK_DISPLAY_TEMPLATE, ORDERED_REFERENCE_DISPLAY_TEMPLATE,
                         TEMPLATE_RISK_DISPLAY_TEMPLATE, PINNED_REFERENCE_DISPLAY_TEMPLATE,
                         CLOSED_BOUNDARY_DISPLAY_TEMPLATE, BARRIER_ONLY_DISPLAY_TEMPLATE}
    citation_source = (deepcopy(full) if not already_dynamic
                       and selected_template in product_templates else None)
    assessment = report.get("assessment") if isinstance(report.get("assessment"), dict) else {}
    disease_assessment = assessment.get("diseaseAssessment") if isinstance(assessment.get("diseaseAssessment"), dict) else {}
    attention_mode = disease_assessment.get("mode") == "data_driven_attention"
    # Public v1 previews have no internal assessment; retain the explicit new
    # presentation marker when such a preview is projected for a callback.
    attention_mode |= any(
        section.get("id") == "disease_risks" and any(
            block.get("kind") == "callout" and block.get("title") == "本次关注指数"
            for block in section["blocks"])
        for section in full["sections"])
    plans = report.get("interventions") if isinstance(report.get("interventions"), dict) else {}
    selected_references = []
    if not already_dynamic:
        for reference in _items(plans.get("references")):
            if not isinstance(reference, dict) or reference.get("type") not in _DYNAMIC_REFERENCE_TYPES:
                continue
            selected_references.append({
                "citation": reference.get("title"), "url": reference.get("url"),
                "year": reference.get("year"), "publisher": reference.get("source"),
                "doi": reference.get("doi"), "location": reference.get("location"),
            })
        selected_references.extend(deepcopy(_items(disease_assessment.get("references"))))
    sections = []
    reference_title = "5.5 参考文献"
    supplemental_references = []
    for section in full["sections"]:
        identifier = section["id"]
        if identifier not in {"overview", "disease_risks", "appendix"}:
            continue
        blocks = []
        for block in section["blocks"]:
            title, kind = block["title"], block["kind"]
            if identifier == "overview" and (
                kind == "paragraph" and title == "1.2 检测指标分项阐述"
                or kind == "list" and title == "指标意义"
            ):
                continue
            if identifier == "overview":
                if kind == "facts" and title == "受检者信息":
                    for item in block["items"]:
                        if item["label"] == "检测项目":
                            item["value"] = None
                if kind == "cards" and title == "肠道屏障功能评分（IBFC）":
                    for item in block["items"]:
                        if item["label"] == "IBFC评分" and item["detail"] == "满分15分":
                            item["detail"] = None
            if identifier == "disease_risks":
                # Current release removes only exact known introductions in
                # template_risk_review; unnamed clinical/safety text survives.
                if kind == "paragraph" and not title and not already_dynamic and not template_risk:
                    continue
                if kind == "table" and block["columns"] == _RISK_COLUMNS:
                    for row in block["rows"]:
                        cells = row["cells"] if isinstance(row, dict) else row
                        if len(cells) != 4:
                            raise ValueError("disease table must preserve the four-column contract")
                        if not already_dynamic and not attention_mode:
                            cells[3] = None
            if identifier == "appendix":
                if already_dynamic and kind == "references" and title in {"5.5 参考文献", "4.4 参考文献"}:
                    selected_references = block["items"]
                    reference_title = title
                elif already_dynamic and kind == "references" and title == "4.5 补充参考资料":
                    # Already frozen v10 receipts own this bibliography. It is
                    # not regenerated from today's source or display catalog.
                    supplemental_references.append(block)
                elif already_dynamic and kind == "table" and title == CURRENT_QC_TITLE:
                    blocks.append(block)
                continue
            blocks.append(block)
        if identifier == "appendix":
            blocks.append(_block({"kind": "references", "title": reference_title, "items": selected_references}))
            blocks.extend(supplemental_references)
        section["blocks"] = blocks
        sections.append(section)
    output = {"schemaVersion": DYNAMIC_REPORT_SCHEMA_VERSION, "meta": full["meta"], "sections": sections}
    if not already_dynamic and selected_template in product_templates:
        model_names = _product_disease_blocks(output, assessment, explicit_qualitative=ordered_references)
        output = _append_current_result_blocks(normalize_current_dynamic_report(
            output, product_feedback=True, model_names=model_names,
            ordered_reference_layout=ordered_references, assessment=assessment,
            word_summary=template_risk), barrier_only=selected_template == BARRIER_ONLY_DISPLAY_TEMPLATE)
        _publish_product_references(output, citation_source)
        if template_risk:
            _template_overview_blocks(output)
            from .template_risk_review import apply_template_risk_review
            apply_template_risk_review(output, assessment)
        if ordered_references:
            from .reference_order import (
                PUBLIC_REFERENCE_LIMIT, load_reference_asset, publish_ordered_references,
            )
            reference_asset = load_reference_asset(selected_template, archive_directory=archive_directory)
            if template_risk:
                from .reference_pins import pinned_reference_audit, pinned_references
                publish_ordered_references(output, citation_source, reference_asset,
                                           reference_limit=PUBLIC_REFERENCE_LIMIT,
                                           reference_audit=reference_audit,
                                           pinned_references=pinned_references() if pinned_layout else (),
                                           allow_fixed_budget_omission=pinned_layout)
                if pinned_layout and reference_audit is not None:
                    reference_audit["referencePublication"]["pinnedCatalog"] = pinned_reference_audit()
            else:
                # Preserve the archived v10 projection call and complete-source
                # semantics. Selection is an opt-in of newly generated v11+.
                publish_ordered_references(output, citation_source, reference_asset)
        return output
    if not already_dynamic and selected_template == CANONICAL_DISPLAY_TEMPLATE:
        return _append_current_result_blocks(normalize_current_dynamic_report(output))
    return output
