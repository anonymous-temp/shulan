"""Semantic catalogue coverage and separate candidate publication decisions."""

from __future__ import annotations

import unicodedata
from contextlib import contextmanager
from copy import deepcopy

from modules.health_analysis.applicability import age_applicability
from modules.health_analysis.contracts import AnalysisGenerationFailure
from modules.health_analysis.factors import HealthAnalysisGenerationError
from modules.health_analysis.factors import array
from modules.health_analysis.factors import partition
from modules.health_analysis.factors import personal_quote_units
from modules.health_analysis.factors import refs
from modules.health_analysis.factors import text
from modules.health_analysis.policy import LEGACY_LIFESTYLE_QUESTIONS
from modules.health_analysis.policy import SURVEY_DESCRIPTION
from modules.health_analysis.policy import SURVEY_PAGE_NAME
from modules.health_analysis.policy import SURVEY_TITLE
from modules.health_analysis.policy import catalogue

COVERAGE_STATES = frozenset({'askable', 'history_covered', 'material_covered', 'inapplicable', 'uncertain'})


def normalized_title(value: str) -> str:
    """Normalize typography, preserving punctuation that can change quantities.

    This is an exact duplicate guard only. Different titles still require the
    semantic coverage/review stages; shared choices alone prove no duplication.
    """
    value = unicodedata.normalize('NFKC', value)
    result = []
    for index, char in enumerate(value):
        category = unicodedata.category(char)
        if char.isspace() or category in {'Cf', 'Zs', 'Zl', 'Zp'}:
            continue
        if category.startswith('P'):
            between_digits = 0 < index < len(value) - 1 and value[index - 1].isdigit() and value[index + 1].isdigit()
            if char != '%' and not (between_digits and char in './:-'):
                continue
        result.append(char)
    return ''.join(result)


_LEGACY_LIFESTYLE_TEMPLATES = {
    normalized_title(title): {
        'id': target_id,
        'question': {
            'type': question_type,
            'choices': [{'value': str(index), 'text': label} for index, label in enumerate(labels, start=1)],
        },
    }
    for target_id, (title, question_type, labels) in LEGACY_LIFESTYLE_QUESTIONS.items()
}


def _known_lifestyle_target(row, templates):
    # An external survey's title, instructions, or page may change the scope of an otherwise identical question.
    if (
        row.get('surveyTitle') != SURVEY_TITLE
        or row.get('surveyDescription') != SURVEY_DESCRIPTION
        or row.get('pageName') != SURVEY_PAGE_NAME
    ):
        return None
    question = row['question']
    target = templates.get(normalized_title(question['title']))
    if target is not None and all(
        question.get(key) == target['question'].get(key)
        for key in ('type', 'description', 'choices', 'showNoneItem', 'noneValue', 'noneText')
    ):
        return target['id']
    return None


def definite_duplicates(questions, *, history, retained=()) -> tuple[list[dict], list[dict]]:
    historical, historical_targets = set(), set()
    for row in history:
        question = row['question']
        title = normalized_title(question['title'])
        historical.add(title)
        if target_id := _known_lifestyle_target(row, _LEGACY_LIFESTYLE_TEMPLATES):
            historical_targets.add(target_id)
    seen = {normalized_title(row['question']['title']): True for row in retained}
    accepted, filtered = [], []
    for row in questions:
        key = normalized_title(row['question']['title'])
        reason = None
        if key in historical or row.get('targetId') in historical_targets:
            reason = 'exact_history_duplicate'
        elif key in seen:
            reason = 'exact_new_duplicate'
        if reason is not None:
            duplicate = {
                'candidateId': row['candidateId'],
                'targetId': row.get('targetId'),
                'kind': 'questions',
                'reason': reason,
            }
            if reason == 'exact_new_duplicate' and not seen[key]:
                duplicate['duplicateOfDraftCandidate'] = True
            filtered.append(duplicate)
        else:
            accepted.append(row)
            seen[key] = False
    return accepted, filtered


class SemanticCoverage:
    VERSION = 'health-analysis-coverage-v3'

    def __init__(self, *, batch_chars=24000, max_material_batches=8):
        self.batch_chars, self.max_material_batches = batch_chars, max_material_batches

    async def assess(self, snapshot, call, *, background=(), feedback=None) -> tuple[list[dict], dict]:
        targets = catalogue()
        history = snapshot.get('asked_questions', [])
        materials = snapshot.get('personal_materials', [])
        background_evidence = {question['name']: [] for question in background}
        templates = {question['name']: question for question in background}
        background_titles = {normalized_title(question['title']): name for name, question in templates.items()}
        lifestyle_templates = {normalized_title(target['question']['title']): target for target in targets}
        known_lifestyle_templates = _LEGACY_LIFESTYLE_TEMPLATES | lifestyle_templates
        answer_scopes = {}
        for material in materials if background else ():
            scope = _answer_scope(material, background_titles, lifestyle_templates)
            if scope is not None:
                answer_scopes[material['id']] = scope
            if scope in templates:
                question = material['content']['question']
                template = templates[scope]
                if _matches_background_template(question, template):
                    if units := personal_quote_units(material):
                        background_evidence[scope].append({'evidenceId': material['id'], 'quote': units[0]})
        if not history and not materials:
            return (
                [
                    {
                        'targetId': t['id'],
                        'status': 'askable',
                        'historyRefs': [],
                        'materialRefs': [],
                        'reason': 'No historical questions or personal materials in this run',
                    }
                    for t in targets
                ],
                background_evidence,
            )
        inputs = [{'inputKind': 'history', 'row': row} for row in history]
        inputs.extend({'inputKind': 'material', 'row': row} for row in materials)
        groups = partition(inputs, max_chars=self.batch_chars, max_batches=self.max_material_batches)
        outcomes = {target['id']: [] for target in targets}
        for group in groups:
            asked = [item['row'] for item in group if item['inputKind'] == 'history']
            personal = [item['row'] for item in group if item['inputKind'] == 'material']
            exact_history_refs = {}
            for row in asked:
                if target_id := _known_lifestyle_target(row, known_lifestyle_templates):
                    exact_history_refs.setdefault(target_id, []).append(row['pointer'])
            payload = {
                'targets': targets,
                'askedQuestions': asked,
                'materials': personal,
                'profileGender': snapshot.get('profile_gender'),
                'feedback': feedback or [],
            }
            if background and personal:
                payload['backgroundTargets'] = [
                    {'id': question['name'], 'question': question} for question in background
                ]
            output = await call('coverage', payload)
            if 'backgroundTargets' in payload:
                self._collect_background(output.get('backgroundCoverage'), personal, answer_scopes, background_evidence)
            rows = array(output.get('coverage'), 'catalogue coverage')
            by_id = {}
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get('targetId'), str) and row['targetId'] in outcomes:
                    by_id.setdefault(row['targetId'], []).append(row)
            for target in targets:
                target_id = target['id']
                if history_refs := exact_history_refs.get(target_id):
                    outcomes[target_id].append(
                        {
                            'targetId': target_id,
                            'status': 'history_covered',
                            'historyRefs': history_refs,
                            'materialRefs': [],
                            'reason': 'Matches a previously published lifestyle question',
                        }
                    )
                    continue
                proposals = by_id.get(target_id, [])
                try:
                    if len(proposals) != 1:
                        raise HealthAnalysisGenerationError('Catalogue coverage is incomplete or duplicated')
                    checked = self._check(proposals[0], asked, personal)
                except HealthAnalysisGenerationError:
                    checked = {
                        'targetId': target['id'],
                        'status': 'uncertain',
                        'historyRefs': [],
                        'materialRefs': [],
                        'reason': 'Missing or invalid semantic coverage decision',
                    }
                outcomes[target['id']].append(checked)
        result = []
        for target in targets:
            pieces = outcomes[target['id']]
            states = {row['status'] for row in pieces}
            state = next(
                (
                    state
                    for state in ('history_covered', 'material_covered', 'inapplicable', 'uncertain')
                    if state in states
                ),
                'askable',
            )
            result.append(
                {
                    'targetId': target['id'],
                    'status': state,
                    'historyRefs': list(dict.fromkeys(ref for row in pieces for ref in row['historyRefs'])),
                    'materialRefs': list(dict.fromkeys(ref for row in pieces for ref in row['materialRefs'])),
                    'reason': '\n'.join(dict.fromkeys(row['reason'] for row in pieces)),
                    'decisions': pieces,
                }
            )
        return result, background_evidence

    @staticmethod
    def _collect_background(rows, materials, answer_scopes, collected):
        if not isinstance(rows, list):
            return
        by_id = {material['id']: material for material in materials}
        for target, evidence in collected.items():
            matches = [row for row in rows if isinstance(row, dict) and row.get('targetId') == target]
            if len(matches) != 1 or matches[0].get('state') != 'answered':
                continue
            citations = matches[0].get('evidence')
            if not isinstance(citations, list) or not citations:
                continue
            checked = []
            for citation in citations:
                if not isinstance(citation, dict) or not isinstance(citation.get('evidenceId'), str):
                    break
                material = by_id.get(citation['evidenceId'])
                quote = citation.get('quote')
                if (
                    material is None
                    or not isinstance(quote, str)
                    or not quote.strip()
                    or quote not in _background_quote_units(material, target, answer_scopes.get(material['id']))
                ):
                    break
                checked.append({'evidenceId': citation['evidenceId'], 'quote': quote})
            else:
                evidence.extend(citation for citation in checked if citation not in evidence)

    @staticmethod
    def _check(row, history, materials):
        state = row.get('status')
        if not isinstance(state, str) or state not in COVERAGE_STATES:
            raise HealthAnalysisGenerationError('Unknown catalogue coverage state')
        historical = refs(row.get('historyRefs'), {q['pointer'] for q in history}, 'historyRefs')
        personal = refs(row.get('materialRefs'), {m['id'] for m in materials}, 'materialRefs')
        if state == 'history_covered' and not historical:
            raise HealthAnalysisGenerationError('Historical coverage lacks a question reference')
        if state in {'material_covered', 'inapplicable'} and not personal:
            raise HealthAnalysisGenerationError('Material coverage lacks a personal reference')
        if state in {'material_covered', 'inapplicable'} and any(
            m.get('status') == 'unknown' for m in materials if m['id'] in personal
        ):
            raise HealthAnalysisGenerationError('Unknown materials do not reliably cover collection conditions')
        return {
            'targetId': row['targetId'],
            'status': state,
            'historyRefs': historical,
            'materialRefs': personal,
            'reason': text(row.get('reason'), 'coverage reason'),
        }


def _matches_background_template(question, template):
    if any(question.get(key) != template.get(key) for key in ('type', 'showNoneItem', 'noneText')):
        return False
    # Match the earlier template that omitted the trailing unknown choice.
    choices = template['choices']
    return question.get('choices') in (choices, choices[:-1])


def _answer_scope(material, background_titles, lifestyle_titles):
    content = material.get('content')
    if material.get('kind') != 'answer' or not isinstance(content, dict):
        return None
    question = content.get('question')
    if (
        not isinstance(question, dict)
        or question.get('type') not in ('radiogroup', 'checkbox')
        or not isinstance(question.get('title'), str)
    ):
        return None
    title = normalized_title(question['title'])
    if title in background_titles:
        return background_titles[title]
    if title in lifestyle_titles or _known_lifestyle_target(content, _LEGACY_LIFESTYLE_TEMPLATES) is not None:
        return 'lifestyle'
    return None


def _background_quote_units(material, target, answer_scope):
    scope = material.get('backgroundScope') or answer_scope
    if scope is not None and scope != target:
        return []
    if material.get('kind') != 'report':
        return personal_quote_units(material)
    if material.get('status') == 'unknown':
        return []
    content = material['content']
    if isinstance(content, dict):
        if 'resultValue' in content or 'rawValue' in content:
            return []
        fact_type = content.get('factType')
        if isinstance(fact_type, str):
            if fact_type.startswith(('anthropometric.', 'laboratory.')) or fact_type == 'condition.finding':
                return []
            if fact_type.startswith('family_history.') and target != 'family_history':
                return []
            if fact_type.startswith('condition.') and target != 'medical_history':
                return []
            if fact_type.startswith(('goal.', 'symptom.')) and target != 'health_goals':
                return []
        observation = content.get('observation')
        if isinstance(observation, dict) and isinstance(observation.get('description'), str):
            return [observation['description']]
    return [quote for quote in personal_quote_units(material) if any(char.isalpha() for char in quote)]


class SurveyReviewProtocolError(HealthAnalysisGenerationError):
    """The joint reviewer did not provide a usable verdict for the current draft."""


@contextmanager
def check_review_output(reason: AnalysisGenerationFailure):
    try:
        yield
    except SurveyReviewProtocolError:
        raise
    except HealthAnalysisGenerationError as error:
        raise SurveyReviewProtocolError(str(error), reason=reason) from None


async def review_candidates(
    call, *, questions, interviews, tests, snapshot, sources, retained_questions=(), survey=None
) -> dict:
    """Missing/uncertain decisions filter one candidate without closing its target."""
    definite_filters = []
    if survey is None:
        questions, definite_filters = definite_duplicates(
            questions,
            history=snapshot.get('asked_questions', []),
            retained=retained_questions,
        )
    if not questions and not interviews and not tests and survey is None:
        return {'questions': [], 'interviews': [], 'tests': [], 'filters': definite_filters}
    output = await call(
        'review',
        {
            'questions': questions,
            'interviews': interviews,
            'tests': tests,
            'askedQuestions': snapshot.get('asked_questions', []),
            'retainedQuestions': deepcopy(list(retained_questions)),
            'materials': snapshot.get('personal_materials', []),
            'profileGender': snapshot.get('profile_gender'),
            'sources': sources,
            **({'survey': survey} if survey is not None else {}),
        },
    )
    result = {'questions': [], 'interviews': [], 'tests': [], 'filters': definite_filters}
    if survey is not None:
        result['surveyFindings'] = output.get('surveyFindings')
        result['draftVersion'] = output.get('draftVersion')
        with check_review_output(AnalysisGenerationFailure.REVIEW_VERDICTS_INVALID):
            result['questionVerdicts'] = _question_verdicts(output.get('questions'), questions)
        if any(not isinstance(output.get(kind, []), list) for kind in ('interviews', 'tests')):
            raise SurveyReviewProtocolError(
                'Joint survey review returned invalid recommendation lists',
                reason=AnalysisGenerationFailure.REVIEW_RECOMMENDATIONS_INVALID,
            )
    for kind, candidates in [('questions', questions), ('interviews', interviews), ('tests', tests)]:
        raw_rows = array(output.get(kind, []), f'{kind} review')
        for candidate in candidates:
            matches = [
                row for row in raw_rows if isinstance(row, dict) and row.get('candidateId') == candidate['candidateId']
            ]
            verdict = matches[0] if len(matches) == 1 else {}
            approved = (
                verdict.get('verdict') == 'approved'
                and isinstance(verdict.get('reason'), str)
                and bool(verdict['reason'].strip())
            )
            age_decision = age_applicability(
                verdict.get('ageRule'), snapshot.get('effective_demographics', {}), sources
            )
            approved = approved and age_decision['eligible']
            if kind == 'questions':
                required = _question_approval_fields(candidate)
                approved = approved and all(verdict.get(key) is True for key in required)
            if approved:
                result[kind].append(
                    {**deepcopy(candidate), 'semanticReview': deepcopy(verdict), 'ageApplicability': age_decision}
                )
            else:
                result['filters'].append(
                    {
                        'candidateId': candidate['candidateId'],
                        'targetId': candidate.get('targetId'),
                        'kind': kind,
                        'ageApplicability': age_decision,
                        'reason': verdict.get('verdict')
                        if isinstance(verdict.get('verdict'), str)
                        and verdict['verdict']
                        in {'duplicate', 'invalid', 'uncertain', 'inapplicable', 'requires_revision'}
                        else 'uncertain',
                    }
                )
    if survey is None:
        result['questions'], final_filters = definite_duplicates(
            result['questions'],
            history=snapshot.get('asked_questions', []),
            retained=retained_questions,
        )
        result['filters'].extend(final_filters)
    return result


def _question_approval_fields(candidate):
    if candidate.get('background'):
        return ('targetAligned', 'suitable')
    return ('targetAligned', 'lifestyleOnly', 'novel', 'suitable')


def _question_verdicts(value, questions):
    expected = {row['candidateId']: row for row in questions}
    verdicts = {}
    for row in array(value, 'joint question review'):
        candidate_id = row.get('candidateId') if isinstance(row, dict) else None
        if not isinstance(candidate_id, str) or candidate_id not in expected or candidate_id in verdicts:
            raise HealthAnalysisGenerationError('Joint survey review returned an invalid question identity')
        if row.get('verdict') not in (
            'approved',
            'duplicate',
            'invalid',
            'uncertain',
            'inapplicable',
            'requires_revision',
        ):
            raise HealthAnalysisGenerationError('Joint survey review returned an invalid question verdict')
        text(row.get('reason'), 'joint survey review question reason')
        if row['verdict'] == 'approved' and any(
            not isinstance(row.get(key), bool) for key in _question_approval_fields(expected[candidate_id])
        ):
            raise HealthAnalysisGenerationError('Joint survey review returned missing or invalid approval flags')
        verdicts[candidate_id] = row
    if verdicts.keys() != expected.keys():
        raise SurveyReviewProtocolError(
            'Joint survey review omitted questions', reason=AnalysisGenerationFailure.REVIEW_QUESTIONS_MISSING
        )
    return verdicts
