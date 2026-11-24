"""内部绑定标识不能进入用户解释；清理不改变事实、原文与引用关联。"""

from copy import deepcopy

import pytest

from agents.graphs.health_profile import interpretation as interpretation_module
from agents.graphs.health_profile.focus import POLICY_VERSION
from agents.graphs.health_profile.focus import fact_rank
from agents.graphs.health_profile.interpretation import evidence_items
from agents.graphs.health_profile.interpretation import validate_insights
from agents.knowledge import KnowledgeDocument
from agents.knowledge import KnowledgeSource
from tests.unit.agents.graphs.health_profile.test_profile_interpretation import ScenarioLLM
from tests.unit.agents.graphs.health_profile.test_profile_interpretation import fact
from tests.unit.agents.graphs.health_profile.test_profile_interpretation import interpreter
from tests.unit.agents.graphs.health_profile.test_profile_interpretation import run

FACT_ID = 'fact_a97dc2b8fbd5421c996d9ce04467642f'
EVIDENCE_ID = 'evidence_60f5b8090d835a19f2bd7dce'
TOPIC_ID = 'other.synthetic_microbe'
BASE = 'Bacteroides coprophilus 当前结果偏高'
QUOTE = 'Bacteroides coprophilus is discussed in this synthetic source passage.'


def validate_text(explanation, *, quote=QUOTE):
    facts = [fact(FACT_ID, 'Bacteroides coprophilus', 'report_observation.microbe', 'report_observation')]
    topics = [
        {
            'topicId': TOPIC_ID,
            'factIds': [FACT_ID],
            'rank': fact_rank(facts[0]),
        }
    ]
    document = KnowledgeDocument(
        source=KnowledgeSource.LITERATURE,
        title='合成检测资料',
        content=quote,
        metadata={'id': 'synthetic-citation', 'sourceTexts': [{'kind': 'abstract', 'text': quote}]},
    )
    documents = {EVIDENCE_ID: {'document': document, 'factIds': [FACT_ID], 'topicIds': [TOPIC_ID]}}
    raw = {
        'topicId': TOPIC_ID,
        'kind': 'association',
        'title': '具体菌种关联',
        'explanation': explanation,
        'action': '',
        'followUp': '',
        'factIds': [FACT_ID],
        'supports': [{'evidenceId': EVIDENCE_ID, 'quote': quote}],
    }
    before = deepcopy(raw)
    accepted, complete = validate_insights({'insights': [raw]}, facts, topics, documents)
    assert raw == before
    return accepted, complete, documents, facts


@pytest.mark.parametrize(
    'suffix,expected',
    [
        (f'（{FACT_ID}）。', '。'),
        (f'({EVIDENCE_ID}).', '.'),
        (f'[{TOPIC_ID}]。', '。'),
        (f'（factIds: [{FACT_ID}，{FACT_ID}]） 。', '。'),
        (f'（"factId": "{FACT_ID}"; "evidenceId": "{EVIDENCE_ID}"）。', '。'),
        (f'【{FACT_ID}、{EVIDENCE_ID}】。', '。'),
        (f' {FACT_ID} {EVIDENCE_ID}。', '。'),
        (f'（factId：{FACT_ID}；evidenceId={EVIDENCE_ID}；topicId: {TOPIC_ID}）。', '。'),
        (f'（事实ID：{FACT_ID}，证据编号：{EVIDENCE_ID}）。', '。'),
        (f'（{FACT_ID}，{FACT_ID}，{EVIDENCE_ID}）。', '。'),
        ('（fact_not_supplied_123；evidence_unknown_456）。', '。'),
        (f'（{FACT_ID}；Bacteroides coprophilus，5.5123；{EVIDENCE_ID}）。', '（Bacteroides coprophilus，5.5123）。'),
        (f'（5.5123；{FACT_ID}；参考范围0-0.2394）。', '（5.5123；参考范围0-0.2394）。'),
        (f'，factId：{FACT_ID}，需结合原报告评估。', '，需结合原报告评估。'),
    ],
)
def test_internal_references_removed_without_discarding_valid_explanation(suffix, expected):
    accepted, complete, documents, facts = validate_text(BASE + suffix)
    assert complete and len(accepted) == 1
    assert accepted[0]['explanation'] == BASE + expected
    assert accepted[0]['factIds'] == [FACT_ID]
    assert accepted[0]['supports'] == [{'evidenceId': EVIDENCE_ID, 'quote': QUOTE}]
    items = evidence_items(documents, accepted, facts)
    assert items[0]['conclusion'] == BASE + expected
    assert items[0]['evidenceId'] == EVIDENCE_ID
    assert items[0]['factIds'] == [FACT_ID]
    assert items[0]['citations'][0]['citationId'] == 'synthetic-citation'


def test_user_report_example_keeps_measurements_and_original_explanation():
    text = (
        '您的检测结果显示“Bacteroides coprophilus”为5.5123，高于参考范围0-0.2394'
        f'（{FACT_ID}）。所提供的原始研究下载了170例2型糖尿病患者和174例健康对照者的肠道宏基因组数据，'
        '发现Bacteroides coprocola（注意：该研究针对的是B. coprocola，与您的检测项B. coprophilus为不同菌种）'
        '的相对丰度在两组间无差异，但在T2DM患者中具有特征性的单核苷酸多态性分布，'
        '并鉴定出65个基因存在显著不同的SNP富集，其中部分基因编码糖苷水解酶；'
        '研究者提出不同菌株可能对人体肠道有不同作用，且特定的B. coprocola菌株与T2DM相关'
        f'（{EVIDENCE_ID}）。需要注意的是，该研究针对的是B. coprocola而非B. coprophilus，'
        '种名不同，结论不能直接套用于您的检测结果；此外，该研究为观察性比较，不能说明该菌导致疾病，'
        '也不能据此判断您个人是否感染或患病。'
    )
    accepted, complete, documents, facts = validate_text(text)
    assert complete
    expected = text.replace(f'（{FACT_ID}）', '').replace(f'（{EVIDENCE_ID}）', '')
    assert evidence_items(documents, accepted, facts)[0]['conclusion'] == expected


def test_normal_prose_numbers_and_publication_identifiers_are_unchanged():
    text = (
        BASE + '（B. coprophilus，5.5123；参考范围0-0.2394），原文170例和174例，65个基因。'
        f'DOI: 10.1186/s40168-017-0247-0、10.1234/{TOPIC_ID}.appendix；PMID: 28143583。'
    )
    accepted, complete, _, _ = validate_text(text)
    assert complete
    assert accepted[0]['explanation'] == text


def test_original_quote_is_not_cleaned():
    quote = QUOTE + f' Source annotation: {FACT_ID}.'
    accepted, complete, documents, _ = validate_text(BASE + f'（{FACT_ID}）。', quote=quote)
    assert complete
    assert FACT_ID not in accepted[0]['explanation']
    assert accepted[0]['supports'][0]['quote'] == quote
    assert documents[EVIDENCE_ID]['document'].metadata['sourceTexts'][0]['text'] == quote


@pytest.mark.parametrize(
    'text',
    [
        f'（factId：{FACT_ID}；evidenceId：{EVIDENCE_ID}）',
        f'{BASE.replace("Bacteroides coprophilus ", "")}（{FACT_ID}）。',
        f'Bacteroides coprophilus（{FACT_ID}）可作为健康背景。',
    ],
)
def test_empty_or_unqualified_cleaned_explanation_is_rejected(text):
    accepted, complete, documents, facts = validate_text(text)
    assert accepted == [] and complete is False
    assert evidence_items(documents, accepted, facts) == []


@pytest.mark.asyncio
async def test_cached_results_are_clean_and_do_not_add_model_calls():
    llm = ScenarioLLM(mode='internal_ids')
    instance = interpreter(llm=llm)
    facts = [fact(FACT_ID, '血糖', 'laboratory.glucose')]
    first = await run(instance, facts)
    second = await run(instance, facts)
    assert first['complete'] and second['complete']
    assert first['evidence']['status'] == 'ready'
    assert first['evidence']['generatedAt'] == second['evidence']['generatedAt']
    for result in (first, second):
        assert result['evidence']['items']
        assert all(
            'fact_' not in item['conclusion'] and 'evidence_' not in item['conclusion']
            for item in result['evidence']['items']
        )
        assert all(
            'fact_' not in insight['explanation'] and 'evidence_' not in insight['explanation']
            for insight in result['insights']
        )
        assert result['evidence']['items'][0]['factIds'] == [FACT_ID]
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_previous_policy_cache_cannot_return_id_leaking_text(monkeypatch):
    llm = ScenarioLLM()
    instance = interpreter(llm=llm)
    facts = [fact(FACT_ID, '血糖', 'laboratory.glucose')]
    monkeypatch.setattr(interpretation_module, 'POLICY_VERSION', 'gut-metabolic-explanation-v2')
    await run(instance, facts)
    for _, cached in instance.cache.entries.values():
        cached['evidence']['items'][0]['conclusion'] = f'旧缓存 {FACT_ID}'
    monkeypatch.setattr(interpretation_module, 'POLICY_VERSION', POLICY_VERSION)
    result = await run(instance, facts)
    assert result['evidence']['status'] == 'ready'
    assert FACT_ID not in result['evidence']['items'][0]['conclusion']
    assert len(llm.calls) == 4


@pytest.mark.asyncio
async def test_id_only_model_response_returns_empty_items():
    llm = ScenarioLLM(mode='only_internal_ids')
    result = await run(interpreter(llm=llm), [fact(FACT_ID, '血糖', 'laboratory.glucose')])
    assert result['evidence']['status'] == 'empty'
    assert result['evidence']['items'] == []
    assert result['insights'] == []
