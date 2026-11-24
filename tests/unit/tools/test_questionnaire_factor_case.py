"""Synthetic-only provenance, uncertainty, mapping and transport regressions."""

import asyncio
import copy
import json
import subprocess
import sys

import pytest

from tests.api.scripts.questionnaire_factor_case import generate
from tests.api.scripts.questionnaire_factor_case import normalize_knowledge
from tests.unit._support.harnesses.questionnaire_factor_case import build_case
from tests.unit._support.harnesses.questionnaire_factor_case import build_views
from tests.unit._support.harnesses.questionnaire_factor_case import render_html
from tests.unit._support.harnesses.questionnaire_factor_case import validate_analysis
from tests.unit._support.harnesses.questionnaire_factor_case import write_outputs


@pytest.fixture
def sample():
    fact = {'id': 'answer:S!G2', 'kind': 'user_answer', 'raw': '每周两次', 'answerState': 'provided'}
    case = {
        'fingerprint': 'test',
        'evidence': [fact],
        'limitations': [],
        'reportTime': '2020-01-01',
        'answeredAt': '2020-02-01',
        'questionnaireQuestionCount': 1,
        'answerCount': 1,
    }
    catalog = {'concepts': [{'id': 'food', 'label': '饮食因素', 'allowedRelations': ['candidate_contributor']}]}
    knowledge = {
        'sources': [
            {
                'id': 'K1',
                'title': 'Synthetic source',
                'url': 'https://example.invalid',
                'sourceText': 'Synthetic research excerpt.',
                'provenance': 'public_source_excerpt',
            }
        ]
    }
    factor = {
        'conceptId': 'food',
        'targetRiskId': 'risk1',
        'relationType': 'candidate_contributor',
        'presence': 'present',
        'judgment': 'needs_verification',
        'temporalMatch': 'unknown',
        'evidenceRefs': ['answer:S!G2'],
        'counterEvidenceRefs': [],
        'unverifiedConditions': ['份量'],
        'knowledgeRefs': [{'sourceId': 'K1', 'quote': 'research excerpt'}],
        'interpretation': '测试解释',
    }
    analysis = {
        'inputFingerprint': 'test',
        'risks': [
            {'id': 'risk1', 'label': '测试风险', 'interpretation': '测试风险解释', 'evidenceRefs': ['answer:S!G2']}
        ],
        'factors': [factor],
        'limitations': [],
    }
    return case, analysis, catalog, knowledge


@pytest.mark.parametrize(
    'field,value',
    [
        ('relationType', 'causes'),
        ('relationType', 'risk_factor'),
        ('presence', 'likely'),
        ('judgment', 'confirmed_cause'),
        ('temporalMatch', True),
        ('conceptId', 'invented'),
        ('targetRiskId', 'invented'),
        ('evidenceRefs', ['missing']),
        ('counterEvidenceRefs', ['missing']),
    ],
)
def test_invalid_factor_is_not_published(sample, field, value):
    case, analysis, catalog, knowledge = sample
    analysis['factors'][0][field] = value
    with pytest.raises(ValueError):
        validate_analysis(case, analysis, catalog, knowledge)


@pytest.mark.parametrize('presence', ['unknown', 'conflicting', 'absent'])
def test_unresolved_or_denied_presence_never_becomes_supported(sample, presence):
    case, analysis, catalog, knowledge = sample
    analysis['factors'][0].update(presence=presence, judgment='supported')
    with pytest.raises(ValueError, match='cannot be supported'):
        validate_analysis(case, analysis, catalog, knowledge)


def test_empty_answers_do_not_establish_absence(sample):
    case, analysis, catalog, knowledge = sample
    case['evidence'][0].update(raw=None, answerState='unknown')
    analysis['factors'][0].update(presence='absent', judgment='not_supported')
    with pytest.raises(ValueError, match='empty answer'):
        validate_analysis(case, analysis, catalog, knowledge)


def test_other_input_fingerprint_is_rejected(sample):
    case, analysis, catalog, knowledge = sample
    analysis['inputFingerprint'] = 'other-person'
    with pytest.raises(ValueError, match='different input'):
        validate_analysis(case, analysis, catalog, knowledge)


@pytest.mark.parametrize('mutation', ['wrong_quote', 'summary', 'unknown_source', 'extra_diagnosis'])
def test_source_and_contract_failures(sample, mutation):
    case, analysis, catalog, knowledge = sample
    factor = analysis['factors'][0]
    if mutation == 'wrong_quote':
        factor['knowledgeRefs'][0]['quote'] = 'not in source'
    elif mutation == 'summary':
        knowledge['sources'][0]['provenance'] = 'ai_summary'
    elif mutation == 'unknown_source':
        factor['knowledgeRefs'][0]['sourceId'] = 'invented'
    else:
        factor['diagnosis'] = 'uncontrolled conclusion'
    with pytest.raises(ValueError):
        validate_analysis(case, analysis, catalog, knowledge)


def test_projections_preserve_one_result_and_do_not_invent_standard_codes(sample):
    _, analysis, catalog, _ = sample
    original = copy.deepcopy(analysis)
    views = build_views(analysis, catalog)
    assert analysis == original
    assert views[0]['clinical']['text'] == original['factors'][0]['interpretation']
    assert views[0]['mesh']['relationDescriptorId'] is None
    assert views[0]['snomedInspired']['code'] is None
    assert views[0]['snomedInspired']['directCauseAsserted'] is False


def test_workbook_adapter_keeps_full_numbers_and_does_not_guess_alignment(tmp_path):
    from openpyxl import Workbook

    report = {
        'reportTime': '2020-01-01',
        'subject': {'age': 40, 'gender': 'male', 'name': 'do-not-export-name'},
        'observations': [{'itemName': '合成测量' + str(i), 'resultValue': '1800', 'unit': '毫升'} for i in range(24)],
        'aiSummary': '禁止作为证据',
        'sections': [{'reportTime': '2020-01-03', 'expansion': {'diagnosis': '合成影像原文'}}],
    }
    questionnaire = {
        'pages': [
            {
                'elements': [
                    {'name': 'a', 'title': '是否喝水', 'description': 'AI推测'},
                    {'name': 'b', 'title': '是否喝水'},
                ]
            }
        ]
    }
    report_path, questionnaire_path = tmp_path / 'report.json', tmp_path / 'questions.json'
    report_path.write_text(json.dumps(report), encoding='utf-8')
    questionnaire_path.write_text(json.dumps(questionnaire), encoding='utf-8')
    workbook = Workbook()
    workbook.active.append(['提交时间', 'IP', '1、是否喝水', '2、旧版本不同问题', '3、空回答'])
    workbook.active.append(['2020-02-01', 'do-not-export', '1800毫升', '没有', None])
    workbook_path = tmp_path / 'answers.xlsx'
    workbook.save(workbook_path)
    result = build_case(report_path, questionnaire_path, workbook_path)
    assert result['answerCount'] == 3
    assert len([e for e in result['evidence'] if e['kind'] == 'report_extraction']) == 25
    answers = [e for e in result['evidence'] if e['kind'] == 'user_answer']
    assert answers[0]['alignment'] == 'ambiguous' and answers[0]['questionNameHint'] is None
    assert answers[1]['alignment'] == 'unmatched'
    assert answers[2]['answerState'] == 'unknown'
    serialized = json.dumps(result, ensure_ascii=False)
    assert '1800毫升' in serialized and 'do-not-export' not in serialized
    patient_facts = json.dumps(result['evidence'], ensure_ascii=False)
    assert 'AI推测' not in patient_facts and '禁止作为证据' not in patient_facts
    assert {item['text'] for item in result['historicalInterpretations']} == {'AI推测', '禁止作为证据'}
    assert result['reportSourceAvailability']['originalDocumentProvided'] is False
    assert result['reportSourceAvailability']['originalDocumentVerified'] is False
    assert result['reportSourceAvailability']['reportAiSummaryAvailable'] is True
    assert answers[0]['questionRef'] == 'question:Sheet!C1'
    assert 'do-not-export-name' not in serialized
    assert result['evidence'][0]['context']['age']['value'] == 40
    expansion = next(e for e in result['evidence'] if e['id'] == 'report:/sections/0/expansion')
    assert expansion['context']['reportTime']['value'] == '2020-01-03'
    assert result['fingerprint'] == build_case(report_path, questionnaire_path, workbook_path)['fingerprint']
    workbook.active.cell(2, 3).value = '=1+1'
    workbook.save(workbook_path)
    with pytest.raises(ValueError, match='formula'):
        build_case(report_path, questionnaire_path, workbook_path)


def test_html_escapes_patient_and_model_text(sample):
    case, analysis, catalog, knowledge = sample
    analysis['factors'][0]['interpretation'] = '<script>alert(1)</script>'
    result = {
        'case': case,
        'analysis': analysis,
        'knowledge': knowledge,
        'views': build_views(analysis, catalog),
        'run': {'mode': 'synthetic'},
    }
    output = render_html(result)
    assert '<script>' not in output and '&lt;script&gt;' in output


def test_output_replay_cannot_overwrite_results(sample, tmp_path):
    case, analysis, catalog, knowledge = sample
    write_outputs(tmp_path, case, analysis, catalog, knowledge, mode='synthetic')
    saved = (tmp_path / 'result.json').read_bytes()
    assert json.loads(saved)['catalog'] == catalog
    assert json.loads(saved)['catalogFingerprint']
    with pytest.raises(ValueError, match='already exists'):
        write_outputs(tmp_path, case, analysis, catalog, knowledge, mode='synthetic')
    assert (tmp_path / 'result.json').read_bytes() == saved


def test_knowledge_uses_matching_original_blocks_not_summaries():
    payload = {
        'code': 200,
        'data': {
            'guide': {
                'list': [{'id': 1, 'title': 'T', 'summary': 'NOT ORIGINAL'}],
                'blocks': [{'guideId': 1, 'text': 'Original'}, {'guideId': 2, 'text': 'Unrelated'}],
            }
        },
    }
    rows = normalize_knowledge(payload, 'guide', 'K1')
    assert rows[0]['sourceText'] == 'Original'
    payload['data']['guide']['blocks'] = []
    assert normalize_knowledge(payload, 'guide', 'K1') == []


def test_literature_url_mapping_is_normalized():
    payload = {
        'code': 200,
        'data': {
            'literature': {
                'list': [
                    {
                        'id': 7,
                        'title': 'Synthetic',
                        'abstract': 'Original',
                        'url': {'pubmed': 'https://example.invalid/paper'},
                    }
                ]
            }
        },
    }
    assert normalize_knowledge(payload, 'literature', 'K')[0]['url'] == 'https://example.invalid/paper'


def test_two_calls_share_full_case_and_validate_retrieval(sample):
    case, analysis, catalog, knowledge = sample
    prompts = []

    async def complete(prompt):
        prompts.append(prompt)
        return {'queries': [{'query': 'generic test', 'source': 'guide'}]} if len(prompts) == 1 else analysis

    async def retrieve(query, prefix):
        assert query['source'] == 'guide' and prefix == 'K1'
        return knowledge['sources']

    actual, sources = asyncio.run(generate(case, catalog, complete, retrieve))
    assert actual == analysis and len(prompts) == 2
    assert 'answer:S!G2' in prompts[0] and 'answer:S!G2' in prompts[1]
    validate_analysis(case, actual, catalog, sources)


def test_empty_retrieval_does_not_claim_success(sample):
    case, _, catalog, _ = sample
    calls = []

    async def complete(prompt):
        calls.append(prompt)
        return {'queries': [{'query': 'generic test', 'source': 'guide'}]}

    async def retrieve(query, prefix):
        return []

    with pytest.raises(ValueError, match='no original medical source'):
        asyncio.run(generate(case, catalog, complete, retrieve))
    assert len(calls) == 1


def test_import_does_not_load_private_configuration_or_application_runtime():
    code = (
        'import sys; import tests.api.scripts.questionnaire_factor_case; '
        'assert "config.env" not in sys.modules; assert "agents" not in sys.modules; '
        'assert "utils.knowledge" not in sys.modules'
    )
    result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
