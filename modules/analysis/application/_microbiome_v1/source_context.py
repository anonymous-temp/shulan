"""Document-owned v1 leaf algorithms frozen from efca880 Report source_context.

Only the native-source dependency closure is retained. Import bindings point to
this fixed package; future Report changes must not alter this implementation.
"""

from __future__ import annotations

import json
import re
import unicodedata

from modules.analysis.application._microbiome_v1 import parsing

_BACKGROUND = {
    '肠道健康问题': ('symptoms', '原报告记载的症状'),
    '症状': ('symptoms', '原报告记载的症状'),
    '主诉': ('symptoms', '原报告记载的症状'),
    '疾病情况': ('history', '原报告记载的病史'),
    '既往病史': ('history', '原报告记载的病史'),
    '用药情况': ('medication', '原报告记载的用药'),
    '目前用药': ('medication', '原报告记载的用药'),
}


_STRUCTURAL = {'肠型', '肠道菌群分型', '菌群分型', '平衡状态', '菌群平衡状态', '肠道微生态平衡状态'}


_FIELD = re.compile(r'^([^:：]{1,25})[:：]\s*(.*)$')


_EMPTY = {'', '/', '\\', '-', '—', '暂无', '未填写'}


def _cell(value):
    return ' '.join(str(value or '').split()).strip()


def _section(table, fallback):
    if table.section_title:
        return _cell(table.section_title)
    headings = re.findall(r'(?m)^\s*(?:\d+(?:\.\d+)*[.、]?[ \t]+)?([^\n]{2,55}(?:评估|筛查))\s*$', table.context or '')
    return _cell(headings[-1]) if headings else fallback


def _row(document, name, value, category, page, section, quote, **extra):
    return dict(
        dataType='EXAM',
        itemName=name,
        examResult=value,
        sourceReportType='microbiome',
        sourceCategory=category,
        sourcePage=page,
        sourceSection=section,
        sourceText=quote,
        sourceFile=document.filename,
        sourceChecksum=document.metadata.get('content_sha256'),
        reportTime=parsing._explicit_report_date(document),
        **extra,
    )


def _personal_context(text):
    if re.search(r'受检(?:人|者)信息|个人(?:信息|资料)|基本信息|患者(?:信息|资料)|病史(?:及|与)用药', text):
        return True
    return len(set(re.findall(r'(姓名|性别|年龄)[:：]', text))) >= 2


def _background_rows(document):
    result = []
    # Field labels bound the value. Tables are preferred over linear OCR since
    # a line break is not evidence that the following field belongs to this one.
    for table in document.tables:
        context = (table.context or '') + '\n' + '\n'.join(' | '.join(row) for row in table.rows)
        if not _personal_context(context):
            continue
        for cells in table.rows:
            for cell in cells:
                match = _FIELD.fullmatch(_cell(cell))
                if match and match[1] in _BACKGROUND and match[2] not in _EMPTY:
                    kind, name = _BACKGROUND[match[1]]
                    result.append(
                        _row(
                            document,
                            name,
                            match[2],
                            'sourceReportedBackground',
                            table.page_number,
                            '原报告受检人资料',
                            cell,
                            sourceBackgroundKind=kind,
                        )
                    )
    seen = {(row['sourcePage'], row['sourceBackgroundKind']) for row in result}
    for page, text in parsing._document_pages_with_numbers(document.text or ''):
        if not _personal_context(text):
            continue
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            match = _FIELD.fullmatch(line)
            if not match or match[1] not in _BACKGROUND:
                continue
            kind, name = _BACKGROUND[match[1]]
            if (page, kind) in seen:
                continue
            value = match[2]
            if not value:
                chunks = []
                for following in lines[index + 1 :]:
                    if (
                        _FIELD.match(following)
                        or re.fullmatch(r'[^\n]{1,20}(?:信息|说明)', following)
                        or following.startswith(('样本编号', '====='))
                        or re.fullmatch(r'\d+\s*/\s*\d+', following)
                    ):
                        break
                    chunks.append(following)
                value = ' '.join(chunks)
            if value not in _EMPTY and len(value) <= 4000:
                result.append(
                    _row(
                        document,
                        name,
                        value,
                        'sourceReportedBackground',
                        page,
                        '原报告受检人资料',
                        f'{match[1]}：{value}',
                        sourceBackgroundKind=kind,
                    )
                )
                seen.add((page, kind))
    return result


def _related_taxa(value):
    result = []
    for original in re.split(r'[,，、;；\n]+', value or ''):
        original = original.strip()
        match = re.fullmatch(r'(.+?)\s*([↑↓▲▼])', original)
        if match:
            # Native PDF soft wraps sometimes insert whitespace inside Chinese.
            name = re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])', '', match[1]).strip()
            result.append(dict(name=name, direction=match[2], sourceText=original))
    return result


def _genes(value):
    result = []
    for token in re.split(r'[,，;；\n]+', value or ''):
        # Whitespace in an otherwise atomic gene cell is a PDF soft wrap.
        token = re.sub(r'\s+', '', token)
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]*', token):
            result.append(token)
    return list(dict.fromkeys(result))


def source_context_records(document):
    """Return HospitalDataInput-compatible literal context and panel relations.

    ``detectedGenes`` and ``candidateGenes`` remain separate. A source topic
    marked 检出 means an association-panel result, never a clinical diagnosis.
    """
    if not re.search(r'菌群|微生态|宏基因', document.text or '') and not any(
        re.search(r'菌群|微生态|宏基因', table.context or '') for table in document.tables
    ):
        return []
    result = _background_rows(document)
    for table in document.tables:
        if not table.rows:
            continue
        header = [re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(cell or ''))) for cell in table.rows[0]]
        roles = {
            name: header.index(name)
            for name in ('健康项目', '检测结果', '异常指标', '名称', '检出毒力基因', '毒力基因列表')
            if name in header
        }
        if {'健康项目', '检测结果', '异常指标'} <= roles.keys():
            for cells in table.rows[1:]:
                name = parsing._cell(cells, roles['健康项目'])
                status = parsing._cell(cells, roles['检测结果'])
                evidence = parsing._cell(cells, roles['异常指标'])
                if not name or status not in {'检出', '未检出'}:
                    continue
                taxa = _related_taxa(evidence)
                result.append(
                    _row(
                        document,
                        name,
                        status + (f'；相关菌：{evidence}' if evidence not in _EMPTY else ''),
                        'healthAssociation',
                        table.page_number,
                        _section(table, '与健康相关肠道菌群评估'),
                        ' | '.join(cells),
                        resultStatusName=status,
                        sourceStatus=status,
                        relatedTaxa=taxa,
                    )
                )
        elif {'名称', '检测结果', '检出毒力基因', '毒力基因列表'} <= roles.keys():
            for cells in table.rows[1:]:
                name, status = (parsing._cell(cells, roles[key]) for key in ('名称', '检测结果'))
                if not name or status not in {'检出', '未检出'}:
                    continue
                detected = _genes(parsing._cell(cells, roles['检出毒力基因']))
                candidates = _genes(parsing._cell(cells, roles['毒力基因列表']))
                result.append(
                    _row(
                        document,
                        name,
                        status + (f'；检出基因：{",".join(detected)}' if detected else ''),
                        'virulence',
                        table.page_number,
                        '重要毒力基因评估',
                        ' | '.join(cells),
                        resultStatusName=status,
                        sourceStatus=status,
                        detectedGenes=detected,
                        candidateGenes=candidates,
                    )
                )
        # Only explicit two-column result tables can restore structural labels;
        # definitions of enterotypes and model/ratio legends cannot do so.
        if (
            len(header) == 2
            and header[0] in {'检测项目', '项目', '评估项目'}
            and header[1] in {'检测结果', '结果', '评估结果'}
        ):
            for cells in table.rows[1:]:
                name, value = (parsing._cell(cells, i) for i in (0, 1))
                if name in _STRUCTURAL and value not in _EMPTY and len(value) < 100:
                    result.append(
                        _row(
                            document,
                            name,
                            value,
                            'structuralAssessment',
                            table.page_number,
                            '肠道菌群总体评估',
                            ' | '.join(cells),
                        )
                    )
    return list({json.dumps(row, sort_keys=True, ensure_ascii=False): row for row in result}.values())


def source_table_section(document, table):
    """Resolve a continuation panel from preceding headings, never later prose."""
    if table.section_title:
        return _cell(table.section_title)
    heading = re.compile(
        r'(?m)^\s*(\d+(?:\.\d+)*[.、]?[ \t]+[^\n]{1,80}[A-Za-z\u3400-\u9fff][^\n]{0,80}|附录[^\n]{0,80})\s*$'
    )
    local = list(heading.finditer(table.context or ''))
    if local:
        return _cell(local[-1][1])
    previous = []
    if table.page_number is not None:
        for page, text in parsing._document_pages_with_numbers(document.text or ''):
            if page is not None and page < table.page_number:
                previous.extend(heading.finditer(text))
    return _cell(previous[-1][1]) if previous else None


def source_taxon_occurrences(document):
    """Read every positioned organism row, including repeated status conflicts."""
    from modules.analysis.application._microbiome_v1.document_source import printed_table_taxa

    rows = []
    printed_table_taxa(document, occurrences=rows)
    return rows


def split_taxon_source_rows(document):
    """Recover an atomic table row whose parenthesized Latin name wraps pages.

    A complete printed header and numeric row must end one page and the next
    page must start with that row's Latin name. A later prose mention, another
    numbered section or a name found elsewhere cannot complete the row.
    """
    header = re.compile(r'(?m)^\s*菌名\s*\n\s*样本含量(?:[（(]%[）)])?\s*\n\s*参考范围\s*\n\s*结果\s*\n')
    atomic = re.compile(
        r'(?P<chinese>[\u3400-\u9fff][\u3400-\u9fff· -]{0,55})\s*\n'
        r'\s*(?P<value>(?:[<>]=?|[≤≥])?[+-]?\d+(?:\.\d+)?%?)\s*\n'
        r'\s*(?P<range>\d+(?:\.\d+)?\s*[~～–-]\s*\d+(?:\.\d+)?)\s*\n'
        r'\s*(?P<status>正常|偏低|偏高|异常|未检出|检出|阴性|阳性|极低|极高)\s*(?:\n|$)'
    )
    latin = re.compile(r'[（(]([A-Z][a-z][A-Za-z0-9._-]*(?:\s+[A-Za-z][A-Za-z0-9._-]*)+)[）)]')
    heading = re.compile(r'\d+(?:\.\d+)*[.、]?[ \t]+[^\n]{1,80}[A-Za-z\u3400-\u9fff][^\n]{0,80}')
    pages = dict(parsing._document_pages_with_numbers(document.text or ''))
    result = []
    for page, text in pages.items():
        if page is None or page + 1 not in pages:
            continue
        next_lines = [line.strip() for line in pages[page + 1].splitlines() if line.strip()]
        continuation = latin.fullmatch(next_lines[0]) if next_lines else None
        anchors = list(header.finditer(text))
        if not continuation or not anchors:
            continue
        body = text[anchors[-1].end() :].strip()
        row = atomic.match(body)
        if not row:
            continue
        remaining = [line.strip() for line in body[row.end() :].splitlines() if line.strip()]
        if any(
            not (heading.fullmatch(line) or line.startswith('样本编号：') or re.fullmatch(r'\d+\s*/\s*\d+', line))
            for line in remaining
        ):
            continue
        headings = [line for line in remaining if heading.fullmatch(line)]
        section = headings[0] if len(headings) == 1 else None
        if section is None:
            prior = re.findall(
                r'(?m)^\s*(\d+(?:\.\d+)*[.、]?[ \t]+[^\n]{1,80}[A-Za-z\u3400-\u9fff][^\n]{0,80})\s*$',
                text[: anchors[-1].start()],
            )
            section = _cell(prior[-1]) if prior else None
        result.append(
            dict(
                latinName=continuation[1],
                chineseName=row['chinese'],
                value=row['value'],
                referenceRange=row['range'],
                sourceStatus=row['status'],
                unit=None,
                sourcePage=page,
                sourceContinuationPage=page + 1,
                sourceSection=section,
                sourceText=row[0].strip() + '\n' + next_lines[0],
                sourceFile=document.filename,
                sourceChecksum=document.metadata.get('content_sha256'),
                sourceRowOrigin='split-page-table',
            )
        )
    return result
