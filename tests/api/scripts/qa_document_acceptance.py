"""Live upload-to-first-question acceptance with explicit model and DocMind opt-in."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.unit._support.harnesses import qa_document_acceptance as harness

ROOT = Path(__file__).resolve().parents[3]


def live_dependencies(settings, max_calls):
    import aion
    from langgraph.checkpoint.memory import InMemorySaver

    from adapters.analysis.docmind import BoundedDocMindClient
    from agents import AgentHub
    from agents import AgentRuntime
    from agents import GraphRegistry
    from agents.graphs.analysis import register_analysis_health_report
    from agents.graphs.record import register_record_graphs
    from agents.llm import ModelProfileRegistry
    from agents.llm.aion_gateway import AionLLMGateway
    from tests.unit.agents.graphs.record.test_record_agent import FakeLeaseManager

    clients = []

    def factory(*args, **kwargs):
        if len(clients) >= max_calls:
            raise RuntimeError('acceptance model-call budget exhausted')
        client = aion.create_chat_client(*args, **kwargs)
        clients.append(client)
        return client

    class CountedDocMind(BoundedDocMindClient):
        submissions = 0
        submit_ms = 0.0
        wait_ms = 0.0

        def submit_local_file(self, *args, **kwargs):
            if self.submissions:
                raise AssertionError('acceptance permits only one DocMind submission')
            self.submissions += 1
            started = time.perf_counter()
            try:
                return super().submit_local_file(*args, **kwargs)
            finally:
                self.submit_ms += (time.perf_counter() - started) * 1000

        def get_result(self, *args, **kwargs):
            started = time.perf_counter()
            try:
                return super().get_result(*args, **kwargs)
            finally:
                self.wait_ms += (time.perf_counter() - started) * 1000

        def submit_online_file(self, *args, **kwargs):
            raise AssertionError('acceptance must upload the downloaded bytes')

    gateway = AionLLMGateway(
        provider=settings['SHULAN_LLM_PROVIDER'],
        api_key=settings['SHULAN_LLM_API_KEY'],
        default_model=settings['SHULAN_LLM_DEFAULT_MODEL'],
        client_factory=factory,
    )
    registry = GraphRegistry()
    runtime = AgentRuntime(
        hub=AgentHub(registry),
        registry=registry,
        profiles=ModelProfileRegistry(),
        llm_gateway=gateway,
        context_repository=object(),
        lease_manager=FakeLeaseManager(),
        checkpointer=InMemorySaver(),
        _close_callback=lambda: None,
    )
    register_analysis_health_report(runtime)
    register_record_graphs(
        runtime,
        question_topics=json.loads((ROOT / 'config/question_topics.json').read_text(encoding='utf-8')),
    )
    parser = CountedDocMind(
        access_key_id=settings['SHULAN_ALIYUN_DOCMIND_ACCESSKEY_ID'],
        access_key_secret=settings['SHULAN_ALIYUN_DOCMIND_ACCESSKEY_SECRET'],
        endpoint=settings['SHULAN_ALIYUN_DOCMIND_ENDPOINT'],
    )
    return runtime, clients, parser



def configure_live_dependencies(documents, qa, settings, max_calls):
    from adapters.analysis.document_download import HttpDocumentDownloader

    runtime, clients, parser = live_dependencies(settings, max_calls)
    documents.interpreter._agent_runtime = runtime
    documents.interpreter.docmind_client = parser
    documents.coordinator.downloader = HttpDocumentDownloader()
    qa.manager._agent_runtime = runtime
    return clients, parser


def main():
    return harness.main(online=True, dependency_factory=configure_live_dependencies)


if __name__ == '__main__':
    raise SystemExit(main())
