"""合成病例下的解释边界、来源校验、预算和缓存测试，不调用真实服务。"""

import asyncio
import json
from copy import deepcopy

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agents import AgentHub
from agents import AgentRuntime
from agents import GraphRegistry
from agents.graphs.health_profile import register_profile_evidence
from agents.graphs.health_profile.focus import association_anchor
from agents.graphs.health_profile.focus import clinical_anchor
from agents.graphs.health_profile.focus import default_topics
from agents.graphs.health_profile.interpretation import FocusPlan
from agents.graphs.health_profile.interpretation import InterpretationCache
from agents.graphs.health_profile.interpretation import ProfileInterpreter
from agents.graphs.health_profile.interpretation import _planned_topics
from agents.graphs.health_profile.profile import create_profile_focus_profile
from agents.graphs.health_profile.profile import create_profile_synthesis_profile
from agents.knowledge import KnowledgeDocument
from agents.knowledge import KnowledgeSource
from agents.llm import ModelProfileRegistry
from agents.llm.types import LLMResult


def fact(identity, name, fact_type, category='laboratory', interpretation='high', value=None):
    return {
        'factId': identity,
        'displayName': name,
        'factType': fact_type,
        'category': category,
        'interpretation': interpretation,
        'value': value,
        'assertionLevel': 'observed',
        'verificationStatus': 'verified_source',
    }


def case_facts():
    return [
        fact('dental', '牙齿发育异常', 'condition.diagnosis', 'condition', None, '牙齿发育异常'),
        fact('gut', '便秘', 'symptom.digestive', 'symptom', None, '便秘'),
        fact('glucose', '血糖', 'laboratory.glucose'),
        fact('lipids', '甘油三酯', 'laboratory.triglyceride'),
        fact('critical', '血钾', 'laboratory.potassium', interpretation='critical'),
    ]


class ScenarioKnowledge:
    enabled = True

    def __init__(self, *, original=True, mode='success', delay=0, count=1):
        self.original = original
        self.mode = mode
        self.delay = delay
        self.count = count
        self.queries = []
        self.active = 0
        self.peak = 0

    async def retrieve(self, query):
        self.queries.append(query)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.mode == 'failure' or (self.mode == 'partial' and '血糖' in query.query):
                raise RuntimeError('synthetic retrieval failure')
            if self.mode == 'empty' or (self.mode == 'literature_only' and query.source is KnowledgeSource.GUIDE):
                return ()
            text = query.query + (
                '曾在胃肠炎病例中被报告，应结合症状与具体检测方法判断关联。'
                if '菌' in query.query
                else '相关评估需要结合症状、检验结果和原始报告进行综合判断。'
            )
            return tuple(
                KnowledgeDocument(
                    source=query.source,
                    title=f'{query.query}合成资料{index}',
                    content=text,
                    metadata={
                        'id': f'{query.query}-{index}',
                        'knowledgeBaseRevision': 'synthetic-v1',
                        'sourceTexts': [{'kind': 'abstract', 'text': text}] if self.original else [],
                    },
                )
                for index in range(self.count)
            )
        finally:
            self.active -= 1


class ScenarioLLM:
    def __init__(self, *, mode='success'):
        self.calls = []
        self.mode = mode

    def stream(self, request):
        raise NotImplementedError

    async def complete(self, request):
        payload = json.loads(request.content)
        self.calls.append((request, payload))
        if self.mode == 'all_failure':
            raise RuntimeError('synthetic model failure')
        if request.profile.name == 'profile.plan_focus':
            return LLMResult(content='{"topics": []}')
        if self.mode == 'synthesis_failure':
            raise RuntimeError('synthetic synthesis failure')
        if self.mode == 'slow':
            await asyncio.sleep(1)
        if self.mode == 'malformed':
            return LLMResult(content='not-json')
        if self.mode == 'no_relevant':
            return LLMResult(content='{"insights": []}')
        indexed = {item['factId']: item for item in payload['facts']}
        insights = []
        for topic in payload['topics']:
            doc = next((doc for doc in payload['documents'] if topic['topicId'] in doc['topicIds']), None)
            if doc is None:
                continue
            texts = doc['sourceTexts']
            quote = texts[0]['text'] if texts else doc['title'] + '未提供原文'
            kind = 'risk' if any(clinical_anchor(indexed[key]) for key in topic['factIds']) else 'association'
            insight = {
                'topicId': topic['topicId'],
                'kind': kind,
                'title': topic['title'] + '相关问题',
                'explanation': '当前'
                + topic['title']
                + '存在需关注的记录，所引资料说明应结合症状、检验结果和原始报告评估。',
                'action': '携带原始报告评估' + topic['title'],
                'followUp': '补充相关症状及病程信息',
                'factIds': topic['factIds'],
                'supports': [{'evidenceId': doc['evidenceId'], 'quote': quote}],
            }
            if kind == 'association':
                insight['explanation'] = (
                    topic['title'] + '的当前结果异常或明确检出；原文报告其与胃肠炎病例有关，但不能据此判断本人感染。'
                )
            if self.mode == 'vague_association' and kind == 'association':
                insight['explanation'] = topic['title'] + '需要结合现有事实进一步评估。'
            if self.mode in {'internal_ids', 'only_internal_ids'}:
                reference = (
                    f'（factId：{topic["factIds"][0]}；evidenceId：{doc["evidenceId"]}；topicId：{topic["topicId"]}）'
                )
                insight['explanation'] = (
                    reference if self.mode == 'only_internal_ids' else insight['explanation'] + reference
                )
            if self.mode == 'fake_quote':
                insight['supports'][0]['quote'] = '这段杜撰原文根本不存在于输入资料中。'
            elif self.mode == 'blank_quote':
                insight['supports'][0]['quote'] = ' ' * 20
            elif self.mode == 'fake_fact':
                insight['factIds'] = ['invented-fact']
            elif self.mode == 'fake_evidence':
                insight['supports'][0]['evidenceId'] = 'invented-evidence'
            elif self.mode == 'diagnosis':
                insight['explanation'] = '患者已确诊某种疾病。'
            elif self.mode == 'background_risk':
                insight['kind'] = 'risk'
            insights.append(insight)
        return LLMResult(content=json.dumps({'insights': insights}, ensure_ascii=False))


def interpreter(knowledge=None, llm=None, **kwargs):
    return ProfileInterpreter(
        knowledge or ScenarioKnowledge(),
        llm_gateway=llm or ScenarioLLM(),
        planner_profile=create_profile_focus_profile(),
        synthesis_profile=create_profile_synthesis_profile(),
        **kwargs,
    )


def runtime_hub(knowledge, llm):
    registry = GraphRegistry()
    runtime = AgentRuntime(
        hub=AgentHub(registry),
        registry=registry,
        profiles=ModelProfileRegistry(),
        llm_gateway=llm,
        knowledge_gateway=knowledge,
        context_repository=object(),
        lease_manager=object(),
        checkpointer=InMemorySaver(),
        _close_callback=lambda: None,
    )
    register_profile_evidence(runtime)
    return runtime.hub


async def run(instance, facts=None, **kwargs):
    options = {
        'user_id': 'synthetic-user',
        'subject_id': 'synthetic-subject',
        'profile_version': 1,
        'clinical_context': {'age': 35},
    }
    options.update(kwargs)
    return await instance.run(facts if facts is not None else case_facts(), **options)


@pytest.mark.asyncio
async def test_focus_covers_both_specialties_and_preserves_other_problems():
    llm = ScenarioLLM()
    knowledge = ScenarioKnowledge(count=3)
    output = await run(interpreter(knowledge, llm))
    assert output['complete'] is True
    ids = [item['factIds'] for item in output['insights']]
    assert ids[0] == ['critical']
    assert ids.index(['gut']) < ids.index(['dental'])
    assert ids.index(['glucose']) < ids.index(['dental'])
    # 条目只保留实际支撑已验证解释的资料；结论一律为解释态。
    items = output['evidence']['items']
    assert items
    assert len(items) <= 12
    assert all(item['conclusion'] and '检索资料摘要' not in item['conclusion'] for item in items)
    cited = {support['evidenceId'] for insight in output['insights'] for support in insight['supports']}
    assert {item['evidenceId'] for item in items} == cited
    assert {key for item in items for key in item['factIds']} == {
        'critical',
        'gut',
        'glucose',
        'lipids',
        'dental',
    }
    assert all(query.source is KnowledgeSource.GUIDE for query in knowledge.queries)
    assert len(llm.calls) == 2
    assert all(request.conversation_key is None and request.thinking is False for request, _ in llm.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['fake_quote', 'blank_quote', 'fake_fact', 'fake_evidence', 'diagnosis', 'malformed'])
async def test_invalid_model_conclusions_return_no_summary_items(mode):
    output = await run(interpreter(llm=ScenarioLLM(mode=mode)))
    assert output['insights'] == []
    assert output['complete'] is False
    # 解释全部未通过校验时不再返回摘要态条目凑数。
    assert output['evidence']['status'] == 'empty'
    assert output['evidence']['items'] == []


@pytest.mark.asyncio
async def test_ai_summary_cannot_independently_support_new_disease_risk():
    output = await run(interpreter(knowledge=ScenarioKnowledge(original=False)))
    assert output['insights'] == []
    assert output['evidence']['status'] == 'empty'
    assert output['evidence']['items'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('interpretation', [None, 'critical'])
async def test_aggregate_microbiome_metric_never_starts_evidence(interpretation):
    knowledge = ScenarioKnowledge()
    llm = ScenarioLLM()
    facts = [fact('micro', '菌群多样性', 'microbiome.diversity', interpretation=interpretation)]
    output = await run(interpreter(knowledge, llm), facts)
    assert output['evidence']['status'] == 'empty'
    assert output['evidence']['items'] == []
    assert output['insights'] == []
    assert knowledge.queries == [] and llm.calls == []


def test_only_meaningful_current_facts_start_topics():
    facts = [
        fact('normal', '血糖', 'laboratory.glucose', interpretation='normal'),
        fact('abnormal', '甘油三酯', 'laboratory.triglyceride'),
        fact('condition', '慢性便秘', 'condition.diagnosis', 'condition', None, '慢性便秘'),
        fact('denied', '腹痛', 'symptom.abdominal_pain', 'symptom', None, '否认腹痛'),
        fact('taxon', '类志贺邻单胞菌', 'report_observation.taxon', 'report_observation', 'high', 0.0002),
        fact('detected', '病原菌甲', 'report_observation.detected', 'report_observation', None, '检出'),
        fact('unmarked', '菌属乙', 'report_observation.unmarked', 'report_observation', None, 0.3),
        fact('not_detected', '菌属丙', 'report_observation.negative', 'report_observation', None, '未检出'),
        fact('score', '肠道年龄', 'assessment.microbiome_age', 'assessment', 'critical', 52),
    ]
    topics = default_topics(facts)
    selected = {key for topic in topics for key in topic['factIds']}
    assert selected == {'abnormal', 'condition', 'taxon', 'detected'}
    assert association_anchor(facts[4]) and association_anchor(facts[5])
    assert not association_anchor(facts[6]) and not association_anchor(facts[7])


@pytest.mark.asyncio
async def test_retrieved_but_unrelated_documents_do_not_become_summary_items():
    knowledge = ScenarioKnowledge()
    output = await run(interpreter(knowledge, ScenarioLLM(mode='no_relevant')))
    assert knowledge.queries
    assert output['evidence']['status'] == 'empty'
    assert output['evidence']['items'] == []


@pytest.mark.asyncio
async def test_association_requires_original_quote_and_never_uses_summary():
    facts = [
        fact(
            'microbe',
            '类志贺邻单胞菌',
            'report_observation.microbe',
            'report_observation',
            'high',
            0.0002,
        )
    ]
    invalid = await run(interpreter(llm=ScenarioLLM(mode='fake_quote')), facts)
    assert invalid['evidence']['status'] == 'empty'
    assert invalid['evidence']['items'] == []
    no_original = await run(interpreter(knowledge=ScenarioKnowledge(original=False)), facts)
    assert no_original['evidence']['status'] == 'empty'
    assert no_original['evidence']['items'] == []
    vague = await run(interpreter(llm=ScenarioLLM(mode='vague_association')), facts)
    assert vague['evidence']['status'] == 'empty'
    assert vague['evidence']['items'] == []


def test_model_cannot_add_normal_only_topic():
    facts = [
        fact('normal', '血糖', 'laboratory.glucose', interpretation='normal'),
        fact('abnormal', '甘油三酯', 'laboratory.triglyceride'),
    ]
    plan = FocusPlan.model_validate(
        {
            'topics': [
                {
                    'title': '正常血糖',
                    'factIds': ['normal'],
                    'query': '血糖 评价',
                    'domain': 'metabolic',
                    'relationship': 'direct',
                    'rationale': '正常值',
                }
            ]
        }
    )
    topics, valid = _planned_topics(plan, facts, default_topics(facts))
    assert valid is False
    assert {key for topic in topics for key in topic['factIds']} == {'abnormal'}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,status', [('empty', 'empty'), ('failure', 'unavailable'), ('partial', 'ready')])
async def test_retrieval_outcome_is_separate_from_model_outcome(mode, status):
    output = await run(interpreter(knowledge=ScenarioKnowledge(mode=mode)))
    assert output['evidence']['status'] == status
    if mode != 'empty':
        assert output['complete'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['all_failure', 'synthesis_failure', 'slow'])
async def test_stage_failure_or_deadline_preserves_completed_retrieval(mode):
    output = await run(interpreter(llm=ScenarioLLM(mode=mode), timeout_seconds=0.08))
    # 解释阶段失败时不再返回摘要条目；检索结果仅作为内部线索被丢弃。
    assert output['evidence']['status'] == 'empty'
    assert output['evidence']['items'] == []
    assert output['insights'] == []
    assert output['complete'] is False


@pytest.mark.asyncio
async def test_query_budget_and_parallelism_include_literature_fallback():
    knowledge = ScenarioKnowledge(mode='literature_only', delay=0.002)
    facts = [*case_facts(), fact('thyroid', '促甲状腺激素', 'laboratory.tsh')]
    output = await run(interpreter(knowledge), facts)
    assert len(knowledge.queries) == 8
    assert len([query for query in knowledge.queries if query.source is KnowledgeSource.LITERATURE]) == 2
    assert knowledge.peak <= 3
    assert output['evidence']['status'] == 'ready'


@pytest.mark.asyncio
async def test_flagged_microbe_checks_literature_even_when_guide_hits():
    knowledge = ScenarioKnowledge(count=3)
    llm = ScenarioLLM()
    facts = [
        fact(
            'microbe',
            '类志贺邻单胞菌',
            'report_observation.microbe',
            'report_observation',
            'high',
            0.0002,
        )
    ]
    output = await run(interpreter(knowledge, llm), facts)
    assert [query.source for query in knowledge.queries] == [
        KnowledgeSource.GUIDE,
        KnowledgeSource.LITERATURE,
    ]
    assert output['evidence']['status'] == 'ready'
    assert output['evidence']['items'][0]['citations'][0]['source'] == KnowledgeSource.LITERATURE.value
    assert output['insights'][0]['kind'] == 'association'
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_singleflight_cache_version_identity_and_input_isolation():
    knowledge = ScenarioKnowledge(delay=0.002)
    llm = ScenarioLLM()
    instance = interpreter(knowledge, llm)
    first, second = await asyncio.gather(run(instance), run(instance))
    assert len(llm.calls) == 2
    assert first['evidence']['generatedAt'] == second['evidence']['generatedAt']
    first['evidence']['items'].clear()
    assert (await run(instance))['evidence']['items']
    for options in (
        {'profile_version': 2},
        {'subject_id': 'other'},
        {'user_id': 'other'},
        {'clinical_context': {'age': 36}},
    ):
        await run(instance, **options)
    changed = deepcopy(case_facts())
    changed[0]['value'] = '另一条当前事实'
    await run(instance, changed)
    assert len(llm.calls) == 12


@pytest.mark.asyncio
async def test_cache_expiration_capacity_and_failed_results():
    clock = [0.0]
    cache = InterpretationCache(ttl=10, capacity=2, clock=lambda: clock[0])
    calls = []

    async def create(complete=True):
        calls.append(1)
        return {'complete': complete}

    for key in ('a', 'b', 'c'):
        await cache.get_or_create(key, create)
    assert list(cache.entries) == ['b', 'c']
    clock[0] = 11
    await cache.get_or_create('c', create)
    await cache.get_or_create('fail', lambda: create(False))
    await cache.get_or_create('fail', lambda: create(False))
    assert len(calls) == 6
    assert list(cache.entries) == ['c']


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_cancel_another_waiter():
    cache = InterpretationCache()
    started = asyncio.Event()
    release = asyncio.Event()

    async def create():
        started.set()
        await release.wait()
        return {'complete': True}

    first = asyncio.create_task(cache.get_or_create('same', create))
    await started.wait()
    second = asyncio.create_task(cache.get_or_create('same', create))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert (await second)['complete'] is True
    assert cache.inflight == {}


@pytest.mark.asyncio
async def test_cancel_last_waiter_cancels_generation_and_does_not_cache():
    knowledge = ScenarioKnowledge(delay=1)
    instance = interpreter(knowledge)
    task = asyncio.create_task(run(instance))
    while not knowledge.queries:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert knowledge.active == 0
    assert instance.cache.inflight == {}
    assert instance.cache.entries == {}


def test_unrelated_condition_falls_back_without_inventing_specialty_link():
    topics = default_topics([case_facts()[0]])
    assert len(topics) == 1
    assert topics[0]['domain'] == 'other'
    assert '肠道' not in topics[0]['query']


def test_single_peripheral_fact_cannot_be_promoted_by_weak_indirect_association():
    facts = [case_facts()[0]]
    plan = FocusPlan.model_validate(
        {
            'topics': [
                {
                    'title': '肠道问题',
                    'factIds': ['dental'],
                    'query': '牙齿发育异常 肠道风险',
                    'domain': 'gut',
                    'relationship': 'indirect',
                    'rationale': '仅有远端相关性',
                }
            ]
        }
    )
    topics, valid = _planned_topics(plan, facts, default_topics(facts))
    assert valid is False
    assert topics[0]['domain'] == 'other'


def test_model_combining_all_facts_cannot_remove_a_specialty_from_plan():
    facts = case_facts()[1:4]
    plan = FocusPlan.model_validate(
        {
            'topics': [
                {
                    'title': '合并的健康问题',
                    'factIds': [fact['factId'] for fact in facts],
                    'query': '便秘 血糖 甘油三酯',
                    'domain': 'gut',
                    'relationship': 'direct',
                    'rationale': '多项事实合并',
                }
            ]
        }
    )
    topics, valid = _planned_topics(plan, facts, default_topics(facts))
    assert valid is True
    assert {topic['domain'] for topic in topics} == {'gut', 'metabolic'}


@pytest.mark.asyncio
async def test_shared_document_keeps_every_topic_fact_and_original_passage():
    class SharedKnowledge(ScenarioKnowledge):
        async def retrieve(self, query):
            doc = (await super().retrieve(query))[0]
            return (
                KnowledgeDocument(
                    source=doc.source,
                    title='共同的合成指南',
                    content=doc.content,
                    metadata={**doc.metadata, 'id': 'shared-guide'},
                ),
            )

    result = await run(interpreter(knowledge=SharedKnowledge()), case_facts()[1:3])
    assert len(result['evidence']['items']) == 1
    assert set(result['evidence']['items'][0]['factIds']) == {'gut', 'glucose'}
    assert {insight['factIds'][0] for insight in result['insights']} == {'gut', 'glucose'}


def test_explicit_false_symptom_is_not_a_positive_clinical_anchor():
    assert not clinical_anchor(fact('denied', '腹痛', 'symptom.abdominal_pain', 'symptom', None, False))


@pytest.mark.asyncio
async def test_malformed_original_text_metadata_does_not_erase_other_retrieved_results():
    class MalformedMetadata(ScenarioKnowledge):
        async def retrieve(self, query):
            docs = await super().retrieve(query)
            return tuple(
                KnowledgeDocument(
                    source=doc.source,
                    title=doc.title,
                    content=doc.content,
                    metadata={**doc.metadata, 'sourceTexts': None},
                )
                for doc in docs
            )

    result = await run(interpreter(knowledge=MalformedMetadata()))
    # 无原文支撑的解释不会通过校验；条目不再降级为摘要态。
    assert result['evidence']['status'] == 'empty'
    assert result['insights'] == []
