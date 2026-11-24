"""Exact-version, synchronous Profile evidence interpretation; no I/O or models."""

from __future__ import annotations

from dataclasses import dataclass

from . import _evidence_v1
from .evidence_contracts import PROFILE_STATEMENT_EVIDENCE_V1
from .evidence_contracts import ProfileEvidenceContextResultV1
from .evidence_contracts import ProfileEvidenceContextV1
from .evidence_contracts import ProfileEvidencePolicyPort
from .evidence_contracts import ProfileEvidencePolicySelection
from .evidence_contracts import ProfileStatementAssessmentV1
from .evidence_contracts import ProfileStatementEvidenceV1


class ProfileEvidencePolicyUnsupportedError(ValueError):
    """The requested contract/revision has no executable implementation."""


@dataclass(frozen=True, slots=True)
class _StatementEvidenceV1:
    @property
    def selection(self) -> ProfileEvidencePolicySelection:
        return PROFILE_STATEMENT_EVIDENCE_V1

    def classify_context(self, request: ProfileEvidenceContextV1) -> ProfileEvidenceContextResultV1:
        request = ProfileEvidenceContextV1.model_validate(request)
        return ProfileEvidenceContextResultV1(
            selection=self.selection, topic=_evidence_v1.question_topic(request.question, request.dimension)
        )

    def assess_statement(self, request: ProfileStatementEvidenceV1) -> ProfileStatementAssessmentV1:
        request = ProfileStatementEvidenceV1.model_validate(request)
        statement = _evidence_v1.normalize_record_statement(
            fact_type='condition.diagnosis' if request.kind == 'condition' else 'symptom.reported',
            value=request.label,
            display_name=request.label,
            evidence=request.label,
            answer=request.answer,
            question=request.question,
            dimension=request.dimension,
            subject_kind='self',
            subject_hint='本人',
            stable_relationship=False,
        )
        if statement is None:
            return ProfileStatementAssessmentV1(
                selection=self.selection, statement_kind=None, subject_kind=None, presence=None
            )
        kinds = {
            'condition.diagnosis': 'condition',
            'symptom.reported': 'symptom',
            'condition.history_status': 'history',
            'family_history.reported': 'family_history',
            'family_history.condition': 'family_history',
            'goal.health': 'interest',
        }
        presence = statement.value.get('presence') if isinstance(statement.value, dict) else 'present'
        return ProfileStatementAssessmentV1(
            selection=self.selection,
            statement_kind=kinds[statement.fact_type],
            subject_kind=statement.subject_kind,
            presence=presence,
        )


def create_profile_evidence_policy(*, selection: ProfileEvidencePolicySelection) -> ProfileEvidencePolicyPort:
    """Resolve an explicitly selected implementation; missing versions never use latest."""
    selection = ProfileEvidencePolicySelection.model_validate(selection)
    if selection != PROFILE_STATEMENT_EVIDENCE_V1:
        raise ProfileEvidencePolicyUnsupportedError(
            f'Unsupported Profile evidence policy: {selection.contract_version}/{selection.implementation_revision}'
        )
    return _StatementEvidenceV1()
