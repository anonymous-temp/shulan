"""Questionnaire's whole-workflow main Agent; no additional routing model."""

from copy import deepcopy
from dataclasses import asdict

from agents import AgentSelection
from agents import WorkflowAgentPort
from agents.errors import AgentConfigurationError
from agents.errors import AgentProtocolError
from modules.questionnaire.evidence import LEGACY_EVIDENCE_RULE_SELECTION
from modules.questionnaire.evidence import evidence_rule_selection
from modules.questionnaire.generation_policy import QuestionnairePolicyVersionError
from modules.questionnaire.generation_policy import generation_policy_selection
from modules.questionnaire.generation_policy import require_generation_policy

QUESTIONNAIRE_MAIN_SELECTION = AgentSelection('questionnaire.main', 'questionnaire-main-v1')
LEGACY_QUESTIONNAIRE_MANIFEST = {
    'contractVersion': 'questionnaire-agent-manifest.v1',
    'main': asdict(AgentSelection('questionnaire.main', 'questionnaire-main-v1')),
    'generation': 'questionnaire-generation-v1',
    'reportPlan': 'questionnaire-report-plan-v1',
    'factorPipeline': 'factor-assessment-v1',
    'factorModel': 'questionnaire-factor-model-v1',
}


class QuestionnaireExecutionVersionError(AgentConfigurationError, ValueError):
    """The accepted workflow or one of its private dependencies is unavailable."""


def questionnaire_agent_manifest():
    return {
        **deepcopy(LEGACY_QUESTIONNAIRE_MANIFEST),
        'contractVersion': 'questionnaire-agent-manifest.v3',
        'generationPolicySelection': generation_policy_selection(),
        'main': asdict(QUESTIONNAIRE_MAIN_SELECTION),
        'evidenceRuleSelection': deepcopy(LEGACY_EVIDENCE_RULE_SELECTION),
    }


def resolve_questionnaire_manifest(manifest):
    # Historical v1/v2 tasks map only to the executable, frozen Q policy v1.
    # A missing policy in the new v3 schema is corruption, never a latest alias.
    if manifest is None or manifest == LEGACY_QUESTIONNAIRE_MANIFEST:
        return {
            **deepcopy(LEGACY_QUESTIONNAIRE_MANIFEST),
            'contractVersion': 'questionnaire-agent-manifest.v3',
            'evidenceRuleSelection': deepcopy(LEGACY_EVIDENCE_RULE_SELECTION),
            'generationPolicySelection': generation_policy_selection(),
        }
    if not isinstance(manifest, dict) or manifest.get('contractVersion') not in {
        'questionnaire-agent-manifest.v2',
        'questionnaire-agent-manifest.v3',
    }:
        raise QuestionnaireExecutionVersionError('Questionnaire requires an unavailable execution manifest')
    try:
        selection = evidence_rule_selection(manifest.get('evidenceRuleSelection'))
    except ValueError as error:
        raise QuestionnaireExecutionVersionError('Questionnaire evidence version is unavailable') from error
    policy = manifest.get('generationPolicySelection')
    if manifest['contractVersion'] == 'questionnaire-agent-manifest.v2' and 'generationPolicySelection' not in manifest:
        policy = generation_policy_selection()
    try:
        policy = require_generation_policy(policy)
    except QuestionnairePolicyVersionError as error:
        raise QuestionnaireExecutionVersionError('Questionnaire generation policy is unavailable') from error
    return {
        **deepcopy(manifest),
        'contractVersion': 'questionnaire-agent-manifest.v3',
        'evidenceRuleSelection': selection,
        'generationPolicySelection': policy,
    }


def bind_questionnaire_execution(document, manifest, execution):
    stored = document.get('questionnaireAgentManifest')
    if stored is None:
        stored = deepcopy(LEGACY_QUESTIONNAIRE_MANIFEST)
    if resolve_questionnaire_manifest(stored) != resolve_questionnaire_manifest(manifest):
        raise QuestionnaireExecutionVersionError('Questionnaire requires an unavailable execution manifest')
    document['questionnaireAgentManifest'] = deepcopy(stored)
    document['questionnaireAgentExecution'] = deepcopy(execution)


class QuestionnaireAgent:
    def __init__(self, *, generation=None, factors=None, manifest_provider=None):
        self.generation, self.factors = generation, factors
        self.manifest_provider = manifest_provider
        self.port = WorkflowAgentPort(self._run, QUESTIONNAIRE_MAIN_SELECTION, 'questionnaire')

    def execution_manifest(self):
        manifest = self.manifest_provider() if self.manifest_provider is not None else questionnaire_agent_manifest()
        return deepcopy(manifest)

    async def invoke(self, request, context, *, thread_id=None):
        return await self.port.invoke(request, context, thread_id=thread_id)

    async def stream(self, request, context, *, thread_id=None):
        raise NotImplementedError('Questionnaire tasks use durable result queries')
        yield  # Keep the AgentPort streaming shape without starting work.

    async def _run(self, request, context, *, thread_id):
        payload = request.payload
        if payload.get('userId') != context.user_id or payload.get('sessionId') != context.session_id:
            raise AgentProtocolError('Questionnaire task scope differs from its context')
        execution = {
            'runId': context.run_id,
            'requestId': context.request_id,
            'mainThreadId': thread_id,
            'operation': payload.get('operation'),
        }
        if payload.get('operation') == 'generate' and self.generation is not None:
            return await self.generation(
                payload['userId'], payload['questionnaireId'], session_id=context.session_id, execution=execution
            )
        if payload.get('operation') == 'factor_assessment' and self.factors is not None:
            job = payload['job']
            if job.get('userId') != context.user_id or job.get('sessionId') != context.session_id:
                raise AgentProtocolError('Questionnaire factor job differs from its context')
            return await self.factors(deepcopy(payload['job']), execution=execution)
        raise AgentProtocolError('Questionnaire main operation is unavailable')


def install_questionnaire_agent(runtime, service):
    """Register the domain atomically; failed AI registration leaves pure reads usable."""
    installed = runtime.install_module(
        'questionnaire.main',
        lambda staged: staged.ports.register(QUESTIONNAIRE_MAIN_SELECTION, service.agent_port),
    )
    service.set_agent_readiness(installed)
    return installed
