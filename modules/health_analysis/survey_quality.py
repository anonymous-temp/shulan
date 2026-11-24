"""Joint review and bounded correction of the actual supplement questionnaire."""

from modules.health_analysis.contracts import AnalysisGenerationFailure
from modules.health_analysis.contracts import AnalysisGenerationStage
from modules.health_analysis.contracts import analysis_generation_stage
from modules.health_analysis.deduplication import SurveyReviewProtocolError
from modules.health_analysis.deduplication import check_review_output
from modules.health_analysis.deduplication import definite_duplicates
from modules.health_analysis.deduplication import normalized_title
from modules.health_analysis.deduplication import review_candidates
from modules.health_analysis.factors import HealthAnalysisGenerationError
from modules.health_analysis.factors import array
from modules.health_analysis.factors import personal_citations
from modules.health_analysis.factors import refs
from modules.health_analysis.factors import text
from modules.health_analysis.policy import BACKGROUND_UNKNOWN_TEXT
from modules.health_analysis.policy import LIFESTYLE_TARGETS
from modules.health_analysis.policy import POLICY_VERSION
from modules.health_analysis.policy import build_schema
from modules.health_analysis.policy import remove_covered_unknown_options
from modules.health_analysis.policy import validate_question

_BACKGROUND_EDITABLE_FIELDS = ('title', 'description', 'choices')
_EDITABLE_FIELDS = (*_BACKGROUND_EDITABLE_FIELDS, 'showNoneItem', 'noneValue', 'noneText')
_OPTIONAL_FIELDS = ('description', 'showNoneItem', 'noneValue', 'noneText')
_ISSUE_FIELDS = {
    'question_applicability': ('question',),
    'question_wording': ('title', 'description'),
    'options_match_question': ('choices', 'showNoneItem', 'noneValue', 'noneText'),
    'unknown_option': ('choices', 'noneText'),
    'bedtime_clock': ('title', 'description', 'choices'),
    'sleep_duration_scope': ('title', 'description'),
    'rest_schedule_scene': ('title', 'description'),
    'diet_option_categories': ('choices',),
}
_NO_ALCOHOL = frozenset(
    {'不饮酒', '从不饮酒', '不喝酒', '从不喝酒', '我不喝酒', '我从不喝酒', '我不饮酒', '我从不饮酒'}
)


async def review_survey(
    call,
    *,
    questions,
    retained_questions,
    required_background,
    interviews,
    tests,
    snapshot,
    sources,
    rules,
    draft_version,
    max_rewrites,
    allow_protocol_retry,
):
    rows = {row['candidateId']: row for row in [*retained_questions, *questions]}
    pending = {row['candidateId'] for row in questions}
    _check_background(rows, required_background)
    rule_ids = {row['ruleId'] for row in rules if row['scope'] == 'survey'}
    question_rules = {
        rule['ruleId']: rule.get('targetIds') for rule in rules if rule['scope'] in ('question', 'option')
    }
    audit, filters, excluded = [], [], {}
    rewrites = 0
    protocol_recoveries = []
    protocol_feedback = None
    recheck_survey = False
    approved_interviews, approved_tests = [], []
    while pending or recheck_survey or interviews or tests:
        schema = build_schema([row['question'] for row in rows.values()])
        survey = None
        if schema is not None and (pending or recheck_survey):
            survey = {
                'draftVersion': draft_version,
                'policyVersion': POLICY_VERSION,
                'title': schema['title'],
                'description': schema['description'],
                'questionIds': list(rows),
                'requiredBackgroundTargets': required_background,
                'rules': rules,
                'previousFindings': [item for item in audit[-1]['surveyFindings'] if item['status'] != 'passed']
                if audit
                else [],
                'previousQuestionFindings': [item for item in audit[-1]['findings'] if item['candidateId'] in pending]
                if audit
                else [],
            }
        if survey is not None and protocol_feedback is not None:
            survey['protocolFeedback'] = protocol_feedback
        try:
            checked = await review_candidates(
                call,
                questions=[_for_review(row) for key, row in rows.items() if key in pending],
                interviews=interviews,
                tests=tests,
                snapshot=snapshot,
                sources=sources,
                retained_questions=[_for_review(row) for key, row in rows.items() if key not in pending],
                survey=survey,
            )
            if survey is not None:
                with check_review_output(AnalysisGenerationFailure.REVIEW_RULES_INVALID):
                    findings = _survey_findings(checked, rule_ids, set(rows), draft_version)
                with check_review_output(AnalysisGenerationFailure.REVIEW_ISSUES_INVALID):
                    question_issues = {
                        key: _question_issues(verdict.get('issues'), question_rules, rows[key]['targetId'])
                        for key, verdict in checked['questionVerdicts'].items()
                    }
        except SurveyReviewProtocolError as error:
            # No verdict from a malformed reply is applied to the current draft.
            if not allow_protocol_retry or protocol_recoveries or rewrites >= max_rewrites:
                raise
            protocol_feedback = error.reason.value
            protocol_recoveries.append({'draftVersion': draft_version, 'reason': protocol_feedback})
            continue

        protocol_feedback = None
        filters.extend(checked['filters'])
        if not audit:
            approved_interviews, approved_tests = checked['interviews'], checked['tests']
        interviews, tests = [], []
        if survey is None:
            break

        approved = {row['candidateId']: row for row in checked['questions']}
        decisions = checked['questionVerdicts']
        rejected = {row['candidateId']: row for row in checked['filters'] if row['kind'] == 'questions'}
        issues, removed = {}, []
        for key, verdict in decisions.items():
            row = rows[key]
            notes = question_issues[key]

            if key in approved and not notes:
                rows[key] = {**row, 'semanticReview': verdict, 'ageApplicability': approved[key]['ageApplicability']}
                continue
            exclusion = _inapplicable(row, verdict, rejected.get(key, {}), snapshot)
            decision = verdict.get('verdict')
            if decision == 'inapplicable' and exclusion is None:
                notes.append(_issue('question_applicability', 'question', '现有证据无法确认该采集目标对本人不适用。'))
            if exclusion is not None:
                excluded[row['targetId']] = exclusion
            elif (
                notes or row.get('background') or row.get('rewritten') or decision in ('approved', 'requires_revision')
            ):
                issues[key] = notes or [_issue('question_applicability', 'question', verdict['reason'].strip()[:300])]
                continue

            removed.append(key)
            rows.pop(key)

        failed_rules = [item for item in findings if item['status'] != 'passed']
        for item in failed_rules:
            if set(item['questionIds']).isdisjoint(removed):
                for key in item['questionIds']:
                    issues.setdefault(key, []).append(_issue(item['ruleId'], 'question', item['reason']))

        audit.append(
            {
                'draftVersion': draft_version,
                'surveyFindings': findings,
                'findings': [{'candidateId': key, 'issues': notes} for key, notes in issues.items()],
                'removedQuestionIds': removed,
                'reviewedQuestionIds': list(decisions),
                'questionDecisions': [
                    {'candidateId': key, 'verdict': row['verdict'], 'reason': row['reason']}
                    for key, row in decisions.items()
                ],
            }
        )
        _check_background(rows, required_background)
        if not rows or not (issues or failed_rules):
            break
        remaining_rewrites = max_rewrites - rewrites - len(protocol_recoveries)
        if remaining_rewrites <= 0:
            raise HealthAnalysisGenerationError(
                'Joint survey review did not pass within the rewrite budget',
                reason=AnalysisGenerationFailure.SURVEY_REVIEW_EXHAUSTED,
            )

        previous = {key: rows[key]['question'] for key in issues}
        with analysis_generation_stage(AnalysisGenerationStage.SURVEY_REWRITE):
            revised, attempts = await _rewrite_questions(
                call, rows, decisions, issues, snapshot, rules, max_attempts=remaining_rewrites
            )
        audit[-1]['revisions'] = [
            {
                'candidateId': key,
                'changes': {
                    field: {'from': previous[key].get(field), 'to': rows[key]['question'].get(field)}
                    for field in _EDITABLE_FIELDS
                    if previous[key].get(field) != rows[key]['question'].get(field)
                },
            }
            for key in revised
        ]
        pending = set(revised)
        rewrites += attempts
        draft_version += 1
        # A survey finding can require another global check even if a removal
        # resolved the only named question and none of the surviving text changed.
        recheck_survey = not pending

    return {
        'questions': list(rows.values()),
        'interviews': approved_interviews,
        'tests': approved_tests,
        'filters': filters,
        'inapplicable': excluded,
        'qualityReviews': audit,
        'protocolRecoveries': protocol_recoveries,
        'rewrites': rewrites,
        'draftVersion': draft_version,
    }


def _for_review(row):
    goal = row['collectionGoal'] if row.get('background') else LIFESTYLE_TARGETS[row['targetId']]['question']
    return {
        'candidateId': row['candidateId'],
        'targetId': row['targetId'],
        'background': bool(row.get('background')),
        'collectionGoal': goal,
        'question': row['question'],
    }


def _check_background(rows, required):
    actual = [row['targetId'] for row in rows.values() if row.get('background')]
    if actual != required or [row['targetId'] for row in list(rows.values())[: len(required)]] != required:
        raise HealthAnalysisGenerationError('Survey draft does not preserve the required background questions')


def _survey_findings(checked, expected_rules, question_ids, draft_version):
    if type(checked['draftVersion']) is not int or checked['draftVersion'] != draft_version:
        raise SurveyReviewProtocolError(
            'Joint survey review did not match the current draft version',
            reason=AnalysisGenerationFailure.REVIEW_VERSION_MISMATCH,
        )
    findings = {}
    for row in array(checked['surveyFindings'], 'joint survey review findings'):
        rule_id = row.get('ruleId') if isinstance(row, dict) else None
        if not isinstance(rule_id, str) or rule_id not in expected_rules or rule_id in findings:
            raise HealthAnalysisGenerationError('Joint survey review returned an invalid survey rule identity')
        status = row.get('status')
        if status not in ('passed', 'requires_revision', 'uncertain'):
            raise HealthAnalysisGenerationError('Joint survey review returned an invalid rule verdict')
        findings[rule_id] = {
            'ruleId': rule_id,
            'status': status,
            'questionIds': refs(row.get('questionIds'), question_ids, 'survey rule questionIds'),
            'reason': text(row.get('reason'), 'survey rule reason', maximum=300),
        }
    if findings.keys() != expected_rules:
        raise SurveyReviewProtocolError(
            'Joint survey review omitted survey rules', reason=AnalysisGenerationFailure.REVIEW_RULES_MISSING
        )
    return list(findings.values())


def _issue(rule_id, field, reason):
    return {'ruleId': rule_id, 'field': field, 'reason': reason}


def _question_issues(value, question_rules, target_id):
    issues = []
    for item in array(value, 'invalid question issue list'):
        rule_id = item.get('ruleId') if isinstance(item, dict) else None
        if (
            not isinstance(rule_id, str)
            or rule_id not in question_rules
            or item.get('field') not in _ISSUE_FIELDS.get(rule_id, ())
        ):
            raise HealthAnalysisGenerationError('Joint survey review returned an invalid question issue')
        targets = question_rules[rule_id]
        if (targets is not None and target_id not in targets) or (
            rule_id == 'unknown_option' and target_id not in LIFESTYLE_TARGETS
        ):
            raise HealthAnalysisGenerationError('Joint survey review returned an invalid question issue')
        issues.append(
            _issue(rule_id, item['field'], text(item.get('reason'), 'invalid question issue reason', maximum=300))
        )
    return issues


def _inapplicable(row, verdict, rejected, snapshot):
    # Candidate-level age or wording restrictions cannot close the catalogue goal.
    if row['targetId'] != 'alcohol_amount' or verdict.get('verdict') != 'inapplicable':
        return None
    materials = {
        material['id']: material for material in snapshot.get('personal_materials', []) if _alcohol_evidence(material)
    }
    try:
        evidence = personal_citations(verdict.get('materialEvidence', []), materials)
    except HealthAnalysisGenerationError:
        return None
    if not evidence or any(citation['quote'].rstrip('。.!！') not in _NO_ALCOHOL for citation in evidence):
        return None
    return {
        'targetId': row['targetId'],
        'status': 'inapplicable',
        'historyRefs': [],
        'materialRefs': list(dict.fromkeys(citation['evidenceId'] for citation in evidence)),
        'reason': verdict['reason'].strip()[:300],
        'evidence': evidence,
        'ageApplicability': rejected.get('ageApplicability', {}),
    }


def _alcohol_evidence(material):
    if material.get('status') == 'unknown' or material.get('backgroundScope') == 'family_history':
        return False
    if material.get('kind') == 'chat':
        return True
    content = material.get('content')
    if material.get('kind') != 'answer' or not isinstance(content, dict):
        return False
    question = content.get('question')
    title = question.get('title') if isinstance(question, dict) else None
    return isinstance(title, str) and normalized_title(title) in {
        normalized_title(LIFESTYLE_TARGETS[key]['question']) for key in ('alcohol', 'alcohol_amount')
    }


async def _rewrite_questions(call, rows, decisions, issues, snapshot, rules, *, max_attempts):
    changed, unresolved = [], {}
    for key, notes in issues.items():
        proposal = decisions.get(key, {}).get('replacement')
        problem = _apply_revision(rows, key, proposal, snapshot)
        if problem is None:
            changed.append(key)
        else:
            unresolved[key] = [*notes, _issue('revision_validation', 'question', problem)]

    if not unresolved:
        return changed, 1
    for attempt in range(1, max_attempts + 1):
        try:
            output = await call(
                'survey_rewrite',
                {
                    'questions': [_for_review(rows[key]) for key in unresolved],
                    'feedback': [{'candidateId': key, 'issues': notes} for key, notes in unresolved.items()],
                    'surveyRules': rules,
                },
            )
        except HealthAnalysisGenerationError as error:
            if error.reason is not AnalysisGenerationFailure.INVALID_MODEL_RESPONSE:
                raise
            output = {}

        replacements = _responses(output, set(unresolved))
        remaining = {}
        for key in unresolved:
            problem = (
                '改写响应须逐一返回待改题的标识和题面。'
                if replacements is None
                else _apply_revision(rows, key, replacements[key].get('question'), snapshot)
            )
            if problem is None:
                changed.append(key)
            else:
                remaining[key] = [*issues[key], _issue('revision_validation', 'question', problem)]
        if not remaining:
            return changed, attempt
        unresolved = remaining

    raise HealthAnalysisGenerationError(
        'Survey rewrite did not produce a valid correction within the rewrite budget',
        reason=AnalysisGenerationFailure.SURVEY_REWRITE_INVALID,
    )


def _apply_revision(rows, key, proposal, snapshot):
    if not isinstance(proposal, dict):
        return '请给出能直接应用的题干、说明或选项改稿。'
    row = rows[key]
    previous = row['question']
    question = previous.copy()
    editable_fields = _BACKGROUND_EDITABLE_FIELDS if row.get('background') else _EDITABLE_FIELDS
    for field in editable_fields:
        if field in proposal:
            if field in _OPTIONAL_FIELDS and proposal[field] is None:
                question.pop(field, None)
            else:
                question[field] = proposal[field]
    if row.get('background') and 'choices' in proposal and isinstance(question['choices'], list):
        unknown = next(choice for choice in previous['choices'] if choice.get('text') == BACKGROUND_UNKNOWN_TEXT)
        if unknown not in question['choices']:
            question['choices'] = [*question['choices'], unknown]
    question = validate_question(question, snapshot.get('profile_gender'), target_id=row['targetId'])
    if question is None:
        return '改稿未通过题型、选项或适用性规则，请重新修正。'
    question = remove_covered_unknown_options(row['targetId'], question)
    if question == previous:
        return '仍需实际修正原题指出的问题。'
    revised = {**row, 'question': question, 'rewritten': True}
    _, duplicates = definite_duplicates(
        [revised],
        history=() if row.get('background') else snapshot.get('asked_questions', []),
        retained=[retained for other, retained in rows.items() if other != key],
    )
    if duplicates:
        return f'改写后与已发或本卷题目重复（{duplicates[0]["reason"]}）。'
    revised.pop('semanticReview', None)
    revised.pop('ageApplicability', None)
    rows[key] = revised
    return None


def _responses(output, expected):
    answers = output.get('questions')
    if not isinstance(answers, list):
        return None
    rows = {}
    for row in answers:
        key = row.get('candidateId') if isinstance(row, dict) else None
        if not isinstance(key, str) or key not in expected or key in rows:
            return None
        rows[key] = row
    return rows if rows.keys() == expected else None
