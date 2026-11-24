from types import SimpleNamespace

from modules.report.current_source_evidence import _unit_key
from modules.report.current_source_evidence import verified_current_report
from modules.report.current_source_tables import archived_table_rows


def cell(text, row, col, *, end_row=None, end_col=None):
    return {
        'ysc': row,
        'yec': row if end_row is None else end_row,
        'xsc': col,
        'xec': col if end_col is None else end_col,
        'layouts': [{'text': text}],
    }


def table(*cells, rows=3, cols=4, page=0):
    return {'type': 'table', 'numRow': rows, 'numCol': cols, 'pageNum': [page], 'cells': list(cells)}


def test_archived_spanning_headers_preserve_parallel_result_roles():
    labels = ('项目名称', '检查结果', '参考值', '单位') * 2
    headers = [cell(label, 0, col, end_row=2) for col, label in enumerate(labels)]
    data = [
        cell(value, 3, col)
        for col, value in enumerate(('血红蛋白', '123', '110–150', 'g/L', '白蛋白', '41', '35–55', 'g/L'))
    ]
    layout = table(*headers, *data, rows=4, cols=8, page=5)

    rows = tuple(archived_table_rows([None, layout]))

    assert [(r.label, r.value, r.reference_range, r.unit) for r in rows] == [
        ('血红蛋白', '123', '110–150', 'g/L'),
        ('白蛋白', '41', '35–55', 'g/L'),
    ]
    assert [r.locator for r in rows] == ['archive:table:1:row:3:col:1', 'archive:table:1:row:3:col:5']
    assert [r.page for r in rows] == [6, 6]


def test_staggered_header_preserves_same_row_date_and_skips_missing_result():
    layout = table(
        cell('项目名称', 0, 0, end_row=2),
        cell('本次结果', 1, 1, end_row=2),
        cell('单位', 2, 2),
        cell('检查日期', 0, 3, end_row=2),
        cell('白蛋白', 3, 0),
        cell('41', 3, 1),
        cell('g/L', 3, 2),
        cell('2026-01-08', 3, 3),
        cell('血红蛋白', 4, 0),
        cell('-', 4, 1),
        cell('g/L', 4, 2),
        rows=5,
    )

    rows = tuple(archived_table_rows([layout]))

    assert [(r.label, r.value, r.date) for r in rows] == [('白蛋白', '41', '2026-01-08')]


def test_arbitrary_current_reference_and_history_fields_do_not_supply_missing_results():
    layout = table(
        cell('项目名称', 0, 0),
        cell('检查结果', 0, 1),
        cell('参考值', 0, 2),
        cell('上次结果', 0, 3),
        cell('白蛋白', 1, 0),
        cell('41', 1, 2),
        cell('45', 1, 3),
    )

    assert list(archived_table_rows([layout])) == []


def test_combined_header_accepts_independent_literal_measurements_in_body():
    layout = table(
        cell('项目名称检查结果项目名称检查结果', 0, 0, end_col=1),
        cell('血压（收缩压）：118mmHg 血压（舒张压）：72mmHg', 1, 0, end_col=1),
        cell('体重63kg身高168cm；体重指数22.3kg/m²', 2, 0, end_col=1),
        cell('建议体重63kg', 3, 0, end_col=1),
        cell('身高60kg', 4, 0, end_col=1),
        rows=5,
        cols=2,
    )

    rows = tuple(archived_table_rows([layout]))

    assert [(r.label, r.value, r.unit) for r in rows] == [
        ('血压(收缩压)', '118', 'mmHg'),
        ('血压(舒张压)', '72', 'mmHg'),
        ('体重', '63', 'kg'),
        ('身高', '168', 'cm'),
        ('体重指数', '22.3', 'kg/m2'),
    ]
    assert len({r.locator for r in rows}) == 5
    assert rows[0].names == ('血压(收缩压)', '收缩压')


def test_merged_results_under_a_reference_header_are_not_treated_as_observations():
    layout = table(
        cell('项目名称参考值', 0, 0, end_col=1),
        cell('体重63kg身高168cm', 1, 0, end_col=1),
        rows=2,
        cols=2,
    )

    assert list(archived_table_rows([layout])) == []


def test_merged_body_spans_adjacent_result_header_groups():
    layout = table(
        cell('项目名称检查结果', 0, 0),
        cell('项目名称检查结果', 0, 1),
        cell('体重63.0kg身高168.0cm', 1, 0, end_col=1),
        cell('体重指数22.3kg/m²裸眼视力（右眼）', 2, 0, end_col=1),
        rows=3,
        cols=2,
    )

    rows = tuple(archived_table_rows([layout]))

    assert [(r.label, r.value, r.locator) for r in rows] == [
        ('体重', '63.0', 'archive:table:0:cell:1:0:0'),
        ('身高', '168.0', 'archive:table:0:cell:1:0:1'),
        ('体重指数', '22.3', 'archive:table:0:cell:2:0:0'),
    ]


def test_archive_result_table_after_advice_and_reference_examples_are_distinguished():
    layout = table(
        cell('项目名称', 0, 0),
        cell('检查结果', 0, 1),
        cell('白蛋白', 1, 0),
        cell('41', 1, 1),
        rows=2,
        cols=2,
    )
    advice = table(
        cell('项目名称', 0, 0),
        cell('结果（建议）', 0, 1),
        cell('白蛋白', 1, 0),
        cell('43', 1, 1),
        rows=2,
        cols=2,
    )
    layouts = [
        {'type': 'title', 'text': '1. 检查建议'},
        advice,
        layout,
        {'type': 'title', 'text': '参考案例'},
        layout,
        {'type': 'title', 'text': '实际检查结果'},
        layout,
    ]

    assert [(r.locator, r.value) for r in archived_table_rows(layouts)] == [
        ('archive:table:2:row:1:col:1', '41'),
        ('archive:table:6:row:1:col:1', '41'),
    ]


def test_archived_bounds_require_explicit_page_geometry():
    name = cell('白蛋白', 1, 0)
    result = cell('41', 1, 1)
    name['layouts'][0]['pos'] = [
        {'x': 200, 'y': 300},
        {'x': 400, 'y': 300},
        {'x': 400, 'y': 400},
        {'x': 200, 'y': 400},
    ]
    result['layouts'][0]['pos'] = [
        {'x': 500, 'y': 300},
        {'x': 560, 'y': 300},
        {'x': 560, 'y': 400},
        {'x': 500, 'y': 400},
    ]
    layout = table(cell('项目名称', 0, 0), cell('检查结果', 0, 1), name, result, rows=2, cols=2)

    (row,) = list(archived_table_rows([layout]))
    assert row.name_bounds is None
    assert row.result_bounds is None

    (row,) = list(archived_table_rows([layout], page_sizes={1: (1000, 2000)}))
    assert row.name_bounds == (0.2, 0.15, 0.4, 0.2)
    assert row.result_bounds == (0.5, 0.15, 0.56, 0.2)


def test_same_unmerged_markdown_and_archive_table_represent_one_original_cell():
    headers = ('项目名称', '检查结果', '单位', '检查日期')
    result = ('AST', '53', 'U/L', '2026-11-12')
    layout = table(
        *(cell(value, row, col) for row, values in enumerate((headers, result)) for col, value in enumerate(values)),
        rows=2,
    )
    text = '===== PAGE 1 =====\n' + '\n'.join(
        '| ' + ' | '.join(values) + ' |' for values in (headers, ('---',) * 4, result)
    )
    observation = {'kind': 'measurement', 'itemName': 'AST', 'resultValue': '53', 'unit': 'U/L'}
    snapshot = SimpleNamespace(
        text=text,
        include_analysis_observation_refs=True,
        archive_layouts=[layout],
        archive_page_sizes={},
        report={
            'reportTime': '2026-11-12',
            'observations': [{**observation, 'observationRef': '/observations/0'}],
        },
    )

    _, originals, refs = verified_current_report(snapshot, None)

    (original,) = originals
    assert original.locator.startswith('text:')
    assert refs == {original.locator: ['/observations/0']}

    snapshot.archive_layouts.append(layout)
    report, originals, refs = verified_current_report(snapshot, None)

    assert len({row.locator for row in originals}) == 3
    assert not report['observations']
    assert not refs


def test_count_per_liter_unit_preserves_the_difference_between_count_and_scale():
    assert _unit_key('109/L') != _unit_key('10^9/L')
    assert _unit_key('×10⁹/L') == _unit_key('10^9/L')


def test_standalone_reference_layout_blocks_its_table_until_the_current_results_begin():
    layout = table(
        cell('项目名称', 0, 0),
        cell('检查结果', 0, 1),
        cell('AST', 1, 0),
        cell('53', 1, 1),
        rows=2,
        cols=2,
    )
    layouts = [
        {'type': 'text', 'text': '参考案例：'},
        layout,
        {'type': 'table_name', 'text': '案例明细'},
        layout,
        {'type': 'title', 'text': '实际检查结果'},
        layout,
    ]

    assert [row.locator for row in archived_table_rows(layouts)] == ['archive:table:5:row:1:col:1']


def test_reference_markdown_cannot_reopen_inactive_patient_result_scopes():
    text = (
        '===== PAGE 1 =====\n体检日期：2026-11-12\n参考案例\n'
        '| 项目名称 | 检查结果 | 单位 |\n| --- | --- | --- |\n| ALT | 40 | U/L |\n'
        '检查结果\n检查日期：2026-11-12\n'
        '| 项目名称 | 检查结果 | 单位 |\n| --- | --- | --- |\n| AST | 53 | U/L |\n'
    )
    snapshot = SimpleNamespace(
        text=text,
        include_analysis_observation_refs=True,
        archive_layouts=[],
        archive_page_sizes={},
        report={'reportTime': '2026-11-12', 'observations': []},
    )

    _, originals, _ = verified_current_report(snapshot, None)

    assert {row.label for row in originals} == {'AST'}
