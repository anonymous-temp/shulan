"""Q policy stays executable when Record's private templates/validators change."""

import ast
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from adapters.questionnaire_factors import QuestionnaireFactorModel
from adapters.questionnaire_generation import QuestionnairePlanGateway
from modules.questionnaire import generation_policy as policy
from modules.questionnaire.agent import LEGACY_QUESTIONNAIRE_MANIFEST
from modules.questionnaire.agent import QuestionnaireExecutionVersionError
from modules.questionnaire.agent import bind_questionnaire_execution
from modules.questionnaire.agent import questionnaire_agent_manifest
from modules.questionnaire.agent import resolve_questionnaire_manifest
from modules.questionnaire.evidence import LEGACY_EVIDENCE_RULE_SELECTION
from modules.questionnaire.generation import build_evidence
from modules.questionnaire.generation import build_questionnaire
from tests.unit.questionnaire.test_factor_composition import composed as composed
from tests.unit.questionnaire.test_factor_tasks import factor_harness as factor_harness
from tests.unit.questionnaire.test_generation import REPORT
from tests.unit.questionnaire.test_generation import plan
from tests.unit.questionnaire.test_lifecycle import harness as harness
from tests.unit.questionnaire.test_questionnaire_main_agent import generated


def old_v2():
    return {
        **deepcopy(LEGACY_QUESTIONNAIRE_MANIFEST),
        'contractVersion': 'questionnaire-agent-manifest.v2',
        'evidenceRuleSelection': deepcopy(LEGACY_EVIDENCE_RULE_SELECTION),
    }


def mutate_record(monkeypatch):
    from modules.record.application import flow
    from modules.record.application import lifestyle
    from modules.record.application import presentation
    from modules.record.application import public_text

    monkeypatch.setitem(
        lifestyle.TOPICS, 'diet_structure', {'question': 'changed', 'options': [], 'questionType': 'text'}
    )
    monkeypatch.setitem(lifestyle.TOPICS, 'future_topic', {'question': 'future'})
    monkeypatch.setitem(lifestyle.BRIDGES, 'diet_structure', 'changed bridge')
    for module, names in (
        (flow, ('GeneratedQuestion', 'validate_new_question')),
        (presentation, ('validate_choice_labels', 'validate_answer_entry_instructions', 'normalize_option')),
        (public_text, ('validate_internal_notice',)),
    ):
        for name in names:
            monkeypatch.setattr(module, name, Mock(side_effect=AssertionError('Q cannot run Record private policy')))


@pytest.mark.asyncio
async def test_record_changes_do_not_change_published_questions_or_either_model_input(monkeypatch):
    gateway = SimpleNamespace(complete=AsyncMock(return_value=SimpleNamespace(content='{}')))
    planner = QuestionnairePlanGateway(llm_gateway=gateway)
    factors = QuestionnaireFactorModel(llm_gateway=gateway)
    evidence = build_evidence([{'interpretation': deepcopy(REPORT)}], 'male')
    original = build_questionnaire(evidence, plan(), 'male')
    await planner.plan(evidence=evidence, gender={'value': 'male'})
    await factors.run('candidates', {'targets': []})
    requests = [call.args[0].content for call in gateway.complete.call_args_list]
    mutate_record(monkeypatch)
    assert build_questionnaire(evidence, plan(), 'male') == original
    # Newly instantiated adapters after a deployment retain the same Q inputs too.
    await QuestionnairePlanGateway(llm_gateway=gateway).plan(evidence=evidence, gender={'value': 'male'})
    await QuestionnaireFactorModel(llm_gateway=gateway).run('candidates', {'targets': []})
    assert [call.args[0].content for call in gateway.complete.call_args_list[2:]] == requests
    assert list(policy.TOPICS)[:3] == ['diet_structure', 'staple_portion', 'sweet_intake']
    daily = original['questionnaire']['pages'][-1]['elements']
    assert [item['name'] for item in daily] == [
        'diet_structure',
        'meal_pattern',
        'exercise_frequency',
        'sleep_duration',
        'smoking',
        'alcohol',
        'stress_coping',
    ]
    assert daily[0]['title'] == '您平时的正餐通常吃到哪些食物？可多选。'
    assert [item['value'] for item in daily[0]['choices']] == [
        '米饭',
        '面条',
        '杂粮',
        '蔬菜',
        '水果',
        '猪肉',
        '鱼肉',
        '鸡蛋',
        '豆制品',
    ]


@pytest.mark.asyncio
async def test_factor_lifestyle_key_allowlist_does_not_follow_record(monkeypatch):
    mutate_record(monkeypatch)
    gateway = SimpleNamespace(
        complete=AsyncMock(
            return_value=SimpleNamespace(
                content=json.dumps(
                    {
                        'candidates': [
                            {'conditions': [{'lifestyleKey': 'diet_structure'}, {'lifestyleKey': 'future_topic'}]}
                        ]
                    }
                )
            )
        )
    )
    result = await QuestionnaireFactorModel(llm_gateway=gateway).run('candidates', {})
    assert result['output']['candidates'][0]['conditions'] == [{'lifestyleKey': 'diet_structure'}, {}]


@pytest.mark.parametrize(
    'options,code',
    [
        (['每周1～2天', '从不'], None),
        (['1/2杯', '1杯'], None),
        (['约1拳或更少', '约2拳'], None),
        (['其他', '无'], 'ambiguous_shortcut_forbidden'),
        (['有补充', '无'], 'ambiguous_shortcut_forbidden'),
        (['吃药或运动', '无'], 'ambiguous_shortcut_forbidden'),
        (['暂不回答', '无'], 'refusal_shortcut_forbidden'),
        (['选项（说明）', '无'], 'ambiguous_shortcut_forbidden'),
        (['字' * 25, '无'], 'ambiguous_shortcut_forbidden'),
    ],
)
def test_frozen_patient_choice_rules(options, code):
    question = policy.GeneratedQuestion(question='近期的情况如何？', questionType='radio', options=options)
    if code:
        with pytest.raises(policy.QuestionnairePresentationError) as error:
            policy.validate_new_question(question, action='ask_intake')
        assert error.value.code == code
    else:
        policy.validate_new_question(question, action='ask_intake')


@pytest.mark.parametrize(
    'text,code',
    [
        ('请通过文字或语音补充描述。', None),
        ('不能在选项里输入。', None),
        ('在已复查选项里输入结果。', 'invalid_answer_entry_instruction'),
        ('点击“其他”后填写内容。', 'invalid_answer_entry_instruction'),
        ('系统生成失败，稍后重试。', 'internal_processing_notice_forbidden'),
    ],
)
def test_frozen_entry_and_internal_notice_rules(text, code):
    if code:
        with pytest.raises(policy.QuestionnairePresentationError) as error:
            policy.validate_answer_entry_instructions(text, options=['已复查', '尚未复查'])
        assert error.value.code == code
    else:
        policy.validate_answer_entry_instructions(text, options=['已复查', '尚未复查'])


def test_option_normalization_and_strict_schema_preserve_existing_behavior():
    value = policy.GeneratedQuestion(
        question='  最近的情况？  ', questionType='radio', options=[' 有（请补充） ', '无']
    )
    assert value.model_dump() == {'question': '最近的情况？', 'questionType': 'radio', 'options': ['有', '无']}
    for options in (['有', '有'], [''], ['有', 2]):
        with pytest.raises(ValueError):
            policy.GeneratedQuestion(question='情况？', questionType='radio', options=options)


@pytest.mark.parametrize('manifest', [None, LEGACY_QUESTIONNAIRE_MANIFEST, old_v2()])
def test_legacy_manifests_map_only_to_owned_v1_and_keep_original_identity(manifest):
    raw = {} if manifest is None else {'questionnaireAgentManifest': deepcopy(manifest)}
    expected = deepcopy(raw.get('questionnaireAgentManifest', LEGACY_QUESTIONNAIRE_MANIFEST))
    bind_questionnaire_execution(raw, questionnaire_agent_manifest(), {'runId': 'restored'})
    assert raw['questionnaireAgentManifest'] == expected
    assert resolve_questionnaire_manifest(expected)['generationPolicySelection'] == policy.generation_policy_selection()


@pytest.mark.parametrize('kind', ['missing', 'null', 'future', 'extra', 'alias'])
def test_new_manifest_never_resolves_missing_or_unknown_policy(kind):
    stored = questionnaire_agent_manifest()
    if kind == 'missing':
        stored.pop('generationPolicySelection')
    elif kind == 'null':
        stored['generationPolicySelection'] = None
    elif kind == 'future':
        stored['generationPolicySelection']['implementationRevision'] = 'future'
    elif kind == 'extra':
        stored['generationPolicySelection']['latest'] = True
    else:
        stored['generationPolicySelection'] = 'latest'
    with pytest.raises(QuestionnaireExecutionVersionError, match='policy'):
        bind_questionnaire_execution({'questionnaireAgentManifest': stored}, questionnaire_agent_manifest(), {})


@pytest.mark.asyncio
async def test_old_v2_generation_recovers_without_get_or_record_policy(harness, monkeypatch):
    h = harness
    schedule = h.service._schedule
    h.service._schedule = Mock()
    created = await h.service.create('user', 'session')
    stored = h.store.docs[('user', created['questionnaireId'])]
    stored['questionnaireAgentManifest'] = old_v2()
    stored['generationAttempts'] = 2
    mutate_record(monkeypatch)
    h.service._schedule = schedule
    h.service.get = AsyncMock(side_effect=AssertionError('no GET recovery'))
    await h.service.recover_pending()
    await asyncio.gather(*list(h.service._tasks.values()))
    restored = await h.store.get_by_id('user', created['questionnaireId'])
    assert restored['status'] == 'ready' and restored['generationAttempts'] == 3
    assert restored['questionnaireAgentManifest'] == old_v2()
    h.generator.generate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
async def test_unknown_policy_stops_generation_before_documents_or_model(composed, legacy):
    h = composed
    h.service._schedule = Mock()
    created = await h.service.create('user', 'session')
    stored = h.source_store.docs[('user', created['questionnaireId'])]
    stored['questionnaireAgentManifest'] = old_v2() if legacy else questionnaire_agent_manifest()
    stored['questionnaireAgentManifest']['generationPolicySelection'] = {'implementationRevision': 'future'}
    await h.service._generate('user', created['questionnaireId'])
    result = await h.source_store.get_by_id('user', created['questionnaireId'])
    assert result['status'] == 'failed' and result['retryable'] is False and result['generationAttempts'] == 0
    h.service.generator.documents.stage.assert_not_awaited()
    h.runtime.llm_gateway.complete.assert_not_awaited()
    h.runtime.knowledge_gateway.search.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('supported', [True, False])
async def test_factor_recovery_checks_policy_before_using_checkpoint(factor_harness, supported):
    h = factor_harness
    source = await generated(h)
    await h.service.submit('user', source['questionnaireId'], {'note': 'unchanged raw answer'})
    job = next(iter(h.collection.documents.values()))
    job['questionnaireAgentManifest'] = old_v2()
    job['checkpoint'] = {'stage': 'fixed checkpoint'}
    original = deepcopy({key: job[key] for key in ('assessmentId', 'inputFingerprint', 'inputSnapshot', 'checkpoint')})
    if not supported:
        job['questionnaireAgentManifest']['generationPolicySelection'] = None
    await h.service.drain_factor_assessments()
    assert {key: job[key] for key in original if key != 'checkpoint'} == {
        key: value for key, value in original.items() if key != 'checkpoint'
    }
    if supported:
        assert job['status'] == 'completed' and job['questionnaireAgentManifest'] == old_v2()
        assert h.evaluator.evaluate.call_args.args[0]['_factorCheckpoint'] == original['checkpoint']
    else:
        assert job['status'] == 'failed' and job['retryable'] is False
        h.evaluator.evaluate.assert_not_awaited()


def test_question_policy_has_no_record_private_import():
    root = Path(__file__).resolve().parents[3]
    for relative in (
        'modules/questionnaire/generation.py',
        'modules/questionnaire/generation_policy.py',
        'adapters/questionnaire_generation.py',
        'adapters/questionnaire_factors.py',
    ):
        tree = ast.parse((root / relative).read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or '').startswith('modules.record'), relative
            elif isinstance(node, ast.Import):
                assert not any(alias.name.startswith('modules.record') for alias in node.names), relative
