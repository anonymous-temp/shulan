"""Project verified report findings and build report-specific Record question text."""

from utils.report_flags import normalize_report_flag
from utils.report_flags import report_flag_is_abnormal

BACKGROUND_INTERVIEW = (
    {'question': '问卷选项之外，您还有哪些身体不适或想改善的健康问题？', 'reason': '补充本次健康管理目标'},
    {'question': '除了已经选出的疾病，医生还确诊过其他疾病吗？', 'reason': '补充个人既往病史'},
    {'question': '您提到的家族病史分别涉及哪位血亲？还有没有其他家族病史？', 'reason': '确认具体亲属和疾病'},
)
RISK_PRIORITIES = {'priority': '优先跟进', 'routine': '常规跟进', 'uncertain': '待进一步确认'}
FREE_TEXT_REQUESTS = ('请填写', '请描述', '请写出', '请补充', '请上传', '请提供', '请发送')


def short_text(value, limit=160):
    return value.strip()[:limit] if isinstance(value, str) else ''


def report_evidence(reports):
    evidence = []
    for position, report in enumerate(reports, 1):
        index = report.get('_preview_report_number', position)
        findings = []
        seen = set()
        for section in (report, *(report.get('sections') or [])):
            report_date = short_text(section.get('reportTime') or report.get('reportTime'), 35)
            for row in section.get('observations') or []:
                date = short_text(row.get('observedAt'), 35) or report_date
                name = short_text(row.get('itemName'), 80)
                value = row.get('resultValue')
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    value = str(value)
                value = short_text(value)
                unit = short_text(row.get('unit'), 32)
                flag = row.get('flag')
                key = (name, value, unit, date, short_text(flag, 30))
                if not name or not value or key in seen:
                    continue
                seen.add(key)
                abnormal = report_flag_is_abnormal(flag)
                finding = dict(name=name, value=value, unit=unit, date=date, abnormal=abnormal)
                direction = normalize_report_flag(flag)
                if direction or abnormal:
                    finding['flag'] = direction or short_text(flag, 30)
                reference = short_text(row.get('referenceRange'), 80)
                if reference:
                    finding['reference'] = reference
                findings.append(finding)
            expansion = section.get('expansion')
            if isinstance(expansion, dict):
                conclusion = short_text(expansion.get('diagnosis') or expansion.get('findings'))
                name = short_text(section.get('title'), 80) or '影像检查'
                key = (name, conclusion, '', report_date, '')
                if conclusion and key not in seen:
                    seen.add(key)
                    findings.append(dict(name=name, value=conclusion, unit='', date=report_date, abnormal=False))
        findings.sort(key=lambda finding: not finding['abnormal'])
        for sequence, finding in enumerate(findings[:16], 1):
            evidence.append(dict(finding, id=f'r{index}_{sequence}', report=index))
    return evidence


def _rows(plan, key):
    value = plan.get(key)
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _sources(row, by_id):
    ids = row.get('evidenceIds')
    if not isinstance(ids, list) or not ids:
        return []
    if any(not isinstance(key, str) or key not in by_id for key in ids):
        return []
    return [by_id[key] for key in dict.fromkeys(ids)][:3]


def _finding_value(item):
    return ' '.join(part for part in (item['name'], item['value'], item['unit']) if part)


def report_question_copy(evidence, plan):
    """Turn evidence-linked output into customer choices and manager follow-up materials."""
    by_id = {item['id']: item for item in evidence}
    tests = {}
    questions = {}
    interview = {}
    risks = {}
    bridges = {}
    for row in _rows(plan, 'risks'):
        assessment = short_text(row.get('assessment'), 220)
        priority = row.get('priority')
        sources = _sources(row, by_id)
        if assessment and isinstance(priority, str) and priority in RISK_PRIORITIES and sources:
            risk = {
                'priority': RISK_PRIORITIES[priority],
                'assessment': assessment,
                'evidence': list(dict.fromkeys(_finding_value(item) for item in sources)),
            }
            risks.setdefault(assessment, (priority, risk))
    for row in _rows(plan, 'tests'):
        name = short_text(row.get('name'), 60)
        reason = short_text(row.get('reason'), 100)
        if name and reason and _sources(row, by_id):
            tests.setdefault(name, reason.rstrip('。'))
    for row in _rows(plan, 'questions'):
        title = short_text(row.get('title'), 120)
        description = short_text(row.get('description'))
        names = list(dict.fromkeys(item['name'] for item in _sources(row, by_id)))
        if title and names:
            focus = short_text(row.get('focus'), 80) or f'{"、".join(names)}的检查背景和后续情况'
            raw_choices = row.get('choices')
            choices = (
                list(dict.fromkeys(short_text(choice, 80) for choice in raw_choices))
                if isinstance(raw_choices, list)
                else []
            )
            needs_text = any(phrase in title for phrase in FREE_TEXT_REQUESTS)
            if 2 <= len(choices) <= 7 and all(choices) and 'none' not in choices and not needs_text:
                bridge = f'报告中涉及{"、".join(names)}。'
                choice = {
                    'description': bridge + description,
                    'focus': focus,
                    'choices': choices,
                    'multiple': row.get('multiple') is True,
                }
                questions.setdefault(title, choice)
            else:
                interview.setdefault(title, focus)
    for row in _rows(plan, 'interview'):
        question = short_text(row.get('question'), 160)
        reason = short_text(row.get('reason'), 100)
        if question and reason and _sources(row, by_id):
            interview.setdefault(question, reason)
    for row in _rows(plan, 'bridges'):
        key = row.get('name')
        text = short_text(row.get('description'))
        if isinstance(key, str) and text and _sources(row, by_id):
            bridges.setdefault(key, text)

    abnormal = list(dict.fromkeys(item['name'] for item in evidence if item['abnormal']))[:3]
    if not questions:
        for name in abnormal:
            questions[f'这次体检之后，您复查过{name}吗？'] = {
                'description': f'报告将{name}标记为异常。复查结果可以帮助了解它之后的变化。',
                'focus': f'{name}是否已复查',
                'choices': ['复查过', '还没有复查', '不清楚'],
                'multiple': False,
            }
            interview.setdefault(f'如果您复查过{name}，是哪一天做的？方便发一下结果吗？', '核对最近一次复查结果')
        if not questions and evidence:
            name = evidence[0]['name']
            questions[f'您当时为什么做{name}？'] = {
                'description': f'报告记录了{name}的检查结果。',
                'focus': f'做{name}的原因',
                'choices': ['例行体检', '身体不适', '按医生建议复查', '其他原因', '不清楚'],
                'multiple': False,
            }
            interview.setdefault(f'如果做{name}时有不适，主要是什么感觉，从什么时候开始的？', '了解检查与症状的关系')
    if not tests and abnormal:
        tests.update({f'复查{name}': '对比这次报告中的结果，看看是否还有变化' for name in abnormal})

    report_lines = []
    for index in dict.fromkeys(item['report'] for item in evidence):
        entries = [item for item in evidence if item['report'] == index]
        results = []
        for item in entries[:3]:
            suffix = f'，报告标记{item["flag"]}' if item.get('abnormal') and item.get('flag') else ''
            date = f'（{item["date"]}）' if item['date'] else ''
            value = ' '.join(part for part in (item['value'], item['unit']) if part)
            results.append(f'{item["name"]}{date} {value}{suffix}')
        report_lines.append(f'第{index}份报告记录了' + '、'.join(results))

    followups = list(questions.items())[:5]
    focuses = list(dict.fromkeys([*(item['focus'] for _, item in followups), *interview.values()]))[:3]
    summary = '；'.join(report_lines) or '没有提取到可用的具体检查结果'
    order = {key: index for index, key in enumerate(RISK_PRIORITIES)}
    assessments = [risk for _, risk in sorted(risks.values(), key=lambda entry: order[entry[0]])][:3]
    risk_note = (
        '；'.join(f'{risk["priority"]}：{risk["assessment"].rstrip("。")}' for risk in assessments) + '。'
        if assessments
        else '目前还不能给出可靠的风险评判，需先核实检查结果及相关背景。'
    )
    test_note = (
        '可与医生讨论' + '、'.join(list(tests)[:4]) + '。'
        if tests
        else '目前先核对检查背景和已有结果，再判断是否需要补查。'
    )
    info_note = '；'.join(focuses) or '您的主要不适、既往情况和已经做过的检查'
    opening = f'报告总结：{summary}。\n简要风险评判：{risk_note}\n补充检测：{test_note}\n接下来想了解：{info_note}。'
    message = ''
    if tests:
        items = '\n'.join(f'{i}. {name}：{reason}。' for i, (name, reason) in enumerate(list(tests.items())[:4], 1))
        message = (
            '您好，我看了您发来的体检报告。为了更好地了解这次检查的情况，'
            '建议您就诊时带上报告，和医生确认是否需要做以下检查：\n\n'
            f'{items}\n\n如果您近期已经做过，可以把最近一次结果发给我。我会结合这次体检结果继续跟进。'
        )
    guidance = {
        'riskAssessment': assessments,
        'riskNote': risk_note if evidence and not assessments else '',
        'clientCheckupMessage': message,
        'managerInterview': [
            *({'question': question, 'reason': reason} for question, reason in list(interview.items())[:8]),
            *BACKGROUND_INTERVIEW,
        ],
    }
    return opening, followups, bridges, guidance
