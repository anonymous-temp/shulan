"""Offline regression tests for the independent gut barrier domain."""

from decimal import Decimal
from copy import deepcopy
from itertools import product

import pytest
from pydantic import ValidationError

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.rules import (
    BOUNDARIES, RULE_VERSION, RULE_SOURCE, grade_score, normalize_measurement_result, score_marker,
)
from modules.gut_barrier.schemas import GutBarrierRequest

TODAY = "2026-09-23"


def request(d=19, dao=13, lps=24, **extra):
    data = {
        "userId": "u-1", "sessionId": "s-1", "age": 35, "sex": "男",
        "gutHealthIndicator": {
            "dLacticAcid": {"value": d, "unit": "mg/L"},
            "diamineOxidase": {"value": dao, "unit": "U/L"},
            "bacterialEndotoxin": {"value": lps, "unit": "U/L"},
        },
    }
    data.update(extra)
    return GutBarrierRequest.model_validate(data)


def assess(req, **kwargs):
    context = {"currentDate": TODAY, **kwargs.pop("context", {})}
    return assess_case(req, context=context, **kwargs)


def hs(value=4.2, **extra):
    return {"name": "超敏C反应蛋白", "value": value, "unit": "mg/L", "date": TODAY, **extra}


def marker(result, code):
    return next(item for item in result["markers"] if item["code"] == code)


def assert_no_crp_assessment(result):
    assert "inflammation" not in result
    assert {item["code"] for item in result["markers"]} == {"D_LACTATE", "DAO", "LPS"}
    assert "HS_CRP" not in result["missingData"]
    assert all(item.get("code") != "HS_CRP" for item in result["provenance"]["observations"])
    assert all("CRP" not in str(row["basis"]) for row in result["risks"])


def test_current_observation_method_survives_assessment_but_is_not_published():
    from modules.gut_barrier.interventions import build_interventions
    from modules.gut_barrier.presentation import build_report
    result = assess(request(), observations=[{"code": "DAO", "value": 13, "unit": "U/L", "date": TODAY, "method": "  酶法  "}])
    assert marker(result, "DAO")["method"] == "酶法"
    report = build_report(result, build_interventions(result), report_id="method-test")
    assert marker(report["assessment"], "DAO")["method"] == "酶法"
    assert not any(block.get("title") == "本次检测方法与参考信息"
                   for section in report["sections"] for block in section["blocks"])


def test_conflicting_or_historical_methods_are_not_published_as_current():
    observation = {"code": "DAO", "value": 13, "unit": "U/L", "date": TODAY}
    conflict = assess(request(), observations=[dict(observation, method="方法 A"), dict(observation, method="方法 B")])
    assert marker(conflict, "DAO")["method"] is None
    assert "conflicting_assay_methods" in marker(conflict, "DAO")["reasons"]
    historical = assess(request(), observations=[dict(observation, method="方法 A", date="2020-01-01")])
    assert marker(historical, "DAO")["method"] is None


def test_actual_sample_quality_values_remain_internal_without_changing_fixed_appendix():
    from modules.gut_barrier.interventions import build_interventions
    from modules.gut_barrier.presentation import build_report

    empty = assess(request())
    actual = assess(request(), context={"sampleType": "血清", "qualityControl": {
        "sampleQuality": "机构记录：样本可接受", "preparation": "机构制备记录 A",
        "analytical": "机构分析记录 B", "examiner": "检测角色甲", "reviewer": "审核角色乙",
    }})
    reports = [build_report(result, build_interventions(result), report_id="qc-test") for result in (empty, actual)]
    assert reports[1]["assessment"]["context"]["qualityControl"] == actual["context"]["qualityControl"]
    assert actual["context"]["qualityControl"]["sampleQuality"] == "机构记录：样本可接受"
    assert actual["context"]["sampleType"] == "血清"
    assert not any(block.get("title") == "本次样本与质控记录"
                   for report in reports for section in report["sections"] for block in section["blocks"])
    fixed = [[block for section in report["sections"] if section["id"] == "appendix"
              for block in section["blocks"] if block["kind"] != "references"] for report in reports]
    assert fixed[0] == fixed[1]


def test_demo_uses_new_five_grade_version_not_legacy_grade():
    result = assess(request(), observations=[hs()])
    assert result["ruleVersion"] == RULE_VERSION
    assert [m["score"] for m in result["markers"][:3]] == [2, 2.5, 3]
    assert result["barrier"] == {
        "score": 7.5, "grade": "IV", "pattern": "+++", "patternCode": "111",
        "type": "混合型受损", "status": "evaluated", "reasons": [],
    }
    assert_no_crp_assessment(result)
    assert all(risk["riskIndex"] is None and risk["probability"] is None for risk in result["risks"])


def test_latest_case_has_nine_point_five_v_grade_without_crp_assessment():
    result = assess(request(26, 17, 23, age=50), observations=[hs(2.7)])
    assert RULE_SOURCE["sha256"] == "f108ec74f6cfe805d27e4469369c7d488e9cb75c60e438cfebbeebbba356133d"
    assert [item["score"] for item in result["markers"][:3]] == [3, 3.5, 3]
    assert result["barrier"]["score"] == 9.5
    assert result["barrier"]["grade"] == "V" and result["barrier"]["pattern"] == "+++"
    assert_no_crp_assessment(result)
    assert result["riskModel"]["status"] == "not_configured"
    assert len(result["diseaseRisks"]) == 23
    assert all(row["riskIndex"] is None and row["riskLevel"] is None for row in result["diseaseRisks"])


@pytest.mark.parametrize("value", [0, 0.5, 0.5001, 0.9999, 1, 1.0001, 2.7, 2.9999, 3, 3.0001, 10, 10.0001])
def test_retired_crp_strata_do_not_change_assessment(value):
    result = assess(request(), observations=[hs(value)])
    assert result == assess(request())
    assert_no_crp_assessment(result)


@pytest.mark.parametrize("raw", ["≤0.5", "<1", "<=1", ">1", ">=1", "<3", "<=3", ">3", ">=3"])
def test_retired_crp_ranges_are_ignored_without_measurement_derivation(raw):
    result = assess(request(), observations=[hs(raw)])
    assert result == assess(request())
    assert_no_crp_assessment(result)


@pytest.mark.parametrize("age", [None, 0, 17])
@pytest.mark.parametrize("value", [0.4, 1, 2.7, 3, ">3"])
def test_retired_crp_is_ignored_for_children_and_unknown_age(age, value):
    result = assess(request(age=age), observations=[hs(value)])
    assert result == assess(request(age=age))
    assert_no_crp_assessment(result)


@pytest.mark.parametrize("flag", ["acuteInfection", "acuteInflammation"])
@pytest.mark.parametrize("value", [0.4, 1, 2.7, 3, ">3"])
def test_confirmed_acute_context_survives_without_crp_interpretation(flag, value):
    result = assess(request(), observations=[hs(value)], context={flag: True})
    assert result["context"][flag] is True
    assert result == assess(request(), context={flag: True})
    assert_no_crp_assessment(result)


def test_retired_crp_missing_and_conflicting_observations_are_equivalent():
    baseline = assess(request())
    for observations in ([hs(2.7, date="2025-01-01")], [hs(2.7), hs(4.2)]):
        result = assess(request(), observations=observations)
        assert result == baseline
        assert_no_crp_assessment(result)


@pytest.mark.parametrize("value,unit,display,lower", [
    (">1", "mg/dL", ">10", "10"), (">10", "mg/dL", ">100", "100"),
    (">1.0", "mg/dL", ">10", "10"), ("≥2", "mg/dL", ">=20", "20"),
    (2, "mg/dL", "=20", "20"), (0, "mg/dL", "=0", "0"),
])
def test_unit_normalization_never_removes_significant_integer_zero(value, unit, display, lower):
    result, error = normalize_measurement_result("D_LACTATE", value, unit)
    assert error is None and result["displayValue"] == display
    assert result["bounds"]["lower"] == Decimal(lower)


def test_retired_crp_unit_conversions_do_not_create_assessment_content():
    result = assess(request(), observations=[hs(">1", unit="mg/dL")])
    assert result == assess(request())
    assert_no_crp_assessment(result)


def test_retired_crp_reference_ranges_do_not_change_other_marker_ranges():
    observations = [hs(2.7, referenceRange="0–5 mg/L", source={"kind": "verified_report", "page": 1})]
    hospital = [{"itemName": "总胆固醇", "value": "6.0", "unit": "mmol/L", "date": TODAY,
                 "referenceRange": "0–5.2"}]
    before = deepcopy(observations)
    result = assess(request(hospitalData=hospital), observations=observations)
    assert observations == before
    assert result == assess(request(hospitalData=hospital))
    assert marker(result, "D_LACTATE")["laboratoryReferenceRanges"] == []
    assert_no_crp_assessment(result)


def test_request_extra_fields_cannot_inject_medical_approval_or_invent_context():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        request(bmi=35, symptoms=["腹胀"], clinicianApproved=True,
                diseaseRisks=[{"riskIndex": 8.5}], riskModel={"status": "approved"})


@pytest.mark.parametrize("age", [0, 1, 17])
def test_children_do_not_receive_retired_crp_content(age):
    result = assess(request(age=age), observations=[hs()])
    assert result == assess(request(age=age))
    assert_no_crp_assessment(result)


def test_unknown_age_does_not_receive_retired_crp_content():
    result = assess(request(age=None), observations=[hs()])
    assert result == assess(request(age=None))
    assert_no_crp_assessment(result)


@pytest.mark.parametrize("empty", [None, {}])
def test_all_missing_indicators_are_valid_input_not_normal(empty):
    req = GutBarrierRequest.model_validate({"userId": "u", "sessionId": "s", "gutHealthIndicator": empty, "fileList": None, "testData": None, "hospitalData": None})
    assert req.file_list == req.test_data == req.hospital_data == []
    result = assess(req)
    assert result["barrier"]["status"] == "incomplete"
    assert result["barrier"]["score"] is None
    assert result["barrier"]["pattern"] is None
    assert all(item["status"] == "missing" for item in result["markers"])


@pytest.mark.parametrize("value", [{}, None, {"value": None, "unit": None}])
def test_empty_nested_indicator_is_missing(value):
    req = request(gutHealthIndicator={"dLacticAcid": value})
    assert marker(assess(req), "D_LACTATE")["status"] == "missing"


@pytest.mark.parametrize("value", [{"value": 19}, {"unit": "mg/L"}, {"value": None, "unit": "mg/L"}, {"value": "", "unit": "mg/L"}])
def test_half_indicator_preserves_available_input_and_other_marker_assessments(value):
    req = request(gutHealthIndicator={
        "dLacticAcid": value,
        "diamineOxidase": {"value": 13, "unit": "U/L"},
        "bacterialEndotoxin": {"value": 24, "unit": "U/L"},
    })
    provided_value = None if value.get("value") == "" else value.get("value")
    assert req.gut_health_indicator.d_lactic_acid.value == provided_value
    assert req.gut_health_indicator.d_lactic_acid.unit == value.get("unit")
    result = assess(req)
    incomplete = marker(result, "D_LACTATE")
    assert incomplete["status"] == ("needs_review" if provided_value is not None else "missing")
    assert incomplete["value"] is None and incomplete["score"] is None
    if provided_value is not None:
        assert incomplete["source"][0]["value"] == provided_value
        assert incomplete["source"][0]["unit"] is None
    assert result["barrier"]["score"] is None
    assert marker(result, "DAO")["status"] == marker(result, "LPS")["status"] == "evaluated"
    assert marker(result, "DAO")["value"] == 13 and marker(result, "DAO")["score"] == 2.5
    assert marker(result, "LPS")["value"] == 24 and marker(result, "LPS")["score"] == 3


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "NaN", "inf", "1" * 257])
def test_invalid_input_numbers_rejected(value):
    with pytest.raises(ValidationError):
        request(d=value)


@pytest.mark.parametrize("data", ['{"userId":"u",/*comment*/"sessionId":"s"}', '{&quot;userId&quot;:"u"}', '<!-- {"userId":"u","sessionId":"s"} -->'])
def test_comments_and_html_are_not_json(data):
    with pytest.raises(ValidationError):
        GutBarrierRequest.model_validate_json(data)


def test_markdown_links_normalized_in_all_declared_url_fields():
    req = request(fileList=[{"url": "[报告](https://example.test/report.pdf)", "fileType": "pdf"}], testData=[{"resultDetailUrl": "[详情](http://example.test/a)", "resultSummaryUrl": "[摘要](https://example.test/b)", "other": "unchanged"}])
    assert req.file_list[0].url == "https://example.test/report.pdf"
    assert req.test_data[0] == {"resultDetailUrl": "http://example.test/a", "resultSummaryUrl": "https://example.test/b", "other": "unchanged"}
    assert "dLacticAcid" in req.model_dump(by_alias=True)["gutHealthIndicator"]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "https://user:secret@example.test/a", "https://example.test/a&amp;b", "x [a](https://example.test/a)", "https://example.test/" + "a" * 4096])
def test_invalid_source_urls_rejected(url):
    with pytest.raises(ValidationError):
        request(testData=[{"resultDetailUrl": url}])


@pytest.mark.parametrize("field,count,item", [("fileList", 11, {"url": "https://example.test/a", "fileType": "pdf"}), ("testData", 11, {}), ("hospitalData", 1001, {})])
def test_source_list_limits(field, count, item):
    with pytest.raises(ValidationError):
        request(**{field: [item] * count})


def test_free_structure_retained_but_bounded():
    req = request(hospitalData=[{"arbitrary": {"nested": [1, "value"]}}])
    assert req.hospital_data[0]["arbitrary"]["nested"] == [1, "value"]
    with pytest.raises(ValidationError):
        request(hospitalData=[{"text": "a" * 20001}])


@pytest.mark.parametrize("flags", list(product((False, True), repeat=3)))
def test_all_seven_patterns_and_all_normal(flags):
    d, dao, lps = (19 if flags[0] else 10), (13 if flags[1] else 5), (24 if flags[2] else 10)
    result = assess(request(d, dao, lps))
    assert result["barrier"]["pattern"] == "".join("+" if flag else "-" for flag in flags)
    assert result["barrier"]["patternCode"] == "".join("1" if flag else "0" for flag in flags)
    if not any(flags):
        assert result["barrier"]["score"] == 0
        assert result["barrier"]["grade"] == "normal"


@pytest.mark.parametrize("code,point", [(code, point) for code, points in BOUNDARIES.items() for point in points])
def test_confirmed_marker_endpoint_starts_the_next_half_open_bin(code, point):
    exact = score_marker(code, point)
    before = score_marker(code, point - Decimal("0.01"))
    after = score_marker(code, point + Decimal("0.01"))
    assert exact["status"] == "evaluated"
    assert exact == after
    assert before["score"] < after["score"]


@pytest.mark.parametrize("score,before,at,after", [
    ("2", "I", "I", "II"), ("4.5", "II", "II", "III"),
    ("6", "III", "III", "IV"), ("8", "IV", "IV", "V"),
])
def test_grade_endpoints_follow_explicit_next_grade_strict_lower_bound(score, before, at, after):
    point = Decimal(score)
    assert grade_score(point - Decimal("0.01")) == (before, [])
    assert grade_score(point) == (at, [])
    assert grade_score(point + Decimal("0.01")) == (after, [])


@pytest.mark.parametrize("score,grade", [("0", "normal"), ("0.01", "I"), ("0.5", "I"), ("1", "I"), ("1.5", "I"), ("2.5", "II"), ("5", "III"), ("7.5", "IV"), ("8.5", "V"), ("15", "V")])
def test_five_grade_interior_values(score, grade):
    assert grade_score(Decimal(score)) == (grade, [])


def test_subhealth_can_be_scored_without_inventing_positive_definition():
    result = assess(request(15, 9, 20))
    assert result["barrier"]["score"] == 3.5
    assert result["barrier"]["grade"] == "II"
    assert result["barrier"]["pattern"] is None
    assert result["barrier"]["status"] == "needs_review"


def test_highest_scores_sum_to_fifteen_not_probability():
    result = assess(request(31, 21, 36))
    assert result["barrier"]["score"] == 15
    assert result["barrier"]["grade"] == "V"
    assert all(row["probability"] is None for row in result["risks"])


@pytest.mark.parametrize("bins", list(product(range(5), repeat=3)))
def test_all_125_interior_score_combinations_are_exact(bins):
    values = ((10, 15, 19, 25, 31), (5, 9, 13, 18, 21), (10, 20, 24, 30, 36))
    scores = ((0, 1, 2, 3, 4), (0, 1, 2.5, 3.5, 5), (0, 1.5, 3, 4.5, 6))
    result = assess(request(*(values[index][choice] for index, choice in enumerate(bins))))
    assert result["barrier"]["score"] == sum(scores[index][choice] for index, choice in enumerate(bins))


@pytest.mark.parametrize("label", ["CRP", "C反应蛋白", "常规CRP", "hs-CRP/PA", "超敏C反应蛋白/白蛋白比"])
def test_ordinary_crp_never_substitutes_for_hscrp(label):
    result = assess(request(hospitalData=[hs(name=label)]))
    assert_no_crp_assessment(result)


def test_hscrp_from_nested_hospital_structure_with_parent_date():
    result = assess(request(hospitalData=[{"checkTime": TODAY + " 10:00:00", "items": [{"code": "LAB123", "itemName": "超敏C反应蛋白(hs-CRP)", "testResultValue": "0.42", "unit": "mg/dL"}]}]))
    assert_no_crp_assessment(result)


@pytest.mark.parametrize("value", [0.9, 1, 2, 3, 3.01, 10, 10.01])
def test_retired_crp_categories_do_not_create_risks(value):
    result = assess(request(), observations=[hs(value)])
    assert result["risks"] == assess(request())["risks"]
    assert_no_crp_assessment(result)


@pytest.mark.parametrize("record", [hs(date="2020-01-01"), hs(date=None), hs(date="2020-01-01", isCurrent=True)])
def test_retired_crp_observation_dates_are_not_evaluated(record):
    result = assess(request(), observations=[record])
    assert result == assess(request())
    assert_no_crp_assessment(result)


def test_explicit_sample_date_evaluates_target_sample_not_today():
    result = assess(request(), observations=[{"code": "DAO", "value": 13, "unit": "U/L", "date": "2020-01-01"}], context={"sampleDate": "2020-01-01"})
    assert marker(result, "DAO")["value"] == 13
    assert marker(result, "DAO")["status"] == "evaluated"


def test_conflicting_source_values_not_silently_overwritten():
    result = assess(request(), observations=[{"code": "DAO", "value": 18, "unit": "U/L", "date": TODAY}])
    dao = marker(result, "DAO")
    assert dao["status"] == "needs_review"
    assert "conflicting_source_values" in dao["reasons"]
    assert len(dao["source"]) == 2
    assert result["barrier"]["score"] is None


def test_equal_values_after_dimensionally_valid_conversion_agree():
    result = assess(request(), observations=[{"code": "D_LACTATE", "value": 1.9, "unit": "mg/dL", "date": TODAY}])
    assert result["barrier"]["score"] == 7.5


def test_conflicting_current_dates_need_review():
    observation = {"code": "DAO", "value": 13, "unit": "U/L", "date": TODAY, "isCurrent": True}
    result = assess(request(), observations=[observation, dict(observation, date="2026-09-22")])
    assert "conflicting_current_sample_dates" in marker(result, "DAO")["reasons"]


def test_old_observation_does_not_override_current_direct_value():
    result = assess(request(), observations=[{"code": "DAO", "value": 18, "unit": "U/L", "date": "2020-01-01"}])
    assert marker(result, "DAO")["value"] == 13
    assert len(marker(result, "DAO")["source"]) == 2


def test_endotoxin_units_not_assumed_equivalent():
    result = assess(request(gutHealthIndicator={"bacterialEndotoxin": {"value": 24, "unit": "EU/mL"}}))
    assert marker(result, "LPS")["status"] == "needs_review"
    assert "unsupported_unit" in marker(result, "LPS")["reasons"]


@pytest.mark.parametrize("value", [-1, "<14", "not a number", "1e10000"])
def test_non_exact_or_invalid_measurement_needs_review(value):
    result = assess(request(d=value))
    assert marker(result, "D_LACTATE")["score"] is None
    assert result["barrier"]["status"] == "needs_review"


def test_unknown_context_is_not_filled_from_demo_or_extra_request_fields():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        request(bmi=28.1, symptoms=["腹胀"])
    result = assess(request())
    assert "bmi" not in result["subject"]
    assert "symptoms" not in result["subject"]
    assert not any(row["code"] == "metabolic" for row in result["risks"])
    assert "allergies" in result["missingData"]


def test_verified_context_is_preserved_and_acute_context_blocks_risk_band():
    result = assess(request(), observations=[hs()], context={"bmi": 28.1, "symptoms": ["腹胀"], "acuteInfection": True})
    assert result["subject"]["bmi"] == 28.1
    assert result["subject"]["symptoms"] == ["腹胀"]
    assert result["context"]["acuteInfection"] is True
    assert_no_crp_assessment(result)
    assert not any(row["code"] == "cardiovascular" for row in result["risks"])


def test_assessment_does_not_mutate_input_or_context():
    req = request()
    original = req.model_dump()
    context = {"sampleDate": TODAY}
    observations = [hs()]
    assess_case(req, observations=observations, context=context)
    assert req.model_dump() == original
    assert context == {"sampleDate": TODAY}
    assert observations == [hs()]


@pytest.mark.parametrize("file", [
    {"documentId": "doc-1"},
    {"documentId": "doc-1", "url": "https://example.test/a", "fileType": "pdf"},
    {"url": "https://example.test/a", "fileType": "html"},
    {"url": "https://example.test/a"},
])
def test_file_inputs_reject_unimplemented_references_and_types(file):
    with pytest.raises(ValidationError):
        request(fileList=[file])


def test_historical_lipids_are_risk_facts_not_current_diagnoses():
    row = {"dataType": "INSPECTION", "itemName": "低密度脂蛋白胆固醇", "checkTime": "2024-03-05 08:00:00", "testResultValue": "3.8", "resultStatusName": "偏高", "resultStatus": 2}
    result = assess(request(hospitalData=[row, dict(row)]))
    findings = result["context"]["clinicalFindings"]
    assert len(findings) == 1
    assert len(findings[0]["source"]) == 2
    assert findings[0]["temporalStatus"] == "historical"
    assert findings[0]["unit"] is None
    assert findings[0]["value"] == "3.8"
    assert result["subject"]["clinicalFindings"] == findings
    cardiovascular = next(risk for risk in result["risks"] if risk["code"] == "cardiovascular")
    assert len(cardiovascular["basis"]) == 1
    assert "既往2024-03-05" in cardiovascular["basis"][0]
    assert cardiovascular["probability"] is None
    assert "conditions" not in result["subject"]


def test_total_cholesterol_and_ldl_are_combined_as_risk_basis_not_duplicate_rows():
    rows = [{"itemName": name, "date": "2024-03-05", "value": "6.0", "resultStatusName": "偏高"} for name in ("总胆固醇", "LDL-C")]
    result = assess(request(hospitalData=rows))
    cardiovascular = [risk for risk in result["risks"] if risk["code"] == "cardiovascular"]
    assert len(cardiovascular) == 1 and len(cardiovascular[0]["basis"]) == 2


@pytest.mark.parametrize("status", ["未见升高", "无偏高", "正常", "不高", 2, None])
def test_lipid_risk_needs_explicit_positive_status_not_negation_or_numeric_enum(status):
    row = {"itemName": "总胆固醇", "date": "2024-03-05", "value": "6.0", "resultStatusName": status, "examResult": "否认高脂血症"}
    result = assess(request(hospitalData=[row]))
    assert not any(risk["code"] == "cardiovascular" for risk in result["risks"])
    assert result["subject"]["clinicalFindings"][0]["examResult"] == "否认高脂血症"


def test_examination_text_retained_without_keyword_diagnosis_or_invented_symptoms():
    rows = [
        {"dataType": "EXAM", "itemName": "胃镜", "checkTime": "2024-03-05", "examResult": "慢性胃炎；未见肿瘤。"},
        {"dataType": "EXAM", "itemName": "肠镜", "checkTime": "2024-03-05", "examResult": "未见明显异常；无结肠炎证据。"},
    ]
    result = assess(request(gutHealthIndicator=None, hospitalData=rows))
    assert [finding["examResult"] for finding in result["subject"]["clinicalFindings"]] == [row["examResult"] for row in rows]
    assert result["risks"] == []
    assert "symptoms" not in result["subject"]
    assert "bmi" not in result["subject"]
    assert "conditions" not in result["subject"]


def test_same_date_conflicting_lipid_findings_preserved_but_not_selected():
    row = {"itemName": "LDL-C", "date": "2024-03-05", "value": "3.8", "resultStatusName": "偏高"}
    result = assess(request(hospitalData=[row, {**row, "value": "2.0", "resultStatusName": "正常"}]))
    findings = result["context"]["clinicalFindings"]
    assert len(findings) == 1
    assert findings[0]["conflictingResults"] is True
    assert findings[0]["alternatives"][0]["value"] == "2.0"
    assert not any(risk["code"] == "cardiovascular" for risk in result["risks"])


@pytest.mark.parametrize("source_date", [None, "2030-01-01", "not-a-date"])
def test_unknown_or_future_dates_not_represented_as_historical_lipid_risk(source_date):
    row = {"itemName": "总胆固醇", "date": source_date, "value": "6", "resultStatusName": "偏高"}
    result = assess(request(hospitalData=[row]))
    assert "clinicalFindings" in result["context"]
    assert not any(risk["code"] == "cardiovascular" for risk in result["risks"])


def test_unusual_free_form_metadata_does_not_crash_assessment():
    result = assess(request(hospitalData=[{"itemName": "胃镜", "dataType": {"legacy": "EXAM"}, "examResult": "无明显异常"}]), context={"clinicalFindings": None}, observations=[hs(method={"unexpected": True})])
    assert result["subject"]["clinicalFindings"][0]["examResult"] == "无明显异常"
    assert_no_crp_assessment(result)


def test_gut_health_indicator_uses_three_markers_and_accepts_ignored_crp():
    req = GutBarrierRequest.model_validate({
        "userId": "new-input", "sessionId": "new-input", "age": 28,
        "gutHealthIndicator": {
            "dLacticAcid": {"value": "0.06", "unit": "mg/L"},
            "diamineOxidase": {"value": "3.8", "unit": "U/L"},
            "bacterialEndotoxin": {"value": "12.5", "unit": "U/L"},
            "hsCRP": {"value": ">3.0", "unit": "mg/L"},
        },
    })
    assert "gutHealthIndicator" not in (req.__pydantic_extra__ or {})
    result = assess(req)
    assert result["barrier"]["pattern"] == "---"
    assert result["barrier"]["score"] == 0
    assert_no_crp_assessment(result)


@pytest.mark.parametrize("value", [None, {}])
def test_new_indicator_null_or_empty_is_valid_missing_input(value):
    req = GutBarrierRequest.model_validate({
        "userId": "u", "sessionId": "s", "gutHealthIndicator": value,
    })
    result = assess(req)
    assert result["barrier"]["status"] == "incomplete"
    assert all(item["status"] == "missing" for item in result["markers"])


def test_removed_legacy_envelope_is_rejected_even_when_current_values_are_present():
    with pytest.raises(ValidationError, match="gutBarrierIndicator"):
        request(gutBarrierIndicator={"dLacticAcid": {"value": 18, "unit": "mg/L"}})


def test_comparison_unit_conversion_keeps_source_and_uses_normalized_display():
    normalized, error = normalize_measurement_result("D_LACTATE", ">0.3", "mg/dL")
    assert error is None
    assert normalized["raw"] == ">0.3" and normalized["rawUnit"] == "mg/dL"
    assert normalized["displayValue"] == ">3"
    assert normalized["bounds"]["lower"] == Decimal("3.0")
    req = request(gutHealthIndicator={"dLacticAcid": {"value": ">0.3", "unit": "mg/dL"}})
    result = assess(req)
    current = marker(result, "D_LACTATE")
    assert current["raw"] == ">0.3" and current["rawUnit"] == "mg/dL"
    assert current["displayValue"] == ">3" and current["unit"] == "mg/L"
    assert current["status"] == "needs_review"


def test_empty_comparison_interval_requires_review_not_low_risk():
    parsed, error = normalize_measurement_result("D_LACTATE", "<0", "mg/L")
    assert parsed is None and error == "empty_measurement_interval"
    result = assess(request(gutHealthIndicator={"dLacticAcid": {"value": "<0", "unit": "mg/L"}}))
    assert marker(result, "D_LACTATE")["status"] == "needs_review"
    assert "empty_measurement_interval" in marker(result, "D_LACTATE")["reasons"]
    assert result["barrier"]["score"] is None


def test_parenthesized_lipid_aliases_keep_four_historical_risk_facts_without_units():
    rows = [
        {"itemName": "低密度脂蛋白(LDL-c)", "date": "2025-12-15", "value": "3.70", "resultStatusName": "偏高"},
        {"itemName": "总胆固醇(TC)", "date": "2025-12-15", "value": "6.48", "resultStatusName": "偏高"},
        {"itemName": "总胆固醇（TC）", "date": "2025-10-29", "value": "6.09", "resultStatusName": "偏高"},
        {"itemName": "低密度脂蛋白（LDL-c）", "date": "2025-10-29", "value": "3.44", "resultStatusName": "偏高"},
    ]
    result = assess(request(hospitalData=rows))
    lipid_findings = [item for item in result["context"]["clinicalFindings"] if item["code"]]
    assert [item["code"] for item in lipid_findings] == ["LDL_C", "TOTAL_CHOLESTEROL", "TOTAL_CHOLESTEROL", "LDL_C"]
    assert all(item["unit"] is None for item in lipid_findings)
    cardiovascular = next(row for row in result["risks"] if row["code"] == "cardiovascular")
    assert len(cardiovascular["basis"]) == 4
    assert all("原值" in item for item in cardiovascular["basis"])
    assert not any("mmol" in item or "mg/" in item for item in cardiovascular["basis"])
