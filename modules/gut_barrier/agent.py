"""GutBarrier is a peer business main Agent; deterministic rules remain tools."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

from agents import AgentRequest
from agents import AgentRunContext
from agents import AgentSelection
from agents import WorkflowAgentPort

from .content_catalog import CONTENT_VERSION
from .content_catalog import TEMPLATE_VERSION
from .display_versions import LATEST_DISPLAY_TEMPLATE
from .policy import SERVICE_VERSION
from .rule_tools import RULE_TOOL_VERSIONS
from .schemas import GutBarrierRequest

GUT_BARRIER_MAIN_SELECTION = AgentSelection('gut_barrier.main', 'gut-barrier-main-v1')


class GutBarrierExecutionVersionError(RuntimeError):
    code = 'GUT_BARRIER_EXECUTION_VERSION_UNAVAILABLE'


class GutBarrierAgent:
    def __init__(self, pipeline, *, timeout_seconds, dependencies=None):
        self.pipeline = pipeline
        self._manifest = {
            'contractVersion': 'gut-barrier-execution.v1',
            'mainAgent': asdict(GUT_BARRIER_MAIN_SELECTION),
            'serviceVersion': SERVICE_VERSION,
            'pipelineVersion': pipeline.version,
            'ruleTools': dict(RULE_TOOL_VERSIONS),
            'contentVersion': CONTENT_VERSION,
            'templateVersion': TEMPLATE_VERSION,
            'displayTemplateVersion': LATEST_DISPLAY_TEMPLATE,
            'generationTimeoutSeconds': timeout_seconds,
            'dependencies': deepcopy(dependencies or {}),
            'recoveryPolicy': 'seal-interrupted-generation',
        }
        self.port = WorkflowAgentPort(self._run, GUT_BARRIER_MAIN_SELECTION, domain='gut_barrier')

    def execution_manifest(self):
        return deepcopy(self._manifest)

    async def generate(self, job, *, evaluation_date):
        context = AgentRunContext(
            user_id=job['userId'], session_id=job['sessionId'], request_id=job['_id'], run_id='gut-main:' + job['_id']
        )
        result = await self.port.invoke(
            AgentRequest(
                {
                    'request': deepcopy(job['request']),
                    'reportId': job['_id'],
                    'evaluationDate': evaluation_date,
                    'executionManifest': deepcopy(job.get('executionManifest')),
                }
            ),
            context,
            thread_id=job['_id'],
        )
        return result.data['report'], result.data['audit']

    async def _run(self, request, context, *, thread_id):
        payload = request.payload
        if (
            payload.get('executionManifest') != self._manifest
            or self.pipeline.version != self._manifest['pipelineVersion']
        ):
            raise GutBarrierExecutionVersionError('the accepted GutBarrier execution manifest is unavailable')
        body = GutBarrierRequest.model_validate(payload['request'])
        if body.user_id != context.user_id or body.session_id != context.session_id:
            raise PermissionError('GutBarrier task scope differs from main Agent scope')
        report, audit = await self.pipeline.generate(
            body,
            report_id=payload['reportId'],
            evaluation_date=payload['evaluationDate'],
            **({'rule_run_id': context.run_id} if getattr(self.pipeline, 'supports_rule_tools', False) else {}),
        )
        if not isinstance(report, dict) or not isinstance(audit, dict):
            raise TypeError('GutBarrier final result must be a report and audit')
        audit = {
            **audit,
            'agentExecution': {
                'mainAgent': asdict(GUT_BARRIER_MAIN_SELECTION),
                'runId': context.run_id,
                'threadId': thread_id,
            },
        }
        return {'report': report, 'audit': audit}


def install_gut_barrier_agent(runtime, service):
    agent = service.main_agent
    if agent is None:
        raise ValueError('GutBarrier production service requires its main Agent')
    ready = runtime.install_module(
        'gut-barrier-main', lambda staged: staged.ports.register(GUT_BARRIER_MAIN_SELECTION, agent.port)
    )
    if not ready:
        service.main_agent = UnavailableGutBarrierAgent()
        service.stop_admission()
    return ready


class UnavailableGutBarrierAgent:
    def execution_manifest(self):
        return {'contractVersion': 'gut-barrier-execution.unavailable.v1'}

    async def generate(self, job, *, evaluation_date):
        raise GutBarrierExecutionVersionError('GutBarrier main Agent is unavailable')
