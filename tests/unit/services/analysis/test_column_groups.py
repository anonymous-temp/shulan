"""Column roles and hospital headers are interpreted by the LLM, not pre-parsed."""

import pytest

from tests.unit.services.analysis.test_analysis_json import interpreter_with


@pytest.mark.parametrize(
    'source',
    [
        '|项目|结果|\n|---|---|\n|甲|1|',
        '|Analyte|Measured today|\n|---|---|\n|甲|1|',
        '|参考|本次读数|被测对象|\n|---|---|---|\n|0-2|1|甲|',
        '综合检查\n甲      1      阴性\n',
        '项目和值合在同一单元格：甲=1',
    ],
)
@pytest.mark.asyncio
async def test_unknown_column_layout_is_sent_unchanged_and_needs_only_normal_json(source):
    output = {'reportType': 'lab', 'observations': [{'itemName': '甲', 'resultValue': '1'}]}
    interpreter, graph = interpreter_with(lambda _: output)
    result = await interpreter.analyze(source)
    assert len(graph.calls) == 1
    assert graph.calls[0]['input']['report_text'] == source
    assert result['processing']['status'] == 'complete'
    assert result['observations'][0]['itemName'] == '甲'
