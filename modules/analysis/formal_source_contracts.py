"""Fixed public commands for formal Document publication, separate from preparation."""

from dataclasses import dataclass
from typing import Protocol

from modules.analysis.contracts import UnsupportedOutputVersionError
from modules.analysis.report_publication_contracts import DocumentPreparedPublicationInput
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import digest

FORMAL_SOURCE_REVISION = 'document-formal-source-v1'
FORMAL_REPORT_REUSE_REVISION = 'document-formal-source-v2'
FORMAL_TERMINAL = frozenset({'completed', 'not_published', 'failed'})


class DocumentFormalSourceConflict(ValueError):
    """A fixed command, selected route, owner or previous publication changed."""


class DocumentFormalSourceLeaseLost(RuntimeError):
    """Only the worker with a current durable claim may advance this command."""


@dataclass(frozen=True, slots=True)
class DocumentFormalSourceCommand:
    scope: DocumentScope
    request_id: str
    operation: str
    inputs: tuple[DocumentPreparedPublicationInput, ...]
    command_revision: str

    def __post_init__(self):
        if not isinstance(self.scope, DocumentScope):
            raise TypeError('an explicit Document scope is required')
        if (
            not isinstance(self.request_id, str)
            or not self.request_id
            or self.request_id.strip() != self.request_id
            or len(self.request_id) > 256
        ):
            raise ValueError('a fixed normalized request ID is required')
        if self.command_revision not in {FORMAL_SOURCE_REVISION, FORMAL_REPORT_REUSE_REVISION}:
            raise UnsupportedOutputVersionError('the formal Document command implementation is unavailable')
        if self.operation not in {'base', 'report'}:
            raise ValueError('the formal command must select base or report')
        if self.command_revision == FORMAL_REPORT_REUSE_REVISION and self.operation != 'report':
            raise UnsupportedOutputVersionError('formal v2 only supports fixed Report projection reuse')
        if not isinstance(self.inputs, tuple) or not 1 <= len(self.inputs) <= 100:
            raise ValueError('one to one hundred ordered fixed inputs are required')
        if any(not isinstance(item, DocumentPreparedPublicationInput) for item in self.inputs):
            raise TypeError('formal publication requires exact prepared Document inputs')
        if self.operation == 'base' and len(self.inputs) != 1:
            raise ValueError('a base command selects one fixed input')

    @property
    def command_id(self):
        return 'document_formal_' + digest([self.scope.as_dict(), self.request_id])

    def as_dict(self):
        return {
            'scope': self.scope.as_dict(),
            'requestId': self.request_id,
            'operation': self.operation,
            'inputs': [item.as_dict() for item in self.inputs],
            'commandRevision': self.command_revision,
        }

    @classmethod
    def from_dict(cls, value):
        if (
            not isinstance(value, dict)
            or set(value) != {'scope', 'requestId', 'operation', 'inputs', 'commandRevision'}
            or not isinstance(value['scope'], dict)
            or set(value['scope']) != {'userId', 'subjectId'}
        ):
            raise DocumentFormalSourceConflict('the complete persisted formal command is required')
        return cls(
            DocumentScope(value['scope']['userId'], value['scope']['subjectId']),
            value['requestId'],
            value['operation'],
            tuple(DocumentPreparedPublicationInput.from_dict(item) for item in value['inputs']),
            value['commandRevision'],
        )


@dataclass(frozen=True, slots=True)
class DocumentFormalSourcePolicy:
    deadline_seconds: int = 86400
    lease_seconds: int = 120
    io_timeout_seconds: int = 20
    retry_seconds: int = 30
    max_transport_failures: int = 8

    def __post_init__(self):
        for value in (
            self.deadline_seconds,
            self.lease_seconds,
            self.io_timeout_seconds,
            self.retry_seconds,
            self.max_transport_failures,
        ):
            if type(value) is not int or value < 1:
                raise ValueError('formal source bounds must be positive exact integers')
        if self.io_timeout_seconds * 2 >= self.lease_seconds:
            raise ValueError('formal source calls must fit inside the durable lease')


class DocumentFormalSourcePort(Protocol):
    async def submit(self, command: DocumentFormalSourceCommand) -> dict:
        """Persist fixed inputs before downstream work; acknowledgement is not application."""
        ...

    async def execute(self, command: DocumentFormalSourceCommand) -> dict:
        """Submit and advance within the fixed bound, returning pending without waiting for application."""
        ...

    async def prepare_report(self, command: DocumentFormalSourceCommand):
        """Prepare the fixed v16 business projection independently of publication/application."""
        ...

    async def read_report_projection(self, *, scope: DocumentScope, projection_ref: str):
        """Read a previously prepared projection without extraction, publication or recovery."""
        ...

    async def read_receipt(self, *, scope: DocumentScope, command_id: str) -> dict:
        """Purely read the existing command result without generation or recovery."""
        ...
