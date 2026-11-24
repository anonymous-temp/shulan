"""Field-level report demographics: AI judgments with fixed-source proof validation."""

import json
import re
from copy import deepcopy
from datetime import datetime
from math import isclose
from math import isfinite

from .extraction import normalize_document_time

FIELDS = ('name', 'gender', 'age')
REPORT_RELIABILITY = frozenset({'low', 'medium'})
BASIC_INFO_CONTRACT = 'analysis.basic-info.v1'
STATUSES = frozenset({'extracted', 'not_found', 'unresolved', 'conflicting'})


def empty_basic_info(*, status='unresolved', reason='基础信息尚未完成核查。'):
    return {key: None for key in FIELDS}, {
        key: {
            'status': status,
            'sourceReliability': None,
            'observedAt': None,
            'reason': reason,
            'evidence': None,
            'subjectRef': None,
            'subjectEvidence': None,
            'subjectStatus': 'unknown',
            'candidates': [],
        }
        for key in FIELDS
    }


def basic_info_projections(parts):
    """Copy every root/section demographic judgment without carrying clinical data."""
    projected = []

    def collect(node):
        if not isinstance(node, dict):
            return
        metadata = node.get('basicInfoMeta')
        if isinstance(metadata, dict):
            projected.append({'basicInfo': deepcopy(node.get('basicInfo', {})), 'basicInfoMeta': deepcopy(metadata)})
            if node.get('_basicInfoMerged') is True:
                # This root was derived by the program from these same children.
                # Original independent roots/sections never receive this marker.
                return
        children = node.get('sections', [])
        if isinstance(children, list):
            for child in children:
                collect(child)

    for part in parts:
        collect(part)
    return projected


def has_basic_info(node):
    return any(node.get('basicInfo', {}).get(key) is not None for key in FIELDS)


def has_basic_material(node):
    return has_basic_info(node) or any(
        meta.get('candidates') for meta in node.get('basicInfoMeta', {}).values() if isinstance(meta, dict)
    )


def _value(field, value):
    if field == 'age':
        return value if type(value) in (int, float) and isfinite(value) and 0 <= value <= 150 else None
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    return value if field == 'name' or value in ('男', '女') else None


_TIME_COMPONENTS = r'(?:[0-9]{2}(?::[0-9]{2}){0,2}|[0-9]{4}(?:[0-9]{2})?)'
_OBSERVED_AT = re.compile(
    rf'(?P<year>[0-9]{{4}})(?P<date_separator>-?)(?P<month>[0-9]{{2}})(?P=date_separator)(?P<day>[0-9]{{2}})'
    rf'(?:[T ](?P<clock>{_TIME_COMPONENTS})(?P<fraction>[.,][0-9]+)?'
    rf'(?:(?P<utc>Z)|(?P<sign>[+-])(?P<zone>{_TIME_COMPONENTS})(?P<zone_fraction>[.,][0-9]+)?)?)?'
)


def _observed_at(value):
    """Normalize calendar syntax while preserving the source clock precision."""
    if not isinstance(value, str):
        return None

    match = _OBSERVED_AT.fullmatch(value.strip())
    if match is None:
        # The shared OCR formatter preserves the original offset digits.
        normalized = normalize_document_time(value)
        match = _OBSERVED_AT.fullmatch(normalized) if normalized else None
        if match is None:
            return None

    timestamp = f'{match["year"]}-{match["month"]}-{match["day"]}'
    if match['clock']:
        clock = ':'.join(re.findall(r'[0-9]{2}', match['clock']))
        fraction = (match['fraction'] or '').replace(',', '.')
        timestamp += f'T{clock}{fraction}'

    if match['utc']:
        timestamp += '+00:00'
    elif match['zone']:
        zone = re.findall(r'[0-9]{2}', match['zone'])
        # fromisoformat carries overflow in offset components. Reject it before parsing.
        if int(zone[0]) > 23 or any(int(part) > 59 for part in zone[1:]):
            return None
        fraction = (match['zone_fraction'] or '').replace(',', '.')
        timestamp += match['sign'] + ':'.join(zone) + fraction

    try:
        datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return timestamp


def normalize_basic_info_times(result) -> bool:
    """Normalize existing demographic times in an in-flight result and its sections."""
    if not isinstance(result, dict):
        return False
    changed = False
    metadata = result.get('basicInfoMeta')
    if isinstance(metadata, dict):
        for field in FIELDS:
            item = metadata.get(field)
            if isinstance(item, dict) and 'observedAt' in item:
                observed_at = _observed_at(item['observedAt'])
                if observed_at != item['observedAt']:
                    item['observedAt'] = observed_at
                    changed = True
    sections = result.get('sections')
    if isinstance(sections, list):
        for section in sections:
            changed |= normalize_basic_info_times(section)
    return changed


def _proof(value, text, offset):
    if not isinstance(value, dict) or not isinstance(value.get('quote'), str) or not value['quote'].strip():
        return None
    quote = value['quote']
    if not isinstance(text, str):
        return None
    start, end = value.get('start'), value.get('end')
    if start is not None or end is not None:
        if type(start) is not int or type(end) is not int:
            return None
        local = start - offset
        if local < 0 or end - start != len(quote) or text[local : local + len(quote)] != quote:
            return None
    else:
        local = text.find(quote)
        if local < 0:
            return None
        start, end = offset + local, offset + local + len(quote)
    return {'quote': quote, 'start': start, 'end': end}


_NUMBER = r'(?<![\d.])\d+(?:\.\d+)?(?![\d.])'
_YEAR_UNIT = r'(?:周岁|岁|歲|years?|yrs?)(?![A-Za-z])'
_MONTH_UNIT = r'(?:个月|個月|月龄|月齡|月|months?|mos?)(?![A-Za-z])'
_DAY_UNIT = r'(?:天|日龄|日齡|days?)(?![A-Za-z])'
_AGE_QUANTITY = re.compile(
    rf'(?<![\d.])(?:'
    rf'(?P<years>\d+(?:\.\d+)?)\s*{_YEAR_UNIT}'
    rf'(?:\s*(?:零|又|and|,|，)?\s*(?P<extra_months>\d+(?:\.\d+)?)\s*{_MONTH_UNIT})?'
    rf'|(?P<months>\d+(?:\.\d+)?)\s*{_MONTH_UNIT}'
    rf'|(?P<days>\d+(?:\.\d+)?)\s*{_DAY_UNIT})',
    re.IGNORECASE,
)
_MONTH_AGE_PREFIX = re.compile(
    r'(?:月龄|月齡|age\s*(?:\(\s*months?\s*\)|in\s+months?))\s*[:：]?\s*(\d+(?:\.\d+)?)',
    re.IGNORECASE,
)
_GENDER_ENUM = re.compile(
    r'(?:\bsex\b|\bgender\b|性别|性別)\s*[:：=]?\s*(female|male|f|m)\b'
    r'(?!\s*[/、,|]\s*(?:female|male|f|m)\b)',
    re.IGNORECASE,
)


def _contains_age(value, quote):
    # Validate explicit unit normalization, leaving patient and confidence judgments to AI.
    consumed, supported = [], []
    for match in _MONTH_AGE_PREFIX.finditer(quote):
        consumed.append(match.span())
        supported.append(float(match[1]) / 12)
    for match in _AGE_QUANTITY.finditer(quote):
        if any(start <= match.start() < end for start, end in consumed):
            continue
        consumed.append(match.span())
        if match['days'] is not None:
            # A day count has no exact year conversion without a calendar basis.
            continue
        years = float(match['years'] or 0)
        months = float(match['extra_months'] or match['months'] or 0)
        supported.append(years + months / 12)
    for match in re.finditer(_NUMBER, quote):
        if not any(start <= match.start() < end for start, end in consumed):
            supported.append(float(match[0]))
    return any(isclose(item, value, rel_tol=1e-9, abs_tol=1e-9) for item in supported)


def _contains_value(field, value, quote):
    if field == 'age':
        return _contains_age(value, quote)
    if field == 'gender' and value not in quote:
        expected = {'男': {'male', 'm'}, '女': {'female', 'f'}}[value]
        return any(match[1].lower() in expected for match in _GENDER_ENUM.finditer(quote))
    return str(value) in quote


def validate_basic_info(raw, *, text, offset=0):
    """Validate allowed judgments and quotes; never replace AI's semantic grading."""
    values, metadata = empty_basic_info()
    issues = []
    incoming = raw.get('basicInfo') if isinstance(raw, dict) else None
    incoming_meta = raw.get('basicInfoMeta') if isinstance(raw, dict) else None
    if not isinstance(incoming, dict) or not isinstance(incoming_meta, dict):
        return (
            values,
            metadata,
            [
                {
                    'code': 'invalid_json_schema',
                    'path': '$.basicInfo',
                    'expected': 'basicInfo and per-field basicInfoMeta objects',
                    'sourceIds': [],
                    'recoverable': True,
                    'severity': 'core',
                }
            ],
        )
    for field in FIELDS:
        item = incoming_meta.get(field)
        if not isinstance(item, dict):
            issues.append(
                {
                    'code': 'invalid_json_schema',
                    'path': f'$.basicInfoMeta.{field}',
                    'expected': 'field metadata',
                    'sourceIds': [],
                    'recoverable': True,
                }
            )
            continue
        meta = metadata[field]
        status = item.get('status')
        reason = item.get('reason')
        proof = _proof(item.get('evidence'), text, offset)
        subject_proof = _proof(item.get('subjectEvidence'), text, offset)
        subject_ref = item.get('subjectRef')
        subject_status = item.get('subjectStatus', 'unknown')
        if subject_status not in ('single', 'ambiguous', 'unknown'):
            subject_status = 'unknown'
        if not isinstance(subject_ref, str) or not subject_ref.strip():
            subject_ref = None
        elif not subject_proof or subject_ref not in subject_proof['quote']:
            subject_ref = None
        else:
            subject_ref = subject_ref.strip()
        candidates = []
        pending = item.get('candidates', [])
        rejected_candidates = not isinstance(pending, list) or len(pending) > 100
        if isinstance(pending, list):
            for candidate in pending[:100]:
                if not isinstance(candidate, dict):
                    rejected_candidates = True
                    continue
                candidate_proof = _proof(candidate.get('evidence'), text, offset)
                candidate_value = _value(field, candidate.get('value'))
                grade = candidate.get('sourceReliability')
                if (
                    candidate_proof is None
                    or candidate_value is None
                    or not isinstance(grade, str)
                    or grade not in REPORT_RELIABILITY
                    or not _contains_value(field, candidate_value, candidate_proof['quote'])
                ):
                    rejected_candidates = True
                    continue
                candidates.append(
                    {
                        'value': candidate_value,
                        'evidence': candidate_proof,
                        'sourceReliability': grade,
                        'reason': candidate['reason'][:2000] if isinstance(candidate.get('reason'), str) else '',
                        'subjectRef': candidate.get('subjectRef')
                        if isinstance(candidate.get('subjectRef'), str)
                        else None,
                    }
                )
        meta.update(
            reason=reason.strip()[:2000] if isinstance(reason, str) and reason.strip() else '原文依据不足。',
            evidence=proof,
            subjectEvidence=subject_proof,
            subjectRef=subject_ref,
            subjectStatus=subject_status,
            candidates=candidates,
        )
        if rejected_candidates:
            suffix = '部分候选未通过规范校验，原始候选保留于抽取留痕。'
            meta['reason'] = meta['reason'][: 2000 - len(suffix)] + suffix
            if status in ('not_found', 'extracted'):
                status = 'unresolved'
        meta['observedAt'] = _observed_at(item.get('observedAt'))
        value = _value(field, incoming.get(field))
        if status == 'not_found' and incoming.get(field) is None and not candidates and proof is None:
            meta.update(status='not_found', reason=meta['reason'])
            continue
        if status in ('conflicting', 'unresolved'):
            meta['status'] = status
            continue
        valid = (
            status == 'extracted'
            and value is not None
            and isinstance(item.get('sourceReliability'), str)
            and item['sourceReliability'] in REPORT_RELIABILITY
            and isinstance(reason, str)
            and bool(reason.strip())
            and proof is not None
            and subject_status == 'single'
            and _contains_value(field, value, proof['quote'])
        )
        candidate_values = {candidate['value'] for candidate in candidates}
        if valid:
            candidate_values.add(value)
        if len(candidate_values) > 1 or subject_status == 'ambiguous':
            if valid:
                candidates.append(
                    {
                        'value': value,
                        'evidence': proof,
                        'sourceReliability': item['sourceReliability'],
                        'reason': meta['reason'],
                        'subjectRef': subject_ref,
                    }
                )
            meta['status'] = 'conflicting'
            continue
        if valid:
            values[field] = value
            meta.update(status='extracted', sourceReliability=item['sourceReliability'], candidates=[])
        else:
            meta.update(status='unresolved', reason='字段、允许等级、受检者状态或原文证据未通过校验。')
            issues.append(
                {
                    'code': 'source_evidence_mismatch',
                    'path': f'$.basicInfo.{field}',
                    'sourceIds': [],
                    'recoverable': True,
                }
            )
    return values, metadata, issues


def attach_basic_info(validated, raw, *, text, offset=0):
    values, metadata, issues = validate_basic_info(raw, text=text, offset=offset)
    validated['basicInfo'], validated['basicInfoMeta'] = values, metadata
    raw_sections = raw.get('sections', []) if isinstance(raw, dict) else []
    if not isinstance(raw_sections, list):
        raw_sections = []
    for child, original in zip(validated.get('sections', []), raw_sections):
        if isinstance(original, dict):
            issues.extend(attach_basic_info(child, original, text=text, offset=offset))
    return issues


def _mergeable_row(field, value, meta):
    """Read only proof-validated values; pending evidence never becomes a selection."""
    proof = meta.get('evidence')
    grade = meta.get('sourceReliability')
    return (
        _value(field, value) is not None
        and isinstance(grade, str)
        and grade in REPORT_RELIABILITY
        and isinstance(proof, dict)
        and isinstance(proof.get('quote'), str)
        and bool(proof['quote'].strip())
        and _contains_value(field, value, proof['quote'])
    )


def _field_rows(part, field):
    meta = part.get('basicInfoMeta', {}).get(field, {})
    if not isinstance(meta, dict):
        return []
    rows = []
    value = part.get('basicInfo', {}).get(field)
    if meta.get('status') == 'extracted' and _mergeable_row(field, value, meta):
        rows.append({'value': value, **deepcopy(meta), '_selected': True})
    for candidate in meta.get('candidates', []):
        if isinstance(candidate, dict) and _mergeable_row(field, candidate.get('value'), candidate):
            rows.append(
                {
                    **deepcopy(meta),
                    **deepcopy(candidate),
                    'subjectRef': candidate.get('subjectRef') or meta.get('subjectRef'),
                    '_selected': False,
                }
            )
    return rows


def _pending_rows(rows):
    result, seen = [], set()
    for row in rows:
        candidate = {
            key: deepcopy(row.get(key)) for key in ('value', 'evidence', 'sourceReliability', 'reason', 'subjectRef')
        }
        key = json.dumps(candidate, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


def _merge_basic_parts(parts, target, *, same_source=False):
    present, seen = [], set()
    for part in parts:
        if 'basicInfoMeta' not in part:
            continue
        key = json.dumps(
            {field: part.get(field) for field in ('basicInfo', 'basicInfoMeta')},
            sort_keys=True,
            ensure_ascii=False,
        )
        if key not in seen:
            seen.add(key)
            present.append(part)
    if not present:
        return target
    values, metadata = empty_basic_info()
    sources = [{field: _field_rows(part, field) for field in FIELDS} for part in present]
    sources = [source for source in sources if any(source.values())]
    rows = [row for source in sources for field_rows in source.values() for row in field_rows]
    references = {row['subjectRef'] for row in rows if row.get('subjectRef')}
    # Sections that repeat supported selected values need no separate patient
    # identifier. Distinct unanchored sections still cannot supply a composite identity.
    selected_fields = [
        {(field, row['value']) for field in FIELDS for row in source[field] if row['_selected']} for source in sources
    ]
    unanchored = [
        fields
        for source, fields in zip(sources, selected_fields, strict=True)
        if any(row['_selected'] and not row['subjectRef'] for values in source.values() for row in values)
    ]
    unsafe_subject = (
        any(
            isinstance(meta, dict) and meta.get('subjectStatus') == 'ambiguous'
            for part in present
            for meta in part.get('basicInfoMeta', {}).values()
        )
        or len(references) > 1
        or not same_source
        and len(sources) > 1
        and any(not any(fields <= other for other in selected_fields if other is not fields) for fields in unanchored)
    )
    for field in FIELDS:
        rows = [row for source in sources for row in source[field]]
        selected = [row for row in rows if row['_selected']]
        distinct_values = {row['value'] for row in rows}
        if selected and not unsafe_subject and len(distinct_values) == 1:
            # Only an AI-extracted value can supply the current value or its grade.
            chosen = next((row for row in selected if row['sourceReliability'] == 'medium'), selected[0])
            values[field] = chosen['value']
            metadata[field] = {
                key: deepcopy(value) for key, value in chosen.items() if key not in {'value', '_selected'}
            }
            continue
        metas = [part.get('basicInfoMeta', {}).get(field, {}) for part in present]
        if rows and (unsafe_subject or len(distinct_values) > 1):
            subject_ref = next(iter(references)) if not unsafe_subject and len(references) == 1 else None
            subject_evidence = next(
                (row.get('subjectEvidence') for row in rows if row.get('subjectRef') == subject_ref), None
            )
            metadata[field].update(
                status='conflicting',
                reason='受检者归属或字段候选不能唯一合并。',
                subjectStatus='ambiguous' if unsafe_subject else 'single',
                subjectRef=subject_ref,
                subjectEvidence=deepcopy(subject_evidence) if subject_ref else None,
                candidates=_pending_rows(rows),
            )
        else:
            # Keep the explicit patient judgment even when this field has no usable value.
            chosen = next((item for item in metas if item.get('subjectStatus') == 'ambiguous'), None)
            if chosen is None:
                chosen = next((item for item in metas if item.get('status') in ('conflicting', 'unresolved')), metas[0])
            metadata[field] = deepcopy(chosen) if chosen else metadata[field]
            if rows:
                # A sole unresolved candidate remains unresolved; do not infer extraction.
                metadata[field]['candidates'] = _pending_rows(rows)
    target['basicInfo'], target['basicInfoMeta'] = values, metadata
    return target


def merge_basic_info(parts, target):
    """Combine every validated selected/pending value without splicing unproven people."""
    return _merge_basic_parts(parts, target)


def merge_basic_check(previous, checked):
    """Merge demographic evidence from this same source while keeping all first-stage clinical data."""
    return _merge_basic_parts([previous, checked], deepcopy(previous), same_source=True)
