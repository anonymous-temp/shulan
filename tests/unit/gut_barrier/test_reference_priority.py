"""Supplied reviewed mechanisms affect citations, never patient risk scores."""
from copy import deepcopy
import json
import re

import pytest

from modules.gut_barrier.barrier_context import catalog_mechanism_reference_ids
from modules.gut_barrier.dynamic_diseases import _reference as current_reference
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.reference_order import REFERENCE_FIELDS, publish_ordered_references
from modules.gut_barrier.reference_priority import preferred_mechanism, reference_selection_audit
from modules.gut_barrier.template_risk_review import apply_template_risk_review
from tests.unit.gut_barrier.test_template_risk_review import (
    _assessment, _overview, _references, _report, _rows, _selected,
)


PREFERRED_DOIS = {
    "ibs": {"10.1038/nrdp.2016.14"},
    "ibd": {"10.1159/000447252", "10.1053/j.gastro.2017.05.056"},
    "nafld": {"10.1159/000447252"},
}
ADDITIONAL_IDS = {
    "ibs": ["Q33"],
    "ibd": ["Q32"],
    "nafld": ["Q35"],
}
CLINICAL_GUIDELINE_IDS = {
    "ibs": ["Q5"],
    "ibd": ["Q5", "Q11", "Q14"],
    "nafld": ["Q1", "Q2"],
}
DISPLAY_NAMES = {"ibs": "肠易激综合征", "ibd": "炎症性肠病", "nafld": "代谢相关脂肪性肝病"}



def _reference(identifier):
    # Frozen assessments can still carry these historical clinical sources.
    # Their removed questionnaire triggers must not be reintroduced to test replay.
    archived = {
        "Q5": {"citation": "[评估5] ACG Clinical Guideline: Management of Irritable Bowel Syndrome",
               "year": 2021, "publisher": "American Journal of Gastroenterology",
               "doi": "10.14309/ajg.0000000000001036", "url": "https://pubmed.ncbi.nlm.nih.gov/33315591/", "location": None},
        "Q11": {"citation": "[评估11] AGA Clinical Practice Guidelines on the Laboratory Evaluation of Functional Diarrhea and Diarrhea-Predominant Irritable Bowel Syndrome in Adults",
                "year": 2019, "publisher": "Gastroenterology", "doi": "10.1053/j.gastro.2019.07.004",
                "url": "https://gastro.org/clinical-guidance/laboratory-evaluation-of-functional-diarrhea-and-diarrhea-predominant-irritable-bowel-syndrome-in-adults-ibs-d/", "location": None},
        "Q14": {"citation": "[评估14] Guidelines for the investigation of chronic diarrhoea in adults: British Society of Gastroenterology, 3rd edition",
                "year": 2018, "publisher": "Gut", "doi": "10.1136/gutjnl-2017-315909",
                "url": "https://pubmed.ncbi.nlm.nih.gov/29653941/", "location": None},
    }
    return deepcopy(archived[identifier]) if identifier in archived else current_reference(identifier)

def _cited_references(report, cells):
    references = _references(report)
    return [references[int(number) - 1] for number in re.findall(r"\[(\d+)\]", cells[3])]


@pytest.mark.parametrize("code", PREFERRED_DOIS)
def test_selection_uses_verified_supplied_publications_and_returns_isolated_data(code):
    selection = preferred_mechanism(code)
    assert {source["doi"] for source in selection["sources"]} == PREFERRED_DOIS[code]
    assert selection["additionalEvidenceIds"] == ADDITIONAL_IDS[code]
    assert selection["text"].count("。") == 1
    for source in selection["sources"]:
        assert set(REFERENCE_FIELDS) <= set(source)
        assert source["providedFile"].endswith(".pdf")
        assert re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
        assert source["reviewedPages"] and all(page >= 1 for page in source["reviewedPages"])
        assert source["url"].startswith("https://pubmed.ncbi.nlm.nih.gov/")
    original = deepcopy(selection)
    selection["sources"][0]["citation"] = "must not replace registered source"
    selection["sources"][0]["reviewedPages"].clear()
    selection["additionalEvidenceIds"].clear()
    assert preferred_mechanism(code) == original


def test_selection_does_not_guess_sources_for_unmatched_diseases_and_keeps_audit_internal():
    assert preferred_mechanism("lung_cancer") is None
    assert preferred_mechanism("unregistered_disease") is None
    audit = reference_selection_audit()
    assert set(audit["diseaseCodes"]) == set(PREFERRED_DOIS)
    assert len(audit["sources"]) == 3
    assert {source["doi"] for source in audit["sources"].values()} == set.union(*PREFERRED_DOIS.values())
    assert audit["mode"] == "matched_mechanism_only_not_score_validation"
    original = deepcopy(audit)
    audit["sources"].clear()
    audit["diseaseCodes"].clear()
    assert reference_selection_audit() == original


@pytest.mark.parametrize("code", PREFERRED_DOIS)
def test_preferred_citations_apply_without_a_dynamic_result_or_invented_scores(code):
    report = _report()
    apply_template_risk_review(report, {})
    cells = next(row for row in _rows(report) if row[0] == DISPLAY_NAMES[code])
    assert cells[1:3] == [None, None]
    assert {reference["doi"] for reference in _cited_references(report, cells)} == PREFERRED_DOIS[code]
    assert len(_rows(report)) == 23 and _overview(report)["rows"] == []
    assert "动物" not in cells[3] and "小鼠" not in cells[3]


@pytest.mark.parametrize("code", PREFERRED_DOIS)
def test_preferred_mechanism_overrides_old_text_preserving_only_scoped_human_studies(code):
    selected = {**_selected(code, status="evaluated"), "riskIndex": 7.2, "riskLevel": "high"}
    old_mechanism_ids = catalog_mechanism_reference_ids(code)
    selected["qualitative"]["evidenceIds"] = [
        *ADDITIONAL_IDS[code], *CLINICAL_GUIDELINE_IDS[code], *old_mechanism_ids,
    ]
    selected["mechanism"] = "旧动态机制不能绕过已核验的人体文献选择。"
    assessment = _assessment(selected, model=True)
    assessment["diseaseAssessment"]["references"] = [
        _reference(identifier) for identifier in selected["qualitative"]["evidenceIds"]
    ]
    report = _report([selected["name"], 7.2, "high", selected["mechanism"] + "[1]"])
    original_assessment = deepcopy(assessment)
    apply_template_risk_review(report, assessment)
    cells = next(row for row in _rows(report) if row[0] == DISPLAY_NAMES[code])
    assert cells[1:3] == [7.2, "高"]
    assert cells[3].startswith(preferred_mechanism(code)["text"])
    assert "旧动态机制" not in cells[3]
    cited = _cited_references(report, cells)
    assert PREFERRED_DOIS[code] <= {reference["doi"] for reference in cited}
    expected_urls = {source["url"] for source in preferred_mechanism(code)["sources"]}
    expected_urls.update(_reference(identifier)["url"] for identifier in ADDITIONAL_IDS[code])
    assert {reference["url"] for reference in cited} == expected_urls
    assert all(set(reference) == set(REFERENCE_FIELDS) and reference["location"] is None for reference in cited)
    assert len(_rows(report)) == 23
    assert _overview(report)["rows"] == [["消化系统", DISPLAY_NAMES[code], 7.2, "高"]]
    assert assessment == original_assessment


@pytest.mark.parametrize("code", PREFERRED_DOIS)
def test_clinical_guidelines_remain_available_for_separate_clinical_content_not_mechanism_claims(code):
    selected = _selected(code)
    clinical_id = CLINICAL_GUIDELINE_IDS[code][0]
    selected["qualitative"]["evidenceIds"] = [clinical_id]
    assessment = _assessment(selected)
    clinical_reference = _reference(clinical_id)
    assessment["diseaseAssessment"]["references"] = [clinical_reference]
    safety = {"kind": "callout", "title": "实际临床安全提示", "tone": "caution",
              "text": "此处保留已经采纳的临床管理指引。[1]"}
    report = _report(blocks=[safety])
    public_reference = {key: deepcopy(clinical_reference.get(key)) for key in REFERENCE_FIELDS}
    public_reference.update(citation=re.sub(r"^\[评估\d+\]\s*", "", public_reference["citation"]), location=None)
    _references(report).append(public_reference)
    original_assessment = deepcopy(assessment)
    apply_template_risk_review(report, assessment)
    cells = next(row for row in _rows(report) if row[0] == DISPLAY_NAMES[code])
    assert clinical_reference["url"] not in {reference["url"] for reference in _cited_references(report, cells)}
    asset = {"sectionOrder": ["disease_risks", "appendix"], "fixedSections": [],
             "overviewInsertions": [], "appendixBlocks": []}
    publish_ordered_references(report, deepcopy(report), asset)
    clinical_block = next(block for section in report["sections"] for block in section["blocks"]
                          if block.get("title") == safety["title"])
    number = int(re.search(r"\[(\d+)\]", clinical_block["text"])[1])
    assert _references(report)[number - 1]["url"] == clinical_reference["url"]
    assert assessment == original_assessment


def test_selected_sources_keep_first_citation_numbering_and_frozen_replay(monkeypatch):
    from modules.gut_barrier import reference_order, template_risk_review

    report = _report()
    apply_template_risk_review(report, {})
    before = deepcopy(report)
    apply_template_risk_review(report, {})
    assert report == before
    asset = {"sectionOrder": ["disease_risks", "appendix"], "fixedSections": [],
             "overviewInsertions": [], "appendixBlocks": []}
    publish_ordered_references(report, before, asset)
    numbers = [int(number) for row in _rows(report) for number in re.findall(r"\[(\d+)\]", row[3])]
    assert list(dict.fromkeys(numbers)) == list(range(1, len(_references(report)) + 1))
    references = _references(report)
    assert len(references) == len({reference["url"] for reference in references})
    serialized = json.dumps(report, ensure_ascii=False)
    assert not any(key in serialized for key in ("providedFile", "sha256", "reviewedPages", "evidenceType", "referenceSelection"))
    frozen = project_callback_report(report)
    original = deepcopy(frozen)

    def unexpected_reselection(*args, **kwargs):
        pytest.fail("frozen replay must not select today's sources or reference assets")

    monkeypatch.setattr(template_risk_review, "apply_template_risk_review", unexpected_reselection)
    monkeypatch.setattr(reference_order, "load_reference_asset", unexpected_reselection)
    assert project_callback_report(frozen) == original == frozen
