"""Five chapter builders. Presentation only; never compute clinical scores."""
from __future__ import annotations

from copy import deepcopy

from . import content_catalog as content
from .rules import GRADE_BANDS
from .review_release import OVERVIEW_GUIDANCE
from .presentation import (
    SCHEMA_VERSION, _age_scope, _clinical_rows, _dict, _items,
    _measurement_value, _paragraph, _pattern,
    _public_microbiome_sources, _section, _status, _table, _text,
)

_GRADES = {"normal": "未见评分异常", "I": "I 级（亚健康）", "II": "II 级（轻度）",
           "III": "III 级（中度）", "IV": "IV 级（重度）", "V": "V 级（极重度）"}
_MARKERS = (("D_LACTATE", "D-乳酸（D-Lactate）", "＜14 mg/L"),
            ("DAO", "二胺氧化酶（DAO）", "＜8.5 U/L"),
            ("LPS", "细菌脂多糖（LPS）", "＜18 U/L"))
_MARKER_SHORT_NAMES = {"D_LACTATE": "D-乳酸", "DAO": "DAO", "LPS": "LPS"}
_MARKER_MEANINGS = {
    "D_LACTATE": "D-乳酸主要与细菌糖代谢有关。血中水平受肠腔生成、跨屏障吸收及机体清除共同影响；升高可作为肠腔代谢与通透性变化的间接线索，与DAO、LPS联合观察更有助于区分相关环节。",
    "DAO": "二胺氧化酶（DAO）在小肠绒毛上皮中较为丰富。上皮受损时，细胞内DAO释放可影响血中活性，因此本项用于辅助观察黏膜上皮完整性；它并非内镜或组织病理损伤程度的直接测量。",
    "LPS": "细菌脂多糖（LPS）是革兰阴性菌外膜的组成成分，也称内毒素。血中信号升高与内毒素暴露相关；LPS可经免疫受体识别启动炎症反应，是连接屏障状态与全身炎症的一条机制线索。",
}
_PATTERN_MEANINGS = {
    "+--": "本次以D-乳酸指标异常为特征，呈现肠腔代谢产物相关异常。通透性改变、细菌代谢负荷及机体清除均可影响这一结果；DAO、LPS未同步升高。后续重点是核对消化吸收与饮食耐受，观察症状和D-乳酸的变化。",
    "-+-": "本次以DAO指标异常为特征，提示黏膜上皮完整性相关指标异常；D-乳酸、LPS未同步升高。上皮更新需要充足的能量与营养原料，管理重点是识别黏膜刺激因素、纠正已确认的营养不足并观察DAO变化。",
    "--+": "本次以LPS指标异常为特征，呈现内毒素暴露相关异常；D-乳酸、DAO未同步升高。内毒素可通过免疫识别参与炎症反应，需结合感染背景及检测条件判断其临床意义，不能仅由该组合定位暴露来源。",
    "++-": "本次D-乳酸与DAO指标异常，分别涉及代谢产物跨屏障吸收和黏膜上皮完整性相关环节，LPS未同步升高。修复方向同时关注消化耐受与上皮营养，随访两项变化以观察相关功能是否改善。",
    "+-+": "本次D-乳酸与LPS指标异常，呈现代谢产物与内毒素信号并存的变化，DAO未同步升高。通透性改变可能增加机体接触肠腔细菌产物的机会；后续应结合症状评估饮食、菌群代谢及其他影响因素。",
    "-++": "本次DAO与LPS指标异常，提示黏膜上皮完整性与内毒素暴露相关指标同时异常，D-乳酸未同步升高。上皮修复条件与炎症负担需共同关注，结合营养状态、临床背景和复测结果确定管理重点。",
    "+++": "本次D-乳酸、DAO与LPS指标异常，呈现混合型变化：D-乳酸关联肠腔代谢产物与通透性，DAO关联黏膜上皮完整性，LPS关联内毒素暴露。三个环节共同异常时，需兼顾屏障保护、上皮更新和炎症负担；以诱因处理、可耐受的营养支持及菌群生态管理为修复方向，并用症状和连续检测评价变化。",
    "---": "本次屏障三项未见评分异常，以日常养护与症状随访为主；已有疾病或持续症状仍按其临床需求评估。",
}


def _chapter(identifier, blocks):
    return _section(identifier, dict(content.CHAPTERS)[identifier], blocks)


def _readout(item):
    value = item.get("displayValue")
    if item.get("operator") == "=" and isinstance(value, str):
        return value.removeprefix("=")
    return _measurement_value(item)


def _barrier_text(barrier, *, include_unknown_pattern=True):
    score, grade = barrier.get("score"), barrier.get("grade")
    if score is None:
        return "三项屏障检测资料尚未齐备或结果需核对，暂不能评分及确定综合等级。"
    if grade == "normal":
        return "本次三项屏障相关指标未见异常，肠道屏障功能评分（IBFC）为 0 分。"
    grade_text = _GRADES.get(grade, "分级待复核")
    pattern = barrier.get("type") or ("受损类型待核对" if include_unknown_pattern else None)
    return f"肠道屏障功能评分（IBFC）为 {_text(score)} 分，{grade_text}" + (f"，{pattern}。" if pattern else "。")








def _overview(assessed, context, report_id, plans):
    subject, barrier = map(_dict, (assessed.get("subject"), assessed.get("barrier")))
    review = _dict(assessed.get("reportReview"))
    sex = {"male": "男", "female": "女", "M": "男", "F": "女"}.get(subject.get("sex"), subject.get("sex"))
    facts = [("姓名", subject.get("name")), ("性别", sex),
             ("年龄", f"{subject['age']} 岁" if subject.get("age") is not None else None),
             ("样本类型", context.get("sampleType")), ("检测项目", "肠道屏障三项"),
             ("检测日期", context.get("sampleDate")), ("报告日期", context.get("currentDate"))]
    by_code = {m.get("code"): m for m in _items(assessed.get("markers")) if isinstance(m, dict)}
    rows = []
    for code, name, ref in _MARKERS:
        marker = by_code.get(code, {})
        measured = _readout(marker)
        result = f"{measured} {_text(marker.get('unit'))}".strip() if measured else None
        level = marker.get("level")
        evaluation = level or _status(marker.get("status") or "missing")
        if (marker.get("status") == "evaluated" and marker.get("operator") == "="
                and type(marker.get("score")) in {int, float} and marker["score"] > 0):
            evaluation = f"{_MARKER_SHORT_NAMES[code]}指标异常"
        reference = marker.get("projectReferenceRange") or ref
        rows.append([name, result, reference, evaluation, marker.get("score")])
    barrier_text = _barrier_text(barrier, include_unknown_pattern=not bool(review))
    normal = barrier.get("grade") == "normal"
    guidance = OVERVIEW_GUIDANCE["normal" if normal else "abnormal"]
    if _age_scope(subject) != "adult":
        guidance = OVERVIEW_GUIDANCE["age_specific"]
    signals = {s.get("code") for s in _items(context.get("hospitalSafetySignals")) if isinstance(s, dict)}
    for codes, text in (
        ({"fiber_hold"}, "已有消化道通行相关记录，先核对饮食形式，不按常规目标增加膳食纤维。"),
        ({"kidney_problem", "liver_problem"}, "已有肝肾功能相关记录，营养补充与医疗营养方案需先完成专科安全评估。"),
        ({"immune_compromised", "severe_immune_compromise"}, "已有免疫状态相关记录，活菌及菌群移植措施需先完成专科适用性评估。"),
        ({"activity_hold"}, "已有活动安全相关记录，不套用常规运动目标，应先完成临床评估。"),
        ({"acute_infection", "acute_inflammation"}, "先处理当前急性感染或炎症相关情况，稳定后再复评屏障指标。"),
    ):
        if signals & codes:
            guidance += text
    if not signals & {"acute_infection", "acute_inflammation"} and (
        context.get("acuteInfection") is True or context.get("acuteInflammation") is True
    ):
        guidance += "已提供急性感染或炎症背景，应先完成临床处理，稳定后再复评。"
    microbiome = _dict(plans.get("microbiome"))
    safety_plans = [*_items(plans.get("nutrition")), *_items(plans.get("lifestyle")),
                    _dict(plans.get("barrierCare")), _dict(microbiome.get("mdf")), _dict(microbiome.get("pfmt"))]
    restrictions = []
    for plan in safety_plans:
        if not isinstance(plan, dict):
            continue
        status = plan.get("status")
        if isinstance(status, str) and (status.startswith("safety_hold") or status in {
            "interaction_review_required", "medical_clearance_required",
            "suppressed_due_to_allergy", "safety_review_liver_kidney",
        }):
            reason = plan.get("reason")
            if isinstance(reason, str) and reason not in restrictions:
                restrictions.append(reason)
    from .review_release import combined_text
    conclusions = [["肠道屏障功能", barrier_text],
                   ["综合评价", combined_text(assessed)], ["干预方向", guidance]]
    blocks = [dict(kind="facts", title="受检者信息",
                   items=[dict(label=label, value=value) for label, value in facts]),
              _table(["评价项目", "综合结论"], conclusions, "1.1 检测综合评估结论"),
              *([dict(kind="callout", title="个体干预限制", tone="caution", text="\n".join(restrictions))] if restrictions else []),
              _paragraph("本次从屏障相关代谢、黏膜上皮状态及内毒素信号三个方面进行评价，各项结果相互补充。", "1.2 检测指标分项阐述"),
              _table(["检测指标", "检测结果", "参考范围", "结果评价", "单项评分"], rows, "三项检测指标"),
              dict(kind="list", title="指标意义", items=[_MARKER_MEANINGS[code] for code, _, _ in _MARKERS]),
              _paragraph(barrier_text, "1.3 肠道屏障功能评分（IBFC）与损伤分级"),
              dict(kind="cards", title="肠道屏障功能评分（IBFC）", items=[
                  dict(label="IBFC评分", value=barrier.get("score"), detail="满分15分"),
                  dict(label="损伤等级", value=_GRADES.get(barrier.get("grade")), detail="分级待复核" if barrier.get("score") is not None and not barrier.get("grade") else None),
                  dict(label="受损类型", value=barrier.get("type"), detail=None)]),
              dict(kind="bands", title="屏障评分分级", items=[
                  dict(range=label, label=_GRADES[grade], selected=barrier.get("grade") == grade)
                  for grade, label in GRADE_BANDS]),
              _paragraph((f"本次受损类型：{barrier['type']}。" if barrier.get("type") else "") +
                         _PATTERN_MEANINGS.get(_pattern(barrier.get("pattern")), "当前三项组合尚未确定，结合已获得的单项结果进一步完善分型评价。"),
                         "1.4 肠道屏障受损类型与受损状况")]
    if review:
        stage = _dict(assessed.get("overallStage"))
        for block in blocks:
            if block.get("kind") == "cards" and block.get("title") == "肠道屏障功能评分（IBFC）":
                block["items"].append(dict(label="总体阶段", value=stage.get("label"), detail=None))
        if stage.get("status") == "evaluated" and stage.get("label"):
            conclusions.insert(0, ["总体阶段", stage["label"]])
    return _chapter("overview", blocks), {"title": "检测结果综述", "text": barrier_text,
                                                  "findings": [barrier_text]}


def _education():
    return _chapter("education", [_paragraph(text, title) for title, text in content.EDUCATION])


def _attention_summary(priorities):
    """Summarize selected directions without repeating entire table cells.

    Only supported rows selected by the caller enter this summary. Qualitative
    follow-up directions do not acquire a score merely by being summarized.
    There is no default disease list or inference from the presence of a system.
    """
    groups = {}
    for row in priorities:
        system = row.get("systemName")
        name = row.get("name")
        if isinstance(system, str) and isinstance(name, str):
            names = groups.setdefault(system, [])
            if name not in names:
                names.append(name)
    result = [f"{system}关注方向：{'、'.join(names)}。" for system, names in groups.items()]
    # Explain an actual overlap in patient evidence, not two independent events.
    lipid_rows = [row for row in priorities if any(
        isinstance(fact, dict) and fact.get("domain") == "lipids"
        for fact in _items(_dict(row.get("attention")).get("facts")))]
    if len({row.get("systemCode") for row in lipid_rows}) > 1:
        result.append("血脂与心血管关注共用本次提供的血脂异常资料，宜合并随访，不视为两项独立诊断。")
    return result


def consolidated_disease_summary(assessed, *, systems=()):
    """Summarize adopted facts without re-evaluating a source or listing diseases.

    This is used only by the new publication layout. Historical source findings
    come from selected qualitative rows, never from a second pass over the raw
    hospital ledger. Dates remain historical and do not become current causes.
    """
    assessed = _dict(assessed)
    rows = [row for row in _items(assessed.get("diseaseRisks")) if isinstance(row, dict)
            and row.get("status") in {"attention_index", "qualitative_followup", "evaluated"}]
    current = [row for row in rows if row.get("status") in {"attention_index", "evaluated"}]
    current_systems = {row.get("systemCode") for row in current}
    domains = {fact.get("domain") for row in current
               for fact in _items(_dict(row.get("attention")).get("facts")) if isinstance(fact, dict)}
    markers = {row.get("code"): row for row in _items(assessed.get("markers"))
               if isinstance(row, dict) and row.get("status") == "evaluated"
               and row.get("operator") in {None, "="} and not row.get("reasons")}
    names = {"D_LACTATE": "D-乳酸", "DAO": "DAO", "LPS": "LPS"}
    elevated = [name for code, name in names.items() if markers.get(code, {}).get("abnormal") is True]
    normal = [name for code, name in names.items() if markers.get(code, {}).get("abnormal") is False]
    observations = []
    if elevated:
        observations.append(f"本次以{'、'.join(elevated)}升高为主要变化")
        if normal:
            observations.append(f"{'、'.join(normal)}未同步升高")
    elif len(normal) == 3:
        observations.append("本次屏障三项未见评分异常")
    elif normal:
        observations.append(f"本次已确认的{'、'.join(normal)}未见评分异常，其余屏障资料仍需完善")
    sentences = ["，".join(observations) + "。"] if observations else []

    if current:
        related = []
        if "metabolic" in current_systems:
            related.append("糖脂代谢")
        if "cardiovascular" in current_systems:
            related.append("心血管相关因素")
        if "intestinal_barrier" in domains and elevated:
            focus = "综合评估关注屏障功能与消化道表现的联系"
            if related:
                focus += f"，并结合{'及'.join(related)}确定复核重点"
            sentences.append(focus + "。")
        else:
            selected_systems = list(dict.fromkeys(row.get("systemName") for row in current
                                                  if isinstance(row.get("systemName"), str)))
            if selected_systems:
                sentences.append(f"综合评估重点涉及{'、'.join(selected_systems)}的相关因素。")

    historical_dates, historical_systems = set(), []
    for row in rows:
        if row.get("status") != "qualitative_followup":
            continue
        historical = [finding for finding in _items(_dict(row.get("qualitative")).get("sourceFindings"))
                      if isinstance(finding, dict) and finding.get("temporalStatus") == "historical"
                      and isinstance(finding.get("date"), str)]
        if historical:
            historical_dates.update(finding["date"] for finding in historical)
            system = row.get("systemName")
            if isinstance(system, str) and system not in historical_systems:
                historical_systems.append(system)
    if historical_dates:
        scope = "、".join(historical_systems) or "专项"
        sentences.append(f"既往{scope}检查（{'、'.join(sorted(historical_dates))}）需核对后续处理和复查记录。")

    urine_reviews = [finding for row in rows if row.get("status") == "qualitative_followup"
                     and row.get("systemCode") == "urinary"
                     for finding in _items(_dict(row.get("qualitative")).get("recordReviews"))
                     if isinstance(finding, dict) and isinstance(finding.get("date"), str)
                     and finding.get("date")
                     and "urine_record_requires_confirmation" in _items(finding.get("reasonCodes"))]
    for temporal_status, label in (("historical", "既往"), ("current", "本次")):
        reviews = [finding for finding in urine_reviews if finding.get("temporalStatus") == temporal_status]
        if not reviews:
            continue
        reasons = {reason for finding in reviews for reason in _items(finding.get("reasonCodes"))}
        issues = [text for reason, text in (
            ("missing_unit", "部分检验单位缺失"),
            ("result_status_conflict", "结果与状态标记不一致"),
            ("source_result_conflict", "来源记录存在冲突"),
        ) if reason in reasons]
        detail = "、".join(issues) or "结果仍待复核"
        dates = "、".join(sorted({finding["date"] for finding in reviews}))
        sentences.append(f"{label}尿检（{dates}）{detail}，宜核对原报告及后续复查。")

    if elevated:
        sentences.append("后续结合消化道症状、饮食耐受及病程，复核屏障相关检测和必要的专项检查。")
    elif rows:
        sentences.append("后续结合当前症状、已有记录及近期专项检查，确定复查与随访安排。")
    elif systems:
        sentences.append(f"本次评估涉及{'、'.join(dict.fromkeys(systems))}，后续结合实际症状、既往病史及专项检查核对当前情况。")
    else:
        sentences.append("现有资料尚不足以形成具体疾病关注方向，后续先完善症状、病史及必要的检查资料。")
    return "".join(sentences)


def _diseases(assessed, mechanism_citations=None):
    from .dynamic_diseases import MODE, NOTICE
    if _dict(assessed.get("diseaseAssessment")).get("mode") == MODE:
        rows = _items(assessed.get("diseaseRisks"))
        blocks = [dict(kind="callout", title="本次关注指数", tone="info", text=NOTICE)]
        groups = {}
        for row in rows:
            groups.setdefault(row["systemCode"], []).append(row)
        order = [key for key in content.DISEASE_SECTION_ORDER if key in groups]
        order.extend(key for key in groups if key not in order)
        for index, system in enumerate(order, 1):
            items = groups[system]
            cells = []
            for row in items:
                finding = row.get("qualitative") or {}
                references = "".join(f"[评估{key[1:]}]" for key in finding.get("evidenceIds", []))
                mechanism = (row.get("mechanism") or "") + references
                if row.get("status") == "evaluated":
                    mechanism = "本项数值来自独立的已配置模型，不与本章自定义关注指数比较。" + mechanism
                cells.append([row["name"], row.get("riskIndex"), row.get("riskLevel"), mechanism])
            name = items[0]["systemName"]
            title = f"2.{index} {name}疾病风险评估" if name != "肿瘤" else f"2.{index} 肿瘤风险评估"
            blocks.append(_table(["疾病", "风险指数", "风险等级", "机制关联"], cells, title))
        if not rows:
            missing = [name for code, name, _ in _MARKERS
                       if not any(marker.get("code") == code and marker.get("status") == "evaluated"
                                  for marker in _items(assessed.get("markers")))]
            detail = (f"本次尚缺少可直接用于完整分层的{'、'.join(missing)}结果。" if missing else
                      "本次三项检测可用于屏障状态评价，但尚无充分个体资料支持具体病种分层。")
            blocks.append(_paragraph("现有资料未形成可由相应证据支持的具体疾病关注方向。" + detail +
                                     "结合症状、病史及常规检查完善后再评估；未列出疾病不表示已排除疾病。", "本次疾病评估"))
            blocks.append(dict(kind="list", title="本章小结", items=[
                "本次未给出具体疾病风险分数，不将资料不足解释为0分或低风险。",
                "随访应结合实际症状和既往疾病，补充适用的专项资料。",
            ]))
        else:
            # Registered model values use different scales and must not enter
            # the same attention-card ranking, even when their value is zero.
            priorities = [row for row in rows if row.get("status") == "attention_index"
                          and _dict(row.get("attention")).get("facts")
                          and isinstance(_dict(row.get("attention")).get("score"), (int, float))
                          and not isinstance(_dict(row.get("attention")).get("score"), bool)
                          and _dict(row.get("qualitative")).get("evidenceIds")]
            priorities.sort(key=lambda row: (-row["attention"]["score"], str(row.get("code") or "")))
            qualitative = [row for row in rows if row.get("status") == "qualitative_followup"
                           and _dict(row.get("qualitative")).get("evidenceIds")]
            blocks.append(dict(kind="list", title="本章小结", items=_attention_summary(priorities + qualitative)))
        clinical = _clinical_rows(_items(_dict(assessed.get("context")).get("clinicalFindings")
                                        or _dict(assessed.get("subject")).get("clinicalFindings")))
        from .clinical_attention import imaging_attention_findings, imaging_attention_block, imaging_attention_summary
        imaging = imaging_attention_findings(assessed)
        if imaging:
            summary_index = next(index for index, block in enumerate(blocks) if block.get("title") == "本章小结")
            blocks[summary_index]["items"].append(imaging_attention_summary(imaging))
            blocks.insert(summary_index, imaging_attention_block(imaging))
        if clinical:
            blocks.append(_table(["检查日期", "项目", "时间属性", "原始检查摘要"], clinical, "个体临床背景"))
        return _chapter("disease_risks", blocks)
    from .risk_model import build_disease_risks
    rows = assessed.get("diseaseRisks")
    if not isinstance(rows, list) or len(rows) != 22:
        rows, _ = build_disease_risks()
    groups = {}
    for row in rows:
        groups.setdefault(row["systemCode"], []).append(row)
    blocks = [_paragraph("以下按系统列示相关疾病与机制关联。风险指数需完整模型及相应个体资料支持；未评估不表示低风险，也不代表已患该病。")]
    qualitative = isinstance(assessed.get("diseaseAssessment"), dict)
    if qualitative:
        supported = [row for row in rows if row["systemCode"] in content.DISEASE_SECTION_ORDER
                     and (row.get("qualitative") or {}).get("evidenceIds")]
        names = "、".join(row["name"] for row in supported)
        blocks.append(_paragraph(
            (f"本次可结合已有资料重点评估的方向包括：{names}。具体相关因素、症状鉴别与复核依据见下表；这些关注方向不代表已确诊相应疾病。" if supported else
             "本次可用资料尚不足以支持具体病种的个体风险分层。下表保留各病种所需的专项资料，后续根据实际症状与病史决定是否补充。"),
            "本次疾病评估"))
    mechanism_citations = mechanism_citations or {}
    for index, system in enumerate(content.DISEASE_SECTION_ORDER, 1):
        items = groups[system]
        name = items[0]["systemName"]
        title = f"2.{index} {name}疾病风险评估" if name != "肿瘤" else f"2.{index} 肿瘤风险评估"
        blocks.append(_table(["疾病", "风险指数", "风险等级", "机制关联"], [
            [row["name"], row.get("riskIndex"), row.get("riskLevel") or "未评估",
             (row.get("mechanism") or "") + mechanism_citations.get(row["code"], "")]
            for row in items], title))
        if qualitative:
            details = []
            for row in items:
                finding = row.get("qualitative") or {}
                citation = "".join(f"[评估{key[1:]}]" for key in finding.get("evidenceIds", []))
                details.append(f"{row['name']}：{finding.get('basis', '')}{finding.get('recommendation', '')}{citation}")
            blocks.append(dict(kind="list", title=f"{name} · 本次评估依据", items=details))
    clinical = _clinical_rows(_items(_dict(assessed.get("context")).get("clinicalFindings")
                                     or _dict(assessed.get("subject")).get("clinicalFindings")))
    if clinical:
        blocks.append(_table(["检查日期", "项目", "时间属性", "原始检查摘要"], clinical, "个体临床背景"))
    attention = []
    for risk in _items(assessed.get("risks")):
        if not isinstance(risk, dict):
            continue
        if _age_scope(_dict(assessed.get("subject"))) != "adult" and risk.get("code") in {"cardiovascular", "metabolic"}:
            continue
        attention.append("；".join(filter(None, [_text(risk.get("name")), _text(risk.get("basis")), _text(risk.get("recommendations"))])))
    if qualitative:
        attention = [f"{row['name']}：{row['qualitative']['basis']}{row['qualitative']['recommendation']}" for row in rows
                     if row.get("qualitative", {}).get("evidenceIds") and row["systemCode"] in content.DISEASE_SECTION_ORDER]
    blocks.append(dict(kind="list", title="本章小结", items=attention or [
        "本次疾病风险数值未完成评估。后续结合症状、病史及常规危险因素确定需要重点关注的方向。"
    ]))
    return _chapter("disease_risks", blocks)


def _repairs():
    # This chapter is deliberately identical across patients. Individual
    # findings and safety qualifications belong to chapter one, not this copy.
    blocks = [
        dict(kind="list", title="3.1 肠道屏障修复 5 原则总览", items=list(content.PRINCIPLES)),
        _paragraph("五原则形成从诱因处理、营养支持到菌群重建与长期维护的连续路径。各环节围绕屏障稳态相互配合，结合第一章的个体评估与适用条件选择实施。"),
        *[_paragraph(text, title) for title, text in content.REPAIR_SECTIONS],
    ]
    return _chapter("repair_plan", blocks)


def _supplemental_evidence(plans):
    """Keep source numbering and resolve mechanism citations from one list.

    Only the server-owned evidence catalog supplies these references. Raw
    request material and model traces never become public bibliography entries.
    """
    from .risk_model import build_disease_risks, mechanism_evidence

    evidence = content.supplemental_references()
    for reference in _items(plans.get("references")):
        if not isinstance(reference, dict) or reference.get("type") not in {
            "official_fact_sheet", "professional_guideline", "regulatory_safety_alert",
            "official_guideline", "professional_public_guidance", "official_public_guidance",
            "scientific_statement", "official_product_information", "expert_consensus",
        }:
            continue
        evidence.append(dict(citation=reference.get("title"), url=reference.get("url"),
                             year=reference.get("year"), publisher=reference.get("source"),
                             doi=reference.get("doi"), location=reference.get("location")))
    primary = {item["url"]: index for index, item in enumerate(content.references(), 1) if item.get("url")}
    supplemental = {item["url"]: index for index, item in enumerate(evidence, 1) if item.get("url")}
    displayed_codes = {row["code"] for row in build_disease_risks()[0]
                       if row["systemCode"] in content.DISEASE_SECTION_ORDER}
    citations = {}
    for item in mechanism_evidence():
        if item["code"] not in displayed_codes:
            continue
        main_numbers, extra_numbers = [], []
        for study in item["studies"]:
            url = study["url"]
            if url in primary:
                main_numbers.append(primary[url])
                continue
            if url not in supplemental:
                evidence.append(dict(
                    citation=study.get("title") or "机制研究：" + study["scope"], url=url,
                    year=study.get("year"), publisher=study.get("publisher"), doi=study.get("doi"),
                    location=study.get("location") or study["population"],
                ))
                supplemental[url] = len(evidence)
            extra_numbers.append(supplemental[url])
        citations[item["code"]] = "".join(
            "[" + prefix + ",".join(str(index) for index in dict.fromkeys(numbers)) + "]"
            for prefix, numbers in (("", main_numbers), ("补", extra_numbers)) if numbers
        )
    return evidence, citations


def _appendix(plans, supplemental=None):
    # Fixed quality/method descriptions, never fabricated individual lab passes.
    blocks = [_table(["质量控制环节", "流程与要求"], [list(row) for row in content.QUALITY_CONTROL], "5.1 质量控制校验"),
              _paragraph(content.QUALITY_CONTROL_NOTE)]
    blocks.extend([_table(["检测指标", "方法学介绍", "评估用途"], [list(row) for row in content.METHODS], "5.2 检测方法学介绍"),
                   _paragraph("通常流程为样本采集、样本处理、上机检测、结果审核和报告出具；本次方法及质控记录以检测机构实际信息为准。"),
                   _paragraph(content.RISK_SCALE_TEXT, "5.3 风险指数标尺说明"),
                   dict(kind="list", title="5.4 免责声明", items=list(content.DISCLAIMER)),
                   dict(kind="references", title="5.5 参考文献", items=content.references())])
    # Preserve the supplied 14 numbered groups, while making the actual
    # intervention safety/management sources publicly traceable as well.
    evidence = supplemental if supplemental is not None else _supplemental_evidence(plans)[0]
    if evidence:
        blocks.append(dict(kind="references", title="营养与微生态干预补充依据", items=evidence))
    return _chapter("appendix", blocks)


def _auxiliary_source_block(sources):
    if sources:
        # Source-model indices do not belong in the new 22-disease risk table.
        rows = []
        for source in sources:
            enterotype = _dict(source.get("enterotype"))
            rows.append([source.get("sampleDate"), "菌群资料", _text(enterotype.get("raw") or enterotype.get("value"), "已纳入来源核对")])
        if rows:
            return _table(["采样日期", "资料类型", "原报告肠型 / 摘要"], rows, "辅助资料摘要")
    return None


def compose_report(assessment, interventions, *, report_id, source_summary=None):
    if not isinstance(assessment, dict) or not isinstance(interventions, dict):
        raise TypeError("assessment and interventions must be dictionaries")
    if not isinstance(report_id, str) or not report_id.strip():
        raise ValueError("report_id must be a non-empty string")
    assessed, plans = deepcopy(assessment), deepcopy(interventions)
    subject = _dict(assessed.get("subject"))
    context = {**_dict(subject.get("context")), **_dict(assessed.get("context"))}
    sources = _public_microbiome_sources(_items(context.get("microbiomeSources") or subject.get("microbiomeSources")))
    for owner in (assessed, subject, _dict(assessed.get("context")), _dict(subject.get("context"))):
        owner.pop("microbiomeSources", None)
    context.pop("microbiomeSources", None)
    overview, summary = _overview(assessed, context, report_id, plans)
    auxiliary = _auxiliary_source_block(sources)
    if auxiliary:
        overview["blocks"].append(auxiliary)
    supplemental, mechanism_citations = _supplemental_evidence(plans)
    supplemental.extend(deepcopy(_dict(assessed.get("diseaseAssessment")).get("references", [])))
    sections = [overview, _diseases(assessed, mechanism_citations), _repairs(), _education(),
                _appendix(plans, supplemental)]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "meta": {"reportId": report_id, "title": content.REPORT_TITLE, "mode": "clinical",
                 "ruleVersion": assessed.get("ruleVersion"), "contentVersion": content.CONTENT_VERSION,
                 "templateVersion": content.TEMPLATE_VERSION, "contentSource": deepcopy(content.REPORT_SOURCE)},
        "subject": subject, "assessment": assessed, "interventions": plans,
        "summary": summary, "sections": sections, "references": content.references(),
        "narrativeCandidates": [], "interpretation": [],
        "sourceSummary": deepcopy(source_summary or []), "microbiomeSources": sources,
    }
