"""Lossless document segmentation and request-scoped, stable result merging."""

from __future__ import annotations

import asyncio
import re
import weakref
from dataclasses import dataclass
from datetime import date
from datetime import datetime


@dataclass(frozen=True, slots=True)
class DocumentTextChunk:
    start: int
    end: int
    text: str


def split_document_text(content: str, limit: int, *, offset: int = 0) -> list[DocumentTextChunk]:
    """Partition every character, preferring page/line boundaries over arbitrary cuts.

    A long OCR line must occasionally be split. Offsets and the untouched source
    slices remain available for auditing; no head/tail summarization is used.
    """
    if limit < 1:
        raise ValueError('document chunk limit must be positive')
    chunks = []
    start = 0
    while start < len(content):
        end = min(start + limit, len(content))
        if end < len(content):
            lower = start + limit // 2
            boundary = content.rfind('\f', lower, end)
            if boundary < lower:
                boundary = content.rfind('\n', lower, end)
            if boundary < lower:
                boundary = max(content.rfind('。', lower, end), content.rfind(';', lower, end))
            if boundary >= lower:
                end = boundary + 1
        chunks.append(DocumentTextChunk(offset + start, offset + end, content[start:end]))
        start = end
    return chunks


# Shared across interpreter/service instances in one running event loop. Weak
# references prevent a completed loop or semaphore from retaining user requests.
_ANALYSIS_LIMITERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
ANALYSIS_CONCURRENCY = 4

_CALENDAR_DATE = re.compile(
    r'^(?P<year>\d{4})\s*(?:'
    r'(?P<separator>[-_/.])\s*(?P<month>\d{1,2})\s*(?P=separator)\s*(?P<day>\d{1,2})'
    r'|年\s*(?P<cn_month>\d{1,2})\s*月\s*(?P<cn_day>\d{1,2})\s*日?'
    r')(?P<time>(?:[ T.].*)?)$'
)
_CLOCK_TIME = re.compile(
    r'^[ T.](?P<hour>\d{1,2}):(?P<minute>\d{2})'
    r'(?::(?P<second>\d{2})(?P<fraction>\.\d{1,6})?)?'
    r'(?P<zone>Z|[+-]\d{2}:?\d{2})?$'
)
_DATE_FIELD_LABEL = re.compile(r'^(?:报告|送检|检测|检验|检查|体检|采样|采集)(?:日期|时间)\s*[:：]\s*')


def normalize_document_time(value):
    """Normalize only explicit, valid year-first dates; preserve time/zone data.

    Ambiguous, partial and invalid dates are left untouched, not guessed or
    assigned a current year. This is formatting normalization, not fact filling.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    original = value.strip()
    # OCR commonly retains a full-width clock colon. Normalize only this date
    # field and only publish the conversion after calendar/clock validation.
    # A model may retain the explicit source field label in this date field.
    # Remove only a recognized leading label; never search arbitrary prose for
    # dates or accept a partly parsed range/multiple dates.
    date_text = _DATE_FIELD_LABEL.sub('', original)
    match = _CALENDAR_DATE.fullmatch(date_text.replace('：', ':'))
    if match is None:
        return original
    try:
        calendar = date(
            int(match['year']), int(match['month'] or match['cn_month']), int(match['day'] or match['cn_day'])
        ).isoformat()
    except ValueError:
        return original
    tail = match['time']
    if not tail:
        return calendar
    clock = _CLOCK_TIME.fullmatch(tail)
    if clock is None:
        return original
    clock_text = f'{int(clock["hour"]):02d}:{clock["minute"]}'
    if clock['second'] is not None:
        clock_text += ':' + clock['second'] + (clock['fraction'] or '')
    zone = clock['zone'] or ''
    if zone == 'Z':
        zone = '+00:00'
    elif zone and ':' not in zone:
        zone = zone[:3] + ':' + zone[3:]
    try:
        datetime.fromisoformat(f'{calendar}T{clock_text}{zone}')
    except ValueError:
        return original
    return f'{calendar}T{clock_text}{zone}'


def merge_document_times(values):
    """Unify equivalent formats without concealing genuinely different dates."""
    unique = list(dict.fromkeys(normalize_document_time(value) for value in values if value))
    if len(unique) <= 1:
        return unique[0] if unique else None
    try:
        days = {date.fromisoformat(value) for value in unique if 'T' not in value}
        timestamps = [datetime.fromisoformat(value) for value in unique if 'T' in value]
    except (TypeError, ValueError):
        return None
    # A date-only page is compatible with one precisely timed report on that
    # same day. Keep the explicit time rather than truncating it to a date.
    if timestamps and len(set(timestamps)) == 1 and (not days or days == {timestamps[0].date()}):
        return next(value for value in unique if 'T' in value)
    return None


def validated_document_time(value):
    """Return a calendar-validated date/time, never an unchanged invalid input."""
    normalized = normalize_document_time(value)
    if not normalized:
        return None
    try:
        datetime.fromisoformat(normalized)
    except (TypeError, ValueError):
        return None
    return normalized


def analysis_limiter() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    reference = _ANALYSIS_LIMITERS.get(loop)
    limiter = reference() if reference is not None else None
    if limiter is None:
        limiter = asyncio.Semaphore(ANALYSIS_CONCURRENCY)
        _ANALYSIS_LIMITERS[loop] = weakref.ref(limiter)
    return limiter


def document_sections(part: dict) -> list[dict]:
    """Keep independent root facts; remove only an identifiable merged projection.

    Current model data may contain root table rows alongside imaging sections.
    Only items carrying ``sourceSection`` and matching that section's projected
    occurrence are mirrors. Merely having sections does not make the root empty.
    """
    from copy import deepcopy

    children = part.get('sections')
    if not children:
        return [deepcopy(part)]
    sections = [node for child in children for node in document_sections(child)]
    root = deepcopy(part)
    root.pop('sections', None)
    root.pop('sectionIndex', None)

    def is_projection(item, field='observations'):
        index = item.get('sourceSection')
        if type(index) is not int or not 0 <= index < len(sections):
            return False
        source = sections[index]
        candidate = {key: value for key, value in item.items() if key != 'sourceSection'}
        for original in source.get(field, []):
            projected = {key: value for key, value in original.items() if key != 'sourceSection'}
            date_text = normalize_document_time(original.get('observedAt')) or normalize_document_time(
                source.get('reportTime')
            )
            if date_text:
                projected['observedAt'] = date_text
            if candidate == projected:
                return True
        return False

    root['observations'] = [item for item in root.get('observations', []) if not is_projection(item)]
    root['unresolvedItems'] = [
        item for item in root.get('unresolvedItems', []) if not is_projection(item, 'unresolvedItems')
    ]
    if isinstance(root.get('expansion'), list):
        root['expansion'] = deepcopy(root['observations'])
    expansion = root.get('expansion')
    # merge_document_results produces joined imaging text without root proof.
    # A separately evidenced root finding is an independent occurrence even if
    # another section happens to contain the same wording.
    if root.get('reportType') == 'imaging' and isinstance(expansion, dict) and not root.get('expansionEvidence'):
        if all(type(section.get('sectionIndex')) is int for section in sections):
            for key in ('findings', 'diagnosis'):
                child_values = [
                    section['expansion'][key]
                    for section in sections
                    if isinstance(section.get('expansion'), dict) and section['expansion'].get(key)
                ]
                joined = '\n'.join(dict.fromkeys(value.strip() for value in child_values)) or None
                if expansion.get(key) == joined:
                    expansion[key] = None
    has_root_findings = (
        root.get('reportType') == 'imaging'
        and isinstance(expansion, dict)
        and any(expansion.get(key) for key in ('findings', 'diagnosis'))
    )
    # Metadata on a container is not an additional subreport. Callers preserve
    # that container's metadata separately while flattening its real sections.
    from .basic_info import has_basic_material

    keep_root = root['observations'] or root['unresolvedItems'] or has_root_findings or has_basic_material(root)
    return ([root] if keep_root else []) + sections


def merge_document_results(parts: list[dict]) -> dict:
    """Preserve dated subreports before constructing the compatible public view."""
    if len(parts) == 1:
        return parts[0]
    from copy import deepcopy

    def joined(values):
        return '\n'.join(dict.fromkeys(v.strip() for v in values if isinstance(v, str) and v.strip())) or None

    sections = []
    for part in parts:
        sections.extend(document_sections(part))
    observations = []
    identities = []
    unresolved = []
    unresolved_identities = []
    for index, section in enumerate(sections):
        section.pop('sections', None)
        section['sectionIndex'] = index
        for original in section.get('observations', []):
            item = deepcopy(original)
            # A source report date is retained as provenance, not guessed from another page.
            date_text = normalize_document_time(item.get('observedAt')) or normalize_document_time(
                section.get('reportTime')
            )
            if date_text:
                item['observedAt'] = date_text
            # Different dates, samples, values or methods survive. Full occurrences remain in sections.
            identity = deepcopy(item)
            identity.pop('sourceSection', None)
            proof = identity.get('evidence') or {}
            if proof.get('quote') and not proof.get('occurrenceKey'):
                # A repeated OCR block cannot prove that the same quote denotes
                # another physical measurement. Preserve every proof in sections,
                # but keep one compatible public occurrence for identical facts.
                for key in ('sourceId', 'evidence', 'metadataEvidence'):
                    identity.pop(key, None)
            item['sourceSection'] = index
            if identity not in identities:
                identities.append(identity)
                observations.append(item)
        for original in section.get('unresolvedItems', []):
            item = deepcopy(original)
            item.pop('sourceSection', None)
            if item not in unresolved_identities:
                unresolved_identities.append(deepcopy(item))
                item['sourceSection'] = index
                unresolved.append(item)
    report_type = next(
        (p['reportType'] for p in sections if p['reportType'] in {'checkup', 'microbiome'}),
        next((p['reportType'] for p in sections if p['reportType'] != 'unknown'), 'unknown'),
    )
    if report_type in {'lab', 'microbiome'}:
        expansion = deepcopy(observations)
    elif report_type == 'imaging':
        expansion = {
            key: joined(p['expansion'].get(key) for p in sections if isinstance(p.get('expansion'), dict))
            for key in ('findings', 'diagnosis')
        }
    elif report_type == 'checkup':
        expansion = {
            key: next(
                (
                    p['expansion'].get(key)
                    for p in sections
                    if isinstance(p.get('expansion'), dict) and p['expansion'].get(key)
                ),
                None,
            )
            for key in ('checkupOrgName', 'checkupTime')
        }
    else:
        expansion = None
    statuses = {p['status'] for p in sections}
    result = {
        'reportType': report_type,
        'title': next((p.get('title') for p in sections if p.get('title')), None),
        'reportTime': merge_document_times(p.get('reportTime') for p in sections),
        'aiSummary': joined(p.get('aiSummary') for p in sections),
        'status': 'abnormal' if 'abnormal' in statuses else 'normal' if 'normal' in statuses else 'pending',
        'expansion': expansion,
        'observations': observations,
        'unresolvedItems': unresolved,
        'sections': sections,
    }
    proofs = {}
    containers = [part for part in parts if part.get('sections') and part.get('metadataEvidence')]
    titled = [part for part in containers if part.get('title') and part['metadataEvidence'].get('title')]
    if titled:
        result['title'] = titled[0]['title']
    container_dates = [
        part['reportTime']
        for part in containers
        if part.get('reportTime') and part['metadataEvidence'].get('reportTime')
    ]
    if container_dates:
        result['reportTime'] = merge_document_times([*(p.get('reportTime') for p in sections), *container_dates])
    for key in ('title', 'reportTime'):
        for section in [*containers, *sections]:
            proof = (section.get('metadataEvidence') or {}).get(key)
            if proof and section.get(key) == result[key]:
                proofs[key] = deepcopy(proof)
                break
    if proofs:
        result['metadataEvidence'] = proofs
    from .basic_info import basic_info_projections
    from .basic_info import merge_basic_info

    merge_basic_info(basic_info_projections(parts), result)
    if 'basicInfoMeta' in result and any(part.get('_basicInfoMerged') is True for part in parts):
        result['_basicInfoMerged'] = True
    return result
