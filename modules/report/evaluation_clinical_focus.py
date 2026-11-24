"""Select source problems for the patient report's main editorial duties.

Every observed result stays in the detail inventory. An organizer's review
flag does not, by itself, require a second priority row or a new medical
intervention. This module grants no diagnosis, urgency or action permission.
"""
from __future__ import annotations

import re

from .evaluation_schema import clinical_problem_projection_codes


_PRIMARY_RECORD = re.compile(r'主检|主要(?:结论|诊断)|临床诊断|出院诊断|诊断结论|'
    r'\b(?:diagnosis|diagnoses|impression)\b', re.I)
_HISTORY_QUESTION = re.compile(r'曾|既往|病史|历史|诊断|家族|亲属|血亲|父亲|母亲|'
    r'\b(?:history|diagnos\w*|family)\b', re.I)


def _current_witnesses(frame, facts):
    for source in frame.sources:
        fact = facts.get(source.fact_id)
        if (fact is None or fact.kind not in {'clinical', 'self_reported'}
                or not source.quote or source.quote not in fact.value
                or (source.name, source.unit, source.reference_range, source.status, source.date)
                    != (fact.name, fact.unit, fact.reference_range, fact.status, fact.date)
                or source.source_code != ('A' if fact.kind == 'clinical' else 'B')):
            return False
    return bool(frame.sources)


def selected_source_clinical_focus(data, document):
    """Return exact source owners for main summaries and patient-level plans.

    Actual primary records and positive reported symptoms take precedence
    over isolated lab review items. Historical self-reports remain visible
    in detail; when they are the only available clinical material, they can
    serve as explicitly self-reported focus rather than a new diagnosis.
    """
    if document is None:
        return ()
    facts = {fact.fact_id: fact for fact in document.facts}
    codes = clinical_problem_projection_codes(data)
    groups = {}
    for frame in data._clinical_problem_projection:
        code = codes.get(frame.problem_id)
        if code:
            groups.setdefault(code, []).append(frame)
    rows = {row.code: row for row in data.medical_assessment.findings}
    selected, history = [], []
    for code, frames in groups.items():
        if not all(_current_witnesses(frame, facts) for frame in frames):
            continue
        ids = sorted({source.fact_id for frame in frames for source in frame.sources})
        own = [facts[fid] for fid in ids]
        row = rows.get(code)
        name = row.name if row is not None else frames[0].title
        reasons = []
        if any(frame.priority == 'priority' for frame in frames):
            reasons.append('source_organizer_primary')
        if any(fact.kind == 'clinical' and _PRIMARY_RECORD.search(fact.name) for fact in own):
            reasons.append('explicit_primary_clinical_record')
        if any(frame.certainty == 'self_reported' for frame in frames) and any(
                fact.kind == 'self_reported' and not _HISTORY_QUESTION.search(fact.name) for fact in own):
            reasons.append('positive_reported_symptom')
        entry = {'code': code, 'name': name, 'certainty': frames[0].certainty,
            'problemIds': sorted({frame.problem_id for frame in frames}),
            'factIds': ids, 'roleReason': reasons,
            'duty': 'source_problem_editorial_focus_not_new_medical_authority'}
        if reasons:
            selected.append(entry)
        elif any(frame.certainty == 'self_reported' for frame in frames):
            history.append({**entry, 'roleReason': ['reported_condition_context']})
    return tuple(selected or history)
