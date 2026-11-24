"""instruction 的综合评估（info.body_mass）与健康风险提示（info.risk_warning）。

两段都以原报告已有结论为蓝本：
  evaluate  ：diagnosis_summary.conclusion / clinical_context["report.section.microbiome_analysis"]
  gutBarrier：第一章「1.1 综合评估指数」/ 第二章「2.x 本章小结」
"""

import json
from unittest.mock import AsyncMock

import pytest

from modules.report import reports

GUT_OVERALL = ("综合本次检测的全部信息，您的肠道屏障功能评分为IBFC 2 分，本次以D-乳酸升高为主要变化，"
               "伴随hs-CRP中度升高，当前可针对可干预因素开展健康管理，并结合症状及复查结果观察变化。")
GUT_OVERALL_CURRENT = GUT_OVERALL.replace("伴随hs-CRP中度升高，", "")
GUT_SUMMARY = ("本次以D-乳酸升高为主要变化，DAO、LPS未同步升高，并伴hs-CRP升高。"
               "本章按8个系统逐项复核。本次试行规则评分结果为低风险22项、中风险4项、高风险0项。")

GUT_SUMMARY_CURRENT = GUT_SUMMARY.replace("，并伴hs-CRP升高", "")

def gut_report(*, overall_block=None, summary_block=None, summary_title="2.9 本章小结"):
    overview = [{"kind": "facts", "title": "受检者信息", "items": [{"label": "性别", "value": "男"}]}]
    if overall_block is not False:
        overview.append(overall_block or {"kind": "paragraph", "title": "1.1 综合评估指数（IBFC · 肠屏障功能评分）",
                                          "text": GUT_OVERALL})
    risks = [{"kind": "table", "title": "2.1 消化系统", "columns": ["疾病", "等级"], "rows": [["胃炎", "关注"]]}]
    if summary_block is not False:
        risks.append(summary_block or {"kind": "paragraph", "title": summary_title, "text": GUT_SUMMARY})
    return {"type": "gutBarrier", "report": {"sections": [
        {"id": "overview", "title": "第一章 肠道屏障检测结果综述", "blocks": overview},
        {"id": "disease_risks", "title": "第二章 相关疾病风险评估", "blocks": risks},
    ]}}


# ---- 参考原文提取 ----

@pytest.mark.parametrize("title", ["2.9 本章小结", "2.12 本章小结", "本章小结"])
def test_gut_barrier_sources_follow_report_sections(title):
    followup_input = reports._gut_barrier_followup_input(gut_report(summary_title=title))
    assert reports._instruction_summary_sources(followup_input) == {
        "body_mass": GUT_OVERALL_CURRENT, "risk_warning": GUT_SUMMARY_CURRENT,
    }


def test_gut_barrier_sources_accept_older_block_shapes():
    data = gut_report(
        overall_block={"kind": "table", "title": "1.1 检测综合评估结论", "columns": ["评价项目", "综合结论"],
                       "rows": [["肠道屏障功能", "屏障三项未见异常"], ["综合评价", "本次以炎症指标升高为主要检测发现。"]]},
        summary_block={"kind": "list", "title": "本章小结", "items": ["代谢系统关注方向：高脂血症。", "消化系统关注方向：既往胃炎随访"]},
    )
    sources = reports._instruction_summary_sources(reports._gut_barrier_followup_input(data))
    assert sources["body_mass"] == "屏障三项未见异常"
    assert sources["risk_warning"] == "代谢系统关注方向：高脂血症。消化系统关注方向：既往胃炎随访"


def test_gut_barrier_sources_empty_when_blocks_missing():
    data = gut_report(overall_block=False, summary_block=False)
    assert reports._instruction_summary_sources(reports._gut_barrier_followup_input(data)) == {
        "body_mass": "", "risk_warning": "",
    }


def test_evaluate_sources_come_from_diagnosis_summary():
    data = {"type": "evaluate", "diagnosis_summary": {
        "conclusion": "本次健康资料提示，当前管理重点为代谢系统。",
        "clinical_context": {"report.section.microbiome_analysis": "菌群评估提示2型糖尿病风险。"},
    }}
    assert reports._instruction_summary_sources(data) == {
        "body_mass": "本次健康资料提示，当前管理重点为代谢系统。", "risk_warning": "菌群评估提示2型糖尿病风险。",
    }
    assert reports._instruction_summary_sources({}) == {"body_mass": "", "risk_warning": ""}


# ---- _info：参考原文进提示词；模型没给内容时退回原文 ----

@pytest.mark.asyncio
async def test_info_prompt_carries_sources(monkeypatch):
    model = AsyncMock(return_value=json.dumps({"body_mass": "综合", "risk_warning": "风险"}, ensure_ascii=False))
    monkeypatch.setattr(reports, "safe_complete_async", model)
    await reports._info(reports._gut_barrier_followup_input(gut_report()))
    prompt = model.call_args.kwargs["prompt"]
    assert f"【综合评估参考】{GUT_OVERALL_CURRENT}" in prompt
    assert f"【健康风险参考】{GUT_SUMMARY_CURRENT}" in prompt


@pytest.mark.asyncio
async def test_info_prompt_marks_missing_sources_and_has_fallback_pattern(monkeypatch):
    model = AsyncMock(return_value="{}")
    monkeypatch.setattr(reports, "safe_complete_async", model)
    await reports._info({"type": "evaluate", "diagnosis_summary": {"conclusion": ""}})
    prompt = model.call_args.kwargs["prompt"]
    assert "【综合评估参考】（无）" in prompt and "【健康风险参考】（无）" in prompt
    assert "程序会在你写的内容后面按固定句式接上机制说明" in prompt


@pytest.mark.asyncio
async def test_info_falls_back_to_source_text_when_model_returns_nothing(monkeypatch):
    monkeypatch.setattr(reports, "safe_complete_async", AsyncMock(return_value="not json"))
    monkeypatch.setattr(reports, "_build_scope_note", lambda _data: "")
    data = {"type": "evaluate", "diagnosis_summary": {
        "conclusion": "当前管理重点为代谢系统。肠道菌群结构存在需要持续管理的变化。第三句。如出现不适，应及时到相应专科进一步评估。",
        "clinical_context": {"report.section.microbiome_analysis": "菌群评估提示2型糖尿病风险。"},
    }}
    info = await reports._info(data)
    assert info["body_mass"] == "当前管理重点为代谢系统。肠道菌群结构存在需要持续管理的变化。"
    assert info["risk_warning"] == "菌群评估提示2型糖尿病风险。"


# ---- 后处理 ----

@pytest.mark.parametrize("text,expected", [
    ("hs-CRP>3.0 mg/L（参考＜0.5 mg/L），超出功能医学标准范围；", "hs-CRP>3.0 mg/L，超出功能医学标准范围。"),
    ("您的肠道屏障功能评分为IBFC 2 分。", "您的肠道屏障功能评分为IBFC 2 分。"),  # gutBarrier 的评分保留
    (None, None),
    ("", ""),
])
def test_polish_body_mass(text, expected):
    assert reports._polish_body_mass(text) == expected


def test_lifestyle_filler_dropped_when_there_are_findings(monkeypatch):
    monkeypatch.setattr(reports, "_build_scope_note", lambda _data: "")
    findings = "存在代谢系统风险，其中高脂血症需重点关注。"
    assert reports._polish_risk_warning(findings + "维持膳食均衡、规律作息与适度运动。", {}) == findings


def test_lifestyle_only_text_is_kept(monkeypatch):
    monkeypatch.setattr(reports, "_build_scope_note", lambda _data: "")
    text = "维持膳食结构均衡、规律作息与适度运动，注重肠道微生态的长期稳态维护。"
    assert reports._polish_risk_warning(text, {}) == text


# ---- 个体体质特征（info.constitution）----

def gut_constitution_report(*, grade="II 级（轻度）", score=3.0, crp_evaluation="中度升高", risk_rows=None,
                            markers=("升高", "正常", "正常"), summary=None, overall=None):
    """markers = (D-乳酸, DAO, LPS) 在 1.2 表里的结果评价。"""
    rows = risk_rows if risk_rows is not None else [
        ["肠易激综合征", 3.8, "中", ""], ["炎症性肠病", 4.3, "中", ""], ["肝硬化", 2.2, "低", ""],
        ["消化道占位性病变复核", 5.0, "中", ""], ["冠心病", 7.0, "高", ""],
    ]
    lac, dao, lps = markers
    overview = [
        {"kind": "cards", "title": "肠道屏障功能评分（IBFC）", "items": [
            {"label": "IBFC评分", "value": score}, {"label": "损伤等级", "value": grade}]},
        {"kind": "table", "title": "1.2 检测项目与结果总览",
         "columns": ["检测项目", "检测指标", "检测结果", "参考区间", "结果评价", "反映维度"],
         "rows": [["肠道屏障功能", "二胺氧化酶（DAO）", "1", "＜8.5 U/L", dao, ""],
                  ["肠道屏障功能", "细菌脂多糖（LPS）", "1", "＜18 U/L", lps, ""],
                  ["肠道屏障功能", "D-乳酸（D-Lactate）", "1", "＜14 mg/L", lac, ""],
                  ["炎症指标", "超敏C反应蛋白（hs-CRP）", "2.35 mg/L", "＜0.5 mg/L", crp_evaluation, "炎症"]]},
    ]
    if overall:
        overview.insert(0, {"kind": "paragraph", "title": "1.1 综合评估指数（IBFC · 肠屏障功能评分）", "text": overall})
    risks = [{"kind": "table", "title": "2.1 消化系统疾病风险评估", "columns": ["疾病", "风险指数", "风险等级", "机制关联"],
              "rows": rows}]
    if summary:
        risks.append({"kind": "paragraph", "title": "2.10 本章小结", "text": summary})
    return {"type": "gutBarrier", "report": {"sections": [
        {"id": "overview", "title": "第一章 肠道屏障检测结果综述", "blocks": overview},
        {"id": "disease_risks", "title": "第二章 相关疾病风险评估", "blocks": risks},
    ]}}


def constitution(data):
    return reports._instruction_constitution(reports._gut_barrier_followup_input(data))


def test_constitution_follows_product_sentence():
    data = gut_constitution_report(score=2.5, markers=("升高", "升高", "升高"))
    assert constitution(data) == (
        "本次肠道屏障评分IBFC为2.5分，损伤类型：应激+炎症(最重)，损伤分级为II级。"
        "结合您的评估问卷结果，存在冠心病、炎症性肠病、肠易激综合征慢病风险。"
    )


def test_constitution_without_inflammation():
    data = gut_constitution_report(score=1.0, grade="", crp_evaluation="理想范围", markers=("正常", "升高", "正常"))
    assert constitution(data) == (
        "本次肠道屏障评分IBFC为1分，损伤类型：单纯应激性损伤，损伤分级为I级。"
        "结合您的评估问卷结果，存在冠心病、炎症性肠病、肠易激综合征慢病风险。"
    )


def test_constitution_diseases_from_summary_text_when_no_risk_table():
    data = gut_constitution_report(score=1.0, grade="", crp_evaluation="理想范围", markers=("正常", "升高", "正常"),
                                   risk_rows=[],
                                   summary="需重点留意的慢病风险包括炎症性肠病（4.8分，中风险）、抑郁症（1.7分，低风险）。")
    sections = data["report"]["sections"]
    sections[1]["blocks"] = [b for b in sections[1]["blocks"] if b["kind"] != "table"]
    assert constitution(data).endswith("结合您的评估问卷结果，存在炎症性肠病慢病风险。")


@pytest.mark.parametrize("markers,damage_type", [
    (("升高", "升高", "升高"), "应激+炎症(最重)"),
    (("升高", "正常", "升高"), "炎症性损伤(伴移位)"),
    (("正常", "升高", "升高"), "应激性损伤(伴移位)"),
    (("正常", "正常", "升高"), "非肠源性移位"),
    (("升高", "正常", "正常"), "单纯炎症性损伤"),
    (("正常", "升高", "正常"), "单纯应激性损伤"),
    (("升高", "升高", "正常"), "炎症+应激(无移位)"),
    (("正常", "正常", "正常"), ""),
])
def test_damage_type_rule_table(markers, damage_type):
    data = gut_constitution_report(markers=markers)
    assert reports._gb_damage_type([], data["report"]["sections"]) == damage_type


def test_damage_type_reads_markers_from_chapter_summary_text_first():
    # 表里三项都正常，但本章小结写了 D-乳酸升高、DAO/LPS 未同步升高 -> 以文字为准
    data = gut_constitution_report(markers=("正常", "正常", "正常"),
                                   summary="本次以D-乳酸升高为主要变化，DAO、LPS未同步升高，并伴hs-CRP升高。")
    assert "单纯炎症性损伤" in constitution(data)


def test_damage_type_uses_label_stated_in_report():
    summary = "您的DAO等肠道屏障相关指标异常，综合评估提示肠道屏障损伤等级为I 级。损伤类型为单纯应激性损伤。"
    data = gut_constitution_report(grade="", score=1.0, markers=("升高", "升高", "正常"), summary=summary,
                                   crp_evaluation="理想范围", risk_rows=[["肝硬化", 2.2, "低", ""]])
    assert constitution(data) == ("本次肠道屏障评分IBFC为1分，损伤类型：单纯应激性损伤，损伤分级为I级。"
                                  "结合您的评估问卷结果，存在肝硬化慢病风险。")  # 没有中高风险时取最高的低风险


def test_grade_read_from_report_text_when_card_is_empty():
    overall = "您当前的肠道屏障评分 IBFC 为 2 分，伴有DAO异常升高，损伤分级为 I 级，建议围绕肠道屏障修复开展管理。"
    data = gut_constitution_report(grade="", score=2.0, markers=("正常", "升高", "正常"), overall=overall,
                                   crp_evaluation="理想范围")
    # 卡片没给等级时取报告文字里的 I 级，而不是按分值区间推出的 II 级
    assert "损伤分级为I级" in constitution(data)


def test_constitution_normal_barrier():
    data = gut_constitution_report(grade="未见评分异常", score=0, crp_evaluation="正常", markers=("正常",) * 3,
                                   risk_rows=[])
    # 报告里一个疾病风险都没有：不写慢病句，也不写「暂未见」
    assert constitution(data) == "本次肠道屏障评分IBFC为0分，肠道屏障未见损伤。"


def test_real_callback_sample():
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[3] / "api_test" / "callback.json"
    if not path.is_file():
        pytest.skip("no local callback sample")
    text = constitution(json.loads(path.read_text(encoding="utf-8")))
    assert text.startswith("本次肠道屏障评分IBFC为")


def evaluate_report(*, score=65, available=True, level="需要关注", diseases=None):
    diseases = diseases if diseases is not None else [
        [("便秘", "低风险"), ("2型糖尿病", "中风险")],
        [("炎症性肠病", "高风险"), ("2型糖尿病", "高风险"), ("两肾细小结石", "风险待评估")],
    ]
    return {"type": "evaluate", "healthAssessment": {
        "overallScore": score, "displayOverallScore": str(score) if score is not None else "",
        "overallScoreAvailable": available, "levelLabel": level,
        "dimensions": [{"name": f"维度{i}", "relatedDiseases": [{"name": n, "riskLabel": r} for n, r in rows]}
                       for i, rows in enumerate(diseases)],
    }}


def test_evaluate_constitution_uses_assessment_and_unbalance():
    info = {"unbalance_type": "二度失衡（局限微生态失衡）"}
    assert reports._instruction_constitution(evaluate_report(), info) == (
        "您的健康管理评估指数为65分，整体处于「需要关注」状态，肠道菌群为二度失衡。"
        "结合您的评估问卷结果，存在炎症性肠病、2型糖尿病慢病风险。"
    )


def test_evaluate_constitution_without_score_microbiome_or_mid_high_risk():
    data = evaluate_report(score=None, available=False, diseases=[[("便秘", "低风险")]])
    assert reports._instruction_constitution(data, {"unbalance_type": ""}) == (
        "您的健康管理评估整体处于「需要关注」状态。结合您的评估问卷结果，存在便秘慢病风险。"
    )
    assert reports._instruction_constitution({}, None) == ""


@pytest.mark.asyncio
async def test_evaluate_info_carries_constitution(monkeypatch):
    response = json.dumps({"unbalance_type": "一度失衡（潜伏型微生态失衡）"}, ensure_ascii=False)
    monkeypatch.setattr(reports, "safe_complete_async", AsyncMock(return_value=response))
    info = await reports._info(evaluate_report())
    assert info["constitution"].startswith("您的健康管理评估指数为65分，整体处于「需要关注」状态，肠道菌群为一度失衡。")


@pytest.mark.asyncio
async def test_info_carries_constitution(monkeypatch):
    monkeypatch.setattr(reports, "safe_complete_async", AsyncMock(return_value="{}"))
    info = await reports._info(reports._gut_barrier_followup_input(gut_constitution_report()))
    assert info["constitution"].startswith("本次肠道屏障评分IBFC为3分，损伤类型：单纯炎症性损伤，损伤分级为II级")


@pytest.mark.parametrize("score,expected", [
    ("0", "normal"), ("0.5", "I"), ("1", "I"), ("1.5", "I"), ("2", "I"), ("4", "II"), ("4.5", "II"),
    ("6", "III"), ("8", "IV"), ("12", "V"), ("", ""),
])
def test_gut_grade_from_score(score, expected):
    assert reports._gb_grade_from_score(score) == expected


# ---- 健康风险提示的固定结尾（机制说明 + 最严重的 2–3 个慢病）----

TAIL_BARRIER = "肠道屏障受损，导致内毒素入血，可能引起全身性慢性低度炎症，会引发各类慢病的产生。"


def gut_risk_tail(data):
    return reports._instruction_risk_tail(reports._gut_barrier_followup_input(data))


def test_gut_risk_tail_barrier_impaired_takes_top_three():
    rows = [["A病", 3.5, "中", ""], ["B病", 4.0, "中", ""], ["C病", 7.0, "高", ""], ["D病", 3.4, "中", ""]]
    assert gut_risk_tail(gut_constitution_report(risk_rows=rows)) == TAIL_BARRIER + "其中C病、B病、A病慢病风险较高。"


def test_gut_risk_tail_inflammation_only_and_no_mid_high_risk():
    data = gut_constitution_report(grade="未见评分异常", score=0, risk_rows=[["肝硬化", 2.2, "低", ""]])
    assert gut_risk_tail(data) == "肝硬化慢病风险较高。"


def test_gut_risk_tail_without_mechanism():
    data = gut_constitution_report(grade="未见评分异常", score=0, crp_evaluation="正常")
    assert gut_risk_tail(data) == "冠心病、炎症性肠病、肠易激综合征慢病风险较高。"


def test_evaluate_risk_tail_uses_unbalance_and_assessment():
    info = {"unbalance_type": "一度失衡"}
    assert reports._instruction_risk_tail(evaluate_report(), info) == (
        "肠道菌群失衡，可能引起全身性慢性低度炎症，会引发各类慢病的产生。其中炎症性肠病、2型糖尿病慢病风险较高。"
    )
    assert reports._instruction_risk_tail(evaluate_report(), {}) == "炎症性肠病、2型糖尿病慢病风险较高。"


@pytest.mark.asyncio
async def test_info_risk_warning_is_model_text_plus_tail(monkeypatch):
    response = json.dumps({"risk_warning": "本次以D-乳酸升高为主要变化，并伴hs-CRP升高"}, ensure_ascii=False)
    monkeypatch.setattr(reports, "safe_complete_async", AsyncMock(return_value=response))
    info = await reports._info(reports._gut_barrier_followup_input(gut_constitution_report()))
    assert info["risk_warning"] == (
        "本次以D-乳酸指标异常为主要变化。" + TAIL_BARRIER
        + "其中冠心病、炎症性肠病、肠易激综合征慢病风险较高。"
    )


@pytest.mark.asyncio
async def test_info_risk_warning_drops_lifestyle_filler_when_tail_exists(monkeypatch):
    response = json.dumps({"risk_warning": "维持膳食均衡、规律作息与适度运动。"}, ensure_ascii=False)
    monkeypatch.setattr(reports, "safe_complete_async", AsyncMock(return_value=response))
    monkeypatch.setattr(reports, "_build_scope_note", lambda _data: "")
    info = await reports._info(evaluate_report())
    assert info["risk_warning"] == "炎症性肠病、2型糖尿病慢病风险较高。"


def test_low_risk_only_still_lists_top_two():
    rows = [["肝硬化", 2.2, "低", ""], ["抑郁症", 3.1, "低", ""], ["焦虑症", 1.0, "低", ""]]
    data = gut_constitution_report(risk_rows=rows)
    assert constitution(data).endswith("结合您的评估问卷结果，存在抑郁症、肝硬化慢病风险。")
    assert "暂未见" not in constitution(data)


def test_evaluate_low_risk_only_prefers_worst_dimension():
    data = evaluate_report(diseases=[[("便秘", "低风险")], [("2型糖尿病", "低风险"), ("肥胖", "低风险")]])
    data["healthAssessment"]["dimensions"][0]["score"] = 80
    data["healthAssessment"]["dimensions"][1]["score"] = 40
    assert reports._instruction_constitution(data, {}).endswith("存在2型糖尿病、肥胖慢病风险。")


# ---- 指标异常统一写成「××指标异常」----

@pytest.mark.parametrize("text,expected", [
    ("伴二胺氧化酶（DAO）异常升高，损伤分级为 I 级", "伴二胺氧化酶（DAO）指标异常，损伤分级为 I 级"),
    ("并伴hs-CRP中度升高。", "并伴hs-CRP指标异常。"),
    ("hs-CRP 显著升高", "hs-CRP指标异常"),
    ("D-乳酸 20.88 mg/L（轻度升高）", "D-乳酸 20.88 mg/L（指标异常）"),
    ("hs-CRP 2.35 mg/L，中度升高", "hs-CRP 2.35 mg/L，指标异常"),
    ("炎症相关指标升高", "炎症相关指标异常"),
    ("LDL-C 3.70 mmol/L偏高", "LDL-C 3.70 mmol/L指标异常"),
    # 否定说法、非指标的升高不动
    ("DAO、LPS未同步升高", "DAO、LPS未同步升高"),
    ("LPS未见升高", "LPS未见升高"),
    ("相关风险升高", "相关风险升高"),
    ("本次超敏C反应蛋白为范围型结果，具体数值待定量确认。", "本次超敏C反应蛋白为范围型结果，具体数值待定量确认。"),
])
def test_normalize_abnormal_wording(text, expected):
    assert reports._normalize_abnormal_wording(text) == expected


def test_body_mass_and_risk_warning_use_unified_wording(monkeypatch):
    monkeypatch.setattr(reports, "_build_scope_note", lambda _data: "")
    body = "肠道屏障评分 IBFC 为 1 分，伴二胺氧化酶（DAO）异常升高，损伤分级为 I 级，建议围绕肠道屏障修复开展管理。"
    assert reports._polish_body_mass(body) == (
        "肠道屏障评分 IBFC 为 1 分，伴二胺氧化酶（DAO）指标异常，损伤分级为 I 级，建议围绕肠道屏障修复开展管理。")
    risk = "本次以D-乳酸升高为主要变化，DAO、LPS未同步升高，并伴hs-CRP中度升高。"
    assert reports._polish_risk_warning(risk, {}) == "本次以D-乳酸指标异常为主要变化，DAO、LPS未同步升高，并伴hs-CRP指标异常。"


def test_info_prompt_asks_for_unified_abnormal_wording():
    prompt = reports._render_followup_prompt("info", evaluate_json="{}", body_mass_source="（无）",
                                             risk_warning_source="（无）")
    assert prompt.count("指标异常一律写成“××指标异常”") == 2
