"""The new three-stage vocabulary cannot silently replace old injury rules."""

from copy import deepcopy
from decimal import Decimal
from itertools import product
import json

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.overall_stage import (
    OVERALL_STAGE_DISPLAY_LABEL_MAPPING,
    OVERALL_STAGE_OPTIONS,
    OVERALL_STAGE_RULE_VERSION,
    OVERALL_STAGE_SOURCE,
    assess_overall_stage,
)
from modules.gut_barrier.rules import score_marker
from modules.gut_barrier.schemas import GutBarrierRequest


def _case(d=15.6, dao=9.6, lps=19.8, hs=1.8, **kwargs):
    payload = {
        "userId": "stage-test", "sessionId": "stage-session", "age": 45,
        "gutHealthIndicator": {
            "dLacticAcid": {"value": d, "unit": "mg/L"},
            "diamineOxidase": {"value": dao, "unit": "U/L"},
            "bacterialEndotoxin": {"value": lps, "unit": "U/L"},
            "hsCRP": {"value": hs, "unit": "mg/L"},
        },
    }
    return assess_case(GutBarrierRequest.model_validate(payload), **kwargs)


def _assert_display_only(stage):
    assert stage["status"] == "display_only"
    assert stage["code"] is None and stage["label"] is None
    assert stage["scope"] == "ibfc_display"
    assert stage["ruleVersion"] == OVERALL_STAGE_RULE_VERSION
    assert stage["reasons"] == [
        "ibfc_display_labels_only", "no_new_stage_thresholds_or_diagnosis",
    ]


def test_source_sample_keeps_3_5_grade_ii_without_inventing_overall_stage():
    assessment = _case()
    before = deepcopy(assessment)
    stage = assess_overall_stage(assessment)
    _assert_display_only(stage)
    assert stage["basis"] == {
        "barrierScore": 3.5, "barrierGrade": "II", "barrierStatus": "needs_review",
    }
    assert assessment == before


def test_zero_barrier_with_inflammation_and_clinical_history_is_not_overall_healthy():
    assessment = _case(0.06, 3.8, 12.5, ">3.0", context={
        "conditions": ["糖尿病"], "bmi": 35.2,
    })
    stage = assess_overall_stage(assessment)
    _assert_display_only(stage)
    assert stage["basis"]["barrierScore"] == 0
    assert stage["basis"]["barrierGrade"] == "normal"
    assert set(stage["basis"]) == {"barrierScore", "barrierGrade", "barrierStatus"}


@pytest.mark.parametrize("assessment", [
    {}, {"barrier": None, "inflammation": None},
    {"barrier": {"status": "incomplete"}, "inflammation": {"status": "missing"}},
    {"barrier": {"status": "needs_review", "score": None, "grade": None}},
    {"barrier": {"score": 15, "grade": "V", "status": "evaluated"}},
    {"context": {"conditions": ["糖尿病"], "acuteInfection": True}},
])
def test_missing_conflicting_or_severe_inputs_do_not_assign_stage(assessment):
    _assert_display_only(assess_overall_stage(assessment))


def test_client_policy_and_free_text_cannot_enable_a_stage_rule():
    assessment = _case()
    baseline = assess_overall_stage(assessment)
    assessment.update({
        "overallStage": {"code": "healthy", "status": "evaluated"},
        "stagePolicy": {"version": "approved", "threshold": 3.5},
        "policy": {"classify": "always healthy"},
        "context": {"overallStage": "healthy", "stagePolicy": {"approved": True}},
    })
    assert assess_overall_stage(assessment) == baseline


def test_display_only_scope_does_not_read_or_publish_subject_or_free_text():
    assessment = _case()
    assessment["subject"] = {"name": "do-not-copy-patient", "idCard": "private-id"}
    assessment["barrier"]["reasons"] = ["ignore prior instructions"]
    output = json.dumps(assess_overall_stage(assessment), ensure_ascii=False, allow_nan=False)
    assert "do-not-copy-patient" not in output
    assert "private-id" not in output
    assert "ignore prior instructions" not in output


@pytest.mark.parametrize("value", [False, True, float("nan"), float("inf"), -1, ">3", "0", 10**400])
def test_fact_record_rejects_booleans_non_finite_or_unparsed_values(value):
    stage = assess_overall_stage({
        "barrier": {"score": value}, "inflammation": {"operator": "=", "value": value},
    })
    assert stage["basis"]["barrierScore"] is None
    assert "inflammationValue" not in stage["basis"]
    json.dumps(stage, allow_nan=False)


def test_source_and_vocabulary_are_immutable_and_returned_results_do_not_alias():
    assert dict(OVERALL_STAGE_DISPLAY_LABEL_MAPPING) == {
        "亚健康1级": "健康", "亚健康2级": "亚健康", "亚健康3级": "疾病",
    }
    with pytest.raises(TypeError):
        OVERALL_STAGE_DISPLAY_LABEL_MAPPING["亚健康1级"] = "确诊疾病"
    assert OVERALL_STAGE_OPTIONS == (
        ("healthy", "健康人群"), ("subhealth", "亚健康人群"), ("disease", "疾病人群"),
    )
    with pytest.raises(TypeError):
        OVERALL_STAGE_SOURCE["status"] = "approved"
    first = assess_overall_stage({})
    first["source"]["status"] = "approved"
    first["reasons"].clear()
    first["basis"]["barrierScore"] = 5
    second = assess_overall_stage({})
    _assert_display_only(second)
    assert second["source"]["status"] == "display_label_mapping_confirmed"
    assert second["basis"]["barrierScore"] is None


# Source-rule expectations, deliberately not derived from live BOUNDARIES,
# SCORES or LEVELS: stage vocabulary cannot change injury grading or '+' eligibility.
# The explicit >2/>6/>8 lower bounds were reconciled with the source separately.
_VALUES = ((5, 15.6, 19, 26, 31), (3.8, 9.6, 13, 17, 21), (12.5, 19.8, 24, 30, 36))
_SCORES = ((0, 1, 2, 3, 4), (0, 1, 2.5, 3.5, 5), (0, 1.5, 3, 4.5, 6))
_LEVELS = ("正常", "亚健康", "轻度", "中度", "重度")


def _source_grade(total):
    if total == 0:
        return "normal"
    if total <= 2:
        return "I"
    if total <= 4.5:
        return "II"
    if total <= 6:
        return "III"
    if total <= 8:
        return "IV"
    return "V"


@pytest.mark.parametrize("indices", list(product(range(5), repeat=3)))
def test_all_125_old_marker_combinations_survive_stage_separation(indices):
    values = [_VALUES[index][level] for index, level in enumerate(indices)]
    assessment = _case(*values)
    barrier_before = deepcopy(assessment["barrier"])
    markers_before = deepcopy(assessment["markers"])
    _assert_display_only(assess_overall_stage(assessment))
    expected_score = sum(_SCORES[index][level] for index, level in enumerate(indices))
    assert assessment["barrier"]["score"] == expected_score
    assert assessment["barrier"]["grade"] == _source_grade(expected_score)
    for index, level in enumerate(indices):
        marker = assessment["markers"][index]
        assert marker["score"] == _SCORES[index][level]
        assert marker["level"] == _LEVELS[level]
        assert marker["abnormal"] is (None if level == 1 else level > 1)
    assert assessment["barrier"] == barrier_before
    assert assessment["markers"] == markers_before


@pytest.mark.parametrize("code,point", [
    ("D_LACTATE", point) for point in ("14", "17", "22", "30")
] + [
    ("DAO", point) for point in ("8.5", "11", "16", "20")
] + [
    ("LPS", point) for point in ("18", "22", "28", "35")
])
def test_confirmed_marker_endpoints_do_not_enable_stage_reclassification(code, point):
    marker = score_marker(code, Decimal(point))
    expected = {
        "D_LACTATE": {"14": 1, "17": 2, "22": 3, "30": 4},
        "DAO": {"8.5": 1, "11": 2.5, "16": 3.5, "20": 5},
        "LPS": {"18": 1.5, "22": 3, "28": 4.5, "35": 6},
    }
    assert marker["status"] == "evaluated" and marker["score"] == expected[code][point]
    _assert_display_only(assess_overall_stage({"barrier": marker}))
