"""Report token planning and settlement at the model boundary."""

import asyncio

import pytest

from adapters.analysis.tokenizer import count_report_input_tokens
from agents.graphs.analysis.profile import create_analysis_health_report_profile
from agents.llm.aion_gateway import AionLLMGateway
from modules.analysis.application import service
from modules.analysis.application.analysis_display import display_result
from modules.analysis.application.input_preparation import MAX_INPUT_TOKENS
from modules.analysis.application.input_preparation import input_token_budget
from modules.analysis.application.input_preparation import prepare_input
from tests.unit.services.analysis.test_analysis_json import MemoryTrace
from tests.unit.services.analysis.test_analysis_json import interpreter_with


@pytest.mark.parametrize(('tokens', 'part_count'), [(102400, 1), (102401, 2), (204800, 2), (256000, 3)])
def test_actual_aion_token_boundaries_on_a_long_single_line(tokens, part_count):
    text = ' a' * tokens
    prepared = prepare_input(text, count_tokens=count_report_input_tokens)
    assert prepared.estimated_tokens == tokens
    assert len(prepared.chunks) == part_count
    assert ''.join(chunk.text for chunk in prepared.chunks) == text
    counts = [chunk.estimated_tokens for chunk in prepared.chunks]
    assert max(counts) <= MAX_INPUT_TOKENS
    assert max(counts) - min(counts) <= 1


def test_smaller_context_window_counts_prompt_correction_envelope_and_output():
    budget = input_token_budget(1200, 100, 'p' * 40, 'c' * 16, count_tokens=len)
    assert budget == 20
    text = '1234567890\n1234567890\n123456789'
    prepared = prepare_input(text, token_limit=budget, count_tokens=len)
    assert len(prepared.chunks) == 2
    assert prepared.chunks[0].text.endswith('\n')
    assert ''.join(chunk.text for chunk in prepared.chunks) == text
    assert all(chunk.estimated_tokens <= budget for chunk in prepared.chunks)
    tight = text + '0'
    forced = prepare_input(tight, token_limit=budget, count_tokens=len)
    assert len(forced.chunks) == 2
    assert not forced.chunks[0].text.endswith('\n')
    assert ''.join(chunk.text for chunk in forced.chunks) == tight


class ProviderRejection(Exception):
    status_code = 400


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['model_input_rejected', 'model_call_timeout', 'truncated'])
async def test_failed_call_keeps_the_preplanned_parts_and_partial_result(failure):
    text = 'a' * 15 + 'b' * 15
    sent = []

    async def respond(part):
        sent.append(part)
        if part.startswith('a'):
            if failure == 'model_input_rejected':
                raise ProviderRejection()
            if failure == 'model_call_timeout':
                await asyncio.Event().wait()
            return '{"reportType":"lab","title":"incomplete'
        return {'reportType': 'lab', 'observations': [{'itemName': 'confirmed', 'resultValue': 0}]}

    interpreter, graph = interpreter_with(respond)
    interpreter._count_input_tokens = len
    interpreter.MAX_ANALYZE_INPUT_TOKENS = 15
    interpreter.CALL_TIMEOUT_SECONDS = 0.05
    trace = MemoryTrace()
    result = await interpreter.analyze(text, trace=trace)
    assert len(graph.calls) == 2
    assert sent == ['a' * 15, 'b' * 15]
    assert result['observations'][0]['resultValue'] == 0
    assert result['processing']['status'] == 'partial'
    assert [issue['code'] for issue in result['processing']['issues']] == [failure]
    assert trace.payloads['analysis_metrics']['estimatedPartTokens'] == [15, 15]
    assert trace.payloads['analysis_metrics']['outputRecoveryCalls'] == 0


@pytest.mark.asyncio
async def test_missing_values_are_displayed_without_extra_call_and_with_partial_status():
    reply = {
        'reportType': 'lab',
        'observations': [
            {'itemName': 'not recorded', 'resultValue': ''},
            {'itemName': 'null', 'resultValue': None},
            {'itemName': 'omitted'},
            {'itemName': 'dash', 'resultValue': '-'},
            {'itemName': 'zero', 'resultValue': 0},
            {'itemName': 'negative', 'resultValue': '阴性'},
        ],
    }
    interpreter, graph = interpreter_with(lambda _: reply)
    result = display_result(await interpreter.analyze('original report'))
    assert len(graph.calls) == 1
    assert result['processing']['status'] == 'partial'
    assert result['processing']['issueCount'] == 0
    assert [item['resultValue'] for item in result['observations']] == ['-', 0, '阴性']
    assert [item['itemName'] for item in result['unresolvedItems']] == ['not recorded', 'null', 'omitted']


@pytest.mark.asyncio
async def test_runtime_budget_plans_every_part_before_the_first_model_request(monkeypatch):
    started = False
    prompt = 'system prompt'
    text = 'a' * 30 + 'b' * 30

    def count_tokens(value):
        assert not started
        return len(value)

    def respond(_):
        nonlocal started
        started = True
        return {'reportType': 'lab', 'observations': [{'itemName': 'confirmed', 'resultValue': 0}]}

    interpreter, graph = interpreter_with(respond)
    interpreter._count_input_tokens = count_tokens
    runtime = interpreter._agent_runtime
    runtime.profiles.register(create_analysis_health_report_profile())
    runtime.llm_gateway = AionLLMGateway(
        provider='deepseek', api_key='offline', default_model='deepseek-flash', prompt_renderer=lambda _: prompt
    )
    profile = create_analysis_health_report_profile()
    window = profile.model_kwargs['max_tokens'] + 1024 + len(prompt) + len(service._CORRECTION_PREFIX) + 30
    monkeypatch.setitem(service._DEEPSEEK_CONTEXT_WINDOWS, 'deepseek-flash', window)
    trace = MemoryTrace()
    result = await interpreter.analyze(text, trace=trace)
    assert [call['input']['report_text'] for call in graph.calls] == ['a' * 30, 'b' * 30]
    assert result['processing']['status'] == 'complete'
    assert trace.payloads['analysis_metrics']['inputTokenLimit'] == 30
