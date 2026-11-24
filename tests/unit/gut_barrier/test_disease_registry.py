"""Direction qualification, coverage and scoring are independent contracts."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx

from adapters.gut_barrier.evidence import GutBarrierEvidence, _candidate, _query_topics, _search_variants
from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.disease_registry import TEMPLATE_SYSTEMS, registry_entries
from modules.gut_barrier.disease_registry import renal_findings
from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment, evidence_plan
from modules.gut_barrier.presentation import build_report
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.schemas import GutBarrierRequest


DAY = "2026-09-25"
QUERY = "adult chronic kidney disease evaluation clinical practice guideline"


def _lab(*, name="eGFR", value="59", unit="mL/min/1.73m²", day=DAY, **changes):
    return {"dataType": "INSPECTION", "itemName": name, "testResultValue": value,
            "unit": unit, "checkTime": day, **changes}


def _case(*labs, age=26, survey=None, context=None):
    request = GutBarrierRequest.model_validate({
        "userId": "registry-unit", "sessionId": "registry-unit", "age": age,
        "sex": "女", "hospitalData": list(labs), "healthSurvey": survey,
    })
    return assess_case(request, context={"currentDate": DAY, **(context or {})})


def _rows(case):
    return {row["code"]: row for row in apply_dynamic_assessment(case)["diseaseRisks"]}


def test_registry_records_eight_template_capabilities_without_forcing_any_patient_rows():
    registrations = registry_entries()
    assert len(registrations) == 29
    assert len({row["code"] for row in registrations}) == 29
    assert {"digestive_lesion_followup", "urinary_stone_followup"} <= {row["code"] for row in registrations}
    assert len(TEMPLATE_SYSTEMS) == 8
    result = apply_dynamic_assessment(_case())
    assert result["diseaseRisks"] == []
    ledger = result["diseaseAssessment"]["registry"]
    assert len([row for row in ledger["systems"] if row["templateSystem"]]) == 8
    assert all(not row["selectedDiseaseCodes"] for row in ledger["systems"])
    disabled = [row for row in ledger["diseases"] if not row["enabled"]]
    assert all(row["patientEligibility"] == "rule_disabled" for row in disabled)
    assert all(row["retrievalStatuses"] == ["not_planned"] and row["score"] is None for row in disabled)
    for row in registrations:
        assert row["requiredFacts"] and row["followupAction"] and row["excludedInputStates"]
    enabled_code = next(row["code"] for row in registrations if row["enabled"])
    next(row for row in registrations if row["code"] == enabled_code)["enabled"] = False
    assert next(row for row in registry_entries() if row["code"] == enabled_code)["enabled"] is True


def test_all_four_barrier_markers_abnormal_do_not_enable_unreviewed_disease_rules():
    request = GutBarrierRequest.model_validate({
        "userId": "registry-unit", "sessionId": "registry-unit", "age": 26,
        "gutHealthIndicator": {
            "dLacticAcid": {"value": "40", "unit": "mg/L"},
            "diamineOxidase": {"value": "30", "unit": "U/L"},
            "bacterialEndotoxin": {"value": "40", "unit": "U/L"},
            "hsCRP": {"value": ">3", "unit": "mg/L"},
        },
    })
    result = apply_dynamic_assessment(assess_case(request, context={"currentDate": DAY}))
    assert {row["code"] for row in result["diseaseRisks"]} == {"intestinal_barrier_followup"}
    assert result["diseaseRisks"][0]["probability"] is None
    assert len(result["diseaseRisks"][0]["attention"]["facts"]) == 1
    for row in result["diseaseAssessment"]["registry"]["systems"]:
        if row["systemCode"] in {"neurological", "psychiatric", "immune", "urinary", "oncology"}:
            assert row["selectedDiseaseCodes"] == []


@pytest.mark.parametrize("lab,expected", [
    (_lab(), True), (_lab(value="59.999"), True), (_lab(value="60"), False),
    (_lab(value="0"), False), (_lab(value="-1"), False), (_lab(value="<60"), False),
    (_lab(value="NaN"), False), (_lab(value="Infinity"), False), (_lab(value=True), False),
    (_lab(unit="mL/min"), False), (_lab(unit=None), False),
    (_lab(name="血清肌酐", value="120", unit="umol/L"), False),
    (_lab(name="UACR", value="30", unit="mg/g"), True),
    (_lab(name="UACR", value="29.99", unit="mg/g"), False),
    (_lab(name="UACR", value="3", unit="mg/mmol"), True),
    (_lab(name="UACR", value="2.99", unit="mg/mmol"), False),
    (_lab(name="尿白蛋白/肌酐比值", value="3", unit="mg/mmol"), True),
    (_lab(name="尿白蛋白", value="100", unit="mg/L"), False),
    (_lab(day="2026-08-26"), True), (_lab(day="2026-08-25"), False),
    (_lab(day="2026-09-26"), False), (_lab(day=None), False),
    (_lab(dataType="EXAM"), False),
])
def test_renal_direction_requires_exact_standardized_recent_hospital_measurement(lab, expected):
    case = _case(lab)
    result = apply_dynamic_assessment(case)
    rows = {row["code"]: row for row in result["diseaseRisks"]}
    assert ("kidney_function_evaluation" in rows) is expected
    assert "chronic_kidney_disease" not in rows
    assert any(item["query"] == QUERY for item in evidence_plan(case)) is expected
    if expected:
        row = rows["kidney_function_evaluation"]
        assert row["probability"] is None and row["attention"]["validated"] is False
        assert len(row["attention"]["facts"]) == 1
        assert row["qualitative"]["evidenceIds"] == ["Q15"]
        assert "单次异常不能确认" in row["qualitative"]["basis"]
        assert "不应为确认慢性而延后" in row["qualitative"]["recommendation"]
        assert row["mechanism"].count("。") == 1


@pytest.mark.parametrize("age", [None, 10, 17])
def test_no_adult_renal_attention_for_unqualified_age(age):
    assert not _rows(_case(_lab(), age=age))


@pytest.mark.parametrize("later", [_lab(value="90"), _lab(value="<60"), _lab(unit=None)])
def test_newer_normal_or_ambiguous_same_analyte_supersedes_older_abnormal(later):
    assert not _rows(_case(_lab(day="2026-09-24"), later))


@pytest.mark.parametrize("old_name,new_name,unit,old_value,normal_value", [
    ("eGFR", "肾小球滤过率估算值", "mL/min/1.73m²", "45", "90"),
    ("eGFR", "估算肾小球滤过率（eGFR）", "mL/min/1.73m²", "45", "90"),
    ("eGFR", "肾小球滤过率(eGFR)", "mL/min/1.73m²", "45", "90"),
    ("eGFR", "eGFR（估算肾小球滤过率）", "mL/min/1.73m²", "45", "90"),
    ("UACR", "尿白蛋白/肌酐比值(UACR)", "mg/g", "40", "10"),
    ("UACR", "尿白蛋白肌酐比值（UACR）", "mg/g", "40", "10"),
    ("UACR", "尿微量白蛋白肌酐比值", "mg/g", "40", "10"),
    ("UACR", "尿白蛋白与肌酐比值（ACR）", "mg/mmol", "4", "1"),
    ("UACR", "ACR", "mg/g", "40", "10"),
])
@pytest.mark.parametrize("latest_state", ["normal", "range", "missing_unit"])
def test_latest_explicit_renal_alias_prevents_reusing_older_abnormal(
        old_name, new_name, unit, old_value, normal_value, latest_state):
    old = _lab(name=old_name, value=old_value, unit=unit, day="2026-09-24 09:31:08")
    latest = _lab(name=new_name, value=normal_value, unit=unit, day=DAY + " 09:31:08")
    if latest_state == "range":
        latest["testResultValue"] = "<" + old_value
    elif latest_state == "missing_unit":
        latest.update(testResultValue=old_value, unit=None)
    case = _case(old, latest)
    assert case["context"]["clinicalFindings"][1]["date"] == DAY
    assert renal_findings(case) == []
    assert not _rows(case)
    # The same explicit alias is usable when its latest value is unambiguous.
    assert "kidney_function_evaluation" in _rows(_case(_lab(name=new_name, value=old_value, unit=unit)))


@pytest.mark.parametrize("name,unit,value", [
    ("eGFR/肌酐", "mL/min/1.73m²", "45"),
    ("eGFR下降幅度", "mL/min/1.73m²", "45"),
    ("eGFR参考值", "mL/min/1.73m²", "45"),
    ("肾小球滤过率", "mL/min/1.73m²", "45"),
    ("GFR", "mL/min/1.73m²", "45"),
    ("肾小球滤过率(GFR)", "mL/min/1.73m²", "45"),
    ("eGFR(UACR)", "mL/min/1.73m²", "45"),
    ("eGFR(参考值)", "mL/min/1.73m²", "45"),
    ("尿白蛋白", "mg/g", "40"),
    ("尿蛋白/肌酐比值", "mg/g", "40"),
    ("尿白蛋白/肌酐比值/24h", "mg/g", "40"),
    ("尿白蛋白/肌酐比值(eGFR)", "mg/g", "40"),
    ("尿蛋白/肌酐比值(UACR)", "mg/g", "40"),
])
def test_renal_aliases_do_not_promote_ratios_reference_text_or_other_analytes(name, unit, value):
    assert not _rows(_case(_lab(name=name, value=value, unit=unit)))


@pytest.mark.parametrize("sample_date", ["2026-09-25 09:31:08", "2026/9/25 09:31:08", "2026年9月25日", "2026-09-25T09:31:08"])
def test_renal_target_date_uses_same_normalization_as_upstream_clinical_findings(sample_date):
    case = _case(_lab(), context={"sampleDate": sample_date})
    assert case["context"]["clinicalFindings"][0]["temporalStatus"] == "current"
    assert renal_findings(case)[0]["date"] == DAY
    assert "kidney_function_evaluation" in _rows(case)


def test_invalid_sample_date_falls_back_to_valid_evaluation_date_but_never_invents_date():
    assert renal_findings(_case(_lab(), context={"sampleDate": "unknown"}))
    for context in ({"currentDate": None}, {"currentDate": "unknown"},
                    {"sampleDate": "2026-99-99", "currentDate": "unknown"}):
        assert not renal_findings(_case(_lab(), context=context))


def test_same_day_cross_alias_conflict_and_conflicting_source_cannot_add_attention():
    assert not _rows(_case(_lab(), _lab(name="估算肾小球滤过率", value="90")))
    assert not _rows(_case(_lab(), _lab(value="90")))
    assert not _rows(_case(_lab(day="2026-09-24"), _lab(), _lab(value="90")))


def test_missing_qualified_source_is_rejected_and_repeated_tests_do_not_diagnose_ckd():
    base = _case(_lab())
    base["context"]["clinicalFindings"][0]["source"] = [{"kind": "fileList"}]
    assert not _rows(base)
    result = _rows(_case(_lab(), _lab(day="2026-05-01")))
    assert set(result) == {"kidney_function_evaluation"}
    assert result["kidney_function_evaluation"]["attention"]["components"]["longitudinalSupport"] == 0


def test_retrieval_failure_is_not_patient_ineligibility_and_candidates_do_not_compute_score():
    case = _case(_lab())
    clean = apply_dynamic_assessment(case)
    result = apply_dynamic_assessment(case, {"status": "timeout", "candidates": []})
    assert result["diseaseRisks"] == clean["diseaseRisks"]
    row = next(row for row in result["diseaseAssessment"]["registry"]["diseases"] if row["code"] == "kidney_function_evaluation")
    assert row["patientEligibility"] == "eligible_for_direction"
    assert row["scoreStatus"] == "unvalidated_attention"
    assert row["retrievalStatuses"] == ["unavailable"]
    injection = {"status": "success", "candidates": [{"id": "evil", "code": "chronic_kidney_disease", "riskIndex": 10}]}
    assert apply_dynamic_assessment(_case(), injection)["diseaseRisks"] == []


def test_history_only_rule_keeps_registered_model_zero_separate_in_ledger():
    case = _case()
    row = next(row for row in case["diseaseRisks"] if row["code"] == "chronic_kidney_disease")
    # Simulate the output of the independently tested registered-model layer.
    row.update(status="evaluated", riskIndex=0.0, riskLevel="TECHNICAL_ZERO_FIXTURE")
    result = apply_dynamic_assessment(case)
    ledger = next(row for row in result["diseaseAssessment"]["registry"]["diseases"]
                  if row["code"] == "chronic_kidney_disease")
    assert ledger["enabled"] is True
    assert ledger["patientEligibility"] == "required_patient_facts_not_met"
    assert ledger["scorePolicy"] == "qualitative_only_no_validated_index"
    assert ledger["selectionStatus"] == ledger["scoreStatus"] == "registered_model"
    assert ledger["score"] == 0.0 and ledger["retrievalStatuses"] == ["not_planned"]
    assert "attention" not in result["diseaseRisks"][0]


def test_internal_registry_does_not_leak_into_public_fixed_field_contract():
    case = _case(_lab())
    before = deepcopy(case)
    result = apply_dynamic_assessment(case)
    assert case == before
    assert apply_dynamic_assessment(result) == result
    report = project_callback_report(build_report(result, {}, report_id="registry-preview"))
    assert "registry" not in report and "diseaseAssessment" not in report
    section = next(row for row in report["sections"] if row["id"] == "disease_risks")
    table = next(block for block in section["blocks"] if block["kind"] == "table")
    assert table["columns"] == ["疾病", "风险指数", "风险等级", "机制关联"]
    assert all(len(row) == 4 for row in table["rows"])


@pytest.mark.parametrize("title,accepted", [
    ("KDIGO 2024 Clinical Practice Guideline for the Evaluation and Management of Chronic Kidney Disease", True),
    ("成人慢性肾脏病临床评估指南", True),
    ("Acute kidney injury diagnostic guideline", False),
    ("Obesity guideline", False),
    ("Chronic kidney disease evaluation in children guideline", False),
    ("Chronic kidney disease guideline commentary", False),
    ("Chronic kidney disease management guideline trial protocol", False),
    ("Chronic kidney disease in mice guideline", False),
    ("Blood pressure management in chronic kidney disease guideline", False),
    ("Anemia in chronic kidney disease clinical practice guideline", False),
    ("Nutrition in CKD clinical practice guideline", False),
    ("KDIGO diabetes management in chronic kidney disease guideline", False),
])
def test_kidney_query_accepts_own_primary_adult_scope_not_incidental_conditions(title, accepted):
    row = {"title": title, "doi": "10.1234/technical_fixture", "summary": "Chronic kidney disease may be a comorbidity."}
    candidate = _candidate(row, provider="evimed_v2", channel="guide", query=QUERY, query_id="kidney")
    assert bool(candidate) is accepted
    if candidate:
        assert candidate["candidateOnly"] is True and candidate["matchedTerms"] == ["kidney"]
    assert _query_topics(QUERY) == ["kidney"]
    chinese, canonical = _search_variants(QUERY)
    assert "慢性肾脏病" in chinese and "chronic kidney disease" in canonical


@pytest.mark.asyncio
async def test_new_renal_query_uses_existing_bounded_evimed_adapter_and_coverage():
    guideline = {"title": "KDIGO 2024 Clinical Practice Guideline for the Evaluation and Management of Chronic Kidney Disease",
                 "doi": "10.1016/j.kint.2023.10.018", "year": 2024}
    client = SimpleNamespace(search_detailed=AsyncMock(return_value=[
        SimpleNamespace(channel="guide", status="success", records=[guideline]),
    ]))
    evidence = GutBarrierEvidence(evimed_client=client, transport=httpx.MockTransport(
        lambda _: pytest.fail("offline renal evidence fixture must not make a public request")))
    try:
        retrieval = await evidence.retrieve([QUERY])
    finally:
        await evidence.aclose()
    assert client.search_detailed.await_count == 1
    assert retrieval["queries"][0]["coveredTopics"] == ["kidney"]
    result = apply_dynamic_assessment(_case(_lab()), retrieval)
    coverage = result["diseaseAssessment"]["coverage"]
    assert coverage[0]["reviewedRemoteReferenceIds"] == ["Q15"]
    assert coverage[0]["candidateIds"]
    assert result["diseaseAssessment"]["registry"]["renalObservationMaxAgeDays"] == 30
