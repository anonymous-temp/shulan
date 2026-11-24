"""Recover evaluate AMR scalars from the exact selected original PDF table."""
from __future__ import annotations

import re
import unicodedata

from .schemas import HospitalDataInput


def _key(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or '')))


_AMR_TITLE = re.compile(r'(?:抗生素耐药基因(?:评估)?|抗菌药物耐药基因(?:评估)?|'
    r'(?:antibiotic|antimicrobial)\s+resistance\s+genes?)', re.I)
_AMR_LATIN_CLASSES = frozenset({'beta-lactam', 'aminoglycoside', 'oxazolidinone',
    'fluoroquinolone', 'sulfonamide', 'rifamycin', 'phenicol', 'tetracycline',
    'nitroimidazole', 'macrolide', 'glycopeptide', 'lincosamide', 'streptogramin',
    'nitrofuran', 'polymyxin', 'quinolone'})


def _numbered_headings(text):
    """Read independent numbered titles, including the PDF's split footer."""
    lines = [line.strip() for line in str(text or '').splitlines() if line.strip()]
    headings = []
    for index, line in enumerate(lines):
        match = re.fullmatch(r'([1-9]\d?(?:\.\d{1,2})+)\s+(.+)', line)
        if match:
            headings.append(match[2])
        elif re.fullmatch(r'[1-9]\d?(?:\.\d{1,2})+', line) and index + 1 < len(lines):
            title = lines[index + 1]
            if re.search(r'[A-Za-z\u4e00-\u9fff]', title):
                headings.append(title)
    return headings


def _continued_amr_table(document, table):
    """Allow one next-page class table after the same original's AMR title.

    A page's explanatory prose cannot establish this scope. Both the previous
    page's final numbered title and every native Latin class cell must agree;
    another numbered section before this table closes the continuation.
    """
    from .parsing import _document_pages_with_numbers

    if type(table.page_number) is not int or table.page_number < 2:
        return False
    pages = dict(_document_pages_with_numbers(document.text))
    previous = pages.get(table.page_number - 1)
    if table.page_number not in pages or previous is None:
        return False
    headings = _numbered_headings(previous)
    if not headings or not _AMR_TITLE.fullmatch(headings[-1]):
        return False
    current = _numbered_headings(table.context)
    if current and not _AMR_TITLE.fullmatch(current[-1]):
        return False
    header = [_key(cell) for cell in table.rows[0]] if table.rows else []
    if not {'拉丁学名', '中文名', '样本含量', '参考范围', '结果'} <= set(header):
        return False
    index = header.index('拉丁学名')
    classes = [_key(row[index]).casefold().replace('β', 'beta')
        for row in table.rows[1:] if len(row) == len(header)]
    return bool(classes) and len(classes) == len(table.rows) - 1 and all(
        name in _AMR_LATIN_CLASSES for name in classes)


def original_pdf_amr_rows(document):
    """Keep native class/value/range/status and physical page, without genes."""
    from .parsing import ExtractedDocument, original_amr_table_rows, _explicit_report_date
    if not document or not document.filename or not document.metadata.get('content_sha256'):
        return []
    result, seen = [], set()
    observed_at = _explicit_report_date(document)
    for table_index, table in enumerate(document.tables):
        header = {_key(cell) for cell in table.rows[0]} if table.rows else set()
        if {'拉丁学名', '中文名', '样本含量', '参考范围', '结果'} <= header:
            # The same five headings also describe ordinary genus/species
            # tables. A general resistance description is not an AMR section.
            title = re.compile(r'(?im)^\s*(?:\d+(?:\.\d+)+\s*)?'
                + _AMR_TITLE.pattern + r'\s*$')
            if (not title.search(table.context or '')
                    and not title.fullmatch(getattr(table, 'section_title', None) or '')
                    and not _continued_amr_table(document, table)):
                continue
        single = ExtractedDocument(document.filename, document.source_type, '', [table], document.metadata)
        for original in original_amr_table_rows(single):
            matching = [(i, cells) for i, cells in enumerate(table.rows)
                if any(_key(cell) == _key(original['itemName']) for cell in cells)
                and any(_key(cell) == _key(original['value']) for cell in cells)]
            if len(matching) != 1:
                continue
            row_index, cells = matching[0]
            identity = tuple(original.get(k) for k in ('itemName', 'antibioticClass', 'value',
                'unit', 'referenceRange', 'status'))
            if identity in seen:
                continue
            seen.add(identity)
            result.append(dict(original, reportTime=observed_at, sourcePage=table.page_number,
                sourceTableIndex=table_index, sourceRowIndex=row_index,
                sourceFile=document.filename, sourceChecksum=document.metadata['content_sha256'],
                sourceText=' | '.join(str(cell) if cell is not None else '' for cell in cells)))
    return result


def attach_original_pdf_amr(parsed, document):
    """Repair this evaluate copy only when the original file identity matches.

    The source summary 'no abnormal items detected' never becomes a zero class
    score or absence of every resistance gene. JSON selection never calls this
    function. Shared parsed snapshots and unrelated report paths are unchanged.
    """
    if (document is None or parsed.source_name != document.filename
            or not parsed.source_checksum
            or parsed.source_checksum != document.metadata.get('content_sha256')):
        return parsed
    originals = original_pdf_amr_rows(document)
    if not originals:
        return parsed
    output = parsed.model_copy(deep=True)
    names = {_key(row['itemName']) for row in originals}
    # Replace only this source's corresponding classes with their actual cells.
    # Other measurements and every original scalar's spelling remain intact.
    output.metrics = [row for row in output.metrics if _key(row.name) not in names
        and not (_key(row.name) == '抗生素耐药基因' and row.value == '未检出')]
    output.supplementary_hospital_data = [row for row in output.supplementary_hospital_data
        if not (row.model_extra.get('sourceReportType') == 'microbiome'
            and row.model_extra.get('sourceFile') in (None, document.filename)
            and row.model_extra.get('sourceChecksum') in (None, parsed.source_checksum)
            and (_key(row.item_name) in names or _key(row.item_name) == '抗生素耐药基因'
                and row.exam_result == '未检出'))]
    output.evaluation_amr_classes = [row for row in output.evaluation_amr_classes
        if not (_key(row.get('itemName')) in names
            and row.get('sourceFile') in (None, document.filename)
            and row.get('sourceChecksum') in (None, parsed.source_checksum))]
    for original in originals:
        row = HospitalDataInput(dataType='EXAM', itemName=original['itemName'],
            examResult=original['value'], unit=original.get('unit'),
            referenceRange=original.get('referenceRange'), resultStatusName=original.get('status'),
            reportTime=original['reportTime'], sourceReportType='microbiome',
            sourceCategory='antimicrobialResistance', sourceFile=document.filename,
            sourceChecksum=parsed.source_checksum, sourcePage=original['sourcePage'],
            sourceSection='抗生素耐药基因', sourceText=original['sourceText'])
        row._evaluation_record_id = (f'source:{parsed.source_checksum}:page:{original["sourcePage"]}'
            f':amr:{original["sourceTableIndex"]}:{original["sourceRowIndex"]}')
        output.supplementary_hospital_data.append(row)
        output.evaluation_amr_classes.append({key: original.get(key) for key in (
            'itemName', 'value', 'unit', 'status', 'antibioticClass', 'reportTime', 'sourceFile', 'sourceChecksum')})
    output._evaluation_source_audit = [*getattr(parsed, '_evaluation_source_audit', []),
        dict(kind='selected_pdf_amr_table', sourceFile=document.filename,
            sourceChecksum=parsed.source_checksum, originalRows=originals)]
    return output


def selected_pdf_amr_display_rows(rows, parsed):
    """Project one selected table, retaining its unchanged source inventory.

    Generic PDF readers can mistake the Latin/Chinese class-name columns for
    results, or repeat the overview's abnormal-panel summary as a measured
    class. Only the exact selected original's recovered table can resolve those
    mirrors. Other sources, conflicting cells and genuinely different dates
    remain independent observations.
    """
    from copy import deepcopy

    filename, checksum = getattr(parsed, 'source_name', None), getattr(parsed, 'source_checksum', None)
    originals = [row for audit in getattr(parsed, '_evaluation_source_audit', [])
        if audit.get('kind') == 'selected_pdf_amr_table'
        and audit.get('sourceFile') == filename and audit.get('sourceChecksum') == checksum
        for row in audit.get('originalRows', [])]
    if not filename or not checksum or not originals:
        return rows
    aliases = {_key(name) for original in originals
        for name in (original.get('itemName'), original.get('antibioticClass'),
            *str(original.get('sourceText') or '').split(' | ')[:2]) if name}
    locators = (f'source:{checksum}:page:', f'source:{filename}:page:')
    result, groups = [], {}

    def owned(row):
        return row.kind in {'metric', 'function'} and row.source_name == filename and any(
            (source.record_id or '').startswith(locators) for source in row.sources)

    def identity(row):
        return (_key(row.name), str(row.value), _key(row.unit),
            _key(row.reference_range), _key(row.source_status))

    def matched(row):
        return next((original for original in originals
            if identity(row) == (_key(original.get('itemName')), str(original.get('value')),
                _key(original.get('unit')), _key(original.get('referenceRange')),
                _key(original.get('status')))
            and (not row.observed_at or not original.get('reportTime')
                or row.observed_at[:10] == original['reportTime'][:10])), None)

    for row in rows:
        if not owned(row):
            result.append(row)
            continue
        name = _key(row.name)
        # These are category headings/summary cells, not individual class
        # measurements. The actual class table remains visible below.
        summary = name == '抗生素耐药基因' or bool(re.fullmatch(
            r'在\d+种抗生素耐药基因中[，,]?您肠道含量', name))
        shifted_name = name in aliases and _key(row.value) in aliases
        current_date = not row.observed_at or any(not original.get('reportTime')
            or row.observed_at[:10] == original['reportTime'][:10] for original in originals)
        if current_date and (shifted_name or summary and not re.fullmatch(
                r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)', str(row.value))):
            continue
        original = matched(row)
        if original is None:
            result.append(row)
            continue
        key = identity(row) + (original.get('reportTime'),)
        if key not in groups:
            groups[key] = [len(result), deepcopy(row)]
            result.append(groups[key][1])
            continue
        index, previous = groups[key]
        # Prefer the exact recovered cell's date and row locator. Merge every
        # genuine mirror's references rather than dropping its provenance.
        recovered = any(':amr:' in (source.record_id or '') for source in row.sources)
        chosen = deepcopy(row) if recovered else previous
        for other in (previous, row):
            for source in other.sources:
                if source not in chosen.sources:
                    chosen.sources.append(deepcopy(source))
            for fact_id in other.fact_ids:
                if fact_id not in chosen.fact_ids:
                    chosen.fact_ids.append(fact_id)
        result[index] = chosen
        groups[key][1] = chosen
    return result
