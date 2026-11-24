"""Three existing evaluate follow-ups projected from the published seven chapters.

No retrieval, product selection or medical authorship occurs here. A private
publication object is minted by the final assembler; plain dictionaries, source
clinical_context and product catalogs cannot grant medical content permission.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from .evaluation_callback import build_evaluation_callback

_TOKEN = object()
_SOURCE_KEY = '_reviewed_seven_chapter'
_MARKER = '_seven_chapter_mainline'
_CHAPTERS = ('healthOverview', 'medicalAssessment', 'microbiomeAssessment',
    'diseaseRiskAssessment', 'nutritionAssessment', 'functionalPathways', 'comprehensiveConclusion')
_TITLES = ('第一部分 健康综合评估', '第二部分 主要问题分析', '第三部分 干预方案', '第四部分 循证支持')


def _plain(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json', by_alias=True, exclude_none=False)
    return deepcopy(value) if isinstance(value, (dict, list)) else value


def _get(value, *keys):
    for key in keys:
        if isinstance(value, dict) and key in value:
            return value[key]
        if not isinstance(value, dict) and hasattr(value, key):
            return getattr(value, key)
    return None


def _text(value):
    if value is None:
        return None
    return str(value) if isinstance(value, (str, int, float, bool)) else None


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _at(value, path):
    for key in path:
        value = value[key] if isinstance(key, int) or isinstance(value, dict) else getattr(value, key)
    return value


def _public_path(data, path):
    value, result = data, []
    for key in path:
        if isinstance(key, int):
            value = value[key]
            result.append(key)
        elif isinstance(value, dict):
            value = value[key]
            result.append(key)
        else:
            field = type(value).model_fields[key]
            result.append(field.serialization_alias or field.alias or key)
            value = getattr(value, key)
    return result


def _same_display(a, b):
    # The existing public table formatter may join paragraph whitespace only.
    return isinstance(a, str) and isinstance(b, str) and ''.join(a.split()) == ''.join(b.split())


@dataclass(frozen=True)
class ReviewedEvaluationFollowupSource:
    public_data: dict
    paragraphs: tuple[dict, ...]
    focus: tuple[dict, ...]
    subject: dict
    sample: dict
    status: str
    partial_reasons: tuple[str, ...]
    gaps: tuple[str, ...]
    fingerprint: str
    _token: Any

    def __deepcopy__(self, memo):
        return self


def _source_state(source):
    return {'data': source.public_data, 'paragraphs': source.paragraphs,
        'focus': source.focus, 'subject': source.subject, 'sample': source.sample,
        'status': source.status, 'partialReasons': source.partial_reasons, 'gaps': source.gaps}


def reviewed_followup_source_matches(source):
    if not isinstance(source, ReviewedEvaluationFollowupSource) or source._token is not _TOKEN:
        return False
    try:
        return source.fingerprint == _hash(_source_state(source))
    except (TypeError, ValueError, AttributeError):
        return False


def mint_reviewed_followup_source(*, blueprint, data, accepted, subject, sample,
                                  status, partial_reasons):
    """Seal the actual applied, final-numbered paragraphs, never a public DTO."""
    gaps, paragraphs = [], []
    subject, sample = _plain(subject) or {}, _plain(sample) or {}
    try:
        # Only the data subtree is retained. Required envelope IDs are local
        # placeholders, never patient metadata or a second report request.
        public = build_evaluation_callback({'userId': 'reviewed-followup-projection',
            'sessionId': 'reviewed-followup-projection', 'status': status, 'report': {
            'evaluationData': data, 'subject': subject, 'sample': sample}})['data']
        public.pop('assessmentPresentation', None)
        targets = {target['id']: target for chapter in blueprint.chapters for target in chapter['targets']}
        final_numbers = {row['number'] for row in public['appendices']['referenceLiterature']}
        seen = set()
        for block in accepted or []:
            target = targets.get(block.get('targetId'))
            if target is None or block['targetId'] in seen:
                gaps.append('unknown_or_duplicate_applied_target')
                continue
            path = list(target['path'])
            try:
                working = _at(data, path)
                wire_path = _public_path(data, path)
                published = _at(public, wire_path)
            except (KeyError, IndexError, TypeError, AttributeError):
                gaps.append('applied_target_not_published')
                continue
            if working != block.get('text') or not _same_display(published, block.get('text')):
                gaps.append('applied_target_text_changed')
                continue
            numbers = list(dict.fromkeys(block.get('evidenceRefs') or []))
            if any(type(number) is not int or number not in final_numbers for number in numbers):
                gaps.append('applied_target_reference_missing')
                continue
            sentences = deepcopy(block.get('sentences') or [])
            if not sentences or any(not isinstance(sentence, dict) or sentence.get('role') not in target['allowedRoles']
                    for sentence in sentences):
                gaps.append('applied_target_role_changed')
                continue
            if not _same_display('\n\n'.join(str(s.get('text') or '').strip() for s in sentences), block.get('text')):
                gaps.append('applied_target_sentences_changed')
                continue
            details = []
            for detail in block.get('actionDetails') or []:
                if not isinstance(detail, dict):
                    gaps.append('applied_action_details_changed')
                    continue
                indices = detail.get('sentenceIndices')
                if (detail.get('kind') not in {'test', 'consultation', 'lifestyle', 'alert', 'avoid'}
                        or not isinstance(indices, list) or not indices
                        or any(type(i) is not int or i < 0 or i >= len(sentences)
                            or sentences[i].get('role') != 'action' for i in indices)):
                    gaps.append('applied_action_details_changed')
                    continue
                action_text = '\n'.join(sentences[i]['text'] for i in indices)
                name, timeframe = detail.get('name'), detail.get('timeframe')
                if (not isinstance(name, str) or not name or name not in action_text
                        or (timeframe is not None and (not isinstance(timeframe, str)
                            or not timeframe or timeframe not in action_text))):
                    gaps.append('applied_action_details_changed')
                    continue
                details.append(deepcopy(detail))
            owner = deepcopy(target.get('owner') or {})
            paragraphs.append({'targetId': block['targetId'], 'path': path, 'publicPath': wire_path,
                'chapter': wire_path[0], 'text': published, 'owner': owner,
                'factIds': list(block.get('factIds') or []), 'evidenceRefs': numbers,
                'sentences': sentences, 'actionDetails': details, 'contentDuty': target.get('contentDuty'),
                'completion': block.get('completion', 'complete')})
            seen.add(block['targetId'])
        focus = tuple(deepcopy(blueprint.focus))
    except (TypeError, ValueError, KeyError, AttributeError):
        # Recovery keeps the same declared fields and never calls old models.
        public, focus = {}, ()
        gaps.append('reviewed_publication_unavailable')
    state = {'data': public, 'paragraphs': tuple(paragraphs), 'focus': focus,
        'subject': subject, 'sample': sample, 'status': str(status),
        'partialReasons': tuple(partial_reasons or ()), 'gaps': tuple(dict.fromkeys(gaps))}
    return ReviewedEvaluationFollowupSource(public, tuple(paragraphs), focus, subject, sample,
        str(status), tuple(partial_reasons or ()), state['gaps'], _hash(state), _TOKEN)


def is_seven_chapter_followup(data):
    if not isinstance(data, dict) or data.get('type') == 'gutBarrier':
        return False
    if _SOURCE_KEY in data or data.get(_MARKER) is True:
        return True
    context = (data.get('diagnosis_summary') or {}).get('clinical_context') or {}
    return isinstance(context, dict) and any(str(key).startswith('report.section.evaluate.') for key in context)


def followup_metadata(data):
    """Only transport metadata reaches the shared archive/terminal wrapper."""
    return {key: deepcopy(data[key]) for key in ('type', 'subject', 'sample', 'status', 'groupId') if key in data}


def _subject(source, fallback):
    raw = source.subject if source else fallback.get('subject') or {}
    sample = source.sample if source else fallback.get('sample') or {}
    return {'name': _get(raw, 'name'), 'gender': _get(raw, 'gender', 'sex'),
        'age': _text(_get(raw, 'age')), 'report_date': _get(sample, 'reportDate', 'report_date')}


def _script(text=None):
    return {'kind': 'script', 'text': text}


def _section(number):
    base = {'part_no': number, 'title': _TITLES[number-1], 'key_points': [],
        'script': {'kind': 'script', 'opening': None, 'body': [], 'closing': None},
        'visual_guide': [], 'faq': [], 'cautions': {'must_say': [], 'easy_wrong': [], 'forbidden': []}}
    if number == 2:
        base.update(problems=[], microbiome_talk={'kind': 'script', 'text': None, 'must_say': None})
    elif number == 3:
        base.update(pillars=[], plan_talk=[], clinical_followup_talk={'kind': 'script', 'intro': None, 'items': []},
            product_bridge={'kind': 'script', 'step1_mechanism': None, 'step2_need': None,
                'step3_direction': None, 'step4_handoff': None, 'forbidden': []})
    elif number == 4:
        base.update(note=None, qa_table=[], evidence_levels=[], glossary=[], limits_talk=_script())
    return base


def empty_projected_followup(kind, *, user_id, session_id, subject=None):
    """Declared output fields without legacy treatment or test defaults."""
    raw_subject = _plain(subject) or {}
    body = {'type': kind, 'userId': user_id, 'sessionId': session_id,
        'subject': {'name': _text(_get(raw_subject, 'name')),
            'gender': _text(_get(raw_subject, 'gender', 'sex')), 'age': _text(_get(raw_subject, 'age')),
            'report_date': _text(_get(raw_subject, 'report_date', 'reportDate'))}, 'evidence': []}
    if kind == 'instruction':
        body.update(info={key: None for key in ('intestine_type', 'best_bacteria', 'unbalance_type',
            'body_mass', 'risk_warning', 'constitution')}, recommend={key: None for key in (
            'mdf_code', 'pfmt_code', 'PFMT', 'recommended_combination_method', 'mdf_product_rationale',
            'pfmt_product_rationale', 'combination_rationale', 'mdf_action_focus', 'mdf_observation_focus',
            'pfmt_action_focus', 'pfmt_observation_focus', 'mdf_productSpecification', 'mdf_shelfLife',
            'mdf_compositeEfficacyComponents', 'mdf_usageInstructions', 'pfmt_viableCountPerCapsule',
            'pfmt_totalViableCountPerBox', 'pfmt_usageInstructions')},
            usage={'pfmt_usage': [], 'mdf_usage': []}, exception_handling=[],
            notes={'pfmt_notes': [], 'mdf_notes': []},
            non_drug_intervention={'lifestyle_intervention': {'intro': None, 'items': []},
                'gut_microbiota_regulation': {'intro': None,
                    'microbiota_findings': {'summary': None, 'points': [], 'evidence_refs': []},
                    'probiotics': {'summary': None, 'products': []},
                    'prebiotics': {'summary': None, 'points': []},
                    'gut_barrier': {'applicable': False, 'summary': None, 'points': []}, 'diet_synergy': None},
                'supplements': {'intro': None, 'items': [], 'tip': None}},
            recommended_tests={'intro': None, 'items': []})
        body['info']['evidence_refs'] = []
        body['recommend']['MDF'] = []
    elif kind == 'opinion':
        body.update(diagnosis_summary={'conclusion': None, 'clinical_context': {}, 'risk': None, 'evidence_refs': []},
            diagnostic_basis={'abnormal_findings': [], 'evidence_refs': []},
            diagnostic_advice={'report_interpretation': [], 'Plan': {key: None for key in (
                '未来1-2周', '未来1个月', '未来2-3个月', '未来6-12个月')},
                'not_recommended_behaviors': {key: None for key in ('不建议依据微量DNA抗感染',
                    '不建议以单一指标作为唯一干预目标', '不建议仅凭功能预测盲目补充维生素')}, 'medical_alerts': []})
    elif kind == 'explain':
        body.update(manual_meta={'title': '用户报告解读参考手册', 'scene': '30分钟一对一面谈',
            'report_date': None, 'evaluate_status': None, 'data_gaps': None, 'partial_reasons': [], 'confidential_note': None},
            preface={'quick_card': {'overall': {'score': None, 'level_label': None}, 'dimension_scores': [],
                'indicator_rows': [], 'imaging_rows': [], 'microbe_row_count': 0, 'microbe_rows': []},
                'reader_notice': [], 'opening_script': {'kind': 'script', 'self_intro': None,
                    'role_statement': None, 'agenda': None}},
            sections=[_section(number) for number in range(1, 5)],
            closing={'wrap_up_script': {'kind': 'script', 'summary': None, 'three_things': [],
                'confirm': None, 'follow_up': None, 'contact': None},
                'forbidden_words': [], 'self_check': []})
    else:
        raise ValueError('unknown evaluate follow-up type')
    return body


def _joined(paragraphs):
    return '\n\n'.join(dict.fromkeys(p['text'] for p in paragraphs if p.get('text'))) or None


def _refs(paragraphs):
    return list(dict.fromkeys(number for p in paragraphs for number in p['evidenceRefs']))


def _select(source, prefix, *, duty=None):
    return [p for p in source.paragraphs if tuple(p['path'][:len(prefix)]) == tuple(prefix)
        and (duty is None or p['contentDuty'] == duty)]


def _actions(source):
    return [p for p in source.paragraphs if p['contentDuty'] == 'management'
        and any(s.get('role') == 'action' for s in p['sentences'])]


def _action_details(source, kind=None):
    return [(p, detail) for p in source.paragraphs for detail in p.get('actionDetails', [])
        if kind is None or detail['kind'] == kind]


def _plan_phase(timeframe):
    if not isinstance(timeframe, str):
        return None
    # A display bucket never invents a timetable; values remain the exact
    # approved action and only these explicitly stated durations are routed.
    value = re.sub(r'\s+', '', timeframe).replace('至', '-').replace('—', '-').replace('～', '-')
    value = re.sub(r'^(未来|接下来)', '', value)
    value = re.sub(r'(内|后)$', '', value)
    return {'1-2周': '未来1-2周', '1个月': '未来1个月',
        '2-3个月': '未来2-3个月', '6-12个月': '未来6-12个月'}.get(value)


def _focus_rows(source):
    wanted = {row.get('code') for row in source.focus}
    rows = source.public_data.get('medicalAssessment', {}).get('findings', [])
    return [row for row in rows if row.get('code') in wanted]


def _owner_paragraphs(source, code):
    return [p for p in source.paragraphs if p['owner'].get('code') == code]


def _qualified_name(row):
    name = row.get('name')
    if row.get('certainty') == 'self_reported':
        return '本人自述：' + str(name or '')
    if row.get('certainty') == 'suspected':
        return '来源疑似：' + str(name or '')
    return name


def _opinion(body, source):
    summary = _select(source, ('comprehensive_conclusion', 'overall_conclusion')) or _select(source, ('health_overview', 'overall_summary'))
    body['diagnosis_summary'].update(conclusion=_joined(summary), evidence_refs=_refs(summary),
        clinical_context={chapter: _joined([p for p in source.paragraphs if p['chapter'] == chapter])
            for chapter in _CHAPTERS if any(p['chapter'] == chapter for p in source.paragraphs)})
    findings = []
    for row in _focus_rows(source):
        paragraphs = _owner_paragraphs(source, row['code'])
        if not paragraphs:
            continue
        actions = [p for p in _actions(source) if p['owner'].get('code') == row['code']]
        mechanism = [p for p in paragraphs if p['owner'].get('semanticRole') == 'mechanism']
        findings.append({'problem': _qualified_name(row), 'priority': None,
            'indicators': [{'name': row.get('name'), 'value': _text(row.get('value')), 'status': row.get('sourceStatus')}],
            'biological_mechanism': _joined(mechanism), 'recommendations': [p['text'] for p in actions],
            'tips': [p['text'] for p in paragraphs if p not in actions and p not in mechanism],
            'evidence_refs': _refs(paragraphs), 'evidence_level': None})
    body['diagnostic_basis'].update(abnormal_findings=findings,
        evidence_refs=list(dict.fromkeys(number for row in findings for number in row['evidence_refs'])))
    body['diagnostic_advice']['report_interpretation'] = [p['text'] for p in source.paragraphs
        if p['chapter'] in {'medicalAssessment', 'diseaseRiskAssessment'} and p['contentDuty'] != 'management']
    for action, detail in _action_details(source):
        phase = _plan_phase(detail.get('timeframe'))
        if phase:
            current = body['diagnostic_advice']['Plan'][phase]
            body['diagnostic_advice']['Plan'][phase] = '\n\n'.join(dict.fromkeys(filter(None, [current, action['text']])))
        if detail['kind'] == 'alert':
            body['diagnostic_advice']['medical_alerts'].append(action['text'])
        if detail['kind'] == 'avoid' and detail['name'] in body['diagnostic_advice']['not_recommended_behaviors']:
            body['diagnostic_advice']['not_recommended_behaviors'][detail['name']] = action['text']
    # The existing dynamic clinical_context renders every approved chapter.
    return [p for p in source.paragraphs if p['chapter'] in _CHAPTERS]


def _body_mass(source):
    rows = [row for row in source.public_data.get('medicalAssessment', {}).get('findings', [])
        if row.get('name') in {'体重', '体重指数', 'BMI', '腰围', '身高', '肥胖'}]
    paragraphs = [p for row in rows for p in _owner_paragraphs(source, row.get('code'))
        if p['contentDuty'] != 'management']
    if paragraphs:
        return _joined(paragraphs), paragraphs
    return ('；'.join(str(row.get('name')) + '：' + str(row['value']) + str(row.get('unit') or '')
        for row in rows if row.get('value') is not None) or None), []


def _instruction(body, source):
    data = source.public_data
    structure = data.get('microbiomeAssessment', {}).get('overallStructure', [])
    enterotype = next((row.get('value') for row in structure if row.get('code') == 'enterotype'), None)
    balance = next((row.get('value') if row.get('value') is not None else row.get('sourceStatus')
        for row in structure if row.get('code') in {'balance', 'balanceState', 'beBalanceScore'}), None)
    overview = _select(source, ('health_overview', 'overall_summary'))
    risk = _select(source, ('disease_risk_assessment',))
    body_mass, mass_paragraphs = _body_mass(source)
    rendered = [*overview, *risk, *mass_paragraphs]
    body['info'].update(intestine_type=_text(enterotype), unbalance_type=_text(balance),
        body_mass=body_mass, constitution=_joined(overview), risk_warning=_joined(risk),
        evidence_refs=_refs(rendered))
    gut = body['non_drug_intervention']['gut_microbiota_regulation']
    flora = [p for p in source.paragraphs if p['chapter'] == 'microbiomeAssessment']
    gut['microbiota_findings'].update(summary=_joined(flora), evidence_refs=_refs(flora))
    next_steps = list({p['targetId']: p for p, _ in _action_details(source, 'lifestyle')}.values())
    body['non_drug_intervention']['lifestyle_intervention']['items'] = [
        {'key': p['owner'].get('code') or p['targetId'], 'title': p['owner'].get('name'),
            'summary': p['text'], 'points': []} for p in next_steps]
    clinical = [p for p in _actions(source) if p['path'][:2] == ['comprehensive_conclusion', 'priority_directions']]
    body['recommended_tests']['intro'] = _joined(clinical)
    rendered.extend([*flora, *next_steps, *clinical])
    for action, attributes in _action_details(source):
        # New structured details are accepted only as exact approved text.
        if attributes.get('kind') == 'test' and isinstance(attributes.get('name'), str) and attributes['name'] in action['text']:
            timeframe = attributes.get('timeframe')
            if timeframe is not None and (not isinstance(timeframe, str) or timeframe not in action['text']):
                continue
            body['recommended_tests']['items'].append({'name': attributes['name'], 'reason': action['text'], 'timeframe': timeframe})
            rendered.append(action)
    return rendered


def _manual_problem(row, source, number):
    paragraphs = _owner_paragraphs(source, row['code'])
    states = [p for p in paragraphs if p['contentDuty'] != 'management']
    actions = [p for p in _actions(source) if p['owner'].get('code') == row['code']]
    steps = {key: _script() for key in ('status', 'causes', 'mechanism', 'outlook', 'action')}
    steps['status']['text'] = _joined(states)
    steps['action']['text'] = _joined(actions)
    rendered = [*states, *actions]
    for role in ('causes', 'mechanism', 'outlook'):
        selected = [p for p in paragraphs if p['owner'].get('semanticRole') == role]
        steps[role]['text'] = _joined(selected)
        rendered.extend(selected)
    return {'no': number, 'problem': _qualified_name(row), 'priority': None,
        'certainty_note': row.get('certainty'), 'five_steps': steps, 'goal': None,
        'analogy': None, 'evidence_refs': _refs(rendered)}, rendered


def _explain(body, source):
    from .explain_defaults import READER_NOTICE, CONFIDENTIAL_NOTE
    from .explain_compliance import FORBIDDEN_WORDS
    data = source.public_data
    body['manual_meta'].update(report_date=body['subject']['report_date'], evaluate_status=source.status,
        partial_reasons=list(source.partial_reasons), confidential_note=CONFIDENTIAL_NOTE)
    body['preface']['reader_notice'] = list(READER_NOTICE)
    body['closing']['forbidden_words'] = deepcopy(list(FORBIDDEN_WORDS))
    card = body['preface']['quick_card']
    overall = data.get('healthOverview', {}).get('overallScore', {})
    card['overall'] = {'score': _text(overall.get('score')), 'level_label': overall.get('assessmentLevel')}
    card['dimension_scores'] = [{'name': row.get('name'), 'score': _text(row.get('score')),
        'level_label': row.get('assessmentLevel')} for row in data.get('healthOverview', {}).get('assessmentChart', [])
        if row.get('score') is not None or row.get('assessmentLevel') is not None]
    medical = data.get('medicalAssessment', {}).get('findings', [])
    for row in medical:
        cell = {'name': row.get('name'), 'value': _text(row.get('value')), 'unit': row.get('unit'),
            'reference': row.get('referenceRange'), 'status': row.get('sourceStatus'),
            'date': next((s.get('observedAt') for s in row.get('sources', []) if s.get('observedAt')), None),
            'related_problem': _qualified_name(row)}
        kind = 'imaging_rows' if row.get('system') == '影像科' else 'indicator_rows'
        card[kind].append(cell)
    taxa = data.get('microbiomeAssessment', {}).get('taxa', [])
    card.update(microbe_row_count=len(taxa), microbe_rows=[{'name': row.get('name'),
        'value': _text(row.get('value')), 'unit': row.get('unit'), 'reference': row.get('referenceRange'),
        'status': row.get('sourceStatus')} for row in taxa])
    groups = (
        [p for p in source.paragraphs if p['chapter'] == 'healthOverview'],
        [p for p in source.paragraphs if p['chapter'] in {'medicalAssessment', 'microbiomeAssessment',
            'diseaseRiskAssessment', 'nutritionAssessment', 'functionalPathways'}],
        _actions(source), [],
    )
    for section, paragraphs in zip(body['sections'], groups):
        section['script']['body'] = list(dict.fromkeys(p['text'] for p in paragraphs))
        section['key_points'] = [{'text': p['text'], 'anchor': '.'.join(map(str, p['publicPath']))} for p in paragraphs]
    rendered = [p for group in groups for p in group]
    problems = [_manual_problem(row, source, i+1)
        for i, row in enumerate(_focus_rows(source)) if _owner_paragraphs(source, row['code'])]
    body['sections'][1]['problems'] = [problem for problem, _ in problems]
    for _, paragraphs in problems:
        rendered.extend(paragraphs)
    flora = [p for p in source.paragraphs if p['chapter'] == 'microbiomeAssessment']
    body['sections'][1]['microbiome_talk']['text'] = _joined(flora)
    clinical = [p for p in _actions(source) if p['path'][:2] == ['comprehensive_conclusion', 'priority_directions']]
    body['sections'][2]['clinical_followup_talk']['items'] = [p['text'] for p in clinical]
    body['sections'][2]['pillars'] = [{'code': p['owner'].get('code') or p['targetId'],
        'title': p['owner'].get('name'), 'script': _script(p['text'])} for p in _actions(source)]
    for action, detail in _action_details(source):
        phase = detail.get('timeframe')
        if isinstance(phase, str):
            body['sections'][2]['plan_talk'].append({'phase': phase, 'content': action['text']})
            rendered.append(action)
    summary = _select(source, ('comprehensive_conclusion', 'overall_conclusion')) or _select(source, ('health_overview', 'overall_summary'))
    body['closing']['wrap_up_script']['summary'] = _joined(summary)
    priorities = [p for p in _select(source,
        ('comprehensive_conclusion', 'priority_directions')) if p['path'][-1] == 'basis'][:3]
    body['closing']['wrap_up_script']['three_things'] = [p['text'] for p in priorities]
    rendered.extend([*summary, *priorities])
    # A bibliography's existence does not mint an FAQ, evidence grade or claim.
    return rendered


def _attach_references(body, source, rendered):
    # Reuse final published identities only for paragraphs actually rendered
    # in this report; no candidate backfill or new evidence grading.
    used = set(_refs(rendered))
    from .schemas import FixedEvidence
    body['evidence'] = [FixedEvidence(number=row['number'], title=row.get('title'),
        source=row.get('journal'), publication_date=row.get('year'), url=row.get('url'),
        authors=list(row.get('authors') or []), study_type=row.get('studyType')).model_dump(
            mode='json', by_alias=True, exclude_none=False)
        for row in source.public_data.get('appendices', {}).get('referenceLiterature', [])
        if row.get('number') in used]


def project_reviewed_followup(kind, source, *, user_id, session_id, fallback=None):
    valid = reviewed_followup_source_matches(source)
    source = source if valid else None
    body = empty_projected_followup(kind, user_id=user_id, session_id=session_id,
        subject=_subject(source, fallback or {}))
    gaps = list(source.gaps) if source else ['reviewed_publication_missing_or_changed']
    if source:
        rendered = {'instruction': _instruction, 'opinion': _opinion, 'explain': _explain}[kind](body, source)
        _attach_references(body, source, rendered)
    return body, tuple(dict.fromkeys(gaps))
