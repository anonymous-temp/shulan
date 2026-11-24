"""Versioned, report-scoped factor preparation and answer reassessment."""

from __future__ import annotations

import asyncio
import inspect
import json
from copy import deepcopy

from agents.knowledge.types import KnowledgeQuery
from agents.knowledge.types import KnowledgeSource

from .evidence import QuestionnaireEvidenceVersionError
from .evidence import evidence_rule_selection
from .evidence import preparation_evidence_selection
from .evidence import require_evidence_selection
from .factors_answers import target_answer_refs
from .factors_evidence import batches
from .factors_evidence import citation
from .factors_evidence import index_reports
from .factors_evidence import now_iso
from .factors_evidence import source_material
from .schemas import normalize_answers
from .schemas import stable_hash
from .schemas import validate_questionnaire

PIPELINE_VERSION = 'factor-assessment-v1'
RELATIONS = frozenset({'risk_factor', 'trigger', 'aggravating_factor', 'possible_contributor'})
CONDITION_STATES = frozenset({'met', 'not_met', 'unknown', 'conflicting'})
PERSONAL_REPORT_KINDS = frozenset({'report_record', 'report_background'})


class FactorContractError(RuntimeError):
    """A model response violated the evidence contract and may be retried."""


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise FactorContractError(f'{field} must be non-empty text')
    return value.strip()


def _array(value, field):
    if not isinstance(value, list):
        raise FactorContractError(f'{field} must be an array')
    return value


def _refs(value, allowed, field, *, required=True):
    refs = _array(value, field)
    if any(not isinstance(item, str) or item not in allowed for item in refs) or (required and not refs):
        raise FactorContractError(f'{field} contains absent evidence')
    return list(dict.fromkeys(refs))


def _empty_assessment(candidate):
    return {
        'presence': 'unknown',
        'applicability': 'unknown',
        'conditionAssessments': [
            {
                'conditionId': row['id'],
                'status': 'unknown',
                'support': [],
                'contradicts': [],
                'evidenceRefs': [],
                'answerRefs': [],
                'explanation': '现有资料尚不足以判断该条件。',
            }
            for row in candidate['conditions']
        ],
        'explanation': '医学关系与个人存在情况、时间和适用条件需分别判断。',
    }


def _refresh(prepared):
    prepared['version'] = (
        'fp_' + stable_hash({key: value for key, value in prepared.items() if key not in {'version', 'createdAt'}})[:24]
    )
    return prepared


def _question_map(questionnaire):
    return {row['name']: row for page in questionnaire.get('pages', []) for row in page.get('elements', [])}


def bind_questions(prepared: dict, questionnaire: dict, plan: dict) -> dict:
    """Freeze only published question/candidate/condition links; invalid links are gaps."""
    result = deepcopy(prepared)
    questions = _question_map(validate_questionnaire(questionnaire))
    if len(questions) > 15:
        raise ValueError('factor questionnaires allow at most 15 questions')
    allowed = {(c['id'], row['id']) for c in result.get('candidates', []) for row in c['conditions']}
    bindings, rejected, seen = [], [], set()
    for row in plan.get('factorBindings', []) if isinstance(plan.get('factorBindings', []), list) else []:
        if not isinstance(row, dict):
            rejected.append({'reason': 'invalid_binding'})
            continue
        key = (row.get('questionName'), row.get('candidateId'), row.get('conditionId'))
        if not all(isinstance(item, str) for item in key) or key[0] not in questions or key[1:] not in allowed:
            rejected.append({'reason': 'binding_not_published_or_unknown', 'binding': deepcopy(row)})
            continue
        if key in seen:
            continue
        seen.add(key)
        bindings.append(
            {
                'questionName': key[0],
                'candidateId': key[1],
                'conditionId': key[2],
                'question': deepcopy(questions[key[0]]),
                'questionVersion': stable_hash(questions[key[0]]),
            }
        )
    result['bindings'] = bindings
    result['questionnaireHash'] = stable_hash(questionnaire)
    result['coverage'].setdefault('bindingGaps', []).extend(rejected)
    result['coverage']['unboundConditions'] = [
        {'candidateId': cid, 'conditionId': condition}
        for cid, condition in sorted(allowed)
        if not any(row['candidateId'] == cid and row['conditionId'] == condition for row in bindings)
    ]
    return _refresh(result)


class QuestionnaireFactorPipeline:
    VERSION = 'factor-assessment-v1'

    def __init__(
        self,
        *,
        model,
        knowledge,
        evidence_policy,
        phase_timeout_seconds=60,
        batch_chars=30000,
        max_batches=64,
        max_material_batches=12,
        concurrency=3,
    ):
        if batch_chars < 1000 or max_batches < 1 or max_material_batches < 1 or concurrency < 1:
            raise ValueError('factor pipeline budgets must be positive and batch_chars at least 1000')
        self.model, self.knowledge = model, knowledge
        self.evidence_policy = evidence_policy
        self._evidence_selection = evidence_rule_selection(evidence_policy.execution_selection())
        self.phase_timeout_seconds = phase_timeout_seconds
        self.batch_chars, self.max_batches = batch_chars, max_batches
        self.max_material_batches = max_material_batches
        self._semaphore = asyncio.Semaphore(concurrency)

    def evidence_rule_selection(self):
        require_evidence_selection(self.evidence_policy.execution_selection(), self._evidence_selection)
        return deepcopy(self._evidence_selection)

    async def _run(self, stage, payload, *, web_search=False):
        self.evidence_rule_selection()
        async with self._semaphore:
            self.evidence_rule_selection()
            try:
                async with asyncio.timeout(self.phase_timeout_seconds):
                    result = await self.model.run(stage, deepcopy(payload), web_search=web_search)
            except Exception:
                self.evidence_rule_selection()
                raise
            self.evidence_rule_selection()
        if not isinstance(result, dict) or not isinstance(result.get('output'), dict):
            raise FactorContractError('factor model adapter returned an invalid result')
        return result

    async def prepare(self, *, reports: list, questionnaire_id: str) -> dict:
        if not isinstance(reports, list):
            raise ValueError('reports must be a list')
        selection = self.evidence_rule_selection()
        evidence, coverage = index_reports(reports)
        prepared = {
            'version': '',
            'pipelineVersion': PIPELINE_VERSION,
            'evidenceRuleSelection': selection,
            'questionnaireId': questionnaire_id,
            'inputHash': stable_hash(reports),
            'createdAt': now_iso(),
            'phase': 'pre_answer',
            'targets': [],
            'candidates': [],
            'sources': [],
            'evidence': evidence,
            'coverage': coverage,
            'questionHints': [],
            'bindings': [],
        }
        groups = batches(evidence, max_chars=self.batch_chars, max_items=64)
        targets = {}
        for number, group in enumerate(groups):
            # Preserve oversized/over-budget records in the index and state the gap;
            # never silently truncate a clinical value or change a frozen partition.
            if number >= self.max_batches or len(json.dumps(group, ensure_ascii=False)) > self.batch_chars * 4:
                coverage['unprocessedEvidenceIds'].extend(row['id'] for row in group)
                coverage['gaps'].append({'reason': 'planning_budget_exceeded', 'evidenceIds': [r['id'] for r in group]})
                continue
            response = (await self._run('targets', {'evidence': group, 'phase': 'pre_answer'}))['output']
            ids = {row['id'] for row in group}
            covered = _refs(response.get('coveredEvidenceIds'), ids, 'coveredEvidenceIds')
            if set(covered) != ids:
                raise FactorContractError('target planning omitted report records')
            coverage['analyzedEvidenceIds'].extend(covered)
            factual = {row['id'] for row in group if row['kind'] == 'report_record'}
            for raw in _array(response.get('targets'), 'targets'):
                target = self._target(raw, factual, phase='pre_answer')
                if target['id'] in targets:
                    existing = targets[target['id']]
                    existing['reportEvidenceIds'] = list(
                        dict.fromkeys(existing['reportEvidenceIds'] + target['reportEvidenceIds'])
                    )
                    existing['riskInterpretation'].extend(target['riskInterpretation'])
                else:
                    targets[target['id']] = target
        prepared['targets'] = list(targets.values())
        outcomes = await asyncio.gather(*(self._research(target, phase='pre_answer') for target in targets.values()))
        for outcome in outcomes:
            self._merge_research(prepared, outcome)
        factual = [row for row in evidence if row['kind'] in PERSONAL_REPORT_KINDS]
        before = await asyncio.gather(*(self._match(candidate, factual, []) for candidate in prepared['candidates']))
        for candidate, assessment in zip(prepared['candidates'], before, strict=True):
            candidate['before'] = assessment
            coverage['gaps'].extend(
                {'candidateId': candidate['id'], **gap} for gap in assessment.get('evidenceGaps', [])
            )
            for condition in candidate['conditions']:
                state = next(row for row in assessment['conditionAssessments'] if row['conditionId'] == condition['id'])
                if state['status'] in {'unknown', 'conflicting'}:
                    prepared['questionHints'].append(
                        {
                            'candidateId': candidate['id'],
                            'conditionId': condition['id'],
                            'targetId': candidate['targetId'],
                            'question': condition.get('questionHint') or condition['label'],
                            'informationFacet': condition.get('informationFacet'),
                            'lifestyleKey': condition.get('lifestyleKey'),
                        }
                    )
        self._coverage(prepared)
        self.evidence_rule_selection()
        return _refresh(prepared)

    @staticmethod
    def bind_questions(prepared: dict, questionnaire: dict, plan: dict) -> dict:
        return bind_questions(prepared, questionnaire, plan)

    @staticmethod
    def _target(raw, allowed, *, phase, answers=False):
        if not isinstance(raw, dict):
            raise FactorContractError('target must be an object')
        label = _text(raw.get('label'), 'target.label')
        kind = raw.get('kind')
        if kind not in {'indicator', 'symptom', 'disease'}:
            raise FactorContractError('invalid target kind')
        field = 'answerIds' if answers else 'evidenceIds'
        refs = _refs(raw.get(field), allowed, field)
        query = _text(raw.get('query'), 'target.query')
        if len(query) > 128:
            raise FactorContractError('medical search query exceeds 128 characters')
        return {
            'id': 't_' + stable_hash([label, kind])[:20],
            'label': label,
            'kind': kind,
            'query': query,
            'phase': phase,
            'reportEvidenceIds': [] if answers else refs,
            'answerIds': refs if answers else [],
            'riskInterpretation': [
                {
                    'text': _text(raw.get('interpretation'), 'target.interpretation'),
                    'evidenceIds': [] if answers else refs,
                    'answerIds': refs if answers else [],
                    'phase': phase,
                    'origin': 'factor_analysis',
                }
            ],
        }

    async def _research(self, target, *, phase):
        documents, problems = [], []
        search_trace, search_reason = None, None
        if self.knowledge is not None and self.knowledge.enabled:

            async def retrieve(source):
                async with self._semaphore, asyncio.timeout(self.phase_timeout_seconds):
                    self.evidence_rule_selection()
                    result = await self.knowledge.retrieve(
                        KnowledgeQuery(
                            source=source,
                            query=target['query'],
                            count=3,
                            include_source_text=True,
                        )
                    )
                    self.evidence_rule_selection()
                    return result

            results = await asyncio.gather(
                retrieve(KnowledgeSource.GUIDE),
                retrieve(KnowledgeSource.LITERATURE),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    if isinstance(result, (asyncio.CancelledError, QuestionnaireEvidenceVersionError)):
                        raise result
                    problems.append({'reason': 'knowledge_base_error', 'errorType': type(result).__name__})
                else:
                    documents.extend(result)
        else:
            problems.append({'reason': 'knowledge_base_unavailable'})
        sources = [source for row in documents if (source := source_material(row, channel='knowledge_base'))]
        candidates, candidate_gaps = await self._from_material(target, sources, phase=phase)
        problems.extend(candidate_gaps)
        used_web = not candidates
        if used_web:
            problems.append({'reason': 'knowledge_base_no_applicable_material'})
            # Never send individual report records or answers to an external search.
            try:
                web = await self._run('search', {'query': target['query']}, web_search=True)
                search_trace = web.get('searchTrace')
            except QuestionnaireEvidenceVersionError:
                raise
            except Exception as error:
                # A target-level retrieval outage does not erase other targets.
                # Cancellation is not swallowed (CancelledError is BaseException).
                problems.append({'reason': 'web_search_failed', 'errorType': type(error).__name__})
                search_trace = getattr(error, 'search_trace', None)
                search_reason = getattr(error, 'search_reason', None)
                web = {'searchSources': []}
            web_sources = [
                source for row in web.get('searchSources', []) if (source := source_material(row, channel='web_search'))
            ]
            sources.extend(web_sources)
            if not web_sources:
                problems.append({'reason': 'web_source_material_unavailable'})
            candidates, candidate_gaps = await self._from_material(target, web_sources, phase=phase)
            problems.extend(candidate_gaps)
        return {
            'targetId': target['id'],
            'candidates': candidates,
            'sources': sources,
            'searchTrace': search_trace,
            'searchReason': search_reason,
            'phase': phase,
            'retrieval': {
                'targetId': target['id'],
                'status': 'supported' if candidates else 'unavailable',
                'usedWebSearch': used_web,
                'gaps': problems,
            },
        }

    async def _from_material(self, target, sources, *, phase):
        if not sources:
            return [], []
        source_map = {row['id']: row for row in sources}
        passages = []
        for source in sources:
            for passage in source['passages']:
                text = passage['text']
                for start in range(0, len(text), self.batch_chars // 2):
                    passages.append(
                        {
                            'sourceId': source['id'],
                            'title': source['title'],
                            'url': source['url'],
                            'sourceVersion': source['version'],
                            'passageId': passage['id'],
                            'text': text[start : start + self.batch_chars // 2],
                            'locator': passage['locator'],
                            'characterOffset': start,
                        }
                    )
        groups = batches(passages, max_chars=self.batch_chars)
        candidates, gaps = {}, []
        for number, group in enumerate(groups):
            if number >= self.max_material_batches:
                gaps.append({'reason': 'material_budget_exceeded', 'omittedBatchCount': len(groups) - number})
                break
            output = (
                await self._run(
                    'candidates',
                    {
                        'target': {'id': target['id'], 'label': target['label'], 'kind': target['kind']},
                        'passages': group,
                    },
                )
            )['output']
            for raw in _array(output.get('candidates'), 'candidates'):
                try:
                    candidate = self._candidate(raw, target, source_map, phase=phase)
                except (ValueError, FactorContractError) as error:
                    gaps.append({'reason': 'candidate_evidence_rejected', 'errorType': type(error).__name__})
                    continue
                key = candidate['id']
                if key in candidates:
                    # Same canonical concept can have several independent sources.
                    existing = candidates[key]
                    existing['knowledgeEvidence'] = list(
                        {
                            stable_hash(row): row
                            for row in existing['knowledgeEvidence'] + candidate['knowledgeEvidence']
                        }.values()
                    )
                    existing['conditions'] = list(
                        {row['id']: row for row in existing['conditions'] + candidate['conditions']}.values()
                    )
                else:
                    candidates[key] = candidate
            for reason in output.get('gaps', []) if isinstance(output.get('gaps', []), list) else []:
                if isinstance(reason, str):
                    gaps.append({'reason': 'material_not_applicable', 'detail': reason})
        return list(candidates.values()), gaps

    @staticmethod
    def _candidate(raw, target, sources, *, phase):
        if not isinstance(raw, dict) or not isinstance(raw.get('concept'), dict):
            raise FactorContractError('candidate requires a concept')
        concept = {
            'id': _text(raw['concept'].get('id'), 'concept.id'),
            'label': _text(raw['concept'].get('label'), 'concept.label'),
            'codingSystem': 'internal',
        }
        relation = raw.get('relationType')
        if relation not in RELATIONS:
            raise FactorContractError('unsupported factor relationship')
        refs = [citation(row, sources) for row in _array(raw.get('knowledgeEvidence'), 'knowledgeEvidence')]
        if not refs:
            raise FactorContractError('candidate requires original medical evidence')
        candidate_id = 'c_' + stable_hash([target['id'], concept['id'], relation])[:20]
        conditions, seen = [], set()
        for item in _array(raw.get('conditions'), 'conditions'):
            if not isinstance(item, dict) or item.get('kind') not in {'presence', 'temporality', 'applicability'}:
                raise FactorContractError('invalid candidate condition')
            local_id = _text(item.get('id'), 'condition.id')
            if local_id in seen:
                raise FactorContractError('duplicate candidate condition')
            seen.add(local_id)
            conditions.append(
                {
                    'id': candidate_id + '_' + local_id,
                    'label': _text(item.get('label'), 'condition.label'),
                    'kind': item['kind'],
                    'required': True,
                    **{
                        key: item[key]
                        for key in ('questionHint', 'informationFacet', 'lifestyleKey')
                        if isinstance(item.get(key), str)
                    },
                }
            )
        if not {'presence', 'temporality'}.issubset({row['kind'] for row in conditions}):
            raise FactorContractError('candidate requires presence and temporality conditions')
        return {
            'id': candidate_id,
            'targetId': target['id'],
            'concept': concept,
            'relationType': relation,
            'phase': phase,
            'knowledgeEvidence': refs,
            'conditions': conditions,
            'supportExplanation': _text(raw.get('supportExplanation'), 'supportExplanation'),
            'causalityEstablished': False,
        }

    @staticmethod
    def _merge_research(prepared, outcome):
        prepared['candidates'].extend(outcome['candidates'])
        prepared['sources'] = list({row['id']: row for row in prepared['sources'] + outcome['sources']}.values())
        prepared['coverage']['targetRetrieval'].append(outcome['retrieval'])
        # Keep transport diagnostics with the internal snapshot, not patient
        # evidence or public assessment prose. Legacy snapshots need no migration.
        if isinstance(outcome.get('searchTrace'), dict):
            prepared.setdefault('searchTraces', []).append(
                {
                    'targetId': outcome['targetId'],
                    'phase': outcome['phase'],
                    'reason': outcome.get('searchReason'),
                    'trace': deepcopy(outcome['searchTrace']),
                }
            )

    async def _match(self, candidate, evidence, answers):
        personal = evidence + answers
        if not personal:
            return _empty_assessment(candidate)
        groups = batches(personal, max_chars=self.batch_chars, max_items=96)
        all_assessments, explanations, incomplete = [], [], False
        for number, group in enumerate(groups):
            if number >= self.max_batches or len(json.dumps(group, ensure_ascii=False)) > self.batch_chars * 4:
                incomplete = True
                continue
            reports = [row for row in group if row.get('kind') in PERSONAL_REPORT_KINDS]
            answer_rows = [row for row in group if row.get('kind') == 'answer']
            output = (
                await self._run(
                    'match',
                    {
                        'candidate': {k: deepcopy(v) for k, v in candidate.items() if k != 'before'},
                        'evidence': reports,
                        'answers': answer_rows,
                    },
                )
            )['output']
            all_assessments.append(self._validate_match(output, candidate, reports, answer_rows))
            if isinstance(output.get('explanation'), str):
                explanations.append(output['explanation'])
        merged = _empty_assessment(candidate)
        for row in merged['conditionAssessments']:
            pieces = [next(r for r in group if r['conditionId'] == row['conditionId']) for group in all_assessments]
            row['support'] = list({stable_hash(ref): ref for piece in pieces for ref in piece['support']}.values())
            row['contradicts'] = list(
                {stable_hash(ref): ref for piece in pieces for ref in piece['contradicts']}.values()
            )
            states = {piece['status'] for piece in pieces}
            row['status'] = (
                'conflicting'
                if 'conflicting' in states or {'met', 'not_met'}.issubset(states)
                else 'unknown'
                if incomplete
                else 'met'
                if 'met' in states
                else 'not_met'
                if 'not_met' in states
                else 'unknown'
            )
            refs = row['support'] + row['contradicts']
            row['evidenceRefs'] = list(
                dict.fromkeys(ref['evidenceId'] for ref in refs if ref['kind'] in PERSONAL_REPORT_KINDS)
            )
            row['answerRefs'] = list(dict.fromkeys(ref['evidenceId'] for ref in refs if ref['kind'] == 'answer'))
            row['explanation'] = '\n'.join(dict.fromkeys(piece['explanation'] for piece in pieces))
        presence = [
            r['status']
            for r in merged['conditionAssessments']
            if next(c for c in candidate['conditions'] if c['id'] == r['conditionId'])['kind'] == 'presence'
        ]
        applicability = [
            r['status']
            for r in merged['conditionAssessments']
            if next(c for c in candidate['conditions'] if c['id'] == r['conditionId'])['kind'] != 'presence'
        ]

        def aggregate(states, positive, negative):
            if 'conflicting' in states:
                return 'conflicting'
            if 'not_met' in states:
                return negative
            return positive if states and all(state == 'met' for state in states) else 'unknown'

        merged['presence'] = aggregate(presence, 'present', 'absent')
        merged['applicability'] = aggregate(applicability, 'matched', 'not_matched')
        merged['explanation'] = '\n'.join(dict.fromkeys(explanations))
        if incomplete:
            merged['evidenceGaps'] = [{'reason': 'matching_budget_exceeded'}]
        return merged

    @staticmethod
    def _validate_match(output, candidate, reports, answers):
        materials = {row['id']: row for row in reports + answers}
        conditions = {row['id'] for row in candidate['conditions']}
        result, seen = [], set()
        for raw in _array(output.get('conditionAssessments'), 'conditionAssessments'):
            if not isinstance(raw, dict) or raw.get('conditionId') not in conditions or raw['conditionId'] in seen:
                raise FactorContractError('condition assessment references an absent or duplicate condition')
            seen.add(raw['conditionId'])
            status = raw.get('status')
            if status not in CONDITION_STATES:
                raise FactorContractError('invalid condition assessment status')
            row = {
                'conditionId': raw['conditionId'],
                'status': status,
                'explanation': _text(raw.get('explanation'), 'condition.explanation'),
            }
            for field in ('support', 'contradicts'):
                row[field] = []
                for ref in _array(raw.get(field, []), field):
                    if not isinstance(ref, dict) or ref.get('evidenceId') not in materials:
                        raise FactorContractError('personal assessment references absent evidence')
                    material = materials[ref['evidenceId']]
                    if material['kind'] == 'answer' and not material.get('informative', True):
                        raise FactorContractError('an unknown answer cannot support or contradict a condition')
                    quote = _text(ref.get('quote'), 'personal.quote')
                    if material['kind'] == 'answer':
                        if quote.lower() in {'不清楚', '不知道', '不确定', '记不清', 'unknown', 'unsure'}:
                            raise FactorContractError('an unknown answer unit is not personal evidence')
                        units = material.get('answerQuoteUnits', [material['answerText']])
                    else:
                        record = material['record']
                        value = record.get('resultValue', record.get('result')) if isinstance(record, dict) else record
                        units = [str(value)] if value is not None else []
                        source_quote = (record.get('evidence') or {}).get('quote') if isinstance(record, dict) else None
                        if isinstance(source_quote, str):
                            units.append(source_quote)
                        if material.get('observedAt'):
                            units.append(str(material['observedAt']))
                    if ' '.join(quote.split()) not in {' '.join(unit.split()) for unit in units}:
                        raise FactorContractError(
                            'personal quote is absent from complete reported values or answer units'
                        )
                    row[field].append({**deepcopy(ref), 'kind': material['kind']})
            if row['support'] and row['contradicts']:
                row['status'] = status = 'conflicting'
            if status == 'met' and not row['support']:
                raise FactorContractError('met condition requires personal evidence')
            if status == 'not_met' and not row['contradicts']:
                raise FactorContractError('unmet condition requires counterevidence')
            if status == 'conflicting' and not (row['support'] and row['contradicts']):
                raise FactorContractError('conflicting condition requires both evidence directions')
            result.append(row)
        if seen != conditions:
            raise FactorContractError('personal assessment omitted candidate conditions')
        return result

    @staticmethod
    def _coverage(prepared):
        coverage = prepared['coverage']
        unavailable = [row['targetId'] for row in coverage['targetRetrieval'] if row['status'] != 'supported']
        coverage['targetsWithoutApplicableEvidence'] = unavailable
        material_budget = any(
            gap.get('reason') == 'material_budget_exceeded'
            for row in coverage['targetRetrieval']
            for gap in row.get('gaps', [])
        )
        incomplete = unavailable or coverage['gaps'] or coverage['unprocessedEvidenceIds'] or material_budget
        coverage['status'] = 'partial' if incomplete else 'complete'

    @staticmethod
    def _answers(snapshot):
        questionnaire = validate_questionnaire(snapshot['questionnaire'])
        supplied = snapshot.get('answers') or {}
        if not isinstance(supplied, dict):
            raise ValueError('answers must be an object')
        if supplied and all(isinstance(row, dict) and 'rawValue' in row for row in supplied.values()):
            raw = {name: row['rawValue'] for name, row in supplied.items() if row.get('provided', True)}
        else:
            raw = supplied
        rows, _ = normalize_answers(questionnaire, raw)
        answers = []
        for name, row in rows.items():
            if row['status'] != 'answered':
                continue
            question = row['question']
            choices = {choice['value']: choice.get('text', choice['value']) for choice in question.get('choices', [])}
            if question.get('showNoneItem'):
                choices['none'] = question.get('noneText', '无')
            value = row['value']
            answer_text = (
                '\n'.join(choices.get(item, item) for item in value)
                if isinstance(value, list)
                else choices.get(value, value)
            )
            quote_units = [choices.get(item, item) for item in value] if isinstance(value, list) else [answer_text]
            unknown_answers = {'不清楚', '不知道', '不确定', '记不清', 'unknown', 'unsure'}
            informative = any(part.strip().lower() not in unknown_answers for part in answer_text.splitlines())
            answers.append(
                {
                    'id': 'ans_' + stable_hash([name, row['rawValue'], question])[:20],
                    'kind': 'answer',
                    'questionName': name,
                    'question': deepcopy(question),
                    'rawAnswer': deepcopy(row['rawValue']),
                    'answer': deepcopy(value),
                    'answerText': answer_text,
                    'status': 'answered',
                    'answerQuoteUnits': quote_units,
                    'informative': informative,
                    'answeredAt': snapshot.get('submittedAt'),
                }
            )
        return answers

    async def evaluate(self, snapshot: dict, *, checkpoint=None) -> dict:
        reports = deepcopy(snapshot.get('reports') or [])
        answers = self._answers(snapshot)
        selection = self.evidence_rule_selection()
        if '_factorEvidenceRuleSelection' in snapshot:
            require_evidence_selection(snapshot['_factorEvidenceRuleSelection'], selection)
        preparation_evidence_selection(snapshot.get('factorPreparation'), selection)
        legacy_hash = stable_hash([reports, snapshot['questionnaire'], answers, snapshot.get('factorPreparation')])
        input_hash = stable_hash([legacy_hash, selection])
        saved = snapshot.get('_factorCheckpoint')
        if isinstance(saved, dict):
            require_evidence_selection(
                saved.get('evidenceRuleSelection'), selection, legacy='evidenceRuleSelection' not in saved
            )
            preparation_evidence_selection(saved.get('preparation'), selection)
            accepted_hashes = {input_hash}
            if 'evidenceRuleSelection' not in saved:
                accepted_hashes.add(legacy_hash)
            saved = deepcopy(saved) if saved.get('inputHash') in accepted_hashes else {}
        else:
            saved = {}
        prepared = saved.get('preparation') or deepcopy(snapshot.get('factorPreparation'))
        fresh = (
            not isinstance(prepared, dict)
            or prepared.get('pipelineVersion') != PIPELINE_VERSION
            or prepared.get('status') == 'unavailable'
        )
        if fresh:
            prepared = await self.prepare(
                reports=reports, questionnaire_id=str(snapshot.get('questionnaireId', snapshot.get('_id', '')))
            )
            prepared['phase'] = 'post_answer'
            for target in prepared['targets']:
                target['phase'] = 'post_answer'
                for interpretation in target['riskInterpretation']:
                    interpretation['phase'] = 'post_answer'
            for candidate in prepared['candidates']:
                candidate['phase'] = 'post_answer'
                candidate.pop('before', None)
            for trace in prepared.get('searchTraces', []):
                trace['phase'] = 'post_answer'
            _refresh(prepared)
        if prepared.get('inputHash') != stable_hash(reports):
            raise ValueError('factor preparation belongs to different report inputs')
        if prepared.get('questionnaireHash') and prepared['questionnaireHash'] != stable_hash(
            snapshot['questionnaire']
        ):
            raise ValueError('published questionnaire changed after factor preparation')
        prepared.setdefault('basePreparationVersion', prepared['version'])
        if not saved.get('answerDiscoveryComplete'):
            await self._discover_answers(prepared, answers)
            _refresh(prepared)
        evaluations = saved.get('evaluations', {})

        async def save():
            self.evidence_rule_selection()
            if checkpoint is not None:
                value = checkpoint(
                    deepcopy(
                        {
                            'inputHash': input_hash,
                            'evidenceRuleSelection': selection,
                            'preparation': prepared,
                            'evaluations': evaluations,
                            'answerDiscoveryComplete': True,
                        }
                    )
                )
                if inspect.isawaitable(value):
                    await value
                self.evidence_rule_selection()

        await save()
        factual = [row for row in prepared['evidence'] if row['kind'] in PERSONAL_REPORT_KINDS]
        for candidate in prepared['candidates']:
            if candidate['id'] not in evaluations:
                evaluations[candidate['id']] = await self._match(candidate, factual, answers)
                await save()
        self.evidence_rule_selection()
        return self._result(prepared, answers, evaluations, input_hash)

    async def _discover_answers(self, prepared, answers):
        answers = [row for row in answers if row.get('informative', True)]
        if not answers:
            return
        groups = batches(answers, max_chars=self.batch_chars, max_items=15)
        existing = {target['id'] for target in prepared['targets']}
        for number, group in enumerate(groups):
            if number >= self.max_batches or len(json.dumps(group, ensure_ascii=False)) > self.batch_chars * 4:
                prepared['coverage']['gaps'].append({'reason': 'answer_discovery_budget_exceeded'})
                continue
            output = (
                await self._run(
                    'answer_targets',
                    {
                        'targets': [{'id': t['id'], 'label': t['label']} for t in prepared['targets']],
                        'answers': group,
                    },
                )
            )['output']
            for raw in _array(output.get('targets'), 'answer targets'):
                target = self._target(raw, {row['id'] for row in group}, phase='post_answer', answers=True)
                refs = target_answer_refs(target, group, evidence_policy=self.evidence_policy)
                if not refs:
                    prepared['coverage']['gaps'].append(
                        {
                            'reason': 'answer_target_not_self_asserted',
                            'targetLabel': target['label'],
                            'answerIds': target['answerIds'],
                        }
                    )
                    continue
                target['answerIds'] = refs
                for interpretation in target['riskInterpretation']:
                    interpretation['answerIds'] = refs
                if target['id'] in existing:
                    continue
                existing.add(target['id'])
                prepared['targets'].append(target)
                self._merge_research(prepared, await self._research(target, phase='post_answer'))
        self._coverage(prepared)

    @staticmethod
    def _result(prepared, answers, evaluations, input_hash):
        evidence_map = {row['id']: row for row in prepared['evidence']}
        answer_map = {row['id']: row for row in answers}
        targets = []
        for target in prepared['targets']:
            candidates, factors, gaps = [], [], []
            retrieval = next(
                (row for row in prepared['coverage']['targetRetrieval'] if row['targetId'] == target['id']), {}
            )
            gaps.extend(retrieval.get('gaps', []))
            if retrieval.get('status') != 'supported' and not gaps:
                gaps.append({'reason': 'medical_evidence_unavailable'})
            for candidate in prepared['candidates']:
                if candidate['targetId'] != target['id']:
                    continue
                current = deepcopy(candidate)
                after = deepcopy(evaluations[candidate['id']])
                current['after'] = after
                refs = {ref for row in after['conditionAssessments'] for ref in row.get('answerRefs', [])}
                current['answerEvidence'] = [deepcopy(answer_map[ref]) for ref in sorted(refs)]
                current['questionBindings'] = [
                    deepcopy(row) for row in prepared['bindings'] if row['candidateId'] == candidate['id']
                ]
                applicable = after['presence'] == 'present' and after['applicability'] == 'matched'
                current['conclusion'] = (
                    '本次资料支持该因素存在且符合所列医学适用条件，可作为相关因素；不证明它是个人病因。'
                    if applicable
                    else '现有资料不足以把该候选归为已符合条件的个人相关因素，具体限制见条件证据。'
                )
                current['causalityEstablished'] = False
                candidates.append(current)
                if applicable:
                    factors.append(deepcopy(current))
                gaps.extend(after.get('evidenceGaps', []))
            targets.append(
                {
                    **deepcopy(target),
                    'reportEvidence': [deepcopy(evidence_map[key]) for key in target['reportEvidenceIds']],
                    'answerEvidence': [deepcopy(answer_map[key]) for key in target.get('answerIds', [])],
                    'candidateFactors': candidates,
                    'factors': factors,
                    'evidenceGaps': gaps,
                }
            )
        matched = sum(len(row['factors']) for row in targets)
        coverage = deepcopy(prepared['coverage'])
        matching_gaps = [
            gap
            for target in targets
            for gap in target['evidenceGaps']
            if gap.get('reason') == 'matching_budget_exceeded'
        ]
        if matching_gaps:
            coverage['gaps'].extend(matching_gaps)
            coverage['status'] = 'partial'
        return {
            'version': 'fa_' + stable_hash([input_hash, prepared['version'], evaluations])[:24],
            'pipelineVersion': PIPELINE_VERSION,
            'preparationVersion': prepared.get('basePreparationVersion', prepared['version']),
            'assessmentPreparationVersion': prepared['version'],
            'summary': (
                f'已整理{len(targets)}个分析目标，{matched}项因素符合当前个人资料及所列适用条件；相关性不等于个人因果。'
            ),
            'targets': targets,
            'sources': deepcopy(prepared['sources']),
            'coverage': coverage,
            'reportInterpretations': [
                {**deepcopy(row), 'origin': 'report_interpretation'}
                for row in prepared['evidence']
                if row['kind'] == 'ai_interpretation'
            ],
            'generatedAt': now_iso(),
        }
