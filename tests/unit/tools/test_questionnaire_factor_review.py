"""Synthetic-only evidence review validation and readable, safe projection."""

import copy

import pytest

from tests.unit._support.harnesses.questionnaire_factor_review import build_review_projection
from tests.unit._support.harnesses.questionnaire_factor_review import render_review_html
from tests.unit._support.harnesses.questionnaire_factor_review import render_review_markdown
from tests.unit._support.harnesses.questionnaire_factor_review import validate_review


@pytest.fixture
def review_result():
    report = {
        'id': 'report:/observations/0',
        'kind': 'report_extraction',
        'raw': {'itemName': '合成指标', 'resultValue': '6.2', 'unit': 'mmol/L', 'referenceRange': '合成范围'},
        'context': {'reportTime': {'value': '2020-01-01', 'pointer': '/reportTime'}},
    }
    exposure = {
        'id': 'report:/observations/1',
        'kind': 'report_extraction',
        'raw': {'itemName': '合成暴露测量', 'resultValue': '28.2', 'unit': 'kg/m²'},
    }
    answer = {
        'id': 'answer:Sheet!C2',
        'kind': 'user_answer',
        'raw': '父亲有相关疾病',
        'answerState': 'provided',
        'question': '1、一级亲属有无相关疾病？',
        'questionRef': 'question:Sheet!C1',
        'alignment': 'title_only',
        'questionNameHint': 'family_history',
    }
    case = {
        'reportTime': '2020-01-01',
        'answeredAt': '2020-02-01',
        'sourceFiles': {'report': {'sha256': 'synthetic'}},
        'evidence': [report, exposure, answer],
        'historicalInterpretations': [
            {
                'id': 'history:question/description',
                'kind': 'historical_ai_interpretation',
                'text': '旧问卷 AI 风险解释',
                'sourceArtifact': 'questionnaire_json',
                'pointer': '/pages/0/elements/0/description',
            }
        ],
        'reportSourceAvailability': {'originalDocumentProvided': False, 'originalDocumentVerified': False},
    }
    factor = {
        'conceptId': 'family_history',
        'targetRiskId': 'risk1',
        'presence': 'present',
        'judgment': 'supported',
        'interpretation': '个人家族史支持相应风险关系，尚未建立个人病因。',
        'evidenceRefs': [answer['id'], exposure['id']],
        'counterEvidenceRefs': [],
    }
    target = {
        'id': 'target1',
        'label': '合成指标 6.2 mmol/L',
        'kind': 'measurement',
        'sourceRecordRefs': [report['id']],
        'historicalInterpretationRefs': ['history:question/description'],
        'interpretation': '本轮对合成指标的解释。',
    }
    link = {
        'sourceId': 'K1',
        'quote': 'Family history is associated with risk.',
        'meaning': '家族史与风险有关。',
        'applicability': '适用于资料所述人群。',
        'limitations': '一般关联不证明个人因果。',
    }
    condition = {
        'id': 'family_present',
        'label': '一级亲属相关病史是否存在',
        'initialState': 'unknown',
        'finalState': 'present',
        'evidenceRefs': [answer['id']],
    }
    effect = {
        'conditionId': condition['id'],
        'answerRef': answer['id'],
        'before': 'unknown',
        'after': 'present',
        'effect': 'supports',
        'reason': '回答说明一级亲属病史存在，不据此确定个人病因。',
    }
    chain = {
        'candidateId': 'candidate1',
        'conceptId': factor['conceptId'],
        'targetRiskId': 'risk1',
        'targetProblemIds': ['target1'],
        'initialCandidate': {
            'hypothesis': '将家族背景列为待核实的风险因素。',
            'basis': '依据合成指标及医学关系回顾列入。',
            'knowledgeLinks': [link],
            'reportEvidenceRefs': [report['id']],
        },
        'conditions': [condition],
        'answerEffects': [effect],
        'finalAssessment': {
            'conclusion': factor['interpretation'],
            'confirmed': '一级亲属相关病史的自述。',
            'notEstablished': '个人因果关系。',
            'nextVerification': '确认亲属诊断类型及相关时间。',
        },
    }
    analysis = {
        'risks': [
            {
                'id': 'risk1',
                'label': '合成风险方向',
                'interpretation': '本轮派生风险解读。',
                'evidenceRefs': [report['id']],
            }
        ],
        'factors': [factor],
        'review': {'version': 'factor-evidence-review-v2', 'targetProblems': [target], 'chains': [chain]},
    }
    source = {
        'id': 'K1',
        'title': 'Synthetic source',
        'url': 'https://example.invalid/source',
        'sourceText': 'Family history is associated with risk. Population applicability remains limited.',
        'provenance': 'public_source_excerpt',
        'sourceType': '机构健康资料',
        'publishedAt': None,
        'retrievedAt': '2020-02-02',
        'locator': 'Risk factors / family history',
        'evidenceGrading': 'ungraded',
        'metadata': {'publisher': 'Synthetic publisher'},
    }
    return {
        'case': case,
        'analysis': analysis,
        'catalog': {'concepts': [{'id': 'family_history', 'label': '家族背景'}]},
        'knowledge': {'sources': [source]},
        'run': {'medicalReview': 'approved'},
        'views': [
            {
                'clinical': {'label': '家族背景', 'relation': '风险因素'},
                'mesh': {'relationHeading': 'Risk Factors'},
                'snomedInspired': {'relation': 'risk_factor', 'code': None},
            }
        ],
    }


def validate(result):
    return validate_review(result['case'], result['analysis'], result['catalog'], result['knowledge'])


def chain(result):
    return result['analysis']['review']['chains'][0]


def test_projection_preserves_input_and_expands_all_layers_without_fabricating_history(review_result):
    before = copy.deepcopy(review_result)
    projected = build_review_projection(review_result)
    assert review_result == before
    assert projected['traceBasis'] == {
        'mode': 'retrospective_reconstruction',
        'historicalPreAnswerRunAvailable': False,
        'analysisInputIncludedAnswers': True,
        'clinicalReview': 'pending',
    }
    expanded = projected['chains'][0]
    assert expanded['targetProblems'][0]['sourceRecords'][0]['raw']['resultValue'] == '6.2'
    assert expanded['targetProblems'][0]['historicalInterpretations'][0]['text'] == '旧问卷 AI 风险解释'
    assert expanded['answerEffects'][0]['question'] == '1、一级亲属有无相关疾病？'
    assert expanded['answerEffects'][0]['rawAnswer'] == '父亲有相关疾病'
    assert expanded['answerEffects'][0]['alignment'] == 'title_only'
    assert expanded['initialCandidate']['knowledgeLinks'][0]['source'] == review_result['knowledge']['sources'][0]
    assert expanded['factorEvidenceRecords'][1]['raw']['resultValue'] == '28.2'
    expanded['answerEffects'][0]['rawAnswer'] = 'changed'
    assert review_result == before


@pytest.mark.parametrize('field', ['conclusion', 'confirmed', 'notEstablished', 'nextVerification'])
def test_missing_final_field_is_rejected(review_result, field):
    del chain(review_result)['finalAssessment'][field]
    with pytest.raises(ValueError, match='final assessment fields'):
        validate(review_result)


@pytest.mark.parametrize('location', ['target', 'candidate', 'condition', 'factor'])
def test_historical_ai_text_cannot_be_personal_evidence(review_result, location):
    historical = 'history:question/description'
    if location == 'target':
        review_result['analysis']['review']['targetProblems'][0]['sourceRecordRefs'] = [historical]
    elif location == 'candidate':
        chain(review_result)['initialCandidate']['reportEvidenceRefs'] = [historical]
    elif location == 'condition':
        chain(review_result)['conditions'][0]['evidenceRefs'] = [historical]
    else:
        review_result['analysis']['factors'][0]['evidenceRefs'] = [historical]
    with pytest.raises(ValueError, match='reference'):
        validate(review_result)


@pytest.mark.parametrize(
    'mutation',
    ['duplicate_target', 'wrong_target', 'wrong_pair', 'missing_chain', 'duplicate_pair', 'wrong_conclusion'],
)
def test_target_factor_and_final_conclusion_links_are_closed(review_result, mutation):
    review = review_result['analysis']['review']
    if mutation == 'duplicate_target':
        review['targetProblems'].append(copy.deepcopy(review['targetProblems'][0]))
    elif mutation == 'wrong_target':
        review['targetProblems'][0]['sourceRecordRefs'] = ['report:/observations/1']
    elif mutation == 'wrong_pair':
        chain(review_result)['targetRiskId'] = 'unknown'
    elif mutation == 'missing_chain':
        review['chains'] = []
    elif mutation == 'duplicate_pair':
        duplicate = copy.deepcopy(chain(review_result))
        duplicate['candidateId'] = 'candidate2'
        review['chains'].append(duplicate)
    else:
        chain(review_result)['finalAssessment']['conclusion'] = '另一结论'
    with pytest.raises(ValueError):
        validate(review_result)


@pytest.mark.parametrize('mutation', ['unknown_source', 'false_quote', 'summary', 'missing_meaning'])
def test_initial_candidate_requires_original_knowledge_and_explanation(review_result, mutation):
    link = chain(review_result)['initialCandidate']['knowledgeLinks'][0]
    if mutation == 'unknown_source':
        link['sourceId'] = 'unknown'
    elif mutation == 'false_quote':
        link['quote'] = 'Fabricated sentence.'
    elif mutation == 'summary':
        review_result['knowledge']['sources'][0]['provenance'] = 'ai_summary'
    else:
        del link['meaning']
    with pytest.raises(ValueError):
        validate(review_result)


def test_symptom_candidate_can_come_only_from_answer_without_report_refs(review_result):
    answer_ref = 'answer:Sheet!C2'
    target = review_result['analysis']['review']['targetProblems'][0]
    target.update(kind='symptom', sourceRecordRefs=[answer_ref])
    review_result['analysis']['risks'][0]['evidenceRefs'] = [answer_ref]
    chain(review_result)['initialCandidate']['reportEvidenceRefs'] = []
    validate(review_result)
    chain(review_result)['initialCandidate']['reportEvidenceRefs'] = [answer_ref]
    with pytest.raises(ValueError, match='wrong source kind'):
        validate(review_result)


@pytest.mark.parametrize(
    'mutation',
    ['wrong_before', 'wrong_final', 'no_change_changed', 'report_as_answer', 'no_question', 'duplicate_effect'],
)
def test_answer_effects_must_explain_condition_transitions(review_result, mutation):
    row = chain(review_result)
    effect = row['answerEffects'][0]
    if mutation == 'wrong_before':
        effect['before'] = 'absent'
    elif mutation == 'wrong_final':
        row['conditions'][0]['finalState'] = 'absent'
    elif mutation == 'no_change_changed':
        effect['effect'] = 'no_change'
    elif mutation == 'report_as_answer':
        effect['answerRef'] = 'report:/observations/0'
    elif mutation == 'no_question':
        del review_result['case']['evidence'][2]['questionRef']
    else:
        row['answerEffects'].append(copy.deepcopy(effect))
    with pytest.raises(ValueError):
        validate(review_result)


@pytest.mark.parametrize('raw', [None, '', '不清楚', '不知道'])
def test_unknown_answer_cannot_create_presence_or_denial(review_result, raw):
    review_result['case']['evidence'][2]['raw'] = raw
    with pytest.raises(ValueError):
        validate(review_result)
    row = chain(review_result)
    row['conditions'][0]['finalState'] = 'unknown'
    row['answerEffects'][0].update(after='unknown', effect='no_change')
    review_result['analysis']['factors'][0].update(presence='unknown', judgment='needs_verification')
    validate(review_result)


def test_supported_risk_relation_can_retain_unknown_secondary_condition(review_result):
    chain(review_result)['conditions'].append(
        {
            'id': 'history_timing',
            'label': '亲属诊断时间',
            'initialState': 'unknown',
            'finalState': 'unknown',
            'evidenceRefs': [],
        }
    )
    validate(review_result)
    review_result['analysis']['factors'][0]['presence'] = 'unknown'
    with pytest.raises(ValueError, match='supported factor'):
        validate(review_result)


def test_unknown_new_answer_does_not_erase_known_report_condition(review_result):
    row = chain(review_result)
    review_result['case']['evidence'][2]['raw'] = None
    row['conditions'][0].update(initialState='present', evidenceRefs=['report:/observations/0', 'answer:Sheet!C2'])
    row['answerEffects'][0].update(before='present', after='present', effect='no_change')
    validate(review_result)


def test_known_conflict_is_not_silently_overwritten(review_result):
    row = chain(review_result)
    row['conditions'][0].update(initialState='absent', evidenceRefs=['report:/observations/0', 'answer:Sheet!C2'])
    row['answerEffects'][0]['before'] = 'absent'
    with pytest.raises(ValueError, match='preserve conflict'):
        validate(review_result)
    row['conditions'][0]['finalState'] = 'conflicting'
    row['answerEffects'][0].update(after='conflicting', effect='conflicts')
    review_result['analysis']['factors'][0].update(presence='conflicting', judgment='conflicting')
    validate(review_result)


def test_model_cannot_supply_historical_execution_metadata(review_result):
    review_result['analysis']['review']['traceBasis'] = {'historicalPreAnswerRunAvailable': True}
    with pytest.raises(ValueError, match='review fields'):
        validate(review_result)


@pytest.mark.parametrize('renderer', [render_review_html, render_review_markdown])
def test_all_four_layers_and_source_qualifiers_are_visible(renderer, review_result):
    output = renderer(review_result)
    for text in [
        '老板审阅',
        '报告解读 JSON 记录（未核验原始体检文件）',
        '旧问卷 AI 风险解释',
        '本轮派生风险解读',
        'Family history is associated with risk.',
        '家族史与风险有关',
        '机构健康资料',
        'Risk factors / family history',
        '未分级',
        '一级亲属有无相关疾病',
        '父亲有相关疾病',
        '尚未建立',
        '下一步核实',
        '合成暴露测量',
        '28.2',
        '三种视图',
    ]:
        assert text in output
    if renderer is render_review_html:
        assert '<details><summary>本次保存的知识原文' in output
        assert '<details><summary>具体问题' not in output


def test_html_escapes_all_untrusted_content_and_only_links_http_sources(review_result):
    source = review_result['knowledge']['sources'][0]
    source.update(title='<script>alert(1)</script>', url='javascript:alert(1)')
    review_result['case']['evidence'][2]['raw'] = '<img src=x onerror=alert(1)>'
    review_result['case']['historicalInterpretations'][0]['text'] = '<svg onload=alert(1)>'
    output = render_review_html(review_result)
    assert '<script>' not in output and '<img' not in output and '<svg' not in output
    assert '&lt;script&gt;' in output and '&lt;img' in output and '&lt;svg' in output
    assert 'href="javascript:' not in output
    source['url'] = 'https://example.invalid/source?a=1&b=2'
    output = render_review_html(review_result)
    assert 'href="https://example.invalid/source?a=1&amp;b=2"' in output


@pytest.mark.parametrize('renderer', [render_review_html, render_review_markdown])
def test_unassigned_target_keeps_records_and_interpretations_without_inventing_factor(review_result, renderer):
    record = {
        'id': 'report:/observations/2',
        'kind': 'report_extraction',
        'raw': {'itemName': '合成未挂链指标', 'resultValue': '2.6', 'unit': 'ng/mL'},
    }
    historical = {
        'id': 'history:unassigned',
        'kind': 'historical_ai_interpretation',
        'text': '未挂链指标的旧解读文案',
        'sourceArtifact': 'questionnaire_json',
        'pointer': '/pages/0/elements/1/description',
    }
    review_result['case']['evidence'].append(record)
    review_result['case']['historicalInterpretations'].append(historical)
    review_result['analysis']['review']['targetProblems'].append(
        {
            'id': 'target_unassigned',
            'label': '合成未挂链问题',
            'kind': 'measurement',
            'sourceRecordRefs': [record['id']],
            'historicalInterpretationRefs': [historical['id']],
            'interpretation': '保留该项问题，尚未形成有依据的因素关联。',
        }
    )
    original_factor_count = len(review_result['analysis']['factors'])
    projected = build_review_projection(review_result)
    assert [row['id'] for row in projected['unassignedTargetProblems']] == ['target_unassigned']
    assert len(projected['chains']) == original_factor_count
    output = renderer(review_result)
    for text in [
        '尚未建立诱因链的问题',
        '合成未挂链指标',
        '2.6',
        '未挂链指标的旧解读文案',
        '尚未形成有依据的因素关联',
        '不表示已排除',
    ]:
        assert text in output
    assert len(review_result['analysis']['factors']) == original_factor_count


@pytest.mark.parametrize('renderer', [render_review_html, render_review_markdown])
def test_compact_records_preserve_extra_data_and_do_not_repeat_initial_report_body(review_result, renderer):
    report = review_result['case']['evidence'][0]
    report['raw']['extraSourceDetail'] = '必须完整保留的额外记录'
    review_result['analysis']['factors'][0]['evidenceRefs'].append(report['id'])
    chain(review_result)['initialCandidate']['reportEvidenceRefs'].append('report:/observations/1')
    review_result['analysis']['factors'][0]['temporalMatch'] = 'unknown'
    output = renderer(review_result)
    assert output.count('合成指标：6.2 mmol/L') == 1
    assert output.count('合成暴露测量：28.2 kg/m²') == 1
    assert output.count('1、一级亲属有无相关疾病？') == 1
    assert '参考范围：合成范围；日期：2020-01-01' in output
    assert '必须完整保留的额外记录' in output
    assert '候选关联的已展示报告记录' in output
    assert '仅题干对应' in output and '时段尚未核实' in output and '公开资料原文摘录' in output
    projected = build_review_projection(review_result)
    assert projected['chains'][0]['targetProblems'][0]['sourceRecords'][0]['raw'] == report['raw']
    if renderer is render_review_html:
        assert '<details><summary>更多记录字段</summary>' in output


@pytest.mark.parametrize('renderer', [render_review_html, render_review_markdown])
@pytest.mark.parametrize(
    'mode,label',
    [
        ('assistant_analysis_replay', '助手整理、程序校验回放；未运行项目模型或私有知识库'),
        ('live_aion_evimed', '真实调用 AION 模型与 EviMed 知识检索服务'),
    ],
)
def test_run_mode_is_displayed_without_promoting_medical_review(review_result, renderer, mode, label):
    review_result['run']['mode'] = mode
    output = renderer(review_result)
    assert label in output and '医学审核：待完成' in output
    assert build_review_projection(review_result)['traceBasis']['clinicalReview'] == 'pending'


@pytest.mark.parametrize('renderer', [render_review_html, render_review_markdown])
@pytest.mark.parametrize('summary_available', [True, False, None])
def test_source_availability_note_and_missing_summary_warning(review_result, renderer, summary_available):
    availability = review_result['case']['reportSourceAvailability']
    availability.update(note='合成来源说明：仅提供解读结果。', reportAiSummaryAvailable=summary_available)
    output = renderer(review_result)
    assert '合成来源说明：仅提供解读结果。' in output
    warning = '所提供解读 JSON 没有 AI 风险总结；下列历史解读来自旧问卷承接文本。'
    assert (warning in output) is (summary_available is False)
