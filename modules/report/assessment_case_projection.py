"""Project a model-authored case into the six-chapter presentation.

The callback contract (``assessment.v2``) is unchanged: this module fills the
same fields from the validated analysis plan and the reviewed cells. Display
rows (values, references, chart fields) always come from the program's
clinical and microbiome views; only grouping, grades and prose come from the
model.
"""

from __future__ import annotations

import re
from copy import deepcopy

from .assessment_case_plan import NOT_TESTED
from .assessment_case_plan import OTHER_SYSTEM
from .assessment_case_plan import STATUS_LEVELS
from .assessment_case_plan import SYSTEM_GROUP
from .assessment_case_sheet import CaseSheet
from .assessment_case_sheet import readable_units
from .assessment_case_sheet import same_reading
from .assessment_chart_fields import ANCHOR_LIMIT
from .assessment_chart_fields import LEVEL_RANK
from .assessment_chart_fields import anchor_of

CASE_PRESENTATION_VERSION = 'assessment-case-presentation.v1'
CHAPTER_THREE_NAMES = {
    'cardiometabolic': '糖脂代谢、胰岛素敏感性与心血管系统评估',
    'digestiveRenal': '肝胆胰消化、肾脏代谢与营养辅酶状态评估',
    'endocrineMusculoskeletal': '内分泌激素、骨骼肌肉与器官影像学评估',
    'hematologyImmune': '血液学、铁代谢储备与免疫炎症评估',
}
# Chapter three follows the template: metabolism, digestive, haematology, endocrine, then others.
_CHAPTER_THREE_ORDER = {'cardiometabolic': 0, 'digestiveRenal': 1, 'hematologyImmune': 2, 'endocrineMusculoskeletal': 3}
READING_GUIDE = (
    ('报告定位', '在本次资料覆盖范围内，说明需优先排查的发现、需协同管理的失衡方向，以及可作为今后对照的稳健基线。'),
    (
        '双轨评价',
        '每项检测对照两条线：临床参考范围取自检验单，用于判断是否超出正常范围；理想目标更严格，'
        '用于识别参考范围内的早期偏离。',
    ),
    (
        '结构导览',
        '第一章为全息健康图景概览与优先排查事项；第二章解析主诉与客观指标的关联；第三章与第四章分别为'
        '分系统临床检验评估和肠道微生态评估；第五章归纳系统失衡集群与生活诱因；第六章给出健康风险分层结论与干预方向。',
    ),
)
NO_MICROBIOME_LEAD = '本次未提供肠道菌群检测结果，本章不作菌群评估；健康状态以前三章的临床检查结果为依据。'
DEGRADED_STATEMENT = '本次未能完成综合分析，本节暂缺；检查记录与需优先处理的事项见第一章和第三章。'
NO_QUESTIONNAIRE_LIFESTYLE = '本次未提供家族史、既往病史与生活方式资料，健康诱因暂无法复盘；补充问卷资料后可完善本节。'
_SIGNAL_VALUE_LIMIT = 28
_ROW_LIMIT = 10
_SPAN_LIMIT = 4
_LEVEL_ORDER = ('normal', 'borderline', 'focus')


def _public(row: dict) -> dict:
    return {key: deepcopy(value) for key, value in row.items() if not key.startswith('_')}


def _short(value, limit=_SIGNAL_VALUE_LIMIT):
    text = re.sub(r'\s+', ' ', str(value)).strip() if value is not None else ''
    return text if text and len(text) <= limit else None


def _signal(row: dict, **extra) -> dict:
    name = str(row.get('name') or '')
    return {
        'name': name.rsplit(' · ', 1)[-1] if ' · ' in name else name,
        'value': _short(row.get('value')),
        'unit': row.get('unit'),
        'referenceRange': _short(row.get('referenceRange'), 60),
        'status': row.get('status'),
        'observedAt': row.get('observedAt'),
        'level': row.get('level'),
        'direction': row.get('direction'),
        'evidenceRefs': [],
        **extra,
    }


class _Projection:
    def __init__(self, sheet: CaseSheet, plan: dict, cells: dict):
        self.sheet, self.plan, self.cells = sheet, plan, cells
        self.missing: list[str] = []

    def text(self, identifier, *, required=False):
        value = self.cells.get(identifier)
        if not value and required:
            self.missing.append(identifier)
        return readable_units(value) if value else None

    def measured_rows(self, handles, title='') -> list[dict]:
        """Display rows of the records a table row cites.

        A record with many recorded spans (an imaging description, a device
        printout) shows the spans the row title names, else its abnormal
        spans, else one collapsed row, so a table cell stays readable.
        """
        tokens = [token for token in re.split(r'[、，,；;/（）()与和及或\s]+', title or '') if len(token) >= 2]
        result, seen = [], set()

        def take(row):
            code = row.get('code')
            if code not in seen and len(result) < _ROW_LIMIT and not _repeats(row, result):
                seen.add(code)
                result.append(_public(row))

        for handle in handles:
            entry = self.sheet.entry(handle)
            if not entry or entry['kind'] not in {'clinical', 'derived'}:
                continue
            rows = entry.get('rows') or []
            if not rows:
                # A record without a display row (a calculated count, a text finding) is shown as recorded.
                take(
                    {
                        'code': entry.get('factId') or handle,
                        'name': entry['name'],
                        'value': entry.get('value') or None,
                        'unit': entry.get('unit') or None,
                        'referenceRange': entry.get('reference') or None,
                        'status': entry.get('status') or None,
                        'observedAt': entry.get('date') or None,
                        'sourceKind': 'clinical',
                        'evidenceRefs': [],
                        'level': None,
                        'direction': None,
                    }
                )
                continue
            if len(rows) <= 2:
                for row in rows:
                    take(row)
                continue
            named = []
            for row in rows:
                label = str(row.get('name') or '').rsplit(' · ', 1)[-1]
                text = f'{row.get("name") or ""}{row.get("value") or ""}'.split(' · ', 1)[-1]
                if (label and len(label) <= 12 and label in (title or '')) or any(token in text for token in tokens):
                    named.append(row)
            abnormal = [row for row in rows if row.get('level') in {'focus', 'borderline'} or row.get('direction')]
            chosen = (named or abnormal)[:_SPAN_LIMIT]
            if chosen:
                for row in chosen:
                    take(row)
                continue
            values = []
            for row in rows:
                value = re.sub(r'\s+', ' ', str(row.get('value') or '')).strip()
                if value and value not in values and len('；'.join([*values, value])) <= 72:
                    values.append(value)
            first = rows[0]
            take(
                {
                    **first,
                    'code': entry.get('factId') or first.get('code'),
                    'name': entry['name'],
                    'value': '；'.join(values) or first.get('value'),
                    'level': 'normal' if any(row.get('level') == 'normal' for row in rows) else first.get('level'),
                }
            )
        return result

    def signals(self, handles, limit=12) -> list[dict]:
        result, seen = [], set()
        for handle in handles:
            entry = self.sheet.entry(handle)
            if not entry:
                continue
            if entry['kind'] == 'reported':
                candidates = [
                    {
                        'name': entry['name'],
                        'value': _short(entry['value'], 80),
                        'unit': None,
                        'referenceRange': None,
                        'status': '本人自述',
                        'observedAt': None,
                        'level': None,
                        'direction': None,
                        'evidenceRefs': [],
                        'sourceKind': 'selfReported',
                    }
                ]
            elif entry['kind'] == 'advice':
                candidates = [
                    {
                        'name': entry['name'],
                        'value': None,
                        'unit': None,
                        'referenceRange': None,
                        'status': '体检结论',
                        'observedAt': entry.get('date') or None,
                        'level': None,
                        'direction': None,
                        'evidenceRefs': [],
                        'kind': 'advice',
                    }
                ]
            elif entry.get('rows'):
                candidates = [_signal(row) for row in entry['rows']]
            else:
                candidates = [
                    {
                        'name': entry['name'],
                        'value': _short(entry['value']),
                        'unit': entry.get('unit') or None,
                        'referenceRange': _short(entry.get('reference'), 60),
                        'status': entry.get('status') or None,
                        'observedAt': entry.get('date') or None,
                        'level': None,
                        'direction': None,
                        'evidenceRefs': [],
                    }
                ]
            for candidate in candidates:
                key = (candidate['name'], candidate['value'])
                if key in seen:
                    continue
                seen.add(key)
                result.append(candidate)
                if len(result) >= limit:
                    return result
        return result

    def anchors(self, handles, level) -> tuple[list[dict], int]:
        """Chips of the cited records that need attention; a record with many spans gives its abnormal ones."""
        anchors, seen = [], set()
        for handle in handles:
            entry = self.sheet.entry(handle)
            rows = (entry or {}).get('rows') or ()
            flagged = [row for row in rows if row.get('level') in {'focus', 'borderline'}]
            if not flagged and len(rows) == 1 and level in {'focus', 'borderline'}:
                flagged = list(rows)
            for row in flagged:
                name = row.get('name')
                if name in seen:
                    continue
                seen.add(name)
                anchors.append(
                    anchor_of(row, row.get('level') if row.get('level') in {'focus', 'borderline'} else level)
                )
        return anchors[:ANCHOR_LIMIT], max(0, len(anchors) - ANCHOR_LIMIT)


def _clean_program_rows(node) -> None:
    """Reader wording for program-built rows and chips; model-written prose is left as reviewed.

    Row labels taken from questionnaire prompts lose the second person, and
    provenance words inside names, values and printed references are replaced
    (values, units and numbers stay as recorded).
    """
    from .evaluation_presentation import _assessment_reference
    from .evaluation_presentation import _question_label
    from .evaluation_presentation import assessment_reader_text

    if isinstance(node, list):
        for item in node:
            _clean_program_rows(item)
        return
    if not isinstance(node, dict):
        return
    # A display row or chip: it has a name and a value but no members of its own.
    is_row = (
        'name' in node
        and ('value' in node or 'referenceRange' in node)
        and not ('rows' in node or 'items' in node or 'templateRows' in node)
    )
    if is_row:
        for key in ('name', 'value', 'status', 'templateStatus'):
            if isinstance(node.get(key), str):
                node[key] = assessment_reader_text(node[key])
    if isinstance(node.get('referenceRange'), str):
        node['referenceRange'] = _assessment_reference(node['referenceRange'])
    if isinstance(node.get('question'), str):
        node['question'] = _question_label(node['question'])
    for key, value in node.items():
        if isinstance(value, (dict, list)) and key != 'sources':
            _clean_program_rows(value)


def _repeats(row, shown) -> bool:
    """Whether a numeric result is already shown under an equivalent name (one line per reading)."""
    return any(same_reading(row, other) for other in shown)


def _cap_levels(rows, level):
    """A member is never shown more severe than the grade of its table row."""
    if level not in _LEVEL_ORDER:
        return
    for row in rows:
        if row.get('level') in _LEVEL_ORDER and _LEVEL_ORDER.index(row['level']) > _LEVEL_ORDER.index(level):
            row['level'] = level


def _fallback_listing(rows) -> str | None:
    parts = []
    for row in rows[:8]:
        value = _short(row.get('value'), 40)
        if not row.get('name') or not value:
            continue
        reference = _short(row.get('referenceRange'), 40)
        unit = f' {row["unit"]}' if row.get('unit') else ''
        parts.append(f'{row["name"]} {value}{unit}' + (f'（参考 {reference}）' if reference else ''))
    return '；'.join(parts) + '。' if parts else None


def _table_row(projection: _Projection, row, cell_id, code) -> dict:
    level = STATUS_LEVELS.get(row['status'])
    rows = projection.measured_rows(row['handles'], row['title'])
    _cap_levels(rows, level)
    summary = projection.text(cell_id)
    completion = 'complete'
    if not summary:
        summary = _fallback_listing(rows)
        completion = 'source_only'
        projection.missing.append(cell_id)
    # Chips of a table row are the displayed members that need attention.
    flagged = [member for member in rows if member.get('level') in {'focus', 'borderline'}]
    anchors = [anchor_of(member, member['level']) for member in flagged[:ANCHOR_LIMIT]]
    more = max(0, len(flagged) - ANCHOR_LIMIT)
    notes = [
        entry['name']
        for handle in row['handles']
        if (entry := projection.sheet.entry(handle)) and entry['kind'] == 'advice'
    ]
    return {
        'code': code,
        'name': row['title'],
        'rows': rows,
        'status': row['status'],
        'comparisonReference': row.get('ideal'),
        'summary': summary,
        'level': level,
        'anchors': anchors,
        'moreCount': more,
        'recordedConclusions': notes,
        'evidenceRefs': [],
        'completion': completion,
    }


def _timeframe(text, days):
    return {'text': text, 'days': days} if text or days else None


def _priority_sections(projection: _Projection, statements) -> list[dict]:
    plan = projection.plan
    urgent_items = []
    for item in plan['priorityItems']:
        text = projection.text(f'overview.priority.{item["index"]}')
        if not text:
            checks = '、'.join(item.get('checks') or ())
            detail = '，'.join(
                filter(
                    None,
                    (
                        item.get('timeframe')
                        and (item['timeframe'] if '就诊' in item['timeframe'] else f'{item["timeframe"]}就诊'),
                        item.get('department'),
                        checks and f'完成{checks}',
                    ),
                )
            )
            text = (f'{item["title"]}：{detail}' if detail else item['title']) + '。'
            projection.missing.append(f'overview.priority.{item["index"]}')
        urgent_items.append(
            {
                'code': (item.get('ruleIds') or [f'priority-{item["index"]}'])[0],
                'name': item['title'],
                'priority': 'P0',
                'timeframe': _timeframe(item.get('timeframe'), item.get('days')),
                'department': item.get('department'),
                'checks': list(item.get('checks') or ()),
                'basisRows': projection.signals(item['handles'], 6),
                'text': text,
                'clusterId': item.get('clusterId'),
                'origin': item.get('origin'),
                **({'ruleId': item['ruleIds'][0]} if item.get('ruleIds') else {}),
                'evidenceRefs': list(item.get('evidenceRefs') or ()),
            }
        )
    routine_items = []
    for item in plan['followups']:
        text = (
            projection.text(f'overview.followup.{item["index"]}')
            or '，'.join(
                filter(
                    None, (item['title'], item.get('department') and f'{item["department"]}随访', item.get('interval'))
                )
            )
            + '。'
        )
        routine_items.append(
            {
                'code': f'followup-{item["index"]}',
                'name': item['title'],
                'priority': None,
                'timeframe': _timeframe(item.get('interval'), item.get('days')),
                'department': item.get('department'),
                'checks': list(item.get('checks') or ()),
                'basisRows': projection.signals(item['handles'], 4),
                'text': text,
                'evidenceRefs': list(item.get('evidenceRefs') or ()),
            }
        )
    gap_items = []
    for item in plan['testGaps']:
        text = projection.text(f'overview.gap.{item["index"]}') or (
            (f'因{item["because"]}，' if item.get('because') else '') + f'建议补查{item["test"]}。'
        )
        gap_items.append(
            {
                'code': f'gap-{item["index"]}',
                'name': item['test'],
                'priority': None,
                'timeframe': None,
                'department': None,
                'checks': [],
                'basisRows': projection.signals(item['handles'], 4),
                'text': text,
                'reason': item.get('because'),
                'evidenceRefs': [],
            }
        )

    def section(code, name, items, empty):
        text = '\n\n'.join(item['text'] for item in items) if items else empty
        first = items[0] if items else {}
        return {
            'code': code,
            'name': name,
            'priority': 'P0' if code == 'urgent' and items else None,
            'text': text,
            'evidenceRefs': list(dict.fromkeys(ref for item in items for ref in item['evidenceRefs'])),
            'items': items,
            'action': text,
            'timeframe': first.get('timeframe'),
            'department': first.get('department'),
        }

    sections = [
        section('urgent', '优先排查', urgent_items, statements['noUrgent']),
        section('routine', '常规专科随访', routine_items, statements['noRoutine']),
    ]
    if gap_items:
        sections.append(section('gaps', '检测盲区', gap_items, None))
    return sections


def build_case_presentation(
    *, request, parsed, document, data, clinical, microbiome, sheet: CaseSheet, case, timing=None
):
    """``AssessmentPresentation`` of one authored case."""
    from . import assessment_presentation as legacy
    from .assessment_clinical_view import attach_target_references
    from .assessment_clinical_view import cite_reference
    from .assessment_clinical_view import reference_index

    plan, cells = case['plan'], case.get('cells') or {}
    degraded = bool(case.get('degraded'))
    clinical, microbiome = deepcopy(clinical), deepcopy(microbiome)
    projection = _Projection(sheet, plan, cells)

    # Bibliography: checked target sources and the guidelines behind matched danger-signal rules.
    attach_target_references(clinical, data)
    library = getattr(getattr(data, 'appendices', None), 'reference_literature', None)
    if library is not None:
        numbers = reference_index(library)
        for key in ('priorityItems', 'followups'):
            for item in plan[key]:
                refs = [
                    cite_reference(
                        library, numbers, source, study_type='指南', section='assessmentPresentation.rule.priority'
                    )
                    for source in item.get('sources') or ()
                    if source.get('key') not in {'program_default', 'original_advice'} and source.get('title')
                ]
                item['evidenceRefs'] = list(dict.fromkeys(refs))
    # Rows attached to sheet entries are the same dict objects as in ``case_inputs``; re-read
    # their target references from the copies that now carry bibliography numbers.
    targets = {}
    for rows in (
        clinical.get('bodyMetrics') or (),
        *(group.get('rows') or () for group in clinical.get('clinicalGroups') or ()),
        clinical.get('specialtyFindings') or (),
    ):
        for row in rows:
            if isinstance(row.get('target'), dict):
                targets[row.get('code')] = row['target']
    for entry in sheet.entries.values():
        for row in entry.get('rows') or ():
            if row.get('code') in targets:
                row['target'] = deepcopy(targets[row['code']])

    # Chapter 1.1
    body_groups = []
    for row in plan['bodyRows']:
        group = _table_row(projection, row, f'overview.bodyRow.{row["index"]}', f'body{row["index"]}')
        group.pop('recordedConclusions', None)
        body_groups.append(group)
    clinical['bodyGroups'] = body_groups
    body_summary = projection.text('overview.bodySummary', required=bool(body_groups))
    if not body_groups and not clinical.get('bodyMetrics'):
        body_summary = legacy.NO_BODY_SUMMARY

    # Chapter 1.2
    systems = []
    for system in plan['systems']:
        code = system['code']
        level = STATUS_LEVELS.get(system['status'], 'notTested')
        tested = system['status'] != NOT_TESTED
        anchors, more = projection.anchors(system['keyHandles'], level) if tested else ([], 0)
        systems.append(
            {
                'code': 'otherClinical' if code == OTHER_SYSTEM else code,
                'name': system['name'],
                'status': system['status'],
                'level': level,
                'keyFindings': projection.text(f'overview.system.{code}.evidence') if tested else None,
                'summary': projection.text(f'overview.system.{code}.conclusion', required=tested) if tested else None,
                'rows': projection.signals(system['keyHandles'], 8) if tested else [],
                'anchors': anchors,
                'moreCount': more,
                'evidenceRefs': [],
            }
        )

    statements = {'noUrgent': legacy._NO_URGENT, 'noRoutine': legacy._NO_ROUTINE}
    clusters = []
    for cluster in plan['clusters']:
        base = f'clusters.{cluster["id"]}'
        clusters.append(
            {
                'id': cluster['id'],
                'name': cluster['name'],
                'priority': cluster['priority'],
                'interpretation': projection.text(base + '.impact', required=True),
                'action': projection.text(base + '.action'),
                'clinicalEvidence': projection.text(base + '.clinicalEvidence'),
                'microbiomeEvidence': projection.text(base + '.microbiomeEvidence'),
                'clinicalSignals': projection.signals(cluster['clinicalHandles'], 14),
                'microbiomeSignals': projection.signals(cluster['microbiomeHandles'], 12),
                'reportedSignals': projection.signals(cluster['reportedHandles'], 6),
                'evidenceRefs': list(
                    dict.fromkeys(
                        ref
                        for item in plan['priorityItems']
                        if item.get('clusterId') == cluster['id']
                        for ref in item.get('evidenceRefs') or ()
                    )
                ),
            }
        )

    overview = {
        'summary': projection.text('overview.summary'),
        'evidenceRefs': [],
        'bodySummary': body_summary,
        'bodySummaryEvidenceRefs': [],
        'systems': systems,
        'priorityFindings': _priority_sections(projection, statements),
        'graphHub': '核心健康主题' if clusters else None,
        'graphClusterIds': [cluster['id'] for cluster in clusters],
        'graphSummary': projection.text('overview.graphSummary')
        if clusters
        else (DEGRADED_STATEMENT if degraded else legacy._NO_GRAPH),
        'graphSummaryEvidenceRefs': [],
        'phenotype': plan['phenotype'].get('label'),
        'dataNotes': [
            {'kind': note['kind'], 'name': note['title'], 'text': readable_units(note['note'])}
            for note in plan['dataNotes']
        ],
        'readingGuide': [{'name': name, 'text': text} for name, text in READING_GUIDE],
        'preface': legacy.ASSESSMENT_PREFACE,
        'dataTiming': timing if timing is not None else legacy._input_timing(request, microbiome),
    }

    # Chapter 2
    symptoms = []
    for item in plan['symptoms']:
        base = f'symptoms.{item["index"]}'
        question = next((entry['name'] for handle in item['handles'] if (entry := sheet.entry(handle))), None)
        symptoms.append(
            {
                'code': item['code'],
                'name': item['name'],
                'value': item['name'],
                'history': projection.text(base + '.history'),
                'unit': None,
                'referenceRange': None,
                'status': '本人自述',
                'observedAt': None,
                'interpretationTitle': projection.text(base + '.title'),
                'interpretation': projection.text(base + '.explanation', required=True),
                'clinicalEvidence': projection.text(base + '.clinicalEvidence'),
                'microbiomeEvidence': projection.text(base + '.microbiomeEvidence'),
                'sourceKind': 'selfReported',
                'sources': [],
                'evidenceRefs': [],
                'clinicalSignals': projection.signals(item['clinicalHandles'], 8),
                'microbiomeSignals': projection.signals(item['microbiomeHandles'], 8),
                'question': question,
            }
        )
    clinical['symptoms'] = symptoms
    clinical['symptomsLead'] = (
        projection.text('symptoms.lead') if symptoms else (DEGRADED_STATEMENT if degraded else legacy.NO_SYMPTOM_LEAD)
    )
    clinical['symptomSummary'] = (
        projection.text('symptoms.summary') if symptoms else (None if degraded else legacy.NO_SYMPTOM_SUMMARY)
    )
    clinical['symptomSummaryEvidenceRefs'] = []

    # Chapter 3
    program_groups = {group.get('code'): group for group in clinical.get('clinicalGroups') or ()}
    cited = set()
    groups = []
    for system in sorted(plan['systems'], key=lambda item: _CHAPTER_THREE_ORDER.get(item['code'], 9)):
        code = system['code']
        if code == 'microbiome' or not system['rows']:
            continue
        group_code = SYSTEM_GROUP.get(code, 'otherClinical')
        level = STATUS_LEVELS.get(system['status'])
        template_rows = [
            _table_row(projection, row, f'clinical.{code}.row.{row["index"]}', f'row{row["index"]}')
            for row in system['rows']
        ]
        for row in system['rows']:
            cited.update(
                entry['factId'] for handle in row['handles'] if (entry := sheet.entry(handle)) and entry.get('factId')
            )
        flat = [_public(row) for row in (program_groups.get(group_code) or {}).get('rows') or ()]
        if not flat:
            seen = set()
            flat = [
                row
                for template in template_rows
                for row in template['rows']
                if row.get('code') not in seen and not seen.add(row.get('code'))
            ]
        anchors, more = projection.anchors(system['keyHandles'], level)
        groups.append(
            {
                'code': group_code,
                'name': CHAPTER_THREE_NAMES.get(code) or system['name'] + '评估',
                'rows': flat,
                'templateRows': template_rows,
                'summary': projection.text(f'clinical.{code}.summary'),
                'calibrationNote': projection.text(f'clinical.{code}.calibration'),
                'status': system['status'],
                'level': level,
                'anchors': anchors,
                'moreCount': more,
                'evidenceRefs': [],
            }
        )
    clinical['clinicalGroups'] = groups
    clinical['specialtyFindings'] = [
        _public(row)
        for row in clinical.get('specialtyFindings') or ()
        if str(row.get('code') or '').split(':span:', 1)[0] not in cited
    ]
    has_clinical = sheet.has_clinical
    clinical['summary'] = projection.text('clinicalFrame.lead') if has_clinical else legacy.NO_CLINICAL_SUMMARY
    clinical['evidenceRefs'] = []
    clinical['specialtySummary'] = projection.text('clinicalFrame.notIncluded') if has_clinical else None
    clinical['specialtySummaryEvidenceRefs'] = []
    clinical['trendSummary'] = (projection.text('clinicalFrame.trend') if has_clinical else None) or (
        None if clinical.get('trends') else legacy.NO_TREND_STATEMENT
    )
    clinical['trendSummaryEvidenceRefs'] = []
    clinical['baselineSummary'] = (
        projection.text('clinicalFrame.baseline') if has_clinical else legacy.NO_BASELINE_SUMMARY
    )
    clinical['baselineSummaryEvidenceRefs'] = []

    # Chapter 4
    has_microbiome = sheet.has_microbiome
    digestive = clinical.get('digestiveContext') or []
    if not has_microbiome:
        # Without microbiome data chapter four only carries its statement; the
        # digestive examinations are already assessed in chapter three.
        clinical['digestiveContext'] = []
    elif digestive:
        for group in digestive:
            group['summary'] = projection.text(f'micro.digestive.{group.get("code")}')
            group['evidenceRefs'] = []
    else:
        clinical['digestiveContext'] = [
            {
                'code': 'digestiveNotRecorded',
                'name': '消化道相关检查',
                'rows': [],
                'summary': legacy.NO_DIGESTIVE_STATEMENT,
                'evidenceRefs': [],
            }
        ]
    for key, prefix in (
        ('structureGroups', 'micro.structure'),
        ('organismGroups', 'micro.organisms'),
        ('functionalGroups', 'micro.function'),
    ):
        for group in microbiome.get(key) or ():
            group['summary'] = projection.text(f'{prefix}.{group.get("code")}', required=True)
            group['evidenceRefs'] = []
    microbiome['overviewSummary'] = projection.text('micro.lead') if has_microbiome else NO_MICROBIOME_LEAD
    microbiome['overviewSummaryEvidenceRefs'] = []
    microbiome['sampleSummary'] = projection.text('micro.sample') if has_microbiome else None
    microbiome['sampleSummaryEvidenceRefs'] = []
    microbiome['fermentationSummary'] = projection.text('micro.fermentation') if has_microbiome else None
    microbiome['fermentationSummaryEvidenceRefs'] = []
    microbiome['structureSummary'] = projection.text('micro.structureSummary') if has_microbiome else None
    microbiome['structureSummaryEvidenceRefs'] = []
    models = legacy._reported_model_summary(parsed) if has_microbiome else None
    microbiome['riskSummary'] = (
        '\n\n'.join(filter(None, (projection.text('micro.risk') if has_microbiome else None, models))) or None
    )
    microbiome['riskSummaryEvidenceRefs'] = []

    # Chapter 5
    lifestyle = []
    for row in plan['lifestyle']:
        base = f'lifestyle.{row["dimension"]}'
        lifestyle.append(
            {
                'code': row['dimension'],
                'name': row['name'],
                'kind': 'habit',
                'value': projection.text(base + '.behavior', required=True),
                'interpretation': projection.text(base + '.effect'),
                'clusterIds': list(row['clusterIds']),
                'evidenceRefs': [],
            }
        )
    background = projection.text('lifestyle.background')
    if not background and not lifestyle:
        background = NO_QUESTIONNAIRE_LIFESTYLE if not sheet.has_questionnaire else legacy.NO_LIFESTYLE_SUMMARY
        if degraded:
            background = DEGRADED_STATEMENT
    recognitions = [
        {'code': f'recognition{item["index"]}', 'name': item['title'], 'text': text, 'evidenceRefs': []}
        for item in plan['recognitions']
        if (text := projection.text(f'lifestyle.recognition.{item["index"]}'))
    ]

    # Chapter 6
    by_priority = {
        priority: [cluster['id'] for cluster in clusters if cluster['priority'] == priority]
        for priority in ('P0', 'P1', 'P2')
    }
    layer_clusters = {'clinical': by_priority['P0'], 'core': by_priority['P1'], 'maintenance': by_priority['P2']}
    layers = [
        {
            'code': code,
            'name': name,
            'text': projection.text(f'conclusion.layer.{code}')
            or (DEGRADED_STATEMENT if degraded else legacy._LAYER_EMPTY[code]),
            'evidenceRefs': [],
            'clusterIds': layer_clusters[code],
        }
        for code, name in legacy._CONCLUSION_LAYERS
    ]
    directions = []
    has_urgent = bool(plan['priorityItems'])
    if has_microbiome:
        items = [
            {'code': code, 'name': name, 'text': text, 'evidenceRefs': []}
            for code, name in legacy._MICROBIOME_ITEMS
            if (text := projection.text(f'conclusion.{code}'))
        ]
        directions.append(
            {'name': legacy._MTPN_NAME, 'items': items, 'text': legacy._MTPN_AFTER_P0 if has_urgent else None}
        )
    else:
        directions.append({'name': legacy._MTPN_NAME, 'items': [], 'text': legacy._MTPN_NO_MICROBIOME})
    directions.append(
        {
            'code': 'lifestyle',
            'name': legacy._LSMI_NAME,
            'text': projection.text('conclusion.lifestyle') or legacy._LSMI_NO_FACTORS,
            'evidenceRefs': [],
        }
    )
    conclusion = {
        'summary': projection.text('conclusion.summary'),
        'evidenceRefs': [],
        'priorities': [
            {
                'id': cluster['id'],
                'name': cluster['name'],
                'priority': cluster['priority'],
                'action': cluster['action'],
                'evidenceRefs': cluster['evidenceRefs'],
            }
            for cluster in clusters
        ],
        'layers': layers,
        'managementDirections': directions,
    }

    legacy._strip_display_metadata(clinical)
    legacy._strip_display_metadata(microbiome)
    legacy._state_missing_clinical_data(overview, clinical)
    for node in (overview, clinical, microbiome, clusters):
        _clean_program_rows(node)
    presentation = legacy.AssessmentPresentation(
        overview=overview,
        clinical=clinical,
        microbiome=microbiome,
        clusters=clusters,
        clusterSummary=(
            projection.text('clusters.lead') if clusters else (DEGRADED_STATEMENT if degraded else legacy._NO_CLUSTERS)
        ),
        lifestyleFactors=lifestyle,
        lifestyleSummary=background,
        lifestyleRecognition=recognitions,
        conclusion=conclusion,
    )
    case['projection'] = {'version': CASE_PRESENTATION_VERSION, 'missingCells': sorted(set(projection.missing))}
    return presentation


def worst_level(levels):
    known = [level for level in levels if level in LEVEL_RANK]
    return max(known, key=LEVEL_RANK.get) if known else None


__all__ = ['CASE_PRESENTATION_VERSION', 'build_case_presentation', 'worst_level']
