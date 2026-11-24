"""Synthetic boundary tests: diagnosis histories are not inferred risks."""
from copy import deepcopy
import json

import pytest

from adapters.gut_barrier.evidence import _candidate, _query_topics, _search_variants
from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.disease_registry import HISTORY_FOLLOWUP_RULES, diagnosis_followup_findings
from modules.gut_barrier.dynamic_diseases import _reference, apply_dynamic_assessment, attention_score, evidence_plan
from modules.gut_barrier.presentation import build_report
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.schemas import GutBarrierRequest


DAY = "2026-09-28"


def exam(text="确诊：抑郁症", **changes):
    return {"dataType": "EXAM", "itemName": "门诊病历", "checkTime": "2025-12-18",
            "examResult": text, **changes}


def case(*rows, age=28, context=None, survey=None):
    request = GutBarrierRequest.model_validate({"userId": "synthetic", "sessionId": "synthetic",
        "age": age, "sex": "男", "hospitalData": list(rows), "healthSurvey": survey})
    return assess_case(request, context={"currentDate": DAY, **(context or {})})


def diseases(assessment, retrieval=None):
    return apply_dynamic_assessment(assessment, retrieval)["diseaseRisks"]


@pytest.mark.parametrize("code", list(HISTORY_FOLLOWUP_RULES))
def test_documented_history_enables_only_its_specific_qualitative_followup(code):
    rule = HISTORY_FOLLOWUP_RULES[code]
    original = case(exam("出院诊断：" + rule["name"]))
    before = deepcopy(original)
    result = apply_dynamic_assessment(original)
    assert before == original
    assert [row["code"] for row in result["diseaseRisks"]] == [code]
    row = result["diseaseRisks"][0]
    assert row["riskIndex"] is row["attention"] is row["probability"] is None
    assert row["status"] == "qualitative_followup"
    assert row["qualitative"]["evidenceIds"][:len(rule["evidenceIds"])] == list(rule["evidenceIds"])
    assert len(row["qualitative"]["evidenceIds"]) > len(rule["evidenceIds"])
    assert any(item["applicationScope"] == "mechanism_association_not_risk_coefficient"
               for item in result["diseaseAssessment"]["appliedEvidence"])
    assert "2025-12-18" in row["qualitative"]["basis"]
    assert "当前严重程度" not in row["mechanism"]
    assert row["mechanism"].count("。") == 1
    assert "复核管理状态" in row["qualitative"]["basis"]
    assert evidence_plan(original)[0]["query"] == rule["query"]
    assert apply_dynamic_assessment(result) == result
    entry = next(item for item in result["diseaseAssessment"]["registry"]["diseases"] if item["code"] == code)
    assert entry["enabled"] and entry["scorePolicy"] == "qualitative_only_no_validated_index"
    assert entry["patientEligibility"] == "eligible_for_direction" and entry["score"] is None
    public = project_callback_report(build_report(result, {}, report_id="documented-followup"))
    section = next(item for item in public["sections"] if item["id"] == "disease_risks")
    table = next(item for item in section["blocks"] if item.get("columns") == ["疾病", "风险指数", "风险等级", "机制关联"])
    assert len(table["rows"][0]) == 4 and table["rows"][0][1] is None
    assert "candidateReviews" not in json.dumps(public)


@pytest.mark.parametrize("text", [
    "抑郁症", "疑似抑郁症", "考虑抑郁症", "确诊：抑郁症？", "未确诊：抑郁症",
    "否认抑郁症", "诊断：抑郁症，复核后排除", "诊断：抑郁症；诊断已撤销",
    "母亲确诊：抑郁症", "家族史：抑郁症", "患者母亲确诊：抑郁症",
    "如果确诊：抑郁症，应转诊", "临床诊断：抑郁症", "申请诊断：抑郁症",
    "确诊：抑郁症的风险因素", "确诊：抑郁症可能性大", "诊断：双相情感障碍",
    "确诊：抑郁症；误诊记录", "确诊：抑郁症，家属否认既往诊断",
    "确诊：抑郁症；无证据支持", "诊断：抑郁症，但该诊断不成立", "诊断：抑郁症，复核不支持",
])
def test_negation_uncertainty_family_referral_and_teaching_do_not_become_diagnosis(text):
    assert not diseases(case(exam(text)))


@pytest.mark.parametrize("changes", [
    {"checkTime": None}, {"checkTime": "2027-01-01"}, {"itemName": "头颅CT"},
    {"itemName": "血常规"}, {"dataType": "INSPECTION"},
    {"itemName": "病理报告"},
])
def test_history_requires_traceable_date_and_a_diagnosis_bearing_document(changes):
    assert not diseases(case(exam(**changes)))


@pytest.mark.parametrize("age", [None, 0, 17])
def test_adult_history_rules_do_not_infer_applicability_to_children(age):
    assert not diseases(case(exam(), age=age))


def test_missing_reference_date_nonhospital_and_conflicting_sources_are_rejected():
    original = case(exam(), context={"currentDate": None})
    assert diagnosis_followup_findings(original) == {}
    original = case(exam())
    original["context"]["clinicalFindings"][0]["source"] = [{"kind": "fileList", "index": 0}]
    assert not diseases(original)
    assert not diseases(case(exam(), exam("否认抑郁症")))
    assert not diseases(case(exam(), exam("诊断已排除抑郁症", checkTime=DAY)))
    assert not diseases(case(exam(), exam("考虑抑郁症", checkTime=DAY)))


def test_questionnaire_words_and_four_markers_never_create_new_diagnosis_histories():
    original = case(survey={"pastHistory": "确诊：抑郁症"})
    original["barrier"]["score"] = 15
    assert not diseases(original)


def test_documented_diagnosis_zero_model_is_preserved_and_unregistered_conditions_stay_empty():
    original = case(exam())
    row = next(item for item in original["diseaseRisks"] if item["code"] == "depression")
    row.update(status="evaluated", riskIndex=0, probability=0, riskLevel="synthetic_zero")
    result = diseases(original)[0]
    assert result["riskIndex"] == result["probability"] == 0
    assert result["riskLevel"] == "synthetic_zero" and result["status"] == "evaluated"
    assert not diseases(case(exam("确诊：胰腺癌")))


def test_invalid_target_date_cannot_promote_history_and_valid_sample_date_is_used():
    assert diagnosis_followup_findings(case(exam(), context={"currentDate": "unknown"})) == {}
    result = diagnosis_followup_findings(case(exam(), context={"currentDate": "unknown", "sampleDate": DAY}))
    assert result["depression"]["date"] == "2025-12-18"


def test_identity_match_without_applied_patient_scope_does_not_claim_adoption():
    from modules.gut_barrier.dynamic_diseases import _coverage

    original = case(exam())
    plan = next(row for row in evidence_plan(original) if row["topic"] == "depression")
    ref = _reference("Q20")
    retrieval = {"status": "success", "queries": [{"id": "q", "text": plan["query"], "status": "success"}],
        "candidates": [{"id": "fixture", "queryIds": ["q"], "doi": ref["doi"], "url": ref["url"]}]}
    # A bibliographic match cannot claim patient application when no disease
    # row has adopted its scope; the query itself still comes from a hospital record.
    coverage = next(row for row in _coverage(original, retrieval, []) if row["topic"] == "depression")
    review = coverage["candidateReviews"][0]
    assert review["matchedReferenceIds"] == ["Q20"]
    assert review["applicableReferenceIds"] == [] and review["adoptionState"] == "not_adopted"
    assert review["appliedTo"] == []


def test_guidelines_are_not_numerical_bonus_and_missing_scope_has_no_score():
    fact = {"domain": "lipids", "strength": 3, "longitudinal": 0,
            "sourceType": "objective_historical", "location": "synthetic historical lipids"}
    assert attention_score([fact], guideline_support=1) == attention_score([fact], guideline_support=2)
    scored = attention_score([fact], guideline_support=True)
    assert scored["score"] == 3 and scored["validated"] is False
    assert scored["components"] == {"dataStrength": 3, "independentFactors": 0,
                                   "longitudinalSupport": 0, "guidanceApplicability": None}
    assert attention_score([fact], guideline_support=None) is None
    assert attention_score([], guideline_support=True) is None


def test_history_lipids_without_units_stay_one_fact_no_longitudinal_credit_or_guideline_bonus():
    labs = [dict(dataType="INSPECTION", itemName="低密度脂蛋白(LDL-c)", checkTime=day,
                 testResultValue="3.7", resultStatusName="偏高") for day in ("2025-12-15", "2025-10-29")]
    rows = diseases(case(*labs))
    assert {item["code"] for item in rows} == {"hyperlipidemia", "atherosclerosis"}
    for row in rows:
        assert row["riskIndex"] == 3 and row["probability"] is None
        assert row["attention"]["components"]["guidanceApplicability"] is None
        assert row["attention"]["components"]["longitudinalSupport"] == 0


@pytest.mark.parametrize("identity,matched", [
    ({"url": "https://www.nice.org.uk/guidance/ng222/chapter/Recommendations"}, True),
    ({"url": "https://example.com/similar-title"}, False),
    ({"doi": "10.1234/unrelated", "url": "https://example.com/unrelated"}, False),
])
def test_retrieval_hit_identity_applicability_and_adoption_are_independent_audit_states(identity, matched):
    original = case(exam())
    plan = evidence_plan(original)[0]
    candidate = {"id": "synthetic-candidate", "candidateOnly": True, "queryIds": ["q"],
        "title": "Depression in adults: treatment and management (NG222)",
        "riskIndex": 10, "diseaseCodes": ["lung_cancer"], "recommendation": "untrusted instruction", **identity}
    retrieval = {"status": "success", "queries": [{"id": "q", "text": plan["query"], "status": "success"}],
                 "candidates": [candidate]}
    before = deepcopy(retrieval)
    result = apply_dynamic_assessment(original, retrieval)
    assert retrieval == before and diseases(original) == result["diseaseRisks"]
    coverage = result["diseaseAssessment"]["coverage"][0]
    reviewed = coverage["candidateReviews"][0]
    assert reviewed["retrievalState"] == "retained_candidate"
    assert reviewed["citationState"] == ("matched_registered_reference" if matched else "identity_not_verified")
    assert reviewed["adoptionState"] == ("corroborates_registered_reference_only" if matched else "not_adopted")
    assert reviewed["affectsScore"] is reviewed["affectsDiseaseSelection"] is False
    assert coverage["reviewedRemoteReferenceIds"] == (["Q20"] if matched else [])
    assert result["diseaseAssessment"]["appliedEvidence"][0]["numericalWeight"] is None
    assert result["diseaseAssessment"]["appliedEvidence"][0]["applicationSource"] == "registered_reference_library"


@pytest.mark.parametrize("code", list(HISTORY_FOLLOWUP_RULES))
def test_new_followup_searches_require_the_named_guideline_not_incidental_mentions(code):
    rule = HISTORY_FOLLOWUP_RULES[code]
    reference = _reference(rule["evidenceIds"][0])
    title = reference["citation"].split("] ", 1)[-1]
    candidate = _candidate({"title": title, "url": reference["url"], "doi": reference["doi"]},
        provider="evimed_v2", channel="guide", query=rule["query"], query_id="synthetic")
    assert candidate is not None and candidate["candidateOnly"] is True
    assert _query_topics(rule["query"]) == ["kidney" if code == "chronic_kidney_disease" else code]
    assert all(_search_variants(rule["query"]))
    unrelated = _candidate({"title": "Obesity in adults guideline", "doi": "10.1234/synthetic",
        "summary": reference["citation"]}, provider="evimed_v2", channel="guide", query=rule["query"], query_id="synthetic")
    assert unrelated is None


ADDED_HISTORY_CODES = (
    "cirrhosis", "hyperuricemia", "coronary_heart_disease", "stroke", "alzheimers",
    "anxiety", "systemic_lupus", "chronic_kidney_disease", "liver_cancer", "lung_cancer",
)


def test_remaining_template_codes_only_gain_explicit_history_followup_not_a_score():
    records = [exam("确诊：" + HISTORY_FOLLOWUP_RULES[code]["name"], checkTime=f"2025-12-{index + 1:02}")
               for index, code in enumerate(ADDED_HISTORY_CODES)]
    result = apply_dynamic_assessment(case(*records))
    rows = {row["code"]: row for row in result["diseaseRisks"]}
    assert set(rows) == set(ADDED_HISTORY_CODES)
    assert all(row["riskIndex"] is row["probability"] is row["attention"] is None for row in rows.values())
    assert all(row["status"] == "qualitative_followup" for row in rows.values())
    assert {row["systemCode"] for row in rows.values()} == {
        "digestive", "metabolic", "cardiovascular", "neurological", "psychiatric", "immune", "urinary", "oncology"}
    assert all(item["numericalWeight"] is None for item in result["diseaseAssessment"]["appliedEvidence"])


@pytest.mark.parametrize("code", ADDED_HISTORY_CODES)
@pytest.mark.parametrize("template", [
    "{name}", "疑似{name}", "未确诊：{name}", "确诊：{name}？", "诊断：{name}，复核后排除",
    "母亲确诊：{name}", "家族史：{name}", "如果确诊：{name}，请转诊",
    "申请诊断：{name}", "确诊：{name}的风险因素", "示例：\n确诊：{name}",
    "检查正常。\n术后注意事项：\n确诊：{name}",
])
def test_added_histories_never_promote_bare_names_negation_family_or_aftercare(code, template):
    assert not diseases(case(exam(template.format(name=HISTORY_FOLLOWUP_RULES[code]["name"]))))


@pytest.mark.parametrize("code", ADDED_HISTORY_CODES)
@pytest.mark.parametrize("changes", [
    {"checkTime": None}, {"checkTime": "2027-01-01"}, {"itemName": "CT检查"},
    {"dataType": "INSPECTION"},
])
def test_added_histories_require_dated_diagnosis_document(code, changes):
    assert not diseases(case(exam("确诊：" + HISTORY_FOLLOWUP_RULES[code]["name"], **changes)))


@pytest.mark.parametrize("code", ADDED_HISTORY_CODES)
def test_added_histories_keep_clinical_qualification_without_identity_filtering(code):
    name = HISTORY_FOLLOWUP_RULES[code]["name"]
    old = exam("确诊：" + name)
    assert not diseases(case(old, exam("诊断已排除" + name, checkTime=DAY)))
    assert not diseases(case(old, exam("考虑" + name, checkTime=DAY)))
    assert not diseases(case(old, age=17))
    accepted = case(old)
    for field, value in (("status", "identity_conflict"), ("identityStatus", "conflict")):
        original = deepcopy(accepted)
        original["context"]["clinicalFindings"][0][field] = value
        assert [row["code"] for row in diseases(original)] == [code]
    original = deepcopy(accepted)
    original["context"]["clinicalFindings"][0]["source"][0]["identityStatus"] = "conflict"
    assert [row["code"] for row in diseases(original)] == [code]
    assert not diseases(case(survey={"name": "另一受检者", "gender": "女", "pastDisease": "确诊：" + name}))


@pytest.mark.parametrize("name", ["肝癌", "胆管癌", "肝转移癌", "痴呆", "认知障碍", "焦虑"])
def test_generic_or_different_disease_names_do_not_inherit_specific_history_guidance(name):
    assert not diseases(case(exam("确诊：" + name)))


@pytest.mark.parametrize("name,code", [
    ("非小细胞肺癌", "lung_cancer"), ("肝细胞癌", "liver_cancer"), ("结肠癌", "colorectal_cancer"),
])
def test_explicit_cancer_pathology_is_history_without_stage_or_recurrence_inference(name, code):
    row, = diseases(case(exam("诊断：" + name, itemName="病理报告")))
    assert row["code"] == code and row["status"] == "qualitative_followup"
    assert row["riskIndex"] is row["probability"] is None


def test_sle_guidance_without_guideline_title_requires_reviewed_title_and_doi_pair():
    rule = HISTORY_FOLLOWUP_RULES["systemic_lupus"]
    ref = _reference("Q29")
    title = ref["citation"].split("] ", 1)[-1]
    for doi in (None, "10.1234/unreviewed"):
        assert _candidate({"title": title, "url": ref["url"], "doi": doi},
            provider="evimed_v2", channel="guide", query=rule["query"], query_id="synthetic") is None


@pytest.mark.parametrize("canonical,score_anchor", [(True, "1.1"), (False, "1.3")])
def test_default_interpretation_score_links_match_current_and_frozen_report(canonical, score_anchor):
    from modules.gut_barrier.display_versions import CANONICAL_DISPLAY_TEMPLATE, WORD_REVIEW_DISPLAY_TEMPLATE
    from modules.report.gut_barrier_context import build_gut_barrier_contexts
    from modules.report.gut_barrier_defaults import default_section
    from tests.unit.gut_barrier.final_input_fixture import current_health_input

    assessed = assess_case(GutBarrierRequest.model_validate(current_health_input()),
                           context={"currentDate": "2026-10-01"})
    internal = build_report(assessed, {}, report_id="score-link-test")
    internal["meta"]["displayTemplateVersion"] = CANONICAL_DISPLAY_TEMPLATE if canonical else WORD_REVIEW_DISPLAY_TEMPLATE
    report = project_callback_report(internal)
    before = deepcopy(report)
    manual = default_section(1, build_gut_barrier_contexts({"report": report}))

    score_point = next(item for item in manual["key_points"] if "肠道屏障功能评分" in item["text"])
    score_guide = next(item for item in manual["visual_guide"] if item["target"] == "bands")
    score_faq = next(item for item in manual["faq"] if item["question"] == "这个分数是怎么算的？")
    assert score_point["anchor"] == score_guide["anchor"] == score_anchor
    assert score_faq["source"] == "报告 " + score_anchor
    headings = {block["title"].split(" ", 1)[0] for section in report["sections"]
                if section["id"] == "overview" for block in section["blocks"] if block.get("title")}
    assert score_anchor in headings
    if canonical:
        assert all(item["anchor"] in {"1.1", "1.2"} for item in manual["key_points"] + manual["visual_guide"])
    assert report == before


@pytest.mark.parametrize('text', [
    '确诊：冠心病。未见肺部感染。',
    '确诊：冠心病，未见肺部感染。',
    '确诊：冠心病；家族史：无。',
    '家族史：无。确诊：冠心病。',
    '确诊：冠心病，复核后排除肺部感染。',
    '确诊：冠心病；母亲无糖尿病史。',
    '家族史：母亲糖尿病；患者确诊：冠心病。',
    '确诊：冠心病；确诊：肺癌；诊断已撤销。',
])
def test_unrelated_negation_or_family_history_preserves_patient_diagnosis(text):
    facts = diagnosis_followup_findings(case(exam(text)))
    assert 'coronary_heart_disease' in facts


@pytest.mark.parametrize('text', [
    '确诊：冠心病；复核后排除冠心病。',
    '确诊：冠心病；该诊断已撤销。',
    '确诊：冠心病，家属否认既往诊断。',
    '家族史：\n确诊：冠心病。',
    '母亲：\n确诊：冠心病。',
    '示例：\n确诊：冠心病。',
])
def test_related_negation_revision_and_relative_subject_still_block_diagnosis(text):
    assert 'coronary_heart_disease' not in diagnosis_followup_findings(case(exam(text)))


def test_same_condition_in_family_history_does_not_cancel_explicit_patient_diagnosis():
    original = case(exam('患者确诊：冠心病；家族史：母亲冠心病'))
    fact = diagnosis_followup_findings(original)['coronary_heart_disease']
    assert fact['excerpt'] == '患者确诊：冠心病'
    assert 'coronary_heart_disease' in diagnosis_followup_findings(case(
        exam('确诊：冠心病'), exam('家族史：母亲冠心病', checkTime=DAY)))


def test_multiline_relative_history_is_not_a_patient_diagnosis():
    assert diagnosis_followup_findings(case(exam('母亲病史：\n确诊：冠心病'))) == {}


@pytest.mark.parametrize('heading', ['家族史', '母亲', '母亲病史', '父亲病史', '示例'])
def test_multiline_relative_or_example_heading_without_colon_is_not_patient_history(heading):
    assert diagnosis_followup_findings(case(exam(heading + '\n确诊：冠心病'))) == {}


@pytest.mark.parametrize('text,expected', [
    ('示例：\n患者确诊：冠心病', set()),
    ('如果\n确诊：冠心病', set()),
    ('患者确诊：冠心病；示例：\n患者确诊：肺癌', {'coronary_heart_disease'}),
])
def test_example_or_hypothetical_patient_does_not_reset_illustrative_scope(text, expected):
    assert set(diagnosis_followup_findings(case(exam(text)))) == expected
