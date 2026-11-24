"""Public immutable user evidence, never domain-provided authoritative facts."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

from .contracts import ProfileContract
from .contracts import ProfileScope


class ProfileAnswerEvidenceV1(ProfileContract):
    answer_index: int = Field(strict=True, ge=0)
    source_pointer: str = Field(min_length=1, max_length=1024)
    question_id: str | None = None
    question_type: str | None = None
    intake_dimension: str | None = None
    question: str = Field(min_length=1, max_length=8192)
    answer: Any
    answered_at: str | None = None
    provided: Literal[True] = True

    @field_validator('answer')
    @classmethod
    def json_value(cls, value):
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return value


class ProfileAnswerEvidenceV2(ProfileContract):
    """Fixed Questionnaire answer_rows v1 projection, without changing v1 evidence."""

    answer_index: int = Field(strict=True, ge=0)
    source_pointer: str = Field(strict=True, min_length=1, max_length=1039)
    question_id: str = Field(strict=True, min_length=1, max_length=512)
    question_type: Literal['text', 'radiogroup', 'checkbox']
    intake_dimension: str = Field(strict=True, min_length=1, max_length=512)
    question: str = Field(strict=True, min_length=1, max_length=512)
    answer: str = Field(strict=True)
    answered_at: str = Field(strict=True, min_length=1, max_length=128)
    provided: Literal[True] = True


class ProfileQuestionnaireSubmissionProofV1(ProfileContract):
    """Neutral committed-submission identity shared with attachment publication."""

    contract_version: Literal['questionnaire-submission-proof.v1']
    status: Literal['submitted']
    delivery_mode: Literal['profile-source-event.v1']
    user_id: str = Field(strict=True, min_length=1, max_length=256)
    questionnaire_id: str = Field(strict=True, min_length=1, max_length=256)
    submission_id: str = Field(strict=True, min_length=1, max_length=256)
    session_id: str = Field(strict=True, min_length=1, max_length=512)
    source_revision: int = Field(strict=True, ge=1)
    submitted_at: str = Field(strict=True, min_length=1, max_length=128)
    answer_hash: str = Field(strict=True, pattern=r'^[0-9a-f]{64}$')
    raw_answer_hash: str = Field(strict=True, pattern=r'^[0-9a-f]{64}$')
    attachment_inventory_digest: str = Field(strict=True, pattern=r'^[0-9a-f]{64}$')


class ProfileUserInputFieldV1(ProfileContract):
    """A value explicitly supplied by the user; not a report/model-derived field."""

    field: Literal['name', 'gender', 'age', 'height', 'weight']
    value: Any
    provenance: Literal['user_input']


class _UserSourceSnapshot(ProfileContract):
    scope: ProfileScope
    source_id: str = Field(min_length=1, max_length=256)
    source_revision: int = Field(strict=True, ge=1)
    session_id: str = Field(min_length=1, max_length=256)

    @model_validator(mode='after')
    def bounded_json(self):
        if (
            len(json.dumps(self.model_dump(mode='json', by_alias=True), ensure_ascii=False, allow_nan=False).encode())
            > 4_000_000
        ):
            raise ValueError('public source snapshot exceeds its byte budget')
        return self


class ProfileChatSourceSnapshotV1(_UserSourceSnapshot):
    contract_version: Literal['profile.chat-source.v1'] = 'profile.chat-source.v1'
    source_type: Literal['chat'] = 'chat'
    role: Literal['user'] = 'user'
    content: str = Field(min_length=1, max_length=128 * 1024)
    created_at: datetime

    @field_validator('created_at')
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('created_at must be timezone-aware')
        return value


class ProfileChatSourceSnapshotV2(_UserSourceSnapshot):
    """Raw user text and original ISO business time; normalization belongs to Profile."""

    contract_version: Literal['profile.chat-source.v2'] = 'profile.chat-source.v2'
    source_type: Literal['chat'] = 'chat'
    role: Literal['user'] = 'user'
    content: str = Field(min_length=1, max_length=128 * 1024)
    timestamp: str = Field(min_length=1, max_length=128)


class _AnswerSnapshot(_UserSourceSnapshot):
    answers: tuple[ProfileAnswerEvidenceV1, ...] = Field(max_length=1000)

    @model_validator(mode='after')
    def unique_answers(self):
        if len({row.answer_index for row in self.answers}) != len(self.answers):
            raise ValueError('answer indexes must be unique')
        if len({row.source_pointer for row in self.answers}) != len(self.answers):
            raise ValueError('answer source pointers must be unique')
        return self


class ProfileRecordSourceSnapshotV1(_AnswerSnapshot):
    contract_version: Literal['profile.record-source.v1'] = 'profile.record-source.v1'
    source_type: Literal['record'] = 'record'
    round_number: int = Field(strict=True, ge=1)
    completed_at: str
    user_inputs: tuple[ProfileUserInputFieldV1, ...] = Field(default=(), max_length=5)

    @model_validator(mode='after')
    def unique_fields(self):
        if len({row.field for row in self.user_inputs}) != len(self.user_inputs):
            raise ValueError('basic user input fields must be unique')
        return self


class ProfileRecordSourceSnapshotV2(_UserSourceSnapshot):
    """Original Record inputs; Profile retains sanitization, extraction and governance."""

    contract_version: Literal['profile.record-source.v2'] = 'profile.record-source.v2'
    source_type: Literal['record'] = 'record'
    round_number: int = Field(strict=True, ge=1)
    completed_at: str
    qa_history: tuple[Any, ...] = Field(default=(), max_length=1000)
    user_inputs: tuple[ProfileUserInputFieldV1, ...] = Field(default=(), max_length=5)
    hospital_data: tuple[Any, ...] = Field(default=(), max_length=1000)
    we_run_data: tuple[Any, ...] = Field(default=(), max_length=1000)
    smart_device_data: dict[str, Any] = Field(default_factory=dict, max_length=1000)

    @model_validator(mode='after')
    def unique_fields(self):
        if len({row.field for row in self.user_inputs}) != len(self.user_inputs):
            raise ValueError('basic user input fields must be unique')
        return self


class ProfileQuestionnaireSourceSnapshotV1(_AnswerSnapshot):
    contract_version: Literal['profile.questionnaire-source.v1'] = 'profile.questionnaire-source.v1'
    source_type: Literal['questionnaire'] = 'questionnaire'
    status: Literal['submitted'] = 'submitted'
    submitted_at: str


class ProfileQuestionnaireSourceSnapshotV2(_AnswerSnapshot):
    """Complete mapped answers and their fixed submitted-material proof.

    The 32 MiB budget is specific to the current Questionnaire submission layout
    (15 MiB BSON) and answer_rows v1 mapping. It does not widen any v1 contract.
    """

    contract_version: Literal['profile.questionnaire-source.v2'] = 'profile.questionnaire-source.v2'
    source_type: Literal['questionnaire'] = 'questionnaire'
    status: Literal['submitted'] = 'submitted'
    session_id: str = Field(strict=True, min_length=1, max_length=512)
    submitted_at: str = Field(strict=True, min_length=1, max_length=128)
    answers: tuple[ProfileAnswerEvidenceV2, ...] = Field(max_length=500)
    submission_proof: ProfileQuestionnaireSubmissionProofV1

    @model_validator(mode='after')
    def bounded_json(self):
        if (
            len(json.dumps(self.model_dump(mode='json', by_alias=True), ensure_ascii=False, allow_nan=False).encode())
            > 32 * 1024 * 1024
        ):
            raise ValueError('public Questionnaire v2 snapshot exceeds its byte budget')
        return self

    @model_validator(mode='after')
    def exact_submission(self):
        proof = self.submission_proof
        if (
            proof.user_id != self.scope.user_id
            or proof.questionnaire_id != self.source_id
            or proof.submission_id != self.source_id
            or proof.session_id != self.session_id
            or proof.source_revision != self.source_revision
            or proof.submitted_at != self.submitted_at
        ):
            raise ValueError('Questionnaire submission proof differs from its source snapshot')
        return self


SOURCE_SNAPSHOT_TYPES = {
    'profile.chat-source.v1': ProfileChatSourceSnapshotV1,
    'profile.chat-source.v2': ProfileChatSourceSnapshotV2,
    'profile.record-source.v1': ProfileRecordSourceSnapshotV1,
    'profile.record-source.v2': ProfileRecordSourceSnapshotV2,
    'profile.questionnaire-source.v1': ProfileQuestionnaireSourceSnapshotV1,
    'profile.questionnaire-source.v2': ProfileQuestionnaireSourceSnapshotV2,
}


def parse_profile_source_snapshot(value):
    if isinstance(value, _UserSourceSnapshot):
        value = value.model_dump(mode='json', by_alias=True)
    if not isinstance(value, dict):
        raise TypeError('a public source snapshot must be a mapping')
    model = SOURCE_SNAPSHOT_TYPES.get(value.get('contractVersion', value.get('contract_version')))
    if model is None:
        raise ValueError('unsupported public source snapshot contract')
    return model.model_validate(value)
