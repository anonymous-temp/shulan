"""Confirmed score bins cover exact endpoints once without weakening input gates."""
from copy import deepcopy
from decimal import Decimal
from itertools import product

import pytest

from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.rules import GRADE_BANDS, RULE_VERSION, grade_score, score_marker
from tests.unit.gut_barrier.test_assessment import assess, marker, request


# Independent transcription of the confirmed interval contract.
_SINGLE_MARKER_BINS = {
    "D_LACTATE": ((0, 14, 0), (14, 17, 1), (17, 22, 2), (22, 30, 3), (30, None, 4)),
    "DAO": ((0, 8.5, 0), (8.5, 11, 1), (11, 16, 2.5), (16, 20, 3.5), (20, None, 5)),
    "LPS": ((0, 18, 0), (18, 22, 1.5), (22, 28, 3), (28, 35, 4.5), (35, None, 6)),
}
_GRADE_INTERVALS = ((0, 2, "I"), (2, 4.5, "II"), (4.5, 6, "III"), (6, 8, "IV"), (8, None, "V"))


@pytest.mark.parametrize("code", _SINGLE_MARKER_BINS)
def test_each_single_marker_interval_covers_both_sides_of_every_endpoint_exactly_once(code):
    bins = _SINGLE_MARKER_BINS[code]
    points = {Decimal(0), Decimal(100000)}
    for lower, upper, _ in bins:
        for raw in (lower, upper):
            if raw is not None:
                point = Decimal(str(raw))
                points.update(value for value in (point - Decimal("0.000001"), point, point + Decimal("0.000001")) if value >= 0)
    for value in sorted(points):
        matches = [score for lower, upper, score in bins
                   if Decimal(str(lower)) <= value and (upper is None or value < Decimal(str(upper)))]
        assert len(matches) == 1, (code, value, matches)
        result = score_marker(code, value)
        assert result["status"] == "evaluated" and result["score"] == matches[0]
        assert "ambiguous_source_endpoint" not in result["reasons"]


def test_ibfc_positive_domain_has_no_gap_or_overlap_at_zero_and_all_grade_endpoints():
    points = {Decimal("0.000001"), Decimal(15)}
    for endpoint in ("2", "4.5", "6", "8"):
        point = Decimal(endpoint)
        points.update((point - Decimal("0.000001"), point, point + Decimal("0.000001")))
    for value in sorted(points):
        matches = [grade for lower, upper, grade in _GRADE_INTERVALS
                   if Decimal(str(lower)) < value and (upper is None or value <= Decimal(str(upper)))]
        assert len(matches) == 1, (value, matches)
        assert grade_score(value) == (matches[0], [])
    assert grade_score(Decimal(0)) == ("normal", [])
    assert grade_score(Decimal("-0.01")) == (None, ["score_not_covered_by_source_grade"])
    assert GRADE_BANDS[:2] == (("I", "＞0–2分"), ("II", "＞2–4.5分"))


@pytest.mark.parametrize("choices", product(range(5), repeat=3))
def test_all_125_combinations_including_exact_lower_bounds_produce_complete_scores(choices):
    selected = [_SINGLE_MARKER_BINS[code][index]
                for code, index in zip(("D_LACTATE", "DAO", "LPS"), choices)]
    assessed = assess(request(*(low for low, _, _ in selected)))
    expected = sum(score for _, _, score in selected)
    assert [item["score"] for item in assessed["markers"][:3]] == [score for _, _, score in selected]
    assert assessed["barrier"]["score"] == expected
    expected_grade = "normal" if expected == 0 else next(
        grade for lower, upper, grade in _GRADE_INTERVALS if lower < expected and (upper is None or expected <= upper)
    )
    assert assessed["barrier"]["grade"] == expected_grade
    assert "grade_endpoint_requires_medical_confirmation" not in assessed["barrier"]["reasons"]
    assert assessed["ruleVersion"] == RULE_VERSION == "gut-barrier-2026-five-grade-v4.20261008-closed-boundaries"


@pytest.mark.parametrize("raw", ["<14", ">=14", ">22", "<=30"])
def test_closed_bins_do_not_turn_inequality_barrier_results_into_exact_measurements(raw):
    assessed = assess(request(d=raw))
    result = marker(assessed, "D_LACTATE")
    assert result["status"] == "needs_review" and result["value"] is None and result["score"] is None
    assert "barrier_score_requires_exact_value" in result["reasons"]
    assert assessed["barrier"]["score"] is None


def test_exact_endpoint_requires_valid_units_and_nonconflicting_current_sources():
    bad_unit = assess(request(gutHealthIndicator={"dLacticAcid": {"value": 14, "unit": "invalid"}}))
    assert marker(bad_unit, "D_LACTATE")["score"] is None
    assert "unsupported_unit" in marker(bad_unit, "D_LACTATE")["reasons"]
    conflict = assess(request(d=14), observations=[
        {"code": "D_LACTATE", "value": 15, "unit": "mg/L", "isCurrent": True},
    ])
    assert marker(conflict, "D_LACTATE")["score"] is None
    assert "conflicting_source_values" in marker(conflict, "D_LACTATE")["reasons"]
    assert conflict["barrier"]["score"] is None


def test_new_endpoint_scores_do_not_invent_a_subhealth_pattern_definition():
    assessed = assess(request(14, 8.5, 18))
    assert assessed["barrier"]["score"] == 3.5 and assessed["barrier"]["grade"] == "II"
    assert assessed["barrier"]["pattern"] is None
    assert "subhealth_pattern_definition_pending" in assessed["barrier"]["reasons"]


@pytest.mark.parametrize("d_value,d_score,ibfc", [(14, None, None), (19, 2, 4.5)])
def test_frozen_previous_endpoint_callback_keeps_saved_unknown_grade_and_values(d_value, d_score, ibfc):
    frozen = {
        "schemaVersion": "gut-barrier-public.v2",
        "meta": {"title": "已冻结报告", "reportId": "frozen-v3-endpoint"},
        "sections": [{"id": "overview", "title": "第一章", "subtitle": "", "visible": True, "blocks": [
            {"kind": "table", "title": "四项检测指标",
             "columns": ["检测指标", "检测结果", "参考范围", "结果评价", "单项评分"],
             "rows": [["D-乳酸", f"{d_value} mg/L", "＜14 mg/L", "需核对后评估" if d_score is None else "轻度升高", d_score],
                      ["DAO", "13 U/L", "＜8.5 U/L", "轻度升高", 2.5],
                      ["LPS", "5 U/L", "＜18 U/L", "正常", 0]]},
            {"kind": "cards", "title": "肠道屏障功能评分（IBFC）", "items": [
                {"label": "IBFC评分", "value": ibfc, "detail": None},
                {"label": "损伤等级", "value": None, "detail": "分级待复核"},
            ]},
        ]}],
    }
    before = deepcopy(frozen)
    assert project_callback_report(frozen) == before == frozen
    # New scoring is deliberately different from the saved pre-closure report.
    assert score_marker("D_LACTATE", Decimal(14))["score"] == 1
    assert grade_score(Decimal("4.5")) == ("II", [])
