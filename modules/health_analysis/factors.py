"""Bounded, source-backed risk discovery and synchronous personal matching."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from datetime import timezone

from agents.knowledge.errors import KnowledgeRetrievalError
from agents.knowledge.types import KnowledgeQuery
from agents.knowledge.types import KnowledgeSource
from modules.health_analysis.contracts import AnalysisCapacityError
from modules.health_analysis.contracts import AnalysisGenerationError
from modules.health_analysis.contracts import AnalysisTimeoutError
from modules.health_analysis.contracts import KnowledgeUnavailableError


class HealthAnalysisGenerationError(AnalysisGenerationError):
    """An upstream generation contract failed; messages contain no health data."""


def check_deadline(deadline_at) -> None:
    if deadline_at is not None and asyncio.get_running_loop().time() >= deadline_at:
        raise AnalysisTimeoutError('Health Analysis execution deadline exceeded')


_IDENTITY_KEYS = frozenset(
    {
        'userid',
        'groupid',
        'senderid',
        'sendid',
        'messageid',
        'servermsgid',
        'clientmsgid',
        'documentid',
        'subjectid',
        'patientid',
        'recordid',
        'runid',
        'fileid',
        'username',
        'patientname',
        'realname',
        'phone',
        'mobile',
        'email',
        'idcard',
    }
)
_CONTACT_QUERY = re.compile(
    r'[a-z][a-z0-9+.-]*://|www\.|\b[a-z0-9.-]+\.(?:com|cn|net|org|edu|gov|io|info)\b'
    r'|[^\s@]+@[^\s@]+\.[^\s@]+'
    r'|(?<!\d)(?:\+?\d[\d ()-]{5,}\d)(?!\d)',
    re.IGNORECASE,
)


def _query_normalize(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def _identity_values(value) -> set[str]:
    result = set()
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = re.sub(r'[_-]', '', key).casefold() if isinstance(key, str) else ''
            if normalized_key in _IDENTITY_KEYS:
                if isinstance(item, str) and item.strip():
                    result.add(item)
                elif isinstance(item, int) and not isinstance(item, bool):
                    result.add(str(item))
            result.update(_identity_values(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_identity_values(item))
    return result


def _query_contains(query: str, value: str) -> bool:
    normalized = _query_normalize(value)
    if not normalized:
        return False
    # Short numeric identifiers must not prohibit general concepts such as
    # 2型糖尿病. Longer identifiers still match next to Chinese prose.
    if normalized.isdecimal() and len(normalized) < 4:
        return re.search(r'(?<!\w)' + re.escape(normalized) + r'(?!\w)', query) is not None
    return normalized in query


def safe_medical_query(value, materials: list[dict], *, exclusion_values=()) -> str:
    """Block known identity/contact values and verbatim personal input locally.

    This is a minimum disclosure guard, not a semantic anonymization guarantee.
    Rejected strings never enter the returned target or its diagnostic record.
    """
    query = text(value, 'general medical query', maximum=128)
    normalized = _query_normalize(query)
    if _CONTACT_QUERY.search(normalized):
        raise HealthAnalysisGenerationError('General medical query contains private or contact material')
    identities = _identity_values(materials)
    identities.update(item for item in exclusion_values if isinstance(item, str) and item.strip())
    if any(_query_contains(normalized, item) for item in identities):
        raise HealthAnalysisGenerationError('General medical query contains a known identity value')
    for material in materials:
        if material.get('kind') not in {'chat', 'answer'}:
            continue
        content = material.get('content')
        originals = [content] if isinstance(content, str) else []
        if isinstance(content, dict):
            readable = content.get('readableAnswer')
            if isinstance(readable, str):
                originals.append(readable)
            if (content.get('question') or {}).get('type') == 'text' and isinstance(content.get('rawValue'), str):
                originals.append(content['rawValue'])
        if any(_query_contains(normalized, original) for original in originals):
            raise HealthAnalysisGenerationError('General medical query contains verbatim personal input')
    return query


def stable_hash(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def text(value, field: str, *, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise HealthAnalysisGenerationError(f'{field} is invalid')
    return value.strip()


def array(value, field: str) -> list:
    if not isinstance(value, list):
        raise HealthAnalysisGenerationError(f'{field} must be an array')
    return value


def refs(value, allowed: set[str], field: str, *, required: bool = False) -> list[str]:
    rows = array(value, field)
    if any(not isinstance(item, str) or item not in allowed for item in rows) or (required and not rows):
        raise HealthAnalysisGenerationError(f'{field} references unavailable material')
    return list(dict.fromkeys(rows))


def partition(rows: list[dict], *, max_chars: int, max_batches: int) -> list[list[dict]]:
    groups, current, size = [], [], 0
    for row in rows:
        count = len(json.dumps(row, ensure_ascii=False, allow_nan=False))
        if count > max_chars:
            raise AnalysisCapacityError('A material exceeds the offline execution guard')
        if current and size + count > max_chars:
            groups.append(current)
            current, size = [], 0
        current.append(deepcopy(row))
        size += count
    if current:
        groups.append(current)
    if len(groups) > max_batches:
        raise AnalysisCapacityError('Material batches exceed the offline execution guard')
    return groups


def personal_quote_units(material: dict) -> list[str]:
    """Answers expose actual readable selections, never question/choice metadata."""
    content = material.get('content')
    if isinstance(content, str):
        return [content]
    if material.get('kind') != 'answer':
        return reported_quote_units(content)
    if not isinstance(content, dict):
        return []
    readable = content.get('readableAnswer')
    if not isinstance(readable, str) or not readable.strip():
        return []
    units = [readable]
    question = content.get('question')
    selected = content.get('value')
    if isinstance(question, dict) and question.get('type') == 'checkbox' and isinstance(selected, list):
        if selected == ['none'] and question.get('showNoneItem'):
            actual_units = [question.get('noneText', '以上均无')]
        else:
            choices = {choice['value']: choice.get('text', choice['value']) for choice in question.get('choices', [])}
            if any(not isinstance(value, str) or value not in choices for value in selected):
                return []
            actual_units = [choices[value] for value in selected]
        # Keep whole selected units as well as the whole multi-answer string.
        # No partial substring may remove a denial, condition or time qualifier.
        if readable != '\n'.join(actual_units):
            return []
        units.extend(actual_units)
    return list(dict.fromkeys(units))


def reported_quote_units(value) -> list[str]:
    """Only actual result/narrative fields can support a patient condition.

    Item labels, reference intervals, units, dates and identity metadata remain
    available as context but cannot by themselves establish a patient result.
    """
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return [str(value)]
    if isinstance(value, list):
        return [unit for item in value for unit in reported_quote_units(item)]
    if isinstance(value, dict):
        fields = {
            'resultValue',
            'rawValue',
            'value',
            'result',
            'text',
            'rawText',
            'findings',
            'diagnosis',
            'record',
            'observation',
        }
        units = [unit for key, item in value.items() if key in fields for unit in reported_quote_units(item)]
        context = value.get('reportContext')
        context_fields = {
            'age',
            'ageYears',
            'ageMonths',
            'ageDays',
            'lifeStage',
            'physiologicalStage',
            'specialStage',
            'pregnancyStatus',
            'lactationStatus',
            'pregnant',
            'lactating',
            'breastfeeding',
            'gestationalWeeks',
            'pregnancyWeeks',
            'gestationalAge',
        }
        numeric_context_fields = {
            'age',
            'ageYears',
            'ageMonths',
            'ageDays',
            'gestationalWeeks',
            'pregnancyWeeks',
            'gestationalAge',
        }
        if isinstance(context, list):
            for row in context:
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get('field'), str)
                    or row['field'] not in context_fields
                ):
                    continue
                actual = row.get('value')
                if isinstance(actual, bool) and row['field'] not in numeric_context_fields:
                    units.append('true' if actual else 'false')
                elif isinstance(actual, (str, int, float)) and not isinstance(actual, bool):
                    units.append(str(actual))
        return units
    return []


def personal_citations(value, materials: dict[str, dict]) -> list[dict]:
    result = []
    for row in array(value, 'personal citations'):
        if (
            not isinstance(row, dict)
            or not isinstance(row.get('evidenceId'), str)
            or row['evidenceId'] not in materials
        ):
            raise HealthAnalysisGenerationError('Personal citation references unavailable material')
        quote = text(row.get('quote'), 'personal quote', maximum=20000)
        material = materials[row['evidenceId']]
        normalized = ' '.join(quote.split())
        if normalized not in {' '.join(unit.split()) for unit in personal_quote_units(material)}:
            raise HealthAnalysisGenerationError('Personal citation is not a complete source unit')
        if material.get('status') == 'unknown' or normalized.lower() in {
            '不清楚',
            '不知道',
            '不确定',
            '记不清',
            'unknown',
            'unsure',
        }:
            raise HealthAnalysisGenerationError('Unknown material cannot establish a personal condition')
        result.append({'evidenceId': row['evidenceId'], 'quote': quote})
    return result


def source_material(document) -> dict | None:
    """Only transport-returned original passages qualify; AI summaries do not."""
    metadata = document.metadata
    if not isinstance(metadata, Mapping):
        return None
    passages = []
    raw_passages = metadata.get('sourceTexts', [])
    if not isinstance(raw_passages, (list, tuple)):
        return None
    for index, row in enumerate(raw_passages):
        if (
            isinstance(row, Mapping)
            and row.get('kind') in {'abstract', 'fullText', 'guideBlock'}
            and isinstance(row.get('text'), str)
            and row['text'].strip()
        ):
            passages.append(
                {
                    'id': f'p{index + 1}',
                    'text': row['text'],
                    'kind': row['kind'],
                    'locator': deepcopy(row.get('locator') or {'field': row['kind'], 'index': index}),
                }
            )
    if not passages:
        return None
    digest = stable_hash(passages)
    return {
        'id': 'ks_' + stable_hash([str(document.source), metadata.get('id'), document.url, digest])[:24],
        'sourceId': metadata.get('id'),
        'channel': str(document.source),
        'title': document.title,
        'url': document.url,
        'contentHash': digest,
        'version': metadata.get('knowledgeBaseRevision') or metadata.get('version') or digest,
        'retrievedAt': datetime.now(timezone.utc).isoformat(),
        'passages': passages,
    }


def medical_citations(value, sources: dict[str, dict], *, required: bool = True) -> list[dict]:
    result = []
    for row in array(value, 'medical citations'):
        if not isinstance(row, dict) or not isinstance(row.get('sourceId'), str) or row['sourceId'] not in sources:
            raise HealthAnalysisGenerationError('Medical citation references unavailable material')
        source = sources[row['sourceId']]
        passage = next((p for p in source['passages'] if p['id'] == row.get('passageId')), None)
        quote = text(row.get('quote'), 'medical quote', maximum=12000)
        if passage is None or ' '.join(quote.split()) not in ' '.join(passage['text'].split()):
            raise HealthAnalysisGenerationError('Medical quote is absent from the actual passage')
        result.append(
            {
                'sourceId': source['id'],
                'passageId': passage['id'],
                'quote': quote,
                'sourceVersion': source['version'],
                'locator': deepcopy(passage['locator']),
            }
        )
    if required and not result:
        raise HealthAnalysisGenerationError('Medical citations are required')
    return result


class FactorAnalysis:
    VERSION = 'health-analysis-factors-v1'

    def __init__(
        self,
        knowledge=None,
        *,
        semaphore=None,
        max_targets=4,
        max_candidates=12,
        max_material_batches=8,
        batch_chars=24000,
        knowledge_timeout_seconds=25,
    ):
        self.knowledge = knowledge
        self.semaphore = semaphore or asyncio.Semaphore(2)
        self.max_targets, self.max_candidates = max_targets, max_candidates
        self.max_material_batches, self.batch_chars = max_material_batches, batch_chars
        self.knowledge_timeout_seconds = knowledge_timeout_seconds

    async def analyze(self, materials: list[dict], call, *, deadline_at=None, query_exclusion_values=()) -> dict:
        trace = {
            'version': self.VERSION,
            'targets': [],
            'sources': [],
            'candidates': [],
            'retrieval': [],
            'filters': [],
            'coveredMaterialIds': [],
            'unresearchedTargetIds': [],
        }
        targets = {}
        for group in partition(materials, max_chars=self.batch_chars, max_batches=self.max_material_batches):
            output = await call('targets', {'materials': group})
            allowed = {row['id']: row for row in group}
            covered = refs(output.get('coveredMaterialIds'), set(allowed), 'coveredMaterialIds')
            if set(covered) != set(allowed):
                raise HealthAnalysisGenerationError('Target discovery omitted personal materials')
            trace['coveredMaterialIds'].extend(covered)
            for raw in array(output.get('targets'), 'targets'):
                try:
                    target = self._target(
                        raw, allowed, query_materials=materials, query_exclusion_values=query_exclusion_values
                    )
                except HealthAnalysisGenerationError:
                    trace['filters'].append({'stage': 'targets', 'reason': 'invalid_target'})
                    continue
                if target['id'] in targets:
                    targets[target['id']]['personalEvidence'].extend(target['personalEvidence'])
                else:
                    targets[target['id']] = target
        trace['targets'] = list(targets.values())
        selected = trace['targets'][: self.max_targets]
        trace['unresearchedTargetIds'] = [row['id'] for row in trace['targets'][self.max_targets :]]
        for target in selected:
            check_deadline(deadline_at)
            sources, retrieval = await self._retrieve(target, deadline_at=deadline_at)
            trace['retrieval'].extend(retrieval)
            trace['sources'].extend(sources)
            if not sources:
                continue
            output = await call('research', {'target': target, 'sources': sources})
            applicable = refs(output.get('applicableSourceIds'), {s['id'] for s in sources}, 'applicableSourceIds')
            if not applicable:
                trace['retrieval'].append({'targetId': target['id'], 'status': 'inapplicable'})
                continue
            by_id = {s['id']: s for s in sources if s['id'] in applicable}
            for raw in array(output.get('candidates'), 'candidates'):
                if len(trace['candidates']) >= self.max_candidates:
                    trace['filters'].append({'stage': 'research', 'reason': 'candidate_budget'})
                    break
                try:
                    candidate = self._candidate(raw, target, by_id)
                except HealthAnalysisGenerationError:
                    trace['filters'].append({'stage': 'research', 'reason': 'invalid_candidate'})
                    continue
                if any(row['id'] == candidate['id'] for row in trace['candidates']):
                    continue
                assessment = await call('match', {'candidate': candidate, 'materials': materials})
                try:
                    candidate['assessment'] = self._match(assessment, candidate, {m['id']: m for m in materials})
                except HealthAnalysisGenerationError:
                    trace['filters'].append({'stage': 'match', 'reason': 'invalid_personal_assessment'})
                    continue
                trace['candidates'].append(candidate)
        trace['sources'] = list({row['id']: row for row in trace['sources']}.values())
        return trace

    @staticmethod
    def _target(raw, materials, *, query_materials=None, query_exclusion_values=()):
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get('kind'), str)
            or raw['kind'] not in {'indicator', 'symptom', 'disease', 'health_goal'}
        ):
            raise HealthAnalysisGenerationError('Target is invalid')
        label = text(raw.get('label'), 'target label', maximum=160)
        evidence = personal_citations(raw.get('personalEvidence'), materials)
        if not evidence:
            raise HealthAnalysisGenerationError('Target requires personal evidence')
        return {
            'id': 't_' + stable_hash([label, raw['kind']])[:20],
            'label': label,
            'kind': raw['kind'],
            'query': safe_medical_query(
                raw.get('query'),
                list(materials.values()) if query_materials is None else query_materials,
                exclusion_values=query_exclusion_values,
            ),
            'interpretation': text(raw.get('interpretation'), 'risk interpretation'),
            'personalEvidence': evidence,
        }

    async def _retrieve(self, target, *, deadline_at=None):
        check_deadline(deadline_at)
        if self.knowledge is None or not self.knowledge.enabled:
            return [], [{'targetId': target['id'], 'status': 'disabled'}]

        async def channel(source):
            try:
                check_deadline(deadline_at)
                async with self.semaphore, asyncio.timeout(self.knowledge_timeout_seconds):
                    check_deadline(deadline_at)
                    documents = await self.knowledge.retrieve(
                        KnowledgeQuery(
                            source=source,
                            query=target['query'],
                            count=3,
                            include_source_text=True,
                        )
                    )
            except AnalysisTimeoutError:
                raise
            except (KnowledgeRetrievalError, KnowledgeUnavailableError, TimeoutError, ConnectionError):
                return [], {'targetId': target['id'], 'channel': str(source), 'status': 'failure'}
            materials = [material for document in documents if (material := source_material(document))]
            status = 'available' if materials else 'no_original_text' if documents else 'empty'
            return materials, {'targetId': target['id'], 'channel': str(source), 'status': status}

        pending = [
            asyncio.create_task(channel(source)) for source in (KnowledgeSource.GUIDE, KnowledgeSource.LITERATURE)
        ]
        try:
            results = await asyncio.gather(*pending)
        except BaseException:
            # Cleanup only: cancellation and programming errors retain their
            # original identity, and no sibling query outlives a failed run.
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise
        return [m for materials, _ in results for m in materials], [state for _, state in results]

    @staticmethod
    def _candidate(raw, target, sources):
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get('relationType'), str)
            or raw['relationType'] not in {'risk_factor', 'trigger', 'aggravating_factor', 'possible_contributor'}
            or raw.get('materialSupportsRelation') is not True
            or raw.get('applicabilityReviewed') is not True
        ):
            raise HealthAnalysisGenerationError('Candidate relation lacks assessed material support')
        concept = text(raw.get('concept'), 'candidate concept', maximum=160)
        conditions, seen = [], set()
        for row in array(raw.get('conditions'), 'candidate conditions'):
            if (
                not isinstance(row, dict)
                or not isinstance(row.get('kind'), str)
                or row['kind'] not in {'presence', 'temporality', 'applicability'}
            ):
                raise HealthAnalysisGenerationError('Candidate condition is invalid')
            condition_id = text(row.get('id'), 'condition identity', maximum=80)
            if condition_id in seen:
                raise HealthAnalysisGenerationError('Candidate condition identities are duplicated')
            seen.add(condition_id)
            conditions.append({'id': condition_id, 'kind': row['kind'], 'label': text(row.get('label'), 'condition')})
        if not {'presence', 'temporality'}.issubset({row['kind'] for row in conditions}):
            raise HealthAnalysisGenerationError('Candidate omits presence or temporality conditions')
        return {
            'id': 'c_' + stable_hash([target['id'], concept, raw['relationType']])[:20],
            'targetId': target['id'],
            'concept': concept,
            'relationType': raw['relationType'],
            'conditions': conditions,
            'knowledgeEvidence': medical_citations(raw.get('knowledgeEvidence'), sources),
            'supportExplanation': text(raw.get('supportExplanation'), 'medical relation support'),
            'causalityEstablished': False,
        }

    @staticmethod
    def _match(output, candidate, materials):
        rows, seen = [], set()
        conditions = {row['id'] for row in candidate['conditions']}
        for raw in array(output.get('conditions'), 'personal condition assessments'):
            if (
                not isinstance(raw, dict)
                or not isinstance(raw.get('id'), str)
                or raw.get('id') not in conditions
                or raw['id'] in seen
                or not isinstance(raw.get('status'), str)
                or raw.get('status') not in {'met', 'not_met', 'unknown', 'conflicting'}
            ):
                raise HealthAnalysisGenerationError('Personal condition assessment is invalid')
            support = personal_citations(raw.get('support', []), materials)
            contradicts = personal_citations(raw.get('contradicts', []), materials)
            state = 'conflicting' if support and contradicts else raw['status']
            if (
                (state == 'met' and not support)
                or (state == 'not_met' and not contradicts)
                or (state == 'conflicting' and not (support and contradicts))
            ):
                raise HealthAnalysisGenerationError('Personal condition status lacks the required evidence')
            seen.add(raw['id'])
            rows.append(
                {
                    'id': raw['id'],
                    'status': state,
                    'support': support,
                    'contradicts': contradicts,
                    'explanation': text(raw.get('explanation'), 'personal condition explanation'),
                }
            )
        if seen != conditions:
            raise HealthAnalysisGenerationError('Personal matching omitted candidate conditions')
        return {'conditions': rows, 'supported': all(r['status'] == 'met' for r in rows), 'causalityEstablished': False}
