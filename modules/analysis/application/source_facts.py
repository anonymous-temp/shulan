"""Reconcile permission-checked source cells without changing the public schema."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass

from .extraction import normalize_document_time
from .extraction import validated_document_time


def source_key(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


@dataclass(frozen=True)
class SourceFacts:
    observations: tuple[dict, ...] = ()
    replaced_names: frozenset[str] = frozenset()
    excluded_layouts: frozenset[int] = frozenset()
    covered_source_ids: frozenset[str] = frozenset()
    title: str | None = None
    report_time: str | None = None
    non_result_items: frozenset[tuple[str, str]] = frozenset()


def _layout(item):
    proof = item.get('evidence') or {}
    locator = proof.get('locator') or {}
    if type(locator.get('layout')) is int:
        return locator['layout']
    source = item.get('sourceId') or proof.get('sourceId') or ''
    match = re.match(r'(?:t|l)(\d+)', source)
    return int(match[1]) if match else None


def _physical_row(item):
    locator = (item.get('evidence') or {}).get('locator') or {}
    page = locator.get('physicalPage', locator.get('sourcePage'))
    table, row = locator.get('sourceTableIndex'), locator.get('sourceRowIndex')
    checksum = locator.get('sourceChecksum')
    if checksum and all(type(value) is int for value in (page, table, row)):
        return checksum, page, table, row
    return None


def _unique_unlocated_names(report):
    if report.get('reportType') != 'microbiome' or report.get('sections'):
        return set()
    observations = report.get('observations', [])
    pending = report.get('unresolvedItems', [])
    groups = [observations + pending]
    expansion = report.get('expansion')
    if isinstance(expansion, list) and expansion != observations:
        # Only the established expansion mirror can be ignored for uniqueness.
        groups.append(expansion)
    counts = Counter(source_key(item.get('itemName')) for group in groups for item in group)
    return {name for name, count in counts.items() if count == 1}


def _covered_by_date_alias(item, candidates, date, *, allow_unlocated):
    normalized = validated_document_time(date)
    if not normalized:
        return False
    position = _physical_row(item)
    if position is not None:
        candidates = [native for native in candidates if _physical_row(native) == position]
    elif (
        not allow_unlocated
        or len(candidates) != 1
        or item.get('evidence')
        or item.get('sourceId')
        or 'sourceSection' in item
    ):
        return False
    candidates = [native for native in candidates if validated_document_time(native.get('observedAt')) == normalized]
    if len(candidates) != 1:
        return False
    native = candidates[0]
    return all(
        not item.get(field) or not native.get(field) or item[field] == native[field]
        for field in ('unit', 'specimen', 'method')
    )


def _covered(item, facts, report_time, unlocated_names):
    proof = item.get('evidence') or {}
    if (item.get('sourceId') or proof.get('sourceId')) in facts.covered_source_ids:
        return True
    if _layout(item) in facts.excluded_layouts:
        return True
    key = source_key(item.get('itemName'))
    if key not in facts.replaced_names:
        return False
    candidates = []
    for native in facts.observations:
        proof = native.get('evidence') or {}
        aliases = (proof.get('rawFields') or {}).get('nameAliases') or []
        if key in {source_key(native.get('itemName')), *(source_key(name) for name in aliases)}:
            candidates.append(native)
    if not candidates:
        return False
    date = item.get('observedAt') or (report_time if 'sourceSection' not in item else None)
    known_dates = {native['observedAt'] for native in candidates if native.get('observedAt')}
    if date and known_dates and date not in known_dates:
        return _covered_by_date_alias(item, candidates, date, allow_unlocated=key in unlocated_names)
    if date:
        candidates = [native for native in candidates if native.get('observedAt') in (None, date)]
    locator = (item.get('evidence') or {}).get('locator') or {}
    for field in ('sourceFile', 'sourceChecksum'):
        owner = locator.get(field)
        owners = {(native.get('evidence') or {}).get('locator', {}).get(field) for native in candidates}
        owners.discard(None)
        if owner and owners and owner not in owners:
            return False
    page = locator.get('physicalPage', locator.get('sourcePage'))
    pages = {page} if page is not None else {value + 1 for value in locator.get('pages', []) if type(value) is int}
    native_pages = {
        (native.get('evidence') or {})
        .get('locator', {})
        .get('physicalPage', (native.get('evidence') or {}).get('locator', {}).get('sourcePage'))
        for native in candidates
    }
    native_pages.discard(None)
    return not (pages and native_pages and pages.isdisjoint(native_pages))


def _report_dates(report):
    date = normalize_document_time(report.get('reportTime'))
    dates = {date} if date else set()
    for child in report.get('sections', []):
        dates.update(_report_dates(child))
    return dates


def reconciled_issues(report, facts, issues):
    """Remove only diagnostics for source occurrences repaired by the reader."""
    if not facts or not facts.observations:
        return issues
    repaired = set(facts.covered_source_ids)
    unlocated_names = _unique_unlocated_names(report)
    source_date = validated_document_time(facts.report_time)

    def visit(node):
        for field in ('observations', 'unresolvedItems'):
            for item in node.get(field, []):
                if _covered(item, facts, node.get('reportTime'), unlocated_names):
                    proof = item.get('evidence') or {}
                    if source := item.get('sourceId') or proof.get('sourceId'):
                        repaired.add(source)
        for child in node.get('sections', []):
            visit(child)

    visit(report)
    # An unanchored timeout or an uncovered source remains partial. A successful
    # cell repair is not proof that the rest of the document was processed.
    return [
        issue
        for issue in issues
        if not (
            (
                issue.get('sourceIds')
                and set(issue['sourceIds']) <= repaired
                and issue.get('code') not in {'analysis_deadline', 'source_input_too_long'}
            )
            or issue.get('code') == 'report_title_evidence_missing'
            and facts.title
            and issue.get('path') in (None, '$.title')
            or issue.get('code') == 'report_date_evidence_missing'
            and source_date
            and issue.get('path') in (None, '$.reportTime')
            and _report_dates(report) <= {source_date}
        )
    ]


def reconcile_source_facts(report, facts):
    """Replace only this source's explicitly covered items; keep unknowns pending.

    A native table row can correct a bad OCR/model mirror. It cannot discard a
    different patient, file, date or unsupported name by a substring match.
    Non-patient tables are excluded by their original layout identity only.
    """
    if not facts or not facts.observations:
        return report
    result = deepcopy(report)
    metadata = result.get('metadataEvidence')
    if facts.title:
        if result.get('title') != facts.title and isinstance(metadata, dict):
            metadata.pop('title', None)
        result['title'] = facts.title
    source_date = validated_document_time(facts.report_time)
    if source_date and _report_dates(result) <= {source_date} and not result.get('reportTime'):
        # Restore an absent cover date only within a consistent report date range.
        if isinstance(metadata, dict):
            metadata.pop('reportTime', None)
        result['reportTime'] = facts.report_time

    unlocated_names = _unique_unlocated_names(report)

    def visit(node):
        report_time = node.get('reportTime')
        for key in ('observations', 'unresolvedItems'):
            node[key] = [item for item in node.get(key, []) if not _covered(item, facts, report_time, unlocated_names)]
        node['unresolvedItems'] = [
            item
            for item in node['unresolvedItems']
            if not (
                item.get('resultValue') is None
                and (
                    item.get('sourceId') or (item.get('evidence') or {}).get('sourceId'),
                    source_key(item.get('itemName')),
                )
                in facts.non_result_items
            )
        ]
        if isinstance(node.get('expansion'), list):
            node['expansion'] = [
                item for item in node['expansion'] if not _covered(item, facts, report_time, unlocated_names)
            ]
        for child in node.get('sections', []):
            visit(child)

    visit(result)
    # The existing section link is also the public locator: readers can tell
    # apart contradictory labels printed on different source panels/pages.
    groups = {}
    for original in facts.observations:
        item = deepcopy(original)
        locator = (item.get('evidence') or {}).get('locator') or {}
        page = locator.get('physicalPage', locator.get('sourcePage'))
        if page is not None:
            title = locator.get('sourceSection') or locator.get('sourceCategory') or '菌群检测原始记录'
            key = page, title
            if key not in groups:
                sections = result.setdefault('sections', [])
                index = len(sections)
                section = {
                    'reportType': 'microbiome',
                    'title': f'{title}（原报告第{page}页）',
                    'reportTime': item.get('observedAt'),
                    'status': 'pending',
                    'aiSummary': None,
                    'observations': [],
                    'expansion': [],
                }
                sections.append(section)
                groups[key] = index, section
            index, section = groups[key]
            item['sourceSection'] = index
            section['observations'].append(deepcopy(item))
            section['expansion'].append(deepcopy(item))
        result['observations'].append(item)
    if result.get('reportType') in {'lab', 'microbiome'}:
        result['expansion'] = deepcopy(result['observations'])
    return result
