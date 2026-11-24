# -*- coding: utf-8 -*-
"""肠道屏障报告解读（explain_gutBarrier）模型输出的归一化。

复用 ``explain_normalize`` 的通用能力（条数字数截断、anchor 校验、条数补齐、
数值防篡改），只在这里处理本报告特有的结构对齐：

- 指标条目的名称、结果、参考范围、结果评价一律以上下文为准，模型只贡献
  ``plain`` 与 ``talk`` 两段话术；
- 第二章的系统标题与疾病名照抄，模型只贡献 ``talk``；
- 两句固定话术（未评估口径、问卷自述）由程序写入，
  模型改写了也会被覆盖；
- 照读话术里的文献标记 ``[4]``、``[补19]`` 一律剥掉。

禁用语替换与必含声明兜底仍在 ``explain_compliance``。
"""

from __future__ import annotations

import re
from typing import Any

from modules.report.explain_compliance import prune_empty
from modules.report.explain_normalize import LIMITS
from modules.report.explain_normalize import allowed_numbers
from modules.report.explain_normalize import clip_text
from modules.report.explain_normalize import dict_list
from modules.report.explain_normalize import normalize_cautions
from modules.report.explain_normalize import normalize_faq
from modules.report.explain_normalize import normalize_key_points
from modules.report.explain_normalize import normalize_script
from modules.report.explain_normalize import normalize_visual_guide
from modules.report.explain_normalize import scrub_numbers
from modules.report.explain_normalize import text_of
from modules.report.gut_barrier_defaults import ANCHOR_PART1
from modules.report.gut_barrier_defaults import ANCHOR_PART2
from modules.report.gut_barrier_defaults import QUESTIONNAIRE_STATEMENT
from modules.report.gut_barrier_context import without_crp_content
from modules.report.gut_barrier_defaults import SECTION_TITLES
from modules.report.gut_barrier_defaults import UNASSESSED_STATEMENT
from modules.report.gut_barrier_defaults import default_opening
from modules.report.gut_barrier_defaults import default_section
from modules.report.gut_barrier_defaults import strip_citations

# 本报告的章节号：第一章 1.1–1.5，第二章 2.1–2.7
ANCHOR_WHITELIST: frozenset[str] = frozenset(ANCHOR_PART1 + ANCHOR_PART2)
# 可视元素：表格、指标卡、分级条、纯文字（没有雷达图）
VISUAL_TARGETS: frozenset[str] = frozenset({"table", "cards", "bands", "text"})
DEFAULT_TARGET = "text"

# 本报告特有的字数上限
CHARS = {
    "indicator": 120,   # 单项指标的 plain / talk
    "block": 200,       # ibfc / damage / inflammation / questionnaire / risk_overview / background
    "system": 120,      # 每个系统一句
    "summary": 150,
}


def _script(text: str) -> dict[str, str]:
    return {"kind": "script", "text": text}


def _clean(value: Any, limit: int) -> str:
    """截断 + 剥掉文献标记：照读话术里不出现 [4]、[补19] 这类编号。"""
    return clip_text(strip_citations(value), limit)


# ---------------------------------------------------------------- 第一章

def _normalize_indicator_talk(value: Any, contexts: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    """指标条目与上下文一一对齐；数值类字段照抄，模型只贡献两段话术。"""
    source = value if isinstance(value, dict) else {}
    fallback_items = {text_of(item.get("name")): item for item in dict_list(fallback.get("items"))}
    model_items = {text_of(item.get("name")): item for item in dict_list(source.get("items"))}

    items = []
    for row in contexts["part1"]["indicator_table"].get("rows", []):
        values = list(row.values())
        name = row.get("检测指标", values[0] if values else "")
        model = model_items.get(name, {})
        base = fallback_items.get(name, {})
        items.append({
            "name": name,
            "result": row.get("检测结果", ""),
            "reference": row.get("参考区间", row.get("参考范围", "")),
            "evaluation": row.get("结果评价", ""),
            "plain": _clean(model.get("plain"), CHARS["indicator"]) or text_of(base.get("plain")),
            "talk": _clean(model.get("talk"), CHARS["indicator"]) or text_of(base.get("talk")),
        })
    return {
        "kind": "script",
        "intro": _clean(source.get("intro"), CHARS["block"]) or text_of(fallback.get("intro")),
        "items": items,
    }


def _normalize_part1(
    source: dict[str, Any],
    contexts: dict[str, Any],
    fallback: dict[str, Any],
    issues: list[str],
) -> dict[str, Any]:
    part = contexts["part1"]
    result: dict[str, Any] = {}

    result["indicator_talk"] = _normalize_indicator_talk(
        source.get("indicator_talk"), contexts, fallback.get("indicator_talk", {}),
    )

    ibfc = source.get("ibfc_talk") if isinstance(source.get("ibfc_talk"), dict) else {}
    base_ibfc = fallback.get("ibfc_talk", {})
    result["ibfc_talk"] = {
        "kind": "script",
        "text": _clean(ibfc.get("text"), CHARS["block"]) or text_of(base_ibfc.get("text")),
        "band_guide": _clean(ibfc.get("band_guide"), CHARS["block"]) or text_of(base_ibfc.get("band_guide")),
    }

    damage = source.get("damage_talk") if isinstance(source.get("damage_talk"), dict) else {}
    result["damage_talk"] = _script(
        _clean(damage.get("text"), CHARS["block"])
        or text_of((fallback.get("damage_talk") or {}).get("text"))
    )

    # Keep the published object shape, with no retired assessment content.
    result["inflammation_talk"] = {"kind": "script", "text": "", "must_say": ""}

    questionnaire = source.get("questionnaire_talk") if isinstance(source.get("questionnaire_talk"), dict) else {}
    base_questionnaire = fallback.get("questionnaire_talk", {})
    if text_of(questionnaire.get("must_say")) not in ("", QUESTIONNAIRE_STATEMENT):
        issues.append("sections[1].questionnaire_talk.must_say 被改写，已覆盖为固定话术")
    result["questionnaire_talk"] = {
        "kind": "script",
        "text": _clean(questionnaire.get("text"), CHARS["block"]) or text_of(base_questionnaire.get("text")),
        "must_say": QUESTIONNAIRE_STATEMENT,
    }
    return result


# ---------------------------------------------------------------- 第二章

# 本次没有做出分层时，话术里不许出现这些字眼——模型最容易在这里自己补一个等级
_RISK_CLAIM_WORDS: tuple[str, ...] = (
    "风险指数", "高风险", "中风险", "低风险", "风险等级", "风险较高", "风险偏高",
)


def _strip_risk_claims(text: str, assessed: int, issues: list[str], path: str) -> str:
    """整章未分层时，丢掉自行给出风险等级的句子。"""
    if assessed or not text:
        return text
    kept, dropped = [], []
    for sentence in re.split(r"(?<=[。！？；])", text):
        if not sentence.strip():
            continue
        if any(word in sentence for word in _RISK_CLAIM_WORDS):
            dropped.append(sentence.strip())
            continue
        kept.append(sentence)
    if dropped:
        issues.append(f"{path} 在未做分层的情况下给出了风险等级，已删除相关语句")
    return "".join(kept)


def _normalize_systems(value: Any, contexts: dict[str, Any], fallback: list[Any], issues: list[str]) -> list[dict[str, Any]]:
    """系统标题与疾病名照抄上下文，模型只贡献 talk。"""
    model_by_title = {text_of(item.get("title")): item for item in dict_list(value)}
    fallback_by_title = {text_of(item.get("title")): item for item in fallback if isinstance(item, dict)}
    assessed = contexts["part2"]["stats"]["assessed"]
    systems = []
    for index, system in enumerate(contexts["part2"]["systems"], start=1):
        title = system["title"]
        model = model_by_title.get(title, {})
        base = fallback_by_title.get(title, {})
        talk = _clean(model.get("talk"), CHARS["system"])
        talk = _strip_risk_claims(talk, assessed, issues, f"sections[2].systems[{index}].talk")
        systems.append({
            "title": title,
            "diseases": [row["disease"] for row in system["rows"] if row.get("disease")],
            "talk": talk or text_of(base.get("talk")),
        })
    extra = [title for title in model_by_title if title not in {s["title"] for s in systems}]
    if extra:
        issues.append(f"sections[2].systems 多出上下文没有的系统 {extra}，已丢弃")
    return systems


def _normalize_part2(
    source: dict[str, Any],
    contexts: dict[str, Any],
    fallback: dict[str, Any],
    issues: list[str],
) -> dict[str, Any]:
    part = contexts["part2"]
    result: dict[str, Any] = {}

    overview = source.get("risk_overview") if isinstance(source.get("risk_overview"), dict) else {}
    base_overview = fallback.get("risk_overview", {})
    if text_of(overview.get("must_say")) not in ("", UNASSESSED_STATEMENT):
        issues.append("sections[2].risk_overview.must_say「未评估」口径被改写，已覆盖为固定话术")
    result["risk_overview"] = {
        "kind": "script",
        "text": _clean(overview.get("text"), CHARS["block"]) or text_of(base_overview.get("text")),
        "must_say": UNASSESSED_STATEMENT,
        # 统计数字由程序给，不让模型自己数
        "stats": {key: value for key, value in part["stats"].items() if key != "assessed_rows"},
    }

    result["systems"] = _normalize_systems(
        source.get("systems"), contexts, fallback.get("systems", []), issues,
    )

    background = source.get("background_talk") if isinstance(source.get("background_talk"), dict) else {}
    base_background = fallback.get("background_talk", {})
    context_background = part.get("clinical_background") or {}
    rows = context_background.get("rows", []) if isinstance(context_background, dict) else []
    result["background_talk"] = {
        "kind": "script",
        "rows": rows,  # 检查日期、项目、数值照抄，模型碰不到
        "talk": (_clean(background.get("talk"), CHARS["block"]) or text_of(base_background.get("talk")))
        if rows else "",
    }

    summary = source.get("summary_talk") if isinstance(source.get("summary_talk"), dict) else {}
    base_summary = fallback.get("summary_talk", {})
    result["summary_talk"] = {
        "kind": "script",
        "points": list(part["summary_points"]),  # 照抄原文
        "talk": _clean(summary.get("talk"), CHARS["summary"]) or text_of(base_summary.get("talk")),
    }
    return result


# ---------------------------------------------------------------- 对外入口

def normalize_section(
    part_no: int,
    section: Any,
    contexts: dict[str, Any],
    *,
    issues: list[str] | None = None,
    allowed: frozenset[str] | None = None,
) -> dict[str, Any]:
    """归一化一章的五件套 + 本章特有块。"""
    issues = issues if issues is not None else []
    source = without_crp_content(section) if isinstance(section, dict) else {}
    fallback = default_section(part_no, contexts)
    path = f"sections[{part_no}]"
    anchors = frozenset(ANCHOR_PART1 if part_no == 1 else ANCHOR_PART2)

    result: dict[str, Any] = {
        "part_no": part_no,
        "title": SECTION_TITLES[part_no],
        "key_points": normalize_key_points(
            source.get("key_points"), fallback["key_points"], issues, path, anchors=anchors,
        ),
        "script": normalize_script(source.get("script"), fallback["script"], issues, path),
    }

    if part_no == 1:
        result.update(_normalize_part1(source, contexts, fallback, issues))
    else:
        result.update(_normalize_part2(source, contexts, fallback, issues))

    result["visual_guide"] = normalize_visual_guide(
        source.get("visual_guide"), fallback["visual_guide"], issues, path,
        anchors=anchors, targets=VISUAL_TARGETS, default_target=DEFAULT_TARGET,
    )
    result["faq"] = normalize_faq(source.get("faq"), fallback["faq"], issues, path)
    result["cautions"] = normalize_cautions(source.get("cautions"), fallback["cautions"])

    numbers = allowed if allowed is not None else allowed_numbers(contexts)
    result = without_crp_content(prune_empty(scrub_numbers(result, fallback, numbers, issues, path)))
    if part_no == 1:
        result["inflammation_talk"] = {"kind": "script", "text": "", "must_say": ""}
    return result


def normalize_opening(
    opening: Any,
    contexts: dict[str, Any],
    *,
    issues: list[str] | None = None,
    allowed: frozenset[str] | None = None,
) -> dict[str, Any]:
    """归一化开场白与收尾脚本；三件事由程序生成，模型改了也会被覆盖。"""
    issues = issues if issues is not None else []
    source = without_crp_content(opening) if isinstance(opening, dict) else {}
    fallback = default_opening(contexts)
    chars = LIMITS["opening"]["chars"]

    model_open = source.get("opening_script") if isinstance(source.get("opening_script"), dict) else {}
    base_open = fallback["opening_script"]
    model_wrap = source.get("wrap_up_script") if isinstance(source.get("wrap_up_script"), dict) else {}
    base_wrap = fallback["wrap_up_script"]
    three_things = list(base_wrap["three_things"])

    if model_wrap.get("three_things") and list(model_wrap["three_things"]) != three_things:
        issues.append("closing.wrap_up_script.three_things 与程序生成的不一致，已按上下文覆盖")

    result = {
        "opening_script": {
            "kind": "script",
            **{
                key: _clean(model_open.get(key), chars) or text_of(base_open.get(key))
                for key in ("self_intro", "role_statement", "report_intro", "agenda")
            },
        },
        "wrap_up_script": {
            "kind": "script",
            "summary": _clean(model_wrap.get("summary"), LIMITS["wrap"]["chars"]) or text_of(base_wrap.get("summary")),
            "three_things": three_things,
            **{
                key: _clean(model_wrap.get(key), LIMITS["cautions"]["chars"]) or text_of(base_wrap.get(key))
                for key in ("confirm", "follow_up", "contact")
            },
        },
    }
    numbers = allowed if allowed is not None else allowed_numbers(contexts)
    return prune_empty(scrub_numbers(result, fallback, numbers, issues, "closing"))


__all__ = [
    "ANCHOR_WHITELIST",
    "VISUAL_TARGETS",
    "normalize_opening",
    "normalize_section",
]
