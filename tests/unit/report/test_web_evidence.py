import asyncio
from collections import Counter
from types import SimpleNamespace

import httpx
import pytest

from adapters.report import web_evidence
from adapters.report.deepseek_tools import DeepSeekToolsClient
from adapters.report.web_evidence import WEB_PROVIDER
from adapters.report.web_evidence import WebEvidenceClient
from adapters.report.web_evidence import web_original_key
from modules.report.evaluation_originals import evaluation_original_text
from modules.report.web_source_policy import classify_web_source
from modules.report.web_source_policy import clean_page_title
from modules.report.web_source_policy import evidence_grade
from modules.report.web_source_policy import source_display_name

BODY = (
    'Adults with a positive stool-based screening test should undergo follow-up colonoscopy. '
    'Delays beyond nine months are associated with a higher risk of colorectal cancer. '
) * 8


def test_only_allow_listed_sources_are_admitted():
    assert classify_web_source('https://mp.weixin.qq.com/s?x=1', '肠镜全攻略') is None
    assert classify_web_source('https://www.nhs.uk/conditions/cervical-spondylosis/', 'Cervical spondylosis') is None
    assert classify_web_source('https://medlineplus.gov/ency/patientinstructions/000120.htm', 'Constipation') is None
    assert classify_web_source('https://ouci.dntb.gov.ua/en/?backlinks_to=10.1038/x', 'OUCI') is None
    assert classify_web_source('https://www.sciencedirect.com/topics/medicine/eggerthella', 'Eggerthella') is None
    assert classify_web_source('https://www.ncbi.xyz/pmc/articles/PMC10201371/', 'mirror') is None

    guideline = classify_web_source('https://www.nice.org.uk/guidance/ng222', 'Depression in adults')
    assert (guideline.tier, guideline.category) == ('guideline_body', 'guideline')
    # An authority's news, Q&A and lay explainers are not guidelines.
    assert (
        classify_web_source('https://www.who.int/news-room/questions-and-answers/item/x', 'Refractive errors') is None
    )
    assert classify_web_source('https://www.cma.org.cn/art/2024/4/10/art_1.html', '体检报告看不懂？读懂肝功能') is None
    assert (
        classify_web_source('https://www.nhc.gov.cn/yzygj/x.shtml', '肥胖症诊疗指南（2024年版）').category
        == 'guideline'
    )
    assert (
        classify_web_source('https://pmc.ncbi.nlm.nih.gov/articles/PMC1/', 'Giant hepatic cyst: a case report') is None
    )
    article = classify_web_source(
        'https://rd.springer.com/article/10.1038/s41467-024-51651-9#3',
        'Gut Microbiome Wellness Index 2 enhances health status prediction',
    )
    assert (article.tier, article.category, article.doi) == (
        'peer_reviewed',
        'literature',
        '10.1038/s41467-024-51651-9',
    )
    assert article.canonical_url.endswith('s41467-024-51651-9')
    mirror = classify_web_source('http://staging.europepmc.org/backend/articlerender.fcgi?accid=PMC6463060#2', 'x')
    assert mirror.pmcid == 'PMC6463060' and mirror.canonical_url == 'https://pmc.ncbi.nlm.nih.gov/articles/PMC6463060/'
    reference = classify_web_source('https://www.msdmanuals.cn/professional/oncology/x', '结直肠癌筛查')
    assert reference.tier == 'professional_reference' and reference.category == 'literature'


def test_evidence_grade_describes_the_source_type():
    assert evidence_grade('guideline', []) == ('A', '指南/共识')
    assert evidence_grade('literature', ['Meta-Analysis'])[0] == 'A'
    assert evidence_grade('literature', ['Randomized Controlled Trial'])[0] == 'B'
    assert evidence_grade('literature', ['Review']) == ('C', '综述')
    assert evidence_grade('literature', []) == ('C', '研究论文')
    assert evidence_grade('literature', ['Animal or In Vitro Study']) == ('D', '动物或体外研究')
    assert evidence_grade('literature', ['Journal Article', 'Humans', 'Animals']) == ('C', '研究论文')


def test_a_library_guideline_label_needs_a_guideline_title_and_animal_work_grades_d():
    # Library guideline channels also return reviews; the title decides.
    assert evidence_grade('guideline', [], 'Management of gallbladder polyps: updated joint guidelines')[0] == 'A'
    assert evidence_grade('guideline', [], '不明原因消化道出血诊治推荐流程') == ('A', '指南/共识')
    assert evidence_grade('guideline', [], 'Toward precision colorectal cancer screening') == ('C', '研究论文')
    assert evidence_grade('guideline', [], 'Probiotics for anxiety: a systematic review and meta-analysis') == (
        'A',
        '系统综述/Meta分析',
    )
    assert evidence_grade(
        'guideline', [], 'Effects of L. rhamnosus on anxiety: a systematic review of evidence from animal studies'
    ) == ('D', '动物或体外研究')
    assert evidence_grade('literature', [], 'Butyrate protects mice from colitis') == ('D', '动物或体外研究')


def test_studies_about_a_guideline_and_body_sentences_are_not_labelled_as_such():
    assert evidence_grade('guideline', [], 'Impact of the 2025 ASE Guidelines on diastolic dysfunction') == (
        'C',
        '研究论文',
    )
    assert classify_web_source(
        'https://www.ahajournals.org/doi/10.1161/x', 'Adherence to guidelines in 2024'
    ).category == ('literature')
    assert (
        clean_page_title('Recent research has shown that gut bacteria contribute to purine homeostasis, which') is None
    )
    assert source_display_name('https://europepmc.org/article/MED/1', 'europepmc.org') == 'Europe PMC'
    for title in (
        'Five year experience of gallbladder polyp surveillance and cost effective analysis against new guidelines',
        'A public health milestone: China publishes new Physical Activity and Sedentary Behaviour Guidelines',
    ):
        assert evidence_grade('guideline', [], title) == ('C', '研究论文'), title


def test_meeting_abstracts_and_challenge_tokens_are_not_citable_addresses():
    assert classify_web_source('https://gut.bmj.com/content/gutjnl/75/Suppl_2/A119.2.full.pdf', 'Strain CY01') is None
    assert classify_web_source('https://www.bmj.com/content/1', 'IDDF2026-ABS-0141 IDENTIFICATION OF A STRAIN') is None
    page = classify_web_source('https://www.nature.com/articles/s41586-1?__cf_chl_tk=abc', 'A cohort study in adults')
    assert page.canonical_url == 'https://www.nature.com/articles/s41586-1'


def test_search_result_titles_are_cleaned_or_rejected():
    assert (
        clean_page_title('指南规范详情-人卫临床助手 - 首页 > 临床指南 > 基层2型糖尿病筛查专家共识')
        == '基层2型糖尿病筛查专家共识'
    )
    assert clean_page_title('The critical role of F. prausnitzii in human health: An overview - ScienceDirect') == (
        'The critical role of F. prausnitzii in human health: An overview'
    )
    assert clean_page_title('observations were zero; 64.93% at the class level') is None
    assert clean_page_title('Bacteria can stimulate immune development,44 therefore we investigated') is None
    assert source_display_name('https://rs.yiigle.com/cmaid/1', '百度学术') == '中华医学期刊全文数据库'


def test_web_hits_become_citable_originals_with_integrity_key():
    def handler(request):
        if request.url.host == 'api.deepseek.com':
            return httpx.Response(
                200,
                json={
                    'results': [
                        {
                            'title': 'Follow-up colonoscopy after positive stool tests',
                            'date': '2026-08-18T00:00:00Z',
                            'url': 'https://rd.springer.com/article/10.1007/s11606-026-10630-0#1',
                            'content': BODY,
                            'site_name': 'SpringerLink',
                            'rerank_score': 1.9,
                            'authority': 4,
                        },
                        {
                            'title': '大便带血别等了',
                            'url': 'https://mp.weixin.qq.com/s?id=1',
                            'content': BODY,
                            'rerank_score': 2.5,
                            'authority': 3,
                        },
                        {
                            'title': 'Low score paper',
                            'url': 'https://www.nature.com/articles/s41586-000-0000-0',
                            'content': BODY,
                            'rerank_score': 0.4,
                            'authority': 4,
                        },
                    ]
                },
            )
        assert request.url.host == 'api.crossref.org'
        return httpx.Response(
            200,
            json={
                'message': {
                    'title': ['Informing Measurement of Timely Follow-up Colonoscopies'],
                    'container-title': ['Journal of General Internal Medicine'],
                    'issued': {'date-parts': [[2026, 8]]},
                    'author': [{'given': 'Ada', 'family': 'Researcher'}],
                }
            },
        )

    async def run():
        transport = httpx.MockTransport(handler)
        client = WebEvidenceClient(DeepSeekToolsClient(api_key='k', transport=transport), transport=transport)
        try:
            return await client.search_detailed('positive stool test follow-up colonoscopy', supports=['topic-1'])
        finally:
            await client.aclose()

    outcomes = asyncio.run(run())
    records = [record for outcome in outcomes for record in outcome.records]
    assert [outcome.channel for outcome in outcomes] == ['guide', 'literature']
    assert len(records) == 1
    record = records[0]
    assert record.provider == WEB_PROVIDER and record.doi == '10.1007/s11606-026-10630-0'
    assert record.source == 'Journal of General Internal Medicine' and record.authors == ['Ada Researcher']
    assert record.source_evidence_key == web_original_key(record.evidence_text)
    assert evaluation_original_text(record) == record.evidence_text
    tampered = record.model_copy(update={'evidence_text': record.evidence_text + ' edited'})
    assert evaluation_original_text(tampered) == ''


def _article_hit(index, *, title=None, content=BODY, authority=4, score=None):
    return {
        'title': title or f'Gut microbiome cohort study {index}',
        'url': f'https://www.nature.com/articles/10.1038/study-{index}',
        'content': content,
        'rerank_score': score if score is not None else 3.0 - index / 100,
        'authority': authority,
    }


def _crossref_response(index):
    return httpx.Response(
        200,
        json={
            'message': {
                'title': [f'Gut microbiome cohort study {index}'],
                'container-title': ['Microbiome'],
                'issued': {'date-parts': [[2025]]},
                'author': [{'given': 'Ada', 'family': 'Researcher'}],
            }
        },
    )


def test_ranked_enrichment_stops_after_the_same_first_four_records():
    async def run():
        requests = []
        two_started, release = asyncio.Event(), asyncio.Event()
        hits = [_article_hit(index) for index in range(10)]
        # The provider's input order is not its final authority/score order.
        hits = list(reversed(hits))

        async def handler(request):
            if request.url.host == 'api.deepseek.com':
                return httpx.Response(200, json={'results': hits})
            index = int(request.url.path.rsplit('-', 1)[1])
            requests.append(index)
            if len(requests) == 2:
                two_started.set()
            await release.wait()
            return _crossref_response(index)

        transport = httpx.MockTransport(handler)
        client = WebEvidenceClient(DeepSeekToolsClient(api_key='k', transport=transport), transport=transport)
        task = asyncio.create_task(client.search_detailed('gut microbiome cohort', supports=['topic']))
        try:
            # At least two lookups can run together; this is not a serial walk.
            await asyncio.wait_for(two_started.wait(), timeout=1)
            release.set()
            outcomes = await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await client.aclose()
        records = [record for outcome in outcomes for record in outcome.records]
        assert [record.title for record in records] == [f'Gut microbiome cohort study {i}' for i in range(4)]
        assert sorted(requests) == [0, 1, 2, 3]
        assert outcomes[0].records == []
        assert all(record.supports == ['topic'] for record in records)
        assert all(evaluation_original_text(record) == record.evidence_text for record in records)

    asyncio.run(run())


def test_rejected_and_duplicate_hits_are_replaced_from_later_ranked_batches():
    async def run():
        hits = [_article_hit(index) for index in range(10)]
        hits[0]['url'] = 'https://mp.weixin.qq.com/s?x=1'
        hits[2].update(url=hits[1]['url'], title=hits[1]['title'])
        hits[3]['content'] = 'Too short for a citable original.'
        requests = []

        def handler(request):
            if request.url.host == 'api.deepseek.com':
                return httpx.Response(200, json={'results': hits})
            index = int(request.url.path.rsplit('-', 1)[1])
            requests.append(index)
            if index == 4:
                raise RuntimeError('Malformed provider response')
            return _crossref_response(index)

        transport = httpx.MockTransport(handler)
        client = WebEvidenceClient(
            DeepSeekToolsClient(api_key='k', transport=transport), transport=transport, fetch_when_short=False
        )
        try:
            outcomes = await client.search_detailed('gut microbiome cohort', supports=['topic'])
        finally:
            await client.aclose()
        records = [record for outcome in outcomes for record in outcome.records]
        assert [record.title for record in records] == [f'Gut microbiome cohort study {i}' for i in (1, 5, 6, 7)]
        assert Counter(requests)[1] == 1
        assert not set(requests) & {8, 9}

    asyncio.run(run())


def test_literature_only_search_still_keeps_guideline_originals():
    async def run():
        def handler(request):
            return httpx.Response(
                200,
                json={
                    'results': [
                        {
                            'title': 'Depression in adults: treatment and management',
                            'url': 'https://www.nice.org.uk/guidance/ng222',
                            'content': BODY,
                            'rerank_score': 2.0,
                            'authority': 4,
                        }
                    ]
                },
            )

        transport = httpx.MockTransport(handler)
        client = WebEvidenceClient(DeepSeekToolsClient(api_key='k', transport=transport), transport=transport)
        try:
            outcomes = await client.search_detailed(
                'depression management', supports=['topic'], channels=('literature',)
            )
        finally:
            await client.aclose()
        assert outcomes[0].status == 'not_requested' and outcomes[0].records == []
        assert outcomes[1].status == 'success'
        assert len(outcomes[1].records) == 1 and outcomes[1].records[0].category == 'guideline'

    asyncio.run(run())


@pytest.mark.parametrize('lookup_kind', ['doi', 'pmcid', 'title'])
def test_metadata_lookup_is_shared_by_simultaneous_callers(lookup_kind):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def handler(request):
            requests.append(request)
            started.set()
            await release.wait()
            if lookup_kind == 'pmcid':
                return httpx.Response(
                    200,
                    json={
                        'result': {
                            '123': {
                                'title': 'Gut microbiome cohort study 1',
                                'articleids': [],
                                'pubdate': '2025',
                            }
                        }
                    },
                )
            if lookup_kind == 'title':
                return httpx.Response(
                    200,
                    json={
                        'message': {
                            'items': [
                                {
                                    'title': ['Gut microbiome cohort study 1'],
                                    'DOI': '10.1038/study-1',
                                }
                            ]
                        }
                    },
                )
            return _crossref_response(1)

        transport = httpx.MockTransport(handler)
        client = WebEvidenceClient(
            DeepSeekToolsClient(api_key='k', transport=transport), transport=transport, ncbi_api_key='test-key'
        )

        def lookup():
            if lookup_kind == 'title':
                return client._metadata_by_title('Gut microbiome cohort study 1')
            return client._metadata(
                pmcid='PMC123' if lookup_kind == 'pmcid' else None,
                doi='10.1038/study-1' if lookup_kind == 'doi' else None,
            )

        tasks = [asyncio.create_task(lookup()) for _ in range(3)]
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            release.set()
            results = await asyncio.gather(*tasks)
            assert len(requests) == 1
            assert results[0] == results[1] == results[2]
            results[0].setdefault('authors', []).append('Changed by caller')
            assert 'Changed by caller' not in results[1].get('authors', [])
            assert 'Changed by caller' not in (await lookup()).get('authors', [])
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.aclose()

    asyncio.run(run())


def test_cancelling_one_metadata_waiter_does_not_cancel_another():
    async def run():
        started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        requests = []

        async def handler(request):
            requests.append(request)
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return _crossref_response(1)

        transport = httpx.MockTransport(handler)
        client = WebEvidenceClient(DeepSeekToolsClient(api_key='k', transport=transport), transport=transport)
        tasks = [asyncio.create_task(client._metadata(pmcid=None, doi='10.1038/study-1')) for _ in range(2)]
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            assert not cancelled.is_set()
            release.set()
            result = await tasks[1]
            assert result['title'] == 'Gut microbiome cohort study 1'
            assert len(requests) == 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize('close_client', [False, True])
def test_unused_metadata_requests_are_cancelled_and_drained(close_client):
    async def run():
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def handler(request):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        transport = httpx.MockTransport(handler)
        client = WebEvidenceClient(DeepSeekToolsClient(api_key='k', transport=transport), transport=transport)
        task = asyncio.create_task(client._metadata(pmcid=None, doi='10.1038/study-1'))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            if close_client:
                await client.aclose()
            else:
                task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cancelled.is_set()
            assert client._metadata_inflight == {}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.aclose()

    asyncio.run(run())


def test_positive_and_negative_metadata_caches_have_separate_expiry(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(web_evidence, 'time', SimpleNamespace(monotonic=lambda: clock[0]))

    async def run():
        calls = Counter()

        def handler(request):
            index = int(request.url.path.rsplit('-', 1)[1])
            calls[index] += 1
            if index == 2 and calls[index] == 1:
                return httpx.Response(503)
            return _crossref_response(index)

        transport = httpx.MockTransport(handler)
        client = WebEvidenceClient(
            DeepSeekToolsClient(api_key='k', transport=transport),
            transport=transport,
            metadata_cache_ttl_seconds=10,
            metadata_negative_cache_ttl_seconds=2,
        )
        try:
            assert await client._metadata(pmcid=None, doi='10.1038/study-1')
            assert await client._metadata(pmcid=None, doi='10.1038/study-2') == {}
            clock[0] = 1
            assert await client._metadata(pmcid=None, doi='10.1038/study-1')
            assert await client._metadata(pmcid=None, doi='10.1038/study-2') == {}
            assert calls == {1: 1, 2: 1}
            clock[0] = 3
            assert await client._metadata(pmcid=None, doi='10.1038/study-2')
            assert await client._metadata(pmcid=None, doi='10.1038/study-1')
            assert calls == {1: 1, 2: 2}
            clock[0] = 11
            assert await client._metadata(pmcid=None, doi='10.1038/study-1')
            assert calls == {1: 2, 2: 2}
        finally:
            await client.aclose()

    asyncio.run(run())


def test_metadata_cache_evicts_the_least_recently_used_identity():
    async def run():
        calls = Counter()

        def handler(request):
            index = int(request.url.path.rsplit('-', 1)[1])
            calls[index] += 1
            return _crossref_response(index)

        transport = httpx.MockTransport(handler)
        client = WebEvidenceClient(
            DeepSeekToolsClient(api_key='k', transport=transport), transport=transport, metadata_cache_size=2
        )
        try:
            for index in (1, 2, 1, 3, 2):
                assert await client._metadata(pmcid=None, doi=f'10.1038/study-{index}')
            assert calls == {1: 1, 2: 2, 3: 1}
            assert len(client._metadata_cache) == 2
        finally:
            await client.aclose()

    asyncio.run(run())
