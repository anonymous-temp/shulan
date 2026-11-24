"""Regression coverage for the ordinary intake fast path and explicit thinking."""

import json

import pytest

from agents import AgentRequest
from agents import AgentRunContext
from agents.graphs.record import RECORD_QUESTION_CAPABILITY
from agents.llm import LLMResult
from tests.unit._support.agents import FakeKnowledgeGateway
from tests.unit._support.agents import FakeLLMGateway

from .test_dynamic_record import PatientGateway
from .test_record_agent import _build_runtime
from .test_record_agent import _load_record_service


@pytest.mark.asyncio
async def test_explicit_empty_retrieval_query_skips_router_with_knowledge_enabled():
    gateway = FakeLLMGateway(complete_result=LLMResult(content='{"question":"具体有哪些不适？"}'))
    knowledge = FakeKnowledgeGateway()
    runtime = _build_runtime(gateway, knowledge_gateway=knowledge)

    result = await runtime.hub.invoke(
        RECORD_QUESTION_CAPABILITY,
        AgentRequest({'prompt': '继续收集症状', 'retrieval_query': ''}),
        AgentRunContext.create(user_id='u', session_id='s'),
    )

    assert result.data['retrieval_status'] == 'skipped'
    assert [request.profile.name for request in gateway.complete_requests] == ['record.question']
    assert knowledge.queries == []


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('thinking', [None, True])
@pytest.mark.parametrize('effort', [None, 'low', 'high', 'max'])
async def test_intake_uses_two_models_and_preserves_explicit_thinking(streaming, thinking, effort):
    gateway = PatientGateway()
    knowledge = FakeKnowledgeGateway()
    runtime = _build_runtime(gateway, knowledge_gateway=knowledge)
    module = _load_record_service()
    manager = module.QASessionManager(runtime)
    current = await manager.createSession('u', 's')
    assert [request.profile.name for request in gateway.requests] == ['record.plan', 'record.question']
    assert not any(request.effective_thinking for request in gateway.requests)
    gateway.requests.clear()
    options = {} if thinking is None else {'thinking': thinking}
    if effort is not None:
        options['reasoning_effort'] = effort

    if streaming:
        events = [
            event async for event in manager.submitAnswerStream('u', 's', current['questionId'], '餐后腹胀', **options)
        ]
        assert events[-1]['data']['type'] == 'finish'
        assert any(event['data']['type'] == 'reasoning' for event in events) is bool(thinking)
    else:
        result = await manager.submitAnswer('u', 's', current['questionId'], '餐后腹胀', **options)
        assert bool(result['reasoning']) is bool(thinking)

    assert [request.profile.name for request in gateway.requests] == ['record.plan', 'record.question']
    assert gateway.requests[0].effective_thinking is bool(thinking)
    assert gateway.requests[0].effective_reasoning_effort == (effort or 'low')
    assert gateway.requests[1].effective_thinking is False
    assert knowledge.queries == []


@pytest.mark.parametrize('abnormal', [False, True])
def test_retrieval_keeps_new_concerns_and_abnormal_report_but_omits_unrelated_history(abnormal):
    module = _load_record_service()
    patient = {
        '报告总体情况': '存在异常提示' if abnormal else '未见异常提示',
        '本次报告': '血脂异常',
        '本次回答': '应该注意哪些饮食？',
        '已问过的内容': ['不需要送给检索路由的长历史'],
    }
    decision = {
        'action': 'ask',
        'responses': [] if abnormal else [{'concernId': 'c1', 'text': '饮食注意事项', 'resolution': 'answered'}],
    }

    query = module.QASessionManager._presentationRetrievalQuery(patient, decision)

    data = json.loads(query)
    assert data['responseTask'] == decision
    assert data['本次回答'] == patient['本次回答']
    assert data['异常报告资料'] == ('血脂异常' if abnormal else None)
    assert '已问过的内容' not in query


def test_already_answered_concern_does_not_repeat_retrieval():
    module = _load_record_service()
    query = module.QASessionManager._presentationRetrievalQuery(
        {}, {'responses': [{'concernId': 'c1', 'reuseResponse': '已确认的回答'}]}
    )
    assert query == ''
