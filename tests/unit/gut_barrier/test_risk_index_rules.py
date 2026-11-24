"""Acceptance tests for the trial relative-risk rubric, using synthetic cases.

These assert explicit software rules and input provenance. They do not assert
clinical calibration, population probabilities, or a desired sales distribution.
"""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment
from modules.gut_barrier.risk_index_rules import (
    RISK_PROFILES, apply_risk_index_rules, confirmed_rule_result, risk_level,
)
from modules.gut_barrier.schemas import GutBarrierRequest


TODAY = "2026-10-07"
CATALOG_CODES = {
    "nafld", "ibs", "ibd", "cirrhosis", "type2_diabetes", "obesity",
    "hyperlipidemia", "hyperuricemia", "atherosclerosis",
    "coronary_heart_disease", "hypertension", "stroke", "alzheimers",
    "parkinsons", "depression", "anxiety", "rheumatoid_arthritis",
    "systemic_lupus", "chronic_kidney_disease", "colorectal_cancer",
    "liver_cancer", "lung_cancer",
    "intestinal_barrier_followup",
}
MARKERS = (
    ("dLacticAcid", "D_LACTATE", "mg/L"),
    ("diamineOxidase", "DAO", "U/L"),
    ("bacterialEndotoxin", "LPS", "U/L"),
    ("hsCRP", "HS_CRP", "mg/L"),
)


def _payload(*, values=(1, 1, 1, .3), age=42, survey=None, hospital=()):
    return {
        "userId": "synthetic-rubric-user", "sessionId": "synthetic-rubric-session",
        "name": "合成评分测试样本", "age": age, "sex": "男",
        "healthSurvey": deepcopy(survey), "hospitalData": deepcopy(list(hospital)),
        "gutHealthIndicator": {
            field: {"value": value, "unit": unit}
            for (field, _, unit), value in zip(MARKERS, values)
        },
    }


def _assessment(payload=None, *, observations=None, context=None, **kwargs):
    request = GutBarrierRequest.model_validate(payload or _payload(**kwargs))
    return apply_dynamic_assessment(assess_case(
        request, observations=observations,
        context={"currentDate": TODAY, **(context or {})},
    ))


def _rules(**kwargs):
    return apply_risk_index_rules(_assessment(**kwargs))


def _items(assessment):
    return {item["code"]: item for item in assessment["riskRuleAssessment"]["items"]}


def _scores(assessment):
    return {code: (item["riskIndex"], item["riskLevel"])
            for code, item in _items(assessment).items()}


def _base_components(item):
    return item["components"]


def _lipid(day=TODAY, *, name="低密度脂蛋白(LDL-c)", value="4.1", status="偏高", unit="mmol/L"):
    return {"dataType": "INSPECTION", "checkTime": day, "itemName": name,
            "testResultValue": value, "unit": unit, "resultStatusName": status}


def test_complete_normal_panel_scores_all_23_with_confirmed_system_grades_and_distinct_scores():
    result = _rules(values=(0, 0, 0, 0))
    ledger = result["riskRuleAssessment"]
    assert set(_items(result)) == CATALOG_CODES
    assert ledger["status"] == "evaluated"
    assert ledger["validated"] is False
    assert ledger["panelCoverage"] == {"available": 3, "required": 3}
    assert ledger["allocation"]["highCodes"] == []
    assert ledger["allocation"]["mediumCodes"] == []
    assert _scores(result)["ibd"] == (3.2, "低")
    assert sum(level == "低" for _, level in _scores(result).values()) == 23
    assert sorted(score for score, _ in _scores(result).values()) == [i / 10 for i in range(10, 33)]
    assert all(item["components"] == {
        "baseline": 1, "barrier": 0.0, "individualFactors": 0.0,
        "organFactor": 0.0, "organContribution": 0.0,
    } and item["factors"] == [] and item["reasons"] == [] for item in ledger["items"])
    assert len(ledger["inputs"]) == 3
    assert all(row["source"] for row in ledger["inputs"])


def test_valid_zero_measurements_are_retained_and_are_not_missing_data():
    result = _rules(values=(0, 0, 0, 0))
    assert result["riskRuleAssessment"]["status"] == "evaluated"
    assert [item["value"] for item in result["riskRuleAssessment"]["inputs"]] == [0.0] * 3
    assert _scores(result)["ibd"] == (3.2, "低")
    assert sum(level == "低" for _, level in _scores(result).values()) == 23
    assert len({score for score, _ in _scores(result).values()}) == 23
    assert all(row["rawScore"] == 1.0 for row in _items(result).values())


def test_all_maximum_markers_use_one_barrier_domain_and_disease_specific_weights():
    result = _rules(values=(35, 30, 80, 8))
    rows = _items(result)
    # IBD raw = baseline 1 + grade V * WB 4 + 2*p(D)=7.
    # The fixed disease weight is not the maximum of the patient's markers.
    assert rows["ibd"]["riskIndex"] == 9.3
    assert rows["ibd"]["rawScore"] == 7
    assert rows["ibd"]["riskLevel"] == "高"
    assert rows["ibd"]["components"] == {
        "baseline": 1, "barrier": 4.0, "individualFactors": 0.0,
        "organFactor": 1.0, "organContribution": 2.0,
    }
    assert rows["nafld"]["rawScore"] == 6
    assert rows["hyperlipidemia"]["rawScore"] == pytest.approx(1 + 1.8 + 2 * .5)
    assert rows["lung_cancer"]["rawScore"] == 1.3
    assert _scores(result)["intestinal_barrier_followup"] == (9.2, "高")
    assert "metabolic_barrier_followup" not in rows
    assert {item["riskLevel"] for item in rows.values()} == {"高"}


def test_extension_followups_preserve_sources_without_expanding_the_23_scores():
    result = _rules(values=(35, 30, 80, 8), survey={
        "heightCm": "160", "weightKg": "90", "bowelMovement": "水样腹泻4周",
        "digestion": "消化不良", "skin": "痤疮",
    }, hospital=[
        {"dataType": "EXAM", "itemName": "电子胃镜", "checkTime": TODAY,
         "examResult": "慢性非萎缩性胃炎"},
        {"dataType": "EXAM", "itemName": "CT全腹平扫+增强", "checkTime": TODAY,
         "examResult": "结肠肝曲见结节样强化灶，考虑间质瘤可能。双肾见小结石。"},
        {"dataType": "INSPECTION", "itemName": "eGFR", "checkTime": TODAY,
         "testResultValue": "59", "unit": "mL/min/1.73m²"},
    ])
    expected_extensions = {
        "intestinal_barrier_followup", "kidney_function_evaluation",
        "gastritis_followup", "digestive_lesion_followup", "urinary_stone_followup",
    }
    assert set(_items(result)) == CATALOG_CODES
    assert expected_extensions <= {row["code"] for row in result["diseaseRisks"]}
    assert result["riskRuleAssessment"]["status"] == "evaluated"
    assert all(item["status"] == "evaluated" and 1 <= item["riskIndex"] <= 10
               and item["riskLevel"] in {"低", "中", "高"} for item in _items(result).values())
    assert set(_items(_rules())) == CATALOG_CODES


def test_supplied_marker_pattern_uses_continuous_factors_and_system_quotas():
    result = _rules(values=(12.77, 24.9, 11.9, 1.02), survey={
        "heightCm": "160", "weightKg": "90",
        "bowelMovement": "时常腹泻", "digestion": "消化不良",
    })
    scores = _scores(result)
    assert result["barrier"]["score"] == 5.0
    assert result["barrier"]["grade"] == "III"
    rows = _items(result)
    assert rows["ibs"]["rawScore"] == pytest.approx(1 + 3 * .6 + 2)
    assert scores["ibs"][1] == "高"
    assert rows["type2_diabetes"]["rawScore"] == pytest.approx(1 + 2.2 * .6 + 2 * .3)
    assert rows["obesity"]["components"]["individualFactors"] == 0
    assert "dyspepsia_evaluation" not in scores
    assert sum(level == "高" for _, level in scores.values()) == 6
    without_survey = _items(_rules(values=(12.77, 24.9, 11.9, 1.02)))
    assert rows == without_survey


def test_synthetic_recent_low_dao_lps_pattern_uses_low_ibfc_system_grades_and_unchanged_raw():
    result = _rules(values=(20.88, .5, 2.4, 2.35))
    assert result["barrier"]["score"] == 2.0
    assert result["barrier"]["grade"] == "I"
    assert _items(result)["ibd"]["rawScore"] == pytest.approx(1 + 4 * .2 + 2 * ((20.88 - 17) / 5))
    assert _scores(result)["ibd"] == (6.9, "高")
    assert result["riskRuleAssessment"]["allocation"]["mediumCodes"] == [
        "cirrhosis", "colorectal_cancer", "intestinal_barrier_followup", "nafld",
    ]
    assert sum(level == "低" for _, level in _scores(result).values()) == 17
    assert all(1 <= score <= 10 for score, _ in _scores(result).values())
    assert result["riskRuleAssessment"]["allocation"]["highCodes"] == ["ibd", "type2_diabetes"]


@pytest.mark.parametrize("values,score,grade,strength", [
    ((1, 1, 1, .3), 0.0, "normal", 0.0),
    ((15, 1, 1, .3), 1.0, "I", .2),
    ((1, 13, 1, .3), 2.5, "II", .4),
    ((1, 26, 1, .3), 5.0, "III", .6),
    ((1, 18, 25, .3), 6.5, "IV", .8),
    ((24, 18, 25, .3), 9.5, "V", 1.0),
])
def test_every_confirmed_ibfc_grade_sets_one_barrier_contribution_per_disease(
    values, score, grade, strength,
):
    result = _rules(values=values)
    assert result["barrier"]["score"] == score
    assert result["barrier"]["grade"] == grade
    assert result["riskRuleAssessment"]["status"] == "evaluated"
    assert result["riskRuleAssessment"]["barrierBasis"] == {
        "score": score, "grade": grade, "normalizedSeverity": strength,
    }
    for code, row in _items(result).items():
        # The first parameter is the declared IBFC weight, retained from the
        # previous fixed barrier domain maximum, not a patient's marker MAX.
        disease_weight = RISK_PROFILES[code].barrier_weight
        assert _base_components(row)["barrier"] == pytest.approx(strength * disease_weight)
        assert row["components"]["barrier"] == pytest.approx(strength * row["weights"][0])
        assert "inflammation" not in row["components"]
        assert row["components"]["individualFactors"] == 0.0


@pytest.mark.parametrize("values,score", [
    ((24, 1, 1, .3), 3.0),
    ((1, 13, 1, .3), 2.5),
    ((1, 1, 25, .3), 3.0),
])
def test_different_marker_combinations_with_the_same_ibfc_grade_have_the_same_barrier_effect(values, score):
    result = _rules(values=values)
    reference = _rules(values=(24, 1, 1, .3))
    assert result["barrier"]["score"] == score
    assert result["barrier"]["grade"] == reference["barrier"]["grade"] == "II"
    for code in CATALOG_CODES:
        assert _items(result)[code]["components"]["barrier"] == _items(reference)[code]["components"]["barrier"]
    p_d = .25 if values[0] == 24 else .2 / 14
    assert _items(result)["ibd"]["rawScore"] == pytest.approx(1 + 4 * .4 + 2 * p_d)
    if values[0] != 24:
        assert _items(result)["ibd"]["rawScore"] != _items(reference)["ibd"]["rawScore"]


def test_an_added_abnormal_marker_can_raise_ibfc_contribution_without_changing_the_previous_main_marker():
    dao_only = _rules(values=(1, 18, 1, .3))
    dao_and_lps = _rules(values=(1, 18, 25, .3))
    assert dao_only["barrier"]["score"] == 3.5
    assert dao_only["barrier"]["grade"] == "II"
    assert dao_and_lps["barrier"]["score"] == 6.5
    assert dao_and_lps["barrier"]["grade"] == "IV"
    assert _base_components(_items(dao_only)["ibd"])["barrier"] == 1.6
    assert _items(dao_only)["ibd"]["components"]["barrier"] == 1.6
    assert _items(dao_and_lps)["ibd"]["components"]["barrier"] == 3.2
    assert _items(dao_only)["ibd"]["rawScore"] == pytest.approx(2.6 + 2 * (.2 / 14))
    assert _items(dao_and_lps)["ibd"]["rawScore"] - _items(dao_only)["ibd"]["rawScore"] == pytest.approx(1.6)
    assert sum(row["riskLevel"] == "高" for row in _items(dao_only).values()) == 3
    assert sum(row["riskLevel"] == "高" for row in _items(dao_and_lps).values()) == 7


@pytest.mark.parametrize("values,ibfc,grade,raws,ibd_level", [
    ((12.24, 17.25, 22.25, "<0.5"), 6.5, "IV", (4.2 + 2 * (12.24 / 14 * .2), 4.025, 3.4 + 1.25 / 4 + .25 / 6), "高"),
    ((7.18, 11.77, 15.04, 2.2), 2.5, "II", (2.6 + 2 * (7.18 / 14 * .2), 2.2 + 2 * (.77 / 5), 2.2 + .77 / 5 + 15.04 / 18 * .2), "中"),
])
def test_supplied_pdf_panels_are_reconstructed_with_ibfc_grades_without_inventing_individual_factors(
    values, ibfc, grade, raws, ibd_level,
):
    result = _rules(values=values, age=28)
    assert result["barrier"]["score"] == ibfc
    assert result["barrier"]["grade"] == grade
    assert [_items(result)[code]["rawScore"] for code in ("ibd", "ibs", "nafld")] == pytest.approx(raws)
    assert _scores(result)["ibd"][1] == ibd_level
    assert all(row["components"]["individualFactors"] == 0.0 and row["factors"] == []
               for row in _items(result).values())
    assert confirmed_rule_result(result, "ibd") == _scores(result)["ibd"]


def test_confirmed_four_point_five_ibfc_uses_grade_two_for_risk_scoring():
    result = _rules(values=(1, 1, 32, .3))
    assert result["barrier"]["score"] == 4.5
    assert result["barrier"]["grade"] == "II"
    assert "grade_endpoint_requires_medical_confirmation" not in result["barrier"]["reasons"]
    assert result["riskRuleAssessment"]["status"] == "evaluated"
    assert result["riskRuleAssessment"]["barrierBasis"] == {
        "score": 4.5, "grade": "II", "normalizedSeverity": .4,
    }
    assert _items(result)["ibd"]["rawScore"] == pytest.approx(2.6 + .4 / 14)
    assert sum(level == "高" for _, level in _scores(result).values()) == 4
    assert confirmed_rule_result(result, "ibd") == _scores(result)["ibd"]


@pytest.mark.parametrize("change", [
    {"score": 2.5}, {"score": None}, {"score": True},
    {"grade": "II"}, {"grade": None}, {"grade": "synthetic-unknown-grade"},
    {"score": 2.5, "grade": "II"},
])
def test_risk_scoring_rejects_a_barrier_score_or_grade_that_disagrees_with_the_actual_panel(change):
    assessment = _assessment(values=(1, 18, 25, .3))
    assert assessment["barrier"]["score"] == 6.5
    assert assessment["barrier"]["grade"] == "IV"
    assessment["barrier"].update(change)
    before = deepcopy(assessment)
    result = apply_risk_index_rules(assessment)
    assert assessment == before
    assert result["riskRuleAssessment"]["status"] == "not_evaluated"
    assert result["riskRuleAssessment"]["reasons"]
    assert all(row["riskIndex"] is None and row["riskLevel"] is None for row in _items(result).values())
    assert confirmed_rule_result(result, "ibd") is None


@pytest.mark.parametrize("preserve_total", [False, True])
def test_risk_scoring_independently_rescores_marker_values_before_trusting_a_matching_derived_ibfc(preserve_total):
    assessment = _assessment(values=(1, 18, 25, .3))
    markers = {row["code"]: row for row in assessment["markers"]}
    assert markers["DAO"]["score"] == 3.5
    assert markers["LPS"]["score"] == 3.0
    markers["DAO"].update(score=5, level="重度")
    if preserve_total:
        # Both fake scores remain individually within their permitted maxima,
        # and the IBFC sum and grade are unchanged. Only independent rescoring
        # of the accepted values can reject this substituted score composition.
        markers["LPS"].update(score=1.5, level="亚健康")
    else:
        assessment["barrier"].update(score=8.0, grade="IV")
    assert sum(row["score"] for code, row in markers.items() if code != "HS_CRP") == assessment["barrier"]["score"]
    result = apply_risk_index_rules(assessment)
    assert result["riskRuleAssessment"]["status"] == "not_evaluated"
    assert result["riskRuleAssessment"]["reasons"]
    assert all(row["riskIndex"] is None and row["riskLevel"] is None for row in _items(result).values())
    assert confirmed_rule_result(result, "ibd") is None


@pytest.mark.parametrize("index,values", [
    (0, [1, 15, 20, 24, 35]), (1, [1, 9, 13, 18, 26]),
    (2, [1, 20, 25, 32, 40]), (3, [.3, .7, 2, 5]),
])
def test_accepted_marker_severity_keeps_monotonic_barrier_contribution(index, values):
    previous = None
    for value in values:
        panel = [1, 1, 1, .3]
        panel[index] = value
        result = _rules(values=panel)
        # v2.7 p resets at a tier boundary; raw/final scores need not be monotone.
        # The preserved B contribution remains monotone; CRP has no effect.
        current = {code: row["components"]["barrier"]
                   for code, row in _items(result).items()}
        assert result["barrier"]["grade"] is not None
        assert result["riskRuleAssessment"]["status"] == "evaluated"
        if previous is not None:
            assert all(current[code] >= previous[code] for code in CATALOG_CODES)
        previous = current


@pytest.mark.parametrize("score,level", [(1, "低"), (3.3, "低"), (3.35, "中"), (3.4, "中"),
                                        (6.6, "中"), (6.65, "高"), (6.7, "高"), (10, "高")])
def test_displayed_score_boundaries(score, level):
    assert risk_level(score) == level


@pytest.mark.parametrize("score", [None, True, False, "6.7", 0, -1, 10.1, float("inf"), float("nan")])
def test_invalid_displayed_scores_have_no_grade(score):
    assert risk_level(score) is None


@pytest.mark.parametrize("field,code,unit", MARKERS[:3])
def test_missing_marker_does_not_use_a_baseline_as_a_completed_risk_score(field, code, unit):
    payload = _payload()
    del payload["gutHealthIndicator"][field]
    result = _rules(payload=payload)
    assert result["riskRuleAssessment"]["status"] == "not_evaluated"
    assert f"{code}:measurement_requires_review" in result["riskRuleAssessment"]["reasons"]
    assert result["riskRuleAssessment"]["panelCoverage"] == {"available": 2, "required": 3}
    assert all(item["riskIndex"] is None and item["riskLevel"] is None
               and item["components"] is None and item["reasons"] for item in _items(result).values())
    assert confirmed_rule_result(result, "nafld") is None


@pytest.mark.parametrize("field,code,unit", MARKERS[:3])
def test_barrier_inequality_results_never_become_exact_marker_scores(field, code, unit):
    payload = _payload()
    payload["gutHealthIndicator"][field] = {"value": ">3.0", "unit": unit}
    result = _rules(payload=payload)
    assert f"{code}:measurement_requires_review" in result["riskRuleAssessment"]["reasons"]
    assert all(item["riskIndex"] is None for item in _items(result).values())














def test_range_hscrp_does_not_supply_an_ibfc_score_for_unknown_barrier_values():
    result = _rules(values=(">3", 1, 1, ">3"))
    assert "D_LACTATE:measurement_requires_review" in result["riskRuleAssessment"]["reasons"]
    assert result["barrier"]["score"] is None
    assert all(row["riskIndex"] is None for row in _items(result).values())




@pytest.mark.parametrize("field,code,unit", MARKERS[:3])
def test_invalid_measurement_units_block_the_rubric_with_marker_specific_reasons(field, code, unit):
    payload = _payload()
    payload["gutHealthIndicator"][field]["unit"] = "unconfirmed-unit"
    result = _rules(payload=payload)
    assert f"{code}:measurement_requires_review" in result["riskRuleAssessment"]["reasons"]
    assert all(item["riskIndex"] is None for item in _items(result).values())








@pytest.mark.parametrize("field,code,unit", MARKERS[:3])
def test_conflicting_current_sources_stop_the_full_panel_rubric(field, code, unit):
    result = _rules(observations=[{
        "code": code, "value": 9, "unit": unit, "isCurrent": True,
        "source": {"kind": "synthetic-observation", "id": "conflicting-value"},
    }])
    assert f"{code}:measurement_requires_review" in result["riskRuleAssessment"]["reasons"]
    assert all(item["riskIndex"] is None for item in _items(result).values())


@pytest.mark.parametrize("age", [None, 0, 17, 101])
def test_age_outside_trial_adult_range_does_not_generate_scores(age):
    result = _rules(age=age)
    assert "adult_age_required" in result["riskRuleAssessment"]["reasons"]
    assert not any(item["riskIndex"] is not None for item in _items(result).values())


@pytest.mark.parametrize("age", [18, 100])
def test_trial_adult_age_endpoints_can_use_a_complete_panel(age):
    assert _rules(age=age)["riskRuleAssessment"]["status"] == "evaluated"




def test_individual_factors_are_capped_without_discarding_their_provenance():
    result = _rules(survey={"lifestyle": "吸烟", "pastDisease": "动脉粥样硬化",
                            "familyDisease": "父亲动脉粥样硬化"}, hospital=[_lipid(), {
        "dataType": "EXAM", "itemName": "门诊病历", "checkTime": TODAY, "examResult": "确诊冠心病。",
    }])
    row = _items(result)["coronary_heart_disease"]
    assert sum(factor["contribution"] for factor in row["factors"]) == 5.0
    assert row["components"]["individualFactors"] == 4.0
    assert row["rawScore"] == 5  # 1 + P4; B=0.
    # Zero IBFC stays all low; P is retained, not an extra quota.
    assert row["riskLevel"] == "低"
    assert row["rankingScore"] == 5
    assert {factor["domain"] for factor in row["factors"]} == {
        "lipids", "condition_history",
    }


def test_combined_severe_panel_and_related_factors_never_exceed_ten():
    result = _rules(values=(35, 30, 80, 8), context={"bmi": 40}, survey={
        "bowelMovement": "便血", "pastDisease": "炎症性肠病", "familyDisease": "炎症性肠病",
    })
    row = _items(result)["obesity"]
    assert row["components"] == {
        "baseline": 1, "barrier": 2.0, "individualFactors": 0,
        "organFactor": 4.5, "organContribution": 9.0,
    }
    assert row["rawScore"] == 12  # raw is intentionally not capped at ten.
    assert row["riskIndex"] == 9.9
    assert row["riskLevel"] == "高"
    assert all(item["riskIndex"] <= 10 for item in _items(result).values())


def test_duplicate_lipids_and_repeated_symptoms_do_not_raise_a_domain_contribution():
    one = _rules(survey={"bowelMovement": "时常腹泻"}, hospital=[_lipid()])
    repeated = _rules(survey={"bowelMovement": "时常腹泻┋时常腹泻", "digestion": "时常腹泻"},
                      hospital=[_lipid(), _lipid(), _lipid(name="总胆固醇(TC)", value="6.8")])
    assert _scores(one) == _scores(repeated)
    assert _items(repeated)["ibs"]["factors"] == []
    assert len(_items(repeated)["atherosclerosis"]["factors"]) == 1
    lipids = _items(repeated)["hyperlipidemia"]
    assert lipids["factors"] == []
    assert len(lipids["excludedFactors"]) == 1
    assert lipids["components"]["organContribution"] == 6  # 2*(3*1 + .4*.6).


@pytest.mark.parametrize("survey", [
    {"bowelMovement": "无腹泻", "digestion": "没有消化不良"},
    {"bowelMovement": "既往腹泻，现已好转"},
    {"lifestyle": "不吸烟"}, {"lifestyle": "戒烟"},
    {"fourHigh": "无高血脂", "pastDisease": "没有肥胖", "familyDisease": "无高血压"},
    {"bowelMovement": "无┋腹泻"},
])
def test_negative_resolved_and_conflicting_survey_answers_do_not_add_points(survey):
    result = _rules(survey=survey)
    assert _scores(result) == _scores(_rules())
    assert all(item["components"]["individualFactors"] == 0 and item["factors"] == [] for item in _items(result).values())


@pytest.mark.parametrize("survey", [
    {"pastDisease": "未患高血压"}, {"pastDisease": "高血压已排除"},
    {"pastDisease": "高血压未确诊"}, {"pastDisease": "疑似高血压"},
    {"pastDisease": "高血压待查"}, {"pastDisease": "高血压尚不能排除"},
    {"pastDisease": "如患高血压"}, {"pastDisease": "示例：高血压"},
    {"pastDisease": "父亲高血压"}, {"pastDisease": "家族史：高血压"},
    {"fourHigh": "母亲高血压"},
    {"lifestyle": "从未吸烟"}, {"lifestyle": "尚未吸烟"},
    {"lifestyle": "父亲吸烟"},
    {"lifestyle": "吸烟史：无"}, {"lifestyle": "已停止吸烟"},
    {"lifestyle": "吸烟已停止"}, {"lifestyle": "曾经吸烟"},
    {"familyDisease": "父亲未患高血压"}, {"familyDisease": "父亲疑似高血压"},
    {"familyDisease": "无高血压家族史"}, {"familyDisease": "本人高血压"},
])
def test_negative_uncertain_or_wrong_subject_history_and_smoking_do_not_add_points(survey):
    result = _rules(survey=survey)
    assert _scores(result) == _scores(_rules())
    assert all(item["components"]["individualFactors"] == 0 and item["factors"] == [] for item in _items(result).values())


def test_questionnaire_personal_and_family_history_do_not_contribute():
    original = _items(_rules())["hypertension"]
    personal = _items(_rules(survey={"pastDisease": "高血压"}))["hypertension"]
    family = _items(_rules(survey={"familyDisease": "父亲高血压"}))["hypertension"]
    together = _items(_rules(survey={"pastDisease": "高血压", "familyDisease": "父亲高血压"}))["hypertension"]
    assert personal == family == together == original
    assert original["components"]["individualFactors"] == 0
    assert original["factors"] == []


def test_latest_normal_lipid_result_overrides_older_high_result():
    old = _rules(hospital=[_lipid("2026-09-07")])
    corrected = _rules(hospital=[_lipid("2026-09-07"), _lipid(value="2.0", status="正常")])
    assert _items(old)["hyperlipidemia"]["rawScore"] == 4  # 1 + 2*(3*.5).
    assert _scores(corrected) == _scores(_rules())
    assert _items(corrected)["hyperlipidemia"]["factors"] == []
    assert _items(corrected)["atherosclerosis"]["factors"] == []


def test_historical_lipid_strength_is_half_current_strength():
    current = _items(_rules(hospital=[_lipid()]))["hyperlipidemia"]
    historical = _items(_rules(hospital=[_lipid("2026-09-07")]))["hyperlipidemia"]
    assert current["components"]["organContribution"] == 6
    assert historical["components"]["organContribution"] == 3
    assert current["components"]["individualFactors"] == historical["components"]["individualFactors"] == 0
    assert current["excludedFactors"][0]["sourceType"] == "objective_current"
    assert historical["excludedFactors"][0]["sourceType"] == "objective_historical"


@pytest.mark.parametrize("days,expected_strength,expected_raw", [
    (0, 1.0, 7), (1, .5, 4), (365, .5, 4), (366, 0.0, 1),
])
def test_lipid_date_cutoffs_are_inclusive_only_through_day_365(days, expected_strength, expected_raw):
    from datetime import date, timedelta
    day = (date.fromisoformat(TODAY) - timedelta(days=days)).isoformat()
    result = _rules(hospital=[_lipid(day)])
    row = _items(result)["hyperlipidemia"]
    # B=0; raw = 1 + 2*(3*strength).
    assert row["rawScore"] == expected_raw
    assert row["components"]["individualFactors"] == 0
    assert result["riskRuleAssessment"]["continuousFactors"]["lipids"]["strength"] == expected_strength
    if days <= 365:
        assert len(row["excludedFactors"]) == 1
        assert row["excludedFactors"][0]["source"]["date"] == day
    else:
        assert row["excludedFactors"] == []
        assert _scores(result) == _scores(_rules())


@pytest.mark.parametrize("changes", [
    {"dataType": "EXAM"}, {"dataType": "unsupported-type"},
    {"status": "needs_review"}, {"status": "conflict"},
    {"conflictingResults": True}, {"conflict": True},
])
def test_source_qualified_lipid_ledger_still_requires_an_accepted_inspection_state(changes):
    assessment = _assessment(hospital=[_lipid()])
    finding = assessment["context"]["clinicalFindings"][0]
    assert finding["code"] == "LDL_C"
    finding.update(changes)
    result = apply_risk_index_rules(assessment)
    assert _scores(result) == _scores(_rules())
    assert _items(result)["hyperlipidemia"]["factors"] == []


def test_same_day_conflicting_lipid_aliases_do_not_select_the_high_value():
    result = _rules(hospital=[
        _lipid(), _lipid(name="低密度脂蛋白胆固醇(LDL-C)", value="2.0", status="正常"),
    ])
    assert _scores(result) == _scores(_rules())
    assert _items(result)["hyperlipidemia"]["factors"] == []


@pytest.mark.parametrize("day,contribution,source_type", [
    (TODAY, 3.0, "objective_current"),
    ("2026-09-30", 1.5, "objective_historical"),
])
def test_renal_factor_respects_current_historical_strength_and_deduplicates_measurements(day, contribution, source_type):
    lab = {"dataType": "INSPECTION", "itemName": "eGFR", "checkTime": day,
           "testResultValue": "59", "unit": "mL/min/1.73m²"}
    single = _rules(hospital=[lab])
    repeated = _rules(hospital=[lab, deepcopy(lab), {
        "dataType": "INSPECTION", "itemName": "UACR", "checkTime": day,
        "testResultValue": "40", "unit": "mg/g",
    }])
    assert _scores(single) == _scores(repeated)
    row = _items(repeated)["chronic_kidney_disease"]
    assert row["rawScore"] == 2 + contribution  # GFR 59 gives R=.5, 2*R=1.
    assert row["components"]["individualFactors"] == contribution
    assert row["components"]["organContribution"] == 1
    assert len(row["factors"]) == 1
    assert row["factors"][0]["domain"] == "renal_observation"
    assert row["factors"][0]["sourceType"] == source_type
    assert row["factors"][0]["source"]
    assert "kidney_function_evaluation" not in _items(repeated)
    assert any(item["code"] == "kidney_function_evaluation" for item in repeated["diseaseRisks"])


@pytest.mark.parametrize("code,item_name,text,current_strength", [
    ("gastritis_followup", "电子胃镜", "慢性非萎缩性胃炎", 2.0),
    ("digestive_lesion_followup", "CT全腹平扫+增强", "结肠肝曲见结节样强化灶，考虑间质瘤可能。", 2.0),
    ("urinary_stone_followup", "CT全腹平扫+增强", "双肾见小结石。", 2.0),
])
def test_dated_historical_examination_factors_are_half_current_strength(code, item_name, text, current_strength):
    from modules.gut_barrier.dynamic_diseases import _signals
    from modules.gut_barrier.risk_index_rules import _factors
    exam = {"dataType": "EXAM", "itemName": item_name, "examResult": text}
    current = _rules(hospital=[{**exam, "checkTime": TODAY}])
    historical = _rules(hospital=[{**exam, "checkTime": "2024-05-09"}])
    # Keep the accepted clinical source logic; these followup items are no
    # longer scored or allowed to consume the 23-item quotas.
    for result, strength, source_type in ((current, current_strength, "objective_current"),
                                           (historical, current_strength / 2, "objective_historical")):
        factors = _factors(result, _signals(result), code, None)
        assert sum(factor["contribution"] for factor in factors) == strength
        assert factors[0]["sourceType"] == source_type
        assert factors[0]["source"]
        assert code not in _items(result)
        assert any(item["code"] == code for item in result["diseaseRisks"])


@pytest.mark.parametrize("hospital", [
    [_lipid("2025-09-07")], [_lipid("2026-11-07")], [_lipid(None)],
    [_lipid(status="未见升高")], [_lipid(unit="unconfirmed-unit")],
    [_lipid(), _lipid(value="2.0", status="正常")],
])
def test_ineligible_lipid_records_never_add_risk_points(hospital):
    result = _rules(hospital=hospital)
    assert _scores(result) == _scores(_rules())
    assert _items(result)["hyperlipidemia"]["factors"] == []


def test_rubric_preserves_original_input_existing_model_and_all_clinical_data():
    payload = _payload(values=(35, 30, 80, 8), survey={"bowelMovement": "时常腹泻"})
    raw_before = deepcopy(payload)
    original = _assessment(payload)
    original["riskModel"] = {"modelId": "synthetic-approved-model", "modelVersion": "test-v1",
                             "status": "evaluated", "items": []}
    original["diseaseRisks"][0].update(status="evaluated", riskIndex=9.2, riskLevel="TEST_HIGH")
    before = deepcopy(original)
    result = apply_risk_index_rules(original)
    assert original == before
    assert payload == raw_before
    assert {key: value for key, value in result.items() if key != "riskRuleAssessment"} == before
    assert result is not original
    result["riskRuleAssessment"]["inputs"][0]["source"][0]["source"]["path"] = "changed-copy"
    assert original == before


def test_rule_generation_is_idempotent_and_replaces_stale_internal_ledger():
    original = _assessment()
    original["riskRuleAssessment"] = {"status": "evaluated", "items": [{"code": "nafld", "riskIndex": 10}]}
    result = apply_risk_index_rules(original)
    assert _scores(result) == _scores(_rules())
    assert apply_risk_index_rules(result) == result


def test_unknown_dynamic_disease_does_not_receive_an_invented_generic_profile():
    assessment = _assessment()
    assessment["diseaseRisks"].append({"code": "synthetic-unknown", "name": "Synthetic unsupported disease"})
    result = apply_risk_index_rules(assessment)
    assert result["riskRuleAssessment"]["status"] == "evaluated"
    assert "synthetic-unknown" not in _items(result)
    assert result["diseaseRisks"] == assessment["diseaseRisks"]
    assert confirmed_rule_result(result, "synthetic-unknown") is None
    assert _scores(result) == _scores(_rules())
    assert confirmed_rule_result(result, "nafld") == _scores(result)["nafld"]


def test_client_cannot_supply_a_rule_ledger_at_the_public_request_level():
    payload = _payload()
    payload["riskRuleAssessment"] = {"status": "evaluated", "items": [{"code": "lung_cancer", "riskIndex": 10}]}
    with pytest.raises(ValidationError, match="riskRuleAssessment"):
        GutBarrierRequest.model_validate(payload)


def test_free_form_source_cannot_smuggle_a_rule_ledger_or_public_score():
    payload = _payload(survey={"riskRuleAssessment": {"status": "evaluated", "items": [
        {"code": "lung_cancer", "riskIndex": 10, "riskLevel": "高"},
    ]}})
    payload["hospitalData"] = [{"riskRuleAssessment": payload["healthSurvey"]["riskRuleAssessment"],
                                "riskIndex": 10, "riskLevel": "高"}]
    result = _rules(payload=payload)
    assert _scores(result)["lung_cancer"] == (1.0, "低")
    assert confirmed_rule_result(result, "lung_cancer") == (1.0, "低")


@pytest.mark.parametrize("markers_state", ["absent", "empty"])
def test_transplanted_complete_rule_ledger_without_actual_markers_is_not_publishable(markers_state):
    result = _rules(values=(35, 30, 80, 8))
    assert confirmed_rule_result(result, "ibd") == (9.3, "高")
    # This is a structurally complete, internally consistent ledger. It must
    # still be reconciled with the accepted patient's actual four-marker panel.
    assert result["riskRuleAssessment"]["status"] == "evaluated"
    assert result["riskRuleAssessment"]["panelCoverage"] == {"available": 3, "required": 3}
    if markers_state == "absent":
        del result["markers"]
    else:
        result["markers"] = []
    assert all(confirmed_rule_result(result, code) is None for code in CATALOG_CODES)


@pytest.mark.parametrize("change", [
    {"riskIndex": 10.0}, {"riskLevel": "高"}, {"riskIndex": True},
    {"weights": [4, 4, 4, 4]}, {"status": "not_evaluated"},
    {"reasons": ["synthetic-unresolved-source"]},
    {"components": {"baseline": 1, "barrier": 8, "inflammation": 0, "individualFactors": 0}},
])
def test_publication_rejects_tampered_or_unfinished_rule_entries(change):
    result = _rules()
    _items(result)["nafld"].update(change)
    assert confirmed_rule_result(result, "nafld") is None


def test_publication_rejects_duplicate_entries_and_incomplete_panel_audit():
    result = _rules()
    result["riskRuleAssessment"]["items"].append(deepcopy(_items(result)["nafld"]))
    assert confirmed_rule_result(result, "nafld") is None
    result = _rules()
    result["riskRuleAssessment"]["panelCoverage"]["available"] = 2
    assert confirmed_rule_result(result, "nafld") is None


@pytest.mark.parametrize('name_status,fallback', [(None, 'H'), ('', 'high'), (None, '偏高'), ('↑', None)])
def test_lipid_status_fields_have_identical_scoring_and_completeness(name_status, fallback):
    control = _rules(hospital=[_lipid()])
    source = _lipid(status=name_status)
    source['resultStatus'] = fallback
    result = _rules(hospital=[source])
    assert _items(result) == _items(control)
    assert result['riskRuleAssessment']['clinicalCompleteness'] == control['riskRuleAssessment']['clinicalCompleteness']
    assert {row['code'] for row in result['diseaseRisks']} == {row['code'] for row in control['diseaseRisks']}


@pytest.mark.parametrize('field', ['unit', 'resultStatusName', 'resultStatus'])
@pytest.mark.parametrize('invalid', [[], {}, ['H'], {'value': 'H'}])
def test_malformed_auxiliary_lipid_fields_are_isolated_without_blocking_report(field, invalid):
    malformed = _lipid()
    if field == 'resultStatus':
        malformed.pop('resultStatusName')
    malformed[field] = invalid
    result = _rules(hospital=[malformed])
    assert result['riskRuleAssessment']['status'] == 'evaluated'
    assert len(_items(result)) == 23
    assert result['riskRuleAssessment']['continuousFactors']['lipids']['strength'] == 0
    assert _scores(result) == _scores(_rules())
    valid = _lipid(name='总胆固醇(TC)', value='6.8')
    alongside = _rules(hospital=[malformed, valid])
    assert alongside['riskRuleAssessment']['continuousFactors']['lipids']['strength'] == 1
    assert _scores(alongside) == _scores(_rules(hospital=[valid]))


@pytest.mark.parametrize('suffix', ['。未见肺部感染。', '；家族史：无。', '，复核后排除肺部感染。'])
def test_unrelated_clinical_text_does_not_remove_confirmed_diagnosis_points(suffix):
    source = {'dataType': 'EXAM', 'itemName': '门诊病历', 'checkTime': TODAY,
              'examResult': '确诊：冠心病'}
    base = _rules(hospital=[source])
    variant = _rules(hospital=[{**source, 'examResult': source['examResult'] + suffix}])
    assert _scores(variant) == _scores(base)
    assert _items(variant)['coronary_heart_disease']['components']['individualFactors'] == 3


def test_scaled_egfr_is_identical_for_clinical_points_and_continuous_renal_factor():
    source = {'dataType': 'INSPECTION', 'itemName': 'eGFR', 'checkTime': TODAY,
              'testResultValue': '54', 'unit': 'mL/min/1.73m²'}
    base = _rules(hospital=[source])
    scaled = _rules(hospital=[{**source, 'testResultValue': '.9', 'unit': 'mL/s/1.73m²'}])
    assert _scores(scaled) == _scores(base)
    assert _items(scaled)['chronic_kidney_disease']['components'] == _items(base)['chronic_kidney_disease']['components']
    assert _items(scaled)['chronic_kidney_disease']['components']['individualFactors'] == 3


@pytest.mark.parametrize('reverse', [False, True])
def test_same_name_same_day_equivalent_gfr_keeps_both_sources_without_conflict(reverse):
    normal = {'dataType': 'INSPECTION', 'itemName': 'eGFR', 'checkTime': TODAY,
              'testResultValue': '54', 'unit': 'mL/min/1.73m²'}
    scaled = {**normal, 'testResultValue': '0.9', 'unit': 'mL/s/1.73m²'}
    records = [normal, scaled]
    if reverse:
        records.reverse()
    result = _rules(hospital=records)
    assert _scores(result) == _scores(_rules(hospital=[normal]))
    assert result['riskRuleAssessment']['continuousFactors']['kidney']['factor'] == .5
    finding = result['context']['clinicalFindings'][0]
    assert not finding.get('conflictingResults')
    assert finding['value'] == records[0]['testResultValue']
    assert finding['unit'] == records[0]['unit']
    assert len(finding['source']) == 2
    assert finding['alternatives'][0]['value'] == records[1]['testResultValue']
    assert finding['alternatives'][0]['unit'] == records[1]['unit']
    assert _items(result)['chronic_kidney_disease']['components']['individualFactors'] == 3


def test_same_name_non_equivalent_gfr_keeps_conflict_after_unit_normalization():
    normal = {'dataType': 'INSPECTION', 'itemName': 'eGFR', 'checkTime': TODAY,
              'testResultValue': '54', 'unit': 'mL/min/1.73m²'}
    result = _rules(hospital=[normal, {**normal, 'testResultValue': '1.5', 'unit': 'mL/s/1.73m²'}])
    assert result['context']['clinicalFindings'][0]['conflictingResults'] is True
    assert result['riskRuleAssessment']['continuousFactors']['kidney']['factor'] == 0
    assert _items(result)['chronic_kidney_disease']['components']['individualFactors'] == 0
