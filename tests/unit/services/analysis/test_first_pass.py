"""A valid LLM JSON result completes on the first pass without source-rule recovery."""

import pytest

from tests.unit.services.analysis.test_analysis_json import MemoryTrace
from tests.unit.services.analysis.test_analysis_json import interpreter_with


@pytest.mark.asyncio
async def test_normal_json_is_accepted_in_one_call_and_records_are_not_extracted_by_code():
    data = {
        'reportType': 'microbiome',
        'observations': [{'itemName': '算法评分', 'resultValue': '68', 'kind': 'assessment'}],
    }
    interpreter, graph = interpreter_with(lambda _: data)
    trace = MemoryTrace()
    result = await interpreter.analyze('任意布局原文：算法评分为68。', trace=trace)
    assert len(graph.calls) == 1
    assert result['observations'][0]['kind'] == 'assessment'
    assert result['processing']['status'] == 'complete'
    metrics = trace.payloads['analysis_metrics']
    assert metrics['firstPassSuccess'] and metrics['validationPassed']
    assert metrics['modelCalls'] == 1 and metrics['outputRecoveryCalls'] == 0
