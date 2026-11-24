"""Single-run five-step analysis, catalogue coverage and four-field rendering."""

from __future__ import annotations

from copy import deepcopy

from modules.health_analysis.agent import HEALTH_ANALYSIS_EXECUTION
from modules.health_analysis.contracts import AnalysisGenerationFailure
from modules.health_analysis.contracts import AnalysisGenerationStage
from modules.health_analysis.contracts import analysis_generation_stage
from modules.health_analysis.deduplication import SemanticCoverage
from modules.health_analysis.deduplication import definite_duplicates
from modules.health_analysis.factors import FactorAnalysis
from modules.health_analysis.factors import HealthAnalysisGenerationError
from modules.health_analysis.factors import array
from modules.health_analysis.factors import check_deadline
from modules.health_analysis.factors import medical_citations
from modules.health_analysis.factors import partition
from modules.health_analysis.factors import refs
from modules.health_analysis.factors import text
from modules.health_analysis.model_projection import project_names
from modules.health_analysis.policy import CATALOGUE_VERSION
from modules.health_analysis.policy import DEFAULT_ORDER
from modules.health_analysis.policy import MAX_QUESTIONS
from modules.health_analysis.policy import POLICY_VERSION
from modules.health_analysis.policy import background_questions
from modules.health_analysis.policy import build_schema
from modules.health_analysis.policy import catalogue
from modules.health_analysis.policy import remove_covered_unknown_options
from modules.health_analysis.policy import suitable_text
from modules.health_analysis.policy import survey_rules
from modules.health_analysis.policy import template_question
from modules.health_analysis.policy import validate_question
from modules.health_analysis.survey_quality import review_survey


def _unresolved_lifestyle_goals(coverage, accepted):
    return {row['targetId'] for row in coverage if row['status'] in {'askable', 'uncertain'}} - {
        row['targetId'] for row in accepted
    }


class HealthAnalysisGenerator:
    VERSION = 'health-analysis-generation-v7'

    def __init__(
        self,
        model,
        knowledge=None,
        *,
        semaphore=None,
        max_targets=4,
        max_candidates=12,
        max_material_batches=8,
        batch_chars=24000,
        knowledge_timeout_seconds=25,
        max_survey_rewrites=2,
    ):
        for value in (max_targets, max_candidates, max_material_batches, batch_chars):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError('Health Analysis execution guards must be positive integers')
        if type(max_survey_rewrites) is not int or max_survey_rewrites < 0:
            raise ValueError('Survey rewrite budget must be a nonnegative integer')
        self.model = model
        self.max_material_batches, self.batch_chars = max_material_batches, batch_chars
        self.max_survey_rewrites = max_survey_rewrites
        self.survey_rules = survey_rules()
        self.factors = FactorAnalysis(
            knowledge,
            semaphore=semaphore,
            max_targets=max_targets,
            max_candidates=max_candidates,
            max_material_batches=max_material_batches,
            batch_chars=batch_chars,
            knowledge_timeout_seconds=knowledge_timeout_seconds,
        )
        self.coverage = SemanticCoverage(batch_chars=batch_chars, max_material_batches=max_material_batches)

    async def generate(self, snapshot: dict) -> dict:
        # All per-run mutation is local: concurrent requests share dependencies,
        # never this run's evidence, model identity list or publication state.
        snapshot = deepcopy(snapshot)
        deadline_at = snapshot.pop('deadline_at', None)
        query_exclusion_values = snapshot.pop('query_exclusion_values', ())
        redaction_names = snapshot.pop('model_redaction_names', ())
        snapshot, projection = project_names(snapshot, redaction_names)
        if not isinstance(query_exclusion_values, (list, tuple)):
            raise ValueError('Health Analysis query exclusions must be an internal sequence')
        check_deadline(deadline_at)
        identities = []

        async def call(stage, payload):
            check_deadline(deadline_at)
            payload = deepcopy(payload)
            payload['effectiveDemographics'] = deepcopy(snapshot.get('effective_demographics', {}))
            payload, _ = project_names(payload, redaction_names)
            with analysis_generation_stage(AnalysisGenerationStage(stage)):
                if deadline_at is None:
                    response = await self.model.run(stage, deepcopy(payload))
                else:
                    response = await self.model.run(stage, deepcopy(payload), deadline_at=deadline_at)
                check_deadline(deadline_at)
                if not isinstance(response, dict) or not isinstance(response.get('output'), dict):
                    raise HealthAnalysisGenerationError('Health Analysis stage returned an invalid result')
                identity = response.get('model_identity')
                if not isinstance(identity, dict):
                    raise HealthAnalysisGenerationError('Health Analysis stage omitted its model identity')
            identities.append({**deepcopy(identity), 'stage': stage})
            return response['output']

        with analysis_generation_stage(AnalysisGenerationStage.MATERIALS):
            materials, material_decisions = await self._prepare_materials(snapshot.get('personal_materials', []), call)
        snapshot['personal_materials'] = [
            row for row in materials if row.get('kind') != 'report' or row.get('status') != 'unknown'
        ]
        clinical_materials = [row for row in materials if self._usable_for_analysis(row)]
        has_personal = bool(clinical_materials)
        has_history = bool(snapshot.get('has_historical_schemas'))
        gender = snapshot.get('profile_gender')
        trace = {
            'execution': deepcopy(HEALTH_ANALYSIS_EXECUTION),
            'modelInputProjection': projection,
            'generationVersion': self.VERSION,
            'policyVersion': POLICY_VERSION,
            'catalogueVersion': CATALOGUE_VERSION,
            'surveyReviewRules': [rule['ruleId'] for rule in self.survey_rules],
            'effectiveHasPersonalData': has_personal,
            'hasHistoricalSchemas': has_history,
            'materialDecisions': material_decisions,
            'filters': [],
            'bindings': [],
            'guards': {
                'maxTargets': self.factors.max_targets,
                'maxCandidates': self.factors.max_candidates,
                'maxMaterialBatches': self.max_material_batches,
                'batchChars': self.batch_chars,
                'maxSurveyRewrites': self.max_survey_rewrites,
            },
        }
        plan, factors = {}, None
        if has_personal:
            with analysis_generation_stage(AnalysisGenerationStage.RISK_ANALYSIS):
                factors = await self.factors.analyze(
                    [row for row in clinical_materials if row.get('backgroundScope') != 'family_history'],
                    call,
                    deadline_at=deadline_at,
                    query_exclusion_values=query_exclusion_values,
                )
            clinical_snapshot = {**snapshot, 'personal_materials': clinical_materials}
            plan = await call(
                'plan',
                {
                    'snapshot': clinical_snapshot,
                    'materials': clinical_materials,
                    'factors': factors,
                    'catalogue': catalogue(),
                    'surveyRules': self.survey_rules,
                },
            )
            with analysis_generation_stage(AnalysisGenerationStage.PLAN):
                review_text, tests, interviews, filters = self._plan_contents(
                    plan, clinical_materials, factors, snapshot
                )
            trace['filters'].extend(filters)
        else:
            review_text, tests, interviews = '', [], []
        trace.update({'mode': 'personalized' if has_personal else 'history_basic', 'factors': factors})
        background = background_questions()
        with analysis_generation_stage(AnalysisGenerationStage.COVERAGE):
            coverage, evidence = await self.coverage.assess(snapshot, call, background=background)
        initial_questions = [question for question in background if not evidence[question['name']]]
        if any(validate_question(question, gender) is None for question in initial_questions):
            raise HealthAnalysisGenerationError('The frozen background questions violate publication policy')
        initial_bindings = [{'questionName': q['name'], 'origin': 'template'} for q in initial_questions]
        trace['backgroundCoverage'] = {
            name: {'answered': bool(citations), 'evidence': citations} for name, citations in evidence.items()
        }
        if not has_personal and not has_history:
            lifestyle = [template_question(key) for key in DEFAULT_ORDER[:10]]
            questions = initial_questions + lifestyle
            if any(validate_question(q, gender) is None for q in lifestyle):
                raise HealthAnalysisGenerationError('The frozen basic template violates publication policy')
            trace.update(
                {
                    'mode': 'complete_basic',
                    'coverage': coverage,
                    'questionBindings': initial_bindings
                    + [{'questionName': q['name'], 'origin': 'template'} for q in lifestyle],
                    'factors': None,
                    'selectedQuestionCount': len(questions),
                }
            )
            result = self._result('', [], [], questions)
            return {'result': result, 'trace': trace, 'model_identity': {'calls': identities}}

        initial_questions, selected, tests, interviews = await self._review_questionnaire(
            call,
            background=initial_questions,
            coverage=coverage,
            plan=plan,
            snapshot=snapshot,
            sources=factors['sources'] if factors else [],
            tests=tests,
            interviews=interviews,
            trace=trace,
        )
        questions = initial_questions + [row['question'] for row in selected]
        trace['selectedQuestionCount'] = len(questions)
        trace['bindings'], trace['unboundConditions'] = self._bindings(plan, factors, selected)
        trace['contentBindings'] = {
            'healthReview': {
                'text': review_text,
                'materialIds': (plan.get('healthReview') or {}).get('materialIds', []),
                'targetIds': (plan.get('healthReview') or {}).get('targetIds', []),
            },
            'suggestedMaterials': deepcopy(tests),
            'advancedQuestions': deepcopy(interviews),
        }
        result = self._result(review_text, tests, interviews, questions)
        return {'result': result, 'trace': trace, 'model_identity': {'calls': identities}}

    async def _review_questionnaire(
        self, call, *, background, coverage, plan, snapshot, sources, tests, interviews, trace
    ):
        required = [question['name'] for question in background]
        base = [
            {
                'candidateId': question['name'],
                'targetId': question['name'],
                'question': question,
                'collectionGoal': question['title'],
                'background': True,
                'origin': 'template',
            }
            for question in background
        ]
        limit = MAX_QUESTIONS - len(base)
        minimum = 10 - len(base)
        order = self._order(plan)
        preferred, invalid = self._preferred_questions(plan, snapshot)
        trace['filters'].extend(invalid)
        accepted, attempted, excluded = [], {}, {}
        quality_reviews, protocol_recoveries = [], []
        draft_version, rewrites_left = 1, self.max_survey_rewrites
        # Each target contributes at most one planned question and one template.
        # Only the first backfill can retry a previously processed question.
        for round_index in range(2 + 2 * len(order)):
            askable = {row['targetId'] for row in coverage if row['status'] == 'askable'}
            retained = [*base, *accepted]
            accepted_targets = {row['targetId'] for row in accepted}
            candidates = []
            for key in order:
                if key not in askable or key in accepted_targets:
                    continue
                question = preferred.get(key) if key not in attempted else None
                origin = 'model' if question is not None else 'template'
                candidate = deepcopy(question or template_question(key))
                candidate['name'] = f'ha_{key}'
                checked = validate_question(candidate, snapshot.get('profile_gender'), target_id=key)
                if checked is None:
                    trace['filters'].append({'targetId': key, 'reason': 'invalid_candidate', 'round': round_index})
                    continue
                if round_index > 1 and checked == attempted.get(key):
                    continue
                candidates.append(
                    {'candidateId': f'q{round_index}_{key}', 'targetId': key, 'question': checked, 'origin': origin}
                )
            candidates.sort(
                key=lambda row: (
                    row['question'] == attempted.get(row['targetId']),
                    row['targetId'] in attempted,
                )
            )
            candidate_questions = {row['targetId']: row['question'] for row in candidates}
            candidates, duplicates = definite_duplicates(
                candidates, history=snapshot.get('asked_questions', []), retained=retained
            )
            candidates = candidates[: limit - len(accepted)]
            processed = {
                row['targetId']: candidate_questions[row['targetId']]
                for row in [*candidates, *duplicates]
                if not row.get('duplicateOfDraftCandidate')
            }
            initial = round_index == 0
            if not initial and not candidates:
                trace['filters'].extend(duplicates)
                # A filtered model draft can still have a distinct catalogue template.
                if not any(key not in attempted and key in preferred for key in processed):
                    break
                attempted.update(processed)
                continue
            attempted.update(processed)
            with analysis_generation_stage(AnalysisGenerationStage.REVIEW):
                checked = await review_survey(
                    call,
                    questions=[*base, *candidates] if initial else candidates,
                    retained_questions=[] if initial else retained,
                    required_background=required,
                    interviews=interviews if initial else [],
                    tests=tests if initial else [],
                    snapshot=snapshot,
                    sources=sources,
                    rules=self.survey_rules,
                    draft_version=draft_version,
                    max_rewrites=rewrites_left,
                    allow_protocol_retry=not protocol_recoveries,
                )
            draft_version = checked['draftVersion'] + 1
            rewrites_left -= checked['rewrites'] + len(checked['protocolRecoveries'])
            protocol_recoveries.extend(checked['protocolRecoveries'])
            quality_reviews.extend(checked['qualityReviews'])
            filters = [*duplicates, *checked['filters']]
            trace['filters'].extend(filters)
            excluded.update(checked['inapplicable'])
            coverage = [excluded.get(row['targetId'], row) for row in coverage]
            base = [row for row in checked['questions'] if row.get('background')]
            accepted = [row for row in checked['questions'] if not row.get('background')]
            if initial:
                interviews, tests = checked['interviews'], checked['tests']
            if len(accepted) >= minimum or not _unresolved_lifestyle_goals(coverage, accepted):
                break
            if initial and filters:
                # Keep the decisions already established for the reviewed draft.
                retained_targets = {row['targetId'] for row in accepted} | excluded.keys()
                retained_coverage = {row['targetId']: row for row in coverage if row['targetId'] in retained_targets}
                with analysis_generation_stage(AnalysisGenerationStage.COVERAGE):
                    coverage, _ = await self.coverage.assess(snapshot, call, feedback=filters)
                coverage = [retained_coverage.get(row['targetId'], row) for row in coverage]
        if len(accepted) < minimum and _unresolved_lifestyle_goals(coverage, accepted):
            raise HealthAnalysisGenerationError(
                'Insufficient questions without complete catalogue coverage',
                stage=AnalysisGenerationStage.COVERAGE,
                reason=AnalysisGenerationFailure.SURVEY_COVERAGE_INCOMPLETE,
            )

        trace['coverage'] = coverage
        trace['surveyQualityReviews'] = quality_reviews
        trace['surveyProtocolRecoveries'] = protocol_recoveries
        trace['questionBindings'] = [*base, *accepted]
        return [row['question'] for row in base], accepted, tests, interviews

    async def _prepare_materials(self, materials, call):
        if not isinstance(materials, list) or any(
            not isinstance(row, dict) or not isinstance(row.get('id'), str) or 'content' not in row for row in materials
        ):
            raise ValueError('Health Analysis snapshot materials are invalid')
        if len({row['id'] for row in materials}) != len(materials):
            raise ValueError('Health Analysis snapshot material identities are duplicated')
        accepted = [row for row in materials if row.get('kind') != 'chat']
        chats = [row for row in materials if row.get('kind') == 'chat']
        decisions = [{'id': row['id'], 'classification': 'provider_material', 'accepted': True} for row in accepted]
        for group in partition(chats, max_chars=self.batch_chars, max_batches=self.max_material_batches):
            output = await call('materials', {'materials': group})
            rows = array(output.get('items'), 'chat material classifications')
            expected = {row['id']: row for row in group}
            seen = set()
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get('id'), str)
                    or row['id'] not in expected
                    or row['id'] in seen
                ):
                    raise HealthAnalysisGenerationError('Chat material classification has an invalid identity')
                kind = row.get('classification')
                if not isinstance(kind, str) or kind not in {
                    'self_statement',
                    'self_unknown',
                    'family_history',
                    'family_history_unknown',
                    'question',
                    'other_subject',
                    'nonclinical',
                    'uncertain',
                }:
                    raise HealthAnalysisGenerationError('Chat material classification is invalid')
                original = expected[row['id']]
                if not isinstance(original['content'], str) or row.get('quote') != original['content']:
                    raise HealthAnalysisGenerationError('Chat material classification must retain the complete message')
                reason = text(row.get('reason'), 'chat classification reason')
                family_history = kind in {'family_history', 'family_history_unknown'}
                use = family_history or kind in {'self_statement', 'self_unknown'}
                decisions.append(
                    {
                        'id': row['id'],
                        'classification': kind,
                        'accepted': use,
                        'reason': reason,
                        'quote': original['content'],
                    }
                )
                if use:
                    material = {
                        **deepcopy(original),
                        'status': 'unknown' if kind in {'self_unknown', 'family_history_unknown'} else 'answered',
                    }
                    if family_history:
                        material['backgroundScope'] = 'family_history'
                    accepted.append(material)
                seen.add(row['id'])
            if seen != set(expected):
                raise HealthAnalysisGenerationError('Chat material classification omitted messages')
        return accepted, decisions

    @staticmethod
    def _usable_for_analysis(material):
        if material.get('status') == 'unknown':
            return False
        content = material.get('content')
        if material.get('kind') == 'report' and isinstance(content, dict):
            observation = content.get('observation')
            if isinstance(observation, dict) and observation.get('presence') in ('unknown', 'conflicting'):
                return False
        return True

    @staticmethod
    def _order(plan):
        preferred = plan.get('lifestyleOrder', [])
        if not isinstance(preferred, list):
            raise HealthAnalysisGenerationError('Lifestyle ordering is invalid')
        return list(
            dict.fromkeys(
                [*(key for key in preferred if isinstance(key, str) and key in DEFAULT_ORDER), *DEFAULT_ORDER]
            )
        )

    @staticmethod
    def _preferred_questions(plan, snapshot):
        questions, filters = {}, []
        for row in array(plan.get('questions', []), 'planned questions'):
            if not isinstance(row, dict) or row.get('targetId') not in DEFAULT_ORDER:
                filters.append({'reason': 'non_lifestyle_question'})
                continue
            checked = validate_question(row.get('question'), snapshot.get('profile_gender'), target_id=row['targetId'])
            if checked is None:
                filters.append({'targetId': row['targetId'], 'reason': 'invalid_question'})
            else:
                clean = remove_covered_unknown_options(row['targetId'], checked)
                if clean is not checked:
                    filters.append({'targetId': row['targetId'], 'reason': 'unknown_placeholder_removed'})
                questions.setdefault(row['targetId'], clean)
        return questions, filters

    @staticmethod
    def _plan_contents(plan, materials, factors, snapshot):
        ids = {row['id'] for row in materials}
        if set(refs(plan.get('consideredMaterialIds'), ids, 'consideredMaterialIds')) != ids:
            raise HealthAnalysisGenerationError('Integrated planning omitted personal materials')
        review = plan.get('healthReview')
        if not isinstance(review, dict):
            raise HealthAnalysisGenerationError('Integrated health review is invalid')
        review_text = text(review.get('text'), 'health review', maximum=16000)
        refs(review.get('materialIds'), ids, 'health review materialIds', required=True)
        refs(review.get('targetIds'), {row['id'] for row in factors['targets']}, 'health review targetIds')
        tests, interviews, filters = [], [], []
        sources = {row['id']: row for row in factors['sources']}
        for raw in array(plan.get('suggestedMaterials'), 'suggestedMaterials'):
            try:
                if not isinstance(raw, dict):
                    raise HealthAnalysisGenerationError('Suggested test is invalid')
                item = {key: text(raw.get(key), 'suggested test content') for key in ('name', 'purpose', 'condition')}
                item['materialIds'] = refs(raw.get('materialIds'), ids, 'test materialIds', required=True)
                item['knowledgeEvidence'] = medical_citations(raw.get('knowledgeEvidence'), sources)
                tests.append({'candidateId': f'test_{len(tests)}', **item})
            except HealthAnalysisGenerationError:
                filters.append({'kind': 'tests', 'reason': 'invalid_or_unsupported_test'})
        for raw in array(plan.get('advancedQuestions'), 'advancedQuestions'):
            try:
                if not isinstance(raw, dict):
                    raise HealthAnalysisGenerationError('Open question is invalid')
                item = {
                    key: text(raw.get(key), 'open question content')
                    for key in ('question', 'purpose', 'collectionGoal')
                }
                if not all(suitable_text(value, snapshot.get('profile_gender')) for value in item.values()):
                    raise HealthAnalysisGenerationError('Open question violates applicability policy')
                item['materialIds'] = refs(raw.get('materialIds'), ids, 'open question materialIds', required=True)
                interviews.append({'candidateId': f'interview_{len(interviews)}', **item})
            except HealthAnalysisGenerationError:
                filters.append({'kind': 'interviews', 'reason': 'invalid_or_inapplicable_open_question'})
        if len(tests) > 8:
            filters.append({'kind': 'tests', 'reason': 'list_budget', 'filteredCount': len(tests) - 8})
        if len(interviews) > 12:
            filters.append({'kind': 'interviews', 'reason': 'list_budget', 'filteredCount': len(interviews) - 12})
        return review_text, tests[:8], interviews[:12], filters

    @staticmethod
    def _bindings(plan, factors, selected):
        candidates = {row['id']: row for row in (factors or {}).get('candidates', [])}
        questions = {row['targetId']: row['question']['name'] for row in selected}
        bindings = []
        for row in array(plan.get('factorBindings', []), 'factor bindings'):
            if (
                not isinstance(row, dict)
                or not isinstance(row.get('candidateId'), str)
                or row['candidateId'] not in candidates
            ):
                continue
            candidate = candidates[row['candidateId']]
            if (
                not isinstance(row.get('conditionId'), str)
                or row['conditionId'] not in {c['id'] for c in candidate['conditions']}
                or not isinstance(row.get('targetId'), str)
                or row.get('targetId') not in questions
            ):
                continue
            bindings.append({**deepcopy(row), 'questionName': questions[row['targetId']]})
        unbound = [
            {'candidateId': candidate['id'], 'conditionId': condition['id'], 'reason': 'no_published_question'}
            for candidate in candidates.values()
            for condition in candidate['conditions']
            if not any(b['candidateId'] == candidate['id'] and b['conditionId'] == condition['id'] for b in bindings)
        ]
        return bindings, unbound

    @staticmethod
    def _result(review, tests, interviews, questions):
        schema = build_schema(questions)
        if schema is not None:
            from modules.health_analysis.surveys import decode_surveys

            decode_surveys([{'schema': schema, 'answer': {}}])
        return {
            'healthReview': review,
            'suggestedMaterials': '\n'.join(
                f'{i}. {row["name"]}：{row["purpose"]}；{row["condition"]}' for i, row in enumerate(tests, 1)
            ),
            'advancedQuestions': '\n'.join(
                f'{i}. {row["question"]}（{row["purpose"]}）' for i, row in enumerate(interviews, 1)
            ),
            'advancedSurveySchema': schema,
        }
