"""Report's main Agent owns the existing workflow, with no routing model call."""

from __future__ import annotations

import inspect
import json
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict

from agents import AgentRequest
from agents import AgentRunContext
from agents import AgentSelection
from agents import WorkflowAgentPort
from modules.report.document_source import EXTRACTOR_VERSION
from modules.report.schemas import ReportRequest
from modules.report.task_coordinator import REPORT_CONTENT_REVISION

REPORT_MAIN_SELECTION = AgentSelection('report.main', 'report-main-v1')
REPORT_NARRATIVE_SELECTION = AgentSelection('report.narrative', 'report-narrative-v1')
REPORT_FOLLOWUP_SELECTION = AgentSelection('report.followup', 'report-followup-v1')


class ReportExecutionVersionError(RuntimeError):
    code = 'REPORT_EXECUTION_VERSION_UNAVAILABLE'


class ReportAgent:
    def __init__(self, pipeline, *, dependencies=None, verify_dependencies=None):
        self.pipeline = pipeline
        self._ensure_research_available()
        self._followup_runner = None
        self._verify_dependencies = verify_dependencies
        self._manifest = {
            'contractVersion': 'report-execution.v2',
            'mainAgent': asdict(REPORT_MAIN_SELECTION),
            'contentRevision': REPORT_CONTENT_REVISION,
            'documentOutput': {
                'profileName': 'report_source',
                'contractVersion': '1',
                'implementationRevision': EXTRACTOR_VERSION,
            },
            'documentConsumer': self._document_input_manifest(),
            'evaluateTimeoutSeconds': getattr(pipeline, 'generation_timeout_seconds', 1800.0),
            'dependencies': deepcopy(dependencies or {}),
            'recoveryPolicy': 'seal-missing-results-without-regeneration',
        }
        factory = getattr(pipeline, 'document_source_factory', None)
        describe = getattr(factory, 'supported_document_manifests', None)
        self._document_manifests = deepcopy(describe()) if callable(describe) else [self._manifest['documentConsumer']]
        if self._manifest['documentConsumer'] not in self._document_manifests:
            raise ReportExecutionVersionError('the default document consumer is not registered')
        self.port = WorkflowAgentPort(self._run, REPORT_MAIN_SELECTION, domain='report')

    def _ensure_research_available(self):
        research = getattr(self.pipeline, 'research_agent', None)
        verify = getattr(research, 'ensure_available', None)
        if callable(verify):
            verify()

    def _document_input_manifest(self):
        factory = getattr(self.pipeline, 'document_source_factory', None)
        if factory is None:
            return {'consumerRevision': 'report-no-document-source.v1'}
        describe = getattr(factory, 'document_input_manifest', None)
        if not callable(describe):
            raise ReportExecutionVersionError('Report document consumer has no supported input manifest')
        try:
            value = describe()
            if inspect.iscoroutine(value):
                value.close()
                raise TypeError('document input manifest must be synchronous')
            if (
                not isinstance(value, dict)
                or not isinstance(value.get('consumerRevision'), str)
                or not value['consumerRevision'].strip()
                or value['consumerRevision'] != value['consumerRevision'].strip()
            ):
                raise ValueError('an exact document consumer revision is required')
            # A detached JSON value is the durable admission identity, never a
            # live mutable factory object or an implicit current implementation.
            return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
        except Exception as error:
            raise ReportExecutionVersionError('Report document consumer manifest is unavailable') from error

    def execution_manifest(self):
        return deepcopy(self._manifest)

    def bind_followup_runner(self, runner):
        if self._followup_runner is not None and self._followup_runner != runner:
            raise ValueError('Report follow-up workflow is already bound')
        self._followup_runner = runner

    async def execute(self, job):
        return await self._invoke(job, operation='generate')

    async def execute_followup(self, job):
        return await self._invoke(job, operation='gut_barrier_followup')

    async def _invoke(self, job, *, operation):
        manifest = deepcopy(job.get('executionManifest'))
        if manifest is None:
            # The historical record has no reproducible dependency manifest.
            # Existing started jobs still use seal-only recovery; unstarted
            # unknown versions fail explicitly rather than running new code.
            raise ReportExecutionVersionError('legacy Report task has no supported execution manifest')
        context = AgentRunContext(
            user_id=job['userId'],
            session_id=job['sessionId'],
            request_id=job['_id'],
            run_id='report-main:' + job['_id'],
        )
        output = await self.port.invoke(
            AgentRequest(
                {
                    'request': deepcopy(job['request']),
                    'operation': operation,
                    'job': deepcopy(job),
                    'finalReportId': job['_id'],
                    'executionManifest': manifest,
                    'compatibility': None,
                }
            ),
            context,
            thread_id=job['_id'],
        )
        return output.data['result']

    async def _run(self, request, context, *, thread_id):
        self._ensure_research_available()
        payload = request.payload
        accepted = payload['executionManifest']
        consumer = accepted.get('documentConsumer') if isinstance(accepted, dict) else None
        expected = {**self._manifest, 'documentConsumer': consumer}
        if consumer not in self._document_manifests or accepted != expected:
            raise ReportExecutionVersionError('the accepted Report execution manifest is unavailable')
        if self._document_input_manifest() != self._manifest['documentConsumer']:
            raise ReportExecutionVersionError('Report document consumer changed after admission')
        if self._verify_dependencies is not None:
            try:
                actual = self._verify_dependencies()
            except Exception as error:
                raise ReportExecutionVersionError('a Report dependency is unavailable') from error
            if actual != self._manifest['dependencies']:
                raise ReportExecutionVersionError('a Report dependency changed after admission')
        if (
            payload['request'].get('userId') != context.user_id
            or payload['request'].get('sessionId') != context.session_id
        ):
            raise PermissionError('Report request scope differs from the main Agent context')
        factory = getattr(self.pipeline, 'document_source_factory', None)
        bind = getattr(factory, 'bind_document_manifest', None)
        if callable(bind):
            # Validate and enter before business I/O, then always reset after
            # success, failure or cancellation. Child tasks inherit this scope.
            binding = bind(consumer)
        elif consumer == self._manifest['documentConsumer']:
            binding = nullcontext()
        else:
            raise ReportExecutionVersionError('the admitted document dispatcher is unavailable')
        try:
            binding.__enter__()
        except Exception as error:
            raise ReportExecutionVersionError('the admitted document factory is unavailable') from error
        try:
            return await self._execute_payload(payload, context, thread_id=thread_id)
        finally:
            binding.__exit__(None, None, None)

    async def _execute_payload(self, payload, context, *, thread_id):
        if payload['operation'] == 'generate':
            body = ReportRequest.model_validate(payload['request'])
            result = await self.pipeline.execute_and_callback(
                body,
                final_report_id=payload['finalReportId'],
                defer_callbacks=True,
            )
            outputs = (result.evaluate_payload, *result.followup_payloads)
            source_id = payload['finalReportId']
        elif payload['operation'] == 'gut_barrier_followup' and self._followup_runner is not None:
            result = await self._followup_runner(payload['job'])
            outputs = result.payloads
            source_id = payload['job']['sourceFinalReportId']
        else:
            raise ReportExecutionVersionError('unsupported Report main Agent operation')
        for output in outputs:
            if any(
                output.get(key) != value
                for key, value in {
                    'userId': context.user_id,
                    'sessionId': context.session_id,
                    'finalReportId': source_id,
                }.items()
            ):
                raise ValueError('Report final result identity does not match its accepted task')
        return {
            'result': result,
            'execution': {
                'mainAgent': asdict(REPORT_MAIN_SELECTION),
                'runId': context.run_id,
                'threadId': thread_id,
                'compatibility': payload['compatibility'],
            },
        }


class UnavailableReportAgent:
    def execution_manifest(self):
        return {'contractVersion': 'report-execution.unavailable.v1'}

    def bind_followup_runner(self, runner):
        pass

    async def execute(self, job):
        raise ReportExecutionVersionError('Report main Agent dependencies are unavailable')

    async def execute_followup(self, job):
        raise ReportExecutionVersionError('Report main Agent dependencies are unavailable')
