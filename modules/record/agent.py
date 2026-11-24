"""Record's whole use-case Agent; planning and question graphs remain private steps."""

from __future__ import annotations

from contextlib import aclosing
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict

from agents import AgentEvent
from agents import AgentEventType
from agents import AgentRequest
from agents import AgentRunContext
from agents.contracts import AgentSelection
from agents.errors import AgentConfigurationError
from agents.ports import WorkflowAgentPort

# These aliases describe the pre-versioned implementation and never follow a
# later default. New releases add selections; old receipts retain these values.
LEGACY_RECORD_PLAN_SELECTION = AgentSelection('record.plan', 'record-patient-v11', '1')
LEGACY_RECORD_QUESTION_SELECTION = AgentSelection('record.question', 'record-response-v19', '1')
LEGACY_RECORD_AGENT_SELECTION = AgentSelection('record.main', 'record-workflow-v1', '1')
LEGACY_RECORD_AGENT_MANIFEST = {
    'capability': 'record.main',
    'contractVersion': '1',
    'implementationRevision': 'record-workflow-v1',
    'children': {
        'plan': asdict(LEGACY_RECORD_PLAN_SELECTION),
        'question': asdict(LEGACY_RECORD_QUESTION_SELECTION),
    },
    'flowVersion': 2,
    'defaultInterviewPolicyVersion': 2,
    'responseStyleVersion': 15,
    'contentRevision': 'record-content-20261110',
}
RECORD_AGENT_SELECTION = LEGACY_RECORD_AGENT_SELECTION
RECORD_PLAN_SELECTION = LEGACY_RECORD_PLAN_SELECTION
RECORD_QUESTION_SELECTION = LEGACY_RECORD_QUESTION_SELECTION
RECORD_AGENT_MANIFEST = deepcopy(LEGACY_RECORD_AGENT_MANIFEST)


class RecordAgent:
    def __init__(self, service):
        self.service = service
        self._execution = ContextVar(f'record-agent-execution-{id(self)}', default=None)
        self.port = WorkflowAgentPort(self._invoke, RECORD_AGENT_SELECTION, 'record', streamer=self._stream)

    @property
    def manifest(self):
        return deepcopy(RECORD_AGENT_MANIFEST)

    def _arguments(self, request, context):
        data = dict(request.payload)
        if data.get('user_id') != context.user_id or data.get('session_id') != context.session_id:
            raise ValueError('Record Agent context does not match the use-case identity')
        operation = data.pop('operation')
        return operation, data

    async def _invoke(self, request, context, *, thread_id):
        operation, arguments = self._arguments(request, context)
        execution = self._execution.get() or {}
        if operation in {'create', 'recover'} and not callable(execution.get('lease_guard')):
            raise AgentConfigurationError('Record creation/recovery requires an owned service execution lease')
        if operation == 'recover' and not isinstance(execution.get('session'), dict):
            raise AgentConfigurationError('Record recovery requires the owned session snapshot')
        if operation == 'create':
            return await self.service._createSessionUnderLeaseImpl(**arguments, **execution)
        if operation == 'recover':
            return await self.service._recoverReplyUnderLeaseImpl(**arguments, **execution)
        if operation == 'submit':
            async with aclosing(self.service._submitTurnImpl(**arguments)) as events:
                async for event in events:
                    if event['type'] == 'result':
                        return event['result']
            raise RuntimeError('Record Agent did not produce a committed result')
        raise ValueError('unsupported Record Agent operation')

    async def _stream(self, request, context, *, thread_id):
        operation, arguments = self._arguments(request, context)
        if operation != 'submit':
            raise ValueError('only Record submission has a streaming operation')
        async with aclosing(self.service._submitTurnImpl(**arguments)) as events:
            async for event in events:
                yield AgentEvent(AgentEventType.PROGRESS, context.run_id, {'event': event})

    async def invoke(self, operation, arguments, **execution):
        context = AgentRunContext.create(user_id=arguments['user_id'], session_id=arguments['session_id'])
        token = self._execution.set(execution)
        try:
            result = await self.port.invoke(AgentRequest({'operation': operation, **arguments}), context)
            return dict(result.data)
        finally:
            self._execution.reset(token)

    async def submit(self, arguments):
        if not arguments.get('stream'):
            yield {'type': 'result', 'result': await self.invoke('submit', arguments)}
            return
        context = AgentRunContext.create(user_id=arguments['user_id'], session_id=arguments['session_id'])
        async with aclosing(self.port.stream(AgentRequest({'operation': 'submit', **arguments}), context)) as events:
            async for event in events:
                yield event.data['event']


def install_record_agent(runtime, manager) -> bool:
    """Install the main port atomically; never close shared dependencies."""

    def configure(staged):
        for selection in (RECORD_PLAN_SELECTION, RECORD_QUESTION_SELECTION):
            runtime.registry.get(selection.capability, selection=selection)
        staged.ports.register(RECORD_AGENT_SELECTION, manager.record_agent.port)

    installed = runtime.install_module('record.main', configure)
    manager.set_agent_readiness(installed)
    return installed
