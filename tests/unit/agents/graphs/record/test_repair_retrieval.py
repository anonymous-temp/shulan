"""Reuse this turn's retrieval outcome during bounded presentation repair."""

import pytest

from agents import AgentRequest
from agents import AgentRunContext
from agents.graphs.record import RECORD_QUESTION_CAPABILITY
from agents.knowledge import KnowledgeDocument
from agents.knowledge import KnowledgeSource
from agents.llm import LLMResult
from tests.unit._support.agents import FakeKnowledgeGateway
from tests.unit._support.agents import FakeLLMGateway

from .test_dynamic_record import PatientGateway
from .test_dynamic_record import assessment
from .test_dynamic_record import cached
from .test_dynamic_record import concern
from .test_dynamic_record import supplement
from .test_record_agent import _build_runtime
from .test_record_agent import _load_record_service


async def invoke(runtime, **inputs):
    return await runtime.hub.invoke(
        RECORD_QUESTION_CAPABILITY,
        AgentRequest({'prompt': '仅修正本轮回答格式。', 'thinking': False, **inputs}),
        AgentRunContext.create(user_id='synthetic-u', session_id='synthetic-s'),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('retain', [False, True])
async def test_retrieval_context_is_returned_only_with_explicit_internal_opt_in(retain):
    gateway = FakeLLMGateway(
        complete_results=[
            LLMResult(content='{"needRetrieval":true,"queries":[{"source":"guide","query":"饮食背景"}]}'),
            LLMResult(content='{"answer":"synthetic"}'),
            LLMResult(content='{"answer":"repaired"}'),
        ]
    )
    knowledge = FakeKnowledgeGateway(
        {
            KnowledgeSource.GUIDE: [
                KnowledgeDocument(source=KnowledgeSource.GUIDE, title='合成资料', content='了解饮食背景。')
            ]
        }
    )
    runtime = _build_runtime(gateway, knowledge_gateway=knowledge)
    first = await invoke(runtime, retain_knowledge_context=retain)
    context = gateway.complete_requests[-1].supplemental_context
    assert context and '合成资料' in context
    assert first.data['knowledge_context'] == (context if retain else None)
    if retain:
        repaired = await invoke(runtime, reuse_knowledge=True, knowledge_context=first.data['knowledge_context'])
        assert repaired.data['retrieval_status'] == 'reused'
        assert repaired.data['knowledge_context'] is None
        assert gateway.complete_requests[-1].supplemental_context == context
        assert len(gateway.complete_requests) == 3
        assert len(knowledge.queries) == 1


@pytest.mark.asyncio
async def test_repair_of_empty_retrieval_result_does_not_route_or_search_again():
    gateway = FakeLLMGateway(complete_results=[LLMResult(content='{}')])
    knowledge = FakeKnowledgeGateway({})
    runtime = _build_runtime(gateway, knowledge_gateway=knowledge)
    result = await invoke(runtime, reuse_knowledge=True, knowledge_context=None)
    assert result.data['retrieval_status'] == 'reused'
    assert gateway.complete_requests[0].profile.name == RECORD_QUESTION_CAPABILITY
    assert gateway.complete_requests[0].supplemental_context is None
    assert len(gateway.complete_requests) == 1
    assert knowledge.queries == []


@pytest.mark.asyncio
async def test_default_invocation_discards_incoming_context_without_explicit_reuse():
    gateway = FakeLLMGateway(complete_results=[LLMResult(content='{}')])
    runtime = _build_runtime(gateway, knowledge_gateway=FakeKnowledgeGateway({}))
    result = await invoke(runtime, retrieval_query='', knowledge_context='上一次无关的资料')
    assert result.data['knowledge_context'] is None
    assert result.data['retrieval_status'] == 'skipped'
    assert gateway.complete_requests[0].supplemental_context is None


@pytest.mark.asyncio
async def test_service_expression_repair_reuses_evidence_and_routes_only_once():
    class RoutedPatientGateway(PatientGateway):
        async def complete(self, request):
            if request.profile.name == 'knowledge.route':
                self.requests.append(request)
                return LLMResult(content='{"needRetrieval":true,"queries":[{"source":"guide","query":"饮食背景"}]}')
            return await super().complete(request)

    gateway = RoutedPatientGateway()
    knowledge = FakeKnowledgeGateway(
        {
            KnowledgeSource.GUIDE: [
                KnowledgeDocument(source=KnowledgeSource.GUIDE, title='合成资料', content='饮食背景。')
            ]
        }
    )
    runtime = _build_runtime(gateway, knowledge_gateway=knowledge)
    module = _load_record_service()
    manager = module.QASessionManager(runtime)
    current = await supplement(manager, gateway)
    answer = '餐后腹胀，需要注意哪些饮食问题？'
    gateway.next_assessment = assessment(concerns=[concern(answer)])
    gateway.invalid_response = 1
    calls = len(gateway.requests)
    result = await manager.submitAnswer('u', 's', current['questionId'], answer)
    requests = gateway.requests[calls:]
    assert result['status'] == 'question'
    assert [item.profile.name for item in requests] == [
        'record.plan',
        'knowledge.route',
        'record.question',
        'record.question',
    ]
    assert len(knowledge.queries) == 1
    assert requests[-2].supplemental_context
    assert requests[-1].supplemental_context == requests[-2].supplemental_context
    assert cached(module)['qaFlow']['generationQuality']['mode'] == 'repaired'
