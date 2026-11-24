# -*- coding: utf-8 -*-
"""报告解读（explain）模型输出的归一化。

模型只负责把评估报告的书面语改写成可照读的口语话术，其余都由程序钉死：

1. **条数与字数**：超出提示词约定的条数一律截断，过长的话术按句子边界收口；
   条数不足时从同一份上下文拼出的兜底内容里补。
2. **结构对齐**：问题、阶段、检查项、支柱的顺序与标题照抄上下文，模型只能
   贡献话术；编号、优先级、文献编号一律以上下文为准。
3. **anchor 与枚举校验**：章节号必须是评估报告里真实存在的（1.1–4.5），
   `visual_guide.target` 只能取四个枚举值之一。
4. **数值防篡改**：照读话术里出现的数字必须能在上下文里找到，
   否则整句丢弃——这是红线 1「数据逐字」的最后一道保险。

禁用语替换与必含声明兜底在 ``explain_compliance``，本模块只管结构与数值。
"""

from __future__ import annotations

import json
import re
from typing import Any

from modules.report.explain_compliance import READABLE_FIELDS
from modules.report.explain_compliance import SCAN_SKIP_KEYS
from modules.report.explain_compliance import prune_empty
from modules.report.explain_compliance import sentences
from modules.report.explain_defaults import PRODUCT_FORBIDDEN
from modules.report.explain_defaults import SECTION_TITLES
from modules.report.explain_defaults import default_opening
from modules.report.explain_defaults import default_section

# ---------------------------------------------------------------- 约束

# 与 prompts/report/followups/explain_part*.txt 的「字数与条数约束」保持一致，
# 改提示词时这里要同步改。
LIMITS: dict[str, Any] = {
    "key_points": {"min": 3, "max": 5, "chars": 40},
    "script_body": {"max": 5, "chars": 120},
    "script_line": {"chars": 120},
    "visual_guide": {"max": 4, "order": 4, "chars": 120},
    "faq": {"min": 5, "max": 8, "chars": 120},
    "cautions": {"max": 3, "chars": 50},
    "five_steps": {"chars": 200},
    "pillar": {"chars": 250},
    "plan": {"chars": 120},
    "exam": {"chars": 200},
    "bridge": {"chars": 200},
    "qa_table": {"min": 8, "max": 12, "chars": 120},
    "evidence_levels": {"min": 2, "max": 4, "chars": 60},
    "glossary": {"chars": 60},
    "limits": {"chars": 150},
    "opening": {"chars": 80},
    "wrap": {"chars": 80},
}

# 允许出现的章节号：评估报告实际有的四部分
ANCHOR_WHITELIST: frozenset[str] = frozenset(
    [f"1.{i}" for i in range(1, 5)]
    + [f"2.{i}" for i in range(1, 7)]
    + [f"3.{i}" for i in range(1, 8)]
    + [f"4.{i}" for i in range(1, 6)]
)

VISUAL_TARGETS: frozenset[str] = frozenset({"radar", "score", "table", "text"})
DEFAULT_TARGET = "text"

PILLAR_CODES: tuple[str, ...] = ("lifestyle", "microbiome", "nutrition")

FIVE_STEP_KEYS: tuple[str, ...] = ("status", "causes", "mechanism", "outlook", "action")

# 数值防篡改：话术里常见的「三十分钟」「四个阶段」这类小数字不算数据，
# 只有更具体的数值才必须在上下文里找得到。
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_SAFE_NUMBERS: frozenset[str] = frozenset(str(n) for n in range(0, 13))
# 句末收口允许的超出比例：宁可多留半句，也不要把话术切在半中间
_CLIP_TOLERANCE = 1.3


# ---------------------------------------------------------------- 基础工具

def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _clip(text: str, limit: int) -> str:
    """按句子边界收口；实在没有边界再硬截。"""
    text = _text(text)
    if len(text) <= limit * _CLIP_TOLERANCE:
        return text
    kept: list[str] = []
    length = 0
    for sentence in sentences(text):
        if kept and length + len(sentence) > limit:
            break
        kept.append(sentence)
        length += len(sentence)
    if kept:
        return "".join(kept)
    return text[:limit] + "…"


def _str_list(value: Any, *, limit: int, chars: int) -> list[str]:
    items = [_clip(item, chars) for item in value if _text(item)] if isinstance(value, list) else []
    return [item for item in items if item][:limit]


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _anchor(value: Any, whitelist: frozenset[str] = ANCHOR_WHITELIST) -> str:
    """章节号必须是报告里真实存在的；白名单由调用方按报告类型传入。"""
    anchor = _text(value)
    return anchor if anchor in whitelist else ""


def _script_block(text: str) -> dict[str, str]:
    return {"kind": "script", "text": text}


def _top_up(items: list[Any], fallback_items: list[Any], *, minimum: int, key: str) -> list[Any]:
    """条数不足时用兜底内容补齐，按 ``key`` 去重；兜底也不够就保持现状。"""
    if len(items) >= minimum:
        return items
    seen = {_text(item.get(key)) for item in items if isinstance(item, dict)}
    for candidate in fallback_items:
        if len(items) >= minimum:
            break
        if not isinstance(candidate, dict):
            continue
        if _text(candidate.get(key)) in seen:
            continue
        items.append(candidate)
        seen.add(_text(candidate.get(key)))
    return items


# ---------------------------------------------------------------- 数值防篡改

def allowed_numbers(contexts: dict[str, Any]) -> frozenset[str]:
    """上下文里出现过的全部数字，外加小整数白名单。"""
    raw = json.dumps(contexts, ensure_ascii=False)
    found = set(_NUMBER_RE.findall(raw))
    # 「0.0296%」在上下文里，话术写成「0.0296」也应放行
    found.update(number.rstrip("0").rstrip(".") for number in list(found) if "." in number)
    # 章节号（如「见报告 4.3」）是指路用的，不是检测数值
    found.update(ANCHOR_WHITELIST)
    return frozenset(found | _SAFE_NUMBERS)


def _offending_numbers(text: str, allowed: frozenset[str]) -> set[str]:
    return {number for number in _NUMBER_RE.findall(text or "") if number not in allowed}


def _scrub_string(
    value: str,
    fallback: Any,
    allowed: frozenset[str],
    issues: list[str],
    path: str,
    *,
    in_list: bool,
) -> str:
    bad = _offending_numbers(value, allowed)
    if not bad:
        return value
    kept = [sentence for sentence in sentences(value) if not _offending_numbers(sentence, allowed)]
    cleaned = "".join(kept)
    if not cleaned.strip():
        # 列表条目整段空了就让 prune_empty 删掉这一条，不要套用别人的兜底文案
        cleaned = "" if in_list else (fallback if isinstance(fallback, str) else "")
    issues.append(
        f"{path} 出现评估报告中没有的数字（{'、'.join(sorted(bad))}），已删除相关语句"
    )
    return cleaned


def scrub_numbers(
    node: Any,
    fallback: Any,
    allowed: frozenset[str],
    issues: list[str],
    path: str = "",
    *,
    readable: bool = False,
    in_list: bool = False,
) -> Any:
    """照读话术里出现上下文没有的数字时，整句丢弃；``readable`` 向下传递。"""
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
                    result[key] = _scrub_string(
                        value, child_fallback, allowed, issues, child_path, in_list=in_list,
                    )
                continue
            result[key] = scrub_numbers(
                value, child_fallback, allowed, issues, child_path,
                readable=child_readable, in_list=in_list,
            )
        return result
    if isinstance(node, list):
        fallback_list = fallback if isinstance(fallback, list) else []
        items = []
        for index, item in enumerate(node):
            child_fallback = fallback_list[index] if index < len(fallback_list) else None
            child_path = f"{path}[{index}]"
            if isinstance(item, str):
                items.append(
                    _scrub_string(item, child_fallback, allowed, issues, child_path, in_list=True)
                    if readable else item
                )
                continue
            items.append(scrub_numbers(
                item, child_fallback, allowed, issues, child_path,
                readable=readable, in_list=True,
            ))
        return [item for item in items if not (isinstance(item, str) and not item.strip())]
    return node


# ---------------------------------------------------------------- 五件套

def _normalize_key_points(
    value: Any,
    fallback: list[Any],
    issues: list[str],
    path: str,
    *,
    anchors: frozenset[str] = ANCHOR_WHITELIST,
) -> list[dict[str, str]]:
    rule = LIMITS["key_points"]
    points = []
    for item in _dict_list(value):
        text = _clip(item.get("text"), rule["chars"])
        if not text:
            continue
        anchor = _anchor(item.get("anchor"), anchors)
        if item.get("anchor") and not anchor:
            issues.append(f"{path}.key_points 的 anchor {item.get('anchor')!r} 不是报告里的章节号，已清空")
        points.append({"text": text, "anchor": anchor})
    if len(points) > rule["max"]:
        issues.append(f"{path}.key_points 超出 {rule['max']} 条，已截断")
        points = points[:rule["max"]]
    points = _top_up(points, fallback, minimum=rule["min"], key="text")
    return points or [item for item in fallback if isinstance(item, dict)]


def _normalize_script(value: Any, fallback: dict[str, Any], issues: list[str], path: str) -> dict[str, Any]:
    rule = LIMITS["script_body"]
    script = value if isinstance(value, dict) else {}
    fallback = fallback if isinstance(fallback, dict) else {}
    body = _str_list(script.get("body"), limit=rule["max"], chars=rule["chars"])
    if isinstance(script.get("body"), list) and len(script["body"]) > rule["max"]:
        issues.append(f"{path}.script.body 超出 {rule['max']} 段，已截断")
    if not body:
        body = list(fallback.get("body") or [])
    return {
        "kind": "script",
        "opening": _clip(script.get("opening"), LIMITS["script_line"]["chars"]) or _text(fallback.get("opening")),
        "body": body,
        "closing": _clip(script.get("closing"), LIMITS["script_line"]["chars"]) or _text(fallback.get("closing")),
    }


def _normalize_visual_guide(
    value: Any,
    fallback: list[Any],
    issues: list[str],
    path: str,
    *,
    anchors: frozenset[str] = ANCHOR_WHITELIST,
    targets: frozenset[str] = VISUAL_TARGETS,
    default_target: str = DEFAULT_TARGET,
) -> list[dict[str, Any]]:
    rule = LIMITS["visual_guide"]
    guides = []
    for item in _dict_list(value):
        talk = _clip(item.get("talk"), rule["chars"])
        if not talk:
            continue
        target = _text(item.get("target"))
        if target not in targets:
            if target:
                issues.append(f"{path}.visual_guide 的 target {target!r} 不是枚举值，已改为 {default_target}")
            target = default_target
        anchor = _anchor(item.get("anchor"), anchors)
        if item.get("anchor") and not anchor:
            issues.append(f"{path}.visual_guide 的 anchor {item.get('anchor')!r} 不是报告里的章节号，已清空")
        guides.append({
            "anchor": anchor,
            "target": target,
            "order": _str_list(item.get("order"), limit=rule["order"], chars=rule["chars"]),
            "talk": talk,
        })
    guides = guides[:rule["max"]]
    return guides or [item for item in fallback if isinstance(item, dict)]


def _normalize_faq(value: Any, fallback: list[Any], issues: list[str], path: str, rule_key: str = "faq") -> list[dict[str, str]]:
    rule = LIMITS[rule_key]
    items = []
    for item in _dict_list(value):
        question = _clip(item.get("question"), rule["chars"])
        answer = _clip(item.get("answer"), rule["chars"])
        if not question or not answer:
            continue
        items.append({
            "question": question,
            "answer": answer,
            "source": _text(item.get("source")),
        })
    if len(items) > rule["max"]:
        issues.append(f"{path}.{rule_key} 超出 {rule['max']} 条，已截断")
        items = items[:rule["max"]]
    items = _top_up(items, fallback, minimum=rule["min"], key="question")
    return items or [item for item in fallback if isinstance(item, dict)]


def _normalize_cautions(value: Any, fallback: dict[str, Any]) -> dict[str, list[str]]:
    rule = LIMITS["cautions"]
    source = value if isinstance(value, dict) else {}
    fallback = fallback if isinstance(fallback, dict) else {}
    result = {}
    for key in ("must_say", "easy_wrong", "forbidden"):
        items = _str_list(source.get(key), limit=rule["max"], chars=rule["chars"])
        result[key] = items or list(fallback.get(key) or [])
    return result


# ---------------------------------------------------------------- 各部分特有块

def _normalize_problems(value: Any, contexts: dict[str, Any], fallback: list[Any], issues: list[str]) -> list[dict[str, Any]]:
    """问题的编号、标题、优先级、文献编号一律以上下文为准，模型只贡献话术。"""
    chars = LIMITS["five_steps"]["chars"]
    by_no = {str(item.get("no")): item for item in _dict_list(value)}
    by_name = {_text(item.get("problem")): item for item in _dict_list(value)}
    fallback_by_no = {str(item.get("no")): item for item in fallback if isinstance(item, dict)}

    problems = []
    for source in contexts["part2"]["problems"]:
        no = str(source["no"])
        model = by_no.get(no) or by_name.get(source["problem"]) or {}
        base = fallback_by_no.get(no, {})
        base_steps = base.get("five_steps", {}) or {}
        model_steps = model.get("five_steps", {}) or {}
        steps = {}
        for key in FIVE_STEP_KEYS:
            text = _clip((model_steps.get(key) or {}).get("text"), chars)
            if not text:
                text = _text((base_steps.get(key) or {}).get("text"))
            steps[key] = _script_block(text)
        problems.append({
            "no": source["no"],
            "problem": source["problem"],
            "priority": source["priority"],
            # 限定语提示由程序按 certainty 判定，不交给模型
            "certainty_note": _text(base.get("certainty_note")),
            "five_steps": steps,
            "goal": _clip(model.get("goal"), chars) or _text(base.get("goal")),
            "analogy": _clip(model.get("analogy"), LIMITS["cautions"]["chars"]) or _text(base.get("analogy")),
            "evidence_refs": list(source.get("evidence_refs") or []),
        })
    extra = [key for key in by_no if key not in {str(p["no"]) for p in problems}]
    if extra:
        issues.append(f"sections[2].problems 多出上下文没有的条目 {extra}，已丢弃")
    return problems


def _normalize_pillars(value: Any, fallback: list[Any], issues: list[str]) -> list[dict[str, Any]]:
    chars = LIMITS["pillar"]["chars"]
    by_code = {_text(item.get("code")): item for item in _dict_list(value)}
    fallback_by_code = {_text(item.get("code")): item for item in fallback if isinstance(item, dict)}
    pillars = []
    for code in PILLAR_CODES:
        model = by_code.get(code) or {}
        base = fallback_by_code.get(code)
        text = _clip((model.get("script") or {}).get("text"), chars)
        if not text and base:
            text = _text((base.get("script") or {}).get("text"))
        if not text:
            continue
        pillars.append({
            "code": code,
            "title": _text(model.get("title")) or _text((base or {}).get("title")),
            "script": _script_block(text),
        })
    if len(by_code) > len(pillars):
        issues.append("sections[3].pillars 含未知 code，已按 lifestyle/microbiome/nutrition 重排")
    return pillars


def _normalize_plan(value: Any, contexts: dict[str, Any], fallback: list[Any]) -> list[dict[str, Any]]:
    chars = LIMITS["plan"]["chars"]
    by_phase = {_text(item.get("phase")): item for item in _dict_list(value)}
    fallback_by_phase = {_text(item.get("phase")): item for item in fallback if isinstance(item, dict)}
    plan = []
    for phase in contexts["part3"]["plan"]:
        name = phase["phase"]
        if not phase.get("content"):
            continue
        model = by_phase.get(name) or {}
        base = fallback_by_phase.get(name, {})
        text = _clip((model.get("script") or {}).get("text"), chars) or \
            _text((base.get("script") or {}).get("text")) or phase["content"]
        plan.append({"phase": name, "script": _script_block(text)})
    return plan


def _normalize_followup(value: Any, contexts: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    chars = LIMITS["exam"]["chars"]
    source = value if isinstance(value, dict) else {}
    fallback = fallback if isinstance(fallback, dict) else {}
    by_name = {_text(item.get("name")): item for item in _dict_list(source.get("items"))}
    fallback_items = {_text(item.get("name")): item for item in _dict_list(fallback.get("items"))}
    items = []
    for exam in contexts["part3"]["examinations"]:
        name = exam["name"]
        model = by_name.get(name) or {}
        base = fallback_items.get(name, {})
        items.append({
            "name": name,
            "purpose": exam.get("purpose", ""),
            "talk": _clip(model.get("talk"), chars) or _text(base.get("talk")) or exam.get("reason", ""),
            "confirm": _clip(model.get("confirm"), LIMITS["cautions"]["chars"]) or _text(base.get("confirm")),
        })
    return {
        "kind": "script",
        "intro": _clip(source.get("intro"), chars) or _text(fallback.get("intro")),
        "items": items,
    }


def _normalize_bridge(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    chars = LIMITS["bridge"]["chars"]
    source = value if isinstance(value, dict) else {}
    fallback = fallback if isinstance(fallback, dict) else {}
    steps = {}
    for key in ("step1_mechanism", "step2_need", "step3_direction", "step4_handoff"):
        steps[key] = _clip(source.get(key), chars) or _text(fallback.get(key))
    return {
        "kind": "script",
        **steps,
        # 合规红线固定，不让模型改写
        "forbidden": list(PRODUCT_FORBIDDEN),
    }


def _normalize_pairs(value: Any, fallback: list[Any], *, key: str, text_key: str, chars: int,
                     minimum: int = 0, maximum: int = 99) -> list[dict[str, str]]:
    items = []
    for item in _dict_list(value):
        name = _text(item.get(key))
        text = _clip(item.get(text_key), chars)
        if not name or not text:
            continue
        items.append({key: name, text_key: text})
    items = items[:maximum]
    items = _top_up(items, fallback, minimum=minimum, key=key)
    return items or [item for item in fallback if isinstance(item, dict)]


# ---------------------------------------------------------------- 对外入口

def normalize_section(
    part_no: int,
    section: Any,
    contexts: dict[str, Any],
    *,
    issues: list[str] | None = None,
    allowed: frozenset[str] | None = None,
) -> dict[str, Any]:
    """归一化一组五件套；``issues`` 记录程序做过的删改，写进自检清单。"""
    issues = issues if issues is not None else []
    source = section if isinstance(section, dict) else {}
    fallback = default_section(part_no, contexts)
    path = f"sections[{part_no}]"

    result: dict[str, Any] = {
        "part_no": part_no,
        "title": SECTION_TITLES[part_no],
        "key_points": _normalize_key_points(source.get("key_points"), fallback["key_points"], issues, path),
        "script": _normalize_script(source.get("script"), fallback["script"], issues, path),
    }

    if part_no == 2:
        result["problems"] = _normalize_problems(
            source.get("problems"), contexts, fallback.get("problems", []), issues,
        )
        talk = source.get("microbiome_talk") if isinstance(source.get("microbiome_talk"), dict) else {}
        base_talk = fallback.get("microbiome_talk", {})
        result["microbiome_talk"] = {
            "kind": "script",
            "text": _clip(talk.get("text"), LIMITS["five_steps"]["chars"]) or _text(base_talk.get("text")),
            "must_say": _text(talk.get("must_say")) or _text(base_talk.get("must_say")),
        }
    elif part_no == 3:
        result["pillars"] = _normalize_pillars(source.get("pillars"), fallback.get("pillars", []), issues)
        result["plan_talk"] = _normalize_plan(source.get("plan_talk"), contexts, fallback.get("plan_talk", []))
        result["clinical_followup_talk"] = _normalize_followup(
            source.get("clinical_followup_talk"), contexts, fallback.get("clinical_followup_talk", {}),
        )
        result["product_bridge"] = _normalize_bridge(
            source.get("product_bridge"), fallback.get("product_bridge", {}),
        )
    elif part_no == 4:
        result["note"] = _text(source.get("note")) or _text(fallback.get("note"))
        result["qa_table"] = _normalize_faq(
            source.get("qa_table"), fallback.get("qa_table", []), issues, path, rule_key="qa_table",
        )
        result["evidence_levels"] = _normalize_pairs(
            source.get("evidence_levels"), fallback.get("evidence_levels", []),
            key="level", text_key="plain", chars=LIMITS["evidence_levels"]["chars"],
            minimum=LIMITS["evidence_levels"]["min"], maximum=LIMITS["evidence_levels"]["max"],
        )
        result["glossary"] = _normalize_pairs(
            source.get("glossary"), fallback.get("glossary", []),
            key="term", text_key="plain", chars=LIMITS["glossary"]["chars"],
        )
        limits_talk = source.get("limits_talk") if isinstance(source.get("limits_talk"), dict) else {}
        result["limits_talk"] = _script_block(
            _clip(limits_talk.get("text"), LIMITS["limits"]["chars"])
            or _text((fallback.get("limits_talk") or {}).get("text"))
        )

    result["visual_guide"] = _normalize_visual_guide(
        source.get("visual_guide"), fallback["visual_guide"], issues, path,
    )
    result["faq"] = _normalize_faq(source.get("faq"), fallback["faq"], issues, path)
    result["cautions"] = _normalize_cautions(source.get("cautions"), fallback["cautions"])

    numbers = allowed if allowed is not None else allowed_numbers(contexts)
    # 数值核对后，正文被清空的问答/要点条目整条删掉，避免问句配错答案
    return prune_empty(scrub_numbers(result, fallback, numbers, issues, path))


def normalize_opening(
    opening: Any,
    contexts: dict[str, Any],
    *,
    issues: list[str] | None = None,
    allowed: frozenset[str] | None = None,
) -> dict[str, Any]:
    """归一化开场白与收尾脚本；三件事强制与第一部分一致。"""
    issues = issues if issues is not None else []
    source = opening if isinstance(opening, dict) else {}
    fallback = default_opening(contexts)
    chars = LIMITS["opening"]["chars"]

    model_open = source.get("opening_script") if isinstance(source.get("opening_script"), dict) else {}
    base_open = fallback["opening_script"]
    model_wrap = source.get("wrap_up_script") if isinstance(source.get("wrap_up_script"), dict) else {}
    base_wrap = fallback["wrap_up_script"]

    result = {
        "opening_script": {
            "kind": "script",
            **{
                key: _clip(model_open.get(key), chars) or _text(base_open.get(key))
                for key in ("self_intro", "role_statement", "agenda")
            },
        },
        "wrap_up_script": {
            "kind": "script",
            "summary": _clip(model_wrap.get("summary"), LIMITS["wrap"]["chars"]) or _text(base_wrap.get("summary")),
            # 三件事以第一部分为准，不允许模型增删或改写问题名
            "three_things": list(contexts["part1"]["top_three"]),
            **{
                key: _clip(model_wrap.get(key), LIMITS["cautions"]["chars"]) or _text(base_wrap.get(key))
                for key in ("confirm", "follow_up", "contact")
            },
        },
    }
    if model_wrap.get("three_things") and list(model_wrap["three_things"]) != result["wrap_up_script"]["three_things"]:
        issues.append("closing.wrap_up_script.three_things 与第一部分不一致，已按上下文覆盖")

    numbers = allowed if allowed is not None else allowed_numbers(contexts)
    return prune_empty(scrub_numbers(result, fallback, numbers, issues, "closing"))


# ---------------------------------------------------------------- 供其它报告复用
# 肠道与炎症报告（explain_gutBarrier）复用这一套通用归一化：结构一致，
# 只有 anchor 白名单与 target 枚举不同，按参数传入即可。
clip_text = _clip
text_of = _text
str_list = _str_list
dict_list = _dict_list
top_up = _top_up
script_block = _script_block
normalize_key_points = _normalize_key_points
normalize_script = _normalize_script
normalize_visual_guide = _normalize_visual_guide
normalize_faq = _normalize_faq
normalize_cautions = _normalize_cautions


__all__ = [
    "ANCHOR_WHITELIST",
    "clip_text",
    "dict_list",
    "normalize_cautions",
    "normalize_faq",
    "normalize_key_points",
    "normalize_script",
    "normalize_visual_guide",
    "script_block",
    "str_list",
    "text_of",
    "top_up",
    "LIMITS",
    "VISUAL_TARGETS",
    "allowed_numbers",
    "normalize_opening",
    "normalize_section",
    "scrub_numbers",
]
