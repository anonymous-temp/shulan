"""Full template coverage is independent of whether risk can be quantified."""
from copy import deepcopy
import asyncio
import html as html_module
import json
import re

import pytest

from modules.gut_barrier.integration_content import (
    ATTENTION_OVERVIEW_TITLE, DISEASE_INTRODUCTION, PRODUCT_DISEASE_INTRODUCTION, RISK_COLUMNS,
)
from modules.gut_barrier.reference_order import REFERENCE_FIELDS, publish_ordered_references
from modules.gut_barrier.risk_model import build_disease_risks, mechanism_evidence
from modules.gut_barrier.template_risk_review import (
    RISK_OVERVIEW_COLUMNS, RISK_OVERVIEW_TITLE, STATE_FLOOR_NOTICE_TITLE, apply_template_risk_review,
)


def _section(identifier, blocks):
    return {"id": identifier, "title": identifier, "subtitle": None, "visible": True, "blocks": blocks}


def _report(*rows, blocks=()):
    return {"schemaVersion": "gut-barrier-public.v2", "meta": {"title": "合成测试", "reportId": "synthetic"},
            "sections": [_section("disease_risks", [
                {"kind": "callout", "title": "本次关注指数", "text": "旧指数说明", "tone": "default"},
                {"kind": "table", "title": ATTENTION_OVERVIEW_TITLE, "columns": [], "rows": []},
                {"kind": "table", "title": "2.1 消化系统疾病风险评估", "columns": RISK_COLUMNS[:], "rows": list(rows)},
                *deepcopy(blocks),
                {"kind": "paragraph", "title": "2.2 本章小结", "text": "已有小结应保留。"},
            ]), _section("appendix", [{"kind": "references", "title": "4.4 参考文献", "items": []}])]}


def _chapter(report):
    return next(s for s in report["sections"] if s["id"] == "disease_risks")


def _tables(report):
    return [b for b in _chapter(report)["blocks"] if b.get("columns") == RISK_COLUMNS]


def _rows(report):
    return [row for table in _tables(report) for row in table["rows"]]


def _overview(report):
    return next(b for b in _chapter(report)["blocks"] if b["title"] == RISK_OVERVIEW_TITLE)


def _references(report):
    return next(b["items"] for s in report["sections"] if s["id"] == "appendix"
                for b in s["blocks"] if b.get("kind") == "references")


def _assert_chapter_synthesis(report, systems=("消化", "代谢", "心脑血管", "神经", "精神", "免疫", "泌尿", "肿瘤")):
    summary = _chapter(report)["blocks"][-1]
    assert summary["kind"] == "paragraph"
    assert summary["title"] == f"2.{len(systems) + 1} 本章小结"
    assert set(summary) == {"kind", "title", "text"}
    assert all(term in summary["text"] for term in ("屏障", "内毒素", "血液循环", "慢性低度炎症和氧化应激", "风险", "菌群稳态"))
    assert not any(term in summary["text"] for term in (
        "复核", "核对", "已有小结应保留", "试行规则评分结果", "本次试行规则下", "上述疾病风险评估涵盖", "资料不足",
        "最低风险", "临床资料待补充", "待评估", "尚不能", "无法评定", "缺项", "尚不足以确定",
    ))
    assert summary["text"].count("\n\n") == 1
    assert all(part and "\n" not in part for part in summary["text"].split("\n\n"))
    assert summary["text"].endswith("修复肠道屏障功能，恢复肠道菌群稳态。")
    return summary


def _selected(code="obesity", *, status="attention_index"):
    base = next(row for row in build_disease_risks()[0] if row["code"] == code)
    return {**base, "status": status, "riskIndex": 9, "riskLevel": "优先评估",
            "reasons": [], "attention": {"score": 9, "validated": False, "facts": [{"domain": "body", "strength": 3}]},
            "qualitative": {"basis": "合成的已核实代谢相关因素。", "evidenceIds": ["Q34"]}}


def _assessment(*rows, model=False):
    result = {"diseaseRisks": list(rows), "diseaseAssessment": {"references": [{
        "citation": "[评估34]合成临床依据", "url": "https://example.test/accepted-guidance", "year": 2024}]}}
    if model:
        result["riskModel"] = {"status": "partial", "modelId": "reviewed-synthetic-model", "modelVersion": "v1",
                               "items": [{"code": row["code"], "status": "evaluated", "reasons": []} for row in rows]}
    return result


def test_chapter_introduction_matches_word_in_callback_and_static_template():
    from adapters.gut_barrier.frontend_template import load_static_template
    from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE
    expected = ("肠道屏障受损与慢性低度炎症是多种慢性疾病与机体衰老的共同上游机制。"
                "本章基于本次检测结果与相关健康评估，评估相关疾病风险。"
                "风险等级（低、中、高）为基于检测指标的健康风险评估推断，"
                "用于风险提示与预防参考，不构成疾病诊断。")
    report = _report()
    apply_template_risk_review(report, {})
    introduction = {"kind": "paragraph", "title": None, "text": expected}
    assert _chapter(report)["blocks"][0] == introduction
    assert load_static_template(LATEST_DISPLAY_TEMPLATE)["diseaseIntroduction"] == introduction


@pytest.mark.parametrize("old_introduction", [
    DISEASE_INTRODUCTION, PRODUCT_DISEASE_INTRODUCTION,
    "以下按系统列示相关疾病与机制关联。风险指数需完整模型及相应个体资料支持；未评估不表示低风险，也不代表已患该病。",
])
def test_only_exact_generated_introductions_are_replaced(old_introduction):
    report = _report(blocks=[{"kind": "paragraph", "title": None, "text": old_introduction}])
    apply_template_risk_review(report, {})
    paragraphs = [block for block in _chapter(report)["blocks"]
                  if block["kind"] == "paragraph" and block["title"] is None]
    assert len(paragraphs) == 1
    assert paragraphs[0]["text"] != old_introduction
    before = deepcopy(report)
    apply_template_risk_review(report, {})
    assert report == before


@pytest.mark.parametrize("text", [
    "既往检查记录显示胃黏膜异常，需结合检查日期观察变化。",
    "出现便血、持续发热或进行性腹痛时，应及时就医。",
    PRODUCT_DISEASE_INTRODUCTION + "本例伴持续腹痛，应及时就医。",
    DISEASE_INTRODUCTION + "本例既往检查异常需保留随访。",
])
def test_anonymous_clinical_and_safety_paragraphs_are_retained_without_prefix_matching(text):
    retained = {"kind": "paragraph", "title": None, "text": text}
    report = _report(blocks=[retained])
    apply_template_risk_review(report, {})
    assert _chapter(report)["blocks"].count(retained) == 1
    before = deepcopy(report)
    apply_template_risk_review(report, {})
    assert report == before
    assert _chapter(report)["blocks"].count(retained) == 1


def test_stale_disease_assessment_paragraph_is_removed_without_hiding_current_results_or_safety():
    obsolete = {"kind": "paragraph", "title": "本次疾病评估", "text": "旧版缺项提示。"}
    explanations = [{"kind": "callout", "title": title, "text": "待移除的旧风险计算说明。", "tone": "default"}
                    for title in ("风险指数说明", STATE_FLOOR_NOTICE_TITLE, "风险评估资料提示")]
    safety = {"kind": "callout", "title": "便血就医提示", "text": "出现便血应及时就医。", "tone": "caution"}
    other = {"kind": "paragraph", "title": "其他疾病评估", "text": "其他已核实临床信息仍保留。"}
    report = _report(blocks=[obsolete, *explanations, safety, other])
    apply_template_risk_review(report, {})
    blocks = _chapter(report)["blocks"]
    assert not any(block.get("title") == "本次疾病评估" for block in blocks)
    assert all(block not in blocks for block in explanations)
    assert safety in blocks and other in blocks
    assert any(block.get("title") == RISK_OVERVIEW_TITLE for block in blocks)
    assert not any(block.get("title") == "风险评估资料提示" for block in blocks)
    assert len(_rows(report)) == 23
    assert all(row[1:3] == [None, None] for row in _rows(report))
    frozen = deepcopy(report)
    apply_template_risk_review(report, {})
    assert report == frozen


def test_subhealth_panel_uses_v27_quota_without_old_state_floor_in_callback_or_html():
    from adapters.gut_barrier.rendering import render_report_html
    from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE
    from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment
    from modules.gut_barrier.risk_index_rules import apply_risk_index_rules
    assessed = apply_risk_index_rules(apply_dynamic_assessment(
        _clinical_assessment((8.29, 9.95, 17.44, "<0.5"))))
    before = deepcopy(assessed)
    callback = _publish(assessed)
    assert not any(block.get("title") == "本次疾病评估" for block in _chapter(callback)["blocks"])
    assert len(_overview(callback)["rows"]) == 23
    assert assessed["riskRuleAssessment"]["allocation"]["highCodes"] == ["ibs", "type2_diabetes"]
    assert assessed["riskRuleAssessment"]["allocation"]["mediumCodes"] == ["cirrhosis", "nafld", "intestinal_barrier_followup"]
    assert [row[1:] for row in _overview(callback)["rows"] if row[3] == "高"] == [
        ["肠易激综合征", 6.8, "高"], ["2型糖尿病", 6.7, "高"],
    ]
    assert [row[1:] for row in _overview(callback)["rows"] if row[3] == "中"] == [
        ["肠道炎症与功能紊乱", 4.0, "中"], ["代谢相关脂肪性肝病", 4.1, "中"], ["肝硬化", 4.2, "中"],
    ]
    assert sum(row[3] == "低" for row in _overview(callback)["rows"]) == 18
    ibd = next(row for row in assessed["riskRuleAssessment"]["items"] if row["code"] == "ibd")
    assert next(row for row in _rows(callback) if row[0] == "炎症性肠病")[1:3] == [ibd["riskIndex"], ibd["riskLevel"]]
    assert ibd["weights"] == [4]
    public_text = json.dumps(callback, ensure_ascii=False)
    assert all(text not in public_text for text in (
        STATE_FLOOR_NOTICE_TITLE, "风险指数说明", "炎症性肠病基础分1.8分", "依据屏障状态下限调整",
    ))
    assert not any(block.get("title") == "风险评估资料提示" for block in _chapter(callback)["blocks"])
    assert assessed == before
    rendered = render_report_html(callback, template_version=LATEST_DISPLAY_TEMPLATE)
    assert "本次疾病评估" not in rendered
    assert all(text not in rendered for text in (
        STATE_FLOOR_NOTICE_TITLE, "风险指数说明", "风险评估结果说明", "炎症性肠病基础分1.8分",
    ))
    assert "本次疾病风险评估总览" in rendered
    assert json.loads(re.search(r'id="report-data">(.*?)</script>', rendered, re.S)[1]) == callback


def test_removing_stale_current_paragraph_does_not_rewrite_frozen_callback():
    from modules.gut_barrier.publication import project_callback_report
    frozen = _report(blocks=[{"kind": "paragraph", "title": "本次疾病评估", "text": "已冻结的历史原文。"}])
    before = deepcopy(frozen)
    assert project_callback_report(frozen) == before
    assert frozen == before


def test_complete_23_catalog_order_and_eight_systems_without_inventing_low_risk():
    report = _report()
    apply_template_risk_review(report, {})
    assert len(_tables(report)) == 8
    assert [b["title"] for b in _tables(report)] == [
        "2.1 消化系统疾病风险评估", "2.2 代谢系统疾病风险评估", "2.3 心脑血管系统疾病风险评估",
        "2.4 神经系统疾病风险评估", "2.5 精神系统疾病风险评估", "2.6 免疫系统疾病风险评估",
        "2.7 泌尿系统疾病风险评估", "2.8 肿瘤风险评估",
    ]
    catalog, _ = build_disease_risks()
    assert [row[0] for row in _rows(report)] == [
        "代谢相关脂肪性肝病" if row["code"] == "nafld" else row["name"] for row in catalog]
    assert len(_rows(report)) == 23
    assert all(row[1] is None and row[2] is None for row in _rows(report))
    chart = next(b for b in _chapter(report)["blocks"] if b["title"] == RISK_OVERVIEW_TITLE)
    assert chart["columns"] == RISK_OVERVIEW_COLUMNS
    assert chart["rows"] == []
    _assert_chapter_synthesis(report)
    assert not any(b["title"] == "风险评估资料提示" for b in _chapter(report)["blocks"])
    assert "不预设" not in _chapter(report)["blocks"][-1]["text"]
    assert "高、中、低风险的数量" not in _chapter(report)["blocks"][-1]["text"]
    assert all(b["title"] not in {ATTENTION_OVERVIEW_TITLE, "本次关注指数"} for b in _chapter(report)["blocks"])


def test_attention_facts_survive_separately_but_score_never_becomes_disease_risk():
    chosen = _selected()
    report = _report([chosen["name"], 9, "优先评估", "脂肪组织变化与代谢异常相关。"])
    assessment = _assessment(chosen)
    before = deepcopy(assessment)
    apply_template_risk_review(report, assessment)
    obesity = next(row for row in _rows(report) if row[0] == "肥胖")
    assert obesity[1:3] == [None, None]
    assert next(row for row in _rows(report) if row[0] == "2型糖尿病")[2] is None
    assert not any(b.get("title") == "本次相关因素与复核依据" for b in _chapter(report)["blocks"])
    assert assessment["diseaseRisks"][0]["qualitative"]["basis"] == "合成的已核实代谢相关因素。"
    _assert_chapter_synthesis(report)
    assert all(row[1] is None for row in _rows(report))
    assert _overview(report)["rows"] == []
    assert assessment == before


def test_new_review_removes_only_extra_basis_block_and_replaces_review_summary():
    basis = {"kind": "paragraph", "title": "本次相关因素与复核依据", "text": "仅内部使用的合成依据。"}
    safety = {"kind": "callout", "title": "便血就医提示", "tone": "caution", "text": "出现便血应及时就医。"}
    clinical = {"kind": "paragraph", "title": None, "text": "患者既往检查资料原文仍应保留。"}
    imaging = {"kind": "callout", "title": "影像检查待核实事项", "tone": "caution", "text": "合成历史影像原文。"}
    report = _report(blocks=[basis, safety, clinical, imaging])
    _chapter(report)["blocks"][-1]["text"] = "既往2025年检查仍需复核，单位尚待核对。低风险22项。"
    assessed = _assessment(_selected())
    before = deepcopy(assessed)
    apply_template_risk_review(report, assessed)
    blocks = _chapter(report)["blocks"]
    assert not any(block.get("title") in {"本次相关因素与复核依据", "影像检查待核实事项"} for block in blocks)
    assert safety in blocks and clinical in blocks
    assert len(_rows(report)) == 23 and _references(report)
    _assert_chapter_synthesis(report)
    assert assessed == before
    projected = deepcopy(report)
    apply_template_risk_review(report, assessed)
    assert report == projected


@pytest.mark.parametrize("score,level,display_level", [
    (0, "low", "低"), (5, "medium", "中"), (9, "high", "高"),
    (1, "低风险", "低"), (4, "中风险", "中"), (8, "高风险", "高"),
    (2, "低", "低"), (6, "中", "中"), (7, "高", "高"), (0, None, None),
    (9, "TEST_LOW", None), (1, "HIGH", None), (4, " low ", None),
    (9, "low", "低"),  # Preserve model semantics, not a score-based regrading.
])
def test_confirmed_model_value_and_semantics_survive_display_names_including_zero(score, level, display_level):
    chosen = {**_selected(status="evaluated"), "riskIndex": score, "riskLevel": level}
    report = _report([chosen["name"], score, level, "脂肪组织与代谢相关。"], blocks=[
        {"kind": "callout", "title": "独立模型说明", "tone": "default", "text": "不纳入旧图"}])
    assessment = _assessment(chosen, model=True)
    before = deepcopy(assessment)
    apply_template_risk_review(report, assessment)
    assert next(row for row in _rows(report) if row[0] == "肥胖")[1:3] == [score, display_level]
    assert _overview(report)["rows"] == [["代谢系统", "肥胖", score, display_level]]
    assert assessment == before
    assert not any(b.get("title") == "本次相关因素与复核依据" for b in _chapter(report)["blocks"])
    if level is not None and display_level is None:
        assert not any(b["title"] == "风险评估资料提示" for b in _chapter(report)["blocks"])
        assert assessment["diseaseRisks"][0]["riskLevel"] == level
    _assert_chapter_synthesis(report)
    model_notice = next(b for b in _chapter(report)["blocks"] if b["title"] == "独立模型说明")
    assert model_notice["text"] == "以下模型结果沿用各自适用范围、时间窗及标尺，不直接横向比较。"


@pytest.mark.parametrize("change", ["missing_audit", "failed_item", "failed_audit", "nan", "boolean", "row_reason"])
def test_unverified_model_like_values_cannot_leak_into_risk_display(change):
    chosen = {**_selected(status="evaluated"), "riskIndex": 8, "riskLevel": "high"}
    assessment = _assessment(chosen, model=True)
    if change == "missing_audit":
        assessment.pop("riskModel")
    elif change == "failed_item":
        assessment["riskModel"]["items"][0]["status"] = "failed"
    elif change == "failed_audit":
        assessment["riskModel"]["status"] = "failed"
    elif change == "nan":
        chosen["riskIndex"] = float("nan")
    elif change == "boolean":
        chosen["riskIndex"] = True
    else:
        chosen["reasons"] = ["model_result_invalid"]
    report = _report()
    apply_template_risk_review(report, assessment)
    assert next(row for row in _rows(report) if row[0] == "肥胖")[1:3] == [None, None]
    assert _overview(report)["rows"] == []


def test_historical_extensions_and_actual_safety_callouts_are_preserved_not_promoted_to_cancer_or_ckd():
    extras = [{"code": code, "name": name, "systemCode": system, "systemName": system_name,
               "status": "qualitative_followup", "riskIndex": None, "riskLevel": "随访关注",
               "qualitative": {"basis": "既往合成检查有记录，后续复核情况待核对。", "evidenceIds": ["Q34"]}}
              for code, name, system, system_name in [
                  ("gastritis_followup", "既往胃炎复核", "digestive", "消化系统"),
                  ("digestive_lesion_followup", "消化道局灶性病变复核", "digestive", "消化系统"),
                  ("urinary_stone_followup", "泌尿系结石复核", "urinary", "泌尿系统")]]
    safety = {"kind": "callout", "title": "便血就医提示", "tone": "caution", "text": "实际已采纳的安全提示。"}
    report = _report(*[[row["name"], None, "随访关注", "相关组织改变可形成局部病变。"] for row in extras], blocks=[safety])
    apply_template_risk_review(report, _assessment(*extras))
    assert len(_rows(report)) == 26
    assert all(next(row for row in _rows(report) if row[0] == item["name"])[1:3] == [None, None]
               for item in extras)
    assert next(row for row in _rows(report) if row[0] == "结直肠癌")[2] is None
    assert next(row for row in _rows(report) if row[0] == "慢性肾脏病")[2] is None
    assert safety in _chapter(report)["blocks"]
    assert any(row[0] == "泌尿系结石复核" for row in _tables(report)[6]["rows"])
    assert _overview(report)["rows"] == []


def test_numeric_overview_excludes_qualitative_and_attention_rows_in_a_mixed_report():
    model = {**_selected(status="evaluated"), "riskIndex": 0, "riskLevel": "low"}
    attention = _selected("type2_diabetes")
    qualitative = {**_selected("ibs", status="qualitative_followup"), "riskIndex": None,
                   "riskLevel": "随访关注", "attention": None}
    assessment = _assessment(model, attention, qualitative, model=True)
    assessment["riskModel"]["items"] = [{"code": "obesity", "status": "evaluated", "reasons": []}]
    report = _report()
    apply_template_risk_review(report, assessment)
    assert _overview(report)["rows"] == [["代谢系统", "肥胖", 0, "低"]]
    assert len(_rows(report)) == 23
    assert next(row for row in _rows(report) if row[0] == "2型糖尿病")[1:3] == [None, None]
    assert next(row for row in _rows(report) if row[0] == "肠易激综合征")[1:3] == [None, None]


def test_catalog_mechanisms_are_single_scoped_sentences_with_actual_reviewed_references():
    report = _report()
    apply_template_risk_review(report, {})
    refs = _references(report)
    studies = {entry["code"]: entry for entry in mechanism_evidence()}
    preferred_dois = {
        "ibs": {"10.1038/nrdp.2016.14"},
        "ibd": {"10.1159/000447252", "10.1053/j.gastro.2017.05.056"},
        "nafld": {"10.1159/000447252"},
    }
    for cells, catalog_row in zip(_rows(report), build_disease_risks()[0]):
        entry = studies[catalog_row["code"]]
        text = cells[3]
        assert text.count("。") == 1
        assert not any(term in text for term in ("本次", "提示", "关注", "诊断", "建议", "就医"))
        numbers = [int(number) for number in re.findall(r"\[(\d+)\]", text)]
        assert numbers
        if entry["code"] in preferred_dois:
            assert {refs[n - 1]["doi"] for n in numbers} == preferred_dois[entry["code"]]
        else:
            assert {refs[n - 1]["url"] for n in numbers} == {study["url"] for study in entry["studies"]}
    assert "小鼠" in next(row[3] for row in _rows(report) if row[0] == "帕金森病")
    assert "探索性" in next(row[3] for row in _rows(report) if row[0] == "肺癌")
    assert all(set(ref) == set(REFERENCE_FIELDS) and ref["location"] is None for ref in refs)


def test_appended_references_remain_valid_through_first_citation_ordering_and_repeat_is_idempotent():
    report = _report()
    apply_template_risk_review(report, {})
    before = deepcopy(report)
    apply_template_risk_review(report, {})
    assert report == before
    asset = {"sectionOrder": ["disease_risks", "appendix"], "fixedSections": [],
             "overviewInsertions": [], "appendixBlocks": []}
    publish_ordered_references(report, before, asset)
    numbers = [int(number) for row in _rows(report) for number in re.findall(r"\[(\d+)\]", row[3])]
    assert list(dict.fromkeys(numbers)) == list(range(1, len(_references(report)) + 1))
    assert all(1 <= number <= len(_references(report)) for number in numbers)


def test_unknown_legacy_direction_is_retained_without_untraceable_score():
    report = _report(["补充方向", 8, "high", "来源机制保留。"])
    apply_template_risk_review(report, None)
    assert len(_rows(report)) == 24
    assert next(row for row in _rows(report) if row[0] == "补充方向") == ["补充方向", None, None, "来源机制保留。"]
    assert _overview(report)["rows"] == []


def test_absent_chapter_is_not_recreated_in_a_frozen_or_unrelated_shape():
    report = {"sections": [_section("overview", [])]}
    before = deepcopy(report)
    apply_template_risk_review(report, None)
    assert report == before


def test_system_total_matches_extensions_and_replaces_previously_fixed_eight_without_duplication():
    extras = [{"code": code, "name": name, "systemCode": system, "systemName": system_name,
               "status": "qualitative_followup", "riskIndex": None, "riskLevel": "随访关注",
               "qualitative": {"basis": "合成的个体资料仍须专项复核。", "evidenceIds": ["Q34"]}}
              for code, name, system, system_name in [
                  ("synthetic_skin", "合成皮肤项目", "skin", "皮肤系统"),
                  ("synthetic_respiratory", "合成呼吸项目", "respiratory", "呼吸系统")]]
    report = _report()
    _chapter(report)["blocks"][-1]["text"] += "本章按8个系统逐项复核，未量化或资料不足的项目不能据此判为低风险。"
    assessment = _assessment(*extras)
    apply_template_risk_review(report, assessment)
    assert len(_tables(report)) == 10
    summary = _assert_chapter_synthesis(report, (
        "消化", "代谢", "心脑血管", "神经", "精神", "免疫", "泌尿", "肿瘤", "皮肤", "呼吸",
    ))
    assert "8个系统" not in summary["text"] and "10个系统" not in summary["text"]
    before = deepcopy(report)
    apply_template_risk_review(report, assessment)
    assert report == before


@pytest.mark.parametrize("version", ["gut-barrier-static.v10", "gut-barrier-static.v13"])
def test_frozen_dynamic_report_is_not_reclassified_by_the_new_publication_rule(version):
    from modules.gut_barrier.publication import project_callback_report
    frozen = _report(["历史合成项目", None, "资料不足", "冻结的机制文本。"])
    frozen["meta"]["displayTemplateVersion"] = version
    basis = {"kind": "paragraph", "title": "本次相关因素与复核依据", "text": "历史冻结的相关因素说明。"}
    imaging = {"kind": "callout", "title": "影像检查待核实事项", "tone": "caution", "text": "历史冻结影像记录。"}
    _chapter(frozen)["blocks"][-1:-1] = [basis, imaging]
    _chapter(frozen)["blocks"][-1]["text"] += "本章按8个系统逐项复核，未量化或资料不足的项目不能据此判为低风险。"
    before = deepcopy(frozen)
    sanitized = project_callback_report(frozen)
    assert _tables(sanitized)[0]["rows"] == [["历史合成项目", None, "资料不足", "冻结的机制文本。"]]
    assert _chapter(sanitized)["blocks"][-1] == _chapter(frozen)["blocks"][-1]
    assert basis in _chapter(sanitized)["blocks"] and imaging in _chapter(sanitized)["blocks"]
    assert sanitized["sections"] == before["sections"]
    assert project_callback_report(sanitized) == sanitized
    assert frozen == before


def _new_template_overview_report(version=None):
    """Build real chapter blocks from synthetic assessed data, without I/O."""
    from modules.gut_barrier.assessment import assess_case
    from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE
    from modules.gut_barrier.presentation import build_report
    from modules.gut_barrier.review_release import apply_review_release
    from modules.gut_barrier.schemas import GutBarrierRequest

    request = GutBarrierRequest.model_validate({
        "userId": "three-stage-test-user", "sessionId": "three-stage-test-session",
        "name": "合成三阶段样本", "age": 42, "sex": "男",
        "gutHealthIndicator": {
            "dLacticAcid": {"value": "20.88", "unit": "mg/L"},
            "diamineOxidase": {"value": "0.5", "unit": "U/L"},
            "bacterialEndotoxin": {"value": "2.4", "unit": "U/L"},
            "hsCRP": {"value": "2.35", "unit": "mg/L"},
        },
    })
    assessment = apply_review_release(assess_case(request, context={
        "currentDate": "2026-10-07", "microbiomeSources": [{
            "sourceId": "synthetic-auxiliary-source", "format": "json",
            "status": "parsed", "sampleDate": "2026-10-01",
            "enterotype": {"raw": "合成辅助肠型", "value": "synthetic-enterotype"},
            "metrics": [{"name": "合成保留数据", "value": 0}],
        }],
    }))
    report = build_report(assessment, {}, report_id="gbr_three_stage_test")
    report["meta"]["displayTemplateVersion"] = version or LATEST_DISPLAY_TEMPLATE
    return report


def _overview_blocks(report):
    return next(section["blocks"] for section in report["sections"] if section["id"] == "overview")


@pytest.mark.parametrize("text", [
    "既往检查记录显示胃黏膜异常，需结合检查日期观察变化。",
    "出现便血、持续发热或进行性腹痛时，应及时就医。",
    PRODUCT_DISEASE_INTRODUCTION + "本例持续腹痛，应及时就医。",
])
def test_new_current_callback_and_html_preserve_anonymous_clinical_body_and_frozen_replay(text):
    from adapters.gut_barrier.rendering import render_report_html
    from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE
    from modules.gut_barrier.publication import project_callback_report
    from tests.unit.gut_barrier.preview.support import visible_source

    internal = _new_template_overview_report()
    paragraph = {"kind": "paragraph", "title": None, "text": text}
    _chapter(internal)["blocks"].append(deepcopy(paragraph))
    before = deepcopy(internal)
    callback = project_callback_report(internal)
    assert _chapter(callback)["blocks"].count(paragraph) == 1
    assert internal == before
    frozen = deepcopy(callback)
    html = render_report_html(callback, template_version=LATEST_DISPLAY_TEMPLATE)
    assert visible_source(html).count(text) == 1
    assert json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.S)[1]) == frozen
    assert callback == frozen == project_callback_report(callback)
    assert internal == before


def test_new_current_callback_has_only_three_concept_stages_and_keeps_internal_source_audit():
    from modules.gut_barrier.publication import project_callback_report

    internal = _new_template_overview_report()
    before = deepcopy(internal)
    original_blocks = _overview_blocks(internal)
    original_bands = next(block for block in original_blocks if block["title"] == "屏障评分分级")
    assert len(original_bands["items"]) == 5
    assert any(block["title"] == "辅助资料摘要" for block in original_blocks)
    callback = project_callback_report(internal)
    blocks = _overview_blocks(callback)
    bands = next(block for block in blocks if block["title"] == "屏障评分分级")
    assert bands == {
        "kind": "bands", "title": "屏障评分分级", "items": [
            {"range": None, "label": label, "selected": False}
            for label in ("健康", "亚健康", "疾病")
        ],
    }
    assert all(set(item) == {"range", "label", "selected"} for item in bands["items"])
    assert not any(block["kind"] == "table" and block["title"] == "辅助资料摘要" for block in blocks)
    assert not any(block["title"] == "hs-CRP 项目功能分层" for block in blocks)
    cards = next(block["items"] for block in blocks if block["kind"] == "cards")
    assert next(item for item in cards if item["label"] == "损伤等级") == {
        "label": "损伤等级", "value": None, "detail": None,
    }
    assert next(item for item in cards if item["label"] == "总体阶段") == {
        "label": "总体阶段", "value": None, "detail": None,
    }
    assert next(item["value"] for item in cards if item["label"] == "IBFC评分") == 2
    texts = [block["text"] for block in blocks if block["kind"] == "paragraph"]
    assert any("IBFC 为 2 分" in text and "损伤分级为 I 级" in text for text in texts)
    assert not any("II 级" in text or "Ⅱ级" in text or "中度损伤" in text for text in texts)
    assert internal == before
    assert internal["assessment"]["barrier"]["score"] == 2
    assert internal["assessment"]["barrier"]["grade"] == "I"
    assert internal["microbiomeSources"][0]["metrics"][0]["value"] == 0
    assert project_callback_report(callback) == callback


def test_frozen_current_callback_keeps_prior_five_grade_bands_and_auxiliary_table():
    from modules.gut_barrier.publication import project_callback_report

    internal = _new_template_overview_report()
    callback = project_callback_report(internal)
    blocks = _overview_blocks(callback)
    old_bands = deepcopy(next(block for block in _overview_blocks(internal) if block["title"] == "屏障评分分级"))
    current_bands = next(block for block in blocks if block["title"] == "屏障评分分级")
    current_bands["items"] = old_bands["items"]
    blocks.append(deepcopy(next(block for block in _overview_blocks(internal) if block["title"] == "辅助资料摘要")))
    before = deepcopy(callback)
    assert project_callback_report(callback) == before == callback
    assert len(next(block for block in blocks if block["title"] == "屏障评分分级")["items"]) == 5


def test_v10_internal_projection_does_not_adopt_three_stage_or_auxiliary_removal(monkeypatch):
    from modules.gut_barrier import reference_order
    from modules.gut_barrier.display_versions import ORDERED_REFERENCE_DISPLAY_TEMPLATE
    from modules.gut_barrier.publication import project_callback_report

    monkeypatch.setattr(reference_order, "load_reference_asset", lambda version, **kwargs: {})
    monkeypatch.setattr(reference_order, "publish_ordered_references", lambda *args: None)
    internal = _new_template_overview_report(ORDERED_REFERENCE_DISPLAY_TEMPLATE)
    before = deepcopy(internal)
    callback = project_callback_report(internal)
    blocks = _overview_blocks(callback)
    assert next(block for block in blocks if block["title"] == "屏障评分分级") == next(
        block for block in _overview_blocks(internal) if block["title"] == "屏障评分分级"
    )
    assert len(next(block for block in blocks if block["title"] == "屏障评分分级")["items"]) == 5
    assert any(block["kind"] == "table" and block["title"] == "辅助资料摘要" for block in blocks)
    assert internal == before


def _clinical_assessment(panel):
    from modules.gut_barrier.assessment import assess_case
    from modules.gut_barrier.schemas import GutBarrierRequest
    request = {"userId": "synthetic-template-review", "sessionId": "fixture-v13", "age": 46, "sex": "女"}
    if panel is not None:
        request["gutHealthIndicator"] = {
            key: {"value": value, "unit": unit} for key, unit, value in zip(
                ("dLacticAcid", "diamineOxidase", "bacterialEndotoxin", "hsCRP"),
                ("mg/L", "U/L", "U/L", "mg/L"), panel) if value is not None}
    return assess_case(GutBarrierRequest.model_validate(request), context={"currentDate": "2026-10-07"})


def _publish(assessed):
    from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE
    from modules.gut_barrier.interventions import build_interventions
    from modules.gut_barrier.presentation import build_report
    from modules.gut_barrier.publication import project_callback_report
    source = build_report(assessed, build_interventions(assessed), report_id="synthetic-template-publication")
    source["meta"]["displayTemplateVersion"] = LATEST_DISPLAY_TEMPLATE
    return project_callback_report(source)


def test_retired_hscrp_and_external_directions_do_not_expand_current_23_item_publication():
    from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment
    from modules.gut_barrier.risk_index_rules import apply_risk_index_rules
    from modules.gut_barrier.review_release import apply_review_release
    assessed = apply_dynamic_assessment(_clinical_assessment((1, 1, 1, ">3.0")))
    assessed["diseaseRisks"].extend([
        {"code": "acne", "name": "痤疮", "systemCode": "skin", "systemName": "皮肤系统",
         "status": "not_evaluated", "riskIndex": None, "riskLevel": None, "reasons": []},
        {"code": "sleep_apnea", "name": "睡眠呼吸暂停", "systemCode": "sleep_respiration", "systemName": "睡眠与呼吸系统",
         "status": "not_evaluated", "riskIndex": None, "riskLevel": None, "reasons": []},
    ])
    assessed = apply_review_release(apply_risk_index_rules(assessed))
    before = deepcopy(assessed)
    report = _publish(assessed)
    rows = _rows(report)
    assert len(_tables(report)) == 8
    assert len(rows) == 23
    scored = [row for row in rows if row[1] is not None]
    assert len(scored) == 23
    assert all(isinstance(row[1], (int, float)) and 1 <= row[1] <= 10 and row[2] in {"低", "中", "高"}
               for row in scored)
    assert all(row[0] not in {"痤疮", "睡眠呼吸暂停"} for row in rows)
    assert _overview(report)["rows"] == [[re.sub(r"^2\.\d+\s+|(?:疾病)?风险评估$", "", table["title"]), *row[:3]]
                                         for table in _tables(report) for row in table["rows"] if row[1] is not None]
    _assert_chapter_synthesis(report)
    report_text = json.dumps(report, ensure_ascii=False)
    assert STATE_FLOOR_NOTICE_TITLE not in report_text and "风险指数说明" not in report_text
    assert "本次为范围型检测结果，建议定量复测观察变化。" not in report_text
    assert assessed == before
    assert all(marker["code"] != "HS_CRP" for marker in assessed["markers"])


@pytest.mark.parametrize("model_count", [1, 23])
def test_v27_rule_results_override_old_models_consistently_without_new_notices(model_count):
    from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment
    from modules.gut_barrier.risk_index_rules import apply_risk_index_rules
    assessed = apply_dynamic_assessment(_clinical_assessment((1, 1, 1, .3)))
    catalog = build_disease_risks()[0]
    models = [{**row, "status": "evaluated", "riskIndex": 9, "riskLevel": "high", "reasons": []}
              for row in catalog[:model_count]]
    assessed["diseaseRisks"] = models
    assessed["riskModel"] = {
        "status": "evaluated", "modelId": "synthetic-model-only", "modelVersion": "test-v1",
        "items": [{"code": row["code"], "status": "evaluated", "reasons": []} for row in models],
    }
    assessed = apply_risk_index_rules(assessed)
    report = _report(blocks=[{"kind": "callout", "title": "独立模型说明", "tone": "default", "text": "原模型说明"}])
    apply_template_risk_review(report, assessed)
    overview = _overview(report)
    assert len(overview["rows"]) == 23
    assert [row[1:] for row in overview["rows"] if row[3] == "中"] == []
    assert sum(row[3] == "低" for row in overview["rows"]) == 23
    assert [row[1:] for row in overview["rows"] if row[3] == "高"] == []
    ruled = {row["code"]: [row["riskIndex"], row["riskLevel"]]
             for row in assessed["riskRuleAssessment"]["items"]}
    by_name = {"代谢相关脂肪性肝病" if row["code"] == "nafld" else row["name"]: ruled[row["code"]]
               for row in catalog}
    assert {row[1]: row[2:] for row in overview["rows"]} == by_name
    assert {row[0]: row[1:3] for row in _rows(report)} == by_name
    blocks = _chapter(report)["blocks"]
    _assert_chapter_synthesis(report)
    assert not any(block["title"] in {STATE_FLOOR_NOTICE_TITLE, "风险指数说明"} for block in blocks)
    assert not any(block["title"] == "独立模型说明" for block in blocks)


def test_v27_unavailable_scores_cannot_fall_back_to_old_model_numbers():
    from modules.gut_barrier.risk_index_rules import apply_risk_index_rules
    chosen = {**_selected(status="evaluated"), "riskIndex": 9, "riskLevel": "high"}
    assessed = _clinical_assessment(None)
    assessed.update(_assessment(chosen, model=True))
    assessed = apply_risk_index_rules(assessed)
    before = deepcopy(assessed)
    report = _publish(assessed)
    assert all(row[1:3] == [None, None] for row in _rows(report))
    assert _overview(report)["rows"] == []
    assert assessed == before


def test_v27_external_followup_model_stays_internal_without_adding_a_public_risk_row():
    from modules.gut_barrier.risk_index_rules import apply_risk_index_rules
    followup = {"code": "gastritis_followup", "name": "既往胃炎随访", "systemCode": "digestive",
                "systemName": "消化系统", "status": "evaluated", "riskIndex": 9, "riskLevel": "high",
                "reasons": [], "mechanism": "原有随访内容。", "qualitative": {"evidenceIds": ["Q34"]}}
    assessed = _clinical_assessment((1, 1, 1, .3))
    assessed.update(_assessment(followup, model=True))
    assessed = apply_risk_index_rules(assessed)
    before = deepcopy(assessed)
    report = _report([followup["name"], 9, "高", "原有随访内容。"])
    apply_template_risk_review(report, assessed)
    assert len(_rows(report)) == 23
    assert all(row[0] != followup["name"] for row in _rows(report))
    assert len(_overview(report)["rows"]) == 23
    assert all(row[1] != followup["name"] for row in _overview(report)["rows"])
    assert assessed == before


@pytest.mark.parametrize("code,name,system,system_name", [
    ("gastritis_followup", "既往胃炎复核", "digestive", "消化系统"),
    ("digestive_lesion_followup", "消化道占位性病变复核", "digestive", "消化系统"),
    ("urinary_stone_followup", "泌尿系统结石及尿检复核", "urinary", "泌尿系统"),
    ("asthma", "已确诊哮喘", "respiratory", "呼吸系统"),
    ("custom_direction", "自定义未知方向", "other", "其他系统"),
])
@pytest.mark.parametrize("panel", [None, (20, 12, 23, None)])
def test_current_rule_publication_excludes_all_external_rows_without_losing_internal_sources(
        code, name, system, system_name, panel):
    from modules.gut_barrier.risk_index_rules import apply_risk_index_rules

    extra = {"code": code, "name": name, "systemCode": system, "systemName": system_name,
             "status": "qualitative_followup", "riskIndex": None, "riskLevel": "随访关注",
             "qualitative": {"basis": "医院检查中的合成来源事实。", "sourceFindings": [{
                 "date": "2024-05-09", "text": "原始检查记录", "source": [{"kind": "hospitalData", "index": 0}]}]}}
    assessed = _clinical_assessment(panel)
    assessed["diseaseRisks"] = [extra]
    assessed = apply_risk_index_rules(assessed)
    before = deepcopy(assessed)
    other_name = "历史未知项目"
    narrow_name = "历史三列项目"
    clinical = {"kind": "table", "title": "个体临床背景", "columns": ["资料", "日期"],
                "rows": [["已采纳医院记录", "2024-05-09"]]}
    report = _report(
        [name, None, "随访关注", "目录外历史机制。"],
        [other_name, 9, "高", "未知历史机制。"],
        ["非酒精性脂肪性肝病", 9, "高", "旧名称不能增加第二条同病种风险。"],
        blocks=[{"kind": "table", "title": "补充疾病风险评估",
                 "columns": ["疾病", "风险等级", "机制关联"],
                 "rows": [[narrow_name, "高", "自定义旧风险表。"]]}, clinical],
    )
    apply_template_risk_review(report, assessed)

    catalog = build_disease_risks()[0]
    expected_names = ["代谢相关脂肪性肝病" if row["code"] == "nafld" else row["name"] for row in catalog]
    rows = _rows(report)
    assert len(_tables(report)) == 8
    assert [row[0] for row in rows] == expected_names
    assert len({row[0] for row in rows}) == 23
    assert clinical in _chapter(report)["blocks"]
    public_text = json.dumps(_chapter(report), ensure_ascii=False)
    assert all(excluded not in public_text for excluded in (name, other_name, narrow_name, "非酒精性脂肪性肝病"))
    expected_overview = [[re.sub(r"^2\.\d+\s+|(?:疾病)?风险评估$", "", table["title"]), *row[:3]]
                         for table in _tables(report) for row in table["rows"] if row[1] is not None]
    assert _overview(report)["rows"] == expected_overview
    assert len(expected_overview) == (23 if panel else 0)
    assert assessed == before
    assert any(row["code"] == code and row["qualitative"] == extra["qualitative"]
               for row in assessed["diseaseRisks"])
    frozen = deepcopy(report)
    apply_template_risk_review(report, assessed)
    assert report == frozen


@pytest.mark.parametrize("panel", [None, (1, 1, 1, 0.3), (36, 22, 32, 2.6)])
def test_actual_assessment_to_callback_does_not_default_missing_or_abnormal_panels_to_low_high(panel):
    from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment
    assessed = apply_dynamic_assessment(_clinical_assessment(panel))
    before = deepcopy(assessed)
    report = _publish(assessed)
    assert len(_rows(report)) >= 22
    assert all(row[1] is None for row in _rows(report))
    assert _overview(report)["rows"] == []
    assert all(row[2] is None for row in _rows(report))
    _assert_chapter_synthesis(report)
    if panel and panel[0] > 30:
        assert any(row["status"] == "attention_index" for row in assessed["diseaseRisks"])
        assert any(row[0] == "肠道炎症与功能紊乱" and row[2] is None for row in _rows(report))
    assert next(row for row in _rows(report) if row[0] == "结直肠癌")[2] is None
    assert assessed == before
    references = _references(report)
    for row in _rows(report):
        assert "[评估" not in str(row[3])
        assert all(1 <= int(number) <= len(references) for number in re.findall(r"\[(\d+)\]", str(row[3])))


@pytest.mark.parametrize("score,level,display_level", [
    (0, "original-test-band", None),
    (0, "low", "低"), (5, "medium", "中"), (9, "high", "高"),
])
def test_registered_adapter_audit_preserves_value_and_grade_through_callback_and_html(score, level, display_level):
    from adapters.gut_barrier.rendering import render_report_html
    from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE
    from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment
    from modules.gut_barrier.risk_evaluator import RiskBand, RiskModelRegistry, RiskModelSpecification, evaluate_registered_risks

    class SyntheticAdapter:
        async def evaluate(self, inputs, *, disease_codes):
            return {"modelId": "synthetic-zero-only", "modelVersion": "v1", "results": [
                {"code": code, "riskIndex": score, "riskLevel": level} for code in disease_codes]}

    registry = RiskModelRegistry()
    registry.register(RiskModelSpecification(
        model_id="synthetic-zero-only", version="v1", approval_reference="test-only approval",
        validation_reference="test-only validation", endpoint="synthetic endpoint", time_horizon="synthetic horizon",
        population="synthetic fixture", disease_inputs=(("obesity", ("age",)),),
        bands=(RiskBand(level, 0, 10, True, True),), precision=0,
        min_age=18, max_age=100, allowed_sexes=("女",), max_observation_age_days=30,
    ), SyntheticAdapter())
    assessed = _clinical_assessment(None)
    assessed["diseaseRisks"], assessed["riskModel"] = asyncio.run(evaluate_registered_risks(
        assessed, registry=registry, model_id="synthetic-zero-only", version="v1", evaluation_date="2026-10-07"))
    assessed = apply_dynamic_assessment(assessed)
    assert next(row for row in assessed["diseaseRisks"] if row["code"] == "obesity")["status"] == "evaluated"
    report = _publish(assessed)
    assert next(row for row in _rows(report) if row[0] == "肥胖")[1:3] == [score, display_level]
    assert _overview(report)["rows"] == [["代谢系统", "肥胖", score, display_level]]
    html = render_report_html(report, template_version=LATEST_DISPLAY_TEMPLATE)
    assert _chapter(report)["blocks"][0]["text"] in html
    if display_level is not None:
        assert f'<td data-label="风险等级">{display_level}</td>' in html
    else:
        assert '<td data-label="风险等级">original-test-band</td>' not in html
        assert "独立模型原始等级为“original-test-band”" not in html
        assert "部分独立模型等级不能转换" not in html
    assert "本次相关因素与复核依据" not in html
    _assert_chapter_synthesis(report)
    assert next(row for row in assessed["diseaseRisks"] if row["code"] == "obesity")["riskLevel"] == level


@pytest.mark.parametrize("panel,expected_marker,expected_grade", [
    ((20.88, .5, 2.4, 2.35), "D-乳酸", "I"),
    ((1, 18, 1, 1), "DAO", "II"),
    ((1, 24.9, 1, .3), "DAO", "III"),
    ((36, 22, 32, 3), "D-乳酸、DAO、LPS", "V"),
    ((20.88, .5, 2.4, None), "D-乳酸", "I"),
])
def test_patient_chapter_summary_reads_actual_markers_grade_without_crp_description(
        panel, expected_marker, expected_grade):
    from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment
    from modules.gut_barrier.risk_index_rules import apply_risk_index_rules
    assessed = apply_risk_index_rules(apply_dynamic_assessment(_clinical_assessment(panel)))
    before = deepcopy(assessed)
    report = _publish(assessed)
    summary = _assert_chapter_synthesis(report)["text"]
    assert assessed["barrier"]["grade"] == expected_grade
    marker_text = f"您的{expected_marker}等肠道屏障相关指标异常"
    grade_text = f"肠道屏障损伤等级为{expected_grade} 级"
    mechanism_text = "肠道屏障受损后，内毒素等物质可能进入血液循环"
    assert summary.index(marker_text) < summary.index(grade_text) < summary.index(mechanism_text)
    assert "hs-CRP" not in summary
    assert "提示需关注肠源性慢性低度炎症相关因素" not in summary
    assert "健康问卷" not in summary
    catalog_names = {"代谢相关脂肪性肝病" if row["code"] == "nafld" else row["name"]
                     for row in build_disease_risks()[0]}
    scored_catalog = [row for row in _rows(report) if row[0] in catalog_names and row[1] is not None]
    highest = sorted(scored_catalog, key=lambda row: (-{"低": 0, "中": 1, "高": 2}[row[2]], -row[1]))[:3]
    risk_prose = summary.split("需重点留意的慢病风险包括", 1)[1]
    listed = re.findall(r"([^，。、；]+)（([\d.]+)分，([低中高])风险）", risk_prose)
    assert listed == [(row[0], f"{row[1]:g}", row[2]) for row in highest]
    assert len(listed) == 3
    assert assessed == before


@pytest.mark.parametrize("panel,expected_marker", [
    ((20.88, .5, 2.4, 2.35), "D-乳酸"),
    ((1, 18, 1, .3), "DAO"),
    ((1, 1, 1, .3), None),
])
def test_new_summary_has_two_visible_paragraphs_with_actual_results_and_unchanged_callback(
        panel, expected_marker):
    from adapters.gut_barrier.rendering import render_report_html
    from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE
    from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment
    from modules.gut_barrier.publication import project_callback_report
    from modules.gut_barrier.risk_index_rules import apply_risk_index_rules

    assessment = apply_risk_index_rules(apply_dynamic_assessment(_clinical_assessment(panel)))
    original_assessment = deepcopy(assessment)
    callback = _publish(assessment)
    summary = _assert_chapter_synthesis(callback)
    paragraphs = summary["text"].split("\n\n")
    assert len(paragraphs) == 2
    assert "hs-CRP" not in summary["text"]
    if expected_marker is not None:
        assert f"您的{expected_marker}等肠道屏障相关指标异常" in paragraphs[0]
        assert f"肠道屏障损伤等级为{assessment['barrier']['grade']} 级" in paragraphs[0]
    else:
        assert "IBFC为0分" in paragraphs[0] and "损伤等级为" not in paragraphs[0]
    assert "内毒素等物质可能进入血液循环" in paragraphs[1]
    assert "本次试行规则下" not in summary["text"]
    catalog_names = {"代谢相关脂肪性肝病" if row["code"] == "nafld" else row["name"]
                     for row in build_disease_risks()[0]}
    ranked = sorted([row for row in _rows(callback) if row[0] in catalog_names and row[1] is not None],
                    key=lambda row: (-{"低": 0, "中": 1, "高": 2}[row[2]], -row[1]))[:3]
    assert "基础关注／待评估" not in paragraphs[1] and "状态下限" not in paragraphs[1]
    actual = re.findall(r"([^，。、；]+)（([\d.]+)分，([低中高])风险）",
                        paragraphs[1].split("需重点留意的慢病风险包括", 1)[1])
    assert actual == [(row[0], f"{row[1]:g}", row[2]) for row in ranked]
    assert len(actual) == 3
    assert paragraphs[1].endswith("修复肠道屏障功能，恢复肠道菌群稳态。")
    assert len([block for block in _chapter(callback)["blocks"]
                if block.get("title") == summary["title"]]) == 1
    assert set(summary) == {"kind", "title", "text"}
    frozen = deepcopy(callback)
    html = render_report_html(callback, template_version=LATEST_DISPLAY_TEMPLATE)
    body = re.search(r'<div class="block" id="disease_risks-2-9">(.*?)</div>', html, re.S)[1]
    assert body.count(f'<h3>{summary["title"]}</h3>') == 1
    rendered_paragraphs = re.findall(r"<p>(.*?)</p>", body, re.S)
    assert len(rendered_paragraphs) == 2
    assert [html_module.unescape(paragraph) for paragraph in rendered_paragraphs] == paragraphs
    assert json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.S)[1]) == frozen
    assert callback == frozen == project_callback_report(callback)
    assert assessment == original_assessment


def test_frozen_single_paragraph_summary_keeps_its_original_text_and_visible_heading():
    from adapters.gut_barrier.rendering import render_report_html
    from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE
    from modules.gut_barrier.publication import project_callback_report

    frozen = _publish(_clinical_assessment((20.88, .5, 2.4, 2.35)))
    summary = _chapter(frozen)["blocks"][-1]
    original_text = "历史冻结小结：本次试行规则下，保留此前发布的完整正文。"
    summary["text"] = original_text
    before = deepcopy(frozen)
    assert project_callback_report(frozen) == before
    html = render_report_html(frozen, template_version=LATEST_DISPLAY_TEMPLATE)
    body = re.search(r'<div class="block" id="disease_risks-2-9">(.*?)</div>', html, re.S)[1]
    assert body == f'<h3>{summary["title"]}</h3><p>{original_text}</p>'
    assert json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.S)[1]) == before
    assert frozen == before


def test_zero_normal_case_uses_v27_final_scores_without_old_state_floor_summary():
    from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment
    from modules.gut_barrier.risk_index_rules import apply_risk_index_rules
    assessed = apply_risk_index_rules(apply_dynamic_assessment(_clinical_assessment((1, 1, 1, .3))))
    report = _publish(assessed)
    summary = _assert_chapter_synthesis(report)["text"]
    assert assessed["barrier"]["score"] == 0 and assessed["barrier"]["grade"] == "normal"
    assert summary.startswith("本次屏障三项未见评分异常，IBFC为0分。")
    assert "损伤等级为" not in summary and "结合hs-CRP" not in summary
    assert "肠道屏障受损时" in summary and "肠道屏障受损后" not in summary
    assert "基础关注／待评估" not in summary and "状态下限" not in summary
    assert "最低风险，临床资料待补充" not in summary
    assert "本次所选项目均为低风险，不提示高风险" not in summary
    assert "肠易激综合征（3.2分，低风险）" in summary
    assert "肝硬化（3.1分，低风险）" in summary and "代谢相关脂肪性肝病（3分，低风险）" in summary
    assert len(re.findall(r"（[\d.]+分，中风险）", summary)) == 0
    assert len(re.findall(r"（[\d.]+分，低风险）", summary)) == 3
    assert len(re.findall(r"（[\d.]+分，高风险）", summary)) == 0
    assert [row[:3] for row in _rows(report) if row[2] == "中"] == []
    assert sum(row[2] == "低" for row in _rows(report)) == 23
    assert all(1 <= row[1] <= 3.3 for row in _rows(report) if row[2] == "低")
    assert len({row[1] for row in _rows(report)}) == 23
    assert not any(block["title"] == "风险评估资料提示" for block in _chapter(report)["blocks"])


@pytest.mark.parametrize("age", [None, 12])
def test_no_adult_grade_is_asserted_when_age_is_missing_or_not_applicable(age):
    assessed = _clinical_assessment((20.88, .5, 2.4, 2.35))
    assessed["subject"]["age"] = age
    summary = _assert_chapter_synthesis(_publish(assessed))["text"]
    assert "本次屏障结果需结合年龄适用的参考范围进行评估。" not in summary
    assert "肠道屏障受损后" in summary and "氧化应激" in summary
    assert "损伤等级为" not in summary and "结合hs-CRP" not in summary


@pytest.mark.parametrize("context_flag", ["acuteInfection", "acuteInflammation"])
def test_known_acute_context_prevents_chronic_inflammation_attribution(context_flag):
    assessed = _clinical_assessment((20.88, .5, 2.4, 2.35))
    assessed["context"][context_flag] = True
    summary = _assert_chapter_synthesis(_publish(assessed))["text"]
    assert "已有急性感染或炎症背景，应先处理当前临床问题" in summary
    assert "结合hs-CRP" not in summary
    assert "提示需关注肠源性慢性低度炎症相关因素" not in summary


@pytest.mark.parametrize("include_risks", [False, True])
@pytest.mark.parametrize("survey,bmi_rows,mentions_questionnaire", [
    ({"source": "healthSurvey", "answers": {}}, [], False),
    ({"source": "healthSurvey", "answers": {"diet": {"status": "unknown"}}}, [], False),
    ({"answers": {"bowelMovement": {"status": "known"}}}, [], True),
    ({"answers": {"fourHigh": {"status": "explicit_none"}}}, [], True),
    ({"source": "healthSurvey", "answers": {}}, [["BMI", "35.2 kg/m²（自述）"]], True),
])
def test_summary_mentions_only_substantive_questionnaire_sources(survey, bmi_rows, mentions_questionnaire, include_risks):
    assessed = _clinical_assessment((20.88, .5, 2.4, 2.35))
    assessed["context"].update(healthSurvey=survey, bmi=35.2, healthSurveyRows=bmi_rows)
    if include_risks:
        from modules.gut_barrier.risk_index_rules import apply_risk_index_rules
        assessed = apply_risk_index_rules(assessed)
    before = deepcopy(assessed)
    summary = _assert_chapter_synthesis(_publish(assessed))["text"]
    assert "健康问卷" not in summary
    assert assessed == before


def test_v27_summary_uses_the_same_final_rule_results_as_tables_even_with_old_models():
    from modules.gut_barrier.risk_index_rules import apply_risk_index_rules
    chosen = [{**_selected(code, status="evaluated"), "riskIndex": score, "riskLevel": level}
              for code, score, level in [("obesity", 9, "low"), ("type2_diabetes", 4, "high"),
                                          ("hyperlipidemia", 5, "medium")]]
    assessed = _clinical_assessment((20.88, .5, 2.4, 2.35))
    assessed.update(_assessment(*chosen, model=True))
    assessed["riskModel"].update(scale={"kind": "relative_index"},
                                 metadata={"endpoint": "synthetic endpoint", "timeHorizon": "synthetic horizon"})
    assessed = apply_risk_index_rules(assessed)
    before = deepcopy(assessed)
    report = _report()
    apply_template_risk_review(report, assessed)
    summary = _assert_chapter_synthesis(report)["text"]
    clauses = re.findall(r"需重点留意的慢病风险包括([^；]+)；", summary)
    assert len(clauses) == 1
    assert "独立模型下" not in summary
    assert "本次试行规则下" not in summary
    assert "肥胖（9分，低风险）" not in summary
    top = sorted(_rows(report), key=lambda row: (-{"低": 0, "中": 1, "高": 2}[row[2]], -row[1]))[:3]
    for name, score, level, _ in top:
        assert f"{name}（{score:g}分，{level}风险）" in summary
    obesity = next(row for row in assessed["riskRuleAssessment"]["items"] if row["code"] == "obesity")
    assert next(row for row in _rows(report) if row[0] == "肥胖")[1:3] == [obesity["riskIndex"], obesity["riskLevel"]]
    assert assessed == before


@pytest.mark.parametrize("missing", ["scale", "endpoint", "timeHorizon", "unknown_grade"])
def test_undeclared_or_unknown_model_semantics_cannot_rank_chronic_diseases(missing):
    chosen = {**_selected(status="evaluated"), "riskIndex": 9, "riskLevel": "high"}
    assessed = _assessment(chosen, model=True)
    assessed["riskModel"].update(scale={"kind": "relative_index"},
                                 metadata={"endpoint": "synthetic endpoint", "timeHorizon": "synthetic horizon"})
    if missing == "scale":
        assessed["riskModel"].pop("scale")
    elif missing == "unknown_grade":
        chosen["riskLevel"] = "TEST_UNKNOWN"
    else:
        assessed["riskModel"]["metadata"].pop(missing)
    report = _report()
    apply_template_risk_review(report, assessed)
    summary = _assert_chapter_synthesis(report)["text"]
    assert "现有结果尚不能比较具体慢病风险高低" not in summary
    assert "肥胖（" not in summary and "独立模型下" not in summary
    assert next(row for row in _rows(report) if row[0] == "肥胖")[1:3] == [9, None if missing == "unknown_grade" else "高"]
