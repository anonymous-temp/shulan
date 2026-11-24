"""Content acceptance checks of one six-chapter evaluate run (assessment.v2).

    python scripts/check_assessment_report.py RUN_DIR --request request.json
    python scripts/check_assessment_report.py RUN_DIR --request request.json --strict --output check.json

RUN_DIR is the output directory of ``scripts/generate_evaluation_report.py``.
The delivered six-chapter body is read from ``callback_v2.json`` when present,
otherwise ``callback.json`` is converted with
``build_frontend_evaluation_callback``. ``audit.json`` is optional.

The checks follow wiki/report-assessment-v2.md (sections 1.2, 1.3, 2 and 5):

1. sections of the six chapters that are empty (a missing-data sentence counts
   as content), and list entries without their interpretation text;
2. forbidden reader-facing words, with the field path of every hit;
3. abnormal checkup rows of the request (non-normal ``resultStatusName`` or a
   value outside ``referenceRange``) that never appear in the report;
4. numbers with a unit in report prose that cannot be traced to the request,
   the chapter-4 microbiome rows, program-derived rows or target tables;
5. P0 items against the red-flag rules (``modules.report.assessment_red_flags``,
   skipped when that module is absent);
6. length: Han characters and table rows of reader-visible content;
7. status, partialReasons and elapsedSeconds.

The JSON result is written to ``RUN_DIR/assessment_check.json`` unless
``--output`` says otherwise; a readable summary goes to stdout. With
``--strict`` the exit status is 1 when any check fails. Results may quote
report text, so keep them next to the run, outside the repository.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
import unicodedata
from decimal import Decimal
from decimal import InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TIME_LIMIT_SECONDS = 1800
TIME_TARGET_SECONDS = 1200

FORBIDDEN_TERMS = (
    '您',
    '原件',
    '原报告',
    '主检',
    '模板',
    '未形成解读',
    '部分报告',
    '按原件',
    '问卷显示',
    '检测方',
    '检测机构',
    '免责',
    '仅供参考',
)
# Need a human look: "模型" is allowed for a microbiome risk model.
ADVISORY_TERMS = ('模型', '绝非', '必然', '彻底', '全军覆没', '引擎', '为什么')
PLACEHOLDER_TERMS = ('暂无可展示', '暂无解读', '暂无内容', '待补充')
# Ordinary words that contain a forbidden term ("检测方法" is not "检测方").
TERM_EXCEPTIONS = {'检测方': r'检测方(?=法|式|案|面|向)', '模板': r'模板(?=化)'}

# Keys that carry identifiers or rendering metadata, never reader text.
HIDDEN_KEYS = frozenset(
    {
        'code',
        'id',
        'recordId',
        'sourceCode',
        'sources',
        'evidenceRefs',
        'url',
        'doi',
        'clinicalDataType',
        'sourceKind',
        'detectionStatus',
        'sourcePage',
        'sourceSection',
        'specimen',
        'method',
        'filledSegments',
        'kind',
        'question',
        'level',
        'direction',
        'referenceKind',
        'graphClusterIds',
        'clusterIds',
        'medicationRecords',
    }
)
PROSE_KEYS = frozenset(
    {
        'summary',
        'interpretation',
        'text',
        'action',
        'preface',
        'description',
        'explanation',
        'history',
        'conclusion',
    }
)
TABLE_KEYS = frozenset(
    {
        'rows',
        'templateRows',
        'specialtyFindings',
        'clinicalSignals',
        'microbiomeSignals',
        'lifestyleFactors',
        'panelStates',
        'basisRows',
    }
)
CHAPTER_NAMES = {
    'introduction': '封面与前言',
    'overview': '第一章',
    'symptoms': '第二章',
    'clinical': '第三章',
    'microbiome': '第四章',
    'clusters': '第五章',
    'conclusion': '第六章',
}
# (id, title, chapter code, fields); "list[].field" reads one field of every entry.
SECTIONS = (
    ('cover', '封面（资料日期与时效）', 'introduction', ('dataTiming',)),
    ('preface', '前言与阅读指引', 'introduction', ('preface', 'readingGuide')),
    ('1.1', '1.1 基础体格与人体成分', 'overview', ('bodySummary', 'bodyGroups', 'bodyMetrics')),
    ('1.2', '1.2 多系统健康状态分级总览', 'overview', ('systems',)),
    ('1.3', '1.3 临床优先排查项与未检风险提示', 'overview', ('priorityFindings',)),
    ('1.4', '1.4 核心系统失衡态势速览', 'overview', ('graphSummary',)),
    ('2.1', '2.1 主诉症状谱与临床检验信号印证', 'symptoms', ('symptoms', 'symptomsLead')),
    ('2.2', '2.2 躯体感知背后的微生态与代谢线索', 'symptoms', ('symptoms[].interpretation', 'symptomSummary')),
    ('3.1-3.5', '3.1–3.5 分系统临床检验表', 'clinical', ('clinicalGroups', 'summary')),
    ('3.6a', '3.6 历年指标演变趋势', 'clinical', ('trends', 'trendSummary')),
    ('3.6b', '3.6 已确认稳健安全基线', 'clinical', ('normalBaselines',)),
    ('4.0', '第四章章首说明（采样与发酵耐受）', 'microbiome', ('sampleSummary', 'fermentationSummary')),
    ('4.1', '4.1 消化道器质基础与消化液微环境', 'microbiome', ('digestiveContext',)),
    ('4.2', '4.2 微生态宏观稳态指数', 'microbiome', ('structureGroups', 'structure', 'overviewSummary')),
    ('4.3', '4.3 核心功能菌群结构与条件致病谱', 'microbiome', ('organismGroups',)),
    ('4.4', '4.4 菌群营养合成与代谢潜能', 'microbiome', ('functionalGroups',)),
    ('4.5', '第四章风险小结', 'microbiome', ('riskSummary',)),
    ('5.1', '5.1 系统失衡集群定性与健康影响', 'clusters', ('clusters', 'clusterSummary')),
    ('5.2', '5.2 ATM 健康诱因复盘', 'clusters', ('lifestyleFactors', 'lifestyleSummary')),
    ('6.1', '6.1 核心健康风险分层总结', 'conclusion', ('layers', 'priorities', 'summary')),
    ('6.2', '6.2 微生态与生活方式协同管理方向', 'conclusion', ('managementDirections',)),
)
# Entries of these lists must carry their own interpretation text.
ENTRY_TEXT_RULES = (
    ('overview', 'systems', 'summary'),
    ('symptoms', 'symptoms', 'interpretation'),
    ('clinical', 'clinicalGroups', 'summary'),
    ('microbiome', 'digestiveContext', 'summary'),
    ('microbiome', 'structureGroups', 'summary'),
    ('microbiome', 'organismGroups', 'summary'),
    ('microbiome', 'functionalGroups', 'summary'),
    ('clusters', 'clusters', 'interpretation'),
)
NORMAL_STATUS = frozenset({'', '正常', '阴性', '阴性(-)', '-', '—', '参考范围内', '未见异常', '合格', 'n', 'normal'})
QUALITATIVE_NORMAL = ('阴性', '未查见', '未见', '无', '正常', '-')
VALUE_NORMAL = ('阴性', '未查见', '未见', '正常', '无')
UNITS = (
    'mL/min/1.73m2',
    'mL/min/1.73m²',
    'ml/min/1.73m2',
    '×10^9/L',
    '×10^12/L',
    '×10⁹/L',
    '×10¹²/L',
    '10^9/L',
    '10^12/L',
    '10E9/L',
    '10E12/L',
    'mmol/mol',
    'mmol/L',
    'μmol/L',
    'µmol/L',
    'umol/L',
    'nmol/L',
    'pmol/L',
    'μIU/mL',
    'µIU/mL',
    'uIU/mL',
    'mIU/mL',
    'mIU/L',
    'IU/mL',
    'IU/L',
    'U/mL',
    'U/L',
    'mg/dL',
    'mg/L',
    'mg/g',
    'g/dL',
    'g/L',
    'ng/mL',
    'ng/L',
    'μg/L',
    'µg/L',
    'ug/L',
    'μg/dL',
    'pg/mL',
    'kg/m2',
    'kg/m²',
    'mmHg',
    'cm/s',
    'ml/s',
    'mL/min',
    'kcal',
    'cm2',
    'cm²',
    '/HP',
    '次/分',
    'bpm',
    'pg',
    'fL',
    'kg',
    'cm',
    'mm',
    'mL',
    '%',
    '岁',
    '分',
)
_NUMBER = r'(?<![\d.])(\d+(?:\.\d+)?)'
_UNIT_RE = '|'.join(re.escape(unit) for unit in sorted(UNITS, key=len, reverse=True))
UNIT_NUMBER = re.compile(
    _NUMBER + r'(?:\s*[-~–—至]\s*(\d+(?:\.\d+)?))?\s*(' + _UNIT_RE + r')(?![A-Za-z钟])',
    re.IGNORECASE,
)
ANY_NUMBER = re.compile(r'\d+(?:\.\d+)?')
TARGET_FILES = ('modules/report/assessment_targets.json', 'modules/report/assessment_template_01_comparisons.json')
RED_FLAG_MODULE = 'modules.report.assessment_red_flags'
RED_FLAG_FUNCTIONS = (
    'evaluate_red_flags',
    'match_red_flags',
    'detect_red_flags',
    'find_red_flags',
    'red_flag_hits',
    'evaluate',
)


# ---------------------------------------------------------------- loading


def load_body(run_dir: Path, callback: Path | None = None) -> tuple[dict, str]:
    """Return the delivered assessment.v2 callback body and the file it came from."""
    candidates = [callback] if callback else [run_dir / 'callback_v2.json', run_dir / 'callback.json']
    for path in candidates:
        if not path or not path.is_file():
            continue
        body = json.loads(path.read_text(encoding='utf-8'))
        if (body.get('data') or {}).get('version') == 'assessment.v2':
            return body, path.name
        from modules.report.evaluation_callback import build_frontend_evaluation_callback

        converted = build_frontend_evaluation_callback(body)
        return converted, f'{path.name} (converted)'
    raise SystemExit(f'no callback_v2.json or callback.json in {run_dir}')


def load_audit(run_dir: Path) -> dict:
    path = run_dir / 'audit.json'
    if not path.is_file():
        return {}
    audit = json.loads(path.read_text(encoding='utf-8'))
    return {key: audit.get(key) for key in ('elapsedSeconds', 'status', 'partialReasons')}


def chapters_of(body: dict) -> dict[str, dict]:
    data = body.get('data') or {}
    sections = {'introduction': data.get('introduction') or {}}
    for chapter in data.get('chapters') or ():
        sections[chapter.get('code')] = chapter.get('content') or {}
    return sections


# ---------------------------------------------------------------- walking


def _hidden(key: str) -> bool:
    return key in HIDDEN_KEYS or key.endswith('EvidenceRefs') or key.endswith('Ids')


def _filled_list(value) -> bool:
    return isinstance(value, list) and any(_filled(item) for item in value)


def _skipped_keys(node: dict) -> set[str]:
    """Duplicates the renderer drops (see compact_assessment_for_rendering)."""
    skipped = set()
    if _filled_list(node.get('bodyGroups')):
        skipped.add('bodyMetrics')
    if _filled_list(node.get('structureGroups')):
        skipped.add('structure')
    if node.get('rows') and 'names' in node:
        skipped.add('names')
    if node.get('code') != 'otherClinical' and any(row.get('rows') for row in _dicts(node.get('templateRows'))):
        skipped.add('rows')
    return skipped


def _dicts(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def visible_strings(node, path: str = '', key: str = ''):
    """Yield (path, key, text) for every reader-visible string."""
    if isinstance(node, str):
        if node.strip():
            yield path, key, node
    elif isinstance(node, dict):
        skipped = _skipped_keys(node)
        for child_key, value in node.items():
            if _hidden(child_key) or child_key in skipped:
                continue
            yield from visible_strings(value, f'{path}.{child_key}' if path else child_key, child_key)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from visible_strings(item, f'{path}[{index}]', key)


def _is_prose(key: str) -> bool:
    return key in PROSE_KEYS or key.endswith('Summary') or key.endswith('Lead')


def _filled(value) -> bool:
    """True when a field holds reader content; placeholders do not count."""
    if value is None or value is False:
        return False
    if isinstance(value, str):
        text = value.strip()
        return bool(text) and not any(term in text for term in PLACEHOLDER_TERMS)
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, list):
        return any(_filled(item) for item in value)
    if isinstance(value, dict):
        return any(_filled(item) for name, item in value.items() if not _hidden(name))
    return False


def _normalize(text: str) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).casefold()


def _number_key(text: str) -> str | None:
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    normalized = number.normalize()
    return format(normalized, 'f') if normalized == normalized.to_integral() else str(normalized)


def _numbers(text: str) -> set[str]:
    return {key for key in map(_number_key, ANY_NUMBER.findall(unicodedata.normalize('NFKC', text))) if key}


def _all_texts(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        yield str(node)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _all_texts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _all_texts(item)


def _excerpt(text: str, start: int, end: int, width: int = 14) -> str:
    return text[max(0, start - width) : end + width].replace('\n', ' ')


# ---------------------------------------------------------------- 1. sections


def _field_value(content: dict, field: str):
    if '[].' in field:
        name, _, child = field.partition('[].')
        return [entry.get(child) for entry in _dicts(content.get(name))]
    return content.get(field)


def check_sections(chapters: dict[str, dict]) -> dict:
    results = []
    for section_id, title, chapter, fields in SECTIONS:
        content = chapters.get(chapter)
        filled = [field for field in fields if content and _filled(_field_value(content, field))]
        results.append(
            {
                'id': section_id,
                'title': title,
                'chapter': chapter,
                'chapterPresent': content is not None,
                'filledFields': filled,
                'empty': not filled,
            }
        )
    entries = []
    for chapter, field, text_field in ENTRY_TEXT_RULES:
        for index, entry in enumerate(_dicts((chapters.get(chapter) or {}).get(field))):
            state = f'{entry.get("level") or ""}{entry.get("status") or ""}'
            if 'notTested' in state or '未检测' in state:
                continue
            if not _filled(entry.get(text_field)):
                entries.append(
                    {
                        'path': f'{chapter}.{field}[{index}].{text_field}',
                        'name': entry.get('name') or entry.get('id') or entry.get('code'),
                    }
                )
    return {
        'checked': len(results),
        'empty': [row['title'] for row in results if row['empty']],
        'emptyEntries': entries,
        'missingChapters': [CHAPTER_NAMES[code] for code in CHAPTER_NAMES if code not in chapters],
        'details': results,
    }


# ---------------------------------------------------------------- 2. words


def check_words(chapters: dict[str, dict]) -> dict:
    hits, advisory, placeholders = [], [], []
    for chapter, content in chapters.items():
        for path, _, text in visible_strings(content, chapter):
            for terms, sink in ((FORBIDDEN_TERMS, hits), (ADVISORY_TERMS, advisory), (PLACEHOLDER_TERMS, placeholders)):
                for term in terms:
                    exception = TERM_EXCEPTIONS.get(term)
                    for match in re.finditer(re.escape(term), text):
                        if exception and re.match(exception, text[match.start() :]):
                            continue
                        sink.append({'term': term, 'path': path, 'excerpt': _excerpt(text, match.start(), match.end())})

    def by_term(rows):
        counts = {}
        for row in rows:
            counts[row['term']] = counts.get(row['term'], 0) + 1
        return dict(sorted(counts.items(), key=lambda item: -item[1]))

    return {
        'total': len(hits),
        'byTerm': by_term(hits),
        'locations': hits,
        'advisory': {'total': len(advisory), 'byTerm': by_term(advisory), 'locations': advisory},
        'placeholders': placeholders,
    }


# ---------------------------------------------------------------- 3. abnormal rows


def _sex_segment(reference: str, sex: str | None) -> str:
    """Pick the segment of a combined '男…;女…' range that matches the subject."""
    parts = [part for part in re.split(r'[;；,，]', reference) if part.strip()]
    if sex in ('男', '女') and len(parts) > 1:
        for part in parts:
            if sex in part:
                return part
    return reference


def parse_reference(reference: str | None, sex: str | None = None):
    """Return (low, high, qualitative-token) parsed from a printed reference range."""
    if not reference:
        return None, None, None
    text = unicodedata.normalize('NFKC', _sex_segment(reference, sex))
    text = text.replace('＜', '<').replace('＞', '>')
    match = re.search(r'(-?\d+(?:\.\d+)?)\s*(?:-|~|—|–|至)\s*(\d+(?:\.\d+)?)', text)
    if match:
        return float(match[1]), float(match[2]), None
    match = re.search(r'(<=|≤|<)\s*(\d+(?:\.\d+)?)', text)
    if match:
        return None, float(match[2]), None
    match = re.search(r'(>=|≥|>)\s*(\d+(?:\.\d+)?)', text)
    if match:
        return float(match[2]), None, None
    for token in QUALITATIVE_NORMAL:
        if token in text:
            return None, None, token
    return None, None, None


def _value_text(value) -> str | None:
    """The number of a single numeric result such as '15.2', '<1.20' or '3.0↓'."""
    text = unicodedata.normalize('NFKC', str(value)).strip()
    match = re.fullmatch(r'[<>≤≥]?\s*(-?\d+(?:\.\d+)?)\s*[↑↓HL]?', text)
    return match[1] if match else None


def _value_number(value) -> float | None:
    text = _value_text(value)
    return float(text) if text is not None else None


def abnormal_reason(row: dict, sex: str | None) -> str | None:
    status = unicodedata.normalize('NFKC', str(row.get('resultStatusName') or '')).strip()
    if status.casefold() not in NORMAL_STATUS:
        return f'status:{status}'
    if row.get('dataType') != 'INSPECTION':
        return None
    value = row.get('testResultValue')
    if value in (None, ''):
        return None
    low, high, token = parse_reference(row.get('referenceRange'), sex)
    number = _value_number(value)
    if number is not None and (low is not None or high is not None):
        if low is not None and number < low:
            return 'range:low'
        if high is not None and number > high:
            return 'range:high'
        return None
    if token and number is None:
        shown = unicodedata.normalize('NFKC', str(value)).strip()
        if shown not in ('-', '−') and not any(word in shown for word in VALUE_NORMAL):
            return 'qualitative'
    return None


def abnormal_rows(request: dict) -> list[dict]:
    sex = request.get('sex') or request.get('gender')
    rows = []
    for row in request.get('hospitalData') or ():
        reason = abnormal_reason(row, sex)
        if reason:
            rows.append(
                {
                    'name': row.get('itemName'),
                    'value': row.get('testResultValue') if row.get('dataType') == 'INSPECTION' else None,
                    'unit': row.get('unit'),
                    'referenceRange': row.get('referenceRange'),
                    'date': row.get('reportTime') or row.get('checkTime'),
                    'reason': reason,
                }
            )
    return rows


def name_variants(name: str) -> list[str]:
    """Full name, the part outside brackets, abbreviations, Greek-free and suffix-free forms."""
    text = unicodedata.normalize('NFKC', name or '')
    inner = re.findall(r'[(（]([^()（）]+)[)）]', text)
    outer = re.sub(r'[(（][^()（）]*[)）]', '', text)
    variants = [text, outer, *inner]
    for part in list(variants):
        variants.extend(re.split(r'[/、]', part))
        variants.append(re.sub(r'^[αβγδ]-?', '', part))
        variants.append(re.sub(r'(?:试验|检测|测定|检查|计数|测量)$', '', part))
    seen = []
    for variant in variants:
        key = _normalize(variant)
        if len(key) >= 2 and not re.fullmatch(r'[\d.\-]+', key) and key not in seen:
            seen.append(key)
    return seen


def _contains_name(haystack: str, variant: str) -> bool:
    if re.fullmatch(r'[a-z0-9\-]+', variant):
        return re.search(r'(?<![a-z])' + re.escape(variant) + r'(?![a-z])', haystack) is not None
    return variant in haystack


def check_abnormal_coverage(request: dict, chapters: dict[str, dict]) -> dict:
    strings = [(key, text) for content in chapters.values() for _, key, text in visible_strings(content)]
    everything = _normalize('\n'.join(text for _, text in strings))
    prose = _normalize('\n'.join(text for key, text in strings if _is_prose(key)))
    numbers_all = _numbers('\n'.join(text for _, text in strings))
    items, missing, value_only, not_in_prose = [], [], [], []
    for row in abnormal_rows(request):
        variants = name_variants(row['name'])
        named = any(_contains_name(everything, variant) for variant in variants)
        number = _value_text(row['value']) if row['value'] is not None else None
        valued = number is not None and _number_key(number) in numbers_all
        match = 'name+value' if named and valued else 'name' if named else 'value' if valued else None
        in_prose = any(_contains_name(prose, variant) for variant in variants)
        item = {**row, 'match': match, 'inProse': in_prose}
        items.append(item)
        label = f'{row["name"]} {row["value"] if row["value"] is not None else ""}'.strip()
        if match is None:
            missing.append(label)
        elif match == 'value':
            value_only.append(label)
        if match and not in_prose:
            not_in_prose.append(label)
    return {
        'abnormalCount': len(items),
        'missing': missing,
        'valueOnly': value_only,
        'notInProse': not_in_prose,
        'items': items,
    }


# ---------------------------------------------------------------- 4. numbers


def _target_numbers(chapters: dict[str, dict]) -> set[str]:
    numbers = set()
    for relative in TARGET_FILES:
        path = ROOT / relative
        if path.is_file():
            try:
                numbers |= _numbers('\n'.join(_all_texts(json.loads(path.read_text(encoding='utf-8')))))
            except (OSError, ValueError):
                pass

    def visit(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ('target', 'comparisonReference', 'targetRange'):
                    numbers.update(_numbers('\n'.join(_all_texts(value))))
                else:
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(chapters)
    return numbers


def _derived_numbers(chapters: dict[str, dict]) -> set[str]:
    numbers = set()

    def visit(node):
        if isinstance(node, dict):
            if node.get('sourceKind') == 'derived':
                numbers.update(_numbers(' '.join(str(node.get(key) or '') for key in ('name', 'value'))))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(chapters)
    return numbers


def _scalars(node):
    """Numeric values under reader-visible keys (scores, abundances, bounds)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if _hidden(key):
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                yield str(value)
            else:
                yield from _scalars(value)
    elif isinstance(node, list):
        for item in node:
            yield from _scalars(item)


def _microbiome_numbers(chapters: dict[str, dict]) -> set[str]:
    """Structured (non-prose) values of chapter 4 and of every microbiomeSignals row."""
    microbiome = chapters.get('microbiome') or {}
    texts = [text for _, key, text in visible_strings(microbiome) if not _is_prose(key)]
    texts.extend(_scalars(microbiome))

    def visit(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == 'microbiomeSignals':
                    texts.extend(text for _, child, text in visible_strings(value) if not _is_prose(child))
                else:
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(chapters)
    return _numbers('\n'.join(texts))


def _rule_numbers() -> set:
    """Thresholds quoted from the red-flag and test-gap rule tables (not patient data)."""
    texts = []
    for module_name, attribute in (
        ('modules.report.assessment_test_gaps', 'GAP_RULES'),
        ('modules.report.assessment_red_flags', 'RULES'),
    ):
        try:
            rules = getattr(importlib.import_module(module_name), attribute, ())
        except Exception:  # an optional rule table must not break the check
            continue
        texts.append(json.dumps(list(rules), ensure_ascii=False, default=str))
    return _numbers('\n'.join(texts))


def check_numbers(request: dict, chapters: dict[str, dict]) -> dict:
    pools = {
        'request': _numbers('\n'.join(_all_texts(request))),
        'microbiome': _microbiome_numbers(chapters),
        'derived': _derived_numbers(chapters),
        'target': _target_numbers(chapters),
        'rule': _rule_numbers(),
    }
    checked, by_pool, missing, seen = 0, dict.fromkeys(pools, 0), [], set()
    for chapter, content in chapters.items():
        for path, key, text in visible_strings(content, chapter):
            if not _is_prose(key):
                continue
            normalized = unicodedata.normalize('NFKC', text)
            for match in UNIT_NUMBER.finditer(normalized):
                for raw in filter(None, (match[1], match[2])):
                    number = _number_key(raw)
                    marker = (path, number, match[3])
                    if number is None or marker in seen:
                        continue
                    seen.add(marker)
                    checked += 1
                    pool = next((name for name, values in pools.items() if number in values), None)
                    if pool:
                        by_pool[pool] += 1
                    else:
                        missing.append(
                            {
                                'value': f'{raw} {match[3]}',
                                'path': path,
                                'excerpt': _excerpt(normalized, match.start(), match.end()),
                            }
                        )
    return {'checked': checked, 'found': checked - len(missing), 'byPool': by_pool, 'missing': missing}


# ---------------------------------------------------------------- 5. P0


def report_p0_items(chapters: dict[str, dict]) -> list[dict]:
    items, seen = [], set()

    def visit(node, path):
        if isinstance(node, dict):
            if str(node.get('priority') or '').upper() == 'P0':
                label = node.get('name') or node.get('text') or node.get('action') or node.get('id') or ''
                text = ' '.join(str(node.get(key) or '') for key in ('name', 'text', 'action', 'interpretation'))
                if _normalize(label) not in seen:
                    seen.add(_normalize(label))
                    items.append({'path': path, 'label': label, 'text': text})
            for key, value in node.items():
                if not _hidden(key):
                    visit(value, f'{path}.{key}')
        elif isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, f'{path}[{index}]')

    for chapter, content in chapters.items():
        visit(content, chapter)
    return items


def _hit_label(hit) -> str:
    if isinstance(hit, str):
        return hit
    fields = hit if isinstance(hit, dict) else getattr(hit, '__dict__', {}) or {}
    if not fields and hasattr(hit, 'model_dump'):
        fields = hit.model_dump()
    keys = ('itemName', 'item', 'name', 'label', 'title', 'finding', 'trigger', 'rule', 'text', 'reason')
    parts = [str(fields[key]) for key in keys if isinstance(fields.get(key), str) and fields.get(key)]
    return ' '.join(dict.fromkeys(parts)) or str(hit)


def _bigrams(text: str) -> set[str]:
    chars = re.sub(r'[^一-鿿a-z0-9]', '', _normalize(text))
    return {chars[index : index + 2] for index in range(len(chars) - 1)}


def _related(hit_label: str, p0_text: str) -> bool:
    hit_terms = [variant for variant in name_variants(hit_label) if len(variant) >= 3]
    target = _normalize(p0_text)
    if any(term in target for term in hit_terms):
        return True
    grams = _bigrams(hit_label)
    return bool(grams) and len(grams & _bigrams(p0_text)) / len(grams) >= 0.5


def request_fact_rows(request: dict) -> list[dict]:
    """Source fact rows (the red-flag rule input shape) from request ``hospitalData``."""
    rows = []
    for index, row in enumerate(request.get('hospitalData') or ()):
        if not isinstance(row, dict):
            continue
        value = row.get('testResultValue')
        if value in (None, ''):
            value = row.get('examResult') or row.get('examConclusion')
        rows.append(
            {
                'factId': f'R{index + 1}',
                'kind': 'clinical',
                'name': row.get('itemName'),
                'value': value,
                'unit': row.get('unit'),
                'status': row.get('resultStatusName'),
                'referenceRange': row.get('referenceRange'),
            }
        )
    return rows


def red_flag_hits(request: dict, function: str | None = None) -> dict:
    """Call the optional red-flag rule module; never fail the whole check."""
    module_name, _, attribute = (function or RED_FLAG_MODULE).partition(':')
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name == module_name:
            return {'available': False, 'reason': f'{module_name} 不存在，已跳过', 'hits': []}
        return {'available': False, 'reason': f'{module_name} 导入失败：{error}', 'hits': []}
    except Exception as error:  # a broken rule module must not hide the other checks
        return {'available': False, 'reason': f'{module_name} 导入失败：{type(error).__name__}', 'hits': []}
    names = (attribute,) if attribute else RED_FLAG_FUNCTIONS
    candidates = [getattr(module, name) for name in names if callable(getattr(module, name, None))]
    if not candidates:
        return {'available': False, 'reason': f'{module_name} 中未找到 {"/".join(names)}', 'hits': []}
    evaluate = getattr(module, 'evaluate_red_flags', None)
    if callable(evaluate) and not attribute and module_name == RED_FLAG_MODULE:
        # The rule table reads source fact rows; build them from the request rows.
        facts = request_fact_rows(request)
        subject = {'sex': request.get('sex'), 'age': request.get('age')}
        try:
            result = evaluate(facts, subject=subject)
        except (TypeError, AttributeError, KeyError, ValueError) as error:
            return {'available': False, 'reason': f'evaluate_red_flags 调用失败：{type(error).__name__}', 'hits': []}
        hits = list((result or {}).get('priorityItems') or [])
        return {
            'available': True,
            'reason': f'{module_name}.evaluate_red_flags(请求体检行 {len(facts)} 条)',
            'hits': [_hit_label(hit) for hit in hits],
        }
    arguments = []
    try:
        from modules.report.schemas import ReportRequest

        arguments.append(ReportRequest.model_validate(request))
    except Exception:  # an unparsable request still gets the raw shapes below
        pass
    arguments += [request, request.get('hospitalData') or []]
    errors = []
    for candidate in candidates:
        for argument in arguments:
            try:
                result = candidate(argument)
            except (TypeError, AttributeError, KeyError, ValueError) as error:
                errors.append(f'{candidate.__name__}({type(argument).__name__}): {type(error).__name__}')
                continue
            if isinstance(result, dict):
                result = result.get('hits') or result.get('items') or result.get('flags') or []
            hits = list(result or [])
            return {
                'available': True,
                'reason': f'{module_name}.{candidate.__name__}({type(argument).__name__})',
                'hits': [_hit_label(hit) for hit in hits],
            }
    return {'available': False, 'reason': '红旗规则调用失败：' + '; '.join(errors[:4]), 'hits': []}


def check_p0(request: dict | None, chapters: dict[str, dict], function: str | None = None) -> dict:
    p0 = report_p0_items(chapters)
    flags = (
        red_flag_hits(request, function)
        if request is not None
        else {'available': False, 'reason': '未提供请求', 'hits': []}
    )
    result = {'reportP0': [item['label'] for item in p0], 'redFlags': flags, 'consistent': None}
    if not flags['available']:
        return result
    unreflected = [hit for hit in flags['hits'] if not any(_related(hit, item['text']) for item in p0)]
    unsupported = [item['label'] for item in p0 if not any(_related(hit, item['text']) for hit in flags['hits'])]
    result.update(
        unreflectedHits=unreflected, p0WithoutRedFlag=unsupported, consistent=not unreflected and not unsupported
    )
    return result


# ---------------------------------------------------------------- 6/7. length and status


def _count_rows(node, inside_template: bool = False) -> int:
    total = 0
    if isinstance(node, dict):
        skipped = _skipped_keys(node)
        for key, value in node.items():
            if _hidden(key) or key in skipped:
                continue
            if key in TABLE_KEYS and isinstance(value, list):
                if not (inside_template and key == 'rows'):
                    total += len(_dicts(value))
                for item in _dicts(value):
                    total += _count_rows(item, inside_template or key == 'templateRows')
            else:
                total += _count_rows(value, inside_template)
    elif isinstance(node, list):
        total += sum(_count_rows(item, inside_template) for item in node)
    return total


def check_length(chapters: dict[str, dict]) -> dict:
    hanzi, rows = {}, {}
    for chapter, content in chapters.items():
        label = CHAPTER_NAMES.get(chapter, chapter)
        text = ''.join(text for _, _, text in visible_strings(content))
        hanzi[label] = len(re.findall(r'[一-鿿]', text))
        rows[label] = _count_rows(content)
    return {
        'hanzi': {'total': sum(hanzi.values()), 'byChapter': hanzi},
        'tableRows': {'total': sum(rows.values()), 'byChapter': rows},
    }


def check_status(body: dict, audit: dict) -> dict:
    elapsed = audit.get('elapsedSeconds')
    return {
        'status': body.get('status'),
        'partialReasons': body.get('partialReasons') or [],
        'elapsedSeconds': elapsed,
        'overTimeLimit': elapsed is not None and elapsed > TIME_LIMIT_SECONDS,
        'overTimeTarget': elapsed is not None and elapsed > TIME_TARGET_SECONDS,
    }


# ---------------------------------------------------------------- report


def check_run(
    run_dir: Path,
    request: dict | None,
    *,
    callback: Path | None = None,
    red_flag_function: str | None = None,
) -> dict:
    body, source = load_body(run_dir, callback)
    chapters = chapters_of(body)
    result = {
        'runDir': str(run_dir),
        'callbackSource': source,
        'status': check_status(body, load_audit(run_dir)),
        'sections': check_sections(chapters),
        'words': check_words(chapters),
        'abnormalCoverage': check_abnormal_coverage(request, chapters) if request is not None else None,
        'numberTrace': check_numbers(request, chapters) if request is not None else None,
        'p0': check_p0(request, chapters, red_flag_function),
        'length': check_length(chapters),
    }
    result['failures'] = failures(result)
    return result


def failures(result: dict) -> list[str]:
    found = []
    status = result['status']
    if status['status'] == 'failed':
        found.append('status=failed')
    if status['overTimeLimit']:
        found.append(f'耗时 {status["elapsedSeconds"]}s 超过 {TIME_LIMIT_SECONDS}s')
    sections = result['sections']
    if sections['empty'] or sections['emptyEntries']:
        found.append(f'空节 {len(sections["empty"])}、空条目 {len(sections["emptyEntries"])}')
    if result['words']['total']:
        found.append(f'禁用词 {result["words"]["total"]} 处')
    if result['words']['placeholders']:
        found.append(f'占位语 {len(result["words"]["placeholders"])} 处')
    coverage = result['abnormalCoverage']
    if coverage and coverage['missing']:
        found.append(f'异常项遗漏 {len(coverage["missing"])} 项')
    trace = result['numberTrace']
    if trace and trace['missing']:
        found.append(f'数值无法回溯 {len(trace["missing"])} 处')
    if result['p0']['consistent'] is False:
        found.append('P0 与危险信号不一致')
    return found


def summary_lines(result: dict, *, limit: int = 8) -> list[str]:
    status, sections, words = result['status'], result['sections'], result['words']
    lines = [
        f'== {result["runDir"]}  [{result["callbackSource"]}]',
        f'状态 {status["status"]}  partialReasons={",".join(status["partialReasons"]) or "-"}  '
        f'耗时 {status["elapsedSeconds"] if status["elapsedSeconds"] is not None else "未知"}s',
        f'1 空节 {len(sections["empty"])}/{sections["checked"]}: {"、".join(sections["empty"]) or "无"}',
    ]
    if sections['missingChapters']:
        lines.append(f'  缺章: {"、".join(sections["missingChapters"])}')
    if sections['emptyEntries']:
        shown = '; '.join(f'{row["path"]}({row["name"]})' for row in sections['emptyEntries'][:limit])
        lines.append(f'  空条目 {len(sections["emptyEntries"])}: {shown}')
    terms = '、'.join(f'{term}×{count}' for term, count in words['byTerm'].items())
    lines.append(f'2 禁用词 {words["total"]} 处: {terms or "无"}')
    for row in words['locations'][:limit]:
        lines.append(f'  {row["term"]} @ {row["path"]}')
    if words['advisory']['total']:
        advisory = '、'.join(f'{term}×{count}' for term, count in words['advisory']['byTerm'].items())
        lines.append(f'  待人工判断: {advisory}')
    if words['placeholders']:
        lines.append(f'  占位语 {len(words["placeholders"])} 处')
    coverage = result['abnormalCoverage']
    if coverage is None:
        lines.append('3 异常项覆盖: 未提供请求，跳过')
    else:
        lines.append(
            f'3 异常项 {coverage["abnormalCount"]} 项，遗漏 {len(coverage["missing"])}: '
            f'{"、".join(coverage["missing"]) or "无"}'
        )
        if coverage['valueOnly']:
            lines.append(f'  仅数值匹配 {len(coverage["valueOnly"])}: {"、".join(coverage["valueOnly"][:limit])}')
        if coverage['notInProse']:
            lines.append(f'  未进入解读文字 {len(coverage["notInProse"])}: {"、".join(coverage["notInProse"][:limit])}')
    trace = result['numberTrace']
    if trace is None:
        lines.append('4 数值回溯: 未提供请求，跳过')
    else:
        pools = '、'.join(f'{name} {count}' for name, count in trace['byPool'].items())
        lines.append(
            f'4 带单位数值 {trace["checked"]} 处，可回溯 {trace["found"]}（{pools}），找不到 {len(trace["missing"])}'
        )
        for row in trace['missing'][:limit]:
            lines.append(f'  {row["value"]} @ {row["path"]}')
    p0 = result['p0']
    flags = p0['redFlags']
    lines.append(f'5 报告 P0 {len(p0["reportP0"])} 项: {"、".join(p0["reportP0"]) or "无"}')
    if not flags['available']:
        lines.append(f'  危险信号: {flags["reason"]}')
    else:
        lines.append(f'  危险信号 {len(flags["hits"])} 条（{flags["reason"]}）一致={p0["consistent"]}')
        if p0.get('unreflectedHits'):
            lines.append(f'  未进入 P0 的危险信号: {"、".join(p0["unreflectedHits"])}')
        if p0.get('p0WithoutRedFlag'):
            lines.append(f'  无危险信号依据的 P0: {"、".join(p0["p0WithoutRedFlag"])}')
    length = result['length']
    hanzi = '、'.join(f'{name} {count}' for name, count in length['hanzi']['byChapter'].items())
    rows = '、'.join(f'{name} {count}' for name, count in length['tableRows']['byChapter'].items())
    lines.append(f'6 汉字 {length["hanzi"]["total"]}（{hanzi}）')
    lines.append(f'  表格行 {length["tableRows"]["total"]}（{rows}）')
    lines.append(f'结论: {"通过" if not result["failures"] else "未通过 — " + "；".join(result["failures"])}')
    return lines


def _arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('run_dir', help='output directory of generate_evaluation_report.py')
    parser.add_argument('--request', help='request JSON of the run (enables checks 3, 4 and 5)')
    parser.add_argument('--callback', help='callback file to check instead of RUN_DIR/callback_v2.json|callback.json')
    parser.add_argument('--output', help='JSON result path (default RUN_DIR/assessment_check.json; "-" = stdout)')
    parser.add_argument('--red-flags', metavar='MODULE[:FUNCTION]', help='red-flag rule entry point override')
    parser.add_argument('--limit', type=int, default=8, help='locations listed per check in the summary')
    parser.add_argument('--strict', action='store_true', help='exit 1 when any check fails')
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _arguments(argv)
    run_dir = Path(args.run_dir).expanduser().resolve()
    request = json.loads(Path(args.request).read_text(encoding='utf-8')) if args.request else None
    result = check_run(
        run_dir,
        request,
        callback=Path(args.callback).expanduser().resolve() if args.callback else None,
        red_flag_function=args.red_flags,
    )
    document = json.dumps(result, ensure_ascii=False, indent=1)
    if args.output == '-':
        print(document)
    else:
        output = Path(args.output) if args.output else run_dir / 'assessment_check.json'
        output.write_text(document, encoding='utf-8')
        print('\n'.join(summary_lines(result, limit=args.limit)))
        print(f'JSON: {output}')
    return 1 if args.strict and result['failures'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
