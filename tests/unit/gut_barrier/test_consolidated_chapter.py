"""v10 keeps adopted clinical facts internal and publishes one synthesis."""
from copy import deepcopy

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.chapters import _diseases, consolidated_disease_summary
from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment
from modules.gut_barrier.integration_content import (
    ATTENTION_OVERVIEW_TITLE, RISK_COLUMNS, complete_disease_content,
)
from modules.gut_barrier.schemas import GutBarrierRequest


# Dates and imaging wording are synthetic; their clinical relationships are intentional.
DAY = "2026-10-06"
CLINICAL_COLUMNS = ["检查日期", "项目", "时间属性", "原始检查摘要"]


def _assessment(*, d=20.88, dao=0.5, lps=2.4, crp=2.35, historical=False):
    hospital = []
    if historical:
        hospital = [{"dataType": "EXAM", "itemName": "CT全腹平扫+增强", "checkTime": "2024-05-09",
                     "examResult": "结肠肝曲见结节样强化灶，考虑间质瘤可能。双肾见小结石。"}]
        hospital.extend({"dataType": "INSPECTION", "itemName": f"保留原始项目{index}",
                         "checkTime": "2022-08-11", "testResultValue": str(index),
                         "resultStatus": 0, "resultStatusName": "正常"} for index in range(41))
    request = GutBarrierRequest.model_validate({
        "userId": "chapter-summary-unit", "sessionId": "synthetic", "age": 31, "sex": "男",
        "hospitalData": hospital,
        "gutHealthIndicator": {key: {"value": value, "unit": unit}
                               for key, value, unit in (
                                   ("dLacticAcid", d, "mg/L"), ("diamineOxidase", dao, "U/L"),
                                   ("bacterialEndotoxin", lps, "U/L"), ("hsCRP", crp, "mg/L"))
                               if value is not None},
    })
    return apply_dynamic_assessment(assess_case(request, context={"currentDate": DAY}))


def _qualitative_row(system, name, *dates):
    return {"code": f"synthetic_{system}", "name": name, "systemCode": system,
            "systemName": {"digestive": "消化系统", "urinary": "泌尿系统"}[system],
            "status": "qualitative_followup", "riskIndex": None, "riskLevel": "随访关注",
            "probability": None, "mechanism": "既往专项检查发现仍需核实。",
            "qualitative": {"evidenceIds": ["Q15"], "basis": "已采纳的历史检查记录",
                            "recommendation": "核对后续记录。",
                            "sourceFindings": [{"date": value, "itemName": "已采纳检查",
                                                "excerpt": "保留不确定性的原始摘要", "temporalStatus": "historical"}
                                               for value in dates]}}


def _summary(blocks):
    result = [block for block in blocks if "本章小结" in (block.get("title") or "")]
    assert len(result) == 1 and result[0]["kind"] == "paragraph"
    assert set(result[0]) == {"kind", "title", "text"}
    return result[0]


def test_new_layout_hides_raw_clinical_table_but_retains_all_internal_facts_and_risk_rows():
    assessed = _assessment(historical=True)
    original_assessment = deepcopy(assessed)
    blocks = _diseases(assessed)["blocks"]
    original_blocks = deepcopy(blocks)
    risk_tables = [block for block in blocks if block.get("columns") == RISK_COLUMNS]
    background = next(block for block in blocks if block.get("columns") == CLINICAL_COLUMNS)
    assert len(background["rows"]) == len(assessed["context"]["clinicalFindings"]) == 42
    result = complete_disease_content(blocks, product_feedback=True,
                                      ordered_reference_layout=True, assessment=assessed)
    assert not any(block.get("columns") == CLINICAL_COLUMNS for block in result)
    assert "保留原始项目" not in str(result)
    assert [block for block in result if block.get("columns") == RISK_COLUMNS] == risk_tables
    assert any(block["title"] == "影像检查待核实事项" for block in result)
    assert _summary(result)["title"] == f"2.{len(risk_tables) + 1} 本章小结"
    assert result[-1] == _summary(result)
    assert assessed == original_assessment and blocks == original_blocks


def test_synthesis_relates_current_findings_without_repeating_disease_rows():
    assessed = _assessment()
    text = consolidated_disease_summary(assessed)
    assert "D-乳酸升高" in text and "DAO、LPS未同步升高" in text and "hs-CRP" not in text
    assert "屏障功能与消化道表现的联系" in text
    assert "糖脂代谢" not in text and "心血管相关因素" not in text
    assert "复核屏障相关检测" in text
    assert "\n" not in text
    assert all(row["name"] not in text for row in assessed["diseaseRisks"])
    assert not any(term in text for term in ("神经退行", "肠源性炎症确诊", "高风险", "低风险", "患病概率"))


def test_only_adopted_historical_rows_supply_dates_and_followup_scope():
    assessed = _assessment()
    assessed["diseaseRisks"].extend([
        _qualitative_row("digestive", "消化道占位性病变复核", "2024-05-09"),
        _qualitative_row("urinary", "泌尿系统结石及尿检复核", "2024-05-09", "2022-08-11"),
    ])
    assessed["context"]["clinicalFindings"] = [
        {"date": "2020-01-01", "itemName": "未采纳脑部影像", "examResult": "阿尔茨海默病"}]
    original = deepcopy(assessed)
    text = consolidated_disease_summary(assessed)
    assert "既往消化系统、泌尿系统检查（2022-08-11、2024-05-09）" in text
    assert "核对后续处理和复查记录" in text
    assert "2020-01-01" not in text and "阿尔茨海默" not in text and "神经系统" not in text
    assert "结石导致" not in text and "确诊" not in text
    assert assessed == original


def test_historical_only_case_does_not_invent_current_barrier_or_inflammatory_abnormality():
    assessed = _assessment(d=None, dao=None, lps=None, crp=None)
    assessed["diseaseRisks"] = [_qualitative_row("urinary", "既往泌尿资料复核", "2022-08-11")]
    text = consolidated_disease_summary(assessed)
    assert "既往泌尿系统检查（2022-08-11）" in text
    assert not any(term in text for term in ("本次炎症", "本次血尿", "升高", "消化系统", "心血管"))
    assert "当前症状" in text and "随访" in text


def test_adopted_urine_review_preserves_date_and_specific_record_problems_without_raw_values_or_diagnosis():
    assessed = _assessment()
    row = _qualitative_row("urinary", "泌尿系统结石及尿检复核", "2024-05-09")
    row["qualitative"]["recordReviews"] = [
        {"date": "2022-08-11", "temporalStatus": "historical", "itemName": "红细胞计数",
         "value": "26", "reasonCodes": ["urine_record_requires_confirmation", "missing_unit"]},
        {"date": "2022-08-11", "temporalStatus": "historical", "itemName": "隐血",
         "value": "3+", "reasonCodes": ["urine_record_requires_confirmation", "result_status_conflict"]},
    ]
    assessed["diseaseRisks"].append(row)
    original = deepcopy(assessed)
    result = complete_disease_content(_diseases(assessed)["blocks"], product_feedback=True,
                                      ordered_reference_layout=True, assessment=assessed)
    text = _summary(result)["text"]
    assert "既往尿检（2022-08-11）部分检验单位缺失、结果与状态标记不一致，宜核对原报告及后续复查。" in text
    assert text.count("2022-08-11") == 1
    assert all(term not in text for term in ("3+", "血尿", "便血", "结石导致", "确诊"))
    assert assessed == original


@pytest.mark.parametrize("reasons,expected", [
    (["urine_record_requires_confirmation"], "结果仍待复核"),
    (["urine_record_requires_confirmation", "source_result_conflict"], "来源记录存在冲突"),
])
def test_urine_review_summary_uses_only_its_own_adopted_flags_and_time(reasons, expected):
    assessed = _assessment(d=None, dao=None, lps=None, crp=None)
    row = _qualitative_row("urinary", "泌尿资料复核")
    row["qualitative"]["recordReviews"] = [
        {"date": DAY, "temporalStatus": "current", "reasonCodes": reasons},
        {"date": "2020-01-01", "temporalStatus": "date_unknown", "reasonCodes": reasons},
        {"date": "2020-01-02", "temporalStatus": "historical", "reasonCodes": ["missing_unit"]},
    ]
    assessed["diseaseRisks"] = [row]
    text = consolidated_disease_summary(assessed)
    assert f"本次尿检（{DAY}）{expected}，宜核对原报告及后续复查。" in text
    assert "既往尿检" not in text and "2020-01-01" not in text and "2020-01-02" not in text
    assert "结果与状态标记不一致" not in text and "检验单位缺失" not in text


@pytest.mark.parametrize("d,crp", [(None, None), (">30", ">3")])
def test_missing_or_unresolved_values_do_not_become_exact_abnormalities_in_summary(d, crp):
    assessed = _assessment(d=d, dao=None, lps=None, crp=crp)
    text = consolidated_disease_summary(assessed)
    assert "升高" not in text
    assert "低风险" not in text and "已排除" not in text
    assert "完善" in text


def test_normal_measurements_are_not_turning_absence_of_disease_rows_into_low_risk():
    assessed = _assessment(d=1, dao=1, lps=1, crp=0.3)
    text = consolidated_disease_summary(assessed)
    assert "屏障三项未见评分异常" in text
    assert "低风险" not in text and "无疾病" not in text
    assert "不足以形成具体疾病关注方向" in text


def test_empty_assessment_uses_only_actual_table_systems_and_retains_independent_zero():
    blocks = [{"kind": "callout", "title": "本次关注指数", "text": "既有说明", "tone": "default"},
              {"kind": "table", "title": "2.1 心脑血管系统疾病风险评估", "columns": RISK_COLUMNS[:],
               "rows": [["独立模型结果", 0, "基础关注", "已确认机制。[评估4]"]]},
              {"kind": "list", "title": "2.2 本章小结", "items": ["不应继续逐病重述"]}]
    result = complete_disease_content(blocks, product_feedback=True, ordered_reference_layout=True,
                                      model_names={"独立模型结果"})
    assert next(block for block in result if block.get("columns") == RISK_COLUMNS)["rows"] == blocks[1]["rows"]
    assert next(block for block in result if block["title"] == ATTENTION_OVERVIEW_TITLE)["rows"] == []
    text = _summary(result)["text"]
    assert "心脑血管系统" in text and "消化系统" not in text and "屏障" not in text
    assert "不应继续逐病重述" not in str(result)


def test_new_consolidation_is_idempotent_and_keeps_existing_alarm_callouts():
    assessed = _assessment()
    blocks = _diseases(assessed)["blocks"]
    alarm = {"kind": "callout", "title": "便血就医提示", "tone": "caution", "text": "已核实的便血提示。"}
    blocks.append(alarm)
    first = complete_disease_content(blocks, product_feedback=True,
                                     ordered_reference_layout=True, assessment=assessed)
    assert alarm in first
    assert complete_disease_content(first, product_feedback=True,
                                    ordered_reference_layout=True, assessment=assessed) == first


def test_v9_default_keeps_original_clinical_table_and_list_summary():
    assessed = _assessment(historical=True)
    blocks = _diseases(assessed)["blocks"]
    for block in blocks:
        if block["title"] == "本章小结":
            block["title"] = "2.5 本章小结"
    original = deepcopy(blocks)
    result = complete_disease_content(blocks, product_feedback=True)
    assert next(block for block in result if block.get("columns") == CLINICAL_COLUMNS) == next(
        block for block in blocks if block.get("columns") == CLINICAL_COLUMNS)
    summary = [block for block in result if block.get("title") == "2.5 本章小结"]
    assert [block["kind"] for block in summary] == ["paragraph", "list"]
    assert summary[1]["items"] == next(block["items"] for block in blocks if block["title"] == "2.5 本章小结")
    assert blocks == original
