"""Five-step evidence uses original passages and complete personal quote units."""

import asyncio
from copy import deepcopy

import pytest

from agents.knowledge.errors import KnowledgeRetrievalError
from agents.knowledge.types import KnowledgeDocument
from agents.knowledge.types import KnowledgeSource
from modules.health_analysis.contracts import AnalysisCapacityError
from modules.health_analysis.contracts import AnalysisTimeoutError
from modules.health_analysis.factors import FactorAnalysis
from modules.health_analysis.factors import HealthAnalysisGenerationError
from modules.health_analysis.factors import medical_citations
from modules.health_analysis.factors import partition
from modules.health_analysis.factors import personal_citations
from modules.health_analysis.factors import safe_medical_query
from modules.health_analysis.factors import source_material
from modules.health_analysis.surveys import decode_surveys

MATERIAL = {'id': 'm1', 'kind': 'report', 'content': {'resultValue': 0, 'record': '近期未出现合成情况'}}


class Knowledge:
    enabled = True

    def __init__(self, *, mode='available'):
        self.mode, self.queries = mode, []

    async def retrieve(self, query):
        self.queries.append(query)
        if self.mode == 'failure':
            raise KnowledgeRetrievalError(1, 1)
        if self.mode == 'program_error':
            raise TypeError('synthetic program bug')
        if self.mode == 'empty':
            return ()
        return (
            KnowledgeDocument(
                source=query.source,
                title='合成证据',
                content='模型摘要不属于原始证据',
                metadata={
                    'id': 'synthetic-source',
                    'version': 'v1',
                    'sourceTexts': []
                    if self.mode == 'summary_only'
                    else [
                        {
                            'kind': 'guideBlock',
                            'text': '合成原文：某生活因素与合成目标存在一般关联，需满足时间及人群条件。',
                        }
                    ],
                },
            ),
        )


class AnalysisStages:
    def __init__(self):
        self.calls = []

    async def __call__(self, stage, payload):
        self.calls.append((stage, deepcopy(payload)))
        if stage == 'targets':
            return {
                'coveredMaterialIds': [m['id'] for m in payload['materials']],
                'targets': [
                    {
                        'label': '合成关注目标',
                        'kind': 'indicator',
                        'query': '合成指标 一般关系',
                        'interpretation': '本次检查记录需结合时间理解，不作新诊断。',
                        'personalEvidence': [{'evidenceId': 'm1', 'quote': '0'}],
                    }
                ],
            }
        if stage == 'research':
            source = payload['sources'][0]
            return {
                'applicableSourceIds': [source['id']],
                'candidates': [
                    {
                        'concept': '合成生活因素',
                        'relationType': 'possible_contributor',
                        'materialSupportsRelation': True,
                        'applicabilityReviewed': True,
                        'knowledgeEvidence': [
                            {
                                'sourceId': source['id'],
                                'passageId': source['passages'][0]['id'],
                                'quote': '存在一般关联',
                            }
                        ],
                        'supportExplanation': '原文只支持条件性一般关系。',
                        'conditions': [
                            {'id': 'p', 'kind': 'presence', 'label': '合成个人条件'},
                            {'id': 't', 'kind': 'temporality', 'label': '时间是否适合'},
                        ],
                    }
                ],
            }
        if stage == 'match':
            return {
                'conditions': [
                    {
                        'id': 'p',
                        'status': 'not_met',
                        'support': [],
                        'contradicts': [{'evidenceId': 'm1', 'quote': '近期未出现合成情况'}],
                        'explanation': '实际否认资料',
                    },
                    {'id': 't', 'status': 'unknown', 'support': [], 'contradicts': [], 'explanation': '时间资料不足'},
                ]
            }
        raise AssertionError('unexpected stage')


@pytest.mark.asyncio
async def test_full_factors_keep_zero_counterevidence_original_passage_and_unknown_time_separate():
    stages, knowledge = AnalysisStages(), Knowledge()
    output = await FactorAnalysis(knowledge).analyze([MATERIAL], stages)
    assert output['coveredMaterialIds'] == ['m1']
    assert len(output['targets']) == len(output['candidates']) == 1
    candidate = output['candidates'][0]
    assert candidate['assessment']['conditions'][0]['status'] == 'not_met'
    assert candidate['assessment']['conditions'][1]['status'] == 'unknown'
    assert candidate['assessment']['supported'] is False
    assert candidate['causalityEstablished'] is False
    assert candidate['knowledgeEvidence'][0]['sourceVersion'] == 'v1'
    assert all(query.include_source_text for query in knowledge.queries)
    assert all(query.query == '合成指标 一般关系' for query in knowledge.queries)
    assert [stage for stage, _ in stages.calls] == ['targets', 'research', 'match']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'mode,status', [('empty', 'empty'), ('failure', 'failure'), ('summary_only', 'no_original_text')]
)
async def test_knowledge_empty_failure_and_summary_only_have_distinct_states_without_fabricated_sources(mode, status):
    stages = AnalysisStages()
    output = await FactorAnalysis(Knowledge(mode=mode)).analyze([MATERIAL], stages)
    assert {row['status'] for row in output['retrieval']} == {status}
    assert output['candidates'] == output['sources'] == []
    assert [stage for stage, _ in stages.calls] == ['targets']


@pytest.mark.asyncio
async def test_disabled_knowledge_is_distinct_from_an_empty_enabled_library():
    knowledge = Knowledge()
    knowledge.enabled = False
    output = await FactorAnalysis(knowledge).analyze([MATERIAL], AnalysisStages())
    assert output['retrieval'][0]['status'] == 'disabled'
    assert knowledge.queries == []


@pytest.mark.asyncio
async def test_inapplicable_actual_material_is_recorded_and_cannot_create_a_candidate():
    stages = AnalysisStages()

    async def call(stage, payload):
        if stage == 'research':
            return {'applicableSourceIds': [], 'candidates': []}
        return await stages(stage, payload)

    output = await FactorAnalysis(Knowledge()).analyze([MATERIAL], call)
    assert output['retrieval'][-1]['status'] == 'inapplicable'
    assert output['sources'] and not output['candidates']


@pytest.mark.parametrize('quote', ['出现合成情况', '近期出现合成情况', '近期未出现', '0.0'])
def test_personal_quotes_cannot_remove_negation_time_or_slice_numbers(quote):
    with pytest.raises(HealthAnalysisGenerationError):
        personal_citations([{'evidenceId': 'm1', 'quote': quote}], {'m1': MATERIAL})


def test_real_unknown_can_be_collected_without_supporting_a_positive_condition():
    with pytest.raises(HealthAnalysisGenerationError):
        personal_citations(
            [{'evidenceId': 'm1', 'quote': '不清楚'}], {'m1': {'content': '不清楚', 'status': 'unknown'}}
        )


def test_fabricated_medical_quotes_and_ai_summary_cannot_pass_citation_validation():
    document = KnowledgeDocument(
        source=KnowledgeSource.GUIDE,
        title='合成指南',
        content='AI摘要',
        metadata={'id': 's', 'sourceTexts': [{'kind': 'guideBlock', 'text': '真实原文'}]},
    )
    source = source_material(document)
    with pytest.raises(HealthAnalysisGenerationError):
        medical_citations([{'sourceId': source['id'], 'passageId': 'p1', 'quote': 'AI摘要'}], {source['id']: source})


def test_unhashable_model_reference_values_are_rejected_without_exposing_materials():
    for invalid in ([], {}):
        with pytest.raises(HealthAnalysisGenerationError):
            personal_citations([{'evidenceId': invalid, 'quote': '0'}], {'m1': MATERIAL})
        with pytest.raises(HealthAnalysisGenerationError):
            medical_citations([{'sourceId': invalid, 'passageId': 'p1', 'quote': '合成'}], {})


@pytest.mark.asyncio
async def test_invalid_single_candidate_is_filtered_without_losing_valid_candidate_or_other_materials():
    stages = AnalysisStages()

    async def call(stage, payload):
        output = await stages(stage, payload)
        if stage == 'research':
            invalid = deepcopy(output['candidates'][0])
            invalid['knowledgeEvidence'][0]['quote'] = '模型伪造引文'
            output['candidates'].insert(0, invalid)
        return output

    output = await FactorAnalysis(Knowledge()).analyze([MATERIAL], call)
    assert len(output['candidates']) == 1
    assert output['filters'] == [{'stage': 'research', 'reason': 'invalid_candidate'}]


def test_personal_matching_retains_both_directions_as_conflict_without_asserting_causality():
    candidate = {'conditions': [{'id': 'p'}]}
    output = FactorAnalysis._match(
        {
            'conditions': [
                {
                    'id': 'p',
                    'status': 'met',
                    'support': [{'evidenceId': 'a', 'quote': '合成肯定'}],
                    'contradicts': [{'evidenceId': 'b', 'quote': '合成否认'}],
                    'explanation': '时间主体已核对但仍冲突',
                }
            ]
        },
        candidate,
        {'a': {'content': '合成肯定'}, 'b': {'content': '合成否认'}},
    )
    assert output['conditions'][0]['status'] == 'conflicting'
    assert not output['supported'] and not output['causalityEstablished']


@pytest.mark.asyncio
async def test_catalogue_discovery_omission_is_an_error_instead_of_false_complete_analysis():
    async def omitted(stage, payload):
        return {'coveredMaterialIds': [], 'targets': []}

    with pytest.raises(HealthAnalysisGenerationError, match='omitted'):
        await FactorAnalysis().analyze([MATERIAL], omitted)


def test_batches_preserve_complete_values_and_explicitly_reject_over_capacity():
    rows = [{'id': str(i), 'content': 'x' * 20} for i in range(3)]
    assert [r for group in partition(rows, max_chars=100, max_batches=3) for r in group] == rows
    with pytest.raises(AnalysisCapacityError):
        partition(rows, max_chars=10, max_batches=3)
    with pytest.raises(AnalysisCapacityError):
        partition(rows, max_chars=60, max_batches=1)


@pytest.mark.asyncio
async def test_expected_knowledge_outages_do_not_swallow_programming_errors_or_cancelled_execution():
    with pytest.raises(TypeError):
        await FactorAnalysis(Knowledge(mode='program_error')).analyze([MATERIAL], AnalysisStages())

    class Cancelled(Knowledge):
        async def retrieve(self, query):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await FactorAnalysis(Cancelled()).analyze([MATERIAL], AnalysisStages())


@pytest.mark.asyncio
async def test_knowledge_calls_share_the_supplied_semaphore_across_requests():
    class Metered(Knowledge):
        active = maximum = 0

        async def retrieve(self, query):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            try:
                await asyncio.sleep(0)
                return await super().retrieve(query)
            finally:
                self.active -= 1

    knowledge, semaphore = Metered(), asyncio.Semaphore(1)
    await asyncio.gather(
        *(FactorAnalysis(knowledge, semaphore=semaphore).analyze([MATERIAL], AnalysisStages()) for _ in range(3))
    )
    assert knowledge.maximum == 1 and knowledge.active == 0


@pytest.mark.asyncio
async def test_expired_deadline_prevents_knowledge_dispatch():
    knowledge = Knowledge()
    with pytest.raises(AnalysisTimeoutError):
        await FactorAnalysis(knowledge)._retrieve(
            {'id': 't', 'query': '合成查询'}, deadline_at=asyncio.get_running_loop().time() - 1
        )
    assert knowledge.queries == []


@pytest.mark.parametrize(
    'query',
    [
        'https://example.invalid/person/report 糖尿病',
        'www.example.com 风险',
        'patient@example.invalid 运动',
        '13812345678 合成指标',
        '+86 138-1234-5678 合成指标',
    ],
)
def test_contact_or_url_in_medical_query_is_rejected_without_echoing_value(query):
    with pytest.raises(HealthAnalysisGenerationError) as error:
        safe_medical_query(query, [MATERIAL])
    assert query not in str(error.value)


@pytest.mark.parametrize(
    'field',
    ['userId', 'groupId', 'senderId', 'sendID', 'serverMsgID', 'clientMsgID', 'documentId', 'subject_id', 'patientId'],
)
def test_known_material_identity_fields_do_not_cross_into_knowledge_queries(field):
    material = {**deepcopy(MATERIAL), 'metadata': {field: 'synthetic-private-identity-9182'}}
    with pytest.raises(HealthAnalysisGenerationError):
        safe_medical_query('synthetic-private-identity-9182 合成指标', [material])


@pytest.mark.parametrize(
    'material',
    [
        {'id': 'c', 'kind': 'chat', 'content': '我最近每晚只睡三个小时并且担心工作'},
        {'id': 'a', 'kind': 'answer', 'content': '最近三周平均每天饮用两瓶合成饮料'},
        {'id': 'a', 'kind': 'answer', 'content': {'readableAnswer': '每周四天进行合成活动'}},
    ],
)
def test_complete_chat_and_readable_answer_are_not_reused_as_medical_search_text(material):
    original = material['content'] if isinstance(material['content'], str) else material['content']['readableAnswer']
    with pytest.raises(HealthAnalysisGenerationError):
        safe_medical_query(original + ' 风险因素', [material])


def test_general_medical_numbers_are_allowed_without_banning_all_numeric_identity_substrings():
    assert safe_medical_query('2型糖尿病 维生素B12 风险因素', [MATERIAL], exclusion_values=['2']) == (
        '2型糖尿病 维生素B12 风险因素'
    )
    with pytest.raises(HealthAnalysisGenerationError):
        safe_medical_query('用户123456 合成指标', [MATERIAL], exclusion_values=['123456'])


@pytest.mark.asyncio
async def test_rejected_query_is_a_local_invalid_target_with_no_knowledge_call_or_raw_diagnostic_query():
    stages, knowledge = AnalysisStages(), Knowledge()
    private = 'synthetic-private-scope-8123'

    async def call(stage, payload):
        output = await stages(stage, payload)
        if stage == 'targets':
            output['targets'][0]['query'] = private + ' 合成指标'
        return output

    result = await FactorAnalysis(knowledge).analyze([MATERIAL], call, query_exclusion_values=[private])
    assert knowledge.queries == []
    assert result['targets'] == []
    assert result['filters'] == [{'stage': 'targets', 'reason': 'invalid_target'}]
    assert private not in str(result)


def actual_answer_material(values):
    schema = {
        'title': '合成既往情况',
        'pages': [
            {
                'name': 'p',
                'elements': [
                    {
                        'name': 'history',
                        'type': 'checkbox',
                        'title': '医生曾明确诊断以下哪些疾病？',
                        'choices': [
                            {'value': 'dm_code', 'text': '糖尿病'},
                            {'value': 'ht_code', 'text': '高血压'},
                            {'value': 'unknown', 'text': '不清楚'},
                        ],
                        'showNoneItem': True,
                        'noneText': '以上均无',
                    }
                ],
            }
        ],
    }
    row = decode_surveys([{'schema': schema, 'answer': {'history': values}}]).personal_answers[0]
    return {'id': 'actual-answer', 'kind': 'answer', 'content': row, 'status': row['status']}


def test_none_answer_cannot_cite_unselected_diagnoses_question_title_or_raw_codes_as_personal_facts():
    answer = actual_answer_material(['none'])
    for quote in ('糖尿病', '高血压', '医生曾明确诊断以下哪些疾病？', 'none', 'dm_code'):
        with pytest.raises(HealthAnalysisGenerationError):
            personal_citations([{'evidenceId': answer['id'], 'quote': quote}], {answer['id']: answer})
    assert personal_citations([{'evidenceId': answer['id'], 'quote': '以上均无'}], {answer['id']: answer})


def test_only_selected_readable_answer_units_and_complete_combination_can_support_personal_matching():
    answer = actual_answer_material(['ht_code', 'dm_code'])
    for quote in ('糖尿病', '高血压', '糖尿病\n高血压'):
        assert personal_citations([{'evidenceId': answer['id'], 'quote': quote}], {answer['id']: answer})
    for quote in ('dm_code', 'ht_code', '不清楚', '糖尿'):
        with pytest.raises(HealthAnalysisGenerationError):
            personal_citations([{'evidenceId': answer['id'], 'quote': quote}], {answer['id']: answer})
    unknown = actual_answer_material(['unknown'])
    with pytest.raises(HealthAnalysisGenerationError):
        personal_citations([{'evidenceId': unknown['id'], 'quote': '不清楚'}], {unknown['id']: unknown})


def test_report_name_reference_units_dates_and_identity_cannot_establish_a_personal_result():
    material = {
        'id': 'r',
        'kind': 'report',
        'content': {
            'itemName': '糖尿病',
            'resultValue': '未见相关诊断',
            'unit': '合成单位',
            'referenceRange': '合成正常区间',
            'observedAt': '2026-11-01',
            'metadata': {'patientName': '合成姓名'},
        },
    }
    for quote in ('糖尿病', '合成单位', '合成正常区间', '2026-11-01', '合成姓名', '相关诊断'):
        with pytest.raises(HealthAnalysisGenerationError):
            personal_citations([{'evidenceId': 'r', 'quote': quote}], {'r': material})
    assert personal_citations([{'evidenceId': 'r', 'quote': '未见相关诊断'}], {'r': material})


def test_actual_report_narrative_and_zero_are_usable_complete_counterevidence_units():
    material = {
        'id': 'r',
        'kind': 'report',
        'content': {
            'resultValue': 0,
            'record': '近期未出现合成情况',
            'rawText': '原报告未提示合成病变',
            'findings': '未见合成异常所见',
            'diagnosis': '原报告记录阴性',
        },
    }
    for quote in ('0', '近期未出现合成情况', '原报告未提示合成病变', '未见合成异常所见', '原报告记录阴性'):
        assert personal_citations([{'evidenceId': 'r', 'quote': quote}], {'r': material})


def test_fixed_report_context_accepts_actual_age_and_stage_values_not_field_names_or_pointers():
    material = {
        'id': 'r',
        'kind': 'report',
        'content': {
            'resultValue': '合成实际结果',
            'reportContext': [
                {'field': 'age', 'value': 0, 'pointer': '/subject/age'},
                {'field': 'pregnancyStatus', 'value': '明确非孕期', 'pointer': '/subject/pregnancyStatus'},
                {'field': 'pregnant', 'value': False, 'pointer': '/subject/pregnant'},
                {'field': 'ageUnit', 'value': '合成年龄单位', 'pointer': '/subject/ageUnit'},
                {'field': 'patientName', 'value': '合成不应进入的姓名', 'pointer': '/subject/name'},
            ],
        },
    }
    for quote in ('0', '明确非孕期', 'false'):
        assert personal_citations([{'evidenceId': 'r', 'quote': quote}], {'r': material})
    for quote in (
        'age',
        'pregnancyStatus',
        '/subject/age',
        '/subject/pregnant',
        'False',
        'true',
        '合成年龄单位',
        '合成不应进入的姓名',
    ):
        with pytest.raises(HealthAnalysisGenerationError):
            personal_citations([{'evidenceId': 'r', 'quote': quote}], {'r': material})
    assessment = FactorAnalysis._match(
        {
            'conditions': [
                {
                    'id': 'pregnant',
                    'status': 'not_met',
                    'support': [],
                    'contradicts': [{'evidenceId': 'r', 'quote': 'false'}],
                    'explanation': '明确false保留为反证',
                }
            ]
        },
        {'conditions': [{'id': 'pregnant'}]},
        {'r': material},
    )
    assert not assessment['supported']
    assert assessment['conditions'][0]['status'] == 'not_met'


@pytest.mark.parametrize('actual,quote', [('原报告未见合成病变', '原报告未见合成病变'), (0, '0')])
def test_report_context_wrapper_preserves_a_scalar_observation_without_promoting_metadata(actual, quote):
    material = {
        'id': 'r',
        'kind': 'report',
        'content': {
            'observation': actual,
            'reportContext': [{'field': 'age', 'value': 48, 'pointer': '/subject/age'}],
            'metadata': {'label': '不应成为患者结果的标签'},
        },
    }
    assert personal_citations([{'evidenceId': 'r', 'quote': quote}], {'r': material})
    with pytest.raises(HealthAnalysisGenerationError):
        personal_citations([{'evidenceId': 'r', 'quote': '不应成为患者结果的标签'}], {'r': material})
