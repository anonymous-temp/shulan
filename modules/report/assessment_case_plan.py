"""Check and complete the model's whole-case analysis.

The analysis decides the content of the report: rows, grades, complaints,
clusters, priority items, follow-ups and test gaps. This module does not make
those decisions. It keeps only references that exist in the fact sheet, fills
what the reader must not lose (an abnormal record nobody mentioned, a danger
signal the model missed) and records every such correction for the audit.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .assessment_case_guides import LIFESTYLE_DIMENSIONS
from .assessment_case_guides import PRIORITIES
from .assessment_case_guides import STATUS_LABELS
from .assessment_case_guides import SYSTEMS
from .assessment_case_sheet import CaseSheet
from .assessment_case_sheet import same_reading

CASE_PLAN_VERSION = 'assessment-case-plan.v1'
NOT_TESTED = '未检测'
OTHER_SYSTEM = 'other'
OTHER_SYSTEM_NAME = '其他器官与专项检查'
# Display level of the callback contract for each of the five status labels.
STATUS_LEVELS = {
    '重点关注': 'focus',
    '亚临床边界': 'borderline',
    '高于理想值': 'borderline',
    '低于理想值': 'borderline',
    '稳健达标': 'normal',
    NOT_TESTED: 'notTested',
}
_LEVEL_STATUS = {'focus': '重点关注', 'borderline': '亚临床边界', 'normal': '稳健达标'}
_SEVERITY = {'重点关注': 3, '亚临床边界': 2, '高于理想值': 1, '低于理想值': 1, '稳健达标': 0, NOT_TESTED: -1}
# Program groups of the clinical view and the system each one belongs to.
_GROUP_SYSTEM = {
    'cardiometabolic': 'cardiometabolic',
    'organMetabolism': 'digestiveRenal',
    'endocrineMusculoskeletal': 'endocrineMusculoskeletal',
    'hematologyImmune': 'hematologyImmune',
    'otherClinical': OTHER_SYSTEM,
}
SYSTEM_GROUP = {system: group for group, system in _GROUP_SYSTEM.items()}
_LIMITS = {
    'bodyRows': 6,
    'systemRows': 12,
    'symptoms': 5,
    'clusters': 6,
    'priorityItems': 5,
    'followups': 6,
    'testGaps': 6,
    'recognitions': 4,
    'dataNotes': 6,
    'keyHandles': 8,
}
_MEASURED = ('clinical', 'derived')


def _text(value, limit=None) -> str:
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    return text[:limit] if limit else text


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _test_key(name) -> str:
    return re.sub(r'\W+', '', str(name or '')).casefold()


def _status(value, default='亚临床边界') -> str:
    text = _text(value)
    for label in (*STATUS_LABELS, NOT_TESTED):
        if label in text:
            return label
    return default


def _priority(value) -> str:
    text = _text(value).upper()
    return next((code for code in PRIORITIES if code in text), 'P2')


def _days(value) -> int | None:
    try:
        days = int(float(value))
    except (TypeError, ValueError):
        return None
    return days if 0 < days <= 3650 else None


def _row_level(entry) -> str | None:
    levels = [row.get('level') for row in entry.get('rows') or () if isinstance(row, dict)]
    for level in ('focus', 'borderline', 'normal'):
        if level in levels:
            return level
    return None


def _needs_attention(entry) -> bool:
    """A measured record the reader must find somewhere in chapter one or three."""
    return bool(entry.get('abnormal')) or _row_level(entry) == 'focus'


def _program_system(entry, group_of_fact) -> str:
    return _GROUP_SYSTEM.get(group_of_fact.get(entry.get('factId')), OTHER_SYSTEM)


def clinical_fact_groups(clinical) -> tuple[dict[str, str], set[str]]:
    """``{factId: program group code}`` of the clinical view and the fact ids of body measurements."""
    groups: dict[str, str] = {}
    for group in (clinical or {}).get('clinicalGroups') or ():
        for row in group.get('rows') or ():
            groups.setdefault(str(row.get('code') or '').split(':span:', 1)[0], group.get('code'))
    body = {str(row.get('code') or '').split(':span:', 1)[0] for row in (clinical or {}).get('bodyMetrics') or ()}
    return groups, body


def _rows(raw, sheet: CaseSheet, limit: int, dropped: list, scope: str) -> list[dict]:
    rows = []
    for item in _list(raw):
        item = _dict(item)
        # One imaging or examination record may hold several findings, so a
        # record can be cited by more than one row.
        handles = sheet.valid(item.get('handles'), ('clinical', 'derived', 'advice'))
        title = _text(item.get('title'), 60)
        if not handles or not title:
            if title or item.get('handles'):
                dropped.append({'scope': scope, 'title': title, 'reason': 'no_valid_record'})
            continue
        rows.append(
            {
                'title': title,
                'handles': handles,
                'ideal': _text(item.get('ideal'), 120) or None,
                'status': _status(item.get('status')),
                'origin': 'model',
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _enforce_status(row, sheet: CaseSheet, corrections: list, scope: str) -> None:
    """A record far outside its printed range is never shown as fully normal."""
    if row['status'] != '稳健达标':
        return
    for handle in row['handles']:
        entry = sheet.entry(handle)
        if entry and entry['kind'] in _MEASURED and _row_level(entry) == 'focus':
            row['status'] = '亚临床边界'
            corrections.append(
                {
                    'scope': scope,
                    'title': row['title'],
                    'handle': handle,
                    'reason': 'record_outside_reference_shown_as_normal',
                }
            )
            return


def normalize_case_analysis(raw: Any, sheet: CaseSheet, *, clinical=None) -> dict[str, Any]:
    """Validated plan of the report; every reference exists in ``sheet``."""
    raw = _dict(raw)
    dropped: list[dict] = []
    corrections: list[dict] = []
    added: list[dict] = []
    group_of_fact, body_facts = clinical_fact_groups(clinical)

    phenotype = _dict(raw.get('phenotype'))
    plan: dict[str, Any] = {
        'version': CASE_PLAN_VERSION,
        'phenotype': {
            'label': _text(phenotype.get('label'), 80) or None,
            'handles': sheet.valid(phenotype.get('handles')),
        },
        'dataNotes': [],
    }
    for item in _list(raw.get('dataNotes'))[: _LIMITS['dataNotes']]:
        item = _dict(item)
        note = _text(item.get('note'), 240)
        if note:
            kind = _text(item.get('kind'))
            plan['dataNotes'].append(
                {
                    'kind': kind if kind in {'timing', 'calibration', 'missing'} else 'calibration',
                    'title': _text(item.get('title'), 40) or None,
                    'note': note,
                    'handles': sheet.valid(item.get('handles')),
                }
            )

    plan['bodyRows'] = _rows(raw.get('bodyRows'), sheet, _LIMITS['bodyRows'], dropped, 'body')

    systems: dict[str, dict] = {}
    others: list[dict] = []
    known = dict(SYSTEMS)
    for item in _list(raw.get('systems')):
        item = _dict(item)
        code = _text(item.get('code'))
        if code not in known and not code.startswith(OTHER_SYSTEM):
            code = next(
                (key for key, name in SYSTEMS if _text(item.get('name')) and _text(item.get('name')) in name),
                OTHER_SYSTEM,
            )
        system = {
            'code': code if code in known else OTHER_SYSTEM,
            'name': known.get(code) or _text(item.get('name'), 30) or OTHER_SYSTEM_NAME,
            'status': _status(item.get('status'), default=NOT_TESTED),
            'keyHandles': sheet.valid(item.get('keyHandles'))[: _LIMITS['keyHandles']],
            'rows': _rows(item.get('rows'), sheet, _LIMITS['systemRows'], dropped, code),
        }
        if system['code'] == 'microbiome':
            # Chapter four shows the microbiome tables as the test report groups them.
            system['rows'] = []
            dropped[:] = [row for row in dropped if row.get('scope') != 'microbiome']
        if system['code'] in known:
            if system['code'] in systems:
                # A repeated system only adds its rows to the first one.
                first = systems[system['code']]
                taken = {handle for row in first['rows'] for handle in row['handles']}
                first['rows'] += [row for row in system['rows'] if not set(row['handles']) & taken]
            else:
                systems[system['code']] = system
        elif system['rows'] or system['keyHandles']:
            others.append(system)
    if len(others) > 1:
        # One extra section keeps the chapter readable; further ones join it.
        merged = others[0]
        for extra in others[1:]:
            merged['rows'] += extra['rows']
            merged['keyHandles'] = (merged['keyHandles'] + extra['keyHandles'])[: _LIMITS['keyHandles']]
            if _SEVERITY[extra['status']] > _SEVERITY[merged['status']]:
                merged['status'] = extra['status']
        merged['name'] = OTHER_SYSTEM_NAME
        others = [merged]

    ordered = []
    for code, name in SYSTEMS:
        ordered.append(
            systems.get(code) or {'code': code, 'name': name, 'status': NOT_TESTED, 'keyHandles': [], 'rows': []}
        )
    ordered += others

    # Every record that needs attention is shown in chapter one or three.
    covered = {handle for row in plan['bodyRows'] for handle in row['handles']}
    covered |= {handle for system in ordered for row in system['rows'] for handle in row['handles']}
    # A repeated reading that the analysis reconciled in a data note is already accounted for.
    covered |= {handle for note in plan['dataNotes'] for handle in note['handles']}
    by_code = {system['code']: system for system in ordered}
    shown = [entry for handle in covered if (entry := sheet.entry(handle)) and entry['kind'] in _MEASURED]
    for handle in sheet.handles():
        entry = sheet.entries[handle]
        if entry['kind'] not in _MEASURED or handle in covered or not _needs_attention(entry):
            continue
        # A second record of a reading that a row already shows needs no row of its own.
        if any(same_reading(entry, other) for other in shown):
            covered.add(handle)
            continue
        status = _LEVEL_STATUS.get(_row_level(entry) or 'borderline', '亚临床边界')
        row = {
            'title': entry['name'][:60],
            'handles': [handle],
            'ideal': entry.get('target') or None,
            'status': status,
            'origin': 'program',
        }
        if entry.get('factId') in body_facts:
            plan['bodyRows'].append(row)
            scope = 'body'
        else:
            code = _program_system(entry, group_of_fact)
            if code not in by_code:
                by_code[code] = {
                    'code': OTHER_SYSTEM,
                    'name': OTHER_SYSTEM_NAME,
                    'status': status,
                    'keyHandles': [],
                    'rows': [],
                }
                ordered.append(by_code[code])
            by_code[code]['rows'].append(row)
            scope = code
        covered.add(handle)
        shown.append(entry)
        added.append({'scope': scope, 'handle': handle, 'name': entry['name'], 'reason': 'abnormal_record_unmentioned'})

    for row in plan['bodyRows']:
        _enforce_status(row, sheet, corrections, 'body')
    for system in ordered:
        for row in system['rows']:
            _enforce_status(row, sheet, corrections, system['code'])
        has_micro = system['code'] == 'microbiome' and sheet.has_microbiome
        if system['status'] == NOT_TESTED and (system['rows'] or has_micro):
            worst = max((row['status'] for row in system['rows']), key=_SEVERITY.get, default='亚临床边界')
            system['status'] = worst
            corrections.append({'scope': system['code'], 'reason': 'system_with_records_marked_not_tested'})
        if system['status'] != NOT_TESTED and not system['rows'] and not has_micro and not system['keyHandles']:
            system['status'] = NOT_TESTED
        if not system['keyHandles']:
            ranked = sorted(system['rows'], key=lambda row: -_SEVERITY[row['status']])
            system['keyHandles'] = [handle for row in ranked for handle in row['handles']][: _LIMITS['keyHandles']]
        for index, row in enumerate(system['rows'], 1):
            row['index'] = index
    for index, row in enumerate(plan['bodyRows'], 1):
        row['index'] = index
    plan['systems'] = ordered

    # Clusters first, so that every other part can refer to their final ids.
    clusters = []
    for position, item in enumerate(_list(raw.get('clusters')), 1):
        item = _dict(item)
        name = _text(item.get('name'), 80)
        cluster = {
            'source': position,
            'name': name,
            'priority': _priority(item.get('priority')),
            'clinicalHandles': sheet.valid(item.get('clinicalHandles'), ('clinical', 'derived', 'advice')),
            'microbiomeHandles': sheet.valid(item.get('microbiomeHandles'), ('microbiome',)),
            'reportedHandles': sheet.valid(item.get('reportedHandles'), ('reported',)),
        }
        if not name or not (cluster['clinicalHandles'] or cluster['microbiomeHandles'] or cluster['reportedHandles']):
            dropped.append({'scope': 'clusters', 'title': name, 'reason': 'no_valid_record'})
            continue
        clusters.append(cluster)
    clusters = clusters[: _LIMITS['clusters']]

    items = []
    for item in _list(raw.get('priorityItems'))[: _LIMITS['priorityItems']]:
        item = _dict(item)
        title, handles = _text(item.get('title'), 60), sheet.valid(item.get('handles'))
        if not title or not handles:
            dropped.append({'scope': 'priorityItems', 'title': title, 'reason': 'no_valid_record'})
            continue
        try:
            source = int(item.get('cluster'))
        except (TypeError, ValueError):
            source = None
        items.append(
            {
                'title': title,
                'timeframe': _text(item.get('timeframe'), 30) or None,
                'days': _days(item.get('days')),
                'department': _text(item.get('department'), 30) or None,
                'checks': [_text(check, 40) for check in _list(item.get('checks')) if _text(check)][:6],
                'handles': handles,
                'clusterSource': source,
                'origin': 'model',
            }
        )
    # A priority item makes its cluster a priority cluster.
    for item in items:
        cluster = next((row for row in clusters if row['source'] == item['clusterSource']), None)
        if cluster is None:
            cluster = next((row for row in clusters if set(item['handles']) & set(row['clinicalHandles'])), None)
        if cluster is not None:
            if cluster['priority'] != 'P0':
                corrections.append(
                    {'scope': 'clusters', 'title': cluster['name'], 'reason': 'cluster_of_priority_item_promoted'}
                )
                cluster['priority'] = 'P0'
            item['clusterSource'] = cluster['source']
        else:
            item['clusterSource'] = None
    clusters.sort(key=lambda row: PRIORITIES.index(row['priority']))
    renumber = {}
    for index, cluster in enumerate(clusters, 1):
        renumber[cluster.pop('source')] = f'C{index}'
        cluster.update(index=index, id=f'C{index}')
    for index, item in enumerate(items, 1):
        item.update(index=index, clusterId=renumber.get(item.pop('clusterSource')))
    plan['clusters'], plan['priorityItems'] = clusters, items

    plan['followups'] = []
    for item in _list(raw.get('followups')):
        item = _dict(item)
        title, handles = _text(item.get('title'), 60), sheet.valid(item.get('handles'))
        if not title or not handles:
            continue
        plan['followups'].append(
            {
                'index': len(plan['followups']) + 1,
                'title': title,
                'department': _text(item.get('department'), 30) or None,
                'interval': _text(item.get('interval'), 30) or None,
                'days': _days(item.get('days')),
                'checks': [_text(check, 40) for check in _list(item.get('checks')) if _text(check)][:4],
                'handles': handles,
                'origin': 'model',
            }
        )
        if len(plan['followups']) >= _LIMITS['followups']:
            break

    recorded = set()
    for entry in sheet.entries.values():
        if entry['kind'] in _MEASURED:
            # "空腹葡萄糖(Glu)" is also recorded under its name without the abbreviation.
            recorded.add(_test_key(entry['name']))
            recorded.add(_test_key(re.sub(r'[（(][^）)]*[）)]', '', entry['name'])))
    recorded.discard('')
    plan['testGaps'] = []
    for item in _list(raw.get('testGaps')):
        item = _dict(item)
        test = _text(item.get('test'), 60)
        if not test:
            continue
        if _test_key(test) in recorded or _test_key(re.sub(r'[（(][^）)]*[）)]', '', test)) in recorded:
            dropped.append({'scope': 'testGaps', 'title': test, 'reason': 'test_already_recorded'})
            continue
        plan['testGaps'].append(
            {
                'index': len(plan['testGaps']) + 1,
                'test': test,
                'because': _text(item.get('because'), 160) or None,
                'handles': sheet.valid(item.get('handles')),
            }
        )
        if len(plan['testGaps']) >= _LIMITS['testGaps']:
            break

    plan['symptoms'] = []
    for item in _list(raw.get('symptoms')):
        item = _dict(item)
        name, handles = _text(item.get('name'), 40), sheet.valid(item.get('handles'), ('reported',))
        if not name or not handles:
            # A complaint is only what the person reported.
            if name:
                dropped.append({'scope': 'symptoms', 'title': name, 'reason': 'not_reported_by_subject'})
            continue
        index = len(plan['symptoms']) + 1
        plan['symptoms'].append(
            {
                'index': index,
                'code': f'S{index}',
                'name': name,
                'handles': handles,
                'clinicalHandles': sheet.valid(item.get('clinicalHandles'), ('clinical', 'derived', 'advice')),
                'microbiomeHandles': sheet.valid(item.get('microbiomeHandles'), ('microbiome',)),
            }
        )
        if index >= _LIMITS['symptoms']:
            break

    plan['background'] = {'handles': sheet.valid(_dict(raw.get('background')).get('handles'))}
    plan['protective'] = {'handles': sheet.valid(_dict(raw.get('protective')).get('handles'))}
    dimensions = dict(LIFESTYLE_DIMENSIONS)
    merged: dict[str, dict] = {}
    for item in _list(raw.get('lifestyle')):
        item = _dict(item)
        code = _text(item.get('dimension'))
        handles = sheet.valid(item.get('handles'), ('reported', 'clinical', 'advice'))
        if code not in dimensions or not handles:
            continue
        linked = []
        for value in _list(item.get('clusters')):
            # The model numbers clusters in its own order; "C2" means its second cluster too.
            if isinstance(value, str) and re.fullmatch(r'[Cc]\d+', value.strip()):
                value = value.strip()[1:]
            try:
                identifier = renumber.get(int(value))
            except (TypeError, ValueError):
                identifier = None
            if identifier and identifier not in linked:
                linked.append(identifier)
        row = merged.setdefault(code, {'dimension': code, 'name': dimensions[code], 'handles': [], 'clusterIds': []})
        row['handles'] += [handle for handle in handles if handle not in row['handles']]
        row['clusterIds'] += [identifier for identifier in linked if identifier not in row['clusterIds']]
    plan['lifestyle'] = [merged[code] for code, _ in LIFESTYLE_DIMENSIONS if code in merged]
    for index, row in enumerate(plan['lifestyle'], 1):
        row['index'] = index

    plan['recognitions'] = []
    for item in _list(raw.get('recognitions'))[: _LIMITS['recognitions']]:
        item = _dict(item)
        title = _text(item.get('title'), 50)
        if title:
            plan['recognitions'].append(
                {'index': len(plan['recognitions']) + 1, 'title': title, 'handles': sheet.valid(item.get('handles'))}
            )
    plan['audit'] = {'dropped': dropped, 'corrections': corrections, 'programRows': added}
    return plan


def _fact_handles(sheet: CaseSheet) -> dict[str, str]:
    return {entry['factId']: handle for handle, entry in sheet.entries.items() if entry.get('factId')}


def apply_priority_safety_net(plan: dict[str, Any], sheet: CaseSheet, flags: dict | None) -> list[dict]:
    """Add danger-signal rule hits the analysis did not list; never remove a model item.

    The rules are a net under the model's judgement: a rule hit that shares
    no record with any listed priority item is appended as its own item, and a
    listed item that a rule also found takes the rule's sources and the
    stricter of the two time limits. Returns the audit entries.
    """
    audit: list[dict] = []
    handle_of = _fact_handles(sheet)
    items = plan['priorityItems']
    for hit in (flags or {}).get('priorityItems') or ():
        handles = [handle_of[fid] for fid in hit.get('factIds') or () if fid in handle_of]
        if not handles:
            continue
        timeframe = hit.get('timeframe') or {}
        match = next((item for item in items if set(item['handles']) & set(handles)), None)
        if match is not None:
            match.setdefault('ruleIds', []).append(hit.get('ruleId'))
            match.setdefault('sources', []).extend(deepcopy(hit.get('sources') or []))
            days = timeframe.get('days')
            # A rule may only tighten the model's time limit, never loosen it.
            if isinstance(days, int) and (match.get('days') is None or days < match['days']):
                audit.append(
                    {
                        'action': 'timeframe_tightened',
                        'title': match['title'],
                        'ruleId': hit.get('ruleId'),
                        'from': match.get('days'),
                        'to': days,
                    }
                )
                match.update(days=days, timeframe=timeframe.get('text') or match.get('timeframe'))
            continue
        cluster = next((row for row in plan['clusters'] if set(handles) & set(row['clinicalHandles'])), None)
        if cluster is not None and cluster['priority'] != 'P0':
            cluster['priority'] = 'P0'
        items.append(
            {
                'index': len(items) + 1,
                'title': _text(hit.get('title'), 60),
                'timeframe': timeframe.get('text'),
                'days': timeframe.get('days'),
                'department': hit.get('department'),
                'checks': list(hit.get('checks') or ())[:6],
                'handles': handles,
                'clusterId': cluster['id'] if cluster is not None else None,
                'origin': 'safety_net',
                'ruleIds': [hit.get('ruleId')],
                'sources': deepcopy(hit.get('sources') or []),
            }
        )
        audit.append(
            {
                'action': 'priority_item_added',
                'title': hit.get('title'),
                'ruleId': hit.get('ruleId'),
                'handles': handles,
            }
        )
    if any(entry['action'] == 'priority_item_added' for entry in audit):
        # Promotions may have changed the order of the clusters.
        order = sorted(plan['clusters'], key=lambda row: (PRIORITIES.index(row['priority']), row['index']))
        renumber = {}
        for index, cluster in enumerate(order, 1):
            renumber[cluster['id']] = f'C{index}'
        for cluster in order:
            cluster.update(index=int(renumber[cluster['id']][1:]), id=renumber[cluster['id']])
        plan['clusters'] = order
        for item in items:
            item['clusterId'] = renumber.get(item.get('clusterId'))
        for row in plan['lifestyle']:
            row['clusterIds'] = sorted(
                {renumber[identifier] for identifier in row['clusterIds'] if identifier in renumber}
            )
    for hit in (flags or {}).get('routineFollowups') or ():
        handles = [handle_of[fid] for fid in hit.get('factIds') or () if fid in handle_of]
        match = next((item for item in plan['followups'] if set(item['handles']) & set(handles)), None)
        if match is not None:
            match.setdefault('ruleIds', []).append(hit.get('ruleId'))
            match.setdefault('sources', []).extend(deepcopy(hit.get('sources') or []))
    plan['audit']['safetyNet'] = audit
    return audit


def plan_statistics(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        'systems': [(system['code'], system['status'], len(system['rows'])) for system in plan['systems']],
        'bodyRows': len(plan['bodyRows']),
        'symptoms': len(plan['symptoms']),
        'clusters': [(cluster['id'], cluster['priority']) for cluster in plan['clusters']],
        'priorityItems': [(item['title'], item['origin']) for item in plan['priorityItems']],
        'followups': len(plan['followups']),
        'testGaps': len(plan['testGaps']),
        'lifestyle': [row['dimension'] for row in plan['lifestyle']],
        'recognitions': len(plan['recognitions']),
        'dropped': len(plan['audit']['dropped']),
        'corrections': len(plan['audit']['corrections']),
        'programRows': len(plan['audit']['programRows']),
    }
