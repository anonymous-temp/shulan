"""Accepted rule identities survive provider changes and checkpoint recovery."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from modules.questionnaire.agent import LEGACY_QUESTIONNAIRE_MANIFEST
from modules.questionnaire.agent import QuestionnaireExecutionVersionError
from modules.questionnaire.agent import bind_questionnaire_execution
from modules.questionnaire.agent import questionnaire_agent_manifest
from modules.questionnaire.evidence import LEGACY_EVIDENCE_RULE_SELECTION
from modules.questionnaire.evidence import QuestionnaireEvidenceVersionError
from modules.questionnaire.evidence import QuestionnaireStatementAssessment
from modules.questionnaire.factors import QuestionnaireFactorPipeline
from modules.questionnaire.factors_answers import target_answer_refs
from modules.questionnaire.generation import QuestionnaireGenerator
from modules.questionnaire.schemas import stable_hash
from tests.unit.modules.questionnaire.test_factors import QUESTIONNAIRE
from tests.unit.questionnaire.test_lifecycle import harness as harness


class Rules:
    def __init__(self):
        self.selection = deepcopy(LEGACY_EVIDENCE_RULE_SELECTION)
        self.calls = []

    def execution_selection(self):
        return deepcopy(self.selection)

    def classify_context(self, **request):
        self.calls.append(deepcopy(request))
        return request['dimension']

    def assess_statement(self, **request):
        self.calls.append(deepcopy(request))
        return QuestionnaireStatementAssessment(request['kind'], 'self', 'present')


def pipeline(rules=None):
    model = SimpleNamespace(run=AsyncMock(side_effect=AssertionError('no model expected')))
    worker = QuestionnaireFactorPipeline(model=model, knowledge=None, evidence_policy=rules or Rules())
    return worker, model


def test_raw_answer_selection_and_acceptance_are_owned_by_questionnaire():
    rules = Rules()
    raw = ['  高血压  ', '未治疗']
    answers = [
        {
            'id': 'answer1',
            'questionName': 'medical_history',
            'question': {'title': ' 曾确诊哪些疾病？ ', 'type': 'checkbox'},
            'answerText': '高血压\n未治疗',
            'answer': raw,
            'answerQuoteUnits': raw,
        }
    ]
    original = deepcopy(answers)
    target = {'kind': 'disease', 'label': '高血压', 'answerIds': ['answer1']}
    assert target_answer_refs(target, answers, evidence_policy=rules) == ['answer1']
    assert answers == original
    assert rules.calls[-1] == {
        'kind': 'condition',
        'label': '高血压',
        'answer': raw,
        'question': ' 曾确诊哪些疾病？ ',
        'dimension': 'medical_history',
    }
    answers[0]['questionName'] = 'family_history'
    rules.calls.clear()
    assert target_answer_refs(target, answers, evidence_policy=rules) == []
    assert len(rules.calls) == 1  # Family context is rejected before assertion assessment.


@pytest.mark.parametrize('old', [None, LEGACY_QUESTIONNAIRE_MANIFEST])
def test_only_known_legacy_manifest_can_run_fixed_rules(old):
    document = {} if old is None else {'questionnaireAgentManifest': deepcopy(old)}
    bind_questionnaire_execution(document, questionnaire_agent_manifest(), {'runId': 'run'})
    assert document['questionnaireAgentManifest'] == LEGACY_QUESTIONNAIRE_MANIFEST
    new = questionnaire_agent_manifest()
    new['evidenceRuleSelection']['implementationRevision'] = 'future'
    with pytest.raises(QuestionnaireExecutionVersionError):
        bind_questionnaire_execution(document, new, {'runId': 'other'})


@pytest.mark.parametrize('change', ['missing', 'null', 'future', 'unexpected'])
def test_current_manifest_requires_an_exact_rule_identity(change):
    stored = questionnaire_agent_manifest()
    if change == 'missing':
        stored.pop('evidenceRuleSelection')
    elif change == 'null':
        stored['evidenceRuleSelection'] = None
    elif change == 'future':
        stored['evidenceRuleSelection']['implementationRevision'] = 'future'
    else:
        stored['evidenceRuleSelection']['latest'] = True
    with pytest.raises(QuestionnaireExecutionVersionError):
        bind_questionnaire_execution({'questionnaireAgentManifest': stored}, questionnaire_agent_manifest(), {})


@pytest.mark.asyncio
@pytest.mark.parametrize('location', ['preparation', 'checkpoint', 'checkpoint_preparation', 'task'])
@pytest.mark.parametrize('value', [None, {'contractVersion': 'unknown', 'implementationRevision': 'future'}])
async def test_unavailable_rules_fail_before_models_or_research(location, value):
    worker, model = pipeline()
    snapshot = {'reports': [], 'questionnaire': QUESTIONNAIRE, 'answers': {}}
    if location == 'preparation':
        snapshot['factorPreparation'] = {'evidenceRuleSelection': value}
    elif location == 'checkpoint':
        snapshot['_factorCheckpoint'] = {'inputHash': 'unrelated', 'evidenceRuleSelection': value}
    elif location == 'checkpoint_preparation':
        snapshot['_factorCheckpoint'] = {'preparation': {'evidenceRuleSelection': value}}
    else:
        snapshot['_factorEvidenceRuleSelection'] = value
    with pytest.raises(QuestionnaireEvidenceVersionError):
        await worker.evaluate(snapshot)
    model.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_mutated_provider_is_rejected_before_generation_or_preparation():
    rules = Rules()
    worker, model = pipeline(rules)
    documents = SimpleNamespace(stage=AsyncMock())
    generator = QuestionnaireGenerator(documents=documents, factor_pipeline=worker)
    rules.selection['implementationRevision'] = 'future'
    with pytest.raises(QuestionnaireEvidenceVersionError):
        await generator.generate(user_id='u', questionnaire_id='q', file_list=[])
    with pytest.raises(QuestionnaireEvidenceVersionError):
        await worker.prepare(reports=[], questionnaire_id='q')
    documents.stage.assert_not_awaited()
    model.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_checkpoint_maps_to_v1_without_repeating_completed_work():
    worker, model = pipeline()
    prepared = await worker.prepare(reports=[], questionnaire_id='q')
    prepared.pop('evidenceRuleSelection')
    snapshot = {'reports': [], 'questionnaire': QUESTIONNAIRE, 'answers': {}, 'factorPreparation': prepared}
    old_hash = stable_hash([[], QUESTIONNAIRE, worker._answers(snapshot), prepared])
    saved = {
        'inputHash': old_hash,
        'preparation': deepcopy(prepared),
        'answerDiscoveryComplete': True,
        'evaluations': {},
    }
    worker.prepare = AsyncMock(side_effect=AssertionError('must reuse fixed preparation'))
    worker._discover_answers = AsyncMock(side_effect=AssertionError('completed discovery must not repeat'))
    checkpoints = []
    result = await worker.evaluate({**snapshot, '_factorCheckpoint': saved}, checkpoint=checkpoints.append)
    assert result['targets'] == []
    assert checkpoints[-1]['evidenceRuleSelection'] == LEGACY_EVIDENCE_RULE_SELECTION
    assert checkpoints[-1]['inputHash'] != old_hash
    resumed = await worker.evaluate({**snapshot, '_factorCheckpoint': checkpoints[-1]})
    assert {key: value for key, value in result.items() if key != 'generatedAt'} == {
        key: value for key, value in resumed.items() if key != 'generatedAt'
    }
    model.run.assert_not_awaited()
    worker.prepare.assert_not_awaited()
    worker._discover_answers.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_change_while_waiting_for_model_slot_prevents_dispatch():
    rules = Rules()
    worker, model = pipeline(rules)
    worker._semaphore = asyncio.Semaphore(0)
    task = asyncio.create_task(worker._run('targets', {}))
    await asyncio.sleep(0)
    rules.selection['implementationRevision'] = 'future'
    worker._semaphore.release()
    with pytest.raises(QuestionnaireEvidenceVersionError):
        await task
    model.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_change_during_model_call_cannot_return_result():
    rules = Rules()
    worker, model = pipeline(rules)

    async def change_provider(*args, **kwargs):
        rules.selection['implementationRevision'] = 'future'
        return {'output': {'targets': []}}

    model.run.side_effect = change_provider
    with pytest.raises(QuestionnaireEvidenceVersionError):
        await worker._run('targets', {})
    model.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_rule_failure_is_not_converted_to_generation_or_search_fallback():
    rules = Rules()
    worker, model = pipeline(rules)

    async def staged(**kwargs):
        rules.selection['implementationRevision'] = 'future'
        return {'reports': [], 'artifacts': []}

    generator = QuestionnaireGenerator(documents=SimpleNamespace(stage=staged), factor_pipeline=worker)
    with pytest.raises(QuestionnaireEvidenceVersionError):
        await generator.generate(user_id='u', questionnaire_id='q', file_list=[])
    with pytest.raises(QuestionnaireEvidenceVersionError):
        await worker._research({'id': 't', 'query': 'q'}, phase='post_answer')
    model.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_change_with_model_failure_cannot_turn_into_search_fallback():
    rules = Rules()
    worker, model = pipeline(rules)

    async def failed_search(*args, **kwargs):
        rules.selection['implementationRevision'] = 'future'
        raise RuntimeError('provider failed after rules changed')

    model.run.side_effect = failed_search
    with pytest.raises(QuestionnaireEvidenceVersionError):
        await worker._research({'id': 't', 'query': 'q'}, phase='post_answer')
    model.run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('before_generation', [True, False])
async def test_unavailable_rules_seal_generation_without_background_retries(harness, before_generation):
    h = harness
    h.service._schedule = Mock()
    created = await h.service.create('user', 'session')

    def unavailable():
        raise QuestionnaireEvidenceVersionError('fixed rules unavailable')

    if before_generation:
        h.service._domain_agent.manifest_provider = unavailable
    else:
        h.generator.generate.side_effect = QuestionnaireEvidenceVersionError('fixed rules unavailable')
    await h.service._generate('user', created['questionnaireId'])
    saved = await h.store.get_by_id('user', created['questionnaireId'])
    assert saved['status'] == 'failed' and saved['retryable'] is False
    assert saved['generationAttempts'] == (0 if before_generation else 1)
    if before_generation:
        h.generator.generate.assert_not_awaited()
    else:
        h.generator.generate.assert_awaited_once()
