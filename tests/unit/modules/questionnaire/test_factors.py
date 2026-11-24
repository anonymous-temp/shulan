"""Offline evidence and lifecycle contracts for individual factor assessment."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from adapters.questionnaire_evidence import create_questionnaire_evidence_policy
from adapters.questionnaire_factors import QuestionnaireFactorModel
from agents.knowledge.types import KnowledgeDocument
from agents.knowledge.types import KnowledgeSource
from modules.questionnaire.evidence import LEGACY_EVIDENCE_RULE_SELECTION
from modules.questionnaire.factors import FactorContractError
from modules.questionnaire.factors import QuestionnaireFactorPipeline
from modules.questionnaire.factors import bind_questions
from modules.questionnaire.factors_evidence import index_reports
from modules.questionnaire.factors_evidence import source_material
from modules.questionnaire.schemas import normalize_answers

ORIGINAL = 'Excess adiposity is associated with elevated triglycerides.'
REPORT = {
    'reportTime': '2026-06-30',
    'observations': [
        {'itemName': '甘油三酯', 'resultValue': '3.34', 'unit': 'mmol/L', 'flag': 'high'},
        {'itemName': 'BMI', 'resultValue': '29.3', 'unit': 'kg/m2', 'interpretation': 'AI claims a definite cause'},
    ],
}
QUESTIONNAIRE = {
    'title': '本次问卷',
    'pages': [
        {
            'name': 'p',
            'elements': [
                {
                    'name': 'duration',
                    'type': 'radiogroup',
                    'title': '体检前是否已存在此情况？',
                    'choices': [
                        {'value': 'before', 'text': '体检前已持续一年'},
                        {'value': 'unknown', 'text': '不清楚'},
                    ],
                },
                {'name': 'new_symptom', 'type': 'text', 'title': '本次还有哪些症状？'},
            ],
        }
    ],
}


class Knowledge:
    enabled = True

    def __init__(self, mode='valid'):
        self.mode, self.calls = mode, []

    async def retrieve(self, query):
        self.calls.append(query)
        assert query.include_source_text is True
        if self.mode == 'error':
            raise RuntimeError('offline unavailable')
        if self.mode == 'empty':
            return ()
        metadata = {'id': 'guide-1', 'knowledgeBaseRevision': '2026-01', 'sourceTexts': []}
        if self.mode != 'summary_only':
            metadata['sourceTexts'] = [{'kind': 'abstract', 'text': ORIGINAL}]
        return (
            KnowledgeDocument(
                source=KnowledgeSource.GUIDE,
                title='Original guideline',
                content='An AI-generated summary.',
                url='https://example.org/guideline',
                metadata=metadata,
            ),
        )


class Model:
    def __init__(self, *, web_material=True, kb_inapplicable=False, fake_personal_quote=False, fake_quote=False):
        self.calls = []
        self.web_material = web_material
        self.kb_inapplicable = kb_inapplicable
        self.fake_personal_quote = fake_personal_quote
        self.fake_quote = fake_quote

    async def run(self, stage, payload, *, web_search=False):
        self.calls.append((stage, deepcopy(payload), web_search))
        sources = []
        if stage == 'targets':
            factual = [row['id'] for row in payload['evidence'] if row['kind'] == 'report_record']
            output = {
                'targets': [
                    {
                        'label': '甘油三酯升高',
                        'kind': 'indicator',
                        'evidenceIds': factual,
                        'interpretation': '模型风险解释；报告值不等于个人病因。',
                        'query': 'elevated triglycerides risk factors',
                    }
                ]
                if factual
                else [],
                'coveredEvidenceIds': [row['id'] for row in payload['evidence']],
            }
        elif stage == 'search':
            output = {'sources': [{'url': 'https://invented.invalid', 'text': 'invented by model'}]}
            if self.web_material:
                sources = [
                    {
                        'title': 'Actual retrieved source',
                        'url': 'https://example.org/actual',
                        'text': ORIGINAL,
                        'locator': {'section': 'Risk factors'},
                        'version': '1',
                    }
                ]
        elif stage == 'candidates':
            passage = payload['passages'][0]
            inapplicable = self.kb_inapplicable and passage['url'].endswith('guideline')
            output = {
                'candidates': []
                if inapplicable
                else [
                    {
                        'concept': {'id': 'excess_adiposity', 'label': '脂肪过量'},
                        'relationType': 'risk_factor',
                        'supportExplanation': '材料支持脂肪过量与该指标的关联，不能据此证明个人病因。',
                        'knowledgeEvidence': [
                            {
                                'sourceId': passage['sourceId'],
                                'passageId': passage['passageId'],
                                'quote': 'fabricated association' if self.fake_quote else ORIGINAL,
                            }
                        ],
                        'conditions': [
                            {'id': 'presence', 'label': '本人存在脂肪过量', 'kind': 'presence', 'required': True},
                            {
                                'id': 'time',
                                'label': '相关时期已存在',
                                'kind': 'temporality',
                                'required': True,
                                'questionHint': '体检前此情况持续多久？',
                                'informationFacet': 'duration',
                            },
                        ],
                    }
                ],
                'gaps': ['材料不适用于目标'] if inapplicable else [],
            }
        elif stage == 'answer_targets':
            selected = [row for row in payload['answers'] if '反复头痛' in row['answerText']]
            output = {
                'targets': [
                    {
                        'label': '反复头痛',
                        'kind': 'symptom',
                        'answerIds': [selected[0]['id']],
                        'interpretation': '原答提供新症状，病因未明。',
                        'query': 'recurrent headache associated factors',
                    }
                ]
                if selected
                else []
            }
        else:
            assessments = []
            for condition in payload['candidate']['conditions']:
                refs = []
                if condition['kind'] == 'presence':
                    bmi = [
                        row
                        for row in payload['evidence']
                        if isinstance(row['record'], dict) and row['record'].get('itemName') == 'BMI'
                    ]
                    if bmi:
                        refs = [{'evidenceId': bmi[0]['id'], 'quote': '29.3'}]
                elif condition['kind'] == 'temporality':
                    selected = [row for row in payload['answers'] if '体检前已持续一年' in row['answerText']]
                    if selected:
                        refs = [{'evidenceId': selected[0]['id'], 'quote': '体检前已持续一年'}]
                    if self.fake_personal_quote and payload['answers']:
                        refs = [{'evidenceId': payload['answers'][0]['id'], 'quote': '是否已存在此情况'}]
                assessments.append(
                    {
                        'conditionId': condition['id'],
                        'status': 'met' if refs else 'unknown',
                        'support': refs,
                        'contradicts': [],
                        'explanation': '依据当前引用判断；未提供的资料保持未知。',
                    }
                )
            output = {'conditionAssessments': assessments, 'explanation': '因素存在及时间分别判断，非个人因果证明。'}
        return {'output': output, 'searchSources': sources}


def pipeline(model=None, knowledge=None, **kwargs):
    kwargs.setdefault('evidence_policy', create_questionnaire_evidence_policy(selection=LEGACY_EVIDENCE_RULE_SELECTION))
    return QuestionnaireFactorPipeline(model=model or Model(), knowledge=knowledge or Knowledge(), **kwargs)


@pytest.mark.asyncio
async def test_full_report_is_indexed_without_sixteen_item_or_text_truncation():
    report = deepcopy(REPORT)
    report['observations'].extend({'itemName': f'检验{i}', 'resultValue': '完整值' * 400} for i in range(40))
    model = Model()
    prepared = await pipeline(model=model).prepare(reports=[report], questionnaire_id='q')
    observations = [
        row for row in prepared['evidence'] if '/observations/' in row['pointer'] and row['kind'] == 'report_record'
    ]
    assert len(observations) == 42
    assert observations[-1]['record']['resultValue'] == '完整值' * 400
    assert 'interpretation' not in observations[1]['record']
    ai_rows = [row for row in prepared['evidence'] if row['kind'] == 'ai_interpretation']
    assert ai_rows[0]['record'] == 'AI claims a definite cause'
    assert set(prepared['coverage']['analyzedEvidenceIds']) == {row['id'] for row in prepared['evidence']}
    assert all(
        not any(row['kind'] == 'ai_interpretation' for row in call[1]['evidence'])
        for call in model.calls
        if call[0] == 'match'
    )


@pytest.mark.asyncio
async def test_frozen_candidate_reassessment_preserves_sources_and_actual_answer_chain_and_resumes():
    model = Model()
    worker = pipeline(model=model)
    prepared = await worker.prepare(reports=[REPORT], questionnaire_id='q')
    candidate = prepared['candidates'][0]
    assert candidate['before']['presence'] == 'present'
    assert candidate['before']['applicability'] == 'unknown'
    condition = next(row for row in candidate['conditions'] if row['kind'] == 'temporality')
    frozen = bind_questions(
        prepared,
        QUESTIONNAIRE,
        {
            'factorBindings': [
                {'questionName': 'duration', 'candidateId': candidate['id'], 'conditionId': condition['id']},
                {'questionName': 'not_published', 'candidateId': candidate['id'], 'conditionId': condition['id']},
            ]
        },
    )
    assert len(frozen['bindings']) == 1
    assert prepared['bindings'] == []
    untouched = deepcopy(frozen)
    rows, _ = normalize_answers(QUESTIONNAIRE, {'duration': 'before'})
    snapshot = {'reports': [REPORT], 'questionnaire': QUESTIONNAIRE, 'answers': rows, 'factorPreparation': frozen}
    checkpoints = []

    async def checkpoint(value):
        checkpoints.append(deepcopy(value))

    result = await worker.evaluate(snapshot, checkpoint=checkpoint)
    related = result['targets'][0]['factors'][0]
    assert related['causalityEstablished'] is False
    assert related['answerEvidence'][0]['rawAnswer'] == 'before'
    assert related['answerEvidence'][0]['answerText'] == '体检前已持续一年'
    assert related['before']['applicability'] == 'unknown'
    assert related['after']['applicability'] == 'matched'
    assert result['sources'][0]['passages'][0]['text'] == ORIGINAL
    assert result['sources'][0]['version'] == '2026-01'
    assert result['targets'][0]['riskInterpretation'][0]['origin'] == 'factor_analysis'
    assert frozen == untouched
    assert all(not call[2] for call in model.calls)
    count = len(model.calls)
    resumed = await worker.evaluate({**snapshot, '_factorCheckpoint': checkpoints[-1]})
    assert len(model.calls) == count
    assert resumed['version'] == result['version']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'mode,inapplicable', [('empty', False), ('error', False), ('summary_only', False), ('valid', True)]
)
async def test_knowledge_failure_no_hit_no_original_or_inapplicable_must_use_aion_search(mode, inapplicable):
    model = Model(kb_inapplicable=inapplicable)
    prepared = await pipeline(model=model, knowledge=Knowledge(mode)).prepare(reports=[REPORT], questionnaire_id='q')
    search = [call for call in model.calls if call[0] == 'search']
    assert search and search[0][2] is True
    assert search[0][1] == {'query': 'elevated triglycerides risk factors'}
    assert prepared['candidates']
    assert prepared['coverage']['targetRetrieval'][0]['usedWebSearch'] is True


@pytest.mark.asyncio
async def test_model_generated_sources_do_not_count_when_aion_has_no_actual_material():
    model = Model(web_material=False)
    prepared = await pipeline(model=model, knowledge=Knowledge('empty')).prepare(reports=[REPORT], questionnaire_id='q')
    assert prepared['sources'] == []
    assert prepared['candidates'] == []
    assert prepared['coverage']['status'] == 'partial'
    assert prepared['coverage']['targetRetrieval'][0]['status'] == 'unavailable'
    assert any(
        row['reason'] == 'web_source_material_unavailable' for row in prepared['coverage']['targetRetrieval'][0]['gaps']
    )


@pytest.mark.asyncio
async def test_fabricated_medical_quote_is_rejected_in_both_retrieval_channels():
    prepared = await pipeline(model=Model(fake_quote=True)).prepare(reports=[REPORT], questionnaire_id='q')
    assert prepared['candidates'] == []
    assert prepared['coverage']['status'] == 'partial'
    assert prepared['coverage']['targetRetrieval'][0]['usedWebSearch'] is True


@pytest.mark.asyncio
async def test_blank_and_unknown_answers_do_not_become_exposure_or_timing_facts():
    worker = pipeline()
    prepared = await worker.prepare(reports=[REPORT], questionnaire_id='q')
    result = await worker.evaluate(
        {
            'reports': [REPORT],
            'questionnaire': QUESTIONNAIRE,
            'answers': {'duration': 'unknown', 'new_symptom': ''},
            'factorPreparation': prepared,
        }
    )
    candidate = result['targets'][0]['candidateFactors'][0]
    assert result['targets'][0]['factors'] == []
    assert candidate['after']['presence'] == 'present'
    assert candidate['after']['applicability'] == 'unknown'
    assert candidate['answerEvidence'] == []


@pytest.mark.asyncio
async def test_question_wording_cannot_be_cited_as_an_actual_answer():
    worker = pipeline(model=Model(fake_personal_quote=True))
    prepared = await worker.prepare(reports=[REPORT], questionnaire_id='q')
    with pytest.raises(FactorContractError, match='absent from'):
        await worker.evaluate(
            {
                'reports': [REPORT],
                'questionnaire': QUESTIONNAIRE,
                'answers': {'duration': 'before'},
                'factorPreparation': prepared,
            }
        )


@pytest.mark.asyncio
async def test_no_report_preparation_skips_model_and_meaningful_answer_adds_post_answer_target():
    model = Model()
    worker = pipeline(model=model)
    prepared = await worker.prepare(reports=[], questionnaire_id='q')
    assert model.calls == []
    result = await worker.evaluate(
        {
            'reports': [],
            'questionnaire': QUESTIONNAIRE,
            'answers': {'new_symptom': '最近反复头痛'},
            'factorPreparation': prepared,
        }
    )
    target = result['targets'][0]
    assert target['label'] == '反复头痛'
    assert target['phase'] == 'post_answer'
    assert target['answerEvidence'][0]['answer'] == '最近反复头痛'
    assert all(
        candidate['phase'] == 'post_answer' and 'before' not in candidate for candidate in target['candidateFactors']
    )
    assert prepared['targets'] == []


@pytest.mark.asyncio
async def test_unavailable_preparation_is_rebuilt_and_never_mislabeled_as_pre_answer():
    result = await pipeline().evaluate(
        {
            'reports': [REPORT],
            'questionnaire': QUESTIONNAIRE,
            'answers': {},
            'factorPreparation': {'version': 'factor-preparation-unavailable-v1', 'status': 'unavailable'},
        }
    )
    assert all(target['phase'] == 'post_answer' for target in result['targets'])
    assert all('before' not in candidate for target in result['targets'] for candidate in target['candidateFactors'])


@pytest.mark.asyncio
async def test_budget_limit_preserves_all_records_and_marks_unprocessed_coverage():
    report = {'observations': [{'itemName': '长记录', 'resultValue': 'x' * 6000}]}
    model = Model()
    prepared = await pipeline(model=model, batch_chars=1000).prepare(reports=[report], questionnaire_id='q')
    assert prepared['evidence'][0]['record']['resultValue'] == 'x' * 6000
    assert prepared['coverage']['status'] == 'partial'
    assert prepared['coverage']['unprocessedEvidenceIds'] == [prepared['evidence'][0]['id']]
    assert model.calls == []


def test_report_index_keeps_original_flags_separate_from_ai_interpretation():
    report = {
        'observations': [
            {
                'itemName': 'HbA1c',
                'resultValue': '5.75',
                'flag': 'normal',
                'referenceRange': '4-6',
                'riskAssessment': 'different guideline category',
            }
        ]
    }
    rows, _ = index_reports([report])
    original = next(row for row in rows if row['kind'] == 'report_record')
    assert original['record']['flag'] == 'normal'
    assert original['record']['referenceRange'] == '4-6'
    assert 'riskAssessment' not in original['record']
    assert next(row for row in rows if row['kind'] == 'ai_interpretation')['record'] == 'different guideline category'


@pytest.mark.asyncio
async def test_adapter_enables_shared_gateway_search_and_uses_only_transport_material():
    class Gateway:
        request = None

        async def complete(self, request):
            self.request = request
            return SimpleNamespace(content='{"sources":[{"url":"https://invented.invalid"}]}', search_sources=())

    gateway = Gateway()
    result = await QuestionnaireFactorModel(llm_gateway=gateway).run(
        'search', {'query': 'medical topic'}, web_search=True
    )
    assert gateway.request.effective_web_search is True
    assert gateway.request.search_query == 'medical topic'
    assert len(gateway.request.content) > 128
    assert gateway.request.profile.context_policy.value == 'stateless'
    assert result == {'output': {}, 'searchSources': []}


@pytest.mark.parametrize('query', ['', '   ', None, 'x' * 129])
@pytest.mark.asyncio
async def test_search_adapter_rejects_invalid_queries_before_calling_gateway(query):
    class NoCalls:
        async def complete(self, request):
            pytest.fail('invalid search query reached the gateway')

    with pytest.raises(ValueError, match='1 to 128'):
        await QuestionnaireFactorModel(llm_gateway=NoCalls()).run('search', {'query': query}, web_search=True)


@pytest.mark.parametrize('answers', [False, True])
@pytest.mark.parametrize('length', [1, 128, 129])
def test_factor_targets_share_search_query_boundary_before_and_after_answers(answers, length):
    raw = {
        'label': 'target',
        'kind': 'indicator',
        'answerIds' if answers else 'evidenceIds': ['e1'],
        'interpretation': 'risk interpretation',
        'query': 'x' * length,
    }
    if length > 128:
        with pytest.raises(FactorContractError, match='128'):
            QuestionnaireFactorPipeline._target(
                raw, {'e1'}, phase='post_answer' if answers else 'pre_answer', answers=answers
            )
    else:
        result = QuestionnaireFactorPipeline._target(
            raw, {'e1'}, phase='post_answer' if answers else 'pre_answer', answers=answers
        )
        assert result['query'] == raw['query']


@pytest.mark.asyncio
async def test_actual_aion_search_material_reaches_questionnaire_candidate_citation():
    import aion
    import httpx

    from agents.llm.aion_gateway import AionLLMGateway

    requests, clients = [], []

    def respond(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path == '/v1/tools/search':
            assert body['query'] == 'elevated triglycerides risk factors'
            return httpx.Response(
                200,
                json={
                    'results': [
                        {'title': 'Actual retrieved source', 'url': 'https://example.org/actual', 'content': ''}
                    ]
                },
            )
        if request.url.path == '/v1/tools/fetch':
            return httpx.Response(200, json={'content': ORIGINAL})
        assert request.url.path == '/chat/completions'
        assert ORIGINAL in body['messages'][-1]['content']
        assert '3.34' not in body['messages'][-1]['content']
        return httpx.Response(
            200,
            json={
                'id': 'test',
                'created': 0,
                'model': 'deepseek-flash',
                'object': 'chat.completion',
                'choices': [
                    {
                        'index': 0,
                        'message': {'role': 'assistant', 'content': '{"sources":[{"url":"https://invented.invalid"}]}'},
                        'finish_reason': 'stop',
                    }
                ],
            },
        )

    def factory(provider, **kwargs):
        client = aion.create_chat_client(provider, **kwargs)
        original = client.async_client
        client.async_client = original.with_options(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), max_retries=0
        )
        clients.append((client, original))
        return client

    gateway = AionLLMGateway(
        provider='deepseek', api_key='unit-test-key', default_model='deepseek-flash', client_factory=factory
    )
    adapter = QuestionnaireFactorModel(llm_gateway=gateway)

    class ModelWithActualSearch(Model):
        async def run(self, stage, payload, *, web_search=False):
            if stage == 'search':
                return await adapter.run(stage, payload, web_search=web_search)
            return await super().run(stage, payload, web_search=web_search)

    try:
        worker = pipeline(model=ModelWithActualSearch(), knowledge=Knowledge('empty'))
        prepared = await worker.prepare(reports=[REPORT], questionnaire_id='q')
        assert [path for path, _ in requests] == ['/v1/tools/search', '/v1/tools/fetch', '/chat/completions']
        source = prepared['sources'][0]
        assert source['passages'][0]['text'] == ORIGINAL
        assert source['passages'][0]['locator']['excerpt_origin'] == 'fetch'
        assert source['sourceId'] == '1'
        assert prepared['candidates'][0]['knowledgeEvidence'][0]['sourceId'] == source['id']
        trace = prepared['searchTraces'][0]['trace']
        assert [row['kind'] for row in trace['operations']] == ['search', 'fetch']
        assert trace['sources'][0]['excerpt'] == ORIGINAL
        assert 'invented.invalid' not in json.dumps(prepared)
        result = await worker.evaluate(
            {
                'reports': [REPORT],
                'questionnaire': QUESTIONNAIRE,
                'answers': {},
                'factorPreparation': prepared,
                'questionnaireId': 'q',
            }
        )
        assert 'searchTraces' not in result
    finally:
        for client, original in clients:
            await client.async_client.close()
            await original.close()
            client.client.close()


def test_schema_aware_index_never_uses_summary_metadata_missing_result_or_microbiome_as_fact():
    bacteria = {'itemName': '菌种甲', 'resultValue': '0.2', 'unit': '%'}
    report = {
        'status': 'abnormal',
        'reportType': 'mixed',
        'aiSummary': '模型猜测患病',
        'subject': {'gender': '女', 'age': 40, 'name': '不索引姓名'},
        'observations': [
            deepcopy(bacteria),
            {'itemName': '项目缺失', 'resultValue': None},
            {'itemName': '真实血糖', 'resultValue': '5.3'},
        ],
        'unresolvedItems': [{'itemName': '未提取项目', 'missingFields': ['resultValue']}],
        'sections': [
            {'reportType': 'microbiome', 'observations': [deepcopy(bacteria)]},
            {'reportType': 'imaging', 'expansion': {'findings': '原文所见', 'diagnosis': '原文结论'}},
        ],
    }
    rows, _ = index_reports([report])
    facts = [row for row in rows if row['kind'] == 'report_record']
    assert [row['record'] for row in facts] == [
        {'itemName': '真实血糖', 'resultValue': '5.3'},
        '原文所见',
        '原文结论',
    ]
    assert next(row for row in rows if row['pointer'] == '/aiSummary')['kind'] == 'ai_interpretation'
    assert next(row for row in rows if row['pointer'] == '/status')['kind'] == 'nonclinical_metadata'
    assert next(row for row in rows if row['pointer'] == '/observations/1')['kind'] == 'unresolved_report'
    assert len([row for row in rows if row['kind'] == 'report_background']) == 2
    assert not any(row['pointer'] == '/subject/name' for row in rows)


@pytest.mark.asyncio
async def test_aion_target_failure_keeps_partial_preparation_and_original_report_interpretation():
    class UnavailableSearch(Model):
        async def run(self, stage, payload, *, web_search=False):
            if stage == 'search':
                raise RuntimeError('offline AION search outage')
            return await super().run(stage, payload, web_search=web_search)

    worker = pipeline(model=UnavailableSearch(), knowledge=Knowledge('empty'))
    prepared = await worker.prepare(reports=[{**REPORT, 'aiSummary': '原有AI解读'}], questionnaire_id='q')
    assert prepared['coverage']['status'] == 'partial'
    assert any(gap['reason'] == 'web_search_failed' for gap in prepared['coverage']['targetRetrieval'][0]['gaps'])
    result = await worker.evaluate(
        {
            'reports': [{**REPORT, 'aiSummary': '原有AI解读'}],
            'questionnaire': QUESTIONNAIRE,
            'answers': {},
            'factorPreparation': prepared,
        }
    )
    assert result['targets'][0]['reportEvidence']
    assert any(
        row['record'] == '原有AI解读' and row['origin'] == 'report_interpretation'
        for row in result['reportInterpretations']
    )


@pytest.mark.asyncio
async def test_search_failure_trace_is_internal_and_does_not_become_patient_or_medical_evidence():
    from agents.errors import AionGatewayError

    trace = {
        'query': 'elevated triglycerides risk factors',
        'sources': [],
        'operations': [
            {
                'kind': 'search',
                'target': 'elevated triglycerides risk factors',
                'started_at': '2026-11-07T00:00:00Z',
                'status': 'failed',
                'status_code': 503,
                'error': 'InternalServerError',
            }
        ],
    }

    class FailedSearch(Model):
        async def run(self, stage, payload, *, web_search=False):
            if stage == 'search':
                error = AionGatewayError('search failed')
                error.search_trace = deepcopy(trace)
                error.search_reason = 'request_failed'
                raise error
            return await super().run(stage, payload, web_search=web_search)

    worker = pipeline(model=FailedSearch(), knowledge=Knowledge('empty'))
    prepared = await worker.prepare(reports=[REPORT], questionnaire_id='q')
    assert prepared['searchTraces'][0]['trace'] == trace
    assert prepared['searchTraces'][0]['reason'] == 'request_failed'
    assert prepared['sources'] == []
    assert prepared['candidates'] == []
    result = await worker.evaluate(
        {'reports': [REPORT], 'questionnaire': QUESTIONNAIRE, 'answers': {}, 'factorPreparation': prepared}
    )
    assert 'searchTraces' not in result
    assert 'InternalServerError' not in json.dumps(result)
    assert result['coverage']['status'] == 'partial'
    checkpoints = []
    await worker.evaluate(
        {'reports': [REPORT], 'questionnaire': QUESTIONNAIRE, 'answers': {}}, checkpoint=checkpoints.append
    )
    rebuilt = checkpoints[-1]['preparation']
    assert rebuilt['phase'] == 'post_answer'
    assert rebuilt['searchTraces'][0]['phase'] == 'post_answer'
    assert rebuilt['searchTraces'][0]['trace'] == trace


@pytest.mark.parametrize(
    'kind,value,quote',
    [
        ('answer', '我从不喝含糖饮料', '喝含糖饮料'),
        ('answer', '运动后没有出现胸痛', '出现胸痛'),
        ('report_record', '1800', '800'),
    ],
)
def test_personal_quote_cannot_remove_negation_or_slice_numeric_value(kind, value, quote):
    material = {
        'id': 'personal',
        'kind': kind,
        'record': {'resultValue': value},
        'answerText': value,
        'answerQuoteUnits': [value],
    }
    candidate = {'conditions': [{'id': 'condition'}]}
    output = {
        'conditionAssessments': [
            {
                'conditionId': 'condition',
                'status': 'met',
                'support': [{'evidenceId': 'personal', 'quote': quote}],
                'contradicts': [],
                'explanation': '失真摘录',
            }
        ]
    }
    with pytest.raises(FactorContractError, match='complete reported values'):
        QuestionnaireFactorPipeline._validate_match(
            output,
            candidate,
            [material] if kind == 'report_record' else [],
            [material] if kind == 'answer' else [],
        )


def test_met_with_real_counterevidence_is_conflicting_not_accepted_as_met():
    candidate = {'conditions': [{'id': 'condition'}]}
    materials = [
        {'id': 'a', 'kind': 'answer', 'answerText': '从不运动'},
        {'id': 'b', 'kind': 'answer', 'answerText': '每天运动一小时'},
    ]
    result = QuestionnaireFactorPipeline._validate_match(
        {
            'conditionAssessments': [
                {
                    'conditionId': 'condition',
                    'status': 'met',
                    'support': [{'evidenceId': 'a', 'quote': '从不运动'}],
                    'contradicts': [{'evidenceId': 'b', 'quote': '每天运动一小时'}],
                    'explanation': '信息相互冲突',
                }
            ]
        },
        candidate,
        [],
        materials,
    )
    assert result[0]['status'] == 'conflicting'


@pytest.mark.asyncio
async def test_ai_interpretation_cannot_be_a_target_factual_reference():
    class CiteSummary(Model):
        async def run(self, stage, payload, *, web_search=False):
            result = await super().run(stage, payload, web_search=web_search)
            if stage == 'targets':
                result['output']['targets'][0]['evidenceIds'] = [
                    next(row['id'] for row in payload['evidence'] if row['kind'] == 'ai_interpretation')
                ]
            return result

    with pytest.raises(FactorContractError, match='absent evidence'):
        await pipeline(model=CiteSummary()).prepare(reports=[REPORT], questionnaire_id='q')


def test_web_material_requires_a_usable_source_url_even_with_provider_id():
    assert source_material({'id': '1', 'url': 'https://[invalid', 'text': ORIGINAL}, channel='web_search') is None
    assert source_material({'id': '1', 'text': ORIGINAL}, channel='web_search') is None


@pytest.mark.asyncio
async def test_candidate_adapter_only_exposes_existing_lifestyle_question_keys():
    class Gateway:
        request = None

        async def complete(self, request):
            self.request = request
            return SimpleNamespace(
                content='{"candidates":[{"conditions":[{"lifestyleKey":"sweet_intake"},'
                '{"lifestyleKey":"made_up_question"}]}]}',
                search_sources=(),
            )

    gateway = Gateway()
    result = await QuestionnaireFactorModel(llm_gateway=gateway).run('candidates', {'passages': []})
    assert 'lifestyleTopics' in gateway.request.content
    conditions = result['output']['candidates'][0]['conditions']
    assert conditions == [{'lifestyleKey': 'sweet_intake'}, {}]


@pytest.mark.asyncio
async def test_explicit_subject_background_is_available_for_applicability_but_not_a_disease_target():
    class BackgroundModel(Model):
        async def run(self, stage, payload, *, web_search=False):
            result = await super().run(stage, payload, web_search=web_search)
            if stage == 'candidates':
                result['output']['candidates'][0]['conditions'].append(
                    {'id': 'adult', 'label': '适用成人', 'kind': 'applicability', 'required': True}
                )
            if stage == 'match':
                age = next(
                    (r for r in payload['evidence'] if r['kind'] == 'report_background' and r['label'] == 'age'), None
                )
                if age:
                    row = next(
                        r for r in result['output']['conditionAssessments'] if r['conditionId'].endswith('_adult')
                    )
                    row.update(status='met', support=[{'evidenceId': age['id'], 'quote': '40'}])
            return result

    report = {**REPORT, 'subject': {'age': 40, 'gender': '女'}}
    prepared = await pipeline(model=BackgroundModel()).prepare(reports=[report], questionnaire_id='q')
    candidate = prepared['candidates'][0]
    assessed = next(r for r in candidate['before']['conditionAssessments'] if r['conditionId'].endswith('_adult'))
    assert assessed['status'] == 'met'
    assert assessed['evidenceRefs']
    background_ids = {row['id'] for row in prepared['evidence'] if row['kind'] == 'report_background'}
    assert set(assessed['evidenceRefs']).issubset(background_ids)
    assert not background_ids.intersection(prepared['targets'][0]['reportEvidenceIds'])


def test_wrapper_gender_source_preserves_explicit_original_proof_only():
    source = {'value': 'female', 'quote': '性别：女', 'pointer': '/docmind_result/text', 'start': 10, 'end': 14}
    rows, _ = index_reports([{'fileIndex': 2, 'interpretation': REPORT, 'genderEvidence': [source]}])
    row = next(r for r in rows if r['pointer'] == '/genderEvidence/0')
    assert row['kind'] == 'report_background'
    assert row['record']['evidence'] == source
    assert row['reportIndex'] == 2
    missing, _ = index_reports([{'interpretation': REPORT, 'genderEvidence': [{'value': 'female', 'quote': '王女士'}]}])
    assert not any(r['kind'] == 'report_background' for r in missing)


@pytest.mark.asyncio
async def test_parallel_targets_share_a_bounded_knowledge_retrieval_limit():
    class CountingKnowledge(Knowledge):
        active = 0
        maximum = 0

        async def retrieve(self, query):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            try:
                await asyncio.sleep(0)
                return await super().retrieve(query)
            finally:
                self.active -= 1

    knowledge = CountingKnowledge()
    worker = pipeline(knowledge=knowledge, concurrency=3)
    targets = [
        {'id': f't{i}', 'label': '甘油三酯', 'kind': 'indicator', 'query': 'triglycerides risk factors'}
        for i in range(10)
    ]
    await asyncio.gather(*(worker._research(target, phase='pre_answer') for target in targets))
    assert len(knowledge.calls) == 20
    assert knowledge.maximum <= 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'question_name,question_type,answer,label,kind,accepted',
    [
        ('family_history', 'checkbox', ['糖尿病'], '糖尿病', 'disease', False),
        ('health_goals', 'text', '想了解糖尿病', '糖尿病', 'disease', False),
        ('health_goals', 'text', '糖尿病', '糖尿病', 'disease', False),
        ('health_goals', 'text', '父亲患有糖尿病', '糖尿病', 'disease', False),
        ('health_goals', 'text', '我最近容易疲劳', '疲劳', 'symptom', True),
        ('medical_history', 'checkbox', ['糖尿病'], '糖尿病', 'disease', True),
        ('medical_history', 'checkbox', ['高血压'], '糖尿病', 'disease', False),
        ('new_symptom', 'text', '医生确诊我患有糖尿病', '糖尿病', 'disease', True),
        ('new_symptom', 'text', '我尚未确诊糖尿病', '糖尿病', 'disease', False),
        ('new_symptom', 'text', '糖尿病', '糖尿病', 'disease', False),
    ],
)
async def test_answer_targets_require_self_assertion_and_keep_goals_family_and_symptoms_distinct(
    question_name,
    question_type,
    answer,
    label,
    kind,
    accepted,
):
    class ProposedTargetModel(Model):
        async def run(self, stage, payload, *, web_search=False):
            if stage == 'answer_targets':
                return {
                    'output': {
                        'targets': [
                            {
                                'label': label,
                                'kind': kind,
                                'answerIds': [payload['answers'][0]['id']],
                                'interpretation': '模型提出的目标，需校验主体和原答。',
                                'query': 'general medical factors',
                            }
                        ]
                    },
                    'searchSources': [],
                }
            return await super().run(stage, payload, web_search=web_search)

    question = {'name': question_name, 'type': question_type, 'title': '本次信息'}
    if question_type == 'checkbox':
        question['choices'] = [{'value': '糖尿病'}, {'value': '高血压'}]
    questionnaire = {'title': '本次问卷', 'pages': [{'name': 'p', 'elements': [question]}]}
    model = ProposedTargetModel()
    worker = pipeline(model=model)
    prepared = await worker.prepare(reports=[], questionnaire_id='q')
    result = await worker.evaluate(
        {
            'reports': [],
            'questionnaire': questionnaire,
            'answers': {question_name: answer},
            'factorPreparation': prepared,
        }
    )
    assert bool(result['targets']) is accepted
    if accepted:
        assert result['targets'][0]['label'] == label
        assert result['targets'][0]['answerEvidence'][0]['rawAnswer'] == answer
    else:
        assert not any(call[0] in {'search', 'candidates'} for call in model.calls)
        assert any(gap['reason'] == 'answer_target_not_self_asserted' for gap in result['coverage']['gaps'])
