"""Complete private clinical observations, separate from reader display choices."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy

from .clinical_source_projection import PATIENT_ROLES
from .clinical_source_projection import ensure_patient_projection_index
from .clinical_source_projection import get_patient_projection
from .clinical_source_projection import get_patient_rows
from .clinical_source_projection import valid_source_span

_MEASURED_ROLES = frozenset({'direct_measurement', 'derived_measurement', 'exam_finding', 'physical_finding'})
_CONDITION = re.compile(
    r'(?P<non_fasting>非空腹|未空腹|(?<![A-Za-z])(?:non[-‐‑–—\s]*fasting|not\s+fasting)(?![A-Za-z]))|'
    r'(?P<postprandial>餐后(?:\s*(?:\d+(?:\.\d+)?|[一二两三四五六七八九十半]+)\s*(?:小时|[hH]))?'
    r'|(?<![A-Za-z])post[-‐‑–—\s]*prandial(?![A-Za-z]))|'
    r'(?P<fasting>空腹|(?<![A-Za-z])(?:fasting|FPG|FINS)(?![A-Za-z]))',
    re.I,
)
_CONDITION_QUESTION = re.compile(
    r'是否|[?？]|\b(?:whether|requirements?|required|requires?|recommended)\b|'
    r'(?:状态|条件|要求|情况)\s*$|\b(?:status|conditions?)\s*[:：]?\s*$',
    re.I,
)
_COLLECTION_LABEL = re.compile(
    r'(?:(?:本次)?(?:采血状态|采样状态|采集条件|采血条件|采样条件|饮食状态)|'
    r'(?<![A-Za-z])(?:collection|sampling|blood\s*(?:draw|sampling))\s+(?:status|condition))\s*[:：]\s*',
    re.I,
)


def _get(value, *names, default=None):
    for name in names:
        result = value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
        if result is not None:
            return result
    return default


def _record_id(fact):
    return _get(fact, 'fact_id', 'factId', 'recordId')


def _owned_rows(fact, projection_index):
    fid = _record_id(fact)
    rows = []
    for row in get_patient_rows(fact, projection_index=projection_index, include_claims=True):
        if row.get('ownRecordId') != fid:
            continue
        spans = row.get('sourceSpans') or []
        if not spans or not all(valid_source_span(fact, span) for span in spans):
            continue
        if row.get('role') not in PATIENT_ROLES | {'summary_patient_claim'}:
            continue
        rows.append(row)
    return rows


def _acquisition_context(projection, row=None):
    context = deepcopy(projection.get('sourceContext') or {})
    result = {
        key: context[key]
        for key in ('sourceSha256', 'documentId', 'sourcePage', 'method', 'specimen', 'subject', 'sourceReportedAge')
        if context.get(key) is not None
    }
    observed = row.get('observedAt') if row is not None else context.get('examDate')
    if observed is not None:
        result['observedAt'] = observed
    if context.get('reportDate') is not None:
        result['reportDate'] = context['reportDate']
    return result


def _acquisition_conditions(fact, projection, rows):
    """Admit an item's own printed condition, never a code or another answer."""
    acquired_roles = _MEASURED_ROLES - {'derived_measurement'}
    if not rows or not any(row.get('role') in acquired_roles for row in rows):
        return []
    fid = _record_id(fact)
    name, value = str(_get(fact, 'name', default='')), str(_get(fact, 'value', default=''))
    candidates = []
    compound = len(projection.get('patientRows', ())) > 1 and bool(re.search(r'与|和|及|、|/|&|\band\b', name, re.I))
    if not compound and not _CONDITION_QUESTION.search(name):
        candidates.extend(('name', match, 0) for match in _CONDITION.finditer(name))
    if compound:
        # A compound title cannot lend its first item's condition to every
        # other item. A child's own admitted literal clause can state it.
        for row in rows:
            if row.get('role') not in acquired_roles:
                continue
            for span in row['sourceSpans']:
                if not _CONDITION_QUESTION.search(span['quote']):
                    candidates.extend(('value', match, span['start']) for match in _CONDITION.finditer(span['quote']))
    # A labelled condition in the same actual source record can qualify its
    # acquisition. General education and advice records have no measured row.
    for label in () if compound else _COLLECTION_LABEL.finditer(value):
        if match := _CONDITION.match(value, label.end()):
            excluded = [
                *projection.get('adviceSpans', ()),
                *(
                    span
                    for span in projection.get('contextSpans', ())
                    if span.get('role') in {'generic_explanation', 'medical_advice'}
                ),
            ]
            if not any(
                span.get('recordId') == fid
                and type(span.get('start')) is int
                and type(span.get('end')) is int
                and span['start'] < match.end()
                and label.start() < span['end']
                for span in excluded
            ):
                candidates.append(('value', match, 0))
    result = []
    for field, match, offset in candidates:
        original = name if field == 'name' else value
        start, end = offset + match.start(), offset + match.end()
        before = original[max(0, start - 32) : start].rstrip()
        after = original[end:]
        if re.search(r'(?:无需|无须|不需|不要求|否认|未|非|不|\bnot|\bno|\bfalse)$', before, re.I):
            continue
        if re.match(r'\s*(?:状态|status|condition)?\s*[:：=]\s*(?:否|不是|未|\bno\b|\bfalse\b)', after, re.I):
            continue
        condition = {
            'condition': match.lastgroup,
            'quote': match.group(),
            'recordId': fid,
            'sourceField': field,
            'start': start,
            'end': end,
            'scope': 'own_source_acquisition_only',
        }
        if condition not in result:
            result.append(condition)
    return result


def model_clinical_observation(fact, *, projection_index=None):
    """A compact source-level model view, with no legacy clinical code semantics."""
    projection = get_patient_projection(fact, projection_index=projection_index)
    if projection is None:
        return None
    rows = _owned_rows(fact, projection_index)
    roles = list(dict.fromkeys(row['role'] for row in rows))
    role = roles[0] if len(roles) == 1 else 'mixed_record' if roles else projection.get('role')
    scalar = len(rows) == 1 and rows[0]['role'] in {'direct_measurement', 'derived_measurement'}
    label = str(_get(fact, 'name', default=''))
    conditions = _acquisition_conditions(fact, projection, rows)
    value = (
        projection.get('patientValue')
        if rows and len(rows) == len(projection.get('patientRows', ()))
        else '；'.join(dict.fromkeys(str(row['value']) for row in rows)) or None
    )
    return {
        'label': label,
        'sourceLabel': label,
        'value': value,
        'unit': rows[0].get('unit') if scalar else None,
        'status': rows[0].get('status') if scalar else None,
        'role': role,
        'measurementType': 'reported_clinical_assessment' if role == 'summary_patient_claim' else role,
        'observationIds': [row['code'] for row in rows],
        'acquisitionContext': _acquisition_context(projection, rows[0] if scalar else None),
        'acquisitionConditions': conditions,
        'acquisitionConditionStatus': 'explicit_source_condition' if conditions else 'not_provided',
        # Do not resend a raw educational paragraph under a second key. The
        # immutable fact and full projection already preserve it privately.
        'literalSource': {'name': label},
    }


def _span_identity(span):
    return span.get('recordId'), span.get('start'), span.get('end'), span.get('quote')


def _view_memberships(view):
    groups, codes, spans = {}, defaultdict(list), defaultdict(list)

    def add(code, name, parent_code, parent_name, rows):
        groups.setdefault(code, {'code': code, 'name': name, 'parentCode': parent_code, 'parentName': parent_name})
        for row in rows:
            entry = (code, row)
            if row.get('code'):
                codes[row['code']].append(entry)
            for span in row.get('_sourceSpans') or []:
                spans[_span_identity(span)].append(entry)

    for group in view.get('clinicalGroups', ()):
        for panel in group.get('templateRows', ()):
            add(group['code'] + '.' + panel['code'], panel['name'], group['code'], group['name'], panel['rows'])
    for field, parent, name in (
        ('bodyGroups', 'body', '体格与人体成分'),
        ('normalBaselines', 'normalBaselines', '补充检查基线'),
    ):
        for group in view.get(field, ()):
            add(parent + '.' + group['code'], group['name'], parent, name, group['rows'])
    if view.get('specialtyFindings'):
        add('clinical.specialty', '专项检查', 'clinical', '完整临床来源', view['specialtyFindings'])
    return groups, codes, spans


def _fallback_group(row):
    from .assessment_clinical_view import _BODY_GROUPS
    from .assessment_clinical_view import _CLINICAL_GROUPS
    from .assessment_clinical_view import _CLINICAL_PANELS
    from .assessment_clinical_view import _clinical_panel
    from .assessment_clinical_view import _group_code

    role = row.get('_sourceRole')
    if role == 'summary_patient_claim':
        return {
            'code': 'clinical.reportedAssessments',
            'name': '原医生评估与结果声称',
            'parentCode': 'clinical',
            'parentName': '完整临床来源',
        }
    if role == 'reported_behavior':
        return {
            'code': 'clinical.reportedBehavior',
            'name': '临床记录中的本人生活状态',
            'parentCode': 'clinical',
            'parentName': '完整临床来源',
        }
    group = _group_code(row['name'], _BODY_GROUPS, specimen=row.get('specimen'), section=row.get('sourceSection'))
    if group is not None:
        label = next(name for code, name, _ in _BODY_GROUPS if code == group)
        return {'code': 'body.' + group, 'name': label, 'parentCode': 'body', 'parentName': '体格与人体成分'}
    group = _group_code(row['name'], _CLINICAL_GROUPS, specimen=row.get('specimen'), section=row.get('sourceSection'))
    if group is not None:
        panels = _CLINICAL_PANELS[group]
        panel = _clinical_panel(row, panels)
        label = next((name for code, name, _ in panels if code == panel), '其他相关检查')
        parent = next(name for code, name, _ in _CLINICAL_GROUPS if code == group)
        return {'code': group + '.' + panel, 'name': label, 'parentCode': group, 'parentName': parent}
    return {
        'code': 'clinical.related',
        'name': '其他已提供的临床观察',
        'parentCode': 'clinical',
        'parentName': '完整临床来源',
    }


def build_clinical_observation_registry(document, *, clinical_view=None, facts=None):
    """Inventory every admitted owned atom; display membership cannot exclude one."""
    from .assessment_clinical_view import clinical_observation_assessment

    projection_index = ensure_patient_projection_index(document)
    definitions, by_code, by_span = _view_memberships(clinical_view or {})
    members = defaultdict(dict)
    for fact in getattr(document, 'facts', ()):
        if _get(fact, 'kind') != 'clinical':
            continue
        fid = _record_id(fact)
        projection = get_patient_projection(fact, projection_index=projection_index)
        if projection is None:
            continue
        admitted = _owned_rows(fact, projection_index)
        context = projection.get('sourceContext') or {}
        for atom in admitted:
            conditions = _acquisition_conditions(fact, projection, [atom])
            row = {
                **deepcopy(atom),
                '_ownRecordId': fid,
                '_equivalentOwnRecordIds': [fid],
                '_sourceRole': atom['role'],
                '_sourceNature': atom.get('sourceNature'),
                '_sourceSectionRole': atom.get('sectionRole'),
                '_sourceSpans': deepcopy(atom['sourceSpans']),
                'templateStatus': atom.get('status'),
                'specimen': context.get('specimen'),
                'sourceSection': context.get('sourceSection') or _get(fact, 'name'),
            }
            matches = list(by_code.get(atom['code'], ()))
            for span in atom['sourceSpans']:
                matches.extend(by_span.get(_span_identity(span), ()))
            group_codes = list(dict.fromkeys(code for code, _ in matches))
            if not group_codes:
                definition = _fallback_group(row)
                definitions.setdefault(definition['code'], definition)
                group_codes = [definition['code']]
            # A merged display row can include support clauses. Only the
            # exact atom or its identical value inherits verifiedFinding.
            verified = atom['role'] in _MEASURED_ROLES and any(
                bool(other.get('_verifiedFinding'))
                and (other.get('code') == atom['code'] or str(other.get('value')) == str(atom['value']))
                for _, other in matches
            )
            member = {
                'observationId': atom['code'],
                'recordId': fid,
                'recordIds': [fid],
                'groupCodes': group_codes,
                **{
                    key: deepcopy(atom.get(key))
                    for key in (
                        'name',
                        'value',
                        'unit',
                        'referenceRange',
                        'observedAt',
                        'sourceReportedAge',
                        'status',
                    )
                },
                **clinical_observation_assessment(row),
                'verifiedFinding': bool(verified),
                'sourceRole': atom['role'],
                'sourceNature': atom.get('sourceNature'),
                'sectionRole': atom.get('sectionRole'),
                'sourceLabel': _get(fact, 'name'),
                'observationType': 'reported_clinical_assessment'
                if atom['role'] == 'summary_patient_claim'
                else atom['role'],
                'sourceSpans': deepcopy(atom['sourceSpans']),
                'relatedRecordIds': list(atom.get('relatedRecordIds') or []),
                'acquisitionContext': _acquisition_context(projection, atom),
                'acquisitionConditions': deepcopy(conditions) if atom['role'] in _MEASURED_ROLES else [],
                'acquisitionConditionStatus': 'explicit_source_condition'
                if conditions and atom['role'] in _MEASURED_ROLES
                else 'not_provided',
            }
            for code in group_codes:
                members[code].setdefault(member['observationId'], member)
    return [
        {
            **definitions[code],
            'recordIds': list(dict.fromkeys(member['recordId'] for member in inventory.values())),
            'members': list(inventory.values()),
        }
        for code, inventory in members.items()
    ]
