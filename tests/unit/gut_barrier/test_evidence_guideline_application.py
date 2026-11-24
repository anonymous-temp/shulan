"""Guideline application studies are not the primary guideline publication."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.gut_barrier.evidence import GutBarrierEvidence, _candidate


QUERY = "adult cardiovascular prevention clinical practice guideline"
TITLE = ("Guideline-Directed Application of¬†Coronary Artery Calcium Scores "
         "for¬†Primary Prevention of Atherosclerotic¬†Cardiovascular Disease.")
# Paraphrased fixture of PMID 40047745; not clinical evidence prose.
ABSTRACT = ("BACKGROUND Existing cardiovascular guidelines use coronary calcium for prevention. "
            "OBJECTIVES This study compared the application of two guidelines. "
            "METHODS A total of 1,903 statin-naive participants aged 55 to 75 years "
            "from the prospective Rotterdam cohort were included. "
            "RESULTS The guidelines differed in eligibility and risk reclassification.")


def screen(title, abstract=None, *, provider="evimed_v2", **fields):
    exclusions = {}
    value = _candidate({"title": title, "abstract": abstract, "pmid": "40047745", **fields},
        provider=provider, channel="literature", query=QUERY, query_id="offline",
        exclusions=exclusions)
    return value, exclusions


@pytest.mark.parametrize("provider", ["evimed_v2", "europe_pmc"])
@pytest.mark.parametrize("abstract", [ABSTRACT, None])
def test_actual_guideline_directed_cohort_is_not_guidance_even_when_tagged_as_guideline(provider, abstract):
    candidate, exclusions = screen(TITLE, abstract, provider=provider,
        study_types=["指南/共识"], _trusted_pubtypes=["Practice Guideline"])
    assert candidate is None
    assert exclusions == {"guideline_application_study": 1}


@pytest.mark.parametrize("title", [
    "Adherence to cardiovascular prevention guidelines: a multicentre cohort study",
    "Adherence to clinical practice guidelines for cardiovascular prevention",
    "Guideline adherence and cardiovascular outcomes: a registry analysis",
    "Implementation of cardiovascular prevention guidelines: a prospective study",
    "Guideline-directed medical therapy and cardiovascular outcomes in a real-world cohort",
    "心血管病预防指南依从性与临床结局的队列研究",
    "心血管预防指南实施效果的横断面研究",
])
def test_application_adherence_and_implementation_studies_do_not_fill_guideline_slots(title):
    candidate, exclusions = screen(title, ABSTRACT)
    assert candidate is None
    assert exclusions == {"guideline_application_study": 1}


@pytest.mark.parametrize("title,abstract", [
    ("Cardiovascular prevention clinical practice guideline",
     "OBJECTIVE To develop clinical recommendations. METHODS The panel used GRADE "
     "and a Delphi consensus after systematic reviews of trials and cohort studies. "
     "RESULTS The panel issued 25 recommendations. CONCLUSIONS These recommendations support prevention."),
    ("Guideline on cardiovascular prevention and medication adherence",
     "METHODS A multidisciplinary panel assessed evidence and implementation considerations. "
     "RESULTS Recommendations address shared decision making and adherence."),
    ("中国基层心血管病一级预防指南（2023年）",
     "采用GRADE评价既有临床研究的证据，形成推荐意见，并讨论指南实施和依从性。"),
    ("Guideline-directed cardiovascular prevention: a clinical practice guideline",
     "A panel produced recommendations using GRADE and considered evidence from prospective cohort studies."),
])
def test_real_guidelines_keep_grade_methods_results_and_implementation_discussion(title, abstract):
    candidate, exclusions = screen(title, abstract)
    assert candidate is not None and candidate["candidateOnly"] is True
    assert exclusions == {}


@pytest.mark.asyncio
async def test_application_study_cannot_prevent_bounded_public_fallback():
    client = SimpleNamespace(enabled=True, search_detailed=AsyncMock(return_value=[SimpleNamespace(
        channel="literature", status="success", records=[{
            "title": TITLE, "abstract": ABSTRACT, "pmid": "40047745", "study_types": ["指南/共识"],
        }],
    )]))
    calls = []

    def public(request):
        calls.append(request.url.params["query"])
        return httpx.Response(200, json={"resultList": {"result": [{
            "source": "MED", "id": "12345", "title": "Cardiovascular primary prevention guideline",
        }]}})

    adapter = GutBarrierEvidence(evimed_client=client, transport=httpx.MockTransport(public))
    try:
        result = await adapter.retrieve([QUERY])
    finally:
        await adapter.aclose()
    assert client.search_detailed.await_count == 2 and len(calls) == 1
    attempts = result["queries"][0]["attempts"]
    assert all(item["channels"][0]["exclusions"] == {"guideline_application_study": 1} for item in attempts[:2])
    assert [candidate["pmid"] for candidate in result["candidates"]] == ["12345"]
    assert result["partial"] is False
