import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters.analysis.microbiome_source import _covered_ids
from modules.analysis.application.documents import DocumentParsingError
from modules.analysis.application.source_facts import SourceFacts
from modules.analysis.application.source_facts import reconcile_source_facts
from modules.analysis.application.source_units import prepare_source_units
from tests.unit.services.analysis.test_analysis_json import interpreter_with


def test_native_coverage_keeps_signed_source_values_and_separate_model_input():
    text = '|项目|检查结果|\n|菌群评分|−2.613|'
    rows = (('项目', '检查结果'), ('菌群评分', '−2.613'))
    cells = [
        {'ysc': y, 'yec': y, 'xsc': x, 'xec': x, 'layouts': [{'text': value}]}
        for y, row in enumerate(rows)
        for x, value in enumerate(row)
    ]
    docmind = {
        'data': {
            'layouts': [
                {'type': 'table', 'pageNum': [0], 'text': text, 'numRow': 2, 'numCol': 2, 'cells': cells},
            ]
        }
    }
    units = prepare_source_units(text, docmind=docmind)
    (record,) = [record for unit in units for record in unit.records]
    native = {
        'itemName': '菌群评分',
        'resultValue': '-2.613',
        'evidence': {'locator': {'physicalPage': 1}, 'rawFields': {'nameAliases': ['菌群评分']}},
    }
    document = SimpleNamespace(text='===== PAGE 1 =====\n菌群评分 −2.613')
    assert _covered_ids(document, docmind, [native], [], [], set()) == {record.source_id}
    assert not _covered_ids(document, docmind, [{**native, 'resultValue': '2.613'}], [], [], set())


@pytest.mark.asyncio
async def test_native_pdf_correction_does_not_change_model_body():
    text = '肠道菌群报告\n检测日期：2026-09-01\n菌群评分检测结果：−2.613'
    content = b'%PDF-1.7 fixture'
    snapshot = {
        'content': content,
        'fileName': 'original.pdf',
        'contentSha256': hashlib.sha256(content).hexdigest(),
    }
    model = {
        'reportType': 'microbiome',
        'reportTime': '2026-09-01',
        'observations': [
            {'itemName': '菌群评分', 'resultValue': '2.613'},
        ],
    }
    interpreter, graph = interpreter_with(lambda _: model)
    reader = Mock(
        return_value=SourceFacts(
            observations=({'itemName': '菌群评分', 'resultValue': '-2.613', 'observedAt': '2026年9月1日'},),
            replaced_names=frozenset({'菌群评分'}),
            report_time='2026年9月1日',
        )
    )
    interpreter._source_fact_reader = reader
    outcome = await interpreter.analyze(text, source_snapshot=snapshot)
    assert [call['input']['report_text'] for call in graph.calls] == [text]
    assert [row['resultValue'] for row in outcome['observations']] == ['-2.613']
    assert outcome['reportTime'] == '2026-09-01'
    assert 'verifiedObservationPointers' not in outcome
    reader.assert_called_once_with(content=content, text=text, file_name='original.pdf', docmind=None)
    with pytest.raises(DocumentParsingError, match='校验失败'):
        await interpreter.analyze(text, source_snapshot={**snapshot, 'contentSha256': 'different'})
    assert reader.call_count == len(graph.calls) == 1


@pytest.mark.parametrize(
    'case', ['different_date', 'invalid_date', 'mixed_report', 'multiple_native', 'multiple_model', 'unit_conflict']
)
def test_date_alias_does_not_expand_ambiguous_coverage(case):
    item = {'itemName': '菌群评分', 'resultValue': '2.613'}
    native = {'itemName': '菌群评分', 'resultValue': '-2.613', 'observedAt': '2026年9月1日'}
    report = {'reportType': 'microbiome', 'reportTime': '2026-09-01', 'observations': [item]}
    if case == 'different_date':
        report['reportTime'] = '2026-09-02'
    elif case == 'invalid_date':
        report['reportTime'] = '2026-02-30'
    elif case == 'mixed_report':
        report['sections'] = [{'reportType': 'lab', 'reportTime': '2026-09-01', 'observations': [item]}]
    elif case == 'multiple_model':
        report['observations'].append({**item, 'resultValue': '3.7'})
    elif case == 'unit_conflict':
        item['unit'], native['unit'] = 'mg/L', '%'
    originals = (native, {**native, 'resultValue': '-3.1'}) if case == 'multiple_native' else (native,)
    facts = SourceFacts(observations=originals, replaced_names=frozenset({'菌群评分'}))
    result = reconcile_source_facts(report, facts)
    assert result['observations'][0] == item
    if case == 'mixed_report':
        assert result['sections'][0]['observations'] == [item]


@pytest.mark.parametrize('native_page', [1, 2])
def test_mixed_report_date_alias_requires_the_same_physical_row(native_page):
    position = {'sourceChecksum': 'fixture', 'physicalPage': 1, 'sourceTableIndex': 0, 'sourceRowIndex': 1}
    item = {'itemName': '菌群评分', 'resultValue': '2.613', 'evidence': {'locator': position}}
    native = {
        **item,
        'resultValue': '-2.613',
        'observedAt': '2026年9月1日',
        'evidence': {'locator': {**position, 'physicalPage': native_page}},
    }
    report = {
        'reportType': 'checkup',
        'sections': [
            {'reportType': 'microbiome', 'reportTime': '2026-09-01', 'observations': [item]},
        ],
    }
    facts = SourceFacts(observations=(native,), replaced_names=frozenset({'菌群评分'}))
    result = reconcile_source_facts(report, facts)
    assert result['sections'][0]['observations'] == ([] if native_page == 1 else [item])
