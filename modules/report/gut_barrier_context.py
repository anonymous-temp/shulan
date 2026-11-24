# -*- coding: utf-8 -*-
"""肠道屏障报告（gutBarrier）的解读上下文构建器。

与评估报告（evaluate）不同，这份数据本身就是一份**排好版的报告**：

    report.sections[] -> blocks[]，block 有 facts / table / paragraph /
    cards / bands / list / references 七种 kind。

整份只有 7KB 左右，不需要像 explain 那样做大幅裁剪；本模块负责的是：

1. **受检者信息归位**：姓名、性别、年龄等藏在第一章第一个 facts 块里，
   这里提取出来还原成和其它报告一致的 ``subject`` 结构。
2. **结构还原**：把 columns + rows 的二维表转成带列名的字典数组，
   cards / bands / list / paragraph 各自归一，缺失或 ``visible=false``
   的章节直接跳过（第三、四章是固定文案，本来就不下发）。
3. **派生判断**：哪几项检测超出参考范围、IBFC 命中哪一档、
   第二章有多少行还没做风险分层、问卷里哪些是有意义的自述。
   这些都由程序算好，不让模型自己判断。

**本模块不调用模型。** 只解读第一章与第二章，第五章附录只取参考文献。
"""

from __future__ import annotations

import json
import re
from typing import Any

from modules.gut_barrier.integration_content import ATTENTION_OVERVIEW_COLUMNS, ATTENTION_OVERVIEW_TITLE
from modules.gut_barrier.rules import RETIRED_MARKER_PATTERN
from modules.gut_barrier.template_risk_review import RISK_OVERVIEW_COLUMNS, RISK_OVERVIEW_TITLE

# ---------------------------------------------------------------- 常量

SECTION_OVERVIEW = "overview"        # 第一章 肠道屏障检测结果综述
SECTION_DISEASE_RISKS = "disease_risks"  # 第二章 相关疾病风险评估
SECTION_APPENDIX = "appendix"        # 第五章 附录（只取参考文献）

# 第三、四章是固定文案，不参与解读；这里显式列出，避免以后有人以为漏了
SKIPPED_SECTIONS: tuple[str, ...] = ("repair_plan", "mechanism")

# 结果评价里代表「没问题」的说法；其余一律按需要关注处理
NORMAL_LABELS: tuple[str, ...] = (
    "正常", "未见异常", "理想", "阴性", "未见评分异常", "未超出参考范围", "参考范围内", "符合功能医学标准值",
)
# 风险等级里代表「本次没做分层」的说法
UNASSESSED_LABELS: tuple[str, ...] = ("未评估", "未评价", "待评估", "暂未评估")
# 问卷里这些值等于没填，不作为个性化素材
EMPTY_ANSWERS: tuple[str, ...] = ("无", "无（自述）", "其他（具体情况待补充）", "待补充", "-", "—")
# 问卷里这几项是元信息，不进解读素材
QUESTIONNAIRE_SKIP: tuple[str, ...] = ("问卷提交时间",)

# 区间型结果（如「>3.0 mg/L」）：无法精确分层，话术要带限定
RANGE_MARKERS: tuple[str, ...] = (">", "＞", "<", "＜", "≥", "≤", "~", "-")

# 受检者信息在 facts 块里的标签 → 标准字段名
SUBJECT_LABELS: dict[str, str] = {
    "姓名": "name",
    "性别": "gender",
    "年龄": "age",
    "样本类型": "sample_type",
    "检测项目": "test_item",
    "报告编号": "report_id",
    "检测日期": "sample_date",
    "报告日期": "report_date",
}


# Only new gut-barrier interpretations use this boundary. Stored reports and
# the general evaluate flow retain their original content.
_CRP_NAME = re.compile(RETIRED_MARKER_PATTERN, re.I)
_CRP_ASSESSMENT = re.compile(r"炎症指标|炎症状态|炎症分层|(?:慢性低度)?炎症评估")
_RETIRED_LABELS = {"炎症状态", "炎症指标", "炎症分层", "炎症评估", "慢性低度炎症评估"}


def strip_crp_text(value: str) -> str:
    """Remove CRP clauses and their dependent advice, retaining other facts."""
    if not (_CRP_NAME.search(value) or _CRP_ASSESSMENT.search(value)):
        return value
    parts = re.split(r"([，,。；;\n])", value)
    kept = []
    crp_clause = False
    for index in range(0, len(parts), 2):
        clause = parts[index]
        delimiter = parts[index + 1] if index + 1 < len(parts) else ""
        # Only share predicates, never transfer a CRP numeric result to DAO.
        clause = re.sub(
            r"((?:D-乳酸|DAO|LPS|肠道屏障三项))[、及和与]\s*(?:" + _CRP_NAME.pattern + r")"
            r"(?=\s*(?:(?:指标)?(?:异常|升高|偏高|正常)|$))",
            r"\1", clause, flags=re.I,
        )
        retired = bool(_CRP_NAME.search(clause) or _CRP_ASSESSMENT.search(clause))
        dependent = bool(crp_clause and re.match(
            r"\s*(?:提示|需要|需|建议|应|不能|无法|待|反映|高于|低于|其|该|属于|符合|范围型|具体)", clause,
        ))
        # Actual clinical diagnoses and neighbouring barrier facts stay usable.
        independent = bool(re.search(r"D-?乳酸|DAO|LPS|IBFC|已(?:确诊|明确)|诊断(?:为)?", clause, re.I))
        if retired or (dependent and not independent):
            crp_clause = delimiter not in ("。", "；", ";", "\n")
            if delimiter in ("。", "；", ";", "\n") and kept:
                kept[-1] = kept[-1].rstrip("，,；; ") + delimiter
            continue
        if clause.strip():
            kept.append(clause + delimiter)
            crp_clause = False
        if delimiter in ("。", "；", ";", "\n"):
            crp_clause = False
    cleaned = "".join(kept).strip()
    if cleaned.endswith(("，", ",", "；", ";")):
        cleaned = cleaned[:-1] + "。"
    return cleaned


def without_crp_content(value: Any) -> Any:
    """Remove retired CRP content from a newly generated gut interpretation.

    Keep fixed object fields and types; remove CRP-only list entries, including
    entire citations rather than rewriting a publication title.
    """
    if isinstance(value, str):
        return strip_crp_text(value)
    if isinstance(value, dict):
        return {key: without_crp_content(item) for key, item in value.items()}
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, dict):
                identity = " ".join(str(item.get(key) or "") for key in ("label", "name", "item", "title", "citation"))
                if _CRP_NAME.search(identity) or _CRP_ASSESSMENT.search(identity):
                    continue
                cells = item.get("cells")
                if isinstance(cells, list) and any(_CRP_NAME.search(str(cell)) for cell in cells[:2]):
                    continue
            elif isinstance(item, list) and any(_CRP_NAME.search(str(cell)) or str(cell) in _RETIRED_LABELS for cell in item[:2]):
                continue
            cleaned = without_crp_content(item)
            if (isinstance(item, list) and len(item) > 1 and cleaned != item
                    and all(cell is None or cell == "" for cell in cleaned[1:])):
                continue
            result.append(cleaned)
        return result
    return value


# ---------------------------------------------------------------- 基础工具

def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        # 0.0 要显示成 0，不显示成 0.0
        return str(int(value)) if float(value).is_integer() else str(value)
    return ""


def _list(value: Any) -> list:
    return list(value) if isinstance(value, list) else []


def _dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _report(data: dict) -> dict:
    return _dict(without_crp_content(_dict(data).get("report")))


def _sections(data: dict) -> list[dict]:
    """只取下发且可见的章节；第三、四章是固定文案，本来就不会下发。"""
    return [
        section for section in _list(_report(data).get("sections"))
        if isinstance(section, dict) and section.get("visible") is not False
    ]


def _section(data: dict, section_id: str) -> dict:
    for section in _sections(data):
        if _text(section.get("id")) == section_id:
            return section
    return {}


def _blocks(section: dict, kind: str | None = None) -> list[dict]:
    blocks = [block for block in _list(section.get("blocks")) if isinstance(block, dict)]
    return [block for block in blocks if kind is None or _text(block.get("kind")) == kind]


def _find_block(section: dict, kind: str, *keywords: str) -> dict:
    """按 kind + 标题关键词找块；标题里的编号可能变，用关键词匹配更稳。"""
    for block in _blocks(section, kind):
        title = _text(block.get("title"))
        if all(keyword in title for keyword in keywords):
            return block
    return {}


def _table_rows(block: dict) -> list[dict[str, str]]:
    """columns + rows 的二维表 → 带列名的字典数组。"""
    columns = [_text(column) for column in _list(block.get("columns"))]
    rows = []
    for row in _list(block.get("rows")):
        cells = _list(row.get("cells")) if isinstance(row, dict) else _list(row)
        item = {}
        for index, column in enumerate(columns):
            item[column or f"col{index + 1}"] = _text(cells[index]) if index < len(cells) else ""
        if any(item.values()):
            rows.append(item)
    return rows


def _table(block: dict) -> dict[str, Any]:
    return {
        "title": _text(block.get("title")),
        "columns": [_text(column) for column in _list(block.get("columns"))],
        "rows": _table_rows(block),
    }


def _items(block: dict) -> list[dict[str, str]]:
    """cards / facts 的 items：{label, value, detail}。"""
    items = []
    for item in _list(block.get("items")):
        if not isinstance(item, dict):
            continue
        items.append({
            "label": _text(item.get("label")),
            "value": _text(item.get("value")),
            "detail": _text(item.get("detail")),
        })
    return items


def _bands(block: dict) -> list[dict[str, Any]]:
    """bands：分级条，selected 标记命中哪一档。"""
    bands = []
    for item in _list(block.get("items")):
        if not isinstance(item, dict):
            continue
        bands.append({
            "range": _text(item.get("range")),
            "label": _text(item.get("label")),
            "selected": bool(item.get("selected")),
        })
    return bands


def _selected_band(bands: list[dict[str, Any]]) -> str:
    for band in bands:
        if band["selected"]:
            return f"{band['label']}（{band['range']}）"
    return ""


def _paragraph(section: dict, *keywords: str) -> str:
    return _text(_find_block(section, "paragraph", *keywords).get("text"))


def _string_items(block: dict) -> list[str]:
    return [_text(item) for item in _list(block.get("items")) if _text(item)]


def _has_content(rows: list[dict[str, str]], *, skip_first_column: bool = True) -> bool:
    """整张表除了第一列之外是不是全空——全空就等于这块没出结果。"""
    for row in rows:
        values = list(row.values())
        cells = values[1:] if skip_first_column and len(values) > 1 else values
        if any(cell for cell in cells):
            return True
    return False


def _is_range_result(value: str) -> bool:
    return any(marker in value for marker in RANGE_MARKERS)


def _normalize_block(block: dict) -> dict[str, Any]:
    """把任意 block 归一成结构化对象。

    已知 kind 各自展开；未知 kind（例如图示、量尺）保留标题与原始 payload，
    保证同事后续加块时内容不会凭空消失。
    """
    kind = _text(block.get("kind"))
    normalized: dict[str, Any] = {"kind": kind, "title": _text(block.get("title"))}
    if kind == "table":
        normalized.update({"columns": [_text(c) for c in _list(block.get("columns"))],
                           "rows": _table_rows(block)})
    elif kind == "paragraph":
        normalized["text"] = _text(block.get("text"))
    elif kind in ("facts", "cards"):
        normalized["items"] = _items(block)
    elif kind == "bands":
        normalized["items"] = _bands(block)
    elif kind == "list":
        normalized["items"] = _string_items(block)
    elif kind == "references":
        normalized["items"] = _list(block.get("items"))
    else:
        # 未知 kind：原样带上，除了 kind/title 之外的字段都留着
        payload = {key: value for key, value in block.items() if key not in ("kind", "title")}
        normalized["payload"] = payload
    return normalized


def _remaining_blocks(section: dict, consumed: list[dict]) -> list[dict[str, Any]]:
    """本章里没有被已知提取器用掉的块，按原顺序归一后带出。"""
    used = {id(block) for block in consumed if block}
    return [
        _normalize_block(block) for block in _blocks(section)
        if id(block) not in used
    ]


# ---------------------------------------------------------------- 元信息

def build_meta(data: dict) -> dict[str, Any]:
    """受检者信息 + 报告元信息。

    姓名等字段在第一章第一个 facts 块里，这里提取出来还原成标准结构。
    """
    data = _dict(data)
    overview = _section(data, SECTION_OVERVIEW)
    facts = _items(_find_block(overview, "facts", "受检者"))
    if not facts:  # 标题换了措辞时退回第一个 facts 块
        blocks = _blocks(overview, "facts")
        facts = _items(blocks[0]) if blocks else []

    subject: dict[str, str] = {field: "" for field in SUBJECT_LABELS.values()}
    extra_facts = []
    for item in facts:
        field = SUBJECT_LABELS.get(item["label"])
        if field:
            subject[field] = item["value"]
        elif item["label"]:
            extra_facts.append(item)

    report_meta = _dict(_report(data).get("meta"))
    return {
        **subject,
        "report_title": _text(report_meta.get("title")) or "肠道屏障功能评估报告",
        "report_id": subject.get("report_id") or _text(report_meta.get("reportId")),
        "final_report_id": _text(data.get("finalReportId")),
        "status": _text(data.get("status")),
        "schema_version": _text(_report(data).get("schemaVersion")) or _text(data.get("schemaVersion")),
        # facts 块里多出来的字段原样留着，不丢数据
        "extra_facts": extra_facts,
    }


# ---------------------------------------------------------------- 第一章

def build_part1_context(data: dict) -> dict[str, Any]:
    """第一章 肠道屏障检测结果综述（1.1–1.5）。"""
    data = _dict(data)
    overview = _section(data, SECTION_OVERVIEW)

    summary_block = (_find_block(overview, "table", "检测综合评估结论")
                     or _find_block(overview, "paragraph", "综合评估指数", "IBFC"))
    indicator_block = (_find_block(overview, "table", "检测指标")
                       or _find_block(overview, "table", "四项")
                       or _find_block(overview, "table", "检测项目与结果总览"))
    questionnaire_block = _find_block(overview, "table", "健康问卷")
    method_block = _find_block(overview, "table", "检测方法")
    quality_block = _find_block(overview, "table", "质控")
    # 1.2 的「指标意义」：每项指标测什么，是话术里大白话解释的来源，
    # 有就用报告原文，没有才让模型自己组织语言。
    meaning_block = _find_block(overview, "paragraph", "指标意义") or \
        _find_block(overview, "table", "指标意义") or \
        _find_block(overview, "list", "指标意义")
    facts_block = _find_block(overview, "facts", "受检者") or \
        ((_blocks(overview, "facts") or [None])[0])

    summary = _table(summary_block)
    if summary_block.get("kind") == "paragraph":
        # The current callback owns the paragraph text; adapt only this private
        # consumer shape so existing manual builders can keep reading rows.
        summary["columns"] = ["评价项目", "综合结论"]
        summary["rows"] = ([{"评价项目": summary["title"], "综合结论": _text(summary_block.get("text"))}]
                           if _text(summary_block.get("text")) else [])
    indicators = _table(indicator_block)
    questionnaire = _table(questionnaire_block)
    method = _table(method_block)
    quality = _table(quality_block)

    indicator_rows = indicators.get("rows", [])
    abnormal, pending = _split_indicators(indicator_rows)

    ibfc_card_block = _find_block(overview, "cards", "IBFC")
    ibfc_band_block = _find_block(overview, "bands", "屏障评分")
    ibfc_paragraph_block = next((block for block in _blocks(overview, "paragraph")
                                 if block is not summary_block and "IBFC" in _text(block.get("title"))), {})
    damage_block = _find_block(overview, "paragraph", "受损类型")
    if indicator_block.get("columns") == ["检测项目", "检测指标", "检测结果", "参考区间", "结果评价", "反映维度"]:
        # V8 preserves these three paragraphs with anonymous titles, in the
        # published order around 1.2. Do not parse or rewrite clinical prose.
        blocks = _blocks(overview)
        table_index = next(i for i, block in enumerate(blocks) if block is indicator_block)
        score_end = next((i + 1 for i, block in enumerate(blocks[:table_index])
                          if block is ibfc_band_block), 0)
        before = [block for block in blocks[score_end:table_index]
                  if block.get("kind") == "paragraph" and not _text(block.get("title"))]
        if len(before) == 2:
            ibfc_paragraph_block = ibfc_paragraph_block or before[0]
            damage_block = damage_block or before[1]

    ibfc_cards = _items(ibfc_card_block)
    ibfc_bands = _bands(ibfc_band_block)

    data_gaps = []
    if not _has_content(method.get("rows", [])):
        data_gaps.append("检测方法与实验室参考信息本次未提供")
    if not _has_content(quality.get("rows", [])):
        data_gaps.append("样本与质控记录本次未提供")
    if pending:
        data_gaps.append("有检测项为区间型结果，需补充定量结果后才能精确分层")

    return {
        "chapter": "第一章 肠道屏障检测结果综述",
        # 1.1 检测综合评估结论
        "summary_table": summary,
        # 1.2 检测指标分项阐述（三项检测指标表 + 指标意义）
        "indicator_table": indicators,
        "indicator_meanings": _normalize_block(meaning_block) if meaning_block else None,
        "abnormal_indicators": abnormal,     # 超出参考范围的项
        "pending_indicators": pending,       # 区间型结果，无法精确分层
        "normal_count": len(indicator_rows) - len(abnormal) - len(pending),
        # 1.3 肠道屏障功能评分（IBFC）与损伤分级
        "ibfc": {
            "paragraph": _text(ibfc_paragraph_block.get("text")),
            "cards": ibfc_cards,
            "score": _card_value(ibfc_cards, "IBFC评分"),
            "grade": _card_value(ibfc_cards, "损伤等级"),
            "damage_type": _card_value(ibfc_cards, "受损类型"),
            "bands": ibfc_bands,
            "selected_band": _selected_band(ibfc_bands),
        },
        # 1.4 受损类型与受损状况
        "damage_paragraph": _text(damage_block.get("text")),
        # 问卷背景：全部是自述，话术里必须保留「自述」限定
        "questionnaire": questionnaire,
        "questionnaire_highlights": _questionnaire_highlights(questionnaire.get("rows", [])),
        "method_table": method if _has_content(method.get("rows", [])) else None,
        "quality_table": quality if _has_content(quality.get("rows", [])) else None,
        "data_gaps": data_gaps,
        # 同事以后加的块（图示、量尺等）不会凭空消失
        "extra_blocks": _remaining_blocks(overview, [
            facts_block, summary_block, indicator_block, meaning_block,
            questionnaire_block, method_block, quality_block,
            ibfc_card_block, ibfc_band_block,
            ibfc_paragraph_block, damage_block,
        ]),
    }


def _card_value(cards: list[dict[str, str]], label: str) -> str:
    for card in cards:
        if card["label"] == label:
            return card["value"]
    return ""


def _split_indicators(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """把检测项分成「需要关注」与「区间型待明确」两类。"""
    abnormal, pending = [], []
    for row in rows:
        values = list(row.values())
        name = row.get("检测指标", values[0] if values else "")
        result = row.get("检测结果", values[1] if len(values) > 1 else "")
        evaluation = row.get("结果评价", "")
        item = {
            "name": name,
            "result": result,
            "reference": row.get("参考范围") or row.get("参考区间", ""),
            "evaluation": evaluation,
            "score": row.get("单项评分", ""),
        }
        if evaluation and not any(label in evaluation for label in NORMAL_LABELS):
            # 「需核对后评估」「>3.0」这类既异常又无法精确分层的，归到 pending
            if _is_range_result(result) or "核对" in evaluation or "补充" in evaluation:
                pending.append(item)
            else:
                abnormal.append(item)
    return abnormal, pending


def _questionnaire_highlights(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """挑出有内容的自述项；「无」「待补充」这类等于没填。"""
    highlights = []
    for row in rows:
        values = list(row.values())
        if not values:
            continue
        label = values[0]
        answer = values[1] if len(values) > 1 else ""
        if label in QUESTIONNAIRE_SKIP or not answer:
            continue
        stripped = answer.replace("（自述）", "").strip()
        if stripped in EMPTY_ANSWERS or "待补充" in stripped:
            continue
        highlights.append({"label": label, "answer": answer})
    return highlights


# ---------------------------------------------------------------- 第二章

def build_part2_context(data: dict) -> dict[str, Any]:
    """第二章 相关疾病风险评估（2.1–2.7）。"""
    data = _dict(data)
    risks = _section(data, SECTION_DISEASE_RISKS)

    background_block = _find_block(risks, "table", "临床背景")
    systems = []
    consumed = [background_block]
    total = assessed_total = 0
    assessed_rows: list[dict[str, str]] = []
    for block in _blocks(risks, "table"):
        if block is background_block:
            continue
        if ((block.get("title") == ATTENTION_OVERVIEW_TITLE
                and block.get("columns") == ATTENTION_OVERVIEW_COLUMNS)
                or (block.get("title") == RISK_OVERVIEW_TITLE
                    and block.get("columns") == RISK_OVERVIEW_COLUMNS)):
            # This chart repeats the system tables. Do not count its first
            # column (system name) as another disease in report explanations.
            consumed.append(block)
            continue
        columns = _list(block.get("columns"))
        if "疾病" not in columns or not any(column in columns for column in ("风险指数", "风险等级")):
            # Clinical/safety tables are supporting content, not extra risks.
            # Leave them unconsumed so extra_blocks keeps the original facts.
            continue
        consumed.append(block)
        table = _table(block)
        rows = []
        for row in table["rows"]:
            item = {
                "disease": row.get("疾病", ""),
                "risk_index": row.get("风险指数", ""),
                "risk_level": row.get("风险等级", ""),
                "mechanism": row.get("机制关联", ""),
            }
            rows.append(item)
            total += 1
            if item["risk_level"] and not any(
                label in item["risk_level"] for label in UNASSESSED_LABELS
            ):
                assessed_total += 1
                assessed_rows.append({**item, "system": table["title"]})
        systems.append({"title": table["title"], "rows": rows})

    summary_block = _find_block(risks, "list", "小结") or (_blocks(risks, "list") or [{}])[0]
    consumed.append(summary_block)
    return {
        "chapter": "第二章 相关疾病风险评估",
        "systems": systems,
        # 个体临床背景：本次/既往检查摘要，是本章唯一的个性化素材
        "clinical_background": _table(background_block) if background_block else None,
        "summary_points": _string_items(summary_block),
        "stats": {
            "total": total,
            "assessed": assessed_total,
            "unassessed": total - assessed_total,
            # 本次真正做出分层的条目；为空说明整章都没有分层结果
            "assessed_rows": assessed_rows,
        },
        "extra_blocks": _remaining_blocks(risks, consumed),
    }


# ---------------------------------------------------------------- 附录

def build_references(data: dict) -> list[dict[str, Any]]:
    """第五章附录只取参考文献；编号由程序按顺序生成。

    字段与评估报告不同：这里是 citation / publisher / year / url，
    展示格式为「citation. publisher. year.」。
    """
    appendix = _section(_dict(data), SECTION_APPENDIX)
    blocks = _blocks(appendix, "references")
    main_block = _find_block(appendix, "references", "参考文献")
    if not main_block and blocks:
        main_block = blocks[0]
    references = _reference_items(main_block, prefix="")
    # 「营养与微生态干预补充依据」等补充文献单独成列，编号加「补」前缀，
    # 与正文里的 [补19] 这类标记对得上。
    for block in blocks:
        if block is main_block:
            continue
        references.extend(_reference_items(block, prefix="补"))
    return references


def _reference_items(block: dict | None, *, prefix: str) -> list[dict[str, Any]]:
    items = []
    for index, item in enumerate(_list((block or {}).get("items")), start=1):
        if not isinstance(item, dict):
            continue
        citation = _text(item.get("citation"))
        if not citation:
            continue
        items.append({
            "number": f"{prefix}{index}",
            "citation": citation,
            "publisher": _text(item.get("publisher")),
            "year": _text(item.get("year")),
            "url": _text(item.get("url")),
            "doi": _text(item.get("doi")),
            "supplementary": bool(prefix),
        })
    return items


# ---------------------------------------------------------------- 速查卡

def build_quick_card(data: dict) -> dict[str, Any]:
    """关键数据速查卡：逐字摘自报告，不经过模型。"""
    part1 = build_part1_context(data)
    meta = build_meta(data)
    return {
        "subject": {
            "name": meta["name"],
            "gender": meta["gender"],
            "age": meta["age"],
            "report_date": meta["report_date"],
            "report_id": meta["report_id"],
        },
        "indicator_rows": part1["indicator_table"].get("rows", []),
        "ibfc": {
            "score": part1["ibfc"]["score"],
            "grade": part1["ibfc"]["grade"],
            "damage_type": part1["ibfc"]["damage_type"],
            "selected_band": part1["ibfc"]["selected_band"],
        },
        "inflammation_band": "",  # Retained empty for callback compatibility.
        "abnormal_indicators": part1["abnormal_indicators"],
        "pending_indicators": part1["pending_indicators"],
        "questionnaire_highlights": part1["questionnaire_highlights"],
    }


# ---------------------------------------------------------------- 汇总入口

def build_gut_barrier_contexts(data: dict) -> dict[str, Any]:
    """一次性构建肠道屏障报告解读所需的全部上下文。"""
    data = _dict(data)
    return {
        "meta": build_meta(data),
        "quick_card": build_quick_card(data),
        "part1": build_part1_context(data),
        "part2": build_part2_context(data),
        "references": build_references(data),
    }


def dumps(value: Any) -> str:
    """提示词渲染用：紧凑但保留中文的 JSON 文本。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "NORMAL_LABELS",
    "SECTION_APPENDIX",
    "SECTION_DISEASE_RISKS",
    "SECTION_OVERVIEW",
    "UNASSESSED_LABELS",
    "build_gut_barrier_contexts",
    "build_meta",
    "build_part1_context",
    "build_part2_context",
    "build_quick_card",
    "build_references",
    "dumps",
]
