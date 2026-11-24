# -*- coding: utf-8 -*-
"""报告解读（explain）的固定文案与兜底内容。

分工与 ``followup_defaults.py`` 一致：
- 固定文案（解读人须知、禁用语表、合规声明）写死在这里，产品要改只改一处；
- 兜底内容不是静态套话，而是由 ``explain_context`` 裁好的上下文**确定性拼装**，
  因此模型全挂时手册里仍然是这位受检者的真实数据，而不是模板范文；
- 模型只负责把这些内容改写成可照读的口语话术。

术语：
- ``kind="script"`` 的文本会被解读人一字不改地念给用户听（【照读】）；
- ``kind="internal"`` 的文本只给解读人自己看（【内部】）。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from modules.report.explain_compliance import FORBIDDEN_WORDS
from modules.report.explain_compliance import MICROBIOME_DISCLAIMER
from modules.report.explain_compliance import apply_compliance
from modules.report.explain_compliance import run_self_check
from modules.report.explain_context import build_explain_contexts

# ---------------------------------------------------------------- 固定文案

SECTION_TITLES: dict[int, str] = {
    1: "第一部分 健康综合评估",
    2: "第二部分 主要问题分析",
    3: "第三部分 干预方案",
    4: "第四部分 循证支持",
}

MANUAL_SCENE = "30分钟一对一面谈"

CONFIDENTIAL_NOTE = (
    "本手册为内部培训与解读参考材料，不构成医疗诊断与治疗建议；"
    "解读人不具备医师资质，所有诊疗问题转交医生处理。"
    "手册含用户健康隐私信息，仅限团队内部使用，不得整册转发用户或外传。"
)

READER_NOTICE: tuple[str, ...] = (
    "解读人不是医生，只做报告信息传递与健康教育；涉及诊断、用药的问题一律转交医生或健康管理师。",
    "本手册仅供内部使用，不得整册转发用户；可以发给用户的只有报告本身。",
    "遇到手册未覆盖的问题，记录下来转交健康管理师或医生回复，不临场发挥。",
)

PRODUCT_HANDOFF = "具体配方我请健康管理师根据您的报告来配，配好了再给您详细讲。"

PRODUCT_FORBIDDEN: tuple[str, ...] = ("不报价", "不逼单", "不承诺疗效与时间")

PILLAR_TITLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("lifestyle", "支柱一：个性化生活方式干预", ("diet", "exercise", "sleep")),
    ("microbiome", "支柱二：个性化肠道菌群与肠屏障调节",
     ("microbiome_rationale", "probiotics", "prebiotics", "barrier", "microbiome_followup")),
    ("nutrition", "支柱三：个性化营养素补充", ("nutrition",)),
)


# ---------------------------------------------------------------- 小工具

def _script(text: str) -> dict[str, str]:
    return {"kind": "script", "text": text}


def _paragraphs(text: str, limit: int = 3) -> list[str]:
    """把评估报告里的多行正文切成段，供话术 body 使用。"""
    lines = [line.strip() for line in str(text or "").split("\n") if line.strip()]
    return lines[:limit] if lines else []


def _join(blocks: list[dict[str, Any]], codes: tuple[str, ...]) -> str:
    wanted = [block for block in blocks if block.get("code") in codes]
    return "\n".join(block.get("content", "") for block in wanted if block.get("content"))


def _certainty_note(problem: dict[str, Any]) -> str:
    """suspected / 待复核类问题必须带限定语，这里给解读人的【内部】提示。"""
    certainty = str(problem.get("certainty") or "")
    name = str(problem.get("problem") or "")
    uncertain = certainty == "suspected" or any(
        word in name for word in ("待复核", "可能", "疑似")
    )
    if uncertain:
        return "这项目前还没有确定性质，讲的时候必须说「疑似、待进一步明确」，不要说成已经确诊。"
    return "这项在报告里有明确记录，按报告原文讲即可，仍然不要下诊断结论。"


# ---------------------------------------------------------------- 兜底：四组五件套

def _default_part1(ctx: dict[str, Any]) -> dict[str, Any]:
    part = ctx["part1"]
    assessment = part["assessment"]
    top_three = part["top_three"]
    key_points = []
    if assessment["score"]:
        key_points.append({
            "text": f"综合评分 {assessment['score']} 分，报告归为「{assessment['level_label']}」。",
            "anchor": "1.1",
        })
    if assessment["lowest_dimension"]:
        key_points.append({
            "text": f"六个系统里 {assessment['lowest_dimension']} 分数最低，是本次最需要关注的方向。",
            "anchor": "1.1",
        })
    if top_three:
        key_points.append({
            "text": "需要优先关注的事项：" + "、".join(top_three) + "。",
            "anchor": "1.2",
        })
    if not key_points:
        key_points.append({"text": "本次资料未包含可核对的综合分层或优先事项，需结合原始报告进一步确认。", "anchor": "1.1"})

    body = _paragraphs(part["overview"]) or _paragraphs(assessment["summary"])
    return {
        "part_no": 1,
        "title": SECTION_TITLES[1],
        "key_points": key_points,
        "script": {
            "kind": "script",
            "opening": "我们先看整体，这样您心里有个全貌。",
            "body": body or ["我们先一起看这份报告的整体情况。"],
            "closing": "整体就是这样，接下来我重点讲最需要您重视的几个问题。",
        },
        "visual_guide": [
            {
                "anchor": "1.1",
                "target": "radar",
                "order": [
                    f"先指 {assessment['lowest_dimension'] or '分数最低'} 这根最短的轴",
                    f"再指 {assessment['highest_dimension'] or '分数较高'} 这根较长的轴",
                ],
                "talk": "您看这张图，哪根轴短就说明那个系统这次更需要关注，长的说明目前还比较稳。",
            },
            {
                "anchor": "1.4",
                "target": "table",
                "order": ["从上往下按级别读", "读到级别最高那条停下来确认用户听懂"],
                "talk": "这张表就像交通信号灯：级别最高的要尽快处理，中间的这个月安排，最后的定期观察就行。",
            },
        ],
        "faq": [
            {
                "question": f"{assessment['score'] or ''}分是不是很差？".lstrip(),
                "answer": "这是报告按六个系统算出来的提示分，不是考试分数，它的作用是告诉我们先关注哪一块。",
                "source": "报告 1.1 健康总览",
            },
            {
                "question": "这些关联疾病是说我有这些病吗？",
                "answer": f"不是的。{MICROBIOME_DISCLAIMER}报告列出来是提醒我们平时留意相关的变化。",
                "source": "报告 1.1 六维健康管理评估",
            },
            {
                "question": "这么多异常，要紧吗？",
                "answer": "报告已经按轻重缓急分好级了，我们按级别一件一件来，不用一次全处理。",
                "source": "报告 1.4 问题分级清单",
            },
        ],
        "cautions": {
            "must_say": ["六维评分是健康管理评估，不是疾病诊断", "先讲级别最高的事项"],
            "easy_wrong": ["不要把「中风险」说成「有这个病」", "不要自己换算分数或比较高低"],
            "forbidden": ["不得逐条念异常指标表", "不得在本部分展开机制细节"],
        },
    }


def _default_part2(ctx: dict[str, Any]) -> dict[str, Any]:
    part = ctx["part2"]
    problems = []
    for problem in part["problems"]:
        problems.append({
            "no": problem["no"],
            "problem": problem["problem"],
            "priority": problem["priority"],
            "certainty_note": _certainty_note(problem),
            "five_steps": {
                "status": _script(problem["significance"] or "；".join(problem["indicators"][:1])),
                "causes": _script(problem["causes"]),
                "mechanism": _script(problem["mechanism"]),
                "outlook": _script(problem["outlook"]),
                "action": _script(problem["action"]),
            },
            "goal": problem["goal"],
            "analogy": "这个问题按报告原文讲即可，暂不使用类比。",
            "evidence_refs": problem["evidence_refs"],
        })

    key_points = [
        {"text": f"{problem['problem']}（{problem['priority']}）", "anchor": f"2.{problem['no']}"}
        for problem in part["problems"][:5]
    ] or [{"text": "本次资料未包含可供逐项解读的临床问题记录，不能据此判断没有异常。", "anchor": "2.1"}]

    return {
        "part_no": 2,
        "title": SECTION_TITLES[2],
        "key_points": key_points,
        "problems": problems,
        "microbiome_talk": {
            "kind": "script",
            "text": part["microbiome_analysis"] or "菌群这部分报告作为辅助说明，帮助理解整体情况。",
            "must_say": MICROBIOME_DISCLAIMER,
        },
        "script": {
            "kind": "script",
            "opening": "接下来我们一个一个看需要重点关注的问题。",
            "body": ["每个问题我按报告的顺序讲：现在是什么情况、可能的原因、身体里发生了什么、需要留意什么变化、以及先做什么。"],
            "closing": "以上按已有问题记录解读，接下来核对报告中的干预建议。",
        },
        "visual_guide": [
            {
                "anchor": "2.1",
                "target": "text",
                "order": ["先指问题标题", "再逐段往下带读"],
                "talk": "我们对着报告这一节看，我一段一段给您讲。",
            },
        ],
        "faq": [
            {
                "question": "这个问题严重吗？",
                "answer": "报告给的是关注级别，不是严重程度的判断。具体要请医生结合检查来看，所以复查这一步很重要。",
                "source": "报告 1.4 问题分级清单",
            },
            {
                "question": "为什么会出现这个情况？",
                "answer": "报告在「可能的原因」里写了，我念给您听，这些是常见的相关因素，具体原因要医生判断。",
                "source": "报告第二部分",
            },
            {
                "question": "不管它会怎么样？",
                "answer": "报告写了需要关注的后续变化，我们按建议去复查，就是为了把情况弄清楚。",
                "source": "报告第二部分",
            },
        ],
        "cautions": {
            "must_say": ["疑似或待复核的问题必须带限定语", MICROBIOME_DISCLAIMER],
            "easy_wrong": ["不要把风险倾向说成一定会得某种病", "不要解释影像与病理细节"],
            "forbidden": ["不得对报告未收录的问题展开讲解", "不得估计患病概率"],
        },
    }


def _default_part3(ctx: dict[str, Any]) -> dict[str, Any]:
    part = ctx["part3"]
    blocks = part["blocks"]
    by_code = {block["code"]: block for block in blocks}

    pillars = []
    for code, title, section_codes in PILLAR_TITLES:
        content = _join(blocks, section_codes)
        if not content:
            continue
        pillars.append({"code": code, "title": title, "script": _script(content)})

    plan_talk = [
        {"phase": phase["phase"], "script": _script(phase["content"])}
        for phase in part["plan"] if phase["content"]
    ]

    items = [
        {
            "name": exam["name"],
            "purpose": exam["purpose"],
            "talk": exam["reason"],
            "confirm": "您看这一项什么时候方便去？我记一下时间。",
        }
        for exam in part["examinations"]
    ]

    overview = part["microbe_overview"]
    need_text = (
        f"这次菌群结果一共 {overview['total']} 项，其中 {overview['over_count']} 项超标、"
        f"{overview['lack_count']} 项偏低。"
        if overview["total"] else "这次没有菌群检测结果，所以先不谈菌群相关的补充。"
    )

    return {
        "part_no": 3,
        "title": SECTION_TITLES[3],
        "key_points": [
            {"text": "方案分三层：生活方式打地基、菌群调节修土壤、营养素补短板。", "anchor": "3.1"},
            {"text": "生活方式是地基，产品不能替代它。", "anchor": "3.2"},
            {"text": "报告建议的检查要逐项确认时间。", "anchor": "3.6"},
        ],
        "pillars": pillars or [{
            "code": "lifestyle",
            "title": PILLAR_TITLES[0][1],
            "script": _script("以均衡饮食、规律作息和适度运动为基础，先把地基打牢。"),
        }],
        "plan_talk": plan_talk,
        "clinical_followup_talk": {
            "kind": "script",
            "intro": by_code.get("clinical_followup", {}).get("content")
                     or ("接下来这几项是报告建议去做的检查，我们一项一项确认时间。"
                         if items else "本次资料未包含具体检查安排，需先核对原始报告与医生建议。"),
            "items": items,
        },
        "product_bridge": {
            "kind": "script",
            "step1_mechanism": by_code.get("microbiome_rationale", {}).get("content", ""),
            "step2_need": need_text,
            "step3_direction": by_code.get("probiotics", {}).get("content")
                               or by_code.get("prebiotics", {}).get("content", ""),
            "step4_handoff": PRODUCT_HANDOFF,
            "forbidden": list(PRODUCT_FORBIDDEN),
        },
        "script": {
            "kind": "script",
            "opening": "问题看完了，我们来说怎么做。",
            "body": [by_code.get("strategy", {}).get("content") or "先按报告的总体思路安排，再落到每天的执行上。"],
            "closing": "执行上有任何问题随时找我，接下来如果您想看依据，我们翻到第四部分。",
        },
        "visual_guide": [
            {
                "anchor": "3.5",
                "target": "text",
                "order": (["按报告所列阶段从近到远读", "在第一个阶段停下来确认"]
                          if plan_talk else ["先核对报告是否提供阶段安排"]),
                "talk": ("按报告给出的阶段安排，先确认最近一个阶段要做的事。"
                         if plan_talk else "本次资料未包含可执行的阶段安排，先核对原始报告。"),
            },
        ],
        "faq": [
            {
                "question": "这些我全都要做吗？",
                "answer": ("先按报告明确的优先顺序和阶段安排执行，不自行增加项目。"
                           if plan_talk else "需要先核对具体项目及适用情况，不能用通用话术代替个人执行安排。"),
                "source": "报告 3.5 阶段化实施路线图",
            },
            {
                "question": "要不要买点什么吃？",
                "answer": f"先把生活方式这一层做扎实，产品是配套。{PRODUCT_HANDOFF}",
                "source": "报告 3.3 / 3.4",
            },
            {
                "question": "这些检查一定要做吗？",
                "answer": "具体是否需要检查、何时安排，需要结合报告记录和医生建议确认。",
                "source": "报告 3.6 医疗就医建议清单",
            },
        ],
        "cautions": {
            "must_say": ["生活方式是地基，产品不能替代", "就医事项要逐项确认时间"],
            "easy_wrong": ["不要先讲产品再讲生活方式", "不要自行增加报告里没有的补充剂或剂量"],
            "forbidden": list(PRODUCT_FORBIDDEN) + ["不得弱化就医必要性"],
        },
    }


def _default_part4(ctx: dict[str, Any]) -> dict[str, Any]:
    part = ctx["part4"]
    glossary = []
    for line in str(part["glossary"] or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        term, _, plain = line.partition("：")
        if plain:
            glossary.append({"term": term.strip(), "plain": plain.strip()})
    return {
        "part_no": 4,
        "title": SECTION_TITLES[4],
        "note": "不主动逐条讲，作为答疑后盾使用。",
        "key_points": [
            {"text": (f"报告一共列了 {part['evidence_count']} 篇依据，用户问起时可以翻给他看。"
                      if part["evidence_count"] else "本次资料未包含可核对的参考依据，不能代为补造引文。"),
             "anchor": "4.4"},
            {"text": "讲依据只说层级和名称，不念英文题名。", "anchor": "4.1"},
            {"text": "来自特定患病人群的研究，不代表用户本人有那个病。", "anchor": "4.2"},
        ],
        "qa_table": [
            {
                "question": "这个建议有依据吗？",
                "answer": (f"报告第四部分列了 {part['evidence_count']} 篇依据，需要按具体建议的引用编号逐条核对。"
                           if part["evidence_count"] else "本次资料未提供可核对的参考依据，需要补充来源后确认。"),
                "source": "报告 4.4 主要参考依据",
            },
            {
                "question": "网上说的跟这个不一样啊？",
                "answer": "需要核对具体建议的来源、研究对象和适用范围，再结合医生的判断；不能只凭出处名称判断是否适用。",
                "source": "报告 4.1 证据体系说明",
            },
            {
                "question": "这个报告准吗？",
                "answer": part["limits"] or "报告只基于已提供的检查资料，不能替代面诊和医生的综合判断。",
                "source": "报告 4.5 局限性与免责声明",
            },
        ],
        "evidence_levels": [
            {"level": "临床指南与系统综述", "plain": "分量最重，可以用来支持管理方向。"},
            {"level": "观察性研究", "plain": "说明两件事有关联，不等于一定会发生在您身上。"},
            {"level": "基础实验与动物研究", "plain": "只是机制上的提示，不能直接当成人体效果。"},
        ],
        "glossary": glossary or [{"term": "参考范围", "plain": "大多数健康人的检测结果落在这个区间里。"}],
        "limits_talk": _script(
            part["limits"] or "这份报告只基于已经提供的检查资料，不能替代面诊和医生的综合判断。"
        ),
        "script": {
            "kind": "script",
            "opening": "您要是想看依据，我们翻到第四部分。",
            "body": [part["evidence_reading"] or "报告里的依据分几类，用途不一样，我给您说说怎么看。"],
            "closing": "依据就在报告上，您随时可以自己翻。",
        },
        "visual_guide": [
            {
                "anchor": "4.4",
                "target": "table",
                "order": ["先指编号", "再指题名与链接"],
                "talk": "您看每条建议后面的编号，对应的就是这张表里的这一篇。",
            },
        ],
        "faq": [
            {
                "question": "这些文献我能自己看吗？",
                "answer": "可以，报告里每篇都附了链接，不过内容比较专业，有疑问我们一起看。",
                "source": "报告 4.4 主要参考依据",
            },
            {
                "question": "这个词是什么意思？",
                "answer": "报告 4.3 有名词解释，我按大白话给您说一遍。",
                "source": "报告 4.3 名词科学释义",
            },
            {
                "question": "为什么不直接说结论？",
                "answer": "报告把依据的类型标出来，是为了让您知道每条建议的分量，避免被不可靠的说法带偏。",
                "source": "报告 4.1 证据体系说明",
            },
        ],
        "cautions": {
            "must_say": ["讲依据只说层级与名称", "患病人群的研究结论不能套到本人身上"],
            "easy_wrong": ["不要把关联说成因果", "不要凭印象报文献名、作者或年份"],
            "forbidden": ["不得编造文献", "不得用文献证明某个产品有效"],
        },
    }


_PART_BUILDERS = {1: _default_part1, 2: _default_part2, 3: _default_part3, 4: _default_part4}


def default_section(part_no: int, contexts: dict[str, Any]) -> dict[str, Any]:
    """单组五件套的兜底内容；模型失败时只降级这一组。"""
    builder = _PART_BUILDERS.get(int(part_no))
    if builder is None:
        raise ValueError(f"unsupported explain part: {part_no!r}")
    return builder(contexts)


def default_opening(contexts: dict[str, Any]) -> dict[str, Any]:
    """开场白与收尾脚本的兜底内容。"""
    meta = contexts["meta"]
    top_three = contexts["part1"]["top_three"]
    name = meta.get("name") or "您"
    return {
        "opening_script": {
            "kind": "script",
            "self_intro": f"{name}您好，我是您的健康顾问，今天由我帮您把这份报告过一遍。",
            "role_statement": "我负责帮您看懂这份报告，涉及诊断和用药的问题，我会帮您对接医生。",
            "agenda": "大概三十分钟，我分四部分讲，您随时可以打断我提问。",
        },
        "wrap_up_script": {
            "kind": "script",
            "summary": "我总结一下您接下来要做的几件事，您听完帮我复述一遍，看我说清楚没有。",
            "three_things": list(top_three),
            "confirm": "就医这几项我们确认一下时间，您打算哪天去？",
            "follow_up": "回访时间按报告与医生建议确认，有变化及时联系。",
            "contact": "这期间有任何疑问，直接微信或电话找我都可以。",
        },
    }


# ---------------------------------------------------------------- 整份兜底

def build_manual_meta(contexts: dict[str, Any]) -> dict[str, Any]:
    meta = contexts["meta"]
    name = meta.get("name") or ""
    return {
        "title": f"{name}用户报告解读参考手册" if name else "用户报告解读参考手册",
        "scene": MANUAL_SCENE,
        "report_date": meta.get("report_date", ""),
        "evaluate_status": meta.get("evaluate_status", ""),
        # 资料缺口与部分成功原因只给【内部】看，不进话术
        "data_gaps": contexts["part1"].get("data_gaps", ""),
        "partial_reasons": list(meta.get("partial_reasons", [])),
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
    """把五个子任务的产出拼成完整手册报文。

    ``issues`` 是归一化阶段记录的改动；``compliance`` 为真时再做一遍禁用语
    替换与必含声明兜底（兜底路径自己拼的内容本来就合规，可以关掉）。
    """
    meta = contexts["meta"]
    payload: dict[str, Any] = {
        "type": "explain",
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
        "evidence": contexts["part4"]["evidence"],
    }
    issues = list(issues or [])
    if compliance:
        payload, compliance_issues = apply_compliance(payload, fallback=_fallback_for(contexts, user_id, session_id))
        issues.extend(compliance_issues)
    payload["closing"]["self_check"] = run_self_check(payload, issues=issues)
    return payload


def _fallback_for(contexts: dict[str, Any], user_id: str, session_id: str) -> dict[str, Any]:
    """整段话术被清空时的取材来源：同一份上下文拼出的兜底手册。"""
    return assemble_manual(
        contexts=contexts,
        sections=[default_section(part_no, contexts) for part_no in (1, 2, 3, 4)],
        opening=default_opening(contexts),
        user_id=user_id,
        session_id=session_id,
        compliance=False,
    )


def default_explain_manual(
    *,
    evaluate: dict[str, Any],
    user_id: str,
    session_id: str,
) -> dict[str, Any]:
    """整份手册的兜底：模型全挂时仍然输出这位受检者的真实内容。"""
    contexts = build_explain_contexts(evaluate or {})
    return _fallback_for(contexts, user_id, session_id)


def empty_explain_manual() -> dict[str, Any]:
    """空报告骨架：字段类型与正常报文一致，前端不会因为拿到 {} 而渲染报错。"""
    return {
        "manual_meta": {},
        "preface": {"quick_card": {}, "reader_notice": [], "opening_script": {}},
        "sections": [],
        "closing": {"wrap_up_script": {}, "forbidden_words": [], "self_check": []},
        "evidence": [],
    }


__all__ = [
    "SECTION_TITLES",
    "READER_NOTICE",
    "FORBIDDEN_WORDS",
    "MICROBIOME_DISCLAIMER",
    "PRODUCT_HANDOFF",
    "assemble_manual",
    "build_manual_meta",
    "default_explain_manual",
    "default_opening",
    "default_section",
    "empty_explain_manual",
    "run_self_check",
]
