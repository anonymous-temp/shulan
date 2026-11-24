"""Prepare bounded source regions and preserve unambiguous result cells verbatim."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace

from utils.report_flags import normalize_report_flag

from .report_dates import date_fallback
from .report_dates import document_dates
from .report_dates import report_date_scopes

BACKGROUND_PREFIX = '【文档背景：仅用于识别报告类型、日期及表头，不重复提取其中的检测结果】\n'
CONTENT_PREFIX = '\n【本段待提取原文】\n'
_BACKGROUND_HEADING = re.compile(
    r'^(?:\d+[.、\s]*)*(?:参考文献|参考资料|参考人群|参考案例|示例|样例|案例|目录|科普|术语说明|检测原理|方法学|参考范围说明)'
)
_GRADE_WORDS = re.compile(r'正常|较弱|弱|强|耐药|超标|缺乏|偏高|偏低|平衡')


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    names: tuple[str, ...]
    value: str
    fields: dict[str, str]
    locator: dict
    direct: bool = True
    kind: str | None = None
    raw_fields: dict[str, str] = field(default_factory=dict)

    def observation(self):
        if not self.direct or not self.names:
            return None
        chinese = [name for name in self.names if re.search(r'[\u3400-\u9fff]', name)]
        name = max(chinese, key=len) if chinese else self.names[0]
        raw = {
            'itemName': name,
            'nameAliases': list(self.names),
            'resultValue': self.value,
            **self.raw_fields,
            **self.fields,
        }
        item = {
            'itemName': name,
            'resultValue': self.value,
            **self.fields,
            'sourceId': self.source_id,
            'flag': normalize_report_flag(self.fields.get('flag')),
            'rawFlag': self.fields.get('flag'),
            'evidence': {'sourceId': self.source_id, 'locator': self.locator, 'rawFields': raw},
        }
        if self.kind:
            item['kind'] = self.kind
        date_fallback(item, self)
        return item


@dataclass(frozen=True)
class InputUnit:
    text: str
    context: str = ''
    records: tuple[SourceRecord, ...] = ()
    source_id: str = ''
    raw_text: str = ''
    locator: dict = field(default_factory=dict)
    result_eligible: bool = True
    issues: tuple[dict, ...] = ()
    unresolved_items: tuple[dict, ...] = ()


def _compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _label(value):
    return re.sub(r'\s|[（(].*?[)）]', '', value).rstrip(':：')


_NAME_COLUMNS = {
    '项目',
    '项目名称',
    '检测项目',
    '检验项目',
    '指标',
    '指标名称',
    '名称',
    '中文名称',
    '中文名',
    '菌名',
    '菌群名称',
    '拉丁学名',
    '拉丁名',
    '英文名称',
    '英文名',
    '疾病名称',
    '疾病类型',
    '短链脂肪酸',
}
_QUALITATIVE_RESULT_COLUMNS = {'结果', '检查结果', '检测结果', '检验结果'}
_EXPLICIT_VALUE_COLUMNS = {
    '测定值', '检测值', '本次结果', '评估值', '风险指数', '风险值', '样本含量', '相对丰度', '丰度', '含量', '得分',
}
_RESULT_COLUMNS = _QUALITATIVE_RESULT_COLUMNS | _EXPLICIT_VALUE_COLUMNS
_AUXILIARY_COLUMNS = {'代码', '项目代码', '项目编码', '编码', '代号', '缩写', '英文缩写', '序号'}
_COMPARISON_COLUMN = re.compile(r'(?:上次|前次|历次|既往|首次|复查)(?:检测|检验)?(?:结果|值)')
_FIELD_COLUMNS = {
    '参考范围': 'referenceRange',
    '参考值': 'referenceRange',
    '正常范围': 'referenceRange',
    '阈值': 'referenceRange',
    '单位': 'unit',
    '结果评价': 'flag',
    '结果评估': 'flag',
    '判读标准': 'flag',
    '风险等级': 'flag',
    '合成能力评估': 'flag',
    '提示': 'flag',
    '异常标志': 'flag',
    '检测方法': 'method',
    '方法': 'method',
    '标本': 'specimen',
    '标本类型': 'specimen',
    '检测日期': 'observedAt',
    '检查日期': 'observedAt',
    '检验日期': 'observedAt',
}


def _table_roles(headers):
    """Resolve value/status roles within one item group, never from cell proximity.

    In a qualitative lab table ``检测结果`` is the actual result.  If the same
    group explicitly supplies ``样本含量`` or ``风险值``, its ``结果`` column is
    a source grade instead. Multiple explicit value columns stay ambiguous.
    """
    labels = {col: _label(c[5]) for c in headers for col in range(c[3], c[4] + 1)}
    explicit = {col for col, label in labels.items() if label in _EXPLICIT_VALUE_COLUMNS}
    values = explicit or {col for col, label in labels.items() if label in _QUALITATIVE_RESULT_COLUMNS}
    fields = {
        col: ('flag' if explicit and label in _QUALITATIVE_RESULT_COLUMNS else _FIELD_COLUMNS.get(label))
        for col, label in labels.items()
    }
    return values, fields


def result_table_candidate_count(text):
    """A conservative omission bound, never a rule for accepting patient facts."""
    if text.lstrip().startswith('|'):
        rows = [line for line in text.splitlines() if line.lstrip().startswith('|')]
        return sum(
            not re.fullmatch(r'[\s|:\-]+', row)
            and not any(_label(value.strip()) in _RESULT_COLUMNS for value in row.split('|'))
            for row in rows
        )
    return len(re.findall(r'(?<!\S)[+-]?\d+(?:\.\d+)?(?=\s{2,})', text))


def _join_text(values):
    # Join Chinese line wrapping without inserting a new name; keep Latin word boundaries.
    text = ' '.join(v.strip() for v in values if isinstance(v, str) and v.strip())
    return re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])', '', text)


def _table_units(layout, table_id, surrounding):
    """Only unambiguous one-result groups get a row identity; ambiguous tables stay verbatim."""
    cells = layout.get('cells')
    rows, cols = layout.get('numRow'), layout.get('numCol')
    if (
        not isinstance(cells, list)
        or type(rows) is not int
        or type(cols) is not int
        or not (1 < rows <= 10000 and 1 < cols <= 1000)
    ):
        return None
    parsed = []
    for index, cell in enumerate(cells):
        if not isinstance(cell, dict):
            return None
        bounds = [cell.get(key) for key in ('ysc', 'yec', 'xsc', 'xec')]
        if any(type(value) is not int for value in bounds):
            return None
        y0, y1, x0, x1 = bounds
        if not (0 <= y0 <= y1 < rows and 0 <= x0 <= x1 < cols):
            return None
        children = cell.get('layouts', [])
        if not isinstance(children, list):
            return None
        value = _join_text([child.get('text', '') for child in children if isinstance(child, dict)])
        parsed.append((index, y0, y1, x0, x1, value))
    parsed.sort(key=lambda cell: (cell[1], cell[3], cell[0]))
    headers = [c for c in parsed if c[1] == 0]
    if any(c[2] != 0 for c in headers):
        return None
    # A comparison header describes several times for one item, not independent
    # groups of items. Do not silently interpret its first value as the whole row.
    if any(document_dates(c[5]) or _COMPARISON_COLUMN.search(_label(c[5])) for c in headers):
        return None
    occupied = [col for c in headers for col in range(c[3], c[4] + 1)]
    if len(occupied) != len(set(occupied)):
        return None
    labels = {col: c[5] for c in headers for col in range(c[3], c[4] + 1)}
    name_cols = {col for col, value in labels.items() if _label(value) in _NAME_COLUMNS}
    value_cols = {col for col, value in labels.items() if _label(value) in _RESULT_COLUMNS}
    if not name_cols or not value_cols:
        return None
    starts, seen_value = [0], False
    for col in sorted(labels):
        if col in name_cols and seen_value:
            start = col
            while start > starts[-1] and _label(labels.get(start - 1, '')) in _AUXILIARY_COLUMNS:
                start -= 1
            starts.append(start)
            seen_value = False
        if col in value_cols:
            seen_value = True
    # Retain the established single-group path, including paired name aliases.
    if len(value_cols) == 1:
        starts = [0]
    if len(starts) == 1 and len(_table_roles(headers)[0]) != 1:
        return None
    units = []
    for index, start in enumerate(starts):
        end = starts[index + 1] - 1 if index + 1 < len(starts) else cols - 1
        group_id = table_id if len(starts) == 1 else f'{table_id}g{index}'
        group_locator = {} if len(starts) == 1 else {'columnGroup': index, 'groupColumns': [start, end]}
        group_headers = [c for c in headers if start <= c[3] and c[4] <= end]
        group_cells = [c for c in parsed if c[3] <= end and c[4] >= start]
        units.extend(
            _table_group_units(
                group_cells, group_headers, rows, start, end, layout, table_id, group_id, group_locator, surrounding
            )
        )
    return units or None


def _table_fields(active, headers, field_columns):
    fields, raw_fields, uncertain_fields, rendered, by_field = {}, {}, set(), [], {}
    for head in headers:
        matching = [c[5] for c in active if head[3] <= c[3] and c[4] <= head[4] and c[5]]
        rendered.append(_join_text(matching))
        field_name = field_columns.get(head[3])
        if field_name and matching:
            by_field.setdefault(field_name, []).extend(matching)
    for field_name, matching in by_field.items():
        value = _join_text(matching)
        raw_fields[field_name] = value
        uncertain = len(set(matching)) > 1
        if field_name == 'flag':
            uncertain |= len(_GRADE_WORDS.findall(value)) > 1 or bool(re.fullmatch(r'[\d.\s<>%]+', value))
        if uncertain:
            uncertain_fields.add(field_name)
        else:
            fields[field_name] = matching[0]
    return fields, raw_fields, uncertain_fields, rendered


def _unresolved_table_unit(active, names, fields, raw_fields, source_id, locator, context, reason):
    raw_text = _compact({'unresolvedCells': active})
    unresolved = ()
    if names:
        chinese = [name for name in names if re.search(r'[\u3400-\u9fff]', name)]
        name = max(chinese, key=len) if chinese else names[0]
        item = {
            'sourceId': source_id,
            'itemName': name,
            'resultValue': None,
            **fields,
            'missingFields': ['resultValue'],
            'reason': reason,
            'evidence': {
                'sourceId': source_id,
                'locator': locator,
                'rawFields': {'itemName': name, **raw_fields},
            },
        }
        unresolved = (item,)
    return InputUnit(
        f'【原文 {source_id}】{raw_text}\n',
        context,
        source_id=source_id,
        raw_text=raw_text,
        locator=locator,
        unresolved_items=unresolved,
        issues=({'code': 'unresolved_source_row', 'sourceIds': [source_id], 'severity': 'core', 'recoverable': True},),
    )


def _table_group_units(parsed, headers, rows, start, end, layout, table_id, group_id, group_locator, surrounding):
    labels = {col: c[5] for c in headers for col in range(c[3], c[4] + 1)}
    name_cols = {col for col, value in labels.items() if _label(value) in _NAME_COLUMNS}
    value_cols, field_columns = _table_roles(headers)
    header = _compact([c[5] for c in headers])
    context = surrounding + f'\n【表格 {group_id}，页 {layout.get("pageNum")}，列】{header}'
    base_locator = {'table': table_id, 'pages': layout.get('pageNum'), **group_locator}
    if not name_cols or len(value_cols) != 1:
        units = []
        for row in range(1, rows):
            active = [c for c in parsed if c[1] > 0 and c[1] <= row <= c[2]]
            if not any(c[5] for c in active):
                continue
            names = tuple(c[5] for c in active if c[3] in name_cols and start <= c[3] <= c[4] <= end and c[5])
            units.append(
                _unresolved_table_unit(
                    active,
                    names,
                    {},
                    {},
                    f'{group_id}r{row}',
                    {**base_locator, 'rows': [row, row]},
                    context,
                    '本组结果列与项目的对应关系尚未确认。',
                )
            )
        return units
    value_col = next(iter(value_cols))
    parents = list(range(rows))

    def root(row):
        while parents[row] != row:
            parents[row] = parents[parents[row]]
            row = parents[row]
        return row

    for _, y0, y1, x0, x1, value in parsed:
        if y0 and value and start <= x0 <= x1 <= end and x0 in name_cols | value_cols:
            for row in range(y0 + 1, y1 + 1):
                parents[root(row)] = root(y0)
    groups = {}
    for row in range(1, rows):
        groups.setdefault(root(row), []).append(row)
    units = []
    for group in groups.values():
        lo, hi = min(group), max(group)
        active = [c for c in parsed if c[1] > 0 and c[1] <= hi and c[2] >= lo]
        if not any(c[5] for c in active):
            continue
        owned = [c for c in active if start <= c[3] <= c[4] <= end]
        values = [c for c in owned if c[3] == c[4] == value_col and c[5]]
        names = tuple(_join_text([c[5] for c in owned if c[3] == c[4] == col]) for col in sorted(name_cols))
        names = tuple(value for value in names if value and value not in {'-', '—', '/', '无'})
        # Preserve the established single-table wrapped-name reconstruction.
        # New repeated groups need explicit cell evidence when a shared value
        # covers several distinct name cells in the same name column.
        ambiguous_names = bool(group_locator) and any(
            len({c[5] for c in owned if c[3] == c[4] == col and c[5] not in {'', '-', '—', '/', '无'}}) > 1
            for col in name_cols
        )
        fields, raw_fields, uncertain_fields, rendered = _table_fields(owned, headers, field_columns)
        crossing = [c for c in active if c[5] and (c[3] < start or c[4] > end or c[3] != c[4])]
        core_crossing = any(any(c[3] <= col <= c[4] for col in name_cols | value_cols) for c in crossing)
        for head in headers:
            key = field_columns.get(head[3])
            if key and any(
                c[3] <= head[4] and c[4] >= head[3] and not (head[3] <= c[3] <= c[4] <= head[4]) for c in crossing
            ):
                fields.pop(key, None)
                uncertain_fields.add(key)
                raw_fields[key] = _join_text([c[5] for c in active if c[3] <= head[4] and c[4] >= head[3] and c[5]])
        if core_crossing or not values:
            # Empty trailing groups are padding. A named row without a value is
            # useful source material, but must never become a clinical fact.
            if not names and not values and not core_crossing:
                continue
            source_id = f'{group_id}r{lo}'
            units.append(
                _unresolved_table_unit(
                    active,
                    names,
                    fields,
                    raw_fields,
                    source_id,
                    {**base_locator, 'rows': [lo, hi]},
                    context,
                    '项目与结果的单元格跨列或归属不明确。' if core_crossing else '原文未提供可确认的结果值。',
                )
            )
            continue
        # A spanning name can cover several real measurements. Keep the cells and
        # their geometry for the model; each result cell remains a separate source.
        if len(values) != 1 or not names or ambiguous_names:
            for value_cell in values:
                related = [
                    cell
                    for cell in owned
                    if cell[1] <= value_cell[2]
                    and cell[2] >= value_cell[1]
                    and (cell[3] != value_col or cell[0] == value_cell[0])
                ]
                source_id = f'{group_id}c{value_cell[0]}'
                record = SourceRecord(
                    source_id,
                    (),
                    value_cell[5],
                    {},
                    {
                        **base_locator,
                        'valueCell': value_cell[0],
                        'ambiguous': True,
                        'nameColumns': sorted(name_cols),
                        'fieldColumns': {
                            field_name: col
                            for col, field_name in field_columns.items()
                            if field_name and field_name not in uncertain_fields
                        },
                    },
                    direct=False,
                )
                raw_text = _compact({'cells': related, 'resultCell': value_cell[0]})
                units.append(
                    InputUnit(
                        f'【来源 {source_id}】{raw_text}\n',
                        context + '\n单元格格式：[编号,起始行,终止行,起始列,终止列,原文]；'
                        '关联未确定，由模型判断，不能合并不同结果单元格。',
                        (record,),
                        source_id=source_id,
                        raw_text=raw_text,
                        locator=record.locator,
                    )
                )
            continue
        if 'unit' not in fields:
            unit = re.search(r'[（(]([^()（）]+)[)）]', labels[value_col])
            if unit and unit[1].strip() in {'%', '‰'}:
                fields['unit'] = unit[1].strip()
        source_id = f'{group_id}r{lo}'
        record = SourceRecord(
            source_id,
            names,
            values[0][5],
            fields,
            {
                **base_locator,
                'rows': [lo, hi],
                'valueCell': values[0][0],
                'nameCells': [cell[0] for cell in owned if cell[3] in name_cols and cell[5]],
            },
            kind=(
                'assessment'
                if _label(labels[value_col]) in {'评估值', '风险指数', '风险值', '得分'}
                or any(re.search(r'评分|风险|评估|肠道年龄', name) for name in names)
                else 'measurement'
            ),
            raw_fields=raw_fields,
        )
        raw_text = _compact(rendered)
        units.append(
            InputUnit(
                f'【来源 {source_id}】【已确认】{raw_text}\n',
                context,
                (record,),
                source_id=source_id,
                raw_text=raw_text,
                locator=record.locator,
                issues=tuple(
                    {
                        'code': 'ambiguous_source_metadata',
                        'sourceIds': [source_id],
                        'fields': [key],
                        'severity': 'metadata',
                        'recoverable': False,
                    }
                    for key in sorted(uncertain_fields)
                ),
            )
        )
    return units


def _prose_unit(text, source_id, surrounding='', *, locator=None, result_eligible=True):
    tag = '原文' if result_eligible else '背景原文'
    return InputUnit(
        f'【{tag} {source_id}】{text}',
        surrounding,
        source_id=source_id,
        raw_text=text,
        locator=locator or {},
        result_eligible=result_eligible,
    )


def _markdown_units(content, prefix='m', surrounding='', *, locator=None, result_eligible=True):
    """Recognize explicit Markdown result tables, retaining every other line unchanged."""
    lines = content.splitlines(keepends=True)
    units = []
    index = 0
    while index < len(lines):
        if (
            index + 1 < len(lines)
            and '|' in lines[index]
            and re.fullmatch(r'[\s|:\-]+', lines[index + 1])
            and '-' in lines[index + 1]
        ):
            end = index + 2
            while end < len(lines) and lines[end].lstrip().startswith('|'):
                end += 1
            table_lines = [lines[index], *lines[index + 2 : end]]
            matrix = []
            for line in table_lines:
                row = line.strip()
                # Remove only the outer delimiters: trailing empty cells are
                # real padding in side-by-side groups and preserve their width.
                if row.startswith('|'):
                    row = row[1:]
                if row.endswith('|'):
                    row = row[:-1]
                matrix.append([value.strip() for value in row.split('|')])
            if matrix and len({len(row) for row in matrix}) == 1:
                cells = [
                    {'ysc': r, 'yec': r, 'xsc': c, 'xec': c, 'layouts': [{'text': value}]}
                    for r, row in enumerate(matrix)
                    for c, value in enumerate(row)
                ]
                table = {'numRow': len(matrix), 'numCol': len(matrix[0]), 'cells': cells}
                prepared = _table_units(table, f'{prefix}{index}', surrounding) if result_eligible else None
                if prepared:
                    units.extend(prepared)
                    index = end
                    continue
                if result_eligible and any(_label(value) in _RESULT_COLUMNS for value in matrix[0]):
                    # Unknown/multiple result columns remain a whole unresolved
                    # region; do not silently treat a result table as prose.
                    units.append(
                        _prose_unit(
                            ''.join(lines[index:end]),
                            f'{prefix}p{index}',
                            surrounding,
                            locator={**(locator or {}), 'resultTable': True},
                            result_eligible=True,
                        )
                    )
                    index = end
                    continue
        end = index + 1
        while end < len(lines):
            if end + 1 < len(lines) and '|' in lines[end] and re.fullmatch(r'[\s|:\-]+', lines[end + 1]):
                break
            end += 1
        raw_text = ''.join(lines[index:end])
        units.append(
            _prose_unit(
                raw_text,
                f'{prefix}p{index}',
                surrounding,
                locator=locator,
                result_eligible=result_eligible,
            )
        )
        index = end
    return units


def prepare_source_units(content, *, docmind=None):
    """Read source occurrences for native verification independently of model input."""
    layouts = (
        (docmind.get('data') or {}).get('layouts')
        if isinstance(docmind, dict) and isinstance(docmind.get('data'), dict)
        else None
    )
    units = []
    if (
        isinstance(layouts, list)
        and all(isinstance(layout, dict) and isinstance(layout.get('text'), str) for layout in layouts)
        and '\n'.join(layout['text'] for layout in layouts) == content
    ):
        surrounding = ''
        result_section = True
        scopes = report_date_scopes(layouts)
        for index, layout in enumerate(layouts):
            raw = layout['text'] + ('\n' if index + 1 < len(layouts) else '')
            layout_type = layout.get('type')
            if layout_type in {'title', 'table_name'}:
                surrounding = raw.strip()[:240]
                result_section = not bool(_BACKGROUND_HEADING.match(surrounding))
            eligible = result_section and layout_type not in {
                'head',
                'header_line',
                'contents',
                'foot_pagenum',
                'foot',
                'split_line',
                'stamp',
                'head_image',
                'corner_note',
            }
            table = _table_units(layout, f't{index}', surrounding) if layout_type == 'table' and eligible else None
            if table:
                prepared_units = table
            else:
                prepared_units = _markdown_units(
                    raw,
                    f'l{index}m',
                    surrounding,
                    locator={
                        'layout': index,
                        'pages': layout.get('pageNum'),
                        **(
                            {'resultTable': True}
                            if layout_type == 'figure'
                            and re.search(r'检测结果|检验结果|评估值|风险指数', raw)
                            and result_table_candidate_count(raw) > 1
                            else {}
                        ),
                    },
                    result_eligible=eligible,
                )
            for unit in prepared_units:
                if scope := scopes.get(index):
                    unit = replace(
                        unit,
                        locator={**unit.locator, 'reportScope': scope},
                        records=tuple(
                            replace(record, locator={**record.locator, 'reportScope': scope}) for record in unit.records
                        ),
                        unresolved_items=tuple(
                            {
                                **item,
                                'evidence': {
                                    **item['evidence'],
                                    'locator': {**item['evidence']['locator'], 'reportScope': scope},
                                },
                            }
                            for item in unit.unresolved_items
                        ),
                    )
                units.append(unit)
    else:
        units = _markdown_units(content)
    return units
