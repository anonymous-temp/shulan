"""Read literal microbiome facts from the permission-checked original PDF.

This adapter never downloads, runs OCR, calls a model or invents missing rows.
Source legends and printed grades remain distinct, including source conflicts.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from decimal import Decimal
from decimal import InvalidOperation

from modules.analysis.application._microbiome_v1 import parsing
from modules.analysis.application._microbiome_v1.document_source import strict_report_source_diseases
from modules.analysis.application._microbiome_v1.document_source import strict_report_source_records
from modules.analysis.application._microbiome_v1.source_context import source_context_records
from modules.analysis.application._microbiome_v1.source_context import source_taxon_occurrences
from modules.analysis.application.source_facts import SourceFacts
from modules.analysis.application.source_facts import source_key
from modules.analysis.application.source_units import prepare_source_units
from utils.report_flags import normalize_report_flag

_REPORT = re.compile(
    r'肠道(?:菌群|微生物|微生态)|宏基因(?:组)?(?:检测|测序)|gut\s+microbiome|microbiome\s+report', re.I
)
_RESULT_COLUMNS = re.compile(r'结果|含量|风险值|风险指数|评估值|丰度|实测值|检测值|测定值|得分')
_TEACHING_COLUMNS = re.compile(r'检测意义|食物来源|膳食来源|指导建议|参考文献|references', re.I)
MICROBIOME_SOURCE_REVISION = 'document-microbiome-source-v1'


def _literal(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or ''))).replace('，', ',')


def _cover_title(document):
    pages = parsing._document_pages_with_numbers(document.text or '')
    if not pages:
        return None
    lines = [line.strip() for line in pages[0][1].splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if not re.search(r'(?:检测|检验|分析|评估)报告$|\breport$', line, re.I):
            continue
        if _REPORT.search(line):
            return line
        if index and _REPORT.search(lines[index - 1]) and len(lines[index - 1]) < 80:
            return lines[index - 1] + line
    return None


def _function_summaries(document, records):
    for page, text in parsing._document_pages_with_numbers(document.text or ''):
        charts = [
            row
            for row in records
            if row.get('sourcePage') == page
            and row.get('sourceSection')
            and row.get('referenceRange')
            and '表示' in row['referenceRange']
        ]
        if len(charts) < 2:
            continue
        for match in re.finditer(r'从肠道菌群来看[，,]\s*您的(.{2,70}?)异常的包括[:：]([^。]{1,1500})。', text, re.S):
            section, value = (re.sub(r'\s+', '', part) for part in match.groups())
            if source_key(section) not in {source_key(row['sourceSection']) for row in charts}:
                continue
            if value != '无':
                names = {source_key(row['itemName']) for row in charts}
                entries = [part.strip() for part in re.split(r'[，,、;；]', value) if part.strip()]
                if not entries or any(
                    not re.fullmatch(r'.+[↑↓]', part) or source_key(part[:-1]) not in names for part in entries
                ):
                    continue
            yield dict(
                itemName=section + '（原报告异常项）',
                value=value,
                sourcePage=page,
                sourceSection=section,
                sourceText=match[0],
                sourceCategory='sourceFunctionSummary',
            )


def _verified_risk_cards(document, diseases):
    """Accept a page-local patient sentence only when its summary agrees exactly."""
    for page, text in parsing._document_pages_with_numbers(document.text or ''):
        compact = _literal(text)
        for model in diseases:
            if model.source_score is None or not model.source_level:
                continue
            pattern = (
                r'您患' + re.escape(_literal(model.name)) + r'(?:的)?风险值为:'
                r'([+-]?\d+(?:\.\d+)?%?)[,。;；]?属于(低风险|中低风险|中风险|中高风险|高风险)'
            )
            for match in re.finditer(pattern, compact):
                value, grade = match.groups()
                if (
                    _score(value) != _score(model.source_score)
                    or value.endswith('%') != model.source_score.endswith('%')
                    or grade != model.source_level
                ):
                    continue
                yield dict(
                    itemName=model.name,
                    value=value,
                    status=grade,
                    sourcePage=page,
                    sourceText=match[0],
                    sourceCategory='diseaseRiskCard',
                )


def _source_value(value):
    text = _literal(value).replace('−', '-').replace('–', '-')
    if re.fullmatch(r'[+-]?\d+(?:\.\d+)?', text):
        return 'number', Decimal(text)
    return 'literal', text


def _covered_ids(document, docmind, observations, summaries, cards, excluded):
    """Cover source units individually; a page can contain unrelated results."""
    layouts = (docmind.get('data') or {}).get('layouts') if isinstance(docmind, dict) else None
    if not isinstance(layouts, list) or not all(
        isinstance(row, dict) and isinstance(row.get('text'), str) for row in layouts
    ):
        return frozenset()
    content = '\n'.join(layout['text'] for layout in layouts)
    units = prepare_source_units(content, docmind=docmind)
    native_pages = dict(parsing._document_pages_with_numbers(document.text or ''))
    candidates = {}
    for item in observations:
        proof = item['evidence']
        locator, raw = proof['locator'], proof['rawFields']
        for name in raw['nameAliases']:
            candidates.setdefault((locator.get('physicalPage'), source_key(name)), []).append(item)
    verified_layouts = set(excluded)
    for index, layout in enumerate(layouts):
        pages = layout.get('pageNum') or []
        if len(pages) != 1 or type(pages[0]) is not int:
            continue
        page, text = pages[0] + 1, layout['text']
        if any(row['sourcePage'] == page and _literal(row['sourceText']) == _literal(text) for row in summaries):
            verified_layouts.add(index)
        local_cards = [row for row in cards if row['sourcePage'] == page]
        # Both the patient statement and the chart signature are required. A
        # different numeric figure on the same page never becomes covered.
        predicted = re.search(
            r'(?<![A-Za-z])(?:yourr?|pur)\s+predicted\s+(?:score|soore)\s*[:：]?\s*'
            r'([+-]?\d+(?:\.\d+)?%?)(?![\d.])',
            text,
            re.I,
        )
        if (
            layout.get('type') == 'figure'
            and len(local_cards) == 1
            and predicted
            and re.search(r'predicted\s+(?:score|soore)|疾病预测', text, re.I)
            and all(word in text for word in ('患病', '健康', '预测得分'))
            and _source_value(predicted[1]) == _source_value(local_cards[0]['value'])
        ):
            verified_layouts.add(index)
    covered = set()
    for unit in units:
        locator = unit.locator
        table = locator.get('table')
        layout_index = locator.get('layout')
        if layout_index is None and isinstance(table, str) and (match := re.fullmatch(r't(\d+)', table)):
            layout_index = int(match[1])
        if layout_index in verified_layouts:
            covered.add(unit.source_id)
            covered.update(record.source_id for record in unit.records)
            continue
        for record in unit.records:
            pages = record.locator.get('pages') or []
            if len(pages) != 1 or type(pages[0]) is not int:
                continue
            matches = [
                item
                for name in record.names
                for item in candidates.get((pages[0] + 1, source_key(name)), [])
                if _source_value(item['resultValue']) == _source_value(record.value)
            ]
            if not matches:
                continue
            # Missing OCR metadata is recoverable. Contradictory cells need a
            # native occurrence matching their complete original metadata.
            for field in ('flag', 'referenceRange', 'unit', 'observedAt'):
                value = record.fields.get(field)
                if value:
                    matches = [
                        item
                        for item in matches
                        if _literal(value) == _literal(item.get('rawFlag') if field == 'flag' else item.get(field))
                    ]
            scope_date = (record.locator.get('reportScope') or {}).get('reportTime')
            if scope_date:
                matches = [item for item in matches if item.get('observedAt') in (None, scope_date)]
            if matches:
                covered.add(record.source_id)
        # A merged, explicitly labelled taxon description has no patient result
        # cell. Verify the literal native paragraph before classifying this row.
        if table and type(layout_index) is int and layout_index < len(layouts):
            header = (layouts[layout_index]['text'].splitlines() or [''])[0]
            if re.search(r'菌名|拉丁学名|中文名', header) and _RESULT_COLUMNS.search(header):
                try:
                    raw = json.loads(unit.raw_text)
                    cells = raw.get('unresolvedCells', []) if isinstance(raw, dict) else []
                    values = {cell[5] for cell in cells if cell[5]}
                except (ValueError, TypeError, IndexError):
                    values = set()
                pages = locator.get('pages') or []
                if (
                    len(values) == 1
                    and len(pages) == 1
                    and type(pages[0]) is int
                    and re.match(r'^描述[:：]', next(iter(values)))
                    and _literal(next(iter(values))) in _literal(native_pages.get(pages[0] + 1, ''))
                ):
                    covered.add(unit.source_id)
    return frozenset(covered)


def _non_result_items(docmind, cards):
    """A plot caption is not a missing measurement; its pixels stay unverified."""
    layouts = (docmind.get('data') or {}).get('layouts') if isinstance(docmind, dict) else None
    if not isinstance(layouts, list) or not all(
        isinstance(row, dict) and isinstance(row.get('text'), str) for row in layouts
    ):
        return frozenset()
    names_by_layout = {}
    for index, layout in enumerate(layouts):
        pages = layout.get('pageNum') or []
        if layout.get('type') != 'figure' or len(pages) != 1 or type(pages[0]) is not int:
            continue
        local = [card for card in cards if card['sourcePage'] == pages[0] + 1]
        text = layout['text']
        if len(local) != 1 or not all(word in text for word in ('患病', '健康', '预测得分')):
            continue
        names = {
            match[0]
            for match in re.finditer(
                r'(?<![A-Za-z])(?:yourr?|pur)\s+predicted\s+(?:score|soore)(?![A-Za-z])', text, re.I
            )
        }
        title = local[0]['itemName'] + '疾病预测'
        if text.startswith(title):
            names.add(title)
            # OCR may concatenate a caption and vertical axis labels. The
            # prefix ends before the population/result region; no score is read.
            prefix = text.split('健康', 1)[0].strip()
            if len(prefix) <= 150 and re.search(r'predicted\s+(?:score|soore)', prefix, re.I):
                names.add(prefix)
        if names:
            names_by_layout[index] = names
    units = prepare_source_units('\n'.join(row['text'] for row in layouts), docmind=docmind)
    return frozenset(
        (unit.source_id, source_key(name))
        for unit in units
        for name in names_by_layout.get(unit.locator.get('layout'), ())
    )


def _aliases(name):
    names = {str(name or '').strip()}
    match = re.fullmatch(r'\s*(.*?)\s*[（(]([^()（）]+)[）)]\s*', str(name or ''))
    if match and bool(re.search(r'[\u3400-\u9fff]', match[1])) != bool(re.search(r'[\u3400-\u9fff]', match[2])):
        names.update(part.strip() for part in match.groups())
    return names - {''}


def _score(value):
    try:
        return Decimal(str(value).rstrip('%'))
    except InvalidOperation:
        return None


def _explicit_report(document):
    # A checkup mentioning intestinal flora in educational prose is not enough.
    if not _REPORT.search((document.text or '')[:24000]):
        return False
    for table in document.tables:
        if not table.rows or table.native_clinical_columns:
            continue
        header = ''.join(str(c or '') for c in table.rows[0])
        if re.search(r'菌名|拉丁学名|相对丰度', header) and _RESULT_COLUMNS.search(header):
            return True
        if any(
            re.search(r'肠道菌群(?:种类|多样性|分型)|肠道微生物组?健康指数', ' '.join(row)) for row in table.rows[1:]
        ):
            return True
    return False


def _excluded_layouts(docmind):
    layouts = (docmind.get('data') or {}).get('layouts') if isinstance(docmind, dict) else None
    excluded = set()
    for index, layout in enumerate(layouts if isinstance(layouts, list) else []):
        if not isinstance(layout, dict) or layout.get('type') != 'table':
            continue
        text = layout.get('text') or ''
        header = text.splitlines()[0] if text else ''
        if _TEACHING_COLUMNS.search(header) and not _RESULT_COLUMNS.search(header):
            excluded.add(index)
    return frozenset(excluded)


def _disease_occurrences(document, diseases):
    allowed = {
        (source_key(row.name), _score(row.source_score), row.source_level)
        for row in diseases
        if row.source_score is not None
    }
    covered = set()
    for table_index, table in enumerate(document.tables):
        if not table.rows or table.native_clinical_columns:
            continue
        header = table.rows[0]
        name_col = parsing._header_index_any(
            header, '疾病名称', '疾病类型', '疾病或健康问题', '健康问题', '风险名称', '名称'
        )
        value_col = parsing._header_index_any(header, '风险指数', '风险评分', '模型指数', '模型分值', '风险值')
        grade_col = parsing._header_index_any(header, '风险等级', '风险评估', '风险级别', '检测结果')
        if None in (name_col, value_col, grade_col):
            continue
        for row_index, cells in enumerate(table.rows[1:], 1):
            name, value, grade = (parsing._cell(cells, col) for col in (name_col, value_col, grade_col))
            key = (source_key(name), _score(value), grade)
            if key in allowed:
                covered.add(key)
                yield dict(
                    itemName=name,
                    value=value,
                    status=grade,
                    sourcePage=table.page_number,
                    sourceTableIndex=table_index,
                    sourceRowIndex=row_index,
                    sourceText=' | '.join(cells),
                    sourceSection=table.section_title,
                    sourceCategory='diseaseRisk',
                )
    # A source-only risk card is bounded to its own page by the existing reader.
    for page, text in parsing._document_pages_with_numbers(document.text or ''):
        single = parsing.ExtractedDocument(document.filename, 'pdf', f'===== PAGE {page} =====\n{text}')
        for disease in strict_report_source_diseases(single):
            key = (source_key(disease.name), _score(disease.source_score), disease.source_level)
            if key not in allowed or key in covered or disease.source_score is None:
                continue
            yield dict(
                itemName=disease.name,
                value=disease.source_score,
                status=disease.source_level,
                sourcePage=page,
                sourceText=text,
                sourceCategory='diseaseRisk',
            )


def _amr_occurrences(document):
    for table_index, table in enumerate(document.tables):
        single = parsing.ExtractedDocument(document.filename, 'pdf', '', tables=[table])
        for record in parsing.original_amr_table_rows(single):
            matches = [
                (index, row)
                for index, row in enumerate(table.rows)
                if record['itemName'] in row and record['value'] in row
            ]
            for row_index, cells in matches:
                aliases = set()
                header = [parsing._compact_metric_label(c) for c in table.rows[0]]
                for label in ('中文名', '拉丁学名'):
                    if label in header and (value := parsing._cell(cells, header.index(label))):
                        aliases.add(value)
                yield {
                    **record,
                    'sourcePage': table.page_number,
                    'sourceTableIndex': table_index,
                    'sourceRowIndex': row_index,
                    'sourceText': ' | '.join(cells),
                    'nameAliases': sorted(aliases),
                }


def _additional_printed_states(document, records):
    for table_index, table in enumerate(document.tables):
        if not table.rows or table.native_clinical_columns:
            continue
        header = table.rows[0]
        name_col = parsing._header_index_any(header, '检测项目', '检验项目', '项目', '评估项目', '指标名称')
        value_col = parsing._header_index_any(header, '检测结果', '评估值', '结果', '评估结果')
        if name_col is None or value_col is None:
            continue
        for row_index, cells in enumerate(table.rows[1:], 1):
            name, value = (parsing._cell(cells, col) for col in (name_col, value_col))
            matched = [
                row
                for row in records
                if row.get('sourcePage') == table.page_number
                and source_key(row['itemName']) == source_key(name)
                and row['value'] == value
            ]
            if not matched:
                continue
            for col, label in enumerate(header):
                label = ' '.join(str(label or '').split())
                state = parsing._cell(cells, col)
                if (
                    not re.search(r'状态$|分级$|等级$', label)
                    or not state
                    or len(state) > 100
                    or state in {'/', '\\', '-', '—'}
                    or any(row.get('status') == state for row in matched)
                ):
                    continue
                yield dict(
                    itemName=f'{name}（{label}）',
                    value=state,
                    sourcePage=table.page_number,
                    sourceTableIndex=table_index,
                    sourceRowIndex=row_index,
                    sourceText=' | '.join(cells),
                    sourceSection=table.section_title,
                    sourceCategory='sourcePrintedState',
                    referenceRange=matched[0].get('referenceRange'),
                )


def read_microbiome_source_facts(
    *,
    content: bytes | None,
    text: str,
    file_name: str,
    docmind: dict | None,
    implementation_revision: str = MICROBIOME_SOURCE_REVISION,
) -> SourceFacts | None:
    """Correct only explicit source facts for this one verified PDF original."""
    if implementation_revision != MICROBIOME_SOURCE_REVISION:
        raise ValueError('unsupported Document microbiome source implementation')
    if not isinstance(content, bytes) or b'%PDF' not in content[:1024]:
        return None
    try:
        document = parsing._extract_pdf(file_name, content, ocr=False)
    except parsing.SourceParseError:
        return None
    if not _explicit_report(document):
        return None
    checksum = hashlib.sha256(content).hexdigest()
    date = parsing._explicit_report_date(document)
    observations, replaced = [], set()

    def add(name, value, row, *, kind='assessment', status=None, reference=None, unit=None, aliases=(), origin=None):
        if not name or value is None or not str(value).strip():
            return
        locator = {
            key: row[key]
            for key in ('sourcePage', 'sourceSection', 'sourceTableIndex', 'sourceRowIndex')
            if row.get(key) is not None
        }
        if row.get('sourcePage') is not None:
            locator['physicalPage'] = row['sourcePage']
        locator.update(
            sourceFile=file_name,
            sourceChecksum=checksum,
            sourceCategory=row.get('sourceCategory') or row.get('category'),
        )
        raw = {
            key: row[key]
            for key in ('relatedTaxa', 'detectedGenes', 'candidateGenes', 'sourceBackgroundKind')
            if key in row
        }
        raw.update(
            itemName=name,
            resultValue=str(value),
            referenceRange=reference,
            unit=unit,
            flag=status,
            nameAliases=sorted({name, *aliases}),
        )
        identity = json.dumps(
            [locator, name, str(value), status, reference, origin], sort_keys=True, ensure_ascii=False
        )
        occurrence = hashlib.sha256(identity.encode()).hexdigest()
        proof = dict(locator=locator, occurrenceKey=occurrence, rawFields=raw)
        if row.get('sourceText'):
            proof['quote'] = row['sourceText']
        if origin:
            proof['gradeOrigin'] = origin
        source_id = 'nativepdf:' + occurrence[:24]
        proof['sourceId'] = source_id
        observations.append(
            dict(
                itemName=name,
                resultValue=str(value),
                referenceRange=reference,
                unit=unit,
                flag=normalize_report_flag(status),
                rawFlag=status,
                kind=kind,
                observedAt=date,
                sourceId=source_id,
                evidence=proof,
            )
        )
        replaced.update(source_key(alias) for alias in {name, *aliases} if alias)
        if status and normalize_report_flag(status) is None:
            add(
                name + '（原报告分级）',
                status,
                {**row, 'sourceCategory': 'sourcePrintedGrade'},
                reference=reference,
                origin='printed',
            )

    records = strict_report_source_records(document)
    for row in records:
        name = row['itemName']
        aliases = _aliases(name) | {row.get('metricName') or name}
        for code, canonical, alternatives, _ in parsing._TABLE_METRIC_DEFINITIONS:
            if code == row.get('metricCode'):
                aliases.update((canonical, *alternatives))
        if row.get('metricCode') == 'gmhi':
            aliases.update(
                prefix + suffix
                for prefix in ('肠道微生物健康指数', '肠道微生物组健康指数')
                for suffix in ('', '（GMWI2）')
            )
        add(
            name,
            row['value'],
            row,
            status=row.get('status'),
            reference=row.get('referenceRange'),
            unit=row.get('unit'),
            aliases=aliases,
            origin=row.get('sourceStatusOrigin'),
        )
        if row.get('sourceReferenceGrade'):
            add(
                name + '（原报告区间分级）',
                row['sourceReferenceGrade'],
                row,
                reference=row.get('referenceRange'),
                origin='reference',
            )

    for row in _additional_printed_states(document, records):
        add(row['itemName'], row['value'], row, reference=row.get('referenceRange'), origin='printed')

    summaries = list(_function_summaries(document, records))
    for row in summaries:
        add(row['itemName'], row['value'], row)

    diseases = strict_report_source_diseases(document)
    cards = list(_verified_risk_cards(document, diseases))
    for row in _disease_occurrences(document, diseases):
        aliases = {row['itemName'] + suffix for suffix in ('风险值', '风险指数', '风险评分')}
        add(row['itemName'], row['value'], row, status=row.get('status'), aliases=aliases, origin='printed')

    for row in _amr_occurrences(document):
        add(
            row['itemName'],
            row['value'],
            row,
            kind='measurement',
            status=row.get('status'),
            reference=row.get('referenceRange'),
            unit=row.get('unit'),
            aliases=row.get('nameAliases', ()),
            origin='printed',
        )

    for row in source_taxon_occurrences(document):
        name = row.get('chineseName') or row['latinName']
        aliases = _aliases(name) | {row['latinName']}
        if row.get('chineseName'):
            aliases.add(row['chineseName'] + '（' + row['latinName'] + '）')
        add(
            name,
            row.get('value'),
            row,
            kind='measurement',
            status=row.get('sourceStatus'),
            reference=row.get('referenceRange'),
            unit=row.get('unit'),
            aliases=aliases,
            origin='printed',
        )

    for row in source_context_records(document):
        name = row['itemName']
        if row.get('sourceCategory') == 'healthAssociation':
            name = '菌群关联筛查：' + name
        add(name, row.get('examResult'), row, status=None)
        for field, suffix in (('detectedGenes', '检出基因'), ('candidateGenes', '候选基因')):
            if row.get(field):
                add(row['itemName'] + '（' + suffix + '）', ','.join(row[field]), row)

    unique = {item['sourceId']: item for item in observations}
    if not unique:
        return None
    for item in unique.values():
        proof = item['evidence']
        if proof['locator'].get('sourceCategory') != 'diseaseRisk':
            continue
        occurrences = [
            dict(
                locator=dict(
                    physicalPage=row['sourcePage'],
                    sourceCategory='diseaseRiskCard',
                    sourceFile=file_name,
                    sourceChecksum=checksum,
                ),
                quote=row['sourceText'],
            )
            for row in cards
            if row['itemName'] == item['itemName']
            and _source_value(row['value']) == _source_value(item['resultValue'])
            and row['status'] == item.get('rawFlag')
        ]
        if occurrences:
            proof['sourceOccurrences'] = occurrences
    excluded = _excluded_layouts(docmind)
    covered = _covered_ids(document, docmind, tuple(unique.values()), summaries, cards, excluded)
    return SourceFacts(
        observations=tuple(unique.values()),
        replaced_names=frozenset(replaced),
        excluded_layouts=excluded,
        covered_source_ids=covered,
        non_result_items=_non_result_items(docmind, cards),
        title=_cover_title(document),
        report_time=date,
    )
