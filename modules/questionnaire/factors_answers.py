"""Pure subject/assertion checks for targets first discovered in survey answers."""

from __future__ import annotations

from .evidence import QuestionnaireEvidencePolicyPort


def target_answer_refs(
    target: dict, answers: list[dict], *, evidence_policy: QuestionnaireEvidencePolicyPort
) -> list[str]:
    """Keep self-reported targets without promoting family history or interests."""
    accepted = []
    for row in answers:
        if row['id'] not in target['answerIds']:
            continue
        if target['label'] not in row['answerText']:
            continue
        question = row['question']
        dimension = {
            'health_goals': 'goal',
            'medical_history': 'medical_history',
            'family_history': 'family_history',
        }.get(row['questionName'])
        topic = evidence_policy.classify_context(question=question['title'], dimension=dimension)
        # Family answers remain available to candidate matching, but are not
        # evidence for a newly asserted disease/symptom in the respondent.
        if topic == 'family_history':
            continue
        kind = target['kind']
        if kind not in {'disease', 'symptom'}:
            accepted.append(row['id'])
            continue
        # A named, selected disease in the explicit personal-history question is
        # an assertion. A bare disease in free text needs affirmative wording.
        selected_history = topic == 'medical_history' and question['type'] in {'checkbox', 'radiogroup'}
        assertion_topic = 'medical_history' if selected_history else 'goal'
        statement_kind = 'condition' if kind == 'disease' else 'symptom'
        statement = evidence_policy.assess_statement(
            kind=statement_kind,
            label=target['label'],
            answer=row['answerQuoteUnits'] if isinstance(row['answer'], list) else row['answerText'],
            question=question['title'],
            dimension=assertion_topic,
        )
        if (
            statement is None
            or statement.statement_kind != statement_kind
            or statement.subject_kind != 'self'
            or statement.presence != 'present'
        ):
            continue
        accepted.append(row['id'])
    return accepted
