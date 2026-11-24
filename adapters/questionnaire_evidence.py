"""Translate the public fixed Profile policy into Questionnaire-owned decisions."""

from __future__ import annotations

from copy import deepcopy

from pydantic import ValidationError

from modules.health_profile.application import create_profile_evidence_policy
from modules.health_profile.evidence_contracts import ProfileEvidenceContextResultV1
from modules.health_profile.evidence_contracts import ProfileEvidenceContextV1
from modules.health_profile.evidence_contracts import ProfileEvidencePolicySelection
from modules.health_profile.evidence_contracts import ProfileStatementAssessmentV1
from modules.health_profile.evidence_contracts import ProfileStatementEvidenceV1
from modules.questionnaire.evidence import QuestionnaireEvidenceVersionError
from modules.questionnaire.evidence import QuestionnaireStatementAssessment
from modules.questionnaire.evidence import evidence_rule_selection
from modules.questionnaire.evidence import require_evidence_selection


class ProfileQuestionnaireEvidencePolicy:
    def __init__(self, *, provider, selection):
        self._provider = provider
        self._selection = evidence_rule_selection(selection)
        self.execution_selection()

    def execution_selection(self):
        try:
            actual = ProfileEvidencePolicySelection.model_validate(self._provider.selection).model_dump(
                mode='json', by_alias=True
            )
        except (ValidationError, AttributeError) as error:
            raise QuestionnaireEvidenceVersionError('Profile evidence selection is invalid') from error
        require_evidence_selection(actual, self._selection)
        return deepcopy(self._selection)

    def _result_selection(self, result):
        self.execution_selection()
        require_evidence_selection(result.selection.model_dump(mode='json', by_alias=True), self._selection)

    def classify_context(self, *, question, dimension):
        self.execution_selection()
        try:
            result = ProfileEvidenceContextResultV1.model_validate(
                self._provider.classify_context(ProfileEvidenceContextV1(question=question, dimension=dimension))
            )
        except ValidationError as error:
            raise QuestionnaireEvidenceVersionError('Profile evidence context contract is unavailable') from error
        self._result_selection(result)
        return result.topic

    def assess_statement(self, *, kind, label, answer, question, dimension):
        self.execution_selection()
        try:
            result = ProfileStatementAssessmentV1.model_validate(
                self._provider.assess_statement(
                    ProfileStatementEvidenceV1(
                        kind=kind, label=label, answer=answer, question=question, dimension=dimension
                    )
                )
            )
        except ValidationError as error:
            raise QuestionnaireEvidenceVersionError('Profile statement evidence contract is unavailable') from error
        self._result_selection(result)
        return QuestionnaireStatementAssessment(result.statement_kind, result.subject_kind, result.presence)


def create_questionnaire_evidence_policy(*, selection):
    fixed = evidence_rule_selection(selection)
    try:
        provider = create_profile_evidence_policy(selection=ProfileEvidencePolicySelection.model_validate(fixed))
    except ValueError as error:
        raise QuestionnaireEvidenceVersionError('The selected Questionnaire evidence policy is unavailable') from error
    return ProfileQuestionnaireEvidencePolicy(provider=provider, selection=fixed)
