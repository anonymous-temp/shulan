"""A hospital endoscopy history is a qualified fact, not a disease score.

Fixtures are synthetic and test extraction/contract behavior, not calibration.
"""
from copy import deepcopy
import json

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.clinical_facts import gastritis_findings
from modules.gut_barrier.disease_registry import registry_entries
from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment, evidence_plan
from modules.gut_barrier.presentation import build_report
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.schemas import GutBarrierRequest


DAY = "2026-09-28"
PAST = "2025-12-18"
QUERY = "adult chronic non-atrophic gastritis evaluation clinical guideline"


def _exam(text="慢性非萎缩性胃炎", *, day=PAST, **extra):
    return {"dataType": "EXAM", "itemName": "无痛胃镜检查(附带肠镜)（胃）",
            "checkTime": day, "examResult": text, **extra}


def _case(*exams, survey=None, age=31, context=None):
    request = GutBarrierRequest.model_validate({
        "userId": "gastritis-synthetic", "sessionId": "gastritis-synthetic",
        "name": "合成胃镜样本", "age": age, "sex": "男",
        "healthSurvey": survey, "hospitalData": list(exams),
    })
    return assess_case(request, context={"currentDate": DAY, **(context or {})})


def _row(assessment):
    return next((row for row in apply_dynamic_assessment(assessment)["diseaseRisks"]
                 if row["code"] == "gastritis_followup"), None)


def test_historical_gastroscopy_is_a_non_numeric_followup_with_reviewed_sources():
    case = _case(_exam("慢性非萎缩性胃炎\r术后注意事项：如果出现腹痛，及时就诊。"))
    before = deepcopy(case)
    result = apply_dynamic_assessment(case)
    row = result["diseaseRisks"][0]
    assert case == before
    assert row["code"] == "gastritis_followup"
    assert row["name"] == "既往胃炎随访" and row["systemCode"] == "digestive"
    assert row["status"] == "qualitative_followup" and row["riskLevel"] == "随访关注"
    assert row["riskIndex"] is row["probability"] is row["attention"] is None
    assert row["qualitative"]["evidenceIds"] == ["Q16", "Q17", "Q18"]
    assert "2025-12-18" in row["qualitative"]["basis"]
    assert "术后注意事项" not in row["mechanism"]
    assert all(prefix not in row["mechanism"] for prefix in ("机制关联：", "本次依据：", "随访重点："))
    assert row["mechanism"].count("。") == 1
    assert row["qualitative"]["basis"] and row["qualitative"]["recommendation"]
    assert "胃黏膜" in row["mechanism"]
    assert {row["code"] for row in result["diseaseRisks"]} == {"gastritis_followup"}
    assert {ref["doi"] for ref in result["diseaseAssessment"]["references"]} == {None, "10.1111/1751-2980.13193"}
    assert all(item["reviewDate"] == "2026-09-28" for item in result["diseaseAssessment"]["appliedEvidence"])
    assert apply_dynamic_assessment(result) == result


@pytest.mark.parametrize("text", [
    "未见慢性非萎缩性胃炎", "否认慢性非萎缩性胃炎", "排除慢性非萎缩性胃炎",
    "慢性非萎缩性胃炎：阴性", "慢性非萎缩性胃炎（-）", "慢性非萎缩性胃炎未证实",
    "考虑慢性非萎缩性胃炎", "慢性非萎缩性胃炎？", "疑似慢性非萎缩性胃炎",
    "不能排除慢性非萎缩性胃炎", "慢性非萎缩性胃炎待排", "慢性非萎缩性胃炎，已治愈",
    "家族史：母亲慢性非萎缩性胃炎", "如果存在慢性非萎缩性胃炎，应由医师评估",
    "预防慢性非萎缩性胃炎", "慢性非萎缩性胃炎的发生机制", "慢性非萎缩性胃炎的诊断标准",
    "临床诊断：慢性非萎缩性胃炎\n检查结论：未见明显异常",
    "检查目的：排除慢性非萎缩性胃炎", "慢性非萎缩性胃炎；复核后排除慢性非萎缩性胃炎",
    "胃黏膜未见明显异常。术后注意事项：慢性非萎缩性胃炎患者应避免刺激性食物。",
    "所见结直肠黏膜大致正常", "慢性萎缩性胃炎", "胃炎", "", None,
    "No chronic non-atrophic gastritis", "Chronic non-atrophic gastritis, resolved",
    "慢性非萎缩性胃炎：无", "慢性非萎缩性胃炎：否", "并非慢性非萎缩性胃炎",
    "家属患慢性非萎缩性胃炎", "配偶患慢性非萎缩性胃炎", "丈夫患慢性非萎缩性胃炎",
    "Spouse has chronic non-atrophic gastritis", "Child has chronic non-atrophic gastritis",
])
def test_nonaffirmative_and_teaching_text_do_not_create_a_followup(text):
    assert gastritis_findings(_case(_exam(text))) == []
    assert _row(_case(_exam(text))) is None


@pytest.mark.parametrize("text", [
    "慢性非萎缩性胃炎", "内镜结论：慢性非萎缩性胃炎",
    "未见食管病变，慢性非萎缩性胃炎", "胃镜提示：慢性 非萎缩性 胃炎",
    "Chronic non-atrophic gastritis",
    "术后注意事项：按时就诊\n检查结论：慢性非萎缩性胃炎",
])
def test_explicit_result_variants_preserve_the_positive_gastric_record(text):
    assert _row(_case(_exam(text))) is not None


@pytest.mark.parametrize("changes", [
    {"day": None}, {"day": "不详"}, {"day": "2026-09-29"},
    {"dataType": "INSPECTION"}, {"itemName": "无痛肠镜检查（肠）"},
    {"itemName": "患者自述"},
])
def test_source_type_procedure_and_date_are_required(changes):
    assert _row(_case(_exam(**changes))) is None


@pytest.mark.parametrize("changes", [
    {"conflict": True}, {"conflictingResults": True}, {"status": "needs_review"},
    {"status": "conflict"}, {"temporalStatus": "date_unknown"}, {"date": "2026-09-29"},
    {"source": [{"kind": "healthSurvey"}]}, {"source": []},
])
def test_unaccepted_clinical_ledger_records_are_not_reinterpreted(changes):
    case = _case(_exam())
    case["context"]["clinicalFindings"][0].update(changes)
    assert gastritis_findings(case) == []


def test_current_record_is_not_mislabeled_as_historical_and_children_are_not_included():
    assert _row(_case(_exam(day=DAY)))["name"] == "胃炎检查记录随访"
    assert _row(_case(_exam(), age=17)) is None


def test_questionnaire_does_not_change_hospital_followup_with_different_demographics():
    case = _case(_exam(), survey={"name": "其他合成样本", "age": "26", "gender": "女",
                                  "heightCm": "160", "weightKg": "90", "digestion": "消化不良",
                                  "bowelMovement": "时常腹泻"})
    rows = apply_dynamic_assessment(case)["diseaseRisks"]
    assert {row["code"] for row in rows} == {"gastritis_followup"}
    assert rows == apply_dynamic_assessment(_case(_exam()))["diseaseRisks"]
    assert "bmi" not in case["context"]
    assert case["context"]["symptoms"] == ["消化不良", "时常腹泻"]
    assert not any(issue["code"] == "HEALTH_SURVEY_IDENTITY_CONFLICT"
                   for issue in case["context"].get("healthSurveyIssues", []))
    followup = next(row for row in rows if row["code"] == "gastritis_followup")
    assert followup["status"] == "qualitative_followup"
    assert followup["riskIndex"] is None
    assert _row(_case(survey={"pastDisease": "慢性非萎缩性胃炎"})) is None


def test_conflicting_duplicates_are_not_accepted_and_exact_duplicates_keep_provenance():
    assert _row(_case(_exam(), _exam("未见慢性非萎缩性胃炎"))) is None
    case = _case(_exam(), _exam())
    facts = gastritis_findings(case)
    assert len(facts) == 1 and len(facts[0]["source"]) == 2
    assert len(apply_dynamic_assessment(case)["diseaseRisks"]) == 1


def test_evidence_plan_is_topic_specific_and_remote_search_is_not_claimed_for_local_sources():
    case = _case(_exam())
    plan = evidence_plan(case)
    assert len(plan) == 1 and plan[0]["query"] == QUERY
    assert plan[0]["topic"] == "gastritis"
    assert plan[0]["diseaseCodes"] == ["gastritis_followup"]
    result = apply_dynamic_assessment(case, {"status": "success", "queries": [], "candidates": []})
    coverage = result["diseaseAssessment"]["coverage"][0]
    assert coverage["basisSource"] == "reviewed_reference_library"
    assert coverage["reviewedRemoteReferenceIds"] == []
    assert coverage["retrievalStatus"] == "not_attempted"


def test_registry_audit_distinguishes_enabled_followup_from_missing_patient_data():
    registrations = registry_entries()
    disabled = [row for row in registrations if not row["enabled"]]
    assert {row["code"] for row in disabled} == {"ibs", "ibd"}
    history_rules = [row for row in registrations if (row["predicate"] or "").startswith("adult_documented_diagnosis_")]
    assert len(history_rules) == 14
    assert all(row["scorePolicy"] == "qualitative_only_no_validated_index" for row in history_rules)
    result = apply_dynamic_assessment(_case(_exam()))
    rows = {row["code"]: row for row in result["diseaseAssessment"]["registry"]["diseases"]}
    assert rows["gastritis_followup"]["patientEligibility"] == "eligible_for_direction"
    assert rows["gastritis_followup"]["score"] is None
    assert rows["gastritis_followup"]["scoreNotComputedReason"] == "qualitative_followup_without_validated_scoring_rule"
    assert rows["gastritis_followup"]["scorePolicy"] == "qualitative_only_no_validated_index"
    assert rows["colorectal_cancer"]["patientEligibility"] == "required_patient_facts_not_met"
    assert all(rows[rule["code"]]["patientEligibility"] == "required_patient_facts_not_met" for rule in history_rules)
    assert all(rows[rule["code"]]["score"] is None for rule in history_rules)


def test_public_report_preserves_existing_table_shape_and_null_score():
    result = apply_dynamic_assessment(_case(_exam()))
    public = project_callback_report(build_report(result, {}, report_id="gastritis-contract"))
    section = next(item for item in public["sections"] if item["id"] == "disease_risks")
    table = next(block for block in section["blocks"] if block.get("columns") == ["疾病", "风险指数", "风险等级", "机制关联"])
    assert set(table) == {"kind", "title", "columns", "rows"}
    assert len(table["rows"]) == 1 and len(table["rows"][0]) == 4
    assert table["rows"][0][:3] == ["既往胃炎随访", None, "随访关注"]
    serialized = json.dumps(public, ensure_ascii=False)
    assert "gastritisFindings" not in serialized and "disabledReason" not in serialized
    assert project_callback_report(public) == public


def test_registered_model_result_is_not_overwritten_by_the_qualitative_rule():
    case = _case(_exam())
    model = {"code": "gastritis_followup", "systemCode": "digestive", "systemName": "消化系统",
             "name": "合成已配置模型", "status": "evaluated", "riskIndex": 0, "riskLevel": "模型0档",
             "probability": 0, "mechanism": "合成模型说明", "source": {"model": "synthetic"}}
    case["diseaseRisks"].append(model)
    row = _row(case)
    assert row["status"] == "evaluated" and row["riskIndex"] == row["probability"] == 0
    assert row["riskLevel"] == "模型0档" and row["source"] == {"model": "synthetic"}


def test_lipid_rows_separate_mechanism_from_data_without_a_guideline_bonus():
    lipid = {"dataType": "INSPECTION", "itemName": "低密度脂蛋白(LDL-c)", "checkTime": PAST,
             "testResultValue": "3.70", "resultStatusName": "偏高"}
    rows = apply_dynamic_assessment(_case(lipid))["diseaseRisks"]
    assert {row["code"] for row in rows} == {"hyperlipidemia", "atherosclerosis"}
    assert all(row["riskIndex"] == 3 for row in rows)
    assert all(all(label not in row["mechanism"] for label in ("机制关联：", "本次依据：", "随访重点：")) for row in rows)
    assert all(row["mechanism"].count("。") == 1 and row["qualitative"]["basis"] and row["qualitative"]["recommendation"] for row in rows)
