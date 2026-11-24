"""Source-first blueprint for the seven existing evaluate chapters.

This module projects input facts and declares writable prose locations. It does
not discover/score disease risks, call models, retrieve evidence, or authorize
medical claims. Complete literal inventories stay private; targets follow the
actual callback shape so authoring is not discarded by delivery compaction.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from typing import Any

from . import evaluation_schema as public
from .assessment_rule_sources import cell_rule_conclusions
from .evaluation_builder import project_evaluation_source_data
from .evaluation_clinical_focus import selected_source_clinical_focus
from .evaluation_derived_indices import derived_index_context
from .evaluation_narrative import add_evaluation_source_facts
from .evaluation_template_contract import CHAPTERS as _CHAPTERS
from .evaluation_template_contract import cell_contract
from .evaluation_template_contract import topic_guidance


@dataclass
class ReportBlueprint:
    data: public.EvaluationData
    facts: list[dict[str, Any]]
    focus: list[dict[str, Any]]
    chapters: list[dict[str, Any]]
    clinical_trends: list[dict[str, Any]] = field(default_factory=list)
    assessment_sources: dict[str, Any] = field(default_factory=dict)


_WRITABLE = frozenset({'text', 'summary', 'explanation', 'association', 'health_meaning',
    'description', 'basis', 'followup_action', 'attention_basis', 'evidence_strength', 'modifiability',
    'source_advice', 'resistance_notice'})


def _get(value, *names, default=None):
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _present(value):
    return value is not None and value != '' and value != [] and value != {}


def _seed_original_action(target):
    """Use source-verified instructions in slots whose duty includes original advice."""
    owner = target['owner']
    code = owner.get('contentScopeCode') or ''
    source_duty = code in {'priority.urgent', 'priority.planned', 'priority.routine'} or (
        code.startswith('synthesisAction.') and owner.get('actionBasisScope') == 'personal_clinical_or_behavior'
    )
    include_advice = code in {'layer.clinical', 'digestive.lowerDigestive'}
    if not (source_duty or include_advice or code.startswith('synthesisAction.')):
        return
    actions = owner.get('sourceClinicalActions') or ()
    bound = set(target['sourceFactIds'])
    by_text = {}
    quotes = {}
    objects = set()
    all_scoped = bool(actions)
    for action in actions:
        scopes = action.get('sourceAdviceScopes') or ()
        all_scoped = all_scoped and bool(scopes)
        for scope in scopes:
            if scope.get('displayScope') == 'source_reference':
                all_scoped = False
                continue
            identifiers = scope.get('sourceFactIds') or ()
            quote = scope.get('quote')
            if (not quote or not identifiers or not set(identifiers) <= bound or not scope.get('sourceSpans')
                    or scope.get('scopeStatus') not in {'explicit_object', 'source_record_scope'}):
                all_scoped = False
                continue
            # The object heading and clinical text are both originally
            # verified. Keep the quote intact, including any conditions.
            labels = scope.get('objectLabels') or scope.get('sourceRecordLabels') or ()
            label = '、'.join(labels)
            text = f'针对{label}，检查建议为“{quote}”' if label else f'检查建议为“{quote}”'
            if code == 'digestive.lowerDigestive' and not set(identifiers).intersection(owner['primaryFactIds']):
                continue
            by_text.setdefault(text, {}).update(dict.fromkeys(identifiers))
            quotes[text] = quote
            objects.update(labels)
    primary = set(owner.get('primaryFactIds') or ())
    if not any(primary.intersection(ids) for ids in by_text.values()):
        return
    sentences = [
        {'text': text, 'factIds': list(ids), 'role': 'action', 'evidence': [], 'support': 'source',
         'sourceQuote': quotes[text]}
        for text, ids in by_text.items()
    ]
    # A list of original instructions does not rank several problems, explain
    # a cluster, or establish a care plan. Keep these passages available to
    # the writer and the grounding editor through sourceClinicalActions.
    if len(sentences) != 1 or len(objects) != 1:
        return
    complete = source_duty and all_scoped
    slot = 'sourceBlock' if complete else 'sourceFallback'
    target[slot] = {
        'targetId': target['id'], 'completion': 'complete' if complete else 'source_only',
        'analysisGenerated': False, 'sentences': sentences,
    }


def _seed_source_observations(target, facts, sample):
    """Publish exact calculation inputs or the recorded sample-time boundary."""
    owner = target['owner']
    scope = owner.get('contentScopeCode', '')
    if scope.startswith('clinicalRow.'):
        notes = []
        for fid in owner['primaryFactIds']:
            context = derived_index_context(facts[fid])
            if context and context['valueKind'] == 'calculated':
                notes.append({
                    'text': f'{context["formula"]}；代入原结果：{"、".join(context["inputs"])}。',
                    'factIds': [fid], 'role': 'observation', 'evidence': [], 'support': 'source',
                })
        if len(notes) > 1:
            target.setdefault('sourceSupplement', []).extend(notes)
            target['sourceFallback'] = {
                'targetId': target['id'], 'completion': 'source_only', 'analysisGenerated': False,
                'sentences': notes,
            }
    if scope != 'sampleSummary' or sample.get('sampleDate') or sample.get('qualityControl'):
        return
    dates = [
        (label, date, [fid for fid in owner['primaryFactIds']
                      if facts[fid].get('name') == '来源' + label and facts[fid].get('value') == date])
        for label, date in (('收样日期', sample.get('receivedDate')), ('报告日期', sample.get('reportDate')))
        if isinstance(date, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', date)
    ]
    confirmed = [(label, date, ids) for label, date, ids in dates if ids]
    if confirmed:
        dates_text = '，'.join(f'{label}为{date}' for label, date, _ in confirmed)
        target['sourceBlock'] = {
            'targetId': target['id'], 'completion': 'complete', 'analysisGenerated': False,
            'sentences': [{
                'text': f'菌群检测{dates_text}。采样日未注明，样本采集与临床体检的间隔无法确定。',
                'factIds': list(dict.fromkeys(fid for _, _, ids in confirmed for fid in ids)),
                'role': 'observation', 'evidence': [], 'support': 'source',
            }],
        }


def _result(row):
    return any(_present(row.get(key)) for key in ('value', 'sourceStatus', 'assessmentLevel', 'detectionStatus'))


def _references(value):
    result = []
    if isinstance(value, dict):
        for source in value.get('sources', []):
            if isinstance(source, dict) and source not in result:
                result.append(source)
        for child in value.values():
            for source in _references(child):
                if source not in result:
                    result.append(source)
    elif isinstance(value, list):
        for child in value:
            for source in _references(child):
                if source not in result:
                    result.append(source)
    return result


def _codes(value):
    result = set()
    if isinstance(value, dict):
        if value.get('code') is not None:
            result.add(str(value['code']))
        for child in value.values():
            result.update(_codes(child))
    elif isinstance(value, list):
        for child in value:
            result.update(_codes(child))
    return result


def _fact_ids(value, facts):
    return list(dict.fromkeys(source.get('recordId') for source in _references(value)
        if source.get('recordId') in facts))


def _typed_path(data, path):
    """Translate an existing wire path into existing Python model attributes."""
    value, result = data, []
    for part in path:
        if isinstance(part, int):
            value = value[part]
            result.append(part)
            continue
        if isinstance(value, dict):
            value = value[part]
            result.append(part)
            continue
        fields = type(value).model_fields
        name = next((name for name, field in fields.items()
            if name == part or field.alias == part or field.serialization_alias == part), None)
        if name is None:
            raise KeyError(tuple(path))
        value = getattr(value, name)
        result.append(name)
    return result


def _wire_get(value, path):
    for part in path:
        value = value[part]
    return value


def _source_rows(data, facts, document, directions=None, *, parsed=None):
    terms = {}
    for problem in _get(document, 'problems', default=[]) or []:
        for witness in _get(problem, 'sources', default=[]) or []:
            fid = _get(witness, 'fact_id', 'factId')
            terms.setdefault(fid, [])
            for term in _get(problem, 'search_terms', 'searchTerms', default=[]) or []:
                if term not in terms[fid]:
                    terms[fid].append(term)
    rows = []
    for item in data.appendices.source_items:
        row = item.model_dump(mode='json', by_alias=True, exclude_none=False)
        ids = _fact_ids(row, facts)
        row['sourceFactIds'] = ids
        row['searchTerms'] = list(dict.fromkeys(term for fid in ids for term in terms.get(fid, [])))
        row['aliases'] = list(dict.fromkeys([name for name in
            (row.get('name'), row.get('scientificName')) if isinstance(name, str) and name]))
        # The existing input adapter already separates detected genes from
        # panel candidates. Carry that literal typed metadata to the model
        # view; a scalar "detected" label cannot identify the measured gene.
        if row.get('category') == 'virulence':
            row_refs = {ref.get('recordId') for ref in row.get('sources', ())}
            gene_context = []
            for gene in getattr(data, '_template_layout', {}).get('geneDetections', ()):
                if (set(gene.get('sourceFactIds', ())).intersection(ids)
                        or row_refs.intersection(ref.get('record_id', ref.get('recordId'))
                            for ref in gene.get('sources', ()))):
                    gene_context.append({key: deepcopy(gene.get(key)) for key in (
                        'reportedLabel', 'annotationVerified', 'detectedGenes',
                        'candidateGenes', 'claimObjects', 'sourceStatus')})
            if gene_context:
                row['geneIdentityContext'] = gene_context
            originals = _get(parsed, 'supplementary_hospital_data', default=[]) or []
            origins = set()
            for source in row.get('sources', ()):
                match = re.fullmatch(r'parsed\.supplementaryHospitalData\[(\d+)\]',
                    str(source.get('recordId') or ''))
                if match and int(match[1]) < len(originals):
                    origins.add(int(match[1]))
            # An ambiguous source join remains unresolved rather than joining
            # lists from different occurrences or promoting panel candidates.
            if len(origins) == 1:
                original = originals[next(iter(origins))]
                for key, snake in (('detectedGenes', 'detected_genes'),
                        ('candidateGenes', 'candidate_genes')):
                    value = _get(original, key, snake)
                    if isinstance(value, list) and all(isinstance(gene, str) for gene in value):
                        row[key] = deepcopy(value)
        if binding := (directions or {}).get(item.observation_id):
            row.update({key: deepcopy(value) for key, value in binding.items()
                if key in {'direction', 'directions'}})
            row['sourceFactIds'] = list(dict.fromkeys([*row['sourceFactIds'], *binding['factIds']]))
        rows.append(row)
    return rows


def _native_location(source):
    """Read only a current source locator; never infer origin from a label."""
    record = source.record_id or ''
    match = re.fullmatch(r'source:([a-fA-F0-9]{64}):(json:|page:\d+:template:)(.+)', record)
    if match and source.source_code == 'C':
        return match[1].casefold(), match[2], match[3], source.observed_at
    return None


def _seed_virulence_observation(target, panel_row, detections):
    """Publish only literal positive sequence targets owned by this measured panel."""
    primary = set(target['owner']['primaryFactIds'])
    symbols, anchors = [], []
    for record in detections:
        owned = [fid for fid in record.get('sourceFactIds', ()) if fid in primary]
        hits = record.get('claimObjects') or ()
        if owned and hits:
            symbols.extend(hits)
            anchors.extend(owned)
    if symbols:
        summary = '粪便样本检出' + '、'.join(dict.fromkeys(symbols)) + '基因序列。'
    elif (panel_row.get('sourceStatus') or panel_row.get('value')) == '检出':
        summary = '粪便样本的基因序列检测面板记录为检出。'
        anchors = target['owner']['primaryFactIds'][:1]
    else:
        return
    target['sourceBlock'] = {
        'targetId': target['id'], 'completion': 'complete', 'analysisGenerated': False,
        'sentences': [{
            'text': summary, 'role': 'observation', 'evidence': [], 'support': 'source',
            'factIds': list(dict.fromkeys(anchors)),
        }],
    }



def _seed_barrier_observation(target, panel_row, microbiome_view, facts):
    """Record the native panel and its independently observed member deviations."""
    from .assessment_microbiome_view import _barrier_summary
    from .assessment_microbiome_view import _source_is_abnormal

    scopes = {'jun_pzss_all_result': 'jun_pzss', 'jun_children_pzss_all_result': 'jun_children_pzss'}
    panel_locators = []
    for reference in panel_row['sources']:
        source = public.SourceReference.model_validate(reference)
        if (location := _native_location(source)) and location[2] in scopes:
            panel_locators.append((reference['recordId'], location))
    if len(panel_locators) != 1:
        return
    locator, (checksum, format_prefix, panel_path, date) = panel_locators[0]
    rows = [row for group in microbiome_view['structureGroups'] if group['code'] == 'barrier'
            for row in group['rows']]
    panels = [row for row in rows if row['code'] == 'barrierRelatedBacteria']
    if len(panels) != 1 or not any(ref['recordId'] == locator for ref in panels[0]['sources']):
        return
    primary = set(target['owner']['primaryFactIds'])

    def matching_ids(row, kind):
        return [fid for fid in _fact_ids(row, facts) if fid in primary
                and facts[fid]['kind'] == kind and facts[fid]['date'] == date
                and facts[fid]['name'].split(' / ', 1)[0] == row['name']
                and facts[fid]['value'] == row['value'] and facts[fid]['status'] == row['status']]

    panel_ids = matching_ids(panels[0], 'context')
    member_prefix = f'source:{checksum}:{format_prefix}{scopes[panel_path]}['
    members, member_ids = [], []
    for row in rows:
        if row['unit'] != '%' or not _source_is_abnormal(row['status']):
            continue
        known_locations = {ref['recordId'] for ref in row['sources'] if ref['sourceCode'] == 'C'
                           and ref['observedAt'] == date and ref['recordId'].startswith(member_prefix)}
        owned = matching_ids(row, 'microbe') if known_locations else []
        if not owned:
            return
        members.append(row)
        member_ids.extend(owned)
    if not (panel_ids and members and (text := _barrier_summary([*panels, *members]))):
        return
    target['sourceOnlyComplete'] = True
    target['sourceFallback'] = {
        'targetId': target['id'], 'completion': 'source_only', 'analysisGenerated': False,
        'sentences': [{'text': text, 'role': 'observation', 'support': 'source', 'evidence': [],
            'factIds': list(dict.fromkeys([*panel_ids, *member_ids]))}],
    }


def _bind_organism_panel(chapter, data, published_group, panels, facts, *, source_code, metric_code):
    """Share prose only when both projections own the same native result."""
    from .assessment_microbiome_view import _scfa_producer_observation
    from .evaluation_builder import _source_locator
    from .evaluation_source_recovery import EvaluationHealthSummaryMetric
    from .evaluation_template_input import EvaluationTemplateHealthMetric

    native = [
        (index, item) for index, item in enumerate(panels)
        if isinstance(item, (EvaluationHealthSummaryMetric, EvaluationTemplateHealthMetric))
        and item.code == source_code
    ]
    if len(native) != 1:
        return
    index, panel = native[0]
    fact_id = f'parsed.evaluationPanelSummaries[{index}]'
    locator = _source_locator(panel)
    if not locator or not panel.source_checksum or fact_id not in facts:
        return
    ids = {fact_id, locator}
    rendered = [
        row for row in (*published_group['rows'], *published_group.get('normalBaseline', {}).get('rows', ()))
        if row['code'] == metric_code
    ]
    metrics = data.microbiome_assessment.health_metrics
    slots = [(offset, row) for offset, row in enumerate(metrics) if row.code == metric_code]
    if len(rendered) != 1 or len(slots) != 1:
        return
    offset, metric = slots[0]
    if not (
        ids.issubset(source['recordId'] for source in rendered[0]['sources'] if source['sourceCode'] == 'C')
        and ids.issubset(source.record_id for source in metric.sources if source.source_code == 'C')
    ):
        return
    path = ['microbiome_assessment', 'health_metrics', offset, 'explanation']
    targets = chapter['targets']
    group_target = next((target for target in targets if target['path'] == [
        '_assessment_editorial', 'organisms.' + published_group['code'], 'text']), None)
    metric_target = next((target for target in targets if target['path'] == path), None)
    planned = isinstance(getattr(data, '_assessment_content_plan', None), dict)
    if not group_target or fact_id not in group_target['owner']['primaryFactIds']:
        return
    if not planned:
        if not metric_target or fact_id not in metric_target['owner']['primaryFactIds']:
            return
        group_target['required'] = metric_target['required']
        group_target['sharedMicrobiomePanel'] = {
            'path': path, 'factId': fact_id, 'locator': locator, 'code': metric_code,
        }
    for scoped_ids in (group_target['owner']['primaryFactIds'], group_target['sourceFactIds']):
        scoped_ids.remove(fact_id)
        scoped_ids.insert(0, fact_id)
    if planned:
        group_target['owner'].setdefault('requiredFactGroups', []).append([fact_id])
        group_target['cellContract']['question'] += (
            '引用本组原始组合评定的事实ID，保留其实际状态与测量层级，再按已审核关联计划解释'
            '实际组内构成及有据的个人关联。组状态不能被别组方向或功能预测替代。'
        )
        # New planned prose is independently authored and reviewed. A literal
        # direction list cannot stand in for that responsibility.
        group_target['sourceOnlyComplete'] = False
        group_target.pop('sourceFallback', None)
        if metric_target:
            targets.remove(metric_target)
        return
    group_target['cellContract']['question'] += (
        '用组合评定本身的事实ID写明本次组合所见，再分别说明组内偏离的方向。'
        '本段限于该菌群组合及组内检测，不加入个人症状、用药或补充剂记录。'
    )
    if metric_code == 'scfaProducerBacteria':
        members = [*published_group['rows'], *published_group.get('normalBaseline', {}).get('rows', ())]
        names = {row['name'].casefold() for row in members}
        codes = {row['code'] for row in members}
        inside, outside = set(names), set()
        for taxon in data.microbiome_assessment.taxa:
            aliases = {name.casefold() for name in (taxon.name, taxon.scientific_name) if name and len(name) >= 4}
            if taxon.code in codes or aliases.intersection(names):
                inside.update(aliases)
            else:
                outside.update(aliases)
        if outside - inside:
            group_target['organismPanelAliases'] = {
                'inside': sorted(inside), 'outside': sorted(outside - inside),
            }
        group_target['cellContract']['question'] += (
            '同时出现升高和降低时，说明组内双向偏离，无需罗列全部原值。'
            '相对丰度和短链脂肪酸的合成能力预测属于两项不同检测。'
            '避免把组内构成方向说成短链脂肪酸实际生成量及疾病风险。'
        )
        summary, witnesses = _scfa_producer_observation(published_group)
        allowed = set(group_target['sourceFactIds'])
        source_ids = [
            [ref['recordId'] for ref in row['sources']
             if ref['sourceCode'] == 'C' and ref.get('recordId') in allowed]
            for row in witnesses
        ]
        if summary and source_ids and all(source_ids) and fact_id in source_ids[0]:
            # The observed directions may complete this measured group only
            # when every directional witness belongs to the panel's own snapshot.
            member_locators = {
                source.record_id for source in metric.sources
                if source.record_id != locator and (native_location := _native_location(source))
                and native_location[0] == panel.source_checksum.casefold()
            }
            group_target['sourceOnlyComplete'] = len(witnesses) > 1 and all(
                any(ref['sourceCode'] == 'C' and ref.get('recordId') in member_locators for ref in row['sources'])
                for row in witnesses[1:]
            )
            group_target['sourceFallback'] = {
                'targetId': group_target['id'], 'completion': 'source_only', 'analysisGenerated': False,
                'sentences': [{
                    'text': summary, 'role': 'observation', 'evidence': [], 'support': 'source',
                    'factIds': list(dict.fromkeys(fid for ids in source_ids for fid in ids)),
                }],
            }
    targets.remove(metric_target)


def _bind_organism_panels(chapter, data, parsed, microbiome_view, facts):
    scopes = {
        'beneficial': ('health_summary_beneficial', 'beneficialBacteria'),
        'scfaProducers': ('health_summary_scfa_producers', 'scfaProducerBacteria'),
    }
    for group in microbiome_view.get('organismGroups', ()):
        if binding := scopes.get(group['code']):
            source_code, metric_code = binding
            _bind_organism_panel(chapter, data, group, parsed.evaluation_panel_summaries, facts,
                source_code=source_code, metric_code=metric_code)


def _direction_name(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', str(value or '')).casefold())


def _project_source_directions(data, document):
    """Bind literal native panel annotations to measurements and every mirror.

    Source inventory and existing facts remain unchanged. Added context facts
    quote an actual note verbatim; arrows do not create or change any grade.
    The same source checksum, panel, date and unambiguous member are required.
    """
    from .schemas import ReportSourceFact

    inventory = data.appendices.source_items
    members, contexts = {}, []
    for item in inventory:
        code = re.fullmatch(r'template_function:(.+)\[(\d+)\]\.score', item.code or '')
        for source in item.sources:
            location = _native_location(source)
            if location is None:
                continue
            checksum, format_key, path, date = location
            if code and path == f'{code[1]}[{code[2]}].score':
                panel = checksum, format_key, code[1], date
                member = members.setdefault(panel, {}).setdefault(path,
                    {'item': item, 'names': set()})
                member['names'].update(_direction_name(name) for name in (item.name, item.scientific_name) if name)
            elif (isinstance(item.description, str) and re.search('[↑↓]', item.description)
                    and (stem := re.fullmatch(r'(.+)_all(?:_result)?', path))):
                contexts.append(((checksum, format_key, stem[1], date), item, source))

    contexts = [row for row in contexts if row[0] in members]
    facts = {fact.fact_id: fact for fact in document.facts}
    proposals, metadata, context_metadata = {}, {}, {}
    for panel, item, source in contexts:
        note = item.description
        # Match only members actually supplied in this exact panel. Repeated
        # display mirrors do not count as additional measured members.
        parsed = []
        for match in re.finditer(r'([^,，;；\n]+?)\s*([↑↓])', note):
            name, direction = match[1].strip(), match[2]
            matching = [path for path, member in members[panel].items()
                if _direction_name(name) in member['names']]
            if len(matching) == 1:
                parsed.append((matching[0], name, direction))
        if not parsed:
            continue
        fact_id = 'source-annotation-' + hashlib.sha256(json.dumps(
            [source.record_id, source.observed_at, note], ensure_ascii=False).encode()).hexdigest()[:24]
        if fact_id not in facts:
            fact = ReportSourceFact(fact_id=fact_id, kind='context',
                name=(item.name or '源面板') + '原始方向注解', value=note,
                date=source.observed_at or item.observed_at,
                source=f'{item.source_name or "C"}:{source.record_id}#description')
            document.facts.append(fact)
            facts[fact_id] = fact
        elif facts[fact_id].value != note:
            continue
        provenance = {'factId': fact_id, 'sourceRecordId': source.record_id,
            'observedAt': source.observed_at, 'inventoryObservationId': item.observation_id,
            'literalNote': note}
        context_metadata.setdefault(item.observation_id, {'factIds': [], 'directions': []})
        for path, name, direction in parsed:
            key = panel, path
            proposals.setdefault(key, set()).add(direction)
            metadata.setdefault(key, []).append(provenance)
            context = context_metadata[item.observation_id]
            if fact_id not in context['factIds']:
                context['factIds'].append(fact_id)
            entry = {'name': name, 'direction': direction, 'sourceFactId': fact_id}
            if entry not in context['directions']:
                context['directions'].append(entry)

    for (panel, path), signs in proposals.items():
        original = members[panel][path]['item'].source_status
        if isinstance(original, str) and original.strip() in {'↑', '↓'}:
            signs.add(original.strip())
    bindings = {key: next(iter(signs)) for key, signs in proposals.items() if len(signs) == 1}
    result = dict(context_metadata)
    for (panel, path), direction in bindings.items():
        item = members[panel][path]['item']
        result[item.observation_id] = {'direction': direction,
            'factIds': list(dict.fromkeys(row['factId'] for row in metadata[(panel, path)]))}

    seen = set()
    def visit(value):
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif hasattr(type(value), 'model_fields'):
            if isinstance(value, public.MetricAssessment):
                found = []
                for source in list(value.sources):
                    if (location := _native_location(source)) is None:
                        continue
                    checksum, format_key, path, date = location
                    member = re.fullmatch(r'(.+)\[\d+\]\.score', path)
                    key = ((checksum, format_key, member[1], date), path) if member else None
                    raw = members.get(key[0], {}).get(path) if key else None
                    if (key in bindings and raw is not None
                            and (_direction_name(value.name) in raw['names']
                                or value.code == raw['item'].code)
                            and value.value == raw['item'].value):
                        found.append((key, bindings[key]))
                signs = {direction for _, direction in found}
                if len(signs) == 1:
                    direction = next(iter(signs))
                    original = value.source_status
                    if not original:
                        value.source_status = direction
                    elif direction not in original:
                        value.source_status = original + '；原方向' + direction
                    for key, _ in found:
                        for origin in metadata[key]:
                            ref = public.SourceReference(source_code='C', record_id=origin['factId'],
                                observed_at=origin['observedAt'])
                            if ref not in value.sources:
                                value.sources.append(ref)
            for name in type(value).model_fields:
                if name != 'appendices':
                    visit(getattr(value, name))
    visit(data)
    return result


def _chapter_rows(code, shape, rows, facts):
    if code in {'healthOverview', 'comprehensiveConclusion'}:
        return deepcopy(rows)
    own = shape[code]
    ids, codes = set(_fact_ids(own, facts)), _codes(own)
    result = []
    for row in rows:
        source_codes = {source.get('sourceCode') for source in row.get('sources', [])}
        if code == 'medicalAssessment':
            include = bool(source_codes & {'A', 'B'})
        elif code == 'diseaseRiskAssessment':
            include = (row.get('kind') == 'disease' or row.get('category') == 'healthAssociation'
                or bool(set(row['sourceFactIds']) & ids))
        else:
            include = bool(set(row['sourceFactIds']) & ids) or (
                not row['sourceFactIds'] and str(row.get('code')) in codes
            )
        if include:
            result.append(deepcopy(row))
    return result


def _focus(data, document):
    problems = {problem.problem_id: problem for problem in document.problems}
    result = []
    for entry in selected_source_clinical_focus(data, document):
        reasons = set(entry['roleReason'])
        # Respect the source organizer's verified priority. A laboratory
        # observation is not promoted merely for being abnormal, but a real
        # priority finding must not be lost because it is not an image.
        primary = bool(reasons & {'explicit_primary_clinical_record',
            'positive_reported_symptom', 'source_organizer_primary'})
        if not primary:
            continue
        row = deepcopy(entry)
        row['searchTerms'] = list(dict.fromkeys(term for pid in entry['problemIds']
            if pid in problems for term in problems[pid].search_terms))
        row['aliases'] = list(dict.fromkeys([entry['name'], *row['searchTerms']]))
        result.append(row)
    return result


def _focus_slots(data, focus):
    medical = {row.code: row for row in data.medical_assessment.findings}
    data.health_overview.key_findings = []
    data.comprehensive_conclusion.priority_directions = []
    data.comprehensive_conclusion.next_steps = []
    for index, entry in enumerate(focus):
        row = medical.get(entry['code'])
        if row is None:
            continue
        sources = deepcopy(row.sources)
        data.health_overview.key_findings.append(public.OverviewFinding(rank=index+1,
            name=row.name, module=row.system, sources=sources, availability='partial'))
        data.comprehensive_conclusion.priority_directions.append(public.PriorityDirection(
            rank=index+1, name=row.name, sources=deepcopy(sources), availability='partial'))
        data.comprehensive_conclusion.next_steps.append(public.NextStep(code=row.code,
            name=row.name, sources=deepcopy(sources), availability='partial'))


def prepare_template_data(request, parsed, document, health_assessment=None):
    """Normalize source data once; editorial grouping happens before writing."""
    from .assessment_presentation import ASSESSMENT_TITLE

    data = project_evaluation_source_data(request, parsed, document, health_assessment, [])
    add_evaluation_source_facts(document, parsed, data)
    data = project_evaluation_source_data(request, parsed, document, health_assessment, [])
    public.materialize_clinical_problem_findings(data)
    data.introduction.title = ASSESSMENT_TITLE
    data.introduction.important_declarations = []
    data.health_overview.assessment_chart = [public.AssessmentScore(code=code, name=name)
        for code, name in public.catalog.RISK_CATEGORY_DEFINITIONS]
    data.disease_risk_assessment.overview_matrix = [public.RiskMatrixRow(code=code, name=name)
        for code, name in public.catalog.RISK_CATEGORY_DEFINITIONS]
    data.medical_assessment.findings = [row for row in data.medical_assessment.findings
        if row.certainty not in {'model_risk', 'source_association'}]
    return data


def _clinical_observation_groups(view, facts, document=None):
    """Inventory admitted observations for scope/coverage, not display selection."""
    if document is not None:
        from .assessment_clinical_observation import build_clinical_observation_registry

        return build_clinical_observation_registry(document, clinical_view=view, facts=facts)
    from .assessment_clinical_view import _owned_observation_ids
    from .assessment_clinical_view import clinical_observation_assessment

    groups = []
    inventories = [
        (group['code'], group['name'], panel)
        for group in view.get('clinicalGroups', ())
        for panel in group.get('templateRows', ())
    ]
    inventories.extend(('body', '体格与人体成分', group) for group in view.get('bodyGroups', ()))
    inventories.extend(('normalBaselines', '补充检查基线', group) for group in view.get('normalBaselines', ()))
    for parent_code, parent_name, panel in inventories:
        members, seen = [], set()
        for row in panel.get('rows', ()):
            role = row.get('_sourceRole')
            if role is not None and role not in {
                'direct_measurement', 'derived_measurement', 'exam_finding', 'physical_finding',
            }:
                continue
            owned = _owned_observation_ids(row)
            ids = [fid for fid in facts if fid in owned and facts[fid].get('kind') in {'clinical', 'derived'}]
            if not ids:
                continue
            identity = (tuple(ids), row.get('name'), str(row.get('value')), row.get('unit'),
                        row.get('referenceRange'), row.get('observedAt'))
            if identity in seen:
                continue
            seen.add(identity)
            members.append({
                'observationId': row.get('code'), 'recordId': ids[0], 'recordIds': ids,
                **{key: deepcopy(row.get(key)) for key in (
                    'name', 'value', 'unit', 'referenceRange', 'observedAt', 'sourceReportedAge',
                )},
                'status': row.get('templateStatus') or row.get('status'),
                **clinical_observation_assessment(row),
                'verifiedFinding': bool(row.get('_verifiedFinding')),
                'sourceRole': role,
            })
        if members:
            groups.append({
                'code': parent_code + '.' + panel['code'], 'name': panel['name'],
                'parentCode': parent_code, 'parentName': parent_name,
                'recordIds': list(dict.fromkeys(fid for member in members for fid in member['recordIds'])),
                'members': members,
            })
    return groups


def _activity_constraint_groups(observation_groups, clinical_actions):
    """Keep actual findings in exertional and structural examination domains.

    Domain identities are the clinical template's examination categories.
    They identify material to consider, not contraindications or prescriptions.
    """
    activity_panels = {'cardiac', 'arterial', 'cerebrovascular', 'spinalImaging', 'boneDensity'}
    result = []
    for group in observation_groups:
        if group['code'].rsplit('.', 1)[-1] not in activity_panels:
            continue
        observations = [member for member in group['members']
                        if member['assessmentState'] == 'abnormal' or member['verifiedFinding']]
        ids = list(dict.fromkeys(fid for member in observations for fid in member['recordIds']))
        if not ids:
            continue
        advice = [deepcopy(action) for action in clinical_actions if set(action['sourceFactIds']).intersection(ids)]
        result.append({
            'code': group['code'], 'name': group['name'], 'recordIds': ids,
            'adviceRecordIds': list(dict.fromkeys(fid for action in advice for fid in action['sourceFactIds'])),
            'observations': deepcopy(observations),
            'originalAdvice': advice,
            'phaseResponsibilities': ['current_start', 'progression', 'long_term_target'],
            'purpose': '以本域真实所见及原医嘱明确活动选择和递进时的个人考量，保留适用条件与原建议的直接性。',
        })
    return result


def build_report_blueprint(request, parsed, document, health_assessment=None, *, prepared_data=None):
    """Build one source-only data/owner snapshot before chapter authoring."""
    from .assessment_clinical_view import build_clinical_view
    from .assessment_microbiome_view import _PANEL_GROUPS
    from .assessment_microbiome_view import _number_key
    from .assessment_microbiome_view import _reference_key
    from .assessment_microbiome_view import _row as microbiome_source_row
    from .assessment_microbiome_view import _source_key
    from .assessment_microbiome_view import build_microbiome_view
    from .assessment_presentation import _planned_source_bindings
    from .assessment_presentation import _source_clinical_actions
    from .assessment_presentation import assessment_editorial_jobs
    from .assessment_presentation import assessment_publication_rows
    from .assessment_presentation import assessment_template_duty
    from .clinical_source_projection import PATIENT_ROLES
    from .clinical_source_projection import clinical_claim_context_dependencies
    from .clinical_source_projection import ensure_patient_projection_index
    from .clinical_source_projection import reported_temporal_context
    from .evaluation_builder import original_medical_advice_witnesses
    from .microbiome_source_semantics import assay_groups

    data = prepared_data if prepared_data is not None else prepare_template_data(
        request, parsed, document, health_assessment)
    patient_projections = ensure_patient_projection_index(document)
    data._clinical_patient_projections = patient_projections
    directions = _project_source_directions(data, document)
    from .evaluation_template_layout import normalize_template_source_states
    normalize_template_source_states(data)
    layout = getattr(data, '_template_layout', {})
    known_ids = {fact.fact_id for fact in document.facts}
    if layout.get('groups'):
        indexed = {row.code: row for row in data.medical_assessment.findings}
        focus = [{**entry, 'name': indexed[entry['code']].name,
            'factIds': list(dict.fromkeys(source.record_id for source in indexed[entry['code']].sources
                if source.record_id in known_ids)),
            'aliases': [indexed[entry['code']].name], 'searchTerms': [indexed[entry['code']].name]}
            for entry in layout['groups'] if entry.get('code') in indexed]
    else:
        focus = _focus(data, document)
        _focus_slots(data, focus)
    # The approved HTML has no source-description chapter or questionnaire
    # inventory. Empty behavior placeholders and old generic paragraphs are
    # not patient-facing content; the original facts remain in the document.
    for section in (data.medical_assessment, data.microbiome_assessment,
            data.disease_risk_assessment, data.nutrition_assessment, data.functional_pathways):
        section.description = ''
    data.medical_assessment.trigger_analysis = [row for row in data.medical_assessment.trigger_analysis
        if row.observations and row.sources]
    data.microbiome_assessment.key_microbial_signals.text = None
    data.microbiome_assessment.resistance_notice = ''
    data.nutrition_assessment.weak_synthesis_chart = []
    # One owned projection: delivery must not rebuild/rename grouped rows.
    wire = data.model_dump(mode='json', by_alias=True, exclude_none=False)
    facts = {fact.fact_id: fact.model_dump(mode='json', by_alias=True, exclude_none=False)
        for fact in document.facts}
    data._clinical_advice_witnesses_by_code = {
        group['code']: original_medical_advice_witnesses(document, [
            fid for fid in group.get('originalFactIds', group.get('sourceFactIds', ())) if fid in facts
        ]) for group in layout.get('groups', ()) if group.get('origin') == 'clinical_group'
    }
    rows = _source_rows(data, facts, document, directions, parsed=parsed)
    clinical_view = build_clinical_view(request, parsed, document, data)
    clinical_observation_groups = _clinical_observation_groups(clinical_view, facts, document)
    microbiome_view = build_microbiome_view(parsed, data, source_facts=document.facts)
    content_plan = getattr(data, '_assessment_content_plan', None)
    reading_rows = {}
    if isinstance(content_plan, dict):
        reader = build_clinical_view(request, parsed, document, data, reader_plan=True)
        clinical_view['specialtyFindings'] = reader['specialtyFindings']
        reading_rows = {
            'clinicalRow.' + group['code'] + '.' + row['code']: row
            for group in reader['clinicalGroups'] for row in group.get('templateRows', ())
        }
        reading_rows.update({'body.' + row['code']: row for row in reader.get('bodyGroups', ())})
    specialty_rows = {row['code']: row for row in clinical_view['specialtyFindings']}
    planned_bindings = _planned_source_bindings(
        clinical_view, microbiome_view, facts, content_plan, data=data,
    ) if isinstance(content_plan, dict) else {}
    editorial_jobs = list(assessment_editorial_jobs(clinical_view, microbiome_view, facts, data))
    publication_rows = assessment_publication_rows(clinical_view, microbiome_view)
    group_meaning_keys = {job[1] for job in editorial_jobs}
    microbial_group_meaning_prefixes = [
        'microbiome.' + field + '.' + group['code'] + '.'
        for field, prefix in (('structureGroups', 'structure.'), ('organismGroups', 'organisms.'),
                              ('functionalGroups', 'function.'))
        for group in microbiome_view.get(field, ())
        if prefix + group['code'] in group_meaning_keys
    ] if isinstance(content_plan, dict) else []
    clinical_group_meaning_prefixes = [
        'clinical.' + field + '.' + group['code'] + '.'
        for field, prefix in (('clinicalGroups', 'clinical.'), ('digestiveContext', 'digestive.'))
        for group in clinical_view.get(field, ())
        if prefix + group['code'] in group_meaning_keys
    ] if isinstance(content_plan, dict) else []
    microbial_action_ids = {
        fid for slot, row in publication_rows.items()
        if slot.startswith('microbiome.') and '.panelStates.' not in slot
        and row.get('code') not in _PANEL_GROUPS
        and (row.get('_sourceSemantics') or {}).get('objectScope') != 'panel'
        and (isinstance(_number_key(row.get('value')), Decimal)
             or row.get('detectionStatus') in {'detected', 'not_detected'})
        for fid in _fact_ids(row, facts) if facts[fid].get('kind') in {'microbe', 'function'}
    }

    def publication_slots(typed, own):
        if typed[0] == '_assessment_editorial':
            key = typed[1]
            named = {
                'bodySummary': 'overview.bodySummary', 'clinicalLead': 'clinical.summary',
                'symptomSummary': 'clinical.symptomSummary', 'specialtySummary': 'clinical.specialtySummary',
                'trendSummary': 'clinical.trendSummary', 'baselineSummary': 'clinical.baselineSummary',
                'microbiomeLead': 'microbiome.overviewSummary',
                'sampleSummary': 'microbiome.sampleSummary', 'fermentationSummary': 'microbiome.fermentationSummary',
                'riskSummary': 'microbiome.riskSummary', 'graphSummary': 'overview.graphSummary',
                'clusterSummary': 'clusterSummary', 'lifestyleSummary': 'lifestyleSummary',
            }
            if key in named:
                return [named[key]]
            prefix, _, suffix = key.partition('.')
            collections = {
                'body': ('clinical.bodyGroups', 'summary'),
                'clinical': ('clinical.clinicalGroups', 'summary'),
                'digestive': ('clinical.digestiveContext', 'summary'),
                'structure': ('microbiome.structureGroups', 'summary'),
                'organisms': ('microbiome.organismGroups', 'summary'),
                'function': ('microbiome.functionalGroups', 'summary'),
                'system': ('overview.systems', 'summary'),
                'symptom': ('clinical.symptoms', 'interpretation'),
                'lifestyle': ('lifestyleFactors', 'interpretation'),
                'recognition': ('lifestyleRecognition', 'text'),
                'layer': ('conclusion.layers', 'text'),
                'management': ('conclusion.managementDirections', 'text'),
                'priority': ('overview.priorityFindings', 'action'),
            }
            if prefix in collections:
                collection, field = collections[prefix]
                return [f'{collection}[code={suffix}].{field}']
            if prefix == 'clinicalRow':
                group_code, _, row_code = suffix.partition('.')
                return [f'clinical.clinicalGroups[code={group_code}].templateRows[code={row_code}].summary']
            if prefix == 'specialtyRow':
                return [f'clinical.specialtyFindings[code={suffix}].interpretation']
            if prefix == 'symptomEvidence':
                symptom_code, _, column = suffix.rpartition('.')
                return [f'clinical.symptoms[code={symptom_code}].{column}Signals[*].interpretation']
            if prefix in {'synthesis', 'synthesisAction'}:
                field = 'interpretation' if prefix == 'synthesis' else 'action'
                return [f'clusters[name={own["name"]}].{field}']
            return []
        aggregate_slots = {
            ('health_overview', 'overall_summary', 'text'): 'overview.summary',
            ('comprehensive_conclusion', 'overall_conclusion', 'text'): 'conclusion.summary',
            ('microbiome_assessment', 'structure_interpretation', 'text'): 'microbiome.structureSummary',
        }
        if slot := aggregate_slots.get(tuple(typed)):
            return [slot]
        if len(typed) != 4 or typed[-1] != 'explanation':
            return []
        primary = set(own['primaryFactIds'])
        if typed[:2] == ['medical_assessment', 'findings']:
            source = wire['medicalAssessment']['findings'][typed[2]]
            def name_key(value):
                return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()
            def consumes_clinical_meaning(slot):
                if any(slot.startswith(prefix) for prefix in clinical_group_meaning_prefixes):
                    return False
                if slot.startswith('clinical.normalBaselines.'):
                    return False  # The baseline inventory renders names, not another conclusion.
                if slot.startswith('clinical.bodyMetrics.') and clinical_view.get('bodyGroups'):
                    return False
                return True

            return [slot for slot, row in publication_rows.items() if slot.startswith('clinical.')
                    and consumes_clinical_meaning(slot)
                    and name_key(row.get('name')) == name_key(source.get('name'))
                    and str(row.get('value')) == str(source.get('value'))
                    and (row.get('unit') or '') == (source.get('unit') or '')
                    and primary.intersection(_fact_ids(row, facts))]
        if typed[0] == 'microbiome_assessment':
            source_item = data
            for part in typed[:-1]:
                source_item = source_item[part] if isinstance(part, int) else getattr(source_item, part)
            source_row = microbiome_source_row(source_item)

            def occurrence_ids(row):
                return {source['recordId'] for source in row.get('sources', ())
                        if re.fullmatch(r'parsed\.sourceTaxonOccurrences\[\d+\]', source.get('recordId') or '')}

            def consumes_interpretation(slot, row):
                # The planned table has one meaning owner per group. Its
                # members are source observations, not a second hidden set
                # of individually authored medical conclusions in JSON.
                if any(slot.startswith(prefix) for prefix in microbial_group_meaning_prefixes):
                    return False
                if (isinstance(content_plan, dict) and slot.startswith('microbiome.structure.')
                        and 'structureGroups' in microbiome_view):
                    return False
                # These rows intentionally expose a panel state only. Their
                # independently reviewed organisms.* cell owns the meaning.
                if slot.startswith('microbiome.organismGroups.') and row.get('code') in _PANEL_GROUPS:
                    return False
                if row.get('code') == own.get('code') and primary.intersection(_fact_ids(row, facts)):
                    return True
                if typed[1] != 'taxa' or not source_row or not occurrence_ids(row).intersection(
                    occurrence_ids(source_row),
                ):
                    return False
                # _raw_group_members transfers canonical prose to an original
                # occurrence before _merge_observation_rows retains its own
                # code. Match that exact physical occurrence and comparison,
                # rather than using canonical code or arbitrary support refs.
                return (
                    _number_key(row.get('value')) == _number_key(source_row.get('value'))
                    and _source_key(row.get('unit')) == _source_key(source_row.get('unit'))
                    and _reference_key(row.get('referenceRange')) == _reference_key(source_row.get('referenceRange'))
                    and _source_key(row.get('status')) == _source_key(source_row.get('status'))
                )

            return [slot for slot, row in publication_rows.items()
                    if slot.startswith('microbiome.') and consumes_interpretation(slot, row)]
        return []

    chapters = []
    clinical_actions = _source_clinical_actions(data, facts, projections=patient_projections)
    activity_constraint_groups = _activity_constraint_groups(clinical_observation_groups, clinical_actions)
    actions_by_code = {row['code']: row for row in clinical_actions}
    advice_by_code = {row['code']: row['originalAdvice'] for row in clinical_actions}
    for entry in focus:
        entry['originalAdvice'] = deepcopy(advice_by_code.get(entry.get('code'), []))
    verified_diagnoses = [group['name'] for group in layout.get('groups', ())
        if group.get('origin') == 'clinical_group' and group.get('certainty') == 'confirmed']
    planned_clusters = content_plan.get('clusters', ()) if isinstance(content_plan, dict) else ()
    clustered_problems = {item for row in planned_clusters for item in row.get('problemCodes', ())}
    personal_action_ids = {
        fid for section in ('lifestyleFactors', 'symptoms')
        for row in (content_plan or {}).get(section, ()) for fid in row.get('factIds', ()) if fid in facts
    }

    def temporal_relations(identifiers):
        clinical = [fid for fid in identifiers if facts[fid]['kind'] == 'clinical']
        result = []
        roles = getattr(document, '_clinical_reported_source_roles', {})
        for fid in identifiers:
            if facts[fid]['kind'] != 'self_reported':
                continue
            temporal = reported_temporal_context(facts[fid], reported_role=roles.get(fid))
            if temporal.get('kind') != 'retrospective_event':
                continue
            event_id = temporal.get('eventId')
            linked = [other for other in clinical if event_id and event_id == (
                patient_projections.get(other, {}).get('sourceContext') or {}
            ).get('eventId')]
            for established, related in ((False, [other for other in clinical if other not in linked]),
                                         (True, linked)):
                if not related and (established or linked):
                    continue
                result.append({
                    'recordId': fid, 'relatedRecordIds': related,
                    'kind': 'same_event' if established else 'clinical_context',
                    'scopeQuote': temporal.get('scopeQuote'), 'eventDate': temporal.get('eventDate'),
                    'eventId': event_id, 'currentStateConfirmed': temporal.get('currentStateConfirmed', False),
                    'eventLinkEstablished': established,
                    'eventLinkStatus': 'established' if established else 'unestablished',
                    'clinicalCooccurrenceConfirmed': established,
                })
        return result

    def action_group_matches(group, scope_code, primary):
        identifiers = group.get('originalFactIds', group.get('sourceFactIds', ()))
        if not set(primary).intersection(identifiers):
            return False
        if scope_code == 'priority.routine':
            return group.get('code') not in clustered_problems and group.get('focusLabel') != '优先查因'
        if scope_code == 'priority.urgent':
            urgent_problems = {code for row in planned_clusters if row.get('priority') == 'P0'
                               for code in row.get('problemCodes', ())}
            return (group.get('code') in urgent_problems if planned_clusters
                    else group.get('focusLabel') == '优先查因')
        if scope_code == 'priority.planned':
            managed_problems = {code for row in planned_clusters if row.get('priority') in {'P1', 'P2'}
                                for code in row.get('problemCodes', ())}
            return group.get('code') in managed_problems
        if scope_code and scope_code.startswith('synthesisAction.'):
            cluster_code = scope_code.partition('.')[2]
            cluster = next((row for row in planned_clusters if row['code'] == cluster_code), {})
            return group.get('code') in cluster.get('problemCodes', ())
        return True

    def owner(kind, row, *, category=None, role='observation'):
        ids = _fact_ids(row, facts)
        return {'kind': kind, 'code': row.get('code'), 'name': row.get('name'),
            'certainty': row.get('certainty'), 'sourceKinds': list(dict.fromkeys(
                facts[fid]['kind'] for fid in ids)),
            'measurementType': ('source_model_or_association' if kind == 'disease_source_model'
                else 'source_observation'), 'category': category,
            'originalAdvice': list(advice_by_code.get(row.get('code'), [])),
            **({key: deepcopy(actions_by_code[row['code']][key])
                for key in ('conditionality', 'noNewPrerequisite', 'sourceAdviceScopes', 'unscopedAdvice')
                if key in actions_by_code[row['code']]}
               if row.get('code') in actions_by_code else {}),
            'requiredRole': role}

    for number, (code, title, purpose) in enumerate(_CHAPTERS, 1):
        source_rows = _chapter_rows(code, wire, rows, facts)
        chapter_ids = list(dict.fromkeys(fid for row in source_rows for fid in row['sourceFactIds']))
        chapter = {'code': code, 'title': title, 'number': number, 'purpose': purpose,
            'targets': [], 'sourceRows': source_rows}

        def add(path, own=None, ids=None, *, required=True, duty='assessment', editorial=None):
            if editorial:
                value, typed = None, path
            else:
                try:
                    value, typed = _wire_get(wire, path), _typed_path(data, path)
                except (KeyError, IndexError, TypeError):
                    return
            if typed[-1] not in _WRITABLE or value is not None and not isinstance(value, str):
                return
            primary = list(dict.fromkeys(chapter_ids if ids is None else ids))
            primary = [fid for fid in primary if fid in facts]
            if not primary:
                return
            own = deepcopy(own or {'kind': 'summary', 'code': code, 'name': title,
                'aggregate': True, 'measurementType': 'integrated_source_context'})
            own['presentation'] = 'assessment.v1'
            own['verifiedDiagnoses'] = verified_diagnoses
            own['primaryFactIds'] = primary
            own['requiredRole'] = 'action' if duty == 'management' else 'interpretation'
            own['requiresScientificMeaning'] = True
            scope_code = path[1] if editorial else {
                ('health_overview', 'overall_summary', 'text'): 'overviewSummary',
                ('comprehensive_conclusion', 'overall_conclusion', 'text'): 'conclusionSummary',
                ('microbiome_assessment', 'structure_interpretation', 'text'): 'structureSummary',
            }.get(tuple(typed))
            if scope_code:
                own['contentScopeCode'] = scope_code
            priority = 0 if scope_code == 'priority.urgent' else 2
            local_editorial = bool(editorial and path[1].startswith((
                'function.', 'organisms.', 'structure.', 'digestive.', 'clinicalRow.', 'specialtyRow.', 'system.',
            )))
            if scope_code and scope_code.startswith('specialtyRow.'):
                source_row = specialty_rows[scope_code.partition('.')[2]]
                own['sourceRow'] = {field: source_row[field] for field in
                    ('name', 'value', 'unit', 'referenceRange', 'templateStatus', 'observedAt')
                    if source_row.get(field) is not None}
            elif scope_code in reading_rows:
                displayed = reading_rows[scope_code]
                own['sourceRow'] = {
                    field: displayed[field] for field in ('name', 'status', 'comparisonReference')
                    if displayed.get(field)
                }
                if displayed.get('_comparisonClaims'):
                    own['comparisonClaims'] = deepcopy(displayed['_comparisonClaims'])
            if code in {'nutritionAssessment', 'functionalPathways', 'microbiomeAssessment'}:
                own['managementContextFactIds'] = list(dict.fromkeys(
                    fid for group in layout.get('groups', ())
                    if group.get('origin') == 'clinical_group'
                    and group.get('category') in {'intestinal', 'metabolic'}
                    for fid in group.get('sourceFactIds', ()) if fid in facts))
            # One patient's materials are shared interpretation context. A
            # chapter is an editorial responsibility, not a source-type fence:
            # relevant gut/function or other-test evidence may support a
            # clinical paragraph. Primary owner anchors and per-claim review
            # still prevent unrelated facts from replacing this row's result.
            if isinstance(content_plan, dict):
                scoped = next((scope for scope in content_plan.get('cellScopes', ())
                    if scope.get('code') == scope_code), planned_bindings.get(scope_code, {}))
                if scoped:
                    primary = [fid for fid in scoped.get('factIds', ()) if fid in facts]
                    own['primaryFactIds'] = list(dict.fromkeys(primary))
                context = [fid for fid in scoped.get('contextFactIds', ()) if fid in facts]
                bound = list(dict.fromkeys([*primary, *context]))
                own['contextFactIds'] = context
                own['contentPlanVersion'] = content_plan.get('version')
                # A planner scope keeps its own purpose; program-decided content
                # of the same slot (1.3 items, body-type elements) is appended.
                program_purpose = planned_bindings.get(scope_code, {}).get('contextPurpose')
                own['contextPurpose'] = '\n'.join(dict.fromkeys(filter(None, (
                    scoped.get('contextPurpose'), program_purpose,
                )))) or None
                own['cellRelations'] = deepcopy(scoped.get('relations') or [])
                if scope_code == 'digestive.lowerDigestive':
                    measured = [fid for fid in primary if facts[fid]['kind'] == 'clinical']
                    if measured:
                        own['requiredFactGroups'] = [measured]
                if editorial and scope_code and scope_code.startswith('lifestyle.'):
                    lifestyle_code = scope_code.partition('.')[2]
                    factor = next((row for row in content_plan.get('lifestyleFactors', ())
                                   if row['code'] == lifestyle_code), {})
                    own['sourceSelections'] = deepcopy(factor.get('sourceSelections') or [])
                if editorial and path[1].startswith('symptomEvidence.'):
                    stem, column = path[1].rsplit('.', 1)
                    symptom_code = stem.removeprefix('symptomEvidence.')
                    symptom = next((row for row in content_plan.get('symptoms', ())
                                    if row['code'] == symptom_code), {})
                    own['requiredFactGroups'] = [list(symptom.get('factIds', ())),
                                                list(symptom.get(column + 'FactIds', ()))]
                    own['clinicalSelections'] = deepcopy(symptom.get('clinicalSelections') or [])
                elif editorial and path[1].startswith('symptom.'):
                    symptom_code = path[1].partition('.')[2]
                    symptom = next((row for row in content_plan.get('symptoms', ())
                                    if row['code'] == symptom_code), {})
                    own['clinicalSelections'] = deepcopy(symptom.get('clinicalSelections') or [])
                if editorial and path[1].startswith(('synthesis.', 'synthesisAction.')):
                    cluster_code = path[1].partition('.')[2]
                    cluster = next((row for row in content_plan.get('clusters', ())
                                    if row['code'] == cluster_code), {})
                    if cluster.get('priority') == 'P0':
                        priority = 0
                    own['clinicalSelections'] = deepcopy(cluster.get('clinicalSelections') or [])
                    if path[1].startswith('synthesisAction.'):
                        advice_ids = {fid for action in clinical_actions
                            if action['code'] in cluster.get('problemCodes', ()) for fid in action['sourceFactIds']}
                        action_basis = [fid for fid in primary if (
                            fid in personal_action_ids or fid in advice_ids or any(
                                item.get('role') in PATIENT_ROLES and item.get('role') != 'reported_behavior'
                                for item in patient_projections.get(fid, {}).get('patientRows', ())
                            )
                        )]
                        own['actionBasisFactIds'] = action_basis
                        own['actionBasisScope'] = 'personal_clinical_or_behavior'
                        own['actionSourceDuty'] = 'personal_clinical_or_behavior_or_original_advice'
                        if not action_basis:
                            action_basis = [fid for fid in primary if fid in microbial_action_ids]
                            own['actionBasisFactIds'] = action_basis
                            own['actionBasisScope'] = 'microbial_support_only' if action_basis else 'insufficient_basis'
                            own['actionSourceDuty'] = (
                                'evidence_supported_microbial_and_qualitative_dietary_support_only'
                                if action_basis else 'insufficient_source_basis'
                            )
                        own['requiredFactGroups'] = [action_basis] if action_basis else []
                    else:
                        own['requiredFactGroups'] = [
                            list(cluster[field]) for field in ('clinicalFactIds', 'microbiomeFactIds')
                            if cluster.get(field)
                        ]
                if scope_code in {'overviewSummary', 'conclusionSummary', 'layer.clinical', 'symptomSummary',
                                  'clusterSummary', 'graphSummary'} or (
                    scope_code and scope_code.startswith((
                        'priority.', 'synthesis.', 'synthesisAction.', 'clinical.', 'clinicalRow.', 'digestive.',
                        'system.', 'symptom.', 'symptomEvidence.',
                    ))
                ):
                    own['sourceClinicalActions'] = [
                        deepcopy(actions_by_code[group['code']])
                        for group in layout.get('groups', ())
                        if group.get('code') in actions_by_code
                        and action_group_matches(group, scope_code, [*primary, *context])
                    ]
                    advice_ids = [
                        fid for action in own['sourceClinicalActions']
                        for span in action.get('sourceAdviceScopes', ())
                        if span.get('scopeStatus') in {'explicit_object', 'source_record_scope'}
                        for fid in span.get('sourceFactIds', ()) if fid in facts and fid not in primary
                    ]
                    own['contextFactIds'] = list(dict.fromkeys([*own['contextFactIds'], *advice_ids]))
                    bound = list(dict.fromkeys([*bound, *advice_ids]))
                integrated_action = scope_code and (
                    scope_code in {'priority.planned', 'management.lifestyle'}
                    or scope_code.startswith(('layer.', 'synthesisAction.'))
                )
                if duty == 'management' or integrated_action:
                    baselines = list(dict.fromkeys(fid for row in content_plan.get('lifestyleFactors', ())
                        for fid in row.get('factIds', ()) if fid in facts)) if integrated_action else []
                    symptoms = list(dict.fromkeys(fid for row in content_plan.get('symptoms', ())
                        for fid in row.get('factIds', ()) if fid in facts)) if integrated_action else []
                    constraints = list(dict.fromkeys(
                        fid for group in layout.get('groups', ())
                        if group.get('origin') == 'clinical_group'
                        and group.get('role') in {'health_problem', 'secondary_finding'}
                        for fid in group.get('originalFactIds', group.get('sourceFactIds', ()))
                        if fid in facts and any(row.get('role') in PATIENT_ROLES
                            and row.get('role') != 'reported_behavior'
                            for row in patient_projections.get(fid, {}).get('patientRows', ()))
                    )) if integrated_action else []
                    own['contextFactIds'] = list(dict.fromkeys([
                        *own.get('contextFactIds', ()),
                        *(fid for fid in [*baselines, *symptoms, *constraints] if fid not in primary),
                    ]))
                    bound = list(dict.fromkeys([*primary, *own['contextFactIds']]))
                    own['actionContext'] = {
                        'baselineFactIds': baselines, 'symptomFactIds': symptoms,
                        'clinicalConstraintFactIds': constraints,
                        'originalAdvice': deepcopy(own.get('sourceClinicalActions') or [
                            action for action in clinical_actions
                            if set(action['sourceFactIds']).intersection(bound)
                        ]),
                        'phaseResponsibilities': [
                            'clinical_assessment', 'current_start', 'progression', 'long_term_target', 'review',
                        ],
                    }
                    if scope_code == 'management.lifestyle' and activity_constraint_groups:
                        own['actionContext']['activityConstraintGroups'] = deepcopy(activity_constraint_groups)
                        groups = [list(group['recordIds']) for group in activity_constraint_groups]
                        own.setdefault('requiredFactGroups', []).extend(groups)
                        additional = list(dict.fromkeys(fid for group in groups for fid in group if fid not in primary))
                        own['contextFactIds'] = list(dict.fromkeys([*own['contextFactIds'], *additional]))
                        own['actionContext']['clinicalConstraintFactIds'] = list(dict.fromkeys([
                            *own['actionContext']['clinicalConstraintFactIds'], *additional,
                        ]))
                        bound = list(dict.fromkeys([*primary, *own['contextFactIds']]))
                if editorial:
                    prefix, _, group_code = path[1].partition('.')
                    semantic_groups = {'structure': microbiome_view.get('structureGroups', ()),
                        'organisms': microbiome_view.get('organismGroups', ()),
                        'function': microbiome_view.get('functionalGroups', ())}
                    group = next((row for row in semantic_groups.get(prefix, ())
                                  if row['code'] == group_code), {})
                    if group.get('_sourceSemantics'):
                        own['sourceSemantics'] = deepcopy(group['_sourceSemantics'])
            else:
                bound = primary if local_editorial else list(dict.fromkeys([*primary, *facts]))
            if not isinstance(content_plan, dict) and editorial and path[1].startswith('function.'):
                # Only already linked patient records can supplement a functional result.
                related = (
                    fid for group in layout.get('groups', ())
                    if group.get('origin') == 'clinical_group'
                    and set(primary).intersection(group.get('riskContextFactIds', ()))
                    for fid in group.get('originalFactIds', ())
                    if fid in facts and facts[fid]['kind'] in {'clinical', 'self_reported'}
                )
                bound = list(dict.fromkeys([*primary, *related]))
            # The selected anchors stay primary. Only a verified, direct
            # clinician-claim/measurement dependency can widen the final
            # context scope; every branch above reaches this single point.
            claim_context = clinical_claim_context_dependencies(
                bound, projection_index=patient_projections, facts=facts,
            )
            if claim_context:
                own['contextFactIds'] = list(dict.fromkeys([
                    *own.get('contextFactIds', ()), *claim_context,
                ]))
                bound = list(dict.fromkeys([*bound, *claim_context]))
            if isinstance(content_plan, dict):
                own['temporalRelations'] = temporal_relations(bound)
            # Source requirements must be reachable within this job's actual
            # scope. An impossible contract cannot be repaired by redrafting
            # prose or inventing citations, so reject it at plan construction.
            for group in own.get('requiredFactGroups', ()):
                if group and not set(group).intersection(bound):
                    raise ValueError(
                        f'report source scope excludes a required observation group: {scope_code or typed}'
                    )
            # Semantic cells have no literal-source fallback. Their previous
            # catalog/extraction explanation is input context, not a finished
            # patient-facing interpretation.
            if editorial:
                data._assessment_editorial[path[1]] = public.TextAssessment()
                contract = editorial
            else:
                parent = data
                for part in typed[:-1]:
                    parent = parent[part] if isinstance(part, int) else getattr(parent, part)
                setattr(parent, typed[-1], None)
                contract = cell_contract(path)
            slots = publication_slots(typed, own) if isinstance(content_plan, dict) else []
            if (isinstance(content_plan, dict) and len(typed) == 4 and typed[-1] == 'explanation' and not slots
                    and (typed[0] == 'microbiome_assessment' or typed[:2] == ['medical_assessment', 'findings'])):
                # Preserve the confirmed field as None. Source-only inventory
                # members have no independent reader duty and therefore no
                # optional authoring job that could repopulate hidden prose.
                observation_paths = list(getattr(data, '_assessment_observation_only_paths', ()))
                observation_paths.append(tuple(typed))
                data._assessment_observation_only_paths = observation_paths
                return
            publication_scope = (
                'assessment' if slots else 'compatibility'
            ) if isinstance(content_plan, dict) else 'assessment'
            if publication_scope == 'assessment' and isinstance(content_plan, dict):
                duty_key = scope_code or ('microbiomeRow' if typed[0] == 'microbiome_assessment' else 'clinicalRow')
                duty_text = assessment_template_duty(duty_key or 'clinicalRow')
                function_duty = microbiome_view.get('_functionCellDuties', {}).get(scope_code)
                if function_duty:
                    # A reader's question contains the positive clinical
                    # duty, not internal chapter hand-off instructions.
                    duty_text = ' '.join(function_duty[field] for field in (
                        'definition', 'currentPattern',
                    ) if function_duty.get(field))
                    duty_text += (' 给出本组最能区分偏离与正常分项的一条判断。'
                                  '仅当其他检测会实际改变此判断且有直接依据时提及。')
                own['templateDuty'] = duty_text
                if sample := microbiome_view.get('sampleContext'):
                    own['microbiomeDates'] = {field: sample.get(field)
                                            for field in ('sampleDate', 'receivedDate', 'reportDate')}
                if duty_key in {
                    'overviewSummary', 'conclusionSummary', 'bodySummary', 'graphSummary', 'clinicalLead',
                    'symptomSummary', 'specialtySummary', 'structureSummary', 'microbiomeLead',
                    'riskSummary', 'clusterSummary', 'digestive.lowerDigestive',
                } or duty_key.startswith(('body.', 'system.', 'symptomEvidence.', 'specialtyRow.', 'priority.')):
                    own['usesReviewedFactsOnly'] = True
                if (duty_key.startswith('clinicalRow.') and duty_key.endswith('.stool')
                        or duty_key.startswith(('synthesis.', 'synthesisAction.')) and priority == 0):
                    own['usesReviewedFactsOnly'] = True
                if scope_code and (scope_code.startswith(('priority.', 'synthesisAction.'))
                                   or scope_code == 'management.lifestyle'):
                    own['requiresPersonalAction'] = own.get('actionBasisScope') != 'insufficient_basis'
                own.pop('editorialPurpose', None)
                own.pop('patientTask', None)
                contract = {
                    'label': contract['label'], 'form': 'paragraph', 'question': duty_text,
                    'structure': ['形成与本格职责相符的个人判断', '说明有据的健康意义及本格负责的关注方向'],
                    'maxChars': contract.get('maxChars', 220),
                }
                if len(typed) == 4 and typed[-1] == 'explanation':
                    required = True
            if (not (isinstance(content_plan, dict) and publication_scope == 'assessment')
                    and (topic := topic_guidance(own))):
                contract['topicFocus'] = topic
            if own.get('patientTask'):
                contract['patientTask'] = own['patientTask']
            if own.get('kind') == 'behavior' and contract.get('label') == '与当前健康问题的关联分析':
                contract.update(
                    question='已记录的实际习惯与哪些具体健康问题直接相关，对管理有何意义？',
                    structure=['说明最直接的健康联系及影响，执行方案集中在第六章'],
                    maxChars=105,
                )
            if own.get('analysisMode') == 'management_direction':
                contract['analysisMode'] = 'management_direction'
                contract['structure'] = ['直接给本项相称的管理或预防行动', '需要时说明目的和执行条件']
            if contract.get('allowedValues'):
                duty = 'classification'
            identity = json.dumps([typed, own.get('kind'), own.get('code'), primary],
                ensure_ascii=False, separators=(',', ':'))
            if isinstance(content_plan, dict) and publication_scope == 'assessment':
                # Rule conclusions that matched inside this cell's scope are a
                # citable source (R) for writing and source review.
                rule_conclusions = cell_rule_conclusions(
                    scope_code, content_plan, comparison_claims=own.get('comparisonClaims') or (),
                    scope_fact_ids=bound,
                )
                if rule_conclusions:
                    own['ruleConclusions'] = rule_conclusions
            allowed_roles = ['observation', 'interpretation', 'action']
            if own.get('contentPlanVersion') and (scope_code or '').startswith(
                ('body.', 'function.', 'organisms.', 'structure.', 'system.', 'specialtyRow.'),
            ):
                allowed_roles.remove('action')
            target = {'id': 'report:' + hashlib.sha256(identity.encode()).hexdigest()[:24],
                'path': typed, 'purpose': contract['question'], 'owner': own, 'sourceFactIds': bound,
                'required': bool(required), 'allowedRoles': allowed_roles,
                'contentDuty': duty, 'sourceOnlyComplete': False, 'cellContract': contract,
                'authoringPriority': priority,
                'publicationScope': publication_scope, 'publicationSlots': slots}
            if publication_scope == 'assessment' and isinstance(content_plan, dict):
                _seed_original_action(target)
                _seed_source_observations(target, facts, microbiome_view.get('sampleContext') or {})
            chapter['targets'].append(target)

        for section, key, name, question, ids, compact in editorial_jobs:
            if section != code:
                continue
            planned_required = isinstance(getattr(data, '_assessment_content_plan', None), dict) and (
                key.startswith(('body.', 'clinical.', 'clinicalRow.', 'specialtyRow.', 'digestive.',
                    'structure.', 'organisms.', 'function.', 'system.', 'symptom.', 'symptomEvidence.',
                    'synthesis.', 'synthesisAction.',
                    'lifestyle.', 'recognition.', 'layer.', 'priority.', 'management.'))
                or key in {'clinicalLead', 'bodySummary', 'symptomSummary', 'microbiomeLead', 'riskSummary',
                    'clusterSummary', 'graphSummary', 'lifestyleSummary', 'specialtySummary',
                    'trendSummary', 'baselineSummary', 'sampleSummary', 'fermentationSummary'}
            )
            add(['_assessment_editorial', key, 'text'],
                {'kind': 'assessment_editorial', 'code': key, 'name': name, 'aggregate': True}, ids,
                required=planned_required or key.startswith('clinicalRow.') or key == 'management.lifestyle',
                duty='management' if key.startswith(('management.', 'priority.', 'synthesisAction.', 'layer.'))
                else 'assessment',
                editorial={'label': name, 'form': 'paragraph', 'question': question,
                    'maxChars': 95 if compact else 220})

        if code == 'healthOverview':
            add([code, 'overallSummary', 'text'],
                ids=planned_bindings.get('overviewSummary', {}).get('factIds', [])
                if isinstance(content_plan, dict) else None)
            add([code, 'overallScore', 'explanation'])
            for domain in ('medical', 'microbiome', 'nutrition'):
                domain_code = {'medical':'medicalAssessment', 'microbiome':'microbiomeAssessment',
                    'nutrition':'nutritionAssessment'}[domain]
                ids = _fact_ids(wire[domain_code], facts)
                if ids:
                    add([code, 'summaries', domain, 'text'], ids=ids)
            # The confirmed radar displays domain names and scores only.
            # Domain interpretations have their visible matrix/category cells;
            # hidden duplicate paragraphs are not report writing targets.
            for i, row in enumerate(wire[code]['keyFindings']):
                add([code, 'keyFindings', i, 'summary'],
                    {'kind': 'summary', 'code': f'focus:{i}', 'name': row['name'], 'aggregate': True},
                    _fact_ids(row, facts))
        elif code == 'medicalAssessment':
            clinical_owners = {group.get('code'): group for group in layout.get('groups', ())}
            for i, row in enumerate(wire[code]['findings']):
                if _result(row):
                    own = owner('clinical', row, role='interpretation')
                    group = clinical_owners.get(row.get('code'), {})
                    own['clinicalRole'] = group.get('role')
                    if group.get('role') == 'secondary_finding':
                        own['analysisMode'] = 'management_direction'
                        own['patientTask'] = (
                            '轻微或次要观察只给相称维护/处理行动，不展开新的疾病推断、人体功能排除或正常项目清单。'
                        )
                    add([code, 'findings', i, 'explanation'], own, _fact_ids(row, facts))
                    add([code, 'findings', i, 'sourceAdvice'], own, _fact_ids(row, facts), duty='management')
            for i, row in enumerate(wire[code]['questionnaireSummary']):
                ids = _fact_ids(row, facts)
                if ids:
                    own = owner('symptom', row, role='interpretation')
                    own['patientTask'] = (
                        '先说明这个人的已知感受和与它直接有关的实际线索，避免检查清单。'
                        '只使用会改变判断的事实，最多三条。正常检查不作为排除病因的叙述开头。'
                        '本人记录的时间、频次和情境只按实际回答表述。缺直接证据时给一项可观察的现象。'
                    )
                    add([code, 'questionnaireSummary', i, 'explanation'], own, ids, required=False)
            for i, row in enumerate(wire[code]['triggerAnalysis']):
                ids = _fact_ids(row, facts)
                if ids and row.get('observations'):
                    own = owner('behavior', row, role='interpretation')
                    for field in ('association', 'evidenceStrength', 'modifiability'):
                        add([code, 'triggerAnalysis', i, field], own, ids)
            add([code, 'triggerConclusion', 'text'], ids=_fact_ids(wire[code]['triggerAnalysis'], facts),
                required=bool(wire[code]['triggerAnalysis']))
        elif code == 'microbiomeAssessment':
            for collection in ('overallStructure', 'healthMetrics', 'antimicrobialResistance'):
                for i, row in enumerate(wire[code][collection]):
                    if collection == 'overallStructure' and row.get('code') == 'beRatio' and _result(row):
                        # B/E is a composition ratio. The source's reference comparison is shown
                        # in its result column. The ratio alone cannot establish clinical function.
                        data.microbiome_assessment.overall_structure[i].explanation = None if isinstance(
                            content_plan, dict,
                        ) else (
                            'B/E 比值反映肠道菌群中双歧杆菌属与肠杆菌科的相对构成。'
                        )
                        continue
                    if _result(row):
                        previous_count = len(chapter['targets'])
                        add([code, collection, i, 'explanation'],
                            owner('microbiome', row, category=collection, role='interpretation'), _fact_ids(row, facts))
                        if len(chapter['targets']) == previous_count:
                            continue
                        if row.get('code') == 'virulenceGenes' and not isinstance(content_plan, dict):
                            _seed_virulence_observation(
                                chapter['targets'][-1], row, layout.get('geneDetections', ()))
                        if row.get('code') == 'barrierRelatedBacteria' and not isinstance(content_plan, dict):
                            _seed_barrier_observation(chapter['targets'][-1], row, microbiome_view, facts)
            _bind_organism_panels(chapter, data, parsed, microbiome_view, facts)
            # Legacy row layouts can own a taxon's interpretation. Planned
            # grouped layouts resolve ownership in add(): their group cell
            # explains the actual pattern and member rows remain observations.
            seen_taxa = set()
            for i, row in enumerate(wire[code]['taxa']):
                state = str(row.get('sourceStatus') or row.get('assessmentLevel') or '')
                if not re.search(r'偏[高低]|过[高低]|极[高低]|超标|缺乏|不足|异常', state):
                    continue
                identity = unicodedata.normalize(
                    'NFKC', str(row.get('scientificName') or row.get('name') or ''),
                ).casefold()
                ids = _fact_ids(row, facts)
                if not identity or not ids or identity in seen_taxa:
                    continue
                seen_taxa.add(identity)
                own = owner('abnormal_flora', row, category='taxon', role='interpretation')
                own['scientificName'] = row.get('scientificName')
                own['aliases'] = [name for name in (row.get('name'), row.get('scientificName')) if name]
                add([code, 'taxa', i, 'explanation'], own, ids, required=False)
            for field, collection in (('structureInterpretation', 'overallStructure'),
                    ('keyMicrobialSignals', 'healthMetrics'), ('scfaInterpretation', 'shortChainFattyAcids')):
                ids = _fact_ids(wire[code][collection], facts)
                if field == 'structureInterpretation' and isinstance(content_plan, dict):
                    add([code, field, 'text'], ids=planned_bindings.get('structureSummary', {}).get('factIds', []))
                    continue
                if field == 'structureInterpretation':
                    structural_codes = {'balanceStatus', 'diversity', 'gmwi2', 'barrierRelatedBacteria'}
                    measurements = [
                        row for group in microbiome_view.get('structureGroups', ()) for row in group['rows']
                        if row['code'] in structural_codes
                    ]
                    sentences = []
                    seen = set()
                    for row in measurements:
                        state = str((row['value'] if row['code'] == 'balanceStatus' else row['status']) or '').strip()
                        anchors = [ref['recordId'] for ref in row['sources'] if ref['recordId'] in facts]
                        if not (state and anchors and row['name']) or (row['name'], state) in seen:
                            continue
                        seen.add((row['name'], state))
                        sentences.append({
                            'text': f'{row["name"]}为{state}。',
                            'factIds': list(dict.fromkeys(anchors)),
                            'role': 'observation', 'evidence': [], 'support': 'source',
                        })
                    if sentences:
                        ids = list(dict.fromkeys([
                            *ids, *(fid for sentence in sentences for fid in sentence['factIds']),
                        ]))
                        count = len(chapter['targets'])
                        add([code, field, 'text'], ids=ids)
                        if len(chapter['targets']) > count:
                            target = chapter['targets'][-1]
                            target['sourceBlock'] = {
                                'targetId': target['id'], 'completion': 'complete',
                                'analysisGenerated': False, 'sentences': sentences,
                            }
                        continue
                add([code, field, 'text'], ids=ids, required=bool(ids))
        elif code == 'diseaseRiskAssessment':
            risk_owners = {group.get('riskCode'): group for group in layout.get('groups', ())}
            for i, row in enumerate(wire[code]['riskRanking']):
                group = risk_owners.get(row.get('code'), {})
                is_clinical = group.get('origin') == 'clinical_group'
                own = owner('clinical' if is_clinical else 'disease_source_model', row, role='interpretation')
                own['certainty'] = group.get('certainty')
                if not is_clinical or group.get('certainty') in {'potential_risk', 'suspected'}:
                    own['analysisMode'] = 'management_direction'
                    own['patientTask'] = (
                        '本行是预防、筛查或症状识别任务，不是本人已患此病。直接给适用行动，'
                        '不把疾病名称写成本人当前疾病，也不用未确诊等免责声明占据段落。'
                    )
                own['sourceOwnerCodes'] = [value for value in [group.get('code'),
                    *group.get('nativeCodes', ())] if value]
                add([code, 'riskRanking', i, 'explanation'], own, _fact_ids(row, facts))
            for i, category in enumerate(wire[code]['categories']):
                relevant = [row for row in wire[code]['riskRanking']
                    if (':' + category['code'] + ':') in str(row.get('code', ''))]
                # A category summary owns its selected personal problems and
                # relevant clinical follow-up context, not an unrelated raw
                # vendor cancer panel when the selected result frame is empty.
                context_codes = {group.get('code') for group in layout.get('groups', ())
                    if group.get('category') == category['code']
                    and group.get('origin') == 'clinical_group'
                    and group.get('role') not in {'normal_screening', 'management_goal'}}
                context = [row for row in wire['medicalAssessment']['findings']
                    if row.get('code') in context_codes]
                sources = relevant + context
                ids = _fact_ids(sources, facts)
                if not ids:
                    ids = _fact_ids(category, facts)
                clinical_groups = [group for group in layout.get('groups', ())
                    if group.get('category') == category['code']
                    and group.get('origin') == 'clinical_group'
                    and group.get('role') not in {'normal_screening', 'management_goal'}]
                actual_names = [group['name'] for group in clinical_groups
                    if group.get('certainty') in {'confirmed', 'current_problem', 'self_reported'}]
                own = {'kind': 'summary', 'code': category['code'], 'name': category['name'],
                    'aggregate': True, 'personalProblemCount': len(relevant),
                    'clinicalContextCodes': list(context_codes),
                    'actualPersonalProblems': actual_names,
                    'summaryMode': 'personal_management' if actual_names else 'routine_maintenance'}
                if not actual_names:
                    own['analysisMode'] = 'management_direction'
                    own['patientTask'] = (
                        '本类没有已识别个人疾病，写适用的日常预防与症状识别行动，'
                        '不创造过敏体质、情绪障碍、神经疾病等个人状态。'
                    )
                if ids:
                    add([code, 'categories', i, 'clinicalCorrelation', 'text'], own, ids)
                    add([code, 'categories', i, 'attentionBasis'], own, ids)
                    add([code, 'overviewMatrix', i, 'basis'], own, ids)
        elif code == 'nutritionAssessment':
            for collection in ('detailedMetabolism', 'vitaminsAndMinerals'):
                for i, row in enumerate(wire[code][collection]):
                    ids = _fact_ids(row, facts)
                    if ids:
                        add([code, collection, i, 'explanation', 'text'],
                            owner('nutrition', row, category=row['code'], role='interpretation'), ids)
            for field, collection in (('aminoMetaboliteInterpretation', 'aminoAcidsAndMetabolites'),
                    ('neurotransmitterInterpretation', 'neurotransmitters')):
                ids = _fact_ids(wire[code][collection], facts)
                if ids:
                    add([code, field, 'text'], {'kind': 'nutrition', 'code': collection,
                        'name': '氨基酸与其他代谢物' if collection == 'aminoAcidsAndMetabolites' else '神经递质',
                        'aggregate': True}, ids)
            add([code, 'nutritionInterpretation', 'text'])
        elif code == 'functionalPathways':
            for i, row in enumerate(wire[code]['pathways']):
                ids = _fact_ids(row, facts)
                if ids:
                    add([code, 'pathways', i, 'healthMeaning'],
                        owner('pathway', row, category=row['code'], role='interpretation'), ids)
            add([code, 'summary', 'text'])
        elif code == 'comprehensiveConclusion':
            add([code, 'overallConclusion', 'text'],
                ids=planned_bindings.get('conclusionSummary', {}).get('factIds', [])
                if isinstance(content_plan, dict) else None)
            for i, row in enumerate(wire[code]['nextSteps']):
                ids = _fact_ids(row, facts)
                add([code, 'nextSteps', i, 'description'], owner('focus', row, role='action'), ids, duty='management')
            # The management table of the report: one line per priority with
            # its key result and its first measure (time limit; action; clinic).
            for i, row in enumerate(wire[code]['priorityDirections']):
                ids = _fact_ids(row, facts)
                own = owner('focus', row, role='action')
                add([code, 'priorityDirections', i, 'basis'], own, ids, required=False)
                add([code, 'priorityDirections', i, 'followupAction'], own, ids, required=False, duty='management')
        chapters.append(chapter)
    def source_handles(row):
        return list(dict.fromkeys(source['recordId'] for source in row.get('sources', ())
            if source.get('recordId') in facts))

    source_context = {
        'clinicalActions': [
            {**{key: deepcopy(value) for key, value in action.items() if key != 'sourceFactIds'},
             'recordIds': list(action['sourceFactIds'])}
            for action in clinical_actions
        ],
        'clinicalPatientProjections': deepcopy(patient_projections),
        'clinicalObservationGroups': deepcopy(clinical_observation_groups),
        'activityConstraintGroups': deepcopy(activity_constraint_groups),
        'microbiomeSourceSemantics': deepcopy(microbiome_view.get('_sourceSemanticsByFactId', {})),
        'assayGroups': assay_groups(microbiome_view),
        'functionCellDuties': deepcopy(microbiome_view.get('_functionCellDuties', {})),
        'reportedSourceRoles': deepcopy(getattr(document, '_clinical_reported_source_roles', {})),
        'reportedTemporalContexts': {
            fact.fact_id: reported_temporal_context(fact, reported_role=getattr(
                document, '_clinical_reported_source_roles', {},
            ).get(fact.fact_id))
            for fact in document.facts if fact.kind == 'self_reported'
        },
        'symptomSlots': [
            {'code': row['code'], 'name': row['name'], 'factIds': source_handles(row)}
            for row in clinical_view['symptoms']
        ],
        'panelContexts': [
            {'code': group['code'], 'name': group['name'],
             'sourceCounts': deepcopy(group.get('_sourcePanelCounts', {}))}
            for group in microbiome_view.get('organismGroups', ())
        ],
    }
    return ReportBlueprint(data=data, facts=list(facts.values()), focus=focus, chapters=chapters,
        clinical_trends=clinical_view['trends'], assessment_sources=source_context)
