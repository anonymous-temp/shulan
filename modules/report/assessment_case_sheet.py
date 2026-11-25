"""One numbered fact sheet of a whole case for the model-driven assessment.

The sheet is the only patient material the case analysis and the chapter
writers see: every examination record, recorded conclusion, questionnaire
answer and microbiome result gets a short handle (``L12``, ``A3``, ``D2``,
``Q7``, ``M41``). The model refers to records only by these handles, so the
program can check each reference and attach the structured display rows the
frontend draws. Nothing is filtered by hand-written medical rules here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from typing import Any

HANDLE = re.compile(r'(?<![A-Za-z0-9])[LADQM]\d{1,4}(?![A-Za-z0-9])')
_SECTION = re.compile(r'检查类别：([^；;]+)')
_CONCLUSION_SECTION = re.compile(r'主检|总结与建议|体检结论|总检')
_CONCLUSION_PREFIX = re.compile(r'^(?:主检结论|体检结论|总检结论)[：:]\s*')
# Every digit run of the sheet counts as recorded (a superset keeps the check free of false alarms).
_NUMBER = re.compile(r'\d+(?:\.\d+)?')
# Normal microbiome members are listed without handles; the model may quote them by name.
_NORMAL_MEMBER_LIMIT = 40


@dataclass
class CaseSheet:
    """Fact sheet text plus the lookup tables the projection needs."""

    text: str
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    subject: dict[str, Any] = field(default_factory=dict)
    has_clinical: bool = False
    has_questionnaire: bool = False
    has_microbiome: bool = False
    numbers: frozenset[str] = frozenset()

    def entry(self, handle: str) -> dict[str, Any] | None:
        return self.entries.get(handle)

    def handles(self, kind: str | None = None) -> list[str]:
        return [key for key, row in self.entries.items() if kind is None or row['kind'] == kind]

    def valid(self, handles, kinds=None) -> list[str]:
        """Existing handles in first-seen order, optionally limited to some kinds."""
        result = []
        for handle in handles or ():
            handle = str(handle).strip()
            row = self.entries.get(handle)
            if row is None or handle in result or (kinds and row['kind'] not in kinds):
                continue
            result.append(handle)
        return result


def _clean(value) -> str:
    return '' if value is None else re.sub(r'\s+', ' ', str(value)).strip()


_SAME_CYCLE_DAYS = 183


_EXPONENT = re.compile(r'(?<=\d)\s*[×xX*]?\s*10\s*(?:\^|[Ee])\s*(\d{1,2})(?=\s*/)')
_SUPERSCRIPT = str.maketrans('0123456789', '⁰¹²³⁴⁵⁶⁷⁸⁹')


def readable_units(text: str) -> str:
    """Prose form of a count unit copied from a laboratory sheet: ``4.1 10E9/L`` and ``4.1×10^9/L`` as ``4.1×10⁹/L``."""
    return _EXPONENT.sub(lambda match: '×10' + match[1].translate(_SUPERSCRIPT), text)


def _plain_name(name) -> str:
    return re.sub(r'[\W_]+', '', str(name or ''))


def same_reading(first, second) -> bool:
    """Whether two records carry one reading under equivalent names.

    The same measurement is often recorded twice (a general examination and a
    device printout: "体重指数(BMI) 29.3" and "体重指数 BMI 29.3"). Only numeric
    results count; ``first`` and ``second`` need ``name``, ``value`` and ``unit``.
    """
    value = str(first.get('value') or '').strip()
    if not re.search(r'\d', value) or value != str(second.get('value') or '').strip():
        return False
    if (first.get('unit') or '') != (second.get('unit') or ''):
        return False
    one, other = _plain_name(first.get('name')), _plain_name(second.get('name'))
    return bool(one and other and (one.startswith(other) or other.startswith(one)))


def number_forms(token: str) -> set[str]:
    """Spellings of one number that count as the same recorded value."""
    forms = {token}
    if '.' in token:
        stripped = token.rstrip('0').rstrip('.')
        forms.add(stripped)
        try:
            forms.add(str(float(token)))
        except ValueError:
            pass
    else:
        forms.add(token + '.0')
    return forms


def text_numbers(text: str) -> set[str]:
    numbers = set()
    for token in _NUMBER.findall(text or ''):
        numbers.update(number_forms(token))
    return numbers


def _walk_rows(node):
    if isinstance(node, dict):
        if 'code' in node and 'name' in node and 'value' in node:
            yield node
        for value in node.values():
            yield from _walk_rows(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_rows(value)


def _clinical_rows_by_fact(clinical) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {}
    seen = set()
    for key in (
        'bodyMetrics',
        'bodyGroups',
        'clinicalGroups',
        'specialtyFindings',
        'normalBaselines',
        'digestiveContext',
    ):
        for row in _walk_rows(clinical.get(key) or ()):
            code = str(row.get('code') or '')
            fact_id = code.split(':span:', 1)[0]
            if not fact_id or id(row) in seen or (fact_id, code) in seen:
                continue
            seen.add(id(row))
            seen.add((fact_id, code))
            rows.setdefault(fact_id, []).append(row)
    return rows


def _days_between(first, second) -> int | None:
    try:
        return abs((date.fromisoformat(str(second)[:10]) - date.fromisoformat(str(first)[:10])).days)
    except (TypeError, ValueError):
        return None


def _line(*cells) -> str:
    return '\t'.join(_clean(cell) for cell in cells)


def _target_text(rows) -> str:
    for row in rows:
        target = row.get('target')
        if isinstance(target, dict) and target.get('text'):
            return _clean(target['text'])
    return ''


def _subject(parsed, request) -> dict[str, Any]:
    subject = getattr(parsed, 'subject', None)
    sex = _clean(getattr(subject, 'sex', None) or getattr(request, 'sex', None) or getattr(request, 'gender', None))
    age = _clean(getattr(subject, 'age', None) or getattr(request, 'age', None))
    return {'sex': sex or None, 'age': age or None}


def _questionnaires(request) -> list[tuple[str, str, str]]:
    """(questionnaire title, question, answer) for every answered item of the request."""
    result = []

    def text_of(answer, choices):
        values = answer if isinstance(answer, list) else [answer]
        labels = []
        for value in values:
            if value is None or value == '':
                continue
            labels.append(_clean(choices.get(str(value), value)))
        return '、'.join(label for label in labels if label)

    def collect(schema, answer, submitted=None):
        if not isinstance(schema, dict):
            return
        if isinstance(answer, dict) and isinstance(answer.get('answers'), dict):
            answer = answer['answers']
        if not isinstance(answer, dict):
            return
        title = _clean(schema.get('title')) or '健康问卷'
        if submitted:
            title += f'（提交时间 {_clean(submitted)}）'
        for page in schema.get('pages') or ():
            for element in (page or {}).get('elements') or ():
                name = (element or {}).get('name')
                if name not in answer:
                    continue
                choices = {}
                for choice in element.get('choices') or ():
                    if isinstance(choice, dict):
                        choices[str(choice.get('value'))] = choice.get('text') or choice.get('value')
                    else:
                        choices[str(choice)] = choice
                reply = text_of(answer[name], choices)
                if reply:
                    result.append((title, _clean(element.get('title')) or _clean(name), reply))

    surveys = getattr(request, 'survey_list', None)
    if surveys is not None:
        for survey in surveys:
            schema = getattr(survey, 'schema_', None) or getattr(survey, 'schema', None)
            if callable(schema):
                schema = None
            if schema is None and isinstance(survey, dict):
                schema = survey.get('schema')
            answer = getattr(survey, 'answer', None) if not isinstance(survey, dict) else survey.get('answer')
            submitted = (
                getattr(survey, 'submitted_at', None) if not isinstance(survey, dict) else survey.get('submittedAt')
            )
            if hasattr(schema, 'model_dump'):
                schema = schema.model_dump(mode='json', by_alias=True)
            if hasattr(answer, 'model_dump'):
                answer = answer.model_dump(mode='json', by_alias=True)
            collect(schema, answer, submitted)
    else:
        for schema_name, answer_name in (
            ('init_survey_schema', 'init_survey_answer'),
            ('advanced_survey_schema', 'advanced_survey_answer'),
        ):
            schema, answer = getattr(request, schema_name, None), getattr(request, answer_name, None)
            if hasattr(schema, 'model_dump'):
                schema = schema.model_dump(mode='json', by_alias=True)
            if hasattr(answer, 'model_dump'):
                answer = answer.model_dump(mode='json', by_alias=True)
            collect(schema, answer)
    return result


def build_case_sheet(
    *, request, parsed, document, clinical, microbiome, disease_models: str | None = None, timing: dict | None = None
) -> CaseSheet:
    """Number every record of the case and render the sheet the model reads."""
    entries: dict[str, dict[str, Any]] = {}
    counters = {'L': 0, 'A': 0, 'D': 0, 'Q': 0, 'M': 0}

    def add(prefix, kind, **values) -> str:
        counters[prefix] += 1
        handle = f'{prefix}{counters[prefix]}'
        entries[handle] = {'handle': handle, 'kind': kind, 'rows': [], **values}
        return handle

    subject = _subject(parsed, request)
    lines = ['# 受检者', _line(f'性别：{subject["sex"] or "未记录"}', f'年龄：{subject["age"] or "未记录"}')]
    if timing:
        parts = []
        if timing.get('clinicalDate'):
            parts.append(f'临床体检日期 {timing["clinicalDate"]}')
        sample = timing.get('microbiomeSampleDate')
        received = timing.get('microbiomeReceivedDate')
        if sample:
            parts.append(f'菌群采样日期 {sample}')
        elif received:
            parts.append(f'菌群收样日期 {received}（采样日期未注明）')
        days = timing.get('daysBetween')
        label = '两者'
        if days is None:
            days, label = _days_between(timing.get('clinicalDate'), received), '体检日期与菌群收样日期'
        if isinstance(days, int):
            # The reading of the interval is stated here so that it does not vary between runs.
            cycle = (
                '不超过 6 个月，属同一健康周期，两类资料可互相印证'
                if abs(days) <= _SAME_CYCLE_DAYS
                else '超过 6 个月，结论以较新的资料为准'
            )
            parts.append(f'{label}相隔 {abs(days)} 天（{cycle}）')
        if parts:
            lines.append('资料日期：' + '；'.join(parts))

    facts = list(getattr(document, 'facts', ()) or ())
    rows_by_fact = _clinical_rows_by_fact(clinical or {})
    measured, conclusions, derived = [], [], []
    for fact in facts:
        if fact.kind != 'clinical':
            continue
        section = (_SECTION.search(fact.source or '') or [None, ''])[1]
        if fact.fact_id.startswith('derived-fact-'):
            derived.append((fact, section))
        elif _CONCLUSION_SECTION.search(section) or _CONCLUSION_PREFIX.match(fact.name or ''):
            conclusions.append((fact, section))
        else:
            measured.append((fact, section))

    if measured:
        lines += ['', '# 体检与检验记录', _line('编号', '项目', '结果', '单位', '参考范围', '标记', '检查类别', '日期')]
        for fact, section in measured:
            rows = rows_by_fact.get(fact.fact_id, [])
            handle = add(
                'L',
                'clinical',
                factId=fact.fact_id,
                name=_clean(fact.name),
                value=_clean(fact.value),
                unit=_clean(fact.unit),
                reference=_clean(fact.reference_range),
                status=_clean(fact.status),
                date=_clean(fact.date),
                section=_clean(section),
                abnormal=bool(fact.abnormal),
                imaging=bool(fact.imaging),
                target=_target_text(rows),
            )
            entries[handle]['rows'] = rows
            lines.append(
                _line(handle, fact.name, fact.value, fact.unit, fact.reference_range, fact.status, section, fact.date)
            )
    if derived:
        lines += [
            '',
            '# 派生指标（已按公式算好，直接使用，不要另行计算其他综合指数）',
            _line('编号', '指标', '结果', '单位', '分级或参考', '判定', '算式与代入值'),
        ]
        for fact, _ in derived:
            rows = rows_by_fact.get(fact.fact_id, [])
            handle = add(
                'D',
                'derived',
                factId=fact.fact_id,
                name=_clean(fact.name),
                value=_clean(fact.value),
                unit=_clean(fact.unit),
                reference=_clean(fact.reference_range),
                status=_clean(fact.status),
                date=_clean(fact.date),
                section='派生指标',
                abnormal=bool(fact.abnormal),
                imaging=False,
                target=_target_text(rows),
                formula=_clean(fact.source),
            )
            entries[handle]['rows'] = rows
            lines.append(
                _line(handle, fact.name, fact.value, fact.unit, fact.reference_range, fact.status, fact.source)
            )
    targets = [(handle, row['name'], row['target']) for handle, row in entries.items() if row.get('target')]
    if targets:
        lines += ['', '# 已核对出处的理想目标（有这些项目时优先采用）']
        lines += [_line(handle, name, target) for handle, name, target in targets]
    if conclusions:
        lines += [
            '',
            '# 体检结论与检查建议（体检机构给出的文字结论，可能与上面的数值不一致）',
            _line('编号', '结论', '建议或说明'),
        ]
        for fact, _ in conclusions:
            name = _CONCLUSION_PREFIX.sub('', _clean(fact.name))
            handle = add(
                'A',
                'advice',
                factId=fact.fact_id,
                name=name,
                value=_clean(fact.value),
                unit='',
                reference='',
                status='',
                date=_clean(fact.date),
                section='体检结论',
                abnormal=False,
                imaging=False,
                target='',
            )
            lines.append(_line(handle, name, fact.value))

    answers = _questionnaires(request)
    reported = [fact for fact in facts if fact.kind == 'self_reported' and not fact.reported_assertion_kind]
    if answers:
        lines += ['', '# 问卷与自述', _line('编号', '问题', '回答')]
        current = None
        fact_by_question = {}
        for fact in reported:
            fact_by_question.setdefault((_clean(fact.name), _clean(fact.value)), fact)
        for title, question, reply in answers:
            if title != current:
                lines.append(f'## {title}')
                current = title
            fact = fact_by_question.get((question, reply))
            handle = add(
                'Q',
                'reported',
                factId=fact.fact_id if fact is not None else None,
                name=question,
                value=reply,
                unit='',
                reference='',
                status='',
                date='',
                section=title,
                abnormal=False,
                imaging=False,
                target='',
            )
            lines.append(_line(handle, question, reply))
    elif reported:
        lines += ['', '# 问卷与自述', _line('编号', '问题', '回答')]
        for fact in reported:
            handle = add(
                'Q',
                'reported',
                factId=fact.fact_id,
                name=_clean(fact.name),
                value=_clean(fact.value),
                unit='',
                reference='',
                status='',
                date='',
                section='问卷',
                abnormal=False,
                imaging=False,
                target='',
            )
            lines.append(_line(handle, fact.name, fact.value))

    micro_lines = _microbiome_lines(microbiome or {}, add, entries)
    if micro_lines:
        context = (microbiome or {}).get('sampleContext') or {}
        head = '；'.join(
            filter(
                None,
                (
                    f'样本 {_clean(context.get("sampleType"))}' if context.get('sampleType') else '',
                    f'采样日期 {_clean(context.get("sampleDate"))}' if context.get('sampleDate') else '',
                    f'收样日期 {_clean(context.get("receivedDate"))}' if context.get('receivedDate') else '',
                    f'报告日期 {_clean(context.get("reportDate"))}' if context.get('reportDate') else '',
                ),
            )
        )
        lines += [
            '',
            '# 肠道菌群宏基因组检测' + (f'（{head}）' if head else ''),
            '功能评分是基因层面的预测潜能（0–100 分），不是体内实测浓度；丰度是样本内相对丰度（%）。',
            _line('编号', '项目', '结果', '单位', '参考或分档', '标记'),
        ]
        lines += micro_lines
        if disease_models:
            lines += ['## 菌群疾病关联模型（相似度指数，不是患病概率）', _clean(disease_models)]

    if not measured and not derived and not conclusions:
        lines += ['', '# 体检与检验记录', '本次没有提供体检或检验资料。']
    if not answers and not reported:
        lines += ['', '# 问卷与自述', '本次没有提供问卷或本人自述。']
    if not micro_lines:
        lines += ['', '# 肠道菌群宏基因组检测', '本次没有提供肠道菌群检测结果。']

    text = '\n'.join(lines) + '\n'
    return CaseSheet(
        text=text,
        entries=entries,
        subject=subject,
        has_clinical=bool(measured or derived),
        has_questionnaire=bool(answers or reported),
        has_microbiome=bool(micro_lines),
        numbers=frozenset(text_numbers(text)),
    )


def _micro_value(row) -> tuple[str, str]:
    reference = _clean(row.get('referenceRange'))
    if len(reference) > 90:
        reference = reference[:90] + '…'
    mark = _clean(row.get('status')) or {'up': '↑', 'down': '↓'}.get(row.get('direction') or '', '')
    return reference, mark


def _microbiome_lines(microbiome, add, entries) -> list[str]:
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()

    def emit(group, row, section):
        key = (_clean(row.get('name')), _clean(row.get('value')))
        if key in seen or not key[0]:
            return
        seen.add(key)
        reference, mark = _micro_value(row)
        handle = add(
            'M',
            'microbiome',
            factId=row.get('code'),
            name=key[0],
            value=key[1],
            unit=_clean(row.get('unit')),
            reference=reference,
            status=mark,
            date=_clean(row.get('observedAt')),
            section=section,
            group=group.get('code') if group else None,
            abnormal=row.get('level') in {'focus', 'borderline'},
            imaging=False,
            target='',
        )
        entries[handle]['rows'] = [row]
        lines.append(_line(handle, key[0], key[1], row.get('unit'), reference, mark))

    def normal(baseline):
        rows = (baseline or {}).get('rows') or ()
        members = []
        for row in rows[:_NORMAL_MEMBER_LIMIT]:
            name, value = _clean(row.get('name')), _clean(row.get('value'))
            if not name or (name, value) in seen:
                continue
            seen.add((name, value))
            members.append(f'{name} {value}{_clean(row.get("unit"))}')
        count = (baseline or {}).get('count') or len(rows)
        if members:
            more = f'等共 {count} 项' if count and count > len(members) else f'共 {len(members)} 项'
            lines.append('参考范围内（' + more + '）：' + '；'.join(members))
        elif count:
            lines.append(f'参考范围内：共 {count} 项')

    for key, title in (
        ('structureGroups', '宏观指数'),
        ('organismGroups', '菌群结构'),
        ('functionalGroups', '功能潜能'),
    ):
        groups = microbiome.get(key) or ()
        if not groups:
            continue
        lines.append(f'## {title}')
        for group in groups:
            name = _clean(group.get('name'))
            states = '；'.join(
                f'{_clean(state.get("sourceName"))}：{_clean(state.get("status"))}'
                + (f'（{_clean(state.get("details"))}）' if state.get('details') else '')
                for state in group.get('panelStates') or ()
            )
            lines.append(
                f'### {name}'
                + (f'｜面板结论 {states}' if states else '')
                + (f'｜{_clean(group.get("status"))}' if group.get('status') else '')
            )
            for row in group.get('rows') or ():
                emit(group, row, name)
            normal(group.get('normalBaseline'))
    flat = [
        row
        for row in microbiome.get('structure') or ()
        if (_clean(row.get('name')), _clean(row.get('value'))) not in seen
    ]
    if flat:
        lines.append('## 其他宏观指数')
        for row in flat:
            emit(None, row, '宏观指数')
    reported = [
        row
        for row in microbiome.get('_reportedAssessmentRows') or ()
        if (_clean(row.get('name')), _clean(row.get('value'))) not in seen
    ]
    if reported:
        lines.append('## 菌群关联评估（检测报告给出的定性结论）')
        for row in reported:
            emit(None, row, '菌群关联评估')
    return lines
