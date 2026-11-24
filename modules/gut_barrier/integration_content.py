"""Callback-owned chapter context and chart values for newly published reports.

Use existing paragraph/table/list contracts. This is a projection of the final
disease tables, never a second scoring model or a template disease inventory.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re


RISK_COLUMNS = ["疾病", "风险指数", "风险等级", "机制关联"]
QUALITATIVE_FOLLOWUP_LEVEL = "随访关注（定性）"
ATTENTION_OVERVIEW_TITLE = "本次健康关注指数总览"
ATTENTION_OVERVIEW_COLUMNS = ["系统", "疾病", "健康关注指数", "关注等级"]
DISEASE_INTRODUCTION = (
    "本章结合本次检测结果与已提供的健康资料，按系统汇总疾病评估及健康关注方向，"
    "并结合肠道屏障与炎症状态解读。下方总览与各系统明细使用同一组评估结果。"
    "健康关注指数用于安排资料核实与随访关注的先后，不等同于患病概率或疾病诊断；"
    "定性随访项目不赋予数值，独立模型结果按其原有标尺解释。"
)
PRODUCT_DISEASE_INTRODUCTION = (
    "肠道屏障受损、慢性低度炎症与多种慢性疾病及机体衰老有关。"
    "本章基于本次检测结果与相关健康评估，评估相关疾病风险。"
    "关注等级（基础关注、加强关注、优先评估）为基于检测指标与健康资料的综合评估结果，"
    "用于风险提示与预防参考，不构成疾病诊断。"
)
PRODUCT_ATTENTION_NOTICE = (
    "关注指数为自定义、尚未经临床验证的0–10分关注标尺："
    "1–3分为基础关注，4–6分为加强关注，7–10分为优先评估。"
)


def _cells(row):
    cells = row.get("cells") if isinstance(row, dict) else row
    return cells if isinstance(cells, list) else []


def _system_name(table):
    title = table.get("title") or ""
    return re.sub(r"(?:疾病)?风险评估$", "", re.sub(r"^2\.\d+\s+", "", title))


def _numeric_attention(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and 0 <= value <= 10)


def _overview_rows(tables, attention_mode, *, model_names=()):
    if not attention_mode:
        return []
    result = []
    for table in tables:
        for row in table.get("rows", []):
            cells = _cells(row)
            if len(cells) != 4:
                continue
            name, score, level, mechanism = cells
            if (name in model_names
                    or str(mechanism or "").startswith("本项数值来自独立的已配置模型")
                    or not isinstance(level, str)
                    or not ("关注" in level or "优先评估" in level)):
                continue
            qualitative = score is None and (level == "随访关注" or "定性" in level)
            if _numeric_attention(score) or qualitative:
                result.append([_system_name(table), name, score, level])
    return result


def _summary_text(tables, overview_rows):
    systems = list(dict.fromkeys(_system_name(table) for table in tables if table.get("rows")))
    count = sum(len(table.get("rows", [])) for table in tables)
    if not count:
        return ("本次资料尚未形成可由相应证据支持的具体疾病关注方向，不能据此解释为低风险或已排除疾病。"
                "肠道屏障与炎症检测结果仍需结合症状、病史及相关检查共同解读；"
                "后续应先完善缺失或待核实的资料，再确定需要关注的系统与复评内容。")
    text = f"本章依据本次可用资料，汇总了{'、'.join(systems)}的{count}项疾病评估或健康关注方向。"
    quantitative = sum(_numeric_attention(row[2]) for row in overview_rows)
    qualitative = len(overview_rows) - quantitative
    if quantitative:
        text += f"其中{quantitative}项提供健康关注指数，用于随访关注排序。"
    if qualitative:
        text += f"另有{qualitative}项为定性随访，不赋予数值分层。"
    return text + (
        "各系统的评估需将肠道屏障、炎症指标与相应专项资料共同考虑，机制关联不代表已患病或已确定肠道来源。"
        "后续结合本章评估结果、症状变化、既往病史和对应检查安排复评，"
        "并与第一章的屏障及炎症结果一起观察。"
    )


def complete_disease_content(blocks, *, product_feedback=False, model_names=(),
                             ordered_reference_layout=False, assessment=None):
    """Enrich a new current callback, preserving every existing direction.

    Legacy layouts retain their original list after the summary paragraph.
    The new ordered-reference layout publishes one synthesis paragraph and
    omits the raw clinical ledger without changing its internal assessment.
    Explicit empty chart rows mean no comparable attention values are available.
    """
    blocks = deepcopy(blocks)
    if ordered_reference_layout:
        # Only new v10 publication enters here. The source assessment and its
        # full clinical ledger stay internal; frozen callbacks are not rebuilt.
        clinical_columns = ["检查日期", "项目", "时间属性", "原始检查摘要"]
        blocks = [block for block in blocks if not (
            block.get("kind") == "table" and (
                block.get("columns") == clinical_columns
                or re.fullmatch(r"(?:2\.\d+\s+)?个体临床背景", block.get("title") or "")))
            and not re.fullmatch(r"(?:2\.\d+\s+)?本章小结", block.get("title") or "")]
    tables = [block for block in blocks
              if block.get("kind") == "table" and block.get("columns") == RISK_COLUMNS]
    attention_mode = any(block.get("kind") == "callout" and block.get("title") == "本次关注指数"
                         for block in blocks)
    chart = next((block for block in blocks if block.get("kind") == "table"
                  and block.get("title") == ATTENTION_OVERVIEW_TITLE
                  and block.get("columns") == ATTENTION_OVERVIEW_COLUMNS), None)
    if chart is None:
        chart = {"kind": "table", "title": ATTENTION_OVERVIEW_TITLE,
                 "columns": ATTENTION_OVERVIEW_COLUMNS[:],
                 "rows": _overview_rows(tables, attention_mode, model_names=model_names)}
        first_table = next((i for i, block in enumerate(blocks) if block in tables),
                           next((i for i, block in enumerate(blocks) if block.get("kind") == "list"), len(blocks)))
        blocks.insert(first_table, chart)
    introduction = {"kind": "paragraph", "title": None,
                    "text": PRODUCT_DISEASE_INTRODUCTION if product_feedback else DISEASE_INTRODUCTION}
    if introduction not in blocks:
        blocks.insert(0, introduction)
    if ordered_reference_layout:
        from .chapters import consolidated_disease_summary
        systems = [_system_name(table) for table in tables if table.get("rows")]
        blocks.append({"kind": "paragraph", "title": f"2.{len(tables) + 1} 本章小结",
                       "text": consolidated_disease_summary(assessment, systems=systems)})
        return blocks
    for index, block in enumerate(blocks):
        if (block.get("kind") == "list"
                and re.fullmatch(r"2\.\d+ 本章小结", block.get("title") or "")):
            previous = blocks[index - 1] if index else {}
            if previous.get("kind") != "paragraph" or previous.get("title") != block["title"]:
                if product_feedback:
                    count = sum(len(table.get("rows", [])) for table in tables)
                    systems = list(dict.fromkeys(_system_name(table) for table in tables if table.get("rows")))
                    summary = (f"本次主要关注{'、'.join(systems)}，共{count}项相关问题。" if count else
                               "本次资料尚不足以明确具体疾病关注方向。")
                else:
                    summary = _summary_text(tables, chart["rows"])
                blocks.insert(index, {"kind": "paragraph", "title": block["title"], "text": summary})
            break
    return blocks
