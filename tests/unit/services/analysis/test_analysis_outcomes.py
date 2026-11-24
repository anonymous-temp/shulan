import pytest

from modules.analysis.application.analysis_outcomes import combine_verified
from modules.analysis.application.analysis_outcomes import empty_analysis
from modules.analysis.application.analysis_outcomes import evidence_from
from modules.analysis.application.analysis_outcomes import settle
from modules.analysis.application.analysis_outcomes import source_bindings
from modules.analysis.application.extraction import document_sections
from modules.analysis.application.extraction import merge_document_results
from modules.analysis.application.service import HealthReportInterpreter


def test_repair_preserves_multiple_results_in_one_source_block_without_replacing_them():
    first = {
        **empty_analysis(),
        'observations': [
            {
                'sourceId': 'block1',
                'itemName': '甲',
                'resultValue': '1',
                'evidence': {'sourceId': 'block1', 'quote': '甲=1，乙=2'},
            },
            {
                'sourceId': 'block1',
                'itemName': '乙',
                'resultValue': '2',
                'evidence': {'sourceId': 'block1', 'quote': '甲=1，乙=2'},
            },
        ],
    }
    correction = {
        **empty_analysis(),
        'observations': [
            first['observations'][0],
            {
                'sourceId': 'block2',
                'itemName': '丙',
                'resultValue': '3',
                'evidence': {'sourceId': 'block2', 'quote': '丙=3'},
            },
        ],
    }
    result = combine_verified(first, correction)
    assert [item['itemName'] for item in result['observations']] == ['甲', '乙', '丙']


def test_correction_cannot_replace_an_already_accepted_table_row():
    first = {**empty_analysis(), 'observations': [{'sourceId': 't1r1', 'itemName': '甲', 'resultValue': '0'}]}
    correction = {**empty_analysis(), 'observations': [{'sourceId': 't1r1', 'itemName': '乙', 'resultValue': '7'}]}
    assert combine_verified(first, correction)['observations'][0]['resultValue'] == '0'
    assert len(combine_verified(first, correction)['observations']) == 1


def test_partial_normal_slice_does_not_prove_a_normal_document():
    raw = {**empty_analysis(), 'status': 'normal', 'observations': [{'itemName': '甲', 'resultValue': '0'}]}
    result = settle(raw, issues=[{'code': 'missing_source'}])
    assert result['processing']['status'] == 'partial'
    assert result['status'] == 'pending'


def test_verified_metadata_can_fill_missing_fields_without_duplicating_a_fact():
    item = {
        'sourceId': 'p1',
        'itemName': '甲',
        'resultValue': '1',
        'unit': None,
        'evidence': {'sourceId': 'p1', 'quote': '甲=1', 'occurrenceKey': 'p1:11:14'},
    }
    corrected = {
        **item,
        'unit': '%',
        'observedAt': '2026-01-01',
        'evidence': {'sourceId': 'p1', 'quote': '2026-01-01 甲=1%', 'occurrenceKey': 'p1:11:14'},
    }
    merged = combine_verified(
        {**empty_analysis(), 'observations': [item]}, {**empty_analysis(), 'observations': [corrected]}
    )
    assert len(merged['observations']) == 1
    assert merged['observations'][0]['unit'] == '%'
    assert merged['observations'][0]['observedAt'] == '2026-01-01'
    assert merged['observations'][0]['metadataEvidence']['unit']['quote'] == corrected['evidence']['quote']


def test_source_binding_survives_metadata_enrichment_but_does_not_guess_between_occurrences():
    item = {'itemName': '甲', 'resultValue': '1', 'evidence': {'locator': {'table': 't1', 'valueCell': 4}}}
    result = {**empty_analysis(), 'observations': [{'itemName': '甲', 'resultValue': '1'}]}
    initial = source_bindings(result, [item])
    result['observations'][0]['unit'] = '%'
    assert source_bindings(result, [{**item, 'unit': '%'}]) == initial
    second = {**item, 'evidence': {'locator': {'table': 't2', 'valueCell': 4}}}
    assert source_bindings(result, [item, second]) == {}


def test_imaging_source_identity_is_retained_for_findings_and_diagnosis():
    raw = {
        **empty_analysis(),
        'reportType': 'imaging',
        'expansion': {'findings': '左肺见结节。', 'diagnosis': '建议复查。'},
        'expansionEvidence': {
            'findings': {'sourceId': 'p2', 'occurrenceKey': 'p2:0:7'},
            'diagnosis': {'sourceId': 'p3', 'occurrenceKey': 'p3:0:5'},
        },
    }
    bindings = source_bindings(raw, evidence_from(raw))
    assert set(bindings) == {'/expansion/findings', '/expansion/diagnosis'}
    assert bindings['/expansion/findings'] != bindings['/expansion/diagnosis']
    assert source_bindings({'sections': [raw]}, evidence_from(raw)) == {
        f'/sections/0{path}': value for path, value in bindings.items()
    }


def mixed_root_and_imaging():
    row = {
        'sourceId': 't1r1',
        'itemName': '甲',
        'resultValue': '5',
        'flag': '偏高',
        'evidence': {'sourceId': 't1r1', 'locator': {'table': 't1', 'valueCell': 4}},
    }
    imaging = {
        **empty_analysis(),
        'reportType': 'imaging',
        'reportTime': '2026-01-02',
        'expansion': {'findings': '左肺见结节。', 'diagnosis': None},
        'expansionEvidence': {'findings': {'sourceId': 'p2', 'occurrenceKey': 'p2:0:7'}},
    }
    return {**empty_analysis(), 'reportType': 'lab', 'observations': [row], 'sections': [imaging]}


def test_mixed_root_table_and_imaging_keep_both_evidence_and_bindings():
    raw = mixed_root_and_imaging()
    evidence = evidence_from(raw)
    assert [item['itemName'] for item in evidence] == ['甲', '影像所见']
    bindings = source_bindings(raw, evidence)
    assert set(bindings) == {'/observations/0', '/sections/0/expansion/findings'}
    recovered = combine_verified(raw, empty_analysis())
    assert [item['itemName'] for item in recovered['observations']] == ['甲']
    assert recovered['observations'][0].get('observedAt') is None
    assert [item['itemName'] for item in evidence_from(recovered)] == ['甲', '影像所见']


def test_remerging_mixed_roots_removes_only_generated_projection():
    initial = merge_document_results([mixed_root_and_imaging(), empty_analysis()])
    assert [item['itemName'] for item in initial['observations']] == ['甲']
    restored = document_sections(initial)
    assert len(restored) == len(initial['sections'])
    assert sum(len(part['observations']) for part in restored) == 1
    merged = merge_document_results([initial, empty_analysis()])
    assert [item['itemName'] for item in merged['observations']] == ['甲']
    assert sum(len(part['observations']) for part in merged['sections']) == 1
    assert [item['itemName'] for item in evidence_from(merged)] == ['甲', '影像所见']


def test_independent_root_imaging_is_kept_but_joined_projection_is_not_duplicated():
    root = mixed_root_and_imaging()['sections'][0]
    same_child = {**root, 'expansionEvidence': {'findings': {'sourceId': 'p3', 'occurrenceKey': 'p3:0:7'}}}
    raw = {**root, 'sections': [same_child]}
    assert len(document_sections(raw)) == 2
    merged = merge_document_results([raw, empty_analysis()])
    assert len([part for part in document_sections(merged) if part['reportType'] == 'imaging']) == 2
    assert len(evidence_from(merged)) == 2


def test_unanchored_repeated_result_cannot_borrow_date_from_another_occurrence():
    first = {
        'sourceId': 'p1',
        'itemName': '甲',
        'resultValue': '1',
        'evidence': {'sourceId': 'p1', 'quote': '2026-01-01 甲=1'},
    }
    correction = {
        **first,
        'observedAt': '2026-02-01',
        'evidence': {'sourceId': 'p1', 'quote': '2026-02-01 甲=1'},
    }
    result = combine_verified(
        {**empty_analysis(), 'observations': [first]}, {**empty_analysis(), 'observations': [correction]}
    )
    assert len(result['observations']) == 2
    assert result['observations'][0].get('observedAt') is None
    assert result['observations'][1]['observedAt'] == '2026-02-01'


def test_distinct_physical_occurrences_remain_separate_with_same_name_and_value():
    first = {
        'sourceId': 'p1',
        'itemName': '甲',
        'resultValue': '1',
        'evidence': {'sourceId': 'p1', 'quote': '甲=1', 'occurrenceKey': 'p1:0:3'},
    }
    other = {**first, 'evidence': {**first['evidence'], 'occurrenceKey': 'p1:10:13'}}
    result = combine_verified(
        {**empty_analysis(), 'observations': [first]}, {**empty_analysis(), 'observations': [other]}
    )
    assert len(result['observations']) == 2


def test_dated_projection_is_not_retained_as_an_independent_root():
    raw = {**empty_analysis(), 'reportType': 'lab', 'reportTime': '2026年1月2日'}
    raw['observations'] = [{'itemName': '甲', 'resultValue': '1'}]
    merged = merge_document_results([raw, empty_analysis()])
    assert merged['observations'][0]['observedAt'] == '2026-01-02'
    assert len(document_sections(merged)) == 2
    assert sum(len(part['observations']) for part in document_sections(merged)) == 1


@pytest.mark.asyncio
async def test_repaired_mixed_sources_reach_final_settlement_without_mirrored_sections(monkeypatch):
    interpreter = object.__new__(HealthReportInterpreter)
    interpreter._count_input_tokens = len
    repaired = combine_verified(mixed_root_and_imaging(), empty_analysis())

    async def analyzed_part(*args, **kwargs):
        return interpreter._settle_part(repaired, [])

    monkeypatch.setattr(interpreter, '_analyze_part', analyzed_part)
    result = await interpreter.analyze('用于构造单个原文片段')
    assert [item['itemName'] for item in result['observations']] == ['甲']
    assert sum(len(part['observations']) for part in result['sections']) == 0
    assert len([part for part in result['sections'] if part['reportType'] == 'imaging']) == 1
    assert result['analysisEvidence'] == []
    assert result['status'] == 'pending'  # Preserve model status; do not infer it from flags.
    assert result['observations'][0].get('observedAt') is None
    assert not result.get('sourceBindings')
