"""Offline regressions for the 2026-09-28 manual retrieval audit.

The Spanish consensus title is the actual audited retrieval. Chinese lipid
guideline titles/identifiers were checked against PubMed 36925135/37840633;
short summaries below are paraphrased screening fixtures, not quoted evidence.
No fixture promotes a retrieval candidate into a patient recommendation.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.gut_barrier.evidence import GutBarrierEvidence, _candidate, _query_topics


CARDIO = "adult cardiovascular prevention clinical practice guideline"
LIPIDS = "adult dyslipidemia evaluation clinical practice guideline"
SPANISH_TITLE = (
    "Obesity and cardiovascular and renal disease. Consensus document of the "
    "Preventive Cardiology, Clinical Cardiology, and Heart Failure Associations "
    "of the Spanish Society of Cardiology"
)


def record(title, **changes):
    return {"title": title, "doi": "10.1234/offline.population", "year": 2024, **changes}


def screen(title, *, query=CARDIO, **changes):
    exclusions = {}
    candidate = _candidate(record(title, **changes), provider="evimed_v2", channel="literature",
                           query=query, query_id="offline-scope", exclusions=exclusions)
    return candidate, exclusions


@pytest.mark.parametrize("abstract", [None,
    "Obesity raises cardiovascular and renal concerns. This consensus provides guidance "
    "for prevention and treatment of complications related to obesity.",
])
def test_actual_obesity_cardiorenal_consensus_cannot_cover_general_cardiovascular_query(abstract):
    candidate, exclusions = screen(SPANISH_TITLE, abstract=abstract)
    assert candidate is None
    assert exclusions == {"population_scope_mismatch": 1}


@pytest.mark.parametrize("title", [
    "Clinical guideline on cardiovascular prevention in adults with diabetes",
    "Cardiovascular prevention in patients with chronic kidney disease: consensus",
    "肥胖相关心血管并发症管理专家共识",
    "糖尿病患者心血管疾病预防指南",
])
def test_explicit_restricted_population_is_not_general_coverage(title):
    candidate, exclusions = screen(title)
    assert candidate is None
    assert exclusions == {"population_scope_mismatch": 1}


def test_generic_title_does_not_hide_explicit_restricted_scope_in_abstract():
    candidate, exclusions = screen("Cardiovascular prevention clinical guideline", abstract=(
        "Cardiovascular disease prevention is important. Obesity is a frequent risk factor. "
        "This guideline applies only to adults with obesity."
    ))
    assert candidate is None
    assert exclusions == {"population_scope_mismatch": 1}


@pytest.mark.parametrize("summary", [
    "This guideline focuses on cardiovascular prevention among obese adults.",
    "本共识仅针对肥胖患者的心血管并发症预防与管理。",
])
def test_explicit_intended_population_in_summary_is_audited(summary):
    candidate, exclusions = screen("Cardiovascular prevention consensus", summary=summary)
    assert candidate is None
    assert exclusions == {"population_scope_mismatch": 1}


@pytest.mark.parametrize("abstract", [None, "Further details are unavailable."])
def test_ambiguous_population_title_without_scope_is_audited_not_assumed_general(abstract):
    candidate, exclusions = screen("Cardiovascular prevention and obesity: clinical consensus", abstract=abstract)
    assert candidate is None
    assert exclusions == {"population_scope_unconfirmed": 1}


@pytest.mark.parametrize("title,abstract", [
    ("2019 ACC/AHA Guideline on the Primary Prevention of Cardiovascular Disease",
     "The guideline addresses prevention in the general adult population. It discusses obesity, "
     "diabetes, hypertension, diet and smoking among risk factors."),
    ("Cardiovascular prevention guideline",
     "Obesity is one cardiovascular risk factor. The guideline includes blood pressure and lipid management."),
    ("Cardiovascular prevention clinical guideline",
     "This guideline covers cardiovascular prevention in adults, including those with obesity. "
     "Multiple risk factors are discussed without restricting the guideline to a single condition."),
    ("中国心血管病一级预防指南", "危险因素包括肥胖、糖尿病、吸烟和高血压，指南覆盖一般成人预防。"),
    ("Cardiovascular prevention and obesity: clinical consensus",
     "This consensus provides comprehensive cardiovascular prevention guidance for the general adult population."),
])
def test_general_guidance_is_not_rejected_for_discussing_obesity(title, abstract):
    candidate, exclusions = screen(title, abstract=abstract)
    assert candidate is not None and candidate["candidateOnly"] is True
    assert exclusions == {}
    assert candidate["matchedTerms"] == ["cardiovascular"]


def test_explicit_query_population_can_retrieve_matching_specialized_candidate():
    candidate, exclusions = screen(SPANISH_TITLE,
        query="obesity cardiovascular prevention adult clinical guideline")
    assert candidate is not None and candidate["candidateOnly"] is True
    assert exclusions == {}


@pytest.mark.parametrize("title,pmid", [
    ("中国血脂管理指南（2023年）", "36925135"),
    ("[Chinese guidelines for lipid management (2023)]", "36925135"),
    ("2023 China Guidelines for Lipid Management", "37840633"),
])
@pytest.mark.parametrize("abstract", [None,
    "Atherosclerotic cardiovascular disease creates a major burden. Lipid management is important for prevention.",
])
def test_official_chinese_lipid_management_titles_are_not_lost_without_old_dyslipidemia_term(title, pmid, abstract):
    candidate, exclusions = screen(title, query=LIPIDS, pmid=pmid, abstract=abstract)
    assert candidate is not None and candidate["matchedTerms"] == ["dyslipidemia"]
    assert candidate["candidateOnly"] is True
    assert exclusions == {}


@pytest.mark.parametrize("query", ["adult lipid management clinical guideline", "成人 血脂管理 指南"])
def test_lipid_management_query_uses_existing_dyslipidemia_topic(query):
    assert _query_topics(query) == ["dyslipidemia"]


@pytest.mark.parametrize("title", [
    "中国血脂管理指南（2023年）解读",
    "Commentary on Chinese guidelines for lipid management",
    "Lipid nanoparticle manufacturing guidelines",
    "Consensus on lipid metabolism in mouse models",
])
def test_lipid_synonyms_do_not_accept_secondary_or_nonclinical_lipid_documents(title):
    candidate, exclusions = screen(title, query=LIPIDS)
    assert candidate is None and exclusions


def outcome(rows):
    return SimpleNamespace(channel="literature", status="success", records=rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["expanded", "public", "none"])
async def test_population_mismatch_continues_existing_bounded_retrieval(recovery):
    good = record("Guideline on primary prevention of cardiovascular disease", doi="10.1234/general")
    bad = record(SPANISH_TITLE)
    client = SimpleNamespace(enabled=True, search_detailed=AsyncMock(side_effect=[
        [outcome([bad])], [outcome([good] if recovery == "expanded" else [bad])],
    ]))
    public_calls = []

    def public(request):
        public_calls.append(request)
        assert request.url.host == "www.ebi.ac.uk"
        assert request.url.params["query"] == "cardiovascular prevention adults clinical guideline"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"resultList": {"result": [] if recovery == "none" else [{
            "source": "MED", "id": "12345", "title": good["title"], "pubYear": "2024",
        }]}})

    adapter = GutBarrierEvidence(evimed_client=client, transport=httpx.MockTransport(public))
    try:
        result = await adapter.retrieve([CARDIO])
    finally:
        await adapter.aclose()
    assert client.search_detailed.await_count == 2
    assert len(public_calls) == int(recovery != "expanded")
    attempts = result["queries"][0]["attempts"]
    assert attempts[0]["channels"][0]["exclusions"] == {"population_scope_mismatch": 1}
    assert all(row["candidateOnly"] for row in result["candidates"])
    assert not any(row["title"] == SPANISH_TITLE for row in result["candidates"])
    if recovery == "none":
        assert result["queries"][0]["status"] == "nohit"
        assert result["queries"][0]["uncoveredTopics"] == ["cardiovascular"]
        assert result["partial"] is True
    else:
        assert result["queries"][0]["status"] == "success"
        assert result["queries"][0]["coveredTopics"] == ["cardiovascular"]
        assert result["partial"] is False
        assert all(issue.get("recovered") for issue in result["issues"])


@pytest.mark.asyncio
async def test_valid_lipid_guideline_does_not_trigger_unnecessary_expansion_or_public_search():
    client = SimpleNamespace(enabled=True, search_detailed=AsyncMock(return_value=[outcome([
        record("中国血脂管理指南（2023年）", pmid="36925135"),
    ])]))
    adapter = GutBarrierEvidence(evimed_client=client,
        transport=httpx.MockTransport(lambda _: pytest.fail("primary guideline covers the topic")))
    try:
        result = await adapter.retrieve([LIPIDS])
    finally:
        await adapter.aclose()
    assert client.search_detailed.await_count == 1
    assert result["queries"][0]["coveredTopics"] == ["dyslipidemia"]
    assert result["partial"] is False


@pytest.mark.asyncio
async def test_public_metadata_cannot_bypass_original_query_population_screen():
    def public(_):
        return httpx.Response(200, json={"resultList": {"result": [{
            "source": "MED", "id": "12345", "title": SPANISH_TITLE,
            "pubTypeList": {"pubType": ["Practice Guideline"]},
        }]}})

    adapter = GutBarrierEvidence(transport=httpx.MockTransport(public))
    try:
        result = await adapter.retrieve([CARDIO])
    finally:
        await adapter.aclose()
    assert result["candidates"] == []
    assert result["queries"][0]["attempts"][-1]["exclusions"] == {"population_scope_mismatch": 1}
    assert result["queries"][0]["status"] == "nohit"
    assert result["queries"][0]["uncoveredTopics"] == ["cardiovascular"]
