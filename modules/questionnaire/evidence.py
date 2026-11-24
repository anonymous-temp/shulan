"""Questionnaire-owned evidence rules port and fixed execution identity."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol

LEGACY_EVIDENCE_RULE_SELECTION = {
    'contractVersion': 'profile.statement-evidence.v1',
    'implementationRevision': 'profile-statement-evidence-v1',
}


class QuestionnaireEvidenceVersionError(ValueError):
    """The accepted evidence rules are unavailable or no longer match execution."""


def evidence_rule_selection(value):
    if (
        not isinstance(value, dict)
        or set(value) != {'contractVersion', 'implementationRevision'}
        or any(not isinstance(item, str) or not item or item.strip() != item for item in value.values())
    ):
        raise QuestionnaireEvidenceVersionError('Questionnaire evidence selection must name an exact version')
    return deepcopy(value)


@dataclass(frozen=True, slots=True)
class QuestionnaireStatementAssessment:
    statement_kind: str | None
    subject_kind: str | None
    presence: str | None


class QuestionnaireEvidencePolicyPort(Protocol):
    def execution_selection(self) -> dict: ...
    def classify_context(self, *, question: str, dimension: str | None) -> str | None: ...
    def assess_statement(
        self, *, kind: str, label: str, answer, question: str, dimension: str | None
    ) -> QuestionnaireStatementAssessment: ...


def require_evidence_selection(value, expected, *, legacy=False):
    if value is None and legacy:
        value = LEGACY_EVIDENCE_RULE_SELECTION
    if evidence_rule_selection(value) != evidence_rule_selection(expected):
        raise QuestionnaireEvidenceVersionError('Questionnaire requires unavailable evidence rules')


def preparation_evidence_selection(prepared, expected):
    if not isinstance(prepared, dict):
        return
    # Missing metadata is only the explicitly frozen historical v1 mapping.
    # A present null value is corruption, not permission to resolve latest.
    legacy = 'evidenceRuleSelection' not in prepared
    if prepared.get('pipelineVersion') not in {None, 'factor-assessment-v1'}:
        raise QuestionnaireEvidenceVersionError('Unknown legacy factor preparation version')
    require_evidence_selection(prepared.get('evidenceRuleSelection'), expected, legacy=legacy)
