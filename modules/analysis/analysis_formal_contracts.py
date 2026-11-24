"""Freeze Analysis preparation intent before its shielded document execution starts."""

from dataclasses import asdict
from dataclasses import dataclass

from modules.analysis.formal_source_contracts import FORMAL_SOURCE_REVISION
from modules.analysis.formal_source_contracts import DocumentFormalSourceCommand
from modules.analysis.formal_source_contracts import DocumentFormalSourceConflict
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import digest

ANALYSIS_FORMAL_REVISION = 'analysis-formal-source-v1'
ANALYSIS_CURRENT_PUBLICATION = 'analysis-current-source-v1'


@dataclass(frozen=True, slots=True)
class AnalysisFormalCommand:
    scope: DocumentScope
    trace_id: str
    document_id: str
    canonical_report_id: str
    content_sha256: str
    interpretation_revision: str | None
    interpretation_round: int
    report_id: str | None
    command_revision: str = ANALYSIS_FORMAL_REVISION

    publication_revision_name = ANALYSIS_FORMAL_REVISION

    def __post_init__(self):
        if self.command_revision != self.publication_revision_name:
            raise DocumentFormalSourceConflict('the accepted Analysis delivery version is unavailable')
        if not isinstance(self.scope, DocumentScope):
            raise TypeError('the exact Analysis scope is required')
        for value in (self.trace_id, self.document_id, self.canonical_report_id, self.content_sha256):
            if not isinstance(value, str) or not value or value.strip() != value:
                raise ValueError('an exact Analysis preparation identity is required')
        if self.interpretation_revision is not None and (
            not isinstance(self.interpretation_revision, str) or not self.interpretation_revision
        ):
            raise ValueError('the original interpretation implementation is required')
        if type(self.interpretation_round) is not int or self.interpretation_round < 0:
            raise ValueError('the exact accepted interpretation round is required')
        if self.report_id is not None and (
            not isinstance(self.report_id, str)
            or not self.report_id
            or self.report_id.strip() != self.report_id
            or len(self.report_id) > 128
        ):
            raise ValueError('an exact reserved Analysis report alias is required')

    @property
    def request_id(self):
        return 'analysis:' + self.trace_id

    def base_command(self, prepared):
        return DocumentFormalSourceCommand(self.scope, self.request_id, 'base', (prepared,), FORMAL_SOURCE_REVISION)

    @property
    def command_id(self):
        return 'analysis_formal_' + digest([self.scope.as_dict(), self.request_id])

    def as_dict(self):
        result = asdict(self)
        result['scope'] = self.scope.as_dict()
        return result

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, dict) or set(raw) != set(cls.__dataclass_fields__):
            raise DocumentFormalSourceConflict('the fixed Analysis command is incomplete')
        if not isinstance(raw['scope'], dict) or set(raw['scope']) != {'userId', 'subjectId'}:
            raise DocumentFormalSourceConflict('the fixed Analysis scope differs')
        return cls(**{**raw, 'scope': DocumentScope(raw['scope']['userId'], raw['scope']['subjectId'])})
