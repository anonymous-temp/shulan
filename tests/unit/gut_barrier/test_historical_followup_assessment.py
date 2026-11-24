"""Historical review expands coverage, never disease probabilities or scores."""
from copy import deepcopy

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment, attention_score
from modules.gut_barrier.schemas import GutBarrierRequest


# Dates and imaging wording are synthetic; their clinical relationships are intentional.
def _assessment(*, history=True, severe=False, extra=None):
    request = {
        "userId": "history-rule-test", "sessionId": "synthetic", "age": 31, "sex": "男",
        "gutHealthIndicator": {key: {"value": value, "unit": unit}
            for key, value, unit in zip(("dLacticAcid", "diamineOxidase", "bacterialEndotoxin", "hsCRP"),
                (35, 25, 40, 2.35) if severe else (20.88, 0.5, 2.4, 2.35), ("mg/L", "U/L", "U/L", "mg/L"))},
        "hospitalData": [{"dataType": "EXAM", "checkTime": "2024-05-09", "itemName": "CT全腹平扫+增强",
                          "examResult": "结肠肝曲见结节样强化灶，考虑间质瘤可能。双肾见小结石。"}] if history else [],
        **(extra or {}),
    }
    return assess_case(GutBarrierRequest.model_validate(request), context={"currentDate": "2026-10-06"})


def test_synthetic_fact_pattern_adds_two_sourced_qualitative_rows_without_reweighting_existing_barrier_row():
    original = _assessment()
    before = deepcopy(original)
    actual = apply_dynamic_assessment(original)
    baseline = apply_dynamic_assessment(_assessment(history=False))
    rows = {row["code"]: row for row in actual["diseaseRisks"]}
    base = {row["code"]: row for row in baseline["diseaseRisks"]}
    assert len(rows) == 3 and len(base) == 1
    assert {code: row["riskIndex"] for code, row in base.items()} == {
        "intestinal_barrier_followup": 3}
    for code in base:
        assert rows[code] == base[code]
        assert "（关注方向）" not in rows[code]["name"]
    for code, refs in (("digestive_lesion_followup", ["Q36"]), ("urinary_stone_followup", ["Q37"])):
        row = rows[code]
        assert row["riskIndex"] is row["probability"] is row["attention"] is None
        assert row["status"] == "qualitative_followup" and row["riskLevel"] == "随访关注"
        assert row["qualitative"]["evidenceIds"] == refs
        assert row["qualitative"]["sourceFindings"][0]["date"] == "2024-05-09"
        assert row["qualitative"]["sourceFindings"][0]["temporalStatus"] == "historical"
    assert "考虑间质瘤可能" in rows["digestive_lesion_followup"]["qualitative"]["basis"]
    topics = actual["diseaseAssessment"]["selection"]["topicPlan"]
    planned = {code for topic in topics if topic["eligibility"] == "supported_direction" for code in topic["diseaseCodes"]}
    assert rows.keys() <= planned
    assert original == before
    assert apply_dynamic_assessment(actual) == actual


def test_severe_markers_change_related_attention_but_never_raise_unrelated_vascular_score():
    mild = {row["code"]: row for row in apply_dynamic_assessment(_assessment(history=False))["diseaseRisks"]}
    severe = {row["code"]: row for row in apply_dynamic_assessment(_assessment(history=False, severe=True))["diseaseRisks"]}
    assert severe["intestinal_barrier_followup"]["riskIndex"] == 5
    assert mild["intestinal_barrier_followup"]["riskIndex"] == 3
    assert set(severe) == set(mild) == {"intestinal_barrier_followup"}
    assert len(severe["intestinal_barrier_followup"]["attention"]["facts"]) == 1


def test_urinary_review_basis_uses_the_attributed_excerpt_without_repeating_its_label():
    case = _assessment(extra={"hospitalData": [
        {"dataType": "EXAM", "checkTime": "2024-05-09", "itemName": "CT全腹", "examResult": "双肾见小结石。"},
        {"dataType": "INSPECTION", "checkTime": "2022-08-11 11:30:00", "itemName": "尿胆原", "testResultValue": "正常"},
        {"dataType": "INSPECTION", "checkTime": "2022-08-11 11:30:00", "itemName": "隐血", "testResultValue": "3+", "resultStatus": 0},
    ]})
    row = next(row for row in apply_dynamic_assessment(case)["diseaseRisks"] if row["code"] == "urinary_stone_followup")
    assert "2022-08-11 隐血：3+" in row["qualitative"]["basis"]
    assert "隐血：隐血" not in row["qualitative"]["basis"]
    assert row["riskIndex"] is None


def test_priority_band_remains_attainable_from_independent_supported_factors_not_sales_or_citation_count():
    facts = [dict(domain="barrier", strength=5, longitudinal=0, sourceType="objective_current", location="synthetic"),
             dict(domain="metabolic", strength=3, longitudinal=0, sourceType="objective_current", location="synthetic"),
             dict(domain="lipids", strength=3, longitudinal=0, sourceType="objective_current", location="synthetic")]
    actual = attention_score(facts, guideline_support=True)
    assert actual["score"] == 7 and actual["label"] == "优先评估"
    assert attention_score(facts + facts, guideline_support=True) == actual
    assert attention_score(facts, guideline_support=2) == actual
    assert actual["validated"] is False


@pytest.mark.parametrize("data", [
    {"hospitalData": []},
    {"hospitalData": [{"dataType": "EXAM", "checkTime": "2024-05-09", "itemName": "CT全腹",
                       "examResult": "未见消化道结节；两肾未见结石。"}]},
])
def test_absent_or_negative_history_does_not_fill_a_disease_catalog(data):
    actual = apply_dynamic_assessment(_assessment(extra=data))
    assert {row["code"] for row in actual["diseaseRisks"]} == {
        "intestinal_barrier_followup"}
