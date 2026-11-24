"""Patient text keeps findings without copying historical aftercare advice."""
from copy import deepcopy

import pytest

from modules.gut_barrier.chapters import _attention_summary, _diseases
from modules.gut_barrier.dynamic_diseases import MODE
from modules.gut_barrier.exam_excerpt import exam_result_excerpt
from modules.gut_barrier.presentation import _clinical_rows
from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.display_versions import (
    ORDERED_REFERENCE_DISPLAY_TEMPLATE, LATEST_DISPLAY_TEMPLATE,
)
from modules.gut_barrier.presentation import build_report
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.review_release import (
    OVERVIEW_GUIDANCE, apply_review_release, overview_management_constraints,
    word_overview_summary, barrier_abnormal_names,
    barrier_injury_grade,
)
from modules.gut_barrier.schemas import GutBarrierRequest


@pytest.mark.parametrize("source, expected", [
    ("慢性非萎缩性胃炎\r术后注意事项：病理报告5个工作日后领取。2小时恢复饮水。", "慢性非萎缩性胃炎"),
    ("所见结直肠粘膜大致正常\n检查后注意事项：如出现便血请就医。", "所见结直肠粘膜大致正常"),
    ("考虑胃炎，尚不能确定。", "考虑胃炎，尚不能确定。"),
    ("未见胃炎。建议结合临床。", "未见胃炎。建议结合临床。"),
    ("患者术后持续腹痛，已排除穿孔。", "患者术后持续腹痛，已排除穿孔。"),
    ("Findings: mild erythema\nPost-procedure instructions: drink water.", "Findings: mild erythema"),
    ("术后注意事项：如腹痛请就医。\n检查结论：未见明显异常。", "检查结论：未见明显异常。"),
    ("诊断意见：胃炎。\n注意事项：饮水。\n病理结论：慢性炎症。\n健康宣教：定期随访。", "诊断意见：胃炎。\n病理结论：慢性炎症。"),
    ("术后注意事项：按时饮水。", ""),
    (None, ""),
])
def test_excerpt_is_extractive_and_preserves_assertion(source, expected):
    assert exam_result_excerpt(source) == expected


def test_public_excerpt_does_not_mutate_raw_record_or_drop_row():
    findings = [{"date": "2025-12-18", "temporalStatus": "historical", "dataType": "EXAM",
                 "itemName": "胃镜", "examResult": "慢性非萎缩性胃炎\r术后注意事项：2小时后饮水。"},
                {"date": "2025-12-15", "temporalStatus": "historical", "dataType": "INSPECTION",
                 "itemName": "检验", "value": 0, "unit": None, "resultStatus": 0},
                {"dataType": "EXAM", "examResult": "术后注意事项：观察。"}]
    before = deepcopy(findings)
    rows = _clinical_rows(findings)
    assert findings == before and len(rows) == 3
    assert rows[0][3] == "慢性非萎缩性胃炎"
    assert rows[1][3].startswith("0")
    assert rows[2][3] == "未提供可用的检查结论"
    assert all(len(row) == 4 for row in rows)


def test_summary_uses_only_present_directions_and_does_not_repeat_basis_or_action():
    rows = [{"code": code, "systemCode": system, "systemName": name, "name": condition,
             "qualitative": {"basis": "检查结论全文", "recommendation": "建议全文"},
             "attention": {"facts": [{"domain": "lipids"}]}}
            for code, system, name, condition in [
                ("hyperlipidemia", "metabolic", "代谢系统", "高脂血症"),
                ("atherosclerosis", "cardiovascular", "心脑血管系统", "心血管关注")]]
    before = deepcopy(rows)
    summary = _attention_summary(rows)
    assert rows == before and len(summary) == 3
    assert "共用" in summary[-1]
    assert "检查结论全文" not in str(summary) and "建议全文" not in str(summary)
    assert "胃炎" not in str(summary) and "神经系统" not in str(summary)
    assert _attention_summary([]) == []
    assert len(_attention_summary(rows[:1])) == 1


def test_qualitative_followup_remains_in_summary_without_inventing_score():
    assessed = {"diseaseAssessment": {"mode": MODE}, "diseaseRisks": [{
        "code": "gastritis_followup", "systemCode": "digestive", "systemName": "消化系统",
        "name": "既往胃炎随访", "riskIndex": None, "riskLevel": "随访关注",
        "status": "qualitative_followup", "attention": None,
        "mechanism": "记录依据", "qualitative": {"evidenceIds": ["Q16"]},
    }]}
    before = deepcopy(assessed)
    chapter = _diseases(assessed)
    summary = next(block for block in chapter["blocks"] if block["title"] == "本章小结")
    table = next(block for block in chapter["blocks"] if block["kind"] == "table")
    assert summary["items"] == ["消化系统关注方向：既往胃炎随访。"]
    assert table["rows"][0][1] is None
    assert assessed == before


def _word_assessment(*, age=42, values=None, omit=(), context=None, observations=None):
    panel = {
        "dLacticAcid": ("20.88", "mg/L"), "diamineOxidase": ("0.5", "U/L"),
        "bacterialEndotoxin": ("2.4", "U/L"), "hsCRP": ("2.35", "mg/L"),
    }
    panel.update(values or {})
    request = GutBarrierRequest.model_validate({
        "userId": "summary-test-user", "sessionId": "summary-test-session", "age": age,
        "gutHealthIndicator": {
            key: {"value": value, "unit": unit}
            for key, (value, unit) in panel.items() if key not in omit
        },
    })
    return apply_review_release(assess_case(
        request, observations=observations,
        context={"currentDate": "2026-10-07", **(context or {})},
    ))


def _word_internal(assessment, version=LATEST_DISPLAY_TEMPLATE):
    report = build_report(assessment, {}, report_id="gbr_word_summary_test")
    report["meta"]["displayTemplateVersion"] = version
    return report


def _overview_block(report, title):
    overview = next(section for section in report["sections"] if section["id"] == "overview")
    return next(block for block in overview["blocks"] if block["title"] == title)


SUMMARY_TITLE = "1.1 综合评估指数（IBFC · 肠屏障功能评分）"


def test_word_summary_uses_same_panel_values_without_copying_sample_claims():
    assessment = _word_assessment()
    before = deepcopy(assessment)
    expected = (
        "您当前的肠道屏障评分 IBFC 为 2 分，伴有D-乳酸指标异常，损伤分级为 I 级，"
        "建议围绕肠道屏障修复开展管理。"
        "目前可围绕可干预因素开展管理，及时改善肠道屏障功能。"
    )
    assert word_overview_summary(assessment) == expected
    assert assessment == before
    report = _word_internal(assessment)
    original = deepcopy(report)
    callback = project_callback_report(report)
    summary = _overview_block(callback, SUMMARY_TITLE)
    assert summary == {"kind": "paragraph", "title": SUMMARY_TITLE, "text": expected}
    assert "\n" not in summary["text"]
    assert not any(claim in summary["text"] for claim in (
        "3.5", "Ⅱ级", "亚健康状态", "整体为肠源性", "慢性低度炎症激活", "可逆转", "关键窗口期",
        "DAO升高", "LPS升高", "保障可耐受的能量和蛋白质",
    ))
    assert report == original
    assert _overview_block(callback, "肠道屏障功能评分（IBFC）")["items"][0]["value"] == 2


@pytest.mark.parametrize("hscrp, expected", [
    ("0", "以维持肠道稳态和持续观察为主"),
    ("2.35", "以维持肠道稳态和持续观察为主"),
])
def test_word_summary_preserves_normal_zero_without_claiming_barrier_damage(hscrp, expected):
    assessment = _word_assessment(values={
        "dLacticAcid": ("0", "mg/L"), "diamineOxidase": ("0", "U/L"),
        "bacterialEndotoxin": ("0", "U/L"), "hsCRP": (hscrp, "mg/L"),
    })
    text = word_overview_summary(assessment)
    assert "IBFC 为 0 分，屏障三项未见评分异常" in text and expected in text
    assert "损伤" not in text and "亚健康状态" not in text and "肠源性" not in text
    assert assessment["barrier"]["score"] == 0
    assert _overview_block(project_callback_report(_word_internal(assessment)), SUMMARY_TITLE)["text"] == text


@pytest.mark.parametrize("omit", [
    ("dLacticAcid",), ("diamineOxidase",), ("bacterialEndotoxin",),
    ("dLacticAcid", "diamineOxidase", "bacterialEndotoxin", "hsCRP"),
])
def test_word_summary_cannot_assign_ibfc_grade_when_barrier_panel_is_incomplete(omit):
    text = word_overview_summary(_word_assessment(omit=omit))
    assert "暂不能确定IBFC评分" in text
    assert "Ⅰ级" not in text and "亚健康状态" not in text
    assert "本次检测的全部信息" not in text


def test_word_summary_keeps_missing_inflammation_unknown_without_discarding_known_ibfc():
    assessment = _word_assessment(omit=("hsCRP",))
    text = word_overview_summary(assessment)
    assert "IBFC 为 2 分" in text and "炎症状态仍需补充检测资料" not in text
    assert "结合hs-CRP" not in text


@pytest.mark.parametrize("age", [None, 0, 17])
def test_word_summary_does_not_apply_adult_grade_to_missing_age_or_children(age):
    assessment = _word_assessment(age=age)
    text = word_overview_summary(assessment)
    assert "年龄适用的参考范围" in text and "生长发育" in text
    assert "Ⅰ级" not in text and "亚健康状态" not in text and "hs-CRP升高" not in text
    callback = project_callback_report(_word_internal(assessment))
    callout = _overview_block(callback, "个体管理提示")
    assert callout["tone"] == "caution"
    assert callout["text"] == OVERVIEW_GUIDANCE["age_specific"]
    assert "不套用成人营养剂量和制剂用法" in callout["text"]


@pytest.mark.parametrize("raw", [">20", ">=20", "<20"])
def test_word_summary_does_not_turn_barrier_range_into_exact_score(raw):
    assessment = _word_assessment(values={"dLacticAcid": (raw, "mg/L")})
    text = word_overview_summary(assessment)
    assert assessment["barrier"]["score"] is None
    assert "暂不能确定IBFC评分" in text and "IBFC 2" not in text


@pytest.mark.parametrize("raw", ["<0.5", "<=0.5", ">3", ">10", "100"])
def test_word_summary_ignores_crp_ranges_and_high_results(raw):
    assessment = _word_assessment(values={"hsCRP": (raw, "mg/L")})
    before = deepcopy(assessment)
    text = word_overview_summary(assessment)
    assert text == word_overview_summary(_word_assessment(omit=("hsCRP",)))
    assert "目前可围绕可干预因素开展管理" in text
    assert assessment == before and "inflammation" not in assessment



@pytest.mark.parametrize("key,value,name,score,grade", [
    ("dLacticAcid", "20.88", "D-乳酸", 2, "I 级"),
    ("diamineOxidase", "24.9", "DAO", 5, "III 级"),
    ("bacterialEndotoxin", "24", "LPS", 3, "II 级"),
])
def test_patient_summary_uses_actual_marker_and_existing_grade_not_example_values(key, value, name, score, grade):
    values = {"dLacticAcid": ("0", "mg/L"), "diamineOxidase": ("0", "U/L"),
              "bacterialEndotoxin": ("0", "U/L"), "hsCRP": ("0.5", "mg/L")}
    values[key] = (value, values[key][1])
    assessment = _word_assessment(values=values)
    before = deepcopy(assessment)
    assert barrier_abnormal_names(assessment) == [name]
    assert barrier_injury_grade(assessment) == grade
    text = word_overview_summary(assessment)
    assert f"IBFC 为 {score} 分" in text and f"伴有{name}指标异常" in text
    assert f"损伤分级为 {grade}" in text
    assert "炎症" not in text and "hs-CRP" not in text
    assert assessment == before


@pytest.mark.parametrize("raw", ["0", "0.49", "0.5"])
def test_project_ideal_hscrp_never_creates_elevation_or_intestinal_chronic_inflammation(raw):
    assessment = _word_assessment(values={"hsCRP": (raw, "mg/L")})
    before = deepcopy(assessment)
    text = word_overview_summary(assessment)
    assert "hs-CRP" not in text and "炎症" not in text
    assert assessment == before


@pytest.mark.parametrize("assessment", [None, {}, {"barrier": {}},
                                          {"barrier": {"score": 0, "grade": "normal"}},
                                          {"barrier": {"score": 4.5, "grade": None}}])
def test_patient_findings_helpers_do_not_invent_missing_or_normal_grade(assessment):
    before = deepcopy(assessment)
    assert barrier_abnormal_names(assessment) == []
    assert barrier_injury_grade(assessment) is None
    assert assessment == before


def test_stale_internal_crp_data_cannot_reappear_in_current_summary():
    assessment = _word_assessment()
    expected = word_overview_summary(assessment)
    assessment["inflammation"] = {"value": 100, "operator": "=", "possibleBands": ["requires_clinical_review"]}
    assert word_overview_summary(assessment) == expected



@pytest.mark.parametrize("age,context,raw,omit", [
    (None, {}, "2.35", ()), (17, {}, "2.35", ()),
    (42, {"acuteInfection": True}, "2.35", ()),
    (42, {"acuteInflammation": True}, "2.35", ()),
    (42, {}, "10.1", ()), (42, {}, ">3", ()),
    (42, {}, "2.35", ("hsCRP",)),
])
def test_patient_inflammation_clause_never_overrides_missing_age_acute_or_unbounded_ranges(age, context, raw, omit):
    assessment = _word_assessment(age=age, context=context, values={"hsCRP": (raw, "mg/L")}, omit=omit)
    before = deepcopy(assessment)
    assert "肠源性" not in word_overview_summary(assessment)
    assert assessment == before


@pytest.mark.parametrize("marker_code", ["DAO", "HS_CRP"])
def test_word_summary_does_not_choose_one_conflicting_measurement(marker_code):
    assessment = _word_assessment(observations=[{
        "code": marker_code, "value": "12", "unit": "U/L" if marker_code == "DAO" else "mg/L",
        "isCurrent": True, "source": {"kind": "file", "location": "synthetic-conflict.pdf"},
    }])
    text = word_overview_summary(assessment)
    if marker_code == "DAO":
        assert "暂不能确定IBFC评分" in text and "DAO升高" not in text
    else:
        assert "IBFC 为 2 分" in text and "hs-CRP" not in text


@pytest.mark.parametrize("flag", ["acuteInfection", "acuteInflammation"])
def test_word_summary_prioritizes_documented_acute_context(flag):
    assessment = _word_assessment(context={flag: True})
    text = word_overview_summary(assessment)
    assert "已有急性感染或炎症背景，应先处理当前临床问题，稳定后再复评" in text
    assert "目前可围绕可干预因素开展管理" not in text
    assert "肠源性" not in text and "慢性低度炎症" not in text
    callback = project_callback_report(_word_internal(assessment))
    assert _overview_block(callback, SUMMARY_TITLE)["text"] == text
    assert "急性感染或炎症背景" in _overview_block(callback, "个体管理提示")["text"]


def test_high_crp_does_not_invent_acute_context_or_override_barrier_summary():
    text = word_overview_summary(_word_assessment(values={"hsCRP": ("10.1", "mg/L")}))
    assert "目前可围绕可干预因素开展管理" in text
    assert "已有急性感染" not in text and "hs-CRP" not in text



@pytest.mark.parametrize("code, expected", [
    ("fiber_hold", "已有消化道通行相关记录，先核对饮食形式，不按常规目标增加膳食纤维。"),
    ("kidney_problem", "已有肝肾功能相关记录，营养补充与医疗营养方案需先完成专科安全评估。"),
    ("liver_problem", "已有肝肾功能相关记录，营养补充与医疗营养方案需先完成专科安全评估。"),
    ("immune_compromised", "已有免疫状态相关记录，活菌及菌群移植措施需先完成专科适用性评估。"),
    ("severe_immune_compromise", "已有免疫状态相关记录，活菌及菌群移植措施需先完成专科适用性评估。"),
    ("activity_hold", "已有活动安全相关记录，不套用常规运动目标，应先完成临床评估。"),
    ("acute_infection", "先处理当前急性感染或炎症相关情况，稳定后再复评屏障指标。"),
    ("acute_inflammation", "先处理当前急性感染或炎症相关情况，稳定后再复评屏障指标。"),
])
def test_word_publication_moves_each_safety_suffix_into_patient_management_callout(code, expected):
    assessment = _word_assessment()
    # This presentation-unit test consumes an already accepted assessment signal.
    assessment["context"]["hospitalSafetySignals"] = [{"code": code}]
    report = _word_internal(assessment)
    original = deepcopy(report)
    callback = project_callback_report(report)
    callout = _overview_block(callback, "个体管理提示")
    assert callout == {"kind": "callout", "title": "个体管理提示", "tone": "caution", "text": expected}
    assert expected not in _overview_block(callback, SUMMARY_TITLE)["text"]
    assert "围绕可干预诱因" not in callout["text"]
    assert report == original and project_callback_report(callback) == callback


@pytest.mark.parametrize("prefix", [OVERVIEW_GUIDANCE["normal"], OVERVIEW_GUIDANCE["abnormal"]])
def test_management_constraint_extraction_only_removes_known_generic_prefix(prefix):
    suffix = "必须保留的个体限制。另一条自定义临床提醒。"
    assert overview_management_constraints(prefix + suffix) == suffix
    assert overview_management_constraints(prefix) == ""


def test_management_constraint_extraction_preserves_age_specific_safety_advice_and_suffix():
    prefix = OVERVIEW_GUIDANCE["age_specific"]
    suffix = "另有明确的临床安全限制。"
    assert overview_management_constraints(prefix) == prefix
    assert overview_management_constraints(prefix + suffix) == prefix + suffix


@pytest.mark.parametrize("guidance, expected", [
    ("自定义临床建议，不能因精简总评而删除。", "自定义临床建议，不能因精简总评而删除。"),
    (None, ""), (False, ""), (0, ""),
])
def test_management_constraint_extraction_retains_unknown_text_and_handles_absent_advice(guidance, expected):
    assert overview_management_constraints(guidance) == expected


def test_current_summary_does_not_rewrite_an_already_dynamic_frozen_receipt():
    callback = project_callback_report(_word_internal(_word_assessment()))
    _overview_block(callback, SUMMARY_TITLE)["text"] = "历史已经冻结的三段总评。\n历史干预正文。\n不得按当前模板重写。"
    original = deepcopy(callback)
    assert project_callback_report(callback) == original
    assert callback == original


@pytest.mark.parametrize("score,grade", [(2, "I"), (3.5, "II"), (5, "III"), (8, "IV"), (12, "V"), (4.5, None)])
def test_new_patient_summary_reads_existing_injury_grade_without_reclassifying_stage_or_cards(score, grade):
    from adapters.gut_barrier.rendering import render_report_html
    import json
    import re

    assessment = _word_assessment()
    # Publication-unit fixture: read a previously calculated score/grade;
    # do not ask the publication layer to invent or recalculate either.
    assessment["barrier"].update(score=score, grade=grade)
    internal = _word_internal(assessment)
    before = deepcopy(internal)
    callback = project_callback_report(internal)
    overview = next(section for section in callback["sections"] if section["id"] == "overview")
    cards = _overview_block(callback, "肠道屏障功能评分（IBFC）")["items"]
    assert next(item for item in cards if item["label"] == "损伤等级") == {
        "label": "损伤等级", "value": None, "detail": None,
    }
    assert next(item for item in cards if item["label"] == "总体阶段") == {
        "label": "总体阶段", "value": None, "detail": None,
    }
    assert next(item["value"] for item in cards if item["label"] == "IBFC评分") == score
    assert not any(item["label"] == "受损类型" for item in cards)
    summary = _overview_block(callback, SUMMARY_TITLE)["text"]
    assert f"损伤分级为 {grade} 级" in summary if grade else "损伤分级需进一步确认" in summary
    assert barrier_injury_grade(assessment) == (f"{grade} 级" if grade else None)
    texts = json.dumps(overview, ensure_ascii=False)
    assert not re.search(r"(?:轻度|中度|重度|极重度)损伤", texts)
    assert "亚健康状态" not in texts
    html = render_report_html(callback, template_version=LATEST_DISPLAY_TEMPLATE)
    visible = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", html, flags=re.S)
    assert '<div class="fact-label">损伤等级</div>' not in visible
    assert '<div class="fact-label">总体阶段</div>' not in visible
    if grade:
        assert f"损伤分级为 {grade} 级" in visible
    assert all("级" not in item["label"] for item in _overview_block(callback, "屏障评分分级")["items"])
    assert json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.S)[1]) == callback
    assert internal == before
    assert internal["assessment"]["barrier"]["grade"] == grade
    assert internal["assessment"]["barrier"]["type"] == assessment["barrier"]["type"]


def test_neutral_score_projection_retains_custom_safety_suffix_and_other_disease_grades():
    from adapters.gut_barrier.rendering import render_report_html
    from tests.unit.gut_barrier.preview.support import visible_source
    assessment = _word_assessment()
    report = _word_internal(assessment)
    suffix = "已有重度慢性肾病，须专科评估；不改写临床Ⅱ级心功能记录。"
    _overview_block(report, "1.3 肠道屏障功能评分（IBFC）与损伤分级")["text"] += suffix
    _overview_block(report, "肠道屏障功能评分（IBFC）")["items"][1]["detail"] = "原始临床安全备注必须保留。"
    before = deepcopy(report)
    callback = project_callback_report(report)
    paragraphs = [block["text"] for block in next(s for s in callback["sections"] if s["id"] == "overview")["blocks"]
                  if block["kind"] == "paragraph"]
    assert any(text.startswith("肠道屏障功能评分（IBFC）为 2 分") and text.endswith(suffix) for text in paragraphs)
    assert "原始临床安全备注必须保留。" in visible_source(render_report_html(callback, template_version=LATEST_DISPLAY_TEMPLATE))
    assert report == before


def test_frozen_current_old_grade_card_and_summary_remain_verbatim():
    callback = project_callback_report(_word_internal(_word_assessment()))
    _overview_block(callback, SUMMARY_TITLE)["text"] = "冻结结论：IBFC 3.5，Ⅱ级（轻度损伤）。"
    cards = _overview_block(callback, "肠道屏障功能评分（IBFC）")["items"]
    next(item for item in cards if item["label"] == "损伤等级")["value"] = "II 级（轻度）"
    before = deepcopy(callback)
    assert project_callback_report(callback) == before == callback


@pytest.mark.parametrize("raw", ["0.99", "1", "2.99", "3", "3.01", ">3", "100"])
def test_current_public_word_summary_and_marker_table_exclude_crp(raw):
    from adapters.gut_barrier.rendering import render_report_html
    import json
    import re
    assessment = _word_assessment(values={"hsCRP": (raw, "mg/L")})
    before = deepcopy(assessment)
    callback = project_callback_report(_word_internal(assessment))
    blocks = next(s["blocks"] for s in callback["sections"] if s["id"] == "overview")
    table = next(b for b in blocks if b["title"] == "1.2 检测项目与结果总览")
    assert len(table["rows"]) == 3
    assert not re.search(r"crp|C反应蛋白|C-reactive", json.dumps(callback,ensure_ascii=False),re.I)
    html = render_report_html(callback, template_version=LATEST_DISPLAY_TEMPLATE)
    assert not re.search(r"crp|C反应蛋白|C-reactive", html,re.I)
    assert json.loads(re.search(r'id="report-data">(.*?)</script>', html,re.S)[1]) == callback
    assert assessment == before



@pytest.mark.parametrize("context,age", [({"acuteInfection": True},42), ({"acuteInflammation":True},42), ({},17), ({},None)])
def test_ignoring_crp_preserves_age_and_documented_acute_guards(context,age):
    assessment = _word_assessment(age=age,values={"hsCRP": ("3","mg/L")},context=context)
    assert "hsCRP" not in assessment["reportReview"]
    text = word_overview_summary(assessment)
    assert "hs-CRP" not in text
    assert ("已有急性感染或炎症背景" if age == 42 else "年龄适用的参考范围") in text



def test_crp_range_does_not_create_followup_or_chronicity_claim():
    assessment = _word_assessment(values={"hsCRP": (">3.0", "mg/L")})
    text = word_overview_summary(assessment)
    assert not any(x in text for x in ("hs-CRP", "定量复测", "不能排除 >10", "肠源性", "慢性炎症已确诊"))



def test_old_v10_projection_keeps_previous_joined_summary(monkeypatch):
    from modules.gut_barrier import reference_order

    # Isolate bibliography I/O: this test is for the release-gated narrative,
    # not permission to make archived production assets available automatically.
    monkeypatch.setattr(reference_order, "load_reference_asset", lambda version, **kwargs: {})
    monkeypatch.setattr(reference_order, "publish_ordered_references", lambda *args: None)
    report = _word_internal(_word_assessment(), ORDERED_REFERENCE_DISPLAY_TEMPLATE)
    conclusions = _overview_block(report, "1.1 检测综合评估结论")["rows"]
    expected = "\n".join(value for label, value in conclusions if label != "炎症状态")
    before = deepcopy(report)
    callback = project_callback_report(report)
    assert _overview_block(callback, SUMMARY_TITLE)["text"] == expected
    assert "围绕可干预诱因" in expected and "\n" in expected
    assert report == before


@pytest.mark.parametrize("assessment", [None, {}, {"barrier": {}}])
def test_word_summary_does_not_replace_custom_documents_without_assessed_barrier(assessment):
    assert word_overview_summary(assessment) is None


_UNKNOWN_PATTERN_FALLBACK = "当前三项组合尚未确定，结合已获得的单项结果进一步完善分型评价。"


def _overview_blocks(report):
    return next(section["blocks"] for section in report["sections"] if section["id"] == "overview")


def _scope_block(kind="paragraph"):
    from modules.gut_barrier.source_scope import source_scope_block
    block = source_scope_block([{"code": "OCR_UNAVAILABLE"}], [])
    if kind == "callout":
        block.update(kind="callout", tone="caution")
    return block


@pytest.mark.parametrize("scope_kind", ["paragraph", "callout"])
def test_new_current_publication_removes_scope_and_type_card_without_rewriting_internal_assessment(scope_kind):
    from adapters.gut_barrier.rendering import render_report_html
    import json
    import re

    internal = _word_internal(_word_assessment())
    _overview_blocks(internal).insert(1, _scope_block(scope_kind))
    original = deepcopy(internal)
    original_cards = _overview_block(internal, "肠道屏障功能评分（IBFC）")["items"]
    assert any(item["label"] == "受损类型" for item in original_cards)
    callback = project_callback_report(internal)
    blocks = _overview_blocks(callback)
    assert not any(block["title"] == "本次评估资料范围" for block in blocks)
    cards = _overview_block(callback, "肠道屏障功能评分（IBFC）")
    assert set(cards) == {"kind", "title", "items"}
    assert all(set(item) == {"label", "value", "detail"} for item in cards["items"])
    assert not any(item["label"] == "受损类型" for item in cards["items"])
    assert next(item["value"] for item in cards["items"] if item["label"] == "IBFC评分") == 2
    assert internal == original
    assert internal["assessment"]["barrier"]["type"] == original["assessment"]["barrier"]["type"]
    assert internal["assessment"]["barrier"]["type"] is not None
    html = render_report_html(callback, template_version=LATEST_DISPLAY_TEMPLATE)
    assert json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.S)[1]) == callback
    visible = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", html, flags=re.S)
    assert "本次评估资料范围" not in visible
    assert '<div class="fact-label">受损类型</div>' not in visible


@pytest.mark.parametrize("suffix", ["", "自定义临床安全提醒不能删除。后续须核对检验原件。"])
def test_new_current_removes_only_known_unknown_pattern_prefix_and_preserves_custom_suffix(suffix):
    internal = _word_internal(_word_assessment(omit=("dLacticAcid",)))
    source = _overview_block(internal, "1.4 肠道屏障受损类型与受损状况")
    assert source["text"] == _UNKNOWN_PATTERN_FALLBACK
    source["text"] += suffix
    before = deepcopy(internal)
    callback = project_callback_report(internal)
    paragraphs = [block for block in _overview_blocks(callback) if block["kind"] == "paragraph"]
    assert not any(_UNKNOWN_PATTERN_FALLBACK in block["text"] for block in paragraphs)
    if suffix:
        assert {"kind": "paragraph", "title": None, "text": suffix} in paragraphs
    assert internal == before
    assert internal["assessment"]["barrier"]["score"] is None
    assert internal["assessment"]["barrier"]["type"] is None
    assert project_callback_report(callback) == callback


def test_new_current_preserves_real_pattern_explanation_and_its_appended_safety_text():
    internal = _word_internal(_word_assessment())
    source = _overview_block(internal, "1.4 肠道屏障受损类型与受损状况")
    assert "本次以D-乳酸指标异常为特征" in source["text"]
    suffix = "已有临床肠梗阻，任何纤维补充前均须专科评估。"
    source["text"] += suffix
    expected = source["text"]
    before = deepcopy(internal)
    callback = project_callback_report(internal)
    assert {"kind": "paragraph", "title": None, "text": expected} in _overview_blocks(callback)
    assert internal == before


def test_unknown_pattern_cleanup_does_not_match_a_custom_sentence_containing_the_fallback():
    internal = _word_internal(_word_assessment(omit=("dLacticAcid",)))
    custom = "保留的自定义复核说明：" + _UNKNOWN_PATTERN_FALLBACK + "不能按固定开头截断。"
    _overview_block(internal, "1.4 肠道屏障受损类型与受损状况")["text"] = custom
    before = deepcopy(internal)
    callback = project_callback_report(internal)
    assert {"kind": "paragraph", "title": None, "text": custom} in _overview_blocks(callback)
    assert internal == before


def test_frozen_dynamic_scope_type_card_and_unknown_pattern_text_remain_verbatim():
    callback = project_callback_report(_word_internal(_word_assessment()))
    blocks = _overview_blocks(callback)
    blocks.insert(1, _scope_block())
    cards = _overview_block(callback, "肠道屏障功能评分（IBFC）")["items"]
    cards.append({"label": "受损类型", "value": "冻结的原受损类型", "detail": "冻结的原始备注"})
    blocks.append({"kind": "paragraph", "title": None, "text": _UNKNOWN_PATTERN_FALLBACK + "冻结后缀。"})
    before = deepcopy(callback)
    assert project_callback_report(callback) == before == callback


def test_v10_keeps_scope_type_card_and_unknown_pattern_text(monkeypatch):
    from modules.gut_barrier import reference_order
    monkeypatch.setattr(reference_order, "load_reference_asset", lambda version, **kwargs: {})
    monkeypatch.setattr(reference_order, "publish_ordered_references", lambda *args: None)
    internal = _word_internal(_word_assessment(omit=("dLacticAcid",)), ORDERED_REFERENCE_DISPLAY_TEMPLATE)
    scope = _scope_block()
    _overview_blocks(internal).insert(1, scope)
    before = deepcopy(internal)
    callback = project_callback_report(internal)
    blocks = _overview_blocks(callback)
    assert scope in blocks
    assert any(item["label"] == "受损类型" for item in _overview_block(callback, "肠道屏障功能评分（IBFC）")["items"])
    assert {"kind": "paragraph", "title": None, "text": _UNKNOWN_PATTERN_FALLBACK} in blocks
    assert internal == before


def test_standalone_normalizer_does_not_adopt_current_scope_or_type_cleanup():
    from modules.gut_barrier.publication import normalize_current_dynamic_report, project_public_report
    internal = _word_internal(_word_assessment(omit=("dLacticAcid",)))
    scope = _scope_block()
    _overview_blocks(internal).insert(1, scope)
    public = project_public_report(internal)
    before = deepcopy(public)
    normalized = normalize_current_dynamic_report(public)
    blocks = _overview_blocks(normalized)
    assert scope in blocks
    assert any(item["label"] == "受损类型" for item in _overview_block(normalized, "肠道屏障功能评分（IBFC）")["items"])
    assert {"kind": "paragraph", "title": None, "text": _UNKNOWN_PATTERN_FALLBACK} in blocks
    assert public == before
