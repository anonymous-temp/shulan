"""Offline regressions for active digestive topics and the query-budget contract."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.gut_barrier.evidence import GutBarrierEvidence, _matches, _query_topics, _search_variants


GASTRITIS_QUERY = "chronic gastritis adults clinical guideline"
GASTRITIS_TITLE = "Guidelines for diagnosis and treatment of chronic gastritis in China"
IBD_QUERY = "inflammatory bowel disease adults diagnostic clinical guideline"
IBD_TITLE = "Inflammatory bowel disease diagnosis clinical guideline"


def candidate(title, **changes):
    return {"title": title, "abstract": None, "summary": None,
            "channel": "literature", "studyTypes": [], **changes}


@pytest.mark.parametrize("query,title,expected", [
    (GASTRITIS_QUERY, GASTRITIS_TITLE, ["gastritis"]),
    (GASTRITIS_QUERY, "中国慢性胃炎诊治指南", ["gastritis"]),
    ("成人慢性胃炎指南", GASTRITIS_TITLE, ["gastritis"]),
    (IBD_QUERY, IBD_TITLE, ["ibd"]),
    (IBD_QUERY, "成人炎症性肠病诊治指南", ["ibd"]),
    ("成人炎症性肠病诊断指南", IBD_TITLE, ["ibd"]),
    ("inflammatory bowel disease adults guideline", "Crohn's disease guidelines", ["ibd"]),
    (IBD_QUERY, "Ulcerative colitis diagnosis and management guidelines", ["ibd"]),
])
def test_named_digestive_disease_is_sufficient_without_all_query_modifiers(query, title, expected):
    assert _matches(query, candidate(title)) == expected


@pytest.mark.parametrize("field", ["abstract", "summary"])
@pytest.mark.parametrize("focus", [
    "This guideline evaluates inflammatory bowel disease in adults.",
    "本共识阐述炎症性肠病的评估流程。",
])
def test_ibd_in_introductory_clinical_focus_is_accepted(field, focus):
    assert _matches(IBD_QUERY, candidate("Digestive clinical practice guidance", **{field: focus})) == ["ibd"]


@pytest.mark.parametrize("title,summary", [
    ("Antibiotic-associated diarrhea: management guidance", "Chronic diarrhea is considered in this guideline."),
    ("慢性腹泻诊治指南", "慢性腹泻需要评估相关病因。"),
    ("Non-inflammatory bowel disease conditions: a clinical review", "Chronic diarrhea and bloating."),
    ("Functional bowel disorders and chronic diarrhea guideline", "Functional symptoms in adults."),
    ("功能性腹泻诊疗共识", "成人腹泻的临床诊断。"),
    ("Dyslipidemia clinical guideline", "Cardiovascular prevention guidance."),
])
def test_symptom_or_unrelated_shared_word_cannot_replace_ibd(title, summary):
    assert _matches(IBD_QUERY, candidate(title, summary=summary)) == []


@pytest.mark.parametrize("query,title,expected", [
    ("inflammatory bowel disease irritable bowel syndrome guideline", IBD_TITLE, ["ibd"]),
    ("inflammatory bowel disease irritable bowel syndrome guideline",
     "ACG Clinical Guideline: Management of Irritable Bowel Syndrome", []),
    ("IBS diarrhea calprotectin inflammatory bowel disease guideline",
     "Fecal calprotectin in chronic diarrhea guidance", []),
])
def test_ibd_takes_precedence_over_broader_ibs_terms(query, title, expected):
    assert _matches(query, candidate(title)) == expected


def test_incidental_late_abstract_mention_is_not_a_core_ibd_match():
    abstract = "This guideline concerns chronic diarrhea. Its scope is antibiotic exposure. This review mentions inflammatory bowel disease incidentally."
    assert _matches(IBD_QUERY, candidate("Antibiotic-associated diarrhea guidance", abstract=abstract)) == []


@pytest.mark.parametrize("query", [
    "functional dyspepsia adults clinical guideline", "成人功能性消化不良指南",
    "celiac disease adults guideline", "成人乳糜泻诊治指南", "coeliac disease adults guideline",
    "adult acne vulgaris guideline", "成人痤疮指南", "成人痤瘡指南",
    "polycystic ovary syndrome diagnosis guideline", "adult PCOS guideline", "成人多囊卵巢综合征指南",
    "diarrhea adults diagnostic clinical guideline", "成人慢性腹泻病因指南",
])
def test_retired_questionnaire_directions_have_no_topic_or_synonym_expansion(query):
    assert _query_topics(query) == []
    assert _search_variants(query) == (None, query)


def outcome(rows, channel):
    return SimpleNamespace(channel=channel, status="success", records=rows)


def record(title, pmid, **changes):
    return {"title": title, "pmid": pmid, "year": 2023, **changes}


@pytest.mark.asyncio
@pytest.mark.parametrize("query,title,pmid", [
    (GASTRITIS_QUERY, GASTRITIS_TITLE, "37245073"),
    (IBD_QUERY, IBD_TITLE, "12345678"),
])
async def test_unsuitable_guide_does_not_block_relevant_literature(query, title, pmid):
    client = SimpleNamespace(enabled=True, search_detailed=AsyncMock(return_value=[
        outcome([record("Chronic diarrhea clinical guideline", "10001")], "guide"),
        outcome([record(title, pmid)], "literature"),
    ]))
    provider = GutBarrierEvidence(evimed_client=client,
        transport=httpx.MockTransport(lambda _: pytest.fail("relevant literature exists")))
    result = await provider.retrieve([query])
    assert result["partial"] is False and result["provider"] == "evimed_v2"
    assert [row["pmid"] for row in result["candidates"]] == [pmid]
    assert result["candidates"][0]["candidateOnly"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("query,title,pmid", [
    (GASTRITIS_QUERY, GASTRITIS_TITLE, "37245073"),
    (IBD_QUERY, IBD_TITLE, "12345678"),
])
async def test_only_nonspecific_primary_results_trigger_official_fallback(query, title, pmid):
    client = SimpleNamespace(enabled=True, search_detailed=AsyncMock(return_value=[
        outcome([record("Functional bowel disorders guideline", "10001")], "guide"),
        outcome([record("Chronic diarrhea guideline", "10002")], "literature"),
    ]))
    requests = []

    def public(request):
        requests.append(request)
        return httpx.Response(200, json={"resultList": {"result": [
            {"source": "MED", "id": pmid, "title": title, "pubYear": "2023"},
        ]}})

    provider = GutBarrierEvidence(evimed_client=client, transport=httpx.MockTransport(public))
    try:
        result = await provider.retrieve([query])
    finally:
        await provider.aclose()
    assert len(requests) == 1 and requests[0].url.host == "www.ebi.ac.uk"
    assert result["provider"] == "europe_pmc" and result["partial"] is False
    assert [row["pmid"] for row in result["candidates"]] == [pmid]
    assert any(row["code"] == "EVIDENCE_PRIMARY_EMPTY_OR_UNSUITABLE" and row["recovered"] for row in result["issues"])


@pytest.mark.asyncio
@pytest.mark.parametrize("query_limit", [4, 6])
async def test_each_query_within_configured_budget_runs_without_expanding_concurrency(query_limit):
    queries_and_titles = [
        ("obesity adults guideline", "Obesity in adults guidelines"),
        ("diabetes adults guideline", "Diabetes management guidelines"),
        ("cardiovascular adults guideline", "Cardiovascular prevention guidelines"),
        ("intestinal barrier adult review", "Intestinal barrier in adults"),
        (GASTRITIS_QUERY, GASTRITIS_TITLE),
        (IBD_QUERY, IBD_TITLE),
    ]
    rows = {query: record(title, str(20000 + index))
            for index, (query, title) in enumerate(queries_and_titles)}
    calls, active, peak = [], 0, 0

    async def search(query, **kwargs):
        nonlocal active, peak
        calls.append(query)
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0)
            return [outcome([rows[query]], "guide")]
        finally:
            active -= 1

    client = SimpleNamespace(enabled=True, search_detailed=search)
    provider = GutBarrierEvidence(evimed_client=client, concurrency=2, max_queries=query_limit,
        transport=httpx.MockTransport(lambda _: pytest.fail("all requested topics have primary candidates")))
    terms = [query for query, _ in queries_and_titles]
    result = await provider.retrieve(terms)
    assert set(calls) == set(terms[:query_limit])
    assert [row["text"] for row in result["queries"]] == terms
    assert len(result["candidates"]) == query_limit and peak <= 2
    assert result["partial"] is (query_limit < len(terms))
    assert bool(result["issues"]) is (query_limit < len(terms))
    if query_limit < len(terms):
        assert result["issues"] == [{"code": "EVIDENCE_QUERY_LIMIT_REACHED"}]
        assert all(row["status"] == "skipped_limit" for row in result["queries"][query_limit:])
