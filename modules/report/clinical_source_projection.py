"""Private, literal patient projections; immutable source facts remain the audit record."""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Mapping
from decimal import Decimal

from .evaluation_derived_indices import derived_index_context
from .evaluation_derived_indices import is_derived_index

VERSION = 'clinical-patient-source.v1'
PATIENT_ROLES = frozenset(
    {
        'direct_measurement',
        'derived_measurement',
        'exam_finding',
        'physical_finding',
        'reported_behavior',
    }
)
_NUMERIC_LITERAL = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
_SCALAR = re.compile(rf'\s*(?:[<>≤≥]=?\s*)?{_NUMERIC_LITERAL}(?:\s*[-–~至]\s*{_NUMERIC_LITERAL})?\s*')
_NUMBER = re.compile(r'(?<![\d.])(?:[<>≤≥]=?\s*)?[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?![\d.])')
_UNAVAILABLE = frozenset({'', '--', '—', '/', '未检', '未检查', '未检测', '未提供', '无数据'})
_RESULT_HEADING = re.compile(
    r'(?:诊断提示|诊断意见|诊断结论|检查结论|超声提示|病理诊断|结果分析|检测结果和建议|检测结果|结论)\s*[:：]'
)
_INSTRUCTION = re.compile(
    r'^(?:(?:原报告|设备|医师)\s*)?(?:请|建议|应当|应|需要|需|宜|务必|定期|复查|注意|避免|保持)'
    r'|^(?:每\s*(?:\d+|[一二两三四五六七八九十半]+)?\s*(?:个)?(?:天|日|周|月|年)'
    r'|(?:\d+|[一二两三四五六七八九十半]+)\s*(?:个)?(?:天|日|周|月|年)后)\s*(?:复查|随访|复诊|检查)'
)
_GENERAL = re.compile(
    r'如果|假如|(?:^|[，,])(?:若|如有|如出现|一旦)|通常|一般|常见于|多见于|常见|例如|比如|有助于|用于|可用于|'
    r'可导致|会导致|可引起|可能引起|为好|较好|的患者|者应|者需'
)
_REGISTRATION = re.compile(r'登记|身份|基本信息|质控|接收日期|报告日期|体检日期|检查日期|样本类型|姓名|性别|年龄')
_SUMMARY = re.compile(r'主检|总检|总结|汇总|健康教育|解读|原理|意义|建议原文')
_BEHAVIOR = re.compile(r'生活方式|病史记录|睡眠|饮食记录|运动记录|饮酒记录|吸烟记录')
_PERSONAL = re.compile(r'本次|此次|受检者|患者自述|本人|查体(?:见|示)|检查(?:见|发现|示)')
_SECTION_HEADING = re.compile(
    r'(?P<label>检查所见|影像所见|超声所见|检查描述|所见|测量结果|测量数据|'
    r'诊断提示|诊断意见|诊断结论|检查结论|超声提示|病理诊断|结果分析|检测结果和建议|检测结果|结论|'
    r'原报告建议|医嘱|处理意见|随访建议|建议|登记信息|基本信息|基本资料|患者资料|受检者信息|质控信息)\s*[:：]'
)
_ACQUISITION_FIELD = re.compile(
    r'^(?:(?:(?:单页|设备|原报告|报告|检查|体检|采样|接收|审核|送检|申请|病人|患者|受检者|汇总页|汇总|登记)\s*)*'
    r'(?:姓名|性别|年龄|日期|时间|编号|病历号|检查号|样本号|标本号|身高登记|体重登记)|'
    r'(?:设备|送检|原报告)?登记(?:身高|体重)|送检材料|标本类型|样本类型|检测机构|报告医师|审核医师|操作人|审核人|质控)\s*[:：]?'
)
_ACQUISITION_RECORD = re.compile(r'原始.{0,20}(?:通道|波形|电化学|电阻抗|信号|箭头)|登记|核对|校对|审计')


def reported_temporal_context(fact, *, reported_role=None):
    """Keep a questionnaire's event condition separate from its submission time."""
    if _get(fact, 'kind') != 'self_reported':
        return {}
    reported_role = reported_role or {}
    value = str(_get(fact, 'value', default=''))
    wrapped = re.match(r'^\s*问题[：:](.*?)；\s*回答[：:](.*)$', value, re.S)
    question = str(reported_role.get('question') or (wrapped[1] if wrapped else _get(fact, 'name', default='')))
    answer = str(reported_role.get('answer') or (wrapped[2] if wrapped else value))
    facet = str(reported_role.get('extractedFacet') or _get(fact, 'name', default=''))
    role = reported_role.get('role')
    # The actual answer clause, rather than a mixed question's goal wording,
    # determines whether an extracted assertion is a management goal.
    clauses = [part for part in re.split(r'[。；;┋|｜，,]', answer) if facet and facet in part]
    goal = (
        role in {'management_goal', 'reported_goal'}
        or bool(clauses)
        and all(re.search(r'希望|目标|打算|计划|期望|想要', part[: part.find(facet)] + facet) for part in clauses)
    )
    if role != 'reported_symptom' and not re.search(r'不适|症状|困扰', question):
        goal = goal or bool(re.search(r'希望|目标|想改善|最想', question))
    event = re.search(r'(?:这次|此次|当次|上次|那次|当|在|此前)[^?？。；;]{0,60}?(?:期间|时候|当天|当日|时)', question)
    if event is None:
        event = re.search(r'(?:最近|上|前|这|此|当|那)(?:一)?次[^，,?？。；;]{0,60}', question)
    recent = re.search(r'最近(?:\s*\d+\s*(?:天|周|月|年))?|近期|目前|现在|平时|通常', question)
    history = re.search(r'曾经|曾|既往|以前|过去', question)
    if goal:
        kind, scope, current = 'management_goal', next(iter(clauses), ''), False
    elif event:
        kind, scope, current = 'retrospective_event', event.group(), False
    elif history or role in {'reported_history', 'qualified_history_context'}:
        kind, scope, current = 'reported_history', history.group() if history else '', False
    elif recent:
        kind, scope, current = 'recent_state', recent.group(), True
    else:
        kind, scope, current = 'unspecified', '', False
    literal_date = (
        re.search(r'\d{4}(?:[-/]\d{1,2}[-/]\d{1,2}|年\d{1,2}月\d{1,2}日?)', scope)
        if kind == 'retrospective_event'
        else None
    )
    return {
        'kind': kind,
        'scopeQuote': scope,
        'reportedAt': reported_role.get('reportedAt') if 'reportedAt' in reported_role else _get(fact, 'date'),
        'reportedAtSourcePath': reported_role.get('reportedAtSourcePath'),
        **({'reportedAtConflict': True} if reported_role.get('reportedAtConflict') else {}),
        'scopeSourcePath': reported_role.get('sourcePath') or str(_get(fact, 'source', default='')),
        'scopeTarget': 'reported_answer_only',
        'eventDate': literal_date.group() if literal_date else None,
        'eventId': None,
        # A question's recalled collection episode has no source identity
        # link to any dated laboratory result. Even a literal event date
        # alone cannot establish that two records describe the same sample.
        'eventLinkStatus': 'unestablished' if kind == 'retrospective_event' else 'not_applicable',
        'clinicalCooccurrenceConfirmed': False if kind == 'retrospective_event' else None,
        'mayQualifyDatedClinicalResult': False if kind == 'retrospective_event' else None,
        'currentStateConfirmed': current,
        'submissionTimeIsEventTime': False,
    }


def reported_source_time(document, source_path):
    """Read a timestamp only from the exact answer path's actual ancestors."""
    if not isinstance(source_path, str) or not re.match(r'^patientReported(?:\.|\[|$)', source_path):
        return {}
    parts = list(re.finditer(r'(?:^|\.)([A-Za-z_][A-Za-z_0-9]*)|\[(\d+)\]', source_path))
    if ''.join(part.group() for part in parts) != source_path:
        return {}
    value = getattr(document, 'background', {})
    path, ancestry = '', []
    for part in parts:
        if part[1] is not None:
            if not isinstance(value, Mapping) or part[1] not in value:
                return {}
            value = value[part[1]]
            path += ('.' if path else '') + part[1]
        else:
            index = int(part[2])
            if not isinstance(value, list) or index >= len(value):
                return {}
            value = value[index]
            path += '[' + str(index) + ']'
        if isinstance(value, Mapping):
            ancestry.append((path, value))
    for parent_path, parent in reversed(ancestry):
        times = [
            (key, parent[key])
            for key in ('answeredAt', 'submittedAt', 'submissionTime', 'submitTime')
            if isinstance(parent.get(key), str) and parent[key].strip()
        ]
        if times:
            if len({time for _, time in times}) != 1:
                return {'reportedAt': None, 'reportedAtSourcePath': parent_path, 'reportedAtConflict': True}
            key, time = times[0]
            return {'reportedAt': time, 'reportedAtSourcePath': parent_path + '.' + key}
    return {}


def patient_reference_context(fact):
    """Record-level reference authority; never promote it to a whole panel."""
    from .evaluation_builder import _Observation
    from .evaluation_builder import _printed_reference_relation
    from .evaluation_builder import _qualitative_polarity
    from .evaluation_builder import _status_state

    reference = _get(fact, 'reference_range', 'referenceRange')
    supplied = isinstance(reference, str) and reference.strip() not in {'', '-', '—', '/', '--'}
    status = _get(fact, 'status')
    value = _get(fact, 'value', default='')
    source_state = _status_state(status)
    if source_state is None:
        literal_state = _status_state(value)
        if (
            literal_state == 'normal'
            or _qualitative_polarity(value) == 'positive'
            or (
                isinstance(value, str)
                and re.fullmatch(r'(?:轻度)?异常|偏高|偏低|升高|降低|abnormal|high|low|[↑↓]', value.strip(), re.I)
            )
        ):
            # A sentence explaining what a decrease may mean is not a
            # printed personal abnormal status, even if it contains that word.
            source_state = literal_state or 'abnormal'
    relation = (
        _printed_reference_relation(
            _Observation(
                observation_id=str(_get(fact, 'fact_id', 'factId', 'code', default='')),
                code=str(_get(fact, 'code', default='')),
                name=str(_get(fact, 'name', default='')),
                value=value,
                unit=_get(fact, 'unit'),
                reference_range=reference,
                kind='clinical',
                source_type='checkup',
            ),
            allow_exact_match=True,
        )
        if supplied
        else None
    )
    if relation is None and supplied and source_state == 'normal' and _compact(value) == _compact(reference):
        relation = 'within'  # An exact printed qualitative reference, not a guessed numeric cutoff.
    reference_state = {'within': 'normal', 'outside': 'abnormal'}.get(relation)
    conflict = bool(reference_state and source_state and reference_state != source_state)
    assessment = 'abnormal' if 'abnormal' in {reference_state, source_state} else reference_state or source_state
    return {
        'assessmentState': assessment,
        'hasExplicitReferenceRange': supplied,
        'referenceAssessmentState': relation or ('indeterminate' if supplied else 'not_provided'),
        'sourceStatusState': source_state,
        'statusReferenceConflict': conflict,
        'normalityScope': 'individual_observation_only',
    }


def _scalar_measurement(fact):
    value = unicodedata.normalize('NFKC', str(_get(fact, 'value', default=''))).strip()
    if _acquisition_statement(str(_get(fact, 'name', default=''))):
        return False
    unit = unicodedata.normalize('NFKC', str(_get(fact, 'unit', default='') or '')).strip()
    if unit and value.endswith(unit):
        value = value[: -len(unit)].strip()
    if _SCALAR.fullmatch(value):
        return True
    # A paired reading needs its own printed unit. A date or a channel list
    # cannot acquire a unit from another record merely because it has digits.
    return bool(unit and re.fullmatch(rf'{_NUMERIC_LITERAL}\s*/\s*{_NUMERIC_LITERAL}', value))


def _advice_statement(text):
    if _INSTRUCTION.search(text):
        return True
    return bool(
        re.match(r'^(?:如(?:果|有|出现)?|若(?:有|出现)?|一旦|当[^，,]{1,35}时)[^。；;]*[，,]', text)
        and re.search(r'就诊|就医|复查|复诊|随访|检查|治疗|请|建议|应|需', text)
    )


def _acquisition_statement(text):
    text = unicodedata.normalize('NFKC', str(text)).strip().strip('。；;，,')
    if _ACQUISITION_FIELD.match(text):
        return True
    # Standalone demographic values and missing metadata describe the
    # acquisition, including a duplicated value printed by two source cells.
    # They never become a patient finding merely because they follow EXAM.
    return bool(
        re.fullmatch(r'(?:(?:男|女)(?:性)?|\d{1,3}\s*岁)(?:\s+(?:(?:男|女)(?:性)?|\d{1,3}\s*岁))*', text)
        or re.fullmatch(
            r'(?:未见|未列|未提供|未注明|未载明|无)(?:检查|检测|采样|报告|登记)?(?:日期|时间|年龄|性别)(?:信息)?',
            text,
        )
    )


def _unavailable_result_statement(text):
    return bool(
        re.fullmatch(
            r'[^。；;]{1,48}(?:栏(?:位)?(?:均)?(?:为空白|空白)|未填写|未记录|未提供|未测量|未检测)',
            _clean_result(text),
        )
    )


def _general_statement(text, *, result_frame=False):
    if _GENERAL.search(text):
        return True
    # A causal explanation of a class of results is not a personal result.
    # Conversely, "提示" inside an actual examination's result section does
    # not turn a literal, possibly uncertain finding into education.
    consequence = re.search(r'[，,]\s*(?:提示|意味着|反映|代表|说明)', text)
    return bool(
        consequence
        and not result_frame
        and not _PERSONAL.search(text)
        and not _NUMBER.search(text[: consequence.start()])
    )


def _get(value, *keys, default=None):
    for key in keys:
        item = value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)
        if item is not None:
            return item
    return default


def _compact(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', '' if value is None else str(value))).casefold()


def _span(fid, value, start, end, role):
    while start < end and value[start].isspace():
        start += 1
    while end > start and value[end - 1].isspace():
        end -= 1
    return {'recordId': fid, 'start': start, 'end': end, 'quote': value[start:end], 'role': role}


def _sentences(fid, value):
    # An ASCII period between digits belongs to a number, never a sentence boundary.
    for match in re.finditer(r'.+?(?:[。；;\n]+|(?<!\d)\.(?!\d)|$)', value, re.S):
        if match.group().strip():
            yield _span(fid, value, match.start(), match.end(), '')


def valid_source_span(fact, span):
    value = str(_get(fact, 'value', default=''))
    start, end = span.get('start'), span.get('end')
    return (
        type(start) is int
        and type(end) is int
        and 0 <= start < end <= len(value)
        and value[start:end] == span.get('quote')
        and span.get('recordId') == _get(fact, 'fact_id', 'factId', 'recordId')
    )


def _clean_result(text):
    return text.strip().rstrip('。；;，,\n').strip()


def _append_supporting_span(row, span):
    """Keep each literal witness once, and display a covered observation once."""
    spans = row['sourceSpans']
    identity = (span['recordId'], span['start'], span['end'])
    if any((prior['recordId'], prior['start'], prior['end']) == identity for prior in spans):
        return
    spans.append(span)
    row['value'] = _span_display_value(spans)


def _span_display_value(spans):
    """Compose the positional union while leaving every witness immutable."""
    selected = []
    for span in spans:
        matching = [
            item
            for item in selected
            if item['recordId'] == span['recordId'] and item['start'] <= span['end'] and span['start'] <= item['end']
        ]
        if not matching:
            selected.append(dict(span))
            continue
        start = min(span['start'], *(item['start'] for item in matching))
        end = max(span['end'], *(item['end'] for item in matching))
        text = [''] * (end - start)
        for witness in [*matching, span]:
            text[witness['start'] - start : witness['end'] - start] = witness['quote']
        first = min(selected.index(item) for item in matching)
        selected = [item for item in selected if item not in matching]
        selected.insert(first, {**span, 'start': start, 'end': end, 'quote': ''.join(text)})
    return '；'.join(dict.fromkeys(_clean_result(item['quote']) for item in selected if _clean_result(item['quote'])))


def _finding_name(text):
    """Separate a trailing physical size from its literal finding name."""
    size = re.search(
        r'\d+(?:\.\d+)?(?:\s*[×xX*]\s*\d+(?:\.\d+)?){0,2}\s*'
        r'(?:mm|cm|毫米|厘米)(?![A-Za-z])',
        text,
        re.I,
    )
    if size is None or text[size.end() :].strip('。；;. '):
        return text
    head = re.sub(r'(?:长径|短径|直径|大小|约|为|[：:\s])+$', '', text[: size.start()])
    return head if head else text


def _source_nature(span, role):
    if role in {'direct_measurement', 'derived_measurement'}:
        return 'measurement'
    if span.get('sectionRole') == 'source_component':
        return 'measurement_component'
    text = _clean_result(span['quote'])
    clauses = [part for part in re.split(r'[，,；;。]', text) if part.strip()]
    normal_context = r'正常|未见|未发现|未查见|未检出|无殊|阴性|不宽|不大|光整|光滑|通畅|完整|清晰|均匀'
    if clauses and all(
        re.search(normal_context, part) and not re.search(r'欠|不均|不规则|增高|升高|减低|降低|变直|变形', part)
        for part in clauses
    ):
        return 'normal_comparison'
    return 'patient_finding'


def _row(fact, context, span, role, *, name=None):
    fid = _get(fact, 'fact_id', 'factId')
    measured = role in {'direct_measurement', 'derived_measurement'}
    value = _get(fact, 'value') if measured else span['quote']
    at = context.get('examDate') or _get(fact, 'date')
    return {
        'code': f'{fid}:span:{span["start"]}:{span["end"]}',
        'ownRecordId': fid,
        'name': name or _get(fact, 'name'),
        'value': value,
        'unit': _get(fact, 'unit') if measured else None,
        'referenceRange': _get(fact, 'reference_range', 'referenceRange') if measured else None,
        'status': _get(fact, 'status') if measured else None,
        'observedAt': at,
        'sourceReportedAge': context.get('sourceReportedAge'),
        'role': role,
        'sourceNature': _source_nature(span, role),
        'sectionRole': span.get('sectionRole', 'source_observation'),
        'sourceSpans': [{**span, 'role': role}],
        'relatedRecordIds': [],
        'sources': [{'sourceCode': 'A', 'recordId': fid, 'observedAt': at}],
        'evidenceRefs': [],
    }


def _measurement_match(text, fact, candidates):
    """A summary's personal numeric claim needs an identifiable actual measurement."""
    matches = []
    own_context = getattr(fact, '_clinical_source_context', {})
    for candidate in candidates:
        name = str(_get(candidate, 'name', default=''))
        value = str(_get(candidate, 'value', default='')).strip()
        if not name or name not in text or not _scalar_measurement(candidate):
            continue
        unit = _get(candidate, 'unit')
        if unit and _compact(unit) not in _compact(text):
            continue
        context = getattr(candidate, '_clinical_source_context', {})
        origin = own_context.get('sourceSha256') or own_context.get('documentId')
        other_origin = context.get('sourceSha256') or context.get('documentId')
        if origin and other_origin and origin != other_origin:
            continue
        parsed = _NUMBER.fullmatch(value)
        if parsed is None:
            continue
        for printed in _NUMBER.finditer(text):
            if _compact(printed.group()) == _compact(value):
                matches.append(candidate)
                break
            if re.fullmatch(r'[+-]?[\d.]+', printed.group()) and re.fullmatch(r'[+-]?[\d.]+', value):
                if Decimal(printed.group()) == Decimal(value):
                    matches.append(candidate)
                    break
    # Equal values on different dates are not a unique acquisition link.
    identities = {(_get(row, 'name'), _get(row, 'value'), _get(row, 'unit'), _get(row, 'date')) for row in matches}
    return matches if len(identities) == 1 else []


def _record_spans(fact, context):
    """Read the source's sections before deciding which clauses are patient results."""
    fid = _get(fact, 'fact_id', 'factId')
    value = str(_get(fact, 'value', default=''))
    name = str(_get(fact, 'name', default=''))
    record_role = context.get('recordRole')
    raw_acquisition = record_role in {
        'acquisition',
        'acquisition_context',
        'registration',
        'registration_context',
        'raw_device_channels',
        'source_audit',
    } or bool(_ACQUISITION_RECORD.search(name) and not re.search(r'所见|结果|结论|测量', name))
    default = (
        'medical_advice'
        if record_role == 'medical_advice' or '建议原文' in name
        else 'acquisition_context'
        if raw_acquisition
        else 'source_description'
    )
    headings = list(_SECTION_HEADING.finditer(value))
    single_result = not headings and len(list(_sentences(fid, value))) == 1
    regions = [(0, headings[0].start() if headings else len(value), default)]
    for index, heading in enumerate(headings):
        label = heading['label']
        section = (
            'medical_advice'
            if re.search(r'建议|医嘱|处理意见', label) and label != '检测结果和建议'
            else 'acquisition_context'
            if re.search(r'登记|基本信息|基本资料|患者资料|受检者信息|质控', label)
            else 'source_description'
            if re.search(r'所见|描述', label)
            else 'source_conclusion'
        )
        regions.append(
            (heading.end(), headings[index + 1].start() if index + 1 < len(headings) else len(value), section)
        )
    for start, end, section in regions:
        for part in _sentences(fid, value):
            lo, hi = max(start, part['start']), min(end, part['end'])
            if lo >= hi:
                continue
            whole = _span(fid, value, lo, hi, '')
            if section == 'medical_advice' or _advice_statement(_clean_result(whole['quote'])):
                yield {**whole, 'sectionRole': 'medical_advice'}
                continue
            if re.match(r'^(?:如果|假如|若|如有|如出现|一旦)', whole['quote'].strip()):
                yield {**whole, 'sectionRole': 'generic_explanation'}
                continue
            # An explicit instruction after a result is another source role,
            # even when both were printed in the same sentence.
            split = (
                r'[，,]\s*'
                if section == 'source_description'
                and (_get(fact, 'imaging') or _acquisition_statement(_clean_result(part['quote'])))
                else (r'[，,]\s*(?=请|建议|应当|需要|务必)')
            )
            boundaries = [lo, *(lo + match.end() for match in re.finditer(split, value[lo:hi])), hi]
            for left, right in zip(boundaries, boundaries[1:]):
                span = _span(fid, value, left, right, '')
                text = _clean_result(span['quote'])
                if not text:
                    continue
                role = (
                    'medical_advice'
                    if section == 'medical_advice' or _advice_statement(text)
                    else 'registration_context'
                    if _acquisition_statement(text)
                    else 'unavailable_context'
                    if _unavailable_result_statement(text)
                    else 'acquisition_context'
                    if section == 'acquisition_context'
                    else 'generic_explanation'
                    if _general_statement(text, result_frame=True)
                    else section
                )
                component = re.fullmatch(
                    r'([^\d，,。；;:：]{1,32}?)[：:·…\s]*'
                    r'((?:[<>≤≥]=?\s*)?' + _NUMERIC_LITERAL + r')\s*'
                    r'([A-Za-zμµΩ°%/·*^²³()\s]*)',
                    text,
                )
                if role == 'source_description' and component:
                    role = 'source_component'
                    span['componentName'] = component[1].strip('·…:： ')
                    span['componentValue'] = component[2]
                    span['componentUnit'] = component[3].strip() or None
                elif role == 'source_description' and single_result and not re.search(r'[，,]', text):
                    role = 'source_conclusion'
                yield {**span, 'sectionRole': role}


def _result_spans(fact, context):
    for span in _record_spans(fact, context):
        if span['sectionRole'] in {'source_description', 'source_conclusion', 'source_component'}:
            yield {**span, 'role': 'exam_finding'}


def _build_projection(fact, candidates=()):
    if _get(fact, 'kind') != 'clinical':
        return None
    fid = _get(fact, 'fact_id', 'factId')
    value = str(_get(fact, 'value', default=''))
    context = copy.deepcopy(getattr(fact, '_clinical_source_context', {}) or {})
    for key in (
        'sourceSha256',
        'documentId',
        'sourcePage',
        'examDate',
        'reportDate',
        'method',
        'sourceReportedAge',
        'sourceSection',
        'subject',
    ):
        context.setdefault(key, None)
    name = str(_get(fact, 'name', default=''))
    rows, claims, advice, background = [], [], [], []
    behavior = bool(context.get('backgroundKind') or _BEHAVIOR.search(name))
    advice_record = context.get('recordRole') == 'medical_advice' or '建议原文' in name
    scalar = bool(_scalar_measurement(fact) and not behavior and not advice_record)
    qualitative = bool(
        context.get('dataType') == 'INSPECTION'
        and not re.search(r'[。；;\n]', value)
        and (not _NUMBER.search(value) or re.fullmatch(r'\s*[0-4]?[+＋±-]{1,4}\s*', value))
        and value.strip() not in _UNAVAILABLE
        and not behavior
        and not advice_record
        and not _advice_statement(value)
        and not _acquisition_statement(value)
        and not _unavailable_result_statement(value)
        and not _general_statement(value)
    )
    if value.strip() not in _UNAVAILABLE and (scalar or qualitative):
        role = (
            'derived_measurement'
            if is_derived_index(_get(fact, 'source', default=''))
            else 'direct_measurement'
        )
        span = _span(fid, value, 0, len(value), role)
        rows.append(_row(fact, context, span, role))
    else:
        parts = list(_sentences(fid, value))
        explicit_result = bool(_get(fact, 'imaging') or _RESULT_HEADING.search(value))
        explanatory = any(_general_statement(part['quote'], result_frame=explicit_result) for part in parts)
        summary = bool(
            context.get('recordRole') == 'clinical_summary'
            or _SUMMARY.search(name)
            and not _get(fact, 'imaging')
            or explanatory
            and not explicit_result
        )
        actual_exam = bool(_get(fact, 'imaging') or context.get('dataType') == 'EXAM')
        classified = list(_record_spans(fact, context)) if actual_exam and not summary and not behavior else []
        results = [
            span
            for span in classified
            if span['sectionRole'] in {'source_conclusion', 'source_description', 'source_component'}
        ]
        for span in classified:
            if span['sectionRole'] == 'medical_advice':
                advice.append({**span, 'role': 'medical_advice'})
            elif span not in results:
                background.append({**span, 'role': span['sectionRole']})
        for part in parts:
            if classified:
                continue
            text = _clean_result(part['quote'])
            if not text:
                continue
            if advice_record or _advice_statement(text):
                advice.append({**part, 'role': 'medical_advice'})
                continue
            if summary:
                matched = _measurement_match(text, fact, candidates)
                # A named result followed by its number is a personal assertion;
                # a general consequence/comparison of a result is not.
                colon_result = bool(re.match(r'[^:：。；;]+[:：]\s*[<>≤≥+-]?\s*\d', text))
                if matched and colon_result and not _general_statement(text):
                    span = {**part, 'role': 'summary_patient_claim'}
                    row = _row(fact, context, span, 'summary_patient_claim')
                    row['relatedRecordIds'] = list(dict.fromkeys(_get(item, 'fact_id', 'factId') for item in matched))
                    rows.append(row)
                    claims.append(span)
                elif not explanatory and not _general_statement(text) and not _REGISTRATION.search(text):
                    # A pure clinician result list has no explanatory/advice prose.
                    span = {**part, 'role': 'summary_patient_claim'}
                    rows.append(_row(fact, context, span, 'summary_patient_claim'))
                    claims.append(span)
                else:
                    background.append(
                        {**part, 'role': 'generic_explanation' if _general_statement(text) else 'registration_context'}
                    )
                continue
            if behavior and not _general_statement(text) and not _REGISTRATION.search(text):
                rows.append(_row(fact, context, {**part, 'role': 'reported_behavior'}, 'reported_behavior'))
            else:
                background.append(
                    {**part, 'role': 'registration_context' if _REGISTRATION.search(text) else 'unclassified_context'}
                )
        for span in results:
            text = _clean_result(span['quote'])
            label = (
                name
                if re.fullmatch(
                    r'(?:(?:诊断提示|检查结论|结论)\s*[:：]?\s*)?(?:(?:大致|基本|总体)?正常|未见(?:明显)?异常|阴性)',
                    text,
                )
                else _finding_name(text)
            )
            if span.get('sectionRole') == 'source_component':
                label = name + ' · ' + span['componentName']
            elif span.get('sectionRole') == 'source_description':
                label = name + ' · ' + text.split('，', 1)[0]
            role = 'exam_finding' if _get(fact, 'imaging') or _RESULT_HEADING.search(value) else 'physical_finding'
            row = _row(fact, context, span, role, name=label)
            row['value'] = text
            if span.get('sectionRole') == 'source_component':
                row['value'] = span['componentValue']
                row['unit'] = span['componentUnit']
            if role in {'exam_finding', 'physical_finding'} and span.get('sectionRole') == 'source_conclusion':
                from .clinical_report import _source_lesion_measurement_quotes

                for quote in _source_lesion_measurement_quotes(value, label):
                    start = value.find(quote)
                    if start >= 0:
                        end = start + len(quote)
                        for admitted in results:
                            lo, hi = max(start, admitted['start']), min(end, admitted['end'])
                            if lo < hi:
                                _append_supporting_span(row, _span(fid, value, lo, hi, role))
            rows.append(row)
    roles = list(dict.fromkeys(row['role'] for row in rows))
    role = roles[0] if len(roles) == 1 else 'mixed_record' if roles else 'advice_only' if advice else 'context_only'
    projection = {
        'version': VERSION,
        'recordId': fid,
        'role': role,
        'patientValue': (
            _span_display_value([span for row in rows for span in row['sourceSpans']])
            if rows and all(row['role'] in {'exam_finding', 'physical_finding'} for row in rows)
            else '；'.join(dict.fromkeys(str(row['value']) for row in rows))
        )
        or None,
        'patientRows': rows,
        'claimSpans': claims,
        'adviceSpans': advice,
        'contextSpans': background,
        'sourceContext': context,
        'sourceSemantics': {
            'kind': 'clinical_patient_projection',
            'recordIds': [fid],
            'role': role,
            'reportedObject': name,
            'statusAuthority': (
                'source_summary_claim'
                if role == 'summary_patient_claim'
                else 'calculated_value_only'
                if role == 'derived_measurement'
                else 'source_printed_status'
                if _get(fact, 'status') is not None
                else 'printed_reference_only'
                if _get(fact, 'reference_range', 'referenceRange')
                else 'source_result_text'
            ),
            'referenceText': _get(fact, 'reference_range', 'referenceRange'),
            'referenceAssessment': patient_reference_context(fact),
            'sourceContext': copy.deepcopy(context),
        },
    }
    if derivation := derived_index_context(fact):
        projection['sourceSemantics']['derivedIndex'] = derivation
        if derivation['valueKind'] == 'measured_classification':
            projection['sourceSemantics']['statusAuthority'] = 'measured_value_classification'
    return projection


def ensure_patient_projection_index(document):
    """Compute once per immutable fact inventory; do not mutate its public fields."""
    facts = list(getattr(document, 'facts', ()))
    signature = tuple((fact.fact_id, fact.value, fact.date, fact.status, fact.reference_range) for fact in facts)
    if getattr(document, '_clinical_patient_projection_signature', None) == signature:
        return document._clinical_patient_projections
    scalar = [fact for fact in facts if fact.kind == 'clinical' and _scalar_measurement(fact)]
    index = {}
    for fact in facts:
        if projection := _build_projection(fact, scalar):
            index[fact.fact_id] = projection
            fact._clinical_patient_projection = projection
    document._clinical_patient_projections = index
    document._clinical_patient_projection_signature = signature
    return index


def get_patient_projection(fact, *, projection_index=None):
    if _get(fact, 'kind') != 'clinical':
        return None
    fid = _get(fact, 'fact_id', 'factId', 'recordId')
    if projection_index is not None and fid in projection_index:
        return copy.deepcopy(projection_index[fid])
    cached = getattr(fact, '_clinical_patient_projection', None)
    return copy.deepcopy(cached) if cached is not None else _build_projection(fact)


def _claim_measurement_acquisition_matches(claim_projection, measured_projection):
    """Verify an existing link without substituting a report date for an exam."""
    claim = claim_projection.get('sourceContext') or {}
    measured = measured_projection.get('sourceContext') or {}
    shared_origin = False
    for key in ('sourceSha256', 'documentId'):
        left, right = claim.get(key), measured.get(key)
        if left and right:
            if left != right:
                return False
            shared_origin = True
    if not shared_origin:
        return False
    dates = []
    for context in (claim, measured):
        value = context.get('examDate')
        match = re.fullmatch(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](.+))?', str(value or '').strip())
        if match is None:
            return False
        dates.append((tuple(int(match[index]) for index in (1, 2, 3)), match[4]))
    if dates[0][0] != dates[1][0] or dates[0][1] and dates[1][1] and dates[0][1] != dates[1][1]:
        return False
    for key in ('method', 'specimen', 'sourceReportedAge'):
        left, right = claim.get(key), measured.get(key)
        if left is not None and right is not None and _compact(left) != _compact(right):
            return False
    left_subject, right_subject = claim.get('subject'), measured.get('subject')
    if isinstance(left_subject, Mapping) and isinstance(right_subject, Mapping):
        for key in left_subject.keys() & right_subject.keys():
            left, right = left_subject[key], right_subject[key]
            if left not in (None, '') and right not in (None, '') and _compact(left) != _compact(right):
                return False
    return True


def clinical_claim_context_dependencies(record_ids, *, projection_index, facts):
    """Return only direct, already linked claim/measurement context dependencies.

    A selected measurement may need its clinician's conflicting source claim,
    and a selected claim may need the actual measurement. This does not select
    additional patient results for a reader table or admit any raw summary text.
    """
    selected = set(record_ids)
    dependencies = []

    def owned(fact, row):
        return bool(
            row.get('ownRecordId') == _get(fact, 'fact_id', 'factId', 'recordId')
            and row.get('sourceSpans')
            and all(valid_source_span(fact, span) for span in row['sourceSpans'])
        )

    for fid, projection in projection_index.items():
        fact = facts.get(fid)
        if fact is None or _get(fact, 'kind') != 'clinical':
            continue
        for claim in projection.get('patientRows', ()):
            if claim.get('role') != 'summary_patient_claim' or not owned(fact, claim):
                continue
            for measured_id in claim.get('relatedRecordIds', ()):
                if fid not in selected and measured_id not in selected:
                    continue
                measured_fact = facts.get(measured_id)
                measured_projection = projection_index.get(measured_id, {})
                if measured_fact is None or not _claim_measurement_acquisition_matches(projection, measured_projection):
                    continue
                if not any(
                    row.get('role') == 'direct_measurement' and owned(measured_fact, row)
                    for row in measured_projection.get('patientRows', ())
                ):
                    continue
                for dependency in (fid, measured_id):
                    if dependency not in selected and dependency not in dependencies:
                        dependencies.append(dependency)
    return dependencies


def patient_projection_context(projection):
    """Small model-facing view; the complete projection remains private audit data."""

    def present(value):
        if isinstance(value, dict):
            result = {key: item for key, child in value.items() if (item := present(child)) is not None}
            return result or None
        if isinstance(value, list):
            result = [item for child in value if (item := present(child)) is not None]
            return result or None
        return None if value is None or value == '' else value

    if projection is None:
        return {}
    result = {
        'role': projection['role'],
        'sourceContext': present(projection['sourceContext']),
        'sourceSemantics': {
            key: projection['sourceSemantics'][key]
            for key in ('kind', 'statusAuthority', 'role', 'referenceAssessment')
            if key in projection['sourceSemantics']
        },
    }
    if projection['adviceSpans']:
        result['adviceSpans'] = [
            {key: span[key] for key in ('recordId', 'start', 'end', 'quote')} for span in projection['adviceSpans']
        ]
    rows = projection['patientRows']
    if not (len(rows) == 1 and rows[0]['role'] in {'direct_measurement', 'derived_measurement'}):
        result['patientRows'] = [
            present(
                {
                    key: row[key]
                    for key in (
                        'name',
                        'value',
                        'unit',
                        'status',
                        'observedAt',
                        'role',
                        'sourceNature',
                        'sectionRole',
                        'sourceSpans',
                        'relatedRecordIds',
                    )
                }
            )
            for row in rows
        ]
    return {key: value for key, value in result.items() if value not in ({}, [], None)}


def admitted_source_spans(fact, *, projection_index=None, include_claims=True):
    projection = get_patient_projection(fact, projection_index=projection_index)
    if projection is None:
        return []
    return [
        copy.deepcopy(span)
        for row in projection['patientRows']
        if row['role'] in PATIENT_ROLES or include_claims and row['role'] == 'summary_patient_claim'
        for span in row['sourceSpans']
        if valid_source_span(fact, span)
    ]


def get_patient_rows(fact, *, projection_index=None, topic=None, source_quotes=(), include_claims=False):
    projection = get_patient_projection(fact, projection_index=projection_index)
    if projection is None:
        return []
    rows = [
        row
        for row in projection['patientRows']
        if row['role'] in PATIENT_ROLES or include_claims and row['role'] == 'summary_patient_claim'
    ]
    if topic:
        topic_key = _compact(topic)
        specific = [row for row in rows if topic_key in _compact(row['name']) or topic_key in _compact(row['value'])]
        rows = specific
    if source_quotes:
        rows = [
            row
            for row in rows
            if any(
                _compact(span['quote']) in _compact(quote) or _compact(quote) in _compact(span['quote'])
                for span in row['sourceSpans']
                for quote in source_quotes
                if quote
            )
        ]
    return copy.deepcopy(rows)
