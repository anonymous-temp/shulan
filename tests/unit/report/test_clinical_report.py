"""Synthetic clinical-first tests: source facts, citations and fixed DTO."""
import asyncio

import pytest

from modules.report.callback import _fixed_callback_body
from modules.report.clinical_report import (
    ANALYSIS_FIELDS, _citation_review_failures, _validate_block, callback_sections, clinical_queries, collect_source_facts,
    ensure_document_fallback, organize_problems,
)
from modules.report.health_assessment import build_health_assessment
from modules.report.evidence import _screen, retain_referenced_evidence
from modules.report.pipeline import ReportPipeline, _GenerationProgress, _deadline_partial_payload
from modules.report.schemas import (
    AbnormalMicrobesSection, ClinicalProblemSource, ClinicalReportBlock,
    ClinicalReportProblem, DiseaseRisksSection, EvidenceRecord, EvidenceReference,
    FixedReportCallback, HealthAssessment, HealthManagementSection,
    ParsedMicrobiomeReport, ReportRequest, SubjectInfo,
)


def _citation_review_passes(review, source_text, *, node=None, facts=None):
    return not _citation_review_failures(review, source_text, node=node, facts=facts)


def approved_scope():
    return {"population": "Unrestricted general information", "requirements": [],
        "missing": [], "adviceKind": "general_care"}


def approved_review(item):
    """Synthetic reviewer; dedicated tests below cover negative decisions."""
    evidence = item.get("evidence")
    return {"id": item["id"], "valid": True,
        "supportQuotes": [evidence["text"]] if evidence else [], "unsupportedClaims": [],
        "applicability": "general", "clinicalRelevance": "direct" if item["node"] in {"causes", "mechanism", "outlook"} else "general",
        "sourceAbsenceCheck": {"inventoryChecked": True, "verdict": "absent", "matches": [], "reason": "synthetic"},
        "claimRole": {"causes": "etiology", "mechanism": "mechanism", "outlook": "outcome"}.get(item["node"], "management") if evidence else "management",
        "scope": approved_scope()}


def source():
    request = ReportRequest.model_validate({
        "userId": "synthetic", "sessionId": "clinical-test",
        "hospitalData": [
            {"dataType": "EXAM", "itemName": "影像检查", "examResult": "某器官结节，性质待查。", "checkTime": "2024-01-01"},
            {"dataType": "INSPECTION", "itemName": "尿隐血", "testResultValue": "3+", "resultStatusName": "正常", "checkTime": "2021-01-01"},
            {"dataType": "INSPECTION", "itemName": "字典外检测项目", "testResultValue": "26", "referenceRange": "0–10", "unit": "U/L", "resultStatusName": "偏高", "checkTime": "2022-01-01"},
            {"dataType": "INSPECTION", "itemName": "字典外检测项目", "testResultValue": "8", "referenceRange": "0–10", "unit": "U/L", "resultStatusName": "正常", "checkTime": "2024-01-01"},
        ]})
    parsed = ParsedMicrobiomeReport(sourceType="microbiome_data", sourceName="synthetic",
        subject=SubjectInfo(name="样例"), taxa=[
            {"taxonId": f"taxon-{i}", "category": "other", "latinName": f"Synthetic taxon {i}",
             "value": "0%" if i == 0 else "0.002", "unit": "%", "sourceStatus": "偏低", "explicitlyAbnormal": True}
            for i in range(5)])
    return request, parsed


class Model:
    def __init__(self):
        self.calls = []

    async def generate(self, payload):
        self.calls.append(payload)
        if payload["task"] == "extract_reported_clinical_facts":
            return {"items": [{"id": i["id"], "assertions": []} for i in payload["items"]]}
        if payload["task"] == "validate_clinical_plan":
            return {"sections": [], "analyses": [], "items": [{"id": i['id'], "valid": True,
                "comparedPhases": [o['id'] for o in payload['items'] if o['id'] != i['id']],
                "reason": "Synthetic schedule validation"} for i in payload['items']]}
        if payload["task"] == "validate_clinical_citations":
            return {"items": [approved_review(item) for item in payload["items"]]}
        if payload["task"] == "supplement_common_examinations":
            return {"items": [{"diseaseId": d["diseaseId"], "examination": None} for d in payload["diseases"]]}
        if payload["task"] == "organize_clinical_problems":
            f = payload["facts"][0]
            return {"problems": [{"title": "某器官结节", "priority": "priority",
                "sources": [{"factId": f["factId"], "quote": f["value"]}], "searchTerms": ["organ nodule"]}]}
        if payload["task"] == "prepare_clinical_search_terms":
            return {"items": []}
        if payload.get("planSelectionOnly"):
            return {"sections": [{"code": s["code"], "actionIds": [payload["actionCatalog"][0]["id"]]}
                for s in payload["requestedSections"]]}
        def block():
            return {"text": "需核对相应的既往随访资料。", "state": "insufficient", "factIds": [], "evidenceRefs": []}
        return {"sections": [{"code": s["code"], **block()} for s in payload["requestedSections"]],
            "analyses": [{"problemId": pid, **{f: block() for f in ANALYSIS_FIELDS}} for pid in payload["requestedAnalyses"]]}


def test_source_inventory_never_depends_on_dictionary_or_evidence():
    request, parsed = source()
    document = collect_source_facts(request, parsed)
    assert len(document.facts) == 9
    urine = next(f for f in document.facts if f.name == "尿隐血")
    assert (urine.value, urine.status, urine.date) == ("3+", "正常", "2021-01-01")
    history = [f for f in document.facts if f.name == "字典外检测项目"]
    assert len(history) == 2
    assert history[0].unit == "U/L"
    assert len({f.fact_id for f in history}) == 2
    assert len([f for f in document.facts if f.kind == "microbe"]) == 5
    assert document.facts == collect_source_facts(request, parsed).facts


@pytest.mark.parametrize("specimen,expected", [("尿液", "尿红细胞总数"), ("blood", "红细胞总数"), (None, "红细胞总数")])
def test_indicator_specimen_requires_explicit_source_context(specimen, expected):
    request, parsed = source()
    request = ReportRequest.model_validate({"userId": "u", "sessionId": "s", "hospitalData": [{
        "dataType": "INSPECTION", "itemName": "红细胞总数", "testResultValue": "32",
        "specimen": specimen, "referenceRange": "0-10", "unit": "/uL"}]})
    fact, = collect_source_facts(request, parsed.model_copy(update={"taxa": []})).facts
    assert fact.name == expected
    assert fact.value == "32"


@pytest.mark.asyncio
async def test_multi_problem_image_uses_exact_independent_fragments_and_keeps_original():
    from modules.report.clinical_report import attach_clinical_problems
    request, parsed = source()
    original = "甲器官结节，性质待查；乙器官细小结石。其余未见异常。"
    request = ReportRequest.model_validate({"userId": "u", "sessionId": "s", "hospitalData": [{
        "dataType": "EXAM", "itemName": "影像", "examResult": original, "reportTime": "2021-01-01"}]})
    doc = collect_source_facts(request, parsed.model_copy(update={"taxa": []}))
    class Organizer:
        async def generate(self, payload):
            return {"problems": [{"title": title, "sources": [{"factId": payload["facts"][0]["factId"], "quote": original}]}
                for title in ("甲器官结节", "乙器官细小结石")]}
    await organize_problems(Organizer(), doc)
    assert doc.facts[0].value == original
    stone = next(p for p in doc.problems if p.title == "乙器官细小结石")
    assert stone.sources[0].quote == "乙器官细小结石。"
    assert stone.sources[0].quote in original
    nodule = next(p for p in doc.problems if "甲器官" in p.title)
    assert nodule.certainty == "suspected" and "待查" in nodule.sources[0].quote
    diseases = DiseaseRisksSection()
    attach_clinical_problems(doc, diseases)
    assert next(d for d in diseases.items if d.name == stone.title).existing_results[0].result == stone.sources[0].quote
    ensure_document_fallback(doc)
    result = callback_sections(doc, diseases, [])
    row = next(f for f in result["diagnostic_basis"]["abnormal_findings"] if f["problem"] == stone.title)
    assert "甲器官" not in row["indicators"][0]["value"]
    assert "2021-01-01" in row["indicators"][0]["value"]


@pytest.mark.parametrize("value,unit,expected", [("0 %", "%", "0 %"), ("3.4 mmol / L", "mmol/L", "3.4 mmol / L"), ("0", None, "0")])
def test_fact_display_units_do_not_duplicate_or_invent_units(value, unit, expected):
    from modules.report.clinical_report import fact_text
    from modules.report.schemas import ReportSourceFact
    fact = ReportSourceFact(factId="f", kind="clinical", name="项目", value=value, unit=unit, source="synthetic")
    assert fact_text(fact) == "项目：" + expected


@pytest.mark.parametrize("kind", ["diet", "intake_assessment", None])
def test_nutrient_table_rejects_dietary_management_masquerading_as_supplements(kind):
    errors = []
    assert _validate_block({"nutrientItems": [{"kind": kind, "name": "来源条目"}]},
        code="nutrition", facts={}, evidence={}, allowed_refs=set(), rejections=errors) is None
    assert "仅收有适用依据的补充剂" in errors[0]["reason"]


def test_indirect_background_cannot_validate_problem_cause_or_outcome():
    review = approved_review({"id": "1", "node": "causes", "evidence": {"text": "A general population observation with no disease-specific finding."}})
    review["clinicalRelevance"] = "general"
    assert not _citation_review_passes(review, "A general population observation with no disease-specific finding.", node="causes")


@pytest.mark.asyncio
async def test_retrieval_reassessment_preserves_roles_and_targets_only_remaining_gaps():
    from modules.report.clinical_report import assess_clinical_evidence, clinical_gap_queries
    doc = collect_source_facts(*source())
    doc.problems = [ClinicalReportProblem(problemId="p", title="来源结节", searchTerms=["source nodule"])]
    quote = "An exact original adult clinical finding with its limitations."
    record = EvidenceRecord(evidenceKey="r", source="synthetic", category="literature", title="Human review",
        summary=quote, supports=["p"], intents=["disease_clinical_significance"])
    doc.evidence_analysis_roles = {"r": {"p": ["etiology"]}}
    class Assessor:
        async def generate(self, payload):
            return {"items": [{"id": "r", "applicable": True, "supports": ["p"], "quote": quote, "claim": "限定结论",
                "analysisRoles": [{"support": "p", "role": "mechanism", "quote": quote}]}]}
    accepted = await assess_clinical_evidence(Assessor(), [record], doc)
    assert doc.evidence_analysis_roles["r"]["p"] == ["etiology", "mechanism"]
    queries = clinical_gap_queries(doc, 30, accepted)
    analyses = [q for q in queries if q.intent == "disease_clinical_significance"]
    assert len(analyses) == 1 and "natural history" in analyses[0].query
    assert analyses[0].supports == ("p",) and queries[0] == analyses[0]


def test_source_facts_are_frozen_and_zero_is_not_missing():
    from pydantic import ValidationError
    from modules.report.clinical_report import fact_text
    document = collect_source_facts(*source())
    fact = next(f for f in document.facts if f.fact_id == "taxon-0")
    assert "0%" in fact_text(fact)
    assert "%%" not in fact_text(fact)
    with pytest.raises(ValidationError):
        fact.value = "20"


def test_assessment_adapter_separates_model_rows_without_changing_engine_or_sources():
    from modules.report.clinical_report import clinical_assessment_request
    request, parsed = scored_source()
    from modules.report.schemas import HospitalDataInput, ParsedDisease
    model_row = HospitalDataInput(dataType="EXAM", itemName="溃疡性结肠炎", examResult="88.78",
        sourceReportType="microbiome", sourceFile="mixed.pdf")
    lab_row = HospitalDataInput(dataType="INSPECTION", itemName="LDL-C(mmol/L)", testResultValue="3.9",
        sourceReportType="lab", sourceFile="mixed.pdf", resultStatusName="偏高")
    request.hospital_data.extend([model_row, lab_row])
    parsed.disease_results = [ParsedDisease(diseaseId="source-risk", name="溃疡性结肠炎", sourceLevel="低风险")]
    before_request, before_parsed = request.model_dump(), parsed.model_dump()
    adapted = clinical_assessment_request(request, parsed)
    assert model_row not in adapted.hospital_data and lab_row in adapted.hospital_data
    assert request.model_dump() == before_request and parsed.model_dump() == before_parsed
    expected_request = request.model_copy(update={"hospital_data": [r for r in request.hospital_data if r is not model_row]})
    expected = build_health_assessment(expected_request, parsed, {}).model_dump()
    actual = build_health_assessment(adapted, parsed, {}).model_dump()
    assert actual == expected
    risks = [r for d in actual["dimensions"] for r in d["related_diseases"] if r["name"] == "溃疡性结肠炎"]
    assert risks and all(r["source"] == "菌群报告" and r["risk_level"] == "low" for r in risks)
    doc = collect_source_facts(request, parsed)
    assert any(f.kind == "function" and f.name == "溃疡性结肠炎" and f.value == "88.78" for f in doc.facts)


def test_auxiliary_table_projection_keeps_values_separate_from_clinical_measurements():
    doc = collect_source_facts(*source())
    ensure_document_fallback(doc)
    before = doc.model_dump()
    body = callback_sections(doc, DiseaseRisksSection(), [])
    context = body["diagnosis_summary"]["clinical_context"]
    assert context["report.auxiliary.version"] == "1"
    assert context["report.auxiliary.taxon-0.value"] == "0%"
    assert context["report.auxiliary.taxon-0.kind"] == "microbe"
    assert doc.model_dump() == before


@pytest.mark.parametrize("text", ["不能根据菌群变化推算个人患病概率。", "单次检出不能据此确诊为感染。"])
def test_negated_diagnosis_boundaries_reach_semantic_review(text):
    block = _validate_block({"text": text}, code="limits", facts={}, evidence={}, allowed_refs=set())
    assert block is not None and block.text == text
    # Passing structure is not publication approval; a negative semantic
    # review still blocks even a boundary-looking sentence.
    review = approved_review({"id": "0", "node": "limits", "evidence": None})
    review.update(valid=False, unsupportedClaims=["An unsupported parallel assertion"])
    assert not _citation_review_passes(review, None, node="limits")


@pytest.mark.parametrize("role,expected", [("background", False), ("source_fact", False), ("management", True)])
def test_stage_plans_require_management_not_a_verified_research_definition(role, expected):
    review = approved_review({"id": "0", "node": "phase_1", "evidence": None})
    review['claimRole'] = role
    assert _citation_review_passes(review, None, node="phase_1") is expected




@pytest.mark.parametrize("value,reference,unit,abnormal", [
    ("12", "0–10", "U/L", True),
    ("3", "5~9", None, True),
    ("1.01", "0.80~0.90", None, True),
    ("-2.1", "-2.0至-1.0", None, True),
    ("1.2e-3", "0-1e-3", "g/L", True),
    ("12 U/L", "0-10 U/L", "U/L", True),
    ("10", "<10", None, True),
    ("10", "≤10", None, False),
    ("10", ">10", None, True),
    ("10", "≥10", None, False),
    ("10", "0-10", None, False),
    ("0", "0-10", None, False),
    ("2", "1-3", None, False),
    ("<12", "0-10", None, False),
    ("12", "男0-10，女0-8", None, False),
    ("12", "0-10 mg/L", "mmol/L", False),
    ("12", "0-10 mg/L", None, False),
    ("43.1", "360~44.0rj", None, False),
    ("12", "20-10", None, False),
    ("12", "详见报告", None, False),
    ("12", None, None, False),
    ("1,200", "0-10", None, False),
])
def test_clinical_abnormality_can_use_only_unambiguous_printed_reference(value, reference, unit, abnormal):
    request = ReportRequest.model_validate({"userId": "u", "sessionId": "s", "hospitalData": [{
        "itemName": "未收录数值项目", "dataType": "INSPECTION", "testResultValue": value,
        "referenceRange": reference, "unit": unit,
    }]})
    doc = collect_source_facts(request, ParsedMicrobiomeReport(sourceType="pending", sourceName="synthetic", subject=SubjectInfo()))
    fact, = doc.facts
    assert fact.abnormal is abnormal
    assert (fact.value, fact.reference_range, fact.unit, fact.status) == (value, reference, unit, None)


@pytest.mark.asyncio
async def test_reference_only_abnormality_survives_empty_model_without_inventing_diagnosis():
    request = ReportRequest.model_validate({"userId": "u", "sessionId": "s", "hospitalData": [{
        "itemName": "未收录数值项目", "dataType": "INSPECTION", "testResultValue": "12",
        "referenceRange": "0-10", "unit": "U/L", "reportTime": "2024-02-01",
    }]})
    doc = collect_source_facts(request, ParsedMicrobiomeReport(sourceType="pending", sourceName="synthetic", subject=SubjectInfo()))
    class Empty:
        async def generate(self, payload):
            return {"problems": []}
    await organize_problems(Empty(), doc)
    assert [p.title for p in doc.problems] == ["未收录数值项目检查异常待复核"]
    assert doc.problems[0].sources[0].quote == "12"
    assert doc.facts[0].date == "2024-02-01"


@pytest.mark.asyncio
@pytest.mark.parametrize("value,reference,status,source_type", [
    ("8", "0-10", None, "lab"),
    ("12", "0-10", "正常", "lab"),
    ("12", None, None, "lab"),
    ("12", "0-10", None, "microbiome"),
])
async def test_model_cannot_label_normal_conflicting_or_unbounded_numeric_fact_abnormal(value, reference, status, source_type):
    request = ReportRequest.model_validate({"userId": "u", "sessionId": "s", "hospitalData": [{
        "itemName": "未收录数值项目", "dataType": "INSPECTION", "testResultValue": value,
        "referenceRange": reference, "resultStatusName": status, "sourceReportType": source_type,
    }]})
    doc = collect_source_facts(request, ParsedMicrobiomeReport(sourceType="pending", sourceName="synthetic", subject=SubjectInfo()))
    class FalseAbnormal:
        async def generate(self, payload):
            f = payload["facts"][0]
            return {"problems": [{"title": f["name"] + "检查异常待复核",
                "sources": [{"factId": f["factId"], "quote": f["value"]}]}]}
    await organize_problems(FalseAbnormal(), doc)
    assert not doc.problems
    assert not doc.facts[0].abnormal


@pytest.mark.asyncio
async def test_source_fallback_problems_receive_search_terms_without_changing_facts():
    from modules.report.clinical_report import _fallback_problems, prepare_clinical_search_terms
    doc = collect_source_facts(*source())
    doc.problems = _fallback_problems(doc)
    before = [f.model_dump() for f in doc.facts]
    titles = [(p.problem_id, p.title, p.certainty) for p in doc.problems]
    calls = []
    class Terms:
        async def generate(self, payload):
            calls.append(payload)
            lookup = {f['factId']: f for f in payload['facts']}
            return {'items': [{'problemId': p['problemId'], 'sourceFactId': p['sources'][0]['factId'],
                'sourceQuote': lookup[p['sources'][0]['factId']]['value'],
                'sourceTerm': lookup[p['sources'][0]['factId']]['name'],
                'englishTerm': 'unlisted clinical measurement'} for p in payload['problems']]}
    model = Terms()
    await prepare_clinical_search_terms(model, doc)
    await prepare_clinical_search_terms(model, doc)
    assert len(calls) == 1  # One bounded batch; already prepared problems are not retranslated.
    assert before == [f.model_dump() for f in doc.facts]
    assert titles == [(p.problem_id, p.title, p.certainty) for p in doc.problems]
    queries = [q for q in clinical_queries(doc, 30, public=True) if q.disease_id]
    assert queries and all('"unlisted clinical measurement"' in q.query for q in queries)
    assert not doc.incomplete


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['wrong_source', 'fake_quote', 'nonliteral_term', 'query_injection', 'unavailable'])
async def test_search_term_preparation_failure_never_removes_source_problem(failure):
    from modules.report.clinical_report import _fallback_problems, prepare_clinical_search_terms
    doc = collect_source_facts(*source())
    doc.problems = _fallback_problems(doc)
    original = [p.model_dump() for p in doc.problems]
    class Terms:
        async def generate(self, payload):
            if failure == 'unavailable':
                raise TimeoutError()
            p, f = payload['problems'][0], payload['facts'][0]
            item = {'problemId': p['problemId'], 'sourceFactId': f['factId'], 'sourceQuote': f['value'],
                'sourceTerm': f['name'], 'englishTerm': 'unlisted clinical measurement'}
            key, value = {'wrong_source': ('sourceFactId', 'not-this-source'),
                'fake_quote': ('sourceQuote', 'invented result'),
                'nonliteral_term': ('sourceTerm', '新增诊断'),
                'query_injection': ('englishTerm', 'measurement OR cancer')}[failure]
            item[key] = value
            return {'items': [item]}
    await prepare_clinical_search_terms(Terms(), doc)
    assert original == [p.model_dump() for p in doc.problems]
    assert 'CLINICAL_SEARCH_TERMS_INCOMPLETE' in doc.incomplete
    queries = [q for q in clinical_queries(doc, 30) if q.disease_id]
    assert queries[0].query == '字典外检测项目 etiology pathophysiology'


@pytest.mark.parametrize('code', ['microbiome_analysis', 'microbiome_rationale', 'barrier'])
def test_absent_microbiome_fallback_does_not_invent_a_detected_change(code):
    request, parsed = source()
    parsed.taxa = []
    doc = collect_source_facts(request, parsed)
    block = _validate_block({'text': '资料不足。', 'state': 'insufficient', 'factIds': [], 'evidenceRefs': []},
        code=code, facts={f.fact_id: f for f in doc.facts}, evidence={}, allowed_refs=set(), needs_evidence=True)
    assert block is not None
    assert '菌群变化' not in block.text and '单次菌群结果' not in block.text
    assert '资料' in block.text


@pytest.mark.asyncio
async def test_overprecise_english_query_gets_source_concept_without_losing_original_context():
    from modules.report.clinical_report import prepare_clinical_search_terms
    doc = collect_source_facts(*source())
    model = Model()
    await organize_problems(model, doc)
    problem = doc.problems[0]
    problem.search_terms = ['decreased right unlisted organ measurement', 'alias']
    original = [f.model_dump() for f in doc.facts]
    class Terms:
        async def generate(self, payload):
            p, f = payload['problems'][0], payload['facts'][0]
            return {'items': [{'problemId': p['problemId'], 'sourceFactId': f['factId'],
                'sourceQuote': p['sources'][0]['quote'], 'sourceTerm': f['name'], 'conceptRole': 'finding',
                'englishTerm': 'decreased right unlisted organ measurement', 'searchConcept': 'unlisted measurement'}]}
    await prepare_clinical_search_terms(Terms(), doc, include_existing=True)
    queries = [q for q in clinical_queries(doc, 30, public=True) if q.disease_id == problem.problem_id]
    assert all('"unlisted measurement" OR "decreased right unlisted organ measurement"' in q.query for q in queries)
    assert original == [f.model_dump() for f in doc.facts]
    paper = EvidenceRecord(evidenceKey='synthetic-concept', source='pubmed', title='Unlisted measurement in adult patients', summary='Human clinical review.', category='guideline')
    assert _screen(paper, query=queries[0])[0] is True


@pytest.mark.parametrize('concept,translation,valid', [
    ('urine specific gravity', 'low urine specific gravity', True),
    ('colonic nodule', 'enhancing colonic hepatic flexure nodule', True),
    ('colonic nodule', 'enhancing nodule in the colonic hepatic flexure lumen', True),
    ('visual acuity', 'decreased visual acuity right eye', True),
    ('unlisted measurement', 'right unlisted organ measurement', True),
    ('new diagnosis', 'unlisted organ measurement', False),
    ('unlisted unlisted', 'unlisted organ measurement', False),
    ('blood specific gravity', 'low urine specific gravity', False),
    ('nodule', 'colonic nodule', False),
    ('low urine specific gravity', 'low urine specific gravity', True),
    ('measurement OR cancer', 'unlisted measurement', False),
])
def test_search_concept_is_a_bounded_literal_subset_not_a_disease_mapping(concept, translation, valid):
    from modules.report.clinical_report import _valid_search_concept
    assert _valid_search_concept(concept, translation) is valid


@pytest.mark.parametrize('verdict,claim,valid', [
    ('contradicted', '未提供相关检查结果。', False),
    ('absent', '未提供相关检查结果。', False),
    ('historical_only', '未提供相关检查结果。', False),
    ('historical_only', '未提供近期复查结果。', True),
])
def test_absence_claim_distinguishes_existing_history_from_missing_update(verdict, claim, valid):
    from modules.report.clinical_report import _source_absence_failures
    check = {'sourceAbsenceCheck': {'inventoryChecked': True, 'verdict': verdict,
        'matches': [{'factId': 'old', 'factQuote': '原始历史结果'}]}}
    failures = _source_absence_failures(check, claim, {'old': {'name': '检查', 'value': '原始历史结果'}})
    assert (not failures) == valid


def test_absence_check_cannot_pass_without_full_inventory_or_with_invented_pointer():
    from modules.report.clinical_report import _source_absence_failures
    assert _source_absence_failures({}, '未提供近期记录。', {})
    assert _source_absence_failures({'sourceAbsenceCheck': {'inventoryChecked': True,
        'verdict': 'historical_only', 'matches': [{'factId': 'invented', 'factQuote': '内容'}]}}, '缺少近期复查记录。', {})


def test_related_result_is_not_the_missing_different_measurement():
    from modules.report.clinical_report import _source_absence_failures, _citation_review_failures
    claim = '已有检查甲，缺少不同的检查乙结果。'
    facts = {'known': {'name': '检查甲', 'value': '原始结果'}}
    review = approved_review({'id': '1', 'node': 'gaps'})
    review.update(claimRole='uncertainty', sourceAbsenceCheck={'inventoryChecked': True,
        'verdict': 'partial_information', 'matches': [{'factId': 'known', 'factQuote': '原始结果'}],
        'reason': '检查甲与所缺的检查乙是不同观测。'})
    review['scope'].update(adviceKind='explanation', missing=['检查乙结果'])
    assert not _source_absence_failures(review, claim, facts)
    assert not _citation_review_failures(review, None, node='gaps', facts=facts, claim=claim)
    # The exception must never satisfy an intervention's unmet prerequisites.
    review['claimRole'] = 'management'
    assert _citation_review_failures(review, None, node='gaps', facts=facts, claim=claim)


@pytest.mark.asyncio
async def test_search_concept_repairs_invalid_item_once_without_changing_valid_problem():
    from modules.report.clinical_report import prepare_clinical_search_terms
    doc = collect_source_facts(*source())
    await organize_problems(Model(), doc)
    doc.problems[0].search_terms = []
    for problem in doc.problems[1:]:
        problem.search_terms = ['existing concept']
    already_valid = [p.model_dump() for p in doc.problems[1:]]
    calls = []
    class Terms:
        async def generate(self, payload):
            calls.append(payload)
            p, f = payload['problems'][0], payload['facts'][0]
            return {'items': [{'problemId': p['problemId'], 'sourceFactId': f['factId'],
                'sourceQuote': f['value'], 'sourceTerm': f['name'],
                'englishTerm': 'right unlisted organ measurement',
                'searchConcept': 'unlisted disease' if len(calls) == 1 else 'unlisted measurement'}]}
    await prepare_clinical_search_terms(Terms(), doc)
    assert len(calls) == 2
    assert 'unlisted measurement' in doc.problems[0].search_terms
    assert 'CLINICAL_SEARCH_TERMS_INCOMPLETE' not in doc.incomplete
    assert already_valid == [p.model_dump() for p in doc.problems[1:]]


def test_evidence_quote_handles_are_local_exact_and_cannot_hide_unknown_ids():
    from modules.report.clinical_report import _review_quote_options, _resolve_review_quotes
    source_text = 'Adults participated. The observed finding had a defined scope.'
    options = _review_quote_options(source_text)
    review = approved_review({'id':'0','node':'nutrition','evidence':{'text':source_text}})
    review['supportQuoteIds'] = ['Q2']
    review['scope']['requirements'] = [{'condition':'adult', 'evidenceQuoteId':'Q1'}]
    resolved = _resolve_review_quotes(review, options)
    assert resolved['supportQuotes'] == ['The observed finding had a defined scope.']
    assert resolved['scope']['requirements'][0]['evidenceQuote'] == 'Adults participated.'
    assert 'evidenceQuoteId' in review['scope']['requirements'][0]
    assert _resolve_review_quotes({'supportQuoteIds':['Q99']}, options)['supportQuotes'] == ['']
    assert _resolve_review_quotes({'scope':{'requirements':[{'evidenceQuoteId':'Q99'}]}}, options)['scope']['requirements'][0]['evidenceQuote'] == ''


@pytest.mark.parametrize('size,accepted', [(4, True), (5, False)])
def test_microbiome_prose_limit_requests_rewrite_instead_of_truncating(size, accepted):
    raw = {'sentences': [{'text': f'独立辅助说明第{i}项。', 'evidenceRefs': []} for i in range(size)]}
    result = _validate_block(raw, code='microbiome_analysis', facts={}, evidence={}, allowed_refs=set())
    assert (result is not None) is accepted
    assert len(raw['sentences']) == size
    if accepted:
        assert len(result.text.splitlines()) == size


def test_multi_taxon_source_summary_binds_facts_for_semantic_direction_review():
    document = collect_source_facts(*source())
    facts = {f.fact_id: f for f in document.facts}
    before = document.model_dump()
    raw = {"sentences": [{"text": "Synthetic taxon 1和Synthetic taxon 2均偏低。", "evidenceRefs": []}]}
    reasons = []
    result = _validate_block(raw, code="microbiome_analysis", facts=facts, evidence={},
        allowed_refs=set(), rejections=reasons)
    assert result.text == raw['sentences'][0]['text']
    assert set(result.fact_ids) == {'taxon-1', 'taxon-2'}
    review = approved_review({'id': '0', 'node': 'microbiome_analysis', 'evidence': None})
    review.update(valid=False, unsupportedClaims=['来源未支持此方向'])
    assert not _citation_review_passes(review, None, node='microbiome_analysis')
    assert document.model_dump() == before


def test_multi_taxon_source_restatement_does_not_delete_independent_cited_explanation():
    document = collect_source_facts(*source())
    facts = {f.fact_id: f for f in document.facts}
    before = document.model_dump()
    statement = "此项说明具有独立的引用，不依赖前文的菌群方向复述。"
    raw = {"sentences": [{"text": "Synthetic taxon 1和Synthetic taxon 2均偏低。", "evidenceRefs": []},
        {"text": statement, "evidenceRefs": [1]}]}
    result = _validate_block(raw, code="microbiome_analysis", facts=facts,
        evidence={1: {"interpretation": {"effect": "This is the verified source quotation."}}},
        allowed_refs={1}, needs_evidence=True)
    assert result is not None and result.text == raw['sentences'][0]['text'] + '\n' + statement
    assert result.evidence_refs == [1] and result.evidence_claims == {"1": statement}
    assert len(raw["sentences"]) == 2 and document.model_dump() == before


def test_grouped_source_summary_is_not_removed_before_its_dependent_sentence():
    facts = {f.fact_id: f for f in collect_source_facts(*source()).facts}
    result = _validate_block({"sentences": [{"text": "Synthetic taxon 1和Synthetic taxon 2均偏低。"},
        {"text": "这些变化提示需要进一步解释。"}]}, code="microbiome_analysis",
        facts=facts, evidence={}, allowed_refs=set())
    assert result.text == 'Synthetic taxon 1和Synthetic taxon 2均偏低。\n这些变化提示需要进一步解释。'


@pytest.mark.parametrize("text,expected", [
    ("Synthetic taxon 1偏低。", {"taxon-1"}),
    ("Synthetic taxon 10偏低。", {"genus"}),
    ("OtherSynthetic taxon 10偏低。", set()),
    ("Synthetic taxon 1偏低，Synthetic属另有来源。", {"taxon-1", "genus"}),
])
def test_microbe_mentions_do_not_count_genus_inside_species(text, expected):
    from modules.report.clinical_report import _mentioned_microbe_ids
    document = collect_source_facts(*source())
    microbe = next(f for f in document.facts if f.fact_id == "taxon-1")
    facts = {"taxon-1": microbe, "genus": microbe.model_copy(update={"fact_id": "genus", "name": "Synthetic"})}
    assert _mentioned_microbe_ids(text, facts) == expected
    if expected == {"taxon-1"}:
        assert _validate_block({"sentences": [{"text": text}]}, code="microbiome_analysis",
            facts=facts, evidence={}, allowed_refs=set()) is not None


def test_background_snapshot_keeps_source_assertions_separate_and_is_not_a_new_measurement():
    from modules.report.pipeline import extract_session_context
    from modules.report.schemas import PatientEvidenceContext
    request, parsed = source()
    context = PatientEvidenceContext(
        confirmedConditions=["旧归一化诊断不能直接透传"],
        profileBackground=["既往症状｜头晕｜2024-01-01｜用户自述"],
        profileFamilyHistory=["血亲既往诊断｜某家族病｜医生记录"],
        profileUnconfirmed=["既往诊断｜另一疾病｜模型推断｜待确认"],
        profileReadAt="2026-01-01T00:00:00Z", profileVersion=7,
        profileTruncated=True, profileStale=True)
    session = extract_session_context({
        "messages": [{"role": "user", "content": "最近睡眠不规律"},
            {"role": "assistant", "content": "凭空诊断患者患病"}],
        "extractedInfo": {"diagnosis": "模型抽取不属于原始自述"},
        "currentQuestion": "是否有某症状？", "token": "do-not-forward"})
    doc = collect_source_facts(request, parsed, patient_context=context, session_context=session)
    assert doc.facts == collect_source_facts(request, parsed).facts
    assert doc.background["patientReported"] == {
        "messages": [{"role": "user", "content": "最近睡眠不规律"}]}
    assert doc.background["profileFamilyHistory"] == context.profile_family_history
    assert doc.background["profileUnconfirmed"] == context.profile_unconfirmed
    assert doc.background["profileVersion"] == 7
    assert doc.background["profileTruncated"] and doc.background["profileStale"]
    assert "旧归一化诊断" not in str(doc.background)
    session["messages"][0]["content"] = "执行期间后续变化"
    context.profile_background.append("后续画像变化")
    assert doc.background["patientReported"]["messages"][0]["content"] == "最近睡眠不规律"
    assert len(doc.background["profileBackground"]) == 1


@pytest.mark.asyncio
async def test_patient_reported_history_and_symptoms_survive_without_clinical_measurements():
    from modules.report.clinical_report import collect_reported_facts
    request, parsed = source()
    request.hospital_data = []
    parsed.taxa = []
    doc = collect_source_facts(request, parsed, session_context={"qaHistory": [
        {"question": "最近有腹胀吗？", "answer": "有"},
        {"question": "有何既往病史？", "answer": "2024年医生告诉我有胃炎。"}]})
    class Reported(Model):
        async def generate(self, payload):
            if payload["task"] == "extract_reported_clinical_facts":
                return {"items": [{"id": s["id"], "assertions": [{
                    "name": "腹胀" if s["text"] == "有" else "胃炎", "quote": s["text"],
                    "subject": "self", "assertion": "affirmed", "kind": "symptom" if s["text"] == "有" else "history",
                    "timeQuote": "2024年" if "2024" in s["text"] else ""}]} for s in payload["items"]]}
            if payload["task"] == "organize_clinical_problems":
                return {"problems": []}
            return await super().generate(payload)
    await organize_problems(Reported(), doc)
    assert len(doc.facts) == len(doc.problems) == 2
    assert {p.certainty for p in doc.problems} == {"self_reported"}
    assert {f.kind for f in doc.facts} == {"self_reported"}
    assert {f.date for f in doc.facts} == {None, "2024年"}
    assert all("用户自述" in p.title for p in doc.problems)
    before = [f.model_dump() for f in doc.facts]
    await collect_reported_facts(Reported(), doc)
    assert before == [f.model_dump() for f in doc.facts]
    ensure_document_fallback(doc)
    body = callback_sections(doc, DiseaseRisksSection(), [])
    context = body["diagnosis_summary"]["clinical_context"]
    assert len([k for k in context if k.startswith("report.fact.self_reported.")]) == 2
    assert not any(k.startswith("report.fact.clinical.") for k in context)


@pytest.mark.asyncio
@pytest.mark.parametrize("text, quote", [
    ("我没有腹痛。", "腹痛"), ("我父亲有腹痛。", "腹痛"),
    ("我是否有腹痛？", "腹痛"), ("我腹痛？", "我腹痛"),
    ("我担心腹痛。", "腹痛"), ("如果腹痛应该怎么办？", "腹痛"),
])
async def test_reported_fact_cropped_negation_family_and_questions_cannot_be_promoted(text, quote):
    from modules.report.clinical_report import collect_reported_facts
    doc = collect_source_facts(*source(), session_context={"symptoms": text})
    before = list(doc.facts)
    class Cropped:
        async def generate(self, payload):
            return {"items": [{"id": i["id"], "assertions": [{"name": "腹痛", "quote": quote,
                "subject": "self", "assertion": "affirmed", "kind": "symptom"}]} for i in payload["items"]]}
    await collect_reported_facts(Cropped(), doc)
    assert doc.facts == before
    assert "REPORTED_FACT_ASSERTION_MISMATCH" in doc.incomplete


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "最近一周饭后腹胀，没有腹痛。父亲有高血压，我没有被医生确诊为高血压。",
    "父亲有高血压。我最近一周饭后腹胀，没有腹痛。",
    "没有腹痛，但最近一周饭后腹胀。",
])
async def test_mixed_reported_statement_is_reviewed_per_claim_without_losing_positive_symptom(text):
    from modules.report.clinical_report import collect_reported_facts
    doc = collect_source_facts(*source(), session_context={"symptoms": text})
    class Mixed:
        def __init__(self):
            self.reviews = []

        async def generate(self, payload):
            if payload["task"] == "extract_reported_clinical_facts":
                return {"items": [{"id": i["id"], "assertions": [
                    {"name": name, "quote": i["text"], "subject": "self", "assertion": "affirmed",
                     "kind": "symptom", "timeQuote": "最近一周" if name == "饭后腹胀" else ""}
                    for name in ("饭后腹胀", "腹痛")]} for i in payload["items"]]}
            assert payload["task"] == "validate_reported_clinical_facts"
            self.reviews.append(payload)
            return {"items": [{"id": i["id"], "valid": i["name"] == "饭后腹胀",
                "subject": "self", "assertion": "affirmed" if i["name"] == "饭后腹胀" else "denied",
                "contextReviewed": True, "supportQuote": "最近一周饭后腹胀"}
                for i in payload["items"]]}
    model = Mixed()
    await collect_reported_facts(model, doc)
    actual = [f for f in doc.facts if f.kind == "self_reported"]
    assert len(actual) == 1 and actual[0].name == "饭后腹胀"
    assert actual[0].value == text and actual[0].date == "最近一周"
    assert actual[0].status == "用户自述，未核验"
    assert len(model.reviews) == 1
    assert all(i["source"]["text"] == text for i in model.reviews[0]["items"])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "missing", "duplicate", "unreviewed", "fake_quote", "family", "denied"])
async def test_mixed_reported_statement_review_fails_closed(failure):
    from modules.report.clinical_report import collect_reported_facts
    doc = collect_source_facts(*source(), session_context={"symptoms": "最近一周饭后腹胀，没有腹痛。"})
    before = list(doc.facts)
    class InvalidReview:
        async def generate(self, payload):
            if payload["task"] == "extract_reported_clinical_facts":
                return {"items": [{"id": i["id"], "assertions": [{"name": "饭后腹胀", "quote": i["text"],
                    "subject": "self", "assertion": "affirmed", "kind": "symptom"}]} for i in payload["items"]]}
            if failure == "timeout":
                raise TimeoutError()
            item = {"id": payload["items"][0]["id"], "valid": True, "subject": "self",
                "assertion": "affirmed", "contextReviewed": True, "supportQuote": "最近一周饭后腹胀"}
            if failure == "missing":
                return {"items": []}
            if failure == "duplicate":
                return {"items": [item, item]}
            field, value = {"unreviewed": ("contextReviewed", False), "fake_quote": ("supportQuote", "经检查已确诊"),
                "family": ("subject", "family"), "denied": ("assertion", "denied")}[failure]
            item[field] = value
            return {"items": [item]}
    await collect_reported_facts(InvalidReview(), doc)
    assert doc.facts == before and doc.incomplete


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_result", "fake_quote", "fake_date", "wrong_subject", "uncertain", "failure"])
async def test_reported_fact_extraction_never_turns_missing_or_invalid_results_into_confirmed_facts(failure):
    from modules.report.clinical_report import collect_reported_facts
    doc = collect_source_facts(*source(), session_context={"symptoms": "最近一周腹痛。"})
    before = list(doc.facts)
    class Invalid:
        async def generate(self, payload):
            if failure == "failure":
                raise TimeoutError()
            if failure == "missing_result":
                return {"items": []}
            a = {"name": "腹痛", "quote": "最近一周腹痛。", "subject": "self", "assertion": "affirmed", "kind": "symptom"}
            key, value = {"fake_quote": ("quote", "已确诊腹痛。"), "fake_date": ("timeQuote", "2026-09-22"),
                "wrong_subject": ("subject", "family"), "uncertain": ("assertion", "uncertain")}[failure]
            a[key] = value
            return {"items": [{"id": payload["items"][0]["id"], "assertions": [a]}]}
    await collect_reported_facts(Invalid(), doc)
    assert doc.facts == before
    assert bool(doc.incomplete) == (failure not in {"wrong_subject", "uncertain"})


def test_reported_source_items_exclude_model_text_and_unanswered_questions():
    from modules.report.clinical_report import _reported_source_items
    doc = collect_source_facts(*source(), session_context={"messages": [
        {"role": "user", "content": "最近头痛"}, {"role": "assistant", "content": "已确诊"}],
        "qaHistory": [{"question": "患有另一疾病吗？"}, {"question": "腹痛吗？", "answer": "否"}],
        "healthData": {"extractedInfo": {"diagnosis": "推断疾病"}}})
    entries = _reported_source_items(doc)
    assert {i["text"] for i in entries} == {"最近头痛", "否"}
    assert next(i for i in entries if i["text"] == "否")["question"] == "腹痛吗？"


@pytest.mark.asyncio
@pytest.mark.parametrize("question, answer", [("最近有腹痛吗？", "否"), ("父亲有腹痛吗？", "有"), ("从未腹痛吗？", "是")])
async def test_short_answer_cannot_invert_denial_or_family_subject(question, answer):
    from modules.report.clinical_report import collect_reported_facts
    doc = collect_source_facts(*source(), session_context={"qaHistory": [{"question": question, "answer": answer}]})
    class Wrong:
        async def generate(self, payload):
            return {"items": [{"id": i["id"], "assertions": [{"name": "腹痛", "quote": answer,
                "subject": "self", "assertion": "affirmed", "kind": "symptom"}]} for i in payload["items"]]}
    await collect_reported_facts(Wrong(), doc)
    assert not any(f.kind == "self_reported" for f in doc.facts)
    assert "REPORTED_FACT_ASSERTION_MISMATCH" in doc.incomplete


def test_microbiome_observations_do_not_enter_clinical_problem_pool():
    from modules.report.schemas import HospitalDataInput
    request, parsed = source()
    request.hospital_data.append(HospitalDataInput(dataType="INSPECTION", itemName="Synthetic taxon 0", testResultValue="0", resultStatusName="偏低"))
    doc = collect_source_facts(request, parsed)
    repeated = next(f for f in doc.facts if f.name == "Synthetic taxon 0" and f.fact_id != "taxon-0")
    assert repeated.kind == "context"
    assert len([f for f in doc.facts if f.kind == "clinical"]) == 4


def test_unselected_source_taxonomy_is_not_reinterpreted_as_clinical_disease():
    from modules.report.schemas import HospitalDataInput
    request, parsed = source()
    parsed.source_microbiome_terms = ["来源中的新属名"]
    request.hospital_data.append(HospitalDataInput(dataType="INSPECTION", itemName="来源中的新属名", testResultValue="0.02", resultStatusName="偏低"))
    doc = collect_source_facts(request, parsed)
    assert next(f for f in doc.facts if f.name == "来源中的新属名").kind == "context"
    assert "sourceMicrobiomeTerms" not in parsed.model_dump(by_alias=True)


def test_microbial_function_scores_are_not_clinical_measurements():
    from modules.report.schemas import HospitalDataInput
    request, parsed = source()
    request.hospital_data.extend([
        HospitalDataInput(dataType="INSPECTION", itemName="维生素 B2", testResultValue="85",
            sourceFile=parsed.source_name, sourceReportType="microbiome"),
        HospitalDataInput(dataType="INSPECTION", itemName="新型代谢功能指数", testResultValue="70",
            resultStatusName="偏低", sourceFile=parsed.source_name),
        HospitalDataInput(dataType="INSPECTION", itemName="维生素 B2", testResultValue="12",
            unit="nmol/L", sourceFile=parsed.source_name, sourceReportType="lab"),
        HospitalDataInput(dataType="INSPECTION", itemName="新型代谢功能指数", testResultValue="70",
            resultStatusName="偏低", sourceFile="clinical.pdf", sourceReportType="lab"),
    ])
    doc = collect_source_facts(request, parsed)
    assert next(f for f in doc.facts if f.name == "维生素 B2" and f.value == "85").kind == "function"
    assert next(f for f in doc.facts if f.name == "新型代谢功能指数").kind == "function"
    assert next(f for f in doc.facts if f.name == "维生素 B2" and f.value == "12").kind == "clinical"
    assert {f.kind for f in doc.facts if f.name == "新型代谢功能指数"} == {"clinical", "function"}


def test_source_genus_rows_are_not_clinical_diseases_but_other_specimens_are_kept():
    from modules.report.schemas import HospitalDataInput, ParsedTaxon
    request, parsed = source()
    parsed.taxa.append(ParsedTaxon(taxonId="new-source", latinName="Novigenus example", category="other", value="0.2"))
    for source_file in (parsed.source_name, "blood-culture.pdf"):
        request.hospital_data.append(HospitalDataInput(dataType="INSPECTION", itemName="Novigenus",
            testResultValue="0.2", resultStatusName="偏高", sourceFile=source_file,
            reportTime="2024-01-01" if source_file == parsed.source_name else "2024-01-02"))
    doc = collect_source_facts(request, parsed)
    assert next(f for f in doc.facts if f.name == "Novigenus" and f.source == parsed.source_name).kind == "context"
    assert next(f for f in doc.facts if f.name == "Novigenus" and f.source == "blood-culture.pdf").kind == "clinical"


def test_explicit_clinical_provenance_overrides_matching_microbial_terminology():
    from modules.report.schemas import HospitalDataInput, SourceMetric
    request, parsed = source()
    parsed.metrics.append(SourceMetric(code="vitamin_score", name="某营养素", value="85"))
    request.hospital_data.extend([
        HospitalDataInput(dataType="INSPECTION", itemName="某营养素", testResultValue="12",
            unit="nmol/L", sourceFile=parsed.source_name, sourceReportType="lab"),
        HospitalDataInput(dataType="INSPECTION", itemName="Synthetic taxon 0", testResultValue="阳性",
            sourceFile="blood-culture.pdf", sourceReportType="lab"),
    ])
    document = collect_source_facts(request, parsed)
    assert next(f for f in document.facts if f.name == "某营养素" and f.value == "12").kind == "clinical"
    assert next(f for f in document.facts if f.name == "Synthetic taxon 0" and f.value == "阳性").kind == "clinical"
    assert next(f for f in document.facts if f.name == "某营养素" and f.value == "85").kind == "function"


def test_insufficient_state_cannot_publish_unreferenced_mechanism():
    block = _validate_block({"text": "该表现源于某个基因突变。", "state": "insufficient"},
        code="mechanism", facts={}, evidence={}, allowed_refs=set(), needs_evidence=True)
    assert block is not None
    assert "基因突变" not in block.text
    assert "尚无" in block.text


def test_protocol_not_used_as_completed_clinical_evidence():
    record = EvidenceRecord(evidenceKey="a", source="synthetic", category="literature",
        title="Human gut clinical systematic review protocol", summary="Adults human gut study")
    query = clinical_queries(collect_source_facts(*source()), 30)[0]
    from dataclasses import replace
    assert not _screen(record, query=replace(query, required_concepts=()))[0]


@pytest.mark.parametrize("title, excluded", [
    ("Recommended protocols for clinical measurement in adults", False),
    ("Clinical practice protocol for adult assessment", False),
    ("Protocol for a randomized controlled trial", True),
    ("A systematic review protocol", True),
    ("Clinical measurement: study protocol", True),
    ("Retraction: clinical measurement study", True),
])
def test_protocol_title_filter_distinguishes_study_plan_from_practice(title, excluded):
    from modules.report.evidence import is_uncompleted_clinical_evidence
    from dataclasses import replace
    assert is_uncompleted_clinical_evidence(title) is excluded
    record = EvidenceRecord(evidenceKey="practice", source="synthetic", category="guideline",
        title=title, summary="An adult clinical practice guideline with human outcomes.")
    query = replace(clinical_queries(collect_source_facts(*source()), 30)[0], required_concepts=())
    assert _screen(record, query=query)[0] is not excluded


def test_general_clinical_query_without_required_terms_does_not_divide_by_zero():
    from dataclasses import replace
    record = EvidenceRecord(evidenceKey="a", source="synthetic", category="guideline",
        title="Physical activity guidelines for adults", summary="Human adult physical activity recommendations.")
    query = replace(clinical_queries(collect_source_facts(*source()), 30)[0], required_concepts=())
    assert _screen(record, query=query)[0]
    assert record.relevance == 0.0


@pytest.mark.asyncio
async def test_clinical_applicability_requires_exact_quote_and_supported_ids():
    from modules.report.clinical_report import assess_clinical_evidence
    doc = collect_source_facts(*source())
    record = EvidenceRecord(evidenceKey="a", source="synthetic", category="literature",
        title="Human clinical review", summary="A precise supported human statement.", supports=["clinical-section:diet"])
    class Assessor:
        async def generate(self, payload):
            return {"items": [{"id": "a", "applicable": True, "supports": ["clinical-section:diet"],
                "claim": "范围明确的临床建议", "quote": "A precise supported human statement."}]}
    accepted = await assess_clinical_evidence(Assessor(), [record], doc)
    assert len(accepted) == 1
    assert record.interpretation is None  # does not mutate cached source
    record.summary = "Different source text"
    assert await assess_clinical_evidence(Assessor(), [record], doc) == []


@pytest.mark.asyncio
async def test_general_management_evidence_has_its_own_scope_not_a_disease_requirement():
    from modules.report.clinical_report import assess_clinical_evidence
    doc = collect_source_facts(*source())
    doc.problems = [ClinicalReportProblem(problemId="problem", title="来源临床问题")]
    record = EvidenceRecord(evidenceKey="general", source="synthetic", category="literature",
        title="Human dietary review", summary="A general adult dietary finding with limited applicability.",
        supports=["clinical-section:prebiotics"])
    class Assessor:
        async def generate(self, payload):
            assert payload["subject"] == {"gender": "男", "age": 31}
            context = payload["records"][0]["supportContexts"]["clinical-section:prebiotics"]
            assert context["kind"] == "general_management"
            assert context["title"] == "膳食底物与益生元"
            return {"items": [{"id": "general", "applicable": True,
                "supports": ["clinical-section:prebiotics"], "claim": "有限的一般膳食说明，并非个体疾病治疗。",
                "quote": record.summary}]}
    accepted = await assess_clinical_evidence(Assessor(), [record], doc, subject={"gender": "男", "age": 31})
    assert accepted[0].supports == ["clinical-section:prebiotics"]
    assert not accepted[0].disease_ids


@pytest.mark.asyncio
async def test_priority_cap_applies_after_source_bound_candidate_validation():
    request = ReportRequest.model_validate({"userId": "u", "sessionId": "s", "hospitalData": [
        {"dataType": "EXAM", "itemName": f"影像-{i}", "examResult": f"实际发现-{i}"} for i in range(7)]})
    parsed = source()[1]
    document = collect_source_facts(request, parsed)
    class Ranked:
        async def generate(self, payload):
            return {"problems": [{"title": f["value"], "priority": "priority" if n == 6 else "routine",
                "sources": [{"factId": f["factId"], "quote": f["value"]}]} for n, f in enumerate(payload["facts"])]}
    await organize_problems(Ranked(), document)
    assert len(document.problems) == 5
    assert document.problems[0].title == "实际发现-6"
    assert len([f for f in document.facts if f.kind == "clinical"]) == 7


def test_preclinical_source_is_still_rejected_for_clinical_query():
    doc = collect_source_facts(*source())
    doc.problems = [ClinicalReportProblem(problemId="p", title="某器官结节", searchTerms=["organ nodule"])]
    record = EvidenceRecord(evidenceKey="a", source="synthetic", category="literature", title="organ nodule in mice", studyTypes=["animal"])
    assert not _screen(record, query=clinical_queries(doc, 40)[0])[0]


@pytest.mark.parametrize("role, allowed", [("source_fact", False), ("background", False),
    ("uncertainty", True), ("management", True)])
def test_gap_requires_unresolved_information_or_its_verification_not_another_overview(role, allowed):
    review = approved_review({"id":"0", "node":"gaps", "evidence":None})
    review["claimRole"] = role
    facts = {"known": {"kind": "clinical", "value": "已记录异常"}}
    if role == "source_fact":
        review["scope"]["requirements"] = [{"condition": "原记录存在异常", "factId": "known",
            "factQuote": "已记录异常", "evidenceQuote": "", "useAs": "clinical_record"}]
    assert _citation_review_passes(review, None, node="gaps", facts=facts) == allowed
    # The restriction is node-specific: an accurate known fact remains valid
    # in the overview and cannot disappear from the source inventory.
    assert _citation_review_passes(review, None, node="overview", facts=facts)


@pytest.mark.parametrize("role, allowed", [("mechanism", True), ("intervention_mechanism", False)])
def test_problem_mechanism_does_not_accept_intervention_mechanism_even_with_real_quote(role, allowed):
    quote = "The intervention inhibited a signaling pathway in an experimental model."
    review = approved_review({"id":"0", "node":"mechanism", "evidence":{"text":quote}})
    review["claimRole"] = role
    assert _citation_review_passes(review, quote, node="mechanism") == allowed


def test_public_callback_drops_unbound_references_without_changing_source_facts():
    from modules.report.schemas import ReportSourceFact
    doc = collect_source_facts(*source())
    doc.facts.append(ReportSourceFact(factId="x", kind="clinical", name="<script>", value="&value", source="synthetic", abnormal=True))
    ensure_document_fallback(doc)
    sections = callback_sections(doc, DiseaseRisksSection(), [{"number": 88, "title": "Unbound"}])
    assert sections["evidence"] == []
    body = FixedReportCallback.model_validate({"userId": "u", "sessionId": "s", "status": "partial_success", **sections}).model_dump(mode="json", by_alias=True)
    assert body["evidence"] == []
    assert any(f.name == "<script>" and f.value == "&value" for f in doc.facts)


@pytest.mark.asyncio
async def test_model_titles_must_have_source_and_uncertainty_is_preserved():
    document = collect_source_facts(*source())
    model = Model()
    await organize_problems(model, document)
    assert len(document.problems) == 2
    assert any(p.title == "字典外检测项目检查异常待复核" for p in document.problems)
    assert document.problems[0].certainty == "suspected"
    assert "待核实" in document.problems[0].title
    assert all(q.screen_profile == "clinical_report" for q in clinical_queries(document, 30))
    assert all("stool" not in q.query for q in clinical_queries(document, 30))


@pytest.mark.asyncio
async def test_fabricated_problem_rejected_without_losing_facts():
    class Bad:
        async def generate(self, payload):
            f = payload["facts"][0]
            return {"problems": [{"title": "恶性肿瘤确诊", "sources": [{"factId": f["factId"], "quote": f["value"]}]}]}
    doc = collect_source_facts(*source())
    await organize_problems(Bad(), doc)
    assert not any("肿瘤" in p.title for p in doc.problems)
    assert len(doc.facts) == 9
    assert "CLINICAL_PROBLEM_TITLE_MISMATCH" in doc.incomplete


@pytest.mark.asyncio
async def test_literal_imaging_title_retains_original_quote_despite_model_rewording():
    doc = collect_source_facts(*source())
    class Reworded(Model):
        async def generate(self, payload):
            f = payload["facts"][0]
            return {"problems": [{"title": "某器官结节", "sources": [
                {"factId": f["factId"], "quote": "模型重写的引用，不得输出"}]}]}
    await organize_problems(Reworded(), doc)
    problem = next(p for p in doc.problems if p.title.startswith("某器官结节"))
    assert problem.sources[0].quote == "某器官结节，性质待查。"
    assert problem.certainty == "suspected"


@pytest.mark.asyncio
async def test_negated_imaging_finding_cannot_become_positive_diagnosis():
    request = ReportRequest.model_validate({"userId": "u", "sessionId": "s", "hospitalData": [
        {"dataType": "EXAM", "itemName": "影像", "examResult": "未见明确肿瘤。"}]})
    doc = collect_source_facts(request, source()[1])
    class Negated:
        async def generate(self, payload):
            f = payload["facts"][0]
            return {"problems": [{"title": "肿瘤", "sources": [{"factId": f["factId"], "quote": f["value"]}]}]}
    await organize_problems(Negated(), doc)
    assert not doc.problems
    assert "CLINICAL_PROBLEM_NEGATED_SOURCE" in doc.incomplete


def test_normal_unrecognised_clinical_fact_counts_for_artifact_persistence():
    from modules.report.pipeline import _has_evaluate_source_facts
    assert _has_evaluate_source_facts({"diagnosis_summary": {"clinical_context": {
        "report.version": "clinical-v1", "report.fact.clinical.any": "新检测项目：正常"}}})
    assert not _has_evaluate_source_facts({"diagnosis_summary": {"clinical_context": {
        "report.version": "clinical-v1", "report.section.overview": "未形成判断"}}})


def scored_source():
    request, parsed = source()
    request = ReportRequest.model_validate({**request.model_dump(by_alias=True), "age": 38, "sex": "男", "hospitalData": [
        *[row.model_dump(by_alias=True) for row in request.hospital_data],
        {"dataType": "INSPECTION", "itemName": "LDL-C(mmol/L)", "testResultValue": "3.7",
            "unit": "mmol/L", "referenceRange": "0-3.37", "resultStatusName": "偏高"},
        {"dataType": "INSPECTION", "itemName": "空腹血糖(mmol/L)", "testResultValue": "5.4",
            "referenceRange": "3.9-6.1", "resultStatusName": "正常"},
    ]})
    return request, parsed


def test_observation_projection_separates_abnormalities_from_normal_history_and_microbes():
    doc = collect_source_facts(*source())
    ensure_document_fallback(doc)
    before = doc.model_dump()
    body = callback_sections(doc, DiseaseRisksSection(), [])
    context = body["diagnosis_summary"]["clinical_context"]
    assert len([k for k in context if k.startswith("report.fact.clinical.")]) == 4
    rows = [k.removesuffix("kind") for k in context if k.startswith("report.observation.") and k.endswith(".kind")]
    assert len(rows) == 2  # One abnormal result and one imaging finding, not normal history.
    lab = next(base for base in rows if context[base + "kind"] == "indicator")
    assert [context[lab + c] for c in ("name", "value", "unit", "reference", "status", "date")] == [
        "字典外检测项目", "26", "U/L", "0–10", "偏高", "2022-01-01"]
    assert doc.model_dump() == before
    assert all(isinstance(v, str) for v in context.values())


def test_reference_detected_abnormality_keeps_missing_original_status():
    from modules.report.schemas import ReportSourceFact
    doc = collect_source_facts(*source())
    doc.facts = [ReportSourceFact(factId="custom", kind="clinical", name="新项目", value="12",
        referenceRange="0-10", abnormal=True, source="synthetic.pdf")]
    ensure_document_fallback(doc)
    context = callback_sections(doc, DiseaseRisksSection(), [])["diagnosis_summary"]["clinical_context"]
    assert context["report.observation.custom.status"] == ""
    assert context["report.observation.custom.value"] == "12"


def test_block_rejects_wrong_evidence_and_invented_measurements():
    kwargs = dict(code="mechanism", facts={}, evidence={3: {"summary": "A precise human clinical statement."}}, allowed_refs={3}, needs_evidence=True)
    assert _validate_block({"text": "已确诊为其他疾病", "evidenceRefs": [3], "evidenceQuotes": {"3": "A precise human clinical statement."}}, **kwargs) is None
    assert _validate_block({"text": "通用机制", "evidenceRefs": [3], "evidenceQuotes": {"3": "不存在的证据"}}, **kwargs) is None
    assert _validate_block({"text": "可用于理解一般机制，并非个体病因证明。", "evidenceRefs": [3], "evidenceQuotes": {"3": "A precise human clinical statement."}, "evidenceClaims": {"3": "可用于理解一般机制"}}, **kwargs) is not None
    guarded = _validate_block({"text": "没有文献也直接作出机制判断。"}, **kwargs)
    assert guarded.state == "insufficient"
    assert "直接作出机制判断" not in guarded.text


@pytest.mark.parametrize('code,dose,cited,expected', [
    ('nutrition', '200 mg', True, True), ('nutrition', '400 mg', True, False),
    ('nutrition', '200 mg', False, False), ('diet', '200 mg', True, False),
])
def test_nutrition_numeric_rows_require_original_dose_and_citation(code, dose, cited, expected):
    # Synthetic support checks binding only; production still runs semantic
    # population, indication and per-field dose-type review after this gate.
    line = f'营养素：测试营养素｜每日剂量：{dose}｜剂型/理由：测试条件｜服用方式：测试方式｜注意事项：测试提醒'
    quote = 'Synthetic trial used 200 mg of a nutrient in a defined population.'
    block = _validate_block({'sentences': [{'text': line, 'evidenceRefs': [1] if cited else []}]},
        code=code, facts={}, evidence={1: {'summary': quote, 'interpretation': {'effect': quote}}}, allowed_refs={1})
    assert (block is not None) is expected


def test_nutrition_search_is_not_a_fixed_disease_or_nutrient_map():
    doc = collect_source_facts(*source())
    doc.problems = [ClinicalReportProblem(problemId='new', title='字典外临床问题', searchTerms=['novel clinical finding'])]
    queries = clinical_queries(doc, 30)
    assert any(q.query == 'novel clinical finding nutrition diet prevention' for q in queries)
    assert any(q.supports == ('clinical-section:nutrition',) for q in queries)
    assert all('维生素D' not in q.query and '鱼油' not in q.query for q in queries)


def test_nutrient_dose_can_be_in_original_source_outside_the_short_explanatory_quote():
    quote = 'Nutrition requirements were considered in adult participants.'
    source_text = quote + ' The food intake target in the synthetic study was 200 mg daily.'
    text = '营养素：测试营养素｜每日剂量：膳食总摄入200 mg，不是补剂量｜剂型/理由：食物来源｜服用方式：不限定时点｜注意事项：不另设补剂'
    raw = {'sentences': [{'text': text, 'evidenceRefs': [1]}]}
    block = _validate_block(raw, code='nutrition', facts={},
        evidence={1: {'sourceText': source_text, 'summary': source_text, 'interpretation': {'effect': quote}}}, allowed_refs={1})
    assert block is not None
    # Passing a numeric provenance check is not clinical approval: unsupported
    # quantity/type/population still fails the independent semantic audit.
    review = approved_review({'id': 'x', 'node': 'nutrition', 'evidence': {'text': source_text}})
    review['unsupportedClaims'] = ['剂量口径不匹配']
    assert not _citation_review_passes(review, source_text, node='nutrition')


@pytest.mark.parametrize('text', [
    '钙：每日剂量：不定量｜剂型/理由：食物｜服用方式：随餐｜注意事项：按耐受',
    '营养素：某营养素｜每日剂量：不定量｜剂型/理由：食物｜服用方式：随餐｜注意事项：按耐受',
])
def test_uncited_or_malformed_nutrient_table_rows_request_repair(text):
    errors = []
    assert _validate_block({'sentences': [{'text': text, 'evidenceRefs': []}]},
        code='nutrition', facts={}, evidence={}, allowed_refs=set(), rejections=errors) is None
    assert '五个固定标签' in errors[0]['reason']


def test_structured_nutrient_fields_project_to_the_existing_string_contract():
    quote = 'A synthetic source describes a general dietary nutrient option.'
    raw = {'sentences': [{'text': '以食物来源的营养支持为主。', 'evidenceRefs': []}],
        'nutrientItems': [{'kind':'supplement', 'name':'测试营养素', 'dailyAmount':'不另设补充剂量', 'rationale':'食物来源',
            'intake':'不限定时点', 'cautions':'不额外指定', 'evidenceRefs':[1]}]}
    result = _validate_block(raw, code='nutrition', facts={},
        evidence={1:{'summary':quote, 'interpretation':{'effect':quote}}}, allowed_refs={1})
    row = '营养素：测试营养素｜每日剂量：不另设补充剂量｜剂型/理由：食物来源｜服用方式：不限定时点｜注意事项：不额外指定'
    assert result.text == '以食物来源的营养支持为主。\n' + row
    assert result.evidence_claims == {'1':row}
    assert 'nutrientItems' not in result.model_dump()
    raw['nutrientItems'][0]['name'] += '｜伪造字段'
    assert _validate_block(raw, code='nutrition', facts={}, evidence={}, allowed_refs=set()) is None


def test_a_single_reference_cannot_cover_another_taxon_by_joint_citation():
    doc = collect_source_facts(*source())
    facts = {f.fact_id:f for f in doc.facts}
    quote = 'Synthetic taxon 1 has a reported metabolic role in adults.'
    raw = {'sentences': [{'text': 'Synthetic taxon 1和Synthetic taxon 2具有相同作用。', 'evidenceRefs': [1]}]}
    errors = []
    assert _validate_block(raw, code='microbiome_analysis', facts=facts,
        evidence={1: {'summary': quote, 'supports': ['taxon-1'], 'interpretation': {'effect': quote}}},
        allowed_refs={1}, rejections=errors) is None
    assert '全部菌群' in errors[0]['reason']


@pytest.mark.parametrize('code,text', [
    ('strategy', '本报告坚持临床优先，菌群只作辅助。'),
    ('significance', '明确结节性质，以判断后续方案。'),
])
def test_patient_summary_does_not_reuse_policy_or_an_abstract_goal(code, text):
    assert _validate_block({'text':text}, code=code, facts={}, evidence={}, allowed_refs=set()) is None


def test_significance_is_an_optional_string_context_extension_not_a_dto_change():
    from modules.report.schemas import ClinicalProblemAnalysis
    doc = collect_source_facts(*source())
    fid = doc.facts[0].fact_id
    doc.problems = [ClinicalReportProblem(problemId='p', title='来源问题', sources=[ClinicalProblemSource(factId=fid, quote=doc.facts[0].value)])]
    doc.analyses = [ClinicalProblemAnalysis(problemId='p', significance=ClinicalReportBlock(code='significance', text='这项发现的通俗含义。'))]
    ensure_document_fallback(doc)
    result = callback_sections(doc, DiseaseRisksSection(), [])
    assert result['diagnosis_summary']['clinical_context']['report.problem.p.significance'] == '这项发现的通俗含义。'


def test_overview_requests_rewrite_instead_of_truncating_a_checklist():
    errors = []
    raw = {'text': '一项需复查。二项需复查。三项需复查。四项需复查。', 'factIds': []}
    assert _validate_block(raw, code='overview', facts={}, evidence={}, allowed_refs=set(), rejections=errors) is None
    assert '不直接截句' in errors[0]['reason']
    assert raw['text'].endswith('四项需复查。')


def test_block_binds_verified_source_quote_instead_of_regenerating_it():
    statement = "Verified adult human source statement with precise scope."
    result = _validate_block({"text": "这项研究只能支持有限的一般说明。", "evidenceRefs": [3],
        "evidenceQuotes": {"3": "模型重新抄写导致不一致"}, "evidenceClaims": {"3": "这项研究只能支持有限的一般说明。"}},
        code="diet", facts={}, evidence={3: {"interpretation": {"effect": statement}}}, allowed_refs={3})
    assert result is not None
    assert result.evidence_quotes == {"3": statement}
    assert _validate_block({"text": "这一段引用了不存在的原文。", "evidenceRefs": [4]},
        code="diet", facts={}, evidence={3: {"interpretation": {"effect": statement}}}, allowed_refs={3}) is None


def test_sentence_citations_use_identical_prose_even_when_not_adjacent():
    raw = {"state": "ready", "sentences": [
        {"text": "这篇文献支持有限的一般说明。", "evidenceRefs": [3]},
        {"text": "这一句仅为一般的观察安排。", "evidenceRefs": []},
        {"text": "同一篇文献还说明了研究的局限。", "evidenceRefs": [3]},
    ]}
    kwargs = dict(code="diet", facts={}, evidence={3: {"interpretation": {
        "effect": "Verified human evidence and explicit study limitations."}}}, allowed_refs={3})
    block = _validate_block(raw, **kwargs)
    assert block.evidence_refs == [3]
    assert block.evidence_claims['3'] == "这篇文献支持有限的一般说明。\n同一篇文献还说明了研究的局限。"
    assert block.text == "\n".join(s['text'] for s in raw['sentences'])
    assert '这一句仅为一般的观察安排' not in block.evidence_claims['3']
    assert _validate_block({"sentences": [{"text": "引用不存在的支持编号。", "evidenceRefs": [99]}]}, **kwargs) is None
    assert _validate_block({"sentences": [{"text": "结构不合法。", "evidenceRefs": [True]}]}, **kwargs) is None
    assert _validate_block({"sentences": []}, **kwargs) is None


def test_suggested_examination_cannot_become_completed_result_or_leak_rules():
    kwargs = dict(code="phase_4", facts={}, evidence={}, allowed_refs=set(), suggested_exams={"某项超声"})
    assert _validate_block({"text": "已由某项超声进一步评估。"}, **kwargs) is None
    assert _validate_block({"text": "某项超声结果显示没有异常。"}, **kwargs) is None
    assert _validate_block({"text": "可由医生判断是否需要某项超声评估。"}, **kwargs) is not None
    assert _validate_block({"text": "引用编号由程序统一生成。"}, **kwargs) is None
    assert _validate_block({"text": "不建议自行购买益生菌产品。"}, **kwargs) is not None
    assert _validate_block({"text": "推荐购买某产品。"}, **kwargs) is None


def test_missing_demographics_are_not_invented_and_verified_context_is_preserved():
    kwargs = dict(code="clinical_followup", facts={}, evidence={}, allowed_refs=set())
    raw = {"text": "如存在月经情况，复查时间可由接诊医生确认。"}
    assert _validate_block(raw, subject={"gender": "男"}, **kwargs) is None
    assert _validate_block(raw, subject={"gender": "女"}, **kwargs) is not None
    assert _validate_block(raw, **kwargs) is not None


def test_new_chapter_citations_survive_pruning_and_renumbering():
    doc = collect_source_facts(*source())
    doc.sections = [ClinicalReportBlock(code="diet", text="依据说明", evidenceRefs=[8], evidenceQuotes={"8": "source quote"}, evidenceClaims={"8": "限定支持句"}, evidenceFactIds={"8": ["source-fact"]})]
    refs = [EvidenceReference(number=8, category="guideline", title="Test clinical guide", source="Synthetic source")]
    retained = retain_referenced_evidence(refs, microbes=AbnormalMicrobesSection(), diseases=DiseaseRisksSection(), health=HealthManagementSection(), clinical_document=doc)
    assert len(retained) == 1
    assert retained[0].number == 1
    assert doc.sections[0].evidence_refs == [1]
    assert doc.sections[0].evidence_quotes == {"1": "source quote"}
    assert doc.sections[0].evidence_claims == {"1": "限定支持句"}
    assert doc.sections[0].evidence_fact_ids == {"1": ["source-fact"]}


def test_patient_facts_are_not_unioned_across_every_reference_in_paragraph():
    doc = collect_source_facts(*source())
    ids = [f.fact_id for f in doc.facts][:2]
    doc.sections = [ClinicalReportBlock(code="overview", text="分开引用的两条说明。", factIds=ids,
        evidenceRefs=[1, 2], evidenceFactIds={"1": [ids[0]], "2": [ids[1]]},
        evidenceClaims={"1": "第一条限定说明", "2": "第二条限定说明"})]
    refs = callback_sections(doc, DiseaseRisksSection(), [{"number": 1}, {"number": 2}])["evidence"]
    assert len(refs[0]["patient_related_facts"]) == len(refs[1]["patient_related_facts"]) == 1
    assert refs[0]["patient_related_facts"] != refs[1]["patient_related_facts"]


def test_citation_approval_requires_original_quotes_and_full_applicability():
    source_text = "Adult population source. This result supports a limited conclusion."
    valid = {"valid": True, "supportQuotes": ["This result supports a limited conclusion."],
             "unsupportedClaims": [], "applicability": "general", "clinicalRelevance": "direct", "scope": approved_scope()}
    assert _citation_review_passes(valid, source_text)
    assert not _citation_review_passes({"valid": True}, source_text)
    for change in ({"supportQuotes": []}, {"supportQuotes": ["Invented original sentence."]},
                   {"unsupportedClaims": ["不受支持的另一项作用"]}, {"applicability": "not_applicable"},
                   {"applicability": "unknown"}, {"applicability": {}}, {"valid": "true"},
                   {"clinicalRelevance": "unrelated"}, {"clinicalRelevance": None}):
        assert not _citation_review_passes({**valid, **change}, source_text)


@pytest.mark.parametrize("fault", ["missing", "wrong_fact", "fake_fact_quote", "fake_evidence_quote", "no_scope", "drug", "invalid_kind"])
def test_positive_review_cannot_override_patient_prerequisites_or_report_scope(fault):
    # Synthetic, unlisted disease: applicability never depends on a dictionary.
    text = "This intervention was studied only in adults with synthetic subtype Z."
    fact = {"name": "来源结论", "value": "synthetic subtype Z", "status": "已确认"}
    review = {"valid": True, "supportQuotes": [text], "unsupportedClaims": [],
        "applicability": "applicable", "clinicalRelevance": "direct", "claimRole": "management",
        "scope": {"population": "adults with synthetic subtype Z", "missing": [],
            "adviceKind": "general_care", "requirements": [{"condition": "synthetic subtype Z",
                "evidenceQuote": "adults with synthetic subtype Z", "factId": "source", "factQuote": "synthetic subtype Z"}]}}
    assert _citation_review_passes(review, text, node="action", facts={"source": fact})
    scope = review["scope"]
    if fault == "missing":
        scope["missing"] = ["尚未确定病因亚型"]
    elif fault == "wrong_fact":
        scope["requirements"][0]["factId"] = "another_patient"
    elif fault == "fake_fact_quote":
        scope["requirements"][0]["factQuote"] = "inferred subtype"
    elif fault == "fake_evidence_quote":
        scope["requirements"][0]["evidenceQuote"] = "invented research population"
    elif fault == "no_scope":
        review.pop("scope")
    elif fault == "drug":
        scope["adviceKind"] = "drug_or_procedure"
    else:
        scope["adviceKind"] = {"unknown": "shape"}
    assert not _citation_review_passes(review, text, node="action", facts={"source": fact})


def test_uncited_care_is_allowed_without_research_but_not_new_medical_inference():
    review = approved_review({"id": "0", "node": "action", "evidence": None})
    assert _citation_review_passes(review, None, node="action")
    for role in ("etiology", "mechanism", "outcome"):
        assert not _citation_review_passes({**review, "claimRole": role}, None, node="action")
    assert not _citation_review_passes({**review, "claimRole": "source_fact"}, None, node="action")
    for role in ("uncertainty", "management", "background"):
        assert _citation_review_passes({**review, "claimRole": role}, None, node="action")


@pytest.mark.parametrize("kind,expected_use", [
    ("function", "source_model_result"), ("microbe", "microbiome_result"),
    ("self_reported", "patient_report"), ("context", "source_context"),
])
@pytest.mark.parametrize("use", [None, "clinical_record", "correct"])
def test_source_kind_cannot_be_upgraded_by_a_positive_reviewer(kind, expected_use, use):
    from modules.report.schemas import ReportSourceFact
    fact = ReportSourceFact(factId="unlisted-fact", kind=kind, name="字典外来源项目",
        value="17", status="偏低", source="合成资料")
    requirement = {"condition": "本句使用的患者前提", "factId": fact.fact_id,
        "factQuote": "17", "evidenceQuote": ""}
    if use is not None:
        requirement["useAs"] = expected_use if use == "correct" else use
    review = {**approved_review({"id": "0", "node": "nutrition", "evidence": None}),
        "claimRole": "source_fact"}
    review["scope"]["requirements"] = [requirement]
    assert _citation_review_passes(review, None, node="nutrition",
        facts={fact.fact_id: fact.model_dump(mode="json", by_alias=True)}) == (use == "correct")


def test_same_named_lab_and_model_results_keep_separate_allowed_uses():
    from modules.report.schemas import ReportSourceFact
    clinical = ReportSourceFact(factId="lab", kind="clinical", name="未收录成分",
        value="17", status="偏低", source="检验单")
    model = clinical.model_copy(update={"fact_id": "model", "kind": "function", "source": "菌群模型"})
    facts = {f.fact_id: f.model_dump(mode="json", by_alias=True) for f in (clinical, model)}
    review = {**approved_review({"id": "0", "node": "nutrition", "evidence": None}),
        "claimRole": "source_fact"}
    requirement = {"condition": "临床测得该成分偏低", "factId": "lab",
        "factQuote": "偏低", "evidenceQuote": "", "useAs": "clinical_record"}
    review["scope"]["requirements"] = [requirement]
    assert _citation_review_passes(review, None, node="nutrition", facts=facts)
    requirement["factId"] = "model"
    assert not _citation_review_passes(review, None, node="nutrition", facts=facts)


@pytest.mark.parametrize("kind,use", [("function", "source_model_result"),
    ("microbe", "microbiome_result"), ("context", "source_context")])
@pytest.mark.parametrize("purpose,allowed", [(None, False), ("clinical_action", False),
    ("microbiome_action", True), ("description", True), ("unverified", False)])
def test_auxiliary_values_cannot_be_clinical_action_indications(kind, use, purpose, allowed):
    from modules.report.schemas import ReportSourceFact
    fact = ReportSourceFact(factId="aux", kind=kind, name="未收录项目", value="17",
        status="偏低", source="合成辅助报告")
    review = approved_review({"id": "0", "node": "nutrition", "evidence": None})
    requirement = {"condition": "本句所依据的患者前提", "factId": "aux", "factQuote": "偏低",
        "evidenceQuote": "", "useAs": use}
    if purpose is not None:
        requirement["purpose"] = purpose
    review["scope"]["requirements"] = [requirement]
    assert _citation_review_passes(review, None, node="nutrition",
        facts={"aux": fact.model_dump(by_alias=True)}) is allowed


def test_real_clinical_action_basis_is_not_blocked_by_an_identical_model_value():
    from modules.report.schemas import ReportSourceFact
    lab = ReportSourceFact(factId="lab", kind="clinical", name="未收录成分", value="17",
        status="偏低", source="临床检测")
    model = lab.model_copy(update={"fact_id": "aux", "kind": "function", "source": "模型评估"})
    facts = {f.fact_id: f.model_dump(by_alias=True) for f in (lab, model)}
    review = approved_review({"id": "0", "node": "clinical_followup", "evidence": None})
    review["scope"]["adviceKind"] = "referral_or_test"
    requirement = {"condition": "实际检查结果需要复核", "factId": "lab", "factQuote": "偏低",
        "evidenceQuote": "", "useAs": "clinical_record", "purpose": "clinical_action"}
    review["scope"]["requirements"] = [requirement]
    assert _citation_review_passes(review, None, node="clinical_followup", facts=facts)
    requirement.update(factId="aux", useAs="source_model_result")
    assert not _citation_review_passes(review, None, node="clinical_followup", facts=facts)
    # An unconditional general-care explanation needs no invented abnormality.
    review["scope"].update(requirements=[], adviceKind="general_care")
    assert _citation_review_passes(review, None, node="clinical_followup", facts=facts)


def test_wrong_node_names_remain_invalid_review_roles():
    quote = "Adults with this recorded finding may experience recurrent discomfort."
    review = approved_review({"id": "0", "node": "outlook", "evidence": {"text": quote}})
    assert _citation_review_passes(review, quote, node="outlook")
    for invalid in ("outlook", "causes", "action", "unknown"):
        assert not _citation_review_passes({**review, "claimRole": invalid}, quote, node="outlook")


@pytest.mark.parametrize("code", ["nutrition", "diet", "exercise", "clinical_followup", "monitoring", "overview"])
@pytest.mark.parametrize("kind,allowed", [("function", False), ("microbe", False),
    ("clinical", True), ("self_reported", True)])
def test_clinical_sections_cannot_reference_auxiliary_scores_even_with_same_name(code, kind, allowed):
    from modules.report.schemas import ReportSourceFact
    fact = ReportSourceFact(factId="f", kind=kind, name="字典外同名项目", value="17", source="合成来源")
    block = _validate_block({"text":"需要核实已有资料的实际意义。", "factIds":["f"]},
        code=code, facts={"f":fact}, evidence={}, allowed_refs=set())
    assert (block is not None) is allowed


@pytest.mark.parametrize("condition_quote,allowed", [("Adults", True), ("Adults with", True),
    ("", False), ("   ", False), ("Children", False), (None, False)])
def test_short_literal_population_condition_keeps_full_claim_evidence_checks(condition_quote, allowed):
    quote = "Adults with this recorded finding may experience recurrent discomfort."
    review = approved_review({"id": "0", "node": "outlook", "evidence": {"text": quote}})
    review["scope"]["requirements"] = [{"condition": "成年", "evidenceQuote": condition_quote,
        "factId": "subject.age", "factQuote": "36", "useAs": "subject_attribute", "purpose": "eligibility"}]
    facts = {"subject.age": {"kind": "subject_attribute", "value": "36"}}
    assert _citation_review_passes(review, quote, node="outlook", facts=facts) is allowed
    # An eligibility word alone never proves the medical outcome claim.
    review["supportQuotes"] = ["Adults"]
    assert not _citation_review_passes(review, quote, node="outlook", facts=facts)


@pytest.mark.asyncio
async def test_composition_short_ids_round_trip_only_identifier_fields():
    import copy
    from modules.report.clinical_report import _generate_composition
    from modules.report.schemas import ReportSourceFact
    fid = "clinical-fact-original-long-unchanged-0123456789"
    fact = ReportSourceFact(factId=fid, kind="clinical", name="字典外检验", value="17", unit="U/L", source="合成检验单")
    payload = {"task": "compose_clinical_report", "rules": [],
        "facts": [fact.model_dump(mode="json", by_alias=True)],
        "problems": [{"problemId": "p", "sources": [{"factId": fid, "quote": fid}]}],
        "evidence": [{"supports": [fid, "p"], "summary": fid}],
        "finalizedContent": {"sections": [{"factIds": [fid], "text": fid,
            "evidenceFactIds": {"1": [fid]}}]},
        "actionCatalog": [{"id": "action-original", "factIds": [fid], "context": fid}],
        "background": {"factId": fid}, "validationErrors": [{"reason": fid}]}
    before = copy.deepcopy(payload)
    class ExactModel:
        async def generate(self, request):
            assert request["facts"][0]["factId"] == "F1"
            assert request["facts"][0]["value"] == "17"
            assert request["facts"][0]["unit"] == "U/L"
            assert request["problems"] == [{"problemId": "p", "sources": [{"factId": "F1", "quote": fid}]}]
            assert request["evidence"] == [{"supports": ["F1", "p"], "summary": fid}]
            assert request["finalizedContent"]["sections"][0] == {
                "factIds": ["F1"], "text": fid, "evidenceFactIds": {"1": ["F1"]}}
            assert request["actionCatalog"] == [{"id": "action-original", "factIds": ["F1"], "context": fid}]
            assert request["background"] == before["background"]
            assert request["validationErrors"] == before["validationErrors"]
            return {"sections": [{"code": "diet", "factIds": ["F1"], "text": "F1",
                "evidenceFactIds": {"1": ["F1"]}}],
                "analyses": [{"problemId": "p", "action": {"factIds": ["F1"]}}]}
    response = await _generate_composition(ExactModel(), payload, {fid: fact})
    assert payload == before
    assert fact.fact_id == fid
    assert response["sections"][0] == {"code": "diet", "factIds": [fid], "text": "F1",
        "evidenceFactIds": {"1": [fid]}}
    assert response["analyses"][0]["action"]["factIds"] == [fid]


@pytest.mark.asyncio
async def test_short_fact_handles_are_deterministic_collision_free_and_request_local():
    from modules.report.clinical_report import _generate_composition
    from modules.report.schemas import ReportSourceFact
    first = {fid: ReportSourceFact(factId=fid, kind="clinical", name="同名记录", value="17", source="合成检验单")
        for fid in ("z-long-id", "F1", "_F1")}
    second = {"other-user": ReportSourceFact(factId="other-user", kind="function", name="同名记录", value="17", source="合成菌群报告")}
    class Echo:
        def __init__(self, original_ids):
            self.original_ids = original_ids

        async def generate(self, payload):
            await asyncio.sleep(0)
            ids = [f["factId"] for f in payload["facts"]]
            assert len(set(ids)) == len(ids)
            assert not set(ids) & set(self.original_ids)
            expected = {fid: f"__F{index}" for index, fid in enumerate(sorted(first), 1)} if len(ids) == 3 else {"other-user": "F1"}
            assert ids == [expected[fid] for fid in self.original_ids]
            return {"sections": [{"factIds": ids}]}
    async def run(facts):
        return await _generate_composition(Echo(facts), {"facts": [f.model_dump(by_alias=True) for f in facts.values()]}, facts)
    one, two, reverse = await asyncio.gather(run(first), run(second), run(dict(reversed(list(first.items())))))
    assert one["sections"][0]["factIds"] == list(first)
    assert two["sections"][0]["factIds"] == list(second)
    assert reverse["sections"][0]["factIds"] == list(reversed(first))


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["F9999", "F01", "f1", "F1 ", "long-id-with-one-typo"])
async def test_unknown_or_mistyped_short_reference_is_never_guessed(invalid):
    from modules.report.clinical_report import _generate_composition
    from modules.report.schemas import ReportSourceFact
    fact = ReportSourceFact(factId="long-id", kind="clinical", name="字典外指标", value="17", source="合成检验单")
    class Wrong:
        async def generate(self, payload):
            return {"sections": [{"code": "diet", "state": "ready", "text": "先了解饮食情况。", "factIds": [invalid]}]}
    output = await _generate_composition(Wrong(), {"facts": [fact.model_dump(by_alias=True)]}, {fact.fact_id: fact})
    raw = output["sections"][0]
    assert raw["factIds"] == [invalid]
    assert _validate_block(raw, code="diet", facts={fact.fact_id: fact}, evidence={}, allowed_refs=set()) is None


@pytest.mark.parametrize("fault", [None, "value", "unit", "date", "other_fact", "invented_quote_text"])
def test_citation_patient_quote_can_use_exact_program_formatted_fact_only(fault):
    from modules.report.clinical_report import fact_text
    from modules.report.schemas import ReportSourceFact
    fact = ReportSourceFact(factId="unlisted-result", kind="clinical", name="字典外指标",
        value="17", unit="U/L", date="2020-01-02", referenceRange="0–9",
        status="偏高", source="来源报告")
    quote = fact_text(fact)
    if fault == "value":
        quote = quote.replace("17", "71")
    elif fault == "unit":
        quote = quote.replace("U/L", "mg/L")
    elif fault == "date":
        quote = quote.replace("2020-01-02", "2026-01-02")
    elif fault == "other_fact":
        quote = quote.replace("字典外指标", "另一次检查")
    elif fault == "invented_quote_text":
        quote = "已确诊某种新的疾病"
    data = fact.model_dump(mode="json", by_alias=True)
    data["quoteText"] = quote  # Validator must recompute, not trust this key.
    review = approved_review({"id": "0", "node": "action", "evidence": None})
    review["scope"]["requirements"] = [{"condition": "既往来源记录", "evidenceQuote": "",
        "factId": fact.fact_id, "factQuote": quote}]
    assert _citation_review_passes(review, None, node="action", facts={fact.fact_id: data}) == (fault is None)


@pytest.mark.parametrize("top,nested,expected", [
    ("referral_or_test", None, True), ("general_care", "general_care", True),
    ("drug_or_procedure", None, False), ("drug_or_procedure", "general_care", False),
    ("general_care", "drug_or_procedure", False), ("unknown", None, False),
])
def test_unique_review_classification_can_be_read_without_discarding_conflicts(top, nested, expected):
    review = approved_review({"id": "0", "node": "action", "evidence": None})
    review["adviceKind"] = top
    if nested is None:
        review["scope"].pop("adviceKind")
    else:
        review["scope"]["adviceKind"] = nested
    assert _citation_review_passes(review, None, node="action") is expected


def test_overview_source_fallback_keeps_self_reported_certainty():
    doc = collect_source_facts(*source())
    fact = doc.facts[0]
    doc.problems = [ClinicalReportProblem(problemId="reported", title="某项病史", certainty="self_reported",
        sources=[ClinicalProblemSource(factId=fact.fact_id, quote=fact.value)])]
    ensure_document_fallback(doc)
    assert "自述某项病史（待核实）" in next(b for b in doc.sections if b.code == "overview").text


@pytest.mark.parametrize("fault", ["source_quote_as_literature", "mechanism", "fabricated_literature", "drug"])
def test_review_schema_split_does_not_relax_existing_evidence_validation(fault):
    review = approved_review({"id":"0", "node":"monitoring", "evidence":None})
    source_text = None
    if fault == "source_quote_as_literature":
        review["supportQuotes"] = ["某器官结节，性质待查。"]
    elif fault == "mechanism":
        review["claimRole"] = "mechanism"
    elif fault == "fabricated_literature":
        source_text = "The actual source has a different result."
        review["supportQuotes"] = ["This fabricated claim is not in the source."]
    else:
        review["scope"]["adviceKind"] = "drug_or_procedure"
    assert not _citation_review_passes(review, source_text, node="monitoring")


@pytest.mark.parametrize("fault", ["empty", "unknown_source"])
def test_block_repair_distinguishes_empty_text_from_unknown_source_ids(fault):
    doc = collect_source_facts(*source())
    raw = {"text":"原资料可供复诊时核对。", "factIds":[], "evidenceRefs":[]}
    if fault == "empty":
        raw["text"] = ""
    else:
        raw["factIds"] = ["misspelled-source-id"]
    errors = []
    assert _validate_block(raw, code="phase_4", facts={f.fact_id:f for f in doc.facts},
        evidence={}, allowed_refs=set(), rejections=errors) is None
    assert ("正文为空" in errors[0]["reason"]) == (fault == "empty")
    assert ("misspelled-source-id" in errors[0]["reason"]) == (fault == "unknown_source")


def test_overview_does_not_repeat_specific_microbes():
    doc = collect_source_facts(*source())
    assert _validate_block({"text": "Synthetic taxon 1 是本次关注的菌群。"}, code="overview",
        facts={f.fact_id: f for f in doc.facts}, evidence={}, allowed_refs=set(), needs_evidence=False) is None


@pytest.mark.parametrize("node,role", [("causes", "etiology"), ("mechanism", "mechanism"),
    ("outlook", "outcome"), ("action", "management"), ("goal", "management")])
def test_real_reference_must_explain_the_requested_analysis_node(node, role):
    original = "A documented finding in a clinical study."
    review = {"valid": True, "supportQuotes": [original], "unsupportedClaims": [],
        "applicability": "general", "clinicalRelevance": "direct", "claimRole": role, "scope": approved_scope()}
    assert _citation_review_passes(review, original, node=node)
    for wrong in ("diagnostic_accuracy", "background", None):
        assert not _citation_review_passes({**review, "claimRole": wrong}, original, node=node)
    # A genuine association is still not a biological mechanism.
    assert not _citation_review_passes({**review, "claimRole": "outcome"}, original, node="mechanism")


def test_clinical_source_text_is_original_not_model_interpretation():
    from modules.report.pipeline import _evidence_summaries
    from modules.report.clinical_report import _citation_source_text
    record = EvidenceRecord(evidenceKey="source", source="synthetic", category="literature", title="Source review",
        abstract="Original human study results.", summary="Shorter summary.")
    legacy = _evidence_summaries([record], {"source": 1})
    clinical = _evidence_summaries([record], {"source": 1}, include_source=True)
    assert "sourceText" not in legacy[0]
    assert _citation_source_text(clinical[0]) == record.abstract
    assert _citation_source_text({"interpretation": {"claim": "模型生成的支持结论"}}) == ""


@pytest.mark.parametrize('claim', [
    '微生态方面没有需要据此调整的菌群结果。',
    '本次没有直接检测肠道屏障功能的临床检查。',
    '现有资料未进行相关检查。',
])
def test_absence_assertions_cannot_hide_behind_alternate_patient_wording(claim):
    from modules.report.clinical_report import _needs_source_absence_check
    assert _needs_source_absence_check(claim)


def test_clinical_screen_does_not_require_gut_specimen():
    doc = collect_source_facts(*source())
    doc.problems = [ClinicalReportProblem(problemId="p", title="某器官结节", searchTerms=["organ nodule"])]
    record = EvidenceRecord(evidenceKey="a", category="guideline", title="organ nodule clinical assessment guideline", source="Synthetic source")
    assert _screen(record, query=clinical_queries(doc, 40)[0])[0]


@pytest.mark.parametrize("text, expected", [
    ("Adult patients with organ nodules need clinical assessment.", True),
    ("Adult patients with organ and tissue nodules need assessment.", True),
    ("Adult patients with unrelated organ failure and a long discussion of lung nodules.", False),
    ("Adult organ. Unrelated nodules in another sentence.", False),
    ("Adults with non-organ nodules are excluded.", False),
    ("Adults with organ dysfunction without the second concept.", False),
])
def test_clinical_initial_screen_accepts_bounded_plural_phrases_only(text, expected):
    from modules.report.evidence import _clinical_concept_match_score
    assert bool(_clinical_concept_match_score(text, "organ nodule")) is expected


def test_recall_expansion_uses_original_abstract_and_does_not_change_legacy_or_taxa():
    from dataclasses import replace
    from modules.report.evidence import EvidenceQuery
    query = EvidenceQuery(query="organ nodule", required_concepts=(("organ nodule",),), supports=("p",),
        disease_id="p", target="organ nodule", intent="disease_clinical_significance", screen_profile="clinical_report")
    record = EvidenceRecord(evidenceKey="r", category="literature", source="synthetic",
        title="Clinical review in adults", summary="A short summary without the query concept.",
        abstract="Human adults with organ and tissue nodules were evaluated.")
    assert _screen(record, query=query)[0]
    assert not _screen(record, query=replace(query, screen_profile="default"))[0]
    taxon = EvidenceRecord(evidenceKey="t", category="literature", source="synthetic",
        title="Human adult stool study", summary="Synthetic unrelated taxons in adult participants.")
    taxon_query = replace(query, microbe_id="t", disease_id=None, required_concepts=(("Synthetic taxon",),), intent="gut_function")
    assert not _screen(taxon, query=taxon_query)[0]


def test_clinical_queries_use_source_aliases_and_public_alternatives():
    doc = collect_source_facts(*source())
    doc.problems = [ClinicalReportProblem(problemId="p", title="某器官结节", searchTerms=["器官结节", "organ nodule"])]
    primary = clinical_queries(doc, 40)[0]
    public = clinical_queries(doc, 40, public=True)[0]
    assert primary.query == "organ nodule etiology pathophysiology"
    assert primary.query_language == "english"
    assert public.query == '"organ nodule" AND (etiology OR pathophysiology OR natural history OR review)'
    assert primary.supports == public.supports == ("p",)
    assert "器官结节" in primary.required_concepts[0]
    problem_queries = [q for q in clinical_queries(doc, 40) if q.disease_id == "p"]
    assert len(problem_queries) == 4
    assert {q.query for q in problem_queries} == {
        "organ nodule etiology pathophysiology", "organ nodule management",
        "organ nodule nutrition diet prevention", "organ nodule diagnosis",
    }
    nutrient = next(q for q in problem_queries if "nutrition diet" in q.query)
    assert nutrient.supports == ("p", "clinical-section:nutrition")


@pytest.mark.asyncio
async def test_clinical_retrieval_keeps_candidates_after_old_health_cap():
    from modules.report.evidence import retrieve_evidence
    query = next(q for q in clinical_queries(collect_source_facts(*source()), 40)
        if q.supports == ("clinical-section:diet",))
    class Evidence:
        async def search(self, text, *, supports):
            return [EvidenceRecord(evidenceKey=f"source-{i}", source="synthetic", category="literature",
                title=f"Human dietary nutrition review {i}", abstract="Adult human diet findings.",
                supports=supports) for i in range(8)]
    result = await retrieve_evidence(Evidence(), [query])
    assert len(result.records) == 8
    assert result.query_audits[0].retained_count == 8


@pytest.mark.asyncio
async def test_later_valid_candidates_survive_early_rejections_and_malformed_items():
    from modules.report.clinical_report import assess_clinical_evidence
    doc = collect_source_facts(*source())
    records = [EvidenceRecord(evidenceKey=f"r{i}", source="synthetic", category="literature",
        title=f"Human dietary review {i}", summary="A precise adult dietary finding.",
        supports=["clinical-section:diet"], intents=["health_management"]) for i in range(18)]
    calls = []
    class Assessor:
        async def generate(self, payload):
            calls.append([r["id"] for r in payload["records"]])
            return {"items": [{"id": [], "applicable": True},
                {"id": payload["records"][0]["id"], "applicable": True, "supports": [{}]},
                *[{"id": r["id"], "applicable": r["id"] in {"r6", "r7"},
                   "supports": ["clinical-section:diet"], "quote": "A precise adult dietary finding.",
                   "claim": "有限的人体膳食研究结论"} for r in payload["records"]]]}
    accepted = await assess_clinical_evidence(Assessor(), records, doc)
    assert [r.evidence_key for r in accepted] == ["r6", "r7"]
    assert len(calls) == 2  # Stops after actual validated scope coverage, not two raw hits.
    assert not doc.incomplete


@pytest.mark.asyncio
async def test_analysis_continues_past_two_accepted_diagnostic_background_papers():
    from modules.report.clinical_report import assess_clinical_evidence, clinical_evidence_coverage
    doc = collect_source_facts(*source())
    doc.problems = [ClinicalReportProblem(problemId="p", title="某器官结节")]
    quote = "An exact original adult clinical finding with its limitations."
    records = [EvidenceRecord(evidenceKey=f"r{i}", source="synthetic", category="literature",
        title=f"Human clinical review {i}", summary=quote,
        supports=["p"], intents=["disease_clinical_significance"]) for i in range(18)]
    calls = []
    class Assessor:
        async def generate(self, payload):
            calls.append([r["id"] for r in payload["records"]])
            return {"items": [{"id": r["id"], "applicable": True, "supports": ["p"],
                "quote": quote, "claim": "有范围限定的临床研究结论",
                "analysisRoles": [{"support": "p", "role": role, "quote": quote}
                    for role in ({"r6": ["etiology", "mechanism"], "r7": ["outcome"]}.get(r["id"], []))]}
                for r in payload["records"]]}
    accepted = await assess_clinical_evidence(Assessor(), records, doc)
    assert len(calls) == 2  # Does not stop after six accepted but diagnostic-only papers.
    assert [r.evidence_key for r in accepted] == [f"r{i}" for i in range(12)]
    assert ("p", "disease_clinical_significance") in clinical_evidence_coverage(accepted, doc)
    assert ("p", "disease_clinical_significance") not in clinical_evidence_coverage(accepted[:6], doc)
    assert "evidenceAnalysisRoles" not in doc.model_dump(by_alias=True)


@pytest.mark.asyncio
async def test_analysis_coverage_requires_this_problem_and_original_role_quote():
    from modules.report.clinical_report import assess_clinical_evidence, clinical_evidence_coverage
    doc = collect_source_facts(*source())
    doc.problems = [ClinicalReportProblem(problemId=p, title="某器官结节") for p in ("p", "other")]
    quote = "An exact original adult clinical finding with its limitations."
    record = EvidenceRecord(evidenceKey="r", source="synthetic", category="literature",
        title="Human clinical review", summary=quote, supports=["p"], intents=["disease_clinical_significance"])
    class Assessor:
        async def generate(self, payload):
            return {"items": [{"id": "r", "applicable": True, "supports": ["p"], "quote": quote,
                "claim": "有限结论", "analysisRoles": [
                    {"support": "p", "role": "etiology", "quote": quote},
                    {"support": "other", "role": "mechanism", "quote": quote},
                    {"support": "p", "role": "outcome", "quote": "Fabricated quotation."},
                    {"support": "p", "role": [], "quote": quote}, None]}]}
    accepted = await assess_clinical_evidence(Assessor(), [record], doc)
    assert len(accepted) == 1  # A valid limited source is retained, not discarded.
    assert doc.evidence_analysis_roles == {"r": {"p": ["etiology"]}}
    assert ("p", "disease_clinical_significance") not in clinical_evidence_coverage(accepted, doc)
    assert ("other", "disease_clinical_significance") not in clinical_evidence_coverage(accepted, doc)
    extra = record.model_copy(update={"evidence_key": "fallback"})
    doc.evidence_analysis_roles["fallback"] = {"p": ["mechanism", "outcome"]}
    assert ("p", "disease_clinical_significance") in clinical_evidence_coverage([*accepted, extra], doc)


@pytest.mark.asyncio
async def test_single_character_title_does_not_become_a_clinical_problem():
    doc = collect_source_facts(*source())
    class Fragment(Model):
        async def generate(self, payload):
            result = await super().generate(payload)
            result["problems"][0]["title"] = "器"
            return result
    await organize_problems(Fragment(), doc)
    assert all(p.title != "器" for p in doc.problems)
    assert "CLINICAL_PROBLEM_TITLE_MISMATCH" in doc.incomplete
    assert doc.facts[0].value == "某器官结节，性质待查。"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_analysis_paper, roles, needs_analysis", [
    (False, [], True), (True, [], True), (True, ["etiology"], True),
    (True, ["etiology", "mechanism", "outcome"], False),
])
async def test_diagnostic_evidence_does_not_suppress_cause_and_management_fallback(monkeypatch, has_analysis_paper, roles, needs_analysis):
    from modules.report import pipeline as pipeline_module
    from modules.report.evidence import EvidenceRetrievalResult
    doc = collect_source_facts(*source())
    doc.problems = [ClinicalReportProblem(problemId="p", title="某器官结节", searchTerms=["organ nodule"])]
    calls = []
    async def retrieve(client, queries, **kwargs):
        calls.append(queries)
        records = [] if len(calls) > 1 else [EvidenceRecord(evidenceKey="diagnostic", source="synthetic",
            category="guideline", title="Diagnostic guide", supports=["p"], intents=["diagnostic_guideline"])]
        if len(calls) == 1 and has_analysis_paper:
            records.append(EvidenceRecord(evidenceKey="analysis", source="synthetic", category="literature",
                title="Clinical background", supports=["p"], intents=["disease_clinical_significance"]))
            doc.evidence_analysis_roles["analysis"] = {"p": roles}
        return EvidenceRetrievalResult(records=records, failed_supports=set(), query_audits=[], relation_coverage=[])
    async def assess(client, records, document, **kwargs):
        return records
    monkeypatch.setattr(pipeline_module, "_retrieve_evidence_until", retrieve)
    monkeypatch.setattr(pipeline_module, "assess_clinical_evidence", assess)
    async def no_session(*args, **kwargs):
        return None
    pipeline = ReportPipeline(source_client=object(), evidence_client=object(), public_evidence_client=object(),
        callback_client=object(), session_loader=no_session, narrative_client=None)
    await pipeline._retrieve_clinical_evidence(doc, SubjectInfo(age=40), None, 1, 2, {}, None,
        AbnormalMicrobesSection(), DiseaseRisksSection())
    fallback_intents = {q.intent for q in calls[1] if q.disease_id == "p"}
    assert fallback_intents == ({"health_management", "disease_clinical_significance"}
        if needs_analysis else {"health_management"})
    assert "CLINICAL_EVIDENCE_COVERAGE_INCOMPLETE" in doc.incomplete


def test_timeout_keeps_clinical_document_and_original_assessment():
    request, parsed = scored_source()
    doc = collect_source_facts(request, parsed)
    assessment = build_health_assessment(request, parsed, {})
    assert any(d.score_available for d in assessment.dimensions)
    progress = _GenerationProgress(parsed=parsed, microbes=AbnormalMicrobesSection(), diseases=DiseaseRisksSection(),
        health=HealthManagementSection(), health_assessment=assessment, clinical_document=doc)
    result = _deadline_partial_payload(request, progress)
    body = _fixed_callback_body(result)
    assert body["status"] == "partial_success"
    assert body["diagnosis_summary"]["conclusion"]
    assert body["diagnosis_summary"]["clinical_context"]["report.version"] == "clinical-v1"
    assert len(body["diagnostic_basis"]["abnormal_findings"]) == 5
    assert body["healthAssessment"] == assessment.model_dump(mode="json", by_alias=True)


def test_timeout_preserves_validated_chapters_with_citation_remapping():
    request, parsed = source()
    doc = collect_source_facts(request, parsed)
    doc.sections = [ClinicalReportBlock(code="diet", text="可保留的已验证建议。", evidenceRefs=[8],
        evidenceQuotes={"8": "Original human evidence statement."}, evidenceClaims={"8": "可保留的已验证建议。"})]
    progress = _GenerationProgress(parsed=parsed, microbes=AbnormalMicrobesSection(), diseases=DiseaseRisksSection(),
        health=HealthManagementSection(), health_assessment=HealthAssessment(), clinical_document=doc,
        references=[EvidenceReference(number=8, category="guideline", title="Original guide", source="Synthetic source")])
    body = _fixed_callback_body(_deadline_partial_payload(request, progress))
    assert body["diagnosis_summary"]["clinical_context"]["report.section.diet"] == "可保留的已验证建议。"
    assert body["evidence"][0]["number"] == 1
    assert body["evidence"][0]["conclusion"] == "可保留的已验证建议。"
    assert doc.sections[0].evidence_refs == [8]


@pytest.mark.asyncio
async def test_production_enabled_pipeline_routes_clinical_document_to_callback(monkeypatch):
    from modules.report import pipeline as pipeline_module
    from modules.report.ports import EviMedChannelResult
    class EmptyEvidence:
        async def search_detailed(self, query, *, supports, channels):
            return [EviMedChannelResult(channel, "success", []) for channel in channels]
    async def no_session(*args):
        return {"messages": [{"role": "user", "content": "最近睡眠不规律"},
            {"role": "assistant", "content": "模型此前推断的疾病"}]}
    request, _ = scored_source()
    scored = []
    def original_scoring(*args, **kwargs):
        value = build_health_assessment(*args, **kwargs)
        scored.append(value.model_dump(mode="json", by_alias=True))
        return value
    monkeypatch.setattr(pipeline_module, "build_health_assessment", original_scoring)
    model = Model()
    pipeline = ReportPipeline(source_client=object(), evidence_client=EmptyEvidence(),
        callback_client=object(), session_loader=no_session, narrative_client=model,
        clinical_report_enabled=True)
    result = await pipeline.generate(request)
    body = _fixed_callback_body(result)
    assert body["diagnosis_summary"]["clinical_context"]["report.version"] == "clinical-v1"
    assert "待核实" in body["diagnostic_basis"]["abnormal_findings"][0]["problem"]
    assert any(d["scoreAvailable"] for d in scored[0]["dimensions"])
    assert body["healthAssessment"] == scored[0]
    chapter_calls = [c for c in model.calls if c["task"] in {"organize_clinical_problems", "compose_clinical_report"}]
    assert chapter_calls
    assert all(c["background"]["patientReported"] == {
        "messages": [{"role": "user", "content": "最近睡眠不规律"}]} for c in chapter_calls)


@pytest.mark.asyncio
async def test_pipeline_can_publish_self_reported_symptom_without_pdf_or_hospital_rows():
    from modules.report.ports import EviMedChannelResult
    class EmptyEvidence:
        async def search_detailed(self, query, *, supports, channels):
            return [EviMedChannelResult(channel, "success", []) for channel in channels]
    async def session(*args):
        return {"messages": [{"role": "user", "content": "最近一周饭后腹胀。"}]}
    class Reported(Model):
        async def generate(self, payload):
            if payload["task"] == "extract_reported_clinical_facts":
                return {"items": [{"id": i["id"], "assertions": [{"name": "饭后腹胀", "quote": i["text"],
                    "subject": "self", "assertion": "affirmed", "kind": "symptom", "timeQuote": "最近一周"}]} for i in payload["items"]]}
            if payload["task"] == "organize_clinical_problems":
                f = payload["facts"][0]
                return {"problems": [{"title": "饭后腹胀", "certainty": "confirmed", "sources": [
                    {"factId": f["factId"], "quote": f["value"]}], "searchTerms": ["postprandial bloating"]}]}
            return await super().generate(payload)
    request = ReportRequest(userId="synthetic", sessionId="only-symptom")
    pipeline = ReportPipeline(source_client=object(), evidence_client=EmptyEvidence(),
        callback_client=object(), session_loader=session, narrative_client=Reported(), clinical_report_enabled=True)
    body = _fixed_callback_body(await pipeline.generate(request))
    context = body["diagnosis_summary"]["clinical_context"]
    findings = body["diagnostic_basis"]["abnormal_findings"]
    assert len(findings) == 1 and findings[0]["problem"] == "饭后腹胀（用户自述）"
    assert context[f'report.problem.{findings[0]["item_id"]}.certainty'] == "self_reported"
    assert len([k for k in context if k.startswith("report.fact.self_reported.")]) == 1
    assert body["diagnosis_summary"]["conclusion"]
    assert not body["healthAssessment"]["overallScoreAvailable"]
