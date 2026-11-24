import pymupdf

from modules.report.parsing import _extract_pdf
from modules.report.parsing import original_clinical_table_rows


def test_native_result_proof_reads_parallel_borderless_tables_after_advice():
    pdf = pymupdf.open()
    page = pdf.new_page(width=650, height=800)

    def line(y, cells, start=24):
        for offset, cell in zip((0, 80, 150, 215), cells):
            page.insert_text((start + offset, y), cell, fontname='china-s', fontsize=10)

    line(38, ['体检日期：2026/11/12'])
    line(60, ['建议'])
    line(80, ['无据指标', '44', '7-40', 'U/L'])
    headers = ['项目名称', '检查结果', '参考值', '单位']
    line(120, headers)
    line(120, headers, 335)
    line(138, ['项目甲', '42', '7-40', 'U/L'])
    line(138, ['项目甲', '42', '7-40', 'U/L'], 335)
    line(156, ['无值项目', '', '2-4', 'mmol/L'])
    line(156, ['项目乙', '1↓', '2-4', 'mmol/L'], 335)
    line(174, ['参考值', '2', '2-4', 'mmol/L'])
    line(192, ['健康建议'])
    line(210, ['区块外值', '42', '7-40', 'U/L'])
    line(228, ['参考案例'])
    line(246, headers)
    line(264, ['同日期示例', '42', '7-40', 'U/L'])

    page = pdf.new_page(width=650, height=800)
    line(98, ['本次检查结果'])
    line(120, headers)
    line(138, ['项目丙', '25', '7-40', 'U/L'])
    line(156, ['参考案例'])
    line(175, headers)
    line(192, ['非本人项目', '6', '7-40', 'U/L'])

    page = pdf.new_page(width=650, height=800)
    line(120, headers)
    line(138, ['续页非本人项目', '25', '7-40', 'U/L'])
    line(156, ['本次检查结果'])
    line(175, headers)
    line(192, ['项目丁', '20', '7-40', 'U/L'])

    content = pdf.tobytes()
    pdf.close()
    legacy = _extract_pdf('fixture.pdf', content, ocr=False)
    assert legacy.native_result_rows == ()
    assert legacy.native_page_dates == {}
    assert '同日期示例' in {row.item_name for row in original_clinical_table_rows(legacy)}

    proven = _extract_pdf('fixture.pdf', content, ocr=False, prove_result_rows=True)

    rows = [
        (row.label, row.value, row.reference_range, row.unit, row.flag, row.date) for row in proven.native_result_rows
    ]
    assert rows == [
        ('项目甲', '42', '7-40', 'U/L', None, '2026-11-12'),
        ('项目甲', '42', '7-40', 'U/L', None, '2026-11-12'),
        ('项目乙', '1', '2-4', 'mmol/L', '偏低', '2026-11-12'),
        ('项目丙', '25', '7-40', 'U/L', None, None),
        ('项目丁', '20', '7-40', 'U/L', None, None),
    ]
    assert proven.native_page_dates == {1: '2026-11-12'}
    assert len({row.locator for row in proven.native_result_rows}) == 5
    first, second, *_ = proven.native_result_rows
    assert first.result_bounds[2] < second.result_bounds[0]
    assert first.name_bounds[2] < first.result_bounds[0]
    assert first.result_bounds[1] == second.result_bounds[1]
    original_rows = {row.item_name: row for row in original_clinical_table_rows(proven)}
    assert {'同日期示例', '非本人项目', '续页非本人项目'}.isdisjoint(original_rows)
    row = original_rows['项目丙']
    bounds = row.model_extra['sourceResultBounds']
    assert bounds[0] == proven.native_result_rows[3].result_bounds[0]
    assert row.model_extra['sourceNameBounds'][2] < bounds[0]
