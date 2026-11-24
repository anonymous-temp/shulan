"""Offline topic coverage, source-kind, quota and bounded-expansion regressions."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.gut_barrier.evidence import GutBarrierEvidence, _candidate, _matches, _query_topics


TOPICS = [
    ("adult obesity evaluation clinical practice guideline", "Obesity in adults guideline", "obesity"),
    ("adult metabolic dysfunction associated steatotic liver disease clinical practice guideline", "Steatotic liver disease guidance", "fatty_liver"),
    ("adult type 2 diabetes screening clinical practice guideline", "Type 2 diabetes clinical guideline", "diabetes"),
    ("adult dyslipidemia evaluation clinical practice guideline", "Dyslipidemia guideline", "dyslipidemia"),
    ("adult hypertension screening clinical practice guideline", "Hypertension guideline", "hypertension"),
    ("chronic gastritis adults clinical guideline", "Chronic gastritis guideline", "gastritis"),
    ("Helicobacter pylori dyspepsia adults clinical guideline", "Helicobacter pylori guideline", "h_pylori"),
    ("inflammatory bowel disease adults diagnostic clinical guideline", "Inflammatory bowel disease diagnostic guideline", "ibd"),
    ("adult depression follow up clinical guideline", "Depression follow up guideline", "depression"),
    ("adult obstructive sleep apnea screening clinical guideline", "Obstructive sleep apnea guideline", "sleep_apnea"),
    ("adult cirrhosis follow up clinical guideline", "Cirrhosis follow up guideline", "cirrhosis"),
    ("adult chronic kidney disease evaluation clinical practice guideline", "Chronic kidney disease guideline", "kidney"),
]


def record(title, index=1, **changes):
    return {"title": title, "doi": f"10.1234/offline.{index}", "year": 2025, **changes}


def outcome(rows, channel="guide", status="success"):
    return SimpleNamespace(channel=channel, status=status, records=rows)


def no_public(_):
    pytest.fail("No public request is necessary when this topic has accepted primary candidates")


@pytest.mark.parametrize("query,title,topic", TOPICS)
def test_each_supported_query_has_an_independent_topic(query, title, topic):
    assert _query_topics(query) == [topic]
    candidate = _candidate(record(title), provider="evimed_v2", channel="guide", query=query, query_id="q")
    assert candidate is not None and topic in candidate["matchedTerms"]


@pytest.mark.parametrize("query,title,topic", [row for row in TOPICS if row[2] != "obesity"])
def test_obesity_is_not_coverage_for_another_direction(query, title, topic):
    assert _candidate(record("Obesity in adults guideline"), provider="evimed_v2", channel="guide",
                      query=query, query_id="q") is None


@pytest.mark.parametrize("query,title,topic", [
    ("成人 高血压 指南", "Hypertension guideline", "hypertension"),
    ("成人 血脂异常 共识", "Dyslipidemia guideline", "dyslipidemia"),
    ("成人 抑郁症 随访 指南", "Depression follow up guideline", "depression"),
    ("成人 幽门螺杆菌 消化不良 指南", "Helicobacter pylori guideline", "h_pylori"),
    ("成人 炎症性肠病 诊断 指南", "Inflammatory bowel disease diagnostic guideline", "ibd"),
    ("成人 阻塞性睡眠呼吸暂停 指南", "Obstructive sleep apnoea guideline", "sleep_apnea"),
    ("成人 肝硬化 随访 共识", "Cirrhosis follow up guideline", "cirrhosis"),
    ("成人 慢性肾病 评估 指南", "Chronic kidney disease guideline", "kidney"),
])
def test_server_chinese_synonyms_use_the_same_topic_screen(query, title, topic):
    value = _candidate(record(title), provider="evimed_v2", channel="literature", query=query, query_id="q")
    assert value is not None and topic in value["matchedTerms"]


@pytest.mark.parametrize("title", ["Pneumonia diagnosis guideline", "Tuberculosis guideline", "Cancer immunotherapy guideline"])
def test_crp_incidental_mentions_do_not_qualify_another_disease_guideline(title):
    value = _candidate(record(title, abstract="High sensitivity C reactive protein is used for inflammation monitoring."),
                      provider="evimed_v2", channel="guide", query="high sensitivity C reactive protein inflammation interpretation clinical guideline", query_id="q")
    assert value is None


@pytest.mark.asyncio
async def test_twelve_topics_are_retrieved_with_two_active_searches_and_complete_audit():
    active = peak = 0
    rows = {query: (title, topic, index) for index, (query, title, topic) in enumerate(TOPICS)}

    async def search(query, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            assert kwargs == {"supports": [], "channels": ("guide", "literature")}
            await asyncio.sleep(0)
            title, _, index = rows[query]
            return [outcome([record(title, index * 10 + offset) for offset in range(3)])]
        finally:
            active -= 1

    adapter = GutBarrierEvidence(evimed_client=SimpleNamespace(enabled=True, search_detailed=search),
                                transport=httpx.MockTransport(no_public))
    assert (adapter.max_queries, adapter.max_results, adapter.per_query_results, adapter.timeout_seconds) == (12, 36, 3, 120)
    result = await adapter.retrieve(list(rows))
    assert peak == 2 and active == 0
    assert result["status"] == "success" and len(result["candidates"]) == 36
    assert len(result["queries"]) == 12
    for audit, (_, _, topic) in zip(result["queries"], TOPICS):
        assert audit["status"] == "success" and audit["coveredTopics"] == [topic]
        assert audit["uncoveredTopics"] == []
        assert audit["candidateCount"] == len(audit["candidateIds"]) == 3
        assert set(audit["candidateIds"]) == {row["id"] for row in result["candidates"] if audit["id"] in row["queryIds"]}


@pytest.mark.asyncio
async def test_query_limit_retains_every_unique_safe_theme_as_explicit_skipped_audit():
    client = SimpleNamespace(enabled=True, search_detailed=AsyncMock(return_value=[outcome([record(TOPICS[0][1])])]))
    adapter = GutBarrierEvidence(evimed_client=client, max_queries=1, transport=httpx.MockTransport(no_public))
    result = await adapter.retrieve([row[0] for row in TOPICS] + [TOPICS[0][0].upper()])
    assert len(result["queries"]) == 12 and client.search_detailed.await_count == 1
    assert all(row["status"] == "skipped_limit" and row["candidateCount"] == 0 and row["candidateIds"] == []
               and row["attempts"] == [] for row in result["queries"][1:])
    assert result["partial"] is True


@pytest.mark.asyncio
async def test_global_result_budget_round_robins_topics_and_rewrites_retained_counts():
    titles = {query: (title, i) for i, (query, title, _) in enumerate(TOPICS[:4])}

    async def search(query, **kwargs):
        title, index = titles[query]
        return [outcome([record(title, index * 10 + offset) for offset in range(3)])]

    adapter = GutBarrierEvidence(evimed_client=SimpleNamespace(enabled=True, search_detailed=search),
                                max_results=5, transport=httpx.MockTransport(no_public))
    result = await adapter.retrieve(list(titles))
    assert len(result["candidates"]) == 5
    assert [row["acceptedCandidateCount"] for row in result["queries"]] == [3, 3, 3, 3]
    assert [row["candidateCount"] for row in result["queries"]] == [2, 1, 1, 1]
    assert all(len(row["candidateIds"]) == row["candidateCount"] for row in result["queries"])
    assert any(issue["code"] == "EVIDENCE_RESULT_LIMIT_REACHED" for issue in result["issues"])


@pytest.mark.asyncio
async def test_guide_cannot_fill_all_slots_or_hide_literature_channel_failure():
    query, title, _ = TOPICS[0]
    guide = [record(title, index) for index in range(4)]
    client = SimpleNamespace(enabled=True, search_detailed=AsyncMock(return_value=[
        outcome(guide), outcome([record(title, 100)], "literature")]))
    adapter = GutBarrierEvidence(evimed_client=client, transport=httpx.MockTransport(no_public))
    result = await adapter.retrieve([query])
    assert [row["channel"] for row in result["candidates"]] == ["guide", "literature", "guide"]
    assert [row["channel"] for row in result["queries"][0]["attempts"][0]["channels"]] == ["guide", "literature"]
    client.search_detailed.return_value = [outcome(guide), outcome([], "literature", "failed")]
    failed = await adapter.retrieve([query])
    assert failed["partial"] is True and failed["candidates"]
    assert any(issue["code"] == "EVIDENCE_CHANNEL_UNAVAILABLE" for issue in failed["issues"])


@pytest.mark.parametrize("provider,channel,types", [
    ("evimed_v2", "guide", []), ("evimed_v2", "guide", ["meta", "Practice Guideline"]),
    ("evimed_v2", "literature", ["meta-analysis"]), ("europe_pmc", "literature", ["Practice Guideline"]),
])
def test_channel_or_normalized_study_types_cannot_turn_a_study_into_a_guideline(provider, channel, types):
    exclusions = {}
    value = _candidate(record("Obesity and diet randomized trial", study_types=types), provider=provider,
                      channel=channel, query=TOPICS[0][0], query_id="q", exclusions=exclusions)
    assert value is None and exclusions == {"not_a_guideline": 1}


@pytest.mark.parametrize("title,reason", [
    ("Obesity guideline development: study protocol", "protocol_without_results"),
    ("肥胖指南研究方案", "protocol_without_results"),
    ("Obesity guideline in children", "pediatric_only"),
    ("Obesity guideline in adolescents", "pediatric_only"),
    ("儿童肥胖诊疗指南", "pediatric_only"),
    ("Obesity guideline studies in mice", "animal_only"),
    ("Obesity in adult rats guideline", "animal_only"),
    ("肥胖小鼠研究指南", "animal_only"),
])
def test_protocol_pediatric_only_and_animal_only_records_are_excluded_with_reason(title, reason):
    exclusions = {}
    assert _candidate(record(title, _trusted_pubtypes=["Practice Guideline"]), provider="europe_pmc",
                      channel="literature", query=TOPICS[0][0], query_id="q", exclusions=exclusions) is None
    assert exclusions == {reason: 1}


@pytest.mark.asyncio
async def test_only_original_public_pubtype_can_qualify_a_title_without_guideline_label():
    def public(_):
        return httpx.Response(200, json={"resultList": {"result": [
            {"title": "Obesity in adults", "source": "MED", "id": "101", "pubTypeList": {"pubType": ["Practice Guideline"]}},
            {"title": "Obesity in adults", "source": "MED", "id": "102", "pubTypeList": {"pubType": ["Meta-Analysis"]}},
        ]}})
    adapter = GutBarrierEvidence(transport=httpx.MockTransport(public))
    try:
        result = await adapter.retrieve([TOPICS[0][0]])
    finally:
        await adapter.aclose()
    assert [row["pmid"] for row in result["candidates"]] == ["101"]
    assert result["queries"][0]["attempts"][-1]["exclusions"] == {"not_a_guideline": 1}


@pytest.mark.asyncio
async def test_unhit_topic_gets_one_server_synonym_then_one_canonical_public_query():
    query, title, _ = TOPICS[6]
    client = SimpleNamespace(enabled=True, search_detailed=AsyncMock(return_value=[outcome([]), outcome([], "literature")]))
    calls = []

    def public(request):
        calls.append(request.url.params["query"])
        return httpx.Response(200, json={"resultList": {"result": [{"source": "MED", "id": "123", "title": title}]}})

    adapter = GutBarrierEvidence(evimed_client=client, transport=httpx.MockTransport(public))
    try:
        result = await adapter.retrieve([query])
    finally:
        await adapter.aclose()
    submitted = [call.args[0] for call in client.search_detailed.await_args_list]
    assert submitted == [query, "成人 幽门螺杆菌 感染 治疗 指南 共识"]
    assert calls == ["Helicobacter pylori adults clinical guideline"]
    assert [row["stage"] for row in result["queries"][0]["attempts"]] == ["primary", "expanded", "public"]
    assert [row["status"] for row in result["queries"][0]["attempts"]] == ["nohit", "nohit", "success"]
    assert result["status"] == "success" and all(issue["recovered"] for issue in result["issues"])


@pytest.mark.asyncio
async def test_expansion_success_does_not_search_public_or_hide_its_channel_failure():
    query, title, _ = TOPICS[0]
    client = SimpleNamespace(enabled=True, search_detailed=AsyncMock(side_effect=[
        [outcome([])], [outcome([record(title)]), outcome([], "literature", "failed")]]))
    adapter = GutBarrierEvidence(evimed_client=client, transport=httpx.MockTransport(no_public))
    result = await adapter.retrieve([query])
    assert result["partial"] is True and client.search_detailed.await_count == 2
    assert result["issues"][0]["recovered"] is True
    assert "recovered" not in result["issues"][-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_nohit_and_provider_failure_are_distinct_with_a_bounded_attempt_count(failure):
    async def search(*args, **kwargs):
        if failure:
            raise ConnectionError("NEVER-REFLECT-SECRET")
        return [outcome([]), outcome([], "literature")]

    client = SimpleNamespace(enabled=True, search_detailed=AsyncMock(side_effect=search))
    adapter = GutBarrierEvidence(evimed_client=client,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"resultList": {"result": []}})))
    try:
        result = await adapter.retrieve([TOPICS[0][0]])
    finally:
        await adapter.aclose()
    audit = result["queries"][0]
    assert audit["status"] == ("failure" if failure else "nohit")
    assert client.search_detailed.await_count == 2 and len(audit["attempts"]) == 3
    assert audit["attempts"][0]["status"] == ("failure" if failure else "nohit")
    assert "NEVER-REFLECT-SECRET" not in json.dumps(result)


@pytest.mark.asyncio
async def test_legacy_mixed_query_does_not_claim_all_metabolic_topics_from_one_hit():
    query = "adult obesity metabolic fatty liver diabetes clinical practice guideline"
    client = SimpleNamespace(enabled=True, search_detailed=AsyncMock(return_value=[outcome([record("Obesity guideline")])]))
    adapter = GutBarrierEvidence(evimed_client=client, transport=httpx.MockTransport(no_public))
    result = await adapter.retrieve([query])
    audit = result["queries"][0]
    assert audit["status"] == "partial_coverage" and result["partial"] is True
    assert audit["coveredTopics"] == ["obesity"] and set(audit["uncoveredTopics"]) == {"diabetes", "fatty_liver"}


@pytest.mark.asyncio
async def test_deadline_audits_capacity_wait_separately_and_joins_active_requests():
    entered, stopped = 0, 0

    async def search(*args, **kwargs):
        nonlocal entered, stopped
        entered += 1
        try:
            await asyncio.Event().wait()
        finally:
            stopped += 1

    adapter = GutBarrierEvidence(evimed_client=SimpleNamespace(enabled=True, search_detailed=search),
        timeout_seconds=0.02, request_timeout_seconds=1, transport=httpx.MockTransport(no_public))
    result = await adapter.retrieve([row[0] for row in TOPICS])
    assert entered == stopped == 2
    assert len(result["queries"]) == 12 and all(row["status"] == "timeout" for row in result["queries"])
    assert [row["phase"] for row in result["issues"]].count("capacity_wait") == 10
    assert [row["phase"] for row in result["issues"]].count("provider_request") == 2


@pytest.mark.parametrize("query", [
    "irritable bowel syndrome adults diagnostic clinical guideline",
    "irritable bowel syndrome screening guideline",
    "irritable bowel syndrome evaluation guideline",
    "irritable bowel syndrome diarrhea fecal calprotectin clinical guideline",
    "成人 肠易激综合征 诊断 评估 指南",
])
@pytest.mark.parametrize("title", [
    "AGA guideline on fecal microbiota-based therapies for gastrointestinal disease",
    "FMT guideline for irritable bowel syndrome",
    "Guideline on evaluation of FMT for irritable bowel syndrome",
    "Donor screening guideline for FMT in irritable bowel syndrome",
    "肠易激综合征粪菌移植治疗指南",
    "肠易激综合征菌群移植诊疗指南",
    "Pharmacological management of irritable bowel syndrome guideline",
])
def test_intervention_or_donor_guideline_does_not_answer_disease_diagnosis(query, title):
    exclusions = {}
    row = record(title, summary="These recommendations discuss irritable bowel syndrome. Screening and diagnosis are mentioned for treatment eligibility and donor selection.")
    assert _matches(query, row) == []
    assert _candidate(row, provider="evimed_v2", channel="guide", query=query, query_id="q",
                      exclusions=exclusions) is None
    assert exclusions == {"clinical_question_mismatch": 1}


@pytest.mark.parametrize("title,summary", [
    ("ACG Clinical Guideline: Management of Irritable Bowel Syndrome", "This guideline addresses a positive diagnostic strategy for irritable bowel syndrome."),
    ("Irritable bowel syndrome diagnosis and treatment guideline", ""),
    ("British Society guideline for irritable bowel syndrome diagnosis", ""),
    ("成人肠易激综合征诊断与治疗指南", ""),
    ("中国肠易激综合征诊治专家共识", ""),
    ("Chronic diarrhea diagnostic guideline", "Fecal calprotectin testing supports differentiation of chronic diarrhea."),
])
def test_actual_diagnostic_and_broad_disease_guidelines_remain_candidates(title, summary):
    value = _candidate(record(title, summary=summary), provider="evimed_v2", channel="guide",
                       query="irritable bowel syndrome adults diagnostic clinical guideline", query_id="q")
    assert value is not None and value["candidateOnly"] is True


def test_explicit_treatment_query_can_retrieve_a_treatment_guideline_without_diagnostic_claim():
    title = "AGA guideline on fecal microbiota-based therapies for gastrointestinal disease"
    value = _candidate(record(title, summary="This guideline discusses fecal microbiota transplantation in irritable bowel syndrome."),
                       provider="evimed_v2", channel="guide",
                       query="irritable bowel syndrome fecal microbiota therapy guideline", query_id="q")
    assert value is not None and value["candidateOnly"] is True


@pytest.mark.parametrize("query", [
    "inflammatory bowel disease adults diagnostic clinical guideline",
    "成人 炎症性肠病 诊断 鉴别 指南 共识",
    "IBD adults diagnostic guideline",
])
@pytest.mark.parametrize("title", [
    "Inflammatory bowel diseases diagnostic guideline",
    "成人炎症性肠病诊断指南",
    "Crohn's Disease in Adults diagnosis guideline",
    "Ulcerative colitis diagnosis guideline",
])
def test_ibd_topic_has_a_strong_disease_anchor_and_server_chinese_synonym(query, title):
    assert _query_topics(query) == ["ibd"]
    value = _candidate(record(title), provider="evimed_v2", channel="literature", query=query, query_id="q")
    assert value is not None and value["matchedTerms"] == ["ibd"]


@pytest.mark.parametrize("title,summary", [
    ("Irritable bowel syndrome diagnostic guideline", "This guideline addresses diarrhea and abdominal pain."),
    ("Chronic diarrhea diagnostic guideline", "Fecal calprotectin testing helps clinical evaluation."),
    ("肠易激综合征诊疗指南", "腹痛、腹泻需要鉴别。"),
    ("慢性腹泻病因诊断指南", "反复腹泻需要进一步检查。"),
    ("Non-inflammatory bowel disease clinical guideline", "Diarrhea evaluation."),
])
def test_ibd_cannot_be_covered_by_ibs_or_diarrhea_alone(title, summary):
    value = _candidate(record(title, summary=summary), provider="evimed_v2", channel="guide",
                       query="inflammatory bowel disease adults diagnostic clinical guideline", query_id="q")
    assert value is None


@pytest.mark.asyncio
async def test_question_mismatch_is_audited_then_diagnostic_literature_is_retained():
    fmt = record("AGA guideline on fecal microbiota-based therapies for gastrointestinal disease", 1,
                 summary="This guideline discusses therapy in irritable bowel syndrome.")
    diagnosis = record("Irritable bowel syndrome diagnosis guideline", 2)
    client = SimpleNamespace(enabled=True, search_detailed=AsyncMock(return_value=[
        outcome([fmt]), outcome([diagnosis], "literature")]))
    adapter = GutBarrierEvidence(evimed_client=client, transport=httpx.MockTransport(no_public))
    result = await adapter.retrieve(["irritable bowel syndrome adults diagnostic clinical guideline"])
    assert result["status"] == "success" and [row["doi"] for row in result["candidates"]] == [diagnosis["doi"]]
    channels = result["queries"][0]["attempts"][0]["channels"]
    assert channels[0]["exclusions"] == {"clinical_question_mismatch": 1}
    assert channels[0]["status"] == "nohit" and channels[1]["status"] == "success"


@pytest.mark.asyncio
async def test_ibd_miss_expands_only_to_ibd_and_cannot_use_ibs_results():
    query = "inflammatory bowel disease adults diagnostic clinical guideline"
    client = SimpleNamespace(enabled=True, search_detailed=AsyncMock(return_value=[
        outcome([record("Irritable bowel syndrome diagnostic guideline")])]))
    calls = []

    def public(request):
        calls.append(request.url.params["query"])
        return httpx.Response(200, json={"resultList": {"result": [{"source": "MED", "id": "321",
            "title": "Inflammatory bowel disease diagnostic guideline"}]}})

    adapter = GutBarrierEvidence(evimed_client=client, transport=httpx.MockTransport(public))
    try:
        result = await adapter.retrieve([query])
    finally:
        await adapter.aclose()
    assert [call.args[0] for call in client.search_detailed.await_args_list] == [query, "成人 炎症性肠病 诊断 鉴别 指南 共识"]
    assert calls == [query]
    assert result["queries"][0]["coveredTopics"] == ["ibd"]
    assert [row["pmid"] for row in result["candidates"]] == ["321"]


@pytest.mark.parametrize("query,title,summary,reason", [
    (TOPICS[9][0], "The 2025 American Academy of Sleep Medicine Central Sleep Apnoea Guidelines and the Heart Failure Patient.", "This review summarises the central sleep apnea guideline.", "disease_subtype_mismatch"),
    (TOPICS[9][0], "Clinical guideline highlights for the hospitalist: Evaluation and management of obstructive sleep apnea in adults hospitalized for medical care.", "", "secondary_guideline_publication"),
    ("Helicobacter pylori dyspepsia adults diagnostic clinical guideline", "HELICOBACTER PYLORI DETECTION AMONG PATIENTS WITH DYSPEPSIA UNDERGOING ESOPHAGOGASTRODUODENOSCOPY ON PROTON PUMP INHIBITORS -UNPACKING THE GAP BETWEEN GUIDELINES AND CLINICAL PRACTICE.", "Methods: We prospectively enrolled 165 cases.", "secondary_guideline_publication"),
    (TOPICS[3][0], "Implications of the 2026 Versus 2018 ACC/AHA Multisociety Dyslipidemia Guidelines on the Clinical Care of U.S. Adults.", "NHANES population impact analysis, not the guideline itself.", "secondary_guideline_publication"),
    (TOPICS[1][0], "《2025年代谢功能障碍相关脂肪性肝病和脂肪性肝炎全球共识建议》摘译", "本文摘译共识。", "secondary_guideline_publication"),
    (TOPICS[1][0], "脂肪性肝病临床指南摘要", "摘要介绍指南内容。", "secondary_guideline_publication"),
    (TOPICS[9][0], "中国成人失眠共病阻塞性睡眠呼吸暂停诊治指南(2024版)", "COMISA clinical management in patients with both conditions.", "specialized_scope_not_initial_assessment"),
    ("Helicobacter pylori dyspepsia adults diagnostic clinical guideline", "2022中国幽门螺杆菌感染治疗指南", "本指南主要介绍感染后根除方案。", "clinical_question_mismatch"),
    ("Helicobacter pylori dyspepsia adults diagnostic clinical guideline", "2024 AGA指南：治疗成人幽门螺杆菌感染的有效方法", "治疗方案摘要。", "clinical_question_mismatch"),
    (TOPICS[7][0], "2018年英国胃肠病学会《成人慢性腹泻的调查指南》解读", "本文结合指南更新内容探讨慢性腹泻。", "secondary_guideline_publication"),
    (TOPICS[0][0], "《成人肥胖食养指南(2024年版)》解读", "笔者对该指南进行解读。", "secondary_guideline_publication"),
    (TOPICS[1][0], "2025年代谢功能障碍相关的脂肪性肝病及脂肪性肝炎全球共识意见解读", "介绍脂肪肝全球共识。", "secondary_guideline_publication"),
    (TOPICS[3][0], "Management of dyslipidemia—Comments of the German Cardiac Society on the guidelines of the ESC/EAS (2025): From the Committee for Clinical Cardiovascular Medicine", "The guideline update discusses cardiovascular risk classification.", "secondary_guideline_publication"),
    (TOPICS[3][0], "Evolving paradigms in the management of dyslipidemia: comparison between the 2019 ESC/EAS guidelines and the 2025 focus update.", "This comparison discusses guideline changes.", "secondary_guideline_publication"),
    (TOPICS[10][0], "《肝硬化诊治专家共识》解读", "文章进行详细解读。", "secondary_guideline_publication"),
    (TOPICS[6][0], "2024 中国指南：中国早期胃癌预防、诊断和治疗", "This guideline provides comprehensive recommendations for early gastric cancer. It includes Helicobacter pylori eradication strategies.", "disease_not_title_subject"),
    (TOPICS[2][0], "青少年起病的成人型糖尿病筛查与诊治专家共识", "本共识聚焦MODY筛查与诊治，临床上常被误诊为2型糖尿病。", "disease_subtype_mismatch"),
    (TOPICS[3][0], "血脂异常基层合理用药指南", "该指南介绍血脂异常定义、分类以及他汀类用药剂量。", "clinical_question_mismatch"),
    (TOPICS[9][0], "2025 ERS专家共识：阻塞性睡眠呼吸暂停的先进远程医疗", "Advanced telemedicine for diagnosis, treatment and follow-up of obstructive sleep apnoea.", "specialized_scope_not_initial_assessment"),
    (TOPICS[9][0], "2025 ATS指南：将成人阻塞性睡眠呼吸暂停的内表型转化为临床的研究重点", "This research statement outlines endophenotyping and research priorities.", "specialized_scope_not_initial_assessment"),
    (TOPICS[0][0], "成人肥胖食养指南（2024 年版）", "指南包含肥胖定义、食养原则以及饮食管理。", "nutrition_scope_not_disease_assessment"),
])
def test_reviewed_live_false_positives_have_precise_metadata_exclusion(query, title, summary, reason):
    exclusions = {}
    row = record(title, summary=summary, study_types=["指南/共识", "Practice Guideline"])
    assert _matches(query, row) == []
    assert _candidate(row, provider="evimed_v2", channel="guide", query=query, query_id="q",
                      exclusions=exclusions) is None
    assert exclusions == {reason: 1}


@pytest.mark.parametrize("query,title", [
    (TOPICS[5][0], "中国慢性胃炎诊治专家共识"),
    (TOPICS[0][0], "2025 NICE指南：超重和肥胖的管理（NG.246）"),
    (TOPICS[1][0], "2024 EASL–EASD–EASO临床实践指南：代谢功能障碍相关脂肪肝病 (MASLD)的管理"),
    (TOPICS[2][0], "2026 NICE指南：成人2型糖尿病的管理"),
    (TOPICS[4][0], "中国高血压临床实践指南"),
    (TOPICS[4][0], "2024 ESC指南：血压升高及高血压管理"),
    (TOPICS[4][0], "成人高血压合并2型糖尿病和血脂异常基层防治中国专家共识（2024年版）"),
    (TOPICS[3][0], "2025 focused update of ESC/EAS guidelines on management of dyslipidemia"),
    (TOPICS[6][0], "Helicobacter pylori infection diagnosis and management guideline"),
    (TOPICS[9][0], "Obstructive sleep apnea initial screening and diagnosis guideline"),
    (TOPICS[10][0], "Cirrhosis follow up and management guideline"),
    (TOPICS[10][0], "肝硬化随访管理指南"),
    (TOPICS[0][0], "Diagnosis of obesity and nutritional management guideline"),
    (TOPICS[-1][0], "慢性肾病诊断与评估指南"),
    (TOPICS[-1][0], "Chronic kidney disease guideline for clinical assessment"),
])
def test_scope_screen_retains_original_comprehensive_or_direct_diagnostic_guidelines(query, title):
    value = _candidate(record(title), provider="evimed_v2", channel="guide", query=query, query_id="q")
    assert value is not None and value["candidateOnly"] is True


@pytest.mark.parametrize("title", [
    "MODY diabetes screening consensus", "Maturity-onset diabetes of the young clinical guideline",
    "Monogenic diabetes guideline", "Type 1 diabetes management guideline", "Gestational diabetes guideline",
])
def test_other_diabetes_subtypes_cannot_supply_ordinary_type2_screening_evidence(title):
    exclusions = {}
    assert _candidate(record(title, summary="Differential diagnosis may include type 2 diabetes."), provider="evimed_v2",
                      channel="guide", query=TOPICS[2][0], query_id="q", exclusions=exclusions) is None
    assert exclusions == {"disease_subtype_mismatch": 1}


@pytest.mark.parametrize("title", [
    "Commentary on obesity clinical guidelines", "Review of obesity clinical guidelines",
    "Comparison of clinical guidelines for obesity management", "解读《中国肥胖指南》",
])
def test_secondary_publication_cannot_be_rescued_by_provider_guideline_tags(title):
    exclusions = {}
    assert _candidate(record(title, _trusted_pubtypes=["Practice Guideline"]), provider="europe_pmc",
                      channel="literature", query=TOPICS[0][0], query_id="q", exclusions=exclusions) is None
    assert exclusions == {"secondary_guideline_publication": 1}


@pytest.mark.asyncio
async def test_reviewed_scope_mismatch_is_audited_and_cannot_suppress_fallback():
    query = TOPICS[6][0]
    bad = record("2024 中国指南：中国早期胃癌预防、诊断和治疗", summary="Helicobacter pylori eradication is discussed.")
    client = SimpleNamespace(enabled=True, search_detailed=AsyncMock(return_value=[outcome([bad])]))
    calls = []

    def public(request):
        calls.append(request.url.params["query"])
        return httpx.Response(200, json={"resultList": {"result": [{"source": "MED", "id": "456",
            "title": "Helicobacter pylori infection clinical guideline"}]}})

    adapter = GutBarrierEvidence(evimed_client=client, transport=httpx.MockTransport(public))
    try:
        result = await adapter.retrieve([query])
    finally:
        await adapter.aclose()
    assert client.search_detailed.await_count == 2 and len(calls) == 1
    attempts = result["queries"][0]["attempts"]
    assert all(row["channels"][0]["exclusions"] == {"disease_not_title_subject": 1} for row in attempts[:2])
    assert [row["pmid"] for row in result["candidates"]] == ["456"]
    assert result["queries"][0]["candidateCount"] == 1


@pytest.mark.parametrize("title", [
    "C-reactive protein and cardiovascular prevention guideline", "超敏C反应蛋白检验结果解读指南",
    "hs-CRP laboratory interpretation guideline", "HS_CRP and cardiovascular prevention guideline",
    "高敏C－反應蛋白與心血管疾病指南", "high sensitivity C reactive protein cardiovascular guideline",
])
def test_retired_biomarker_sources_do_not_reappear_as_cardiovascular_evidence(title):
    exclusions = {}
    assert _candidate(record(title), provider="evimed_v2", channel="guide",
                      query="adult cardiovascular prevention clinical practice guideline", query_id="q",
                      exclusions=exclusions) is None
    assert exclusions == {"excluded_biomarker_subject": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    "high sensitivity C reactive protein interpretation guideline", "超敏C反应蛋白 检测 解读 指南",
    "HS_CRP cardiovascular prevention guideline", "高敏C－反應蛋白 心血管疾病 指南",
])
async def test_retired_biomarker_query_never_calls_a_search_provider(query):
    client = SimpleNamespace(enabled=True, search_detailed=AsyncMock())
    adapter = GutBarrierEvidence(evimed_client=client, transport=httpx.MockTransport(no_public))
    result = await adapter.retrieve([query])
    client.search_detailed.assert_not_called()
    assert result["candidates"] == []
