"""Explicit Questionnaire attachment publication; independent of Document Facts."""

import json
from dataclasses import dataclass
from typing import Protocol

from modules.analysis.complete_source_contracts import exact_text
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import canonical_json
from modules.analysis.snapshots import digest
from modules.health_profile.contracts import ProfileSourceEventV1

QUESTIONNAIRE_PUBLICATION_SELECTION = 'document-questionnaire-attachment-publication-v1'
QUESTIONNAIRE_COMMAND_CONTRACT = 'document.questionnaire-attachment-command.v1'
QUESTIONNAIRE_RECEIPT_CONTRACT = 'document.questionnaire-attachment-receipt.v1'
SUPPORTED_QUESTIONNAIRE_MATERIALS = frozenset({'profile.questionnaire-source.v2'})
SUBMITTED_SELECTION = {
    'contractVersion': 'questionnaire.submitted-material.v1',
    'implementationRevision': 'questionnaire-submitted-material-v1',
}


class QuestionnairePublicationConflict(ValueError):
    pass


class QuestionnairePublicationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class QuestionnairePublicationRequest:
    scope: DocumentScope
    request_id: str
    submitted_reference: dict
    submitted_digest: str
    questionnaire_event: dict
    selection: str

    def __post_init__(self):
        if not isinstance(self.scope, DocumentScope):
            raise TypeError('an explicit fixed Document scope is required')
        exact_text(self.request_id, 'Questionnaire publication request')
        if self.selection != QUESTIONNAIRE_PUBLICATION_SELECTION:
            raise QuestionnairePublicationConflict('unsupported Questionnaire attachment publication implementation')
        if not isinstance(self.submitted_reference, dict) or not isinstance(self.questionnaire_event, dict):
            raise TypeError('fixed public Questionnaire reference and event JSON are required')
        reference = self.submitted_reference
        if (
            reference.get('provider') != 'questionnaire'
            or any(reference.get(key) != value for key, value in SUBMITTED_SELECTION.items())
            or type(reference.get('sourceRevision')) is not int
            or reference['sourceRevision'] != 1
            or not isinstance(self.submitted_digest, str)
            or len(self.submitted_digest) != 64
            or any(char not in '0123456789abcdef' for char in self.submitted_digest)
            or reference.get('snapshotId') != 'qsubmitted_' + self.submitted_digest
        ):
            raise QuestionnairePublicationConflict('the exact submitted Questionnaire material is required')
        event = ProfileSourceEventV1.model_validate(self.questionnaire_event)
        if (
            event.scope.model_dump(by_alias=True) != self.scope.as_dict()
            or event.source_type != 'questionnaire'
            or event.source_id != reference.get('questionnaireId')
            or event.source_revision != 1
            or event.publication_revision != 1
            or event.event_kind != 'publish'
            or event.legacy_source_revision is not None
            or event.snapshot_ref.provider != 'questionnaire'
            or event.snapshot_ref.contract_version not in SUPPORTED_QUESTIONNAIRE_MATERIALS
        ):
            raise QuestionnairePublicationConflict('the fixed Questionnaire event identity or material is unsupported')
        # Store a canonical envelope; the raw answer material is never normalized here.
        object.__setattr__(self, 'questionnaire_event', event.model_dump(mode='json', by_alias=True))
        object.__setattr__(self, 'submitted_reference', json.loads(canonical_json(reference)))

    @property
    def command_id(self):
        return 'document_questionnaire_publication_' + digest(
            [self.scope.as_dict(), self.questionnaire_event['sourceId'], self.request_id]
        )

    def as_dict(self):
        return {
            'scope': self.scope.as_dict(),
            'requestId': self.request_id,
            'submittedReference': self.submitted_reference,
            'submittedDigest': self.submitted_digest,
            'questionnaireEvent': self.questionnaire_event,
            'selection': self.selection,
        }

    @classmethod
    def from_dict(cls, value):
        if set(value) != {
            'scope',
            'requestId',
            'submittedReference',
            'submittedDigest',
            'questionnaireEvent',
            'selection',
        } or set(value['scope']) != {'userId', 'subjectId'}:
            raise QuestionnairePublicationConflict('the complete fixed Questionnaire publication request is required')
        return cls(
            DocumentScope(value['scope']['userId'], value['scope']['subjectId']),
            value['requestId'],
            value['submittedReference'],
            value['submittedDigest'],
            value['questionnaireEvent'],
            value['selection'],
        )


class QuestionnaireAttachmentPublicationPort(Protocol):
    async def prepare(
        self,
        *,
        scope: DocumentScope,
        request_id: str,
        submitted_reference: dict,
        submitted_digest: str,
        questionnaire_event: dict,
        selection: str,
    ) -> dict: ...

    async def publish(self, *, scope: DocumentScope, command_id: str) -> dict: ...

    async def read_receipt(self, *, scope: DocumentScope, command_id: str) -> dict | None: ...
