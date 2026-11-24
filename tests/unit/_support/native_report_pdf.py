"""Synthetic native microbiome PDF with positioned original table cells."""

import pymupdf


def native_pdf():
    """Use the existing native-PDF fixture's portable built-in CJK font."""
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((40, 40), '肠道菌群检测报告', fontname='china-s', fontsize=14)
    page.insert_text((40, 65), '报告日期：2026-09-01', fontname='china-s', fontsize=11)
    xs, ys = [40, 250, 360, 510], [100, 130, 160, 190]
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]))
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y))
    rows = [
        ['检测项目', '检测结果', '参考范围'],
        ['肠道菌群多样性', '7.36', '5-8'],
        ['肠道微生物健康指数', '-0.02', '0-1'],
    ]
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            page.insert_text((xs[j] + 5, ys[i] + 20), value, fontname='china-s', fontsize=10)
    content = pdf.tobytes()
    pdf.close()
    return content
