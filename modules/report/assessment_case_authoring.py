"""Model-driven authoring of the six-chapter assessment.

Three steps, each a small number of model calls on the whole fact sheet:

1. ``analyze``: one call reads the complete case and decides the content
   (rows, grades, complaints, clusters, priority items, follow-ups, gaps).
2. ``write``: the sections are written in parallel from the same analysis.
3. ``review``: independent reviewers check the text against the fact sheet
   and return corrected cells.

The program only validates references, checks numbers and applies the
danger-signal safety net (``assessment_case_plan``).
"""

from __future__ import annotations

import asyncio
import re
import time
from decimal import ROUND_DOWN
from decimal import ROUND_HALF_UP
from decimal import Decimal
from decimal import InvalidOperation
from typing import Any

from .assessment_case_guides import ANALYSIS_METHOD
from .assessment_case_guides import ANALYSIS_SCHEMA
from .assessment_case_guides import CASE_GUIDE_VERSION
from .assessment_case_guides import REVIEW_GUIDE
from .assessment_case_guides import SECTION_GUIDES
from .assessment_case_guides import WRITING_GUIDE
from .assessment_case_plan import NOT_TESTED
from .assessment_case_plan import apply_priority_safety_net
from .assessment_case_plan import normalize_case_analysis
from .assessment_case_plan import plan_statistics
from .assessment_case_sheet import HANDLE
from .assessment_case_sheet import CaseSheet
from .assessment_case_sheet import number_forms
from .assessment_case_sheet import readable_units

ANALYZE_TASK = 'analyze_assessment_case'
WRITE_TASK = 'write_assessment_case_section'
REVIEW_TASK = 'review_assessment_case'
CASE_TASKS = frozenset({ANALYZE_TASK, WRITE_TASK, REVIEW_TASK})

# Share of the remaining time each step may use, and what must be left for it.
_ANALYSIS_SHARE = 0.45
_WRITE_SHARE = 0.7
_MIN_STEP_SECONDS = 20.0
# One analysis attempt; a second attempt must still fit into the analysis share.
_ANALYSIS_ATTEMPT_SECONDS = 240.0
_SECOND_PERSON = re.compile(r'您')
_MARKDOWN = re.compile(r'\*\*|__|`|^#{1,6}\s*', re.M)
_HANDLE_GROUP = re.compile(r'[（(]\s*(?:[LADQM]\d{1,4}\s*[、，,/–—~-]?\s*)+[）)]')
# Decimals and long integers that stand alone: not part of a test name (CA19-9, B12, HbA1c) or a longer number.
_CHECK_NUMBER = re.compile(r'(?<![A-Za-z0-9.])(?<![A-Za-z0-9]-)(?:\d+\.\d+|\d{3,})(?![0-9])(?!\.\d)')
_THOUSANDS = re.compile(r'(?<=\d),(?=\d{3}(?!\d))')
_PLAIN_NUMBER = re.compile(r'\d+(?:\.\d+)?')
_BACKSTAGE = ('分析底稿', '资料编号', '问卷显示', '体检显示', '主检', '原报告', '仅供参考')
# Chapters reviewed together; smaller groups run in parallel and finish sooner.
_REVIEW_GROUPS = (
    ('overview',),
    ('symptoms',),
    ('clinical', 'clinicalFrame'),
    ('micro',),
    ('clusters',),
    ('lifestyle', 'conclusion'),
)


class CaseAuthoringError(RuntimeError):
    """The whole-case analysis could not be produced."""


def _remaining(deadline) -> float:
    return deadline - time.monotonic()


async def _generate(client, payload, deadline, audit, label, *, attempt_seconds=None):
    """One model call bounded by the report deadline; timing goes to the audit."""
    started = time.monotonic()
    entry = {'label': label, 'task': payload['task']}
    audit.setdefault('calls', []).append(entry)
    budget = _remaining(deadline)
    if attempt_seconds is not None:
        budget = min(budget, attempt_seconds)
    if budget <= 1:
        entry['status'] = 'skipped_no_time'
        raise TimeoutError('no time left for ' + label)
    try:
        result = await asyncio.wait_for(client.generate(payload), timeout=budget)
    except Exception as error:
        entry.update(status='failed', error=type(error).__name__, seconds=round(time.monotonic() - started, 1))
        raise
    entry.update(status='ok', seconds=round(time.monotonic() - started, 1))
    return result


def _plan_for_model(plan) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key not in {'audit', 'version'}}


def analysis_payload(sheet: CaseSheet) -> dict[str, Any]:
    return {
        'task': ANALYZE_TASK,
        'instructions': '读完整份 caseSheet，按 method 对这位受检者做整体分析，按 outputSchema 返回一个 JSON 对象。',
        'method': list(ANALYSIS_METHOD),
        'caseSheet': sheet.text,
        'outputSchema': ANALYSIS_SCHEMA,
    }


def _cell(identifier, duty, limit, **context) -> dict[str, Any]:
    return {
        'id': identifier,
        'duty': duty,
        'maxChars': limit,
        **{key: value for key, value in context.items() if value not in (None, [], '')},
    }


def _row_cells(prefix, rows, duty, limit) -> list[dict]:
    return [
        _cell(
            f'{prefix}.{row["index"]}',
            duty,
            limit,
            title=row['title'],
            records=row['handles'],
            ideal=row.get('ideal'),
            status=row['status'],
        )
        for row in rows
    ]


def _group_cells(prefix, groups, duty, limit) -> list[dict]:
    cells = []
    for group in groups or ():
        if not group.get('code'):
            continue
        members = [
            f'{row.get("name")} {row.get("value")}{row.get("unit") or ""}'.strip()
            for row in (group.get('rows') or ())[:12]
            if row.get('name')
        ]
        cells.append(_cell(f'{prefix}.{group["code"]}', duty, limit, group=group.get('name'), members=members))
    return cells


def section_jobs(plan, sheet: CaseSheet, *, clinical=None, microbiome=None) -> list[dict[str, Any]]:
    """The writing jobs of one report: ``{'code', 'guide', 'cells'}`` per section."""
    jobs: list[dict[str, Any]] = []
    clinical, microbiome = clinical or {}, microbiome or {}

    cells = []
    if plan['bodyRows']:
        cells.append(_cell('overview.bodySummary', '表型判断与说明', 260))
        cells += _row_cells('overview.bodyRow', plan['bodyRows'], '生理与代谢意义', 110)
    for system in plan['systems']:
        if system['status'] == NOT_TESTED:
            continue
        cells.append(
            _cell(
                f'overview.system.{system["code"]}.evidence',
                '核心异常与关键实测值',
                140,
                system=system['name'],
                status=system['status'],
                records=system['keyHandles'],
            )
        )
        cells.append(
            _cell(
                f'overview.system.{system["code"]}.conclusion',
                '当前阶段评估结论',
                110,
                system=system['name'],
                status=system['status'],
            )
        )
    cells.append(_cell('overview.summary', '全局概述', 150))
    for item in plan['priorityItems']:
        cells.append(
            _cell(
                f'overview.priority.{item["index"]}',
                '优先排查的完整推理',
                280,
                title=item['title'],
                timeframe=item.get('timeframe'),
                department=item.get('department'),
                checks=item.get('checks'),
                records=item['handles'],
            )
        )
    for item in plan['followups']:
        cells.append(
            _cell(
                f'overview.followup.{item["index"]}',
                '常规专科随访',
                80,
                title=item['title'],
                department=item.get('department'),
                interval=item.get('interval'),
                checks=item.get('checks'),
                records=item['handles'],
            )
        )
    for item in plan['testGaps']:
        cells.append(
            _cell(
                f'overview.gap.{item["index"]}',
                '检测盲区',
                80,
                test=item['test'],
                because=item.get('because'),
                records=item['handles'],
            )
        )
    if plan['clusters']:
        cells.append(_cell('overview.graphSummary', '集群关系一句话', 120))
    jobs.append({'code': 'overview', 'guide': SECTION_GUIDES['overview'], 'cells': cells})

    if plan['symptoms']:
        cells = [_cell('symptoms.lead', '主诉概述', 140)]
        for item in plan['symptoms']:
            base = f'symptoms.{item["index"]}'
            context = {'symptom': item['name'], 'reported': item['handles']}
            cells.append(_cell(base + '.history', '持续时间与加重情境', 100, **context))
            # An evidence column without a record to cite stays empty instead of a filler sentence.
            if item['clinicalHandles']:
                cells.append(
                    _cell(
                        base + '.clinicalEvidence',
                        '临床检验与影像关联信号',
                        170,
                        records=item['clinicalHandles'],
                        **context,
                    )
                )
            if item['microbiomeHandles']:
                cells.append(
                    _cell(
                        base + '.microbiomeEvidence',
                        '菌群与功能代谢关联证据',
                        170,
                        records=item['microbiomeHandles'],
                        **context,
                    )
                )
            cells += [
                _cell(base + '.title', '解析的名词性标题', 30, **context),
                _cell(base + '.explanation', '成因解析', 360, **context),
            ]
        cells.append(_cell('symptoms.summary', '综合印证', 230))
        jobs.append({'code': 'symptoms', 'guide': SECTION_GUIDES['symptoms'], 'cells': cells})

    notes = [note for note in plan['dataNotes'] if note['kind'] == 'calibration']
    for system in plan['systems']:
        if system['code'] == 'microbiome' or not system['rows']:
            continue
        code = system['code']
        cells = _row_cells(f'clinical.{code}.row', system['rows'], '解读', 160)
        cells.append(_cell(f'clinical.{code}.summary', '表后小结', 190, system=system['name']))
        # A calibration note belongs to the system whose rows cite the reconciled records.
        cited = {handle for row in system['rows'] for handle in row['handles']}
        relevant = [note for note in notes if cited & set(note['handles'])]
        if relevant:
            cells.append(
                _cell(
                    f'clinical.{code}.calibration', '数据校准说明', 230, dataNotes=[note['note'] for note in relevant]
                )
            )
        jobs.append(
            {
                'code': f'clinical.{code}',
                'guide': SECTION_GUIDES['clinicalSystem'],
                'cells': cells,
                'system': system['name'],
            }
        )
    if sheet.has_clinical:
        jobs.append(
            {
                'code': 'clinicalFrame',
                'guide': SECTION_GUIDES['clinicalFrame'],
                'cells': [
                    _cell('clinicalFrame.lead', '第三章导语', 140),
                    _cell('clinicalFrame.notIncluded', '本次未纳入的专项检测', 170),
                    _cell('clinicalFrame.trend', '历次趋势或基线说明', 170),
                    _cell('clinicalFrame.baseline', '已确认的稳健基线', 260),
                ],
            }
        )

    if sheet.has_microbiome:
        cells = [
            _cell('micro.lead', '第四章导语', 190),
            _cell('micro.sample', '样本与采样前情况', 180),
            _cell('micro.fermentation', '发酵耐受情况', 180),
        ]
        cells += _group_cells('micro.digestive', clinical.get('digestiveContext'), '对菌群定植与消化吸收的影响', 130)
        cells += _group_cells('micro.structure', microbiome.get('structureGroups'), '宏观指数解读', 130)
        cells.append(_cell('micro.structureSummary', '整体生态状态', 190))
        jobs.append({'code': 'microbiomeStructure', 'guide': SECTION_GUIDES['microbiomeStructure'], 'cells': cells})
        cells = _group_cells('micro.organisms', microbiome.get('organismGroups'), '功能影响与调节意义', 180)
        if cells:
            jobs.append({'code': 'microbiomeOrganisms', 'guide': SECTION_GUIDES['microbiomeOrganisms'], 'cells': cells})
        cells = _group_cells('micro.function', microbiome.get('functionalGroups'), '与临床指标及不适的交叉印证', 180)
        cells.append(_cell('micro.risk', '疾病关联模型与临床结果', 250))
        jobs.append({'code': 'microbiomeFunctions', 'guide': SECTION_GUIDES['microbiomeFunctions'], 'cells': cells})

    if plan['clusters']:
        cells = [_cell('clusters.lead', '集群总述', 190)]
        for cluster in plan['clusters']:
            base = f'clusters.{cluster["id"]}'
            context = {'cluster': cluster['name'], 'priority': cluster['priority']}
            if cluster['clinicalHandles']:
                cells.append(
                    _cell(
                        base + '.clinicalEvidence', '临床与影像证据', 190, records=cluster['clinicalHandles'], **context
                    )
                )
            if cluster['microbiomeHandles']:
                cells.append(
                    _cell(
                        base + '.microbiomeEvidence',
                        '菌群与代谢证据',
                        190,
                        records=cluster['microbiomeHandles'],
                        **context,
                    )
                )
            cells += [
                _cell(base + '.impact', '对身体的影响与走向', 360, reported=cluster['reportedHandles'], **context),
                _cell(base + '.action', '处理方向', 160, **context),
            ]
        jobs.append({'code': 'clusters', 'guide': SECTION_GUIDES['clusters'], 'cells': cells})

    cells = []
    if plan['background']['handles'] or plan['protective']['handles']:
        cells.append(
            _cell(
                'lifestyle.background',
                '先天背景、既往事件与保护因素',
                260,
                background=plan['background']['handles'],
                protective=plan['protective']['handles'],
            )
        )
    for row in plan['lifestyle']:
        base = f'lifestyle.{row["dimension"]}'
        cells += [
            _cell(base + '.behavior', '受检者的行为特征', 110, dimension=row['name'], reported=row['handles']),
            _cell(base + '.effect', '正在维持的异常', 130, dimension=row['name'], clusters=row['clusterIds']),
        ]
    for item in plan['recognitions']:
        cells.append(
            _cell(
                f'lifestyle.recognition.{item["index"]}', '认知校准', 250, title=item['title'], records=item['handles']
            )
        )
    if cells:
        jobs.append({'code': 'lifestyle', 'guide': SECTION_GUIDES['lifestyle'], 'cells': cells})

    cells = [
        _cell('conclusion.layer.clinical', '第一层级：首月的专科排查、随访与补查', 300),
        _cell('conclusion.layer.core', '第二层级：90 天的核心干预', 300),
        _cell('conclusion.layer.maintenance', '第三层级：长期的营养储备与维持', 260),
    ]
    if sheet.has_microbiome:
        cells += [
            _cell('conclusion.probiotic', '功效菌选择依据', 180),
            _cell('conclusion.fiber', '益生元与膳食纤维方向', 180),
            _cell('conclusion.nutrient', '营养素方向与需避开的成分', 190),
        ]
    cells += [
        _cell('conclusion.lifestyle', '去诱因的生活方式方向', 300),
        _cell('conclusion.summary', '综合结论', 230),
    ]
    jobs.append({'code': 'conclusion', 'guide': SECTION_GUIDES['conclusion'], 'cells': cells})
    return [job for job in jobs if job['cells']]


def section_payload(job, plan, sheet: CaseSheet) -> dict[str, Any]:
    return {
        'task': WRITE_TASK,
        'instructions': (
            '按 writingGuide 和 sectionGuide 写出 cells 中每一格的正文。每格的 records、reported '
            '等是它应当使用的资料编号，'
            '到 caseSheet 里查对应的名称和数值；analysis 是已经确定的整体判断，各格必须与它一致。'
            '返回 {"cells": {"<id>": "正文"}}，键必须是给定的 id；按 sectionGuide 允许为空的格返回 null。'
        ),
        'writingGuide': list(WRITING_GUIDE),
        'caseSheet': sheet.text,
        'analysis': _plan_for_model(plan),
        'section': job['code'],
        'sectionGuide': list(job['guide']),
        'cells': job['cells'],
    }


def clean_cell_text(value) -> str | None:
    """Reader text without handles, Markdown marks or second-person address."""
    if not isinstance(value, str):
        return None
    text = _HANDLE_GROUP.sub('', value)
    text = HANDLE.sub('', text)
    text = _MARKDOWN.sub('', text)
    text = _SECOND_PERSON.sub('受检者', text)
    text = re.sub(r'[ \t]+', ' ', text)
    # A removed handle must not leave a space in front of punctuation.
    text = re.sub(r' +(?=[，。；：、！？,.;:!?）)])', '', text)
    text = re.sub(r'\s*\n\s*', '\n', text).strip()
    text = re.sub(r'([，、；：])\s*([，、；。：])', r'\2', text)
    text = re.sub(r'（\s*）|\(\s*\)', '', text)
    if not text or text.casefold() in {'null', 'none', '无', '（无）'}:
        return None
    return readable_units(text)


def _accept_cells(job, response) -> tuple[dict[str, str], list[str]]:
    wanted = {cell['id'] for cell in job['cells']}
    returned = response.get('cells') if isinstance(response, dict) else None
    if not isinstance(returned, dict):
        returned = response if isinstance(response, dict) else {}
    accepted = {}
    for identifier, value in returned.items():
        text = clean_cell_text(value)
        if identifier in wanted and text:
            accepted[identifier] = text
    missing = sorted(wanted - set(accepted) - {identifier for identifier, value in returned.items() if value is None})
    return accepted, missing


async def write_case_sections(client, plan, sheet: CaseSheet, *, clinical=None, microbiome=None, deadline, audit):
    """Write every section in parallel; a failed section is retried once."""
    jobs = section_jobs(plan, sheet, clinical=clinical, microbiome=microbiome)
    cells: dict[str, str] = {}
    limits = {cell['id']: cell['maxChars'] for job in jobs for cell in job['cells']}
    failed: list[str] = []
    incomplete: dict[str, list[str]] = {}

    async def write(job, attempt=1):
        try:
            response = await _generate(
                client,
                section_payload(job, plan, sheet),
                deadline,
                audit,
                f'write:{job["code"]}' + ('' if attempt == 1 else f':retry{attempt}'),
            )
        except Exception:
            if attempt == 1 and _remaining(deadline) > _MIN_STEP_SECONDS:
                return await write(job, 2)
            failed.append(job['code'])
            return
        accepted, missing = _accept_cells(job, response)
        mostly_missing = len(missing) > len(job['cells']) // 2
        if mostly_missing and attempt == 1 and _remaining(deadline) > _MIN_STEP_SECONDS:
            return await write(job, 2)
        cells.update(accepted)
        if missing:
            incomplete[job['code']] = missing

    await asyncio.gather(*(write(job) for job in jobs))
    audit['sections'] = {
        'jobs': [job['code'] for job in jobs],
        'failed': failed,
        'incomplete': incomplete,
        'cellCount': len(cells),
        'requestedCells': len(limits),
    }
    return cells, limits


def unverified_numbers(text: str, sheet: CaseSheet, allowed=frozenset()) -> list[str]:
    """Decimals and long integers of ``text`` that the fact sheet does not contain.

    A number is also verified when it is a recorded value written with fewer
    decimals, or the gap between a number of the same text and another number
    of the text, a planned target, or a bound of the record that the text quotes.
    """
    plain = _THOUSANDS.sub('', text or '')
    missing, quoted = [], None
    for token in _CHECK_NUMBER.findall(plain):
        forms = number_forms(token)
        if forms & sheet.numbers or forms & allowed or token in missing or _rounded_record(token, sheet.numbers):
            continue
        if quoted is None:
            quoted = _quoted_numbers(plain, sheet, allowed)
        if _is_stated_gap(_decimal(token), *quoted):
            continue
        missing.append(token)
    return missing


def _decimal(token):
    try:
        return Decimal(token).normalize()
    except InvalidOperation:
        return None


def _quoted_numbers(text: str, sheet: CaseSheet, allowed) -> tuple[set, set]:
    """Numbers of a text, and the bounds it may measure them against (targets, bounds of the quoted records)."""
    stated = {value for token in _PLAIN_NUMBER.findall(text) if (value := _decimal(token)) is not None}
    bounds = {value for token in allowed if (value := _decimal(token)) is not None}
    for entry in sheet.entries.values():
        value = _decimal(entry.get('value') or '')
        if value is None or value not in stated:
            continue
        for source in (entry.get('reference'), entry.get('target')):
            bounds.update(v for token in _PLAIN_NUMBER.findall(source or '') if (v := _decimal(token)) is not None)
    return stated, bounds


def _is_stated_gap(number, stated, bounds) -> bool:
    """Whether ``number`` is the gap between another number of the text and a further stated number or bound."""
    if number is None or not number:
        return False
    others = stated - {number}
    return any(abs(first - second) == number for first in others for second in (others | bounds) - {first, number})


def _rounded_record(token: str, recorded) -> bool:
    """Whether ``token`` is a recorded value written with fewer decimals (4.7804 as 4.78)."""
    if '.' not in token:
        return False
    places = len(token.split('.', 1)[1])
    try:
        target = Decimal(token)
    except InvalidOperation:
        return False
    quantum = Decimal(1).scaleb(-places)
    for number in recorded:
        if '.' not in number or len(number.split('.', 1)[1]) <= places:
            continue
        try:
            value = Decimal(number)
        except InvalidOperation:
            continue
        if (
            value.quantize(quantum, rounding=ROUND_HALF_UP) == target
            or value.quantize(quantum, rounding=ROUND_DOWN) == target
        ):
            return True
    return False


def _plan_numbers(plan) -> frozenset[str]:
    numbers: set[str] = set()

    def collect(value):
        if isinstance(value, str):
            for token in re.findall(r'\d+(?:\.\d+)?', value):
                numbers.update(number_forms(token))
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect({key: value for key, value in plan.items() if key != 'audit'})
    return frozenset(numbers)


def cell_issues(cells: dict[str, str], limits: dict[str, int], sheet: CaseSheet, plan) -> dict[str, dict]:
    """Program findings per cell: unverified numbers, backstage wording, overlong text."""
    allowed = _plan_numbers(plan)
    issues = {}
    for identifier, text in cells.items():
        found = {}
        numbers = unverified_numbers(text, sheet, allowed)
        if numbers:
            found['unverifiedNumbers'] = numbers
        words = [word for word in _BACKSTAGE if word in text]
        if words:
            found['backstageWords'] = words
        limit = limits.get(identifier)
        if limit and len(text) > limit * 1.6:
            found['overLength'] = {'length': len(text), 'maxChars': limit}
        if found:
            issues[identifier] = found
    return issues


def _strings(value) -> list:
    """A list of items; a single string is one item, not its characters."""
    if isinstance(value, str):
        return [value]
    return list(value) if isinstance(value, (list, tuple)) else []


def _resolve_cell_id(value, cells) -> str | None:
    """The cell a reviewer means; an abbreviated id is accepted when it names exactly one cell."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value in cells:
        return value
    matches = [identifier for identifier in cells if identifier.endswith('.' + value)]
    return matches[0] if len(matches) == 1 else None


def _review_payload(group, cells, issues, plan, sheet: CaseSheet) -> dict[str, Any] | None:
    selected = [
        {'id': identifier, 'text': text} for identifier, text in cells.items() if identifier.split('.', 1)[0] in group
    ]
    if not selected:
        return None
    flagged = {identifier: issue for identifier, issue in issues.items() if identifier.split('.', 1)[0] in group}
    return {
        'task': REVIEW_TASK,
        'instructions': (
            '按 reviewGuide 对照 caseSheet 和 analysis 复核 cells 里的成稿。programFindings 是程序发现的疑点，'
            '逐个处理。'
            '返回 {"issues": [{"id", "problem", "revised"}], "missedFindings": [{"title", "handles", "timeframe", '
            '"days", "department", "checks", "text"}]}。'
        ),
        'reviewGuide': list(REVIEW_GUIDE),
        'caseSheet': sheet.text,
        'analysis': _plan_for_model(plan),
        'reviewScope': list(group),
        'cells': selected,
        'programFindings': flagged,
    }


async def review_case(client, cells, limits, plan, sheet: CaseSheet, *, deadline, audit) -> dict[str, Any]:
    """Independent review of the written cells; corrected text replaces the draft."""
    issues = cell_issues(cells, limits, sheet, plan)
    result: dict[str, Any] = {
        'programFindings': issues,
        'revised': [],
        'rejected': [],
        'missedFindings': [],
        'failedGroups': [],
    }

    async def review(group):
        payload = _review_payload(group, cells, issues, plan, sheet)
        if payload is None:
            return
        try:
            response = await _generate(client, payload, deadline, audit, 'review:' + '+'.join(group))
        except Exception:
            result['failedGroups'].append(list(group))
            return
        for issue in response.get('issues') or () if isinstance(response, dict) else ():
            if not isinstance(issue, dict):
                continue
            identifier = _resolve_cell_id(issue.get('id'), cells)
            revised = clean_cell_text(issue.get('revised'))
            if identifier not in cells or not revised:
                result['rejected'].append({'id': identifier, 'reason': 'unknown_cell_or_empty_revision'})
                continue
            limit = limits.get(identifier) or len(cells[identifier])
            if len(revised) > max(limit * 1.6, len(cells[identifier]) * 1.3):
                result['rejected'].append({'id': identifier, 'reason': 'revision_too_long'})
                continue
            result['revised'].append({'id': identifier, 'problem': str(issue.get('problem') or '')[:200]})
            cells[identifier] = revised
        for finding in response.get('missedFindings') or () if isinstance(response, dict) else ():
            if isinstance(finding, dict) and sheet.valid(finding.get('handles')):
                result['missedFindings'].append(finding)

    await asyncio.gather(*(review(group) for group in _REVIEW_GROUPS))
    covered = {handle for item in plan['priorityItems'] for handle in item['handles']}
    for finding in result['missedFindings']:
        handles = sheet.valid(finding.get('handles'))
        text = clean_cell_text(finding.get('text'))
        title = str(finding.get('title') or '').strip()[:60]
        if not title or not text or set(handles) & covered:
            continue
        try:
            days = int(finding.get('days'))
        except (TypeError, ValueError):
            days = None
        index = len(plan['priorityItems']) + 1
        plan['priorityItems'].append(
            {
                'index': index,
                'title': title,
                'timeframe': str(finding.get('timeframe') or '').strip()[:30] or None,
                'days': days if days and days > 0 else None,
                'department': str(finding.get('department') or '').strip()[:30] or None,
                'checks': [str(check)[:40] for check in _strings(finding.get('checks')) if str(check).strip()][:6],
                'handles': handles,
                'clusterId': None,
                'origin': 'review',
            }
        )
        cells[f'overview.priority.{index}'] = text
        covered.update(handles)
    result['remainingFindings'] = cell_issues(cells, limits, sheet, plan)
    audit['review'] = result
    return result


async def author_assessment_case(
    client, sheet: CaseSheet, *, clinical=None, microbiome=None, flags=None, deadline, audit
) -> dict[str, Any]:
    """Analyse, write and review one case. Raises ``CaseAuthoringError`` without an analysis."""
    audit['guideVersion'] = CASE_GUIDE_VERSION
    started = time.monotonic()
    analysis_deadline = min(deadline, started + max(_MIN_STEP_SECONDS, _remaining(deadline) * _ANALYSIS_SHARE))
    raw = plan = None
    for attempt in (1, 2):
        try:
            raw = await _generate(
                client,
                analysis_payload(sheet),
                analysis_deadline,
                audit,
                'analysis' if attempt == 1 else 'analysis:retry',
                attempt_seconds=_ANALYSIS_ATTEMPT_SECONDS,
            )
        except Exception as error:
            audit['analysisError'] = type(error).__name__
            raw = None
        if isinstance(raw, dict):
            plan = normalize_case_analysis(raw, sheet, clinical=clinical)
            # An analysis that placed no examination record in any table is not usable.
            placed = [*plan['bodyRows'], *(row for system in plan['systems'] for row in system['rows'])]
            if not sheet.has_clinical or any(row.get('origin') == 'model' for row in placed):
                break
            audit['analysisError'] = 'no_clinical_rows'
        raw = plan = None
        if _remaining(analysis_deadline) < _MIN_STEP_SECONDS:
            break
    if plan is None:
        raise CaseAuthoringError('whole-case analysis unavailable')
    apply_priority_safety_net(plan, sheet, flags)
    audit['plan'] = plan_statistics(plan)
    audit['analysisSeconds'] = round(time.monotonic() - started, 1)

    phase = time.monotonic()
    write_deadline = min(deadline, phase + max(_MIN_STEP_SECONDS, _remaining(deadline) * _WRITE_SHARE))
    cells, limits = await write_case_sections(
        client, plan, sheet, clinical=clinical, microbiome=microbiome, deadline=write_deadline, audit=audit
    )
    audit['writeSeconds'] = round(time.monotonic() - phase, 1)
    phase = time.monotonic()
    if _remaining(deadline) > _MIN_STEP_SECONDS:
        await review_case(client, cells, limits, plan, sheet, deadline=deadline, audit=audit)
    else:
        audit['review'] = {'skipped': 'no_time', 'programFindings': cell_issues(cells, limits, sheet, plan)}
    audit['reviewSeconds'] = round(time.monotonic() - phase, 1)
    audit['totalSeconds'] = round(time.monotonic() - started, 1)
    return {'plan': plan, 'cells': cells, 'limits': limits, 'raw': raw}
