"""Source-first evaluation generation behind the existing report API.

One source projection, one chapter blueprint and one bibliography are carried
through the execution.  Risk discovery/registries and the former per-field
composition/reprojection chain are deliberately not part of this mainline.
The transport, admission gates and durable job lifecycle stay in pipeline.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from utils.log import getLogger

from .evaluation_schema import ReferenceLiterature
from .evaluation_schema import SourceReference
from .performance import report_metrics_snapshot
from .schemas import ClinicalReportBlock
from .schemas import EvidenceCoverageIssue
from .schemas import ReportAudit
from .schemas import ReportContent
from .schemas import ReportPerformanceAudit
from .schemas import SuccessCallback

logger = getLogger(__name__)

_DIAGNOSTIC_SECRET_KEY = re.compile(
    r'(?:api[_-]?key|authorization|password|passwd|access[_-]?token|refresh[_-]?token|'
    r'secret|cookie|credential|connection[_-]?uri)', re.I)


def _diagnostic_plain(value, seen=None):
    """JSON-safe work product, without opaque clients or credential fields."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (datetime, Decimal, Path)):
        return str(value)
    if isinstance(value, Enum):
        return _diagnostic_plain(value.value, seen)
    seen = set() if seen is None else seen
    if id(value) in seen:
        return {'diagnosticCycle': type(value).__qualname__}
    seen = {*seen, id(value)}
    if isinstance(value, BaseModel):
        # Read data fields without invoking the failing public serializer.
        return _diagnostic_plain({type(value).model_fields[key].serialization_alias
                or type(value).model_fields[key].alias or key:
                getattr(value, key) for key in type(value).model_fields}, seen)
    if isinstance(value, dict):
        return {str(key): ('[REDACTED]' if _DIAGNOSTIC_SECRET_KEY.search(str(key))
            else _diagnostic_plain(item, seen)) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_diagnostic_plain(item, seen) for item in value]
    from dataclasses import fields
    from dataclasses import is_dataclass
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: ('[REDACTED]' if _DIAGNOSTIC_SECRET_KEY.search(field.name)
            else _diagnostic_plain(getattr(value, field.name), seen))
            for field in fields(value) if field.name != '_token'}
    return {'diagnosticUnsupportedType': type(value).__module__ + '.' + type(value).__qualname__}


def _safe_validation_errors(error):
    """Keep validation addresses and reasons, never rejected input or ctx."""
    try:
        rows = error.errors(include_input=False, include_url=False)
    except (AttributeError, TypeError):
        return []
    return [{'loc': list(row.get('loc', ())), 'type': str(row.get('type', '')),
        'msg': str(row.get('msg', ''))[:400]} for row in rows]


def _write_private_diagnostic(label, value):
    """Write request-owned diagnostics under the existing private log root.

    The location is configured by the existing LOG_DIR, never a public request
    parameter. Failure to record a diagnostic cannot alter report execution.
    """
    temporary = None
    try:
        from config.env import LOG_DIR

        from .artifacts import active_final_report_id
        final_id = active_final_report_id()
        if not final_id:
            return None
        directory = Path(LOG_DIR) / 'report-private'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        final_id = str(final_id)
        identity = re.sub(r'[^A-Za-z0-9_.-]', '_', final_id)[:120]
        name = re.sub(r'[^A-Za-z0-9_.-]', '_', label)[:80]
        target = directory / (identity + '.' + name + '.json')
        temporary = target.with_name('.' + target.name + '.' + str(os.getpid()) + '.tmp')
        body = {'kind': 'PRIVATE_REPORT_DIAGNOSTIC_NOT_CALLBACK',
            'recordedAt': datetime.now(UTC).isoformat(), 'finalReportId': final_id,
            'stage': label, 'data': _diagnostic_plain(value)}
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(body, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, target)
        target.chmod(0o600)
        logger.info('report private diagnostic recorded: stage=%s path=%s', label, target)
        return str(target)
    except Exception as error:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        logger.warning('report private diagnostic unavailable: stage=%s error_type=%s',
            label, type(error).__name__)
        return None

_WRITABLE_TEXT_FIELDS = frozenset({
    'text', 'summary', 'explanation', 'association', 'health_meaning',
    'description', 'basis', 'followup_action', 'attention_basis',
    'evidence_strength', 'modifiability', 'source_advice', 'resistance_notice',
})


@dataclass
class EvaluationReportState:
    request: Any
    parsed: Any
    document: Any
    health_assessment: Any
    microbes: Any
    diseases: Any
    health: Any
    started: float
    stage_timings: dict[str, float]
    blueprint: Any = None
    evidence: Any = None
    approved_blocks: dict[str, list[dict]] = field(default_factory=dict)
    accepted_blocks: list[dict] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)
    incomplete: list[str] = field(default_factory=list)
    withheld_diseases: list[Any] = field(default_factory=list)


def _facts(blueprint):
    return {row['factId']: row for row in blueprint.facts if row.get('factId')}


async def _project(operation, *args):
    """Finish one owned source projection before cancellation recovery reads it."""
    worker = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if worker.done() and not worker.cancelled():
            worker.exception()
        raise


def _targets(blueprint):
    return {target['id']: target for chapter in blueprint.chapters
        for target in chapter['targets']}


def _node(data, path):
    current = data
    for part in path:
        current = current[part] if isinstance(part, int) or isinstance(current, dict) else getattr(current, part)
    return current


def _source_references(facts, identifiers):
    refs = []
    for fid in identifiers:
        fact = facts[fid]
        candidates = fact.get('sources') or [{
            'sourceCode': fact.get('sourceCode') or (
                'A' if fact.get('kind') == 'clinical' else
                'B' if fact.get('kind') == 'self_reported' else 'C'),
            'recordId': fid, 'observedAt': fact.get('date'),
        }]
        for raw in candidates:
            reference = SourceReference.model_validate(raw)
            if reference not in refs:
                refs.append(reference)
    return refs


def _source_scalar_signature(data):
    """Check original values independently of generated prose and citations."""
    keys = ('code', 'name', 'scientific_name', 'value', 'unit',
        'reference_range', 'source_status', 'assessment_level',
        'detection_status', 'certainty')
    result = []

    def walk(node, path=()):
        if isinstance(node, BaseModel):
            selected = {key: getattr(node, key) for key in keys
                if key in type(node).model_fields}
            if selected:
                result.append((path, selected))
            for key in type(node).model_fields:
                if key not in {'source_items', 'reference_literature',
                        'indicator_standards', 'sources'}:
                    walk(getattr(node, key), (*path, key))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, (*path, index))
    walk(data)
    return hashlib.sha256(json.dumps(result, ensure_ascii=False,
        sort_keys=True, default=str).encode()).hexdigest()


def apply_reviewed_report_blocks(blueprint, evidence, approved_blocks):
    """Apply text only to program-owned target addresses; never rewrite facts."""
    from .assessment_reviewed_fields import record_accepted_field
    from .assessment_reviewed_fields import source_identity

    data = blueprint.data
    before = _source_scalar_signature(data)
    targets, facts = _targets(blueprint), _facts(blueprint)
    originals = {row['number']: row for row in evidence.summaries}
    accepted, rejected = [], []
    seen = set()
    for chapter in blueprint.chapters:
        chapter_targets = {target['id'] for target in chapter['targets']}
        authored = approved_blocks.get(chapter['code'], [])
        fallbacks = [(target['sourceFallback'], True) for target in chapter['targets'] if target.get('sourceFallback')]
        for block, is_fallback in [(block, False) for block in authored] + fallbacks:
            identifier = block.get('targetId')
            if is_fallback and identifier in seen:
                continue
            if identifier not in chapter_targets or identifier in seen:
                rejected.append({'targetId': identifier, 'reason': 'unknown_or_duplicate_target'})
                continue
            target = targets[identifier]
            path = tuple(target['path'])
            if not path or path[-1] not in _WRITABLE_TEXT_FIELDS:
                rejected.append({'targetId': identifier, 'reason': 'source_field_is_read_only'})
                continue
            sentences = block.get('sentences')
            if not isinstance(sentences, list) or not sentences:
                continue
            primary_portion = len(sentences)
            if not is_fallback and target.get('sourceSupplement'):
                def comparison_text(text):
                    return re.sub(r'[\W_]+', '', text).casefold()

                prior = [(part['text'], comparison_text(part['text']), set(part.get('factIds') or ()))
                         for part in sentences if isinstance(part, dict) and isinstance(part.get('text'), str)]
                sentences = [
                    *sentences,
                    *(part for part in target['sourceSupplement']
                      if not any(text == part['text'] or (
                          part.get('sourceQuote') and ids.intersection(part['factIds'])
                          and comparison_text(part['sourceQuote']) in comparable
                      ) for text, comparable, ids in prior)),
                ]
            block_facts, block_refs, pieces = [], [], []
            primary_bindings = set()
            valid = True
            for index, sentence in enumerate(sentences):
                if not isinstance(sentence, dict):
                    valid = False
                    break
                text = sentence.get('text')
                identifiers = sentence.get('factIds') or []
                if (not isinstance(text, str) or not text.strip()
                        or sentence.get('role') not in target['allowedRoles']
                        or not isinstance(identifiers, list)
                        or any(fid not in facts or fid not in target['sourceFactIds']
                            for fid in identifiers)):
                    valid = False
                    break
                for citation in sentence.get('evidence') or []:
                    if not isinstance(citation, dict):
                        valid = False
                        break
                    number = citation.get('number')
                    record = originals.get(number) if type(number) is int else None
                    quotes = citation.get('quotes')
                    if (record is None or not isinstance(quotes, list) or not quotes
                            or any(not isinstance(quote, str) or not quote.strip()
                                or quote not in record['sourceText'] for quote in quotes)):
                        valid = False
                        break
                    if number not in block_refs:
                        block_refs.append(number)
                if not valid:
                    break
                pieces.append(text.strip())
                block_facts.extend(fid for fid in identifiers if fid not in block_facts)
                if index < primary_portion:
                    primary_bindings.update(identifiers)
            if not valid:
                rejected.append({'targetId': identifier, 'reason': 'invalid_fact_or_original_anchor'})
                continue
            primary = target.get('owner', {}).get('primaryFactIds') or []
            from .evaluation_report_authoring import reviewed_management_owner_binding
            reviewed_owner = reviewed_management_owner_binding(block, target, facts)
            if primary and not set(primary).intersection(primary_bindings) and not reviewed_owner:
                rejected.append({'targetId': identifier, 'reason': 'target_primary_source_missing'})
                continue
            parent = _node(data, path[:-1])
            text = '\n\n'.join(pieces)
            allowed_values = target.get('cellContract', {}).get('allowedValues')
            if allowed_values and text not in allowed_values:
                rejected.append({'targetId': identifier, 'reason': 'template_cell_value_outside_vocabulary'})
                continue
            destinations = [(parent, path[-1], path, source_identity(parent))]
            if mirror := target.get('sharedMicrobiomePanel'):
                mirror_path = tuple(mirror['path'])
                metric = _node(data, mirror_path[:-1])
                if ((block.get('completion') != 'complete' and not is_fallback)
                        or mirror['factId'] not in block_facts or metric.code != mirror['code']
                        or not {mirror['factId'], mirror['locator']}.issubset(
                            source.record_id for source in metric.sources if source.source_code == 'C'
                        )):
                    rejected.append({'targetId': identifier, 'reason': 'shared_panel_original_anchor_missing'})
                    continue
                destinations.append((metric, mirror_path[-1], mirror_path, source_identity(metric)))
            completion = block.get('completion', 'complete')
            measured_complete = is_fallback and completion == 'source_only' and target.get('sourceOnlyComplete') is True
            for record, text_field, _, _ in destinations:
                setattr(record, text_field, text)
                if hasattr(record, 'evidence_refs'):
                    record.evidence_refs = list(dict.fromkeys([*record.evidence_refs, *block_refs]))
                if hasattr(record, 'sources'):
                    for source in _source_references(facts, block_facts):
                        if source not in record.sources:
                            record.sources.append(source)
                if hasattr(record, 'availability'):
                    numeric_missing = hasattr(record, 'score') and record.score is None
                    record.availability = ('partial' if numeric_missing
                        or (completion == 'source_only' and not measured_complete) else 'available')
            accepted.append({'targetId': identifier, 'text': text,
                'factIds': block_facts, 'evidenceRefs': block_refs,
                'sentences': deepcopy(sentences),
                'actionDetails': deepcopy(block.get('actionDetails', [])),
                **({'ownerFactIds': deepcopy(block['ownerFactIds']),
                    'ownerBindingMode': 'object_identity', 'ownerBindingValid': True}
                    if reviewed_owner else {}),
                **({'measuredSourceComplete': True} if measured_complete else {}),
                'completion': completion})
            for record, _, location, native_sources in destinations:
                record_accepted_field(record, target_id=identifier, path=location,
                    primary_fact_ids=primary, used_fact_ids=block_facts,
                    completion=completion, native_sources=native_sources)
            seen.add(identifier)
    if _source_scalar_signature(data) != before:
        raise ValueError('report authorship changed a source value')
    return accepted, rejected



def _apply_evaluation_blocks(state, blocks):
    from .assessment_presentation import retain_applicable_symptom_blocks
    from .assessment_presentation import reviewed_conclusion_fallback

    blueprint = state.blueprint
    blocks = retain_applicable_symptom_blocks(blueprint, state.evidence, blocks)
    accepted, rejected = apply_reviewed_report_blocks(blueprint, state.evidence, blocks)
    fallback = reviewed_conclusion_fallback(blueprint, accepted, state.document, state.parsed.subject)
    if not fallback:
        return accepted, rejected
    target, block = fallback
    chapter = next(chapter for chapter in blueprint.chapters if chapter['code'] == 'comprehensiveConclusion')
    conclusion = replace(blueprint, chapters=[{**chapter, 'targets': [target]}])
    reused, failed = apply_reviewed_report_blocks(conclusion, state.evidence, {chapter['code']: [block]})
    if reused:
        state.audit['reviewedConclusionReuseTarget'] = target['id']
    return [*accepted, *reused], [*rejected, *failed]


def _record_content_gaps(blueprint, accepted, rejected, audit):
    targets = _targets(blueprint)
    compatibility = {target_id for target_id, target in targets.items()
                     if target.get('publicationScope') == 'compatibility'}
    accepted_ids = {block['targetId'] for block in accepted}
    audit['unresolved'] = [
        row for row in audit.get('unresolved', []) if row.get('targetId') not in accepted_ids
    ]
    all_missing = [target_id for target_id, target in targets.items()
                   if target.get('required') and target_id not in accepted_ids]
    missing = [target_id for target_id in all_missing if target_id not in compatibility]
    source_only = [block['targetId'] for block in accepted
                   if block['completion'] == 'source_only' and targets[block['targetId']].get('required')
                   and block['targetId'] not in compatibility]
    if compatibility:
        audit['compatibilityContentGaps'] = {
            'missingRequiredTargets': [target_id for target_id in all_missing if target_id in compatibility],
            'publicationRejections': [row for row in rejected if row.get('targetId') in compatibility],
            'medicalUnresolved': [row for row in audit.get('medicalUnresolved', ())
                                  if row.get('targetId') in compatibility],
        }
    measured_complete = {block['targetId'] for block in accepted if block.get('measuredSourceComplete') is True}
    audit['missingRequiredTargets'] = missing
    audit['sourceOnlyRequiredTargets'] = source_only
    if measured_complete:
        audit['completedMeasuredSourceTargets'] = sorted(measured_complete)
    reused = {audit.get('reviewedConclusionReuseTarget')} & accepted_ids

    # Optional editorial failures and optional additions to a complete measured
    # finding stay in the audit. A publication rejection remains a real failure.
    optional_editorial_ids = {
        target_id for target_id, target in targets.items()
        if target['path'][0] == '_assessment_editorial' and not target['required']
    }
    return bool(
        missing or set(source_only) - measured_complete
        or any(row.get('targetId') not in (optional_editorial_ids | compatibility) for row in rejected)
        or any(row.get('targetId') not in (optional_editorial_ids | measured_complete | reused | compatibility)
               for row in audit.get('medicalUnresolved', []))
    )


def finalize_report_references(data, evidence):
    """Keep and renumber the bibliography once, using final actual citations."""
    from .web_source_policy import publication_url

    used = []
    owners = []

    def walk(node, path=()):
        if isinstance(node, BaseModel):
            if hasattr(node, 'evidence_refs'):
                owners.append((node, '.'.join(map(str, path))))
                for number in node.evidence_refs:
                    if number not in used:
                        used.append(number)
            for key in type(node).model_fields:
                if key not in {'reference_literature', 'source_items'}:
                    walk(getattr(node, key), (*path, key))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, (*path, index))
    walk(data)
    for key, narrative in data._assessment_editorial.items():
        walk(narrative, ('assessmentPresentation', key))
    supplied = {reference.number: reference for reference in evidence.references}
    if any(number not in supplied for number in used):
        raise ValueError('final report cites an unknown publication')
    remap = {old: index + 1 for index, old in enumerate(used)}
    sections = {old: [] for old in used}
    for owner, path in owners:
        for old in owner.evidence_refs:
            if path not in sections[old]:
                sections[old].append(path)
        owner.evidence_refs = [remap[old] for old in dict.fromkeys(owner.evidence_refs)]
    from .web_source_policy import evidence_grade
    data.appendices.reference_literature = []
    references = []
    for old in used:
        reference = supplied[old].model_copy(deep=True, update={
            'number': remap[old], 'url': publication_url(supplied[old].url, supplied[old].doi),
        })
        references.append(reference)
        grade, label = evidence_grade(reference.category, reference.study_types, reference.title)
        data.appendices.reference_literature.append(ReferenceLiterature(
            number=remap[old], title=reference.title, authors=reference.authors,
            year=str(reference.year) if reference.year is not None else None,
            journal=reference.source, url=reference.url, doi=reference.doi,
            study_type=label, evidence_grade=grade, supporting_sections=sections[old]))
    return references, remap


def _document_sections(blueprint, accepted):
    """Carry the same reviewed prose/claims into the existing follow-up DTO."""
    targets = _targets(blueprint)
    result = []
    for block in accepted:
        path = targets[block['targetId']]['path']
        code = ('overview' if list(path) == ['health_overview', 'overall_summary', 'text']
            else 'evaluate.' + '.'.join(map(str, path)))
        quotes, claims, fact_ids = {}, {}, {}
        for sentence in block['sentences']:
            for citation in sentence.get('evidence', []):
                key = str(citation['number'])
                quotes.setdefault(key, [])
                quotes[key].extend(quote for quote in citation['quotes'] if quote not in quotes[key])
                claims.setdefault(key, [])
                if sentence['text'] not in claims[key]:
                    claims[key].append(sentence['text'])
                fact_ids.setdefault(key, [])
                fact_ids[key].extend(fid for fid in sentence['factIds'] if fid not in fact_ids[key])
        # The established private block DTO has a 4000-character transport
        # limit. Keep every character, in ordered parts, rather than clipping.
        pieces = [block['text'][index:index+4000] for index in range(0, len(block['text']), 4000)]
        for index, piece in enumerate(pieces):
            result.append(ClinicalReportBlock(code=code if index == 0 else f'{code}.part{index+1}',
                text=piece, fact_ids=block['factIds'], evidence_refs=block['evidenceRefs'],
                evidence_quotes={key: '\n'.join(values) for key, values in quotes.items()},
                evidence_claims={key: '\n'.join(values) for key, values in claims.items()},
                evidence_fact_ids=fact_ids))
    return result


def _performance(state, references):
    audits = state.evidence.query_audits if state.evidence is not None else []
    counters, elapsed = report_metrics_snapshot()
    state.stage_timings['total'] = round((time.monotonic() - state.started) * 1000, 3)
    counts, times = {}, {}
    for audit in audits:
        for channel in audit.channels:
            if channel.status != 'not_requested':
                key = f'{channel.provider or "unknown"}:{channel.channel}'
                counts[key] = counts.get(key, 0) + 1
                times[key] = round(times.get(key, 0) + channel.elapsed_ms, 3)
    return ReportPerformanceAudit(stage_timings_ms=state.stage_timings,
        provider_query_counts=counts, provider_elapsed_ms=times,
        runtime_counters=counters, runtime_elapsed_ms=elapsed,
        raw_hit_count=sum(a.raw_count for a in audits),
        screened_hit_count=sum(a.screened_count for a in audits),
        interpreted_hit_count=sum(
            bool(row.get('topicLinks')) for row in (state.evidence.summaries if state.evidence else [])
        ),
        public_evidence_count=len(references),
        source_snapshot_count=len(state.parsed.source_history),
        source_error_count=len(state.parsed.source_errors))


def _payload(state, *, extra_reasons=()):
    from .evaluation_schema import EvaluationData

    reasons = list(dict.fromkeys([*state.incomplete, *state.document.incomplete, *extra_reasons]))
    data = state.blueprint.data.model_copy(deep=True) if state.blueprint is not None else EvaluationData()
    references = []
    remap = {}
    if state.blueprint is not None and state.evidence is not None:
        references, remap = finalize_report_references(data, state.evidence)
    # The reader deals with one provider: no other parties, capture markers or
    # workflow labels in displayed text (values and units are untouched).
    from .evaluation_presentation import present_for_reader
    from .evaluation_presentation import reader_quality
    before = data.model_copy(deep=True)
    try:
        state.audit['readerWordingChanges'] = (
            0 if isinstance(getattr(data, '_assessment_content_plan', None), dict)
            else present_for_reader(data)
        )
        type(data).model_validate(data.model_dump(mode='json', by_alias=True))
    except (ValueError, TypeError) as error:
        # Wording is presentation only; it never costs the delivery.
        data = before
        state.audit['readerWordingError'] = type(error).__name__
    state.audit['readerQuality'] = reader_quality(data)
    if state.blueprint is not None:
        from .assessment_presentation import assessment_projection_issues
        from .assessment_presentation import build_assessment_presentation

        phase = time.monotonic()
        try:
            data.assessment_presentation = build_assessment_presentation(
                request=state.request, parsed=state.parsed, document=state.document,
                data=data, blueprint=state.blueprint, accepted=state.accepted_blocks,
            )
            gaps = assessment_projection_issues(data.assessment_presentation, state.blueprint)
            if gaps:
                state.audit['assessmentProjectionIssues'] = gaps
                if 'REPORT_LAYOUT_INCOMPLETE' not in reasons:
                    reasons.append('REPORT_LAYOUT_INCOMPLETE')
        except (ValueError, TypeError, KeyError) as error:
            if 'REPORT_LAYOUT_INCOMPLETE' not in reasons:
                reasons.append('REPORT_LAYOUT_INCOMPLETE')
            state.audit['assessmentPresentationError'] = type(error).__name__
            logger.warning('assessment presentation incomplete: %s', type(error).__name__)
        state.stage_timings['assessment_presentation'] = round((time.monotonic() - phase) * 1000, 3)
    summary = data.health_overview.overall_summary.text
    state.health.health_profile = summary
    document = state.document.model_copy(deep=True)
    if state.blueprint is not None:
        from .evaluation_report_bridge import adapt_followup_document
        adapt_followup_document(document, state.blueprint.data,
            state.blueprint, state.accepted_blocks)
    from .clinical_report import blocks as clinical_blocks
    for block in clinical_blocks(document):
        block.evidence_refs = [remap[number] for number in block.evidence_refs if number in remap]
        for field_name in ('evidence_quotes', 'evidence_claims', 'evidence_fact_ids'):
            original = getattr(block, field_name)
            setattr(block, field_name, {str(remap[int(number)]): value
                for number, value in original.items() if int(number) in remap})
    document.background['reportGeneration'] = {
        'mainline': 'template-cell-seven-chapters-v2',
        'audit': deepcopy(state.audit),
        'publicationNumberMap': remap,
    }
    performance = _performance(state, references)
    _write_private_diagnostic('generation-audit', {
        'mainline': 'template-cell-seven-chapters-v2', 'partialReasons': reasons,
        'audit': state.audit,
        'templateLayout': getattr(data, '_template_layout', None),
        'templateDimensionScores': getattr(data, '_template_dimension_scores', None),
        'sourceFactCount': len(state.blueprint.facts) if state.blueprint else 0,
        'acceptedTargetCount': len(state.accepted_blocks),
        'adoptedPublicationCount': len(references),
        'selectedOriginals': state.evidence.summaries if state.evidence else [],
        'evidencePlan': state.evidence.plan if state.evidence else [],
        'performance': performance,
    })
    logger.info('report source-first generation performance: %s', json.dumps({
        'status': 'partial_success' if reasons else 'success',
        'partialReasons': reasons,
        'performance': performance.model_dump(mode='json', by_alias=True),
    }, ensure_ascii=False))
    result = SuccessCallback(user_id=state.request.user_id,
        session_id=state.request.session_id,
        status='partial_success' if reasons else 'success',
        generated_at=datetime.now(UTC), partial_reasons=reasons,
        report=ReportContent(subject=state.parsed.subject, sample=state.parsed.sample,
            health_assessment=state.health_assessment, executive_summary=summary,
            abnormal_microbes=state.microbes, disease_risks=state.diseases,
            health_management=state.health, evidence_references=references,
            clinical_document=document, evaluation_data=data),
        audit=ReportAudit(source_count_reconciliation=state.parsed.panel_counts,
            evidence_queries=state.evidence.query_audits if state.evidence else [],
            withheld_disease_items=state.withheld_diseases,
            coverage_issues=[EvidenceCoverageIssue(target_type='source',
                target_id='evaluation', reason=reason) for reason in reasons],
            performance=performance))
    if state.blueprint is not None:
        from .evaluation_report_followups import mint_reviewed_followup_source
        published = deepcopy(state.accepted_blocks)
        for block in published:
            block['evidenceRefs'] = [remap[number] for number in block['evidenceRefs'] if number in remap]
            for sentence in block['sentences']:
                sentence['evidence'] = [{**citation, 'number': remap[citation['number']]}
                    for citation in sentence.get('evidence', ()) if citation['number'] in remap]
        # This server-owned value is deliberately not a public model field.
        # The API body is unchanged; follow-ups consume only this run's exact
        # applied/reviewed content, with the final public reference numbers.
        object.__setattr__(result, '_reviewed_seven_chapter', mint_reviewed_followup_source(
            blueprint=state.blueprint, data=data, accepted=published,
            subject=state.parsed.subject, sample=state.parsed.sample,
            status=result.status, partial_reasons=reasons))
    return result


def content_plan_degraded_sections(content_plan):
    """Plan sections whose reviewed items were dropped or replaced by rule rows.

    Such a plan keeps ``status == 'reviewed'`` for downstream compatibility;
    the callback reports it as the existing partial layout reason.
    """
    status = content_plan.get('sectionStatus') if isinstance(content_plan, dict) else None
    sections = sorted(
        key for key, entry in (status or {}).items()
        if isinstance(entry, dict) and entry.get('state') in {'partial', 'fallback', 'failed'}
    )
    if not sections and isinstance(content_plan, dict) and content_plan.get('completeness') == 'degraded':
        return ['unspecified']
    return sections


def _authoring_mode():
    """'chapter' (default) writes and reviews by chapter; 'cell' is the former path."""
    try:
        from config.env import REPORT_AUTHORING_MODE
    except Exception:
        return 'chapter'
    return REPORT_AUTHORING_MODE


# Legacy seven-chapter paths that the reviewed follow-up projections
# (evaluation_report_followups, evaluation_report_bridge) read by name.
_FOLLOWUP_PATH_PREFIXES = (
    ('health_overview', 'overall_summary'),
    ('comprehensive_conclusion', 'overall_conclusion'),
    ('comprehensive_conclusion', 'priority_directions'),
    ('comprehensive_conclusion', 'next_steps'),
    ('disease_risk_assessment',),
    ('medical_assessment', 'findings'),
    ('microbiome_assessment',),
)


def followup_consumes_path(path):
    """Whether a legacy compatibility field has a named follow-up consumer."""
    path = tuple(path or ())
    return any(path[:len(prefix)] == prefix for prefix in _FOLLOWUP_PATH_PREFIXES)


def source_only_fallback_block(target, facts):
    """Recorded values of an unfinished duty, or None when nothing can be listed.

    The text copies only names, values, units, printed references and source
    states. It is published as ``source_only`` so the report stays partial.
    """
    from .assessment_source_receipts import source_only_text

    owner = target.get('owner') or {}
    contract = target.get('cellContract') or {}
    if (target.get('sourceFallback') or target.get('sharedMicrobiomePanel') or contract.get('allowedValues')
            or owner.get('requiresPersonalAction') or 'observation' not in (target.get('allowedRoles') or ())):
        return None
    allowed = set(target.get('sourceFactIds') or ())
    primary = [fid for fid in owner.get('primaryFactIds') or () if fid in allowed and fid in facts]
    if not primary:
        return None
    rows = [facts[fid] for fid in primary]
    # Values outside the printed reference come first; the listing is bounded.
    rows.sort(key=lambda row: 0 if row.get('abnormal') is True
              or re.search(r'偏|高|低|异常|阳性|升|降', str(row.get('status') or '')) else 1)
    text = source_only_text(rows)
    if not text:
        return None
    used = [row.get('factId') for row in rows if row.get('factId') and (row.get('name') or '') in text]
    return {
        'targetId': target['id'], 'completion': 'source_only', 'analysisGenerated': False,
        'sentences': [{'text': text, 'factIds': used or primary[:1], 'role': 'observation',
                       'evidence': [], 'support': 'source'}],
    }


def rescue_unfinished_public_targets(state, targets, public_ids, required_ids, unresolved, salvage, accepted,
                                     checkpoint):
    """Publish retained reviewed sentences, else a source-only listing, for open duties."""
    facts = _facts(state.blueprint)
    open_ids = {target_id for _, target_id in unresolved if target_id in public_ids} | (required_ids - accepted)
    retained, listed = [], []
    for target_id in sorted(open_ids - accepted):
        code = next((code for code, tid in unresolved if tid == target_id), None)
        if code is None:
            code = next((chapter['code'] for chapter in state.blueprint.chapters
                         if any(target['id'] == target_id for target in chapter['targets'])), None)
        if target_id in salvage:
            code, kept = salvage[target_id]
            checkpoint(code, [deepcopy(kept['block'])], [])
            retained.append({'targetId': target_id, 'chapterCode': code, 'phase': kept.get('phase'),
                             'removed': deepcopy(kept.get('removed', []))})
            continue
        block = source_only_fallback_block(targets[target_id], facts)
        if block is not None and code is not None:
            checkpoint(code, [block], [])
            listed.append({'targetId': target_id, 'chapterCode': code})
    if retained:
        state.audit['sentenceRetention'] = retained
    if listed:
        state.audit['sourceOnlyFallbackTargets'] = listed


async def _author_chapters(state, client, subject, deadline, *, recall=None):
    """Review all ready public duties before retrying unresolved targets."""
    from .evaluation_report_writer import author_report_chapters
    from .fallback_policy import EVALUATE_AUTHORING_TARGET_SECONDS
    from .fallback_policy import EVALUATE_MODEL_CONCURRENCY

    targets = _targets(state.blueprint)
    model_gate = asyncio.Semaphore(EVALUATE_MODEL_CONCURRENCY)
    started = time.monotonic()
    # Optional sweeps must finish by the delivery target; the hard deadline
    # still bounds the first pass and the first repair sweep.
    target_deadline = None if deadline is None else min(
        deadline, getattr(state, 'started', started) + EVALUATE_AUTHORING_TARGET_SECONDS,
    )
    public_chapters = [
        {**chapter, 'targets': [target for target in chapter['targets']
            if target.get('publicationScope') != 'compatibility']}
        for chapter in state.blueprint.chapters
    ]
    # Legacy fields reach readers only through the follow-up projections,
    # which read named paths. Prose for other legacy fields has no consumer.
    compatibility_chapters = [
        {**chapter, 'targets': [target for target in chapter['targets']
            if target.get('publicationScope') == 'compatibility' and followup_consumes_path(target['path'])]}
        for chapter in state.blueprint.chapters
    ]
    unconsumed = sum(target.get('publicationScope') == 'compatibility' and not followup_consumes_path(target['path'])
                     for chapter in state.blueprint.chapters for target in chapter['targets'])
    if unconsumed:
        state.audit['compatibilityTargetsWithoutConsumer'] = unconsumed
    final_chapters = [chapter for chapter in public_chapters if chapter['number'] in {1, 7}]
    public_ids = {target['id'] for chapter in public_chapters for target in chapter['targets']}
    required_ids = {target_id for target_id in public_ids
                    if targets[target_id].get('required') and not targets[target_id].get('sourceOnlyComplete')}
    source_planned = isinstance(getattr(state.blueprint.data, '_assessment_content_plan', None), dict)
    unresolved = {(row.get('chapterCode'), row.get('targetId')): row for row in state.audit.get('writerUnresolved', [])}
    salvage = {}
    active_target_ids = set()
    invocation_count = 0

    def accepted_public_ids():
        return {block['targetId'] for blocks in state.approved_blocks.values() for block in blocks}

    def unfinished_ids(scope_ids):
        accepted = accepted_public_ids()
        return ((required_ids & scope_ids) | {
            target_id for _, target_id in unresolved if target_id in scope_ids
        }) - accepted

    def selected_chapters(chapters, identifiers):
        return [{**chapter, 'targets': [target for target in chapter['targets'] if target['id'] in identifiers]}
                for chapter in chapters]

    def checkpoint(code, blocks, missing):
        # Runs synchronously on the report event loop: no other chapter can
        # replace the shared checkpoint between this merge and publication.
        bucket = {block['targetId']: block for block in state.approved_blocks.get(code, [])}
        for block in blocks:
            bucket.setdefault(block['targetId'], block)
        state.approved_blocks[code] = list(bucket.values())
        for target_id in bucket:
            unresolved.pop((code, target_id), None)
        for row in missing:
            if row.get('salvage'):
                # Keep the latest retained subset; a later failed sweep need
                # not have a reviewed candidate of its own.
                salvage[row.get('targetId')] = (code, row['salvage'])
            if row.get('targetId') not in bucket:
                unresolved[(row.get('chapterCode'), row.get('targetId'))] = row
        state.audit['writerUnresolved'] = list(unresolved.values())
        state.audit['reviewedBlocks'] = deepcopy(state.approved_blocks)

    async def author(chapters, scope, phase_deadline, *, evidence=None):
        nonlocal invocation_count
        accepted = accepted_public_ids()
        chapters = [{**chapter, 'targets': [target for target in chapter['targets']
                     if target['id'] not in accepted and target['id'] not in active_target_ids]}
                    for chapter in chapters]
        chapters = [chapter for chapter in chapters if chapter['targets']]
        if not chapters:
            return
        invocation_ids = {target['id'] for chapter in chapters for target in chapter['targets']}
        # Admission, reservation and the startup snapshot occur without an
        # await. Concurrent invocations cannot claim the same target, and a
        # writer never mistakes the live checkpoint for a startup snapshot.
        active_target_ids.update(invocation_ids)
        previous_reviewed = deepcopy(state.approved_blocks)
        invocation_count += 1
        authoring_scope = {
            'phase': scope,
            'invocationId': f'{scope}:{invocation_count:03d}',
            'previousReviewedSnapshotIds': sorted(accepted),
        }
        local_audit = {}
        node_started = time.monotonic()
        status = 'interrupted'
        try:
            result = await author_report_chapters(
                client,
                blueprint=replace(state.blueprint, chapters=chapters),
                evidence=state.evidence if evidence is None else evidence,
                subject=subject,
                deadline=phase_deadline,
                audit=local_audit,
                previous_reviewed=previous_reviewed,
                authoring_scope=authoring_scope,
                context_targets=targets,
                model_gate=model_gate,
                on_checkpoint=checkpoint,
                prior_review_findings=deepcopy(state.audit.get('writer', [])),
                prior_unresolved=[deepcopy(row) for (_, tid), row in unresolved.items() if tid in invocation_ids],
                defer_content_repairs=source_planned and scope.startswith('public_'),
            )
            for chapter in chapters:
                code = chapter['code']
                checkpoint(code, result.get(code, []), [])
            status = 'completed'
        finally:
            active_target_ids.difference_update(invocation_ids)
            # Local writer journals never overwrite another node's accepted
            # blocks. Preserve accepted partial batches before timeout recovery.
            for chapter in chapters:
                code = chapter['code']
                checkpoint(code, local_audit.get('reviewedBlocks', {}).get(code, []), [])
            for row in local_audit.get('writerUnresolved', []):
                code, target_id = row.get('chapterCode'), row.get('targetId')
                if row.get('salvage'):
                    salvage[target_id] = (code, row['salvage'])
                accepted = {block['targetId'] for block in state.approved_blocks.get(code, ())}
                if target_id not in accepted:
                    unresolved[(code, target_id)] = row
            for key in ('writer', 'writerTimings'):
                state.audit.setdefault(key, []).extend(local_audit.get(key, []))
            elapsed = round((time.monotonic() - node_started) * 1000, 3)
            codes = [chapter['code'] for chapter in chapters]
            state.stage_timings[f'authoring.{"+".join(codes)}.{scope}'] = elapsed
            state.audit.setdefault('authoringSchedule', []).append(
                {
                    'chapters': codes,
                    'scope': scope,
                    'authoringScope': deepcopy(authoring_scope),
                    'startedMs': round((node_started - started) * 1000, 3),
                    'elapsedMs': elapsed,
                    'status': status,
                    'targetCount': sum(len(chapter['targets']) for chapter in chapters),
                }
            )
            state.audit['writerUnresolved'] = list(unresolved.values())
            state.audit['medicalUnresolved'] = list(unresolved.values())

    async def author_body(chapter):
        def aggregate(target):
            return bool((target.get('owner') or {}).get('aggregate'))

        await author(
            [{**chapter, 'targets': [t for t in chapter['targets'] if not aggregate(t)]}], 'details', deadline
        )
        await author(
            [{**chapter, 'targets': [t for t in chapter['targets'] if aggregate(t)]}], 'summary', deadline
        )

    tasks = [] if source_planned else [
        asyncio.create_task(author_body(chapter))
        for chapter in public_chapters
        if chapter['number'] in {2, 3, 4, 5, 6}
    ]
    try:
        first_deadline = deadline
        if source_planned and deadline is not None:
            available = max(0.0, deadline - time.monotonic())
            first_deadline -= min(180.0, available * 0.18)
        remaining = None if first_deadline is None else max(0.0, first_deadline - time.monotonic())
        if remaining is not None and remaining <= 0 and recall is not None:
            await recall()
        if remaining is None or remaining > 0:
            try:
                async with asyncio.timeout(remaining) as public_window:
                    if source_planned:
                        early_ids = {
                            target_id for target_id in public_ids
                            if (owner := targets[target_id].get('owner', {})).get('usesReviewedFactsOnly')
                            and (owner.get('contentScopeCode') or '').startswith(
                                ('body.', 'system.', 'specialtyRow.', 'symptomEvidence.'),
                            )
                        }
                        if recall is None:
                            await author(public_chapters, 'public_first_pass', first_deadline)
                        else:
                            from .evaluation_report_evidence import ReportEvidenceBundle

                            async with asyncio.TaskGroup() as ready:
                                recalled = ready.create_task(recall())
                                if early_ids:
                                    ready.create_task(author(
                                        selected_chapters(public_chapters, early_ids),
                                        'public_originals', first_deadline, evidence=ReportEvidenceBundle(),
                                    ))
                                await recalled
                                ready.create_task(author(public_chapters, 'public_first_pass', first_deadline))
                    else:
                        if recall is not None:
                            await recall()
                        if tasks:
                            await asyncio.gather(*tasks)
                        await author(final_chapters, 'overview_and_conclusion', deadline)
            except TimeoutError:
                if not public_window.expired() and (first_deadline is None or time.monotonic() < first_deadline):
                    raise
                state.audit['concurrentPublicAuthoringBudgetExhausted'] = True
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        # The first repair sweep reaches every unresolved duty before
        # spending another review on any one troublesome batch.
        accepted_ids = accepted_public_ids()
        repair_ids = unfinished_ids(public_ids)
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if repair_ids and (remaining is None or remaining > 0):
            # Review receipts are exact-text bound. Reuse accepted cells and
            # spend remaining time only on unfinished public duties; a
            # metadata failure must not leave the remaining budget idle.
            repair_chapters = selected_chapters(public_chapters, repair_ids)
            try:
                async with asyncio.timeout(remaining) as repair_window:
                    await author(repair_chapters, 'public_repair', deadline)
            except TimeoutError:
                if not repair_window.expired() and (deadline is None or time.monotonic() < deadline):
                    raise
                state.audit['publicRepairBudgetExhausted'] = True
            accepted_ids = accepted_public_ids()
        if source_planned and (needed := unfinished_ids(public_ids) & required_ids):
            from statistics import median

            repair_calls = [
                row['serviceMs'] / 1000 for row in state.audit.get('writerTimings', ())
                if row.get('stage') == 'model_call' and row.get('status') == 'success'
                and (row.get('authoringScope') or {}).get('phase') == 'public_repair'
                and row.get('serviceMs', 0) > 0
            ]
            remaining = None if target_deadline is None else max(0.0, target_deadline - time.monotonic())
            minimum_sweep = 3 * median(repair_calls) if repair_calls else None
            if remaining is None or minimum_sweep is not None and remaining >= minimum_sweep:
                try:
                    async with asyncio.timeout(remaining) as final_review_window:
                        await author(selected_chapters(public_chapters, needed), 'public_final_repair', target_deadline)
                except TimeoutError:
                    if not final_review_window.expired() and (
                            target_deadline is None or time.monotonic() < target_deadline):
                        raise
                    state.audit['publicFinalRepairBudgetExhausted'] = True
                accepted_ids = accepted_public_ids()
            else:
                state.audit['publicFinalRepairSkippedTarget'] = {
                    'remainingSeconds': remaining, 'minimumSweepSeconds': minimum_sweep,
                }
        if source_planned:
            # Duties still open after the last sweep keep their reviewed
            # sentences that pass every check, or show their recorded values.
            rescue_unfinished_public_targets(
                state, targets, public_ids, required_ids, unresolved, salvage, accepted_public_ids(), checkpoint,
            )
            accepted_ids = accepted_public_ids()
        if required_ids <= accepted_ids and not any(target_id in public_ids for _, target_id in unresolved):
            # Compatibility fields retain their names and source values.
            # Their optional prose can use spare time after all published
            # content; its timeout is private, not a report-wide failure.
            remaining = None if target_deadline is None else max(0.0, target_deadline - time.monotonic())
            from statistics import median

            call_durations = [row['serviceMs'] / 1000 for row in state.audit.get('writerTimings', ())
                              if row.get('stage') == 'model_call' and row.get('status') == 'success'
                              and isinstance(row.get('serviceMs'), (int, float)) and row['serviceMs'] > 0]
            # Optional prose needs at least a normal drafting/review pass.
            # Optional jobs must not consume the outer timeout after the
            # published report is complete and leave no time to assemble it.
            minimum_pass = 2 * median(call_durations) if call_durations else None
            if remaining is None or minimum_pass is not None and remaining > minimum_pass:
                try:
                    async with asyncio.timeout(remaining):
                        await author(compatibility_chapters, 'compatibility', target_deadline)
                except TimeoutError:
                    state.audit['compatibilityAuthoringBudgetExhausted'] = True
                except Exception as error:
                    state.audit['compatibilityAuthoringFailure'] = type(error).__name__
            else:
                state.audit['compatibilityAuthoringSkippedBudget'] = {
                    'remainingSeconds': remaining,
                    'minimumPassSeconds': minimum_pass,
                    'reason': 'insufficient_window' if minimum_pass is not None else 'no_duration_estimate',
                }
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        state.audit['medicalUnresolved'] = list(unresolved.values())


def recover_evaluation_report(state, *, reason):
    """Deliver the same source/accepted-chapter checkpoint after interruption."""
    if state.blueprint is None:
        from .evaluation_report_plan import build_report_blueprint

        state.blueprint = build_report_blueprint(state.request, state.parsed, state.document, state.health_assessment)
    if state.blueprint is not None:
        if state.evidence is None:
            from .evaluation_report_evidence import ReportEvidenceBundle
            state.evidence = ReportEvidenceBundle()
        blocks = state.audit.get('reviewedBlocks') or state.approved_blocks or {}
        accepted, rejected = _apply_evaluation_blocks(state, blocks)
        state.accepted_blocks = accepted
        state.audit['acceptedTargets'] = [block['targetId'] for block in accepted]
        state.audit['publicationRejections'] = rejected
        state.document.sections = _document_sections(state.blueprint, accepted)
    return _payload(state, extra_reasons=[reason])


async def generate_evaluation_report(*, pipeline, request, parsed, document,
        client, health_assessment, microbes, diseases, health, progress,
        started, stage_timings, withheld_diseases=()):
    from .clinical_report import attach_clinical_problems
    from .clinical_report import organize_problems
    from .evaluation_report_authoring import author_report_sections
    from .evaluation_report_evidence import recall_report_evidence
    from .evaluation_report_evidence import retrieve_report_evidence
    from .evaluation_report_plan import build_report_blueprint
    from .evaluation_report_plan import prepare_template_data
    from .evaluation_template_layout import arrange_template_rows
    from .evaluation_template_layout import link_template_risk_context
    from .evaluation_template_scores import score_template_dimensions

    state = EvaluationReportState(request, parsed, document, health_assessment,
        microbes, diseases, health, started, stage_timings,
        withheld_diseases=list(withheld_diseases))
    if progress is not None:
        progress.evaluation_report_state = state
    if parsed.source_errors:
        state.incomplete.append('DOCUMENT_SOURCE_INCOMPLETE')
    state.incomplete.extend(document.incomplete)
    work_seconds = max(.01, pipeline.generation_timeout_seconds
        - min(60., pipeline.generation_timeout_seconds * .1))
    work_deadline = min(started + work_seconds,
        getattr(progress, 'evaluation_work_deadline', None) or started + work_seconds)
    # The outer work timeout also owns application and presentation. A later
    # inner deadline must not turn optional work into a report-wide timeout.
    completion_reserve = min(10.0, work_seconds * .01)
    deadline = max(started, work_deadline - completion_reserve)
    state.audit['generationDeadlines'] = {
        'workDeadline': work_deadline,
        'authoringDeadline': deadline,
        'completionReserveSeconds': completion_reserve,
    }
    chapter_mode = _authoring_mode() == 'chapter'
    state.audit['authoringMode'] = 'chapter' if chapter_mode else 'cell'
    if chapter_mode:
        # Arithmetic on laboratory values is done once by formula, before the
        # clinical problems are organised, so every chapter cites one number.
        from .evaluation_derived_indices import append_derived_indices
        state.audit['derivedIndexFactIds'] = append_derived_indices(document, parsed.subject)
    phase = time.monotonic()
    await organize_problems(client, document, evaluation_mode=True)
    state.audit['clinicalSourceDiagnostics'] = deepcopy(
        getattr(document, '_clinical_problem_source_validation', []))
    if state.audit['clinicalSourceDiagnostics']:
        _write_private_diagnostic('clinical-source-validation', state.audit['clinicalSourceDiagnostics'])
    attach_clinical_problems(document, diseases)
    stage_timings['clinical_problem_organization'] = round(
        (time.monotonic() - phase) * 1000, 3)
    phase = time.monotonic()
    data = await _project(prepare_template_data, request, parsed, document, health_assessment)
    layout_started = time.monotonic()
    data = await arrange_template_rows(client, data, document, parsed.subject, deadline, state.audit)
    stage_timings['report_template_layout'] = round((time.monotonic() - layout_started) * 1000, 3)
    for group in getattr(data, '_template_layout', {}).get('groups', ()):
        group['originalFactIds'] = list(group.get('sourceFactIds', ()))
    risk_started = time.monotonic()
    data = await link_template_risk_context(client, data, document, parsed.subject, deadline, state.audit)
    stage_timings['report_template_risk_context'] = round((time.monotonic() - risk_started) * 1000, 3)
    score_input = data.model_copy(deep=True) if chapter_mode else None
    if score_input is None:
        data = await score_template_dimensions(client, data, document, parsed.subject, deadline, state.audit)
    layout_audit = state.audit.get('templateLayout', {})
    if chapter_mode:
        # Rows the organiser left ungrouped are still shown as their own rows;
        # self-reported history and symptoms can appear without a clinical group.
        has_clinical_finding = any(
            finding.certainty != 'self_reported' for finding in data.medical_assessment.findings
        )
        if state.audit.get('templateLayoutError') or (
            has_clinical_finding
            and layout_audit.get('sourceClinicalRowCount')
            and not layout_audit.get('acceptedGroups')
        ):
            state.incomplete.append('REPORT_LAYOUT_INCOMPLETE')
    elif layout_audit.get('unresolvedMemberCodes'):
        state.incomplete.append('REPORT_LAYOUT_INCOMPLETE')
    def build_source_checkpoint():
        state.blueprint = build_report_blueprint(request, parsed, document, health_assessment, prepared_data=data)
    await _project(build_source_checkpoint)
    if chapter_mode:
        from .evaluation_content_plan import plan_assessment_content

        async def plan_content():
            plan_started = time.monotonic()
            planned = await plan_assessment_content(
                client, state.blueprint, parsed.subject, deadline=deadline, audit=state.audit,
            )
            stage_timings['report_content_plan'] = round((time.monotonic() - plan_started) * 1000, 3)
            return planned

        async def score_dimensions():
            score_started = time.monotonic()
            try:
                await score_template_dimensions(client, score_input, document, parsed.subject, deadline, state.audit)
            except Exception as error:
                state.audit['templateDimensionScoringError'] = type(error).__name__
                return False
            finally:
                stage_timings['report_template_dimension_scoring'] = round((time.monotonic() - score_started) * 1000, 3)
            return True

        # The public six-chapter plan does not read scores. Explain still
        # consumes the approved score rows, so calculate them independently
        # on a copy and merge only those rows before rebuilding the blueprint.
        async with asyncio.TaskGroup() as preparation:
            planned = preparation.create_task(plan_content())
            scored = preparation.create_task(score_dimensions())
        content_plan = planned.result()
        if scored.result():
            data.health_overview.assessment_chart = score_input.health_overview.assessment_chart
            data.health_overview.overall_score.score = score_input.health_overview.overall_score.score
            data.disease_risk_assessment.overview_matrix = score_input.disease_risk_assessment.overview_matrix
            score_receipt = getattr(score_input, '_template_dimension_scores', None)
            if score_receipt is not None:
                object.__setattr__(data, '_template_dimension_scores', score_receipt)
        logger.info('report content plan: status=%s clusters=%s symptoms=%s reason=%s',
            content_plan['status'], len(content_plan['clusters']), len(content_plan['symptoms']),
            state.audit.get('contentPlan', {}).get('reason'))
        object.__setattr__(data, '_assessment_content_plan', content_plan)
        degraded_sections = content_plan_degraded_sections(content_plan)
        if degraded_sections:
            state.audit['contentPlanDegradedSections'] = degraded_sections
        if content_plan['status'] != 'reviewed' or degraded_sections:
            state.incomplete.append('REPORT_LAYOUT_INCOMPLETE')
        # Rebuild the actual writing jobs from the source-bound plan. The
        # initial blueprint was only the immutable source catalogue for planning.
        await _project(build_source_checkpoint)
    stage_timings['report_blueprint'] = round((time.monotonic() - phase) * 1000, 3)
    state.audit['sourceScalarSha256'] = _source_scalar_signature(state.blueprint.data)
    phase = time.monotonic()
    source_blocks = {
        chapter['code']: blocks
        for chapter in state.blueprint.chapters
        if (blocks := [deepcopy(target['sourceBlock']) for target in chapter['targets'] if target.get('sourceBlock')])
    }
    state.approved_blocks = deepcopy(source_blocks)
    state.audit['reviewedBlocks'] = deepcopy(source_blocks)
    state.audit['sourceGeneratedTargets'] = [block['targetId'] for blocks in source_blocks.values() for block in blocks]
    stage_timings['report_source_composition'] = round((time.monotonic() - phase) * 1000, 3)
    phase = time.monotonic()
    evidence_started = phase
    retrieval_deadline = min(deadline, phase + max(.01, (deadline - phase) * .35))
    from .artifacts import active_final_report_id

    def record_evidence():
        stage_timings['report_evidence'] = round((time.monotonic() - evidence_started) * 1000, 3)
        if progress is not None:
            progress.evidence_records = list(state.evidence.records)
            progress.references = list(state.evidence.references)
            progress.query_audits = list(state.evidence.query_audits)

    if chapter_mode:
        from .evaluation_evidence_recall import recall_report_evidence_sources

        async def recall():
            state.evidence = await recall_report_evidence_sources(pipeline.evidence_client,
                pipeline.public_evidence_client, getattr(pipeline, 'web_evidence_client', None), client,
                blueprint=state.blueprint, subject=parsed.subject, deadline=retrieval_deadline,
                audit=state.audit)
            record_evidence()

        await _author_chapters(state, client, parsed.subject, deadline, recall=recall)
    else:
        state.evidence = await retrieve_report_evidence(pipeline.evidence_client,
            pipeline.public_evidence_client, client, blueprint=state.blueprint,
            subject=parsed.subject, deadline=retrieval_deadline, audit=state.audit,
            research_agent=getattr(pipeline, 'research_agent', None),
            research_scope=(request.user_id, request.session_id, active_final_report_id()))
        record_evidence()
        phase = time.monotonic()
        def chapter_aggregate(target):
            path = target['path']
            return (path[0] == '_assessment_editorial'
                or (path[0] == 'medical_assessment' and path[1] == 'trigger_conclusion')
                or (path[0] == 'microbiome_assessment' and path[1] in {
                    'structure_interpretation', 'key_microbial_signals'})
                or (path[0] == 'disease_risk_assessment' and path[1] in {'categories', 'overview_matrix'})
                or (path[0] == 'nutrition_assessment' and path[1] == 'nutrition_interpretation')
                or (path[0] == 'functional_pathways' and path[1] == 'summary'))
        body_chapters = [chapter for chapter in state.blueprint.chapters
            if chapter['number'] in {2, 3, 4, 5, 6}]
        medical_chapters = [{**chapter, 'targets': [target for target in chapter['targets']
            if not chapter_aggregate(target)]} for chapter in body_chapters]
        medical_chapters = [chapter for chapter in medical_chapters if chapter['targets']]
        state.approved_blocks = await author_report_sections(client,
            blueprint=replace(state.blueprint, chapters=medical_chapters), evidence=state.evidence,
            subject=parsed.subject, deadline=deadline, audit=state.audit,
            previous_reviewed=state.approved_blocks)

        # One actual biomedical gap feedback point for the complete report. Source
        # distribution prose has already been composed from immutable typed data;
        # no missing-paper loop can erase or re-author its grades/dates/counts.
        gaps = list(state.audit.get('medicalUnresolved', []))
        if gaps and time.monotonic() < deadline:
            recall_started = time.monotonic()
            recall_deadline = min(deadline, recall_started + min(60.,
                max(.01, (deadline - recall_started) * .15)))
            changed = await recall_report_evidence(pipeline.evidence_client,
                pipeline.public_evidence_client, blueprint=state.blueprint,
                subject=parsed.subject, bundle=state.evidence, gaps=gaps,
                deadline=recall_deadline, audit=state.audit)
            stage_timings['report_review_gap_recall'] = round(
                (time.monotonic() - recall_started) * 1000, 3)
            if changed:
                changed_numbers = set(state.evidence.changed_source_numbers)
                fresh = {row['targetId'] for row in gaps}
                for blocks in state.approved_blocks.values():
                    fresh.update(block['targetId'] for block in blocks if any(
                        citation['number'] in changed_numbers for sentence in block['sentences']
                        for citation in sentence.get('evidence', [])))
                seeds = {block['targetId']: block for blocks in source_blocks.values() for block in blocks}
                reset = {}
                for code, blocks in state.approved_blocks.items():
                    reset[code] = [deepcopy(seeds[block['targetId']])
                        if block['targetId'] in fresh and block['targetId'] in seeds else block
                        for block in blocks if block['targetId'] not in fresh or block['targetId'] in seeds]
                state.approved_blocks = reset
                state.audit['reviewedBlocks'] = deepcopy(reset)
                refresh = [{**chapter, 'targets': [target for target in chapter['targets']
                    if target['id'] in fresh]} for chapter in medical_chapters]
                refresh = [chapter for chapter in refresh if chapter['targets']]
                state.approved_blocks = await author_report_sections(client,
                    blueprint=replace(state.blueprint, chapters=refresh), evidence=state.evidence,
                    subject=parsed.subject, deadline=deadline, audit=state.audit,
                    previous_reviewed=state.approved_blocks)

        # Group/chapter conclusions are composed after their detailed cells. A
        # parallel detail batch cannot also claim to have summarized a complete
        # reviewed chapter that has not yet been written.
        aggregate_chapters = [{**chapter, 'targets': [target for target in chapter['targets']
            if chapter_aggregate(target)]} for chapter in body_chapters]
        aggregate_chapters = [chapter for chapter in aggregate_chapters if chapter['targets']]
        state.approved_blocks = await author_report_sections(client,
            blueprint=replace(state.blueprint, chapters=aggregate_chapters), evidence=state.evidence,
            subject=parsed.subject, deadline=deadline, audit=state.audit,
            previous_reviewed=state.approved_blocks)

        # Overview and conclusion synthesize reviewed body chapters, rather than
        # being composed in parallel from raw observations and repeating them.
        final_chapters = [chapter for chapter in state.blueprint.chapters if chapter['number'] in {1, 7}]
        state.approved_blocks = await author_report_sections(client,
            blueprint=replace(state.blueprint, chapters=final_chapters), evidence=state.evidence,
            subject=parsed.subject, deadline=deadline, audit=state.audit,
            previous_reviewed=state.approved_blocks)

    stage_timings['report_chapter_authoring_review'] = round(
        (time.monotonic() - phase) * 1000, 3)
    phase = time.monotonic()
    accepted, rejected = await _project(_apply_evaluation_blocks, state, state.approved_blocks)
    if chapter_mode:
        from .evaluation_report_writer import mirror_duplicate_taxa
        state.audit['mirroredTaxonRows'] = mirror_duplicate_taxa(state.blueprint.data)
    state.accepted_blocks = accepted
    state.audit['acceptedTargets'] = [block['targetId'] for block in accepted]
    state.audit['publicationRejections'] = rejected
    if _record_content_gaps(state.blueprint, accepted, rejected, state.audit):
        state.incomplete.append('REPORT_CONTENT_INCOMPLETE')
    if not any(state.blueprint.facts):
        state.incomplete.append('ASSESSMENT_DATA_UNAVAILABLE')
    document.sections = _document_sections(state.blueprint, accepted)
    stage_timings['report_assembly'] = round((time.monotonic() - phase) * 1000, 3)
    if chapter_mode:
        cited_numbers = {number for block in accepted for number in block['evidenceRefs']}
        web_client = getattr(pipeline, 'web_evidence_client', None)
        metadata_started = time.monotonic()
        # Preserve time for the final projection and callback. Metadata
        # lookup enriches only already accepted citation identities.
        metadata_window = min(5.0, work_deadline - metadata_started - 4.0)
        if cited_numbers and web_client is not None and metadata_window > 0:
            from .evaluation_evidence_recall import enrich_report_reference_metadata

            try:
                async with asyncio.timeout(metadata_window):
                    state.audit['reportCitationMetadata'] = await enrich_report_reference_metadata(
                        state.evidence, web_client, cited_numbers, deadline=metadata_started + metadata_window,
                    )
            except TimeoutError:
                state.audit['reportCitationMetadata'] = {'deadlineExhausted': True}
            except Exception as error:
                state.audit['reportCitationMetadata'] = {'errorType': type(error).__name__}
            stage_timings['report_citation_metadata'] = round((time.monotonic() - metadata_started) * 1000, 3)
    return await _project(_payload, state)
