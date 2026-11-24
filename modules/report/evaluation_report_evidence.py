"""Question-led evidence recall for the seven-chapter evaluate report.

The plan belongs to the supplied source inventory, not a derived risk registry.
Retrieval associations and source packets are candidates only. Chapter writing
and its independent review decide which literal propositions are applicable.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
from dataclasses import dataclass
from dataclasses import field
from types import SimpleNamespace
from typing import Any

from .evaluation_originals import build_evaluation_evidence_summaries
from .evaluation_originals import evaluation_citation_records
from .evaluation_originals import evaluation_original_passages
from .evaluation_originals import evaluation_original_text
from .evaluation_originals import evaluation_passage_role
from .evaluation_originals import evaluation_publication_identity
from .evaluation_originals import evaluation_source_packet
from .evaluation_originals import partition_evaluation_originals
from .evaluation_source_scope import source_scope_options
from .evidence import EvidenceQuery
from .evidence import number_evidence
from .schemas import EvidenceChannelAudit
from .schemas import EvidenceQueryAudit


@dataclass
class ReportEvidenceBundle:
    records: list[Any] = field(default_factory=list)
    summaries: list[dict[str, Any]] = field(default_factory=list)
    references: list[Any] = field(default_factory=list)
    query_audits: list[Any] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    plan: list[dict[str, Any]] = field(default_factory=list)
    # Raw transport results and quarantined sources remain private audit data.
    recalled_records: list[Any] = field(default_factory=list, repr=False)
    quarantined_records: list[Any] = field(default_factory=list, repr=False)
    changed_source_numbers: list[int] = field(default_factory=list)
    _query_budget: int = field(default=48, repr=False)
    _review_recall_limit: int = field(default=4, repr=False)
    _review_recall_used: bool = field(default=False, repr=False)
    _citation_numbers: dict[str, int] = field(default_factory=dict, repr=False)
    _topic_links: dict[Any, list[dict[str, Any]]] = field(default_factory=dict, repr=False)
    _reviewed_numbers: set[int] = field(default_factory=set, repr=False)
    _selection_fingerprints: dict[Any, str] = field(default_factory=dict, repr=False)
    _priority_topic_ids: set[str] = field(default_factory=set, repr=False)
    _selector_client: Any = field(default=None, repr=False)
    _selector_calls: int = field(default=0, repr=False)
    # Selection limits of the recall path that built this bundle.
    _per_topic: int = field(default=2, repr=False)
    _max_publications: int = field(default=36, repr=False)


def _get(value, *names, default=None):
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _unique(values):
    result = []
    for value in values:
        if value is not None and value != '' and value not in result:
            result.append(value)
    return result


def _strings(value):
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [item for item in (value or ()) if isinstance(item, str) and item.strip()]


def _key(value):
    return re.sub(r'\W+', '', str(value or '').casefold())


def _target_primary_ids(target):
    owner = _get(target, 'owner', default={}) or {}
    if isinstance(owner, dict) and 'primaryFactIds' in owner:
        return owner['primaryFactIds'] or ()
    return _get(target, 'sourceFactIds', default=()) or ()


def report_target_accepts_external_evidence(target):
    """A fact-only owner cannot request or receive a publication edge."""
    return _get(_get(target, 'owner'), 'usesReviewedFactsOnly') is not True


def _clinical_target_match(code, fact_ids, target):
    owner = _get(target, 'owner', default={}) or {}
    owner_code = str(_get(owner, 'code', default=''))
    # A separately bound clinical premise may supply an appropriate practical
    # action to a functional cell. The passage library grants only action for
    # this bridge, never a mechanism or causal interpretation of that function.
    if target.get('cellContract') and set(fact_ids) & set(owner.get('managementContextFactIds', ())):
        return True
    if _get(owner, 'kind') in {'goal', 'clinical_decision'} and owner_code:
        return owner_code == code
    if (_get(owner, 'kind') in {'clinical', 'clinical_observation', 'focus'}
            and owner_code.startswith('clinicalProblem:')):
        return owner_code == code
    return owner_code == code or bool(set(fact_ids) & set(_target_primary_ids(target)))


# Names of measured functional families, never patient diagnoses or thresholds.
# One family search can serve its nutrition group and corresponding pathway.
_FUNCTION_FAMILIES = (
    ('carbohydrate', ('碳水化合物', '糖类', 'carbohydrate'), 'gut microbial carbohydrate metabolism'),
    ('lipid', ('脂质', '脂类', 'lipid', '胆汁酸'), 'gut microbial lipid and bile acid metabolism'),
    ('scfa', ('短链脂肪酸', 'short chain fatty', 'scfa'), 'gut microbial short chain fatty acid production'),
    ('aminoAcid', ('氨基酸', 'amino acid'), 'gut microbial amino acid biosynthesis'),
    ('vitamin', ('维生素', 'vitamin'), 'gut microbial vitamin biosynthesis'),
    ('neurotransmitter', ('神经递质', 'neurotransmitter'), 'gut microbial neurotransmitter synthesis'),
    ('nucleotide', ('核苷酸', '核苷', '嘌呤', 'nucleotide', 'purine'), 'gut microbial nucleotide and purine metabolism'),
    ('pigment', ('天然色素', 'pigment'), 'gut microbial pigment biosynthesis'),
    ('xenobiotic', ('毒性物质分解', 'xenobiotic', 'detoxification'), 'gut microbial xenobiotic degradation'),
    ('harmfulMetabolite', ('有害代谢物', 'harmful metabolite'), 'gut microbial harmful metabolite production'),
    ('pathogenicPotential', ('致病潜力', '有害负荷', '毒素', 'toxin'), 'gut microbial toxin genes pathogenic potential'),
    ('defence', ('防御与耐受', 'defence', 'defense'), 'gut microbial defence stress tolerance functional pathways'),
    ('amr', ('耐药', 'antimicrobial resistance'), 'gut microbiome antimicrobial resistance genes'),
    ('traceElement', ('微量元素', 'trace element'), 'gut microbiome trace element metabolism'),
)


def _functional_family(name):
    folded = str(name).casefold()
    return next(((code, term) for code, names, term in _FUNCTION_FAMILIES
        if any(alias.casefold() in folded for alias in names)), None)


def _explicit_abnormal(row, facts):
    if any(_get(fact, 'abnormal') is True for fact in facts):
        return True
    statuses = [_get(row, name) for name in ('sourceStatus', 'source_status', 'assessmentLevel', 'status')]
    statuses.extend(_get(fact, 'status') for fact in facts)
    return any(isinstance(status, str) and re.search(
        r'异常|偏[高低]|过[高低多少]|极[高低]|不健康|不达标|\babnormal\b|\b(?:high|low)\b', status, re.I)
        for status in statuses)


def build_report_evidence_plan(blueprint) -> list[dict[str, Any]]:
    """Bind reusable questions to actual owners; no member-by-member searches."""
    chapters = list(_get(blueprint, 'chapters', default=()) or ())
    entries, all_targets = {}, []
    for chapter in chapters:
        code = str(_get(chapter, 'code', default=''))
        all_targets.extend((code, target) for target in _get(chapter, 'targets', default=()) or ()
            if report_target_accepts_external_evidence(target))

    def add(identity, kind, name, terms, fact_ids, chapter_codes, targets=(), roles=(), priority=2):
        targets = _unique(targets)
        if not name or not fact_ids or not targets:
            return
        topic = entries.setdefault(identity, {'topicId': 'report-topic-' + hashlib.sha256(identity.encode()).hexdigest()[:20],
            'kind': kind, 'title': str(name), 'aliases': [], 'sourceFactIds': [],
            'chapterCodes': [], 'targetIds': [], 'allowedRoles': [], 'priority': priority})
        for key, values in [('aliases', terms), ('sourceFactIds', fact_ids), ('chapterCodes', chapter_codes),
                ('targetIds', targets), ('allowedRoles', roles)]:
            topic[key] = _unique([*topic[key], *values])
        topic['priority'] = max(topic['priority'], priority)

    for focus in _get(blueprint, 'focus', default=()) or ():
        name = _get(focus, 'name', 'title', default='')
        code = str(_get(focus, 'code', 'id', default=_key(name)))
        ids = _strings(_get(focus, 'factIds', 'sourceFactIds', default=()))
        matching = [(chapter, target) for chapter, target in all_targets
            if _clinical_target_match(code, ids, target)]
        kind = str(_get(focus, 'kind', 'focusKind', default='clinical'))
        if kind not in {'clinical', 'goal', 'clinical_decision'}:
            kind = 'clinical'
        identity = kind + ':' + code
        add(identity, kind, name,
            _unique([*_strings(_get(focus, 'searchTerms', default=())),
                *_strings(_get(focus, 'aliases', default=())), str(name)]), ids,
            _unique(['medicalAssessment', 'diseaseRiskAssessment', 'comprehensiveConclusion',
                *(chapter for chapter, _ in matching)]),
            [str(_get(target, 'id', default='')) for _, target in matching],
            ['interpretation', 'action'], priority=3)
        if identity in entries:
            entries[identity]['ownerCode'] = code

    facts = {_get(fact, 'factId', 'fact_id'): fact
        for fact in _get(blueprint, 'facts', default=()) or ()}
    for chapter in chapters:
        if _get(chapter, 'code') != 'microbiomeAssessment':
            continue
        for row in _get(chapter, 'sourceRows', 'source_rows', default=()) or ():
            ids = _strings(_get(row, 'sourceFactIds', default=()))
            actual = [facts[fid] for fid in ids if fid in facts and _get(facts[fid], 'kind') == 'microbe']
            if not actual or not _explicit_abnormal(row, actual):
                continue
            name = _get(row, 'scientificName', 'scientific_name') or _get(row, 'name') or _get(actual[0], 'name')
            associated = [target for code, target in all_targets if code == 'microbiomeAssessment'
                and set(ids) & set(_target_primary_ids(target))]
            add('abnormal_flora:' + _key(name), 'abnormal_flora', name,
                _unique([*_strings(_get(row, 'searchTerms', default=())),
                    *_strings(_get(row, 'aliases', default=())), str(name)]), ids,
                ['microbiomeAssessment'], [str(_get(target, 'id', default='')) for target in associated],
                ['interpretation'], priority=3)

    for chapter_code, target in all_targets:
        owner = _get(target, 'owner', default={}) or {}
        kind = str(_get(owner, 'kind', default=''))
        name = str(_get(owner, 'name', default=''))
        code = str(_get(owner, 'code', default=_key(name)))
        ids = _strings(_target_primary_ids(target))
        roles = _strings(_get(target, 'allowedRoles', default=()))
        if kind == 'assessment_editorial' and code.startswith('symptomEvidence.microbiome.'):
            terms = [term for pattern, term in (
                (r'便秘|排便困难|粪便偏硬|排便费力|排便频率', 'constipation'),
                (r'腹泻', 'diarrhea'),
                (r'腹胀|胀气', 'abdominal bloating'),
            ) if re.search(pattern, name)]
            identity = 'symptom_microbiome:' + _key(name)
            add(identity, 'microbiome', name, [name, *terms], ids, [chapter_code],
                [target['id']], ['interpretation'], priority=3)
            if terms and identity in entries:
                entries[identity]['queryTerms'] = [f'{terms[0]} fecal microbiota human observational association']
            continue
        if kind == 'assessment_editorial' and code == 'structure.gmwi2':
            kind = 'microbiome'
        if kind not in {'abnormal_flora', 'nutrition', 'pathway', 'microbiome', 'behavior', 'goal', 'clinical_decision'}:
            continue
        if roles and set(roles) <= {'observation', 'source_state', 'source_data'}:
            continue
        if kind == 'abnormal_flora' and code == 'microbial_structure':
            kind = 'microbiome'
        terms = _unique([*_strings(_get(owner, 'searchTerms', default=())),
            *_strings(_get(owner, 'aliases', default=())), name])
        family = _functional_family(name) if kind in {'nutrition', 'pathway'} else None
        if family:
            identity = 'function:' + family[0]
            terms = _unique([family[1], *terms])
        elif kind == 'microbiome':
            identity = 'microbiome:structure'
            terms = _unique(['gut microbiome diversity richness ecological health indices', *terms])
        else:
            identity = kind + ':' + code
        add(identity, kind, name, terms, ids, [chapter_code],
            [str(_get(target, 'id', default=''))], roles, priority=3 if kind in {'abnormal_flora', 'goal', 'clinical_decision'} else 2)
        if kind in {'goal', 'clinical_decision'} and identity in entries:
            entries[identity]['ownerCode'] = code
            entries[identity]['sourceQuestion'] = str(_get(owner, 'sourceQuestion', default=''))
            entries[identity]['reportedGoal'] = str(_get(owner, 'reportedGoal', default=name))

    for chapter in chapters:
        chapter_code = str(_get(chapter, 'code', default=''))
        for row in _get(chapter, 'sourceRows', 'source_rows', default=()) or ():
            ids = _strings(_get(row, 'sourceFactIds', default=()))
            status = str(_get(row, 'sourceStatus', 'source_status', 'status', default='') or '')
            detected = _get(row, 'detected')
            symbols = _strings(_get(row, 'detectedGenes', 'detected_genes', default=()))
            if (detected is False or re.search(r'未检出|未检测|not detected|negative', status, re.I)):
                continue
            if detected is True or status in {'检出', '阳性', 'detected', 'positive'}:
                symbols = _unique([*symbols, *_strings(_get(row, 'geneName', 'gene_name', default=())),
                    *_strings(_get(row, 'geneNames', 'gene_names', default=()))])
                if _get(row, 'kind') in {'gene', 'gene_marker'} and not symbols:
                    symbols = _strings(_get(row, 'name', default=''))
            for symbol in symbols:
                associated = [target for code, target in all_targets if code == chapter_code
                    and set(ids) & set(_target_primary_ids(target))]
                add('gene:' + _key(symbol), 'gene', symbol, [str(symbol)], ids, [chapter_code],
                    [str(_get(target, 'id', default='')) for target in associated], ['interpretation'], priority=4)
                entry = entries.get('gene:' + _key(symbol))
                if entry is not None:
                    panel = str(_get(row, 'panelId', 'panel_id', 'observationId', 'code', default='') or '|'.join(sorted(ids)))
                    entry['sourcePanelIds'] = _unique([*entry.get('sourcePanelIds', ()), panel])
                    entry['sourceOwnerNames'] = _unique([*entry.get('sourceOwnerNames', ()), str(_get(row, 'name', default=''))])

    # A source paragraph/fact may mention multiple problems. Its aliases are
    # not synonyms for each clinical owner, measured gene or functional group.
    # Exact row aliases already entered the taxon topic at its own construction.
    for chapter_code, target in all_targets:
        if chapter_code not in {'healthOverview', 'comprehensiveConclusion'}:
            continue
        ids = set(_target_primary_ids(target))
        for topic in entries.values():
            if topic['kind'] in {'clinical', 'goal', 'clinical_decision'} and not _clinical_target_match(
                    topic['ownerCode'], topic['sourceFactIds'], target):
                continue
            if ids & set(topic['sourceFactIds']):
                topic['chapterCodes'] = _unique([*topic['chapterCodes'], chapter_code])
                topic['targetIds'] = _unique([*topic['targetIds'], str(_get(target, 'id', default=''))])
    # Patient-wide management is a different question from a microbial
    # mechanism. Bind a few actual clinical/behavior owners to action cells
    # whose management context contains those same real patient facts.
    candidates = {'clinical': [], 'behavior': []}
    for focus in _get(blueprint, 'focus', default=()) or ():
        if _get(focus, 'origin') == 'source_association' or _get(focus, 'kind') in {'goal', 'clinical_decision'}:
            continue
        ids = [fid for fid in _strings(_get(focus, 'factIds', 'sourceFactIds', default=()))
            if fid in facts and _get(facts[fid], 'kind') in {'clinical', 'self_reported'}]
        name = str(_get(focus, 'name', 'title', default='') or '')
        if ids and name:
            candidates['clinical'].append((str(_get(focus, 'code', default=_key(name))), name,
                _unique([*_strings(_get(focus, 'searchTerms', default=())),
                    *_strings(_get(focus, 'aliases', default=())), name]), ids))
    seen_behavior = set()
    for _, target in all_targets:
        own = _get(target, 'owner', default={}) or {}
        if _get(own, 'kind') != 'behavior':
            continue
        code, name = str(_get(own, 'code', default='')), str(_get(own, 'name', default=''))
        ids = [fid for fid in _strings(_target_primary_ids(target))
            if fid in facts and _get(facts[fid], 'kind') in {'clinical', 'self_reported'}]
        if code and name and ids and code not in seen_behavior:
            seen_behavior.add(code)
            candidates['behavior'].append((code, name,
                _unique([*_strings(_get(own, 'searchTerms', default=())),
                    *_strings(_get(own, 'aliases', default=())), name]), ids))
    selected_management = []
    for index in range(max((len(rows) for rows in candidates.values()), default=0)):
        for rows in candidates.values():
            if index < len(rows):
                selected_management.append(rows[index])
        if len(selected_management) >= 6:
            break
    for code, name, terms, ids in selected_management[:6]:
        matched = []
        for chapter_code, target in all_targets:
            own = _get(target, 'owner', default={}) or {}
            context = set(_strings(_get(own, 'managementContextFactIds', default=())))
            context.update(_strings(_target_primary_ids(target)))
            if ('action' in _strings(_get(target, 'allowedRoles', default=()))
                    and _get(target, 'contentDuty') != 'source_state' and context.intersection(ids)):
                matched.append((chapter_code, str(_get(target, 'id', default=''))))
        if not matched:
            continue
        identity = 'patient_management:' + code
        add(identity, 'patient_management', name, terms, ids,
            _unique(chapter for chapter, _ in matched), [tid for _, tid in matched], ['action'], priority=4)
        topic = entries[identity]
        topic.update(ownerCode=code, questionIntent='management',
            sourceClinicalTerms=list(terms), sourceQuestion='How should the recorded problem or behavior be managed?',
            reportedGoal=name, managementPurpose='patient_context_actions_only_not_microbial_mechanism')

    targets_by_id = {str(_get(target, 'id', default='')): target for _, target in all_targets}
    for topic in entries.values():
        analytic_targets = [target_id for target_id in topic['targetIds']
            if _get(targets_by_id.get(target_id), 'cellContract')
            and _get(targets_by_id.get(target_id), 'contentDuty') != 'source_state']
        topic['analysisCellTargetIds'] = analytic_targets
        # A column's responsibility, rather than the source's biological kind,
        # decides whether evidence is needed. A nutrition/pathway health-meaning
        # cell is medical interpretation just as a clinical-risk cell is.
        topic['retrievalMode'] = ('external_meaning' if analytic_targets else
            'source_comparison' if topic['kind'] in
            {'nutrition', 'pathway', 'microbiome', 'behavior'} else 'external_meaning')
    return list(entries.values())


# Recall synonyms are existing catalog identities, not new patient diagnoses.
def _clinical_aliases(topic):
    if topic['kind'] in {'goal', 'clinical_decision', 'patient_management'} and topic.get('objectTerms'):
        return list(topic['objectTerms'])
    aliases = _unique([*(topic['aliases'] or [topic['title']]), *topic.get('queryTerms', ())])
    if topic['kind'] in {'clinical', 'patient_management'}:
        from .evaluation_catalog import FIXED_DISEASE_ALIASES
        keys = {_key(alias) for alias in aliases}
        for values in FIXED_DISEASE_ALIASES.values():
            if keys & {_key(alias) for alias in values}:
                aliases.extend(alias for alias in values if len(alias) > 3)
    return _unique(aliases)


def _query(topic, *, recall=False, age_group='unknown', public=False):
    aliases = topic.get('queryTerms') or _clinical_aliases(topic)
    english = [value.strip() for value in aliases if re.search(r'[A-Za-z]{3}', value)]
    term = (english[1] if recall and len(english) > 1 else english[0] if english else aliases[0]).strip()
    kind = topic['kind']
    if kind in {'goal', 'clinical_decision', 'patient_management'}:
        purpose = topic.get('questionIntent', 'assessment')
        suffix = {'assessment': 'assessment guideline', 'screening': 'screening recommendations',
            'prevention': 'primary prevention guideline', 'management': ('guideline recommendations' if topic.get('queryTerms') else 'lifestyle follow-up management recommendations') if kind == 'patient_management' else 'management recommendations'}.get(purpose, 'assessment guideline')
        if recall:
            suffix += ' systematic review'
        intent = 'diagnostic_guideline'
    elif kind == 'clinical':
        # The actual problem remains the search head. No broad clinical/evaluation
        # word salad that ranks unrelated guideline front matter above the object.
        suffix, intent = ('management recommendations systematic review' if recall else 'lifestyle treatment recommendations' if public else 'management guideline'), 'diagnostic_guideline'
    elif kind == 'abnormal_flora':
        term = '"' + term.replace('"', '') + '"'
        suffix, intent = ('human gut microbiome' if not recall else 'human intestinal association'), 'microbe_clinical_significance'
    elif kind == 'gene':
        term = '"' + term.replace('"', '') + '"'
        suffix, intent = 'gene function', 'gut_function'
    elif kind == 'behavior':
        population = 'adult ' if age_group == 'adult' else 'pediatric ' if age_group == 'pediatric' else ''
        suffix, intent = population + 'guideline', 'health_management'
    else:
        suffix, intent = 'review', 'gut_function'
    return EvidenceQuery(query=' '.join((term, suffix)), supports=(topic['topicId'],),
        required_concepts=(), intent=intent, target=topic['title'], age_group=age_group,
        screen_profile='evaluate_general_function' if intent == 'gut_function' else 'clinical_report')


def _round_robin_topics(topics):
    # Clinical questions precede source-only families. All topics remain in the
    # inventory even when source comparison requires no external publication.
    order = ('clinical', 'patient_management', 'goal', 'clinical_decision', 'gene', 'abnormal_flora', 'nutrition', 'pathway', 'microbiome', 'behavior')
    return [topic for kind in order for topic in topics if topic['kind'] == kind]


async def _emit(audit, event, payload):
    if callable(audit):
        result = audit(event, payload)
        if inspect.isawaitable(result):
            await result
    elif isinstance(audit, dict):
        audit.setdefault('reportEvidenceEvents', []).append({'event': event, **payload})


def _enabled(client):
    return client is not None and getattr(client, 'enabled', True)


async def _retrieve(client, queries, *, deadline, concurrency, provider, shared_gate=None, chapter_recall=False):
    """Keep original transport limits, cancellation and stable submission order."""
    local_gate = asyncio.Semaphore(max(1, concurrency))
    shared_gate = shared_gate or asyncio.Semaphore(max(1, concurrency))

    def search_method(query):
        if chapter_recall and callable(search := getattr(client, 'search_recall_detailed', None)):
            return 'chapter_recall', search
        if query.intent == 'gut_function' and hasattr(client, 'search_evaluation_function_detailed'):
            return 'general_function', client.search_evaluation_function_detailed
        return 'regular', getattr(client, 'search_detailed', None)

    async def fetch(query, search, supports):
        records, channels, rejected = [], [], {}
        async with local_gate, shared_gate:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                rejected['deadline_exhausted'] = 1
            else:
                try:
                    async with asyncio.timeout(remaining):
                        if search is not None:
                            outcomes = await search(query.query, supports=supports, channels=query.channels)
                            for outcome in outcomes:
                                channels.append(EvidenceChannelAudit(channel=outcome.channel,
                                    status=outcome.status, provider=outcome.provider or provider,
                                    elapsed_ms=outcome.elapsed_ms, raw_count=len(outcome.records)))
                                if outcome.status == 'success':
                                    records.extend(outcome.records)
                        else:
                            records = await client.search(query.query, supports=supports)
                            channels = [EvidenceChannelAudit(channel='literature', status='success',
                                provider=provider, raw_count=len(records))]
                except TimeoutError:
                    rejected['deadline_exhausted'] = 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    rejected['transport_failure'] = 1
        return records, channels, rejected

    def audit_query(query, records, channels, rejected):
        copied = []
        for record in records:
            row = record.model_copy(deep=True)
            row.supports = _unique([*row.supports, *query.supports])
            row.query_ids = _unique([*row.query_ids, query.query_id])
            row.intents = _unique([*row.intents, query.intent])
            row.targets = _unique([*row.targets, query.target])
            copied.append(row)
        literal_count = sum(bool(evaluation_original_text(row)) for row in copied)
        return copied, EvidenceQueryAudit(query_id=query.query_id, query_text=query.query,
            intent=query.intent, target=query.target, channels=channels, raw_count=len(copied),
            screened_count=literal_count, retained_count=literal_count, rejected_reasons=rejected)
    requests = {}
    for index, query in enumerate(queries):
        route, search = search_method(query)
        # Same wire text can belong to multiple topics. Provider routes or
        # channel sets with different search behavior remain distinct.
        key = (route, query.query, query.channels) if chapter_recall else index
        requests.setdefault(key, (search, []))[1].append((index, query))

    async def run(search, entries):
        supports = _unique(support for _, query in entries for support in query.supports)
        records, channels, rejected = await fetch(entries[0][1], search, supports)
        return [(index, audit_query(query, records, channels, rejected)) for index, query in entries]

    grouped = await asyncio.gather(*(run(search, entries) for search, entries in requests.values()))
    results = [result for _, result in sorted(item for group in grouped for item in group)]
    return [record for records, _ in results for record in records], [row for _, row in results]


_STOP = {'gut', 'human', 'microbial', 'microbiome', 'review', 'and', 'the', 'for', 'with',
    'clinical', 'evaluation', 'management', 'guideline', 'guidelines', 'health', 'adult', 'adults'}


def _contains(text, alias):
    alias = str(alias or '').strip().casefold()
    if not alias:
        return False
    if re.search(r'[\u4e00-\u9fff]', alias):
        return len(alias) >= 2 and alias in text
    words = re.findall(r'[a-z0-9]+(?:[-\u2019\'][a-z0-9]+)*', alias)
    words = [word for word in words if word not in _STOP]
    if not words:
        return False
    # An object is a phrase, not unrelated words scattered across a long paper.
    pattern = r'[^a-z0-9]+'.join(re.escape(word) for word in words)
    return bool(re.search(r'(?<![a-z0-9])' + pattern + r'(?![a-z0-9])', text))


def _object_quotes(text, aliases, *, limit=2):
    sentences = [sentence for passage in evaluation_original_passages(text)
        if passage['eligibleForObjectMatch']
        for sentence in re.split(r'(?<=[.!?。！？])\s+|\n{2,}', passage['text'])]
    return _unique(sentence.strip() for sentence in sentences if sentence.strip()
        and any(_contains(sentence.casefold(), alias) for alias in aliases))[:limit]


def _content_link(record, topic):
    """Literal-object eligibility only; provider query tags cannot create edges.

    A clinical subject must be central in the title, rather than a comorbidity,
    incidental symptom, reference list or boilerplate elsewhere in a guideline.
    Final author/review still has to prove its particular claim and premises.
    """
    full = evaluation_original_text(record) or _get(record, 'sourceText', default='') or ''
    if not full:
        return None
    title = str(_get(record, 'title', default='') or '').casefold()
    aliases = _clinical_aliases(topic)
    kind = topic['kind']
    title_match = any(_contains(title, alias) for alias in aliases)
    if kind in {'clinical', 'goal', 'clinical_decision'} and not title_match:
        return None
    if kind in {'abnormal_flora', 'gene'}:
        # Exact measured taxon / gene identity; no genus-to-species or candidate
        # marker-to-detected marker expansion is manufactured here.
        aliases = topic['aliases'] or [topic['title']]
    quotes = _object_quotes(full, aliases)
    if not quotes:
        return None  # A title or AI summary alone is not an available proposition.
    if kind == 'patient_management' and not _MANAGEMENT.search(' '.join(quotes)):
        return None
    if kind in {'nutrition', 'pathway', 'microbiome'} and not re.search(
            r'\b(?:microbi\w*|bacteri\w*|intestinal|fecal|faecal|gut)\b|菌群|微生物',
            title + '\n' + ' '.join(quotes), re.I):
        return None
    if kind == 'behavior' and not title_match:
        return None
    if re.search(r'copyright|all rights reserved|disclaimer', full, re.I) and not any(
            re.search(r'recommend|suggest|study|trial|participants|patients|results|associated|found|研究|建议|结果', q, re.I)
            for q in quotes):
        return None
    return {'topicId': topic['topicId'], 'targetIds': list(topic['targetIds']),
        'sourceFactIds': list(topic['sourceFactIds']), 'ownerCode': topic.get('ownerCode'),
        'objectName': topic['title'], 'objectKind': topic['kind'],
        'questionIntent': topic.get('questionIntent'), 'objectTerms': list(topic.get('objectTerms', ())),
        'sourcePanelIds': list(topic.get('sourcePanelIds', ())),
        'matchBasis': 'literal_title_and_object' if title_match else 'literal_measured_object',
        'selectedQuotes': quotes, 'scopeNotes': ['Candidate only; claim-specific population and conditions require chapter review.'],
        'score': 3 if title_match else 2}


def _topic_match(record, topic):
    return _content_link(record, topic) is not None


def _group_records(records):
    grouped = {}
    for record in records:
        if evaluation_original_text(record):
            grouped.setdefault(evaluation_publication_identity(record), []).append(record)
    return grouped


def _literal_links(records, topics):
    grouped = _group_records(records)
    links = {}
    for identity, blocks in grouped.items():
        found = []
        for topic in topics:
            matches = [_content_link(block, topic) for block in blocks]
            matches = [match for match in matches if match]
            if matches:
                chosen = max(matches, key=lambda match: match['score'])
                chosen['selectedQuotes'] = _unique(q for match in matches for q in match['selectedQuotes'])[:2]
                found.append(chosen)
        if found:
            links[identity] = found
    return links


def _publication_rank(blocks, links):
    return (max((link.get('score', 0) for link in links), default=0),
        any(_get(row, 'category') == 'guideline' for row in blocks),
        max((_get(row, 'year', default=0) or 0 for row in blocks), default=0))


def _choose_publications(grouped, topics, links, *, preferred=(), per_topic=2, max_publications=36, priority_topic_ids=()):
    """Cover real topic edges fairly; never pad a topic with unrelated papers."""
    by_topic = {}
    ordered = _round_robin_topics(topics)
    ordered.sort(key=lambda topic: topic['topicId'] not in priority_topic_ids)
    for topic in ordered:
        tid = topic['topicId']
        choices = [identity for identity in grouped if any(link['topicId'] == tid for link in links.get(identity, ()))]
        choices.sort(key=lambda identity: _publication_rank(grouped[identity],
            [link for link in links[identity] if link['topicId'] == tid]), reverse=True)
        by_topic[tid] = choices
    retained = [identity for identity in preferred if identity in grouped and links.get(identity)][:max_publications]
    for index in range(per_topic):
        for topic in ordered:
            choices = by_topic[topic['topicId']]
            if index < len(choices) and choices[index] not in retained and len(retained) < max_publications:
                retained.append(choices[index])
    return retained


def _select_records(records, topics, *, per_topic=2, max_publications=36):
    grouped = _group_records(records)
    chosen = _choose_publications(grouped, topics, _literal_links(records, topics),
        per_topic=per_topic, max_publications=max_publications)
    return [record for record in records if evaluation_publication_identity(record) in chosen]


async def _enrich_originals(client, records, topics, *, deadline):
    if not hasattr(client, 'enrich_screened') or time.monotonic() >= deadline:
        return []
    selected = _select_records(records, topics, per_topic=1, max_publications=2)
    parents = {}
    for record in selected:
        parents.setdefault(evaluation_publication_identity(record), record)
    # A real PubMed title with no abstract may still have an existing licensed
    # PMC body. Metadata enables that transport read, never a literal claim.
    for record in records:
        if len(parents) >= 2:
            break
        if any(any(_contains(record.title.casefold(), alias) for alias in _clinical_aliases(topic))
                for topic in topics if topic['kind'] == 'clinical'):
            parents.setdefault(evaluation_publication_identity(record), record)
    async def read(parent):
        async with asyncio.timeout(max(.001, deadline - time.monotonic())):
            return await client.enrich_screened([parent], query=parent.title,
                supports=list(parent.supports), full_text_limit=1)
    results = await asyncio.gather(*(read(parent) for parent in parents.values()), return_exceptions=True)
    output = []
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            raise result
        if not isinstance(result, BaseException):
            output.extend(result)
    return output


_MANAGEMENT = re.compile(r'\b(?:recommend(?:s|ed)?|suggest(?:s|ed)?|advise[sd]?|offer(?:s|ing|ed)?|'
    r'refer(?:s|red|ring)?|encourag(?:e|es|ed|ing)|avoid(?:s|ed|ing)?|consider(?:s|ed|ing)?|'
    r'provide|should|must|need(?:s)? to|supports? (?:continual|regular|patient.centered))\b|建议|应当|推荐|避免|宜', re.I)


_NEURO_PRODUCT = (
    r'\b(?:GABA|dopamine|serotonin|5[- ]HT|5[- ]hydroxytryptamine|(?:gamma|γ)[- ]aminobutyric acid)\b|'
    r'多巴胺|血清素|5[-－]?羟色胺|(?:γ[-－]?)?氨基丁酸'
)
_NEURO_PRODUCER = (
    r'\b(?:bacteria(?:l (?:strains?|isolates?|communit\w*|consorti\w*|species|cultures?))?|microbiota|'
    r'microbiome|microbes?|microbial (?:strains?|isolates?|communit\w*|consorti\w*|species|cultures?))\b|'
    r'(?-i:\b[A-Z][a-z]{3,}\s+(?:sp|spp|ssp)\b)|菌群|微生物|细菌|菌株'
)
_NEURO_PRODUCTION = (
    r'\b(?:synthesi[sz]e[sd]?|biosynthesi[sz]e[sd]?|produc(?:e[sd]?|ing)|generat(?:e[sd]?|ing)|yield(?:s|ed|ing)?)\b|'
    r'\b(?:convert\w*|decarboxylat\w*|transform\w*)\b[^。.!?；;]{0,55}\b(?:to|into)\b|合成|产生|生成|转化为'
)
_NEURO_CAUSAL_CLAUSES = re.compile(
    r'[^。.!?；;]+?(?=\b(?:whereas|however|rather than)\b|[。.!?；;]|$)', re.I,
)
_NEURO_GUT_ORIGIN = re.compile(
    r'\b(?:human|adult|healthy adults?)[- ]+(?:\w+[- ]+){0,3}'
    r'(?:gut|intestin\w*|gastrointestinal|colonic|faecal|fecal|stool)\b|'
    r'\b(?:faecal|fecal|stool) (?:samples?|isolates?) from (?:healthy )?(?:human|adults?|volunteers?)\b|'
    r'(?:人类|人源|人体|健康成人)(?:的|来源的)?(?:肠道|结肠|粪便)', re.I,
)
_NEURO_INAPPLICABLE_ACTOR = re.compile(
    r'\b(?:enteric neurons?|(?:intestinal |gastrointestinal |host )?epithelial cells?|enterochromaffin|'
    r'enteroendocrine|(?:host|mammalian)[- ](?:derived|produced|serotonin|dopamine|GABA)|'
    r'rhizospheres?|plant(?:[- ]associated| roots?)|endophy\w*|grapevines?|phyllospheres?|'
    r'engineered|transgenic|knockout|mutants?|genetically (?:modified|engineered))\b|'
    r'肠(?:道)?神经元|肠(?:道)?上皮细胞|嗜铬细胞|肠内分泌细胞|宿主(?:来源|合成|产生)|'
    r'植物(?:内生|根系)|根际|葡萄植株|基因改造|工程菌|敲除|突变株', re.I,
)
_NEURO_REPORTED_POTENTIAL = re.compile(
    r'\b(?:genetic potential|predicted (?:ability|capacity|production|potential)|potential to|hypothes\w*|'
    r'whether|regulat\w*|stimulat\w*|induc\w*|promot\w*)\b|'
    r'\b(?:did not|do(?:es)? not|cannot|could not|fail\w* to)\s+(?:synthesi[sz]e|produc\w*|yield)\b|'
    r'基因潜力|预测(?:能力|合成)|调节|刺激|诱导|促进|未能(?:产生|合成)', re.I,
)
_NEURO_DIRECT_PRODUCTION = re.compile(
    rf'(?:{_NEURO_PRODUCER})[^。.!?；;]{{0,100}}(?:{_NEURO_PRODUCTION})[^。.!?；;]{{0,50}}'
    rf'(?:{_NEURO_PRODUCT})|(?:{_NEURO_PRODUCT})[^。.!?；;]{{0,55}}(?:{_NEURO_PRODUCTION}|\bproduction\b)'
    rf'[^。.!?；;]{{0,32}}(?:\bby\b|由)[^。.!?；;]{{0,45}}(?:{_NEURO_PRODUCER})|'
    rf'\b(?:production|biosynthesis|synthesis) of\s+(?:{_NEURO_PRODUCT})[^。.!?；;]{{0,35}}'
    rf'\bby\b[^。.!?；;]{{0,45}}(?:{_NEURO_PRODUCER})', re.I,
)
_NEURO_PRODUCER_LABEL = re.compile(rf'(?:{_NEURO_PRODUCT})[- ]producing\b|产(?:{_NEURO_PRODUCT})(?=的?[菌微])', re.I)
_SPECIES_ABBREVIATION = re.compile(r'\b(sp|spp|ssp|[A-Z])\.(?=\s)')


def supports_human_gut_neurotransmitter_synthesis(sentence, unit, title):
    """Require a neurotransmitter product attributed to native human-gut microbes."""
    # The site may be given in the same paragraph or in the title. Both the
    # producing organism and the measured product must occur in the result.
    origin = f'{title}\n{unit}'
    if not _NEURO_GUT_ORIGIN.search(origin):
        return False
    for clause in _NEURO_CAUSAL_CLAUSES.findall(_SPECIES_ABBREVIATION.sub(r'\1', sentence)):
        if _NEURO_INAPPLICABLE_ACTOR.search(clause) or _NEURO_REPORTED_POTENTIAL.search(clause):
            continue
        if not _NEURO_GUT_ORIGIN.search(clause) and _NEURO_INAPPLICABLE_ACTOR.search(origin):
            continue
        if _NEURO_DIRECT_PRODUCTION.search(_NEURO_PRODUCER_LABEL.sub(' ', clause)):
            return True
    return False


def _complete_passages(full, raw_passages):
    """Exact complete sentences, retaining the selected paragraph's prefix.

    An upstream excerpt may end mid-sentence. Its complete prefix can be read,
    but the broken tail/list item cannot be promoted to a governing quotation.
    No punctuation, subject or recommendation is generated by this helper.
    """
    result = []
    digest = hashlib.sha256(full.encode()).hexdigest()
    for passage in raw_passages:
        start, end = passage['start'], passage['end']
        role = evaluation_passage_role(full, start, end)
        if not role['eligibleForObjectMatch']:
            continue
        text = full[start:end]
        endings = list(re.finditer(r'[。！？]|[.!?]["\u201d\u2019\)]*(?=\s|$)', text))
        complete_sentence = bool(endings and endings[-1].end() == len(text.rstrip()))
        entire_original = next((row for row in evaluation_original_passages(full)
            if row['start'] == start and row['end'] == end), None)
        balanced = all(text.count(left) == text.count(right) for left, right in (('(', ')'), ('（', '）'), ('[', ']')))
        structural = entire_original and balanced and entire_original['structuralUnitKind']
        if structural:
            final = len(text)
            unit_kind = entire_original['structuralUnitKind']
        elif endings:
            final = endings[-1].end()
            complete_sentence = True
            unit_kind = 'complete_sentence'
        else:
            continue
        # Do not call a terminal abbreviation/heading a complete proposition.
        prefix = text[:final]
        if len(re.findall(r'[A-Za-z]+|[\u4e00-\u9fff]', prefix)) < 4:
            continue
        end = start + final
        result.append({'start': start, 'end': end, 'text': full[start:end], **role,
            'passageId': 'source-passage-' + hashlib.sha256(f'{digest}:{start}:{end}'.encode()).hexdigest()[:24],
            'completeSentences': complete_sentence, 'completeSourceUnit': True, 'unitKind': unit_kind,
            'sourceUnitProtocol': 'server_original_source_unit_v1',
            'hasManagementPredicate': bool(_MANAGEMENT.search(prefix)),
            'excerptTailOmitted': final < len(text.rstrip())})
    return result


def validate_original_source_unit(full, original):
    """Recompute a literal unit's identity/role; serialized flags grant nothing."""
    if not isinstance(full, str) or not isinstance(original, dict):
        return None
    start, end = original.get('start'), original.get('end')
    digest = hashlib.sha256(full.encode()).hexdigest()
    identity = original.get('id', original.get('passageId'))
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(full)
            or original.get('fullSourceSha256') != digest or full[start:end] != original.get('text')):
        return None
    units = _complete_passages(full, [{'start': start, 'end': end}])
    if len(units) != 1 or units[0]['passageId'] != identity or units[0]['text'] != original['text']:
        return None
    return {**units[0], 'id': identity, 'fullSourceSha256': digest}


def _anchored_topic_links(full, links, *, title=''):
    """Grant exact body passages, including to deterministic recall candidates.

    A transport receipt proves location/role, never medical applicability.
    References and metadata cannot enter through an older unanchored edge.
    """
    digest = hashlib.sha256(full.encode()).hexdigest()
    body = [row for row in evaluation_original_passages(full) if row['eligibleForObjectMatch']]
    result = []
    for link in links:
        neurotransmitter = link.get('functionFamily') == 'neurotransmitter'
        originals = []
        stored = link.get('selectedOriginalPassages')
        if isinstance(stored, list):
            for original in stored:
                if not isinstance(original, dict):
                    continue
                if (original.get('id', original.get('passageId')) not in link.get('selectedPassageIds', ())
                        or original.get('text') not in link.get('selectedQuotes', ())):
                    continue
                if (unit := validate_original_source_unit(full, original)) is None:
                    continue
                if neurotransmitter and not any(
                    passage['start'] <= unit['start'] <= unit['end'] <= passage['end']
                    and supports_human_gut_neurotransmitter_synthesis(unit['text'], passage['text'], title)
                    for passage in body
                ):
                    continue
                originals.append(unit)
        elif 'selectedOriginalPassages' not in link:
            # Find the quote only inside real body spans. A repeated phrase
            # in the bibliography is neither an anchor nor a reason to drop
            # its independent occurrence in the article's actual prose.
            for quote in _strings(link.get('selectedQuotes')):
                for passage in body:
                    offset = passage['text'].find(quote)
                    if offset < 0:
                        continue
                    if neurotransmitter and not supports_human_gut_neurotransmitter_synthesis(
                        quote, passage['text'], title
                    ):
                        continue
                    span = passage
                    if link.get('passageSelection') == 'mechanism_sentence':
                        start = passage['start'] + offset
                        span = {'start': start, 'end': start + len(quote)}
                    complete = _complete_passages(full, [span])
                    if link.get('passageSelection') == 'mechanism_sentence':
                        complete = [unit for unit in complete if unit['text'] == quote]
                    if complete:
                        originals.extend(complete)
                        break
        originals = list({row['passageId']: row for row in originals}.values())
        if not originals:
            continue
        roles = set(link.get('eligibleRoles', ('interpretation', 'action')))
        for original in originals:
            roles.intersection_update(original['allowedEvidenceUses'])
        # Knowledge about a function, study design or reporting technique
        # does not become a patient-management recommendation by containing
        # "should" or "recommend". The selector must identify that duty in
        # addition to the server's exact original-section permission.
        if (link.get('candidateUse') == 'general_function'
                or link.get('recommendationDomain') != 'patient_management'
                or not any(row['hasManagementPredicate'] for row in originals)):
            roles.discard('action')
        if not roles:
            continue
        secondary = any(row['citationRole'] == 'secondary_background' for row in originals)
        notes = list(link.get('scopeNotes') or [])
        domain = link.get('recommendationDomain')
        if domain:
            notes.append(f'Source recommendation duty: {domain}; only the admitted evidence roles are available.')
        if secondary:
            notes.append('Secondary attribution in the article body: background interpretation only; '
                'not this publication\'s own result or an independent management recommendation.')
        anchored = {**link, 'selectedQuotes': [row['text'] for row in originals],
            'selectedPassageIds': [row['passageId'] for row in originals],
            'selectedOriginalPassages': [{**row, 'id': row['passageId'], 'fullSourceSha256': digest}
                for row in originals],
            'eligibleRoles': sorted(roles), 'scopeNotes': notes,
            'sourceRole': 'secondary_background' if secondary else 'primary_body',
            'sourceRoleProtocol': 'server_original_section_role_v1'}
        bridge_targets = set(link.get('clinicalBridgeTargetIds', ()))
        if not bridge_targets:
            result.append(anchored)
            continue
        other = [tid for tid in link['targetIds'] if tid not in bridge_targets]
        bridges = [tid for tid in link['targetIds'] if tid in bridge_targets]
        if other:
            result.append({**anchored, 'targetIds': other})
        if bridges and 'action' in roles:
            result.append({**anchored, 'targetIds': bridges, 'eligibleRoles': ['action']})
    return result


def report_source_packet(record, *, chapter_code, quotes=(), max_chars=12000):
    """Bounded complete literal passages; never repair a provider fragment."""
    full = evaluation_original_text(record) or _get(record, 'sourceText', default='') or ''
    quoted = tuple(quote for quote in quotes if isinstance(quote, str) and quote)
    query = _get(record, 'chapterQueryTerms', default={}).get(chapter_code, '')
    raw = evaluation_source_packet(record, query=query, selected_quotes=quoted, max_chars=max_chars)
    passages = _complete_passages(full, raw['selectedPassages'])
    selected = '\n\n'.join(passage['text'] for passage in passages)
    complete = raw['isFullSource'] and selected == full
    packet = {**raw, 'text': selected, 'selectedSourceText': selected, 'selectedPassages': passages,
        'sourceOffsets': [{'start': row['start'], 'end': row['end']} for row in passages],
        'selectedSourceLength': len(selected),
        'sourceCoverage': sum(row['end']-row['start'] for row in passages)/len(full) if full else 0,
        'isFullSource': complete, 'isFullPublicationBody': complete and raw['isFullPublicationBody'],
        'requiresFullSourceScopeReview': not complete,
        'passageProtocol': 'server_exact_complete_source_passage_v1'}
    options = _body_source_scope_options(full, _get(record, 'title', default='') or '',
        selected_quotes=quoted or tuple(passage['text'] for passage in passages))
    availability = _get(record, 'originalAvailability', default=None)
    if availability is None:
        blocks = _get(record, 'originalTextBlocks', default=()) or (record,)
        availability = {'completeAvailableLiteralRetained': bool(full),
            'publicationBodyAvailable': any(_get(block, 'provider') == 'pmc'
                and re.fullmatch(r'PMC\d+', str(_get(block, 'sourceId', 'source_id', default='') or ''), re.I)
                for block in blocks)}
    return {'packet': packet, 'passages': [{'id': row['passageId'], **row,
            'fullSourceSha256': packet['fullSourceSha256'], 'available': True,
            'governingContextStatus': 'requires_semantic_review'} for row in passages],
        'sourceScopeOptions': options, 'originalAvailability': availability,
        'unreadQuoteCount': sum(quote not in selected for quote in quoted),
        'invalidQuoteCount': sum(quote not in full for quote in quoted)}


def _body_source_scope_options(full, title, *, selected_quotes=()):
    bodies = [row['text'] for row in evaluation_original_passages(full) if row['eligibleForObjectMatch']]
    return [option for option in source_scope_options(full, title, selected_quotes=selected_quotes)
        if option.get('sourceOrigin') == 'title' or (isinstance(option.get('sourceQuote'), str)
            and option['sourceQuote'] and any(option['sourceQuote'] in body for body in bodies))]


# Veterinary and aquaculture hosts never describe a human reader; laboratory
# rodents stay (they are graded as animal research).
_VETERINARY_HOST = re.compile(r'\b(?:mussels?|oysters?|shrimps?|prawns?|chickens?|broilers?|poultry|piglets?|'
    r'swine|cattle|cows?|bovine|calves|sheep|goats?|horses?|honeybees?|livestock|aquaculture|veterinary)\b|'
    r'家禽|仔猪|奶牛|水产', re.I)


def veterinary_host(title):
    return bool(_VETERINARY_HOST.search(str(title or '')))


def _subject_scope_matches(title, subject, facts=()):
    """Reject explicit incompatible/unknown demographic source populations.

    Whole multi-population guideline branches are not combined as AND duties;
    this check concerns the central title population only. The batch selector
    must additionally match the actual selected passage's conditions.
    """
    title = str(title).casefold()
    age = _get(subject, 'age')
    age = int(age) if str(age).isdigit() else None
    sex = str(_get(subject, 'sex', 'gender', default='') or '').casefold()
    female = sex in {'女', '女性', 'female', 'f', 'woman'}
    male = sex in {'男', '男性', 'male', 'm', 'man'}
    child = bool(re.search(r'\b(?:child\w*|pediatric\w*|paediatric\w*|infant\w*|adolescent\w*)\b|儿童|儿科|青少年', title))
    adult = bool(re.search(r'\badults?\b|成人', title))
    if child and not adult and (age is None or age >= 18):
        return False
    if adult and not child and (age is None or age < 18):
        return False
    # A source written for older people does not govern a middle-aged reader
    # (targets, weight-loss ranges and screening intervals differ).
    older = bool(re.search(r'\b(?:older (?:adults?|people|persons|patients)|elderly|geriatric\w*)\b|老年', title))
    if older and not re.search(r'middle[- ]aged|中年|all ages', title) and (age is None or age < 60):
        return False
    women = bool(re.search(r'\b(?:women|woman|female|perimenopausal|postmenopausal)\b|女性|妇女|围绝经|绝经后', title))
    men = bool(re.search(r'\b(?:men|man|male)\b|男性', title))
    if women and not men and not female or men and not women and not male:
        return False
    if veterinary_host(title):
        return False
    for pattern in (r'pregnan\w*|妊娠|孕妇', r'perimenopausal|postmenopausal|围绝经|绝经后',
            # Distinct patient populations: findings there do not transfer
            # to a reader who does not belong to them.
            r'cystic fibrosis|critical(?:ly)? ill\w*|intensive care|\bicu\b|\bsepsis\b|\bseptic\b|traumatic brain injur\w*|'
            r'\bhiv\b|ha?emodialysis|(?:organ|kidney|renal|liver|heart|lung|stem cell) transplant\w*|transplant recipients?|'
            r'preterm|neonat\w*|囊性纤维化|重症监护|重症患者|脓毒症|颅脑损伤|血液透析|器官移植|移植受者|早产|新生儿'):
        if re.search(pattern, title):
            actual = ' '.join(str(_get(fact, 'name', default='')) + ' ' + str(_get(fact, 'value', default='')) for fact in facts)
            if not re.search(pattern, actual, re.I):
                return False
    return True


def _json_value(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json', by_alias=True)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _selector_model_packet(payload):
    """Short request-local addresses, with source text and values unchanged.

    Only known ID-bearing fields are translated. Names, measured values,
    aliases, literal paragraphs and scope notes are never rewritten, even if
    their text happens to equal an identifier. Unknown returned handles retain
    their original spelling and fail the existing canonical scope checks.
    """
    facts = {row['factId']: f'F{index:03d}'
        for index, row in enumerate(payload['sourceFacts'], 1)}
    topics = {row['topicId']: f'T{index:03d}'
        for index, row in enumerate(payload['topics'], 1)}
    reverse_facts = {short: canonical for canonical, short in facts.items()}
    reverse_topics = {short: canonical for canonical, short in topics.items()}
    model = _json_value(payload)
    for row in model['sourceFacts']:
        row['factId'] = facts[row['factId']]
    for row in model.get('sourceFocus', ()):
        row['factIds'] = [facts.get(fid, fid) for fid in row.get('factIds', ())]
    for row in model['topics']:
        row['topicId'] = topics[row['topicId']]
        row['sourceFactIds'] = [facts.get(fid, fid) for fid in row.get('sourceFactIds', ())]
    for row in model['publications']:
        row['candidateTopicIds'] = [topics.get(tid, tid) for tid in row['candidateTopicIds']]

    def decode(response):
        value = _json_value(response)
        publications = value.get('publications', ()) if isinstance(value, dict) else ()
        for publication in publications if isinstance(publications, list) else ():
            links = publication.get('links', ()) if isinstance(publication, dict) else ()
            for link in links if isinstance(links, list) else ():
                if not isinstance(link, dict):
                    continue
                tid = link.get('topicId')
                if isinstance(tid, str):
                    link['topicId'] = reverse_topics.get(tid, tid)
                premises = link.get('premiseFactIds')
                if isinstance(premises, list):
                    link['premiseFactIds'] = [reverse_facts.get(fid, fid)
                        if isinstance(fid, str) else fid for fid in premises]
        return value
    return model, decode


def _selection_batches(common, publications, topics, facts, focus, *, max_chars=64000):
    """Pack owner/publication edges without dropping facts or literal passages.

    Every batch carries the entire clinical/questionnaire/context inventory,
    including negative observations and conflicting results, plus all measured
    facts belonging to its topics. Unrelated microbial inventories need not be
    duplicated in a clinical question's packet. A required combination that is
    itself too large is reported incomplete, never admitted with missing facts.
    """
    topic_map = {topic['topicId']: topic for topic in topics}
    fact_map = {_get(fact, 'factId'): fact for fact in facts if _get(fact, 'factId')}
    context_ids = {fid for fid, fact in fact_map.items()
        if _get(fact, 'kind') in {'clinical', 'self_reported', 'context'}}
    # Focus carries the source distinction (diagnosis, observation, goal), so its
    # own premises also remain available even when another topic owns the row.
    context_ids.update(fid for row in focus for fid in _strings(_get(row, 'factIds')) if fid in fact_map)
    topic_fields = ('topicId', 'kind', 'title', 'aliases', 'sourceFactIds',
        'questionIntent', 'objectTerms', 'managementPurpose', 'allowedRoles')
    fact_fields = {'factId', 'kind', 'name', 'value', 'unit', 'status', 'date',
        'referenceRange', 'abnormal', 'imaging'}
    focus_rows = [{**{key: _json_value(value) for key, value in row.items()
        if key in {'code', 'name', 'kind', 'certainty'}},
        'factIds': [fid for fid in _strings(_get(row, 'factIds')) if fid in fact_map]} for row in focus]
    publication_map = {row['id']: row for row in publications}

    def packet(units):
        topic_ids = _unique(tid for _, tid in units)
        required_ids = context_ids | {fid for tid in topic_ids
            for fid in topic_map[tid]['sourceFactIds']}
        missing_ids = required_ids - fact_map.keys()
        rows = []
        for pid in _unique(pid for pid, _ in units):
            rows.append({**publication_map[pid], 'candidateTopicIds':
                _unique(tid for owner, tid in units if owner == pid)})
        value = {**common,
            'sourceFacts': [{key: _json_value(item) for key, item in fact.items()
                if key in fact_fields} for fact in facts
                if _get(fact, 'factId') in required_ids],
            'sourceFocus': focus_rows,
            'topics': [{key: topic_map[tid].get(key) for key in topic_fields}
                for tid in topic_ids], 'publications': rows}
        canonical_size = len(json.dumps(value, ensure_ascii=False, allow_nan=False))
        # Admission is measured on the actual request-local model packet.
        # Long canonical IDs stay on the server; charging their repeated
        # spelling can reject a complete edge that fits on the wire.
        model, _ = _selector_model_packet(value)
        wire_size = len(json.dumps(model, ensure_ascii=False, allow_nan=False))
        return value, wire_size, missing_ids, canonical_size

    batches, rejected = [], []
    for publication in publications:
        for tid in _unique(publication['candidateTopicIds']):
            unit = (publication['id'], tid)
            single, size, missing, canonical_size = packet([unit])
            if missing or size > max_chars:
                rejected.append({'publicationId': unit[0], 'topicId': tid,
                    'reason': 'required_source_facts_missing' if missing else 'required_context_exceeds_payload_budget',
                    'payloadChars': size, 'canonicalPayloadChars': canonical_size, 'missingFactIds': sorted(missing)})
                continue
            for batch in batches:
                candidate, combined_size, _, combined_canonical_size = packet([*batch['units'], unit])
                if combined_size <= max_chars:
                    batch.update(units=[*batch['units'], unit], payload=candidate, chars=combined_size,
                        canonicalChars=combined_canonical_size)
                    break
            else:
                batches.append({'units': [unit], 'payload': single, 'chars': size, 'canonicalChars': canonical_size})
    return batches, rejected


async def _select_applicable_links(client, records, topics, *, blueprint, subject, deadline, audit):
    """One bounded selection phase, with scoped batches through the same gateway.

    Only actually read, matched passage edges survive. Batches share a single
    selection deadline; omitted/oversize premises and failed decisions remain
    incomplete and never acquire a weaker fallback eligibility path.
    """
    links = _literal_links(records, topics)
    if not callable(getattr(client, 'generate', None)) or not links or time.monotonic() >= deadline:
        await _emit(audit, 'candidate_selection_not_completed', {'eligibleEdges': 0,
            'reason': 'selector_unavailable_or_no_literal_candidates'})
        return {}
    grouped = _group_records(records)
    chosen = _choose_publications(grouped, topics, links, per_topic=2, max_publications=28)
    topic_map = {topic['topicId']: topic for topic in topics}
    # Keep all clinical context, rather than retaining only positively bound
    # owner facts and losing normal/reassuring or contradictory information.
    facts = list(_get(blueprint, 'facts', default=()) or ())
    focus = list(_get(blueprint, 'focus', default=()) or ())
    publications, lookups = [], {}
    for index, identity in enumerate(chosen, 1):
        blocks = grouped[identity]
        candidates = [link for link in links[identity] if topic_map[link['topicId']]['kind'] == 'gene'
            or _subject_scope_matches(blocks[0].title, subject, facts)]
        if not candidates:
            continue
        full = '\n\n'.join(_unique(evaluation_original_text(row) for row in blocks))
        query = ' '.join(alias for link in candidates for alias in topic_map[link['topicId']]['aliases'][:2])
        view = report_source_packet({'sourceText': full, 'title': blocks[0].title,
            'chapterQueryTerms': {'': query}}, chapter_code='', max_chars=2600)
        packet = view['packet']
        if not packet['selectedPassages']:
            continue
        pid = 'publication-' + str(index)
        publications.append({'id': pid, 'title': blocks[0].title, 'year': blocks[0].year,
            'sourcePacket': packet, 'sourceScopeOptions': view['sourceScopeOptions'],
            'candidateTopicIds': [link['topicId'] for link in candidates]})
        lookups[pid] = (identity, {link['topicId']: link for link in candidates},
            {row['passageId']: row for row in packet['selectedPassages']})
    common = {'task': 'select_report_original_candidates', 'instructions': [
        '筛本次报告真正适用的原文候选，不写报告、不批准医学结论。只可保留candidateTopicIds。',
        '逐publication完整阅读packet内原段与scope；未读、段不完整、对象/人群/疾病前提未知或不符，返回空links。不得因为标题相近/检索词/泛词回填。',
        'clinical/goal/clinical_decision必须核实际subject与sourceFacts/sourceFocus；goal是用户目标而非疾病，不能把血糖控制诉求当糖尿病。',
        'patient_management只筛实际患者临床/行为背景适用的管理推荐，非菌种机制证明；用clinical_context且只能action。临床实际事实符合指南管理人群即可，不要求原文复述患者排序，也不要求功能预测先成为疾病诊断。',
        '患者管理建议仍须完整推荐原段、真实适用人群与条件；不得由检出菌种/功能预测推本人确诊。abnormal_flora/gene/nutrition/pathway的机制解释仍独立匹配对象，不能借管理指南放行。',
        'action必须有本段完整真实推荐/治理谓词和条件；指南scope/范围/摘要介绍、无谓词列表、仅依从性不足/疾病机制都不能冒充管理建议。',
        '先判推荐的实际接受者和用途，再分角色：general_function永远只interpretation；'
        '研究目的、研究方法/注释工具介绍、报告编写规范中的应包含/可注明，均不是患者管理action。'
        '只有本段确实向适用患者提出该管理动作时recommendationDomain才是patient_management；'
        '给研究者/检验或报告人员的技术要求分别为research_method/reporting_method，其他为not_a_recommendation。'
        '可解释方法的原段只供真实相关的方法解释，不用写作者流程代替患者本格意义。',
        'goal的assessment/screening/prevention是待评估问题，不是本人疾病史；筛查人群可适用，确诊患者治疗人群不能据此匹配。',
        '同一原句只适用其真实对象/人群；单病菌/单gene机制不得给另一个panel或多个gene背书；源provider的毒力/耐药label不是独立注释证据。',
        'sectionRole只界定本篇正文/结果数据/明确推荐原行，仍须审对象、人群、终点及完整适用条件。'
        'completeSourceUnit可为完整句或原始完整结果数据/推荐行，不可补标点冒充句子。'
        'citationRole=secondary_background仅供明确归属的背景解释，不可冒充本篇实测结果或独立管理推荐；'
        '参考目录/元数据不是研究命题。',
        'gene基本功能只能general_function/interpretation，不能由序列检出推表达/感染/人体功能/药物反应。人体菌群关联研究需匹配人群且不能推因果。',
        '每link选择已给出的完整passageIds（1至2），不要自由截取quotes；server复制该原段。scopeStatus必须matched，premiseFactIds绑定真实本owner原fact；不能发明前提。',
        '输出JSON {publications:[{id,links:[{topicId,use:"clinical_context"|"general_function",'
        'roles:["interpretation"|"action"],'
        'recommendationDomain:"patient_management"|"reporting_method"|"research_method"|"not_a_recommendation",'
        'passageIds:[ID],scopeStatus:"matched",premiseFactIds:[真实factID],scopeNotes:[原文真实范围/限制]}]}]}。'],
        'subject': _json_value(subject)}
    batches, rejected = _selection_batches(common, publications, topics, facts, focus)
    if rejected:
        await _emit(audit, 'candidate_selection_batch_incomplete', {'incompleteEdges': rejected,
            'eligibleEdgesFromIncompletePackets': 0, 'weakFallbackAllowed': False})
    if not batches:
        await _emit(audit, 'candidate_selection_not_completed', {'eligibleEdges': 0,
            'reason': 'no_complete_owner_publication_packet', 'incompleteEdgeCount': len(rejected)})
        return {}
    selection_deadline = min(deadline, time.monotonic() + 60.)
    model_calls = 0
    model_packet_chars = []

    async def select(batch):
        nonlocal model_calls
        payload = batch['payload']
        model_payload, decode = _selector_model_packet(payload)
        model_packet_chars.append(len(json.dumps(model_payload, ensure_ascii=False, allow_nan=False)))
        ids_in_packet = {row['factId'] for row in payload['sourceFacts']}
        allowed_by_pid = {row['id']: set(row['candidateTopicIds']) for row in payload['publications']}
        if time.monotonic() >= selection_deadline:
            await _emit(audit, 'candidate_selection_batch_incomplete', {
                'reason': 'shared_selection_deadline_exhausted',
                'publicationIds': list(allowed_by_pid), 'eligibleEdges': 0, 'weakFallbackAllowed': False})
            return {}
        try:
            # generate() still owns the configured per-report/application model
            # pools. No new provider, model setting, or unbounded retry is used.
            async with asyncio.timeout(max(.001, selection_deadline-time.monotonic())):
                model_calls += 1
                response = decode(await client.generate(model_payload))
            if not isinstance(response, dict) or not isinstance(response.get('publications'), list):
                raise ValueError('invalid_candidate_selection')
            result = {}
            for publication in response['publications']:
                if not isinstance(publication, dict) or publication.get('id') not in allowed_by_pid:
                    continue
                identity, allowed, passages = lookups[publication['id']]
                valid = []
                for answer in publication.get('links', ()):
                    if (not isinstance(answer, dict)
                            or answer.get('topicId') not in allowed_by_pid[publication['id']]
                            or answer.get('scopeStatus') != 'matched'):
                        continue
                    topic = topic_map[answer['topicId']]
                    ids = _strings(answer.get('passageIds', ()))
                    premises = _strings(answer.get('premiseFactIds', ()))
                    if not 1 <= len(ids) <= 2 or any(pid not in passages for pid in ids):
                        continue
                    selected = [passages[pid] for pid in ids]
                    if not set(premises) <= ids_in_packet or not set(premises) & set(topic['sourceFactIds']):
                        continue
                    quotes = [row['text'] for row in selected]
                    if not any(any(_contains(quote.casefold(), alias) for alias in _clinical_aliases(topic)) for quote in quotes):
                        continue
                    use = answer.get('use')
                    if use not in {'clinical_context', 'general_function'}:
                        continue
                    if topic['kind'] in {'clinical', 'goal', 'clinical_decision', 'patient_management'} and use != 'clinical_context':
                        continue
                    roles = _unique(role for role in _strings(answer.get('roles', ())) if role in {'interpretation', 'action'})
                    for passage in selected:
                        roles = [role for role in roles if role in passage['allowedEvidenceUses']]
                    recommendation_domain = answer.get('recommendationDomain')
                    if use == 'general_function' or recommendation_domain != 'patient_management':
                        roles = [role for role in roles if role == 'interpretation']
                    if topic['kind'] == 'patient_management':
                        roles = [role for role in roles if role == 'action']
                    if topic['kind'] == 'gene' and use != 'general_function':
                        continue
                    if topic['kind'] == 'gene':
                        roles = [role for role in roles if role == 'interpretation']
                    if 'action' in roles and not any(row['hasManagementPredicate'] for row in selected):
                        roles.remove('action')
                    if not roles:
                        continue
                    publication_packet = next(row for row in payload['publications'] if row['id'] == publication['id'])
                    originals = [{**_json_value(row), 'id': row['passageId'],
                        'fullSourceSha256': publication_packet['sourcePacket']['fullSourceSha256'],
                        'scopeOptions': _json_value(publication_packet.get('sourceScopeOptions', []))}
                        for row in selected]
                    valid.append({**allowed[answer['topicId']], 'selectedQuotes': quotes,
                        'selectedPassageIds': ids, 'selectedOriginalPassages': originals, 'eligibleRoles': roles, 'scopePremiseFactIds': premises,
                        'matchBasis': 'read_complete_passage_and_owner_scope_selection',
                        'scopeNotes': _strings(answer.get('scopeNotes', ())), 'candidateUse': use,
                        'recommendationDomain': recommendation_domain, 'score': 4})
                if valid:
                    result[identity] = valid
            return result
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await _emit(audit, 'candidate_applicability_selection_unavailable', {'modelCalls': 1,
                'publicationCount': len(payload['publications']), 'eligibleEdges': 0,
                'reason': type(error).__name__, 'weakFallbackAllowed': False})
            return {}

    tasks = [asyncio.create_task(select(batch)) for batch in batches]
    try:
        selected_batches = await asyncio.gather(*tasks)
    finally:
        # Explicitly drain siblings on caller cancellation; no model work may
        # continue after the request's evidence-selection phase is cancelled.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    result = {}
    for selected in selected_batches:
        for identity, edges in selected.items():
            actual = result.setdefault(identity, [])
            for edge in edges:
                if edge not in actual:
                    actual.append(edge)
    await _emit(audit, 'candidate_applicability_selection', {'modelCalls': model_calls,
        'batchCount': len(batches), 'publicationCount': len(publications),
        'payloadChars': max(model_packet_chars, default=0),
        'batchPayloadChars': model_packet_chars,
        'canonicalBatchPayloadChars': [batch.get('canonicalChars', batch['chars']) for batch in batches],
        'literalEdgeCount': sum(map(len, links.values())), 'selectedEdgeCount': sum(map(len, result.values())),
        'incompleteEdgeCount': len(rejected),
        'unreadOrUnselectedPublicationCount': len(links)-len(result), 'weakFallbackAllowed': False})
    return result


def _compile_bundle(bundle, pool, topics):
    old_sources = {row['number']: (row['sourceText'], tuple(row.get('targetIds', ()))) for row in bundle.summaries}
    grouped = _group_records(pool)
    # No old-pool lock: prefer actually reviewed/cited sources, then reselect by
    # effective topic coverage. Unused weak old hits can always be displaced.
    preferred = _unique(evaluation_publication_identity(record) for record in bundle.records
        if any(row['number'] in bundle._reviewed_numbers and row['evidenceKey'] == record.evidence_key
            for row in bundle.summaries))
    links = bundle._topic_links
    chosen = _choose_publications(grouped, topics, links, preferred=preferred,
        per_topic=bundle._per_topic, max_publications=bundle._max_publications,
        priority_topic_ids=bundle._priority_topic_ids)
    bundle.records = [record for record in pool if evaluation_publication_identity(record) in chosen
        and evaluation_original_text(record)]
    citations, mapping = evaluation_citation_records(bundle.records)
    references, sorted_numbers = number_evidence(citations)
    number_keys = {number: key for key, number in sorted_numbers.items()}
    for reference in references:
        key = number_keys[reference.number]
        if key not in bundle._citation_numbers:
            bundle._citation_numbers[key] = max(bundle._citation_numbers.values(), default=0) + 1
        reference.number = bundle._citation_numbers[key]
        original_key = next(original for original, citation_key in mapping.items() if citation_key == key)
        identity = next(evaluation_publication_identity(row) for row in bundle.records if row.evidence_key == original_key)
        actual_ids = {edge['topicId'] for edge in links.get(identity, ())}
        reference.targets = [topic['title'] for topic in topics if topic['topicId'] in actual_ids]
    bundle.references = sorted(references, key=lambda row: row.number)
    reference_by_number = {reference.number: reference for reference in bundle.references}
    numbers = {key: bundle._citation_numbers[value] for key, value in mapping.items()}
    bundle.summaries = sorted(build_evaluation_evidence_summaries(bundle.records, numbers), key=lambda row: row['number'])
    topic_map = {topic['topicId']: topic for topic in topics}
    identities_by_key = {record.evidence_key: evaluation_publication_identity(record) for record in bundle.records}
    for summary in bundle.summaries:
        reference = reference_by_number[summary['number']]
        summary.update(title=reference.title, year=reference.year, doi=reference.doi, url=reference.url)
        identity = identities_by_key[summary['evidenceKey']]
        edges = _anchored_topic_links(summary['sourceText'], links.get(identity, []), title=summary['title'])
        assigned = [topic_map[edge['topicId']] for edge in edges]
        summary.update(queryTopicIds=list(summary.get('supports', ())), queryTargets=list(summary.get('targets', ())),
            supports=[topic['topicId'] for topic in assigned], targets=[topic['title'] for topic in assigned],
            topicLinks=[dict(edge) for edge in edges],
            topicIds=[topic['topicId'] for topic in assigned],
            chapterCodes=_unique(code for topic in assigned for code in topic['chapterCodes']),
            targetIds=_unique(target for topic in assigned for target in topic['targetIds']),
            sourceFactIds=_unique(fid for topic in assigned for fid in topic['sourceFactIds']),
            queryPurpose='literal_object_candidates_for_independent_chapter_review', candidateOnly=True)
        summary['chapterQueryTerms'] = {code: ' '.join(alias for topic in assigned
            if code in topic['chapterCodes'] for alias in topic['aliases'][:2]) for code in summary['chapterCodes']}
        summary['originalAvailability'] = {'completeAvailableLiteralRetained': True,
            'publicationBodyAvailable': any(block.get('provider') == 'pmc'
                and re.fullmatch(r'PMC\d+', str(block.get('sourceId') or ''), re.I)
                for block in summary['originalTextBlocks']),
            'sourceKinds': _unique(packet.get('sourceKind') for packet in summary.get('modelSourcePackets', []))}
        summary['sourcePacket'] = report_source_packet(summary, chapter_code='')['packet']
        summary['chapterPackets'], summary['chapterScopeOptions'] = {}, {}
        for code in summary['chapterCodes']:
            view = report_source_packet(summary, chapter_code=code)
            summary['chapterPackets'][code] = view['packet']
            summary['chapterScopeOptions'][code] = view['sourceScopeOptions']
        summary['sourceScopeOptions'] = _body_source_scope_options(summary['sourceText'], summary['title'],
            selected_quotes=tuple(passage['text'] for passage in summary['sourcePacket']['selectedPassages']))
    new_sources = {row['number']: (row['sourceText'], tuple(row.get('targetIds', ()))) for row in bundle.summaries}
    bundle.changed_source_numbers = sorted(number for number in old_sources.keys() | new_sources.keys()
        if old_sources.get(number) != new_sources.get(number))
    bundle.gaps = [row for row in bundle.gaps if row.get('reason') not in
        {'no_literal_source_candidate', 'source_comparison_without_external_candidate'}]
    for topic in topics:
        if not any(topic['topicId'] in row['topicIds'] for row in bundle.summaries):
            bundle.gaps.append({'reason': 'source_comparison_without_external_candidate'
                if topic.get('retrievalMode') == 'source_comparison' else 'no_literal_source_candidate',
                'topicId': topic['topicId'], 'chapterCodes': topic['chapterCodes'],
                'targetIds': topic['targetIds'], 'sourceFactIds': topic['sourceFactIds']})


def _merge_pool(bundle, existing, candidates):
    partition = partition_evaluation_originals(existing, candidates)
    bundle.quarantined_records.extend(partition.quarantined)
    blocked = {record.evidence_key for record in bundle.quarantined_records}
    bundle.gaps.extend(dict(row) for row in partition.gaps)
    return [record for record in (*partition.existing, *partition.candidates) if record.evidence_key not in blocked]


def _age_group(subject):
    age = _get(subject, 'age')
    age = int(age) if str(age).isdigit() else None
    return 'adult' if age is not None and age >= 18 else 'pediatric' if age is not None else 'unknown'


def _panel_round_robin(topics):
    buckets = {}
    for topic in topics:
        panel = next(iter(topic.get('sourcePanelIds', ())), topic['topicId'])
        buckets.setdefault(panel, []).append(topic)
    return [bucket[index] for index in range(max((len(rows) for rows in buckets.values()), default=0))
        for bucket in buckets.values() if index < len(bucket)]


def _initial_jobs(topics, evidence_client, public_client, limit, age_group):
    """Cover distinct template analysis duties before duplicate-provider recall.

    Raw source-state rows do not trigger member-level generic searches. The
    grouped nutrition, pathway, structure and behavior analysis cells do require
    originals; they share the existing total query budget with clinical owners.
    """
    clinical = [topic for topic in topics if topic['kind'] in {'clinical', 'goal', 'clinical_decision'}]
    management = [topic for topic in topics if topic['kind'] == 'patient_management']
    genes = _panel_round_robin([topic for topic in topics if topic['kind'] == 'gene'])[:4]
    flora = [topic for topic in topics if topic['kind'] == 'abnormal_flora'][:4]
    analytic = [topic for topic in topics if topic.get('analysisCellTargetIds')
        and topic.get('retrievalMode') == 'external_meaning'
        and topic['kind'] not in {'clinical', 'goal', 'clinical_decision', 'patient_management', 'gene', 'abnormal_flora'}]
    primary = evidence_client if _enabled(evidence_client) else public_client
    if not _enabled(primary):
        return []
    jobs = []
    if analytic or management:
        # First give every clinical owner and template analysis family a real
        # primary query. Repeating clinical queries against the fallback must
        # not spend all slots before nutrition/functions can be retrieved.
        families = [clinical, management, analytic, genes, flora]
        for index in range(max((len(rows) for rows in families), default=0)):
            for rows in families:
                if index < len(rows):
                    topic = rows[index]
                    provider = public_client if topic['kind'] == 'gene' and _enabled(public_client) else primary
                    jobs.append((provider, topic))
        if _enabled(public_client) and public_client is not primary:
            jobs.extend((public_client, topic) for topic in [*clinical, *management, *analytic])
        cap = min(48, max(0, limit))
    else:
        # Existing source-state-only callers retain their small recall shape.
        jobs.extend((primary, topic) for topic in clinical)
        if _enabled(public_client) and public_client is not primary:
            jobs.extend((public_client, topic) for topic in clinical)
        jobs.extend((public_client if _enabled(public_client) else primary, topic) for topic in genes)
        jobs.extend((primary, topic) for topic in flora)
        cap = min(max(0, limit), 24)
    return [(client, topic, _query(topic, public=client is public_client, age_group=age_group))
        for client, topic in jobs[:cap]]


async def _run_jobs(jobs, *, public_client, deadline, concurrency):
    gate = asyncio.Semaphore(min(8, max(1, concurrency)))
    grouped = []
    for client, _, query in jobs:
        entry = next((entry for entry in grouped if entry[0] is client), None)
        if entry is None:
            entry = [client, []]
            grouped.append(entry)
        entry[1].append(query)
    results = await asyncio.gather(*(_retrieve(client, queries, deadline=deadline,
        concurrency=min(2 if client is public_client else 8, max(1, concurrency)),
        provider='public_literature' if client is public_client else 'evimed_v2', shared_gate=gate)
        for client, queries in grouped))
    return [record for records, _ in results for record in records], [row for _, rows in results for row in rows]


def _goal_term_is_literal_question(term, topic):
    """A disease in a screening question is not a positive diagnosis.

    Only an existing-disease treatment assumption needs source support. This
    is query metadata; neither object terms nor translated queries add facts.
    """
    from .evaluation_catalog import FIXED_DISEASE_ALIASES
    if not re.search(r'[A-Za-z]{3}', term) or len(term) > 160:
        return False
    purpose = topic.get('questionIntent', 'assessment')
    source = ' '.join(topic.get('sourceClinicalTerms', ()))
    existing = False
    supported_words, unknown_words = set(), set()
    assumption = purpose == 'management' or bool(re.search(
        r'\b(?:management|treat\w*|therap\w*|pharmac\w*|medicat\w*|drugs?)\b', term, re.I))
    generic = {'disease','syndrome','disorder','functional','associated','metabolic','chronic',
        'primary','secondary','related','blood','pressure','weight','adult','acute','recurrent','type'}
    for aliases in FIXED_DISEASE_ALIASES.values():
        words = {word for alias in aliases for word in re.findall(r'[a-z]{5,}', alias.casefold()) if word not in generic}
        matched = {word for word in words if _contains(term.casefold(), word)}
        literal = any(_contains(term.casefold(), alias) for alias in aliases)
        if not matched and not literal:
            continue
        known = any(_contains(source.casefold(), alias) for alias in aliases)
        if literal and not known and assumption:
            return False
        if known:
            existing = True
            supported_words.update(matched)
        else:
            unknown_words.update(matched)
    if assumption and unknown_words - supported_words:
        return False
    treatment = bool(re.search(r'\b(?:treat\w*|therap\w*|pharmac\w*|medicat\w*|drugs?)\b', term, re.I))
    if treatment and (purpose != 'management' or not existing):
        return False
    return purpose in {'assessment', 'screening', 'prevention', 'management'}


async def _plan_report_questions(client, topics, *, blueprint, deadline, audit):
    """Translate source-owned goals/questions once; never generate a diagnosis."""
    questions = [topic for topic in topics if topic['kind'] in {'goal', 'clinical_decision', 'patient_management'}]
    if not questions or not callable(getattr(client, 'generate', None)) or time.monotonic() >= deadline:
        return
    by_id = {topic['topicId']: topic for topic in questions}
    clinical_terms = _unique(term for focus in _get(blueprint, 'focus', default=())
        if _get(focus, 'certainty') in {'source_finding', 'confirmed', 'current_problem'}
        and _get(focus, 'origin') != 'source_association' for term in
        [*_strings(_get(focus, 'searchTerms', default=())), *_strings(_get(focus, 'aliases', default=())), str(_get(focus, 'name', default=''))])
    for topic in questions:
        topic['sourceClinicalTerms'] = _unique([*_strings(topic.get('sourceClinicalTerms')), *clinical_terms])
        topic.setdefault('questionIntent', 'assessment')
    payload = {'task': 'plan_report_evidence_questions',
        'instructions': ['把真实用户目标/医疗问题转换为少量准确英文检索术语，不写事实或诊断。',
            '目标默认assessment/screening/prevention；允许用相关待评疾病词检索筛查/风险评估/预防原文，但不能生成本人已患病事实。',
            'patient_management以真实问题查适用的健康管理推荐：在饮食、活动、症状应对、诊断性随访中选本问题真正需要的1至2方向，不把所有方向拼成一个检索串；已有临床疾病/真实行为可查对应管理，不把菌群信号当诊断前提。',
            '只有sourceClinicalTerms已有对应疾病来源才允许既病治疗management；不能把筛查问题改成确诊治疗。',
            '输出JSON {questions:[{topicId,questionIntent:assessment|screening|prevention|management,objectTerms:[1至3个英文对象词],searchTerms:[1至3个简短英文问题术语]}]}，不新增来源无关问题/基因/干预。'],
        'questions': [{key: topic.get(key) for key in ('topicId','kind','title','questionIntent','managementPurpose','sourceQuestion','reportedGoal','sourceClinicalTerms')}
            for topic in questions]}
    json.dumps(payload, ensure_ascii=False, allow_nan=False)
    try:
        async with asyncio.timeout(max(.001, min(30., deadline-time.monotonic()))):
            response = await client.generate(payload)
        for answer in response.get('questions', ()) if isinstance(response, dict) else ():
            if not isinstance(answer, dict) or answer.get('topicId') not in by_id:
                continue
            topic = by_id[answer['topicId']]
            purpose = answer.get('questionIntent', 'assessment')
            if purpose not in {'assessment', 'screening', 'prevention', 'management'}:
                continue
            planned = {**topic, 'questionIntent': purpose}
            terms = [term.strip() for term in _strings(answer.get('searchTerms', ()))
                if _goal_term_is_literal_question(term, planned)][:3]
            objects = [term.strip() for term in _strings(answer.get('objectTerms', ()))
                if re.search(r'[A-Za-z]{3}', term) and len(term) <= 120][:3]
            if terms:
                topic['queryTerms'] = _unique(terms)
                topic['questionIntent'] = purpose
                topic['objectTerms'] = _unique(objects or terms)
        await _emit(audit, 'source_goal_question_terms', {'modelCalls': 1,
            'questionCount': len(questions), 'translatedCount': sum(bool(topic.get('queryTerms')) for topic in questions)})
    except asyncio.CancelledError:
        raise
    except Exception:
        await _emit(audit, 'source_goal_question_terms_unavailable', {'modelCalls': 1, 'questionCount': len(questions)})


async def retrieve_report_evidence(evidence_client, public_client=None, narrative_client=None, *,
        blueprint, subject, deadline, audit=None, max_queries=48, max_gap_queries=8, concurrency=8,
        research_agent=None, research_scope=None):
    """Question-led complementary recall, one candidate selection, no risk chain."""
    # Keep the request-owned client behind a callable, never a serializable
    # adapter/hub/config object in candidate audit snapshots.
    bundle = ReportEvidenceBundle(plan=build_report_evidence_plan(blueprint),
        _selector_client=lambda: narrative_client)
    await _plan_report_questions(narrative_client, bundle.plan, blueprint=blueprint, deadline=deadline, audit=audit)
    topics = _round_robin_topics(bundle.plan)
    budget = min(48, max(0, int(max_queries)))
    reserve = min(max(0, int(max_gap_queries)), max(0, budget // 4))
    bundle._query_budget = budget
    bundle._review_recall_limit = min(4, reserve)
    age_group = _age_group(subject)
    jobs = _initial_jobs(topics, evidence_client, public_client, budget - reserve, age_group)
    await _emit(audit, 'plan', {'topicCount': len(topics), 'primaryQueryCount': len(jobs),
        'totalQueryBudget': budget, 'actualGapReserve': bundle._review_recall_limit,
        'sourceComparisonTopicCount': sum(topic.get('retrievalMode') == 'source_comparison' for topic in topics)})
    if not jobs and any(topic.get('retrievalMode') != 'source_comparison' for topic in topics):
        bundle.gaps.append({'reason': 'evidence_provider_unavailable' if not (_enabled(evidence_client)
            or _enabled(public_client)) else 'query_budget_unavailable'})
    records, rows = await _run_jobs(jobs, public_client=public_client, deadline=deadline, concurrency=concurrency)
    bundle.query_audits.extend(rows)
    bundle.recalled_records.extend(records)
    pool = _merge_pool(bundle, [], records)
    # At most two initial actual missing clinical questions; never backfill the
    # unused query allowance with every deferred taxon or nutrition member.
    links = _literal_links(pool, topics)
    covered = {edge['topicId'] for edges in links.values() for edge in edges}
    missing = [topic for topic in topics if topic['kind'] in {'clinical', 'goal', 'clinical_decision'} and topic['topicId'] not in covered]
    room = min(2, max(0, budget - len(rows) - bundle._review_recall_limit))
    fallback = public_client if _enabled(public_client) else evidence_client
    attempts = {row.query_id for row in rows}
    extra = []
    if _enabled(fallback):
        for topic in missing:
            query = _query(topic, recall=True, public=fallback is public_client, age_group=age_group)
            if query.query_id not in attempts:
                extra.append((fallback, topic, query))
                attempts.add(query.query_id)
            if len(extra) >= room:
                break
    if room and extra and time.monotonic() < deadline:
        recalled, rows = await _run_jobs(extra[:room], public_client=public_client, deadline=deadline, concurrency=concurrency)
        bundle.query_audits.extend(rows)
        bundle.recalled_records.extend(recalled)
        pool = _merge_pool(bundle, pool, recalled)
    if _enabled(public_client) and time.monotonic() < deadline:
        public_rows = [record for record in pool if _get(record, 'provider') in {'pubmed', 'pmc', 'crossref'}]
        try:
            enriched = await _enrich_originals(public_client, public_rows, topics, deadline=deadline)
            bundle.recalled_records.extend(enriched)
            pool = _merge_pool(bundle, pool, enriched)
        except asyncio.CancelledError:
            raise
        except Exception:
            bundle.gaps.append({'reason': 'public_original_enrichment_unavailable'})
    if research_agent is not None and research_scope and research_scope[2]:
        # Bridge only the existing source-owned questions into develop's
        # optional research capability. Returned originals are candidates:
        # they still enter the same conflict quarantine, source selection and
        # per-sentence clinical review as every other retrieval channel.
        from .research_integration import supplement_evidence

        external_topics = [topic for topic in topics
            if topic.get('retrievalMode') != 'source_comparison']
        candidate_links = _literal_links(pool, external_topics)
        by_topic = {}
        for identity, edges in candidate_links.items():
            for edge in edges:
                by_topic.setdefault(edge['topicId'], []).append(identity)
        research_queries = tuple(_query(topic, age_group=age_group) for topic in external_topics)
        research_plan = SimpleNamespace(topics=tuple(SimpleNamespace(
            topic_id=topic['topicId'], title=topic['title'], query_term=query.query,
            query_enabled=True) for topic, query in zip(external_topics, research_queries, strict=True)),
            queries=research_queries)
        supplement = await supplement_evidence(research_agent, research_plan,
            SimpleNamespace(by_topic=by_topic), scope=research_scope, deadline=deadline)
        bundle.query_audits.extend(supplement.query_audits)
        bundle.recalled_records.extend(supplement.records)
        pool = _merge_pool(bundle, pool, supplement.records)
        await _emit(audit, 'research_recall', {'queryCount': len(supplement.query_audits),
            'candidateCount': len(supplement.records)})
    bundle._selection_fingerprints = _source_fingerprints(pool)
    bundle._topic_links = await _select_applicable_links(narrative_client, pool, topics,
        blueprint=blueprint, subject=subject, deadline=deadline, audit=audit)
    bundle._selector_calls += int(callable(getattr(narrative_client, 'generate', None)))
    _compile_bundle(bundle, pool, topics)
    await _emit(audit, 'retrieval_complete', {'queryCount': len(bundle.query_audits),
        'recalledRecordCount': len(bundle.recalled_records), 'literalRecordCount': len(bundle.records),
        'publicationCount': len(bundle.references), 'gapCount': len(bundle.gaps)})
    return bundle


def _source_fingerprints(records):
    return {identity: hashlib.sha256(json.dumps(sorted({(
        str(_get(row, 'title', default='')), str(_get(row, 'year', default='')),
        str(_get(row, 'doi', default='')), evaluation_original_text(row)) for row in blocks}),
        ensure_ascii=False).encode()).hexdigest() for identity, blocks in _group_records(records).items()}


def _reviewed_numbers(audit):
    numbers = set()
    if not isinstance(audit, dict):
        return numbers
    def walk(value):
        if isinstance(value, dict):
            if 'evidence' in value and isinstance(value['evidence'], list):
                for item in value['evidence']:
                    number = item.get('number') if isinstance(item, dict) else item
                    if isinstance(number, int) and not isinstance(number, bool):
                        numbers.add(number)
            for item in value.values():
                walk(item)
        elif isinstance(value, (tuple, list)):
            for item in value:
                walk(item)
    walk(audit.get('reviewedBlocks', {}))
    return numbers


def _ordinary_query_count(bundle):
    # Research owns a separately bounded ledger. Its audit stays in the
    # report, without consuming the ordinary pipeline's reserved gap recall.
    return sum(not any(_get(channel, 'provider') == 'research_v1'
        for channel in _get(row, 'channels', default=()) or ())
        for row in bundle.query_audits)


async def recall_report_evidence(evidence_client, public_client, *, blueprint, subject,
        bundle, gaps, deadline, audit=None):
    """One real review-gap round, stable numbers and effective pool reselection."""
    bundle.changed_source_numbers = []
    if bundle._review_recall_used:
        return False
    bundle._review_recall_used = True
    target_ids, topic_ids = set(), set()
    for gap in gaps or ():
        if not isinstance(gap, dict):
            continue
        target_ids.update(_strings(gap.get('targetIds', ())))
        target_ids.update(_strings(gap.get('targetId')))
        topic_ids.update(_strings(gap.get('topicIds', ())))
        topic_ids.update(_strings(gap.get('topicId')))
    requested = [topic for topic in _round_robin_topics(bundle.plan)
        if topic['topicId'] in topic_ids or target_ids & set(topic['targetIds'])]
    limit = min(bundle._review_recall_limit, max(0, bundle._query_budget - _ordinary_query_count(bundle)))
    client = public_client if _enabled(public_client) else evidence_client
    if not requested or limit <= 0 or not _enabled(client) or time.monotonic() >= deadline:
        return False
    attempted = {row.query_id for row in bundle.query_audits}
    jobs = []
    for topic in requested:
        aliases = _clinical_aliases(topic)
        alternatives = [topic, *({**topic, 'aliases': [alias]} for alias in aliases)]
        query = next((candidate for candidate in (_query(option, recall=True,
            public=client is public_client, age_group=_age_group(subject)) for option in alternatives)
            if candidate.query_id not in attempted), None)
        if query is not None:
            jobs.append((client, topic, query))
            attempted.add(query.query_id)
        if len(jobs) >= limit:
            break
    if not jobs:
        return False
    old_view = {(row['number'], hashlib.sha256(row['sourceText'].encode()).hexdigest(),
        tuple(row.get('targetIds', ()))) for row in bundle.summaries}
    recalled, rows = await _run_jobs(jobs, public_client=public_client, deadline=deadline, concurrency=8)
    bundle.query_audits.extend(rows)
    if client is public_client:
        try:
            recalled.extend(await _enrich_originals(public_client, recalled, [job[1] for job in jobs], deadline=deadline))
        except asyncio.CancelledError:
            raise
        except Exception:
            bundle.gaps.append({'reason': 'public_original_enrichment_unavailable'})
    pool = _merge_pool(bundle, bundle.recalled_records, recalled)
    bundle.recalled_records.extend(recalled)
    fingerprints = _source_fingerprints(pool)
    changed_ids = {identity for identity, fingerprint in fingerprints.items()
        if fingerprint != bundle._selection_fingerprints.get(identity)}
    for identity in changed_ids:
        bundle._topic_links.pop(identity, None)
    # A late source is never allowed around the initial applicability selector.
    # One bounded initial + one actual-gap selector share this request lifetime.
    if changed_ids and bundle._selector_calls < 2:
        changed_records = [record for record in pool if evaluation_publication_identity(record) in changed_ids]
        selector_client = bundle._selector_client() if callable(bundle._selector_client) else None
        selected = await _select_applicable_links(selector_client, changed_records,
            [job[1] for job in jobs], blueprint=blueprint, subject=subject, deadline=deadline, audit=audit)
        bundle._topic_links.update(selected)
        bundle._selector_calls += int(callable(getattr(selector_client, 'generate', None)))
    bundle._selection_fingerprints = fingerprints
    bundle._reviewed_numbers.update(_reviewed_numbers(audit))
    bundle._priority_topic_ids = {job[1]['topicId'] for job in jobs}
    _compile_bundle(bundle, pool, _round_robin_topics(bundle.plan))
    new_view = {(row['number'], hashlib.sha256(row['sourceText'].encode()).hexdigest(),
        tuple(row.get('targetIds', ()))) for row in bundle.summaries}
    changed = old_view != new_view
    await _emit(audit, 'retrieval_gap_recall', {'queryCount': len(rows),
        'totalQueryCount': len(bundle.query_audits), 'candidateLibraryChanged': changed,
        'changedSourceNumbers': list(bundle.changed_source_numbers),
        'requestedTopicIds': [job[1]['topicId'] for job in jobs]})
    return changed
