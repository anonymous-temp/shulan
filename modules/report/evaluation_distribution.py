"""Private, source-bound distributions for report state paragraphs.

Callers establish observation binding and grading provenance. This module
counts those observations; it neither binds by a similar name nor calculates
a grade, diagnosis, detection result, or physiological capacity.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
import json
import hashlib
import re
import unicodedata


_SOURCE_STATE_SEAL = object()


@dataclass(frozen=True, slots=True)
class _SourceStateAuthority:
    """Ephemeral source export, never a DTO flag or a meaning approval."""

    path: tuple
    text: str
    fact_ids: tuple[str, ...]
    distribution_json: str
    input_signature: str
    data_identity: int
    document_identity: int
    complete: bool
    _seal: object = field(default=None, repr=False, compare=False)

    @property
    def distribution(self):
        return json.loads(self.distribution_json)


def _mint_source_state_authority(*, path, text, fact_ids, distribution,
                                 input_signature, data, document):
    authority = _SourceStateAuthority(tuple(path), text, tuple(fact_ids),
        json.dumps(distribution, ensure_ascii=False, sort_keys=True), input_signature,
        id(data), id(document), not bool(distribution.get('unresolvedMembers')))
    return replace(authority, _seal=(_SOURCE_STATE_SEAL, _authority_content_digest(authority)))


def _authority_content_digest(authority):
    return hashlib.sha256(json.dumps((authority.path, authority.text, authority.fact_ids,
        authority.distribution_json, authority.input_signature, authority.data_identity,
        authority.document_identity, authority.complete), ensure_ascii=False).encode()).hexdigest()


def _source_state_sealed(authority):
    return (isinstance(authority, _SourceStateAuthority) and isinstance(authority._seal, tuple)
        and len(authority._seal) == 2 and authority._seal[0] is _SOURCE_STATE_SEAL
        and authority._seal[1] == _authority_content_digest(authority))


def _normalized(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value))) if value is not None else None


def _literal(value):
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return 'true' if value else 'false'
    text = str(value).strip()
    return None if text.casefold() in {'', '-', '--', 'null', 'none', 'n/a', 'na', '未提供', '无数据'} else text


def _number(value):
    if isinstance(value, bool) or not re.fullmatch(r'[-+]?\d+(?:\.\d+)?', str(value).strip()):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _measurement_ids(row, facts):
    # Inventory's summaryFactIds also contains aggregate/direction facts.
    # The single bound measurement always wins over that combined support set.
    fact = row.get('_summaryFact')
    ids = (row.get('_summaryMeasurementFactIds') or ([fact.fact_id] if fact is not None else [])
        or row.get('_stateFactIds') or row.get('measurementFactIds') or [])
    return tuple(dict.fromkeys(fid for fid in ids if fid in facts))


def build_source_distribution(rows, facts, *, native_context=(), group_name=None,
                              measurement_kind='function'):
    """Return a private frame from already verified rows without mutating input.

    ``_stateFactIds`` or ``_summaryFact`` is a caller's binding receipt.
    Source labels, proven display grades and native arrows are independent
    axes. Unbound supplied rows remain in coverage, never in a normal count.
    """
    members, unresolved, seen, context_rows = [], [], {}, []
    rows = list(rows or ())
    contexts = [*(native_context or ()), *(context for row in rows if isinstance(row, dict)
        for context in row.get('_summaryNativeContexts') or ())]
    contextual_ids = {fid for context in contexts for fid in context.get('factIds') or ()}
    contextual_ids.update(fid for context in contexts
        for fid in [*(context.get('aggregateFactIds') or ()), *(context.get('directionFactIds') or ())])
    for row in rows:
        if not isinstance(row, dict) or _literal(row.get('value')) is None:
            continue
        ids = _measurement_ids(row, facts)
        if not ids:
            unresolved.append({'code': row.get('code'), 'name': row.get('name'), 'value': deepcopy(row.get('value'))})
            continue
        bound = [facts[fid] for fid in ids]
        # A caller cannot use a receipt for another value, unit or source label.
        if any(_literal(fact.value) != _literal(row.get('value'))
                or _normalized(fact.unit) != _normalized(row.get('unit'))
                or _normalized(fact.status) != _normalized(row.get('sourceStatus')) for fact in bound):
            unresolved.append({'code': row.get('code'), 'name': row.get('name'), 'value': deepcopy(row.get('value'))})
            continue
        if set(ids) <= contextual_ids:
            context_rows.append({'name': row.get('name'), 'value': deepcopy(row.get('value')),
                'sourceLabel': row.get('sourceStatus'), 'factIds': list(ids)})
            continue
        source_labels = list(dict.fromkeys(fact.status for fact in bound if fact.status is not None))
        source_label = source_labels[0] if len(source_labels) == 1 else None
        grade, origin = row.get('assessmentLevel'), row.get('_assessmentOrigin')
        if origin not in {'source', 'reference', 'report'}:
            grade, origin = row.get('_summaryLabel'), row.get('_summaryOrigin')
        if origin not in {'source', 'reference', 'report'} or origin == 'source' and grade != source_label:
            grade, origin = None, None
        own_contexts = [context for context in [*contexts, *(row.get('_summaryNativeContexts') or ())]
            if any(set(member.get('factIds') or ()).intersection(ids) for member in context.get('members') or ())]
        direction_ids, aggregate_ids, directions, panels = [], [], [], []
        for context in own_contexts:
            direction_ids.extend(context.get('directionFactIds') or ())
            aggregate_ids.extend(context.get('aggregateFactIds') or ())
            panels.append((context.get('panel'), tuple(context.get('aggregateFactIds') or ())))
            # Directions require the literal direction context fact, rather
            # than merely an aggregate abnormal label or a derived score.
            if not context.get('directionFactIds'):
                continue
            for member in context.get('members') or ():
                if set(member.get('factIds') or ()).intersection(ids):
                    directions.extend(member.get('directions') or ([member['direction']] if member.get('direction') else []))
        directions = sorted(set(directions).intersection({'up', 'down'}))
        sources = deepcopy(row.get('sources') or [])
        documents = sorted({match[1].lower() for source in sources if isinstance(source, dict)
            and (match := re.match(r'^source:([a-fA-F0-9]{64}):', str(source.get('recordId') or '')))})
        observations = sorted({(fact.source, fact.date) for fact in bound}, key=repr)
        refs = list(dict.fromkeys(fact.reference_range for fact in bound if fact.reference_range is not None))
        # A common unit alone does not establish a common scale. Native panel
        # identity or an explicit same-reference grading receipt is required.
        scope = (tuple(sorted(set(panels), key=repr)), tuple(documents), tuple(observations))
        proof = ('native', scope, tuple(_normalized(ref) for ref in refs)) if panels and row.get('unit') in {'分', 'score', 'points'} else (
            ('reference', tuple(refs), scope) if refs and origin == 'reference' else None)
        if not documents and not panels and any(date is None for _, date in observations):
            proof = None
        scale = (measurement_kind, _normalized(row.get('unit')), proof) if proof else None
        member = {'code': row.get('code'), 'name': row.get('name') or bound[0].name,
            'measurementType': row.get('_distributionObjectType') or measurement_kind,
            'value': deepcopy(row.get('value')), 'unit': row.get('unit'),
            'referenceRange': row.get('referenceRange'), 'sourceLabel': source_label,
            'displayGrade': grade, 'displayGradeOrigin': origin, 'directions': directions,
            'measurementFactIds': list(ids), 'contextFactIds': list(dict.fromkeys([*aggregate_ids, *direction_ids])),
            'aggregateFactIds': list(dict.fromkeys(aggregate_ids)),
            'directionFactIds': list(dict.fromkeys(direction_ids)), 'sources': sources,
            'sourceDocuments': documents, 'sourceObservations': observations, 'scaleKey': scale}
        # Merge actual mirrors only: a shared context fact never identifies a
        # measurement, and two conflicting fields keep separate observations.
        fingerprint = (_literal(row.get('value')), _normalized(row.get('unit')),
            _normalized(row.get('referenceRange')), source_label, grade, origin,
            tuple(directions), tuple(documents), tuple(observations))
        mirror = next((index for fid in ids for index in seen.get(fid, ())
            if members[index]['_fingerprint'] == fingerprint), None)
        if mirror is not None:
            existing = members[mirror]
            existing['measurementFactIds'] = list(dict.fromkeys([*existing['measurementFactIds'], *ids]))
            existing['aliases'] = list(dict.fromkeys([*existing.get('aliases', [existing['name']]), member['name']]))
            for source in sources:
                if source not in existing['sources']:
                    existing['sources'].append(source)
            continue
        member['_fingerprint'] = fingerprint
        for fid in ids:
            seen.setdefault(fid, []).append(len(members))
        members.append(member)

    aggregates = []
    used_aggregate_ids = {fid for member in members for fid in member['aggregateFactIds']}
    used_aggregate_ids.update(fid for row in context_rows for fid in row['factIds'])
    for context in contexts + [context for row in rows or () if isinstance(row, dict)
            for context in row.get('_summaryNativeContexts') or ()]:
        ids = context.get('aggregateFactIds') or []
        if ids and set(ids).intersection(used_aggregate_ids) and not any(item['factIds'] == ids for item in aggregates):
            aggregates.append({'name': context.get('name'), 'sourceLabel': context.get('sourceStatus'),
                'panel': context.get('panel'), 'factIds': list(ids)})
    minimum = None
    numeric = [_number(member['value']) for member in members]
    scales = {repr(member['scaleKey']) for member in members}
    if (members and measurement_kind not in {'structure', 'microbialHealth'}
            and len({member['measurementType'] for member in members}) == 1
            and all(number is not None for number in numeric)
            and len(scales) == 1 and members[0]['scaleKey'] is not None):
        lowest = min(numeric)
        winners = [member for member, number in zip(members, numeric) if number == lowest]
        minimum = {'value': winners[0]['value'], 'unit': winners[0]['unit'],
            'names': [member['name'] for member in winners],
            'measurementFactIds': list(dict.fromkeys(fid for member in winners for fid in member['measurementFactIds']))}
    for member in members:
        member.pop('_fingerprint', None)
    components = {}
    for member in members:
        component = components.setdefault((member['measurementType'], repr(member['scaleKey'])),
            {'measurementType': member['measurementType'], 'scaleKey': member['scaleKey'], 'members': []})
        component['members'].append(member)
    for component in components.values():
        own = component['members']
        component.update(measuredCount=len(own),
            sourceLabelCounts=dict(Counter(member['sourceLabel'] for member in own if member['sourceLabel'] is not None)),
            displayGradeCounts=dict(Counter(member['displayGrade'] for member in own if member['displayGrade'] is not None)),
            ungradedCount=sum(member['displayGrade'] is None for member in own))
    measurement_ids = list(dict.fromkeys(fid for member in members for fid in member['measurementFactIds']))
    context_ids = list(dict.fromkeys([*(fid for member in members for fid in member['contextFactIds']),
        *(fid for row in context_rows for fid in row['factIds'])]))
    return {'name': group_name, 'measurementKind': measurement_kind, 'members': members,
        'aggregates': aggregates, 'measuredCount': len(members), 'suppliedCount': len(members) + len(unresolved),
        'contextOnlyRows': context_rows, 'contextRowCount': len(context_rows),
        'measurementGroups': list(components.values()),
        'unresolvedMembers': unresolved, 'sourceLabelCounts': dict(Counter(member['sourceLabel'] for member in members
            if member['sourceLabel'] is not None)),
        'displayGradeCounts': dict(Counter(member['displayGrade'] for member in members if member['displayGrade'] is not None)),
        'ungradedCount': sum(member['displayGrade'] is None for member in members),
        'directionCounts': {direction: sum(direction in member['directions'] for member in members) for direction in ('up', 'down')},
        'directionConflictCount': sum(len(member['directions']) > 1 for member in members),
        'measurementFactIds': measurement_ids, 'contextFactIds': context_ids,
        'factIds': list(dict.fromkeys([*measurement_ids, *context_ids])), 'minimum': minimum}


def render_source_distribution(frame, *, include_minimum=True):
    """Render the full distribution with bounded examples, not a second table."""
    members = frame.get('members') or []
    if not members:
        return None

    def value_text(member):
        value = _literal(member.get('value'))
        return str(member['name']) + ((' ' + value) if value else '') + (member.get('unit') or '')

    def examples(rows, *, limit, values=False):
        selected = rows[:limit]
        text = '、'.join(value_text(row) if values else str(row['name']) for row in selected)
        return text + ('等' if len(rows) > limit else '')

    statements = []
    for aggregate in frame.get('aggregates') or ():
        if aggregate.get('name') and aggregate.get('sourceLabel'):
            statements.append(f'原报告“{aggregate["name"]}”汇总标记为{aggregate["sourceLabel"]}。')
    types = {}
    for member in members:
        types.setdefault(member.get('measurementType') or frame.get('measurementKind'), []).append(member)
    for measurement_type, own in types.items():
        # Labels classify the original observation. They are not independent
        # biological conclusions about a patient's organism or host function.
        groups = {}
        for member in own:
            groups.setdefault((member.get('displayGrade'), member.get('sourceLabel')), []).append(member)
        clauses = []
        for (grade, source), rows in groups.items():
            label = grade if grade is not None else source
            if len(own) == 1:
                subject = value_text(rows[0])
                if _literal(rows[0].get('value')) == _literal(label):
                    subject = str(rows[0]['name'])
                phrase = subject + (f'，分级为{grade}' if grade is not None else f'，原标记为{source}' if source is not None else '')
            else:
                sample = examples(rows, limit=2 if label in {'正常', '丰富', '未检出', '阴性'} else 3,
                    values=len(rows) <= 3)
                phrase = (f'{len(rows)}项{label}（{sample}）' if label is not None
                    else f'{len(rows)}项为{sample}')
            if grade is not None and source is not None and grade != source:
                phrase += f'，原标记为{source}'
            clauses.append(phrase)
        if len(own) == 1:
            body = clauses[0]
        else:
            body = f'已测{len(own)}项中，' + '；'.join(clauses)
        if measurement_type in {'function', '菌群功能测量'}:
            prefix = ('菌群功能评分' if all(row.get('unit') in {'分', 'score', 'points'}
                for row in own) else '菌群功能估计')
            body = prefix + '：' + body
        elif len(types) > 1 or measurement_type in {'耐药基因检测', '原报告基因标记条目'}:
            body = str(measurement_type) + '：' + body
        statements.append(body + '。')
    directions = {}
    for member in members:
        if member.get('directions'):
            directions.setdefault(tuple(member['directions']), []).append(member)
    arrow_clauses = []
    for axis, rows in directions.items():
        arrow = '↑与↓并存' if len(axis) > 1 else '↑' if axis == ('up',) else '↓'
        subject = examples(rows, limit=3) if len(rows) <= 3 else f'{len(rows)}项（' + examples(rows, limit=2) + '）'
        arrow_clauses.append(subject + arrow)
    if arrow_clauses:
        statements.append('原报告方向标记：' + '；'.join(arrow_clauses) + '。')
    minimum = frame.get('minimum')
    winners = [member for member in members if minimum and
        set(member['measurementFactIds']).intersection(minimum['measurementFactIds'])]
    useful_minimum = any(member.get('displayGrade') in {'极低', '偏低', '弱', '较弱', '合成能力弱'}
        or member.get('displayGrade') is None and 'down' in member.get('directions', []) for member in winners)
    if include_minimum and minimum and len(members) > 1 and useful_minimum:
        statements.append('同量表数值中，' + '、'.join(minimum['names'])
            + f'最低（{_literal(minimum["value"])}{minimum.get("unit") or ""}）。')
    return ''.join(statements)
