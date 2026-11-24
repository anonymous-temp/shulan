"""Hospital records remain clinically available regardless of source identity."""

from copy import deepcopy
import json

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.schemas import GutBarrierRequest
from modules.gut_barrier.source_scope import source_scope_block


TODAY = "2026-10-01"


def _request(rows, **extra):
    return GutBarrierRequest.model_validate({
        "userId": "registered-user", "sessionId": "identity-test-session",
        "name": "登记受检者", "age": 28, "sex": "男", "hospitalData": rows, **extra,
    })


def _inspection(**extra):
    return {"dataType": "INSPECTION", "itemName": "D-乳酸", "testResultValue": "7.2",
            "unit": "mg/L", "reportTime": TODAY, **extra}


def _exam(**extra):
    return {"dataType": "EXAM", "itemName": "内科检查", "reportTime": TODAY,
            "examResult": "急性感染；慢性肾病", **extra}


def _assess(request):
    return assess_case(request, context={"currentDate": TODAY})


def _lactate(assessment):
    return next(marker for marker in assessment["markers"] if marker["code"] == "D_LACTATE")


@pytest.mark.parametrize("identity", [
    {"patientName": "不同受检者"}, {"patient_name": "不同受检者"},
    {"subjectName": "不同受检者"}, {"受检者姓名": "不同受检者"},
    {"userId": "different-user"}, {"user_id": "different-user"},
])
def test_differing_identity_keeps_markers_findings_and_safety(identity):
    rows = [_inspection(**identity), _exam(**identity)]
    before = deepcopy(rows)
    request = _request(rows)
    result = _assess(request)
    assert rows == before and request.hospital_data == before
    assert _lactate(result)["value"] == 7.2
    assert len(result["context"]["clinicalFindings"]) == 2
    assert {signal["code"] for signal in result["context"]["hospitalSafetySignals"]} == {
        "acute_infection", "kidney_problem",
    }
    assert result["context"]["acuteInfection"] is True
    assert "hospitalDataIssues" not in result["context"]
    assert result["subject"]["name"] == "登记受检者"


@pytest.mark.parametrize("identity", [
    {}, {"patientName": " 登记受检者 "}, {"userId": "registered-user"},
    {"name": "另一个检查项目名称"}, {"patientId": "unrelated-hospital-identifier"},
])
def test_matching_or_noncomparable_identity_keeps_existing_records(identity):
    result = _assess(_request([_inspection(**identity), _exam(**identity)]))
    assert _lactate(result)["value"] == 7.2
    assert len(result["context"]["clinicalFindings"]) == 2
    assert {signal["code"] for signal in result["context"]["hospitalSafetySignals"]} == {
        "acute_infection", "kidney_problem",
    }
    assert "hospitalDataIssues" not in result["context"]


@pytest.mark.parametrize("record_key", ["patientId", "patient_id"])
def test_hospital_patient_id_does_not_filter_submitted_measurements(record_key):
    request = _request([_inspection(**{record_key: "another-patient"})])
    result = _assess(request)
    assert _lactate(result)["value"] == 7.2
    assert "hospitalDataIssues" not in result["context"]


@pytest.mark.parametrize("child_key", ["items", "results", "indicators", "data", "inspectionItems", "testItems", "details"])
def test_differing_parent_identity_preserves_subtree(child_key):
    rows = [{"patientName": "不同受检者", child_key: [
        _inspection(patientName="登记受检者"), _exam(userId="registered-user"),
    ]}]
    result = _assess(_request(rows))
    assert _lactate(result)["value"] == 7.2
    assert len(result["context"]["clinicalFindings"]) == 2
    assert {signal["code"] for signal in result["context"]["hospitalSafetySignals"]} == {
        "acute_infection", "kidney_problem",
    }
    assert "hospitalDataIssues" not in result["context"]


def test_nested_identity_differences_preserve_both_siblings():
    rows = [{"patientName": "登记受检者", "results": [
        {"items": [_inspection(patientName="不同受检者")]},
        _exam(patientName="登记受检者"),
    ]}]
    before = deepcopy(rows)
    result = _assess(_request(rows))
    assert rows == before
    assert _lactate(result)["value"] == 7.2
    assert len(result["context"]["clinicalFindings"]) == 2
    assert {row["dataType"] for row in result["context"]["clinicalFindings"]} == {"EXAM", "INSPECTION"}
    assert {signal["code"] for signal in result["context"]["hospitalSafetySignals"]} == {
        "acute_infection", "kidney_problem",
    }
    assert "hospitalDataIssues" not in result["context"]


def test_top_level_patient_name_absent_does_not_infer_mismatch():
    result = _assess(_request([_inspection(patientName="来源受检者")], name=None))
    assert _lactate(result)["value"] == 7.2
    assert "hospitalDataIssues" not in result["context"]


def test_different_identity_does_not_hide_real_disagreement_between_measurements():
    result = _assess(_request([_inspection(patientName="不同受检者")],
                              gutHealthIndicator={"dLacticAcid": {"value": "2.66", "unit": "mg/L"}}))
    assert _lactate(result)["value"] is None and _lactate(result)["status"] == "needs_review"
    assert _lactate(result)["reasons"] == ["conflicting_source_values"]
    assert len(_lactate(result)["source"]) == 2
    assert _lactate(result)["source"][0]["source"]["path"] == "gutHealthIndicator.dLacticAcid"
    assert _lactate(result)["source"][1]["value"] == "7.2"
    assert "hospitalDataIssues" not in result["context"]


def test_obsolete_identity_issue_cannot_publish_exclusion_claim():
    scope = source_scope_block([{"code": "HOSPITAL_DATA_IDENTITY_CONFLICT", "patientName": "不同受检者"}], [])
    assert scope is None


@pytest.mark.asyncio
async def test_pipeline_includes_hospital_data_without_identity_conflict_audit():
    request = _request([_inspection(patientName="不同受检者"), _exam(patientName="不同受检者")],
                       gutHealthIndicator={"dLacticAcid": {"value": "2.66", "unit": "mg/L"}})
    report, audit = await GutBarrierPipeline().generate(request, report_id="hospital-identity-report", evaluation_date=TODAY)
    assert audit["partial"] is True
    assert not any("IDENTITY_CONFLICT" in issue["code"] for issue in audit["issues"])
    assert _lactate(report["assessment"])["value"] == 2.66
    assert _lactate(report["assessment"])["source"][0]["source"]["path"] == "gutHealthIndicator.dLacticAcid"
    assert any(row["value"] == "7.2" for row in report["assessment"]["context"]["clinicalFindings"])
    assert len(report["assessment"]["context"]["hospitalSafetySignals"]) == 2
    assert len(report["assessment"]["context"]["clinicalFindings"]) == 2
    public = project_callback_report(report)
    rendered = json.dumps(public, ensure_ascii=False)
    assert "受检者信息不一致的院内检查、检验记录未纳入评估。" not in rendered
    assert "2.66 mg/L" in rendered and "急性感染" in rendered
    assert "不同受检者" not in rendered and "HOSPITAL_DATA_IDENTITY_CONFLICT" not in rendered
