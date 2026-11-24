"""Request-local original panel/member links for evaluate risk interpretation.

These links describe an uploaded report's own layout, not a medical association
or an approval to publish a disease claim. Measured facts are never rewritten.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any

from .evaluation_schema import EvaluationData
from .schemas import ClinicalReportDocument


_LOCATION = re.compile(r'^source:([a-fA-F0-9]{64}):(?:json:|page:\d+:template:)(.+)$')


@dataclass(frozen=True)
class _SourceLinkBinding:
    owner: int
    data: EvaluationData
    facts_digest: str
    inventory_digest: str
    edges: tuple[dict[str, Any], ...]
    association_panels: tuple[dict[str, Any], ...] = ()
    observed_problems: tuple[dict[str, Any], ...] = ()


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str).encode()).hexdigest()


def _facts_digest(document):
    return _digest(sorted((fact.model_dump(mode='json', by_alias=True)
        for fact in document.facts), key=lambda row: row['factId']))


def _inventory_digest(data):
    return _digest([row.model_dump(mode='json', by_alias=True)
        for row in data.appendices.source_items])


def _locations(row):
    return [(match[1].lower(), match[2]) for source in row.sources
        if source.source_code == 'C' and (match := _LOCATION.fullmatch(source.record_id or ''))]


def _same_value(left, right):
    if left is None or right is None:
        return left is right
    if str(left) == str(right):
        return True
    try:
        a, b = Decimal(str(left)), Decimal(str(right))
        return a.is_finite() and b.is_finite() and a == b
    except InvalidOperation:
        return False


def _names(row):
    return {str(value).strip().casefold() for value in (row.name, row.scientific_name) if value}


def _bound_fact_ids(row, facts, kinds):
    """A source path identifies the row; aliases then verify its exact value."""
    output = []
    for source in row.sources:
        fact = facts.get(source.record_id)
        if fact is None or fact.kind not in kinds or source.source_code != 'C':
            continue
        if (fact.source != row.source_name or fact.date != row.observed_at
                or source.observed_at not in (None, fact.date)
                or not _same_value(row.value, fact.value)
                or row.unit != fact.unit or row.source_status != fact.status
                or row.reference_range != fact.reference_range):
            continue
        aliases = {value.strip().casefold() for value in fact.name.split(' / ')}
        if not aliases & _names(row):
            continue
        if fact.fact_id not in output:
            output.append(fact.fact_id)
    return output


def _direction(value):
    text = str(value or '')
    return 'up' if re.search(r'偏高|升高|超标|↑', text) else (
        'down' if re.search(r'偏低|降低|缺乏|↓', text) else None)


def _declared_members(row):
    result = {}
    # The source abnormal-list field already survives as description. An
    # occurrence still must belong to this exact native panel below.
    for clause in re.split(r'[,，、;；\n]+', row.description or ''):
        match = re.fullmatch(r'\s*(.+?)\s*([↑↓])\s*', clause)
        if match:
            result[match[1].strip().casefold()] = 'up' if match[2] == '↑' else 'down'
    for name in row.related_taxa:
        result.setdefault(name.strip().casefold(), None)
    return result


def bind_evaluation_source_links(document, data):
    """Bind typed source inventory to its exact original native occurrences.

    The trusted evaluate pipeline calls this after fact and source projection.
    A callback/dictionary has neither this private binding nor usable authority.
    """
    if not isinstance(document, ClinicalReportDocument):
        return
    object.__setattr__(document, '_evaluation_source_links', None)
    if not isinstance(data, EvaluationData):
        return
    facts = {fact.fact_id: fact for fact in document.facts}
    inventory = data.appendices.source_items
    edges = {}
    association_panels = []
    observed_problems = []
    from .clinical_report import _outside_printed_reference
    for row in data.medical_assessment.findings:
        if str(row.code or '').startswith('integratedRisk:') or row.certainty in {'model_risk', 'source_association'}:
            continue
        own = list(dict.fromkeys(source.record_id for source in row.sources
            if source.record_id in facts and facts[source.record_id].kind in {'clinical', 'self_reported'}))
        if not own:
            continue
        abnormal = [fid for fid in own if facts[fid].kind == 'clinical' and (facts[fid].abnormal
            or _outside_printed_reference(facts[fid].value, facts[fid].reference_range, facts[fid].unit))]
        reported = [fid for fid in own if facts[fid].kind == 'self_reported']
        if not abnormal and not (row.certainty == 'self_reported' and reported) and row.certainty != 'confirmed':
            continue
        observed_problems.append({'observationId': row.code or _digest((row.name, own)),
            'name': row.name, 'certainty': row.certainty or 'source_finding',
            'value': row.value,
            'factIds': list(dict.fromkeys([*abnormal, *reported])) or own,
            'sourceDiagnosis': row.certainty == 'confirmed'})
    for panel in inventory:
        if panel.kind != 'context' or not (panel.category in {
                'healthAssociation', 'structuralAssessment'}
                or str(panel.code or '').startswith('health_summary_')):
            continue
        parents = _bound_fact_ids(panel, facts, {'context'})
        if not parents:
            continue
        # The report declares the condition being screened. Its name need
        # not occur in our disease glossary, and no abnormal-member list is
        # required to retain the report's explicit positive screen result.
        origins = [source.record_id for source in panel.sources
            if source.source_code == 'C' and re.fullmatch(
                r'source:[a-fA-F0-9]{64}:.+', source.record_id or '')]
        if panel.category == 'healthAssociation' and origins:
            association_panels.append({'factIds': parents, 'name': panel.name,
                'value': panel.value, 'sourceStatus': panel.source_status,
                'sourceName': panel.source_name, 'observedAt': panel.observed_at,
                'sourceRefs': origins})
        declaration = _declared_members(panel)
        for checksum, panel_path in _locations(panel):
            match = re.fullmatch(r'((?:jkj|jun|yingyang)_[A-Za-z0-9_]+|yingyang2_[A-Za-z0-9_]+)_all_result', panel_path)
            if not match:
                continue
            prefix = match[1]
            if panel.category == 'healthAssociation':
                from .evaluation_template_input import HEALTH_ASSOCIATION_PANELS
                from .evaluation_disease_identity import same_source_disease
                expected = HEALTH_ASSOCIATION_PANELS.get(prefix)
                if not expected or not (panel.name == expected or same_source_disease(panel.name, expected)):
                    continue
            if panel.category == 'healthAssociation' and not declaration:
                continue  # No abnormal member was explicitly listed.
            for member in inventory:
                if (member.kind not in {'taxon', 'microbe', 'function', 'metric'}
                        or (member.source_name, member.observed_at) != (panel.source_name, panel.observed_at)):
                    continue
                member_paths = [path for sha, path in _locations(member)
                    if sha == checksum and re.fullmatch(re.escape(prefix) + r'\[\d+\](?:\.score)?', path)]
                if not member_paths:
                    continue
                expected_kinds = {'microbe'} if member.kind in {'taxon', 'microbe'} else {'function'}
                member_ids = _bound_fact_ids(member, facts, expected_kinds)
                if not member_ids:
                    continue
                declared = [declaration[name] for name in _names(member) if name in declaration]
                if panel.category == 'healthAssociation' and (not declared or any(
                        direction and direction != _direction(member.source_status) for direction in declared)):
                    continue
                role = ('source_association_member' if panel.category == 'healthAssociation'
                    else 'source_function_member' if expected_kinds == {'function'}
                    else 'source_detection_panel_member')
                for parent in parents:
                    for path in member_paths:
                        organism = (member.scientific_name or member.name or '').strip().casefold()
                        key = (parent, checksum, panel_path, path, organism,
                            str(member.value), member.unit, member.reference_range, str(member.source_status))
                        if key not in edges:
                            edges[key] = {'panelFactId': parent, 'panelName': panel.name,
                                'ownerFactIds': [parent],
                                'panelValue': panel.value, 'memberFactId': member_ids[0],
                                'memberFactIds': list(member_ids), 'name': member.name,
                                'sourceNames': [name for name in (member.name, member.scientific_name) if name],
                                'scientificName': member.scientific_name, 'value': member.value,
                                'unit': member.unit, 'referenceRange': member.reference_range,
                                'sourceStatus': member.source_status, 'sourceName': member.source_name,
                                'observedAt': member.observed_at, 'checksum': checksum,
                                'panelPath': panel_path, 'memberPath': path, 'relationRole': role,
                                'panelSourceRefs': [source.record_id for source in panel.sources
                                    if source.source_code == 'C' and (match := _LOCATION.fullmatch(source.record_id or ''))
                                    and (match[1].lower(), match[2]) == (checksum, panel_path)],
                                'memberSourceRefs': [source.record_id for source in member.sources
                                    if source.source_code == 'C' and (match := _LOCATION.fullmatch(source.record_id or ''))
                                    and (match[1].lower(), match[2]) == (checksum, path)],
                                'originalDirection': _direction(member.source_status)}
                        else:
                            edges[key]['memberFactIds'].extend(fid for fid in member_ids
                                if fid not in edges[key]['memberFactIds'])
                            edges[key]['sourceNames'].extend(name for name in (member.name, member.scientific_name)
                                if name and name not in edges[key]['sourceNames'])
                            edges[key]['memberSourceRefs'].extend(source.record_id for source in member.sources
                                if source.source_code == 'C' and (match := _LOCATION.fullmatch(source.record_id or ''))
                                and (match[1].lower(), match[2]) == (checksum, path)
                                and source.record_id not in edges[key]['memberSourceRefs'])
    # A builder disease item can explicitly carry both its source-model fact
    # and this exact panel/member occurrence. Preserve that supplied bridge;
    # a shared disease name or a same-name global organism is insufficient.
    from .evaluation_disease_identity import same_source_disease
    items = [item for category in data.disease_risk_assessment.categories for item in category.items]
    items.extend(data.disease_risk_assessment.risk_ranking)
    for item in items:
        ids = {source.record_id for source in item.sources if source.source_code == 'C'}
        models = [facts[fid] for fid in ids if fid in facts and facts[fid].kind == 'function'
            and facts[fid].name.endswith('（来源疾病模型指数）')
            and same_source_disease(item.name, facts[fid].name.removesuffix('（来源疾病模型指数）'))
            and _same_value(item.value, facts[fid].value) and item.source_status == facts[fid].status]
        if not models:
            continue
        for edge in edges.values():
            if edge['panelFactId'] not in ids:
                continue
            if not any((edge['checksum'], edge['memberPath']) in _locations(member)
                    and set(edge['memberFactIds']) & {source.record_id for source in member.sources}
                    for member in item.related_taxa):
                continue
            for model in models:
                if model.source == edge['sourceName'] and model.date == edge['observedAt']:
                    if model.fact_id not in edge['ownerFactIds']:
                        edge['ownerFactIds'].append(model.fact_id)
    binding = _SourceLinkBinding(id(document), data, _facts_digest(document),
        _inventory_digest(data), tuple(edges.values()), tuple(association_panels), tuple(observed_problems))
    object.__setattr__(document, '_evaluation_source_links', binding)


def source_link_view(document):
    """Return fresh internal values only while both original sources match."""
    if not isinstance(document, ClinicalReportDocument):
        return {}
    binding = getattr(document, '_evaluation_source_links', None)
    if (not isinstance(binding, _SourceLinkBinding) or binding.owner != id(document)
            or binding.facts_digest != _facts_digest(document)
            or binding.inventory_digest != _inventory_digest(binding.data)):
        return {}
    result = {}
    # A JSON copy prevents a model-author packet from mutating the binding.
    for edge in json.loads(json.dumps(binding.edges, ensure_ascii=False)):
        for owner in edge['ownerFactIds']:
            result.setdefault(owner, []).append(json.loads(json.dumps(edge, ensure_ascii=False)))
    return result


def source_association_view(document):
    """Exact, live report declarations; not authority to diagnose a person."""
    if not isinstance(document, ClinicalReportDocument):
        return []
    binding = getattr(document, '_evaluation_source_links', None)
    if (not isinstance(binding, _SourceLinkBinding) or binding.owner != id(document)
            or binding.facts_digest != _facts_digest(document)
            or binding.inventory_digest != _inventory_digest(binding.data)):
        return []
    return json.loads(json.dumps(binding.association_panels, ensure_ascii=False))


def source_current_problem_view(document):
    """Adopted current findings, kept separately from future-disease exposure."""
    if not isinstance(document, ClinicalReportDocument):
        return []
    binding = getattr(document, '_evaluation_source_links', None)
    if (not isinstance(binding, _SourceLinkBinding) or binding.owner != id(document)
            or binding.facts_digest != _facts_digest(document)
            or binding.inventory_digest != _inventory_digest(binding.data)):
        return []
    return json.loads(json.dumps(binding.observed_problems, ensure_ascii=False))


def transfer_evaluation_source_links(original, copied):
    """Transfer a live source proof only to an explicit same-facts work copy."""
    if not isinstance(copied, ClinicalReportDocument):
        return False
    object.__setattr__(copied, '_evaluation_source_links', None)
    binding = getattr(original, '_evaluation_source_links', None)
    if (not isinstance(binding, _SourceLinkBinding) or binding.owner != id(original)
            or _facts_digest(original) != binding.facts_digest
            or _inventory_digest(binding.data) != binding.inventory_digest
            or not (binding.edges or binding.association_panels or binding.observed_problems)
            or _facts_digest(copied) != binding.facts_digest):
        return False
    object.__setattr__(copied, '_evaluation_source_links', _SourceLinkBinding(
        id(copied), binding.data, binding.facts_digest, binding.inventory_digest, binding.edges,
        binding.association_panels, binding.observed_problems))
    return True


copy_evaluation_source_links = transfer_evaluation_source_links
