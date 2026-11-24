"""Project independently reviewed risks into the existing evaluate contract.

Source model rows stay in the category detail. The ranked view contains one
integrated decision per disease, using its own code and scale. No source
measurement, original advice, or callback field name is overwritten.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
import re

from . import evaluation_catalog as catalog
from . import evaluation_schema as public
from .evaluation_risk_assessment import approved_assessments, display_risk_assessments, risk_priority_key


@dataclass(frozen=True)
class _ProjectionProof:
    digest: str
    clinical_support: tuple = ()
    disease_only: bool = False


def _fingerprint(data):
    value = {
        'findings': [row.model_dump(mode='json', by_alias=True)
            for row in data.health_overview.key_findings],
        'priorities': [row.model_dump(mode='json', by_alias=True)
            for row in data.comprehensive_conclusion.priority_directions],
        'ranking': [row.model_dump(mode='json', by_alias=True)
            for row in data.disease_risk_assessment.risk_ranking],
        'nextSteps': [row.model_dump(mode='json', by_alias=True)
            for row in data.comprehensive_conclusion.next_steps],
        'medical': [row.model_dump(mode='json', by_alias=True)
            for row in data.medical_assessment.findings],
        'summary': data.health_overview.overall_summary.model_dump(mode='json', by_alias=True),
        'summaries': data.health_overview.summaries.model_dump(mode='json', by_alias=True),
        'conclusion': data.comprehensive_conclusion.overall_conclusion.model_dump(mode='json', by_alias=True),
        'categories': [{key: value for key, value in row.model_dump(mode='json', by_alias=True).items()
            if key != 'items'} for row in data.disease_risk_assessment.categories],
        'matrix': [row.model_dump(mode='json', by_alias=True)
            for row in data.disease_risk_assessment.overview_matrix],
        'dimensionChart': [row.model_dump(mode='json', by_alias=True)
            for row in data.health_overview.assessment_chart
            if row.code in dict(catalog.RISK_CATEGORY_DEFINITIONS)],
        'overallScore': data.health_overview.overall_score.model_dump(mode='json', by_alias=True),
    }
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        allow_nan=False).encode()).hexdigest()


def has_integrated_risk_projection(data):
    proof = getattr(data, '_integrated_risk_projection', None)
    return isinstance(proof, _ProjectionProof) and proof.digest == _fingerprint(data)


def integrated_clinical_support_ids(data):
    """Exact clinical remarks adopted through a live reviewed disease link."""
    if not has_integrated_risk_projection(data):
        return {}
    return {code: set(ids) for code, ids in data._integrated_risk_projection.clinical_support}


def has_integrated_disease_projection(data):
    return (has_integrated_risk_projection(data)
        and data._integrated_risk_projection.disease_only)


def _health_priority(risk, decisions):
    decision = decisions.get(risk.risk_id, {})
    score = decision.get('index')
    return ({'高风险': 0, '中风险': 1, '低风险': 2}.get(decision.get('level'), 3),
        score if isinstance(score, (int, float)) and not isinstance(score, bool) else 101,
        risk_priority_key(risk))


def _disease_centered_risks(document, risks, facts):
    """Keep a lab condition as evidence only when its whole premise is adopted.

    The target keeps its independently reviewed name, certainty and score.
    Partial links cannot erase the source condition or transfer other facts.
    """
    from .evaluation_risk_assessment import indicator_problem_outcome_links, _clinical_observed_abnormal
    by_id = {risk.risk_id: risk for risk in risks}
    covered, remarks = set(), {}
    for link in indicator_problem_outcome_links(document):
        source, target = by_id.get(link['problemRiskId']), by_id.get(link['outcomeRiskId'])
        if source is None or target is None:
            continue
        premise = set(link.get('sourcePositiveFactIds', ())) or {
            fid for fid in source.support_fact_ids if fid in facts
            and _clinical_observed_abnormal(facts[fid])}
        shared = set(link['sharedFactIds'])
        if not premise or not premise <= shared:
            continue
        covered.add(source.risk_id)
        remarks.setdefault(target.risk_id, set()).update(shared)
    return [risk for risk in risks if risk.risk_id not in covered], remarks


def _sentences(values):
    return ''.join(dict.fromkeys(text.rstrip('。 ') + '。'
        for value in values if isinstance(value, str) and (text := value.strip())))


def _actions(actions):
    return _sentences(action.text for action in actions)


def _risk_code(risk):
    return 'integratedRisk:' + risk.category + ':' + risk.disease_id


def _public_risk_name(risk):
    """Use the disease name; separate certainty/grade still own its meaning."""
    # A risk-table label is not a diagnosis. Keep the approved internal risk
    # and certainty unchanged; do not append a repeated qualifier to every
    # disease in all three public views.
    return re.sub(r'(?:相关风险)+$', '', str(risk.name or '').strip()).strip()


def _editorial_risks(risks, facts, *, document=None):
    """One reviewed priority order, with approved same-theme representatives."""
    from .evaluation_risk_assessment import editorial_risk_representatives
    if document is not None:
        risks = editorial_risk_representatives(document, risks)
    unique = {}
    for risk in sorted(risks, key=risk_priority_key):
        # A duplicated display label never becomes a second clinical issue.
        # Different diseases, source rows and original model grades stay intact.
        unique.setdefault((risk.category, risk.disease_id), risk)
    selected = list(unique.values())
    # A generic multimorbidity association is contextual support when its
    # entire positive premise is already represented by concrete problems.
    # Keep the original assessment/ranking and source signal for traceability.
    concrete = {fid for risk in selected if not re.search(r'多病共存|multimorbid', risk.name, re.I)
        for fid in risk.support_fact_ids}
    return [risk for risk in selected if not (re.search(r'多病共存|multimorbid', risk.name, re.I)
        and risk.support_fact_ids and set(risk.support_fact_ids) <= concrete)]


def _is_main_health_risk(risk, decisions):
    """A missing numeric review cannot erase a separately approved disease.

    The old assessment identifies a retained concern only. It never supplies
    a new health score or public grade. A completed low-risk health decision
    stays in the detailed disease table rather than the main shortlist.
    """
    if risk.certainty in {'confirmed', 'current_problem'}:
        return True
    decision = decisions.get(risk.risk_id)
    if decision and decision.get('level') in {'低风险', '中风险', '高风险'}:
        return decision['level'] in {'中风险', '高风险'}
    return risk.level not in {None, '', '正常', '低风险'}




def _claim_citations(document, risk, identifiers):
    codes = {'integratedRisk:'+risk.risk_id+':'+identifier
        for identifier in identifiers}
    return sorted({number for block in document.sections if block.code in codes for number in block.evidence_refs})


def _action_citations(document, risk, actions):
    return _claim_citations(document, risk, [identifier for action in actions for identifier in action.claim_ids])


def _source_refs(data, facts, ids):
    result = []
    for fid in dict.fromkeys(ids):
        original = [source for row in data.appendices.source_items
            for source in row.sources if source.record_id == fid]
        if not original and fid in facts:
            fact = facts[fid]
            original = [public.SourceReference(record_id=fid, observed_at=fact.date,
                source_code='A' if fact.kind == 'clinical' else 'B'
                    if fact.kind == 'self_reported' or fid.startswith('profile:') else 'C')]
        for source in original:
            if source not in result:
                result.append(source.model_copy(deep=True))
    return result


def _basis(facts, risk, *, include_reference=True):
    values = []
    for fid in risk.support_fact_ids:
        fact = facts.get(fid)
        if fact is None:
            continue
        name = fact.name.removesuffix('（来源疾病模型指数）')
        value = fact.value
        if fact.name in {'肠道微生态总评分', '肠道菌群健康度评分'}:
            value = str(public._source_total_score_value(value))
        if fact.kind == 'self_reported':
            quotes = list(dict.fromkeys(claim.fact_quotes[fid]
                for claim in getattr(risk, 'claims', ())
                if claim.role != 'management' and fid in claim.fact_quotes))
            if quotes:
                # Two independently approved clauses may quote a long and
                # short span of the same answer. Retain the complete span
                # once; different symptoms and different facts stay intact.
                comparable = lambda quote: re.sub(r'[\s。；;]+', '', quote)
                quotes = [quote for quote in quotes if not any(
                    comparable(quote) != comparable(other)
                    and comparable(quote) in comparable(other) for other in quotes)]
                # One answer may contain multiple symptoms, negatives and
                # goals. Only the exact reviewed support span belongs under
                # this disease; the complete answer remains in section 3.2.
                def source_time_span(quote):
                    quote=re.sub(r'^\s*回答[：:]\s*', '', quote)
                    # Restore only a literally adjacent, already supplied
                    # observation duration; never infer or transplant time.
                    if fact.date and fact.date+quote in fact.value:
                        return fact.date+quote
                    return quote
                value = '；'.join(source_time_span(quote) for quote in quotes)
            value = re.sub(r'^\s*回答[：:]\s*', '', value)
            value = re.sub(r'[（(]用户自述(?:，未核验)?[）)]', '', value).strip('；; ')
        if fact.unit and fact.unit not in value:
            value += ' ' + fact.unit
        if include_reference and fact.reference_range:
            value += '（参考' + fact.reference_range + '）'
        if fact.status and fact.status not in value and fact.kind != 'self_reported':
            value += '，' + fact.status
        from .evaluation_risk_assessment import _fact_role
        if _fact_role(fact) == 'microbiome_association_screen':
            name += '相关菌群信号'
        # Questionnaire wording is an input locator, not a second patient
        # result. Preserve the full adopted answer without repeating its
        # question or an organizer's verification status.
        values.append(value if fact.kind == 'self_reported' else name + '：' + value)
    return '；'.join(dict.fromkeys(values)) or None


def _medical_refs(row):
    return {source.record_id for source in row.sources if source.record_id}


def _unclassified_clinical_measurement(row, facts):
    """Retain an exact live A measurement without making it a condition.

    Neither an unclassified DTO nor a shared source locator proves a result.
    The complete value/metadata/date must still match its current source fact.
    C models and association screens remain in their original category rows.
    """
    if row.certainty is not None or row.value is None or not row.sources \
            or any(source.source_code != 'A' for source in row.sources):
        return False
    value = json.dumps(row.value, ensure_ascii=False) if isinstance(row.value, bool) else str(row.value)
    for source in row.sources:
        fact = facts.get(source.record_id)
        if (fact is not None and fact.kind == 'clinical'
                and (row.name, value, row.unit, row.reference_range, row.source_status)
                    == (fact.name, fact.value, fact.unit, fact.reference_range, fact.status)
                and source.observed_at == fact.date
                and all(ref.observed_at in {None, fact.date} for ref in row.sources)):
            return True
    return False


def _literal_clinical_measurement(row):
    """Keep the source row; reviewed prose returns only via owner receipts."""
    return row.model_copy(deep=True, update={'assessment_level': None,
        'explanation': None, 'source_advice': None, 'evidence_refs': [],
        'availability': 'partial'})


def _clinical_specialties(actions):
    """Reuse specialties literally named in the reviewed referral action."""
    names = []
    for action in actions:
        for match in re.finditer(r'(?:至|到|在)([\u4e00-\u9fff或、/／]{2,30}?)就诊', action.text):
            for name in re.split(r'或|及|、|/|／', match[1]):
                if 2 <= len(name) <= 12 and name.endswith(('科', '门诊')) and name not in names:
                    names.append(name)
    return '／'.join(names) or None


def _atomic_clinical_fact(fact):
    from .evaluation_builder import _clinical_number
    return fact.kind == 'clinical' and not fact.imaging and (
        _clinical_number(fact.value, fact.unit) is not None
        or bool(re.fullmatch(r'\s*[<>≤≥]?[+-]?(?:\d+(?:\.\d*)?|\.\d+)'
            r'(?:\s*/\s*\d+(?:\.\d*)?)?\s*(?:[A-Za-zμµ%/⋅·^0-9⁹²³*.-]{1,24})?\s*', fact.value))
        or bool(re.fullmatch(r'\s*(?:阳性|阴性|未检出|检出|正常|异常|[+＋\-−]{1,4}|\d[+＋])\s*', fact.value)))


def _related_taxa(data, risk, *, source_links=None):
    """Carry only the same disease's literal source panel members.

    A screened-condition fact may be the adopted risk premise while its
    organisms stay background. Their exact source panel association is still
    report data, not a newly inferred scientific relationship or diagnosis.
    """
    from .evaluation_risk_assessment import _risk_identity
    ids = set(risk.support_fact_ids)
    original = [public.TaxonSignal(name=row.name, scientific_name=row.scientific_name,
        value=row.value, unit=row.unit, source_status=row.source_status,
        sources=[source.model_copy(deep=True) for source in row.sources])
        for row in data.microbiome_assessment.taxa if ids & _medical_refs(row)]
    links = source_links or {}
    owners = ids | set(risk.source_model_fact_ids)
    linked = []
    for fid in owners:
        for edge in links.get(fid, []):
            if (edge.get('relationRole') != 'source_association_member'
                    or _risk_identity(edge.get('panelName', '')) != risk.disease_id
                    or not set(edge.get('ownerFactIds', [])) & owners):
                continue
            sources = [public.SourceReference(source_code='C', record_id=value,
                observed_at=edge.get('observedAt')) for value in dict.fromkeys([
                    *edge.get('memberSourceRefs', []), *edge.get('memberFactIds', [])])]
            linked.append(public.TaxonSignal(name=edge['name'], scientific_name=edge.get('scientificName'),
                value=edge.get('value'), unit=edge.get('unit'), source_status=edge.get('sourceStatus'),
                sources=sources))
    result = []
    for row in [*linked, *original]:
        identity = (row.scientific_name or row.name, str(row.value), row.unit, row.source_status)
        previous = next((old for old in result if
            (old.scientific_name or old.name, str(old.value), old.unit, old.source_status) == identity
            and _medical_refs(old) & _medical_refs(row)), None)
        if previous is None:
            result.append(row)
        else:
            previous.sources.extend(source for source in row.sources if source not in previous.sources)
    return result


def _paired_bmi_indicator(data, facts):
    """Calculate a measurement only from an unambiguous same-report pair.

    The existing unit normalizer is reused. This contributes no diagnosis,
    reference limit, grade or risk score; both actual source measurements
    remain its provenance. Conflicting or comparative values are not paired.
    """
    from .health_assessment.normalization import normalize_unit, date_rank
    groups = {}
    for fact in facts.values():
        if fact.kind not in {'clinical', 'self_reported'}:
            continue
        code = ('height' if re.fullmatch(r'身高|height', fact.name, re.I) else
            'weight' if re.fullmatch(r'体重|体质量|weight', fact.name, re.I) else None)
        if code is None or not fact.source:
            continue
        match = re.fullmatch(r'\s*([-+]?\d+(?:\.\d+)?)\s*(cm|m|kg|lbs?|厘米|米|千克|公斤|磅)?\s*',
            str(fact.value), re.I)
        if not match:
            continue
        inline_unit = match[2]
        if not fact.unit and not inline_unit:
            continue
        number, unit = normalize_unit(code, float(match[1]), fact.unit or inline_unit)
        if inline_unit and fact.unit and normalize_unit(code, float(match[1]), inline_unit) != (number, unit):
            continue
        if not math.isfinite(number) or number <= 0 or unit != ('cm' if code == 'height' else 'kg'):
            continue
        group = groups.setdefault((fact.kind, fact.source, fact.date), {'height': {}, 'weight': {}})
        group[code].setdefault(number, []).append(fact.fact_id)
    pairs = [(key, value) for key, value in groups.items()
        if len(value['height']) == len(value['weight']) == 1]
    if not pairs:
        return None
    newest = max(date_rank(key[2]) for key, _ in pairs)
    pairs = [(key, value) for key, value in pairs if date_rank(key[2]) == newest]
    values = {next(iter(value['weight'])) / (next(iter(value['height'])) / 100) ** 2
        for _, value in pairs}
    if len(values) != 1:
        return None
    value = float(Decimal(str(values.pop())).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP))
    ids = [fid for _, pair in pairs for part in pair.values() for records in part.values() for fid in records]
    return public.MetricAssessment(code='derived:bmi', name='BMI', value=value, unit='kg/m²',
        sources=_source_refs(data, facts, ids), availability='available')


def _related_indicators(data, facts, risk, *, source_links=None, clinical_support=()):
    """Expose this assessed condition's actual A/B/C supporting evidence.

    Contrary/background observations do not acquire a positive role merely
    by sharing an organ or a value. A questionnaire excerpt stays within the
    independently adopted clause. Original model indices are kept in their
    source rows rather than repeated as circular disease evidence.
    """
    from .evaluation_risk_assessment import _fact_role, _risk_identity
    rows, seen = [], {}
    positive = list(dict.fromkeys([*risk.support_fact_ids, *clinical_support,
        *(fid for claim in risk.claims if claim.role != 'management' for fid in claim.fact_quotes)]))
    for fid in positive:
        fact = facts.get(fid)
        if (fact is None or fid in risk.opposing_fact_ids or fact.kind not in {'clinical', 'self_reported', 'function'}
                or _fact_role(fact) in {
                'source_disease_model', 'microbiome_association_screen'} or fact.kind == 'microbe'):
            continue
        value = fact.value
        if fact.kind == 'self_reported':
            quotes = list(dict.fromkeys(claim.fact_quotes[fid] for claim in risk.claims
                if claim.role != 'management' and fid in claim.fact_quotes))
            quotes = [quote for quote in quotes if not any(quote != other and quote in other for other in quotes)]
            value = '；'.join(quotes) if quotes else value
            value = re.sub(r'^\s*回答[：:]\s*', '', value)
        signature = (fact.kind, fact.name, str(value), fact.unit, fact.reference_range, fact.status, fact.date)
        sources = _source_refs(data, facts, [fid])
        if signature in seen:
            previous = seen[signature]
            previous.sources.extend(source for source in sources if source not in previous.sources)
            continue
        row = public.MetricAssessment(code=fid, name=fact.name, value=value,
            unit=fact.unit, reference_range=fact.reference_range, source_status=fact.status,
            sources=sources, availability='available')
        rows.append(row)
        seen[signature] = row
    if _risk_identity(risk.name) in {'obesity', 'centralObesity'} and not any(
            re.fullmatch(r'BMI|体质指数|体重指数', str(row.name), re.I) for row in rows):
        bmi = _paired_bmi_indicator(data, facts)
        if bmi is not None:
            rows.insert(0, bmi)
    for taxon in _related_taxa(data, risk, source_links=source_links):
        if not taxon.name and not taxon.scientific_name:
            continue
        row = public.MetricAssessment(name=taxon.name or taxon.scientific_name,
            source_status=taxon.source_status,
            sources=[source.model_copy(deep=True) for source in taxon.sources], availability='available')
        signature = ('microbe', row.name, row.source_status)
        if signature not in seen:
            rows.append(row)
            seen[signature] = row
    order = lambda row: (0 if any(source.source_code == 'A' for source in row.sources)
        else 1 if any(source.source_code == 'B' for source in row.sources) else 2)
    return sorted(rows, key=order)


def _coalesce_risk_source_rows(rows):
    """Join two projections of one original model cell, never two results."""
    result = []
    for row in rows:
        own = {(source.source_code, source.record_id, source.observed_at) for source in row.sources}
        key = (row.code, row.name, row.value, row.unit, row.reference_range,
            row.source_status, row.assessment_level)
        previous = next((old for old in result
            if str(row.code or '').startswith('disease-') and own
            and key == (old.code, old.name, old.value, old.unit, old.reference_range,
                old.source_status, old.assessment_level)
            and own & {(source.source_code, source.record_id, source.observed_at) for source in old.sources}
            and len({source.observed_at for source in [*row.sources, *old.sources]
                if source.observed_at}) <= 1), None)
        if previous is None:
            result.append(row)
            continue
        for source in row.sources:
            if source not in previous.sources:
                previous.sources.append(source.model_copy(deep=True))
        for taxon in row.related_taxa:
            if taxon not in previous.related_taxa:
                previous.related_taxa.append(taxon.model_copy(deep=True))
        if row.explanation and (not previous.explanation or len(row.evidence_refs) > len(previous.evidence_refs)):
            previous.explanation = row.explanation
        previous.evidence_refs = sorted(set([*previous.evidence_refs, *row.evidence_refs]))
    return result


def _unmeasured_clinical_compatibility(row, assessed, source_rows=()):
    """A legacy computed clinical grade is not an original detection result.

    Preserve every actual value, source grade and C observation. Only the
    unmeasured compatibility mirror is removed; the independently reviewed
    clinical condition and its actual findings remain in the medical/risk
    projections.
    """
    if row.value is not None or row.source_status is not None:
        return False
    codes = {source.source_code for source in row.sources}
    if 'C' in codes:
        # A clinical compatibility row may have inherited a C screen's
        # references and taxa without acquiring that screen's actual result.
        # Remove that empty mirror only when the same original C result is
        # separately retained. A grade-only native observation stays intact.
        from .evaluation_disease_identity import same_source_disease
        native_ids = {source.record_id for source in row.sources
            if source.source_code == 'C' and source.record_id}
        return bool(codes & {'A', 'B'}) and any(
            other is not row
            and (other.value is not None or other.source_status is not None)
            and (row.code == other.code or same_source_disease(row.name, other.name))
            and native_ids & {source.record_id for source in other.sources
                if source.source_code == 'C' and source.record_id}
            for other in source_rows)
    if codes & {'A', 'B'}:
        return True
    from .evaluation_disease_identity import same_source_disease
    return any(row.code == risk.disease_id or same_source_disease(row.name, risk.name)
        for risk in assessed)


def _same_reported_answer(left, right):
    return (left.kind == right.kind == 'self_reported'
        and bool(left.source) and left.source == right.source
        and left.value == right.value and left.date == right.date)


def _reported_finding_is_covered(row, published, facts):
    """Remove only a fully represented facet of the same original answer."""
    if row.certainty != 'self_reported':
        return False
    ids = _medical_refs(row) & facts.keys()
    from .evaluation_disease_identity import same_source_disease
    def symptom_name(value):
        text = re.sub(r'[（(]用户自述(?:，未核验)?[）)]', '', str(value or '')).strip()
        return re.sub(r'^(?:明显|反复|偶发|持续|轻度|重度|严重)', '', text).removesuffix('症状').strip()
    def same_problem(target):
        return (same_source_disease(row.name, target.name)
            or str(row.code or '').startswith('reportedSymptom:')
            and symptom_name(target.name) in {symptom_name(name)
                for name in str(row.name or '').split('/')} - {''})
    own = [target for target in published
        if target is not row and same_problem(target)
        and target.certainty in {'self_reported', 'current_problem', 'confirmed', 'source_finding'}
        and (ids & _medical_refs(target) or any(
            _same_reported_answer(facts[fid], facts[adopted]) for fid in ids
            for adopted in _medical_refs(target) if adopted in facts))]
    def text(value):
        value = re.sub(r'^\s*回答[：:]\s*', '', str(value or ''))
        value = re.sub(r'[（(]用户自述(?:，未核验)?[）)]', '', value)
        return re.sub(r'[\s，,；;。.:：、（）()]+', '', value)
    clauses = [text(value) for value in re.split(r'[；;\n]', str(row.value or '')) if text(value)]
    return bool(own and clauses and all(any(clause in text(target.value)
        for target in own) for clause in clauses))


def _compact_published_reported_findings(data, facts):
    """Use final remarks after the source guard restores exact qualifiers.

    This can only remove a duplicate already fully present in a published
    current problem. It does not restore any rejected prose or alter facts.
    """
    # Only the already-delivered rows grant formatting coverage. A serialized
    # registry cannot create a new decision or turn a future risk into a
    # current problem here.
    published = [row for row in data.medical_assessment.findings
        if str(row.code or '').startswith(('integratedRisk:', 'clinicalProblem:'))]
    data.medical_assessment.findings = [row for row in data.medical_assessment.findings
        if str(row.code or '').startswith(('integratedRisk:', 'clinicalProblem:'))
        or not _reported_finding_is_covered(row, published, facts)]


def _current_source_owner_code(data, document, risk, facts, *, selected_only=False):
    """Locate one verified current source owner, never by a shared image alone."""
    if risk.certainty not in {'confirmed', 'current_problem', 'self_reported'}:
        return None
    from .evaluation_clinical_focus import selected_source_clinical_focus
    from .evaluation_disease_identity import same_source_disease
    from .evaluation_schema import clinical_problem_projection_codes
    codes = ({entry['code'] for entry in selected_source_clinical_focus(data, document)}
        if selected_only else set(clinical_problem_projection_codes(data).values()))
    supported = {fid for fid in risk.support_fact_ids if fid in facts
        and facts[fid].kind in {'clinical', 'self_reported'}}
    matched = [row.code for row in data.medical_assessment.findings if row.code in codes
        and same_source_disease(row.name, _public_risk_name(risk))
        and _medical_refs(row) & supported]
    return matched[0] if len(matched) == 1 else None


def _medical_projection(data, document, risks, facts, *, assessed=None, clinical_support=None,
                        health_scores=None):
    from .evaluation_narrative import capture_reviewed_medical_text, restore_reviewed_medical_text
    medical_text = capture_reviewed_medical_text(data, document)
    from .evaluation_risk_assessment import editorial_risk_representatives
    risks=editorial_risk_representatives(document,risks,current_only=True)
    existing = [row for row in data.medical_assessment.findings
        if not str(row.code or '').startswith('integratedRisk:')]
    from .evaluation_clinical_focus import selected_source_clinical_focus
    selected_codes = {row['code'] for row in selected_source_clinical_focus(data, document)}
    clinical_support = clinical_support or {}
    health_scores = health_scores or {}
    result = []
    from .evaluation_fixed_assessment import medical_finding_specialty
    for risk in risks:
        if _current_source_owner_code(data, document, risk, facts):
            # Clinical observations keep their own complete source witnesses.
            # The scored assessment remains in its disease view; it is not a
            # second medical row or authority to overwrite the source owner.
            continue
        decision = health_scores.get(risk.risk_id)
        support_ids = list(dict.fromkeys([*risk.support_fact_ids, *clinical_support.get(risk.risk_id, ())]))
        sources = _source_refs(data, facts, support_ids)
        # An output slot may contain a generated action.  It is never an
        # original-source witness, even when it shares a supporting fact.
        # Exact reviewed actions are restored separately below.
        original_advice = []
        ranges = list(dict.fromkeys(facts[fid].name + '：' + facts[fid].reference_range
        for fid in support_ids if fid in facts and facts[fid].kind == 'clinical'
            and facts[fid].reference_range))
        clinical = _reviewed_summary(data, document, facts, 'clinical:' + risk.risk_id)
        interpretation = clinical.text if clinical is not None else None
        citations = clinical.evidence_refs if clinical is not None else []
        basis_risk = risk.model_copy(update={'support_fact_ids': support_ids})
        row = public.MedicalFinding(code=_risk_code(risk), name=_public_risk_name(risk),
            value=_basis(facts, basis_risk, include_reference=False), reference_range='；'.join(ranges) or None,
            assessment_level=decision['level'] if decision else None,
            certainty=risk.certainty, explanation=interpretation or None,
            source_advice='；'.join(original_advice) or None,
            sources=sources, evidence_refs=citations,
            availability=('available' if interpretation and clinical.availability == 'available'
                and decision and decision.get('availability') == 'available' else 'partial'))
        row.system = _clinical_specialties(risk.clinical_actions) or medical_finding_specialty({'name': risk.name})
        if row.system == '综合内科':
            row.system = {'intestinal': '消化科', 'metabolic': '内分泌科',
                'autoimmune': '风湿免疫科', 'neurological': '神经内科',
                'mental': '精神心理科', 'aging': '老年医学科', 'tumor': '肿瘤科'}.get(risk.category, row.system)
        result.append(row)
    published = list(result)
    # Shared broad imaging facts may describe additional problems: keep those
    # original problem rows. Only fully represented scalar/report rows become
    # remarks under the integrated problem instead of duplicate main risks.
    for row in existing:
        if _unclassified_clinical_measurement(row, facts):
            result.append(_literal_clinical_measurement(row))
            continue
        if row.certainty not in {'confirmed', 'suspected', 'source_finding',
                'self_reported', 'current_problem'}:
            continue
        # One measurement also carries request/PDF/index locators. Those
        # aliases are not additional patient observations that prevent the
        # exact adopted fact from becoming a remark under its reviewed risk.
        # Source finding rows may be compacted only when the final problem
        # projection proves their exact quoted values are already visible.
        # Sharing a fact with an adopted disease is insufficient.
        if row.certainty in {'model_risk', 'source_association'}:
            # Chapter 5 retains every original signal/grade. Neither an
            # unreviewed source association nor a source signal reviewed as
            # low concern can become a second personal decision in the main
            # problem list through a legacy default grade.
            continue
        if row.code not in selected_codes and _reported_finding_is_covered(row, published, facts):
            continue
        result.append(row)
    data.medical_assessment.findings = result
    restore_reviewed_medical_text(data, document, medical_text)




def _dimension_projection(data, document, facts, assessed):
    """Both charts consume a single live, independently reviewed snapshot."""
    from .evaluation_narrative import capture_reviewed_projection_text, restore_reviewed_projection_text
    reviewed_text = capture_reviewed_projection_text(data, document)
    from .evaluation_dimension_scores import approved_dimension_scores
    decisions = approved_dimension_scores(document)
    matrices = {row.code: row for row in data.disease_risk_assessment.overview_matrix}
    categories = {row.code: row for row in data.disease_risk_assessment.categories}
    chart = []
    for code, name in catalog.RISK_CATEGORY_DEFINITIONS:
        decision = decisions.get(code)
        own = [risk for risk in assessed if risk.category == code]
        sources = _source_refs(data, facts, [fid for risk in own for fid in risk.support_fact_ids])
        numbers = sorted({number for risk in own for number in _claim_citations(
            document, risk, [claim.claim_id for claim in risk.claims if claim.role != 'management'])})
        level = decision['level'] if decision else None
        score = decision['index'] if decision else None
        availability = decision.get('availability', 'available') if decision else 'unavailable'
        chart.append(public.AssessmentScore(code=code, name=name, score=score,
            assessment_level=level, sources=sources if decision else [],
            evidence_refs=numbers if decision else [], availability=availability))
        if code not in matrices:
            matrix = public.RiskMatrixRow(code=code, name=name)
            data.disease_risk_assessment.overview_matrix.append(matrix)
            matrices[code] = matrix
        matrix = matrices[code]
        matrix.score, matrix.attention_level, matrix.availability = score, level, availability
        if decision:
            matrix.sources, matrix.evidence_refs, matrix.availability = sources, numbers, availability
        else:
            matrix.basis, matrix.sources, matrix.evidence_refs = None, [], []
        if code in categories:
            categories[code].attention_level = level
    data.health_overview.assessment_chart = chart
    from .evaluation_dimension_scores import build_overall_dimension_score
    data.health_overview.overall_score = build_overall_dimension_score(document, chart)
    restore_reviewed_projection_text(data, document, reviewed_text)


def _reviewed_summary(data, document, facts, slot):
    from .evaluation_risk_assessment import approved_risk_summaries, risk_summary_reference_numbers
    summary = approved_risk_summaries(document).get(slot)
    if summary is None:
        return None
    from .evaluation_narrative import _body_boundary_sentence
    if any(_body_boundary_sentence(sentence) for sentence in re.split(r'[。！？\n]', summary.text)):
        return None
    risks = {risk.risk_id: risk for risk in approved_assessments(document)}
    ids = [fid for rid in summary.risk_ids if rid in risks
        for fid in risks[rid].support_fact_ids]
    return public.TextAssessment(text=summary.text,
        sources=_source_refs(data, facts, ids),
        evidence_refs=risk_summary_reference_numbers(document, slot),
        availability='available')






def _summarize(data, document, risks, facts):
    # Overview, domain interpretation and conclusion serve different reader
    # questions. Only independently reviewed editorial summaries may replace
    # them; concatenating the same risk conclusion across chapters does not.
    from .evaluation_narrative import reviewed_evaluation_text
    for slot, owner, attr, path in (
        ('overview', data.health_overview, 'overall_summary', ('healthOverview', 'overallSummary', 'text')),
        ('medical', data.health_overview.summaries, 'medical', ('healthOverview', 'summaries', 'medical', 'text')),
        ('microbiome', data.health_overview.summaries, 'microbiome', ('healthOverview', 'summaries', 'microbiome', 'text')),
        ('nutrition', data.health_overview.summaries, 'nutrition', ('healthOverview', 'summaries', 'nutrition', 'text')),
        ('conclusion', data.comprehensive_conclusion, 'overall_conclusion', ('comprehensiveConclusion', 'overallConclusion', 'text')),
        ('pathways', data.functional_pathways, 'summary', ('functionalPathways', 'summary', 'text')),
    ):
        # A later projection must not replace a complete, independently
        # reviewed chapter paragraph with a source-only priority sentence.
        # The live block, its exact path and current source scope grant this
        # permission; a persisted/public DTO never does.
        paragraph = reviewed_evaluation_text(data, document, path)
        if paragraph is not None:
            setattr(owner, attr, public.TextAssessment.model_validate(paragraph))
            continue
        summary = _reviewed_summary(data, document, facts, slot)
        if summary is not None:
            setattr(owner, attr, summary)
        else:
            # Interpretation is authored from adopted evidence. A list of
            # metrics or microbes cannot replace a failed model conclusion.
            setattr(owner, attr, public.TextAssessment(availability='partial'))
    data.health_overview.availability = ('available'
        if data.health_overview.overall_summary.text and all(getattr(
            data.health_overview.summaries, key).text for key in ('medical', 'microbiome', 'nutrition'))
        else 'partial')


def _safe_risk_prose(text, facts, risk):
    """Apply the same final source checks before selecting a reviewed excerpt."""
    from .evaluation_narrative import (_patient_goal_assertion_failures,
        _microbial_absence_upgrade_failures, _model_absence_upgrade_failures,
        _attention_severity_upgrade_failures, _body_boundary_sentence,
        _contains_authoring_instruction)
    own = [facts[fid] for fid in risk.support_fact_ids if fid in facts]
    return bool(text and not (_patient_goal_assertion_failures(text, own)
        or _microbial_absence_upgrade_failures(text, own)
        or _model_absence_upgrade_failures(text, own)
        or _attention_severity_upgrade_failures(text, own)
        or _body_boundary_sentence(text) or _contains_authoring_instruction(text)))


def _short_risk_parts(data, document, facts, risk, kind):
    summary = _reviewed_summary(data, document, facts, kind + ':' + risk.risk_id)
    if summary is not None and _safe_risk_prose(summary.text, facts, risk):
        return summary.text, summary.evidence_refs
    return None, []


def _detailed_risk_parts(data, document, facts, risk):
    """A detailed disease interpretation has its own reviewed writing duty.

    A short homepage prompt or a repeated measurement is not a completed
    explanation.  On author failure, retain only actual reviewed clinical
    meaning and actions and keep the partial state visible internally.
    """
    summary = _reviewed_summary(data, document, facts, 'detail:' + risk.risk_id)
    if summary is not None and _safe_risk_prose(summary.text, facts, risk):
        return summary.text, summary.evidence_refs, 'available'
    return None, [], 'partial'


def _priority_followup(data, document, facts, risk, *, patient_actions=None):
    if patient_actions is not None:
        group = next((group for group in patient_actions if any(
            member.risk_id == risk.risk_id for member, _ in group['members'])), None)
        if group is not None:
            own = next((action for action in risk.lifestyle_actions
                if action.text == group['action'].text), None)
            # The single patient-level list may name several related goals.
            # A risk-specific path still needs its own action permission;
            # another risk's reviewed sentence cannot grant that authority.
            if own is not None:
                return _actions([own]), _action_citations(document, risk, [own])
        return None, []
    actions = risk.lifestyle_actions[:1]
    if actions:
        return _actions(actions), _action_citations(document, risk, actions)
    # Clinical investigations remain in the internal reviewed registry and
    # original source advice. They cannot fill a lifestyle-improvement duty.
    return None, []


def _short_risk_text(data, document, facts, risk, kind):
    return _short_risk_parts(data, document, facts, risk, kind)[0]


def _short_risk_citations(data, document, facts, risk, kind):
    return _short_risk_parts(data, document, facts, risk, kind)[1]




def _walking_schedule_scope(action, facts):
    """Only a shared, measured walking habit identifies the same daily task."""
    text = action.text
    if (not re.search(r'快走', text) or not re.search(r'每周.{0,45}(?:天|次)', text)
            or not re.search(r'每(?:天|次).{0,12}分钟', text)
            or re.search(r'饮水|蔬菜|水果|全谷|饮食|睡眠|抗阻|力量训练|骑车|游泳', text)):
        return frozenset()
    return frozenset(fid for fid in action.fact_ids if fid in facts
        and facts[fid].kind == 'self_reported'
        and re.search(r'快走', facts[fid].name + ' ' + str(facts[fid].value)))


def _patient_lifestyle_actions(data, facts, risks, *, approved_risk_ids=frozenset()):
    """One existing reviewed instruction per shared patient walking task.

    This is publication consolidation, not a new intervention calculation.
    Different foods, activities and exposure scopes keep separate actions.
    The selected instruction is copied whole from one approved risk, with
    its own claim citations; no frequency or duration is constructed.
    """
    clinical_text = ''
    if data is not None:
        medical = getattr(data, 'medical_assessment', None)
        categories = getattr(getattr(data, 'disease_risk_assessment', None), 'categories', [])
        clinical_text = '\n'.join([*(row.explanation or '' for row in getattr(medical, 'findings', [])),
            *(category.attention_basis or '' for category in categories),
            *(category.clinical_correlation.text or '' for category in categories)])
    groups = []
    for risk in risks:
        for action in risk.lifestyle_actions:
            scope = (_walking_schedule_scope(action, facts)
                if getattr(risk,'risk_id',None) in approved_risk_ids else frozenset())
            literal = re.sub(r'[\s。；;]+', '', action.text)
            group = next((group for group in groups if group['literal'] == literal
                or scope and group['walkingScope'] & scope), None)
            if group is None:
                groups.append({'owner':risk, 'action':action, 'members':[(risk,action)],
                    'literal':literal, 'walkingScope':scope})
                continue
            group['members'].append((risk, action))
            group['walkingScope'] = group['walkingScope'] | scope
            # Existing clinical prose can prefer one of two already approved
            # complete instructions. Its presence grants no new permission;
            # both choices must already belong to the approved risk snapshot.
            if clinical_text.count(action.text) > clinical_text.count(group['action'].text):
                group['owner'], group['action'], group['literal'] = risk, action, literal
    return groups


def _lifestyle_plan_sources(document):
    """Keep approved daily actions independently of the disease shortlist.

    An actual symptom or lab finding can justify a reviewed daily action
    without becoming a disease. Neither a low health-risk grade nor this
    separate observation identity invalidates its original planning proof.
    """
    from .evaluation_risk_assessment import is_publishable_disease_assessment
    return [risk for risk in approved_assessments(document) if risk.lifestyle_actions
        and (is_publishable_disease_assessment(risk)
            or risk.entity_kind in {'symptom', 'indicator_problem'})]


def _next_step_projection(data, document, facts, risks, *, patient_actions=None):
    """Publish reviewed lifestyle plans; clinical actions are not substitutes."""
    result, existing = [], {}
    groups = patient_actions if patient_actions is not None else _patient_lifestyle_actions(data, facts, risks)
    for group in groups:
        risk, action = group['owner'], group['action']
        text = _actions([action])
        if not text:
            continue
        key = re.sub(r'[\s。；;]+', '', text)
        names = list(dict.fromkeys(_public_risk_name(member) for member, _ in group['members']))
        source_ids = list(dict.fromkeys([*(fid for member, _ in group['members']
            for fid in member.support_fact_ids), *action.fact_ids]))
        sources = _source_refs(data, facts, source_ids)
        numbers = _action_citations(document, risk, [action])
        if key in existing:
            row = existing[key]
            for name in names:
                if name not in row.name.split('、'):
                    row.name += '、' + name
            row.sources.extend(source for source in sources if source not in row.sources)
            row.evidence_refs = sorted(set([*row.evidence_refs, *numbers]))
            continue
        row = public.NextStep(code=_risk_code(risk), name='、'.join(names), description=text,
            sources=sources, evidence_refs=numbers, availability='available')
        result.append(row)
        existing[key] = row
    return result


def _source_problem_focus(data, document, *, patient_actions=None):
    """Keep real current problems visible independently of disease scoring.

    These source owners carry neither a new disease diagnosis nor a risk
    score.  Their prose and actions still require the ordinary narrative
    review at each exact consumer path.
    """
    from .evaluation_clinical_focus import selected_source_clinical_focus
    frames = {entry['code']: entry for entry in selected_source_clinical_focus(data, document)}
    own = [row for row in data.medical_assessment.findings if row.code in frames]
    if not own:
        return

    def identity(row):
        return (row.name, tuple(sorted((source.source_code or '', source.record_id or '',
            source.observed_at or '') for source in row.sources)))

    current_keys = {identity(row) for row in data.health_overview.key_findings}
    current_priorities = {identity(row) for row in data.comprehensive_conclusion.priority_directions}
    current_steps = {row.code for row in data.comprehensive_conclusion.next_steps}
    covered_plans = set()
    if patient_actions:
        facts = {fact.fact_id: fact for fact in document.facts}
        for group in patient_actions:
            text = _actions([group['action']])
            if not any(step.code == _risk_code(group['owner']) and step.description == text
                    for step in data.comprehensive_conclusion.next_steps):
                continue
            for member, action in group['members']:
                if action.text == group['action'].text:
                    code = _current_source_owner_code(data, document, member, facts, selected_only=True)
                    if code:
                        covered_plans.add(code)
    for row in own:
        stamp = identity(row)
        if stamp not in current_keys:
            data.health_overview.key_findings.append(public.OverviewFinding(
                rank=len(data.health_overview.key_findings) + 1, name=row.name,
                module=row.system, sources=row.sources,
                summary=None, evidence_refs=[], availability='partial'))
        if stamp not in current_priorities:
            data.comprehensive_conclusion.priority_directions.append(public.PriorityDirection(
                rank=len(data.comprehensive_conclusion.priority_directions) + 1,
                name=row.name, basis=row.value, followup_action=None,
                sources=row.sources, evidence_refs=[], availability='partial'))
        if row.code not in current_steps and row.code not in covered_plans:
            data.comprehensive_conclusion.next_steps.append(public.NextStep(
                code=row.code, name=row.name, description=None,
                sources=row.sources, evidence_refs=[], availability='partial'))


def project_integrated_risks(data, document, references=None, *, final=False):
    """Use one approved snapshot; a numeric DTO alone grants no permission."""
    if document is None:
        return data
    from .evaluation_narrative import capture_reviewed_projection_text, restore_reviewed_projection_text
    reviewed_projection = capture_reviewed_projection_text(data, document)
    all_assessed = display_risk_assessments(document, approved_assessments(document))
    if not all_assessed:
        # A failed assessment cannot leave legacy source-model grades posing
        # as new personal health scores or measured findings as disease risks.
        data.health_overview.key_findings = []
        data.comprehensive_conclusion.priority_directions = []
        data.comprehensive_conclusion.next_steps = []
        data.disease_risk_assessment.risk_ranking = []
        data.disease_risk_assessment.risk_index_standards = [public.StandardDefinition(**row)
            for row in catalog.PRESENTATION_RISK_INDEX_STANDARDS]
        facts = {fact.fact_id: fact for fact in document.facts}
        data.medical_assessment.findings = [
            _literal_clinical_measurement(row) if _unclassified_clinical_measurement(row, facts) else row
            for row in data.medical_assessment.findings
            if row.certainty in {'confirmed', 'suspected', 'source_finding',
                'self_reported', 'current_problem'} or _unclassified_clinical_measurement(row, facts)]
        _source_problem_focus(data, document)
        _summarize(data, document, [], {fact.fact_id: fact for fact in document.facts})
        _dimension_projection(data, document, {}, [])
        restore_reviewed_projection_text(data, document, reviewed_projection)
        object.__setattr__(data, '_integrated_risk_projection',
            _ProjectionProof(_fingerprint(data), (), True))
        return data
    from .evaluation_risk_assessment import (
        is_publishable_disease_assessment, disease_assessment_bases)
    from .evaluation_dimension_scores import approved_health_scores
    health_scores = approved_health_scores(document)
    facts = {fact.fact_id: fact for fact in document.facts}
    linked, clinical_support = _disease_centered_risks(document, all_assessed, facts)
    basis_view = disease_assessment_bases(document)
    assessed = [risk for risk in linked if is_publishable_disease_assessment(risk)
        and (basis := basis_view.get(risk.risk_id)) is not None
        and basis['assessment'] is risk
        and basis['basis'] in {'clinical_classification', 'individual_risk'}]
    valid_ids = {risk.risk_id for risk in assessed}
    clinical_support = {rid: ids for rid, ids in clinical_support.items() if rid in valid_ids}
    # The reviewed numerical index has one direction. Other source scales do
    # not participate in this ordering and remain literal category evidence.
    assessed = sorted(assessed, key=lambda risk: _health_priority(risk, health_scores))
    main_ids = {risk.risk_id for risk in assessed if _is_main_health_risk(risk, health_scores)}
    risks = [risk for risk in assessed if risk.risk_id in main_ids
        or risk.risk_id in clinical_support]
    editorial = _editorial_risks(risks, facts, document=document)
    editorial.sort(key=lambda risk: _health_priority(risk, health_scores))
    ranking = []
    from .evaluation_risk_source_links import source_link_view
    source_links = source_link_view(document)
    key_parts = {risk.risk_id: _short_risk_parts(data, document, facts, risk, 'key') for risk in assessed}
    priority_parts = {risk.risk_id: _short_risk_parts(data, document, facts, risk, 'priority')
        for risk in editorial}
    # Keep every independently assessed condition in the detailed numeric
    # view, including low-concern positive screens. Only the editorial/main
    # problem view is shortened; an assessment is not silently dropped.
    for risk in assessed:
        decision = health_scores.get(risk.risk_id)
        detail, detail_refs, detail_availability = _detailed_risk_parts(data, document, facts, risk)
        ranking.append(public.DiseaseRiskItem(code=_risk_code(risk), name=_public_risk_name(risk),
            value=decision['index'] if decision else None, unit=None,
            assessment_level=decision['level'] if decision else None,
            explanation=detail,
            sources=_source_refs(data, facts, risk.support_fact_ids),
            related_taxa=_related_taxa(data, risk, source_links=source_links),
            related_indicators=_related_indicators(data, facts, risk, source_links=source_links,
                clinical_support=clinical_support.get(risk.risk_id, ())),
            evidence_refs=detail_refs, availability=(detail_availability
                if decision and decision.get('availability', 'available') == 'available' else 'partial')))
    data.disease_risk_assessment.risk_ranking = ranking
    data.disease_risk_assessment.risk_index_standards = [public.StandardDefinition(**row)
        for row in catalog.PRESENTATION_RISK_INDEX_STANDARDS]
    data.disease_risk_assessment.notice = ''
    matrices = {row.code: row for row in data.disease_risk_assessment.overview_matrix}
    for category_index, category in enumerate(data.disease_risk_assessment.categories):
        # Fixed-template rebuilding may have copied the ranked output back
        # into its input/model table. Keep integrated decisions once in the
        # ranking; the category detail retains original source observations.
        category.items = _coalesce_risk_source_rows([row for row in category.items
            if not str(row.code or '').startswith('integratedRisk:')
            and not _unmeasured_clinical_compatibility(row, assessed, category.items)])
        own = [risk for risk in assessed if risk.category == category.code]
        if not own:
            # A literal source-model grade cannot stand in for an unassessed
            # composite dimension. Source items and their grades stay intact.
            category.attention_level = category.attention_basis = None
            category.clinical_correlation = public.TextAssessment()
            category.availability = 'partial' if category.items else 'unavailable'
            if category.code in matrices:
                matrix = matrices[category.code]
                matrix.attention_level = matrix.basis = None
                matrix.evidence_refs = []
                matrix.availability = category.availability
            continue
        sources = _source_refs(data, facts, [fid for risk in own for fid in risk.support_fact_ids])
        numbers = []
        reviewed = _reviewed_summary(data, document, facts, 'disease:' + category.code)
        summary = reviewed.text if reviewed is not None else None
        if reviewed is not None:
            numbers = reviewed.evidence_refs
        category.attention_basis = summary
        category.sources, category.evidence_refs = sources, numbers
        category.availability = 'available'
        from .evaluation_narrative import reviewed_evaluation_text
        paragraph = reviewed_evaluation_text(data, document,
            ('diseaseRiskAssessment', 'categories', category_index, 'clinicalCorrelation', 'text'))
        category.clinical_correlation = (public.TextAssessment.model_validate(paragraph)
            if paragraph is not None else public.TextAssessment(availability='partial'))
        if category.code in matrices:
            matrix = matrices[category.code]
            matrix.basis = summary
            matrix.sources, matrix.evidence_refs, matrix.availability = sources, [], 'available'
    data.disease_risk_assessment.availability = 'available'
    display_editorial = [risk for risk in editorial
        if not _current_source_owner_code(data, document, risk, facts, selected_only=True)]
    data.health_overview.key_findings = [public.OverviewFinding(rank=i + 1,
        name=_public_risk_name(risk), module=dict(catalog.RISK_CATEGORY_DEFINITIONS)[risk.category],
        assessment_level=health_scores.get(risk.risk_id, {}).get('level'),
        summary=key_parts[risk.risk_id][0],
        sources=_source_refs(data, facts, risk.support_fact_ids),
        evidence_refs=key_parts[risk.risk_id][1],
        availability=('available' if key_parts[risk.risk_id][0]
            and health_scores.get(risk.risk_id, {}).get('availability') == 'available' else 'partial'))
        for i, risk in enumerate(display_editorial)]
    # Choose the shared activity instruction against the actual projected
    # clinical text, not an earlier authoring snapshot that final projection
    # can still replace. This keeps the second and detailed chapters aligned.
    _medical_projection(data, document, editorial, facts, assessed=assessed,
        clinical_support=clinical_support, health_scores=health_scores)
    plan_sources = _lifestyle_plan_sources(document)
    patient_actions = _patient_lifestyle_actions(data, facts, plan_sources,
        approved_risk_ids={risk.risk_id for risk in plan_sources})
    followups = {risk.risk_id: _priority_followup(data, document, facts, risk,
        patient_actions=patient_actions) for risk in editorial}
    data.comprehensive_conclusion.priority_directions = [public.PriorityDirection(rank=i + 1,
        name=_public_risk_name(risk), basis=priority_parts[risk.risk_id][0],
        followup_action=followups[risk.risk_id][0] or None,
        sources=_source_refs(data, facts, risk.support_fact_ids),
        evidence_refs=sorted(set([*priority_parts[risk.risk_id][1],
            *followups[risk.risk_id][1]])),
        availability='available' if followups[risk.risk_id][0] else 'partial')
        for i, risk in enumerate(display_editorial)]
    data.comprehensive_conclusion.next_steps = _next_step_projection(data, document, facts, plan_sources,
        patient_actions=patient_actions)
    _source_problem_focus(data, document, patient_actions=patient_actions)
    restore_reviewed_projection_text(data, document, reviewed_projection)
    # Main-priority grouping is editorial. It must not remove a distinct
    # adopted current condition or replace it solely with a future disease.
    _summarize(data, document, editorial or assessed, facts)
    if final:
        from .evaluation_narrative import guard_evaluation_published_content
        guard_evaluation_published_content(data, document)
        _compact_published_reported_findings(data, facts)
        # The three domain abstracts and final conclusion have
        # distinct authored responsibilities and remain in their own slots.
    _dimension_projection(data, document, facts, all_assessed)
    for index, finding in enumerate(data.health_overview.key_findings):
        finding.rank = index + 1
    coverage = tuple((_risk_code(risk), tuple(sorted(clinical_support[risk.risk_id])))
        for risk in editorial if risk.risk_id in clinical_support)
    object.__setattr__(data, '_integrated_risk_projection', _ProjectionProof(_fingerprint(data), coverage, True))
    return data
