# -*- coding: utf-8 -*-
"""报告解读（explain）的合规校验与自检。

职责：
1. **禁用语替换**：扫描所有【照读】文本，命中禁用语的句子整句丢弃；整段被
   清空时回退到兜底文案。解读人手里的话术是要一字不改念出去的，留一句
   违规表述比少一句更危险。
2. **必含声明兜底**：菌群风险「不等于疾病诊断」、解读人角色边界两条声明
   缺失时由程序补上，不依赖模型是否听话。
3. **自检清单**：结构完整性、禁用语、合规声明、归一化改动，结果随报文返回
   （《解读参考提示词》第九章要求手册末尾附自查结果）。

只有会被念给用户听的字段参与扫描：禁用语对照表、沟通禁忌、限定语提示这类
【内部】文本本身就要引用禁用词，扫它们只会误报。
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------- 常量

# 第六章禁用语对照表；`word` 用于全文扫描，命中即视为生成失败
FORBIDDEN_WORDS: tuple[dict[str, str], ...] = (
    {"word": "确诊", "why": "解读人无诊断资质，属越权行医表述", "instead": "报告提示异常或风险，建议到相应科室复查确认"},
    {"word": "你得了", "why": "越权下诊断", "instead": "报告显示这项指标超出了参考范围"},
    {"word": "您得了", "why": "越权下诊断", "instead": "报告显示这项指标超出了参考范围"},
    {"word": "治愈", "why": "疗效承诺，违规且有法律风险", "instead": "有助于改善"},
    {"word": "根治", "why": "疗效承诺，违规且有法律风险", "instead": "支持恢复"},
    {"word": "治好", "why": "疗效承诺", "instead": "有助于改善"},
    {"word": "替代药物", "why": "干预医嘱，极度危险", "instead": "营养素与生活方式是辅助支持，用药请遵医嘱"},
    {"word": "可以不吃药", "why": "干预医嘱，极度危险", "instead": "用药请遵医嘱，不要自行调整"},
    {"word": "百分百", "why": "绝对化承诺，广告合规禁止", "instead": "多数人坚持一段时间后指标会有改善"},
    {"word": "肯定有效", "why": "绝对化承诺", "instead": "这是目前证据比较充分的做法"},
    {"word": "特效", "why": "绝对化用语", "instead": "证据比较充分的做法"},
    {"word": "神效", "why": "绝对化用语", "instead": "证据比较充分的做法"},
    {"word": "不用去医院", "why": "弱化就医必要性，可能造成延误", "instead": "这项请务必先去复查，其他我们再安排"},
    {"word": "绝症", "why": "恐吓式表述，违背伦理", "instead": "早发现早处理的窗口期"},
)

MICROBIOME_DISCLAIMER = "这是菌群模型评估出的风险倾向，不等于疾病诊断。"
ROLE_STATEMENT_KEYWORD = "对接医生"

# 合规必含声明：缺任何一条都算自检不通过
REQUIRED_STATEMENTS: tuple[tuple[str, str], ...] = (
    ("角色边界声明", ROLE_STATEMENT_KEYWORD),
    ("菌群风险声明", "不等于疾病诊断"),
)

# 否定语境里的禁用词不算命中：「并不等于已经确诊」「不能替代药物」这类
# 恰恰是合规说法，误报会让自检结果失去意义。
_NEGATION_MARKERS: tuple[str, ...] = ("不", "非", "未", "没", "无需", "别")
_NEGATION_WINDOW = 8

# 会被念给用户听的字段；扫描与数值核对都只看这些内容
READABLE_FIELDS: frozenset[str] = frozenset({
    "question", "answer", "text", "talk", "plain", "intro", "confirm",
    "summary", "follow_up", "contact", "self_intro", "role_statement", "agenda",
    "goal", "analogy", "must_say", "opening", "closing",
})
SCAN_SKIP_KEYS: frozenset[str] = frozenset({
    "forbidden_words", "forbidden", "cautions", "certainty_note",
    "data_gaps", "partial_reasons", "confidential_note", "evidence",
    "self_check", "reader_notice", "quick_card",
    # three_things 是从上下文原样搬来的问题名，属于数据不是话术，不参与清洗
    "three_things", "evidence_refs",
})

# 句子切分：中文句末标点与换行
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；!?;])|\n")


# ---------------------------------------------------------------- 扫描

def _has_negation(prefix: str) -> bool:
    return any(marker in prefix for marker in _NEGATION_MARKERS)


def scan_forbidden(text: str) -> set[str]:
    """扫描一段文本中的禁用语，跳过否定语境。"""
    hits: set[str] = set()
    if not text:
        return hits
    for entry in FORBIDDEN_WORDS:
        word = entry["word"]
        start = text.find(word)
        while start != -1:
            if not _has_negation(text[max(0, start - _NEGATION_WINDOW):start]):
                hits.add(word)
                break
            start = text.find(word, start + len(word))
    return hits


def sentences(text: str) -> list[str]:
    parts = [part.strip() for part in _SENTENCE_SPLIT.split(text or "") if part and part.strip()]
    return parts


def sanitize_text(text: str) -> tuple[str, set[str]]:
    """丢弃含禁用语的整句；返回 (清理后的文本, 命中的禁用语)。"""
    hits = scan_forbidden(text)
    if not hits:
        return text, set()
    kept = [sentence for sentence in sentences(text) if not scan_forbidden(sentence)]
    return "".join(kept), hits


def readable_texts(node: Any) -> list[str]:
    """摊平所有【照读】文本。

    判定规则：``kind == "script"`` 的块整块算照读内容；其余对象只取
    READABLE_FIELDS 里的字段；SCAN_SKIP_KEYS 整枝跳过。
    """
    texts: list[str] = []
    if isinstance(node, dict):
        is_script = node.get("kind") == "script"
        for key, value in node.items():
            if key in SCAN_SKIP_KEYS:
                continue
            if isinstance(value, str):
                if is_script or key in READABLE_FIELDS:
                    texts.append(value)
                continue
            texts.extend(readable_texts(value))
    elif isinstance(node, list):
        for value in node:
            texts.extend(readable_texts(value))
    return texts


# ---------------------------------------------------------------- 替换

def _clean_string(
    value: str,
    fallback: Any,
    issues: list[str],
    path: str,
    *,
    in_list: bool,
) -> str:
    cleaned, hits = sanitize_text(value)
    if not hits:
        return value
    if cleaned.strip():
        issues.append(f"{path} 命中禁用语（{'、'.join(sorted(hits))}），已删除相关语句")
        return cleaned
    # 列表条目整段空了就让 prune_empty 把这条删掉，不要张冠李戴地套兜底文案
    replacement = "" if in_list else (fallback if isinstance(fallback, str) else "")
    issues.append(
        f"{path} 命中禁用语（{'、'.join(sorted(hits))}），"
        + ("整条已删除" if in_list or not replacement else "整段替换为兜底文案")
    )
    return replacement


def _sanitize_node(
    node: Any,
    fallback: Any,
    issues: list[str],
    path: str,
    *,
    readable: bool = False,
    in_list: bool = False,
) -> Any:
    """递归清理【照读】文本；``readable`` 沿着 script 块与照读字段向下传递。"""
    if isinstance(node, dict):
        is_script = node.get("kind") == "script"
        result = dict(node)
        for key, value in node.items():
            if key in SCAN_SKIP_KEYS:
                continue
            child_path = f"{path}.{key}" if path else key
            child_fallback = fallback.get(key) if isinstance(fallback, dict) else None
            child_readable = is_script or key in READABLE_FIELDS
            if isinstance(value, str):
                if child_readable:
                    result[key] = _clean_string(value, child_fallback, issues, child_path, in_list=in_list)
                continue
            result[key] = _sanitize_node(
                value, child_fallback, issues, child_path,
                readable=child_readable, in_list=in_list,
            )
        return result
    if isinstance(node, list):
        fallback_list = fallback if isinstance(fallback, list) else []
        cleaned_items = []
        for index, item in enumerate(node):
            child_fallback = fallback_list[index] if index < len(fallback_list) else None
            child_path = f"{path}[{index}]"
            if isinstance(item, str):
                cleaned_items.append(
                    _clean_string(item, child_fallback, issues, child_path, in_list=True)
                    if readable else item
                )
                continue
            cleaned_items.append(_sanitize_node(
                item, child_fallback, issues, child_path,
                readable=readable, in_list=True,
            ))
        kept = [item for item in cleaned_items if not (isinstance(item, str) and not item.strip())]
        # 整段话术被清空（例如全部命中禁用语）时回退到兜底文案，不留空列表
        if not kept and fallback_list and all(isinstance(item, str) for item in fallback_list):
            return list(fallback_list)
        return kept
    return node


# 这些列表里的条目一旦正文被清空就没有意义，整条删掉
LIST_TEXT_KEYS: dict[str, str] = {
    "faq": "answer",
    "qa_table": "answer",
    "key_points": "text",
    "glossary": "plain",
    "evidence_levels": "plain",
    "visual_guide": "talk",
}


def prune_empty(node: Any) -> Any:
    """删掉正文被清空的列表条目（问答、要点、释义等）。"""
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            if key in LIST_TEXT_KEYS and isinstance(value, list):
                text_key = LIST_TEXT_KEYS[key]
                value = [
                    item for item in value
                    if not isinstance(item, dict) or str(item.get(text_key) or "").strip()
                ]
            result[key] = prune_empty(value)
        return result
    if isinstance(node, list):
        return [prune_empty(item) for item in node]
    return node


def apply_compliance(
    payload: dict[str, Any],
    *,
    fallback: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """对整份报文做禁用语替换与必含声明兜底。

    ``fallback`` 是同一份上下文拼装出的兜底手册，用于整段被清空时取同位置文案。
    """
    issues: list[str] = []
    cleaned = prune_empty(_sanitize_node(payload, fallback or {}, issues, ""))

    # 必含声明：缺了就由程序补，不指望模型每次都写
    for section in cleaned.get("sections") or []:
        if section.get("part_no") != 2:
            continue
        talk = section.get("microbiome_talk")
        if isinstance(talk, dict) and "不等于疾病诊断" not in str(talk.get("must_say") or ""):
            talk["must_say"] = MICROBIOME_DISCLAIMER
            issues.append("sections[2].microbiome_talk.must_say 缺菌群风险声明，已补")

    opening = (cleaned.get("preface") or {}).get("opening_script")
    if isinstance(opening, dict):
        statement = str(opening.get("role_statement") or "")
        if ROLE_STATEMENT_KEYWORD not in statement:
            opening["role_statement"] = "我负责帮您看懂这份报告，涉及诊断和用药的问题，我会帮您对接医生。"
            issues.append("preface.opening_script.role_statement 缺角色边界声明，已补")
    return cleaned, issues


# ---------------------------------------------------------------- 自检

_REQUIRED_SECTION_KEYS = ("key_points", "script", "visual_guide", "faq", "cautions")
CHECK_PASS = "通过"


def run_self_check(
    payload: dict[str, Any],
    *,
    issues: list[str] | None = None,
    expected_sections: int = 4,
    required_statements: tuple[tuple[str, str], ...] = REQUIRED_STATEMENTS,
) -> list[dict[str, str]]:
    """生成自检清单；``issues`` 是归一化与合规替换过程中记录的改动。

    ``expected_sections`` 与 ``required_statements`` 按报告类型传入：
    报告解读是四组五件套 + 菌群风险声明，肠道与炎症报告是两章 + 自己的必含声明。
    """
    checks: list[dict[str, str]] = []
    sections = payload.get("sections") or []
    checks.append({
        "item": "章节对应完整性",
        "standard": f"{expected_sections} 组五件套齐全",
        "result": (CHECK_PASS if len(sections) == expected_sections
                   else f"不通过（{len(sections)}/{expected_sections}）"),
    })

    missing = [
        f"第{section.get('part_no')}部分缺 {key}"
        for section in sections
        for key in _REQUIRED_SECTION_KEYS
        if not section.get(key)
    ]
    checks.append({
        "item": "五件套完整性",
        "standard": "每组均含核心要点卡、解读话术、看图看表指引、高频提问、沟通禁忌",
        "result": CHECK_PASS if not missing else "不通过（" + "；".join(missing[:3]) + "）",
    })

    joined = "\n".join(readable_texts(payload))
    hits = sorted(scan_forbidden(joined))
    checks.append({
        "item": "禁用语扫描",
        "standard": "照读话术中禁用语零命中",
        "result": CHECK_PASS if not hits else "不通过（命中：" + "、".join(hits) + "）",
    })

    missing_statements = [name for name, keyword in required_statements if keyword not in joined]
    checks.append({
        "item": "合规声明",
        "standard": "含 " + "、".join(name for name, _ in required_statements),
        "result": CHECK_PASS if not missing_statements else "不通过（缺：" + "、".join(missing_statements) + "）",
    })

    fixes = list(issues or [])
    checks.append({
        "item": "归一化与合规修正",
        "standard": "记录程序对模型输出所做的删改，便于排查提示词问题",
        "result": f"{CHECK_PASS}（无改动）" if not fixes else f"已修正 {len(fixes)} 处：" + "；".join(fixes[:5]),
    })
    return checks


__all__ = [
    "CHECK_PASS",
    "LIST_TEXT_KEYS",
    "prune_empty",
    "FORBIDDEN_WORDS",
    "MICROBIOME_DISCLAIMER",
    "REQUIRED_STATEMENTS",
    "apply_compliance",
    "readable_texts",
    "run_self_check",
    "sanitize_text",
    "scan_forbidden",
    "sentences",
]
