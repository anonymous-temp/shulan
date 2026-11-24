"""Choose comparable current clinical results for evaluate.

Original source rows remain in the internal selection audit. The same selected
snapshot feeds assessment, prose and charts after explicit subject conflicts
and older comparable reports have been removed from the current snapshot.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from datetime import timezone

from modules.report.schemas import SubjectInfo


def subject_mismatch(expected: SubjectInfo, actual: SubjectInfo) -> bool:
    """Exclude affirmative name/sex conflicts, never historical age changes."""
    left, right = _key(expected.name), _key(actual.name)
    if left and right and left != right:
        return True
    left, right = normalized_subject_sex(expected.sex), normalized_subject_sex(actual.sex)
    return left is not None and right is not None and left != right


def normalized_subject_sex(value: str | None) -> str | None:
    return {'男': '男', 'male': '男', 'm': '男', '女': '女', 'female': '女', 'f': '女'}.get(_key(value))


def _row_subject(row):
    extra = row.model_extra or {}
    personal = extra.get('subject') or extra.get('patient') or {}
    if not isinstance(personal, dict):
        personal = {}
    return SubjectInfo(
        name=_first(extra, 'patientName', 'subjectName', 'patient_name', 'subject_name') or personal.get('name'),
        sex=_first(extra, 'patientSex', 'subjectSex', 'patient_sex', 'subject_sex')
        or _first(personal, 'sex', 'gender'),
    )


def _key(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


def _time(value):
    text = str(value or '').strip()
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        # Normalize only the date prefix. A global dot replacement corrupts
        # valid ISO fractional seconds such as 08:00:00.500+08:00.
        match = re.fullmatch(r'(\d{4})[年/.-](\d{1,2})[月/.-](\d{1,2})日?(.*)', text)
        if not match:
            return None
        year, month, day, rest = match.groups()
        try:
            parsed = datetime.fromisoformat(
                f'{int(year):04}-{int(month):02}-{int(day):02}{rest}'.replace('Z', '+00:00')
            )
        except ValueError:
            return None
    try:
        return parsed.astimezone(timezone.utc) if parsed.utcoffset() is not None else parsed
    except (ValueError, OverflowError):
        return None


def _date(row):
    # Current evaluate results follow the report publication time. Examination
    # time is the fallback only when no usable report time was supplied.
    return _time(row.report_time) or _time(row.check_time)


def _first(extra, *keys):
    return next((extra[key] for key in keys if extra.get(key) is not None and str(extra[key]).strip()), None)


def _specimen(value):
    """Unify explicit specimen labels, without inventing a missing specimen."""
    text = _key(value)
    if not text:
        return ''
    aliases = {
        '尿': {'尿', '尿液', '尿标本', '尿液标本', '尿常规', '尿液常规', '尿液分析', '尿沉渣', '尿检', 'urine'},
        '粪便': {'粪', '粪便', '大便', '便常规', '大便常规', '粪便常规', '粪便标本', 'stool', 'faeces', 'feces'},
        '血清': {'血清', '血清标本', 'serum'},
        '血浆': {'血浆', '血浆标本', 'plasma'},
        '全血': {'全血', '全血标本', '血常规', 'wholeblood'},
    }
    return next((name for name, names in aliases.items() if text in names), text)


def _section_specimen(value):
    text = str(value or '')
    # A department such as 泌尿外科 is not an explicit urine specimen. Do not
    # collapse a timed collection into an untimed specimen either.
    if re.search(r'24\s*(?:小时|h).*尿', text, re.I):
        return text
    match = re.search(
        r'(?<![\w\u4e00-\u9fff])(尿(?:常规|液(?:常规|分析)?|沉渣|检)|粪便(?:常规)?|大便(?:常规)?|便常规|血常规|血清|血浆|全血)(?:检查|检测|项目|结果)?(?![\w\u4e00-\u9fff])',
        text,
    )
    return match.group(1) if match else None


_URINE_ITEM = re.compile(
    r'^尿(?:液(?:分析)?|常规|沉渣|检|红细胞|白细胞|上皮细胞|隐血|潜血|蛋白|微量白蛋白|'
    r'葡萄糖|糖|酮体|亚硝酸盐|胆原|胆红素|比重|酸碱度|结晶|管型|透明度|颜色|其它|其他|肌酐)'
    r'|^(?:urine|urinary|urinalysis)\b',
    re.I,
)


def urine_item_label(name: str | None) -> bool:
    """Identify explicitly named urine tests without relying on units or adjacent tests."""
    return _URINE_ITEM.search(unicodedata.normalize('NFKC', name or '').strip()) is not None


def _unit(row, extra):
    explicit = _key(_first(extra, 'unit', 'testResultUnit', 'test_result_unit', 'resultUnit', 'result_unit'))
    # Local clinical PDF rows can embed the unit in the value instead of an
    # extra unit field. The number is preserved; only its printed unit is used
    # to decide comparability. A semiquantitative result such as 3+ has no unit.
    match = re.fullmatch(
        r'\s*[<>≤≥=]?\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*(.*?)\s*', str(row.test_result_value or '')
    )
    suffix = _key(match.group(1)) if match else ''
    embedded = suffix if re.search(r'[^\d\s+\-.]', suffix) else ''
    # Conflicting explicit/embedded units are kept in their own group rather
    # than silently choosing one of the source representations.
    return tuple(dict.fromkeys(unit for unit in (explicit, embedded) if unit))


def _comparison_key(row):
    extra = row.model_extra or {}
    # Do not conflate urine and stool occult-blood tests, incompatible units,
    # different methods or separately named anatomical imaging examinations.
    name = _key(row.item_name)
    aliases = {'红细胞计数': '红细胞总数', '隐血试验': '隐血', '隐血检查': '隐血'}
    name = aliases.get(name, name)
    specimen = _first(
        extra,
        'specimen',
        'specimenType',
        'specimen_type',
        'sampleType',
        'sample_type',
        'sampleName',
        'sample_name',
        '标本',
        '标本类型',
    )
    section = _first(extra, 'sourceSection', 'source_section', 'checkItem', 'check_item', 'panelName', 'panel_name')
    # Only specimen-identifying sections belong in the comparison identity;
    # filename, page and generic hospital department do not create new tests.
    if not specimen:
        specimen = _section_specimen(section)
    if not specimen and row.data_type == 'INSPECTION' and urine_item_label(row.item_name):
        specimen = '尿'
    when = _date(row)
    # With no timezone on one measurement, its absolute ordering against an
    # aware timestamp cannot be established. Preserve both instead of assuming
    # a timezone from the machine running the report.
    clock = when.tzinfo is not None if when is not None else None
    return (
        row.data_type,
        name,
        _unit(row, extra),
        _specimen(specimen),
        _key(_first(extra, 'method', 'testMethod', 'test_method')),
        clock,
    )


def select_clinical_snapshot(request, parsed, *, classifier):
    """Return a deduplicated evaluate request and matching parsed supplements.

    Clinical source values are selected by comparable item and report
    time. Distinct same-time or undated results remain separate evidence;
    page order never decides which of two conflicting results is true.
    Only explicit identity conflicts are excluded; age differences and absent
    names do not establish that a historical record belongs to someone else.
    """
    groups = {}
    comparable = []
    untouched = []
    audit = []
    expected = SubjectInfo(name=request.name or parsed.subject.name, sex=request.sex or parsed.subject.sex)
    # At this stage supplements have not yet been appended to request rows.
    for origin, rows in (
        ('request.hospitalData', request.hospital_data),
        ('parsed.supplementaryHospitalData', parsed.supplementary_hospital_data),
    ):
        for index, original in enumerate(rows):
            row = original.model_copy(deep=True)
            row._evaluation_record_id = f'{origin}[{index}]'
            if subject_mismatch(expected, _row_subject(row)):
                audit.append({'recordId': row._evaluation_record_id, 'reason': 'different_subject'})
                continue
            if classifier(row) != 'clinical':
                untouched.append(row)
                continue
            comparable.append(row)
    # An absent specimen is not established by a different report's label,
    # even if only one explicit specimen type occurs in this request. Keep
    # unknown and explicit specimens independent; otherwise an unrelated
    # later stool result can erase an earlier unlabelled positive screen.
    for row in comparable:
        key = _comparison_key(row)
        groups.setdefault(key, []).append(row)
    selected = []
    history = []
    for rows in groups.values():
        dates = [value for row in rows if (value := _date(row)) is not None]
        latest = max(dates) if dates else None
        seen = {}
        for row in rows:
            when = _date(row)
            if latest is not None and when is not None and when < latest:
                history.append(row)
                audit.append(
                    {
                        'recordId': row._evaluation_record_id,
                        'reason': 'older_comparable_result',
                        'selectedReportTime': latest.isoformat(),
                    }
                )
                continue
            signature = (
                str(row.test_result_value),
                row.exam_result,
                row.reference_range,
                str(row.result_status),
                row.result_status_name,
                when,
            )
            if signature in seen:
                seen[signature]._evaluation_duplicate_record_ids.append(row._evaluation_record_id)
                audit.append(
                    {
                        'recordId': row._evaluation_record_id,
                        'reason': 'same_result_duplicate',
                        'selectedRecordId': seen[signature]._evaluation_record_id,
                    }
                )
                continue
            seen[signature] = row
            selected.append(row)
    combined = [*selected, *untouched]
    supplements = [row for row in combined if row._evaluation_record_id.startswith('parsed.')]
    # The pipeline's normal append step subsequently creates the exact common
    # snapshot for all consumers, retaining original input paths privately.
    chosen = request.model_copy(
        update={'hospital_data': [row for row in combined if row._evaluation_record_id.startswith('request.')]}
    )
    parsed.supplementary_hospital_data = supplements
    parsed._evaluation_source_audit.extend(audit)
    parsed._evaluation_clinical_history.extend(history)
    return chosen
