# -*- coding: utf-8 -*-
"""肠道屏障报告解读（explain_gutBarrier）的固定文案与兜底内容。

分工与 ``explain_defaults.py`` 一致：固定文案写死在这里，兜底内容由
``gut_barrier_context`` 裁好的上下文确定性拼装，模型只负责把报告原文改写
成可照读的口语话术。

只解读第一章与第二章：第三章分层修复方案、第四章屏障与炎症机制是固定文案，
第五章附录只用参考文献。
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from modules.report.explain_compliance import FORBIDDEN_WORDS
from modules.report.explain_compliance import apply_compliance
from modules.report.explain_compliance import run_self_check
from modules.report.explain_defaults import CONFIDENTIAL_NOTE
from modules.report.explain_defaults import MANUAL_SCENE
from modules.report.explain_defaults import READER_NOTICE
from modules.report.gut_barrier_context import build_gut_barrier_contexts, without_crp_content

# 本报告的必含声明：没有菌群内容，换成「未评估」口径与自述限定
REQUIRED_STATEMENTS: tuple[tuple[str, str], ...] = (
    ("角色边界声明", "对接医生"),
    ("未评估口径", "未评估"),
    ("自述限定", "您自己填的"),
)
EXPECTED_SECTIONS = 2

# ---------------------------------------------------------------- 固定文案

SECTION_TITLES: dict[int, str] = {
    1: "第一章 肠道屏障检测结果综述",
    2: "第二章 相关疾病风险评估",
}

# 这两句是本报告最容易讲错、也最容易违规的地方，写死在程序里，模型不得改写
UNASSESSED_STATEMENT = (
    "这一章里写「未评估」的，是这次还没有做出风险分层，不是没有风险，也不是风险低。"
    "要做分层需要补充定量结果。"
)
QUESTIONNAIRE_STATEMENT = "问卷这部分是您自己填的情况，不是检测结果，我们只用它来理解背景。"
REPORT_INTRO = (
    "这份报告看的是肠道屏障功能和相关健康风险，"
    "它是一项专项评估，不是体检，也不是疾病诊断。"
)
MECHANISM_NOTE = "这一列写的是一般机制，不是您本人已经发生的情况。"

# 报告没带「指标意义」时的兜底解释：只说这项测什么，不延伸到疾病结论
INDICATOR_PLAIN: tuple[tuple[str, str], ...] = (
    ("D-乳酸", "这项和肠道里细菌的代谢有关，用来间接观察肠壁的通透性变化。"),
    ("DAO", "这项酶主要在小肠绒毛上皮里，用来辅助观察肠道上皮是不是完整。"),
    ("二胺氧化酶", "这项酶主要在小肠绒毛上皮里，用来辅助观察肠道上皮是不是完整。"),
    ("脂多糖", "这是革兰阴性菌外膜的成分，也叫内毒素，用来观察细菌产物进入血液的负荷。"),
    ("LPS", "这是革兰阴性菌外膜的成分，也叫内毒素，用来观察细菌产物进入血液的负荷。"),
)

ANCHOR_PART1 = ("1.1", "1.2", "1.3", "1.4", "1.5")
ANCHOR_PART2 = tuple(f"2.{i}" for i in range(1, 8))

# 报告原文的机制描述里带 [4]、[补19] 这类文献标记，照读话术里要去掉
_CITATION_RE = re.compile(r"[\[［]\s*补?\s*\d+(?:\s*[,，]\s*补?\s*\d+)*\s*[\]］]")


def strip_citations(text: Any) -> str:
    """去掉文献标记；解读人不会把编号念出来。"""
    cleaned = _CITATION_RE.sub("", str(text or ""))
    return re.sub(r"\s{2,}", " ", cleaned).strip()


# ---------------------------------------------------------------- 小工具

def _script(text: str) -> dict[str, str]:
    return {"kind": "script", "text": text}


def _plain_for(name: str) -> str:
    for keyword, plain in INDICATOR_PLAIN:
        if keyword.lower() in name.lower():
            return plain
    return "这项是本次检测的项目之一，具体含义以报告原文为准。"


def _indicator_talk_line(row: dict[str, str]) -> str:
    values = list(row.values())
    name = row.get("检测指标", values[0] if values else "")
    result = row.get("检测结果", "")
    reference = row.get("参考范围") or row.get("参考区间", "")
    evaluation = row.get("结果评价", "")
    parts = [f"{name}这一项是 {result}".rstrip()]
    if reference:
        parts.append(f"参考范围是 {reference}")
    if evaluation:
        parts.append(f"报告给的评价是「{evaluation}」")
    return "，".join(parts) + "。"


def _sentences(text: str, limit: int = 2) -> str:
    parts = [part.strip() for part in str(text or "").split("\n") if part.strip()]
    return "".join(parts[:limit])


# ---------------------------------------------------------------- 兜底：第一章

def _default_part1(ctx: dict[str, Any]) -> dict[str, Any]:
    part = ctx["part1"]
    ibfc = part["ibfc"]
    indicator_rows = part["indicator_table"].get("rows", [])
    canonical_layout = part["indicator_table"].get("columns") == [
        "检测项目", "检测指标", "检测结果", "参考区间", "结果评价", "反映维度",
    ]
    abnormal = part["abnormal_indicators"]
    pending = part["pending_indicators"]

    key_points = []
    if abnormal:
        key_points.append({
            "text": "本次有 " + "、".join(item["name"] for item in abnormal[:2]) + " 等项超出参考范围。",
            "anchor": "1.2",
        })
    else:
        confirmed_normal = sum(
            row.get("结果评价", "").strip() in {
                "正常", "未见异常", "理想", "阴性", "未见评分异常", "未超出参考范围", "参考范围内",
            }
            for row in indicator_rows
        )
        if confirmed_normal:
            key_points.append({"text": f"报告明确标注 {confirmed_normal} 项未超出参考范围。", "anchor": "1.2"})
    if ibfc["score"]:
        grade_text = f"，{ibfc['grade']}" if ibfc["grade"] else ""
        key_points.append({"text": f"肠道屏障功能评分 {ibfc['score']} 分{grade_text}。",
                           "anchor": "1.1" if canonical_layout else "1.3"})
    if pending:
        key_points.append({
            "text": "有 " + "、".join(item["name"] for item in pending[:2]) + " 需要补定量结果才能分层。",
            "anchor": "1.2" if canonical_layout else "1.5",
        })
    if not key_points:
        key_points.append({"text": "本次以检测结果综述为主，按报告原文逐项讲解。", "anchor": "1.1"})

    body = [
        f"{row.get('评价项目', '')}：{_sentences(row.get('综合结论', ''))}"
        for row in part["summary_table"].get("rows", [])[:3]
        if row.get("综合结论")
    ]

    meanings = part.get("indicator_meanings") or {}
    meaning_text = _sentences(meanings.get("text", ""), limit=4) if isinstance(meanings, dict) else ""

    items = []
    for row in indicator_rows:
        values = list(row.values())
        name = row.get("检测指标", values[0] if values else "")
        items.append({
            "name": name,
            "result": row.get("检测结果", ""),
            "reference": row.get("参考范围") or row.get("参考区间", ""),
            "evaluation": row.get("结果评价", ""),
            "plain": _plain_for(name),
            "talk": _indicator_talk_line(row),
        })

    band_guide = (
        f"这次落在「{ibfc['selected_band']}」这一档。" if ibfc["selected_band"]
        else "这次没有落进任何一档，我按报告给的等级说明跟您讲。"
    )
    highlights = part["questionnaire_highlights"]
    questionnaire_text = (
        "您在问卷里提到" + "、".join(
            f"{item['label']}{item['answer'].replace('（自述）', '')}" for item in highlights[:3]
        ) + "，这些我们和检测结果放在一起看。"
    ) if highlights else "本次资料未包含可供解读的问卷内容。"

    ibfc_details = []
    if ibfc["score"]:
        ibfc_details.append(f"肠道屏障功能评分 {ibfc['score']} 分")
    if ibfc["grade"]:
        ibfc_details.append(ibfc["grade"])
    if ibfc["damage_type"]:
        ibfc_details.append(f"受损类型是{ibfc['damage_type']}")
    ibfc_text = "，".join(ibfc_details) + "。" if ibfc_details else "本次资料未包含可核对的肠道屏障评分与分级结果。"

    return {
        "part_no": 1,
        "title": SECTION_TITLES[1],
        "key_points": key_points,
        "script": {
            "kind": "script",
            "opening": "我们先看这次的检测结果，我按报告的顺序一项一项给您讲。",
            "body": body or ["我按报告原文给您讲这次的检测结果。"],
            "closing": "第一章就是这些，接下来看这些结果和哪些系统有关。",
        },
        "indicator_talk": {
            "kind": "script",
            "intro": meaning_text or "这三项分别看肠道通透性、上皮状态和内毒素负荷，相互补充。",
            "items": items,
        },
        "ibfc_talk": {
            "kind": "script",
            "text": part["ibfc"]["paragraph"] or ibfc_text,
            "band_guide": band_guide,
        },
        "damage_talk": _script(part["damage_paragraph"] or "受损状况以报告原文为准。"),
        "inflammation_talk": {
            "kind": "script",
            "text": "",
            "must_say": "",
        },
        "questionnaire_talk": {
            "kind": "script",
            "text": questionnaire_text,
            "must_say": QUESTIONNAIRE_STATEMENT,
        },
        "visual_guide": [
            {
                "anchor": "1.2",
                "target": "table",
                "order": ["从上往下逐项读", "每项连参考范围一起报"],
                "talk": "我们对着这张表看，我把每一项的数值和参考范围念给您。",
            },
            {
                "anchor": "1.1" if canonical_layout else "1.3",
                "target": "bands",
                "order": ["先指分数", "再指分级条上的位置"],
                "talk": "这条是分级尺，从左到右越来越重，我指给您看这次在哪儿。",
            },
        ],
        "faq": [
            {
                "question": "这几项指标是查什么的？",
                "answer": "分别看肠道通透性、上皮状态和内毒素负荷，三项结合起来解读。",
                "source": "报告 1.2",
            },
            {
                "question": "没超范围是不是就没事了？",
                "answer": "即使检测结果未超出参考范围，也不等于完全没问题；是否正常需先核对本次结果，有症状时仍要结合症状评估。",
                "source": "报告 1.2",
            },
            {
                "question": "这个分数是怎么算的？",
                "answer": "评分由报告根据肠道屏障检测结果计算，具体算法以报告说明为准，这里不自行推算。",
                "source": "报告 1.1" if canonical_layout else "报告 1.3",
            },
        ],
        "cautions": {
            "must_say": ["报数必须连参考范围一起报", QUESTIONNAIRE_STATEMENT],
            "easy_wrong": ["不要把「未超范围」说成「完全没问题」", "不要自己换算或汇总评分"],
            "forbidden": ["不得把某一项指标说成某种疾病的证据", "不得自行升档降档"],
        },
    }


# ---------------------------------------------------------------- 兜底：第二章

def _default_part2(ctx: dict[str, Any]) -> dict[str, Any]:
    part = ctx["part2"]
    stats = part["stats"]
    systems = []
    for index, system in enumerate(part["systems"], start=1):
        diseases = [row["disease"] for row in system["rows"] if row["disease"]]
        first_mechanism = next(
            (strip_citations(row["mechanism"]) for row in system["rows"] if row.get("mechanism")), "",
        )
        talk = (
            f"这一块列的是{ '、'.join(diseases[:3]) }等。" +
            (f"{_sentences(first_mechanism, limit=1)}{MECHANISM_NOTE}" if first_mechanism
             else "报告在这里列的是机制上的关联方向。")
        )
        systems.append({"title": system["title"], "diseases": diseases, "talk": talk})

    background = part.get("clinical_background") or {}
    background_rows = background.get("rows", []) if isinstance(background, dict) else []
    background_talk = ""
    if background_rows:
        first = background_rows[0]
        values = list(first.values())
        background_talk = (
            "报告这里还列了您既有的检查记录，比如" + "、".join(str(v) for v in values[:3] if v) +
            "。这些是已有的检查结果，我们和这次的屏障检测放在一起看。"
        )

    if stats["assessed"]:
        overview_text = (
            f"这一章一共列了 {stats['total']} 条，其中 {stats['assessed']} 条做出了风险分层，"
            f"其余 {stats['unassessed']} 条写的是未评估。我们先看做出分层的那几条。"
        )
    elif stats["total"]:
        overview_text = (
            f"这一章一共列了 {stats['total']} 条，本次都还没有做出风险分层。"
            "所以这一章我们主要看它讲的机制方向，以及下一步要补什么。"
        )
    else:
        overview_text = "本次资料未包含可供解读的疾病风险条目，不能据此判断风险低或没有风险。"

    return {
        "part_no": 2,
        "title": SECTION_TITLES[2],
        "key_points": [
            {"text": f"本章列了 {stats['total']} 条相关疾病，{stats['unassessed']} 条未做分层。", "anchor": "2.1"},
            {"text": "「未评估」是这次没做分层，不是没有风险。", "anchor": "2.1"},
            {"text": "这一章讲的是机制关联方向，不是诊断。", "anchor": "2.1"},
        ],
        "script": {
            "kind": "script",
            "opening": "接下来这一章，是把肠道屏障和炎症跟各个系统的关系梳理一遍。",
            "body": [overview_text],
            "closing": "这一章先了解到这里，具体要不要进一步检查，我们按刚才说的安排。",
        },
        "risk_overview": {
            "kind": "script",
            "text": overview_text,
            "must_say": UNASSESSED_STATEMENT,
            "stats": {key: value for key, value in stats.items() if key != "assessed_rows"},
        },
        "systems": systems,
        "background_talk": {
            "kind": "script",
            "rows": deepcopy((part.get("clinical_background") or {}).get("rows") or []),
            "talk": background_talk,
        },
        "summary_talk": {
            "kind": "script",
            "points": list(part["summary_points"]),
            "talk": _sentences("\n".join(part["summary_points"]), limit=2)
                    or "本章小结以报告原文为准。",
        },
        "visual_guide": [
            {
                "anchor": "2.1",
                "target": "table",
                "order": ["先指疾病这一列", "再指风险等级这一列", "最后说明机制关联是一般机制"],
                "talk": "您看这张表，左边是相关疾病，中间是风险等级，右边讲的是机制上的关系。",
            },
        ],
        "faq": [
            {
                "question": "写着未评估是什么意思？",
                "answer": UNASSESSED_STATEMENT,
                "source": "报告第二章",
            },
            {
                "question": "是不是说我会得这些病？",
                "answer": "不是。这一章讲的是机制上的关联方向，不是说您已经有这些病或一定会得。",
                "source": "报告第二章",
            },
            {
                "question": "那我现在要做什么？",
                "answer": "先把需要补的检测补上，有症状的按症状找对应的专科，其余按日常管理来。",
                "source": "报告第二章",
            },
        ],
        "cautions": {
            "must_say": [UNASSESSED_STATEMENT, MECHANISM_NOTE],
            "easy_wrong": ["不要把机制关联讲成个人诊断", "不要自己给风险等级"],
            "forbidden": ["不得估计患病概率", "不得对报告未收录的疾病展开讲解"],
        },
    }


_PART_BUILDERS = {1: _default_part1, 2: _default_part2}


def default_section(part_no: int, contexts: dict[str, Any]) -> dict[str, Any]:
    builder = _PART_BUILDERS.get(int(part_no))
    if builder is None:
        raise ValueError(f"unsupported gut barrier part: {part_no!r}")
    return builder(contexts)


# ---------------------------------------------------------------- 三件事 / 开场白

def build_top_three(contexts: dict[str, Any]) -> list[str]:
    """本报告没有临床问题清单，按「待明确项 → 个体背景异常 → 资料缺口」取前三条。"""
    part1, part2 = contexts["part1"], contexts["part2"]
    items: list[str] = []
    for pending in part1["pending_indicators"]:
        items.append(f"补一次 {pending['name']} 的定量检测")
    for abnormal in part1["abnormal_indicators"]:
        items.append(f"关注 {abnormal['name']}（本次{abnormal['evaluation']}），按医生建议复查")
    background = part2.get("clinical_background") or {}
    for row in (background.get("rows", []) if isinstance(background, dict) else [])[:2]:
        values = [value for value in row.values() if value]
        if len(values) >= 2:
            items.append(f"把 {values[1]} 的既往结果带给医生一起看")
    for gap in part1["data_gaps"]:
        if "未提供" in gap:
            items.append(gap.replace("本次未提供", "尽量补齐"))
    if not items:
        items.append("先核对本次报告中的检测结果和管理建议，再确认后续安排")
    # 去重后取前三条
    seen, unique = set(), []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique[:3]


def default_opening(contexts: dict[str, Any]) -> dict[str, Any]:
    meta = contexts["meta"]
    name = meta.get("name") or "您"
    return {
        "opening_script": {
            "kind": "script",
            "self_intro": f"{name}您好，我是您的健康顾问，今天由我陪您把这份报告过一遍。",
            "role_statement": "我负责帮您看懂这份报告，涉及诊断和用药的问题，我会帮您对接医生。",
            "report_intro": REPORT_INTRO,
            "agenda": "报告分两章，我们大概二十分钟过一遍，您随时可以打断我提问。",
        },
        "wrap_up_script": {
            "kind": "script",
            "summary": "我把接下来要做的几件事说一下，您听完帮我复述一遍。",
            "three_things": build_top_three(contexts),
            "confirm": "需要补的检测，您看什么时候方便去？我记一下时间。",
            "follow_up": "补完检测我们再约一次，把新的结果放进来一起看。",
            "contact": "这期间有任何疑问，直接微信或电话找我都可以。",
        },
    }


# ---------------------------------------------------------------- 组装

def build_manual_meta(contexts: dict[str, Any]) -> dict[str, Any]:
    meta = contexts["meta"]
    name = meta.get("name") or ""
    return {
        "title": f"{name}肠道屏障报告解读参考手册" if name else "肠道屏障报告解读参考手册",
        "scene": MANUAL_SCENE,
        "report_title": meta.get("report_title", ""),
        "report_id": meta.get("report_id", ""),
        "report_date": meta.get("report_date", ""),
        "final_report_id": meta.get("final_report_id", ""),
        "source_status": meta.get("status", ""),
        # 资料缺口只给【内部】看，不进照读话术
        "data_gaps": list(contexts["part1"].get("data_gaps") or []),
        "confidential_note": CONFIDENTIAL_NOTE,
    }


def assemble_manual(
    *,
    contexts: dict[str, Any],
    sections: list[dict[str, Any]],
    opening: dict[str, Any],
    user_id: str,
    session_id: str,
    issues: list[str] | None = None,
    compliance: bool = True,
) -> dict[str, Any]:
    """把三个子任务的产出拼成完整手册报文。"""
    meta = contexts["meta"]
    payload: dict[str, Any] = {
        "type": "explain_gutBarrier",
        "userId": str(user_id),
        "sessionId": str(session_id),
        "subject": {
            "name": meta.get("name"),
            "gender": meta.get("gender"),
            "age": meta.get("age"),
            "report_date": meta.get("report_date"),
        },
        "manual_meta": build_manual_meta(contexts),
        "preface": {
            "quick_card": contexts["quick_card"],
            "reader_notice": list(READER_NOTICE),
            "opening_script": opening.get("opening_script", {}),
        },
        "sections": sections,
        "closing": {
            "wrap_up_script": opening.get("wrap_up_script", {}),
            "forbidden_words": [deepcopy(entry) for entry in FORBIDDEN_WORDS],
            "self_check": [],
        },
        # 字段结构与评估报告的 evidence 不同（citation/publisher/year），键名单独用 references
        "references": contexts["references"],
    }
    issues = list(issues or [])
    if compliance:
        payload, compliance_issues = apply_compliance(
            payload, fallback=_fallback_for(contexts, user_id, session_id),
        )
        issues.extend(compliance_issues)
    payload["closing"]["self_check"] = run_self_check(
        payload,
        issues=issues,
        expected_sections=EXPECTED_SECTIONS,
        required_statements=REQUIRED_STATEMENTS,
    )
    payload = without_crp_content(payload)
    return payload


def _fallback_for(contexts: dict[str, Any], user_id: str, session_id: str) -> dict[str, Any]:
    return assemble_manual(
        contexts=contexts,
        sections=[default_section(part_no, contexts) for part_no in (1, 2)],
        opening=default_opening(contexts),
        user_id=user_id,
        session_id=session_id,
        compliance=False,
    )


def default_gut_barrier_manual(
    *,
    evaluate: dict[str, Any],
    user_id: str,
    session_id: str,
) -> dict[str, Any]:
    """整份手册的兜底：模型全挂时仍然按这份报告的真实内容输出。"""
    contexts = build_gut_barrier_contexts(evaluate or {})
    return _fallback_for(contexts, user_id, session_id)


def empty_gut_barrier_manual() -> dict[str, Any]:
    """空骨架：字段类型与正常报文一致，前端不会因为拿到 {} 而渲染报错。"""
    return {
        "manual_meta": {},
        "preface": {"quick_card": {}, "reader_notice": [], "opening_script": {}},
        "sections": [],
        "closing": {"wrap_up_script": {}, "forbidden_words": [], "self_check": []},
        "references": [],
    }


__all__ = [
    "ANCHOR_PART1",
    "ANCHOR_PART2",
    "MECHANISM_NOTE",
    "QUESTIONNAIRE_STATEMENT",
    "SECTION_TITLES",
    "UNASSESSED_STATEMENT",
    "assemble_manual",
    "build_manual_meta",
    "build_top_three",
    "default_gut_barrier_manual",
    "default_opening",
    "default_section",
    "empty_gut_barrier_manual",
    "strip_citations",
]
