"""用户健康画像循证检索 Graph 的无网络测试。"""

from __future__ import annotations

import unittest

from langgraph.checkpoint.memory import InMemorySaver

from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents import AgentRuntime
from agents import GraphRegistry
from agents.graphs.health_profile import PROFILE_EVIDENCE_CAPABILITY
from agents.graphs.health_profile import PROFILE_EVIDENCE_GRAPH_VERSION
from agents.graphs.health_profile import register_profile_evidence
from agents.knowledge import KnowledgeDocument
from agents.knowledge import KnowledgeSource
from agents.llm import ModelProfileRegistry


class FakeKnowledgeGateway:
    def __init__(self, *, enabled=True, documents=(), error=None):
        self._enabled = enabled
        self.documents = tuple(documents)
        self.error = error
        self.queries = []

    @property
    def enabled(self):
        return self._enabled

    async def retrieve(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.documents


def _build_runtime(gateway):
    registry = GraphRegistry()
    runtime = AgentRuntime(
        hub=AgentHub(registry),
        registry=registry,
        profiles=ModelProfileRegistry(),
        llm_gateway=object(),
        context_repository=object(),
        lease_manager=object(),
        checkpointer=InMemorySaver(),
        _close_callback=lambda: None,
        knowledge_gateway=gateway,
    )
    registration = register_profile_evidence(runtime)
    return runtime, registration


class ProfileEvidenceGraphTest(unittest.IsolatedAsyncioTestCase):
    async def test_retrieves_real_document_without_llm_generated_grades(self):
        gateway = FakeKnowledgeGateway(documents=(KnowledgeDocument(
            source=KnowledgeSource.GUIDE,
            title='肝功能异常评估指南',
            content='肝酶异常需要结合病史、用药和复查结果综合评估。',
            url='https://example.test/guide-1',
            metadata={
                'id': 'guide-1',
                'publisher': '测试医学会',
                'year': '2025',
                'knowledgeBaseRevision': 'kb-test-1',
            },
        ),))
        runtime, registration = _build_runtime(gateway)

        result = await runtime.hub.invoke(
            PROFILE_EVIDENCE_CAPABILITY,
            AgentRequest({'facts': [{
                'factId': 'fact-alt',
                'factType': 'lab.alt',
                'category': 'laboratory',
                'displayName': '丙氨酸氨基转移酶',
                'interpretation': 'high',
            }]}),
            AgentRunContext.create(
                user_id='user-1',
                session_id='health-profile:subject-1',
                subject_id='subject-1',
                request_id='request-1',
                run_id='run-1',
            ),
        )

        evidence = result.data['evidence']
        # 该测试未提供 LLM 网关，综合解释不可用，不再降级为摘要态条目。
        self.assertEqual(evidence['status'], 'empty')
        self.assertEqual(evidence['knowledgeBaseRevision'], 'kb-test-1')
        self.assertEqual(evidence['items'], [])
        self.assertEqual(gateway.queries[0].source, KnowledgeSource.GUIDE)
        self.assertEqual(
            gateway.queries[0].query,
            '丙氨酸氨基转移酶 异常',
        )
        self.assertEqual(
            registration.version,
            PROFILE_EVIDENCE_GRAPH_VERSION,
        )
        self.assertIsNone(registration.graph.checkpointer)

    async def test_disabled_and_failed_gateway_degrade_explicitly(self):
        for gateway in (
            FakeKnowledgeGateway(enabled=False),
            FakeKnowledgeGateway(error=RuntimeError('temporary failure')),
        ):
            with self.subTest(enabled=gateway.enabled):
                runtime, _ = _build_runtime(gateway)
                result = await runtime.hub.invoke(
                    PROFILE_EVIDENCE_CAPABILITY,
                    AgentRequest({'facts': [{
                        'factId': 'fact-1',
                        'factType': 'condition.hypertension',
                        'category': 'condition',
                        'displayName': '高血压',
                        'interpretation': None,
                    }]}),
                    AgentRunContext.create(
                        user_id='user-1',
                        session_id='health-profile:subject-1',
                    ),
                )
                self.assertEqual(
                    result.data['evidence']['status'],
                    'unavailable',
                )
                self.assertEqual(result.data['evidence']['items'], [])


if __name__ == '__main__':
    unittest.main()
