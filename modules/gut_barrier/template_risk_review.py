"""Full template review with independently audited model or rubric results.

This current-template publication step runs after named citations are resolved and
before first-citation ordering. It consumes the server's audited rule/model
results without mutating them or treating missing measurements as normal.
"""
from __future__ import annotations

from copy import deepcopy
import re

from .integration_content import (
    ATTENTION_OVERVIEW_TITLE, DISEASE_INTRODUCTION, PRODUCT_DISEASE_INTRODUCTION, RISK_COLUMNS,
)
from .reference_order import PRIMARY_TITLE, REFERENCE_FIELDS, reference_keys
from .reference_priority import preferred_mechanism
from .risk_model import build_disease_risks, mechanism_evidence
from .risk_index_rules import VERSION as RISK_RULE_VERSION, confirmed_rule_result, confirmed_rule_results
from .risk_results import RISK_LEVEL_NAMES as _RISK_LEVEL_NAMES, model_result as _model_result
from .rules import MARKER_UNITS, decimal_value, score_marker
from .review_release import (
    barrier_abnormal_names, barrier_injury_grade,
)


RISK_OVERVIEW_TITLE = "本次疾病风险评估总览"
RISK_OVERVIEW_COLUMNS = ["系统", "疾病", "风险指数", "风险等级"]
_BASIS_TITLE = "本次相关因素与复核依据"
_IMAGING_TITLE = "影像检查待核实事项"
_DATA_NOTICE_TITLE = "风险评估资料提示"
STATE_FLOOR_NOTICE_TITLE = "状态下限说明"
_INTRODUCTION = "肠道屏障受损与慢性低度炎症是多种慢性疾病与机体衰老的共同上游机制。本章基于本次检测结果与相关健康评估，评估相关疾病风险。风险等级（低、中、高）为基于检测指标的健康风险评估推断，用于风险提示与预防参考，不构成疾病诊断。"
# Exact generated introductions from integration_content and the original
# chapters layout only. Anonymous patient-specific or safety text is not boilerplate.
_GENERATED_INTRODUCTIONS = frozenset({
    _INTRODUCTION, DISEASE_INTRODUCTION, PRODUCT_DISEASE_INTRODUCTION,
    "以下按系统列示相关疾病与机制关联。风险指数需完整模型及相应个体资料支持；未评估不表示低风险，也不代表已患该病。",
})
_DISPLAY_NAMES = {"nafld": "代谢相关脂肪性肝病"}
_CITATION = re.compile(r"\[\d+(?:\s*[,，、]\s*\d+)*\]")
# Product-supplied section 2.9 combinations. These are display classifications
# based on elevations above the normal reference, including subhealth scores;
# they do not change the frozen scoring rules, treatment, or source attribution.
_SUMMARY_INJURY_TYPES = {
    "+++": "应激性 + 炎症性损伤",
    "+-+": "炎症性损伤（伴移位）",
    "-++": "应激性损伤（伴移位）",
    "--+": "非肠源性移位",
    "+--": "单纯炎症性损伤",
    "-+-": "单纯应激性损伤",
    "++-": "炎症性 + 应激性损伤（无移位）",
}

# Condensed from the catalog's reviewed primary studies, not its example
# patient scores. Experimental/observational scope is part of the claim.
_MECHANISMS = {
    "nafld": "实验研究中，肠源性细菌产物相关的 LPS–TLR4 信号可参与肝内炎症及纤维化反应，并与脂质代谢变化相互影响。",
    "ibs": "部分腹泻型肠易激综合征患者研究观察到黏膜通透性增加与肥大细胞增多相关，可能涉及黏膜免疫和内脏敏感性变化。",
    "ibd": "动物研究中，黏液层与上皮屏障变化可增加微生物和黏膜免疫系统的接触，并与肠道炎症相互影响。",
    "cirrhosis": "慢性肝损伤实验中，细菌产物相关 TLR4 信号可增强肝星状细胞的促纤维化反应，参与胶原沉积。",
    "type2_diabetes": "动物实验和人体脂肪组织研究显示，LPS 相关免疫信号可促进炎症介质释放，并与胰岛素敏感性下降相关。",
    "obesity": "动物实验和人体脂肪组织研究显示，肠源性 LPS 相关信号与脂肪组织免疫激活及代谢变化相关。",
    "hyperlipidemia": "人体低剂量内毒素刺激研究观察到急性炎症反应与富含甘油三酯脂蛋白的代谢变化相关。",
    "hyperuricemia": "高尿酸人群研究中，ABCG2 转运功能变化与肾外尿酸清除减少相关，涉及肠道尿酸排泄过程。",
    "atherosclerosis": "动物研究中，TLR4/MyD88 免疫信号与血管壁炎症及斑块形成相关。",
    "coronary_heart_disease": "血管壁炎症与脂质负荷共同参与动脉粥样硬化，局部免疫细胞活化与斑块进展相关。",
    "hypertension": "实验研究中，血管紧张素Ⅱ相关的 IL-6 信号可影响内皮素表达与肾脏纤维化，参与血压调节变化。",
    "stroke": "部分缺血性卒中与动脉粥样硬化及血栓形成相关；血管壁炎症是斑块形成机制中的一个环节。",
    "alzheimers": "认知受损老年人研究中，特定菌群及外周炎症变化与脑淀粉样蛋白沉积相关。",
    "parkinsons": "α-突触核蛋白过表达小鼠研究中，菌群及其代谢物可影响小胶质细胞活化、神经炎症和运动表现。",
    "depression": "部分抑郁或焦虑人群研究观察到血浆 LPS、zonulin 与 FABP2 升高并与菌群改变相关，涉及屏障、外周炎症与肠脑轴。",
    "anxiety": "部分肠易激综合征患者菌群移植至无菌小鼠后，研究观察到屏障改变、免疫激活及焦虑样行为。",
    "rheumatoid_arthritis": "类风湿性关节炎患者研究中，肠道紧密连接蛋白减少及细菌移位相关标志物与疾病活动度存在关联。",
    "systemic_lupus": "遗传易感动物中，特定肠道共生菌移位可激活适应性免疫和自身抗体反应，部分自身免疫患者组织中也检出相关细菌。",
    "chronic_kidney_disease": "尿毒症患者血浆处理肠上皮细胞的实验显示紧密连接蛋白减少与通透性改变，涉及肠肾轴的双向作用。",
    "colorectal_cancer": "肠腺瘤动物模型中，局部屏障缺陷使微生物产物接触免疫细胞，激活 IL-23/IL-17 通路并支持肿瘤生长。",
    "liver_cancer": "慢性肝损伤动物模型中，肠道菌群及 TLR4 信号可促进已受损肝脏的肿瘤进展。",
    "lung_cancer": "既往心肌梗死且炎症升高人群的探索性试验分析观察到，IL-1β 抑制与肺癌事件变化相关。",
}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _items(value):
    return value if isinstance(value, list) else []


def _cells(row):
    return _items(row.get("cells")) if isinstance(row, dict) else _items(row)


def _level(row, assessment, code=None, rule_results=None):
    ruled = (rule_results.get(code or row.get("code")) if rule_results is not None
             else confirmed_rule_result(assessment, code or row.get("code")))
    if _current_rule_assessment(assessment):
        # The fixed catalog uses one scale and one quota assignment. An older
        # model cannot replace a missing or unverified current-rule result.
        return ruled if ruled is not None else (None, None)
    if _model_result(row, assessment):
        # Translate equivalent names only after model validation. Never derive
        # grades from scores, attention labels, or the Word example's grades.
        level = row.get("riskLevel")
        level = _RISK_LEVEL_NAMES.get(level) if isinstance(level, str) else None
        return deepcopy(row.get("riskIndex")), deepcopy(level)
    if ruled is not None:
        return ruled
    # A non-quantified review state is not a fourth risk grade. Its clinical
    # basis remains internal; necessary safety callouts remain public.
    return None, None


def _current_rule_assessment(assessment):
    return ("v2.7" in RISK_RULE_VERSION
            and _dict(assessment.get("riskRuleAssessment")).get("version") == RISK_RULE_VERSION)


def _append_reference(references, reference):
    projected = {key: deepcopy(reference.get(key)) for key in REFERENCE_FIELDS}
    projected["location"] = None  # Internal claim scope is not a citation locator.
    keys = reference_keys(projected)
    for index, existing in enumerate(references, 1):
        if keys & reference_keys(existing):
            return index
    references.append(projected)
    return len(references)


def _selected_citations(row, assessment, references, *, allowed_ids=None):
    evidence_ids = _items(_dict(row.get("qualitative")).get("evidenceIds"))
    if allowed_ids is not None:
        evidence_ids = [key for key in evidence_ids if key in allowed_ids]
    wanted = {f"评估{str(key)[1:]}" for key in evidence_ids}
    result = []
    for ref in _items(_dict(assessment.get("diseaseAssessment")).get("references")):
        if not isinstance(ref, dict):
            continue
        match = re.match(r"^\[(评估\d+)\]\s*", str(ref.get("citation") or ""))
        if match and match[1] in wanted:
            clean = {**ref, "citation": ref["citation"][match.end():]}
            number = _append_reference(references, clean)
            if number not in result:
                result.append(number)
    return "".join(f"[{number}]" for number in result)


def _summary_sources(assessment):
    """Mention only sources that contribute to risk assessment."""
    context = _dict(assessment.get("context"))
    sources = ["本次检测结果"]
    if any(isinstance(row, dict) and row.get("temporalStatus") in {"current", "historical"}
           and not row.get("conflict") and row.get("status") not in {"conflict", "needs_review"}
           for row in _items(context.get("clinicalFindings"))):
        sources.append("已提供的检查资料")
    return "、".join(sources)


def _risk_summary(candidates, assessment):
    """At most three catalog diseases, ranked within a single audited scale.

    Independent model indices and the trial rubric are never numerically mixed.
    Follow-up directions are not renamed into diagnosed chronic diseases.
    """
    families = {"model": [], "rule": []}
    for family, cells in candidates:
        if cells[2] in {"低", "中", "高"}:
            families[family].append(cells)
    model = _dict(assessment.get("riskModel"))
    metadata = _dict(model.get("metadata"))
    if (_dict(model.get("scale")).get("kind") != "relative_index"
            or not metadata.get("endpoint") or not metadata.get("timeHorizon")):
        families["model"] = []  # No cross-disease ranking without a declared model scale.
    clauses, remaining = [], 3
    for family in ("model", "rule"):
        rows = sorted(families[family], key=lambda cells: (-{"低": 0, "中": 1, "高": 2}[cells[2]], -cells[1]))
        limit = min(remaining, 2 if family == "model" and families["rule"] else 3)
        rows = rows[:limit]
        if not rows:
            continue
        remaining -= len(rows)
        values = "、".join(f"{row[0]}（{row[1]:g}分，{row[2]}风险）" for row in rows)
        scope = "独立模型下，" if family == "model" else ""
        qualification = ("；这些项目的相关风险因素相对突出，宜优先开展预防管理"
                         if any(row[2] != "低" for row in rows) else "")
        clauses.append(f"{scope}需重点留意的慢病风险包括{values}{qualification}。")
    # Missing/unrankable results stay unscored in the existing tables and
    # internal audit; the synthesis does not append a missing-data disclaimer.
    return "".join(clauses)


def _summary_clinical_context_clause(assessment):
    """Keep known acute context without describing a removed laboratory marker in section 2.9."""
    context = _dict(assessment.get("context"))
    if context.get("acuteInfection") is True or context.get("acuteInflammation") is True:
        return "已有急性感染或炎症背景，应先处理当前临床问题。"
    return ""


def _summary_injury_clause(assessment):
    """Read three accepted exact results into the supplied display combination.

    A subhealth point is an elevation for this product display, but remains
    undefined in the independent clinical pattern rules. Missing or range
    results and inconsistent score/value pairs cannot be typed.
    """
    codes = ("D_LACTATE", "DAO", "LPS")
    markers = [row for row in _items(assessment.get("markers"))
               if isinstance(row, dict) and row.get("code") in codes]
    if len(markers) != len(codes) or len({row["code"] for row in markers}) != len(codes):
        return ""
    markers = {row["code"]: row for row in markers}
    signs = []
    for code in codes:
        marker = markers[code]
        value, score = decimal_value(marker.get("value")), decimal_value(marker.get("score"))
        if (marker.get("status") != "evaluated" or marker.get("operator") != "="
                or marker.get("unit") != MARKER_UNITS[code] or value is None or score is None):
            return ""
        expected = score_marker(code, value)
        if expected["status"] != "evaluated" or decimal_value(expected["score"]) != score:
            return ""
        signs.append("+" if score > 0 else "-")
    pattern = "".join(signs)
    injury_type = _SUMMARY_INJURY_TYPES.get(pattern)
    if injury_type is None:
        return ""
    qualifier = "（指标组合分型）" if pattern == "--+" else ""
    return f"损伤类型为{injury_type}{qualifier}。"


def _chapter_summary(assessment, candidates):
    """Patient-specific synthesis: barrier -> clinical context -> risks."""
    barrier = _dict(assessment.get("barrier"))
    age = _dict(assessment.get("subject")).get("age")
    elevated, grade = barrier_abnormal_names(assessment), barrier_injury_grade(assessment)
    if type(age) not in {int, float} or age < 18 or barrier.get("score") is None:
        finding = ""
    elif barrier.get("score") == 0:
        finding = "本次屏障三项未见评分异常，IBFC为0分。"
    else:
        finding = (f"您的{'、'.join(elevated)}等肠道屏障相关指标异常，" if elevated else "")
        finding += (f"综合评估提示肠道屏障损伤等级为{grade}。" if grade
                    else f"IBFC为{barrier['score']:g}分。")
        finding += _summary_injury_clause(assessment)
    clinical_context = _summary_clinical_context_clause(assessment)
    mechanism = ("肠道屏障受损后，内毒素等物质可能进入血液循环，促进全身性慢性低度炎症和氧化应激，并与相关慢病风险增加相联系。"
                 if elevated else "肠道屏障受损时，内毒素等物质可能进入血液循环，促进全身性慢性低度炎症和氧化应激，并与相关慢病风险增加相联系。")
    risks = _risk_summary(candidates, assessment)
    conclusion = (f"结合{_summary_sources(assessment)}，{risks}" if risks else "")
    conclusion += "修复肠道屏障功能，恢复肠道菌群稳态。"
    # Preserve the two-paragraph display without inventing a patient finding
    # or a replacement disclaimer when no specific finding can be stated.
    return "\n\n".join([finding + clinical_context, mechanism + conclusion]
                        if finding or clinical_context else [mechanism, conclusion])


def apply_template_risk_review(output, assessment):
    """Compose current chapter results before freezing, preserving wire fields."""
    assessment = _dict(assessment)
    current_rules = _current_rule_assessment(assessment)
    rule_results = confirmed_rule_results(assessment)
    sections = _items(output.get("sections"))
    chapter = next((s for s in sections if s.get("id") == "disease_risks" and s.get("visible") is not False), None)
    appendix = next((s for s in sections if s.get("id") == "appendix"), None)
    if chapter is None or appendix is None:
        return
    reference_block = next((b for b in appendix.get("blocks", []) if b.get("kind") == "references"), None)
    if reference_block is None:
        reference_block = {"kind": "references", "title": PRIMARY_TITLE, "items": []}
        appendix.setdefault("blocks", []).append(reference_block)
    references = reference_block.setdefault("items", [])
    old_blocks = _items(chapter.get("blocks"))
    old_tables = [b for b in old_blocks if b.get("kind") == "table" and b.get("columns") == RISK_COLUMNS]
    old_cells = {cells[0]: cells for table in old_tables for row in _items(table.get("rows"))
                 if len(cells := _cells(row)) == 4 and isinstance(cells[0], str)}
    catalog, _ = build_disease_risks()
    studies = {entry["code"]: entry["studies"] for entry in mechanism_evidence()}
    selected = {row["code"]: row for row in _items(assessment.get("diseaseRisks"))
                if isinstance(row, dict) and isinstance(row.get("code"), str)}
    groups, summary_candidates = {}, []

    def add(row, *, template=False):
        code = row["code"]
        evaluated = selected.get(code, {})
        name = _DISPLAY_NAMES.get(code, row.get("name")) if template else row.get("name")
        system = row.get("systemCode") or "other"
        system_name = row.get("systemName") or "其他系统"
        score, level = _level(evaluated, assessment, code, rule_results)
        previous = old_cells.get(evaluated.get("name"), [])
        mechanism = previous[3] if len(previous) == 4 and isinstance(previous[3], str) else None
        preferred = preferred_mechanism(code) if template else None
        citations = _selected_citations(
            evaluated, assessment, references,
            allowed_ids=preferred["additionalEvidenceIds"] if preferred else None,
        )
        if preferred:
            numbers = [_append_reference(references, {key: study.get(key) for key in REFERENCE_FIELDS})
                       for study in preferred["sources"]]
            mechanism = preferred["text"] + "".join(f"[{number}]" for number in dict.fromkeys(numbers)) + citations
        elif evaluated and not mechanism and citations and evaluated.get("mechanism"):
            mechanism = evaluated["mechanism"] + citations
        elif mechanism and citations and not _CITATION.search(mechanism):
            mechanism += citations
        if not preferred and (not evaluated or not mechanism):
            if template:
                numbers = [_append_reference(references, {"citation": study["title"], **{
                    key: study.get(key) for key in ("url", "year", "publisher", "doi")}}) for study in studies[code]]
                mechanism = (_MECHANISMS.get(code) or row["mechanism"]) + "".join(
                    f"[{number}]" for number in dict.fromkeys(numbers))
            else:
                mechanism = mechanism or None
        cells = [name, score, level, mechanism]
        group = groups.setdefault(system, {"name": system_name, "rows": []})
        group["rows"].append(cells)
        if code in rule_results or (not current_rules and _model_result(evaluated, assessment)):
            group.setdefault("scoredRows", []).append(cells)
        if template and score is not None:
            family = "model" if not current_rules and _model_result(evaluated, assessment) else "rule"
            summary_candidates.append((family, cells))

    for row in catalog:
        add(row, template=True)
    if not current_rules:
        # Legacy documents may include qualitative directions. Current-rule
        # reports publish only the fixed catalog; follow-up facts stay in the
        # assessment and must not become extra, ungraded disease-table rows.
        catalog_codes = {row["code"] for row in catalog}
        for code, row in selected.items():
            if code not in catalog_codes:
                add(row)
        known_names = (
            {row.get("name") for row in catalog}
            | {row.get("name") for row in selected.values()}
            | set(_DISPLAY_NAMES.values())
        )
        for table in old_tables:
            title = re.sub(r"^2\.\d+\s+", "", table.get("title") or "")
            system_name = re.sub(r"(?:疾病)?风险评估$", "", title) or "其他系统"
            system = next((key for key, group in groups.items() if group["name"] == system_name), "other")
            for source_row in _items(table.get("rows")):
                cells = _cells(source_row)
                if len(cells) != 4 or cells[0] in known_names:
                    continue
                retained = [cells[0], None, None, deepcopy(cells[3])]
                groups.setdefault(system, {"name": system_name, "rows": []})["rows"].append(retained)
                known_names.add(cells[0])
    # The overview consumes the SAME audited values as the complete tables.
    overview = [[group["name"], *cells[:3]] for group in groups.values() for cells in group.get("scoredRows", [])]
    revised = [{"kind": "paragraph", "title": None, "text": _INTRODUCTION},
               {"kind": "table", "title": RISK_OVERVIEW_TITLE,
                "columns": RISK_OVERVIEW_COLUMNS[:], "rows": overview}]
    for index, group in enumerate(groups.values(), 1):
        label = group["name"] + ("风险评估" if group["name"] == "肿瘤" else "疾病风险评估")
        revised.append({"kind": "table", "title": f"2.{index} {label}",
                        "columns": RISK_COLUMNS[:], "rows": group["rows"]})
    for block in old_blocks:
        title = block.get("title") or ""
        if re.fullmatch(r"(?:2\.\d+\s+)?本章小结", title):
            continue
        elif (block in old_tables or title in {ATTENTION_OVERVIEW_TITLE, RISK_OVERVIEW_TITLE, "本次关注指数", "风险指数说明", STATE_FLOOR_NOTICE_TITLE, _BASIS_TITLE, _IMAGING_TITLE, _DATA_NOTICE_TITLE}
              or (current_rules and block.get("kind") == "table"
                  and any(column in _items(block.get("columns")) for column in ("风险指数", "风险等级")))
              or (current_rules and title == "独立模型说明")
              or (block.get("kind") == "paragraph" and title == "本次疾病评估")
              or (block.get("kind") == "paragraph" and not title
                  and isinstance(block.get("text"), str) and block["text"] in _GENERATED_INTRODUCTIONS)):
            continue
        else:
            preserved = deepcopy(block)
            if block.get("kind") == "callout" and title == "独立模型说明":
                preserved["text"] = "以下模型结果沿用各自适用范围、时间窗及标尺，不直接横向比较。"
            revised.append(preserved)
    chapter["blocks"] = revised + [{"kind": "paragraph", "title": f"2.{len(groups) + 1} 本章小结",
                                    "text": _chapter_summary(assessment, summary_candidates)}]
