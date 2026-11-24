"""Exercise current handlers/services/graphs with real APIs and memory persistence.

Run manually. Reads model and optional knowledge credentials from JSON stdin;
never stores them. Real model/knowledge calls may incur usage. Uses synthetic
patients, no MongoDB/Redis/profile workers, and no report generation or callbacks.
This measures local HTTP + application + upstream latency, not deployed latency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.api.scripts.qa_latency import measure
from tests.api.scripts.qa_latency import milliseconds
from tests.unit._support.harnesses.record_dialogue_acceptance import MemoryLeases
from tests.unit._support.harnesses.record_dialogue_acceptance import MemorySessions
from tests.unit._support.harnesses.record_dialogue_acceptance import isolated_environment
from utils.artifact_paths import external_artifact_path


class MemoryChatSessions(MemorySessions):
    async def save(self, user_id, session_id, session, *, upsert=True):
        self.rows[user_id, session_id] = deepcopy(session)
        return True

    async def list_session_ids(self, user_id):
        return [session_id for owner, session_id in self.rows if owner == user_id]


class TimedGateway:
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = []

    def begin(self, request, mode):
        row = {
            'profile': request.profile.name,
            'thinking': request.effective_thinking,
            'input_chars': len(request.effective_content),
            'mode': mode,
            'started_monotonic': time.perf_counter(),
        }
        if hasattr(self.delegate, 'describe'):
            row.update(self.delegate.describe(request.profile.name))
        self.calls.append(row)
        return row

    async def complete(self, request):
        row = self.begin(request, 'complete')
        try:
            result = await self.delegate.complete(request)
            row['output_chars'] = len(result.content)
            row['usage'] = result.usage.to_dict() if result.usage else None
            # AION normalizes this from its client configuration, not the wire response.
            row['configured_result_model'] = result.model
            return result
        except BaseException as error:
            row['error_type'] = type(error).__name__
            raise
        finally:
            row['elapsed_ms'] = milliseconds(row['started_monotonic'])

    async def stream(self, request):
        row = self.begin(request, 'stream')
        row['output_chars'] = 0
        try:
            async for chunk in self.delegate.stream(request):
                if chunk.content:
                    row.setdefault('first_content_ms', milliseconds(row['started_monotonic']))
                    row['output_chars'] += len(chunk.content)
                if chunk.reasoning_content:
                    row.setdefault('first_reasoning_ms', milliseconds(row['started_monotonic']))
                if chunk.usage:
                    row['usage'] = chunk.usage.to_dict()
                yield chunk
        except BaseException as error:
            row['error_type'] = type(error).__name__
            raise
        finally:
            row['elapsed_ms'] = milliseconds(row['started_monotonic'])


class TimedKnowledge:
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = []

    @property
    def enabled(self):
        return self.delegate.enabled

    async def retrieve(self, query):
        row = {'source': query.source.value, 'started_monotonic': time.perf_counter()}
        self.calls.append(row)
        try:
            documents = await self.delegate.retrieve(query)
            row['document_count'] = len(documents)
            return documents
        except BaseException as error:
            row['error_type'] = type(error).__name__
            raise
        finally:
            row['elapsed_ms'] = milliseconds(row['started_monotonic'])


class ProfileGateway:
    """Route benchmark profiles without changing production model selection."""

    def __init__(self, default, overrides, settings):
        self.default = default
        self.overrides = overrides
        self.settings = settings

    def describe(self, name):
        config = self.settings.get('profile_overrides', {}).get(name, self.settings)
        return {key: config[key] for key in ('provider', 'model')}

    def select(self, request):
        config = self.settings.get('profile_overrides', {}).get(request.profile.name, self.settings)
        options = {**request.profile.model_kwargs, **config.get('model_kwargs', {})}
        profile = replace(request.profile, model=config['model'], model_kwargs=options)
        return self.overrides.get(profile.name, self.default), replace(request, profile=profile)

    async def complete(self, request):
        gateway, routed = self.select(request)
        return await gateway.complete(routed)

    async def stream(self, request):
        gateway, routed = self.select(request)
        async for chunk in gateway.stream(routed):
            yield chunk


def build_gateway(settings, contexts, clients):
    import aion

    from agents.llm.aion_gateway import AionLLMGateway

    def build(config):
        # Do not inherit a key across providers by merging partially specified routes.
        if not all(config.get(key) for key in ('provider', 'model', 'api_key')):
            raise ValueError('Every model route requires its own provider, model and api_key')

        def factory(*args, **kwargs):
            if len(clients) >= 40:
                raise RuntimeError('Live measurement call budget exhausted')
            client = aion.create_chat_client(*args, **kwargs)
            clients.append(client)
            if config.get('base_url'):
                client.client.base_url = config['base_url']
                client.async_client.base_url = config['base_url']
            return client

        return AionLLMGateway(
            provider=config['provider'],
            api_key=config['api_key'],
            default_model=config['model'],
            context_repository=contexts,
            client_factory=factory,
        )

    default = build(settings)
    overrides = {name: build(config) for name, config in settings.get('profile_overrides', {}).items()}
    return TimedGateway(ProfileGateway(default, overrides, settings))


async def run(settings, output):
    output = external_artifact_path(output)
    import tornado.httpserver
    import tornado.netutil
    import tornado.web
    from langgraph.checkpoint.memory import InMemorySaver

    from adapters.record import AionPromptRenderer
    from agents import AgentHub
    from agents import AgentRuntime
    from agents import GraphRegistry
    from agents.graphs.chat import register_chat_reply
    from agents.graphs.record import register_record_graphs
    from agents.knowledge import EviMedKnowledgeGateway
    from agents.llm import ModelProfileRegistry
    from modules.chat.application.service import ChatSessionManager
    from modules.record.application.service import QASessionManager
    from tests.unit._support.agents import InMemoryLLMContextRepository
    from tests.unit.services.chat.test_chat_service_agent import FakeCustomMessageOutbox
    from utils.knowledge import Knowledge
    from web.handlers.chat import ChatSendStreamHandler
    from web.handlers.health import HealthCheckHandler
    from web.handlers.record import QASessionAnswerStreamHandler
    from web.handlers.record import QASessionCreateHandler
    from web.handlers.record import QASessionGetHandler

    clients = []

    contexts = InMemoryLLMContextRepository()
    gateway = build_gateway(settings, contexts, clients)
    knowledge = TimedKnowledge(EviMedKnowledgeGateway(Knowledge(api_key=settings.get('knowledge_api_key', ''))))
    registry = GraphRegistry()
    runtime = AgentRuntime(
        hub=AgentHub(registry),
        registry=registry,
        profiles=ModelProfileRegistry(),
        llm_gateway=gateway,
        context_repository=contexts,
        lease_manager=MemoryLeases(),
        checkpointer=InMemorySaver(),
        knowledge_gateway=knowledge,
        _close_callback=lambda: None,
    )
    register_record_graphs(runtime, question_topics=json.loads(Path('config/question_topics.json').read_text('utf-8')))
    register_chat_reply(runtime)
    record = QASessionManager(
        runtime,
        session_repository=MemorySessions(),
        session_cache=MemorySessions(),
        prompt_renderer=AionPromptRenderer(),
    )
    rejections = []
    original_rejection = record._logGenerationRejection

    def observe_rejection(*args, **kwargs):
        diagnostics = original_rejection(*args, **kwargs)
        rejections.append({'started_monotonic': time.perf_counter(), **diagnostics})
        return diagnostics

    record._logGenerationRejection = observe_rejection
    chat = ChatSessionManager(
        runtime, session_repository=MemoryChatSessions(), custom_message_outbox=FakeCustomMessageOutbox()
    )
    application = tornado.web.Application(
        [
            (r'/health', HealthCheckHandler),
            (r'/api/v1/chat/send/stream', ChatSendStreamHandler, {'service': chat}),
            (r'/api/v1/qa/create', QASessionCreateHandler, {'service': record}),
            (r'/api/v1/qa/status', QASessionGetHandler, {'service': record}),
            (r'/api/v1/qa/answer/stream', QASessionAnswerStreamHandler, {'service': record}),
        ]
    )
    sockets = tornado.netutil.bind_sockets(0, address='127.0.0.1')
    server = tornado.httpserver.HTTPServer(application)
    server.add_sockets(sockets)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    args = SimpleNamespace(
        base_url=f'http://127.0.0.1:{sockets[0].getsockname()[1]}',
        output=output,
        revision=revision,
        record_only=settings.get('record_only', False),
    )
    try:
        async with asyncio.timeout(600):
            await measure(args)
    finally:
        server.stop()
        await server.close_all_connections()
        for client in clients:
            await client.async_client.close()
            client.client.close()
        path = Path(output)
        report = json.loads(path.read_text('utf-8')) if path.exists() else {'rows': []}
        report.update(
            provider=settings['provider'],
            model=settings['model'],
            knowledge_enabled=knowledge.enabled,
            storage='in-memory; MongoDB/Redis/profile loads and deployment network excluded',
            model_calls=gateway.calls,
            knowledge_calls=knowledge.calls,
            validation_rejections=rejections,
            profile_models={
                name: gateway.delegate.describe(name)
                for name in ('record.plan', 'record.question', 'chat.reply', 'knowledge.route')
            },
            parameter_overrides={
                name: config.get('model_kwargs', {})
                for name, config in [('default', settings), *settings.get('profile_overrides', {}).items()]
            },
        )
        for row in report['rows']:
            started = row['started_monotonic']
            ended = started + row['total_ms'] / 1000
            row['model_calls'] = [call for call in gateway.calls if started <= call['started_monotonic'] <= ended]
            row['knowledge_calls'] = [call for call in knowledge.calls if started <= call['started_monotonic'] <= ended]
            row['validation_rejections'] = [
                item for item in rejections if started <= item['started_monotonic'] <= ended
            ]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    settings = json.load(sys.stdin)
    logging.disable(logging.CRITICAL)
    with patch.dict(os.environ, isolated_environment()), patch('loongspears.utils.env.load_env', return_value=None):
        asyncio.run(run(settings, args.output))
