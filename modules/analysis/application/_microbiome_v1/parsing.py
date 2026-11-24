"""Document-owned v1 leaf algorithms frozen from efca880 Report parsing.

Only the native-source dependency closure is retained. Import bindings point to
this fixed package; future Report changes must not alter this implementation.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from typing import Any

import pymupdf

from modules.analysis.application._microbiome_v1.schemas import ParsedDisease
from modules.analysis.application._microbiome_v1.schemas import ParsedTaxon
from modules.analysis.application._microbiome_v1.schemas import SourceMetric
from modules.analysis.application._microbiome_v1.schemas import stable_id
from utils.log import getLogger

logger = getLogger(__name__)


class SourceParseError(RuntimeError):
    pass


@dataclass
class ExtractedTable:
    page_number: int
    rows: list[list[str]]
    context: str = ''
    observed_at: str | None = None
    section_title: str | None = None
    native_clinical_columns: bool = False


@dataclass
class ExtractedDocument:
    filename: str
    source_type: str
    text: str
    tables: list[ExtractedTable] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    native_metrics: list[SourceMetric] = field(default_factory=list)


_PAGE_MARKER_RE = re.compile(r'^===== PAGE (?P<page>\d+) =====$', re.MULTILINE)


_RESULT_VALUE_RE = re.compile(r'^(?:[<>≤≥]\s*)?-?\d+(?:\.\d+)?(?:\s*[Ee][+-]?\d+)?%?$')


_REFERENCE_VALUE_RE = re.compile(
    r'^(?:-|阴性|未检出|(?:[<>≤≥]\s*)?-?\d+(?:\.\d+)?'
    r'(?:\s*[-~～至–—]\s*(?:[<>≤≥]\s*)?-?\d+(?:\.\d+)?)?)$'
)


_CATEGORICAL_RESULT_RE = re.compile(r'^(?:阴性|阳性|弱阳性|未检出|检出|正常|异常|偏高|偏低|升高|降低)$')


def _document_pages_with_numbers(text: str) -> list[tuple[int | None, str]]:
    matches = list(_PAGE_MARKER_RE.finditer(text))
    if not matches:
        return [(None, text)]
    pages: list[tuple[int | None, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        pages.append((int(match.group('page')), text[match.end() : end]))
    return pages


def _clean_document_line(value: str) -> str:
    return ' '.join(value.replace('\u3000', ' ').split()).strip(' |\t')


def _looks_like_item_name(value: str) -> bool:
    compact = _clean_document_line(value)
    if not 1 < len(compact) <= 100 or _RESULT_VALUE_RE.fullmatch(compact):
        return False
    if _REFERENCE_VALUE_RE.fullmatch(compact) or _CATEGORICAL_RESULT_RE.fullmatch(compact):
        return False
    if compact.startswith('=====') or re.fullmatch(r'\d+', compact):
        return False
    return bool(re.search(r'[A-Za-z\u3400-\u9fff]', compact))


def _clinical_table_role_groups(
    rows: list[list[str]],
) -> tuple[int | None, list[dict[str, int]]]:
    for row_index, row in enumerate(rows[:3]):
        groups: list[dict[str, int]] = []
        roles: dict[str, int] = {}
        for column, raw in enumerate(row):
            cell = re.sub(r'\s+', '', _clean(raw) or '').casefold()
            if not cell:
                continue
            if any(token in cell for token in ('项目', '名称', '检验项', '检查项')):
                if 'name' in roles and ('value' in roles or 'conclusion' in roles):
                    groups.append(roles)
                    roles = {}
                roles['name'] = column
            elif any(token in cell for token in ('检查结论', '体检结论', '异常结果', '结论')):
                roles.setdefault('conclusion', column)
            elif any(token in cell for token in ('参考范围', '参考值', '正常范围')):
                roles.setdefault('reference', column)
            elif '单位' in cell:
                roles.setdefault('unit', column)
            elif any(token in cell for token in ('状态', '提示', '异常标志', '结果标志')):
                roles.setdefault('status', column)
            elif any(token in cell for token in ('结果', '测定值', '检测值', '数值')):
                roles.setdefault('value', column)
        if ('name' in roles and 'value' in roles) or 'conclusion' in roles:
            groups.append(roles)
        valid = [group for group in groups if ('name' in group and 'value' in group) or 'conclusion' in group]
        if valid:
            return row_index, valid
    return None, []


def _native_clinical_tables(pages: list[tuple[int, dict]]) -> list[ExtractedTable]:
    """Read positioned cells under explicit clinical headers, with section dates.

    A footer date belongs to the active examination, including its continuation
    pages. A new numbered examination closes that scope; its absent date never
    inherits an earlier examination's footer or the document's general date.
    """
    tables: list[ExtractedTable] = []
    section_tables: list[ExtractedTable] = []
    columns: list[tuple[float, str]] = []
    section_title: str | None = None
    section_date: str | None = None
    active: ExtractedTable | None = None
    date_pattern = re.compile(
        r'检查时间\s*[:：]?\s*(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?)'
    )
    identity = re.compile(r'^(?:姓名|性别|年龄|编号|电话|体检号|身份证|病历|受检者|检查者|报告人|出生日期)')
    for page_number, native in pages:
        spans = [
            span
            for block in native.get('blocks', [])
            for line in block.get('lines', [])
            for span in line.get('spans', [])
            if str(span.get('text', '')).strip() and len(span.get('bbox', ())) == 4
        ]
        lines: list[list[dict]] = []

        # Font ascenders differ between Chinese labels and Latin values.
        # Their PDF baselines, rather than glyph-box tops, identify a row.
        def baseline(span):
            return span.get('origin', (0, span['bbox'][1]))[1]

        for span in sorted(spans, key=lambda s: (round(baseline(s), 1), s['bbox'][0])):
            if not lines or abs(baseline(span) - baseline(lines[-1][0])) > 3:
                lines.append([span])
            else:
                lines[-1].append(span)
        active = None
        for line in lines:
            line.sort(key=lambda s: s['bbox'][0])
            texts = [_clean_document_line(s['text']) for s in line]
            text = ' '.join(texts)
            if re.match(r'^\d{1,3}[.、．]\s*[^\d\s]', text):
                section_title = text
                section_date = None
                section_tables = []
                columns = []
                active = None
                continue
            date_match = date_pattern.search(text)
            if date_match:
                if section_tables:
                    for table in section_tables:
                        table.observed_at = date_match.group(1)
                    section_tables = []
                    section_date = None
                    section_title = None
                    columns = []
                    active = None
                elif section_title:
                    # A leading date is usable only inside an explicit new
                    # examination, never as a previous section's loose footer.
                    section_date = date_match.group(1)
                continue
            _, role_groups = _clinical_table_role_groups([texts])
            clinical_header = len(role_groups) == 1 and {'name', 'value', 'reference'} <= role_groups[0].keys()
            if clinical_header and any(re.fullmatch(r'(?:检验|检查)?项目(?:名称)?|项目名称', cell) for cell in texts):
                columns = [(span['bbox'][0], cell) for span, cell in zip(line, texts)]
                active = ExtractedTable(page_number, [texts], section_title or '', section_date, section_title, True)
                tables.append(active)
                section_tables.append(active)
                continue
            if not columns:
                continue
            edges = [(columns[i][0] + columns[i + 1][0]) / 2 for i in range(len(columns) - 1)]
            cells = [''] * len(columns)
            for span in line:
                column = sum(span['bbox'][0] >= edge for edge in edges)
                cells[column] += span['text']
            cells = [_clean_document_line(cell) for cell in cells]
            _, groups = _clinical_table_role_groups([[label for _, label in columns]])
            roles = groups[0]
            name, value = _cell(cells, roles['name']), _cell(cells, roles['value'])
            named_cell = _looks_like_item_name(name) or bool(re.fullmatch(r'[\u3400-\u9fff]', name))
            if not named_cell or not value or identity.search(name):
                continue
            if active is None:
                active = ExtractedTable(
                    page_number,
                    [[label for _, label in columns]],
                    section_title or '',
                    section_date,
                    section_title,
                    True,
                )
                tables.append(active)
                section_tables.append(active)
            active.rows.append(cells)
    return [table for table in tables if len(table.rows) > 1]


def original_amr_table_rows(document: ExtractedDocument) -> list[dict]:
    """Read resistance metadata only under an explicit native AMR table header.

    Merged class cells establish the meaning of short labels such as A/B;
    isolated letters or enzyme names elsewhere do not establish resistance.
    Flags remain the literal printed labels, without clinical interpretation.
    """
    rows = []
    header_roles = {
        'class': {
            '抗生素耐药风险评估',
            '抗生素耐药性评估',
            '抗菌药物耐药风险评估',
            'antibioticresistanceriskassessment',
            'antimicrobialresistanceriskassessment',
        },
        'name': {'检测项目', '项目名称', '指标名称', 'item', 'testitem'},
        'value': {'评估值', '检测结果', '数值', 'value', 'result'},
        'status': {'结果评价', '结果评估', '评价结果', 'status', 'assessment'},
        'unit': {'单位', 'unit'},
    }
    for table in document.tables:
        # A compact antibiotic-class panel has explicit bilingual class cells;
        # its neighbouring "abnormal items: not detected" is not a zero result.
        header = [_compact_metric_label(c) for c in table.rows[0]] if table.rows else []
        if (
            not table.native_clinical_columns
            and re.search(r'抗生素耐药|抗菌药物耐药|antibiotic.*resistance', table.context or '', re.I)
            and {'拉丁学名', '中文名', '样本含量', '参考范围', '结果'} <= set(header)
        ):
            indexes = {name: header.index(name) for name in ('中文名', '样本含量', '参考范围', '结果')}
            for cells in table.rows[1:]:
                name, value = (_cell(cells, indexes[k]) for k in ('中文名', '样本含量'))
                if name and re.fullmatch(r'[<>≤≥]?\s*\d+(?:\.\d+)?', value):
                    rows.append(
                        {
                            'itemName': name,
                            'value': value,
                            'unit': None,
                            'referenceRange': _cell(cells, indexes['参考范围']) or None,
                            'status': _cell(cells, indexes['结果']) or None,
                            'antibioticClass': name,
                            'sourceCategory': 'antimicrobialResistance',
                        }
                    )
            continue
        category = None
        roles = {}
        for cells in table.rows:
            detected = {}
            for index, cell in enumerate(cells):
                label = re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(cell or ''))).casefold()
                for role, labels in header_roles.items():
                    if label in labels:
                        detected[role] = index
            if {'class', 'name', 'value', 'status'} <= detected.keys():
                roles, category = detected, None
                continue
            if not roles:
                continue
            category = _clean(_cell(cells, roles['class'])) or category
            name, value = _clean(_cell(cells, roles['name'])), _clean(_cell(cells, roles['value']))
            if not category or not name or not value or not _RESULT_VALUE_RE.fullmatch(value):
                continue
            rows.append(
                {
                    'itemName': name,
                    'value': value,
                    'unit': _clean(_cell(cells, roles['unit'])) if 'unit' in roles else None,
                    'status': _clean(_cell(cells, roles['status'])),
                    'antibioticClass': category,
                    'sourceCategory': 'antimicrobialResistance',
                }
            )
    return rows


def _native_source_metrics(pages: list[tuple[int, dict]]) -> list[SourceMetric]:
    """Read explicit source indices and score labels without using chart shapes.

    Ratio tables and labels ending in a printed score unit are separate source
    observations. No range, status or independent medical/nutrition score is
    calculated here. Conflicting native values remain unresolved.
    """
    candidates: list[SourceMetric] = []
    score_labels = {
        'microbiome_health_score': ('肠道菌群健康度评分', '肠道菌群健康评分'),
        'be_score': ('肠道菌群平衡状态评估', 'B/E评分', 'B/E比值评分'),
        'fb_score': ('肠道菌群热量代谢评估', 'F/B评分', 'F/B比值评分'),
        'anti_inflammatory_capacity': ('菌群抗炎能力评估', '肠道抗炎能力评估', '肠道菌群抗炎能力评估'),
        'gut_immunity': ('肠道免疫力评估',),
    }
    header_roles = {
        'name': {'检测项目', '检验项目', '项目名称', '指标名称', '短链脂肪酸'},
        'value': {'评估值', '检测值', '检测结果', '结果', '数值'},
        'status': {'结果评估', '结果评价', '肠道健康状态', '合成能力评估'},
        'reference': {'参考范围', '参考值', '阈值'},
        'unit': {'单位'},
    }

    def baseline(span):
        return span.get('origin', (0, span['bbox'][1]))[1]

    for _, native in pages:
        spans = [
            s
            for b in native.get('blocks', [])
            for line in b.get('lines', [])
            for s in line.get('spans', [])
            if str(s.get('text', '')).strip() and len(s.get('bbox', ())) == 4
        ]
        lines: list[list[dict]] = []
        for span in sorted(spans, key=lambda s: (baseline(s), s['bbox'][0])):
            if not lines or abs(baseline(span) - baseline(lines[-1][0])) > 3:
                lines.append([span])
            else:
                lines[-1].append(span)
        for line in lines:
            line.sort(key=lambda s: s['bbox'][0])
        for label in spans:
            raw_label = label['text'].strip()
            name = re.sub(r'\s+分\s*$', '', raw_label).strip()
            code = next(
                (
                    code
                    for code, aliases in score_labels.items()
                    if _compact_metric_label(name) in {_compact_metric_label(alias) for alias in aliases}
                ),
                None,
            )
            if code is None:
                continue
            numbers = [
                s
                for s in spans
                if _RESULT_VALUE_RE.fullmatch(s['text'].strip())
                and abs(baseline(s) - baseline(label)) <= 4
                and label['bbox'][0] <= s['bbox'][0] <= label['bbox'][2] + 60
            ]
            if len(numbers) != 1:
                continue
            number = numbers[0]
            printed_unit = name != raw_label or any(
                s['text'].strip() == '分'
                and abs(baseline(s) - baseline(number)) <= 4
                and s['bbox'][0] <= number['bbox'][2] + 35
                and number['bbox'][2] <= s['bbox'][2] <= number['bbox'][2] + 60
                for s in spans
            )
            if printed_unit:
                candidates.append(SourceMetric(code=code, name=name, value=number['text'].strip(), unit='分'))
        for line_index, header in enumerate(lines):
            roles = {}
            for index, span in enumerate(header):
                cell = _compact_metric_label(span['text'])
                for role, aliases in header_roles.items():
                    if cell in aliases:
                        roles.setdefault(role, index)
            if not {'name', 'value'} <= roles.keys() or not ({'status', 'reference'} & roles.keys()):
                continue
            synthesis_table = any(_compact_metric_label(s['text']) == '短链脂肪酸' for s in header) and any(
                _compact_metric_label(s['text']) == '合成能力评估' for s in header
            )
            header_y = baseline(header[0])
            heading = ' '.join(s['text'] for s in spans if baseline(s) < header_y)
            stop_y = min(
                (
                    baseline(next_line[0])
                    for next_line in lines[line_index + 1 :]
                    if any(re.search(r'检测结果说明|参考文献', s['text']) for s in next_line)
                ),
                default=float('inf'),
            )
            edges = [(header[i]['bbox'][0] + header[i + 1]['bbox'][0]) / 2 for i in range(len(header) - 1)]
            left_edge = header[0]['bbox'][0] - (header[1]['bbox'][0] - header[0]['bbox'][0]) / 2
            right_edge = header[-1]['bbox'][2] + (header[-1]['bbox'][0] - header[-2]['bbox'][0]) / 2

            def column(span):
                return sum(span['bbox'][0] >= edge for edge in edges)

            anchors = sorted(
                [
                    s
                    for s in spans
                    if column(s) == roles['value']
                    and left_edge <= s['bbox'][0] < right_edge
                    and header_y + 4 < baseline(s) < stop_y
                    and _RESULT_VALUE_RE.fullmatch(s['text'].strip())
                ],
                key=baseline,
            )
            for index, anchor in enumerate(anchors):
                y = baseline(anchor)
                low = max(header_y + 4, y - 30, (baseline(anchors[index - 1]) + y) / 2 if index else y - 30)
                high = min(y + 25, (y + baseline(anchors[index + 1])) / 2 if index + 1 < len(anchors) else y + 25)
                cells = [''] * len(header)
                for span in sorted(spans, key=lambda s: (baseline(s), s['bbox'][0])):
                    if left_edge <= span['bbox'][0] < right_edge and low < baseline(span) < high:
                        cells[column(span)] += span['text']
                cells = [_clean_document_line(cell) for cell in cells]
                name = _cell(cells, roles['name'])
                compact = _compact_metric_label(name)
                if compact in {'肠道微生物健康指数', '肠道菌群健康指数'} and re.search(r'GMWI\s*2', heading, re.I):
                    code, name = 'gmwi2', '肠道菌群健康指数（GMWI2）'
                elif compact in {'肠道菌群种类评估', '肠道微生物种类评估', '肠道微生物种类', '菌种数'}:
                    code = 'species_count'
                elif compact in {'肠道菌群多样性评估', '菌群多样性评估', '菌群多样性', '肠道微生物多样性'}:
                    code = 'diversity'
                elif compact in {'肠道菌群致病潜力', '肠道菌群致病潜力评估'}:
                    code = 'pathogenic_potential'
                elif compact in {'肠道膳食纤维需求', '肠道膳食纤维需求评估'}:
                    code = 'dietary_fiber_need'
                elif compact in {'肠道菌群生物膜合成能力', '肠道菌群生物膜合成能力评估'}:
                    code = 'biofilm_capacity'
                elif compact in {'肠道菌群氧化胁迫耐受', '肠道菌群氧化胁迫耐受评估'}:
                    code = 'oxidative_stress_tolerance'
                elif compact in {'肠道免疫力', '肠道免疫力评估'}:
                    code = 'gut_immunity'
                elif compact in {'肠道菌群抗炎能力', '肠道菌群抗炎能力评估', '菌群抗炎能力评估'}:
                    code = 'anti_inflammatory_capacity'
                elif synthesis_table and compact in {'甲酸', '乙酸', '丙酸', '丁酸', '戊酸'}:
                    code = {
                        '甲酸': 'formate',
                        '乙酸': 'acetate',
                        '丙酸': 'propionate',
                        '丁酸': 'butyrate',
                        '戊酸': 'valerate',
                    }[compact]
                elif compact in {'be比值', '肠道菌群平衡状态', '肠道菌群平衡状态评估'} and re.search(
                    r'B\s*/\s*E', heading + ' ' + name, re.I
                ):
                    code = 'be_ratio'
                elif compact in {'fb比值', '肠道菌群热量代谢fb比值', '肠道菌群热量代谢'} and re.search(
                    r'F\s*/\s*B', heading + ' ' + name, re.I
                ):
                    code = 'fb_ratio'
                else:
                    continue
                unit = _clean(_cell(cells, roles['unit'])) if 'unit' in roles else None
                if code in {'be_ratio', 'fb_ratio'} and (
                    unit == '分' or re.search(r'(?:B\s*/\s*E|F\s*/\s*B).{0,12}(?:评分|得分)', heading, re.I)
                ):
                    code = 'be_score' if code == 'be_ratio' else 'fb_score'
                    name = ('B/E' if code == 'be_score' else 'F/B') + '比值评分'
                status = _clean(_cell(cells, roles['status'])) if 'status' in roles else None
                if (
                    code in {'formate', 'acetate', 'propionate', 'butyrate', 'valerate'}
                    and status
                    and not re.fullmatch(r'(?:合成能力)?(?:强|较强|正常|一般|较弱|弱)', status)
                ):
                    status = None  # Multiple chart legend levels are not a printed patient direction.
                candidates.append(
                    SourceMetric(
                        code=code,
                        name=name,
                        value=anchor['text'].strip(),
                        unit=unit,
                        status=status,
                        reference_range=_clean(_cell(cells, roles['reference'])) if 'reference' in roles else None,
                    )
                )
    result = []
    for code in dict.fromkeys(item.code for item in candidates):
        group = [item for item in candidates if item.code == code]
        fields = {
            key: {
                re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)): value
                for item in reversed(group)
                if (value := _clean(getattr(item, key))) is not None
            }
            for key in ('value', 'unit', 'status', 'reference_range')
        }
        if any(len(values) > 1 for values in fields.values()):
            logger.warning('Report native metric conflict code=%s candidates=%s', code, len(group))
            continue
        result.append(
            SourceMetric(
                code=code,
                name=group[0].name,
                **{key: next(iter(values.values())) if values else None for key, values in fields.items()},
            )
        )
    return result


def _extract_pdf(filename: str, content: bytes, *, ocr: bool = True) -> ExtractedDocument:
    text_parts: list[str] = []
    tables: list[ExtractedTable] = []
    metadata: dict[str, str] = {'content_sha256': hashlib.sha256(content).hexdigest()}
    ocr_pages: list[int] = []
    ocr_failures: list[int] = []
    actual_text_characters = 0
    native_pages: list[tuple[int, dict]] = []
    try:
        document = pymupdf.open(stream=content, filetype='pdf')
    except Exception as exc:
        raise SourceParseError('PDF cannot be opened') from exc
    with document:
        for page_index, page in enumerate(document, start=1):
            try:
                native = page.get_text('dict')
                if isinstance(native, dict):
                    native_pages.append((page_index, native))
            except Exception:  # Native geometry is optional for scans.
                pass
            native_text = page.get_text('text') or ''
            page_text = native_text
            if ocr and _pdf_page_needs_ocr(page, native_text):
                try:
                    text_page = page.get_textpage_ocr(
                        language='chi_sim+eng',
                        dpi=300,
                        full=True,
                    )
                    ocr_text = page.get_text('text', textpage=text_page) or ''
                    if _meaningful_text_length(ocr_text) > _meaningful_text_length(native_text):
                        page_text = ocr_text
                        ocr_pages.append(page_index)
                except Exception:  # noqa: BLE001 - DocMind remains the fallback.
                    ocr_failures.append(page_index)
            actual_text_characters += _meaningful_text_length(page_text)
            text_parts.append(f'\n===== PAGE {page_index} =====\n{page_text}')
            if enterotype := _detect_enterotype(page, page_text):
                metadata['enterotype'] = enterotype
            try:
                found = page.find_tables().tables
            except Exception:  # noqa: BLE001 - table detection is optional per page.
                found = []
            blocks = [
                (float(row[1]), float(row[3]), _clean(row[4]) or '') for row in page.get_text('blocks') if len(row) >= 5
            ]
            for table in found:
                rows = _clean_table(table.extract())
                if not rows:
                    continue
                for index, table_row in enumerate(table.rows):
                    if index >= len(rows):
                        break
                    status = _visual_table_row_status(page, table_row.bbox)
                    if not status:
                        continue
                    if len(rows[index]) >= 4 and not rows[index][-1]:
                        rows[index][-1] = status
                    elif status not in rows[index]:
                        rows[index].append(status)
                top = float(table.bbox[1])
                context = '\n'.join(text for _y0, y1, text in blocks if y1 <= top + 2)[-4000:]
                tables.append(ExtractedTable(page_index, rows, context))
    tables.extend(_native_clinical_tables(native_pages))
    text = ''.join(text_parts)
    if actual_text_characters == 0:
        raise SourceParseError('PDF contains no extractable text')
    if ocr_pages:
        metadata['ocr_pages'] = ','.join(str(item) for item in ocr_pages)
    if ocr_failures:
        metadata['ocr_failed_pages'] = ','.join(str(item) for item in ocr_failures)
    return ExtractedDocument(filename, 'pdf', text, tables, metadata, _native_source_metrics(native_pages))


def _meaningful_text_length(text: str) -> int:
    """Count visible source content and ignore synthetic page separators."""

    return len(re.findall(r'[A-Za-z0-9\u3400-\u9fff]', text or ''))


def _pdf_page_needs_ocr(page: Any, native_text: str) -> bool:
    """Choose OCR per page so native and scanned pages can coexist."""

    visible = _meaningful_text_length(native_text)
    if visible < 24:
        return True
    if visible >= 180:
        return False
    try:
        page_area = max(1.0, float(page.rect.width) * float(page.rect.height))
        covered = 0.0
        for image in page.get_images(full=True):
            for rect in page.get_image_rects(int(image[0])):
                covered += max(0.0, float(rect.width) * float(rect.height))
        return covered / page_area >= 0.45
    except Exception:  # noqa: BLE001 - image geometry is only an OCR hint.
        return False


def _compact_metric_label(value: str | None) -> str:
    return re.sub(r'[\s_()（）/\\:：\-]+', '', str(value or '')).casefold()


def _document_taxa(document: ExtractedDocument) -> list[ParsedTaxon]:
    results: list[ParsedTaxon] = []
    known_genera = {
        latin.split()[0]
        for table in document.tables
        for row in table.rows
        if row
        for _chinese, latin in [_split_taxon_name(row[0])]
        if latin and ' ' in latin
    }
    active_category: str | None = None
    active_taxon_layout = False
    pending_split_row: tuple[str, str, str | None, str] | None = None
    for table in document.tables:
        header_text = ' '.join(table.rows[0]) if table.rows else ''
        if ('相关性' in header_text and '参考范围' in header_text) or (
            '拉丁学名' in header_text and '人群检出率' in header_text
        ):
            active_category = None
        # Only preceding page text may switch a continuing table's section.
        # Row descriptions frequently contain words such as "致病菌" and must
        # not reclassify beneficial species.
        context_category = _category_from_text(table.context)
        if context_category:
            active_category = context_category
            active_taxon_layout = True
        elif _taxon_section_ended(table.context):
            active_category = None
            active_taxon_layout = False
        is_combined_panel_header = '菌名' in header_text and ('检测结果' in header_text or '丰度' in header_text)
        header = table.rows[0] if table.rows else []
        indexes = _microbiome_panel_indexes(header)
        if is_combined_panel_header or indexes is not None:
            active_taxon_layout = True
        elif not active_taxon_layout:
            continue
        for row in table.rows:
            joined = ' '.join(row)
            if category := _category_from_aggregate_row(row):
                active_category = category
                continue
            if '菌名' in joined and ('检测结果' in joined or '丰度' in joined):
                continue
            if active_category is None or len(row) < 2:
                continue

            # Metagenomic reports use a regular four/five-column panel table.
            # Parse it by header semantics instead of treating the first cell as
            # a combined Chinese/Latin name (the 16S layout below does that).
            if indexes is not None:
                if row is header or _is_microbiome_panel_header(row):
                    continue
                panel_row = _microbiome_panel_row(
                    row,
                    indexes,
                    known_genera=known_genera,
                )
                if panel_row is None:
                    continue
                chinese, latin, value, reference_range, status = panel_row
                results.append(
                    ParsedTaxon(
                        taxon_id=stable_id('taxon', latin),
                        category=active_category,
                        latin_name=latin,
                        chinese_name=chinese,
                        value=value,
                        reference_range=reference_range,
                        source_status=status,
                        explicitly_abnormal=(_is_explicit_abnormal(status, False)),
                        detection_status=detection_status(value, status=status),
                    )
                )
                continue

            name_cell = row[0]
            value = _taxon_value(row)
            if not name_cell:
                continue
            chinese, latin = _split_taxon_name(
                name_cell,
                known_genera=known_genera,
            )
            status = _taxon_status(row)
            if latin and value is None and pending_split_row and not chinese:
                pending_name, pending_value, pending_status, pending_category = pending_split_row
                chinese = pending_name
                value = pending_value
                status = status or pending_status
                active_category = pending_category
                pending_split_row = None
            if not latin and value is not None and status is not None and re.search(r'[\u4e00-\u9fff]', name_cell):
                pending_split_row = (name_cell, value, status, active_category)
                continue
            if value is None:
                continue
            if not latin:
                continue
            results.append(
                ParsedTaxon(
                    taxon_id=stable_id('taxon', latin),
                    category=active_category,
                    latin_name=latin,
                    chinese_name=_normalize_source_chinese_name(chinese),
                    value=value,
                    source_status=status,
                    explicitly_abnormal=_is_explicit_abnormal(status, False),
                    detection_status=detection_status(value, status=status),
                )
            )
    return _merge_document_taxa(results)


def _document_diseases(document: ExtractedDocument) -> list[ParsedDisease]:
    results: list[ParsedDisease] = []

    # Summary tables are the most reliable source when available.  They also
    # preserve related-taxon labels without trying to interpret them.
    for table in document.tables:
        if not table.rows:
            continue
        header = table.rows[0]
        score_index = _header_index_any(
            header,
            '风险指数',
            '风险评分',
            '模型指数',
            '模型分值',
            '风险值',
        )
        level_index = _header_index_any(
            header,
            '风险等级',
            '风险评估',
            '风险级别',
            '检测结果',
        )
        # ``名称`` is accepted only after the row has been proven to be a
        # risk table, so arbitrary clinical tables cannot enter this parser.
        disease_index = (
            _header_index_any(
                header,
                '疾病名称',
                '疾病类型',
                '疾病或健康问题',
                '健康问题',
                '风险名称',
                '名称',
            )
            if score_index is not None and level_index is not None
            else None
        )
        related_index = _header_index_any(
            header,
            '正负相关菌',
            '相关菌群',
            '关联菌群',
            '相关菌',
        )
        data_rows = table.rows[1:]
        if (
            (disease_index is None or score_index is None or level_index is None)
            and _disease_risk_context_active(table.context)
            and len(header) >= 3
            and _looks_like_disease_or_health_problem(_cell(header, 0))
            and _source_risk_score(_cell(header, 1))
            and _source_risk_level(_cell(header, 2))
        ):
            # OCR can interrupt one Markdown table with a page header. The
            # continuation then starts directly with a data row. Reuse the
            # semantic three-column layout only while the surrounding context
            # remains inside the same disease-risk section.
            disease_index, score_index, level_index = 0, 1, 2
            related_index = 3 if len(header) > 3 else None
            data_rows = table.rows
        if disease_index is not None and score_index is not None and level_index is not None:
            for row in data_rows:
                name = _normalize_source_disease_name(_cell(row, disease_index))
                score = _source_risk_score(_cell(row, score_index))
                level = _source_risk_level(_cell(row, level_index))
                if not name or not score or not level:
                    continue
                results.append(
                    ParsedDisease(
                        disease_id=stable_id('disease', name),
                        name=name,
                        source_score=score,
                        source_level=level,
                        related_taxa=_split_source_related_taxa(_cell(row, related_index)),
                    )
                )
            continue

        # Some provider PDFs ship an unresolved export template: the disease
        # name is present, while score and level cells contain literal image
        # placeholders such as ``<constipation_png>`` and ``<constipation>``.
        # Those rows are still authoritative source items.  Preserve their
        # names one-for-one, but never infer a score or risk level from the
        # placeholder, disease prose, abnormal taxa or external evidence.
        if len(header) >= 3:
            for row in table.rows:
                name = _normalize_source_disease_name(_cell(row, 0))
                score_placeholder = _cell(row, 1)
                level_placeholder = _cell(row, 2)
                if (
                    not name
                    or not _looks_like_disease_or_health_problem(name)
                    or not _is_source_image_placeholder(score_placeholder)
                    or not _is_source_image_placeholder(level_placeholder)
                ):
                    continue
                results.append(
                    ParsedDisease(
                        disease_id=stable_id('disease', name),
                        name=name,
                    )
                )

    # Full-page disease cards contain the complete source list, including
    # clear zero-valued model results omitted from the overview table.  The
    # repeated title is required so explanatory prose cannot be mistaken for
    # a patient result.
    for page_text in _document_pages(document.text):
        results.extend(_disease_similarity_results(page_text))
        match = re.search(
            r'^\s*(?P<name>[^\n]{2,60})\s*\n'
            r'\s*疾病风险评估\s*\n'
            r'\s*(?P=name)\s*\n'
            r'\s*风险为\s*[:：]?\s*\n?'
            r'\s*(?P<score>\d{1,3}(?:\.\d+)?)\s*\n'
            r'\s*(?P<level>低风险|中低风险|中风险|中高风险|高风险)',
            page_text,
        )
        if not match:
            continue
        name = _normalize_source_disease_name(match.group('name'))
        if not name:
            continue
        results.append(
            ParsedDisease(
                disease_id=stable_id('disease', name),
                name=name,
                source_score=match.group('score'),
                source_level=match.group('level'),
            )
        )

    for block in _disease_risk_text_blocks(document.text):
        for score, name, level in re.findall(
            r'(?m)^[ \t]*(\d+(?:\.\d+)?)[ \t]*[]\[()DIl<>-]*[ \t]*'
            r'([Ⅰ-ⅿ2㐀-鿿]{2,30}?)[ \t]*'
            r'(低风险|中低风险|中风险|中高风险|高风险)[ \t]*$',
            block,
        ):
            cleaned_name = _normalize_source_disease_name(name)
            if not cleaned_name or not _looks_like_disease_or_health_problem(cleaned_name):
                continue
            results.append(
                ParsedDisease(
                    disease_id=stable_id('disease', cleaned_name),
                    name=cleaned_name,
                    source_score=score,
                    source_level=level,
                )
            )
    # Prefer the overview row when it carries related taxa, otherwise retain
    # the detailed-card row.  Merge without changing source names or scores.
    merged: dict[str, ParsedDisease] = {}
    for item in results:
        existing = merged.get(item.disease_id)
        if existing is None:
            merged[item.disease_id] = item
            continue
        if not existing.related_taxa and item.related_taxa:
            existing.related_taxa = item.related_taxa
        if existing.source_score is None:
            existing.source_score = item.source_score
        if existing.source_level is None:
            existing.source_level = item.source_level
    descriptions = _document_disease_descriptions(
        document.text,
        disease_names=(item.name for item in merged.values()),
    )
    for item in merged.values():
        item.source_description = item.source_description or descriptions.get(item.name.casefold())
    return list(merged.values())


def _disease_similarity_results(page_text: str) -> list[ParsedDisease]:
    """Read generic source disease/symptom score triplets from one page.

    The parser is anchored to a source section heading and understands the
    semantic score/level layout. It does not contain a disease catalogue.
    """

    if not re.search(r'疾病(?:相似|风险)评估', page_text):
        return []
    lines = [_normalized_source_text(line) for line in page_text.splitlines() if _normalized_source_text(line)]
    results: list[ParsedDisease] = []
    for index in range(len(lines) - 2):
        name, raw_score, raw_level = lines[index : index + 3]
        if name.strip('：: ') in {
            '指标范围',
            '疾病相似评估',
            '疾病风险评估',
            '风险为',
            '风险指数',
            '风险评分',
            '风险等级',
        }:
            continue
        # A pipe row already has a complete table meaning. Treating it as a
        # name and searching the next rows' numbers shifts every patient result.
        if (
            re.search(r'[|｜。；;，,:：]|低风险|中风险|高风险|如果|根据|检测|您|建议', name)
            or not re.fullmatch(r'-?\d+(?:\.\d+)?%?', raw_score)
            or not re.fullmatch(r'低风险|中低风险|中风险|中高风险|高风险|低相似|中相似|高相似|注意|需关注', raw_level)
        ):
            continue
        score = _source_risk_score(raw_score)
        level = _source_risk_level(raw_level)
        normalized_name = _normalize_source_disease_name(name)
        if not score or not level or not normalized_name or not _looks_like_disease_or_health_problem(normalized_name):
            continue
        results.append(
            ParsedDisease(
                disease_id=stable_id('disease', normalized_name),
                name=normalized_name,
                source_score=score,
                source_level=level,
            )
        )
    return results


def _document_disease_descriptions(
    text: str,
    *,
    disease_names: Iterable[str],
) -> dict[str, str]:
    """Extract provider-authored disease-card prose without inventing facts.

    Some PDF overview tables contain image placeholders instead of readable
    scores.  Their later disease cards still contain a general description.
    Preserve at most the first two sentences as source provenance so the row
    is not visually blank, while leaving scores, levels and independent
    evidence untouched.
    """

    descriptions: dict[str, str] = {}
    for name in dict.fromkeys(disease_names):
        heading = re.search(
            rf'\{{\s*{re.escape(name)}\s*\}}\s*',
            text,
        )
        if heading is None:
            continue
        tail = text[heading.end() :]
        next_heading = re.search(r'\n\s*\{[^{}\n]{2,60}\}\s*\n', tail)
        block = tail[: next_heading.start()] if next_heading else tail
        block = re.sub(
            r'(?im)^\s*(?:16S\s*)?INTESTINAL MICROBIAL GENE TEST REPORT\s*$|'
            r'^\s*16S\s*肠道微生物基因检测报告\s*$|'
            r'^\s*=+\s*PAGE\s+\d+\s*=+\s*$',
            ' ',
            block,
        )
        cleaned = _clean(block)
        if not cleaned:
            continue
        # PDF line wrapping inserts spaces inside Chinese sentences.  Remove
        # only CJK-adjacent wrapping spaces; preserve ordinary English words.
        cleaned = re.sub(
            r'(?<=[\u3400-\u9fff，。；：、（）])\s+'
            r'(?=[\u3400-\u9fff，。；：、（）])',
            '',
            cleaned,
        )
        sentences = [
            sentence.strip() for sentence in re.findall(r'[^。！？!?]+[。！？!?]?', cleaned) if sentence.strip()
        ]
        description = ''.join(sentences[:2]).strip()
        if description and _source_description_names_disease(description, name):
            descriptions[name.casefold()] = description[:600]
    return descriptions


def _source_description_names_disease(description: str, disease_name: str) -> bool:
    """Prevent an adjacent table heading from becoming another disease's prose."""

    aliases = {
        'II型糖尿病': ('糖尿病', '2型糖尿病', 'Ⅱ型糖尿病'),
        '肾脏疾病': ('肾病', '肾炎', '肾脏'),
        '非酒精性脂肪肝': ('非酒精性脂肪性肝病', 'NAFLD'),
        '帕金森': ('帕金森病',),
        '酒精肝': ('酒精性肝病',),
    }.get(disease_name, ())
    target_terms = (disease_name, *aliases)
    opening = description[:240].casefold()
    return any(term.casefold() in opening for term in target_terms)


def _explicit_report_date(document: ExtractedDocument) -> str | None:
    """Read one unambiguous, calendar-valid labelled report date, not sampling.

    Native PDFs can contain CJK compatibility glyphs; images use the same
    rule over shared OCR text/tables. Conflicting headers are not resolved by
    taking the first page, the latest date, or the current date.
    """
    surfaces = [document.text, *(' | '.join(row) for table in document.tables for row in table.rows)]
    text = unicodedata.normalize('NFKC', '\n'.join(surfaces))
    pattern = (
        r'(?<![\w])(?:报告(?:日期|时间|生成(?:日期|时间)?)|出具日期'
        r'|Report\s+date|Date\s+reported|Date\s+of\s+report)\s*[:：|]?\s*'
        r'(\d{4})\s*[-_/年]\s*(\d{1,2})\s*[-_/月]\s*(\d{1,2})(?!\d)日?'
    )
    dates = {}
    for match in re.finditer(pattern, text, re.I):
        try:
            identity = date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            return None
        # Validate/compare dates canonically, but keep the printed spelling.
        dates.setdefault(identity, text[match.start(1) : match.end()].strip())
    return next(iter(dates.values())) if len(dates) == 1 else None


def _normalized_source_text(value: Any) -> str:
    if value is None:
        return ''
    return ' '.join(unicodedata.normalize('NFKC', str(value)).split())


def detection_status(value: Any, *, status: str | None = None) -> str:
    status_text = _clean(status) or ''
    if any(marker in status_text for marker in ('未检出', '阴性')):
        return 'not_detected'
    value_text = _clean(value) or ''
    if not value_text or value_text in {'-', '—', '--', '未报告'}:
        return 'unknown'
    if value_text.upper() in {'ND', 'N.D.'}:
        return 'not_detected'
    if value_text.startswith('<'):
        return 'trace'
    match = re.search(r'-?\d+(?:\.\d+)?', value_text)
    if match:
        return 'detected' if float(match.group(0)) > 0 else 'not_detected'
    if any(marker in status_text for marker in ('检出', '阳性')):
        return 'detected'
    return 'unknown'


def _detect_enterotype(page: pymupdf.Page, page_text: str) -> str | None:
    if match := _ENTEROTYPE_TEXT.search(page_text):
        return _normalize_enterotype(match.group(1))
    if '肠型分析' not in page_text or '检测项目' not in page_text:
        return None
    width, height = page.rect.width, page.rect.height
    x_start, x_end = width * 0.265, width * 0.89
    y_start, y_end = height * 0.202, height * 0.252
    segment = (x_end - x_start) / 3
    scores: list[int] = []
    for index in range(3):
        clip = pymupdf.Rect(
            x_start + segment * index,
            y_start,
            x_start + segment * (index + 1),
            y_end,
        )
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=clip, alpha=False)
        blue = 0
        for offset in range(0, len(pixmap.samples), pixmap.n):
            red, green, blue_value = pixmap.samples[offset : offset + 3]
            if blue_value - red > 25 and green - red > 15 and blue_value > 130:
                blue += 1
        scores.append(blue)
    winner = max(range(3), key=scores.__getitem__)
    alternatives = [score for index, score in enumerate(scores) if index != winner]
    if scores[winner] >= 300 and scores[winner] >= max(alternatives, default=0) * 3:
        return ('肠型I', '肠型II', '肠型III')[winner]
    return None


def _normalize_enterotype(value: str) -> str:
    normalized = value.strip().upper()
    return {
        'I': '肠型I',
        'Ⅰ': '肠型I',
        '1': '肠型I',
        'II': '肠型II',
        'Ⅱ': '肠型II',
        '2': '肠型II',
        'III': '肠型III',
        'Ⅲ': '肠型III',
        '3': '肠型III',
        'BACTEROIDES': '肠型I',
        'PREVOTELLA': '肠型II',
        'RUMINOCOCCUS': '肠型III',
    }.get(normalized, value)


def _category_from_text(value: str) -> str | None:
    compact = value.replace(' ', '')
    # Prefer section headings.  Introductory paragraphs may list several panel
    # types and must not make the last word in the paragraph win.
    heading_aliases = {
        '潜在致病真菌': 'fungi_pathogenic',
        '条件致病菌': 'opportunistic',
        '有害菌': 'opportunistic',
        '致病菌': 'pathogenic',
        '有益菌': 'beneficial',
        '其他健康相关菌': 'other',
        '其他菌': 'other',
    }
    standalone_hits: list[tuple[int, str]] = []
    for match in re.finditer(
        r'(?:^|\n)\s*(?:\d+(?:\.\d+)*[.\u3001]?\s*)?'
        r'(潜在致病真菌|条件致病菌|有害菌|(?<!条件)(?<!潜在)致病菌|'
        r'有益菌|其他健康相关菌|其他菌)\s*(?=\n|$)',
        value,
    ):
        standalone_hits.append((match.start(), heading_aliases[match.group(1)]))
    if standalone_hits:
        return max(standalone_hits, key=lambda item: item[0])[1]

    heading_patterns = (
        (r'核心菌属(?:构成)?表?', 'other'),
        (r'其它重要菌属(?:构成)?表?', 'other'),
        (r'其他重要菌属(?:构成)?表?', 'other'),
        (r'益生菌(?:种)?', 'beneficial'),
        (r'有害菌属(?:构成)?表?', 'opportunistic'),
        (r'病原菌属(?:构成)?表?', 'pathogenic'),
        (r'部分和健康相关的其他菌种检测结果', 'other'),
        (r'其他健康相关菌(?:检测|检出)?结果', 'other'),
        (r'其他菌(?:检测|检出)?结果', 'other'),
        (r'潜在致病真菌(?:检测|检出)?结果', 'fungi_pathogenic'),
        (r'条件致病菌(?:检测|检出)?结果', 'opportunistic'),
        (r'有害菌(?:检测|检出)?结果', 'opportunistic'),
        (
            r'(?<!条件)(?<!潜在)致病菌(?:检测|检出)?结果',
            'pathogenic',
        ),
        (r'有益菌(?:检测|检出)?结果', 'beneficial'),
    )
    heading_hits: list[tuple[int, str]] = []
    for pattern, category in heading_patterns:
        heading_hits.extend((match.start(), category) for match in re.finditer(pattern, compact))
    if heading_hits:
        return max(heading_hits, key=lambda item: item[0])[1]

    generic_hits = []
    generic_text = compact
    for marker, category in (
        ('潜在致病真菌', 'fungi_pathogenic'),
        ('条件致病菌', 'opportunistic'),
    ):
        if marker in generic_text:
            generic_hits.append(category)
            generic_text = generic_text.replace(marker, '')
    for marker, category in (
        ('有害菌', 'opportunistic'),
        ('致病菌', 'pathogenic'),
        ('有益菌', 'beneficial'),
    ):
        if marker in generic_text:
            generic_hits.append(category)
    if len(set(generic_hits)) == 1:
        return generic_hits[0]
    return None


def _category_from_aggregate_row(row: list[str]) -> str | None:
    first = row[0].strip() if row else ''
    return {
        '有益菌': 'beneficial',
        '有害菌': 'opportunistic',
        '条件致病菌': 'opportunistic',
        '致病菌': 'pathogenic',
        '潜在致病真菌': 'fungi_pathogenic',
        '其他健康相关菌': 'other',
        '其他菌': 'other',
    }.get(first)


def _taxon_section_ended(value: str) -> bool:
    compact = value.replace(' ', '')
    return any(
        marker in compact
        for marker in (
            '营养物质代谢能力',
            '短链脂肪酸合成',
            '维生素合成能力',
            '疾病风险预测',
            '疾病相关风险评估',
            '门级水平结果总览',
            '纲级水平结果总览',
            '目级水平结果总览',
            '科级水平结果总览',
            '属级水平结果总览',
            '疾病正、负相关菌检出结果',
            '疾病风险评估',
            '慢性疾病风险评估',
            '肠道菌群与疾病相关风险评估',
            '肠道微生物检测结果分析',
            '肠道微生物整体健康状况',
            '肠道微生物健康指数',
            '肠道微生物定植抗力',
            '厚壁菌门/拟杆菌门',
            '过敏风险评估',
            '肠道抗炎能力',
            '肠道免疫力',
            '问题相关性',
            '相关菌',
        )
    )


def _disease_risk_context_active(value: str) -> bool:
    compact = re.sub(r'\s+', '', value)
    starts = [
        compact.rfind(marker)
        for marker in (
            '肠道菌群与疾病相关风险评估',
            '慢性疾病风险评估',
            '疾病相关风险评估',
            '疾病风险评估',
        )
    ]
    start = max(starts, default=-1)
    if start < 0:
        return False
    stop = max(
        (
            compact.rfind(marker)
            for marker in (
                '肠道微生物检测结果分析',
                'ANALYSISOFINTESTINALMICROBIOTA',
                '健康建议',
                '参考文献',
            )
        ),
        default=-1,
    )
    return start > stop


def _disease_risk_text_blocks(text: str) -> list[str]:
    starts = list(
        re.finditer(
            r'(?:肠道菌群与)?(?:慢性)?疾病(?:相关)?风险评估',
            text,
        )
    )
    blocks: list[str] = []
    for match in starts:
        tail = text[match.end() :]
        stop = re.search(
            r'肠道微生物检测结果分析|'
            r'ANALYSIS\s*OF\s*INTESTINAL\s*MICROBIOTA|'
            r'参考文献',
            tail,
            flags=re.IGNORECASE,
        )
        blocks.append(tail[: stop.start() if stop else min(len(tail), 12000)])
    return blocks


def _taxon_value(row: list[str]) -> str | None:
    for cell in reversed(row[1:]):
        normalized = cell.strip().replace('%', '')
        if normalized in {'-', '—', '--'}:
            return None
        if re.fullmatch(r'<?\d+(?:\.\d+)?', normalized):
            return normalized
    return None


def _taxon_status(row: list[str]) -> str | None:
    for cell in reversed(row[1:]):
        compact = cell.replace(' ', '')
        for status in _TABLE_RESULT_STATUSES:
            if compact == status:
                return status
    return None


def _visual_table_row_status(
    page: pymupdf.Page,
    bbox: tuple[float, float, float, float],
) -> str | None:
    y0, y1 = float(bbox[1]), float(bbox[3])
    clip = pymupdf.Rect(
        page.rect.width * 0.82,
        y0,
        page.rect.width * 0.93,
        y1,
    )
    compact = (page.get_text('text', clip=clip) or '').replace(' ', '').replace('\n', '')
    for status in _TABLE_RESULT_STATUSES:
        if status in compact:
            return status
    return None


def _split_taxon_name(
    value: str,
    *,
    known_genera: Iterable[str] = (),
) -> tuple[str | None, str | None]:
    normalized = ' '.join(value.replace('#', '').replace('*', '').split())
    match = re.search(r'([A-Z][A-Za-z.-]+(?:\s+[a-z][A-Za-z.-]+)+)$', normalized)
    if not match:
        match = re.search(r'([A-Z][A-Za-z.-]{3,})$', normalized)
    if not match:
        return None, None
    latin = match.group(1)
    if ' ' not in latin:
        genus = next(
            (
                candidate
                for candidate in sorted(set(known_genera), key=len, reverse=True)
                if latin.startswith(candidate)
                and len(latin) - len(candidate) >= 3
                and latin[len(candidate) : len(candidate) + 1].islower()
            ),
            None,
        )
        if genus:
            latin = f'{genus} {latin[len(genus) :]}'
    chinese = normalized[: match.start()].strip(' （(') or None
    return chinese, latin


def _is_explicit_abnormal(status: Any, flag: Any) -> bool:
    if flag is True:
        return True
    text = _clean(status) or ''
    return any(
        marker in text
        for marker in (
            '偏高',
            '升高',
            '丰度高',
            '超标',
            '偏低',
            '降低',
            '丰度低',
            '缺乏',
            '异常',
            '高风险',
        )
    )


def _microbiome_panel_indexes(header: list[str]) -> dict[str, int | None] | None:
    if not _is_microbiome_panel_header(header):
        return None
    return {
        'name': _header_index_any(header, '名称', '菌名', '微生物名称'),
        'latin': _header_index_any(header, '拉丁学名', '学名'),
        'chinese': _header_index_any(header, '中文名称', '中文名'),
        'value': _header_index_any(header, '检测结果', '相对丰度', '丰度', '检测值'),
        'range': _header_index_any(header, '参考范围', '正常范围', '参考区间'),
        'status': _header_index_any(header, '结果评价', '是否异常', '状态', '评估'),
    }


def _is_microbiome_panel_header(row: list[str]) -> bool:
    has_separate_names = (
        _header_index_any(row, '拉丁学名', '学名') is not None
        and _header_index_any(row, '中文名称', '中文名') is not None
    )
    has_combined_name = _header_index_any(row, '名称', '菌名', '微生物名称') is not None
    has_value = _header_index_any(row, '检测结果', '相对丰度', '丰度', '检测值') is not None
    return has_value and (has_separate_names or has_combined_name)


def _microbiome_panel_row(
    row: list[str],
    indexes: dict[str, int | None],
    *,
    known_genera: Iterable[str] = (),
) -> tuple[str | None, str, str, str | None, str | None] | None:
    """Parse a taxon row by column meaning, including flattened PDF rows."""

    latin = _cell(row, indexes.get('latin'))
    chinese = _normalize_source_chinese_name(_cell(row, indexes.get('chinese')))
    combined_name = _cell(row, indexes.get('name'))
    raw_value = _cell(row, indexes.get('value'))
    reference_range = _clean(_cell(row, indexes.get('range')))
    status = _clean(_cell(row, indexes.get('status')))

    if not latin and combined_name and raw_value:
        chinese, latin = _split_taxon_name(
            combined_name,
            known_genera=known_genera,
        )

    # Native PDF extraction sometimes flattens all cells into the first
    # column. Recover only the source-printed name, abundance and interval;
    # trailing population percentiles are deliberately ignored.
    if (not latin or not raw_value) and combined_name:
        flattened = _flattened_taxon_row(
            combined_name,
            known_genera=known_genera,
        )
        if flattened is not None:
            chinese, latin, raw_value, printed_range = flattened
            reference_range = reference_range or printed_range

    if not latin or not _looks_like_latin_taxon(latin) or not raw_value:
        return None
    value, arrow_status = _source_value_and_arrow_status(raw_value)
    if value is None:
        return None
    status = status or arrow_status or _status_from_reference(value, reference_range)
    return chinese, latin, value, reference_range, status


def _flattened_taxon_row(
    value: str,
    *,
    known_genera: Iterable[str] = (),
) -> tuple[str | None, str, str, str | None] | None:
    normalized = _normalized_source_text(value)
    match = re.match(
        r'^(?P<name>.+?)\s+'
        r'(?P<value>ND|N\.D\.|<?-?\d+(?:\.\d+)?)(?P<arrow>[↑↓▲▼\uf175\uf176]?)%?\s+'
        r'(?P<range><?-?\d+(?:\.\d+)?(?:\s*[-~—–至]\s*<?-?\d+(?:\.\d+)?)?)'
        r'(?:\s|$)',
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    chinese, latin = _split_taxon_name(
        match.group('name'),
        known_genera=known_genera,
    )
    if not latin:
        return None
    raw_value = f'{match.group("value")}{match.group("arrow")}'
    return chinese, latin, raw_value, match.group('range')


def _status_from_reference(value: str, reference_range: str | None) -> str | None:
    if not reference_range:
        return None
    # Taxon abundance intervals are non-negative. A hyphen is the interval
    # separator, not the sign of the upper bound.
    numbers = [float(item) for item in re.findall(r'\d+(?:\.\d+)?', reference_range)]
    if not numbers:
        return None
    lower = numbers[0]
    upper = numbers[-1] if len(numbers) > 1 else numbers[0]
    if value.upper().replace('.', '') == 'ND':
        return '缺乏' if lower > 0 else None
    numeric = float(value.lstrip('<'))
    if numeric < lower:
        return '缺乏'
    if numeric > upper:
        return '超标'
    return None


def _header_index(row: list[str], expected: str) -> int | None:
    compact_expected = re.sub(r'\s+', '', expected)
    for index, cell in enumerate(row):
        compact_cell = re.sub(r'\s+', '', cell)
        if compact_expected in compact_cell:
            return index
    return None


def _header_index_any(row: list[str], *expected: str) -> int | None:
    for value in expected:
        index = _header_index(row, value)
        if index is not None:
            return index
    return None


def _source_risk_score(value: str) -> str | None:
    """Recover one explicit numeric score from a noisy OCR table cell.

    Brackets, isolated image markers and HTML line breaks are common layout
    artifacts. Multiple numeric values remain ambiguous and are rejected so
    the parser never invents or silently combines a source model score.
    """

    cleaned = re.sub(r'<br\s*/?>', ' ', value or '', flags=re.IGNORECASE)
    numbers = re.findall(r'(?<!\d)-?\d+(?:\.\d+)?(?!\d)', cleaned)
    if len(numbers) == 1:
        return numbers[0]
    decimals = [number for number in numbers if '.' in number]
    integers = [number for number in numbers if '.' not in number]
    # Page numbers and OCR image labels are sometimes appended after a score
    # with a line break. Keep the one decimal score only when every other token
    # is an integer artifact; competing decimal values remain ambiguous.
    if len(decimals) == 1 and integers:
        return decimals[0]
    return None


def _source_risk_level(value: str) -> str | None:
    match = re.search(
        r'(低风险|中低风险|中风险|中高风险|高风险|低相似|中相似|高相似|注意|需关注)',
        value or '',
    )
    if not match:
        return None
    return {
        '低相似': '低风险',
        '中相似': '中风险',
        '高相似': '高风险',
        '注意': '中风险',
        '需关注': '中风险',
    }.get(match.group(1), match.group(1))


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index < 0 or index >= len(row):
        return ''
    return ' '.join(row[index].split())


def _looks_like_latin_taxon(value: str) -> bool:
    return bool(
        re.fullmatch(
            r'[A-Za-z][A-Za-z0-9._-]*(?:\s+[A-Za-z][A-Za-z0-9._-]*)*',
            ' '.join(value.split()),
        )
    )


def _normalize_source_chinese_name(value: Any) -> str | None:
    normalized = _clean(value)
    if not normalized or normalized in {'-', '--', '—'}:
        return None
    return re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])', '', normalized)


def _source_value_and_arrow_status(value: str) -> tuple[str | None, str | None]:
    compact = _normalized_source_text(value).replace(' ', '').replace('%', '')
    arrow_status = None
    if any(marker in compact for marker in ('↑', '▲', '\uf176')):
        arrow_status = '超标'
    elif any(marker in compact for marker in ('↓', '▼', '\uf175')):
        arrow_status = '缺乏'
    normalized = compact.translate(str.maketrans('', '', '↑↓▲▼\uf175\uf176'))
    if normalized.upper() in {'ND', 'N.D.'}:
        return 'ND', arrow_status
    if not re.fullmatch(r'<?-?\d+(?:\.\d+)?', normalized):
        return None, arrow_status
    return normalized, arrow_status


def _document_pages(text: str) -> list[str]:
    pages = re.split(r'\n===== PAGE \d+ =====\n', text)
    return [page for page in pages if page.strip()]


def _normalize_source_disease_name(value: str) -> str | None:
    normalized = re.sub(r'\s+', '', value).strip('：:')
    if not normalized or len(normalized) > 60:
        return None
    aliases = {
        'Ⅱ型糖尿病': 'II型糖尿病',
        '非酒精性脂肪性肝病': '非酒精性脂肪肝',
    }
    return aliases.get(normalized, normalized)


def _split_source_related_taxa(value: str) -> list[str]:
    normalized = re.sub(r'\s+', '', value)
    return [item.strip('、，,') for item in re.split(r'[、，,]', normalized) if item.strip('、，,')]


def _is_source_image_placeholder(value: str) -> bool:
    normalized = re.sub(r'\s+', '', value).strip('_')
    return bool(re.fullmatch(r'<[A-Za-z0-9_-]+(?:png)?>', normalized, re.IGNORECASE))


def _looks_like_disease_or_health_problem(value: str) -> bool:
    """Reject adjacent provider-template rows that are not health outcomes.

    Unresolved provider exports use the same placeholder syntax for disease
    scores and for later environmental/function panels.  Disease-like Chinese
    names have a clinical outcome marker; substance names such as ``重金属`` do
    not.  This semantic guard avoids a report-specific disease allow-list.
    """

    compact = re.sub(r'\s+', '', value)
    return any(
        marker in compact
        for marker in (
            '病',
            '症',
            '炎',
            '癌',
            '肝',
            '狼疮',
            '硬化',
            '综合征',
            '障碍',
            '风险',
            '便秘',
            '腹泻',
            '癫痫',
            '帕金森',
            '高血压',
            '高血脂',
            '高尿酸',
        )
    )


def _clean_table(rows: list[list[Any]]) -> list[list[str]]:
    return [
        [' '.join(str(cell or '').replace('\n', ' ').split()) for cell in row]
        for row in rows
        if any(str(cell or '').strip() for cell in row)
    ]


def _clean(value: Any) -> str | None:
    # Numeric zero is a measured/source-model result, not a missing value.
    text = ' '.join(str(value if value is not None else '').split()).strip()
    return text or None


def _dedupe(items: Iterable[Any], *, key: Any) -> list[Any]:
    result: list[Any] = []
    seen: set[Any] = set()
    for item in items:
        marker = key(item)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


def _merge_document_taxa(items: Iterable[ParsedTaxon]) -> list[ParsedTaxon]:
    """Deduplicate repeated tables without losing an explicit source flag."""
    merged: dict[str, ParsedTaxon] = {}
    for item in items:
        existing = merged.get(item.taxon_id)
        if existing is None:
            merged[item.taxon_id] = item
            continue
        if item.explicitly_abnormal and not existing.explicitly_abnormal:
            merged[item.taxon_id] = item
            continue
        if (
            item.explicitly_abnormal == existing.explicitly_abnormal
            and item.reference_range
            and not existing.reference_range
        ):
            merged[item.taxon_id] = item
    return list(merged.values())


_TABLE_RESULT_STATUSES = (
    '重度超标',
    '中度超标',
    '轻度超标',
    '重度缺乏',
    '中度缺乏',
    '轻度缺乏',
    '丰度高',
    '丰度低',
    '偏高',
    '偏低',
    '异常',
    '未检出',
    '检出',
    '正常',
    '优秀',
)


_ENTEROTYPE_TEXT = re.compile(
    r'肠型\s*[:：]?\s*(?:肠型\s*)?(III|II|I|Ⅲ|Ⅱ|Ⅰ|3|2|1)(?!\s*/)',
    flags=re.IGNORECASE,
)


_TABLE_METRIC_DEFINITIONS = (
    ('gmhi', '肠道微生物健康指数', ('肠道微生物健康指数', '肠道菌群健康指数'), None),
    ('diversity', '菌群多样性', ('菌群多样性',), None),
    (
        'species_count',
        '肠道微生物种类',
        ('肠道菌群种类', '肠道微生物种类', '菌种数'),
        '种',
    ),
    ('be_ratio', '肠道微生物定植抗力', ('B/E比值', '肠道微生物定植抗力'), None),
    ('fb_ratio', '厚壁菌门/拟杆菌门', ('F/B比值', '肠道菌群热量代谢'), None),
    ('pathogenic_potential', '肠道菌群致病潜力', ('肠道菌群致病潜力',), None),
    ('dietary_fiber_need', '肠道膳食纤维需求', ('肠道膳食纤维需求',), None),
    ('biofilm_capacity', '肠道菌群生物膜合成能力', ('肠道菌群生物膜合成能力',), None),
    (
        'oxidative_stress_tolerance',
        '肠道菌群氧化胁迫耐受',
        ('肠道菌群氧化胁迫耐受',),
        None,
    ),
    ('formate', '甲酸合成能力', ('甲酸',), None),
    ('acetate', '乙酸合成能力', ('乙酸',), None),
    ('propionate', '丙酸合成能力', ('丙酸',), None),
    ('butyrate', '丁酸合成能力', ('丁酸',), None),
    ('valerate', '戊酸合成能力', ('戊酸',), None),
)


_DOCUMENT_METRICS = (
    (
        'gmhi',
        '肠道微生物健康指数',
        (r'肠道微生物健康指数(?:（GMWI2）)?(?:评估)?\s*(?:[|｜:：]\s*)?(-?\d+(?:\.\d+)?)',),
        None,
    ),
    (
        'diversity',
        '菌群多样性',
        (
            r'(?:肠道)?菌群多样性(?:评估)?\s*(?:[|｜:：]\s*)?(\d+(?:\.\d+)?)',
            r'肠道微生物多样性\s*(?:[|｜:：]\s*)?(\d+(?:\.\d+)?)',
        ),
        None,
    ),
    (
        'species_count',
        '肠道微生物种类',
        (
            r'肠道(?:微生物|菌群)种类(?:评估)?\s*(?:[|｜:：]\s*)?(\d+)',
            r'菌种数\s*[:：|]?\s*(\d+)',
        ),
        '种',
    ),
    (
        'fb_ratio',
        '厚壁菌门/拟杆菌门',
        (r'F/B(?:比值)?\s*(?:[|｜:：]\s*)?(\d+(?:\.\d+)?)',),
        None,
    ),
    (
        'be_ratio',
        '肠道微生物定植抗力',
        (
            r'B/E(?:比值)?\s*(?:[|｜:：]\s*)?(\d+(?:\.\d+)?)',
            r'肠道微生物定植抗力(?:评估)?\s*(?:[|｜:：]\s*)?(\d+(?:\.\d+)?)',
        ),
        None,
    ),
    (
        'pathogenic_potential',
        '肠道菌群致病潜力',
        (r'肠道菌群致病潜力(?:评估)?\s*(?:[|｜:：]\s*)?(\d+(?:\.\d+)?)',),
        None,
    ),
    (
        'dietary_fiber_need',
        '肠道膳食纤维需求',
        (r'肠道膳食纤维需求(?:评估)?\s*(?:[|｜:：]\s*)?(\d+(?:\.\d+)?)',),
        None,
    ),
    (
        'biofilm_capacity',
        '肠道菌群生物膜合成能力',
        (r'肠道菌群生物膜合成能力(?:评估)?\s*(?:[|｜:：]\s*)?(\d+(?:\.\d+)?)',),
        None,
    ),
    (
        'oxidative_stress_tolerance',
        '肠道菌群氧化胁迫耐受',
        (r'肠道菌群氧化胁迫耐受(?:评估)?\s*(?:[|｜:：]\s*)?(\d+(?:\.\d+)?)',),
        None,
    ),
)
