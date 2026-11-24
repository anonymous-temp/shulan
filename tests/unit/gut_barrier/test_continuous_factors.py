"""Specified v2.7 factor mathematics and source selection, not clinical validity."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json

import pytest

from modules.gut_barrier.continuous_factors import (
    build_continuous_factors, bmi_strength, clinical_factor_code,
    gfr_abnormality, marker_position, reference_abnormality,
    reference_interval,
)


D = Decimal
DAY = "2026-10-08"


def _lab(name, value, reference="10-50", *, day=DAY, unit="U/L", **extra):
    return {"itemName": name, "value": value, "referenceRange": reference,
            "date": day, "unit": unit, "source": [{"kind": "hospitalData", "index": 0}],
            "dataType": "INSPECTION", **extra}


def _assessment(rows=(), **context):
    return {"context": {"currentDate": DAY, "clinicalFindings": list(rows), **context},
            "markers": [
                {"code": "D_LACTATE", "value": 10.83, "operator": "=", "source": [{}]},
                {"code": "DAO", "value": 20.01, "operator": "=", "source": [{}]},
                {"code": "LPS", "value": 22.98, "operator": "=", "source": [{}]},
                {"code": "HS_CRP", "value": None, "operator": ">", "source": [{}]},
            ]}


@pytest.mark.parametrize("code,bounds", [
    ("D_LACTATE", ("14", "17", "22", "30")),
    ("DAO", ("8.5", "11", "16", "20")),
    ("LPS", ("18", "22", "28", "35")),
])
def test_barrier_positions_keep_each_boundary_and_reset_as_specified(code, bounds):
    values = list(map(D, bounds))
    assert marker_position(code, 0) == 0
    assert marker_position(code, values[0] / 2) == D("0.1")
    assert marker_position(code, values[0] - D("0.001")) < D("0.2")
    for lower, upper in zip(values, values[1:]):
        assert marker_position(code, lower) == 0
        assert marker_position(code, (lower + upper) / 2) == D("0.5")
        assert 0 < marker_position(code, upper - D("0.001")) < 1
    assert marker_position(code, values[-1]) == 1
    assert marker_position(code, values[-1] + 100) == 1


@pytest.mark.parametrize("value", [None, True, "<14", "NaN", float("inf"), -1])
def test_positions_do_not_turn_invalid_results_into_exact_values(value):
    with pytest.raises(ValueError):
        marker_position("D_LACTATE", value)


def test_marker_position_rejects_unknown_code_and_keeps_decimal_precision():
    with pytest.raises(ValueError):
        marker_position("ALT", 30)
    assert marker_position("DAO", "9.56") == D("1.06") / D("2.5")
    # The prose's 0.35 example is not substituted for the defined arithmetic.
    assert marker_position("DAO", "9.56") == D("0.424")
    assert marker_position("DAO", "10.47") == D("0.788")






@pytest.mark.parametrize("reference", ["10-50", "10～50", "10至50", "10—50", "[10,50]", "10.0 - 50.0 U/L", [10, 50], {"lower": 10, "upper": 50}])
def test_patient_reference_range_formats(reference):
    assert reference_interval(reference) == (D(10), D(50))


@pytest.mark.parametrize("reference", [None, "", "<50", "≤50", "10", "10-10", "50-10", "男10-50女8-40", {"upper": 50}])
def test_missing_or_ambiguous_width_is_not_invented(reference):
    assert reference_interval(reference) is None
    assert reference_abnormality(70, reference) is None


@pytest.mark.parametrize("value,expected", [
    (20, "0"), (40, "0"), ("19.999", "0.5"), ("40.001", "0.5"),
    ("10.001", "0.5"), (10, "1"), (0, "1"),
    ("49.999", "0.5"), (50, "1"), (100, "1"),
])
def test_reference_width_fifty_percent_boundary_in_both_directions(value, expected):
    assert reference_abnormality(value, "20-40") == D(expected)


@pytest.mark.parametrize("value,expected", [(0, 2), ("59.99", 2), (60, .5), ("89.99", .5), (90, 0), (120, 0)])
def test_gfr_special_weights(value, expected):
    assert gfr_abnormality(value) == D(str(expected))


@pytest.mark.parametrize("value,expected", [(None, 0), ("NaN", 0), ("27.99", 0), (28, 1), ("31.99", 1), (32, 1.5), ("34.99", 1.5), (35, 2), (80, 2)])
def test_bmi_strict_thresholds(value, expected):
    assert bmi_strength(value) == D(str(expected))


@pytest.mark.parametrize("name,expected", [
    ("谷丙转氨酶(ALT)", "ALT"), ("血清天门冬氨酸氨基转移酶", "AST"),
    ("碱性磷酸酶", "ALP"), ("γ-谷氨酰转肽酶", "GGT"),
    ("总胆红素(TBIL)", "TOTAL_BILIRUBIN"), ("直接胆红素", "DIRECT_BILIRUBIN"),
    ("白蛋白", "ALBUMIN"), ("白/球比", "AG_RATIO"),
    ("尿素", "UREA"), ("血清肌酐", "CREATININE"),
    ("尿酸", "URIC_ACID"), ("估算肾小球滤过率(eGFR)", "GFR"),
    ("低密度脂蛋白(LDL-c)", "LDL_C"), ("总胆固醇", "TOTAL_CHOLESTEROL"),
    ("ALT/AST", None), ("尿微量白蛋白", None), ("hs-CRP/白蛋白", None),
])
def test_exact_aliases_do_not_promote_ratios_or_other_tests(name, expected):
    assert clinical_factor_code({"itemName": name}) == expected


def test_patient_reference_and_gfr_combine_without_early_rounding():
    data = _assessment([
        _lab("白蛋白", 39.7, "40-55", unit="g/L"),
        _lab("谷丙转氨酶", 85, "10-50"),
        _lab("尿素", 9, "3-7", unit="mmol/L"),
        _lab("GFR", 59, None, unit="mL/min/1.73m²"),
    ])
    original = deepcopy(data)
    lipid = {"strength": .5, "date": "2026-10-07", "source": [{"kind": "hospitalData"}]}
    values, audit = build_continuous_factors(data, bmi=32, lipid=lipid)
    assert values["L"] == D("0.375")
    assert values["R"] == D("0.75")
    assert values["p_D_LACTATE"] == D("10.83") / 14 * D("0.2")
    assert values["p_DAO"] == 1
    assert values["p_LPS"] == D("0.98") / 6
    assert "h_pos" not in values
    assert values["bmiStrength"] == D("1.5")
    assert values["lipidStrength"] == D("0.5")
    assert {row["code"] for row in audit["positions"]} == {"D_LACTATE", "DAO", "LPS"}
    assert len(audit["liver"]["items"]) == 8
    assert len(audit["kidney"]["items"]) == 4
    json.dumps(audit, allow_nan=False)
    assert data == original


def test_all_organ_components_cap_at_one():
    names = ("ALT", "AST", "ALP", "GGT", "总胆红素", "直接胆红素", "白蛋白", "白球比")
    kidneys = ("尿素", "肌酐", "尿酸")
    values, audit = build_continuous_factors(_assessment(
        [_lab(name, 100) for name in names + kidneys] + [_lab("GFR", 30, None, unit="mL/min/1.73m²")]))
    assert values["L"] == values["R"] == 1
    assert audit["liver"]["total"] == 8
    assert audit["kidney"]["total"] == 5


def test_latest_valid_record_before_sample_replaces_older_abnormality():
    rows = [
        _lab("ALT", 90, day="2026-10-06"),
        _lab("谷丙转氨酶(ALT)", 30, day="2026-10-07", unit=None),
        _lab("ALT", 90, day="2026-10-08"),
        _lab("ALT", 90, day="2026-10-07", reference=None),
        _lab("ALT", 90, day=None),
    ]
    values, audit = build_continuous_factors(_assessment(rows, sampleDate="2026-10-07"))
    assert values["L"] == 0
    item = audit["liver"]["items"][0]
    assert item["selected"]["value"] == 30
    assert item["selected"]["unit"] is None
    assert {row["reason"] for row in item["excluded"]} == {
        "older_valid_measurement", "measurement_after_evaluation",
        "reference_interval_missing_or_invalid", "measurement_date_missing",
    }


def test_same_day_conflict_is_never_resolved_by_source_order():
    normal, abnormal = _lab("ALT", 30), _lab("ALT", 90)
    for rows in ([normal, abnormal], [abnormal, normal]):
        values, audit = build_continuous_factors(_assessment(rows))
        assert values["L"] == 0
        assert audit["liver"]["items"][0]["status"] == "conflicting"
        assert audit["liver"]["items"][0]["selected"] is None


@pytest.mark.parametrize("canonical,alias,organ", [
    ("ALT", "谷丙转氨酶", "liver"), ("肌酐", "CREA", "kidney"),
])
@pytest.mark.parametrize("same_name", [False, True])
def test_assessment_merged_or_aliased_latest_conflict_never_revives_older_value(canonical, alias, organ, same_name):
    from modules.gut_barrier.assessment import assess_case
    from modules.gut_barrier.schemas import GutBarrierRequest

    def raw_row(name, value, day):
        return {"dataType": "INSPECTION", "itemName": name, "checkTime": day,
                "testResultValue": value, "unit": "U/L", "referenceRange": "10-50"}

    old = raw_row(canonical, 90, "2026-10-06")
    first = raw_row(canonical, 20, "2026-10-07")
    second = raw_row(canonical if same_name else alias, 30, "2026-10-07")
    for raw_rows in ([old, first, second], [second, first, old]):
        request = GutBarrierRequest.model_validate({
            "userId": "synthetic-factor-conflict", "sessionId": "synthetic-factor-conflict",
            "name": "合成因子冲突测试", "age": 42, "sex": "男", "hospitalData": raw_rows,
        })
        assessed = assess_case(request, context={"currentDate": DAY})
        findings = assessed["context"]["clinicalFindings"]
        assert any(row.get("conflictingResults") for row in findings) is same_name
        values, audit = build_continuous_factors(assessed)
        assert values["L" if organ == "liver" else "R"] == 0
        code = "ALT" if organ == "liver" else "CREATININE"
        item = next(row for row in audit[organ]["items"] if row["code"] == code)
        assert item["status"] == "conflicting"
        assert item["selected"] is None
        assert any(row["date"] == "2026-10-06" for row in item["excluded"])


def test_newer_valid_record_can_supersede_an_older_premerged_conflict():
    rows = [_lab("ALT", 90, day="2026-10-06", conflictingResults=True),
            _lab("ALT", 30, day="2026-10-07")]
    values, audit = build_continuous_factors(_assessment(rows))
    assert values["L"] == 0
    item = audit["liver"]["items"][0]
    assert item["status"] == "accepted"
    assert item["selected"]["date"] == "2026-10-07"


def test_newer_uncomputable_reference_does_not_hide_an_older_valid_measurement():
    rows = [_lab("ALT", 90, day="2026-10-06"),
            _lab("ALT", 30, day="2026-10-07", reference=None)]
    values, audit = build_continuous_factors(_assessment(rows))
    assert values["L"] == D("0.25")
    item = audit["liver"]["items"][0]
    assert item["status"] == "accepted"
    assert item["selected"]["date"] == "2026-10-06"


def test_identical_rows_contribute_once_with_both_sources_preserved():
    first, second = _lab("ALT", 90), _lab("谷丙转氨酶", "90.0")
    second["source"] = [{"kind": "hospitalData", "index": 1}]
    values, audit = build_continuous_factors(_assessment([first, second]))
    assert values["L"] == D("0.25")
    assert len(audit["liver"]["items"][0]["selected"]["source"]) == 2


def test_undated_workbook_values_stay_audited_without_becoming_current_measurements():
    values, audit = build_continuous_factors(_assessment([
        _lab("白蛋白", 39.7, "40-55", day=None, unit=None),
        _lab("GFR", 55, None, day=None, unit=None),
    ]))
    assert values["L"] == values["R"] == 0
    albumin = next(item for item in audit["liver"]["items"] if item["code"] == "ALBUMIN")
    assert albumin["excluded"][0]["reason"] == "measurement_date_missing"
    assert audit["kidney"]["items"][-1]["excluded"][0]["reason"] == "measurement_date_missing"


def test_missing_optional_sources_produce_zero_factors_and_audit_only():
    values, audit = build_continuous_factors({})
    assert all(value == 0 for value in values.values())
    assert audit["evaluationDate"] is None
    assert all(item["status"] == "missing" for item in audit["liver"]["items"])


def test_audit_keeps_decimal_and_date_sources_serializable_without_mutating_them():
    record = _lab("ALT", D("70"), [D("10"), D("50")], day=date(2026, 10, 8))
    data = _assessment([record])
    values, audit = build_continuous_factors(data, bmi=D("32"))
    assert values["L"] == D("0.25")
    assert audit["liver"]["items"][0]["selected"]["date"] == DAY
    json.dumps(audit, allow_nan=False)
    assert isinstance(record["value"], Decimal)
    assert isinstance(record["date"], date)


def test_invalid_numeric_source_is_audited_without_nonstandard_json_numbers():
    values, audit = build_continuous_factors(_assessment([_lab("ALT", float("nan"))]))
    assert values["L"] == 0
    excluded = audit["liver"]["items"][0]["excluded"][0]
    assert excluded["value"] == "nan"
    assert excluded["reason"] == "exact_value_missing"
    json.dumps(audit, allow_nan=False)


@pytest.mark.parametrize('value,unit,expected', [
    (90, 'mL/min/1.73m²', 0), (90, ' ML / MIN / 1.73 m^2 ', 0),
    (90, 'mL/min/(1.73m2)', 0), (90, 'mL/(min·1.73m²)', 0),
    (1.5, 'mL/s/1.73m²', 0), (1, 'mL/s/(1.73m²)', .125),
    (.9, 'mL/(s·1.73m²)', .5), (59, 'mL/min/1.73m2', .5),
])
def test_gfr_uses_normalized_indexed_units_before_applying_thresholds(value, unit, expected):
    values, audit = build_continuous_factors(_assessment([_lab('eGFR', value, None, unit=unit)]))
    assert values['R'] == D(str(expected))
    gfr = next(row for row in audit['kidney']['items'] if row['code'] == 'GFR')
    assert gfr['status'] == 'accepted'
    assert gfr['selected']['unit'] == unit


@pytest.mark.parametrize('unit', [None, '', 'mg/L', 'U/L', 'mL/min', [], {}, ['mL/min/1.73m²']])
def test_gfr_missing_or_incompatible_unit_has_no_organ_contribution(unit):
    values, audit = build_continuous_factors(_assessment([_lab('eGFR', 1.5, None, unit=unit)]))
    assert values['R'] == 0
    gfr = next(row for row in audit['kidney']['items'] if row['code'] == 'GFR')
    assert gfr['selected'] is None
    assert gfr['excluded'][0]['reason'] == 'gfr_unit_missing_or_unsupported'


def test_same_day_gfr_aliases_with_equivalent_units_do_not_conflict():
    values, audit = build_continuous_factors(_assessment([
        _lab('eGFR', .9, None, unit='mL/s/1.73m²'),
        _lab('估算肾小球滤过率', 54, None, unit='mL/min/1.73m²'),
    ]))
    assert values['R'] == D('.5')
    assert next(row for row in audit['kidney']['items'] if row['code'] == 'GFR')['status'] == 'accepted'
