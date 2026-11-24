"""Regression cases for the October product feedback, all synthetic subjects."""
from copy import deepcopy

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.barrier_context import (
    BARRIER_LINKED_CODES, catalog_mechanism_reference_ids, patient_mechanism,
)
from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment
from modules.gut_barrier.risk_model import build_disease_risks
from modules.gut_barrier.schemas import GutBarrierRequest


def case(*, d=1, dao=1, lps=1, crp=0.3, survey=None, age=31, context=None):
    request = GutBarrierRequest.model_validate({
        "userId": "barrier-linkage-unit", "sessionId": "synthetic", "age": age, "sex": "男",
        "healthSurvey": survey,
        "gutHealthIndicator": {
            "dLacticAcid": {"value": d, "unit": "mg/L"} if d is not None else None,
            "diamineOxidase": {"value": dao, "unit": "U/L"} if dao is not None else None,
            "bacterialEndotoxin": {"value": lps, "unit": "U/L"} if lps is not None else None,
            "hsCRP": {"value": crp, "unit": "mg/L"},
        },
    })
    return assess_case(request, context={"currentDate": "2026-10-06", **(context or {})})


def rows(assessment):
    return {row["code"]: row for row in apply_dynamic_assessment(assessment)["diseaseRisks"]}


def test_report_case_includes_accepted_d_lactate_with_confirmed_two_point_grade():
    original = case(d=20.88, dao=0.5, lps=2.4, crp=2.35)
    assert original["barrier"]["status"] == "evaluated"
    assert original["barrier"]["score"] == 2
    assert original["barrier"]["grade"] == "I"
    result = apply_dynamic_assessment(original)
    selected = {row["code"]: row for row in result["diseaseRisks"]}
    assert set(selected) == {"intestinal_barrier_followup"}
    assert selected["intestinal_barrier_followup"]["riskIndex"] == 3
    assert "D-乳酸 20.88 mg/L" in selected["intestinal_barrier_followup"]["qualitative"]["basis"]
    assert "Q32" in selected["intestinal_barrier_followup"]["qualitative"]["evidenceIds"]
    plan = result["diseaseAssessment"]["selection"]["topicPlan"]
    planned = {code for item in plan if item["eligibility"] == "supported_direction" for code in item["diseaseCodes"]}
    assert set(selected) <= planned
    assert apply_dynamic_assessment(result) == result


def test_normal_panel_and_missing_panel_do_not_manufacture_directions():
    assert rows(case()) == {}
    assert rows(case(d=None, dao=None, lps=None)) == {}


@pytest.mark.parametrize("age", [None, 0, 17])
def test_unconfirmed_or_child_age_does_not_activate_adult_barrier_routes(age):
    assert rows(case(d=35, dao=25, lps=40, crp=4, age=age)) == {}


@pytest.mark.parametrize("d", [">30", "14", "15"])
def test_comparison_and_subhealth_values_do_not_trigger_clinical_barrier_routes(d):
    assert rows(case(d=d)) == {}


@pytest.mark.parametrize("d,score", [("17", 2), ("22", 3), ("30", 4)])
def test_confirmed_clinical_marker_endpoints_reach_supported_barrier_routes(d, score):
    assessment = case(d=d)
    assert assessment["markers"][0]["score"] == score
    assert "intestinal_barrier_followup" in rows(assessment)


def test_conflict_does_not_add_score_but_independent_accepted_marker_remains_usable():
    original = case(d=35)
    marker = next(row for row in original["markers"] if row["code"] == "D_LACTATE")
    marker.update(status="needs_review", reasons=["conflicting_source_values"])
    assert rows(original) == {}
    original = case(d=35, dao=25)
    marker = next(row for row in original["markers"] if row["code"] == "D_LACTATE")
    marker.update(status="needs_review", reasons=["conflicting_source_values"])
    selected = rows(original)
    facts = selected["intestinal_barrier_followup"]["attention"]["facts"]
    assert len(facts) == 1 and [row["code"] for row in facts[0]["markers"]] == ["DAO"]


@pytest.mark.parametrize("observations,reason", [
    ([{"code": "D_LACTATE", "value": 20, "unit": "mg/L", "date": "2026-10-06"},
      {"code": "D_LACTATE", "value": 35, "unit": "mg/L", "date": "2026-10-06"}],
     "conflicting_source_values"),
    ([{"code": "D_LACTATE", "value": 35, "unit": "mg/L", "date": "2026-10-05", "isCurrent": True}],
     "current_sample_not_confirmed"),
    ([{"code": "D_LACTATE", "value": 35, "unit": "mmol/L", "date": "2026-10-06"}],
     "unsupported_unit"),
])
@pytest.mark.parametrize("accepted_dao", [False, True])
def test_unusable_raw_observations_never_contribute_barrier_attention(observations, reason, accepted_dao):
    request = GutBarrierRequest.model_validate({
        "userId": "observation-source-unit", "sessionId": "synthetic", "age": 31,
        "healthSurvey": {"heightCm": "160", "weightKg": "90"},
        "gutHealthIndicator": {"hsCRP": {"value": 0.3, "unit": "mg/L"}},
    })
    comparison_panel = [
        {"code": "DAO", "value": 25 if accepted_dao else 1, "unit": "U/L", "date": "2026-10-06"},
        {"code": "LPS", "value": 1, "unit": "U/L", "date": "2026-10-06"},
    ]
    baseline = assess_case(request, observations=comparison_panel, context={"currentDate": "2026-10-06"})
    assessed = assess_case(request, observations=[*comparison_panel, *observations],
                          context={"currentDate": "2026-10-06"})
    marker = next(row for row in assessed["markers"] if row["code"] == "D_LACTATE")
    assert marker["status"] == "needs_review" and reason in marker["reasons"]
    assert marker["value"] is None and marker["score"] is None
    selected = rows(assessed)
    # Compare the complete rows, including the score, citations, mechanism and
    # adopted facts: unavailable D-lactate must behave like absent D-lactate.
    assert selected == rows(baseline)
    barrier_facts = [fact for row in selected.values() for fact in row["attention"]["facts"]
                     if fact["domain"] == "intestinal_barrier"]
    assert bool(barrier_facts) is accepted_dao
    for fact in barrier_facts:
        assert [marker["code"] for marker in fact["markers"]] == ["DAO"]


def test_valid_current_raw_observation_reaches_the_barrier_direction():
    request = GutBarrierRequest.model_validate({"userId": "observation-control-unit", "sessionId": "synthetic", "age": 31})
    assessed = assess_case(request, observations=[
        {"code": "D_LACTATE", "value": 35, "unit": "mg/L", "date": "2026-10-06"},
    ], context={"currentDate": "2026-10-06"})
    selected = rows(assessed)
    assert set(selected) == {"intestinal_barrier_followup"}
    assert selected["intestinal_barrier_followup"]["riskIndex"] == 5
    fact = selected["intestinal_barrier_followup"]["attention"]["facts"][0]
    assert fact["markers"][0]["source"][0]["source"]["kind"] == "observations"


@pytest.mark.parametrize("crp,context,expected", [
    (0.3, {}, False), (1.02, {}, False), (2.35, {}, False), (4, {}, False),
    (20, {}, False), (">3", {}, False), (4, {"acuteInfection": True}, False),
    (4, {"acuteInflammation": True}, False),
])
def test_metabolic_direction_requires_independent_accepted_nonacute_context(crp, context, expected):
    selected = rows(case(d=35, crp=crp, context=context))
    assert ("metabolic_barrier_followup" in selected) is expected
    assert "intestinal_barrier_followup" in selected
    assert not {"ibs", "ibd", "type2_diabetes", "nafld", "obesity", "lung_cancer", "alzheimers"}.intersection(selected)


def test_barrier_severity_affects_attention_without_counting_three_independent_markers():
    mild = rows(case(d=20))["intestinal_barrier_followup"]
    moderate = rows(case(d=25))["intestinal_barrier_followup"]
    severe = rows(case(d=35))["intestinal_barrier_followup"]
    all_severe = rows(case(d=35, dao=25, lps=40))["intestinal_barrier_followup"]
    assert [row["riskIndex"] for row in (mild, moderate, severe)] == [3, 4, 5]
    assert severe["riskIndex"] == all_severe["riskIndex"]
    assert all_severe["attention"]["components"]["independentFactors"] == 0
    assert len(all_severe["attention"]["facts"]) == 1
    assert all(row["probability"] is None for row in (mild, moderate, severe, all_severe))


def test_specific_diseases_still_require_clinical_trigger_and_only_related_rows_change():
    survey = {"heightCm": "160", "weightKg": "90", "bowelMovement": "时常腹泻", "digestion": "消化不良"}
    normal = rows(case(survey=survey, context={"bmi": 35}))
    abnormal = rows(case(dao=24.9, survey=survey, context={"bmi": 35}))
    for code in ("obesity", "nafld", "type2_diabetes"):
        assert abnormal[code]["riskIndex"] > normal[code]["riskIndex"]
        assert any(f["domain"] == "intestinal_barrier" for f in abnormal[code]["attention"]["facts"])
        assert "屏障" in abnormal[code]["mechanism"]
    for code in ("hypertension", "hyperlipidemia", "sleep_apnea"):
        assert abnormal[code]["riskIndex"] == normal[code]["riskIndex"]
    assert not {"ibs", "ibd", "cirrhosis", "colorectal_cancer",
                "diarrhea_evaluation", "dyspepsia_evaluation"}.intersection(abnormal)


def test_independent_model_zero_survives_barrier_linkage():
    original = case(d=35, survey={"heightCm": "160", "weightKg": "90"})
    authoritative = next(row for row in original["diseaseRisks"] if row["code"] == "nafld")
    authoritative.update(status="evaluated", riskIndex=0.0, riskLevel="TEST_ZERO", probability=0.0)
    selected = rows(original)["nafld"]
    assert selected["riskIndex"] == 0.0 and selected["riskLevel"] == "TEST_ZERO"
    assert selected["probability"] == 0.0 and "attention" not in selected


def test_mechanisms_are_single_sentences_with_action_kept_separately():
    original = case(dao=25, survey={"heightCm": "160", "weightKg": "90", "digestion": "反复腹泻伴腹痛", "bowelMovement": "便血"})
    before = deepcopy(original)
    selected = rows(original)
    assert original == before
    for row in selected.values():
        assert row["mechanism"].count("。") == 1 and "\n" not in row["mechanism"]
        assert not any(text in row["mechanism"] for text in (
            "本次", "关注", "提示", "线索", "依据", "随访", "不能诊断", "不替代", "建议", "就医"))
    assert set(selected) == {"intestinal_barrier_followup"}
    assert selected["intestinal_barrier_followup"]["qualitative"]["recommendation"]


@pytest.mark.parametrize("code,mechanism_concepts", [
    ("intestinal_barrier_followup", ("屏障", "炎症", "功能紊乱")),
    ("metabolic_barrier_followup", ("屏障", "炎症", "糖脂代谢")),
    ("obesity", ("屏障", "炎症", "体脂", "胰岛素")),
    ("nafld", ("屏障", "通透性", "肝脏", "脂质")),
    ("type2_diabetes", ("屏障", "炎症", "胰岛素", "糖代谢")),
    ("ibs", ("屏障", "免疫", "腹痛", "排便")),
    ("ibd", ("屏障", "免疫", "炎症")),
])
def test_barrier_mechanism_explains_biology_without_assessment_or_diagnostic_claims(code, mechanism_concepts):
    mechanism = patient_mechanism(code, barrier_linked=True)
    assert all(concept in mechanism for concept in mechanism_concepts)
    assert any(qualifier in mechanism for qualifier in ("可能", "相关"))
    assert not any(text in mechanism for text in (
        "本次", "关注", "提示", "线索", "依据", "评估", "随访", "就医", "建议",
        "确诊", "证实", "证明", "必然", "导致", "患病概率", "风险升高"))
    assert mechanism.count("。") == 1


@pytest.mark.parametrize("catalog_row", build_disease_risks()[0], ids=lambda row: row["code"])
def test_catalog_mechanism_remains_biological_and_preserves_experimental_scope(catalog_row):
    mechanism = patient_mechanism(catalog_row["code"], catalog_mechanism=catalog_row["mechanism"])
    assert not any(text in mechanism for text in (
        "本次", "关注", "提示", "线索", "本次依据", "随访", "就医", "建议"))
    if catalog_row["code"] in {"parkinsons", "systemic_lupus", "colorectal_cancer"}:
        assert "小鼠" in mechanism or "动物" in mechanism or "模型" in mechanism
        assert catalog_mechanism_reference_ids(catalog_row["code"])
    if catalog_row["code"] in {"alzheimers", "depression"}:
        assert "人群" in mechanism or "老年人研究" in mechanism
        assert "相关" in mechanism and catalog_mechanism_reference_ids(catalog_row["code"])


@pytest.mark.parametrize("survey", [
    {"heightCm": "160", "weightKg": "90", "digestion": "反复腹泻伴腹痛"},
    {"heightCm": "160", "weightKg": "90", "digestion": "反复腹泻伴腹痛", "bowelMovement": "便血"},
])
def test_pure_mechanisms_keep_the_adopted_barrier_facts_references_and_followup(survey):
    selected = rows(case(dao=25, crp=2.35, survey=survey))
    for code, row in selected.items():
        if code not in BARRIER_LINKED_CODES:
            continue
        assert set(BARRIER_LINKED_CODES[code]) <= set(row["qualitative"]["evidenceIds"])
        assert "二胺氧化酶 25 U/L" in row["qualitative"]["basis"]
        assert row["qualitative"]["recommendation"]
        assert row["riskIndex"] is not None and row["probability"] is None
        assert any(fact["domain"] == "intestinal_barrier" for fact in row["attention"]["facts"])
        assert not any(text in row["mechanism"] for text in ("本次", "提示", "关注", "就医"))


def test_remote_studies_cannot_add_diseases_or_numeric_weights():
    original = case(d=20, crp=2.35)
    clean = apply_dynamic_assessment(original)
    remote = {"status": "success", "candidates": [{"doi": "10.1155/2019/8536952", "code": "lung_cancer", "riskIndex": 10}]}
    actual = apply_dynamic_assessment(original, remote)
    assert clean["diseaseRisks"] == actual["diseaseRisks"]
    assert all(ref["numericalWeight"] is None for ref in actual["diseaseAssessment"]["appliedEvidence"])
