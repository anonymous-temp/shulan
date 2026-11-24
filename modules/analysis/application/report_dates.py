"""Bind explicit report-date metadata to bounded source report occurrences."""

from __future__ import annotations

import hashlib
import re

from .extraction import validated_document_time

_DATE = re.compile(r'(?<![\d./])\d{4}(?:([-_/.])\d{1,2}\1\d{1,2}|年\d{1,2}月\d{1,2}日?)(?![\d./])')
_DATE_ROLES = {
    '报告日期': 'reportTime',
    '报告时间': 'reportTime',
    '送检日期': 'submittedAt',
    '送检时间': 'submittedAt',
    '采样日期': 'collectedAt',
    '采样时间': 'collectedAt',
    '采集日期': 'collectedAt',
    '检查日期': 'observedAt',
    '检查时间': 'observedAt',
    '检测日期': 'observedAt',
    '检测时间': 'observedAt',
    '检验日期': 'observedAt',
}
_IDENTITIES = {'检测编号', '样本编号', '报告编号', '标本编号', '申请单号', '标本号'}
_REPORT_TITLE = re.compile(r'(?:报告单|检验报告|检查报告|检测报告|体检报告|影像报告|健康评估报告)(?:$|[（(])')
_REFERENCE_TITLE = re.compile(r'^(?:\d+[.、\s]*)*(?:参考文献|参考资料|参考人群|参考案例|示例|样例|案例|科普|方法学)')
_LABELS = '|'.join(map(re.escape, sorted(_DATE_ROLES.keys() | _IDENTITIES, key=len, reverse=True)))
_FIELD_LINE = re.compile(rf'^\s*({_LABELS})\s*[:：]\s*(.+?)\s*$')


def document_dates(text):
    """Find complete, valid dates, excluding numeric ranges and DOI identifiers."""
    found = []
    for line in text.splitlines():
        if re.search(r'\bdoi\b|doi\.org', line, re.I):
            continue
        for match in _DATE.finditer(line):
            if value := validated_document_time(match.group()):
                found.append(value)
    return found


def labelled_dates(text, roles):
    """Read date roles locally; an unlabelled date is never auto-promoted."""
    found = []
    for match in _DATE.finditer(text):
        prefix = text[max(0, match.start() - 24) : match.start()]
        label = re.search(r'([\u3400-\u9fff]{4})\s*[:：|]\s*(?:\|\s*)?$', prefix)
        if label and _DATE_ROLES.get(label[1]) in roles:
            if value := validated_document_time(match.group()):
                found.append(value)
    return found


def metadata_fields(layout, index):
    """Read labelled key/value rows or lines; geometry must identify one value."""
    fields = []

    def add(label, raw, proof):
        label = re.sub(r'\s+', '', label).rstrip(':：')
        if label in _IDENTITIES:
            value = raw.strip()
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_\-/]{2,79}', value):
                return
            role = 'reportIdentity'
        elif label in _DATE_ROLES:
            role = _DATE_ROLES[label]
            value = validated_document_time(raw)
            if not value:
                return
        else:
            return
        fields.append({'role': role, 'value': value, 'rawValue': raw, 'label': label, 'locator': proof})

    cells = layout.get('cells')
    if isinstance(cells, list):
        rows = {}
        for ci, cell in enumerate(cells):
            if (
                not isinstance(cell, dict)
                or type(cell.get('ysc')) is not int
                or cell.get('ysc') != cell.get('yec')
                or type(cell.get('xsc')) is not int
                or not isinstance(cell.get('layouts'), list)
            ):
                continue
            text = ' '.join(
                child['text'].strip()
                for child in cell['layouts']
                if isinstance(child, dict) and isinstance(child.get('text'), str)
            ).strip()
            if text:
                rows.setdefault(cell.get('ysc'), []).append((cell.get('xsc', 0), ci, text))
        for row in rows.values():
            row.sort()
            if len(row) == 2:
                add(
                    row[0][2],
                    row[1][2],
                    {
                        'layout': index,
                        'pages': layout.get('pageNum'),
                        'labelCell': row[0][1],
                        'valueCell': row[1][1],
                    },
                )
    for line_index, line in enumerate(layout.get('text', '').splitlines()):
        if match := _FIELD_LINE.fullmatch(line):
            add(match[1], match[2], {'layout': index, 'pages': layout.get('pageNum'), 'line': line_index})
    return fields


def report_date_scopes(layouts):
    """A new report heading/identity ends the previous date scope.

    A later labelled metadata sheet can confirm an earlier cover bearing the
    exact same report/sample identity. It never supplies dates to another cover
    or to a distinct identity. Conflicting dates inside one identity are kept
    unresolved instead of choosing one by proximity.
    """
    fields, reference_regions = [], set()
    in_reference = False
    for index, layout in enumerate(layouts):
        if layout.get('type') in {'title', 'table_name'}:
            in_reference = bool(_REFERENCE_TITLE.match(layout.get('text', '').strip()))
        if in_reference:
            reference_regions.add(index)
        fields.append([] if in_reference else metadata_fields(layout, index))
    identities = {field['value'] for group in fields for field in group if field['role'] == 'reportIdentity'}
    groups = []
    current = {'start': 0, 'identity': None, 'fields': [], 'title': None}

    def finish(end):
        nonlocal current
        if end > current['start']:
            groups.append({**current, 'end': end})
        current = {'start': end, 'identity': None, 'fields': [], 'title': None}

    for index, layout in enumerate(layouts):
        if index in reference_regions:
            continue
        text = layout.get('text', '').strip()
        is_heading = layout.get('type') == 'title' and len(text) <= 100 and bool(_REPORT_TITLE.search(text))
        if is_heading:
            if index > current['start'] and (current['identity'] or current['fields'] or current['title']):
                finish(index)
            current['title'] = text
        group = list(fields[index])
        # An unlabelled cover identity is accepted only when a labelled metadata
        # row elsewhere supplies the exact identifier, never by guessing a token.
        if text in identities and not any(field['role'] == 'reportIdentity' for field in group):
            group.append(
                {
                    'role': 'reportIdentity',
                    'value': text,
                    'rawValue': text,
                    'label': None,
                    'locator': {'layout': index, 'pages': layout.get('pageNum')},
                }
            )
        local_ids = {field['value'] for field in group if field['role'] == 'reportIdentity'}
        if len(local_ids) == 1:
            identity = next(iter(local_ids))
            if current['identity'] and current['identity'] != identity:
                finish(index)
            current['identity'] = identity
        elif len(local_ids) > 1:
            # Distinct identifiers can be distinct ID roles for one sheet. The
            # relationship is unresolved; do not bridge this sheet to a cover.
            finish(index)
            current['identityConflict'] = True
        report_dates = {field['value'] for field in group if field['role'] == 'reportTime'}
        previous_dates = {field['value'] for field in current['fields'] if field['role'] == 'reportTime'}
        if report_dates and previous_dates and report_dates != previous_dates and not current['identity']:
            finish(index)
        current['fields'].extend(group)
    finish(len(layouts))

    scopes = {}
    for group in groups:
        dates = [field for field in group['fields'] if field['role'] == 'reportTime']
        unique = {field['value'] for field in dates}
        if not dates:
            continue
        # Metadata without an identity can govern only the forward interval from
        # its report header/date, not earlier unrelated result pages.
        start = group['start']
        if not group['identity'] and not group['title']:
            start = min(field['locator']['layout'] for field in dates)
        key = hashlib.sha256(f'{start}:{group["end"]}:{group["identity"] or ""}'.encode()).hexdigest()[:16]
        scope = {
            'id': key,
            'startLayout': start,
            'endLayout': group['end'],
            'reportIdentity': group['identity'],
        }
        if len(unique) == 1 and not group.get('identityConflict'):
            scope['reportTime'] = dates[0]['value']
            scope['evidence'] = dates[0]
        else:
            scope['dateConflict'] = True
        for index in range(start, group['end']):
            scopes[index] = scope
    return scopes


def source_report_date(unit):
    scope = unit.locator.get('reportScope') or {}
    return scope.get('reportTime') if not scope.get('dateConflict') else None


def date_fallback(item, unit):
    """Preserve explicit item time; otherwise bind the owning report's date."""
    scope = unit.locator.get('reportScope') or {}
    if item.get('observedAt'):
        if normalized := validated_document_time(item['observedAt']):
            item['observedAt'] = normalized
        else:
            item.setdefault('evidence', {})['unresolvedDate'] = {
                'role': 'observedAt',
                'rawValue': item.pop('observedAt'),
            }
    if not item.get('observedAt') and (value := source_report_date(unit)):
        item['observedAt'] = value
        item.setdefault('evidence', {})['date'] = {
            'scopeId': scope['id'],
            'role': 'reportTime',
            'fallback': True,
            **scope['evidence'],
        }
