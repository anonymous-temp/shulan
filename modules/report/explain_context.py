# -*- coding: utf-8 -*-
"""报告解读（explain）上下文构建器。

把评估报告（约 300KB、clinical_context 上千个键）裁剪成四份小上下文，
分别喂给四个子任务的提示词。**本模块不调用模型**，只做数据搬运与整理：

    build_explain_contexts(data) -> {
        "meta":       受检者与报告元信息,
        "quick_card": 关键数据速查卡（逐字原文，不过模型）,
        "part1": 第一部分 健康综合评估,
        "part2": 第二部分 主要问题分析,
        "part3": 第三部分 干预方案,
        "part4": 第四部分 循证支持,
    }

设计原则
--------
1. 数值、单位、参考范围、日期一律原样搬运，不做换算、不做四舍五入，
   对应《解读参考提示词》红线 1「数据逐字」。
2. clinical_context 是扁平 dict，键名里的点号是键名的一部分而不是嵌套，
   本模块负责把 ``report.section.*`` / ``report.problem.<id>.*`` /
   ``report.observation.<id>.*`` / ``report.auxiliary.<id>.*`` 四类前缀
   还原成结构化对象。
3. 只保留 finding_type == "disease" 的临床问题；菌群空壳条目（只有菌名
   与数值、没有机制与建议）不参与逐条解读。
4. 所有取值都走 ``_text`` / ``_list``，None 统一成 "" 或 []，
   下游提示词与兜底逻辑不需要再判空类型。
"""

from __future__ import annotations

import json
import re
from typing import Any

# ---------------------------------------------------------------- 常量

# 六维固定顺序：心脑血管 → 代谢 → 消化 → 免疫 → 泌尿 → 神经
DIMENSION_ORDER: tuple[str, ...] = (
    "cardiovascular_system",
    "metabolic_system",
    "digestive_system",
    "immune_system",
    "urinary_system",
    "nervous_system",
)

# 临床问题优先级，从急到缓；用于排序与「需要优先关注的三件事」
PRIORITY_ORDER: tuple[str, ...] = ("优先复核", "建议复核", "常规关注")

# report.problem.<item_id>.<part> 的片段，对应用户报告第二部分的五段结构
PROBLEM_PARTS: tuple[str, ...] = (
    "significance",  # 一句话说明（1.4 分级清单）
    "causes",        # 二、可能的原因
    "mechanism",     # 三、发生发展的机制
    "outlook",       # 四、需要关注的后续变化
    "action",        # 五、怎么办、先做什么
    "goal",          # 本阶段目标
    "certainty",     # suspected / confirmed 等，话术限定语用
)

# 第三部分按用户报告 3.1–3.7 的顺序取用的章节
PART3_SECTIONS: tuple[tuple[str, str], ...] = (
    ("strategy", "3.1 总体思路"),
    ("diet", "3.2 饮食与饮水"),
    ("exercise", "3.2 运动与久坐管理"),
    ("sleep", "3.2 睡眠、压力与日常行为"),
    ("microbiome_rationale", "3.3 调节理由与适用条件"),
    ("probiotics", "3.3 益生菌评估"),
    ("prebiotics", "3.3 膳食底物与益生元"),
    ("barrier", "3.3 肠道屏障支持"),
    ("microbiome_followup", "3.3 调节后的观察与复评"),
    ("nutrition", "3.4 个性化营养素补充"),
    ("clinical_followup", "3.6 医疗就医建议清单"),
    ("monitoring", "3.7 复查与监测计划"),
    ("alerts", "3.7 需要及时就医的情况"),
    ("avoid", "3.7 不建议的做法"),
)

PLAN_PHASES: tuple[tuple[str, str], ...] = (
    ("未来1-2周", "phase_1"),
    ("未来1个月", "phase_2"),
    ("未来2-3个月", "phase_3"),
    ("未来6-12个月", "phase_4"),
)

# 速查卡是否保留 22 行菌群原始结果。逐条解读一律不做（菌群条目没有机制与
# 建议），这里只决定「解读人手上要不要有这张表可以报数」。
INCLUDE_MICROBE_TABLE = True

# 单条文献结论的截断长度：提示词里只需要够改写成一句应答话术。
EVIDENCE_CONCLUSION_LIMIT = 260
EVIDENCE_FACT_LIMIT = 3
# 参考文献最多列几位作者，超出时补「等 / et al」
EVIDENCE_AUTHOR_LIMIT = 3
# 参与逐条讲解的临床问题上限（评估报告本身最多 5 条）
MAX_PROBLEMS = 5


# ---------------------------------------------------------------- 基础工具

def _text(value: Any) -> str:
    """任何标量转成去空白的字符串；None / 非字符串一律安全降级。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _list(value: Any) -> list:
    return list(value) if isinstance(value, list) else []


def _dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _clinical_context(data: dict) -> dict[str, Any]:
    return _dict(_dict(data.get("diagnosis_summary")).get("clinical_context"))


def _group_by_prefix(context: dict[str, Any], prefix: str) -> dict[str, dict[str, str]]:
    """把 ``<prefix><id>.<field>`` 形式的扁平键还原成 {id: {field: value}}。

    保持源字典的插入顺序，因此还原后的条目顺序与评估报告一致。
    """
    grouped: dict[str, dict[str, str]] = {}
    for key, value in context.items():
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix):]
        if "." not in rest:
            continue  # 形如 report.auxiliary.version 的版本号，跳过
        item_id, field = rest.rsplit(".", 1)
        grouped.setdefault(item_id, {})[field] = _text(value)
    return grouped


def _sections(context: dict[str, Any]) -> dict[str, str]:
    prefix = "report.section."
    return {
        key[len(prefix):]: _text(value)
        for key, value in context.items()
        if key.startswith(prefix)
    }


def _problem_parts(context: dict[str, Any], item_id: str) -> dict[str, str]:
    prefix = f"report.problem.{item_id}."
    parts = {part: _text(context.get(prefix + part)) for part in PROBLEM_PARTS}
    return {key: value for key, value in parts.items() if value}


# ---------------------------------------------------------------- 取数

def _clinical_findings(data: dict) -> list[dict[str, Any]]:
    """只保留临床问题；菌群空壳条目不参与解读。

    新版评估报告的 abnormal_findings 已经只放临床问题，旧版里混着 22 条
    只有菌名与数值的 microbe 条目，这里统一过滤掉；万一某份报告全部条目
    都没有 finding_type，则原样返回，避免把整块内容丢空。
    """
    findings = _list(_dict(data.get("diagnostic_basis")).get("abnormal_findings"))
    typed = [item for item in findings if isinstance(item, dict) and item.get("finding_type")]
    if not typed:
        return [item for item in findings if isinstance(item, dict)][:MAX_PROBLEMS]
    diseases = [item for item in typed if item.get("finding_type") == "disease"]
    return (diseases or typed)[:MAX_PROBLEMS]


def _priority_rank(priority: str) -> int:
    try:
        return PRIORITY_ORDER.index(priority)
    except ValueError:
        return len(PRIORITY_ORDER)


def _indicator_values(finding: dict) -> list[str]:
    """问题的原始来源记录；每条都是含原值、参考范围与日期的完整文本。"""
    values = []
    for indicator in _list(finding.get("indicators")):
        if isinstance(indicator, dict):
            value = _text(indicator.get("value")) or _text(indicator.get("name"))
        else:
            value = _text(indicator)
        if value:
            values.append(value)
    return values


def _evidence_index(data: dict) -> dict[str, dict[str, Any]]:
    """文献按编号建索引；编号可能是 int 也可能是 str，统一成 str 键。"""
    index: dict[str, dict[str, Any]] = {}
    for item in _list(data.get("evidence")):
        if not isinstance(item, dict):
            continue
        number = _text(item.get("number"))
        if number:
            index[number] = item
    return index


def _authors(value: Any) -> list[str]:
    """作者可能是数组也可能是一整串，统一成数组；只留展示用的前几位。"""
    if isinstance(value, str):
        names = [part.strip() for part in re.split(r"[,，;；]", value) if part.strip()]
    else:
        names = [_text(name) for name in _list(value) if _text(name)]
    return names[:EVIDENCE_AUTHOR_LIMIT + 1]  # 多留一位用于判断是否要写「等 / et al」


def _slim_evidence(item: dict) -> dict[str, Any]:
    """文献瘦身：只留展示与答疑用得上的字段。

    参考文献按「作者. 文献名. 期刊. 年份.」展示，因此 authors / source /
    publication_date 一并带出；精简版回调里这三项可能没有，缺哪项展示层就
    少写哪一段，不补造。
    """
    conclusion = _text(item.get("conclusion"))
    if len(conclusion) > EVIDENCE_CONCLUSION_LIMIT:
        conclusion = conclusion[:EVIDENCE_CONCLUSION_LIMIT] + "…"
    return {
        "number": _text(item.get("number")),
        "title": _text(item.get("title")),
        "url": _text(item.get("url")),
        "authors": _authors(item.get("authors") or item.get("author")),
        "organization": _text(item.get("organization")),
        "source": _text(item.get("source")),
        "publication_date": _text(item.get("publication_date")),
        "study_type": _text(item.get("study_type")) or _text(item.get("evidence_type")),
        "conclusion": conclusion,
        "patient_related_facts": [
            _text(fact) for fact in _list(item.get("patient_related_facts"))[:EVIDENCE_FACT_LIMIT] if _text(fact)
        ],
    }


def _refs(value: Any) -> list[str]:
    """evidence_refs 可能是 int 数组也可能是 str 数组，统一成 str。"""
    return [_text(ref) for ref in _list(value) if _text(ref)]


# ---------------------------------------------------------------- 元信息 / 速查卡

def build_meta(data: dict) -> dict[str, Any]:
    subject = _dict(data.get("subject"))
    sample = _dict(data.get("sample"))
    return {
        "name": _text(subject.get("name")),
        "gender": _text(subject.get("gender")),
        "age": _text(subject.get("age")),
        "report_date": _text(sample.get("report_date")) or _text(subject.get("report_date")),
        "generated_at": _text(data.get("generated_at")),
        "evaluate_status": _text(data.get("status")),
        # 部分成功时说明哪些环节资料不全，供【内部】提示使用，不进话术
        "partial_reasons": [_text(reason) for reason in _list(data.get("partial_reasons")) if _text(reason)],
    }


def _observation_rows(context: dict[str, Any], kind: str) -> list[dict[str, str]]:
    rows = []
    for item in _group_by_prefix(context, "report.observation.").values():
        if item.get("kind") != kind:
            continue
        rows.append({
            "name": item.get("name", ""),
            "value": item.get("value", ""),
            "unit": item.get("unit", ""),
            "reference": item.get("reference", ""),
            "status": item.get("status", ""),
            "date": item.get("date", ""),
            "related_problem": item.get("problems", ""),
        })
    return rows


def _microbe_rows(context: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    for item in _group_by_prefix(context, "report.auxiliary.").values():
        if item.get("kind") != "microbe":
            continue
        rows.append({
            "name": item.get("name", ""),
            "value": item.get("value", ""),
            "unit": item.get("unit", ""),
            "reference": item.get("reference", ""),
            "status": item.get("status", ""),
        })
    return rows


def _microbe_overview(context: dict[str, Any], sections: dict[str, str]) -> dict[str, Any]:
    """菌群只给汇总口径与整体说明，不逐条解读；原始行在速查卡里。"""
    rows = _microbe_rows(context)
    return {
        "total": len(rows),
        "over_count": sum(1 for row in rows if "超标" in row["status"]),
        "lack_count": sum(1 for row in rows if "缺乏" in row["status"]),
        "note": sections.get("microbiome_analysis", ""),
    }


def build_quick_card(data: dict) -> dict[str, Any]:
    """关键数据速查卡：全部逐字摘自评估报告，不经过模型。"""
    context = _clinical_context(data)
    assessment = _dict(data.get("healthAssessment"))
    microbes = _microbe_rows(context)
    card = {
        "overall": {
            "score": _text(assessment.get("displayOverallScore")) or _text(assessment.get("overallScore")),
            "level_label": _text(assessment.get("levelLabel")),
        },
        "dimension_scores": [
            {
                "name": _text(dimension.get("name")),
                "score": _text(dimension.get("displayScore")) or _text(dimension.get("score")),
                "level_label": _text(dimension.get("levelLabel")),
            }
            for dimension in _sorted_dimensions(assessment)
        ],
        "indicator_rows": _observation_rows(context, "indicator"),
        "imaging_rows": _observation_rows(context, "imaging"),
        "microbe_row_count": len(microbes),
    }
    if INCLUDE_MICROBE_TABLE:
        # 不逐条解读，但解读人被问到具体某个菌时要能照着报数
        card["microbe_rows"] = microbes
    return card


# ---------------------------------------------------------------- 第一部分

def _sorted_dimensions(assessment: dict) -> list[dict[str, Any]]:
    dimensions = [item for item in _list(assessment.get("dimensions")) if isinstance(item, dict)]
    return sorted(dimensions, key=lambda item: _dimension_rank(_text(item.get("code"))))


def _dimension_rank(code: str) -> int:
    try:
        return DIMENSION_ORDER.index(code)
    except ValueError:
        return len(DIMENSION_ORDER)


def build_part1_context(data: dict) -> dict[str, Any]:
    """第一部分 健康综合评估（全局速览）。

    素材：六维评分对象 + 优先关注事项 + 三张异常总览表 + 问题分级清单。
    """
    context = _clinical_context(data)
    sections = _sections(context)
    assessment = _dict(data.get("healthAssessment"))
    findings = _clinical_findings(data)

    dimensions = []
    for dimension in _sorted_dimensions(assessment):
        dimensions.append({
            "code": _text(dimension.get("code")),
            "name": _text(dimension.get("name")),
            "score": _text(dimension.get("displayScore")) or _text(dimension.get("score")),
            "level_label": _text(dimension.get("levelLabel")),
            "explanation": _text(dimension.get("explanation")),
            "related_diseases": [
                {"name": _text(disease.get("name")), "risk_label": _text(disease.get("riskLabel"))}
                for disease in _list(dimension.get("relatedDiseases"))
                if isinstance(disease, dict) and _text(disease.get("name"))
            ],
            # 以下为【内部】提示素材：评分口径与资料缺口，不进照读话术
            "confidence": _text(dimension.get("confidence")),
            "conflicts": [_text(conflict) for conflict in _list(dimension.get("conflicts")) if _text(conflict)],
        })

    ordered = sorted(findings, key=lambda item: _priority_rank(_text(item.get("priority"))))
    grading = [
        {
            "priority": _text(finding.get("priority")),
            "problem": _text(finding.get("problem")),
            "significance": _problem_parts(context, _text(finding.get("item_id"))).get("significance", ""),
        }
        for finding in ordered
    ]
    priority_items = [
        {
            "problem": _text(finding.get("problem")),
            "priority": _text(finding.get("priority")),
            "actions": [_text(action) for action in _list(finding.get("recommendations")) if _text(action)],
        }
        for finding in ordered
    ]

    return {
        "overview": _text(_dict(data.get("diagnosis_summary")).get("conclusion")) or sections.get("overview", ""),
        "assessment": {
            "score": _text(assessment.get("displayOverallScore")) or _text(assessment.get("overallScore")),
            "level_label": _text(assessment.get("levelLabel")),
            "summary": _text(assessment.get("summary")),
            "dimensions": dimensions,
            "lowest_dimension": dimensions[_lowest_index(dimensions)]["name"] if dimensions else "",
            "highest_dimension": dimensions[_highest_index(dimensions)]["name"] if dimensions else "",
        },
        "priority_items": priority_items,
        # 「需要优先关注的三件事」：按优先级取前三条，供收尾复述环节复用
        "top_three": [item["problem"] for item in priority_items[:3]],
        "grading_list": grading,
        "indicator_rows": _observation_rows(context, "indicator"),
        "imaging_rows": _observation_rows(context, "imaging"),
        # 菌群表只给汇总口径，不逐条讲；原始 22 行在速查卡里
        "microbe_overview": _microbe_overview(context, sections),
        "data_gaps": sections.get("gaps", ""),
    }


def _lowest_index(dimensions: list[dict[str, Any]]) -> int:
    return min(range(len(dimensions)), key=lambda i: _score_value(dimensions[i]["score"]))


def _highest_index(dimensions: list[dict[str, Any]]) -> int:
    return max(range(len(dimensions)), key=lambda i: _score_value(dimensions[i]["score"]))


def _score_value(score: str) -> float:
    try:
        return float(score)
    except (TypeError, ValueError):
        return -1.0


# ---------------------------------------------------------------- 第二部分

def build_part2_context(data: dict) -> dict[str, Any]:
    """第二部分 主要问题分析（机制讲解是核心）。

    每个临床问题带齐五段原文（现状 / 原因 / 机制 / 后续变化 / 怎么办）+
    本阶段目标 + 原始指标文本 + 引用文献，供模型改写成照读话术。
    """
    context = _clinical_context(data)
    sections = _sections(context)
    evidence_index = _evidence_index(data)
    findings = sorted(_clinical_findings(data), key=lambda item: _priority_rank(_text(item.get("priority"))))

    problems = []
    for order, finding in enumerate(findings, start=1):
        item_id = _text(finding.get("item_id"))
        parts = _problem_parts(context, item_id)
        refs = _refs(finding.get("evidence_refs"))
        problems.append({
            "no": order,
            "problem": _text(finding.get("problem")),
            "priority": _text(finding.get("priority")),
            # suspected 等限定：话术必须带「疑似 / 待复核」，不得说成已确认
            "certainty": parts.get("certainty", ""),
            "indicators": _indicator_values(finding),
            "significance": parts.get("significance", ""),
            "causes": parts.get("causes", ""),
            "mechanism": parts.get("mechanism", ""),
            "outlook": parts.get("outlook", ""),
            "action": parts.get("action", "") or "\n".join(
                _text(item) for item in _list(finding.get("recommendations"))
            ),
            "goal": parts.get("goal", "") or _text(_dict(finding.get("tips")).get("practical_goal")),
            "evidence_refs": refs,
            "evidence_brief": [
                {"number": ref, "title": _text(_dict(evidence_index.get(ref)).get("title"))}
                for ref in refs if ref in evidence_index
            ],
        })

    return {
        "problems": problems,
        # 2.6 肠道菌群分析（辅助说明）：整体讲法的原文来源
        "microbiome_analysis": sections.get("microbiome_analysis", ""),
        "data_gaps": sections.get("gaps", ""),
    }


# ---------------------------------------------------------------- 第三部分

def build_part3_context(data: dict) -> dict[str, Any]:
    """第三部分 干预方案（落地执行与产品衔接）。"""
    context = _clinical_context(data)
    sections = _sections(context)
    advice = _dict(data.get("diagnostic_advice"))

    # health_recommendations 只提供 topic → evidence_refs 的映射，正文在 sections 里
    refs_by_topic: dict[str, list[str]] = {}
    for item in _list(advice.get("health_recommendations")):
        if not isinstance(item, dict):
            continue
        topic = _text(item.get("topic"))
        if topic:
            refs_by_topic[topic] = _refs(item.get("evidence_refs"))

    blocks = []
    for code, title in PART3_SECTIONS:
        content = sections.get(code, "")
        if not content:
            continue
        blocks.append({
            "code": code,
            "title": title,
            "content": content,
            "evidence_refs": refs_by_topic.get(f"report.section.{code}", []),
        })

    plan_source = _dict(advice.get("Plan"))
    plan = []
    for phase, fallback_code in PLAN_PHASES:
        content = _text(plan_source.get(phase)) or sections.get(fallback_code, "")
        plan.append({"phase": phase, "content": content})

    examinations = [
        {
            "name": _text(item.get("name")),
            "purpose": _text(item.get("purpose")),
            "reason": _text(item.get("reason")),
            "evidence_refs": _refs(item.get("evidence_refs")),
        }
        for item in _list(advice.get("recommended_examinations"))
        if isinstance(item, dict) and _text(item.get("name"))
    ]

    return {
        "blocks": blocks,
        "plan": plan,
        "examinations": examinations,
        # 产品衔接第二步「用数据指出缺口」的素材：缺乏/超标的菌群条目计数
        "microbe_overview": _microbe_overview(context, sections),
    }


# ---------------------------------------------------------------- 第四部分

def build_part4_context(data: dict) -> dict[str, Any]:
    """第四部分 循证支持（答疑弹药库）。"""
    context = _clinical_context(data)
    sections = _sections(context)
    evidence = [_slim_evidence(item) for item in _list(data.get("evidence")) if isinstance(item, dict)]
    return {
        "evidence_reading": sections.get("evidence_reading", ""),
        "glossary": sections.get("glossary", ""),
        "limits": sections.get("limits", ""),
        "evidence": evidence,
        "evidence_count": len(evidence),
    }


# ---------------------------------------------------------------- 汇总入口

def build_explain_contexts(data: dict) -> dict[str, Any]:
    """一次性构建报告解读所需的全部上下文。"""
    data = _dict(data)
    return {
        "meta": build_meta(data),
        "quick_card": build_quick_card(data),
        "part1": build_part1_context(data),
        "part2": build_part2_context(data),
        "part3": build_part3_context(data),
        "part4": build_part4_context(data),
    }


def dumps(value: Any) -> str:
    """提示词渲染用：紧凑但保留中文的 JSON 文本。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "build_explain_contexts",
    "build_meta",
    "build_quick_card",
    "build_part1_context",
    "build_part2_context",
    "build_part3_context",
    "build_part4_context",
    "dumps",
    "DIMENSION_ORDER",
    "PRIORITY_ORDER",
    "INCLUDE_MICROBE_TABLE",
]
