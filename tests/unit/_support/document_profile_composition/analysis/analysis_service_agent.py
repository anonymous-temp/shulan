"""Shared synthetic fixture closure extracted from tests.unit.services.analysis.test_analysis_service_agent."""

from __future__ import annotations

from copy import deepcopy

from langgraph.checkpoint.memory import InMemorySaver

from agents import AgentHub
from agents import AgentRuntime
from agents import GraphRegistry
from agents.graphs.analysis import ANALYSIS_HEALTH_REPORT_CAPABILITY
from agents.llm import ModelProfileRegistry


class FakeDocMindClient:
    def __init__(self, result=None, submit_error=None):
        self.result = result or {'success': True, 'text': '报告正文'}
        self.submit_error = submit_error
        self.submit_calls = []
        self.result_calls = []

    def submit_online_file(self, file_url, *, file_name=None, file_type=None):
        self.submit_calls.append((file_url, file_name, file_type))
        if self.submit_error is not None:
            raise self.submit_error
        return 'task-1'

    def get_result(self, task_id, *, poll_interval):
        self.result_calls.append((task_id, poll_interval))
        return deepcopy(self.result)


class RecordingAnalysisGraph:
    def __init__(self, content):
        self.content = content
        self.calls = []

    async def ainvoke(self, input, config=None, *, context=None, **kwargs):
        self.calls.append(
            {
                'input': deepcopy(input),
                'config': deepcopy(config),
                'context': context,
            }
        )
        return {
            **input,
            'content': self.content,
            'status': 'completed',
        }


def _build_runtime(content):
    graph = RecordingAnalysisGraph(content)
    registry = GraphRegistry()
    registry.register(
        ANALYSIS_HEALTH_REPORT_CAPABILITY,
        graph,
        version='analysis-health-report-v1',
    )
    runtime = AgentRuntime(
        hub=AgentHub(registry),
        registry=registry,
        profiles=ModelProfileRegistry(),
        llm_gateway=object(),
        context_repository=object(),
        lease_manager=object(),
        checkpointer=InMemorySaver(),
        _close_callback=lambda: None,
    )
    return runtime, graph
