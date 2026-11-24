"""Only the current request field names are accepted; metadata cannot invent results."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.schemas import GutBarrierRequest
from tests.unit.gut_barrier.final_input_fixture import current_health_input


@pytest.mark.parametrize("field", ["gutBarrierIndicator", "gut_health_indicator"])
def test_old_indicator_envelope_is_rejected_instead_of_silently_losing_values(field):
    payload = current_health_input()
    payload[field] = payload.pop("gutHealthIndicator")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        GutBarrierRequest.model_validate(payload)


@pytest.mark.parametrize("field", ["dlacticAcid", "DLACTICACID", "d_lactic_acid", "hscrp", "hs_crp"])
def test_indicator_aliases_are_not_supported(field):
    payload = current_health_input()
    canonical = "hsCRP" if field.startswith("hs") else "dLacticAcid"
    payload["gutHealthIndicator"][field] = payload["gutHealthIndicator"].pop(canonical)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        GutBarrierRequest.model_validate(payload)


@pytest.mark.parametrize("marker", [
    {"Value": "1.02", "Unit": "mg/L"},
    {"result": "1.02", "units": "mg/L"},
    {"value": "1.02", "unit": "mg/L", "unexpected": True},
])
def test_measurement_keys_must_match_instead_of_silently_becoming_missing(marker):
    payload = current_health_input()
    payload["gutHealthIndicator"]["dLacticAcid"] = marker
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        GutBarrierRequest.model_validate(payload)


@pytest.mark.parametrize("metadata", [
    {"file_type": "pdf"}, {"fileType": "pdf", "unexpected": True},
])
def test_file_metadata_uses_only_current_keys(metadata):
    payload = current_health_input()
    payload["fileList"] = [{"url": "https://example.com/report.pdf", **metadata}]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        GutBarrierRequest.model_validate(payload)


@pytest.mark.parametrize("complete", [True, False, None, "absent"])
@pytest.mark.parametrize("missing_hscrp", [False, True])
def test_current_input_roundtrip_uses_measurements_not_complete_flag(complete, missing_hscrp):
    payload = current_health_input()
    if complete == "absent":
        payload["gutHealthIndicator"].pop("complete")
    else:
        payload["gutHealthIndicator"]["complete"] = complete
    if missing_hscrp:
        payload["gutHealthIndicator"]["hsCRP"] = {}
    original = deepcopy(payload)
    request = GutBarrierRequest.model_validate(payload)
    restored = GutBarrierRequest.model_validate_json(request.model_dump_json(by_alias=True))
    assert restored == request and payload == original
    assert restored.age == 42 and restored.sex == "男"
    assert restored.file_list == restored.test_data == restored.hospital_data == []
    assert restored.microbiome_data is None
    assert restored.gut_health_indicator.complete is (None if complete == "absent" else complete)
    assessment = assess_case(restored, context={"currentDate": "2026-10-01"})
    markers = {m["code"]: m for m in assessment["markers"]}
    assert {code: markers[code]["value"] for code in ("D_LACTATE", "DAO", "LPS")} == {
        "D_LACTATE": 12.77, "DAO": 24.9, "LPS": 11.9}
    assert "HS_CRP" not in markers
    assert "hsCRP" not in restored.model_dump(by_alias=True)["gutHealthIndicator"]
    assert assessment["barrier"]["score"] == 5.0 and assessment["barrier"]["grade"] == "III"
    assert assessment["barrier"]["pattern"] == "-+-"
    assert assessment["subject"]["name"] == payload["name"]
    assert not any("IDENTITY" in i["code"] for i in assessment["context"]["healthSurveyIssues"])
    assert "bmi" not in assessment["context"]
    assert "35.2 kg/m²" in dict(assessment["context"]["healthSurveyRows"])["BMI"]
    assert assessment["context"]["symptoms"] == ["消化不良", "时常腹泻"]
    assert assessment["context"]["skinConcerns"] == ["长痘痤疮", "暗沉发黄"]


@pytest.mark.parametrize("raw,unit", [(0, "mg/L"), (">3.0", "mg/L"), ("1.02", "mg/L")])
def test_legacy_hscrp_accepts_but_discards_zero_and_comparison_values(raw, unit):
    payload = current_health_input()
    payload["gutHealthIndicator"]["hsCRP"] = {"value": raw, "unit": unit}
    request = GutBarrierRequest.model_validate(payload)
    assert request.gut_health_indicator.hs_crp is None
    assert all(row["code"] != "HS_CRP" for row in assess_case(request)["markers"])


@pytest.mark.parametrize("invalid", [{"value": True, "unit": "mg/L"},
                                     {"value": "NaN", "unit": "mg/L"},
                                     "2 mg/L"])
def test_measurement_sanity_checks_are_not_removed_with_identity_checks(invalid):
    payload = current_health_input()
    payload["gutHealthIndicator"]["dLacticAcid"] = invalid
    with pytest.raises(ValidationError):
        GutBarrierRequest.model_validate(payload)


@pytest.mark.parametrize("age,expected", [("26岁", 26), (" 26 岁 ", 26), ("26.0岁", 26),
                                         ("0岁", 0), ("  ", None), ("N/A", None), ("未填写", None)])
def test_explicit_age_formats_and_missing_placeholders_are_compatible(age, expected):
    payload = current_health_input()
    payload["age"] = age
    request = GutBarrierRequest.model_validate(payload)
    assert request.age == expected
    assert assess_case(request)["subject"]["age"] == expected


@pytest.mark.parametrize("age", [True, "成年", "26至30", "26.5岁", -1, 151])
def test_uncertain_or_invalid_age_is_not_guessed(age):
    payload = current_health_input()
    payload["age"] = age
    with pytest.raises(ValidationError):
        GutBarrierRequest.model_validate(payload)


@pytest.mark.parametrize("raw,unit,expected_status", [(2, None, "needs_review"), (2, "N/A", "needs_review"),
                                                     (None, "mg/L", "missing"), ("未检测", "mg/L", "missing")])
def test_incomplete_measurement_does_not_reject_request_or_invent_missing_value(raw, unit, expected_status):
    payload = current_health_input()
    payload["gutHealthIndicator"]["dLacticAcid"] = {"value": raw, "unit": unit}
    request = GutBarrierRequest.model_validate(payload)
    marker = next(row for row in assess_case(request)["markers"] if row["code"] == "D_LACTATE")
    assert marker["status"] == expected_status
    assert marker["value"] is None and marker["score"] is None
    if expected_status == "needs_review":
        assert request.gut_health_indicator.d_lactic_acid.value == raw
        assert request.gut_health_indicator.d_lactic_acid.unit is None
        assert marker["source"][0]["value"] == raw and marker["source"][0]["unit"] is None


@pytest.mark.parametrize("field", ["fileList", "testData", "hospitalData"])
@pytest.mark.parametrize("empty", [None, "", "N/A", "未提供"])
def test_optional_source_list_missing_placeholders_do_not_reject_request(field, empty):
    payload = current_health_input()
    payload[field] = empty
    request = GutBarrierRequest.model_validate(payload)
    assert request.model_dump(by_alias=True)[field] == []


@pytest.mark.parametrize("field", ["testData", "hospitalData"])
def test_structured_source_with_empty_url_keeps_other_source_fields(field):
    payload = current_health_input()
    payload[field] = [{"resultSummaryUrl": "N/A", "value": "原始资料", "date": "2026-10-01"}]
    request = GutBarrierRequest.model_validate(payload)
    assert request.model_dump(by_alias=True)[field] == [
        {"resultSummaryUrl": None, "value": "原始资料", "date": "2026-10-01"}]
