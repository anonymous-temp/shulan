"""Program rule conclusions as a citable source (R) of writing and source review.

Three deterministic tables decide content that report cells only phrase: the
danger-signal rules (P0 items and routine follow-ups, ``assessment_red_flags``),
the test-gap rules (``assessment_test_gaps``) and the evidence-based target
ranges (``assessment_targets.json`` selected by ``assessment_clinical_rules``).

A cell may cite the conclusions of the rules that matched inside its own scope
(1.3 and the first layer of 6.1, the P0 cluster that carries an item, and a
chapter-3 row with an applicable target). Each conclusion is handed to the
writer and to the independent source review as an ``R`` handle that carries its
rule id, conclusion text, time limit, department, checks, test and reason or
target range, the cell's own trigger records and the rule's literature.

An R supports only what the rule decided. Patient findings, values and dates
still need the bound records (F/O); the deterministic checks here keep a cited
paragraph within the time limits, departments, procedures and target values of
its R (or of the original advice A and records it also uses). The rule
literature enters the report bibliography after the final renumbering, in the
same way as the target-table literature.
"""

from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from decimal import Decimal
from decimal import InvalidOperation

from .assessment_measurement_claims import action_terms
from .assessment_red_flags import _DEPARTMENTS as _RULE_DEPARTMENTS

RULE_HANDLE = re.compile(r'^R\d{2}$')
# Keys that name a program default or the original advice, not literature.
_NON_LITERATURE = frozenset({'program_default', 'original_advice'})
_ORIGINAL_ADVICE_ORIGIN = 'original_advice'
_PRIORITY_SCOPES = {
    'priority.urgent': ('priority',),
    'priority.routine': ('routine',),
    'priority.gaps': ('gaps',),
    'layer.clinical': ('priority', 'routine', 'gaps'),
}
# Emergency wording is a general safety instruction, not a rule department.
_DEPARTMENT_VOCABULARY = tuple(
    sorted(
        {*(name for name in _RULE_DEPARTMENTS if name != '急诊'), '内分泌代谢科', '心血管科', '肝胆科', '胃肠科'},
        key=lambda name: (-len(name), name),
    )
)
_DEPARTMENT_MENTION = re.compile('|'.join(re.escape(name) for name in _DEPARTMENT_VOCABULARY))
_RANGE_TIME = re.compile(
    r'(?P<low>\d+(?:\.\d+)?)\s*[-–—~～至到]\s*(?P<high>\d+(?:\.\d+)?)\s*(?P<u>个?月|年|周|天)',
)
_NUMBER = re.compile(r'(?<![A-Za-z\d.])\d+(?:\.\d+)?(?![A-Za-z\d])')
_STATEMENT = re.compile(r'[^。！？；;\n]+[。！？；;\n]*')
_TARGET_STATEMENT = re.compile(r'目标')


def is_rule_handle(value):
    return isinstance(value, str) and bool(RULE_HANDLE.match(value))


def rule_handles(values):
    return [value for value in values or () if is_rule_handle(value)]


def passage_handles(values):
    return [value for value in values or () if isinstance(value, str) and not is_rule_handle(value)]


def _text(value):
    return str(value).strip() if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ''


def _strings(value):
    if isinstance(value, (str, dict)):
        value = [value]
    result = []
    for item in value if isinstance(value, (list, tuple)) else ():
        if isinstance(item, dict):
            item = item.get('name') or item.get('test') or item.get('title') or item.get('text')
        if isinstance(item, str) and item.strip() and item.strip() not in result:
            result.append(item.strip())
    return result


def _period(value):
    if not isinstance(value, dict) or not _text(value.get('text')):
        return None
    period = {'text': _text(value['text'])}
    if isinstance(value.get('days'), int) and not isinstance(value.get('days'), bool):
        period['days'] = value['days']
    if _text(value.get('basis')):
        period['basis'] = _text(value['basis'])
    return period


def _literature(sources):
    """Citable rule sources; program defaults and original advice are not literature."""
    result = []
    for source in sources or ():
        if not isinstance(source, dict) or source.get('key') in _NON_LITERATURE or not _text(source.get('title')):
            continue
        row = {
            key: deepcopy(source[key])
            for key in ('key', 'title', 'publisher', 'citation', 'journal', 'year', 'doi', 'url')
            if source.get(key) not in (None, '')
        }
        if row not in result:
            result.append(row)
    return result


def _ids(row, *fields):
    return list(dict.fromkeys(fid for field in fields for fid in (row.get(field) or ()) if isinstance(fid, str)))


def _priority_entry(row):
    title = _text(row.get('title') or row.get('name'))
    timeframe, department, checks = (
        _period(row.get('timeframe')),
        _text(row.get('department')),
        _strings(row.get('checks')),
    )
    if not title or not (timeframe or department or checks):
        return None
    parts = [timeframe['text'] if timeframe else '', f'科室：{department}' if department else '']
    parts.append(f'需完成：{"、".join(checks)}' if checks else '')
    entry = {
        'kind': 'priority_item',
        'ruleId': _text(row.get('ruleId') or row.get('code')),
        'code': _text(row.get('code') or row.get('ruleId')),
        'title': title,
        'conclusion': f'{title}：' + '；'.join(part for part in parts if part) + '。',
        'factIds': _ids(row, 'factIds'),
        'sources': _literature(row.get('sources')),
    }
    if timeframe:
        entry['timeframe'] = timeframe
        if timeframe.get('basis') == 'program_default':
            entry['basisNote'] = '出处只要求及时处理，时限取规则表默认档位'
    if department:
        entry['department'] = department
    if checks:
        entry['checks'] = checks
    return entry


def _routine_entry(row):
    title = _text(row.get('title') or row.get('name'))
    interval, department = _period(row.get('interval') or row.get('timeframe')), _text(row.get('department'))
    checks = _strings(row.get('checks'))
    if not title or not (interval or department):
        return None
    parts = [interval['text'] if interval else '', f'科室：{department}' if department else '']
    parts.append(f'复查：{"、".join(checks)}' if checks else '')
    entry = {
        'kind': 'routine_followup',
        'ruleId': _text(row.get('ruleId') or row.get('code')),
        'code': _text(row.get('code') or row.get('ruleId')),
        'title': title,
        'conclusion': f'{title}：' + '；'.join(part for part in parts if part) + '。',
        'factIds': _ids(row, 'factIds'),
        'sources': _literature(row.get('sources')),
    }
    if interval:
        entry['interval'] = interval
    if department:
        entry['department'] = department
    if checks:
        entry['checks'] = checks
    return entry


def _gap_entry(row):
    test = _text(row.get('test') or row.get('title') or row.get('name'))
    if not test:
        return None
    reason = _text(row.get('reason'))
    entry = {
        'kind': 'test_gap',
        'ruleId': _text(row.get('ruleId') or row.get('code')),
        'code': _text(row.get('code') or row.get('ruleId')),
        'test': test,
        'conclusion': f'建议补查{test}' + (f'：{reason}' if reason else '。'),
        'factIds': _ids(row, 'triggeredBy', 'factIds'),
        'sources': _literature(row.get('sources')),
    }
    if reason:
        entry['reason'] = reason
    if _text(row.get('finding')):
        entry['finding'] = _text(row['finding'])
    return entry


def _target_entry(claim):
    label, text = _text(claim.get('label')), _text(claim.get('reference'))
    if not label or not text or claim.get('authority', 'target') != 'target':
        return None
    entry = {
        'kind': 'target',
        'ruleId': _text(claim.get('targetId')) or label,
        'code': _text(claim.get('targetId')) or label,
        'label': label,
        'target': text,
        'conclusion': f'{label} 目标区间 {text}。',
        'factIds': _ids(claim, 'recordIds'),
        'sources': _literature(claim.get('sources')),
    }
    if isinstance(claim.get('applicability'), (dict, str)) and claim['applicability']:
        entry['applicability'] = deepcopy(claim['applicability'])
    return entry


def _plan_rows(plan, key):
    rows = plan.get(key) if isinstance(plan, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def cell_rule_conclusions(scope_code, plan, *, comparison_claims=(), scope_fact_ids=None):
    """Rule conclusions admitted to one cell, with the cell's own trigger records.

    Only rules that matched inside the cell scope are admitted: 1.3 items to
    their section and the first layer of 6.1, the P0 items of a P0 cluster to
    that cluster's 5.1 cells, and applicable targets to their chapter-3 row.
    Items that only restate original advice stay with the advice (A). A rule
    whose trigger records lie outside the cell's scope is not admitted.
    """
    if not isinstance(plan, dict) or not isinstance(scope_code, str) or not scope_code:
        return []
    builders = {
        'priority': lambda: [
            _priority_entry(row)
            for row in _plan_rows(plan, 'priorityItems')
            if row.get('priority') in (None, '', 'P0') and row.get('origin') != _ORIGINAL_ADVICE_ORIGIN
        ],
        'routine': lambda: [
            _routine_entry(row)
            for row in _plan_rows(plan, 'routineFollowups')
            if row.get('origin') != _ORIGINAL_ADVICE_ORIGIN
        ],
        'gaps': lambda: [_gap_entry(row) for row in _plan_rows(plan, 'testGaps')],
    }
    entries = []
    if scope_code in _PRIORITY_SCOPES:
        for kind in _PRIORITY_SCOPES[scope_code]:
            entries.extend(builders[kind]())
    elif scope_code.startswith(('synthesis.', 'synthesisAction.')):
        code = scope_code.partition('.')[2]
        cluster = next((row for row in _plan_rows(plan, 'clusters') if row.get('code') == code), {})
        owned = set(_strings(cluster.get('priorityItemCodes')))
        if cluster.get('priority') == 'P0' and owned:
            entries.extend(
                _priority_entry(row)
                for row in _plan_rows(plan, 'priorityItems')
                if row.get('origin') != _ORIGINAL_ADVICE_ORIGIN and {row.get('code'), row.get('ruleId')} & owned
            )
    elif scope_code.startswith('clinicalRow.'):
        entries.extend(_target_entry(claim) for claim in comparison_claims or () if isinstance(claim, dict))
    scope = None if scope_fact_ids is None else set(scope_fact_ids)
    admitted, seen = [], set()
    for entry in entries:
        if entry is None:
            continue
        identity = (entry['kind'], entry['code'], entry.get('title') or entry.get('test') or entry.get('label'))
        if identity in seen:
            continue
        if scope is not None and entry['factIds']:
            inside = [fid for fid in entry['factIds'] if fid in scope]
            if not inside:
                continue
            entry['factIds'] = inside
        seen.add(identity)
        admitted.append(entry)
    return admitted


def rule_conclusion_map(entries):
    """``{R01: entry, ...}`` in the cell's stable order."""
    return {f'R{index:02d}': entry for index, entry in enumerate(entries or (), 1) if isinstance(entry, dict)}


def model_rule_conclusions(entries, handles):
    """Writer/reviewer view: R handles and F handles instead of private identifiers."""
    result = []
    for rid, entry in rule_conclusion_map(entries).items():
        row = {'id': rid}
        for key in (
            'kind',
            'ruleId',
            'title',
            'test',
            'label',
            'conclusion',
            'timeframe',
            'interval',
            'department',
            'checks',
            'reason',
            'finding',
            'target',
            'applicability',
            'basisNote',
        ):
            if entry.get(key) not in (None, '', []):
                row[key] = deepcopy(entry[key])
        row['recordIds'] = [handles[fid] for fid in entry.get('factIds', ()) if fid in handles]
        row['sources'] = [
            {key: source[key] for key in ('title', 'citation', 'journal', 'year') if source.get(key)}
            for source in entry.get('sources', ())
        ]
        result.append(row)
    return result


def _normal(text):
    return unicodedata.normalize('NFKC', str(text or '')).casefold()


def _intervals(texts):
    found = set()
    for text in texts:
        if not text:
            continue
        found |= action_terms(text)[2]
        for match in _RANGE_TIME.finditer(_normal(text)):
            for amount in (match['low'], match['high']):
                found |= action_terms(f'{amount}{match["u"]}内')[2]
    return found


def _departments(text):
    return set(_DEPARTMENT_MENTION.findall(_normal(text)))


def rule_action_issue(candidate, rules, advice=()):
    """Deterministic check of one patient-action claim against its selected R (and A).

    The claim may not add or change a specialty, a recognized procedure, a time
    limit or a department named by the selected rule conclusions and advice.
    """
    term = candidate.get('term') or ''
    rule_texts = [
        ' '.join(
            filter(
                None,
                (
                    rule.get('conclusion'),
                    rule.get('department'),
                    '、'.join(rule.get('checks') or ()),
                    (rule.get('timeframe') or {}).get('text'),
                    (rule.get('interval') or {}).get('text'),
                    rule.get('test'),
                ),
            )
        )
        for rule in rules
    ]
    advice_texts = [str(item.get('sourceQuote') or '') for item in advice]
    if candidate.get('sourceAttribution') and not advice:
        return '规则结论不是原医嘱，不能写作按原检查建议或原医生安排；原医嘱部分另给精确A。'
    union = '；'.join(text for text in (*rule_texts, *advice_texts) if text)
    specialties, procedures, _ = action_terms(term)
    allowed_specialties, allowed_procedures, _ = action_terms(union)
    if not specialties <= allowed_specialties or not _departments(term) <= _departments(union):
        return '转述的就诊科室不在所选规则结论R或所选A中，不能改动或新增科室。'
    if not procedures <= allowed_procedures:
        return '转述的检查项目不在所选规则结论R或所选A中，不能新增检查。'
    intervals = action_terms(term)[2]
    if intervals and not intervals <= _intervals([*rule_texts, *advice_texts]):
        return '转述的时限或复查间隔与所选规则结论R或所选A不一致，不能改动规则时限。'
    return None


def _decimal(value):
    try:
        return Decimal(value).normalize()
    except (InvalidOperation, ValueError):
        return None


def _numbers(text):
    return {number for number in (_decimal(item) for item in _NUMBER.findall(_normal(text))) if number is not None}


def _rule_text(rule):
    return json.dumps(
        {key: rule.get(key) for key in ('conclusion', 'timeframe', 'interval', 'department', 'checks', 'target')},
        ensure_ascii=False,
    )


def rule_paragraph_issues(part, rules, *, unknown=(), corpus=()):
    """Literal checks of one immutable P that cites R handles.

    ``rules`` are the R entries this P cites; ``unknown`` are R handles it cites
    that are not admitted to the cell; ``corpus`` is the P's own original
    advice, record text and cited passages. Returns reader-facing issue strings; the label in
    「」 names the rejected wording for sentence retention.
    """
    issues = [f'引用的规则编号「{rid}」不属于本格范围内的规则结论，不能引用。' for rid in unknown]
    if not rules:
        return issues
    bound = set(part.get('recordIds') or ())
    for rid, rule in rules.items():
        triggers = set(rule.get('recordIds') or ())
        if triggers and not triggers & bound:
            issues.append(
                f'{rid}的触发记录未在本段绑定；受检者的发现和测值须由本段F支持，规则结论只支持其时限、科室、检查等安排。'
            )
    text = (part.get('title') + '：' if part.get('title') else '') + part['text']
    rule_texts = [_rule_text(rule) for rule in rules.values()]
    allowed_text = '；'.join([*rule_texts, *corpus])
    allowed_intervals = _intervals([*rule_texts, *corpus])
    allowed_departments = _departments(allowed_text)
    for statement in _STATEMENT.findall(text):
        intervals = action_terms(statement)[2]
        if intervals and not intervals <= allowed_intervals:
            match = re.search(
                r'(?:每隔|间隔|每|满)?\s*(?:\d+|[一二两三四五六七八九十半]+)\s*(?:个?月|年|周|天|小时)\s*(?:内|后)?',
                statement,
            )
            label = match.group().strip() if match else statement.strip(' 。；;')
            issues.append(f'时限「{label}」与所引规则结论及原记录均不一致，不能改动或新增规则给出的时限。')
        for department in sorted(_departments(statement) - allowed_departments):
            issues.append(f'科室「{department}」不在所引规则结论或原记录中，不能改动或新增就诊科室。')
    targets = [rule for rule in rules.values() if rule.get('kind') == 'target']
    if targets:
        allowed_numbers = _numbers(allowed_text)
        for statement in _STATEMENT.findall(text):
            if not _TARGET_STATEMENT.search(statement):
                continue
            for raw in _NUMBER.findall(_normal(statement)):
                number = _decimal(raw)
                if number is not None and number not in allowed_numbers:
                    issues.append(f'目标数值「{raw}」与所引目标区间及本段记录不一致，目标值只照用所引R。')
                    break
    return list(dict.fromkeys(issues))


def sentence_rules(owner, cite):
    """Rule conclusions a published sentence cites, with their literature."""
    rules = rule_conclusion_map((owner or {}).get('ruleConclusions'))
    result = []
    for rid in rule_handles(cite):
        entry = rules.get(rid)
        if entry is None:
            continue
        row = {'ruleId': entry.get('ruleId'), 'kind': entry.get('kind'), 'sources': deepcopy(entry.get('sources', []))}
        if row not in result:
            result.append(row)
    return result


_RULE_SECTIONS = {
    'priority_item': 'assessmentPresentation.rule.priority',
    'routine_followup': 'assessmentPresentation.rule.routine',
    'test_gap': 'assessmentPresentation.rule.testGap',
    'target': 'assessmentPresentation.clinical.target',
}


def attach_rule_references(data, blueprint, accepted):
    """Give each published text the bibliography numbers of the rules it cites.

    Runs after the final renumbering of the report bibliography, like the
    target-table literature: a rule source reuses an existing entry with the
    same DOI or title, or is appended with the next free number. Repeated runs
    on the same data add nothing new.
    """
    from .assessment_clinical_view import cite_reference
    from .assessment_clinical_view import reference_index
    from .evaluation_report import _node

    appendices = getattr(data, 'appendices', None)
    library = getattr(appendices, 'reference_literature', None)
    if library is None:
        return []
    targets = {target['id']: target for chapter in blueprint.chapters for target in chapter['targets']}
    numbers = reference_index(library)
    added = []
    for block in accepted or ():
        target = targets.get(block.get('targetId')) if isinstance(block, dict) else None
        if target is None:
            continue
        cited = [
            (rule.get('kind'), source)
            for sentence in block.get('sentences') or ()
            if isinstance(sentence, dict)
            for rule in sentence.get('rules') or ()
            for source in rule.get('sources') or ()
        ]
        if not cited:
            continue
        try:
            node = _node(data, target['path'][:-1])
        except (AttributeError, KeyError, IndexError, TypeError):
            continue
        if not hasattr(node, 'evidence_refs'):
            continue
        refs = [
            cite_reference(
                library,
                numbers,
                source,
                study_type='指南' if kind != 'target' else None,
                section=_RULE_SECTIONS.get(kind, 'assessmentPresentation.rule'),
                added=added,
            )
            for kind, source in cited
        ]
        node.evidence_refs = list(dict.fromkeys([*node.evidence_refs, *refs]))
    return added


__all__ = [
    'RULE_HANDLE',
    'attach_rule_references',
    'cell_rule_conclusions',
    'is_rule_handle',
    'model_rule_conclusions',
    'passage_handles',
    'rule_action_issue',
    'rule_conclusion_map',
    'rule_handles',
    'rule_paragraph_issues',
    'sentence_rules',
]
