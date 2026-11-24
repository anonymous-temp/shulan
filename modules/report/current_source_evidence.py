"""Verify current Report candidates against identifiable occurrences in the archived original."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from dataclasses import replace
from decimal import Decimal

from modules.analysis.application.extraction import normalize_document_time
from modules.analysis.application.report_dates import document_dates
from modules.analysis.application.source_units import _NAME_COLUMNS
from modules.analysis.application.source_units import prepare_source_units
from modules.report import parsing
from modules.report.current_source_tables import _rendering_digest
from modules.report.current_source_tables import archived_table_rows
from modules.report.current_source_tables import is_nonpatient_heading
from modules.report.schemas import HospitalDataInput
from utils.report_observation_comparison import observation_label_key as _name
from utils.report_observation_comparison import observation_literal as _literal
from utils.report_observation_comparison import observation_unit_key as _unit_key

_DATE = re.compile(r'(?m)^[ \t]*(?:报告日期|报告时间|检查日期|检验日期|检测日期)[ \t]*[:：][ \t]*(.+)$')
_IMAGING = re.compile(r'^[ \t]*(?:影像|超声|检查)(所见|诊断)[ \t]*[:：][ \t]*(.*)$')
_FIELD = re.compile(r'^[ \t]*[\w /-]{1,40}[ \t]*[:：]')
_BACKGROUND = re.compile(
    r'^\s*(?:#{1,6}\s*)?(?:[\w]{0,8}建议|参考文献|健康科普|科普|健康教育|健康指导|注意事项|参考资料|参考范围|参考值|正常范围|说明)'
    r'(?:\s*[:：]|\s*$)'
)
_NON_RESULT_LABEL = re.compile(r'^(?:参考值|参考范围|正常范围|建议|说明|文献|阈值)')
_RESULTS = re.compile(
    r'^\s*(?:#{1,6}\s*)?(?:检验结果|检测结果|检查结果|检查结论|体检结论|影像报告|超声报告)\s*[:：]?\s*$'
)

_QUANTITY = re.compile(r'([<>≤≥]?)[ \t]*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d{1,6})?)\s*([↑↓]?)')
_EXPLICIT_RANGE = re.compile(r'([+-]?\d+(?:\.\d+)?)\s*[-~～–—至]\s*([+-]?\d+(?:\.\d+)?)')
_PAGE_DATE = re.compile(r'^\s*(?:体检日期|检查日期|检验日期|检测日期)\s*[:：]\s*(.*)$')
_PAGE = re.compile(r'^\s*(?:=====\s*)?PAGE\s+(\d+)(?:\s*=====)?\s*$', re.I)
_INLINE_RESULT = re.compile(
    r'^[ \t]*(?P<name>[A-Za-z\u3400-\u9fff][\w ()/.-]{0,70}?)\s+'
    r'(?P<value>[<>≤≥]?\s*[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s+'
    r'(?P<unit>[%μµA-Za-z][%μµA-Za-z0-9^/().*+-]{0,23})[ \t]*$'
)


def _block_pages(text):
    page, start, offset = None, 0, 0
    for line in text.splitlines(keepends=True):
        if marker := _PAGE.match(line):
            if offset > start:
                yield page, start, text[start:offset]
            page, start = int(marker[1]), offset + len(line)
        offset += len(line)
    if offset > start:
        yield page, start, text[start:offset]


def _result(value, unit):
    value, unit = _literal(value), _literal(unit)
    return value if not unit or value.endswith(unit) else f'{value} {unit}'


def _value_key(value, unit):
    if match := _QUANTITY.fullmatch(_literal(value)):
        number = Decimal(match[2])
        if number.is_finite():
            return match[1], number, _unit_key(unit)
    return _result(value, unit)


@dataclass(frozen=True)
class _Occurrence:
    locator: str
    names: tuple[str, ...]
    value: str
    role: str
    date: str | None = None
    unit: str | None = None
    specimen: str | None = None
    method: str | None = None
    reference_range: str | None = None
    flag: str | None = None
    page: int | None = None
    label: str = ''
    in_summary: bool = False
    result_bounds: tuple[float, float, float, float] | None = None
    name_bounds: tuple[float, float, float, float] | None = None
    rendered_cell: tuple[str, int, int] | None = None

    @property
    def key(self):
        return self.role, _result(self.value, self.unit)

    def matches(self, item, date, *, extended=False):
        return (
            (not date or not self.date or normalize_document_time(date) == normalize_document_time(self.date))
            and all(
                not expected or not provided or _name(expected) == _name(provided)
                for expected, provided in ((self.specimen, item.get('specimen')), (self.method, item.get('method')))
            )
            and (
                not item.get('unit')
                or not self.unit
                or (
                    _unit_key(item['unit']) == _unit_key(self.unit)
                    if extended
                    else _literal(item['unit']) == _literal(self.unit)
                )
            )
        )


def _clinical_value(name, value):
    value = _literal(value)
    label, sep, conclusion = value.partition(':')
    return conclusion.strip() if sep and _name(label) == _name(name) else value


def _section_kind(line, *, recover_tables=False):
    if recover_tables and is_nonpatient_heading(line):
        return 'nonpatient'
    heading = re.sub(r'^\s*#{1,6}\s*', '', unicodedata.normalize('NFKC', line))
    heading = re.sub(r'^\s*(?:\d+(?:\.\d+)*[.、)]?\s+|\d+(?:\.\d+)*[.、)]|[一二三四五六七八九十]+[、.)])', '', heading)
    heading = ''.join(heading.split())
    if _BACKGROUND.match(heading):
        return 'background'
    if _RESULTS.match(heading):
        return 'results'
    return None


def _restore_result_table(lines, index):
    # A later table can qualify on its own. A results header never reopens
    # the surrounding advice or allows unlabelled text through this boundary.
    line = lines[index].strip()
    if not line.startswith('|'):
        return ()
    cells = {_name(cell) for cell in line.strip('|').split('|')}
    current_results = cells & {'检查结果', '检验结果', '检测结果', '本次结果'}
    if not (cells & _NAME_COLUMNS and current_results and cells & {'检查结果', '本次结果', '单位'}):
        return ()
    end = index + 1
    while end < len(lines) and lines[end].lstrip().startswith('|'):
        end += 1
    return lines[index:end] if end > index + 1 else ()


def _result_blocks(text, *, recover_tables=False):
    """Bound date and advice sections before inspecting structured rows or narrative."""
    block, date, active = [], None, True
    nonpatient = False
    start = offset = index = 0
    last_page_marker = ''
    lines = text.splitlines(keepends=True)
    while index < len(lines):
        line = lines[index]
        if recover_tables and _PAGE.match(line):
            if block:
                yield start, date, ''.join(block)
                block = []
            last_page_marker, date = line, None
        section = _section_kind(line, recover_tables=recover_tables)
        match = _DATE.match(line.rstrip('\r\n')) or (recover_tables and _PAGE_DATE.match(line))
        starts_table = recover_tables and not active and not nonpatient and _restore_result_table(lines, index)
        if section in {'background', 'nonpatient'} or (match and active):
            if block:
                yield start, date, ''.join(block)
            block = []
        if section in {'background', 'nonpatient'}:
            active = False
            nonpatient |= section == 'nonpatient'
        elif match and active:
            date = normalize_document_time(match[1])
        elif section == 'results' and not active:
            active, date, nonpatient = True, None, False
        if active:
            if not block:
                start = offset
                if recover_tables and last_page_marker and not _PAGE.match(line):
                    start -= len(last_page_marker)
                    block.append(last_page_marker)
            block.append(line)
        elif starts_table:
            yield offset - len(last_page_marker), date, last_page_marker + ''.join(starts_table)
            offset += sum(map(len, starts_table))
            index += len(starts_table)
            continue
        offset += len(line)
        index += 1
    if block:
        yield start, date, ''.join(block)


def current_result_text(text, *, recover_tables=False):
    """Expose only result-bearing regions to the existing v16 source parsers."""
    return '\n'.join(block for _, _, block in _result_blocks(text, recover_tables=recover_tables))


def current_native_document(document, *, recover_tables=False):
    def is_result_table(table):
        headings = [_section_kind(line, recover_tables=recover_tables) for line in table.context.splitlines()]
        return next((kind for kind in reversed(headings) if kind), None) not in {'background', 'nonpatient'}

    return replace(
        document,
        text=current_result_text(document.text, recover_tables=recover_tables),
        tables=[table for table in document.tables if is_result_table(table)],
    )


def _text_occurrences(text, *, recover_tables=False):
    for offset, date, content in _result_blocks(text, recover_tables=recover_tables):
        for page, page_offset, block in _block_pages(content):
            yield from _block_occurrences(offset + page_offset, date, block, page, recover_tables=recover_tables)


def _markdown_rendering(lines, header_index):
    if header_index + 1 >= len(lines):
        return None
    end = header_index + 2
    while end < len(lines) and lines[end].lstrip().startswith('|'):
        end += 1
    rows = []
    for line in (lines[header_index], *lines[header_index + 2 : end]):
        content = line.strip().removeprefix('|').removesuffix('|')
        rows.append([value.strip() for value in content.split('|')])
    if len(rows) > 1 and len({len(row) for row in rows}) == 1:
        return _rendering_digest(rows), len(rows[0])
    return None


def _text_result_cell(lines, record, renderings):
    table = record.locator.get('table')
    if not isinstance(table, str) or not re.fullmatch(r'm\d+', table):
        return None
    index = int(table[1:])
    if index not in renderings:
        renderings[index] = _markdown_rendering(lines, index)
    if rendering := renderings[index]:
        digest, width = rendering
        row, col = divmod(record.locator['valueCell'], width)
        return digest, row, col
    return None


def _block_occurrences(offset, date, block, page, *, recover_tables=False):
    lines = block.splitlines()
    renderings = {}
    for unit in prepare_source_units(block):
        for record in unit.records:
            if item := record.observation():
                yield _Occurrence(
                    f'text:{offset}:table:{record.source_id}',
                    tuple(_name(name) for name in record.names),
                    record.value,
                    item.get('kind', 'measurement'),
                    item.get('observedAt') or date,
                    item.get('unit'),
                    item.get('specimen'),
                    item.get('method'),
                    item.get('referenceRange'),
                    item.get('flag'),
                    page=page,
                    label=item['itemName'],
                    rendered_cell=_text_result_cell(lines, record, renderings) if recover_tables else None,
                )

    # Report's existing parsers recognize result columns and the patient
    # conclusion section, including narrative rows. Tables above provide
    # more precise occurrence and specimen/method information for scalars.
    clinical = parsing._generic_summary_facts(
        [line for raw in block.splitlines() if (line := parsing._clean_document_line(raw))],
        source_name=None,
        source_page=page,
        report_date=date,
    )
    summary_count = len(clinical)
    table_document = parsing.ExtractedDocument(
        '', 'document_text', '', tables=parsing._markdown_tables(block, page_number=page)
    )
    clinical.extend(parsing._generic_clinical_facts_from_tables(table_document, report_date=date))
    for index, fact in enumerate(clinical):
        role = 'measurement' if fact.category == 'laboratory' else 'assessment'
        value = fact.value if role == 'measurement' else _clinical_value(fact.item_name, fact.conclusion)
        if value:
            yield _Occurrence(
                f'text:{offset}:clinical:{index}',
                (_name(fact.item_name),),
                value,
                role,
                fact.check_time,
                fact.unit,
                reference_range=fact.reference_range,
                flag=fact.status,
                page=page,
                label=fact.item_name,
                in_summary=index < summary_count,
            )

    for index, line in enumerate(lines):
        inline = parsing._generic_inline_laboratory_facts(
            [line],
            source_name=None,
            source_page=page,
            report_date=date,
        )
        if inline and not _NON_RESULT_LABEL.match(_name(inline[0].item_name)):
            fact = inline[0]
            yield _Occurrence(
                f'text:{offset}:line:{index}',
                (_name(fact.item_name),),
                fact.value,
                'measurement',
                date,
                fact.unit,
                reference_range=fact.reference_range,
                flag=fact.status,
                page=page,
                label=fact.item_name,
            )
        elif literal := _INLINE_RESULT.match(line):
            name, value, unit = literal.group('name', 'value', 'unit')
            if (
                parsing._looks_like_item_name(name)
                and parsing._looks_like_unit(unit)
                and not _NON_RESULT_LABEL.match(_name(name))
            ):
                yield _Occurrence(
                    f'text:{offset}:line:{index}',
                    (_name(name),),
                    value,
                    'measurement',
                    date,
                    unit,
                    page=page,
                    label=name,
                )
        match = _IMAGING.match(line)
        if not match:
            continue
        role = 'findings' if match[1] == '所见' else 'diagnosis'
        content = [match[2].strip()]
        for following in lines[index + 1 :]:
            if not following.strip() or _FIELD.match(following) or following.lstrip().startswith(('#', '|')):
                break
            content.append(following.strip())
        # Expose the exact labelled line and, when present, its whole
        # bounded continuation as aliases of the same physical occurrence.
        for value in dict.fromkeys((content[0], '\n'.join(content).strip())):
            if value and len(value) <= 20000:
                yield _Occurrence(f'text:{offset}:imaging:{index}', (), value, role, date, page=page)


def _native_occurrences(document):
    if document is None:
        return
    for index, row in enumerate(parsing.original_clinical_table_rows(document)):
        extra = row.model_extra or {}
        yield _Occurrence(
            f'native:table:{index}',
            (_name(row.item_name),),
            str(row.test_result_value),
            'measurement',
            row.report_time,
            extra.get('unit'),
            reference_range=row.reference_range,
            flag=row.result_status_name,
            page=extra.get('sourcePage'),
            label=row.item_name,
            result_bounds=extra.get('sourceResultBounds'),
            name_bounds=extra.get('sourceNameBounds'),
        )
    for index, fact in enumerate(parsing._generic_clinical_facts_from_tables(document, report_date=None)):
        if fact.category != 'laboratory' and fact.conclusion:
            yield _Occurrence(
                f'native:conclusion:{index}',
                (_name(fact.item_name),),
                _clinical_value(fact.item_name, fact.conclusion),
                'assessment',
                fact.check_time,
                page=fact.source_page,
                label=fact.item_name,
            )


def _same_context(left, right):
    return (
        (not left.date or not right.date or normalize_document_time(left.date) == normalize_document_time(right.date))
        and (left.page is None or right.page is None or left.page == right.page)
        and all(
            not a or not b or _name(a) == _name(b)
            for a, b in ((left.specimen, right.specimen), (left.method, right.method))
        )
    )


def _original_occurrences(text, native_document, *, include_summary=False, recover_tables=False):
    archived = list(_text_occurrences(text, recover_tables=recover_tables))
    structured = {
        (row.locator.split(':', 2)[1], row.key, name)
        for row in archived
        if ':table:' in row.locator
        for name in row.names
    }
    native = list(_native_occurrences(native_document))
    if recover_tables and native_document is not None:
        native = [replace(row, date=row.date or native_document.native_page_dates.get(row.page)) for row in native]
    native_by_name = defaultdict(list)
    for row in native:
        for name in row.names:
            native_by_name[(row.role, name)].append(row)
    result = list(native)
    for row in archived:
        if (
            ':clinical:' in row.locator
            and not (include_summary and row.in_summary)
            and any((row.locator.split(':', 2)[1], row.key, name) in structured for name in row.names)
        ):
            continue
        # Native result cells are authoritative where their context overlaps
        # OCR. They keep their physical identities even when OCR does not
        # distinguish multiple identical cells on the same page.
        matching_native = {
            source for name in row.names for source in native_by_name[(row.role, name)] if _same_context(source, row)
        }
        if (include_summary and row.in_summary) or not matching_native:
            result.append(row)
        elif recover_tables and row.date and len(matching_native) == 1:
            source = next(iter(matching_native))
            matches = [other for other in archived if _same_reading(source, other)]
            if not source.date and len(matches) == 1 and matches[0] == row:
                result[result.index(source)] = replace(source, date=row.date)
    return result


def _same_reading(left, right):
    return (
        left.role == right.role
        and bool(set(left.names) & set(right.names))
        and _same_context(left, right)
        and _value_key(left.value, left.unit) == _value_key(right.value, right.unit)
    )


def _archive_dates(layouts):
    by_page, past_table = defaultdict(set), set()
    for layout in layouts:
        if not isinstance(layout, dict):
            continue
        pages = layout.get('pageNum')
        if not isinstance(pages, list) or len(pages) != 1 or type(pages[0]) is not int or pages[0] < 0:
            continue
        page, text = pages[0] + 1, layout.get('text')
        if layout.get('type') == 'table' or (
            isinstance(text, str) and _section_kind(text, recover_tables=True) in {'background', 'nonpatient'}
        ):
            past_table.add(page)
        if page not in past_table and layout.get('type') == 'text' and isinstance(text, str):
            if match := _PAGE_DATE.match(text.strip()):
                by_page[page].update(document_dates(match[1]))
    return by_page


def _with_reference_flag(row):
    if row.role != 'measurement' or row.flag:
        return row
    value, interval = _QUANTITY.fullmatch(_literal(row.value)), _EXPLICIT_RANGE.fullmatch(_literal(row.reference_range))
    if value and value[3]:
        return replace(row, value=row.value.rstrip('↑↓').strip(), flag=value[3])
    if not value or value[1] or not interval:
        return row
    result = Decimal(value[2])
    low, high = map(Decimal, interval.groups())
    if low > high:
        return row
    flag = 'low' if result < low else 'high' if result > high else 'normal'
    return replace(row, flag=flag)


def _table_readings(layouts, document, page_sizes):
    dates = _archive_dates(layouts)
    if document is not None:
        for page, date in document.native_page_dates.items():
            dates[page].add(date)
        for row in document.native_result_rows:
            yield _Occurrence(
                row.locator,
                (_name(row.label),),
                row.value,
                'measurement',
                row.date,
                row.unit,
                reference_range=row.reference_range,
                flag=row.flag,
                page=row.page,
                label=row.label,
                result_bounds=row.result_bounds,
                name_bounds=row.name_bounds,
            )
    for row in archived_table_rows(layouts, page_sizes=page_sizes):
        candidates = dates[row.page]
        date = row.date or (next(iter(candidates)) if len(candidates) == 1 else None)
        yield _Occurrence(
            row.locator,
            tuple(_name(name) for name in row.names),
            row.value,
            row.kind,
            date,
            row.unit,
            row.specimen,
            row.method,
            row.reference_range,
            row.flag,
            row.page,
            row.label,
            result_bounds=row.result_bounds,
            name_bounds=row.name_bounds,
            rendered_cell=row.rendered_cell,
        )


def _collapse_text_archive_mirrors(text_rows, archive_rows):
    text_by_cell, archive_by_cell = defaultdict(list), defaultdict(list)
    for rows, index in ((text_rows, text_by_cell), (archive_rows, archive_by_cell)):
        for row in rows:
            if row.page is not None and row.rendered_cell:
                index[(row.page, row.rendered_cell)].append(row)
    hidden, rekeyed = set(), {}
    for key, texts in text_by_cell.items():
        archives = archive_by_cell.get(key, ())
        if len(texts) != 1 or len(archives) != 1 or not _same_reading(texts[0], archives[0]):
            continue
        text, archive = texts[0], archives[0]
        hidden.add(text.locator)
        rekeyed[archive.locator] = replace(archive, locator=text.locator, date=archive.date or text.date)
    return (
        [row for row in text_rows if row.locator not in hidden],
        [rekeyed.get(row.locator, row) for row in archive_rows],
    )


def _overlapping_bounds(a, b):
    if a is None or b is None:
        return False
    overlap = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    smaller = min((r[2] - r[0]) * (r[3] - r[1]) for r in (a, b))
    return smaller > 0 and overlap / smaller >= 0.55


def _overlapping_result_cells(left, right):
    return (
        left.role == right.role
        and _overlapping_bounds(left.result_bounds, right.result_bounds)
        and (bool(set(left.names) & set(right.names)) or _overlapping_bounds(left.name_bounds, right.name_bounds))
    )


def _merge_table_readings(originals, readings):
    # Only merge a pair of views when both identify a single compatible
    # reading on the same page. Repeated measurements retain their positions.
    readings = list(readings)
    merged, hidden = [], set()
    for row in originals:
        matches = [
            other
            for other in readings
            if row.page is not None and row.page == other.page and _overlapping_result_cells(row, other)
        ]
        if len(matches) == 1:
            other = matches[0]
            if sum(item.page == other.page and _overlapping_result_cells(item, other) for item in originals) == 1:
                hidden.add(other.locator)
                if _same_reading(row, other):
                    row = replace(
                        row,
                        date=row.date or other.date,
                        reference_range=row.reference_range or other.reference_range,
                        flag=row.flag or other.flag,
                    )
        merged.append(row)
    merged.extend(row for row in readings if row.locator not in hidden)
    return merged


def _with_native_inline_occurrences(originals, document):
    # The archived parser may print native table cells as an inline result.
    # A separate line in native text proves its own occurrence; table cell
    # spans are returned separately by native text extraction.
    native_inline = [row for row in _text_occurrences(document.text) if ':line:' in row.locator]
    if not native_inline:
        return originals
    unmatched = list(originals)
    for native in native_inline:
        for index, row in enumerate(unmatched):
            if (
                row.locator.startswith('text:')
                and ':line:' in row.locator
                and row.names == native.names
                and row.key == native.key
                and _same_context(native, row)
            ):
                del unmatched[index]
                break
    return [*unmatched, *(replace(row, locator='native:' + row.locator) for row in native_inline)]


def _occurrence_index(rows):
    occurrences = defaultdict(list)
    for row in rows:
        for name in dict.fromkeys(row.names or ('',)):
            occurrences[(row.role, name)].append(row)
    return occurrences


def _matching_occurrences(rows, item, role, date, *, used=(), extended=False):
    key = _value_key if extended else _result
    value = key(item['resultValue'], item.get('unit'))
    return [
        row
        for row in rows
        if row.role == role
        and key(row.value, row.unit) == value
        and row.locator not in used
        and row.matches(item, date, extended=extended)
    ]


def _match(rows, item, role, date, *, used=(), extended=False):
    # Rank evidence before testing the model value or contextual fields.
    # A contradiction in a known result cell must not fall through to a
    # less specific scalar view of that result.
    tables = [row for row in rows if ':table:' in row.locator and row.locator.startswith('text:')]
    native = [row for row in rows if row.locator.startswith(('native:table:', 'native:conclusion:', 'native:result:'))]
    archive = [row for row in rows if row.locator.startswith('archive:table:')]
    matching = _matching_occurrences(
        [*tables, *native, *archive] or rows, item, role, date, used=used, extended=extended
    )
    if date and any(row.date for row in matching):
        matching = [row for row in matching if row.date]
    for field in ('specimen', 'method'):
        if item.get(field) and any(getattr(row, field) for row in matching):
            matching = [row for row in matching if getattr(row, field)]
    by_occurrence = {row.locator: row for row in matching}
    return next(iter(by_occurrence.values())) if len(by_occurrence) == 1 else None


def verified_current_report(snapshot, native_document):
    """Return supported fixed context and source occurrences for this original."""
    extended = snapshot.include_analysis_observation_refs
    originals = _original_occurrences(snapshot.text, native_document, include_summary=extended, recover_tables=extended)
    if extended:
        readings = tuple(_table_readings(snapshot.archive_layouts, native_document, snapshot.archive_page_sizes))
        native = [row for row in readings if row.locator.startswith('native:')]
        archive = [row for row in readings if row.locator.startswith('archive:')]
        originals, archive = _collapse_text_archive_mirrors(originals, archive)
        readings = _merge_table_readings(native, archive)
        originals = _merge_table_readings(originals, readings)
        originals = [_with_reference_flag(row) for row in originals]
    if snapshot.include_analysis_observation_refs and native_document is not None:
        originals = _with_native_inline_occurrences(originals, native_document)
    occurrences = _occurrence_index(originals)
    locators_by_ref = defaultdict(set)
    used_imaging = set()

    def prove(refs, rows):
        if not snapshot.include_analysis_observation_refs:
            return
        locators = {row.locator for row in rows}
        for ref in refs:
            if ref and locators:
                locators_by_ref[ref].update(locators)

    def visit(node):
        sections = [visit(child) for child in node.get('sections', ())]
        date = node.get('reportTime')
        report = {key: node[key] for key in ('reportType', 'title', 'reportTime') if key in node}
        report['sections'] = sections
        report['observations'] = []
        for item in node.get('observations', ()):
            role = 'assessment' if item.get('kind') == 'assessment' else 'measurement'
            rows = occurrences[(role, _name(item.get('itemName')))]
            observed_at = item.get('observedAt') or date
            if row := _match(rows, item, role, observed_at, extended=extended):
                if extended:
                    matching = _matching_occurrences(rows, item, role, observed_at, extended=extended)
                    if {match.locator for match in matching} == {row.locator}:
                        prove((item.get('observationRef'), *item.get('mirrorObservationRefs', ())), (row,))
                report['observations'].append(
                    {
                        **item,
                        'observedAt': _source_date(
                            item.get('observedAt') or date,
                            row.date,
                            require_original=snapshot.include_analysis_observation_refs,
                        ),
                        'unit': row.unit,
                        'specimen': row.specimen,
                        'method': row.method,
                        'referenceRange': row.reference_range,
                        'flag': row.flag,
                        'sourceOccurrence': row.locator,
                        'sourcePage': row.page,
                    }
                )
        imaging = {}
        for field, value in node.get('expansion', {}).items():
            candidate = {'resultValue': value}
            used = used_imaging if field in node.get('derivedImagingFields', ()) else ()
            rows = occurrences[(field, '')]
            ref = node.get('imagingObservationRefs', {}).get(field)
            if row := _match(rows, candidate, field, date, used=used):
                matching = _matching_occurrences(rows, candidate, field, date)
                if not any(match.locator in used for match in matching):
                    prove((ref,), matching)
                used_imaging.add(row.locator)
                imaging[field] = {
                    'value': value,
                    'sourceOccurrence': row.locator,
                    'date': _source_date(date, row.date, require_original=snapshot.include_analysis_observation_refs),
                    'page': row.page,
                }
        if imaging:
            report['verifiedImaging'] = imaging
        return report

    report = visit(snapshot.report)
    refs_by_locator = defaultdict(list)
    for ref, locators in locators_by_ref.items():
        if len(locators) == 1:
            refs_by_locator[next(iter(locators))].append(ref)
    return report, originals, refs_by_locator


def _source_date(fixed, original, *, require_original=False):
    if not original:
        return None if require_original else fixed
    return fixed if normalize_document_time(fixed) == normalize_document_time(original) else original


def original_clinical_rows(snapshot, originals, contextual, consumed, refs_by_locator):
    """Use the verified occurrence even when model context is missing or ambiguous."""
    context = defaultdict(list)
    for row in contextual:
        context[row.model_extra['sourceOccurrence']].append(row)
    selected = {}
    for occurrence in originals:
        if occurrence.locator in selected or occurrence.locator in consumed:
            continue
        candidates = context[occurrence.locator]
        if candidates and len({row.model_extra['sourceReportType'] for row in candidates}) == 1:
            row = candidates[0]
        elif occurrence.names:
            row = _original_row(snapshot, occurrence)
        else:
            continue
        if refs := refs_by_locator.get(occurrence.locator):
            row = row.model_copy(update={'analysisObservationRefs': refs})
        selected[occurrence.locator] = row
    # Keep the fixed tree order (including independent root findings) for
    # contextual rows. Source-only rows retain their original order.
    order = dict.fromkeys(row.model_extra['sourceOccurrence'] for row in contextual)
    order.update(dict.fromkeys(selected))
    return [selected[locator] for locator in order if locator in selected]


def _original_row(snapshot, occurrence):
    assessment = occurrence.role == 'assessment'
    return HospitalDataInput(
        data_type='EXAM' if assessment else 'INSPECTION',
        item_name=occurrence.label,
        test_result_value=None if assessment else _result(occurrence.value, occurrence.unit),
        exam_result=occurrence.value if assessment else None,
        report_time=occurrence.date,
        reference_range=occurrence.reference_range,
        result_status_name=occurrence.flag,
        sourceFile=snapshot.file_name,
        sourceChecksum=snapshot.content_sha256,
        sourceDocumentId=snapshot.document_id,
        sourceOccurrence=occurrence.locator,
        sourcePage=occurrence.page,
        specimen=occurrence.specimen,
        method=occurrence.method,
    )


def remaining_source_context(originals):
    """Keep v16's specialized original sections outside generic clinical tables."""
    categories = {
        'sourceReportedBackground',
        'healthAssociation',
        'virulence',
        'structuralAssessment',
        'microbiome',
        'microbiomeSummary',
    }
    return [row for row in originals if (row.model_extra or {}).get('sourceCategory') in categories]
