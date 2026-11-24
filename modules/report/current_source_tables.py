"""Read result cells from the fixed DocMind table geometry."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass

from modules.analysis.application.source_units import _AUXILIARY_COLUMNS
from modules.analysis.application.source_units import _BACKGROUND_HEADING
from modules.analysis.application.source_units import _FIELD_COLUMNS
from modules.analysis.application.source_units import _NAME_COLUMNS
from modules.analysis.application.source_units import _RESULT_COLUMNS
from modules.analysis.application.source_units import _join_text
from modules.analysis.application.source_units import _label
from modules.analysis.application.source_units import _table_units

_MAX_HEADER_ROWS = 8
_REQUIRED_CONTEXT = frozenset({'observedAt', 'unit', 'specimen', 'method'})
_ROLES = _NAME_COLUMNS | _RESULT_COLUMNS | _AUXILIARY_COLUMNS | _FIELD_COLUMNS.keys()
_ADVICE_HEADING = re.compile(
    r'^(?:(?:\d+(?:\.\d+)*|[一二三四五六七八九十]+)[.、)]\s*)?[\u3400-\u9fff]{0,10}建议(?:[:：]|$)'
)
_NONPATIENT_HEADING = re.compile(
    r'^(?:(?:\d+(?:\.\d+)*|[一二三四五六七八九十]+)[.、)]\s*)?'
    r'(?:参考文献|参考资料|参考人群|参考案例|参考范围(?:说明)?|参考值|正常范围|示例|样例|'
    r'历史(?:结果|记录|检测结果)|历次(?:结果|记录)|上次(?:结果|检查结果)|检测原理|方法学)'
    r'\s*[:：]?\s*$'
)
_CURRENT_RESULTS_HEADING = re.compile(
    r'^(?:(?:\d+(?:\.\d+)*|[一二三四五六七八九十]+)[.、)]\s*)?'
    r'(?:实际|本次|当前|详细|临床)?(?:体检|检查|检测|检验)(?:结果|报告)\s*[:：]?\s*$'
)
_CURRENT_RESULT_HEADERS = frozenset({'检查结果', '检测结果', '检验结果', '本次结果'})
_COMBINED_HEADER = re.compile(r'(?:(?:项目名称|检查项目|检测项目)(?:检查结果|检测结果|检验结果))+')
_VITAL_NAMES = {
    '血压(收缩压)': ('血压(收缩压)', '收缩压'),
    '血压(舒张压)': ('血压(舒张压)', '舒张压'),
    '收缩压': ('收缩压', '血压(收缩压)'),
    '舒张压': ('舒张压', '血压(舒张压)'),
    '体重指数(bmi)': ('体重指数(BMI)', '体重指数', '体质指数', 'BMI'),
    '体重指数': ('体重指数', '体质指数', 'BMI'),
    '体质指数': ('体质指数', '体重指数', 'BMI'),
    '身高': ('身高',),
    '体重': ('体重',),
}
_VITAL_RESULT = re.compile(
    r'(?P<label>' + '|'.join(map(re.escape, _VITAL_NAMES)) + r')[:=]?'
    r'(?P<value>[<>≤≥]?[+-]?\d+(?:\.\d+)?)(?P<unit>mmHg|kg/m2|kg(?!/)|cm)(?P<flag>[↑↓])?',
    re.I,
)
_VITAL_UNITS = {
    '血压(收缩压)': 'mmhg',
    '血压(舒张压)': 'mmhg',
    '收缩压': 'mmhg',
    '舒张压': 'mmhg',
    '体重指数(bmi)': 'kg/m2',
    '体重指数': 'kg/m2',
    '体质指数': 'kg/m2',
    '身高': 'cm',
    '体重': 'kg',
}
_VITAL_SEPARATOR = re.compile(r'[,;、]*')
_EMPTY_VISION_SLOT = re.compile(r'裸眼视力\((?:左眼|右眼)\)')
_MISSING_VALUES = frozenset({'-', '--', '—', '/', '未检', '未检测', '未测', '未报告', '未出结果', '无结果'})


@dataclass(frozen=True)
class ArchivedTableRow:
    locator: str
    page: int
    names: tuple[str, ...]
    value: str
    unit: str | None = None
    reference_range: str | None = None
    flag: str | None = None
    specimen: str | None = None
    method: str | None = None
    date: str | None = None
    kind: str = 'measurement'
    result_bounds: tuple[float, float, float, float] | None = None
    name_bounds: tuple[float, float, float, float] | None = None
    rendered_cell: tuple[str, int, int] | None = None

    @property
    def label(self):
        return self.names[0]


@dataclass(frozen=True)
class _Cell:
    item: dict
    index: int
    row: int
    end_row: int
    col: int
    end_col: int
    text: str

    @property
    def heading(self):
        return _label(self.text)


def _cells(layout):
    items, rows, cols = layout.get('cells'), layout.get('numRow'), layout.get('numCol')
    if (
        type(rows) is not int
        or type(cols) is not int
        or not (1 < rows <= 10000 and 1 <= cols <= 1000)
        or not isinstance(items, list)
    ):
        return []
    cells = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(children := item.get('layouts', []), list):
            return []
        bounds = tuple(item.get(key) for key in ('ysc', 'yec', 'xsc', 'xec'))
        if any(type(bound) is not int for bound in bounds):
            return []
        row, end_row, col, end_col = bounds
        if not (0 <= row <= end_row < rows and 0 <= col <= end_col < cols):
            return []
        text = unicodedata.normalize(
            'NFKC', _join_text(part.get('text', '') for part in children if isinstance(part, dict))
        )
        cells.append(_Cell(item, index, row, end_row, col, end_col, text))
    return cells


def _flatten_header(layout, cells):
    """Fit a bounded vertical header band to the established result-cell parser."""
    top = [cell for cell in cells if cell.row == 0 and cell.text]
    roles = [cell for cell in top if cell.heading in _ROLES]
    if not roles:
        return None, ()
    end = max(cell.end_row for cell in roles)
    if end >= _MAX_HEADER_ROWS or end + 1 >= layout['numRow']:
        return None, ()
    # Exact current roles can occupy different lines within a vertical band.
    # Keep first-line history/comparison labels so the existing parser can
    # reject ambiguous times. Drop only parent captions that wrap child roles.
    headers = [
        cell for cell in cells if cell.text and (cell.row == 0 or (cell.end_row <= end and cell.heading in _ROLES))
    ]
    role_columns = {col for cell in headers if cell.heading in _ROLES for col in range(cell.col, cell.end_col + 1)}
    headers = [
        cell
        for cell in headers
        if cell.heading in _ROLES
        or cell.col == cell.end_col
        or not all(col in role_columns for col in range(cell.col, cell.end_col + 1))
        or re.search(r'历史|既往|上次|前次|参考|建议|示例', cell.text)
    ]
    if any(cell.row <= end < cell.end_row for cell in cells if cell.text):
        return None, ()
    body = [cell for cell in cells if cell.row > end]
    # Any sub-header or body cell lost inside the band makes its structure
    # unsafe; never reinterpret a likely result row as a header.
    header_ids = {cell.index for cell in headers}
    if any(cell.text and 0 < cell.row <= end and cell.index not in header_ids for cell in cells):
        return None, ()
    transformed = [{**cell.item, 'ysc': 0, 'yec': 0} for cell in headers] + [
        {**cell.item, 'ysc': cell.row - end, 'yec': cell.end_row - end} for cell in body
    ]
    return {**layout, 'numRow': layout['numRow'] - end, 'cells': transformed}, [*headers, *body]


def _bounds(cell, page_size):
    if not page_size:
        return None
    width, height = page_size
    if not all(type(size) in {int, float} and size > 0 for size in (width, height)):
        return None
    layouts = cell.item.get('layouts') or ()
    if len(layouts) != 1 or not isinstance(layouts[0], dict):
        return None
    points = layouts[0].get('pos')
    if not isinstance(points, list) or len(points) != 4:
        return None
    if any(
        not isinstance(point, dict)
        or type(point.get('x')) not in {int, float}
        or type(point.get('y')) not in {int, float}
        or not (0 <= point['x'] <= width and 0 <= point['y'] <= height)
        for point in points
    ):
        return None
    x, y = [point['x'] for point in points], [point['y'] for point in points]
    if min(x) == max(x) or min(y) == max(y):
        return None
    return min(x) / width, min(y) / height, max(x) / width, max(y) / height


def is_nonpatient_heading(text):
    heading = re.sub(r'^\s*#{1,6}\s*', '', unicodedata.normalize('NFKC', text)).strip()
    return bool(_NONPATIENT_HEADING.fullmatch(heading))


def _rendering_digest(rows):
    return hashlib.sha256(json.dumps(rows).encode('ascii')).hexdigest()


def _unmerged_rendering(layout, cells):
    if len(cells) != layout['numRow'] * layout['numCol']:
        return None
    if any(cell.row != cell.end_row or cell.col != cell.end_col for cell in cells):
        return None
    if len({(cell.row, cell.col) for cell in cells}) != len(cells):
        return None
    rows = [[] for _ in range(layout['numRow'])]
    for cell in sorted(cells, key=lambda item: (item.row, item.col)):
        rows[cell.row].append(
            _join_text(part.get('text', '') for part in cell.item.get('layouts', ()) if isinstance(part, dict))
        )
    return _rendering_digest(rows)


def _column_rows(layout, cells, page, prefix, page_size, *, require_current=False):
    flattened, originals = _flatten_header(layout, cells)
    if not flattened:
        return
    # Generic result labels under an advice heading may describe goals.
    # A later table can provide its own explicit current-result columns.
    current_columns = (
        {
            original.col
            for cell, original in zip(flattened['cells'], originals)
            if cell['ysc'] == 0 and ''.join(original.text.split()) in _CURRENT_RESULT_HEADERS
        }
        if require_current
        else ()
    )
    units = _table_units(flattened, 'fixed', '')
    rendering = _unmerged_rendering(layout, cells) if units else None
    for unit in units or ():
        if any(_REQUIRED_CONTEXT.intersection(issue.get('fields', ())) for issue in unit.issues):
            continue
        for record in unit.records:
            item = record.observation()
            if not item or unicodedata.normalize('NFKC', record.value).strip() in _MISSING_VALUES:
                continue
            result = originals[record.locator['valueCell']]
            if require_current and result.col not in current_columns:
                continue
            names = (item['itemName'], *(name for name in record.names if name != item['itemName']))
            name_indices = record.locator['nameCells']
            name_bounds = _bounds(originals[name_indices[0]], page_size) if len(name_indices) == 1 else None
            yield ArchivedTableRow(
                f'{prefix}:row:{result.row}:col:{result.col}',
                page,
                names,
                record.value,
                item.get('unit'),
                item.get('referenceRange'),
                item.get('rawFlag'),
                item.get('specimen'),
                item.get('method'),
                item.get('observedAt'),
                item.get('kind', 'measurement'),
                _bounds(result, page_size),
                name_bounds,
                (rendering, result.row, result.col) if rendering else None,
            )


def _literal_vitals(text):
    """Accept whole merged cells with explicit, supported label/value pairs."""
    compact = ''.join(text.split())
    matches = []
    position = 0
    while position < len(compact):
        # A trailing labelled slot without a reading does not change the
        # independent explicit result immediately before it.
        if matches and _EMPTY_VISION_SLOT.fullmatch(compact, position):
            break
        match = _VITAL_RESULT.match(compact, position)
        if not match or match['unit'].casefold() != _VITAL_UNITS[match['label'].casefold()]:
            return ()
        matches.append(match)
        position = _VITAL_SEPARATOR.match(compact, match.end()).end()
    return matches


def _merged_cell_rows(cells, page, prefix):
    combined = [cell for cell in cells if cell.row == 0 and _COMBINED_HEADER.fullmatch(''.join(cell.text.split()))]
    if not combined:
        return
    for cell in cells:
        if not all(
            sum(head.end_row < cell.row and head.col <= col <= head.end_col for head in combined) == 1
            for col in range(cell.col, cell.end_col + 1)
        ):
            continue
        for index, match in enumerate(_literal_vitals(cell.text)):
            label, value, unit, flag = match.group('label', 'value', 'unit', 'flag')
            yield ArchivedTableRow(
                f'{prefix}:cell:{cell.row}:{cell.col}:{index}',
                page,
                _VITAL_NAMES[label.casefold()],
                value,
                unit,
                flag=flag,
            )


def archived_table_rows(layouts, *, page_sizes=None):
    """Yield only independent current result occurrences in single-page tables."""
    background = advice = nonpatient = False
    for index, layout in enumerate(layouts or ()):
        if not isinstance(layout, dict):
            continue
        kind = layout.get('type')
        text = layout.get('text')
        if kind in {'text', 'title', 'table_name'} and isinstance(text, str):
            heading = unicodedata.normalize('NFKC', text).strip()
            if is_nonpatient_heading(heading):
                nonpatient = True
            elif _CURRENT_RESULTS_HEADING.fullmatch(heading):
                nonpatient = False
            if kind in {'title', 'table_name'}:
                background = bool(_BACKGROUND_HEADING.match(heading))
                advice = bool(_ADVICE_HEADING.match(heading))
        if kind != 'table' or background or nonpatient:
            continue
        pages = layout.get('pageNum')
        if not isinstance(pages, list) or len(pages) != 1 or type(pages[0]) is not int or pages[0] < 0:
            continue
        cells = _cells(layout)
        if not cells:
            continue
        prefix, page = f'archive:table:{index}', pages[0] + 1
        yield from _column_rows(layout, cells, page, prefix, (page_sizes or {}).get(page), require_current=advice)
        yield from _merged_cell_rows(cells, page, prefix)
