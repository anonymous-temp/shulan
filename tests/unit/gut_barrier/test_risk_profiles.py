"""Fixed disease-code profiles describe existing trial weights, not diagnoses.

These tests preserve every v2.7 coefficient. A profile cannot be selected
from the patient's most abnormal marker or a requested risk distribution.
"""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from modules.gut_barrier import risk_index_rules as rules
from tests.unit.gut_barrier.test_risk_index_rules import _assessment, _base_components, _payload


_EXPECTED_WEIGHTS = {
    'nafld': (3,),
    'ibs': (3,),
    'ibd': (4,),
    'cirrhosis': (1.5,),
    'type2_diabetes': (2.2,),
    'obesity': (2,),
    'hyperlipidemia': (1.8,),
    'hyperuricemia': (1.2,),
    'atherosclerosis': (1,),
    'coronary_heart_disease': (1,),
    'hypertension': (0.5,),
    'stroke': (0.5,),
    'alzheimers': (0.5,),
    'parkinsons': (0.5,),
    'depression': (1,),
    'anxiety': (1,),
    'rheumatoid_arthritis': (1,),
    'systemic_lupus': (1,),
    'chronic_kidney_disease': (1,),
    'colorectal_cancer': (0.5,),
    'liver_cancer': (0.8,),
    'lung_cancer': (0.3,),
    'intestinal_barrier_followup': (4,),
}


def _all_profile_assessment(**kwargs):
    assessment = _assessment(**kwargs)
    present = {row["code"] for row in assessment["diseaseRisks"]}
    assessment["diseaseRisks"].extend(
        {"code": code, "name": f"Technical profile fixture: {code}"}
        for code in _EXPECTED_WEIGHTS if code not in present
    )
    return assessment


def _items(assessment):
    return {row["code"]: row for row in assessment["riskRuleAssessment"]["items"]}


def _result(**kwargs):
    return rules.apply_risk_index_rules(_all_profile_assessment(**kwargs))


def test_profile_catalog_covers_exactly_all_23_v27_rules_and_current_identity():
    assert len(_EXPECTED_WEIGHTS) == 23
    assert set(rules.RISK_PROFILES) == set(_EXPECTED_WEIGHTS)
    assert rules.VERSION == "gut-barrier-relative-risk-v2.7.20261010-clinical-source-fixes"
    assert {code: profile.weights for code, profile in rules.RISK_PROFILES.items()} == _EXPECTED_WEIGHTS


@pytest.mark.parametrize("code", _EXPECTED_WEIGHTS)
def test_each_fixed_profile_locks_existing_weights_type_and_private_audit(code):
    profile = rules.risk_profile(code)
    assert profile is rules.RISK_PROFILES[code]
    assert isinstance(profile.weights, tuple) and profile.weights == _EXPECTED_WEIGHTS[code]
    assert profile.audit() == {
        "selection": "fixed_disease_code",
        "barrierWeight": _EXPECTED_WEIGHTS[code][0],
    }


def test_catalog_and_profile_are_read_only_and_audit_returns_an_independent_copy():
    with pytest.raises(TypeError):
        rules.RISK_PROFILES["ibd"] = None
    profile = rules.risk_profile("ibd")
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        profile.barrier_weight = 10
    with pytest.raises(TypeError):
        profile.weights[0] = 0
    first = profile.audit()
    first["type"] = "synthetic-mutated-copy"
    first["barrierWeight"] = 0
    assert profile.audit() == rules.risk_profile("ibd").audit()
    assert profile.audit()["barrierWeight"] == 4 and profile.weights == (4,)






@pytest.mark.parametrize("bad_weight", [
    True, False, None, "1", -1, -.01, 10.01, float("inf"), float("-inf"), float("nan"),
    Decimal("NaN"), Decimal("Infinity"),
])
def test_non_numeric_non_finite_boolean_negative_or_over_limit_weights_are_rejected(bad_weight):
    with pytest.raises((ValueError, TypeError)):
        rules.RiskProfile(bad_weight)


@pytest.mark.parametrize("values", [
    (1, 1, 1, .3), (24, 18, 25, .3), (1, 1, 1, 3), (24, 18, 25, 3),
])
def test_profiles_are_fixed_by_disease_code_not_patient_measurements_names_or_metadata(values):
    assessment = _all_profile_assessment(values=values)
    for row in assessment["diseaseRisks"]:
        row["name"] = "IBD 高风险 ++++ 炎症型 TEST_ONLY"
        row["profile"] = {"type": "inflammation_dominant", "barrierWeight": 10}
    before = deepcopy(assessment)
    result = rules.apply_risk_index_rules(assessment)
    for code, row in _items(result).items():
        assert row["profile"] == rules.risk_profile(code).audit()
        assert row["weights"] == list(_EXPECTED_WEIGHTS[code])
    assert assessment == before


def test_all_normal_profiles_keep_confirmed_system_grades_without_changing_weights():
    result = _result(values=(0, 0, 0, 0))
    assert len(_items(result)) == 23
    assert result["riskRuleAssessment"]["allocation"]["highCodes"] == []
    assert result["riskRuleAssessment"]["allocation"]["mediumCodes"] == []
    assert _items(result)["ibd"]["riskIndex"] == 3.2
    assert _items(result)["ibd"]["riskLevel"] == "低"
    assert sum(row["riskLevel"] == "低" for row in _items(result).values()) == 23
    assert sorted(row["riskIndex"] for row in _items(result).values()) == [i / 10 for i in range(10, 33)]
    for code, row in _items(result).items():
        assert row["profile"] == rules.risk_profile(code).audit()
        assert row["weights"] == list(_EXPECTED_WEIGHTS[code])
        assert row["components"] == {"baseline": 1, "barrier": 0.0,
                                     "individualFactors": 0.0,
                                     "organFactor": 0.0, "organContribution": 0.0}


def test_increasing_ibfc_grade_only_increases_base_barrier_contribution_for_every_profile():
    panels = [
        (1, 1, 1, .3), (15, 1, 1, .3), (1, 13, 1, .3),
        (1, 26, 1, .3), (1, 18, 25, .3), (24, 18, 25, .3),
    ]
    strengths = (0, .2, .4, .6, .8, 1)
    results = [_items(_result(values=panel)) for panel in panels]
    for code, (barrier_weight,) in _EXPECTED_WEIGHTS.items():
        contributions = [_base_components(result[code])["barrier"] for result in results]
        assert contributions == sorted(contributions)
        assert contributions == pytest.approx([strength * barrier_weight for strength in strengths])
        assert all("inflammation" not in result[code]["components"] for result in results)
        assert all(result[code]["components"]["individualFactors"] == 0 for result in results)
        assert all(result[code]["profile"] == rules.risk_profile(code).audit() for result in results)




def test_individual_body_factor_changes_without_reselecting_type_or_altering_marker_contributions():
    results = [_items(_result(values=(1, 13, 1, 1), context={"bmi": weight / 1.6 ** 2}))
               for weight in (65, 74, 84, 92)]
    contributions = [result["obesity"]["components"]["organContribution"] for result in results]
    # B=.4 contributes .4 through 2*(B/2); BMI enters only the organ term.
    assert contributions == [0.4, 4.4, 6.4, 8.4]
    assert all(result["obesity"]["components"]["individualFactors"] == 0 for result in results)
    assert [result["obesity"]["riskIndex"] for result in results] == sorted(
        result["obesity"]["riskIndex"] for result in results
    )
    assert len({result["obesity"]["components"]["barrier"] for result in results}) == 1
    assert all("inflammation" not in result["obesity"]["components"] for result in results)
    assert all(result["obesity"]["profile"] == rules.risk_profile("obesity").audit() for result in results)


@pytest.mark.parametrize("mutation", [
    "type", "selection", "barrierWeight", "inflammationWeight", "weights", "components", "missing_profile",
])
def test_publication_rejects_a_forged_profile_weight_or_component_ledger(mutation):
    result = _result(values=(24, 18, 25, 3))
    assert rules.confirmed_rule_results(result)
    row = _items(result)["ibd"]
    if mutation == "weights":
        row["weights"][0] = 0
    elif mutation == "components":
        row["components"]["barrier"] = 0
    elif mutation == "missing_profile":
        del row["profile"]
    else:
        row["profile"][mutation] = {
            "type": "inflammation_dominant", "selection": "patient_indicator_pattern",
            "barrierWeight": 0, "inflammationWeight": 10,
        }[mutation]
    assert rules.confirmed_rule_results(result) == {}


def test_unknown_disease_has_no_profile_or_invented_weights_and_known_rules_remain_auditable():
    assessment = _assessment()
    assessment["diseaseRisks"].append({"code": "unknown_TEST_ONLY", "name": "Unknown technical fixture"})
    result = rules.apply_risk_index_rules(assessment)
    assert rules.risk_profile("unknown_TEST_ONLY") is None
    assert "unknown_TEST_ONLY" not in _items(result)
    assert assessment["diseaseRisks"] == result["diseaseRisks"]
    assert set(rules.confirmed_rule_results(result)) == set(_EXPECTED_WEIGHTS)
    assert rules.confirmed_rule_results(result)["ibd"][1] == "低"


@pytest.mark.parametrize("field", ["dLacticAcid", "diamineOxidase", "bacterialEndotoxin"])
def test_missing_measurements_retain_profile_metadata_but_never_supply_scores(field):
    payload = _payload(values=(24, 18, 25, 3))
    del payload["gutHealthIndicator"][field]
    result = _result(payload=payload)
    assert rules.confirmed_rule_results(result) == {}
    for code, row in _items(result).items():
        assert row["profile"] == rules.risk_profile(code).audit()
        assert row["status"] == "not_evaluated" and row["components"] is None
        assert row["riskIndex"] is None and row["riskLevel"] is None


@pytest.mark.parametrize("age,context,crp", [
    (12, {}, 3), (None, {}, 3), (42, {"acuteInfection": True}, 3),
    (42, {"acuteInflammation": True}, 3),
])
def test_profile_types_never_bypass_age_or_acute_inflammation_safety_gates(age, context, crp):
    result = _result(values=(24, 18, 25, crp), age=age, context=context)
    assert rules.confirmed_rule_results(result) == {}
    for code, row in _items(result).items():
        assert row["profile"] == rules.risk_profile(code).audit()
        assert row["status"] == "not_evaluated" and row["components"] is None
        assert row["riskIndex"] is None and row["riskLevel"] is None


def test_current_report_uses_v27_scores_but_preserves_model_values_and_patient_facts():
    from modules.gut_barrier.risk_model import build_disease_risks
    assessment = _assessment(values=(24, 18, 25, 3))
    if not any(row["code"] == "ibd" for row in assessment["diseaseRisks"]):
        assessment["diseaseRisks"].append(deepcopy(next(
            row for row in build_disease_risks()[0] if row["code"] == "ibd"
        )))
    assessment["riskModel"] = {
        "status": "evaluated", "modelId": "TEST_ONLY_independent_model", "modelVersion": "TEST_ONLY_v1",
        "items": [{"code": "ibd", "status": "evaluated", "reasons": []}],
    }
    model_row = next(row for row in assessment["diseaseRisks"] if row["code"] == "ibd")
    model_row.update(status="evaluated", riskIndex=9.2, riskLevel="low", reasons=[])
    before = deepcopy(assessment)
    result = rules.apply_risk_index_rules(assessment)
    assert assessment == before
    assert {key: value for key, value in result.items() if key != "riskRuleAssessment"} == before
    assert result["riskModel"] == before["riskModel"]
    assert next(row for row in result["diseaseRisks"] if row["code"] == "ibd")["riskIndex"] == 9.2
    assert _items(result)["ibd"]["profile"]["barrierWeight"] == 4
    from tests.unit.gut_barrier.test_template_risk_review import _publish
    callback = _publish(result)
    chapter = next(section for section in callback["sections"] if section["id"] == "disease_risks")
    disease_rows = [row for block in chapter["blocks"]
                    if block.get("columns") == ["疾病", "风险指数", "风险等级", "机制关联"]
                    for row in block["rows"]]
    # raw IBD = 1+4+2*(24-22)/(30-22)=5.5, map high=8.3.
    assert next(row for row in disease_rows if row[0] == model_row["name"])[1:3] == [8.3, "高"]
    assert "profile" not in callback and "riskRuleAssessment" not in callback
    assert result["riskModel"] == before["riskModel"] and assessment == before


def test_profile_audits_and_generated_assessments_do_not_share_mutable_state():
    original = _all_profile_assessment(values=(24, 18, 25, 3))
    before = deepcopy(original)
    first, second = rules.apply_risk_index_rules(original), rules.apply_risk_index_rules(original)
    original_profile = deepcopy(_items(first)["ibd"]["profile"])
    _items(first)["ibd"]["profile"]["type"] = "mutated_copy"
    _items(first)["ibd"]["weights"][0] = 0
    _items(first)["ibd"]["components"]["barrier"] = 0
    assert original == before
    assert _items(second)["ibd"]["profile"] == original_profile == rules.risk_profile("ibd").audit()
    assert _items(second)["ibd"]["weights"] == [4]
    assert rules.confirmed_rule_results(first) == {} and rules.confirmed_rule_results(second)


def test_all_23_organ_mappings_with_nonzero_liver_kidney_body_and_lipids():
    from tests.unit.gut_barrier.test_risk_index_rules import TODAY, _lipid

    hospital = [
        {"dataType": "INSPECTION", "checkTime": TODAY, "itemName": name,
         "testResultValue": "100", "referenceRange": "10-50", "unit": "U/L"}
        for name in ("ALT", "AST", "ALP")
    ] + [
        {"dataType": "INSPECTION", "checkTime": TODAY, "itemName": "GFR",
         "testResultValue": "59", "unit": "mL/min/1.73m²"}, _lipid(),
    ]
    result = _result(values=(24, 18, 25, 6.5),
                     context={"bmi": 30}, hospital=hospital)
    factors = result["riskRuleAssessment"]["continuousFactors"]
    assert factors["liver"]["factor"] == .75 and factors["kidney"]["factor"] == .5
    assert factors["bmi"]["strength"] == factors["lipids"]["strength"] == 1
    # Fixed expectations from section 5: B=1, pD=.25, pDAO=pLPS=.5.
    expected = {
        "ibd": .25, "intestinal_barrier_followup": .5, "ibs": .5, "nafld": .5,
        "cirrhosis": .75, "type2_diabetes": 1.75, "obesity": 2.5,
        "hyperlipidemia": 3.725, "hyperuricemia": .5, "atherosclerosis": 0,
        "coronary_heart_disease": 0, "stroke": 0, "hypertension": 0,
        "alzheimers": .25, "parkinsons": .25, "depression": 0, "anxiety": 0,
        "rheumatoid_arthritis": 0, "systemic_lupus": 0,
        "chronic_kidney_disease": .5, "liver_cancer": .75,
        "colorectal_cancer": .25, "lung_cancer": 0,
    }
    rows = _items(result)
    assert set(rows) == set(expected)
    for code, organ in expected.items():
        components = rows[code]["components"]
        assert components["organFactor"] == pytest.approx(organ)
        assert components["organContribution"] == pytest.approx(2 * organ)
        wb, = _EXPECTED_WEIGHTS[code]
        assert rows[code]["rawScore"] == pytest.approx(
            1 + wb + 2 * organ + components["individualFactors"])
    assert rows["type2_diabetes"]["components"]["individualFactors"] == 0
    assert rows["obesity"]["components"]["individualFactors"] == 0
    assert rows["hyperlipidemia"]["components"]["individualFactors"] == .5
