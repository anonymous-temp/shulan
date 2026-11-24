"""v2.7 acceptance, clinical provenance, and frozen HTTP callback tests.

These verify calculation and provenance, not clinical calibration or probability.
"""
from copy import deepcopy
from decimal import Decimal
import json

import pytest
import tornado.testing

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment
from modules.gut_barrier.risk_index_rules import (
    apply_risk_index_rules, confirmed_rule_results,
)
from modules.gut_barrier.risk_model import build_disease_risks
from modules.gut_barrier.schemas import GutBarrierRequest
from tests.unit.gut_barrier import test_api as api_fixture
from tests.unit.gut_barrier.template_risk_assertions import assert_template_risk_contract
from modules.gut_barrier.risk_grading import high_quota, grade_items, mapped_score, separate_scores
from modules.gut_barrier.risk_index_rules import VERSION


def _allocation(ibfc, raw=None, personal=None):
    catalog, _ = build_disease_risks()
    rows = [{"code": r["code"], "systemCode": r["systemCode"],
             "components": {"individualFactors": (personal or {}).get(r["code"], 0)}} for r in catalog]
    scores = {r["code"]: Decimal(str((raw or {}).get(r["code"], 1))) for r in catalog}
    audit = grade_items(rows, ibfc, scores)
    return {r["code"]: r for r in rows}, audit


# Independent acceptance table, including both sides of every boundary.
IBFC_COUNTS = [
    (0, 0, 0, 23), ("0.000001", 2, 2, 19), ("0.999999", 2, 2, 19),
    (1, 2, 3, 18), ("1.999999", 2, 3, 18),
    (2, 2, 4, 17), ("2.499999", 2, 4, 17),
    ("2.5", 2, 7, 14), ("2.999999", 2, 7, 14),
    (3, 3, 7, 13), ("3.999999", 3, 7, 13),
    (4, 4, 7, 12), ("4.5", 4, 7, 12),
    ("4.500001", 5, 8, 10), ("4.999999", 5, 8, 10),
    (5, 6, 8, 9), ("5.999999", 6, 8, 9),
    (6, 7, 8, 8), ("6.5", 7, 8, 8),
    ("6.500001", 8, 11, 4), ("6.999999", 8, 11, 4),
    (7, 10, 9, 4), ("7.999999", 10, 9, 4),
    (8, 12, 8, 3), ("8.999999", 12, 8, 3),
    (9, 14, 7, 2), ("9.999999", 14, 7, 2),
    (10, 16, 6, 1), ("10.000001", 18, 5, 0),
    ("14.999999", 18, 5, 0), (15, 23, 0, 0),
]


def _counts(rows):
    return tuple(sum(row["riskLevel"] == level for row in rows.values()) for level in ("高", "中", "低"))


@pytest.mark.parametrize("ibfc,high,medium,low", IBFC_COUNTS)
def test_every_ibfc_boundary_has_the_confirmed_exact_counts(ibfc, high, medium, low):
    rows, audit = _allocation(ibfc)
    assert high_quota(ibfc) == audit["highQuota"] == high
    assert len(audit["highCodes"]) == high
    assert audit["mediumQuota"] == len(audit["mediumCodes"]) == medium
    assert len(audit["lowCodes"]) == low
    assert _counts(rows) == (high, medium, low)
    assert len(rows) == len({r["riskIndex"] for r in rows.values()}) == 23
    assert not (set(audit["highCodes"]) & set(audit["mediumCodes"]))
    assert set(audit["highCodes"] + audit["mediumCodes"] + audit["lowCodes"]) == set(rows)


@pytest.mark.parametrize("ibfc,high,medium,low", IBFC_COUNTS)
@pytest.mark.parametrize("raw_pattern", ["catalog_ties", "reverse_catalog", "outside_system_dominates"])
def test_system_requirements_and_exact_counts_hold_for_adversarial_raw_order(ibfc, high, medium, low, raw_pattern):
    catalog = build_disease_risks()[0]
    raw = ({r["code"]: index + 1 for index, r in enumerate(catalog)}
           if raw_pattern == "reverse_catalog" else {})
    if raw_pattern == "outside_system_dominates":
        raw = {"lung_cancer": 200, "chronic_kidney_disease": 100, "anxiety": 90}
    rows, audit = _allocation(ibfc, raw)
    value = Decimal(str(ibfc))

    def count(system, levels):
        return sum(row["systemCode"] == system and row["riskLevel"] in levels for row in rows.values())

    assert _counts(rows) == (high, medium, low)
    if value == 0:
        assert {row["riskLevel"] for row in rows.values()} == {"低"}
        return
    assert count("digestive", {"高"}) >= 1
    assert count("metabolic", {"高"}) >= 1
    if value < Decimal("2.5"):
        assert count("digestive", {"高"}) == count("metabolic", {"高"}) == 1
    elif value <= Decimal("4.5"):
        assert count("cardiovascular", {"中"}) >= 3
        assert count("neurological", {"中"}) == 2
    else:
        assert count("digestive", {"中", "高"}) >= 4
        assert count("metabolic", {"中", "高"}) >= 3
        assert count("cardiovascular", {"高"}) >= 1
        assert count("neurological", {"高"}) >= 1
    if value > Decimal("6.5"):
        for system in ("oncology", "immune", "psychiatric"):
            assert count(system, {"高"}) >= 1
            assert count(system, {"中", "高"}) >= 2
            if value <= 10:
                assert count(system, {"中"}) >= 1
    if value >= 15:
        assert {row["riskLevel"] for row in rows.values()} == {"高"}
        assert not audit["mediumCodes"] and not audit["lowCodes"]


@pytest.mark.parametrize("seed", range(12))
def test_rising_ibfc_never_reduces_high_or_non_low_counts_despite_clinical_factors(seed):
    import random

    rng = random.Random(seed)
    catalog = build_disease_risks()[0]
    raw = {row["code"]: Decimal(rng.randrange(1, 120)) / 10 for row in catalog}
    personal = {row["code"]: rng.randrange(5) for row in catalog}
    previous = (0, 0)
    for ibfc, high, medium, low in IBFC_COUNTS:
        rows, _ = _allocation(ibfc, raw, personal)
        assert _counts(rows) == (high, medium, low)
        assert high >= previous[0] and high + medium >= previous[1]
        previous = (high, high + medium)


@pytest.mark.parametrize("ibfc", ["0.000001", 1, 2, "2.499999", "2.5", "2.500001", 15])
def test_positive_ibfc_never_leaves_an_eligible_catalog_all_low(ibfc):
    rows, audit = _allocation(ibfc)
    assert any(row["riskLevel"] == "高" for row in rows.values())
    assert len(audit["highCodes"]) >= 2


def test_zero_normal_panel_is_all_low_without_inflating_measurements_weights_or_raw():
    original = _assessment((0, 0, 0, 0))
    scored = apply_risk_index_rules(original)
    ledger = scored["riskRuleAssessment"]
    assert ledger["allocation"]["highCodes"] == []
    assert ledger["allocation"]["mediumCodes"] == []
    assert {row["riskLevel"] for row in ledger["items"]} == {"低"}
    assert all(row["rawScore"] == 1 for row in ledger["items"])
    assert len({row["riskIndex"] for row in ledger["items"]}) == 23
    assert scored["markers"] == original["markers"]
    assert scored["barrier"] == original["barrier"]


@pytest.mark.parametrize("crp", [0, .5, .9, 1, 2.999999, "<3"])
def test_zero_ibfc_below_high_crp_threshold_stays_all_low(crp):
    scored = apply_risk_index_rules(_assessment((0, 0, 0, crp)))
    assert _counts(_items(scored)) == (0, 0, 23)
    assert scored["riskRuleAssessment"]["allocation"]["ibfcBand"] == "zero"




def test_low_ibfc_selects_highest_raw_within_each_required_system():
    rows, audit = _allocation(1, {"ibs": 2, "obesity": 3})
    assert audit["highCodes"] == ["obesity", "ibs"]
    assert audit["mediumCodes"] == ["ibd", "intestinal_barrier_followup", "nafld"]
    assert rows["obesity"]["riskIndex"] == 6.8 and rows["ibs"]["riskIndex"] == 6.7


@pytest.mark.parametrize("markers,expected", [
    ((15, 0, 0), (2, 3, 18)),       # IBFC 1
    ((20, 0, 0), (2, 4, 17)),       # IBFC 2
    ((0, 11, 0), (2, 7, 14)),       # IBFC 2.5
    ((22, 0, 0), (3, 7, 13)),       # IBFC 3
    ((30, 0, 0), (4, 7, 12)),       # IBFC 4
    ((14, 16, 0), (4, 7, 12)),      # IBFC 4.5
    ((0, 20, 0), (6, 8, 9)),        # IBFC 5
    ((0, 0, 35), (7, 8, 8)),        # IBFC 6
    ((0, 16, 22), (7, 8, 8)),       # IBFC 6.5
    ((14, 0, 35), (10, 9, 4)),      # IBFC 7
    ((17, 0, 35), (12, 8, 3)),      # IBFC 8
    ((22, 0, 35), (14, 7, 2)),      # IBFC 9
    ((30, 0, 35), (16, 6, 1)),      # IBFC 10
    ((14, 16, 35), (18, 5, 0)),     # IBFC 10.5
    ((30, 20, 35), (23, 0, 0)),     # IBFC 15
])
def test_same_positive_ibfc_ignores_crp_for_scores_and_grades(markers, expected):
    low_crp = apply_risk_index_rules(_assessment((*markers, 0)))
    before = _items(low_crp)
    for crp in (.5, .999999, 1, 2.999999, 3, 9.999999, 10):
        raised_crp = apply_risk_index_rules(_assessment((*markers, crp)))
        assert low_crp["barrier"] == raised_crp["barrier"]
        after = _items(raised_crp)
        assert {code: row["riskLevel"] for code, row in before.items()} == {
            code: row["riskLevel"] for code, row in after.items()}
        assert _counts(before) == _counts(after) == expected
        assert {code: row["rankingScore"] for code, row in before.items()} == {
            code: row["rankingScore"] for code, row in after.items()}
        assert before == after


@pytest.mark.parametrize("raw", [1, 8, 100])
def test_maximum_ibfc_supports_23_distinct_high_scores(raw):
    catalog = build_disease_risks()[0]
    rows, audit = _allocation(15, {r["code"]: raw for r in catalog})
    assert len(audit["highCodes"]) == 23 and not audit["mediumCodes"] and not audit["lowCodes"]
    scores = [Decimal(str(rows[code]["riskIndex"])) for code in audit["highCodes"]]
    assert all(Decimal("6.7") <= value <= Decimal("9.9") for value in scores)
    assert all(a - b == Decimal(".1") for a, b in zip(scores, scores[1:]))


@pytest.mark.parametrize("level,raw,expected", [
    ("高", 1, "6.7"), ("高", 3, "6.7"), ("高", 4, "7.3"), ("高", 8, "9.9"), ("高", 100, "9.9"),
    ("中", 1, "3.4"), ("中", 1.5, "3.4"), ("中", 4, "5.4"), ("中", 5.5, "6.6"),
    ("低", 1, "1.0"), ("低", 2.2, "3.3"), ("低", 50, "3.3"),
    ("高", "3.078125", "6.8"),
])
def test_band_formulas_and_half_up_without_early_raw_cap(level, raw, expected):
    assert mapped_score(raw, level) == Decimal(expected)


def test_downward_ties_and_floor_lift_are_exact_and_deterministic():
    assert separate_scores([8, 8, 8], "高") == list(map(Decimal, ["9.9", "9.8", "9.7"]))
    assert separate_scores([1, 1, 1], "中") == list(map(Decimal, ["3.6", "3.5", "3.4"]))
    assert separate_scores([1] * 23, "低") == [Decimal("3.2") - Decimal("0.1") * n for n in range(23)]
    with pytest.raises(ValueError):
        separate_scores([1] * 25, "低")


def test_higher_raw_in_same_system_wins_and_catalog_breaks_ties():
    rows, audit = _allocation(1, {"ibs": 5, "ibd": 5, "nafld": 4})
    assert audit["highCodes"] == ["ibd", "type2_diabetes"]
    assert audit["mediumCodes"] == ["ibs", "nafld", "intestinal_barrier_followup"]
    assert rows["ibd"]["riskIndex"] == 8.0


def test_sorting_precision_preserves_small_business_raw_differences():
    _, audit = _allocation(1, {"ibd": "5", "ibs": "5.000000000001"})
    assert audit["highCodes"] == ["ibs", "type2_diabetes"]
    assert audit["mediumCodes"] == ["ibd", "intestinal_barrier_followup", "nafld"]


def test_sorting_ignores_decimal_context_tail_without_changing_original_raw():
    rows = [{"code": row["code"], "systemCode": row["systemCode"],
             "components": {"individualFactors": 0}} for row in build_disease_risks()[0]]
    raw = {row["code"]: Decimal("5") for row in rows}
    raw["ibs"] = Decimal("5.000000000000000000000000001")
    original = raw.copy()
    audit = grade_items(rows, 1, raw)
    assert audit["highCodes"] == ["ibd", "type2_diabetes"]
    assert audit["mediumCodes"][:2] == ["intestinal_barrier_followup", "nafld"]
    assert raw == original


def test_equal_raw_from_actual_marker_calculation_uses_catalog_order():
    scored = apply_risk_index_rules(_assessment((0, 0, 0, .6)))
    ledger = scored["riskRuleAssessment"]
    equal_raw_codes = ["cirrhosis", "obesity", "parkinsons", "chronic_kidney_disease"]
    assert [code for code in ledger["allocation"]["lowCodes"]
            if code in equal_raw_codes] == equal_raw_codes
    items = _items(scored)
    assert [items[code]["rawScore"] for code in equal_raw_codes] == [1] * 4
    assert [items[code]["riskIndex"] for code in equal_raw_codes] == [2.8, 2.6, 1.8, 1.3]


def test_neurological_high_selection_uses_raw_without_overriding_total_quota():
    rows, audit = _allocation(9, {"alzheimers": 100, "parkinsons": 99})
    assert _counts(rows) == (14, 7, 2)
    assert rows["alzheimers"]["riskLevel"] == rows["parkinsons"]["riskLevel"] == "高"
    assert rows["alzheimers"]["riskIndex"] == 9.9 and rows["parkinsons"]["riskIndex"] == 9.8


@pytest.mark.parametrize("ibfc,high,medium,low", IBFC_COUNTS)
def test_clinical_and_supported_system_factors_cannot_expand_fixed_counts(ibfc, high, medium, low):
    codes = [row["code"] for row in build_disease_risks()[0]]
    raw = {code: 5 for code in codes}
    without_personal, _ = _allocation(ibfc, raw)
    with_personal, audit = _allocation(ibfc, raw, {code: 4 for code in codes})
    # P is already represented in raw. There is no second grade-only promotion.
    assert with_personal == {code: {**row, "components": {"individualFactors": 4}}
                             for code, row in without_personal.items()}
    assert _counts(with_personal) == (high, medium, low)
    assert audit["mediumQuota"] == medium
    assert not any(event["reason"] in {"clinical_P_ge_2", "system_raw_ge_3"}
                   for event in audit["floorEvents"])


def test_questionnaire_answers_cannot_change_allocation_or_scores():
    original = apply_risk_index_rules(_assessment((15, 0, 0, 0)))
    with_history = apply_risk_index_rules(_assessment((15, 0, 0, 0), survey={"pastDisease": "肥胖"}))
    before, after = _items(original), _items(with_history)
    assert _counts(before) == _counts(after) == (2, 3, 18)
    assert before == after
    assert before["obesity"]["components"]["individualFactors"] == 0


def test_new_ledger_preserves_input_and_recomputes_without_historical_maximum():
    original = _assessment((15, 10.99, 30, "<0.5"))
    before = deepcopy(original)
    scored = apply_risk_index_rules(original)
    assert original == before
    assert scored["riskRuleAssessment"]["version"] == VERSION
    assert "stateFloor" not in scored["riskRuleAssessment"]
    assert apply_risk_index_rules(scored) == scored
    # Explicit v2.7 band reset, not a newly introduced monotonicity rule.
    next_case = _assessment((15, 11, 30, "<0.5"))
    next_case["riskRuleAssessment"] = scored["riskRuleAssessment"]
    second = apply_risk_index_rules(next_case)
    assert _items(scored)["ibs"]["rawScore"] == pytest.approx(5.392)
    assert _items(second)["ibs"]["rawScore"] == 3.4
    assert _items(scored)["ibs"]["riskLevel"] == _items(second)["ibs"]["riskLevel"] == "高"
    assert _items(second)["ibs"]["riskIndex"] < _items(scored)["ibs"]["riskIndex"]
    assert second == apply_risk_index_rules(_assessment((15, 11, 30, "<0.5")))


@pytest.mark.parametrize("path,value", [
    (("allocation", "highQuota"), 99), (("allocation", "highCodes"), []),
    (("allocation", "systemMinimumNonLow"), {}), (("allocation", "ibfcBand"), "below_2.5"),
    (("allocation", "minimumLowCount"), 99),
    (("items", 0, "rankingScore"), 99),
    (("items", 0, "weights"), [0, 0]), (("items", 0, "riskIndex"), 10.0),
    (("items", 0, "rawScore"), 100), (("items", 0, "components", "organFactor"), 1),
    (("continuousFactors",), {}), (("barrierBasis", "grade"), "normal"),
])
def test_publication_recomputes_all_new_fields_and_rejects_tampering(path, value):
    result = apply_risk_index_rules(_assessment((24, 18, 25, 1.5)))
    assert len(confirmed_rule_results(result)) == 23
    target = result["riskRuleAssessment"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert confirmed_rule_results(result) == {}


@pytest.mark.parametrize("values,age,context", [
    ((None, 1, 1, .3), 42, {}), ((1, 1, 1, .3), 10, {}),
    ((1, 1, 1, 3), 42, {"acuteInfection": True}),
])
def test_ineligible_panels_never_receive_quota_or_made_up_scores(values, age, context):
    scored = apply_risk_index_rules(_assessment(values, age=age, context=context))
    assert scored["riskRuleAssessment"]["allocation"] is None
    assert all(row["riskIndex"] is None for row in _items(scored).values())
    assert not confirmed_rule_results(scored)


def _payload(values=(20.88, .5, 2.4, 2.35), *, age=42, survey=None):
    markers = (("dLacticAcid", "mg/L"), ("diamineOxidase", "U/L"),
               ("bacterialEndotoxin", "U/L"), ("hsCRP", "mg/L"))
    return {
        "userId": "synthetic-v27-user", "sessionId": "synthetic-v27-session",
        "name": "合成等级评分测试样本", "age": age, "sex": "男",
        "healthSurvey": deepcopy(survey),
        "gutHealthIndicator": {field: {"value": value, "unit": unit}
                               for (field, unit), value in zip(markers, values) if value is not None},
    }


def _assessment(values=(20.88, .5, 2.4, 2.35), *, age=42, survey=None, context=None):
    request = GutBarrierRequest.model_validate(_payload(values, age=age, survey=survey))
    result = apply_dynamic_assessment(assess_case(
        request, context={"currentDate": "2026-10-07", **(context or {})},
    ))
    return result


def _items(assessment):
    return {item["code"]: item for item in assessment["riskRuleAssessment"]["items"]}


@pytest.mark.parametrize("survey", [None, {}, {"digestion": None, "bowelMovement": None,
                                               "pastDisease": None, "familyDisease": None}])
def test_absent_clinical_answers_remain_missing_rather_than_explicitly_negative(survey):
    result = apply_risk_index_rules(_assessment((1, 1, 1, .3), survey=survey))
    completeness = result["riskRuleAssessment"]["clinicalCompleteness"]
    assert completeness["status"] == "incomplete"
    assert set(completeness["missingDomains"]) == {"lipids", "diagnosis"}
    assert all(domain["status"] == "missing" for domain in completeness["domains"])


def test_explicit_negative_questionnaire_does_not_complete_clinical_domains():
    result = apply_risk_index_rules(_assessment((1, 1, 1, .3), survey={
        "heightCm": "160", "weightKg": "55", "digestion": "无", "bowelMovement": "无",
        "fourHigh": "无", "pastDisease": "无", "familyDisease": "无",
    }))
    completeness = result["riskRuleAssessment"]["clinicalCompleteness"]
    domains = {row["code"]: row["status"] for row in completeness["domains"]}
    assert set(domains) == {"lipids", "diagnosis"}
    assert set(domains.values()) == {"missing"}
    assert set(completeness["missingDomains"]) == {"lipids", "diagnosis"}
    assert completeness["status"] == "incomplete" and completeness["basicAttention"] is True
    assert all(not row["factors"] for row in _items(result).values())


@pytest.mark.parametrize("crp", ["<1", "<=3", ">0"])
def test_range_with_normal_minimum_and_unknown_higher_tier_is_not_a_confirmed_normal_panel(crp):
    result = apply_risk_index_rules(_assessment((1, 1, 1, crp)))
    completeness = result["riskRuleAssessment"]["clinicalCompleteness"]
    assert completeness["status"] == "incomplete"
    assert completeness["basicAttention"] is True
    assert result["riskRuleAssessment"]["allocation"]["highCodes"] == []


@pytest.mark.parametrize("changes", [{"unit": "unconfirmed"}, {"value": "-1"}, {"value": "NaN"}])
def test_a_normal_lipid_label_cannot_complete_an_invalid_measurement_domain(changes):
    from tests.unit.gut_barrier.test_risk_index_rules import _lipid, _rules

    result = _rules(hospital=[_lipid(value=changes.get("value", "2.0"), status="正常",
                                    unit=changes.get("unit", "mmol/L"))])
    completeness = result["riskRuleAssessment"]["clinicalCompleteness"]
    assert "lipids" in completeness["missingDomains"]
    assert next(row for row in completeness["domains"] if row["code"] == "lipids")["status"] == "missing"
    assert not _items(result)["hyperlipidemia"]["factors"]


def test_contradictory_survey_answer_is_not_counted_as_completed_negative_history():
    result = apply_risk_index_rules(_assessment((1, 1, 1, .3), survey={
        "fourHigh": "无", "pastDisease": "无┋肥胖", "familyDisease": "无",
    }))
    completeness = result["riskRuleAssessment"]["clinicalCompleteness"]
    assert next(row for row in completeness["domains"] if row["code"] == "diagnosis")["status"] == "missing"
    assert "diagnosis" in completeness["missingDomains"]
    assert not _items(result)["obesity"]["factors"]


def test_accepted_sources_can_complete_all_clinical_domains_without_requiring_an_optional_model():
    from tests.unit.gut_barrier.test_risk_index_rules import _lipid, _rules, TODAY

    result = _rules(hospital=[_lipid(value="2.0", status="正常"), {
        "dataType": "EXAM", "itemName": "电子胃镜", "checkTime": TODAY,
        "examResult": "慢性非萎缩性胃炎",
    }])
    completeness = result["riskRuleAssessment"]["clinicalCompleteness"]
    assert completeness["status"] == "complete"
    assert completeness["missingDomains"] == [] and completeness["basicAttention"] is False
    assert "gastritis_followup" not in _items(result)
    assert any(row["code"] == "gastritis_followup" for row in result["diseaseRisks"])


@pytest.mark.parametrize("age,values", [(101, (1, 1, 1, .3)), (42, (None, 1, 1, .3))])
def test_rule_ineligibility_does_not_erase_available_body_measurements(age, values):
    result = apply_risk_index_rules(_assessment(values, age=age, survey={"heightCm": "160", "weightKg": "55"}, context={"bmi": 21.5}))
    ledger = result["riskRuleAssessment"]
    assert ledger["status"] == "not_evaluated"
    assert all(row["riskIndex"] is None for row in ledger["items"])
    assert result["context"]["bmi"] == 21.5
    assert "bmi" not in ledger["clinicalCompleteness"]["missingDomains"]


def test_new_normal_lipid_batch_supersedes_conflicting_older_results_for_completeness():
    from tests.unit.gut_barrier.test_risk_index_rules import _assessment as assessment_with_hospital, _lipid

    original = assessment_with_hospital(hospital=[
        _lipid("2026-09-07", value="4.1", status="偏高"),
        _lipid("2026-09-07", value="2.0", status="正常"),
        _lipid(value="2.0", status="正常"),
    ])
    result = apply_risk_index_rules(original)
    completeness = result["riskRuleAssessment"]["clinicalCompleteness"]
    assert next(row for row in completeness["domains"] if row["code"] == "lipids")["status"] == "available"
    assert "lipids" not in completeness["missingDomains"]
    assert not _items(result)["hyperlipidemia"]["factors"]
    assert _items(result)["hyperlipidemia"]["components"]["individualFactors"] == 0


@pytest.mark.parametrize("day,expected,source_type", [
    ("2026-10-07", 4.0, "objective_current"),
    ("2026-09-07", 2.5, "objective_historical"),
])
def test_current_and_historical_diagnoses_keep_P_without_double_counting_self_report(day, expected, source_type):
    from tests.unit.gut_barrier.test_risk_index_rules import _rules

    diagnosis = {"dataType": "EXAM", "itemName": "门诊病历", "checkTime": day, "examResult": "确诊肝硬化。"}
    result = _rules(survey={"pastDisease": "肝硬化"}, hospital=[diagnosis, deepcopy(diagnosis)])
    row = _items(result)["cirrhosis"]
    assert row["components"]["individualFactors"] == expected - 1
    assert sum(f["contribution"] for f in row["factors"]) == expected - 1
    assert len(row["factors"]) == 1
    assert row["factors"][0]["domain"] == "condition_history"
    assert row["factors"][0]["sourceType"] == source_type


@pytest.mark.parametrize("survey,expected", [
    ({"pastDisease": "高血压"}, 1.0),
    ({"familyDisease": "父亲高血压"}, 1.0),
    ({"pastDisease": "高血压", "familyDisease": "父亲高血压"}, 1.0),
])
def test_self_report_and_family_history_have_no_clinical_contributions(survey, expected):
    result = apply_risk_index_rules(_assessment((1, 1, 1, .3), survey=survey))
    row = _items(result)["hypertension"]
    assert row["components"]["individualFactors"] == expected - 1
    assert sum(f["contribution"] for f in row["factors"]) == expected - 1


class TestRiskGradingHTTP(tornado.testing.AsyncHTTPTestCase):
    get_app = api_fixture.TestGutBarrierAPI.get_app
    post = api_fixture.TestGutBarrierAPI.post

    def tearDown(self):
        self.io_loop.run_sync(self.transport.aclose)
        super().tearDown()

    @tornado.testing.gen_test
    async def test_real_callback_uses_final_scores_and_replays_identical_frozen_bytes(self):
        self.service.callback_url = self.get_url("/test-callback")
        self._app.settings["receiver_ready"] = True
        payload = _payload()
        response = await self.post("/api/v1/gut-barrier/report", payload)
        assert response.code == 200
        receipt = json.loads(response.body)["data"]
        await self.supervisor.wait_idle()
        assert len(self._app.settings["received"]) == 1
        key, body = self._app.settings["received"][0]
        frozen_bytes = self._app.settings["received_bytes"][0]
        assert body["status"] in {"completed", "partial"} and body["error"] is None
        assert set(body) == {"type", "schemaVersion", "finalReportId", "userId", "sessionId", "status", "report", "error"}
        assert body["schemaVersion"] == "2.0" and body["type"] == "gutBarrier"
        saved = self.collection.documents[receipt["finalReportId"]]
        assessment = saved["audit"]["reportDocument"]["assessment"]
        by_name, chart, tables = assert_template_risk_contract(body["report"], assessment)
        assert len(chart["rows"]) == 23
        assert sum(row[3] == "高" for row in chart["rows"]) == 2
        assert sum(row[3] == "中" for row in chart["rows"]) == 4
        assert len({row[2] for row in chart["rows"]}) == 23
        chapter = next(section for section in body["report"]["sections"] if section["id"] == "disease_risks")
        assert not any(block["title"] in {"状态下限说明", "风险指数说明", "风险评估资料提示"}
                       for block in chapter["blocks"])
        audit_items = {item["code"]: item for item in assessment["riskRuleAssessment"]["items"]}
        assert audit_items["ibd"]["rawScore"] == pytest.approx(3.352)
        assert audit_items["ibd"]["riskLevel"] == "高"
        assert audit_items["ibd"]["riskIndex"] == 6.9
        assert "stateFloor" not in audit_items["ibd"]
        assert audit_items["ibd"]["weights"] == [4]
        assert all(len(row) == 4 for table in tables for row in table["rows"])
        assert "riskRuleAssessment" not in json.dumps(body["report"], ensure_ascii=False)
        assert saved["generationAttempts"] == 1 and saved["deliveryStatus"] == "delivered"

        repeated = await self.post("/api/v1/gut-barrier/report", payload)
        assert repeated.code == 200 and json.loads(repeated.body)["data"] == receipt
        await self.supervisor.wait_idle()
        assert self.pipeline.generate.await_count == 1
        assert self._app.settings["received"] == [(key, body), (key, body)]
        assert self._app.settings["received_bytes"] == [frozen_bytes, frozen_bytes]


