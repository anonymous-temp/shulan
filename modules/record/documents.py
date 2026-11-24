"""Record-owned fixed interpretation renderer and durable document checkpoints."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict
from dataclasses import dataclass
from typing import Any
from typing import ClassVar
from typing import Protocol

from modules.record.document_context import RecordDocumentContextV1
from modules.record.document_context import document_processing_summary

LEGACY_RECORD_DOCUMENT_CONSUMER = 'legacy-record-document.v1'
RECORD_DOCUMENT_RENDERER = 'record-document-renderer.v1'
RECORD_DOCUMENT_CHECKPOINT = 'record-document-preparation.v1'
RECORD_FORMAL_DOCUMENT_CHECKPOINT = 'record-document-formal-preparation.v2'
RECORD_CURRENT_DOCUMENT_CHECKPOINT = 'record-current-document-preparation.v1'
RECORD_FORMAL_SOURCE_REVISION = 'document-formal-source-v1'
RECORD_UNDERSTANDING_SELECTION = ('legacy_understanding', '1', 'legacy-understanding-v1')

# Frozen with the renderer, independently of shared Document presentation changes.
_CONTENT_MESSAGES_V1 = {
    'no_readable_text': '未识别到可读的报告正文，请检查文件内容或清晰度。',
    'no_verified_results': '暂未获得可确认的报告结果，请检查文件内容或清晰度。',
    'interpretation_incomplete': '本次解读未完成，已保留可确认的信息，可稍后重试。',
}


class RecordDocumentCheckpointError(ValueError):
    code = 'record.document_checkpoint_invalid'
    retryable = False


class RecordDocumentVersionError(RecordDocumentCheckpointError):
    code = 'record.document_version_unsupported'


class RecordCurrentDocumentConflict(RecordDocumentCheckpointError):
    """The saved current Record command differs from fixed provider material."""

    code = 'record.document_source_conflict'


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value.strip() == value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True, slots=True)
class RecordPreparedDocumentV1:
    """The durable boundary written before the formal publication command."""

    user_id: str
    subject_id: str
    session_id: str
    file_index: int
    document_id: str
    source_id: str
    source_revision: int
    material_snapshot_id: str
    facts_snapshot_id: str
    understanding_snapshot_id: str
    preparation_trace_id: str
    preparation_reused: bool
    attachment_json: str
    renderer_revision: str = RECORD_DOCUMENT_RENDERER
    selection: tuple[str, str, str] = RECORD_UNDERSTANDING_SELECTION
    expected_selection: ClassVar[tuple[str, str, str] | None] = RECORD_UNDERSTANDING_SELECTION

    def __post_init__(self):
        if (
            self.renderer_revision != RECORD_DOCUMENT_RENDERER
            or (self.expected_selection is not None and self.selection != self.expected_selection)
            or len(self.selection) != 3
            or not all(_identifier(value) for value in self.selection)
        ):
            raise RecordDocumentVersionError(
                'the exact Record document renderer or understanding version is unavailable'
            )
        if not all(
            _identifier(value)
            for value in (
                self.user_id,
                self.subject_id,
                self.session_id,
                self.document_id,
                self.source_id,
                self.material_snapshot_id,
                self.facts_snapshot_id,
                self.understanding_snapshot_id,
                self.preparation_trace_id,
            )
        ):
            raise RecordDocumentCheckpointError('a prepared document requires all fixed identities')
        if type(self.file_index) is not int or self.file_index < 0:
            raise RecordDocumentCheckpointError('invalid attachment position')
        if type(self.source_revision) is not int or self.source_revision < 1:
            raise RecordDocumentCheckpointError('invalid material source revision')
        if type(self.preparation_reused) is not bool:
            raise RecordDocumentCheckpointError('preparation reuse must be the actual boolean receipt')
        try:
            attachment = json.loads(self.attachment_json)
            if not isinstance(attachment, dict):
                raise ValueError
            if not (_identifier(attachment.get('url')) or _identifier(attachment.get('documentId'))):
                raise ValueError
            object.__setattr__(self, 'attachment_json', _json(attachment))
        except (TypeError, ValueError) as error:
            raise RecordDocumentCheckpointError('invalid prepared attachment identity') from error

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            'contractVersion': RECORD_DOCUMENT_CHECKPOINT,
            'rendererRevision': self.renderer_revision,
            'scope': {'userId': self.user_id, 'subjectId': self.subject_id},
            'sessionId': self.session_id,
            'fileIndex': self.file_index,
            'attachment': json.loads(self.attachment_json),
            'documentId': self.document_id,
            'sourceId': self.source_id,
            'sourceRevision': self.source_revision,
            'materialSnapshotRef': self.material_snapshot_id,
            'factsSnapshotRef': self.facts_snapshot_id,
            'understandingSnapshotRef': self.understanding_snapshot_id,
            'selection': dict(
                zip(('profile_name', 'contract_version', 'implementation_revision'), self.selection, strict=True)
            ),
            'preparationReceipt': {
                'documentId': self.document_id,
                'traceId': self.preparation_trace_id,
                'reused': self.preparation_reused,
            },
        }

    @staticmethod
    def _checkpoint_fields(value):
        receipt = value['preparationReceipt']
        if receipt['documentId'] != value['documentId']:
            raise RecordDocumentCheckpointError('preparation receipt identifies another document')
        return {
            'user_id': value['scope']['userId'],
            'subject_id': value['scope']['subjectId'],
            'session_id': value['sessionId'],
            'file_index': value['fileIndex'],
            'document_id': value['documentId'],
            'source_id': value['sourceId'],
            'source_revision': value['sourceRevision'],
            'material_snapshot_id': value['materialSnapshotRef'],
            'facts_snapshot_id': value['factsSnapshotRef'],
            'understanding_snapshot_id': value['understandingSnapshotRef'],
            'preparation_trace_id': receipt['traceId'],
            'preparation_reused': receipt['reused'],
            'attachment_json': _json(value['attachment']),
            'renderer_revision': value['rendererRevision'],
            'selection': tuple(
                value['selection'][field] for field in ('profile_name', 'contract_version', 'implementation_revision')
            ),
        }

    @classmethod
    def from_checkpoint(cls, value: Mapping[str, Any]) -> RecordPreparedDocumentV1:
        try:
            if value['contractVersion'] != RECORD_DOCUMENT_CHECKPOINT:
                raise RecordDocumentVersionError('unsupported prepared document checkpoint')
            return cls(**cls._checkpoint_fields(value))
        except RecordDocumentCheckpointError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RecordDocumentCheckpointError('incomplete prepared document checkpoint') from error

    def check_request(self, *, user_id, session_id, file_index, attachment):
        if (self.user_id, self.session_id, self.file_index) != (user_id, session_id, file_index):
            raise RecordDocumentCheckpointError('prepared document belongs to another Record request')
        original = json.loads(self.attachment_json)
        # Creation recovery replaces the URL with its durable document reference.
        if dict(attachment) not in (original, {'documentId': self.document_id}):
            raise RecordDocumentCheckpointError('prepared document belongs to another attachment')


class RecordDocumentPublicationPending(RuntimeError):
    """An acknowledged fixed formal command has no actual base receipt yet."""

    def __init__(self, command_id):
        self.command_id = command_id
        super().__init__('fixed Document formal publication is still pending')


class RecordDocumentFormalUnavailable(RuntimeError):
    """Terminal formal-source refusal settles only this attachment as unavailable."""

    retryable = False

    def __init__(self, *, document_id, reason):
        self.document_id = document_id
        self.reason = reason
        super().__init__('fixed Document formal publication is unavailable')


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordFormalPreparedDocumentV2(RecordPreparedDocumentV1):
    formal_request_id: str
    formal_command_id: str
    formal_revision: str = RECORD_FORMAL_SOURCE_REVISION

    def __post_init__(self):
        RecordPreparedDocumentV1.__post_init__(self)
        if self.formal_revision != RECORD_FORMAL_SOURCE_REVISION:
            raise RecordDocumentVersionError('the exact Document formal command version is unavailable')
        if (
            not _identifier(self.formal_request_id)
            or len(self.formal_request_id) > 256
            or not _identifier(self.formal_command_id)
        ):
            raise RecordDocumentCheckpointError('a fixed formal command identity is required')

    def to_checkpoint(self):
        value = RecordPreparedDocumentV1.to_checkpoint(self)
        value['contractVersion'] = RECORD_FORMAL_DOCUMENT_CHECKPOINT
        value['formalCommand'] = {
            'commandId': self.formal_command_id,
            'requestId': self.formal_request_id,
            'commandRevision': self.formal_revision,
            'operation': 'base',
        }
        return value

    @classmethod
    def from_checkpoint(cls, value):
        try:
            if value['contractVersion'] != RECORD_FORMAL_DOCUMENT_CHECKPOINT:
                raise RecordDocumentVersionError('unsupported formal prepared document checkpoint')
            fixed = dict(value)
            fixed['contractVersion'] = RECORD_DOCUMENT_CHECKPOINT
            prepared = RecordPreparedDocumentV1.from_checkpoint(fixed)
            command = value['formalCommand']
            if (
                set(command) != {'commandId', 'requestId', 'commandRevision', 'operation'}
                or command['operation'] != 'base'
            ):
                raise RecordDocumentCheckpointError('the complete fixed base command is required')
            return cls(
                **asdict(prepared),
                formal_request_id=command['requestId'],
                formal_command_id=command['commandId'],
                formal_revision=command['commandRevision'],
            )
        except RecordDocumentCheckpointError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RecordDocumentCheckpointError('incomplete formal document checkpoint') from error


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordCurrentPreparedDocumentV1(RecordPreparedDocumentV1):
    lifecycle_epoch: int
    selection: tuple[str, str, str]
    expected_selection: ClassVar[None] = None

    def __post_init__(self):
        RecordPreparedDocumentV1.__post_init__(self)
        if type(self.lifecycle_epoch) is not int or self.lifecycle_epoch < 1:
            raise RecordDocumentCheckpointError('the original Profile lifecycle is required')

    @property
    def request_id(self):
        fields = RecordPreparedDocumentV1.to_checkpoint(self)
        fields['contractVersion'] = RECORD_CURRENT_DOCUMENT_CHECKPOINT
        fields['lifecycleEpoch'] = self.lifecycle_epoch
        return 'record-current-document-' + hashlib.sha256(_json(fields).encode('utf-8')).hexdigest()

    def to_checkpoint(self):
        value = RecordPreparedDocumentV1.to_checkpoint(self)
        value['contractVersion'] = RECORD_CURRENT_DOCUMENT_CHECKPOINT
        value['lifecycleEpoch'] = self.lifecycle_epoch
        value['publicationRequestId'] = self.request_id
        return value

    @classmethod
    def from_checkpoint(cls, value):
        try:
            if value['contractVersion'] != RECORD_CURRENT_DOCUMENT_CHECKPOINT:
                raise RecordDocumentVersionError('unsupported current prepared document checkpoint')
            fixed = cls(**cls._checkpoint_fields(value), lifecycle_epoch=value['lifecycleEpoch'])
            if value['publicationRequestId'] != fixed.request_id:
                raise RecordCurrentDocumentConflict('the fixed publication request identity changed')
            return fixed
        except RecordDocumentCheckpointError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RecordDocumentCheckpointError('incomplete current prepared document checkpoint') from error


_PREPARED_CHECKPOINTS = {
    RECORD_DOCUMENT_CHECKPOINT: RecordPreparedDocumentV1,
    RECORD_FORMAL_DOCUMENT_CHECKPOINT: RecordFormalPreparedDocumentV2,
    RECORD_CURRENT_DOCUMENT_CHECKPOINT: RecordCurrentPreparedDocumentV1,
}


def supports_record_prepared_document_checkpoint(version):
    return isinstance(version, str) and version in _PREPARED_CHECKPOINTS


def record_prepared_document_from_checkpoint(value):
    version = value.get('contractVersion') if isinstance(value, Mapping) else None
    if not supports_record_prepared_document_checkpoint(version):
        raise RecordDocumentVersionError('unsupported prepared document checkpoint')
    return _PREPARED_CHECKPOINTS[version].from_checkpoint(value)


@dataclass(frozen=True, slots=True)
class RecordDocumentPublicationV1:
    canonical_report_id: str
    legacy_source_revision: int
    ingestion_status: str
    ingestion_job_id: str | None = None

    def __post_init__(self):
        if not _identifier(self.canonical_report_id) or not _identifier(self.ingestion_status):
            raise RecordDocumentCheckpointError('publication must return its actual source identity and status')
        if type(self.legacy_source_revision) is not int or self.legacy_source_revision < 1:
            raise RecordDocumentCheckpointError('publication must return its actual legacy source revision')
        if self.ingestion_job_id is not None and not _identifier(self.ingestion_job_id):
            raise RecordDocumentCheckpointError('invalid ingestion job identity')

    @property
    def source_revision(self):
        return self.legacy_source_revision

    def to_checkpoint(self):
        return {
            'canonicalReportId': self.canonical_report_id,
            'legacySourceRevision': self.legacy_source_revision,
            'ingestionStatus': self.ingestion_status,
            'ingestionJobId': self.ingestion_job_id,
        }


@dataclass(frozen=True, slots=True)
class RecordCurrentDocumentPublicationV1:
    canonical_report_id: str
    status: str
    source_revision: int | None
    source_snapshot_ref: str | None
    publication_revision: int | None
    receipt_ref: str | None

    def __post_init__(self):
        statuses = ('published', 'unchanged', 'no_contribution')
        if not _identifier(self.canonical_report_id) or self.status not in statuses:
            raise RecordDocumentCheckpointError('publication must identify the actual source and status')
        if self.status == 'no_contribution':
            if any(
                item is not None
                for item in (
                    self.source_revision,
                    self.source_snapshot_ref,
                    self.publication_revision,
                    self.receipt_ref,
                )
            ):
                raise RecordDocumentCheckpointError('an archive-only outcome cannot claim a publication')
        elif (
            type(self.source_revision) is not int
            or self.source_revision < 1
            or not _identifier(self.source_snapshot_ref)
            or type(self.publication_revision) is not int
            or self.publication_revision < 1
            or (self.receipt_ref is not None and not _identifier(self.receipt_ref))
            or (self.status == 'published' and not self.receipt_ref)
        ):
            raise RecordDocumentCheckpointError('a published source requires actual durable evidence')

    def to_checkpoint(self):
        return {
            'canonicalReportId': self.canonical_report_id,
            'status': self.status,
            'sourceRevision': self.source_revision,
            'sourceSnapshotRef': self.source_snapshot_ref,
            'publicationRevision': self.publication_revision,
            'receiptRef': self.receipt_ref,
        }


RecordDocumentPublication = RecordDocumentPublicationV1 | RecordCurrentDocumentPublicationV1


class RecordPreparedDocumentPublisher(Protocol):
    """Translate this boundary to the public Document command, never private saves."""

    async def publish_prepared(self, *, prepared: RecordPreparedDocumentV1) -> RecordDocumentPublication: ...


@dataclass(frozen=True)
class RecordPreparedDocumentContextV1(RecordDocumentContextV1):
    prepared: RecordPreparedDocumentV1
    publication: RecordDocumentPublication

    def to_checkpoint(self):
        result = super().to_checkpoint()
        result['documentProjection'] = {
            'prepared': self.prepared.to_checkpoint(),
            'publication': self.publication.to_checkpoint(),
        }
        return result


def render_record_document_v1(
    interpretation: dict,
    *,
    prepared: RecordPreparedDocumentV1,
    publication: RecordDocumentPublication,
) -> RecordPreparedDocumentContextV1:
    """Pure projection of the selected understanding and actual publication receipt."""
    is_current = isinstance(prepared, RecordCurrentPreparedDocumentV1)
    if is_current != isinstance(publication, RecordCurrentDocumentPublicationV1):
        raise RecordDocumentCheckpointError('publication differs from the accepted Record consumer')
    if publication.canonical_report_id != prepared.source_id or (
        is_current and publication.source_revision not in (None, prepared.source_revision)
    ):
        raise RecordDocumentCheckpointError('publication identifies a different canonical source')
    if not isinstance(interpretation, dict):
        raise RecordDocumentCheckpointError('a fixed full interpretation is required')
    report = _public_interpretation_v1(interpretation)
    report['reportId'] = publication.canonical_report_id
    report['userId'] = prepared.user_id
    report['subjectId'] = prepared.subject_id

    revision = publication.source_revision
    if is_current:
        report.pop('sourceRevision', None)
        if isinstance(report.get('processing'), dict):
            report['processing'].pop('sourceRevision', None)
        if revision is not None:
            report['sourceRevision'] = revision
    elif 'processing' in report or revision > 1:
        report['sourceRevision'] = revision
    report.pop('documentId', None)
    return RecordPreparedDocumentContextV1(
        report=report,
        document={
            'fileIndex': prepared.file_index,
            'documentId': prepared.document_id,
            'reportId': publication.canonical_report_id,
            'reused': prepared.preparation_reused,
        },
        processing=document_processing_summary(
            prepared.file_index,
            report.get('processing'),
            document_id=prepared.document_id,
            source_revision=revision,
        ),
        prepared=prepared,
        publication=publication,
    )


def _public_interpretation_v1(result):
    content = deepcopy(result)
    content.pop('analysisEvidence', None)
    content.pop('sourceBindings', None)
    content.pop('ingestionObservations', None)
    content.pop('verifiedObservationPointers', None)
    content.pop('metadataEvidence', None)
    content.pop('expansionEvidence', None)
    for field in ('observations', 'expansion'):
        for item in content.get(field, []) if isinstance(content.get(field), list) else []:
            if isinstance(item, dict):
                for key in ('sourceId', 'evidence', 'rawFlag', 'metadataEvidence'):
                    item.pop(key, None)
    if isinstance(content.get('unresolvedItems'), list):
        fields = (
            'itemName',
            'resultValue',
            'referenceRange',
            'unit',
            'flag',
            'kind',
            'observedAt',
            'method',
            'specimen',
        )
        unresolved = []
        for original in content['unresolvedItems']:
            if not isinstance(original, dict):
                continue
            item = {field: deepcopy(original.get(field)) for field in fields}
            missing = [field for field in ('itemName', 'resultValue') if item[field] is None]
            item['missingFields'] = missing
            item['reason'] = (
                '已识别项目名称，检测结果尚未确认。' if item['itemName'] else '已识别结果值，所属检测项目尚未确认。'
            )
            if type(original.get('sourceSection')) is int:
                item['sourceSection'] = original['sourceSection']
            unresolved.append(item)
        content['unresolvedItems'] = unresolved
    processing = content.get('processing')
    if isinstance(processing, dict):
        summary = {
            key: deepcopy(processing[key])
            for key in (
                'status',
                'retryable',
                'issueCount',
                'version',
                'sourceRevision',
            )
            if key in processing
        }
        summary['warnings'] = (
            ['部分内容尚未确认，已保留可确认的结果。']
            if processing.get('status') == 'partial'
            else ['暂未获得可用的个人检测结果。']
            if processing.get('status') == 'unavailable'
            else []
        )
        if processing.get('failureKind') == 'technical' and processing.get('status') == 'partial':
            summary['warnings'] = ['部分处理因服务异常未完成，已保留可确认的信息，可稍后重试。']
        outcome = processing.get('contentOutcome')
        if isinstance(outcome, str):
            # Stored classification selects a safe explanation; exception text
            # and internal diagnostic codes never become user-facing prose.
            if (
                outcome in _CONTENT_MESSAGES_V1
                and processing.get('status') != 'complete'
                and (outcome != 'no_verified_results' or processing.get('status') == 'unavailable')
            ):
                summary['warnings'] = [_CONTENT_MESSAGES_V1[outcome]]
        content['processing'] = summary
    for section in content.get('sections', []) if isinstance(content.get('sections'), list) else []:
        if isinstance(section, dict):
            sanitized = _public_interpretation_v1(section)
            section.clear()
            section.update(sanitized)
    return content
