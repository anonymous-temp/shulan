"""Request-free Report extraction over archived shared-service snapshots.

No downloads, OCR submission, patient overrides or latest-report selection take
place here. The shared service owns full text/AI extraction and version caching;
Report retains its table relationships and source-fact interpretation.
"""

from __future__ import annotations

import re
import unicodedata

from pydantic import Field

from modules.report import parsing
from modules.report.current_source_evidence import current_native_document
from modules.report.current_source_evidence import current_result_text
from modules.report.current_source_evidence import original_clinical_rows
from modules.report.current_source_evidence import remaining_source_context
from modules.report.current_source_evidence import verified_current_report
from modules.report.schemas import HospitalDataInput
from modules.report.schemas import ParsedMicrobiomeReport
from modules.report.schemas import ParsedTaxon
from modules.report.schemas import SourceMetric
from modules.report.schemas import stable_id
from modules.report.source_context import clean_source_hospital_rows
from modules.report.source_context import clean_source_taxa
from modules.report.source_context import source_context_records
from modules.report.source_context import source_enterotype
from modules.report.source_context import source_table_section
from modules.report.source_context import split_taxon_source_rows

EXTRACTOR_VERSION = 'report-source-v16'


class EvaluationDiseaseEvidenceMetric(SourceMetric):
    """A verified disease card's own table, in the excluded evaluate lane."""

    disease_name: str
    disease_score: str
    disease_level: str
    report_date: str | None = None
    source_page: int
    source_file: str
    source_checksum: str
    taxa: list[dict] = Field(default_factory=list)
    source_notice: str | None = None


def evaluation_disease_evidence_metrics(document):
    """Copy only a same-page risk card and its actual correlation results.

    The model's educational counts are not patient observations. A literal
    no-abnormality result is kept separately from measured organism rows.
    Neither this helper nor its metadata changes the shared ParsedDisease.
    """
    checksum = document.metadata.get('content_sha256')
    if not checksum or not document.filename:
        return []
    result = []
    date = parsing._explicit_report_date(document)
    for page, text in parsing._document_pages_with_numbers(document.text or ''):
        compact = re.sub(r'\s+', '', text)
        if (
            type(page) is not int
            or '疾病正、负相关菌检出结果' not in compact
            or '疾病风险评估' not in compact
            or '风险为' not in compact
        ):
            continue
        page_document = parsing.ExtractedDocument(
            document.filename,
            document.source_type,
            text,
            tables=[table for table in document.tables if table.page_number == page],
        )
        cards = strict_report_source_diseases(page_document)
        identities = {
            (card.name, card.source_score, card.source_level)
            for card in cards
            if card.name and card.source_score is not None and card.source_level is not None
        }
        # Positioned cards may place score and level on one line, while the
        # shared parser expects separate lines. Both layouts still require
        # the repeated patient-card title and its own explicit risk label.
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        printed_titles = []
        for position, line in enumerate(lines):
            if line != '疾病风险评估' or position == 0:
                continue
            printed_title = lines[position - 1]
            name = parsing._normalize_source_disease_name(printed_title)
            if not name:
                continue
            printed_titles.append((printed_title, name))
            body = re.sub(r'\s+', '', ''.join(lines[position + 1 :])).split('疾病正、负相关菌检出结果', 1)[0]
            label = r'(?:低风险|中低风险|中风险|中高风险|高风险)'
            score_pattern = r'\d{1,3}(?:\.\d+)?'
            literal_title = re.escape(re.sub(r'\s+', '', printed_title))
            patterns = (
                rf'{literal_title}风险为[:：]?({score_pattern})({label})',
                rf'{literal_title}({score_pattern})({label})风险为[:：]?',
            )
            for pattern in patterns:
                if match := re.fullmatch(pattern, body):
                    identities.add((name, match[1], match[2]))
        if len(identities) != 1:
            continue
        name, score, level = next(iter(identities))
        taxa = []
        for table in page_document.tables:
            if not table.rows:
                continue
            context = re.sub(r'\s+', '', table.context or '')
            if '疾病正、负相关菌检出结果' not in context or not any(
                re.sub(r'\s+', '', title) in context for title, normalized in printed_titles if normalized == name
            ):
                continue
            headers = [re.sub(r'\s+', '', str(cell or '')) for cell in table.rows[0]]
            required = ('拉丁学名', '中文名称', '相关性', '检测结果', '参考范围', '是否异常')
            if any(headers.count(label) != 1 for label in required):
                continue
            roles = {label: headers.index(label) for label in required}
            for row_index, row in enumerate(table.rows[1:], 1):
                values = {label: parsing._cell(row, index) for label, index in roles.items()}
                latin, chinese = values['拉丁学名'], values['中文名称']
                value, status = values['检测结果'], values['是否异常']
                if (
                    not re.fullmatch(r'[A-Za-z][A-Za-z0-9._ -]*', latin)
                    or not _MEASURED_VALUE.fullmatch(value)
                    or values['相关性'] not in {'+', '-', '＋', '－'}
                    or status not in {'正常', '超标', '缺乏', '偏高', '偏低', '异常', '未检出', '检出'}
                ):
                    continue
                actual = dict(
                    name=chinese or latin,
                    scientificName=latin,
                    value=value,
                    unit='%' if value.endswith('%') else None,
                    sourceStatus=status,
                    referenceRange=values['参考范围'] or None,
                    correlation=values['相关性'],
                    sourceRow=row_index,
                    sourceText=' | '.join(row),
                )
                if actual not in taxa:
                    taxa.append(actual)
        notice = (
            '本次检测未检出与该疾病相关的正、负相关菌异常'
            if '本次检测未检出与该疾病相关的正、负相关菌异常' in compact
            else None
        )
        if not taxa and not notice:
            continue
        # A conflicting page cannot silently turn actual abnormal rows into
        # a normal statement. Its measured rows remain available on their own.
        if taxa:
            notice = None
        result.append(
            EvaluationDiseaseEvidenceMetric(
                code='disease_source_evidence:' + stable_id('card', '|'.join((name, score, level, str(page)))),
                name=name,
                disease_name=name,
                disease_score=score,
                disease_level=level,
                report_date=date,
                source_page=page,
                source_file=document.filename,
                source_checksum=checksum,
                taxa=taxa,
                source_notice=notice,
            )
        )
    return result


_SCIENTIFIC_NAME = re.compile(r'[A-Z][a-z]+(?: [a-z][a-z/-]+| (?:sp|spp)\.| subsp\. [a-z]+){1,3}')
_MEASURED_VALUE = re.compile(
    r'(?:[<>]=?|[≤≥])?\s*[+-]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?\s*%?'
    r'|\d+\+|<\s*DL|not detected|未检出',
    re.I,
)
_TAXON_HEADING = re.compile(
    r'Genus\s*/\s*species|(?:Scientific|Latin)\s+name|拉丁学名|菌名|菌群组成'
    r'|Bacteria\s*(?:\([^)]*\))?\s*Result|Bacteriology\s*\(?Culture'
    r'|Organism\s*(?:Result|Count|Abundance)',
    re.I,
)


def _scalar(value):
    return str(value).strip() if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None


def _source_metric_identity(name):
    """Exact source terminology, independent of patient, filename and score."""
    compact = parsing._compact_metric_label(name)
    extra = {
        '肠道微生态总评分': ('gut_overall_score', '肠道总评分'),
        '肠道微生物组健康指数gmwi2': ('gmhi', '肠道微生物健康指数'),
        '肠道菌群多样性': ('diversity', '菌群多样性'),
        '肠道年龄评估': ('gut_age', '肠道年龄'),
        '您的肠道年龄': ('gut_age', '肠道年龄'),
        '您的实际年龄': ('chronological_age', '来源报告实际年龄'),
        '肠道年龄差': ('gut_age_difference', '肠道年龄差'),
        '肠道菌群定植抗力': ('be_ratio', '肠道微生物定植抗力'),
        '厚壁菌门拟杆菌门fb': ('fb_ratio', '厚壁菌门/拟杆菌门'),
        '肠道抗炎能力': ('anti_inflammatory', '肠道抗炎能力'),
        '肠道免疫力': ('gut_immunity', '肠道免疫力'),
        '嘌呤': ('purine_metabolism', '嘌呤代谢能力'),
        '类胡萝卜素': ('carotenoid', '类胡萝卜素合成能力'),
        '类黄酮': ('flavonoid', '类黄酮合成能力'),
    }
    if compact in extra:
        return extra[compact]
    if match := re.fullmatch(r'维生素([a-k](?:\d+)?)', compact):
        return 'vitamin_' + match[1], name + '合成能力'
    for code, canonical, aliases, _ in parsing._TABLE_METRIC_DEFINITIONS:
        if compact in {parsing._compact_metric_label(v) for v in (canonical, *aliases)}:
            return code, canonical
    return metric_identity(name)


def _printed_gmwi_method_legend(text):
    """Copy a complete, explicitly named five-band source method verbatim.

    Printed inequalities/degree labels may disagree with the source's result
    label. They remain literal metadata and never reclassify the measurement.
    """
    text = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text or ''))
    if 'GMWI2' not in text.upper():
        return None
    match = re.search(r'根据评估的数值将肠道菌群健康状况分为([^。]{10,200}?其对应分值区间分别为[^。]{3,100})。', text)
    if not match:
        return None
    labels, bounds = match[1].split('其对应分值区间分别为', 1)
    labels = re.findall(r'(?:不健康倾向|健康倾向|亚健康|不健康|健康)\([+-]?\d+度\)', labels)
    intervals = re.split(r'[、,，]', bounds)
    number = r'[+-]?(?:\d+(?:\.\d+)?|\.\d+)'
    interval = rf'(?:[<>]=?|[≤≥]){number}|{number}[~～–—-]{number}'
    if len(labels) != 5 or len(intervals) != 5 or not all(re.fullmatch(interval, value) for value in intervals):
        return None
    return match[0]


def strict_report_source_records(document):
    """Read explicit summary columns and bounded, source-labelled score charts.

    A chart must have its own numbered function section, result statement and
    complete multi-band legend on the same page. Adjacent label/value pairs
    are observations, never each other's reference interval. Source boundary
    values belonging to two printed bands keep an empty grade. The origin of
    any private grade obtained by looking up the source legend stays explicit;
    the original status remains empty when it is not printed for that item.
    """
    result = []
    date = parsing._explicit_report_date(document)
    pages = dict(parsing._document_pages_with_numbers(document.text or ''))
    legend_pattern = re.compile(
        r'(?m)^[ \t]*(极低|偏低|正常|偏高)[ \t]*[:：][ \t]*[（(]'
        r'[ \t]*(\d+(?:\.\d+)?)[ \t]*[-–—至][ \t]*(\d+(?:\.\d+)?)'
        r'[ \t]*[）)][ \t]*表示[^\n]*'
    )

    def record(name, value, *, page, reference=None, status=None, origin=None, section=None, quote=None):
        identity = _source_metric_identity(name)
        return {
            'itemName': name,
            'value': value,
            'unit': None,
            'referenceRange': reference,
            'status': status,
            'sourcePage': page,
            'sourceFile': document.filename,
            'sourceChecksum': document.metadata.get('content_sha256'),
            'reportTime': date,
            'sourceCategory': 'microbiome',
            'sourceSubcategory': section,
            'sourceSection': section,
            'sourceText': quote,
            'sourceStatusOrigin': origin,
            'metricCode': identity[0] if identity else None,
            'metricName': identity[1] if identity else None,
        }

    for page, raw_text in pages.items():
        text = unicodedata.normalize('NFKC', raw_text)
        section_match = re.search(r'(?m)^[ \t]*\d+(?:\.\d+)+[ \t]+([^\n]{2,55}(?:代谢|能力))\s*$', text)
        bands = list(legend_pattern.finditer(text))
        if not section_match or len(bands) < 2 or '异常的包括' not in text:
            continue
        legend = '\n'.join(item[0].strip() for item in bands)
        if len({(m[1], m[2], m[3]) for m in bands}) != len(bands):
            continue
        lines = [line.strip() for line in text[: bands[0].start()].splitlines() if line.strip()]
        pairs = []
        index = len(lines)
        while index >= 2:
            name, value = lines[index - 2 : index]
            if (
                re.fullmatch(r'[A-Za-zα-ωΑ-Ω\u3400-\u9fff][A-Za-z0-9α-ωΑ-Ω\u3400-\u9fff ()·-]{0,39}', name)
                and re.fullmatch(r'\d+(?:\.\d+)?', value)
                and 0 <= float(value) <= 100
            ):
                pairs.insert(0, (name, value))
                index -= 2
            else:
                break
        if len(pairs) < 2:
            continue
        for name, value in pairs:
            statuses = {m[1] for m in bands if float(m[2]) <= float(value) <= float(m[3])}
            status = next(iter(statuses)) if len(statuses) == 1 else None
            item = record(
                name, value, page=page, reference=legend, section=section_match[1], quote=f'{name}\n{value}\n{legend}'
            )
            item.update(sourceGradeOrigin='reference' if status else None, sourceReferenceGrade=status)
            result.append(item)

    for table in document.tables:
        if not table.rows or table.native_clinical_columns:
            continue
        header = [parsing._compact_metric_label(cell) for cell in table.rows[0]]
        # A two-row age table prints three patient values horizontally, unlike
        # a reference legend whose rows are multiple ranges and advice text.
        if len(table.rows) == 2 and header and all(_source_metric_identity(cell) for cell in table.rows[0]):
            for name, value in zip(table.rows[0], table.rows[1]):
                if re.fullmatch(r'[-+]?\d+(?:\.\d+)?', value.strip()):
                    result.append(
                        record(
                            name,
                            value.strip(),
                            page=table.page_number,
                            origin=None,
                            section=table.section_title,
                            quote=' | '.join((name, value)),
                        )
                    )
            continue
        roles = {}
        labels = {
            'name': {'检测项目', '检验项目', '项目', '评估项目', '指标名称'},
            'value': {'检测结果', '评估值', '结果', '评估结果'},
            'status': {'结果评价', '说明', '肠道健康状态', '肠道状态'},
            'range': {'参考范围', '正常范围', '参考区间'},
        }
        for role, candidates in labels.items():
            indexes = [i for i, value in enumerate(header) if value in candidates]
            if len(indexes) == 1:
                roles[role] = indexes[0]
        if not {'name', 'value'} <= roles.keys():
            continue
        for cells in table.rows[1:]:
            name = parsing._cell(cells, roles['name'])
            value = parsing._cell(cells, roles['value'])
            if not _source_metric_identity(name) or not re.fullmatch(r'[-+]?\d+(?:\.\d+)?', value):
                continue
            status = parsing._cell(cells, roles.get('status')) or None
            if status and ('见' in status or status in {'/', '\\', '-'} or re.search(r'\d|[<>≤≥]', status)):
                status = None
            reference = parsing._cell(cells, roles.get('range')) or None
            if reference in {'/', '\\', '-'}:
                reference = None
            if not reference and _source_metric_identity(name)[0] in {'gmhi', 'gmwi2'}:
                reference = _printed_gmwi_method_legend(pages.get(table.page_number, ''))
            result.append(
                record(
                    name,
                    value,
                    page=table.page_number,
                    reference=reference,
                    status=status,
                    origin='printed' if status else None,
                    section=table.section_title,
                    quote=' | '.join(cells),
                )
            )
    return list({tuple(row.items()): row for row in result}.values())


def _strict_source_metrics(records):
    """Preserve competing explicit scalars; never choose a conflicting score."""
    metrics = [
        SourceMetric(
            code=row['metricCode'],
            name=row['itemName'],
            value=row['value'],
            unit=row['unit'],
            reference_range=row['referenceRange'],
            status=row['status'],
        )
        for row in records
        if row['metricCode']
    ]
    result = []
    for code in dict.fromkeys(item.code for item in metrics):
        candidates = [item for item in metrics if item.code == code]
        fields = {
            field: {getattr(item, field) for item in candidates if getattr(item, field) is not None}
            for field in ('value', 'unit', 'reference_range', 'status')
        }
        if any(len(values) > 1 for values in fields.values()):
            result.extend(parsing._dedupe(candidates, key=lambda item: item.model_dump_json()))
        else:
            result.append(
                SourceMetric(
                    code=code,
                    name=candidates[0].name,
                    **{field: next(iter(values)) if values else None for field, values in fields.items()},
                )
            )
    return result


def strict_report_source_diseases(document):
    """Only complete risk-table cells or atomic same-page source card rows.

    No association-panel label, source prose, neighbouring row or scale legend
    provides a patient score. Conflicting explicit rows remain separate.
    """
    tables = []
    for table in document.tables:
        if not table.rows:
            continue
        header = table.rows[0]
        name_index = parsing._header_index_any(
            header, '疾病名称', '疾病类型', '疾病或健康问题', '健康问题', '风险名称', '名称'
        )
        score_index = parsing._header_index_any(header, '风险指数', '风险评分', '模型指数', '模型分值', '风险值')
        level_index = parsing._header_index_any(header, '风险等级', '风险评估', '风险级别', '检测结果')
        if name_index is not None and score_index is not None and level_index is not None:
            rows = [header]
            for row in table.rows[1:]:
                name = parsing._cell(row, name_index)
                score = parsing._cell(row, score_index)
                level = parsing._cell(row, level_index)
                if (
                    name
                    and len(name) <= 60
                    and not re.search(r'[|｜。；;，,:：]', name)
                    and re.fullmatch(r'-?\d+(?:\.\d+)?%?', score)
                    and re.fullmatch(r'低风险|中低风险|中风险|中高风险|高风险|低相似|中相似|高相似|注意|需关注', level)
                ):
                    rows.append(row)
            tables.append(parsing.ExtractedTable(table.page_number, rows, table.context))
        elif parsing._disease_risk_context_active(table.context):
            # Existing unresolved provider exports still carry actual named
            # source items; image placeholders never yield a numeric score.
            rows = [
                row
                for row in table.rows
                if len(row) >= 3
                and parsing._looks_like_disease_or_health_problem(row[0])
                and parsing._is_source_image_placeholder(row[1])
                and parsing._is_source_image_placeholder(row[2])
            ]
            if rows:
                tables.append(parsing.ExtractedTable(table.page_number, rows, table.context))
    rows = parsing._document_diseases(
        parsing.ExtractedDocument(document.filename, document.source_type, '', tables=tables)
    )
    for _, page_text in parsing._document_pages_with_numbers(document.text or ''):
        rows.extend(parsing._disease_similarity_results(page_text))
        # Printed repeated-name cards separate their risk label and value.
        # Running the existing reader on this single proven card cannot cross
        # a page boundary or absorb an unrelated section's score.
        card_text = '\n'.join(line.strip() for line in page_text.splitlines() if line.strip())
        if re.search(r'^([^\n]{2,60})\n疾病风险评估\n\1\n风险为', card_text):
            rows.extend(
                parsing._document_diseases(
                    parsing.ExtractedDocument(document.filename, document.source_type, card_text)
                )
            )
    descriptions = parsing._document_disease_descriptions(document.text, disease_names=(row.name for row in rows))
    for row in rows:
        row.source_description = row.source_description or descriptions.get(row.name.casefold())
    merged = {}
    for row in rows:
        score = row.source_score
        if score and re.fullmatch(r'-?\d+\.\d+', score):
            score = score.rstrip('0').rstrip('.')
        key = (row.name, score, row.source_level, row.source_description)
        if key in merged:
            merged[key].related_taxa = list(dict.fromkeys([*merged[key].related_taxa, *row.related_taxa]))
        else:
            merged[key] = row
    return list(merged.values())


def _strict_hospital_rows(records):
    return [
        HospitalDataInput(
            data_type='INSPECTION',
            item_name=row['itemName'],
            test_result_value=row['value'],
            reference_range=row['referenceRange'],
            result_status_name=row['status'],
            report_time=row['reportTime'],
            sourceReportType='microbiome',
            **{key: value for key, value in row.items() if key.startswith('source')},
        )
        for row in records
    ]


def _is_replaced_source_row(row, records):
    """Drop misaligned legacy chart rows only where a chart is source proven."""
    charts = [
        record
        for record in records
        if record.get('sourceGradeOrigin') == 'reference'
        or record['referenceRange']
        and '表示' in record['referenceRange']
    ]
    names = {record['itemName'] for record in records}
    metadata = row.model_extra or {}
    if row.item_name in names:
        return True
    if (metadata.get('source_page') or metadata.get('sourcePage')) in {record['sourcePage'] for record in charts}:
        return True
    compact = parsing._compact_metric_label(row.item_name)
    for record in charts:
        label = parsing._compact_metric_label(record['itemName'])
        if compact.startswith(label) and (compact[len(label) :].startswith(record['value']) or '得分' in row.item_name):
            return True
    return False


def evaluation_panel_summary_metrics(document):
    """Preserve explicitly printed distribution-heading ratios for evaluate.

    These headings do not define their numerator/denominator as detected,
    abnormal or tested counts. Keep the literal ratio separate from ordinary
    source metrics and PanelCountAudit; organism rows never supply a missing
    numerator, denominator or health grade. Conflicting source candidates stay
    distinct so the evaluate projection can decline to select either value.
    """
    labels = {
        '有益菌': 'panel_title_beneficial',
        '致病菌': 'panel_title_pathogenic',
        '条件致病菌': 'panel_title_opportunistic',
        '部分与健康相关的其他菌种': 'panel_title_other',
        '其他健康相关菌': 'panel_title_other',
    }
    label_pattern = '|'.join(re.escape(label) for label in labels)
    heading = re.compile(rf'(?P<name>{label_pattern})\s*\(\s*(?P<left>\d+)\s*/\s*(?P<right>\d+)\s*\)')
    active = False
    found = {}
    for raw_line in unicodedata.normalize('NFKC', document.text or '').splitlines():
        line = re.sub(r'^[\s#>*●•·-]+|[\s*]+$', '', raw_line).strip()
        compact = re.sub(r'\s+', '', line)
        if compact in {'肠道菌群精确分布', '肠道菌群精准分布'}:
            active = True
            continue
        if compact in {
            '慢性疾病风险评估',
            '疾病风险评估',
            '肠菌参与代谢能力评估',
            '肠道菌群总体评估',
            '肠道菌群全面评估',
        }:
            active = False
        match = heading.fullmatch(line) if active else None
        if match is None:
            continue
        left, right = match['left'], match['right']
        if int(right) <= 0 or int(left) > int(right):
            continue
        metric = SourceMetric(code=labels[match['name']], name=match['name'], value=f'{left}/{right}')
        found.setdefault((metric.code, metric.name, metric.value), metric)
    return list(found.values())


def evaluation_amr_class_rows(document):
    """Bind literal AMR class cells to the exact source and original scalar.

    The existing native table reader resolves merged cells. No drug-name or
    mechanism dictionary supplies a category, and conflicting originals remain
    separate candidates for the evaluate-only projection to reject.
    """
    checksum = document.metadata.get('content_sha256')
    if not checksum or not document.filename:
        return []
    date = parsing._explicit_report_date(document)
    rows = {}
    for original in parsing.original_amr_table_rows(document):
        row = {key: original.get(key) for key in ('itemName', 'value', 'unit', 'status', 'antibioticClass')}
        row.update(reportTime=date, sourceFile=document.filename, sourceChecksum=checksum)
        rows.setdefault(tuple(row.items()), row)
    return list(rows.values())


def metric_identity(name):
    """Exact terminology -> existing Report code, never a disease/organism whitelist."""
    label = unicodedata.normalize('NFKC', name or '').strip().casefold()
    definitions = (
        *parsing._TABLE_METRIC_DEFINITIONS,
        *((code, name, (), unit) for code, name, _, unit in parsing._DOCUMENT_METRICS),
        ('gut_overall_score', '肠道总评分', ('肠道总体评分',), None),
        ('be_ratio', '肠道微生物定植抗力', ('肠道定植抗力',), None),
        ('gmhi', '肠道微生物健康指数', ('肠道微生物健康指数(GMWI2)', '肠道微生物组健康指数(GMWI2)'), None),
        ('panel_beneficial', '有益菌总检出丰度', ('有益菌',), '%'),
        ('panel_harmful', '有害菌总检出丰度', ('有害菌',), '%'),
    )
    for code, canonical, aliases, _ in definitions:
        if canonical.endswith('合成能力'):
            aliases = ()  # A metabolite concentration is not a synthesis score.
        if label in {unicodedata.normalize('NFKC', v).casefold() for v in (canonical, *aliases)}:
            return code, canonical
    return None


def is_scale_label(value):
    """Exclude an axis/legend carrying both endpoints, not a patient's result."""
    text = ' '.join(str(value or '').split())
    return bool(re.fullmatch(r'LOW (.+) HIGH \1', text, flags=re.I))


def printed_table_taxa(document, *, occurrences=None):
    """Retain measured taxa outside named panels, without inferring a disease.

    A literal scientific-name column establishes taxonomy even for a genus or
    family absent from the main panel. Numeric disease risk/association columns
    are not measurements. Existing main-panel interpretation stays authoritative.
    """
    taxa = parsing._document_taxa(document)
    # Keep this source-table path separate from the established named-panel
    # parser above: its categories remain authoritative for existing reports.
    # New bilingual tables and explicit taxonomic overview columns may carry
    # repeated observations with different printed flags or ranges. Retain
    # those candidates instead of selecting whichever page appeared first.
    measured = []
    layout = None
    previous_page = None
    pending = None
    source_category = 'other'
    pages = dict(parsing._document_pages_with_numbers(document.text or ''))
    section_categories = {
        '有益菌': 'beneficial',
        '致病菌': 'pathogenic',
        '条件致病菌': 'opportunistic',
        '潜在致病真菌': 'fungi_pathogenic',
        '致病性真菌': 'fungi_pathogenic',
    }

    def bilingual_name(raw):
        match = re.fullmatch(r'\s*(.*?)\s*[（(]([^()（）]+)[）)]\s*', raw)
        if not match:
            return None
        chinese, latin = (part.strip() for part in match.groups())
        if chinese and not re.search(r'[\u3400-\u9fff]', chinese):
            if not re.fullmatch(r'[A-Za-z0-9 ._/-]+', chinese):
                return None
            chinese = None  # An untranslated/truncated label is not Chinese.
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9._-]*(?:\s+[A-Za-z0-9][A-Za-z0-9._-]*)*', latin):
            return None
        return chinese or None, latin

    def keep_occurrence(item, table, row_index, raw, *, page=None):
        if occurrences is None:
            return
        section = source_table_section(document, table)
        record = item.model_dump(mode='json', by_alias=True)
        record.update(
            sourcePage=table.page_number if page is None else page,
            sourceSection=section,
            sourceText=' | '.join(raw),
            sourceTableIndex=document.tables.index(table),
            sourceRowIndex=row_index,
            sourceFile=document.filename,
            sourceChecksum=document.metadata.get('content_sha256'),
        )
        if record not in occurrences:
            occurrences.append(record)

    def append_measured(name, value, reference, status, unit, category, table, row_index, raw, *, page=None):
        chinese, latin = name
        main = next((item for item in taxa if item.latin_name.casefold() == latin.casefold()), None)
        item = ParsedTaxon(
            taxon_id=main.taxon_id if main else stable_id('taxon', latin),
            category=main.category if main else category,
            latin_name=latin,
            chinese_name=chinese,
            value=value,
            unit=unit,
            reference_range=reference,
            source_status=status,
            explicitly_abnormal=parsing._is_explicit_abnormal(status, False),
            detection_status=parsing.detection_status(value, status=status),
        )
        measured.append(item)
        keep_occurrence(item, table, row_index, raw, page=page)

    for table in document.tables:
        if table.native_clinical_columns or not table.rows:
            continue
        header = [parsing._compact_metric_label(cell).replace('%', '') for cell in table.rows[0]]
        context = re.sub(r'\s+', '', table.context or '')
        # An AMR table uses a Latin drug-class column, not organism taxonomy.
        if (
            '抗生素耐药' in context
            or '抗菌药物耐药' in context
            or re.search(r'antibiotic|antimicrobial', context, re.I)
        ):
            layout, pending = None, None
            continue
        headings = list(re.finditer(r'(?m)^\s*\d+(?:\.\d+)+[.、]?[ \t]+([^\n]+)', table.context or ''))
        if headings:
            source_category = section_categories.get(headings[-1][1].strip(), 'other')
        roles = {}
        column_names = {
            'name': {'菌名'},
            'latin': {'拉丁学名', '学名'},
            'chinese': {'中文名', '中文名称'},
            'value': {'样本含量', '检测结果', '相对丰度', '丰度', '检测值'},
            'range': {'参考范围', '正常范围', '参考区间'},
            'status': {'结果', '说明', '结果评价', '是否异常', '状态'},
            'unit': {'单位'},
        }
        for role, labels in column_names.items():
            indexes = [index for index, label in enumerate(header) if label in labels]
            if len(indexes) == 1:
                roles[role] = indexes[0]
        combined_header = {'name', 'value', 'range', 'status'} <= roles.keys()
        # The older separate-Latin result layout is handled below unchanged.
        # Here, the explicit sample-content column also covers class/order/
        # family/genus overviews without assuming every organism is a species.
        separate_header = {'latin', 'chinese', 'value', 'range', 'status'} <= roles.keys() and header[
            roles['value']
        ] == '样本含量'
        if combined_header or separate_header:
            if (
                not headings
                and table.page_number is not None
                and previous_page is not None
                and table.page_number == previous_page + 1
            ):
                # A new panel may be introduced after the preceding page's
                # last table. Its heading does not reclassify that old table,
                # but does bind the next page's newly printed header.
                previous_headings = list(
                    re.finditer(r'(?m)^\s*\d+(?:\.\d+)+[.、]?[ \t]+([^\n]+)', pages.get(previous_page, ''))
                )
                if previous_headings:
                    source_category = section_categories.get(previous_headings[-1][1].strip(), 'other')
            layout = dict(
                roles,
                width=len(header),
                combined=combined_header,
                value_unit='%' if '%' in table.rows[0][roles['value']] else None,
            )
            pending = None
            rows = table.rows[1:]
        elif (
            layout
            and previous_page is not None
            and table.page_number is not None
            and previous_page <= table.page_number <= previous_page + 1
            and len(header) == layout['width']
            and not any(
                label in {'名称', '检测意义', '食物来源', '指导建议', '风险指数', '风险等级', '疾病名称'}
                for label in header
            )
        ):
            rows = table.rows
        else:
            layout, pending = None, None
            previous_page = table.page_number
            continue
        prior_page = previous_page
        previous_page = table.page_number
        for row_index, row in enumerate(rows):
            source_row_index = row_index + (1 if combined_header or separate_header else 0)
            raw = parsing._cell(row, layout.get('name')) if layout['combined'] else ''
            if (
                pending is not None
                and row_index == 0
                and table.page_number is not None
                and pending['page'] is not None
                and table.page_number == pending['page'] + 1
                and prior_page == pending['page']
                and raw
                and all(not parsing._cell(row, layout[key]) for key in ('value', 'range', 'status'))
            ):
                name = bilingual_name(pending['name'] + ' ' + raw)
                if name:
                    append_measured(
                        name,
                        pending['value'],
                        pending['reference'],
                        pending['status'],
                        pending['unit'],
                        pending['category'],
                        pending['table'],
                        pending['row_index'],
                        [*pending['raw'], *row],
                        page=pending['page'],
                    )
                pending = None
                continue
            value = parsing._cell(row, layout['value'])
            reference = parsing._cell(row, layout['range'])
            status = parsing._cell(row, layout['status']) or None
            if not _MEASURED_VALUE.fullmatch(value) or not re.search(r'\d\s*[~～–-]\s*\d', reference):
                continue
            unit = parsing._cell(row, layout.get('unit')) or layout['value_unit']
            if layout['combined']:
                name = bilingual_name(raw)
                if name is None:
                    pending = None
                    if re.fullmatch(r'[\u3400-\u9fff][\u3400-\u9fff· -]{0,55}', raw):
                        pending = dict(
                            name=raw,
                            value=value,
                            reference=reference,
                            status=status,
                            unit=unit,
                            category=source_category,
                            page=table.page_number,
                            table=table,
                            row_index=source_row_index,
                            raw=row,
                        )
                    continue
            else:
                latin = parsing._cell(row, layout['latin'])
                if not parsing._looks_like_latin_taxon(latin):
                    continue
                name = parsing._normalize_source_chinese_name(parsing._cell(row, layout['chinese'])), latin
            pending = None
            if not name:
                continue
            append_measured(name, value, reference, status, unit, source_category, table, source_row_index, row)
    additions = {}
    for table in document.tables:
        header = table.rows[0] if table.rows else []
        indexes = parsing._microbiome_panel_indexes(header)
        if not indexes or indexes.get('latin') is None or indexes.get('value') is None:
            continue
        for row_index, row in enumerate(table.rows[1:], start=1):
            name = parsing._cell(row, indexes['latin']).strip()
            raw = parsing._cell(row, indexes['value']).strip()
            if not parsing._looks_like_latin_taxon(name):
                continue
            # A row must carry an actual measured scalar, not a percentile,
            # clinical statement or a source risk level.
            value = raw.rstrip('↑↓▲▼').strip()
            if not _MEASURED_VALUE.fullmatch(value):
                continue
            chinese = parsing._normalize_source_chinese_name(parsing._cell(row, indexes.get('chinese')))
            status = _scalar(parsing._cell(row, indexes.get('status'))) or None
            if not status and raw.endswith(('↑', '▲', '↓', '▼')):
                status = '偏高' if raw.endswith(('↑', '▲')) else '偏低'
            unit_index = parsing._header_index_any(header, '单位')
            unit = _scalar(parsing._cell(row, unit_index)) or None
            if not unit and ('%' in header[indexes['value']] or value.endswith('%')):
                unit = '%'
            item = ParsedTaxon(
                taxon_id=stable_id('taxon', name),
                category='other',
                latin_name=name,
                chinese_name=chinese,
                value=value,
                unit=unit,
                reference_range=_scalar(parsing._cell(row, indexes.get('range'))) or None,
                source_status=status,
                explicitly_abnormal=parsing._is_explicit_abnormal(status, False),
                detection_status=parsing.detection_status(value, status=status),
            )
            keep_occurrence(item, table, row_index, row)
            additions.setdefault(name.casefold(), {})[item.model_dump_json()] = item
    # An overview may omit a unit explicitly printed in the detail table.
    # Only identical remaining fields can share that printed unit. Different
    # values, ranges, directions or two explicit units stay ambiguous.
    for rows in additions.values():
        items = list(rows.values())
        main = next((item for item in taxa if item.latin_name.casefold() == items[0].latin_name.casefold()), None)
        if main is not None:
            # A later related-organism table may print a flag omitted from the
            # main panel. Fill only an empty flag for this same measured row;
            # keep panel classification and detection independent. Competing
            # explicit labels or measurement metadata cannot be reconciled.
            matching = [
                item
                for item in items
                if item.value == main.value
                and item.reference_range == main.reference_range
                and (item.unit is None or item.unit == main.unit)
            ]
            statuses = {item.source_status for item in matching if item.source_status}
            if not main.source_status and len(statuses) == 1:
                status = next(iter(statuses))
                if parsing._is_explicit_abnormal(status, False) == main.explicitly_abnormal:
                    main.source_status = status
            continue
        identities = {item.model_dump_json(exclude={'unit'}) for item in items}
        units = {item.unit for item in items if item.unit}
        if len(identities) == 1 and len(units) <= 1:
            taxa.append(next((item for item in items if item.unit), items[0]))
    for row in split_taxon_source_rows(document):
        main = next((item for item in taxa if item.latin_name.casefold() == row['latinName'].casefold()), None)
        item = ParsedTaxon(
            taxon_id=main.taxon_id if main else stable_id('taxon', row['latinName']),
            category=main.category if main else 'other',
            latin_name=row['latinName'],
            chinese_name=row['chineseName'],
            value=row['value'],
            unit=row['unit'],
            reference_range=row['referenceRange'],
            source_status=row['sourceStatus'],
            explicitly_abnormal=parsing._is_explicit_abnormal(row['sourceStatus'], False),
            detection_status=parsing.detection_status(row['value'], status=row['sourceStatus']),
        )
        measured.append(item)
        if occurrences is not None and not any(
            entry.get('sourcePage') == row['sourcePage']
            and entry.get('latinName') == item.latin_name
            and entry.get('value') == item.value
            and entry.get('sourceStatus') == item.source_status
            for entry in occurrences
        ):
            occurrences.append({**item.model_dump(mode='json', by_alias=True), **row})
    # Exact repetitions may share an entry; different printed values, ranges,
    # names or flags retain separate source candidates for the adapter audit.
    return parsing._dedupe([*taxa, *measured], key=lambda item: item.model_dump_json())


def _shared_metric(item):
    identity = metric_identity(item.get('itemName'))
    value, unit = _scalar(item.get('resultValue')), _scalar(item.get('unit'))
    if not identity or not value:
        return None
    code, name = identity
    if code.startswith('panel_') and '%' not in unicodedata.normalize('NFKC', value + (unit or '')):
        return None  # A printed organism count must not become relative abundance.
    return SourceMetric(
        code=code,
        name=name,
        value=value,
        unit=unit,
        reference_range=_scalar(item.get('referenceRange')),
        status=_scalar(item.get('flag')),
    )


def _shared_taxon(item, text):
    name, value = _scalar(item.get('itemName')), _scalar(item.get('resultValue'))
    if not name or not value or item.get('kind') != 'measurement' or not _MEASURED_VALUE.fullmatch(value):
        return None
    chinese = None
    if bilingual := re.fullmatch(r'(.+?)[（(]([^()（）]+)[）)]', name):
        chinese, name = (part.strip() for part in bilingual.groups())
        if not re.search(r'[\u3400-\u9fff]', chinese):
            return None
    if not _SCIENTIFIC_NAME.fullmatch(name):
        return None  # Do not guess an organism from an arbitrary English label.
    if re.search(r'\b(total|count|index|score|ratio|concentration|diversity|abundance|level|capacity)\b', name, re.I):
        return None  # Aggregate labels are measurements, not Latin organisms.
    # A capitalized English measurement (e.g. Fecal elastase) is not proof of
    # taxonomy. Require a printed organism section or explicit bilingual name.
    pages = re.split(r'(?im)^\s*(?:Page\s*\d+|第\s*\d+\s*页)\s*$', text)
    if not any(name in page and (_TAXON_HEADING.search(page) or chinese and '菌' in chinese) for page in pages):
        return None
    flag = _scalar(item.get('flag'))
    return ParsedTaxon(
        taxon_id=stable_id('taxon', name),
        category='other',
        latin_name=name,
        chinese_name=chinese,
        value=value,
        unit=_scalar(item.get('unit')),
        reference_range=_scalar(item.get('referenceRange')),
        source_status=flag,
        explicitly_abnormal=flag in {'偏高', '偏低', '异常'},
        detection_status='unknown',
    )


def _shared_microbiome_facts(report, text):
    metrics, taxa = [], []
    if report.get('reportType') == 'microbiome':
        for item in report.get('observations') or []:
            if not isinstance(item, dict) or not _scalar(item.get('resultValue')):
                continue
            if metric := _shared_metric(item):
                metrics.append(metric)
            elif taxon := _shared_taxon(item, text):
                taxa.append(taxon)
    for section in report.get('sections') or []:
        if isinstance(section, dict):
            child_metrics, child_taxa = _shared_microbiome_facts(section, text)
            metrics.extend(child_metrics)
            taxa.extend(child_taxa)
    return metrics, taxa


def _current_microbiome_occurrences(report, text):
    occurrences = set()
    if report.get('reportType') == 'microbiome':
        for item in report.get('observations', ()):
            if _shared_metric(item) or _shared_taxon(item, text):
                occurrences.add(item['sourceOccurrence'])
    for section in report.get('sections', ()):
        occurrences.update(_current_microbiome_occurrences(section, text))
    return occurrences


def _clinical_rows(report, filename, text='', *, accepted_observations=False, inherited_type=None, current_source=None):
    """Use source observations/imagery, never aiSummary or proposed diagnoses.

    Shared DocumentSnapshots contain accepted public observations: their private
    evidence is deliberately removed by report_public_content. Only that trusted
    boundary may use an assessment without its private quote. Unresolved items
    and summaries never participate in this projection.
    """
    rows = []
    report_type = report.get('reportType') or inherited_type
    observations = report.get('observations') or (
        report.get('expansion') if isinstance(report.get('expansion'), list) else []
    )
    for item in observations:
        if not isinstance(item, dict):
            continue
        name, value = _scalar(item.get('itemName')), _scalar(item.get('resultValue'))
        if not name or value is None or is_scale_label(value):
            continue
        assessment = item.get('kind') == 'assessment'
        if assessment:
            # Shared, source-bound checkup/imaging findings are patient facts,
            # unlike microbiome risk interpretations or generated summaries.
            evidence = item.get('evidence') or {}
            quote = _scalar(evidence.get('quote')) if isinstance(evidence, dict) else None
            source_bound = bool(quote and re.sub(r'\s+', '', value) in re.sub(r'\s+', '', quote))
            accepted_public = accepted_observations and 'evidence' not in item
            numeric_function = report_type == 'microbiome' and bool(
                _MEASURED_VALUE.fullmatch(value) and re.search(r'\d', value)
            )
            # A source-approved numeric model result is still a source fact.
            # Preserve it with its microbial provenance so Report classifies
            # it as auxiliary function data, never a clinical diagnosis.
            # Free-form microbial disease interpretations remain excluded.
            if (report_type not in {'checkup', 'imaging', 'lab'} and not numeric_function) or not (
                source_bound or accepted_public or current_source is not None
            ):
                continue
        if report_type == 'microbiome' and (
            _shared_metric(item) or _shared_taxon(item, text) or not _MEASURED_VALUE.fullmatch(value)
        ):
            continue
        unit = _scalar(item.get('unit'))
        if unit and not value.endswith(unit):
            value = f'{value} {unit}'
        provenance = {}
        if current_source is not None:
            provenance = {
                'sourceChecksum': current_source.content_sha256,
                'sourceDocumentId': current_source.document_id,
                'sourceOccurrence': item['sourceOccurrence'],
                'sourcePage': item['sourcePage'],
                'specimen': item['specimen'],
                'method': item['method'],
            }
        rows.append(
            HospitalDataInput(
                data_type='EXAM' if assessment else 'INSPECTION',
                item_name=name,
                test_result_value=None if assessment else value,
                exam_result=value if assessment else None,
                reference_range=_scalar(item.get('referenceRange')),
                result_status_name=_scalar(item.get('flag')),
                report_time=(
                    _scalar(item.get('observedAt'))
                    if current_source is not None and current_source.include_analysis_observation_refs
                    else _scalar(item.get('observedAt')) or _scalar(report.get('reportTime'))
                ),
                sourceFile=filename,
                sourceReportType=report_type,
                **provenance,
            )
        )
    if current_source is not None:
        for field, label in (('findings', '影像所见'), ('diagnosis', '影像诊断')):
            if evidence := report.get('verifiedImaging', {}).get(field):
                rows.append(
                    HospitalDataInput(
                        data_type='EXAM',
                        item_name=f'{report.get("title") or filename} {label}',
                        exam_result=evidence['value'],
                        report_time=evidence['date'],
                        sourceFile=filename,
                        sourceReportType=report_type,
                        sourceChecksum=current_source.content_sha256,
                        sourceDocumentId=current_source.document_id,
                        sourceOccurrence=evidence['sourceOccurrence'],
                        sourcePage=evidence['page'],
                    )
                )
    expansion = report.get('expansion')
    if report_type == 'imaging' and isinstance(expansion, dict):
        for key, label in (('findings', '影像所见'), ('diagnosis', '影像诊断')):
            value = _scalar(expansion.get(key))
            if value:
                rows.append(
                    HospitalDataInput(
                        data_type='EXAM',
                        item_name=f'{report.get("title") or filename} {label}',
                        exam_result=value,
                        report_time=_scalar(report.get('reportTime')),
                        sourceFile=filename,
                        sourceReportType=report_type,
                    )
                )
    for section in report.get('sections') or []:
        if isinstance(section, dict):
            rows.extend(
                _clinical_rows(
                    section,
                    filename,
                    text,
                    accepted_observations=accepted_observations,
                    inherited_type=report_type,
                    current_source=current_source,
                )
            )
    return rows


def _document_facts(document, checksum):
    strict_records = strict_report_source_records(document)
    explicit_metrics = _strict_source_metrics(strict_records)
    explicit_codes = {item.code for item in explicit_metrics}
    metrics = parsing._document_metrics(document)
    metrics = [item for item in metrics if item.code not in explicit_codes]
    metrics.extend(explicit_metrics)
    occurrences = []
    source_taxa = printed_table_taxa(document, occurrences=occurrences)
    taxa = parsing._merge_document_taxa(
        [
            *source_taxa,
            *parsing._document_taxa_from_linear_text(document.text),
        ]
    )
    diseases = strict_report_source_diseases(document)
    return ParsedMicrobiomeReport(
        source_type=document.source_type if metrics or taxa or diseases else 'pending',
        source_name=document.filename,
        source_checksum=checksum,
        subject=parsing._document_subject(document),
        sample=parsing._document_sample(document),
        metrics=metrics,
        taxa=taxa,
        disease_results=diseases,
        evaluation_panel_summaries=evaluation_panel_summary_metrics(document),
        evaluation_amr_classes=evaluation_amr_class_rows(document),
        enterotype=source_enterotype(document),
        source_taxon_occurrences=occurrences,
        supplementary_hospital_data=[
            item
            for item in parsing._supplementary_hospital_data_from_document(document)
            if not _is_replaced_source_row(item, strict_records)
        ]
        + _strict_hospital_rows(strict_records)
        + [HospitalDataInput.model_validate(row) for row in source_context_records(document)],
    )


def extract_intrinsic_report_source(snapshots):
    """Registered ``report_source`` strategy; input contains file facts only."""
    if not snapshots:
        raise ValueError('Report extraction requires source snapshots')
    first = snapshots[0]
    current_inputs = all(getattr(item, 'manifest', {}).get('mode') == 'current' for item in snapshots)
    text = first.text or ''
    filename = first.file_name
    file_type = 'pdf' if first.file_type == 'pdf' else 'image'
    checksum = first.content_sha256
    if len(snapshots) > 1:
        if any(item.file_type == 'pdf' for item in snapshots):
            raise ValueError('Only continuous image pages may use combined Report extraction')
        group = [
            (index, parsing.DownloadedFile(item.file_name, '', b'', 'image'), item.text or '')
            for index, item in enumerate(snapshots)
        ]
        if not parsing._looks_like_continuous_image_report(group):
            raise ValueError('Images do not form a continuous source report')
        text = '\n'.join(f'\n===== IMAGE PAGE {i} =====\n{item.text or ""}' for i, item in enumerate(snapshots, 1))
        filename = ' / '.join(item.file_name for item in snapshots)
        checksum = None
    recover_tables = current_inputs and any(item.include_analysis_observation_refs for item in snapshots)
    if current_inputs:
        text = current_result_text(text, recover_tables=recover_tables)
    metadata = {}
    if match := parsing._ENTEROTYPE_TEXT.search(text):
        metadata['enterotype'] = parsing._normalize_enterotype(match.group(1))
    shared_document = parsing.ExtractedDocument(
        filename,
        file_type,
        text,
        tables=parsing._markdown_tables(text, page_number=None),
        metadata=metadata,
    )
    parsed = _document_facts(shared_document, checksum)
    local = None
    native_document = None
    strict_records = strict_report_source_records(shared_document)
    if len(snapshots) == 1 and file_type == 'pdf' and first.content:
        try:
            # Only recover native table geometry. OCR has already run in the
            # shared service; do not submit again or run a second local OCR.
            if recover_tables:
                native_document = parsing._extract_pdf(filename, first.content, ocr=False, prove_result_rows=True)
            else:
                native_document = parsing._extract_pdf(filename, first.content, ocr=False)
            if current_inputs:
                native_document = current_native_document(native_document, recover_tables=recover_tables)
            local = _document_facts(native_document, checksum)
            native_records = strict_report_source_records(native_document)
            if native_records:
                strict_records = native_records
        except (parsing.SourceParseError, ValueError):
            pass
    clinical_rows = []
    if local is not None:
        clinical_rows.extend(local.supplementary_hospital_data)
        if local.source_type != 'pending':
            parsing._merge_same_snapshot(local, parsed)
            local.subject = parsing._merge_subjects(local.subject, parsed.subject)
            parsed, shared_parsed = local, parsed
            clinical_rows.extend(shared_parsed.supplementary_hospital_data)
    clinical_rows.extend(parsed.supplementary_hospital_data)
    current_rows = []
    for snapshot in snapshots:
        current = current_inputs
        if current:
            report, occurrences, refs_by_locator = verified_current_report(snapshot, native_document)
        else:
            report = snapshot.report
        rows = _clinical_rows(
            report,
            snapshot.file_name,
            snapshot.text or '',
            accepted_observations=not current,
            current_source=snapshot if current else None,
        )
        shared_metrics, shared_taxa = _shared_microbiome_facts(report, snapshot.text or '')
        if current:
            consumed = _current_microbiome_occurrences(report, snapshot.text or '')
            current_rows.extend(original_clinical_rows(snapshot, occurrences, rows, consumed, refs_by_locator))
        else:
            clinical_rows.extend(rows)
        # Keep source-approved values/units (including an unknown unit), rather
        # than inventing '%' or '分' from a parser default. Conflicting mirrors
        # remain candidates for the existing uniqueness check in the Adapter.
        shared_codes = {item.code for item in shared_metrics}
        parsed.metrics = [item for item in parsed.metrics if item.code not in shared_codes]
        parsed.metrics.extend(
            parsing._dedupe(
                shared_metrics,
                key=lambda item: (
                    item.code,
                    item.value,
                    item.unit,
                    item.reference_range,
                    item.status,
                ),
            )
        )
        existing_taxa = {(item.latin_name, item.value) for item in parsed.taxa}
        for item in shared_taxa:
            if (item.latin_name, item.value) not in existing_taxa:
                parsed.taxa.append(item)
                existing_taxa.add((item.latin_name, item.value))
    # Explicit columns and same-page chart evidence are authoritative over
    # substring/global-scale guesses and accepted shared mirrors of the same
    # source item. This never changes the shared Analysis output profile.
    if strict_records:
        explicit_metrics = _strict_source_metrics(strict_records)
        explicit_codes = {item.code for item in explicit_metrics}
        parsed.metrics = [item for item in parsed.metrics if item.code not in explicit_codes] + explicit_metrics
        clinical_rows = [item for item in clinical_rows if not _is_replaced_source_row(item, strict_records)]
        clinical_rows.extend(_strict_hospital_rows(strict_records))
    # The intrinsic payload is a fact pool too: remove source-proven food,
    # definition, citation and shifted summary fragments before persisting it.
    # Prefer native geometry when available; OCR remains the fallback only.
    originals = [native_document] if native_document is not None else [shared_document]
    clinical_rows = clean_source_hospital_rows(clinical_rows, originals)
    if current_inputs:
        clinical_rows = [*current_rows, *remaining_source_context(clinical_rows)]
    if native_document is not None and local is not None and local.source_type != 'pending':
        parsed.taxa = clean_source_taxa(parsed.taxa, printed_table_taxa(native_document))
    # Keep different dates, values and reference intervals distinct.
    unique = {}
    for row in clinical_rows:
        extra = row.model_extra or {}
        if occurrence := extra.get('sourceOccurrence'):
            unique.setdefault((extra['sourceDocumentId'], occurrence), row)
            continue
        key = (
            row.item_name,
            str(row.test_result_value),
            row.exam_result,
            row.report_time,
            row.check_time,
            row.reference_range,
            row.result_status_name,
        )
        unique.setdefault(key, row)
    result = parsed.model_dump(mode='json', by_alias=True)
    # This is an internal shared-extractor payload, never a public report or
    # model input. ParsedMicrobiomeReport excludes the field everywhere else.
    result['evaluationPanelSummaries'] = [
        item.model_dump(mode='json', by_alias=True) for item in parsed.evaluation_panel_summaries
    ]
    result['evaluationAmrClasses'] = list(parsed.evaluation_amr_classes)
    result['sourceTaxonOccurrences'] = list(parsed.source_taxon_occurrences)
    result['supplementaryHospitalData'] = [row.model_dump(mode='json', by_alias=True) for row in unique.values()]
    return result
