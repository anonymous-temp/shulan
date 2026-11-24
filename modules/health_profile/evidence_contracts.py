"""Pure public statement-evidence contracts, independent of persistence and Agents."""

from __future__ import annotations

from copy import deepcopy
from typing import Literal
from typing import Protocol

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import model_validator


def _camel(name: str) -> str:
    head, *tail = name.split('_')
    return head + ''.join(item.title() for item in tail)


class _EvidenceContract(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        populate_by_name=True,
        extra='forbid',
        frozen=True,
        strict=True,
        revalidate_instances='always',
        allow_inf_nan=False,
    )

    @model_validator(mode='before')
    @classmethod
    def copy_input(cls, value):
        return deepcopy(value) if isinstance(value, dict) else value


class ProfileEvidencePolicySelection(_EvidenceContract):
    contract_version: str = Field(min_length=1, pattern=r'^[a-zA-Z0-9_.-]+$')
    implementation_revision: str = Field(min_length=1, pattern=r'^[a-zA-Z0-9_.-]+$')


PROFILE_STATEMENT_EVIDENCE_V1 = ProfileEvidencePolicySelection(
    contract_version='profile.statement-evidence.v1',
    implementation_revision='profile-statement-evidence-v1',
)


class ProfileEvidenceContextV1(_EvidenceContract):
    question: str
    dimension: str | None = None


class ProfileEvidenceContextResultV1(_EvidenceContract):
    selection: ProfileEvidencePolicySelection
    topic: Literal['goal', 'medical_history', 'family_history'] | None


class ProfileStatementEvidenceV1(_EvidenceContract):
    """Original selected label and answer; no authoritative fact is supplied."""

    kind: Literal['condition', 'symptom']
    label: str
    answer: JsonValue
    question: str
    dimension: str | None = None


class ProfileStatementAssessmentV1(_EvidenceContract):
    """Interpretation of source wording, never a stored or verified health fact."""

    selection: ProfileEvidencePolicySelection
    statement_kind: Literal['condition', 'symptom', 'history', 'family_history', 'interest'] | None
    subject_kind: Literal['self', 'blood_relative'] | None
    presence: Literal['present', 'absent', 'unknown', 'conflicting'] | None


class ProfileEvidencePolicyPort(Protocol):
    @property
    def selection(self) -> ProfileEvidencePolicySelection: ...

    def classify_context(self, request: ProfileEvidenceContextV1) -> ProfileEvidenceContextResultV1: ...

    def assess_statement(self, request: ProfileStatementEvidenceV1) -> ProfileStatementAssessmentV1: ...
