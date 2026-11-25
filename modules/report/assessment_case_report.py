"""Entry points that connect the model-driven case authoring to the report pipeline."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

CASE_MAINLINE = 'assessment-case-v1'
# Upper bound of the whole-case authoring inside the report deadline. A run
# that cannot finish in this window falls back to the per-chapter path.
CASE_AUTHORING_BUDGET_SECONDS = 780.0
# The per-chapter fallback is only worth starting with this much time left.
CHAPTER_FALLBACK_MIN_SECONDS = 600.0
# Reasons raised by the former problem-organisation and layout steps. They
# describe legacy fields, not the six chapters written from the fact sheet.
FORMER_PATH_REASONS = frozenset(
    {
        'REPORTED_FACT_SOURCE_MISMATCH',
        'REPORTED_FACT_EXTRACTION_INCOMPLETE',
        'REPORTED_FACT_ASSERTION_MISMATCH',
        'CLINICAL_PROBLEM_TITLE_MISMATCH',
        'CLINICAL_PROBLEM_SOURCE_MISMATCH',
        'CLINICAL_PROBLEM_NEGATED_SOURCE',
        'CLINICAL_PROBLEM_VALIDATION_INCOMPLETE',
        'CLINICAL_PROBLEM_ORGANIZATION_UNAVAILABLE',
        'CLINICAL_SEARCH_TERMS_INCOMPLETE',
        'REPORT_LAYOUT_INCOMPLETE',
        'INTEGRATED_RISK_ASSESSMENT_INCOMPLETE',
    }
)
# Legacy fields that follow-up reports read, and the case cell that fills each.
_LEGACY_TEXT_CELLS = (
    (('health_overview', 'overall_summary', 'text'), ('overview.summary',)),
    (('microbiome_assessment', 'structure_interpretation', 'text'), ('micro.structureSummary',)),
    (('comprehensive_conclusion', 'overall_conclusion', 'text'), ('conclusion.summary',)),
)


def case_inputs(*, request, parsed, document, data, blueprint) -> dict[str, Any]:
    """Views, fact sheet and safety-net rule hits of one case (no model call)."""
    from .assessment_case_sheet import build_case_sheet
    from .assessment_clinical_view import build_clinical_view
    from .assessment_microbiome_view import annotate_microbiome_chart_fields
    from .assessment_microbiome_view import build_microbiome_view
    from .assessment_presentation import _input_timing
    from .assessment_presentation import _reported_model_summary
    from .assessment_red_flags import evaluate_red_flags

    clinical = build_clinical_view(request, parsed, document, data, reader_plan=True)
    clinical.pop('_readerOmittedCells', None)
    microbiome = annotate_microbiome_chart_fields(build_microbiome_view(parsed, data, source_facts=document.facts))
    timing = _input_timing(request, microbiome)
    sheet = build_case_sheet(
        request=request,
        parsed=parsed,
        document=document,
        clinical=clinical,
        microbiome=microbiome,
        disease_models=_reported_model_summary(parsed),
        timing=timing,
    )
    try:
        flags = evaluate_red_flags(list(blueprint.facts), parsed.subject, ())
    except Exception:  # the safety net never decides whether a report can be written
        logger.warning('danger-signal safety net unavailable', exc_info=True)
        flags = {'priorityItems': [], 'routineFollowups': [], 'error': True}
    return {'clinical': clinical, 'microbiome': microbiome, 'sheet': sheet, 'flags': flags, 'timing': timing}


async def author_report_case(state, client, *, deadline) -> dict[str, Any] | None:
    """Author the six chapters from the whole case; ``None`` when the analysis is unavailable."""
    from .assessment_case_authoring import author_assessment_case

    audit: dict[str, Any] = {}
    state.audit['caseAuthoring'] = audit
    started = time.monotonic()
    try:
        inputs = await asyncio.to_thread(
            case_inputs,
            request=state.request,
            parsed=state.parsed,
            document=state.document,
            data=state.blueprint.data,
            blueprint=state.blueprint,
        )
        state.case_inputs = inputs
        sheet = inputs['sheet']
        audit['sheet'] = {
            'characters': len(sheet.text),
            'records': len(sheet.entries),
            'hasClinical': sheet.has_clinical,
            'hasQuestionnaire': sheet.has_questionnaire,
            'hasMicrobiome': sheet.has_microbiome,
        }
        if not sheet.entries:
            audit['error'] = 'no_records'
            return None
        case = await author_assessment_case(
            client,
            sheet,
            clinical=inputs['clinical'],
            microbiome=inputs['microbiome'],
            flags=inputs['flags'],
            deadline=min(deadline, started + CASE_AUTHORING_BUDGET_SECONDS),
            audit=audit,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        audit['error'] = type(error).__name__
        logger.warning('whole-case authoring unavailable: %s', type(error).__name__)
        return None
    case['inputs'] = inputs
    case['audit'] = audit
    state.stage_timings['report_case_authoring'] = round((time.monotonic() - started) * 1000, 3)
    logger.info(
        'whole-case authoring: plan=%s cells=%s seconds=%s',
        audit.get('plan'),
        len(case['cells']),
        audit.get('totalSeconds'),
    )
    return case


def rule_only_case(inputs) -> dict[str, Any]:
    """A case without model text: recorded values in tables plus the danger-signal items.

    Used when the analysis is unavailable and there is no time left for the
    per-chapter path, so that a partial report still names what needs prompt
    medical attention.
    """
    from .assessment_case_plan import apply_priority_safety_net
    from .assessment_case_plan import normalize_case_analysis

    sheet = inputs['sheet']
    plan = normalize_case_analysis({}, sheet, clinical=inputs['clinical'])
    apply_priority_safety_net(plan, sheet, inputs['flags'])
    return {
        'plan': plan,
        'cells': {},
        'limits': {},
        'raw': None,
        'inputs': inputs,
        'degraded': True,
        'audit': {'ruleOnly': True},
    }


def legacy_case_blocks(blueprint, case) -> dict[str, list[dict]]:
    """Reviewed blocks for the legacy summary fields that follow-up reports consume."""
    cells = case.get('cells') or {}
    facts = {row['factId'] for row in blueprint.facts if row.get('factId')}
    blocks: dict[str, list[dict]] = {}
    for chapter in blueprint.chapters:
        for target in chapter['targets']:
            cell_ids = next((ids for path, ids in _LEGACY_TEXT_CELLS if tuple(target['path']) == path), None)
            text = next((cells[cell] for cell in cell_ids or () if cells.get(cell)), None)
            if not text or 'interpretation' not in (target.get('allowedRoles') or ()):
                continue
            allowed = set(target.get('sourceFactIds') or ()) & facts
            primary = [fid for fid in (target.get('owner') or {}).get('primaryFactIds') or () if fid in allowed]
            bound = primary[:6] or sorted(allowed)[:1]
            if not bound:
                continue
            blocks.setdefault(chapter['code'], []).append(
                {
                    'targetId': target['id'],
                    'completion': 'complete',
                    'actionDetails': [],
                    'sentences': [
                        {
                            'text': text,
                            'factIds': bound,
                            'role': 'interpretation',
                            'evidence': [],
                            'support': 'clinical_synthesis',
                        }
                    ],
                }
            )
    return blocks


def build_report_case_presentation(*, request, parsed, document, data, blueprint, accepted, case):
    """Six-chapter presentation of an authored case (the checkpointable projection entry)."""
    from .assessment_case_projection import build_case_presentation

    inputs = case.get('inputs')
    if inputs is None:
        inputs = case_inputs(request=request, parsed=parsed, document=document, data=data, blueprint=blueprint)
        case['inputs'] = inputs
    return build_case_presentation(
        request=request,
        parsed=parsed,
        document=document,
        data=data,
        clinical=inputs['clinical'],
        microbiome=inputs['microbiome'],
        sheet=inputs['sheet'],
        case=case,
        timing=inputs['timing'],
    )


def case_incomplete(case) -> list[str]:
    """What the authored case could not provide, for the partial-success reason."""
    audit = case.get('audit') or {}
    sections = audit.get('sections') or {}
    missing = list((case.get('projection') or {}).get('missingCells') or ())
    return [*(f'section:{code}' for code in sections.get('failed') or ()), *missing]


def case_audit_summary(case) -> dict[str, Any]:
    """Audit of one authored case without the patient text."""
    audit = dict(case.get('audit') or {})
    review = dict(audit.get('review') or {})
    for key in ('programFindings', 'remainingFindings'):
        if isinstance(review.get(key), dict):
            review[key] = {'cells': len(review[key])}
    if isinstance(review.get('missedFindings'), list):
        review['missedFindings'] = [
            {'handles': list(finding.get('handles') or ())}
            for finding in review['missedFindings']
            if isinstance(finding, dict)
        ]
    audit['review'] = review
    audit['planAudit'] = (case.get('plan') or {}).get('audit')
    audit['projection'] = case.get('projection')
    return audit
