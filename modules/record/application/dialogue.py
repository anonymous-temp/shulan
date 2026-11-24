"""Expression-only attention references grounded in original patient sources.

This projection never selects an intake question or produces patient facts.
Published question metadata is the sole source of rotation counts.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

from modules.record.application.public_text import strip_internal_notices
from modules.record.application.validation import RecordValidationError

CONTEXT_KEY = '对话衔接参考'
MAX_RECENT_TURNS = 8
MAX_REFERENCES = 60
MAX_QUOTE_CHARS = 600
MAX_LABEL_CHARS = 200
_GOAL_DIMENSIONS = frozenset(('chief_complaint', 'goal'))
_GOAL_LABELS = frozenset(('主要困扰', '希望了解的问题'))
_EMPTY_GOAL_STATES = frozenset(('没有', '不清楚', '暂无改善需求', '没有改善需求', '没有具体目标', '暂无具体目标'))
_USER_SOURCES = ('本次回答', '历史回答')
_UNAVAILABLE = frozenset(('unavailable', 'failed', 'pending', 'processing'))


def _fragments(value):
    if isinstance(value, str):
        if value.strip():
            yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _fragments(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _fragments(item)


def _normalized(value: str) -> str:
    # Preserve decimal points, signs, units and negation. Only normalize spacing
    # and equivalent Chinese/ASCII delimiters, never 6.8 into 68.
    return re.sub(r'\s+', '', value).translate(str.maketrans('：，；', ':,;')).casefold()


def _contains(quote: str, fragments: list[str]) -> bool:
    text = _normalized(quote)
    if not text:
        return False
    prefix = r'(?<![\d.])' if text[0].isdigit() else ''
    suffix = r'(?![\d.])' if text[-1].isdigit() else ''
    pattern = re.compile(prefix + re.escape(text) + suffix)
    return any(pattern.search(_normalized(fragment)) for fragment in fragments)


def _source_passage(quote: str, fragments: list[str]) -> str:
    """Retain the original sentence's negation/conditions around a short quote."""
    for raw in fragments:
        if not _contains(quote, [raw]):
            continue
        for sentence in re.split(r'(?<=[。！？\n])', raw):
            if len(sentence) <= MAX_QUOTE_CHARS and _contains(quote, [sentence]):
                return sentence.strip()
        if len(raw) <= MAX_QUOTE_CHARS:
            return raw.strip()
    return ''


def _sources(patient: dict) -> dict[str, list[str]]:
    return {
        '本次回答': list(_fragments(patient.get('本次回答'))),
        '历史回答': [
            text
            for item in patient.get('历史问答') or []
            if isinstance(item, dict)
            for text in _fragments(item.get('用户回答'))
        ],
        '本次报告': list(_fragments(patient.get('本次报告'))),
    }


def _reference(kind: str, text: str, quote: str, *, path: str = '') -> str:
    # A user answer moving from current to history must not change its identity.
    # Persisted complaint/fact/report identities survive later evidence updates.
    identity = [kind, path] if path else [kind, _normalized(text), _normalized(quote)]
    payload = json.dumps(identity, ensure_ascii=False)
    return 'dialogue_' + hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]


def _goal_values(item: dict, text, evidence):
    """Known coverage can describe an absent goal; only reported goals are usable.

    The aggregated intake stores meaning on observations, not on its parent.
    In mixed statements, retain the positive observation without promoting the
    absent/unknown part. Unspecified but present discomfort remains a valid broad
    attention point; specificity alone does not decide presence.
    """
    if item.get('presence') not in (None, 'present'):
        return
    if item.get('status', item.get('状态')) not in (None, 'known', '已了解'):
        return
    observations = item.get('observations')
    if isinstance(observations, list) and observations:
        for observation in observations:
            if not isinstance(observation, dict) or observation.get('presence') != 'present':
                continue
            yield observation.get('description'), observation.get('evidence'), _goal_observation_identity(observation)
        return
    suffix = _goal_observation_identity(item) if item.get('presence') == 'present' else ''
    yield text, evidence, suffix


def _goal_observation_identity(observation: dict) -> str:
    """Keep separate goals in one aggregate slot stable across quote/order edits."""
    entities = sorted(
        _normalized(entity)
        for entity in observation.get('entities') or []
        if isinstance(entity, str) and entity.strip()
    )
    identity = entities or [_normalized(str(observation.get('description') or observation.get('summary') or ''))]
    return json.dumps(identity, ensure_ascii=False)


def _goal_references(patient: dict, intake: dict, sources: dict):
    preferred = intake.get('preferredId')
    complaints = intake.get('complaints') or []
    complaint_ids = {item.get('id') for item in complaints if isinstance(item, dict) and item.get('id')}
    for item in complaints:
        if not isinstance(item, dict):
            continue
        for text, evidence, _ in _goal_values(item, item.get('text'), item.get('evidence')):
            yield from _grounded_goal(
                text,
                evidence,
                sources,
                preferred=item.get('id') == preferred,
                identity=f'complaint:{item["id"]}' if item.get('id') else '',
            )
    for key, item in (intake.get('facts') or {}).items():
        if not isinstance(item, dict) or item.get('dimension') not in _GOAL_DIMENSIONS:
            continue
        if item.get('status') not in (None, 'known'):
            continue
        # A goal/chief-complaint fact attached to an existing complaint describes
        # the same attention target; do not offer it again as a rotation escape.
        if complaint_ids.intersection(item.get('complaintIds') or []):
            continue
        for text, evidence, suffix in _goal_values(item, item.get('summary'), item.get('evidence')):
            yield from _grounded_goal(
                text,
                evidence,
                sources,
                preferred=bool(preferred and preferred in (item.get('complaintIds') or [])),
                identity=f'fact:{key}:{suffix}' if suffix else f'fact:{key}',
            )
    # Compatibility for expression projections built without the raw intake.
    if not intake:
        for item in patient.get('已识别的困扰') or []:
            if not isinstance(item, dict):
                continue
            text = item.get('困扰')
            if not isinstance(text, str):
                continue
            evidence = [{'source': source, 'quote': text} for source in _USER_SOURCES]
            yield from _grounded_goal(
                text,
                evidence,
                sources,
                preferred=bool(item.get('用户优先关注')),
                identity=f'complaint:{item["编号"]}' if item.get('编号') else '',
            )
        for item in patient.get('已了解的信息') or []:
            if not isinstance(item, dict) or item.get('内容') not in _GOAL_LABELS:
                continue
            if item.get('状态') not in (None, '已了解'):
                continue
            for text, evidence, _ in _goal_values(item, item.get('已知情况'), item.get('依据')):
                yield from _grounded_goal(text, evidence, sources)


def _grounded_goal(text, evidence, sources: dict, *, preferred: bool = False, identity: str = ''):
    if not isinstance(text, str) or not text.strip():
        return
    if text.strip().rstrip('。.!！') in _EMPTY_GOAL_STATES:
        return
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        quote = item.get('quote', item.get('原文'))
        if item.get('source', item.get('来源')) not in _USER_SOURCES or not isinstance(quote, str):
            continue
        source = next((name for name in _USER_SOURCES if _contains(quote, sources[name])), None)
        if source is None:
            continue
        if len(quote) > MAX_QUOTE_CHARS:
            quote = next(
                (
                    part
                    for part in re.split(r'[。！？\n]', quote)
                    if len(part) <= MAX_QUOTE_CHARS and _contains(text, [part])
                ),
                '',
            )
            if not quote:
                continue
        quote = _source_passage(quote, sources[source])
        if not quote:
            continue
        # A synthesized label cannot introduce symptoms unsupported by its quote.
        label = text if _contains(text, [quote]) else quote
        if len(label) > MAX_LABEL_CHARS or label.strip().rstrip('。.!！') in _EMPTY_GOAL_STATES:
            continue
        yield {
            'ref': _reference('goal', label, quote, path=identity),
            'kind': 'goal',
            'text': label,
            'source': source,
            'quote': quote,
            'path': None,
            'preferred': preferred,
        }
        # A second historical/current quotation must not multiply one concern.
        return


def _scalar(value) -> str:
    return str(value).strip() if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ''


def _available_report(node: dict) -> bool:
    processing = node.get('processing') or {}
    return node.get('status') not in _UNAVAILABLE and not (
        isinstance(processing, dict) and processing.get('status') in _UNAVAILABLE
    )


def _report_detail(item: dict, key: str, label: str, quote: str, exposed: list[str]) -> str:
    value = _scalar(item.get(key))
    if len(value) > MAX_LABEL_CHARS:
        return ''
    return (
        value
        if any(
            _contains(quote, [line]) and _contains(f'{label}：{value}', [line])
            for raw in exposed
            for line in raw.splitlines()
        )
        else ''
    )


def _report_references(reports: list | dict, sources: dict):
    exposed = sources['本次报告']
    if isinstance(reports, dict):
        reports = [reports]
    for index, report in enumerate(reports):
        if not isinstance(report, dict):
            continue
        if not _available_report(report):
            continue
        identity = report.get('reportId') or report.get('documentId') or f'report:{index}'
        nodes = [('', report)] + [
            (f'/sections/{section_index}', section)
            for section_index, section in enumerate(report.get('sections') or [])
            if isinstance(section, dict)
        ]
        seen = set()
        for node_path, node in nodes:
            if not _available_report(node):
                continue
            for key in ('observations', 'expansion'):
                items = node.get(key)
                if not isinstance(items, list):
                    continue
                for item_index, item in enumerate(items):
                    if not isinstance(item, dict) or not _available_report(item):
                        continue
                    name, value, unit = (_scalar(item.get(field)) for field in ('itemName', 'resultValue', 'unit'))
                    if not name or not value or len(name) > MAX_LABEL_CHARS:
                        continue
                    if unit and not value.endswith(unit):
                        value += ' ' + unit
                    quote = f'{name}：{value}'
                    if len(quote) > MAX_QUOTE_CHARS or quote in seen or not _contains(quote, exposed):
                        continue
                    seen.add(quote)
                    path = f'{identity}{node_path}/{key}/{item_index}'
                    yield {
                        'ref': _reference('report', name, quote, path=path),
                        'kind': 'report',
                        'text': name,
                        'source': '本次报告',
                        'quote': quote,
                        'path': path,
                        'preferred': False,
                        'reportStatus': report.get('status', 'unknown'),
                        'flag': _report_detail(item, 'flag', '标记', quote, exposed),
                        'referenceRange': _report_detail(item, 'referenceRange', '参考范围', quote, exposed),
                    }
            # Imaging findings are original report assertions, unlike aiSummary.
            expansion = node.get('expansion')
            if not isinstance(expansion, dict):
                continue
            for key, label in (('findings', '影像所见'), ('diagnosis', '影像诊断')):
                value = _scalar(expansion.get(key))
                quote = f'{label}：{value}'
                if not value or len(value) > MAX_LABEL_CHARS or quote in seen or not _contains(quote, exposed):
                    continue
                seen.add(quote)
                path = f'{identity}{node_path}/expansion/{key}'
                yield {
                    'ref': _reference('report', value, quote, path=path),
                    'kind': 'report',
                    'text': value,
                    'source': '本次报告',
                    'quote': quote,
                    'path': path,
                    'preferred': False,
                    'reportStatus': report.get('status', 'unknown'),
                }


def _published_questions(previous: dict):
    seen = set()
    questions = [item.get('question') for item in previous.get('qaHistory') or [] if isinstance(item, dict)]
    questions.append(previous.get('currentQuestion'))
    for question in questions:
        if not isinstance(question, dict):
            continue
        identity = question.get('questionId') or question.get('id')
        if not identity or identity in seen:
            continue
        seen.add(identity)
        yield question


def _recent_turns(patient: dict, previous: dict) -> list[dict]:
    history = list(previous.get('qaHistory') or [])
    if previous.get('currentQuestion') and patient.get('本次回答') is not None:
        history.append({'question': previous['currentQuestion'], 'answer': patient['本次回答']})
    result, seen = [], set()
    for item in history:
        if not isinstance(item, dict) or item.get('answer') is None:
            continue
        question = item.get('question')
        if not isinstance(question, dict):
            continue
        identity = question.get('questionId') or question.get('id')
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        result.append(
            {
                '问题编号': identity,
                'AI发言': strip_internal_notices(
                    question.get('assistantMessage', question.get('acknowledgementText', ''))
                ),
                '问题': strip_internal_notices(question.get('question', '')),
                '用户回答': item['answer'],
            }
        )
    return result[-MAX_RECENT_TURNS:]


def build_dialogue_context(patient: dict, previous: dict, target: dict | None, *, intake=None, reports=None) -> dict:
    """Return an isolated expression projection, without modifying any input."""
    dimension = (target or {}).get('dimension')
    sources = _sources(patient)
    references = list(_goal_references(patient, intake or {}, sources))
    if dimension != 'medical_history':
        references.extend(_report_references(reports or [], sources))
    # Apply the catalog budget after preserving the patient's goals and the
    # report's explicit flags. This only orders expression references; it never
    # changes the selected question or infers disease/lifestyle associations.
    references.sort(
        key=lambda item: (
            0 if item['kind'] == 'goal' else 1,
            not item['preferred'] if item['kind'] == 'goal' else item.get('flag') in (None, '', '正常', 'normal'),
        )
    )
    family_exclusions = []
    if dimension == 'family_history':
        family_exclusions = list(
            dict.fromkeys(
                item['text']
                for item in references
                if 2 <= len(item['text']) <= 40
                and item['text'] not in ('健康', '身体健康', '健康情况', '健康背景', '身体情况')
            )
        )
        references = []
    catalog = {}
    goal_labels = {}
    for item in references:
        if item['kind'] == 'goal' and _normalized(item['text']) in goal_labels:
            catalog[goal_labels[_normalized(item['text'])]]['preferred'] |= item['preferred']
            continue
        if item['ref'] in catalog:
            catalog[item['ref']]['preferred'] |= item['preferred']
        elif len(catalog) < MAX_REFERENCES:
            catalog[item['ref']] = item
            if item['kind'] == 'goal':
                goal_labels[_normalized(item['text'])] = item['ref']
    counts, latest, last_ref = Counter(), {}, None
    for question in _published_questions(previous):
        ref = question.get('dialogueFocusRef')
        if isinstance(ref, str) and ref:
            counts[ref] += 1
            latest[ref] = question.get('questionId') or question.get('id')
        # A neutral most recent question has no focus. Do not pretend an older
        # focused question was the immediately preceding one.
        last_ref = ref if isinstance(ref, str) and ref else None
    return {
        CONTEXT_KEY: {
            '用途': '仅用于对话引入与表达去重；历史AI发言及关注引用不是用户事实或画像证据。',
            '当前细项': dimension,
            '可用关注点': list(catalog.values()),
            '家族史不绑定的具体关注': family_exclusions,
            '已发布使用': [
                {'ref': ref, 'count': count, 'lastQuestionId': latest[ref]} for ref, count in counts.items()
            ],
            '上一关注引用': last_ref,
            '最近完整对话': _recent_turns(patient, previous),
            '选择规则': (
                '当前问题已由程序确定，不改变选题。只选与本题真实相关的关注点；尊重用户明确优先关注。'
                '明确引用时，关联相当则先避开上一关注引用，再选择已发布使用较少的关注点。'
                '已建立的话题可以自然延续，不必每轮点名主诉或说明帮助目的；未明确引用时 focusRef 可为空。'
                '结合最近完整对话避免重复开头、句式和关注点，不以更换名词或同义词代替自然交流。'
                '家族史不绑定具体关注；个人史仅承接主诉或目标。没有可靠关联时 focusRef 为空，自然引入当前主题。'
            ),
        }
    }


def validate_dialogue_focus(message, patient: dict, *, action: str) -> None:
    """Validate both reference identity and grounded evidence, never associations."""
    ref = getattr(message, 'focusRef', None)
    context = patient.get(CONTEXT_KEY) or {}
    if context.get('当前细项') == 'family_history' and any(
        _contains(text, [message.acknowledgement]) for text in context.get('家族史不绑定的具体关注', [])
    ):
        raise RecordValidationError(
            'dialogue_family_focus_narrowed', '家族史承接不绑定具体主诉或报告指标，保持血亲背景范围'
        )
    if ref is None:
        return
    if context.get('当前细项') == 'family_history' or action not in (
        'open_intake',
        'ask_intake',
        'clarify_concern',
    ):
        raise RecordValidationError('dialogue_focus_forbidden', '本轮应自然衔接，不绑定具体关注引用')
    entry = next((item for item in context.get('可用关注点', []) if item['ref'] == ref), None)
    if entry is None:
        raise RecordValidationError('dialogue_focus_unknown', '关注引用必须从本轮提供的可用关注点中选择')
    if context.get('当前细项') == 'medical_history' and entry['kind'] != 'goal':
        raise RecordValidationError('dialogue_focus_forbidden', '个人病史仅承接已有主诉或健康目标')
    sources = _sources(patient)
    source, quote = entry['source'], entry['quote']
    if not _contains(quote, sources.get(source, [])):
        raise RecordValidationError('dialogue_focus_source_invalid', '关注依据必须能在当前提供的真实来源中核对')
    for evidence in message.evidence:
        if evidence.source != source or not _contains(evidence.quote, sources.get(source, [])):
            continue
        if _contains(quote, [evidence.quote]) or (
            len(_normalized(evidence.quote)) >= 2
            and _contains(evidence.quote, [quote])
            and _contains(entry['text'], [evidence.quote])
        ):
            return
    raise RecordValidationError('dialogue_focus_evidence_required', '关注引用须附有该关注点的真实来源原文依据')
