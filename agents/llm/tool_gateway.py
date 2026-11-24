"""AION 0.6 bounded tool loops with durable project-side guards and receipts.

The existing text gateway is unchanged. Native tool history stays inside this
isolated unit; interrupted units are never advertised as step-resumable graphs.
"""

import asyncio
import inspect
import json
from collections.abc import Mapping
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict
from dataclasses import replace

from aion.base.tools import Tool
from aion.base.tools import ToolOptions
from aion.base.tools import ToolTrace
from aion.base.usage import TokenUsage

from agents.tools.contracts import ToolCallContext
from agents.tools.contracts import ToolOutcome
from agents.tools.contracts import ToolRunConflict
from agents.tools.contracts import ToolRunEvent
from agents.tools.contracts import ToolRunRequest
from agents.tools.contracts import ToolRunResult
from agents.tools.contracts import ToolRunStopped
from agents.tools.ledger import digest
from agents.tools.ledger import usage_sum

from .aion_gateway import AionLLMGateway
from .aion_gateway import _normalize_usage
from .types import LLMResult
from .types import LLMUsage


def plain(value):
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    return value


def usage_dict(value):
    if isinstance(value, TokenUsage):
        return {name: getattr(value, name) for name in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
    normalized = _normalize_usage(value)
    return (
        None
        if normalized is None
        else {
            'prompt_tokens': normalized.prompt_tokens,
            'completion_tokens': normalized.completion_tokens,
            'total_tokens': normalized.total_tokens,
        }
    )


class AionToolRunGateway:
    """Inject into a domain's private research task; it does not enable any tool itself."""

    def __init__(self, gateway: AionLLMGateway, ledger, *, authorize=None, poll_seconds=0.5, max_output_bytes=262144):
        if not isinstance(gateway, AionLLMGateway):
            raise TypeError('gateway must be the configured AionLLMGateway')
        if poll_seconds <= 0 or max_output_bytes < 1:
            raise ValueError('Polling and output bounds must be positive')
        self.gateway, self.ledger, self.authorize = gateway, ledger, authorize
        self.poll_seconds, self.max_output_bytes = poll_seconds, max_output_bytes
        self._tasks = set()

    def _retain(self, task):
        self._tasks.add(task)

        def done(completed):
            self._tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)
        return task

    async def run(self, request: ToolRunRequest) -> ToolRunResult:
        result = None
        async for event in self.stream_events(request):
            if event.kind == 'completed':
                result = event.result
        if result is None:
            raise ToolRunConflict('Tool run returned no durable outcome')
        return result

    async def stream_events(self, request: ToolRunRequest):
        """Only metadata and one final outcome leave the gateway; no draft text deltas."""
        if not isinstance(request, ToolRunRequest):
            raise TypeError('request must be ToolRunRequest')
        # Freeze nested mutable caller inputs before the first suspension. Keep
        # handler ownership intact (deep-copying a bound method clones its owner).
        profile = replace(
            request.llm_request.profile,
            prompt_variables=deepcopy(dict(request.llm_request.profile.prompt_variables)),
            model_kwargs=deepcopy(dict(request.llm_request.profile.model_kwargs)),
        )
        request = replace(
            request,
            llm_request=replace(request.llm_request, profile=profile),
            tools=tuple(replace(tool, parameters=deepcopy(dict(tool.parameters))) for tool in request.tools),
        )
        queue = asyncio.Queue()
        task = self._retain(asyncio.create_task(self._run(request, queue.put)))
        try:
            while True:
                if task.done() and queue.empty():
                    await task
                    break
                receive = asyncio.create_task(queue.get())
                try:
                    ready, _ = await asyncio.wait((receive, task), return_when=asyncio.FIRST_COMPLETED)
                    if receive in ready:
                        yield receive.result()
                    elif queue.empty():
                        await task
                        break
                finally:
                    if not receive.done():
                        receive.cancel()
                        with suppress(asyncio.CancelledError):
                            await receive
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    @staticmethod
    def _result(node):
        totals = node.get('observed_usage', usage_sum(node['usage'], node['child_usage']))
        usage = LLMUsage(**totals) if any(value is not None for value in totals.values()) else None
        stored = node.get('result')
        final = None if stored is None else LLMResult(**stored, usage=usage)
        return ToolRunResult(
            node['context']['run_id'],
            node['status'],
            final,
            usage,
            tuple(node['calls'].values()),
            node.get('reason'),
            node['usage_complete'] and node['status'] == 'completed',
        )

    def _native_tools(self, request, token_ref, active):
        native = []

        def bind(_definition):
            async def execute(**arguments):
                call = active.get('call')
                if call is None or call.name != _definition.name:
                    raise ToolRunConflict('Native tool call identity is unavailable')
                token = token_ref['token']
                context = ToolCallContext(
                    request.context,
                    call.id,
                    digest(
                        [
                            request.context.user_id,
                            request.context.subject_id,
                            request.context.root_run_id,
                            request.context.run_id,
                            call.id,
                        ]
                    ),
                    _definition.name,
                    _definition.version,
                )
                await self.ledger.check(request.context, token)
                await self.ledger.reserve(
                    request.context,
                    token,
                    'tool:' + call.id,
                    kind='tool',
                    identity={
                        'name': _definition.name,
                        'version': _definition.version,
                        'arguments_digest': digest(arguments),
                        'idempotency_key': context.idempotency_key,
                    },
                )
                dispatched = False
                try:
                    if self.authorize is not None:
                        permission = self.authorize(_definition, plain(arguments), context)
                        if inspect.isawaitable(permission):
                            permission = await permission
                        if permission is not True:
                            raise PermissionError('Tool resource authorization denied')
                    await self.ledger.check(request.context, token)
                    dispatched = True
                    if inspect.iscoroutinefunction(_definition.handler):
                        outcome = await _definition.handler(plain(arguments), context)
                    else:
                        outcome = await asyncio.to_thread(_definition.handler, plain(arguments), context)
                        if inspect.isawaitable(outcome):
                            outcome = await outcome
                    if not isinstance(outcome, ToolOutcome):
                        outcome = ToolOutcome(outcome)
                    if _definition.effect == 'command' and not outcome.receipt_ref:
                        raise ToolRunConflict('A side-effect tool must return its owner-issued receipt')
                    encoded = json.dumps(plain(outcome.value), ensure_ascii=False, allow_nan=False)
                    if len(encoded.encode()) > self.max_output_bytes:
                        raise ToolRunStopped('Tool output exceeds the configured bound')
                    if outcome.receipt_ref is not None and (
                        not isinstance(outcome.receipt_ref, str) or len(outcome.receipt_ref) > 2048
                    ):
                        raise ToolRunConflict('Invalid owner-issued receipt reference')
                    await self.ledger.settle(request.context, token, 'tool:' + call.id, receipt_ref=outcome.receipt_ref)
                    return plain(outcome.value)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    with suppress(Exception):
                        await self.ledger.settle(
                            request.context,
                            token,
                            'tool:' + call.id,
                            status='unknown' if dispatched and _definition.effect == 'command' else 'failed',
                        )
                    raise

            return execute

        for definition in request.tools:
            execute = bind(definition)

            # AION requires named Python parameters. Publish an exact signature
            # while keeping server context out of the model's argument schema.
            parameters = plain(definition.parameters)
            required = set(parameters.get('required', ()))
            signature = []
            for name, schema in parameters.get('properties', {}).items():
                if name not in required and 'default' not in schema:
                    raise ValueError('Optional tool parameters must declare a schema default')
                default = inspect.Parameter.empty if name in required else schema['default']
                signature.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default))
            execute.__signature__ = inspect.Signature(signature)
            native.append(
                Tool(execute, name=definition.name, description=definition.description, parameters=parameters)
            )
        return native

    async def _run(self, request, emit):
        token_ref, active = {}, {}
        tools = self._native_tools(request, token_ref, active)
        llm = request.llm_request
        system = self.gateway.render_prompt(llm.profile)
        fingerprint = digest(
            {
                'content': llm.effective_content,
                'profile': asdict(llm.profile),
                'system': system,
                'thinking': llm.effective_thinking,
                'reasoning_effort': llm.effective_reasoning_effort,
                'provider_model': self.gateway.request_model(llm.profile),
                'tools': [
                    {
                        'name': item.name,
                        'version': item.version,
                        'effect': item.effect,
                        'description': item.description,
                        'parameters': plain(item.parameters),
                    }
                    for item in request.tools
                ],
                'allowed_tools': sorted(request.allowed_tools),
            }
        )
        await self.ledger.admit(request.context, request.budget, fingerprint)
        token = await self.ledger.claim(request.context)
        if token is None:
            snapshot = await self.ledger.snapshot(request.context)
            if snapshot['status'] == 'running':
                raise ToolRunConflict('The run is already owned by another execution')
            await emit(ToolRunEvent('completed', request.context.run_id, result=self._result(snapshot)))
            return
        token_ref['token'] = token
        trace = ToolTrace()
        core = self._retain(asyncio.create_task(self._drive(request, token, tools, trace, active, emit, system)))
        watcher = asyncio.create_task(self._watch(request.context, token))
        try:
            ready, _ = await asyncio.wait((core, watcher), return_when=asyncio.FIRST_COMPLETED)
            if watcher in ready:
                await watcher
            final = await core
            node = await self.ledger.finish(
                request.context,
                token,
                status='completed',
                result={
                    'content': final.content,
                    'reasoning_content': final.reasoning_content,
                    'provider': final.provider,
                    'model': final.model,
                },
            )
        except (Exception, asyncio.CancelledError) as error:
            core.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(core), timeout=1)
            cause = error
            while cause.__cause__ is not None:
                cause = cause.__cause__
            cancelled = isinstance(error, asyncio.CancelledError) or (
                isinstance(cause, ToolRunStopped) and cause.code == 'cancelled'
            )
            status = 'cancelled' if cancelled else 'failed'
            with suppress(Exception):
                await self.ledger.finish(request.context, token, status=status, reason=type(error).__name__)
            if isinstance(error, asyncio.CancelledError):
                raise
            node = await self.ledger.snapshot(request.context)
            if node['status'] == 'running':
                # Expired ownership is a terminal unknown unit, never a claim
                # to resume a native tool step. Valid competing owners stay live.
                await self.ledger.claim(request.context)
                node = await self.ledger.snapshot(request.context)
                if node['status'] == 'running':
                    raise
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await watcher
        node = await self.ledger.snapshot(request.context)
        await emit(ToolRunEvent('completed', request.context.run_id, result=self._result(node)))

    async def _watch(self, context, token):
        while True:
            await asyncio.sleep(min(self.poll_seconds, self.ledger.lease_seconds / 3))
            await self.ledger.check(context, token)

    async def _drive(self, request, token, tools, trace, active, emit, system):
        llm = request.llm_request
        session = await self.gateway._create_session(llm, rendered_prompt=system)
        stream = self.gateway._stream_tools(
            llm,
            session,
            tools=tools,
            trace=trace,
            options=ToolOptions(
                max_rounds=max(1, request.budget.max_model_calls), max_calls=max(1, request.budget.max_tool_calls)
            ),
        )
        next_model, model_pending = 1, True
        try:
            while True:
                await self.ledger.check(request.context, token)
                if model_pending:
                    await self.ledger.reserve(
                        request.context, token, f'model:{next_model}', kind='model', identity={'step': next_model}
                    )
                    model_pending = False
                try:
                    event = await anext(stream)
                except StopAsyncIteration:
                    raise ToolRunConflict('Native tool stream ended without a final result') from None
                if event.kind == 'model_done':
                    await self.ledger.settle(
                        request.context, token, f'model:{event.step_index}', usage=usage_dict(event.usage)
                    )
                    await emit(ToolRunEvent('model_done', request.context.run_id, event.step_index))
                    if event.outcome == 'tools':
                        calls = trace.snapshot().steps[-1].calls
                        if not calls:
                            raise ToolRunConflict('Tool response has no recorded calls')
                        active['remaining'] = len(calls)
                        next_model = event.step_index + 1
                elif event.kind == 'tool_start':
                    active['call'] = event.call
                    await emit(
                        ToolRunEvent(
                            'tool_started', request.context.run_id, event.step_index, event.call.id, event.call.name
                        )
                    )
                elif event.kind == 'tool_result':
                    active['remaining'] -= 1
                    model_pending = active['remaining'] == 0
                    await emit(
                        ToolRunEvent(
                            'tool_finished', request.context.run_id, event.step_index, event.call.id, event.call.name
                        )
                    )
                elif event.kind == 'completed':
                    normalized = self.gateway._normalize_result(session.client, event.result)
                    # ChatResult.usage is the last response; ToolTrace covers every
                    # completed native round. Child usage is added by the ledger.
                    totals = usage_dict(trace.snapshot().usage)
                    return replace(
                        normalized, usage=None if totals is None else LLMUsage(**totals), transport_retries=None
                    )
                elif event.kind != 'model_delta':
                    raise ToolRunConflict('Unknown native tool event')
        finally:
            await stream.aclose()
