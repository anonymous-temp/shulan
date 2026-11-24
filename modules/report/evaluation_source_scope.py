"""Necessary literal source-scope gates; no scientific approval is inferred.

Only in-process receipts are returned. Public metadata, model ``general`` flags,
provider tags and a bibliography never manufacture scope permission.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from functools import lru_cache
from copy import deepcopy
from contextvars import ContextVar


def _norm(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', str(value or ''))).strip()


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


_SCOPE_TOKEN = object()
_CONDITION_TOKEN = object()
_MINTING_CONDITIONS = ContextVar('minting_source_clinical_conditions', default=frozenset())


@dataclass(frozen=True)
class SourceScopeReceipt:
    original_hash: str
    title_hash: str
    selected_quotes: tuple[str, ...]
    role: str
    claim_hash: str
    patient_facts_hash: str
    context_hash: str
    matched_premises: tuple[str, ...]
    permitted_scope: str
    condition_signatures: tuple[str, ...]
    _token: object = field(repr=False, compare=False)

    def __deepcopy__(self, memo):
        # All payload members are immutable. Keep the process-local token
        # through ordinary Pydantic/block deep copies without reissuing proof.
        return self


@dataclass(frozen=True)
class ScopeValidation:
    valid: bool
    errors: tuple[str, ...]
    receipt: SourceScopeReceipt | None = None


@dataclass(frozen=True)
class SourceClinicalConditionWitness:
    condition: str
    certainty: str
    fact_ids: tuple[str, ...]
    subject_hash: str
    authority_hash: str
    risk_id: str
    _document: object = field(repr=False, compare=False)
    _token: object = field(repr=False, compare=False)

    def __deepcopy__(self, memo):
        return self


def _condition_authority(document, risk_id):
    """Check current classification authority without calling approval again."""
    from .evaluation_risk_assessment import (_digest as risk_digest, _explicit_personal_disease_source)
    registry = getattr(document, 'risk_registry', None)
    if registry is None or not getattr(registry, '_proofs', {}).get(risk_id):
        return None
    row = next((row for row in registry.assessments if row.risk_id == risk_id), None)
    candidate = next((row for row in registry.candidates if row.risk_id == risk_id), None)
    if row is None or candidate is None or row.certainty not in {'confirmed', 'current_problem'}:
        return None
    facts = {fact.fact_id: fact for fact in document.facts}
    ids = set(row.support_fact_ids)
    literal, classifications = False, []
    for claim in row.claims:
        if (_explicit_personal_disease_source(claim, row, facts)
                and any(facts[fid].kind == 'clinical' for fid in claim.fact_quotes if fid in facts)):
            literal = True
            ids.update(claim.fact_quotes)
        seal = getattr(claim, '_classification_receipt', None)
        if seal is not None:
            current = risk_digest({'claim': claim.model_dump(), 'target': candidate.name,
                'certainty': candidate.certainty,
                'facts': {fid: facts[fid].model_dump() for fid in claim.fact_quotes if fid in facts},
                'sources': {anchor.evidence_key: registry._evidence_hashes.get(anchor.evidence_key)
                    for anchor in claim.evidence}})
            if seal == current:
                classifications.append(seal)
                ids.update(claim.fact_quotes)
    candidate_seal = getattr(candidate, '_current_classification_scope', None)
    current_candidate = candidate_seal == risk_digest(candidate.model_dump())
    if not literal and not classifications and not current_candidate:
        return None
    if not ids or not ids <= facts.keys():
        return None
    subject = dict(registry.subject_snapshot)
    authority = {'name': row.name, 'certainty': row.certainty,
        'facts': {fid: facts[fid].model_dump(mode='json', by_alias=True) for fid in sorted(ids)},
        'subject': subject, 'literal': literal, 'classifications': classifications,
        'candidateClassification': candidate_seal if current_candidate else None}
    return row, tuple(sorted(ids)), subject, _digest(authority)


def _live_condition_witness(witness, *, subject=None):
    if not isinstance(witness, SourceClinicalConditionWitness) or witness._token is not _CONDITION_TOKEN:
        return False
    current = _condition_authority(witness._document, witness.risk_id)
    if current is None or current[3] != witness.authority_hash or current[0].name != witness.condition:
        return False
    if subject is not None:
        from .evaluation_risk_assessment import _subject_view
        if _digest(_subject_view(subject)) != witness.subject_hash:
            return False
    return True


def source_clinical_condition_witnesses(document, *, subject=None):
    """Mint only from live approved assessments, never a numeric/JSON label."""
    if document is None or isinstance(document, Mapping):
        return ()
    registry = getattr(document, 'risk_registry', None)
    if registry is None:
        return ()
    existing = tuple(witness for witness in getattr(document, '_source_clinical_condition_witnesses', ())
        if isinstance(witness, SourceClinicalConditionWitness) and witness._document is document
        and _live_condition_witness(witness, subject=subject))
    key = id(document)
    active = _MINTING_CONDITIONS.get()
    if key in active:
        return existing
    token = _MINTING_CONDITIONS.set(active | {key})
    try:
        from .evaluation_risk_assessment import approved_assessments, _subject_view
        result = list(existing)
        known = {w.risk_id for w in result}
        for row in approved_assessments(document):
            if row.risk_id in known:
                continue
            authority = _condition_authority(document, row.risk_id)
            if authority is None:
                continue
            _, ids, snapshot, fingerprint = authority
            if subject is not None and _subject_view(subject) != snapshot:
                continue
            result.append(SourceClinicalConditionWitness(row.name, row.certainty, ids,
                _digest(snapshot), fingerprint, row.risk_id, document, _CONDITION_TOKEN))
        document._source_clinical_condition_witnesses = tuple(result)
        return tuple(result)
    finally:
        _MINTING_CONDITIONS.reset(token)


def _condition_witness_match(option, witnesses, context):
    if option['kind'] != 'condition' or option.get('logicalRole') != 'eligibility':
        return ()
    valid = [w for w in witnesses if _live_condition_witness(w, subject=context.get('subject'))]
    actual = set().union(*(_terms(w.condition + ' ' + _controlled_source_concepts(w.condition)) for w in valid)) if valid else set()
    branches = [_terms(value) for value in re.split(r'\bor\b|或者|或', option['value'], flags=re.I)]
    for branch in branches:
        if branch and branch <= actual:
            return tuple(w.authority_hash for w in valid if branch & _terms(w.condition + ' ' + _controlled_source_concepts(w.condition)))
    return ()


_PROTOCOL = re.compile(r'\bprotocol\b.{0,100}\b(?:trial|study|randomi[sz]ed|prospective)\b|'
    r'\b(?:trial|study|review) protocol\b|研究方案|研究计划', re.I)
_GENETIC = re.compile(r'\b([A-Z][A-Za-z0-9-]{1,15})\s+(?:gene\s+)?(?:mutations?|variants?|genotyp\w*)\b|'
    r'\b(?:mutations?|variants?)\s+(?:in|of)\s+([A-Z][A-Za-z0-9-]{1,15})\b')
_POPULATION = re.compile(r'\b(?:children|childhood|pediatric|paediatric|infants?|neonat\w*|adolescents?|'
    r'adults?|elderly|older adults?|women|female|men|male|pregnan\w*|maternal|postpartum)\b|'
    r'儿童|婴幼儿|新生儿|青少年|成人|老年|孕妇|妊娠|产后|女性|男性', re.I)
_COHORT = re.compile(r'\b(?:patients?|people|participants?|subjects?|individuals?|adults?|children|women|men)\s+'
    r'(?:(?:diagnosed|living)\s+)?(?:with|having|positive for|undergoing|receiving)\s+([^.;:\n]{3,180})|'
    r'(?:纳入|入组|招募|受试者为|研究对象为)([^。；;\n]{2,120})', re.I)
_SETTING = re.compile(r'\b(?:industrial|bioreactor|fermentation broth|engineered strains?|'
    r'cell[- ]line|in vitro|murine|mice|mouse|rats?|clinical trial|preoperative|postoperative)\b|'
    r'工业|发酵罐|工程菌|细胞系|体外实验|小鼠|大鼠|术前|术后|临床试验', re.I)
_AGE_RANGE = re.compile(r'\b(?:aged?|ages?)\s*(\d{1,3})\s*[-–—]\s*(\d{1,3})(?:\s*(?:years?|months?|weeks?|days?))?(?!\d)|'
    r'年龄\s*(\d{1,3})\s*[-–—至]\s*(\d{1,3})(?:\s*[岁月天])?(?!\d)', re.I)
_SITES = {'thyroid': ('thyroid', '甲状腺'), 'renal': ('renal', 'kidney', '肾'),
    'hepatic': ('hepatic', 'liver', '肝'), 'pulmonary': ('pulmonary', 'lung', '肺'),
    'colorectal': ('colorectal', 'colon', 'rectal', '结肠', '直肠'),
    'breast': ('breast', '乳腺'), 'gastric': ('gastric', 'stomach', '胃'),
    'cardiac': ('cardiac', 'heart', '心脏')}
_DEFINITION = re.compile(r'\b(?:is|are|refers? to|defined as|characteri[sz]ed by|consists? of)\b|'
    r'是指|定义为|指的是|属于|是一种', re.I)
_APPLICATION = re.compile(r'\b(?:your|this patient|increased? risk|higher risk|should|must|recommend|'
    r'predicts?|probability|treat|screen)\b|本次|该患者|您的|患病概率|风险升高|更高风险|应当|建议|需进行|可予', re.I)
_STOP = set('patients patient participants participant subjects subject individuals individual adults adult children '
    'with having positive negative diagnosed diagnosis presence absence mutations mutation variants variant gene '
    'study cohort population this that those these their were was are and for the who receiving undergoing '
    'clinical identified found based among from nodular group groups'.split())


@lru_cache(maxsize=128)
def _source_scope_options_all(source_text: str | None, title: str = ''):
    """Server-owned exact context witnesses for existing review payloads.

    Each reviewer must address every option. ``sourceQuote`` is never rewritten;
    genetic names are discovered from syntax rather than a patient/gene list.
    """
    if not isinstance(source_text, str) or not source_text.strip():
        return []
    chunks = [('title', title)] if title else []
    chunks += [('source', text.strip()) for text in re.split(r'(?<=[.!?。！？])\s+|[;；\r\n]+', source_text) if text.strip()]
    options = []
    seen = set()
    for origin, quote in chunks:
        tests = []
        if _PROTOCOL.search(quote):
            tests.append(('study_design', 'unfinished_study'))
        genes = [next(value for value in match.groups() if value) for match in _GENETIC.finditer(quote)]
        tests.extend(('genetic', gene) for gene in genes)
        populations = list(dict.fromkeys(match.group() for match in _POPULATION.finditer(quote)))
        # Mixed branches are alternatives, never an AND of adult + child or
        # male + female eligibility. Source clauses keep their own antecedent.
        age_population, sex_population, reproductive = [], [], []
        for value in populations:
            if re.search(r'women|female|\bmen\b|\bmale\b|女性|男性', value, re.I):
                sex_population.append(value)
            elif re.search(r'pregnan|maternal|postpartum|孕|妊娠|产后', value, re.I):
                reproductive.append(value)
            else:
                age_population.append(value)
        for dimension in (age_population, sex_population):
            if dimension:
                tests.append(('population', ' OR '.join(dimension)))
        tests.extend(('population', value) for value in reproductive)
        tests.extend(('age_range', match.group()) for match in _AGE_RANGE.finditer(quote))
        tests.extend(('site', site) for site, aliases in _SITES.items() if any(
            re.search(r'(?<![A-Za-z])' + re.escape(alias) + r'(?![A-Za-z])', quote, re.I) for alias in aliases))
        for match in _COHORT.finditer(quote):
            condition = match.group(1) or match.group(2)
            # Keep the governing condition, not the following recommendation
            # predicate (whose words cannot be patient eligibility facts).
            condition = re.split(r',\s*(?:(?:the|a|an)\s+)?(?:[A-Za-z][\w-]*\s+){0,4}'
                r'(?:recommends?|suggests?|advises?|endorses?|should|must)\b|'
                r'\s+(?:should|must|are advised|were treated|received)\b|[，,]\s*(?:建议|推荐|应当|应予)',
                condition, maxsplit=1, flags=re.I)[0].strip()
            tests.append(('condition', condition))
        tests.extend(('setting', match.group()) for match in _SETTING.finditer(quote))
        for kind, value in tests:
            signature = (kind, _norm(value).casefold(), quote)
            if signature in seen:
                continue
            seen.add(signature)
            options.append({'premiseId': 'scope:' + _digest([origin, kind, value, quote])[:20],
                'kind': kind, 'value': value, 'sourceQuote': quote, 'sourceOrigin': origin,
                'logicalRole': ('comparison_context' if kind not in {'genetic', 'study_design', 'population', 'age_range'}
                    and re.search(r'\b(?:control group|controls|compared with|comparison group)\b|对照组|比较组', quote, re.I)
                    else 'exclusion' if re.search(r'\b(?:excluded|exclusion|ineligible)\b|排除|不纳入', quote, re.I)
                    else 'eligibility'),
                'globalContext': origin == 'title' or kind == 'study_design'
                    or bool(re.search(r'\b(?:we\s+(?:enrolled|recruited)|we\s+included\s+(?:patients?|participants?|subjects?|adults?|children)|(?:patients?|participants?|subjects?)\s+(?:were|was)\s+(?:enrolled|recruited|eligible|included))\b|(?:本研究|本试验)(?:纳入|入组|招募)(?:患者|受试者|成人|儿童)', quote, re.I))})
    return tuple(options)


def source_scope_options(source_text: str | None, title: str = '', *, selected_quotes=None) -> list[dict]:
    """Return governing quote context, not all unrelated guideline branches."""
    options = deepcopy(list(_source_scope_options_all(source_text, title)))
    if selected_quotes is None:
        return options
    quotes = [_norm(q) for q in selected_quotes if isinstance(q, str) and q.strip()]
    chunks = [chunk.strip() for chunk in re.split(r'(?<=[.!?。！？])\s+|[;；\r\n]+', source_text or '') if chunk.strip()]
    antecedents = set()
    for index, chunk in enumerate(chunks):
        if any(q in _norm(chunk) or _norm(chunk) in q for q in quotes) and re.match(
                r'(?i)^(?:these|those|such|they|their|this group|in this group|上述|这些|该类|其)', chunk):
            antecedents.update(_norm(value) for value in chunks[max(0, index - 2):index])
    return [{**option, 'governingContext': True} for option in options if option['globalContext']
        or _norm(option['sourceQuote']) in antecedents
        or any(q in _norm(option['sourceQuote']) or _norm(option['sourceQuote']) in q for q in quotes)]


def _facts(values):
    result = {}
    for key, value in (values.items() if isinstance(values, Mapping) else enumerate(values or ())):
        if hasattr(value, 'model_dump'):
            value = value.model_dump(mode='json', by_alias=True)
        if not isinstance(value, Mapping):
            continue
        row = dict(value)
        identifier = row.get('factId', row.get('fact_id', key))
        result[str(identifier)] = row
    return result


def _terms(value):
    value = re.sub(r'gerd\s+symptoms?|symptoms?\s+of\s+gerd|gastroesophageal reflux(?:-related)?\s+symptoms?',
        'reflux-symptom', _norm(value).casefold())
    return {word.rstrip('s') for word in re.findall(r'[a-z][a-z0-9-]{3,}', value)
        if word not in _STOP}


def _controlled_source_concepts(value):
    # These aliases classify an already literal source expression. They never
    # turn a BMI/value, candidate endpoint or family history into a diagnosis.
    from .evaluation_catalog import FIXED_DISEASE_ALIASES
    normalized = _norm(value).casefold()
    groups = []
    for aliases in FIXED_DISEASE_ALIASES.values():
        affirmed = False
        for alias in aliases:
            literal = _norm(alias).casefold()
            match = (re.search(r'(?<![a-z0-9])' + re.escape(literal) + r'(?![a-z0-9])', normalized)
                if literal.isascii() else None)
            position = match.start() if match is not None else -1 if literal.isascii() else normalized.find(literal)
            if position >= 0 and not re.search(r'(?:无|非|否认|未见|not |non[- ])$', normalized[:position]):
                affirmed = True
        if affirmed:
            if '便秘' in aliases and not re.search(r'功能性|functional', normalized):
                groups.append(('constipation',))
            else:
                groups.append(aliases)
    if re.search(r'反酸|烧心|acid regurgitation|heartburn|reflux symptoms|gerd\s*symptoms?', normalized):
        groups.append(('reflux-symptom', 'acid regurgitation', 'heartburn'))
    diagnostic = re.sub(r'gerd\s*(?:symptoms?|症状)|symptoms?\s+of\s+gerd', '', normalized)
    if re.search(r'胃食管反流病|gastroesophageal reflux disease|\bgerd\b', diagnostic):
        groups.append(('GERD', 'gastroesophageal reflux disease'))
    return ' '.join(alias for group in groups for alias in group)


def _literal_fact_quotes(row, facts):
    quotes = row.get('factQuotes', {})
    if isinstance(quotes, list):
        quotes = {item.get('factId'): item.get('quote', item.get('factQuote')) for item in quotes if isinstance(item, Mapping)}
    if not isinstance(quotes, Mapping):
        return []
    result = []
    for fid in row.get('factIds', []):
        fact, quote = facts.get(str(fid)), quotes.get(str(fid))
        if fact and isinstance(quote, str) and quote.strip() and quote in str(fact.get('value', '')):
            result.append((fact, quote))
    return result


def _patient_matches(option, row, facts, context):
    subject = context.get('subject') or {}
    value = _norm(option['value']).casefold()
    if option.get('logicalRole') == 'exclusion' and option['kind'] in {'population', 'age_range'}:
        if re.search(r'women|female|\bmen\b|\bmale\b|女性|男性', value):
            known = str(subject.get('gender', subject.get('sex', ''))).casefold() in {'男', '女', 'male', 'female', 'm', 'f'}
        else:
            known = isinstance(subject.get('age'), (int, float)) and not isinstance(subject.get('age'), bool)
        return known and not _patient_matches({**option, 'logicalRole': 'eligibility'}, row, facts, context)
    if option['kind'] == 'age_range':
        age = subject.get('age')
        for unit, key in ((r'month|月', 'ageMonths'), (r'week|周', 'ageWeeks'), (r'day|天', 'ageDays')):
            if re.search(unit, value, re.I):
                age = subject.get(key)
                break
        limits = [int(v) for v in re.findall(r'\d+', value)]
        return len(limits) == 2 and isinstance(age, (int, float)) and not isinstance(age, bool) and limits[0] <= age <= limits[1]
    if option['kind'] == 'population':
        if ' OR ' in option['value']:
            return any(_patient_matches({**option, 'value': part}, row, facts, context)
                for part in option['value'].split(' OR '))
        age = subject.get('age')
        sex = str(subject.get('gender', subject.get('sex', ''))).casefold()
        if re.search(r'neonat|新生儿', value):
            days = subject.get('ageDays')
            return type(days) in (int, float) and 0 <= days <= 28
        if re.search(r'infant|婴幼儿', value):
            return type(age) in (int, float) and 0 <= age < 1
        if re.search(r'adolescen|青少年', value):
            return type(age) in (int, float) and 10 <= age < 18
        if re.search(r'child|pediatric|paediatric|儿童', value):
            return isinstance(age, (int, float)) and not isinstance(age, bool) and age < 18
        if re.search(r'adult|成人', value) and not re.search(r'older|老年', value):
            return isinstance(age, (int, float)) and not isinstance(age, bool) and age >= 18
        if re.search(r'elderly|older|老年', value):
            return isinstance(age, (int, float)) and not isinstance(age, bool) and age >= 65
        if re.search(r'women|female|女性', value):
            return sex in {'女', 'female', 'f'}
        if re.search(r'\bmen\b|\bmale\b|男性', value):
            return sex in {'男', 'male', 'm'}
    bound = _literal_fact_quotes(row, facts)
    if not bound:
        return False
    aliases = ' '.join(str(x) for x in context.get('source_concepts', ()))
    allowed = set(context.get('problem_fact_ids', ()))
    for fact, quote in bound:
        fid = str(fact.get('factId', fact.get('fact_id', '')))
        if allowed and fid not in allowed:
            continue
        if option['kind'] == 'condition' and fact.get('kind') not in {'clinical', 'self_reported'}:
            continue
        literal = ' '.join((str(fact.get('name', '')), quote))
        negative = bool(re.search(r'否认|未见|没有|阴性|未检出|野生型|wild[- ]?type|^无(?:$|[，。；])|^(?:否|false|0)$|\bno\b|\bnot\b|\bnegative\b', quote, re.I))
        if negative != (option.get('logicalRole') == 'exclusion'):
            continue
        if option['kind'] == 'genetic':
            if (fact.get('kind') != 'clinical' or re.search(r'家族|父亲|母亲|兄弟|姐妹|\b(?:mother|father|family|sibling)\b', literal + ' ' + str(fact.get('source', '')), re.I)
                    or not re.search(r'(?<![A-Za-z0-9])' + re.escape(option['value']) + r'(?![A-Za-z0-9])', literal, re.I)):
                continue
            uncertain = r'可能|疑似|待定|不确定|uncertain|suspected|possible|\bVUS\b'
            if re.search(uncertain, literal, re.I) and not re.search(uncertain, option['sourceQuote'], re.I):
                continue
            if re.search(r'mutat|variant|positive|detected|检出|突变|变异|阳性', literal, re.I):
                return True
            continue
        if option['kind'] == 'site':
            if any(re.search(r'(?<![A-Za-z])' + re.escape(alias) + r'(?![A-Za-z])', literal + ' ' + aliases, re.I)
                    for alias in _SITES[option['value']]):
                return True
            continue
        actual = _terms(literal + ' ' + aliases + ' ' + _controlled_source_concepts(quote))
        branches = re.split(r'\bor\b|或者|或', option['value'], flags=re.I)
        required_branches = [_terms(branch) for branch in branches]
        if any(required and required <= actual for required in required_branches):
            return True
        if not any(required_branches) and _norm(option['value']) in _norm(literal):
            return True
    return False


def validate_source_scope(*, source_text: str | None, title: str = '', selected_quotes: Sequence[str],
        role: str, claim: str, patient_facts, target_context: Mapping | None = None,
        review_scope: Mapping | None = None, verified_conditions=()) -> ScopeValidation:
    """Necessary scope gate after ordinary original-text/scientific reviews.

    Review schema: ``premiseMatches=[{premiseId, sourceQuote, result:'met'|
    'not_applicable', factIds:[], factQuotes:{factId:exactOriginalQuote}}]``.
    Known subject age/sex is checked directly; other requirements need literal
    same-problem facts. A definition receipt cannot authorize personal risk.
    """
    errors = []
    if not isinstance(source_text, str) or not source_text.strip():
        return ScopeValidation(False, ('missing_original',))
    quotes = tuple(selected_quotes or ())
    if not quotes or any(not isinstance(q, str) or not q.strip() or _norm(q) not in _norm(source_text) for q in quotes):
        return ScopeValidation(False, ('selected_quote_not_in_original',))
    facts = _facts(patient_facts)
    context, scope = dict(target_context or {}), dict(review_scope or {})
    conditions = tuple(verified_conditions or ())
    if any(not _live_condition_witness(witness, subject=context.get('subject')) for witness in conditions):
        return ScopeValidation(False, ('untrusted_or_stale_clinical_condition',))
    used_conditions = set()
    options = source_scope_options(source_text, title, selected_quotes=quotes)
    definitions = role in {'definition', 'disease_meaning', 'general_function'}
    unrestricted = (definitions and all(_DEFINITION.search(q) and not any(pattern.search(q)
        for pattern in (_GENETIC, _COHORT, _SETTING, _PROTOCOL, _POPULATION)) for q in quotes)
        and not _APPLICATION.search(claim))
    rows = scope.get('premiseMatches', [])
    rows = rows if isinstance(rows, list) else []
    matches = {row.get('premiseId'): row for row in rows if isinstance(row, Mapping)}
    all_ids = {option['premiseId'] for option in source_scope_options(source_text, title)}
    if len(matches) != len(rows) or any(key not in all_ids for key in matches):
        errors.append('unknown_or_duplicate_scope_premise')
    for option in options:
        row = matches.get(option['premiseId'])
        if unrestricted:
            # Only the actual selected independent definition is reusable.
            # This receipt remains definition-only regardless of model flags.
            continue
        relevant = option.get('governingContext') or option['globalContext'] or any(_norm(q) in _norm(option['sourceQuote']) or _norm(option['sourceQuote']) in _norm(q) for q in quotes)
        if row is None:
            errors.append('scope_premise_omitted:' + option['premiseId'])
            continue
        if _norm(row.get('sourceQuote')) != _norm(option['sourceQuote']):
            errors.append('scope_context_quote_mismatch:' + option['premiseId'])
            continue
        if row.get('result') == 'not_applicable' and not relevant:
            continue
        if option.get('logicalRole') == 'comparison_context' and row.get('result') == 'context_preserved':
            # A reference group is not the patient's inclusion requirement.
            # The separate scientific/comparison-scope review remains needed.
            continue
        if option['kind'] == 'study_design':
            errors.append('unfinished_study_cannot_support_application')
        elif row.get('result') != 'met':
            errors.append('patient_scope_requirement_not_met:' + option['premiseId'])
        elif not _patient_matches(option, row, facts, context):
            signatures = _condition_witness_match(option, conditions, context)
            if signatures:
                used_conditions.update(signatures)
            else:
                errors.append('patient_scope_requirement_not_met:' + option['premiseId'])
    if definitions and options and not unrestricted:
        # Calling a cohort-specific claim "general" cannot remove its scope.
        if not rows:
            errors.append('restricted_source_is_not_unrestricted_definition')
    if errors:
        return ScopeValidation(False, tuple(dict.fromkeys(errors)))
    receipt = SourceScopeReceipt(_digest(source_text), _digest(title), quotes, role, _digest(claim),
        _digest(facts), _digest(context), tuple(o['premiseId'] for o in options),
        'definition_only' if unrestricted else 'conditional_application', tuple(sorted(used_conditions)), _SCOPE_TOKEN)
    return ScopeValidation(True, (), receipt)


def source_scope_receipt_matches(receipt, *, source_text, title='', selected_quotes, role,
        claim, patient_facts, target_context=None, verified_conditions=()):
    """Verify an actual same-process receipt against its exact current owner."""
    conditions = tuple(verified_conditions or ())
    return bool(isinstance(receipt, SourceScopeReceipt) and receipt._token is _SCOPE_TOKEN
        and all(_live_condition_witness(witness, subject=dict(target_context or {}).get('subject')) for witness in conditions)
        and set(receipt.condition_signatures) <= {w.authority_hash for w in conditions}
        and receipt.original_hash == _digest(source_text) and receipt.title_hash == _digest(title)
        and receipt.selected_quotes == tuple(selected_quotes or ()) and receipt.role == role
        and receipt.claim_hash == _digest(claim) and receipt.patient_facts_hash == _digest(_facts(patient_facts))
        and receipt.context_hash == _digest(dict(target_context or {})))


def source_candidate_scope_errors(*, source_text, title='', patient_facts, target_context=None, selected_quotes=None):
    """Cheap necessary candidate filtering, never a review receipt/approval."""
    facts = _facts(patient_facts)
    row = {'factIds': list(facts), 'factQuotes': {key: str(fact.get('value', '')) for key, fact in facts.items()}}
    context = dict(target_context or {})
    errors = []
    for option in source_scope_options(source_text, title, selected_quotes=selected_quotes):
        if (selected_quotes is None and not option['globalContext']) or option.get('logicalRole') == 'comparison_context':
            continue
        if option['kind'] == 'study_design':
            errors.append('unfinished_study')
        elif not _patient_matches(option, row, facts, context):
            errors.append('source_' + option['kind'] + '_not_in_patient_context')
    return tuple(dict.fromkeys(errors))
