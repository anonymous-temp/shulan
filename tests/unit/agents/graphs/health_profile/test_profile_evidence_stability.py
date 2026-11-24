"""同一输入重复读取不应重新生成已验证结果；临时失败仍可恢复。"""

import json

import pytest

from agents.llm.types import LLMResult
from tests.unit.agents.graphs.health_profile.test_profile_interpretation import ScenarioKnowledge
from tests.unit.agents.graphs.health_profile.test_profile_interpretation import ScenarioLLM
from tests.unit.agents.graphs.health_profile.test_profile_interpretation import case_facts
from tests.unit.agents.graphs.health_profile.test_profile_interpretation import interpreter
from tests.unit.agents.graphs.health_profile.test_profile_interpretation import run


class AlternatingLLM(ScenarioLLM):
    def __init__(self):
        super().__init__()
        self.synthesis_calls = 0

    async def complete(self, request):
        result = await super().complete(request)
        if request.profile.name != 'profile.synthesize_evidence':
            return result
        self.synthesis_calls += 1
        output = json.loads(result.content)
        if self.synthesis_calls % 2 == 0:
            output['insights'] = []
        else:
            output['insights'][-1]['supports'][0]['quote'] = 'This fabricated quote is absent from the source.'
        return LLMResult(content=json.dumps(output, ensure_ascii=False))


@pytest.mark.asyncio
async def test_partial_valid_result_does_not_alternate_with_empty_on_read():
    llm = AlternatingLLM()
    knowledge = ScenarioKnowledge()
    instance = interpreter(knowledge, llm)
    outputs = [await run(instance, case_facts()[1:3], user_id='001') for _ in range(3)]
    assert all(output['evidence']['status'] == 'ready' for output in outputs)
    assert all(output['complete'] is False for output in outputs)
    assert outputs[0] == outputs[1] == outputs[2]
    assert len(llm.calls) == 2
    assert len(knowledge.queries) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['no_relevant', 'no_documents', 'no_original', 'no_topics'])
async def test_successful_empty_result_is_reused(kind):
    llm = ScenarioLLM(mode='no_relevant' if kind == 'no_relevant' else 'success')
    knowledge = ScenarioKnowledge(mode='empty' if kind == 'no_documents' else 'success', original=kind != 'no_original')
    instance = interpreter(knowledge, llm)
    facts = [] if kind == 'no_topics' else case_facts()[1:3]
    first = await run(instance, facts)
    counts = len(llm.calls), len(knowledge.queries)
    second = await run(instance, facts)
    assert first['evidence']['status'] == 'empty'
    assert first['complete'] is True
    assert first == second
    assert counts == (len(llm.calls), len(knowledge.queries))


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['fake_quote', 'synthesis_failure', 'all_failure'])
async def test_failed_explanation_is_not_frozen_and_can_recover(failure):
    llm = ScenarioLLM(mode=failure)
    instance = interpreter(llm=llm)
    first = await run(instance)
    assert first['evidence']['status'] == 'empty' and not first['complete']
    llm.mode = 'success'
    recovered = await run(instance)
    assert recovered['evidence']['status'] == 'ready'
    assert len(llm.calls) == 4
    assert (await run(instance)) == recovered


@pytest.mark.asyncio
async def test_retrieval_outage_is_not_frozen_and_can_recover():
    knowledge = ScenarioKnowledge(mode='failure')
    instance = interpreter(knowledge)
    assert (await run(instance))['evidence']['status'] == 'unavailable'
    knowledge.mode = 'success'
    result = await run(instance)
    assert result['evidence']['status'] == 'ready'
    assert (await run(instance)) == result
